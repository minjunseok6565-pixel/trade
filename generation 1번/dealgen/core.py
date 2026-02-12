from __future__ import annotations

from dataclasses import replace
import random

from typing import Dict, List, Optional, Set, Tuple

from ...models import Deal, PlayerAsset, PickAsset, SwapAsset
from ...valuation.types import DealDecision, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import TradeAssetCatalog, TeamOutgoingCatalog, build_trade_asset_catalog

from .types import DealGeneratorConfig, DealGeneratorBudget, DealGeneratorStats, DealProposal, DealCandidate
from .config import _scale_budget
from .rng import _compute_seed, _compute_sweetener_seed
from .utils import (
    _get_trade_deadline_date,
    _get_second_apron_threshold,
    _estimate_team_payroll_after_dollars,
    _can_absorb_without_outgoing,
    _count_players,
)
from .dedupe import dedupe_hash
from .targets import select_targets_buy, select_targets_sell, select_buyers_for_sale_asset
from .skeletons import build_offer_skeletons_buy, build_offer_skeletons_sell, expand_variants
from .repair import repair_until_valid
from .scoring import evaluate_and_score, _proposal_from_cached_eval, _should_discard_prop
from .sweetener import maybe_apply_sweeteners

# =============================================================================
# DealGenerator
# =============================================================================


class DealGenerator:
    """Tick-scoped caches를 사용하는 2-team deal generator."""

    def __init__(self, config: Optional[DealGeneratorConfig] = None):
        self.config = config or DealGeneratorConfig()
        self.last_stats: Optional[DealGeneratorStats] = None

        # Cache for per-call asset catalogs built with allow_locked_by_deal_id.
        #
        # NOTE:
        # TradeGenerationTickContext is @dataclass(slots=True) and is NOT weakref-able.
        # So we keep a single-tick cache keyed by allow_locked_by_deal_id, and clear it
        # whenever the tick_ctx identity changes.
        self._asset_catalog_cache_tick_id: Optional[int] = None
        self._asset_catalog_cache: Dict[str, TradeAssetCatalog] = {}


    def _get_asset_catalog_for_call(
        self,
        tick_ctx: TradeGenerationTickContext,
        *,
        allow_locked_by_deal_id: Optional[str],
    ) -> Optional[TradeAssetCatalog]:
        """이번 generate_for_team 호출에 사용할 TradeAssetCatalog를 반환.

        정책:
        - allow_locked_by_deal_id가 None/blank 이거나, config.rebuild_catalog_when_allow_locked=False면:
          tick_ctx.asset_catalog을 재사용하되, 없으면 build해서 tick_ctx.asset_catalog에 주입 후 사용.
        - allow_locked_by_deal_id가 유효하고 rebuild가 True면:
          allow_locked_by_deal_id별로 catalog를 build하고, tick_ctx 단위(id 기준)로 캐싱하여 재사용.
        - allow-locked rebuild 실패 시:
          base catalog(tick_ctx.asset_catalog)을 fallback으로 사용하고,
          같은 tick에서 반복 rebuild 시도를 막기 위해 fallback을 negative-cache로 저장.
        """
        # Normalize allow_locked_by_deal_id (treat empty/whitespace as None)
        allow_id = str(allow_locked_by_deal_id or "").strip()

        # If allow-locked rebuild is disabled OR allow_id is empty => base catalog path
        if not allow_id or not bool(getattr(self.config, "rebuild_catalog_when_allow_locked", True)):
            if tick_ctx.asset_catalog is None:
                try:
                    tick_ctx.asset_catalog = build_trade_asset_catalog(tick_ctx=tick_ctx)
                except Exception:
                    return None
            return tick_ctx.asset_catalog

        # Ensure base catalog exists for fallback
        if tick_ctx.asset_catalog is None:
            try:
                tick_ctx.asset_catalog = build_trade_asset_catalog(tick_ctx=tick_ctx)
            except Exception:
                return None
        base_cat = tick_ctx.asset_catalog

        # Single-tick cache: clear whenever tick_ctx identity changes.
        tick_id = id(tick_ctx)
        if self._asset_catalog_cache_tick_id != tick_id:
            self._asset_catalog_cache_tick_id = tick_id
            self._asset_catalog_cache.clear()

        cached = self._asset_catalog_cache.get(allow_id)
        if cached is not None:
            return cached

        # Build allow-locked catalog (and cache)
        try:
            cat = build_trade_asset_catalog(
                tick_ctx=tick_ctx,
                allow_locked_by_deal_id=allow_id,
            )
        except Exception:
            # Fallback to base catalog and negative-cache to avoid repeated rebuild attempts this tick.
            self._asset_catalog_cache[allow_id] = base_cat
            return base_cat

        self._asset_catalog_cache[allow_id] = cat
        return cat


    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def generate_for_team(
        self,
        team_id: str,
        tick_ctx: TradeGenerationTickContext,
        *,
        max_results: int = 8,
        allow_locked_by_deal_id: Optional[str] = None,
    ) -> List[DealProposal]:
        """team_id를 기준으로 2-team 딜 후보를 생성.

        posture에 따라 모드가 달라진다.
        - BUY/SOFT_BUY/STAND_PAT: team_id를 buyer로 간주
        - SELL/SOFT_SELL: team_id를 seller로 간주(매물 제안 모드)

        반환:
        - score 내림차순
        - 모든 딜은 validate 통과
        - buyer/seller 모두 evaluate 포함
        """

        # --- asset catalog 확보
        # allow_locked_by_deal_id가 주어진 경우, tick_ctx 단위 캐시를 사용해 1회만 재빌드/재사용한다.
        # tick_ctx.asset_catalog이 None이면 자동으로 build한다.
        catalog = self._get_asset_catalog_for_call(tick_ctx, allow_locked_by_deal_id=allow_locked_by_deal_id)
        if catalog is None:
            return []

        tid = str(team_id).upper()

        # trade deadline hard stop (SSOT: DeadlineRule)
        deadline = _get_trade_deadline_date(tick_ctx)
        if deadline is not None and tick_ctx.current_date > deadline:
            self.last_stats = DealGeneratorStats(mode="SKIP_DEADLINE")
            return []

        ts = tick_ctx.get_team_situation(tid)

        # 즉시 중단
        if bool(getattr(ts, "constraints", None) and ts.constraints.cooldown_active):
            self.last_stats = DealGeneratorStats(mode="SKIP")
            return []

        posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
        if posture == "STAND_PAT" and float(getattr(ts, "urgency", 0.0) or 0.0) < 0.35:
            self.last_stats = DealGeneratorStats(mode="SKIP")
            return []

        budget = _scale_budget(self.config, ts)
        rng = random.Random(_compute_seed(self.config, tick_ctx, tid))

        stats = DealGeneratorStats(mode="SELL" if posture in {"SELL", "SOFT_SELL"} else "BUY")

        if posture in {"SELL", "SOFT_SELL"}:
            proposals = _generate_sell_mode(
                initiator_seller_id=tid,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=self.config,
                budget=budget,
                rng=rng,
                max_results=int(max_results),
                allow_locked_by_deal_id=allow_locked_by_deal_id,
                stats=stats,
            )
        else:
            proposals = _generate_buy_mode(
                initiator_buyer_id=tid,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=self.config,
                budget=budget,
                rng=rng,
                max_results=int(max_results),
                allow_locked_by_deal_id=allow_locked_by_deal_id,
                stats=stats,
            )

        self.last_stats = stats
        return proposals


