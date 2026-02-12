from __future__ import annotations

import random
from typing import Dict, List, Optional, Set, Tuple

from ...models import Deal, PlayerAsset

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import TradeAssetCatalog, TeamOutgoingCatalog, BucketId

from .types import DealGeneratorConfig, DealGeneratorBudget, TargetCandidate, DealCandidate, SellAssetCandidate
from .utils import (
    _add_pick_package,
    _best_need_tag,
    _can_absorb_without_outgoing,
    _clone_deal,
    _get_need_map,
    _pick_bucket_player_for_need,
    _pick_from_id_pool_for_need,
    _pick_return_player_salaryish_with_need,
    _split_young_candidates,
    _shape_ok,
)
from .targets import _is_seller_willing_to_move_player


# =============================================================================
# Offer skeletons
# =============================================================================


def _with_core_tags(tags: List[str], *, mode: str, focal_player_id: str, archetype: str) -> List[str]:
    """(D) 후속 디버깅/분석을 위해 일관된 핵심 태그를 보장한다."""

    out = list(tags)
    for t in (f"mode:{str(mode).upper()}", f"focal:{focal_player_id}", f"arch:{archetype}"):
        if t not in out:
            out.append(t)
    return out


def build_offer_skeletons_buy(
    buyer_id: str,
    seller_id: str,
    target: TargetCandidate,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    *,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
) -> List[DealCandidate]:
    """BUY 모드: target 1명 기준 2~4개 archetype 스켈레톤."""

    buyer_out = catalog.outgoing_by_team.get(str(buyer_id).upper())
    seller_out = catalog.outgoing_by_team.get(str(seller_id).upper())
    if buyer_out is None or seller_out is None:
        return []

    # seller가 해당 선수를 "매물"로 갖고 있는지(= outgoing pool에 포함) 확인
    if not _is_seller_willing_to_move_player(target.player_id, seller_out):
        return []

    # return-ban precheck
    cand_target = seller_out.players.get(target.player_id)
    if cand_target is not None:
        if str(buyer_id).upper() in set(cand_target.return_ban_teams or ()):
            return []
    # learned receiver-specific ban (from repair stage)
    buyer_u = str(buyer_id).upper()
    if banned_receivers_by_player is not None:
        if buyer_u in banned_receivers_by_player.get(str(target.player_id), set()):
            return []

    ts_buyer = tick_ctx.get_team_situation(buyer_id)
    ts_seller = tick_ctx.get_team_situation(seller_id)
    seller_need_map = _get_need_map(tick_ctx, seller_id)

    # soft 2nd apron guard는 _soft_guard_second_apron_candidates(=payroll_after_est 기반)에서 처리

    # base deal: seller sends target
    base = Deal(
        teams=[str(buyer_id).upper(), str(seller_id).upper()],
        legs={
            str(buyer_id).upper(): [],
            str(seller_id).upper(): [PlayerAsset(kind="player", player_id=target.player_id)],
        },
    )

    out: List[DealCandidate] = []

    # archetype 1) picks-only
    # cap space로 흡수 가능한 경우에만 생성(그 외는 salary_matching repair로 억지 변형되는 비율이 높아 현실감/비용에 악영향)
    if _can_absorb_without_outgoing(ts_buyer, float(target.salary_m)):
        deal1 = _clone_deal(base)
        # seller rebuild이면 pick을 조금 더
        max_picks = 2 if str(getattr(ts_seller, "time_horizon", "RE_TOOL") or "RE_TOOL") == "REBUILD" else 1
        _add_pick_package(
            deal1,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND", "FIRST_SAFE"),
            max_picks=max_picks,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal1,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="picks_only",
                tags=_with_core_tags([f"need:{target.need_tag}", "pkg:picks"], mode="BUY", focal_player_id=target.player_id, archetype="picks_only"),
            )
        )

    # archetype 2) young + pick (one outgoing player)
    seller_horizon = str(getattr(ts_seller, "time_horizon", "RE_TOOL") or "RE_TOOL")
    rebuildish = seller_horizon in {"REBUILD", "RE_TOOL"}

    prospect_ids, throwin_ids = _split_young_candidates(
        buyer_out,
        config=config,
        receiver_team_id=seller_id,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
    )

    young_id: Optional[str] = None
    source_tag: Optional[str] = None

    if rebuildish:
        pool = prospect_ids if prospect_ids else throwin_ids
        if pool:
            # variety: shuffle among top candidates (deterministic rng)
            max_prospect = int(getattr(config, "young_prospect_max_candidates", 6) or 6)
            top_n = max(2, min(max_prospect, len(pool)))
            bucket = list(pool[:top_n])
            rng.shuffle(bucket)
            young_id = bucket[0]
            source_tag = "young_source:prospect" if prospect_ids else "young_source:throwin"
    else:
        # non-rebuild sellers treat "young" as cheap throw-in only (no prospect fallback)
        pool = throwin_ids
        if pool:
            young_id = _pick_from_id_pool_for_need(
                buyer_out,
                pool_ids=pool,
                receiver_team_id=seller_id,
                target_salary_m=float(target.salary_m),
                need_map=seller_need_map,
                rng=rng,
                banned_players=banned_players,
                banned_receivers_by_player=banned_receivers_by_player,
                must_be_aggregation_friendly=True,
                top_scan=6,
            )
            source_tag = "young_source:throwin"

    if young_id:
        deal2 = _clone_deal(base)
        deal2.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=young_id))
        _add_pick_package(
            deal2,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        tags = [f"need:{target.need_tag}", "pkg:young+pick"]
        if source_tag:
            tags.append(source_tag)
        c_ret = buyer_out.players.get(str(young_id))
        if c_ret is not None:
            rt = _best_need_tag(seller_need_map, c_ret)
            if rt:
                tags.append(f"return_need:{rt}")
        out.append(
            DealCandidate(
                deal=deal2,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="young_plus_pick",
                tags=_with_core_tags(tags, mode="BUY", focal_player_id=target.player_id, archetype="young_plus_pick"),
            )
        )

    # archetype 3) player-for-player (salary-ish)
    filler_id = _pick_return_player_salaryish_with_need(
        buyer_out,
        receiver_team_id=seller_id,
        target_salary_m=float(target.salary_m),
        need_map=seller_need_map,
        rng=rng,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        # aggregation_solo_only는 '묶음 금지'이므로 1-for-1(p4p) 스켈레톤에서는 허용
        must_be_aggregation_friendly=False,
    )
    if filler_id:
        deal3 = _clone_deal(base)
        deal3.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=filler_id))

        tags = [f"need:{target.need_tag}", "pkg:player_for_player"]
        c_ret = buyer_out.players.get(str(filler_id))
        if c_ret is not None:
            rt = _best_need_tag(seller_need_map, c_ret)
            if rt:
                tags.append(f"return_need:{rt}")
        
        out.append(
            DealCandidate(
                deal=deal3,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="p4p_salary",
                tags=_with_core_tags(tags, mode="BUY", focal_player_id=target.player_id, archetype="p4p_salary"),
            )
        )

    # archetype 4) consolidate 2-for-1
    cons_id = _pick_bucket_player_for_need(
        buyer_out,
        bucket="CONSOLIDATE",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
        need_map=seller_need_map,
    )
    cheap_id = _pick_bucket_player_for_need(
        buyer_out,
        bucket="FILLER_CHEAP",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
        need_map=seller_need_map,
    )
    if cons_id and cheap_id and cons_id != cheap_id:
        deal4 = _clone_deal(base)
        deal4.legs[str(buyer_id).upper()].extend(
            [
                PlayerAsset(kind="player", player_id=cons_id),
                PlayerAsset(kind="player", player_id=cheap_id),
            ]
        )
        _add_pick_package(
            deal4,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        tags = [f"need:{target.need_tag}", "pkg:consolidate"]
        rt_seen: Set[str] = set()
        for _pid in (cons_id, cheap_id):
            c_ret = buyer_out.players.get(str(_pid))
            if c_ret is None:
                continue
            rt = _best_need_tag(seller_need_map, c_ret)
            if rt and rt not in rt_seen:
                rt_seen.add(rt)
                tags.append(f"return_need:{rt}")
                
        out.append(
            DealCandidate(
                deal=deal4,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=target.player_id,
                archetype="consolidate_2_for_1",
                tags=_with_core_tags(tags, mode="BUY", focal_player_id=target.player_id, archetype="consolidate_2_for_1"),
            )
        )

    # shape cap
    trimmed: List[DealCandidate] = []
    for c in out:
        if _shape_ok(c.deal, config=config, catalog=catalog):
            trimmed.append(c)

    # beam cap
    return trimmed[: max(2, int(budget.beam_width))]


def build_offer_skeletons_sell(
    *,
    seller_id: str,
    buyer_id: str,
    sale_asset: SellAssetCandidate,
    match_tag: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
) -> List[DealCandidate]:
    """SELL 모드: (seller sends sale_asset.player_id) 기준 BUYER 패키지를 생성."""

    buyer_out = catalog.outgoing_by_team.get(str(buyer_id).upper())
    seller_out = catalog.outgoing_by_team.get(str(seller_id).upper())
    if buyer_out is None or seller_out is None:
        return []

    pid = sale_asset.player_id
    if pid in banned_players:
        return []

    # return-ban precheck
    c_sale = seller_out.players.get(pid)
    if c_sale is not None:
        if str(buyer_id).upper() in set(c_sale.return_ban_teams or ()):
            return []
    # learned receiver-specific ban
    buyer_u = str(buyer_id).upper()
    if banned_receivers_by_player is not None:
        if buyer_u in banned_receivers_by_player.get(str(pid), set()):
            return []

    # base deal: seller sends player to buyer
    base = Deal(
        teams=[str(buyer_id).upper(), str(seller_id).upper()],
        legs={
            str(buyer_id).upper(): [],
            str(seller_id).upper(): [PlayerAsset(kind="player", player_id=pid)],
        },
    )

    ts_seller = tick_ctx.get_team_situation(seller_id)
    ts_buyer = tick_ctx.get_team_situation(buyer_id)
    time_horizon = str(getattr(ts_seller, "time_horizon", "RE_TOOL") or "RE_TOOL")
    seller_need_map = _get_need_map(tick_ctx, seller_id)

    # soft 2nd apron guard는 _soft_guard_second_apron_candidates(=payroll_after_est 기반)에서 처리

    out: List[DealCandidate] = []

    # archetype 1) buyer picks package to seller
    # buyer가 선수 없이 salary를 흡수할 cap space가 있을 때만 생성
    if _can_absorb_without_outgoing(ts_buyer, float(sale_asset.salary_m)):
        deal1 = _clone_deal(base)
        # rebuild seller는 picks 선호
        max_picks = 2 if time_horizon == "REBUILD" else 1
        _add_pick_package(
            deal1,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND", "FIRST_SAFE"),
            max_picks=max_picks,
            banned_asset_keys=banned_asset_keys,
        )
        out.append(
            DealCandidate(
                deal=deal1,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=pid,
                archetype="buyer_picks",
                tags=_with_core_tags([f"match:{match_tag}", "pkg:picks"], mode="SELL", focal_player_id=pid, archetype="buyer_picks"),
            )
        )

    # archetype 2) buyer young + pick
    rebuildish = time_horizon in {"REBUILD", "RE_TOOL"}

    prospect_ids, throwin_ids = _split_young_candidates(
        buyer_out,
        config=config,
        receiver_team_id=seller_id,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
    )

    young_id: Optional[str] = None
    source_tag: Optional[str] = None

    if rebuildish:
        pool = prospect_ids if prospect_ids else throwin_ids
        if pool:
            max_prospect = int(getattr(config, "young_prospect_max_candidates", 6) or 6)
            top_n = max(2, min(max_prospect, len(pool)))
            bucket = list(pool[:top_n])
            rng.shuffle(bucket)
            young_id = bucket[0]
            source_tag = "young_source:prospect" if prospect_ids else "young_source:throwin"
    else:
        pool = throwin_ids
        if pool:
            young_id = _pick_from_id_pool_for_need(
                buyer_out,
                pool_ids=pool,
                receiver_team_id=seller_id,
                target_salary_m=float(sale_asset.salary_m),
                need_map=seller_need_map,
                rng=rng,
                banned_players=banned_players,
                banned_receivers_by_player=banned_receivers_by_player,
                must_be_aggregation_friendly=True,
                top_scan=6,
            )
            source_tag = "young_source:throwin"

    if young_id:
        deal2 = _clone_deal(base)
        deal2.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=young_id))
        _add_pick_package(
            deal2,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        tags = [f"match:{match_tag}", "pkg:young+pick"]
        if source_tag:
            tags.append(source_tag)
        c_ret = buyer_out.players.get(str(young_id))
        if c_ret is not None:
            rt = _best_need_tag(seller_need_map, c_ret)
            if rt:
                tags.append(f"return_need:{rt}")
        out.append(
            DealCandidate(
                deal=deal2,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=pid,
                archetype="buyer_young_plus_pick",
                tags=_with_core_tags(tags, mode="SELL", focal_player_id=pid, archetype="buyer_young_plus_pick"),
            )
        )

    # archetype 3) buyer sends salary-ish player back (WIN_NOW seller라면 우선)
    if time_horizon in {"WIN_NOW", "RE_TOOL"}:
        filler_id = _pick_return_player_salaryish_with_need(
            buyer_out,
            receiver_team_id=seller_id,
            target_salary_m=float(sale_asset.salary_m),
            need_map=seller_need_map,
            rng=rng,
            banned_players=banned_players,
            banned_receivers_by_player=banned_receivers_by_player,
            # aggregation_solo_only는 '묶음 금지'이므로 1-for-1(p4p) 스켈레톤에서는 허용
            must_be_aggregation_friendly=False,
        )
        if filler_id:
            deal3 = _clone_deal(base)
            deal3.legs[str(buyer_id).upper()].append(PlayerAsset(kind="player", player_id=filler_id))

            tags = [f"match:{match_tag}", "pkg:player_for_player"]
            c_ret = buyer_out.players.get(str(filler_id))
            if c_ret is not None:
                rt = _best_need_tag(seller_need_map, c_ret)
                if rt:
                    tags.append(f"return_need:{rt}")
            
            out.append(
                DealCandidate(
                    deal=deal3,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    focal_player_id=pid,
                    archetype="buyer_p4p",
                    tags=_with_core_tags(tags, mode="SELL", focal_player_id=pid, archetype="buyer_p4p"),
                )
            )

    # archetype 4) consolidate (buyer 2-for-1)
    cons_id = _pick_bucket_player_for_need(
        buyer_out,
        bucket="CONSOLIDATE",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
        need_map=seller_need_map,
    )
    cheap_id = _pick_bucket_player_for_need(
        buyer_out,
        bucket="FILLER_CHEAP",
        receiver_team_id=seller_id,
        banned_players=banned_players,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
        need_map=seller_need_map,
    )
    if cons_id and cheap_id and cons_id != cheap_id:
        deal4 = _clone_deal(base)
        deal4.legs[str(buyer_id).upper()].extend(
            [PlayerAsset(kind="player", player_id=cons_id), PlayerAsset(kind="player", player_id=cheap_id)]
        )
        _add_pick_package(
            deal4,
            from_team=buyer_id,
            out_cat=buyer_out,
            catalog=catalog,
            config=config,
            rng=rng,
            prefer=("SECOND",),
            max_picks=1,
            banned_asset_keys=banned_asset_keys,
        )
        tags = [f"match:{match_tag}", "pkg:consolidate"]
        rt_seen: Set[str] = set()
        for _pid in (cons_id, cheap_id):
            c_ret = buyer_out.players.get(str(_pid))
            if c_ret is None:
                continue
            rt = _best_need_tag(seller_need_map, c_ret)
            if rt and rt not in rt_seen:
                rt_seen.add(rt)
                tags.append(f"return_need:{rt}")
        out.append(
            DealCandidate(
                deal=deal4,
                buyer_id=buyer_id,
                seller_id=seller_id,
                focal_player_id=pid,
                archetype="buyer_consolidate",
                tags=_with_core_tags(tags, mode="SELL", focal_player_id=pid, archetype="buyer_consolidate"),
            )
        )

    trimmed: List[DealCandidate] = []
    for c in out:
        if _shape_ok(c.deal, config=config, catalog=catalog):
            trimmed.append(c)

    return trimmed[: max(2, int(budget.beam_width))]




# =============================================================================
# Candidate variant expansion (light beam)
# =============================================================================


def expand_variants(
    buyer_id: str,
    seller_id: str,
    target: TargetCandidate,
    base_candidates: List[DealCandidate],
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    *,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
) -> List[DealCandidate]:
    """스켈레톤을 '얕게' 확장한다.

    목표
    - 타깃당 6~12개 수준에서만 변형을 만들어 탐색을 깊게(하지만 폭발은 방지).
    - 변형은 **동일 archetype 내에서** player/pick을 약간 교체하는 수준만 수행.

    주의
    - validate/evaluate 비용이 크므로, 여기서는 **항상 정적(cap) 상한**을 둔다.
    - 중복 제거는 상위 루프(dedupe_hash)에서 처리한다.
    """

    goal = min(12, max(6, int(budget.beam_width)))
    if not base_candidates:
        return []

    buyer = str(buyer_id).upper()
    seller = str(seller_id).upper()

    buyer_out = catalog.outgoing_by_team.get(buyer)
    if buyer_out is None:
        return list(base_candidates)

    out: List[DealCandidate] = list(base_candidates)

    # 내부 hard cap: goal의 2배를 넘기지 않음(폭발 방지)
    hard_cap = max(goal, min(24, goal * 2))

    def _push(deal: Deal, archetype: str, tags: List[str]) -> None:
        if len(out) >= hard_cap:
            return
        if not _shape_ok(deal, config=config, catalog=catalog):
            return
        out.append(
            DealCandidate(
                deal=deal,
                buyer_id=buyer,
                seller_id=seller,
                focal_player_id=target.player_id,
                archetype=archetype,
                tags=_with_core_tags(tags, mode="BUY", focal_player_id=target.player_id, archetype=archetype),
            )
        )

    def _base_deal() -> Deal:
        return Deal(
            teams=[buyer, seller],
            legs={
                buyer: [],
                seller: [PlayerAsset(kind="player", player_id=target.player_id)],
            },
        )

    # --- archetype: picks-only variants
    # cap space 흡수 가능할 때만
    try:
        ts_buyer = tick_ctx.get_team_situation(buyer)
    except Exception:
        ts_buyer = None
    if ts_buyer is not None and _can_absorb_without_outgoing(ts_buyer, float(target.salary_m)):
        # (SECOND) / (SECOND+SECOND) / (FIRST_SAFE) / (FIRST_SAFE+SECOND)
        pick_plans: List[Tuple[Tuple[str, ...], int]] = [
            (("SECOND",), 1),
            (("SECOND", "SECOND"), 2),
            (("FIRST_SAFE",), 1),
            (("FIRST_SAFE", "SECOND"), 2),
        ]
        for prefer, max_picks in pick_plans:
            d = _base_deal()
            _add_pick_package(
                d,
                from_team=buyer,
                out_cat=buyer_out,
                catalog=catalog,
                rng=rng,
                prefer=prefer,
                max_picks=max_picks,
                config=config,
                banned_asset_keys=banned_asset_keys,
            )
            _push(d, "picks_only", [f"need:{target.need_tag}", "pkg:picks", "var:picks"])

    # --- archetype: young + pick variants (prospect vs throw-in split)
    try:
        ts_seller = tick_ctx.get_team_situation(seller)
    except Exception:
        ts_seller = None
    seller_horizon = str(getattr(ts_seller, "time_horizon", "RE_TOOL") or "RE_TOOL") if ts_seller is not None else "RE_TOOL"
    rebuildish = seller_horizon in {"REBUILD", "RE_TOOL"}

    prospect_ids, throwin_ids = _split_young_candidates(
        buyer_out,
        config=config,
        banned_players=banned_players,
        receiver_team_id=seller,
        banned_receivers_by_player=banned_receivers_by_player,
        must_be_aggregation_friendly=True,
    )

    source_tag: Optional[str] = None
    if rebuildish:
        pool = prospect_ids if prospect_ids else throwin_ids
        if pool:
            source_tag = "young_source:prospect" if prospect_ids else "young_source:throwin"
            k = 2
            max_prospect = int(getattr(config, "young_prospect_max_candidates", 6) or 6)
            top_n = max(2, min(max_prospect, len(pool)))
            bucket = list(pool[:top_n])
            rng.shuffle(bucket)
            young_ids = bucket[:k]
        else:
            young_ids = []
    else:
        pool = throwin_ids
        if pool:
            source_tag = "young_source:throwin"
            k = 1
            bucket = list(pool[: max(1, min(8, len(pool)))])
            rng.shuffle(bucket)
            young_ids = bucket[:k]
        else:
            young_ids = []

    for pid in young_ids:
        for prefer, max_picks in [(("SECOND",), 1), (("SECOND", "SECOND"), 2)]:
            d = _base_deal()
            d.legs[buyer].append(PlayerAsset(kind="player", player_id=pid))
            _add_pick_package(
                d,
                from_team=buyer,
                out_cat=buyer_out,
                catalog=catalog,
                rng=rng,
                prefer=prefer,
                max_picks=max_picks,
                config=config,
                banned_asset_keys=banned_asset_keys,
            )
            tags = [f"need:{target.need_tag}", "pkg:young+pick", "var:young"]
            if source_tag:
                tags.append(source_tag)
            _push(d, "young_plus_pick", tags)

    # --- archetype: p4p salary variants (top 3 fillers by salary gap)
    filler_ids = _top_k_fillers_by_salary_gap(
        buyer_out,
        target_salary_m=float(target.salary_m),
        k=3,
        banned_players=banned_players,
        receiver_team_id=seller,
        banned_receivers_by_player=banned_receivers_by_player,
        # aggregation_solo_only는 '묶음 금지'이므로 1-for-1(p4p) 변형에서는 허용
        must_be_aggregation_friendly=False,
    )
    for pid in filler_ids:
        d = _base_deal()
        d.legs[buyer].append(PlayerAsset(kind="player", player_id=pid))
        _push(d, "p4p_salary", [f"need:{target.need_tag}", "pkg:player_for_player", "var:salary"])

    # --- archetype: consolidate variants (top 2 consolidate + cheap fillers 2)
    cons_ids = _top_k_bucket_players_by_market(
        buyer_out,
        bucket="CONSOLIDATE",
        k=2,
        banned_players=banned_players,
        descending=True,
        receiver_team_id=seller,
        banned_receivers_by_player=banned_receivers_by_player,
    )
    cheap_ids = _top_k_bucket_players_by_market(
        buyer_out,
        bucket="FILLER_CHEAP",
        k=2,
        banned_players=banned_players,
        descending=False,
        receiver_team_id=seller,
    )
    for cid in cons_ids:
        for fid in cheap_ids:
            if cid == fid:
                continue
            d = _base_deal()
            d.legs[buyer].extend(
                [
                    PlayerAsset(kind="player", player_id=cid),
                    PlayerAsset(kind="player", player_id=fid),
                ]
            )
            _add_pick_package(
                d,
                from_team=buyer,
                out_cat=buyer_out,
                catalog=catalog,
                rng=rng,
                prefer=("SECOND",),
                max_picks=1,
                config=config,
                banned_asset_keys=banned_asset_keys,
            )
            _push(
                d,
                "consolidate_2_for_1",
                [f"need:{target.need_tag}", "pkg:consolidate", "var:consolidate"],
            )

    # 마지막으로 goal 수준에서만 남기기(상위에서 shuffle 후 slice하지만,
    # 여기서도 폭발 방지 차원에서 한 번 더 컷)
    if len(out) > hard_cap:
        out = out[:hard_cap]
    return out


def _top_k_fillers_by_salary_gap(
    out: TeamOutgoingCatalog,
    *,
    target_salary_m: float,
    k: int,
    banned_players: Set[str],
    receiver_team_id: Optional[str] = None,
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
    must_be_aggregation_friendly: bool = True,
) -> List[str]:
    """target salary 근처 filler 후보 top-k.

    BUY 모드 variant 생성에서 invalid 낭비를 줄이기 위해,
    - receiver_team_id가 주어지면 return_ban_teams 사전 필터를 적용한다.
    - must_be_aggregation_friendly=True면 aggregation_solo_only 후보는 제외한다.
    """
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    ids: List[str] = []
    for b in ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT"):
        ids.extend(list(out.player_ids_by_bucket.get(b, tuple())))

    scored: List[Tuple[float, float, str]] = []
    for pid in ids:
        if pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue

        if receiver and receiver in (c.return_ban_teams or ()):
            continue
        if receiver and banned_receivers_by_player is not None:
            if receiver in banned_receivers_by_player.get(str(pid), set()):
                continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue

        gap = abs(float(c.salary_m) - float(target_salary_m))
        scored.append((gap, float(c.market.total), pid))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [pid for _, _, pid in scored[: int(k)]]


def _top_k_bucket_players_by_market(
    out: TeamOutgoingCatalog,
    *,
    bucket: BucketId,
    k: int,
    banned_players: Set[str],
    descending: bool,
    receiver_team_id: Optional[str] = None,
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
    must_be_aggregation_friendly: bool = True,
) -> List[str]:
    """특정 버킷에서 market.total 기준 top-k.

    BUY 모드 variant 생성에서 invalid 낭비를 줄이기 위해,
    - receiver_team_id가 주어지면 return_ban_teams 사전 필터를 적용한다.
    - must_be_aggregation_friendly=True면 aggregation_solo_only 후보는 제외한다.
    """
    receiver = str(receiver_team_id).upper() if receiver_team_id else None

    scored: List[Tuple[float, str]] = []
    for pid in out.player_ids_by_bucket.get(bucket, tuple()):
        if pid in banned_players:
            continue
        c = out.players.get(pid)
        if c is None:
            continue

        if receiver and receiver in (c.return_ban_teams or ()):
            continue
        if receiver and banned_receivers_by_player is not None:
            if receiver in banned_receivers_by_player.get(str(pid), set()):
                continue
        if must_be_aggregation_friendly and bool(getattr(c, "aggregation_solo_only", False)):
            continue

        scored.append((float(c.market.total), pid))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=bool(descending))
    return [pid for _, pid in scored[: int(k)]]


# =============================================================================
# Validate + Repair
# =============================================================================
