from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}


def _warn_limited(code: str, msg: str, *, limit: int = 5) -> None:
    """Log warning with traceback, but cap repeats per code."""
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1


from derived_formulas import compute_derived
from state import (
    export_full_state_snapshot,
    export_workflow_state,
    get_db_path,
    get_league_context_snapshot,
    players_get,
    players_set,
    teams_get,
    teams_set,
)

# Division/Conference mapping can stay in config (static).
# We intentionally do NOT import ROSTER_DF anymore.
from config import ALL_TEAM_IDS, TEAM_TO_CONF_DIV

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
        # DB schema is guaranteed during server startup (state.startup_init_state()). repo
        yield repo

def _list_active_team_ids() -> List[str]:
    """Return active team ids from DB if possible.

    Notes:
    - If league.db_path is not configured, get_db_path() raises ValueError and this function will propagate.
    - If DB access fails for other reasons (e.g. sqlite error), this falls back to ALL_TEAM_IDS.
    """
    try:
        with _repo_ctx() as repo:
            teams = [str(t).upper() for t in repo.list_teams() if str(t).upper() != "FA"]
            if teams:
                return teams
    except (ImportError, sqlite3.Error, OSError, TypeError) as exc:
        _warn_limited(
            "LIST_TEAMS_FAILED_FALLBACK_ALL",
            f"exc_type={type(exc).__name__}",
            limit=3,
        )
        pass
    return list(ALL_TEAM_IDS)


def _has_free_agents_team() -> bool:
    try:
        with _repo_ctx() as repo:
            return "FA" in {str(t).upper() for t in repo.list_teams()}
    except (ImportError, sqlite3.Error, OSError, TypeError) as exc:
        _warn_limited(
            "HAS_FA_TEAM_CHECK_FAILED",
            f"exc_type={type(exc).__name__}",
            limit=3,
        )
        return False


def _parse_potential(pot_raw: Any) -> float:
    pot_map = {
        "A+": 1.0, "A": 0.95, "A-": 0.9,
        "B+": 0.85, "B": 0.8, "B-": 0.75,
        "C+": 0.7, "C": 0.65, "C-": 0.6,
        "D+": 0.55, "D": 0.5, "F": 0.4,
    }
    if isinstance(pot_raw, str):
        return float(pot_map.get(pot_raw.strip(), 0.6))
    try:
        return float(pot_raw)
    except (TypeError, ValueError):
        return 0.6


def _init_players_and_teams_if_needed() -> None:
    """Initialize state player/team caches.

    Step 6 invariant:
    - players are keyed by **player_id (string)**.
    - Never depend on a pandas DataFrame index for IDs.
    """
    # If players already exist, backfill missing derived using DB row (if present).
    existing_players = players_get()
    if isinstance(existing_players, dict) and existing_players:
        try:
            with _repo_ctx() as repo:
                updated_players = dict(existing_players)
                for pid, pdata in list(updated_players.items()):
                    if not isinstance(pdata, dict):
                        continue

                    derived = pdata.get("derived")
                    if isinstance(derived, dict) and derived:
                        continue
                    try:
                        row = repo.get_player(str(pid))
                    except (sqlite3.Error, TypeError, ValueError):
                        _warn_limited("DB_GET_PLAYER_FAILED", f"player_id={pid!r}", limit=3)
                        continue
                    attrs = row.get("attrs") or {}
                    try:
                        pdata["derived"] = compute_derived(attrs)
                    except (KeyError, TypeError, ValueError, ZeroDivisionError):
                        _warn_limited("DERIVED_COMPUTE_FAILED", f"player_id={pid!r}", limit=3)
                        pass
                players_set(updated_players)
                return
        except (ImportError, sqlite3.Error, OSError, TypeError) as exc:
            _warn_limited(
                "INIT_PLAYERS_CACHE_REFRESH_FAILED",
                f"exc_type={type(exc).__name__}",
                limit=3,
            )
            return

    # Fresh build from DB
    players: Dict[str, Dict[str, Any]] = {}
    team_ids = _list_active_team_ids()
    roster_team_ids = list(team_ids)
    if _has_free_agents_team():
        roster_team_ids.append("FA")

    with _repo_ctx() as repo:
        for tid in roster_team_ids:
            try:
                roster_rows = repo.get_team_roster(tid)
            except (sqlite3.Error, TypeError, ValueError):
                _warn_limited("DB_GET_TEAM_ROSTER_FAILED", f"team_id={tid!r}", limit=3)
                continue
            for row in roster_rows:
                pid = str(row.get("player_id"))
                attrs = row.get("attrs") or {}

                players[pid] = {
                    "player_id": pid,
                    "name": row.get("name") or attrs.get("Name") or "",
                    "team_id": str(tid).upper(),
                    "pos": row.get("pos") or attrs.get("POS") or attrs.get("Position") or "",
                    "age": int(row.get("age") or 0),
                    "overall": float(row.get("ovr") or 0.0),
                    "salary": float(row.get("salary_amount") or 0.0),
                    "potential": _parse_potential(attrs.get("Potential")),
                    "derived": compute_derived(attrs),
                    "signed_date": "1900-01-01",
                    "signed_via_free_agency": False,
                    "acquired_date": "1900-01-01",
                    "acquired_via_trade": False,
                }

    players_set(players)

    teams_meta: Dict[str, Dict[str, Any]] = {}
    for tid in team_ids:
        info = TEAM_TO_CONF_DIV.get(tid, {})
        teams_meta[tid] = {
            "team_id": tid,
            "conference": info.get("conference"),
            "division": info.get("division"),
            "tendency": "neutral",
            "window": "now",
            "market": "mid",
            "patience": 0.5,
        }
    teams_set(teams_meta)


