"""SQLite SSOT schema: draft tables.

This module contains only DDL (and optional migrations) for the draft subsystem.

Tables:
- draft_results: pick-level applied results (SSOT for idempotent/resumable draft execution)

Design notes:
- This table records *applied* outcomes (not mere selections), and must be written in the
  same transaction as the corresponding apply operations (players/roster/contracts/tx/...).
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping


# Signature compatible with LeagueRepo._ensure_table_columns(cur, table, columns)
EnsureColumnsFn = Callable[[sqlite3.Cursor, str, Mapping[str, str]], None]


def ddl(*, now: str, schema_version: str) -> str:
    """Return DDL SQL for draft tables."""
    _ = (now, schema_version)
    return """
                -- Draft pick results (applied outcomes; SSOT for idempotency)
                CREATE TABLE IF NOT EXISTS draft_results (
                    pick_id TEXT PRIMARY KEY,
                    draft_year INTEGER NOT NULL,
                    overall_no INTEGER NOT NULL,
                    round INTEGER NOT NULL,
                    slot INTEGER NOT NULL,
                    original_team TEXT NOT NULL,
                    drafting_team TEXT NOT NULL,
                    prospect_temp_id TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'draft',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Enforce one applied result per (draft_year, overall_no)
                CREATE UNIQUE INDEX IF NOT EXISTS uq_draft_results_year_overall
                    ON draft_results(draft_year, overall_no);

                CREATE INDEX IF NOT EXISTS idx_draft_results_year
                    ON draft_results(draft_year);
"""


def migrate(cur: sqlite3.Cursor, *, ensure_columns: EnsureColumnsFn) -> None:
    """Optional post-DDL migrations for draft tables.

    Currently no-op (initial version).
    Kept for forward compatibility.
    """
    _ = (cur, ensure_columns)
    return
