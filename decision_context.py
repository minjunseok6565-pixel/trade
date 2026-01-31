
"""
decision_context.py

Purpose
-------
Build a per-team "Decision Context" by combining:
  (A) TeamSituation output (from team_situation.py)
  (B) GM trait profile (9 sliders, each 0..1)

The DecisionContext is intended to be the single input into your final trade
valuation/acceptance logic.

Design goals
------------
- Situation drives the baseline (reality), GM traits provide bias (style).
- Urgency/deadline/posture modulate how strongly traits apply.
- Hard constraints (apron/locks/cooldown/hard_flags) are passed through so
  the valuation layer can enforce "cannot do this" vs "should not do this".
- Outputs are simple scalar knobs: weights, multipliers, thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Mapping, Literal, List, Tuple, Callable

import warnings


# ---------------------------------------------------------------------
# Types (mirrors team_situation.py)
# ---------------------------------------------------------------------
CompetitiveTier = Literal["CONTENDER", "PLAYOFF_BUYER", "FRINGE", "RESET", "REBUILD", "TANK"]
TradePosture = Literal["AGGRESSIVE_BUY", "SOFT_BUY", "STAND_PAT", "SOFT_SELL", "SELL"]
TimeHorizon = Literal["WIN_NOW", "RE_TOOL", "REBUILD"]
ApronStatus = Literal["BELOW_CAP", "OVER_CAP", "ABOVE_1ST_APRON", "ABOVE_2ND_APRON"]

# ---------------------------------------------------------------------
# "Reality vs philosophy" elasticity (borrowed from decision_context.py C)
# ---------------------------------------------------------------------
ELASTICITY_BY_TIER: Dict[str, float] = {
    # Reality dominates on the extremes; philosophy dominates in the middle.
    "CONTENDER": 0.25,
    "PLAYOFF_BUYER": 0.35,
    "FRINGE": 0.55,
    "RESET": 0.65,
    "REBUILD": 0.45,
    "TANK": 0.25,
}

POSTURE_BUY_FACTOR: Dict[str, float] = {
    "AGGRESSIVE_BUY": 1.00,
    "SOFT_BUY": 0.65,
    "STAND_PAT": 0.25,
    "SOFT_SELL": 0.10,
    "SELL": 0.00,
}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def clamp01(x: float) -> float:
    try:
        xf = float(x)
    except Exception:
        return 0.0
    return 0.0 if xf < 0.0 else 1.0 if xf > 1.0 else xf

def clamp(x: float, lo: float, hi: float) -> float:
    try:
        xf = float(x)
    except Exception:
        return float(lo)
    return float(lo) if xf < lo else float(hi) if xf > hi else xf

def lerp(a: float, b: float, t: float) -> float:
    return float(a) + (float(b) - float(a)) * clamp01(t)

def apply_bias(base: float, trait: float, *, strength: float) -> float:
    """
    Shift a baseline by a trait centered at 0.5.
      base: 0..1
      trait: 0..1
      strength: 0..1-ish (typical 0.25~0.50)
    """
    return clamp01(clamp01(base) + (clamp01(trait) - 0.5) * float(strength))

def _avg(vals: List[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / max(1, len(vals))

def normalize_team_id(team_id: str) -> str:
    """Best-effort team id normalization.

    - Tries to use schema.normalize_team_id if your project provides it.
    - Falls back to upper-casing a stripped string.

    This is intentionally conservative; keep any richer alias mapping in your
    canonical schema layer if you have one.
    """
    raw = str(team_id or "").strip()
    if not raw:
        return ""

    # Prefer project canonical normalizer if available.
    try:
        from schema import normalize_team_id as _normalize  # type: ignore
    except Exception:
        _normalize = None

    if _normalize is not None:
        try:
            out = _normalize(raw)
            if isinstance(out, str) and out.strip():
                return out.strip()
        except Exception:
            pass

    return raw.upper()


# ---------------------------------------------------------------------
# Input: GM traits (9 sliders)
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GMTradeTraits:
    # 1) CompetitiveWindow (WinNowBias)
    competitive_window: float = 0.5

    # 2) PickPreference (PickValueBias)
    pick_preference: float = 0.5

    # 3) YouthCorePreference (YouthBias)
    youth_core_preference: float = 0.5

    # 4) StarFocus (StarChasing + Consolidation)
    star_focus: float = 0.5

    # 5) SystemFitPriority (FitStrictness)
    system_fit_priority: float = 0.5

    # 6) RiskTolerance (Risk appetite)
    risk_tolerance: float = 0.5

    # 7) FinancialConservatism (cap + contract risk)
    financial_conservatism: float = 0.5

    # 8) NegotiationToughness
    negotiation_toughness: float = 0.5

    # 9) RelationshipSensitivity
    relationship_sensitivity: float = 0.5


# ---------------------------------------------------------------------
# Intermediate: effective traits after situation modulation
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EffectiveTraits:
    eff_win_now: float
    eff_pick_pref: float
    eff_youth_bias: float
    eff_star_focus: float
    eff_fit_strict: float
    eff_risk_tol: float
    eff_fin_cons: float
    eff_neg_tough: float
    eff_rel_sens: float


# ---------------------------------------------------------------------
# Output knobs: what valuation logic should consume
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ValuationKnobs:
    # Core now-vs-future
    w_now: float
    w_future: float

    # Asset/value multipliers
    pick_multiplier: float
    youth_multiplier: float

    # Star/top-heavy behavior
    star_premium_exponent: float
    consolidation_bias: float

    # Fit / needs
    fit_scale: float
    min_fit_threshold: float  # below this, apply strong penalty / veto candidate

    # Discounts / penalties
    risk_discount_scale: float
    finance_penalty_scale: float

    # Negotiation / acceptance
    min_surplus_required: float  # required net surplus for "accept" (positive means stricter)
    overpay_budget: float        # allow up to -X (relative) for urgent win-now buys
    counter_rate: float          # probability to counter instead of accept/reject

    # Relationship scaling
    relationship_scale: float


# ---------------------------------------------------------------------
# Output policies: a structured "view" over knobs for readability/maintenance.
# (These are simple groupings; they do not add new behavior.)
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WeightsPolicy:
    # Now vs future weighting + preference for future assets
    w_now: float
    w_future: float
    pick_multiplier: float
    youth_multiplier: float


@dataclass(frozen=True, slots=True)
class StarPolicy:
    star_premium_exponent: float
    consolidation_bias: float


@dataclass(frozen=True, slots=True)
class FitPolicy:
    fit_scale: float
    need_map: Dict[str, float]


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    risk_discount_scale: float


@dataclass(frozen=True, slots=True)
class FinancePolicy:
    finance_penalty_scale: float


@dataclass(frozen=True, slots=True)
class NegotiationPolicy:
    min_surplus_required: float


@dataclass(frozen=True, slots=True)
class RelationshipPolicy:
    relationship_scale: float


@dataclass(frozen=True, slots=True)
class Policies:
    weights: WeightsPolicy
    star: StarPolicy
    fit: FitPolicy
    risk: RiskPolicy
    finance: FinancePolicy
    negotiation: NegotiationPolicy
    relationship: RelationshipPolicy


# ---------------------------------------------------------------------
# The Decision Context itself
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionContext:
    team_id: str

    # Snapshot identifiers (optional convenience)
    posture: TradePosture
    horizon: TimeHorizon
    tier: CompetitiveTier
    urgency: float

    # “Reality” preferences from TeamSituation
    base_preferences: Dict[str, float]

    # The two things we are combining
    gm_traits: GMTradeTraits
    effective_traits: EffectiveTraits

    # What valuation will use
    knobs: ValuationKnobs
    need_map: Dict[str, float]


    # Hard constraints + context pass-through
    apron_status: ApronStatus
    hard_flags: Dict[str, bool]
    locks_count: int
    cooldown_active: bool
    cooldown_throttle: float
    deadline_pressure: float

    # Debug / explainability
    debug: Dict[str, Any] = field(default_factory=dict)

    # Structured policies (grouped view over knobs)
    policies: Optional[Policies] = None


# ---------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------
def build_decision_context(
    *,
    team_situation: Any,
    gm_traits: GMTradeTraits,
    strength: Optional[Mapping[str, float]] = None,
    team_id: Optional[str] = None,
    warn_on_team_id_mismatch: bool = True,
    team_id_normalizer: Optional[Callable[[str], str]] = None,
) -> DecisionContext:
    """
    Parameters
    ----------
    team_situation:
        The TeamSituation object from team_situation.evaluate_team().
        We avoid a hard import to keep this module portable; we use attribute access.
    gm_traits:
        9-slider GM profile.
    strength:
        Optional per-axis override for bias strength. Keys:
          win_now, pick, youth, star, fit, risk, fin, neg, rel

    team_id:
        Optional explicit team id. If provided, it is normalized and compared
        against team_situation.team_id to catch wiring bugs.
    warn_on_team_id_mismatch:
        If True, emits a warning when normalized ids do not match.
    team_id_normalizer:
        Optional callable to normalize team ids (defaults to normalize_team_id).

    Returns
    -------
    DecisionContext
    """
    # Defaults: keep GM impactful but not reality-breaking.
    s = {
        "win_now": 0.35,
        "pick": 0.30,
        "youth": 0.30,
        "star": 0.35,
        "fit": 0.40,
        "risk": 0.45,
        "fin": 0.35,
        "neg": 0.45,
        "rel": 0.55,
    }
    if strength:
        for k, v in strength.items():
            if k in s:
                try:
                    s[k] = float(v)
                except Exception:
                    pass

    # Pull required fields safely.
    raw_tid = str(getattr(team_situation, "team_id", "") or "")
    provided_tid = str(team_id or raw_tid or "")
    _norm = team_id_normalizer or normalize_team_id
    tid = _norm(provided_tid)
    raw_tid_norm = _norm(raw_tid) if raw_tid else ""

    if warn_on_team_id_mismatch and raw_tid_norm and tid and raw_tid_norm != tid:
        warnings.warn(
            f"[decision_context] team_id mismatch: situation={raw_tid!r} (norm={raw_tid_norm!r}) vs "
            f"provided={provided_tid!r} (norm={tid!r})",
            stacklevel=2,
        )
    # Normalize tokens defensively (supports Enum-like objects via .name)
    _raw_posture = getattr(team_situation, "trade_posture", "STAND_PAT")
    posture = str(getattr(_raw_posture, "name", _raw_posture) or "STAND_PAT").upper()
    _raw_horizon = getattr(team_situation, "time_horizon", "RE_TOOL")
    horizon = str(getattr(_raw_horizon, "name", _raw_horizon) or "RE_TOOL").upper()
    _raw_tier = getattr(team_situation, "competitive_tier", "FRINGE")
    tier = str(getattr(_raw_tier, "name", _raw_tier) or "FRINGE").upper()
    urgency = clamp01(getattr(team_situation, "urgency", 0.5))

    preferences = getattr(team_situation, "preferences", {}) or {}
    if not isinstance(preferences, dict):
        preferences = {}
    p_win_now = clamp01(float(preferences.get("WIN_NOW", 0.5)))
    p_picks = clamp01(float(preferences.get("PICKS", 0.5)))
    p_cap_flex = clamp01(float(preferences.get("CAP_FLEX", 0.5)))

    constraints = getattr(team_situation, "constraints", None)
    apron_status = "OVER_CAP"
    hard_flags: Dict[str, bool] = {}
    locks_count = 0
    cooldown_active = False
    cooldown_throttle = 1.0
    deadline_pressure = 0.0
    if constraints is not None:
        _raw_apron = getattr(constraints, "apron_status", apron_status)
        apron_status = str(getattr(_raw_apron, "name", _raw_apron) or apron_status).upper()
        hard_flags = dict(getattr(constraints, "hard_flags", {}) or {})
        locks_count = int(getattr(constraints, "locks_count", 0) or 0)
        cooldown_active = bool(getattr(constraints, "cooldown_active", False))
        deadline_pressure = clamp01(float(getattr(constraints, "deadline_pressure", 0.0) or 0.0))

    cooldown_throttle = 0.55 if cooldown_active else 1.0

    # -----------------------------------------------------------------
    # Elasticity: shrink/expand how much GM traits can tilt reality.
    # - Extremes (CONTENDER/TANK) -> reality dominates (lower e)
    # - Middle tiers -> GM philosophy shows more (higher e)
    # - Urgency/deadline -> reality dominates more (traits matter less)
    # -----------------------------------------------------------------
    e0 = float(ELASTICITY_BY_TIER.get(tier, 0.55))
    e = e0 * (0.92 - 0.35 * urgency) * (0.95 - 0.25 * deadline_pressure)
    e = clamp(e, 0.15, 0.70)

    # Axis-specific multipliers (borrowed from C) to keep behavior sane:
    # - star is more "philosophy" (slightly less situational)
    # - finance tends to be more rigid (slightly more situational)
    axis_mul = {
        "win_now": 1.00,
        "pick": 0.85,
        "youth": 0.85,
        "star": 0.70,
        "fit": 0.85,
        "risk": 0.75,
        "fin": 0.90,
        "neg": 0.70,
        "rel": 0.70,
    }
    s_eff = {k: float(s[k]) * e * float(axis_mul.get(k, 1.0)) for k in s}

    signals = getattr(team_situation, "signals", None)
    star_power = 0.5
    core_age = 27.5
    young_core = 0.5
    role_fit_health = 0.5
    cap_space = 0.0
    payroll = 0.0
    re_sign_pressure = 0.0
    flexibility = 0.5
    if constraints is not None:
        payroll = float(getattr(constraints, "payroll", 0.0) or 0.0)
        cap_space = float(getattr(constraints, "cap_space", 0.0) or 0.0)
    if signals is not None:
        star_power = clamp01(float(getattr(signals, "star_power", star_power) or star_power))
        core_age = float(getattr(signals, "core_age", core_age) or core_age)
        young_core = clamp01(float(getattr(signals, "young_core", young_core) or young_core))
        role_fit_health = clamp01(float(getattr(signals, "role_fit_health", role_fit_health) or role_fit_health))
        re_sign_pressure = clamp01(float(getattr(signals, "re_sign_pressure", re_sign_pressure) or re_sign_pressure))
        flexibility = clamp01(float(getattr(signals, "flexibility", flexibility) or flexibility))

    needs = getattr(team_situation, "needs", []) or []
    need_weights: List[float] = []
    need_map: Dict[str, float] = {}
    if isinstance(needs, list):
        for n in needs:
            try:
                tag = str(getattr(n, "tag", "") or "")
                w = clamp01(float(getattr(n, "weight", 0.0) or 0.0))
            except Exception:
                continue
            if not tag:
                continue
            need_map[tag] = max(need_map.get(tag, 0.0), w)  # keep strongest
            need_weights.append(w)
    need_intensity = clamp01(_avg(need_weights))

    # -----------------------------------------------------------------
    # Effective traits (baseline from reality + GM bias)
    # -----------------------------------------------------------------

    # 1) Win-now effective
    win_now_base = p_win_now
    if posture in ("AGGRESSIVE_BUY", "SOFT_BUY"):
        win_now_base += 0.08
    elif posture in ("SELL", "SOFT_SELL"):
        win_now_base -= 0.08
    win_now_base += 0.10 * urgency
    win_now_base = clamp01(win_now_base)
    eff_win_now = apply_bias(win_now_base, gm_traits.competitive_window, strength=s_eff["win_now"])

    # 2) Pick preference effective
    pick_base = p_picks
    if posture in ("SELL", "SOFT_SELL"):
        pick_base += 0.10
    if eff_win_now > 0.75:
        pick_base -= 0.10
    pick_base = clamp01(pick_base)
    eff_pick_pref = apply_bias(pick_base, gm_traits.pick_preference, strength=s_eff["pick"])

    # 3) Youth bias effective
    age_youth = clamp01((29.0 - float(core_age)) / 8.0)  # ~21..29
    horizon_boost = 0.2 if horizon == "WIN_NOW" else 0.5 if horizon == "RE_TOOL" else 1.0
    youth_base = 0.40 * age_youth + 0.35 * young_core + 0.25 * horizon_boost
    youth_base -= 0.15 * eff_win_now
    youth_base = clamp01(youth_base)
    eff_youth_bias = apply_bias(youth_base, gm_traits.youth_core_preference, strength=s_eff["youth"])

    # 4) Star focus effective
    star_need = clamp01(0.85 - star_power)  # lack of star -> higher need
    star_base = 0.35 * star_need + 0.35 * eff_win_now + 0.15 * (1.0 if posture in ("AGGRESSIVE_BUY", "SOFT_BUY") else 0.0) + 0.15 * urgency
    star_base = clamp01(star_base)
    eff_star_focus = apply_bias(star_base, gm_traits.star_focus, strength=s_eff["star"])

    # 5) Fit strictness effective
    fit_base = 0.55 * need_intensity + 0.25 * (1.0 - role_fit_health) + 0.20 * (1.0 if posture in ("AGGRESSIVE_BUY", "SOFT_BUY") else 0.0)
    fit_base = clamp01(fit_base)
    eff_fit_strict = apply_bias(fit_base, gm_traits.system_fit_priority, strength=s_eff["fit"])

    # 6) Risk tolerance effective (more rebuild -> more tolerant, more urgent -> less tolerant)
    risk_base = 0.55 * (1.0 - eff_win_now) + 0.25 * (1.0 if horizon == "REBUILD" else 0.0) - 0.20 * urgency
    risk_base = clamp01(risk_base)
    eff_risk_tol = apply_bias(risk_base, gm_traits.risk_tolerance, strength=s_eff["risk"])

    # 7) Financial conservatism effective
    apron_severity = 0.10
    if apron_status == "OVER_CAP":
        apron_severity = 0.35
    elif apron_status == "ABOVE_1ST_APRON":
        apron_severity = 0.70
    elif apron_status == "ABOVE_2ND_APRON":
        apron_severity = 1.00
    cap_pressure = 0.0
    try:
        denom = max(1.0, abs(float(payroll)))
        cap_pressure = clamp01((-float(cap_space)) / denom)  # negative cap space => pressure
    except Exception:
        cap_pressure = 0.0
    fin_base = 0.45 * p_cap_flex + 0.35 * apron_severity + 0.20 * cap_pressure
    fin_base = clamp01(fin_base)
    eff_fin_cons = apply_bias(fin_base, gm_traits.financial_conservatism, strength=s_eff["fin"])
    if apron_status == "ABOVE_2ND_APRON":
        eff_fin_cons = max(eff_fin_cons, 0.75)  # guardrail

    # 8) Negotiation toughness effective (urgency reduces toughness)
    neg_base = 0.50
    if posture in ("SELL", "SOFT_SELL"):
        neg_base += 0.20
    elif posture in ("AGGRESSIVE_BUY", "SOFT_BUY"):
        neg_base -= 0.10
    neg_base -= 0.25 * urgency
    neg_base -= 0.15 * deadline_pressure
    # If key expirings exist, re-sign pressure tends to reduce toughness (need to decide soon).
    neg_base -= 0.10 * re_sign_pressure
    neg_base = clamp01(neg_base)
    eff_neg_tough = apply_bias(neg_base, gm_traits.negotiation_toughness, strength=s_eff["neg"])

    # 9) Relationship sensitivity effective
    rel_base = 0.50 + (0.15 if cooldown_active else 0.0)
    rel_base = clamp01(rel_base)
    eff_rel_sens = apply_bias(rel_base, gm_traits.relationship_sensitivity, strength=s_eff["rel"])

    effective = EffectiveTraits(
        eff_win_now=eff_win_now,
        eff_pick_pref=eff_pick_pref,
        eff_youth_bias=eff_youth_bias,
        eff_star_focus=eff_star_focus,
        eff_fit_strict=eff_fit_strict,
        eff_risk_tol=eff_risk_tol,
        eff_fin_cons=eff_fin_cons,
        eff_neg_tough=eff_neg_tough,
        eff_rel_sens=eff_rel_sens,
    )

    # -----------------------------------------------------------------
    # Convert effective traits into valuation knobs
    # -----------------------------------------------------------------
    # Now-vs-future weights: include urgency bump toward "now".
    w_now = clamp01(0.15 + 0.75 * eff_win_now + 0.10 * urgency)
    w_future = clamp01(1.0 - w_now)

    pick_multiplier = lerp(0.70, 1.30, eff_pick_pref)
    youth_multiplier = lerp(0.75, 1.30, eff_youth_bias)

    star_premium_exponent = 1.0 + 0.80 * eff_star_focus
    consolidation_bias = eff_star_focus

    fit_scale = lerp(0.60, 1.80, eff_fit_strict)
    # Fit gate threshold: stricter fit => higher minimum threshold
    min_fit_threshold = lerp(0.45, 0.70, eff_fit_strict)

    # Higher risk tolerance -> smaller discount
    risk_discount_scale = lerp(0.35, 0.08, eff_risk_tol)

    # Higher financial conservatism -> larger penalty
    finance_penalty_scale = lerp(0.40, 1.60, eff_fin_cons)

    # Min surplus: tougher negotiation means require more surplus.
    min_surplus_required = lerp(-0.03, 0.10, eff_neg_tough)
    if posture in ("SELL", "SOFT_SELL"):
        min_surplus_required += 0.03
    elif posture == "AGGRESSIVE_BUY":
        min_surplus_required -= 0.02
    min_surplus_required = float(min_surplus_required)

    # Overpay budget / counter rate (from C, adapted to dc2)
    buy_factor = float(POSTURE_BUY_FACTOR.get(posture, 0.25))
    overpay_budget = 0.18 * eff_win_now * urgency * buy_factor * (1.0 - 0.45 * eff_neg_tough)
    overpay_budget = clamp(overpay_budget, 0.0, 0.22)

    counter_rate = lerp(0.35, 0.80, eff_neg_tough) * lerp(1.0, 0.70, urgency)
    counter_rate = clamp(counter_rate, 0.10, 0.95)

    relationship_scale = lerp(0.0, 1.5, eff_rel_sens)

    knobs = ValuationKnobs(
        w_now=w_now,
        w_future=w_future,
        pick_multiplier=pick_multiplier,
        youth_multiplier=youth_multiplier,
        star_premium_exponent=star_premium_exponent,
        consolidation_bias=consolidation_bias,
        fit_scale=fit_scale,
        min_fit_threshold=min_fit_threshold,
        risk_discount_scale=risk_discount_scale,
        finance_penalty_scale=finance_penalty_scale,
        min_surplus_required=min_surplus_required,
        overpay_budget=float(overpay_budget),
        counter_rate=float(counter_rate),
        relationship_scale=relationship_scale,
    )

    policies = Policies(
        weights=WeightsPolicy(
            w_now=w_now,
            w_future=w_future,
            pick_multiplier=pick_multiplier,
            youth_multiplier=youth_multiplier,
        ),
        star=StarPolicy(
            star_premium_exponent=star_premium_exponent,
            consolidation_bias=consolidation_bias,
        ),
        fit=FitPolicy(
            fit_scale=fit_scale,
            need_map=dict(need_map),
        ),
        risk=RiskPolicy(
            risk_discount_scale=risk_discount_scale,
        ),
        finance=FinancePolicy(
            finance_penalty_scale=finance_penalty_scale,
        ),
        negotiation=NegotiationPolicy(
            min_surplus_required=min_surplus_required,
        ),
        relationship=RelationshipPolicy(
            relationship_scale=relationship_scale,
        ),
    )

    debug: Dict[str, Any] = {
        "team_id": {"situation": raw_tid, "provided": provided_tid, "normalized": tid},
        "base_preferences": {"WIN_NOW": p_win_now, "PICKS": p_picks, "CAP_FLEX": p_cap_flex},
        "need_intensity": need_intensity,
        "star_power": star_power,
        "core_age": core_age,
        "young_core": young_core,
        "role_fit_health": role_fit_health,
        "urgency": urgency,
        "deadline_pressure": deadline_pressure,
        "apron_status": apron_status,
        "apron_severity": apron_severity,
        "cap_space": cap_space,
        "payroll": payroll,
        "cap_pressure": cap_pressure,
        "strength": dict(s),
        "elasticity": {"base": e0, "final": e, "tier": tier},
        "strength_effective": dict(s_eff),
        "overpay_budget": overpay_budget,
        "counter_rate": counter_rate,
    }

    return DecisionContext(
        team_id=tid,
        posture=posture,
        horizon=horizon,
        tier=tier,
        urgency=urgency,
        base_preferences={"WIN_NOW": p_win_now, "PICKS": p_picks, "CAP_FLEX": p_cap_flex},
        gm_traits=gm_traits,
        effective_traits=effective,
        knobs=knobs,
        need_map=need_map,
        policies=policies,
        apron_status=apron_status,
        hard_flags=hard_flags,
        locks_count=locks_count,
        cooldown_active=cooldown_active,
        cooldown_throttle=float(cooldown_throttle),
        deadline_pressure=deadline_pressure,
        debug=debug,
    )


# ---------------------------------------------------------------------
# Convenience: load GM traits from gm_profiles.profile_json
# ---------------------------------------------------------------------
def gm_traits_from_profile_json(profile: Mapping[str, Any], *, default: Optional[GMTradeTraits] = None) -> GMTradeTraits:
    """
    Parse a gm_profiles.profile_json payload into GMTradeTraits.

    Expected keys (any missing falls back to default):
      - CompetitiveWindow
      - PickPreference
      - YouthCorePreference
      - StarFocus
      - SystemFitPriority
      - RiskTolerance
      - FinancialConservatism
      - NegotiationToughness
      - RelationshipSensitivity
    """
    d = dict(profile or {})
    base = default or GMTradeTraits()

    def _get(k: str, fallback: float) -> float:
        v = d.get(k, fallback)
        try:
            return clamp01(float(v))
        except Exception:
            return fallback

    return GMTradeTraits(
        competitive_window=_get("CompetitiveWindow", base.competitive_window),
        pick_preference=_get("PickPreference", base.pick_preference),
        youth_core_preference=_get("YouthCorePreference", base.youth_core_preference),
        star_focus=_get("StarFocus", base.star_focus),
        system_fit_priority=_get("SystemFitPriority", base.system_fit_priority),
        risk_tolerance=_get("RiskTolerance", base.risk_tolerance),
        financial_conservatism=_get("FinancialConservatism", base.financial_conservatism),
        negotiation_toughness=_get("NegotiationToughness", base.negotiation_toughness),
        relationship_sensitivity=_get("RelationshipSensitivity", base.relationship_sensitivity),
    )
