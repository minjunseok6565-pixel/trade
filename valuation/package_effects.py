from __future__ import annotations

"""package_effects.py

Deal-level (package-level) valuation adjustments.

Why this module exists
----------------------
team_utility.py intentionally operates at *single-asset* granularity.
However, a trade is a *package* of assets, and several realistic effects cannot
be captured by per-player fit/needs matching:

  5) Consolidation / dispersion structure (many-for-1, 1-for-many)
  6) Diminishing returns for redundant incoming players (similar roles/positions)
  7) Soft roster slot / rotation limit waste (too many incoming players)
  8) Outgoing "hole" penalty (sharp loss in guard/wing/big resources)

In addition, team_situation may output need tags that are *deal-structural* and
should NOT be part of per-player fit scoring:

  - GUARD_DEPTH / WING_DEPTH / BIG_DEPTH / BENCH_DEPTH
  - CAP_FLEX
  - OFFENSE_UPGRADE / DEFENSE_UPGRADE

This module consumes DecisionContext.need_map (already computed by team_situation)
and only measures the *delta from the deal*, never re-evaluating the team.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from decision_context import DecisionContext

try:  # path safety (project integration will likely use relative import)
    from role_need_tags import role_to_need_tag_only
except Exception:  # pragma: no cover
    from .role_need_tags import role_to_need_tag_only

from .types import (
    AssetKind,
    AssetSnapshot,
    PlayerSnapshot,
    TeamValuation,
    ValueComponents,
    ValuationStep,
    ValuationStage,
    StepMode,
)


# -----------------------------------------------------------------------------
# Constants (need tags)
# -----------------------------------------------------------------------------
GUARD_DEPTH = "GUARD_DEPTH"
WING_DEPTH = "WING_DEPTH"
BIG_DEPTH = "BIG_DEPTH"
BENCH_DEPTH = "BENCH_DEPTH"
CAP_FLEX = "CAP_FLEX"
OFFENSE_UPGRADE = "OFFENSE_UPGRADE"
DEFENSE_UPGRADE = "DEFENSE_UPGRADE"


# -----------------------------------------------------------------------------
# Helpers (pure)
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


def _vc(now: float = 0.0, future: float = 0.0) -> ValueComponents:
    return ValueComponents(float(now), float(future))


def _split_total_to_components(total_delta: float, *, w_now: float, w_future: float) -> ValueComponents:
    """Split a scalar delta into ValueComponents using weights."""
    wn = max(0.0, float(w_now))
    wf = max(0.0, float(w_future))
    s = wn + wf
    if s <= 1e-9:
        return _vc(total_delta, 0.0)
    return _vc(total_delta * (wn / s), total_delta * (wf / s))


def _pos_tokens(pos: Optional[str]) -> List[str]:
    if not pos:
        return []
    s = str(pos).upper().replace(" ", "")
    # common formats: "PG", "SG/SF", "PF,C" etc.
    for sep in [",", "|", ";"]:
        s = s.replace(sep, "/")
    parts = [p for p in s.split("/") if p]
    return parts


def _infer_frontcourt_by_height(attrs: Mapping[str, Any]) -> Optional[str]:
    """Fallback: classify by height if pos is missing."""
    if not isinstance(attrs, Mapping):
        return None
    for k in ("height_in", "height_inches", "HeightInches", "HEIGHT_IN", "HEIGHT_INCHES"):
        if k in attrs:
            h = _safe_float(attrs.get(k), 0.0)
            if h >= 82:  # ~6'10
                return "BIG"
            if h >= 79:  # ~6'7
                return "WING"
            return "GUARD"
    return None


def classify_depth_buckets(player: PlayerSnapshot) -> List[str]:
    """Return one or more of {GUARD,WING,BIG} for depth accounting."""
    tokens = _pos_tokens(player.pos)
    out: List[str] = []

    if any(t in ("PG", "SG", "G") for t in tokens):
        out.append("GUARD")
    if any(t in ("SF", "WF", "F") for t in tokens):
        out.append("WING")
    if any(t in ("PF", "C", "FC", "B") for t in tokens):
        out.append("BIG")

    if not out:
        by_h = _infer_frontcourt_by_height(player.attrs)
        if by_h:
            out.append(by_h)

    # Still unknown -> treat as wing-ish by default (neutral)
    if not out:
        out.append("WING")
    return out


def _market_now_grade(tv: TeamValuation) -> float:
    """Rotation grade proxy: market now component (team-agnostic)."""
    return _safe_float(tv.market_value.now, 0.0)


def _team_total_grade(tv: TeamValuation) -> float:
    """Total grade proxy for ordering players in package heuristics."""
    return _safe_float(tv.team_value.total, 0.0)


def _primary_archetype_tag(player: PlayerSnapshot) -> str:
    """Pick a single archetype tag used for diminishing-returns bucketing.

    - Prefer role_fit (if present) -> map role->need_tag.
    - Fallback to depth bucket (GUARD/WING/BIG).

    This is intentionally simple: we only need a *stable* grouping.
    """
    role_fit = None
    if isinstance(player.meta, dict):
        role_fit = player.meta.get("role_fit")
    if role_fit is None and isinstance(player.attrs, dict):
        role_fit = player.attrs.get("role_fit")

    best_tag = None
    best_score = 0.0
    if isinstance(role_fit, dict):
        for role, score in role_fit.items():
            tag = role_to_need_tag_only(str(role))
            if tag == "ROLE_GAP":
                continue
            sc = _safe_float(score, 0.0)
            if sc > best_score:
                best_score = sc
                best_tag = tag
    if best_tag:
        return str(best_tag)

    # fallback to positional bucket
    buckets = classify_depth_buckets(player)
    return buckets[0] if buckets else "WING"


def _defense_signal(player: PlayerSnapshot) -> float:
    """0..1 defense signal proxy based on attrs/meta if available."""
    # 1) explicit meta/attrs override
    if isinstance(player.meta, dict) and "defense" in player.meta:
        return _clamp(_safe_float(player.meta.get("defense"), 0.5), 0.0, 1.0)
    if isinstance(player.attrs, dict) and "defense" in player.attrs:
        return _clamp(_safe_float(player.attrs.get("defense"), 0.5), 0.0, 1.0)

    # 2) typical attribute keys (2K-ish)
    keys = (
        "PerimeterDefense",
        "InteriorDefense",
        "Steal",
        "Block",
        "DefIQ",
        "DEF",
        "Defense",
    )
    best = 0.0
    if isinstance(player.attrs, dict):
        for k in keys:
            if k in player.attrs:
                v = _safe_float(player.attrs.get(k), 0.0)
                if v > 1.5:
                    v = v / 99.0
                best = max(best, _clamp(v, 0.0, 1.0))

    # default: neutral
    if best <= 1e-9:
        return 0.5
    return best


def _commitment_metric(player: PlayerSnapshot, *, current_season_year: Optional[int]) -> float:
    """Contract commitment proxy (salary * remaining_years).

    This is intentionally a *soft* approximation:
    - Options/partial guarantees are ignored in v1.
    - If remaining years cannot be derived, fallback to contract.years.
    """
    salary = _safe_float(player.salary_amount, 0.0)
    years = 0.0

    if player.contract is not None:
        # (A) explicit derived remaining_years
        if isinstance(player.contract.meta, dict) and "remaining_years" in player.contract.meta:
            years = _safe_float(player.contract.meta.get("remaining_years"), 0.0)
        else:
            # (B) compute from salary_by_year if current season is known
            if current_season_year is not None and player.contract.salary_by_year:
                years = sum(
                    1.0
                    for y, v in player.contract.salary_by_year.items()
                    if int(y) >= int(current_season_year) and _safe_float(v, 0.0) > 0
                )
            # (C) fallback to declared years
            if years <= 1e-9:
                years = _safe_float(player.contract.years, 0.0)

            # If salary is missing, try current season salary or average
            if salary <= 1e-9 and player.contract.salary_by_year:
                if current_season_year is not None and int(current_season_year) in player.contract.salary_by_year:
                    salary = _safe_float(player.contract.salary_by_year.get(int(current_season_year)), 0.0)
                if salary <= 1e-9:
                    vals = [_safe_float(v, 0.0) for v in player.contract.salary_by_year.values()]
                    if vals:
                        salary = float(sum(vals) / max(1, len(vals)))

    return max(0.0, salary) * max(0.0, years)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PackageEffectsConfig:
    # --- 5) Consolidation / dispersion
    consolidation_neutral: float = 0.5  # knobs.consolidation_bias baseline
    consolidation_scale: float = 0.10   # relative strength vs package totals
    consolidation_cap_ratio: float = 0.18

    # --- 6) Diminishing returns
    diminishing_factors: Tuple[float, ...] = (1.00, 0.78, 0.62, 0.50, 0.42)
    diminishing_now_weight: float = 0.85
    diminishing_future_weight: float = 0.35
    diminishing_min_bucket_grade: float = 1.5  # ignore very low value players

    # --- 7) Soft roster slot / rotation waste
    roster_excess_waste_rate: float = 0.85  # fraction of bottom incoming players' value wasted
    roster_excess_cap_ratio: float = 0.22

    # --- 8) Outgoing hole penalty
    hole_penalty_scale: float = 0.22
    hole_penalty_exponent: float = 1.15
    hole_penalty_cap_ratio: float = 0.18

    # --- Depth needs (structural)
    depth_need_scale: float = 1.00
    bench_low_grade: float = 1.5
    starter_cutoff_grade: float = 8.0

    # --- CAP_FLEX
    cap_flex_scale: float = 0.00000006  # scale salary*years (dollars) -> value units
    cap_flex_cap_ratio: float = 0.16

    # --- Upgrade needs
    upgrade_scale: float = 0.75
    defense_proxy_floor: float = 0.35
    defense_proxy_cap: float = 1.00

    eps: float = 1e-9


# -----------------------------------------------------------------------------
# Engine
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class PackageEffects:
    """Compute deal-level adjustments for a single team perspective."""

    config: PackageEffectsConfig = field(default_factory=PackageEffectsConfig)

    def apply(
        self,
        *,
        team_id: str,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        ctx: DecisionContext,
        current_season_year: Optional[int] = None,
    ) -> Tuple[ValueComponents, Tuple[ValuationStep, ...], Dict[str, Any]]:
        """Return (package_delta, steps, meta).

        package_delta is added to the summed TeamValuation.team_value totals.
        """
        steps: List[ValuationStep] = []
        meta: Dict[str, Any] = {}

        base_in = self._sum_team_values(incoming)
        base_out = self._sum_team_values(outgoing)
        base_out_total = max(base_out.total, self.config.eps)
        base_in_total = max(base_in.total, self.config.eps)
        package_scale_total = max(base_in_total, base_out_total)

        # 5) Consolidation / dispersion structure
        delta1 = self._consolidation_effect(incoming, outgoing, ctx, package_scale_total, steps)

        # 6) Diminishing returns for redundant incoming players
        delta2 = self._diminishing_returns(incoming, steps)

        # 7) Soft roster slot / rotation waste (too many incoming players)
        delta3 = self._roster_excess_waste(incoming, outgoing, base_out_total, steps)

        # Depth needs (GUARD/WING/BIG/BENCH) based on deal delta *only*
        delta4 = self._depth_need_adjustment(incoming, outgoing, ctx, steps)

        # 8) Outgoing hole penalty (position discontinuity)
        delta5 = self._outgoing_hole_penalty(incoming, outgoing, base_out_total, steps)

        # CAP_FLEX adjustment (contract commitment delta)
        delta6 = self._cap_flex_adjustment(incoming, outgoing, ctx, current_season_year, base_out_total, steps)

        # OFF/DEF upgrade adjustments
        delta7 = self._upgrade_adjustment(incoming, outgoing, ctx, steps)

        total = delta1 + delta2 + delta3 + delta4 + delta5 + delta6 + delta7
        meta["base_in"] = {"now": base_in.now, "future": base_in.future, "total": base_in.total}
        meta["base_out"] = {"now": base_out.now, "future": base_out.future, "total": base_out.total}
        meta["package_delta"] = {"now": total.now, "future": total.future, "total": total.total}
        meta["team_id"] = str(team_id)

        return total, tuple(steps), meta

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _sum_team_values(self, items: Sequence[Tuple[TeamValuation, AssetSnapshot]]) -> ValueComponents:
        out = ValueComponents.zero()
        for tv, _ in items:
            out = out + tv.team_value
        return out

    def _players(
        self, items: Sequence[Tuple[TeamValuation, AssetSnapshot]]
    ) -> List[Tuple[TeamValuation, PlayerSnapshot]]:
        out: List[Tuple[TeamValuation, PlayerSnapshot]] = []
        for tv, snap in items:
            if tv.kind == AssetKind.PLAYER and isinstance(snap, PlayerSnapshot):
                out.append((tv, snap))
        return out

    # ------------------------------------------------------------------
    # 5) Consolidation / dispersion
    # ------------------------------------------------------------------
    def _consolidation_effect(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        ctx: DecisionContext,
        package_total: float,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config

        in_vals = [max(_team_total_grade(tv), 0.0) for tv, _ in incoming if _team_total_grade(tv) > cfg.eps]
        out_vals = [max(_team_total_grade(tv), 0.0) for tv, _ in outgoing if _team_total_grade(tv) > cfg.eps]

        if not in_vals or not out_vals:
            return ValueComponents.zero()

        in_total = sum(in_vals)
        out_total = sum(out_vals)

        if in_total <= cfg.eps or out_total <= cfg.eps:
            return ValueComponents.zero()

        in_count = len(in_vals)
        out_count = len(out_vals)

        top_in_share = max(in_vals) / max(in_total, cfg.eps)
        top_out_share = max(out_vals) / max(out_total, cfg.eps)

        # consolidation shape: positive when this team gives many / gets concentrated
        count_shape = (out_count - in_count) / max(float(max(out_count, in_count)), 1.0)
        concentration_shape = (top_in_share - top_out_share)
        shape = _clamp(count_shape * concentration_shape, -1.0, 1.0)

        # preference: consolidation_bias in 0..1, neutral around 0.5
        pref = (_safe_float(ctx.knobs.consolidation_bias, cfg.consolidation_neutral) - cfg.consolidation_neutral) * 2.0
        pref = _clamp(pref, -1.0, 1.0)

        # star focus derived from exponent (1.0..~1.8)
        exp = _safe_float(ctx.knobs.star_premium_exponent, 1.0)
        star_focus = _clamp((exp - 1.0) / 0.8, 0.0, 1.0)

        # scale: bounded and proportional to package size
        raw = cfg.consolidation_scale * (0.60 + 0.40 * star_focus) * pref * shape * package_total
        cap = cfg.consolidation_cap_ratio * package_total
        raw = _clamp(raw, -cap, cap)

        if abs(raw) <= cfg.eps:
            return ValueComponents.zero()

        delta = _split_total_to_components(
            raw,
            w_now=_safe_float(ctx.knobs.w_now, 1.0),
            w_future=_safe_float(ctx.knobs.w_future, 1.0),
        )

        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="CONSOLIDATION_STRUCTURE",
                label="콘솔리데이션/분산(딜 구조) 선호 보정",
                delta=delta,
                meta={
                    "pref": pref,
                    "shape": shape,
                    "count_shape": count_shape,
                    "concentration_shape": concentration_shape,
                    "incoming_count": in_count,
                    "outgoing_count": out_count,
                    "top_in_share": top_in_share,
                    "top_out_share": top_out_share,
                    "star_focus": star_focus,
                },
            )
        )
        return delta

    # ------------------------------------------------------------------
    # 6) Diminishing returns
    # ------------------------------------------------------------------
    def _diminishing_returns(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        players = self._players(incoming)
        if len(players) <= 1:
            return ValueComponents.zero()

        # bucket incoming players by a single archetype tag
        buckets: Dict[str, List[Tuple[TeamValuation, PlayerSnapshot]]] = {}
        for tv, p in players:
            if _team_total_grade(tv) < cfg.diminishing_min_bucket_grade:
                continue
            tag = _primary_archetype_tag(p)
            buckets.setdefault(tag, []).append((tv, p))

        penalty = ValueComponents.zero()
        per_bucket: Dict[str, Any] = {}

        for tag, items in buckets.items():
            if len(items) <= 1:
                continue
            # most valuable first
            items_sorted = sorted(items, key=lambda x: _team_total_grade(x[0]), reverse=True)

            # factors: 1.0 for first, then diminishing
            factors = list(cfg.diminishing_factors)
            if len(items_sorted) > len(factors):
                factors.extend([factors[-1]] * (len(items_sorted) - len(factors)))

            bucket_pen = ValueComponents.zero()
            details: List[Dict[str, Any]] = []
            for i, (tv, p) in enumerate(items_sorted):
                f = _clamp(factors[i], 0.0, 1.0)
                if f >= 0.999:
                    continue
                # we already counted full tv.team_value; subtract wasted portion
                w = 1.0 - f
                bucket_pen = bucket_pen + _vc(
                    now=tv.team_value.now * w * cfg.diminishing_now_weight,
                    future=tv.team_value.future * w * cfg.diminishing_future_weight,
                )
                details.append(
                    {
                        "player_id": p.player_id,
                        "asset_key": tv.asset_key,
                        "rank": i + 1,
                        "factor": f,
                        "waste_ratio": w,
                        "team_value_total": tv.team_value.total,
                    }
                )

            if bucket_pen.total > cfg.eps:
                penalty = penalty - bucket_pen  # negative adjustment
                per_bucket[tag] = {
                    "penalty": {"now": bucket_pen.now, "future": bucket_pen.future, "total": bucket_pen.total},
                    "players": details,
                }

        if abs(penalty.total) <= cfg.eps:
            return ValueComponents.zero()

        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="DIMINISHING_RETURNS",
                label="중복/체감 감소(비슷한 역할/포지션 다수 인바운드)",
                delta=penalty,
                meta={"buckets": per_bucket},
            )
        )
        return penalty

    # ------------------------------------------------------------------
    # 7) Soft roster slot / rotation waste
    # ------------------------------------------------------------------
    def _roster_excess_waste(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        base_out_total: float,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        in_players = self._players(incoming)
        out_players = self._players(outgoing)

        excess = max(0, len(in_players) - len(out_players))
        if excess <= 0:
            return ValueComponents.zero()

        # The least valuable incoming players are most likely to be waived / unused.
        in_sorted = sorted(in_players, key=lambda x: _team_total_grade(x[0]))
        targets = in_sorted[:excess]
        if not targets:
            return ValueComponents.zero()

        wasted = ValueComponents.zero()
        detail: List[Dict[str, Any]] = []
        for tv, p in targets:
            wasted = wasted + tv.team_value.scale(cfg.roster_excess_waste_rate)
            detail.append(
                {
                    "player_id": p.player_id,
                    "asset_key": tv.asset_key,
                    "team_value_total": tv.team_value.total,
                }
            )

        # cap penalty
        cap = cfg.roster_excess_cap_ratio * max(base_out_total, cfg.eps)
        wasted_total = _clamp(wasted.total, 0.0, cap)
        if wasted_total <= cfg.eps:
            return ValueComponents.zero()

        # rescale to match cap if needed
        scale = wasted_total / max(wasted.total, cfg.eps)
        pen = wasted.scale(scale)
        pen = _vc(-abs(pen.now), -abs(pen.future))

        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="ROSTER_EXCESS_WASTE",
                label="로스터 슬롯/회전 한계로 인한 효용 누수(soft)",
                delta=pen,
                meta={
                    "excess_incoming_players": excess,
                    "waive_candidates": detail,
                    "waste_rate": cfg.roster_excess_waste_rate,
                },
            )
        )
        return pen

    # ------------------------------------------------------------------
    # Depth needs (structural, need_map-driven)
    # ------------------------------------------------------------------
    def _depth_need_adjustment(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        need_map = dict(ctx.need_map or {})
        if not need_map and getattr(ctx, "policies", None) is not None:
            try:
                need_map = dict(ctx.policies.fit.need_map or {})
            except Exception:
                need_map = {}

        w_guard = _clamp(_safe_float(need_map.get(GUARD_DEPTH), 0.0), 0.0, 1.0)
        w_wing = _clamp(_safe_float(need_map.get(WING_DEPTH), 0.0), 0.0, 1.0)
        w_big = _clamp(_safe_float(need_map.get(BIG_DEPTH), 0.0), 0.0, 1.0)
        w_bench = _clamp(_safe_float(need_map.get(BENCH_DEPTH), 0.0), 0.0, 1.0)

        if (w_guard + w_wing + w_big + w_bench) <= cfg.eps:
            return ValueComponents.zero()

        def depth_supply(items: Sequence[Tuple[TeamValuation, AssetSnapshot]]) -> Dict[str, float]:
            s = {"GUARD": 0.0, "WING": 0.0, "BIG": 0.0, "BENCH": 0.0}
            for tv, snap in items:
                if tv.kind != AssetKind.PLAYER or not isinstance(snap, PlayerSnapshot):
                    continue
                grade = max(_market_now_grade(tv), 0.0)
                buckets = classify_depth_buckets(snap)
                if not buckets:
                    continue
                share = grade / max(len(buckets), 1)
                for b in buckets:
                    if b in ("GUARD", "WING", "BIG"):
                        s[b] += share

                # bench: only count mid-tier players
                if cfg.bench_low_grade <= grade <= cfg.starter_cutoff_grade:
                    s["BENCH"] += grade
            return s

        in_s = depth_supply(incoming)
        out_s = depth_supply(outgoing)

        delta_guard = in_s["GUARD"] - out_s["GUARD"]
        delta_wing = in_s["WING"] - out_s["WING"]
        delta_big = in_s["BIG"] - out_s["BIG"]
        delta_bench = in_s["BENCH"] - out_s["BENCH"]

        bonus_total = (
            cfg.depth_need_scale
            * (w_guard * delta_guard + w_wing * delta_wing + w_big * delta_big + w_bench * delta_bench)
        )

        if abs(bonus_total) <= cfg.eps:
            return ValueComponents.zero()

        delta = _vc(now=bonus_total, future=0.0)
        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="DEPTH_NEED_DELTA",
                label="뎁스 니즈(G/W/B/Bench) 딜 델타 보정",
                delta=delta,
                meta={
                    "weights": {
                        GUARD_DEPTH: w_guard,
                        WING_DEPTH: w_wing,
                        BIG_DEPTH: w_big,
                        BENCH_DEPTH: w_bench,
                    },
                    "delta": {
                        "guard": delta_guard,
                        "wing": delta_wing,
                        "big": delta_big,
                        "bench": delta_bench,
                    },
                    "incoming_supply": in_s,
                    "outgoing_supply": out_s,
                },
            )
        )
        return delta

    # ------------------------------------------------------------------
    # 8) Outgoing hole penalty (delta-only, need_map-independent)
    # ------------------------------------------------------------------
    def _outgoing_hole_penalty(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        base_out_total: float,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config

        def supply(items: Sequence[Tuple[TeamValuation, AssetSnapshot]]) -> Dict[str, float]:
            s = {"GUARD": 0.0, "WING": 0.0, "BIG": 0.0}
            for tv, snap in items:
                if tv.kind != AssetKind.PLAYER or not isinstance(snap, PlayerSnapshot):
                    continue
                grade = max(_market_now_grade(tv), 0.0)
                buckets = classify_depth_buckets(snap)
                if not buckets:
                    continue
                share = grade / max(len(buckets), 1)
                for b in buckets:
                    if b in s:
                        s[b] += share
            return s

        in_s = supply(incoming)
        out_s = supply(outgoing)

        penalties: Dict[str, float] = {}
        pen_total = 0.0
        for b in ("GUARD", "WING", "BIG"):
            delta = in_s[b] - out_s[b]
            if delta < -cfg.eps:
                p = (abs(delta) ** cfg.hole_penalty_exponent) * cfg.hole_penalty_scale
                penalties[b] = p
                pen_total += p

        cap = cfg.hole_penalty_cap_ratio * max(base_out_total, cfg.eps)
        pen_total = _clamp(pen_total, 0.0, cap)
        if pen_total <= cfg.eps:
            return ValueComponents.zero()

        delta = _vc(now=-pen_total, future=0.0)
        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="OUTGOING_HOLE_PENALTY",
                label="아웃바운드 구멍(포지션/롤 단절) 페널티",
                delta=delta,
                meta={"penalties": penalties, "incoming_supply": in_s, "outgoing_supply": out_s},
            )
        )
        return delta

    # ------------------------------------------------------------------
    # CAP_FLEX (need_map-driven)
    # ------------------------------------------------------------------
    def _cap_flex_adjustment(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        ctx: DecisionContext,
        current_season_year: Optional[int],
        base_out_total: float,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config

        need_map = dict(ctx.need_map or {})
        if not need_map and getattr(ctx, "policies", None) is not None:
            try:
                need_map = dict(ctx.policies.fit.need_map or {})
            except Exception:
                need_map = {}
        w = _clamp(_safe_float(need_map.get(CAP_FLEX), 0.0), 0.0, 1.0)
        if w <= cfg.eps:
            return ValueComponents.zero()

        def sum_commit(items: Sequence[Tuple[TeamValuation, AssetSnapshot]]) -> float:
            acc = 0.0
            for tv, snap in items:
                if tv.kind != AssetKind.PLAYER or not isinstance(snap, PlayerSnapshot):
                    continue
                acc += _commitment_metric(snap, current_season_year=current_season_year)
            return acc

        in_c = sum_commit(incoming)
        out_c = sum_commit(outgoing)
        delta_commit = in_c - out_c

        raw = -w * delta_commit * cfg.cap_flex_scale
        cap = cfg.cap_flex_cap_ratio * max(base_out_total, cfg.eps)
        raw = _clamp(raw, -cap, cap)
        if abs(raw) <= cfg.eps:
            return ValueComponents.zero()

        delta = _vc(now=0.0, future=raw)
        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="CAP_FLEX_DELTA",
                label="유연성(CAP_FLEX) 딜 델타 보정",
                delta=delta,
                meta={
                    "weight": w,
                    "delta_commitment": delta_commit,
                    "scale": cfg.cap_flex_scale,
                    "incoming_commit": in_c,
                    "outgoing_commit": out_c,
                },
            )
        )
        return delta

    # ------------------------------------------------------------------
    # OFF/DEF upgrade (need_map-driven)
    # ------------------------------------------------------------------
    def _upgrade_adjustment(
        self,
        incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
        ctx: DecisionContext,
        steps: List[ValuationStep],
    ) -> ValueComponents:
        cfg = self.config
        need_map = dict(ctx.need_map or {})
        if not need_map and getattr(ctx, "policies", None) is not None:
            try:
                need_map = dict(ctx.policies.fit.need_map or {})
            except Exception:
                need_map = {}

        w_off = _clamp(_safe_float(need_map.get(OFFENSE_UPGRADE), 0.0), 0.0, 1.0)
        w_def = _clamp(_safe_float(need_map.get(DEFENSE_UPGRADE), 0.0), 0.0, 1.0)
        if (w_off + w_def) <= cfg.eps:
            return ValueComponents.zero()

        def sum_off(items: Sequence[Tuple[TeamValuation, AssetSnapshot]]) -> float:
            acc = 0.0
            for tv, snap in items:
                if tv.kind != AssetKind.PLAYER or not isinstance(snap, PlayerSnapshot):
                    continue
                acc += max(_market_now_grade(tv), 0.0)
            return acc

        def sum_def(items: Sequence[Tuple[TeamValuation, AssetSnapshot]]) -> float:
            acc = 0.0
            for tv, snap in items:
                if tv.kind != AssetKind.PLAYER or not isinstance(snap, PlayerSnapshot):
                    continue
                base = max(_market_now_grade(tv), 0.0)
                sig = _clamp(_defense_signal(snap), cfg.defense_proxy_floor, cfg.defense_proxy_cap)
                acc += base * sig
            return acc

        delta_off = sum_off(incoming) - sum_off(outgoing)
        delta_def = sum_def(incoming) - sum_def(outgoing)

        raw = cfg.upgrade_scale * (w_off * delta_off + w_def * delta_def)
        if abs(raw) <= cfg.eps:
            return ValueComponents.zero()

        delta = _vc(now=raw, future=0.0)
        steps.append(
            ValuationStep(
                stage=ValuationStage.PACKAGE,
                mode=StepMode.ADD,
                code="OFF_DEF_UPGRADE_DELTA",
                label="공격/수비 업그레이드(딜 조합) 보정",
                delta=delta,
                meta={
                    "weights": {OFFENSE_UPGRADE: w_off, DEFENSE_UPGRADE: w_def},
                    "delta_off": delta_off,
                    "delta_def": delta_def,
                    "upgrade_scale": cfg.upgrade_scale,
                },
            )
        )
        return delta


# -----------------------------------------------------------------------------
# Convenience functional API (for deal_evaluator)
# -----------------------------------------------------------------------------
def apply_package_effects(
    *,
    team_id: str,
    incoming: Sequence[Tuple[TeamValuation, AssetSnapshot]],
    outgoing: Sequence[Tuple[TeamValuation, AssetSnapshot]],
    ctx: DecisionContext,
    current_season_year: Optional[int] = None,
    config: Optional[PackageEffectsConfig] = None,
) -> Tuple[ValueComponents, Tuple[ValuationStep, ...], Dict[str, Any]]:
    """Stateless wrapper."""
    eng = PackageEffects(config=config or PackageEffectsConfig())
    return eng.apply(
        team_id=team_id,
        incoming=incoming,
        outgoing=outgoing,
        ctx=ctx,
        current_season_year=current_season_year,
    )

