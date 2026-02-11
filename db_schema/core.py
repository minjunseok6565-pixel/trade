# db_schema/core.py
"""SQLite SSOT schema: core league tables.

This module contains *only* DDL and schema migrations.
It must not import LeagueRepo (to avoid circular imports).

NOTE: This is a pure refactor split from league_repo.py.
The SQL and migration behavior must remain functionally identical.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping


# Signature compatible with LeagueRepo._ensure_table_columns(cur, table, columns)
EnsureColumnsFn = Callable[[sqlite3.Cursor, str, Mapping[str, str]], None]


def ddl(*, now: str, schema_version: str) -> str:
    """Return DDL SQL for core tables (as a single executescript string)."""
    return f"""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT INTO meta(key, value) VALUES ('schema_version', '{schema_version}')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', '{now}');

                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT PRIMARY KEY,
                    name TEXT,
                    pos TEXT,
                    age INTEGER,
                    height_in INTEGER,
                    weight_lb INTEGER,
                    ovr INTEGER,
                    attrs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS roster (
                    player_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    salary_amount INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_roster_team_id ON roster(team_id);

                CREATE TABLE IF NOT EXISTS contracts (
                    contract_id TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    start_season_id TEXT,
                    end_season_id TEXT,
                    salary_by_season_json TEXT,
                    contract_type TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_contracts_player_id ON contracts(player_id);
                CREATE INDEX IF NOT EXISTS idx_contracts_team_id ON contracts(team_id);

                -- Transactions log (SSOT)
                CREATE TABLE IF NOT EXISTS transactions_log (
                    tx_hash TEXT PRIMARY KEY,
                    tx_type TEXT NOT NULL,
                    tx_date TEXT,
                    season_year INTEGER,
                    deal_id TEXT,
                    source TEXT,
                    teams_json TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions_log(tx_date);

                -- Contract indices (legacy-compatible SSOT)
                CREATE TABLE IF NOT EXISTS player_contracts (
                    player_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    PRIMARY KEY(player_id, contract_id),
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE,
                    FOREIGN KEY(contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS active_contracts (
                    player_id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE,
                    FOREIGN KEY(contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS free_agents (
                    player_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE
                );
"""


def migrate(cur: sqlite3.Cursor, *, ensure_columns: EnsureColumnsFn) -> None:
    """Apply post-DDL schema migrations.

    This must remain functionally identical to the post-executescript section
    in LeagueRepo.init_db (ensure_table_columns + indices).
    """
    # Extend contracts table with full JSON storage (keeps contract shape stable across versions)
    ensure_columns(
        cur,
        "contracts",
        {
            "signed_date": "TEXT",
            "start_season_year": "INTEGER",
            "years": "INTEGER",
            "options_json": "TEXT",
            "status": "TEXT",
            "contract_json": "TEXT",
        },
    )

    # Ensure transactions_log has season_year as a first-class column (SSOT)
    # This keeps rule queries fast and avoids parsing payload_json for season filtering.
    ensure_columns(
        cur,
        "transactions_log",
        {
            "season_year": "INTEGER",
        },
    )

    # Indices for fast rule filtering (safe after ensuring columns exist).
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_season_year ON transactions_log(season_year);"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_season_type_date ON transactions_log(season_year, tx_type, tx_date);"
    )
