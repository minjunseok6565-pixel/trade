"""Contracts package.

This package exposes DB-backed contract / free-agency operations.
Legacy GAME_STATE-ledger helpers are intentionally not exported.
"""
from contracts.free_agents import (
    FREE_AGENT_TEAM_ID,
    is_free_agent,
    list_free_agents,
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

__all__ = [
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
    "sign_free_agent",
    "re_sign_or_extend",
    "release_to_free_agents",
]
