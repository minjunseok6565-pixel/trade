from __future__ import annotations

"""
trade_engine.py

Core trade validation + application ("the referee + transaction applier").

This module is designed to be:
- deterministic given inputs (no random here)
- reusable for BOTH AI trades and user-initiated trades
- tolerant of partially-initialized state (early dev)

It integrates with the existing codebase:
- config.ROSTER_DF: canonical roster table (Team column is the owner team_id)
- state.GAME_STATE: persistent state (players/teams/transactions)
- contracts.py: salary + payroll helpers
- assets.py: draft pick storage and ownership transfer

This is an MVP draft. You will likely extend:
- multi-team routing (3-team trades)
- more NBA-like salary matching bands
- Stepien rule, pick swaps, trade exceptions, hard-cap aprons
- "trade block"/untouchables, no-trade clauses
"""

from dataclasses import dataclass, asdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

# Optional imports to keep early-dev importability
try:
    from config import ROSTER_DF, HARD_CAP
except Exception:  # pragma: no cover
    ROSTER_DF = None  # type: ignore
    HARD_CAP = 0.0  # type: ignore

try:
    from state import GAME_STATE, _ensure_league_state
except Exception:  # pragma: no cover
    GAME_STATE: Dict[str, Any] = {"league": {}, "players": {}, "teams": {}, "transactions": [], "cached_views": {}}

    def _ensure_league_state() -> Dict[str, Any]:
        return GAME_STATE.setdefault("league", {})

try:
    from contracts import (
        ensure_contracts_initialized,
        salary_of_player,
        total_salary,
        team_payroll,
    )
except Exception:  # pragma: no cover
    def ensure_contracts_initialized(*args: Any, **kwargs: Any) -> None:
        return None

    def salary_of_player(player_id: int, *args: Any, **kwargs: Any) -> float:
        return 0.0

    def total_salary(player_ids: List[int], *args: Any, **kwargs: Any) -> float:
        return 0.0

    def team_payroll(team_id: str, *args: Any, **kwargs: Any) -> float:
        return 0.0

try:
    from assets import (
        ensure_draft_picks_initialized,
        get_pick,
        transfer_pick,
        describe_pick,
    )
except Exception:  # pragma: no cover
    def ensure_draft_picks_initialized(*args: Any, **kwargs: Any) -> None:
        return None

    def get_pick(pick_id: str) -> Optional[Dict[str, Any]]:
        return None

    def transfer_pick(pick_id: str, new_owner_team_id: str) -> Optional[Dict[str, Any]]:
        return None

    def describe_pick(pick: Dict[str, Any]) -> str:
        return str(pick)


# ---------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class TradeProposal:
    """Two-team trade proposal (MVP).

    Outgoing lists are from each team to the other.
    """
    team_a: str
    team_b: str
    send_a_players: List[int]
    send_a_picks: List[str]
    send_b_players: List[int]
    send_b_picks: List[str]
    date_str: Optional[str] = None  # YYYY-MM-DD


@dataclass
class TradeValidationResult:
    ok: bool
    reasons: List[str]
    # Extra computed fields for logging/UI
    payroll_a_before: float = 0.0
    payroll_b_before: float = 0.0
    payroll_a_after: float = 0.0
    payroll_b_after: float = 0.0
    out_a: float = 0.0
    out_b: float = 0.0


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _u(team_id: str) -> str:
    return str(team_id or "").upper().strip()


def _now_date_str() -> Optional[str]:
    league = _ensure_league_state()
    curr = league.get("current_date")
    if isinstance(curr, str) and curr:
        return curr
    return None


