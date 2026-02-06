from __future__ import annotations

"""asset_catalog.py

Tick-scoped "tradable asset catalog" for deal generation.

Goal
----
Provide NBA-like candidate pools (outgoing buckets per team + league-wide incoming
index by need tags + movable picks/swaps with Stepien/locks preprocessed) so a
deal generator can explore many candidates without repeatedly scanning the DB.

Design
------
- Built once per generation tick (uses TradeGenerationTickContext).
- Deterministic ordering (stable sorting + player_id tie-breaks).
- Uses existing SSOT logic:
  - locks: same semantics as AssetLockRule (expires_at + allow_locked_by_deal_id)
  - player bans: same policy helpers used by rules/team_situation
  - fit: FitEngine SSOT
  - market: MarketPricer SSOT
  - Stepien: stepien_policy SSOT (shared with PickRulesRule)
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Literal

# LeagueRepo lives at project root in current layout.
try:
    from league_repo import LeagueRepo  # type: ignore
except Exception:  # pragma: no cover
    from trade.league_repo import LeagueRepo  # type: ignore

# Config (team list)
try:
    from config import ALL_TEAM_IDS  # type: ignore
except Exception:  # pragma: no cover
    from trade.config import ALL_TEAM_IDS  # type: ignore

from ..models import PlayerAsset, PickAsset, SwapAsset, asset_key as _asset_key
from ..rules.policies.player_ban_policy import (
    compute_aggregation_banned_until,
    compute_recent_signing_banned_until,
)
from ..rules.policies.stepien_policy import check_stepien_violation

from ..valuation.fit_engine import FitEngine, FitEngineConfig
from ..valuation.market_pricing import MarketPricer, MarketPricingConfig
from ..valuation.data_context import contract_snapshot_from_dict
from ..valuation.types import (
    PlayerSnapshot,
    PickSnapshot,
    SwapSnapshot,
    ContractSnapshot,
    ValueComponents,
)

# TradeGenerationTickContext import kept local to avoid circular import at module load.


# =============================================================================
# Small helpers (pure)
# =============================================================================
def _canon_team_id(team_id: Any) -> str:
    raw = str(team_id or "").strip()
    if not raw:
        return ""
    try:
        from schema import normalize_team_id  # type: ignore

        return str(normalize_team_id(raw, strict=False)).strip().upper()
    except Exception:
        return raw.upper()


def _canon_player_id(player_id: Any) -> str:
    raw = str(player_id or "").strip()
    if not raw:
        return ""
    try:
        from schema import normalize_player_id  # type: ignore

        return str(normalize_player_id(raw, strict=False, allow_legacy_numeric=True)).strip()
    except Exception:
        return raw


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return int(default)
        return int(x)
    except Exception:
        return int(default)


def _safe_float(x: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _to_iso(d: Optional[date]) -> Optional[str]:
    if d is None:
        return None
    try:
        return d.isoformat()
    except Exception:
        return None


def _parse_iso_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return None


def _remaining_years_from_contract(contract: Optional[ContractSnapshot], season_year: int) -> float:
    """Compute remaining years (inclusive) from contract snapshot.

    Mirrors TeamSituationEvaluator._remaining_years_for_player semantics.
    """
    if contract is None:
        return 0.0

    start = _safe_int(getattr(contract, "start_season_year", None), 0)
    years = _safe_int(getattr(contract, "years", None), 0)
    end_year: Optional[int] = None
    if start > 0 and years > 0:
        end_year = start + years - 1
    else:
        try:
            keys = [int(k) for k in (getattr(contract, "salary_by_year", {}) or {}).keys()]
            end_year = max(keys) if keys else None
        except Exception:
            end_year = None

    if end_year is None:
        return 0.0
    if season_year > end_year:
        return 0.0
    return float(end_year - season_year + 1)


# =============================================================================
# Data structures
# =============================================================================
@dataclass(frozen=True, slots=True)
class MarketValueSummary:
    now: float
    future: float
    total: float

    @staticmethod
    def from_components(vc: ValueComponents) -> "MarketValueSummary":
        now = float(getattr(vc, "now", 0.0) or 0.0)
        fut = float(getattr(vc, "future", 0.0) or 0.0)
        return MarketValueSummary(now=now, future=fut, total=now + fut)


@dataclass(frozen=True, slots=True)
class LockInfo:
    is_locked: bool
    expires_at: Optional[str] = None
    deal_id: Optional[str] = None


BucketId = Literal[
    "CORE",
    "FILLER_BAD_CONTRACT",
    "FILLER_CHEAP",
    "SURPLUS_LOW_FIT",
    "SURPLUS_REDUNDANT",
    "EXPIRING",
    "VETERAN_SALE",
    "CONSOLIDATE",
]


@dataclass(frozen=True, slots=True)
class PlayerTradeCandidate:
    player_id: str
    team_id: str

    snap: PlayerSnapshot
    market: MarketValueSummary

    supply: Dict[str, float]
    top_tags: Tuple[str, ...]

    fit_vs_team: float
    surplus_score: float

    salary_m: float
    remaining_years: float
    is_expiring: bool

    lock: LockInfo
    recent_signing_banned_until: Optional[str]
    aggregation_banned_until: Optional[str]
    aggregation_solo_only: bool
    return_ban_teams: Tuple[str, ...]

    buckets: Tuple[BucketId, ...] = field(default_factory=tuple)

    def as_asset(self, to_team: Optional[str] = None) -> PlayerAsset:
        return PlayerAsset(kind="player", player_id=self.player_id, to_team=to_team)


PickBucketId = Literal["FIRST_SAFE", "FIRST_SENSITIVE", "SECOND"]


@dataclass(frozen=True, slots=True)
class PickTradeCandidate:
    pick_id: str
    owner_team: str

    snap: PickSnapshot
    market: MarketValueSummary

    lock: LockInfo
    within_max_years: bool

    stepien_safe_if_traded_alone: bool
    stepien_sensitive: bool

    bucket: PickBucketId

    def as_asset(self, to_team: Optional[str] = None) -> PickAsset:
        return PickAsset(kind="pick", pick_id=self.pick_id, to_team=to_team, protection=self.snap.protection)


@dataclass(frozen=True, slots=True)
class SwapTradeCandidate:
    swap_id: str
    owner_team: str

    snap: SwapSnapshot
    lock: LockInfo

    def as_asset(self, to_team: Optional[str] = None) -> SwapAsset:
        return SwapAsset(
            kind="swap",
            swap_id=self.swap_id,
            pick_id_a=self.snap.pick_id_a,
            pick_id_b=self.snap.pick_id_b,
            to_team=to_team,
        )


@dataclass(frozen=True, slots=True)
class TeamOutgoingCatalog:
    team_id: str

    player_ids_by_bucket: Dict[BucketId, Tuple[str, ...]]
    pick_ids_by_bucket: Dict[PickBucketId, Tuple[str, ...]]
    swap_ids: Tuple[str, ...]

    players: Dict[str, PlayerTradeCandidate]
    picks: Dict[str, PickTradeCandidate]
    swaps: Dict[str, SwapTradeCandidate]


@dataclass(frozen=True, slots=True)
class IncomingPlayerRef:
    player_id: str
    from_team: str
    tag: str
    tag_strength: float
    market_total: float
    salary_m: float
    remaining_years: float
    age: Optional[float]


@dataclass(frozen=True, slots=True)
class StepienHelper:
    draft_picks: Mapping[str, Mapping[str, Any]]
    current_draft_year: int
    lookahead: int

    def is_compliant_after(
        self,
        *,
        team_id: str,
        outgoing_pick_ids: Set[str],
        incoming_pick_ids: Set[str],
    ) -> bool:
        """True if the team remains Stepien-compliant after pick ownership changes."""
        if self.lookahead <= 0:
            return True

        tid = _canon_team_id(team_id)
        if not tid:
            return True

        # Base owner map from snapshot.
        owner_after: Dict[str, str] = {}
        for pid, pick in self.draft_picks.items():
            if not isinstance(pick, Mapping):
                continue
            owner_after[str(pid)] = _canon_team_id(pick.get("owner_team"))

        # Apply outgoing/incoming mutations.
        for pid in outgoing_pick_ids or set():
            k = str(pid)
            if k not in owner_after:
                continue
            # Remove ownership from this team; the specific destination doesn't matter for Stepien.
            owner_after[k] = "__OUT__"
        for pid in incoming_pick_ids or set():
            k = str(pid)
            if k not in owner_after:
                continue
            owner_after[k] = tid

        violation = check_stepien_violation(
            team_id=tid,
            draft_picks=self.draft_picks,
            current_draft_year=int(self.current_draft_year),
            lookahead=int(self.lookahead),
            owner_after=owner_after,
        )
        return violation is None


@dataclass(frozen=True, slots=True)
class TradeAssetCatalog:
    db_path: str
    built_for_date: date
    season_year: int
    draft_year: int
    trade_rules: Dict[str, Any]

    outgoing_by_team: Dict[str, TeamOutgoingCatalog]
    incoming_by_need_tag: Dict[str, Tuple[IncomingPlayerRef, ...]]
    incoming_cheap_by_need_tag: Dict[str, Tuple[IncomingPlayerRef, ...]]

    stepien: StepienHelper


# =============================================================================
# Catalog build
# =============================================================================

# Thresholds / constants (deterministic)
_TOP_TAG_MIN = 0.55
_LOW_FIT_MAX = 0.45


def _lock_info_for_asset_key(
    *,
    asset_key_value: str,
    asset_locks: Mapping[str, Any],
    current_date: date,
    allow_locked_by_deal_id: Optional[str] = None,
) -> LockInfo:
    lock = asset_locks.get(asset_key_value) if isinstance(asset_locks, Mapping) else None
    if not isinstance(lock, Mapping):
        return LockInfo(is_locked=False, expires_at=None, deal_id=None)

    deal_id = lock.get("deal_id")
    expires_at_raw = lock.get("expires_at")
    expires_at_date = _parse_iso_date(expires_at_raw)
    expires_at_iso = _to_iso(expires_at_date) if expires_at_date else (str(expires_at_raw) if expires_at_raw else None)

    if expires_at_date is not None and current_date > expires_at_date:
        return LockInfo(is_locked=False, expires_at=expires_at_iso, deal_id=str(deal_id) if deal_id else None)

    if allow_locked_by_deal_id and deal_id is not None and str(deal_id) == str(allow_locked_by_deal_id):
        return LockInfo(is_locked=False, expires_at=expires_at_iso, deal_id=str(deal_id))

    return LockInfo(is_locked=True, expires_at=expires_at_iso, deal_id=str(deal_id) if deal_id else None)


def _market_summary_for_player(
    pricer: MarketPricer,
    snap: PlayerSnapshot,
) -> MarketValueSummary:
    a = PlayerAsset(kind="player", player_id=snap.player_id, to_team=None)
    mv = pricer.price_snapshot(snap, asset_key=_asset_key(a))
    return MarketValueSummary.from_components(mv.value)


def _market_summary_for_pick(
    pricer: MarketPricer,
    snap: PickSnapshot,
    *,
    pick_expectation: Optional[Any],
) -> MarketValueSummary:
    a = PickAsset(kind="pick", pick_id=snap.pick_id, to_team=None, protection=snap.protection)
    mv = pricer.price_snapshot(snap, asset_key=_asset_key(a), pick_expectation=pick_expectation)
    return MarketValueSummary.from_components(mv.value)


def _compute_top_tags(supply: Mapping[str, float]) -> Tuple[str, ...]:
    items = [(str(k), float(v or 0.0)) for k, v in (supply or {}).items()]
    items.sort(key=lambda x: (-x[1], x[0]))
    out: List[str] = []
    for tag, v in items[:4]:
        if v >= _TOP_TAG_MIN:
            out.append(tag)
    return tuple(out)


def _bucket_caps_for_posture(posture: str) -> Dict[BucketId, int]:
    p = str(posture or "").upper()
    # Defaults are deliberately moderate; generator can widen later.
    if p in {"SELL", "SOFT_SELL"}:
        return {
            "FILLER_BAD_CONTRACT": 4,
            "FILLER_CHEAP": 4,
            "SURPLUS_LOW_FIT": 7,
            "SURPLUS_REDUNDANT": 6,
            "EXPIRING": 6,
            "VETERAN_SALE": 5,
            "CONSOLIDATE": 0,
            "CORE": 0,
        }
    if p in {"AGGRESSIVE_BUY"}:
        return {
            "FILLER_BAD_CONTRACT": 7,
            "FILLER_CHEAP": 5,
            "SURPLUS_LOW_FIT": 4,
            "SURPLUS_REDUNDANT": 5,
            "EXPIRING": 5,
            "VETERAN_SALE": 0,
            "CONSOLIDATE": 7,
            "CORE": 0,
        }
    if p in {"SOFT_BUY"}:
        return {
            "FILLER_BAD_CONTRACT": 7,
            "FILLER_CHEAP": 5,
            "SURPLUS_LOW_FIT": 4,
            "SURPLUS_REDUNDANT": 5,
            "EXPIRING": 5,
            "VETERAN_SALE": 0,
            "CONSOLIDATE": 5,
            "CORE": 0,
        }
    # STAND_PAT / unknown
    return {
        "FILLER_BAD_CONTRACT": 6,
        "FILLER_CHEAP": 4,
        "SURPLUS_LOW_FIT": 6,
        "SURPLUS_REDUNDANT": 5,
        "EXPIRING": 5,
        "VETERAN_SALE": 0,
        "CONSOLIDATE": 3,
        "CORE": 0,
    }


def _outgoing_priority_for_posture(posture: str) -> Tuple[BucketId, ...]:
    p = str(posture or "").upper()
    if p in {"SELL", "SOFT_SELL"}:
        return (
            "VETERAN_SALE",
            "EXPIRING",
            "SURPLUS_LOW_FIT",
            "SURPLUS_REDUNDANT",
            "FILLER_BAD_CONTRACT",
            "FILLER_CHEAP",
            "CONSOLIDATE",
        )
    if p in {"AGGRESSIVE_BUY", "SOFT_BUY"}:
        return (
            "CONSOLIDATE",
            "FILLER_BAD_CONTRACT",
            "SURPLUS_LOW_FIT",
            "SURPLUS_REDUNDANT",
            "EXPIRING",
            "FILLER_CHEAP",
            "VETERAN_SALE",
        )
    # STAND_PAT / unknown
    return (
        "SURPLUS_LOW_FIT",
        "SURPLUS_REDUNDANT",
        "FILLER_BAD_CONTRACT",
        "EXPIRING",
        "FILLER_CHEAP",
        "CONSOLIDATE",
        "VETERAN_SALE",
    )


def build_trade_asset_catalog(
    *,
    tick_ctx: Any,
    incoming_top_n: int = 120,
    incoming_cheap_n: int = 120,
    bucket_caps_override: Optional[Dict[str, int]] = None,
    allow_locked_by_deal_id: Optional[str] = None,
) -> TradeAssetCatalog:
    """Build TradeAssetCatalog from an existing TradeGenerationTickContext."""

    # --- Extract tick inputs (defensive)
    db_path = str(getattr(tick_ctx, "db_path", ""))
    current_date = getattr(tick_ctx, "current_date", None)
    if not isinstance(current_date, date):
        raise RuntimeError("build_trade_asset_catalog: tick_ctx.current_date must be a date")

    season_year = _safe_int(getattr(tick_ctx, "season_year", 0), 0)
    if season_year <= 0:
        raise RuntimeError("build_trade_asset_catalog: tick_ctx.season_year missing/invalid")

    rule_tick_ctx = getattr(tick_ctx, "rule_tick_ctx", None)
    if rule_tick_ctx is None:
        raise RuntimeError("build_trade_asset_catalog: tick_ctx.rule_tick_ctx missing")

    ctx_state_base = getattr(rule_tick_ctx, "ctx_state_base", {}) or {}
    if not isinstance(ctx_state_base, Mapping):
        ctx_state_base = {}
    league = ctx_state_base.get("league", {}) if isinstance(ctx_state_base, Mapping) else {}
    if not isinstance(league, Mapping):
        league = {}

    trade_rules = league.get("trade_rules", {}) or {}
    if not isinstance(trade_rules, Mapping):
        trade_rules = {}

    draft_year = _safe_int(league.get("draft_year"), 0)
    if draft_year <= 0:
        # Fallback (should not happen once state.export_trade_context_snapshot includes draft_year)
        draft_year = season_year + 1

    asset_locks = ctx_state_base.get("asset_locks", {}) or {}
    if not isinstance(asset_locks, Mapping):
        asset_locks = {}

    provider = getattr(tick_ctx, "provider", None)
    if provider is None:
        raise RuntimeError("build_trade_asset_catalog: tick_ctx.provider missing")

    repo: LeagueRepo = getattr(tick_ctx, "repo", None)
    if repo is None:
        raise RuntimeError("build_trade_asset_catalog: tick_ctx.repo missing")

    # --- Pricing + fit engines (pure + cached)
    pricer = MarketPricer(config=MarketPricingConfig())
    fit_engine = FitEngine(config=FitEngineConfig())

    # --- Stepien helper (shared policy)
    max_pick_years_ahead = _safe_int(trade_rules.get("max_pick_years_ahead"), 7)
    stepien_lookahead = _safe_int(trade_rules.get("stepien_lookahead"), 7)
    stepien = StepienHelper(
        draft_picks=getattr(provider, "draft_picks_map", {}) or {},
        current_draft_year=int(draft_year),
        lookahead=int(stepien_lookahead),
    )

    # --- Determine team ids
    ids_raw: Sequence[str]
    try:
        ids_raw = list(getattr(tick_ctx, "team_situations", {}) or {}.keys())
    except Exception:
        ids_raw = []
    if not ids_raw:
        ids_raw = list(getattr(tick_ctx, "gm_profiles", {}) or {}.keys())
    if not ids_raw:
        ids_raw = list(ALL_TEAM_IDS)
    team_ids = sorted({_canon_team_id(t) for t in ids_raw if _canon_team_id(t)})

    # --- Build outgoing catalogs per team
    outgoing_by_team: Dict[str, TeamOutgoingCatalog] = {}

    # Accumulate all eligible player candidates for league-wide incoming indices.
    incoming_refs_by_tag: Dict[str, List[IncomingPlayerRef]] = {}
    incoming_cheap_refs_by_tag: Dict[str, List[IncomingPlayerRef]] = {}

    # Precompute for all teams to keep deterministic behavior.
    for tid in team_ids:
        # Team posture/decision inputs
        ts = tick_ctx.get_team_situation(tid)
        dc = tick_ctx.get_decision_context(tid)
        posture = str(getattr(ts, "trade_posture", "") or "")

        caps = _bucket_caps_for_posture(posture)
        if bucket_caps_override:
            # override keys are strings; bucket ids are the same string set.
            for k, v in bucket_caps_override.items():
                kk = str(k or "").strip().upper()
                if kk in caps:
                    caps[kk] = _safe_int(v, caps[kk])

        # --- Roster snapshot (1 query per team)
        roster_rows = repo.get_team_roster(tid) or []
        roster_player_ids: List[str] = []
        for r in roster_rows:
            if not isinstance(r, Mapping):
                continue
            pid = _canon_player_id(r.get("player_id"))
            if pid:
                roster_player_ids.append(pid)

        # Build rule meta once per team roster (cached in tick context).
        players_meta = rule_tick_ctx.ensure_players_meta(roster_player_ids)

        # --- Build player candidates
        players: Dict[str, PlayerTradeCandidate] = {}
        per_team_candidates: List[PlayerTradeCandidate] = []

        for r in roster_rows:
            if not isinstance(r, Mapping):
                continue
            pid = _canon_player_id(r.get("player_id"))
            if not pid:
                continue

            # Lock info (same as AssetLockRule)
            lock = _lock_info_for_asset_key(
                asset_key_value=_asset_key(PlayerAsset(kind="player", player_id=pid)),
                asset_locks=asset_locks,
                current_date=current_date,
                allow_locked_by_deal_id=allow_locked_by_deal_id,
            )

            meta = players_meta.get(pid) or {}
            if not isinstance(meta, Mapping):
                meta = {}

            # Player bans
            recent_until_date, _ = compute_recent_signing_banned_until(
                dict(meta),
                trade_rules=dict(trade_rules),
                season_year=int(season_year),
                strict=False,
            )
            aggregation_until_date, _ = compute_aggregation_banned_until(
                dict(meta),
                trade_rules=dict(trade_rules),
                strict=False,
            )
            recent_until_iso = _to_iso(recent_until_date)
            aggregation_until_iso = _to_iso(aggregation_until_date)
            recent_active = bool(recent_until_date is not None and current_date < recent_until_date)
            aggregation_active = bool(aggregation_until_date is not None and current_date < aggregation_until_date)

            # Recent-signing ban means not tradable at all (per rules).
            # Still store candidate object for debugging? We keep it but it won't go into buckets/indices.
            # Contract attach via provider's injected contract ledger maps.
            contract: Optional[ContractSnapshot] = None
            cid = getattr(provider, "active_contract_id_by_player", {}).get(pid)
            if cid:
                cdict = getattr(provider, "contracts_map", {}).get(cid)
                if isinstance(cdict, Mapping):
                    try:
                        contract = contract_snapshot_from_dict(cdict, current_season_year=int(season_year))
                    except Exception:
                        contract = None

            attrs = r.get("attrs") if isinstance(r.get("attrs"), Mapping) else {}
            snap = PlayerSnapshot(
                kind="player",
                player_id=pid,
                name=(str(r.get("name")) if r.get("name") is not None else None),
                pos=(str(r.get("pos")) if r.get("pos") is not None else None),
                age=_safe_float(r.get("age"), None),
                ovr=_safe_float(r.get("ovr"), None),
                team_id=tid,
                salary_amount=_safe_float(r.get("salary_amount"), None),
                attrs=dict(attrs) if isinstance(attrs, Mapping) else {},
                contract=contract,
                meta={},
            )

            market = _market_summary_for_player(pricer, snap)
            supply = fit_engine.compute_player_supply_vector(snap)
            fit_score, _, _ = fit_engine.score_fit(dc.need_map or {}, supply)
            top_tags = _compute_top_tags(supply)

            remaining_years = _remaining_years_from_contract(contract, int(season_year))
            is_expiring = bool(remaining_years <= 1.0 + 1e-9)
            salary_m = float((snap.salary_amount or 0.0) / 1_000_000.0)

            # Return-to-trading-team bans: season-specific list keyed by str(season_year).
            return_bans = ()
            try:
                trb = meta.get("trade_return_bans") if isinstance(meta, Mapping) else None
                if isinstance(trb, Mapping):
                    teams = trb.get(str(int(season_year))) or []
                    if isinstance(teams, list):
                        return_bans = tuple(sorted({_canon_team_id(t) for t in teams if _canon_team_id(t)}))
            except Exception:
                return_bans = ()

            cand = PlayerTradeCandidate(
                player_id=pid,
                team_id=tid,
                snap=snap,
                market=market,
                supply=dict(supply),
                top_tags=top_tags,
                fit_vs_team=float(fit_score),
                surplus_score=float(1.0 - float(fit_score)),
                salary_m=salary_m,
                remaining_years=float(remaining_years),
                is_expiring=is_expiring,
                lock=lock,
                recent_signing_banned_until=recent_until_iso,
                aggregation_banned_until=aggregation_until_iso,
                aggregation_solo_only=bool(aggregation_active),
                return_ban_teams=return_bans,
                buckets=(),
            )
            players[pid] = cand
            per_team_candidates.append(cand)

        # --- Compute per-team bucket memberships
        # Exclude locked and recent-signing-banned players from outgoing lists by default.
        eligible_for_outgoing = [
            c
            for c in per_team_candidates
            if not c.lock.is_locked
            and not (c.recent_signing_banned_until and _parse_iso_date(c.recent_signing_banned_until) and current_date < _parse_iso_date(c.recent_signing_banned_until))  # type: ignore[arg-type]
        ]

        # CORE (kept for potential "big swing" mode; not included in outgoing selection by default)
        core_sorted = sorted(
            eligible_for_outgoing,
            key=lambda c: (-c.market.total, (c.snap.age if c.snap.age is not None else 99.0), c.player_id),
        )
        core_ids = tuple([c.player_id for c in core_sorted[:2]])

        # Precompute redundant score (supply * (1 - need))
        def redundancy_score(c: PlayerTradeCandidate) -> float:
            nm = dc.need_map or {}
            s = 0.0
            for tag, v in (c.supply or {}).items():
                nv = float(nm.get(tag, 0.0) or 0.0)
                s += float(v or 0.0) * (1.0 - nv)
            return float(s)

        # BAD_CONTRACT filler: overpay = salary_m - market.total
        filler_bad = []
        for c in eligible_for_outgoing:
            overpay = float(c.salary_m - c.market.total)
            if c.market.total <= 6.0 or overpay >= 6.0:
                filler_bad.append((overpay, c))
        filler_bad.sort(
            key=lambda t: (-t[0], -t[1].salary_m, t[1].market.total, t[1].player_id)
        )
        filler_bad_ids = [c.player_id for _, c in filler_bad[: max(0, caps.get("FILLER_BAD_CONTRACT", 0))]]

        # CHEAP filler
        filler_cheap = [
            c
            for c in eligible_for_outgoing
            if c.salary_m <= 2.5 and c.market.total <= 4.5
        ]
        filler_cheap.sort(
            key=lambda c: (c.market.total, c.salary_m, c.remaining_years, c.player_id)
        )
        filler_cheap_ids = [c.player_id for c in filler_cheap[: max(0, caps.get("FILLER_CHEAP", 0))]]

        # SURPLUS_LOW_FIT
        low_fit = [c for c in eligible_for_outgoing if c.fit_vs_team <= _LOW_FIT_MAX]
        low_fit.sort(
            key=lambda c: (c.fit_vs_team, c.market.total, -c.salary_m, c.player_id)
        )
        low_fit_ids = [c.player_id for c in low_fit[: max(0, caps.get("SURPLUS_LOW_FIT", 0))]]

        # SURPLUS_REDUNDANT
        redundant_scored = []
        for c in eligible_for_outgoing:
            rs = redundancy_score(c)
            if rs >= 0.55:
                redundant_scored.append((rs, c))
        redundant_scored.sort(
            key=lambda t: (-t[0], t[1].market.total, t[1].fit_vs_team, t[1].player_id)
        )
        redundant_ids = [c.player_id for _, c in redundant_scored[: max(0, caps.get("SURPLUS_REDUNDANT", 0))]]

        # EXPIRING
        expiring = [c for c in eligible_for_outgoing if c.is_expiring and c.salary_m >= 1.0]
        p = str(posture or "").upper()
        if p in {"SELL", "SOFT_SELL"}:
            expiring.sort(key=lambda c: (-c.market.total, -c.salary_m, -(c.snap.age or 0.0), c.player_id))
        else:
            expiring.sort(key=lambda c: (c.market.total, -c.salary_m, -(c.snap.age or 0.0), c.player_id))
        expiring_ids = [c.player_id for c in expiring[: max(0, caps.get("EXPIRING", 0))]]

        # VETERAN_SALE (SELL/REBUILD teams)
        veteran_ids: List[str] = []
        if p in {"SELL", "SOFT_SELL"} or str(getattr(ts, "time_horizon", "") or "").upper() == "REBUILD":
            veteran = [
                c
                for c in eligible_for_outgoing
                if (c.snap.age is not None and float(c.snap.age) >= 29.0) and c.market.now >= 6.0
            ]
            veteran.sort(key=lambda c: (-c.market.now, -(c.snap.age or 0.0), c.remaining_years, c.player_id))
            veteran_ids = [c.player_id for c in veteran[: max(0, caps.get("VETERAN_SALE", 0))]]

        # CONSOLIDATE (BUY teams) - mid-tier by team market rank (30%~70%)
        consolidate_ids: List[str] = []
        if p in {"AGGRESSIVE_BUY", "SOFT_BUY"}:
            ranked = sorted(
                eligible_for_outgoing,
                key=lambda c: (-c.market.total, c.player_id),
            )
            if ranked:
                lo = int(len(ranked) * 0.30)
                hi = int(len(ranked) * 0.70)
                hi = max(hi, lo)
                mid = [c for i, c in enumerate(ranked) if lo <= i <= hi and c.player_id not in core_ids]
                mid.sort(key=lambda c: (-c.market.total, -c.salary_m, c.player_id))
                consolidate_ids = [c.player_id for c in mid[: max(0, caps.get("CONSOLIDATE", 0))]]

        # --- Record bucket membership into candidate objects (for debugging/consistency)
        bucket_members: Dict[BucketId, List[str]] = {
            "CORE": list(core_ids),
            "FILLER_BAD_CONTRACT": list(filler_bad_ids),
            "FILLER_CHEAP": list(filler_cheap_ids),
            "SURPLUS_LOW_FIT": list(low_fit_ids),
            "SURPLUS_REDUNDANT": list(redundant_ids),
            "EXPIRING": list(expiring_ids),
            "VETERAN_SALE": list(veteran_ids),
            "CONSOLIDATE": list(consolidate_ids),
        }
        # buckets per player (all satisfied buckets, even if excluded later by priority selection)
        buckets_by_player: Dict[str, List[BucketId]] = {}
        for b, ids in bucket_members.items():
            for pid in ids:
                buckets_by_player.setdefault(pid, []).append(b)
        # Re-create PlayerTradeCandidate with buckets populated (dataclass frozen)
        for pid, c in list(players.items()):
            bs = tuple(buckets_by_player.get(pid, []))
            if bs:
                players[pid] = PlayerTradeCandidate(
                    player_id=c.player_id,
                    team_id=c.team_id,
                    snap=c.snap,
                    market=c.market,
                    supply=c.supply,
                    top_tags=c.top_tags,
                    fit_vs_team=c.fit_vs_team,
                    surplus_score=c.surplus_score,
                    salary_m=c.salary_m,
                    remaining_years=c.remaining_years,
                    is_expiring=c.is_expiring,
                    lock=c.lock,
                    recent_signing_banned_until=c.recent_signing_banned_until,
                    aggregation_banned_until=c.aggregation_banned_until,
                    aggregation_solo_only=c.aggregation_solo_only,
                    return_ban_teams=c.return_ban_teams,
                    buckets=bs,
                )

        # --- Outgoing selection with priority-based de-dup
        priority = _outgoing_priority_for_posture(posture)
        selected: Set[str] = set()
        outgoing_player_ids_by_bucket: Dict[BucketId, Tuple[str, ...]] = {}
        for b in priority:
            ids = bucket_members.get(b, [])
            out: List[str] = []
            for pid in ids:
                if pid in selected:
                    continue
                out.append(pid)
                selected.add(pid)
            outgoing_player_ids_by_bucket[b] = tuple(out)
        # CORE kept but excluded from outgoing pool in normal mode (stored separately if needed)
        outgoing_player_ids_by_bucket["CORE"] = tuple(core_ids)

        # --- Picks (movable, locks + max_years + Stepien "safe alone")
        picks: Dict[str, PickTradeCandidate] = {}
        pick_ids_by_bucket: Dict[PickBucketId, List[str]] = {"FIRST_SAFE": [], "FIRST_SENSITIVE": [], "SECOND": []}
        draft_picks_map = getattr(provider, "draft_picks_map", {}) or {}
        for pick_id, pick_state in draft_picks_map.items():
            if not isinstance(pick_state, Mapping):
                continue
            owner_team = _canon_team_id(pick_state.get("owner_team"))
            if owner_team != tid:
                continue
            pid = str(pick_id)

            lock = _lock_info_for_asset_key(
                asset_key_value=_asset_key(PickAsset(kind="pick", pick_id=pid)),
                asset_locks=asset_locks,
                current_date=current_date,
                allow_locked_by_deal_id=allow_locked_by_deal_id,
            )
            if lock.is_locked:
                continue

            try:
                snap_pick = provider.get_pick_snapshot(pid)
            except Exception:
                continue

            within_max = bool(int(snap_pick.year) <= int(draft_year) + int(max_pick_years_ahead))
            if not within_max:
                continue

            market = _market_summary_for_pick(pricer, snap_pick, pick_expectation=getattr(provider, "pick_expectations", {}).get(pid))

            if int(snap_pick.round) != 1:
                cand = PickTradeCandidate(
                    pick_id=pid,
                    owner_team=tid,
                    snap=snap_pick,
                    market=market,
                    lock=lock,
                    within_max_years=within_max,
                    stepien_safe_if_traded_alone=True,
                    stepien_sensitive=False,
                    bucket="SECOND",
                )
                picks[pid] = cand
                pick_ids_by_bucket["SECOND"].append(pid)
                continue

            safe_alone = stepien.is_compliant_after(team_id=tid, outgoing_pick_ids={pid}, incoming_pick_ids=set())
            bucket: PickBucketId = "FIRST_SAFE" if safe_alone else "FIRST_SENSITIVE"
            cand = PickTradeCandidate(
                pick_id=pid,
                owner_team=tid,
                snap=snap_pick,
                market=market,
                lock=lock,
                within_max_years=within_max,
                stepien_safe_if_traded_alone=bool(safe_alone),
                stepien_sensitive=bool(not safe_alone),
                bucket=bucket,
            )
            picks[pid] = cand
            pick_ids_by_bucket[bucket].append(pid)

        # Deterministic ordering for pick buckets:
        # - FIRST_SAFE/SENSITIVE: prefer later years? no; keep by (year asc, market desc) to present near-term assets first.
        def _pick_sort_key(pid: str) -> Tuple[int, int, float, str]:
            p = picks.get(pid)
            if not p:
                return (9999, 9, -9999.0, pid)
            return (int(p.snap.year), int(p.snap.round), -float(p.market.total), pid)

        for b in list(pick_ids_by_bucket.keys()):
            pick_ids_by_bucket[b].sort(key=_pick_sort_key)

        pick_ids_by_bucket_final: Dict[PickBucketId, Tuple[str, ...]] = {
            "FIRST_SAFE": tuple(pick_ids_by_bucket["FIRST_SAFE"]),
            "FIRST_SENSITIVE": tuple(pick_ids_by_bucket["FIRST_SENSITIVE"]),
            "SECOND": tuple(pick_ids_by_bucket["SECOND"]),
        }

        # --- Swaps (existing swap_rights only)
        swaps: Dict[str, SwapTradeCandidate] = {}
        swap_ids: List[str] = []
        swap_rights_map = getattr(provider, "swap_rights_map", {}) or {}
        for swap_id, swap_state in swap_rights_map.items():
            if not isinstance(swap_state, Mapping):
                continue
            owner_team = _canon_team_id(swap_state.get("owner_team"))
            if owner_team != tid:
                continue
            if not bool(swap_state.get("active", True)):
                continue
            sid = str(swap_id)

            lock = _lock_info_for_asset_key(
                asset_key_value=_asset_key(SwapAsset(kind="swap", swap_id=sid, pick_id_a=str(swap_state.get("pick_id_a") or ""), pick_id_b=str(swap_state.get("pick_id_b") or ""))),
                asset_locks=asset_locks,
                current_date=current_date,
                allow_locked_by_deal_id=allow_locked_by_deal_id,
            )
            if lock.is_locked:
                continue

            try:
                snap_swap = provider.get_swap_snapshot(sid)
            except Exception:
                continue

            cand = SwapTradeCandidate(
                swap_id=sid,
                owner_team=tid,
                snap=snap_swap,
                lock=lock,
            )
            swaps[sid] = cand
            swap_ids.append(sid)

        swap_ids.sort()

        outgoing_by_team[tid] = TeamOutgoingCatalog(
            team_id=tid,
            player_ids_by_bucket={k: tuple(v) for k, v in outgoing_player_ids_by_bucket.items()},
            pick_ids_by_bucket=pick_ids_by_bucket_final,
            swap_ids=tuple(swap_ids),
            players=players,
            picks=picks,
            swaps=swaps,
        )

        # --- League-wide incoming indices contribution
        # Eligible incoming (by default): not locked, not recent-signing banned.
        for c in players.values():
            if c.team_id != tid:
                continue
            if c.lock.is_locked:
                continue
            if c.recent_signing_banned_until:
                d_ban = _parse_iso_date(c.recent_signing_banned_until)
                if d_ban is not None and current_date < d_ban:
                    continue
            # Index by top tags (>= threshold)
            for tag in c.top_tags:
                strength = float(c.supply.get(tag, 0.0) or 0.0)
                if strength < _TOP_TAG_MIN:
                    continue
                ref = IncomingPlayerRef(
                    player_id=c.player_id,
                    from_team=c.team_id,
                    tag=tag,
                    tag_strength=strength,
                    market_total=float(c.market.total),
                    salary_m=float(c.salary_m),
                    remaining_years=float(c.remaining_years),
                    age=c.snap.age,
                )
                incoming_refs_by_tag.setdefault(tag, []).append(ref)
                incoming_cheap_refs_by_tag.setdefault(tag, []).append(ref)

    # --- Finalize incoming indices (sort + cut)
    incoming_by_need_tag: Dict[str, Tuple[IncomingPlayerRef, ...]] = {}
    incoming_cheap_by_need_tag: Dict[str, Tuple[IncomingPlayerRef, ...]] = {}

    for tag, refs in incoming_refs_by_tag.items():
        refs.sort(key=lambda r: (-r.tag_strength, -r.market_total, -r.remaining_years, r.player_id))
        incoming_by_need_tag[str(tag)] = tuple(refs[: max(0, int(incoming_top_n))])

    for tag, refs in incoming_cheap_refs_by_tag.items():
        refs.sort(key=lambda r: (-r.tag_strength, r.market_total, r.salary_m, r.player_id))
        incoming_cheap_by_need_tag[str(tag)] = tuple(refs[: max(0, int(incoming_cheap_n))])

    return TradeAssetCatalog(
        db_path=db_path,
        built_for_date=current_date,
        season_year=int(season_year),
        draft_year=int(draft_year),
        trade_rules=dict(trade_rules),
        outgoing_by_team=outgoing_by_team,
        incoming_by_need_tag=incoming_by_need_tag,
        incoming_cheap_by_need_tag=incoming_cheap_by_need_tag,
        stepien=stepien,
    )

