from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple, List, Callable

import math

from decision_context import DecisionContext
from role_need_tags import ROLE_TO_NEED_TAG as ROLE_TO_NEED_TAG_SSOT, role_to_need_tag_only

from .types import (
    AssetKind,
    AssetSnapshot,
    PlayerSnapshot,
    PickSnapshot,
    SwapSnapshot,
    FixedAssetSnapshot,
    MarketValuation,
    TeamValuation,
    FitAssessment,
    ValuationStage,
    StepMode,
    ValueComponents,
    ValuationStep,
    snapshot_kind,
    snapshot_ref_id,
)


# =============================================================================
# Module contract
# =============================================================================
"""
team_utility.py

Role
----
Transform MarketValuation (team-agnostic market price) into TeamValuation
(team-specific utility) using DecisionContext only.

Inputs:
- MarketValuation (from market_pricing.py)
- AssetSnapshot (player/pick/swap/fixed)
- DecisionContext (knobs + need_map)

Outputs:
- TeamValuation with:
  - market_value = MarketValuation.value
  - team_value = transformed ValueComponents(now,future)
  - team_steps = explainable adjustments (stage=TEAM)
  - fit assessment (players only, based on need_map matching)

Hard rules:
- Do NOT compute/modify market pricing primitives (OVR curve, pick curve, etc).
- Do NOT create/recompute team needs. Only consume DecisionContext.need_map.
- Do NOT validate feasibility (salary matching, apron rules, Stepien, locks, etc).
"""


# =============================================================================
# Helpers (pure)
# =============================================================================
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


def _vc(now: float = 0.0, future: float = 0.0) -> ValueComponents:
    return ValueComponents(float(now), float(future))


def _scale_components(v: ValueComponents, *, now_factor: float = 1.0, future_factor: float = 1.0) -> ValueComponents:
    return ValueComponents(v.now * float(now_factor), v.future * float(future_factor))


def _add_components(v: ValueComponents, delta: ValueComponents) -> ValueComponents:
    return ValueComponents(v.now + delta.now, v.future + delta.future)


