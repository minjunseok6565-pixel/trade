from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import hashlib
import json
import math
import random
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ...errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    TRADE_DEADLINE_PASSED,
    DUPLICATE_ASSET,
)
from ...models import (
    Deal,
    PlayerAsset,
    PickAsset,
    SwapAsset,
    Asset,
    asset_key,
    canonicalize_deal,
    serialize_deal,
    resolve_asset_receiver,
)
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, DealVerdict, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import (
    TradeAssetCatalog,
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    PickBucketId,
    BucketId,
    build_trade_asset_catalog,
)


# Public DTOs
# =============================================================================


@dataclass(frozen=True, slots=True)
class DealGeneratorConfig:
    """DealGenerator 탐색/수리/복잡도 예산.

    - 이 값들은 "base"이며, generate_for_team() 호출 시 팀 posture/urgency/deadline_pressure에 따라
      동적으로 스케일링된 DealGeneratorBudget이 실제로 사용된다.
    - "상업용" 목표: 어떤 팀/어떤 시즌에서도 틱당 계산량이 폭주하지 않도록 상한을 둔다.
    """

    # --- hard upper bounds (absolute safety)
    max_targets_hard: int = 28
    max_attempts_per_target_hard: int = 80
    max_validations_hard: int = 900
    max_evaluations_hard: int = 450

    # --- base budgets (scaled)
    base_max_targets: int = 14
    base_beam_width: int = 8
    base_max_attempts_per_target: int = 45
    base_max_validations: int = 360
    base_max_evaluations: int = 180
    base_max_repairs: int = 2

    # --- "young + pick" heuristic
    # asset_catalog에 YOUNG 버킷이 없으므로 generator-side에서 정의한다.
    # 기존(v1): age-only(<= 24.5)
    # 변경: age + team control(remaining_years) 기반 (fallback으로 age-only 완화)
    young_age_max: float = 24.5
    young_min_control_years: float = 2.0

    # --- deal shape constraints (generator-side)
    max_assets_per_side: int = 6
    max_players_moved_total: int = 4
    max_players_per_side: int = 2
    max_picks_per_side: int = 3
    max_seconds_per_side: int = 2

    # --- sweetener loop
    sweetener_enabled: bool = True
    sweetener_max_additions: int = 2
    sweetener_max_deficit: float = 10.0  # "조금 부족"(margin deficit)만 수리
    sweetener_min_improvement: float = 0.25  # score 또는 margin 개선이 거의 없으면 중단
    sweetener_try_buckets: Tuple[str, ...] = (
        "SECOND",
        "SWAP",
        "FIRST_SAFE",
        "SECOND",  # allow 2nd second-rounder
        "FIRST_SENSITIVE",
    )

    # --- target selection / incoming pool
    need_tags_max: int = 4
    incoming_pool_per_tag: int = 60
    incoming_use_cheap_pool: bool = True

    # --- opponent diversity / spam prevention
    opponent_repeat_penalty: float = 0.25
    opponent_multi_repeat_penalty: float = 0.18

    # --- scoring
    score_sigmoid_scale: float = 8.0
    penalty_per_asset: float = 0.15
    penalty_per_player: float = 0.10

    # deficit(손해) 패널티: 양쪽 모두에 적용 (buyer는 더 강하게)
    penalty_overpay_weight: float = 1.00

    penalty_opponent_overpay_weight: float = 0.85

    # REJECT를 강하게 벌점(유저 체감상 "말도 안 되는 오퍼" 상위 노출 방지)
    reject_penalty_base: float = 0.35
    reject_penalty_scale: float = 0.06

    # discard gate: 평가 결과가 너무 나쁘면 후보에서 제거
    discard_if_overpay_below: float = -18.0  # buyer margin이 이보다 더 나쁘면 후보 폐기
    discard_if_any_margin_below: float = -22.0  # 어느 한쪽이 이보다 나쁘면 폐기
    discard_if_reject_margin_below: float = -14.0  # REJECT인 팀 margin이 이보다 나쁘면 폐기
    discard_if_both_margins_below: float = -10.0

    # --- RNG determinism
    deterministic_seed_salt: str = "deal_generator_v2"

    # --- catalog behavior
    # allow_locked_by_deal_id가 주어진 경우, catalog를 1회 재빌드하여 locked asset을 풀어줄지
    rebuild_catalog_when_allow_locked: bool = True

    # --- soft guard (invalid 폭발 방지)
    # 딜 적용 후 추정 payroll_after가 second_apron 이상이면 one-for-one 형태만 남긴다(soft).
    # (SSOT: SalaryMatchingRule은 payroll_after 기반으로 apron status를 판정한다)
    soft_guard_second_apron_by_constraints: bool = True

    # --- sweetener candidate search (best-of-N trials per token)
    sweetener_candidate_width: int = 3


