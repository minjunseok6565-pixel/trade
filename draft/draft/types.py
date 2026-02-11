from __future__ import annotations

"""Draft domain types.

This module is deliberately dependency-light so it can be imported by:
- draft.standings (pure standings computation)
- draft.lottery   (lottery simulation)
- draft.order     (round1/round2 slot mapping and pick_id -> slot mapping)
- draft.finalize  (integration layer using LeagueService/DB)

Conventions aligned with this codebase:
- team_id is a 3-letter uppercase string (e.g. 'LAL')
- pick_id uses the seeded format: "{year}_R{round}_{TEAM}" (e.g. "2026_R1_LAL")
- slot is per-round 1..30 (NOT overall 1..60). overall_no can be derived.
"""

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


TeamId = str
PickId = str
RoundNo = int
SlotNo = int


def norm_team_id(v: Any) -> str:
    """Normalize team id into canonical form used across the project."""
    return str(v or "").upper()


def make_pick_id(year: int, round_no: int, original_team: str) -> str:
    """Create a deterministic pick_id for a given original team and round."""
    tid = norm_team_id(original_team)
    return f"{int(year)}_R{int(round_no)}_{tid}"


@dataclass(frozen=True, slots=True)
class TeamRecord:
    """Regular season record snapshot used for draft ordering.

    wins/losses/pf/pa are derived from league.master_schedule games with status='final'.
    """

    team_id: TeamId
    wins: int
    losses: int
    pf: int = 0
    pa: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", norm_team_id(self.team_id))

        def _to_int(x: Any, default: int = 0) -> int:
            try:
                if x is None:
                    return default
                if isinstance(x, bool):
                    return default
                return int(x)
            except Exception:
                return default

        w = max(0, _to_int(self.wins))
        l = max(0, _to_int(self.losses))
        pf = _to_int(self.pf)
        pa = _to_int(self.pa)
        object.__setattr__(self, "wins", w)
        object.__setattr__(self, "losses", l)
        object.__setattr__(self, "pf", pf)
        object.__setattr__(self, "pa", pa)

    @property
    def games_played(self) -> int:
        return int(self.wins + self.losses)

    @property
    def win_fraction(self) -> Fraction:
        gp = self.games_played
        if gp <= 0:
            return Fraction(0, 1)
        return Fraction(int(self.wins), int(gp))

    @property
    def win_pct(self) -> float:
        gp = self.games_played
        if gp <= 0:
            return 0.0
        return float(self.wins) / float(gp)

    @property
    def point_diff(self) -> int:
        return int(self.pf - self.pa)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "team_id": self.team_id,
            "wins": int(self.wins),
            "losses": int(self.losses),
            "games_played": int(self.games_played),
            "win_pct": float(self.win_pct),
            "pf": int(self.pf),
            "pa": int(self.pa),
            "point_diff": int(self.point_diff),
        }


@dataclass(frozen=True, slots=True)
class LotteryResult:
    """Result of a single lottery run for the top-4 picks among 14 teams.

    Notes:
      - seed_order is the 14-team ordering (worst -> best) after tie-break resolution.
      - odds_by_team is the effective lottery weight used for this draw (typically sums to 100.0).
      - winners_top4 is ordered: [slot1, slot2, slot3, slot4]
      - audit is optional, for debugging/telemetry.
    """

    rng_seed: int
    seed_order: Tuple[TeamId, ...]
    odds_by_team: Dict[TeamId, float]
    winners_top4: Tuple[TeamId, TeamId, TeamId, TeamId]
    audit: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rng_seed": int(self.rng_seed),
            "seed_order": list(self.seed_order),
            "odds_by_team": dict(self.odds_by_team),
            "winners_top4": list(self.winners_top4),
            "audit": dict(self.audit) if isinstance(self.audit, dict) else {},
        }

    def top4_by_slot(self) -> Dict[int, TeamId]:
        return {i + 1: tid for i, tid in enumerate(self.winners_top4)}


@dataclass(frozen=True, slots=True)
class DraftOrderPlan:
    """Full computed draft order plan for a given draft year (pre-settlement).

    round1_slot_to_original_team / round2_slot_to_original_team are 30-long sequences
    representing the original owner per slot (1..30). These are then mapped to
    pick_id -> slot and passed into LeagueService.settle_draft_year().
    """

    draft_year: int
    records: Dict[TeamId, TeamRecord]
    rank_worst_to_best: Tuple[TeamId, ...]
    round1_slot_to_original_team: Tuple[TeamId, ...]
    round2_slot_to_original_team: Tuple[TeamId, ...]
    pick_order_by_pick_id: Dict[PickId, int]
    lottery_result: Optional[LotteryResult] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_year": int(self.draft_year),
            "records": {tid: rec.to_dict() for tid, rec in self.records.items()},
            "rank_worst_to_best": list(self.rank_worst_to_best),
            "round1_slot_to_original_team": list(self.round1_slot_to_original_team),
            "round2_slot_to_original_team": list(self.round2_slot_to_original_team),
            "pick_order_by_pick_id": dict(self.pick_order_by_pick_id),
            "lottery_result": None if self.lottery_result is None else self.lottery_result.to_dict(),
            "meta": dict(self.meta) if isinstance(self.meta, dict) else {},
        }


@dataclass(frozen=True, slots=True)
class DraftTurn:
    """A single draft turn (who is on the clock)."""

    round: RoundNo
    slot: SlotNo
    overall_no: int
    pick_id: PickId
    original_team: TeamId
    drafting_team: TeamId
    attrs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "round", int(self.round))
        object.__setattr__(self, "slot", int(self.slot))
        object.__setattr__(self, "overall_no", int(self.overall_no))
        object.__setattr__(self, "pick_id", str(self.pick_id))
        object.__setattr__(self, "original_team", norm_team_id(self.original_team))
        object.__setattr__(self, "drafting_team", norm_team_id(self.drafting_team))
        if not isinstance(self.attrs, dict):
            object.__setattr__(self, "attrs", {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round": int(self.round),
            "slot": int(self.slot),
            "overall_no": int(self.overall_no),
            "pick_id": str(self.pick_id),
            "original_team": self.original_team,
            "drafting_team": self.drafting_team,
            "attrs": dict(self.attrs),
        }
