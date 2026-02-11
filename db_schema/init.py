# db_schema/init.py
"""Public entrypoint for applying the SQLite schema.

NOTE: Pure refactor split from league_repo.py (no functional changes).
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from . import core, trade_assets, gm
from .registry import EnsureColumnsFn, apply_all


DEFAULT_MODULES = (core, trade_assets, gm)


def apply_schema(
    cur: sqlite3.Cursor,
    *,
    now: str,
    schema_version: str,
    ensure_columns: EnsureColumnsFn,
    modules: Iterable[object] = DEFAULT_MODULES,
) -> None:
    """Apply the schema and migrations.

    The default module order matches the intended split:
    core -> trade_assets -> gm
    """
    apply_all(
        cur,
        modules=modules,  # type: ignore[arg-type]
        now=now,
        schema_version=schema_version,
        ensure_columns=ensure_columns,
    )
