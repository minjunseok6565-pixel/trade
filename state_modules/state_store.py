from __future__ import annotations

from typing import Any, Dict

from state_schema import create_default_game_state, validate_game_state

# -------------------------------------------------------------------------
# 1. 전역 상태 및 스케줄/리그 상태 유틸
# -------------------------------------------------------------------------
_STATE: Dict[str, Any] = create_default_game_state()
validate_game_state(_STATE)


def get_state() -> Dict[str, Any]:
    return _STATE


def reset_game_state() -> Dict[str, Any]:
    _STATE.clear()
    _STATE.update(create_default_game_state())
    validate_game_state(_STATE)
    return _STATE
