from __future__ import annotations

from .state_constants import _DEFAULT_TRADE_MARKET, _DEFAULT_TRADE_MEMORY


def _ensure_trade_state(state: dict) -> None:
    """트레이드 관련 상태 키를 보장한다.

    NOTE: draft_picks/swap_rights/fixed_assets/transactions 는 DB로 이동 (SSOT).
    """
    for key in ("trade_market", "trade_memory", "trade_agreements", "negotiations", "asset_locks"):
        if key not in state:
            raise ValueError(f"Missing trade state key: {key}")

    if not isinstance(state.get("trade_market"), dict):
        state["trade_market"] = dict(_DEFAULT_TRADE_MARKET)
    if not isinstance(state.get("trade_memory"), dict):
        state["trade_memory"] = dict(_DEFAULT_TRADE_MEMORY)
    if not isinstance(state.get("trade_agreements"), dict):
        state["trade_agreements"] = {}
    if not isinstance(state.get("negotiations"), dict):
        state["negotiations"] = {}
    if not isinstance(state.get("asset_locks"), dict):
        state["asset_locks"] = {}


def ensure_trade_state_keys(state: dict) -> None:
    """Ensure trade-related state keys exist (startup-only)."""
    _ensure_trade_state(state)
