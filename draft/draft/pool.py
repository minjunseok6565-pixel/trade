from __future__ import annotations

"""Draft prospect pool (in-memory).

MVP scope:
  - represent prospects with a stable temp_id and basic attributes
  - allow deterministic pool generation for tests / sandbox
  - support marking a prospect as drafted

This module is intentionally DB-agnostic.
"""

import random
import string
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .types import TeamId


@dataclass(frozen=True, slots=True)
class Prospect:
    temp_id: str
    name: str
    pos: str
    age: int
    height_in: int
    weight_lb: int
    ovr: int
    attrs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "temp_id": str(self.temp_id),
            "name": str(self.name),
            "pos": str(self.pos),
            "age": int(self.age),
            "height_in": int(self.height_in),
            "weight_lb": int(self.weight_lb),
            "ovr": int(self.ovr),
            "attrs": dict(self.attrs) if isinstance(self.attrs, dict) else {},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Prospect":
        a = dict(d.get("attrs") or {}) if isinstance(d, Mapping) else {}
        return cls(
            temp_id=str(d.get("temp_id") or ""),
            name=str(d.get("name") or "Unknown"),
            pos=str(d.get("pos") or "G"),
            age=int(d.get("age") or 19),
            height_in=int(d.get("height_in") or 78),
            weight_lb=int(d.get("weight_lb") or 210),
            ovr=int(d.get("ovr") or 60),
            attrs=a if isinstance(a, dict) else {},
        )


@dataclass(slots=True)
class DraftPool:
    """A mutable container for prospects during a draft."""

    draft_year: int
    prospects_by_temp_id: Dict[str, Prospect]
    available_temp_ids: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.draft_year = int(self.draft_year)
        if not isinstance(self.prospects_by_temp_id, dict):
            self.prospects_by_temp_id = {}
        if not self.available_temp_ids:
            self.available_temp_ids = set(self.prospects_by_temp_id.keys())

    def list_available(self) -> List[Prospect]:
        ids = sorted(self.available_temp_ids)
        return [self.prospects_by_temp_id[i] for i in ids if i in self.prospects_by_temp_id]

    def get(self, temp_id: str) -> Prospect:
        tid = str(temp_id)
        if tid not in self.prospects_by_temp_id:
            raise KeyError(f"prospect not found: temp_id={temp_id}")
        return self.prospects_by_temp_id[tid]

    def is_available(self, temp_id: str) -> bool:
        return str(temp_id) in self.available_temp_ids

    def mark_picked(self, temp_id: str) -> None:
        tid = str(temp_id)
        if tid not in self.available_temp_ids:
            raise ValueError(f"prospect already picked or unavailable: temp_id={temp_id}")
        self.available_temp_ids.remove(tid)

    def unmark_picked(self, temp_id: str) -> None:
        tid = str(temp_id)
        if tid in self.prospects_by_temp_id:
            self.available_temp_ids.add(tid)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_year": int(self.draft_year),
            "prospects": [p.to_dict() for p in self.prospects_by_temp_id.values()],
            "available_temp_ids": sorted(self.available_temp_ids),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DraftPool":
        dy = int(d.get("draft_year") or 0)
        prospects = {}
        for row in (d.get("prospects") or []):
            if not isinstance(row, Mapping):
                continue
            p = Prospect.from_dict(row)
            if p.temp_id:
                prospects[p.temp_id] = p
        avail = set(d.get("available_temp_ids") or prospects.keys())
        return cls(draft_year=dy, prospects_by_temp_id=prospects, available_temp_ids=avail)


def _random_name(rng: random.Random) -> str:
    first = rng.choice(["Jae", "Min", "Jun", "Hyun", "Dae", "Sung", "Tae", "Woo", "Jay", "Kai"])
    last = rng.choice(["Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon", "Han", "Song"])
    return f"{first} {last}"


def generate_pool(
    *,
    draft_year: int,
    n: int = 90,
    rng_seed: int = 0,
) -> DraftPool:
    """Deterministic MVP pool generator.

    This is a placeholder generator until you plug in:
      - imported NCAA/intl dataset
      - scouting model
      - procedural archetype generator
    """
    rng = random.Random(int(rng_seed))
    dy = int(draft_year)
    positions = ["PG", "SG", "SF", "PF", "C"]
    prospects: Dict[str, Prospect] = {}
    for i in range(1, int(n) + 1):
        temp_id = f"R{dy}_{i:03d}"
        pos = rng.choice(positions)
        age = rng.randint(18, 23)
        # rough height/weight by pos
        if pos in ("PG", "SG"):
            height = rng.randint(71, 77)
            weight = rng.randint(170, 215)
        elif pos in ("SF", "PF"):
            height = rng.randint(76, 82)
            weight = rng.randint(200, 250)
        else:
            height = rng.randint(81, 87)
            weight = rng.randint(225, 285)
        ovr = int(max(40, min(85, rng.gauss(62, 8))))
        name = _random_name(rng)
        attrs = {
            "archetype": rng.choice(["Shooter", "Slasher", "Playmaker", "Defender", "Big"]),
            "potential": int(max(45, min(95, rng.gauss(70, 10)))),
        }
        prospects[temp_id] = Prospect(
            temp_id=temp_id,
            name=name,
            pos=pos,
            age=age,
            height_in=height,
            weight_lb=weight,
            ovr=ovr,
            attrs=attrs,
        )
    return DraftPool(draft_year=dy, prospects_by_temp_id=prospects, available_temp_ids=set(prospects.keys()))
