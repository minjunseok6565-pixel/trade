"""Contracts package."""

from contracts.bootstrap import (
    bootstrap_contracts_from_repo,
    bootstrap_contracts_from_roster_excel,
)
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
from contracts.options import (
    apply_option_decision,
    get_pending_options_for_season,
    normalize_option_record,
    recompute_contract_years_from_salary,
)
from contracts.options_policy import default_option_decision_policy
from contracts.ops import re_sign_or_extend, release_to_free_agents, sign_free_agent
from contracts.store import ensure_contract_state

__all__ = [
    "ensure_contract_state",
    "bootstrap_contracts_from_repo",
    "bootstrap_contracts_from_roster_excel",
    "new_contract_id",
    "make_contract_record",
    "get_active_salary_for_season",
    "default_option_decision_policy",
    "normalize_option_record",
    "get_pending_options_for_season",
    "apply_option_decision",
    "recompute_contract_years_from_salary",
    "FREE_AGENT_TEAM_ID",
    "list_free_agents",
    "is_free_agent",
    "add_free_agent",
    "remove_free_agent",
    "sign_free_agent",
    "re_sign_or_extend",
    "release_to_free_agents",
]
