from __future__ import annotations

from typing import Any, Dict

from state_store import _DEFAULT_TRADE_MARKET, _DEFAULT_TRADE_MEMORY


def ensure_trade_state_keys(state: Dict[str, Any]) -> None:
    """트레이드 관련 상태 키를 보장한다.

    NOTE: draft_picks/swap_rights/fixed_assets/transactions 는 DB로 이동 (SSOT).
    """
    if "trade_market" not in state:
        state["trade_market"] = dict(_DEFAULT_TRADE_MARKET)
    if "trade_memory" not in state:
        state["trade_memory"] = dict(_DEFAULT_TRADE_MEMORY)

    state.setdefault("trade_agreements", {})
    state.setdefault("negotiations", {})
    state.setdefault("asset_locks", {})
