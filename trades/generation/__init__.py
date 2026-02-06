"""Trade generation utilities.

This package introduces a tick-scoped context object that caches expensive
snapshots and per-team decision inputs, enabling a deal generator to explore
many candidates efficiently.
"""

from .generation_tick import TradeGenerationTickContext, build_trade_generation_tick_context
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    IncomingPlayerRef,
    StepienHelper,
    MarketValueSummary,
    LockInfo,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    build_trade_asset_catalog,
)

__all__ = [
    "TradeGenerationTickContext",
    "build_trade_generation_tick_context",
    "TradeAssetCatalog",
    "TeamOutgoingCatalog",
    "IncomingPlayerRef",
    "StepienHelper",
    "MarketValueSummary",
    "LockInfo",
    "PlayerTradeCandidate",
    "PickTradeCandidate",
    "SwapTradeCandidate",
    "build_trade_asset_catalog",
]
