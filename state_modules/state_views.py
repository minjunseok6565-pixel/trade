from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .state_cache import _ensure_cached_views_meta
from .state_core import ensure_league_block
from .state_migrations import _ensure_ingest_turn_backfilled
from .state_schedule import _ensure_master_schedule_indices, initialize_master_schedule_if_needed


def get_scores_view(state: dict, season_id: str, limit: int = 20) -> Dict[str, Any]:
    """Return cached or rebuilt scores view for the given season."""
    _ensure_ingest_turn_backfilled(state)
    cached = state.setdefault("cached_views", {})
    scores_view = cached.setdefault("scores", {"latest_date": None, "games": []})
    meta = _ensure_cached_views_meta(state)
    current_turn = int(state.get("turn", 0) or 0)

    if (
        meta["scores"].get("built_from_turn") == current_turn
        and str(meta["scores"].get("season_id")) == str(season_id)
    ):
        games = scores_view.get("games") or []
        limited_games = [] if limit <= 0 else games[:limit]
        return {"latest_date": scores_view.get("latest_date"), "games": limited_games}

    games: List[Dict[str, Any]] = []
    active_season_id = state.get("active_season_id")
    if active_season_id is not None and str(active_season_id) == str(season_id):
        games.extend(state.get("games") or [])
        phase_results = state.get("phase_results") or {}
        for phase in ("preseason", "play_in", "playoffs"):
            phase_container = phase_results.get(phase) or {}
            games.extend(phase_container.get("games") or [])
    else:
        history = state.get("season_history") or {}
        season_history = history.get(str(season_id)) or {}
        regular = season_history.get("regular") or {}
        games.extend(regular.get("games") or [])
        phase_results = season_history.get("phase_results") or {}
        for phase in ("preseason", "play_in", "playoffs"):
            phase_container = phase_results.get(phase) or {}
            games.extend(phase_container.get("games") or [])

    def _ingest_turn_key(game_obj: Dict[str, Any]) -> int:
        try:
            return int(game_obj.get("ingest_turn") or 0)
        except (TypeError, ValueError):
            return 0

    games_sorted = sorted(games, key=_ingest_turn_key, reverse=True)
    latest_date = games_sorted[0].get("date") if games_sorted else None

    scores_view["games"] = games_sorted
    scores_view["latest_date"] = latest_date
    meta["scores"]["built_from_turn"] = current_turn
    meta["scores"]["season_id"] = season_id

    limited_games = [] if limit <= 0 else games_sorted[:limit]
    return {"latest_date": latest_date, "games": limited_games}


def get_team_schedule_view(
    state: dict,
    team_id: str,
    season_id: str,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Return cached or rebuilt schedule view for a team."""
    active_season_id = state.get("active_season_id")
    if active_season_id is not None and str(season_id) != str(active_season_id):
        return {"past_games": [], "upcoming_games": []}

    initialize_master_schedule_if_needed(state)
    _ensure_master_schedule_indices(state)
    league = ensure_league_block(state)
    master_schedule = league.get("master_schedule") or {}
    by_team = master_schedule.get("by_team") or {}
    by_id = master_schedule.get("by_id") or {}

    cached = state.setdefault("cached_views", {})
    schedule = cached.setdefault("schedule", {})
    teams_cache = schedule.setdefault("teams", {})
    meta = _ensure_cached_views_meta(state)
    current_turn = int(state.get("turn", 0) or 0)

    if (
        meta["schedule"].get("season_id") == season_id
        and meta["schedule"].get("built_from_turn_by_team", {}).get(team_id) == current_turn
        and team_id in teams_cache
    ):
        return teams_cache[team_id]

    game_ids = by_team.get(team_id, [])
    past_games: List[Dict[str, Any]] = []
    upcoming_games: List[Dict[str, Any]] = []

    for game_id in game_ids:
        entry = by_id.get(game_id) if isinstance(by_id, dict) else None
        if not entry:
            continue
        status = entry.get("status")
        home_id = str(entry.get("home_team_id"))
        away_id = str(entry.get("away_team_id"))
        is_home = home_id == team_id
        opponent_team_id = away_id if is_home else home_id
        home_score = entry.get("home_score")
        away_score = entry.get("away_score")
        is_final = status == "final" and isinstance(home_score, int) and isinstance(away_score, int)
        if is_final:
            my_score = home_score if is_home else away_score
            opp_score = away_score if is_home else home_score
            result_for_user_team = "W" if my_score > opp_score else "L"
        else:
            my_score = None
            opp_score = None
            result_for_user_team = None

        row = {
            "game_id": str(entry.get("game_id")),
            "date": str(entry.get("date")),
            "status": str(status),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_score": home_score if isinstance(home_score, int) else None,
            "away_score": away_score if isinstance(away_score, int) else None,
            "opponent_team_id": opponent_team_id,
            "is_home": is_home,
            "my_score": my_score,
            "opp_score": opp_score,
            "result_for_user_team": result_for_user_team,
        }

        if status == "final":
            past_games.append(row)
        else:
            upcoming_games.append(row)

    def _schedule_date_sort_key(entry: Dict[str, Any]) -> tuple[int, Any]:
        raw_date = entry.get("date")
        try:
            return (0, date.fromisoformat(str(raw_date)))
        except (TypeError, ValueError):
            return (1, str(raw_date))

    past_games.sort(key=_schedule_date_sort_key, reverse=True)
    upcoming_games.sort(key=_schedule_date_sort_key)

    teams_cache[team_id] = {"past_games": past_games, "upcoming_games": upcoming_games}
    meta["schedule"]["built_from_turn_by_team"][team_id] = current_turn
    meta["schedule"]["season_id"] = season_id
    return teams_cache[team_id]
