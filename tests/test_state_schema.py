from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from state_schema import create_default_state, validate_game_state  # noqa: E402


def test_default_state_validates() -> None:
    state = create_default_state()
    validate_game_state(state)


def test_unknown_top_level_key_rejected() -> None:
    state = create_default_state()
    state["unexpected"] = True
    with pytest.raises(ValueError, match="Unknown top-level key"):
        validate_game_state(state)
