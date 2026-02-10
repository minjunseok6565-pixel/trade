from __future__ import annotations

"""dealgen.candidates

Split-out candidate selection and deal leg manipulation helpers.
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
from .utils import _canon_team_id
from .need_fit import _need_fit_score

def _pick_from_buckets(
    outcat: TeamOutgoingCatalog,
    buckets: Sequence[str],
    *,
    exclude_players: Set[str],
    to_team: str,
    max_n: int,
    prefer_low_market: bool = True,
    receiver_need_map: Optional[Mapping[str, float]] = None,
    current_outgoing_players_count: int = 0,
) -> List[PlayerTradeCandidate]:
    """Select player candidates from outgoing buckets.

    Uses TeamOutgoingCatalog ordering (already deterministic).
    """
    selected: List[PlayerTradeCandidate] = []
    seen: Set[str] = set(exclude_players or set())
    for b in buckets:
        ids = outcat.player_ids_by_bucket.get(b, tuple()) or tuple()
        for pid in ids:
            if pid in seen:
                continue
            cand = outcat.players.get(pid)
            if cand is None:
                continue
            # aggregation_solo_only: cannot aggregate with other outgoing players in the same package
            try:
                if bool(getattr(cand, "aggregation_solo_only", False)) and int(current_outgoing_players_count) >= 1:
                    continue
            except Exception:
                pass
            # return-to-team ban
            if to_team and to_team in {str(t).upper() for t in (cand.return_ban_teams or tuple())}:
                continue
            selected.append(cand)
            seen.add(pid)
            if len(selected) >= int(max_n):
                break
        if len(selected) >= int(max_n):
            break

    def _fit(c: PlayerTradeCandidate) -> float:
        try:
            return _need_fit_score(getattr(c, "supply", None) or {}, receiver_need_map or {})
        except Exception:
            return 0.0

    if prefer_low_market:
        # fillers: keep market low; fit is a tie-breaker for plausibility
        selected.sort(key=lambda c: (float(c.market.total), float(c.salary_m), -_fit(c), c.player_id))
    else:
        # value pieces: prioritize receiver fit, then market
        selected.sort(key=lambda c: (-_fit(c), -float(c.market.total), -float(c.salary_m), c.player_id))
    return selected[: int(max_n)]


def _closest_salary_players(
    outcat: TeamOutgoingCatalog,
    *,
    target_salary_m: float,
    exclude_players: Set[str],
    to_team: str,
    max_n: int,
    receiver_need_map: Optional[Mapping[str, float]] = None,
) -> List[PlayerTradeCandidate]:
    # pool from all non-core outgoing buckets
    pool_ids: List[str] = []
    for b, ids in (outcat.player_ids_by_bucket or {}).items():
        if b == "CORE":
            continue
        pool_ids.extend(list(ids or []))
    uniq: List[str] = []
    seen: Set[str] = set()
    for pid in pool_ids:
        if pid in seen:
            continue
        seen.add(pid)
        uniq.append(pid)

    pool: List[PlayerTradeCandidate] = []
    for pid in uniq:
        if pid in exclude_players:
            continue
        cand = outcat.players.get(pid)
        if cand is None:
            continue
        if to_team and to_team in {str(t).upper() for t in (cand.return_ban_teams or tuple())}:
            continue
        pool.append(cand)

    def _fit(c: PlayerTradeCandidate) -> float:
        try:
            return _need_fit_score(getattr(c, "supply", None) or {}, receiver_need_map or {})
        except Exception:
            return 0.0

    # prioritize salary closeness, then receiver fit, then lower market (avoid overpay artifacts)
    pool.sort(key=lambda c: (abs(float(c.salary_m) - float(target_salary_m)), -_fit(c), float(c.market.total), c.player_id))
    return pool[: int(max_n)]


def _player_asset(pid: str) -> PlayerAsset:
    return PlayerAsset(kind="player", player_id=str(pid), to_team=None)


def _pick_asset(pid: str, outcat: TeamOutgoingCatalog) -> Optional[PickAsset]:
    cand = outcat.picks.get(str(pid))
    if cand is None:
        return None
    return PickAsset(kind="pick", pick_id=str(pid), to_team=None, protection=cand.snap.protection)


def _swap_asset(sid: str, outcat: TeamOutgoingCatalog) -> Optional[SwapAsset]:
    cand = outcat.swaps.get(str(sid))
    if cand is None:
        return None
    return SwapAsset(
        kind="swap",
        swap_id=str(cand.swap_id),
        pick_id_a=str(cand.snap.pick_id_a),
        pick_id_b=str(cand.snap.pick_id_b),
        to_team=None,
    )


def _remove_one_incoming_player(
    deal: Deal,
    *,
    receiver_team: str,
    protected_players: Set[str],
    prefer_remove_high_salary: bool,
    catalog: TradeAssetCatalog,
) -> bool:
    """Remove one player that is incoming to receiver_team.

    In 2-team deals, incoming to receiver_team are the PlayerAssets in the *other* leg.
    """
    if len(deal.teams) != 2:
        return False
    t0, t1 = deal.teams
    other = t1 if receiver_team == t0 else t0
    assets = list(deal.legs.get(other, []) or [])
    player_assets = [a for a in assets if isinstance(a, PlayerAsset)]
    if not player_assets:
        return False

    # Rank removable players.
    ranked: List[Tuple[float, float, str, PlayerAsset]] = []
    out_other = catalog.outgoing_by_team.get(_canon_team_id(other))
    for a in player_assets:
        pid = str(a.player_id)
        if pid in protected_players:
            continue
        salary_m = 0.0
        market_total = 0.0
        if out_other and pid in out_other.players:
            c = out_other.players[pid]
            salary_m = float(c.salary_m)
            market_total = float(c.market.total)
        key = (-salary_m if prefer_remove_high_salary else market_total)
        ranked.append((float(key), float(market_total), pid, a))

    if not ranked:
        return False
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    _, __, pid, asset_to_remove = ranked[0]
    new_other_leg = [a for a in assets if not (isinstance(a, PlayerAsset) and str(a.player_id) == pid)]
    deal.legs[other] = new_other_leg
    return True


def _add_one_outgoing_filler_player(
    deal: Deal,
    *,
    from_team: str,
    to_team: str,
    catalog: TradeAssetCatalog,
    exclude_players: Set[str],
    max_outgoing_players: int,
    target_add_salary_m: Optional[float] = None,
) -> bool:
    if len(deal.teams) != 2:
        return False
    from_team_u = _canon_team_id(from_team)
    to_team_u = _canon_team_id(to_team)
    outcat = catalog.outgoing_by_team.get(from_team_u)
    if outcat is None:
        return False
    current_leg = list(deal.legs.get(from_team_u, []) or [])
    current_out_players = [a for a in current_leg if isinstance(a, PlayerAsset)]

    # Always exclude players already present in the deal to avoid DUPLICATE_ASSET failures.
    exclude = set(exclude_players or set())
    try:
        for assets in (deal.legs or {}).values():
            for a in (assets or []):
                if isinstance(a, PlayerAsset):
                    exclude.add(str(a.player_id))
    except Exception:
        pass
    if len(current_out_players) >= int(max_outgoing_players):
        return False

    # Choose filler. If we know "needed salary gap", prefer salary that closes it while keeping market low.
    candidates = _pick_from_buckets(
        outcat,
        buckets=("FILLER_CHEAP", "FILLER_BAD_CONTRACT", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT"),
        exclude_players=exclude,
        to_team=to_team_u,
        max_n=10,
        prefer_low_market=True,
        current_outgoing_players_count=len(current_out_players),
    )
    if target_add_salary_m is not None:
        gap = max(0.0, float(target_add_salary_m))
        # Prefer candidates that meet/exceed the gap (more likely to fix matching),
        # then closest to the gap, then lowest market.
        candidates.sort(
            key=lambda c: (
                0 if float(c.salary_m) >= gap else 1,
                abs(float(c.salary_m) - gap),
                float(c.market.total),
                c.player_id,
            )
        )
    for c in candidates:
        # aggregation solo-only cannot be aggregated with others.
        if bool(c.aggregation_solo_only) and len(current_out_players) >= 1:
            continue
        deal.legs[from_team_u] = current_leg + [_player_asset(c.player_id)]
        return True
    return False


def _enforce_one_for_one_players(
    deal: Deal,
    *,
    protected_players: Set[str],
    catalog: TradeAssetCatalog,
) -> bool:
    """Reduce both legs to <= 1 PlayerAsset each.

    Returns True if modified.
    """
    if len(deal.teams) != 2:
        return False
    modified = False
    for team_id in list(deal.teams):
        leg = list(deal.legs.get(team_id, []) or [])
        players = [a for a in leg if isinstance(a, PlayerAsset)]
        if len(players) <= 1:
            continue

        # Remove extras, keep protected if possible.
        keep: Optional[str] = None
        for a in players:
            pid = str(a.player_id)
            if pid in protected_players:
                keep = pid
                break
        if keep is None:
            # Keep the highest market total among outgoing leg players.
            outcat = catalog.outgoing_by_team.get(_canon_team_id(team_id))
            best_pid = None
            best_v = -1e9
            if outcat is not None:
                for a in players:
                    pid = str(a.player_id)
                    c = outcat.players.get(pid)
                    v = float(c.market.total) if c else 0.0
                    if v > best_v:
                        best_v = v
                        best_pid = pid
            keep = best_pid or str(players[0].player_id)

        new_leg: List[Any] = []
        kept_player = False
        for a in leg:
            if isinstance(a, PlayerAsset):
                if not kept_player and str(a.player_id) == keep:
                    new_leg.append(a)
                    kept_player = True
                else:
                    modified = True
                    continue
            else:
                new_leg.append(a)
        deal.legs[team_id] = new_leg

    return modified
