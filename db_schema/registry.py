# db_schema/registry.py
"""Schema registry + applier.

This module orchestrates applying DDL (via executescript) and post-DDL migrations.

NOTE: Pure refactor split from league_repo.py (no functional changes).
"""

from __future__ import annotations

import sqlite3
from types import ModuleType
from typing import Callable, Iterable, Mapping


# Signature compatible with LeagueRepo._ensure_table_columns(cur, table, columns)
EnsureColumnsFn = Callable[[sqlite3.Cursor, str, Mapping[str, str]], None]


def apply_all(
    cur: sqlite3.Cursor,
    *,
    modules: Iterable[ModuleType],
    now: str,
    schema_version: str,
    ensure_columns: EnsureColumnsFn,
) -> None:
    """Apply schema modules.

    Steps:
    1) executescript(concat(ddl))
    2) run migrate() for modules that define it

    This preserves the original LeagueRepo.init_db behavior:
    - big executescript first
    - then ensure_table_columns + index creation
    """
    ddl_parts = [m.ddl(now=now, schema_version=schema_version) for m in modules]
    cur.executescript("\n\n".join(ddl_parts))

    for m in modules:
        migrate = getattr(m, "migrate", None)
        if migrate is None:
            continue
        migrate(cur, ensure_columns=ensure_columns)
