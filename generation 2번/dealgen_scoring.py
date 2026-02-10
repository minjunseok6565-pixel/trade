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
from .dealgen_utils import _canon_team_id, _is_locked

def _spec_to_deal(state: _GenState, spec: _DealSpec) -> Optional[Deal]:
    """Convert a spec to a 2-team Deal object, with strict asset validity checks."""
    catalog = state.catalog
    buyer_id = _canon_team_id(spec.buyer_id)
    seller_id = _canon_team_id(spec.seller_id)
    if not buyer_id or not seller_id or buyer_id == seller_id:
        return None

    buyer_out = catalog.outgoing_by_team.get(buyer_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if buyer_out is None or seller_out is None:
        return None

    # Build legs
    legs: Dict[str, List[Any]] = {buyer_id: [], seller_id: []}

    # buyer players
    for pid in spec.buyer_players_out:
        cand = buyer_out.players.get(pid)
        if cand is None:
            return None
        if pid in state.banned_players[buyer_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[buyer_id].append(cand.as_asset(to_team=None))

    # buyer picks
    for pick_id in spec.buyer_picks_out:
        cand = buyer_out.picks.get(pick_id)
        if cand is None:
            return None
        if pick_id in state.banned_picks[buyer_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[buyer_id].append(cand.as_asset(to_team=None))

    # buyer swaps
    for swap_id in spec.buyer_swaps_out:
        cand = buyer_out.swaps.get(swap_id)
        if cand is None:
            return None
        if swap_id in state.banned_swaps[buyer_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[buyer_id].append(cand.as_asset(to_team=None))

    # seller players
    for pid in spec.seller_players_out:
        cand = seller_out.players.get(pid)
        if cand is None:
            return None
        if pid in state.banned_players[seller_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        # eligibility prefilter (recent-signing ban is absolute; aggregation is handled by solo-only constraint)
        if _is_ban_active(state.tick_ctx.current_date, cand.recent_signing_banned_until):
            return None
        legs[seller_id].append(cand.as_asset(to_team=None))

    # seller picks/swaps rarely used in this generator, but supported
    for pick_id in spec.seller_picks_out:
        cand = seller_out.picks.get(pick_id)
        if cand is None:
            return None
        if pick_id in state.banned_picks[seller_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[seller_id].append(cand.as_asset(to_team=None))

    for swap_id in spec.seller_swaps_out:
        cand = seller_out.swaps.get(swap_id)
        if cand is None:
            return None
        if swap_id in state.banned_swaps[seller_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[seller_id].append(cand.as_asset(to_team=None))

    # Always omit meta for dedupe stability (meta is orchestrator territory)
    meta = {"tags": list(dict.fromkeys(spec.tags))} if spec.tags else {}
    deal = Deal(teams=[buyer_id, seller_id], legs=legs, meta=meta)
    return deal


# =============================================================================
# Evaluate + scoring
# =============================================================================
def _evaluate_and_score(state: _GenState, deal: Deal, *, buyer_id: str, seller_id: str, partner_id: Optional[str] = None) -> Optional[DealProposal]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx

    try:
        buyer_decision, buyer_eval = evaluate_deal_for_team(
            deal,
            buyer_id,
            tick_ctx=tick_ctx,
            include_breakdown=False,
            validate=False,
        )
        seller_decision, seller_eval = evaluate_deal_for_team(
            deal,
            seller_id,
            tick_ctx=tick_ctx,
            include_breakdown=False,
            validate=False,
        )
        state.stats.evaluations += 2
    except TradeError:
        # valuation should rarely raise TradeError when validate=False, but be defensive
        return None
    except Exception:
        return None

    score = _score_deal(cfg, buyer_decision, buyer_eval, seller_decision, seller_eval)
    tags = tuple(_extract_tags_from_deal(deal))

    return DealProposal(
        deal=deal,
        buyer_id=buyer_id,
        seller_id=seller_id,
        buyer_decision=buyer_decision,
        seller_decision=seller_decision,
        buyer_eval=buyer_eval,
        seller_eval=seller_eval,
        score=float(score),
        tags=tags,
    )


def _score_deal(cfg: DealGeneratorConfig, bd: DealDecision, be: TeamDealEvaluation, sd: DealDecision, se: TeamDealEvaluation) -> float:
    mb = float(getattr(be, "net_surplus", 0.0) or 0.0) - float(getattr(bd, "required_surplus", 0.0) or 0.0)
    ms = float(getattr(se, "net_surplus", 0.0) or 0.0) - float(getattr(sd, "required_surplus", 0.0) or 0.0)

    # accept score uses sigmoid on margins
    scale = float(cfg.sigmoid_scale or 3.5)
    accept = _sigmoid(mb / scale) + _sigmoid(ms / scale)

    # complexity
    num_assets = int(_count_assets(be, se))
    num_players = int(_count_players(be, se))
    complexity = cfg.complexity_penalty_assets * max(0, num_assets - 2) + cfg.complexity_penalty_players * max(0, num_players - 2)

    # buyer overpay penalty (discourage buyer losing badly)
    overpay = max(0.0, -mb) * float(cfg.buyer_overpay_penalty)

    return float(accept - complexity - overpay)


def _count_assets(be: TeamDealEvaluation, se: TeamDealEvaluation) -> int:
    # total moved assets is incoming+outgoing across both sides, but those lists are symmetric.
    try:
        return int(len(getattr(be.side, "incoming", ())) + len(getattr(be.side, "outgoing", ())))
    except Exception:
        return 0


def _count_players(be: TeamDealEvaluation, se: TeamDealEvaluation) -> int:
    def _is_player(tv) -> bool:
        k = getattr(tv, "kind", None)
        if k is None:
            return False
        # AssetKind Enum이면 .value가 "player" 형태. (str(Enum)은 "AssetKind.PLAYER"가 될 수 있음)
        v = getattr(k, "value", k)
        return str(v).strip().lower() == "player"

    def _n_players(side):
        n = 0
        for tv in getattr(side, "incoming", ()):
            if _is_player(tv):
                n += 1
        for tv in getattr(side, "outgoing", ()):
            if _is_player(tv):
                n += 1
        return n
    try:
        return int(_n_players(be.side))
    except Exception:
        return 0


def _sigmoid(x: float) -> float:
    # safe sigmoid
    try:
        if x >= 0:
            z = math.exp(-x)
            return 1.0 / (1.0 + z)
        z = math.exp(x)
        return z / (1.0 + z)
    except Exception:
        return 0.5


def _extract_tags_from_deal(deal: Deal) -> List[str]:
    tags: List[str] = []
    meta = getattr(deal, "meta", None)
    if isinstance(meta, dict):
        t = meta.get("tags")
        if isinstance(t, list):
            tags.extend([str(x) for x in t if x])
    return tags
