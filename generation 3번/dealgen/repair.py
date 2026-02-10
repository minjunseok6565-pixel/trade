from __future__ import annotations

"""dealgen.repair

Validate-failure-driven repair mixin and rule-id extraction.
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

from .utils import (
    _canon_team_id,
    _protected_player_ids_from_meta,
)
from .candidates import (
    _remove_one_incoming_player,
    _add_one_outgoing_filler_player,
    _enforce_one_for_one_players,
    _player_asset,
)
from .salary import (
    _player_salary_amount_dollars,
    _is_one_for_one_mode,
)


class _RepairMixin:
    def _try_swap_outgoing_player_for_salary(
        self,
        deal: Deal,
        *,
        from_team: str,
        to_team: str,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        protected_players: Set[str],
        needed_add_salary_m: Optional[float],
    ) -> bool:
        """In one-for-one mode, prefer swapping the outgoing player to a higher-salary option.

        This avoids invalid attempts where the counterparty (or the failing team itself) is
        effectively restricted to 1-for-1 due to being at/over the 2nd apron.
        """
        from_team_u = _canon_team_id(from_team)
        to_team_u = _canon_team_id(to_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False

        leg = list(deal.legs.get(from_team_u, []) or [])
        outgoing_pids = [str(a.player_id) for a in leg if isinstance(a, PlayerAsset)]
        if not outgoing_pids:
            return False

        # choose a replaceable outgoing player (lowest salary, non-protected)
        replace_pid = None
        replace_sal = None
        for pid in outgoing_pids:
            if pid in protected_players:
                continue
            sal = _player_salary_amount_dollars(pid, from_team=from_team_u, tick_ctx=tick_ctx, catalog=catalog)
            if replace_sal is None or float(sal) < float(replace_sal):
                replace_sal = float(sal)
                replace_pid = str(pid)
        if replace_pid is None or replace_sal is None:
            return False

        # desired salary: increase by estimated gap (if available)
        gap_dollars = 0.0
        try:
            if needed_add_salary_m is not None:
                gap_dollars = max(0.0, float(needed_add_salary_m)) * 1_000_000.0
        except Exception:
            gap_dollars = 0.0
        desired = float(replace_sal) + float(gap_dollars)

        # Build replacement pool from non-core buckets.
        buckets = (
            "FILLER_BAD_CONTRACT",
            "EXPIRING",
            "CONSOLIDATE",
            "VETERAN_SALE",
            "SURPLUS_REDUNDANT",
            "SURPLUS_LOW_FIT",
            "FILLER_CHEAP",
        )

        exclude = set(outgoing_pids) | set(protected_players)
        candidates: List[Tuple[float, float, str]] = []  # (salary_dollars, market, pid)
        seen: Set[str] = set()
        for b in buckets:
            for pid in (outcat.player_ids_by_bucket.get(b, tuple()) or tuple()):
                pid_s = str(pid)
                if pid_s in seen or pid_s in exclude:
                    continue
                seen.add(pid_s)
                c = outcat.players.get(pid_s)
                if c is None:
                    continue
                # return-to-team ban
                if to_team_u and to_team_u in {str(t).upper() for t in (c.return_ban_teams or tuple())}:
                    continue
                sal = _player_salary_amount_dollars(pid_s, from_team=from_team_u, tick_ctx=tick_ctx, catalog=catalog)
                if float(sal) <= float(replace_sal) + 1.0:
                    continue
                candidates.append((float(sal), float(c.market.total), pid_s))

        if not candidates:
            return False

        def _key(item: Tuple[float, float, str]) -> Tuple[int, float, float, str]:
            sal, market, pid_s = item
            miss = 0 if float(sal) >= float(desired) else 1
            return (miss, abs(float(sal) - float(desired)), float(market), str(pid_s))

        candidates.sort(key=_key)
        new_pid = candidates[0][2]

        changed = False
        for i, a in enumerate(leg):
            if isinstance(a, PlayerAsset) and str(a.player_id) == str(replace_pid):
                leg[i] = _player_asset(str(new_pid))
                changed = True
                break
        if not changed:
            return False

        deal.legs[from_team_u] = leg
        return True

    def _try_add_one_for_one_salary_match_player(
        self,
        deal: Deal,
        *,
        from_team: str,
        to_team: str,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        protected_players: Set[str],
        needed_out_salary_m: Optional[float],
    ) -> bool:
        """In one-for-one mode, add a single outgoing player that best closes salary-matching.

        This is used when a team has *zero* outgoing players but is restricted to 1-for-1
        due to being at/over the 2nd apron (or will become restricted after the deal).
        """
        from_team_u = _canon_team_id(from_team)
        to_team_u = _canon_team_id(to_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False

        leg = list(deal.legs.get(from_team_u, []) or [])
        if any(isinstance(a, PlayerAsset) for a in leg):
            # must remain one outgoing player max
            return False

        # Desired salary in dollars.
        desired_dollars = 0.0
        try:
            if needed_out_salary_m is not None:
                desired_dollars = max(0.0, float(needed_out_salary_m)) * 1_000_000.0
        except Exception:
            desired_dollars = 0.0

        buckets = (
            "CONSOLIDATE",
            "VETERAN_SALE",
            "FILLER_BAD_CONTRACT",
            "EXPIRING",
            "SURPLUS_REDUNDANT",
            "SURPLUS_LOW_FIT",
            "FILLER_CHEAP",
        )

        exclude = set(protected_players)
        candidates: List[Tuple[float, float, str]] = []  # (salary_dollars, market, pid)
        seen: Set[str] = set()
        for b in buckets:
            for pid in (outcat.player_ids_by_bucket.get(b, tuple()) or tuple()):
                pid_s = str(pid)
                if pid_s in seen or pid_s in exclude:
                    continue
                seen.add(pid_s)
                c = outcat.players.get(pid_s)
                if c is None:
                    continue
                # return-to-team ban
                if to_team_u and any(_canon_team_id(t) == to_team_u for t in (c.return_ban_teams or tuple())):
                    continue
                sal = _player_salary_amount_dollars(pid_s, from_team=from_team_u, tick_ctx=tick_ctx, catalog=catalog)
                if float(sal) <= 0.0:
                    continue
                candidates.append((float(sal), float(c.market.total), pid_s))

        if not candidates:
            return False

        def _key(item: Tuple[float, float, str]) -> Tuple[int, float, float, str]:
            sal, market, pid_s = item
            # prefer meeting/exceeding the desired salary to satisfy matching;
            # then closest salary; then lowest market to keep realism.
            miss = 0 if float(sal) >= float(desired_dollars) else 1
            return (miss, abs(float(sal) - float(desired_dollars)), float(market), str(pid_s))

        candidates.sort(key=_key)
        pick_pid = candidates[0][2]
        deal.legs[from_team_u] = leg + [_player_asset(str(pick_pid))]
        return True

    def _repair_until_valid(
        self,
        deal: Deal,
        exc: TradeError,
        *,
        buyer_id: str,
        seller_id: str,
        target_player_id: str,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
        tags_set: Optional[Set[str]] = None,
    ) -> bool:
        """Try minimal repair based on TradeError details.

        Returns True if deal was modified (repair attempted), else False.
        """
        details = exc.details if isinstance(exc.details, Mapping) else {}
        rule = str(details.get("rule") or "").strip() or _extract_rule_id(exc)
        rule = str(rule or "").strip() or "unknown"

        protected = _protected_player_ids_from_meta(deal) | {str(target_player_id)}

        def _salary_to_m(x: Any) -> Optional[float]:
            try:
                if x is None:
                    return None
                v = float(x)
                if v <= 0:
                    return 0.0
                # if looks like dollars, convert to millions
                if v >= 100000.0:
                    return v / 1_000_000.0
                return v
            except Exception:
                return None

        def _estimate_needed_filler_salary_m(d: Mapping[str, Any]) -> Optional[float]:
            """Estimate additional outgoing salary (in millions) needed for the failing team."""
            inc = _salary_to_m(d.get("incoming_salary"))
            allowed = _salary_to_m(d.get("allowed_in"))
            out = _salary_to_m(d.get("outgoing_salary"))
            method = str(d.get("method") or "")
            if inc is None:
                return None
            if allowed is None:
                # "outgoing_required" case or missing: make a conservative guess
                return max(0.5, 0.40 * float(inc))
            delta = max(0.0, float(inc) - float(allowed))
            if delta <= 0.0:
                return None
            # Infer slope from allowed/outgoing ratio when possible
            slope = 1.0
            if out and float(out) > 0.0 and float(allowed) > 0.0:
                ratio = float(allowed) / float(out)
                if ratio >= 1.75:
                    slope = 2.0
                elif ratio >= 1.20:
                    slope = 1.25
                elif ratio >= 1.05:
                    slope = max(1.0, ratio)
            # additional outgoing needed ~= delta / slope
            need = float(delta) / float(slope)
            # bound to avoid extreme filler grabs
            return max(0.25, min(25.0, need))

        # 1) salary matching
        if rule == "salary_matching" or (exc.code == DEAL_INVALIDATED and str(details.get("rule")) == "salary_matching"):
            team_fail = _canon_team_id(details.get("team_id") or "")
            method = str(details.get("method") or "")
            status = str(details.get("status") or "")
            # second apron one-for-one: enforce both legs <= 1 player
            if method == "second_apron_one_for_one":
                modified = _enforce_one_for_one_players(deal, protected_players=protected, catalog=catalog)
                if modified and tags_set is not None:
                    tags_set.add("repair:one_for_one")
                return modified

            # If either side is predicted to be 2nd-apron one-for-one after this deal, avoid multi-player legs.
            try:
                if (
                    _is_one_for_one_mode(deal=deal, team_id=buyer_id, tick_ctx=tick_ctx, catalog=catalog)
                    or _is_one_for_one_mode(deal=deal, team_id=seller_id, tick_ctx=tick_ctx, catalog=catalog)
                ):
                    # Any leg sending 2+ players will violate one-for-one (outgoing>1 and counterparty incoming>1).
                    if any(
                        sum(1 for a in (deal.legs.get(t, []) or []) if isinstance(a, PlayerAsset)) > 1
                        for t in deal.teams
                    ):
                        modified = _enforce_one_for_one_players(deal, protected_players=protected, catalog=catalog)
                        if modified:
                            if tags_set is not None:
                                tags_set.add("repair:one_for_one")
                            return True
            except Exception:
                pass

            # Otherwise: repair around failing team.
            if team_fail:
                other = seller_id if team_fail == buyer_id else buyer_id
                gap_m = _estimate_needed_filler_salary_m(details)

                # Decide whether we should treat either side as 2nd-apron one-for-one for this deal.
                one_for_one_fail = False
                one_for_one_other = False
                try:
                    one_for_one_fail = _is_one_for_one_mode(deal=deal, team_id=team_fail, tick_ctx=tick_ctx, catalog=catalog)
                    one_for_one_other = _is_one_for_one_mode(deal=deal, team_id=other, tick_ctx=tick_ctx, catalog=catalog)
                except Exception:
                    one_for_one_fail = False
                    one_for_one_other = False

                # Respect one-for-one restriction: never add a 2nd outgoing player when either side is restricted.
                max_out_players = 1 if (one_for_one_fail or one_for_one_other or status == "SECOND_APRON") else 4

                out_players = [a for a in (deal.legs.get(team_fail, []) or []) if isinstance(a, PlayerAsset)]

                # One-for-one restriction: can't add extra outgoing players.
                if max_out_players <= 1:
                    if len(out_players) == 0:
                        # In one-for-one mode with no outgoing player yet, add a single higher-salary outgoing.
                        added_one = self._try_add_one_for_one_salary_match_player(
                            deal,
                            from_team=team_fail,
                            to_team=other,
                            tick_ctx=tick_ctx,
                            catalog=catalog,
                            protected_players=set(protected),
                            needed_out_salary_m=gap_m,
                        )
                        if added_one:
                            if tags_set is not None:
                                tags_set.add("repair:one_for_one")
                                tags_set.add("repair:salary_filler")
                            return True
                        trimmed = _remove_one_incoming_player(
                            deal,
                            receiver_team=team_fail,
                            protected_players=protected,
                            prefer_remove_high_salary=True,
                            catalog=catalog,
                        )
                        if trimmed and tags_set is not None:
                            tags_set.add("repair:salary_trim_incoming")
                        return bool(trimmed)

                    # Already has 1 outgoing player: try to swap up salary.
                    swapped = self._try_swap_outgoing_player_for_salary(
                        deal,
                        from_team=team_fail,
                        to_team=other,
                        tick_ctx=tick_ctx,
                        catalog=catalog,
                        protected_players=set(protected),
                        needed_add_salary_m=gap_m,
                    )
                    if swapped:
                        if tags_set is not None:
                            tags_set.add("repair:salary_swap")
                        return True

                    trimmed = _remove_one_incoming_player(
                        deal,
                        receiver_team=team_fail,
                        protected_players=protected,
                        prefer_remove_high_salary=True,
                        catalog=catalog,
                    )
                    if trimmed and tags_set is not None:
                        tags_set.add("repair:salary_trim_incoming")
                    return bool(trimmed)

                # If the gap is big, swapping outgoing salary can be higher-impact than adding tiny cheap fillers.
                try:
                    if len(out_players) >= 1 and gap_m is not None and float(gap_m) >= 7.5:
                        swapped = self._try_swap_outgoing_player_for_salary(
                            deal,
                            from_team=team_fail,
                            to_team=other,
                            tick_ctx=tick_ctx,
                            catalog=catalog,
                            protected_players=set(protected),
                            needed_add_salary_m=gap_m,
                        )
                        if swapped:
                            if tags_set is not None:
                                tags_set.add("repair:salary_swap")
                            return True
                except Exception:
                    pass

                # Default: add outgoing filler on the failing team
                added = _add_one_outgoing_filler_player(
                    deal,
                    from_team=team_fail,
                    to_team=other,
                    catalog=catalog,
                    exclude_players=set(protected),
                    max_outgoing_players=max_out_players,
                    target_add_salary_m=gap_m,
                )
                if added:
                    if tags_set is not None:
                        tags_set.add("repair:salary_filler")
                    return True

                trimmed = _remove_one_incoming_player(
                    deal,
                    receiver_team=team_fail,
                    protected_players=protected,
                    prefer_remove_high_salary=True,
                    catalog=catalog,
                )
                if trimmed and tags_set is not None:
                    tags_set.add("repair:salary_trim_incoming")
                return bool(trimmed)

            # fallback: try remove one incoming player (non-target)
            trimmed = _remove_one_incoming_player(
                deal,
                receiver_team=buyer_id,
                protected_players=protected,
                prefer_remove_high_salary=True,
                catalog=catalog,
            )
            if trimmed and tags_set is not None:
                tags_set.add("repair:salary_trim_incoming")
            return bool(trimmed)

        # 2) roster limit: reduce incoming players or increase outgoing
        if exc.code == ROSTER_LIMIT or rule == "roster_limit":
            team_fail = _canon_team_id(details.get("team_id") or "")
            if not team_fail:
                team_fail = buyer_id
            # Prefer removing an incoming (non-target) player
            removed = _remove_one_incoming_player(
                deal,
                receiver_team=team_fail,
                protected_players=protected,
                prefer_remove_high_salary=False,
                catalog=catalog,
            )
            if removed:
                if tags_set is not None:
                    tags_set.add("repair:roster_trim_incoming")
                return True
            # else add outgoing filler from team_fail
            other = seller_id if team_fail == buyer_id else buyer_id
            added = _add_one_outgoing_filler_player(
                deal,
                from_team=team_fail,
                to_team=other,
                catalog=catalog,
                exclude_players=set(protected),
                max_outgoing_players=4,
            )
            if added and tags_set is not None:
                tags_set.add("repair:roster_add_outgoing")
            return bool(added)

        # 3) locks/eligibility/return bans/duplicate assets -> prune (no repair)
        if exc.code in {ASSET_LOCKED, DUPLICATE_ASSET}:
            return False

        if rule in {"player_eligibility", "asset_lock", "duplicate_asset", "return_to_trading_team_same_season"}:
            return False

        if rule in {"pick_rules", "ownership", "pick_protection_schema"}:
            # Prefer repairing by replacing/downgrading the last-added pick/swap, rather than just deleting.
            reason = str(details.get("reason") or details.get("subrule") or details.get("code") or "").lower()
            is_stepien = "stepien" in reason or "stepian" in reason or bool(details.get("stepien_violation"))

            found: Optional[Tuple[str, int, List[Any], Any]] = None
            for team in (buyer_id, seller_id):
                leg = list(deal.legs.get(team, []) or [])
                for i in range(len(leg) - 1, -1, -1):
                    if isinstance(leg[i], (PickAsset, SwapAsset)):
                        found = (team, i, leg, leg[i])
                        break
                if found:
                    break
            if not found:
                return False

            team, idx, leg, asset = found
            team_u = _canon_team_id(team)
            other = seller_id if team_u == buyer_id else buyer_id
            outcat = catalog.outgoing_by_team.get(team_u)

            # Remove the problematic asset first, then try to replace.
            leg.pop(idx)
            deal.legs[team_u] = leg

            # If it was a swap, prefer replacing with a SECOND pick.
            if isinstance(asset, SwapAsset):
                ok = self._add_pick_by_bucket(deal, from_team=team_u, to_team=other, catalog=catalog, bucket="SECOND")
                if tags_set is not None:
                    tags_set.add("repair:pick_downgrade" if ok else "repair:pick_remove")
                return True

            # Pick replacement/downgrade logic.
            old_pid = str(getattr(asset, "pick_id", "") or "")
            bucket_map: Dict[str, str] = {}
            if outcat is not None:
                for b, ids in (outcat.pick_ids_by_bucket or {}).items():
                    for pid in (ids or tuple()):
                        bucket_map[str(pid)] = str(b)
            old_bucket = bucket_map.get(old_pid, "")

            # 1) If not Stepien, try swapping to a different pick in the *same* bucket.
            if outcat is not None and old_bucket and (not is_stepien):
                ok = self._add_pick_by_bucket(
                    deal,
                    from_team=team_u,
                    to_team=other,
                    catalog=catalog,
                    bucket=old_bucket,
                    exclude_pick_ids={old_pid},
                )
                if ok:
                    if tags_set is not None:
                        tags_set.add("repair:pick_downgrade")
                    return True

            # 2) Downgrade chain: FIRST_SENSITIVE -> FIRST_SAFE -> SECOND, FIRST_SAFE -> SECOND.
            chain: List[str] = []
            if old_bucket == "FIRST_SENSITIVE":
                chain = ["FIRST_SAFE", "SECOND"]
            elif old_bucket == "FIRST_SAFE":
                chain = ["SECOND"]
            elif old_bucket and old_bucket != "SECOND":
                chain = ["SECOND"]

            for b in chain:
                if outcat is None:
                    break
                ok = self._add_pick_by_bucket(
                    deal,
                    from_team=team_u,
                    to_team=other,
                    catalog=catalog,
                    bucket=b,
                    exclude_pick_ids={old_pid},
                )
                if ok:
                    if tags_set is not None:
                        tags_set.add("repair:pick_downgrade")
                    return True

            # 3) If no replacement was possible, keep the removal.
            if tags_set is not None:
                tags_set.add("repair:pick_remove")
            return True

        return False


def _extract_rule_id(exc: TradeError) -> str:
    details = exc.details if isinstance(exc.details, Mapping) else {}
    rule = details.get("rule")
    if isinstance(rule, str) and rule.strip():
        return rule.strip()
    # Fallback by code
    if exc.code == ROSTER_LIMIT:
        return "roster_limit"
    if exc.code == ASSET_LOCKED:
        return "asset_lock"
    if exc.code == DUPLICATE_ASSET:
        return "duplicate_asset"
    if exc.code == DEAL_INVALIDATED:
        return "deal_invalidated"
    return str(exc.code or "unknown")
