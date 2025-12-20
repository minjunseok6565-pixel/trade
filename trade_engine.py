from __future__ import annotations

"""trade_engine.py

Core trade validation + application ("the referee + transaction applier").

Design goals
- deterministic given inputs (no random here)
- reusable for BOTH AI trades and user-initiated trades
- tolerant of partially-initialized state (early dev)

This revision adds (MVP+)
- richer validation result fields (why it failed + numbers for auto-fixing)
- human-readable explanation helper
- fix suggestions helper for AI negotiation (salary filler / roster count)
- relationship updates on successful trade

NOTE
- weekly news feed integration is kept, but the user asked to ignore weekly news changes.
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
    from contracts import ensure_contracts_initialized, total_salary, team_payroll
except Exception:  # pragma: no cover
    def ensure_contracts_initialized(*args: Any, **kwargs: Any) -> None:
        return None

    def total_salary(player_ids: List[int], *args: Any, **kwargs: Any) -> float:
        return 0.0

    def team_payroll(team_id: str, *args: Any, **kwargs: Any) -> float:
        return 0.0

try:
    from assets import ensure_draft_picks_initialized, get_pick, transfer_pick, describe_pick, DraftPick
except Exception:  # pragma: no cover
    DraftPick = Any  # type: ignore

    def ensure_draft_picks_initialized(*args: Any, **kwargs: Any) -> None:
        return None

    def get_pick(pick_id: str) -> Optional[Any]:
        return None

    def transfer_pick(pick_id: str, new_owner_team_id: str) -> Optional[Any]:
        return None

    def describe_pick(pick: Any) -> str:
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

    # Date / deadline
    date_str: Optional[str] = None
    deadline: Optional[str] = None

    # Roster counts
    roster_a_before: int = 0
    roster_b_before: int = 0
    roster_a_after: int = 0
    roster_b_after: int = 0

    # Salary numbers
    outgoing_salary_a: float = 0.0
    outgoing_salary_b: float = 0.0
    incoming_salary_a: float = 0.0
    incoming_salary_b: float = 0.0
    allowed_incoming_salary_a: float = 0.0
    allowed_incoming_salary_b: float = 0.0

    payroll_a_before: float = 0.0
    payroll_b_before: float = 0.0
    payroll_a_after: float = 0.0
    payroll_b_after: float = 0.0

    cap_space_a_before: float = 0.0
    cap_space_b_before: float = 0.0

    hard_cap_after_excess_a: float = 0.0
    hard_cap_after_excess_b: float = 0.0

    salary_over_amount_a: float = 0.0
    salary_over_amount_b: float = 0.0

    # Ownership / existence diagnostics
    missing_players_a: List[int] = None  # type: ignore[assignment]
    missing_players_b: List[int] = None  # type: ignore[assignment]
    wrong_owner_players_a: List[int] = None  # type: ignore[assignment]
    wrong_owner_players_b: List[int] = None  # type: ignore[assignment]

    missing_picks_a: List[str] = None  # type: ignore[assignment]
    missing_picks_b: List[str] = None  # type: ignore[assignment]
    wrong_owner_picks_a: List[str] = None  # type: ignore[assignment]
    wrong_owner_picks_b: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # normalize list fields
        for k in (
            "missing_players_a",
            "missing_players_b",
            "wrong_owner_players_a",
            "wrong_owner_players_b",
            "missing_picks_a",
            "missing_picks_b",
            "wrong_owner_picks_a",
            "wrong_owner_picks_b",
        ):
            if getattr(self, k) is None:
                setattr(self, k, [])


@dataclass(frozen=True)
class FixSuggestion:
    kind: str
    team_side: str  # "A" or "B"
    amount: Optional[float] = None
    notes: str = ""
    constraints: Optional[Dict[str, Any]] = None


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
    rules.setdefault("soft_cap", float(rules.get("soft_cap", hard_cap * 0.85 if hard_cap else 0.0)))

    # Roster size bounds (NBA-ish, simplified)
    rules.setdefault("roster_min", int(rules.get("roster_min", 12)))
    rules.setdefault("roster_max", int(rules.get("roster_max", 15)))

    # Salary matching knobs (simplified)
    rules.setdefault("salary_match_pct_over_cap", float(rules.get("salary_match_pct_over_cap", 0.25)))  # 125%
    rules.setdefault("salary_match_bonus", float(rules.get("salary_match_bonus", 1_000_000.0)))  # +$1M buffer

    # Optional deadline
    # rules["trade_deadline"] = "YYYY-MM-DD"

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


def _ensure_relationship_slots(a: str, b: str) -> None:
    """Create relationship entries if missing.

    We keep this here (not valuation) because apply_trade must update trust/trade_count.
    """

    # Avoid importing valuation at module import time to prevent circular deps.
    try:
        import valuation

        valuation.ensure_relationships_initialized()
    except Exception:
        # minimal fallback
        rel = GAME_STATE.setdefault("relationships", {})
        if not isinstance(rel, dict):
            GAME_STATE["relationships"] = {}
            rel = GAME_STATE["relationships"]
        rel.setdefault(a, {})
        rel.setdefault(b, {})
        if isinstance(rel.get(a), dict):
            rel[a].setdefault(b, {"trust": 0.5, "trade_count": 0, "last_trade_date": None, "rival": False})
        if isinstance(rel.get(b), dict):
            rel[b].setdefault(a, {"trust": 0.5, "trade_count": 0, "last_trade_date": None, "rival": False})


def _update_relationships_after_trade(a: str, b: str, date_str: Optional[str]) -> None:
    _ensure_relationship_slots(a, b)
    rel = GAME_STATE.get("relationships")
    if not isinstance(rel, dict):
        return
    for x, y in ((a, b), (b, a)):
        m = rel.get(x)
        if not isinstance(m, dict):
            continue
        entry = m.get(y)
        if not isinstance(entry, dict):
            entry = {"trust": 0.5, "trade_count": 0, "last_trade_date": None, "rival": False}
            m[y] = entry
        entry["trade_count"] = int(entry.get("trade_count", 0) or 0) + 1
        entry["last_trade_date"] = date_str
        # small trust bump for completed deals
        try:
            entry["trust"] = float(entry.get("trust", 0.5) or 0.5) + 0.01
        except Exception:
            entry["trust"] = 0.51
        entry["trust"] = max(0.0, min(1.0, float(entry["trust"])))


def _transaction_payload(
    proposal: TradeProposal,
    validation: TradeValidationResult,
    *,
    summary: str,
    explain: Optional[Dict[str, Any]] = None,
    evaluation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    a = _u(proposal.team_a)
    b = _u(proposal.team_b)

    return {
        "type": "trade",
        "date": validation.date_str or proposal.date_str or _now_date_str(),
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
            "a_out": validation.outgoing_salary_a,
            "b_out": validation.outgoing_salary_b,
            "a_in": validation.incoming_salary_a,
            "b_in": validation.incoming_salary_b,
            "a_allowed_in": validation.allowed_incoming_salary_a,
            "b_allowed_in": validation.allowed_incoming_salary_b,
            "a_payroll_before": validation.payroll_a_before,
            "b_payroll_before": validation.payroll_b_before,
            "a_payroll_after": validation.payroll_a_after,
            "b_payroll_after": validation.payroll_b_after,
        },
        "roster": {
            "a_before": validation.roster_a_before,
            "b_before": validation.roster_b_before,
            "a_after": validation.roster_a_after,
            "b_after": validation.roster_b_after,
        },
        "reasons": list(validation.reasons or []),
        "validation": asdict(validation),
        "explain": explain or {},
        "evaluation": evaluation or {},
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

    # normalize date
    date_str = proposal.date_str or _now_date_str()

    reasons: List[str] = []

    # Deadline guard (optional)
    deadline = None
    if rules.get("trade_deadline"):
        deadline = str(rules.get("trade_deadline"))
        if date_str:
            try:
                if date.fromisoformat(date_str) > date.fromisoformat(deadline):
                    reasons.append("past_trade_deadline")
            except Exception:
                # ignore malformed deadline
                pass

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

    missing_players_a: List[int] = []
    missing_players_b: List[int] = []
    wrong_owner_players_a: List[int] = []
    wrong_owner_players_b: List[int] = []

    missing_picks_a: List[str] = []
    missing_picks_b: List[str] = []
    wrong_owner_picks_a: List[str] = []
    wrong_owner_picks_b: List[str] = []

    # Ownership checks: players
    for pid in proposal.send_a_players:
        t = _player_team_from_roster(int(pid))
        if t is None:
            missing_players_a.append(int(pid))
        elif t != a:
            wrong_owner_players_a.append(int(pid))

    for pid in proposal.send_b_players:
        t = _player_team_from_roster(int(pid))
        if t is None:
            missing_players_b.append(int(pid))
        elif t != b:
            wrong_owner_players_b.append(int(pid))

    if missing_players_a:
        reasons.append("unknown_player_team_a")
    if missing_players_b:
        reasons.append("unknown_player_team_b")
    if wrong_owner_players_a:
        reasons.append("player_not_on_team_a")
    if wrong_owner_players_b:
        reasons.append("player_not_on_team_b")

    # Ownership checks: picks
    for pick_id in proposal.send_a_picks:
        p = get_pick(pick_id)
        if not p:
            missing_picks_a.append(str(pick_id))
            continue
        owner = _u(str(getattr(p, "current_owner", "")))
        if owner != a:
            wrong_owner_picks_a.append(str(pick_id))

    for pick_id in proposal.send_b_picks:
        p = get_pick(pick_id)
        if not p:
            missing_picks_b.append(str(pick_id))
            continue
        owner = _u(str(getattr(p, "current_owner", "")))
        if owner != b:
            wrong_owner_picks_b.append(str(pick_id))

    if missing_picks_a:
        reasons.append("unknown_pick_team_a")
    if missing_picks_b:
        reasons.append("unknown_pick_team_b")
    if wrong_owner_picks_a:
        reasons.append("pick_not_owned_by_team_a")
    if wrong_owner_picks_b:
        reasons.append("pick_not_owned_by_team_b")

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

    cap_space_a_before = max(0.0, soft_cap - payroll_a_before) if soft_cap else 0.0
    cap_space_b_before = max(0.0, soft_cap - payroll_b_before) if soft_cap else 0.0

    hard_excess_a = max(0.0, payroll_a_after - hard_cap) if hard_cap else 0.0
    hard_excess_b = max(0.0, payroll_b_after - hard_cap) if hard_cap else 0.0

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

    allowed_a = float(allowed_incoming(payroll_a_before, out_a))
    allowed_b = float(allowed_incoming(payroll_b_before, out_b))

    over_a = max(0.0, in_a - allowed_a)
    over_b = max(0.0, in_b - allowed_b)

    if in_a - 1e-6 > allowed_a:
        reasons.append("salary_match_failed_team_a")
    if in_b - 1e-6 > allowed_b:
        reasons.append("salary_match_failed_team_b")

    ok = len(reasons) == 0
    return TradeValidationResult(
        ok=ok,
        reasons=reasons,
        date_str=date_str,
        deadline=deadline,
        roster_a_before=roster_a_before,
        roster_b_before=roster_b_before,
        roster_a_after=roster_a_after,
        roster_b_after=roster_b_after,
        outgoing_salary_a=out_a,
        outgoing_salary_b=out_b,
        incoming_salary_a=in_a,
        incoming_salary_b=in_b,
        allowed_incoming_salary_a=allowed_a,
        allowed_incoming_salary_b=allowed_b,
        payroll_a_before=payroll_a_before,
        payroll_b_before=payroll_b_before,
        payroll_a_after=payroll_a_after,
        payroll_b_after=payroll_b_after,
        cap_space_a_before=cap_space_a_before,
        cap_space_b_before=cap_space_b_before,
        hard_cap_after_excess_a=hard_excess_a,
        hard_cap_after_excess_b=hard_excess_b,
        salary_over_amount_a=over_a,
        salary_over_amount_b=over_b,
        missing_players_a=missing_players_a,
        missing_players_b=missing_players_b,
        wrong_owner_players_a=wrong_owner_players_a,
        wrong_owner_players_b=wrong_owner_players_b,
        missing_picks_a=missing_picks_a,
        missing_picks_b=missing_picks_b,
        wrong_owner_picks_a=wrong_owner_picks_a,
        wrong_owner_picks_b=wrong_owner_picks_b,
    )


# ---------------------------------------------------------------------
# Explain + fix suggestions (G)
# ---------------------------------------------------------------------


def explain_validation(proposal: TradeProposal, validation: TradeValidationResult, *, locale: str = "ko") -> Dict[str, Any]:
    """Turn a validation result into human-friendly explanation.

    Returns a dict with {summary, details, codes}.
    """

    a = _u(proposal.team_a)
    b = _u(proposal.team_b)

    codes = list(validation.reasons or [])
    details: List[str] = []

    def ko(s: str, en: str) -> str:
        return s if str(locale).lower().startswith("ko") else en

    if not codes:
        return {
            "summary": ko("유효한 트레이드입니다.", "Trade is valid."),
            "details": [],
            "codes": [],
        }

    # Summary: first major reason
    primary = codes[0]
    summary_map = {
        "past_trade_deadline": ko("트레이드 데드라인이 지났습니다.", "Trade deadline has passed."),
        "salary_match_failed_team_a": ko(f"샐러리 매칭 실패 ({a})", f"Salary match failed ({a})"),
        "salary_match_failed_team_b": ko(f"샐러리 매칭 실패 ({b})", f"Salary match failed ({b})"),
        "roster_size_invalid_team_a": ko(f"로스터 인원 규정 위반 ({a})", f"Roster size invalid ({a})"),
        "roster_size_invalid_team_b": ko(f"로스터 인원 규정 위반 ({b})", f"Roster size invalid ({b})"),
        "hard_cap_exceeded_team_a": ko(f"하드캡 초과 ({a})", f"Hard cap exceeded ({a})"),
        "hard_cap_exceeded_team_b": ko(f"하드캡 초과 ({b})", f"Hard cap exceeded ({b})"),
        "unknown_player_team_a": ko(f"({a}) 측 선수 정보가 유효하지 않습니다.", f"Unknown player(s) on {a} side."),
        "unknown_player_team_b": ko(f"({b}) 측 선수 정보가 유효하지 않습니다.", f"Unknown player(s) on {b} side."),
        "player_not_on_team_a": ko(f"({a})가 보유하지 않은 선수가 포함되어 있습니다.", f"Player not owned by {a}."),
        "player_not_on_team_b": ko(f"({b})가 보유하지 않은 선수가 포함되어 있습니다.", f"Player not owned by {b}."),
        "unknown_pick_team_a": ko(f"({a}) 측 픽 정보가 유효하지 않습니다.", f"Unknown pick(s) on {a} side."),
        "unknown_pick_team_b": ko(f"({b}) 측 픽 정보가 유효하지 않습니다.", f"Unknown pick(s) on {b} side."),
        "pick_not_owned_by_team_a": ko(f"({a})가 보유하지 않은 픽이 포함되어 있습니다.", f"Pick not owned by {a}."),
        "pick_not_owned_by_team_b": ko(f"({b})가 보유하지 않은 픽이 포함되어 있습니다.", f"Pick not owned by {b}."),
    }

    summary = summary_map.get(primary) or ko("트레이드가 유효하지 않습니다.", "Trade is invalid.")

    # Details
    if "past_trade_deadline" in codes and validation.deadline:
        details.append(ko(f"데드라인: {validation.deadline}", f"Deadline: {validation.deadline}"))

    if "salary_match_failed_team_a" in codes:
        details.append(
            ko(
                f"{a} incoming {validation.incoming_salary_a:,.0f} > allowed {validation.allowed_incoming_salary_a:,.0f} (초과 {validation.salary_over_amount_a:,.0f})",
                f"{a} incoming {validation.incoming_salary_a:,.0f} > allowed {validation.allowed_incoming_salary_a:,.0f} (over {validation.salary_over_amount_a:,.0f})",
            )
        )
    if "salary_match_failed_team_b" in codes:
        details.append(
            ko(
                f"{b} incoming {validation.incoming_salary_b:,.0f} > allowed {validation.allowed_incoming_salary_b:,.0f} (초과 {validation.salary_over_amount_b:,.0f})",
                f"{b} incoming {validation.incoming_salary_b:,.0f} > allowed {validation.allowed_incoming_salary_b:,.0f} (over {validation.salary_over_amount_b:,.0f})",
            )
        )

    if "roster_size_invalid_team_a" in codes:
        details.append(ko(f"{a} roster {validation.roster_a_before} -> {validation.roster_a_after}", f"{a} roster {validation.roster_a_before} -> {validation.roster_a_after}"))
    if "roster_size_invalid_team_b" in codes:
        details.append(ko(f"{b} roster {validation.roster_b_before} -> {validation.roster_b_after}", f"{b} roster {validation.roster_b_before} -> {validation.roster_b_after}"))

    if "hard_cap_exceeded_team_a" in codes:
        details.append(ko(f"{a} 하드캡 초과분: {validation.hard_cap_after_excess_a:,.0f}", f"{a} hard cap excess: {validation.hard_cap_after_excess_a:,.0f}"))
    if "hard_cap_exceeded_team_b" in codes:
        details.append(ko(f"{b} 하드캡 초과분: {validation.hard_cap_after_excess_b:,.0f}", f"{b} hard cap excess: {validation.hard_cap_after_excess_b:,.0f}"))

    if validation.missing_players_a:
        details.append(ko(f"{a} missing players: {validation.missing_players_a}", f"{a} missing players: {validation.missing_players_a}"))
    if validation.wrong_owner_players_a:
        details.append(ko(f"{a} wrong-owner players: {validation.wrong_owner_players_a}", f"{a} wrong-owner players: {validation.wrong_owner_players_a}"))
    if validation.missing_players_b:
        details.append(ko(f"{b} missing players: {validation.missing_players_b}", f"{b} missing players: {validation.missing_players_b}"))
    if validation.wrong_owner_players_b:
        details.append(ko(f"{b} wrong-owner players: {validation.wrong_owner_players_b}", f"{b} wrong-owner players: {validation.wrong_owner_players_b}"))

    if validation.missing_picks_a:
        details.append(ko(f"{a} missing picks: {validation.missing_picks_a}", f"{a} missing picks: {validation.missing_picks_a}"))
    if validation.wrong_owner_picks_a:
        details.append(ko(f"{a} wrong-owner picks: {validation.wrong_owner_picks_a}", f"{a} wrong-owner picks: {validation.wrong_owner_picks_a}"))
    if validation.missing_picks_b:
        details.append(ko(f"{b} missing picks: {validation.missing_picks_b}", f"{b} missing picks: {validation.missing_picks_b}"))
    if validation.wrong_owner_picks_b:
        details.append(ko(f"{b} wrong-owner picks: {validation.wrong_owner_picks_b}", f"{b} wrong-owner picks: {validation.wrong_owner_picks_b}"))

    return {
        "summary": summary,
        "details": details,
        "codes": codes,
    }


def suggest_fixes(proposal: TradeProposal, validation: TradeValidationResult) -> List[FixSuggestion]:
    """Return fix suggestions that an AI can try automatically."""

    fixes: List[FixSuggestion] = []
    codes = set(validation.reasons or [])

    # Salary fixes
    if "salary_match_failed_team_a" in codes:
        fixes.append(
            FixSuggestion(
                kind="add_outgoing_salary",
                team_side="A",
                amount=float(validation.salary_over_amount_a),
                notes="Add outgoing salary (salary filler) from Team A.",
            )
        )
    if "salary_match_failed_team_b" in codes:
        fixes.append(
            FixSuggestion(
                kind="add_outgoing_salary",
                team_side="B",
                amount=float(validation.salary_over_amount_b),
                notes="Add outgoing salary (salary filler) from Team B.",
            )
        )

    # Roster size fixes
    if "roster_size_invalid_team_a" in codes:
        if validation.roster_a_after > validation.roster_a_before:
            fixes.append(FixSuggestion(kind="add_outgoing_player", team_side="A", amount=1.0, notes="Roster too big: send one more player from Team A."))
        else:
            fixes.append(FixSuggestion(kind="reduce_outgoing", team_side="A", amount=1.0, notes="Roster too small: reduce outgoing players from Team A."))

    if "roster_size_invalid_team_b" in codes:
        if validation.roster_b_after > validation.roster_b_before:
            fixes.append(FixSuggestion(kind="add_outgoing_player", team_side="B", amount=1.0, notes="Roster too big: send one more player from Team B."))
        else:
            fixes.append(FixSuggestion(kind="reduce_outgoing", team_side="B", amount=1.0, notes="Roster too small: reduce outgoing players from Team B."))

    # Ownership / existence: safest fix is to remove the offending asset(s)
    if validation.missing_players_a or validation.wrong_owner_players_a:
        fixes.append(
            FixSuggestion(
                kind="remove_asset",
                team_side="A",
                notes="Remove missing/wrong-owner player(s) from Team A outgoing.",
                constraints={"player_ids": list(set(validation.missing_players_a + validation.wrong_owner_players_a))},
            )
        )
    if validation.missing_players_b or validation.wrong_owner_players_b:
        fixes.append(
            FixSuggestion(
                kind="remove_asset",
                team_side="B",
                notes="Remove missing/wrong-owner player(s) from Team B outgoing.",
                constraints={"player_ids": list(set(validation.missing_players_b + validation.wrong_owner_players_b))},
            )
        )
    if validation.missing_picks_a or validation.wrong_owner_picks_a:
        fixes.append(
            FixSuggestion(
                kind="remove_asset",
                team_side="A",
                notes="Remove missing/wrong-owner pick(s) from Team A outgoing.",
                constraints={"pick_ids": list(set(validation.missing_picks_a + validation.wrong_owner_picks_a))},
            )
        )
    if validation.missing_picks_b or validation.wrong_owner_picks_b:
        fixes.append(
            FixSuggestion(
                kind="remove_asset",
                team_side="B",
                notes="Remove missing/wrong-owner pick(s) from Team B outgoing.",
                constraints={"pick_ids": list(set(validation.missing_picks_b + validation.wrong_owner_picks_b))},
            )
        )

    # Keep it short (AI can call validate again and iterate)
    return fixes[:4]


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------


def apply_trade(
    proposal: TradeProposal,
    *,
    record_transaction: bool = True,
    record_weekly_news: bool = True,
    evaluation: Optional[Dict[str, Any]] = None,
) -> TradeValidationResult:
    """Apply a validated trade to ROSTER_DF and GAME_STATE.

    Raises ValueError if invalid.
    """

    validation = validate_trade(proposal)
    if not validation.ok:
        raise ValueError(f"Invalid trade: {validation.reasons}")

    a = _u(proposal.team_a)
    b = _u(proposal.team_b)
    date_str = validation.date_str or proposal.date_str or _now_date_str()

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

    a_picks = ", ".join(describe_pick(get_pick(pid) or DraftPick(pid, 0, 1, "", "")) for pid in proposal.send_a_picks) if proposal.send_a_picks else ""
    b_picks = ", ".join(describe_pick(get_pick(pid) or DraftPick(pid, 0, 1, "", "")) for pid in proposal.send_b_picks) if proposal.send_b_picks else ""

    def join_bits(parts: List[str]) -> str:
        parts2 = [p for p in parts if p]
        return " + ".join(parts2) if parts2 else "(nothing)"

    summary = f"{a} sends {join_bits([a_names, a_picks])} to {b}; {b} sends {join_bits([b_names, b_picks])} to {a}."
    if date_str:
        summary = f"[{date_str}] " + summary

    explain = explain_validation(proposal, validation, locale="ko")

    if record_transaction:
        tx = _transaction_payload(proposal, validation, summary=summary, explain=explain, evaluation=evaluation)
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

    # Relationship update
    _update_relationships_after_trade(a, b, date_str)

    return validation


def proposal_to_dict(proposal: TradeProposal) -> Dict[str, Any]:
    """Useful for API responses / logging."""

    return asdict(proposal)