# =============================================================================
# Mode orchestrators
# =============================================================================


def _generate_buy_mode(
    *,
    initiator_buyer_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    max_results: int,
    allow_locked_by_deal_id: Optional[str],
    stats: DealGeneratorStats,
) -> List[DealProposal]:
    buyer_id = str(initiator_buyer_id).upper()
    ts_buyer = tick_ctx.get_team_situation(buyer_id)

    # 탐색 상태
    # - seen_skeleton: repair 이전(스켈레톤/변형 단계) 중복 제거
    # - seen_output: 실제로 결과 리스트에 push된(=출력된) 딜 형태 중복 제거
    #
    # IMPORTANT
    # - repair 이후 h_valid를 seen_output에 선등록하면 sweetener 단계에서 갈라질 수 있는
    #   유니크 딜을 놓칠 수 있다. 따라서 seen_output은 '실제로 push된 딜'만 기록한다.
    seen_skeleton: Set[str] = set()
    seen_output: Set[str] = set()

    # base deal(h_valid) 재등장 시 evaluate 비용을 줄이기 위한 캐시
    # score는 opponent_repeat_count 등 동적 요소가 있어 캐시하지 않는다.
    base_eval_cache: Dict[str, Tuple[DealDecision, DealDecision, TeamDealEvaluation, TeamDealEvaluation]] = {}

    # 같은 base deal에서 sweetener를 여러 번 시도할 수 있게 하되 비용 폭증을 막기 위한 카운터
    sweetener_trials_by_base: Dict[str, int] = {}
    # 같은 base deal에서 fit-swap(FIT_FAILS counter)을 여러 번 시도할 수 있게 하되 비용 폭증을 막기 위한 카운터
    fit_swap_trials_by_base: Dict[str, int] = {}
    banned_asset_keys: Set[str] = set()
    banned_players: Set[str] = set()
    banned_receivers_by_player: Dict[str, Set[str]] = {}

    proposals: List[DealProposal] = []

    partner_counts: Dict[str, int] = {}

    target_counts: Dict[str, int] = {}
    # partner diversity
    # - soft penalty는 scoring.score_deal()에서 opponent_repeat_penalty로 이미 처리한다.
    # - hard cap(max_partner_repeats)을 적용하려면, cap 적용 전까지 후보 풀을 넉넉히 유지해야
    #   결과가 한 팀에 쏠릴 때도 다양화된 최종 리스트를 채울 수 있다.
    partner_cap = int(getattr(config, "max_partner_repeats", 0) or 0)
    pool_cap = int(max_results)
    if partner_cap > 0:
        pool_cap = max(pool_cap, int(max_results) * max(2, partner_cap))

    # target diversity: target_repeat_penalty가 켜져 있으면 상단이 빨리 채워져도 더 많은 타깃을 보게 pool을 넓힌다.
    if float(getattr(config, "target_repeat_penalty", 0.0) or 0.0) > 0.0:
        pool_cap = max(pool_cap, int(max_results) * 2)

    max_sweetener_trials_per_base = int(getattr(config, "sweetener_max_trials_per_base", 2))
    max_fit_swap_trials_per_base = int(getattr(config, "fit_swap_max_trials_per_base", 1))

    targets = select_targets_buy(
        buyer_id,
        tick_ctx,
        catalog,
        config,
        budget=budget,
        rng=rng,
        banned_players=banned_players,
    )

    for t in targets:
        if len(proposals) >= pool_cap:
            break
        if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
            break

        stats.targets_considered += 1

        target_pid = str(getattr(t, "player_id", "") or "")

        seller_id = str(t.from_team).upper()
        if seller_id == buyer_id:
            continue

        ts_seller = tick_ctx.get_team_situation(seller_id)
        if bool(getattr(ts_seller, "constraints", None) and ts_seller.constraints.cooldown_active):
            continue

        candidates = build_offer_skeletons_buy(
            buyer_id,
            seller_id,
            t,
            tick_ctx,
            catalog,
            config=config,
            budget=budget,
            rng=rng,
            banned_asset_keys=banned_asset_keys,
            banned_players=banned_players,
            banned_receivers_by_player=banned_receivers_by_player,
        )

        if not candidates:
            continue

        stats.skeletons_built += len(candidates)

        # 변형 확장: 타깃당 6~12개로 제한(폭발 방지)
        candidates = expand_variants(
            buyer_id,
            seller_id,
            t,
            candidates,
            tick_ctx,
            catalog,
            config=config,
            budget=budget,
            rng=rng,
            banned_asset_keys=banned_asset_keys,
            banned_players=banned_players,
            banned_receivers_by_player=banned_receivers_by_player,
        )
        variant_cap = min(12, max(6, int(budget.beam_width)))

        # soft guard: payroll_after_est 기준 2nd apron one-for-one 위반 가능 후보 제거(탐색 낭비/invalid 감소)
        if getattr(config, "soft_guard_second_apron_by_constraints", False):
            candidates = _soft_guard_second_apron_candidates(candidates, tick_ctx)
            if not candidates:
                continue

        candidates = _beam_select_candidates(
            candidates,
            buyer_id=buyer_id,
            seller_id=seller_id,
            tick_ctx=tick_ctx,
            catalog=catalog,
            rng=rng,
            cap=variant_cap,
        )

        attempts = 0
        for cand in candidates:
            if attempts >= budget.max_attempts_per_target:
                break
            if len(proposals) >= pool_cap:
                break
            if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
                break

            attempts += 1
            stats.candidates_attempted += 1

            h = dedupe_hash(cand.deal)
            if h in seen_skeleton:
                continue
            seen_skeleton.add(h)

            ok, cand2, v_used = repair_until_valid(
                cand,
                tick_ctx,
                catalog,
                config,
                allow_locked_by_deal_id=allow_locked_by_deal_id,
                budget=budget,
                banned_asset_keys=banned_asset_keys,
                banned_players=banned_players,
                banned_receivers_by_player=banned_receivers_by_player,
                stats=stats,
            )
            stats.validations += v_used
            if not ok or cand2 is None:
                continue

            # (A) repair 이후 base deal identity (수리 과정에서 서로 다른 스켈레톤이 같은 딜로 수렴 가능)
            h_valid = dedupe_hash(cand2.deal)

            # 이미 출력된 base라면, sweetener/fit-swap 둘 다 더 시도할 여지가 없을 때만 스킵(비용 가드)
            if h_valid in seen_output:
                sweet_left = (
                    bool(getattr(config, "sweetener_enabled", True))
                    and int(getattr(config, "sweetener_max_additions", 0)) > 0
                    and int(sweetener_trials_by_base.get(h_valid, 0)) < int(max_sweetener_trials_per_base)
                )
                fit_left = (
                    bool(getattr(config, "fit_swap_enabled", True))
                    and int(max_fit_swap_trials_per_base) > 0
                    and int(fit_swap_trials_by_base.get(h_valid, 0)) < int(max_fit_swap_trials_per_base)
                )
                if not sweet_left and not fit_left:
                    continue

            # evaluate (cache)
            cached = base_eval_cache.get(h_valid)
            if cached is None:
                base_prop, e_used = evaluate_and_score(
                    cand2.deal,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    tick_ctx=tick_ctx,
                    config=config,
                    tags=tuple(cand2.tags),
                    opponent_repeat_count=int(partner_counts.get(seller_id, 0)),
                    stats=stats,
                )
                stats.evaluations += e_used
                if base_prop is None:
                    continue
                base_eval_cache[h_valid] = (
                    base_prop.buyer_decision,
                    base_prop.seller_decision,
                    base_prop.buyer_eval,
                    base_prop.seller_eval,
                )
            else:
                bd, sd, be, se = cached
                base_prop = _proposal_from_cached_eval(
                    cand2.deal,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    buyer_decision=bd,
                    seller_decision=sd,
                    buyer_eval=be,
                    seller_eval=se,
                    config=config,
                    tags=tuple(cand2.tags),
                    opponent_repeat_count=int(partner_counts.get(seller_id, 0)),
                )

            # filter: 너무 말도 안 되는 손해
            if _should_discard_prop(base_prop, config):
                continue

            # --- FIT_FAILS -> fit swap counter (v2 absorption)
            pre_sweet_prop = base_prop
            pre_sweet_hash = h_valid

            fit_enabled = bool(getattr(config, "fit_swap_enabled", True))
            if fit_enabled and int(max_fit_swap_trials_per_base) > 0:
                if int(fit_swap_trials_by_base.get(h_valid, 0)) < int(max_fit_swap_trials_per_base):
                    try:
                        from .fit_swap import maybe_apply_fit_swap  # local import to avoid cycles
                    except ImportError:
                        maybe_apply_fit_swap = None  # type: ignore

                    if maybe_apply_fit_swap is not None:
                        val_rem = max(0, int(budget.max_validations) - int(stats.validations))
                        eval_rem = max(0, int(budget.max_evaluations) - int(stats.evaluations))

                        trial_idx = int(fit_swap_trials_by_base.get(h_valid, 0))
                        local_seed = _compute_sweetener_seed(
                            config,
                            tick_ctx,
                            initiator_team_id=buyer_id,
                            counterparty_team_id=seller_id,
                            base_hash=pre_sweet_hash,
                            skeleton_hash=f"fit_swap|{h}",
                            trial_index=trial_idx,
                        )
                        local_rng = random.Random(int(local_seed))

                        res = maybe_apply_fit_swap(
                            pre_sweet_prop,
                            tick_ctx=tick_ctx,
                            catalog=catalog,
                            config=config,
                            budget=budget,
                            validations_remaining=int(val_rem),
                            evaluations_remaining=int(eval_rem),
                            allow_locked_by_deal_id=allow_locked_by_deal_id,
                            banned_asset_keys=banned_asset_keys,
                            banned_players=banned_players,
                            banned_receivers_by_player=banned_receivers_by_player,
                            protected_player_id=cand2.focal_player_id,
                            opponent_repeat_count=int(partner_counts.get(seller_id, 0)),
                            rng=local_rng,
                            stats=stats,
                        )

                        # maybe_apply_fit_swap can return None (e.g. no FIT_FAILS / empty pool / budget gate).
                        # Count it as a trial to avoid repeatedly retrying the same base deal.
                        if res is None:
                            fit_swap_trials_by_base[h_valid] = int(fit_swap_trials_by_base.get(h_valid, 0)) + 1
                            
                        # budget counters
                        stats.validations += int(getattr(res, "validations_used", 0) or 0)
                        stats.evaluations += int(getattr(res, "evaluations_used", 0) or 0)

                        # telemetry
                        tried = int(getattr(res, "candidates_tried", 0) or 0)
                        if tried > 0:
                            stats.fit_swap_triggers += 1
                            stats.fit_swap_candidates_tried += tried
                            fit_swap_trials_by_base[h_valid] = int(fit_swap_trials_by_base.get(h_valid, 0)) + 1

                            if bool(getattr(res, "swapped", False)):
                                stats.fit_swap_success += 1

                        # update base for sweetener stage
                        pre_sweet_prop = getattr(res, "proposal", pre_sweet_prop) or pre_sweet_prop
                        pre_sweet_hash = dedupe_hash(pre_sweet_prop.deal)

            # sweetener loop (대개 buyer -> seller)
            best_prop = pre_sweet_prop
            if config.sweetener_enabled and int(config.sweetener_max_additions) > 0:
                trial_idx = int(sweetener_trials_by_base.get(pre_sweet_hash, 0))
                if trial_idx < int(max_sweetener_trials_per_base):
                    sweetener_trials_by_base[pre_sweet_hash] = trial_idx + 1
                    local_seed = _compute_sweetener_seed(
                        config,
                        tick_ctx,
                        initiator_team_id=buyer_id,
                        counterparty_team_id=seller_id,
                        base_hash=pre_sweet_hash,
                        skeleton_hash=h,
                        trial_index=trial_idx,
                    )
                    local_rng = random.Random(int(local_seed))

                    best_prop, extra_v, extra_e = maybe_apply_sweeteners(
                        pre_sweet_prop,
                        tick_ctx=tick_ctx,
                        catalog=catalog,
                        config=config,
                        budget=budget,
                        allow_locked_by_deal_id=allow_locked_by_deal_id,
                        banned_asset_keys=banned_asset_keys,
                        rng=local_rng,
                        stats=stats,
                    )
                    stats.validations += extra_v
                    stats.evaluations += extra_e

            # (B) 최종 중복 제거는 '실제로 push된 딜'만 기준으로 한다.
            #     - sweetener 결과가 중복이면 base 딜을 fallback으로 push할 수 있어야 한다.
            pushed: Optional[DealProposal] = None

            h_best = dedupe_hash(best_prop.deal)
            if h_best not in seen_output:
                pushed = best_prop
                seen_output.add(h_best)
            else:
                # sweetened가 중복이면 (fit-swap 포함) 현재 base라도 유니크할 때는 결과로 남긴다.
                if pre_sweet_hash not in seen_output:
                    pushed = pre_sweet_prop
                    seen_output.add(pre_sweet_hash)

            if pushed is None:
                continue

            # (C) target repetition penalty (v2 absorption) - apply only at final push stage
            if target_pid:
                pushed = _apply_target_repeat_penalty(
                    pushed,
                    target_repeat_count=int(target_counts.get(target_pid, 0)),
                    cfg=config,
                )

            proposals = _push_best(
                proposals,
                pushed,
                max_results=pool_cap,
            )
            partner_counts[pushed.seller_id] = int(partner_counts.get(pushed.seller_id, 0)) + 1
            if target_pid:
                target_counts[target_pid] = int(target_counts.get(target_pid, 0)) + 1

    proposals.sort(key=lambda p: p.score, reverse=True)
    proposals = _apply_partner_cap(
        proposals,
        max_results=max_results,
        partner_side="seller",  # BUY: 다양화 기준 = seller
        cap=partner_cap,
    )
    return proposals


