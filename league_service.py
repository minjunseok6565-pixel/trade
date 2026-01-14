from __future__ import annotations

"""league_service.py

Write-oriented orchestration layer.

Design goals:
- Keep LeagueRepo as the standard DB access interface.
- Put *scenario/command* writes (multi-table updates + validation + logging) here.
- Prefer idempotent / safe operations for boot/seed/migration actions.

This file intentionally starts with a small, safe subset of write APIs. More complex
commands (trade commit, draft settlement, contract lifecycle) can be added incrementally.
"""

import contextlib
import datetime as _dt
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id, season_id_from_year


def _today_iso() -> str:
    return date.today().isoformat()

def _utc_now_iso() -> str:
    # Match LeagueRepo's timestamp format (UTC + "Z", no microseconds).
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)

def _json_loads(value: Any, default: Any):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _coerce_iso(d: date | str | None) -> str:
    if d is None:
        return _today_iso()
    if isinstance(d, str):
        return d
    return d.isoformat()


def _extract_team_ids_from_deal(deal: Any) -> List[str]:
    """Best-effort extraction of team ids from various deal shapes.

    Supports:
    - deal.teams (iterable)
    - dict with 'teams'
    - dict with 'legs' (keys are team ids)
    """
    try:
        teams = getattr(deal, "teams", None)
        if teams:
            return [str(t) for t in list(teams)]
    except Exception:
        pass

    if isinstance(deal, dict):
        teams = deal.get("teams")
        if teams:
            try:
                return [str(t) for t in list(teams)]
            except Exception:
                return [str(teams)]
        legs = deal.get("legs")
        if isinstance(legs, dict) and legs:
            return [str(t) for t in legs.keys()]

    return []


@dataclass(frozen=True)
class ServiceEvent:
    """Small, stable event envelope for write APIs."""

    type: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, **self.payload}


