from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import hashlib
import json
import math
import random
from datetime import date

try:
    from schema import normalize_team_id  # type: ignore
except Exception:  # pragma: no cover
    def normalize_team_id(x: str, strict: bool = False) -> str:  # type: ignore
        return str(x or "").upper()

from ..errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    SWAP_NOT_FOUND,
    SWAP_INVALID,
    DUPLICATE_ASSET,
)

from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, TeamDealEvaluation, DealVerdict

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    IncomingPlayerRef,
    StepienHelper,
)

from .dealgen_types import DealGeneratorConfig, DealProposal, DealGenerationStats, _Budgets, _DealSpec, _GenState
from .dealgen_utils import _deal_complexity_exceeds, _is_locked
from .dealgen_scoring import _spec_to_deal

def _repair_until_valid(state: _GenState, spec: _DealSpec, *, budgets: _Budgets) -> Optional[Deal]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx

    current = spec.copy()

    for attempt in range(max(1, budgets.max_repairs + 1)):
        deal = _spec_to_deal(state, current)
        if deal is None:
            return None

        # complexity guard (early)
        if _deal_complexity_exceeds(cfg, deal):
            return None

        try:
            tick_ctx.validate_deal(deal, allow_locked_by_deal_id=state.allow_locked_by_deal_id)
            state.stats.validations += 1
            return deal
        except TradeError as err:
            state.stats.validations += 1
            state.stats.record_error(err)

            # fatal / non-repairable errors (ban and prune)
            if err.code in (ASSET_LOCKED,):
                state.stats.pruned_locked += 1
                _ban_from_error(state, err)
                return None
            if err.code in (PLAYER_NOT_OWNED, PICK_NOT_OWNED, SWAP_NOT_OWNED, SWAP_NOT_FOUND, SWAP_INVALID, DUPLICATE_ASSET):
                _ban_from_error(state, err)
                return None

            # roster limit: remove lowest-impact filler from receiver side
            if err.code == ROSTER_LIMIT:
                if not _repair_roster_limit(state, current, err):
                    return None
                continue

            # deal invalidated: inspect rule/method/reason
            details = err.details if isinstance(err.details, dict) else {}
            rule = str(details.get("rule") or "")
            reason = str(details.get("reason") or "")
            method = str(details.get("method") or "")
            team_id = _canon_team_id(details.get("team_id") or "")

            if rule == "salary_matching":
                if method == "second_apron_one_for_one":
                    if not _repair_second_apron_one_for_one(state, current, team_id=team_id):
                        return None
                    continue
                if not _repair_salary_matching(state, current, details):
                    return None
                continue

            if rule in ("player_eligibility", "return_to_trading_team_same_season"):
                state.stats.pruned_ineligible += 1
                _ban_from_error(state, err)
                return None

            if rule == "pick_rules":
                if reason in ("stepien_violation", "pick_too_far"):
                    state.stats.pruned_stepien += 1
                    if not _repair_pick_rules(state, current, details):
                        _ban_from_error(state, err)
                        return None
                    continue
                # other pick rule issues: prune
                _ban_from_error(state, err)
                return None

            # unknown invalidation => prune
            return None

    return None


def _ban_from_error(state: _GenState, err: TradeError) -> None:
    details = err.details if isinstance(err.details, dict) else {}
    rule = str(details.get("rule") or "")
    if rule == "return_to_trading_team_same_season":
        pid = details.get("player_id")
        to_team = _canon_team_id(details.get("to_team") or "")
        if pid and to_team:
            state.banned_receivers_by_player[str(pid)].add(to_team)
    # best-effort: ban offending asset id
    pid = details.get("player_id")
    if pid:
        team_id = _canon_team_id(details.get("team_id") or "")
        if team_id:
            state.banned_players[team_id].add(str(pid))
    pick_id = details.get("pick_id")
    if pick_id:
        team_id = _canon_team_id(details.get("team_id") or "")
        if team_id:
            state.banned_picks[team_id].add(str(pick_id))
    swap_id = details.get("swap_id")
    if swap_id:
        team_id = _canon_team_id(details.get("team_id") or "")
        if team_id:
            state.banned_swaps[team_id].add(str(swap_id))


