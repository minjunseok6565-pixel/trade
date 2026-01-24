from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from config import (
    ALL_TEAM_IDS,
    DIVISIONS,
    MAX_GAMES_PER_DAY,
    SEASON_LENGTH_DAYS,
    SEASON_START_DAY,
    SEASON_START_MONTH,
    TEAM_TO_CONF_DIV,
)
from .state_constants import _ALLOWED_SCHEDULE_STATUSES
from schema import season_id_from_year as _schema_season_id_from_year


def _season_id_from_year(season_year: int) -> str:
    """시즌 시작 연도(int) -> season_id 문자열. 예: 2025 -> '2025-26'"""
    return str(_schema_season_id_from_year(int(season_year)))


# - 이 모듈은 "master_schedule 생성/인덱싱/업데이트"만 담당한다.
# - 시즌 전환(archive/reset), active_season_id 설정, season_history 작성,
#   DB seed/cap 모델 적용 등 라이프사이클은 **반드시 state.py(facade)에서만** 수행한다.
#   (레거시 경로 재발 방지)


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


def ensure_master_schedule_indices(master_schedule: dict) -> None:
    """master_schedule의 최소 계약 검증 + by_id 인덱스를 보장한다."""
    if not isinstance(master_schedule, dict):
        raise ValueError("master_schedule must be a dict")
        
    games = master_schedule.get("games") or []
    if not isinstance(games, list):
        raise ValueError("master_schedule.games must be a list")
        
    # Contract check: master_schedule entries must satisfy the minimal schema.
    for i, g in enumerate(games):
        validate_master_schedule_entry(g, path=f"master_schedule.games[{i}]")
    by_id = master_schedule.get("by_id")
    if not isinstance(by_id, dict) or len(by_id) != len(games):
        master_schedule["by_id"] = {g.get("game_id"): g for g in games if isinstance(g, dict) and g.get("game_id")}


