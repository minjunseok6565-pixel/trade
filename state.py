from __future__ import annotations

from copy import deepcopy
from datetime import date
from threading import RLock
from typing import Any, Callable, Optional

from config import ALL_TEAM_IDS, INITIAL_SEASON_YEAR, SEASON_START_DAY, SEASON_START_MONTH
from schema import season_id_from_year as _season_id_from_year
from state_modules.state_constants import (
    DEFAULT_TRADE_RULES,
    _ALLOWED_PHASES,
    _ALLOWED_SCHEDULE_STATUSES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
    _META_PLAYER_KEYS,
)
from state_modules.state_store import _get_state, reset_state_for_dev as _reset_state_for_dev
from state_schema import validate_game_state

__all__ = [
    "DEFAULT_TRADE_RULES",
    "_ALLOWED_PHASES",
    "_ALLOWED_SCHEDULE_STATUSES",
    "_DEFAULT_TRADE_MARKET",
    "_DEFAULT_TRADE_MEMORY",
    "_META_PLAYER_KEYS",
    "startup_init_state",
    "validate_state",
    "export_workflow_state",
    "export_full_state_snapshot",
    "get_current_date",
    "get_current_date_as_date",
    "set_current_date",
    "get_db_path",
    "set_db_path",
    "set_last_gm_tick_date",
    "get_league_context_snapshot",
    "initialize_master_schedule_if_needed",
    "ensure_schedule_for_active_season",
    "start_new_season",
    "get_schedule_summary",
    "get_active_season_id",
    "set_active_season_id",
    "ingest_game_result",
    "get_postseason_snapshot",
    "postseason_set_field",
    "postseason_set_play_in",
    "postseason_set_playoffs",
    "postseason_set_champion",
    "postseason_set_my_team_id",
    "postseason_set_dates",
    "postseason_reset",
    "get_cached_stats_snapshot",
    "set_cached_stats_snapshot",
    "get_cached_weekly_news_snapshot",
    "set_cached_weekly_news_snapshot",
    "get_cached_playoff_news_snapshot",
    "set_cached_playoff_news_snapshot",
    "export_trade_context_snapshot",
    "export_trade_assets_snapshot",
    "trade_agreements_get",
    "trade_agreements_set",
    "asset_locks_get",
    "asset_locks_set",
    "negotiations_get",
    "negotiations_set",
    "negotiation_session_get",
    "negotiation_session_put",
    "negotiation_session_update",
    "trade_market_get",
    "trade_market_set",
    "trade_memory_get",
    "trade_memory_set",
    "players_get",
    "players_set",
    "teams_get",
    "teams_set",
    "reset_state_for_dev",
]


def _s() -> dict:
    return _get_state()


# NOTE: Single-process safety. Serialize all negotiation session mutations
# to prevent lost updates (read-modify-write races).
_NEGOTIATIONS_LOCK = RLock()


def _season_year_from_season_id(season_id: str) -> int:
    """season_id 포맷 'YYYY-YY'에서 시작 연도(YYYY)를 int로 반환한다."""
    if not isinstance(season_id, str) or "-" not in season_id:
        raise ValueError(f"Invalid season_id format: {season_id!r}")
    head = season_id.split("-", 1)[0].strip()
    try:
        year = int(head)
    except Exception as exc:
        raise ValueError(f"Invalid season_id format: {season_id!r}") from exc
    if year <= 0:
        raise ValueError(f"Invalid season_id year: {season_id!r}")
    return year


def _season_id_for_year(season_year: int) -> str:
    return str(_season_id_from_year(int(season_year)))


def _ensure_db_path_in_state() -> str:
    """contracts.offseason이 요구하는 league.db_path를 항상 채워 넣는다."""
    league = _s().get("league")
    if not isinstance(league, dict):
        raise ValueError("GameState invalid: league must be dict")
    db_path = league.get("db_path")
    if db_path:
        return str(db_path)
    # 기존 코드베이스의 기본 정책과 동일하게 league.db를 기본값으로 둔다.
    league["db_path"] = "league.db"
    return "league.db"


