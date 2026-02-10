from __future__ import annotations

"""dealgen.counter

Sweetener and fit-swap loops mixin.
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

from .types import _BudgetTracker
from .utils import (
    _canon_team_id,
    _deal_num_assets,
    _deal_num_players_moved,
    _hash_deal_for_dedupe,
    _protected_player_ids_from_meta,
    _team_posture,
    _team_time_horizon,
)
from .need_fit import (
    _extract_fit_failed_incoming_player_ids,
    _is_rebuildish,
    _team_need_map,
    _need_fit_score,
)
from .candidates import (
    _pick_from_buckets,
    _player_asset,
)


class _CounterMixin:
    def _try_sweeteners(
        self,
        *,
        base_deal: Deal,
        buyer_id: str,
        seller_id: str,
        target_player_id: str,
        buyer_decision: DealDecision,
        buyer_eval: TeamDealEvaluation,
        seller_decision: DealDecision,
        seller_eval: TeamDealEvaluation,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
        allow_locked_by_deal_id: Optional[str],
        seen_deals: Set[str],
        budget: _BudgetTracker,
        tags_set: Optional[Set[str]] = None,
    ) -> Optional[Deal]:
        """If seller is slightly short, add 1-2 sweeteners and re-validate.

        Returns a *new* deal if improved, else None.
        """
        # Only when seller is close (performance + realism)
        seller_margin = float(seller_eval.net_surplus) - float(seller_decision.required_surplus)
        if seller_margin >= 0.0:
            return None
        seller_scale = max(float(seller_eval.outgoing_total), 6.0)
        sweetener_close = min(
            float(self.cfg.sweetener_close_cap),
            max(float(self.cfg.sweetener_close_floor), float(self.cfg.sweetener_close_corridor_ratio) * seller_scale),
        )
        if seller_margin < -sweetener_close:
            return None

        max_seconds = int(self.cfg.max_second_rounders_as_sweetener)
        allow_swaps = bool(self.cfg.allow_swaps_as_sweetener)

        # Sweetener order
        actions: List[Tuple[str, str]] = [("pick", "SECOND")]
        if allow_swaps:
            actions.append(("swap", "SWAP"))
        actions.append(("pick", "FIRST_SAFE"))
        actions.append(("pick", "SECOND"))
        if self.cfg.allow_first_sensitive_as_last_resort:
            actions.append(("pick", "FIRST_SENSITIVE"))

        seconds_added = 0
        scratch_tags: Set[str] = set()
        deal = Deal(teams=list(base_deal.teams), legs={k: list(v) for k, v in base_deal.legs.items()}, meta=dict(base_deal.meta or {}))
        protected = _protected_player_ids_from_meta(deal)
        local_seen: Set[str] = set()  # local pre-validate dedupe for sweetener exploration

        max_assets = int(budgets.get("max_assets", 99_999))
        max_players_moved = int(budgets.get("max_players_moved", 99_999))

        # attempt up to 2 additions
        added = 0
        for kind, bucket in actions:
            if added >= 2:
                break

            # If we're already at/over caps, don't try to add more assets.
            if _deal_num_assets(deal) >= max_assets:
                break
            if _deal_num_players_moved(deal) > max_players_moved:
                return None

            if kind == "pick" and bucket == "SECOND" and seconds_added >= max_seconds:
                continue

            # Snapshot state for this attempt (rollback on failure/duplicate/oversize)
            legs_before = {k: list(v) for k, v in (deal.legs or {}).items()}
            meta_before = dict(deal.meta or {})

            changed = False
            attempt_tags: Set[str] = set()
            tag_added: Optional[str] = None

            if kind == "pick":
                changed = self._add_pick_by_bucket(deal, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket=bucket)
                if changed:
                    tag_added = f"sweetener:{bucket}"
            elif kind == "swap":
                changed = self._add_swap_sweetener(deal, from_team=buyer_id, to_team=seller_id, catalog=catalog)
                if changed:
                    tag_added = "sweetener:SWAP"

            if tag_added:
                attempt_tags.add(tag_added)

            if not changed:
                continue

            # dedupe (local pre-check; rollback if duplicate)
            h_pre = _hash_deal_for_dedupe(deal, ignore_meta=self.cfg.dedupe_ignore_meta)
            if h_pre in local_seen:
                deal.legs = legs_before
                deal.meta = meta_before
                continue
            local_seen.add(h_pre)

            # validate with minimal repair if needed (counts attempted validations regardless of outcome)
            if not budget.try_consume_validations(1):
                deal.legs = legs_before
                deal.meta = meta_before
                return None
            try:
                tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
            except TradeError as exc:
                if exc.code == TRADE_DEADLINE_PASSED:
                    deal.legs = legs_before
                    deal.meta = meta_before
                    return None
                # small repair attempt
                rep = self._repair_until_valid(
                    deal,
                    exc,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    target_player_id=target_player_id,
                    tick_ctx=tick_ctx,
                    catalog=catalog,
                    budgets=budgets,
                    rng=rng,
                    tags_set=attempt_tags,
                )
                if not rep:
                    deal.legs = legs_before
                    deal.meta = meta_before
                    continue
                if not budget.try_consume_validations(1):
                    deal.legs = legs_before
                    deal.meta = meta_before
                    return None
                try:
                    tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                except Exception:
                    deal.legs = legs_before
                    deal.meta = meta_before
                    continue
            except Exception:
                deal.legs = legs_before
                deal.meta = meta_before
                continue

            # Enforce package caps after repair/validation (sweeteners can push us over).
            if _deal_num_assets(deal) > max_assets:
                deal.legs = legs_before
                deal.meta = meta_before
                continue
            if _deal_num_players_moved(deal) > max_players_moved:
                deal.legs = legs_before
                deal.meta = meta_before
                continue

            # Global dedupe after repair/validation (prevents duplicates from repair paths)
            h_final = _hash_deal_for_dedupe(deal, ignore_meta=self.cfg.dedupe_ignore_meta)
            if h_final in seen_deals:
                deal.legs = legs_before
                deal.meta = meta_before
                continue

            # Only now commit the attempt's tags; failed attempts should not pollute tags_set.
            scratch_tags.update(attempt_tags)
            added += 1
            if kind == "pick" and bucket == "SECOND":
                seconds_added += 1

            # early exit if we've added something meaningful
            if added >= 1 and rng.random() < 0.60:
                break
        if added <= 0:
            return None
        # keep protected meta
        if deal.meta is not None and isinstance(deal.meta, dict):
            deal.meta["protected_player_ids"] = list(sorted(protected))
        if tags_set is not None:
            tags_set.update(scratch_tags)
        return deal

    def _try_swap_outgoing_player_for_fit(
        self,
        *,
        base_deal: Deal,
        buyer_id: str,
        seller_id: str,
        target_player_id: str,
        seller_decision: Any,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
        allow_locked_by_deal_id: Optional[str],
        budget: _BudgetTracker,
        tags_set: Optional[Set[str]] = None,
    ) -> Optional[Deal]:
        """If seller rejects due to FIT_FAILS, try swapping a buyer outgoing player to better fit seller needs.

        - Keep salary roughly similar to reduce salary-matching churn.
        - Validate and do at most one minimal repair.
        """
        buyer_id = _canon_team_id(buyer_id)
        seller_id = _canon_team_id(seller_id)

        seller_need_map = _team_need_map(tick_ctx, seller_id)
        if not seller_need_map:
            return None

        # Incoming player ids that failed fit (DecisionPolicy FIT_FAILS meta)
        failed_pids = _extract_fit_failed_incoming_player_ids(seller_decision)

        # Seller horizon / rebuildness to adjust weighting
        seller_ts = tick_ctx.get_team_situation(seller_id)
        horizon = str(_team_time_horizon(seller_ts) or "").upper()
        rebuild_like = _is_rebuildish(seller_ts) or horizon in {"REBUILD", "RE_TOOL", "RETOOL"}
        win_now_like = horizon in {"WIN_NOW", "CONTEND", "COMPETE"} or str(_team_posture(seller_ts) or "").upper() in {
            "AGGRESSIVE_BUY",
            "SOFT_BUY",
        }

        # Only if there is at least one outgoing player from buyer to seller
        buyer_leg = list(base_deal.legs.get(buyer_id, []) or [])
        outgoing_players = [a for a in buyer_leg if isinstance(a, PlayerAsset)]
        if not outgoing_players:
            return None

        protected = _protected_player_ids_from_meta(base_deal) | {str(target_player_id)}

        buyer_out = catalog.outgoing_by_team.get(buyer_id)
        if buyer_out is None:
            return None

        # Compute current fit for each outgoing player to seller; prefer swapping players that actually failed fit.
        def _fit_pid(pid: str) -> float:
            c = buyer_out.players.get(pid)
            if c is None:
                return 0.0
            supply = getattr(c, "supply", None) or {}
            return float(_need_fit_score(supply, seller_need_map))

        outgoing_pids = [str(a.player_id) for a in outgoing_players]
        replace_candidates = [pid for pid in outgoing_pids if pid in set(failed_pids or set()) and pid not in protected]

        worst_pid: Optional[str] = None
        worst_fit = 1e9
        worst_salary = 0.0

        scan_pids = replace_candidates if replace_candidates else [pid for pid in outgoing_pids if pid not in protected]
        for pid in scan_pids:
            f = _fit_pid(pid)
            if f < worst_fit:
                worst_fit = f
                worst_pid = pid
                c = buyer_out.players.get(pid)
                worst_salary = float(c.salary_m) if c else 0.0

        if worst_pid is None:
            return None

        # Candidate replacements: prefer receiver fit, keep salary close
        exclude = {str(a.player_id) for a in outgoing_players} | set(protected)
        pool = _pick_from_buckets(
            buyer_out,
            buckets=("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "CONSOLIDATE", "EXPIRING", "FILLER_CHEAP"),
            exclude_players=exclude,
            to_team=seller_id,
            max_n=16,
            prefer_low_market=False,
            receiver_need_map=seller_need_map,
        )

        if not pool:
            return None

        # rank by horizon-aware primary score, then salary closeness, then market realism
        def _age_years(c: PlayerTradeCandidate) -> Tuple[Optional[float], float]:
            age = None
            try:
                snap = getattr(c, "snap", None)
                if snap is not None and getattr(snap, "age", None) is not None:
                    age = float(getattr(snap, "age"))
            except Exception:
                age = None
            try:
                ry = float(getattr(c, "remaining_years", 0.0) or 0.0)
            except Exception:
                ry = 0.0
            return age, ry

        def _fit_score(c: PlayerTradeCandidate) -> float:
            supply = getattr(c, "supply", None) or {}
            return float(_need_fit_score(supply, seller_need_map))

        def _primary_score(c: PlayerTradeCandidate) -> float:
            fit = _fit_score(c)
            market = float(c.market.total)
            market_norm = market / 50.0  # scale helper (rough)
            age, ry = _age_years(c)
            youth = 0.0
            if age is not None:
                youth += max(0.0, 30.0 - float(age)) / 10.0
            youth += min(4.0, max(0.0, float(ry))) / 4.0

            if rebuild_like:
                # rebuild/retool: youth/years, then fit; avoid over-weighting pure market value
                return float(0.55 * youth + 0.40 * fit - 0.05 * market_norm)
            if win_now_like:
                # win-now: fit + immediate quality
                return float(0.70 * fit + 0.25 * market_norm + 0.05 * youth)
            # neutral: balanced
            return float(0.60 * fit + 0.20 * market_norm + 0.20 * youth)

        ranked: List[Tuple[float, float, float, str, float]] = []
        for c in pool:
            # aggregation solo-only cannot be aggregated with others.
            if bool(c.aggregation_solo_only) and len(outgoing_players) >= 2:
                continue
            fit = _fit_score(c)
            primary = _primary_score(c)
            ranked.append((float(primary), abs(float(c.salary_m) - float(worst_salary)), float(c.market.total), c.player_id, fit))
        ranked.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))

        # Try a few top replacements (bounded)
        for primary, _, __, new_pid, new_fit in ranked[:6]:
            # require meaningful fit improvement
            if float(new_fit) <= float(worst_fit) + 0.03:
                continue

            new_deal = Deal(
                teams=list(base_deal.teams),
                legs={k: list(v) for k, v in base_deal.legs.items()},
                meta=dict(base_deal.meta or {}),
            )
            leg = list(new_deal.legs.get(buyer_id, []) or [])
            replaced = False
            for i in range(len(leg)):
                if isinstance(leg[i], PlayerAsset) and str(leg[i].player_id) == str(worst_pid):
                    leg[i] = _player_asset(str(new_pid))
                    replaced = True
                    break
            if not replaced:
                continue
            new_deal.legs[buyer_id] = leg

            attempt_tags: Set[str] = set()

            # validate + optional minimal repair (counts attempted validations regardless of outcome)
            if not budget.try_consume_validations(1):
                return None
            try:
                tick_ctx.validate_deal(new_deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                if tags_set is not None:
                    tags_set.update(attempt_tags)
                    tags_set.add("repair:fit_swap")
                return new_deal
            except TradeError as exc:
                if exc.code == TRADE_DEADLINE_PASSED:
                    return None
                rep = self._repair_until_valid(
                    new_deal,
                    exc,
                    buyer_id=buyer_id,
                    seller_id=seller_id,
                    target_player_id=target_player_id,
                    tick_ctx=tick_ctx,
                    catalog=catalog,
                    budgets=budgets,
                    rng=rng,
                    tags_set=attempt_tags,
                )
                if not rep:
                    continue
                if not budget.try_consume_validations(1):
                    return None
                try:
                    tick_ctx.validate_deal(new_deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                    if tags_set is not None:
                        tags_set.update(attempt_tags)
                        tags_set.add("repair:fit_swap")
                    return new_deal
                except Exception:
                    continue
            except Exception:
                continue

        return None
