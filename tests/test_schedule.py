import pytest

pytest.importorskip("pandas")

from config import ALL_TEAM_IDS
from state import build_master_schedule, get_league_snapshot, reset_state_for_dev, set_league_state


def _reset_schedule_state():
    reset_state_for_dev()
    league = get_league_snapshot()
    league["master_schedule"] = {"games": [], "by_team": {}, "by_date": {}, "by_id": {}}
    league["trade_rules"] = {"trade_deadline": None}
    league["season_year"] = None
    league["draft_year"] = None
    league["season_start"] = None
    league["current_date"] = None
    league["last_gm_tick_date"] = None
    set_league_state(league)


def test_master_schedule_has_expected_game_counts():
    _reset_schedule_state()
    build_master_schedule(2024)
    league = get_league_snapshot()
    master = league["master_schedule"]

    assert len(master["games"]) == 1230
    assert all(len(master["by_team"].get(tid, [])) == 82 for tid in ALL_TEAM_IDS)


def test_home_away_balance_is_evenly_split():
    _reset_schedule_state()
    build_master_schedule(2024)
    league = get_league_snapshot()
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