def _clear_master_schedule(league: dict) -> None:
    ms = league.get("master_schedule")
    if not isinstance(ms, dict):
        raise ValueError("GameState invalid: league.master_schedule must be dict")
    ms["games"] = []
    ms["by_team"] = {}
    ms["by_date"] = {}
    ms["by_id"] = {}

def _ensure_draft_picks_seeded_for_season_year(season_year: int) -> None:
    """스케줄 생성/시즌 시작을 위해 필요한 draft_picks seed를 보장한다."""
    league = _s()["league"]
    _ensure_db_path_in_state()

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

    from league_repo import LeagueRepo

    db_path = str(_s()["league"]["db_path"])
    draft_year = int(season_year) + 1
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_draft_picks_seeded(draft_year, list(ALL_TEAM_IDS), years_ahead=years_ahead)


def ensure_schedule_for_active_season(*, force: bool = False) -> None:
    """현재 active_season_id에 맞는 master_schedule을 보장한다(시즌 전환은 하지 않음)."""
    state = _s()
    league = state.get("league")
    if not isinstance(league, dict):
        raise ValueError("GameState invalid: league must be dict")

    active = state.get("active_season_id")
    if active is None:
        raise ValueError(
            "active_season_id is None. "
            "Call state.start_new_season(...) or state.set_active_season_id(...) first."
        )
    if not isinstance(active, str):
        raise ValueError("GameState invalid: active_season_id must be str")

    active_year = _season_year_from_season_id(active)

    # SSOT 동기화(미리 채움): league.season_year/draft_year는 active와 일치해야 한다.
    league_year = league.get("season_year")
    if league_year is None:
        league["season_year"] = int(active_year)
        league_year = int(active_year)
    try:
        league_year_i = int(league_year)
    except Exception as exc:
        raise ValueError("GameState invalid: league.season_year must be int") from exc
    if league_year_i != int(active_year):
        raise ValueError(
            f"Season mismatch: league.season_year={league_year_i} != active_season_id={active}. "
            "Use state.start_new_season(...) to transition seasons."
        )

    draft_year = league.get("draft_year")
    if draft_year is None:
        league["draft_year"] = int(league_year_i) + 1
    else:
        try:
            draft_year_i = int(draft_year)
        except Exception as exc:
            raise ValueError("GameState invalid: league.draft_year must be int") from exc
        if draft_year_i != int(league_year_i) + 1:
            raise ValueError("GameState invalid: league.draft_year must equal league.season_year + 1")

    ms = league.get("master_schedule")
    if not isinstance(ms, dict):
        raise ValueError("GameState invalid: league.master_schedule must be dict")

    games = ms.get("games") or []
    rebuild = False
    if force:
        rebuild = True
    elif not isinstance(games, list) or len(games) == 0:
        rebuild = True
    else:
        g0 = games[0] if games else None
        if isinstance(g0, dict):
            sid = g0.get("season_id")
            if sid is not None and str(sid) != str(active):
                rebuild = True

    if rebuild:
        # schedule 생성에 필요한 부수효과는 facade가 수행한다.
        from state_modules.state_cap import _apply_cap_model_for_season
        from state_modules import state_bootstrap, state_schedule

        _ensure_db_path_in_state()
        _apply_cap_model_for_season(league, int(active_year))
        _ensure_draft_picks_seeded_for_season_year(int(active_year))

        season_start = date(int(active_year), SEASON_START_MONTH, SEASON_START_DAY)
        built = state_schedule.build_master_schedule(season_year=int(active_year), season_start=season_start, rng_seed=None)

        ms["games"] = built["games"]
        ms["by_team"] = built["by_team"]
        ms["by_date"] = built["by_date"]
        ms["by_id"] = built["by_id"]

        league["season_start"] = season_start.isoformat()
        trade_deadline = date(int(active_year) + 1, 2, 5)
        league["trade_rules"]["trade_deadline"] = trade_deadline.isoformat()
        league["current_date"] = None
        league["last_gm_tick_date"] = None

        # schedule 생성 직후의 계약 bootstrap 체크포인트(once per season)
        state_bootstrap.ensure_contracts_bootstrapped_after_schedule_creation_once(state)

    # 인덱스 보정
    from state_modules import state_schedule
    state_schedule.ensure_master_schedule_indices(ms)

    validate_game_state(state)


