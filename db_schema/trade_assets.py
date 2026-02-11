# db_schema/trade_assets.py
"""SQLite SSOT schema: trade-asset tables.

This module contains only DDL for:
- draft_picks
- swap_rights
- fixed_assets

NOTE: Pure refactor split from league_repo.py (no functional changes).
"""

from __future__ import annotations


def ddl(*, now: str, schema_version: str) -> str:
    """Return DDL SQL for trade-asset tables."""
    # now/schema_version are kept in the signature for uniformity across modules.
    _ = (now, schema_version)
    return """

                -- Draft picks (SSOT)
                CREATE TABLE IF NOT EXISTS draft_picks (
                    pick_id TEXT PRIMARY KEY,
                    year INTEGER NOT NULL,
                    round INTEGER NOT NULL,
                    original_team TEXT NOT NULL,
                    owner_team TEXT NOT NULL,
                    protection_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_draft_picks_owner ON draft_picks(owner_team);
                CREATE INDEX IF NOT EXISTS idx_draft_picks_year_round ON draft_picks(year, round);

                -- Swap rights (SSOT)
                CREATE TABLE IF NOT EXISTS swap_rights (
                    swap_id TEXT PRIMARY KEY,
                    pick_id_a TEXT NOT NULL,
                    pick_id_b TEXT NOT NULL,
                    year INTEGER,
                    round INTEGER,
                    owner_team TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by_deal_id TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_swap_rights_owner ON swap_rights(owner_team);
                CREATE INDEX IF NOT EXISTS idx_swap_rights_year_round ON swap_rights(year, round);

                -- Fixed assets (SSOT)
                CREATE TABLE IF NOT EXISTS fixed_assets (
                    asset_id TEXT PRIMARY KEY,
                    label TEXT,
                    value REAL,
                    owner_team TEXT NOT NULL,
                    source_pick_id TEXT,
                    draft_year INTEGER,
                    attrs_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fixed_assets_owner ON fixed_assets(owner_team);
"""
