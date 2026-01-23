from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Dict, Optional

from state_schema import (
    create_default_cached_views,
    create_default_phase_results,
    create_default_postseason_state,
    validate_game_state,
)
from state_modules.state_store import _ALLOWED_PHASES, _ALLOWED_SCHEDULE_STATUSES, _get_state
from state_modules.state_utils import (
    _is_number,
    _merge_counter_dict_sum,
    _require_dict,
    _require_list,
)
from state_modules.state_store import _META_PLAYER_KEYS
from state_modules.state_store import reset_state_for_dev as _reset_state_for_dev

_CONTRACTS_BOOTSTRAPPED_SEASONS: set[str] = set()


def validate_state() -> None:
    validate_game_state(_get_state())


def reset_state_for_dev() -> None:
    _reset_state_for_dev()


def startup_init_state() -> None:
    state = _get_state()
    validate_game_state(state)

    from config import ALL_TEAM_IDS
    from league_repo import LeagueRepo
    from state_modules.state_cap import _apply_cap_model_for_season
    from schema import season_id_from_year as _schema_season_id_from_year
    from contracts.offseason import process_offseason

    league = state["league"]
    db_path = str(league.get("db_path") or "league.db")
    if not league.get("db_path"):
        league["db_path"] = db_path

    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_gm_profiles_seeded(ALL_TEAM_IDS)
        repo.validate_integrity()

    try:
        from team_utils import _init_players_and_teams_if_needed

        _init_players_and_teams_if_needed()
    except Exception:
        pass

    trade_rules = league.get("trade_rules") or {}
    season_year = league.get("season_year")
    if season_year:
        try:
            season_year_int = int(season_year)
        except (TypeError, ValueError):
            season_year_int = None
        if season_year_int is not None:
            try:
                salary_cap_value = float(trade_rules.get("salary_cap") or 0)
            except (TypeError, ValueError):
                salary_cap_value = 0.0
            if salary_cap_value <= 0:
                _apply_cap_model_for_season(league, season_year_int)

    season_year = league.get("season_year")
    if season_year:
        try:
            season_year_int = int(season_year)
        except (TypeError, ValueError):
            season_year_int = None
        if season_year_int is not None:
            previous_season = season_year_int - 1
            current_season_id = str(_schema_season_id_from_year(season_year_int))
            if state.get("active_season_id") != current_season_id:
                if state.get("active_season_id") is not None:
                    try:
                        process_offseason(state, previous_season, season_year_int)
                    except Exception:
                        pass
                set_active_season_id(current_season_id)

    validate_game_state(state)


def export_workflow_state() -> dict:
    state_copy = deepcopy(_get_state())
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
    ):
        state_copy.pop(key, None)
    return state_copy


def export_trade_context_snapshot() -> dict:
    state = _get_state()
    league = state["league"]
    return {
        "players": deepcopy(state["players"]),
        "teams": deepcopy(state["teams"]),
        "asset_locks": deepcopy(state["asset_locks"]),
        "negotiations": deepcopy(state["negotiations"]),
        "trade_agreements": deepcopy(state["trade_agreements"]),
        "trade_rules": deepcopy(league.get("trade_rules") or {}),
        "current_date": league.get("current_date"),
    }


def get_current_date() -> Optional[str]:
    return _get_state()["league"]["current_date"]


def set_current_date(date_str: str) -> None:
    state = _get_state()
    state["league"]["current_date"] = date_str
    validate_game_state(state)


def get_db_path() -> Optional[str]:
    return _get_state()["league"].get("db_path")


def set_db_path(path: str) -> None:
    state = _get_state()
    state["league"]["db_path"] = path
    validate_game_state(state)


def ensure_cap_model_populated_if_needed() -> None:
    from state_modules.state_cap import _apply_cap_model_for_season

    state = _get_state()
    league = state["league"]
    trade_rules = league.get("trade_rules") or {}
    season_year = league.get("season_year")
    if not season_year:
        return
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return
    try:
        salary_cap_value = float(trade_rules.get("salary_cap") or 0)
    except (TypeError, ValueError):
        salary_cap_value = 0.0
    if salary_cap_value <= 0:
        _apply_cap_model_for_season(league, season_year_int)
        validate_game_state(state)


def get_league_snapshot() -> dict:
    return deepcopy(_get_state()["league"])


def set_league_state(league: dict) -> None:
    state = _get_state()
    state["league"] = league
    validate_game_state(state)


def get_active_season_id() -> Optional[str]:
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


def get_players_snapshot() -> dict:
    return deepcopy(_get_state()["players"])


