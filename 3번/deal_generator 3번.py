from __future__ import annotations

"""trade.trades.generation.deal_generator

Commercial-grade 2-team deal generator.

Design goals
------------
- NBA-like: need-based targets, plausible packages (players/picks/filers), minimal spam.
- Fast: tick-level caching (TradeGenerationTickContext), bounded search (beam/prune), deterministic RNG.
- Stable: validate_deal() failure details drive minimal repair (1-2 steps); invalid ratio stays low.
- Consistent decisions: final ranking always based on evaluate_deal_for_team(...) results for *both* teams.

3+ team trades
--------------
This module intentionally focuses on 2-team deals. Extension points are left
as internal helpers (_resolve_receiver_team, _add_leg_asset etc.)
so a future multi-team generator can reuse core components.
"""

from dataclasses import dataclass, field
from datetime import date
from math import exp
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import hashlib
import json
import random
import re

from ..errors import (
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    DUPLICATE_ASSET,
    TradeError,
)
from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, TeamDealEvaluation

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    TradeAssetCatalog,
    build_trade_asset_catalog,
)


# =============================================================================
# Config / output types
# =============================================================================


@dataclass(slots=True)
class DealGeneratorConfig:
    """Tuning knobs for exploration and realism."""

    # --- Global caps (hard)
    max_results_default: int = 12
    max_targets_hard_cap: int = 28
    max_attempts_per_target_hard_cap: int = 70
    max_validations_hard_cap: int = 650
    max_evaluations_hard_cap: int = 520

    # --- Baselines (scaled by posture/urgency/deadline_pressure)
    base_max_targets: int = 14
    base_beam_width: int = 9
    base_max_attempts_per_target: int = 40
    base_max_repairs: int = 2
    base_max_assets: int = 6
    base_max_players_moved: int = 4
    base_skeletons_per_target: int = 5

    # --- Sweeteners
    max_second_rounders_as_sweetener: int = 2
    allow_swaps_as_sweetener: bool = True
    allow_first_sensitive_as_last_resort: bool = False

    # Sweetener activation window (performance + realism)
    # We only try sweeteners when seller is "close" to accept.
    # DecisionPolicy's counter corridor defaults to ~0.06*scale; we use 2x that as a starting point.
    sweetener_close_corridor_ratio: float = 0.12
    sweetener_close_floor: float = 0.6
    sweetener_close_cap: float = 8.0

    # --- Heuristics / realism
    need_tags_max: int = 4
    need_tags_min_weight: float = 0.30
    target_repeat_penalty: float = 0.15
    opponent_repeat_penalty: float = 0.10

    # target selection throttles
    per_tag_take: int = 18
    cheap_per_tag_take: int = 10

    # evaluation scoring
    sigmoid_scale: float = 7.0
    complexity_asset_penalty: float = 0.15
    complexity_player_penalty: float = 0.10
    reject_floor_penalty: float = 0.75

    # RNG determinism
    rng_salt: int = 1337

    # If True, generator will attempt to build asset catalog when missing.
    build_catalog_if_missing: bool = True


@dataclass(frozen=True, slots=True)
class DealProposal:
    deal: Deal
    buyer_id: str
    seller_id: str
    buyer_decision: DealDecision
    seller_decision: DealDecision
    buyer_eval: TeamDealEvaluation
    seller_eval: TeamDealEvaluation
    score: float
    tags: Tuple[str, ...] = tuple()


# =============================================================================
# Internal helpers
# =============================================================================


@dataclass(slots=True)
class _BudgetTracker:
    """Hard-cap budget tracker.

    We count *attempted* validations/evaluations (success or failure) because
    the cost is incurred by the call itself.
    """

    max_validations: int
    max_evaluations: int
    validations_used: int = 0
    evaluations_used: int = 0

    def can_consume_validations(self, n: int = 1) -> bool:
        try:
            nn = int(n)
        except Exception:
            nn = 1
        return (self.validations_used + max(0, nn)) <= int(self.max_validations)

    def can_consume_evaluations(self, n: int = 1) -> bool:
        try:
            nn = int(n)
        except Exception:
            nn = 1
        return (self.evaluations_used + max(0, nn)) <= int(self.max_evaluations)

    def try_consume_validations(self, n: int = 1) -> bool:
        if not self.can_consume_validations(n):
            return False
        self.validations_used += max(0, int(n))
        return True

    def try_consume_evaluations(self, n: int = 1) -> bool:
        if not self.can_consume_evaluations(n):
            return False
        self.evaluations_used += max(0, int(n))
        return True


def _canon_team_id(team_id: Any) -> str:
    raw = str(team_id or "").strip()
    if not raw:
        return ""
    try:
        from schema import normalize_team_id  # type: ignore

        return str(normalize_team_id(raw, strict=False)).strip().upper()
    except Exception:
        return raw.upper()


def _sigmoid(x: float) -> float:
    # numerically stable enough for our bounded ranges
    try:
        return 1.0 / (1.0 + exp(-float(x)))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _deal_num_assets(deal: Deal) -> int:
    return int(sum(len(v or []) for v in (deal.legs or {}).values()))


def _deal_num_players_moved(deal: Deal) -> int:
    n = 0
    for assets in (deal.legs or {}).values():
        for a in assets or []:
            if isinstance(a, PlayerAsset):
                n += 1
    return int(n)


def _deal_outgoing_pick_ids(deal: Deal, team_id: str) -> Set[str]:
    out: Set[str] = set()
    for a in deal.legs.get(team_id, []) or []:
        if isinstance(a, PickAsset):
            out.add(str(a.pick_id))
    return out


def _deal_assets_by_team(deal: Deal, team_id: str) -> Tuple[List[PlayerAsset], List[PickAsset], List[SwapAsset]]:
    ps: List[PlayerAsset] = []
    picks: List[PickAsset] = []
    swaps: List[SwapAsset] = []
    for a in deal.legs.get(team_id, []) or []:
        if isinstance(a, PlayerAsset):
            ps.append(a)
        elif isinstance(a, PickAsset):
            picks.append(a)
        elif isinstance(a, SwapAsset):
            swaps.append(a)
    return ps, picks, swaps