def _generate_sell_mode(
    *,
    initiator_seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    rng: random.Random,
    max_results: int,
    allow_locked_by_deal_id: Optional[str],
    stats: DealGeneratorStats,
) -> List[DealProposal]:
    seller_id = str(initiator_seller_id).upper()
    ts_seller = tick_ctx.get_team_situation(seller_id)

    # 탐색 상태
    # - seen_skeleton: repair 이전(스켈레톤/변형 단계) 중복 제거
    # - seen_output: 실제로 결과 리스트에 push된(=출력된) 딜 형태 중복 제거
    #
    # IMPORTANT
    # - repair 이후 h_valid를 seen_output에 선등록하면 sweetener 단계에서 갈라질 수 있는
    #   유니크 딜을 놓칠 수 있다. 따라서 seen_output은 '실제로 push된 딜'만 기록한다.
    seen_skeleton: Set[str] = set()
    seen_output: Set[str] = set()

    # base deal(h_valid) 재등장 시 evaluate 비용을 줄이기 위한 캐시
    # score는 opponent_repeat_count 등 동적 요소가 있어 캐시하지 않는다.
    base_eval_cache: Dict[str, Tuple[DealDecision, DealDecision, TeamDealEvaluation, TeamDealEvaluation]] = {}

    # 같은 base deal에서 sweetener를 여러 번 시도할 수 있게 하되 비용 폭증을 막기 위한 카운터
    sweetener_trials_by_base: Dict[str, int] = {}
    # 같은 base deal에서 fit-swap(FIT_FAILS counter)을 여러 번 시도할 수 있게 하되 비용 폭증을 막기 위한 카운터
    fit_swap_trials_by_base: Dict[str, int] = {}
    banned_asset_keys: Set[str] = set()
    banned_players: Set[str] = set()
    banned_receivers_by_player: Dict[str, Set[str]] = {}

    proposals: List[DealProposal] = []
    partner_counts: Dict[str, int] = {}

    target_counts: Dict[str, int] = {}
    # partner diversity (SELL 모드에서는 'buyer'가 파트너)
    partner_cap = int(getattr(config, "max_partner_repeats", 0) or 0)
    pool_cap = int(max_results)
    if partner_cap > 0:
        pool_cap = max(pool_cap, int(max_results) * max(2, partner_cap))

    # target diversity: target_repeat_penalty가 켜져 있으면 상단이 빨리 채워져도 더 많은 타깃을 보게 pool을 넓힌다.
    if float(getattr(config, "target_repeat_penalty", 0.0) or 0.0) > 0.0:
        pool_cap = max(pool_cap, int(max_results) * 2)

    max_sweetener_trials_per_base = int(getattr(config, "sweetener_max_trials_per_base", 2))
    max_fit_swap_trials_per_base = int(getattr(config, "fit_swap_max_trials_per_base", 1))

    sale_assets = select_targets_sell(
        seller_id,
        tick_ctx,
        catalog,
        config,
        budget=budget,
        rng=rng,
        banned_players=banned_players,
        allow_locked_by_deal_id=allow_locked_by_deal_id,
    )

    for s in sale_assets:
        if len(proposals) >= pool_cap:
            break
        if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
            break

        stats.targets_considered += 1

        target_pid = str(getattr(s, "player_id", "") or "")

        buyer_candidates = select_buyers_for_sale_asset(
            seller_id,
            s,
            tick_ctx,
            catalog,
            config=config,
            budget=budget,
            rng=rng,
        )

        for buyer_id, match_tag in buyer_candidates:
            if len(proposals) >= pool_cap:
                break
            if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
                break

            buyer_id = str(buyer_id).upper()
            if buyer_id == seller_id:
                continue
            ts_buyer = tick_ctx.get_team_situation(buyer_id)
            if bool(getattr(ts_buyer, "constraints", None) and ts_buyer.constraints.cooldown_active):
                continue

            candidates = build_offer_skeletons_sell(
                seller_id=seller_id,
                buyer_id=buyer_id,
                sale_asset=s,
                match_tag=match_tag,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=config,
                budget=budget,
                rng=rng,
                banned_asset_keys=banned_asset_keys,
                banned_players=banned_players,
                banned_receivers_by_player=banned_receivers_by_player,
            )

            if not candidates:
                continue

            stats.skeletons_built += len(candidates)

            # soft guard: payroll_after_est 기준 2nd apron one-for-one 위반 가능 후보 제거(탐색 낭비/invalid 감소)
            if getattr(config, "soft_guard_second_apron_by_constraints", False):
                candidates = _soft_guard_second_apron_candidates(candidates, tick_ctx)
                if not candidates:
                    continue

            candidates = _beam_select_candidates(
                candidates,
                buyer_id=buyer_id,
                seller_id=seller_id,
                tick_ctx=tick_ctx,
                catalog=catalog,
                rng=rng,
                cap=max(1, int(budget.beam_width)),
            )

            attempts = 0
            for cand in candidates:
                if attempts >= budget.max_attempts_per_target:
                    break
                if len(proposals) >= pool_cap:
                    break
                if stats.validations >= budget.max_validations or stats.evaluations >= budget.max_evaluations:
                    break

                attempts += 1
                stats.candidates_attempted += 1

                h = dedupe_hash(cand.deal)
                if h in seen_skeleton:
                    continue
                seen_skeleton.add(h)

                ok, cand2, v_used = repair_until_valid(
                    cand,
                    tick_ctx,
                    catalog,
                    config,
                    allow_locked_by_deal_id=allow_locked_by_deal_id,
                    budget=budget,
                    banned_asset_keys=banned_asset_keys,
                    banned_players=banned_players,
                    banned_receivers_by_player=banned_receivers_by_player,
                    stats=stats,
                )
                stats.validations += v_used
                if not ok or cand2 is None:
                    continue

                # (A) repair 이후 base deal identity (수리 과정에서 서로 다른 스켈레톤이 같은 딜로 수렴 가능)
                h_valid = dedupe_hash(cand2.deal)

                # 이미 출력된 base라면, sweetener/fit-swap 둘 다 더 시도할 여지가 없을 때만 스킵(비용 가드)
                if h_valid in seen_output:
                    sweet_left = (
                        bool(getattr(config, "sweetener_enabled", True))
                        and int(getattr(config, "sweetener_max_additions", 0)) > 0
                        and int(sweetener_trials_by_base.get(h_valid, 0)) < int(max_sweetener_trials_per_base)
                    )
                    fit_left = (
                        bool(getattr(config, "fit_swap_enabled", True))
                        and int(max_fit_swap_trials_per_base) > 0
                        and int(fit_swap_trials_by_base.get(h_valid, 0)) < int(max_fit_swap_trials_per_base)
                    )
                    if not sweet_left and not fit_left:
                        continue

                # evaluate (cache)
                cached = base_eval_cache.get(h_valid)
                if cached is None:
                    base_prop, e_used = evaluate_and_score(
                        cand2.deal,
                        buyer_id=buyer_id,
                        seller_id=seller_id,
                        tick_ctx=tick_ctx,
                        config=config,
                        tags=tuple(cand2.tags),
                        opponent_repeat_count=int(partner_counts.get(buyer_id, 0)),
                        stats=stats,
                    )
                    stats.evaluations += e_used
                    if base_prop is None:
                        continue
                    base_eval_cache[h_valid] = (
                        base_prop.buyer_decision,
                        base_prop.seller_decision,
                        base_prop.buyer_eval,
                        base_prop.seller_eval,
                    )
                else:
                    bd, sd, be, se = cached
                    base_prop = _proposal_from_cached_eval(
                        cand2.deal,
                        buyer_id=buyer_id,
                        seller_id=seller_id,
                        buyer_decision=bd,
                        seller_decision=sd,
                        buyer_eval=be,
                        seller_eval=se,
                        config=config,
                        tags=tuple(cand2.tags),
                        opponent_repeat_count=int(partner_counts.get(buyer_id, 0)),
                    )

                if _should_discard_prop(base_prop, config):
                    continue

                # --- FIT_FAILS -> fit swap counter (v2 absorption)
                pre_sweet_prop = base_prop
                pre_sweet_hash = h_valid

                fit_enabled = bool(getattr(config, "fit_swap_enabled", True))
                if fit_enabled and int(max_fit_swap_trials_per_base) > 0:
                    if int(fit_swap_trials_by_base.get(h_valid, 0)) < int(max_fit_swap_trials_per_base):
                        try:
                            from .fit_swap import maybe_apply_fit_swap  # local import to avoid cycles
                        except ImportError:
                            maybe_apply_fit_swap = None  # type: ignore

                        if maybe_apply_fit_swap is not None:
                            val_rem = max(0, int(budget.max_validations) - int(stats.validations))
                            eval_rem = max(0, int(budget.max_evaluations) - int(stats.evaluations))

                            trial_idx = int(fit_swap_trials_by_base.get(h_valid, 0))
                            local_seed = _compute_sweetener_seed(
                                config,
                                tick_ctx,
                                initiator_team_id=seller_id,
                                counterparty_team_id=buyer_id,
                                base_hash=pre_sweet_hash,
                                skeleton_hash=f"fit_swap|{h}",
                                trial_index=trial_idx,
                            )
                            local_rng = random.Random(int(local_seed))

                            res = maybe_apply_fit_swap(
                                pre_sweet_prop,
                                tick_ctx=tick_ctx,
                                catalog=catalog,
                                config=config,
                                budget=budget,
                                validations_remaining=int(val_rem),
                                evaluations_remaining=int(eval_rem),
                                allow_locked_by_deal_id=allow_locked_by_deal_id,
                                banned_asset_keys=banned_asset_keys,
                                banned_players=banned_players,
                                banned_receivers_by_player=banned_receivers_by_player,
                                protected_player_id=cand2.focal_player_id,
                                opponent_repeat_count=int(partner_counts.get(buyer_id, 0)),
                                rng=local_rng,
                                stats=stats,
                            )
                            # maybe_apply_fit_swap can return None (e.g. no FIT_FAILS / empty pool / budget gate).
                            # Count it as a trial to avoid repeatedly retrying the same base deal.
                            if res is None:
                                fit_swap_trials_by_base[h_valid] = int(fit_swap_trials_by_base.get(h_valid, 0)) + 1

                            # budget counters
                            stats.validations += int(getattr(res, "validations_used", 0) or 0)
                            stats.evaluations += int(getattr(res, "evaluations_used", 0) or 0)

                            # telemetry
                            tried = int(getattr(res, "candidates_tried", 0) or 0)
                            if tried > 0:
                                stats.fit_swap_triggers += 1
                                stats.fit_swap_candidates_tried += tried
                                fit_swap_trials_by_base[h_valid] = int(fit_swap_trials_by_base.get(h_valid, 0)) + 1

                                if bool(getattr(res, "swapped", False)):
                                    stats.fit_swap_success += 1

                            # update base for sweetener stage
                            pre_sweet_prop = getattr(res, "proposal", pre_sweet_prop) or pre_sweet_prop
                            pre_sweet_hash = dedupe_hash(pre_sweet_prop.deal)

                best_prop = pre_sweet_prop
                if config.sweetener_enabled and int(config.sweetener_max_additions) > 0:
                    trial_idx = int(sweetener_trials_by_base.get(pre_sweet_hash, 0))
                    if trial_idx < int(max_sweetener_trials_per_base):
                        sweetener_trials_by_base[pre_sweet_hash] = trial_idx + 1
                        local_seed = _compute_sweetener_seed(
                            config,
                            tick_ctx,
                            initiator_team_id=seller_id,
                            counterparty_team_id=buyer_id,
                            base_hash=pre_sweet_hash,
                            skeleton_hash=h,
                            trial_index=trial_idx,
                        )
                        local_rng = random.Random(int(local_seed))

                        best_prop, extra_v, extra_e = maybe_apply_sweeteners(
                            pre_sweet_prop,
                            tick_ctx=tick_ctx,
                            catalog=catalog,
                            config=config,
                            budget=budget,
                            allow_locked_by_deal_id=allow_locked_by_deal_id,
                            banned_asset_keys=banned_asset_keys,
                            rng=local_rng,
                            stats=stats,
                        )
                        stats.validations += extra_v
                        stats.evaluations += extra_e

                # (B) 최종 중복 제거는 '실제로 push된 딜'만 기준으로 한다.
                pushed: Optional[DealProposal] = None

                h_best = dedupe_hash(best_prop.deal)
                if h_best not in seen_output:
                    pushed = best_prop
                    seen_output.add(h_best)
                else:
                    if pre_sweet_hash not in seen_output:
                        pushed = pre_sweet_prop
                        seen_output.add(pre_sweet_hash)

                if pushed is None:
                    continue

                if target_pid:
                    pushed = _apply_target_repeat_penalty(
                        pushed,
                        target_repeat_count=int(target_counts.get(target_pid, 0)),
                        cfg=config,
                    )

                proposals = _push_best(proposals, pushed, max_results=pool_cap)
                partner_counts[pushed.buyer_id] = int(partner_counts.get(pushed.buyer_id, 0)) + 1
                if target_pid:
                    target_counts[target_pid] = int(target_counts.get(target_pid, 0)) + 1


    proposals.sort(key=lambda p: p.score, reverse=True)
    proposals = _apply_partner_cap(
        proposals,
        max_results=max_results,
        partner_side="buyer",  # SELL: 다양화 기준 = buyer
        cap=partner_cap,
    )
    return proposals