class LeagueService:
    """High-level write API layer built on LeagueRepo."""

    def __init__(self, repo: LeagueRepo):
        self.repo = repo
        
    # ----------------------------
    # Internal common helpers
    # ----------------------------
    @contextlib.contextmanager
    def _atomic(self):
        """
        Yield a cursor inside a DB transaction.

        - If a transaction is already open on the underlying connection (nested call),
          we DO NOT start/commit/rollback; we just yield a cursor.
        - Otherwise we start an explicit BEGIN/COMMIT/ROLLBACK.

        This makes Service helpers safe to compose without triggering
        'cannot start a transaction within a transaction' in SQLite.
        """
        conn = getattr(self.repo, "_conn", None)
        if conn is None:
            # Fallback: use repo.transaction (should never happen in normal runtime).
            with self.repo.transaction() as cur:
                yield cur
            return

        if getattr(conn, "in_transaction", False):
            cur = conn.cursor()
            try:
                yield cur
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            return

        with self.repo.transaction() as cur:
            yield cur

    def _norm_team_id(self, team_id: Any, *, strict: bool = True) -> str:
        return str(normalize_team_id(team_id, strict=strict)).upper()

    def _norm_player_id(self, player_id: Any) -> str:
        return str(normalize_player_id(player_id, strict=False, allow_legacy_numeric=True))

    def _normalize_salary_by_year(self, salary_by_year: Optional[Mapping[int, int]]) -> Dict[str, float]:
        """
        Normalize salary_by_year to the storage shape used by LeagueRepo:
          - keys: season_year as *string*
          - values: numeric (float OK; repo stores JSON)
        """
        if not salary_by_year:
            return {}
        out: Dict[str, float] = {}
        for k, v in salary_by_year.items():
            try:
                year_i = int(k)
            except Exception:
                continue
            if v is None:
                continue
            try:
                val_f = float(v)
            except Exception:
                continue
            out[str(year_i)] = val_f
        return out

    def _salary_for_season(self, contract: Mapping[str, Any], season_year: int) -> Optional[int]:
        """
        Best-effort salary lookup from a contract dict (legacy-friendly).
        Returns integer dollars if present.
        """
        salary_by_year = contract.get("salary_by_year") or {}
        if isinstance(salary_by_year, dict):
            v = salary_by_year.get(str(int(season_year)))
            if v is None:
                v = salary_by_year.get(int(season_year))  # tolerate int keys
            if v is None:
                return None
            try:
                return int(float(v))
            except Exception:
                return None
        return None

    def _tx_exists_by_deal_id(self, cur, deal_id: str) -> bool:
        if not deal_id:
            return False
        row = cur.execute(
            "SELECT 1 FROM transactions_log WHERE deal_id=? LIMIT 1;",
            (str(deal_id),),
        ).fetchone()
        return bool(row)

    def _insert_transactions_in_cur(self, cur, entries: Sequence[Mapping[str, Any]]) -> None:
        """
        Insert transactions_log rows using the same hashing/shape as LeagueRepo.insert_transactions,
        but *within an existing cursor/transaction*.
        """
        if not entries:
            return
        now = _utc_now_iso()
        rows = []
        for e in entries:
            if not isinstance(e, dict):
                e = dict(e)
            payload = _json_dumps(dict(e))
            tx_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
            rows.append(
                (
                    tx_hash,
                    str(e.get("type") or "unknown"),
                    str(e.get("date") or "") if e.get("date") is not None else None,
                    str(e.get("deal_id") or "") if e.get("deal_id") is not None else None,
                    str(e.get("source") or "") if e.get("source") is not None else None,
                    _json_dumps(e.get("teams") or []),
                    payload,
                    now,
                )
            )
        cur.executemany(
            """
            INSERT OR IGNORE INTO transactions_log(tx_hash, tx_type, tx_date, deal_id, source, teams_json, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rows,
        )

    def _move_player_team_in_cur(self, cur, player_id: str, to_team_id: str) -> None:
        """
        Roster move + active contract team sync (same behavior as LeagueRepo.trade_player),
        but within an existing cursor/transaction.
        """
        pid = self._norm_player_id(player_id)
        to_tid = self._norm_team_id(to_team_id, strict=True)
        now = _utc_now_iso()

        exists = cur.execute(
            "SELECT team_id FROM roster WHERE player_id=? AND status='active';",
            (pid,),
        ).fetchone()
        if not exists:
            raise KeyError(f"active roster entry not found for player_id={player_id}")

        cur.execute(
            "UPDATE roster SET team_id=?, updated_at=? WHERE player_id=?;",
            (to_tid, now, pid),
        )
        cur.execute(
            "UPDATE contracts SET team_id=?, updated_at=? WHERE player_id=? AND is_active=1;",
            (to_tid, now, pid),
        )

    def _set_roster_salary_in_cur(self, cur, player_id: str, salary_amount: int) -> None:
        pid = self._norm_player_id(player_id)
        now = _utc_now_iso()
        cur.execute(
            "UPDATE roster SET salary_amount=?, updated_at=? WHERE player_id=?;",
            (int(salary_amount), now, pid),
        )

    def _load_contract_row_in_cur(self, cur, contract_id: str) -> Dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM contracts WHERE contract_id=?;",
            (str(contract_id),),
        ).fetchone()
        if not row:
            raise KeyError(f"contract not found: {contract_id}")

        raw_json = row["contract_json"] if "contract_json" in row.keys() else None
        if raw_json:
            obj = _json_loads(raw_json, None)
            if isinstance(obj, dict):
                obj.setdefault("contract_id", str(row["contract_id"]))
                obj.setdefault("player_id", str(row["player_id"]))
                obj.setdefault("team_id", str(row["team_id"]).upper())
                return obj

        salary_by_year = _json_loads(row["salary_by_season_json"], {})
        if not isinstance(salary_by_year, dict):
            salary_by_year = {}
        options = _json_loads(row["options_json"] if "options_json" in row.keys() else None, [])
        if not isinstance(options, list):
            options = []

        return {
            "contract_id": str(row["contract_id"]),
            "player_id": str(row["player_id"]),
            "team_id": str(row["team_id"]).upper(),
            "signed_date": row["signed_date"] if "signed_date" in row.keys() else None,
            "start_season_year": row["start_season_year"] if "start_season_year" in row.keys() else None,
            "years": row["years"] if "years" in row.keys() else None,
            "salary_by_year": salary_by_year,
            "options": options,
            "status": row["status"] if "status" in row.keys() else None,
            "is_active": bool(int(row["is_active"]) if row["is_active"] is not None else 0),
        }

    def _upsert_contract_records_in_cur(self, cur, contracts_by_id: Mapping[str, Any]) -> None:
        """
        Upsert contract rows (same semantics as LeagueRepo.upsert_contract_records),
        but within an existing cursor/transaction.
        """
        if not contracts_by_id:
            return
        now = _utc_now_iso()
        rows = []
        for cid, c in contracts_by_id.items():
            if not isinstance(c, dict):
                continue
            contract_id = str(c.get("contract_id") or cid)
            player_id = self._norm_player_id(c.get("player_id"))
            team_id = c.get("team_id")
            team_id_norm = self._norm_team_id(team_id, strict=False) if team_id else ""
            signed_date = c.get("signed_date")
            start_year = c.get("start_season_year")
            years = c.get("years")
            status = str(c.get("status") or "")
            options = c.get("options") or []
            salary_by_year = c.get("salary_by_year") or {}
            try:
                start_year_i = int(start_year) if start_year is not None else None
            except Exception:
                start_year_i = None
            try:
                years_i = int(years) if years is not None else None
            except Exception:
                years_i = None
            start_season_id = str(season_id_from_year(start_year_i)) if start_year_i else None
            end_season_id = (
                str(season_id_from_year(start_year_i + max((years_i or 1) - 1, 0)))
                if start_year_i and years_i
                else start_season_id
            )
            salary_json = _json_dumps(salary_by_year)
            contract_json = _json_dumps(c)
            is_active = 1 if status.strip().upper() == "ACTIVE" else 0
            rows.append(
                (
                    contract_id,
                    player_id,
                    team_id_norm,
                    start_season_id,
                    end_season_id,
                    salary_json,
                    "STANDARD",
                    is_active,
                    now,
                    now,
                    str(signed_date) if signed_date is not None else None,
                    start_year_i,
                    years_i,
                    _json_dumps(options),
                    status,
                    contract_json,
                )
            )
        cur.executemany(
            """
            INSERT INTO contracts(
                contract_id, player_id, team_id, start_season_id, end_season_id,
                salary_by_season_json, contract_type, is_active, created_at, updated_at,
                signed_date, start_season_year, years, options_json, status, contract_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contract_id) DO UPDATE SET
                player_id=excluded.player_id,
                team_id=excluded.team_id,
                start_season_id=excluded.start_season_id,
                end_season_id=excluded.end_season_id,
                salary_by_season_json=excluded.salary_by_season_json,
                contract_type=excluded.contract_type,
                is_active=excluded.is_active,
                updated_at=excluded.updated_at,
                signed_date=excluded.signed_date,
                start_season_year=excluded.start_season_year,
                years=excluded.years,
                options_json=excluded.options_json,
                status=excluded.status,
                contract_json=excluded.contract_json;
            """,
            rows,
        )

    def _activate_contract_for_player_in_cur(self, cur, player_id: str, contract_id: str) -> None:
        """
        Make (player_id, contract_id) the active contract, maintaining:
          - contracts.is_active flags for that player
          - active_contracts index
          - player_contracts index
        """
        pid = self._norm_player_id(player_id)
        cid = str(contract_id)
        now = _utc_now_iso()

        # Deactivate all existing contracts for this player, then activate target.
        cur.execute("UPDATE contracts SET is_active=0, updated_at=? WHERE player_id=?;", (now, pid))
        updated = cur.execute(
            "UPDATE contracts SET is_active=1, updated_at=? WHERE contract_id=? AND player_id=?;",
            (now, cid, pid),
        ).rowcount
        if updated <= 0:
            raise KeyError(f"contract not found for player activation: player_id={pid}, contract_id={cid}")

        cur.execute(
            "INSERT OR IGNORE INTO player_contracts(player_id, contract_id) VALUES (?, ?);",
            (pid, cid),
        )
        cur.execute(
            "INSERT OR REPLACE INTO active_contracts(player_id, contract_id, updated_at) VALUES (?, ?, ?);",
            (pid, cid, now),
        )

    # ----------------------------
    # Lifecycle / context helpers
    # ----------------------------
    @classmethod
    @contextmanager
    def open(cls, db_path: str):
        """Open a repo and yield a service bound to it."""
        with LeagueRepo(db_path) as repo:
            # Make all service calls safe even if caller forgot to init explicitly.
            repo.init_db()
            yield cls(repo)

    # ----------------------------
    # (A) Boot / Migration / Seed
    # ----------------------------
    def init_or_migrate_db(self) -> None:
        """Create tables and apply light-weight migrations (safe to call repeatedly)."""
        self.repo.init_db()

    def ensure_gm_profiles_seeded(self, team_ids: Sequence[str]) -> None:
        """Ensure gm_profiles has at least an empty profile row for each team."""
        self.repo.ensure_gm_profiles_seeded(list(team_ids))

    def ensure_draft_picks_seeded(self, draft_year: int, team_ids: Sequence[str], years_ahead: int) -> None:
        """Ensure draft_picks have baseline rows for validation/lookahead."""
        self.repo.ensure_draft_picks_seeded(int(draft_year), list(team_ids), years_ahead=int(years_ahead))

    def ensure_contracts_bootstrapped_from_roster(self, season_year: int) -> None:
        """Ensure roster players have at least a minimal active contract entry."""
        self.repo.ensure_contracts_bootstrapped_from_roster(int(season_year))

    def import_roster_from_excel(
        self,
        excel_path: str,
        *,
        mode: str = "replace",
        sheet_name: Optional[str] = None,
        strict_ids: bool = True,
    ) -> None:
        """Admin import: Excel roster -> SQLite."""
        self.repo.import_roster_excel(
            excel_path,
            mode=mode,
            sheet_name=sheet_name,
            strict_ids=bool(strict_ids),
        )

    # ----------------------------
    # (L) Transactions log
    # ----------------------------
    def append_transaction(self, entry: Mapping[str, Any]) -> Dict[str, Any]:
        """Insert a single transaction entry into transactions_log."""
        d = dict(entry)
        with self._atomic() as cur:
            self._insert_transactions_in_cur(cur, [d])
        return d

    def append_transactions(self, entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Insert multiple transaction entries."""
        payloads = [dict(e) for e in entries]
        with self._atomic() as cur:
            self._insert_transactions_in_cur(cur, payloads)
        return payloads

    def log_trade_transaction(
        self,
        deal: Any,
        *,
        source: str,
        trade_date: date | str | None = None,
        deal_id: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Minimal trade log writer (DB).

        This intentionally does *not* assume a specific Deal model shape.
        The raw deal object is stored under payload.deal for traceability.
        """
        entry: Dict[str, Any] = {
            "type": "trade",
            "date": _coerce_iso(trade_date),
            "source": source or "",
            "teams": _extract_team_ids_from_deal(deal),
            "deal_id": deal_id,
            "meta": dict(meta) if meta else {},
            "deal": deal if isinstance(deal, dict) else None,
        }
        # Remove noisy keys if empty
        if entry.get("deal_id") is None:
            entry.pop("deal_id", None)
        if not entry.get("teams"):
            entry.pop("teams", None)
        if not entry.get("meta"):
            entry.pop("meta", None)
        if entry.get("deal") is None:
            entry.pop("deal", None)

        with self._atomic() as cur:
            self._insert_transactions_in_cur(cur, [entry])
        return entry

    # ----------------------------
    # (G) GM profile write
    # ----------------------------
    def upsert_gm_profile(self, team_id: str, profile_dict: Mapping[str, Any] | None) -> None:
        self.repo.upsert_gm_profile(team_id, profile_dict)

    def upsert_gm_profiles(self, profiles_by_team: Mapping[str, Mapping[str, Any] | None]) -> None:
        self.repo.upsert_gm_profiles(profiles_by_team)

    # ----------------------------
    # (C) Small contract/roster writes (safe subset)
    # ----------------------------
    def set_player_salary(self, player_id: str, salary_amount: int) -> None:
        """Direct roster salary update."""
        with self._atomic() as cur:
            self._set_roster_salary_in_cur(cur, player_id, int(salary_amount))

    def release_player_to_free_agency(self, player_id: str, released_date: date | str | None = None) -> ServiceEvent:
        """Release player to FA by moving roster.team_id to 'FA'.

        free_agents is derived from roster.team_id == 'FA' by default (SSOT),
        so this method only needs to update the roster (and optionally contracts team sync).
        """
        # released_date currently used only for logging; the roster update is date-agnostic.
        with self._atomic() as cur:
            self._move_player_team_in_cur(cur, player_id, "FA")

        event = ServiceEvent(
            type="release_to_free_agency",
            payload={
                "date": _coerce_iso(released_date),
                "player_id": str(player_id),
            },
        )
        # Optional: also log it (caller can decide; keeping it explicit for now).
        return event

    # ----------------------------
    # (T / S / C complex) Planned APIs (stubs)
    # ----------------------------
    def execute_trade(
        self,
        deal: Any,
        *,
        source: str,
        trade_date: date | str | None = None,
        deal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Commit a trade to the DB (players + picks/swaps/fixed assets + log).

        Not implemented in this initial safe drop.
        """
        raise NotImplementedError(
            "execute_trade is not implemented yet in LeagueService. "
            "Add full DB commit (players + draft_picks + swap_rights + fixed_assets + transactions_log) incrementally."
        )

    def settle_draft_year(self, draft_year: int, pick_order_by_pick_id: Mapping[str, int]) -> List[Dict[str, Any]]:
        """Settle protections and swap rights for a given draft year (DB)."""
        raise NotImplementedError("settle_draft_year is not implemented yet in LeagueService.")

    def sign_free_agent(
        self,
        team_id: str,
        player_id: str,
        *,
        signed_date: date | str | None = None,
        years: int = 1,
        salary_by_year: Optional[Mapping[int, int]] = None,
    ) -> ServiceEvent:
        """Sign an FA (DB): roster.team_id + contracts + active contract + salary."""
        raise NotImplementedError("sign_free_agent is not implemented yet in LeagueService.")

    def re_sign_or_extend(
        self,
        team_id: str,
        player_id: str,
        *,
        signed_date: date | str | None = None,
        years: int = 1,
        salary_by_year: Optional[Mapping[int, int]] = None,
    ) -> ServiceEvent:
        """Re-sign / extend a player (DB): contracts + active contract + salary."""
        raise NotImplementedError("re_sign_or_extend is not implemented yet in LeagueService.")

    def apply_contract_option_decision(
        self,
        contract_id: str,
        *,
        season_year: int,
        decision: str,
        decision_date: date | str | None = None,
    ) -> ServiceEvent:
        """Apply team/player option decision (DB)."""
        raise NotImplementedError("apply_contract_option_decision is not implemented yet in LeagueService.")

    def expire_contracts_for_season_transition(
        self,
        from_year: int,
        to_year: int,
        *,
        decision_policy: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Expire contracts and optionally release players (DB)."""
        raise NotImplementedError("expire_contracts_for_season_transition is not implemented yet in LeagueService.")


# ----------------------------
# Convenience module-level APIs
# ----------------------------
def init_or_migrate_db(db_path: str) -> None:
    with LeagueService.open(db_path) as svc:
        svc.init_or_migrate_db()


def ensure_gm_profiles_seeded(db_path: str, team_ids: Sequence[str]) -> None:
    with LeagueService.open(db_path) as svc:
        svc.ensure_gm_profiles_seeded(team_ids)


def ensure_draft_picks_seeded(db_path: str, draft_year: int, team_ids: Sequence[str], years_ahead: int) -> None:
    with LeagueService.open(db_path) as svc:
        svc.ensure_draft_picks_seeded(draft_year, team_ids, years_ahead)


def ensure_contracts_bootstrapped_from_roster(db_path: str, season_year: int) -> None:
    with LeagueService.open(db_path) as svc:
        svc.ensure_contracts_bootstrapped_from_roster(season_year)