def _protected_player_ids_from_meta(deal: Deal) -> Set[str]:
    meta = deal.meta or {}
    ids = meta.get("protected_player_ids")
    if isinstance(ids, (list, tuple, set)):
        return {str(x) for x in ids if x is not None and str(x).strip()}
    return set()


def _hash_deal_for_dedupe(deal: Deal) -> str:
    """Stable content hash for dedupe.

    Important:
    - Must be deterministic across processes (so do NOT use Python's built-in hash()).
    - Keep representation compact (sha1) to reduce memory.
    """
    try:
        canon = canonicalize_deal(deal)
    except Exception:
        canon = deal
    payload = serialize_deal(canon)
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except Exception:
        raw = str(payload).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _team_posture(ts: Any) -> str:
    return str(getattr(ts, "trade_posture", "") or "").upper()


def _team_time_horizon(ts: Any) -> str:
    return str(getattr(ts, "time_horizon", "") or "").upper()


def _team_urgency(ts: Any) -> float:
    return float(getattr(ts, "urgency", 0.0) or 0.0)


def _team_deadline_pressure(ts: Any) -> float:
    c = getattr(ts, "constraints", None)
    return float(getattr(c, "deadline_pressure", 0.0) or 0.0)


def _team_cooldown(ts: Any) -> bool:
    c = getattr(ts, "constraints", None)
    return bool(getattr(c, "cooldown_active", False) or False)


def _team_cap_space(ts: Any) -> float:
    c = getattr(ts, "constraints", None)
    return float(getattr(c, "cap_space", 0.0) or 0.0)


def _team_apron_status(ts: Any) -> str:
    c = getattr(ts, "constraints", None)
    return str(getattr(c, "apron_status", "") or "").upper()


def _is_rebuildish(ts: Any) -> bool:
    th = _team_time_horizon(ts)
    tier = str(getattr(ts, "competitive_tier", "") or "").upper()
    return th in {"REBUILD"} or tier in {"REBUILD", "RESET", "TANK"}


def _choose_top_need_tags(tick_ctx: TradeGenerationTickContext, team_id: str, cfg: DealGeneratorConfig) -> List[str]:
    dc = tick_ctx.get_decision_context(team_id)
    need_map = getattr(dc, "need_map", None) or {}
    if not isinstance(need_map, Mapping) or not need_map:
        return []
    items = [(str(k), float(v or 0.0)) for k, v in need_map.items() if k is not None]
    items = [(k, v) for k, v in items if v >= float(cfg.need_tags_min_weight)]
    items.sort(key=lambda x: (-x[1], x[0]))
    out: List[str] = []
    for k, _ in items:
        if k and k not in out:
            out.append(k)
        if len(out) >= int(cfg.need_tags_max):
            break
    return out


def _team_need_map(tick_ctx: TradeGenerationTickContext, team_id: str) -> Dict[str, float]:
    """Safely extract need_map for a team (string->float)."""
    try:
        dc = tick_ctx.get_decision_context(team_id)
        nm = getattr(dc, "need_map", None) or {}
        if not isinstance(nm, Mapping):
            return {}
        out: Dict[str, float] = {}
        for k, v in nm.items():
            if k is None:
                continue
            kk = str(k)
            if not kk:
                continue
            try:
                out[kk] = float(v or 0.0)
            except Exception:
                out[kk] = 0.0
        return out
    except Exception:
        return {}


def _need_fit_score(supply: Any, need_map: Mapping[str, float]) -> float:
    """Dot(supply, need_map). supply is expected to be Mapping[tag->strength]."""
    if not need_map:
        return 0.0
    if not isinstance(supply, Mapping) or not supply:
        return 0.0
    s = 0.0
    for tag, sv in supply.items():
        try:
            s += float(sv or 0.0) * float(need_map.get(str(tag), 0.0) or 0.0)
        except Exception:
            continue
    return float(s)


def _extract_fit_fail_tags(dec: Any) -> Set[str]:
    """Extract focused tags/needs that caused FIT_FAILS (if present).

    Reason objects differ by implementation, so we try multiple fields:
      - r.meta / r.details / r.data (mapping)
      - keys: 'tags', 'need_tags', 'missing_tags', 'failed_tags', 'positions'
    Returns an uppercased tag set for robust matching.
    """
    out: Set[str] = set()
    if dec is None:
        return out
    reasons = getattr(dec, "reasons", None) or tuple()
    for r in reasons:
        try:
            code = str(getattr(r, "code", "") or "")
        except Exception:
            code = ""
        if code != "FIT_FAILS":
            continue
        meta = None
        for attr in ("meta", "details", "data"):
            v = getattr(r, attr, None)
            if isinstance(v, Mapping):
                meta = v
                break
        if not isinstance(meta, Mapping):
            continue
        for k in ("need_tags", "tags", "missing_tags", "failed_tags", "positions"):
            v = meta.get(k)
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                for t in v:
                    tt = str(t or "").strip()
                    if tt:
                        out.add(tt.upper())
            else:
                # allow comma/space separated string
                s = str(v or "")
                for part in re.split(r"[,\s]+", s):
                    part = part.strip()
                    if part:
                        out.add(part.upper())
    return out


