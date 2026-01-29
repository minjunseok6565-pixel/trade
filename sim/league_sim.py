from __future__ import annotations

import logging
import random
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

import schema

from league_repo import LeagueRepo
from matchengine_v2_adapter import (
    adapt_matchengine_result_to_v2,
    build_context_from_master_schedule_entry,
    build_context_from_team_ids,
)
from matchengine_v3.sim_game import simulate_game
from state import (
    export_full_state_snapshot,
    get_db_path,
    get_league_context_snapshot,
    ingest_game_result,
    set_current_date,
)
from trades_ai import _run_ai_gm_tick_if_needed
from sim.roster_adapter import build_team_state_from_db

logger = logging.getLogger(__name__)

@contextmanager
def _repo_ctx() -> LeagueRepo:
    db_path = get_db_path()
    with LeagueRepo(db_path) as repo:
        try:
            repo.init_db()
        except Exception as exc:
            logger.exception(
                "[DB_INIT_FAILED] sim.league_sim._repo_ctx repo.init_db() failed (db_path=%s): %s",
                db_path,
                str(exc),
            )
            raise
        yield repo


def _run_match(
    *,
    home_team_id: str,
    away_team_id: str,
    game_date: str,
    home_tactics: Optional[Dict[str, Any]] = None,
    away_tactics: Optional[Dict[str, Any]] = None,
    context: schema.GameContext,
) -> Dict[str, Any]:
    rng = random.Random()
    with _repo_ctx() as repo:
        home = build_team_state_from_db(repo=repo, team_id=home_team_id, tactics=home_tactics)
        away = build_team_state_from_db(repo=repo, team_id=away_team_id, tactics=away_tactics)

    raw_result = simulate_game(rng, home, away, context=context)
    v2_result = adapt_matchengine_result_to_v2(
        raw_result,
        context,
        engine_name="matchengine_v3",
    )
    return ingest_game_result(game_result=v2_result, game_date=game_date)


def advance_league_until(
    target_date_str: str,
    user_team_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    league_full = export_full_state_snapshot().get("league", {})
    master_schedule = league_full.get("master_schedule", {})
    by_date: Dict[str, List[str]] = master_schedule.get("by_date") or {}
    games: List[Dict[str, Any]] = master_schedule.get("games") or []

    if not by_date or not games:
        raise RuntimeError(
            "Master schedule is not initialized. Expected state.startup_init_state() to run before calling advance_league_until()."
        )

    
    try:
        target_date = date.fromisoformat(target_date_str)
    except ValueError as exc:
        raise ValueError(f"invalid target_date: {target_date_str}") from exc

    league_context = get_league_context_snapshot()
    current_date_str = league_context.get("current_date")
    if current_date_str:
        try:
            current_date = date.fromisoformat(current_date_str)
        except ValueError:
            current_date = target_date
    else:
        if league_context.get("season_start"):
            try:
                season_start = date.fromisoformat(league_context["season_start"])
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
            g = next((x for x in games if x.get("game_id") == gid), None)
            if not g:
                continue
            if g.get("status") == "final":
                continue

            home_id = str(g["home_team_id"]).upper()
            away_id = str(g["away_team_id"]).upper()

            if user_team_upper and (home_id == user_team_upper or away_id == user_team_upper):
                continue

            # SSOT: context date must come from entry["date"] (no override arg in builder).
            entry_for_ctx = dict(g)
            entry_for_ctx["date"] = day_str
            context = build_context_from_master_schedule_entry(
                entry=entry_for_ctx,
                league_state=league_context,
                phase=str(g.get("phase") or "regular"),
            )

            game_obj = _run_match(
                home_team_id=home_id,
                away_team_id=away_id,
                game_date=day_str,
                context=context,
            )
            simulated_game_objs.append(game_obj)

        day += timedelta(days=1)

    set_current_date(target_date_str)
    _run_ai_gm_tick_if_needed(target_date)
    return simulated_game_objs


def simulate_single_game(
    home_team_id: str,
    away_team_id: str,
    game_date: Optional[str] = None,
    home_tactics: Optional[Dict[str, Any]] = None,
    away_tactics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    league_context = get_league_context_snapshot()
    game_date_str = game_date or date.today().isoformat()
    game_id = f"single_{home_team_id}_{away_team_id}_{uuid4().hex[:8]}"

    context = build_context_from_team_ids(
        game_id,
        game_date_str,
        home_team_id,
        away_team_id,
        league_context,
        phase="regular",
    )

    return _run_match(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        game_date=game_date_str,
        home_tactics=home_tactics,
        away_tactics=away_tactics,
        context=context,
    )