def set_players(players: dict) -> None:
    state = _get_state()
    state["players"] = players
    validate_game_state(state)


def get_teams_snapshot() -> dict:
    return deepcopy(_get_state()["teams"])


def set_teams(teams: dict) -> None:
    state = _get_state()
    state["teams"] = teams
    validate_game_state(state)


def update_players_after_trade(
    player_moves: list[dict[str, Any]],
    acquired_date: str,
    season_key: Optional[str],
) -> None:
    state = _get_state()
    players_state = state.get("players")
    if not isinstance(players_state, dict):
        return
    for move in player_moves:
        player_id = str(move.get("player_id") or "")
        ps = players_state.get(player_id)
        if not isinstance(ps, dict):
            continue
        ps["team_id"] = move.get("to_team")
        if "signed_date" not in ps:
            ps["signed_date"] = "1900-01-01"
        if "signed_via_free_agency" not in ps:
            ps["signed_via_free_agency"] = False
        ps["acquired_date"] = acquired_date
        ps["acquired_via_trade"] = True
        if season_key:
            bans = ps.get("trade_return_bans")
            if not isinstance(bans, dict):
                bans = {}
                ps["trade_return_bans"] = bans
            season_bans = bans.get(season_key)
            if not isinstance(season_bans, list):
                season_bans = []
            from_team = move.get("from_team")
            if from_team and from_team not in season_bans:
                season_bans.append(from_team)
            bans[season_key] = season_bans
    validate_game_state(state)


def get_games_snapshot() -> list:
    return deepcopy(_get_state()["games"])


def get_player_stats_snapshot() -> dict:
    return deepcopy(_get_state()["player_stats"])


def get_team_stats_snapshot() -> dict:
    return deepcopy(_get_state()["team_stats"])


def get_phase_results_snapshot() -> dict:
    return deepcopy(_get_state()["phase_results"])


def get_season_history_snapshot() -> dict:
    return deepcopy(_get_state()["season_history"])


def get_transactions_snapshot() -> list:
    state = _get_state()
    transactions = state.get("transactions")
    if isinstance(transactions, list):
        return deepcopy(transactions)
    return []


def get_cached_views_snapshot() -> dict:
    return deepcopy(_get_state()["cached_views"])


def set_cached_view_stats_leaders(leaders: dict) -> None:
    state = _get_state()
    state["cached_views"]["stats"]["leaders"] = leaders
    validate_game_state(state)


def set_cached_view_playoff_leaders(leaders: dict | None) -> None:
    state = _get_state()
    state["cached_views"]["stats"]["playoff_leaders"] = leaders
    validate_game_state(state)


def set_cached_view_weekly_news(payload: dict | None) -> None:
    state = _get_state()
    state["cached_views"]["weekly_news"] = payload
    validate_game_state(state)


def set_cached_view_playoff_news(payload: dict | None) -> None:
    state = _get_state()
    state["cached_views"]["playoff_news"] = payload
    validate_game_state(state)


def trade_get_asset_locks() -> dict:
    return deepcopy(_get_state()["asset_locks"])


def trade_get_asset_lock(asset_id: str) -> dict | None:
    lock = _get_state()["asset_locks"].get(asset_id)
    return deepcopy(lock) if isinstance(lock, dict) else None


def trade_set_asset_lock(asset_id: str, lock_obj: dict) -> None:
    state = _get_state()
    state["asset_locks"][asset_id] = lock_obj
    validate_game_state(state)


def trade_remove_asset_lock(asset_id: str) -> None:
    state = _get_state()
    state["asset_locks"].pop(asset_id, None)
    validate_game_state(state)


def trade_get_agreement(deal_id: str) -> dict | None:
    entry = _get_state()["trade_agreements"].get(deal_id)
    return deepcopy(entry) if isinstance(entry, dict) else None


def trade_set_agreement(deal_id: str, entry: dict) -> None:
    state = _get_state()
    state["trade_agreements"][deal_id] = entry
    validate_game_state(state)


def trade_update_agreement_status(deal_id: str, status: str) -> None:
    state = _get_state()
    entry = state["trade_agreements"].get(deal_id)
    if isinstance(entry, dict):
        entry["status"] = status
        validate_game_state(state)


def trade_get_agreements_snapshot() -> dict:
    return deepcopy(_get_state()["trade_agreements"])


def negotiation_put(session_id: str, obj: dict) -> None:
    state = _get_state()
    state["negotiations"][session_id] = obj
    validate_game_state(state)


def negotiation_get(session_id: str) -> dict | None:
    entry = _get_state()["negotiations"].get(session_id)
    return deepcopy(entry) if isinstance(entry, dict) else None


