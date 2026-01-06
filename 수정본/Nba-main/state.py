from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from config import (
    ALL_TEAM_IDS,
    TEAM_TO_CONF_DIV,
    SEASON_START_MONTH,
    SEASON_START_DAY,
    SEASON_LENGTH_DAYS,
    MAX_GAMES_PER_DAY,
    DIVISIONS,
)

DEFAULT_TRADE_RULES: Dict[str, Any] = {
    "trade_deadline": None,
    "salary_cap": 0.0,
    "first_apron": 0.0,
    "second_apron": 0.0,
    "match_small_out_max": 7_500_000,
    "match_mid_out_max": 29_000_000,
    "match_mid_add": 7_500_000,
    "match_buffer": 250_000,
    "first_apron_mult": 1.10,
    "second_apron_mult": 1.00,
    "new_fa_sign_ban_days": 90,
    "aggregation_ban_days": 60,
    "max_pick_years_ahead": 7,
    "stepien_lookahead": 7,
}

# -------------------------------------------------------------------------
# 1. 전역 GAME_STATE 및 스케줄/리그 상태 유틸
# -------------------------------------------------------------------------
GAME_STATE: Dict[str, Any] = {
    "schema_version": "1.2",
    "turn": 0,
    "games": [],  # 각 경기의 메타 데이터
    "player_stats": {},  # player_id -> 시즌 누적 스탯
    "cached_views": {
        "scores": {
            "latest_date": None,
            "games": []  # 최근 경기일자 기준 경기 리스트
        },
        "schedule": {
            "teams": {}  # team_id -> {past_games: [], upcoming_games: []}
        },
        "stats": {
            "leaders": None,
        },
        "weekly_news": {
            "last_generated_week_start": None,
            "items": [],
        },
        "playoff_news": {
            "series_game_counts": {},
            "items": [],
        },
    },
    "postseason": {},  # 플레이-인/플레이오프 시뮬레이션 결과 캐시
    "league": {
        "season_year": None,
        "draft_year": None,  # 드래프트 연도(예: 2025-26 시즌이면 2026)
        "season_start": None,  # YYYY-MM-DD
        "current_date": None,  # 마지막으로 리그를 진행한 인게임 날짜
        "master_schedule": {
            "games": [],   # 전체 리그 경기 리스트
            "by_team": {},  # team_id -> [game_id, ...]
            "by_date": {},  # date_str -> [game_id, ...]
        },
        "trade_rules": {**DEFAULT_TRADE_RULES},
        "last_gm_tick_date": None,  # 마지막 AI GM 트레이드 시도 날짜
    },
    "teams": {},      # 팀 성향 / 메타 정보
    "players": {},    # 선수 메타 정보
    "transactions": [],  # 트레이드 등 기록
    "trade_agreements": {},  # deal_id -> committed deal data
    "negotiations": {},  # session_id -> negotiation sessions
    "draft_picks": {},  # Phase 3 later
    "asset_locks": {},  # asset_key -> {deal_id, expires_at}
}


def get_current_date() -> Optional[str]:
    """Return the league's current in-game date, keeping legacy mirrors in sync."""
    league = _ensure_league_state()
    current = league.get("current_date")
    legacy_current = GAME_STATE.get("current_date")

    if current:
        GAME_STATE["current_date"] = current
        return current

    if legacy_current:
        league["current_date"] = legacy_current
        return legacy_current

    return None


def get_current_date_as_date() -> date:
    """Return the league's current in-game date as a date object."""
    current = get_current_date()
    if current:
        try:
            return date.fromisoformat(str(current))
        except ValueError:
            pass

    league = _ensure_league_state()
    season_start = league.get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass

    return date.today()


def set_current_date(date_str: Optional[str]) -> None:
    """Update the league's current date and mirror it at the legacy location."""
    league = _ensure_league_state()
    league["current_date"] = date_str
    if date_str is None:
        GAME_STATE.pop("current_date", None)
    else:
        GAME_STATE["current_date"] = date_str


def _ensure_schedule_team(team_id: str) -> Dict[str, Any]:
    """GAME_STATE.cached_views.schedule에 팀 엔트리가 없으면 생성."""
    schedule = GAME_STATE["cached_views"]["schedule"]
    teams = schedule.setdefault("teams", {})
    if team_id not in teams:
        teams[team_id] = {
            "past_games": [],
            "upcoming_games": [],
        }
    return teams[team_id]