def _pick_from_buckets(
    outcat: TeamOutgoingCatalog,
    buckets: Sequence[str],
    *,
    exclude_players: Set[str],
    to_team: str,
    max_n: int,
    prefer_low_market: bool = True,
    receiver_need_map: Optional[Mapping[str, float]] = None,
) -> List[PlayerTradeCandidate]:
    """Select player candidates from outgoing buckets.

    Uses TeamOutgoingCatalog ordering (already deterministic).
    """
    selected: List[PlayerTradeCandidate] = []
    for b in buckets:
        ids = outcat.player_ids_by_bucket.get(b, tuple()) or tuple()
        for pid in ids:
            if pid in exclude_players:
                continue
            cand = outcat.players.get(pid)
            if cand is None:
                continue
            # return-to-team ban
            if to_team and to_team in {str(t).upper() for t in (cand.return_ban_teams or tuple())}:
                continue
            selected.append(cand)
            if len(selected) >= int(max_n):
                break
        if len(selected) >= int(max_n):
            break

    def _fit(c: PlayerTradeCandidate) -> float:
        try:
            return _need_fit_score(getattr(c, "supply", None) or {}, receiver_need_map or {})
        except Exception:
            return 0.0

    if prefer_low_market:
        # fillers: keep market low; fit is a tie-breaker for plausibility
        selected.sort(key=lambda c: (float(c.market.total), float(c.salary_m), -_fit(c), c.player_id))
    else:
        # value pieces: prioritize receiver fit, then market
        selected.sort(key=lambda c: (-_fit(c), -float(c.market.total), -float(c.salary_m), c.player_id))
    return selected[: int(max_n)]


def _closest_salary_players(
    outcat: TeamOutgoingCatalog,
    *,
    target_salary_m: float,
    exclude_players: Set[str],
    to_team: str,
    max_n: int,
    receiver_need_map: Optional[Mapping[str, float]] = None,
) -> List[PlayerTradeCandidate]:
    # pool from all non-core outgoing buckets
    pool_ids: List[str] = []
    for b, ids in (outcat.player_ids_by_bucket or {}).items():
        if b == "CORE":
            continue
        pool_ids.extend(list(ids or []))
    uniq: List[str] = []
    seen: Set[str] = set()
    for pid in pool_ids:
        if pid in seen:
            continue
        seen.add(pid)
        uniq.append(pid)

    pool: List[PlayerTradeCandidate] = []
    for pid in uniq:
        if pid in exclude_players:
            continue
        cand = outcat.players.get(pid)
        if cand is None:
            continue
        if to_team and to_team in {str(t).upper() for t in (cand.return_ban_teams or tuple())}:
            continue
        pool.append(cand)

    def _fit(c: PlayerTradeCandidate) -> float:
        try:
            return _need_fit_score(getattr(c, "supply", None) or {}, receiver_need_map or {})
        except Exception:
            return 0.0

    # prioritize salary closeness, then receiver fit, then lower market (avoid overpay artifacts)
    pool.sort(key=lambda c: (abs(float(c.salary_m) - float(target_salary_m)), -_fit(c), float(c.market.total), c.player_id))
    return pool[: int(max_n)]


def _player_asset(pid: str) -> PlayerAsset:
    return PlayerAsset(kind="player", player_id=str(pid), to_team=None)


def _pick_asset(pid: str, outcat: TeamOutgoingCatalog) -> Optional[PickAsset]:
    cand = outcat.picks.get(str(pid))
    if cand is None:
        return None
    return PickAsset(kind="pick", pick_id=str(pid), to_team=None, protection=cand.snap.protection)


def _swap_asset(sid: str, outcat: TeamOutgoingCatalog) -> Optional[SwapAsset]:
    cand = outcat.swaps.get(str(sid))
    if cand is None:
        return None
    return SwapAsset(
        kind="swap",
        swap_id=str(cand.swap_id),
        pick_id_a=str(cand.snap.pick_id_a),
        pick_id_b=str(cand.snap.pick_id_b),
        to_team=None,
    )


def _remove_one_incoming_player(
    deal: Deal,
    *,
    receiver_team: str,
    protected_players: Set[str],
    prefer_remove_high_salary: bool,
    catalog: TradeAssetCatalog,
) -> bool:
    """Remove one player that is incoming to receiver_team.

    In 2-team deals, incoming to receiver_team are the PlayerAssets in the *other* leg.
    """
    if len(deal.teams) != 2:
        return False
    t0, t1 = deal.teams
    other = t1 if receiver_team == t0 else t0
    assets = list(deal.legs.get(other, []) or [])
    player_assets = [a for a in assets if isinstance(a, PlayerAsset)]
    if not player_assets:
        return False

    # Rank removable players.
    ranked: List[Tuple[float, float, str, PlayerAsset]] = []
    out_other = catalog.outgoing_by_team.get(_canon_team_id(other))
    for a in player_assets:
        pid = str(a.player_id)
        if pid in protected_players:
            continue
        salary_m = 0.0
        market_total = 0.0
        if out_other and pid in out_other.players:
            c = out_other.players[pid]
            salary_m = float(c.salary_m)
            market_total = float(c.market.total)
        key = (-salary_m if prefer_remove_high_salary else market_total)
        ranked.append((float(key), float(market_total), pid, a))

    if not ranked:
        return False
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    _, __, pid, asset_to_remove = ranked[0]
    new_other_leg = [a for a in assets if not (isinstance(a, PlayerAsset) and str(a.player_id) == pid)]
    deal.legs[other] = new_other_leg
    return True