def get_trade_rules_snapshot() -> dict:
    league = _get_state()["league"]
    return deepcopy(league.get("trade_rules") or {})


def set_last_gm_tick_date(value: str | None) -> None:
    state = _get_state()
    state["league"]["last_gm_tick_date"] = value
    validate_game_state(state)


def initialize_master_schedule_if_needed() -> None:
    from state_modules.state_schedule import ensure_master_schedule_indices, build_master_schedule
    from config import INITIAL_SEASON_YEAR

    state = _get_state()
    league = state["league"]
    master_schedule = league.get("master_schedule") or {}
    if master_schedule.get("games"):
        ensure_master_schedule_indices(league)
        return

    season_year = int(league.get("season_year") or INITIAL_SEASON_YEAR)
    build_master_schedule(league, season_year)
    ensure_master_schedule_indices(league)

    if str(season_year) not in _CONTRACTS_BOOTSTRAPPED_SEASONS:
        try:
            from league_repo import LeagueRepo

            db_path = str(league.get("db_path") or "league.db")
            with LeagueRepo(db_path) as repo:
                repo.init_db()
                repo.ensure_contracts_bootstrapped_from_roster(season_year)
                repo.rebuild_contract_indices()
            _CONTRACTS_BOOTSTRAPPED_SEASONS.add(str(season_year))
        except Exception:
            pass

    validate_game_state(state)


def build_master_schedule(season_year: int) -> None:
    from state_modules.state_schedule import build_master_schedule as _build_schedule

    state = _get_state()
    league = state["league"]
    _build_schedule(league, season_year)
    validate_game_state(state)


def get_schedule_summary() -> dict:
    from state_modules.state_schedule import get_schedule_summary as _get_summary

    initialize_master_schedule_if_needed()
    return _get_summary(_get_state()["league"])


def _accumulate_player_rows(rows: list[dict[str, Any]], season_player_stats: dict[str, Any]) -> None:
    for row in rows:
        player_id = str(row["PlayerID"])
        team_id = str(row["TeamID"])

        entry = season_player_stats.get(player_id)
        if entry is None:
            entry = {
                "player_id": player_id,
                "name": row.get("Name"),
                "team_id": team_id,
                "games": 0,
                "totals": {},
            }
            season_player_stats[player_id] = entry
        entry["name"] = row.get("Name", entry.get("name"))
        entry["team_id"] = team_id
        entry["games"] = int(entry.get("games", 0) or 0) + 1

        totals = entry.get("totals")
        if totals is None:
            totals = {}
            entry["totals"] = totals
        for key, value in row.items():
            if key in _META_PLAYER_KEYS:
                continue
            if _is_number(value):
                try:
                    totals[key] = float(totals.get(key, 0.0)) + float(value)
                except (TypeError, ValueError):
                    continue


def _accumulate_team_game_result(team_id: str, team_game: dict[str, Any], season_team_stats: dict[str, Any]) -> None:
    totals_src = _require_dict(team_game.get("totals"), f"teams.{team_id}.totals")
    breakdowns_src = team_game.get("breakdowns") or {}
    extra_totals = team_game.get("extra_totals") or {}
    extra_breakdowns = team_game.get("extra_breakdowns") or {}

    entry = season_team_stats.get(team_id)
    if entry is None:
        entry = {"team_id": team_id, "games": 0, "totals": {}, "breakdowns": {}}
        season_team_stats[team_id] = entry
    entry["games"] = int(entry.get("games", 0) or 0) + 1

    totals = entry.get("totals")
    if totals is None:
        totals = {}
        entry["totals"] = totals
    for key, value in {**totals_src, **extra_totals}.items():
        if _is_number(value):
            try:
                totals[key] = float(totals.get(key, 0.0)) + float(value)
            except (TypeError, ValueError):
                continue

    breakdowns = entry.get("breakdowns")
    if breakdowns is None:
        breakdowns = {}
        entry["breakdowns"] = breakdowns
    if isinstance(breakdowns_src, dict):
        _merge_counter_dict_sum(breakdowns, breakdowns_src)
    if isinstance(extra_breakdowns, dict):
        _merge_counter_dict_sum(breakdowns, extra_breakdowns)


def _build_game_obj(game_result: dict, game_date: Optional[str]) -> dict:
    game = _require_dict(game_result.get("game"), "game")
    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])
    final = _require_dict(game_result.get("final"), "final")

    game_date_str = str(game_date) if game_date else str(game["date"])
    game_id = str(game["game_id"])

    home_score = int(final[home_id])
    away_score = int(final[away_id])

    return {
        "game_id": game_id,
        "date": game_date_str,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "status": "final",
        "is_overtime": int(game.get("overtime_periods", 0) or 0) > 0,
        "phase": game.get("phase"),
        "season_id": game.get("season_id"),
        "schema_version": "2.0",
    }


