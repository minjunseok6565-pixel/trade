from __future__ import annotations

from datetime import date
from typing import Optional

import state
from .agreements import gc_expired_agreements


def maintain_trade_state(
    *,
    current_date: Optional[date] = None,
    db_path: Optional[str] = None,
) -> None:
    """
    Tick-level trade maintenance.

    This function is intentionally lightweight and safe to call at tick boundaries.
    It centralizes "cleanup" responsibilities that should NOT occur inside validation rules.

    Currently:
      - Expires committed deals / agreements and releases any associated asset locks.

    Args:
        current_date: The in-game date to use for expiration checks.
        db_path: Reserved for future DB-backed maintenance steps (kept for API symmetry).
                Currently unused; state-backed agreements/locks are in SSOT.
    """
    today = current_date or state.get_current_date_as_date()
    gc_expired_agreements(current_date=today)