def _league_rules() -> Dict[str, Any]:
    league = _ensure_league_state()
    rules = league.setdefault("trade_rules", {})
    # Defaults (kept simple and game-friendly)
    hard_cap = float(rules.get("hard_cap", HARD_CAP or 0.0))
    rules.setdefault("hard_cap", hard_cap)

    # If you don't model a "soft cap" yet, we still need a reference point for salary matching.
    # Use a conservative default under the hard cap.
    rules.setdefault("soft_cap", float(rules.get("soft_cap", hard_cap * 0.85 if hard_cap else 0.0)))

    # Roster size bounds (NBA-ish, simplified)
    rules.setdefault("roster_min", int(rules.get("roster_min", 12)))
    rules.setdefault("roster_max", int(rules.get("roster_max", 15)))

    # Salary matching knobs (simplified)
    rules.setdefault("salary_match_pct_over_cap", float(rules.get("salary_match_pct_over_cap", 0.25)))  # 125%
    rules.setdefault("salary_match_bonus", float(rules.get("salary_match_bonus", 1_000_000.0)))  # +$1M buffer
    return rules


def _roster_count(team_id: str) -> int:
    if ROSTER_DF is None:
        return 0
    try:
        return int((ROSTER_DF["Team"].astype(str).str.upper() == _u(team_id)).sum())
    except Exception:
        return 0


def _player_team_from_roster(player_id: int) -> Optional[str]:
    if ROSTER_DF is None:
        return None
    try:
        if int(player_id) not in ROSTER_DF.index:
            return None
        t = ROSTER_DF.loc[int(player_id), "Team"]
        if isinstance(t, str):
            return _u(t)
        return _u(str(t))
    except Exception:
        return None


def _player_name(player_id: int) -> str:
    # prefer GAME_STATE meta
    meta = (GAME_STATE.get("players") or {}).get(int(player_id))
    if isinstance(meta, dict) and meta.get("name"):
        return str(meta["name"])
    if ROSTER_DF is not None:
        try:
            if int(player_id) in ROSTER_DF.index:
                return str(ROSTER_DF.loc[int(player_id), "Name"])
        except Exception:
            pass
    return f"Player#{int(player_id)}"


def _append_weekly_news_item(item: Dict[str, Any]) -> None:
    cached = GAME_STATE.setdefault("cached_views", {})
    weekly = cached.setdefault("weekly_news", {"last_generated_week_start": None, "items": []})
    items = weekly.setdefault("items", [])
    if isinstance(items, list):
        items.append(item)


def _transaction_payload(
    proposal: TradeProposal,
    validation: TradeValidationResult,
    *,
    summary: str,
) -> Dict[str, Any]:
    a = _u(proposal.team_a)
    b = _u(proposal.team_b)
    # Keep a modern structured payload, but also include legacy keys used by older UI/tools.
    return {
        "type": "trade",
        "date": proposal.date_str or _now_date_str(),
        # legacy
        "teams_involved": [a, b],
        "players_from_a": [int(x) for x in proposal.send_a_players],
        "players_from_b": [int(x) for x in proposal.send_b_players],
        "picks_from_a": list(proposal.send_a_picks or []),
        "picks_from_b": list(proposal.send_b_picks or []),
        # modern
        "teams": {"a": a, "b": b},
        "assets": {
            "a_players": [int(x) for x in proposal.send_a_players],
            "a_picks": list(proposal.send_a_picks or []),
            "b_players": [int(x) for x in proposal.send_b_players],
            "b_picks": list(proposal.send_b_picks or []),
        },
        "salary": {
            "a_out": validation.out_a,
            "b_out": validation.out_b,
            "a_payroll_before": validation.payroll_a_before,
            "b_payroll_before": validation.payroll_b_before,
            "a_payroll_after": validation.payroll_a_after,
            "b_payroll_after": validation.payroll_b_after,
        },
        "summary": summary,
    }



# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def validate_trade(proposal: TradeProposal) -> TradeValidationResult:
    """Validate a two-team trade proposal.

    Returns a TradeValidationResult with ok=False and reasons if invalid.
    """
    ensure_contracts_initialized()
    ensure_draft_picks_initialized()

    rules = _league_rules()
    hard_cap = float(rules.get("hard_cap", 0.0))
    soft_cap = float(rules.get("soft_cap", 0.0))
    roster_min = int(rules.get("roster_min", 12))
    roster_max = int(rules.get("roster_max", 15))
    pct_over = float(rules.get("salary_match_pct_over_cap", 0.25))
    bonus = float(rules.get("salary_match_bonus", 1_000_000.0))

    a = _u(proposal.team_a)
    b = _u(proposal.team_b)
    reasons: List[str] = []

    if not a or not b:
        reasons.append("invalid_team_id")
    if a == b:
        reasons.append("teams_must_be_different")

    # Dedupe checks
    if len(set(map(int, proposal.send_a_players))) != len(proposal.send_a_players):
        reasons.append("duplicate_player_in_team_a_outgoing")
    if len(set(map(int, proposal.send_b_players))) != len(proposal.send_b_players):
        reasons.append("duplicate_player_in_team_b_outgoing")
    if set(map(int, proposal.send_a_players)).intersection(set(map(int, proposal.send_b_players))):
        reasons.append("same_player_on_both_sides")
    if len(set(proposal.send_a_picks)) != len(proposal.send_a_picks):
        reasons.append("duplicate_pick_in_team_a_outgoing")
    if len(set(proposal.send_b_picks)) != len(proposal.send_b_picks):
        reasons.append("duplicate_pick_in_team_b_outgoing")
    if set(proposal.send_a_picks).intersection(set(proposal.send_b_picks)):
        reasons.append("same_pick_on_both_sides")

    # Ownership checks: players
    for pid in proposal.send_a_players:
        t = _player_team_from_roster(int(pid))
        if t is None:
            reasons.append(f"unknown_player:{int(pid)}")
        elif t != a:
            reasons.append(f"player_not_on_team:{int(pid)}:{a}")
    for pid in proposal.send_b_players:
        t = _player_team_from_roster(int(pid))
        if t is None:
            reasons.append(f"unknown_player:{int(pid)}")
        elif t != b:
            reasons.append(f"player_not_on_team:{int(pid)}:{b}")

    # Ownership checks: picks
    for pick_id in proposal.send_a_picks:
        p = get_pick(pick_id)
        if not p:
            reasons.append(f"unknown_pick:{pick_id}")
            continue
        owner = _u(str(p.get("owner_team_id", "")))
        if owner != a:
            reasons.append(f"pick_not_owned_by_team:{pick_id}:{a}")
    for pick_id in proposal.send_b_picks:
        p = get_pick(pick_id)
        if not p:
            reasons.append(f"unknown_pick:{pick_id}")
            continue
        owner = _u(str(p.get("owner_team_id", "")))
        if owner != b:
            reasons.append(f"pick_not_owned_by_team:{pick_id}:{b}")

    # Roster size after
    roster_a_before = _roster_count(a)
    roster_b_before = _roster_count(b)
    roster_a_after = roster_a_before - len(proposal.send_a_players) + len(proposal.send_b_players)
    roster_b_after = roster_b_before - len(proposal.send_b_players) + len(proposal.send_a_players)
    if roster_a_after < roster_min or roster_a_after > roster_max:
        reasons.append("roster_size_invalid_team_a")
    if roster_b_after < roster_min or roster_b_after > roster_max:
        reasons.append("roster_size_invalid_team_b")

    # Salary numbers
    out_a = float(total_salary([int(x) for x in proposal.send_a_players]))
    out_b = float(total_salary([int(x) for x in proposal.send_b_players]))
    in_a = out_b
    in_b = out_a

    payroll_a_before = float(team_payroll(a))
    payroll_b_before = float(team_payroll(b))
    payroll_a_after = payroll_a_before - out_a + in_a
    payroll_b_after = payroll_b_before - out_b + in_b

    # Hard cap check
    if hard_cap:
        if payroll_a_after > hard_cap:
            reasons.append("hard_cap_exceeded_team_a")
        if payroll_b_after > hard_cap:
            reasons.append("hard_cap_exceeded_team_b")

    # Simplified salary matching
    def allowed_incoming(payroll_before: float, outgoing: float) -> float:
        if soft_cap and payroll_before < soft_cap:
            cap_space = max(0.0, soft_cap - payroll_before)
            return outgoing + cap_space
        # Over-cap: must match within pct + bonus
        if outgoing <= 0.0:
            return 0.0
        return outgoing * (1.0 + pct_over) + bonus

    allowed_a = allowed_incoming(payroll_a_before, out_a)
    allowed_b = allowed_incoming(payroll_b_before, out_b)

    if in_a - 1e-6 > allowed_a:
        reasons.append("salary_match_failed_team_a")
    if in_b - 1e-6 > allowed_b:
        reasons.append("salary_match_failed_team_b")

    ok = len(reasons) == 0
    return TradeValidationResult(
        ok=ok,
        reasons=reasons,
        payroll_a_before=payroll_a_before,
        payroll_b_before=payroll_b_before,
        payroll_a_after=payroll_a_after,
        payroll_b_after=payroll_b_after,
        out_a=out_a,
        out_b=out_b,
    )


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------

