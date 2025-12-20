from __future__ import annotations

"""trade_ai.py

AI trade orchestrator.

Responsibilities
- decide "when" to run (tick cadence + deadline guard)
- generate candidate trade proposals between AI-controlled teams
- evaluate acceptability using valuation.py
- validate+apply using trade_engine.py

This revision upgrades the AI from a single "1-for-1 plus maybe picks" heuristic to:
- template-based proposal generation (1-for-1, 2-for-1, 1-for-1 + pick)
- simple negotiation (up to 2 counters)
- automatic rule-fix attempts using trade_engine.suggest_fixes (salary filler / roster)
- GM profiles + relationships affecting acceptance thresholds

It is still intentionally lightweight (fast + deterministic enough for a sim loop).
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple
import math
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

from contracts import ensure_contracts_initialized, salary_of_player
from assets import ensure_draft_picks_initialized, team_picks, DraftPick
import valuation
from trade_engine import (
    TradeProposal,
    validate_trade,
    apply_trade,
    suggest_fixes,
)


# ---------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------

@dataclass
class AITuning:
    tick_days: int = 7
    max_trades_per_tick: int = 3

    # Candidate generation
    sellers_per_buyer: int = 5
    targets_per_pair: int = 4

    outgoing_single_candidates: int = 10
    outgoing_pair_pool: int = 8
    proposals_per_pair_cap: int = 25

    # Negotiation
    max_counter_rounds: int = 2
    max_picks_in_offer: int = 2
    allow_young_sweetener: bool = True

    # Decision thresholds (base net utility)
    min_net_contender: float = 1.0
    min_net_neutral: float = 0.5
    min_net_rebuild: float = 0.3

    # Avoid too many blockbuster deals in one tick
    max_star_ovr: float = 89.0

    # Validation-fix safety
    max_fix_attempts_per_proposal: int = 1


def _u(x: str) -> str:
    return str(x or "").upper().strip()


# ---------------------------------------------------------------------
# League timing / market context
# ---------------------------------------------------------------------

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


def _market_context(today: date) -> valuation.MarketContext:
    league = _ensure_league_state()
    rules = league.get("trade_rules") or {}
    dl = rules.get("trade_deadline")
    if not dl:
        return valuation.MarketContext(deadline_pressure=0.0)
    try:
        dl_d = date.fromisoformat(str(dl))
    except Exception:
        return valuation.MarketContext(deadline_pressure=0.0)

    # Simple pressure curve: 0 at 60+ days out, 1 at deadline day
    days_left = (dl_d - today).days
    pressure = 1.0 - (max(0, min(60, days_left)) / 60.0)
    pressure = max(0.0, min(1.0, float(pressure)))
    return valuation.MarketContext(deadline_pressure=pressure)


# ---------------------------------------------------------------------
# Team pools / thresholds
# ---------------------------------------------------------------------

def _teams_for_ai(user_team_id: Optional[str]) -> List[str]:
    ctx = valuation.build_team_contexts()
    ids = sorted(ctx.keys())
    if user_team_id:
        ids = [t for t in ids if _u(t) != _u(user_team_id)]
    return ids


def _base_threshold_for(status: str, tuning: AITuning) -> float:
    s = str(status or "neutral").lower()
    if s.startswith("cont"):
        return tuning.min_net_contender
    if s.startswith("reb"):
        return tuning.min_net_rebuild
    return tuning.min_net_neutral


def threshold_for_team(
    team_ctx: valuation.TeamContext,
    *,
    partner_id: str,
    market: valuation.MarketContext,
    direction: str,
    tuning: AITuning,
) -> float:
    """Acceptance threshold for *net* value.

    Higher threshold => more demanding.

    direction
    - "buy": team is trying to acquire a target
    - "sell": team is giving up the target
    """

    base = float(_base_threshold_for(team_ctx.status, tuning))

    prof = team_ctx.gm_profile or {}
    hardball = float(prof.get("hardball", 0.5) or 0.5)
    aggressiveness = float(prof.get("aggressiveness", 0.5) or 0.5)
    pick_hoarder = float(prof.get("pick_hoarder", 0.5) or 0.5)

    # personality
    base *= (1.0 + (hardball - 0.5) * 0.6)

    # situation
    if str(direction) == "buy" and str(team_ctx.status) == "contender":
        base *= (1.0 - aggressiveness * 0.25)
    if str(direction) == "sell" and str(team_ctx.status) == "rebuild":
        base *= (1.0 + pick_hoarder * 0.20)

    # relationship / rival
    rel = valuation.get_relationship(team_ctx.team_id, partner_id)
    trust = float(rel.get("trust", 0.5) or 0.5)
    rival = bool(rel.get("rival", False))
    rival_pen = float(prof.get("rival_penalty", 0.5) or 0.5)

    base *= (1.0 + (0.5 - trust) * 0.8)
    if rival:
        base += 0.2 * rival_pen

    # market
    if str(team_ctx.status) == "contender" and str(direction) == "buy":
        base *= (1.0 - 0.20 * float(market.deadline_pressure))
    if str(team_ctx.status) == "rebuild" and str(direction) == "sell":
        base *= (1.0 + 0.20 * float(market.deadline_pressure))

    return float(base)


# ---------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------

def _roster_df(team_id: str):
    if ROSTER_DF is None:
        return None
    try:
        return ROSTER_DF[ROSTER_DF["Team"].astype(str).str.upper() == _u(team_id)]
    except Exception:
        return None


def _candidate_targets(seller_id: str, buyer_ctx: valuation.TeamContext, *, tuning: AITuning) -> List[int]:
    """From seller roster, return a shortlist of players buyer might want."""

    df = _roster_df(seller_id)
    if df is None or df.empty:
        return []

    need_pos = set(buyer_ctx.need_positions or ())

    def score_row(row) -> float:
        ovr = float(row.get("OVR", 0.0) or 0.0)
        age = float(row.get("Age", 0.0) or 0.0)
        pos_g = _position_group(str(row.get("POS", "")))
        need_bonus = 3.0 if (need_pos and pos_g in need_pos) else 0.0
        superstar_penalty = 5.0 if ovr >= tuning.max_star_ovr else 0.0
        return ovr + need_bonus - 0.05 * age - superstar_penalty

    rows: List[Tuple[float, int]] = []
    for pid, row in df.iterrows():
        try:
            rows.append((float(score_row(row)), int(pid)))
        except Exception:
            continue
    rows.sort(reverse=True)
    return [pid for _, pid in rows[: int(tuning.targets_per_pair)]]


def _candidate_outgoing_singles(
    buyer_id: str,
    target_salary: float,
    buyer_ctx: valuation.TeamContext,
    *,
    tuning: AITuning,
) -> List[int]:
    """Outgoing single-player candidates from buyer.

    Heuristic: surplus position, low OVR, and salary that can help matching.
    """

    df = _roster_df(buyer_id)
    if df is None or df.empty:
        return []

    surplus = set(buyer_ctx.surplus_positions or ())

    rows: List[Tuple[float, int]] = []
    for pid, row in df.iterrows():
        try:
            pid_i = int(pid)
            ovr = float(row.get("OVR", 0.0) or 0.0)
            pos_g = _position_group(str(row.get("POS", "")))
            sal = float(salary_of_player(pid_i))

            # lower is better
            sal_term = 0.0 if target_salary <= 1e-6 else abs(sal - target_salary) / max(1.0, target_salary)
            surplus_bonus = -0.5 if (surplus and pos_g in surplus) else 0.0
            score = (ovr * 0.08) + (sal_term * 1.2) + surplus_bonus
            rows.append((score, pid_i))
        except Exception:
            continue

    rows.sort()
    return [pid for _, pid in rows[: int(tuning.outgoing_single_candidates)]]


def _candidate_outgoing_pairs(
    buyer_id: str,
    target_salary: float,
    buyer_ctx: valuation.TeamContext,
    *,
    tuning: AITuning,
) -> List[Tuple[int, int]]:
    """Outgoing 2-player combinations (2-for-1 template)."""

    singles = _candidate_outgoing_singles(buyer_id, target_salary, buyer_ctx, tuning=tuning)
    pool = singles[: int(tuning.outgoing_pair_pool)]
    if len(pool) < 2:
        return []

    pairs: List[Tuple[float, Tuple[int, int]]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a = pool[i]
            b = pool[j]
            sal = float(salary_of_player(a)) + float(salary_of_player(b))
            # closeness to target salary
            clos = abs(sal - target_salary) / max(1.0, target_salary)
            pairs.append((clos, (a, b)))

    pairs.sort()
    return [p for _, p in pairs[: max(1, int(tuning.outgoing_single_candidates))]]


# ---------------------------------------------------------------------
# Picks / sweeteners
# ---------------------------------------------------------------------

def _best_pick_sweetener(
    buyer_id: str,
    seller_id: str,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    already_used: Sequence[str] = (),
) -> Optional[str]:
    """Pick to add that is relatively cheap for buyer but helpful for seller."""

    picks = team_picks(buyer_id)
    picks = [p for p in picks if p.pick_id not in set(map(str, already_used))]
    if not picks:
        return None

    scored: List[Tuple[float, str]] = []
    for p in picks:
        try:
            buyer_v = valuation.value_pick_for_team(p.pick_id, buyer_id, team_contexts=team_contexts, market=market)
            seller_v = valuation.value_pick_for_team(p.pick_id, seller_id, team_contexts=team_contexts, market=market)
            # maximize seller gain per buyer cost
            score = float(seller_v - 0.6 * buyer_v)
            scored.append((score, p.pick_id))
        except Exception:
            continue

    if not scored:
        return None

    scored.sort(reverse=True)
    return scored[0][1]


def _best_young_sweetener(
    buyer_id: str,
    seller_id: str,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    exclude_player_ids: Sequence[int] = (),
) -> Optional[int]:
    """Young player to add (cheap for buyer, interesting for seller)."""

    df = _roster_df(buyer_id)
    if df is None or df.empty:
        return None

    exclude = set(int(x) for x in exclude_player_ids)
    cand: List[Tuple[float, int]] = []
    for pid, _row in df.iterrows():
        try:
            pid_i = int(pid)
            if pid_i in exclude:
                continue
            pctx = valuation.build_player_context(pid_i)
            if not pctx:
                continue
            if pctx.age > 24 and pctx.potential < 0.82:
                continue

            buyer_v = valuation.value_player_for_team(pid_i, buyer_id, team_contexts=team_contexts, market=market)
            seller_v = valuation.value_player_for_team(pid_i, seller_id, team_contexts=team_contexts, market=market)
            # prefer low buyer value, decent seller value
            score = float(seller_v - 0.9 * buyer_v)
            cand.append((score, pid_i))
        except Exception:
            continue

    if not cand:
        return None

    cand.sort(reverse=True)
    return cand[0][1]


# ---------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------

def _evaluate_net(
    team_id: str,
    incoming_players: Sequence[int],
    incoming_picks: Sequence[str],
    outgoing_players: Sequence[int],
    outgoing_picks: Sequence[str],
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    partner_id: Optional[str] = None,
) -> float:
    in_val = valuation.package_value_for_team(
        team_id,
        player_ids=list(incoming_players),
        pick_ids=list(incoming_picks),
        team_contexts=team_contexts,
        market=market,
        trade_partner_id=partner_id,
    )
    out_val = valuation.package_value_for_team(
        team_id,
        player_ids=list(outgoing_players),
        pick_ids=list(outgoing_picks),
        team_contexts=team_contexts,
        market=market,
        trade_partner_id=partner_id,
    )
    return float(in_val - out_val)


# ---------------------------------------------------------------------
# Validation fix helper
# ---------------------------------------------------------------------

def _try_fix_proposal(
    proposal: TradeProposal,
    validation,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    tuning: AITuning,
) -> Optional[TradeProposal]:
    """Attempt a single auto-fix using trade_engine.suggest_fixes."""

    fixes = suggest_fixes(proposal, validation)
    if not fixes:
        return None

    # Only one fix attempt per proposal for predictability.
    fix = fixes[0]

    def pick_filler(team_id: str, amount_needed: float = 0.0, exclude: Sequence[int] = ()) -> Optional[int]:
        df = _roster_df(team_id)
        if df is None or df.empty:
            return None
        exclude_set = set(int(x) for x in exclude)
        rows: List[Tuple[float, int]] = []
        for pid, row in df.iterrows():
            try:
                pid_i = int(pid)
                if pid_i in exclude_set:
                    continue
                sal = float(salary_of_player(pid_i))
                if amount_needed > 0 and sal < amount_needed * 0.85:
                    continue
                # low value to its own team
                own_v = valuation.value_player_for_team(pid_i, team_id, team_contexts=team_contexts, market=market)
                score = float(own_v) + (0.000001 * sal)
                rows.append((score, pid_i))
            except Exception:
                continue
        if not rows:
            return None
        rows.sort()  # low own value first
        return rows[0][1]

    # Apply fix
    if fix.kind in ("add_outgoing_salary", "add_outgoing_player"):
        side = str(fix.team_side).upper()
        amt = float(fix.amount or 0.0)
        if side == "A":
            filler = pick_filler(proposal.team_a, amt, exclude=proposal.send_a_players)
            if filler is None:
                return None
            prop2 = TradeProposal(
                team_a=proposal.team_a,
                team_b=proposal.team_b,
                send_a_players=list(dict.fromkeys([*proposal.send_a_players, int(filler)])),
                send_a_picks=list(proposal.send_a_picks),
                send_b_players=list(proposal.send_b_players),
                send_b_picks=list(proposal.send_b_picks),
                date_str=proposal.date_str,
            )
        else:
            filler = pick_filler(proposal.team_b, amt, exclude=proposal.send_b_players)
            if filler is None:
                return None
            prop2 = TradeProposal(
                team_a=proposal.team_a,
                team_b=proposal.team_b,
                send_a_players=list(proposal.send_a_players),
                send_a_picks=list(proposal.send_a_picks),
                send_b_players=list(dict.fromkeys([*proposal.send_b_players, int(filler)])),
                send_b_picks=list(proposal.send_b_picks),
                date_str=proposal.date_str,
            )

        v2 = validate_trade(prop2)
        if v2.ok:
            return prop2
        return None

    return None


# ---------------------------------------------------------------------
# Offer generation + negotiation
# ---------------------------------------------------------------------

def _negotiate_for_acceptance(
    proposal: TradeProposal,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    tuning: AITuning,
    template: str,
    rng: random.Random,
) -> Optional[Tuple[TradeProposal, Dict[str, Any]]]:
    """Try to get both teams over their thresholds (may add sweeteners).

    Returns (final_proposal, evaluation_meta) if accepted.
    """

    buyer_id = _u(proposal.team_a)
    seller_id = _u(proposal.team_b)
    buyer_ctx = team_contexts.get(buyer_id)
    seller_ctx = team_contexts.get(seller_id)
    if not buyer_ctx or not seller_ctx:
        return None

    # First, validate. If invalid, attempt a single fix.
    v = validate_trade(proposal)
    if not v.ok:
        if tuning.max_fix_attempts_per_proposal > 0:
            fixed = _try_fix_proposal(proposal, v, team_contexts=team_contexts, market=market, tuning=tuning)
            if fixed is None:
                return None
            proposal = fixed
            v = validate_trade(proposal)
            if not v.ok:
                return None
        else:
            return None

    # Compute thresholds
    buyer_thr = threshold_for_team(buyer_ctx, partner_id=seller_id, market=market, direction="buy", tuning=tuning)
    seller_thr = threshold_for_team(seller_ctx, partner_id=buyer_id, market=market, direction="sell", tuning=tuning)

    def nets(p: TradeProposal) -> Tuple[float, float]:
        buyer_net = _evaluate_net(
            buyer_id,
            incoming_players=p.send_b_players,
            incoming_picks=p.send_b_picks,
            outgoing_players=p.send_a_players,
            outgoing_picks=p.send_a_picks,
            team_contexts=team_contexts,
            market=market,
            partner_id=seller_id,
        )
        seller_net = _evaluate_net(
            seller_id,
            incoming_players=p.send_a_players,
            incoming_picks=p.send_a_picks,
            outgoing_players=p.send_b_players,
            outgoing_picks=p.send_b_picks,
            team_contexts=team_contexts,
            market=market,
            partner_id=buyer_id,
        )
        return float(buyer_net), float(seller_net)

    buyer_net, seller_net = nets(proposal)
    if buyer_net >= buyer_thr and seller_net >= seller_thr:
        meta = {
            "template": template,
            "buyer_net": buyer_net,
            "seller_net": seller_net,
            "buyer_threshold": buyer_thr,
            "seller_threshold": seller_thr,
            "validation": {"reasons": list(v.reasons)},
        }
        return proposal, meta

    # Negotiation: if seller rejects, buyer can sweeten (picks / young)
    for _round in range(int(tuning.max_counter_rounds)):
        if seller_net >= seller_thr:
            break

        need_delta = float(seller_thr - seller_net)

        # choose sweetener type based on seller profile
        sp = seller_ctx.gm_profile or {}
        pick_pref = float(sp.get("pick_hoarder", 0.5) or 0.5) + (0.35 if seller_ctx.status == "rebuild" else 0.0)
        youth_pref = float(sp.get("youth_bias", 0.5) or 0.5)

        new_proposal: Optional[TradeProposal] = None

        if pick_pref >= youth_pref:
            if len(proposal.send_a_picks) < int(tuning.max_picks_in_offer):
                pk = _best_pick_sweetener(
                    buyer_id,
                    seller_id,
                    team_contexts=team_contexts,
                    market=market,
                    already_used=proposal.send_a_picks,
                )
                if pk:
                    new_proposal = TradeProposal(
                        team_a=proposal.team_a,
                        team_b=proposal.team_b,
                        send_a_players=list(proposal.send_a_players),
                        send_a_picks=[*proposal.send_a_picks, str(pk)],
                        send_b_players=list(proposal.send_b_players),
                        send_b_picks=list(proposal.send_b_picks),
                        date_str=proposal.date_str,
                    )

        if new_proposal is None and tuning.allow_young_sweetener:
            young = _best_young_sweetener(
                buyer_id,
                seller_id,
                team_contexts=team_contexts,
                market=market,
                exclude_player_ids=proposal.send_a_players,
            )
            if young is not None:
                new_proposal = TradeProposal(
                    team_a=proposal.team_a,
                    team_b=proposal.team_b,
                    send_a_players=[*proposal.send_a_players, int(young)],
                    send_a_picks=list(proposal.send_a_picks),
                    send_b_players=list(proposal.send_b_players),
                    send_b_picks=list(proposal.send_b_picks),
                    date_str=proposal.date_str,
                )

        if new_proposal is None:
            break

        v2 = validate_trade(new_proposal)
        if not v2.ok:
            fixed = _try_fix_proposal(new_proposal, v2, team_contexts=team_contexts, market=market, tuning=tuning)
            if fixed is None:
                break
            new_proposal = fixed
            v2 = validate_trade(new_proposal)
            if not v2.ok:
                break

        buyer_net2, seller_net2 = nets(new_proposal)

        # Buyer can't destroy itself to get deal done.
        if buyer_net2 < buyer_thr:
            break

        proposal = new_proposal
        buyer_net, seller_net = buyer_net2, seller_net2

        if buyer_net >= buyer_thr and seller_net >= seller_thr:
            meta = {
                "template": template,
                "buyer_net": buyer_net,
                "seller_net": seller_net,
                "buyer_threshold": buyer_thr,
                "seller_threshold": seller_thr,
                "negotiation_rounds": _round + 1,
                "need_delta_initial": need_delta,
                "validation": {"reasons": list(v2.reasons)},
            }
            return proposal, meta

    return None


def _build_proposals_for_target(
    buyer_id: str,
    seller_id: str,
    target_id: int,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    tuning: AITuning,
    rng: random.Random,
) -> List[Tuple[TradeProposal, str]]:
    """Generate candidate proposals for a specific target."""

    buyer_ctx = team_contexts.get(_u(buyer_id))
    if not buyer_ctx:
        return []

    target_salary = float(salary_of_player(int(target_id)))

    singles = _candidate_outgoing_singles(buyer_id, target_salary, buyer_ctx, tuning=tuning)
    pairs = _candidate_outgoing_pairs(buyer_id, target_salary, buyer_ctx, tuning=tuning)

    proposals: List[Tuple[TradeProposal, str]] = []

    # T1: 1-for-1
    for out_pid in singles:
        proposals.append((
            TradeProposal(
                team_a=_u(buyer_id),
                team_b=_u(seller_id),
                send_a_players=[int(out_pid)],
                send_a_picks=[],
                send_b_players=[int(target_id)],
                send_b_picks=[],
                date_str=None,
            ),
            "1for1",
        ))

    # T2: 2-for-1 (buyer sends 2)
    for (p1, p2) in pairs:
        proposals.append((
            TradeProposal(
                team_a=_u(buyer_id),
                team_b=_u(seller_id),
                send_a_players=[int(p1), int(p2)],
                send_a_picks=[],
                send_b_players=[int(target_id)],
                send_b_picks=[],
                date_str=None,
            ),
            "2for1",
        ))

    # Shuffle a bit so AI is less repetitive
    rng.shuffle(proposals)
    return proposals[: int(tuning.proposals_per_pair_cap)]


def _attempt_trade_between(
    buyer_id: str,
    seller_id: str,
    *,
    team_contexts: Dict[str, valuation.TeamContext],
    market: valuation.MarketContext,
    tuning: AITuning,
    rng: random.Random,
) -> Optional[Tuple[TradeProposal, Dict[str, Any]]]:
    """Try to create and accept one trade between buyer and seller."""

    buyer_ctx = team_contexts.get(_u(buyer_id))
    seller_ctx = team_contexts.get(_u(seller_id))
    if not buyer_ctx or not seller_ctx:
        return None

    targets = _candidate_targets(seller_id, buyer_ctx, tuning=tuning)
    rng.shuffle(targets)

    for target_id in targets:
        proposals = _build_proposals_for_target(
            buyer_id,
            seller_id,
            target_id,
            team_contexts=team_contexts,
            market=market,
            tuning=tuning,
            rng=rng,
        )
        for prop, template in proposals:
            accepted = _negotiate_for_acceptance(
                prop,
                team_contexts=team_contexts,
                market=market,
                tuning=tuning,
                template=template,
                rng=rng,
            )
            if accepted is None:
                continue
            return accepted

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

    # Ensure GM profiles / relationships exist
    valuation.ensure_gm_profiles_initialized()
    valuation.ensure_relationships_initialized()

    team_contexts = valuation.build_team_contexts()
    market = _market_context(today)

    teams = _teams_for_ai(user_team_id)
    if len(teams) < 2:
        _mark_tick(today)
        return

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

    if not buyers:
        buyers = neutrals[:]
    if not sellers:
        sellers = neutrals[:]

    rng = random.Random()
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

        probe = [s for s in sellers if _u(s) not in involved and _u(s) != _u(buyer_id)]
        rng.shuffle(probe)
        probe = probe[: int(tuning.sellers_per_buyer)]

        for seller_id in probe:
            if trades_done >= max_trades:
                break
            if _u(seller_id) in involved:
                continue

            accepted = _attempt_trade_between(
                buyer_id,
                seller_id,
                team_contexts=team_contexts,
                market=market,
                tuning=tuning,
                rng=rng,
            )
            if accepted is None:
                continue

            proposal, meta = accepted
            try:
                proposal = TradeProposal(**{**proposal.__dict__, "date_str": today.isoformat()})
                apply_trade(proposal, record_transaction=True, record_weekly_news=True, evaluation=meta)
                trades_done += 1
                involved.add(_u(buyer_id))
                involved.add(_u(seller_id))
                break
            except Exception:
                continue

    _mark_tick(today)


def _run_ai_gm_tick_if_needed(current_date: date, user_team_id: Optional[str] = None) -> None:
    """Compatibility shim (older league_sim used trades_ai)."""

    tuning = AITuning()
    if not _should_tick(current_date, tuning=tuning):
        return
    run_ai_gm_tick(current_date, user_team_id=user_team_id, tuning=tuning)
