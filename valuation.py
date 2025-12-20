from __future__ import annotations

"""valuation.py

Trade valuation layer.

Key principle
- valuation.py produces numeric "utility/value" signals.
- trade_engine.py uses those values + rules to accept/decline/iterate offers.

This module intentionally uses clear, debuggable heuristics. You'll tune it over time.

This revision adds:
- GM profiles (team meta -> gm_profile dict) and relationships scaffold
- draft pick valuation using a probability distribution + protection "chain"

Notes
- We try to avoid mutating state in valuation, but we *do* setdefault missing GM/relationship
  fields because other modules depend on them being present.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import math
import random
import zlib

try:
    from state import GAME_STATE, _ensure_league_state
except Exception:  # pragma: no cover
    GAME_STATE: Dict[str, Any] = {"league": {}, "players": {}, "teams": {}}

    def _ensure_league_state() -> Dict[str, Any]:
        return GAME_STATE.setdefault("league", {})

from contracts import ensure_contracts_initialized, salary_of_player, get_contract
from assets import DraftPick, get_pick, ensure_draft_picks_initialized, protection_chain


# ---------------------------------------------------------------------
# Context models
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class MarketContext:
    """League-wide modifiers (scarcity, deadline, etc.)."""

    deadline_pressure: float = 0.0  # 0..1 (0=none, 1=deadline day)
    scarcity_by_role: Optional[Dict[str, float]] = None


@dataclass(frozen=True)
class TeamContext:
    team_id: str
    status: str  # contender | neutral | rebuild
    win_pct: float
    games_played: int
    point_diff: float
    need_positions: Tuple[str, ...]
    surplus_positions: Tuple[str, ...]
    gm_profile: Dict[str, Any]


@dataclass(frozen=True)
class PlayerContext:
    player_id: int
    name: str
    team_id: str
    pos: str
    age: int
    overall: float
    potential: float
    salary: float
    years_left: int


# ---------------------------------------------------------------------
# GM + relationship scaffolding
# ---------------------------------------------------------------------

_DEFAULT_GM_KEYS = (
    "aggressiveness",
    "risk_aversion",
    "pick_hoarder",
    "youth_bias",
    "hardball",
    "rival_penalty",
    "trust_baseline",
)


def _stable_team_seed(team_id: str, *, global_seed: int = 0) -> int:
    tid = str(team_id or "").upper().encode("utf-8")
    return int(global_seed) ^ int(zlib.crc32(tid) & 0xFFFFFFFF)


def ensure_gm_profiles_initialized(*, force: bool = False) -> None:
    """Ensure GAME_STATE['teams'][tid]['gm_profile'] exists.

    Creates deterministic pseudo-random profiles using league rng_seed (if any).
    """

    teams = GAME_STATE.get("teams")
    if not isinstance(teams, dict):
        return

    league = _ensure_league_state()
    global_seed = 0
    if league.get("rng_seed") is not None:
        try:
            global_seed = int(league.get("rng_seed") or 0)
        except Exception:
            global_seed = 0

    for tid, meta in teams.items():
        if not isinstance(meta, dict):
            continue
        if ("gm_profile" in meta) and isinstance(meta.get("gm_profile"), dict) and not force:
            # fill missing keys only
            prof = meta["gm_profile"]
            for k in _DEFAULT_GM_KEYS:
                prof.setdefault(k, 0.5)
            meta["gm_profile"] = prof
            continue

        rng = random.Random(_stable_team_seed(str(tid), global_seed=global_seed))
        # Values are 0..1 but not extreme by default.
        prof = {
            "aggressiveness": float(rng.uniform(0.25, 0.75)),
            "risk_aversion": float(rng.uniform(0.25, 0.75)),
            "pick_hoarder": float(rng.uniform(0.20, 0.80)),
            "youth_bias": float(rng.uniform(0.20, 0.80)),
            "hardball": float(rng.uniform(0.25, 0.75)),
            "rival_penalty": float(rng.uniform(0.30, 0.80)),
            "trust_baseline": float(rng.uniform(0.40, 0.60)),
        }
        # Keep any existing patience key in the top-level meta (team_utils uses it)
        meta.setdefault("patience", 0.5)
        meta["gm_profile"] = prof


def ensure_relationships_initialized(*, force: bool = False) -> None:
    """Ensure GAME_STATE['relationships'][A][B] entries exist for all teams."""

    ensure_gm_profiles_initialized()

    rel = GAME_STATE.setdefault("relationships", {})
    if not isinstance(rel, dict):
        GAME_STATE["relationships"] = {}
        rel = GAME_STATE["relationships"]

    teams = GAME_STATE.get("teams")
    if not isinstance(teams, dict) or not teams:
        return

    # rival heuristic: same division => rival
    div_map: Dict[str, str] = {}
    for tid, meta in teams.items():
        if isinstance(meta, dict) and meta.get("division"):
            div_map[str(tid).upper()] = str(meta.get("division"))

    for a in teams.keys():
        a_u = str(a).upper()
        rel.setdefault(a_u, {})
        if not isinstance(rel[a_u], dict):
            rel[a_u] = {}
        for b in teams.keys():
            b_u = str(b).upper()
            if a_u == b_u:
                continue
            if (not force) and isinstance(rel[a_u], dict) and isinstance(rel[a_u].get(b_u), dict):
                # fill missing keys only
                entry = rel[a_u][b_u]
                entry.setdefault("trust", float(teams[a].get("gm_profile", {}).get("trust_baseline", 0.5)))
                entry.setdefault("trade_count", 0)
                entry.setdefault("last_trade_date", None)
                entry.setdefault("rival", bool(div_map.get(a_u) and div_map.get(a_u) == div_map.get(b_u)))
                continue

            trust0 = float(teams[a].get("gm_profile", {}).get("trust_baseline", 0.5)) if isinstance(teams.get(a), dict) else 0.5
            rel[a_u][b_u] = {
                "trust": trust0,
                "trade_count": 0,
                "last_trade_date": None,
                "rival": bool(div_map.get(a_u) and div_map.get(a_u) == div_map.get(b_u)),
            }


def get_relationship(team_id: str, other_team_id: str) -> Dict[str, Any]:
    """Return relationship entry (ensuring it exists)."""

    ensure_relationships_initialized()
    rel = GAME_STATE.get("relationships")
    if not isinstance(rel, dict):
        return {"trust": 0.5, "trade_count": 0, "last_trade_date": None, "rival": False}
    a = str(team_id).upper()
    b = str(other_team_id).upper()
    a_map = rel.get(a)
    if not isinstance(a_map, dict):
        return {"trust": 0.5, "trade_count": 0, "last_trade_date": None, "rival": False}
    entry = a_map.get(b)
    if not isinstance(entry, dict):
        return {"trust": 0.5, "trade_count": 0, "last_trade_date": None, "rival": False}
    return entry


# ---------------------------------------------------------------------
# Building contexts
# ---------------------------------------------------------------------

def build_team_contexts(*, force_recompute: bool = False) -> Dict[str, TeamContext]:
    """Build per-team context once and reuse during a trade tick."""

    ensure_gm_profiles_initialized()

    # Use team_utils if available (records + needs)
    try:
        from team_utils import _init_players_and_teams_if_needed, _compute_team_records, _evaluate_team_needs

        _init_players_and_teams_if_needed()
        records = _compute_team_records()
        needs = _evaluate_team_needs(records)
    except Exception:
        records = {}
        needs = {}

    contexts: Dict[str, TeamContext] = {}
    teams_meta = GAME_STATE.get("teams") or {}
    if not isinstance(teams_meta, dict):
        return contexts

    for tid, meta in teams_meta.items():
        tid_u = str(tid).upper()
        need = needs.get(tid_u) or {}

        rec = records.get(tid_u) or {}
        wins = int(rec.get("wins", 0) or 0)
        losses = int(rec.get("losses", 0) or 0)
        gp = wins + losses
        win_pct = float(need.get("win_pct") or (wins / gp if gp else 0.0))
        pf = float(rec.get("pf", 0) or 0)
        pa = float(rec.get("pa", 0) or 0)
        point_diff = float(pf - pa)

        status = str(need.get("status") or (meta.get("tendency") if isinstance(meta, dict) else None) or "neutral")
        need_pos = tuple(need.get("need_positions") or ())
        surplus_pos = tuple(need.get("surplus_positions") or ())

        gm_profile = {}
        if isinstance(meta, dict):
            gp_meta = meta.get("gm_profile")
            if isinstance(gp_meta, dict):
                gm_profile = dict(gp_meta)
            else:
                gm_profile = {}

        contexts[tid_u] = TeamContext(
            team_id=tid_u,
            status=str(status).lower(),
            win_pct=float(win_pct),
            games_played=int(gp),
            point_diff=float(point_diff),
            need_positions=need_pos,
            surplus_positions=surplus_pos,
            gm_profile=gm_profile,
        )

    return contexts


def build_player_context(player_id: int) -> Optional[PlayerContext]:
    ensure_contracts_initialized()
    players = GAME_STATE.get("players") or {}
    meta = players.get(int(player_id)) if isinstance(players, dict) else None
    if not isinstance(meta, dict):
        return None

    c = get_contract(int(player_id))
    years_left = int(c.years_left) if c else 1

    return PlayerContext(
        player_id=int(player_id),
        name=str(meta.get("name", "")),
        team_id=str(meta.get("team_id", "")).upper(),
        pos=str(meta.get("pos", "")),
        age=int(meta.get("age", 0) or 0),
        overall=float(meta.get("overall", 0.0) or 0.0),
        potential=float(meta.get("potential", 0.6) or 0.6),
        salary=float(meta.get("salary", 0.0) or 0.0),
        years_left=years_left,
    )


# ---------------------------------------------------------------------
# Core valuation functions
# ---------------------------------------------------------------------

def value_player_for_team(
    player_id: int,
    receiving_team_id: str,
    *,
    team_contexts: Optional[Dict[str, TeamContext]] = None,
    market: Optional[MarketContext] = None,
    trade_partner_id: Optional[str] = None,
) -> float:
    """Return numeric value (higher = better) of player for receiving_team_id."""

    receiving_team_id = str(receiving_team_id).upper()
    ctxs = team_contexts or build_team_contexts()
    team_ctx = ctxs.get(receiving_team_id)
    if not team_ctx:
        team_ctx = TeamContext(
            team_id=receiving_team_id,
            status="neutral",
            win_pct=0.0,
            games_played=0,
            point_diff=0.0,
            need_positions=tuple(),
            surplus_positions=tuple(),
            gm_profile={},
        )

    pctx = build_player_context(int(player_id))
    if not pctx:
        return 0.0

    status = str(team_ctx.status or "neutral").lower()

    # --- Base basketball value ---
    v = float(pctx.overall)

    # --- Potential/age curves ---
    if status.startswith("cont"):
        v += pctx.potential * 4.0
        v -= abs(pctx.age - 28) * 0.6
    elif status.startswith("reb"):
        v += pctx.potential * 8.0
        v -= max(0, pctx.age - 24) * 0.9
    else:
        v += pctx.potential * 6.0
        v -= abs(pctx.age - 26) * 0.5

    # --- Contract value (very rough) ---
    salary = float(salary_of_player(pctx.player_id))
    years_left = max(0, int(pctx.years_left))
    cost_per_point = salary / max(1.0, pctx.overall)
    v -= (cost_per_point / 1_000_000.0) * 0.9

    if years_left <= 1:
        v += 1.5 if status.startswith("cont") else 0.5

    # --- Fit bonus ---
    pos_group = _pos_group(pctx.pos)
    if pos_group and pos_group in team_ctx.need_positions:
        v += 2.5
    if pos_group and pos_group in team_ctx.surplus_positions:
        v -= 1.0

    # --- GM personality nudge (still small; big levers live in trade_ai thresholds) ---
    patience = float(GAME_STATE.get("teams", {}).get(receiving_team_id, {}).get("patience", 0.5) or 0.5)  # type: ignore[arg-type]
    if status.startswith("cont"):
        v += (0.5 - patience) * 2.0

    # --- Market deadline pressure (small) ---
    if market:
        v *= (1.0 + 0.03 * float(market.deadline_pressure))

    return float(v)


# -------------------------
# Pick valuation (distribution + protection chain)
# -------------------------

def pick_position_distribution(
    original_team_ctx: TeamContext,
    *,
    draft_year: int,
) -> List[float]:
    """Return probability distribution over pick number 1..30 for original_team in draft_year."""

    # year_offset: 0 means next upcoming draft (league_season_year + 1)
    base_draft_year = _league_season_year() + 1
    year_offset = max(0, int(draft_year) - int(base_draft_year))

    status = str(original_team_ctx.status or "neutral").lower()
    tau = 2.0
    if status.startswith("cont"):
        tau = 3.0
    elif status.startswith("reb"):
        tau = 1.2

    wp = max(0.0, min(1.0, float(original_team_ctx.win_pct)))
    proj_win = 0.5 + (wp - 0.5) * math.exp(-float(year_offset) / max(0.5, tau))

    mu = 1.0 + proj_win * 29.0  # high win -> later pick (near 30)

    gp = max(0, int(original_team_ctx.games_played))
    early_bonus = max(0.0, (20 - gp) * 0.08)

    sigma = 3.5 + year_offset * 1.2 + early_bonus
    sigma = max(1.8, float(sigma))

    weights: List[float] = []
    for k in range(1, 31):
        z = (float(k) - mu) / sigma
        weights.append(math.exp(-0.5 * z * z))

    s = sum(weights)
    if s <= 0:
        return [1.0 / 30.0] * 30
    return [w / s for w in weights]


def _value_unprotected_from_dist(dist: List[float], *, round: int) -> float:
    """Expected pick value from a distribution over 1..30."""

    if not dist or len(dist) != 30:
        return 0.0

    v = 0.0
    for i, p in enumerate(dist):
        pick_num = i + 1
        v += float(p) * _pick_value_from_expected_number(pick_num, round=round)
    return float(v)


def _conditional_tail(dist: List[float], start_pick_num: int) -> List[float]:
    """Return conditional distribution for picks >= start_pick_num."""

    start_pick_num = max(1, min(30, int(start_pick_num)))
    idx = start_pick_num - 1
    tail = dist[idx:]
    s = sum(tail)
    if s <= 1e-12:
        # if tail is impossible, return a flat distribution on the tail domain
        n = len(tail)
        return [1.0 / max(1, n)] * n
    return [x / s for x in tail]


def value_pick_for_team(
    pick_id: str,
    receiving_team_id: str,
    *,
    team_contexts: Optional[Dict[str, TeamContext]] = None,
    market: Optional[MarketContext] = None,
    trade_partner_id: Optional[str] = None,
) -> float:
    """Value a draft pick for a receiving team.

    This version:
    - projects the original owner's pick range using a probability distribution
    - applies protection chains via expectation over convey probability
    - time-discounts future picks
    - scales for team direction and GM preferences
    """

    ensure_draft_picks_initialized()
    receiving_team_id = str(receiving_team_id).upper()

    pick = get_pick(pick_id)
    if not pick:
        return 0.0

    ctxs = team_contexts or build_team_contexts()
    recv_ctx = ctxs.get(receiving_team_id) or TeamContext(receiving_team_id, "neutral", 0.0, 0, 0.0, tuple(), tuple(), {})
    orig_ctx = ctxs.get(str(pick.original_owner).upper()) or TeamContext(str(pick.original_owner).upper(), "neutral", 0.5, 0, 0.0, tuple(), tuple(), {})

    chain = protection_chain(pick)
    if not chain:
        chain = [{"year": int(pick.season_year), "type": "none"}]

    # chain length penalty (very small, keeps multi-hop protection from being overvalued)
    chain_penalty = 0.97 ** max(0, len(chain) - 1)

    reach_prob = 1.0
    ev = 0.0

    for step in chain:
        year = int(step.get("year", pick.season_year) or pick.season_year)
        year_offset = max(0, year - (_league_season_year() + 1))
        dist = pick_position_distribution(orig_ctx, draft_year=year)
        disc = 0.92 ** year_offset

        t = str(step.get("type", "none") or "none").lower()
        if t in ("none", "unprotected", "unprot"):
            ev += reach_prob * disc * _value_unprotected_from_dist(dist, round=int(pick.round))
            reach_prob = 0.0
            break

        if t == "top_n":
            n = int(step.get("n", 10) or 10)
            n = max(1, min(30, n))
            p_protected = float(sum(dist[:n]))
            p_convey = max(0.0, 1.0 - p_protected)

            if p_convey > 1e-9:
                tail_cond = _conditional_tail(dist, n + 1)  # distribution over (n+1..30)
                # expand tail_cond back to len=30 for valuation helper
                tail_dist = [0.0] * n + tail_cond
                ev += reach_prob * p_convey * disc * _value_unprotected_from_dist(tail_dist, round=int(pick.round))

            reach_prob *= p_protected
            continue

        # Unknown types: apply conservative discount and end.
        ev += reach_prob * disc * _value_unprotected_from_dist(dist, round=int(pick.round)) * 0.75
        reach_prob = 0.0
        break

    # If never conveys, apply convert_to if present
    if reach_prob > 1e-9:
        convert_to = pick.protection.get("convert_to") if isinstance(pick.protection, dict) else None
        if isinstance(convert_to, dict):
            cy = int(convert_to.get("year", pick.season_year + 1) or (pick.season_year + 1))
            cr = int(convert_to.get("round", 2) or 2)
            year_offset = max(0, cy - (_league_season_year() + 1))
            disc = 0.92 ** year_offset
            # approximate converted pick as a late-ish pick in that round
            conv_val = _pick_value_from_expected_number(26, round=cr)
            ev += reach_prob * disc * conv_val
        else:
            ev += reach_prob * 0.0

    base = float(ev) * float(chain_penalty)

    # Team direction scaling
    status = str(recv_ctx.status or "neutral").lower()
    if status.startswith("reb"):
        base *= 1.25
    elif status.startswith("cont"):
        base *= 0.90

    # GM preferences (small but meaningful)
    pick_hoarder = float(recv_ctx.gm_profile.get("pick_hoarder", 0.5) or 0.5)
    youth_bias = float(recv_ctx.gm_profile.get("youth_bias", 0.5) or 0.5)
    base *= (1.0 + (pick_hoarder - 0.5) * 0.30)
    base *= (1.0 + (youth_bias - 0.5) * 0.12)

    # Deadline pressure: contenders value picks slightly less near deadline
    if market and status.startswith("cont"):
        base *= (1.0 - 0.05 * float(market.deadline_pressure))

    return float(base)


def package_value_for_team(
    receiving_team_id: str,
    *,
    player_ids: Optional[List[int]] = None,
    pick_ids: Optional[List[str]] = None,
    team_contexts: Optional[Dict[str, TeamContext]] = None,
    market: Optional[MarketContext] = None,
    trade_partner_id: Optional[str] = None,
) -> float:
    """Convenience: value a bundle of assets for a team."""

    total = 0.0
    for pid in (player_ids or []):
        total += value_player_for_team(pid, receiving_team_id, team_contexts=team_contexts, market=market, trade_partner_id=trade_partner_id)
    for pk in (pick_ids or []):
        total += value_pick_for_team(pk, receiving_team_id, team_contexts=team_contexts, market=market, trade_partner_id=trade_partner_id)
    return float(total)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

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


def _pos_group(pos: str) -> str:
    """Use the existing team_utils mapping if available; otherwise a simple fallback."""

    try:
        from team_utils import _position_group  # type: ignore

        return str(_position_group(pos))
    except Exception:
        p = str(pos).upper()
        if p in ("PG", "SG"):
            return "guard"
        if p in ("SF", "PF"):
            return "wing"
        return "big"


def _pick_value_from_expected_number(expected_pick: int, *, round: int = 1) -> float:
    """Convert expected pick number into a value signal."""

    n = max(1, min(30, int(expected_pick)))
    v = (31 - n) ** 1.15
    if int(round) == 2:
        v *= 0.35
    return float(v)
