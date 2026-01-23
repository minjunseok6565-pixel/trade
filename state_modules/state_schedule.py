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
from .state_bootstrap import ensure_contracts_bootstrapped_after_schedule_creation_once
from .state_cap import _apply_cap_model_for_season
from .state_core import (
    _archive_and_reset_season_accumulators,
    _ensure_active_season_id,
    _season_id_from_year,
    ensure_league_block,
    set_current_date,
)
from .state_constants import _ALLOWED_SCHEDULE_STATUSES


def _ensure_schedule_team(state: dict, team_id: str) -> Dict[str, Any]:
    """cached_views.schedule에 팀 엔트리가 없으면 생성."""
    schedule = state["cached_views"]["schedule"]
    teams = schedule.setdefault("teams", {})
    if team_id not in teams:
        teams[team_id] = {
            "past_games": [],
            "upcoming_games": [],
        }
    return teams[team_id]


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


def _ensure_master_schedule_indices(state: dict) -> None:
    league = ensure_league_block(state)
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


def _build_master_schedule(state: dict, season_year: int) -> None:
    """30개 팀 전체에 대한 마스터 스케줄(정규시즌)을 생성한다.

    - 실제 NBA 규칙을 근사하여 **항상 1230경기, 팀당 82경기**가 되도록
      경기 수를 결정한다.
    - 같은 디비전: 4경기
    - 같은 컨퍼런스 다른 디비전: 한 팀당 6개 팀과는 4경기, 4개 팀과는 3경기
      (규칙적인 회전 매핑으로 결정)
    - 다른 컨퍼런스: 2경기
    - 홈/원정은 누적 홈 경기 수를 고려해 최대한 41/41에 가깝게 분배한다.
    - 시즌 기간(SEASON_LENGTH_DAYS) 동안 날짜를 랜덤 배정하되
      * 하루 최대 MAX_GAMES_PER_DAY 경기
      * 한 팀은 하루에 최대 1경기
    """
    league = ensure_league_block(state)
    from league_repo import LeagueRepo

    # season_year는 "시즌 시작 연도" (예: 2025-26 시즌이면 2025)
    # draft_year는 "드래프트 연도" (예: 2025-26 시즌이면 2026)
    # 픽 생성/Stepien/7년 룰은 draft_year를 기준으로 맞추기 위해 미리 저장해 둔다.
    previous_season_year = league.get("season_year")
    league["season_year"] = season_year
    league["draft_year"] = season_year + 1
    _apply_cap_model_for_season(league, season_year)

    # Stepien 룰은 (year, year+1) 쌍을 검사하기 때문에,
    # lookahead=N이면 draft_year+N+1까지 "픽 데이터가 존재"해야 데이터 결측으로 인한 오판을 피할 수 있다.
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

    # 팀별 컨퍼런스/디비전 정보 캐시
    team_info: Dict[str, Dict[str, Optional[str]]] = {}
    for tid in teams:
        info = TEAM_TO_CONF_DIV.get(tid, {"conference": None, "division": None})
        team_info[tid] = {
            "conference": info.get("conference"),
            "division": info.get("division"),
        }

    # 컨퍼런스 내 다른 디비전 4경기 매칭을 결정하는 헬퍼 (5x5 회전 매핑)
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
                    for delta in range(3):  # 각 팀이 상대 디비전 팀 3명에게 4경기 배정
                        b_team = b_div[(idx + delta) % len(b_div)]
                        pair = tuple(sorted((a_team, b_team)))
                        pairs.add(pair)
        return pairs

    four_game_pairs_east = _four_game_pairs_for_conf("East")
    four_game_pairs_west = _four_game_pairs_for_conf("West")
    four_game_pairs = four_game_pairs_east | four_game_pairs_west

    # 1) 팀 쌍별로 경기 수 결정 + 홈/원정 분배
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
                num_games = 2  # 다른 컨퍼런스는 2경기 고정
            elif div1 == div2:
                num_games = 4  # 같은 디비전
            else:
                # 같은 컨퍼런스 다른 디비전
                pair_key = tuple(sorted((t1, t2)))
                num_games = 4 if pair_key in four_game_pairs else 3

            # 홈/원정 분배 (3경기일 때는 현재 홈 수가 적은 팀에 추가 배정)
            home_for_t1 = num_games // 2
            home_for_t2 = num_games // 2
            if num_games % 2 == 1:
                if home_counts[t1] <= home_counts[t2]:
                    home_for_t1 += 1
                else:
                    home_for_t2 += 1

            for _ in range(home_for_t1):
                pair_games.append({
                    "home_team_id": t1,
                    "away_team_id": t2,
                })
            for _ in range(home_for_t2):
                pair_games.append({
                    "home_team_id": t2,
                    "away_team_id": t1,
                })

            home_counts[t1] += home_for_t1
            home_counts[t2] += home_for_t2

    # 2) 날짜 배정
    random.shuffle(pair_games)

    by_date: Dict[str, List[str]] = {}
    teams_per_date: Dict[str, set] = {}
    scheduled_games: List[Dict[str, Any]] = []

    for game in pair_games:
        home_id = game["home_team_id"]
        away_id = game["away_team_id"]

        assigned = False
        for _ in range(100):
            day_index = random.randint(0, SEASON_LENGTH_DAYS - 1)
            game_date = season_start + timedelta(days=day_index)
            date_str = game_date.isoformat()

            teams_today = teams_per_date.setdefault(date_str, set())
            games_today = by_date.setdefault(date_str, [])

            if len(games_today) >= MAX_GAMES_PER_DAY:
                continue
            if home_id in teams_today or away_id in teams_today:
                continue

            teams_today.add(home_id)
            teams_today.add(away_id)

            game_id = f"{date_str}_{home_id}_{away_id}"
            scheduled_games.append({
                "game_id": game_id,
                "date": date_str,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "status": "scheduled",
                "home_score": None,
                "away_score": None,
                "season_id": season_id,
                "phase": phase,
            })
            games_today.append(game_id)
            assigned = True
            break

        if not assigned:
            day_index = random.randint(0, SEASON_LENGTH_DAYS - 1)
            game_date = season_start + timedelta(days=day_index)
            date_str = game_date.isoformat()
            teams_today = teams_per_date.setdefault(date_str, set())
            games_today = by_date.setdefault(date_str, [])
            teams_today.add(home_id)
            teams_today.add(away_id)
            game_id = f"{date_str}_{home_id}_{away_id}"
            scheduled_games.append({
                "game_id": game_id,
                "date": date_str,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "status": "scheduled",
                "home_score": None,
                "away_score": None,
                "season_id": season_id,
                "phase": phase,
            })
            games_today.append(game_id)

    # 3) by_team 인덱스 생성
    by_team: Dict[str, List[str]] = {tid: [] for tid in teams}
    for g in scheduled_games:
        by_team[g["home_team_id"]].append(g["game_id"])
        by_team[g["away_team_id"]].append(g["game_id"])

    master_schedule = league["master_schedule"]
    master_schedule["games"] = scheduled_games
    master_schedule["by_team"] = by_team
    master_schedule["by_date"] = by_date
    master_schedule["by_id"] = {g["game_id"]: g for g in scheduled_games}

    league["season_year"] = season_year
    league["draft_year"] = season_year + 1
    league["season_start"] = season_start.isoformat()
    trade_deadline_date = date(season_year + 1, 2, 5)
    league["trade_rules"]["trade_deadline"] = trade_deadline_date.isoformat()
    set_current_date(state, None)
    league["last_gm_tick_date"] = None
    try:
        previous_season = int(previous_season_year or 0)
    except (TypeError, ValueError):
        previous_season = 0
    try:
        next_season = int(season_year or 0)
    except (TypeError, ValueError):
        next_season = 0
    # 누적 스탯(정규시즌)은 시즌 단위로 끊는다.
    # - 시즌이 바뀌면 기존 누적은 history로 보관하고, 새 시즌 누적을 새로 시작한다.
    if previous_season and next_season and previous_season != next_season:
        from contracts.offseason import process_offseason

        process_offseason(state, previous_season, next_season)
        _archive_and_reset_season_accumulators(
            state, _season_id_from_year(previous_season), _season_id_from_year(next_season)
        )
    else:
        _ensure_active_season_id(state, _season_id_from_year(int(next_season or season_year)))

    from state_schema import validate_game_state

    validate_game_state(state)


