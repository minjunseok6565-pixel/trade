from __future__ import annotations

"""NBA Draft Lottery (pure).

We follow the modern NBA odds (post-2019 reform) for the bottom-14 teams:
  seeds 1..3: 14.0%
  seed 4:     12.5%
  seed 5:     10.5%
  seed 6:      9.0%
  seed 7:      7.5%
  seed 8:      6.0%
  seed 9:      4.5%
  seed 10:     3.0%
  seed 11:     2.0%
  seed 12:     1.5%
  seed 13:     1.0%
  seed 14:     0.5%

This module draws top-4 winners without replacement.

Input contract:
  - seed_order: 14 team_ids ordered worst -> best (after deterministic tie-break).
Output:
  - LotteryResult from draft.types.
"""

import random
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .types import LotteryResult, TeamId, norm_team_id


NBA_LOTTERY_ODDS_2019: Tuple[float, ...] = (
    14.0, 14.0, 14.0,
    12.5,
    10.5,
    9.0,
    7.5,
    6.0,
    4.5,
    3.0,
    2.0,
    1.5,
    1.0,
    0.5,
)


def _weighted_choice(rng: random.Random, items: Sequence[TeamId], weights: Sequence[float]) -> TeamId:
    total = 0.0
    cum: List[float] = []
    for w in weights:
        try:
            ww = float(w)
        except (TypeError, ValueError):
            ww = 0.0
        if ww < 0:
            ww = 0.0
        total += ww
        cum.append(total)

    if total <= 0:
        # Fallback to uniform deterministic choice.
        return items[int(rng.random() * len(items))]

    x = rng.random() * total
    # linear scan is fine (len <= 14)
    for i, c in enumerate(cum):
        if x <= c:
            return items[i]
    return items[-1]


def run_lottery_top4(
    seed_order: Sequence[TeamId],
    *,
    rng_seed: int,
    odds: Sequence[float] = NBA_LOTTERY_ODDS_2019,
    include_audit: bool = False,
) -> LotteryResult:
    """Run the top-4 lottery draw.

    Parameters
    ----------
    seed_order:
        14 teams (worst -> best).
    rng_seed:
        Deterministic RNG seed for reproducibility.
    odds:
        14 odds values corresponding to seed_order.
    include_audit:
        If True, includes draw steps in result.audit.
    """
    seed = [norm_team_id(t) for t in list(seed_order)]
    seed = [t for t in seed if t and t != "FA"]
    if len(seed) != 14:
        raise ValueError(f"seed_order must contain exactly 14 teams, got {len(seed)}")

    if len(odds) != 14:
        raise ValueError(f"odds must have length 14, got {len(list(odds))}")

    rng = random.Random(int(rng_seed))

    remaining_items = list(seed)
    remaining_odds = [float(x) for x in list(odds)]

    winners: List[TeamId] = []
    audit: Dict[str, Any] = {}

    for draw_no in range(1, 5):
        winner = _weighted_choice(rng, remaining_items, remaining_odds)
        winners.append(winner)
        if include_audit:
            audit.setdefault("draws", []).append(
                {
                    "draw_no": draw_no,
                    "candidates": list(remaining_items),
                    "weights": list(remaining_odds),
                    "winner": winner,
                }
            )
        # remove winner
        idx = remaining_items.index(winner)
        remaining_items.pop(idx)
        remaining_odds.pop(idx)

    odds_by_team = {seed[i]: float(list(odds)[i]) for i in range(14)}

    return LotteryResult(
        rng_seed=int(rng_seed),
        seed_order=tuple(seed),
        odds_by_team=odds_by_team,
        winners_top4=(winners[0], winners[1], winners[2], winners[3]),
        audit=audit,
    )
