"""SQLite SSOT schema: college league tables.

This module contains only DDL (and optional migrations) for the college subsystem.

Tables:
- college_teams
- college_players
- college_player_season_stats
- college_team_season_stats
- college_draft_entries
- draft_class_strength

Design notes:
- College stats are ephemeral by gameplay design (can be deleted once drafted).
- player_id namespace is shared with NBA players (promotion keeps the same player_id).
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping


# Signature compatible with LeagueRepo._ensure_table_columns(cur, table, columns)
EnsureColumnsFn = Callable[[sqlite3.Cursor, str, Mapping[str, str]], None]


def ddl(*, now: str, schema_version: str) -> str:
    """Return DDL SQL for college tables."""
    _ = (now, schema_version)
    return """
                -- College teams
                CREATE TABLE IF NOT EXISTS college_teams (
                    college_team_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    conference TEXT NOT NULL,
                    meta_json TEXT NOT NULL
                );

                -- College players (player_id namespace shared with NBA players)
                CREATE TABLE IF NOT EXISTS college_players (
                    player_id TEXT PRIMARY KEY,
                    college_team_id TEXT NOT NULL,
                    class_year INTEGER NOT NULL,
                    entry_season_year INTEGER NOT NULL,
                    status TEXT NOT NULL,

                    name TEXT NOT NULL,
                    pos TEXT NOT NULL,
                    age INTEGER NOT NULL,
                    height_in INTEGER NOT NULL,
                    weight_lb INTEGER NOT NULL,
                    ovr INTEGER NOT NULL,
                    attrs_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_college_players_team ON college_players(college_team_id);
                CREATE INDEX IF NOT EXISTS idx_college_players_status ON college_players(status);
                CREATE INDEX IF NOT EXISTS idx_college_players_entry ON college_players(entry_season_year);

                -- Player season stats (ephemeral; may be deleted when drafted)
                CREATE TABLE IF NOT EXISTS college_player_season_stats (
                    season_year INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    college_team_id TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    PRIMARY KEY (season_year, player_id)
                );

                CREATE INDEX IF NOT EXISTS idx_college_player_stats_season_team
                    ON college_player_season_stats(season_year, college_team_id);

                -- Team season stats
                CREATE TABLE IF NOT EXISTS college_team_season_stats (
                    season_year INTEGER NOT NULL,
                    college_team_id TEXT NOT NULL,
                    wins INTEGER NOT NULL,
                    losses INTEGER NOT NULL,
                    srs REAL NOT NULL,
                    pace REAL NOT NULL,
                    off_ppg REAL NOT NULL,
                    def_ppg REAL NOT NULL,
                    meta_json TEXT NOT NULL,
                    PRIMARY KEY (season_year, college_team_id)
                );

                -- Draft declarations (college -> NBA)
                CREATE TABLE IF NOT EXISTS college_draft_entries (
                    draft_year INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    declared_at TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    PRIMARY KEY (draft_year, player_id)
                );

                CREATE INDEX IF NOT EXISTS idx_college_entries_year ON college_draft_entries(draft_year);

                -- Per-year draft class strength (golden/bust class driver)
                CREATE TABLE IF NOT EXISTS draft_class_strength (
                    draft_year INTEGER PRIMARY KEY,
                    strength REAL NOT NULL,
                    seed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
"""


def migrate(cur: sqlite3.Cursor, *, ensure_columns: EnsureColumnsFn) -> None:
    """Optional post-DDL migrations for college tables.

    Currently no-op (initial version).
    Kept for forward compatibility.
    """
    _ = (cur, ensure_columns)
    return
