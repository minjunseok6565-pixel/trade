"""Contract bootstrap utilities (DB SSOT).

After migration, contracts live only in the SQLite DB.
This module must NOT create or depend on legacy GAME_STATE ledgers:
  - contracts / player_contracts / active_contract_id_by_player / free_agents

Supported flows:
  1) Ensure contracts exist for active roster players (non-FA)
  2) (Admin) Import roster Excel into DB, then bootstrap contracts
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from league_service import LeagueService


def _get_db_path(game_state: dict) -> str:
    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required")
    return str(db_path)


def _get_season_year(game_state: dict, season_year: int | None) -> int:
    if season_year is not None:
        y = int(season_year)
        if y <= 0:
            raise ValueError("season_year must be a positive int")
        return y
    league_state = game_state.get("league") or {}
    y = int(league_state.get("season_year") or 0)
    if y <= 0:
        raise ValueError(
            "game_state['league']['season_year'] is required when season_year is not provided"
        )
    return y


def _clear_contract_tables_in_place(svc: LeagueService) -> None:
    """Clear contract-related DB tables (does not touch roster/players)."""
    repo = svc.repo
    with repo.transaction() as cur:
        # Order matters due to foreign keys / references.
        for table in ("active_contracts", "player_contracts", "free_agents", "contracts"):
            try:
                cur.execute(f"DELETE FROM {table};")
            except Exception:
                # Be resilient if a table is absent in older DBs.
                pass


def _count_contracts(svc: LeagueService) -> int:
    repo = svc.repo
    with repo.transaction() as cur:
        row = cur.execute("SELECT COUNT(*) AS c FROM contracts;").fetchone()
        return int(row["c"] or 0) if row else 0


def bootstrap_contracts_from_roster(
    game_state: dict,
    *,
    season_year: int | None = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Ensure DB contracts exist for active roster players (non-FA).

    - DB is the source of truth.
    - This function does NOT touch any legacy state-ledger keys.
    - Rebuilds contract indices (player_contracts / active_contracts / free_agents) after bootstrapping.
    """
    db_path = _get_db_path(game_state)
    season_year_i = _get_season_year(game_state, season_year)

    with LeagueService.open(db_path) as svc:
        svc.repo.init_db()

        if overwrite:
            _clear_contract_tables_in_place(svc)

        before = _count_contracts(svc)
        svc.ensure_contracts_bootstrapped_from_roster(season_year_i)
        after = _count_contracts(svc)

        created = max(0, int(after) - int(before))

        # Ensure derived/index tables are consistent with contracts.
        svc.repo.rebuild_contract_indices()
        svc.repo.validate_integrity()

    return {
        "skipped": (created == 0 and not overwrite),
        "created_contracts": created,
        "season_year": season_year_i,
        "overwrite": bool(overwrite),
    }


def import_roster_and_bootstrap_contracts(
    game_state: dict,
    *,
    excel_path: str,
    season_year: int | None = None,
    mode: str = "replace",
    sheet_name: Optional[str] = None,
    strict_ids: bool = True,
    overwrite_contracts: bool = False,
) -> Dict[str, Any]:
    """Admin pipeline: import roster Excel -> DB -> bootstrap contracts.

    Notes:
      - LeagueRepo.import_roster_excel(mode='replace') clears roster/contracts/players.
      - If overwrite_contracts=True and mode != 'replace', contracts tables are cleared before bootstrapping.
    """
    db_path = _get_db_path(game_state)
    season_year_i = _get_season_year(game_state, season_year)

    mode_norm = str(mode or "replace").strip().lower()
    # Allow legacy 'merge' as a synonym for 'upsert' (safe default).
    if mode_norm == "merge":
        mode_norm = "upsert"
    if mode_norm not in {"replace", "upsert"}:
        raise ValueError("mode must be one of: 'replace', 'upsert' (or legacy 'merge')")

    with LeagueService.open(db_path) as svc:
        svc.repo.init_db()

        svc.import_roster_from_excel(
            excel_path,
            mode=mode_norm,
            sheet_name=sheet_name,
            strict_ids=bool(strict_ids),
        )

        if overwrite_contracts and mode_norm != "replace":
            _clear_contract_tables_in_place(svc)

        before = _count_contracts(svc)
        svc.ensure_contracts_bootstrapped_from_roster(season_year_i)
        after = _count_contracts(svc)
        created = max(0, int(after) - int(before))

        svc.repo.rebuild_contract_indices()
        svc.repo.validate_integrity()

    return {
        "import": {
            "excel_path": excel_path,
            "mode": mode_norm,
            "sheet_name": sheet_name,
            "strict_ids": bool(strict_ids),
        },
        "bootstrap": {
            "skipped": (created == 0 and not overwrite_contracts),
            "created_contracts": created,
            "season_year": season_year_i,
            "overwrite": bool(overwrite_contracts),
        },
    }