def _apply_target_repeat_penalty(
    prop: DealProposal,
    *,
    target_repeat_count: int,
    cfg: DealGeneratorConfig,
) -> DealProposal:
    """동일 타깃 반복 노출 억제용 score 감점(v2 absorption).

    - v1의 sweetener/fit-swap 내부 선택은 base score를 기준으로 진행되므로,
      여기서는 '최종 push 직전'에만 감점을 적용해 결과 다양성만 유도한다.
    """

    pen = float(getattr(cfg, "target_repeat_penalty", 0.0) or 0.0)
    c = int(target_repeat_count or 0)
    if pen <= 0.0 or c <= 0:
        return prop
    return replace(prop, score=float(prop.score) - pen * float(c))


def _apply_partner_cap(
    proposals: List[DealProposal],
    *,
    max_results: int,
    partner_side: str,
    cap: int,
) -> List[DealProposal]:
    """Final output 다양화를 위한 hard cap 적용.

    v1은 scoring 단계에서 이미 soft penalty(opponent_repeat_penalty)를 적용하고 있으므로,
    여기서는 hard cap만 강제한다.

    partner_side:
      - "seller": BUY 모드에서 seller_id 기준으로 cap
      - "buyer":  SELL 모드에서 buyer_id 기준으로 cap
    """
    max_results_i = max(0, int(max_results))
    if max_results_i <= 0:
        return []

    cap_i = int(cap or 0)
    if cap_i <= 0:
        return proposals[:max_results_i]

    side = str(partner_side or "").lower()
    if side not in ("seller", "buyer"):
        side = "seller"

    counts: Dict[str, int] = {}
    out: List[DealProposal] = []

    for p in proposals:
        partner = p.seller_id if side == "seller" else p.buyer_id
        c = int(counts.get(partner, 0))
        if c >= cap_i:
            continue
        out.append(p)
        counts[partner] = c + 1
        if len(out) >= max_results_i:
            break

    return out


