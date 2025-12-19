from __future__ import annotations

"""
valuation.py

Trade valuation layer.

Key principle:
- valuation.py produces numeric "utility/value" signals.
- trade_engine.py (later) uses those values + rules to accept/decline/iterate offers.

This module is intentionally conservative: it uses simple heuristics with clear knobs,
and relies on external context when available.

Inputs it can consume:
- GAME_STATE["players"][pid]  (from team_utils._init_players_and_teams_if_needed)
- GAME_STATE["teams"][tid]    (tendency/window/market/patience)
- contracts.get_contract(pid) (years_left, salary curve, etc.)
- assets.get_pick(pick_id)    (draft pick metadata)
- team_utils._evaluate_team_needs(...) output (status/need_positions/surplus_positions)

You will likely expand:
- a real projection model for pick ranges
- better "fit" metrics using playstyle tags
- market scarcity and deadline dynamics
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import math

try:
    from state import GAME_STATE
except Exception:  # pragma: no cover
    GAME_STATE: Dict[str, Any] = {"league": {}, "players": {}, "teams": {}}

from contracts import ensure_contracts_initialized, salary_of_player, get_contract
from assets import DraftPick, get_pick, ensure_draft_picks_initialized


# -------------------------
# Context models (lightweight)
# -------------------------
@dataclass(frozen=True)
class MarketContext:
    """League-wide modifiers (scarcity, deadline, etc.)."""
    deadline_pressure: float = 0.0   # 0..1 (0=none, 1=deadline day)
    scarcity_by_role: Optional[Dict[str, float]] = None  # role -> premium multiplier


@dataclass(frozen=True)
class TeamContext:
    team_id: str
    status: str  # contender | neutral | rebuild
    win_pct: float
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


# -------------------------
# Building contexts
# -------------------------
def build_team_contexts(*, force_recompute: bool = False) -> Dict[str, TeamContext]:
    """
    Build per-team context once and reuse during a trade tick.

    Uses team_utils helpers when available; otherwise falls back to GAME_STATE meta.
    """
    # Ensure state is at least initialized
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
    for tid, meta in (teams_meta.items() if isinstance(teams_meta, dict) else []):
        tid_u = str(tid).upper()
        need = needs.get(tid_u) or {}
        status = str(need.get("status") or meta.get("tendency") or "neutral")
        win_pct = float(need.get("win_pct") or 0.0)
        need_pos = tuple(need.get("need_positions") or ())
        surplus_pos = tuple(need.get("surplus_positions") or ())
        gm_profile = dict(meta) if isinstance(meta, dict) else {}
        contexts[tid_u] = TeamContext(
            team_id=tid_u,
            status=status,
            win_pct=win_pct,
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


# -------------------------
# Core valuation functions
# -------------------------
def value_player_for_team(
    player_id: int,
    receiving_team_id: str,
    *,
    team_contexts: Optional[Dict[str, TeamContext]] = None,
    market: Optional[MarketContext] = None,
) -> float:
    """
    Returns a numeric value. Higher means more desirable for receiving_team_id.

    This is intentionally simple (MVP):
    - base: overall
    - add: potential (more for rebuild teams)
    - age curve: win-now prefers prime, rebuild prefers youth
    - contract penalty/bonus (salary vs ovr, years_left)
    - fit bonus if the player's position is in need_positions
    """
    receiving_team_id = str(receiving_team_id).upper()
    ctxs = team_contexts or build_team_contexts()
    team_ctx = ctxs.get(receiving_team_id)
    if not team_ctx:
        team_ctx = TeamContext(
            team_id=receiving_team_id,
            status="neutral",
            win_pct=0.0,
            need_positions=tuple(),
            surplus_positions=tuple(),
            gm_profile={},
        )

    pctx = build_player_context(int(player_id))
    if not pctx:
        return 0.0

    status = team_ctx.status

    # --- Base basketball value ---
    v = float(pctx.overall)

    # --- Potential/age curves ---
    if status == "contender":
        v += pctx.potential * 4.0
        # Prime preference: 26-30
        v -= abs(pctx.age - 28) * 0.6
    elif status == "rebuild":
        v += pctx.potential * 8.0
        # Youth preference: 19-24
        v -= max(0, pctx.age - 24) * 0.9
    else:
        v += pctx.potential * 6.0
        v -= abs(pctx.age - 26) * 0.5

    # --- Contract value (very rough) ---
    salary = float(salary_of_player(pctx.player_id))
    years_left = max(0, int(pctx.years_left))
    # "Cost per OVR point" proxy; higher is worse
    cost_per_point = salary / max(1.0, pctx.overall)
    v -= (cost_per_point / 1_000_000.0) * 0.9  # tune later
    # Expiring premium: contenders like expirings a bit (flexibility); rebuilders don't mind longer if young
    if years_left <= 1:
        v += 1.5 if status == "contender" else 0.5

    # --- Fit bonus ---
    pos_group = _pos_group(pctx.pos)
    if pos_group and pos_group in team_ctx.need_positions:
        v += 2.5
    if pos_group and pos_group in team_ctx.surplus_positions:
        v -= 1.0

    # --- GM personality nudge (uses existing team meta keys if present) ---
    patience = float(team_ctx.gm_profile.get("patience", 0.5) or 0.5)
    # Low patience teams overvalue immediate help
    if status == "contender":
        v += (0.5 - patience) * 2.0

    # --- Market deadline pressure (small) ---
    if market:
        v *= (1.0 + 0.03 * float(market.deadline_pressure))

    return float(v)


def value_pick_for_team(
    pick_id: str,
    receiving_team_id: str,
    *,
    team_contexts: Optional[Dict[str, TeamContext]] = None,
    market: Optional[MarketContext] = None,
) -> float:
    """
    Value a draft pick for a receiving team.
    Depends on:
    - original owner's outlook (win_pct/status) -> expected pick range
    - protection -> discount
    - time discount for far future
    - rebuild teams weight picks higher
    """
    ensure_draft_picks_initialized()
    receiving_team_id = str(receiving_team_id).upper()
    p = get_pick(pick_id)
    if not p:
        return 0.0

    ctxs = team_contexts or build_team_contexts()
    recv_ctx = ctxs.get(receiving_team_id)
    if not recv_ctx:
        recv_ctx = TeamContext(receiving_team_id, "neutral", 0.0, tuple(), tuple(), {})

    orig_ctx = ctxs.get(str(p.original_owner).upper())
    if not orig_ctx:
        # fallback: use whatever team meta is available
        orig_ctx = TeamContext(str(p.original_owner).upper(), "neutral", 0.5, tuple(), tuple(), {})

    expected_pick = _expected_pick_number_from_win_pct(orig_ctx.win_pct)
    base = _pick_value_from_expected_number(expected_pick, round=int(p.round))

    # Protection discount
    base *= _protection_multiplier(p.protection)

    # Time discount
    years_out = max(0, int(p.season_year) - (_league_season_year() + 1))
    base *= (0.92 ** years_out)

    # Team direction scaling (rebuild wants picks)
    if recv_ctx.status == "rebuild":
        base *= 1.25
    elif recv_ctx.status == "contender":
        base *= 0.90

    # Deadline pressure: contenders may pay more for "win-now" and less for picks
    if market and recv_ctx.status == "contender":
        base *= (1.0 - 0.05 * float(market.deadline_pressure))

    return float(base)


def package_value_for_team(
    receiving_team_id: str,
    *,
    player_ids: Optional[List[int]] = None,
    pick_ids: Optional[List[str]] = None,
    team_contexts: Optional[Dict[str, TeamContext]] = None,
    market: Optional[MarketContext] = None,
) -> float:
    """Convenience: value a bundle of assets for a team."""
    total = 0.0
    for pid in (player_ids or []):
        total += value_player_for_team(pid, receiving_team_id, team_contexts=team_contexts, market=market)
    for pk in (pick_ids or []):
        total += value_pick_for_team(pk, receiving_team_id, team_contexts=team_contexts, market=market)
    return float(total)


# -------------------------
# Internal helpers
# -------------------------
def _league_season_year() -> int:
    try:
        from state import _ensure_league_state
        league = _ensure_league_state()
        y = league.get("season_year")
        if isinstance(y, int) and y > 0:
            return y
    except Exception:
        pass
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
        if "G" in p and "F" in p:
            return "G"
        if p.startswith("P") or "C" in p:
            return "C"
        if "F" in p:
            return "F"
        return "G"


def _expected_pick_number_from_win_pct(win_pct: float) -> int:
    """
    Rough mapping: win_pct -> expected pick number (1..30).
    - win_pct 0.0 -> ~1
    - win_pct 0.5 -> ~15-16
    - win_pct 1.0 -> ~30
    """
    wp = max(0.0, min(1.0, float(win_pct)))
    # invert: better team => later pick
    pick = int(round(1 + wp * 29))
    return max(1, min(30, pick))


def _pick_value_from_expected_number(expected_pick: int, round: int = 1) -> float:
    """
    Convert expected pick number into a value signal.
    Higher value for earlier picks. Round 2 is discounted.
    """
    n = max(1, min(30, int(expected_pick)))
    # convex curve: top picks are disproportionately valuable
    v = (31 - n) ** 1.15
    if int(round) == 2:
        v *= 0.35
    return float(v)


def _protection_multiplier(protection: Optional[Dict[str, Any]]) -> float:
    """Very rough discount for protected/conditional picks."""
    if not protection:
        return 1.0
    t = str(protection.get("type", "")).lower()
    if t == "top_n":
        n = int(protection.get("n", 10) or 10)
        n = max(1, min(30, n))
        # more protection => less valuable
        return float(max(0.35, 1.0 - (n / 30.0) * 0.8))
    if t == "lottery":
        return 0.65
    if t == "heavily_protected":
        return 0.45
    return 0.75
