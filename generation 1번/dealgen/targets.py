from __future__ import annotations

from datetime import date
import random
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import TradeAssetCatalog, IncomingPlayerRef, TeamOutgoingCatalog

from .types import DealGeneratorConfig, DealGeneratorBudget, TargetCandidate, SellAssetCandidate
from .utils import _is_locked_candidate


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
    allow_locked_by_deal_id: Optional[str] = None,
) -> List[SellAssetCandidate]:
    """SELL 모드: initiator가 내놓을 매물(선수) 후보를 고른다.

    v2 정합 로직:
    - locked(allow_locked 예외 포함) 선필터
    - recent_signing_banned_until 선필터
    - CORE: SOFT_SELL에서는 제외, SELL에서는 아주 드물게(4%) 허용
    - 정렬: bucket priority -> surplus_score(desc) -> expiring(desc) -> market_total(asc) -> player_id
    - 상위 head만 소폭 셔플해 매번 같은 쇼핑리스트가 되지 않게 한다
    """

    seller_u = str(seller_id).upper()
    out_cat = catalog.outgoing_by_team.get(seller_u)
    if out_cat is None:
        return []

    max_targets = int(getattr(budget, "max_targets", 0) or 0)
    if max_targets <= 0:
        return []

    ts = tick_ctx.get_team_situation(seller_u)
    posture = str(getattr(ts, "trade_posture", "SELL") or "SELL").upper()

    allow_id = str(allow_locked_by_deal_id or "").strip() or None

    # v2와 동일한 우선순위(숫자 낮을수록 우선)
    bucket_pri: Dict[str, int] = {
        "VETERAN_SALE": 0,
        "EXPIRING": 1,
        "SURPLUS_LOW_FIT": 2,
        "SURPLUS_REDUNDANT": 3,
        "FILLER_CHEAP": 4,
        "FILLER_BAD_CONTRACT": 5,
        "CONSOLIDATE": 6,
        "CORE": 99,
    }

    rows: List[Tuple[Tuple[int, float, float, float, str], SellAssetCandidate]] = []

    for pid, c in (out_cat.players or {}).items():
        if not pid:
            continue
        if pid in banned_players:
            continue

        # (1) locked 선필터 (allow_locked 예외 포함)
        if _is_locked_candidate(getattr(c, "lock", None), allow_locked_by_deal_id=allow_id):
            continue

        # (2) recent signing ban 선필터
        if _is_ban_active(tick_ctx.current_date, getattr(c, "recent_signing_banned_until", None)):
            continue

        buckets = tuple(getattr(c, "buckets", None) or ())

        # (3) CORE 처리: SOFT_SELL에서는 제외, SELL에서는 4% 확률만 허용
        if "CORE" in buckets:
            if posture != "SELL":
                continue
            if rng.random() > 0.04:
                continue

        # (4) 정렬 키 구성 (v2와 동일)
        if buckets:
            pri = min(bucket_pri.get(b, 50) for b in buckets)
        else:
            pri = bucket_pri.get("FILLER_CHEAP", 4)

        surplus = float(getattr(c, "surplus_score", 0.0) or 0.0)
        exp = 1.0 if bool(getattr(c, "is_expiring", False)) else 0.0
        value = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)

        sale_cand = SellAssetCandidate(
            player_id=str(pid),
            market_total=float(value),
            salary_m=float(getattr(c, "salary_m", 0.0) or 0.0),
            remaining_years=float(getattr(c, "remaining_years", 0.0) or 0.0),
            is_expiring=bool(getattr(c, "is_expiring", False)),
            top_tags=tuple(getattr(c, "top_tags", None) or ()),
        )

        sort_key = (pri, -surplus, -exp, value, str(pid))
        rows.append((sort_key, sale_cand))

    if not rows:
        return []

    rows.sort(key=lambda x: x[0])
    sale: List[SellAssetCandidate] = [cand for _, cand in rows]

    # v2 스타일: head만 셔플(과도한 순위 붕괴 방지)
    head_n = max(6, min(len(sale), max_targets))
    head = sale[:head_n]
    rng.shuffle(head)
    sale = head + sale[head_n:]

    return sale[:max_targets]


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


def _parse_iso_ymd(value: object) -> Optional[date]:
    """YYYY-MM-DD (or datetime ISO) -> date. 실패 시 None."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _is_ban_active(current_date: date, until_iso: Optional[str]) -> bool:
    """until_iso(YYYY-MM-DD)가 현재 날짜 기준으로 아직 남아있으면 True."""
    d = _parse_iso_ymd(until_iso)
    return bool(d is not None and current_date < d)


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
