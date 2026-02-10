from __future__ import annotations

"""dealgen.types

Split-out types for the deal generator.
"""

from dataclasses import dataclass, field
from datetime import date
from math import exp
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import hashlib
import json
import random
import re

from ...errors import (
    DEAL_INVALIDATED,
    TRADE_DEADLINE_PASSED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    DUPLICATE_ASSET,
    TradeError,
)
from ...models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import (
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    TradeAssetCatalog,
    build_trade_asset_catalog,
)

# =============================================================================
# Config / output types
# =============================================================================


@dataclass(slots=True)
class DealGeneratorConfig:
    """Tuning knobs for exploration and realism."""

    # --- Global caps (hard)
    max_results_default: int = 12
    max_targets_hard_cap: int = 28
    max_attempts_per_target_hard_cap: int = 70
    max_validations_hard_cap: int = 650
    max_evaluations_hard_cap: int = 520

    # --- Baselines (scaled by posture/urgency/deadline_pressure)
    base_max_targets: int = 14
    base_beam_width: int = 9
    base_max_attempts_per_target: int = 40
    base_max_repairs: int = 2
    base_max_assets: int = 6
    base_max_players_moved: int = 4
    base_skeletons_per_target: int = 5

    # --- Sweeteners
    max_second_rounders_as_sweetener: int = 2
    allow_swaps_as_sweetener: bool = True
    allow_first_sensitive_as_last_resort: bool = False

    # Sweetener activation window (performance + realism)
    # We only try sweeteners when seller is "close" to accept.
    # DecisionPolicy's counter corridor defaults to ~0.06*scale; we use 2x that as a starting point.
    sweetener_close_corridor_ratio: float = 0.12
    sweetener_close_floor: float = 0.6
    sweetener_close_cap: float = 8.0

    # --- Heuristics / realism
    need_tags_max: int = 4
    need_tags_min_weight: float = 0.30
    target_repeat_penalty: float = 0.15
    opponent_repeat_penalty: float = 0.10

    # target selection throttles
    per_tag_take: int = 18
    cheap_per_tag_take: int = 10

    # evaluation scoring
    sigmoid_scale: float = 7.0
    complexity_asset_penalty: float = 0.15
    complexity_player_penalty: float = 0.10
    reject_floor_penalty: float = 0.75

    # RNG determinism
    rng_salt: int = 1337

    # If True, generator will attempt to build asset catalog when missing.
    build_catalog_if_missing: bool = True

    # Dedupe
    dedupe_ignore_meta: bool = True

    # Young-asset heuristic for 'young + pick' archetype
    young_max_age: float = 25.0
    young_min_remaining_years: float = 2.0
    young_allow_unknown_age: bool = True
    young_unknown_age_min_remaining_years: float = 2.5
    young_avoid_expiring: bool = True


@dataclass(frozen=True, slots=True)
class DealProposal:
    deal: Deal
    buyer_id: str
    seller_id: str
    buyer_decision: DealDecision
    seller_decision: DealDecision
    buyer_eval: TeamDealEvaluation
    seller_eval: TeamDealEvaluation
    score: float
    tags: Tuple[str, ...] = tuple()


# =============================================================================
# Internal helpers
# =============================================================================


@dataclass(slots=True)
class _BudgetTracker:
    """Hard-cap budget tracker.

    We count *attempted* validations/evaluations (success or failure) because
    the cost is incurred by the call itself.
    """

    max_validations: int
    max_evaluations: int
    validations_used: int = 0
    evaluations_used: int = 0

    def can_consume_validations(self, n: int = 1) -> bool:
        try:
            nn = int(n)
        except Exception:
            nn = 1
        return (self.validations_used + max(0, nn)) <= int(self.max_validations)

    def can_consume_evaluations(self, n: int = 1) -> bool:
        try:
            nn = int(n)
        except Exception:
            nn = 1
        return (self.evaluations_used + max(0, nn)) <= int(self.max_evaluations)

    def try_consume_validations(self, n: int = 1) -> bool:
        if not self.can_consume_validations(n):
            return False
        self.validations_used += max(0, int(n))
        return True

    def try_consume_evaluations(self, n: int = 1) -> bool:
        if not self.can_consume_evaluations(n):
            return False
        self.evaluations_used += max(0, int(n))
        return True
