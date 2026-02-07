from __future__ import annotations

"""
deal_evaluator.py

Deal-level evaluation orchestrator (pure, no DB).

Inputs:
- Deal (trades.models)
- team_id (the evaluating team perspective)
- DecisionContext (already built by decision_context.py + team_situation.py)
- ValuationDataProvider (protocol in types.py)

Pipeline:
1) Resolve incoming/outgoing assets for team_id (deal structure)
2) Resolve AssetSnapshot via provider
3) Market pricing (MarketPricer)
4) Team utility adjustment (TeamUtilityAdjuster)
5) Package effects (apply_package_effects) optional
6) Build TeamSideValuation + TeamDealEvaluation

Hard rules:
- Do NOT run team_situation again
- Do NOT validate legality/feasibility (salary matching, Stepien, etc)
"""

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from decision_context import DecisionContext

from .types import (
    AssetKind,
    AssetSnapshot,
    Deal,
    Asset,
    PlayerAsset,
    PickAsset,
    SwapAsset,
    FixedAsset,
    ValueComponents,
    SideTotals,
    TeamValuation,
    TeamSideValuation,
    TeamDealEvaluation,
    ValuationDataProvider,
    ValuationStep,
    stable_asset_key_from_models,
    snapshot_kind,
    snapshot_ref_id,
    PlayerSnapshot,
    PickSnapshot,
    SwapSnapshot,
    PickExpectation,
)

from .market_pricing import MarketPricer, MarketPricingConfig
from .team_utility import TeamUtilityAdjuster, TeamUtilityConfig

# package effects (your implemented module)
try:
    from .package_effects import apply_package_effects, PackageEffectsConfig
except Exception:  # pragma: no cover
    apply_package_effects = None  # type: ignore
    PackageEffectsConfig = None  # type: ignore

"""

We require the SSOT receiver resolver in trades.models.resolve_asset_receiver.
If it's missing or import paths are wrong, we want an immediate error rather than
silently switching to a divergent fallback implementation.
"""
from ..models import resolve_asset_receiver  # SSOT, required


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _sum_team_values(vals: Sequence[TeamValuation]) -> ValueComponents:
    out = ValueComponents.zero()
    for tv in vals:
        out = out + tv.team_value
    return out


def _attach_leg_meta(
    tv: TeamValuation,
    *,
    direction: str,
    from_team: str,
    to_team: str,
) -> TeamValuation:
    meta = dict(tv.meta or {})
    meta.update({"direction": direction, "from_team": str(from_team), "to_team": str(to_team)})
    return replace(tv, meta=meta)


def _override_pick_protection_if_needed(snap: PickSnapshot, asset: PickAsset) -> PickSnapshot:
    if getattr(asset, "protection", None):
        meta = dict(snap.meta or {})
        meta["deal_protection_override"] = True
        return replace(snap, protection=asset.protection, meta=meta)
    return snap


def _override_swap_fields_if_needed(snap: SwapSnapshot, asset: SwapAsset) -> SwapSnapshot:
    # Ensure swap snapshot matches deal asset fields (defensive)
    if snap.pick_id_a != asset.pick_id_a or snap.pick_id_b != asset.pick_id_b:
        meta = dict(snap.meta or {})
        meta["deal_swap_override"] = True
        return replace(snap, pick_id_a=asset.pick_id_a, pick_id_b=asset.pick_id_b, meta=meta)
    return snap