def _normalize_0_1(value: float, *, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return _clamp((value - lo) / (hi - lo), 0.0, 1.0)


# fit 계산에서 “측정 가능한(=공급 벡터로 정의된)” 태그만 반영한다.
# - depth/upgrade/cap-flex 같은 구조적 니즈 태그는 여기서 0점으로 끌어내리지 않도록 제외.
# - custom supply extractor가 추가 태그를 제공하는 경우(supply.keys)에는 그 태그도 자동 지원.
FIT_SUPPORTED_TAGS_BASE = frozenset(set(ROLE_TO_NEED_TAG_SSOT.values()) | {"DEFENSE"})


# =============================================================================
# Config (tunable, deterministic)
# =============================================================================
@dataclass(frozen=True, slots=True)
class TeamUtilityConfig:
    """
    knobs는 DecisionContext에서 오므로 여기서는 "해석/적용 방식"만 조절한다.
    """

    # --- Weighting (now/future split application)
    weight_now_floor: float = 0.10
    weight_now_cap: float = 2.50
    weight_future_floor: float = 0.10
    weight_future_cap: float = 2.50

    # --- Pick multiplier application
    # pick_multiplier는 future에만 적용하는 편이 깔끔하다(픽은 미래자산).
    pick_future_factor_floor: float = 0.50
    pick_future_factor_cap: float = 2.50

    # --- Youth multiplier application (players)
    youth_age_start: float = 20.0
    youth_age_end: float = 28.0
    youth_future_factor_floor: float = 0.70
    youth_future_factor_cap: float = 1.80

    # --- Star premium exponent application (players)
    # 시장가를 다시 만들지 않고, "팀이 스타를 얼마나 비싸게 치는지"만 반영.
    star_reference_total: float = 15.0
    star_factor_floor: float = 0.80
    star_factor_cap: float = 1.80

    # --- Fit scoring
    fit_neutral_score: float = 0.50
    fit_factor_floor: float = 0.70
    fit_factor_cap: float = 1.35
    fit_below_threshold_floor: float = 0.35
    fit_below_threshold_strength: float = 2.0  # threshold 아래면 더 빠르게 할인

    # --- Risk discount (team preference)
    risk_factor_floor: float = 0.60
    risk_factor_cap: float = 1.00
    risk_age_start: float = 29.0
    risk_age_end: float = 36.0
    risk_contract_years_start: float = 2.0
    risk_contract_years_end: float = 5.0

    # --- Finance penalty (team preference)
    finance_factor_floor: float = 0.55
    finance_factor_cap: float = 1.00
    finance_salary_lo: float = 8_000_000.0
    finance_salary_hi: float = 40_000_000.0
    finance_term_weight: float = 0.35  # 긴 계약일수록 재정 부담 가중

    # --- Supply extraction hooks (확장성)
    # 프로젝트에서 실제 attrs_json 키가 확정되면 여기를 SSOT로 튜닝.
    attr_keys_spacing: Tuple[str, ...] = (
        "ThreePoint", "Three-Point Shot", "3PT", "CatchShoot", "SpotUp",
    )
    attr_keys_rim_pressure: Tuple[str, ...] = (
        "DrivingLayup", "CloseShot", "Finishing", "RimAttack", "DrawFoul",
    )
    attr_keys_primary_initiator: Tuple[str, ...] = (
        "BallHandle", "PassAccuracy", "Playmaking", "SpeedWithBall",
    )
    attr_keys_shot_creation: Tuple[str, ...] = (
        "ShotIQ", "ShotCreating", "PullUp", "Isolation", "MidRange",
    )
    attr_keys_defense: Tuple[str, ...] = (
        "PerimeterDefense", "InteriorDefense", "Steal", "Block", "DefIQ",
    )

    # attrs scale handling
    attr_scale_max: float = 99.0  # 2K-like scale fallback
    eps: float = 1e-9

    # Custom extractor override (원하면 외부에서 주입)
    custom_player_supply_extractor: Optional[Callable[[PlayerSnapshot], Dict[str, float]]] = None


# =============================================================================
# Engine
# =============================================================================
@dataclass(slots=True)
class TeamUtilityAdjuster:
    """
    Team utility engine (pure).
    - MarketValuation을 DecisionContext로 조정해서 TeamValuation 생성.
    """
    config: TeamUtilityConfig = field(default_factory=TeamUtilityConfig)

    # team-specific cache: (team_id, asset_key) -> TeamValuation
    _cache: Dict[Tuple[str, str], TeamValuation] = field(default_factory=dict, init=False)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    def value_asset(
        self,
        market: MarketValuation,
        snap: AssetSnapshot,
        ctx: DecisionContext,
    ) -> TeamValuation:
        """
        단일 진입점.
        - ctx는 knobs/need_map의 공급자.
        - snap은 fit/risk/finance 신호 제공자(공급 측).
        """
        key = (str(ctx.team_id), str(market.asset_key))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        kind = snapshot_kind(snap)
        ref_id = snapshot_ref_id(snap)

        team_steps: List[ValuationStep] = []
        value = market.value

        # 1) Apply now/future weights (DecisionContext.knobs)
        value = self._apply_now_future_weights(value, ctx, team_steps)

        # 2) Asset-type multipliers (DecisionContext.knobs)
        if kind in (AssetKind.PICK, AssetKind.SWAP):
            value = self._apply_pick_preference(value, ctx, team_steps)

        if kind == AssetKind.PLAYER:
            value = self._apply_youth_preference(value, snap, ctx, team_steps)
            value = self._apply_star_preference(value, market, ctx, team_steps)

        # 3) Fit / needs matching (players only; need_map is consumed, never created)
        fit: Optional[FitAssessment] = None
        if kind == AssetKind.PLAYER:
            value, fit = self._apply_fit(value, snap, ctx, team_steps)

        # NOTE:
        # fit은 "측정 가능한 태그"에 대해서만 적용한다.
        # DecisionContext.need_map에 포함된 depth/upgrade/cap-flex 등의 니즈는
        # fit 스코어를 0점으로 끌어내리는 방식으로 반영하지 않는다(과벌점 방지).

        # 4) Risk discount (players primarily; team preference)
        if kind == AssetKind.PLAYER:
            value = self._apply_risk_discount(value, snap, ctx, team_steps)

        # 5) Finance penalty (players primarily; team preference)
        if kind == AssetKind.PLAYER:
            value = self._apply_finance_penalty(value, snap, ctx, team_steps)

        out = TeamValuation(
            asset_key=market.asset_key,
            kind=kind,
            ref_id=str(ref_id),
            market_value=market.value,
            team_value=value,
            market_steps=market.steps,
            team_steps=tuple(team_steps),
            fit=fit,
            meta={"team_id": ctx.team_id},
        )
        self._cache[key] = out
        return out

    # -------------------------------------------------------------------------
    # 1) Weights: now vs future
    # -------------------------------------------------------------------------
    def _apply_now_future_weights(
        self,
        v: ValueComponents,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        w_now = _clamp(_safe_float(ctx.knobs.w_now, 1.0), cfg.weight_now_floor, cfg.weight_now_cap)
        w_fut = _clamp(_safe_float(ctx.knobs.w_future, 1.0), cfg.weight_future_floor, cfg.weight_future_cap)

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="WEIGHT_NOW_FUTURE",
                label="팀 윈도우 기반 now/future 가중",
                factor=None,  # now/future가 분리 적용이므로 factor는 None
                meta={"w_now": w_now, "w_future": w_fut},
            )
        )
        return _scale_components(v, now_factor=w_now, future_factor=w_fut)

    # -------------------------------------------------------------------------
    # 2-A) Picks: preference multiplier
    # -------------------------------------------------------------------------
    def _apply_pick_preference(
        self,
        v: ValueComponents,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        m = _safe_float(ctx.knobs.pick_multiplier, 1.0)
        m = _clamp(m, cfg.pick_future_factor_floor, cfg.pick_future_factor_cap)

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="PICK_MULTIPLIER_FUTURE",
                label="픽/미래자산 선호 배율(미래값에 적용)",
                factor=m,
                meta={"pick_multiplier": m},
            )
        )
        return _scale_components(v, now_factor=1.0, future_factor=m)

    # -------------------------------------------------------------------------
    # 2-B) Youth: preference multiplier (players)
    # -------------------------------------------------------------------------
    def _apply_youth_preference(
        self,
        v: ValueComponents,
        snap: PlayerSnapshot,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        base = _safe_float(ctx.knobs.youth_multiplier, 1.0)
        base = _clamp(base, cfg.youth_future_factor_floor, cfg.youth_future_factor_cap)

        age = _safe_float(snap.age, cfg.youth_age_end)
        # youth_score: younger -> 1.0, older -> 0.0
        youth_score = 1.0 - _normalize_0_1(age, lo=cfg.youth_age_start, hi=cfg.youth_age_end)
        factor = 1.0 + (base - 1.0) * youth_score

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="YOUTH_MULTIPLIER_FUTURE",
                label="유망/젊음 선호 배율(나이 기반, 미래값에 적용)",
                factor=factor,
                meta={"age": age, "youth_score": youth_score, "youth_multiplier": base},
            )
        )
        return _scale_components(v, now_factor=1.0, future_factor=factor)

    # -------------------------------------------------------------------------
    # 2-C) Star preference exponent (players)
    # -------------------------------------------------------------------------
    def _apply_star_preference(
        self,
        v: ValueComponents,
        market: MarketValuation,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        """
        시장가를 다시 만들지 않고,
        팀이 "상위 자산을 더 비싸게/덜 비싸게" 치는 성향을 factor로 환산해 적용한다.
        """
        cfg = self.config
        exp = _safe_float(ctx.knobs.star_premium_exponent, 1.0)
        exp = _clamp(exp, 0.75, 1.60)

        ref = max(cfg.star_reference_total, cfg.eps)
        x = max(market.value.total, cfg.eps) / ref

        # exp=1 -> 1.0
        # exp>1 -> big assets get factor up
        # exp<1 -> big assets get factor down
        factor = x ** (exp - 1.0)
        factor = _clamp(factor, cfg.star_factor_floor, cfg.star_factor_cap)

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="STAR_PREMIUM_FACTOR",
                label="상위 자산(스타) 선호 배율",
                factor=factor,
                meta={"star_premium_exponent": exp, "market_total": market.value.total, "reference_total": ref},
            )
        )
        return v.scale(factor)

    # -------------------------------------------------------------------------
    # 3) Fit: need matching (players)
    # -------------------------------------------------------------------------
    def _apply_fit(
        self,
        v: ValueComponents,
        snap: PlayerSnapshot,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> Tuple[ValueComponents, FitAssessment]:
        cfg = self.config

        need_map = dict(ctx.need_map or {})
        if not need_map and ctx.policies is not None:
            # 정책 뷰가 붙어 있다면 거기에서 보강
            try:
                need_map = dict(ctx.policies.fit.need_map or {})
            except Exception:
                need_map = {}

        threshold = _safe_float(ctx.knobs.min_fit_threshold, 0.0)
        threshold = _clamp(threshold, 0.0, 1.0)
        fit_scale = _safe_float(ctx.knobs.fit_scale, 0.0)
        fit_scale = _clamp(fit_scale, 0.0, 3.0)

        supply = self._player_supply_vector(snap)
        fit_score, matched = self._fit_score(need_map, supply, neutral=cfg.fit_neutral_score)

        passed = bool(fit_score >= threshold)

        fit_assessment = FitAssessment(
            fit_score=fit_score,
            threshold=threshold,
            passed=passed,
            matched_needs=matched,
            meta={"need_map_size": len(need_map), "supply_size": len(supply)},
        )

        # Fit factor around neutral:
        # neutral -> 1.0, above -> up, below -> down
        centered = (fit_score - cfg.fit_neutral_score) * 2.0  # -1..+1 scale
        raw_factor = 1.0 + fit_scale * centered
        factor = _clamp(raw_factor, cfg.fit_factor_floor, cfg.fit_factor_cap)

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="FIT_FACTOR",
                label="팀 니즈 적합도 배율",
                factor=factor,
                meta={"fit_score": fit_score, "neutral": cfg.fit_neutral_score, "fit_scale": fit_scale},
            )
        )
        v2 = v.scale(factor)

        # Below-threshold penalty (soft gate)
        if fit_score < threshold and threshold > cfg.eps:
            severity = (threshold - fit_score) / max(threshold, cfg.eps)  # 0..1+
            penalty = 1.0 / (1.0 + cfg.fit_below_threshold_strength * severity)
            penalty = _clamp(penalty, cfg.fit_below_threshold_floor, 1.0)

            steps.append(
                ValuationStep(
                    stage=ValuationStage.TEAM,
                    mode=StepMode.MUL,
                    code="FIT_BELOW_THRESHOLD_PENALTY",
                    label="적합도 임계치 미달 페널티",
                    factor=penalty,
                    meta={"threshold": threshold, "fit_score": fit_score, "severity": severity},
                )
            )
            v2 = v2.scale(penalty)

        return v2, fit_assessment

    def _fit_supported_tags(self, supply: Mapping[str, float]) -> set[str]:
        # base(역할/휴리스틱) + custom extractor가 실제로 제공하는 태그 확장
        s = set(FIT_SUPPORTED_TAGS_BASE)
        s.update(str(k) for k in supply.keys())
        return s

    def _fit_score(
        self,
        need_map: Mapping[str, float],
        supply: Mapping[str, float],
        *,
        neutral: float,
    ) -> Tuple[float, Dict[str, float]]:
        """
        need_map은 "수요", supply는 "공급".
        - 니즈 생성/재평가는 여기서 하지 않는다.
        - 매칭 결과는 explainability를 위해 tag별로 남긴다.
        """
        # (옵션 1) unknown need 태그는 fit 계산에서 제외한다.
        # - "선수가 못 채움"과 "평가 불가(정의되지 않은 태그)"를 구분하기 위함.
        supported_tags = self._fit_supported_tags(supply)
        
        total_w = 0.0
        acc = 0.0
        matched: Dict[str, float] = {}

        for tag, w in need_map.items():
            if str(tag) not in supported_tags:
                continue
            ww = _clamp(_safe_float(w, 0.0), 0.0, 1.0)
            if ww <= 0.0:
                continue
            s = _clamp(_safe_float(supply.get(tag, 0.0), 0.0), 0.0, 1.0)
            total_w += ww
            acc += ww * s
            if s > 0.0:
                matched[str(tag)] = s

        if total_w <= 0.0:
            # 니즈가 없거나(또는 전부 unsupported): fit은 중립으로 반환
            return _clamp(neutral, 0.0, 1.0), {}

        score = acc / total_w
        return _clamp(score, 0.0, 1.0), matched

    def _player_supply_vector(self, snap: PlayerSnapshot) -> Dict[str, float]:
        """
        공급 벡터는 팀과 무관하게 선수에게서 읽는다.
        - ctx.need_map을 만들지 않는다.
        - attrs_json / meta / role_fit 등 다양한 입력을 방어적으로 해석한다.
        """
        cfg = self.config

        if cfg.custom_player_supply_extractor is not None:
            try:
                out = cfg.custom_player_supply_extractor(snap) or {}
                return {str(k): _clamp(_safe_float(v, 0.0), 0.0, 1.0) for k, v in out.items()}
            except Exception:
                pass

        supply: Dict[str, float] = {}

        # (A) role_fit 기반 공급 (있다면 가장 신뢰)
        # 기대 형태: snap.meta["role_fit"] = {"Initiator_Primary": 0.72, ...}
        role_fit = None
        if isinstance(snap.meta, dict):
            role_fit = snap.meta.get("role_fit")
        if role_fit is None and isinstance(snap.attrs, dict):
            role_fit = snap.attrs.get("role_fit")

        if isinstance(role_fit, dict):
            for role, score in role_fit.items():
                tag = role_to_need_tag_only(str(role))
                # ROLE_GAP은 "정의되지 않은 역할"이므로 공급 태그로 쓰지 않는다.
                if tag == "ROLE_GAP":
                    continue
                s = _clamp(_safe_float(score, 0.0), 0.0, 1.0)
                supply[tag] = max(supply.get(tag, 0.0), s)

        # (B) attrs 기반 휴리스틱 공급 (role_fit이 부족할 때 보강)
        if isinstance(snap.attrs, dict):
            def attr_norm(keys: Tuple[str, ...]) -> float:
                # 여러 키 중 가장 큰 신호를 사용 (키가 섞여도 안정적)
                best = 0.0
                for k in keys:
                    if k in snap.attrs:
                        v = _safe_float(snap.attrs.get(k), 0.0)
                        # 0..99 또는 0..1 형태 모두 방어 처리
                        if v > 1.5:
                            v = v / max(cfg.attr_scale_max, cfg.eps)
                        best = max(best, _clamp(v, 0.0, 1.0))
                return best

            spacing = attr_norm(cfg.attr_keys_spacing)
            rim = attr_norm(cfg.attr_keys_rim_pressure)
            init = attr_norm(cfg.attr_keys_primary_initiator)
            create = attr_norm(cfg.attr_keys_shot_creation)
            defense = attr_norm(cfg.attr_keys_defense)

            if spacing > 0.0:
                supply["SPACING"] = max(supply.get("SPACING", 0.0), spacing)
            if rim > 0.0:
                supply["RIM_PRESSURE"] = max(supply.get("RIM_PRESSURE", 0.0), rim)
            if init > 0.0:
                supply["PRIMARY_INITIATOR"] = max(supply.get("PRIMARY_INITIATOR", 0.0), init)
            if create > 0.0:
                supply["SHOT_CREATION"] = max(supply.get("SHOT_CREATION", 0.0), create)
            if defense > 0.0:
                supply["DEFENSE"] = max(supply.get("DEFENSE", 0.0), defense)

        return supply

    # -------------------------------------------------------------------------
    # 4) Risk discount (players)
    # -------------------------------------------------------------------------
    def _apply_risk_discount(
        self,
        v: ValueComponents,
        snap: PlayerSnapshot,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        scale = _safe_float(ctx.knobs.risk_discount_scale, 0.0)
        scale = _clamp(scale, 0.0, 2.0)

        age = _safe_float(snap.age, cfg.risk_age_start)
        age_risk = _normalize_0_1(age, lo=cfg.risk_age_start, hi=cfg.risk_age_end)

        years = 0.0
        if snap.contract is not None:
            years = _safe_float(snap.contract.years, 0.0)
        term_risk = _normalize_0_1(years, lo=cfg.risk_contract_years_start, hi=cfg.risk_contract_years_end)

        # 단순 합성(0..1)
        risk_score = _clamp(0.65 * age_risk + 0.35 * term_risk, 0.0, 1.0)

        # scale이 클수록 더 할인
        factor = 1.0 - scale * 0.35 * risk_score
        factor = _clamp(factor, cfg.risk_factor_floor, cfg.risk_factor_cap)

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="RISK_DISCOUNT",
                label="리스크 회피 할인(나이/계약 기반)",
                factor=factor,
                meta={
                    "risk_discount_scale": scale,
                    "age": age,
                    "contract_years": years,
                    "age_risk": age_risk,
                    "term_risk": term_risk,
                    "risk_score": risk_score,
                },
            )
        )
        return v.scale(factor)

    # -------------------------------------------------------------------------
    # 5) Finance penalty (players)
    # -------------------------------------------------------------------------
    def _apply_finance_penalty(
        self,
        v: ValueComponents,
        snap: PlayerSnapshot,
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        scale = _safe_float(ctx.knobs.finance_penalty_scale, 0.0)
        scale = _clamp(scale, 0.0, 2.0)

        salary = _safe_float(snap.salary_amount, 0.0)
        if salary <= cfg.eps and snap.contract is not None and isinstance(snap.contract.salary_by_year, dict):
            # fallback: known salary proxy
            vals = [ _safe_float(x, 0.0) for x in snap.contract.salary_by_year.values() ]
            vals = [ x for x in vals if x > 0.0 ]
            if vals:
                salary = float(sorted(vals)[-1])

        burden = _normalize_0_1(salary, lo=cfg.finance_salary_lo, hi=cfg.finance_salary_hi)

        years = 0.0
        if snap.contract is not None:
            years = _safe_float(snap.contract.years, 0.0)
        term = _normalize_0_1(years, lo=1.0, hi=5.0)

        # 재정 부담 합성(0..1)
        finance_score = _clamp((1.0 - cfg.finance_term_weight) * burden + cfg.finance_term_weight * term, 0.0, 1.0)

        # scale이 클수록 더 할인
        factor = 1.0 - scale * 0.45 * finance_score
        factor = _clamp(factor, cfg.finance_factor_floor, cfg.finance_factor_cap)

        steps.append(
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="FINANCE_PENALTY",
                label="재정 부담 페널티(연봉/기간 기반)",
                factor=factor,
                meta={
                    "finance_penalty_scale": scale,
                    "salary": salary,
                    "contract_years": years,
                    "salary_burden": burden,
                    "term_burden": term,
                    "finance_score": finance_score,
                },
            )
        )
        return v.scale(factor)

