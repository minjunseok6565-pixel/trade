from __future__ import annotations

from .asset_lock_rule import AssetLockRule
from .deadline_rule import DeadlineRule
from .duplicate_asset_rule import DuplicateAssetRule
from .ownership_rule import OwnershipRule
from .pick_protection_schema_rule import PickProtectionSchemaRule
from .pick_rules_rule import PickRulesRule
from .player_eligibility_rule import PlayerEligibilityRule
from .return_to_trading_team_rule import ReturnToTradingTeamRule
from .roster_limit_rule import RosterLimitRule
from .salary_matching_rule import SalaryMatchingRule
from .swap_uniqueness_rule import SwapUniquenessRule
from .team_legs_rule import TeamLegsRule

BUILTIN_RULES = [
    AssetLockRule(),
    DeadlineRule(),
    DuplicateAssetRule(),
    PickProtectionSchemaRule(),
    SwapUniquenessRule(),
    OwnershipRule(),
    RosterLimitRule(),
    PlayerEligibilityRule(),
    ReturnToTradingTeamRule(),
    PickRulesRule(),
    SalaryMatchingRule(),
    TeamLegsRule(),
]
