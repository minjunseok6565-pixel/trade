from __future__ import annotations

"""
College subsystem package.

This package is intentionally DB-backed and separate from NBA state_schema,
so it can evolve without breaking league state snapshots.

Public API is exposed via college.service.
"""

from .service import (
    advance_offseason,
    ensure_world_bootstrapped,
    finalize_season_and_generate_entries,
    remove_drafted_player,
)

__all__ = [
    "ensure_world_bootstrapped",
    "advance_offseason",
    "finalize_season_and_generate_entries",
    "remove_drafted_player",
]
