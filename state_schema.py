from __future__ import annotations

from typing import Any, Dict

from state_modules.state_constants import DEFAULT_TRADE_RULES, _DEFAULT_TRADE_MARKET, _DEFAULT_TRADE_MEMORY

SCHEMA_VERSION = "3.0"


def create_default_game_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "turn": 0,
        "games": [],  # 각 경기의 메타 데이터
        "player_stats": {},  # player_id -> 시즌 누적 스탯
        "team_stats": {},  # team_id -> 시즌 누적 팀 스탯(가공 팀 박스)
        "game_results": {},  # game_id -> 매치엔진 원본 결과(신규 엔진 기준)
        "active_season_id": None,  # 현재 누적이 쌓이는 season_id (예: "2025-26")
        "season_history": {},  # season_id -> {games, player_stats, team_stats, game_results}
        "_migrations": {},
        "phase_results": {
            "preseason": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
            "play_in": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
            "playoffs": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        },
        "cached_views": {
            "scores": {
                "latest_date": None,
                "games": [],  # 최근 경기일자 기준 경기 리스트
            },
            "schedule": {
                "teams": {},  # team_id -> {past_games: [], upcoming_games: []}
            },
            "_meta": {
                "scores": {"built_from_turn": -1, "season_id": None},
                "schedule": {"built_from_turn_by_team": {}, "season_id": None},
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
        "postseason": {
            "field": None,
            "play_in": None,
            "playoffs": None,
            "champion": None,
            "my_team_id": None,
            "playoff_player_stats": {},
        },  # 플레이-인/플레이오프 시뮬레이션 결과 캐시
        "league": {
            "season_year": None,
            "draft_year": None,  # 드래프트 연도(예: 2025-26 시즌이면 2026)
            "season_start": None,  # YYYY-MM-DD
            "current_date": None,  # 마지막으로 리그를 진행한 인게임 날짜
            "db_path": None,
            "master_schedule": {
                "games": [],  # 전체 리그 경기 리스트
                "by_team": {},  # team_id -> [game_id, ...]
                "by_date": {},  # date_str -> [game_id, ...]
                "by_id": {},
            },
            "trade_rules": {**DEFAULT_TRADE_RULES},
            "last_gm_tick_date": None,  # 마지막 AI GM 트레이드 시도 날짜
        },
        "teams": {},  # 팀 성향 / 메타 정보
        "players": {},  # 선수 메타 정보
        "trade_agreements": {},  # deal_id -> committed deal data
        "negotiations": {},  # session_id -> negotiation sessions
        "asset_locks": {},  # asset_key -> {deal_id, expires_at}
        "trade_market": dict(_DEFAULT_TRADE_MARKET),
        "trade_memory": dict(_DEFAULT_TRADE_MEMORY),
    }


def _require_container(state: dict, key: str, expected_type: type, type_label: str) -> Any:
    value = state.get(key)
    if not isinstance(value, expected_type):
        raise ValueError(f"GameState invalid: {key} must be {type_label}")
    return value


def _require_nested_container(container: dict, path: str, expected_type: type, type_label: str) -> Any:
    value = container.get(path)
    if not isinstance(value, expected_type):
        raise ValueError(f"GameState invalid: {path} must be {type_label}")
    return value


def validate_game_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError("GameState invalid: state must be a dict")

    schema_version = state.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"GameState invalid: schema_version must be '{SCHEMA_VERSION}'")

    if not isinstance(state.get("turn"), int):
        raise ValueError("GameState invalid: turn must be int")

    _require_container(state, "games", list, "list")
    _require_container(state, "player_stats", dict, "dict")
    _require_container(state, "team_stats", dict, "dict")
    _require_container(state, "game_results", dict, "dict")

    active_season_id = state.get("active_season_id")
    if active_season_id is not None and not isinstance(active_season_id, str):
        raise ValueError("GameState invalid: active_season_id must be str or None")

    _require_container(state, "season_history", dict, "dict")
    phase_results = _require_container(state, "phase_results", dict, "dict")
    cached_views = _require_container(state, "cached_views", dict, "dict")
    postseason = _require_container(state, "postseason", dict, "dict")
    league = _require_container(state, "league", dict, "dict")
    _require_container(state, "teams", dict, "dict")
    _require_container(state, "players", dict, "dict")
    _require_container(state, "trade_agreements", dict, "dict")
    _require_container(state, "negotiations", dict, "dict")
    _require_container(state, "asset_locks", dict, "dict")
    _require_container(state, "trade_market", dict, "dict")
    _require_container(state, "trade_memory", dict, "dict")

    phase_keys = {"preseason", "play_in", "playoffs"}
    if set(phase_results.keys()) != phase_keys:
        raise ValueError("GameState invalid: phase_results must contain preseason, play_in, playoffs only")
    required_result_keys = {"games", "player_stats", "team_stats", "game_results"}
    for phase_key in phase_keys:
        phase_container = phase_results.get(phase_key)
        if not isinstance(phase_container, dict):
            raise ValueError(f"GameState invalid: phase_results.{phase_key} must be dict")
        if set(phase_container.keys()) != required_result_keys:
            raise ValueError(
                "GameState invalid: phase_results containers must contain games, player_stats, team_stats, game_results"
            )
        if not isinstance(phase_container.get("games"), list):
            raise ValueError(f"GameState invalid: phase_results.{phase_key}.games must be list")
        if not isinstance(phase_container.get("player_stats"), dict):
            raise ValueError(f"GameState invalid: phase_results.{phase_key}.player_stats must be dict")
        if not isinstance(phase_container.get("team_stats"), dict):
            raise ValueError(f"GameState invalid: phase_results.{phase_key}.team_stats must be dict")
        if not isinstance(phase_container.get("game_results"), dict):
            raise ValueError(f"GameState invalid: phase_results.{phase_key}.game_results must be dict")

    forbidden_postseason_keys = {"games", "player_stats", "team_stats", "game_results"}
    if any(key in postseason for key in forbidden_postseason_keys):
        raise ValueError("GameState invalid: postseason must not contain results containers")

    scores = _require_nested_container(cached_views, "scores", dict, "dict")
    if "latest_date" not in scores:
        raise ValueError("GameState invalid: cached_views.scores.latest_date is required")
    scores_games = scores.get("games")
    if not isinstance(scores_games, list):
        raise ValueError("GameState invalid: cached_views.scores.games must be list")

    schedule = _require_nested_container(cached_views, "schedule", dict, "dict")
    schedule_teams = schedule.get("teams")
    if not isinstance(schedule_teams, dict):
        raise ValueError("GameState invalid: cached_views.schedule.teams must be dict")

    meta = _require_nested_container(cached_views, "_meta", dict, "dict")
    meta_scores = _require_nested_container(meta, "scores", dict, "dict")
    if not isinstance(meta_scores.get("built_from_turn"), int):
        raise ValueError("GameState invalid: cached_views._meta.scores.built_from_turn must be int")
    meta_schedule = _require_nested_container(meta, "schedule", dict, "dict")
    built_from_turn_by_team = meta_schedule.get("built_from_turn_by_team")
    if not isinstance(built_from_turn_by_team, dict):
        raise ValueError("GameState invalid: cached_views._meta.schedule.built_from_turn_by_team must be dict")

    master_schedule = _require_nested_container(league, "master_schedule", dict, "dict")
    if not isinstance(master_schedule.get("games"), list):
        raise ValueError("GameState invalid: league.master_schedule.games must be list")
    if not isinstance(master_schedule.get("by_team"), dict):
        raise ValueError("GameState invalid: league.master_schedule.by_team must be dict")
    if not isinstance(master_schedule.get("by_date"), dict):
        raise ValueError("GameState invalid: league.master_schedule.by_date must be dict")
    if not isinstance(master_schedule.get("by_id"), dict):
        raise ValueError("GameState invalid: league.master_schedule.by_id must be dict")
