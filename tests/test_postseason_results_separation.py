from datetime import datetime, timezone
from pathlib import Path
import json
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from league_repo import LeagueRepo
from state import (
    export_workflow_state,
    get_postseason_snapshot,
    ingest_game_result,
    postseason_set_playoffs,
    reset_state_for_dev,
    set_db_path,
    startup_init_state,
)


def _seed_minimal_roster(db_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with LeagueRepo(str(db_path)) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO players(
                    player_id,
                    name,
                    pos,
                    age,
                    height_in,
                    weight_lb,
                    ovr,
                    attrs_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "P000001",
                    "Player One",
                    "G",
                    25,
                    72,
                    190,
                    50,
                    json.dumps({"Name": "Player One"}),
                    now,
                    now,
                ),
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO roster(
                    player_id,
                    team_id,
                    salary_amount,
                    status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?);
                """,
                ("P000001", "ATL", 0, "active", now),
            )


def test_postseason_results_separated_from_phase_results(tmp_path: Path):
    reset_state_for_dev()
    db_path = tmp_path / "league.db"
    _seed_minimal_roster(db_path)
    set_db_path(str(db_path))
    startup_init_state()

    postseason_set_playoffs({"dummy": True})

    game_result = {
        "game_id": "playoffs-1",
        "phase": "playoffs",
        "game": {
            "game_id": "playoffs-1",
            "date": "2024-06-01",
            "season_id": "2024",
            "phase": "playoffs",
            "home_team_id": "T1",
            "away_team_id": "T2",
            "overtime_periods": 0,
            "possessions_per_team": 90,
        },
        "final": {"T1": 100, "T2": 90},
        "teams": {
            "T1": {
                "totals": {"PTS": 100},
                "players": [{"PlayerID": "P1", "TeamID": "T1", "Name": "Player 1"}],
            },
            "T2": {
                "totals": {"PTS": 90},
                "players": [{"PlayerID": "P2", "TeamID": "T2", "Name": "Player 2"}],
            },
        },
    }

    ingest_game_result(game_result=game_result)

    workflow_state = export_workflow_state()
    phase_results = workflow_state["phase_results"]
    assert phase_results["playoffs"]["game_results"]

    postseason = get_postseason_snapshot()
    assert "games" not in postseason
