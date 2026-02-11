from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import hashlib
import json
import math
import random
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ...errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    TRADE_DEADLINE_PASSED,
    DUPLICATE_ASSET,
)
from ...models import (
    Deal,
    PlayerAsset,
    PickAsset,
    SwapAsset,
    Asset,
    asset_key,
    canonicalize_deal,
    serialize_deal,
    resolve_asset_receiver,
)
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, DealVerdict, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import (
    TradeAssetCatalog,
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    PickBucketId,
    BucketId,
    build_trade_asset_catalog,
)

from .types import DealGeneratorConfig

# =============================================================================
# Rule SSOT helpers (trade_rules / apron thresholds)
# =============================================================================


def _trade_rules_map(tick_ctx: TradeGenerationTickContext) -> Mapping[str, Any]:
    base = getattr(getattr(tick_ctx, "rule_tick_ctx", None), "ctx_state_base", None)
    if isinstance(base, dict):
        league = base.get("league") if isinstance(base.get("league"), dict) else {}
        tr = league.get("trade_rules") if isinstance(league.get("trade_rules"), dict) else {}
        if isinstance(tr, dict):
            return tr
    return {}


def _get_trade_deadline_date(tick_ctx: TradeGenerationTickContext) -> Optional[date]:
    tr = _trade_rules_map(tick_ctx)
    raw = tr.get("trade_deadline")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except Exception:
        return None


def _get_second_apron_threshold(tick_ctx: TradeGenerationTickContext) -> float:
    tr = _trade_rules_map(tick_ctx)
    try:
        return float(tr.get("second_apron") or 0.0)
    except Exception:
        return 0.0


def _player_salary_dollars(tick_ctx: TradeGenerationTickContext, player_id: str) -> float:
    # Prefer tick SSOT salary map (dollars). Fallback: asset_catalog salary_m.
    pid = str(player_id)
    rt = getattr(tick_ctx, "rule_tick_ctx", None)
    try:
        if rt is not None:
            rt.ensure_active_roster_index()
            sal = rt.player_salary_map.get(pid)
            if sal is not None:
                return float(sal)
    except Exception:
        pass

    try:
        rt.ensure_active_roster_index()  # type: ignore[name-defined]
        owner = rt.player_team_map.get(pid)  # type: ignore[attr-defined]
    except Exception:
        owner = None

    cat = getattr(tick_ctx, "asset_catalog", None)
    if owner and cat is not None:
        out = cat.outgoing_by_team.get(str(owner).upper())
        if out is not None:
            c = out.players.get(pid)
            if c is not None:
                try:
                    return float(c.salary_m) * 1_000_000.0
                except Exception:
                    return 0.0
    return 0.0


def _estimate_team_payroll_after_dollars(
    tick_ctx: TradeGenerationTickContext,
    deal: Deal,
    team_id: str,
) -> float:
    # Estimate payroll_after (dollars) for soft 2nd apron guarding.
    tid = str(team_id).upper()
    rt = getattr(tick_ctx, "rule_tick_ctx", None)
    payroll_before = 0.0
    if rt is not None:
        try:
            rt.ensure_active_roster_index()
            payroll_before = float(rt.team_payroll_before_map.get(tid, 0.0))
        except Exception:
            payroll_before = 0.0

    if payroll_before <= 0.0:
        try:
            ts = tick_ctx.get_team_situation(tid)
            payroll_before = float(getattr(getattr(ts, "constraints", None), "payroll", 0.0) or 0.0)
        except Exception:
            payroll_before = 0.0

    outgoing = 0.0
    for a in deal.legs.get(tid, []) or []:
        if isinstance(a, PlayerAsset):
            outgoing += _player_salary_dollars(tick_ctx, a.player_id)

    incoming = 0.0
    for from_team, assets in deal.legs.items():
        if str(from_team).upper() == tid:
            continue
        for a in assets or []:
            if isinstance(a, PlayerAsset):
                incoming += _player_salary_dollars(tick_ctx, a.player_id)

    return float(payroll_before - outgoing + incoming)




# -----------------------------------------------------------------------------
# Cap space helpers (shared)
# -----------------------------------------------------------------------------

