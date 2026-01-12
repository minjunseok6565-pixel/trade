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
        make_player_id_seq,
        normalize_pick_id,
        parse_pick_id,
        compute_swap_pair_key,
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
        """Create tables if they don't exist and run schema migrations."""
        from db_migrations import (
            migrate_db_schema,
            _ensure_meta_schema_version,
            LATEST_DB_SCHEMA_VERSION,
            get_user_version,
        )

        with self.transaction():
            migrate_db_schema(self._conn)
            _ensure_meta_schema_version(self._conn, get_user_version(self._conn))

        current_version = get_user_version(self._conn)
        if current_version != LATEST_DB_SCHEMA_VERSION:
            raise ValueError(
                f"DB user_version {current_version} != expected {LATEST_DB_SCHEMA_VERSION}"
            )

    def canonicalize_player_id(self, raw_player_id: Any, *, allow_legacy_ids: bool) -> str:
        """Normalize player_id and optionally convert legacy numeric IDs to canonical."""
        try:
            return str(normalize_player_id(raw_player_id, strict=True))
        except ValueError as exc:
            if allow_legacy_ids:
                text = str(raw_player_id).strip()
                if text.isdigit():
                    return str(make_player_id_seq(int(text)))
            raise ValueError(f"invalid player_id '{raw_player_id}'") from exc

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

        allow_legacy_ids = not strict_ids
        canonical_player_ids = [
            self.canonicalize_player_id(pid, allow_legacy_ids=allow_legacy_ids)
            for pid in df[ROSTER_COL_PLAYER_ID].tolist()
        ]
        # Validate uniqueness of player_id inside this file (canonicalized)
        assert_unique_ids(canonical_player_ids, what="player_id (in Excel)")

        players: List[PlayerRow] = []
        roster: List[RosterRow] = []
        team_ids: set[str] = set()

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

            pid = self.canonicalize_player_id(raw_pid, allow_legacy_ids=allow_legacy_ids)
            tid = normalize_team_id(raw_tid, strict=True)
            team_ids.add(str(tid))

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

            for tid in sorted(team_ids):
                cur.execute(
                    """
                    INSERT OR IGNORE INTO teams(team_id, name, attrs_json, created_at, updated_at)
                    VALUES (?, NULL, '{}', ?, ?);
                    """,
                    (tid, now, now),
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
        rows = self._conn.execute(
            "SELECT team_id FROM teams ORDER BY team_id;"
        ).fetchall()
        return [str(r["team_id"]) for r in rows]

    def upsert_team(
        self,
        team_id: str,
        *,
        name: Optional[str] = None,
        attrs: Optional[Dict[str, Any]] = None,
    ) -> None:
        tid = normalize_team_id(team_id, strict=True)
        now = _utc_now_iso()
        attrs_json = json.dumps(attrs or {}, ensure_ascii=False, separators=(",", ":"))
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO teams(team_id, name, attrs_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    name=excluded.name,
                    attrs_json=excluded.attrs_json,
                    updated_at=excluded.updated_at;
                """,
                (str(tid), name, attrs_json, now, now),
            )

    def ensure_team_exists(self, team_id: str) -> None:
        tid = normalize_team_id(team_id, strict=True)
        now = _utc_now_iso()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO teams(team_id, name, attrs_json, created_at, updated_at)
                VALUES (?, NULL, '{}', ?, ?);
                """,
                (str(tid), now, now),
            )

    def backfill_teams_from_roster(self) -> None:
        now = _utc_now_iso()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO teams(team_id, name, attrs_json, created_at, updated_at)
                SELECT DISTINCT team_id, NULL, '{}', ?, ?
                FROM roster
                WHERE team_id IS NOT NULL AND TRIM(team_id) != '';
                """,
                (now, now),
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO teams(team_id, name, attrs_json, created_at, updated_at)
                VALUES ('FA', 'Free Agent', '{}', ?, ?);
                """,
                (now, now),
            )

    # ------------------------
    # Draft picks / swaps / fixed assets
    # ------------------------

    def _pick_row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["protection"] = (
            json.loads(data["protection_json"]) if data.get("protection_json") else None
        )
        return data

    def _normalize_pick_protection(self, protection: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(protection, dict):
            raise ValueError("protection must be a dict")
        protection_type = protection.get("type", protection.get("rule"))
        if not isinstance(protection_type, str):
            raise ValueError("protection type is required")
        protection_type = protection_type.strip().upper()
        if protection_type != "TOP_N":
            raise ValueError("unsupported protection type")
        raw_n = protection.get("n")
        try:
            n_value = int(raw_n)
        except (TypeError, ValueError):
            raise ValueError("protection n must be an integer")
        if n_value < 1 or n_value > 30:
            raise ValueError("protection n out of range")
        normalized = {"type": protection_type, "n": n_value}
        compensation = protection.get("compensation")
        if compensation is not None:
            if not isinstance(compensation, dict):
                raise ValueError("protection compensation must be an object")
            value = compensation.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("protection compensation value must be numeric")
            label = compensation.get("label")
            if not isinstance(label, str) or not label.strip():
                label = "Protected pick compensation"
            normalized["compensation"] = {"label": str(label), "value": value}
        return normalized

    def ensure_draft_picks(
        self,
        draft_year: int,
        *,
        years_ahead: int = 7,
        team_ids: Optional[List[str]] = None,
    ) -> int:
        if not isinstance(draft_year, int):
            raise ValueError("draft_year must be an int")
        if years_ahead < 0:
            raise ValueError("years_ahead must be >= 0")

        if team_ids is None:
            team_ids = [tid for tid in self.list_teams() if tid != "FA"]
        normalized_team_ids = [
            str(normalize_team_id(tid, strict=True)) for tid in team_ids if tid != "FA"
        ]

        now = _utc_now_iso()
        created = 0
        with self.transaction() as cur:
            for year in range(draft_year, draft_year + years_ahead + 1):
                for round_value in (1, 2):
                    for team_id in normalized_team_ids:
                        pick_id = f"{year}_R{round_value}_{team_id}"
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO draft_picks(
                                pick_id,
                                year,
                                round,
                                original_team_id,
                                owner_team_id,
                                protection_json,
                                created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?);
                            """,
                            (pick_id, year, round_value, team_id, team_id, now, now),
                        )
                        if cur.rowcount > 0:
                            created += 1
        return created

    def get_pick(self, pick_id: str) -> Dict[str, Any]:
        pid = normalize_pick_id(pick_id, strict=True)
        row = self._conn.execute(
            "SELECT * FROM draft_picks WHERE pick_id=?;",
            (str(pid),),
        ).fetchone()
        if not row:
            raise KeyError(f"pick not found: {pick_id}")
        return self._pick_row_to_dict(row)

    def list_picks_by_owner(
        self,
        team_id: str,
        *,
        year: Optional[int] = None,
        round: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        tid = normalize_team_id(team_id, strict=True)
        clauses = ["owner_team_id=?"]
        params: List[Any] = [str(tid)]
        if year is not None:
            clauses.append("year=?")
            params.append(int(year))
        if round is not None:
            clauses.append("round=?")
            params.append(int(round))
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM draft_picks WHERE {where} ORDER BY year, round, pick_id;",
            tuple(params),
        ).fetchall()
        return [self._pick_row_to_dict(row) for row in rows]

    def transfer_pick(self, pick_id: str, from_team_id: str, to_team_id: str) -> None:
        pid = normalize_pick_id(pick_id, strict=True)
        from_tid = normalize_team_id(from_team_id, strict=True)
        to_tid = normalize_team_id(to_team_id, strict=True)
        now = _utc_now_iso()
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT owner_team_id FROM draft_picks WHERE pick_id=?;",
                (str(pid),),
            ).fetchone()
            if not row:
                raise KeyError(f"pick not found: {pick_id}")
            owner = str(row["owner_team_id"])
            if owner != str(from_tid):
                raise ValueError(
                    f"pick {pick_id} not owned by {from_tid} (current: {owner})"
                )
            cur.execute(
                """
                UPDATE draft_picks
                SET owner_team_id=?, updated_at=?
                WHERE pick_id=?;
                """,
                (str(to_tid), now, str(pid)),
            )

    def set_pick_protection(self, pick_id: str, protection: Optional[Dict[str, Any]]) -> None:
        pid = normalize_pick_id(pick_id, strict=True)
        now = _utc_now_iso()
        if protection is None:
            payload = None
        else:
            payload = self._normalize_pick_protection(protection)
        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT 1 FROM draft_picks WHERE pick_id=?;",
                (str(pid),),
            ).fetchone()
            if not row:
                raise KeyError(f"pick not found: {pick_id}")
            cur.execute(
                """
                UPDATE draft_picks
                SET protection_json=?, updated_at=?
                WHERE pick_id=?;
                """,
                (payload_json, now, str(pid)),
            )

    def upsert_swap_right(
        self,
        swap_id: str,
        pick_id_a: str,
        pick_id_b: str,
        owner_team_id: str,
        *,
        active: bool = True,
        created_by_deal_id: Optional[str] = None,
    ) -> None:
        pid_a = normalize_pick_id(pick_id_a, strict=True)
        pid_b = normalize_pick_id(pick_id_b, strict=True)
        owner_tid = normalize_team_id(owner_team_id, strict=True)
        year_a, round_a, _ = parse_pick_id(pid_a)
        year_b, round_b, _ = parse_pick_id(pid_b)
        if year_a != year_b or round_a != round_b:
            raise ValueError("swap picks must share the same year and round")

        pick_pair_key = compute_swap_pair_key(pid_a, pid_b)
        now = _utc_now_iso()
        with self.transaction() as cur:
            for pid in (pid_a, pid_b):
                row = cur.execute(
                    "SELECT 1 FROM draft_picks WHERE pick_id=?;",
                    (str(pid),),
                ).fetchone()
                if not row:
                    raise KeyError(f"pick not found: {pid}")
            cur.execute(
                """
                INSERT INTO swap_rights(
                    swap_id,
                    pick_id_a,
                    pick_id_b,
                    year,
                    round,
                    owner_team_id,
                    active,
                    created_by_deal_id,
                    pick_pair_key,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(swap_id) DO UPDATE SET
                    pick_id_a=excluded.pick_id_a,
                    pick_id_b=excluded.pick_id_b,
                    year=excluded.year,
                    round=excluded.round,
                    owner_team_id=excluded.owner_team_id,
                    active=excluded.active,
                    created_by_deal_id=excluded.created_by_deal_id,
                    pick_pair_key=excluded.pick_pair_key,
                    updated_at=excluded.updated_at;
                """,
                (
                    str(swap_id),
                    str(pid_a),
                    str(pid_b),
                    year_a,
                    round_a,
                    str(owner_tid),
                    1 if active else 0,
                    created_by_deal_id,
                    pick_pair_key,
                    now,
                    now,
                ),
            )

    def get_swap_right(self, swap_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM swap_rights WHERE swap_id=?;",
            (str(swap_id),),
        ).fetchone()
        if not row:
            raise KeyError(f"swap right not found: {swap_id}")
        return dict(row)

    def transfer_swap_right(self, swap_id: str, from_team_id: str, to_team_id: str) -> None:
        from_tid = normalize_team_id(from_team_id, strict=True)
        to_tid = normalize_team_id(to_team_id, strict=True)
        now = _utc_now_iso()
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT owner_team_id FROM swap_rights WHERE swap_id=?;",
                (str(swap_id),),
            ).fetchone()
            if not row:
                raise KeyError(f"swap right not found: {swap_id}")
            owner = str(row["owner_team_id"])
            if owner != str(from_tid):
                raise ValueError(
                    f"swap right {swap_id} not owned by {from_tid} (current: {owner})"
                )
            cur.execute(
                """
                UPDATE swap_rights
                SET owner_team_id=?, updated_at=?
                WHERE swap_id=?;
                """,
                (str(to_tid), now, str(swap_id)),
            )

    def deactivate_swap_right(self, swap_id: str) -> None:
        now = _utc_now_iso()
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT 1 FROM swap_rights WHERE swap_id=?;",
                (str(swap_id),),
            ).fetchone()
            if not row:
                raise KeyError(f"swap right not found: {swap_id}")
            cur.execute(
                """
                UPDATE swap_rights
                SET active=0, updated_at=?
                WHERE swap_id=?;
                """,
                (now, str(swap_id)),
            )

    def create_fixed_asset(
        self,
        asset_id: str,
        *,
        label: str,
        value: float,
        owner_team_id: str,
        source_pick_id: Optional[str] = None,
        draft_year: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        owner_tid = normalize_team_id(owner_team_id, strict=True)
        source_pid = (
            normalize_pick_id(source_pick_id, strict=True) if source_pick_id else None
        )
        meta_json = json.dumps(meta or {}, ensure_ascii=False, separators=(",", ":"))
        now = _utc_now_iso()
        with self.transaction() as cur:
            if source_pid:
                row = cur.execute(
                    "SELECT 1 FROM draft_picks WHERE pick_id=?;",
                    (str(source_pid),),
                ).fetchone()
                if not row:
                    raise KeyError(f"pick not found: {source_pid}")
            cur.execute(
                """
                INSERT INTO fixed_assets(
                    asset_id,
                    label,
                    value,
                    owner_team_id,
                    source_pick_id,
                    draft_year,
                    meta_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    str(asset_id),
                    str(label),
                    float(value),
                    str(owner_tid),
                    str(source_pid) if source_pid else None,
                    int(draft_year) if draft_year is not None else None,
                    meta_json,
                    now,
                    now,
                ),
            )

    def get_fixed_asset(self, asset_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM fixed_assets WHERE asset_id=?;",
            (str(asset_id),),
        ).fetchone()
        if not row:
            raise KeyError(f"fixed asset not found: {asset_id}")
        data = dict(row)
        data["meta"] = json.loads(data["meta_json"]) if data.get("meta_json") else {}
        return data

    def transfer_fixed_asset(self, asset_id: str, from_team_id: str, to_team_id: str) -> None:
        from_tid = normalize_team_id(from_team_id, strict=True)
        to_tid = normalize_team_id(to_team_id, strict=True)
        now = _utc_now_iso()
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT owner_team_id FROM fixed_assets WHERE asset_id=?;",
                (str(asset_id),),
            ).fetchone()
            if not row:
                raise KeyError(f"fixed asset not found: {asset_id}")
            owner = str(row["owner_team_id"])
            if owner != str(from_tid):
                raise ValueError(
                    f"fixed asset {asset_id} not owned by {from_tid} (current: {owner})"
                )
            cur.execute(
                """
                UPDATE fixed_assets
                SET owner_team_id=?, updated_at=?
                WHERE asset_id=?;
                """,
                (str(to_tid), now, str(asset_id)),
            )

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
        from db_migrations import LATEST_DB_SCHEMA_VERSION, get_user_version

        current_version = get_user_version(self._conn)
        if current_version != LATEST_DB_SCHEMA_VERSION:
            raise ValueError(
                f"DB user_version {current_version} != expected {LATEST_DB_SCHEMA_VERSION}"
            )

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
        else:
            rows = self._conn.execute("SELECT player_id FROM players;").fetchall()
            for r in rows:
                if not str(r["player_id"]).strip():
                    raise ValueError("players table has empty player_id")

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

        empty_roster = self._conn.execute(
            "SELECT player_id FROM roster WHERE player_id IS NULL OR TRIM(player_id) = '';"
        ).fetchall()
        if empty_roster:
            raise ValueError("roster has empty player_id values")

        # team_id normalization check
        rows = self._conn.execute("SELECT DISTINCT team_id FROM roster WHERE status='active';").fetchall()
        for r in rows:
            normalize_team_id(r["team_id"], strict=True)

        fa_row = self._conn.execute(
            "SELECT team_id FROM teams WHERE team_id='FA';"
        ).fetchone()
        if not fa_row:
            raise ValueError("teams table missing 'FA' entry")

        missing_team_links = self._conn.execute(
            """
            SELECT r.team_id
            FROM roster r
            LEFT JOIN teams t ON t.team_id = r.team_id
            WHERE r.team_id IS NOT NULL AND t.team_id IS NULL;
            """
        ).fetchall()
        if missing_team_links:
            raise ValueError(
                f"roster references missing teams: {[row['team_id'] for row in missing_team_links]}"
            )

        missing_pick_owners = self._conn.execute(
            """
            SELECT owner_team_id FROM draft_picks dp
            LEFT JOIN teams t ON t.team_id = dp.owner_team_id
            WHERE t.team_id IS NULL;
            """
        ).fetchall()
        if missing_pick_owners:
            raise ValueError(
                f"draft_picks references missing owner teams: {[row['owner_team_id'] for row in missing_pick_owners]}"
            )

        missing_pick_originals = self._conn.execute(
            """
            SELECT original_team_id FROM draft_picks dp
            LEFT JOIN teams t ON t.team_id = dp.original_team_id
            WHERE t.team_id IS NULL;
            """
        ).fetchall()
        if missing_pick_originals:
            raise ValueError(
                "draft_picks references missing original teams: "
                f"{[row['original_team_id'] for row in missing_pick_originals]}"
            )

        missing_swap_picks = self._conn.execute(
            """
            SELECT sr.swap_id
            FROM swap_rights sr
            LEFT JOIN draft_picks pa ON pa.pick_id = sr.pick_id_a
            LEFT JOIN draft_picks pb ON pb.pick_id = sr.pick_id_b
            WHERE pa.pick_id IS NULL OR pb.pick_id IS NULL;
            """
        ).fetchall()
        if missing_swap_picks:
            raise ValueError(
                f"swap_rights references missing picks: {[row['swap_id'] for row in missing_swap_picks]}"
            )

        missing_swap_owners = self._conn.execute(
            """
            SELECT sr.owner_team_id
            FROM swap_rights sr
            LEFT JOIN teams t ON t.team_id = sr.owner_team_id
            WHERE t.team_id IS NULL;
            """
        ).fetchall()
        if missing_swap_owners:
            raise ValueError(
                f"swap_rights references missing owner teams: {[row['owner_team_id'] for row in missing_swap_owners]}"
            )

        mismatched_swaps = self._conn.execute(
            """
            SELECT sr.swap_id
            FROM swap_rights sr
            JOIN draft_picks pa ON pa.pick_id = sr.pick_id_a
            JOIN draft_picks pb ON pb.pick_id = sr.pick_id_b
            WHERE pa.year != pb.year
               OR pa.round != pb.round
               OR pa.year != sr.year
               OR pa.round != sr.round;
            """
        ).fetchall()
        if mismatched_swaps:
            raise ValueError(
                f"swap_rights has mismatched pick year/round: {[row['swap_id'] for row in mismatched_swaps]}"
            )

        duplicate_swap_pairs = self._conn.execute(
            """
            SELECT pick_pair_key
            FROM swap_rights
            GROUP BY pick_pair_key
            HAVING COUNT(*) > 1;
            """
        ).fetchall()
        if duplicate_swap_pairs:
            raise ValueError(
                "swap_rights has duplicate pick_pair_key values: "
                f"{[row['pick_pair_key'] for row in duplicate_swap_pairs]}"
            )

        missing_fixed_owner = self._conn.execute(
            """
            SELECT fa.asset_id
            FROM fixed_assets fa
            LEFT JOIN teams t ON t.team_id = fa.owner_team_id
            WHERE t.team_id IS NULL;
            """
        ).fetchall()
        if missing_fixed_owner:
            raise ValueError(
                f"fixed_assets references missing owner teams: {[row['asset_id'] for row in missing_fixed_owner]}"
            )

        missing_fixed_pick = self._conn.execute(
            """
            SELECT fa.asset_id
            FROM fixed_assets fa
            LEFT JOIN draft_picks dp ON dp.pick_id = fa.source_pick_id
            WHERE fa.source_pick_id IS NOT NULL AND dp.pick_id IS NULL;
            """
        ).fetchall()
        if missing_fixed_pick:
            raise ValueError(
                f"fixed_assets references missing source picks: {[row['asset_id'] for row in missing_fixed_pick]}"
            )

        # No duplicate active roster entries (PK ensures), but check status sanity
        rows = self._conn.execute("SELECT COUNT(*) AS c FROM roster WHERE status='active';").fetchone()
        if rows and rows["c"] <= 0:
            raise ValueError("no active roster entries found")

        duplicate_contracts = self._conn.execute(
            """
            SELECT player_id, COUNT(*) AS c
            FROM contracts
            WHERE is_active=1
            GROUP BY player_id
            HAVING COUNT(*) > 1;
            """
        ).fetchall()
        if duplicate_contracts:
            offenders = [row["player_id"] for row in duplicate_contracts]
            raise ValueError(
                f"players with multiple active contracts: {offenders}"
            )

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
