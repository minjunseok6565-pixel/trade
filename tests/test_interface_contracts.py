import unittest


from state_modules.state_results import validate_v2_game_result
from state_modules.state_schedule import validate_master_schedule_entry


def _minimal_valid_v2_game_result():
    # 최소 계약만 만족하는 v2 결과 샘플
    return {
        "schema_version": "2.0",
        "game": {
            "game_id": "2026-01-12_BOS_LAL",
            "date": "2026-01-12",
            "season_id": "2025-26",
            "phase": "regular",
            "home_team_id": "BOS",
            "away_team_id": "LAL",
            "overtime_periods": 0,
            "possessions_per_team": 100,
        },
        "final": {"BOS": 110, "LAL": 105},
        "teams": {
            "BOS": {
                "totals": {"PTS": 110},
                "players": [
                    {"PlayerID": "player_000001", "TeamID": "BOS", "PTS": 25},
                ],
                "breakdowns": {},
            },
            "LAL": {
                "totals": {"PTS": 105},
                "players": [
                    {"PlayerID": "player_000002", "TeamID": "LAL", "PTS": 30},
                ],
                "breakdowns": {},
            },
        },
    }


class TestInterfaceContracts(unittest.TestCase):
    def test_validate_v2_game_result_ok(self):
        validate_v2_game_result(_minimal_valid_v2_game_result())

    def test_validate_v2_game_result_missing_key_fails(self):
        bad = _minimal_valid_v2_game_result()
        del bad["game"]["season_id"]
        with self.assertRaises(ValueError):
            validate_v2_game_result(bad)

    def test_validate_v2_game_result_final_missing_team_fails(self):
        bad = _minimal_valid_v2_game_result()
        bad["final"].pop("LAL")
        with self.assertRaises(ValueError):
            validate_v2_game_result(bad)

    def test_validate_v2_game_result_players_teamid_mismatch_fails(self):
        bad = _minimal_valid_v2_game_result()
        bad["teams"]["BOS"]["players"][0]["TeamID"] = "LAL"
        with self.assertRaises(ValueError):
            validate_v2_game_result(bad)

    def test_validate_master_schedule_entry_ok(self):
        entry = {
            "game_id": "2026-01-12_BOS_LAL",
            "home_team_id": "BOS",
            "away_team_id": "LAL",
            "status": "scheduled",
            "date": "2026-01-12",
        }
        validate_master_schedule_entry(entry, path="master_schedule.games[0]")

    def test_validate_master_schedule_entry_missing_key_fails(self):
        entry = {
            "home_team_id": "BOS",
            "away_team_id": "LAL",
            "status": "scheduled",
        }
        with self.assertRaises(ValueError):
            validate_master_schedule_entry(entry)

    def test_validate_master_schedule_entry_bad_status_fails(self):
        entry = {
            "game_id": "x",
            "home_team_id": "BOS",
            "away_team_id": "LAL",
            "status": "WTF",
        }
        with self.assertRaises(ValueError):
            validate_master_schedule_entry(entry)


if __name__ == "__main__":
    unittest.main()
