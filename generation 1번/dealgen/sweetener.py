from __future__ import annotations

import random
from typing import List, Optional, Set, Tuple

from ...errors import TradeError
from ...models import Deal, PickAsset, SwapAsset, Asset, asset_key
from ...valuation.types import DealVerdict, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import TradeAssetCatalog, TeamOutgoingCatalog, PickBucketId

from .types import DealGeneratorConfig, DealGeneratorBudget, DealGeneratorStats, DealProposal, RuleFailureKind, parse_trade_error
from .utils import _clone_deal, _count_swaps, _count_picks, _count_seconds, _team_pick_flow, _is_locked_candidate
from .dedupe import deal_signature_payload
from .scoring import evaluate_and_score, _should_discard_prop

# =============================================================================
# Sweetener loop
# =============================================================================


def _sweetener_close_corridor(team_eval: TeamDealEvaluation, cfg: DealGeneratorConfig) -> float:
    """Sweetener attempt window scaled by deal size (v2 absorption).

    v2 공식:
      scale = max(team_eval.outgoing_total, 6.0)
      close = min(cap, max(floor, ratio * scale))
      eligible if margin >= -close

    v1 호환:
      - 기존 sweetener_max_deficit는 '최종 상한'으로 존중한다.
      - 새 노브가 없거나 0이면 legacy(=sweetener_max_deficit)로 동작.
    """
    base_max = float(getattr(cfg, "sweetener_max_deficit", 0.0) or 0.0)

    ratio = float(getattr(cfg, "sweetener_close_corridor_ratio", 0.0) or 0.0)
    floor = float(getattr(cfg, "sweetener_close_floor", 0.0) or 0.0)
    cap = float(getattr(cfg, "sweetener_close_cap", 0.0) or 0.0)

    # If scaling knobs are disabled/missing, fallback to legacy behavior.
    if ratio <= 0.0 and cap <= 0.0 and floor <= 0.0:
        return base_max if base_max > 0.0 else 0.0

    scale = float(getattr(team_eval, "outgoing_total", 0.0) or 0.0)
    scale = max(scale, 6.0)

    close = max(floor, ratio * scale)
    if cap > 0.0:
        close = min(cap, close)

    # Keep legacy hard cap as a safety bound (if set).
    if base_max > 0.0:
        close = min(base_max, close)

    return float(close)


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

    V2-style improvements:
    - Transactional: trial deal only; commit only on success.
    - Limit is on *committed* sweeteners (not attempts).
    - Token별 best-of-N 후보를 비교 후 최선만 commit.
    - 남은 예산(validations/evals)에 따라 후보 폭을 자동 축소.
    - pick_rules repair로 sweetener가 '조용히 빠지는' 성공을 방지(여기서는 repair 미사용).

    Returns: (best_prop, extra_validations, extra_evaluations)
    """

    if not config.sweetener_enabled:
        return base, 0, 0

    # 예산 가드
    if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
        return base, 0, 0

    def _margin_for(team_id: str, p: DealProposal) -> float:
        tid = str(team_id).upper()
        if tid == str(p.buyer_id).upper():
            return float(p.buyer_eval.net_surplus) - float(p.buyer_decision.required_surplus)
        if tid == str(p.seller_id).upper():
            return float(p.seller_eval.net_surplus) - float(p.seller_decision.required_surplus)
        return 0.0

    mb = _margin_for(base.buyer_id, base)
    ms = _margin_for(base.seller_id, base)

    # 어느 쪽이 부족한가?
    giver: Optional[str] = None
    receiver: Optional[str] = None
    deficit = 0.0

    # v2-style "close corridor": scale by the *receiver* side outgoing_total
    close_seller = _sweetener_close_corridor(base.seller_eval, config)
    close_buyer = _sweetener_close_corridor(base.buyer_eval, config)

    if base.seller_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and ms < 0.0 and abs(ms) <= float(close_seller):
        giver, receiver, deficit = base.buyer_id, base.seller_id, abs(ms)
    elif base.buyer_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and mb < 0.0 and abs(mb) <= float(close_buyer):
        giver, receiver, deficit = base.seller_id, base.buyer_id, abs(mb)
    else:
        return base, 0, 0

    if not giver or not receiver:
        return base, 0, 0

    giver_u = str(giver).upper()
    receiver_u = str(receiver).upper()

    out_cat = catalog.outgoing_by_team.get(giver_u)
    if out_cat is None:
        return base, 0, 0

    local_sweetener_bans: Set[str] = set()

    extra_v = 0
    extra_e = 0

    verdict_rank = {DealVerdict.REJECT: 0, DealVerdict.COUNTER: 1, DealVerdict.ACCEPT: 2}

    origin_deal = _clone_deal(base.deal)
    current_best = base
    current_deal = _clone_deal(base.deal)

    origin_pick_ids = _all_pick_ids_in_deal(origin_deal, giver_u)
    origin_swap_ids = _all_swap_ids_in_deal(origin_deal, giver_u)

    def _committed_count(d: Deal) -> int:
        cur_p = _all_pick_ids_in_deal(d, giver_u)
        cur_s = _all_swap_ids_in_deal(d, giver_u)
        return len(cur_p - origin_pick_ids) + len(cur_s - origin_swap_ids)

    committed = _committed_count(current_deal)

    max_add = max(0, int(config.sweetener_max_additions))
    if max_add <= 0:
        return base, 0, 0

    # deterministic shuffle inside same bucket selection
    bucket_order = list(config.sweetener_try_buckets)

    def _receiver_verdict(p: DealProposal) -> DealVerdict:
        return p.buyer_decision.verdict if receiver_u == str(p.buyer_id).upper() else p.seller_decision.verdict

    def _other_verdict(p: DealProposal) -> DealVerdict:
        return p.seller_decision.verdict if receiver_u == str(p.buyer_id).upper() else p.buyer_decision.verdict

    def _receiver_margin(p: DealProposal) -> float:
        return _margin_for(receiver_u, p)

    for bucket in bucket_order:
        if committed >= max_add:
            break

        # 남은 예산 기반 candidate 폭 조절
        rem_v = int(budget.max_validations - (stats.validations + extra_v))
        rem_e = int(budget.max_evaluations - (stats.evaluations + extra_e))
        if rem_v <= 0 or rem_e <= 0:
            break

        cand_limit = int(getattr(config, 'sweetener_candidate_width', 3) or 3)
        cand_limit = max(1, min(3, cand_limit))
        cand_limit = min(cand_limit, rem_v)      # ~1 validation per candidate
        cand_limit = min(cand_limit, rem_e // 2) # ~2 evals per candidate
        if cand_limit <= 0:
            break

        # 아직 많이 부족하면 sweetener로 복구 확률이 낮아 낭비 방지
        if _receiver_margin(current_best) < -1.0:
            cand_limit = min(cand_limit, 1)

        # origin 기반도 항상 포함(이전 commit이 이후 token의 단일 경로를 막지 않게)
        base_deals: List[Deal] = [origin_deal]
        if cand_limit >= 2 and deal_signature_payload(current_deal) != deal_signature_payload(origin_deal):
            base_deals.append(current_deal)

        # split candidate width across base_deals
        if len(base_deals) == 1:
            base_limits = [cand_limit]
        else:
            per = max(1, cand_limit // len(base_deals))
            base_limits = [cand_limit - per * (len(base_deals) - 1)] + [per] * (len(base_deals) - 1)

        best_prop: Optional[DealProposal] = None
        best_key: Optional[Tuple[int, int, int, float, float]] = None
        best_deal: Optional[Deal] = None

        for base_deal, base_limit in zip(base_deals, base_limits):
            if base_limit <= 0:
                continue

            candidates = _collect_sweetener_candidates(
                base_deal,
                giver_team=giver_u,
                receiver_team=receiver_u,
                out_cat=out_cat,
                catalog=catalog,
                config=config,
                target_value=float(deficit),
                bucket=str(bucket),
                banned_asset_keys=(set(banned_asset_keys) | set(local_sweetener_bans)),
                rng=rng,
                limit=int(base_limit),
                allow_locked_by_deal_id=allow_locked_by_deal_id,
            )
            if not candidates:
                continue

            for cand_asset in candidates:
                if stats.validations + extra_v >= budget.max_validations or stats.evaluations + extra_e >= budget.max_evaluations:
                    break

                attempted_key = asset_key(cand_asset)

                # Trial (transactional): clone and append
                deal2 = _clone_deal(base_deal)
                deal2.legs.setdefault(giver_u, [])
                deal2.legs[giver_u].append(cand_asset)

                stats.sweetener_attempts += 1
                if hasattr(stats, 'sweetener_trials'):
                    stats.sweetener_trials += 1

                # validate (no repair)
                try:
                    tick_ctx.validate_deal(deal2, allow_locked_by_deal_id=allow_locked_by_deal_id)
                    extra_v += 1
                except TradeError as err:
                    extra_v += 1
                    failure = parse_trade_error(err)
                    stats.bump_failure(str(failure.kind.value))

                    # 근본적으로 금지/미소유/잠김/중복인 케이스는 global ban이 효과적
                    if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
                        banned_asset_keys.add(attempted_key)
                        if failure.asset_key:
                            banned_asset_keys.add(failure.asset_key)
                    # intrinsic horizon: hard-ban the pick candidate
                    elif failure.kind == RuleFailureKind.PICK_RULES and str(failure.reason or '') == 'pick_too_far':
                        banned_asset_keys.add(attempted_key)
                        if failure.asset_key:
                            banned_asset_keys.add(failure.asset_key)
                    else:
                        local_sweetener_bans.add(attempted_key)
                        if failure.asset_key:
                            local_sweetener_bans.add(failure.asset_key)

                    if hasattr(stats, 'sweetener_rollbacks'):
                        stats.sweetener_rollbacks += 1
                    continue
                except Exception:
                    extra_v += 1
                    stats.bump_failure('unexpected_exception_validate')
                    local_sweetener_bans.add(attempted_key)
                    if hasattr(stats, 'sweetener_rollbacks'):
                        stats.sweetener_rollbacks += 1
                    continue

                # evaluate
                trial_round = _committed_count(deal2)
                prop, used = evaluate_and_score(
                    deal2,
                    buyer_id=base.buyer_id,
                    seller_id=base.seller_id,
                    tick_ctx=tick_ctx,
                    config=config,
                    tags=tuple(current_best.tags)
                    + (
                        f'sweetener:{bucket}',
                        f'sweetener_from:{giver_u}',
                        f'sweetener_to:{receiver_u}',
                        f'sweetener_round:{int(trial_round)}',
                    ),
                    opponent_repeat_count=0,
                    stats=stats,
                )
                extra_e += used

                if prop is None or _should_discard_prop(prop, config):
                    if hasattr(stats, 'sweetener_rollbacks'):
                        stats.sweetener_rollbacks += 1
                    continue

                seller_v = prop.seller_decision.verdict
                buyer_v = prop.buyer_decision.verdict
                both_accept = 1 if (seller_v == DealVerdict.ACCEPT and buyer_v == DealVerdict.ACCEPT) else 0
                key = (
                    both_accept,
                    verdict_rank.get(_receiver_verdict(prop), 0),
                    verdict_rank.get(_other_verdict(prop), 0),
                    float(_receiver_margin(prop)),
                    float(prop.score),
                )
                if best_key is None or key > best_key:
                    best_key = key
                    best_prop = prop
                    best_deal = deal2

                if both_accept:
                    break

        if best_prop is None or best_deal is None:
            continue

        min_imp = float(config.sweetener_min_improvement)
        # Commit only if it improves receiver without worsening the other side's verdict.
        old_rv = _receiver_verdict(current_best)
        new_rv = _receiver_verdict(best_prop)
        old_ov = _other_verdict(current_best)
        new_ov = _other_verdict(best_prop)

        # ---- Early stop / waste guard (v1 feature restored, v2-friendly)
        # 의미 있는 개선이 거의 없으면 더 붙여도 낭비일 확률이 높아 전체 루프 중단.
        # 단, receiver verdict가 실제로 개선된 경우(예: COUNTER->ACCEPT)는 무조건 허용.
        score_delta = float(best_prop.score) - float(current_best.score)
        margin_delta = float(_receiver_margin(best_prop)) - float(_receiver_margin(current_best))
        verdict_improved = verdict_rank.get(new_rv, 0) > verdict_rank.get(old_rv, 0)
        if (not verdict_improved) and max(score_delta, margin_delta) < min_imp:
            # best-of-N으로 봤는데도 이 정도면, 다음 sweetener는 효율이 낮다.
            break

        receiver_improve = verdict_rank.get(new_rv, 0) > verdict_rank.get(old_rv, 0) or (_receiver_margin(best_prop) > _receiver_margin(current_best) + 1e-6)
        other_not_worse = verdict_rank.get(new_ov, 0) >= verdict_rank.get(old_ov, 0)

        if receiver_improve and other_not_worse:
            current_best = best_prop
            current_deal = _clone_deal(best_deal)
            new_committed = _committed_count(current_deal)
            if new_committed > committed:
                stats.sweeteners_added += (new_committed - committed)
                if hasattr(stats, 'sweetener_commits'):
                    stats.sweetener_commits += (new_committed - committed)
            committed = new_committed
        else:
            if hasattr(stats, 'sweetener_rollbacks'):
                stats.sweetener_rollbacks += 1

        # 둘 다 accept면 즉시 종료
        if current_best.buyer_decision.verdict == DealVerdict.ACCEPT and current_best.seller_decision.verdict == DealVerdict.ACCEPT:
            break

    return current_best, extra_v, extra_e


def _all_pick_ids_in_deal(deal: Deal, team_id: str) -> Set[str]:
    """Deal 내 해당 팀 leg의 pick_id set."""
    tid = str(team_id).upper()
    return {a.pick_id for a in (deal.legs.get(tid, []) or []) if isinstance(a, PickAsset)}


def _all_swap_ids_in_deal(deal: Deal, team_id: str) -> Set[str]:
    """Deal 내 해당 팀 leg의 swap_id set."""
    tid = str(team_id).upper()
    return {a.swap_id for a in (deal.legs.get(tid, []) or []) if isinstance(a, SwapAsset)}


def _collect_sweetener_candidates(
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
    limit: int,
    allow_locked_by_deal_id: Optional[str],
) -> List[Asset]:
    """주어진 bucket에서 sweetener 후보를 여러 개 수집(Deal은 mutate하지 않음).

    - limit: 최대 후보 수(예산 기반으로 상위에서 잘라 쓴다)
    """

    giver_team = str(giver_team).upper()
    receiver_team = str(receiver_team).upper()
    out: List[Asset] = []

    # shape guards
    if len(deal.legs.get(giver_team, []) or []) >= int(config.max_assets_per_side):
        return out

    # SWAP
    if bucket == 'SWAP':
        if _count_swaps(deal, giver_team) >= 1:
            return out
        cands = list(out_cat.swap_ids or ())
        rng.shuffle(cands)
        for sid in cands:
            s = out_cat.swaps.get(sid)
            if s is None:
                continue
            if _is_locked_candidate(getattr(s, 'lock', None), allow_locked_by_deal_id=allow_locked_by_deal_id):
                continue
            a = s.as_asset()
            k = asset_key(a)
            if k in banned_asset_keys:
                continue
            if _asset_in_deal(deal, a):
                continue
            out.append(a)
            if len(out) >= int(limit):
                break
        return out

    # pick buckets
    if _count_picks(deal, giver_team) >= int(config.max_picks_per_side):
        return out

    pick_bucket: Optional[PickBucketId] = None
    if bucket == 'SECOND':
        if _count_seconds(deal, giver_team, catalog=catalog) >= int(config.max_seconds_per_side):
            return out
        pick_bucket = 'SECOND'
    elif bucket == 'FIRST_SAFE':
        pick_bucket = 'FIRST_SAFE'
    elif bucket == 'FIRST_SENSITIVE':
        pick_bucket = 'FIRST_SENSITIVE'
    else:
        return out

    # exclude pick ids already in this deal (global)
    excluded: Set[str] = {a.pick_id for leg in deal.legs.values() for a in (leg or []) if isinstance(a, PickAsset)}

    # candidates in bucket
    cand_ids = [str(pid) for pid in out_cat.pick_ids_by_bucket.get(pick_bucket, tuple()) if str(pid) not in excluded]
    if not cand_ids:
        return out

    def mv_abs(pid: str) -> float:
        p = out_cat.picks.get(pid)
        try:
            mv = float(p.market.total) if p is not None else 0.0
        except Exception:
            mv = 0.0
        return abs(mv - float(target_value))

    cand_ids.sort(key=mv_abs)

    for pid in cand_ids:
        p = out_cat.picks.get(pid)
        if p is None:
            continue
        # intrinsic horizon: within_max_years가 false면 스킵(validator도 어차피 실패)
        if not bool(getattr(p, 'within_max_years', True)):
            continue
        if _is_locked_candidate(getattr(p, 'lock', None), allow_locked_by_deal_id=allow_locked_by_deal_id):
            continue

        # Stepien check for 1st
        if pick_bucket in ('FIRST_SAFE', 'FIRST_SENSITIVE'):
            out_ids, in_ids = _team_pick_flow(deal, giver_team)
            out_ids = set(out_ids | {pid})
            try:
                if not catalog.stepien.is_compliant_after(team_id=giver_team, outgoing_pick_ids=out_ids, incoming_pick_ids=set(in_ids)):
                    continue
            except Exception:
                # helper 실패 시 validator에 맡긴다.
                pass

        a = p.as_asset()
        k = asset_key(a)
        if k in banned_asset_keys:
            continue
        if _asset_in_deal(deal, a):
            continue

        out.append(a)
        if len(out) >= int(limit):
            break

    return out


def _asset_in_deal(deal: Deal, asset: Asset) -> bool:
    k = asset_key(asset)
    for assets in deal.legs.values():
        for a in assets:
            if asset_key(a) == k:
                return True
    return False
