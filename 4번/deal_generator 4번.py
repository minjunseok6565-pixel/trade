from __future__ import annotations

"""Bilateral deal generator.

This generator produces *validatable* and *evaluable* deals using:

- TradeGenerationTickContext: tick-scoped caches for rule validation + valuation
- TradeAssetCatalog: tick-scoped tradable pools (players by outgoing buckets, movable picks/swaps,
  league-wide incoming index by need tags)

Core ideas
----------
1) Generate plausible *skeletons* (target + rough return) from catalog pools.
2) Fast prune: cheap prefilters (cooldowns, untouchables, 2nd-apron one-for-one, aggregation bans,
   Stepien precheck) before calling validate_deal.
3) Local search: minimal repair loops for salary matching / roster limit, plus a small
   "sweetener" loop (add picks) when the counterparty is close.
4) Strict budgets: max attempts / max validations / max evaluations; deterministic randomness.

Scope
-----
- Two-team trades only (multi-team can be layered later).
- Fixed assets ignored for now (can be added as another sweetener tier).
- Outputs are deals + bilateral evaluations; orchestrator can decide how/when to submit.
"""

from dataclasses import dataclass, field
from datetime import date
import hashlib
import logging
import math
import random
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from schema import normalize_player_id, normalize_team_id

from ..errors import TradeError, DEAL_INVALIDATED, ROSTER_LIMIT
from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, asset_key
from ..valuation.types import DealDecision, TeamDealEvaluation
from ..valuation.fit_engine import FitEngine, FitEngineConfig

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    IncomingPlayerRef,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
)


logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Public output
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DealGeneratorConfig:
    # Output control
    max_results: int = 12
    include_near_misses: bool = False

    # Search budgets
    max_targets: int = 12
    max_seed_offers_per_target: int = 7
    max_attempts_per_target: int = 18  # (validate+evaluate) attempts
    max_repairs_per_offer: int = 4
    max_sweetener_steps: int = 4

    # Global budgets per generator call
    max_validations: int = 120
    max_evaluations: int = 90

    # Deal shape limits
    max_players_moved_per_team: int = 3
    max_total_players_moved: int = 5
    max_picks_from_one_team: int = 4
    allow_swap_sweeteners: bool = True

    # Realism / guardrails
    allow_core_targets: bool = False
    min_need_weight_for_target: float = 0.18
    max_target_salary_m: float = 55.0  # skip ultra-max outliers unless blockbuster mode

    # Sweetener thresholds
    max_close_shortfall: float = 3.5  # if counterparty shortfall is within this, try sweeteners
    sweetener_value_buffer: float = 0.5  # extra buffer above estimated shortfall

    # Determinism
    rng_seed: Optional[int] = None


@dataclass(frozen=True, slots=True)
class GeneratedDeal:
    deal: Deal
    initiator_team_id: str
    counterparty_team_id: str

    mode: str  # "BUY" or "SELL"
    target_player_id: Optional[str]

    score: float

    initiator_decision: DealDecision
    initiator_eval: TeamDealEvaluation
    counterparty_decision: DealDecision
    counterparty_eval: TeamDealEvaluation

    meta: Dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Small pure helpers
# -----------------------------------------------------------------------------


def _canon_team_id(team_id: Any) -> str:
    return str(normalize_team_id(str(team_id), strict=False)).upper()


def _canon_player_id(player_id: Any) -> str:
    return str(normalize_player_id(str(player_id), strict=False, allow_legacy_numeric=True))


def _resolve_asset_catalog(
    tick_ctx: TradeGenerationTickContext,
    supplied: Optional[TradeAssetCatalog],
) -> TradeAssetCatalog:
    """Resolve the tick-scoped TradeAssetCatalog without requiring generation_tick.py changes.

    Priority:
    1) explicit `supplied`
    2) tick_ctx.get_asset_catalog() if present
    3) tick_ctx.asset_catalog if present and not None
    4) lazy build + (best-effort) assign to tick_ctx.asset_catalog
    """
    if supplied is not None:
        return supplied

    getter = getattr(tick_ctx, "get_asset_catalog", None)
    if callable(getter):
        return getter()

    existing = getattr(tick_ctx, "asset_catalog", None)
    if existing is not None:
        return existing

    # Lazy build (defensive: some call sites may construct tick_ctx without pre-building the catalog)
    from .asset_catalog import build_trade_asset_catalog

    built = build_trade_asset_catalog(tick_ctx=tick_ctx)
    try:
        setattr(tick_ctx, "asset_catalog", built)
    except Exception:
        pass
    return built


def _deal_fingerprint(deal: Deal) -> str:
    """Stable fingerprint for de-duplication (independent of ordering noise).

    NOTE: `deal` is expected to be canonicalized already (e.g. via canonicalize_deal).
    This avoids double-canonicalization in tight generation loops.
    """
    d = deal
    parts: List[str] = []
    parts.append("|".join(d.teams))
    for team in d.teams:
        assets = d.legs.get(team, [])
        a_parts = []
        for a in assets:
            to_team = getattr(a, "to_team", None) or ""
            a_parts.append(f"{asset_key(a)}->{to_team}")
        a_parts.sort()
        parts.append(f"{team}:" + ",".join(a_parts))
    raw = "||".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _is_accept(decision: DealDecision) -> bool:
    return str(getattr(decision, "verdict", "")).upper() == "ACCEPT"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# -----------------------------------------------------------------------------
# Internal builder
# -----------------------------------------------------------------------------


class _BilateralDealBuilder:
    """Utility to build 2-team deals safely (no duplicate assets)."""

    def __init__(self, team_a: str, team_b: str) -> None:
        self.team_a = _canon_team_id(team_a)
        self.team_b = _canon_team_id(team_b)
        self._legs: Dict[str, List[Any]] = {self.team_a: [], self.team_b: []}
        self._seen: Set[str] = set()

    @property
    def teams(self) -> Tuple[str, str]:
        return (self.team_a, self.team_b)

    def _other(self, team: str) -> str:
        t = _canon_team_id(team)
        return self.team_b if t == self.team_a else self.team_a

    def add_player(self, from_team: str, player_id: str, *, to_team: Optional[str] = None) -> None:
        ft = _canon_team_id(from_team)
        tt = _canon_team_id(to_team) if to_team else self._other(ft)
        pid = _canon_player_id(player_id)
        a = PlayerAsset(kind="player", player_id=pid, to_team=tt)
        k = asset_key(a)
        if k in self._seen:
            return
        self._seen.add(k)
        self._legs[ft].append(a)

    def add_pick(self, from_team: str, pick_id: str, *, to_team: Optional[str] = None, protection: Optional[Dict[str, Any]] = None) -> None:
        ft = _canon_team_id(from_team)
        tt = _canon_team_id(to_team) if to_team else self._other(ft)
        a = PickAsset(kind="pick", pick_id=str(pick_id), to_team=tt, protection=protection)
        k = asset_key(a)
        if k in self._seen:
            return
        self._seen.add(k)
        self._legs[ft].append(a)

    def add_swap(self, from_team: str, swap: SwapTradeCandidate, *, to_team: Optional[str] = None) -> None:
        ft = _canon_team_id(from_team)
        tt = _canon_team_id(to_team) if to_team else self._other(ft)
        a = SwapAsset(kind="swap", swap_id=str(swap.swap_id), pick_id_a=str(swap.snap.pick_id_a), pick_id_b=str(swap.snap.pick_id_b), to_team=tt)
        k = asset_key(a)
        if k in self._seen:
            return
        self._seen.add(k)
        self._legs[ft].append(a)

    def count_players_out(self, team: str) -> int:
        t = _canon_team_id(team)
        return sum(1 for a in self._legs.get(t, []) if isinstance(a, PlayerAsset))

    def count_players_in(self, team: str) -> int:
        """Number of incoming PlayerAsset objects for `team` (bilateral).

        We count player assets whose `to_team` resolves to `team` and that originate from
        the other leg.
        """
        t = _canon_team_id(team)
        return sum(
            1
            for sender, assets in self._legs.items()
            if sender != t
            for a in assets
            if isinstance(a, PlayerAsset) and _canon_team_id(getattr(a, "to_team", None) or "") == t
        )

    def total_players_moved(self) -> int:
        return self.count_players_out(self.team_a) + self.count_players_out(self.team_b)

    def pick_ids_out(self, team: str) -> Set[str]:
        t = _canon_team_id(team)
        return {a.pick_id for a in self._legs.get(t, []) if isinstance(a, PickAsset)}

    def pick_ids_in(self, team: str) -> Set[str]:
        """Pick ids incoming to `team` (i.e., sent by the other team to this team)."""
        t = _canon_team_id(team)
        return {
            a.pick_id
            for sender, assets in self._legs.items()
            if sender != t
            for a in assets
            if isinstance(a, PickAsset) and _canon_team_id(getattr(a, "to_team", None) or "") == t
        }

    def build(self, *, meta: Optional[Dict[str, Any]] = None) -> Deal:
        d = Deal(teams=[self.team_a, self.team_b], legs={self.team_a: list(self._legs[self.team_a]), self.team_b: list(self._legs[self.team_b])}, meta=dict(meta or {}))
        return canonicalize_deal(d)

    def clone(self) -> "_BilateralDealBuilder":
        b = _BilateralDealBuilder(self.team_a, self.team_b)
        b._legs = {self.team_a: list(self._legs[self.team_a]), self.team_b: list(self._legs[self.team_b])}
        b._seen = set(self._seen)
        return b


# -----------------------------------------------------------------------------
# Deal generator
# -----------------------------------------------------------------------------