def normalize_player_keys(game_state: dict) -> dict:
    """
    Normalizes GAME_STATE player/free_agent identifiers.
    Returns a small report dict (counts + conflicts).
    """
    try:
        from config import ROSTER_DF
    except Exception:
        ROSTER_DF = None

    report = {
        "converted_players_keys_count": 0,
        "player_key_conflicts_count": 0,
        "non_numeric_player_keys_count": 0,
        "converted_free_agents_count": 0,
        "free_agents_non_numeric_count": 0,
        "roster_index_int_count": 0,
        "roster_index_numeric_str_count": 0,
        "roster_index_other_count": 0,
    }
    players = game_state.get("players")
    if not isinstance(players, dict):
        return report

    new_players: Dict[Any, Any] = {}
    source_is_int: Dict[Any, bool] = {}
    source_key_by_target: Dict[Any, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    non_numeric_keys: List[Any] = []

    for key, value in players.items():
        is_int_key = isinstance(key, int) and not isinstance(key, bool)
        target = key
        if not is_int_key:
            key_str = str(key).strip()
            if key_str.isdigit():
                target = int(key_str)
                report["converted_players_keys_count"] += 1
            else:
                non_numeric_keys.append(key)

        if target in new_players:
            existing_is_int = source_is_int[target]
            if existing_is_int and not is_int_key:
                conflicts.append({"int_key": target, "dropped_key": key})
                continue
            if not existing_is_int and is_int_key:
                conflicts.append(
                    {"int_key": target, "dropped_key": source_key_by_target[target]}
                )
                new_players[target] = value
                source_is_int[target] = True
                source_key_by_target[target] = key
                continue
            conflicts.append({"int_key": target, "dropped_key": key})
            continue

        new_players[target] = value
        source_is_int[target] = is_int_key
        source_key_by_target[target] = key

    game_state["players"] = new_players

    fas_present = "free_agents" in game_state
    fas = game_state.get("free_agents")
    non_numeric_free_agents: List[Any] = []
    if fas_present and isinstance(fas, list):
        new_fas: List[int] = []
        seen: set[int] = set()
        for item in fas:
            if isinstance(item, int) and not isinstance(item, bool):
                value = item
            else:
                item_str = str(item).strip()
                if not item_str.isdigit():
                    non_numeric_free_agents.append(item)
                    continue
                value = int(item_str)
                report["converted_free_agents_count"] += 1
            if value in seen:
                continue
            new_fas.append(value)
            seen.add(value)
        game_state["free_agents"] = new_fas
    elif fas_present:
        game_state["free_agents"] = []

    report["player_key_conflicts_count"] = len(conflicts)
    report["non_numeric_player_keys_count"] = len(non_numeric_keys)
    report["free_agents_non_numeric_count"] = len(non_numeric_free_agents)
    if not fas_present:
        report["free_agents_missing"] = True

    if conflicts:
        report["player_key_conflicts"] = conflicts[:10]
    if non_numeric_keys:
        report["non_numeric_player_keys"] = non_numeric_keys[:10]
    if non_numeric_free_agents:
        report["free_agents_non_numeric"] = non_numeric_free_agents[:10]

    if ROSTER_DF is not None:
        try:
            sample_index = list(ROSTER_DF.index[:20])
        except Exception:
            sample_index = []
        roster_index_int_count = 0
        roster_index_numeric_str_count = 0
        roster_index_other_count = 0
        for value in sample_index:
            if isinstance(value, int) and not isinstance(value, bool):
                roster_index_int_count += 1
            elif str(value).strip().isdigit():
                roster_index_numeric_str_count += 1
            else:
                roster_index_other_count += 1
        report["roster_index_int_count"] = roster_index_int_count
        report["roster_index_numeric_str_count"] = roster_index_numeric_str_count
        report["roster_index_other_count"] = roster_index_other_count
        if roster_index_numeric_str_count > 0:
            report.setdefault("warnings", []).append(
                "ROSTER_DF.index contains numeric strings; players keys are normalized to int. "
                "Ensure roster index is int to avoid lookup mismatches."
            )

    debug = game_state.setdefault("debug", {})
    debug.setdefault("normalization", []).append(report)
    return report


def _ensure_league_state() -> Dict[str, Any]:
    """GAME_STATE 안에 league 상태 블록을 보장한다."""
    league = GAME_STATE.setdefault("league", {})
    master_schedule = league.setdefault("master_schedule", {})
    master_schedule.setdefault("games", [])
    master_schedule.setdefault("by_team", {})
    master_schedule.setdefault("by_date", {})
    trade_rules = league.setdefault("trade_rules", {})
    for key, value in DEFAULT_TRADE_RULES.items():
        trade_rules.setdefault(key, value)
    league.setdefault("season_year", None)
    league.setdefault("draft_year", None)
    league.setdefault("season_start", None)
    league.setdefault("current_date", None)
    league.setdefault("last_gm_tick_date", None)
    _ensure_trade_state()
    from contracts.store import ensure_contract_state

    ensure_contract_state(GAME_STATE)
    from team_utils import _init_players_and_teams_if_needed

    _init_players_and_teams_if_needed()
    # Normalize player keys to int to prevent duplicate "12"/12 entries.
    normalize_player_keys(GAME_STATE)

    from contracts.bootstrap import bootstrap_contracts_from_roster_excel

    bootstrap_contracts_from_roster_excel(GAME_STATE, overwrite=False)

    from contracts.store import get_league_season_year
    from contracts.sync import (
        sync_contract_team_ids_from_players,
        sync_players_salary_from_active_contract,
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    season_year = get_league_season_year(GAME_STATE)
    sync_contract_team_ids_from_players(GAME_STATE)
    sync_players_salary_from_active_contract(GAME_STATE, season_year)
    sync_roster_teams_from_state(GAME_STATE)
    sync_roster_salaries_for_season(GAME_STATE, season_year)
    return league


def _ensure_trade_state() -> None:
    """트레이드 관련 GAME_STATE 키를 보장한다."""
    GAME_STATE.setdefault("trade_agreements", {})
    GAME_STATE.setdefault("negotiations", {})
    GAME_STATE.setdefault("draft_picks", {})
    GAME_STATE.setdefault("asset_locks", {})


_ensure_league_state()


def _build_master_schedule(season_year: int) -> None:
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
    league = _ensure_league_state()
    from trades.picks import init_draft_picks_if_needed

    # season_year는 "시즌 시작 연도" (예: 2025-26 시즌이면 2025)
    # draft_year는 "드래프트 연도" (예: 2025-26 시즌이면 2026)
    # 픽 생성/Stepien/7년 룰은 draft_year를 기준으로 맞추기 위해 미리 저장해 둔다.
    previous_season_year = league.get("season_year")
    league["season_year"] = season_year
    league["draft_year"] = season_year + 1

    init_draft_picks_if_needed(GAME_STATE, league["draft_year"], list(ALL_TEAM_IDS))

    season_start = date(season_year, SEASON_START_MONTH, SEASON_START_DAY)
    teams = list(ALL_TEAM_IDS)

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

    league["season_year"] = season_year
    league["draft_year"] = season_year + 1
    league["season_start"] = season_start.isoformat()
    trade_deadline_date = date(season_year + 1, 2, 5)
    league["trade_rules"]["trade_deadline"] = trade_deadline_date.isoformat()
    set_current_date(None)
    league["last_gm_tick_date"] = None
    try:
        previous_season = int(previous_season_year or 0)
    except (TypeError, ValueError):
        previous_season = 0
    try:
        next_season = int(season_year or 0)
    except (TypeError, ValueError):
        next_season = 0
    if previous_season and next_season and previous_season != next_season:
        from contracts.offseason import process_offseason

        process_offseason(GAME_STATE, previous_season, next_season)


def initialize_master_schedule_if_needed() -> None:
    """master_schedule이 비어 있으면 현재 연도를 기준으로 한 번 생성한다."""
    league = _ensure_league_state()
    master_schedule = league["master_schedule"]
    if master_schedule.get("games"):
        return

    today = date.today()
    season_year = today.year
    _build_master_schedule(season_year)


def _mark_master_schedule_game_final(
    game_id: str,
    game_date_str: str,
    home_id: str,
    away_id: str,
    home_score: int,
    away_score: int,
) -> None:
    """마스터 스케줄에 동일한 game_id가 있으면 결과를 반영한다."""
    league = GAME_STATE.get("league")
    if not league:
        return
    master_schedule = (league.get("master_schedule") or {})
    games = master_schedule.get("games") or []

    for g in games:
        if g.get("game_id") == game_id:
            g["status"] = "final"
            g["date"] = game_date_str
            g["home_score"] = home_score
            g["away_score"] = away_score
            return


# -------------------------------------------------------------------------
# 2. 경기를 상태에 반영 / STATE 업데이트 유틸
# -------------------------------------------------------------------------
def update_state_with_game(
    home_id: str,
    away_id: str,
    score: Dict[str, int],
    boxscore: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    game_date: Optional[str] = None,
) -> Dict[str, Any]:
    """매치엔진 결과를 GAME_STATE와 cached_views에 반영.

    - game_date 가 주어지면 그 값을 사용, 없으면 서버 기준 오늘 날짜 사용.
    - boxscore 가 주어지면 시즌 누적 player_stats 에 반영한다.
    """
    game_date_str = str(game_date) if game_date else date.today().isoformat()
    game_id = f"{game_date_str}_{home_id}_{away_id}"

    home_score = int(score.get(home_id, 0))
    away_score = int(score.get(away_id, 0))

    game_obj = {
        "game_id": game_id,
        "date": game_date_str,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "status": "final",
        "is_overtime": False,
    }

    # turn 카운트 증가
    GAME_STATE["turn"] += 1

    # games 리스트에 추가
    GAME_STATE["games"].append(game_obj)

    if boxscore:
        _update_player_stats_from_boxscore(boxscore)

    # scores 캐시 업데이트 (가장 최근 일자 기준)
    scores_view = GAME_STATE["cached_views"]["scores"]
    scores_view["latest_date"] = game_date_str
    scores_view.setdefault("games", [])
    scores_view["games"].insert(0, game_obj)

    # schedule 캐시 (양 팀 모두 과거 경기로 추가)
    for team_id, my_score, opp_score in [
        (home_id, home_score, away_score),
        (away_id, away_score, home_score),
    ]:
        schedule_entry = _ensure_schedule_team(team_id)
        result = "W" if my_score > opp_score else "L"
        schedule_entry["past_games"].insert(0, {
            "game_id": game_id,
            "date": game_date_str,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_score": home_score,
            "away_score": away_score,
            "result_for_user_team": result,
        })


    # 마스터 스케줄에 해당 경기가 존재한다면 결과도 반영
    _mark_master_schedule_game_final(
        game_id=game_id,
        game_date_str=game_date_str,
        home_id=home_id,
        away_id=away_id,
        home_score=home_score,
        away_score=away_score,
    )

    return game_obj


def _update_player_stats_from_boxscore(boxscore: Dict[str, List[Dict[str, Any]]]) -> None:
    """박스스코어를 시즌 누적 player_stats에 반영한다."""
    if not boxscore:
        return

    season_stats = GAME_STATE.setdefault("player_stats", {})
    track_stats = ["PTS", "AST", "REB", "3PM"]

    for team_rows in boxscore.values():
        if not isinstance(team_rows, list):
            continue
        for row in team_rows:
            if not isinstance(row, dict):
                continue
            player_id = row.get("PlayerID")
            if player_id is None:
                continue
            stat_entry = season_stats.setdefault(
                player_id,
                {
                    "player_id": player_id,
                    "name": row.get("Name"),
                    "team_id": row.get("Team"),
                    "games": 0,
                    "totals": {s: 0.0 for s in track_stats},
                },
            )

            stat_entry["name"] = row.get("Name", stat_entry.get("name"))
            stat_entry["team_id"] = row.get("Team", stat_entry.get("team_id"))
            stat_entry["games"] = stat_entry.get("games", 0) + 1

            totals = stat_entry.setdefault("totals", {s: 0.0 for s in track_stats})
            for stat_name in track_stats:
                try:
                    totals[stat_name] = float(totals.get(stat_name, 0.0)) + float(
                        row.get(stat_name, 0) or 0
                    )
                except (TypeError, ValueError):
                    continue



def _update_playoff_player_stats_from_boxscore(boxscore: Dict[str, List[Dict[str, Any]]]) -> None:
    """박스스코어를 포스트시즌 누적 player_stats에 반영한다."""
    if not boxscore:
        return

    postseason = GAME_STATE.setdefault("postseason", {})
    playoff_stats = postseason.setdefault("playoff_player_stats", {})
    track_stats = ["PTS", "AST", "REB", "3PM"]

    for team_rows in boxscore.values():
        if not isinstance(team_rows, list):
            continue
        for row in team_rows:
            if not isinstance(row, dict):
                continue
            player_id = row.get("PlayerID")
            if player_id is None:
                continue
            stat_entry = playoff_stats.setdefault(
                player_id,
                {
                    "player_id": player_id,
                    "name": row.get("Name"),
                    "team_id": row.get("Team"),
                    "games": 0,
                    "totals": {s: 0.0 for s in track_stats},
                },
            )

            stat_entry["name"] = row.get("Name", stat_entry.get("name"))
            stat_entry["team_id"] = row.get("Team", stat_entry.get("team_id"))
            stat_entry["games"] = stat_entry.get("games", 0) + 1

            totals = stat_entry.setdefault("totals", {s: 0.0 for s in track_stats})
            for stat_name in track_stats:
                try:
                    totals[stat_name] = float(totals.get(stat_name, 0.0)) + float(
                        row.get(stat_name, 0) or 0
                    )
                except (TypeError, ValueError):
                    continue


def get_schedule_summary() -> Dict[str, Any]:
    """마스터 스케줄 통계 요약을 반환한다.

    - 총 경기 수, 상태별 경기 수
    - 팀별 총 경기 수(82 보장 여부)와 홈/원정 분배
    """
    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
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
        "team_breakdown": team_breakdown,
    }