@dataclass(frozen=True, slots=True)
class DealGeneratorBudget:
    """팀 posture/urgency 기반으로 스케일된 실제 예산."""

    max_targets: int
    beam_width: int
    max_attempts_per_target: int
    max_validations: int
    max_evaluations: int
    max_repairs: int


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
class DealGeneratorStats:
    """운영/튜닝용 통계(외부 로그/텔레메트리로 보내기 좋음)."""

    mode: str = "BUY"
    targets_considered: int = 0
    skeletons_built: int = 0
    candidates_attempted: int = 0

    validations: int = 0
    evaluations: int = 0
    repairs: int = 0

    sweetener_attempts: int = 0
    sweeteners_added: int = 0

    # failure kind -> count
    failures_by_kind: Dict[str, int] = field(default_factory=dict)

    # sweetener telemetry (v2-style)
    sweetener_trials: int = 0
    sweetener_commits: int = 0
    sweetener_rollbacks: int = 0

    def bump_failure(self, kind: str) -> None:
        self.failures_by_kind[kind] = int(self.failures_by_kind.get(kind, 0)) + 1


# =============================================================================
# Internal DTOs
# =============================================================================


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    """BUY 모드에서 buyer가 원하는 incoming target 후보."""

    player_id: str
    from_team: str
    need_tag: str
    tag_strength: float
    market_total: float
    salary_m: float
    remaining_years: float
    age: Optional[float]


@dataclass(frozen=True, slots=True)
class SellAssetCandidate:
    """SELL 모드에서 initiator(=seller)가 시장에 내놓을 outgoing 후보."""

    player_id: str
    market_total: float
    salary_m: float
    remaining_years: float
    is_expiring: bool
    top_tags: Tuple[str, ...]


@dataclass(slots=True)
class DealCandidate:
    """탐색 중인 후보 딜(스켈레톤/수리 과정에서 mutate 가능)."""

    deal: Deal
    buyer_id: str
    seller_id: str

    # for debug/tagging
    focal_player_id: str
    archetype: str

    tags: List[str] = field(default_factory=list)
    repairs_used: int = 0


# =============================================================================
# TradeError parsing (SSOT: TradeError.code + TradeError.details)
# =============================================================================


class RuleFailureKind(str, Enum):
    DEADLINE = "deadline"
    SALARY_MATCHING = "salary_matching"
    SECOND_APRON_ONE_FOR_ONE = "second_apron_one_for_one"
    ROSTER_LIMIT = "roster_limit"
    ASSET_LOCK = "asset_lock"
    PLAYER_ELIGIBILITY = "player_eligibility"
    RETURN_TO_TRADING_TEAM = "return_to_trading_team_same_season"
    PICK_RULES = "pick_rules"
    OWNERSHIP = "ownership"
    DUPLICATE_ASSET = "duplicate_asset"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class RuleFailure:
    kind: RuleFailureKind
    code: str
    message: str
    rule_id: Optional[str] = None
    team_id: Optional[str] = None
    reason: Optional[str] = None
    method: Optional[str] = None
    status: Optional[str] = None
    player_id: Optional[str] = None
    pick_id: Optional[str] = None
    swap_id: Optional[str] = None
    asset_key: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


