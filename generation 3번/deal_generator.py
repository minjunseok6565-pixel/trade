from __future__ import annotations


"""trade.trades.generation.deal_generator

Commercial-grade 2-team deal generator.

Design goals
------------
- NBA-like: need-based targets, plausible packages (players/picks/filers), minimal spam.
- Fast: tick-level caching (TradeGenerationTickContext), bounded search (beam/prune), deterministic RNG.
- Stable: validate_deal() failure details drive minimal repair (1-2 steps); invalid ratio stays low.
- Consistent decisions: final ranking always based on evaluate_deal_for_team(...) results for *both* teams.

3+ team trades
--------------
This module intentionally focuses on 2-team deals. Extension points are left
as internal helpers (_resolve_receiver_team, _add_leg_asset etc.)
so a future multi-team generator can reuse core components.
"""

from .dealgen.types import DealGeneratorConfig, DealProposal
from .dealgen.core import _CoreMixin
from .dealgen.targeting import _TargetingMixin
from .dealgen.skeletons import _SkeletonsMixin
from .dealgen.sweeteners import _SweetenersMixin
from .dealgen.counter import _CounterMixin
from .dealgen.repair import _RepairMixin
from .dealgen.scoring import _ScoringMixin


class DealGenerator(
    _CoreMixin,
    _TargetingMixin,
    _SkeletonsMixin,
    _SweetenersMixin,
    _CounterMixin,
    _RepairMixin,
    _ScoringMixin,
):
    pass


__all__ = [
    "DealGeneratorConfig",
    "DealProposal",
    "DealGenerator",
]
