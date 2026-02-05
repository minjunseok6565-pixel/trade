"""Trade generation utilities.

This package introduces a tick-scoped context object that caches expensive
snapshots and per-team decision inputs, enabling a deal generator to explore
many candidates efficiently.
"""

from .generation_tick import TradeGenerationTickContext, build_trade_generation_tick_context

__all__ = [
    "TradeGenerationTickContext",
    "build_trade_generation_tick_context",
]
