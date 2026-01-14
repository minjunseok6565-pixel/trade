# league_repo.py
# Developer note:
# - SQLite DB is the single source of truth (SSOT).
# - Excel files are import/export only (no runtime reads/writes).
# - player_id and team_id are canonical strings.
# - Never use DataFrame indices as IDs; always use schema.py normalization helpers.
"""
LeagueRepository: single source of truth (SQLite)

Goal:
- Excel is import/export only.
- All runtime reads/writes go through SQLite.

Usage (CLI):
  python league_repo.py init --db league.db
  python league_repo.py import_roster --db league.db --excel roster.xlsx
  python league_repo.py validate --db league.db
  python league_repo.py export_roster --db league.db --excel roster_export.xlsx

Python:
  from league_repo import LeagueRepo
  repo = LeagueRepo("league.db")
  repo.import_roster_excel("roster.xlsx", mode="replace")
  team = repo.get_team_roster("ATL")
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# We strongly recommend keeping schema.py next to this file.
# It defines canonical IDs, stat keys, and normalization helpers.
try:
    from schema import (
        SCHEMA_VERSION,
        PlayerId,
        TeamId,
        normalize_player_id,
        normalize_team_id,
        season_id_from_year,
        assert_unique_ids,
        ROSTER_COL_PLAYER_ID,
        ROSTER_COL_TEAM_ID,
    )
except Exception as e:  # pragma: no cover
    raise ImportError(
        "schema.py is required. Put schema.py next to league_repo.py and retry.\n"
        f"Import error: {e}"
    )


# ----------------------------
# Helpers
# ----------------------------

_HEIGHT_RE = re.compile(r"^\s*(\d+)\s*'\s*(\d+)\s*\"?\s*$")
_WEIGHT_RE = re.compile(r"^\s*(\d+)\s*(?:lbs?)?\s*$", re.IGNORECASE)


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _json_dumps(obj: Any) -> str:
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )

def _json_loads(value: Any, default: Any):
    """
    Safe JSON loader:
    - None -> default
    - already dict/list -> returns as-is
    - invalid JSON -> default
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default

def parse_height_in(value: Any) -> Optional[int]:
    """Convert \"6' 5\"\" to inches. If unknown, return None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None
    m = _HEIGHT_RE.match(s)
    if not m:
        return None
    feet = int(m.group(1))
    inches = int(m.group(2))
    return feet * 12 + inches


def parse_weight_lb(value: Any) -> Optional[int]:
    """Convert \"205 lbs\" to 205. If unknown, return None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None
    m = _WEIGHT_RE.match(s.replace(",", ""))
    if not m:
        return None
    return int(m.group(1))


