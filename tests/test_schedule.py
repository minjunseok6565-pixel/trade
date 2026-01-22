import pytest

pytest.importorskip("pandas")

from config import ALL_TEAM_IDS
import state as state_facade
from state_modules.state_core import ensure_league_block
from state_modules.state_schedule import _build_master_schedule
from state_modules.state_schema import build_default_state_v3
from state_modules.state_store import (
    DEFAULT_TRADE_RULES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
)


def _reset_schedule_state():
    state = build_default_state_v3(
        db_path="",
        default_trade_market=_DEFAULT_TRADE_MARKET,
        default_trade_memory=_DEFAULT_TRADE_MEMORY,
        default_trade_rules=DEFAULT_TRADE_RULES,
    )
    state["games"] = []
    state["player_stats"] = {}
    state["league"] = {
        "master_schedule": {"games": [], "by_team": {}, "by_date": {}},
        "trade_rules": {"trade_deadline": None},
        "season_year": None,
        "draft_year": None,
        "season_start": None,
        "current_date": None,
        "last_gm_tick_date": None,
    }
    state_facade.import_state(state)


def test_master_schedule_has_expected_game_counts():
    _reset_schedule_state()
    _build_master_schedule(2024)
    league = ensure_league_block()
    master = league["master_schedule"]

    assert len(master["games"]) == 1230
    assert all(len(master["by_team"].get(tid, [])) == 82 for tid in ALL_TEAM_IDS)


def test_home_away_balance_is_evenly_split():
    _reset_schedule_state()
    _build_master_schedule(2024)
    league = ensure_league_block()
    games = league["master_schedule"]["games"]

    home_counts = {tid: 0 for tid in ALL_TEAM_IDS}
    away_counts = {tid: 0 for tid in ALL_TEAM_IDS}

    for g in games:
        home_counts[g["home_team_id"]] += 1
        away_counts[g["away_team_id"]] += 1

    for tid in ALL_TEAM_IDS:
        diff = abs(home_counts[tid] - away_counts[tid])
        assert diff <= 2, f"Home/away split too uneven for {tid}: {diff}"
        assert home_counts[tid] + away_counts[tid] == 82
