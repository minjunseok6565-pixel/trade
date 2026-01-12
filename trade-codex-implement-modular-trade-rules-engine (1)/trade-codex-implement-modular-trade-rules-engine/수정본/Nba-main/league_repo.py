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
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")  # good safety for frequent writes

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def transaction(self):
        """Atomic transaction helper (safe even if executescript commits internally)."""
        cur = self._conn.cursor()
        try:
            self._conn.execute("BEGIN;")
            yield cur
            # conn.commit() is safe even if no transaction is active
            self._conn.commit()
        except Exception:
            # conn.rollback() is safe even if no transaction is active
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ------------------------
    # Schema
    # ------------------------

    def init_db(self) -> None:
        """Create tables if they don't exist."""
        now = _utc_now_iso()
        with self.transaction() as cur:
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

                CREATE TABLE IF NOT EXISTS trade_agreements (
                    deal_id TEXT PRIMARY KEY,
                    deal_json TEXT NOT NULL,
                    assets_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS asset_locks (
                    asset_key TEXT PRIMARY KEY,
                    deal_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(deal_id) REFERENCES trade_agreements(deal_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_asset_locks_deal_id ON asset_locks(deal_id);

                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    entry_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS negotiations (
                    session_id TEXT PRIMARY KEY,
                    session_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS draft_picks (
                    pick_id TEXT PRIMARY KEY,
                    year INTEGER NOT NULL,
                    round INTEGER NOT NULL,
                    original_team TEXT NOT NULL,
                    owner_team TEXT NOT NULL,
                    protection_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_draft_picks_year ON draft_picks(year);
                CREATE INDEX IF NOT EXISTS idx_draft_picks_owner_team ON draft_picks(owner_team);

                CREATE TABLE IF NOT EXISTS swap_rights (
                    swap_id TEXT PRIMARY KEY,
                    year INTEGER NOT NULL,
                    pick_id_a TEXT NOT NULL,
                    pick_id_b TEXT NOT NULL,
                    owner_team TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_swap_rights_year ON swap_rights(year);
                CREATE INDEX IF NOT EXISTS idx_swap_rights_owner_team ON swap_rights(owner_team);

                CREATE TABLE IF NOT EXISTS fixed_assets (
                    asset_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    value REAL NOT NULL,
                    owner_team TEXT NOT NULL,
                    source_pick_id TEXT,
                    draft_year INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_fixed_assets_owner_team ON fixed_assets(owner_team);

                CREATE TABLE IF NOT EXISTS player_state (
                    player_id TEXT PRIMARY KEY,
                    last_contract_action_type TEXT,
                    last_contract_action_date TEXT,
                    signed_via_free_agency INTEGER NOT NULL DEFAULT 0,
                    signed_date TEXT,
                    acquired_via_trade INTEGER NOT NULL DEFAULT 0,
                    acquired_date TEXT,
                    trade_return_bans_json TEXT
                );
                """
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
        # Ensure schema exists before transactional import
        self.init_db()
        with self.transaction() as cur:

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
    # Draft picks / swaps / fixed assets
    # ------------------------

    def get_pick(self, pick_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """
            SELECT pick_id, year, round, original_team, owner_team, protection_json
            FROM draft_picks
            WHERE pick_id=?;
            """,
            (str(pick_id),),
        ).fetchone()
        return self._row_to_pick(row) if row else None

    def list_picks(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT pick_id, year, round, original_team, owner_team, protection_json
            FROM draft_picks;
            """
        ).fetchall()
        return [self._row_to_pick(row) for row in rows]

    def list_picks_by_year(self, year: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT pick_id, year, round, original_team, owner_team, protection_json
            FROM draft_picks
            WHERE year=?;
            """,
            (int(year),),
        ).fetchall()
        return [self._row_to_pick(row) for row in rows]

    def list_picks_by_owner(self, owner_team: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT pick_id, year, round, original_team, owner_team, protection_json
            FROM draft_picks
            WHERE owner_team=?;
            """,
            (str(owner_team).upper(),),
        ).fetchall()
        return [self._row_to_pick(row) for row in rows]

    def get_picks_by_ids(self, pick_ids: Iterable[str]) -> List[Dict[str, Any]]:
        ids = [str(pid) for pid in pick_ids if pid]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"""
            SELECT pick_id, year, round, original_team, owner_team, protection_json
            FROM draft_picks
            WHERE pick_id IN ({placeholders});
            """,
            ids,
        ).fetchall()
        return [self._row_to_pick(row) for row in rows]

    def upsert_pick(self, pick: Dict[str, Any], *, cursor=None) -> None:
        protection = pick.get("protection")
        protection_json = json.dumps(protection) if protection is not None else None
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT INTO draft_picks(
                pick_id,
                year,
                round,
                original_team,
                owner_team,
                protection_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pick_id) DO UPDATE SET
                year=excluded.year,
                round=excluded.round,
                original_team=excluded.original_team,
                owner_team=excluded.owner_team,
                protection_json=excluded.protection_json;
            """,
            (
                str(pick.get("pick_id")),
                int(pick.get("year") or 0),
                int(pick.get("round") or 0),
                str(pick.get("original_team") or "").upper(),
                str(pick.get("owner_team") or "").upper(),
                protection_json,
            ),
        )

    def upsert_picks(self, picks: Iterable[Dict[str, Any]], *, cursor=None) -> None:
        cur = cursor or self._conn
        rows = []
        for pick in picks:
            protection = pick.get("protection")
            protection_json = json.dumps(protection) if protection is not None else None
            rows.append(
                (
                    str(pick.get("pick_id")),
                    int(pick.get("year") or 0),
                    int(pick.get("round") or 0),
                    str(pick.get("original_team") or "").upper(),
                    str(pick.get("owner_team") or "").upper(),
                    protection_json,
                )
            )
        cur.executemany(
            """
            INSERT INTO draft_picks(
                pick_id,
                year,
                round,
                original_team,
                owner_team,
                protection_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pick_id) DO NOTHING;
            """,
            rows,
        )

    def update_pick_owner(self, pick_id: str, owner_team: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute(
            "UPDATE draft_picks SET owner_team=? WHERE pick_id=?;",
            (str(owner_team).upper(), str(pick_id)),
        )

    def set_pick_protection(self, pick_id: str, protection: Optional[Dict[str, Any]], *, cursor=None) -> None:
        protection_json = json.dumps(protection) if protection is not None else None
        cur = cursor or self._conn
        cur.execute(
            "UPDATE draft_picks SET protection_json=? WHERE pick_id=?;",
            (protection_json, str(pick_id)),
        )

    def clear_pick_protection(self, pick_id: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute("UPDATE draft_picks SET protection_json=NULL WHERE pick_id=?;", (str(pick_id),))

    def get_swap(self, swap_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """
            SELECT swap_id, year, pick_id_a, pick_id_b, owner_team, active
            FROM swap_rights
            WHERE swap_id=?;
            """,
            (str(swap_id),),
        ).fetchone()
        return self._row_to_swap(row) if row else None

    def list_swaps_by_year(self, year: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT swap_id, year, pick_id_a, pick_id_b, owner_team, active
            FROM swap_rights
            WHERE year=?;
            """,
            (int(year),),
        ).fetchall()
        return [self._row_to_swap(row) for row in rows]

    def list_swaps(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT swap_id, year, pick_id_a, pick_id_b, owner_team, active FROM swap_rights;"
        ).fetchall()
        return [self._row_to_swap(row) for row in rows]

    def upsert_swap(self, swap: Dict[str, Any], *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT INTO swap_rights(
                swap_id,
                year,
                pick_id_a,
                pick_id_b,
                owner_team,
                active
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(swap_id) DO UPDATE SET
                year=excluded.year,
                pick_id_a=excluded.pick_id_a,
                pick_id_b=excluded.pick_id_b,
                owner_team=excluded.owner_team,
                active=excluded.active;
            """,
            (
                str(swap.get("swap_id")),
                int(swap.get("year") or 0),
                str(swap.get("pick_id_a")),
                str(swap.get("pick_id_b")),
                str(swap.get("owner_team") or "").upper(),
                1 if swap.get("active", True) else 0,
            ),
        )

    def deactivate_swap(self, swap_id: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute("UPDATE swap_rights SET active=0 WHERE swap_id=?;", (str(swap_id),))

    def get_fixed_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """
            SELECT asset_id, label, value, owner_team, source_pick_id, draft_year
            FROM fixed_assets
            WHERE asset_id=?;
            """,
            (str(asset_id),),
        ).fetchone()
        return dict(row) if row else None

    def upsert_fixed_asset(self, asset: Dict[str, Any], *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT INTO fixed_assets(
                asset_id,
                label,
                value,
                owner_team,
                source_pick_id,
                draft_year
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                label=excluded.label,
                value=excluded.value,
                owner_team=excluded.owner_team,
                source_pick_id=excluded.source_pick_id,
                draft_year=excluded.draft_year;
            """,
            (
                str(asset.get("asset_id")),
                str(asset.get("label") or ""),
                float(asset.get("value") or 0),
                str(asset.get("owner_team") or "").upper(),
                asset.get("source_pick_id"),
                asset.get("draft_year"),
            ),
        )

    def list_fixed_assets_by_owner(self, owner_team: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT asset_id, label, value, owner_team, source_pick_id, draft_year
            FROM fixed_assets
            WHERE owner_team=?;
            """,
            (str(owner_team).upper(),),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------
    # Asset locks
    # ------------------------

    def get_asset_lock(self, asset_key_value: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT asset_key, deal_id, expires_at FROM asset_locks WHERE asset_key=?;",
            (str(asset_key_value),),
        ).fetchone()
        return dict(row) if row else None

    def lock_asset(self, asset_key_value: str, deal_id: str, expires_at: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT OR REPLACE INTO asset_locks(asset_key, deal_id, expires_at)
            VALUES (?, ?, ?);
            """,
            (str(asset_key_value), str(deal_id), str(expires_at)),
        )

    def update_asset_lock_expires(self, asset_key_value: str, expires_at: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute(
            "UPDATE asset_locks SET expires_at=? WHERE asset_key=?;",
            (str(expires_at), str(asset_key_value)),
        )

    def release_asset_lock(self, asset_key_value: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute("DELETE FROM asset_locks WHERE asset_key=?;", (str(asset_key_value),))

    def release_asset_locks_for_deal(self, deal_id: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute("DELETE FROM asset_locks WHERE deal_id=?;", (str(deal_id),))

    # ------------------------
    # Trade agreements
    # ------------------------

    def save_trade_agreement(
        self,
        *,
        deal_id: str,
        deal_json: str,
        assets_hash: str,
        created_at: str,
        expires_at: str,
        status: str,
        cursor=None,
    ) -> None:
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT OR REPLACE INTO trade_agreements(
                deal_id,
                deal_json,
                assets_hash,
                created_at,
                expires_at,
                status
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (deal_id, deal_json, assets_hash, created_at, expires_at, status),
        )

    def get_trade_agreement(self, deal_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM trade_agreements WHERE deal_id=?;",
            (str(deal_id),),
        ).fetchone()
        return dict(row) if row else None

    def update_trade_agreement_status(self, deal_id: str, status: str, *, cursor=None) -> None:
        cur = cursor or self._conn
        cur.execute(
            "UPDATE trade_agreements SET status=? WHERE deal_id=?;",
            (str(status), str(deal_id)),
        )

    def list_active_trade_agreements(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT deal_id, expires_at FROM trade_agreements WHERE status='ACTIVE';"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------
    # Transactions / negotiations
    # ------------------------

    def log_transaction(self, created_at: str, entry_json: str, *, cursor=None) -> int:
        cur = cursor or self._conn
        cur.execute(
            "INSERT INTO transactions(created_at, entry_json) VALUES (?, ?);",
            (created_at, entry_json),
        )
        return int(cur.lastrowid)

    def list_transactions(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT transaction_id, created_at, entry_json FROM transactions ORDER BY transaction_id;"
        ).fetchall()
        return [dict(row) for row in rows]

    def save_negotiation(
        self,
        *,
        session_id: str,
        session_json: str,
        status: str,
        created_at: str,
        updated_at: str,
        cursor=None,
    ) -> None:
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT INTO negotiations(
                session_id,
                session_json,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (session_id, session_json, status, created_at, updated_at),
        )

    def update_negotiation(
        self,
        *,
        session_id: str,
        session_json: str,
        status: str,
        updated_at: str,
        cursor=None,
    ) -> None:
        cur = cursor or self._conn
        cur.execute(
            """
            UPDATE negotiations
            SET session_json=?, status=?, updated_at=?
            WHERE session_id=?;
            """,
            (session_json, status, updated_at, session_id),
        )

    def get_negotiation(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT session_json FROM negotiations WHERE session_id=?;",
            (str(session_id),),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------
    # Player state
    # ------------------------

    def get_player_state(self, player_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """
            SELECT
                last_contract_action_type,
                last_contract_action_date,
                signed_via_free_agency,
                signed_date,
                acquired_via_trade,
                acquired_date,
                trade_return_bans_json
            FROM player_state
            WHERE player_id=?;
            """,
            (str(player_id),),
        ).fetchone()
        if not row:
            return None
        trade_return_bans = {}
        trade_return_bans_raw = row["trade_return_bans_json"]
        if trade_return_bans_raw:
            trade_return_bans = json.loads(trade_return_bans_raw)
        return {
            "last_contract_action_type": row["last_contract_action_type"],
            "last_contract_action_date": row["last_contract_action_date"],
            "signed_via_free_agency": bool(row["signed_via_free_agency"]),
            "signed_date": row["signed_date"] or "1900-01-01",
            "acquired_via_trade": bool(row["acquired_via_trade"]),
            "acquired_date": row["acquired_date"] or "1900-01-01",
            "trade_return_bans": trade_return_bans,
        }

    def upsert_player_state(self, player_id: str, state: Dict[str, Any], *, cursor=None) -> None:
        trade_return_bans = state.get("trade_return_bans") or {}
        trade_return_bans_json = json.dumps(trade_return_bans)
        cur = cursor or self._conn
        cur.execute(
            """
            INSERT INTO player_state(
                player_id,
                last_contract_action_type,
                last_contract_action_date,
                signed_via_free_agency,
                signed_date,
                acquired_via_trade,
                acquired_date,
                trade_return_bans_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                last_contract_action_type=excluded.last_contract_action_type,
                last_contract_action_date=excluded.last_contract_action_date,
                signed_via_free_agency=excluded.signed_via_free_agency,
                signed_date=excluded.signed_date,
                acquired_via_trade=excluded.acquired_via_trade,
                acquired_date=excluded.acquired_date,
                trade_return_bans_json=excluded.trade_return_bans_json;
            """,
            (
                str(player_id),
                state.get("last_contract_action_type"),
                state.get("last_contract_action_date"),
                1 if state.get("signed_via_free_agency") else 0,
                state.get("signed_date") or "1900-01-01",
                1 if state.get("acquired_via_trade") else 0,
                state.get("acquired_date") or "1900-01-01",
                trade_return_bans_json,
            ),
        )

    def _row_to_pick(self, row) -> Dict[str, Any]:
        protection = None
        protection_raw = row["protection_json"]
        if protection_raw:
            protection = json.loads(protection_raw)
        return {
            "pick_id": row["pick_id"],
            "year": row["year"],
            "round": row["round"],
            "original_team": row["original_team"],
            "owner_team": row["owner_team"],
            "protection": protection,
        }

    def _row_to_swap(self, row) -> Dict[str, Any]:
        return {
            "swap_id": row["swap_id"],
            "year": row["year"],
            "pick_id_a": row["pick_id_a"],
            "pick_id_b": row["pick_id_b"],
            "owner_team": row["owner_team"],
            "active": bool(row["active"]),
        }

    # ------------------------
    # Writes (Roster operations)
    # ------------------------

    def trade_player(self, player_id: str, to_team_id: str) -> None:
        """Move player to another team."""
        pid = normalize_player_id(player_id, strict=False)
        to_tid = normalize_team_id(to_team_id, strict=True)
        now = _utc_now_iso()

        with self.transaction() as cur:
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

    def release_to_free_agency(self, player_id: str) -> None:
        """Set team_id to FA."""
        self.trade_player(player_id, "FA")

    def set_salary(self, player_id: str, salary_amount: int) -> None:
        pid = normalize_player_id(player_id, strict=False)
        now = _utc_now_iso()
        with self.transaction() as cur:
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
