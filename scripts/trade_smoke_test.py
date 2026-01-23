from __future__ import annotations

import os
import sys
from datetime import timedelta
from typing import List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from config import ROSTER_DF
from state import (
    asset_locks_get,
    asset_locks_set,
    export_workflow_state,
    get_current_date_as_date,
    initialize_master_schedule_if_needed,
    trade_agreements_get,
    trade_agreements_set,
)
from team_utils import _init_players_and_teams_if_needed
from trades.apply import apply_deal
from trades.errors import TradeError, ASSET_LOCKED, DUPLICATE_ASSET, DEAL_INVALIDATED
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
    current_date = get_current_date_as_date()

    # Test A: basic 2-team player trade
    team_a, team_b = _pick_two_teams()
    player_a = _first_player_for_team(team_a)
    player_b = _first_player_for_team(team_b)

    tx_count = len(export_workflow_state().get("transactions", []))

    payload = {
        "teams": [team_a, team_b],
        "legs": {
            team_a: [{"kind": "player", "player_id": player_a}],
            team_b: [{"kind": "player", "player_id": player_b}],
        },
    }
    deal = canonicalize_deal(parse_deal(payload))
    validate_deal(deal, current_date=current_date)
    apply_deal(deal, source="menu", trade_date=current_date)

    assert str(ROSTER_DF.at[player_a, "Team"]).upper() == team_b
    assert str(ROSTER_DF.at[player_b, "Team"]).upper() == team_a
    assert len(export_workflow_state().get("transactions", [])) == tx_count + 1

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
    committed = agreements.create_committed_deal(deal_b, current_date=current_date)
    deal_verified = agreements.verify_committed_deal(
        committed["deal_id"], current_date=current_date
    )
    validate_deal(
        deal_verified,
        current_date=current_date,
        allow_locked_by_deal_id=committed["deal_id"],
    )
    apply_deal(
        deal_verified,
        source="menu",
        deal_id=committed["deal_id"],
        trade_date=current_date,
    )
    agreements.mark_executed(committed["deal_id"])

    for assets in deal_verified.legs.values():
        for asset in assets:
            lock_key = f"{asset.kind}:{getattr(asset, 'player_id', getattr(asset, 'pick_id', ''))}"
            assert lock_key not in asset_locks_get()

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
    committed_c = agreements.create_committed_deal(deal_c, current_date=current_date)

    try:
        validate_deal(deal_c, current_date=current_date)
        raise AssertionError("Expected lock conflict did not occur")
    except TradeError as exc:
        assert exc.code == ASSET_LOCKED

    agreements.release_locks_for_deal(committed_c["deal_id"])
    agreements_state = trade_agreements_get()
    entry = agreements_state.get(committed_c["deal_id"])
    if entry:
        entry["status"] = "INVALIDATED"
        agreements_state[committed_c["deal_id"]] = entry
        trade_agreements_set(agreements_state)

    # Test C2: duplicate asset validation
    payload_dup = {
        "teams": [team_a, team_b],
        "legs": {
            team_a: [
                {"kind": "player", "player_id": player_c},
                {"kind": "player", "player_id": player_c},
            ],
            team_b: [],
        },
    }
    deal_dup = canonicalize_deal(parse_deal(payload_dup))
    try:
        validate_deal(deal_dup)
        raise AssertionError("Expected duplicate asset error did not occur")
    except TradeError as exc:
        assert exc.code == DUPLICATE_ASSET

    # Test C3: expired lock should not block validation
    committed_expired = agreements.create_committed_deal(
        deal_c, current_date=current_date
    )
    asset_lock_key = f"player:{player_c}"
    locks_state = asset_locks_get()
    lock = locks_state.get(asset_lock_key)
    if lock:
        lock["expires_at"] = (get_current_date_as_date() - timedelta(days=1)).isoformat()
        locks_state[asset_lock_key] = lock
        asset_locks_set(locks_state)
    try:
        validate_deal(deal_c, current_date=current_date)
    except TradeError as exc:
        assert exc.code != ASSET_LOCKED
    agreements.release_locks_for_deal(committed_expired["deal_id"])

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
    validate_deal(deal_d, current_date=current_date)
    apply_deal(deal_d, source="menu", trade_date=current_date)

    assert str(ROSTER_DF.at[player_x, "Team"]).upper() == team_y
    assert str(ROSTER_DF.at[player_y, "Team"]).upper() == team_z
    assert str(ROSTER_DF.at[player_z, "Team"]).upper() == team_x

    # Test E: pick ownership in committed deal hash
    picks = export_workflow_state().get("draft_picks", {})
    pick_candidates = [
        pick for pick in picks.values() if pick.get("owner_team") == team_a
    ]
    if pick_candidates:
        pick_id = pick_candidates[0]["pick_id"]
        payload_pick = {
            "teams": [team_a, team_b],
            "legs": {
                team_a: [{"kind": "pick", "pick_id": pick_id}],
                team_b: [],
            },
        }
        deal_pick = canonicalize_deal(parse_deal(payload_pick))
        committed_pick = agreements.create_committed_deal(
            deal_pick, current_date=current_date
        )
        picks[pick_id]["owner_team"] = team_b
        try:
            agreements.verify_committed_deal(committed_pick["deal_id"])
            raise AssertionError("Expected invalidated deal due to pick ownership change")
        except TradeError as exc:
            assert exc.code == DEAL_INVALIDATED
        agreements.release_locks_for_deal(committed_pick["deal_id"])

    print("OK")


if __name__ == "__main__":
    main()