def initialize_master_schedule_if_needed(state: dict) -> None:
    """master_schedule이 비어 있으면 현재 연도를 기준으로 한 번 생성한다."""
    league = ensure_league_block(state)
    master_schedule = league["master_schedule"]
    if master_schedule.get("games"):
        _ensure_master_schedule_indices(state)
        from state_schema import validate_game_state

        validate_game_state(state)
        return

    # season_year는 "시즌 시작 연도" (예: 2025-26 시즌이면 2025)
    season_year = INITIAL_SEASON_YEAR
    _build_master_schedule(state, season_year)
    _ensure_master_schedule_indices(state)

    # Schedule creation is the agreed contract-bootstrap checkpoint (once per season).
    ensure_contracts_bootstrapped_after_schedule_creation_once(state)
    from state_schema import validate_game_state

    validate_game_state(state)


def _mark_master_schedule_game_final(
    state: dict,
    game_id: str,
    game_date_str: str,
    home_id: str,
    away_id: str,
    home_score: int,
    away_score: int,
) -> None:
    """마스터 스케줄에 동일한 game_id가 있으면 결과를 반영한다."""
    league = ensure_league_block(state)
    master_schedule = league.setdefault("master_schedule", {})
    games = master_schedule.get("games") or []
    by_id = master_schedule.setdefault("by_id", {})
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


def get_schedule_summary(state: dict) -> Dict[str, Any]:
    """마스터 스케줄 통계 요약을 반환한다.

    - 총 경기 수, 상태별 경기 수
    - 팀별 총 경기 수(82 보장 여부)와 홈/원정 분배
    """
    initialize_master_schedule_if_needed(state)
    league = ensure_league_block(state)
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

    team_breakdown: Dict[str, Dict[str, Any]] = {}
    for tid in ALL_TEAM_IDS:
        team_breakdown[tid] = {
            "games": len(by_team.get(tid, [])),
            "home": home_away.get(tid, {}).get("home", 0),
            "away": home_away.get(tid, {}).get("away", 0),
        }
        team_breakdown[tid]["home_away_diff"] = (
            team_breakdown[tid]["home"] - team_breakdown[tid]["away"]
        )

    return {
        "total_games": len(games),
        "status_counts": status_counts,
        "teams": team_breakdown,
    }
