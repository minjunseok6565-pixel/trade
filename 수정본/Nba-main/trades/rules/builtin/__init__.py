from __future__ import annotations

from .asset_lock_rule import AssetLockRule
from .deadline_rule import DeadlineRule
from .duplicate_asset_rule import DuplicateAssetRule
from .ownership_rule import OwnershipRule
from .roster_limit_rule import RosterLimitRule
from .team_legs_rule import TeamLegsRule

BUILTIN_RULES = [
    AssetLockRule(),
    DeadlineRule(),
    DuplicateAssetRule(),
    OwnershipRule(),
    RosterLimitRule(),
    TeamLegsRule(),
]
