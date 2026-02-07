from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple, Iterable, List

import math

from .types import (
    AssetKind,
    AssetSnapshot,
    PlayerSnapshot,
    PickSnapshot,
    SwapSnapshot,
    FixedAssetSnapshot,
    PickExpectation,
    ValuationStage,
    StepMode,
    ValueComponents,
    ValuationStep,
    MarketValuation,
    snapshot_kind,
    snapshot_ref_id,
)


# =============================================================================
# Module contract
# =============================================================================
"""
market_pricing.py

Role
----
League-wide asset pricing layer ("market price", team-agnostic).

Inputs:
- AssetSnapshot (player/pick/swap/fixed)
- Optional PickExpectation (expected pick position / uncertainty)

Outputs:
- MarketValuation with:
  - ValueComponents(now, future)
  - ValuationStep logs describing how the price was built

Hard rules:
- Do NOT use DecisionContext / needs / GM traits / team situation.
- Do NOT validate feasibility (salary matching, apron, Stepien, etc).
  Those must already be handled by trades/validator + trades/rules.

Design target:
- Deterministic, explainable, tunable with config.
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


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return int(default)
        return int(x)
    except Exception:
        return int(default)


def _clamp(x: float, lo: float, hi: float) -> float:
    xf = _safe_float(x, lo)
    if xf < lo:
        return float(lo)
    if xf > hi:
        return float(hi)
    return float(xf)


def _sigmoid(x: float) -> float:
    # stable sigmoid
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _softplus(x: float) -> float:
    # stable softplus
    if x > 30:
        return x
    return math.log1p(math.exp(x))


def _vc(now: float = 0.0, future: float = 0.0) -> ValueComponents:
    return ValueComponents(float(now), float(future))


# =============================================================================
# Config: curves & weights (tunable, deterministic)
# =============================================================================
@dataclass(frozen=True, slots=True)
class MarketPricingConfig:
    """
    모든 숫자는 '튜닝 가능'하도록 config로 모은다.
    - 이 config는 market stage에서만 쓰인다.
    """

    # --- Player base pricing
    player_ovr_center: float = 75.0
    player_ovr_scale: float = 6.0
    player_ovr_now_max: float = 25.0  # OVR 기반 now 상한(화폐 단위)
    player_ovr_now_min: float = 0.0

    # OVR 증가가 상위 구간에서 더 비싸지도록(비선형)
    player_star_softplus_scale: float = 0.55
    player_star_softplus_shift: float = 86.0

    # --- Age / horizon split (market-level expectation)
    age_peak: float = 27.0
    age_now_decay_per_year: float = 0.06
    age_future_growth_per_year_under_peak: float = 0.07
    age_future_decay_per_year_over_peak: float = 0.05
    age_future_floor: float = 0.20
    age_future_cap: float = 1.40

    # --- Contract efficiency (market-level)
    # expected salary as function of ovr -> compare vs actual to compute contract factor
    expected_salary_ovr_center: float = 75.0
    expected_salary_ovr_scale: float = 7.0
    expected_salary_midpoint: float = 18_000_000.0  # 달러 가정(프로젝트 단위에 맞게 조정)
    expected_salary_span: float = 16_000_000.0

    contract_efficiency_factor_floor: float = 0.70
    contract_efficiency_factor_cap: float = 1.35
    contract_years_penalty_per_year: float = 0.03  # 긴 계약은 market에서 살짝 할인

    # --- Pick base pricing
    pick_round1_base_future: float = 14.0
    pick_round2_base_future: float = 3.5

    # pick number -> value curve
    pick_num_best: int = 1
    pick_num_worst: int = 30
    pick_num_curve_power: float = 1.65  # 상위픽 프리미엄(비선형)

    # year discount (멀수록 가치 감소)
    pick_year_discount_rate: float = 0.10  # 1년당 할인

    # --- Protection expectation (TOP_N)
    protection_logit_k: float = 0.85  # convey probability sharpness
    protection_logit_bias: float = 0.0

    # --- Swap optionality (market-level)
    swap_exercise_scale: float = 0.65
    swap_gap_to_prob_scale: float = 0.35  # pick gap -> exercise prob

    # --- Fixed assets
    fixed_default_timing: str = "future"  # "now" or "future"

    # --- General
    eps: float = 1e-9


# =============================================================================
# Main engine
# =============================================================================
@dataclass(slots=True)
class MarketPricer:
    """
    market pricing 엔진.
    - pure logic: provider/DB 없음
    - caching 지원: deal evaluator가 같은 에셋을 여러 번 요청해도 안정/고속
    """
    config: MarketPricingConfig = field(default_factory=MarketPricingConfig)

    _cache_player: Dict[str, MarketValuation] = field(default_factory=dict, init=False)
    _cache_pick: Dict[str, MarketValuation] = field(default_factory=dict, init=False)
    _cache_swap: Dict[str, MarketValuation] = field(default_factory=dict, init=False)
    _cache_fixed: Dict[str, MarketValuation] = field(default_factory=dict, init=False)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    def price_snapshot(
        self,
        snap: AssetSnapshot,
        *,
        asset_key: str,
        pick_expectation: Optional[PickExpectation] = None,
        resolved_pick_a: Optional[PickSnapshot] = None,
        resolved_pick_b: Optional[PickSnapshot] = None,
        resolved_pick_a_expectation: Optional[PickExpectation] = None,
        resolved_pick_b_expectation: Optional[PickExpectation] = None,
    ) -> MarketValuation:
        """
        deal_evaluator에서 호출하는 단일 진입점.

        - pick: pick_expectation을 주입할 수 있음
        - swap: swap의 pick_id_a/b를 resolve한 PickSnapshot을 같이 주입할 수 있음
          (swap pricing은 pick snapshot + 기대순번/연도할인 기대치가 필요)
        """
        kind = snapshot_kind(snap)
        ref_id = snapshot_ref_id(snap)

        if kind == AssetKind.PLAYER:
            cached = self._cache_player.get(ref_id)
            if cached is not None:
                return cached
            out = self._price_player(snap, asset_key=asset_key)
            self._cache_player[ref_id] = out
            return out

        if kind == AssetKind.PICK:
            cached = self._cache_pick.get(ref_id)
            if cached is not None:
                return cached
            out = self._price_pick(snap, asset_key=asset_key, expectation=pick_expectation)
            self._cache_pick[ref_id] = out
            return out

        if kind == AssetKind.SWAP:
            cached = self._cache_swap.get(ref_id)
            if cached is not None:
                return cached
            out = self._price_swap(
                snap,
                asset_key=asset_key,
                pick_a=resolved_pick_a,
                pick_b=resolved_pick_b,
                pick_a_expectation=resolved_pick_a_expectation,
                pick_b_expectation=resolved_pick_b_expectation,
            )
            self._cache_swap[ref_id] = out
            return out

        cached = self._cache_fixed.get(ref_id)
        if cached is not None:
            return cached
        out = self._price_fixed(snap, asset_key=asset_key)
        self._cache_fixed[ref_id] = out
        return out

    # -------------------------------------------------------------------------
    # Player pricing
    # -------------------------------------------------------------------------
    def _price_player(self, snap: PlayerSnapshot, *, asset_key: str) -> MarketValuation:
        cfg = self.config
        steps: List[ValuationStep] = []

        ovr = _safe_float(snap.ovr, 70.0)
        age = _safe_float(snap.age, 27.0)

        # 1) OVR -> now base (sigmoid-like)
        now_base = self._ovr_to_now_value(ovr)
        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.ADD,
                code="OVR_NOW_BASE",
                label="OVR 기반 현재가치",
                delta=_vc(now=now_base, future=0.0),
                meta={"ovr": ovr},
            )
        )

        # 2) Star premium (softplus, high-OVR nonlinearity)
        star_bonus = self._ovr_star_bonus(ovr)
        if abs(star_bonus) > cfg.eps:
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.ADD,
                    code="OVR_STAR_BONUS",
                    label="상위 OVR 프리미엄",
                    delta=_vc(now=star_bonus, future=0.0),
                    meta={"ovr": ovr},
                )
            )

        base = _vc(now=now_base + star_bonus, future=0.0)

        # 3) Age -> future multiplier (market-level expected horizon)
        age_future_factor = self._age_to_future_factor(age)
        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.MUL,
                code="AGE_FUTURE_FACTOR",
                label="나이 기반 미래가치 배율",
                factor=age_future_factor,
                delta=_vc(0.0, 0.0),
                meta={"age": age},
            )
        )

        # We create a future component from current base using the factor.
        # This is "market expectation" and does not consider team window.
        future_from_age = base.total * (age_future_factor - 1.0)
        # If factor < 1, future_from_age becomes negative: we keep it as a reduction.
        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.ADD,
                code="AGE_FUTURE_COMPONENT",
                label="나이 기반 미래가치 구성",
                delta=_vc(now=0.0, future=future_from_age),
                meta={"age": age, "factor": age_future_factor},
            )
        )

        value = _vc(now=base.now, future=future_from_age)

        # 4) Contract efficiency factor (market-level)
        contract_factor, contract_meta = self._contract_efficiency_factor(snap)
        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.MUL,
                code="CONTRACT_EFF_FACTOR",
                label="계약 효율(연봉/기간) 배율",
                factor=contract_factor,
                meta=contract_meta,
            )
        )
        value = value.scale(contract_factor)

        # 5) Position scarcity (optional, market-level)
        pos_factor, pos_meta = self._position_scarcity_factor(snap)
        if abs(pos_factor - 1.0) > cfg.eps:
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.MUL,
                    code="POSITION_SCARCITY",
                    label="포지션 희소성 배율",
                    factor=pos_factor,
                    meta=pos_meta,
                )
            )
            value = value.scale(pos_factor)

        return MarketValuation(
            asset_key=asset_key,
            kind=AssetKind.PLAYER,
            ref_id=str(snap.player_id),
            value=value,
            steps=tuple(steps),
            meta={"name": snap.name, "pos": snap.pos, "team_id": snap.team_id},
        )

    def _ovr_to_now_value(self, ovr: float) -> float:
        cfg = self.config
        # Sigmoid mapping to [min, max]
        x = (ovr - cfg.player_ovr_center) / max(cfg.player_ovr_scale, cfg.eps)
        s = _sigmoid(x)
        return cfg.player_ovr_now_min + (cfg.player_ovr_now_max - cfg.player_ovr_now_min) * s

    def _ovr_star_bonus(self, ovr: float) -> float:
        cfg = self.config
        # Softplus above a shift; scaled
        x = (ovr - cfg.player_star_softplus_shift) * cfg.player_star_softplus_scale
        return _softplus(x) * 0.9  # bonus magnitude is tunable; keep deterministic

    def _age_to_future_factor(self, age: float) -> float:
        cfg = self.config
        # below peak -> growth, above peak -> decay, both clamped
        if age <= cfg.age_peak:
            diff = cfg.age_peak - age
            factor = 1.0 + diff * cfg.age_future_growth_per_year_under_peak
        else:
            diff = age - cfg.age_peak
            factor = 1.0 - diff * cfg.age_future_decay_per_year_over_peak
        return _clamp(factor, cfg.age_future_floor, cfg.age_future_cap)

    def _contract_efficiency_factor(self, snap: PlayerSnapshot) -> Tuple[float, Dict[str, Any]]:
        """
        contract efficiency는 '시장가'에 속한다:
        - 같은 선수라도 싼 계약이면 시장가가 올라가고, 비싼 계약이면 내려간다.
        """
        cfg = self.config
        ovr = _safe_float(snap.ovr, 70.0)

        # actual salary for the current season: prefer roster salary_amount, else contract meta
        actual_salary = _safe_float(snap.salary_amount, 0.0)
        contract_years = 0

        if snap.contract is not None:
            contract_years = _safe_int(snap.contract.years, 0)
            # contract salary_by_year may exist; but without current season injection,
            # we keep roster.salary_amount as the primary actual_salary signal.
            if actual_salary <= cfg.eps:
                # fallback to any known salary
                if isinstance(snap.contract.salary_by_year, dict) and snap.contract.salary_by_year:
                    # pick the smallest positive salary as proxy to avoid weird jumps
                    vals = [v for v in snap.contract.salary_by_year.values() if _safe_float(v, 0.0) > 0]
                    if vals:
                        actual_salary = float(sorted(vals)[0])

        expected_salary = self._expected_salary_from_ovr(ovr)
        if actual_salary <= cfg.eps:
            # if we truly don't know, neutral
            return 1.0, {"ovr": ovr, "expected_salary": expected_salary, "actual_salary": None, "years": contract_years}

        ratio = expected_salary / max(actual_salary, cfg.eps)
        # ratio > 1 => underpaid => value up
        # ratio < 1 => overpaid => value down
        factor = _clamp(ratio, cfg.contract_efficiency_factor_floor, cfg.contract_efficiency_factor_cap)

        # long contracts: slight market discount (not "risk", just price of immobility)
        if contract_years > 0:
            years_pen = 1.0 - cfg.contract_years_penalty_per_year * max(contract_years - 1, 0)
            years_pen = _clamp(years_pen, 0.75, 1.0)
            factor *= years_pen

        return float(factor), {
            "ovr": ovr,
            "expected_salary": expected_salary,
            "actual_salary": actual_salary,
            "salary_ratio": ratio,
            "years": contract_years,
        }

    def _expected_salary_from_ovr(self, ovr: float) -> float:
        cfg = self.config
        # sigmoid mapping to [midpoint - span, midpoint + span]
        x = (ovr - cfg.expected_salary_ovr_center) / max(cfg.expected_salary_ovr_scale, cfg.eps)
        s = _sigmoid(x)
        lo = cfg.expected_salary_midpoint - cfg.expected_salary_span
        hi = cfg.expected_salary_midpoint + cfg.expected_salary_span
        return lo + (hi - lo) * s

    def _position_scarcity_factor(self, snap: PlayerSnapshot) -> Tuple[float, Dict[str, Any]]:
        """
        포지션 희소성은 '시장' 레벨에서만 아주 가볍게 반영.
        - 팀 니즈/상황과는 무관하게, 리그 전체에서 구하기 어려운 타입이 비싸질 수 있다.
        - 여기서는 과도한 영향을 피하도록 배율 폭을 억제한다.
        """
        pos = (snap.pos or "").upper()
        # 간단한 예시: C 희소성 살짝, WING( SF/SG ) 살짝
        if "C" in pos and "PF" not in pos:
            return 1.04, {"pos": pos, "bucket": "center"}
        if "SF" in pos and "SG" in pos:
            return 1.02, {"pos": pos, "bucket": "wing_combo"}
        return 1.0, {"pos": pos, "bucket": "neutral"}

    # -------------------------------------------------------------------------
    # Pick pricing
    # -------------------------------------------------------------------------
    def _price_pick(
        self,
        snap: PickSnapshot,
        *,
        asset_key: str,
        expectation: Optional[PickExpectation],
    ) -> MarketValuation:
        cfg = self.config
        steps: List[ValuationStep] = []

        year = int(snap.year)
        rnd = int(snap.round)

        # 1) round base
        base = cfg.pick_round1_base_future if rnd == 1 else cfg.pick_round2_base_future
        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.ADD,
                code="PICK_ROUND_BASE",
                label="라운드 기반 기본가치",
                delta=_vc(now=0.0, future=base),
                meta={"round": rnd},
            )
        )

        # 2) expected pick number curve (if known)
        exp_num = None
        if expectation is not None:
            exp_num = expectation.expected_pick_number

        # fallback: use mid pick when unknown
        if exp_num is None:
            exp_num = 16.0

        curve_bonus = self._pick_number_bonus(float(exp_num), rnd=rnd)
        if abs(curve_bonus) > cfg.eps:
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.ADD,
                    code="PICK_EXPECTED_NUM_BONUS",
                    label="예상 순번 기반 프리미엄",
                    delta=_vc(now=0.0, future=curve_bonus),
                    meta={"expected_pick_number": float(exp_num), "round": rnd},
                )
            )

        value = _vc(now=0.0, future=base + curve_bonus)

        # 3) year discount (needs 'current season year' info to be perfect)
        # market_pricing 자체는 season_year를 모르므로, 기대연도 할인은 meta 기준으로 처리:
        # - expectation.meta["current_season_year"]가 있으면 사용
        cur_sy = None
        if expectation is not None and isinstance(expectation.meta, dict):
            cur_sy = expectation.meta.get("current_season_year")
        if cur_sy is not None:
            cur_sy_i = _safe_int(cur_sy, year)
            years_ahead = max(year - cur_sy_i, 0)
            disc = (1.0 - cfg.pick_year_discount_rate) ** years_ahead
            disc = _clamp(disc, 0.35, 1.0)
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.MUL,
                    code="PICK_YEAR_DISCOUNT",
                    label="연도 할인(먼 미래일수록 감소)",
                    factor=disc,
                    meta={"current_season_year": cur_sy_i, "pick_year": year, "years_ahead": years_ahead},
                )
            )
            value = value.scale(disc)
        else:
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.MUL,
                    code="PICK_YEAR_DISCOUNT_SKIPPED",
                    label="연도 할인(현재 시즌 정보 없음)",
                    factor=1.0,
                    meta={"pick_year": year},
                )
            )

        # 4) protection expectation (TOP_N)
        prot = snap.protection
        if isinstance(prot, dict) and (prot.get("type") or prot.get("rule")):
            value, prot_steps = self._apply_pick_protection(value, exp_num=float(exp_num), protection=prot)
            steps.extend(prot_steps)

        return MarketValuation(
            asset_key=asset_key,
            kind=AssetKind.PICK,
            ref_id=str(snap.pick_id),
            value=value,
            steps=tuple(steps),
            meta={
                "year": year,
                "round": rnd,
                "original_team": snap.original_team,
                "owner_team": snap.owner_team,
                "expected_pick_number": float(exp_num) if exp_num is not None else None,
            },
        )

    def _pick_number_bonus(self, expected_pick_number: float, *, rnd: int) -> float:
        """
        expected pick number가 상위일수록(숫자가 작을수록) 더 비싸지는 비선형 프리미엄.
        """
        cfg = self.config
        n = _clamp(expected_pick_number, float(cfg.pick_num_best), float(cfg.pick_num_worst))
        # normalize: best -> 1.0, worst -> 0.0
        t = (float(cfg.pick_num_worst) - n) / max(float(cfg.pick_num_worst - cfg.pick_num_best), cfg.eps)
        # power curve
        p = t ** cfg.pick_num_curve_power
        # round2는 프리미엄 폭을 낮춘다
        scale = 10.0 if rnd == 1 else 2.0
        return scale * p

    def _apply_pick_protection(
        self,
        value: ValueComponents,
        *,
        exp_num: float,
        protection: Mapping[str, Any],
    ) -> Tuple[ValueComponents, List[ValuationStep]]:
        """
        TOP_N 보호 픽의 시장가 기대값:
        E[value] = p_convey * unprotected_value + (1 - p_convey) * compensation_value

        convey probability는 expected_pick_number와 보호 기준 n의 차이를
        logistic으로 근사한다.
        """
        cfg = self.config
        steps: List[ValuationStep] = []

        prot_type = str(protection.get("type") or protection.get("rule") or "").upper()
        if prot_type != "TOP_N":
            # unknown protection: do not modify, but leave a log
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.ADD,
                    code="PICK_PROTECTION_UNSUPPORTED",
                    label="보호 규칙 미지원(가격 변경 없음)",
                    meta={"type": prot_type},
                )
            )
            return value, steps

        n = _safe_int(protection.get("n"), 0)
        comp = protection.get("compensation") or {}
        comp_value = _safe_float(comp.get("value"), 0.0)
        comp_label = str(comp.get("label") or "Protection compensation")

        # probability that pick conveys (not protected)
        gap = exp_num - float(n)  # positive => likely conveys
        p = _sigmoid(cfg.protection_logit_k * gap + cfg.protection_logit_bias)
        p = _clamp(p, 0.05, 0.95)

        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.MUL,
                code="PICK_CONVEY_PROB",
                label="보호 픽 convey 확률(근사)",
                factor=p,
                meta={"expected_pick_number": exp_num, "top_n": n, "gap": gap},
            )
        )

        # expected value blend (all future for picks)
        unprot = value.future
        blended_future = p * unprot + (1.0 - p) * comp_value

        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.ADD,
                code="PICK_PROTECTION_EXPECTATION",
                label="보호 기대값 블렌딩",
                delta=_vc(now=0.0, future=(blended_future - unprot)),
                meta={"comp_label": comp_label, "comp_value": comp_value},
            )
        )

        return _vc(now=value.now, future=blended_future), steps

    # -------------------------------------------------------------------------
    # Swap pricing
    # -------------------------------------------------------------------------
    def _price_swap(
        self,
        snap: SwapSnapshot,
        *,
        asset_key: str,
        pick_a: Optional[PickSnapshot],
        pick_b: Optional[PickSnapshot],
        pick_a_expectation: Optional[PickExpectation],
        pick_b_expectation: Optional[PickExpectation],
    ) -> MarketValuation:
        cfg = self.config
        steps: List[ValuationStep] = []

        # swap pricing needs underlying pick snapshots; if missing, return neutral with log
        if pick_a is None or pick_b is None:
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.ADD,
                    code="SWAP_MISSING_PICKS",
                    label="스왑 대상 픽 스냅샷 없음(가격 중립)",
                    delta=_vc(0.0, 0.0),
                    meta={"pick_id_a": snap.pick_id_a, "pick_id_b": snap.pick_id_b},
                )
            )
            return MarketValuation(
                asset_key=asset_key,
                kind=AssetKind.SWAP,
                ref_id=str(snap.swap_id),
                value=_vc(0.0, 0.0),
                steps=tuple(steps),
                meta={"active": snap.active, "owner_team": snap.owner_team},
            )

        # Swap market value:
        # value ~= exercise_prob * (V(best) - V(worst)) * swap_exercise_scale
        #
        # IMPORTANT:
        # - Do NOT rely on PickSnapshot.meta for expected pick number.
        # - Reuse pick pricing (_price_pick) so year discount / protection / curves stay consistent.

        mv_a = self._price_pick(
            pick_a,
            asset_key=f"pick:{pick_a.pick_id}",
            expectation=pick_a_expectation,
        )
        mv_b = self._price_pick(
            pick_b,
            asset_key=f"pick:{pick_b.pick_id}",
            expectation=pick_b_expectation,
        )

        v_a = float(mv_a.value.future)
        v_b = float(mv_b.value.future)

        # optionality gain must be symmetric w.r.t. A/B ordering
        gain_raw = max(v_a, v_b) - min(v_a, v_b)

        exp_a = float(pick_a_expectation.expected_pick_number) if (pick_a_expectation and pick_a_expectation.expected_pick_number is not None) else 16.0
        exp_b = float(pick_b_expectation.expected_pick_number) if (pick_b_expectation and pick_b_expectation.expected_pick_number is not None) else 16.0

        gap = abs(exp_a - exp_b)
        exercise_prob = _clamp(gap * cfg.swap_gap_to_prob_scale / 10.0, 0.15, 0.85)

        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.ADD,
                code="SWAP_OPTION_GAIN",
                label="스왑 옵션 기대 이득(프리미엄)",
                delta=_vc(now=0.0, future=gain_raw),
                meta={
                    "exp_a": exp_a,
                    "exp_b": exp_b,
                    "mv_a_future": v_a,
                    "mv_b_future": v_b,
                    "pick_id_a": pick_a.pick_id,
                    "pick_id_b": pick_b.pick_id,
                },
            )
        )

        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.MUL,
                code="SWAP_EXERCISE_PROB",
                label="스왑 행사 확률(근사)",
                factor=exercise_prob,
                meta={"gap": gap},
            )
        )

        steps.append(
            ValuationStep(
                stage=ValuationStage.MARKET,
                mode=StepMode.MUL,
                code="SWAP_EXERCISE_SCALE",
                label="스왑 옵션 스케일",
                factor=cfg.swap_exercise_scale,
                meta={},
            )
        )

        future = gain_raw * exercise_prob * cfg.swap_exercise_scale
        return MarketValuation(
            asset_key=asset_key,
            kind=AssetKind.SWAP,
            ref_id=str(snap.swap_id),
            value=_vc(now=0.0, future=future),
            steps=tuple(steps),
            meta={
                "pick_id_a": snap.pick_id_a,
                "pick_id_b": snap.pick_id_b,
                "owner_team": snap.owner_team,
                "active": snap.active,
            },
        )

    # -------------------------------------------------------------------------
    # Fixed asset pricing
    # -------------------------------------------------------------------------
    def _price_fixed(self, snap: FixedAssetSnapshot, *, asset_key: str) -> MarketValuation:
        cfg = self.config
        steps: List[ValuationStep] = []

        v = _safe_float(snap.value, 0.0)
        timing = (snap.attrs.get("timing") if isinstance(snap.attrs, dict) else None) or cfg.fixed_default_timing
        timing = str(timing).lower().strip()

        if timing == "now":
            value = _vc(now=v, future=0.0)
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.ADD,
                    code="FIXED_VALUE_NOW",
                    label="고정자산 가치(현재)",
                    delta=_vc(now=v, future=0.0),
                    meta={"timing": timing},
                )
            )
        else:
            value = _vc(now=0.0, future=v)
            steps.append(
                ValuationStep(
                    stage=ValuationStage.MARKET,
                    mode=StepMode.ADD,
                    code="FIXED_VALUE_FUTURE",
                    label="고정자산 가치(미래)",
                    delta=_vc(now=0.0, future=v),
                    meta={"timing": timing},
                )
            )

        return MarketValuation(
            asset_key=asset_key,
            kind=AssetKind.FIXED,
            ref_id=str(snap.asset_id),
            value=value,
            steps=tuple(steps),
            meta={"label": snap.label, "owner_team": snap.owner_team, "source_pick_id": snap.source_pick_id},
        )

