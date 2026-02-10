from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import hashlib
import json
import math
import random
from datetime import date

try:
    from schema import normalize_team_id  # type: ignore
except Exception:  # pragma: no cover
    def normalize_team_id(x: str, strict: bool = False) -> str:  # type: ignore
        return str(x or "").upper()

from ..errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    SWAP_NOT_FOUND,
    SWAP_INVALID,
    DUPLICATE_ASSET,
)

from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, TeamDealEvaluation, DealVerdict

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    IncomingPlayerRef,
    StepienHelper,
)

from .dealgen_types import DealGeneratorConfig, DealProposal, DealGenerationStats, _Budgets, _DealSpec, _GenState
from .dealgen_utils import _deal_complexity_exceeds, _is_locked, _canon_team_id, _deal_fingerprint_2team
from .dealgen_scoring import _spec_to_deal, _evaluate_and_score

def _sweetener_loop(state: _GenState, proposal: DealProposal, *, budgets: _Budgets, partner_id: Optional[str] = None) -> List[DealProposal]:
    """Try adding small sweeteners to salvage a near-miss.

    Fixes:
    - Transactional: trial spec only; commit only on success.
    - Limit is on *committed* sweeteners (not attempts).
    - Avoid pick_rules repair that would silently drop the sweetener and still return a valid deal.
    """
    cfg = state.cfg
    buyer_id = proposal.buyer_id
    seller_id = proposal.seller_id

    def _margin(p: DealProposal) -> float:
        return float(getattr(p.seller_eval, "net_surplus", 0.0) or 0.0) - float(getattr(p.seller_decision, "required_surplus", 0.0) or 0.0)

    seller_margin = _margin(proposal)
    if seller_margin >= 0:
        return [proposal]
    if seller_margin < -float(cfg.near_miss_margin_max):
        return [proposal]

    # If they already accept, nothing to do.
    if getattr(proposal.seller_decision, "verdict", None) == DealVerdict.ACCEPT:
        return [proposal]

    buyer_out = state.catalog.outgoing_by_team.get(buyer_id)
    if buyer_out is None:
        return [proposal]

    stepien = state.catalog.stepien

    new_props: List[DealProposal] = [proposal]
    current_best = proposal
    current_spec = _deal_to_spec_guess(current_best.deal, buyer_id=buyer_id, seller_id=seller_id)
    if current_spec is None:
        return [proposal]

    origin_spec = current_spec.copy()
    origin_pick_set = set(origin_spec.buyer_picks_out)
    origin_swap_set = set(origin_spec.buyer_swaps_out)

    def _added_sweeteners(spec: _DealSpec) -> int:
        return len(set(spec.buyer_picks_out) - origin_pick_set) + len(set(spec.buyer_swaps_out) - origin_swap_set)

    max_add = max(0, int(cfg.max_sweeteners))
    if max_add <= 0:
        return [proposal]

    # Track already-added 2RPs (limit at 2).
    second_ids = set(buyer_out.pick_ids_by_bucket.get("SECOND", ()))

    def _count_seconds(spec: _DealSpec) -> int:
        return sum(1 for pid in spec.buyer_picks_out if pid in second_ids)

    committed = _added_sweeteners(current_spec)

    verdict_rank = {DealVerdict.REJECT: 0, DealVerdict.COUNTER: 1, DealVerdict.ACCEPT: 2}

    for token in cfg.sweetener_order:
        if committed >= max_add:
            break
        if state.stats.validations >= budgets.max_validations or state.stats.evaluations >= budgets.max_evaluations:
            break

        # Compare top candidates per token (budget-capped) and commit the best.
        # Always consider the original spec so earlier commits don't block later single-sweetener paths.

        # Budget-aware candidate width (each candidate costs ~1 validation + 2 evaluations)
        cand_limit = int(getattr(cfg, "sweetener_candidate_width", 3) or 3)
        cand_limit = max(1, min(3, cand_limit))
        rem_v = max(0, int(budgets.max_validations - state.stats.validations))
        rem_e = max(0, int(budgets.max_evaluations - state.stats.evaluations))
        cand_limit = min(cand_limit, rem_v)  # 1 validation per candidate
        cand_limit = min(cand_limit, rem_e // 2)  # 2 evals per candidate
        if cand_limit <= 0:
            break

        # If seller is still far (beyond ~1 point), sweeteners rarely fix it; avoid spending trials.
        if _margin(current_best) < -1.0:
            cand_limit = min(cand_limit, 1)

        cand_tag = {
            "SECOND": "sweetener:2RP",
            "FIRST_SAFE": "sweetener:1RP_SAFE",
            "FIRST_SENSITIVE": "sweetener:1RP_SENSITIVE",
            "SWAP": "sweetener:swap",
        }.get(token, "sweetener:asset")

        def _spec_key(s: _DealSpec) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
            return (
                tuple(s.buyer_players_out),
                tuple(s.seller_players_out),
                tuple(sorted(s.buyer_picks_out)),
                tuple(sorted(s.buyer_swaps_out)),
                tuple(sorted(s.seller_picks_out)),
                tuple(sorted(s.seller_swaps_out)),
            )

        base_specs: List[_DealSpec] = [origin_spec]
        if _spec_key(current_spec) != _spec_key(origin_spec) and cand_limit >= 2:
            base_specs.append(current_spec)

        # Split candidate width across base specs without exceeding the overall cand_limit.
        if len(base_specs) == 1:
            base_limits = [cand_limit]
        else:
            per = max(1, cand_limit // len(base_specs))
            base_limits = [cand_limit - per * (len(base_specs) - 1)] + [per] * (len(base_specs) - 1)

        candidates_by_base: List[Tuple[_DealSpec, List[Tuple[Optional[str], Optional[str]]]]] = []
        for base_spec, base_limit in zip(base_specs, base_limits):
            used_picks = set(base_spec.buyer_picks_out)
            used_swaps = set(base_spec.buyer_swaps_out)
            seconds_added = _count_seconds(base_spec)

            candidates: List[Tuple[Optional[str], Optional[str]]] = []
            if token == "SECOND":
                if seconds_added >= 2:
                    continue
                for pid in buyer_out.pick_ids_by_bucket.get("SECOND", ()):  # type: ignore[union-attr]
                    if pid in used_picks or pid in state.banned_picks[buyer_id]:
                        continue
                    cand = buyer_out.picks.get(pid)
                    if cand is None or _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
                        continue
                    if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                        continue
                    candidates.append((pid, None))
                    if len(candidates) >= base_limit:
                        break

            elif token == "FIRST_SAFE":
                for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SAFE", ()):  # type: ignore[union-attr]
                    if pid in used_picks or pid in state.banned_picks[buyer_id]:
                        continue
                    cand = buyer_out.picks.get(pid)
                    if cand is None or _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
                        continue
                    if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                        continue
                    candidates.append((pid, None))
                    if len(candidates) >= base_limit:
                        break

            elif token == "FIRST_SENSITIVE":
                for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()):  # type: ignore[union-attr]
                    if pid in used_picks or pid in state.banned_picks[buyer_id]:
                        continue
                    cand = buyer_out.picks.get(pid)
                    if cand is None or _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
                        continue
                    if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                        continue
                    candidates.append((pid, None))
                    if len(candidates) >= base_limit:
                        break

            elif token == "SWAP":
                for sid in buyer_out.swap_ids:  # type: ignore[union-attr]
                    if sid in used_swaps or sid in state.banned_swaps[buyer_id]:
                        continue
                    cand = buyer_out.swaps.get(sid)
                    if cand is None or _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
                        continue
                    candidates.append((None, sid))
                    if len(candidates) >= base_limit:
                        break

            if candidates:
                candidates_by_base.append((base_spec, candidates))

        if not candidates_by_base:
            continue

        best_p: Optional[DealProposal] = None
        best_spec: Optional[_DealSpec] = None
        best_key: Optional[Tuple[int, int, int, float, float]] = None

        done_token = False
        for base_spec, candidates in candidates_by_base:
            for cand_pick, cand_swap in candidates:
                if state.stats.validations >= budgets.max_validations or state.stats.evaluations >= budgets.max_evaluations:
                    done_token = True
                    break

                trial_spec = base_spec.copy()
                if cand_pick is not None:
                    trial_spec.buyer_picks_out.append(cand_pick)
                    trial_spec.tags.append(cand_tag if token != "SWAP" else "sweetener:pick")
                if cand_swap is not None:
                    trial_spec.buyer_swaps_out.append(cand_swap)
                    trial_spec.tags.append("sweetener:swap")

                # Validate without repair (sweetener must remain attached).
                state.stats.sweetener_trials += 1
                deal2 = _spec_to_deal(state, trial_spec)
                if deal2 is None or _deal_complexity_exceeds(cfg, deal2):
                    state.stats.sweetener_rollbacks += 1
                    continue

                try:
                    state.tick_ctx.validate_deal(deal2, allow_locked_by_deal_id=state.allow_locked_by_deal_id)
                    state.stats.validations += 1
                except TradeError as err:
                    state.stats.validations += 1
                    state.stats.record_error(err)
                    details = err.details if isinstance(err.details, dict) else {}
                    # Intrinsic horizon: hard-ban the candidate pick only in that case.
                    if str(details.get("rule") or "") == "pick_rules" and str(details.get("reason") or "") == "pick_too_far":
                        if cand_pick is not None:
                            state.banned_picks[buyer_id].add(str(cand_pick))
                            state.stats.pruned_stepien += 1
                    state.stats.sweetener_rollbacks += 1
                    continue

                fp = _deal_fingerprint_2team(deal2)
                if fp in state.seen_fingerprints:
                    state.stats.pruned_duplicate += 1
                    state.stats.sweetener_rollbacks += 1
                    continue
                state.seen_fingerprints.add(fp)

                p2 = _evaluate_and_score(state, deal2, buyer_id=buyer_id, seller_id=seller_id, partner_id=partner_id or seller_id)
                if p2 is None:
                    state.stats.sweetener_rollbacks += 1
                    continue
                new_props.append(p2)

                seller_v = getattr(p2.seller_decision, "verdict", DealVerdict.REJECT)
                buyer_v = getattr(p2.buyer_decision, "verdict", DealVerdict.REJECT)
                both_accept = 1 if (seller_v == DealVerdict.ACCEPT and buyer_v == DealVerdict.ACCEPT) else 0
                key = (
                    both_accept,
                    verdict_rank.get(seller_v, 0),
                    verdict_rank.get(buyer_v, 0),
                    _margin(p2),
                    float(getattr(p2, "score", 0.0) or 0.0),
                )
                if best_key is None or key > best_key:
                    best_key = key
                    best_p = p2
                    best_spec = trial_spec

                # If we hit full accept, no need to compare further candidates for this token.
                if both_accept:
                    done_token = True
                    break

            if done_token:
                break

        if best_p is None or best_spec is None:
            continue

        # Commit only if it improves seller outcome (verdict or margin) without worsening buyer verdict.
        old_sv = getattr(current_best.seller_decision, "verdict", DealVerdict.REJECT)
        new_sv = getattr(best_p.seller_decision, "verdict", DealVerdict.REJECT)
        old_bv = getattr(current_best.buyer_decision, "verdict", DealVerdict.REJECT)
        new_bv = getattr(best_p.buyer_decision, "verdict", DealVerdict.REJECT)

        seller_improve = verdict_rank.get(new_sv, 0) > verdict_rank.get(old_sv, 0) or (_margin(best_p) > _margin(current_best) + 1e-6)
        buyer_not_worse = verdict_rank.get(new_bv, 0) >= verdict_rank.get(old_bv, 0)

        if seller_improve and buyer_not_worse:
            current_best = best_p
            current_spec = best_spec
            committed = _added_sweeteners(current_spec)
            state.stats.sweetener_commits += 1
            state.stats.sweetener_commit_by_token[str(token)] += 1

        # Stop early if both accept.
        if getattr(current_best.seller_decision, "verdict", None) == DealVerdict.ACCEPT and getattr(current_best.buyer_decision, "verdict", None) == DealVerdict.ACCEPT:
            break

    new_props.sort(key=lambda x: x.score, reverse=True)
    return new_props[: max(1, min(3, budgets.beam_width))]


def _stepien_ok_after(stepien: StepienHelper, team_id: str, *, outgoing_pick_ids: Set[str]) -> bool:
    """Fast Stepien pre-check using StepienHelper (best-effort).

    We only need to know if the team remains Stepien-compliant after trading away
    the specified outgoing picks. Incoming picks do not affect compliance for the
    outgoing team (ownership is irrelevant), but StepienHelper requires both sets.
    """
    try:
        return bool(stepien.is_compliant_after(team_id=team_id, outgoing_pick_ids=set(outgoing_pick_ids), incoming_pick_ids=set()))
    except Exception:
        # If helper fails, allow validator to catch later.
        return True
def _deal_to_spec_guess(deal: Deal, *, buyer_id: str, seller_id: str) -> Optional[_DealSpec]:
    """Reconstruct a minimal spec from a deal (2-team only)."""
    buyer_id = _canon_team_id(buyer_id)
    seller_id = _canon_team_id(seller_id)
    if not buyer_id or not seller_id:
        return None
    legs = deal.legs or {}
    if buyer_id not in legs or seller_id not in legs:
        return None
    spec = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
    for a in legs.get(buyer_id, []):
        if isinstance(a, PlayerAsset):
            spec.buyer_players_out.append(a.player_id)
        elif isinstance(a, PickAsset):
            spec.buyer_picks_out.append(a.pick_id)
        elif isinstance(a, SwapAsset):
            spec.buyer_swaps_out.append(a.swap_id)
    for a in legs.get(seller_id, []):
        if isinstance(a, PlayerAsset):
            spec.seller_players_out.append(a.player_id)
        elif isinstance(a, PickAsset):
            spec.seller_picks_out.append(a.pick_id)
        elif isinstance(a, SwapAsset):
            spec.seller_swaps_out.append(a.swap_id)
    return spec
