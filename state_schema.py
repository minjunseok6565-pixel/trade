from __future__ import annotations

from typing import Any, Dict

from state_modules.state_constants import DEFAULT_TRADE_RULES, _DEFAULT_TRADE_MARKET, _DEFAULT_TRADE_MEMORY

STATE_SCHEMA_VERSION = "3.0"
ALLOWED_PHASES = {"regular", "preseason", "play_in", "playoffs"}
NON_REGULAR_PHASES = {"preseason", "play_in", "playoffs"}
ALLOWED_TOP_LEVEL_KEYS = {
    "schema_version",
    "turn",
    "active_season_id",
    "season_history",
    "games",
    "player_stats",
    "team_stats",
    "game_results",
    "phase_results",
    "cached_views",
    "league",
    "teams",
    "players",
    "trade_agreements",
    "negotiations",
    "asset_locks",
    "trade_market",
    "trade_memory",
    "postseason",
    "_migrations",
}
ALLOWED_PHASE_RESULTS_KEYS = {"games", "player_stats", "team_stats", "game_results"}
ALLOWED_POSTSEASON_KEYS = {
    "field",
    "play_in",
    "playoffs",
    "champion",
    "my_team_id",
    "play_in_start_date",
    "play_in_end_date",
    "playoffs_start_date",
}
ALLOWED_LEAGUE_KEYS = {
    "season_year",
    "draft_year",
    "season_start",
    "current_date",
    "db_path",
    "master_schedule",
    "trade_rules",
    "last_gm_tick_date",
}
ALLOWED_MASTER_SCHEDULE_KEYS = {"games", "by_team", "by_date", "by_id"}
ALLOWED_CACHED_VIEWS_KEYS = {"scores", "schedule", "stats", "weekly_news", "playoff_news", "_meta"}
ALLOWED_CACHED_META_KEYS = {"scores", "schedule"}
ALLOWED_META_SCORES_KEYS = {"built_from_turn", "season_id"}
ALLOWED_META_SCHEDULE_KEYS = {"built_from_turn_by_team", "season_id"}
ALLOWED_SCORES_VIEW_KEYS = {"latest_date", "games"}
ALLOWED_SCHEDULE_VIEW_KEYS = {"teams"}
ALLOWED_STATS_VIEW_KEYS = {"leaders"}
ALLOWED_WEEKLY_NEWS_KEYS = {"last_generated_week_start", "items"}
ALLOWED_PLAYOFF_NEWS_KEYS = {"series_game_counts", "items"}
ALLOWED_SEASON_HISTORY_RECORD_KEYS = {"regular", "phase_results", "postseason", "archived_at_turn", "archived_at_date"}
ALLOWED_MIGRATIONS_KEYS = {
    "db_initialized",
    "db_initialized_db_path",
    "contracts_bootstrapped_seasons",
    "repo_integrity_validated",
    "repo_integrity_validated_db_path",
    "ingest_turn_backfilled",
}


def create_default_game_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "turn": 0,
        "active_season_id": None,
        "season_history": {},
        "games": [],  # 각 경기의 메타 데이터
        "player_stats": {},  # player_id -> 시즌 누적 스탯
        "team_stats": {},  # team_id -> 시즌 누적 팀 스탯(가공 팀 박스)
        "game_results": {},  # game_id -> 매치엔진 원본 결과(신규 엔진 기준)
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
            "play_in_start_date": None,
            "play_in_end_date": None,
            "playoffs_start_date": None,
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
        "_migrations": {
            "db_initialized": False,
            "db_initialized_db_path": None,
            "contracts_bootstrapped_seasons": {},
            "repo_integrity_validated": False,
            "repo_integrity_validated_db_path": None,
            "ingest_turn_backfilled": False,
        },
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


def _require_exact_keys(container: dict, allowed_keys: set[str], label: str) -> None:
    if set(container.keys()) != allowed_keys:
        raise ValueError(f"GameState invalid: {label} keys must be {sorted(allowed_keys)}")


def _validate_phase_results(container: dict, label: str) -> None:
    _require_exact_keys(container, ALLOWED_PHASE_RESULTS_KEYS, label)
    if not isinstance(container.get("games"), list):
        raise ValueError(f"GameState invalid: {label}.games must be list")
    if not isinstance(container.get("player_stats"), dict):
        raise ValueError(f"GameState invalid: {label}.player_stats must be dict")
    if not isinstance(container.get("team_stats"), dict):
        raise ValueError(f"GameState invalid: {label}.team_stats must be dict")
    if not isinstance(container.get("game_results"), dict):
        raise ValueError(f"GameState invalid: {label}.game_results must be dict")


def _validate_postseason_container(container: dict, label: str) -> None:
    _require_exact_keys(container, ALLOWED_POSTSEASON_KEYS, label)
    forbidden_keys = {"games", "player_stats", "team_stats", "game_results"}
    for value in container.values():
        if isinstance(value, dict) and any(key in value for key in forbidden_keys):
            raise ValueError(f"GameState invalid: {label} must not contain results containers")