def start_new_season(
    season_year: int,
    *,
    rebuild_schedule: bool = True,
    run_offseason: bool = True,
) -> dict:
    """'시즌 전환'의 유일한 공식 API."""
    state = _s()
    league = state.get("league")
    if not isinstance(league, dict):
        raise ValueError("GameState invalid: league must be dict")

    target_year = int(season_year)
    if target_year <= 0:
        raise ValueError("season_year must be positive int")

    _ensure_db_path_in_state()

    prev_active = state.get("active_season_id")
    prev_year = None
    if prev_active is not None:
        prev_year = _season_year_from_season_id(str(prev_active))

    offseason_result = None
    if run_offseason and prev_year is not None and int(prev_year) != int(target_year):
        from contracts.offseason import process_offseason

        offseason_result = process_offseason(
            state,
            from_season_year=int(prev_year),
            to_season_year=int(target_year),
            decision_policy=None,
            draft_pick_order_by_pick_id=None,
        )

    next_sid = _season_id_for_year(int(target_year))
    set_active_season_id(next_sid)

    # SSOT 동기화
    league = state["league"]
    league["season_year"] = int(target_year)
    league["draft_year"] = int(target_year) + 1

    if rebuild_schedule:
        ensure_schedule_for_active_season(force=True)
    else:
        _clear_master_schedule(league)
        validate_game_state(state)

    return {
        "ok": True,
        "from_season_year": prev_year,
        "to_season_year": int(target_year),
        "offseason": offseason_result,
        "active_season_id": state.get("active_season_id"),
    }

def _require_active_season_id_matches(season_id: str) -> str:
    """ingest 등 공개 동작에서 'active season' 불일치를 fail-fast로 차단한다."""
    active = _s().get("active_season_id")
    if active is None:
        raise ValueError(
            "GameState invalid: active_season_id is None. "
            "Call state.set_active_season_id(<season_id>) before ingest."
        )
    if str(active) != str(season_id):
        raise ValueError(
            f"Season mismatch: game_result season_id='{season_id}' != active_season_id='{active}'. "
            "Switch seasons explicitly via state.set_active_season_id(<season_id>)."
        )
    return str(active)


def startup_init_state() -> None:
    validate_game_state(_s())
    from state_modules import state_bootstrap
    from state_modules import state_migrations

    state_bootstrap.ensure_db_initialized_and_seeded(_s())

    # SSOT 초기화: active_season_id / league.season_year가 비어있으면 INITIAL 시즌을 명시적으로 시작한다.
    if _s().get("active_season_id") is None and _s().get("league", {}).get("season_year") is None:
        start_new_season(INITIAL_SEASON_YEAR, rebuild_schedule=True, run_offseason=False)
    else:
        # 불완전 저장/레거시 상태 방어: 한쪽만 존재하면 다른 쪽을 최소 보정(아카이브/리셋 없음)
        active = _s().get("active_season_id")
        league_year = _s().get("league", {}).get("season_year")
        if active is None and league_year is not None:
            sid = _season_id_for_year(int(league_year))
            _s()["active_season_id"] = sid
            _s()["league"]["draft_year"] = int(league_year) + 1
        elif active is not None and league_year is None:
            ay = _season_year_from_season_id(str(active))
            _s()["league"]["season_year"] = int(ay)
            _s()["league"]["draft_year"] = int(ay) + 1

        ensure_schedule_for_active_season(force=False)

    state_bootstrap.ensure_cap_model_populated_if_needed(_s())
    state_bootstrap.validate_repo_integrity_once_startup(_s())
    state_migrations.ensure_ingest_turn_backfilled_once_startup(_s())
    validate_game_state(_s())


def validate_state() -> None:
    validate_game_state(_s())


def export_workflow_state(
    exclude_keys: tuple[str, ...] = (
        "draft_picks",
        "swap_rights",
        "fixed_assets",
        "transactions",
        "contracts",
        "player_contracts",
        "active_contract_id_by_player",
        "free_agents",
        "gm_profiles",
    ),
) -> dict:
    snapshot = deepcopy(_s())
    for key in exclude_keys:
        snapshot.pop(key, None)
    return snapshot


def export_full_state_snapshot() -> dict:
    return deepcopy(_s())


def get_current_date() -> str | None:
    return _s()["league"]["current_date"]