def _compute_team_payroll(team_id: str) -> float:
    """Compute payroll from DB roster (NOT from Excel)."""
    total = 0.0
    with _repo_ctx() as repo:
        roster = repo.get_team_roster(team_id)
        for r in roster:
            try:
                total += float(r.get("salary_amount") or 0.0)
            except (TypeError, ValueError):
                _warn_limited(
                    "PAYROLL_SALARY_COERCE_FAILED",
                    f"team_id={team_id!r} raw={r.get('salary_amount')!r}",
                    limit=3,
                )
                continue
    return float(total)


def _compute_cap_space(team_id: str) -> float:
    payroll = _compute_team_payroll(team_id)
    # Assumes cap model (salary_cap/aprons) is already populated during server startup/hydration.
    league_context = get_league_context_snapshot()
    trade_rules = league_context.get("trade_rules", {})
    try:
        salary_cap = float(trade_rules.get("salary_cap") or 0.0)
    except (TypeError, ValueError):
        _warn_limited("SALARY_CAP_COERCE_FAILED", f"raw={trade_rules.get('salary_cap')!r}", limit=3)
        salary_cap = 0.0
    return salary_cap - payroll


def _compute_team_records() -> Dict[str, Dict[str, Any]]:
    """Compute W/L and points from master_schedule."""
    league = export_full_state_snapshot().get("league", {})
    master_schedule = league.get("master_schedule", {})
    games = master_schedule.get("games") or []

    if not games:
        raise RuntimeError(
            "Master schedule is not initialized. Expected state.startup_init_state() to run before calling team_utils._compute_team_records()."
        )
    
    team_ids = _list_active_team_ids()
    records: Dict[str, Dict[str, Any]] = {
        tid: {"wins": 0, "losses": 0, "pf": 0, "pa": 0}
        for tid in team_ids
    }

    for g in games:
        if g.get("status") != "final":
            continue
        home_id = str(g.get("home_team_id") or "")
        away_id = str(g.get("away_team_id") or "")
        home_score = g.get("home_score")
        away_score = g.get("away_score")
        if home_id not in records or away_id not in records:
            continue
        if home_score is None or away_score is None:
            continue

        records[home_id]["pf"] += int(home_score)
        records[home_id]["pa"] += int(away_score)
        records[away_id]["pf"] += int(away_score)
        records[away_id]["pa"] += int(home_score)

        if home_score > away_score:
            records[home_id]["wins"] += 1
            records[away_id]["losses"] += 1
        elif away_score > home_score:
            records[away_id]["wins"] += 1
            records[home_id]["losses"] += 1

    return records