def _mark_master_schedule_game_final(
    state: dict,
    *,
    game_id: str,
    game_date_str: str,
    home_id: str,
    away_id: str,
    home_score: int,
    away_score: int,
) -> None:
    master_schedule = state["league"]["master_schedule"]
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


def ingest_game_result(game_result: dict, game_date: Optional[str] = None) -> dict:
    state = _get_state()
    state["turn"] += 1

    phase = game_result.get("phase", "regular")
    if phase == "regular":
        container = state
    else:
        if phase not in {"play_in", "playoffs", "preseason"}:
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

    game_obj = game_result.get("game")
    if not isinstance(game_obj, dict):
        game_obj = _build_game_obj(game_result, game_date)

    container["game_results"][str(game_id)] = game_result

    if game_obj not in container["games"]:
        container["games"].append(game_obj)

    teams = _require_dict(game_result.get("teams"), "teams")
    home_id = str(game_obj.get("home_team_id"))
    away_id = str(game_obj.get("away_team_id"))

    for tid in (home_id, away_id):
        team_game = _require_dict(teams.get(tid), f"teams.{tid}")
        _accumulate_team_game_result(tid, team_game, container["team_stats"])
        rows = _require_list(team_game.get("players"), f"teams.{tid}.players")
        _accumulate_player_rows(rows, container["player_stats"])

    if state["league"]["master_schedule"]["by_id"].get(str(game_id)):
        _mark_master_schedule_game_final(
            state,
            game_id=str(game_id),
            game_date_str=str(game_obj.get("date")),
            home_id=home_id,
            away_id=away_id,
            home_score=int(game_obj.get("home_score") or 0),
            away_score=int(game_obj.get("away_score") or 0),
        )

    cached_views = state["cached_views"]
    cached_views["_meta"]["scores"]["built_from_turn"] = -1
    cached_views["_meta"]["schedule"]["built_from_turn_by_team"] = {}
    cached_views["stats"]["leaders"] = None

    validate_game_state(state)
    return game_result


def postseason_reset() -> None:
    state = _get_state()
    state["postseason"] = create_default_postseason_state()
    validate_game_state(state)


def postseason_set_field(field: Optional[dict]) -> None:
    state = _get_state()
    state["postseason"]["field"] = field
    validate_game_state(state)


def postseason_set_play_in(play_in: Optional[dict]) -> None:
    state = _get_state()
    state["postseason"]["play_in"] = play_in
    validate_game_state(state)


def postseason_set_playoffs(playoffs: Optional[dict]) -> None:
    state = _get_state()
    state["postseason"]["playoffs"] = playoffs
    validate_game_state(state)


def postseason_set_champion(team_id: Optional[str]) -> None:
    state = _get_state()
    state["postseason"]["champion"] = team_id
    validate_game_state(state)


def postseason_set_my_team_id(team_id: Optional[str]) -> None:
    state = _get_state()
    state["postseason"]["my_team_id"] = team_id
    validate_game_state(state)


def get_postseason_snapshot() -> dict:
    return deepcopy(_get_state()["postseason"])


def validate_v2_game_result(game_result: Dict[str, Any]) -> None:
    if not isinstance(game_result, dict):
        raise ValueError("GameResultV2 invalid: result must be a dict")

    if game_result.get("schema_version") != "2.0":
        raise ValueError("GameResultV2 invalid: schema_version must be '2.0'")

    game = _require_dict(game_result.get("game"), "game")

    required_game_keys = [
        "game_id",
        "date",
        "season_id",
        "phase",
        "home_team_id",
        "away_team_id",
        "overtime_periods",
        "possessions_per_team",
    ]
    for key in required_game_keys:
        if key not in game:
            raise ValueError(f"GameResultV2 invalid: missing game.{key}")

    if game["phase"] not in _ALLOWED_PHASES:
        raise ValueError(f"GameResultV2 invalid: unsupported phase '{game['phase']}'")

    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])

    final = _require_dict(game_result.get("final"), "final")
    if home_id not in final or away_id not in final:
        raise ValueError("GameResultV2 invalid: final must include both home and away team ids")

    teams = _require_dict(game_result.get("teams"), "teams")
    if home_id not in teams or away_id not in teams:
        raise ValueError("GameResultV2 invalid: teams must include both home and away team ids")

    for tid in (home_id, away_id):
        team_obj = _require_dict(teams.get(tid), f"teams.{tid}")
        totals = _require_dict(team_obj.get("totals"), f"teams.{tid}.totals")
        if "PTS" not in totals:
            raise ValueError(f"GameResultV2 invalid: teams.{tid}.totals.PTS is required")

        players = _require_list(team_obj.get("players"), f"teams.{tid}.players")
        for idx, row in enumerate(players):
            if not isinstance(row, dict):
                raise ValueError(f"GameResultV2 invalid: teams.{tid}.players[{idx}] must be a dict")
            if "PlayerID" not in row:
                raise ValueError(f"GameResultV2 invalid: teams.{tid}.players[{idx}].PlayerID is required")
            if "TeamID" not in row:
                raise ValueError(f"GameResultV2 invalid: teams.{tid}.players[{idx}].TeamID is required")
            if str(row["TeamID"]) != tid:
                raise ValueError(
                    f"GameResultV2 invalid: teams.{tid}.players[{idx}].TeamID must match team id '{tid}'"
                )

        breakdowns = team_obj.get("breakdowns", {})
        if breakdowns is not None and not isinstance(breakdowns, dict):
            raise ValueError(f"GameResultV2 invalid: teams.{tid}.breakdowns must be a dict if present")


