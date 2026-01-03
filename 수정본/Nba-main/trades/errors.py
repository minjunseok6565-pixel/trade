from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class TradeError(Exception):
    code: str
    message: str
    details: Optional[Any] = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


TRADE_DEADLINE_PASSED = "TRADE_DEADLINE_PASSED"
INVALID_TEAM = "INVALID_TEAM"
PLAYER_NOT_OWNED = "PLAYER_NOT_OWNED"
PICK_NOT_OWNED = "PICK_NOT_OWNED"
ROSTER_LIMIT = "ROSTER_LIMIT"
HARD_CAP_EXCEEDED = "HARD_CAP_EXCEEDED"
ASSET_LOCKED = "ASSET_LOCKED"
DEAL_EXPIRED = "DEAL_EXPIRED"
DEAL_INVALIDATED = "DEAL_INVALIDATED"
DEAL_ALREADY_EXECUTED = "DEAL_ALREADY_EXECUTED"
APPLY_FAILED = "APPLY_FAILED"
NEGOTIATION_NOT_FOUND = "NEGOTIATION_NOT_FOUND"
MISSING_TO_TEAM = "MISSING_TO_TEAM"
DUPLICATE_ASSET = "DUPLICATE_ASSET"
