from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .errors import TradeError, DEAL_INVALIDATED, MISSING_TO_TEAM


@dataclass(frozen=True)
class PlayerAsset:
    kind: str
    player_id: int
    to_team: Optional[str] = None


@dataclass(frozen=True)
class PickAsset:
    kind: str
    pick_id: str
    to_team: Optional[str] = None


Asset = Union[PlayerAsset, PickAsset]


@dataclass
class Deal:
    teams: List[str]
    legs: Dict[str, List[Asset]]
    meta: Optional[Dict[str, Any]] = field(default_factory=dict)


def _parse_asset(raw: Dict[str, Any]) -> Asset:
    kind = str(raw.get("kind", "")).lower()
    to_team = raw.get("to_team")
    to_team = str(to_team).upper() if to_team else None
    if kind == "player":
        player_id = raw.get("player_id")
        if player_id is None:
            raise TradeError(DEAL_INVALIDATED, "Missing player_id in asset", raw)
        return PlayerAsset(kind="player", player_id=int(player_id), to_team=to_team)
    if kind == "pick":
        pick_id = raw.get("pick_id")
        if not pick_id:
            raise TradeError(DEAL_INVALIDATED, "Missing pick_id in asset", raw)
        return PickAsset(kind="pick", pick_id=str(pick_id), to_team=to_team)
    raise TradeError(DEAL_INVALIDATED, "Unknown asset kind", raw)


def parse_deal(payload: Dict[str, Any]) -> Deal:
    teams_raw = payload.get("teams")
    legs_raw = payload.get("legs")
    if not isinstance(teams_raw, list) or not isinstance(legs_raw, dict):
        raise TradeError(DEAL_INVALIDATED, "Invalid deal payload", payload)

    teams = [str(t).upper() for t in teams_raw]
    if not teams:
        raise TradeError(DEAL_INVALIDATED, "Deal must include teams", payload)

    normalized_legs_raw = {str(k).upper(): v for k, v in legs_raw.items()}
    legs: Dict[str, List[Asset]] = {}
    for team_id in teams:
        if team_id not in normalized_legs_raw:
            raise TradeError(DEAL_INVALIDATED, f"Missing legs for team {team_id}", payload)
        raw_assets = normalized_legs_raw.get(team_id) or []
        if not isinstance(raw_assets, list):
            raise TradeError(DEAL_INVALIDATED, f"Invalid legs for team {team_id}", payload)
        legs[team_id] = [_parse_asset(asset) for asset in raw_assets]

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
    teams = sorted(deal.teams)
    legs: Dict[str, List[Asset]] = {}
    for team_id in sorted(deal.legs.keys()):
        assets = list(deal.legs.get(team_id, []))
        normalized_assets: List[Asset] = []
        for asset in assets:
            if isinstance(asset, PlayerAsset):
                to_team = asset.to_team.upper() if asset.to_team else None
                normalized_assets.append(
                    PlayerAsset(kind=asset.kind, player_id=asset.player_id, to_team=to_team)
                )
            elif isinstance(asset, PickAsset):
                to_team = asset.to_team.upper() if asset.to_team else None
                normalized_assets.append(
                    PickAsset(kind=asset.kind, pick_id=asset.pick_id, to_team=to_team)
                )
        normalized_assets.sort(key=_asset_sort_key)
        legs[team_id] = normalized_assets
    meta = dict(deal.meta) if deal.meta else {}
    return Deal(teams=teams, legs=legs, meta=meta)


def _asset_sort_key(asset: Asset) -> tuple:
    if isinstance(asset, PlayerAsset):
        return (0, asset.to_team or "", asset.player_id)
    return (1, asset.to_team or "", asset.pick_id)


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
                assets_payload.append(payload_asset)
        payload["legs"][team_id] = assets_payload
    if deal.meta:
        payload["meta"] = dict(deal.meta)
    return payload
