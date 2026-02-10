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

from .dealgen_types import DealGeneratorConfig

def _stable_seed(salt: str, *parts: str) -> int:
    h = hashlib.blake2b(digest_size=8)
    h.update(str(salt).encode("utf-8"))
    for p in parts:
        h.update(b"|")
        h.update(str(p).encode("utf-8"))
    return int.from_bytes(h.digest(), "big", signed=False)


def _canon_team_id(team_id: Any) -> str:
    raw = str(team_id or "").strip()
    if not raw:
        return ""
    try:
        return str(normalize_team_id(raw, strict=False)).strip().upper()
    except Exception:
        return raw.upper()


def _clamp01(x: float) -> float:
    try:
        if x < 0.0:
            return 0.0
        if x > 1.0:
            return 1.0
        return float(x)
    except Exception:
        return 0.5


def _parse_iso_ymd(value: object) -> Optional[date]:
    """Parse YYYY-MM-DD (or datetime ISO) into a date. Returns None on failure."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _deadline_passed(tick_ctx: TradeGenerationTickContext) -> bool:
    """True if trade deadline has passed for this tick.

    Mirrors DeadlineRule(validate) but short-circuits generation to avoid burning budgets.
    """
    try:
        rtc = getattr(tick_ctx, "rule_tick_ctx", None)
        base = getattr(rtc, "ctx_state_base", None)
        if not isinstance(base, dict):
            return False
        trade_deadline = base.get("league", {}).get("trade_rules", {}).get("trade_deadline")
        if not trade_deadline:
            return False
        # Allow YYYY-MM-DD or datetime-like strings by slicing safely.
        d = _parse_iso_ymd(trade_deadline)
        if d is None:
            return False
        return bool(tick_ctx.current_date > d)
    except Exception:
        return False


def _is_ban_active(current_date: date, until_iso: Optional[str]) -> bool:
    """True if a banned-until ISO string is active at current_date."""
    d = _parse_iso_ymd(until_iso)
    return bool(d is not None and current_date < d)
def _deal_complexity_exceeds(cfg: DealGeneratorConfig, deal: Deal) -> bool:
    n_assets = sum(len(v) for v in (deal.legs or {}).values())
    n_players = 0
    for assets in (deal.legs or {}).values():
        for a in assets:
            if isinstance(a, PlayerAsset):
                n_players += 1
    return (n_assets > cfg.max_assets) or (n_players > cfg.max_players_moved)


def _is_locked(lock: Any, *, allow_locked_by_deal_id: Optional[str]) -> bool:
    if not lock:
        return False
    try:
        if not bool(getattr(lock, "is_locked", False)):
            return False
    except Exception:
        return False
    deal_id = getattr(lock, "deal_id", None)
    if allow_locked_by_deal_id and deal_id and str(deal_id) == str(allow_locked_by_deal_id):
        return False
    return True


def _deal_fingerprint_2team(deal: Deal) -> str:
    """
    Canonical fingerprint that ignores meta and treats 2-team deals with to_team=None as standard.
    """
    try:
        d = Deal(teams=list(deal.teams), legs=dict(deal.legs), meta={})
        cd = canonicalize_deal(d)
        payload = serialize_deal(cd)
        payload.pop("meta", None)
        s = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.blake2b(s.encode("utf-8"), digest_size=16).hexdigest()
        return h
    except Exception:
        return str(id(deal))
