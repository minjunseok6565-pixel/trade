from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Dict, Optional

from config import ALL_TEAM_IDS
from state_modules.state_cap import _apply_cap_model_for_season
from state_modules.state_migrations import ensure_ingest_turn_backfilled, normalize_player_ids
from state_modules.state_results import (
    build_game_obj_from_result,
    validate_v2_game_result,
    _accumulate_player_rows,
    _accumulate_team_game_result,
)
from state_modules.state_schedule import (
    build_master_schedule,
    ensure_master_schedule_indices,
    get_schedule_summary as _get_schedule_summary,
    mark_master_schedule_game_final,
    validate_master_schedule_entry,
)
from state_modules.state_store import (
    DEFAULT_TRADE_RULES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
    _get_state,
)
from state_modules.state_trade import ensure_trade_state_keys as _ensure_trade_state_keys
from state_modules.state_utils import _require_dict, _require_list
from state_modules.state_views import get_scores_view as _get_scores_view
from state_modules.state_views import get_team_schedule_view as _get_team_schedule_view
from state_schema import (
    NON_REGULAR_PHASES,
    create_default_cached_views,
    create_default_phase_results,
    create_default_postseason_state,
    validate_game_state,
)

_DB_INITIALIZED_BY_PATH: dict[str, bool] = {}
_REPO_INTEGRITY_VALIDATED_BY_PATH: dict[str, bool] = {}
_CONTRACTS_BOOTSTRAPPED_SEASONS: set[str] = set()


def startup_init_state() -> None:
    state = _get_state()
    validate_game_state(state)
    ensure_db_initialized_and_seeded()
    validate_repo_integrity_once_startup()
    ensure_roster_cache_initialized_once_startup()
    ensure_cap_model_populated_if_needed()
    validate_game_state(state)


def validate_state() -> None:
    validate_game_state(_get_state())


def export_state_snapshot() -> dict:
    return deepcopy(_get_state())


def replace_state_snapshot(snapshot: dict) -> None:
    validate_game_state(snapshot)
    state = _get_state()
    state.clear()
    state.update(deepcopy(snapshot))


def export_workflow_state() -> dict:
    state_snapshot = deepcopy(_get_state())
    for key in (
        "draft_picks",
        "swap_rights",
        "fixed_assets",
        "transactions",
        "contracts",
        "player_contracts",
        "active_contract_id_by_player",
        "free_agents",
        "gm_profiles",
        "players",
        "teams",
    ):
        state_snapshot.pop(key, None)

    league = state_snapshot.get("league")
    if isinstance(league, dict):
        master_schedule = league.get("master_schedule")
        if isinstance(master_schedule, dict):
            master_schedule.pop("games", None)
            master_schedule.pop("by_id", None)

    return state_snapshot


def export_trade_context_snapshot() -> dict:
    state = _get_state()
    league = state.get("league") or {}
    return {
        "players": deepcopy(state.get("players") or {}),
        "teams": deepcopy(state.get("teams") or {}),
        "asset_locks": deepcopy(state.get("asset_locks") or {}),
        "negotiations": deepcopy(state.get("negotiations") or {}),
        "trade_agreements": deepcopy(state.get("trade_agreements") or {}),
        "trade_rules": deepcopy(league.get("trade_rules") or {}),
        "current_date": league.get("current_date"),
    }


def get_trade_rules_snapshot() -> dict:
    league = _get_state().get("league") or {}
    return deepcopy(league.get("trade_rules") or {})


def trade_get_asset_locks() -> dict:
    return deepcopy(_get_state().get("asset_locks") or {})


def trade_set_asset_lock(asset_id: str, lock_obj: dict) -> None:
    state = _get_state()
    locks = state.get("asset_locks")
    if not isinstance(locks, dict):
        locks = {}
        state["asset_locks"] = locks
    locks[asset_id] = lock_obj
    validate_game_state(state)


def trade_remove_asset_lock(asset_id: str) -> None:
    state = _get_state()
    locks = state.get("asset_locks")
    if isinstance(locks, dict):
        locks.pop(asset_id, None)
    validate_game_state(state)


