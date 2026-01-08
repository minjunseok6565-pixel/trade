from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from config import ROSTER_DF
from state import (
    _ensure_league_state,
    initialize_master_schedule_if_needed,
    set_current_date,
    update_state_with_game,
)
from trades_ai import _run_ai_gm_tick_if_needed
from match_engine import Team, MatchEngine


def advance_league_until(
    target_date_str: str,
    user_team_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """리그 전체를 target_date_str까지 자동 진행한다.

    - master_schedule을 기준으로
    - league.current_date 이후 ~ target_date_str 사이에 있는
      * 유저 팀(user_team_id)이 포함되지 않은
      * 아직 status != 'final' 인 경기만
      매치 엔진으로 시뮬레이션한다.
    - 각 경기 결과는 update_state_with_game(...)을 통해 GAME_STATE에 반영한다.
    - 반환값: update_state_with_game가 반환한 game_obj 리스트

    target_date_str 형식이 잘못된 경우 ValueError를 발생시킨다.
    """
    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
    master_schedule = league["master_schedule"]
    by_date: Dict[str, List[str]] = master_schedule.get("by_date") or {}
    games: List[Dict[str, Any]] = master_schedule.get("games") or []

    try:
        target_date = date.fromisoformat(target_date_str)
    except ValueError:
        raise ValueError(f"invalid target_date: {target_date_str}")

    # 마지막으로 리그를 돌린 날짜
    current_date_str = league.get("current_date")
    if current_date_str:
        try:
            current_date = date.fromisoformat(current_date_str)
        except ValueError:
            current_date = target_date
    else:
        # 아직 한 번도 진행 안 했다면 시즌 시작일 하루 전으로 간주
        if league.get("season_start"):
            try:
                season_start = date.fromisoformat(league["season_start"])
            except ValueError:
                season_start = target_date
        else:
            season_start = target_date
        current_date = season_start - timedelta(days=1)

    simulated_game_objs: List[Dict[str, Any]] = []
    user_team_upper = user_team_id.upper() if user_team_id else None

    day = current_date + timedelta(days=1)
    while day <= target_date:
        day_str = day.isoformat()
        game_ids = by_date.get(day_str, [])
        if not game_ids:
            day += timedelta(days=1)
            continue

        for gid in game_ids:
            # 해당 game_id에 대응하는 스케줄 엔트리 찾기
            g = next((x for x in games if x.get("game_id") == gid), None)
            if not g:
                continue
            if g.get("status") == "final":
                continue

            home_id = g["home_team_id"]
            away_id = g["away_team_id"]

            # 유저 팀 경기는 여기서 자동으로 돌리지 않는다.
            if user_team_upper and (home_id == user_team_upper or away_id == user_team_upper):
                continue

            home_df = ROSTER_DF[ROSTER_DF["Team"] == home_id]
            away_df = ROSTER_DF[ROSTER_DF["Team"] == away_id]
            if home_df.empty or away_df.empty:
                continue

            home_team = Team(home_id, home_df)
            away_team = Team(away_id, away_df)
            engine = MatchEngine(home_team, away_team)
            result = engine.simulate_game()
            score = result.get("final_score", {})

            game_obj = update_state_with_game(
                home_id=home_id,
                away_id=away_id,
                score=score,
                boxscore=result.get("boxscore"),
                game_date=day_str,
            )

            # master_schedule 엔트리에도 결과를 저장
            g["status"] = "final"
            g["home_score"] = int(score.get(home_id, 0))
            g["away_score"] = int(score.get(away_id, 0))

            simulated_game_objs.append(game_obj)

        day += timedelta(days=1)

    set_current_date(target_date_str)

    # AI GM 트레이드 틱 (트레이드 데드라인 및 7일 간격 체크 포함)
    # AI trade tick entrypoint; keep this as the sole call site.
    _run_ai_gm_tick_if_needed(target_date)

    return simulated_game_objs


def simulate_single_game(
    home_team_id: str,
    away_team_id: str,
    game_date: Optional[str] = None,
    home_tactics: Optional[Dict[str, Any]] = None,
    away_tactics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """한 경기를 시뮬레이션하고 GAME_STATE에 반영한다.

    - 팀 ID는 로스터 엑셀의 Team 값과 동일해야 한다.
    - 팀을 찾을 수 없는 경우 ValueError를 발생시킨다.
    """
    home_id = home_team_id.upper()
    away_id = away_team_id.upper()

    home_df = ROSTER_DF[ROSTER_DF["Team"] == home_id]
    away_df = ROSTER_DF[ROSTER_DF["Team"] == away_id]

    if home_df.empty:
        raise ValueError(f"Home team '{home_id}' not found in roster excel")
    if away_df.empty:
        raise ValueError(f"Away team '{away_id}' not found in roster excel")

    home_team = Team(home_id, home_df, tactics=home_tactics or {})
    away_team = Team(away_id, away_df, tactics=away_tactics or {})
    engine = MatchEngine(home_team, away_team)
    result = engine.simulate_game()

    # 인게임 날짜를 서버 STATE에도 반영
    update_state_with_game(
        home_id,
        away_id,
        result.get("final_score", {}),
        boxscore=result.get("boxscore"),
        game_date=game_date,
    )

    return result