def _cap_space_m(ts: Any) -> float:
    """TeamConstraints.cap_space는 달러 단위로 들어오는 경우가 많아서(프로젝트 코드 기준) M 단위로 변환."""
    try:
        c = getattr(ts, "constraints", None)
        v = float(getattr(c, "cap_space", 0.0) or 0.0)
    except Exception:
        return 0.0
    return v / 1_000_000.0


def _can_absorb_without_outgoing(ts: Any, incoming_salary_m: float, *, buffer_m: float = 0.25) -> bool:
    """플레이어를 보내지 않고(incoming only) salary를 흡수 가능한지(=cap space로 커버)."""
    cap_m = _cap_space_m(ts)
    return cap_m >= float(incoming_salary_m) + float(buffer_m)


def _clone_deal(deal: Deal) -> Deal:
    return Deal(
        teams=list(deal.teams),
        legs={tid: list(assets) for tid, assets in deal.legs.items()},
    )


def _shape_ok(deal: Deal, *, config: DealGeneratorConfig, catalog: Optional[TradeAssetCatalog] = None) -> bool:
    for assets in deal.legs.values():
        if len(assets) > int(config.max_assets_per_side):
            return False

    n_players = sum(1 for leg in deal.legs.values() for a in leg if isinstance(a, PlayerAsset))
    if n_players > int(config.max_players_moved_total):
        return False

    for tid in deal.teams:
        tid_u = str(tid).upper()
        if _count_players(deal, tid_u) > int(config.max_players_per_side):
            return False
        if _count_picks(deal, tid_u) > int(config.max_picks_per_side):
            return False
        if _count_seconds(deal, tid_u, catalog=catalog) > int(config.max_seconds_per_side):
            return False

    return True


def _count_players(deal: Deal, team_id: str) -> int:
    return sum(1 for a in deal.legs.get(str(team_id).upper(), []) if isinstance(a, PlayerAsset))


def _count_picks(deal: Deal, team_id: str) -> int:
    return sum(1 for a in deal.legs.get(str(team_id).upper(), []) if isinstance(a, PickAsset))


def _count_seconds(deal: Deal, team_id: str, *, catalog: Optional[TradeAssetCatalog] = None) -> int:
    """Deal 내 2라운드 픽 수(best-effort).

    SSOT는 PickSnapshot.round 이지만, deal에는 pick_id만 있으므로:
    - catalog(outgoing_by_team)가 있으면 해당 팀의 SECOND bucket / PickSnapshot.round를 우선 사용
    - 없으면 id 문자열 휴리스틱으로 fallback
    """
    tid = str(team_id).upper()
    assets = deal.legs.get(tid, []) or []

    if catalog is None:
        return sum(1 for a in assets if isinstance(a, PickAsset) and _is_second_round_pick_id(a.pick_id))

    out_cat = catalog.outgoing_by_team.get(tid)
    if out_cat is None:
        return sum(1 for a in assets if isinstance(a, PickAsset) and _is_second_round_pick_id(a.pick_id))

    seconds_set = set(out_cat.pick_ids_by_bucket.get("SECOND", tuple()))
    cnt = 0
    for a in assets:
        if not isinstance(a, PickAsset):
            continue
        pid = str(a.pick_id)
        if pid in seconds_set:
            cnt += 1
            continue
        c = out_cat.picks.get(pid)
        if c is not None and int(getattr(c.snap, "round", 0) or 0) == 2:
            cnt += 1
            continue
        if _is_second_round_pick_id(pid):
            cnt += 1
    return cnt


def _count_swaps(deal: Deal, team_id: str) -> int:
    tid = str(team_id).upper()
    return sum(1 for a in deal.legs.get(tid, []) if isinstance(a, SwapAsset))


def _is_second_round_pick_id(pick_id: str) -> bool:
    # SSOT는 PickSnapshot.round 이지만, 여기선 id 기반으로는 확정이 어렵다.
    # 대신 catalog를 통해 생성된 pick은 대체로 id에 "R2" 등 표기가 있을 수 있으나 보장되지 않는다.
    # 안전하게: Deal 내 PickAsset만으로 판별 불가 -> seconds cap은 generator 단계에서
    # pick bucket(SECOND)로 추가할 때만 증가시키도록 쓰는 편이 이상적.
    # 여기서는 best-effort: 대부분 데이터셋에서 "R2"/"2ND" 포함.
    s = str(pick_id).upper()
    return ("R2" in s) or ("2ND" in s) or ("ROUND2" in s)


