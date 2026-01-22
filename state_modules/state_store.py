from __future__ import annotations

from typing import Any, Dict

from config import (
    CAP_ANNUAL_GROWTH_RATE,
    CAP_BASE_FIRST_APRON,
    CAP_BASE_SALARY_CAP,
    CAP_BASE_SECOND_APRON,
    CAP_BASE_SEASON_YEAR,
    CAP_ROUND_UNIT,
)

DEFAULT_TRADE_RULES: Dict[str, Any] = {
    "trade_deadline": None,
    "salary_cap": 0.0,
    "first_apron": 0.0,
    "second_apron": 0.0,
    "cap_auto_update": True,
    "cap_base_season_year": CAP_BASE_SEASON_YEAR,
    "cap_base_salary_cap": CAP_BASE_SALARY_CAP,
    "cap_base_first_apron": CAP_BASE_FIRST_APRON,
    "cap_base_second_apron": CAP_BASE_SECOND_APRON,
    "cap_annual_growth_rate": CAP_ANNUAL_GROWTH_RATE,
    "cap_round_unit": CAP_ROUND_UNIT,
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

_ALLOWED_SCHEDULE_STATUSES = {"scheduled", "final", "in_progress", "canceled"}

_DEFAULT_TRADE_MARKET: Dict[str, Any] = {
    "last_tick_date": None,
    "listings": {},
    "threads": {},
    "cooldowns": {},
    "events": [],
}

_DEFAULT_TRADE_MEMORY: Dict[str, Any] = {
    "relationships": {},
}

_ALLOWED_PHASES = {"regular", "play_in", "playoffs", "preseason"}

_META_PLAYER_KEYS = {"PlayerID", "TeamID", "Name", "Pos", "Position"}

# -------------------------------------------------------------------------
# 1. 전역 GAME_STATE 및 스케줄/리그 상태 유틸
# -------------------------------------------------------------------------
GAME_STATE: Dict[str, Any] = {
    "schema_version": "2.0",
    "turn": 0,
    "games": [],  # 각 경기의 메타 데이터
    "player_stats": {},  # player_id -> 시즌 누적 스탯
    "team_stats": {},  # team_id -> 시즌 누적 팀 스탯(가공 팀 박스)
    "game_results": {},  # game_id -> 매치엔진 원본 결과(신규 엔진 기준)
    "active_season_id": None,  # 현재 누적이 쌓이는 season_id (예: "2025-26")
    "season_history": {},  # season_id -> {games, player_stats, team_stats, game_results}
    "_migrations": {},
    "cached_views": {
        "scores": {
            "latest_date": None,
            "games": []  # 최근 경기일자 기준 경기 리스트
        },
        "schedule": {
            "teams": {}  # team_id -> {past_games: [], upcoming_games: []}
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
    "trade_agreements": {},  # deal_id -> committed deal data
    "negotiations": {},  # session_id -> negotiation sessions
    "asset_locks": {},  # asset_key -> {deal_id, expires_at}
    "trade_market": {
        "last_tick_date": None,
        "listings": {},
        "threads": {},
        "cooldowns": {},
        "events": [],
    },
    "trade_memory": {
        "relationships": {},
    },
}
