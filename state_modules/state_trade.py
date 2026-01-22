from __future__ import annotations

import state as state_facade


def _ensure_trade_state() -> None:
    """트레이드 관련 state 키를 보장한다.

    NOTE: draft_picks/swap_rights/fixed_assets/transactions 는 DB로 이동 (SSOT).
    """
    state_facade.ensure_trade_blocks()


def ensure_trade_state_keys() -> None:
    """Ensure trade-related state keys exist (startup-only)."""
    _ensure_trade_state()
