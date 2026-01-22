from __future__ import annotations

from .state_store import GAME_STATE, _DEFAULT_TRADE_MARKET, _DEFAULT_TRADE_MEMORY


def _ensure_trade_state() -> None:
    """트레이드 관련 GAME_STATE 키를 보장한다.

    NOTE: draft_picks/swap_rights/fixed_assets/transactions 는 DB로 이동 (SSOT).
    """
    GAME_STATE.setdefault("trade_market", dict(_DEFAULT_TRADE_MARKET))
    GAME_STATE.setdefault("trade_memory", dict(_DEFAULT_TRADE_MEMORY))

    GAME_STATE.setdefault("trade_agreements", {})
    GAME_STATE.setdefault("negotiations", {})
    GAME_STATE.setdefault("asset_locks", {})


def ensure_trade_state_keys() -> None:
    """Ensure trade-related GAME_STATE keys exist (startup-only)."""
    _ensure_trade_state()
