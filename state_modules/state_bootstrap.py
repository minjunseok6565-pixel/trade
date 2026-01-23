from __future__ import annotations

from config import ALL_TEAM_IDS
from .state_cap import _apply_cap_model_for_season
from .state_migrations import normalize_player_ids


def ensure_db_initialized_and_seeded(state: dict) -> None:
    """Ensure LeagueRepo is initialized and GM profiles are seeded (startup-only)."""
    league = state["league"]
    if not isinstance(league, dict):
        raise ValueError("league must be a dict")
    db_path = str(league.get("db_path") or "league.db")

    migrations = state["_migrations"]
    if not isinstance(migrations, dict):
        raise ValueError("_migrations must be a dict")
    if migrations.get("db_initialized") is True and migrations.get("db_initialized_db_path") == db_path:
        return

    from league_repo import LeagueRepo

    with LeagueRepo(db_path) as repo:
        repo.init_db()
        # Keep rows ready for all teams (idempotent).
        repo.ensure_gm_profiles_seeded(ALL_TEAM_IDS)

    migrations["db_initialized"] = True
    migrations["db_initialized_db_path"] = db_path


def ensure_cap_model_populated_if_needed(state: dict) -> None:
    """Populate cap/aprons in league.trade_rules if season_year is known and unset/zero."""
    league = state["league"]
    if not isinstance(league, dict):
        raise ValueError("league must be a dict")
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
    """Normalize player IDs in state (startup-only)."""
    return normalize_player_ids(state, allow_legacy_numeric=allow_legacy_numeric)


def ensure_contracts_bootstrapped_after_schedule_creation_once(state: dict) -> None:
    """Bootstrap contracts from roster once right after schedule creation (per season)."""
    league = state["league"]
    if not isinstance(league, dict):
        raise ValueError("league must be a dict")
    season_year = league.get("season_year")
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return

    migrations = state["_migrations"]
    if not isinstance(migrations, dict):
        raise ValueError("_migrations must be a dict")
    boot = migrations["contracts_bootstrapped_seasons"]
    if not isinstance(boot, dict):
        boot = {}
        migrations["contracts_bootstrapped_seasons"] = boot
    if isinstance(boot, dict) and boot.get(str(season_year_int)) is True:
        return

    from league_repo import LeagueRepo

    db_path = str(league.get("db_path") or "league.db")
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.ensure_contracts_bootstrapped_from_roster(season_year_int)
        # Keep derived indices in sync (especially free_agents derived from roster).
        repo.rebuild_contract_indices()

    if isinstance(boot, dict):
        boot[str(season_year_int)] = True


def validate_repo_integrity_once_startup(state: dict) -> None:
    """Validate DB integrity once at startup (per db_path)."""
    league = state["league"]
    if not isinstance(league, dict):
        raise ValueError("league must be a dict")
    db_path = str(league.get("db_path") or "league.db")
    migrations = state["_migrations"]
    if not isinstance(migrations, dict):
        raise ValueError("_migrations must be a dict")
    if migrations.get("repo_integrity_validated") is True and migrations.get("repo_integrity_validated_db_path") == db_path:
        return
    from league_repo import LeagueRepo
    with LeagueRepo(db_path) as repo:
        try:
            repo.validate_integrity()
        except ValueError as exc:
            if "no active roster entries found" not in str(exc):
                raise
    migrations["repo_integrity_validated"] = True
    migrations["repo_integrity_validated_db_path"] = db_path
