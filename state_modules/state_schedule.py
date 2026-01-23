from __future__ import annotations

import os
import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from config import (
    ALL_TEAM_IDS,
    DIVISIONS,
    INITIAL_SEASON_YEAR,
    MAX_GAMES_PER_DAY,
    SEASON_LENGTH_DAYS,
    SEASON_START_DAY,
    SEASON_START_MONTH,
    TEAM_TO_CONF_DIV,
)
from schema import season_id_from_year as _schema_season_id_from_year
from state_cap import _apply_cap_model_for_season
from state_store import _ALLOWED_SCHEDULE_STATUSES


def _season_id_from_year(season_year: int) -> str:
    return str(_schema_season_id_from_year(int(season_year)))


def validate_master_schedule_entry(entry: Dict[str, Any], *, path: str = "master_schedule.entry") -> None:
    """
    master_schedule.games[*]에서 실제로 "사용되는 필드만" 최소 계약으로 고정한다.

    Required:
      - game_id: str (non-empty)
      - home_team_id: str (non-empty)
      - away_team_id: str (non-empty)
      - status: str (allowed set)

    Optional (if present, must be correct type):
      - date: str (ISO-like recommended)
      - season_id: str
      - phase: str
      - home_score/away_score: int|None
      - home_tactics/away_tactics/tactics: dict|None  (프로젝트별로 사용하는 키가 달라도 안전하게 수용)
    """
    if not isinstance(entry, dict):
        raise ValueError(f"MasterScheduleEntry invalid: '{path}' must be a dict")

    for k in ("game_id", "home_team_id", "away_team_id", "status"):
        if k not in entry:
            raise ValueError(f"MasterScheduleEntry invalid: missing {path}.{k}")

    game_id = entry.get("game_id")
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError(f"MasterScheduleEntry invalid: {path}.game_id must be a non-empty string")

    for k in ("home_team_id", "away_team_id"):
        v = entry.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{k} must be a non-empty string")

    status = entry.get("status")
    if not isinstance(status, str) or status not in _ALLOWED_SCHEDULE_STATUSES:
        raise ValueError(
            f"MasterScheduleEntry invalid: {path}.status must be one of {sorted(_ALLOWED_SCHEDULE_STATUSES)}"
        )

    # Optional: tactics payload(s)
    for tk in ("tactics", "home_tactics", "away_tactics"):
        if tk in entry and entry[tk] is not None and not isinstance(entry[tk], dict):
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{tk} must be a dict if present")

    # Optional: date (string)
    if "date" in entry and entry["date"] is not None and not isinstance(entry["date"], str):
        raise ValueError(f"MasterScheduleEntry invalid: {path}.date must be a string if present")

    # Optional: scores
    for sk in ("home_score", "away_score"):
        if sk in entry and entry[sk] is not None and not isinstance(entry[sk], int):
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{sk} must be int or None if present")


def ensure_master_schedule_indices(league: Dict[str, Any]) -> None:
    master_schedule = league.get("master_schedule") or {}
    games = master_schedule.get("games") or []
    # Contract check: master_schedule entries must satisfy the minimal schema.
    for i, g in enumerate(games):
        validate_master_schedule_entry(g, path=f"master_schedule.games[{i}]")
    by_id = master_schedule.get("by_id")
    if not isinstance(by_id, dict) or len(by_id) != len(games):
        master_schedule["by_id"] = {
            g.get("game_id"): g for g in games if g.get("game_id")
        }


