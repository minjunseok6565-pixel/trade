import pytest

pytest.importorskip("pandas")

from config import ALL_TEAM_IDS, HARD_CAP
from state import GAME_STATE, _build_master_schedule, _ensure_league_state


def _reset_schedule_state():
    GAME_STATE["games"] = []
    GAME_STATE["player_stats"] = {}
    GAME_STATE["league"] = {
        "master_schedule": {"games": [], "by_team": {}, "by_date": {}},
        "trade_rules": {"hard_cap": HARD_CAP, "trade_deadline": None},
        "season_year": None,
        "season_start": None,
        "current_date": None,
        "last_gm_tick_date": None,
    }


def test_master_schedule_has_expected_game_counts():
    _reset_schedule_state()
    _build_master_schedule(2024)
    league = _ensure_league_state()
    master = league["master_schedule"]

    assert len(master["games"]) == 1230
    assert all(len(master["by_team"].get(tid, [])) == 82 for tid in ALL_TEAM_IDS)


def test_home_away_balance_is_evenly_split():
    _reset_schedule_state()
    _build_master_schedule(2024)
    league = _ensure_league_state()
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
