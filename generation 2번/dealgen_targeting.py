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
from .dealgen_utils import _canon_team_id, _is_ban_active, _is_locked, _clamp01

def _select_sale_assets(state: _GenState, *, seller_id: str, budgets: _Budgets) -> List[PlayerTradeCandidate]:
    """Pick outgoing assets to shop in SELL mode (surplus/expiring/vet-sale first)."""
    tick_ctx = state.tick_ctx
    catalog = state.catalog
    rng = state.rng

    seller_id = _canon_team_id(seller_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if seller_out is None:
        return []

    ts = tick_ctx.get_team_situation(seller_id)
    posture = str(getattr(ts, "trade_posture", "SELL") or "SELL").upper()

    # Priorities roughly emulate NBA: expiring/vet-sale/surplus are most likely to be shopped.
    bucket_pri = {
        "VETERAN_SALE": 0,
        "EXPIRING": 1,
        "SURPLUS_LOW_FIT": 2,
        "SURPLUS_REDUNDANT": 3,
        "FILLER_CHEAP": 4,
        "FILLER_BAD_CONTRACT": 5,
        "CONSOLIDATE": 6,
        "CORE": 99,
    }

    rows: List[Tuple[int, float, float, float, str, PlayerTradeCandidate]] = []
    for pid, c in seller_out.players.items():
        if pid in state.banned_players[seller_id]:
            continue
        if _is_locked(c.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            continue
        if _is_ban_active(tick_ctx.current_date, c.recent_signing_banned_until):
            continue

        # Exclude CORE in SOFT_SELL; allow ultra-rare CORE in SELL
        if "CORE" in (c.buckets or ()):
            if posture != "SELL":
                continue
            if rng.random() > 0.04:
                continue

        pri = min(bucket_pri.get(b, 50) for b in (c.buckets or ("FILLER_CHEAP",)))
        surplus = float(getattr(c, "surplus_score", 0.0) or 0.0)
        exp = 1.0 if bool(getattr(c, "is_expiring", False)) else 0.0
        value = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        rows.append((pri, -surplus, -exp, value, c.player_id, c))

    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
    assets = [r[-1] for r in rows]

    # Small shuffle among close ranks to avoid "same shopping list" every tick.
    head = assets[: max(6, min(len(assets), budgets.max_targets))]
    rng.shuffle(head)
    tail = assets[len(head):]
    out = head + tail

    return out[: max(0, budgets.max_targets)]


def _select_buyers_for_sale_asset(
    state: _GenState,
    *,
    seller_id: str,
    sale_player: PlayerTradeCandidate,
    budgets: _Budgets,
) -> List[Tuple[str, str, float]]:
    """For a given sale player, pick plausible buyers based on their need_map."""
    tick_ctx = state.tick_ctx
    catalog = state.catalog

    seller_id = _canon_team_id(seller_id)
    max_buyers = max(4, min(14, 2 * budgets.beam_width))

    rows: List[Tuple[float, str, str]] = []
    tags = list(getattr(sale_player, "top_tags", ()) or ())
    supply = getattr(sale_player, "supply", {}) or {}

    for tid in catalog.outgoing_by_team.keys():
        buyer_id = _canon_team_id(tid)
        if not buyer_id or buyer_id == seller_id:
            continue

        buyer_ts = tick_ctx.get_team_situation(buyer_id)
        if bool(getattr(getattr(buyer_ts, "constraints", None), "cooldown_active", False)):
            continue

        need_map = _get_need_map(tick_ctx, buyer_id)
        if not need_map:
            continue

        best_tag = ""
        best = 0.0
        for t in tags:
            w = float(need_map.get(t, 0.0) or 0.0)
            s = float(supply.get(t, 0.0) or 0.0)
            sc = w * (0.4 + 0.6 * s)
            if sc > best:
                best = sc
                best_tag = t

        if best <= 0.05:
            continue

        # Lightly prefer teams with higher urgency to mimic deadline activity.
        urg = float(getattr(buyer_ts, "urgency", 0.0) or 0.0)
        rows.append((best * (0.85 + 0.30 * _clamp01(urg)), buyer_id, best_tag))

    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(bid, tag, float(sc)) for sc, bid, tag in rows[:max_buyers]]
# =============================================================================
# Budgeting + seeds
# =============================================================================
def _get_need_map(tick_ctx: TradeGenerationTickContext, team_id: str) -> Dict[str, float]:
    """Best-effort need_map for a team.

    Primary source: tick_ctx.get_decision_context(team_id).need_map (SSOT for valuation).
    Fallback: tick_ctx.get_team_situation(team_id).needs -> {tag: weight}
    """
    tid = _canon_team_id(team_id)
    out: Dict[str, float] = {}
    try:
        dc = tick_ctx.get_decision_context(tid)
        nm = getattr(dc, "need_map", {}) or {}
        if isinstance(nm, dict):
            for k, v in nm.items():
                if not k:
                    continue
                try:
                    out[str(k)] = float(v)
                except Exception:
                    continue
    except Exception:
        pass

    if out:
        return out

    # Fallback
    try:
        ts = tick_ctx.get_team_situation(tid)
        needs = getattr(ts, "needs", None)
        if isinstance(needs, list):
            for n in needs:
                tag = getattr(n, "tag", None)
                w = getattr(n, "weight", None)
                if not tag:
                    continue
                try:
                    out[str(tag)] = float(w)
                except Exception:
                    continue
    except Exception:
        pass

    return out

def _select_targets(state: _GenState, *, buyer_id: str, budgets: _Budgets) -> List[IncomingPlayerRef]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    catalog = state.catalog
    buyer_dc = tick_ctx.get_decision_context(buyer_id)
    need_map = _get_need_map(tick_ctx, buyer_id)

    # pick top N tags
    need_items = [(str(k), float(v)) for k, v in need_map.items() if k and v is not None]
    need_items.sort(key=lambda x: x[1], reverse=True)
    need_items = need_items[: max(0, int(cfg.max_need_tags))]

    if not need_items:
        return []

    # candidate pool union
    refs: List[IncomingPlayerRef] = []
    seen_players: Set[str] = set()
    for tag, w in need_items:
        pool = list(catalog.incoming_by_need_tag.get(tag, ()))
        cheap_pool = list(catalog.incoming_cheap_by_need_tag.get(tag, ()))
        # Prefer cheap pool a bit (role players / expiring)
        pool = pool + cheap_pool
        for r in pool:
            if not r.player_id or r.player_id in seen_players:
                continue
            if _canon_team_id(r.from_team) == buyer_id:
                continue
            # seller cooldown filter (avoid spam)
            seller_ts = tick_ctx.get_team_situation(_canon_team_id(r.from_team))
            if bool(getattr(getattr(seller_ts, "constraints", None), "cooldown_active", False)):
                continue

            seen_players.add(r.player_id)
            refs.append(r)

    # rank (lightweight heuristic only; final accept determined later)
    def _rank_key(r: IncomingPlayerRef) -> Tuple[float, float, float, str]:
        # need strength
        w = float(need_map.get(r.tag, 0.0) or 0.0)
        tag_strength = float(getattr(r, "tag_strength", 0.0) or 0.0)
        base = tag_strength * (0.4 + 0.6 * _clamp01(w)) * (0.6 + 0.6 * _clamp01(getattr(buyer_dc, "urgency", 0.5)))
        # cheap bonus
        cheap_bonus = cfg.cheap_incoming_bonus if r in catalog.incoming_cheap_by_need_tag.get(r.tag, ()) else 0.0
        # light salary penalty: high salaries complicate matching
        sal_pen = cfg.target_salary_penalty_scale * (float(getattr(r, "salary_m", 0.0) or 0.0) / 10.0)
        score = base + cheap_bonus - sal_pen
        # tie-break: market_total, then lower salary, then player_id
        return (score, float(getattr(r, "market_total", 0.0) or 0.0), -float(getattr(r, "salary_m", 0.0) or 0.0), str(r.player_id))

    refs.sort(key=_rank_key, reverse=True)
    return refs[: max(0, budgets.max_targets)]


# =============================================================================
# Skeleton generation (archetypes)
# =============================================================================
def _need_fit_score(need_map: Mapping[str, float], cand: PlayerTradeCandidate) -> float:
    """How well a candidate matches a team's needs (0..~)."""
    if not need_map:
        return 0.0
    supply = getattr(cand, "supply", {}) or {}
    tags = getattr(cand, "top_tags", ()) or ()
    score = 0.0
    for t in tags:
        try:
            w = float(need_map.get(t, 0.0) or 0.0)
            s = float(supply.get(t, 0.0) or 0.0)
        except Exception:
            continue
        score += w * (0.4 + 0.6 * s)
    return float(score)


def _best_need_tag(need_map: Mapping[str, float], cand: PlayerTradeCandidate) -> str:
    """Return the best-matching need tag for narrative tags (or empty)."""
    if not need_map:
        return ""
    supply = getattr(cand, "supply", {}) or {}
    tags = getattr(cand, "top_tags", ()) or ()
    best_t = ""
    best = 0.0
    for t in tags:
        try:
            w = float(need_map.get(t, 0.0) or 0.0)
            s = float(supply.get(t, 0.0) or 0.0)
            sc = w * (0.4 + 0.6 * s)
        except Exception:
            continue
        if sc > best:
            best = sc
            best_t = str(t)
    return best_t if best > 0.05 else ""


def _rank_for_need(cands: Sequence[PlayerTradeCandidate], *, need_map: Mapping[str, float]) -> List[PlayerTradeCandidate]:
    """Deterministic ranking of candidates by need fit (then by market value, then salary)."""
    rows = []
    for c in cands:
        nf = _need_fit_score(need_map, c)
        mv = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        sal = float(getattr(c, "salary_m", 0.0) or 0.0)
        rows.append((nf, mv, sal, c.player_id, c))
    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    return [r[-1] for r in rows]


def _sample_for_counterparty(
    cands: Sequence[PlayerTradeCandidate],
    target_salary_m: float,
    *,
    need_map: Mapping[str, float],
    rng: random.Random,
    k: int,
) -> List[PlayerTradeCandidate]:
    """Sample candidates with a blend of salary proximity and need fit.

    This is purely a heuristic for *plausible* packages; SSOT evaluation decides acceptance later.
    """
    rows = []
    for c in cands:
        try:
            sal = float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            sal = 0.0
        mv = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        nf = _need_fit_score(need_map, c)
        dist = abs(sal - float(target_salary_m))
        # Higher is better: need fit dominates slightly; salary distance keeps things plausible.
        score = (1.45 * nf) - (0.14 * dist) - (0.015 * max(0.0, mv - 18.0))
        rows.append((score, -nf, dist, mv, c.player_id, c))

    rows.sort(key=lambda x: (x[0], x[1], -x[2], x[4]), reverse=True)
    top = [r[-1] for r in rows[: max(2, min(10, len(rows)))]]
    rng.shuffle(top)
    return top[: max(0, k)]