def get_current_date_as_date():
    league = _s()["league"]
    current_date = league.get("current_date")
    if current_date:
        try:
            return date.fromisoformat(str(current_date))
        except ValueError:
            pass
    season_start = league.get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass
    return date.today()


def set_current_date(date_str: str | None) -> None:
    _s()["league"]["current_date"] = date_str
    validate_game_state(_s())


def get_db_path() -> str:
    return str(_s()["league"]["db_path"] or "league.db")


def set_db_path(path: str) -> None:
    _s()["league"]["db_path"] = str(path)
    validate_game_state(_s())


def set_last_gm_tick_date(date_str: str | None) -> None:
    _s()["league"]["last_gm_tick_date"] = date_str
    validate_game_state(_s())


def get_league_context_snapshot() -> dict:
    return {
        "season_year": _s()["league"]["season_year"],
        "trade_rules": deepcopy(_s()["league"]["trade_rules"]),
        "current_date": _s()["league"]["current_date"],
        "season_start": _s()["league"]["season_start"],
    }


def initialize_master_schedule_if_needed(force: bool = False) -> None:
    ensure_schedule_for_active_season(force=force)


def get_schedule_summary() -> dict:
    from state_modules import state_schedule

    ensure_schedule_for_active_season(force=False)
    ms = _s()["league"]["master_schedule"]
    return state_schedule.get_schedule_summary(ms)


def get_active_season_id() -> str | None:
    return _s()["active_season_id"]


def set_active_season_id(next_season_id: str) -> None:
    old = _s()["active_season_id"]
    if old is not None:
        _s()["season_history"][str(old)] = {
            "regular": deepcopy(
                {
                    "games": _s()["games"],
                    "player_stats": _s()["player_stats"],
                    "team_stats": _s()["team_stats"],
                    "game_results": _s()["game_results"],
                }
            ),
            "phase_results": deepcopy(_s()["phase_results"]),
            "postseason": deepcopy(_s()["postseason"]),
            "archived_at_turn": int(_s()["turn"]),
            "archived_at_date": _s()["league"]["current_date"],
        }
    _s()["games"] = []
    _s()["player_stats"] = {}
    _s()["team_stats"] = {}
    _s()["game_results"] = {}
    _s()["phase_results"] = {
        "preseason": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        "play_in": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        "playoffs": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
    }
    _s()["postseason"] = {
        "field": None,
        "play_in": None,
        "playoffs": None,
        "champion": None,
        "my_team_id": None,
        "play_in_start_date": None,
        "play_in_end_date": None,
        "playoffs_start_date": None,
    }
    _s()["active_season_id"] = str(next_season_id)
    _s()["cached_views"] = {
        "scores": {"latest_date": None, "games": []},
        "schedule": {"teams": {}},
        "stats": {"leaders": None},
        "weekly_news": {"last_generated_week_start": None, "items": []},
        "playoff_news": {"series_game_counts": {}, "items": []},
        "_meta": {
            "scores": {"built_from_turn": -1, "season_id": None},
            "schedule": {"built_from_turn_by_team": {}, "season_id": None},
        },
    }

    # SSOT 동기화: league.season_year/draft_year를 active와 맞춘다.
    next_year = _season_year_from_season_id(str(next_season_id))
    _s()["league"]["season_year"] = int(next_year)
    _s()["league"]["draft_year"] = int(next_year) + 1

    # 기존 master_schedule은 다른 시즌 것일 수 있으므로 반드시 비워서 stale 제거
    _clear_master_schedule(_s()["league"])

    validate_game_state(_s())