def _push_best(existing: List[DealProposal], prop: DealProposal, *, max_results: int) -> List[DealProposal]:
    existing.append(prop)
    existing.sort(key=lambda p: p.score, reverse=True)
    return existing[: max_results]


def _incoming_player_count(deal: Deal, team_id: str) -> int:
    """team_id 기준 incoming player count(2팀 딜 가정)."""
    tid = str(team_id).upper()
    other = [t for t in deal.teams if str(t).upper() != tid]
    if not other:
        return 0
    other_team = str(other[0]).upper()
    return sum(1 for a in deal.legs.get(other_team, []) if isinstance(a, PlayerAsset))


def _soft_guard_second_apron_candidates(
    candidates: List[DealCandidate],
    tick_ctx: TradeGenerationTickContext,
) -> List[DealCandidate]:
    """Soft guard: 2nd apron one-for-one 제약을 위반할 가능성이 큰 후보를 제거한다.

    SSOT는 validate_deal(SalaryMatchingRule)이며, 이 함수는 탐색 낭비를 줄이기 위한 휴리스틱이다.

    핵심 변경점(SSOT aligned)
    - TeamConstraints.apron_status(현 상태)로만 판단하지 않고,
      deal 적용 후 추정 payroll_after가 second_apron 이상일 때만 one-for-one을 강제한다.

    구현
    - payroll_after_est = payroll_before - outgoing_salary + incoming_salary (dollars)
    - if payroll_after_est >= second_apron:
        outgoing_players_count <= 1 AND incoming_players_count <= 1 이어야 통과

    fallback
    - second_apron 값을 SSOT에서 읽을 수 없으면 기존처럼 constraints.apron_status 기반으로만 soft guard.
    """
    second_apron = _get_second_apron_threshold(tick_ctx)
    out: List[DealCandidate] = []
    for c in candidates:
        d = c.deal
        ok = True
        for tid in [str(t).upper() for t in (d.teams or [])]:
            requires_guard = False

            if second_apron > 0.0:
                try:
                    payroll_after = _estimate_team_payroll_after_dollars(tick_ctx, d, tid)
                    if payroll_after >= float(second_apron):
                        requires_guard = True
                except Exception:
                    requires_guard = False
            else:
                # fallback: 기존 휴리스틱
                try:
                    ts = tick_ctx.get_team_situation(tid)
                    status = str(getattr(getattr(ts, "constraints", None), "apron_status", "") or "")
                    if status == "ABOVE_2ND_APRON":
                        requires_guard = True
                except Exception:
                    requires_guard = False

            if requires_guard:
                if _count_players(d, tid) > 1 or _incoming_player_count(d, tid) > 1:
                    ok = False
                    break
        if ok:
            out.append(c)
    return out


