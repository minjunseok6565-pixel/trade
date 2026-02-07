from __future__ import annotations

import json

from config import ALL_TEAM_IDS, GM_PROFILES_SEED_PATH
from .state_cap import _apply_cap_model_for_season


def _require_db_path(league: dict) -> str:
    """Return league.db_path or raise (no implicit defaults)."""
    if not isinstance(league, dict):
        raise ValueError("league must be a dict")
    db_path = league.get("db_path")
    if not db_path:
        raise ValueError("league.db_path is required")
    return str(db_path)

def _load_gm_profiles_seed(seed_path: str) -> dict[str, dict]:
    """Load gm_profiles seed file and return {team_id: profile_json}.

    Seed format: a JSON list of objects where each object contains 'Team' and trait fields.
    - 'Team' is used as team_id (uppercased/stripped).
    - Stored profile_json excludes 'Team'.
    Fail-fast on any invalid format/content (development stage).
    """
    with open(seed_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("gm_profiles seed must be a JSON list")

    allowed = set(ALL_TEAM_IDS)
    out: dict[str, dict] = {}
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"gm_profiles seed item[{i}] must be an object")
        team = item.get("Team")
        if not team:
            raise ValueError(f"gm_profiles seed item[{i}] missing 'Team'")
        team_id = str(team).strip().upper()
        if team_id not in allowed:
            raise ValueError(f"gm_profiles seed item[{i}] has unknown Team '{team_id}'")
        profile = dict(item)
        profile.pop("Team", None)
        out[team_id] = profile

    missing = [t for t in ALL_TEAM_IDS if t not in out]
    if missing:
        raise ValueError(f"gm_profiles seed missing teams: {missing}")
    return out


def ensure_db_initialized_and_seeded(state: dict) -> None:
    """Ensure LeagueRepo is initialized and GM profiles are seeded (startup-only)."""
    league = state["league"]
    db_path = _require_db_path(league)

    migrations = state["_migrations"]
    if not isinstance(migrations, dict):
        raise ValueError("_migrations must be a dict")
    if migrations.get("db_initialized") is True and migrations.get("db_initialized_db_path") == db_path:
        return

    from league_repo import LeagueRepo

    with LeagueRepo(db_path) as repo:
        repo.init_db()
        # Auto-seed GM profiles on a fresh dev DB (or when only empty {} rows exist).
        existing = repo.get_all_gm_profiles() or {}
        has_any_non_empty = any(isinstance(v, dict) and len(v) > 0 for v in existing.values())
        if not has_any_non_empty:
            seed_mapping = _load_gm_profiles_seed(GM_PROFILES_SEED_PATH)
            repo.upsert_gm_profiles(seed_mapping)
        # Keep rows ready for all teams (idempotent).
        repo.ensure_gm_profiles_seeded(ALL_TEAM_IDS)

    migrations["db_initialized"] = True
    migrations["db_initialized_db_path"] = db_path


def ensure_cap_model_populated_if_needed(state: dict) -> None:
    """Apply season-specific cap/apron values when cap_auto_update is enabled.

    Notes:
    - This is safe to call on startup *and* at every season transition.
    - If league.trade_rules.cap_auto_update is False, this does nothing (manual cap mode).
    """
    league = state["league"]
    if not isinstance(league, dict):
        raise ValueError("league must be a dict")
        
    # Ensure trade_rules is a dict (robustness against malformed state).
    trade_rules = league.get("trade_rules")
    if not isinstance(trade_rules, dict):
        trade_rules = {}
        league["trade_rules"] = trade_rules

    # Respect manual cap mode.
    if trade_rules.get("cap_auto_update") is False:
        return

    season_year = league.get("season_year")
    if not season_year:
        return
    try:
        season_year_int = int(season_year)
    except (TypeError, ValueError):
        return
    # 핵심: salary_cap 값이 이미 존재하더라도, 현재 시즌 기준으로 항상 재계산/적용한다.
    _apply_cap_model_for_season(league, season_year_int)


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

    db_path = _require_db_path(league)
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
    db_path = _require_db_path(league)
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