def ingest_game_result(
    game_result: dict,
    game_date: str | None = None,
) -> dict:
    from state_modules import state_results
    from state_modules import state_schedule

    state_results.validate_v2_game_result(game_result)
    _s()["turn"] = int(_s().get("turn", 0) or 0) + 1
    game = game_result["game"]
    season_id = str(game["season_id"])
    _require_active_season_id_matches(season_id)
    phase = str(game["phase"])
    if phase == "regular":
        container = _s()
    elif phase in {"preseason", "play_in", "playoffs"}:
        container = _s()["phase_results"][phase]
    else:
        raise ValueError("invalid phase")

    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])
    final = game_result["final"]
    game_date_str = str(game_date) if game_date else str(game["date"])
    game_id = str(game["game_id"])
    home_score = int(final[home_id])
    away_score = int(final[away_id])
    game_obj = {
        "game_id": game_id,
        "date": game_date_str,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "status": "final",
        "is_overtime": int(game.get("overtime_periods", 0) or 0) > 0,
        "phase": phase,
        "season_id": season_id,
        "schema_version": "2.0",
        "ingest_turn": int(_s()["turn"]),
    }

    container["games"].append(game_obj)
    container["game_results"][game_id] = game_result

    teams = game_result["teams"]
    season_player_stats = container["player_stats"]
    season_team_stats = container["team_stats"]
    for tid in (home_id, away_id):
        team_game = teams[tid]
        state_results._accumulate_team_game_result(tid, team_game, season_team_stats)
        rows = team_game.get("players") or []
        if not isinstance(rows, list):
            raise ValueError(f"GameResultV2 invalid: teams.{tid}.players must be list")
        state_results._accumulate_player_rows(rows, season_player_stats)

    ms = _s()["league"]["master_schedule"]
    state_schedule.mark_master_schedule_game_final(
        ms,
        game_id=game_id,
        game_date_str=game_date_str,
        home_id=home_id,
        away_id=away_id,
        home_score=home_score,
        away_score=away_score,
    )

    _s()["cached_views"]["_meta"]["scores"]["built_from_turn"] = -1
    _s()["cached_views"]["_meta"]["schedule"]["built_from_turn_by_team"] = {}
    _s()["cached_views"]["stats"]["leaders"] = None
    validate_game_state(_s())
    return game_obj


def validate_v2_game_result(game_result: dict) -> None:
    from state_modules import state_results

    return state_results.validate_v2_game_result(game_result)


def validate_master_schedule_entry(entry: dict, *, path: str = "master_schedule.entry") -> None:
    from state_modules import state_schedule

    return state_schedule.validate_master_schedule_entry(entry, path=path)


def get_postseason_snapshot() -> dict:
    return deepcopy(_s()["postseason"])


def postseason_set_field(field) -> None:
    _s()["postseason"]["field"] = deepcopy(field)
    validate_game_state(_s())


def postseason_set_play_in(state) -> None:
    _s()["postseason"]["play_in"] = deepcopy(state)
    validate_game_state(_s())


def postseason_set_playoffs(state) -> None:
    _s()["postseason"]["playoffs"] = deepcopy(state)
    validate_game_state(_s())


def postseason_set_champion(team_id) -> None:
    _s()["postseason"]["champion"] = team_id
    validate_game_state(_s())


def postseason_set_my_team_id(team_id) -> None:
    _s()["postseason"]["my_team_id"] = team_id
    validate_game_state(_s())


def postseason_set_dates(play_in_start, play_in_end, playoffs_start) -> None:
    _s()["postseason"]["play_in_start_date"] = play_in_start
    _s()["postseason"]["play_in_end_date"] = play_in_end
    _s()["postseason"]["playoffs_start_date"] = playoffs_start
    validate_game_state(_s())


def postseason_reset() -> None:
    _s()["postseason"] = {
        "field": None,
        "play_in": None,
        "playoffs": None,
        "champion": None,
        "my_team_id": None,
        "play_in_start_date": None,
        "play_in_end_date": None,
        "playoffs_start_date": None,
    }
    validate_game_state(_s())


def get_cached_stats_snapshot() -> dict:
    return deepcopy(_s()["cached_views"]["stats"])


def set_cached_stats_snapshot(stats_cache: dict) -> None:
    _s()["cached_views"]["stats"] = deepcopy(stats_cache)
    validate_game_state(_s())


def get_cached_weekly_news_snapshot() -> dict:
    return deepcopy(_s()["cached_views"]["weekly_news"])


def set_cached_weekly_news_snapshot(cache: dict) -> None:
    _s()["cached_views"]["weekly_news"] = deepcopy(cache)
    validate_game_state(_s())


def get_cached_playoff_news_snapshot() -> dict:
    return deepcopy(_s()["cached_views"]["playoff_news"])