def validate_game_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError("GameState invalid: state must be a dict")
        

    _require_exact_keys(state, ALLOWED_TOP_LEVEL_KEYS, "top-level")

    schema_version = state.get("schema_version")
    if schema_version != STATE_SCHEMA_VERSION:
        raise ValueError(f"GameState invalid: schema_version must be '{STATE_SCHEMA_VERSION}'")

    turn = state.get("turn")
    if not isinstance(turn, int) or turn < 0:
        raise ValueError("GameState invalid: turn must be int >= 0")

    _require_container(state, "games", list, "list")
    _require_container(state, "player_stats", dict, "dict")
    _require_container(state, "team_stats", dict, "dict")
    _require_container(state, "game_results", dict, "dict")

    active_season_id = state.get("active_season_id")
    if active_season_id is not None and not isinstance(active_season_id, str):
        raise ValueError("GameState invalid: active_season_id must be str or None")

    season_history = _require_container(state, "season_history", dict, "dict")
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
    migrations = _require_container(state, "_migrations", dict, "dict")

    _require_exact_keys(phase_results, NON_REGULAR_PHASES, "phase_results")
    for phase_key in NON_REGULAR_PHASES:
        phase_container = _require_nested_container(phase_results, phase_key, dict, "dict")
        _validate_phase_results(phase_container, f"phase_results.{phase_key}")

    _validate_postseason_container(postseason, "postseason")

    _require_exact_keys(cached_views, ALLOWED_CACHED_VIEWS_KEYS, "cached_views")
    scores = _require_nested_container(cached_views, "scores", dict, "dict")
    _require_exact_keys(scores, ALLOWED_SCORES_VIEW_KEYS, "cached_views.scores")
    if not isinstance(scores.get("games"), list):
        raise ValueError("GameState invalid: cached_views.scores.games must be list")

    schedule = _require_nested_container(cached_views, "schedule", dict, "dict")
    _require_exact_keys(schedule, ALLOWED_SCHEDULE_VIEW_KEYS, "cached_views.schedule")
    if not isinstance(schedule.get("teams"), dict):
        raise ValueError("GameState invalid: cached_views.schedule.teams must be dict")

    stats_view = _require_nested_container(cached_views, "stats", dict, "dict")
    _require_exact_keys(stats_view, ALLOWED_STATS_VIEW_KEYS, "cached_views.stats")

    weekly_news = _require_nested_container(cached_views, "weekly_news", dict, "dict")
    _require_exact_keys(weekly_news, ALLOWED_WEEKLY_NEWS_KEYS, "cached_views.weekly_news")
    if not isinstance(weekly_news.get("items"), list):
        raise ValueError("GameState invalid: cached_views.weekly_news.items must be list")

    playoff_news = _require_nested_container(cached_views, "playoff_news", dict, "dict")
    _require_exact_keys(playoff_news, ALLOWED_PLAYOFF_NEWS_KEYS, "cached_views.playoff_news")
    if not isinstance(playoff_news.get("series_game_counts"), dict):
        raise ValueError("GameState invalid: cached_views.playoff_news.series_game_counts must be dict")
    if not isinstance(playoff_news.get("items"), list):
        raise ValueError("GameState invalid: cached_views.playoff_news.items must be list")

    meta = _require_nested_container(cached_views, "_meta", dict, "dict")
    _require_exact_keys(meta, ALLOWED_CACHED_META_KEYS, "cached_views._meta")
    meta_scores = _require_nested_container(meta, "scores", dict, "dict")
    _require_exact_keys(meta_scores, ALLOWED_META_SCORES_KEYS, "cached_views._meta.scores")
    if not isinstance(meta_scores.get("built_from_turn"), int):
        raise ValueError("GameState invalid: cached_views._meta.scores.built_from_turn must be int")
    season_id = meta_scores.get("season_id")
    if season_id is not None and not isinstance(season_id, str):
        raise ValueError("GameState invalid: cached_views._meta.scores.season_id must be str or None")
    meta_schedule = _require_nested_container(meta, "schedule", dict, "dict")
    _require_exact_keys(meta_schedule, ALLOWED_META_SCHEDULE_KEYS, "cached_views._meta.schedule")
    built_from_turn_by_team = meta_schedule.get("built_from_turn_by_team")
    if not isinstance(built_from_turn_by_team, dict):
        raise ValueError("GameState invalid: cached_views._meta.schedule.built_from_turn_by_team must be dict")
    schedule_season_id = meta_schedule.get("season_id")
    if schedule_season_id is not None and not isinstance(schedule_season_id, str):
        raise ValueError("GameState invalid: cached_views._meta.schedule.season_id must be str or None")

    _require_exact_keys(league, ALLOWED_LEAGUE_KEYS, "league")
    master_schedule = _require_nested_container(league, "master_schedule", dict, "dict")
    _require_exact_keys(master_schedule, ALLOWED_MASTER_SCHEDULE_KEYS, "league.master_schedule")
    if not isinstance(master_schedule.get("games"), list):
        raise ValueError("GameState invalid: league.master_schedule.games must be list")
    if not isinstance(master_schedule.get("by_team"), dict):
        raise ValueError("GameState invalid: league.master_schedule.by_team must be dict")
    if not isinstance(master_schedule.get("by_date"), dict):
        raise ValueError("GameState invalid: league.master_schedule.by_date must be dict")
    if not isinstance(master_schedule.get("by_id"), dict):
        raise ValueError("GameState invalid: league.master_schedule.by_id must be dict")

        # -----------------------------
    # SSOT: active_season_id <-> league.season_year/draft_year 일치 강제
    # -----------------------------
    league_year = league.get("season_year")
    draft_year = league.get("draft_year")

    if league_year is not None and not isinstance(league_year, int):
        raise ValueError("GameState invalid: league.season_year must be int or None")
    if draft_year is not None and not isinstance(draft_year, int):
        raise ValueError("GameState invalid: league.draft_year must be int or None")

    # active_season_id와 league.season_year는 반드시 함께 존재하거나 둘 다 None이어야 한다.
    if (active_season_id is None) != (league_year is None):
        raise ValueError(
            "GameState invalid: active_season_id and league.season_year must be both set or both None"
        )

    # season_year가 None이면 draft_year도 None이어야 한다(부분 초기화 금지).
    if league_year is None:
        if draft_year is not None:
            raise ValueError("GameState invalid: league.draft_year must be None when league.season_year is None")
    else:
        # season_year가 설정되면 draft_year는 필수이며 season_year+1 이어야 한다.
        if draft_year is None:
            raise ValueError("GameState invalid: league.draft_year must be set when league.season_year is set")
        if int(draft_year) != int(league_year) + 1:
            raise ValueError("GameState invalid: league.draft_year must equal league.season_year + 1")

    # active_season_id가 설정된 경우: active의 연도(YYYY)가 league.season_year와 동일해야 한다.
    if active_season_id is not None:
        # 포맷: 'YYYY-YY' (예: '2026-27')
        if "-" not in active_season_id:
            raise ValueError("GameState invalid: active_season_id must be like 'YYYY-YY'")
        try:
            active_year = int(str(active_season_id).split("-", 1)[0])
        except Exception:
            raise ValueError("GameState invalid: active_season_id must be like 'YYYY-YY'")
        if int(league_year) != int(active_year):
            raise ValueError("GameState invalid: league.season_year must match active_season_id year")

        # -----------------------------
        # master_schedule 시즌 일치 강제
        # - games가 비어있으면(아직 스케줄 생성 전) 검사는 스킵한다.
        # - games가 존재하면, season_id가 있는 모든 엔트리는 active_season_id와 일치해야 한다.
        # -----------------------------
        ms_games = master_schedule.get("games") or []
        if isinstance(ms_games, list) and ms_games:
            for i, g in enumerate(ms_games):
                if not isinstance(g, dict):
                    continue
                sid = g.get("season_id")
                if sid is None:
                    continue
                if str(sid) != str(active_season_id):
                    raise ValueError(
                        f"GameState invalid: league.master_schedule.games[{i}].season_id must match active_season_id"
                    )

    for season_id, record in season_history.items():
        if not isinstance(season_id, str):
            raise ValueError("GameState invalid: season_history keys must be str")
        if not isinstance(record, dict):
            raise ValueError("GameState invalid: season_history records must be dict")
        _require_exact_keys(record, ALLOWED_SEASON_HISTORY_RECORD_KEYS, f"season_history.{season_id}")
        regular = _require_nested_container(record, "regular", dict, "dict")
        _validate_phase_results(regular, f"season_history.{season_id}.regular")
        record_phase_results = _require_nested_container(record, "phase_results", dict, "dict")
        _require_exact_keys(record_phase_results, NON_REGULAR_PHASES, f"season_history.{season_id}.phase_results")
        for phase_key in NON_REGULAR_PHASES:
            phase_container = _require_nested_container(record_phase_results, phase_key, dict, "dict")
            _validate_phase_results(phase_container, f"season_history.{season_id}.phase_results.{phase_key}")
        record_postseason = _require_nested_container(record, "postseason", dict, "dict")
        _validate_postseason_container(record_postseason, f"season_history.{season_id}.postseason")
        archived_at_turn = record.get("archived_at_turn")
        if not isinstance(archived_at_turn, int):
            raise ValueError("GameState invalid: season_history.archived_at_turn must be int")
        archived_at_date = record.get("archived_at_date")
        if archived_at_date is not None and not isinstance(archived_at_date, str):
            raise ValueError("GameState invalid: season_history.archived_at_date must be str or None")

    _require_exact_keys(migrations, ALLOWED_MIGRATIONS_KEYS, "_migrations")


if __name__ == "__main__":
    s = create_default_game_state()
    validate_game_state(s)
    print("OK")
