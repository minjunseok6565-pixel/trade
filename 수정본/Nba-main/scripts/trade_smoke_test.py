from __future__ import annotations

from typing import List, Tuple

from config import ROSTER_DF
from state import GAME_STATE, initialize_master_schedule_if_needed
from team_utils import _init_players_and_teams_if_needed
from trades.apply import apply_deal
from trades.errors import TradeError, ASSET_LOCKED
from trades.models import canonicalize_deal, parse_deal
from trades.validator import validate_deal
from trades import agreements


def _teams_with_players() -> List[str]:
    team_counts = ROSTER_DF["Team"].value_counts()
    return [team for team, count in team_counts.items() if count > 0]


def _first_player_for_team(team_id: str) -> int:
    roster = ROSTER_DF[ROSTER_DF["Team"] == team_id]
    if roster.empty:
        raise RuntimeError(f"No players for team {team_id}")
    return int(roster.index[0])


def _pick_two_teams() -> Tuple[str, str]:
    teams = _teams_with_players()
    if len(teams) < 2:
        raise RuntimeError("Need at least two teams with players")
    return teams[0], teams[1]


def _pick_three_teams() -> Tuple[str, str, str]:
    teams = _teams_with_players()
    if len(teams) < 3:
        raise RuntimeError("Need at least three teams with players")
    return teams[0], teams[1], teams[2]


def main() -> None:
    _init_players_and_teams_if_needed()
    initialize_master_schedule_if_needed()

    # Test A: basic 2-team player trade
    team_a, team_b = _pick_two_teams()
    player_a = _first_player_for_team(team_a)
    player_b = _first_player_for_team(team_b)

    tx_count = len(GAME_STATE.get("transactions", []))

    payload = {
        "teams": [team_a, team_b],
        "legs": {
            team_a: [{"kind": "player", "player_id": player_a}],
            team_b: [{"kind": "player", "player_id": player_b}],
        },
    }
    deal = canonicalize_deal(parse_deal(payload))
    validate_deal(deal)
    apply_deal(deal, source="menu")

    assert str(ROSTER_DF.at[player_a, "Team"]).upper() == team_b
    assert str(ROSTER_DF.at[player_b, "Team"]).upper() == team_a
    assert len(GAME_STATE.get("transactions", [])) == tx_count + 1

    # Test B: committed deal flow
    player_a2 = _first_player_for_team(team_a)
    player_b2 = _first_player_for_team(team_b)
    payload_b = {
        "teams": [team_a, team_b],
        "legs": {
            team_a: [{"kind": "player", "player_id": player_a2}],
            team_b: [{"kind": "player", "player_id": player_b2}],
        },
    }
    deal_b = canonicalize_deal(parse_deal(payload_b))
    committed = agreements.create_committed_deal(deal_b)
    deal_verified = agreements.verify_committed_deal(committed["deal_id"])
    validate_deal(deal_verified, allow_locked_by_deal_id=committed["deal_id"])
    apply_deal(deal_verified, source="menu", deal_id=committed["deal_id"])
    agreements.mark_executed(committed["deal_id"])

    for assets in deal_verified.legs.values():
        for asset in assets:
            lock_key = f"{asset.kind}:{getattr(asset, 'player_id', getattr(asset, 'pick_id', ''))}"
            assert lock_key not in GAME_STATE.get("asset_locks", {})

    # Test C: lock conflict
    player_c = _first_player_for_team(team_a)
    payload_c = {
        "teams": [team_a, team_b],
        "legs": {
            team_a: [{"kind": "player", "player_id": player_c}],
            team_b: [],
        },
    }
    deal_c = canonicalize_deal(parse_deal(payload_c))
    committed_c = agreements.create_committed_deal(deal_c)

    try:
        validate_deal(deal_c)
        raise AssertionError("Expected lock conflict did not occur")
    except TradeError as exc:
        assert exc.code == ASSET_LOCKED

    agreements.release_locks_for_deal(committed_c["deal_id"])
    entry = GAME_STATE.get("trade_agreements", {}).get(committed_c["deal_id"])
    if entry:
        entry["status"] = "INVALIDATED"

    # Test D: multi-team 3-team trade
    team_x, team_y, team_z = _pick_three_teams()
    player_x = _first_player_for_team(team_x)
    player_y = _first_player_for_team(team_y)
    player_z = _first_player_for_team(team_z)

    payload_d = {
        "teams": [team_x, team_y, team_z],
        "legs": {
            team_x: [{"kind": "player", "player_id": player_x, "to_team": team_y}],
            team_y: [{"kind": "player", "player_id": player_y, "to_team": team_z}],
            team_z: [{"kind": "player", "player_id": player_z, "to_team": team_x}],
        },
    }
    deal_d = canonicalize_deal(parse_deal(payload_d))
    validate_deal(deal_d)
    apply_deal(deal_d, source="menu")

    assert str(ROSTER_DF.at[player_x, "Team"]).upper() == team_y
    assert str(ROSTER_DF.at[player_y, "Team"]).upper() == team_z
    assert str(ROSTER_DF.at[player_z, "Team"]).upper() == team_x

    print("OK")


if __name__ == "__main__":
    main()
