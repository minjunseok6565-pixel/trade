from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .errors import (
    TradeError,
    DEAL_INVALIDATED,
    MISSING_TO_TEAM,
    PROTECTION_INVALID,
    INVALID_PLAYER_ID,
    INVALID_INPUT,
)
from schema import normalize_player_id, normalize_team_id, make_player_id_seq


@dataclass(frozen=True)
class PlayerAsset:
    kind: str
    player_id: str
    to_team: Optional[str] = None


@dataclass(frozen=True)
class PickAsset:
    kind: str
    pick_id: str
    to_team: Optional[str] = None
    protection: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SwapAsset:
    kind: str
    swap_id: str
    pick_id_a: str
    pick_id_b: str
    to_team: Optional[str] = None


@dataclass(frozen=True)
class FixedAsset:
    kind: str
    asset_id: str
    to_team: Optional[str] = None


Asset = Union[PlayerAsset, PickAsset, SwapAsset, FixedAsset]


@dataclass
class Deal:
    teams: List[str]
    legs: Dict[str, List[Asset]]
    meta: Optional[Dict[str, Any]] = field(default_factory=dict)


def compute_swap_id(pick_id_a: str, pick_id_b: str) -> str:
    sorted_ids = sorted([str(pick_id_a), str(pick_id_b)])
    return f"SWAP_{sorted_ids[0]}__{sorted_ids[1]}"


def asset_key(asset: Asset) -> str:
    if isinstance(asset, PlayerAsset):
        return f"player:{asset.player_id}"
    if isinstance(asset, PickAsset):
        return f"pick:{asset.pick_id}"
    if isinstance(asset, SwapAsset):
        return f"swap:{asset.swap_id}"
    return f"fixed_asset:{asset.asset_id}"


def _normalize_protection(raw: Dict[str, Any]) -> Dict[str, Any]:
    protection_type = raw.get("type", raw.get("rule"))
    if not isinstance(protection_type, str):
        raise TradeError(PROTECTION_INVALID, "Protection type is required", raw)
    protection_type = protection_type.strip().upper()
    if protection_type != "TOP_N":
        raise TradeError(PROTECTION_INVALID, "Unsupported protection type", raw)

    raw_n = raw.get("n")
    try:
        n_value = int(raw_n)
    except (TypeError, ValueError):
        raise TradeError(PROTECTION_INVALID, "Protection n must be an integer", raw)
    if n_value < 1 or n_value > 30:
        raise TradeError(PROTECTION_INVALID, "Protection n out of range", raw)

    compensation = raw.get("compensation")
    if not isinstance(compensation, dict):
        raise TradeError(PROTECTION_INVALID, "Protection compensation must be an object", raw)
    compensation_value = compensation.get("value")
    if isinstance(compensation_value, bool) or not isinstance(compensation_value, (int, float)):
        raise TradeError(PROTECTION_INVALID, "Protection compensation value must be numeric", raw)
    compensation_label = compensation.get("label")
    if not isinstance(compensation_label, str) or not compensation_label.strip():
        compensation_label = "Protected pick compensation"

    return {
        "type": protection_type,
        "n": n_value,
        "compensation": {"label": str(compensation_label), "value": compensation_value},
    }


def _normalize_team_id(value: Any, *, context: str) -> str:
    try:
        return str(normalize_team_id(value, strict=True))
    except ValueError as exc:
        raise TradeError(
            INVALID_INPUT,
            f"{context}: invalid team_id",
            {"value": value},
        ) from exc


def _is_legacy_numeric(value: Any) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return value >= 0
    if isinstance(value, float) and value.is_integer():
        return value >= 0
    if isinstance(value, str):
        return value.strip().isdigit()
    return False


def _normalize_player_id(
    value: Any,
    *,
    context: str,
    allow_legacy_numeric: bool = False,
) -> str:
    try:
        return str(normalize_player_id(value, strict=True))
    except ValueError as exc:
        if allow_legacy_numeric and _is_legacy_numeric(value):
            try:
                numeric_value = int(float(value))
            except (TypeError, ValueError):
                raise TradeError(
                    INVALID_PLAYER_ID,
                    f"{context}: invalid player_id",
                    {"value": value},
                ) from exc
            return str(make_player_id_seq(numeric_value))
        raise TradeError(
            INVALID_PLAYER_ID,
            f"{context}: invalid player_id",
            {"value": value},
        ) from exc


