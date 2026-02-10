from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import hashlib
import json
import math
import random
from datetime import date

try:
    from schema import normalize_team_id  # type: ignore
except Exception:  # pragma: no cover
    def normalize_team_id(x: str, strict: bool = False) -> str:  # type: ignore
        return str(x or "").upper()

from ..errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    SWAP_NOT_FOUND,
    SWAP_INVALID,
    DUPLICATE_ASSET,
)

from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, TeamDealEvaluation, DealVerdict

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    IncomingPlayerRef,
    StepienHelper,
)


# =============================================================================
# Public types
# =============================================================================
@dataclass(frozen=True, slots=True)
class DealGeneratorConfig:
    """
    Tuning knobs for search budget + realism constraints.

    Tip: keep hard upper bounds conservative. In a game tick, deal generation should
    never be an unbounded search.
    """

    # --- early exit ---
    stand_pat_min_urgency: float = 0.35

    # --- hard budgets (upper bounds) ---
    max_targets: int = 26
    max_attempts_per_target: int = 60
    max_validations: int = 450
    max_evaluations: int = 260
    max_repairs: int = 2

    # --- complexity constraints ---
    max_assets: int = 6
    max_players_moved: int = 4  # total players moved across both teams

    # --- beam ---
    beam_width: int = 12

    # --- archetype controls ---
    enable_consolidate_2for1: bool = True
    enable_picks_only: bool = True
    enable_sweeteners: bool = True

    # --- sweetener loop ---
    max_sweeteners: int = 2
    near_miss_margin_max: float = 2.0  # try sweetener if seller margin in [-near_miss_margin_max, 0)
    sweetener_order: Tuple[str, ...] = ("SECOND", "SWAP", "FIRST_SAFE", "SECOND", "FIRST_SENSITIVE")
    # Try up to N candidates per token and commit the best (budget-capped inside loop).
    sweetener_candidate_width: int = 3

    # --- target selection ---
    max_need_tags: int = 4
    cheap_incoming_bonus: float = 0.25  # slight bump for cheap targets
    target_salary_penalty_scale: float = 0.12  # penalty per $10M (very light)

    # --- scoring ---
    sigmoid_scale: float = 3.5
    complexity_penalty_assets: float = 0.15
    complexity_penalty_players: float = 0.10
    buyer_overpay_penalty: float = 1.0

    # --- market spam control ---
    partner_repeat_penalty: float = 0.35  # score penalty per extra repetition
    max_partner_repeats: int = 3

    # --- deterministic randomness ---
    deterministic_seed_salt: str = "dealgen_v1"

    # --- young asset definition (rebuild realism) ---
    young_max_age: float = 25.0
    young_min_control_years: float = 2.0
    young_throwin_max_market: float = 22.0
    # Prospect pool is the top fraction (by market.total) among young, controllable players.
    young_prospect_top_frac: float = 0.35
    young_prospect_max_candidates: int = 6
    young_throwin_max_candidates: int = 6

    # --- telemetry ---
    telemetry_enabled: bool = True
    on_stats: Optional[Callable[["DealGenerationStats"], None]] = None

    # --- posture-based recommended baselines (used by _compute_budgets) ---
    posture_targets: Mapping[str, int] = field(
        default_factory=lambda: {
            "AGGRESSIVE_BUY": 22,
            "SOFT_BUY": 14,
            "STAND_PAT": 4,
            "SOFT_SELL": 16,
            "SELL": 18,
        }
    )
    posture_beam: Mapping[str, int] = field(
        default_factory=lambda: {
            "AGGRESSIVE_BUY": 12,
            "SOFT_BUY": 8,
            "STAND_PAT": 5,
            "SOFT_SELL": 9,
            "SELL": 10,
        }
    )


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


