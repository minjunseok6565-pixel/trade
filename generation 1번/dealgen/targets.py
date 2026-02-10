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

from .types import DealGeneratorConfig, DealGeneratorBudget, TargetCandidate, SellAssetCandidate


# =============================================================================
# Target selection
# =============================================================================


def select_targets_buy(
    buyer_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    *,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_players: Set[str],
) -> List[TargetCandidate]:
    """need_map 기반 BUY 타깃 후보 구성(우선순위 높은 후보가 앞)."""

    dc = tick_ctx.get_decision_context(buyer_id)
    need_map = dict(getattr(dc, "need_map", {}) or {})

    # fallback: TeamSituation.needs
    if not need_map:
        ts = tick_ctx.get_team_situation(buyer_id)
        for n in getattr(ts, "needs", []) or []:
            try:
                need_map[str(getattr(n, "tag", ""))] = float(getattr(n, "weight", 0.0) or 0.0)
            except Exception:
                continue

    tags_sorted = sorted(need_map.items(), key=lambda kv: float(kv[1] or 0.0), reverse=True)
    tags = [str(t) for t, w in tags_sorted if str(t).strip() and float(w or 0.0) > 0.0]

    ts = tick_ctx.get_team_situation(buyer_id)
    if str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper() == "STAND_PAT":
        tags = tags[:2]
    tags = tags[: int(config.need_tags_max)]

    buyer_u = str(buyer_id).upper()
    seller_out_cache: Dict[str, Optional[TeamOutgoingCatalog]] = {}
    seller_cooldown_cache: Dict[str, bool] = {}

    out: List[TargetCandidate] = []
    for tag in tags:
        refs: Sequence[IncomingPlayerRef] = catalog.incoming_by_need_tag.get(tag, tuple())
        if not refs and bool(config.incoming_use_cheap_pool):
            refs = catalog.incoming_cheap_by_need_tag.get(tag, tuple())
        w_need = float(need_map.get(tag, 0.0) or 0.0)

        # CORE가 섞여 있을 수 있으므로, 후보를 먼저 자르지 말고 일정 범위 내에서 "채울 때까지" 스캔한다.
        need_n = int(config.incoming_pool_per_tag)
        scan_limit = min(len(refs), need_n * 3)  # 3배 스캔 상한(고정)

        added_for_tag = 0
        for r in refs[:scan_limit]:
            from_team = str(r.from_team).upper()
            if from_team == buyer_u:
                continue
            if r.player_id in banned_players:
                continue

            # seller outgoing catalog 확보(캐시)
            seller_out = seller_out_cache.get(from_team)
            if seller_out is None and from_team not in seller_out_cache:
                seller_out = catalog.outgoing_by_team.get(from_team)
                seller_out_cache[from_team] = seller_out
            if seller_out is None:
                continue

            # seller cooldown 미리 컷(캐시)
            cd = seller_cooldown_cache.get(from_team)
            if cd is None:
                ts_seller = tick_ctx.get_team_situation(from_team)
                cd = bool(getattr(ts_seller, "constraints", None) and ts_seller.constraints.cooldown_active)
                seller_cooldown_cache[from_team] = cd
            if cd:
                continue

            # CORE/비매물 컷(타깃 단계에서)
            if not _is_seller_willing_to_move_player(r.player_id, seller_out):
                continue

            # 가벼운 rank score는 정렬에만 사용
            rank = float(r.tag_strength) * (0.55 + 0.45 * w_need) + 0.02 * float(r.market_total)
            rank -= 0.015 * float(r.salary_m)
            rank += rng.random() * 0.01
            out.append(
                TargetCandidate(
                    player_id=r.player_id,
                    from_team=from_team,
                    need_tag=str(tag),
                    tag_strength=float(rank),
                    market_total=float(r.market_total),
                    salary_m=float(r.salary_m),
                    remaining_years=float(r.remaining_years),
                    age=r.age,
                )
            )

            added_for_tag += 1
            if added_for_tag >= need_n:
                break

    out.sort(key=lambda t: (-t.tag_strength, -t.market_total, t.salary_m, t.player_id))
    return out[: int(budget.max_targets)]