# -----------------------------------------------------------------------------
# Deal evaluator
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class DealEvaluator:
    market_config: MarketPricingConfig = field(default_factory=MarketPricingConfig)
    team_config: TeamUtilityConfig = field(default_factory=TeamUtilityConfig)
    package_config: Optional["PackageEffectsConfig"] = None

    _market: MarketPricer = field(init=False)
    _team: TeamUtilityAdjuster = field(init=False)

    def __post_init__(self) -> None:
        self._market = MarketPricer(config=self.market_config)
        self._team = TeamUtilityAdjuster(config=self.team_config)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    def evaluate_team_side(
        self,
        *,
        deal: Deal,
        team_id: str,
        ctx: DecisionContext,
        provider: ValuationDataProvider,
        include_package_effects: bool = True,
        attach_leg_metadata: bool = True,
    ) -> Tuple[TeamSideValuation, TeamDealEvaluation]:
        """
        Evaluate a deal from a single team's perspective.

        Returns:
          (TeamSideValuation, TeamDealEvaluation)

        - TeamSideValuation: incoming/outgoing TeamValuation + package steps/meta
        - TeamDealEvaluation: summary numbers used by decision_policy
        """
        if team_id not in deal.teams:
            raise ValueError(f"team_id {team_id!r} not in deal.teams")

        # 1) Resolve incoming/outgoing model assets
        outgoing_assets = list(deal.legs.get(team_id, []) or [])
        incoming_assets: List[Tuple[str, Asset]] = []
        for sender in deal.teams:
            if sender == team_id:
                continue
            for a in deal.legs.get(sender, []) or []:
                recv = resolve_asset_receiver(deal, sender, a)
                if recv == team_id:
                    incoming_assets.append((sender, a))

        # 2) Snapshot resolve + market/team valuation
        incoming_vals: List[TeamValuation] = []
        outgoing_vals: List[TeamValuation] = []
        incoming_pairs: List[Tuple[TeamValuation, AssetSnapshot]] = []
        outgoing_pairs: List[Tuple[TeamValuation, AssetSnapshot]] = []

        # incoming
        for sender, asset in incoming_assets:
            recv = team_id
            tv, snap = self._value_one_asset(
                asset=asset,
                ctx=ctx,
                provider=provider,
            )
            if attach_leg_metadata:
                tv = _attach_leg_meta(tv, direction="incoming", from_team=sender, to_team=recv)
            incoming_vals.append(tv)
            incoming_pairs.append((tv, snap))

        # outgoing
        for asset in outgoing_assets:
            recv = resolve_asset_receiver(deal, team_id, asset)
            tv, snap = self._value_one_asset(
                asset=asset,
                ctx=ctx,
                provider=provider,
            )
            if attach_leg_metadata:
                tv = _attach_leg_meta(tv, direction="outgoing", from_team=team_id, to_team=recv)
            outgoing_vals.append(tv)
            outgoing_pairs.append((tv, snap))

        # 3) Totals (base, without package effects)
        in_sum = _sum_team_values(incoming_vals)
        out_sum = _sum_team_values(outgoing_vals)
        incoming_totals = SideTotals(value=in_sum, count=len(incoming_vals))
        outgoing_totals = SideTotals(value=out_sum, count=len(outgoing_vals))

        # 4) Package effects (optional)
        package_steps: Tuple[ValuationStep, ...] = tuple()
        package_delta = ValueComponents.zero()
        package_meta: Dict[str, Any] = {}

        if include_package_effects and apply_package_effects is not None:
            package_delta, package_steps, package_meta = apply_package_effects(
                team_id=str(team_id),
                incoming=incoming_pairs,
                outgoing=outgoing_pairs,
                ctx=ctx,
                current_season_year=getattr(provider, "current_season_year", None),
                config=self.package_config,  # can be None -> default inside module
            )

        # 5) Build side valuation
        side = TeamSideValuation(
            team_id=str(team_id),
            incoming=tuple(incoming_vals),
            outgoing=tuple(outgoing_vals),
            incoming_totals=incoming_totals,
            outgoing_totals=outgoing_totals,
            package_steps=package_steps,
            meta={
                "package_delta": {
                    "now": package_delta.now,
                    "future": package_delta.future,
                    "total": package_delta.total,
                },
                "package_meta": package_meta,
                "incoming_count": len(incoming_vals),
                "outgoing_count": len(outgoing_vals),
            },
        )

        # 6) Build evaluation summary (net includes package delta)
        incoming_total = incoming_totals.value.total
        outgoing_total = outgoing_totals.value.total
        base_net = incoming_total - outgoing_total
        net_surplus = base_net + package_delta.total

        eps = 1e-9
        denom = outgoing_total if abs(outgoing_total) > eps else eps
        surplus_ratio = float(net_surplus / denom)

        evaluation = TeamDealEvaluation(
            team_id=str(team_id),
            incoming_total=float(incoming_total),
            outgoing_total=float(outgoing_total),
            net_surplus=float(net_surplus),
            surplus_ratio=float(surplus_ratio),
            side=side,
            meta={
                "base_net": float(base_net),
                "package_delta_total": float(package_delta.total),
                "package_delta": {"now": package_delta.now, "future": package_delta.future},
            },
        )

        return side, evaluation

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------
    def _value_one_asset(
        self,
        *,
        asset: Asset,
        ctx: DecisionContext,
        provider: ValuationDataProvider,
    ) -> Tuple[TeamValuation, AssetSnapshot]:
        """
        Resolve snapshot -> market valuation -> team valuation.
        Returns (TeamValuation, AssetSnapshot).
        """
        akey = stable_asset_key_from_models(asset)

        snap = self._resolve_snapshot(asset=asset, provider=provider)
        kind = snapshot_kind(snap)

        pick_exp = None
        resolved_a = None
        resolved_b = None
        resolved_a_exp: Optional[PickExpectation] = None
        resolved_b_exp: Optional[PickExpectation] = None        

        if kind == AssetKind.PICK and isinstance(snap, PickSnapshot):
            pick_exp = provider.get_pick_expectation(snap.pick_id)

        if kind == AssetKind.SWAP and isinstance(snap, SwapSnapshot):
            # swap pricing needs both pick snapshots
            resolved_a = provider.get_pick_snapshot(snap.pick_id_a)
            resolved_b = provider.get_pick_snapshot(snap.pick_id_b)
            # and their expectations (for expected pick number / year discount, etc.)
            resolved_a_exp = provider.get_pick_expectation(snap.pick_id_a)
            resolved_b_exp = provider.get_pick_expectation(snap.pick_id_b)

        market = self._market.price_snapshot(
            snap,
            asset_key=str(akey),
            pick_expectation=pick_exp,
            resolved_pick_a=resolved_a,
            resolved_pick_b=resolved_b,
            resolved_pick_a_expectation=resolved_a_exp,
            resolved_pick_b_expectation=resolved_b_exp,
        )

        team_val = self._team.value_asset(market, snap, ctx)
        return team_val, snap

    def _resolve_snapshot(self, *, asset: Asset, provider: ValuationDataProvider) -> AssetSnapshot:
        """Convert a Deal Asset -> AssetSnapshot using provider (and apply deal-local overrides)."""
        if isinstance(asset, PlayerAsset):
            return provider.get_player_snapshot(asset.player_id)

        if isinstance(asset, PickAsset):
            snap = provider.get_pick_snapshot(asset.pick_id)
            snap = _override_pick_protection_if_needed(snap, asset)
            return snap

        if isinstance(asset, SwapAsset):
            snap = provider.get_swap_snapshot(asset.swap_id)
            snap = _override_swap_fields_if_needed(snap, asset)
            return snap

        # FixedAsset
        if isinstance(asset, FixedAsset):
            return provider.get_fixed_asset_snapshot(asset.asset_id)

        # Defensive fallback (shouldn't happen if Deal is normalized)
        raise ValueError(f"Unknown asset type: {asset!r}")


# -----------------------------------------------------------------------------
# Convenience functional API
# -----------------------------------------------------------------------------
def evaluate_deal_for_team(
    *,
    deal: Deal,
    team_id: str,
    ctx: DecisionContext,
    provider: ValuationDataProvider,
    include_package_effects: bool = True,
    attach_leg_metadata: bool = True,
    market_config: Optional[MarketPricingConfig] = None,
    team_config: Optional[TeamUtilityConfig] = None,
    package_config: Optional["PackageEffectsConfig"] = None,
) -> Tuple[TeamSideValuation, TeamDealEvaluation]:
    """
    Stateless wrapper for one-off evaluation calls.
    """
    ev = DealEvaluator(
        market_config=market_config or MarketPricingConfig(),
        team_config=team_config or TeamUtilityConfig(),
        package_config=package_config,
    )
    return ev.evaluate_team_side(
        deal=deal,
        team_id=team_id,
        ctx=ctx,
        provider=provider,
        include_package_effects=include_package_effects,
        attach_leg_metadata=attach_leg_metadata,
    )