def _current_pick_ids(deal: Deal, team_id: str) -> Set[str]:
    tid = str(team_id).upper()
    return {a.pick_id for a in deal.legs.get(tid, []) if isinstance(a, PickAsset)}


def _team_pick_flow(deal: Deal, team_id: str) -> Tuple[Set[str], Set[str]]:
    """team_id 기준 (outgoing_pick_ids, incoming_pick_ids)."""

    tid = str(team_id).upper()
    out_ids: Set[str] = set()
    in_ids: Set[str] = set()

    for from_team, assets in deal.legs.items():
        for a in assets:
            if not isinstance(a, PickAsset):
                continue
            receiver = resolve_asset_receiver(deal, str(from_team), a)
            if str(from_team).upper() == tid:
                out_ids.add(str(a.pick_id))
            if str(receiver).upper() == tid:
                in_ids.add(str(a.pick_id))

    return out_ids, in_ids


# =============================================================================
# Asset picking helpers (bucket-aware)
# =============================================================================


def _pick_bucket_player(
    out: TeamOutgoingCatalog,
    *,
    bucket: BucketId,
    receiver_team_id: Optional[str] = None,
    banned_players: Optional[Set[str]] = None,
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
    must_be_aggregation_friendly: bool = True,
) -> Optional[str]:
    receiver = str(receiver_team_id).upper() if receiver_team_id else None
    for pid in list(out.player_ids_by_bucket.get(bucket, tuple())):
        pid_s = str(pid)
        if banned_players and pid_s in banned_players:
            continue
        c = out.players.get(pid_s)
        if c is None:
            continue
        if receiver and receiver in set(getattr(c, "return_ban_teams", None) or ()):
            continue
        if receiver and banned_receivers_by_player is not None:
            if receiver in banned_receivers_by_player.get(pid_s, set()):
                continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue
        return pid_s
    return None


def _pick_lowest_market_player(
    out: TeamOutgoingCatalog,
    *,
    buckets: Tuple[BucketId, ...],
    banned_players: Set[str],
) -> Optional[str]:
    """여러 버킷에서 market.total이 가장 낮은 플레이어를 선택(필러용)."""

    best_pid: Optional[str] = None
    best_mkt = float("inf")
    for b in buckets:
        for pid in out.player_ids_by_bucket.get(b, tuple()):
            if pid in banned_players:
                continue
            c = out.players.get(pid)
            m = float(c.market.total) if c is not None else 0.0
            if m < best_mkt:
                best_mkt = m
                best_pid = pid
    return best_pid


