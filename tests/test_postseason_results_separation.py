from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from state_modules.state_store import reset_state_for_dev  # noqa: E402
import state  # noqa: E402


def test_postseason_results_separation() -> None:
    reset_state_for_dev()
    state.validate_state()

    state.postseason_set_playoffs({"dummy": True})

    game_result = {
        "schema_version": "2.0",
        "phase": "playoffs",
        "game_id": "game-1",
        "game": {
            "game_id": "game-1",
            "date": "2024-06-01",
            "season_id": "2024",
            "phase": "playoffs",
            "home_team_id": "1",
            "away_team_id": "2",
            "overtime_periods": 0,
            "possessions_per_team": 100,
        },
        "final": {"1": 101, "2": 99},
        "teams": {
            "1": {
                "totals": {"PTS": 101},
                "players": [{"PlayerID": "p1", "TeamID": "1", "PTS": 10}],
            },
            "2": {
                "totals": {"PTS": 99},
                "players": [{"PlayerID": "p2", "TeamID": "2", "PTS": 8}],
            },
        },
    }

    state.ingest_game_result(game_result)

    workflow_state = state.export_workflow_state()
    playoff_results = workflow_state["phase_results"]["playoffs"]["game_results"]
    assert playoff_results

    postseason_snapshot = state.get_postseason_snapshot()
    assert "games" not in postseason_snapshot
