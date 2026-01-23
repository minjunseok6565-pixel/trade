from __future__ import annotations

from config import ALL_TEAM_IDS
from state_cap import _apply_cap_model_for_season
from state_migrations import normalize_player_ids

_DB_INIT_PATHS: set[str] = set()
_REPO_INTEGRITY_VALIDATED: set[str] = set()
_CONTRACTS_BOOTSTRAPPED: set[str] = set()


def ensure_db_initialized_and_seeded(state: dict) -> None:
    """Ensure LeagueRepo is initialized and GM profiles are seeded (startup-only)."""
    league = state.get("league") or {}
    db_path = str(league.get("db_path") or "league.db")
    if db_path in _DB_INIT_PATHS:
        return

    from league_repo import LeagueRepo

    with LeagueRepo(db_path) as repo:
        repo.init_db()
        # Keep rows ready for all teams (idempotent).
        repo.ensure_gm_profiles_seeded(ALL_TEAM_IDS)

    _DB_INIT_PATHS.add(db_path)


def ensure_cap_model_populated_if_needed(state: dict) -> None:
    """Populate cap/aprons in league.trade_rules if season_year is known and unset/zero."""
    league = state.get("league") or {}
    trade_rules = league.get("trade_rules") or {}
    season_year = league.get("season_year")
    salary_cap = trade_rules.get("salary_cap") if isinstance(trade_rules, dict) else None
    if not season_year:
        return
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return
    try:
        salary_cap_value = float(salary_cap or 0)
    except (TypeError, ValueError):
        salary_cap_value = 0.0
    if salary_cap_value <= 0:
        _apply_cap_model_for_season(league, season_year_int)


def ensure_player_ids_normalized(state: dict, *, allow_legacy_numeric: bool = True) -> dict:
    """Normalize player IDs (startup-only)."""
    return normalize_player_ids(state, allow_legacy_numeric=allow_legacy_numeric)


def ensure_contracts_bootstrapped_after_schedule_creation_once(state: dict) -> None:
    """Bootstrap contracts from roster once right after schedule creation (per season)."""
    league = state.get("league") or {}
    season_year = league.get("season_year")
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return

    season_key = str(season_year_int)
    if season_key in _CONTRACTS_BOOTSTRAPPED:
        return

    from league_repo import LeagueRepo

    db_path = str(league.get("db_path") or "league.db")
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_contracts_bootstrapped_from_roster(season_year_int)
        # Keep derived indices in sync (especially free_agents derived from roster).
        repo.rebuild_contract_indices()

    _CONTRACTS_BOOTSTRAPPED.add(season_key)


def validate_repo_integrity_once_startup(state: dict) -> None:
    """Validate DB integrity once at startup (per db_path)."""
    league = state.get("league") or {}
    db_path = str(league.get("db_path") or "league.db")
    if db_path in _REPO_INTEGRITY_VALIDATED:
        return
    from league_repo import LeagueRepo
    with LeagueRepo(db_path) as repo:
        repo.validate_integrity()
    _REPO_INTEGRITY_VALIDATED.add(db_path)