# =============================================================================
# Beam selection helpers (cheap heuristic pre-score)
# =============================================================================

def _sum_leg_player_salary_m(
    deal: Deal, *, team_id: str, out_cat: Optional[TeamOutgoingCatalog]
) -> float:
    if out_cat is None:
        return 0.0
    s = 0.0
    for a in deal.legs.get(str(team_id).upper(), []) or []:
        if isinstance(a, PlayerAsset):
            c = out_cat.players.get(a.player_id)
            if c is not None:
                s += float(c.salary_m)
    return float(s)


def _sum_leg_market_total(
    deal: Deal, *, team_id: str, out_cat: Optional[TeamOutgoingCatalog]
) -> float:
    if out_cat is None:
        return 0.0
    s = 0.0
    for a in deal.legs.get(str(team_id).upper(), []) or []:
        if isinstance(a, PlayerAsset):
            c = out_cat.players.get(a.player_id)
            if c is not None:
                s += float(c.market.total)
        elif isinstance(a, PickAsset):
            p = out_cat.picks.get(a.pick_id)
            if p is not None:
                s += float(p.market.total)
        elif isinstance(a, SwapAsset):
            # Swap은 catalog에 market이 없으므로(현재 프로젝트 구조) 0으로 둔다.
            # (beam pre-score용이므로 과도한 추정값을 넣지 않는다)
            s += 0.0
    return float(s)