def set_cached_playoff_news_snapshot(cache: dict) -> None:
    _s()["cached_views"]["playoff_news"] = deepcopy(cache)
    validate_game_state(_s())


def export_trade_context_snapshot() -> dict:
    return deepcopy(
        {
            "players": _s()["players"],
            "teams": _s()["teams"],
            "asset_locks": _s()["asset_locks"],
            "league": get_league_context_snapshot(),
            "my_team_id": _s()["postseason"]["my_team_id"],
        }
    )


def export_trade_assets_snapshot() -> dict:
    from league_repo import LeagueRepo

    with LeagueRepo(get_db_path()) as repo:
        return deepcopy(repo.get_trade_assets_snapshot() or {})


def ensure_cap_model_populated_if_needed() -> None:
    from state_modules import state_bootstrap

    state_bootstrap.ensure_cap_model_populated_if_needed(_s())
    validate_game_state(_s())


def ensure_player_ids_normalized(*, allow_legacy_numeric: bool = True) -> dict:
    from state_modules import state_bootstrap

    report = state_bootstrap.ensure_player_ids_normalized(_s(), allow_legacy_numeric=allow_legacy_numeric)
    validate_game_state(_s())
    return report


def ensure_trade_state_keys() -> None:
    from state_modules import state_trade

    state_trade.ensure_trade_state_keys(_s())
    validate_game_state(_s())


def trade_agreements_get() -> dict:
    return deepcopy(_s().get("trade_agreements") or {})


def trade_agreements_set(value: dict) -> None:
    _s()["trade_agreements"] = deepcopy(value)
    validate_game_state(_s())


def asset_locks_get() -> dict:
    return deepcopy(_s().get("asset_locks") or {})


def asset_locks_set(value: dict) -> None:
    _s()["asset_locks"] = deepcopy(value)
    validate_game_state(_s())


def negotiations_get() -> dict:
    with _NEGOTIATIONS_LOCK:
        return deepcopy(_s().get("negotiations") or {})


def negotiations_set(value: dict) -> None:
    with _NEGOTIATIONS_LOCK:
        _s()["negotiations"] = deepcopy(value)
        validate_game_state(_s())


def negotiation_session_get(session_id: str) -> Optional[dict]:
    """Return a snapshot (deep copy) of one negotiation session, or None."""
    with _NEGOTIATIONS_LOCK:
        negotiations = _s().get("negotiations") or {}
        session = negotiations.get(session_id)
        return deepcopy(session) if session is not None else None


def negotiation_session_put(session_id: str, session: dict) -> None:
    """Upsert one negotiation session (deep-copied) and validate."""
    with _NEGOTIATIONS_LOCK:
        negotiations = _s().setdefault("negotiations", {})
        negotiations[session_id] = deepcopy(session)
        validate_game_state(_s())


def negotiation_session_update(session_id: str, mutator: Callable[[dict], None]) -> dict:
    """Atomically read-modify-write a single session under lock and validate.

    Raises KeyError if the session_id does not exist.
    Returns a snapshot (deep copy) of the updated session.
    """
    with _NEGOTIATIONS_LOCK:
        negotiations = _s().setdefault("negotiations", {})
        if session_id not in negotiations:
            raise KeyError(session_id)

        working = deepcopy(negotiations[session_id])
        mutator(working)
        negotiations[session_id] = working
        validate_game_state(_s())
        return deepcopy(working)


def trade_market_get() -> dict:
    return deepcopy(_s().get("trade_market") or {})


def trade_market_set(value: dict) -> None:
    _s()["trade_market"] = deepcopy(value)
    validate_game_state(_s())


def trade_memory_get() -> dict:
    return deepcopy(_s().get("trade_memory") or {})


def trade_memory_set(value: dict) -> None:
    _s()["trade_memory"] = deepcopy(value)
    validate_game_state(_s())


def players_get() -> dict:
    return deepcopy(_s().get("players") or {})


def players_set(value: dict) -> None:
    _s()["players"] = deepcopy(value)
    validate_game_state(_s())


def teams_get() -> dict:
    return deepcopy(_s().get("teams") or {})


def teams_set(value: dict) -> None:
    _s()["teams"] = deepcopy(value)
    validate_game_state(_s())


def reset_state_for_dev() -> None:
    _reset_state_for_dev()