def apply_trade(
    proposal: TradeProposal,
    *,
    record_transaction: bool = True,
    record_weekly_news: bool = True,
) -> TradeValidationResult:
    """Apply a validated trade to ROSTER_DF and GAME_STATE.

    Raises ValueError if invalid.
    """
    validation = validate_trade(proposal)
    if not validation.ok:
        raise ValueError(f"Invalid trade: {validation.reasons}")

    a = _u(proposal.team_a)
    b = _u(proposal.team_b)
    date_str = proposal.date_str or _now_date_str()

    # Update ROSTER_DF Team ownership (canonical roster table)
    if ROSTER_DF is not None:
        for pid in proposal.send_a_players:
            if int(pid) in ROSTER_DF.index:
                ROSTER_DF.loc[int(pid), "Team"] = b
        for pid in proposal.send_b_players:
            if int(pid) in ROSTER_DF.index:
                ROSTER_DF.loc[int(pid), "Team"] = a

    # Update GAME_STATE players meta
    players = GAME_STATE.setdefault("players", {})
    if isinstance(players, dict):
        for pid in proposal.send_a_players:
            meta = players.get(int(pid))
            if isinstance(meta, dict):
                meta["team_id"] = b
        for pid in proposal.send_b_players:
            meta = players.get(int(pid))
            if isinstance(meta, dict):
                meta["team_id"] = a

    # Transfer picks
    for pick_id in proposal.send_a_picks:
        transfer_pick(pick_id, b)
    for pick_id in proposal.send_b_picks:
        transfer_pick(pick_id, a)

    # Create summary string
    a_names = ", ".join(_player_name(pid) for pid in proposal.send_a_players) if proposal.send_a_players else ""
    b_names = ", ".join(_player_name(pid) for pid in proposal.send_b_players) if proposal.send_b_players else ""
    a_picks = ", ".join(describe_pick(get_pick(pid) or {"pick_id": pid}) for pid in proposal.send_a_picks) if proposal.send_a_picks else ""
    b_picks = ", ".join(describe_pick(get_pick(pid) or {"pick_id": pid}) for pid in proposal.send_b_picks) if proposal.send_b_picks else ""

    def join_bits(parts: List[str]) -> str:
        parts2 = [p for p in parts if p]
        return " + ".join(parts2) if parts2 else "(nothing)"

    summary = f"{a} sends {join_bits([a_names, a_picks])} to {b}; {b} sends {join_bits([b_names, b_picks])} to {a}."
    if date_str:
        summary = f"[{date_str}] " + summary

    if record_transaction:
        tx = _transaction_payload(proposal, validation, summary=summary)
        GAME_STATE.setdefault("transactions", [])
        if isinstance(GAME_STATE["transactions"], list):
            GAME_STATE["transactions"].append(tx)

    if record_weekly_news:
        _append_weekly_news_item({
            "type": "trade",
            "date": date_str,
            "summary": summary,
            "teams": [a, b],
        })

    return validation


def proposal_to_dict(proposal: TradeProposal) -> Dict[str, Any]:
    """Useful for API responses / logging."""
    return asdict(proposal)