def build_master_schedule(
    *,
    season_year: int,
    season_start: Optional[date] = None,
    rng_seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    정규시즌(regular) 마스터 스케줄을 **순수하게 생성**해서 반환한다.

    반환 포맷(league['master_schedule']에 그대로 넣을 수 있음):
      {
        "games": [...],
        "by_team": {team_id: [game_id, ...]},
        "by_date": {date_str: [game_id, ...]},
        "by_id": {game_id: entry},
      }

    주의:
    - 이 함수는 state/DB/season_history/active_season_id에 접근하지 않는다.
    - 시즌 전환/오프시즌 처리/아카이브 등 라이프사이클은 facade(state.py)가 담당한다.
    """
    if season_start is None:
        season_start = date(int(season_year), SEASON_START_MONTH, SEASON_START_DAY)

    teams = list(ALL_TEAM_IDS)
    season_id = _season_id_from_year(int(season_year))
    phase = "regular"

    rng = random.Random(rng_seed)

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

    four_game_pairs = _four_game_pairs_for_conf("East") | _four_game_pairs_for_conf("West")

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
                pair_games.append({"home_team_id": t1, "away_team_id": t2})
            for _ in range(home_for_t2):
                pair_games.append({"home_team_id": t2, "away_team_id": t1})

            home_counts[t1] += home_for_t1
            home_counts[t2] += home_for_t2

    # 2) 날짜 배정
    rng.shuffle(pair_games)

    by_date: Dict[str, List[str]] = {}
    teams_per_date: Dict[str, set] = {}
    scheduled_games: List[Dict[str, Any]] = []

    for game in pair_games:
        home_id = game["home_team_id"]
        away_id = game["away_team_id"]

        assigned = False
        for _ in range(100):
            day_index = rng.randint(0, SEASON_LENGTH_DAYS - 1)
            game_date = season_start + timedelta(days=day_index)
            date_str = game_date.isoformat()

            if date_str not in teams_per_date:
                teams_per_date[date_str] = set()
            if date_str not in by_date:
                by_date[date_str] = []
            teams_today = teams_per_date[date_str]
            games_today = by_date[date_str]

            if len(games_today) >= MAX_GAMES_PER_DAY:
                continue
            if home_id in teams_today or away_id in teams_today:
                continue

            teams_today.add(home_id)
            teams_today.add(away_id)

            game_id = f"{date_str}_{home_id}_{away_id}"
            scheduled_games.append(
                {
                    "game_id": game_id,
                    "date": date_str,
                    "home_team_id": home_id,
                    "away_team_id": away_id,
                    "status": "scheduled",
                    "home_score": None,
                    "away_score": None,
                    "season_id": season_id,
                    "phase": phase,
                }
            )
            games_today.append(game_id)
            assigned = True
            break

        if not assigned:
            # 마지막 안전장치: 제약을 덜고 랜덤 배정
            day_index = rng.randint(0, SEASON_LENGTH_DAYS - 1)
            game_date = season_start + timedelta(days=day_index)
            date_str = game_date.isoformat()
            if date_str not in teams_per_date:
                teams_per_date[date_str] = set()
            if date_str not in by_date:
                by_date[date_str] = []
            teams_today = teams_per_date[date_str]
            games_today = by_date[date_str]
            teams_today.add(home_id)
            teams_today.add(away_id)
            game_id = f"{date_str}_{home_id}_{away_id}"
            scheduled_games.append(
                {
                    "game_id": game_id,
                    "date": date_str,
                    "home_team_id": home_id,
                    "away_team_id": away_id,
                    "status": "scheduled",
                    "home_score": None,
                    "away_score": None,
                    "season_id": season_id,
                    "phase": phase,
                }
            )
            games_today.append(game_id)

    # 3) by_team 인덱스 생성
    by_team: Dict[str, List[str]] = {tid: [] for tid in teams}
    for g in scheduled_games:
        by_team[g["home_team_id"]].append(g["game_id"])
        by_team[g["away_team_id"]].append(g["game_id"])

    return {
        "games": scheduled_games,
        "by_team": by_team,
        "by_date": by_date,
        "by_id": {g["game_id"]: g for g in scheduled_games},
    }


def mark_master_schedule_game_final(
    master_schedule: dict,
    *,
    game_id: str,
    game_date_str: str,
    home_id: str,
    away_id: str,
    home_score: int,
    away_score: int,
) -> None:
    """마스터 스케줄에 동일한 game_id가 있으면 결과를 반영한다."""
    if not isinstance(master_schedule, dict):
        raise ValueError("master_schedule must be a dict")
    games = master_schedule.get("games") or []
    by_id = master_schedule.get("by_id")
    if not isinstance(by_id, dict):
        raise ValueError("master_schedule.by_id must be a dict")
    entry = by_id.get(game_id)
    if entry:
        entry["status"] = "final"
        entry["date"] = game_date_str
        entry["home_score"] = home_score
        entry["away_score"] = away_score
        return

    for g in games:
        if isinstance(g, dict) and g.get("game_id") == game_id:
            g["status"] = "final"
            g["date"] = game_date_str
            g["home_score"] = home_score
            g["away_score"] = away_score
            by_id[game_id] = g
            return


def get_schedule_summary(master_schedule: dict) -> Dict[str, Any]:
    """마스터 스케줄 통계 요약을 반환한다.

    - 총 경기 수, 상태별 경기 수
    - 팀별 총 경기 수(82 보장 여부)와 홈/원정 분배

    주의:
    - 이 함수는 schedule을 생성하지 않는다.
    - schedule 생성/재생성은 facade(state.py)가 ensure_schedule_for_active_season()로 수행해야 한다.
    """
    ensure_master_schedule_indices(master_schedule)

    games = master_schedule.get("games") or []
    by_team = master_schedule.get("by_team") or {}

    status_counts: Dict[str, int] = {}
    home_away: Dict[str, Dict[str, int]] = {tid: {"home": 0, "away": 0} for tid in ALL_TEAM_IDS}

    for g in games:
        if not isinstance(g, dict):
            continue
        status = g.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        home_team_id = g.get("home_team_id")
        away_team_id = g.get("away_team_id")
        if home_team_id in home_away:
            home_away[home_team_id]["home"] += 1
        if away_team_id in home_away:
            home_away[away_team_id]["away"] += 1

    team_breakdown: Dict[str, Dict[str, Any]] = {}
    for tid in ALL_TEAM_IDS:
        games_for_team = by_team.get(tid, [])
        team_breakdown[tid] = {
            "games": len(games_for_team) if isinstance(games_for_team, list) else 0,
            "home": home_away.get(tid, {}).get("home", 0),
            "away": home_away.get(tid, {}).get("away", 0),
        }
        team_breakdown[tid]["home_away_diff"] = team_breakdown[tid]["home"] - team_breakdown[tid]["away"]

    return {
        "total_games": len(games) if isinstance(games, list) else 0,
        "status_counts": status_counts,
        "teams": team_breakdown,
    }
