from __future__ import annotations

"""
assets.py

Draft picks (and future asset types) live here.

Design goals:
- Store assets in GAME_STATE so they persist.
- Provide stable IDs for assets (pick_id) so trades can reference them.
- Keep protection representation simple-but-extensible.

This is an MVP draft. You will likely extend:
- swaps, multi-year protections, convey/rollover rules
- Stepien rule / pick trading restrictions
- conditional second-rounders, cash considerations (if your game supports it)
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
import re

try:
    from state import GAME_STATE, _ensure_league_state
except Exception:  # pragma: no cover
    GAME_STATE: Dict[str, Any] = {"league": {}, "players": {}, "teams": {}, "transactions": []}

    def _ensure_league_state() -> Dict[str, Any]:
        return GAME_STATE.setdefault("league", {})

# Optional: config provides authoritative team ids (preferred)
def _all_team_ids() -> List[str]:
    try:
        from config import ALL_TEAM_IDS  # type: ignore
        return list(ALL_TEAM_IDS)
    except Exception:
        teams = GAME_STATE.get("teams")
        if isinstance(teams, dict) and teams:
            return sorted(list(teams.keys()))
        return []


_ASSETS_KEY = "assets"
_PICKS_KEY = "draft_picks"


@dataclass(frozen=True)
class DraftPick:
    """
    A tradable draft pick.

    pick_id: stable identifier, stays the same even if current_owner changes
    season_year: draft year (e.g., 2026 draft)
    round: 1 or 2
    original_owner: team that originally "generated" this pick
    current_owner: team that currently owns the pick
    protection: optional dict (e.g., {"type":"top_n","n":10,"conveys_to":"unprotected"})
    """
    pick_id: str
    season_year: int
    round: int
    original_owner: str
    current_owner: str
    protection: Optional[Dict[str, Any]] = None
    notes: str = ""


def _assets_store() -> Dict[str, Any]:
    return GAME_STATE.setdefault(_ASSETS_KEY, {})


def _picks_store() -> Dict[str, Dict[str, Any]]:
    assets = _assets_store()
    picks = assets.setdefault(_PICKS_KEY, {})
    if isinstance(picks, dict):
        return picks  # type: ignore[return-value]
    assets[_PICKS_KEY] = {}
    return assets[_PICKS_KEY]  # type: ignore[return-value]


def _league_season_year() -> int:
    league = _ensure_league_state()
    y = league.get("season_year")
    if isinstance(y, int) and y > 0:
        return y
    try:
        from datetime import date
        return date.today().year
    except Exception:  # pragma: no cover
        return 2025


# ---- Pick ID helpers ----
_PICK_ID_RE = re.compile(r"^(?P<year>\d{4})_R(?P<round>[12])_(?P<orig>[A-Z]{2,4})(?:__(?P<tag>.+))?$")


def make_pick_id(season_year: int, round: int, original_owner: str, tag: Optional[str] = None) -> str:
    base = f"{int(season_year)}_R{int(round)}_{str(original_owner).upper()}"
    return f"{base}__{tag}" if tag else base


def parse_pick_id(pick_id: str) -> Optional[Dict[str, Any]]:
    m = _PICK_ID_RE.match(str(pick_id))
    if not m:
        return None
    d = m.groupdict()
    return {
        "season_year": int(d["year"]),
        "round": int(d["round"]),
        "original_owner": str(d["orig"]).upper(),
        "tag": d.get("tag"),
    }


# ---- CRUD ----
def get_pick(pick_id: str) -> Optional[DraftPick]:
    raw = _picks_store().get(str(pick_id))
    if not raw:
        return None
    try:
        return DraftPick(**raw)  # type: ignore[arg-type]
    except Exception:
        return None


def upsert_pick(pick: DraftPick) -> None:
    _picks_store()[str(pick.pick_id)] = asdict(pick)


def ensure_draft_picks_initialized(
    *,
    num_future_years: int = 7,
    include_second_round: bool = True,
    force: bool = False,
) -> None:
    """
    Create a baseline set of picks for every team for upcoming drafts.

    By default:
      - for each team, for each draft year in [season_year+1 .. season_year+num_future_years]
      - create R1 (and R2 if enabled) picks
      - all picks start unprotected (protection=None)
    """
    store = _picks_store()
    if store and not force:
        return

    store.clear()
    teams = _all_team_ids()
    if not teams:
        return

    base_year = _league_season_year()
    for draft_year in range(base_year + 1, base_year + 1 + int(num_future_years)):
        for tid in teams:
            # Round 1
            pid1 = make_pick_id(draft_year, 1, tid)
            upsert_pick(DraftPick(
                pick_id=pid1,
                season_year=draft_year,
                round=1,
                original_owner=tid,
                current_owner=tid,
                protection=None,
            ))
            # Round 2
            if include_second_round:
                pid2 = make_pick_id(draft_year, 2, tid)
                upsert_pick(DraftPick(
                    pick_id=pid2,
                    season_year=draft_year,
                    round=2,
                    original_owner=tid,
                    current_owner=tid,
                    protection=None,
                ))


def team_picks(team_id: str, *, only_owned: bool = True) -> List[DraftPick]:
    """Return picks where current_owner==team_id (default) or all picks involving team."""
    team_id = str(team_id).upper()
    picks: List[DraftPick] = []
    for raw in _picks_store().values():
        try:
            p = DraftPick(**raw)  # type: ignore[arg-type]
        except Exception:
            continue
        if only_owned:
            if str(p.current_owner).upper() == team_id:
                picks.append(p)
        else:
            if str(p.current_owner).upper() == team_id or str(p.original_owner).upper() == team_id:
                picks.append(p)
    picks.sort(key=lambda x: (x.season_year, x.round, x.original_owner))
    return picks


def transfer_pick(pick_id: str, new_owner: str, *, note: str = "") -> DraftPick:
    """
    Transfer ownership of a pick.
    This function only updates the asset record; trade_engine should handle logs/roster rules.
    """
    existing = get_pick(pick_id)
    if not existing:
        meta = parse_pick_id(pick_id)
        if not meta:
            raise KeyError(f"Unknown pick_id: {pick_id}")
        existing = DraftPick(
            pick_id=str(pick_id),
            season_year=int(meta["season_year"]),
            round=int(meta["round"]),
            original_owner=str(meta["original_owner"]),
            current_owner=str(meta["original_owner"]),
            protection=None,
        )

    updated = DraftPick(
        pick_id=existing.pick_id,
        season_year=existing.season_year,
        round=existing.round,
        original_owner=existing.original_owner,
        current_owner=str(new_owner).upper(),
        protection=existing.protection,
        notes=(note or existing.notes or ""),
    )
    upsert_pick(updated)
    return updated


def set_pick_protection(pick_id: str, protection: Optional[Dict[str, Any]]) -> DraftPick:
    """
    Attach or clear protection metadata.

    Example protection dicts (recommended convention):
      {"type":"top_n","n":10, "rollover": {"type":"unprotected", "year_offset":1}}
      {"type":"lottery"}
    """
    p = get_pick(pick_id)
    if not p:
        raise KeyError(f"Unknown pick_id: {pick_id}")
    updated = DraftPick(
        pick_id=p.pick_id,
        season_year=p.season_year,
        round=p.round,
        original_owner=p.original_owner,
        current_owner=p.current_owner,
        protection=protection,
        notes=p.notes,
    )
    upsert_pick(updated)
    return updated


def describe_pick(pick: DraftPick) -> str:
    prot = ""
    if pick.protection:
        t = str(pick.protection.get("type", "")).lower()
        if t == "top_n":
            prot = f" (Top-{pick.protection.get('n')} protected)"
        elif t:
            prot = f" ({t})"
    rnd = "1st" if pick.round == 1 else "2nd"
    return f"{pick.season_year} {rnd} (orig {pick.original_owner}){prot} -> owned by {pick.current_owner}"


# ---- Rule helpers (placeholders) ----
def is_stepien_legal(team_id: str, *, draft_year: int, outgoing_pick_ids: List[str]) -> bool:
    """
    Placeholder Stepien rule check.
    MVP: always return True.
    Later: disallow trading future 1st-rounders in consecutive years.
    """
    return True