def _parse_asset(raw: Dict[str, Any], *, allow_legacy_numeric: bool) -> Asset:
    kind = str(raw.get("kind", "")).lower()
    to_team = raw.get("to_team")
    to_team = _normalize_team_id(to_team, context="asset.to_team") if to_team else None
    if kind == "player":
        player_id = raw.get("player_id")
        if player_id is None:
            raise TradeError(DEAL_INVALIDATED, "Missing player_id in asset", raw)
        pid = _normalize_player_id(
            player_id,
            context="asset.player_id",
            allow_legacy_numeric=allow_legacy_numeric,
        )
        return PlayerAsset(kind="player", player_id=pid, to_team=to_team)
    if kind == "pick":
        pick_id = raw.get("pick_id")
        if not pick_id:
            raise TradeError(DEAL_INVALIDATED, "Missing pick_id in asset", raw)
        protection = None
        if "protection" in raw:
            protection_raw = raw.get("protection")
            if not isinstance(protection_raw, dict):
                raise TradeError(PROTECTION_INVALID, "Protection must be an object", raw)
            protection = _normalize_protection(protection_raw)
        return PickAsset(
            kind="pick",
            pick_id=str(pick_id),
            to_team=to_team,
            protection=protection,
        )
    if kind == "swap":
        pick_id_a = raw.get("pick_id_a")
        pick_id_b = raw.get("pick_id_b")
        if not isinstance(pick_id_a, str) or not pick_id_a.strip():
            raise TradeError(DEAL_INVALIDATED, "Missing pick_id_a in asset", raw)
        if not isinstance(pick_id_b, str) or not pick_id_b.strip():
            raise TradeError(DEAL_INVALIDATED, "Missing pick_id_b in asset", raw)
        swap_id = raw.get("swap_id")
        if not isinstance(swap_id, str) or not swap_id.strip():
            swap_id = compute_swap_id(pick_id_a, pick_id_b)
        return SwapAsset(
            kind="swap",
            swap_id=str(swap_id),
            pick_id_a=str(pick_id_a),
            pick_id_b=str(pick_id_b),
            to_team=to_team,
        )
    if kind == "fixed_asset":
        asset_id = raw.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise TradeError(DEAL_INVALIDATED, "Missing asset_id in asset", raw)
        return FixedAsset(kind="fixed_asset", asset_id=str(asset_id), to_team=to_team)
    raise TradeError(DEAL_INVALIDATED, "Unknown asset kind", raw)


def parse_deal(payload: Dict[str, Any], *, allow_legacy_numeric: bool = False) -> Deal:
    teams_raw = payload.get("teams")
    legs_raw = payload.get("legs")
    if not isinstance(teams_raw, list) or not isinstance(legs_raw, dict):
        raise TradeError(DEAL_INVALIDATED, "Invalid deal payload", payload)

    teams = [_normalize_team_id(t, context="deal.teams") for t in teams_raw]
    if not teams:
        raise TradeError(DEAL_INVALIDATED, "Deal must include teams", payload)

    normalized_legs_raw = {
        _normalize_team_id(k, context="deal.legs key"): v for k, v in legs_raw.items()
    }
    legs: Dict[str, List[Asset]] = {}
    for team_id in teams:
        if team_id not in normalized_legs_raw:
            raise TradeError(DEAL_INVALIDATED, f"Missing legs for team {team_id}", payload)
        raw_assets = normalized_legs_raw.get(team_id) or []
        if not isinstance(raw_assets, list):
            raise TradeError(DEAL_INVALIDATED, f"Invalid legs for team {team_id}", payload)
        legs[team_id] = [
            _parse_asset(asset, allow_legacy_numeric=allow_legacy_numeric)
            for asset in raw_assets
        ]

    if len(teams) >= 3:
        for team_id, assets in legs.items():
            for asset in assets:
                if not asset.to_team:
                    raise TradeError(
                        MISSING_TO_TEAM,
                        "Missing to_team for multi-team deal asset",
                        {"team_id": team_id, "asset": asset},
                    )

    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise TradeError(DEAL_INVALIDATED, "meta must be an object", payload)

    return Deal(teams=teams, legs=legs, meta=meta or {})


