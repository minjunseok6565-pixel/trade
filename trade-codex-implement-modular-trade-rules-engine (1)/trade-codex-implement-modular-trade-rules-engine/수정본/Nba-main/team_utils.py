from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from state import GAME_STATE, _ensure_league_state, initialize_master_schedule_if_needed

# Division/Conference mapping can stay in config (static).
# We intentionally do NOT import ROSTER_DF anymore.
from config import ALL_TEAM_IDS, TEAM_TO_CONF_DIV

_LEAGUE_REPO_IMPORT_ERROR: Optional[Exception] = None
try:
    from league_repo import LeagueRepo  # type: ignore
except Exception as e:  # pragma: no cover
    LeagueRepo = None  # type: ignore
    _LEAGUE_REPO_IMPORT_ERROR = e


@contextmanager
def _repo_ctx() -> "LeagueRepo":
    """Open a SQLite LeagueRepo for the duration of the operation."""
    if LeagueRepo is None:
        raise ImportError(f"league_repo.py is required: {_LEAGUE_REPO_IMPORT_ERROR}")

    league = GAME_STATE.setdefault("league", {})
    db_path = league.get("db_path") or os.environ.get("LEAGUE_DB_PATH", "league.db")

    with LeagueRepo(str(db_path)) as repo:
        try:
            repo.init_db()
        except Exception:
            # init_db is idempotent; ignore if project uses a different init flow
            pass
        yield repo


def _list_active_team_ids() -> List[str]:
    """Return active team ids from DB if possible, else fall back to ALL_TEAM_IDS."""
    try:
        with _repo_ctx() as repo:
            teams = [str(t).upper() for t in repo.list_teams() if str(t).upper() != "FA"]
            if teams:
                return teams
    except Exception:
        pass
    return list(ALL_TEAM_IDS)


def _compute_team_payroll(team_id: str) -> float:
    """Compute payroll from DB roster (NOT from Excel)."""
    total = 0.0
    with _repo_ctx() as repo:
        roster = repo.get_team_roster(team_id)
        for r in roster:
            try:
                total += float(r.get("salary_amount") or 0.0)
            except Exception:
                continue
    return float(total)


def _compute_cap_space(team_id: str) -> float:
    payroll = _compute_team_payroll(team_id)
    league = _ensure_league_state()
    trade_rules = league.get("trade_rules", {})
    try:
        salary_cap = float(trade_rules.get("salary_cap") or 0.0)
    except Exception:
        salary_cap = 0.0
    return salary_cap - payroll


def _compute_team_records() -> Dict[str, Dict[str, Any]]:
    """Compute W/L and points from master_schedule."""
    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
    master_schedule = league["master_schedule"]
    games = master_schedule.get("games") or []

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
    records = _compute_team_records()
    team_ids = _list_active_team_ids()

    team_cards: List[Dict[str, Any]] = []
    for tid in team_ids:
        info = TEAM_TO_CONF_DIV.get(tid, {})
        rec = records.get(tid, {})
        wins = rec.get("wins", 0)
        losses = rec.get("losses", 0)
        gp = wins + losses
        win_pct = wins / gp if gp else 0.0
        card = {
            "team_id": tid,
            "conference": info.get("conference"),
            "division": info.get("division"),
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "tendency": None,
            "payroll": _compute_team_payroll(tid),
            "cap_space": _compute_cap_space(tid),
        }
        team_cards.append(card)

    return team_cards


def get_team_detail(team_id: str) -> Dict[str, Any]:
    """Return team detail (summary + roster) using DB roster."""
    tid = str(team_id).upper()

    team_ids = set(_list_active_team_ids())
    if tid not in team_ids:
        raise ValueError(f"Team '{tid}' not found")

    records = _compute_team_records()
    standings = get_conference_standings()
    rank_map = {r["team_id"]: r for r in standings.get("east", []) + standings.get("west", [])}

    info = TEAM_TO_CONF_DIV.get(tid, {})
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
        "conference": info.get("conference"),
        "division": info.get("division"),
        "wins": wins,
        "losses": losses,
        "win_pct": win_pct,
        "point_diff": point_diff,
        "rank": rank_entry.get("rank"),
        "gb": rank_entry.get("gb"),
        "tendency": None,
        "payroll": _compute_team_payroll(tid),
        "cap_space": _compute_cap_space(tid),
    }

    season_stats = GAME_STATE.get("player_stats", {}) or {}
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


