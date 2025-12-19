from __future__ import annotations

"""
contracts.py

A lightweight contract/finance module intended to be used by the trade system.
Design goals:
- Keep "truth" in GAME_STATE (so save/load is easy).
- Be tolerant of missing roster columns (works with minimal data).
- Provide a stable interface for trade validation (salary matching, expirings, etc.).

This is an MVP draft. You will likely extend:
- options (TO/PO), non-guarantees, trade kicker
- bird rights / cap holds
- season rollover / FA handling
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
import random


# ---- Optional imports (so this module can be imported in isolation during early dev) ----
try:
    from state import GAME_STATE, _ensure_league_state
except Exception:  # pragma: no cover
    GAME_STATE: Dict[str, Any] = {"league": {}, "players": {}, "teams": {}, "transactions": []}

    def _ensure_league_state() -> Dict[str, Any]:
        league = GAME_STATE.setdefault("league", {})
        league.setdefault("trade_rules", {})
        return league

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover
    pd = None  # type: ignore


# ---- Data model ----
@dataclass(frozen=True)
class Contract:
    """A minimal contract representation suitable for salary matching and valuation."""
    player_id: int
    start_year: int  # season year when this contract started (not draft year)
    years_total: int
    years_left: int
    salary_by_year: List[float]  # length >= years_left, salary of each remaining year
    option_type: str = "none"  # none | team | player
    guaranteed: bool = True
    notes: str = ""

    def salary_current(self, season_offset: int = 0) -> float:
        """Return salary for the season offset (0 = current season)."""
        idx = max(0, int(season_offset))
        if idx >= len(self.salary_by_year):
            return float(self.salary_by_year[-1]) if self.salary_by_year else 0.0
        return float(self.salary_by_year[idx])


# ---- GAME_STATE helpers ----
_CONTRACTS_KEY = "contracts"  # top-level key in GAME_STATE for contract dicts


def _league_season_year() -> int:
    league = _ensure_league_state()
    y = league.get("season_year")
    if isinstance(y, int) and y > 0:
        return y
    # fallback to current calendar year (state.initialize_master_schedule_if_needed does this too)
    try:
        from datetime import date
        return date.today().year
    except Exception:  # pragma: no cover
        return 2025


def _contracts_store() -> Dict[int, Dict[str, Any]]:
    store = GAME_STATE.setdefault(_CONTRACTS_KEY, {})
    # normalize keys to int where possible
    if isinstance(store, dict):
        return store  # type: ignore[return-value]
    GAME_STATE[_CONTRACTS_KEY] = {}
    return GAME_STATE[_CONTRACTS_KEY]  # type: ignore[return-value]


def get_contract(player_id: int) -> Optional[Contract]:
    raw = _contracts_store().get(int(player_id))
    if not raw:
        # fallback: some builds may embed contract in GAME_STATE["players"][pid]["contract"]
        p = (GAME_STATE.get("players") or {}).get(int(player_id))  # type: ignore[arg-type]
        raw = p.get("contract") if isinstance(p, dict) else None
    if not raw:
        return None
    try:
        return Contract(**raw)  # type: ignore[arg-type]
    except Exception:
        return None


def set_contract(contract: Contract) -> None:
    _contracts_store()[int(contract.player_id)] = asdict(contract)
    # Optional mirror into player meta (useful for UI)
    players = GAME_STATE.get("players")
    if isinstance(players, dict) and int(contract.player_id) in players:
        meta = players[int(contract.player_id)]
        if isinstance(meta, dict):
            meta["contract"] = asdict(contract)
            # keep salary mirrored
            meta.setdefault("salary", contract.salary_current(0))


# ---- Initialization ----
def ensure_contracts_initialized(
    *,
    force: bool = False,
    default_years_left_range: Tuple[int, int] = (1, 4),
    seed: Optional[int] = None,
) -> None:
    """
    Populate GAME_STATE['contracts'] if missing.

    Sources, in order:
    1) If roster has contract columns, use them.
    2) Else: generate plausible defaults from player meta (deterministic by seed + player_id).
    """
    # Ensure players exist (team_utils initializes GAME_STATE["players"] from roster)
    try:
        from team_utils import _init_players_and_teams_if_needed  # lazy import
        _init_players_and_teams_if_needed()
    except Exception:
        # If team_utils/config isn't available, we still allow contracts init for tests.
        GAME_STATE.setdefault("players", {})

    store = _contracts_store()
    if store and not force:
        return

    players = GAME_STATE.get("players") or {}
    if not isinstance(players, dict):
        GAME_STATE["players"] = {}
        players = GAME_STATE["players"]

    rng = random.Random(seed if seed is not None else (_league_season_year() * 10007 + 1337))

    # Try to read roster if available (optional)
    roster_df = None
    try:
        from config import ROSTER_DF  # type: ignore
        roster_df = ROSTER_DF
    except Exception:
        roster_df = None

    def _infer_from_roster(pid: int) -> Optional[Contract]:
        if roster_df is None or pd is None:
            return None
        try:
            row = roster_df.loc[pid]
        except Exception:
            return None

        salary = float(row.get("SalaryAmount", 0.0))
        # Common-ish column guesses (you will refine later)
        years_left = None
        for col in ("YearsLeft", "YrsLeft", "ContractYearsLeft", "Years_Remaining"):
            if col in roster_df.columns:
                v = row.get(col)
                if v is not None and not pd.isna(v):
                    try:
                        years_left = int(v)
                        break
                    except Exception:
                        pass
        if years_left is None:
            for col in ("ContractYears", "Years", "Yrs"):
                if col in roster_df.columns:
                    v = row.get(col)
                    if v is not None and not pd.isna(v):
                        try:
                            years_left = int(v)
                            break
                        except Exception:
                            pass

        if years_left is None:
            return None

        years_left = max(0, min(int(years_left), 6))
        if years_left == 0:
            years_left = 1

        salary_by_year = [salary for _ in range(years_left)]
        option_type = "none"
        if "Option" in roster_df.columns:
            opt = row.get("Option")
            if isinstance(opt, str):
                opt = opt.strip().lower()
                if "team" in opt:
                    option_type = "team"
                elif "player" in opt:
                    option_type = "player"

        return Contract(
            player_id=int(pid),
            start_year=_league_season_year(),
            years_total=int(years_left),
            years_left=int(years_left),
            salary_by_year=salary_by_year,
            option_type=option_type,
            guaranteed=True,
        )

    def _generate_default(pid: int, meta: Dict[str, Any]) -> Contract:
        # Deterministic by pid to make simulations reproducible.
        local_rng = random.Random(rng.randint(0, 2**31 - 1) ^ (pid * 2654435761 & 0xFFFFFFFF))
        age = int(meta.get("age", 26) or 26)
        ovr = float(meta.get("overall", 75) or 75)
        salary = float(meta.get("salary", 0.0) or 0.0)

        lo, hi = default_years_left_range
        lo = max(1, int(lo))
        hi = max(lo, int(hi))

        # Simple heuristics:
        # - Very young players likely have more years left
        # - Older/bench players likely have shorter deals
        if age <= 22:
            years_left = local_rng.choice([3, 4])
        elif age <= 26:
            years_left = local_rng.choice([2, 3, 4])
        elif age <= 30:
            years_left = local_rng.choice([1, 2, 3])
        else:
            years_left = local_rng.choice([1, 1, 2])

        years_left = max(lo, min(hi, int(years_left)))

        # Slight growth/decline curve placeholder (will be replaced later)
        slope = 0.03 if age <= 25 else (-0.02 if age >= 32 else 0.0)
        salary_by_year = []
        base = max(0.0, salary)
        for y in range(years_left):
            salary_by_year.append(float(base * ((1.0 + slope) ** y)))

        # Options are rare; add as flavour only
        option_type = "none"
        if years_left >= 3 and ovr >= 82 and local_rng.random() < 0.08:
            option_type = "player"
        elif years_left >= 2 and local_rng.random() < 0.06:
            option_type = "team"

        return Contract(
            player_id=int(pid),
            start_year=_league_season_year(),
            years_total=years_left,
            years_left=years_left,
            salary_by_year=salary_by_year,
            option_type=option_type,
            guaranteed=True,
        )

    # Build contracts
    store.clear()
    for pid_raw, meta_raw in players.items():
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        meta = meta_raw if isinstance(meta_raw, dict) else {}
        contract = _infer_from_roster(pid) or _generate_default(pid, meta)
        set_contract(contract)


# ---- Salary utilities (trade validation helpers) ----
def salary_of_player(player_id: int, *, season_offset: int = 0) -> float:
    c = get_contract(int(player_id))
    if c:
        return c.salary_current(season_offset=season_offset)
    # fallback to player meta
    p = (GAME_STATE.get("players") or {}).get(int(player_id))  # type: ignore[arg-type]
    if isinstance(p, dict):
        try:
            return float(p.get("salary", 0.0) or 0.0)
        except Exception:
            return 0.0
    return 0.0


def total_salary(player_ids: List[int], *, season_offset: int = 0) -> float:
    return float(sum(salary_of_player(pid, season_offset=season_offset) for pid in player_ids))


def team_payroll(team_id: str, *, season_offset: int = 0) -> float:
    """Compute payroll from GAME_STATE player teams; falls back to team_utils if needed."""
    players = GAME_STATE.get("players") or {}
    if isinstance(players, dict) and players:
        total = 0.0
        for pid, meta in players.items():
            if isinstance(meta, dict) and str(meta.get("team_id", "")).upper() == str(team_id).upper():
                try:
                    total += salary_of_player(int(pid), season_offset=season_offset)
                except Exception:
                    pass
        return float(total)

    # Fallback for legacy builds that rely on ROSTER_DF
    try:
        from team_utils import _compute_team_payroll  # type: ignore
        return float(_compute_team_payroll().get(team_id, 0.0))
    except Exception:
        return 0.0


# ---- Season progression ----
def advance_contracts_one_year(*, auto_initialize: bool = True) -> None:
    """
    Advance all contracts by one season:
    - years_left decreases
    - salary_by_year shifts
    - expirings are kept with years_left=0 (FA system can pick them up)
    """
    if auto_initialize:
        ensure_contracts_initialized()

    store = _contracts_store()
    updated: Dict[int, Dict[str, Any]] = {}
    for pid, raw in list(store.items()):
        try:
            c = Contract(**raw)  # type: ignore[arg-type]
        except Exception:
            continue

        if c.years_left <= 0:
            updated[int(pid)] = raw
            continue

        new_years_left = max(0, c.years_left - 1)
        new_salary_by_year = c.salary_by_year[1:] if len(c.salary_by_year) > 1 else c.salary_by_year
        # Keep a terminal salary entry for convenience (not super important)
        if new_years_left == 0:
            new_salary_by_year = []
        updated[int(pid)] = asdict(Contract(
            player_id=c.player_id,
            start_year=c.start_year,
            years_total=c.years_total,
            years_left=new_years_left,
            salary_by_year=new_salary_by_year,
            option_type=c.option_type,
            guaranteed=c.guaranteed,
            notes=c.notes,
        ))

    store.clear()
    store.update(updated)

    # Mirror into player metas for UI/other systems
    players = GAME_STATE.get("players") or {}
    if isinstance(players, dict):
        for pid, meta in players.items():
            if not isinstance(meta, dict):
                continue
            c = get_contract(int(pid))
            if c:
                meta["contract"] = asdict(c)
                meta["salary"] = c.salary_current(0) if c.years_left > 0 else float(meta.get("salary", 0.0) or 0.0)


def debug_contract_summary(team_id: Optional[str] = None, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Convenience helper for debugging/UI: returns a list of contract summaries."""
    ensure_contracts_initialized()
    items: List[Dict[str, Any]] = []
    players = GAME_STATE.get("players") or {}
    for pid, meta in (players.items() if isinstance(players, dict) else []):
        if not isinstance(meta, dict):
            continue
        if team_id and str(meta.get("team_id", "")).upper() != str(team_id).upper():
            continue
        c = get_contract(int(pid))
        if not c:
            continue
        items.append({
            "player_id": int(pid),
            "name": meta.get("name"),
            "team_id": meta.get("team_id"),
            "years_left": c.years_left,
            "salary_current": c.salary_current(0),
            "option_type": c.option_type,
        })
        if len(items) >= int(limit):
            break
    return items
