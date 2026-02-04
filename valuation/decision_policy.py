from __future__ import annotations

"""
decision_policy.py

Accept/Reject/Counter policy based on:
- TeamDealEvaluation (numbers already include package effects)
- DecisionContext.knobs (min_surplus_required, overpay_budget, counter_rate, etc.)

This module MUST NOT:
- re-run team_situation evaluation
- re-check hard constraints (salary matching, Stepien, apron rules, etc.)

It is a *thin* decision layer: convert a computed net value into a verdict.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
import math
import random

from decision_context import DecisionContext

from .types import (
    DealDecision,
    DealVerdict,
    DecisionReason,
    CounterProposal,
    TeamDealEvaluation,
    TeamValuation,
    ValueComponents,
)


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _clamp(x: float, lo: float, hi: float) -> float:
    xf = _safe_float(x, lo)
    if xf < lo:
        return float(lo)
    if xf > hi:
        return float(hi)
    return float(xf)


def _sigmoid(x: float) -> float:
    # stable sigmoid for confidence mapping
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _summarize_fit_flags(side_incoming: Sequence[TeamValuation], limit: int = 4) -> Dict[str, Any]:
    """Surface already-computed fit outcomes without re-evaluating."""
    failed: List[Dict[str, Any]] = []
    for tv in side_incoming:
        if tv.fit is None:
            continue
        if not bool(tv.fit.passed):
            failed.append(
                {
                    "asset_key": tv.asset_key,
                    "ref_id": tv.ref_id,
                    "fit_score": float(tv.fit.fit_score),
                    "threshold": float(tv.fit.threshold),
                }
            )
    failed_sorted = sorted(failed, key=lambda d: d["fit_score"])
    return {
        "failed_count": len(failed_sorted),
        "failed_samples": failed_sorted[: max(0, int(limit))],
    }


def _extract_package_delta_total(e: TeamDealEvaluation) -> float:
    # deal_evaluator stores these in meta by design
    if isinstance(e.meta, dict):
        if "package_delta_total" in e.meta:
            return _safe_float(e.meta.get("package_delta_total"), 0.0)
        # alternative nesting
        pkg = e.meta.get("package_delta")
        if isinstance(pkg, dict):
            return _safe_float(pkg.get("total"), 0.0)
    return 0.0


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionPolicyConfig:
    eps: float = 1e-9

    # If outgoing_total is ~0 (e.g., receive something for free), use this as scale
    min_outgoing_scale: float = 6.0

    # How wide is the "counter corridor" around the acceptance boundary
    # corridor = required_surplus ± corridor_ratio*outgoing_total
    counter_corridor_ratio: float = 0.06

    # If net is negative but within this fraction of overpay_allowed,
    # we may still COUNTER rather than hard REJECT (if counter_rate favors it).
    counter_overpay_fraction: float = 0.65

    # Confidence mapping: larger => sharper transitions
    confidence_slope: float = 3.5

    # Surface reasons: how many items to show
    max_reasons: int = 6

    # Whether to use stochastic counter behavior (optional).
    # If false, counter_rate is interpreted deterministically.
    stochastic_counter: bool = False


# -----------------------------------------------------------------------------
# Policy engine
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class DecisionPolicy:
    config: DecisionPolicyConfig = field(default_factory=DecisionPolicyConfig)

    def decide(
        self,
        *,
        evaluation: TeamDealEvaluation,
        ctx: DecisionContext,
        rng: Optional[random.Random] = None,
        allow_counter: bool = True,
    ) -> DealDecision:
        """
        Compute DealDecision from TeamDealEvaluation + DecisionContext.

        evaluation.net_surplus already includes package effects.
        """
        cfg = self.config
        rng = rng or random.Random()

        knobs = ctx.knobs
        outgoing = _safe_float(evaluation.outgoing_total, 0.0)
        incoming = _safe_float(evaluation.incoming_total, 0.0)
        net = _safe_float(evaluation.net_surplus, incoming - outgoing)

        # scale baseline (avoid weirdness when outgoing ~ 0)
        scale = max(outgoing, cfg.min_outgoing_scale)

        # Required surplus and overpay allowance are both proportional to outgoing scale
        min_surplus_ratio = max(0.0, _safe_float(getattr(knobs, "min_surplus_required", 0.0), 0.0))
        overpay_ratio = max(0.0, _safe_float(getattr(knobs, "overpay_budget", 0.0), 0.0))
        counter_rate = _clamp(_safe_float(getattr(knobs, "counter_rate", 0.0), 0.0), 0.0, 1.0)

        required_surplus = float(min_surplus_ratio * scale)
        overpay_allowed = float(overpay_ratio * scale)

        # acceptance windows
        accept_threshold = required_surplus
        overpay_floor = -overpay_allowed  # allow slight negative within budget

        # corridor for counter decisions (around accept boundary)
        corridor = cfg.counter_corridor_ratio * scale

        # ---- Decide verdict (no hard rule checks)
        verdict: DealVerdict
        reasons: List[DecisionReason] = []

        # baseline reasons: net vs thresholds
        reasons.append(
            DecisionReason(
                code="NET_SURPLUS",
                message=f"net_surplus={net:.3f} (incoming={incoming:.3f}, outgoing={outgoing:.3f})",
                impact=net,
                meta={"incoming_total": incoming, "outgoing_total": outgoing},
            )
        )
        reasons.append(
            DecisionReason(
                code="THRESHOLDS",
                message=f"required_surplus={accept_threshold:.3f}, overpay_floor={overpay_floor:.3f}",
                impact=None,
                meta={
                    "min_surplus_required_ratio": min_surplus_ratio,
                    "overpay_budget_ratio": overpay_ratio,
                    "scale_outgoing": scale,
                },
            )
        )

        # highlight package delta if significant
        pkg_delta = _extract_package_delta_total(evaluation)
        if abs(pkg_delta) > cfg.eps:
            reasons.append(
                DecisionReason(
                    code="PACKAGE_EFFECTS",
                    message=f"package_effects_delta={pkg_delta:.3f}",
                    impact=pkg_delta,
                    meta={"package_delta_total": pkg_delta},
                )
            )

        # Surface fit fails (already computed) to explain "why net isn't as high"
        fit_info = _summarize_fit_flags(evaluation.side.incoming)
        if fit_info["failed_count"] > 0:
            reasons.append(
                DecisionReason(
                    code="FIT_FAILS",
                    message=f"incoming_fit_failed={fit_info['failed_count']}",
                    impact=None,
                    meta=fit_info,
                )
            )

        # Decision region:
        # 1) Strong accept: net >= required
        if net >= accept_threshold:
            verdict = DealVerdict.ACCEPT
            reasons.append(
                DecisionReason(
                    code="MEETS_REQUIRED_SURPLUS",
                    message="net surplus meets required threshold",
                    impact=net - accept_threshold,
                )
            )
        else:
            # 2) Not meeting required:
            # If within counter corridor / near acceptance, prefer COUNTER depending on counter_rate.
            # else if within overpay allowance, may still accept (win-now aggressive encoded into knobs)
            in_corridor = (accept_threshold - corridor) <= net < accept_threshold
            within_overpay = net >= overpay_floor

            if allow_counter and (in_corridor or (within_overpay and net < 0 and abs(net) <= cfg.counter_overpay_fraction * overpay_allowed)):
                # determine counter vs reject/accept in gray zone
                if self._choose_counter(counter_rate=counter_rate, rng=rng):
                    verdict = DealVerdict.COUNTER
                    reasons.append(
                        DecisionReason(
                            code="COUNTER_IN_GRAY_ZONE",
                            message="near threshold / within overpay window -> counter tendency applied",
                            impact=accept_threshold - net,
                            meta={"counter_rate": counter_rate, "corridor": corridor},
                        )
                    )
                else:
                    # If not countering, decide accept (if within overpay) else reject
                    if within_overpay:
                        verdict = DealVerdict.ACCEPT
                        reasons.append(
                            DecisionReason(
                                code="ACCEPT_WITHIN_OVERPAY",
                                message="below required but within overpay budget",
                                impact=net - accept_threshold,
                                meta={"overpay_allowed": overpay_allowed},
                            )
                        )
                    else:
                        verdict = DealVerdict.REJECT
                        reasons.append(
                            DecisionReason(
                                code="INSUFFICIENT_SURPLUS",
                                message="below required and outside overpay budget",
                                impact=net - accept_threshold,
                            )
                        )
            else:
                # 3) Clear fail: accept only if within overpay, else reject
                if within_overpay:
                    verdict = DealVerdict.ACCEPT
                    reasons.append(
                        DecisionReason(
                            code="ACCEPT_WITHIN_OVERPAY",
                            message="below required but within overpay budget",
                            impact=net - accept_threshold,
                            meta={"overpay_allowed": overpay_allowed},
                        )
                    )
                else:
                    verdict = DealVerdict.REJECT
                    reasons.append(
                        DecisionReason(
                            code="INSUFFICIENT_SURPLUS",
                            message="below required and outside overpay budget",
                            impact=net - accept_threshold,
                        )
                    )

        # Confidence: map distance to nearest decision boundary
        confidence = self._compute_confidence(
            net=net,
            accept_threshold=accept_threshold,
            overpay_floor=overpay_floor,
            scale=scale,
            verdict=verdict,
        )

        # Optional counter proposal placeholder (counter_builder not implemented yet)
        counter: Optional[CounterProposal] = None
        if verdict == DealVerdict.COUNTER:
            counter = CounterProposal(
                deal=None,
                reasons=tuple(
                    [
                        DecisionReason(
                            code="COUNTER_NOT_IMPLEMENTED",
                            message="counter proposal builder not implemented yet",
                        )
                    ]
                ),
                meta={"target_required_surplus": accept_threshold, "current_net": net},
            )

        # Trim reasons
        if len(reasons) > cfg.max_reasons:
            reasons = reasons[: cfg.max_reasons]

        return DealDecision(
            verdict=verdict,
            required_surplus=float(required_surplus),
            overpay_allowed=float(overpay_allowed),
            confidence=float(confidence),
            reasons=tuple(reasons),
            counter=counter,
            meta={
                "team_id": evaluation.team_id,
                "surplus_ratio": float(evaluation.surplus_ratio),
                "counter_rate": float(counter_rate),
                "accept_threshold": float(accept_threshold),
                "overpay_floor": float(overpay_floor),
                "corridor": float(corridor),
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _choose_counter(self, *, counter_rate: float, rng: random.Random) -> bool:
        """Counter decision: deterministic or stochastic."""
        cfg = self.config
        r = _clamp(_safe_float(counter_rate, 0.0), 0.0, 1.0)
        if r <= cfg.eps:
            return False
        if cfg.stochastic_counter:
            return rng.random() < r
        # deterministic: treat counter_rate as aggressiveness threshold
        # >0.5 => generally counter in gray zones, <=0.5 => generally do not
        return r > 0.5

    def _compute_confidence(
        self,
        *,
        net: float,
        accept_threshold: float,
        overpay_floor: float,
        scale: float,
        verdict: DealVerdict,
    ) -> float:
        cfg = self.config
        # normalize distance to scale for stable confidence across deal sizes
        s = max(scale, cfg.eps)

        if verdict == DealVerdict.ACCEPT:
            # distance from accept boundary (or from overpay floor if accepted via overpay)
            if net >= accept_threshold:
                d = (net - accept_threshold) / s
            else:
                d = (net - overpay_floor) / s
            return _clamp(_sigmoid(cfg.confidence_slope * d), 0.10, 0.98)

        if verdict == DealVerdict.REJECT:
            # distance below overpay floor (or below accept threshold)
            if net < overpay_floor:
                d = (overpay_floor - net) / s
            else:
                d = (accept_threshold - net) / s
            return _clamp(_sigmoid(cfg.confidence_slope * d), 0.10, 0.98)

        # COUNTER: medium confidence, increases if close to boundary
        d = abs(accept_threshold - net) / s
        # closer => higher confidence for counter
        return _clamp(0.40 + 0.40 * _sigmoid(cfg.confidence_slope * (0.12 - d)), 0.20, 0.85)


# -----------------------------------------------------------------------------
# Convenience functional API
# -----------------------------------------------------------------------------
def decide_deal(
    *,
    evaluation: TeamDealEvaluation,
    ctx: DecisionContext,
    config: Optional[DecisionPolicyConfig] = None,
    rng: Optional[random.Random] = None,
    allow_counter: bool = True,
) -> DealDecision:
    """
    Stateless helper for service layer usage.
    """
    pol = DecisionPolicy(config=config or DecisionPolicyConfig())
    return pol.decide(evaluation=evaluation, ctx=ctx, rng=rng, allow_counter=allow_counter)

