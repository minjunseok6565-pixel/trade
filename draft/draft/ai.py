from __future__ import annotations

"""Draft AI policy (MVP).

This is deliberately minimal and deterministic:
  - choose the highest OVR prospect available (BPA).
  - tie-break by (pos, temp_id) to remain stable.

Future extensions:
  - team needs / roster fit
  - positional scarcity
  - trade-down decisions
  - risk/potential model
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from .pool import DraftPool, Prospect
from .types import DraftTurn, TeamId, norm_team_id


@dataclass(frozen=True, slots=True)
class DraftAIContext:
    draft_year: int
    team_id: TeamId
    turn: DraftTurn
    meta: Dict[str, Any] = None  # optional misc knobs

    def __post_init__(self) -> None:
        object.__setattr__(self, "draft_year", int(self.draft_year))
        object.__setattr__(self, "team_id", norm_team_id(self.team_id))
        if self.meta is None:
            object.__setattr__(self, "meta", {})


class DraftAIPolicy(Protocol):
    def choose_prospect_temp_id(self, pool: DraftPool, ctx: DraftAIContext) -> str:
        ...


class BPAByOVRPolicy:
    """Best-player-available by OVR."""

    def choose_prospect_temp_id(self, pool: DraftPool, ctx: DraftAIContext) -> str:
        available = pool.list_available()
        if not available:
            raise RuntimeError("draft pool exhausted")
        # deterministic: sort by (-ovr, pos, temp_id)
        available.sort(key=lambda p: (-int(p.ovr), str(p.pos), str(p.temp_id)))
        return str(available[0].temp_id)
