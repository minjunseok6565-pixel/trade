"""Contract option decision policies."""

from __future__ import annotations

from typing import Literal

Decision = Literal["EXERCISE", "DECLINE"]


def normalize_option_type(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized not in {"TEAM", "PLAYER", "ETO"}:
        raise ValueError(f"Invalid option type: {value}")
    return normalized


def default_option_decision_policy(
    option: dict,
    player_id: int,
    contract: dict,
    game_state: dict,
) -> Decision:
    """
    Minimal default policy for stability:
    - TEAM option: EXERCISE
    - PLAYER option: EXERCISE
    - ETO: EXERCISE
    """
    return "EXERCISE"
