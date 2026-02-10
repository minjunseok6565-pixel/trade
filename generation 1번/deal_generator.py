from __future__ import annotations

"""trade.trades.generation.deal_generator

(SPLIT) This file is a thin wrapper; implementation lives under trade.trades.generation.dealgen.

The original monolithic implementation was split without changing logic.
"""

from .dealgen.core import DealGenerator
from .dealgen.types import (
    DealGeneratorConfig,
    DealGeneratorBudget,
    DealProposal,
    DealGeneratorStats,
)

__all__ = [
    'DealGenerator',
    'DealGeneratorConfig',
    'DealGeneratorBudget',
    'DealProposal',
    'DealGeneratorStats',
]
