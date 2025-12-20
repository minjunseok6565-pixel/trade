from __future__ import annotations

"""assets.py

Draft picks (and future asset types) live here.

Design goals
- Store assets in GAME_STATE so they persist.
- Provide stable IDs for assets (pick_id) so trades can reference them.
- Provide a *normalized* protection representation.

Protection model (MVP+)
- Internally we normalize protections to a `chain` list:

    {
      "chain": [
        {"year": 2026, "type": "top_n", "n": 10},
        {"year": 2027, "type": "none"}
      ],
      "convert_to": {"year": 2028, "round": 2}  # optional
    }

- This makes valuation logic deterministic and easy to extend.
- We still accept legacy dict forms (e.g. {"type":"top_n","n":10,...}) and normalize them.

You will likely extend:
- swaps
- Stepien rule
- multi-hop protection chains (we currently support up to ~3 hops cleanly)
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
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
    """A tradable draft pick.

    pick_id: stable identifier, stays the same even if current_owner changes
    season_year: draft year (e.g., 2026 draft)
    round: 1 or 2
    original_owner: team that originally "generated" this pick
    current_owner: team that currently owns the pick
    protection: normalized protection dict (see module docstring) or None
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


# ---------------------------------------------------------------------
# Protection normalization
# ---------------------------------------------------------------------

def normalize_protection(protection: Optional[Dict[str, Any]], *, base_year: int) -> Optional[Dict[str, Any]]:
    """Normalize a protection dict into the canonical `chain` format.

    Accepts legacy forms:
    - None
    - {"type":"top_n","n":10}
    - {"type":"top_n","n":10,"rollover": {"type":"none","year_offset":1}}
    - {"chain":[...], "convert_to": {...}}

    Returns None or a normalized dict with at least `chain`.
    """

    if not protection:
        return None

    if not isinstance(protection, dict):
        return {"chain": [{"year": int(base_year), "type": "none"}]}

    # Already normalized
    if isinstance(protection.get("chain"), list):
        chain_in = protection.get("chain") or []
        chain: List[Dict[str, Any]] = []
        for step in chain_in:
            if not isinstance(step, dict):
                continue
            year = int(step.get("year", base_year) or base_year)
            t = str(step.get("type", "none") or "none").lower()
            if t in ("unprotected", "none", "unprot"):
                chain.append({"year": year, "type": "none"})
            elif t == "top_n":
                n = int(step.get("n", 10) or 10)
                n = max(1, min(30, n))
                chain.append({"year": year, "type": "top_n", "n": n})
            else:
                chain.append({"year": year, "type": t})

        if not chain:
            chain = [{"year": int(base_year), "type": "none"}]

        out: Dict[str, Any] = {"chain": chain}
        if isinstance(protection.get("convert_to"), dict):
            out["convert_to"] = dict(protection["convert_to"])
        if isinstance(protection.get("notes"), str) and protection.get("notes"):
            out["notes"] = str(protection.get("notes"))
        return out

    # Legacy {type: ...}
    t = str(protection.get("type", "none") or "none").lower()
    chain: List[Dict[str, Any]] = []

    if t in ("unprotected", "none", "unprot", ""):
        chain = [{"year": int(base_year), "type": "none"}]
    elif t == "top_n":
        n = int(protection.get("n", 10) or 10)
        n = max(1, min(30, n))
        chain.append({"year": int(base_year), "type": "top_n", "n": n})

        # Rollover / conveys_to (optional)
        rollover = protection.get("rollover")
        conveys_to = protection.get("conveys_to")

        if isinstance(rollover, dict):
            yoff = int(rollover.get("year_offset", 1) or 1)
            rt = str(rollover.get("type", "none") or "none").lower()
            if rt in ("unprotected", "none", "unprot"):
                chain.append({"year": int(base_year) + yoff, "type": "none"})
            elif rt == "top_n":
                rn = int(rollover.get("n", 10) or 10)
                rn = max(1, min(30, rn))
                chain.append({"year": int(base_year) + yoff, "type": "top_n", "n": rn})
        elif isinstance(conveys_to, str) and conveys_to:
            # Most common: conveys_to="unprotected" the next year
            chain.append({"year": int(base_year) + 1, "type": "none"})

    else:
        # Unknown type (keep but mark)
        chain = [{"year": int(base_year), "type": t}]

    out2: Dict[str, Any] = {"chain": chain}
    if isinstance(protection.get("convert_to"), dict):
        out2["convert_to"] = dict(protection["convert_to"])
    if isinstance(protection.get("notes"), str) and protection.get("notes"):
        out2["notes"] = str(protection.get("notes"))
    return out2


def protection_chain(pick: DraftPick) -> List[Dict[str, Any]]:
    """Return the canonical protection chain for a pick.

    If pick.protection is None, returns [{"year": pick.season_year, "type": "none"}].
    """

    prot = normalize_protection(pick.protection, base_year=int(pick.season_year))
    if not prot:
        return [{"year": int(pick.season_year), "type": "none"}]
    chain = prot.get("chain")
    if isinstance(chain, list) and chain:
        # Ensure sorted by year
        chain2 = [dict(x) for x in chain if isinstance(x, dict)]
        chain2.sort(key=lambda s: int(s.get("year", pick.season_year)))
        return chain2
    return [{"year": int(pick.season_year), "type": "none"}]


