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
from .dealgen_utils import _canon_team_id, _is_ban_active, _is_locked
from .dealgen_targeting import _best_need_tag
from .dealgen_sweeteners import _stepien_ok_after

def _build_offer_skeletons(state: _GenState, *, buyer_id: str, target_ref: IncomingPlayerRef) -> List[_DealSpec]:
    cfg = state.cfg
    catalog = state.catalog
    tick_ctx = state.tick_ctx
    rng = state.rng

    seller_id = _canon_team_id(target_ref.from_team)
    if not seller_id:
        return []

    buyer_out = catalog.outgoing_by_team.get(buyer_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if buyer_out is None or seller_out is None:
        return []

    # resolve full target candidate (the player seller would send out)
    target = seller_out.players.get(target_ref.player_id)
    if target is None:
        return []

    # --- counterpart posture/horizon (used to shape archetypes) ---
    seller_ts = tick_ctx.get_team_situation(seller_id)
    seller_posture = str(getattr(seller_ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    seller_horizon = str(getattr(seller_ts, "time_horizon", "RE_TOOL") or "RE_TOOL").upper()
    rebuildish = (seller_horizon == "REBUILD") or (seller_posture in ("SELL", "SOFT_SELL"))
    win_nowish = (seller_horizon == "WIN_NOW")

    # Light prefilter: avoid CORE unless seller is SELL-ish. Even then, make it rare.
    if "CORE" in (target.buckets or ()):
        if seller_posture not in ("SELL", "SOFT_SELL"):
            return []
        if rng.random() > 0.10:
            return []

    # lock / return-ban / eligibility prefilter using catalog snapshot
    if _is_locked(target.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
        state.stats.pruned_locked += 1
        return []
    if target_ref.from_team and buyer_id in set(target.return_ban_teams or ()):
        state.stats.pruned_ineligible += 1
        return []

    if buyer_id in state.banned_receivers_by_player.get(target.player_id, set()):
        state.stats.pruned_ineligible += 1
        return []
    if _is_ban_active(tick_ctx.current_date, target.recent_signing_banned_until):
        state.stats.pruned_ineligible += 1
        return []

    # --- Hard realism guard: if either side is above 2nd apron, avoid multi-player constructions up front.
    buyer_ts = tick_ctx.get_team_situation(buyer_id)
    buyer_apron = str(getattr(getattr(buyer_ts, "constraints", None), "apron_status", "") or "")
    seller_apron = str(getattr(getattr(seller_ts, "constraints", None), "apron_status", "") or "")
    apron_one_for_one_hint = (buyer_apron == "ABOVE_2ND_APRON") or (seller_apron == "ABOVE_2ND_APRON")

    # Seller need map guides "return player" selection (NBA feel: they ask for fits, not random bodies)
    seller_need_map = _get_need_map(tick_ctx, seller_id)

    # Build candidate sets for buyer outgoing
    buyer_players = _collect_buyer_player_candidates(state, buyer_out, receiver_team_id=seller_id)
    filler = buyer_players["filler"]
    match = buyer_players["match"]
    young_hi = buyer_players["young_prospect"]
    young_lo = buyer_players["young_throwin"]
    cons = buyer_players["consolidate"]

    # Archetype shaping by seller horizon (still bounded by shuffle + final cap)
    p4p_k = 3 if win_nowish else 2
    salary_k = 3 if win_nowish else 2
    picks_pkg_n = 4 if rebuildish else 2
    young_k = 2 if rebuildish else 1
    young_pkg_n = 3 if rebuildish else 1

    enable_2for1 = bool(cfg.enable_consolidate_2for1) and win_nowish

    skeletons: List[_DealSpec] = []

    # --- archetype: player-for-player (return player chosen to match seller needs when possible)
    p4p_pool = _sample_for_counterparty(match, target.salary_m, need_map=seller_need_map, rng=rng, k=p4p_k)
    for p in p4p_pool:
        sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
        sk.seller_players_out = [target.player_id]
        sk.buyer_players_out = [p.player_id]
        sk.tags.append("archetype:p4p")
        sk.tags.append(f"need:{target_ref.tag}")
        if seller_horizon:
            sk.tags.append(f"seller_horizon:{seller_horizon}")
        rt = _best_need_tag(seller_need_map, p)
        if rt:
            sk.tags.append(f"return_need:{rt}")
        skeletons.append(sk)

    # --- archetype: picks-only (rebuildish sellers prefer; win-now sellers rare)
    if cfg.enable_picks_only and _buyer_can_absorb_target(tick_ctx, buyer_id, target.salary_m):
        max_pkg = picks_pkg_n if rebuildish else 1
        for picks, swaps, tag in _picks_packages(state, buyer_out, max_packages=max_pkg):
            sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
            sk.seller_players_out = [target.player_id]
            sk.buyer_picks_out = list(picks)
            sk.buyer_swaps_out = list(swaps)
            sk.tags.append("archetype:picks_only")
            sk.tags.append(tag)
            sk.tags.append(f"need:{target_ref.tag}")
            if seller_horizon:
                sk.tags.append(f"seller_horizon:{seller_horizon}")
            skeletons.append(sk)

    # --- archetype: young + pick (rebuildish / re-tool sellers lean this way)
    # Rebuild sellers should be offered *real prospects*, not just cheap young bodies.
    if young_hi or young_lo:
        max_young_players = young_k if rebuildish else 1
        max_young_pkgs = young_pkg_n if rebuildish else 1

        if rebuildish:
            pool = young_hi if young_hi else young_lo
            # Select from top market prospects for realism; shuffle top bucket for variety.
            pool_sorted = sorted(
                pool,
                key=lambda c: float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0),
                reverse=True,
            )
            top_n = max(2, min(int(cfg.young_prospect_max_candidates), len(pool_sorted)))
            top_bucket = list(pool_sorted[:top_n])
            rng.shuffle(top_bucket)
            chosen = top_bucket[: max(0, max_young_players)]
            source_tag = "young_source:prospect" if young_hi else "young_source:throwin"
        else:
            # Non-rebuild sellers: treat young as a cheap throw-in.
            pool = young_lo
            chosen = _sample_for_counterparty(
                pool[: max(1, 2 * max(1, max_young_players))],
                target.salary_m,
                need_map=seller_need_map,
                rng=rng,
                k=max_young_players,
            )
            source_tag = "young_source:throwin"

        for p in chosen:
            for picks, swaps, tag in _picks_packages(state, buyer_out, max_packages=max_young_pkgs, prefer_second=True):
                sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
                sk.seller_players_out = [target.player_id]
                sk.buyer_players_out = [p.player_id]
                sk.buyer_picks_out = list(picks)
                sk.buyer_swaps_out = list(swaps)
                sk.tags.append("archetype:young+pick")
                sk.tags.append(source_tag)
                sk.tags.append(tag)
                sk.tags.append(f"need:{target_ref.tag}")
                if seller_horizon:
                    sk.tags.append(f"seller_horizon:{seller_horizon}")
                rt = _best_need_tag(seller_need_map, p)
                if rt:
                    sk.tags.append(f"return_need:{rt}")
                skeletons.append(sk)

    # --- archetype: salary match focus (win-now sellers tend to want immediate contributors)
    salary_pool = match if win_nowish else filler
    for p in _sample_for_counterparty(salary_pool, target.salary_m, need_map=seller_need_map, rng=rng, k=salary_k):
        sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
        sk.seller_players_out = [target.player_id]
        sk.buyer_players_out = [p.player_id]
        sk.tags.append("archetype:salary_match")
        sk.tags.append(f"need:{target_ref.tag}")
        if seller_horizon:
            sk.tags.append(f"seller_horizon:{seller_horizon}")
        rt = _best_need_tag(seller_need_map, p)
        if rt:
            sk.tags.append(f"return_need:{rt}")
        skeletons.append(sk)

    # --- archetype: consolidate (2-for-1) (mostly a win-now depth play; search width reduced for 2nd apron hint)
    if enable_2for1 and cons and filler:
        a_take = 1 if apron_one_for_one_hint else 2
        b_take = 2 if apron_one_for_one_hint else 4
        top_a = _rank_for_need(cons[:6], need_map=seller_need_map)[:a_take]
        top_b = _rank_for_need(filler[:10], need_map=seller_need_map)[:b_take]
        for a in top_a:
            for b in top_b:
                if a.player_id == b.player_id:
                    continue
                if a.aggregation_solo_only or b.aggregation_solo_only:
                    continue
                sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
                sk.seller_players_out = [target.player_id]
                sk.buyer_players_out = [a.player_id, b.player_id]
                sk.tags.append("archetype:2for1")
                sk.tags.append(f"need:{target_ref.tag}")
                if seller_horizon:
                    sk.tags.append(f"seller_horizon:{seller_horizon}")
                rta = _best_need_tag(seller_need_map, a)
                rtb = _best_need_tag(seller_need_map, b)
                if rta:
                    sk.tags.append(f"return_need:{rta}")
                if rtb and rtb != rta:
                    sk.tags.append(f"return_need:{rtb}")
                skeletons.append(sk)

    # shuffle + keep only a small bounded set per target
    rng.shuffle(skeletons)
    return skeletons[: max(6, min(16, 2 * state.cfg.beam_width))]


def _buyer_can_absorb_target(tick_ctx: TradeGenerationTickContext, buyer_id: str, target_salary_m: float) -> bool:
    ts = tick_ctx.get_team_situation(buyer_id)
    cap_space = 0.0
    try:
        cap_space = float(getattr(getattr(ts, "constraints", None), "cap_space", 0.0) or 0.0)
    except Exception:
        cap_space = 0.0
    # cap_space is in dollars, target_salary_m is in millions
    return cap_space >= float(target_salary_m) * 1_000_000.0 * 1.02


def _collect_buyer_player_candidates(state: _GenState, buyer_out: TeamOutgoingCatalog, *, receiver_team_id: Optional[str] = None) -> Dict[str, List[PlayerTradeCandidate]]:
    """Bucket buyer outgoing players into candidate sets for archetypes."""
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    buyer_id = buyer_out.team_id
    buyer_ts = tick_ctx.get_team_situation(buyer_id)
    posture = str(getattr(buyer_ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()


    receiver_id = _canon_team_id(receiver_team_id or '') if receiver_team_id else ''

    # gather players excluding banned/locked
    all_players: List[PlayerTradeCandidate] = []
    for pid, cand in buyer_out.players.items():
        if pid in state.banned_players[buyer_id]:
            continue
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            continue
        if _is_ban_active(tick_ctx.current_date, cand.recent_signing_banned_until):
            continue
        if receiver_id:
            # Block sending this player to receiver_id if SSOT return-bans or learned bans apply.
            if receiver_id in set(cand.return_ban_teams or ()):  # type: ignore[arg-type]
                continue
            if receiver_id in state.banned_receivers_by_player.get(pid, set()):
                continue
        all_players.append(cand)

    # classify by buckets
    filler_buckets = ("FILLER_CHEAP", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "VETERAN_SALE", "FILLER_BAD_CONTRACT")
    match_buckets = ("EXPIRING", "SURPLUS_REDUNDANT", "CONSOLIDATE", "SURPLUS_LOW_FIT", "FILLER_CHEAP")
    consolidate_buckets = ("CONSOLIDATE", "SURPLUS_REDUNDANT", "SURPLUS_LOW_FIT")

    def is_core(c: PlayerTradeCandidate) -> bool:
        return "CORE" in (c.buckets or ())

    # filler: low-importance, non-core
    filler = [c for c in all_players if (not is_core(c)) and any(b in (c.buckets or ()) for b in filler_buckets)]
    # match: closer to salary, still non-core
    match = [c for c in all_players if (not is_core(c)) and any(b in (c.buckets or ()) for b in match_buckets)]
    # consolidate: higher quality non-core
    consolidate = [c for c in all_players if (not is_core(c)) and any(b in (c.buckets or ()) for b in consolidate_buckets)]

    # young: derived (age + controllable years). Split into prospect vs throw-in.
    young_pool: List[PlayerTradeCandidate] = []
    young_throwin: List[PlayerTradeCandidate] = []
    for c in all_players:
        if is_core(c):
            continue
        # Avoid aggregation-solo-only assets for young packages: they tend to create invalid combos.
        if bool(getattr(c, "aggregation_solo_only", False)):
            continue
        age = getattr(getattr(c, "snap", None), "age", None)
        try:
            age_f = float(age) if age is not None else None
        except Exception:
            age_f = None
        if age_f is None or age_f > float(cfg.young_max_age):
            continue
        try:
            yrs = float(getattr(c, "remaining_years", 0.0) or 0.0)
        except Exception:
            yrs = 0.0
        if yrs < float(cfg.young_min_control_years):
            continue

        young_pool.append(c)
        mv = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        if mv <= float(cfg.young_throwin_max_market):
            young_throwin.append(c)

    young_prospect: List[PlayerTradeCandidate] = []
    if young_pool:
        young_sorted = sorted(
            young_pool,
            key=lambda c: float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0),
            reverse=True,
        )
        frac = float(getattr(cfg, "young_prospect_top_frac", 0.35) or 0.35)
        try:
            n = int(math.ceil(len(young_sorted) * max(0.05, min(1.0, frac))))
        except Exception:
            n = max(1, int(round(len(young_sorted) * 0.35)))
        n = max(1, min(int(cfg.young_prospect_max_candidates), n, len(young_sorted)))
        young_prospect = young_sorted[:n]

    prospect_ids = {c.player_id for c in young_prospect}
    young_throwin = [c for c in young_throwin if c.player_id not in prospect_ids]

    # Sort
    filler.sort(key=lambda c: (float(getattr(c.market, "total", 0.0) or 0.0), float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    # match: keep salary diversity; do NOT bias to an arbitrary salary anchor (e.g. $10M)
    match.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), -float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    consolidate.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), -float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    young_prospect.sort(key=lambda c: (-float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0), float(getattr(c, "salary_m", 0.0) or 0.0), c.player_id))
    young_throwin.sort(key=lambda c: (-float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0), float(getattr(c, "salary_m", 0.0) or 0.0), c.player_id))

    # In BUY posture, be less willing to ship out high-value consolidate pieces
    if posture in ("AGGRESSIVE_BUY", "SOFT_BUY"):
        consolidate = consolidate[:4]

    return {
        "filler": filler[:14],
        "match": match[:28],
        "young_prospect": young_prospect[: max(0, int(cfg.young_prospect_max_candidates))],
        "young_throwin": young_throwin[: max(0, int(cfg.young_throwin_max_candidates))],
        "consolidate": consolidate[:8],
    }


def _sample_near_salary(cands: Sequence[PlayerTradeCandidate], target_salary_m: float, *, rng: random.Random, k: int) -> List[PlayerTradeCandidate]:
    """Sample up to k candidates with salary close to target."""
    rows = []
    for c in cands:
        try:
            s = float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            s = 0.0
        rows.append((abs(s - float(target_salary_m)), -float(getattr(c.market, "total", 0.0) or 0.0), c.player_id, c))
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    top = [r[3] for r in rows[: max(2, min(8, len(rows)))]]
    rng.shuffle(top)
    return top[: max(0, k)]


def _picks_packages(state: _GenState, buyer_out: TeamOutgoingCatalog, *, max_packages: int, prefer_second: bool = False) -> List[Tuple[Tuple[str, ...], Tuple[str, ...], str]]:
    """Build a few pick/swap packages for the buyer.

    prefer_second=True biases toward SECOND-based packages and uses FIRST picks only as fallback.

    NOTE: We apply a lightweight Stepien precheck here to avoid generating obviously
    invalid first-pick combinations and wasting validation budget.
    """
    team_id = buyer_out.team_id

    def _pick_ok(pid: str) -> bool:
        if pid in state.banned_picks[team_id]:
            return False
        cand = buyer_out.picks.get(pid)
        if cand is None:
            return False
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return False
        return True

    def _swap_ok(sid: str) -> bool:
        if sid in state.banned_swaps[team_id]:
            return False
        cand = buyer_out.swaps.get(sid)
        if cand is None:
            return False
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return False
        return True

    picks_second = [pid for pid in buyer_out.pick_ids_by_bucket.get("SECOND", ()) if _pick_ok(pid)]
    picks_first_safe = [pid for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SAFE", ()) if _pick_ok(pid)]
    picks_first_sens = [pid for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()) if _pick_ok(pid)]
    swaps = [sid for sid in buyer_out.swap_ids if _swap_ok(sid)]

    seconds_pkgs: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    swap_pkgs: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    first_pkgs: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []

    # seconds-first packages
    if picks_second:
        seconds_pkgs.append(((picks_second[0],), tuple(), "sweetener:2RP"))
    if len(picks_second) >= 2:
        seconds_pkgs.append(((picks_second[0], picks_second[1]), tuple(), "sweetener:2RPx2"))
    if picks_second and swaps:
        seconds_pkgs.append(((picks_second[0],), (swaps[0],), "sweetener:2RP+swap"))

    # swaps (cheap but valuable)
    if swaps:
        swap_pkgs.append((tuple(), (swaps[0],), "sweetener:swap"))

    # first-round picks as fallback
    if picks_first_safe:
        first_pkgs.append(((picks_first_safe[0],), tuple(), "sweetener:1RP_SAFE"))
    if picks_first_sens:
        first_pkgs.append(((picks_first_sens[0],), tuple(), "sweetener:1RP_SENSITIVE"))

    ordered: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]]
    if prefer_second:
        ordered = seconds_pkgs + swap_pkgs + first_pkgs
    else:
        # stable cheap->expensive order (legacy)
        ordered = []
        ordered.extend(seconds_pkgs[:2])
        ordered.extend(swap_pkgs[:1])
        ordered.extend(first_pkgs)

    # Stepien precheck (best-effort): only checks the outgoing picks in the package itself.
    stepien = getattr(state.catalog, "stepien", None)
    filtered: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    for picks_out, swaps_out, tag in ordered:
        if picks_out and stepien is not None:
            if not _stepien_ok_after(stepien, team_id, outgoing_pick_ids=set(picks_out)):
                state.stats.stepien_precheck_blocked += 1
                continue
        filtered.append((picks_out, swaps_out, tag))
        if len(filtered) >= max(0, int(max_packages)):
            break

    return filtered


