from __future__ import annotations

"""Draft standings utilities (pure).

This module computes team records from the league master schedule and provides
deterministic helpers for building a "worst -> best" ranking used by draft order.

Data source contract (aligned with state_modules/state_schedule.py):
  state_snapshot['league']['master_schedule']['games'] is a list of dict entries.
  Required keys per game entry: home_team_id, away_team_id, status
  For final games, home_score and away_score should be present and int-like.

Design:
 - No DB I/O here. This module is pure.
 - All randomness (tie-break order) is made deterministic using a stable seed,
   so results can be reproduced across processes.
"""

import hashlib
import random
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import ALL_TEAM_IDS

from .types import TeamId, TeamRecord, norm_team_id


def infer_active_team_ids(state_snapshot: Mapping[str, Any]) -> List[TeamId]:
    """Infer active team ids from a state snapshot, with a safe static fallback."""
    team_ids: set[str] = set()

    league = state_snapshot.get("league") if isinstance(state_snapshot, Mapping) else None
    if isinstance(league, Mapping):
        ms = league.get("master_schedule")
        if isinstance(ms, Mapping):
            by_team = ms.get("by_team")
            if isinstance(by_team, Mapping):
                for k in by_team.keys():
                    tid = norm_team_id(k)
                    if tid and tid != "FA":
                        team_ids.add(tid)

            games = ms.get("games")
            if isinstance(games, list):
                for g in games:
                    if not isinstance(g, Mapping):
                        continue
                    for k in ("home_team_id", "away_team_id"):
                        tid = norm_team_id(g.get(k))
                        if tid and tid != "FA":
                            team_ids.add(tid)

    teams = state_snapshot.get("teams")
    if isinstance(teams, Mapping):
        for k in teams.keys():
            tid = norm_team_id(k)
            if tid and tid != "FA":
                team_ids.add(tid)

    ui_cache = state_snapshot.get("ui_cache")
    if isinstance(ui_cache, Mapping):
        ui_teams = ui_cache.get("teams")
        if isinstance(ui_teams, Mapping):
            for k in ui_teams.keys():
                tid = norm_team_id(k)
                if tid and tid != "FA":
                    team_ids.add(tid)

    # Static roster fallback: ensures we always return 30 ids.
    for tid in ALL_TEAM_IDS:
        team_ids.add(norm_team_id(tid))

    return sorted(t for t in team_ids if t and t != "FA")


def compute_team_records_from_master_schedule(
    state_snapshot: Mapping[str, Any],
    *,
    team_ids: Optional[Sequence[TeamId]] = None,
    require_initialized_schedule: bool = True,
) -> Dict[TeamId, TeamRecord]:
    """Compute W/L and points from master_schedule final games.

    Parameters
    ----------
    state_snapshot:
        Usually state.export_full_state_snapshot() output.
    team_ids:
        Optional explicit team id list. If omitted, inferred from snapshot.
    require_initialized_schedule:
        If True, raises when master_schedule.games is missing/empty.

    Returns
    -------
    Dict[TeamId, TeamRecord]
        Records for all resolved teams.
    """
    league = state_snapshot.get("league", {}) if isinstance(state_snapshot, Mapping) else {}
    master_schedule = league.get("master_schedule", {}) if isinstance(league, Mapping) else {}
    games = master_schedule.get("games") or []

    if require_initialized_schedule and not games:
        raise RuntimeError(
            "Master schedule is not initialized. Expected state.startup_init_state() (or ensure_schedule_for_active_season) "
            "to run before computing draft standings."
        )

    resolved_team_ids = [
        norm_team_id(t) for t in (list(team_ids) if team_ids is not None else infer_active_team_ids(state_snapshot))
    ]
    resolved_team_ids = [t for t in resolved_team_ids if t and t != "FA"]

    acc: Dict[str, Dict[str, int]] = {tid: {"wins": 0, "losses": 0, "pf": 0, "pa": 0} for tid in resolved_team_ids}

    if not isinstance(games, list):
        games = []

    for g in games:
        if not isinstance(g, Mapping):
            continue
        if g.get("status") != "final":
            continue

        hid = norm_team_id(g.get("home_team_id"))
        aid = norm_team_id(g.get("away_team_id"))
        hs = g.get("home_score")
        a_s = g.get("away_score")
        if not hid or not aid or hs is None or a_s is None:
            continue
        try:
            hs_i = int(hs)
            as_i = int(a_s)
        except Exception:
            continue

        for tid in (hid, aid):
            if tid not in acc and tid and tid != "FA":
                acc[tid] = {"wins": 0, "losses": 0, "pf": 0, "pa": 0}

        acc[hid]["pf"] += hs_i
        acc[hid]["pa"] += as_i
        acc[aid]["pf"] += as_i
        acc[aid]["pa"] += hs_i

        if hs_i > as_i:
            acc[hid]["wins"] += 1
            acc[aid]["losses"] += 1
        elif as_i > hs_i:
            acc[aid]["wins"] += 1
            acc[hid]["losses"] += 1
        # ties (rare) are ignored

    return {
        tid: TeamRecord(
            team_id=tid,
            wins=row.get("wins", 0),
            losses=row.get("losses", 0),
            pf=row.get("pf", 0),
            pa=row.get("pa", 0),
        )
        for tid, row in acc.items()
    }


