from __future__ import annotations

from .state_constants import _DEFAULT_TRADE_MARKET, _DEFAULT_TRADE_MEMORY


def _ensure_trade_state(state: dict) -> None:
    """트레이드 관련 상태 키를 보장한다.

    NOTE: draft_picks/swap_rights/fixed_assets/transactions 는 DB로 이동 (SSOT).
    """
    state.setdefault("trade_market", dict(_DEFAULT_TRADE_MARKET))
    state.setdefault("trade_memory", dict(_DEFAULT_TRADE_MEMORY))

    state.setdefault("trade_agreements", {})
    state.setdefault("negotiations", {})
    state.setdefault("asset_locks", {})


def ensure_trade_state_keys(state: dict) -> None:
    """Ensure trade-related state keys exist (startup-only)."""
    _ensure_trade_state(state)
