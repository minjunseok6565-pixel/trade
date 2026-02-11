# db_schema/gm.py
"""SQLite SSOT schema: AI GM tables.

NOTE: Pure refactor split from league_repo.py (no functional changes).
"""

from __future__ import annotations


def ddl(*, now: str, schema_version: str) -> str:
    """Return DDL SQL for AI GM profile tables."""
    _ = (schema_version,)
    return f"""

                -- AI GM profiles (team_id -> JSON blob)
                CREATE TABLE IF NOT EXISTS gm_profiles (
                    team_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
