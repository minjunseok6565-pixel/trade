"""Rule-only player metadata builder (SSOT-backed).

This module builds a minimal ``players`` dictionary suitable for trade rule evaluation.

Design constraints:
- Values returned here are derived from SQLite SSOT (``LeagueRepo``).
- Never read from UI caches (e.g., ``state['ui_cache']``).
- Keep schema minimal and rule-focused; do not include UI-only fields.

Phase 2 implementation notes:
- Only computes metadata for the provided ``player_ids`` (deal-scoped).
- Uses transactions_log (SSOT) to compute:
    * last_contract_action_type/date
    * signed_via_free_agency
    * acquired_via_trade / acquired_date (based on trade player_moves -> to_team)
    * trade_return_bans for the provided season_year
- Falls back to contracts.signed_date for signed_date when needed.

Known limitation / unresolved (by design, noted for future optimization):
- transactions_log has no player_id column; repo filters by player_ids by parsing payload_json in Python.
- Legacy trade payloads without player_moves are partially supported for return bans via assets_summary,
  but acquisition (to_team) cannot be reliably computed. We fail-fast when that ambiguity would matter.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id


def build_rule_players_meta(
    repo: LeagueRepo,
    player_ids: Iterable[str],
    *,
    season_year: Optional[int] = None,
    as_of_date: Optional[date] = None,
    unknown_signed_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build rule-only player metadata for the given player_ids.

    Returns a dict keyed by canonical ``player_id``.

    Args:
        repo: LeagueRepo (SSOT-backed).
        player_ids: iterable of player_id-like values.
        season_year: current season year (SSOT) for rule evaluation. (Phase 2 uses this.)
        as_of_date: "as-of" date for filtering future transactions. (Phase 2 uses this.)
        unknown_signed_date: optional fallback when a player has no active contract
            signed_date in SSOT. (e.g., "1900-01-01" to make everyone immediately eligible)
    """
    canonical_ids = _canonicalize_player_ids(player_ids)
    if not canonical_ids:
        return {}

    pid_set = set(canonical_ids)

    # SSOT reads for only the requested player_ids.
    team_ids_by_player = repo.get_team_ids_by_players(canonical_ids)
    signed_dates_by_player = repo.get_active_signed_dates_by_players(canonical_ids)

      # ---- Transactions (SSOT) ----
    # Contract action types we currently support.
    # (We keep legacy spellings in the allow-list as a safety net during dev.)
    CONTRACT_TYPES = {
        "SIGN_FREE_AGENT",
        "RE_SIGN_OR_EXTEND",
        "RELEASE_TO_FA",
        # legacy spellings (dev)
        "signing",
        "re_sign_or_extend",
        "release_to_free_agency",
    }

    # Trade tx payload type is "trade" (lowercase) in existing code.
    TRADE_TYPES = {"trade", "TRADE"}

    # 1) For last_contract_action + acquired_date we should consider *all* past tx (<= as_of_date),
    # not only current season.
    txs_all = repo.get_transactions_for_players(
        canonical_ids,
        types=sorted(CONTRACT_TYPES | TRADE_TYPES),
        season_year=None,
        up_to_date=as_of_date,
        limit=10000,
    )

    # 2) For return-to-trading-team bans we only consider current season trades.
    txs_season_trades: Sequence[Dict[str, Any]] = []
    if season_year is not None:
        txs_season_trades = repo.get_transactions_for_players(
            canonical_ids,
            types=sorted(TRADE_TYPES),
            season_year=int(season_year),
            up_to_date=as_of_date,
            limit=10000,
        )

    def _tx_type(tx: Dict[str, Any]) -> str:
        # Payload convention is "type". Some legacy paths may have tx_type.
        t = tx.get("type") or tx.get("tx_type") or ""
        return str(t)

    def _tx_date(tx: Dict[str, Any]) -> Optional[str]:
        # Prefer explicit action_date (contract events), fallback to date (generic).
        v = tx.get("action_date")
        if v is None:
            v = tx.get("date")
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    def _normalize_contract_action_type(raw: str) -> str:
        # Normalize legacy spellings to the standardized rule-facing types.
        r = str(raw or "")
        if r == "signing":
            return "SIGN_FREE_AGENT"
        if r == "re_sign_or_extend":
            return "RE_SIGN_OR_EXTEND"
        if r == "release_to_free_agency":
            return "RELEASE_TO_FA"
        return r

    # last contract action: first (most recent) contract tx for each pid (txs_all is sorted desc by date/created_at)
    last_contract_by_pid: Dict[str, Tuple[str, str]] = {}  # pid -> (action_type, action_date_iso)

    # acquisition: first (most recent) trade tx where move.to_team == current_team_id
    acquired_trade_by_pid: Dict[str, str] = {}  # pid -> acquired_date_iso

    # Track whether we saw a legacy trade payload (no player_moves) that mentioned the player.
    legacy_trade_seen: Dict[str, bool] = {pid: False for pid in canonical_ids}

    # Precompute current_team_id for each player (SSOT roster).
    current_team_by_pid: Dict[str, Optional[str]] = {}
    for pid in canonical_ids:
        t = team_ids_by_player.get(pid)
        current_team_by_pid[pid] = str(t).upper() if t is not None else None

    # ---- Pass 1: scan all txs (desc) for last_contract_action + acquired_date ----
    for tx in txs_all:
        ttype = _tx_type(tx)
        tdate = _tx_date(tx)
        if tdate is None:
            # Missing date breaks "most recent" semantics. Fail-fast.
            raise RuntimeError(f"transactions_log payload missing date/action_date for tx_type={ttype!r}")

        if ttype in CONTRACT_TYPES:
            pid = tx.get("player_id")
            if pid is None:
                continue
            pid = str(pid)
            if pid not in pid_set:
                continue
            if pid not in last_contract_by_pid:
                action_type = _normalize_contract_action_type(tx.get("action_type") or ttype)
                last_contract_by_pid[pid] = (action_type, tdate)
            continue

        if ttype in TRADE_TYPES:
            pm = tx.get("player_moves")
            if isinstance(pm, list):
                # Normalize: ensure a player appears at most once per tx.
                seen_in_tx: set[str] = set()
                for m in pm:
                    if not isinstance(m, dict):
                        continue
                    pid = str(m.get("player_id"))
                    if not pid or pid not in pid_set:
                        continue
                    if pid in seen_in_tx:
                        # Multi-team is fine; duplicate per player in same tx is not.
                        raise RuntimeError(f"trade tx has duplicate player_move entries for player_id={pid}")
                    seen_in_tx.add(pid)

                    to_team = m.get("to_team")
                    from_team = m.get("from_team")
                    # Required for acquisition logic.
                    if to_team is None:
                        legacy_trade_seen[pid] = True
                        continue

                    cur_team = current_team_by_pid.get(pid)
                    # If player currently FA, acquired-via-trade is not meaningful.
                    if cur_team is None or cur_team == "FA":
                        continue

                    if str(to_team).upper() == str(cur_team).upper():
                        # First match in descending scan is the most recent acquisition by current team.
                        if pid not in acquired_trade_by_pid:
                            acquired_trade_by_pid[pid] = tdate
                continue

            # Legacy trade payload: no player_moves. We can partially support bans via assets_summary,
            # but cannot reliably compute acquisition to_team. Mark legacy seen for involved players.
            assets = tx.get("assets")
            if isinstance(assets, dict):
                for team, a in assets.items():
                    if not isinstance(a, dict):
                        continue
                    players = a.get("players")
                    if isinstance(players, list):
                        for raw_pid in players:
                            pid = str(raw_pid)
                            if pid in pid_set:
                                legacy_trade_seen[pid] = True
            continue

    # ---- Pass 2: season trade bans ----
    bans_by_pid: Dict[str, set[str]] = {pid: set() for pid in canonical_ids}
    if season_year is not None:
        for tx in txs_season_trades:
            ttype = _tx_type(tx)
            if ttype not in TRADE_TYPES:
                continue
            tdate = _tx_date(tx)
            if tdate is None:
                raise RuntimeError(f"transactions_log payload missing date/action_date for trade tx in season_year={season_year}")

            pm = tx.get("player_moves")
            if isinstance(pm, list):
                seen_in_tx: set[str] = set()
                for m in pm:
                    if not isinstance(m, dict):
                        continue
                    pid = str(m.get("player_id"))
                    if not pid or pid not in pid_set:
                        continue
                    if pid in seen_in_tx:
                        raise RuntimeError(f"trade tx has duplicate player_move entries for player_id={pid}")
                    seen_in_tx.add(pid)

                    from_team = m.get("from_team")
                    if from_team is None:
                        continue
                    bans_by_pid[pid].add(str(from_team).upper())
                continue

            # Legacy fallback for bans: use assets_summary (outgoing players by team).
            assets = tx.get("assets")
            if isinstance(assets, dict):
                for team, a in assets.items():
                    if not isinstance(a, dict):
                        continue
                    players = a.get("players")
                    if isinstance(players, list):
                        for raw_pid in players:
                            pid = str(raw_pid)
                            if pid in pid_set:
                                bans_by_pid[pid].add(str(team).upper())


    out: Dict[str, Dict[str, Any]] = {}
    for pid in canonical_ids:
        team_id = team_ids_by_player.get(pid)
        team_id_u = str(team_id).upper() if team_id is not None else None

        # signed_date base from SSOT contracts (fallback).
        signed_date = signed_dates_by_player.get(pid)

        # last contract action from transactions, if present.
        last_action_type = None
        last_action_date = None
        if pid in last_contract_by_pid:
            last_action_type, last_action_date = last_contract_by_pid[pid]

        # Prefer last contract action date as signed_date when contracts table lacks a value.
        if signed_date is None and last_action_date is not None:
            signed_date = last_action_date
        if signed_date is None and unknown_signed_date is not None:
            signed_date = unknown_signed_date

        signed_via_fa = (last_action_type == "SIGN_FREE_AGENT")

        # Acquisition computation:
        # - If currently FA: treat as not acquired via trade.
        # - Else: if we found a trade move to current team, use it.
        # - Else: default acquired_date to signed_date/last_action_date.
        acquired_via_trade = False
        acquired_date = None

        if team_id_u is None or team_id_u == "FA":
            acquired_via_trade = False
            acquired_date = signed_date or last_action_date
        else:
            if pid in acquired_trade_by_pid:
                acquired_via_trade = True
                acquired_date = acquired_trade_by_pid[pid]
            else:
                # If we saw any legacy trade payload for this player, we cannot reliably compute acquisition
                # (to_team is unknown). Fail-fast to avoid silently weakening rules.
                if legacy_trade_seen.get(pid):
                    raise RuntimeError(
                        "Cannot compute acquired_date for player without normalized trade payload "
                        f"(missing player_moves). player_id={pid}"
                    )
                acquired_via_trade = False
                acquired_date = signed_date or last_action_date

        # Return bans (current season only).
        trade_return_bans: Dict[str, list[str]] = {}
        if season_year is not None:
            teams = sorted(bans_by_pid.get(pid) or [])
            trade_return_bans[str(int(season_year))] = teams

        out[pid] = {
            # identity / current assignment
            "player_id": pid,
            # Keep both keys for now to avoid surprises if any rule expects "team_id".
            "team_id": team_id_u,
            "current_team_id": team_id_u,

            # contract / signing (transactions-driven)
            "signed_date": signed_date,
            "last_contract_action_type": last_action_type,
            "last_contract_action_date": last_action_date,
            "signed_via_free_agency": bool(signed_via_fa),

            # acquisition (transactions-driven)
            "acquired_via_trade": bool(acquired_via_trade),
            "acquired_date": acquired_date,

            # return-to-trading-team bans (season-specific)
            "trade_return_bans": trade_return_bans,
        }

    return out


def _canonicalize_player_ids(player_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in player_ids or []:
        if raw is None:
            continue
        pid = str(normalize_player_id(raw, strict=False, allow_legacy_numeric=True))
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
    return out
