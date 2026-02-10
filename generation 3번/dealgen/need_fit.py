from __future__ import annotations

"""dealgen.need_fit

Split-out need/fit helpers for the deal generator.
"""

from dataclasses import dataclass, field
from datetime import date
from math import exp
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import hashlib
import json
import random
import re

from ...errors import (
    DEAL_INVALIDATED,
    TRADE_DEADLINE_PASSED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    DUPLICATE_ASSET,
    TradeError,
)
from ...models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import (
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    TradeAssetCatalog,
    build_trade_asset_catalog,
)

def _is_rebuildish(ts: Any) -> bool:
    th = _team_time_horizon(ts)
    tier = str(getattr(ts, "competitive_tier", "") or "").upper()
    return th in {"REBUILD"} or tier in {"REBUILD", "RESET", "TANK"}


def _is_young_candidate(c: PlayerTradeCandidate, cfg: DealGeneratorConfig) -> bool:
    """Heuristic for selecting 'young' assets for the young+pick archetype.

    We rely only on fields that exist in PlayerTradeCandidate / PlayerSnapshot:
    - c.snap.age (optional)
    - c.remaining_years
    - c.is_expiring
    """
    try:
        remaining = float(getattr(c, 'remaining_years', 0.0) or 0.0)
    except Exception:
        remaining = 0.0

    # Prefer non-expiring control for 'young' pieces.
    if bool(getattr(cfg, 'young_avoid_expiring', True)) and bool(getattr(c, 'is_expiring', False)):
        return False

    # Minimum remaining years gate.
    if remaining < float(getattr(cfg, 'young_min_remaining_years', 0.0) or 0.0):
        return False

    age = None
    try:
        age = getattr(getattr(c, 'snap', None), 'age', None)
    except Exception:
        age = None

    if age is None:
        if not bool(getattr(cfg, 'young_allow_unknown_age', True)):
            return False
        # Fallback: require more remaining control if age is unknown.
        if remaining < float(getattr(cfg, 'young_unknown_age_min_remaining_years', 0.0) or 0.0):
            return False
        return True

    try:
        age_f = float(age)
    except Exception:
        # Treat unparseable age as unknown.
        if not bool(getattr(cfg, 'young_allow_unknown_age', True)):
            return False
        if remaining < float(getattr(cfg, 'young_unknown_age_min_remaining_years', 0.0) or 0.0):
            return False
        return True

    return age_f <= float(getattr(cfg, 'young_max_age', 25.0) or 25.0)


def _choose_top_need_tags(tick_ctx: TradeGenerationTickContext, team_id: str, cfg: DealGeneratorConfig) -> List[str]:
    dc = tick_ctx.get_decision_context(team_id)
    need_map = getattr(dc, "need_map", None) or {}
    if not isinstance(need_map, Mapping) or not need_map:
        return []
    items = [(str(k), float(v or 0.0)) for k, v in need_map.items() if k is not None]
    items = [(k, v) for k, v in items if v >= float(cfg.need_tags_min_weight)]
    items.sort(key=lambda x: (-x[1], x[0]))
    out: List[str] = []
    for k, _ in items:
        if k and k not in out:
            out.append(k)
        if len(out) >= int(cfg.need_tags_max):
            break
    return out


def _team_need_map(tick_ctx: TradeGenerationTickContext, team_id: str) -> Dict[str, float]:
    """Safely extract need_map for a team (string->float)."""
    try:
        dc = tick_ctx.get_decision_context(team_id)
        nm = getattr(dc, "need_map", None) or {}
        if not isinstance(nm, Mapping):
            return {}
        out: Dict[str, float] = {}
        for k, v in nm.items():
            if k is None:
                continue
            kk = str(k)
            if not kk:
                continue
            try:
                out[kk] = float(v or 0.0)
            except Exception:
                out[kk] = 0.0
        return out
    except Exception:
        return {}


def _need_fit_score(supply: Any, need_map: Mapping[str, float]) -> float:
    """Dot(supply, need_map). supply is expected to be Mapping[tag->strength]."""
    if not need_map:
        return 0.0
    if not isinstance(supply, Mapping) or not supply:
        return 0.0
    s = 0.0
    for tag, sv in supply.items():
        try:
            s += float(sv or 0.0) * float(need_map.get(str(tag), 0.0) or 0.0)
        except Exception:
            continue
    return float(s)


def _extract_fit_fail_tags(dec: Any) -> Set[str]:
    """Extract focused tags/needs that caused FIT_FAILS (if present).

    Reason objects differ by implementation, so we try multiple fields:
      - r.meta / r.details / r.data (mapping)
      - keys: 'tags', 'need_tags', 'missing_tags', 'failed_tags', 'positions'
    Returns an uppercased tag set for robust matching.
    """
    out: Set[str] = set()
    if dec is None:
        return out
    reasons = getattr(dec, "reasons", None) or tuple()
    for r in reasons:
        try:
            code = str(getattr(r, "code", "") or "")
        except Exception:
            code = ""
        if code != "FIT_FAILS":
            continue
        meta = None
        for attr in ("meta", "details", "data"):
            v = getattr(r, attr, None)
            if isinstance(v, Mapping):
                meta = v
                break
        if not isinstance(meta, Mapping):
            continue
        for k in ("need_tags", "tags", "missing_tags", "failed_tags", "positions"):
            v = meta.get(k)
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                for t in v:
                    tt = str(t or "").strip()
                    if tt:
                        out.add(tt.upper())
            else:
                # allow comma/space separated string
                s = str(v or "")
                for part in re.split(r"[,\s]+", s):
                    part = part.strip()
                    if part:
                        out.add(part.upper())
    return out



def _extract_fit_failed_incoming_player_ids(dec: Any) -> Set[str]:
    """Extract player_ids of incoming assets that failed fit (DecisionPolicy FIT_FAILS meta).

    DecisionPolicy populates FIT_FAILS.meta as:
      {"failed_count": int, "failed_samples": [{"asset_key": "player:<id>", "ref_id": <id>, ...}, ...]}

    We primarily use asset_key == 'player:<id>'. As a conservative fallback, we accept
    numeric-only ref_id values (to avoid treating pick ids as player ids).
    """
    out: Set[str] = set()
    if dec is None:
        return out
    reasons = getattr(dec, "reasons", None) or tuple()
    for r in reasons:
        try:
            code = str(getattr(r, "code", "") or "")
        except Exception:
            code = ""
        if code != "FIT_FAILS":
            continue
        meta = None
        for attr in ("meta", "details", "data"):
            v = getattr(r, attr, None)
            if isinstance(v, Mapping):
                meta = v
                break
        if not isinstance(meta, Mapping):
            continue
        samples = meta.get("failed_samples")
        if not isinstance(samples, (list, tuple)):
            continue
        for s in samples:
            if not isinstance(s, Mapping):
                continue
            akey = str(s.get("asset_key") or "").strip()
            if akey.startswith("player:"):
                pid = akey.split("player:", 1)[1].strip()
                if pid:
                    out.add(pid)
                continue
            rid = str(s.get("ref_id") or "").strip()
            if rid and re.match(r"^[0-9]+$", rid):
                out.add(rid)
    return out