def _add_one_outgoing_filler_player(
    deal: Deal,
    *,
    from_team: str,
    to_team: str,
    catalog: TradeAssetCatalog,
    exclude_players: Set[str],
    max_outgoing_players: int,
    target_add_salary_m: Optional[float] = None,
) -> bool:
    if len(deal.teams) != 2:
        return False
    from_team_u = _canon_team_id(from_team)
    to_team_u = _canon_team_id(to_team)
    outcat = catalog.outgoing_by_team.get(from_team_u)
    if outcat is None:
        return False
    current_leg = list(deal.legs.get(from_team_u, []) or [])
    current_out_players = [a for a in current_leg if isinstance(a, PlayerAsset)]
    if len(current_out_players) >= int(max_outgoing_players):
        return False

    # Choose filler. If we know "needed salary gap", prefer salary that closes it while keeping market low.
    candidates = _pick_from_buckets(
        outcat,
        buckets=("FILLER_CHEAP", "FILLER_BAD_CONTRACT", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT"),
        exclude_players=exclude_players,
        to_team=to_team_u,
        max_n=10,
        prefer_low_market=True,
    )
    if target_add_salary_m is not None:
        gap = max(0.0, float(target_add_salary_m))
        # Prefer candidates that meet/exceed the gap (more likely to fix matching),
        # then closest to the gap, then lowest market.
        candidates.sort(
            key=lambda c: (
                0 if float(c.salary_m) >= gap else 1,
                abs(float(c.salary_m) - gap),
                float(c.market.total),
                c.player_id,
            )
        )
    for c in candidates:
        # aggregation solo-only cannot be aggregated with others.
        if bool(c.aggregation_solo_only) and len(current_out_players) >= 1:
            continue
        deal.legs[from_team_u] = current_leg + [_player_asset(c.player_id)]
        return True
    return False


def _enforce_one_for_one_players(
    deal: Deal,
    *,
    protected_players: Set[str],
    catalog: TradeAssetCatalog,
) -> bool:
    """Reduce both legs to <= 1 PlayerAsset each.

    Returns True if modified.
    """
    if len(deal.teams) != 2:
        return False
    modified = False
    for team_id in list(deal.teams):
        leg = list(deal.legs.get(team_id, []) or [])
        players = [a for a in leg if isinstance(a, PlayerAsset)]
        if len(players) <= 1:
            continue

        # Remove extras, keep protected if possible.
        keep: Optional[str] = None
        for a in players:
            pid = str(a.player_id)
            if pid in protected_players:
                keep = pid
                break
        if keep is None:
            # Keep the highest market total among outgoing leg players.
            outcat = catalog.outgoing_by_team.get(_canon_team_id(team_id))
            best_pid = None
            best_v = -1e9
            if outcat is not None:
                for a in players:
                    pid = str(a.player_id)
                    c = outcat.players.get(pid)
                    v = float(c.market.total) if c else 0.0
                    if v > best_v:
                        best_v = v
                        best_pid = pid
            keep = best_pid or str(players[0].player_id)

        new_leg: List[Any] = []
        kept_player = False
        for a in leg:
            if isinstance(a, PlayerAsset):
                if not kept_player and str(a.player_id) == keep:
                    new_leg.append(a)
                    kept_player = True
                else:
                    modified = True
                    continue
            else:
                new_leg.append(a)
        deal.legs[team_id] = new_leg

    return modified


# =============================================================================
# DealGenerator
# =============================================================================


class DealGenerator:
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

                # Dedupe early (pre-repair)
                h_skel = _hash_deal_for_dedupe(deal)
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
                        )
                        if not repaired:
                            deal_valid = False
                            break

                if not deal_valid:
                    continue

                # Final dedupe (post-repair / post-validation)
                h_final = _hash_deal_for_dedupe(deal)
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
                            )
                        # Re-evaluate only if we can afford it.
                        if deal2 is not None:
                            if not budget.try_consume_evaluations(2):
                                hard_stop = True
                            else:
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
                tags = tuple(sorted(set(skel_tags)))

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

    # ---------------------------------------------------------------------
    # Target selection
    # ---------------------------------------------------------------------
    def _select_target_pairs(
        self,
        team_id: str,
        *,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        posture: str,
        urgency: float,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[str, str, str, Optional[str]]]:
        """Return list of (buyer_id, seller_id, target_player_id, tag_hint)."""

        p = str(posture or "").upper()
        if p in {"SELL", "SOFT_SELL"}:
            return self._select_target_pairs_sell_mode(
                team_id,
                tick_ctx=tick_ctx,
                catalog=catalog,
                budgets=budgets,
                rng=rng,
            )
        return self._select_target_pairs_buy_mode(
            team_id,
            tick_ctx=tick_ctx,
            catalog=catalog,
            budgets=budgets,
            rng=rng,
        )

    def _select_target_pairs_buy_mode(
        self,
        buyer_id: str,
        *,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[str, str, str, Optional[str]]]:
        need_tags = _choose_top_need_tags(tick_ctx, buyer_id, self.cfg)
        if not need_tags:
            return []

        buyer_ts = tick_ctx.get_team_situation(buyer_id)
        buyer_apron = _team_apron_status(buyer_ts)

        # Collect refs from incoming indices.
        refs: List[IncomingPlayerRef] = []
        for tag in need_tags:
            refs.extend(list((catalog.incoming_by_need_tag.get(tag) or tuple())[: int(self.cfg.per_tag_take)]))
            refs.extend(list((catalog.incoming_cheap_by_need_tag.get(tag) or tuple())[: int(self.cfg.cheap_per_tag_take)]))

        # Score refs lightly.
        dc = tick_ctx.get_decision_context(buyer_id)
        need_map = getattr(dc, "need_map", None) or {}
        scored: List[Tuple[float, IncomingPlayerRef]] = []
        for r in refs:
            seller_id = _canon_team_id(r.from_team)
            if not seller_id or seller_id == buyer_id:
                continue
            seller_ts = tick_ctx.get_team_situation(seller_id)
            if _team_cooldown(seller_ts):
                continue

            # Only consider targets that seller is plausibly willing to move.
            seller_out = catalog.outgoing_by_team.get(seller_id)
            if seller_out is None:
                continue
            if r.player_id not in seller_out.players:
                continue
            # Exclude seller CORE (kept by catalog)
            if r.player_id in set(seller_out.player_ids_by_bucket.get("CORE", tuple()) or tuple()):
                continue
            # Prefer listed outgoing buckets
            offered = False
            for b in ("VETERAN_SALE", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_BAD_CONTRACT", "FILLER_CHEAP", "CONSOLIDATE"):
                if r.player_id in set(seller_out.player_ids_by_bucket.get(b, tuple()) or tuple()):
                    offered = True
                    break
            if not offered:
                # still allow a small chance for mid-tier movement
                if rng.random() > 0.15:
                    continue

            target_cand = seller_out.players.get(r.player_id)
            if target_cand is None:
                continue
            # return-to-trading-team ban: buyer can't be in banned teams
            if buyer_id in {str(t).upper() for t in (target_cand.return_ban_teams or tuple())}:
                continue

            # If buyer is above 2nd apron, prefer smaller salary targets to avoid hard-to-match.
            apron_pen = 0.0
            if buyer_apron == "ABOVE_2ND_APRON" and float(target_cand.salary_m) >= 15.0:
                apron_pen = 0.45

            need_w = float(need_map.get(r.tag, 0.0) or 0.0)
            score = float(r.tag_strength) * (0.7 + 0.9 * need_w) + 0.15 * float(target_cand.market.total)
            score -= 0.08 * float(target_cand.salary_m)
            score -= apron_pen
            scored.append((score, r))

        scored.sort(key=lambda x: (-x[0], x[1].from_team, x[1].player_id))
        max_targets = int(budgets.get("max_targets", 0))
        max_targets = max(0, max_targets)

        out: List[Tuple[str, str, str, Optional[str]]] = []
        seen: Set[Tuple[str, str]] = set()
        for _, r in scored:
            seller_id = _canon_team_id(r.from_team)
            key = (seller_id, str(r.player_id))
            if key in seen:
                continue
            seen.add(key)
            out.append((buyer_id, seller_id, str(r.player_id), str(r.tag) if r.tag else None))
            if len(out) >= max_targets:
                break

        return out

    def _select_target_pairs_sell_mode(
        self,
        seller_id: str,
        *,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[str, str, str, Optional[str]]]:
        """Shop the seller's outgoing players to likely buyers."""
        seller_out = catalog.outgoing_by_team.get(seller_id)
        if seller_out is None:
            return []

        # Choose a small set of "for sale" players.
        candidate_ids: List[str] = []
        for b in ("VETERAN_SALE", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_BAD_CONTRACT", "CONSOLIDATE"):
            candidate_ids.extend(list(seller_out.player_ids_by_bucket.get(b, tuple()) or tuple()))
        # de-dupe
        uniq: List[str] = []
        sset: Set[str] = set()
        for pid in candidate_ids:
            if pid in sset:
                continue
            sset.add(pid)
            uniq.append(pid)

        # Small ranking: prefer higher value sale pieces.
        pieces: List[PlayerTradeCandidate] = []
        for pid in uniq:
            c = seller_out.players.get(pid)
            if c is None:
                continue
            pieces.append(c)
        pieces.sort(key=lambda c: (-float(c.market.total), -float(c.salary_m), c.player_id))
        pieces = pieces[: max(0, int(budgets.get("max_targets", 0)))]

        # For each piece, choose likely buyers by scanning teams (30 teams; bounded).
        out: List[Tuple[str, str, str, Optional[str]]] = []
        teams = list(catalog.outgoing_by_team.keys())
        for piece in pieces:
            tag_hint = piece.top_tags[0] if piece.top_tags else None

            scored_buyers: List[Tuple[float, str]] = []
            for buyer_id in teams:
                buyer_id_u = _canon_team_id(buyer_id)
                if not buyer_id_u or buyer_id_u == seller_id:
                    continue
                buyer_ts = tick_ctx.get_team_situation(buyer_id_u)
                if _team_cooldown(buyer_ts):
                    continue
                # return-to-team ban
                if buyer_id_u in {str(t).upper() for t in (piece.return_ban_teams or tuple())}:
                    continue
                dc = tick_ctx.get_decision_context(buyer_id_u)
                nm = getattr(dc, "need_map", None) or {}
                if not isinstance(nm, Mapping):
                    continue
                need_fit = 0.0
                for tag, sv in (piece.supply or {}).items():
                    need_fit += float(sv or 0.0) * float(nm.get(tag, 0.0) or 0.0)
                if need_fit <= 0.05:
                    continue
                # prefer buy-ish teams
                bp = _team_posture(buyer_ts)
                posture_bonus = 0.12 if bp in {"AGGRESSIVE_BUY", "SOFT_BUY"} else (-0.05 if bp in {"SELL", "SOFT_SELL"} else 0.0)
                score = float(need_fit) + 0.05 * float(piece.market.total) + posture_bonus
                scored_buyers.append((score, buyer_id_u))

            scored_buyers.sort(key=lambda x: (-x[0], x[1]))
            # Pick top N buyers per piece
            for _, buyer_id_u in scored_buyers[:5]:
                out.append((buyer_id_u, seller_id, piece.player_id, tag_hint))
                if len(out) >= int(budgets.get("max_targets", 0)):
                    break
            if len(out) >= int(budgets.get("max_targets", 0)):
                break

        # Small shuffle for variety
        rng.shuffle(out)
        return out[: int(budgets.get("max_targets", 0))]

    # ---------------------------------------------------------------------
    # Skeleton builder
    # ---------------------------------------------------------------------
    def _build_offer_skeletons(
        self,
        *,
        buyer_id: str,
        seller_id: str,
        target_player_id: str,
        tag_hint: Optional[str],
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[Deal, Set[str]]]:
        buyer_id = _canon_team_id(buyer_id)
        seller_id = _canon_team_id(seller_id)
        pid = str(target_player_id)
        out: List[Tuple[Deal, Set[str]]] = []

        buyer_ts = tick_ctx.get_team_situation(buyer_id)
        seller_ts = tick_ctx.get_team_situation(seller_id)

        buyer_out = catalog.outgoing_by_team.get(buyer_id)
        seller_out = catalog.outgoing_by_team.get(seller_id)
        if buyer_out is None or seller_out is None:
            return []
        if pid not in seller_out.players:
            return []

        target = seller_out.players[pid]
        target_salary_m = float(target.salary_m)
        target_market = float(target.market.total)

        # Seller need-map used to build "what seller wants to receive"
        seller_need_map = _team_need_map(tick_ctx, seller_id)

        tags_base: Set[str] = set()
        if tag_hint:
            tags_base.add(f"need:{tag_hint}")
        tags_base.add(f"target:{pid}")

        # Determine whether picks-only is viable (cap space absorption)
        cap_space = _team_cap_space(buyer_ts)
        cap_space_m = cap_space / 1_000_000.0
        can_absorb = cap_space_m >= target_salary_m and cap_space_m > 0.25
        if can_absorb:
            d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
            added = self._add_picks_package(
                d,
                from_team=buyer_id,
                to_team=seller_id,
                catalog=catalog,
                desired_total_value=max(4.0, 0.75 * target_market),
                max_picks=2,
                include_first_safe=_is_rebuildish(seller_ts),
                rng=rng,
            )
            if added:
                tags = set(tags_base)
                tags.add("archetype:picks_only")
                out.append((d, tags))

        # Young + pick (rebuild sellers)
        if _is_rebuildish(seller_ts) or rng.random() < 0.25:
            young = _pick_from_buckets(
                buyer_out,
                buckets=("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_CHEAP"),
                exclude_players=set(),
                to_team=seller_id,
                max_n=4,
                prefer_low_market=False,
                receiver_need_map=seller_need_map,
            )
            for c in young[:2]:
                d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
                d.legs[buyer_id].append(_player_asset(c.player_id))
                # add a small pick sweetener
                self._add_pick_by_bucket(d, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket="SECOND")
                tags = set(tags_base)
                tags.add("archetype:young_plus_pick")
                tags.add(f"give:{c.player_id}")
                out.append((d, tags))

        # Player-for-player around salary
        p2p = _closest_salary_players(
            buyer_out,
            target_salary_m=target_salary_m,
            exclude_players=set(),
            to_team=seller_id,
            max_n=3,
            receiver_need_map=seller_need_map,
        )
        for c in p2p[:2]:
            d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
            d.legs[buyer_id].append(_player_asset(c.player_id))
            # optional small pick when value mismatch
            if float(c.market.total) + 2.0 < target_market and rng.random() < 0.75:
                self._add_pick_by_bucket(d, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket="SECOND")
            tags = set(tags_base)
            tags.add("archetype:p2p")
            tags.add(f"give:{c.player_id}")
            out.append((d, tags))

        # Consolidate (2-for-1) unless buyer is likely 2nd apron restricted.
        buyer_apron = _team_apron_status(buyer_ts)
        if buyer_apron != "ABOVE_2ND_APRON" and rng.random() < 0.70:
            cons = _pick_from_buckets(
                buyer_out,
                buckets=("CONSOLIDATE", "SURPLUS_REDUNDANT", "SURPLUS_LOW_FIT"),
                exclude_players=set(),
                to_team=seller_id,
                max_n=3,
                prefer_low_market=False,
                receiver_need_map=seller_need_map,
            )
            filler = _pick_from_buckets(
                buyer_out,
                buckets=("FILLER_CHEAP", "FILLER_BAD_CONTRACT", "EXPIRING"),
                exclude_players=set(c.player_id for c in cons),
                to_team=seller_id,
                max_n=4,
                prefer_low_market=True,
                receiver_need_map=seller_need_map,
            )
            if cons and filler:
                d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
                d.legs[buyer_id].append(_player_asset(cons[0].player_id))
                d.legs[buyer_id].append(_player_asset(filler[0].player_id))
                if rng.random() < 0.55:
                    self._add_pick_by_bucket(d, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket="SECOND")
                tags = set(tags_base)
                tags.add("archetype:consolidate")
                tags.add(f"give:{cons[0].player_id}")
                tags.add(f"give:{filler[0].player_id}")
                out.append((d, tags))

        # Cap skeleton count
        out = out[: int(budgets.get("skeletons_per_target", 5))]

        # Shuffle slightly but deterministically (rng already seeded)
        rng.shuffle(out)
        return out[: int(budgets.get("skeletons_per_target", 5))]

    def _make_base_deal(self, buyer_id: str, seller_id: str, *, target_player_id: str) -> Deal:
        buyer_id = _canon_team_id(buyer_id)
        seller_id = _canon_team_id(seller_id)
        deal = Deal(
            teams=[buyer_id, seller_id],
            legs={buyer_id: [], seller_id: [_player_asset(target_player_id)]},
            meta={
                "dealgen": {"version": 1, "target_player_id": str(target_player_id)},
                # protected list used by repair to avoid removing the primary target
                "protected_player_ids": [str(target_player_id)],
            },
        )
        return deal

    # ---------------------------------------------------------------------
    # Pick / sweetener helpers
    # ---------------------------------------------------------------------
    def _add_pick_by_bucket(
        self,
        deal: Deal,
        *,
        from_team: str,
        to_team: str,
        catalog: TradeAssetCatalog,
        bucket: str,
    ) -> bool:
        from_team_u = _canon_team_id(from_team)
        to_team_u = _canon_team_id(to_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False
        ids = list(outcat.pick_ids_by_bucket.get(bucket, tuple()) or tuple())
        if not ids:
            return False
        existing = _deal_outgoing_pick_ids(deal, from_team_u)
        for pid in ids:
            if pid in existing:
                continue
            # Stepien check (quick)
            if not catalog.stepien.is_compliant_after(team_id=from_team_u, outgoing_pick_ids=existing | {pid}, incoming_pick_ids=set()):
                continue
            a = _pick_asset(pid, outcat)
            if a is None:
                continue
            deal.legs[from_team_u] = list(deal.legs.get(from_team_u, []) or []) + [a]
            return True
        return False

    def _add_swap_sweetener(self, deal: Deal, *, from_team: str, to_team: str, catalog: TradeAssetCatalog) -> bool:
        from_team_u = _canon_team_id(from_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False
        existing_swaps = {str(a.swap_id) for a in (deal.legs.get(from_team_u, []) or []) if isinstance(a, SwapAsset)}
        for sid in outcat.swap_ids or tuple():
            if str(sid) in existing_swaps:
                continue
            a = _swap_asset(str(sid), outcat)
            if a is None:
                continue
            deal.legs[from_team_u] = list(deal.legs.get(from_team_u, []) or []) + [a]
            return True
        return False

    def _add_picks_package(
        self,
        deal: Deal,
        *,
        from_team: str,
        to_team: str,
        catalog: TradeAssetCatalog,
        desired_total_value: float,
        max_picks: int,
        include_first_safe: bool,
        rng: random.Random,
    ) -> bool:
        """Add a small picks package from from_team to to_team.

        Returns True if at least one pick was added.
        """
        from_team_u = _canon_team_id(from_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False
        existing = _deal_outgoing_pick_ids(deal, from_team_u)
        rejected: Set[str] = set()  # picks tried and rejected (e.g., Stepien)

        # Candidate picks ordered by bucket preference.
        buckets: List[str] = ["SECOND"]
        if include_first_safe:
            buckets.append("FIRST_SAFE")
        # First sensitive only if config allows
        if self.cfg.allow_first_sensitive_as_last_resort:
            buckets.append("FIRST_SENSITIVE")

        added_any = False
        total_value = 0.0
        # Try add up to max_picks
        for _ in range(int(max_picks)):
            best: Optional[PickTradeCandidate] = None
            best_bucket: Optional[str] = None
            for b in buckets:
                for pid in list(outcat.pick_ids_by_bucket.get(b, tuple()) or tuple()):
                    if pid in existing or pid in rejected:
                        continue
                    cand = outcat.picks.get(pid)
                    if cand is None:
                        continue
                    # prefer smaller incremental value as we approach target
                    if best is None:
                        best = cand
                        best_bucket = b
                        continue
                    # pick the one that gets us closer to desired_total_value
                    cur_diff = abs((total_value + best.market.total) - desired_total_value)
                    new_diff = abs((total_value + cand.market.total) - desired_total_value)
                    if new_diff < cur_diff:
                        best = cand
                        best_bucket = b
            if best is None:
                break

            pid = str(best.pick_id)
            if not catalog.stepien.is_compliant_after(team_id=from_team_u, outgoing_pick_ids=existing | {pid}, incoming_pick_ids=set()):
                # if this pick makes Stepien illegal, skip it.
                # IMPORTANT: do NOT add it to `existing` because it is not actually in the deal.
                rejected.add(pid)
                continue

            a = _pick_asset(pid, outcat)
            if a is None:
                break

            deal.legs[from_team_u] = list(deal.legs.get(from_team_u, []) or []) + [a]
            existing.add(pid)
            total_value += float(best.market.total)
            added_any = True
            if total_value >= desired_total_value * (0.90 + 0.10 * rng.random()):
                break

        return added_any

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
        deal = Deal(teams=list(base_deal.teams), legs={k: list(v) for k, v in base_deal.legs.items()}, meta=dict(base_deal.meta or {}))
        protected = _protected_player_ids_from_meta(deal)
        local_seen: Set[str] = set()  # local pre-validate dedupe for sweetener exploration
        # attempt up to 2 additions
        added = 0
        for kind, bucket in actions:
            if added >= 2:
                break
            if kind == "pick" and bucket == "SECOND" and seconds_added >= max_seconds:
                continue
            changed = False
            if kind == "pick":
                changed = self._add_pick_by_bucket(deal, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket=bucket)
                if changed and bucket == "SECOND":
                    seconds_added += 1
            elif kind == "swap":
                changed = self._add_swap_sweetener(deal, from_team=buyer_id, to_team=seller_id, catalog=catalog)

            if not changed:
                continue

            # dedupe (local pre-check; global dedupe happens after repair/validation)
            h_pre = _hash_deal_for_dedupe(deal)
            if h_pre in local_seen:
                continue
            local_seen.add(h_pre)

            # validate with minimal repair if needed (counts attempted validations regardless of outcome)
            if not budget.try_consume_validations(1):
                return None
            try:
                tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
            except TradeError as exc:
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
                )
                if not rep:
                    continue
                if not budget.try_consume_validations(1):
                    return None
                try:
                    tick_ctx.validate_deal(deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                except Exception:
                    continue
            except Exception:
                continue

            # Global dedupe after repair/validation (prevents duplicates from repair paths)
            h_final = _hash_deal_for_dedupe(deal)
            if h_final in seen_deals:
                continue

            added += 1
            # early exit if we've added something meaningful
            if added >= 1 and rng.random() < 0.60:
                break

        if added <= 0:
            return None
        # keep protected meta
        if deal.meta is not None and isinstance(deal.meta, dict):
            deal.meta["protected_player_ids"] = list(sorted(protected))
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

        # Focused tags from FIT_FAILS meta (if available)
        focus_tags = _extract_fit_fail_tags(seller_decision)

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

        # Compute current fit for each outgoing player to seller; swap the worst one.
        def _fit_pid(pid: str) -> float:
            c = buyer_out.players.get(pid)
            if c is None:
                return 0.0
            supply = getattr(c, "supply", None) or {}
            base_fit = _need_fit_score(supply, seller_need_map)
            if focus_tags:
                focused = 0.0
                for t in focus_tags:
                    try:
                        focused += float(supply.get(t, 0.0) or 0.0) * float(seller_need_map.get(t, 1.0) or 1.0)
                    except Exception:
                        continue
                return float(focused)
            return float(base_fit)

        worst_pid: Optional[str] = None
        worst_fit = 1e9
        worst_salary = 0.0
        for a in outgoing_players:
            pid = str(a.player_id)
            if pid in protected:
                continue
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

        def _focused_fit(c: PlayerTradeCandidate) -> float:
            supply = getattr(c, "supply", None) or {}
            if not focus_tags:
                return _need_fit_score(supply, seller_need_map)
            s = 0.0
            for t in focus_tags:
                try:
                    s += float(supply.get(t, 0.0) or 0.0) * float(seller_need_map.get(t, 1.0) or 1.0)
                except Exception:
                    continue
            return float(s)

        def _primary_score(c: PlayerTradeCandidate) -> float:
            ff = _focused_fit(c)
            base_fit = _need_fit_score(getattr(c, "supply", None) or {}, seller_need_map)
            market = float(c.market.total)
            market_norm = market / 50.0  # scale helper (rough)
            age, ry = _age_years(c)
            youth = 0.0
            if age is not None:
                youth += max(0.0, 30.0 - float(age)) / 10.0
            youth += min(4.0, max(0.0, float(ry))) / 4.0

            # If decision gave focused tags, prioritize solving those first
            if rebuild_like:
                # rebuild/retool: youth + years matter most, then focused fit (so it still looks coherent)
                return float(0.55 * youth + 0.30 * ff + 0.15 * base_fit - 0.05 * market_norm)
            if win_now_like:
                # win-now: fit/quality matter most; don't overweight youth
                return float(0.65 * ff + 0.25 * market_norm + 0.10 * base_fit)
            # neutral: balanced
            return float(0.55 * ff + 0.20 * market_norm + 0.15 * youth + 0.10 * base_fit)

        ranked: List[Tuple[float, float, float, str]] = []
        for c in pool:
            # aggregation solo-only cannot be aggregated with others.
            if bool(c.aggregation_solo_only) and len(outgoing_players) >= 2:
                continue
            ff = _focused_fit(c)
            # If we know which tags failed, require the replacement to address them at least a bit
            if focus_tags and float(ff) <= 0.01:
                continue
            primary = _primary_score(c)
            ranked.append((float(primary), abs(float(c.salary_m) - float(worst_salary)), float(c.market.total), c.player_id))
        ranked.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))

        # Try a few top replacements (bounded)
        for f, _, __, new_pid in ranked[:6]:
            # require meaningful improvement
            if float(f) <= float(worst_fit) + 0.03:
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

            # validate + optional minimal repair (counts attempted validations regardless of outcome)
            if not budget.try_consume_validations(1):
                return None
            try:
                tick_ctx.validate_deal(new_deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                return new_deal
            except TradeError as exc:
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
                )
                if not rep:
                    continue
                if not budget.try_consume_validations(1):
                    return None
                try:
                    tick_ctx.validate_deal(new_deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
                    return new_deal
                except Exception:
                    continue
            except Exception:
                continue

        return None

    # ---------------------------------------------------------------------
    # Repair
    # ---------------------------------------------------------------------
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
            # second apron one-for-one: enforce both legs <= 1 player
            if method == "second_apron_one_for_one":
                return _enforce_one_for_one_players(deal, protected_players=protected, catalog=catalog)

            # Otherwise: add outgoing filler on the failing team (most common)
            if team_fail:
                other = seller_id if team_fail == buyer_id else buyer_id
                # Respect "ABOVE_2ND_APRON" heuristic by limiting outgoing players.
                max_out_players = 1 if _team_apron_status(tick_ctx.get_team_situation(team_fail)) == "ABOVE_2ND_APRON" else 4
                gap_m = _estimate_needed_filler_salary_m(details)
                return _add_one_outgoing_filler_player(
                    deal,
                    from_team=team_fail,
                    to_team=other,
                    catalog=catalog,
                    exclude_players=set(protected),
                    max_outgoing_players=max_out_players,
                    target_add_salary_m=gap_m,
                )

            # fallback: try remove one incoming player (non-target)
            return _remove_one_incoming_player(
                deal,
                receiver_team=buyer_id,
                protected_players=protected,
                prefer_remove_high_salary=True,
                catalog=catalog,
            )

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
                return True
            # else add outgoing filler from team_fail
            other = seller_id if team_fail == buyer_id else buyer_id
            return _add_one_outgoing_filler_player(
                deal,
                from_team=team_fail,
                to_team=other,
                catalog=catalog,
                exclude_players=set(protected),
                max_outgoing_players=4,
            )

        # 3) locks/eligibility/return bans/duplicate assets -> prune (no repair)
        if exc.code in {ASSET_LOCKED, DUPLICATE_ASSET}:
            return False

        if rule in {"player_eligibility", "asset_lock", "duplicate_asset", "return_to_trading_team_same_season"}:
            return False

        if rule in {"pick_rules", "ownership", "pick_protection_schema"}:
            # Try to remove the last-added pick/swap (common failure mode for sweeteners)
            for team in (buyer_id, seller_id):
                leg = list(deal.legs.get(team, []) or [])
                # remove last pick first
                for i in range(len(leg) - 1, -1, -1):
                    if isinstance(leg[i], (PickAsset, SwapAsset)):
                        leg.pop(i)
                        deal.legs[team] = leg
                        return True
            return False

        return False

    # ---------------------------------------------------------------------
    # Scoring
    # ---------------------------------------------------------------------
    def _score_deal(
        self,
        deal: Deal,
        *,
        buyer_id: str,
        seller_id: str,
        buyer_decision: DealDecision,
        seller_decision: DealDecision,
        buyer_eval: TeamDealEvaluation,
        seller_eval: TeamDealEvaluation,
        budgets: Mapping[str, int],
        opponent_seen: Mapping[str, int],
        target_seen: Mapping[str, int],
    ) -> float:
        scale = float(self.cfg.sigmoid_scale)
        mb = float(buyer_eval.net_surplus) - float(buyer_decision.required_surplus)
        ms = float(seller_eval.net_surplus) - float(seller_decision.required_surplus)

        # verdict handling
        vb = buyer_decision.verdict.value if hasattr(buyer_decision.verdict, "value") else str(buyer_decision.verdict)
        vs = seller_decision.verdict.value if hasattr(seller_decision.verdict, "value") else str(seller_decision.verdict)
        verdict_bonus = 0.0
        if vb == "ACCEPT":
            verdict_bonus += 0.12
        elif vb == "COUNTER":
            verdict_bonus += 0.05
        else:
            verdict_bonus -= 0.10
        if vs == "ACCEPT":
            verdict_bonus += 0.12
        elif vs == "COUNTER":
            verdict_bonus += 0.05
        else:
            verdict_bonus -= 0.10

        accept_score = _sigmoid(mb / scale) + _sigmoid(ms / scale)
        # punish strongly if buyer is far negative (avoid silly overpays)
        overpay_penalty = max(0.0, -mb) / max(4.0, scale)

        num_assets = _deal_num_assets(deal)
        num_players = _deal_num_players_moved(deal)
        complexity_penalty = self.cfg.complexity_asset_penalty * max(0, num_assets - 2) + self.cfg.complexity_player_penalty * max(0, num_players - 2)

        score = float(accept_score) + float(verdict_bonus) - float(complexity_penalty) - float(overpay_penalty)

        # If either side is hard reject, apply floor penalty.
        if vb == "REJECT" or vs == "REJECT":
            score -= float(self.cfg.reject_floor_penalty)

        return float(score)


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