def build_master_schedule(league: Dict[str, Any], season_year: int) -> None:
    """30개 팀 전체에 대한 마스터 스케줄(정규시즌)을 생성한다."""
    from league_repo import LeagueRepo

    previous_season_year = league.get("season_year")
    league["season_year"] = season_year
    league["draft_year"] = season_year + 1
    _apply_cap_model_for_season(league, season_year)

    trade_rules = league.get("trade_rules") or {}
    try:
        max_pick_years_ahead = int(trade_rules.get("max_pick_years_ahead") or 7)
    except (TypeError, ValueError):
        max_pick_years_ahead = 7
    try:
        stepien_lookahead = int(trade_rules.get("stepien_lookahead") or 7)
    except (TypeError, ValueError):
        stepien_lookahead = 7

    years_ahead = max(max_pick_years_ahead, stepien_lookahead + 1)
    db_path = league.get("db_path") or os.environ.get("LEAGUE_DB_PATH") or "league.db"
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_draft_picks_seeded(league["draft_year"], list(ALL_TEAM_IDS), years_ahead=years_ahead)

    season_start = date(season_year, SEASON_START_MONTH, SEASON_START_DAY)
    teams = list(ALL_TEAM_IDS)
    season_id = _season_id_from_year(season_year)
    phase = "regular"

    team_info: Dict[str, Dict[str, Optional[str]]] = {}
    for tid in teams:
        info = TEAM_TO_CONF_DIV.get(tid, {"conference": None, "division": None})
        team_info[tid] = {
            "conference": info.get("conference"),
            "division": info.get("division"),
        }

    def _four_game_pairs_for_conf(conf_name: str) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        conf_divs = DIVISIONS.get(conf_name, {})
        div_list = list(conf_divs.values())
        if len(div_list) < 2:
            return pairs

        for i in range(len(div_list)):
            for j in range(i + 1, len(div_list)):
                a_div = div_list[i]
                b_div = div_list[j]
                if not a_div or not b_div:
                    continue
                for idx, a_team in enumerate(a_div):
                    for delta in range(3):
                        b_team = b_div[(idx + delta) % len(b_div)]
                        pair = tuple(sorted((a_team, b_team)))
                        pairs.add(pair)
        return pairs

    four_game_pairs_east = _four_game_pairs_for_conf("East")
    four_game_pairs_west = _four_game_pairs_for_conf("West")
    four_game_pairs = four_game_pairs_east | four_game_pairs_west

    pair_games: List[Dict[str, Any]] = []
    home_counts: Dict[str, int] = {tid: 0 for tid in teams}

    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            t1 = teams[i]
            t2 = teams[j]
            info1 = team_info[t1]
            info2 = team_info[t2]

            conf1, div1 = info1["conference"], info1["division"]
            conf2, div2 = info2["conference"], info2["division"]

            if conf1 is None or conf2 is None:
                num_games = 2
            elif conf1 != conf2:
                num_games = 2
            elif div1 == div2:
                num_games = 4
            else:
                pair_key = tuple(sorted((t1, t2)))
                num_games = 4 if pair_key in four_game_pairs else 3

            home_for_t1 = num_games // 2
            home_for_t2 = num_games // 2
            if num_games % 2 == 1:
                if home_counts[t1] <= home_counts[t2]:
                    home_for_t1 += 1
                else:
                    home_for_t2 += 1

            for _ in range(home_for_t1):
                pair_games.append({"home": t1, "away": t2})
                home_counts[t1] += 1
            for _ in range(home_for_t2):
                pair_games.append({"home": t2, "away": t1})
                home_counts[t2] += 1

    random.shuffle(pair_games)

    total_days = SEASON_LENGTH_DAYS
    all_dates = [season_start + timedelta(days=i) for i in range(total_days)]
    random.shuffle(all_dates)

    games: List[Dict[str, Any]] = []
    day_game_counts: Dict[str, int] = {d.isoformat(): 0 for d in all_dates}
    team_day_played: Dict[str, set[str]] = {tid: set() for tid in teams}

    for idx, match in enumerate(pair_games):
        home = match["home"]
        away = match["away"]

        for d in all_dates:
            d_str = d.isoformat()
            if day_game_counts[d_str] >= MAX_GAMES_PER_DAY:
                continue
            if d_str in team_day_played[home] or d_str in team_day_played[away]:
                continue
            day_game_counts[d_str] += 1
            team_day_played[home].add(d_str)
            team_day_played[away].add(d_str)
            games.append(
                {
                    "game_id": f"g{idx}",
                    "date": d_str,
                    "season_id": season_id,
                    "phase": phase,
                    "home_team_id": home,
                    "away_team_id": away,
                    "status": "scheduled",
                    "home_score": None,
                    "away_score": None,
                }
            )
            break

    by_team: Dict[str, List[str]] = {tid: [] for tid in teams}
    by_date: Dict[str, List[str]] = {}
    for g in games:
        by_team[g["home_team_id"]].append(g["game_id"])
        by_team[g["away_team_id"]].append(g["game_id"])
        by_date.setdefault(g["date"], []).append(g["game_id"])

    master_schedule = league.setdefault("master_schedule", {})
    master_schedule["games"] = games
    master_schedule["by_team"] = by_team
    master_schedule["by_date"] = by_date
    master_schedule["by_id"] = {g["game_id"]: g for g in games}
    master_schedule.setdefault("version", 1)

    if previous_season_year and season_year != previous_season_year:
        league["current_date"] = None


def mark_master_schedule_game_final(
    league: Dict[str, Any],
    game_id: str,
    game_date_str: str,
    home_id: str,
    away_id: str,
    home_score: int,
    away_score: int,
) -> None:
    master_schedule = league.get("master_schedule") or {}
    games = master_schedule.get("games") or []
    by_id = master_schedule.get("by_id")
    if not isinstance(by_id, dict):
        by_id = {}
        master_schedule["by_id"] = by_id
    entry = by_id.get(game_id)
    if entry:
        entry["status"] = "final"
        entry["date"] = game_date_str
        entry["home_score"] = home_score
        entry["away_score"] = away_score
        return

    for g in games:
        if g.get("game_id") == game_id:
            g["status"] = "final"
            g["date"] = game_date_str
            g["home_score"] = home_score
            g["away_score"] = away_score
            by_id[game_id] = g
            return


def get_schedule_summary(league: Dict[str, Any]) -> Dict[str, Any]:
    master = league.get("master_schedule") or {}
    games = master.get("games") or []
    by_team = master.get("by_team") or {}

    status_counts: Dict[str, int] = {}
    home_away: Dict[str, Dict[str, int]] = {
        tid: {"home": 0, "away": 0} for tid in ALL_TEAM_IDS
    }

    for g in games:
        status = g.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        home_id = g.get("home_team_id")
        away_id = g.get("away_team_id")
        if home_id in home_away:
            home_away[home_id]["home"] += 1
        if away_id in home_away:
            home_away[away_id]["away"] += 1

    team_game_counts = {
        tid: len(by_team.get(tid, [])) for tid in ALL_TEAM_IDS
    }

    return {
        "total_games": len(games),
        "status_counts": status_counts,
        "team_game_counts": team_game_counts,
        "home_away_counts": home_away,
    }
