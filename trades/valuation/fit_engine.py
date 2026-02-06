from __future__ import annotations

"""fit_engine.py

SSOT for player-team fit evaluation.

This module extracts the fit logic that previously lived inside
trade/trades/valuation/team_utility.py (TeamUtilityAdjuster._apply_fit and its
helper methods) into a reusable engine.

Design constraints (mirrors original behavior)
---------------------------------------------
- Consumes DecisionContext.need_map only; does not create team needs.
- Supply vector is derived solely from PlayerSnapshot (meta/attrs).
- Unknown/unsupported need tags are excluded from fit scoring.
- Fit factor and below-threshold penalty are applied identically to the
  original TeamUtilityAdjuster logic, including step codes/labels/meta.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, List

from decision_context import DecisionContext
from role_need_tags import ROLE_TO_NEED_TAG as ROLE_TO_NEED_TAG_SSOT, role_to_need_tag_only

from .types import (
    FitAssessment,
    PlayerSnapshot,
    ValuationStage,
    StepMode,
    ValuationStep,
)


# =============================================================================
# Helpers (pure) — copied verbatim from team_utility.py
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


# fit 계산에서 “측정 가능한(=공급 벡터로 정의된)” 태그만 반영한다.
# - depth/upgrade/cap-flex 같은 구조적 니즈 태그는 여기서 0점으로 끌어내리지 않도록 제외.
# - custom supply extractor가 추가 태그를 제공하는 경우(supply.keys)에는 그 태그도 자동 지원.
FIT_SUPPORTED_TAGS_BASE = frozenset(set(ROLE_TO_NEED_TAG_SSOT.values()) | {"DEFENSE"})


# =============================================================================
# Config
# =============================================================================
@dataclass(frozen=True, slots=True)
class FitEngineConfig:
    """Deterministic config for fit evaluation.

    NOTE: Defaults are copied from TeamUtilityConfig in team_utility.py.
    """

    # --- Fit scoring
    fit_neutral_score: float = 0.50
    fit_factor_floor: float = 0.70
    fit_factor_cap: float = 1.35
    fit_below_threshold_floor: float = 0.35
    fit_below_threshold_strength: float = 2.0  # threshold 아래면 더 빠르게 할인

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
# Explainability structures
# =============================================================================
@dataclass(frozen=True, slots=True)
class FitScoreBreakdown:
    total_weight: float
    used_need_tags: Tuple[str, ...]
    ignored_need_tags: Tuple[str, ...]
    weight_by_tag: Dict[str, float]
    supply_by_tag: Dict[str, float]
    contribution_by_tag: Dict[str, float]


@dataclass(frozen=True, slots=True)
class PlayerFitResult:
    need_map: Dict[str, float]
    supply: Dict[str, float]
    fit: FitAssessment
    fit_factor: float
    threshold_penalty: float
    multiplier: float
    steps: Tuple[ValuationStep, ...]
    breakdown: FitScoreBreakdown


# =============================================================================
# Engine
# =============================================================================
@dataclass(slots=True)
class FitEngine:
    config: FitEngineConfig = field(default_factory=FitEngineConfig)

    # ---------------------------------------------------------------------
    # 1) Need map resolution (consume only; never generate)
    # ---------------------------------------------------------------------
    def resolve_need_map(self, ctx: DecisionContext) -> Dict[str, float]:
        """Resolve need_map with the same fallback logic as TeamUtilityAdjuster._apply_fit."""
        need_map = dict(ctx.need_map or {})
        if not need_map and ctx.policies is not None:
            # 정책 뷰가 붙어 있다면 거기에서 보강
            try:
                need_map = dict(ctx.policies.fit.need_map or {})
            except Exception:
                need_map = {}
        return need_map

    # ---------------------------------------------------------------------
    # 2) Supply vector extraction from a player snapshot
    # ---------------------------------------------------------------------
    def compute_player_supply_vector(self, snap: PlayerSnapshot) -> Dict[str, float]:
        """Compute player supply vector.

        Copied from TeamUtilityAdjuster._player_supply_vector.
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

    # ---------------------------------------------------------------------
    # 3) Fit scoring
    # ---------------------------------------------------------------------
    def _fit_supported_tags(self, supply: Mapping[str, float]) -> set[str]:
        # base(역할/휴리스틱) + custom extractor가 실제로 제공하는 태그 확장
        s = set(FIT_SUPPORTED_TAGS_BASE)
        s.update(str(k) for k in supply.keys())
        return s

    def score_fit(
        self,
        need_map: Mapping[str, float],
        supply: Mapping[str, float],
        *,
        neutral: Optional[float] = None,
    ) -> Tuple[float, Dict[str, float], FitScoreBreakdown]:
        """Compute fit score + matched_needs with explainability breakdown.

        Behavior matches TeamUtilityAdjuster._fit_score.
        """
        cfg = self.config
        neutral_score = cfg.fit_neutral_score if neutral is None else float(neutral)

        # (옵션 1) unknown need 태그는 fit 계산에서 제외한다.
        # - "선수가 못 채움"과 "평가 불가(정의되지 않은 태그)"를 구분하기 위함.
        supported_tags = self._fit_supported_tags(supply)

        total_w = 0.0
        acc = 0.0
        matched: Dict[str, float] = {}

        used_need_tags: List[str] = []
        ignored_need_tags: List[str] = []
        weight_by_tag: Dict[str, float] = {}
        supply_by_tag: Dict[str, float] = {}
        contribution_by_tag: Dict[str, float] = {}

        for tag, w in need_map.items():
            tag_s = str(tag)
            if tag_s not in supported_tags:
                ignored_need_tags.append(tag_s)
                continue
            ww = _clamp(_safe_float(w, 0.0), 0.0, 1.0)
            if ww <= 0.0:
                ignored_need_tags.append(tag_s)
                continue
            # NOTE: keep original semantics: lookup uses the original `tag` key.
            s = _clamp(_safe_float(supply.get(tag, 0.0), 0.0), 0.0, 1.0)

            total_w += ww
            acc += ww * s

            used_need_tags.append(tag_s)
            weight_by_tag[tag_s] = ww
            supply_by_tag[tag_s] = s
            contribution_by_tag[tag_s] = ww * s

            if s > 0.0:
                matched[tag_s] = s

        if total_w <= 0.0:
            # 니즈가 없거나(또는 전부 unsupported): fit은 중립으로 반환
            score = _clamp(neutral_score, 0.0, 1.0)
            breakdown = FitScoreBreakdown(
                total_weight=0.0,
                used_need_tags=tuple(),
                ignored_need_tags=tuple(dict.fromkeys(ignored_need_tags)),
                weight_by_tag=weight_by_tag,
                supply_by_tag=supply_by_tag,
                contribution_by_tag=contribution_by_tag,
            )
            return score, {}, breakdown

        score = acc / total_w
        score = _clamp(score, 0.0, 1.0)

        breakdown = FitScoreBreakdown(
            total_weight=total_w,
            used_need_tags=tuple(used_need_tags),
            ignored_need_tags=tuple(dict.fromkeys(ignored_need_tags)),
            weight_by_tag=weight_by_tag,
            supply_by_tag=supply_by_tag,
            contribution_by_tag=contribution_by_tag,
        )
        return score, matched, breakdown

    # ---------------------------------------------------------------------
    # 4) Full assessment (score -> multiplier/penalty + steps)
    # ---------------------------------------------------------------------
    def assess_player_fit(self, snap: PlayerSnapshot, ctx: DecisionContext) -> PlayerFitResult:
        """Assess a player's fit for a given team context.

        Produces the same FitAssessment + valuation steps as TeamUtilityAdjuster._apply_fit,
        plus a consolidated multiplier (fit factor × below-threshold penalty).
        """
        cfg = self.config

        need_map = self.resolve_need_map(ctx)

        threshold = _safe_float(ctx.knobs.min_fit_threshold, 0.0)
        threshold = _clamp(threshold, 0.0, 1.0)
        fit_scale = _safe_float(ctx.knobs.fit_scale, 0.0)
        fit_scale = _clamp(fit_scale, 0.0, 3.0)

        supply = self.compute_player_supply_vector(snap)
        fit_score, matched, breakdown = self.score_fit(need_map, supply, neutral=cfg.fit_neutral_score)

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
        fit_factor = _clamp(raw_factor, cfg.fit_factor_floor, cfg.fit_factor_cap)

        steps: List[ValuationStep] = [
            ValuationStep(
                stage=ValuationStage.TEAM,
                mode=StepMode.MUL,
                code="FIT_FACTOR",
                label="팀 니즈 적합도 배율",
                factor=fit_factor,
                meta={"fit_score": fit_score, "neutral": cfg.fit_neutral_score, "fit_scale": fit_scale},
            )
        ]

        threshold_penalty = 1.0
        if fit_score < threshold and threshold > cfg.eps:
            severity = (threshold - fit_score) / max(threshold, cfg.eps)  # 0..1+
            penalty = 1.0 / (1.0 + cfg.fit_below_threshold_strength * severity)
            penalty = _clamp(penalty, cfg.fit_below_threshold_floor, 1.0)
            threshold_penalty = penalty

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

        multiplier = float(fit_factor) * float(threshold_penalty)

        # Ensure JSON-friendly and stable key types.
        need_map_out = {str(k): _safe_float(v, 0.0) for k, v in (need_map or {}).items()}
        supply_out = {str(k): _clamp(_safe_float(v, 0.0), 0.0, 1.0) for k, v in (supply or {}).items()}

        return PlayerFitResult(
            need_map=need_map_out,
            supply=supply_out,
            fit=fit_assessment,
            fit_factor=float(fit_factor),
            threshold_penalty=float(threshold_penalty),
            multiplier=float(multiplier),
            steps=tuple(steps),
            breakdown=breakdown,
        )
