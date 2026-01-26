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
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}


def _warn_limited(code: str, msg: str, *, limit: int = 5) -> None:
    """Log a WARNING with traceback, but cap repeats per code.

    This avoids spamming logs in hot loops while still recording error types.
    """
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id, season_id_from_year

# Contract creation helpers
from contracts.models import new_contract_id, make_contract_record

# Season inference (fallbacks keep Service usable even in minimal test harnesses)
try:
    from config import SEASON_START_MONTH, SEASON_START_DAY, SEASON_LENGTH_DAYS
except Exception:  # pragma: no cover
    SEASON_START_MONTH = 10
    SEASON_START_DAY = 19
    SEASON_LENGTH_DAYS = 180

# Contract option handling (DB SSOT)
from contracts.options import (
    apply_option_decision,
    get_pending_options_for_season,
    normalize_option_record,
    recompute_contract_years_from_salary,
)
from contracts.options_policy import default_option_decision_policy


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
    except (json.JSONDecodeError, TypeError):
        _warn_limited("JSON_DECODE_FAILED", f"value_preview={repr(str(value))[:120]}", limit=3)
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
    except (AttributeError, TypeError):
        _warn_limited("DEAL_TEAMS_EXTRACT_FAILED", f"deal_type={type(deal).__name__}", limit=3)
        pass

    if isinstance(deal, dict):
        teams = deal.get("teams")
        if teams:
            try:
                return [str(t) for t in list(teams)]
            except (TypeError, ValueError):
                _warn_limited("DEAL_TEAMS_COERCE_FAILED", f"teams_value={teams!r}", limit=3)
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
            except (TypeError, ValueError):
                _warn_limited("SALARY_YEAR_KEY_COERCE_FAILED", f"k={k!r}")
                continue
            if v is None:
                continue
            try:
                val_f = float(v)
            except (TypeError, ValueError):
                _warn_limited("SALARY_VALUE_COERCE_FAILED", f"year_key={k!r} value={v!r}")
                continue
            out[str(year_i)] = val_f
        return out

    def _salary_for_season(self, contract: Mapping[str, Any], season_year: int) -> Optional[int]:
        salary_by_year = contract.get("salary_by_year") or {}
        if isinstance(salary_by_year, dict):
            v = salary_by_year.get(str(int(season_year)))
            if v is None:
                v = salary_by_year.get(int(season_year))  # tolerate int keys
            if v is None:
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                _warn_limited("SALARY_FOR_SEASON_COERCE_FAILED", f"season_year={season_year!r} value={v!r}")
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

                # Merge canonical column fields into contract_json if they are missing.
                # This prevents Service upserts from accidentally dropping/blanking critical fields.
                obj.setdefault(
                    "signed_date",
                    row["signed_date"] if "signed_date" in row.keys() else None,
                )
                obj.setdefault(
                    "start_season_year",
                    row["start_season_year"] if "start_season_year" in row.keys() else None,
                )
                obj.setdefault(
                    "years",
                    row["years"] if "years" in row.keys() else None,
                )

                if "salary_by_year" not in obj:
                    salary_by_year = _json_loads(row["salary_by_season_json"], {})
                    obj["salary_by_year"] = salary_by_year if isinstance(salary_by_year, dict) else {}

                if "options" not in obj:
                    options = _json_loads(
                        row["options_json"] if "options_json" in row.keys() else None, []
                    )
                    obj["options"] = options if isinstance(options, list) else []

                # Preserve status/is_active from columns if not present in json
                is_active_col = bool(int(row["is_active"]) if row["is_active"] is not None else 0)
                status_col = row["status"] if "status" in row.keys() else None
                if not obj.get("status"):
                    obj["status"] = status_col or ("ACTIVE" if is_active_col else "")
                obj.setdefault("is_active", is_active_col)

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
            except (TypeError, ValueError):
                _warn_limited("CONTRACT_START_YEAR_COERCE_FAILED", f"contract_id={contract_id} value={start_year!r}")
                start_year_i = None
            try:
                years_i = int(years) if years is not None else None
            except (TypeError, ValueError):
                _warn_limited("CONTRACT_YEARS_COERCE_FAILED", f"contract_id={contract_id} value={years!r}")
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

    def _upsert_draft_picks_in_cur(self, cur, picks_by_id: Mapping[str, Any]) -> None:
        """Upsert draft_picks within an existing cursor/transaction."""
        if not picks_by_id:
            return
        now = _utc_now_iso()
        rows = []
        for pick_id, pick in picks_by_id.items():
            if not isinstance(pick, dict):
                continue
            pid = str(pick.get("pick_id") or pick_id)
            try:
                year = int(pick.get("year") or 0)
            except (TypeError, ValueError):
                _warn_limited("DRAFT_PICK_YEAR_COERCE_FAILED", f"pick_id={pid} value={pick.get('year')!r}")
                year = 0
            try:
                rnd = int(pick.get("round") or 0)
            except (TypeError, ValueError):
                _warn_limited("DRAFT_PICK_ROUND_COERCE_FAILED", f"pick_id={pid} value={pick.get('round')!r}")
                rnd = 0
            original = str(pick.get("original_team") or "").upper()
            owner = str(pick.get("owner_team") or "").upper()
            protection = pick.get("protection")
            rows.append(
                (
                    pid,
                    year,
                    rnd,
                    original,
                    owner,
                    _json_dumps(protection) if protection is not None else None,
                    now,
                    now,
                )
            )
        if not rows:
            return
        cur.executemany(
            """
            INSERT INTO draft_picks(pick_id, year, round, original_team, owner_team, protection_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pick_id) DO UPDATE SET
                year=excluded.year,
                round=excluded.round,
                original_team=excluded.original_team,
                owner_team=excluded.owner_team,
                protection_json=excluded.protection_json,
                updated_at=excluded.updated_at;
            """,
            rows,
        )

    def _upsert_swap_rights_in_cur(self, cur, swaps_by_id: Mapping[str, Any]) -> None:
        """Upsert swap_rights within an existing cursor/transaction."""
        if not swaps_by_id:
            return
        now = _utc_now_iso()
        rows = []
        for sid, swap in swaps_by_id.items():
            if not isinstance(swap, dict):
                continue
            swap_id = str(swap.get("swap_id") or sid)
            # year/round are nullable in schema; keep None if not cleanly numeric
            year_raw = swap.get("year")
            rnd_raw = swap.get("round")
            year_i = int(year_raw) if isinstance(year_raw, int) or str(year_raw or "").isdigit() else None
            rnd_i = int(rnd_raw) if isinstance(rnd_raw, int) or str(rnd_raw or "").isdigit() else None
            rows.append(
                (
                    swap_id,
                    str(swap.get("pick_id_a") or ""),
                    str(swap.get("pick_id_b") or ""),
                    year_i,
                    rnd_i,
                    str(swap.get("owner_team") or "").upper(),
                    1 if swap.get("active", True) else 0,
                    str(swap.get("created_by_deal_id") or "") if swap.get("created_by_deal_id") is not None else None,
                    str(swap.get("created_at") or now),
                    now,
                )
            )
        if not rows:
            return
        cur.executemany(
            """
            INSERT INTO swap_rights(swap_id, pick_id_a, pick_id_b, year, round, owner_team, active, created_by_deal_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(swap_id) DO UPDATE SET
                pick_id_a=excluded.pick_id_a,
                pick_id_b=excluded.pick_id_b,
                year=excluded.year,
                round=excluded.round,
                owner_team=excluded.owner_team,
                active=excluded.active,
                created_by_deal_id=excluded.created_by_deal_id,
                updated_at=excluded.updated_at;
            """,
            rows,
        )

    def _upsert_fixed_assets_in_cur(self, cur, assets_by_id: Mapping[str, Any]) -> None:
        """Upsert fixed_assets within an existing cursor/transaction."""
        if not assets_by_id:
            return
        now = _utc_now_iso()
        rows = []
        for aid, asset in assets_by_id.items():
            if not isinstance(asset, dict):
                continue
            asset_id = str(asset.get("asset_id") or aid)
            label = asset.get("label")
            value = asset.get("value")
            try:
                value_f = float(value) if value is not None else None
            except Exception:
                value_f = None
            owner = str(asset.get("owner_team") or "").upper()
            source_pick_id = asset.get("source_pick_id")
            draft_year = asset.get("draft_year")
            try:
                draft_year_i = int(draft_year) if draft_year is not None else None
            except Exception:
                draft_year_i = None
            attrs = dict(asset)
            rows.append(
                (
                    asset_id,
                    str(label) if label is not None else None,
                    value_f,
                    owner,
                    str(source_pick_id) if source_pick_id is not None else None,
                    draft_year_i,
                    _json_dumps(attrs),
                    now,
                    now,
                )
            )
        if not rows:
            return
        cur.executemany(
            """
            INSERT INTO fixed_assets(asset_id, label, value, owner_team, source_pick_id, draft_year, attrs_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                label=excluded.label,
                value=excluded.value,
                owner_team=excluded.owner_team,
                source_pick_id=excluded.source_pick_id,
                draft_year=excluded.draft_year,
                attrs_json=excluded.attrs_json,
                updated_at=excluded.updated_at;
            """,
            rows,
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
        Steps (atomic):
          1) validate (rules/validator)
          2) idempotency guard by deal_id (transactions_log)
          3) commit order: players -> picks -> swaps -> fixed_assets -> log
        """
        # Local imports to avoid circular deps (state/trades may import service elsewhere).
        try:
            from trades.models import (
                Deal as TradeDeal,
                PlayerAsset,
                PickAsset,
                SwapAsset,
                FixedAsset,
                canonicalize_deal,
                parse_deal,
                serialize_deal,
                asset_key,
            )
            from trades.errors import (
                TradeError,
                DEAL_ALREADY_EXECUTED,
                PLAYER_NOT_OWNED,
                PICK_NOT_OWNED,
                PROTECTION_CONFLICT,
                SWAP_NOT_OWNED,
                SWAP_INVALID,
                FIXED_ASSET_NOT_FOUND,
                FIXED_ASSET_NOT_OWNED,
                MISSING_TO_TEAM,
            )
        except Exception as exc:  # pragma: no cover
            raise ImportError("trades package is required for execute_trade") from exc

        # Normalize deal object
        if isinstance(deal, dict):
            deal_obj = parse_deal(deal)
        else:
            deal_obj = deal
        if not isinstance(deal_obj, TradeDeal):
            # Best-effort: accept any object with teams/legs
            if not hasattr(deal_obj, "teams") or not hasattr(deal_obj, "legs"):
                raise TypeError("execute_trade requires a trades.models.Deal (or dict parseable into one)")

        deal_obj = canonicalize_deal(deal_obj)  # stable ordering for hashing/logging

        trade_date_iso = _coerce_iso(trade_date)
        try:
            trade_date_as_date = date.fromisoformat(str(trade_date_iso)[:10])
        except (TypeError, ValueError):
            _warn_limited("TRADE_DATE_PARSE_FAILED", f"trade_date={trade_date_iso!r}", limit=3)
            trade_date_as_date = date.today()

        # If deal_id not provided, derive a deterministic id from canonical payload.
        if not deal_id:
            payload = serialize_deal(deal_obj)
            deal_id = hashlib.sha1(_json_dumps(payload).encode("utf-8")).hexdigest()

        # Idempotency: if already executed, return the stored transaction payload (or a stable error).
        conn = getattr(self.repo, "_conn", None)
        if conn is not None:
            row = conn.execute(
                "SELECT payload_json FROM transactions_log WHERE deal_id=? ORDER BY created_at DESC LIMIT 1;",
                (str(deal_id),),
            ).fetchone()
            if row:
                existing = _json_loads(row["payload_json"], {})
                if not isinstance(existing, dict):
                    existing = {"type": "trade", "deal_id": str(deal_id)}
                existing.setdefault("deal_id", str(deal_id))
                existing["already_executed"] = True
                return existing

        # Validation (required): trades.validator.validate_deal must be present in runtime.
        try:
            from trades.validator import validate_deal  # type: ignore
        except ImportError as exc:
            logger.exception(
                "[TRADE_VALIDATOR_IMPORT_FAILED] execute_trade cannot import trades.validator.validate_deal"
            )
            raise ImportError(
                "trades.validator.validate_deal is required to execute trades"
            ) from exc
        except Exception as exc:
            logger.exception(
                "[TRADE_VALIDATOR_IMPORT_FAILED] execute_trade failed while importing trades.validator.validate_deal"
            )
            raise RuntimeError(
                "failed to import trades.validator.validate_deal"
            ) from exc

        if not callable(validate_deal):
            raise TypeError("trades.validator.validate_deal must be callable")

        validate_deal(
            deal_obj,
            current_date=trade_date_as_date,
            allow_locked_by_deal_id=str(deal_id),
        )

        # Helpers
        def _resolve_receiver(sender_team: str, asset: Any) -> str:
            to_team = getattr(asset, "to_team", None)
            if to_team:
                return str(to_team).upper()
            if len(getattr(deal_obj, "teams", []) or []) == 2:
                other = [t for t in deal_obj.teams if str(t).upper() != str(sender_team).upper()]
                if other:
                    return str(other[0]).upper()
            raise TradeError(MISSING_TO_TEAM, "Missing to_team for multi-team deal asset", {"team_id": sender_team})

        # Collect assets by type with duplicate guard
        seen_assets: set[str] = set()
        player_moves: list[tuple[str, str, str]] = []
        pick_moves: list[tuple[str, str, str, Optional[dict]]] = []
        swap_moves: list[tuple[str, str, str, str, str]] = []
        fixed_moves: list[tuple[str, str, str]] = []

        for from_team, assets in deal_obj.legs.items():
            from_team_u = str(from_team).upper()
            for asset in assets:
                key = asset_key(asset)
                if key in seen_assets:
                    # Validator should also catch this, but keep commit safe.
                    raise TradeError("DUPLICATE_ASSET", "Duplicate asset in deal", {"asset_key": key})
                seen_assets.add(key)

                to_team_u = _resolve_receiver(from_team_u, asset)

                if isinstance(asset, PlayerAsset):
                    player_moves.append((str(asset.player_id), from_team_u, to_team_u))
                elif isinstance(asset, PickAsset):
                    pick_moves.append((str(asset.pick_id), from_team_u, to_team_u, asset.protection))
                elif isinstance(asset, SwapAsset):
                    swap_moves.append((str(asset.swap_id), from_team_u, to_team_u, str(asset.pick_id_a), str(asset.pick_id_b)))
                elif isinstance(asset, FixedAsset):
                    fixed_moves.append((str(asset.asset_id), from_team_u, to_team_u))

        # Prepare transaction entry (returned + stored)
        assets_summary: Dict[str, Dict[str, Any]] = {}
        for team_id, assets in deal_obj.legs.items():
            team_u = str(team_id).upper()
            players = [a.player_id for a in assets if isinstance(a, PlayerAsset)]
            picks = [a.pick_id for a in assets if isinstance(a, PickAsset)]
            pick_protections = [
                {"pick_id": a.pick_id, "protection": a.protection, "to_team": a.to_team}
                for a in assets
                if isinstance(a, PickAsset) and a.protection is not None
            ]
            swaps = [
                {"swap_id": a.swap_id, "pick_id_a": a.pick_id_a, "pick_id_b": a.pick_id_b, "to_team": a.to_team}
                for a in assets
                if isinstance(a, SwapAsset)
            ]
            fixed_assets = [{"asset_id": a.asset_id, "to_team": a.to_team} for a in assets if isinstance(a, FixedAsset)]
            assets_summary[team_u] = {
                "players": players,
                "picks": picks,
                "pick_protections": pick_protections,
                "swaps": swaps,
                "fixed_assets": fixed_assets,
            }

        tx_entry: Dict[str, Any] = {
            "type": "trade",
            "date": trade_date_iso,
            "teams": [str(t).upper() for t in list(deal_obj.teams)],
            "assets": assets_summary,
            "source": str(source),
            "deal_id": str(deal_id),
        }

        now = _utc_now_iso()
        with self._atomic() as cur:
            # Idempotency (transactional): avoid double apply even if concurrent.
            if self._tx_exists_by_deal_id(cur, str(deal_id)):
                # Return a stable indication (or fetch stored payload).
                row = cur.execute(
                    "SELECT payload_json FROM transactions_log WHERE deal_id=? ORDER BY created_at DESC LIMIT 1;",
                    (str(deal_id),),
                ).fetchone()
                if row:
                    existing = _json_loads(row["payload_json"], {})
                    if not isinstance(existing, dict):
                        existing = dict(tx_entry)
                    existing["already_executed"] = True
                    return existing
                raise TradeError(
                    DEAL_ALREADY_EXECUTED,
                    "Deal already executed",
                    {"deal_id": str(deal_id)},
                )

            # 1) Players
            for player_id, from_team_u, to_team_u in player_moves:
                pid = self._norm_player_id(player_id)
                row = cur.execute(
                    "SELECT team_id FROM roster WHERE player_id=? AND status='active';",
                    (pid,),
                ).fetchone()
                if not row:
                    raise TradeError(PLAYER_NOT_OWNED, "Player not found in roster", {"player_id": pid})
                current_team = str(row["team_id"]).upper()
                if current_team != from_team_u:
                    raise TradeError(
                        PLAYER_NOT_OWNED,
                        "Player not owned by team",
                        {"player_id": pid, "team_id": from_team_u, "current_team": current_team},
                    )
                self._move_player_team_in_cur(cur, pid, to_team_u)

            # 2) Picks (ownership + protection_json)
            for pick_id, from_team_u, to_team_u, protection in pick_moves:
                pick_row = cur.execute(
                    "SELECT pick_id, owner_team, original_team, year, round, protection_json FROM draft_picks WHERE pick_id=?;",
                    (str(pick_id),),
                ).fetchone()
                if not pick_row:
                    raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pick_id, "team_id": from_team_u})
                current_owner = str(pick_row["owner_team"]).upper()
                if current_owner != from_team_u:
                    raise TradeError(
                        PICK_NOT_OWNED,
                        "Pick not owned by team",
                        {"pick_id": pick_id, "team_id": from_team_u, "owner_team": current_owner},
                    )

                existing_prot = _json_loads(pick_row["protection_json"], None)
                new_prot = existing_prot
                if protection is not None:
                    if existing_prot is None:
                        new_prot = protection
                    elif existing_prot != protection:
                        raise TradeError(
                            PROTECTION_CONFLICT,
                            "Pick protection conflicts with existing record",
                            {"pick_id": pick_id, "existing_protection": existing_prot, "attempted_protection": protection},
                        )

                cur.execute(
                    "UPDATE draft_picks SET owner_team=?, protection_json=?, updated_at=? WHERE pick_id=?;",
                    (
                        str(to_team_u).upper(),
                        _json_dumps(new_prot) if new_prot is not None else None,
                        now,
                        str(pick_id),
                    ),
                )

            # 3) Swaps (update owner or create right)
            for swap_id, from_team_u, to_team_u, pick_id_a, pick_id_b in swap_moves:
                # Validate picks exist and match year/round
                a = cur.execute(
                    "SELECT year, round, owner_team FROM draft_picks WHERE pick_id=?;",
                    (str(pick_id_a),),
                ).fetchone()
                b = cur.execute(
                    "SELECT year, round, owner_team FROM draft_picks WHERE pick_id=?;",
                    (str(pick_id_b),),
                ).fetchone()
                if not a or not b:
                    raise TradeError(
                        SWAP_INVALID,
                        "Swap picks must exist",
                        {"swap_id": swap_id, "pick_id_a": pick_id_a, "pick_id_b": pick_id_b},
                    )
                if int(a["year"]) != int(b["year"]) or int(a["round"]) != int(b["round"]):
                    raise TradeError(
                        SWAP_INVALID,
                        "Swap picks must match year and round",
                        {"swap_id": swap_id, "pick_a": dict(a), "pick_b": dict(b)},
                    )
                swap_row = cur.execute(
                    "SELECT owner_team FROM swap_rights WHERE swap_id=?;",
                    (str(swap_id),),
                ).fetchone()
                if swap_row:
                    current_owner = str(swap_row["owner_team"]).upper()
                    if current_owner != from_team_u:
                        raise TradeError(
                            SWAP_NOT_OWNED,
                            "Swap right not owned by team",
                            {"swap_id": swap_id, "team_id": from_team_u, "owner_team": current_owner},
                        )
                    cur.execute(
                        "UPDATE swap_rights SET owner_team=?, updated_at=? WHERE swap_id=?;",
                        (str(to_team_u).upper(), now, str(swap_id)),
                    )
                else:
                    # Create a new swap right (validator should ensure this is legal)
                    cur.execute(
                        """
                        INSERT INTO swap_rights(
                            swap_id, pick_id_a, pick_id_b, year, round,
                            owner_team, active, created_by_deal_id, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(swap_id) DO UPDATE SET
                            pick_id_a=excluded.pick_id_a,
                            pick_id_b=excluded.pick_id_b,
                            year=excluded.year,
                            round=excluded.round,
                            owner_team=excluded.owner_team,
                            active=excluded.active,
                            created_by_deal_id=excluded.created_by_deal_id,
                            updated_at=excluded.updated_at;
                        """,
                        (
                            str(swap_id),
                            str(pick_id_a),
                            str(pick_id_b),
                            int(a["year"]),
                            int(a["round"]),
                            str(to_team_u).upper(),
                            str(deal_id),
                            str(trade_date_iso),
                            now,
                        ),
                    )

            # 4) Fixed assets
            for asset_id, from_team_u, to_team_u in fixed_moves:
                row = cur.execute(
                    "SELECT owner_team FROM fixed_assets WHERE asset_id=?;",
                    (str(asset_id),),
                ).fetchone()
                if not row:
                    raise TradeError(
                        FIXED_ASSET_NOT_FOUND,
                        "Fixed asset not found",
                        {"asset_id": asset_id, "team_id": from_team_u},
                    )
                current_owner = str(row["owner_team"]).upper()
                if current_owner != from_team_u:
                    raise TradeError(
                        FIXED_ASSET_NOT_OWNED,
                        "Fixed asset not owned by team",
                        {"asset_id": asset_id, "team_id": from_team_u, "owner_team": current_owner},
                    )
                cur.execute(
                    "UPDATE fixed_assets SET owner_team=?, updated_at=? WHERE asset_id=?;",
                    (str(to_team_u).upper(), now, str(asset_id)),
                )

            # 5) Log
            self._insert_transactions_in_cur(
                cur,
                [
                    {
                        "type": "trade",
                        "date": trade_date_iso,
                        "teams": [str(t).upper() for t in list(deal_obj.teams)],
                        "assets": assets_summary,
                        "source": str(source),
                        "deal_id": str(deal_id),
                    }
                ],
            )

        return tx_entry

    def settle_draft_year(self, draft_year: int, pick_order_by_pick_id: Mapping[str, int]) -> List[Dict[str, Any]]:
        """Settle protections and swap rights for a given draft year (DB)."""
        try:
            from trades.pick_settlement import settle_draft_year_in_memory as _legacy_settle_draft_year  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise ImportError("trades.pick_settlement.settle_draft_year is required") from exc

        year_i = int(draft_year)
        pick_order: Dict[str, int] = {}
        for k, v in dict(pick_order_by_pick_id).items():
            try:
                pick_order[str(k)] = int(v)
            except (TypeError, ValueError):
                _warn_limited("PICK_ORDER_INT_COERCE_FAILED", f"pick_id={k!r} value={v!r}", limit=3)
                continue

        # then persist the mutated results back to DB.
        game_state: Dict[str, Any] = {"draft_picks": {}, "swap_rights": {}, "fixed_assets": {}}

        with self._atomic() as cur:
            # Load picks for draft_year
            pick_rows = cur.execute(
                "SELECT pick_id, year, round, original_team, owner_team, protection_json FROM draft_picks WHERE year=?;",
                (year_i,),
            ).fetchall()
            for r in pick_rows:
                game_state["draft_picks"][str(r["pick_id"])] = {
                    "pick_id": str(r["pick_id"]),
                    "year": int(r["year"]),
                    "round": int(r["round"]),
                    "original_team": str(r["original_team"]).upper(),
                    "owner_team": str(r["owner_team"]).upper(),
                    "protection": _json_loads(r["protection_json"], None),
                }

            # Load swaps for draft_year (we only need those for settlement)
            swap_rows = cur.execute(
                """
                SELECT swap_id, pick_id_a, pick_id_b, year, round, owner_team, active, created_by_deal_id, created_at
                FROM swap_rights
                WHERE year=?;
                """,
                (year_i,),
            ).fetchall()
            for r in swap_rows:
                game_state["swap_rights"][str(r["swap_id"])] = {
                    "swap_id": str(r["swap_id"]),
                    "pick_id_a": str(r["pick_id_a"]),
                    "pick_id_b": str(r["pick_id_b"]),
                    "year": int(r["year"]) if r["year"] is not None else None,
                    "round": int(r["round"]) if r["round"] is not None else None,
                    "owner_team": str(r["owner_team"]).upper(),
                    "active": bool(int(r["active"]) if r["active"] is not None else 0),
                    "created_by_deal_id": r["created_by_deal_id"],
                    "created_at": r["created_at"],
                }

            # (Optional) preload fixed_assets for the year; not required for correctness (upsert is idempotent)
            fa_rows = cur.execute(
                "SELECT asset_id, label, value, owner_team, source_pick_id, draft_year, attrs_json FROM fixed_assets WHERE draft_year=?;",
                (year_i,),
            ).fetchall()
            for r in fa_rows:
                attrs = _json_loads(r["attrs_json"], {})
                if not isinstance(attrs, dict):
                    attrs = {}
                attrs.setdefault("asset_id", str(r["asset_id"]))
                attrs.setdefault("label", r["label"])
                attrs.setdefault("value", r["value"])
                attrs.setdefault("owner_team", str(r["owner_team"]).upper())
                attrs.setdefault("source_pick_id", r["source_pick_id"])
                attrs.setdefault("draft_year", r["draft_year"])
                game_state["fixed_assets"][str(r["asset_id"])] = attrs

            events = _legacy_settle_draft_year(game_state, year_i, pick_order)

            # Persist: picks (owner_team + protection cleared)
            picks_by_id = game_state.get("draft_picks") or {}
            if isinstance(picks_by_id, dict) and picks_by_id:
                self._upsert_draft_picks_in_cur(cur, picks_by_id)

            # Persist: swaps (active flags + owner swaps)
            swaps_by_id = game_state.get("swap_rights") or {}
            if isinstance(swaps_by_id, dict) and swaps_by_id:
                self._upsert_swap_rights_in_cur(cur, swaps_by_id)

            # Persist: fixed assets (compensation)
            assets_by_id = game_state.get("fixed_assets") or {}
            if isinstance(assets_by_id, dict) and assets_by_id:
                self._upsert_fixed_assets_in_cur(cur, assets_by_id)

        return events

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
        team_norm = self._norm_team_id(team_id, strict=True)
        pid = self._norm_player_id(player_id)
        signed_date_iso = _coerce_iso(signed_date)
        years_i = int(years)
        if years_i <= 0:
            raise ValueError("years must be >= 1")

        def _infer_start_season_year_from_date(d_iso: str) -> int:
            try:
                d = _dt.date.fromisoformat(str(d_iso)[:10])
            except (TypeError, ValueError):
                _warn_limited("SIGNED_DATE_PARSE_FAILED", f"signed_date={d_iso!r}", limit=3)
                d = _dt.date.today()
            start_this = _dt.date(d.year, int(SEASON_START_MONTH), int(SEASON_START_DAY))
            start_prev = _dt.date(d.year - 1, int(SEASON_START_MONTH), int(SEASON_START_DAY))
            end_prev = start_prev + _dt.timedelta(days=int(SEASON_LENGTH_DAYS))
            if d >= start_this:
                return d.year
            # before next season start: either still in previous season, or offseason for upcoming season
            if d >= end_prev:
                return d.year
            return d.year - 1

        with self._atomic() as cur:
            roster = cur.execute(
                """
                SELECT team_id, salary_amount
                FROM roster
                WHERE player_id=? AND status='active';
                """,
                (pid,),
            ).fetchone()
            if not roster:
                raise KeyError(f"active roster entry not found for player_id={player_id}")

            current_team = str(roster["team_id"]).upper()
            if current_team != "FA":
                raise ValueError(f"player_id={player_id} is not a free agent (team_id={current_team})")

            salary_norm = self._normalize_salary_by_year(salary_by_year)
            if salary_norm:
                start_season_year = min(int(k) for k in salary_norm.keys())
            else:
                start_season_year = _infer_start_season_year_from_date(signed_date_iso)
                base_salary = roster["salary_amount"]
                if base_salary is None:
                    base_salary = 0
                salary_norm = {
                    str(y): float(base_salary)
                    for y in range(int(start_season_year), int(start_season_year) + years_i)
                }

            contract_id = str(new_contract_id())
            contract = make_contract_record(
                contract_id=contract_id,
                player_id=pid,
                team_id=team_norm,
                signed_date_iso=signed_date_iso,
                start_season_year=int(start_season_year),
                years=years_i,
                salary_by_year=salary_norm,
                options=[],
                status="ACTIVE",
            )

            # Persist + activate + roster move
            self._upsert_contract_records_in_cur(cur, {contract_id: contract})
            self._activate_contract_for_player_in_cur(cur, pid, contract_id)
            self._move_player_team_in_cur(cur, pid, team_norm)

            # Roster salary reflects the (inferred) start season salary
            season_salary = salary_norm.get(str(int(start_season_year)))
            if season_salary is not None:
                self._set_roster_salary_in_cur(cur, pid, int(float(season_salary)))

            try:
                cur.execute("DELETE FROM free_agents WHERE player_id=?;", (pid,))
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("no such table" in msg) and ("free_agents" in msg):
                    logger.warning(
                        "[FREE_AGENTS_TABLE_MISSING] free_agents table missing; skipping cleanup (player_id=%s)",
                        pid,
                    )
                else:
                    logger.exception(
                        "[FREE_AGENTS_DELETE_FAILED] failed to delete free_agents row (player_id=%s)",
                        pid,
                    )
                    raise
            except Exception as exc:
                logger.exception(
                    "[FREE_AGENTS_DELETE_FAILED] failed to delete free_agents row (player_id=%s)",
                    pid,
                )
                raise

            # Optional: log signing transaction
            self._insert_transactions_in_cur(
                cur,
                [
                    {
                        "type": "signing",
                        "date": signed_date_iso,
                        "source": "contracts",
                        "teams": [team_norm],
                        "team_id": team_norm,
                        "player_id": pid,
                        "contract_id": contract_id,
                        "start_season_year": int(start_season_year),
                        "years": years_i,
                    }
                ],
            )

        return ServiceEvent(
            type="sign_free_agent",
            payload={
                "team_id": team_norm,
                "player_id": pid,
                "contract_id": contract_id,
                "signed_date": signed_date_iso,
                "start_season_year": int(start_season_year),
                "years": years_i,
            },
        )

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
        team_norm = self._norm_team_id(team_id, strict=True)
        pid = self._norm_player_id(player_id)
        signed_date_iso = _coerce_iso(signed_date)
        years_i = int(years)
        if years_i <= 0:
            raise ValueError("years must be >= 1")

        def _infer_start_season_year_from_date(d_iso: str) -> int:
            try:
                d = _dt.date.fromisoformat(str(d_iso)[:10])
            except (TypeError, ValueError):
                _warn_limited("SIGNED_DATE_PARSE_FAILED", f"signed_date={d_iso!r}", limit=3)
                d = _dt.date.today()
            start_this = _dt.date(d.year, int(SEASON_START_MONTH), int(SEASON_START_DAY))
            start_prev = _dt.date(d.year - 1, int(SEASON_START_MONTH), int(SEASON_START_DAY))
            end_prev = start_prev + _dt.timedelta(days=int(SEASON_LENGTH_DAYS))
            if d >= start_this:
                return d.year
            if d >= end_prev:
                return d.year
            return d.year - 1

        with self._atomic() as cur:
            roster = cur.execute(
                """
                SELECT team_id, salary_amount
                FROM roster
                WHERE player_id=? AND status='active';
                """,
                (pid,),
            ).fetchone()
            if not roster:
                raise KeyError(f"active roster entry not found for player_id={player_id}")

            current_team = str(roster["team_id"]).upper()
            if current_team == "FA":
                raise ValueError(f"player_id={player_id} is currently FA; use sign_free_agent")
            if current_team != team_norm:
                raise ValueError(
                    f"player_id={player_id} is on team_id={current_team}; cannot re-sign/extend for {team_norm}"
                )

            salary_norm = self._normalize_salary_by_year(salary_by_year)
            if salary_norm:
                start_season_year = min(int(k) for k in salary_norm.keys())
            else:
                start_season_year = _infer_start_season_year_from_date(signed_date_iso)
                base_salary = roster["salary_amount"]
                if base_salary is None:
                    base_salary = 0
                salary_norm = {
                    str(y): float(base_salary)
                    for y in range(int(start_season_year), int(start_season_year) + years_i)
                }

            contract_id = str(new_contract_id())
            contract = make_contract_record(
                contract_id=contract_id,
                player_id=pid,
                team_id=team_norm,
                signed_date_iso=signed_date_iso,
                start_season_year=int(start_season_year),
                years=years_i,
                salary_by_year=salary_norm,
                options=[],
                status="ACTIVE",
            )

            self._upsert_contract_records_in_cur(cur, {contract_id: contract})
            self._activate_contract_for_player_in_cur(cur, pid, contract_id)

            # Ensure roster + active contract team_id stay synced (idempotent if unchanged)
            self._move_player_team_in_cur(cur, pid, team_norm)

            season_salary = salary_norm.get(str(int(start_season_year)))
            if season_salary is not None:
                self._set_roster_salary_in_cur(cur, pid, int(float(season_salary)))

            # Optional: log re-sign/extend transaction
            self._insert_transactions_in_cur(
                cur,
                [
                    {
                        "type": "re_sign_or_extend",
                        "date": signed_date_iso,
                        "source": "contracts",
                        "teams": [team_norm],
                        "team_id": team_norm,
                        "player_id": pid,
                        "contract_id": contract_id,
                        "start_season_year": int(start_season_year),
                        "years": years_i,
                    }
                ],
            )

        return ServiceEvent(
            type="re_sign_or_extend",
            payload={
                "team_id": team_norm,
                "player_id": pid,
                "contract_id": contract_id,
                "signed_date": signed_date_iso,
                "start_season_year": int(start_season_year),
                "years": years_i,
            },
        )


    def apply_contract_option_decision(
        self,
        contract_id: str,
        *,
        season_year: int,
        decision: str,
        decision_date: date | str | None = None,
    ) -> ServiceEvent:
        """Apply team/player option decision (DB)."""
        season_year_i = int(season_year)
        decision_date_iso = _coerce_iso(decision_date)

        with self._atomic() as cur:
            contract = self._load_contract_row_in_cur(cur, contract_id)

            # Normalize options safely (drop invalid option records rather than corrupt DB).
            raw_opts = contract.get("options") or []
            normalized_opts: List[dict] = []
            for opt in raw_opts:
                try:
                    normalized_opts.append(normalize_option_record(opt))
                except Exception:
                    continue
            contract["options"] = normalized_opts

            # Find PENDING options for the requested season and apply decision.
            pending_indices = [
                i
                for i, opt in enumerate(contract["options"])
                if int(opt.get("season_year") or -1) == season_year_i
                and str(opt.get("status") or "").upper() == "PENDING"
            ]
            if not pending_indices:
                raise ValueError(
                    f"No pending option found for contract_id={contract_id}, season_year={season_year_i}"
                )

            for idx in pending_indices:
                apply_option_decision(contract, idx, decision, decision_date_iso)

            recompute_contract_years_from_salary(contract)

            # Ensure status doesn't get blanked (blank status would deactivate via upsert helper).
            if not contract.get("status"):
                contract["status"] = "ACTIVE" if contract.get("is_active", True) else ""

            self._upsert_contract_records_in_cur(cur, {str(contract_id): contract})

        return ServiceEvent(
            type="apply_contract_option_decision",
            payload={
                "contract_id": str(contract_id),
                "season_year": season_year_i,
                "decision": str(decision).strip().upper(),
                "decision_date": decision_date_iso,
                "options_updated": len(pending_indices),
                "years": int(contract.get("years") or 0) if isinstance(contract, dict) else None,
            },
        )

    def expire_contracts_for_season_transition(
        self,
        from_year: int,
        to_year: int,
        *,
        decision_policy: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Expire contracts and optionally release players (DB)."""
        from_year_i = int(from_year)
        to_year_i = int(to_year)
        decision_date_iso = _today_iso()

        # Build a robust decision function:
        # - callable -> use directly
        # - mapping -> allow overrides by (player_id, season_year) / player_id / contract_id
        # - None/other -> default policy
        default_fn = default_option_decision_policy
        if callable(decision_policy):
            policy_fn = decision_policy  # type: ignore[assignment]
        elif isinstance(decision_policy, Mapping):
            policy_map = decision_policy

            def policy_fn(option: dict, player_id: str, contract: dict, game_state: dict):
                key_pair = (str(player_id), int(option.get("season_year") or 0))
                if key_pair in policy_map:
                    return policy_map[key_pair]
                if str(player_id) in policy_map:
                    return policy_map[str(player_id)]
                cid = contract.get("contract_id")
                if cid in policy_map:
                    return policy_map[cid]
                return default_fn(option, player_id, contract, game_state)

        else:

            def policy_fn(option: dict, player_id: str, contract: dict, game_state: dict):
                return default_fn(option, player_id, contract, game_state)

        game_state_stub = {"league": {"season_year": to_year_i}}

        expired_contract_ids: List[str] = []
        released_player_ids: List[str] = []
        option_events: List[Dict[str, Any]] = []

        with self._atomic() as cur:
            active_rows = cur.execute(
                "SELECT player_id, contract_id FROM active_contracts;"
            ).fetchall()

            # Process each active contract: apply pending options for to_year, then expire if needed.
            for r in list(active_rows):
                player_id = str(r["player_id"] if hasattr(r, "keys") and "player_id" in r.keys() else r[0])
                contract_id = str(r["contract_id"] if hasattr(r, "keys") and "contract_id" in r.keys() else r[1])

                contract = self._load_contract_row_in_cur(cur, contract_id)

                # Normalize options (drop invalid ones)
                raw_opts = contract.get("options") or []
                normalized_opts: List[dict] = []
                for opt in raw_opts:
                    try:
                        normalized_opts.append(normalize_option_record(opt))
                    except Exception:
                        continue
                contract["options"] = normalized_opts

                # Apply pending options for the new season (to_year)
                pending = get_pending_options_for_season(contract, to_year_i)
                if pending:
                    for idx, opt in enumerate(contract["options"]):
                        if int(opt.get("season_year") or -1) != to_year_i:
                            continue
                        if str(opt.get("status") or "").upper() != "PENDING":
                            continue
                        decision = policy_fn(opt, player_id, contract, game_state_stub)
                        apply_option_decision(contract, idx, decision, decision_date_iso)
                        option_events.append(
                            {
                                "type": "contract_option_auto_decision",
                                "player_id": player_id,
                                "contract_id": contract_id,
                                "season_year": to_year_i,
                                "option_type": opt.get("type"),
                                "decision": str(decision).strip().upper(),
                                "decision_date": decision_date_iso,
                            }
                        )
                    recompute_contract_years_from_salary(contract)

                    # Preserve active status (blank status would deactivate on upsert)
                    if not contract.get("status"):
                        contract["status"] = "ACTIVE"

                    # Persist option-updated contract
                    self._upsert_contract_records_in_cur(cur, {contract_id: contract})

                # Determine expiry after option resolution
                try:
                    start_year = int(contract.get("start_season_year") or 0)
                except (TypeError, ValueError):
                    _warn_limited(
                        "CONTRACT_START_YEAR_COERCE_FAILED",
                        f"contract_id={contract_id} value={contract.get('start_season_year')!r}",
                        limit=3,
                    )
                    start_year = 0
                try:
                    years = int(contract.get("years") or 0)
                except (TypeError, ValueError):
                    _warn_limited(
                        "CONTRACT_YEARS_COERCE_FAILED",
                        f"contract_id={contract_id} value={contract.get('years')!r}",
                        limit=3,
                    )
                    years = 0

                end_exclusive = start_year + max(years, 0)

                if to_year_i >= end_exclusive:
                    # Expire + deactivate
                    contract["status"] = "EXPIRED"
                    contract["is_active"] = False
                    self._upsert_contract_records_in_cur(cur, {contract_id: contract})

                    # Remove active index
                    cur.execute("DELETE FROM active_contracts WHERE player_id=?;", (player_id,))
                    expired_contract_ids.append(contract_id)

                    # Release to FA (best-effort; don't fail whole transition if roster row missing)
                    try:
                        self._move_player_team_in_cur(cur, player_id, "FA")
                        released_player_ids.append(player_id)
                    except KeyError:
                        # No active roster row; skip release
                        pass
                else:
                    # Contract continues: update roster salary for the new season if we can.
                    new_salary = self._salary_for_season(contract, to_year_i)
                    if new_salary is not None:
                        self._set_roster_salary_in_cur(cur, player_id, int(new_salary))

        return {
            "from_year": from_year_i,
            "to_year": to_year_i,
            "expired": len(expired_contract_ids),
            "released": len(released_player_ids),
            "expired_contract_ids": expired_contract_ids,
            "released_player_ids": released_player_ids,
            "option_events": option_events,
        }


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
