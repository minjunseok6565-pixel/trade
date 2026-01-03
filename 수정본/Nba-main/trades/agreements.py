from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from config import ROSTER_DF
from state import GAME_STATE

from .errors import (
    TradeError,
    DEAL_EXPIRED,
    DEAL_INVALIDATED,
    DEAL_ALREADY_EXECUTED,
)
from .models import Deal, PlayerAsset, PickAsset, canonicalize_deal, parse_deal, serialize_deal
from .validator import validate_deal


def _asset_key(asset: PlayerAsset | PickAsset) -> str:
    if isinstance(asset, PlayerAsset):
        return f"player:{asset.player_id}"
    return f"pick:{asset.pick_id}"


def _compute_assets_hash(deal: Deal) -> str:
    ownership_snapshot: Dict[str, str] = {}
    for assets in deal.legs.values():
        for asset in assets:
            if isinstance(asset, PlayerAsset):
                try:
                    ownership_snapshot[str(asset.player_id)] = str(
                        ROSTER_DF.at[asset.player_id, "Team"]
                    ).upper()
                except Exception:
                    ownership_snapshot[str(asset.player_id)] = ""

    payload = {
        "deal": serialize_deal(deal),
        "ownership": ownership_snapshot,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_committed_deal(deal: Deal, valid_days: int = 2) -> Dict[str, Any]:
    canonical = canonicalize_deal(deal)
    validate_deal(canonical)
    deal_id = str(uuid4())
    assets_hash = _compute_assets_hash(canonical)
    today = date.today()
    expires_at = today + timedelta(days=valid_days)

    entry = {
        "deal_id": deal_id,
        "deal": serialize_deal(canonical),
        "assets_hash": assets_hash,
        "created_at": today.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": "ACTIVE",
    }

    GAME_STATE.setdefault("trade_agreements", {})[deal_id] = entry
    _lock_assets_for_deal(canonical, deal_id, entry["expires_at"])
    return entry


def _lock_assets_for_deal(deal: Deal, deal_id: str, expires_at: str) -> None:
    locks = GAME_STATE.setdefault("asset_locks", {})
    for assets in deal.legs.values():
        for asset in assets:
            locks[_asset_key(asset)] = {"deal_id": deal_id, "expires_at": expires_at}


def verify_committed_deal(deal_id: str) -> Deal:
    agreements = GAME_STATE.setdefault("trade_agreements", {})
    entry = agreements.get(deal_id)
    if not entry:
        raise TradeError(DEAL_INVALIDATED, "Committed deal not found")

    status = entry.get("status")
    if status and status != "ACTIVE":
        if status == "EXECUTED":
            raise TradeError(DEAL_ALREADY_EXECUTED, "Deal already executed")
        if status == "EXPIRED":
            raise TradeError(DEAL_EXPIRED, "Deal expired")
        raise TradeError(DEAL_INVALIDATED, "Deal invalidated")

    expires_at = entry.get("expires_at")
    if expires_at and date.today() > date.fromisoformat(str(expires_at)):
        entry["status"] = "EXPIRED"
        release_locks_for_deal(deal_id)
        raise TradeError(DEAL_EXPIRED, "Deal expired")

    deal_payload = entry.get("deal") or {}
    deal = canonicalize_deal(parse_deal(deal_payload))

    if entry.get("assets_hash") != _compute_assets_hash(deal):
        entry["status"] = "INVALIDATED"
        release_locks_for_deal(deal_id)
        raise TradeError(DEAL_INVALIDATED, "Deal assets have changed")

    locks = GAME_STATE.get("asset_locks", {})
    for assets in deal.legs.values():
        for asset in assets:
            lock = locks.get(_asset_key(asset))
            if not lock or lock.get("deal_id") != deal_id:
                entry["status"] = "INVALIDATED"
                release_locks_for_deal(deal_id)
                raise TradeError(DEAL_INVALIDATED, "Asset lock missing")

    return deal


def mark_executed(deal_id: str) -> None:
    agreements = GAME_STATE.setdefault("trade_agreements", {})
    entry = agreements.get(deal_id)
    if not entry:
        return
    entry["status"] = "EXECUTED"
    release_locks_for_deal(deal_id)


def release_locks_for_deal(deal_id: str) -> None:
    locks = GAME_STATE.setdefault("asset_locks", {})
    to_remove = [key for key, lock in locks.items() if lock.get("deal_id") == deal_id]
    for key in to_remove:
        locks.pop(key, None)


def gc_expired_agreements(current_date: Optional[date] = None) -> None:
    agreements = GAME_STATE.setdefault("trade_agreements", {})
    today = current_date or date.today()
    for deal_id, entry in list(agreements.items()):
        if entry.get("status") != "ACTIVE":
            continue
        expires_at = entry.get("expires_at")
        if expires_at and today > date.fromisoformat(str(expires_at)):
            entry["status"] = "EXPIRED"
            release_locks_for_deal(deal_id)
