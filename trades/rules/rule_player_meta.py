"""Rule-only player metadata builder (SSOT-backed).

This module builds a minimal ``players`` dictionary suitable for trade rule evaluation.

Design constraints:
- Values returned here are derived from SQLite SSOT (``LeagueRepo``).
- Never read from UI caches (e.g., ``state['ui_cache']``).
- Keep schema minimal and rule-focused; do not include UI-only fields.

Phase 1 (minimal) implementation notes:
- Only computes metadata for the provided ``player_ids``.
- Uses active contract ``signed_date`` when available.
- Acquisition flags and return-bans are left at conservative defaults and will be
  upgraded in a later phase using ``transactions_log``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id


def build_rule_players_meta(
    repo: LeagueRepo,
    player_ids: Iterable[str],
    *,
    unknown_signed_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build rule-only player metadata for the given player_ids.

    Returns a dict keyed by canonical ``player_id``.

    Args:
        repo: LeagueRepo (SSOT-backed).
        player_ids: iterable of player_id-like values.
        unknown_signed_date: optional fallback when a player has no active contract
            signed_date in SSOT. (e.g., "1900-01-01" to make everyone immediately eligible)
    """
    canonical_ids = _canonicalize_player_ids(player_ids)
    if not canonical_ids:
        return {}

    # Phase 1: bulk SSOT reads for only the requested player_ids.
    team_ids_by_player = repo.get_team_ids_by_players(canonical_ids)
    signed_dates_by_player = repo.get_active_signed_dates_by_players(canonical_ids)

    out: Dict[str, Dict[str, Any]] = {}
    for pid in canonical_ids:
        team_id = team_ids_by_player.get(pid)
        signed_date = signed_dates_by_player.get(pid)
        if signed_date is None and unknown_signed_date is not None:
            signed_date = unknown_signed_date

        out[pid] = {
            # identity / current assignment
            "player_id": pid,
            # Keep both keys for now to avoid surprises if any rule expects "team_id".
            "team_id": team_id,
            "current_team_id": team_id,

            # contract / signing (minimal)
            "signed_date": signed_date,
            # Phase 1: treat last contract action as unknown unless we have signed_date.
            # Phase 2 will compute these from transactions_log.
            "last_contract_action_type": "signed" if signed_date else None,
            "last_contract_action_date": signed_date,
            "signed_via_free_agency": False,

            # acquisition (will be upgraded using transactions_log)
            "acquired_via_trade": False,
            "acquired_date": None,

            # return-to-trading-team bans (will be upgraded using transactions_log)
            "trade_return_bans": {},
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