def trade_get_agreement(deal_id: str) -> dict | None:
    agreements = _get_state().get("trade_agreements")
    if not isinstance(agreements, dict):
        return None
    entry = agreements.get(deal_id)
    return deepcopy(entry) if isinstance(entry, dict) else None


def trade_set_agreement(deal_id: str, entry: dict) -> None:
    state = _get_state()
    agreements = state.get("trade_agreements")
    if not isinstance(agreements, dict):
        agreements = {}
        state["trade_agreements"] = agreements
    agreements[deal_id] = entry
    validate_game_state(state)


def trade_get_agreements_snapshot() -> dict:
    return deepcopy(_get_state().get("trade_agreements") or {})


def negotiation_put(session_id: str, obj: dict) -> None:
    state = _get_state()
    sessions = state.get("negotiations")
    if not isinstance(sessions, dict):
        sessions = {}
        state["negotiations"] = sessions
    sessions[session_id] = obj
    validate_game_state(state)


def negotiation_get(session_id: str) -> dict | None:
    sessions = _get_state().get("negotiations")
    if not isinstance(sessions, dict):
        return None
    entry = sessions.get(session_id)
    return deepcopy(entry) if isinstance(entry, dict) else None


def get_league_snapshot() -> dict:
    return deepcopy(_get_state().get("league") or {})


def get_players_snapshot() -> dict:
    return deepcopy(_get_state().get("players") or {})


def set_players(players: dict) -> None:
    state = _get_state()
    state["players"] = players
    validate_game_state(state)


def get_teams_snapshot() -> dict:
    return deepcopy(_get_state().get("teams") or {})


def set_teams(teams: dict) -> None:
    state = _get_state()
    state["teams"] = teams
    validate_game_state(state)


def get_games_snapshot() -> list:
    return deepcopy(_get_state().get("games") or [])


def get_player_stats_snapshot() -> dict:
    return deepcopy(_get_state().get("player_stats") or {})


def get_team_stats_snapshot() -> dict:
    return deepcopy(_get_state().get("team_stats") or {})


def get_phase_results_snapshot() -> dict:
    return deepcopy(_get_state().get("phase_results") or {})


def get_transactions_snapshot() -> list:
    return deepcopy(_get_state().get("transactions") or [])


def get_draft_picks_snapshot() -> dict:
    return deepcopy(_get_state().get("draft_picks") or {})


def set_transactions(entries: list) -> None:
    state = _get_state()
    state["transactions"] = entries
    validate_game_state(state)


def get_current_date() -> str | None:
    return _get_state()["league"]["current_date"]


def get_current_date_as_date() -> date:
    current = get_current_date()
    if current:
        try:
            return date.fromisoformat(str(current))
        except ValueError:
            pass

    season_start = _get_state()["league"].get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass

    return date.today()


def set_current_date(date_str: str | None) -> None:
    state = _get_state()
    state["league"]["current_date"] = date_str
    validate_game_state(state)


def set_last_gm_tick_date(date_str: str | None) -> None:
    state = _get_state()
    state["league"]["last_gm_tick_date"] = date_str
    validate_game_state(state)


def get_db_path() -> str | None:
    return _get_state()["league"].get("db_path")


def set_db_path(path: str) -> None:
    state = _get_state()
    state["league"]["db_path"] = path
    validate_game_state(state)


def get_active_season_id() -> str | None:
    return _get_state()["active_season_id"]


def set_active_season_id(next_season_id: str) -> None:
    state = _get_state()
    prev = state["active_season_id"]

    if prev is not None:
        state["season_history"][prev] = {
            "regular": {
                "games": deepcopy(state["games"]),
                "player_stats": deepcopy(state["player_stats"]),
                "team_stats": deepcopy(state["team_stats"]),
                "game_results": deepcopy(state["game_results"]),
            },
            "phases": deepcopy(state["phase_results"]),
            "postseason": deepcopy(state["postseason"]),
            "archived_at_turn": state["turn"],
            "archived_at_date": state["league"]["current_date"],
        }

    state["games"] = []
    state["player_stats"] = {}
    state["team_stats"] = {}
    state["game_results"] = {}
    state["phase_results"] = {}
    state["postseason"] = create_default_postseason_state()

    state["active_season_id"] = next_season_id
    state["cached_views"] = create_default_cached_views()
    validate_game_state(state)


