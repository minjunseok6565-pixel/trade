from __future__ import annotations

"""College UI read helpers (DB-backed, read-only).

This module provides user-facing read paths for the college subsystem.
College SSOT lives in SQLite (managed by LeagueRepo + db_schema/college.py).

Design goals
------------
- Read-only: do not mutate DB from UI queries.
- Stable response shapes (avoid UI regressions).
- Strip internal JSON version keys ("__v") from returned payloads.

Notes
-----
- This module does not depend on FastAPI; server routes can call functions here
  and map exceptions (ValueError -> 400/404, etc.).
- We avoid relying on SQLite JSON1 extension; JSON fields are parsed in Python.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from state import get_current_date_as_date, get_db_path, get_league_context_snapshot

logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}


def _warn_limited(code: str, msg: str, *, limit: int = 5) -> None:
    """Log warning with traceback, but cap repeats per code."""
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1


# Optional import pattern (mirrors team_utils.py)
_LEAGUE_REPO_IMPORT_ERROR: Optional[Exception] = None
try:
    from league_repo import LeagueRepo  # type: ignore
except ImportError as e:  # pragma: no cover
    LeagueRepo = None  # type: ignore
    _LEAGUE_REPO_IMPORT_ERROR = e


@contextmanager
def _repo_ctx() -> "LeagueRepo":
    """Open a SQLite LeagueRepo for the duration of the operation."""
    if LeagueRepo is None:
        raise ImportError(f"league_repo.py is required: {_LEAGUE_REPO_IMPORT_ERROR}")

    db_path = get_db_path()
    with LeagueRepo(db_path) as repo:
        # Safe even if already initialized.
        repo.init_db()
        yield repo


def _clamp_int(x: Any, lo: int, hi: int) -> int:
    try:
        v = int(x)
    except Exception:
        return int(lo)
    if v < lo:
        return int(lo)
    if v > hi:
        return int(hi)
    return int(v)


def _safe_json(value: Any, default: Any) -> Any:
    """Best-effort JSON loader.

    Accepts dict/list directly, or JSON in str/bytes.
    Returns `default` on failure.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        s = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)
        s = s.strip()
        if not s:
            return default
        return json.loads(s)
    except Exception:
        return default