def group_teams_by_win_fraction(
    records: Mapping[TeamId, TeamRecord],
    *,
    include_teams: Optional[Iterable[TeamId]] = None,
) -> List[Tuple[Fraction, List[TeamId]]]:
    """Group teams into tie groups by exact win fraction.

    Returns a list of (win_fraction, team_id_list) sorted from worst to best.
    """
    team_ids = list(records.keys()) if include_teams is None else [t for t in include_teams]

    buckets: Dict[Fraction, List[TeamId]] = {}
    for tid in team_ids:
        rec = records.get(norm_team_id(tid))
        if rec is None:
            continue
        buckets.setdefault(rec.win_fraction, []).append(rec.team_id)

    groups = sorted(buckets.items(), key=lambda kv: kv[0])  # worst -> best
    for _, ids in groups:
        ids.sort()  # deterministic baseline
    return groups


def _stable_int_seed(*parts: str) -> int:
    """Cross-process stable seed (avoid Python's randomized hash())."""
    h = hashlib.md5(":".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def rank_teams_worst_to_best(
    records: Mapping[TeamId, TeamRecord],
    *,
    tie_break_seed: Optional[int] = None,
    include_teams: Optional[Iterable[TeamId]] = None,
) -> List[TeamId]:
    """Return team ids sorted from worst -> best by win percentage.

    Tie handling:
      - Teams with identical win% are grouped.
      - If tie_break_seed is provided, each tie group is shuffled deterministically.
        (NBA style "coin flip" ordering within tie groups.)
      - If tie_break_seed is None, ordering within group is alphabetical.
    """
    groups = group_teams_by_win_fraction(records, include_teams=include_teams)
    out: List[TeamId] = []
    for frac, ids in groups:
        ids2 = list(ids)
        if tie_break_seed is not None and len(ids2) > 1:
            sub_seed = _stable_int_seed(str(int(tie_break_seed)), f"{frac.numerator}/{frac.denominator}")
            rng = random.Random(sub_seed)
            rng.shuffle(ids2)
        out.extend(ids2)
    return out


def rank_teams_best_to_worst(
    records: Mapping[TeamId, TeamRecord],
    *,
    tie_break_seed: Optional[int] = None,
    include_teams: Optional[Iterable[TeamId]] = None,
) -> List[TeamId]:
    """Return team ids sorted from best -> worst by win percentage."""
    return list(
        reversed(rank_teams_worst_to_best(records, tie_break_seed=tie_break_seed, include_teams=include_teams))
    )
