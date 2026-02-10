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

from .types import DealGeneratorConfig, DealGeneratorBudget, DealGeneratorStats, DealProposal, RuleFailureKind, parse_trade_error
from .utils import _clone_deal, _count_swaps, _count_picks, _count_seconds, _current_pick_ids, _pick_pick_id_matching_value, _team_pick_flow
from .scoring import evaluate_and_score, _should_discard_prop

# =============================================================================
# Sweetener loop
# =============================================================================


def maybe_apply_sweeteners(
    base: DealProposal,
    *,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    allow_locked_by_deal_id: Optional[str],
    banned_asset_keys: Set[str],
    rng: random.Random,
    stats: DealGeneratorStats,
) -> Tuple[DealProposal, int, int]:
    """"조금 부족"한 쪽이 있을 때 pick/swap sweetener를 1~2개만 추가해서 재시도.

    - base.deal은 이미 validate 통과 상태여야 한다.
    - 추가한 딜도 validate + evaluate를 통과해야 한다.

    Returns: (best_prop, extra_validations, extra_evaluations)
    """

    if not config.sweetener_enabled:
        return base, 0, 0

    # 예산 가드
    if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
        return base, 0, 0

    mb = float(base.buyer_eval.net_surplus) - float(base.buyer_decision.required_surplus)
    ms = float(base.seller_eval.net_surplus) - float(base.seller_decision.required_surplus)

    # 어느 쪽이 부족한가?
    giver: Optional[str] = None
    receiver: Optional[str] = None
    deficit = 0.0

    if base.seller_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and ms < 0.0 and abs(ms) <= float(config.sweetener_max_deficit):
        giver, receiver, deficit = base.buyer_id, base.seller_id, abs(ms)
    elif base.buyer_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and mb < 0.0 and abs(mb) <= float(config.sweetener_max_deficit):
        giver, receiver, deficit = base.seller_id, base.buyer_id, abs(mb)
    else:
        return base, 0, 0

    if not giver or not receiver:
        return base, 0, 0

    out_cat = catalog.outgoing_by_team.get(str(giver).upper())
    if out_cat is None:
        return base, 0, 0

    local_sweetener_bans: Set[str] = set()

    best = base
    extra_v = 0
    extra_e = 0

    deal = _clone_deal(base.deal)
    added_count = 0

    # score/margin 기반 early stop
    last_best_score = float(best.score)

    # deterministic shuffle inside same bucket selection
    bucket_order = list(config.sweetener_try_buckets)

    for bucket in bucket_order:
        if added_count >= int(config.sweetener_max_additions):
            break
        if stats.validations + extra_v >= budget.max_validations or stats.evaluations + extra_e >= budget.max_evaluations:
            break

        added = _try_add_one_sweetener(
            deal,
            giver_team=str(giver).upper(),
            receiver_team=str(receiver).upper(),
            out_cat=out_cat,
            catalog=catalog,
            config=config,
            target_value=float(deficit),
            bucket=str(bucket),
            banned_asset_keys=(set(banned_asset_keys) | set(local_sweetener_bans)),
            rng=rng,
        )
        if not added:
            continue

        added_count += 1
        stats.sweeteners_added += 1
        stats.sweetener_attempts += 1

        # validate
        attempted_key: Optional[str] = None
        if deal.legs.get(str(giver).upper()):
            attempted_key = asset_key(deal.legs[str(giver).upper()][-1])

        try:
            tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
            extra_v += 1
        except TradeError as err:
            # rollback this sweetener (remove last asset from giver leg)
            extra_v += 1
            if attempted_key:
                failure = parse_trade_error(err)
                # 자산 자체가 근본적으로 금지/미소유/잠김/중복인 케이스는 global ban이 효과적
                if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
                    banned_asset_keys.add(attempted_key)
                    if failure.asset_key:
                        banned_asset_keys.add(failure.asset_key)
                else:
                    local_sweetener_bans.add(attempted_key)
                    if failure.asset_key:
                        local_sweetener_bans.add(failure.asset_key)
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue
        except Exception:
            # 예상치 못한 예외는 local ban(동일 sweetener 재시도 방지)만 적용
            extra_v += 1
            if attempted_key:
                local_sweetener_bans.add(attempted_key)
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue

        # evaluate
        # (D) sweetener는 "누가 누구에게" 추가했는지와 round를 남겨두면
        # 후속 counter/협상 로직 구현 시 매우 유용하다.
        round_no = int(added_count)
        prop, used = evaluate_and_score(
            deal,
            buyer_id=base.buyer_id,
            seller_id=base.seller_id,
            tick_ctx=tick_ctx,
            config=config,
            tags=tuple(best.tags)
            + (
                f"sweetener:{bucket}",
                f"sweetener_from:{str(giver).upper()}",
                f"sweetener_to:{str(receiver).upper()}",
                f"sweetener_round:{round_no}",
            ),
            opponent_repeat_count=0,
            stats=stats,
        )
        extra_e += used
        if prop is None:
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue

        if _should_discard_prop(prop, config):
            _rollback_last_asset_from_leg(deal, str(giver).upper())
            continue

        # 개선이 거의 없으면 중단(낭비 방지)
        if float(prop.score) < last_best_score + float(config.sweetener_min_improvement):
            # 계속 추가해도 의미 없을 확률이 높아 중단
            best = max(best, prop, key=lambda p: p.score)
            break

        if float(prop.score) > float(best.score):
            best = prop
            last_best_score = float(best.score)

        # 둘 다 accept면 즉시 종료
        if best.buyer_decision.verdict == DealVerdict.ACCEPT and best.seller_decision.verdict == DealVerdict.ACCEPT:
            break

    return best, extra_v, extra_e