def initialize_master_schedule_if_needed() -> None:
    state = _get_state()
    league = state["league"]
    master_schedule = league.get("master_schedule") or {}
    if master_schedule.get("games"):
        ensure_master_schedule_indices(league)
        return

    season_year = league.get("season_year")
    if not season_year:
        season_year = date.today().year
    build_master_schedule(league, int(season_year))
    ensure_master_schedule_indices(league)
    ensure_contracts_bootstrapped_after_schedule_creation_once()
    validate_game_state(state)


def get_schedule_summary() -> Dict[str, Any]:
    league = _get_state().get("league") or {}
    return _get_schedule_summary(league)


def ingest_game_result(game_result: dict, game_date: str | None = None) -> dict:
    validate_v2_game_result(game_result)
    state = _get_state()
    state["turn"] = int(state["turn"]) + 1

    phase = game_result.get("phase", "regular")
    if phase == "regular":
        container = state
    else:
        if phase not in NON_REGULAR_PHASES:
            raise ValueError(f"Unsupported phase: {phase}")
        if phase not in state["phase_results"]:
            state["phase_results"][phase] = create_default_phase_results()
        container = state["phase_results"][phase]

    if "game_id" in game_result:
        game_id = game_result["game_id"]
    elif "id" in game_result:
        game_id = game_result["id"]
    else:
        raise ValueError("game_result missing game_id")

    game_obj: Dict[str, Any]
    game_payload = game_result.get("game")
    if isinstance(game_payload, dict) and {"home_score", "away_score"}.issubset(game_payload.keys()):
        game_obj = game_payload
    else:
        game_obj = build_game_obj_from_result(game_result, game_date)

    game_obj["ingest_turn"] = int(state["turn"])

    container["game_results"][str(game_id)] = game_result
    if game_obj not in container["games"]:
        container["games"].append(game_obj)

    teams = _require_dict(game_result.get("teams"), "teams")
    season_player_stats = container["player_stats"]
    season_team_stats = container["team_stats"]

    for tid in (str(game_obj.get("home_team_id")), str(game_obj.get("away_team_id"))):
        team_game = _require_dict(teams[tid], f"teams.{tid}")
        _accumulate_team_game_result(tid, team_game, season_team_stats)
        rows = _require_list(team_game.get("players"), f"teams.{tid}.players")
        _accumulate_player_rows(rows, season_player_stats)

    league = state["league"]
    master_schedule = league.get("master_schedule") or {}
    by_id = master_schedule.get("by_id") if isinstance(master_schedule, dict) else None
    if isinstance(by_id, dict) and str(game_id) in by_id:
        mark_master_schedule_game_final(
            league=league,
            game_id=str(game_id),
            game_date_str=game_obj.get("date"),
            home_id=str(game_obj.get("home_team_id")),
            away_id=str(game_obj.get("away_team_id")),
            home_score=int(game_obj.get("home_score")),
            away_score=int(game_obj.get("away_score")),
        )

    state["cached_views"]["_meta"]["scores"]["built_from_turn"] = -1
    state["cached_views"]["_meta"]["schedule"]["built_from_turn_by_team"] = {}
    stats_cache = state["cached_views"].get("stats")
    if not isinstance(stats_cache, dict):
        stats_cache = {}
        state["cached_views"]["stats"] = stats_cache
    stats_cache["leaders"] = None

    validate_game_state(state)
    return game_result


def postseason_reset() -> None:
    state = _get_state()
    state["postseason"] = create_default_postseason_state()
    validate_game_state(state)


def postseason_set_field(field: dict | None) -> None:
    state = _get_state()
    state["postseason"]["field"] = field
    validate_game_state(state)


def postseason_set_play_in(play_in: dict | None) -> None:
    state = _get_state()
    state["postseason"]["play_in"] = play_in
    validate_game_state(state)


def postseason_set_playoffs(playoffs: dict | None) -> None:
    state = _get_state()
    state["postseason"]["playoffs"] = playoffs
    validate_game_state(state)


