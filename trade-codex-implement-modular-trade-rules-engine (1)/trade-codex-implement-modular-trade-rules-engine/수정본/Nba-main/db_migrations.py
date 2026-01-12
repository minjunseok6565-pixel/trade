from __future__ import annotations

import datetime as _dt
import sqlite3

from schema import SCHEMA_VERSION

LATEST_DB_SCHEMA_VERSION = 2


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version;").fetchone()
    return int(row[0] if row else 0)


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)};")


def migrate_db_schema(conn: sqlite3.Connection) -> None:
    current = get_user_version(conn)
    if current == LATEST_DB_SCHEMA_VERSION:
        _ensure_meta_schema_version(conn, current)
        return

    while current < LATEST_DB_SCHEMA_VERSION:
        if current == 0:
            _migration_1(conn)
            current = 1
            set_user_version(conn, current)
            _ensure_meta_schema_version(conn, current)
        elif current == 1:
            _migration_2(conn)
            current = 2
            set_user_version(conn, current)
            _ensure_meta_schema_version(conn, current)
        else:
            raise RuntimeError(f"Unsupported DB schema version: {current}")


def _ensure_meta_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (SCHEMA_VERSION,),
    )
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('db_schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (str(version),),
    )


def _migration_1(conn: sqlite3.Connection) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (SCHEMA_VERSION,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?);
        """,
        (now,),
    )

    conn.executescript(
        """
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

        CREATE TABLE IF NOT EXISTS teams (
            team_id TEXT PRIMARY KEY,
            name TEXT,
            attrs_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS roster (
            player_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            salary_amount INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE,
            FOREIGN KEY(team_id) REFERENCES teams(team_id) ON DELETE RESTRICT
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
        """
    )

    _ensure_roster_team_fk(conn)
    _backfill_teams_from_roster(conn)
    _ensure_team_fa(conn)


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS draft_picks (
            pick_id TEXT PRIMARY KEY,
            year INTEGER NOT NULL,
            round INTEGER NOT NULL CHECK(round IN (1,2)),
            original_team_id TEXT NOT NULL,
            owner_team_id TEXT NOT NULL,
            protection_json TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(original_team_id) REFERENCES teams(team_id) ON DELETE RESTRICT,
            FOREIGN KEY(owner_team_id) REFERENCES teams(team_id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_picks_owner_year_round
            ON draft_picks(owner_team_id, year, round);
        CREATE INDEX IF NOT EXISTS idx_picks_original_year_round
            ON draft_picks(original_team_id, year, round);
        CREATE INDEX IF NOT EXISTS idx_picks_year_round
            ON draft_picks(year, round);

        CREATE TABLE IF NOT EXISTS swap_rights (
            swap_id TEXT PRIMARY KEY,
            pick_id_a TEXT NOT NULL,
            pick_id_b TEXT NOT NULL,
            year INTEGER NOT NULL,
            round INTEGER NOT NULL CHECK(round IN (1,2)),
            owner_team_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_by_deal_id TEXT NULL,
            pick_pair_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(pick_id_a) REFERENCES draft_picks(pick_id),
            FOREIGN KEY(pick_id_b) REFERENCES draft_picks(pick_id),
            FOREIGN KEY(owner_team_id) REFERENCES teams(team_id) ON DELETE RESTRICT,
            UNIQUE(pick_pair_key)
        );

        CREATE INDEX IF NOT EXISTS idx_swaps_owner_year_round
            ON swap_rights(owner_team_id, year, round);

        CREATE TABLE IF NOT EXISTS fixed_assets (
            asset_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            value REAL NOT NULL,
            owner_team_id TEXT NOT NULL,
            source_pick_id TEXT NULL,
            draft_year INTEGER NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(owner_team_id) REFERENCES teams(team_id) ON DELETE RESTRICT,
            FOREIGN KEY(source_pick_id) REFERENCES draft_picks(pick_id)
        );

        CREATE INDEX IF NOT EXISTS idx_fixed_assets_owner
            ON fixed_assets(owner_team_id);
        CREATE INDEX IF NOT EXISTS idx_fixed_assets_draft_year
            ON fixed_assets(draft_year);
        """
    )


def _ensure_roster_team_fk(conn: sqlite3.Connection) -> None:
    fk_rows = conn.execute("PRAGMA foreign_key_list(roster);").fetchall()
    has_fk = any(row[2] == "teams" and row[3] == "team_id" for row in fk_rows)
    if has_fk:
        return

    roster_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='roster';"
    ).fetchone()
    if not roster_exists:
        return

    conn.executescript(
        """
        CREATE TABLE roster_new (
            player_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            salary_amount INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE CASCADE,
            FOREIGN KEY(team_id) REFERENCES teams(team_id) ON DELETE RESTRICT
        );

        INSERT INTO roster_new(player_id, team_id, salary_amount, status, updated_at)
        SELECT player_id, team_id, salary_amount, status, updated_at FROM roster;

        DROP TABLE roster;
        ALTER TABLE roster_new RENAME TO roster;

        CREATE INDEX IF NOT EXISTS idx_roster_team_id ON roster(team_id);
        """
    )


def _backfill_teams_from_roster(conn: sqlite3.Connection) -> None:
    now = _utc_now_iso()
    roster_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='roster';"
    ).fetchone()
    if not roster_exists:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO teams(team_id, name, attrs_json, created_at, updated_at)
        SELECT DISTINCT team_id, NULL, '{}', ?, ?
        FROM roster
        WHERE team_id IS NOT NULL AND TRIM(team_id) != '';
        """,
        (now, now),
    )


def _ensure_team_fa(conn: sqlite3.Connection) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO teams(team_id, name, attrs_json, created_at, updated_at)
        VALUES ('FA', 'Free Agent', '{}', ?, ?);
        """,
        (now, now),
    )