def canonicalize_deal(deal: Deal) -> Deal:
    teams = sorted(_normalize_team_id(team_id, context="deal.teams") for team_id in deal.teams)
    legs: Dict[str, List[Asset]] = {}
    for team_id in sorted(deal.legs.keys()):
        normalized_team_id = _normalize_team_id(team_id, context="deal.legs key")
        assets = list(deal.legs.get(team_id, []))
        normalized_assets: List[Asset] = []
        for asset in assets:
            if isinstance(asset, PlayerAsset):
                to_team = (
                    _normalize_team_id(asset.to_team, context="deal.asset.to_team")
                    if asset.to_team
                    else None
                )
                player_id = _normalize_player_id(
                    asset.player_id,
                    context="deal.asset.player_id",
                    allow_legacy_numeric=False,
                )
                normalized_assets.append(
                    PlayerAsset(kind=asset.kind, player_id=player_id, to_team=to_team)
                )
            elif isinstance(asset, PickAsset):
                to_team = (
                    _normalize_team_id(asset.to_team, context="deal.asset.to_team")
                    if asset.to_team
                    else None
                )
                protection = None
                if asset.protection is not None:
                    protection = _normalize_protection(asset.protection)
                normalized_assets.append(
                    PickAsset(
                        kind=asset.kind,
                        pick_id=asset.pick_id,
                        to_team=to_team,
                        protection=protection,
                    )
                )
            elif isinstance(asset, SwapAsset):
                to_team = (
                    _normalize_team_id(asset.to_team, context="deal.asset.to_team")
                    if asset.to_team
                    else None
                )
                normalized_assets.append(
                    SwapAsset(
                        kind=asset.kind,
                        swap_id=asset.swap_id,
                        pick_id_a=asset.pick_id_a,
                        pick_id_b=asset.pick_id_b,
                        to_team=to_team,
                    )
                )
            elif isinstance(asset, FixedAsset):
                to_team = (
                    _normalize_team_id(asset.to_team, context="deal.asset.to_team")
                    if asset.to_team
                    else None
                )
                normalized_assets.append(
                    FixedAsset(kind=asset.kind, asset_id=asset.asset_id, to_team=to_team)
                )
        normalized_assets.sort(key=_asset_sort_key)
        legs[normalized_team_id] = normalized_assets
    meta = dict(deal.meta) if deal.meta else {}
    return Deal(teams=teams, legs=legs, meta=meta)


def _asset_sort_key(asset: Asset) -> tuple:
    if isinstance(asset, PlayerAsset):
        return (0, asset.to_team or "", asset.player_id)
    if isinstance(asset, PickAsset):
        return (1, asset.to_team or "", asset.pick_id)
    if isinstance(asset, SwapAsset):
        return (2, asset.to_team or "", asset.swap_id)
    return (3, asset.to_team or "", asset.asset_id)


def serialize_deal(deal: Deal) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "teams": list(deal.teams),
        "legs": {},
    }
    for team_id in deal.legs:
        assets_payload = []
        for asset in deal.legs[team_id]:
            if isinstance(asset, PlayerAsset):
                payload_asset = {"kind": "player", "player_id": asset.player_id}
                if asset.to_team:
                    payload_asset["to_team"] = asset.to_team
                assets_payload.append(payload_asset)
            elif isinstance(asset, PickAsset):
                payload_asset = {"kind": "pick", "pick_id": asset.pick_id}
                if asset.to_team:
                    payload_asset["to_team"] = asset.to_team
                if asset.protection is not None:
                    payload_asset["protection"] = dict(asset.protection)
                assets_payload.append(payload_asset)
            elif isinstance(asset, SwapAsset):
                payload_asset = {
                    "kind": "swap",
                    "swap_id": asset.swap_id,
                    "pick_id_a": asset.pick_id_a,
                    "pick_id_b": asset.pick_id_b,
                }
                if asset.to_team:
                    payload_asset["to_team"] = asset.to_team
                assets_payload.append(payload_asset)
            elif isinstance(asset, FixedAsset):
                payload_asset = {"kind": "fixed_asset", "asset_id": asset.asset_id}
                if asset.to_team:
                    payload_asset["to_team"] = asset.to_team
                assets_payload.append(payload_asset)
        payload["legs"][team_id] = assets_payload
    if deal.meta:
        payload["meta"] = dict(deal.meta)
    return payload
