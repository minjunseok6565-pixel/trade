from __future__ import annotations

import math
import random
from typing import Optional, Tuple

from .types import CollegeSeasonStats, DraftEntryDecisionTrace


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def estimate_projected_pick(
    *,
    ovr: int,
    age: int,
    class_year: int,
    potential: int,
    season_stats: Optional[CollegeSeasonStats],
    class_strength: float,
) -> int:
    """
    Rough internal projection -> pick number.
    Returns 1..90 ( >60 implies undrafted risk).
    This is used ONLY for declare decision (not for actual draft order).
    """
    prod = 0.0
    if season_stats:
        # A compact "production score" with diminishing returns
        prod = 0.35 * season_stats.pts + 0.18 * season_stats.reb + 0.22 * season_stats.ast
        prod += 6.0 * (season_stats.ts_pct - 0.52)
        prod += 3.0 * (season_stats.usg - 0.18)
        prod = float(_clamp(prod, -8.0, 22.0))

    # NBA preference: younger + upside (potential) matters
    youth_bonus = float(_clamp(21 - age, -2.0, 3.0))  # younger => positive
    class_bonus = 0.45 * (class_year - 1)  # older have "need to go" pressure but draft likes youth; we keep small here

    score = (
        1.00 * (ovr - 60)
        + 0.55 * (potential - 70)
        + 0.60 * prod
        + 1.10 * youth_bonus
        - 0.40 * class_bonus
        + 2.0 * class_strength
    )

    # Map score to pick: higher score => lower pick number
    # Tuned so typical good prospects land 10~45 range.
    # We allow >60 as undrafted risk zone.
    pick = int(round(55 - 0.9 * score))
    pick = int(_clamp(pick, 1, 90))
    return pick


def declare_probability(
    rng: random.Random,
    *,
    player_id: str,
    draft_year: int,
    ovr: int,
    age: int,
    class_year: int,
    potential: int,
    season_stats: Optional[CollegeSeasonStats],
    class_strength: float,
    projected_pick: Optional[int] = None,
) -> DraftEntryDecisionTrace:
    """
    Compute declare probability with a transparent factor breakdown.

    We intentionally separate:
    - player's desire to declare (age/class pressure)
    - draft outcome expectation (projected_pick / undrafted risk)
    """
    proj = projected_pick
    if proj is None:
        proj = estimate_projected_pick(
            ovr=ovr,
            age=age,
            class_year=class_year,
            potential=potential,
            season_stats=season_stats,
            class_strength=class_strength,
        )

    # Production scalar
    prod = 0.0
    if season_stats:
        prod = 0.25 * season_stats.pts + 0.12 * season_stats.reb + 0.18 * season_stats.ast
        prod += 8.0 * (season_stats.ts_pct - 0.52)
        prod = float(_clamp(prod, -6.0, 16.0))

    # Components (logit space)
    comp_ovr = 0.07 * (ovr - 60)
    comp_pot = 0.04 * (potential - 70)
    comp_prod = 0.10 * prod
    comp_age = 0.18 * (age - 19)
    comp_class = 0.35 * (class_year - 1)
    comp_strength = 0.35 * class_strength

    # Risk adjustment based on projected pick bucket
    # - top 30: strongly encouraged
    # - 31-60: modest encouragement
    # - >60: discouraged (likely return)
    if proj <= 30:
        comp_risk = 1.10
    elif proj <= 60:
        comp_risk = 0.25
    else:
        comp_risk = -1.35

    # Base bias: most players do NOT declare
    bias = -2.10

    logit = bias + comp_ovr + comp_pot + comp_prod + comp_age + comp_class + comp_strength + comp_risk

    # Small randomness in preference (kept tight for stability)
    logit += rng.gauss(0.0, 0.20)

    p = float(_clamp(_sigmoid(logit), 0.01, 0.99))
    declared = rng.random() < p

    return DraftEntryDecisionTrace(
        player_id=player_id,
        draft_year=int(draft_year),
        declared=bool(declared),
        declare_prob=float(p),
        projected_pick=int(proj) if proj is not None else None,
        factors={
            "bias": bias,
            "ovr": comp_ovr,
            "potential": comp_pot,
            "production": comp_prod,
            "age": comp_age,
            "class_year": comp_class,
            "class_strength": comp_strength,
            "risk_bucket": comp_risk,
            "logit": logit,
        },
        notes={
            "risk_proj_pick": int(proj),
            "prod_score": float(prod),
        },
    )