def _split_young_candidates(
    out: TeamOutgoingCatalog,
    *,
    config: DealGeneratorConfig,
    banned_players: Set[str],
    receiver_team_id: Optional[str] = None,
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
    must_be_aggregation_friendly: bool = True,
) -> Tuple[List[str], List[str]]:
    """
    Return (young_prospect_ids, young_throwin_ids).

    v2 parity:
    - young_prospect: top fraction (market.total desc) among young controllable players
    - young_throwin: young controllable players with market.total <= young_throwin_max_market,
      excluding prospect ids

    Filters:
    - receiver return_ban_teams + learned banned_receivers_by_player
    - aggregation_solo_only excluded if must_be_aggregation_friendly=True
    - uses buckets (SURPLUS_LOW_FIT, SURPLUS_REDUNDANT, FILLER_CHEAP, CONSOLIDATE)

    """
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    age_max = float(getattr(config, "young_age_max", 24.5) or 24.5)
    min_control = float(getattr(config, "young_min_control_years", 2.0) or 0.0)

    throwin_max = float(getattr(config, "young_throwin_max_market", 22.0) or 22.0)
    frac = float(getattr(config, "young_prospect_top_frac", 0.35) or 0.35)
    # clamp frac to avoid 0 prospect in small pools
    frac = max(0.05, min(1.0, frac))

    max_prospect = int(getattr(config, "young_prospect_max_candidates", 6) or 0)
    max_throwin = int(getattr(config, "young_throwin_max_candidates", 6) or 0)

    def _eligible(c: PlayerTradeCandidate, *, require_control: bool) -> bool:
        if receiver and receiver in set(getattr(c, "return_ban_teams", None) or ()):
            return False
        if receiver and banned_receivers_by_player is not None:
            if receiver in banned_receivers_by_player.get(str(c.player_id), set()):
                return False
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            return False
        age = getattr(getattr(c, "snap", None), "age", None)
        if age is None or float(age) > age_max:
            return False
        if require_control:
            try:
                ry = float(getattr(c, "remaining_years", 0.0) or 0.0)
            except Exception:
                ry = 0.0
            if ry < min_control:
                return False
        return True

    # Base pool from non-core-ish buckets (same as existing v1 youngish selection)
    base: List[PlayerTradeCandidate] = []
    for b in ("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_CHEAP", "CONSOLIDATE"):
        for pid in out.player_ids_by_bucket.get(b, tuple()):
            pid_s = str(pid)
            if pid_s in banned_players:
                continue
            c = out.players.get(pid_s)
            if c is None:
                continue
            base.append(c)

    if not base:
        return ([], [])

    # controllable young only (v2 parity; no age-only fallback)
    young_pool: List[PlayerTradeCandidate] = [c for c in base if _eligible(c, require_control=True)]
    if not young_pool:
        return ([], [])

    def _mkt(c: PlayerTradeCandidate) -> float:
        try:
            return float(c.market.total)
        except Exception:
            return 0.0

    young_sorted = sorted(young_pool, key=_mkt, reverse=True)

    # prospect count
    n = int(math.ceil(len(young_sorted) * frac))
    n = max(1, min(n, len(young_sorted)))
    if max_prospect > 0:
        n = min(n, max_prospect)

    prospect = young_sorted[:n]
    prospect_ids_set = {str(c.player_id) for c in prospect}

    # throw-in: cheap young bodies excluding prospects
    throwin: List[PlayerTradeCandidate] = []
    for c in young_sorted:
        pid = str(c.player_id)
        if pid in prospect_ids_set:
            continue
        if _mkt(c) <= throwin_max:
            throwin.append(c)

    # Final sort (v2 parity): prefer higher market, then lower salary, then stable id
    def _sort_key(c: PlayerTradeCandidate) -> tuple:
        mv = _mkt(c)
        try:
            sal = float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            sal = 0.0
        return (-mv, sal, str(c.player_id))

    prospect.sort(key=_sort_key)
    throwin.sort(key=_sort_key)

    prospect_ids = [str(c.player_id) for c in prospect]
    throwin_ids = [str(c.player_id) for c in throwin]

    if max_prospect > 0:
        prospect_ids = prospect_ids[:max_prospect]
    if max_throwin > 0:
        throwin_ids = throwin_ids[:max_throwin]

    return (prospect_ids, throwin_ids)


def _pick_youngish_player(
    out: TeamOutgoingCatalog,
    *,
    config: DealGeneratorConfig,
    banned_players: Set[str],
    receiver_team_id: Optional[str] = None,
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
    must_be_aggregation_friendly: bool = True,
) -> Optional[str]:
    """(legacy wrapper) young 후보 1명을 반환.
    - prospect/throw-in split 로직은 _split_young_candidates가 SSOT.
    - 이 함수는 기존 호출 호환을 위해 'prospect 우선, 없으면 throw-in'을 반환한다.
    """
    prospect_ids, throwin_ids = _split_young_candidates(
        out,
        config=config,
        banned_players=banned_players,
        receiver_team_id=receiver_team_id,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=must_be_aggregation_friendly,
    )
    pool = prospect_ids if prospect_ids else throwin_ids
    return pool[0] if pool else None