def _safe_json_dict(value: Any, default: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    obj = _safe_json(value, default)
    if obj is None:
        return None
    if isinstance(obj, dict):
        return dict(obj)
    return None


def _strip_version(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if isinstance(d, dict):
        d.pop("__v", None)
    return d


def _normalize_college_team_id(x: str) -> str:
    tid = str(x or "").strip().upper()
    if not tid or not tid.startswith("COL_"):
        raise ValueError("invalid college_team_id")
    return tid


def _default_stats_season_year() -> int:
    ctx = get_league_context_snapshot() or {}
    try:
        sy = int(ctx.get("season_year") or 0)
    except Exception:
        sy = 0
    return max(1, sy - 1) if sy else 0


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


# -----------------------------------------------------------------------------
# Public UI read API
# -----------------------------------------------------------------------------


def get_college_meta() -> Dict[str, Any]:
    """UI meta/context snapshot.

    Returns a UI-friendly meta block so the client can decide defaults
    (latest stats season, upcoming draft year, etc.).
    """
    ctx = get_league_context_snapshot() or {}
    try:
        season_year = int(ctx.get("season_year") or 0)
    except Exception:
        season_year = 0

    in_game_date: date = get_current_date_as_date()
    current_date = in_game_date.isoformat()

    default_stats_sy = max(1, season_year - 1) if season_year else 0
    upcoming_draft_year = season_year + 1 if season_year else 0

    with _repo_ctx() as repo:
        row = repo._conn.execute("SELECT COUNT(*) AS n FROM college_teams;").fetchone()
        teams_count = int(_row_get(row, "n", 0) or 0)

        rows = repo._conn.execute(
            "SELECT status, COUNT(*) AS n FROM college_players GROUP BY status ORDER BY status ASC;"
        ).fetchall()
        players_by_status: Dict[str, int] = {}
        players_total = 0
        for r in rows:
            st = str(_row_get(r, "status", "") or "").strip()
            if not st:
                continue
            try:
                n = int(_row_get(r, "n", 0) or 0)
            except Exception:
                n = 0
            players_by_status[st] = n
            players_total += n

        row = repo._conn.execute(
            "SELECT MAX(season_year) AS max_sy FROM college_player_season_stats;"
        ).fetchone()
        latest_stats_sy: Optional[int]
        try:
            v = _row_get(row, "max_sy", None)
            latest_stats_sy = int(v) if v is not None else None
        except Exception:
            latest_stats_sy = None

        strength: Optional[float]
        if upcoming_draft_year:
            row = repo._conn.execute(
                "SELECT strength AS strength FROM draft_class_strength WHERE draft_year=?;",
                (int(upcoming_draft_year),),
            ).fetchone()
            try:
                v = _row_get(row, "strength", None)
                strength = float(v) if v is not None else None
            except Exception:
                strength = None
        else:
            strength = None

    return {
        "season_year": int(season_year),
        "current_date": str(current_date),
        "default_stats_season_year": int(default_stats_sy),
        "upcoming_draft_year": int(upcoming_draft_year),
        "latest_stats_season_year": latest_stats_sy,
        "college": {
            "teams": int(teams_count),
            "players_total": int(players_total),
            "players_by_status": dict(players_by_status),
        },
        "class_strength": {
            "draft_year": int(upcoming_draft_year),
            "strength": strength,
        },
    }


def get_college_team_cards(*, season_year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return college team cards for a season.

    Each card includes team meta + (optional) team season stats for `season_year`
    and a roster summary.
    """
    sy = int(season_year) if season_year is not None else _default_stats_season_year()
    if sy <= 0:
        raise ValueError(f"invalid season_year: {season_year}")

    sql = """
SELECT
  t.college_team_id AS college_team_id,
  t.name            AS name,
  t.conference      AS conference,
  t.meta_json       AS meta_json,

  s.wins            AS wins,
  s.losses          AS losses,
  s.srs             AS srs,
  s.pace            AS pace,
  s.off_ppg         AS off_ppg,
  s.def_ppg         AS def_ppg,

  COALESCE(p.roster_count, 0)   AS roster_count,
  COALESCE(p.declared_count, 0) AS declared_count,
  p.top_ovr                     AS top_ovr

FROM college_teams t
LEFT JOIN college_team_season_stats s
  ON s.college_team_id = t.college_team_id
 AND s.season_year = ?
LEFT JOIN (
  SELECT
    college_team_id,
    COUNT(*) AS roster_count,
    SUM(CASE WHEN status='DECLARED' THEN 1 ELSE 0 END) AS declared_count,
    MAX(ovr) AS top_ovr
  FROM college_players
  WHERE status IN ('ACTIVE','DECLARED')
  GROUP BY college_team_id
) p
  ON p.college_team_id = t.college_team_id
ORDER BY t.college_team_id ASC;
""".strip()

    out: List[Dict[str, Any]] = []
    with _repo_ctx() as repo:
        rows = repo._conn.execute(sql, (int(sy),)).fetchall()
        for r in rows:
            out.append(
                {
                    "college_team_id": str(_row_get(r, "college_team_id", "") or ""),
                    "name": str(_row_get(r, "name", "") or ""),
                    "conference": str(_row_get(r, "conference", "") or ""),
                    "season_year": int(sy),
                    "wins": (int(_row_get(r, "wins")) if _row_get(r, "wins", None) is not None else None),
                    "losses": (
                        int(_row_get(r, "losses")) if _row_get(r, "losses", None) is not None else None
                    ),
                    "srs": (float(_row_get(r, "srs")) if _row_get(r, "srs", None) is not None else None),
                    "pace": (float(_row_get(r, "pace")) if _row_get(r, "pace", None) is not None else None),
                    "off_ppg": (
                        float(_row_get(r, "off_ppg")) if _row_get(r, "off_ppg", None) is not None else None
                    ),
                    "def_ppg": (
                        float(_row_get(r, "def_ppg")) if _row_get(r, "def_ppg", None) is not None else None
                    ),
                    "roster_count": int(_row_get(r, "roster_count", 0) or 0),
                    "declared_count": int(_row_get(r, "declared_count", 0) or 0),
                    "top_ovr": (int(_row_get(r, "top_ovr")) if _row_get(r, "top_ovr", None) is not None else None),
                }
            )

    return out


def get_college_team_detail(
    college_team_id: str,
    *,
    season_year: Optional[int] = None,
    include_attrs: bool = False,
) -> Dict[str, Any]:
    """Team detail: team meta + season stats + roster (with optional season stats)."""
    tid = _normalize_college_team_id(college_team_id)
    sy = int(season_year) if season_year is not None else _default_stats_season_year()
    if sy <= 0:
        raise ValueError(f"invalid season_year: {season_year}")

    with _repo_ctx() as repo:
        trow = repo._conn.execute(
            "SELECT college_team_id, name, conference, meta_json FROM college_teams WHERE college_team_id=?;",
            (tid,),
        ).fetchone()
        if not trow:
            raise ValueError("college team not found")

        team_meta = _strip_version(_safe_json_dict(_row_get(trow, "meta_json", "{}"), {})) or {}

        srow = repo._conn.execute(
            """
            SELECT season_year, wins, losses, srs, pace, off_ppg, def_ppg, meta_json
            FROM college_team_season_stats
            WHERE college_team_id=? AND season_year=?;
            """.strip(),
            (tid, int(sy)),
        ).fetchone()

        season_payload: Optional[Dict[str, Any]]
        if srow:
            season_meta = _strip_version(_safe_json_dict(_row_get(srow, "meta_json", "{}"), {})) or {}
            season_payload = {
                "season_year": int(_row_get(srow, "season_year", sy) or sy),
                "wins": int(_row_get(srow, "wins", 0) or 0),
                "losses": int(_row_get(srow, "losses", 0) or 0),
                "srs": float(_row_get(srow, "srs", 0.0) or 0.0),
                "pace": float(_row_get(srow, "pace", 0.0) or 0.0),
                "off_ppg": float(_row_get(srow, "off_ppg", 0.0) or 0.0),
                "def_ppg": float(_row_get(srow, "def_ppg", 0.0) or 0.0),
                "meta": dict(season_meta),
            }
        else:
            season_payload = None

        roster_sql = """
SELECT
  p.player_id        AS player_id,
  p.name             AS name,
  p.pos              AS pos,
  p.age              AS age,
  p.height_in        AS height_in,
  p.weight_lb        AS weight_lb,
  p.ovr              AS ovr,
  p.attrs_json       AS attrs_json,

  p.college_team_id  AS college_team_id,
  p.class_year       AS class_year,
  p.entry_season_year AS entry_season_year,
  p.status           AS status,

  ps.stats_json      AS stats_json

FROM college_players p
LEFT JOIN college_player_season_stats ps
  ON ps.player_id = p.player_id
 AND ps.season_year = ?
WHERE p.college_team_id = ?
  AND p.status IN ('ACTIVE','DECLARED')
ORDER BY p.ovr DESC, p.player_id ASC;
""".strip()

        roster_rows = repo._conn.execute(roster_sql, (int(sy), tid)).fetchall()

    roster: List[Dict[str, Any]] = []
    for r in roster_rows:
        attrs = _safe_json_dict(_row_get(r, "attrs_json", "{}"), {}) or {}
        potential = attrs.get("potential")
        try:
            potential_i = int(potential) if potential is not None else None
        except Exception:
            potential_i = None

        stats = _safe_json_dict(_row_get(r, "stats_json", None), None)
        if isinstance(stats, dict):
            _strip_version(stats)
            # Ensure season_year exists for UI convenience.
            stats.setdefault("season_year", int(sy))

        entry: Dict[str, Any] = {
            "player_id": str(_row_get(r, "player_id", "") or ""),
            "name": str(_row_get(r, "name", "") or ""),
            "pos": str(_row_get(r, "pos", "") or ""),
            "age": int(_row_get(r, "age", 0) or 0),
            "height_in": int(_row_get(r, "height_in", 0) or 0),
            "weight_lb": int(_row_get(r, "weight_lb", 0) or 0),
            "ovr": int(_row_get(r, "ovr", 0) or 0),
            "potential": potential_i,
            "college_team_id": str(_row_get(r, "college_team_id", "") or ""),
            "class_year": int(_row_get(r, "class_year", 1) or 1),
            "entry_season_year": int(_row_get(r, "entry_season_year", 0) or 0),
            "status": str(_row_get(r, "status", "") or ""),
            "stats": stats,
        }
        if include_attrs:
            entry["attrs"] = dict(_strip_version(attrs) or {})
        roster.append(entry)

    return {
        "stats_season_year": int(sy),
        "team": {
            "college_team_id": str(_row_get(trow, "college_team_id", "") or ""),
            "name": str(_row_get(trow, "name", "") or ""),
            "conference": str(_row_get(trow, "conference", "") or ""),
            "meta": dict(team_meta),
        },
        "season": season_payload,
        "roster": roster,
    }


def list_college_players(
    *,
    season_year: Optional[int] = None,
    status: Optional[str] = None,
    college_team_id: Optional[str] = None,
    draft_year: Optional[int] = None,
    declared_only: bool = False,
    q: Optional[str] = None,
    sort: str = "ovr",  # allowed: ovr, age, class_year, name, player_id
    order: str = "desc",  # allowed: asc, desc
    include_attrs: bool = False,
    include_decision: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    """List college players with season stats (for UI browsing)."""
    sy = int(season_year) if season_year is not None else _default_stats_season_year()
    if sy <= 0:
        raise ValueError(f"invalid season_year: {season_year}")

    dy: Optional[int]
    if draft_year is None:
        dy = None
    else:
        dy = int(draft_year)
        if dy <= 0:
            raise ValueError(f"invalid draft_year: {draft_year}")

    if declared_only and dy is None:
        raise ValueError("declared_only=true requires draft_year")

    lim = _clamp_int(limit, 1, 500)
    off = _clamp_int(offset, 0, 10_000_000)

    st_norm = str(status).strip().upper() if status is not None else None
    if st_norm == "":
        st_norm = None

    team_norm = _normalize_college_team_id(college_team_id) if college_team_id else None

    q_norm = str(q).strip().lower() if q else None
    if q_norm == "":
        q_norm = None

    sort_norm = str(sort or "ovr").strip().lower()
    order_norm = str(order or "desc").strip().lower()

    sort_map = {
        "ovr": "p.ovr",
        "age": "p.age",
        "class_year": "p.class_year",
        "name": "p.name",
        "player_id": "p.player_id",
    }
    if sort_norm not in sort_map:
        raise ValueError(f"invalid sort: {sort}")
    if order_norm not in {"asc", "desc"}:
        raise ValueError(f"invalid order: {order}")

    order_sql = "ASC" if order_norm == "asc" else "DESC"
    sort_col = sort_map[sort_norm]

    where: List[str] = []
    params: Dict[str, Any] = {
        "stats_sy": int(sy),
        "draft_year": dy,
        "limit": int(lim),
        "offset": int(off),
    }

    if st_norm:
        where.append("p.status = :status")
        params["status"] = st_norm
    else:
        where.append("p.status IN ('ACTIVE','DECLARED')")

    if team_norm:
        where.append("p.college_team_id = :team_id")
        params["team_id"] = team_norm

    if q_norm:
        where.append("LOWER(p.name) LIKE :q")
        params["q"] = f"%{q_norm}%"

    if declared_only:
        where.append("e.player_id IS NOT NULL")

    where_sql = " AND ".join(where) if where else "1=1"

    sql_from = """
FROM college_players p
LEFT JOIN college_teams t
  ON t.college_team_id = p.college_team_id
LEFT JOIN college_player_season_stats ps
  ON ps.player_id = p.player_id
 AND ps.season_year = :stats_sy
LEFT JOIN college_draft_entries e
  ON e.player_id = p.player_id
 AND e.draft_year = :draft_year
""".strip()

    count_sql = f"SELECT COUNT(*) AS n {sql_from} WHERE {where_sql};"

    data_sql = f"""
SELECT
  p.player_id         AS player_id,
  p.name              AS name,
  p.pos               AS pos,
  p.age               AS age,
  p.height_in         AS height_in,
  p.weight_lb         AS weight_lb,
  p.ovr               AS ovr,
  p.attrs_json        AS attrs_json,

  p.college_team_id   AS college_team_id,
  p.class_year        AS class_year,
  p.entry_season_year AS entry_season_year,
  p.status            AS status,

  t.name              AS college_team_name,
  t.conference        AS conference,

  ps.stats_json       AS stats_json,

  e.declared_at       AS declared_at,
  e.decision_json     AS decision_json
{sql_from}
WHERE {where_sql}
ORDER BY {sort_col} {order_sql}, p.player_id ASC
LIMIT :limit OFFSET :offset;
""".strip()

    with _repo_ctx() as repo:
        total = int(_row_get(repo._conn.execute(count_sql, params).fetchone(), "n", 0) or 0)
        rows = repo._conn.execute(data_sql, params).fetchall()

    players: List[Dict[str, Any]] = []
    for r in rows:
        attrs = _safe_json_dict(_row_get(r, "attrs_json", "{}"), {}) or {}
        potential = attrs.get("potential")
        try:
            potential_i = int(potential) if potential is not None else None
        except Exception:
            potential_i = None

        stats = _safe_json_dict(_row_get(r, "stats_json", None), None)
        if isinstance(stats, dict):
            _strip_version(stats)
            stats.setdefault("season_year", int(sy))

        declared = False
        declared_at: Optional[str] = None
        projected_pick: Optional[int] = None
        decision: Optional[Dict[str, Any]] = None

        if dy is not None:
            declared_at = str(_row_get(r, "declared_at", None)) if _row_get(r, "declared_at", None) is not None else None
            decision_obj = _safe_json_dict(_row_get(r, "decision_json", None), None)
            if isinstance(decision_obj, dict):
                _strip_version(decision_obj)
                declared = True
                # projected_pick convenience
                try:
                    projected_pick = int(decision_obj.get("projected_pick")) if decision_obj.get("projected_pick") is not None else None
                except Exception:
                    projected_pick = None
                if include_decision:
                    decision = decision_obj
            else:
                declared = False

        p: Dict[str, Any] = {
            "player_id": str(_row_get(r, "player_id", "") or ""),
            "name": str(_row_get(r, "name", "") or ""),
            "pos": str(_row_get(r, "pos", "") or ""),
            "age": int(_row_get(r, "age", 0) or 0),
            "height_in": int(_row_get(r, "height_in", 0) or 0),
            "weight_lb": int(_row_get(r, "weight_lb", 0) or 0),
            "ovr": int(_row_get(r, "ovr", 0) or 0),
            "potential": potential_i,
            "college_team_id": str(_row_get(r, "college_team_id", "") or ""),
            "college_team_name": str(_row_get(r, "college_team_name", "") or ""),
            "conference": str(_row_get(r, "conference", "") or ""),
            "class_year": int(_row_get(r, "class_year", 1) or 1),
            "entry_season_year": int(_row_get(r, "entry_season_year", 0) or 0),
            "status": str(_row_get(r, "status", "") or ""),
            "stats": stats,
        }

        if include_attrs:
            p["attrs"] = dict(_strip_version(attrs) or {})

        if dy is not None:
            p["declared"] = bool(declared)
            p["declared_at"] = declared_at
            p["projected_pick"] = projected_pick
            if include_decision:
                p["decision"] = decision

        players.append(p)

    return {
        "stats_season_year": int(sy),
        "draft_year": (int(dy) if dy is not None else None),
        "declared_only": bool(declared_only),
        "total": int(total),
        "offset": int(off),
        "limit": int(lim),
        "players": players,
    }


def get_college_player_detail(
    player_id: str,
    *,
    draft_year: Optional[int] = None,
    include_stats_history: bool = True,
) -> Dict[str, Any]:
    """Single player detail (bio + stats history + optional draft entry)."""
    pid = str(player_id or "").strip()
    if not pid:
        raise ValueError("player_id is required")

    ctx = get_league_context_snapshot() or {}
    try:
        season_year = int(ctx.get("season_year") or 0)
    except Exception:
        season_year = 0

    dy = int(draft_year) if draft_year is not None else (season_year + 1 if season_year else None)
    if dy is not None and dy <= 0:
        raise ValueError(f"invalid draft_year: {draft_year}")

    with _repo_ctx() as repo:
        row = repo._conn.execute(
            """
            SELECT
              p.player_id         AS player_id,
              p.name              AS name,
              p.pos               AS pos,
              p.age               AS age,
              p.height_in         AS height_in,
              p.weight_lb         AS weight_lb,
              p.ovr               AS ovr,
              p.attrs_json        AS attrs_json,

              p.college_team_id   AS college_team_id,
              p.class_year        AS class_year,
              p.entry_season_year AS entry_season_year,
              p.status            AS status,

              t.name              AS college_team_name,
              t.conference        AS conference
            FROM college_players p
            LEFT JOIN college_teams t
              ON t.college_team_id = p.college_team_id
            WHERE p.player_id=?;
            """.strip(),
            (pid,),
        ).fetchone()
        if not row:
            raise ValueError("college player not found")

        attrs = _safe_json_dict(_row_get(row, "attrs_json", "{}"), {}) or {}
        potential = attrs.get("potential")
        try:
            potential_i = int(potential) if potential is not None else None
        except Exception:
            potential_i = None

        stats_history: List[Dict[str, Any]] = []
        if include_stats_history:
            srows = repo._conn.execute(
                """
                SELECT season_year, stats_json
                FROM college_player_season_stats
                WHERE player_id=?
                ORDER BY season_year DESC;
                """.strip(),
                (pid,),
            ).fetchall()
            for sr in srows:
                st = _safe_json_dict(_row_get(sr, "stats_json", None), None)
                if not isinstance(st, dict):
                    continue
                _strip_version(st)
                try:
                    st.setdefault("season_year", int(_row_get(sr, "season_year", 0) or 0))
                except Exception:
                    pass
                stats_history.append(st)

        draft_entry: Optional[Dict[str, Any]] = None
        if dy is not None:
            erow = repo._conn.execute(
                """
                SELECT draft_year, declared_at, decision_json
                FROM college_draft_entries
                WHERE player_id=? AND draft_year=?;
                """.strip(),
                (pid, int(dy)),
            ).fetchone()
            if erow:
                decision = _safe_json_dict(_row_get(erow, "decision_json", "{}"), {}) or {}
                _strip_version(decision)
                draft_entry = {
                    "draft_year": int(_row_get(erow, "draft_year", dy) or dy),
                    "declared_at": str(_row_get(erow, "declared_at", "") or ""),
                    "decision": decision,
                }

    player_payload = {
        "player_id": str(_row_get(row, "player_id", "") or ""),
        "name": str(_row_get(row, "name", "") or ""),
        "pos": str(_row_get(row, "pos", "") or ""),
        "age": int(_row_get(row, "age", 0) or 0),
        "height_in": int(_row_get(row, "height_in", 0) or 0),
        "weight_lb": int(_row_get(row, "weight_lb", 0) or 0),
        "ovr": int(_row_get(row, "ovr", 0) or 0),
        "potential": potential_i,
        "attrs": dict(_strip_version(attrs) or {}),
        "college_team_id": str(_row_get(row, "college_team_id", "") or ""),
        "college_team_name": str(_row_get(row, "college_team_name", "") or ""),
        "conference": str(_row_get(row, "conference", "") or ""),
        "class_year": int(_row_get(row, "class_year", 1) or 1),
        "entry_season_year": int(_row_get(row, "entry_season_year", 0) or 0),
        "status": str(_row_get(row, "status", "") or ""),
    }

    return {
        "draft_year_used": (int(dy) if dy is not None else None),
        "player": player_payload,
        "stats_history": stats_history,
        "draft_entry": draft_entry,
    }


def get_college_draft_pool(
    draft_year: int,
    *,
    season_year: Optional[int] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Return declared draft prospects for a given draft_year.

    Uses `draft.pool.load_pool_from_db()` and post-processes nested JSON.
    """
    dy = int(draft_year)
    if dy <= 0:
        raise ValueError(f"invalid draft_year: {draft_year}")

    sy_used = int(season_year) if season_year is not None else (dy - 1)
    if sy_used <= 0:
        raise ValueError(f"invalid season_year: {season_year}")

    lim = int(limit) if limit is not None else None
    if lim is not None and lim <= 0:
        raise ValueError(f"invalid limit: {limit}")

    # Local import to keep module light and avoid cycles.
    from draft.pool import load_pool_from_db

    pool = load_pool_from_db(
        db_path=get_db_path(),
        draft_year=dy,
        season_year=(int(season_year) if season_year is not None else None),
        limit=lim,
    )

    pool_dict = pool.to_dict()
    prospects = list(pool_dict.get("prospects") or [])

    cleaned: List[Dict[str, Any]] = []
    for p in prospects:
        if not isinstance(p, dict):
            continue
        attrs = p.get("attrs")
        if not isinstance(attrs, dict):
            attrs = {}

        dt = attrs.get("decision_trace")
        if isinstance(dt, dict):
            _strip_version(dt)
        ss = attrs.get("season_stats")
        if isinstance(ss, dict):
            _strip_version(ss)

        # (Optional) strip version at attrs root too.
        _strip_version(attrs)

        p2 = dict(p)
        p2["attrs"] = attrs
        cleaned.append(p2)

    return {
        "draft_year": int(dy),
        "stats_season_year": int(sy_used),
        "count": int(len(cleaned)),
        "prospects": cleaned,
    }