def validate_master_schedule_entry(entry: Dict[str, Any], *, path: str = "master_schedule.entry") -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"MasterScheduleEntry invalid: '{path}' must be a dict")

    for key in ("game_id", "home_team_id", "away_team_id", "status"):
        if key not in entry:
            raise ValueError(f"MasterScheduleEntry invalid: missing {path}.{key}")

    game_id = entry.get("game_id")
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError(f"MasterScheduleEntry invalid: {path}.game_id must be a non-empty string")

    for key in ("home_team_id", "away_team_id"):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{key} must be a non-empty string")

    status = entry.get("status")
    if not isinstance(status, str) or status not in _ALLOWED_SCHEDULE_STATUSES:
        raise ValueError(
            f"MasterScheduleEntry invalid: {path}.status must be one of {sorted(_ALLOWED_SCHEDULE_STATUSES)}"
        )

    for tk in ("tactics", "home_tactics", "away_tactics"):
        if tk in entry and entry[tk] is not None and not isinstance(entry[tk], dict):
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{tk} must be a dict if present")

    if "date" in entry and entry["date"] is not None and not isinstance(entry["date"], str):
        raise ValueError(f"MasterScheduleEntry invalid: {path}.date must be a string if present")

    for sk in ("home_score", "away_score"):
        if sk in entry and entry[sk] is not None and not isinstance(entry[sk], int):
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{sk} must be int or None if present")


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


def ensure_all_initialized_once_startup() -> None:
    startup_init_state()


__all__ = [
    "build_master_schedule",
    "ensure_all_initialized_once_startup",
    "ensure_cap_model_populated_if_needed",
    "export_trade_context_snapshot",
    "export_workflow_state",
    "get_active_season_id",
    "get_cached_views_snapshot",
    "get_current_date",
    "get_current_date_as_date",
    "get_db_path",
    "get_games_snapshot",
    "get_league_snapshot",
    "get_phase_results_snapshot",
    "get_player_stats_snapshot",
    "get_postseason_snapshot",
    "get_schedule_summary",
    "get_season_history_snapshot",
    "get_team_stats_snapshot",
    "get_teams_snapshot",
    "get_players_snapshot",
    "get_trade_rules_snapshot",
    "get_transactions_snapshot",
    "initialize_master_schedule_if_needed",
    "ingest_game_result",
    "negotiation_get",
    "negotiation_put",
    "postseason_reset",
    "postseason_set_champion",
    "postseason_set_field",
    "postseason_set_my_team_id",
    "postseason_set_play_in",
    "postseason_set_playoffs",
    "reset_state_for_dev",
    "set_active_season_id",
    "set_cached_view_playoff_leaders",
    "set_cached_view_playoff_news",
    "set_cached_view_stats_leaders",
    "set_cached_view_weekly_news",
    "set_current_date",
    "set_db_path",
    "set_last_gm_tick_date",
    "set_league_state",
    "set_players",
    "set_teams",
    "startup_init_state",
    "trade_get_agreement",
    "trade_get_agreements_snapshot",
    "trade_get_asset_lock",
    "trade_get_asset_locks",
    "trade_remove_asset_lock",
    "trade_set_agreement",
    "trade_set_asset_lock",
    "trade_update_agreement_status",
    "update_players_after_trade",
    "validate_master_schedule_entry",
    "validate_state",
    "validate_v2_game_result",
]
