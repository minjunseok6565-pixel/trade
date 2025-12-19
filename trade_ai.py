from __future__ import annotations

"""
trade_ai.py

AI trade orchestrator.

Responsibilities:
- decide "when" to run (tick cadence + deadline guard)
- generate candidate trade proposals between AI-controlled teams
- evaluate acceptability using valuation.py
- validate+apply using trade_engine.py

Important design choice:
- trade_ai.py does NOT mutate state directly (except via trade_engine.apply_trade).
- keep valuation separate (valuation.py) so you can tune without touching the engine.

This is an MVP draft. You will likely extend:
- 2-for-1, 3-team, pick swaps
- richer needs/fit/contract logic
- negotiation (counteroffers)
- market dynamics (deadline premium, scarcity)
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple
import random

try:
    from config import ROSTER_DF
except Exception:  # pragma: no cover
    ROSTER_DF = None  # type: ignore

try:
    from state import _ensure_league_state
except Exception:  # pragma: no cover
    def _ensure_league_state() -> Dict[str, Any]:
        return {}

try:
    from team_utils import _init_players_and_teams_if_needed, _position_group
except Exception:  # pragma: no cover
    def _init_players_and_teams_if_needed() -> None:
        return None

    def _position_group(pos: str) -> str:
        pos_u = str(pos or "").upper()
        if pos_u in ("PG", "SG"):
            return "guard"
        if pos_u in ("SF", "PF"):
            return "wing"
        return "big"

from contracts import ensure_contracts_initialized, salary_of_player, team_payroll
from assets import ensure_draft_picks_initialized, team_picks
import valuation
from trade_engine import TradeProposal, validate_trade, apply_trade


# ---------------------------------------------------------------------
# Tuning knobs (reasonable defaults)
# ---------------------------------------------------------------------

@dataclass
class AITuning:
    tick_days: int = 7
    max_trades_per_tick: int = 3

    # Candidate generation
    sellers_per_buyer: int = 4
    targets_per_pair: int = 3
    outgoing_candidates: int = 8

    # Decision thresholds (net utility)
    min_net_contender: float = 1.0
    min_net_neutral: float = 0.5
    min_net_rebuild: float = 0.3

    # Allow AI to include up to N picks to sweeten a deal
    max_picks_in_offer: int = 2

    # Avoid too many blockbuster deals in one tick
    max_star_ovr: float = 89.0  # if you don't want AI to constantly trade superstars


def _u(x: str) -> str:
    return str(x or "").upper().strip()


def _past_deadline(today: date) -> bool:
    league = _ensure_league_state()
    rules = league.get("trade_rules") or {}
    dl = rules.get("trade_deadline")
    if not dl:
        return False
    try:
        dl_d = date.fromisoformat(str(dl))
    except Exception:
        return False
    return today > dl_d


def _should_tick(today: date, *, tuning: AITuning) -> bool:
    league = _ensure_league_state()
    last = league.get("last_gm_tick_date")
    if not last:
        return True
    try:
        last_d = date.fromisoformat(str(last))
    except Exception:
        return True
    return (today - last_d).days >= int(tuning.tick_days)


def _mark_tick(today: date) -> None:
    league = _ensure_league_state()
    league["last_gm_tick_date"] = today.isoformat()


def _teams_for_ai(user_team_id: Optional[str]) -> List[str]:
    # Prefer TEAM meta in GAME_STATE via valuation contexts
    ctx = valuation.build_team_contexts()
    ids = sorted(ctx.keys())
    if user_team_id:
        ids = [t for t in ids if _u(t) != _u(user_team_id)]
    return ids


def _threshold_for(status: str, tuning: AITuning) -> float:
    s = str(status or "neutral").lower()
    if s.startswith("cont"):
        return tuning.min_net_contender
    if s.startswith("reb"):
        return tuning.min_net_rebuild
    return tuning.min_net_neutral


def _roster_df(team_id: str):
    if ROSTER_DF is None:
        return None
    try:
        return ROSTER_DF[ROSTER_DF["Team"].astype(str).str.upper() == _u(team_id)]
    except Exception:
        return None


def _candidate_targets(seller_id: str, buyer_ctx: valuation.TeamContext, *, tuning: AITuning) -> List[int]:
    df = _roster_df(seller_id)
    if df is None or df.empty:
        return []

    need_pos = set(buyer_ctx.need_positions or ())
    # Prefer players that match buyer needs
    def score_row(row) -> float:
        ovr = float(row.get("OVR", 0.0))
        age = float(row.get("Age", 0.0) or 0.0)
        # if position group matches need, boost
        pos_g = _position_group(str(row.get("POS", "")))
        need_bonus = 3.0 if (need_pos and pos_g in need_pos) else 0.0
        # avoid constant superstar swaps unless you want that chaos
        superstar_penalty = 5.0 if ovr >= tuning.max_star_ovr else 0.0
        return ovr + need_bonus - 0.05 * age - superstar_penalty

    # Take top-K by heuristic score
    rows = []
    for pid, row in df.iterrows():
        try:
            rows.append((float(score_row(row)), int(pid)))
        except Exception:
            continue
    rows.sort(reverse=True)
    return [pid for _, pid in rows[: int(tuning.targets_per_pair)]]


def _candidate_outgoing_players(buyer_id: str, target_salary: float, buyer_ctx: valuation.TeamContext, *, tuning: AITuning) -> List[int]:
    df = _roster_df(buyer_id)
    if df is None or df.empty:
        return []

    surplus = set(buyer_ctx.surplus_positions or ())
    # Heuristic: prefer surplus-position players, low OVR, and salary similar to target
    rows = []
    for pid, row in df.iterrows():
        try:
            pid_i = int(pid)
            ovr = float(row.get("OVR", 0.0))
            pos_g = _position_group(str(row.get("POS", "")))
            sal = float(salary_of_player(pid_i))
            # similarity in salary (closer is better)
            if target_salary <= 1e-6:
                sal_term = 0.0
            else:
                sal_term = abs(sal - target_salary) / max(1.0, target_salary)
            surplus_bonus = -0.5 if (surplus and pos_g in surplus) else 0.0
            # low value players are more likely to be shopped
            score = (ovr * 0.08) + (sal_term * 1.2) + surplus_bonus
            rows.append((score, pid_i))
        except Exception:
            continue
    rows.sort()  # lower score = better outgoing candidate
    return [pid for _, pid in rows[: int(tuning.outgoing_candidates)]]


def _best_pick_to_add(buyer_id: str, buyer_ctx: valuation.TeamContext) -> Optional[str]:
    # "best" here means cheapest for the buyer to give away (lowest value to buyer)
    picks = team_picks(buyer_id)
    if not picks:
        return None
    scored: List[Tuple[float, str]] = []
    for p in picks:
        try:
            pid = str(p.get("pick_id"))
            v = valuation.value_pick_for_team(pid, buyer_id, team_contexts={buyer_id: buyer_ctx})
            scored.append((float(v), pid))
        except Exception:
            continue
    if not scored:
        return None
    scored.sort()  # lowest value to buyer => easiest to give
    return scored[0][1]


def _evaluate_net(
    team_id: str,
    incoming_players: Sequence[int],
    incoming_picks: Sequence[str],
    outgoing_players: Sequence[int],
    outgoing_picks: Sequence[str],
    *,
    team_contexts: Dict[str, valuation.TeamContext],
) -> float:
    in_val = valuation.package_value_for_team(team_id, player_ids=list(incoming_players), pick_ids=list(incoming_picks), team_contexts=team_contexts)
    out_val = valuation.package_value_for_team(team_id, player_ids=list(outgoing_players), pick_ids=list(outgoing_picks), team_contexts=team_contexts)
    return float(in_val - out_val)


def _build_and_test_proposals(
    buyer_id: str,
    seller_id: str,
    target_id: int,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    tuning: AITuning,
) -> Optional[TradeProposal]:
    buyer_ctx = team_contexts.get(_u(buyer_id))
    seller_ctx = team_contexts.get(_u(seller_id))
    if not buyer_ctx or not seller_ctx:
        return None

    target_salary = float(salary_of_player(int(target_id)))
    outgoing_candidates = _candidate_outgoing_players(buyer_id, target_salary, buyer_ctx, tuning=tuning)
    if not outgoing_candidates:
        return None

    # Start with 1-for-1, then optionally add picks to satisfy seller
    for out_pid in outgoing_candidates:
        proposal = TradeProposal(
            team_a=_u(buyer_id),
            team_b=_u(seller_id),
            send_a_players=[int(out_pid)],
            send_a_picks=[],
            send_b_players=[int(target_id)],
            send_b_picks=[],
            date_str=None,
        )

        # Validate rules first (cheap filter)
        v = validate_trade(proposal)
        if not v.ok:
            # Salary mismatch can sometimes be solved by using another outgoing candidate; keep trying.
            continue

        buyer_net = _evaluate_net(_u(buyer_id), [target_id], [], [out_pid], [], team_contexts=team_contexts)
        seller_net = _evaluate_net(_u(seller_id), [out_pid], [], [target_id], [], team_contexts=team_contexts)

        if buyer_net >= _threshold_for(buyer_ctx.status, tuning) and seller_net >= _threshold_for(seller_ctx.status, tuning):
            return proposal

        # If seller isn't happy, add up to N picks (one at a time), re-evaluate.
        if seller_net < _threshold_for(seller_ctx.status, tuning):
            added: List[str] = []
            for _ in range(int(tuning.max_picks_in_offer)):
                pick_id = _best_pick_to_add(buyer_id, buyer_ctx)
                if not pick_id or pick_id in added:
                    break
                added.append(pick_id)
                proposal2 = TradeProposal(
                    team_a=_u(buyer_id),
                    team_b=_u(seller_id),
                    send_a_players=[int(out_pid)],
                    send_a_picks=list(added),
                    send_b_players=[int(target_id)],
                    send_b_picks=[],
                    date_str=None,
                )
                v2 = validate_trade(proposal2)
                if not v2.ok:
                    continue

                buyer_net2 = _evaluate_net(_u(buyer_id), [target_id], [], [out_pid], list(added), team_contexts=team_contexts)
                seller_net2 = _evaluate_net(_u(seller_id), [out_pid], list(added), [target_id], [], team_contexts=team_contexts)

                if buyer_net2 >= _threshold_for(buyer_ctx.status, tuning) and seller_net2 >= _threshold_for(seller_ctx.status, tuning):
                    return proposal2

    return None


# ---------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------

def run_ai_gm_tick(today: date, *, user_team_id: Optional[str] = None, tuning: Optional[AITuning] = None) -> None:
    """Run one AI GM tick (may execute 0..N trades)."""
    tuning = tuning or AITuning()

    if _past_deadline(today):
        _mark_tick(today)
        return

    _init_players_and_teams_if_needed()
    ensure_contracts_initialized()
    ensure_draft_picks_initialized()

    team_contexts = valuation.build_team_contexts()

    teams = _teams_for_ai(user_team_id)
    if len(teams) < 2:
        _mark_tick(today)
        return

    # Determine buyer/seller pools
    buyers: List[str] = []
    sellers: List[str] = []
    neutrals: List[str] = []

    for tid in teams:
        ctx = team_contexts.get(_u(tid))
        if not ctx:
            continue
        st = str(ctx.status or "neutral").lower()
        if st.startswith("cont") or ctx.win_pct >= 0.6:
            buyers.append(tid)
        elif st.startswith("reb") or ctx.win_pct <= 0.42:
            sellers.append(tid)
        else:
            neutrals.append(tid)

    # If pools are empty, allow neutral trades very rarely
    if not buyers:
        buyers = neutrals[:]
    if not sellers:
        sellers = neutrals[:]

    rng = random.Random()
    # If you want reproducibility, you can store a seed in league state
    league = _ensure_league_state()
    seed = league.get("rng_seed")
    if seed is not None:
        try:
            rng.seed(int(seed))
        except Exception:
            pass

    max_trades = int(tuning.max_trades_per_tick)
    trades_done = 0
    involved: set[str] = set()

    rng.shuffle(buyers)
    rng.shuffle(sellers)

    for buyer_id in buyers:
        if trades_done >= max_trades:
            break
        if _u(buyer_id) in involved:
            continue

        buyer_ctx = team_contexts.get(_u(buyer_id))
        if not buyer_ctx:
            continue

        # Pick a few sellers to probe
        probe = [s for s in sellers if _u(s) not in involved and _u(s) != _u(buyer_id)]
        rng.shuffle(probe)
        probe = probe[: int(tuning.sellers_per_buyer)]
        for seller_id in probe:
            if trades_done >= max_trades:
                break
            if _u(seller_id) in involved:
                continue
            seller_ctx = team_contexts.get(_u(seller_id))
            if not seller_ctx:
                continue

            # Generate a few targets from this seller
            targets = _candidate_targets(seller_id, buyer_ctx, tuning=tuning)
            rng.shuffle(targets)
            for target_id in targets:
                if trades_done >= max_trades:
                    break

                proposal = _build_and_test_proposals(
                    buyer_id,
                    seller_id,
                    target_id,
                    team_contexts=team_contexts,
                    tuning=tuning,
                )
                if proposal is None:
                    continue

                # Apply
                try:
                    proposal = TradeProposal(**{**proposal.__dict__, "date_str": today.isoformat()})
                    apply_trade(proposal, record_transaction=True, record_weekly_news=True)
                    trades_done += 1
                    involved.add(_u(buyer_id))
                    involved.add(_u(seller_id))
                    break
                except Exception:
                    # If apply fails unexpectedly, ignore and continue.
                    continue
            if _u(buyer_id) in involved:
                break

    _mark_tick(today)


def _run_ai_gm_tick_if_needed(current_date: date, user_team_id: Optional[str] = None) -> None:
    """Compatibility shim with the old trades_ai module name."""
    tuning = AITuning()
    if not _should_tick(current_date, tuning=tuning):
        return
    run_ai_gm_tick(current_date, user_team_id=user_team_id, tuning=tuning)
