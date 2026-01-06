"""Free agent helpers."""

from __future__ import annotations

from contracts.store import ensure_contract_state

FREE_AGENT_TEAM_ID = "FA"


def list_free_agents(game_state: dict) -> list[int]:
    ensure_contract_state(game_state)
    return list(game_state["free_agents"])


def is_free_agent(game_state: dict, player_id: int) -> bool:
    ensure_contract_state(game_state)
    return player_id in game_state["free_agents"]


def add_free_agent(game_state: dict, player_id: int) -> None:
    ensure_contract_state(game_state)
    if player_id not in game_state["free_agents"]:
        game_state["free_agents"].append(player_id)


def remove_free_agent(game_state: dict, player_id: int) -> None:
    ensure_contract_state(game_state)
    if player_id in game_state["free_agents"]:
        game_state["free_agents"].remove(player_id)
