from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ...errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    SWAP_NOT_FOUND,
    SWAP_INVALID,
    TRADE_DEADLINE_PASSED,
    DUPLICATE_ASSET,
)
from ...models import Deal
from ...valuation.types import DealDecision, TeamDealEvaluation


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

    # --- young split (prospect vs throw-in)  [v2 parity]
    # Throw-in: cheap young bodies
    young_throwin_max_market: float = 22.0
    # Prospect pool: top fraction among young controllable (by market.total desc)
    young_prospect_top_frac: float = 0.35
    young_prospect_max_candidates: int = 6
    young_throwin_max_candidates: int = 6

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
    # Sweetener activation window (v2 absorption: scale by receiver's outgoing_total)
    # corridor = clamp(floor, ratio * max(outgoing_total, 6.0), cap)
    # effective corridor = min(corridor, sweetener_max_deficit)  # 기존 상한은 안전장치로 존중
    sweetener_close_corridor_ratio: float = 0.12
    sweetener_close_floor: float = 0.6
    sweetener_close_cap: float = 8.0
    sweetener_min_improvement: float = 0.25  # score 또는 margin 개선이 거의 없으면 중단
    sweetener_try_buckets: Tuple[str, ...] = (
        "SECOND",
        "SWAP",
        "FIRST_SAFE",
        "SECOND",  # allow 2nd second-rounder
        "FIRST_SENSITIVE",
    )
    # sweetener 후보를 token(bucket)별로 몇 개까지 trial 할지(베스트-오브-N).
    # 예산이 빡빡하면 sweetener.py에서 자동으로 더 줄인다.
    sweetener_candidate_width: int = 3
    
    # --- fit swap counter (DecisionReason: FIT_FAILS)
    # FIT_FAILS(=상대가 받는 incoming 플레이어들의 team-fit 불만)일 때,
    # outgoing 플레이어 1명을 "더 맞는 선수"로 교체해보는 카운터를 시도한다.
    fit_swap_enabled: bool = True
    # base(=sweetener 이전) 딜 하나당 fit-swap 시도 상한
    fit_swap_max_trials_per_base: int = 1
    # replacement 후보 풀/시도 제한
    fit_swap_candidate_pool: int = 18
    fit_swap_try_top_n: int = 6
    # 얼마나 fit이 좋아져야 교체 후보로 인정할지
    fit_swap_min_fit_improvement: float = 0.02
    # 샐러리 급격히 달라져 수리 비용이 폭증하는 것을 방지(단위: $M)
    fit_swap_max_salary_diff_m: float = 10.0
    # fit-swap 카운터에서 허용하는 최대 repair 횟수(최소 수리)
    fit_swap_max_repairs: int = 1

    # --- fit swap horizon-aware scoring (v2 absorption)
    # receiver(=FIT_FAILS 낸 팀)의 타임라인/포스처 성향에 따라
    # replacement 후보 랭킹 primary_score를 youth/fit/market_norm 가중합으로 계산한다.
    # primary_score = w_youth*youth + w_fit*fit + w_market*market_norm
    #
    # - REBUILD: youth/years 우선, market은 약하게 감점
    # - WIN_NOW: fit + market(즉시전력) 우선
    # - NEUTRAL: 균형
    #
    # NOTE: fit_swap.py에서만 사용하며, False면 기존(v1)처럼 fit 중심으로 랭킹한다.
    fit_swap_use_horizon_weights: bool = True

    # market normalization: market_norm = market_total / divisor
    fit_swap_market_norm_divisor: float = 50.0

    # youth score shaping:
    # youth = max(0, age_anchor - age) / age_span  +  min(years_cap, remaining_years) / years_span
    fit_swap_youth_age_anchor: float = 30.0
    fit_swap_youth_age_span: float = 10.0
    fit_swap_youth_years_cap: float = 4.0
    fit_swap_youth_years_span: float = 4.0

    # weights are (w_youth, w_fit, w_market)
    fit_swap_weights_rebuild: Tuple[float, float, float] = (0.55, 0.40, -0.05)
    fit_swap_weights_win_now: Tuple[float, float, float] = (0.05, 0.70, 0.25)
    fit_swap_weights_neutral: Tuple[float, float, float] = (0.20, 0.60, 0.20)

    # --- target selection / incoming pool
    need_tags_max: int = 4
    incoming_pool_per_tag: int = 60
    incoming_use_cheap_pool: bool = True

    # --- target diversity / spam prevention (v2 absorption)
    # 동일 타깃(같은 선수)이 결과 상단에 반복 노출되는 것을 억제하기 위한 soft penalty.
    # v2는 core 단계에서 target_seen 카운트 기반으로 score를 감점한다.
    target_repeat_penalty: float = 0.15

    # --- opponent diversity / spam prevention
    opponent_repeat_penalty: float = 0.25
    opponent_multi_repeat_penalty: float = 0.18

    # Final output hard cap: 같은 상대팀(파트너) 반복 상한. 0이면 비활성(기존 동작 유지).
    # - BUY 모드: seller_id 기준
    # - SELL 모드: buyer_id 기준
    max_partner_repeats: int = 0

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

    # sweetener telemetry (v2-style)
    sweetener_trials: int = 0
    sweetener_commits: int = 0
    sweetener_rollbacks: int = 0

    # fit swap counter telemetry
    fit_swap_triggers: int = 0
    fit_swap_candidates_tried: int = 0
    fit_swap_success: int = 0

    # failure kind -> count
    failures_by_kind: Dict[str, int] = field(default_factory=dict)

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
    to_team: Optional[str] = None
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
    if err.code in (PLAYER_NOT_OWNED, PICK_NOT_OWNED, SWAP_NOT_OWNED, SWAP_NOT_FOUND, SWAP_INVALID):
        player_id = str(details.get("player_id") or "") or None
        pick_id = str(details.get("pick_id") or "") or None
        swap_id = str(details.get("swap_id") or "") or None

        # (C) ownership 실패가 반복될 때 예산 낭비를 줄이기 위해 asset_key를 채운다.
        ak: Optional[str] = None
        if err.code == PLAYER_NOT_OWNED and player_id:
            ak = f"player:{player_id}"
        elif err.code == PICK_NOT_OWNED and pick_id:
            ak = f"pick:{pick_id}"
        elif err.code in (SWAP_NOT_OWNED, SWAP_NOT_FOUND, SWAP_INVALID) and swap_id:
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
        to_team = str(details.get("to_team") or "") or None
        to_team = str(to_team).upper() if to_team else None
        return RuleFailure(
            kind=RuleFailureKind.RETURN_TO_TRADING_TEAM,
            code=err.code,
            message=err.message,
            rule_id=rule_id,
            team_id=str(details.get("from_team") or "") or None,
            to_team=to_team,
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