class DealGenerator:
    def __init__(
        self,
        tick_ctx: TradeGenerationTickContext,
        *,
        asset_catalog: Optional[TradeAssetCatalog] = None,
        config: Optional[DealGeneratorConfig] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.tick_ctx = tick_ctx
        self.catalog = _resolve_asset_catalog(tick_ctx, asset_catalog)
        self.cfg = config or DealGeneratorConfig()

        self._external_rng = rng is not None
        self._base_seed: Optional[int] = None

        if rng is not None:
            self.rng = rng
        else:
            if self.cfg.rng_seed is not None:
                seed = int(self.cfg.rng_seed)
            else:
                # Deterministic per tick by default.
                cd: date = getattr(tick_ctx, "current_date", date.today())
                seed = int(cd.toordinal())
            self._base_seed = int(seed)
            self.rng = random.Random(int(seed))

        # Swap pricing cache (for sweeteners)
        self._swap_value_cache: Dict[str, float] = {}
        self._market_pricer = None  # lazy MarketPricer
        # Fingerprints attempted this tick (includes invalid attempts to avoid repeated invalid spam).
        self._seen_deals: Set[str] = set()
        # Fingerprints that validated successfully (useful for debugging/telemetry).
        self._seen_valid_deals: Set[str] = set()
        self._validation_count = 0
        self._evaluation_count = 0

        # Fit engine (SSOT) for counterparty-oriented return-piece selection
        self._fit_engine = FitEngine(config=FitEngineConfig())

        # Cache: team_id -> set(player_id) that are plausibly "on the market" this tick.
        # Built from non-CORE outgoing buckets, to avoid targeting untouchables.
        self._market_listed_players_cache: Dict[str, Set[str]] = {}

    # ------------------------
    # Public entrypoint
    # ------------------------

    def generate_deals_for_team(self, team_id: str) -> List[GeneratedDeal]:
        """Generate a ranked list of bilateral deals initiated by team_id."""
        initiator = _canon_team_id(team_id)
        ts = self.tick_ctx.get_team_situation(initiator)
        posture = str(getattr(ts, "trade_posture", "STAND_PAT")).upper()
        mode = "BUY" if posture in {"AGGRESSIVE_BUY", "SOFT_BUY"} else "SELL" if posture in {"SELL", "SOFT_SELL"} else "BUY"

        if getattr(getattr(ts, "constraints", None), "cooldown_active", False):
            return []

        # Use a deterministic *team-scoped* RNG stream by default.
        # This avoids different teams producing overly synchronized random patterns when the
        # orchestrator instantiates a generator per team.
        if (not self._external_rng) and (self._base_seed is not None):
            with self._scoped_rng(self._team_rng(initiator)):
                results = self._generate_deals_for_team_mode(initiator, mode)
        else:
            results = self._generate_deals_for_team_mode(initiator, mode)

        results.sort(key=lambda x: (-x.score, x.counterparty_team_id, x.target_player_id or ""))
        return results[: self.cfg.max_results]

    def _generate_deals_for_team_mode(self, initiator: str, mode: str) -> List[GeneratedDeal]:
        """Internal helper to generate deals; assumes self.rng is already configured."""
        results: List[GeneratedDeal] = []

        if mode == "BUY":
            targets = self._select_targets_for_buyer(initiator)
            for ref in targets:
                if self._should_stop(results):
                    break
                deals = self._search_for_target(initiator, ref)
                for gd in deals:
                    results.append(gd)
                    if self._should_stop(results):
                        break
        else:
            sell_targets = self._select_sell_candidates(initiator)
            for cand in sell_targets:
                if self._should_stop(results):
                    break
                deals = self._search_for_sell_candidate(initiator, cand)
                for gd in deals:
                    results.append(gd)
                    if self._should_stop(results):
                        break

        return results

    def _derive_team_seed(self, team_id: str, *, salt: str = "dealgen") -> int:
        base = str(self._base_seed if self._base_seed is not None else 0)
        tid = _canon_team_id(team_id)
        raw = f"{base}|{tid}|{salt}"
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        # Use a stable 32-bit integer seed.
        return int(h[:8], 16)

    def _team_rng(self, team_id: str) -> random.Random:
        return random.Random(self._derive_team_seed(team_id))

    @contextmanager
    def _scoped_rng(self, rng: random.Random):
        old = self.rng
        self.rng = rng
        try:
            yield
        finally:
            self.rng = old

    def _log_internal_exception(
        self,
        exc: Exception,
        *,
        stage: str,
        initiator: str,
        counterparty: str,
        fp: Optional[str] = None,
    ) -> None:
        try:
            logger.exception(
                "DealGenerator internal error at %s (initiator=%s counterparty=%s fp=%s)",
                stage,
                initiator,
                counterparty,
                fp,
                exc_info=exc,
            )
        except Exception:
            # Never let logging crash generation.
            pass

    def _estimate_swap_market_value(self, swap: SwapTradeCandidate) -> float:
        """Estimate swap market value using MarketPricer + provider snapshots (cached per tick).

        If pricing fails for any reason, fall back to a conservative constant.
        """
        sid = str(getattr(swap, "swap_id", "") or "")
        if not sid:
            return 2.0
        cached = self._swap_value_cache.get(sid)
        if cached is not None:
            return float(cached)

        # Lazy import/initialization to avoid import cycles at module load time.
        try:
            if self._market_pricer is None:
                from ..valuation.market_pricing import MarketPricer

                self._market_pricer = MarketPricer()

            provider = getattr(self.tick_ctx, "provider", None)
            if provider is None:
                raise AttributeError("tick_ctx.provider missing")

            snap = getattr(swap, "snap", None)
            if snap is None:
                raise AttributeError("swap.snap missing")

            pick_a = provider.get_pick_snapshot(str(getattr(snap, "pick_id_a", "") or ""))
            pick_b = provider.get_pick_snapshot(str(getattr(snap, "pick_id_b", "") or ""))
            exp_a = provider.get_pick_expectation(pick_a.pick_id)
            exp_b = provider.get_pick_expectation(pick_b.pick_id)

            mv = self._market_pricer.price_snapshot(
                snap,
                asset_key=f"swap:{sid}",
                resolved_pick_a=pick_a,
                resolved_pick_b=pick_b,
                resolved_pick_a_expectation=exp_a,
                resolved_pick_b_expectation=exp_b,
            )
            now = float(getattr(getattr(mv, "value", None), "now", 0.0) or 0.0)
            fut = float(getattr(getattr(mv, "value", None), "future", 0.0) or 0.0)
            total = now + fut

            # Conservative dampening/clamp: avoid over-using swaps as cheap fillers.
            total = _clamp(total * 0.75, 0.5, 8.0)
        except Exception:
            total = 2.0

        self._swap_value_cache[sid] = float(total)
        return float(total)

    # ------------------------
    # Target selection
    # ------------------------

    def _top_need_tags(self, team_id: str, k: int = 4) -> List[Tuple[str, float]]:
        dc = self.tick_ctx.get_decision_context(team_id)
        need_map = dict(getattr(dc, "need_map", {}) or {})
        items = [(str(tag), float(w or 0.0)) for tag, w in need_map.items()]
        items.sort(key=lambda x: (-x[1], x[0]))
        out: List[Tuple[str, float]] = []
        for tag, w in items:
            if w < float(self.cfg.min_need_weight_for_target):
                continue
            out.append((tag, w))
            if len(out) >= k:
                break
        return out

    def _market_listed_players(self, team_id: str) -> Set[str]:
        """Return a cached set of player_ids that this team is plausibly willing to move this tick.

        We intentionally restrict BUY-side targeting to these players to prevent "why is that guy
        even available?" moments. This is derived from the catalog's non-CORE outgoing buckets.
        """

        tid = _canon_team_id(team_id)
        cached = self._market_listed_players_cache.get(tid)
        if cached is not None:
            return cached

        out = self.catalog.outgoing_by_team.get(tid)
        if out is None:
            self._market_listed_players_cache[tid] = set()
            return self._market_listed_players_cache[tid]

        allowed_buckets = (
            "FILLER_BAD_CONTRACT",
            "FILLER_CHEAP",
            "SURPLUS_LOW_FIT",
            "SURPLUS_REDUNDANT",
            "EXPIRING",
            "VETERAN_SALE",
            "CONSOLIDATE",
        )

        s: Set[str] = set()
        for b in allowed_buckets:
            for pid in out.player_ids_by_bucket.get(b, ()):
                s.add(_canon_player_id(pid))

        # Optional blockbuster mode: allow a *very small* number of CORE players to be targeted,
        # but only when the seller context suggests unusual availability (rebuild/sell/high urgency).
        # This keeps realism while making `allow_core_targets=True` actually meaningful.
        if self.cfg.allow_core_targets:
            ts = self.tick_ctx.get_team_situation(tid)
            posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "").upper()
            horizon = str(getattr(ts, "time_horizon", "") or "").upper()
            urgency = float(getattr(ts, "urgency", 0.0) or 0.0)

            if posture in {"SELL", "SOFT_SELL"} or horizon == "REBUILD" or urgency >= 0.85:
                core_ids = list(out.player_ids_by_bucket.get("CORE", ()) or ())
                ranked: List[Tuple[float, str]] = []
                for pid in core_ids:
                    pidc = _canon_player_id(pid)
                    cand = out.players.get(pidc)
                    if cand is None:
                        continue
                    # Tradability check; to_team is irrelevant for "market availability" caching.
                    if not self._is_tradable_player(cand, tid, to_team=None):
                        continue
                    ranked.append((float(cand.market.total), pidc))
                ranked.sort(key=lambda t: (-t[0], t[1]))
                top_n = 2 if urgency >= 0.92 else 1
                for _, pidc in ranked[:top_n]:
                    s.add(pidc)

        self._market_listed_players_cache[tid] = s
        return s

    def _select_targets_for_buyer(self, buyer_team_id: str) -> List[IncomingPlayerRef]:
        buyer = _canon_team_id(buyer_team_id)
        ts_buyer = self.tick_ctx.get_team_situation(buyer)
        dc_buyer = self.tick_ctx.get_decision_context(buyer)

        # Candidate pool: union of incoming_by_need_tag lists for top needs.
        needs = self._top_need_tags(buyer, k=5)
        if not needs:
            # Fallback: broaden (still deterministic) by taking strongest need tags from catalog keys.
            all_tags = sorted(self.catalog.incoming_by_need_tag.keys())
            needs = [(t, 0.20) for t in all_tags[:3]]

        pool: List[Tuple[float, IncomingPlayerRef]] = []
        seen_pid: Set[str] = set()

        for tag, w_need in needs:
            refs = self.catalog.incoming_by_need_tag.get(tag, tuple())
            for ref in refs[: max(20, self.cfg.max_targets * 4)]:
                pid = _canon_player_id(ref.player_id)
                if pid in seen_pid:
                    continue
                from_team = _canon_team_id(ref.from_team)
                if from_team == buyer:
                    continue
                # Market-level cooldown: don't spam teams already throttled.
                ts_seller = self.tick_ctx.get_team_situation(from_team)
                if getattr(getattr(ts_seller, "constraints", None), "cooldown_active", False):
                    continue

                # Only target tradable players (catalog-aware: not locked, not recent signing).
                out_cat = self.catalog.outgoing_by_team.get(from_team)
                if out_cat is None:
                    continue
                cand = out_cat.players.get(pid)
                if cand is None:
                    continue

                # Hard realism gate: target must be "on the market" per seller's outgoing buckets.
                # This avoids hitting players that are technically tradable but would never be shopped.
                if pid not in self._market_listed_players(from_team):
                    continue
                if not self._is_tradable_player(cand, from_team, to_team=buyer):
                    continue
                if not self.cfg.allow_core_targets and ("CORE" in (cand.buckets or ())):
                    continue
                if float(cand.salary_m or 0.0) > float(self.cfg.max_target_salary_m):
                    continue

                # Priority score: need weight * tag strength * (small premium for lower salary for flexibility).
                strength = float(ref.tag_strength or 0.0)
                salary_pen = 1.0 / (1.0 + 0.03 * float(ref.salary_m or 0.0))
                tier = str(getattr(ts_buyer, "competitive_tier", ""))
                urgency = float(getattr(dc_buyer, "urgency", 0.5) or 0.5)
                win_now = float(getattr(getattr(dc_buyer, "effective_traits", None), "win_now", 0.5) or 0.5)
                tier_bonus = 1.05 if tier in {"CONTENDER", "PLAYOFF_BUYER"} else 1.0

                # Seller willingness signal: SELL teams are more likely to move value assets.
                seller_posture = str(getattr(ts_seller, "trade_posture", "STAND_PAT")).upper()
                seller_time = str(getattr(ts_seller, "time_horizon", "")).upper()
                if seller_posture in {"SELL", "SOFT_SELL"} or seller_time == "REBUILD":
                    sell_willing = 1.12
                elif seller_posture in {"AGGRESSIVE_BUY", "SOFT_BUY"}:
                    sell_willing = 0.92
                else:
                    sell_willing = 1.0
                score = (w_need * strength) * salary_pen * tier_bonus * (0.90 + 0.20 * urgency + 0.10 * win_now)
                score *= sell_willing

                pool.append((score, ref))
                seen_pid.add(pid)

        pool.sort(key=lambda x: (-x[0], x[1].from_team, x[1].player_id))
        return [ref for _, ref in pool[: self.cfg.max_targets]]

    def _select_sell_candidates(self, seller_team_id: str) -> List[PlayerTradeCandidate]:
        seller = _canon_team_id(seller_team_id)
        out = self.catalog.outgoing_by_team.get(seller)
        if out is None:
            return []
        ts = self.tick_ctx.get_team_situation(seller)
        posture = str(getattr(ts, "trade_posture", "STAND_PAT")).upper()

        # For sellers, focus on veteran sale / expiring / surplus buckets.
        bucket_priority = ["VETERAN_SALE", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_BAD_CONTRACT", "FILLER_CHEAP"]
        if posture in {"SOFT_SELL", "SELL"}:
            bucket_priority = ["VETERAN_SALE", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_BAD_CONTRACT", "FILLER_CHEAP"]
        else:
            bucket_priority = ["SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "EXPIRING", "FILLER_BAD_CONTRACT", "FILLER_CHEAP"]

        cands: List[PlayerTradeCandidate] = []
        seen: Set[str] = set()
        for b in bucket_priority:
            ids = list((out.player_ids_by_bucket.get(b) or ()))
            for pid in ids:
                if pid in seen:
                    continue
                cand = out.players.get(pid)
                if cand is None:
                    continue
                if not self._is_tradable_player(cand, seller, to_team=None):
                    continue
                if not self.cfg.allow_core_targets and ("CORE" in (cand.buckets or ())):
                    continue
                cands.append(cand)
                seen.add(pid)
                if len(cands) >= self.cfg.max_targets:
                    break
            if len(cands) >= self.cfg.max_targets:
                break

        # Rank: higher market now + expiring pressure + lower fit.
        def _rank(c: PlayerTradeCandidate) -> float:
            exp = 0.8 if c.is_expiring else 0.0
            return float(c.market.now + 0.6 * c.market.future + exp + 0.7 * (1.0 - c.fit_vs_team))

        cands.sort(key=lambda c: (-_rank(c), c.player_id))
        return cands[: self.cfg.max_targets]

    # ------------------------
    # Core search loops
    # ------------------------

    def _search_for_target(self, buyer_team_id: str, target_ref: IncomingPlayerRef) -> List[GeneratedDeal]:
        buyer = _canon_team_id(buyer_team_id)
        seller = _canon_team_id(target_ref.from_team)
        if buyer == seller:
            return []

        # Get concrete candidate snapshots from catalog.
        seller_out = self.catalog.outgoing_by_team.get(seller)
        buyer_out = self.catalog.outgoing_by_team.get(buyer)
        if seller_out is None or buyer_out is None:
            return []

        target_pid = _canon_player_id(target_ref.player_id)
        target_cand = seller_out.players.get(target_pid)
        if target_cand is None:
            return []

        # Seed offers (small set) then local search.
        seeds = self._build_seed_offers_for_target(buyer, seller, target_cand)
        out: List[GeneratedDeal] = []

        attempts = 0
        for seed in seeds[: self.cfg.max_seed_offers_per_target]:
            if self._should_stop(out):
                break
            if attempts >= self.cfg.max_attempts_per_target:
                break
            attempts += 1

            gd = self._validate_repair_evaluate(seed, initiator=buyer, counterparty=seller, mode="BUY", target_pid=target_pid)
            if gd is None:
                continue
            out.append(gd)

        out.sort(key=lambda x: (-x.score, x.counterparty_team_id, x.target_player_id or ""))
        return out[: max(1, min(3, self.cfg.max_results))]

    def _search_for_sell_candidate(self, seller_team_id: str, sell_cand: PlayerTradeCandidate) -> List[GeneratedDeal]:
        seller = _canon_team_id(seller_team_id)
        target_pid = _canon_player_id(sell_cand.player_id)

        # Choose likely buyers (30 teams only; cheap to score).
        buyer_ids = self._select_buyers_for_player(seller, sell_cand)

        out: List[GeneratedDeal] = []
        attempts = 0
        for buyer in buyer_ids:
            if self._should_stop(out):
                break
            if attempts >= self.cfg.max_attempts_per_target:
                break
            buyer_out = self.catalog.outgoing_by_team.get(buyer)
            seller_out = self.catalog.outgoing_by_team.get(seller)
            if buyer_out is None or seller_out is None:
                continue

            # Seller sends the player; buyer sends return.
            seeds = self._build_seed_offers_for_sell(seller, buyer, sell_cand)
            for seed in seeds[: max(1, self.cfg.max_seed_offers_per_target // 2)]:
                if self._should_stop(out):
                    break
                attempts += 1
                gd = self._validate_repair_evaluate(seed, initiator=seller, counterparty=buyer, mode="SELL", target_pid=target_pid)
                if gd is None:
                    continue
                out.append(gd)

        out.sort(key=lambda x: (-x.score, x.counterparty_team_id, x.target_player_id or ""))
        return out[: max(1, min(3, self.cfg.max_results))]

    # ------------------------
    # Offer construction
    # ------------------------

    def _build_seed_offers_for_target(self, buyer: str, seller: str, target: PlayerTradeCandidate) -> List[_BilateralDealBuilder]:
        """Buyer wants target from seller."""
        b_out = self.catalog.outgoing_by_team.get(buyer)
        s_out = self.catalog.outgoing_by_team.get(seller)
        if b_out is None or s_out is None:
            return []

        ts_b = self.tick_ctx.get_team_situation(buyer)
        ts_s = self.tick_ctx.get_team_situation(seller)
        dc_b = self.tick_ctx.get_decision_context(buyer)
        dc_s = self.tick_ctx.get_decision_context(seller)
        # NOTE: Second-apron 1-for-1 is enforced using payroll_after semantics in _second_apron_shape_ok().

        target_salary_m = float(target.salary_m or 0.0)
        cap_space = float(getattr(getattr(ts_b, "constraints", None), "cap_space", 0.0) or 0.0) / 1_000_000.0
        can_picks_only = (cap_space >= target_salary_m - 0.25)

        # Desired return market value heuristic (seller perspective).
        min_surplus = float(getattr(getattr(dc_s, "knobs", None), "min_surplus_required", 0.0) or 0.0)
        pick_mul = float(getattr(getattr(dc_s, "knobs", None), "pick_multiplier", 1.0) or 1.0)
        youth_mul = float(getattr(getattr(dc_s, "knobs", None), "youth_multiplier", 1.0) or 1.0)
        pref_bonus = max(0.75, 0.55 + 0.35 * max(pick_mul, youth_mul))
        desired = float(target.market.total + min_surplus) / pref_bonus

        seeds: List[_BilateralDealBuilder] = []

        # Seed 0: need-fit return piece + picks.
        seller_win_now, seller_rebuild = self._counterparty_intent(ts_s)
        return_piece = self._choose_return_player_for_counterparty(
            from_team=buyer,
            to_team=seller,
            from_out=b_out,
            counter_ts=ts_s,
            counter_dc=dc_s,
            required_salary_m=(target_salary_m if seller_win_now else None),
            max_market_total=(18.0 if seller_win_now else 16.0 if seller_rebuild else 18.0),
        )
        if return_piece is not None:
            b0 = _BilateralDealBuilder(buyer, seller)
            b0.add_player(seller, target.player_id, to_team=buyer)
            b0.add_player(buyer, return_piece.player_id, to_team=seller)
            self._add_picks_for_value(
                b0,
                from_team=buyer,
                to_team=seller,
                desired_value=max(0.0, desired - float(return_piece.market.total)),
                b_out=b_out,
                soft=bool(seller_rebuild and not seller_win_now),
            )
            seeds.append(b0)

        # Seed A: picks-only (only when buyer has cap room to absorb)
        if can_picks_only:
            b = _BilateralDealBuilder(buyer, seller)
            b.add_player(seller, target.player_id, to_team=buyer)
            self._add_picks_for_value(b, from_team=buyer, to_team=seller, desired_value=desired, b_out=b_out)
            seeds.append(b)

        # Seed B: 1 salary filler + picks
        b = _BilateralDealBuilder(buyer, seller)
        b.add_player(seller, target.player_id, to_team=buyer)
        filler = self._choose_salary_filler_team_to_send(
            from_team=buyer,
            to_team=seller,
            required_salary_m=target_salary_m,
            team_out=b_out,
            single_player_only=False,
        )
        if filler is not None:
            b.add_player(buyer, filler.player_id, to_team=seller)
        self._add_picks_for_value(b, from_team=buyer, to_team=seller, desired_value=max(0.0, desired - (filler.market.total if filler else 0.0)), b_out=b_out)
        seeds.append(b)

        # Seed C: consolidation piece + smaller picks (buyers often send a mid piece)
        mid_piece = self._choose_consolidation_piece(buyer, seller, b_out, single_player_only=False)
        if mid_piece is not None:
            b2 = _BilateralDealBuilder(buyer, seller)
            b2.add_player(seller, target.player_id, to_team=buyer)
            b2.add_player(buyer, mid_piece.player_id, to_team=seller)
            # smaller pick package
            self._add_picks_for_value(b2, from_team=buyer, to_team=seller, desired_value=max(0.0, desired - mid_piece.market.total), b_out=b_out, soft=True)
            seeds.append(b2)

        # Shuffle lightly for diversity (still deterministic via rng)
        self.rng.shuffle(seeds)
        return seeds

    def _build_seed_offers_for_sell(self, seller: str, buyer: str, sell_cand: PlayerTradeCandidate) -> List[_BilateralDealBuilder]:
        """Seller offers sell_cand to buyer."""
        s_out = self.catalog.outgoing_by_team.get(seller)
        b_out = self.catalog.outgoing_by_team.get(buyer)
        if s_out is None or b_out is None:
            return []

        ts_b = self.tick_ctx.get_team_situation(buyer)
        dc_b = self.tick_ctx.get_decision_context(buyer)
        ts_s = self.tick_ctx.get_team_situation(seller)
        dc_s = self.tick_ctx.get_decision_context(seller)

        desired = float(sell_cand.market.total)  # baseline
        min_surplus_buyer = float(getattr(getattr(dc_b, "knobs", None), "min_surplus_required", 0.0) or 0.0)
        # Buyer should not overpay too much; ask slightly below to allow acceptance.
        desired = max(0.0, desired - 0.35 * min_surplus_buyer)

        seeds: List[_BilateralDealBuilder] = []

        # Seed 0: need-fit return piece + picks (seller's perspective).
        seller_win_now, seller_rebuild = self._counterparty_intent(ts_s)
        return_piece = self._choose_return_player_for_counterparty(
            from_team=buyer,
            to_team=seller,
            from_out=b_out,
            counter_ts=ts_s,
            counter_dc=dc_s,
            required_salary_m=(float(sell_cand.salary_m or 0.0) if seller_win_now else None),
            max_market_total=(18.0 if seller_win_now else 16.0 if seller_rebuild else 18.0),
        )
        if return_piece is not None:
            b0 = _BilateralDealBuilder(seller, buyer)
            b0.add_player(seller, sell_cand.player_id, to_team=buyer)
            b0.add_player(buyer, return_piece.player_id, to_team=seller)
            self._add_picks_for_value(
                b0,
                from_team=buyer,
                to_team=seller,
                desired_value=max(0.0, desired - float(return_piece.market.total)),
                b_out=b_out,
                soft=bool(seller_rebuild and not seller_win_now),
            )
            seeds.append(b0)

        # Seed A: buyer sends picks only if cap room.
        cap_space_m = float(getattr(getattr(ts_b, "constraints", None), "cap_space", 0.0) or 0.0) / 1_000_000.0
        if cap_space_m >= float(sell_cand.salary_m or 0.0) - 0.25:
            b = _BilateralDealBuilder(seller, buyer)
            b.add_player(seller, sell_cand.player_id, to_team=buyer)
            self._add_picks_for_value(b, from_team=buyer, to_team=seller, desired_value=desired, b_out=b_out)
            seeds.append(b)

        # Seed B: salary filler + picks
        b = _BilateralDealBuilder(seller, buyer)
        b.add_player(seller, sell_cand.player_id, to_team=buyer)
        filler = self._choose_salary_filler_team_to_send(
            from_team=buyer,
            to_team=seller,
            required_salary_m=float(sell_cand.salary_m or 0.0),
            team_out=b_out,
            single_player_only=False,
        )
        if filler is not None:
            b.add_player(buyer, filler.player_id, to_team=seller)
        self._add_picks_for_value(b, from_team=buyer, to_team=seller, desired_value=max(0.0, desired - (filler.market.total if filler else 0.0)), b_out=b_out)
        seeds.append(b)

        self.rng.shuffle(seeds)
        return seeds

    # ------------------------
    # Validation / repair / evaluation
    # ------------------------

    def _validate_repair_evaluate(
        self,
        builder: _BilateralDealBuilder,
        *,
        initiator: str,
        counterparty: str,
        mode: str,
        target_pid: Optional[str],
    ) -> Optional[GeneratedDeal]:
        """Validate + (repair if needed) + evaluate; returns GeneratedDeal on success."""

        # Deal shape constraints (cheap prune)
        if builder.total_players_moved() > self.cfg.max_total_players_moved:
            return None
        if builder.count_players_out(initiator) > self.cfg.max_players_moved_per_team:
            return None
        if builder.count_players_out(counterparty) > self.cfg.max_players_moved_per_team:
            return None

        # Second apron one-for-one guard (cheap prune)
        if not self._second_apron_shape_ok(builder, initiator) or not self._second_apron_shape_ok(builder, counterparty):
            return None

        # Aggregation-ban guard (cheap prune)
        if not self._aggregation_shape_ok(builder, initiator) or not self._aggregation_shape_ok(builder, counterparty):
            return None

        # Stepien precheck for outgoing pick sets (cheap prune)
        if not self._stepien_ok(builder, initiator) or not self._stepien_ok(builder, counterparty):
            return None

        # Try validate/repair loop
        cur = builder
        last_err: Optional[TradeError] = None
        for _ in range(max(1, self.cfg.max_repairs_per_offer)):
            if self._validation_count >= self.cfg.max_validations:
                return None

            deal: Optional[Deal] = None
            fp: Optional[str] = None
            stage = "build/fingerprint"
            try:
                deal = cur.build(meta={"gen_mode": mode, "target": target_pid})
                fp = _deal_fingerprint(deal)
                if fp in self._seen_deals:
                    return None
                # Record attempted fingerprints even if validation fails, to avoid repeated invalid spam.
                self._seen_deals.add(fp)

                stage = "validate/evaluate"
                self.tick_ctx.validate_deal(deal, integrity_check=False)
                self._validation_count += 1
                self._seen_valid_deals.add(fp)
                # Evaluate (initiator-first to save work on obvious rejects)
                if self._evaluation_count >= self.cfg.max_evaluations:
                    return None

                # Prefer tick_ctx wrapper if available, otherwise fall back to service API.
                eval_one = getattr(self.tick_ctx, "evaluate_deal_for_team", None)
                if callable(eval_one):
                    a_dec, a_eval = eval_one(
                        deal,
                        initiator,
                        include_breakdown=False,
                        include_package_effects=True,
                        allow_counter=False,
                        rng=self.rng,
                        rng_seed=None,
                        validate=False,
                    )
                else:
                    from ..valuation.service import evaluate_deal_for_team as _eval_deal_for_team
                    a_dec, a_eval = _eval_deal_for_team(
                        deal,
                        initiator,
                        tick_ctx=self.tick_ctx,
                        include_breakdown=False,
                        include_package_effects=True,
                        allow_counter=False,
                        rng=self.rng,
                        rng_seed=None,
                        validate=False,
                    )
                self._evaluation_count += 1

                # If initiator rejects, skip counterparty evaluation entirely.
                if not _is_accept(a_dec):
                    return None

                if self._evaluation_count >= self.cfg.max_evaluations:
                    return None

                if callable(eval_one):
                    b_dec, b_eval = eval_one(
                        deal,
                        counterparty,
                        include_breakdown=False,
                        include_package_effects=True,
                        allow_counter=False,
                        rng=self.rng,
                        rng_seed=None,
                        validate=False,
                    )
                else:
                    from ..valuation.service import evaluate_deal_for_team as _eval_deal_for_team
                    b_dec, b_eval = _eval_deal_for_team(
                        deal,
                        counterparty,
                        tick_ctx=self.tick_ctx,
                        include_breakdown=False,
                        include_package_effects=True,
                        allow_counter=False,
                        rng=self.rng,
                        rng_seed=None,
                        validate=False,
                    )
                self._evaluation_count += 1

                # Local sweetener loop when counterparty is close but rejecting.
                if (not _is_accept(b_dec)) and _is_accept(a_dec):
                    repaired = self._try_sweeten_to_accept(
                        base_builder=cur,
                        giver=initiator,
                        receiver=counterparty,
                        counterparty_dec=b_dec,
                        counterparty_eval=b_eval,
                    )
                    if repaired is not None:
                        cur = repaired
                        continue  # validate/evaluate again

                # Accept criteria
                if _is_accept(a_dec) and _is_accept(b_dec):
                    score = self._score_deal(initiator, a_dec, a_eval, b_dec, b_eval, cur)
                    return GeneratedDeal(
                        deal=deal,
                        initiator_team_id=initiator,
                        counterparty_team_id=counterparty,
                        mode=mode,
                        target_player_id=target_pid,
                        score=score,
                        initiator_decision=a_dec,
                        initiator_eval=a_eval,
                        counterparty_decision=b_dec,
                        counterparty_eval=b_eval,
                        meta={"fingerprint": fp},
                    )

                # Optionally keep near misses (useful for future negotiation/counter)
                if self.cfg.include_near_misses:
                    shortfall = self._shortfall_amount(b_dec, b_eval)
                    if shortfall <= self.cfg.max_close_shortfall:
                        score = self._score_deal(initiator, a_dec, a_eval, b_dec, b_eval, cur) - 5.0
                        return GeneratedDeal(
                            deal=deal,
                            initiator_team_id=initiator,
                            counterparty_team_id=counterparty,
                            mode=mode,
                            target_player_id=target_pid,
                            score=score,
                            initiator_decision=a_dec,
                            initiator_eval=a_eval,
                            counterparty_decision=b_dec,
                            counterparty_eval=b_eval,
                            meta={"fingerprint": fp, "near_miss": True},
                        )

                return None

            except TradeError as e:
                self._validation_count += 1
                last_err = e
                fixed = self._repair_after_validation_error(cur, initiator=initiator, counterparty=counterparty, err=e)
                if fixed is None:
                    break
                cur = fixed
                continue
            except Exception as e:
                # Unknown error (bug, unexpected snapshot shape, etc.).
                # Do not allow generation to crash the orchestration tick.
                self._validation_count += 1
                self._log_internal_exception(e, stage=stage, initiator=initiator, counterparty=counterparty, fp=fp)
                return None

        # Failed to produce a valid/evaluable deal.
        _ = last_err
        return None

    # ------------------------
    # Repairs / sweeteners
    # ------------------------

    def _repair_after_validation_error(
        self,
        builder: _BilateralDealBuilder,
        *,
        initiator: str,
        counterparty: str,
        err: TradeError,
    ) -> Optional[_BilateralDealBuilder]:
        """Minimal, deterministic repairs for common generation-time invalidations."""
        details = getattr(err, "details", None)
        code = str(getattr(err, "code", ""))

        # Roster limit: add outgoing cheap filler from the team that would overflow.
        if code == ROSTER_LIMIT:
            team = None
            if isinstance(details, Mapping):
                team = details.get("team_id")
            team = _canon_team_id(team) if team else None
            if not team:
                return None
            return self._try_fix_roster_limit(builder, team)

        # Rule-based invalidations (details may carry {rule: ...}).
        rule = None
        if isinstance(details, Mapping):
            rule = details.get("rule") or details.get("reason")
        rule = str(rule or "")

        if code == DEAL_INVALIDATED and rule == "salary_matching":
            team = None
            if isinstance(details, Mapping):
                team = details.get("team_id")
            team = _canon_team_id(team) if team else None
            if not team:
                return None
            # Second apron one-for-one failures are not repairable with small edits.
            method = str(details.get("method") or "") if isinstance(details, Mapping) else ""
            if method == "second_apron_one_for_one":
                return None
            return self._try_fix_salary_matching(builder, failing_team=team, details=(details if isinstance(details, Mapping) else None))

        # Recently traded players cannot be aggregated -> reduce outgoing players to 1.
        if code == DEAL_INVALIDATED and rule == "player_eligibility":
            if isinstance(details, Mapping) and details.get("reason") == "aggregation_ban":
                team = _canon_team_id(details.get("team_id"))
                return self._reduce_outgoing_players_to_one(builder, team)
            return None

        # Many invalidations are not worth repairing here (ownership, locks, deadline, etc.).
        return None

    def _try_fix_salary_matching(
        self,
        builder: _BilateralDealBuilder,
        *,
        failing_team: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> Optional[_BilateralDealBuilder]:
        """Attempt a minimal salary-matching repair using SalaryMatchingRule details.

        We use the rule's structured fields (allowed_in, outgoing_salary, incoming_salary, method)
        to choose a plausible *salary filler* from the failing team that actually fixes the mismatch.
        """

        team = _canon_team_id(failing_team)
        other = builder._other(team)

        out = self.catalog.outgoing_by_team.get(team)
        if out is None:
            return None

        # If an aggregation-solo-only player is already outgoing, cannot add another.
        if not self._aggregation_shape_ok(builder, team):
            return None

        # Parse rule details (values are in dollars in the validator).
        d = dict(details or {})
        try:
            outgoing_base = float(d.get("outgoing_salary") or 0.0)
            incoming_salary = float(d.get("incoming_salary") or 0.0)
            allowed_base = float(d.get("allowed_in") or 0.0)
            payroll_before = float(d.get("payroll_before") or 0.0)
        except Exception:
            outgoing_base = 0.0
            incoming_salary = 0.0
            allowed_base = 0.0
            payroll_before = 0.0

        if incoming_salary <= 0.0:
            return None

        shortfall = incoming_salary - allowed_base
        # If details didn't include allowed_in (or it was 0), compute from scratch.
        if shortfall <= 0.0:
            allowed_calc, _, _ = self._salary_allowed_in(
                payroll_before=payroll_before,
                outgoing_salary=outgoing_base,
                incoming_salary=incoming_salary,
                outgoing_players=int(builder.count_players_out(team)),
                incoming_players=int(builder.count_players_out(other)),
            )
            shortfall = incoming_salary - allowed_calc
            if shortfall <= 0.0:
                return None

        # Candidate ids by priority. Prefer "filler" types first.
        priorities = [
            "FILLER_BAD_CONTRACT",
            "EXPIRING",
            "FILLER_CHEAP",
            "SURPLUS_LOW_FIT",
            "SURPLUS_REDUNDANT",
            "CONSOLIDATE",
        ]
        existing = {a.player_id for a in builder._legs.get(team, []) if isinstance(a, PlayerAsset)}

        outgoing_players_base = int(builder.count_players_out(team))
        incoming_players_base = int(builder.count_players_out(other))

        # Target additional outgoing in $ (very rough). We'll use this to prefer minimal overkill.
        needed_additional = max(0.0, shortfall)

        best: Optional[Tuple[float, str]] = None  # (score, player_id)

        for b in priorities:
            for pid in out.player_ids_by_bucket.get(b, ()):
                if pid in existing:
                    continue
                cand = out.players.get(pid)
                if cand is None:
                    continue
                if not self._is_tradable_player(cand, team, to_team=other):
                    continue

                add_salary = float(cand.salary_m or 0.0) * 1_000_000.0
                if add_salary <= 0.0:
                    continue

                outgoing_new = outgoing_base + add_salary
                allowed_new, status_new, method_new = self._salary_allowed_in(
                    payroll_before=payroll_before,
                    outgoing_salary=outgoing_new,
                    incoming_salary=incoming_salary,
                    outgoing_players=outgoing_players_base + 1,
                    incoming_players=incoming_players_base,
                )
                new_shortfall = incoming_salary - allowed_new

                # Penalize candidates that still don't fix the mismatch.
                still_bad_pen = 50.0 if new_shortfall > 0.0 else 0.0

                # Prefer minimal overkill and minimal value cost.
                overkill = max(0.0, allowed_new - incoming_salary)
                overkill_m = overkill / 1_000_000.0
                need_m = needed_additional / 1_000_000.0
                add_m = add_salary / 1_000_000.0

                # "Filler" heuristic: avoid using real assets as salary ballast.
                value_pen = float(cand.market.total) * 0.35
                # Prefer salaries near what we need (avoid sending 20M when we need 2M).
                size_pen = abs(add_m - need_m) * 0.30
                # Prefer expiring/bad contracts as fillers (small negative bonus).
                exp_bonus = -0.25 if bool(cand.is_expiring) else 0.0
                # Method/status can be informative for edge cases (2nd apron / cap-space).
                status_pen = 0.50 if str(status_new) == "SECOND_APRON" and (outgoing_players_base + 1) > 1 else 0.0
                method_pen = 0.15 if str(method_new).startswith("outgoing_") else 0.0

                score = still_bad_pen + overkill_m * 0.12 + value_pen + size_pen + status_pen + method_pen + exp_bonus

                if best is None or score < best[0]:
                    best = (score, pid)

            # If we already found a clean fix in early buckets, don't wander too far.
            if best is not None and best[0] < 8.0:
                break

        if best is None:
            return None

        pid_best = best[1]
        nb = builder.clone()
        nb.add_player(team, pid_best, to_team=other)
        if nb.count_players_out(team) > self.cfg.max_players_moved_per_team:
            return None
        if nb.total_players_moved() > self.cfg.max_total_players_moved:
            return None
        if not self._aggregation_shape_ok(nb, team):
            return None
        # Enforce second-apron 1-for-1 using payroll_after semantics.
        if (not self._second_apron_shape_ok(nb, team)) or (not self._second_apron_shape_ok(nb, other)):
            return None
        return nb

    def _salary_allowed_in(
        self,
        *,
        payroll_before: float,
        outgoing_salary: float,
        incoming_salary: float,
        outgoing_players: int,
        incoming_players: int,
    ) -> Tuple[float, str, str]:
        """Compute allowed_in using the league trade_rules, mirroring SalaryMatchingRule.

        Returns (allowed_in_dollars, status, method).
        """

        # Pull rules from the tick snapshot (read-only).
        league = (getattr(self.tick_ctx.rule_tick_ctx, "ctx_state_base", {}) or {}).get("league", {})
        trade_rules = (league or {}).get("trade_rules", {}) or {}

        salary_cap = float(trade_rules.get("salary_cap") or 0.0)
        first_apron = float(trade_rules.get("first_apron") or 0.0)
        second_apron = float(trade_rules.get("second_apron") or 0.0)
        match_small_out_max = float(trade_rules.get("match_small_out_max") or 7_500_000)
        match_mid_out_max = float(trade_rules.get("match_mid_out_max") or 29_000_000)
        match_mid_add = float(trade_rules.get("match_mid_add") or 7_500_000)
        match_buffer = float(trade_rules.get("match_buffer") or 250_000)
        first_apron_mult = float(trade_rules.get("first_apron_mult") or 1.10)
        second_apron_mult = float(trade_rules.get("second_apron_mult") or 1.00)

        payroll_after = float(payroll_before) - float(outgoing_salary) + float(incoming_salary)
        status = "BELOW_FIRST_APRON"
        if payroll_after >= second_apron:
            status = "SECOND_APRON"
        elif payroll_after >= first_apron:
            status = "FIRST_APRON"

        # Cap-space method (same as rule): only applies if incoming fits within cap_room + outgoing.
        if payroll_before < salary_cap:
            cap_room = salary_cap - payroll_before
            max_incoming = cap_room + outgoing_salary
            if incoming_salary <= max_incoming:
                return float(max_incoming), status, "cap_space"

        if outgoing_salary <= 0.0:
            return 0.0, status, "outgoing_required"

        if status == "SECOND_APRON":
            if outgoing_players > 1 or incoming_players > 1:
                return 0.0, status, "second_apron_one_for_one"
            allowed_in = math.floor(outgoing_salary * second_apron_mult)
            return float(allowed_in), status, "outgoing_second_apron"
        if status == "FIRST_APRON":
            allowed_in = math.floor(outgoing_salary * first_apron_mult)
            return float(allowed_in), status, "outgoing_first_apron"

        # Below first apron: classic 125%/150%+ buffers.
        if outgoing_salary <= match_small_out_max:
            allowed_in = 2 * outgoing_salary + match_buffer
        elif outgoing_salary <= match_mid_out_max:
            allowed_in = outgoing_salary + match_mid_add
        else:
            allowed_in = math.floor(outgoing_salary * 1.25) + match_buffer
        return float(allowed_in), status, "outgoing_below_first_apron"

    def _try_fix_roster_limit(self, builder: _BilateralDealBuilder, team: str) -> Optional[_BilateralDealBuilder]:
        """If team would exceed roster limit, send one extra cheap outgoing player."""
        t = _canon_team_id(team)
        out = self.catalog.outgoing_by_team.get(t)
        if out is None:
            return None
        other = builder._other(t)

        existing = {a.player_id for a in builder._legs.get(t, []) if isinstance(a, PlayerAsset)}
        for b in ["FILLER_CHEAP", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT"]:
            for pid in out.player_ids_by_bucket.get(b, ()):
                if pid in existing:
                    continue
                cand = out.players.get(pid)
                if cand is None:
                    continue
                if not self._is_tradable_player(cand, t, to_team=other):
                    continue
                nb = builder.clone()
                nb.add_player(t, pid, to_team=other)
                if nb.count_players_out(t) > self.cfg.max_players_moved_per_team:
                    continue
                if nb.total_players_moved() > self.cfg.max_total_players_moved:
                    continue
                if not self._aggregation_shape_ok(nb, t):
                    continue
                if (not self._second_apron_shape_ok(nb, t)) or (not self._second_apron_shape_ok(nb, other)):
                    continue
                return nb
        return None

    def _reduce_outgoing_players_to_one(self, builder: _BilateralDealBuilder, team: str) -> Optional[_BilateralDealBuilder]:
        """Drop outgoing players for team until only one remains (keep the highest-salary piece)."""
        t = _canon_team_id(team)
        leg = list(builder._legs.get(t, []))
        players = [a for a in leg if isinstance(a, PlayerAsset)]
        if len(players) <= 1:
            return None

        out = self.catalog.outgoing_by_team.get(t)
        if out is None:
            return None

        # Keep the highest salary outgoing player (best for matching).
        def sal(a: PlayerAsset) -> float:
            cand = out.players.get(_canon_player_id(a.player_id))
            return float(getattr(cand, "salary_m", 0.0) or 0.0)

        keep = max(players, key=sal)
        nb = _BilateralDealBuilder(builder.team_a, builder.team_b)
        # Re-add all assets except the removed outgoing players.
        for team_id in [builder.team_a, builder.team_b]:
            for a in builder._legs.get(team_id, []):
                if team_id == t and isinstance(a, PlayerAsset) and _canon_player_id(a.player_id) != _canon_player_id(keep.player_id):
                    continue
                if isinstance(a, PlayerAsset):
                    nb.add_player(team_id, a.player_id, to_team=a.to_team)
                elif isinstance(a, PickAsset):
                    nb.add_pick(team_id, a.pick_id, to_team=a.to_team, protection=a.protection)
                elif isinstance(a, SwapAsset):
                    # Recreate swap object lightly.
                    nb._legs[_canon_team_id(team_id)].append(a)
                    nb._seen.add(asset_key(a))
        return nb

    def _try_sweeten_to_accept(
        self,
        *,
        base_builder: _BilateralDealBuilder,
        giver: str,
        receiver: str,
        counterparty_dec: DealDecision,
        counterparty_eval: TeamDealEvaluation,
    ) -> Optional[_BilateralDealBuilder]:
        """If receiver is close, add picks from giver in small steps."""
        shortfall = self._shortfall_amount(counterparty_dec, counterparty_eval)
        if shortfall <= 0:
            return None
        if shortfall > float(self.cfg.max_close_shortfall):
            return None

        giver = _canon_team_id(giver)
        receiver = _canon_team_id(receiver)
        out = self.catalog.outgoing_by_team.get(giver)
        if out is None:
            return None

        pool = self._sweetener_pool_for_team(out)
        if not pool:
            return None

        desired = float(shortfall + self.cfg.sweetener_value_buffer)
        nb = base_builder.clone()
        added = 0
        value_added = 0.0

        # Respect pick count limits.
        existing_picks = len(nb.pick_ids_out(giver))
        for kind, val, obj in pool:
            if added >= self.cfg.max_sweetener_steps:
                break
            if value_added >= desired:
                break
            if kind == "PICK":
                if existing_picks + added >= self.cfg.max_picks_from_one_team:
                    continue
                pick: PickTradeCandidate = obj
                # Stepien precheck after adding this pick.
                outgoing = set(nb.pick_ids_out(giver)) | {pick.pick_id}
                incoming = set(nb.pick_ids_in(giver))
                if not self.catalog.stepien.is_compliant_after(team_id=giver, outgoing_pick_ids=outgoing, incoming_pick_ids=incoming):
                    continue
                nb.add_pick(giver, pick.pick_id, to_team=receiver, protection=pick.snap.protection)
                added += 1
                value_added += float(val)
            elif kind == "SWAP" and self.cfg.allow_swap_sweeteners:
                swap: SwapTradeCandidate = obj
                nb.add_swap(giver, swap, to_team=receiver)
                added += 1
                value_added += float(val)

        if added <= 0:
            return None
        return nb

    def _shortfall_amount(self, decision: DealDecision, evaluation: TeamDealEvaluation) -> float:
        """Positive means receiver needs more value to accept."""
        required = float(getattr(decision, "required_surplus", 0.0) or 0.0)
        net = float(getattr(evaluation, "net_surplus", 0.0) or 0.0)
        return float(required - net)

    # ------------------------
    # Cheap feasibility checks
    # ------------------------

    def _team_salary_totals(self, builder: _BilateralDealBuilder, team: str) -> Tuple[float, float, float, float]:
        """Return (payroll_before, outgoing_salary, incoming_salary, payroll_after) in dollars.

        Uses TradeRuleTickContext.ensure_active_roster_index() (SSOT for salaries/payroll).
        """
        t = _canon_team_id(team)
        rtc = getattr(self.tick_ctx, "rule_tick_ctx", None)
        if rtc is None:
            return 0.0, 0.0, 0.0, 0.0
        try:
            rtc.ensure_active_roster_index()
        except Exception:
            pass

        payroll_before = float(getattr(rtc, "team_payroll_before_map", {}).get(t, 0.0) or 0.0)
        sal_map = getattr(rtc, "player_salary_map", {}) or {}

        outgoing_ids = [
            _canon_player_id(a.player_id)
            for a in builder._legs.get(t, [])
            if isinstance(a, PlayerAsset)
        ]
        incoming_ids = [
            _canon_player_id(a.player_id)
            for sender, assets in builder._legs.items()
            if sender != t
            for a in assets
            if isinstance(a, PlayerAsset) and _canon_team_id(getattr(a, "to_team", None) or "") == t
        ]

        outgoing_salary = float(sum(float(sal_map.get(pid) or 0.0) for pid in outgoing_ids))
        incoming_salary = float(sum(float(sal_map.get(pid) or 0.0) for pid in incoming_ids))
        payroll_after = payroll_before - outgoing_salary + incoming_salary
        return payroll_before, outgoing_salary, incoming_salary, payroll_after

    def _resolve_apron_status_after(self, payroll_after: float) -> str:
        """Resolve apron status using the same semantics as SalaryMatchingRule (_resolve_apron_status)."""
        league = (getattr(getattr(self.tick_ctx, "rule_tick_ctx", None), "ctx_state_base", {}) or {}).get("league", {})
        trade_rules = (league or {}).get("trade_rules", {}) or {}
        first_apron = float(trade_rules.get("first_apron") or 0.0)
        second_apron = float(trade_rules.get("second_apron") or 0.0)
        if payroll_after >= second_apron:
            return "SECOND_APRON"
        if payroll_after >= first_apron:
            return "FIRST_APRON"
        return "BELOW_FIRST_APRON"

    def _requires_second_apron_one_for_one(self, builder: _BilateralDealBuilder, team: str) -> bool:
        """True iff the team is SECOND_APRON after the deal *and* is taking in salary."""
        _, _, incoming_salary, payroll_after = self._team_salary_totals(builder, team)
        if incoming_salary <= 0.0:
            return False
        return self._resolve_apron_status_after(float(payroll_after)) == "SECOND_APRON"

    def _second_apron_shape_ok(self, builder: _BilateralDealBuilder, team: str) -> bool:
        """Cheap prune matching SalaryMatchingRule semantics.

        SalaryMatchingRule enforces 1-for-1 only when payroll_after is in SECOND_APRON
        and the team is taking in salary (incoming_salary > 0).
        """
        t = _canon_team_id(team)
        if not self._requires_second_apron_one_for_one(builder, t):
            return True
        # 1-for-1: <=1 outgoing player AND <=1 incoming player.
        if builder.count_players_out(t) > 1:
            return False
        if builder.count_players_in(t) > 1:
            return False
        return True

    def _aggregation_shape_ok(self, builder: _BilateralDealBuilder, team: str) -> bool:
        """If any outgoing player is aggregation-solo-only, outgoing player count must be <= 1."""
        t = _canon_team_id(team)
        out = self.catalog.outgoing_by_team.get(t)
        if out is None:
            return True
        players = [a for a in builder._legs.get(t, []) if isinstance(a, PlayerAsset)]
        if len(players) <= 1:
            return True
        for a in players:
            cand = out.players.get(_canon_player_id(a.player_id))
            if cand is not None and bool(getattr(cand, "aggregation_solo_only", False)):
                return False
        return True

    def _stepien_ok(self, builder: _BilateralDealBuilder, team: str) -> bool:
        t = _canon_team_id(team)
        outgoing = set(builder.pick_ids_out(t))
        if not outgoing:
            return True
        incoming = set(builder.pick_ids_in(t))
        return bool(self.catalog.stepien.is_compliant_after(team_id=t, outgoing_pick_ids=outgoing, incoming_pick_ids=incoming))

    # ------------------------
    # Candidate selection helpers
    # ------------------------

    def _is_tradable_player(self, cand: PlayerTradeCandidate, from_team: str, *, to_team: Optional[str]) -> bool:
        if cand.lock.is_locked:
            return False
        # recent signing ban baked into outgoing buckets, but keep defensive.
        if cand.recent_signing_banned_until:
            try:
                banned_until = date.fromisoformat(str(cand.recent_signing_banned_until))
                if self.tick_ctx.current_date < banned_until:
                    return False
            except Exception:
                pass
        if to_team is not None:
            tt = _canon_team_id(to_team)
            if tt in set(cand.return_ban_teams or ()):  # same-season return ban
                return False
        return True

    def _select_buyers_for_player(self, seller: str, player: PlayerTradeCandidate) -> List[str]:
        seller = _canon_team_id(seller)
        tags = list(player.top_tags or ())
        if not tags:
            # fallback: use any supply tag with value >= 0.6
            tags = [t for t, v in (player.supply or {}).items() if float(v or 0.0) >= 0.6][:3]
        if not tags:
            tags = []

        scored: List[Tuple[float, str]] = []
        for team_id in self.catalog.outgoing_by_team.keys():
            tid = _canon_team_id(team_id)
            if tid == seller:
                continue
            ts = self.tick_ctx.get_team_situation(tid)
            if getattr(getattr(ts, "constraints", None), "cooldown_active", False):
                continue
            dc = self.tick_ctx.get_decision_context(tid)
            need_map = dict(getattr(dc, "need_map", {}) or {})

            fit = 0.0
            for tag in tags:
                fit = max(fit, float(need_map.get(tag, 0.0) or 0.0) * float(player.supply.get(tag, 0.0) or 0.0))
            urgency = float(getattr(dc, "urgency", 0.5) or 0.5)
            posture = str(getattr(ts, "trade_posture", "STAND_PAT")).upper()
            posture_mul = 1.12 if posture in {"AGGRESSIVE_BUY", "SOFT_BUY"} else 1.0
            scored.append((fit * posture_mul * (0.85 + 0.30 * urgency), tid))

        scored.sort(key=lambda x: (-x[0], x[1]))
        # Keep only the most plausible few.
        return [tid for _, tid in scored[:8]]

    def _counterparty_intent(self, ts: Any) -> Tuple[bool, bool]:
        """Classify what the counterparty is likely to want *right now*.

        Returns (is_win_now, is_rebuild).
        """
        tier = str(getattr(ts, "competitive_tier", "") or "").upper()
        posture = str(getattr(ts, "trade_posture", "") or "").upper()
        horizon = str(getattr(ts, "time_horizon", "") or "").upper()

        is_win_now = (horizon == "WIN_NOW") or (tier in {"CONTENDER", "PLAYOFF_BUYER"}) or (posture in {"AGGRESSIVE_BUY", "SOFT_BUY"})
        is_rebuild = (horizon == "REBUILD") or (tier in {"REBUILD", "TANK"}) or (posture in {"SELL", "SOFT_SELL"})
        return bool(is_win_now), bool(is_rebuild)

    def _choose_return_player_for_counterparty(
        self,
        *,
        from_team: str,
        to_team: str,
        from_out: TeamOutgoingCatalog,
        counter_ts: Any,
        counter_dc: Any,
        exclude_player_ids: Optional[Set[str]] = None,
        required_salary_m: Optional[float] = None,
        max_market_total: float = 20.0,
    ) -> Optional[PlayerTradeCandidate]:
        """Pick a player return piece that the counterparty is likely to value.

        Uses FitEngine SSOT to score fit vs counterparty needs, with different preferences for:
        - WIN_NOW: higher fit + higher market.now (immediate impact)
        - REBUILD: youth / expiring / short contracts; fit is secondary
        """
        ft = _canon_team_id(from_team)
        tt = _canon_team_id(to_team)

        is_win_now, is_rebuild = self._counterparty_intent(counter_ts)
        need_map = dict(getattr(counter_dc, "need_map", {}) or {})
        exclude: Set[str] = set(_canon_player_id(p) for p in (exclude_player_ids or set()))

        priorities = ["CONSOLIDATE", "SURPLUS_REDUNDANT", "SURPLUS_LOW_FIT", "EXPIRING", "FILLER_BAD_CONTRACT", "FILLER_CHEAP"]
        if is_rebuild and not is_win_now:
            priorities = ["EXPIRING", "SURPLUS_REDUNDANT", "SURPLUS_LOW_FIT", "CONSOLIDATE", "FILLER_BAD_CONTRACT", "FILLER_CHEAP"]

        def fit_score(c: PlayerTradeCandidate) -> float:
            if not need_map:
                return 0.50
            try:
                f, _, _ = self._fit_engine.score_fit(need_map, c.supply or {})
                return float(f)
            except Exception:
                # Defensive fallback: crude weighted dot-product
                s = 0.0
                total_w = 0.0
                for tag, w in need_map.items():
                    ww = float(w or 0.0)
                    if ww <= 0.0:
                        continue
                    total_w += ww
                    s += ww * float((c.supply or {}).get(tag, 0.0) or 0.0)
                return float(s / total_w) if total_w > 1e-9 else 0.50

        def youth_factor(c: PlayerTradeCandidate) -> float:
            age = getattr(getattr(c, "snap", None), "age", None)
            a = float(age) if age is not None else 99.0
            if a <= 24.5:
                return 1.00
            if a <= 26.5:
                return 0.72
            if a <= 29.5:
                return 0.45
            return 0.25

        scored: List[Tuple[float, PlayerTradeCandidate]] = []
        seen: Set[str] = set()

        for b in priorities:
            ids = list(from_out.player_ids_by_bucket.get(b, ()))[:28]
            for pid in ids:
                pidc = _canon_player_id(pid)
                if pidc in seen or pidc in exclude:
                    continue
                seen.add(pidc)
                cand = from_out.players.get(pidc)
                if cand is None:
                    continue
                if not self._is_tradable_player(cand, ft, to_team=tt):
                    continue
                if not self.cfg.allow_core_targets and ("CORE" in (cand.buckets or ())):
                    continue
                if float(cand.market.total) > float(max_market_total):
                    continue

                sal = float(cand.salary_m or 0.0)
                if sal <= 0.0:
                    continue

                f = fit_score(cand)
                now = float(cand.market.now or 0.0)
                tot = float(cand.market.total or 0.0)

                if is_win_now and not is_rebuild:
                    util = 0.62 * f + 0.30 * _clamp(now / 12.0, 0.0, 1.0) + 0.08 * _clamp(tot / 15.0, 0.0, 1.0)
                    if b in {"FILLER_CHEAP", "FILLER_BAD_CONTRACT"}:
                        util -= 0.10
                elif is_rebuild and not is_win_now:
                    y = youth_factor(cand)
                    exp = 1.0 if bool(getattr(cand, "is_expiring", False)) else 0.0
                    short = 1.0 if float(getattr(cand, "remaining_years", 99.0) or 99.0) <= 2.0 + 1e-9 else 0.0
                    util = 0.55 * y + 0.18 * exp + 0.17 * short + 0.10 * f
                    if b == "FILLER_BAD_CONTRACT" and y < 0.70:
                        util -= 0.12
                else:
                    y = youth_factor(cand)
                    util = 0.45 * f + 0.25 * _clamp(now / 12.0, 0.0, 1.0) + 0.20 * y + 0.10 * (1.0 if cand.is_expiring else 0.0)

                if b in {"SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT"}:
                    util += 0.08
                elif b == "EXPIRING":
                    util += 0.05
                elif b == "CONSOLIDATE":
                    util += 0.03

                if required_salary_m is not None:
                    req = float(required_salary_m or 0.0)
                    util -= 0.04 * abs(sal - req)

                util -= 0.025 * tot
                scored.append((util, cand))

        if not scored:
            return None

        scored.sort(key=lambda t: (-t[0], t[1].player_id))
        top = scored[:6]
        base = top[-1][0]
        weights = [max(0.01, (s - base) + 0.02) for s, _ in top]
        idx = int(self.rng.choices(list(range(len(top))), weights=weights, k=1)[0])
        return top[idx][1]

    # ------------------------
    # Package components
    # ------------------------

    def _choose_salary_filler_team_to_send(
        self,
        *,
        from_team: str,
        to_team: str,
        required_salary_m: float,
        team_out: TeamOutgoingCatalog,
        single_player_only: bool,
    ) -> Optional[PlayerTradeCandidate]:
        """Pick a salary-matching-ish outgoing player with minimal value loss."""
        ft = _canon_team_id(from_team)
        tt = _canon_team_id(to_team)

        # Candidate ids by priority. Prefer low market total per salary (bad contract / expiring).
        buckets = ["FILLER_BAD_CONTRACT", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "CONSOLIDATE", "FILLER_CHEAP"]
        cands: List[PlayerTradeCandidate] = []
        seen: Set[str] = set()
        for b in buckets:
            for pid in team_out.player_ids_by_bucket.get(b, ()):
                if pid in seen:
                    continue
                cand = team_out.players.get(pid)
                if cand is None:
                    continue
                if not self._is_tradable_player(cand, ft, to_team=tt):
                    continue
                # If the player is aggregation-solo-only, treat it as "single player only".
                if cand.aggregation_solo_only and not single_player_only:
                    # We can still use it, but then we cannot add any other outgoing players for ft.
                    pass
                cands.append(cand)
                seen.add(pid)
                if len(cands) >= 18:
                    break
            if len(cands) >= 18:
                break

        if not cands:
            return None

        # If 2nd apron or we want a single player, choose closest salary to required.
        req = float(required_salary_m or 0.0)

        def score(c: PlayerTradeCandidate) -> float:
            # lower is better
            sal = float(c.salary_m or 0.0)
            # penalize huge value pieces as "filler"
            value_pen = float(c.market.total) * 0.25
            # prefer expiring / bad value
            exp = -0.35 if c.is_expiring else 0.0
            return abs(sal - req) + value_pen + exp

        cands.sort(key=lambda c: (score(c), c.player_id))
        # Filter out obviously tiny salaries when we need to match a big incoming.
        for c in cands:
            if req <= 6.0:
                return c
            if float(c.salary_m or 0.0) >= max(2.0, req * 0.18):
                return c
        return cands[0]

    def _choose_consolidation_piece(self, buyer: str, seller: str, b_out: TeamOutgoingCatalog, *, single_player_only: bool) -> Optional[PlayerTradeCandidate]:
        bt = _canon_team_id(buyer)
        st = _canon_team_id(seller)
        ids = list(b_out.player_ids_by_bucket.get("CONSOLIDATE", ())) + list(b_out.player_ids_by_bucket.get("SURPLUS_REDUNDANT", ()))
        self.rng.shuffle(ids)
        for pid in ids[:12]:
            cand = b_out.players.get(pid)
            if cand is None:
                continue
            if not self._is_tradable_player(cand, bt, to_team=st):
                continue
            if single_player_only and cand.aggregation_solo_only:
                # still okay, but then we should not add other outgoing players.
                pass
            if cand.market.total >= 12.0:
                # Avoid sending star-level pieces in this seed.
                continue
            return cand
        return None

    def _add_picks_for_value(
        self,
        builder: _BilateralDealBuilder,
        *,
        from_team: str,
        to_team: str,
        desired_value: float,
        b_out: TeamOutgoingCatalog,
        soft: bool = False,
    ) -> None:
        """Greedy pick selection from from_team to reach desired market value."""
        ft = _canon_team_id(from_team)
        tt = _canon_team_id(to_team)
        desired = max(0.0, float(desired_value or 0.0))

        # Pick tiers: SECOND -> FIRST_SAFE -> FIRST_SENSITIVE.
        # (soft=True uses fewer expensive assets.)
        second_ids = list(b_out.pick_ids_by_bucket.get("SECOND", ()))
        first_safe_ids = list(b_out.pick_ids_by_bucket.get("FIRST_SAFE", ()))
        first_sens_ids = list(b_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()))

        # Deterministic shuffle for variety.
        self.rng.shuffle(second_ids)
        self.rng.shuffle(first_safe_ids)
        self.rng.shuffle(first_sens_ids)

        # Order: for soft offers, prefer seconds then one first.
        tiers: List[List[str]] = [second_ids, first_safe_ids, first_sens_ids]
        if soft:
            tiers = [second_ids, first_safe_ids] + [first_sens_ids]

        total = 0.0
        picks_added = 0
        existing = set(builder.pick_ids_out(ft))

        for tier in tiers:
            for pid in tier:
                if picks_added >= self.cfg.max_picks_from_one_team:
                    return
                if pid in existing:
                    continue
                pick = b_out.picks.get(pid)
                if pick is None:
                    continue
                # Defensive: catalog should already filter these, but keep generator robust.
                if pick.lock.is_locked or not bool(getattr(pick, "within_max_years", True)):
                    continue
                # Stepien precheck after adding.
                outgoing = set(builder.pick_ids_out(ft)) | {pid}
                incoming = set(builder.pick_ids_in(ft))
                if not self.catalog.stepien.is_compliant_after(team_id=ft, outgoing_pick_ids=outgoing, incoming_pick_ids=incoming):
                    continue

                builder.add_pick(ft, pid, to_team=tt, protection=pick.snap.protection)
                existing.add(pid)
                picks_added += 1
                total += float(pick.market.total)

                # stop conditions
                if desired <= 0.01:
                    return
                if total >= desired:
                    return

    def _sweetener_pool_for_team(self, out: TeamOutgoingCatalog) -> List[Tuple[str, float, Any]]:
        """Return [(kind, market_value, obj)] sorted cheap->expensive."""
        items: List[Tuple[str, float, Any]] = []

        # Seconds as tiny sweeteners.
        for pid in out.pick_ids_by_bucket.get("SECOND", ()):
            p = out.picks.get(pid)
            if p is None:
                continue
            if p.lock.is_locked or not bool(getattr(p, "within_max_years", True)):
                continue
            items.append(("PICK", float(p.market.total), p))

        # Then safe firsts.
        for pid in out.pick_ids_by_bucket.get("FIRST_SAFE", ()):
            p = out.picks.get(pid)
            if p is None:
                continue
            if p.lock.is_locked or not bool(getattr(p, "within_max_years", True)):
                continue
            items.append(("PICK", float(p.market.total), p))

        # Sensitive firsts last (still usable).
        for pid in out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()):
            p = out.picks.get(pid)
            if p is None:
                continue
            if p.lock.is_locked or not bool(getattr(p, "within_max_years", True)):
                continue
            items.append(("PICK", float(p.market.total), p))

        if self.cfg.allow_swap_sweeteners:
            for sid in out.swap_ids or ():
                s = out.swaps.get(sid)
                if s is None:
                    continue
                if s.lock.is_locked:
                    continue
                # Estimate swap market value using the same MarketPricer SSOT used by valuation.
                val = self._estimate_swap_market_value(s)
                items.append(("SWAP", float(val), s))

        items.sort(key=lambda t: (t[1], t[0]))
        return items

    # ------------------------
    # Scoring and stopping
    # ------------------------

    def _score_deal(
        self,
        initiator: str,
        a_dec: DealDecision,
        a_eval: TeamDealEvaluation,
        b_dec: DealDecision,
        b_eval: TeamDealEvaluation,
        builder: _BilateralDealBuilder,
    ) -> float:
        """Rank deals primarily by initiator surplus, with mild realism penalties."""
        init = _canon_team_id(initiator)
        init_eval = a_eval if _canon_team_id(a_eval.team_id) == init else b_eval
        opp_eval = b_eval if init_eval is a_eval else a_eval

        s = float(getattr(init_eval, "net_surplus", 0.0) or 0.0)
        opp = float(getattr(opp_eval, "net_surplus", 0.0) or 0.0)

        # Penalize very large lopsidedness (often indicates a generation bug or weird pricing).
        lopsided = abs(s - opp)
        lopsided_pen = 0.10 * max(0.0, lopsided - 6.0)

        # Penalize complexity.
        player_pen = 0.8 * float(builder.total_players_moved())
        pick_pen = 0.25 * float(len(builder.pick_ids_out(builder.team_a)) + len(builder.pick_ids_out(builder.team_b)))

        return s + 0.08 * opp - lopsided_pen - player_pen - pick_pen

    def _should_stop(self, results: Sequence[GeneratedDeal]) -> bool:
        if len(results) >= self.cfg.max_results:
            return True
        if self._validation_count >= self.cfg.max_validations:
            return True
        if self._evaluation_count >= self.cfg.max_evaluations:
            return True
        return False