def postseason_set_champion(team_id: str | None) -> None:
    state = _get_state()
    state["postseason"]["champion"] = team_id
    validate_game_state(state)


def postseason_set_my_team_id(team_id: str | None) -> None:
    state = _get_state()
    state["postseason"]["my_team_id"] = team_id
    validate_game_state(state)


def get_postseason_snapshot() -> dict:
    return deepcopy(_get_state()["postseason"])


def get_weekly_news_cache_snapshot() -> dict:
    cache = _get_state().get("cached_views", {}).get("weekly_news")
    return deepcopy(cache) if isinstance(cache, dict) else {"last_generated_week_start": None, "items": []}


def set_weekly_news_cache(week_key: str, items: list[dict]) -> None:
    state = _get_state()
    cache = state["cached_views"].get("weekly_news")
    if not isinstance(cache, dict):
        cache = {}
        state["cached_views"]["weekly_news"] = cache
    cache["last_generated_week_start"] = week_key
    cache["items"] = items


def get_playoff_news_cache_snapshot() -> dict:
    cache = _get_state().get("cached_views", {}).get("playoff_news")
    return deepcopy(cache) if isinstance(cache, dict) else {"series_game_counts": {}, "items": []}


def set_playoff_news_cache(series_counts: dict, items: list[dict]) -> None:
    state = _get_state()
    cache = state["cached_views"].get("playoff_news")
    if not isinstance(cache, dict):
        cache = {}
        state["cached_views"]["playoff_news"] = cache
    cache["series_game_counts"] = series_counts
    cache["items"] = items


def set_stats_leaders(leaders: dict) -> None:
    state = _get_state()
    stats_cache = state["cached_views"].get("stats")
    if not isinstance(stats_cache, dict):
        stats_cache = {}
        state["cached_views"]["stats"] = stats_cache
    stats_cache["leaders"] = leaders


def set_playoff_stats_leaders(leaders: dict) -> None:
    state = _get_state()
    stats_cache = state["cached_views"].get("stats")
    if not isinstance(stats_cache, dict):
        stats_cache = {}
        state["cached_views"]["stats"] = stats_cache
    stats_cache["playoff_leaders"] = leaders


def get_scores_view(season_id: str, limit: int = 20) -> Dict[str, Any]:
    state = _get_state()
    ensure_ingest_turn_backfilled(state)
    return _get_scores_view(state, season_id, limit=limit)