# ---------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------

def get_pick(pick_id: str) -> Optional[DraftPick]:
    raw = _picks_store().get(str(pick_id))
    if not raw:
        return None
    try:
        p = DraftPick(**raw)  # type: ignore[arg-type]
        # Normalize on the fly (do NOT mutate store here; keep idempotent)
        prot = normalize_protection(p.protection, base_year=int(p.season_year))
        return DraftPick(
            pick_id=p.pick_id,
            season_year=p.season_year,
            round=p.round,
            original_owner=str(p.original_owner).upper(),
            current_owner=str(p.current_owner).upper(),
            protection=prot,
            notes=p.notes,
        )
    except Exception:
        return None


def upsert_pick(pick: DraftPick) -> None:
    # Ensure protection normalized before storing
    prot = normalize_protection(pick.protection, base_year=int(pick.season_year))
    _picks_store()[str(pick.pick_id)] = asdict(
        DraftPick(
            pick_id=str(pick.pick_id),
            season_year=int(pick.season_year),
            round=int(pick.round),
            original_owner=str(pick.original_owner).upper(),
            current_owner=str(pick.current_owner).upper(),
            protection=prot,
            notes=str(pick.notes or ""),
        )
    )


def ensure_draft_picks_initialized(
    *,
    num_future_years: int = 7,
    include_second_round: bool = True,
    force: bool = False,
) -> None:
    """Create a baseline set of picks for every team for upcoming drafts."""

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
            tid_u = str(tid).upper()
            # Round 1
            pid1 = make_pick_id(draft_year, 1, tid_u)
            upsert_pick(
                DraftPick(
                    pick_id=pid1,
                    season_year=draft_year,
                    round=1,
                    original_owner=tid_u,
                    current_owner=tid_u,
                    protection=None,
                )
            )
            # Round 2
            if include_second_round:
                pid2 = make_pick_id(draft_year, 2, tid_u)
                upsert_pick(
                    DraftPick(
                        pick_id=pid2,
                        season_year=draft_year,
                        round=2,
                        original_owner=tid_u,
                        current_owner=tid_u,
                        protection=None,
                    )
                )


def team_picks(team_id: str, *, only_owned: bool = True) -> List[DraftPick]:
    """Return picks where current_owner==team_id (default) or all picks involving team."""

    team_id_u = str(team_id).upper()
    picks: List[DraftPick] = []
    for raw in _picks_store().values():
        try:
            p = DraftPick(**raw)  # type: ignore[arg-type]
        except Exception:
            continue
        if only_owned:
            if str(p.current_owner).upper() == team_id_u:
                picks.append(get_pick(p.pick_id) or p)
        else:
            if str(p.current_owner).upper() == team_id_u or str(p.original_owner).upper() == team_id_u:
                picks.append(get_pick(p.pick_id) or p)
    picks.sort(key=lambda x: (x.season_year, x.round, x.original_owner))
    return picks


def transfer_pick(pick_id: str, new_owner: str, *, note: str = "") -> DraftPick:
    """Transfer ownership of a pick."""

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
        original_owner=str(existing.original_owner).upper(),
        current_owner=str(new_owner).upper(),
        protection=existing.protection,
        notes=(note or existing.notes or ""),
    )
    upsert_pick(updated)
    return updated


def set_pick_protection(pick_id: str, protection: Optional[Dict[str, Any]]) -> DraftPick:
    """Attach or clear protection metadata."""

    p = get_pick(pick_id)
    if not p:
        raise KeyError(f"Unknown pick_id: {pick_id}")
    updated = DraftPick(
        pick_id=p.pick_id,
        season_year=p.season_year,
        round=p.round,
        original_owner=p.original_owner,
        current_owner=p.current_owner,
        protection=normalize_protection(protection, base_year=int(p.season_year)),
        notes=p.notes,
    )
    upsert_pick(updated)
    return updated


def describe_pick(pick: DraftPick) -> str:
    chain = protection_chain(pick)
    prot = ""
    if chain:
        first = chain[0]
        t = str(first.get("type", "none") or "none").lower()
        if t == "top_n":
            prot = f" (Top-{first.get('n')} protected)"
        elif t and t != "none":
            prot = f" ({t})"

    rnd = "1st" if int(pick.round) == 1 else "2nd"
    return f"{int(pick.season_year)} {rnd} (orig {str(pick.original_owner).upper()}){prot} -> owned by {str(pick.current_owner).upper()}"


# ---- Rule helpers (placeholders) ----

def is_stepien_legal(team_id: str, *, draft_year: int, outgoing_pick_ids: List[str]) -> bool:
    """Placeholder Stepien rule check. MVP: always return True."""

    return True
