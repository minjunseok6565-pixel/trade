import math

import pytest

pytest.importorskip("pandas")

import pandas as pd

from contracts import models
from contracts import sync


def test_nan_salary_is_sanitized_and_flagged():
    contract_id = models.new_contract_id()
    contract = models.make_contract_record(
        contract_id=contract_id,
        player_id=1,
        team_id="FA",
        signed_date_iso="2025-01-01",
        start_season_year=2025,
        years=1,
        salary_by_year={"2025": float("nan")},
    )

    assert models.get_active_salary_for_season(contract, 2025) == 0.0

    game_state = {
        "players": {1: {"salary": 0.0, "team_id": "FA"}},
        "contracts": {contract_id: contract},
        "active_contract_id_by_player": {"1": contract_id},
    }
    roster_df = pd.DataFrame({"SalaryAmount": [float("nan")], "Team": ["FA"]}, index=[1])

    sync.sync_players_salary_from_active_contract(game_state, 2025)
    assert game_state["players"][1]["salary"] == 0.0
    assert not math.isnan(game_state["players"][1]["salary"])

    sync.sync_roster_salaries_for_season(game_state, 2025, roster_df=roster_df)
    assert roster_df.at[1, "SalaryAmount"] == 0.0
    assert not math.isnan(roster_df.at[1, "SalaryAmount"])

    with pytest.raises(AssertionError, match="Contract salary is NaN"):
        sync.assert_state_vs_roster_consistency(
            game_state, season_year=2025, roster_df=roster_df
        )