def _try_add_one_sweetener(
    deal: Deal,
    *,
    giver_team: str,
    receiver_team: str,
    out_cat: TeamOutgoingCatalog,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    target_value: float,
    bucket: str,
    banned_asset_keys: Set[str],
    rng: random.Random,
) -> bool:
    """deal에 sweetener 1개를 추가(성공 시 deal mutate).

    bucket:
      - SECOND / FIRST_SAFE / FIRST_SENSITIVE: PickAsset
      - SWAP: SwapAsset
    """

    giver_team = str(giver_team).upper()

    # max assets / picks guard
    if len(deal.legs.get(giver_team, [])) >= int(config.max_assets_per_side):
        return False

    if bucket == "SWAP":
        if _count_swaps(deal, giver_team) >= 1:
            return False
        cands = list(out_cat.swap_ids or ())
        rng.shuffle(cands)
        for sid in cands:
            s = out_cat.swaps.get(sid)
            if s is None:
                continue
            a = s.as_asset()
            if asset_key(a) in banned_asset_keys:
                continue
            if _asset_in_deal(deal, a):
                continue
            deal.legs[giver_team].append(a)
            return True
        return False

    # pick buckets
    if _count_picks(deal, giver_team) >= int(config.max_picks_per_side):
        return False

    pick_bucket: Optional[PickBucketId] = None
    if bucket == "SECOND":
        if _count_seconds(deal, giver_team, catalog=catalog) >= int(config.max_seconds_per_side):
            return False
        pick_bucket = "SECOND"
    elif bucket == "FIRST_SAFE":
        pick_bucket = "FIRST_SAFE"
    elif bucket == "FIRST_SENSITIVE":
        pick_bucket = "FIRST_SENSITIVE"
    else:
        return False

    excluded = _current_pick_ids(deal, giver_team)

    pid = _pick_pick_id_matching_value(out_cat, pick_bucket, excluded=excluded, target_value=target_value)
    if not pid:
        return False

    # Stepien check for 1st
    if pick_bucket in ("FIRST_SAFE", "FIRST_SENSITIVE"):
        out_ids, in_ids = _team_pick_flow(deal, giver_team)
        out_ids = set(out_ids | {pid})
        if not catalog.stepien.is_compliant_after(team_id=giver_team, outgoing_pick_ids=out_ids, incoming_pick_ids=set(in_ids)):
            return False

    a = out_cat.picks[pid].as_asset()
    if asset_key(a) in banned_asset_keys:
        return False
    if _asset_in_deal(deal, a):
        return False

    deal.legs[giver_team].append(a)
    return True


def _rollback_last_asset_from_leg(deal: Deal, team_id: str) -> None:
    tid = str(team_id).upper()
    leg = deal.legs.get(tid)
    if not leg:
        return
    try:
        leg.pop()
    except Exception:
        return


def _asset_in_deal(deal: Deal, asset: Asset) -> bool:
    k = asset_key(asset)
    for assets in deal.legs.values():
        for a in assets:
            if asset_key(a) == k:
                return True
    return False


