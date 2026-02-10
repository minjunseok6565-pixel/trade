from __future__ import annotations

"""dealgen.core

Core generator orchestration methods.
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

from .types import DealGeneratorConfig, DealProposal, _BudgetTracker
from .utils import (
    _canon_team_id,
    _sigmoid,
    _deal_num_assets,
    _deal_num_players_moved,
    _deal_outgoing_pick_ids,
    _deal_incoming_pick_ids,
    _deal_assets_by_team,
    _protected_player_ids_from_meta,
    _hash_deal_for_dedupe,
    _safe_float,
    _team_posture,
    _team_time_horizon,
    _team_urgency,
    _team_deadline_pressure,
    _team_cooldown,
    _team_cap_space,
    _team_apron_status,
    _trade_rules,
    _trade_deadline_date,
    _resolve_receiver_team_id_2team,
)
from .need_fit import (
    _is_rebuildish,
    _is_young_candidate,
    _choose_top_need_tags,
    _team_need_map,
    _need_fit_score,
    _extract_fit_fail_tags,
    _extract_fit_failed_incoming_player_ids,
)
from .candidates import (
    _pick_from_buckets,
    _closest_salary_players,
    _player_asset,
    _pick_asset,
    _swap_asset,
    _remove_one_incoming_player,
    _add_one_outgoing_filler_player,
    _enforce_one_for_one_players,
)
from .salary import (
    _player_salary_amount_dollars,
    _estimate_team_payroll_after_dollars,
    _is_one_for_one_mode,
)
from .repair import _extract_rule_id


class _CoreMixin:
    def __init__(self, config: Optional[DealGeneratorConfig] = None):
        self.cfg = config or DealGeneratorConfig()

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def generate_for_team(
        self,
        team_id: str,
        tick_ctx: TradeGenerationTickContext,
        *,
        max_results: Optional[int] = None,
        allow_locked_by_deal_id: Optional[str] = None,
        rng_seed: Optional[int] = None,
    ) -> List[DealProposal]:
        """Generate candidate 2-team trade deals for a team.

        - If the team posture is BUY-ish, the team is treated as "buyer" (acquiring a target).
        - If the team posture is SELL-ish, the team is treated as "seller" (shopping a player).

        Always bounded by internal budgets.
        """
        tid = _canon_team_id(team_id)
        if not tid:
            return []

        deadline = _trade_deadline_date(tick_ctx)
        if deadline is not None and tick_ctx.current_date > deadline:
            return []

        ts = tick_ctx.get_team_situation(tid)
        if _team_cooldown(ts):
            return []

        posture = _team_posture(ts)
        urgency = _team_urgency(ts)
        deadline_pressure = _team_deadline_pressure(ts)

        # Stand pat -> return very few deals unless urgency is high.
        if posture == "STAND_PAT" and urgency < 0.45 and deadline_pressure < 0.45:
            # still allow tiny exploration for fun/market realism
            if urgency < 0.25 and deadline_pressure < 0.25:
                return []

        # Ensure catalog.
        catalog = getattr(tick_ctx, "asset_catalog", None)
        if catalog is None and self.cfg.build_catalog_if_missing:
            catalog = build_trade_asset_catalog(
                tick_ctx=tick_ctx,
                allow_locked_by_deal_id=allow_locked_by_deal_id,
            )
            try:
                tick_ctx.asset_catalog = catalog
            except Exception:
                pass
        if catalog is None:
            return []

        # Ensure active roster index exists for payroll/salary estimation helpers.
        try:
            rtc = getattr(tick_ctx, "rule_tick_ctx", None)
            if rtc is not None:
                rtc.ensure_active_roster_index()
        except Exception:
            pass

        # Deterministic RNG: date + team_id (+ optional seed override).
        seed = int(rng_seed) if rng_seed is not None else self._default_seed(tick_ctx.current_date, tid)
        rng = random.Random(seed)

        # Budgets
        budgets = self._compute_budgets(posture, urgency, deadline_pressure, max_results=max_results)
        max_results_eff = int(budgets["max_results"])

        budget = _BudgetTracker(
            max_validations=int(budgets["max_validations"]),
            max_evaluations=int(budgets["max_evaluations"]),
        )

        # Exploration state
        proposals: List[DealProposal] = []
        # Dedupe sets:
        # - seen_skeletons: pre-repair deals (cheap early pruning)
        # - seen_deals: final deals after repair/validation (prevents duplicates from different repair paths)
        seen_skeletons: Set[str] = set()
        seen_deals: Set[str] = set()
        opponent_seen: Dict[str, int] = {}
        target_seen: Dict[str, int] = {}
        hard_stop = False
        failures_by_rule: Dict[str, int] = {}

        # Select target pairs
        target_pairs = self._select_target_pairs(
            tid,
            tick_ctx=tick_ctx,
            catalog=catalog,
            posture=posture,
            urgency=urgency,
            budgets=budgets,
            rng=rng,
        )

        for pair in target_pairs:
            if hard_stop:
                break
            if budget.validations_used >= budget.max_validations or budget.evaluations_used >= budget.max_evaluations:
                break
            if len(proposals) >= max_results_eff:
                break

            buyer_id, seller_id, target_pid, tag_hint = pair

            # Build skeletons for this target.
            skeletons = self._build_offer_skeletons(
                buyer_id=buyer_id,
                seller_id=seller_id,
                target_player_id=target_pid,
                tag_hint=tag_hint,
                tick_ctx=tick_ctx,
                catalog=catalog,
                budgets=budgets,
                rng=rng,
            )

            attempts = 0
            per_target_proposals: List[DealProposal] = []
            for skel_deal, skel_tags in skeletons:
                if attempts >= budgets["max_attempts_per_target"]:
                    break
                if hard_stop:
                    break
                if budget.validations_used >= budget.max_validations or budget.evaluations_used >= budget.max_evaluations:
                    break

                attempts += 1
                deal = skel_deal
                tags_set: Set[str] = set(skel_tags)

                # Dedupe early (pre-repair)
                h_skel = _hash_deal_for_dedupe(deal, ignore_meta=self.cfg.dedupe_ignore_meta)
                if h_skel in seen_skeletons:
                    continue
                seen_skeletons.add(h_skel)

                # Repair loop: validate + minimal repairs
                deal_valid = False
                repairs_left = int(budgets["max_repairs"])
                while True:
                    if not budget.try_consume_validations(1):
                        hard_stop = True
                        deal_valid = False
                        break
                    try:
                        tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                        deal_valid = True
                        break
                    except TradeError as exc:
                        if exc.code == TRADE_DEADLINE_PASSED:
                            hard_stop = True
                            deal_valid = False
                            break
                        rule_id = _extract_rule_id(exc)
                        failures_by_rule[rule_id] = failures_by_rule.get(rule_id, 0) + 1

                        if repairs_left <= 0:
                            deal_valid = False
                            break
                        repairs_left -= 1

                        repaired = self._repair_until_valid(
                            deal,
                            exc,
                            buyer_id=buyer_id,
                            seller_id=seller_id,
                            target_player_id=target_pid,
                            tick_ctx=tick_ctx,
                            catalog=catalog,
                            budgets=budgets,
                            rng=rng,
                            tags_set=tags_set,
                        )
                        if not repaired:
                            deal_valid = False
                            break

                if not deal_valid:
                    continue

                # Final dedupe (post-repair / post-validation)
                h_final = _hash_deal_for_dedupe(deal, ignore_meta=self.cfg.dedupe_ignore_meta)
                if h_final in seen_deals:
                    continue
                seen_deals.add(h_final)

                # Asset count / player count sanity (avoid heavy packages)
                if _deal_num_assets(deal) > int(budgets["max_assets"]):
                    continue
                if _deal_num_players_moved(deal) > int(budgets["max_players_moved"]):
                    continue

                # Evaluate both teams (no validate; already valid)
                if not budget.try_consume_evaluations(2):
                    hard_stop = True
                    break
                try:
                    buyer_decision, buyer_eval = evaluate_deal_for_team(
                        deal,
                        buyer_id,
                        tick_ctx=tick_ctx,
                        include_breakdown=False,
                        validate=False,
                    )
                    seller_decision, seller_eval = evaluate_deal_for_team(
                        deal,
                        seller_id,
                        tick_ctx=tick_ctx,
                        include_breakdown=False,
                        validate=False,
                    )
                except Exception:
                    # valuation should be robust, but never crash generation
                    continue

                # Optional sweetener loop when "just a bit" short (seller reject most common)
                # NOTE: This stage can incur extra validate/eval calls. We must respect hard budgets here too.
                if (seller_decision.verdict.value if hasattr(seller_decision.verdict, "value") else str(seller_decision.verdict)) == "REJECT":
                    # If we can't afford a re-evaluation, don't attempt counter-style adjustments.
                    if budget.can_consume_evaluations(2):
                        # DecisionReason 기반 분기:
                        # - FIT_FAILS: picks로 때우기보다 "받는 선수"를 교체(플레이어 스왑)해 현실감 ↑
                        # - INSUFFICIENT_SURPLUS: 픽/스윗너로 미세조정
                        def _has_reason(dec: DealDecision, code: str) -> bool:
                            try:
                                for r in (dec.reasons or tuple()):
                                    if str(getattr(r, "code", "") or "") == code:
                                        return True
                            except Exception:
                                return False
                            return False

                        deal2: Optional[Deal] = None
                        if _has_reason(seller_decision, "FIT_FAILS"):
                            deal2 = self._try_swap_outgoing_player_for_fit(
                                base_deal=deal,
                                buyer_id=buyer_id,
                                seller_id=seller_id,
                                target_player_id=target_pid,
                                seller_decision=seller_decision,
                                tick_ctx=tick_ctx,
                                catalog=catalog,
                                budgets=budgets,
                                rng=rng,
                                allow_locked_by_deal_id=allow_locked_by_deal_id,
                                budget=budget,
                                tags_set=tags_set,
                            )

                        # If no fit swap (or not applicable), fall back to sweeteners (surplus short)
                        if deal2 is None and _has_reason(seller_decision, "INSUFFICIENT_SURPLUS") and not _has_reason(
                            seller_decision, "FIT_FAILS"
                        ):
                            deal2 = self._try_sweeteners(
                                base_deal=deal,
                                buyer_id=buyer_id,
                                seller_id=seller_id,
                                target_player_id=target_pid,
                                buyer_decision=buyer_decision,
                                buyer_eval=buyer_eval,
                                seller_decision=seller_decision,
                                seller_eval=seller_eval,
                                tick_ctx=tick_ctx,
                                catalog=catalog,
                                budgets=budgets,
                                rng=rng,
                                allow_locked_by_deal_id=allow_locked_by_deal_id,
                                seen_deals=seen_deals,
                                budget=budget,
                                tags_set=tags_set,
                            )
                        # Re-evaluate only if we can afford it.
                        # IMPORTANT: deal2 is a *new* deal. Gate it with dedupe + policy caps before committing.
                        h_deal2: Optional[str] = None
                        if deal2 is not None:
                            h_deal2 = _hash_deal_for_dedupe(deal2, ignore_meta=self.cfg.dedupe_ignore_meta)
                            if h_deal2 in seen_deals:
                                deal2 = None
                            elif _deal_num_assets(deal2) > int(budgets["max_assets"]):
                                deal2 = None
                            elif _deal_num_players_moved(deal2) > int(budgets["max_players_moved"]):
                                deal2 = None

                        if deal2 is not None:
                            if not budget.try_consume_evaluations(2):
                                hard_stop = True
                            else:
                                if h_deal2 is not None:
                                    seen_deals.add(h_deal2)
                                deal = deal2
                                try:
                                    buyer_decision, buyer_eval = evaluate_deal_for_team(
                                        deal,
                                        buyer_id,
                                        tick_ctx=tick_ctx,
                                        include_breakdown=False,
                                        validate=False,
                                    )
                                    seller_decision, seller_eval = evaluate_deal_for_team(
                                        deal,
                                        seller_id,
                                        tick_ctx=tick_ctx,
                                        include_breakdown=False,
                                        validate=False,
                                    )
                                except Exception:
                                    pass

                # Score
                score = self._score_deal(
                    deal,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    buyer_decision=buyer_decision,
                    seller_decision=seller_decision,
                    buyer_eval=buyer_eval,
                    seller_eval=seller_eval,
                    budgets=budgets,
                    opponent_seen=opponent_seen,
                    target_seen=target_seen,
                )
                tags = tuple(sorted(tags_set))

                # Add slight penalties for repetition to avoid market spam.
                score -= float(opponent_seen.get(seller_id, 0)) * float(self.cfg.opponent_repeat_penalty)
                score -= float(target_seen.get(target_pid, 0)) * float(self.cfg.target_repeat_penalty)

                prop = DealProposal(
                    deal=deal,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    buyer_decision=buyer_decision,
                    seller_decision=seller_decision,
                    buyer_eval=buyer_eval,
                    seller_eval=seller_eval,
                    score=float(score),
                    tags=tags,
                )
                per_target_proposals.append(prop)

                # Beam prune per target
                per_target_proposals.sort(key=lambda p: p.score, reverse=True)
                per_target_proposals = per_target_proposals[: int(budgets["beam_width"])]

            # Merge best from this target
            for p in per_target_proposals:
                proposals.append(p)
                opponent_seen[p.seller_id] = opponent_seen.get(p.seller_id, 0) + 1
                target_seen[target_pid] = target_seen.get(target_pid, 0) + 1

            proposals.sort(key=lambda p: p.score, reverse=True)
            proposals = proposals[: max_results_eff]

        # Optional: embed debug stats
        for p in proposals:
            try:
                meta = p.deal.meta or {}
                if isinstance(meta, dict):
                    meta.setdefault("dealgen", {})
                    if isinstance(meta["dealgen"], dict):
                        meta["dealgen"].setdefault("failures_by_rule", dict(failures_by_rule))
                        meta["dealgen"].setdefault("seed", seed)
            except Exception:
                pass

        return proposals

    # ---------------------------------------------------------------------
    # Budgeting
    # ---------------------------------------------------------------------
    def _default_seed(self, current_date: date, team_id: str) -> int:
        base = f"{current_date.isoformat()}::{_canon_team_id(team_id)}::{int(self.cfg.rng_salt)}"
        # IMPORTANT: don't use Python's built-in hash(); it is randomized per process.
        return int(hashlib.sha1(base.encode("utf-8")).hexdigest(), 16) % (2**31 - 1)

    def _compute_budgets(
        self,
        posture: str,
        urgency: float,
        deadline_pressure: float,
        *,
        max_results: Optional[int],
    ) -> Dict[str, int]:
        p = str(posture or "").upper()
        u = float(urgency)
        d = float(deadline_pressure)

        # posture base scaling
        if p == "AGGRESSIVE_BUY":
            t_scale = 1.35
            beam = 1.25
        elif p == "SOFT_BUY":
            t_scale = 1.05
            beam = 1.05
        elif p in {"SELL", "SOFT_SELL"}:
            t_scale = 1.10
            beam = 1.00
        else:  # STAND_PAT / unknown
            t_scale = 0.50
            beam = 0.80

        # urgency/deadline scaling (bounded)
        factor = 0.85 + 0.65 * max(0.0, min(1.0, u)) + 0.40 * max(0.0, min(1.0, d))
        factor = max(0.40, min(2.00, factor))

        max_targets = int(round(self.cfg.base_max_targets * t_scale * factor))
        beam_width = int(round(self.cfg.base_beam_width * beam * (0.85 + 0.35 * factor)))
        max_attempts_per_target = int(round(self.cfg.base_max_attempts_per_target * (0.80 + 0.40 * factor)))
        max_repairs = int(round(self.cfg.base_max_repairs))

        # hard caps
        max_targets = max(0, min(int(self.cfg.max_targets_hard_cap), max_targets))
        beam_width = max(2, min(22, beam_width))
        max_attempts_per_target = max(12, min(int(self.cfg.max_attempts_per_target_hard_cap), max_attempts_per_target))
        max_repairs = max(0, min(3, max_repairs))

        max_assets = int(self.cfg.base_max_assets)
        max_players_moved = int(self.cfg.base_max_players_moved)
        max_validations = int(min(self.cfg.max_validations_hard_cap, 160 + max_targets * 16))
        max_evaluations = int(min(self.cfg.max_evaluations_hard_cap, 120 + max_targets * 12))
        skeletons_per_target = int(max(2, min(8, self.cfg.base_skeletons_per_target)))

        mr = int(max_results) if max_results is not None else int(self.cfg.max_results_default)
        mr = max(1, min(40, mr))

        return {
            "max_results": mr,
            "max_targets": max_targets,
            "beam_width": beam_width,
            "max_attempts_per_target": max_attempts_per_target,
            "max_repairs": max_repairs,
            "max_assets": max_assets,
            "max_players_moved": max_players_moved,
            "max_validations": max_validations,
            "max_evaluations": max_evaluations,
            "skeletons_per_target": skeletons_per_target,
        }
