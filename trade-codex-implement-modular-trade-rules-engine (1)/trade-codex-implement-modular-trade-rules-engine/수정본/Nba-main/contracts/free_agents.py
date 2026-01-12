"""Free agent helpers."""

from __future__ import annotations

from schema import normalize_player_id

FREE_AGENT_TEAM_ID = "FA"


def _normalize_player_id(value) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


def list_free_agents(game_state: dict) -> list[str]:
    return [str(player_id) for player_id in game_state.get("free_agents", [])]


def is_free_agent(game_state: dict, player_id: str) -> bool:
    normalized_player_id = _normalize_player_id(player_id)
    return normalized_player_id in game_state.get("free_agents", [])


def add_free_agent(game_state: dict, player_id: str) -> None:
    normalized_player_id = _normalize_player_id(player_id)
    free_agents = game_state.setdefault("free_agents", [])
    if normalized_player_id not in free_agents:
        free_agents.append(normalized_player_id)


def remove_free_agent(game_state: dict, player_id: str) -> None:
    normalized_player_id = _normalize_player_id(player_id)
    free_agents = game_state.get("free_agents", [])
    if normalized_player_id in free_agents:
        free_agents.remove(normalized_player_id)