def get_team_schedule_view(
    team_id: str,
    season_id: str,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    state = _get_state()
    league = state["league"]
    return _get_team_schedule_view(state, league, team_id, season_id, today=today)


def ensure_db_initialized_and_seeded() -> None:
    league = _get_state()["league"]
    db_path = str(league.get("db_path") or "league.db")
    if _DB_INITIALIZED_BY_PATH.get(db_path):
        return

    from league_repo import LeagueRepo

    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_gm_profiles_seeded(ALL_TEAM_IDS)

    _DB_INITIALIZED_BY_PATH[db_path] = True


def ensure_contracts_bootstrapped_after_schedule_creation_once() -> None:
    league = _get_state()["league"]
    season_year = league.get("season_year")
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return

    if str(season_year_int) in _CONTRACTS_BOOTSTRAPPED_SEASONS:
        return

    from league_repo import LeagueRepo

    db_path = str(league.get("db_path") or "league.db")
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_contracts_bootstrapped_from_roster(season_year_int)
        repo.rebuild_contract_indices()

    _CONTRACTS_BOOTSTRAPPED_SEASONS.add(str(season_year_int))


def validate_repo_integrity_once_startup() -> None:
    league = _get_state()["league"]
    db_path = str(league.get("db_path") or "league.db")
    if _REPO_INTEGRITY_VALIDATED_BY_PATH.get(db_path):
        return

    from league_repo import LeagueRepo

    with LeagueRepo(db_path) as repo:
        repo.validate_integrity()

    _REPO_INTEGRITY_VALIDATED_BY_PATH[db_path] = True


def ensure_roster_cache_initialized_once_startup() -> None:
    state = _get_state()
    if not isinstance(state.get("players"), dict):
        state["players"] = {}
    if not isinstance(state.get("teams"), dict):
        state["teams"] = {}


def ensure_cap_model_populated_if_needed() -> None:
    league = _get_state()["league"]
    trade_rules = league.get("trade_rules") or {}
    season_year = league.get("season_year")
    salary_cap = trade_rules.get("salary_cap") if isinstance(trade_rules, dict) else None
    if not season_year:
        return
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return
    try:
        salary_cap_value = float(salary_cap or 0)
    except (TypeError, ValueError):
        salary_cap_value = 0.0
    if salary_cap_value <= 0:
        _apply_cap_model_for_season(league, season_year_int)


def ensure_trade_state_keys() -> None:
    state = _get_state()
    _ensure_trade_state_keys(
        state,
        defaults={
            "trade_market": _DEFAULT_TRADE_MARKET,
            "trade_memory": _DEFAULT_TRADE_MEMORY,
        },
    )


def ensure_player_ids_normalized(*, allow_legacy_numeric: bool = True) -> dict:
    state = _get_state()
    return normalize_player_ids(state, allow_legacy_numeric=allow_legacy_numeric)


def update_players_after_trade(
    moves: list[dict],
    acquired_date: str,
    season_key: str | None,
) -> None:
    state = _get_state()
    players_state = state.get("players")
    if not isinstance(players_state, dict):
        return
    for move in moves:
        player_id = move.get("player_id")
        to_team = move.get("to_team")
        from_team = move.get("from_team")
        if not player_id or not to_team:
            continue
        ps = players_state.get(player_id)
        if not isinstance(ps, dict):
            continue
        ps["team_id"] = to_team
        if "signed_date" not in ps:
            ps["signed_date"] = "1900-01-01"
        if "signed_via_free_agency" not in ps:
            ps["signed_via_free_agency"] = False
        ps["acquired_date"] = acquired_date
        ps["acquired_via_trade"] = True
        if season_key and from_team:
            bans = ps.get("trade_return_bans")
            if not isinstance(bans, dict):
                bans = {}
                ps["trade_return_bans"] = bans
            season_bans = bans.get(season_key)
            if not isinstance(season_bans, list):
                season_bans = []
            if from_team not in season_bans:
                season_bans.append(from_team)
            bans[season_key] = season_bans


__all__ = [
    "DEFAULT_TRADE_RULES",
    "export_state_snapshot",
    "export_trade_context_snapshot",
    "export_workflow_state",
    "get_active_season_id",
    "get_current_date",
    "get_current_date_as_date",
    "get_db_path",
    "get_games_snapshot",
    "get_league_snapshot",
    "get_player_stats_snapshot",
    "get_phase_results_snapshot",
    "get_playoff_news_cache_snapshot",
    "get_postseason_snapshot",
    "get_schedule_summary",
    "get_scores_view",
    "get_trade_rules_snapshot",
    "get_team_schedule_view",
    "get_team_stats_snapshot",
    "get_teams_snapshot",
    "get_transactions_snapshot",
    "get_draft_picks_snapshot",
    "get_weekly_news_cache_snapshot",
    "ingest_game_result",
    "initialize_master_schedule_if_needed",
    "negotiation_get",
    "negotiation_put",
    "postseason_reset",
    "postseason_set_champion",
    "postseason_set_field",
    "postseason_set_my_team_id",
    "postseason_set_play_in",
    "postseason_set_playoffs",
    "replace_state_snapshot",
    "set_active_season_id",
    "set_current_date",
    "set_last_gm_tick_date",
    "set_db_path",
    "set_playoff_news_cache",
    "set_playoff_stats_leaders",
    "set_players",
    "set_stats_leaders",
    "set_teams",
    "set_transactions",
    "set_weekly_news_cache",
    "startup_init_state",
    "trade_get_agreement",
    "trade_get_agreements_snapshot",
    "trade_get_asset_locks",
    "trade_remove_asset_lock",
    "trade_set_agreement",
    "trade_set_asset_lock",
    "update_players_after_trade",
    "validate_master_schedule_entry",
    "validate_state",
    "validate_v2_game_result",
]
