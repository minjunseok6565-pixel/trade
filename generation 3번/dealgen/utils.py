from __future__ import annotations

"""dealgen.utils

Split-out pure helpers for the deal generator.
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

def _canon_team_id(team_id: Any) -> str:
    raw = str(team_id or "").strip()
    if not raw:
        return ""
    try:
        from schema import normalize_team_id  # type: ignore

        return str(normalize_team_id(raw, strict=False)).strip().upper()
    except Exception:
        return raw.upper()


def _sigmoid(x: float) -> float:
    # numerically stable enough for our bounded ranges
    try:
        return 1.0 / (1.0 + exp(-float(x)))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _deal_num_assets(deal: Deal) -> int:
    return int(sum(len(v or []) for v in (deal.legs or {}).values()))


def _deal_num_players_moved(deal: Deal) -> int:
    n = 0
    for assets in (deal.legs or {}).values():
        for a in assets or []:
            if isinstance(a, PlayerAsset):
                n += 1
    return int(n)


def _deal_outgoing_pick_ids(deal: Deal, team_id: str) -> Set[str]:
    out: Set[str] = set()
    for a in deal.legs.get(team_id, []) or []:
        if isinstance(a, PickAsset):
            out.add(str(a.pick_id))
    return out


def _deal_incoming_pick_ids(deal: Deal, team_id: str) -> Set[str]:
    """Return pick_ids that team_id would receive in this deal.

    For 2-team deals, incoming picks to team_id are PickAssets located in the *other*
    team's leg where (to_team is None) or (to_team == team_id).

    This helper exists mainly to improve Stepien quick-checks during generation.
    """
    t = _canon_team_id(team_id)
    out: Set[str] = set()
    try:
        teams = list(deal.teams or [])
        if len(teams) != 2:
            # Best-effort: scan all legs, treat None/explicit to_team as incoming.
            for sender, assets in (deal.legs or {}).items():
                sender_u = _canon_team_id(sender)
                if not sender_u or sender_u == t:
                    continue
                for a in assets or []:
                    if isinstance(a, PickAsset):
                        rt = getattr(a, "to_team", None)
                        if rt is None or _canon_team_id(rt) == t:
                            out.add(str(a.pick_id))
            return out

        t0 = _canon_team_id(teams[0])
        t1 = _canon_team_id(teams[1])
        other = t1 if t == t0 else t0
        for a in (deal.legs.get(other, []) or []):
            if isinstance(a, PickAsset):
                rt = getattr(a, "to_team", None)
                if rt is None or _canon_team_id(rt) == t:
                    out.add(str(a.pick_id))
    except Exception:
        return out
    return out


def _deal_assets_by_team(deal: Deal, team_id: str) -> Tuple[List[PlayerAsset], List[PickAsset], List[SwapAsset]]:
    ps: List[PlayerAsset] = []
    picks: List[PickAsset] = []
    swaps: List[SwapAsset] = []
    for a in deal.legs.get(team_id, []) or []:
        if isinstance(a, PlayerAsset):
            ps.append(a)
        elif isinstance(a, PickAsset):
            picks.append(a)
        elif isinstance(a, SwapAsset):
            swaps.append(a)
    return ps, picks, swaps


def _protected_player_ids_from_meta(deal: Deal) -> Set[str]:
    meta = deal.meta or {}
    ids = meta.get("protected_player_ids")
    if isinstance(ids, (list, tuple, set)):
        return {str(x) for x in ids if x is not None and str(x).strip()}
    return set()


def _hash_deal_for_dedupe(deal: Deal, *, ignore_meta: bool = True) -> str:
    """Stable content hash for dedupe.

    Important:
    - Must be deterministic across processes (so do NOT use Python's built-in hash()).
    - Keep representation compact (sha1) to reduce memory.

    Note: deal.meta is intentionally ignored by default because meta may contain
    debug/telemetry fields that should not affect structural dedupe.
    """
    try:
        canon = canonicalize_deal(deal)
    except Exception:
        canon = deal

    if ignore_meta:
        try:
            canon_for_hash = Deal(teams=list(canon.teams), legs={k: list(v) for k, v in (canon.legs or {}).items()}, meta={})
        except Exception:
            canon_for_hash = canon
    else:
        canon_for_hash = canon

    payload = serialize_deal(canon_for_hash)
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except Exception:
        raw = str(payload).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _team_posture(ts: Any) -> str:
    return str(getattr(ts, "trade_posture", "") or "").upper()


def _team_time_horizon(ts: Any) -> str:
    return str(getattr(ts, "time_horizon", "") or "").upper()


def _team_urgency(ts: Any) -> float:
    return float(getattr(ts, "urgency", 0.0) or 0.0)


def _team_deadline_pressure(ts: Any) -> float:
    c = getattr(ts, "constraints", None)
    return float(getattr(c, "deadline_pressure", 0.0) or 0.0)


def _team_cooldown(ts: Any) -> bool:
    c = getattr(ts, "constraints", None)
    return bool(getattr(c, "cooldown_active", False) or False)


def _team_cap_space(ts: Any) -> float:
    c = getattr(ts, "constraints", None)
    return float(getattr(c, "cap_space", 0.0) or 0.0)


def _team_apron_status(ts: Any) -> str:
    c = getattr(ts, "constraints", None)
    return str(getattr(c, "apron_status", "") or "").upper()


def _trade_rules(tick_ctx: TradeGenerationTickContext) -> Mapping[str, Any]:
    """Best-effort read of league.trade_rules from the tick snapshot.

    We intentionally read from rule_tick_ctx.ctx_state_base (SSOT for rules) instead of
    TeamSituation.constraints which may reflect heuristics or stale snapshots.
    """
    try:
        rtc = getattr(tick_ctx, "rule_tick_ctx", None)
        base = getattr(rtc, "ctx_state_base", None) or {}
        if isinstance(base, Mapping):
            league = base.get("league")
            if isinstance(league, Mapping):
                tr = league.get("trade_rules")
                if isinstance(tr, Mapping):
                    return tr
    except Exception:
        pass
    return {}


def _trade_deadline_date(tick_ctx: TradeGenerationTickContext) -> Optional[date]:
    tr = _trade_rules(tick_ctx)
    raw = tr.get("trade_deadline") if isinstance(tr, Mapping) else None
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except Exception:
        return None


def _resolve_receiver_team_id_2team(deal: Deal, sender_team: str, asset: Any) -> str:
    to_team = getattr(asset, "to_team", None)
    if to_team:
        return _canon_team_id(to_team)
    if len(deal.teams) == 2:
        t0 = _canon_team_id(deal.teams[0])
        t1 = _canon_team_id(deal.teams[1])
        s = _canon_team_id(sender_team)
        return t1 if s == t0 else t0
    return ""
