"""Contract option decision policies."""

from __future__ import annotations

from typing import Literal

from schema import normalize_player_id

Decision = Literal["EXERCISE", "DECLINE"]


def normalize_option_type(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized not in {"TEAM", "PLAYER", "ETO"}:
        raise ValueError(f"Invalid option type: {value}")
    return normalized


def default_option_decision_policy(
    option: dict,
    player_id: str,
    contract: dict,
    game_state: dict,
) -> Decision:
    """
    Minimal default policy for stability:
    - TEAM option: EXERCISE
    - PLAYER option: EXERCISE
    - ETO: EXERCISE
    """
    normalize_player_id(player_id, strict=False, allow_legacy_numeric=True)
    return "EXERCISE"