# =============================================================================
# Validate + repair
# =============================================================================
def _apply_partner_cap(
    state: _GenState,
    proposals: List[DealProposal],
    *,
    max_results: int,
    partner_side: str,
) -> List[DealProposal]:
    """Diversify final output by capping number of proposals per partner team.

    partner_side:
      - 'seller': cap by proposal.seller_id (BUY mode)
      - 'buyer':  cap by proposal.buyer_id  (SELL mode)

    Also applies a soft penalty (partner_repeat_penalty) during selection so repeated
    partners are less likely to crowd out variety *before* hitting the hard cap.
    """
    max_results_i = max(0, int(max_results))
    if max_results_i <= 0:
        return []

    cap = int(state.cfg.max_partner_repeats or 0)
    if cap <= 0:
        return proposals[:max_results_i]

    penalty = float(state.cfg.partner_repeat_penalty or 0.0)
    counts = defaultdict(int)

    # If no penalty is configured, keep the old deterministic behavior.
    if penalty <= 0.0:
        out: List[DealProposal] = []
        for p in proposals:
            partner = p.seller_id if partner_side == 'seller' else p.buyer_id
            if counts[partner] >= cap:
                continue
            out.append(p)
            counts[partner] += 1
            if len(out) >= max_results_i:
                break
        try:
            state.stats.partner_counts.clear()
            state.stats.partner_counts.update(counts)
        except Exception:
            pass
        return out

    # Greedy selection with diversity penalty (bounded by max_results).
    remaining = list(proposals)
    out: List[DealProposal] = []
    while remaining and len(out) < max_results_i:
        best_i = -1
        best_adj = None
        best_raw = None
        for i, p in enumerate(remaining):
            partner = p.seller_id if partner_side == 'seller' else p.buyer_id
            if counts[partner] >= cap:
                continue
            adj = float(p.score) - penalty * float(max(0, counts[partner]))
            if best_adj is None or adj > best_adj or (adj == best_adj and (best_raw is None or p.score > best_raw)):
                best_adj = adj
                best_raw = float(p.score)
                best_i = i
        if best_i < 0:
            break
        chosen = remaining.pop(best_i)
        partner = chosen.seller_id if partner_side == 'seller' else chosen.buyer_id
        out.append(chosen)
        counts[partner] += 1

    try:
        state.stats.partner_counts.clear()
        state.stats.partner_counts.update(counts)
    except Exception:
        pass

    return out
