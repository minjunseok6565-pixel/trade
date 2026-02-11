from __future__ import annotations

"""Draft session state machine (in-memory).

- Holds:
    * ordered turns (DraftTurn list; drafting_team already resolved)
    * prospect pool (DraftPool)
    * cursor (0..len(turns))
    * pick history

- Provides:
    * current_turn()
    * record_pick() (mutating)
    * serialization helpers
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .types import DraftTurn, TeamId, norm_team_id
from .pool import DraftPool


@dataclass(frozen=True, slots=True)
class DraftPick:
    overall_no: int
    round: int
    slot: int
    pick_id: str
    drafting_team: TeamId
    prospect_temp_id: str
    player_id: Optional[str] = None
    contract_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_no": int(self.overall_no),
            "round": int(self.round),
            "slot": int(self.slot),
            "pick_id": str(self.pick_id),
            "drafting_team": norm_team_id(self.drafting_team),
            "prospect_temp_id": str(self.prospect_temp_id),
            "player_id": self.player_id,
            "contract_id": self.contract_id,
            "meta": dict(self.meta) if isinstance(self.meta, dict) else {},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DraftPick":
        meta = dict(d.get("meta") or {}) if isinstance(d.get("meta"), Mapping) else {}
        return cls(
            overall_no=int(d.get("overall_no") or 0),
            round=int(d.get("round") or 0),
            slot=int(d.get("slot") or 0),
            pick_id=str(d.get("pick_id") or ""),
            drafting_team=norm_team_id(d.get("drafting_team") or ""),
            prospect_temp_id=str(d.get("prospect_temp_id") or ""),
            player_id=(str(d.get("player_id")) if d.get("player_id") is not None else None),
            contract_id=(str(d.get("contract_id")) if d.get("contract_id") is not None else None),
            meta=meta,
        )


@dataclass(slots=True)
class DraftSession:
    draft_year: int
    turns: List[DraftTurn]
    pool: DraftPool
    cursor: int = 0
    picks_by_turn_index: Dict[int, DraftPick] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.draft_year = int(self.draft_year)
        if not isinstance(self.turns, list):
            self.turns = list(self.turns or [])
        if self.cursor < 0:
            self.cursor = 0

    def is_complete(self) -> bool:
        return int(self.cursor) >= len(self.turns)

    def current_turn(self) -> DraftTurn:
        if self.is_complete():
            raise IndexError("draft session is complete")
        return self.turns[int(self.cursor)]

    def record_pick(
        self,
        *,
        prospect_temp_id: str,
        player_id: Optional[str] = None,
        contract_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> DraftPick:
        """Record a pick at the current cursor and advance cursor.

        This mutates:
          - pool.available_temp_ids (remove)
          - picks_by_turn_index
          - cursor
        """
        if self.is_complete():
            raise IndexError("cannot record pick: session complete")

        tid = str(prospect_temp_id)
        if not self.pool.is_available(tid):
            raise ValueError(f"prospect not available: temp_id={tid}")

        turn_index = int(self.cursor)
        turn = self.turns[turn_index]

        self.pool.mark_picked(tid)

        dp = DraftPick(
            overall_no=int(turn.overall_no),
            round=int(turn.round),
            slot=int(turn.slot),
            pick_id=str(turn.pick_id),
            drafting_team=turn.drafting_team,
            prospect_temp_id=tid,
            player_id=player_id,
            contract_id=contract_id,
            meta=dict(meta or {}),
        )
        self.picks_by_turn_index[turn_index] = dp
        self.cursor = turn_index + 1
        return dp

    def get_pick_history(self) -> List[DraftPick]:
        return [self.picks_by_turn_index[i] for i in sorted(self.picks_by_turn_index.keys())]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_year": int(self.draft_year),
            "cursor": int(self.cursor),
            "turns": [t.to_dict() for t in self.turns],
            "pool": self.pool.to_dict(),
            "picks": [p.to_dict() for p in self.get_pick_history()],
            "meta": dict(self.meta) if isinstance(self.meta, dict) else {},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DraftSession":
        dy = int(d.get("draft_year") or 0)
        turns = []
        for row in (d.get("turns") or []):
            if not isinstance(row, Mapping):
                continue
            from .types import DraftTurn
            turns.append(DraftTurn(
                round=int(row.get("round") or 0),
                slot=int(row.get("slot") or 0),
                overall_no=int(row.get("overall_no") or 0),
                pick_id=str(row.get("pick_id") or ""),
                original_team=str(row.get("original_team") or ""),
                drafting_team=str(row.get("drafting_team") or ""),
                attrs=dict(row.get("attrs") or {}) if isinstance(row.get("attrs"), Mapping) else {},
            ))
        pool = DraftPool.from_dict(d.get("pool") or {})
        session = cls(
            draft_year=dy,
            turns=turns,
            pool=pool,
            cursor=int(d.get("cursor") or 0),
            picks_by_turn_index={},
            meta=dict(d.get("meta") or {}) if isinstance(d.get("meta"), Mapping) else {},
        )
        # replay picks (preserves cursor if consistent)
        for p in (d.get("picks") or []):
            if not isinstance(p, Mapping):
                continue
            dp = DraftPick.from_dict(p)
            # locate turn index by overall_no
            idx = None
            for i, t in enumerate(turns):
                if int(t.overall_no) == int(dp.overall_no):
                    idx = i
                    break
            if idx is not None:
                session.picks_by_turn_index[int(idx)] = dp
                # ensure pool is marked
                if session.pool.is_available(dp.prospect_temp_id):
                    session.pool.mark_picked(dp.prospect_temp_id)
        return session