@dataclass(slots=True)
class DealGenerationStats:
    """Lightweight counters for tuning + debugging invalid exploration."""

    team_id: str
    validations: int = 0
    evaluations: int = 0
    attempts: int = 0

    # prunes
    pruned_duplicate: int = 0
    pruned_locked: int = 0
    pruned_ineligible: int = 0
    pruned_stepien: int = 0

    # failures
    fail_by_code: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    fail_by_rule: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    fail_by_method: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))

    # partners
    partner_counts: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))

    # sweetener loop
    sweetener_trials: int = 0
    sweetener_commits: int = 0
    sweetener_rollbacks: int = 0
    sweetener_commit_by_token: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))

    # pick rule prechecks
    stepien_precheck_blocked: int = 0
    stepien_soft_drops: int = 0

    def record_error(self, err: TradeError) -> None:
        self.fail_by_code[str(err.code)] += 1
        details = getattr(err, "details", None)
        if isinstance(details, dict):
            rule = details.get("rule")
            method = details.get("method")
            if rule:
                self.fail_by_rule[str(rule)] += 1
            if method:
                self.fail_by_method[str(method)] += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "team_id": self.team_id,
            "validations": self.validations,
            "evaluations": self.evaluations,
            "attempts": self.attempts,
            "pruned_duplicate": self.pruned_duplicate,
            "pruned_locked": self.pruned_locked,
            "pruned_ineligible": self.pruned_ineligible,
            "pruned_stepien": self.pruned_stepien,
            "fail_by_code": dict(self.fail_by_code),
            "fail_by_rule": dict(self.fail_by_rule),
            "fail_by_method": dict(self.fail_by_method),
            "partner_counts": dict(self.partner_counts),
            "sweetener_trials": self.sweetener_trials,
            "sweetener_commits": self.sweetener_commits,
            "sweetener_rollbacks": self.sweetener_rollbacks,
            "sweetener_commit_by_token": dict(self.sweetener_commit_by_token),
            "stepien_precheck_blocked": self.stepien_precheck_blocked,
            "stepien_soft_drops": self.stepien_soft_drops,
        }

# =============================================================================
@dataclass(slots=True)
class _Budgets:
    max_targets: int
    beam_width: int
    max_attempts_per_target: int
    max_validations: int
    max_evaluations: int
    max_repairs: int


@dataclass(slots=True)
class _DealSpec:
    """Intermediate representation before converting to Deal."""
    buyer_id: str
    seller_id: str

    buyer_players_out: List[str] = field(default_factory=list)
    buyer_picks_out: List[str] = field(default_factory=list)
    buyer_swaps_out: List[str] = field(default_factory=list)

    seller_players_out: List[str] = field(default_factory=list)
    seller_picks_out: List[str] = field(default_factory=list)
    seller_swaps_out: List[str] = field(default_factory=list)

    tags: List[str] = field(default_factory=list)

    def copy(self) -> "_DealSpec":
        return _DealSpec(
            buyer_id=self.buyer_id,
            seller_id=self.seller_id,
            buyer_players_out=list(self.buyer_players_out),
            buyer_picks_out=list(self.buyer_picks_out),
            buyer_swaps_out=list(self.buyer_swaps_out),
            seller_players_out=list(self.seller_players_out),
            seller_picks_out=list(self.seller_picks_out),
            seller_swaps_out=list(self.seller_swaps_out),
            tags=list(self.tags),
        )


@dataclass(slots=True)
class _GenState:
    cfg: DealGeneratorConfig
    tick_ctx: TradeGenerationTickContext
    catalog: TradeAssetCatalog
    allow_locked_by_deal_id: Optional[str]
    rng: random.Random
    stats: DealGenerationStats

    # banlists from fatal validation failures: skip in future
    banned_players: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    banned_picks: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    banned_swaps: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))

    # per-player receiver bans learned from validation (e.g., return-to-trading-team same season)
    # keyed by player_id -> set(receiver_team_id)
    banned_receivers_by_player: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))

    # dedupe across generated deals
    seen_fingerprints: Set[str] = field(default_factory=set)

