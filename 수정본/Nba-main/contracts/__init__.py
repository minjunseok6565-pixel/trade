"""Contracts package."""

from contracts.free_agents import (
    FREE_AGENT_TEAM_ID,
    add_free_agent,
    is_free_agent,
    list_free_agents,
    remove_free_agent,
)
from contracts.models import (
    get_active_salary_for_season,
    make_contract_record,
    new_contract_id,
)
from contracts.ops import re_sign_or_extend, release_to_free_agents, sign_free_agent
from contracts.store import ensure_contract_state
from contracts.sync import (
    assert_state_vs_roster_consistency,
    sync_roster_salaries_for_season,
    sync_roster_teams_from_state,
)

__all__ = [
    "ensure_contract_state",
    "new_contract_id",
    "make_contract_record",
    "get_active_salary_for_season",
    "FREE_AGENT_TEAM_ID",
    "list_free_agents",
    "is_free_agent",
    "add_free_agent",
    "remove_free_agent",
    "sign_free_agent",
    "re_sign_or_extend",
    "release_to_free_agents",
    "sync_roster_salaries_for_season",
    "sync_roster_teams_from_state",
    "assert_state_vs_roster_consistency",
]