def _prescore_candidate(
    cand: DealCandidate,
    *,
    buyer_id: str,
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
) -> float:
    """validate/evaluate 없이 후보를 정렬하기 위한 아주 가벼운 pre-score.

    목표:
    - 예산이 타이트할 때도 '실현 가능성 높은' 후보가 evaluate까지 올라가게 한다.
    - 완전 결정적이 되면 다양성이 죽으니, 실제 샘플링은 _beam_select_candidates가 담당.
    """

    buyer = str(buyer_id).upper()
    seller = str(seller_id).upper()

    buyer_out = catalog.outgoing_by_team.get(buyer)
    seller_out = catalog.outgoing_by_team.get(seller)

    d = cand.deal
    n_assets = sum(len(v) for v in d.legs.values())
    n_players = sum(1 for leg in d.legs.values() for a in leg if isinstance(a, PlayerAsset))

    score = 0.0

    # 1) 복잡도: 단순할수록 우선
    score -= 0.10 * max(0, int(n_assets) - 2)
    score -= 0.08 * max(0, int(n_players) - 2)

    # 2) salary plausibility (양쪽 모두 대충 체크)
    try:
        ts_buyer = tick_ctx.get_team_situation(buyer)
    except Exception:
        ts_buyer = None
    try:
        ts_seller = tick_ctx.get_team_situation(seller)
    except Exception:
        ts_seller = None

    buyer_in_m = _sum_leg_player_salary_m(d, team_id=seller, out_cat=seller_out)   # buyer가 받는 salary
    buyer_out_m = _sum_leg_player_salary_m(d, team_id=buyer, out_cat=buyer_out)    # buyer가 보내는 salary
    if buyer_in_m > 0.5:
        gap = abs(float(buyer_out_m) - float(buyer_in_m))
        score -= 0.55 * (gap / max(1.0, float(buyer_in_m)))
        # picks-only 류(보내는 선수가 거의 없음)인데 cap space도 없으면 강한 패널티
        if buyer_out_m < 0.10 and ts_buyer is not None:
            if not _can_absorb_without_outgoing(ts_buyer, buyer_in_m, buffer_m=0.0):
                score -= 5.0

    seller_in_m = buyer_out_m
    seller_out_m = buyer_in_m
    if seller_out_m > 0.5:
        gap = abs(float(seller_out_m) - float(seller_in_m))
        score -= 0.35 * (gap / max(1.0, float(seller_out_m)))
        if seller_in_m < 0.10 and ts_seller is not None:
            if not _can_absorb_without_outgoing(ts_seller, seller_in_m, buffer_m=0.0):
                score -= 2.0

    # 3) 대략적 가치 밸런스(과도한 overpay 후보를 아래로)
    gain_val = _sum_leg_market_total(d, team_id=seller, out_cat=seller_out)  # buyer가 얻는 가치(상대가 보내는 것)
    cost_val = _sum_leg_market_total(d, team_id=buyer, out_cat=buyer_out)    # buyer가 지불하는 가치
    if gain_val > 0.0:
        rel = (float(gain_val) - float(cost_val)) / max(10.0, float(gain_val))
        score += 0.80 * rel

    return float(score)


