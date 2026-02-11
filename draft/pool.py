from __future__ import annotations

"""Draft prospect pool (DB-backed).

Source of truth:
  - college_draft_entries (declared prospects)
  - college_players (bio/ratings)
  - college_player_season_stats (season performance)
  - college_teams (display metadata)
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set


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
    ranked_temp_ids: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.draft_year = int(self.draft_year)
        if not isinstance(self.prospects_by_temp_id, dict):
            self.prospects_by_temp_id = {}
        if not self.available_temp_ids:
            self.available_temp_ids = set(self.prospects_by_temp_id.keys())
        if not self.ranked_temp_ids:
            # Preserve insertion order as default ranking.
            self.ranked_temp_ids = list(self.prospects_by_temp_id.keys())

    def list_available(self) -> List[Prospect]:
        # Prefer ranked order (stable, user-friendly) while filtering for availability.
        out: List[Prospect] = []
        if self.ranked_temp_ids:
            for tid in self.ranked_temp_ids:
                if tid in self.available_temp_ids and tid in self.prospects_by_temp_id:
                    out.append(self.prospects_by_temp_id[tid])
            if out:
                return out
        # Fallback: deterministic ordering.
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
        # Serialize prospects in ranked order if possible (for stable UI + replay).
        if self.ranked_temp_ids:
            prospects_list = [
                self.prospects_by_temp_id[tid].to_dict()
                for tid in self.ranked_temp_ids
                if tid in self.prospects_by_temp_id
            ]
        else:
            prospects_list = [p.to_dict() for p in self.prospects_by_temp_id.values()]
        return {
            "draft_year": int(self.draft_year),
            "prospects": prospects_list,
            "available_temp_ids": sorted(self.available_temp_ids),
            "ranked_temp_ids": list(self.ranked_temp_ids),
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
        ranked = list(d.get("ranked_temp_ids") or [])
        if not ranked:
            ranked = list(prospects.keys())
        return cls(draft_year=dy, prospects_by_temp_id=prospects, available_temp_ids=avail, ranked_temp_ids=ranked)


def _json_loads(value: Any, default: Any) -> Any:
    """Best-effort json.loads helper (accepts str/bytes/dict/list)."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        s = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)
        if not s:
            return default
        return json.loads(s)
    except Exception:
        return default