def _pick_filler_player_for_salary(
    out: TeamOutgoingCatalog,
    *,
    receiver_team_id: Optional[str],
    target_salary_m: float,
    banned_players: Set[str],
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
    must_be_aggregation_friendly: bool = True,
) -> Optional[str]:
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    ids: List[str] = []
    for b in ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT"):
        ids.extend(list(out.player_ids_by_bucket.get(b, tuple())))

    best: Optional[str] = None
    best_gap = 1e9
    for pid in ids:
        if pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue
        if receiver and receiver in set(getattr(c, "return_ban_teams", None) or ()):
            continue
        if receiver and banned_receivers_by_player is not None:
            if receiver in banned_receivers_by_player.get(str(pid), set()):
                continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue
        gap = abs(float(c.salary_m) - float(target_salary_m))
        if gap < best_gap:
            best_gap = gap
            best = pid

    return best


def _add_pick_package(
    deal: Deal,
    *,
    from_team: str,
    out_cat: TeamOutgoingCatalog,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    rng: random.Random,
    prefer: Tuple[str, ...],
    max_picks: int,
    banned_asset_keys: Optional[Set[str]] = None,
) -> None:
    """pick bucket 우선순위 기반으로 pick을 추가.

    catalog는 이미 lock/max_year를 필터했지만 Stepien은 조합에 따라 달라질 수 있으니
    1st 추가 시점에만 StepienHelper로 체크한다.
    """

    tid = str(from_team).upper()
    if tid not in deal.legs:
        return

    picks_added = 0
    outgoing_pick_ids = _current_pick_ids(deal, tid)

    def iter_bucket(name: str) -> Iterable[str]:
        if name == "FIRST_SAFE":
            return out_cat.pick_ids_by_bucket.get("FIRST_SAFE", tuple())
        if name == "FIRST_SENSITIVE":
            return out_cat.pick_ids_by_bucket.get("FIRST_SENSITIVE", tuple())
        if name == "SECOND":
            return out_cat.pick_ids_by_bucket.get("SECOND", tuple())
        return tuple()

    for bucket in prefer:
        if picks_added >= int(max_picks):
            break
        if bucket == "SWAP":
            continue

        for pid in iter_bucket(bucket):
            if picks_added >= int(max_picks):
                break
            if _count_picks(deal, tid) >= int(config.max_picks_per_side):
                break
            if bucket == "SECOND" and _count_seconds(deal, tid, catalog=catalog) >= int(config.max_seconds_per_side):
                break
            pid_s = str(pid)

            # (C) ownership/lock 등으로 금지된 pick은 스켈레톤 단계부터 제외
            if banned_asset_keys is not None and f"pick:{pid_s}" in banned_asset_keys:
                continue

            if pid_s in outgoing_pick_ids:
                continue

            # stepien check for 1st(s)
            if bucket.startswith("FIRST"):
                out_ids, in_ids = _team_pick_flow(deal, tid)
                if not catalog.stepien.is_compliant_after(team_id=tid, outgoing_pick_ids=set(out_ids | {pid_s}), incoming_pick_ids=set(in_ids)):
                    continue

            try:
                deal.legs[tid].append(out_cat.picks[pid_s].as_asset())
            except Exception:
                deal.legs[tid].append(PickAsset(kind="pick", pick_id=pid_s))
            outgoing_pick_ids.add(pid_s)
            picks_added += 1
            break


def _pick_best_pick_id(out_cat: TeamOutgoingCatalog, *, bucket: PickBucketId, excluded: Set[str]) -> Optional[str]:
    for pid in out_cat.pick_ids_by_bucket.get(bucket, tuple()):
        if pid in excluded:
            continue
        return str(pid)
    return None


def _pick_pick_id_matching_value(
    out_cat: TeamOutgoingCatalog,
    bucket: PickBucketId,
    *,
    excluded: Set[str],
    target_value: float,
) -> Optional[str]:
    """bucket 내 pick 중 market.total이 target_value와 가장 가까운 것을 선택."""

    cands = [str(pid) for pid in out_cat.pick_ids_by_bucket.get(bucket, tuple()) if str(pid) not in excluded]
    if not cands:
        return None

    # picks dict에는 market 포함
    def key(pid: str) -> float:
        p = out_cat.picks.get(pid)
        mv = float(p.market.total) if p is not None else 0.0
        return abs(mv - float(target_value))

    cands.sort(key=key)
    return cands[0]


# =============================================================================
# Dedupe helpers
# =============================================================================


# =============================================================================
# End
# =============================================================================
