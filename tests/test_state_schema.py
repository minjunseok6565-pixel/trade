from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from state_schema import create_default_state, validate_game_state


def test_default_state_validates():
    state = create_default_state()
    validate_game_state(state)


def test_unknown_key_rejected():
    state = create_default_state()
    state["unknown_key"] = True
    with pytest.raises(ValueError, match="Unknown top-level key"):
        validate_game_state(state)
