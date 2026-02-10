from __future__ import annotations

"""dealgen.salary

Split-out salary/apron helpers for the deal generator.
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

def _player_salary_amount_dollars(
    player_id: str,
    *,
    from_team: Optional[str],
    tick_ctx: TradeGenerationTickContext,
    catalog: Optional[TradeAssetCatalog] = None,
) -> float:
    """Return salary_amount in dollars (not millions).

    Prefer tick_ctx.rule_tick_ctx active roster index. Fallback to catalog snapshots.
    """
    pid = str(player_id)
    try:
        rtc = getattr(tick_ctx, "rule_tick_ctx", None)
        if rtc is not None:
            try:
                rtc.ensure_active_roster_index()
            except Exception:
                pass
            sal = getattr(rtc, "player_salary_map", {}).get(pid)
            if sal is not None:
                return float(sal or 0.0)
    except Exception:
        pass

    # Fallback: outgoing catalog snapshot (best-effort, may be missing)
    try:
        if catalog is not None and from_team:
            outcat = catalog.outgoing_by_team.get(_canon_team_id(from_team))
            if outcat is not None:
                c = outcat.players.get(pid)
                if c is not None:
                    snap = getattr(c, "snap", None)
                    if snap is not None and getattr(snap, "salary_amount", None) is not None:
                        return float(getattr(snap, "salary_amount") or 0.0)
                    return float(getattr(c, "salary_m", 0.0) or 0.0) * 1_000_000.0
    except Exception:
        pass

    return 0.0


def _estimate_team_payroll_after_dollars(
    deal: Deal,
    *,
    team_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
) -> Optional[float]:
    """Estimate payroll_after in dollars for a team (2-team deals only).

    This mirrors SalaryMatchingRule logic closely enough for *gating* decisions:
    we only need to know whether payroll_after is at/above the 2nd apron.
    """
    if len(deal.teams) != 2:
        return None
    tid = _canon_team_id(team_id)

    # payroll_before: prefer tick index (fast, accurate)
    payroll_before = None
    try:
        rtc = getattr(tick_ctx, "rule_tick_ctx", None)
        if rtc is not None:
            try:
                rtc.ensure_active_roster_index()
            except Exception:
                pass
            pb = getattr(rtc, "team_payroll_before_map", {}).get(tid)
            if pb is not None:
                payroll_before = float(pb or 0.0)
    except Exception:
        payroll_before = None
    if payroll_before is None:
        try:
            ts = tick_ctx.get_team_situation(tid)
            c = getattr(ts, "constraints", None)
            payroll_before = float(getattr(c, "payroll", 0.0) or 0.0)
        except Exception:
            payroll_before = 0.0

    outgoing_salary = 0.0
    incoming_salary = 0.0
    for sender_team, assets in (deal.legs or {}).items():
        sender_u = _canon_team_id(sender_team)
        for a in (assets or []):
            if not isinstance(a, PlayerAsset):
                continue
            sal = _player_salary_amount_dollars(str(a.player_id), from_team=sender_u, tick_ctx=tick_ctx, catalog=catalog)
            if sender_u == tid:
                outgoing_salary += float(sal)
            receiver_u = _resolve_receiver_team_id_2team(deal, sender_u, a)
            if receiver_u == tid:
                incoming_salary += float(sal)

    return float(payroll_before) - float(outgoing_salary) + float(incoming_salary)


def _is_one_for_one_mode(*, deal: Deal, team_id: str, tick_ctx: TradeGenerationTickContext, catalog: TradeAssetCatalog) -> bool:
    """Conservative 'one-for-one' detector.

    Returns True when the team is already ABOVE_2ND_APRON, or when the deal would put payroll_after
    at/above (second_apron - match_buffer). Used to steer generation/repair away from invalid 2+ player packages.
    """
    tid = _canon_team_id(team_id)
    try:
        ts = tick_ctx.get_team_situation(tid)
        if _team_apron_status(ts) == "ABOVE_2ND_APRON":
            return True
    except Exception:
        pass

    tr = _trade_rules(tick_ctx)
    try:
        second_apron = float(tr.get("second_apron") or 0.0)
    except Exception:
        second_apron = 0.0
    if second_apron <= 0:
        return False

    try:
        buffer = float(tr.get("match_buffer") or 250_000)
    except Exception:
        buffer = 250_000.0

    payroll_after = _estimate_team_payroll_after_dollars(deal, team_id=tid, tick_ctx=tick_ctx, catalog=catalog)
    if payroll_after is None:
        return False

    return float(payroll_after) >= float(second_apron - buffer)
