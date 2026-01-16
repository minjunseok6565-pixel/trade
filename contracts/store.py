"""Contract-related helpers.

IMPORTANT:
  Contract / free-agency ledgers are DB SSOT (LeagueRepo / LeagueService).
  Legacy state-ledger helpers were intentionally removed to prevent
  resurrecting discarded GAME_STATE keys.
"""

from __future__ import annotations


def __getattr__(name: str):
    """
    Fail loud on removed legacy helpers.

    We intentionally do NOT provide ensure_contract_state anymore. Any code that
    previously relied on it must migrate to DB-backed services/repos.
    """
    if name == "ensure_contract_state":
        raise AttributeError(
            "ensure_contract_state was removed (contract/FA ledger is DB SSOT). "
            "Migrate callers to LeagueService/LeagueRepo."
        )
    raise AttributeError(name)


def get_league_season_year(game_state: dict) -> int:
    return int(game_state.get("league", {}).get("season_year") or 0)


def get_current_date_iso(game_state: dict) -> str:
    from state import get_current_date_as_date

    return get_current_date_as_date().isoformat()
