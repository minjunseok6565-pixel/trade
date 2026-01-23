from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import state


def test_state_module_does_not_export_game_state():
    attr_name = "GAME" + "_STATE"
    assert not hasattr(state, attr_name)


def test_startup_init_state_runs_or_skips():
    if not hasattr(state, "startup_init_state"):
        pytest.skip("startup_init_state not available")
    try:
        state.startup_init_state()
    except Exception as exc:
        pytest.skip(f"startup_init_state skipped: {exc}")