def _repair_roster_limit(state: _GenState, spec: _DealSpec, err: TradeError) -> bool:
    details = err.details if isinstance(err.details, dict) else {}
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        return False

    # count is the post-trade roster size for the failing team
    try:
        count = int(details.get("count") or 0)
    except Exception:
        count = 0

    buyer_id = spec.buyer_id
    seller_id = spec.seller_id
    catalog = state.catalog

    # (1) Prefer reducing incoming players for the violating team when possible.
    # This keeps the deal structure simple (especially for 2-for-1 archetypes).
    if team_id == buyer_id and len(spec.seller_players_out) > 1:
        spec.seller_players_out = spec.seller_players_out[:1]
        spec.tags.append("repair:roster_trim_seller")
        return True

    if team_id == seller_id and len(spec.buyer_players_out) > 1:
        spec.buyer_players_out = spec.buyer_players_out[:1]
        spec.tags.append("repair:roster_trim_buyer")
        return True

    # (2) Common case: team is already at 15 and receives 1 player (new_count == 16).
    # Repair by having the violating team send out a low-value filler to make room.
    if team_id == buyer_id:
        buyer_out = catalog.outgoing_by_team.get(buyer_id)
        if buyer_out is None:
            return False

        # Don't create multi-player outgoing if any current outgoing is aggregation solo-only.
        if any(buyer_out.players.get(pid) and buyer_out.players[pid].aggregation_solo_only for pid in spec.buyer_players_out):
            return False

        # If count isn't available, still attempt at most one send-out.
        need_send = 1 if count <= 0 else max(0, count - 15)
        if need_send <= 0:
            need_send = 1
        need_send = min(need_send, 1)

        filler_cands = _collect_buyer_player_candidates(state, buyer_out, receiver_team_id=seller_id)["filler"]
        used = set(spec.buyer_players_out)
        allow_solo_only = len(spec.buyer_players_out) == 0
        for c in filler_cands:
            if c.player_id in used:
                continue
            if c.aggregation_solo_only and not allow_solo_only:
                continue
            spec.buyer_players_out.append(c.player_id)
            spec.tags.append("repair:roster_send_filler_buyer")
            return True

        return False

    if team_id == seller_id:
        # Rare in our generator (mostly triggered by 2-for-1 offers).
        # If trimming didn't help, attempt to have seller send out one extra low-value player.
        seller_out = catalog.outgoing_by_team.get(seller_id)
        if seller_out is None:
            return False

        if any(seller_out.players.get(pid) and seller_out.players[pid].aggregation_solo_only for pid in spec.seller_players_out):
            return False

        need_send = 1 if count <= 0 else max(0, count - 15)
        if need_send <= 0:
            need_send = 1
        need_send = min(need_send, 1)

        filler_cands = _collect_buyer_player_candidates(state, seller_out, receiver_team_id=buyer_id)["filler"]
        used = set(spec.seller_players_out)
        allow_solo_only = len(spec.seller_players_out) == 0
        for c in filler_cands:
            if c.player_id in used:
                continue
            if c.aggregation_solo_only and not allow_solo_only:
                continue
            spec.seller_players_out.append(c.player_id)
            spec.tags.append("repair:roster_send_filler_seller")
            return True

        return False

    return False


def _repair_second_apron_one_for_one(state: _GenState, spec: _DealSpec, *, team_id: str) -> bool:
    """Repair for SECOND_APRON one-for-one restriction.

    SalaryMatchingRule enforces that a SECOND_APRON team cannot trade if it would have
    outgoing_players > 1 OR incoming_players > 1.

    In a 2-team deal, that implies BOTH sides must be capped at 1 player-out, because:
      - incoming_players for one team == other team's outgoing players (players assets)
    We only trim lists that actually exceed 1.
    """
    tid = _canon_team_id(team_id or "")
    changed = False

    # For 2-team deals, satisfying the apron team's incoming/outgoing constraints
    # requires both lists to be <= 1. Keep the "primary" player on each side.
    if len(spec.seller_players_out) > 1:
        spec.seller_players_out = spec.seller_players_out[:1]
        changed = True
    if len(spec.buyer_players_out) > 1:
        spec.buyer_players_out = spec.buyer_players_out[:1]
        changed = True

    if changed:
        spec.tags.append(f"repair:second_apron_1for1:{tid or 'unknown'}")
    return changed


