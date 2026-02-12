from __future__ import annotations

import math
from typing import List, Optional, Tuple

from ...errors import TradeError
from ...models import Deal, PlayerAsset, PickAsset, SwapAsset
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, DealVerdict, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext

from .types import DealGeneratorConfig, DealProposal, DealGeneratorStats

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