def load_pool_from_db(
    *,
    db_path: str,
    draft_year: int,
    season_year: Optional[int] = None,
    limit: Optional[int] = None,
) -> DraftPool:
    """Load declared prospects from DB and build a DraftPool.

    - Uses temp_id == player_id (college player ids are the stable identifier).
    - Pulls season stats from (season_year) which defaults to (draft_year - 1).
    """
    dy = int(draft_year)
    if dy <= 0:
        raise ValueError(f"invalid draft_year: {draft_year}")
    sy = int(season_year) if season_year is not None else (dy - 1)
    if sy <= 0:
        raise ValueError(f"invalid season_year: {season_year}")
    lim = int(limit) if limit is not None else None
    if lim is not None and lim <= 0:
        raise ValueError(f"invalid limit: {limit}")

    # Local import to avoid heavy deps / cycles at import time.
    from league_repo import LeagueRepo

    sql = """
SELECT
  e.player_id              AS player_id,
  e.declared_at            AS declared_at,
  e.decision_json          AS decision_json,

  p.college_team_id        AS college_team_id,
  p.class_year             AS class_year,
  p.entry_season_year      AS entry_season_year,
  p.status                 AS status,

  p.name                   AS name,
  p.pos                    AS pos,
  p.age                    AS age,
  p.height_in              AS height_in,
  p.weight_lb              AS weight_lb,
  p.ovr                    AS ovr,
  p.attrs_json             AS attrs_json,

  ps.stats_json            AS stats_json,

  t.name                   AS college_team_name,
  t.conference             AS conference
FROM college_draft_entries e
JOIN college_players p
  ON p.player_id = e.player_id
LEFT JOIN college_player_season_stats ps
  ON ps.player_id = e.player_id
 AND ps.season_year = ?
LEFT JOIN college_teams t
  ON t.college_team_id = p.college_team_id
WHERE e.draft_year = ?;
""".strip()

    with LeagueRepo(str(db_path)) as repo:
        repo.init_db()
        rows = repo._conn.execute(sql, (sy, dy)).fetchall()

    if not rows:
        raise ValueError(
            f"No declared prospects found for draft_year={dy}. "
            f"Run college.finalize_season_and_generate_entries(season_year={sy}, draft_year={dy}) first."
        )

    scored: List[tuple] = []
    for r in rows:
        player_id = str(r["player_id"] or "").strip()
        if not player_id:
            continue

        name = str(r["name"] or "Unknown")
        pos = str(r["pos"] or "G")
        age = int(r["age"] or 19)
        height_in = int(r["height_in"] or 78)
        weight_lb = int(r["weight_lb"] or 210)
        ovr = int(r["ovr"] or 60)

        profile = _json_loads(r["attrs_json"], default={})
        if not isinstance(profile, dict):
            profile = {}

        decision_trace = _json_loads(r["decision_json"], default={})
        if not isinstance(decision_trace, dict):
            decision_trace = {}

        season_stats = _json_loads(r["stats_json"], default=None)
        if season_stats is not None and not isinstance(season_stats, dict):
            season_stats = None

        # Potential: prefer stored value, else a conservative fallback.
        try:
            potential = int(profile.get("potential") or 0)
        except Exception:
            potential = 0
        if potential <= 0:
            potential = max(int(ovr), 65)

        college_team_id = str(r["college_team_id"] or "")
        college_team_name = str(r["college_team_name"] or "")
        conference = str(r["conference"] or "")

        try:
            class_year = int(r["class_year"] or 1)
        except Exception:
            class_year = 1
        try:
            entry_season_year = int(r["entry_season_year"] or 0)
        except Exception:
            entry_season_year = 0

        attrs: Dict[str, Any] = {
            "potential": int(potential),
            "profile": dict(profile),
            "college": {
                "college_team_id": college_team_id,
                "college_team_name": college_team_name,
                "conference": conference,
                "class_year": int(class_year),
                "entry_season_year": int(entry_season_year),
                "status": str(r["status"] or ""),
                "declared_at": str(r["declared_at"] or ""),
            },
            "decision_trace": dict(decision_trace),
            "season_stats": season_stats,
        }

        p = Prospect(
            temp_id=player_id,
            name=name,
            pos=pos,
            age=age,
            height_in=height_in,
            weight_lb=weight_lb,
            ovr=ovr,
            attrs=attrs,
        )

        proj = decision_trace.get("projected_pick")
        try:
            proj_i = int(proj) if proj is not None else 9999
        except Exception:
            proj_i = 9999

        # Sort: projected_pick asc, ovr desc, potential desc, age asc, temp_id asc
        sort_key = (proj_i, -int(ovr), -int(potential), int(age), player_id)
        scored.append((sort_key, p))

    if not scored:
        raise ValueError(
            f"No usable declared prospects found for draft_year={dy}. "
            f"Run college.finalize_season_and_generate_entries(season_year={sy}, draft_year={dy}) first."
        )

    scored.sort(key=lambda x: x[0])
    if lim is not None:
        scored = scored[:lim]

    prospects_by_temp_id: Dict[str, Prospect] = {}
    ranked_temp_ids: List[str] = []
    for _, p in scored:
        if not p.temp_id or p.temp_id in prospects_by_temp_id:
            continue
        prospects_by_temp_id[p.temp_id] = p
        ranked_temp_ids.append(p.temp_id)

    if not prospects_by_temp_id:
        raise ValueError(
            f"No usable declared prospects found for draft_year={dy}. "
            f"Run college.finalize_season_and_generate_entries(season_year={sy}, draft_year={dy}) first."
        )

    return DraftPool(
        draft_year=dy,
        prospects_by_temp_id=prospects_by_temp_id,
        available_temp_ids=set(prospects_by_temp_id.keys()),
        ranked_temp_ids=ranked_temp_ids,
    )
