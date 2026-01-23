from __future__ import annotations

from typing import Any, Dict


def ensure_trade_state_keys(state: dict, *, defaults: Dict[str, Any]) -> None:
    """Ensure trade-related keys exist on the provided state dict."""
    state.setdefault("trade_market", dict(defaults.get("trade_market") or {}))
    state.setdefault("trade_memory", dict(defaults.get("trade_memory") or {}))
    state.setdefault("trade_agreements", {})
    state.setdefault("negotiations", {})
    state.setdefault("asset_locks", {})