def _repair_salary_matching(state: _GenState, spec: _DealSpec, details: Dict[str, Any]) -> bool:
    """Meta-driven salary-match repair (bounded).

    Uses SalaryMatchingRule details:
      - team_id, status, outgoing_salary, incoming_salary, allowed_in, method
    Strategy:
      - If failing team needs MORE outgoing salary: add the cheapest-possible salary filler close to the deficit.
        If SECOND_APRON, prefer swapping to a higher-salary single outgoing instead of adding a 2nd player.
      - If failing team needs LESS incoming salary: trim extra incoming players first, then swap to a cheaper player.
    """
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        return False

    buyer_id = spec.buyer_id
    seller_id = spec.seller_id
    catalog = state.catalog

    buyer_out = catalog.outgoing_by_team.get(buyer_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if buyer_out is None or seller_out is None:
        return False

    # Pull numeric details (dollars)
    try:
        incoming_salary = float(details.get("incoming_salary") or 0.0)
    except Exception:
        incoming_salary = 0.0
    try:
        outgoing_salary = float(details.get("outgoing_salary") or 0.0)
    except Exception:
        outgoing_salary = 0.0
    try:
        allowed_in = float(details.get("allowed_in") or 0.0)
    except Exception:
        allowed_in = 0.0

    status = str(details.get("status") or "")
    method = str(details.get("method") or "")

    def _salary_m(c: PlayerTradeCandidate) -> float:
        try:
            return float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _value(c: PlayerTradeCandidate) -> float:
        try:
            return float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _pool_for(out_cat: TeamOutgoingCatalog, receiver_team_id: str) -> List[PlayerTradeCandidate]:
        packs = _collect_buyer_player_candidates(state, out_cat, receiver_team_id=receiver_team_id)
        filler = list(packs.get("filler") or [])
        match = list(packs.get("match") or [])
        seen: Set[str] = set()
        pool: List[PlayerTradeCandidate] = []
        for c in filler + match:
            if not c or not getattr(c, "player_id", None):
                continue
            if c.player_id in seen:
                continue
            seen.add(c.player_id)
            pool.append(c)
        return pool

    # ---------------------------------------------------------------------
    # Case A: buyer fails (buyer incoming salary too high vs allowed_in) => increase buyer outgoing salary.
    # ---------------------------------------------------------------------
    if team_id == buyer_id:
        # If we're at SECOND_APRON, we cannot add a 2nd outgoing player once we already have one.
        second_apron_one_for_one = (status == "SECOND_APRON") or (method == "outgoing_second_apron")

        # aggregation_solo_only means the team cannot aggregate multiple outgoing players;
        # it can still trade a single player (including another solo-only player).
        has_solo_only_outgoing = any(
            buyer_out.players.get(pid) and buyer_out.players[pid].aggregation_solo_only
            for pid in spec.buyer_players_out
        )

        deficit_dollars = max(0.0, incoming_salary - allowed_in) if allowed_in > 0 else max(0.0, incoming_salary)
        needed_extra_m = deficit_dollars / 1_000_000.0

        pool = _pool_for(buyer_out, receiver_team_id=seller_id)
        used = set(spec.buyer_players_out)

        def pick_best_to_add() -> Optional[PlayerTradeCandidate]:
            best_c: Optional[PlayerTradeCandidate] = None
            best_score: Optional[float] = None
            for c in pool:
                if c.player_id in used:
                    continue
                if c.aggregation_solo_only and len(spec.buyer_players_out) >= 1:
                    continue
                sal = _salary_m(c)
                if sal <= 0:
                    continue
                val = _value(c)
                # Prefer covering most of the deficit with minimal value cost.
                score = abs(sal - needed_extra_m) + 0.03 * val
                if needed_extra_m > 0.75 and sal < 0.60 * needed_extra_m:
                    score += (0.60 * needed_extra_m - sal) * 2.5
                if best_score is None or score < best_score:
                    best_score = score
                    best_c = c
            return best_c

        def pick_best_swap_higher(current_pid: str) -> Optional[PlayerTradeCandidate]:
            cur = buyer_out.players.get(current_pid)
            cur_sal = _salary_m(cur) if cur is not None else 0.0
            min_sal = max(cur_sal + max(0.25, needed_extra_m), cur_sal + 0.25)

            best_c: Optional[PlayerTradeCandidate] = None
            best_score: Optional[float] = None
            for c in pool:
                if c.player_id == current_pid:
                    continue
                if c.player_id in used:
                    continue
                sal = _salary_m(c)
                if sal + 1e-6 < min_sal:
                    continue
                val = _value(c)
                over = sal - min_sal
                score = over + 0.03 * val
                if best_score is None or score < best_score:
                    best_score = score
                    best_c = c
            return best_c

        # Prefer add when allowed; otherwise swap to keep 1 outgoing player.
        can_add_outgoing = (
            not (second_apron_one_for_one and len(spec.buyer_players_out) >= 1)
            and not (has_solo_only_outgoing and len(spec.buyer_players_out) >= 1)
        )
        if can_add_outgoing:
            cand = pick_best_to_add()
            if cand is not None:
                spec.buyer_players_out.append(cand.player_id)
                spec.tags.append("repair:add_salary_filler_buyer")
                return True

        # Fallback: swap a single outgoing to a higher-salary alternative.
        if len(spec.buyer_players_out) == 1:
            cur_pid = spec.buyer_players_out[0]
            cand = pick_best_swap_higher(cur_pid)
            if cand is not None:
                spec.buyer_players_out[0] = cand.player_id
                spec.tags.append("repair:swap_higher_salary_buyer")
                return True

        # If we had 0 outgoing players (picks-only), adding one is still allowed even under SECOND_APRON.
        if len(spec.buyer_players_out) == 0:
            cand = pick_best_to_add()
            if cand is not None:
                spec.buyer_players_out.append(cand.player_id)
                spec.tags.append("repair:add_outgoing_required_buyer")
                return True

        return False

    # ---------------------------------------------------------------------
    # Case B: seller fails (seller incoming salary too high vs allowed_in) => reduce buyer outgoing salary.
    # ---------------------------------------------------------------------
    if team_id == seller_id:
        # Trim extra incoming players first (common for 2-for-1).
        if len(spec.buyer_players_out) >= 2:
            spec.buyer_players_out = spec.buyer_players_out[:1]
            spec.tags.append("repair:trim_incoming_seller")
            return True

        if len(spec.buyer_players_out) != 1:
            return False

        # Replace buyer outgoing player with a cheaper one that fits seller's allowed_in.
        allowed_max_m = allowed_in / 1_000_000.0
        if allowed_max_m <= 0.0:
            return False

        cur_pid = spec.buyer_players_out[0]
        pool = _pool_for(buyer_out, receiver_team_id=seller_id)

        candidates = []
        for c in pool:
            if c.player_id == cur_pid:
                continue
            sal = _salary_m(c)
            if sal <= 0:
                continue
            if sal - 1e-6 > allowed_max_m:
                continue
            candidates.append(c)

        if not candidates:
            return False

        candidates.sort(key=lambda c: (-_value(c), -_salary_m(c), c.player_id))
        spec.buyer_players_out[0] = candidates[0].player_id
        spec.tags.append("repair:swap_cheaper_buyer_for_seller")
        return True

    return False


def _repair_pick_rules(state: _GenState, spec: _DealSpec, details: Dict[str, Any]) -> bool:
    """Downgrade/remove picks to satisfy Stepien or pick horizon.

    IMPORTANT POLICY:
    - stepien_violation is typically a *combination* issue; do NOT hard-ban a pick_id.
      We only drop the last-added asset (soft drop) and let exploration try other combos.
    - pick_too_far (horizon) is effectively intrinsic for that pick, so hard-banning is OK.
    """
    reason = str(details.get("reason") or "")
    hard_ban = reason in ("pick_too_far",)

    # Simple strategy: remove the last-added pick/swap from the side indicated.
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        # If missing, assume buyer side (most common)
        team_id = spec.buyer_id

    def _drop_pick(team: str, *, seller_side: bool) -> bool:
        if team == spec.buyer_id:
            if spec.buyer_picks_out:
                removed = str(spec.buyer_picks_out.pop())
                if hard_ban:
                    state.banned_picks[spec.buyer_id].add(removed)
                else:
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_pick" + ("_seller" if seller_side else ""))
                return True
            if spec.buyer_swaps_out:
                removed = str(spec.buyer_swaps_out.pop())
                if hard_ban:
                    state.banned_swaps[spec.buyer_id].add(removed)
                else:
                    # swaps rarely cause stepien_violation; still keep policy consistent
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_swap" + ("_seller" if seller_side else ""))
                return True
        if team == spec.seller_id:
            if spec.seller_picks_out:
                removed = str(spec.seller_picks_out.pop())
                if hard_ban:
                    state.banned_picks[spec.seller_id].add(removed)
                else:
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_pick_seller")
                return True
            if spec.seller_swaps_out:
                removed = str(spec.seller_swaps_out.pop())
                if hard_ban:
                    state.banned_swaps[spec.seller_id].add(removed)
                else:
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_swap_seller")
                return True
        return False

    if team_id == spec.buyer_id:
        return _drop_pick(spec.buyer_id, seller_side=False)
    if team_id == spec.seller_id:
        return _drop_pick(spec.seller_id, seller_side=True)
    # Fallback
    return _drop_pick(spec.buyer_id, seller_side=False)
