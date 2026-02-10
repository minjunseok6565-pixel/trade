from __future__ import annotations

"""trade.trades.generation.deal_generator

상업용(게임 플레이 루프)에서 "그대로" 돌려도 안정적인 2-team 딜 생성기.

전제(SSOT)
---------
- Hard legality: TradeGenerationTickContext.validate_deal()
- Valuation/decision: trades.valuation.service.evaluate_deal_for_team()
- Candidate pools: TradeAssetCatalog (outgoing buckets / incoming_by_need_tag / picks+swaps + Stepien)

설계 원칙
---------
- 탐색 폭을 강하게 제한: target 수, per-target attempts, validations/evaluations 상한
- invalid 최소화: catalog 단계에서 lock/ban을 최대한 걸러두고, 남은 invalid는
  TradeError.code + TradeError.details 기반으로 "최소 repair"(최대 1~2회)만 수행
- 현실감: need-tag 기반 타깃/상대 선정 + 2~4개 archetype 스켈레톤 + salary filler / pick sweetener
- 결정 일관성: 최종 점수는 항상 양팀 evaluate_deal_for_team 결과로만 계산

확장 포인트
-----------
- 3팀 이상: Deal.teams/legs 구조는 이미 지원하지만, generator는 2팀만 생성.
  (resolve_asset_receiver는 multi-team에서 to_team 필요)

주의
----
- TradeError.meta는 존재하지 않음. meta-like payload는 TradeError.details(dict)이다.
- 2nd apron one-for-one 제약은 TeamConstraints.apron_status만으로 확정할 수 없고,
  SalaryMatchingRule failure.details.method == "second_apron_one_for_one" 를 SSOT로 삼는다.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import hashlib
import json
import math
import random
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ..errors import (
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
from ..models import (
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
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, DealVerdict, TeamDealEvaluation

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
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


# =============================================================================
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


# =============================================================================
# DealGenerator
# =============================================================================


class DealGenerator:
    """Tick-scoped caches를 사용하는 2-team deal generator."""

    def __init__(self, config: Optional[DealGeneratorConfig] = None):
        self.config = config or DealGeneratorConfig()
        self.last_stats: Optional[DealGeneratorStats] = None

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def generate_for_team(
        self,
        team_id: str,
        tick_ctx: TradeGenerationTickContext,
        *,
        max_results: int = 8,
        allow_locked_by_deal_id: Optional[str] = None,
    ) -> List[DealProposal]:
        """team_id를 기준으로 2-team 딜 후보를 생성.

        posture에 따라 모드가 달라진다.
        - BUY/SOFT_BUY/STAND_PAT: team_id를 buyer로 간주
        - SELL/SOFT_SELL: team_id를 seller로 간주(매물 제안 모드)

        반환:
        - score 내림차순
        - 모든 딜은 validate 통과
        - buyer/seller 모두 evaluate 포함
        """

        # --- asset catalog 확보(allow_locked_by_deal_id가 있으면 선택적으로 재빌드)
        catalog = tick_ctx.asset_catalog
        if allow_locked_by_deal_id and self.config.rebuild_catalog_when_allow_locked:
            try:
                catalog = build_trade_asset_catalog(tick_ctx=tick_ctx, allow_locked_by_deal_id=allow_locked_by_deal_id)
            except Exception:
                catalog = tick_ctx.asset_catalog
        if catalog is None:
            return []

        tid = str(team_id).upper()

        # trade deadline hard stop (SSOT: DeadlineRule)
        deadline = _get_trade_deadline_date(tick_ctx)
        if deadline is not None and tick_ctx.current_date > deadline:
            self.last_stats = DealGeneratorStats(mode="SKIP_DEADLINE")
            return []

        ts = tick_ctx.get_team_situation(tid)

        # 즉시 중단
        if bool(getattr(ts, "constraints", None) and ts.constraints.cooldown_active):
            self.last_stats = DealGeneratorStats(mode="SKIP")
            return []

        posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
        if posture == "STAND_PAT" and float(getattr(ts, "urgency", 0.0) or 0.0) < 0.35:
            self.last_stats = DealGeneratorStats(mode="SKIP")
            return []

        budget = _scale_budget(self.config, ts)
        rng = random.Random(_compute_seed(self.config, tick_ctx, tid))

        stats = DealGeneratorStats(mode="SELL" if posture in {"SELL", "SOFT_SELL"} else "BUY")

        if posture in {"SELL", "SOFT_SELL"}:
            proposals = _generate_sell_mode(
                initiator_seller_id=tid,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=self.config,
                budget=budget,
                rng=rng,
                max_results=int(max_results),
                allow_locked_by_deal_id=allow_locked_by_deal_id,
                stats=stats,
            )
        else:
            proposals = _generate_buy_mode(
                initiator_buyer_id=tid,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=self.config,
                budget=budget,
                rng=rng,
                max_results=int(max_results),
                allow_locked_by_deal_id=allow_locked_by_deal_id,
                stats=stats,
            )

        self.last_stats = stats
        return proposals


# =============================================================================
# Budget scaling
# =============================================================================


def _scale_budget(cfg: DealGeneratorConfig, team_situation: Any) -> DealGeneratorBudget:
    posture = str(getattr(team_situation, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    urgency = float(getattr(team_situation, "urgency", 0.0) or 0.0)
    deadline = 0.0
    try:
        deadline = float(getattr(getattr(team_situation, "constraints", None), "deadline_pressure", 0.0) or 0.0)
    except Exception:
        deadline = 0.0

    posture_scale = {
        "AGGRESSIVE_BUY": 1.25,
        "SOFT_BUY": 1.00,
        "SELL": 1.05,
        "SOFT_SELL": 0.95,
        "STAND_PAT": 0.55,
    }.get(posture, 0.75)

    # urgency/deadline (0..1) -> intensity (0.85..1.35)
    u = max(0.0, min(1.0, urgency))
    d = max(0.0, min(1.0, deadline))
    intensity = 0.85 + 0.35 * u + 0.25 * d
    scale = posture_scale * intensity

    def _cap(val: int, hard: int) -> int:
        return max(1, min(int(val), int(hard)))

    return DealGeneratorBudget(
        max_targets=_cap(int(cfg.base_max_targets * scale), cfg.max_targets_hard),
        beam_width=_cap(int(cfg.base_beam_width * scale), 24),
        max_attempts_per_target=_cap(int(cfg.base_max_attempts_per_target * scale), cfg.max_attempts_per_target_hard),
        max_validations=_cap(int(cfg.base_max_validations * scale), cfg.max_validations_hard),
        max_evaluations=_cap(int(cfg.base_max_evaluations * scale), cfg.max_evaluations_hard),
        max_repairs=_cap(int(cfg.base_max_repairs), 3),
    )


def _compute_seed(cfg: DealGeneratorConfig, tick_ctx: TradeGenerationTickContext, team_id: str) -> int:
    """결정적 RNG seed (python hash() 금지)."""

    raw = f"{cfg.deterministic_seed_salt}|{tick_ctx.current_date.isoformat()}|{team_id}"
    h = hashlib.sha256(raw.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=False)


def _compute_sweetener_seed(
    cfg: DealGeneratorConfig,
    tick_ctx: TradeGenerationTickContext,
    *,
    initiator_team_id: str,
    counterparty_team_id: str,
    base_hash: str,
    skeleton_hash: str,
    trial_index: int,
) -> int:
    """결정적 sweetener RNG seed.

    목표
    - 같은 base deal(h_valid)이라도 skeleton/시도 순서에 따라
      다른 sweetener 조합을 시도할 수 있게 하되,
      탐색 순서/전역 RNG 상태에 과도하게 의존하지 않게 한다.
    """

    raw = (
        f"{cfg.deterministic_seed_salt}|sweetener|{tick_ctx.current_date.isoformat()}"
        f"|{str(initiator_team_id).upper()}|{str(counterparty_team_id).upper()}"
        f"|{str(base_hash)}|{str(skeleton_hash)}|{int(trial_index)}"
    )
    h = hashlib.sha256(raw.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=False)


# =============================================================================
# Rule SSOT helpers (trade_rules / apron thresholds)
# =============================================================================


def _trade_rules_map(tick_ctx: TradeGenerationTickContext) -> Mapping[str, Any]:
    base = getattr(getattr(tick_ctx, "rule_tick_ctx", None), "ctx_state_base", None)
    if isinstance(base, dict):
        league = base.get("league") if isinstance(base.get("league"), dict) else {}
        tr = league.get("trade_rules") if isinstance(league.get("trade_rules"), dict) else {}
        if isinstance(tr, dict):
            return tr
    return {}


def _get_trade_deadline_date(tick_ctx: TradeGenerationTickContext) -> Optional[date]:
    tr = _trade_rules_map(tick_ctx)
    raw = tr.get("trade_deadline")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except Exception:
        return None


def _get_second_apron_threshold(tick_ctx: TradeGenerationTickContext) -> float:
    tr = _trade_rules_map(tick_ctx)
    try:
        return float(tr.get("second_apron") or 0.0)
    except Exception:
        return 0.0


def _player_salary_dollars(tick_ctx: TradeGenerationTickContext, player_id: str) -> float:
    # Prefer tick SSOT salary map (dollars). Fallback: asset_catalog salary_m.
    pid = str(player_id)
    rt = getattr(tick_ctx, "rule_tick_ctx", None)
    try:
        if rt is not None:
            rt.ensure_active_roster_index()
            sal = rt.player_salary_map.get(pid)
            if sal is not None:
                return float(sal)
    except Exception:
        pass

    try:
        rt.ensure_active_roster_index()  # type: ignore[name-defined]
        owner = rt.player_team_map.get(pid)  # type: ignore[attr-defined]
    except Exception:
        owner = None

    cat = getattr(tick_ctx, "asset_catalog", None)
    if owner and cat is not None:
        out = cat.outgoing_by_team.get(str(owner).upper())
        if out is not None:
            c = out.players.get(pid)
            if c is not None:
                try:
                    return float(c.salary_m) * 1_000_000.0
                except Exception:
                    return 0.0
    return 0.0


def _estimate_team_payroll_after_dollars(
    tick_ctx: TradeGenerationTickContext,
    deal: Deal,
    team_id: str,
) -> float:
    # Estimate payroll_after (dollars) for soft 2nd apron guarding.
    tid = str(team_id).upper()
    rt = getattr(tick_ctx, "rule_tick_ctx", None)
    payroll_before = 0.0
    if rt is not None:
        try:
            rt.ensure_active_roster_index()
            payroll_before = float(rt.team_payroll_before_map.get(tid, 0.0))
        except Exception:
            payroll_before = 0.0

    if payroll_before <= 0.0:
        try:
            ts = tick_ctx.get_team_situation(tid)
            payroll_before = float(getattr(getattr(ts, "constraints", None), "payroll", 0.0) or 0.0)
        except Exception:
            payroll_before = 0.0

    outgoing = 0.0
    for a in deal.legs.get(tid, []) or []:
        if isinstance(a, PlayerAsset):
            outgoing += _player_salary_dollars(tick_ctx, a.player_id)

    incoming = 0.0
    for from_team, assets in deal.legs.items():
        if str(from_team).upper() == tid:
            continue
        for a in assets or []:
            if isinstance(a, PlayerAsset):
                incoming += _player_salary_dollars(tick_ctx, a.player_id)

    return float(payroll_before - outgoing + incoming)


# =============================================================================
# Mode orchestrators
# =============================================================================


def _generate_buy_mode(
    *,
    initiator_buyer_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    max_results: int,
    allow_locked_by_deal_id: Optional[str],
    stats: DealGeneratorStats,
) -> List[DealProposal]:
    buyer_id = str(initiator_buyer_id).upper()
    ts_buyer = tick_ctx.get_team_situation(buyer_id)

    # 탐색 상태
    # - seen_skeleton: repair 이전(스켈레톤/변형 단계) 중복 제거
    # - seen_output: 실제로 결과 리스트에 push된(=출력된) 딜 형태 중복 제거
    #
    # IMPORTANT
    # - repair 이후 h_valid를 seen_output에 선등록하면 sweetener 단계에서 갈라질 수 있는
    #   유니크 딜을 놓칠 수 있다. 따라서 seen_output은 '실제로 push된 딜'만 기록한다.
    seen_skeleton: Set[str] = set()
    seen_output: Set[str] = set()

    # base deal(h_valid) 재등장 시 evaluate 비용을 줄이기 위한 캐시
    # score는 opponent_repeat_count 등 동적 요소가 있어 캐시하지 않는다.
    base_eval_cache: Dict[str, Tuple[DealDecision, DealDecision, TeamDealEvaluation, TeamDealEvaluation]] = {}

    # 같은 base deal에서 sweetener를 여러 번 시도할 수 있게 하되 비용 폭증을 막기 위한 카운터
    sweetener_trials_by_base: Dict[str, int] = {}
    banned_asset_keys: Set[str] = set()
    banned_players: Set[str] = set()

    proposals: List[DealProposal] = []

    partner_counts: Dict[str, int] = {}

    max_sweetener_trials_per_base = int(getattr(config, "sweetener_max_trials_per_base", 2))

    targets = select_targets_buy(
        buyer_id,
        tick_ctx,
        catalog,
        config,
        budget=budget,
        rng=rng,
        banned_players=banned_players,
    )

    for t in targets:
        if len(proposals) >= max_results:
            break
        if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
            break

        stats.targets_considered += 1

        seller_id = str(t.from_team).upper()
        if seller_id == buyer_id:
            continue

        ts_seller = tick_ctx.get_team_situation(seller_id)
        if bool(getattr(ts_seller, "constraints", None) and ts_seller.constraints.cooldown_active):
            continue

        candidates = build_offer_skeletons_buy(
            buyer_id,
            seller_id,
            t,
            tick_ctx,
            catalog,
            config=config,
            budget=budget,
            rng=rng,
            banned_asset_keys=banned_asset_keys,
            banned_players=banned_players,
        )

        if not candidates:
            continue

        stats.skeletons_built += len(candidates)

        # 변형 확장: 타깃당 6~12개로 제한(폭발 방지)
        candidates = expand_variants(
            buyer_id,
            seller_id,
            t,
            candidates,
            tick_ctx,
            catalog,
            config=config,
            budget=budget,
            rng=rng,
            banned_asset_keys=banned_asset_keys,
            banned_players=banned_players,
        )
        variant_cap = min(12, max(6, int(budget.beam_width)))

        # soft guard: payroll_after_est 기준 2nd apron one-for-one 위반 가능 후보 제거(탐색 낭비/invalid 감소)
        if getattr(config, "soft_guard_second_apron_by_constraints", False):
            candidates = _soft_guard_second_apron_candidates(candidates, tick_ctx)
            if not candidates:
                continue

        candidates = _beam_select_candidates(
            candidates,
            buyer_id=buyer_id,
            seller_id=seller_id,
            tick_ctx=tick_ctx,
            catalog=catalog,
            rng=rng,
            cap=variant_cap,
        )

        attempts = 0
        for cand in candidates:
            if attempts >= budget.max_attempts_per_target:
                break
            if len(proposals) >= max_results:
                break
            if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
                break

            attempts += 1
            stats.candidates_attempted += 1

            h = dedupe_hash(cand.deal)
            if h in seen_skeleton:
                continue
            seen_skeleton.add(h)

            ok, cand2, v_used = repair_until_valid(
                cand,
                tick_ctx,
                catalog,
                config,
                allow_locked_by_deal_id=allow_locked_by_deal_id,
                budget=budget,
                banned_asset_keys=banned_asset_keys,
                banned_players=banned_players,
                stats=stats,
            )
            stats.validations += v_used
            if not ok or cand2 is None:
                continue

            # (A) repair 이후 base deal identity (수리 과정에서 서로 다른 스켈레톤이 같은 딜로 수렴 가능)
            h_valid = dedupe_hash(cand2.deal)

            # 이미 출력된 base인데 sweetener도 더 시도할 여지가 없으면 스킵(비용 가드)
            if h_valid in seen_output and (
                (not config.sweetener_enabled or int(config.sweetener_max_additions) <= 0)
                or int(sweetener_trials_by_base.get(h_valid, 0)) >= int(max_sweetener_trials_per_base)
            ):
                continue

            # evaluate (cache)
            cached = base_eval_cache.get(h_valid)
            if cached is None:
                base_prop, e_used = evaluate_and_score(
                    cand2.deal,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    tick_ctx=tick_ctx,
                    config=config,
                    tags=tuple(cand2.tags),
                    opponent_repeat_count=int(partner_counts.get(seller_id, 0)),
                    stats=stats,
                )
                stats.evaluations += e_used
                if base_prop is None:
                    continue
                base_eval_cache[h_valid] = (
                    base_prop.buyer_decision,
                    base_prop.seller_decision,
                    base_prop.buyer_eval,
                    base_prop.seller_eval,
                )
            else:
                bd, sd, be, se = cached
                base_prop = _proposal_from_cached_eval(
                    cand2.deal,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    buyer_decision=bd,
                    seller_decision=sd,
                    buyer_eval=be,
                    seller_eval=se,
                    config=config,
                    tags=tuple(cand2.tags),
                    opponent_repeat_count=int(partner_counts.get(seller_id, 0)),
                )

            # filter: 너무 말도 안 되는 손해
            if _should_discard_prop(base_prop, config):
                continue

            # sweetener loop (대개 buyer -> seller)
            best_prop = base_prop
            if config.sweetener_enabled and int(config.sweetener_max_additions) > 0:
                trial_idx = int(sweetener_trials_by_base.get(h_valid, 0))
                if trial_idx < int(max_sweetener_trials_per_base):
                    sweetener_trials_by_base[h_valid] = trial_idx + 1
                    local_seed = _compute_sweetener_seed(
                        config,
                        tick_ctx,
                        initiator_team_id=buyer_id,
                        counterparty_team_id=seller_id,
                        base_hash=h_valid,
                        skeleton_hash=h,
                        trial_index=trial_idx,
                    )
                    local_rng = random.Random(int(local_seed))

                    best_prop, extra_v, extra_e = maybe_apply_sweeteners(
                        base_prop,
                        tick_ctx=tick_ctx,
                        catalog=catalog,
                        config=config,
                        budget=budget,
                        allow_locked_by_deal_id=allow_locked_by_deal_id,
                        banned_asset_keys=banned_asset_keys,
                        rng=local_rng,
                        stats=stats,
                    )
                    stats.validations += extra_v
                    stats.evaluations += extra_e

            # (B) 최종 중복 제거는 '실제로 push된 딜'만 기준으로 한다.
            #     - sweetener 결과가 중복이면 base 딜을 fallback으로 push할 수 있어야 한다.
            pushed: Optional[DealProposal] = None

            h_best = dedupe_hash(best_prop.deal)
            if h_best not in seen_output:
                pushed = best_prop
                seen_output.add(h_best)
            else:
                # sweetened가 중복이면 base라도 유니크할 때는 결과로 남긴다.
                if h_valid not in seen_output:
                    pushed = base_prop
                    seen_output.add(h_valid)

            if pushed is None:
                continue

            proposals = _push_best(
                proposals,
                pushed,
                max_results=max_results,
            )
            partner_counts[pushed.seller_id] = int(partner_counts.get(pushed.seller_id, 0)) + 1

    proposals.sort(key=lambda p: p.score, reverse=True)
    return proposals[:max_results]


def _generate_sell_mode(
    *,
    initiator_seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    max_results: int,
    allow_locked_by_deal_id: Optional[str],
    stats: DealGeneratorStats,
) -> List[DealProposal]:
    seller_id = str(initiator_seller_id).upper()
    ts_seller = tick_ctx.get_team_situation(seller_id)

    # 탐색 상태
    # - seen_skeleton: repair 이전(스켈레톤/변형 단계) 중복 제거
    # - seen_output: 실제로 결과 리스트에 push된(=출력된) 딜 형태 중복 제거
    #
    # IMPORTANT
    # - repair 이후 h_valid를 seen_output에 선등록하면 sweetener 단계에서 갈라질 수 있는
    #   유니크 딜을 놓칠 수 있다. 따라서 seen_output은 '실제로 push된 딜'만 기록한다.
    seen_skeleton: Set[str] = set()
    seen_output: Set[str] = set()

    # base deal(h_valid) 재등장 시 evaluate 비용을 줄이기 위한 캐시
    # score는 opponent_repeat_count 등 동적 요소가 있어 캐시하지 않는다.
    base_eval_cache: Dict[str, Tuple[DealDecision, DealDecision, TeamDealEvaluation, TeamDealEvaluation]] = {}

    # 같은 base deal에서 sweetener를 여러 번 시도할 수 있게 하되 비용 폭증을 막기 위한 카운터
    sweetener_trials_by_base: Dict[str, int] = {}
    banned_asset_keys: Set[str] = set()
    banned_players: Set[str] = set()

    proposals: List[DealProposal] = []
    partner_counts: Dict[str, int] = {}

    max_sweetener_trials_per_base = int(getattr(config, "sweetener_max_trials_per_base", 2))

    sale_assets = select_targets_sell(
        seller_id,
        tick_ctx,
        catalog,
        config,
        budget=budget,
        rng=rng,
        banned_players=banned_players,
    )

    for s in sale_assets:
        if len(proposals) >= max_results:
            break
        if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
            break

        stats.targets_considered += 1

        buyer_candidates = select_buyers_for_sale_asset(
            seller_id,
            s,
            tick_ctx,
            catalog,
            config=config,
            budget=budget,
            rng=rng,
        )

        for buyer_id, match_tag in buyer_candidates:
            if len(proposals) >= max_results:
                break
            if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
                break

            buyer_id = str(buyer_id).upper()
            if buyer_id == seller_id:
                continue
            ts_buyer = tick_ctx.get_team_situation(buyer_id)
            if bool(getattr(ts_buyer, "constraints", None) and ts_buyer.constraints.cooldown_active):
                continue

            candidates = build_offer_skeletons_sell(
                seller_id=seller_id,
                buyer_id=buyer_id,
                sale_asset=s,
                match_tag=match_tag,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=config,
                budget=budget,
                rng=rng,
                banned_asset_keys=banned_asset_keys,
                banned_players=banned_players,
            )

            if not candidates:
                continue

            stats.skeletons_built += len(candidates)

            # soft guard: payroll_after_est 기준 2nd apron one-for-one 위반 가능 후보 제거(탐색 낭비/invalid 감소)
            if getattr(config, "soft_guard_second_apron_by_constraints", False):
                candidates = _soft_guard_second_apron_candidates(candidates, tick_ctx)
                if not candidates:
                    continue

            candidates = _beam_select_candidates(
                candidates,
                buyer_id=buyer_id,
                seller_id=seller_id,
                tick_ctx=tick_ctx,
                catalog=catalog,
                rng=rng,
                cap=max(1, int(budget.beam_width)),
            )

            attempts = 0
            for cand in candidates:
                if attempts >= budget.max_attempts_per_target:
                    break
                if len(proposals) >= max_results:
                    break
                if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
                    break

                attempts += 1
                stats.candidates_attempted += 1

                h = dedupe_hash(cand.deal)
                if h in seen_skeleton:
                    continue
                seen_skeleton.add(h)

                ok, cand2, v_used = repair_until_valid(
                    cand,
                    tick_ctx,
                    catalog,
                    config,
                    allow_locked_by_deal_id=allow_locked_by_deal_id,
                    budget=budget,
                    banned_asset_keys=banned_asset_keys,
                    banned_players=banned_players,
                    stats=stats,
                )
                stats.validations += v_used
                if not ok or cand2 is None:
                    continue

                # (A) repair 이후 base deal identity (수리 과정에서 서로 다른 스켈레톤이 같은 딜로 수렴 가능)
                h_valid = dedupe_hash(cand2.deal)

                # 이미 출력된 base인데 sweetener도 더 시도할 여지가 없으면 스킵(비용 가드)
                if h_valid in seen_output and (
                    (not config.sweetener_enabled or int(config.sweetener_max_additions) <= 0)
                    or int(sweetener_trials_by_base.get(h_valid, 0)) >= int(max_sweetener_trials_per_base)
                ):
                    continue

                # evaluate (cache)
                cached = base_eval_cache.get(h_valid)
                if cached is None:
                    base_prop, e_used = evaluate_and_score(
                        cand2.deal,
                        buyer_id=buyer_id,
                        seller_id=seller_id,
                        tick_ctx=tick_ctx,
                        config=config,
                        tags=tuple(cand2.tags),
                        opponent_repeat_count=int(partner_counts.get(buyer_id, 0)),
                        stats=stats,
                    )
                    stats.evaluations += e_used
                    if base_prop is None:
                        continue
                    base_eval_cache[h_valid] = (
                        base_prop.buyer_decision,
                        base_prop.seller_decision,
                        base_prop.buyer_eval,
                        base_prop.seller_eval,
                    )
                else:
                    bd, sd, be, se = cached
                    base_prop = _proposal_from_cached_eval(
                        cand2.deal,
                        buyer_id=buyer_id,
                        seller_id=seller_id,
                        buyer_decision=bd,
                        seller_decision=sd,
                        buyer_eval=be,
                        seller_eval=se,
                        config=config,
                        tags=tuple(cand2.tags),
                        opponent_repeat_count=int(partner_counts.get(buyer_id, 0)),
                    )

                if _should_discard_prop(base_prop, config):
                    continue

                best_prop = base_prop
                if config.sweetener_enabled and int(config.sweetener_max_additions) > 0:
                    trial_idx = int(sweetener_trials_by_base.get(h_valid, 0))
                    if trial_idx < int(max_sweetener_trials_per_base):
                        sweetener_trials_by_base[h_valid] = trial_idx + 1
                        local_seed = _compute_sweetener_seed(
                            config,
                            tick_ctx,
                            initiator_team_id=seller_id,
                            counterparty_team_id=buyer_id,
                            base_hash=h_valid,
                            skeleton_hash=h,
                            trial_index=trial_idx,
                        )
                        local_rng = random.Random(int(local_seed))

                        best_prop, extra_v, extra_e = maybe_apply_sweeteners(
                            base_prop,
                            tick_ctx=tick_ctx,
                            catalog=catalog,
                            config=config,
                            budget=budget,
                            allow_locked_by_deal_id=allow_locked_by_deal_id,
                            banned_asset_keys=banned_asset_keys,
                            rng=local_rng,
                            stats=stats,
                        )
                        stats.validations += extra_v
                        stats.evaluations += extra_e

                # (B) 최종 중복 제거는 '실제로 push된 딜'만 기준으로 한다.
                pushed: Optional[DealProposal] = None

                h_best = dedupe_hash(best_prop.deal)
                if h_best not in seen_output:
                    pushed = best_prop
                    seen_output.add(h_best)
                else:
                    if h_valid not in seen_output:
                        pushed = base_prop
                        seen_output.add(h_valid)

                if pushed is None:
                    continue

                proposals = _push_best(proposals, pushed, max_results=max_results)
                partner_counts[pushed.buyer_id] = int(partner_counts.get(pushed.buyer_id, 0)) + 1

    proposals.sort(key=lambda p: p.score, reverse=True)
    return proposals[:max_results]


def _push_best(existing: List[DealProposal], prop: DealProposal, *, max_results: int) -> List[DealProposal]:
    existing.append(prop)
    existing.sort(key=lambda p: p.score, reverse=True)
    return existing[: max_results]


def _should_discard_prop(prop: DealProposal, cfg: DealGeneratorConfig) -> bool:
    """상위 후보로 올릴 가치가 거의 없는 오퍼를 early discard.

    목표
    - 유저가 보기에 "NBA스럽지 않은"(한쪽이 극단적으로 손해) 오퍼가 상위에 뜨는 것을 방지
    - sweetener loop 이전에도 과감히 거른다(비용/노이즈 감소)

    주의
    - 이 함수는 '완전 불가능'을 판단하지 않는다(그건 validate).
    - 여기서는 '게임 경험' 기준으로 너무 엉터리인 오퍼를 제거한다.
    """

    mb = float(prop.buyer_eval.net_surplus) - float(prop.buyer_decision.required_surplus)
    ms = float(prop.seller_eval.net_surplus) - float(prop.seller_decision.required_surplus)

    # buyer는 게임상 '내 팀'일 가능성이 높으므로 더 강하게 보호
    if mb < float(cfg.discard_if_overpay_below):
        return True

    # 어느 한쪽이 극단적으로 손해면 폐기(상대에게도 NBA스럽지 않음)
    if mb < float(getattr(cfg, "discard_if_any_margin_below", -22.0)) or ms < float(getattr(cfg, "discard_if_any_margin_below", -22.0)):
        return True

    # REJECT인데 deficit이 큰 경우는 거의 의미 없음(스윗너 1~2개로도 복구 어려움)
    rej_thr = float(getattr(cfg, "discard_if_reject_margin_below", -14.0))
    if prop.buyer_decision.verdict == DealVerdict.REJECT and mb < rej_thr:
        return True
    if prop.seller_decision.verdict == DealVerdict.REJECT and ms < rej_thr:
        return True

    # 양쪽 모두 별로면 폐기
    if mb < float(cfg.discard_if_both_margins_below) and ms < float(cfg.discard_if_both_margins_below):
        return True

    return False


def _incoming_player_count(deal: Deal, team_id: str) -> int:
    """team_id 기준 incoming player count(2팀 딜 가정)."""
    tid = str(team_id).upper()
    other = [t for t in deal.teams if str(t).upper() != tid]
    if not other:
        return 0
    other_team = str(other[0]).upper()
    return sum(1 for a in deal.legs.get(other_team, []) if isinstance(a, PlayerAsset))


def _soft_guard_second_apron_candidates(
    candidates: List[DealCandidate],
    tick_ctx: TradeGenerationTickContext,
) -> List[DealCandidate]:
    """Soft guard: 2nd apron one-for-one 제약을 위반할 가능성이 큰 후보를 제거한다.

    SSOT는 validate_deal(SalaryMatchingRule)이며, 이 함수는 탐색 낭비를 줄이기 위한 휴리스틱이다.

    핵심 변경점(SSOT aligned)
    - TeamConstraints.apron_status(현 상태)로만 판단하지 않고,
      deal 적용 후 추정 payroll_after가 second_apron 이상일 때만 one-for-one을 강제한다.

    구현
    - payroll_after_est = payroll_before - outgoing_salary + incoming_salary (dollars)
    - if payroll_after_est >= second_apron:
        outgoing_players_count <= 1 AND incoming_players_count <= 1 이어야 통과

    fallback
    - second_apron 값을 SSOT에서 읽을 수 없으면 기존처럼 constraints.apron_status 기반으로만 soft guard.
    """
    second_apron = _get_second_apron_threshold(tick_ctx)
    out: List[DealCandidate] = []
    for c in candidates:
        d = c.deal
        ok = True
        for tid in [str(t).upper() for t in (d.teams or [])]:
            requires_guard = False

            if second_apron > 0.0:
                try:
                    payroll_after = _estimate_team_payroll_after_dollars(tick_ctx, d, tid)
                    if payroll_after >= float(second_apron):
                        requires_guard = True
                except Exception:
                    requires_guard = False
            else:
                # fallback: 기존 휴리스틱
                try:
                    ts = tick_ctx.get_team_situation(tid)
                    status = str(getattr(getattr(ts, "constraints", None), "apron_status", "") or "")
                    if status == "ABOVE_2ND_APRON":
                        requires_guard = True
                except Exception:
                    requires_guard = False

            if requires_guard:
                if _count_players(d, tid) > 1 or _incoming_player_count(d, tid) > 1:
                    ok = False
                    break
        if ok:
            out.append(c)
    return out


# =============================================================================
# Beam selection helpers (cheap heuristic pre-score)
# =============================================================================


def _cap_space_m(ts: Any) -> float:
    """TeamConstraints.cap_space는 달러 단위로 들어오는 경우가 많아서(프로젝트 코드 기준) M 단위로 변환."""
    try:
        c = getattr(ts, "constraints", None)
        v = float(getattr(c, "cap_space", 0.0) or 0.0)
    except Exception:
        return 0.0
    return v / 1_000_000.0


def _can_absorb_without_outgoing(ts: Any, incoming_salary_m: float, *, buffer_m: float = 0.25) -> bool:
    """플레이어를 보내지 않고(incoming only) salary를 흡수 가능한지(=cap space로 커버)."""
    cap_m = _cap_space_m(ts)
    return cap_m >= float(incoming_salary_m) + float(buffer_m)


def _sum_leg_player_salary_m(
    deal: Deal, *, team_id: str, out_cat: Optional[TeamOutgoingCatalog]
) -> float:
    if out_cat is None:
        return 0.0
    s = 0.0
    for a in deal.legs.get(str(team_id).upper(), []) or []:
        if isinstance(a, PlayerAsset):
            c = out_cat.players.get(a.player_id)
            if c is not None:
                s += float(c.salary_m)
    return float(s)


def _sum_leg_market_total(
    deal: Deal, *, team_id: str, out_cat: Optional[TeamOutgoingCatalog]
) -> float:
    if out_cat is None:
        return 0.0
    s = 0.0
    for a in deal.legs.get(str(team_id).upper(), []) or []:
        if isinstance(a, PlayerAsset):
            c = out_cat.players.get(a.player_id)
            if c is not None:
                s += float(c.market.total)
        elif isinstance(a, PickAsset):
            p = out_cat.picks.get(a.pick_id)
            if p is not None:
                s += float(p.market.total)
        elif isinstance(a, SwapAsset):
            # Swap은 catalog에 market이 없으므로(현재 프로젝트 구조) 0으로 둔다.
            # (beam pre-score용이므로 과도한 추정값을 넣지 않는다)
            s += 0.0
    return float(s)


def _prescore_candidate(
    cand: DealCandidate,
    *,
    buyer_id: str,
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
) -> float:
    """validate/evaluate 없이 후보를 정렬하기 위한 아주 가벼운 pre-score.

    목표:
    - 예산이 타이트할 때도 '실현 가능성 높은' 후보가 evaluate까지 올라가게 한다.
    - 완전 결정적이 되면 다양성이 죽으니, 실제 샘플링은 _beam_select_candidates가 담당.
    """

    buyer = str(buyer_id).upper()
    seller = str(seller_id).upper()

    buyer_out = catalog.outgoing_by_team.get(buyer)
    seller_out = catalog.outgoing_by_team.get(seller)

    d = cand.deal
    n_assets = sum(len(v) for v in d.legs.values())
    n_players = sum(1 for leg in d.legs.values() for a in leg if isinstance(a, PlayerAsset))

    score = 0.0

    # 1) 복잡도: 단순할수록 우선
    score -= 0.10 * max(0, int(n_assets) - 2)
    score -= 0.08 * max(0, int(n_players) - 2)

    # 2) salary plausibility (양쪽 모두 대충 체크)
    try:
        ts_buyer = tick_ctx.get_team_situation(buyer)
    except Exception:
        ts_buyer = None
    try:
        ts_seller = tick_ctx.get_team_situation(seller)
    except Exception:
        ts_seller = None

    buyer_in_m = _sum_leg_player_salary_m(d, team_id=seller, out_cat=seller_out)   # buyer가 받는 salary
    buyer_out_m = _sum_leg_player_salary_m(d, team_id=buyer, out_cat=buyer_out)    # buyer가 보내는 salary
    if buyer_in_m > 0.5:
        gap = abs(float(buyer_out_m) - float(buyer_in_m))
        score -= 0.55 * (gap / max(1.0, float(buyer_in_m)))
        # picks-only 류(보내는 선수가 거의 없음)인데 cap space도 없으면 강한 패널티
        if buyer_out_m < 0.10 and ts_buyer is not None:
            if not _can_absorb_without_outgoing(ts_buyer, buyer_in_m, buffer_m=0.0):
                score -= 5.0

    seller_in_m = buyer_out_m
    seller_out_m = buyer_in_m
    if seller_out_m > 0.5:
        gap = abs(float(seller_out_m) - float(seller_in_m))
        score -= 0.35 * (gap / max(1.0, float(seller_out_m)))
        if seller_in_m < 0.10 and ts_seller is not None:
            if not _can_absorb_without_outgoing(ts_seller, seller_in_m, buffer_m=0.0):
                score -= 2.0

    # 3) 대략적 가치 밸런스(과도한 overpay 후보를 아래로)
    gain_val = _sum_leg_market_total(d, team_id=seller, out_cat=seller_out)  # buyer가 얻는 가치(상대가 보내는 것)
    cost_val = _sum_leg_market_total(d, team_id=buyer, out_cat=buyer_out)    # buyer가 지불하는 가치
    if gain_val > 0.0:
        rel = (float(gain_val) - float(cost_val)) / max(10.0, float(gain_val))
        score += 0.80 * rel

    return float(score)


def _beam_select_candidates(
    candidates: List[DealCandidate],
    *,
    buyer_id: str,
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    rng: random.Random,
    cap: int,
) -> List[DealCandidate]:
    """랜덤 shuffle+slice 대신: pre-score 정렬 + 제한적 랜덤 샘플링(다양성 유지)."""
    cap_n = max(1, int(cap))
    if len(candidates) <= cap_n:
        return candidates

    scored: List[Tuple[float, float, DealCandidate]] = []
    for c in candidates:
        # tie-breaker용 deterministic random
        scored.append((_prescore_candidate(c, buyer_id=buyer_id, seller_id=seller_id, tick_ctx=tick_ctx, catalog=catalog), rng.random(), c))
    scored.sort(key=lambda x: (-x[0], x[1]))

    # 상위 일부는 고정, 나머지는 상위 풀에서 랜덤 추출
    n_fixed = max(2, cap_n // 2)
    fixed = [c for _, __, c in scored[:n_fixed]]

    pool = [c for _, __, c in scored[n_fixed : min(len(scored), n_fixed + cap_n * 3)]]
    rng.shuffle(pool)

    out = list(fixed)
    need = cap_n - len(out)
    if need > 0:
        out.extend(pool[:need])
    return out[:cap_n]


# =============================================================================
# Target selection
# =============================================================================


def select_targets_buy(
    buyer_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    *,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_players: Set[str],
) -> List[TargetCandidate]:
    """need_map 기반 BUY 타깃 후보 구성(우선순위 높은 후보가 앞)."""

    dc = tick_ctx.get_decision_context(buyer_id)
    need_map = dict(getattr(dc, "need_map", {}) or {})

    # fallback: TeamSituation.needs
    if not need_map:
        ts = tick_ctx.get_team_situation(buyer_id)
        for n in getattr(ts, "needs", []) or []:
            try:
                need_map[str(getattr(n, "tag", ""))] = float(getattr(n, "weight", 0.0) or 0.0)
            except Exception:
                continue

    tags_sorted = sorted(need_map.items(), key=lambda kv: float(kv[1] or 0.0), reverse=True)
    tags = [str(t) for t, w in tags_sorted if str(t).strip() and float(w or 0.0) > 0.0]

    ts = tick_ctx.get_team_situation(buyer_id)
    if str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper() == "STAND_PAT":
        tags = tags[:2]
    tags = tags[: int(config.need_tags_max)]

    buyer_u = str(buyer_id).upper()
    seller_out_cache: Dict[str, Optional[TeamOutgoingCatalog]] = {}
    seller_cooldown_cache: Dict[str, bool] = {}

    out: List[TargetCandidate] = []
    for tag in tags:
        refs: Sequence[IncomingPlayerRef] = catalog.incoming_by_need_tag.get(tag, tuple())
        if not refs and bool(config.incoming_use_cheap_pool):
            refs = catalog.incoming_cheap_by_need_tag.get(tag, tuple())
        w_need = float(need_map.get(tag, 0.0) or 0.0)

        # CORE가 섞여 있을 수 있으므로, 후보를 먼저 자르지 말고 일정 범위 내에서 "채울 때까지" 스캔한다.
        need_n = int(config.incoming_pool_per_tag)
        scan_limit = min(len(refs), need_n * 3)  # 3배 스캔 상한(고정)

        added_for_tag = 0
        for r in refs[:scan_limit]:
            from_team = str(r.from_team).upper()
            if from_team == buyer_u:
                continue
            if r.player_id in banned_players:
                continue

            # seller outgoing catalog 확보(캐시)
            seller_out = seller_out_cache.get(from_team)
            if seller_out is None and from_team not in seller_out_cache:
                seller_out = catalog.outgoing_by_team.get(from_team)
                seller_out_cache[from_team] = seller_out
            if seller_out is None:
                continue

            # seller cooldown 미리 컷(캐시)
            cd = seller_cooldown_cache.get(from_team)
            if cd is None:
                ts_seller = tick_ctx.get_team_situation(from_team)
                cd = bool(getattr(ts_seller, "constraints", None) and ts_seller.constraints.cooldown_active)
                seller_cooldown_cache[from_team] = cd
            if cd:
                continue

            # CORE/비매물 컷(타깃 단계에서)
            if not _is_seller_willing_to_move_player(r.player_id, seller_out):
                continue

            # 가벼운 rank score는 정렬에만 사용
            rank = float(r.tag_strength) * (0.55 + 0.45 * w_need) + 0.02 * float(r.market_total)
            rank -= 0.015 * float(r.salary_m)
            rank += rng.random() * 0.01
            out.append(
                TargetCandidate(
                    player_id=r.player_id,
                    from_team=from_team,
                    need_tag=str(tag),
                    tag_strength=float(rank),
                    market_total=float(r.market_total),
                    salary_m=float(r.salary_m),
                    remaining_years=float(r.remaining_years),
                    age=r.age,
                )
            )

            added_for_tag += 1
            if added_for_tag >= need_n:
                break

    out.sort(key=lambda t: (-t.tag_strength, -t.market_total, t.salary_m, t.player_id))
    return out[: int(budget.max_targets)]


def select_targets_sell(
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    *,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_players: Set[str],
) -> List[SellAssetCandidate]:
    """SELL 모드: initiator가 내놓을 매물(선수) 후보를 고른다."""

    out_cat = catalog.outgoing_by_team.get(str(seller_id).upper())
    if out_cat is None:
        return []

    # SELL 모드 우선순위: 현실적으로 팔 만한 버킷 중심
    priority: Tuple[BucketId, ...] = (
        "VETERAN_SALE",
        "EXPIRING",
        "SURPLUS_REDUNDANT",
        "SURPLUS_LOW_FIT",
        "FILLER_BAD_CONTRACT",
        "CONSOLIDATE",
        "FILLER_CHEAP",
    )

    ids: List[str] = []
    for b in priority:
        ids.extend(list(out_cat.player_ids_by_bucket.get(b, tuple())))

    # unique preserve order
    seen: Set[str] = set()
    uniq_ids = []
    for pid in ids:
        if pid in seen or pid in banned_players:
            continue
        seen.add(pid)
        uniq_ids.append(pid)

    sale: List[SellAssetCandidate] = []
    for pid in uniq_ids:
        c = out_cat.players.get(pid)
        if c is None:
            continue
        sale.append(
            SellAssetCandidate(
                player_id=pid,
                market_total=float(c.market.total),
                salary_m=float(c.salary_m),
                remaining_years=float(c.remaining_years),
                is_expiring=bool(c.is_expiring),
                top_tags=tuple(c.top_tags or ()),
            )
        )

    # prefer higher market & more surplus (heuristic: expiring + low fit)
    def score(x: SellAssetCandidate) -> float:
        exp_bonus = 0.7 if x.is_expiring else 0.0
        return float(x.market_total) + exp_bonus - 0.12 * float(x.remaining_years)

    sale.sort(key=lambda x: (-score(x), x.salary_m, x.player_id))

    # deterministically shuffle a bit within top slice for variety
    top = sale[: max(0, min(len(sale), 24))]
    rng.shuffle(top)
    sale = top + sale[len(top) :]

    return sale[: int(budget.max_targets)]


def select_buyers_for_sale_asset(
    seller_id: str,
    sale_asset: SellAssetCandidate,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    *,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
) -> List[Tuple[str, str]]:
    """SELL 모드: 특정 매물에 대해 관심 가질 가능성이 큰 buyer 팀을 고른다.

    Returns: list[(buyer_id, match_tag)]
    """

    tags = list(sale_asset.top_tags or ())
    if not tags:
        return []

    # 후보 팀: 전체 30팀에서 계산해도 비싸지 않지만, 여기선 상한을 둔다.
    # BUY 의지가 있는 팀을 우선.
    all_teams = list(catalog.outgoing_by_team.keys())
    rng.shuffle(all_teams)

    rows: List[Tuple[float, str, str]] = []
    for tid in all_teams:
        buyer_id = str(tid).upper()
        if buyer_id == str(seller_id).upper():
            continue

        ts = tick_ctx.get_team_situation(buyer_id)
        if bool(getattr(ts, "constraints", None) and ts.constraints.cooldown_active):
            continue

        posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
        posture_bonus = {
            "AGGRESSIVE_BUY": 1.2,
            "SOFT_BUY": 0.7,
            "STAND_PAT": 0.2,
            "SOFT_SELL": -0.3,
            "SELL": -0.6,
        }.get(posture, 0.0)

        dc = tick_ctx.get_decision_context(buyer_id)
        need_map = dict(getattr(dc, "need_map", {}) or {})

        best_tag = tags[0]
        best = 0.0
        for tag in tags[:4]:
            v = float(need_map.get(tag, 0.0) or 0.0)
            if v > best:
                best = v
                best_tag = tag

        urgency = float(getattr(ts, "urgency", 0.0) or 0.0)
        score = best + 0.35 * urgency + posture_bonus

        # 매우 낮으면 제외
        if score <= 0.10:
            continue

        rows.append((score, buyer_id, str(best_tag)))

        # hard cap to avoid worst-case O(teams*targets) blow-up
        if len(rows) >= 24:
            break

    rows.sort(key=lambda r: (-r[0], r[1]))

    # 최대 ~10팀만
    out = [(buyer_id, tag) for _, buyer_id, tag in rows[:10]]
    return out


# =============================================================================
# Offer skeletons
# =============================================================================


def _with_core_tags(tags: List[str], *, mode: str, focal_player_id: str, archetype: str) -> List[str]:
    """(D) 후속 디버깅/분석을 위해 일관된 핵심 태그를 보장한다."""

    out = list(tags)
    for t in (f"mode:{str(mode).upper()}", f"focal:{focal_player_id}", f"arch:{archetype}"):
        if t not in out:
            out.append(t)
    return out


def build_offer_skeletons_buy(
    buyer_id: str,
    seller_id: str,
    target: TargetCandidate,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    *,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
) -> List[DealCandidate]:
    """BUY 모드: target 1명 기준 2~4개 archetype 스켈레톤."""

    buyer_out = catalog.outgoing_by_team.get(str(buyer_id).upper())
    seller_out = catalog.outgoing_by_team.get(str(seller_id).upper())
    if buyer_out is None or seller_out is None:
        return []

    # seller가 해당 선수를 "매물"로 갖고 있는지(= outgoing pool에 포함) 확인
    if not _is_seller_willing_to_move_player(target.player_id, seller_out):
        return []

    # return-ban precheck
    cand_target = seller_out.players.get(target.player_id)
    if cand_target is not None:
        if str(buyer_id).upper() in set(cand_target.return_ban_teams or ()):
            return []

    ts_buyer = tick_ctx.get_team_situation(buyer_id)
    ts_seller = tick_ctx.get_team_situation(seller_id)

    # soft 2nd apron guard는 _soft_guard_second_apron_candidates(=payroll_after_est 기반)에서 처리

    # base deal: seller sends target
    base = Deal(
        teams=[str(buyer_id).upper(), str(seller_id).upper()],
        legs={
            str(buyer_id).upper(): [],
            str(seller_id).upper(): [PlayerAsset(kind="player", player_id=target.player_id)],
        },
    )

    out: List[DealCandidate] = []

    # archetype 1) picks-only
    # cap space로 흡수 가능한 경우에만 생성(그 외는 salary_matching repair로 억지 변형되는 비율이 높아 현실감/비용에 악영향)
    if _can_absorb_without_outgoing(ts_buyer, float(target.salary_m)):
        deal1 = _clone_deal(base)
        # seller rebuild이면 pick을 조금 더
        max_picks = 2 if str(getattr(ts_seller, "time_horizon", "RE_TOOL") or "RE_TOOL") == "REBUILD" else 1
        _add_pick_package(
            deal1,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND", "FIRST_SAFE"),
            max_picks=max_picks,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal1,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="picks_only",
                tags=_with_core_tags([f"need:{target.need_tag}", "pkg:picks"], mode="BUY", focal_player_id=target.player_id, archetype="picks_only"),
            )
        )

    # archetype 2) young + pick (one outgoing player)
    young_id = _pick_youngish_player(
        buyer_out,
        receiver_team_id=seller_id,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    if young_id:
        deal2 = _clone_deal(base)
        deal2.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=young_id))
        _add_pick_package(
            deal2,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal2,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="young_plus_pick",
                tags=_with_core_tags([f"need:{target.need_tag}", "pkg:young+pick"], mode="BUY", focal_player_id=target.player_id, archetype="young_plus_pick"),
            )
        )

    # archetype 3) player-for-player (salary-ish)
    filler_id = _pick_filler_player_for_salary(
        buyer_out,
        receiver_team_id=seller_id,
        target_salary_m=target.salary_m,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    if filler_id:
        deal3 = _clone_deal(base)
        deal3.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=filler_id))
        out.append(
            DealCandidate(
                deal=deal3,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="p4p_salary",
                tags=_with_core_tags([f"need:{target.need_tag}", "pkg:player_for_player"], mode="BUY", focal_player_id=target.player_id, archetype="p4p_salary"),
            )
        )

    # archetype 4) consolidate 2-for-1
    cons_id = _pick_bucket_player(
        buyer_out,
        bucket="CONSOLIDATE",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    cheap_id = _pick_bucket_player(
        buyer_out,
        bucket="FILLER_CHEAP",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    if cons_id and cheap_id and cons_id != cheap_id:
        deal4 = _clone_deal(base)
        deal4.legs[str(buyer_id).upper()].extend(
            [
                PlayerAsset(kind="player", player_id=cons_id),
                PlayerAsset(kind="player", player_id=cheap_id),
            ]
        )
        _add_pick_package(
            deal4,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal4,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="consolidate_2_for_1",
                tags=_with_core_tags([f"need:{target.need_tag}", "pkg:consolidate"], mode="BUY", focal_player_id=target.player_id, archetype="consolidate_2_for_1"),
            )
        )

    # shape cap
    trimmed: List[DealCandidate] = []
    for c in out:
        if _shape_ok(c.deal, config=config, catalog=catalog):
            trimmed.append(c)

    # beam cap
    return trimmed[: max(2, int(budget.beam_width))]


def build_offer_skeletons_sell(
    *,
    seller_id: str,
    buyer_id: str,
    sale_asset: SellAssetCandidate,
    match_tag: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
) -> List[DealCandidate]:
    """SELL 모드: (seller sends sale_asset.player_id) 기준 BUYER 패키지를 생성."""

    buyer_out = catalog.outgoing_by_team.get(str(buyer_id).upper())
    seller_out = catalog.outgoing_by_team.get(str(seller_id).upper())
    if buyer_out is None or seller_out is None:
        return []

    pid = sale_asset.player_id
    if pid in banned_players:
        return []

    # return-ban precheck
    c_sale = seller_out.players.get(pid)
    if c_sale is not None:
        if str(buyer_id).upper() in set(c_sale.return_ban_teams or ()):
            return []

    # base deal: seller sends player to buyer
    base = Deal(
        teams=[str(buyer_id).upper(), str(seller_id).upper()],
        legs={
            str(buyer_id).upper(): [],
            str(seller_id).upper(): [PlayerAsset(kind="player", player_id=pid)],
        },
    )

    ts_seller = tick_ctx.get_team_situation(seller_id)
    ts_buyer = tick_ctx.get_team_situation(buyer_id)
    time_horizon = str(getattr(ts_seller, "time_horizon", "RE_TOOL") or "RE_TOOL")

    # soft 2nd apron guard는 _soft_guard_second_apron_candidates(=payroll_after_est 기반)에서 처리

    out: List[DealCandidate] = []

    # archetype 1) buyer picks package to seller
    # buyer가 선수 없이 salary를 흡수할 cap space가 있을 때만 생성
    if _can_absorb_without_outgoing(ts_buyer, float(sale_asset.salary_m)):
        deal1 = _clone_deal(base)
        # rebuild seller는 picks 선호
        max_picks = 2 if time_horizon == "REBUILD" else 1
        _add_pick_package(
            deal1,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND", "FIRST_SAFE"),
            max_picks=max_picks,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal1,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=pid,
                archetype="buyer_picks",
                tags=_with_core_tags([f"match:{match_tag}", "pkg:picks"], mode="SELL", focal_player_id=pid, archetype="buyer_picks"),
            )
        )

    # archetype 2) buyer young + pick
    young_id = _pick_youngish_player(
        buyer_out,
        receiver_team_id=seller_id,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    if young_id:
        deal2 = _clone_deal(base)
        deal2.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=young_id))
        _add_pick_package(
            deal2,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal2,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=pid,
                archetype="buyer_young_plus_pick",
                tags=_with_core_tags([f"match:{match_tag}", "pkg:young+pick"], mode="SELL", focal_player_id=pid, archetype="buyer_young_plus_pick"),
            )
        )

    # archetype 3) buyer sends salary-ish player back (WIN_NOW seller라면 우선)
    if time_horizon in {"WIN_NOW", "RE_TOOL"}:
        filler_id = _pick_filler_player_for_salary(
            buyer_out,
            receiver_team_id=seller_id,
            target_salary_m=float(sale_asset.salary_m),
            banned_players=banned_players,
            must_be_aggregation_friendly=True,
        )
        if filler_id:
            deal3 = _clone_deal(base)
            deal3.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=filler_id))
            out.append(
                DealCandidate(
                    deal=deal3,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    focal_player_id=pid,
                    archetype="buyer_p4p",
                    tags=_with_core_tags([f"match:{match_tag}", "pkg:player_for_player"], mode="SELL", focal_player_id=pid, archetype="buyer_p4p"),
                )
            )

    # archetype 4) consolidate (buyer 2-for-1)
    cons_id = _pick_bucket_player(
        buyer_out,
        bucket="CONSOLIDATE",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    cheap_id = _pick_bucket_player(
        buyer_out,
        bucket="FILLER_CHEAP",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        must_be_aggregation_friendly=True,
    )
    if cons_id and cheap_id and cons_id != cheap_id:
        deal4 = _clone_deal(base)
        deal4.legs[str(buyer_id).upper()].extend(
            [PlayerAsset(kind="player", player_id=cons_id), PlayerAsset(kind="player", player_id=cheap_id)]
        )
        _add_pick_package(
            deal4,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal4,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=pid,
                archetype="buyer_consolidate",
                tags=_with_core_tags([f"match:{match_tag}", "pkg:consolidate"], mode="SELL", focal_player_id=pid, archetype="buyer_consolidate"),
            )
        )

    trimmed: List[DealCandidate] = []
    for c in out:
        if _shape_ok(c.deal, config=config, catalog=catalog):
            trimmed.append(c)

    return trimmed[: max(2, int(budget.beam_width))]


def _is_seller_willing_to_move_player(player_id: str, seller_out: TeamOutgoingCatalog) -> bool:
    core = set(seller_out.player_ids_by_bucket.get("CORE", tuple()))
    if player_id in core:
        return False
    for b, ids in seller_out.player_ids_by_bucket.items():
        if b == "CORE":
            continue
        if player_id in ids:
            return True
    return False


# =============================================================================
# Candidate variant expansion (light beam)
# =============================================================================


def expand_variants(
    buyer_id: str,
    seller_id: str,
    target: TargetCandidate,
    base_candidates: List[DealCandidate],
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    *,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
) -> List[DealCandidate]:
    """스켈레톤을 '얕게' 확장한다.

    목표
    - 타깃당 6~12개 수준에서만 변형을 만들어 탐색을 깊게(하지만 폭발은 방지).
    - 변형은 **동일 archetype 내에서** player/pick을 약간 교체하는 수준만 수행.

    주의
    - validate/evaluate 비용이 크므로, 여기서는 **항상 정적(cap) 상한**을 둔다.
    - 중복 제거는 상위 루프(dedupe_hash)에서 처리한다.
    """

    goal = min(12, max(6, int(budget.beam_width)))
    if not base_candidates:
        return []

    buyer = str(buyer_id).upper()
    seller = str(seller_id).upper()

    buyer_out = catalog.outgoing_by_team.get(buyer)
    if buyer_out is None:
        return list(base_candidates)

    out: List[DealCandidate] = list(base_candidates)

    # 내부 hard cap: goal의 2배를 넘기지 않음(폭발 방지)
    hard_cap = max(goal, min(24, goal * 2))

    def _push(deal: Deal, archetype: str, tags: List[str]) -> None:
        if len(out) >= hard_cap:
            return
        if not _shape_ok(deal, config=config, catalog=catalog):
            return
        out.append(
            DealCandidate(
                deal=deal,
                buyer_id=buyer,
                seller_id=seller,
                focal_player_id=target.player_id,
                archetype=archetype,
                tags=_with_core_tags(tags, mode="BUY", focal_player_id=target.player_id, archetype=archetype),
            )
        )

    def _base_deal() -> Deal:
        return Deal(
            teams=[buyer, seller],
            legs={
                buyer: [],
                seller: [PlayerAsset(kind="player", player_id=target.player_id)],
            },
        )

    # --- archetype: picks-only variants
    # cap space 흡수 가능할 때만
    try:
        ts_buyer = tick_ctx.get_team_situation(buyer)
    except Exception:
        ts_buyer = None
    if ts_buyer is not None and _can_absorb_without_outgoing(ts_buyer, float(target.salary_m)):
        # (SECOND) / (SECOND+SECOND) / (FIRST_SAFE) / (FIRST_SAFE+SECOND)
        pick_plans: List[Tuple[Tuple[str, ...], int]] = [
            (("SECOND",), 1),
            (("SECOND", "SECOND"), 2),
            (("FIRST_SAFE",), 1),
            (("FIRST_SAFE", "SECOND"), 2),
        ]
        for prefer, max_picks in pick_plans:
            d = _base_deal()
            _add_pick_package(
                d,
                from_team=buyer,
                out_cat=buyer_out,
                catalog=catalog,
                rng=rng,
                prefer=prefer,
                max_picks=max_picks,
                config=config,
                banned_asset_keys=banned_asset_keys,
            )
            _push(d, "picks_only", [f"need:{target.need_tag}", "pkg:picks", "var:picks"])

    # --- archetype: young + pick variants (top 2 youngish)
    young_ids = _top_k_youngish_players(buyer_out, k=2, banned_players=banned_players, receiver_team_id=seller)
    for pid in young_ids:
        for prefer, max_picks in [(("SECOND",), 1), (("SECOND", "SECOND"), 2)]:
            d = _base_deal()
            d.legs[buyer].append(PlayerAsset(kind="player", player_id=pid))
            _add_pick_package(
                d,
                from_team=buyer,
                out_cat=buyer_out,
                catalog=catalog,
                rng=rng,
                prefer=prefer,
                max_picks=max_picks,
                config=config,
                banned_asset_keys=banned_asset_keys,
            )
            _push(d, "young_plus_pick", [f"need:{target.need_tag}", "pkg:young+pick", "var:young"])

    # --- archetype: p4p salary variants (top 3 fillers by salary gap)
    filler_ids = _top_k_fillers_by_salary_gap(
        buyer_out,
        target_salary_m=float(target.salary_m),
        k=3,
        banned_players=banned_players,
        receiver_team_id=seller,
    )
    for pid in filler_ids:
        d = _base_deal()
        d.legs[buyer].append(PlayerAsset(kind="player", player_id=pid))
        _push(d, "p4p_salary", [f"need:{target.need_tag}", "pkg:player_for_player", "var:salary"])

    # --- archetype: consolidate variants (top 2 consolidate + cheap fillers 2)
    cons_ids = _top_k_bucket_players_by_market(
        buyer_out,
        bucket="CONSOLIDATE",
        k=2,
        banned_players=banned_players,
        descending=True,
        receiver_team_id=seller,
    )
    cheap_ids = _top_k_bucket_players_by_market(
        buyer_out,
        bucket="FILLER_CHEAP",
        k=2,
        banned_players=banned_players,
        descending=False,
        receiver_team_id=seller,
    )
    for cid in cons_ids:
        for fid in cheap_ids:
            if cid == fid:
                continue
            d = _base_deal()
            d.legs[buyer].extend(
                [
                    PlayerAsset(kind="player", player_id=cid),
                    PlayerAsset(kind="player", player_id=fid),
                ]
            )
            _add_pick_package(
                d,
                from_team=buyer,
                out_cat=buyer_out,
                catalog=catalog,
                rng=rng,
                prefer=("SECOND",),
                max_picks=1,
                config=config,
                banned_asset_keys=banned_asset_keys,
            )
            _push(
                d,
                "consolidate_2_for_1",
                [f"need:{target.need_tag}", "pkg:consolidate", "var:consolidate"],
            )

    # 마지막으로 goal 수준에서만 남기기(상위에서 shuffle 후 slice하지만,
    # 여기서도 폭발 방지 차원에서 한 번 더 컷)
    if len(out) > hard_cap:
        out = out[:hard_cap]
    return out


def _top_k_youngish_players(
    out: TeamOutgoingCatalog,
    *,
    k: int,
    banned_players: Set[str],
    receiver_team_id: Optional[str] = None,
    must_be_aggregation_friendly: bool = True,
) -> List[str]:
    """버킷에 YOUNG가 없으므로 age 기반으로 'young-ish' top-k.

    BUY 모드 variant 생성에서 invalid 낭비를 줄이기 위해,
    - receiver_team_id가 주어지면 return_ban_teams(되돌아가기 금지) 사전 필터를 적용한다.
    - must_be_aggregation_friendly=True면 aggregation_solo_only 후보는 제외한다.
    """
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    cands: List[PlayerTradeCandidate] = []
    for b in ("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_CHEAP", "CONSOLIDATE"):
        for pid in out.player_ids_by_bucket.get(b, tuple()):
            if pid in banned_players:
                continue
            c = out.players.get(pid)
            if c is None:
                continue

            if receiver and receiver in (c.return_ban_teams or ()):
                continue
            if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
                continue

            age = c.snap.age
            if age is not None and float(age) <= 24.5:
                cands.append(c)
    if not cands:
        return []
    cands.sort(key=lambda c: (-float(c.market.total), float(c.salary_m), c.player_id))
    return [c.player_id for c in cands[: int(k)]]


def _top_k_fillers_by_salary_gap(
    out: TeamOutgoingCatalog,
    *,
    target_salary_m: float,
    k: int,
    banned_players: Set[str],
    receiver_team_id: Optional[str] = None,
    must_be_aggregation_friendly: bool = True,
) -> List[str]:
    """target salary 근처 filler 후보 top-k.

    BUY 모드 variant 생성에서 invalid 낭비를 줄이기 위해,
    - receiver_team_id가 주어지면 return_ban_teams 사전 필터를 적용한다.
    - must_be_aggregation_friendly=True면 aggregation_solo_only 후보는 제외한다.
    """
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    ids: List[str] = []
    for b in ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT"):
        ids.extend(list(out.player_ids_by_bucket.get(b, tuple())))

    scored: List[Tuple[float, float, str]] = []
    for pid in ids:
        if pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue

        if receiver and receiver in (c.return_ban_teams or ()):
            continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue

        gap = abs(float(c.salary_m) - float(target_salary_m))
        scored.append((gap, float(c.market.total), pid))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [pid for _, _, pid in scored[: int(k)]]


def _top_k_bucket_players_by_market(
    out: TeamOutgoingCatalog,
    *,
    bucket: BucketId,
    k: int,
    banned_players: Set[str],
    descending: bool,
    receiver_team_id: Optional[str] = None,
    must_be_aggregation_friendly: bool = True,
) -> List[str]:
    """특정 버킷에서 market.total 기준 top-k.

    BUY 모드 variant 생성에서 invalid 낭비를 줄이기 위해,
    - receiver_team_id가 주어지면 return_ban_teams 사전 필터를 적용한다.
    - must_be_aggregation_friendly=True면 aggregation_solo_only 후보는 제외한다.
    """
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    scored: List[Tuple[float, str]] = []
    for pid in out.player_ids_by_bucket.get(bucket, tuple()):
        if pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue

        if receiver and receiver in (c.return_ban_teams or ()):
            continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue

        scored.append((float(c.market.total), pid))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=bool(descending))
    return [pid for _, pid in scored[: int(k)]]


# =============================================================================
# Validate + Repair
# =============================================================================

def repair_until_valid(
    cand: DealCandidate,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    *,
    allow_locked_by_deal_id: Optional[str],
    budget: DealGeneratorBudget,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    stats: DealGeneratorStats,
) -> Tuple[bool, Optional[DealCandidate], int]:
    """validate -> 실패 유형에 따라 최대 budget.max_repairs회 repair.

    Returns: (ok, candidate_or_none, validations_used)
    """

    validations_used = 0

    if not _shape_ok(cand.deal, config=config, catalog=catalog):
        return False, None, validations_used

    for _ in range(int(budget.max_repairs) + 1):
        try:
            tick_ctx.validate_deal(cand.deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
            validations_used += 1
            return True, cand, validations_used
        except TradeError as err:
            validations_used += 1
            failure = parse_trade_error(err)
            stats.bump_failure(str(failure.kind.value))

            if cand.repairs_used >= int(budget.max_repairs):
                _apply_prune_side_effects(failure, banned_asset_keys, banned_players)
                return False, None, validations_used

            repaired = repair_once(
                cand,
                failure,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=config,
                banned_asset_keys=banned_asset_keys,
                banned_players=banned_players,
            )
            if not repaired:
                _apply_prune_side_effects(failure, banned_asset_keys, banned_players)
                return False, None, validations_used

            cand.repairs_used += 1
            stats.repairs += 1

            # repair 후 shape check
            if not _shape_ok(cand.deal, config=config, catalog=catalog):
                return False, None, validations_used
        except Exception:
            # 상업용 루프: 예상 못한 예외로 tick이 죽지 않게 방어
            validations_used += 1
            stats.bump_failure("unexpected_exception_validate")
            return False, None, validations_used

    return False, None, validations_used


def repair_once(
    cand: DealCandidate,
    failure: RuleFailure,
    *,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
) -> bool:
    """실패 유형에 따라 '최소 수정' 1회 적용.

    True면 cand.deal이 mutate되었음을 의미.
    False면 이 후보는 prune.
    """

    # 구조적으로 수리 의미가 거의 없는 유형
    if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
        return False

    if failure.kind == RuleFailureKind.PLAYER_ELIGIBILITY:
        reason = failure.reason or ""
        pid = failure.player_id
        if not pid:
            return False
        if reason == "recent_contract_signing":
            banned_players.add(pid)
            return False
        if reason == "aggregation_ban":
            # aggregation_ban: 해당 선수는 '트레이드 불가'가 아니라
            # '다른 선수와 묶어서(2+ outgoing) 보낼 수 없음'이므로,
            # 최소 수정은 pid를 유지하고 나머지 outgoing player를 제거하여 1-for-1로 만드는 것이다.
            team_id = str(failure.team_id or "").upper()
            if not team_id or team_id not in cand.deal.legs:
                return False
            assets = list(cand.deal.legs[team_id] or [])
            players = [a for a in assets if isinstance(a, PlayerAsset)]
            if len(players) <= 1:
                return False

            keep_player: Optional[PlayerAsset] = None
            for a in players:
                if a.player_id == pid:
                    keep_player = a
                    break
            if keep_player is None:
                # fallback: pid가 leg에 없으면 첫 번째 player만 남긴다.
                keep_player = players[0]

            non_players: List[Asset] = [a for a in assets if not isinstance(a, PlayerAsset)]
            cand.deal.legs[team_id] = [keep_player] + non_players
            cand.tags.append("repair:aggregation_keep_solo")
            return True
        return False

    if failure.kind == RuleFailureKind.RETURN_TO_TRADING_TEAM:
        return False

    if failure.kind == RuleFailureKind.ROSTER_LIMIT:
        team_id = str(failure.team_id or "").upper()
        if not team_id:
            return False
        return _repair_roster_limit(cand, team_id, catalog, config)

    if failure.kind in (RuleFailureKind.SALARY_MATCHING, RuleFailureKind.SECOND_APRON_ONE_FOR_ONE):
        team_id = str(failure.team_id or "").upper()
        if not team_id:
            return False
        if failure.kind == RuleFailureKind.SECOND_APRON_ONE_FOR_ONE:
            return _repair_second_apron_one_for_one(cand, team_id, catalog)
        return _repair_salary_matching(cand, team_id, catalog, config, failure)

    if failure.kind == RuleFailureKind.PICK_RULES:
        team_id = str(failure.team_id or "").upper()
        return _repair_pick_rules(cand, team_id, catalog, config, failure)

    return False


def _apply_prune_side_effects(
    failure: RuleFailure,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
) -> None:
    # (C) 같은 invalid를 반복 생성하지 않도록 금지 목록에 반영
    if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
        if failure.asset_key:
            banned_asset_keys.add(failure.asset_key)

    # ownership에서 플레이어 미소유는 플레이어 후보 자체를 금지하면 효과가 좋다.
    if failure.kind == RuleFailureKind.OWNERSHIP and failure.player_id:
        banned_players.add(failure.player_id)

    if failure.kind == RuleFailureKind.PLAYER_ELIGIBILITY and failure.player_id and failure.reason == "recent_contract_signing":
        banned_players.add(failure.player_id)


@dataclass(frozen=True, slots=True)
class SalaryMatchSimResult:
    """SalaryMatchingRule의 핵심 계산을 로컬에서 재현한 결과(달러 단위 정수).

    SSOT(TradeGenerationTickContext.validate_deal)에서 나온 TradeError.details를 기반으로,
    repair 단계에서 '이 filler를 추가하면 통과할 가능성이 있는가?'를 빠르게 판정하기 위해 사용한다.

    주의:
    - 실제 SSOT는 float + math.floor 기반이므로, floor 연산에는 eps를 더해 부동소수 오차를 상쇄한다.
    - BELOW_FIRST_APRON의 large 구간(1.25배)은 정수 연산((out*5)//4)로 정확히 재현한다.
    """

    ok: bool
    status: str
    method: str
    allowed_in_d: int
    payroll_after_d: int
    max_incoming_cap_room_d: Optional[int] = None
    reason: Optional[str] = None


def _to_int_dollars(x: Any) -> int:
    """float/int/str 등을 달러 단위 정수로 안전하게 변환."""
    try:
        return int(round(float(x)))
    except Exception:
        return 0


def _simulate_salary_matching(
    *,
    payroll_before_d: int,
    outgoing_salary_d: int,
    incoming_salary_d: int,
    trade_rules: Mapping[str, Any],
    eps: float = 1e-6,
) -> SalaryMatchSimResult:
    """SalaryMatchingRule.validate()의 계산을 달러 정수로 재현.

    Returns:
        SalaryMatchSimResult(ok, status, method, allowed_in_d, payroll_after_d, ...)
    """

    # defaults: trade/trades/rules/builtin/salary_matching_rule.py 와 동일
    salary_cap_d = _to_int_dollars(trade_rules.get("salary_cap") or 0.0)
    first_apron_d = _to_int_dollars(trade_rules.get("first_apron") or 0.0)
    second_apron_d = _to_int_dollars(trade_rules.get("second_apron") or 0.0)

    match_small_out_max_d = _to_int_dollars(trade_rules.get("match_small_out_max") or 7_500_000)
    match_mid_out_max_d = _to_int_dollars(trade_rules.get("match_mid_out_max") or 29_000_000)
    match_mid_add_d = _to_int_dollars(trade_rules.get("match_mid_add") or 7_500_000)
    match_buffer_d = _to_int_dollars(trade_rules.get("match_buffer") or 250_000)

    first_apron_mult = float(trade_rules.get("first_apron_mult") or 1.10)
    second_apron_mult = float(trade_rules.get("second_apron_mult") or 1.00)

    payroll_after_d = int(payroll_before_d - outgoing_salary_d + incoming_salary_d)

    if payroll_after_d >= second_apron_d:
        status = "SECOND_APRON"
    elif payroll_after_d >= first_apron_d:
        status = "FIRST_APRON"
    else:
        status = "BELOW_FIRST_APRON"

    # cap-room exception (SSOT와 동일한 위치/순서)
    if payroll_before_d < salary_cap_d:
        cap_room_d = salary_cap_d - payroll_before_d
        max_incoming_d = cap_room_d + outgoing_salary_d
        if incoming_salary_d <= max_incoming_d:
            return SalaryMatchSimResult(
                ok=True,
                status=status,
                method="cap_room",
                allowed_in_d=max_incoming_d,
                payroll_after_d=payroll_after_d,
                max_incoming_cap_room_d=max_incoming_d,
                reason="cap_room_ok",
            )

    if outgoing_salary_d <= 0:
        return SalaryMatchSimResult(
            ok=False,
            status=status,
            method="outgoing_required",
            allowed_in_d=0,
            payroll_after_d=payroll_after_d,
            reason="outgoing_required",
        )

    # NOTE: SECOND_APRON one-for-one 제약은 여기서는 다루지 않는다.
    # (generator가 SECOND_APRON에 대해 별도 repair 루트를 가지며, 이 helper는 'allowed_in' 계산 목적)

    if status == "SECOND_APRON":
        allowed_in_d = int(math.floor(outgoing_salary_d * second_apron_mult + eps))
        method = "outgoing_second_apron"
    elif status == "FIRST_APRON":
        allowed_in_d = int(math.floor(outgoing_salary_d * first_apron_mult + eps))
        method = "outgoing_first_apron"
    else:
        if outgoing_salary_d <= match_small_out_max_d:
            allowed_in_d = int(2 * outgoing_salary_d + match_buffer_d)
        elif outgoing_salary_d <= match_mid_out_max_d:
            allowed_in_d = int(outgoing_salary_d + match_mid_add_d)
        else:
            allowed_in_d = int((outgoing_salary_d * 5) // 4 + match_buffer_d)
        method = "outgoing_below_first_apron"

    if incoming_salary_d > allowed_in_d:
        return SalaryMatchSimResult(
            ok=False,
            status=status,
            method=method,
            allowed_in_d=allowed_in_d,
            payroll_after_d=payroll_after_d,
            reason="incoming_gt_allowed_in",
        )

    return SalaryMatchSimResult(
        ok=True,
        status=status,
        method=method,
        allowed_in_d=allowed_in_d,
        payroll_after_d=payroll_after_d,
        reason="ok",
    )


def _repair_salary_matching(
    cand: DealCandidate,
    failing_team: str,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    failure: RuleFailure,
) -> bool:
    """SalaryMatchingRule 실패 수리.

    가장 안전한 수리:
    - failing_team outgoing에 filler 1명을 추가(FILLER_CHEAP -> EXPIRING -> FILLER_BAD_CONTRACT)

    단, failure.details.status == SECOND_APRON이면 multi-player가 2nd apron one-for-one을
    촉발할 가능성이 매우 높으므로 여기서 추가 수리를 시도하지 않는다.
    """

    status = str(failure.status or "")
    method = str(failure.method or "")
    if status == "SECOND_APRON":
        # second_apron_one_for_one은 RuleFailureKind.SECOND_APRON_ONE_FOR_ONE로 별도 수리된다.
        if method == "outgoing_second_apron":
            return _repair_second_apron_salary_mismatch(cand, failing_team, catalog, config, failure)
        return False

    out_catalog = catalog.outgoing_by_team.get(failing_team)
    if out_catalog is None:
        return False

    # max_players_per_side guard
    if _count_players(cand.deal, failing_team) >= int(config.max_players_per_side):
        return False

    # aggregation_solo_only가 이미 포함되면 추가 player를 붙이면 바로 다시 실패할 확률이 큼
    for a in cand.deal.legs.get(failing_team, []):
        if isinstance(a, PlayerAsset):
            c = out_catalog.players.get(a.player_id)
            if c is not None and bool(getattr(c, "aggregation_solo_only", False)):
                return False

    # receiver team(상대팀) 계산: return-ban 프리필터에 사용
    other = [t for t in cand.deal.teams if str(t).upper() != str(failing_team).upper()]
    receiver_team = str(other[0]).upper() if other else None

    # SalaryMatchingRule failure.details는 달러(float) 기반이므로, 달러 정수로 변환해 사용한다.
    # 이 숫자들은 SSOT(validate_deal)의 '현재 딜 상태' 기준이며, filler 추가 후 상태는 여기서 재시뮬레이션한다.
    payroll_before_d = _to_int_dollars(failure.details.get("payroll_before"))
    outgoing_salary_d0 = _to_int_dollars(failure.details.get("outgoing_salary"))
    incoming_salary_d0 = _to_int_dollars(failure.details.get("incoming_salary"))

    if incoming_salary_d0 <= 0:
        return False

    # max_players_per_side guard는 이미 위에서 통과했으므로, 여기서는 후보 스캔/선정만 한다.
    already = {a.player_id for a in cand.deal.legs.get(failing_team, []) if isinstance(a, PlayerAsset)}

    # 후보 filler를 버킷에서 전수 스캔하고, "salary matching을 실제로 통과시키는" 후보만 남긴다.
    buckets: Tuple[BucketId, ...] = ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT")
    seen: Set[str] = set()
    passing: List[Tuple[int, float, str]] = []  # (salary_d, market_total, player_id)

    trade_rules = catalog.trade_rules or {}

    for b in buckets:
        for pid in out_catalog.player_ids_by_bucket.get(b, tuple()):
            pid = str(pid)
            if pid in seen or pid in already:
                continue
            seen.add(pid)

            c = out_catalog.players.get(pid)
            if c is None:
                continue

            # return-ban / aggregation-solo-only 필터 (기존 _pick_bucket_player와 동일한 의도)
            if receiver_team and receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                continue
            if bool(getattr(c, "aggregation_solo_only", False)):
                continue

            filler_salary_d = int(round(float(c.salary_m) * 1_000_000.0))
            if filler_salary_d <= 0:
                continue

            sim = _simulate_salary_matching(
                payroll_before_d=payroll_before_d,
                outgoing_salary_d=outgoing_salary_d0 + filler_salary_d,
                incoming_salary_d=incoming_salary_d0,
                trade_rules=trade_rules,
            )
            if not sim.ok:
                continue

            mkt = float(getattr(c.market, "total", 0.0))
            passing.append((filler_salary_d, mkt, pid))

    if not passing:
        return False

    # "필요 샐러리를 충족하는 최소 salary" 우선, 그 안에서 market.total 최소
    passing.sort(key=lambda t: (t[0], t[1], t[2]))
    filler = passing[0][2]

    cand.deal.legs[failing_team].append(PlayerAsset(kind="player", player_id=filler))
    cand.tags.append("repair:add_filler_salary")
    return True


def _repair_second_apron_salary_mismatch(
    cand: DealCandidate,
    failing_team: str,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    failure: RuleFailure,
) -> bool:
    """SECOND_APRON + method=outgoing_second_apron salary mismatch 수리.

    원칙:
    - one-for-one 형태는 유지(양쪽 leg에서 PlayerAsset 1명씩인 케이스만)
    - focal_player_id(타깃)는 가능하면 바꾸지 않는다.
      * failing_team outgoing이 focal이 아니면: failing_team outgoing을 더 비싼 선수로 교체(outgoing↑)
      * failing_team outgoing이 focal이면: 상대팀 outgoing(=failing_team incoming)을 더 싼 선수로 교체(incoming↓)
    """

    team = str(failing_team).upper()
    others = [t for t in cand.deal.teams if str(t).upper() != team]
    if not others:
        return False
    other = str(others[0]).upper()

    # --- one-for-one 형태만 다룬다(안전/비용 제한)
    out_players = [a for a in cand.deal.legs.get(team, []) if isinstance(a, PlayerAsset)]
    if len(out_players) != 1:
        return False

    incoming_players: List[PlayerAsset] = []
    for a in cand.deal.legs.get(other, []) or []:
        if not isinstance(a, PlayerAsset):
            continue
        recv = str(resolve_asset_receiver(cand.deal, other, a)).upper()
        if recv == team:
            incoming_players.append(a)
    if len(incoming_players) != 1:
        return False

    incoming_salary = float(failure.details.get("incoming_salary") or 0.0)
    outgoing_salary = float(failure.details.get("outgoing_salary") or 0.0)
    if incoming_salary <= 0.0 or outgoing_salary <= 0.0:
        return False
    if incoming_salary <= outgoing_salary:
        return False

    # dollars 기반 비교: validate(SSOT)와 정렬해 float/rounding으로 인한 재실패를 줄인다.
    # 상업용 기본값: 0.001M(=1,000달러) 수준의 최소 여유
    EPS_M = 0.001
    eps_d = int(round(EPS_M * 1_000_000.0))

    incoming_d = int(round(incoming_salary))
    outgoing_d = int(round(outgoing_salary))

    all_pids = {
        a.player_id
        for leg in cand.deal.legs.values()
        for a in (leg or [])
        if isinstance(a, PlayerAsset)
    }

    out_pid = str(out_players[0].player_id)
    focal_pid = str(cand.focal_player_id or "")

    # =========================================================
    # Case A: failing_team outgoing이 focal이 아니면 -> outgoing을 올리는 교체
    # =========================================================
    if out_pid != focal_pid:
        out_cat = catalog.outgoing_by_team.get(team)
        if out_cat is None:
            return False

        receiver_team = other
        required_out_d = incoming_d + eps_d  # SECOND_APRON: incoming <= outgoing(달러) 목표

        best_pid: Optional[str] = None
        best_key: Optional[Tuple[int, float, int]] = None  # (overshoot_d, market, salary_d)

        scan_buckets: Tuple[BucketId, ...] = (
            "FILLER_BAD_CONTRACT",
            "EXPIRING",
            "FILLER_CHEAP",
            "CONSOLIDATE",
            "SURPLUS_REDUNDANT",
            "SURPLUS_LOW_FIT",
            "VETERAN_SALE",
        )

        for b in scan_buckets:
            for pid in out_cat.player_ids_by_bucket.get(b, tuple()):
                if pid in all_pids:
                    continue
                c = out_cat.players.get(pid)
                if c is None:
                    continue
                if receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                    continue
                if bool(getattr(c, "aggregation_solo_only", False)):
                    continue

                sal_d = int(round(float(c.salary_m) * 1_000_000.0))
                if sal_d < required_out_d:
                    continue

                overshoot_d = sal_d - required_out_d
                mkt = float(c.market.total)
                key = (overshoot_d, mkt, sal_d)
                if best_key is None or key < best_key:
                    best_key = key
                    best_pid = str(pid)

        if not best_pid:
            return False

        # failing_team leg에서 out_pid를 best_pid로 치환
        new_leg = []
        for a in cand.deal.legs.get(team, []) or []:
            if isinstance(a, PlayerAsset) and str(a.player_id) == out_pid:
                new_leg.append(PlayerAsset(kind="player", player_id=best_pid))
            else:
                new_leg.append(a)
        cand.deal.legs[team] = new_leg
        cand.tags.append("repair:second_apron_swap_out_up")
        return True

    # =========================================================
    # Case B: failing_team outgoing이 focal이면 -> incoming을 내리는 교체(상대팀 leg 교체)
    # =========================================================
    other_cat = catalog.outgoing_by_team.get(other)
    if other_cat is None:
        return False

    receiver_team = team
    max_in_d = outgoing_d - eps_d  # incoming <= outgoing(달러) 목표
    if max_in_d < 0:
        return False

    best_pid: Optional[str] = None
    best_key: Optional[Tuple[int, float]] = None  # (slack_d, market)

    scan_buckets2: Tuple[BucketId, ...] = (
        "FILLER_CHEAP",
        "EXPIRING",
        "FILLER_BAD_CONTRACT",
        "SURPLUS_REDUNDANT",
        "SURPLUS_LOW_FIT",
        "CONSOLIDATE",
        "VETERAN_SALE",
    )

    for b in scan_buckets2:
        for pid in other_cat.player_ids_by_bucket.get(b, tuple()):
            if pid in all_pids:
                continue
            c = other_cat.players.get(pid)
            if c is None:
                continue
            if receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                continue
            if bool(getattr(c, "aggregation_solo_only", False)):
                continue

            sal_d = int(round(float(c.salary_m) * 1_000_000.0))
            if sal_d > max_in_d:
                continue

            slack_d = max_in_d - sal_d  # 0에 가까울수록(outgoing에 가까울수록) 좋음
            mkt = float(c.market.total)
            key = (slack_d, mkt)
            if best_key is None or key < best_key:
                best_key = key
                best_pid = str(pid)

    if not best_pid:
        return False

    old_in_pid = str(incoming_players[0].player_id)

    # other leg에서 old_in_pid(=failing_team으로 가는 incoming player)를 best_pid로 치환
    new_leg = []
    for a in cand.deal.legs.get(other, []) or []:
        if isinstance(a, PlayerAsset) and str(a.player_id) == old_in_pid:
            recv = str(resolve_asset_receiver(cand.deal, other, a)).upper()
            if recv == team:
                new_leg.append(PlayerAsset(kind="player", player_id=best_pid))
            else:
                new_leg.append(a)
        else:
            new_leg.append(a)

    cand.deal.legs[other] = new_leg
    cand.tags.append("repair:second_apron_swap_in_down")
    return True


def _repair_second_apron_one_for_one(cand: DealCandidate, failing_team: str, catalog: TradeAssetCatalog) -> bool:
    """2nd apron one-for-one 위반: failing_team의 in/out player count를 1로 낮춘다.

    - market 기반으로 "가치가 낮아 보이는"(대개 filler) 플레이어를 우선 제거
    - 단, deal shape가 더 망가지면 prune(상위에서 재시도하게)
    """

    team = str(failing_team).upper()

    # outgoing trim (failing_team leg)
    out_assets = list(cand.deal.legs.get(team, []))
    out_players = [a for a in out_assets if isinstance(a, PlayerAsset)]
    if len(out_players) > 1:
        out_cat = catalog.outgoing_by_team.get(team)
        if out_cat is not None:
            def market(pid: str) -> float:
                c = out_cat.players.get(pid)
                return float(c.market.total) if c is not None else 0.0
            # keep the highest market (core-like), drop the rest
            keep = sorted(out_players, key=lambda a: market(a.player_id), reverse=True)[0]
        else:
            keep = out_players[0]

        cand.deal.legs[team] = [a for a in out_assets if not (isinstance(a, PlayerAsset) and a.player_id != keep.player_id)]
        cand.tags.append("repair:second_apron_trim_out")
        return True

    # incoming trim (other leg players are incoming to failing_team)
    other = [t for t in cand.deal.teams if str(t).upper() != team]
    if not other:
        return False
    other_team = str(other[0]).upper()

    other_assets = list(cand.deal.legs.get(other_team, []))
    other_players = [a for a in other_assets if isinstance(a, PlayerAsset)]
    if len(other_players) > 1:
        other_out = catalog.outgoing_by_team.get(other_team)
        if other_out is not None:
            def market(pid: str) -> float:
                c = other_out.players.get(pid)
                return float(c.market.total) if c is not None else 0.0
            # remove the lowest market (filler-like)
            pid_remove = sorted([p.player_id for p in other_players], key=market)[0]
        else:
            pid_remove = other_players[-1].player_id

        cand.deal.legs[other_team] = [a for a in other_assets if not (isinstance(a, PlayerAsset) and a.player_id == pid_remove)]
        cand.tags.append("repair:second_apron_trim_in")
        return True

    return False


def _repair_roster_limit(cand: DealCandidate, problem_team: str, catalog: TradeAssetCatalog, config: DealGeneratorConfig) -> bool:
    """ROSTER_LIMIT 수리."""

    other = [t for t in cand.deal.teams if str(t).upper() != problem_team]
    if not other:
        return False
    other_team = str(other[0]).upper()

    # 1) remove an incoming player to problem_team (player asset in other_team leg)
    other_assets = list(cand.deal.legs.get(other_team, []))
    player_ids = [a.player_id for a in other_assets if isinstance(a, PlayerAsset)]
    if len(player_ids) >= 2:
        other_out = catalog.outgoing_by_team.get(other_team)
        if other_out is not None:
            def market(pid: str) -> float:
                c = other_out.players.get(pid)
                return float(c.market.total) if c is not None else 0.0
            pid_remove = sorted(player_ids, key=market)[0]
        else:
            pid_remove = player_ids[-1]
        cand.deal.legs[other_team] = [a for a in other_assets if not (isinstance(a, PlayerAsset) and a.player_id == pid_remove)]
        cand.tags.append("repair:roster_remove_in")
        return True

    # 2) add outgoing from problem_team to reduce net incoming
    prob_out = catalog.outgoing_by_team.get(problem_team)
    if prob_out is None:
        return False

    if _count_players(cand.deal, problem_team) >= int(config.max_players_per_side):
        return False

    already = {a.player_id for a in cand.deal.legs.get(problem_team, []) if isinstance(a, PlayerAsset)}
    filler = _pick_lowest_market_player(
        prob_out,
        buckets=("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT"),
        banned_players=already,
    )
    if not filler:
        return False
    cand.deal.legs[problem_team].append(PlayerAsset(kind="player", player_id=filler))
    cand.tags.append("repair:roster_send_out")
    return True


def _repair_pick_rules(cand: DealCandidate, team_id: str, catalog: TradeAssetCatalog, config: DealGeneratorConfig, failure: RuleFailure) -> bool:
    """PickRulesRule 실패(stepien/pick_too_far 등) 수리."""

    if not team_id or team_id not in cand.deal.legs:
        return False

    reason = failure.reason or ""
    if reason == "pick_too_far" and failure.pick_id:
        pid = str(failure.pick_id)
        cand.deal.legs[team_id] = [a for a in cand.deal.legs[team_id] if not (isinstance(a, PickAsset) and a.pick_id == pid)]
        cand.tags.append("repair:pick_remove_far")
        return True

    out_cat = catalog.outgoing_by_team.get(team_id)
    if out_cat is None:
        return False

    picks_out = [a for a in cand.deal.legs[team_id] if isinstance(a, PickAsset)]
    if not picks_out:
        return False

    sensitive_set = set(out_cat.pick_ids_by_bucket.get("FIRST_SENSITIVE", tuple()))
    safe_set = set(out_cat.pick_ids_by_bucket.get("FIRST_SAFE", tuple()))

    pid_remove: Optional[str] = None
    for a in picks_out:
        if a.pick_id in sensitive_set:
            pid_remove = a.pick_id
            break
    if pid_remove is None:
        for a in picks_out:
            if a.pick_id in safe_set:
                pid_remove = a.pick_id
                break
    if pid_remove is None:
        pid_remove = picks_out[-1].pick_id

    cand.deal.legs[team_id] = [a for a in cand.deal.legs[team_id] if not (isinstance(a, PickAsset) and a.pick_id == pid_remove)]
    cand.tags.append("repair:stepien_remove_pick")

    # optional replacement: first -> second
    if pid_remove in safe_set or pid_remove in sensitive_set:
        if _count_picks(cand.deal, team_id) >= int(config.max_picks_per_side):
            return True
        replacement = _pick_best_pick_id(out_cat, bucket="SECOND", excluded=_current_pick_ids(cand.deal, team_id))
        if replacement:
            cand.deal.legs[team_id].append(out_cat.picks[replacement].as_asset())
            cand.tags.append("repair:stepien_replace_second")

    return True


# =============================================================================
# Sweetener loop
# =============================================================================


def maybe_apply_sweeteners(
    base: DealProposal,
    *,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    allow_locked_by_deal_id: Optional[str],
    banned_asset_keys: Set[str],
    rng: random.Random,
    stats: DealGeneratorStats,
) -> Tuple[DealProposal, int, int]:
    """"조금 부족"한 쪽이 있을 때 pick/swap sweetener를 1~2개만 추가해서 재시도.

    - base.deal은 이미 validate 통과 상태여야 한다.
    - 추가한 딜도 validate + evaluate를 통과해야 한다.

    Returns: (best_prop, extra_validations, extra_evaluations)
    """

    if not config.sweetener_enabled:
        return base, 0, 0

    # 예산 가드
    if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
        return base, 0, 0

    mb = float(base.buyer_eval.net_surplus) - float(base.buyer_decision.required_surplus)
    ms = float(base.seller_eval.net_surplus) - float(base.seller_decision.required_surplus)

    # 어느 쪽이 부족한가?
    giver: Optional[str] = None
    receiver: Optional[str] = None
    deficit = 0.0

    if base.seller_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and ms < 0.0 and abs(ms) <= float(config.sweetener_max_deficit):
        giver, receiver, deficit = base.buyer_id, base.seller_id, abs(ms)
    elif base.buyer_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and mb < 0.0 and abs(mb) <= float(config.sweetener_max_deficit):
        giver, receiver, deficit = base.seller_id, base.buyer_id, abs(mb)
    else:
        return base, 0, 0

    if not giver or not receiver:
        return base, 0, 0

    out_cat = catalog.outgoing_by_team.get(str(giver).upper())
    if out_cat is None:
        return base, 0, 0

    local_sweetener_bans: Set[str] = set()

    best = base
    extra_v = 0
    extra_e = 0

    deal = _clone_deal(base.deal)
    added_count = 0

    # score/margin 기반 early stop
    last_best_score = float(best.score)

    # deterministic shuffle inside same bucket selection
    bucket_order = list(config.sweetener_try_buckets)

    for bucket in bucket_order:
        if added_count >= int(config.sweetener_max_additions):
            break
        if stats.validations + extra_v >= budget.max_validations or stats.evaluations + extra_e >= budget.max_evaluations:
            break

        added = _try_add_one_sweetener(
            deal,
            giver_team=str(giver).upper(),
            receiver_team=str(receiver).upper(),
            out_cat=out_cat,
            catalog=catalog,
            config=config,
            target_value=float(deficit),
            bucket=str(bucket),
            banned_asset_keys=(set(banned_asset_keys) | set(local_sweetener_bans)),
            rng=rng,
        )
        if not added:
            continue

        added_count += 1
        stats.sweeteners_added += 1
        stats.sweetener_attempts += 1

        # validate
        attempted_key: Optional[str] = None
        if deal.legs.get(str(giver).upper()):
            attempted_key = asset_key(deal.legs[str(giver).upper()][-1])

        try:
            tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
            extra_v += 1
        except TradeError as err:
            # rollback this sweetener (remove last asset from giver leg)
            extra_v += 1
            if attempted_key:
                failure = parse_trade_error(err)
                # 자산 자체가 근본적으로 금지/미소유/잠김/중복인 케이스는 global ban이 효과적
                if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
                    banned_asset_keys.add(attempted_key)
                    if failure.asset_key:
                        banned_asset_keys.add(failure.asset_key)
                else:
                    local_sweetener_bans.add(attempted_key)
                    if failure.asset_key:
                        local_sweetener_bans.add(failure.asset_key)
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue
        except Exception:
            # 예상치 못한 예외는 local ban(동일 sweetener 재시도 방지)만 적용
            extra_v += 1
            if attempted_key:
                local_sweetener_bans.add(attempted_key)
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue

        # evaluate
        # (D) sweetener는 "누가 누구에게" 추가했는지와 round를 남겨두면
        # 후속 counter/협상 로직 구현 시 매우 유용하다.
        round_no = int(added_count)
        prop, used = evaluate_and_score(
            deal,
            buyer_id=base.buyer_id,
            seller_id=base.seller_id,
            tick_ctx=tick_ctx,
            config=config,
            tags=tuple(best.tags)
            + (
                f"sweetener:{bucket}",
                f"sweetener_from:{str(giver).upper()}",
                f"sweetener_to:{str(receiver).upper()}",
                f"sweetener_round:{round_no}",
            ),
            opponent_repeat_count=0,
            stats=stats,
        )
        extra_e += used
        if prop is None:
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue

        if _should_discard_prop(prop, config):
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue

        # 개선이 거의 없으면 중단(낭비 방지)
        if float(prop.score) < last_best_score + float(config.sweetener_min_improvement):
            # 계속 추가해도 의미 없을 확률이 높아 중단
            best = max(best, prop, key=lambda p: p.score)
            break

        if float(prop.score) > float(best.score):
            best = prop
            last_best_score = float(best.score)

        # 둘 다 accept면 즉시 종료
        if best.buyer_decision.verdict == DealVerdict.ACCEPT and best.seller_decision.verdict == DealVerdict.ACCEPT:
            break

    return best, extra_v, extra_e


def _try_add_one_sweetener(
    deal: Deal,
    *,
    giver_team: str,
    receiver_team: str,
    out_cat: TeamOutgoingCatalog,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    target_value: float,
    bucket: str,
    banned_asset_keys: Set[str],
    rng: random.Random,
) -> bool:
    """deal에 sweetener 1개를 추가(성공 시 deal mutate).

    bucket:
      - SECOND / FIRST_SAFE / FIRST_SENSITIVE: PickAsset
      - SWAP: SwapAsset
    """

    giver_team = str(giver_team).upper()

    # max assets / picks guard
    if len(deal.legs.get(giver_team, [])) >= int(config.max_assets_per_side):
        return False

    if bucket == "SWAP":
        if _count_swaps(deal, giver_team) >= 1:
            return False
        cands = list(out_cat.swap_ids or ())
        rng.shuffle(cands)
        for sid in cands:
            s = out_cat.swaps.get(sid)
            if s is None:
                continue
            a = s.as_asset()
            if asset_key(a) in banned_asset_keys:
                continue
            if _asset_in_deal(deal, a):
                continue
            deal.legs[giver_team].append(a)
            return True
        return False

    # pick buckets
    if _count_picks(deal, giver_team) >= int(config.max_picks_per_side):
        return False

    pick_bucket: Optional[PickBucketId] = None
    if bucket == "SECOND":
        if _count_seconds(deal, giver_team, catalog=catalog) >= int(config.max_seconds_per_side):
            return False
        pick_bucket = "SECOND"
    elif bucket == "FIRST_SAFE":
        pick_bucket = "FIRST_SAFE"
    elif bucket == "FIRST_SENSITIVE":
        pick_bucket = "FIRST_SENSITIVE"
    else:
        return False

    excluded = _current_pick_ids(deal, giver_team)

    pid = _pick_pick_id_matching_value(out_cat, pick_bucket, excluded=excluded, target_value=target_value)
    if not pid:
        return False

    # Stepien check for 1st
    if pick_bucket in ("FIRST_SAFE", "FIRST_SENSITIVE"):
        out_ids, in_ids = _team_pick_flow(deal, giver_team)
        out_ids = set(out_ids | {pid})
        if not catalog.stepien.is_compliant_after(team_id=giver_team, outgoing_pick_ids=out_ids, incoming_pick_ids=set(in_ids)):
            return False

    a = out_cat.picks[pid].as_asset()
    if asset_key(a) in banned_asset_keys:
        return False
    if _asset_in_deal(deal, a):
        return False

    deal.legs[giver_team].append(a)
    return True


def _rollback_last_asset_from_leg(deal: Deal, team_id: str) -> None:
    tid = str(team_id).upper()
    leg = deal.legs.get(tid)
    if not leg:
        return
    try:
        leg.pop()
    except Exception:
        return


def _asset_in_deal(deal: Deal, asset: Asset) -> bool:
    k = asset_key(asset)
    for assets in deal.legs.values():
        for a in assets:
            if asset_key(a) == k:
                return True
    return False


# =============================================================================
# Evaluation + scoring
# =============================================================================


def _proposal_from_cached_eval(
    deal: Deal,
    *,
    buyer_id: str,
    seller_id: str,
    buyer_decision: DealDecision,
    seller_decision: DealDecision,
    buyer_eval: TeamDealEvaluation,
    seller_eval: TeamDealEvaluation,
    config: DealGeneratorConfig,
    tags: Tuple[str, ...],
    opponent_repeat_count: int,
) -> DealProposal:
    """cached eval(=decision/eval)로부터 DealProposal을 구성한다.

    NOTE
    - score는 opponent_repeat_count 등 런타임 요소가 있어 캐시하지 않는다.
    - evaluate_and_score()와 동일한 shape tag 정책을 유지한다.
    """

    score = score_deal(
        deal,
        buyer_decision=buyer_decision,
        seller_decision=seller_decision,
        buyer_eval=buyer_eval,
        seller_eval=seller_eval,
        config=config,
        opponent_repeat_count=opponent_repeat_count,
    )

    n_assets = sum(len(v) for v in deal.legs.values())
    n_players = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PlayerAsset))
    n_picks = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PickAsset))
    n_swaps = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, SwapAsset))
    shape_tags = (
        f"shape:assets:{n_assets}",
        f"shape:players:{n_players}",
        f"shape:picks:{n_picks}",
        f"shape:swaps:{n_swaps}",
    )

    tags_out: List[str] = list(tags)
    for t in shape_tags:
        if t not in tags_out:
            tags_out.append(t)

    return DealProposal(
        deal=deal,
        buyer_id=str(buyer_id).upper(),
        seller_id=str(seller_id).upper(),
        buyer_decision=buyer_decision,
        seller_decision=seller_decision,
        buyer_eval=buyer_eval,
        seller_eval=seller_eval,
        score=float(score),
        tags=tuple(tags_out),
    )


def evaluate_and_score(
    deal: Deal,
    *,
    buyer_id: str,
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    config: DealGeneratorConfig,
    tags: Tuple[str, ...],
    opponent_repeat_count: int,
    stats: Optional[DealGeneratorStats] = None,
) -> Tuple[Optional[DealProposal], int]:
    """양팀 evaluate_deal_for_team 호출 + score 산정."""

    try:
        buyer_decision, buyer_eval = evaluate_deal_for_team(
            deal,
            buyer_id,
            tick_ctx=tick_ctx,
            include_breakdown=False,
            validate=False,
        )
        seller_decision, seller_eval = evaluate_deal_for_team(
            deal,
            seller_id,
            tick_ctx=tick_ctx,
            include_breakdown=False,
            validate=False,
        )
    except TradeError:
        if stats is not None:
            stats.bump_failure("eval_trade_error")
        return None, 1
    except Exception:
        if stats is not None:
            stats.bump_failure("unexpected_exception_eval")
        return None, 1

    score = score_deal(
        deal,
        buyer_decision=buyer_decision,
        seller_decision=seller_decision,
        buyer_eval=buyer_eval,
        seller_eval=seller_eval,
        config=config,
        opponent_repeat_count=opponent_repeat_count,
    )

    # (D) deal 형태를 태그로 남겨두면 후속 분석/디버깅(특히 spam/중복/비현실 필터)에 유용하다.
    n_assets = sum(len(v) for v in deal.legs.values())
    n_players = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PlayerAsset))
    n_picks = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PickAsset))
    n_swaps = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, SwapAsset))
    shape_tags = (
        f"shape:assets:{n_assets}",
        f"shape:players:{n_players}",
        f"shape:picks:{n_picks}",
        f"shape:swaps:{n_swaps}",
    )

    # 중복 태그 방지(순서 유지)
    tags_out: List[str] = list(tags)
    for t in shape_tags:
        if t not in tags_out:
            tags_out.append(t)

    prop = DealProposal(
        deal=deal,
        buyer_id=str(buyer_id).upper(),
        seller_id=str(seller_id).upper(),
        buyer_decision=buyer_decision,
        seller_decision=seller_decision,
        buyer_eval=buyer_eval,
        seller_eval=seller_eval,
        score=float(score),
        tags=tuple(tags_out),
    )
    return prop, 2


def score_deal(
    deal: Deal,
    *,
    buyer_decision: DealDecision,
    seller_decision: DealDecision,
    buyer_eval: TeamDealEvaluation,
    seller_eval: TeamDealEvaluation,
    config: DealGeneratorConfig,
    opponent_repeat_count: int,
) -> float:
    """게임용 점수: (양팀) ACCEPT에 가까울수록, 단순할수록, 시장 다양할수록 높은 점수.

    원칙
    - 최우선: 양팀이 ACCEPT 가능한 딜
    - 차선: 한쪽이 COUNTER(조금 부족)인 딜(스윗너로 복구 가능)
    - 강한 제외: REJECT가 확실하거나 한쪽이 큰 손해(유저 체감상 비현실)
    """

    mb = float(buyer_eval.net_surplus) - float(buyer_decision.required_surplus)
    ms = float(seller_eval.net_surplus) - float(seller_decision.required_surplus)

    def sigmoid(x: float, scale: float) -> float:
        s = float(scale) if float(scale) != 0 else 1.0
        # clamp로 overflow 방지
        z = max(-60.0, min(60.0, x / s))
        return 1.0 / (1.0 + math.exp(-z))

    accept_score = sigmoid(mb, config.score_sigmoid_scale) + sigmoid(ms, config.score_sigmoid_scale)

    # complexity penalty
    n_assets = sum(len(v) for v in deal.legs.values())
    n_players = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PlayerAsset))
    complexity_penalty = (
        float(config.penalty_per_asset) * max(0, n_assets - 2)
        + float(config.penalty_per_player) * max(0, n_players - 2)
    )

    # deficit penalty (both sides)
    deficit_penalty = (
        float(config.penalty_overpay_weight) * max(0.0, -mb)
        + float(getattr(config, "penalty_opponent_overpay_weight", 0.85)) * max(0.0, -ms)
    )

    # 시장 다양화(동일 파트너 반복 페널티)
    repeat_penalty = 0.0
    if int(opponent_repeat_count) > 0:
        repeat_penalty += float(getattr(config, "opponent_repeat_penalty", 0.0))
        if int(opponent_repeat_count) > 1:
            repeat_penalty += float(getattr(config, "opponent_multi_repeat_penalty", 0.0)) * float(int(opponent_repeat_count) - 1)

    # verdict bonus/penalty
    bonus = 0.0
    if buyer_decision.verdict == DealVerdict.ACCEPT and seller_decision.verdict == DealVerdict.ACCEPT:
        bonus += 0.35
    elif buyer_decision.verdict == DealVerdict.ACCEPT and seller_decision.verdict == DealVerdict.COUNTER:
        bonus += 0.15
    elif seller_decision.verdict == DealVerdict.ACCEPT and buyer_decision.verdict == DealVerdict.COUNTER:
        bonus += 0.15

    reject_penalty = 0.0
    base = float(getattr(config, "reject_penalty_base", 0.35))
    scale = float(getattr(config, "reject_penalty_scale", 0.06))
    if buyer_decision.verdict == DealVerdict.REJECT:
        reject_penalty += base + scale * max(0.0, -mb)
    if seller_decision.verdict == DealVerdict.REJECT:
        reject_penalty += base + scale * max(0.0, -ms)

    return float(accept_score + bonus - complexity_penalty - deficit_penalty - reject_penalty - repeat_penalty)


# =============================================================================
# Dedupe / misc
# =============================================================================


def dedupe_hash(deal: Deal) -> str:
    """Deal identity hash for dedupe.

    IMPORTANT:
    - MUST ignore deal.meta (tags/debug fields) so the same transaction (teams+legs)
      does not survive as duplicates with only meta differences.
    """
    canon = canonicalize_deal(deal)
    payload = serialize_deal(canon)
    # Ignore meta completely for dedupe (A)
    payload.pop("meta", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _clone_deal(deal: Deal) -> Deal:
    return Deal(
        teams=list(deal.teams),
        legs={tid: list(assets) for tid, assets in deal.legs.items()},
    )


def _shape_ok(deal: Deal, *, config: DealGeneratorConfig, catalog: Optional[TradeAssetCatalog] = None) -> bool:
    for assets in deal.legs.values():
        if len(assets) > int(config.max_assets_per_side):
            return False

    n_players = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PlayerAsset))
    if n_players > int(config.max_players_moved_total):
        return False

    for tid in deal.teams:
        tid_u = str(tid).upper()
        if _count_players(deal, tid_u) > int(config.max_players_per_side):
            return False
        if _count_picks(deal, tid_u) > int(config.max_picks_per_side):
            return False
        if _count_seconds(deal, tid_u, catalog=catalog) > int(config.max_seconds_per_side):
            return False

    return True


def _count_players(deal: Deal, team_id: str) -> int:
    return sum(1 for a in deal.legs.get(str(team_id).upper(), []) if isinstance(a, PlayerAsset))


def _count_picks(deal: Deal, team_id: str) -> int:
    return sum(1 for a in deal.legs.get(str(team_id).upper(), []) if isinstance(a, PickAsset))


def _count_seconds(deal: Deal, team_id: str, *, catalog: Optional[TradeAssetCatalog] = None) -> int:
    """Deal 내 2라운드 픽 수(best-effort).

    SSOT는 PickSnapshot.round 이지만, deal에는 pick_id만 있으므로:
    - catalog(outgoing_by_team)가 있으면 해당 팀의 SECOND bucket / PickSnapshot.round를 우선 사용
    - 없으면 id 문자열 휴리스틱으로 fallback
    """
    tid = str(team_id).upper()
    assets = deal.legs.get(tid, []) or []

    if catalog is None:
        return sum(1 for a in assets if isinstance(a, PickAsset) and _is_second_round_pick_id(a.pick_id))

    out_cat = catalog.outgoing_by_team.get(tid)
    if out_cat is None:
        return sum(1 for a in assets if isinstance(a, PickAsset) and _is_second_round_pick_id(a.pick_id))

    seconds_set = set(out_cat.pick_ids_by_bucket.get("SECOND", tuple()))
    cnt = 0
    for a in assets:
        if not isinstance(a, PickAsset):
            continue
        pid = str(a.pick_id)
        if pid in seconds_set:
            cnt += 1
            continue
        c = out_cat.picks.get(pid)
        if c is not None and int(getattr(c.snap, "round", 0) or 0) == 2:
            cnt += 1
            continue
        if _is_second_round_pick_id(pid):
            cnt += 1
    return cnt


def _count_swaps(deal: Deal, team_id: str) -> int:
    tid = str(team_id).upper()
    return sum(1 for a in deal.legs.get(tid, []) if isinstance(a, SwapAsset))


def _is_second_round_pick_id(pick_id: str) -> bool:
    # SSOT는 PickSnapshot.round 이지만, 여기선 id 기반으로는 확정이 어렵다.
    # 대신 catalog를 통해 생성된 pick은 대체로 id에 "R2" 등 표기가 있을 수 있으나 보장되지 않는다.
    # 안전하게: Deal 내 PickAsset만으로 판별 불가 -> seconds cap은 generator 단계에서
    # pick bucket(SECOND)로 추가할 때만 증가시키도록 쓰는 편이 이상적.
    # 여기서는 best-effort: 대부분 데이터셋에서 "R2"/"2ND" 포함.
    s = str(pick_id).upper()
    return ("R2" in s) or ("2ND" in s) or ("ROUND2" in s)


def _current_pick_ids(deal: Deal, team_id: str) -> Set[str]:
    tid = str(team_id).upper()
    return {a.pick_id for a in deal.legs.get(tid, []) if isinstance(a, PickAsset)}


def _team_pick_flow(deal: Deal, team_id: str) -> Tuple[Set[str], Set[str]]:
    """team_id 기준 (outgoing_pick_ids, incoming_pick_ids)."""

    tid = str(team_id).upper()
    out_ids: Set[str] = set()
    in_ids: Set[str] = set()

    for from_team, assets in deal.legs.items():
        for a in assets:
            if not isinstance(a, PickAsset):
                continue
            receiver = resolve_asset_receiver(deal, str(from_team), a)
            if str(from_team).upper() == tid:
                out_ids.add(str(a.pick_id))
            if str(receiver).upper() == tid:
                in_ids.add(str(a.pick_id))

    return out_ids, in_ids


# =============================================================================
# Asset picking helpers (bucket-aware)
# =============================================================================


def _pick_bucket_player(
    out: TeamOutgoingCatalog,
    *,
    bucket: BucketId,
    receiver_team_id: Optional[str] = None,
    banned_players: Optional[Set[str]] = None,
    must_be_aggregation_friendly: bool = True,
) -> Optional[str]:
    receiver = str(receiver_team_id).upper() if receiver_team_id else None
    for pid in list(out.player_ids_by_bucket.get(bucket, tuple())):
        if banned_players and pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue
        if receiver and receiver in set(getattr(c, "return_ban_teams", None) or ()):
            continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue
        return str(pid)
    return None


def _pick_lowest_market_player(
    out: TeamOutgoingCatalog,
    *,
    buckets: Tuple[BucketId, ...],
    banned_players: Set[str],
) -> Optional[str]:
    """여러 버킷에서 market.total이 가장 낮은 플레이어를 선택(필러용)."""

    best_pid: Optional[str] = None
    best_mkt = float("inf")
    for b in buckets:
        for pid in out.player_ids_by_bucket.get(b, tuple()):
            if pid in banned_players:
                continue
            c = out.players.get(pid)
            m = float(c.market.total) if c is not None else 0.0
            if m < best_mkt:
                best_mkt = m
                best_pid = pid
    return best_pid


def _pick_youngish_player(
    out: TeamOutgoingCatalog,
    *,
    banned_players: Set[str],
    receiver_team_id: Optional[str] = None,
    must_be_aggregation_friendly: bool = True,
) -> Optional[str]:
    """버킷에 YOUNG가 없으므로 age 기반 휴리스틱."""

    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    cands: List[PlayerTradeCandidate] = []
    for b in ("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_CHEAP", "CONSOLIDATE"):
        for pid in out.player_ids_by_bucket.get(b, tuple()):
            if pid in banned_players:
                continue
            c = out.players.get(pid)
            if c is None:
                continue
            if receiver and receiver in set(getattr(c, "return_ban_teams", None) or ()):
                continue
            if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
                continue
            age = c.snap.age
            if age is not None and float(age) <= 24.5:
                cands.append(c)

    if not cands:
        return None

    cands.sort(key=lambda c: (-float(c.market.total), float(c.salary_m), c.player_id))
    return cands[0].player_id


def _pick_filler_player_for_salary(
    out: TeamOutgoingCatalog,
    *,
    receiver_team_id: Optional[str],
    target_salary_m: float,
    banned_players: Set[str],
    must_be_aggregation_friendly: bool = True,
) -> Optional[str]:
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    ids: List[str] = []
    for b in ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT"):
        ids.extend(list(out.player_ids_by_bucket.get(b, tuple())))

    best: Optional[str] = None
    best_gap = 1e9
    for pid in ids:
        if pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue
        if receiver and receiver in set(getattr(c, "return_ban_teams", None) or ()):
            continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue
        gap = abs(float(c.salary_m) - float(target_salary_m))
        if gap < best_gap:
            best_gap = gap
            best = pid

    return best


def _add_pick_package(
    deal: Deal,
    *,
    from_team: str,
    out_cat: TeamOutgoingCatalog,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    rng: random.Random,
    prefer: Tuple[str, ...],
    max_picks: int,
    banned_asset_keys: Optional[Set[str]] = None,
) -> None:
    """pick bucket 우선순위 기반으로 pick을 추가.

    catalog는 이미 lock/max_year를 필터했지만 Stepien은 조합에 따라 달라질 수 있으니
    1st 추가 시점에만 StepienHelper로 체크한다.
    """

    tid = str(from_team).upper()
    if tid not in deal.legs:
        return

    picks_added = 0
    outgoing_pick_ids = _current_pick_ids(deal, tid)

    def iter_bucket(name: str) -> Iterable[str]:
        if name == "FIRST_SAFE":
            return out_cat.pick_ids_by_bucket.get("FIRST_SAFE", tuple())
        if name == "FIRST_SENSITIVE":
            return out_cat.pick_ids_by_bucket.get("FIRST_SENSITIVE", tuple())
        if name == "SECOND":
            return out_cat.pick_ids_by_bucket.get("SECOND", tuple())
        return tuple()

    for bucket in prefer:
        if picks_added >= int(max_picks):
            break
        if bucket == "SWAP":
            continue

        for pid in iter_bucket(bucket):
            if picks_added >= int(max_picks):
                break
            if _count_picks(deal, tid) >= int(config.max_picks_per_side):
                break
            if bucket == "SECOND" and _count_seconds(deal, tid, catalog=catalog) >= int(config.max_seconds_per_side):
                break
            pid_s = str(pid)

            # (C) ownership/lock 등으로 금지된 pick은 스켈레톤 단계부터 제외
            if banned_asset_keys is not None and f"pick:{pid_s}" in banned_asset_keys:
                continue

            if pid_s in outgoing_pick_ids:
                continue

            # stepien check for 1st(s)
            if bucket.startswith("FIRST"):
                out_ids, in_ids = _team_pick_flow(deal, tid)
                if not catalog.stepien.is_compliant_after(team_id=tid, outgoing_pick_ids=set(out_ids | {pid_s}), incoming_pick_ids=set(in_ids)):
                    continue

            try:
                deal.legs[tid].append(out_cat.picks[pid_s].as_asset())
            except Exception:
                deal.legs[tid].append(PickAsset(kind="pick", pick_id=pid_s))
            outgoing_pick_ids.add(pid_s)
            picks_added += 1
            break


def _pick_best_pick_id(out_cat: TeamOutgoingCatalog, *, bucket: PickBucketId, excluded: Set[str]) -> Optional[str]:
    for pid in out_cat.pick_ids_by_bucket.get(bucket, tuple()):
        if pid in excluded:
            continue
        return str(pid)
    return None


def _pick_pick_id_matching_value(
    out_cat: TeamOutgoingCatalog,
    bucket: PickBucketId,
    *,
    excluded: Set[str],
    target_value: float,
) -> Optional[str]:
    """bucket 내 pick 중 market.total이 target_value와 가장 가까운 것을 선택."""

    cands = [str(pid) for pid in out_cat.pick_ids_by_bucket.get(bucket, tuple()) if str(pid) not in excluded]
    if not cands:
        return None

    # picks dict에는 market 포함
    def key(pid: str) -> float:
        p = out_cat.picks.get(pid)
        mv = float(p.market.total) if p is not None else 0.0
        return abs(mv - float(target_value))

    cands.sort(key=key)
    return cands[0]


# =============================================================================
# Dedupe helpers
# =============================================================================


# =============================================================================
# End
# =============================================================================