def get_conference_standings() -> Dict[str, List[Dict[str, Any]]]:
    """Return standings grouped by conference."""
    records = _compute_team_records()

    standings = {"east": [], "west": []}

    for tid, rec in records.items():
        info = TEAM_TO_CONF_DIV.get(tid, {})
        conf = info.get("conference")
        if not conf:
            continue

        wins = rec.get("wins", 0)
        losses = rec.get("losses", 0)
        games_played = wins + losses
        win_pct = wins / games_played if games_played else 0.0
        pf = rec.get("pf", 0)
        pa = rec.get("pa", 0)
        point_diff = pf - pa

        entry = {
            "team_id": tid,
            "conference": conf,
            "division": info.get("division"),
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "games_played": games_played,
            "point_diff": point_diff,
        }

        if str(conf).lower() == "east":
            standings["east"].append(entry)
        else:
            standings["west"].append(entry)

    def sort_and_gb(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows_sorted = sorted(
            rows,
            key=lambda r: (r.get("win_pct", 0), r.get("point_diff", 0)),
            reverse=True,
        )
        if not rows_sorted:
            return rows_sorted

        leader = rows_sorted[0]
        leader_w, leader_l = leader.get("wins", 0), leader.get("losses", 0)
        for r in rows_sorted:
            gb = ((leader_w - r.get("wins", 0)) + (r.get("losses", 0) - leader_l)) / 2
            r["gb"] = gb
        for idx, r in enumerate(rows_sorted, start=1):
            r["rank"] = idx
        return rows_sorted

    standings["east"] = sort_and_gb(standings["east"])
    standings["west"] = sort_and_gb(standings["west"])

    return standings


def get_team_cards() -> List[Dict[str, Any]]:
    """Return team summary cards."""
    _init_players_and_teams_if_needed()
    records = _compute_team_records()
    team_ids = _list_active_team_ids()

    team_cards: List[Dict[str, Any]] = []
    for tid in team_ids:
        meta = teams_get().get(tid, {})
        rec = records.get(tid, {})
        wins = rec.get("wins", 0)
        losses = rec.get("losses", 0)
        gp = wins + losses
        win_pct = wins / gp if gp else 0.0
        card = {
            "team_id": tid,
            "conference": meta.get("conference"),
            "division": meta.get("division"),
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "tendency": meta.get("tendency"),
            "payroll": _compute_team_payroll(tid),
            "cap_space": _compute_cap_space(tid),
        }
        team_cards.append(card)

    return team_cards


def get_team_detail(team_id: str) -> Dict[str, Any]:
    """Return team detail (summary + roster) using DB roster."""
    _init_players_and_teams_if_needed()
    tid = str(team_id).upper()

    team_ids = set(_list_active_team_ids())
    if tid not in team_ids:
        raise ValueError(f"Team '{tid}' not found")

    records = _compute_team_records()
    standings = get_conference_standings()
    rank_map = {r["team_id"]: r for r in standings.get("east", []) + standings.get("west", [])}

    meta = teams_get().get(tid, {})
    rec = records.get(tid, {})
    rank_entry = rank_map.get(tid, {})
    wins = rec.get("wins", 0)
    losses = rec.get("losses", 0)
    gp = wins + losses
    win_pct = wins / gp if gp else 0.0
    pf = rec.get("pf", 0)
    pa = rec.get("pa", 0)
    point_diff = pf - pa

    summary = {
        "team_id": tid,
        "conference": meta.get("conference"),
        "division": meta.get("division"),
        "wins": wins,
        "losses": losses,
        "win_pct": win_pct,
        "point_diff": point_diff,
        "rank": rank_entry.get("rank"),
        "gb": rank_entry.get("gb"),
        "tendency": meta.get("tendency"),
        "payroll": _compute_team_payroll(tid),
        "cap_space": _compute_cap_space(tid),
    }

    season_stats = export_workflow_state().get("player_stats", {}) or {}
    roster: List[Dict[str, Any]] = []
    with _repo_ctx() as repo:
        roster_rows = repo.get_team_roster(tid)
        for row in roster_rows:
            pid = str(row.get("player_id"))
            p_stats = season_stats.get(pid, {}) or {}
            games = int(p_stats.get("games", 0) or 0)
            totals = p_stats.get("totals", {}) or {}
            def per_game_val(key: str) -> float:
                try:
                    return float(totals.get(key, 0.0)) / games if games else 0.0
                except (TypeError, ValueError, ZeroDivisionError):
                    return 0.0

            roster.append(
                {
                    "player_id": pid,
                    "name": row.get("name"),
                    "pos": row.get("pos"),
                    "ovr": float(row.get("ovr") or 0.0),
                    "age": int(row.get("age") or 0),
                    "salary": float(row.get("salary_amount") or 0.0),
                    "pts": per_game_val("PTS"),
                    "ast": per_game_val("AST"),
                    "reb": per_game_val("REB"),
                    "three_pm": per_game_val("3PM"),
                }
            )

    roster_sorted = sorted(roster, key=lambda r: r.get("ovr", 0), reverse=True)

    return {
        "summary": summary,
        "roster": roster_sorted,
    }