def parse_salary_int(value: Any) -> Optional[int]:
    """
    Parse salary into integer dollars.
    Accepts: 15161800, "15,161,800", "$15,161,800", etc.
    Returns None for empty/invalid.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return int(value)
        except Exception:
            return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None
    s = s.replace("$", "").replace(",", "")
    if not re.fullmatch(r"-?\d+", s):
        return None
    try:
        return int(s)
    except Exception:
        return None


def _require_columns(cols: Sequence[str], required: Sequence[str]) -> None:
    missing = [c for c in required if c not in cols]
    if missing:
        raise ValueError(f"Excel missing required columns: {missing}. Found: {list(cols)}")


# ----------------------------
# Data types
# ----------------------------

@dataclass(frozen=True)
class PlayerRow:
    player_id: str
    name: Optional[str]
    pos: Optional[str]
    age: Optional[int]
    height_in: Optional[int]
    weight_lb: Optional[int]
    ovr: Optional[int]
    attrs_json: str  # serialized dict


@dataclass(frozen=True)
class RosterRow:
    player_id: str
    team_id: str
    salary_amount: Optional[int]


# ----------------------------
# Repository
# ----------------------------

class LeagueRepo:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # autocommit mode; we manage BEGIN/COMMIT manually to guarantee atomic multi-table writes
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")  # good safety for frequent writes
        self._conn.execute("PRAGMA busy_timeout = 5000;")  # reduce transient 'database is locked'
        self._tx_depth = 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def transaction(self, *, write: bool = True):
        """Transaction helper.

        - Outermost: BEGIN (read) or BEGIN IMMEDIATE (write) on the connection.
        - Nested: SAVEPOINT/RELEASE so callers can safely nest repo/service transactions.
        """
        cur = self._conn.cursor()
        depth0 = self._tx_depth
        sp_name: str | None = None
        try:
            if depth0 == 0:
                self._conn.execute("BEGIN IMMEDIATE;" if write else "BEGIN;")
            else:
                sp_name = f"sp_{depth0}"
                cur.execute(f"SAVEPOINT {sp_name};")

            self._tx_depth += 1
            try:
                yield cur
            except Exception:
                if depth0 == 0:
                    self._conn.rollback()
                else:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                    cur.execute(f"RELEASE SAVEPOINT {sp_name};")
                raise
            else:
                if depth0 == 0:
                    self._conn.commit()
                else:
                    cur.execute(f"RELEASE SAVEPOINT {sp_name};")
            finally:
                self._tx_depth -= 1
        finally:
            cur.close()

    @contextlib.contextmanager
    def _maybe_transaction(self, cur: sqlite3.Cursor | None, *, write: bool = True):
        """Use provided cursor, or open a transaction and create a new cursor."""
        if cur is not None:
            yield cur
        else:
            with self.transaction(write=write) as cur2:
                yield cur2

    # ------------------------
    # Schema
    # ------------------------

    def _ensure_table_columns(self, cur: sqlite3.Cursor, table: str, columns: Mapping[str, str]) -> None:
        """SQLite에는 ADD COLUMN IF NOT EXISTS가 없어서 PRAGMA로 확인 후 추가한다."""
        rows = cur.execute(f"PRAGMA table_info({table});").fetchall()
        existing = {r["name"] for r in rows}
        for col, ddl in columns.items():
            if col in existing:
                continue
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl};")

    def init_db(self) -> None:
        if self._tx_depth != 0:
            # sqlite3 executescript() issues an implicit COMMIT; never run it inside an active transaction.
            raise RuntimeError("init_db() must not run inside an active transaction")
        now = _utc_now_iso()
        cur = self._conn.cursor()
        try:
            cur.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', '{now}');

                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT PRIMARY KEY,
                    name TEXT,
                    pos TEXT,
                    age INTEGER,
                    height_in INTEGER,
                    weight_lb INTEGER,
                    ovr INTEGER,
                    attrs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS roster (
                    player_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    salary_amount INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_roster_team_id ON roster(team_id);

                CREATE TABLE IF NOT EXISTS contracts (
                    contract_id TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    start_season_id TEXT,
                    end_season_id TEXT,
                    salary_by_season_json TEXT,
                    contract_type TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_contracts_player_id ON contracts(player_id);
                CREATE INDEX IF NOT EXISTS idx_contracts_team_id ON contracts(team_id);

                -- Draft picks (SSOT)
                CREATE TABLE IF NOT EXISTS draft_picks (
                    pick_id TEXT PRIMARY KEY,
                    year INTEGER NOT NULL,
                    round INTEGER NOT NULL,
                    original_team TEXT NOT NULL,
                    owner_team TEXT NOT NULL,
                    protection_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_draft_picks_owner ON draft_picks(owner_team);
                CREATE INDEX IF NOT EXISTS idx_draft_picks_year_round ON draft_picks(year, round);

                -- Swap rights (SSOT)
                CREATE TABLE IF NOT EXISTS swap_rights (
                    swap_id TEXT PRIMARY KEY,
                    pick_id_a TEXT NOT NULL,
                    pick_id_b TEXT NOT NULL,
                    year INTEGER,
                    round INTEGER,
                    owner_team TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by_deal_id TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_swap_rights_owner ON swap_rights(owner_team);
                CREATE INDEX IF NOT EXISTS idx_swap_rights_year_round ON swap_rights(year, round);

                -- Fixed assets (SSOT)
                CREATE TABLE IF NOT EXISTS fixed_assets (
                    asset_id TEXT PRIMARY KEY,
                    label TEXT,
                    value REAL,
                    owner_team TEXT NOT NULL,
                    source_pick_id TEXT,
                    draft_year INTEGER,
                    attrs_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fixed_assets_owner ON fixed_assets(owner_team);

                -- Transactions log (SSOT)
                CREATE TABLE IF NOT EXISTS transactions_log (
                    tx_hash TEXT PRIMARY KEY,
                    tx_type TEXT NOT NULL,
                    tx_date TEXT,
                    deal_id TEXT,
                    source TEXT,
                    teams_json TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions_log(tx_date);

                -- Contract indices (legacy-compatible SSOT)
                CREATE TABLE IF NOT EXISTS player_contracts (
                    player_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    PRIMARY KEY(player_id, contract_id),
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE,
                    FOREIGN KEY(contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS active_contracts (
                    player_id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE,
                    FOREIGN KEY(contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS free_agents (
                    player_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE
                );


                -- AI GM profiles (team_id -> JSON blob)
                CREATE TABLE IF NOT EXISTS gm_profiles (
                    team_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
          
        finally:
            cur.close()

        # Post-DDL migrations / column backfills should run in a normal write transaction.
        with self.transaction(write=True) as cur2:
            # Extend contracts table with full JSON storage (keeps contract shape stable across versions)
            self._ensure_table_columns(
                cur2,
                "contracts",
                {
                    "signed_date": "TEXT",
                    "start_season_year": "INTEGER",
                    "years": "INTEGER",
                    "options_json": "TEXT",
                    "status": "TEXT",
                    "contract_json": "TEXT",
                },
            )

    # ------------------------
    # Draft Picks / Swaps / Fixed Assets
    # ------------------------

    def upsert_draft_picks(self, picks_by_id: Mapping[str, Any], *, cur: sqlite3.Cursor | None = None) -> None:
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
            except Exception:
                year = 0
            try:
                rnd = int(pick.get("round") or 0)
            except Exception:
                rnd = 0
            original = str(pick.get("original_team") or "").upper()
            owner = str(pick.get("owner_team") or "").upper()
            protection = pick.get("protection")
            rows.append((pid, year, rnd, original, owner, _json_dumps(protection) if protection is not None else None, now, now))
        with self._maybe_transaction(cur, write=True) as cur:
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

    def ensure_draft_picks_seeded(self, draft_year: int, team_ids: Iterable[str], *, years_ahead: int = 7, cur: sqlite3.Cursor | None = None) -> None:
        now = _utc_now_iso()
        team_ids = [str(normalize_team_id(t, strict=False)).upper() for t in team_ids]
        with self._maybe_transaction(cur, write=True) as cur:
            for year in range(int(draft_year), int(draft_year) + int(years_ahead) + 1):
                for rnd in (1, 2):
                    for tid in team_ids:
                        pick_id = f"{year}_R{rnd}_{tid}"
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO draft_picks(pick_id, year, round, original_team, owner_team, protection_json, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, NULL, ?, ?);
                            """,
                            (pick_id, year, rnd, tid, tid, now, now),
                        )

    def upsert_swap_rights(self, swaps_by_id: Mapping[str, Any], *, cur: sqlite3.Cursor | None = None) -> None:
        if not swaps_by_id:
            return
        now = _utc_now_iso()
        rows = []
        for sid, swap in swaps_by_id.items():
            if not isinstance(swap, dict):
                continue
            swap_id = str(swap.get("swap_id") or sid)
            rows.append(
                (
                    swap_id,
                    str(swap.get("pick_id_a") or ""),
                    str(swap.get("pick_id_b") or ""),
                    int(swap.get("year") or 0) if str(swap.get("year") or "").isdigit() else None,
                    int(swap.get("round") or 0) if str(swap.get("round") or "").isdigit() else None,
                    str(swap.get("owner_team") or "").upper(),
                    1 if swap.get("active", True) else 0,
                    str(swap.get("created_by_deal_id") or "") if swap.get("created_by_deal_id") is not None else None,
                    str(swap.get("created_at") or now),
                    now,
                )
            )
        with self._maybe_transaction(cur, write=True) as cur:
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

    def upsert_fixed_assets(self, assets_by_id: Mapping[str, Any], *, cur: sqlite3.Cursor | None = None) -> None:
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
            rows.append((asset_id, str(label) if label is not None else None, value_f, owner, str(source_pick_id) if source_pick_id is not None else None, draft_year_i, _json_dumps(attrs), now, now))
        with self._maybe_transaction(cur, write=True) as cur:
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

    def _read_draft_picks_map(self, cur: sqlite3.Cursor) -> Dict[str, Dict[str, Any]]:
        rows = cur.execute(
            """
            SELECT pick_id, year, round, original_team, owner_team, protection_json
            FROM draft_picks;
            """
        ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            protection = _json_loads(r["protection_json"], None)
            pick = {
                "pick_id": str(r["pick_id"]),
                "year": int(r["year"]),
                "round": int(r["round"]),
                "original_team": str(r["original_team"]).upper(),
                "owner_team": str(r["owner_team"]).upper(),
                "protection": protection,
            }
            out[pick["pick_id"]] = pick
        return out

    def _read_swap_rights_map(self, cur: sqlite3.Cursor) -> Dict[str, Dict[str, Any]]:
        rows = cur.execute(
            """
            SELECT
                swap_id, pick_id_a, pick_id_b, year, round,
                owner_team, active, created_by_deal_id, created_at
            FROM swap_rights;
            """
        ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            swap = {
                "swap_id": str(r["swap_id"]),
                "pick_id_a": str(r["pick_id_a"]),
                "pick_id_b": str(r["pick_id_b"]),
                "year": int(r["year"]) if r["year"] is not None else None,
                "round": int(r["round"]) if r["round"] is not None else None,
                "owner_team": str(r["owner_team"]).upper(),
                "active": bool(int(r["active"]) if r["active"] is not None else 1),
                "created_by_deal_id": str(r["created_by_deal_id"]) if r["created_by_deal_id"] else None,
                "created_at": str(r["created_at"]) if r["created_at"] else None,
            }
            out[swap["swap_id"]] = swap
        return out

    def _read_fixed_assets_map(self, cur: sqlite3.Cursor) -> Dict[str, Dict[str, Any]]:
        rows = cur.execute(
            """
            SELECT
                asset_id, label, value, owner_team,
                source_pick_id, draft_year, attrs_json
            FROM fixed_assets;
            """
        ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            attrs = _json_loads(r["attrs_json"], {})
            if not isinstance(attrs, dict):
                attrs = {"value": attrs}
            asset = {
                "asset_id": str(r["asset_id"]),
                "label": str(r["label"]) if r["label"] is not None else None,
                "value": float(r["value"]) if r["value"] is not None else None,
                "owner_team": str(r["owner_team"]).upper(),
                "source_pick_id": str(r["source_pick_id"]) if r["source_pick_id"] else None,
                "draft_year": int(r["draft_year"]) if r["draft_year"] is not None else None,
                "attrs": attrs,
            }
            out[asset["asset_id"]] = asset
        return out

    def get_draft_picks_map(self) -> Dict[str, Dict[str, Any]]:
        """Legacy-compatible map: {pick_id -> pick_dict}"""
        cur = self._conn.cursor()
        return self._read_draft_picks_map(cur)

    def get_swap_rights_map(self) -> Dict[str, Dict[str, Any]]:
        """Legacy-compatible map: {swap_id -> swap_dict}"""
        cur = self._conn.cursor()
        return self._read_swap_rights_map(cur)

    def get_fixed_assets_map(self) -> Dict[str, Dict[str, Any]]:
        """Legacy-compatible map: {asset_id -> asset_dict}"""
        cur = self._conn.cursor()
        return self._read_fixed_assets_map(cur)

    def get_trade_assets_snapshot(self, *, cur: sqlite3.Cursor | None = None) -> Dict[str, Any]:
        """
        Read draft_picks / swap_rights / fixed_assets in one DB transaction
        so trade validation can use a consistent snapshot.
        """
        with self._maybe_transaction(cur, write=False) as cur:
            return {
                "draft_picks": self._read_draft_picks_map(cur),
                "swap_rights": self._read_swap_rights_map(cur),
                "fixed_assets": self._read_fixed_assets_map(cur),
            }

    # ------------------------
    # Transactions log
    # ------------------------

    def insert_transactions(self, entries: Sequence[Mapping[str, Any]], *, cur: sqlite3.Cursor | None = None) -> None:
        if not entries:
            return
        now = _utc_now_iso()
        rows = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            payload = _json_dumps(e)
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
        with self._maybe_transaction(cur, write=True) as cur:
            cur.executemany(
                """
                INSERT OR IGNORE INTO transactions_log(tx_hash, tx_type, tx_date, deal_id, source, teams_json, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def list_transactions(
        self,
        *,
        limit: int = 200,
        since_date: Optional[str] = None,
        deal_id: Optional[str] = None,
        tx_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Legacy-compatible list: returns list[dict] where each dict is the original payload_json.
        Filters are optional.
        """
        limit_i = max(1, int(limit))
        where = []
        params: List[Any] = []
        if since_date:
            where.append("tx_date >= ?")
            params.append(str(since_date))
        if deal_id:
            where.append("deal_id = ?")
            params.append(str(deal_id))
        if tx_type:
            where.append("tx_type = ?")
            params.append(str(tx_type))

        sql = "SELECT payload_json FROM transactions_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(tx_date,'') DESC, created_at DESC LIMIT ?"
        params.append(limit_i)

        rows = self._conn.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            payload = _json_loads(r["payload_json"], None)
            if isinstance(payload, dict):
                out.append(payload)
            else:
                out.append({"value": payload})
        return out

    # ------------------------
    # Contracts ledger (legacy-compatible SSOT)
    # ------------------------

    def upsert_contract_records(self, contracts_by_id: Mapping[str, Any], *, cur: sqlite3.Cursor | None = None) -> None:
        if not contracts_by_id:
            return
        now = _utc_now_iso()
        rows = []
        for cid, c in contracts_by_id.items():
            if not isinstance(c, dict):
                continue
            contract_id = str(c.get("contract_id") or cid)
            player_id = str(normalize_player_id(c.get("player_id"), strict=False, allow_legacy_numeric=True))
            team_id = c.get("team_id")
            team_id_norm = str(normalize_team_id(team_id, strict=False)).upper() if team_id else ""
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
            end_season_id = str(season_id_from_year(start_year_i + max((years_i or 1) - 1, 0))) if start_year_i and years_i else start_season_id
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
        with self._maybe_transaction(cur, write=True) as cur:
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

    def rebuild_contract_indices(self, *, cur: sqlite3.Cursor | None = None) -> None:
        """Rebuild derived index tables from SSOT sources.

        SSOT rules (as agreed):
          - free_agents: derived from roster.team_id == 'FA'
          - active_contracts: derived from contracts.is_active == 1
          - player_contracts: derived from contracts (player_id -> contract_id)

        Intended usage:
          - integrity repair / deterministic rebuilds
        """
        now = _utc_now_iso()
        with self._maybe_transaction(cur, write=True) as cur:
            # 1) player_contracts: one row per (player_id, contract_id) found in contracts.
            cur.execute("DELETE FROM player_contracts;")
            cur.execute(
                """
                INSERT OR IGNORE INTO player_contracts(player_id, contract_id)
                SELECT player_id, contract_id
                FROM contracts
                WHERE player_id IS NOT NULL AND contract_id IS NOT NULL;
                """
            )

            # 2) active_contracts: one active contract per player, based on contracts.is_active.
            cur.execute("DELETE FROM active_contracts;")
            active_rows = cur.execute(
                """
                SELECT contract_id, player_id, COALESCE(updated_at, created_at, '') AS ts
                FROM contracts
                WHERE is_active=1 AND player_id IS NOT NULL AND contract_id IS NOT NULL;
                """
            ).fetchall()
            best: Dict[str, Tuple[str, str]] = {}
            for r in active_rows:
                pid = str(r["player_id"])
                cid = str(r["contract_id"])
                ts = str(r["ts"] or "")
                prev = best.get(pid)
                if prev is None:
                    best[pid] = (ts, cid)
                    continue
                # Prefer newest timestamp; tie-break by contract_id for determinism.
                if ts > prev[0] or (ts == prev[0] and cid > prev[1]):
                    best[pid] = (ts, cid)

            if best:
                cur.executemany(
                    "INSERT OR REPLACE INTO active_contracts(player_id, contract_id, updated_at) VALUES (?, ?, ?);",
                    [(pid, cid, now) for pid, (_, cid) in best.items()],
                )

            # 3) free_agents: derived from roster team assignment.
            cur.execute("DELETE FROM free_agents;")
            cur.execute(
                """
                INSERT OR REPLACE INTO free_agents(player_id, updated_at)
                SELECT player_id, ?
                FROM roster
                WHERE status='active' AND UPPER(team_id)='FA' AND player_id IS NOT NULL;
                """,
                (now,),
            )   

    def ensure_contracts_bootstrapped_from_roster(self, season_year: int, *, cur: sqlite3.Cursor | None = None) -> None:
        """state에 contract ledger를 만들지 않고, DB contracts만 최소로 보장한다.

        - roster.status='active' 이면서 team_id != 'FA' 인 선수에 대해
          ACTIVE contract가 없으면 BOOT_{season_id}_{player_id} 로 1년 계약 생성
        """
        now = _utc_now_iso()
        season_year = int(season_year)
        season_id = str(season_id_from_year(season_year))
        with self._maybe_transaction(cur, write=True) as cur:
            rows = cur.execute("SELECT player_id, team_id, salary_amount FROM roster WHERE status='active';").fetchall()
            for r in rows:
                pid = str(normalize_player_id(r["player_id"], strict=False, allow_legacy_numeric=True))
                tid = str(r["team_id"] or "").upper()
                if tid == "FA":
                    continue
                # 이미 ACTIVE가 있으면 스킵
                exists = cur.execute(
                    "SELECT 1 FROM contracts WHERE player_id=? AND is_active=1 LIMIT 1;",
                    (pid,),
                ).fetchone()
                if exists:
                    continue
                contract_id = f"BOOT_{season_id}_{pid}"
                salary = float(r["salary_amount"] or 0.0)
                salary_by_year = {str(season_year): salary}
                contract_json = _json_dumps(
                    {
                        "contract_id": contract_id,
                        "player_id": pid,
                        "team_id": tid,
                        "signed_date": "1900-01-01",
                        "start_season_year": season_year,
                        "years": 1,
                        "salary_by_year": salary_by_year,
                        "options": [],
                        "status": "ACTIVE",
                    }
                )
                cur.execute(
                    """
                    INSERT OR IGNORE INTO contracts(
                        contract_id, player_id, team_id,
                        start_season_id, end_season_id,
                        salary_by_season_json, contract_type, is_active,
                        created_at, updated_at,
                        signed_date, start_season_year, years, options_json, status, contract_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        contract_id,
                        pid,
                        tid,
                        season_id,
                        season_id,
                        _json_dumps(salary_by_year),
                        "STANDARD",
                        1,
                        now,
                        now,
                        "1900-01-01",
                        season_year,
                        1,
                        "[]",
                        "ACTIVE",
                        contract_json,
                    ),
                )  


    def _contract_row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """
        Prefer contract_json if present (keeps old shape).
        Otherwise, synthesize a minimal legacy-friendly dict.
        """
        raw_json = None
        try:
            raw_json = row["contract_json"]
        except Exception:
            raw_json = None

        if raw_json:
            obj = _json_loads(raw_json, None)
            if isinstance(obj, dict):
                # Ensure key fields exist even if legacy json omitted them
                obj.setdefault("contract_id", str(row["contract_id"]))
                obj.setdefault("player_id", str(row["player_id"]))
                obj.setdefault("team_id", str(row["team_id"]).upper())
                return obj

        salary_by_year = _json_loads(row["salary_by_season_json"], {})
        if not isinstance(salary_by_year, dict):
            salary_by_year = {}
        options = _json_loads(getattr(row, "options_json", None) or row.get("options_json") if isinstance(row, dict) else None, [])
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

    def get_contracts_map(self, *, active_only: bool = False) -> Dict[str, Dict[str, Any]]:
        """Legacy-compatible map: {contract_id -> contract_dict}"""
        sql = "SELECT * FROM contracts"
        params: List[Any] = []
        if active_only:
            sql += " WHERE is_active=1"
        rows = self._conn.execute(sql, params).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            c = self._contract_row_to_dict(r)
            out[str(c.get("contract_id"))] = c
        return out

    def get_player_contracts_map(self) -> Dict[str, List[str]]:
        """Legacy-compatible map: {player_id -> [contract_id, ...]}"""
        rows = self._conn.execute(
            "SELECT player_id, contract_id FROM player_contracts;"
        ).fetchall()
        out: Dict[str, List[str]] = {}
        for r in rows:
            pid = str(r["player_id"])
            cid = str(r["contract_id"])
            out.setdefault(pid, []).append(cid)
        # deterministic ordering
        for pid in list(out.keys()):
            out[pid] = sorted(out[pid])
        return out

    def get_active_contract_id_by_player(self) -> Dict[str, str]:
        """
        Legacy-compatible map: {player_id -> contract_id}
        Prefer active_contracts table; fallback to contracts.is_active=1.
        """
        rows = self._conn.execute(
            "SELECT player_id, contract_id FROM active_contracts;"
        ).fetchall()
        if rows:
            return {str(r["player_id"]): str(r["contract_id"]) for r in rows}

        # Fallback: derive from contracts table
        rows2 = self._conn.execute(
            """
            SELECT player_id, contract_id
            FROM contracts
            WHERE is_active=1
            ORDER BY updated_at DESC;
            """
        ).fetchall()
        out: Dict[str, str] = {}
        for r in rows2:
            pid = str(r["player_id"])
            if pid in out:
                continue
            out[pid] = str(r["contract_id"])
        return out

    def list_free_agents(self, *, source: str = "roster") -> List[str]:
        """
        Legacy-compatible list: [player_id, ...]
        - source="roster" (default): derived from roster.team_id == 'FA' (recommended)
        - source="table": reads free_agents table (legacy)
        """
        src = (source or "roster").strip().lower()
        if src == "roster":
            rows = self._conn.execute(
                """
                SELECT player_id
                FROM roster
                WHERE status='active' AND UPPER(team_id)='FA';
                """
            ).fetchall()
            return [str(r["player_id"]) for r in rows]
        if src == "table":
            rows = self._conn.execute(
                "SELECT player_id FROM free_agents;"
            ).fetchall()
            return [str(r["player_id"]) for r in rows]
        raise ValueError(f"Unknown source for list_free_agents: {source}")

    def get_contract_ledger_snapshot(self, *, cur: sqlite3.Cursor | None = None) -> Dict[str, Any]:
        """Read contracts-related legacy keys in one transaction for consistency."""
        with self._maybe_transaction(cur, write=False) as _cur:
            # Use public methods for shape; reads happen under the same BEGIN snapshot.
            # (We intentionally call the public methods so any future normalization stays centralized.)
            return {
                "contracts": self.get_contracts_map(active_only=False),
                "player_contracts": self.get_player_contracts_map(),
                "active_contract_id_by_player": self.get_active_contract_id_by_player(),
                "free_agents": self.list_free_agents(source="roster"),
            }

    # ------------------------
    # GM Profiles (AI)
    # ------------------------

    def upsert_gm_profile(self, team_id: str, profile: Mapping[str, Any] | None, *, cur: sqlite3.Cursor | None = None) -> None:
        """Insert or update a single GM profile (stored as JSON)."""
        tid = normalize_team_id(team_id, strict=False)
        now = _utc_now_iso()
        payload = json.dumps(
            profile or {},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        with self._maybe_transaction(cur, write=True) as cur:
            cur.execute(
                """
                INSERT INTO gm_profiles(team_id, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at;
                """,
                (str(tid), payload, now, now),
           )

    def upsert_gm_profiles(self, profiles_by_team: Mapping[str, Any], *, cur: sqlite3.Cursor | None = None) -> None:
        """Bulk upsert GM profiles."""
        if not profiles_by_team:
            return
        now = _utc_now_iso()
        rows: List[Tuple[str, str, str, str]] = []
        for raw_team_id, raw_profile in profiles_by_team.items():
            tid = normalize_team_id(raw_team_id, strict=False)
            payload = json.dumps(
                raw_profile or {},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
            rows.append((str(tid), payload, now, now))

        with self._maybe_transaction(cur, write=True) as cur:
            cur.executemany(
                """
                INSERT INTO gm_profiles(team_id, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at;
                """,
                rows,
            )

    def get_gm_profile(self, team_id: str) -> Optional[Dict[str, Any]]:
        """Return the GM profile dict for a team, or None if missing."""
        tid = normalize_team_id(team_id, strict=False)
        row = self._conn.execute(
            "SELECT profile_json FROM gm_profiles WHERE team_id=?;", (str(tid),)
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["profile_json"])
            return value if isinstance(value, dict) else {"value": value}
        except Exception:
            # Defensive: if JSON is corrupted, don't crash the game loop.
            return None

    def get_all_gm_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Return all GM profiles keyed by team_id."""
        out: Dict[str, Dict[str, Any]] = {}
        rows = self._conn.execute(
            "SELECT team_id, profile_json FROM gm_profiles;"
        ).fetchall()
        for r in rows:
            try:
                value = json.loads(r["profile_json"])
                out[str(r["team_id"])] = value if isinstance(value, dict) else {"value": value}
            except Exception:
                continue
        return out

    def ensure_gm_profiles_seeded(
        self,
        team_ids: Iterable[str],
        *,
        default_profile: Optional[Mapping[str, Any]] = None,
        cur: sqlite3.Cursor | None = None,
    ) -> None:
        """Ensure each team_id has a row in gm_profiles (idempotent)."""
        ids = [str(normalize_team_id(t, strict=False)) for t in team_ids]
        if not ids:
            return
        now = _utc_now_iso()
        payload = json.dumps(
            default_profile or {},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
           default=str,
        )
        with self._maybe_transaction(cur, write=True) as cur2:
            existing = {
                str(r["team_id"])
                for r in cur2.execute(
                    "SELECT team_id FROM gm_profiles WHERE team_id IN (%s);"
                    % ",".join(["?"] * len(ids)),
                    ids,
                ).fetchall()
            }
            missing = [tid for tid in ids if tid not in existing]
            if not missing:
                return
            rows = [(tid, payload, now, now) for tid in missing]
            cur2.executemany(
                """
                INSERT OR IGNORE INTO gm_profiles(team_id, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?);
                """,
                rows,
            )

    # ------------------------
    # Import / Export (Excel)
    # ------------------------

    def import_roster_excel(
        self,
        excel_path: str | Path,
        *,
        sheet_name: Optional[str] = None,
        mode: str = "replace",  # "replace" or "upsert"
        strict_ids: bool = True,
        cur: sqlite3.Cursor | None = None,
    ) -> None:
        """
        Import roster Excel into SQLite.

        mode:
          - replace: wipe players/roster and re-import
          - upsert: update existing, insert new, do not delete missing rows
        strict_ids:
          - enforce PlayerID format (recommended P000001). If False, allows any non-empty string.
        """
        import pandas as pd  # local import so repo can be used without pandas in non-import contexts

        excel_path = str(excel_path)
        df = pd.read_excel(excel_path, sheet_name=(sheet_name if sheet_name is not None else 0))
        df_columns = list(df.columns)

        _require_columns(df_columns, [ROSTER_COL_TEAM_ID, ROSTER_COL_PLAYER_ID])

        # Basic cleaning: strip whitespace in key columns
        df[ROSTER_COL_TEAM_ID] = df[ROSTER_COL_TEAM_ID].astype(str).str.strip()
        df[ROSTER_COL_PLAYER_ID] = df[ROSTER_COL_PLAYER_ID].astype(str).str.strip()

        # Validate uniqueness of player_id inside this file
        assert_unique_ids(df[ROSTER_COL_PLAYER_ID].tolist(), what="player_id (in Excel)")

        players: List[PlayerRow] = []
        roster: List[RosterRow] = []

        # Columns we treat as "core" (not attributes)
        core_cols = {
            ROSTER_COL_TEAM_ID, ROSTER_COL_PLAYER_ID,
            "Name", "name",
            "POS", "pos",
            "Age", "age",
            "HT", "height", "height_in",
            "WT", "weight", "weight_lb",
            "Salary", "salary", "salary_amount",
            "OVR", "ovr",
        }

        for _, row in df.iterrows():
            raw_pid = row.get(ROSTER_COL_PLAYER_ID)
            raw_tid = row.get(ROSTER_COL_TEAM_ID)

            pid = normalize_player_id(raw_pid, strict=strict_ids, allow_legacy_numeric=not strict_ids)
            tid = normalize_team_id(raw_tid, strict=True)

            # pick best name/pos column
            name = row.get("name", None)
            if name is None:
                name = row.get("Name", None)
            pos = row.get("pos", None)
            if pos is None:
                pos = row.get("POS", None)

            # age
            age = row.get("age", None)
            if age is None:
                age = row.get("Age", None)
            try:
                age_i = int(age) if age is not None and str(age).strip() != "" else None
            except Exception:
                age_i = None

            # height / weight
            ht = row.get("height_in", None)
            if ht is None:
                ht = row.get("HT", None)
            height_in = parse_height_in(ht) if not isinstance(ht, (int, float)) else int(ht)

            wt = row.get("weight_lb", None)
            if wt is None:
                wt = row.get("WT", None)
            weight_lb = parse_weight_lb(wt) if not isinstance(wt, (int, float)) else int(wt)

            # salary
            sal = row.get("salary_amount", None)
            if sal is None:
                sal = row.get("Salary", None)
            salary_amount = parse_salary_int(sal)

            # ovr
            ovr = row.get("ovr", None)
            if ovr is None:
                ovr = row.get("OVR", None)
            try:
                ovr_i = int(ovr) if ovr is not None and str(ovr).strip() != "" else None
            except Exception:
                ovr_i = None

            # attributes: any columns not in core
            attrs: Dict[str, Any] = {}
            for col in df_columns:
                if col in core_cols:
                    continue
                v = row.get(col)
                # keep NaN out of JSON
                if v is None:
                    continue
                try:
                    # pandas NaN check without importing numpy directly
                    if isinstance(v, float) and v != v:
                        continue
                except Exception:
                    pass
                attrs[col] = v

            players.append(
                PlayerRow(
                    player_id=str(pid),
                    name=str(name) if name is not None else None,
                    pos=str(pos) if pos is not None else None,
                    age=age_i,
                    height_in=height_in,
                    weight_lb=weight_lb,
                    ovr=ovr_i,
                    attrs_json=json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
                )
            )
            roster.append(RosterRow(player_id=str(pid), team_id=str(tid), salary_amount=salary_amount))

        now = _utc_now_iso()
        # Ensure schema exists before transactional import (only when we manage the transaction here)
        if cur is None:
            self.init_db()
        with self._maybe_transaction(cur, write=True) as cur:

            if mode == "replace":
                cur.execute("DELETE FROM roster;")
                cur.execute("DELETE FROM contracts;")
                cur.execute("DELETE FROM players;")
            elif mode == "upsert":
                pass
            else:
                raise ValueError("mode must be 'replace' or 'upsert'")

            # Upsert players
            cur.executemany(
                """
                INSERT INTO players(player_id, name, pos, age, height_in, weight_lb, ovr, attrs_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id) DO UPDATE SET
                    name=excluded.name,
                    pos=excluded.pos,
                    age=excluded.age,
                    height_in=excluded.height_in,
                    weight_lb=excluded.weight_lb,
                    ovr=excluded.ovr,
                    attrs_json=excluded.attrs_json,
                    updated_at=excluded.updated_at;
                """,
                [(p.player_id, p.name, p.pos, p.age, p.height_in, p.weight_lb, p.ovr, p.attrs_json, now, now) for p in players],
            )

            # Upsert roster
            cur.executemany(
                """
                INSERT INTO roster(player_id, team_id, salary_amount, status, updated_at)
                VALUES(?, ?, ?, 'active', ?)
                ON CONFLICT(player_id) DO UPDATE SET
                    team_id=excluded.team_id,
                    salary_amount=excluded.salary_amount,
                    status='active',
                    updated_at=excluded.updated_at;
                """,
                [(r.player_id, r.team_id, r.salary_amount, now) for r in roster],
            )

        # Validate after import
        self.validate_integrity(strict_ids=strict_ids)

    def export_roster_excel(self, excel_path: str | Path) -> None:
        """Export canonical roster table back to Excel."""
        import pandas as pd

        rows = self._conn.execute(
            """
            SELECT r.team_id, p.player_id, p.name, p.pos, p.age, p.height_in, p.weight_lb, r.salary_amount, p.ovr, p.attrs_json
            FROM roster r
            JOIN players p ON p.player_id = r.player_id
            WHERE r.status='active'
            ORDER BY r.team_id, p.player_id;
            """
        ).fetchall()

        out: List[Dict[str, Any]] = []
        for r in rows:
            attrs = json.loads(r["attrs_json"]) if r["attrs_json"] else {}
            base = {
                "team_id": r["team_id"],
                "player_id": r["player_id"],
                "name": r["name"],
                "pos": r["pos"],
                "age": r["age"],
                "height_in": r["height_in"],
                "weight_lb": r["weight_lb"],
                "salary_amount": r["salary_amount"],
                "ovr": r["ovr"],
            }
            base.update(attrs)
            out.append(base)

        df = pd.DataFrame(out)
        df.to_excel(str(excel_path), index=False)

    # ------------------------
    # Reads
    # ------------------------

    def get_player(self, player_id: str) -> Dict[str, Any]:
        pid = normalize_player_id(player_id, strict=False)
        row = self._conn.execute("SELECT * FROM players WHERE player_id=?", (str(pid),)).fetchone()
        if not row:
            raise KeyError(f"player not found: {player_id}")
        d = dict(row)
        d["player_id"] = str(d.get("player_id"))
        d["attrs"] = json.loads(d["attrs_json"]) if d.get("attrs_json") else {}
        return d

    def get_team_roster(self, team_id: str) -> List[Dict[str, Any]]:
        tid = normalize_team_id(team_id, strict=True)
        rows = self._conn.execute(
            """
            SELECT p.player_id, p.name, p.pos, p.age, p.height_in, p.weight_lb, p.ovr, r.salary_amount, p.attrs_json
            FROM roster r
            JOIN players p ON p.player_id = r.player_id
            WHERE r.team_id=? AND r.status='active'
            ORDER BY p.ovr DESC, p.player_id ASC;
            """,
            (str(tid),),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["player_id"] = str(d.get("player_id"))
            d["attrs"] = json.loads(d["attrs_json"]) if d.get("attrs_json") else {}
            out.append(d)
        return out

    def get_team_id_by_player(self, player_id: str) -> str:
        pid = normalize_player_id(player_id, strict=False, allow_legacy_numeric=True)
        row = self._conn.execute(
            "SELECT team_id FROM roster WHERE player_id=? AND status='active';",
            (str(pid),),
        ).fetchone()
        if not row:
            raise KeyError(f"active roster entry not found for player_id={player_id}")
        return str(row["team_id"])

    def get_salary_amount(self, player_id: str) -> Optional[int]:
        pid = normalize_player_id(player_id, strict=False, allow_legacy_numeric=True)
        row = self._conn.execute(
            "SELECT salary_amount FROM roster WHERE player_id=? AND status='active';",
            (str(pid),),
        ).fetchone()
        if not row:
            return None
        salary = row["salary_amount"]
        return int(salary) if salary is not None else None

    def get_roster_player_ids(self, team_id: str) -> set[str]:
        tid = normalize_team_id(team_id, strict=True)
        rows = self._conn.execute(
            "SELECT player_id FROM roster WHERE team_id=? AND status='active';",
            (str(tid),),
        ).fetchall()
        return {str(r["player_id"]) for r in rows}

    def get_all_player_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT player_id FROM players;").fetchall()
        return {str(r["player_id"]) for r in rows}

    def list_teams(self) -> List[str]:
        rows = self._conn.execute("SELECT DISTINCT team_id FROM roster WHERE status='active' ORDER BY team_id;").fetchall()
        return [r["team_id"] for r in rows]

    # ------------------------
    # Writes (Roster operations)
    # ------------------------

    def trade_player(self, player_id: str, to_team_id: str, *, cur: sqlite3.Cursor | None = None) -> None:
        """Move player to another team."""
        pid = normalize_player_id(player_id, strict=False)
        to_tid = normalize_team_id(to_team_id, strict=True)
        now = _utc_now_iso()

        with self._maybe_transaction(cur, write=True) as cur:
            # Must exist in roster
            exists = cur.execute("SELECT team_id FROM roster WHERE player_id=? AND status='active';", (str(pid),)).fetchone()
            if not exists:
                raise KeyError(f"active roster entry not found for player_id={player_id}")

            cur.execute(
                "UPDATE roster SET team_id=?, updated_at=? WHERE player_id=?;",
                (str(to_tid), now, str(pid)),
            )
            # If there's an active contract, update team_id too (optional, but helps consistency)
            cur.execute(
                "UPDATE contracts SET team_id=?, updated_at=? WHERE player_id=? AND is_active=1;",
                (str(to_tid), now, str(pid)),
            )

    def release_to_free_agency(self, player_id: str, *, cur: sqlite3.Cursor | None = None) -> None:
        """Set team_id to FA."""
        self.trade_player(player_id, "FA", cur=cur)

    def set_salary(self, player_id: str, salary_amount: int, *, cur: sqlite3.Cursor | None = None) -> None:
        pid = normalize_player_id(player_id, strict=False)
        now = _utc_now_iso()
        with self._maybe_transaction(cur, write=True) as cur:
            cur.execute(
                "UPDATE roster SET salary_amount=?, updated_at=? WHERE player_id=?;",
                (int(salary_amount), now, str(pid)),
            )

    # ------------------------
    # Integrity
    # ------------------------

    def validate_integrity(self, *, strict_ids: bool = True) -> None:
        """
        Fail fast on ID split / missing rows / invalid team codes.
        Run this after imports and after any batch roster changes.
        """
        # schema version check
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version';").fetchone()
        if not row:
            raise ValueError("DB meta.schema_version missing (run init_db)")
        if row["value"] != SCHEMA_VERSION:
            raise ValueError(f"DB schema_version {row['value']} != expected {SCHEMA_VERSION}")

        # player_id uniqueness is enforced by PK; also validate format if strict
        if strict_ids:
            rows = self._conn.execute("SELECT player_id FROM players;").fetchall()
            for r in rows:
                normalize_player_id(r["player_id"], strict=True)

        # roster must reference existing players (FK enforces, but keep explicit check)
        bad = self._conn.execute(
            """
            SELECT r.player_id
            FROM roster r
            LEFT JOIN players p ON p.player_id = r.player_id
            WHERE p.player_id IS NULL;
            """
        ).fetchall()
        if bad:
            raise ValueError(f"roster has player_ids missing in players: {[x['player_id'] for x in bad]}")

        # team_id normalization check
        rows = self._conn.execute("SELECT DISTINCT team_id FROM roster WHERE status='active';").fetchall()
        for r in rows:
            normalize_team_id(r["team_id"], strict=True)

        # No duplicate active roster entries (PK ensures), but check status sanity
        rows = self._conn.execute("SELECT COUNT(*) AS c FROM roster WHERE status='active';").fetchone()
        if rows and rows["c"] <= 0:
            raise ValueError("no active roster entries found")

    def _smoke_check(self) -> None:
        """
        Lightweight self-check for repo wiring.
        Runs init_db(), and only validates if there is roster data present.
        """
        self.init_db()
        has_roster = self._conn.execute("SELECT 1 FROM roster LIMIT 1;").fetchone()
        if has_roster:
            self.validate_integrity()

    # ------------------------
    # Convenience
    # ------------------------

    def __enter__(self) -> "LeagueRepo":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ----------------------------
# CLI
# ----------------------------

def _cmd_init(args) -> None:
    with LeagueRepo(args.db) as repo:
        repo.init_db()
    print(f"OK: initialized {args.db}")

def _cmd_import_roster(args) -> None:
    with LeagueRepo(args.db) as repo:
        repo.import_roster_excel(args.excel, sheet_name=args.sheet, mode=args.mode, strict_ids=not args.allow_legacy_ids)
    print(f"OK: imported roster from {args.excel} into {args.db}")

def _cmd_export_roster(args) -> None:
    with LeagueRepo(args.db) as repo:
        repo.export_roster_excel(args.excel)
    print(f"OK: exported roster to {args.excel}")

def _cmd_validate(args) -> None:
    with LeagueRepo(args.db) as repo:
        repo.validate_integrity(strict_ids=not args.allow_legacy_ids)
    print(f"OK: validation passed for {args.db}")

def main(argv: Optional[Sequence[str]] = None) -> None:
    p = argparse.ArgumentParser(description="LeagueRepo (SQLite single source of truth)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="initialize DB schema")
    p_init.add_argument("--db", required=True, help="path to sqlite db file")
    p_init.set_defaults(func=_cmd_init)

    p_imp = sub.add_parser("import_roster", help="import roster excel into DB")
    p_imp.add_argument("--db", required=True, help="path to sqlite db file")
    p_imp.add_argument("--excel", required=True, help="path to roster excel file")
    p_imp.add_argument("--sheet", default=None, help="sheet name (optional)")
    p_imp.add_argument("--mode", choices=["replace", "upsert"], default="replace")
    p_imp.add_argument("--allow-legacy-ids", action="store_true", help="allow non-P000001 style player_id")
    p_imp.set_defaults(func=_cmd_import_roster)

    p_exp = sub.add_parser("export_roster", help="export roster from DB to excel")
    p_exp.add_argument("--db", required=True, help="path to sqlite db file")
    p_exp.add_argument("--excel", required=True, help="output excel path")
    p_exp.set_defaults(func=_cmd_export_roster)

    p_val = sub.add_parser("validate", help="validate DB integrity")
    p_val.add_argument("--db", required=True, help="path to sqlite db file")
    p_val.add_argument("--allow-legacy-ids", action="store_true", help="allow non-P000001 style player_id")
    p_val.set_defaults(func=_cmd_validate)

    args = p.parse_args(argv)
    args.func(args)

if __name__ == "__main__":
    main()