def select_targets_sell(
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    *,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_players: Set[str],
) -> List[SellAssetCandidate]:
    """SELL 모드: initiator가 내놓을 매물(선수) 후보를 고른다."""

    out_cat = catalog.outgoing_by_team.get(str(seller_id).upper())
    if out_cat is None:
        return []

    # SELL 모드 우선순위: 현실적으로 팔 만한 버킷 중심
    priority: Tuple[BucketId, ...] = (
        "VETERAN_SALE",
        "EXPIRING",
        "SURPLUS_REDUNDANT",
        "SURPLUS_LOW_FIT",
        "FILLER_BAD_CONTRACT",
        "CONSOLIDATE",
        "FILLER_CHEAP",
    )

    ids: List[str] = []
    for b in priority:
        ids.extend(list(out_cat.player_ids_by_bucket.get(b, tuple())))

    # unique preserve order
    seen: Set[str] = set()
    uniq_ids = []
    for pid in ids:
        if pid in seen or pid in banned_players:
            continue
        seen.add(pid)
        uniq_ids.append(pid)

    sale: List[SellAssetCandidate] = []
    for pid in uniq_ids:
        c = out_cat.players.get(pid)
        if c is None:
            continue
        sale.append(
            SellAssetCandidate(
                player_id=pid,
                market_total=float(c.market.total),
                salary_m=float(c.salary_m),
                remaining_years=float(c.remaining_years),
                is_expiring=bool(c.is_expiring),
                top_tags=tuple(c.top_tags or ()),
            )
        )

    # prefer higher market & more surplus (heuristic: expiring + low fit)
    def score(x: SellAssetCandidate) -> float:
        exp_bonus = 0.7 if x.is_expiring else 0.0
        return float(x.market_total) + exp_bonus - 0.12 * float(x.remaining_years)

    sale.sort(key=lambda x: (-score(x), x.salary_m, x.player_id))

    # deterministically shuffle a bit within top slice for variety
    top = sale[: max(0, min(len(sale), 24))]
    rng.shuffle(top)
    sale = top + sale[len(top) :]

    return sale[: int(budget.max_targets)]


def select_buyers_for_sale_asset(
    seller_id: str,
    sale_asset: SellAssetCandidate,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    *,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
) -> List[Tuple[str, str]]:
    """SELL 모드: 특정 매물에 대해 관심 가질 가능성이 큰 buyer 팀을 고른다.

    Returns: list[(buyer_id, match_tag)]
    """

    tags = list(sale_asset.top_tags or ())
    if not tags:
        return []

    # 후보 팀: 전체 30팀에서 계산해도 비싸지 않지만, 여기선 상한을 둔다.
    # BUY 의지가 있는 팀을 우선.
    all_teams = list(catalog.outgoing_by_team.keys())
    rng.shuffle(all_teams)

    rows: List[Tuple[float, str, str]] = []
    for tid in all_teams:
        buyer_id = str(tid).upper()
        if buyer_id == str(seller_id).upper():
            continue

        ts = tick_ctx.get_team_situation(buyer_id)
        if bool(getattr(ts, "constraints", None) and ts.constraints.cooldown_active):
            continue

        posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
        posture_bonus = {
            "AGGRESSIVE_BUY": 1.2,
            "SOFT_BUY": 0.7,
            "STAND_PAT": 0.2,
            "SOFT_SELL": -0.3,
            "SELL": -0.6,
        }.get(posture, 0.0)

        dc = tick_ctx.get_decision_context(buyer_id)
        need_map = dict(getattr(dc, "need_map", {}) or {})

        best_tag = tags[0]
        best = 0.0
        for tag in tags[:4]:
            v = float(need_map.get(tag, 0.0) or 0.0)
            if v > best:
                best = v
                best_tag = tag

        urgency = float(getattr(ts, "urgency", 0.0) or 0.0)
        score = best + 0.35 * urgency + posture_bonus

        # 매우 낮으면 제외
        if score <= 0.10:
            continue

        rows.append((score, buyer_id, str(best_tag)))

        # hard cap to avoid worst-case O(teams*targets) blow-up
        if len(rows) >= 24:
            break

    rows.sort(key=lambda r: (-r[0], r[1]))

    # 최대 ~10팀만
    out = [(buyer_id, tag) for _, buyer_id, tag in rows[:10]]
    return out




def _is_seller_willing_to_move_player(player_id: str, seller_out: TeamOutgoingCatalog) -> bool:
    core = set(seller_out.player_ids_by_bucket.get("CORE", tuple()))
    if player_id in core:
        return False
    for b, ids in seller_out.player_ids_by_bucket.items():
        if b == "CORE":
            continue
        if player_id in ids:
            return True
    return False