def parse_trade_error(err: TradeError) -> RuleFailure:
    """TradeError -> RuleFailure.

    - 대부분의 rules는 TradeError.details에 {"rule": rule_id, ...}를 넣는다.
    - 일부는 code로만 구분한다(예: ROSTER_LIMIT, ASSET_LOCKED, DUPLICATE_ASSET, OWNERSHIP 계열).
    """

    details: Dict[str, Any] = {}
    if isinstance(getattr(err, "details", None), dict):
        details = dict(err.details)  # shallow copy

    # --- code-first
    if err.code == TRADE_DEADLINE_PASSED:
        return RuleFailure(
            kind=RuleFailureKind.DEADLINE,
            code=err.code,
            message=err.message,
            rule_id="deadline",
            details=details,
        )
    if err.code == ROSTER_LIMIT:
        return RuleFailure(
            kind=RuleFailureKind.ROSTER_LIMIT,
            code=err.code,
            message=err.message,
            rule_id="roster_limit",
            team_id=str(details.get("team_id") or "") or None,
            details=details,
        )
    if err.code == ASSET_LOCKED:
        return RuleFailure(
            kind=RuleFailureKind.ASSET_LOCK,
            code=err.code,
            message=err.message,
            rule_id="asset_lock",
            asset_key=str(details.get("asset_key") or "") or None,
            details=details,
        )
    if err.code in (PLAYER_NOT_OWNED, PICK_NOT_OWNED, SWAP_NOT_OWNED):
        player_id = str(details.get("player_id") or "") or None
        pick_id = str(details.get("pick_id") or "") or None
        swap_id = str(details.get("swap_id") or "") or None

        # (C) ownership 실패가 반복될 때 예산 낭비를 줄이기 위해 asset_key를 채운다.
        ak: Optional[str] = None
        if err.code == PLAYER_NOT_OWNED and player_id:
            ak = f"player:{player_id}"
        elif err.code == PICK_NOT_OWNED and pick_id:
            ak = f"pick:{pick_id}"
        elif err.code == SWAP_NOT_OWNED and swap_id:
            ak = f"swap:{swap_id}"

        return RuleFailure(
            kind=RuleFailureKind.OWNERSHIP,
            code=err.code,
            message=err.message,
            rule_id="ownership",
            team_id=str(details.get("team_id") or "") or None,
            player_id=player_id,
            pick_id=pick_id,
            swap_id=swap_id,
            asset_key=ak,
            details=details,
        )
    if err.code == DUPLICATE_ASSET:
        return RuleFailure(
            kind=RuleFailureKind.DUPLICATE_ASSET,
            code=err.code,
            message=err.message,
            rule_id="duplicate_asset",
            asset_key=str(details.get("asset_key") or "") or None,
            details=details,
        )

    # --- details["rule"]
    rule_id = details.get("rule") if isinstance(details.get("rule"), str) else None
    if err.code == DEAL_INVALIDATED and rule_id == "salary_matching":
        method = str(details.get("method") or "")
        kind = RuleFailureKind.SECOND_APRON_ONE_FOR_ONE if method == "second_apron_one_for_one" else RuleFailureKind.SALARY_MATCHING
        return RuleFailure(
            kind=kind,
            code=err.code,
            message=err.message,
            rule_id=rule_id,
            team_id=str(details.get("team_id") or "") or None,
            method=method or None,
            status=str(details.get("status") or "") or None,
            details=details,
        )

    if err.code == DEAL_INVALIDATED and rule_id == "player_eligibility":
        return RuleFailure(
            kind=RuleFailureKind.PLAYER_ELIGIBILITY,
            code=err.code,
            message=err.message,
            rule_id=rule_id,
            team_id=str(details.get("team_id") or "") or None,
            reason=str(details.get("reason") or "") or None,
            player_id=str(details.get("player_id") or "") or None,
            details=details,
        )

    if err.code == DEAL_INVALIDATED and rule_id == "return_to_trading_team_same_season":
        return RuleFailure(
            kind=RuleFailureKind.RETURN_TO_TRADING_TEAM,
            code=err.code,
            message=err.message,
            rule_id=rule_id,
            team_id=str(details.get("from_team") or "") or None,
            reason=str(details.get("reason") or "") or None,
            player_id=str(details.get("player_id") or "") or None,
            details=details,
        )

    if err.code == DEAL_INVALIDATED and rule_id == "pick_rules":
        return RuleFailure(
            kind=RuleFailureKind.PICK_RULES,
            code=err.code,
            message=err.message,
            rule_id=rule_id,
            team_id=str(details.get("team_id") or "") or None,
            reason=str(details.get("reason") or "") or None,
            pick_id=str(details.get("pick_id") or "") or None,
            details=details,
        )

    return RuleFailure(
        kind=RuleFailureKind.OTHER,
        code=str(getattr(err, "code", "")) or "UNKNOWN",
        message=str(getattr(err, "message", "")) or str(err),
        rule_id=rule_id,
        details=details,
    )