def _beam_select_candidates(
    candidates: List[DealCandidate],
    *,
    buyer_id: str,
    seller_id: str,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    rng: random.Random,
    cap: int,
) -> List[DealCandidate]:
    """랜덤 shuffle+slice 대신: pre-score 정렬 + 제한적 랜덤 샘플링(다양성 유지)."""
    cap_n = max(1, int(cap))
    if len(candidates) <= cap_n:
        return candidates

    scored: List[Tuple[float, float, DealCandidate]] = []
    for c in candidates:
        # tie-breaker용 deterministic random
        scored.append((_prescore_candidate(c, buyer_id=buyer_id, seller_id=seller_id, tick_ctx=tick_ctx, catalog=catalog), rng.random(), c))
    scored.sort(key=lambda x: (-x[0], x[1]))

    # 상위 일부는 고정, 나머지는 상위 풀에서 랜덤 추출
    n_fixed = max(2, cap_n // 2)
    fixed = [c for _, __, c in scored[:n_fixed]]

    pool = [c for _, __, c in scored[n_fixed : min(len(scored), n_fixed + cap_n * 3)]]
    rng.shuffle(pool)

    out = list(fixed)
    need = cap_n - len(out)
    if need > 0:
        out.extend(pool[:need])
    return out[:cap_n]

