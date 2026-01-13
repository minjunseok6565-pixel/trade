"""Free agent helpers."""

from __future__ import annotations

from contracts.store import ensure_contract_state
from schema import normalize_player_id

FREE_AGENT_TEAM_ID = "FA"


def _normalize_player_id(value) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


def list_free_agents(game_state: dict) -> list[str]:
    ensure_contract_state(game_state)
    return [str(player_id) for player_id in game_state["free_agents"]]


def is_free_agent(game_state: dict, player_id: str) -> bool:
    ensure_contract_state(game_state)
    normalized_player_id = _normalize_player_id(player_id)
    return normalized_player_id in game_state["free_agents"]


def add_free_agent(game_state: dict, player_id: str) -> None:
    ensure_contract_state(game_state)
    normalized_player_id = _normalize_player_id(player_id)
    if normalized_player_id not in game_state["free_agents"]:
        game_state["free_agents"].append(normalized_player_id)


def remove_free_agent(game_state: dict, player_id: str) -> None:
    ensure_contract_state(game_state)
    normalized_player_id = _normalize_player_id(player_id)
    if normalized_player_id in game_state["free_agents"]:
        game_state["free_agents"].remove(normalized_player_id)
