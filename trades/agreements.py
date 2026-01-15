from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import date, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
from state import GAME_STATE, get_current_date_as_date

from .errors import (
    TradeError,
    DEAL_EXPIRED,
    DEAL_INVALIDATED,
    DEAL_ALREADY_EXECUTED,
)
from .models import (
    Asset,
    Deal,
    FixedAsset,
    PickAsset,
    PlayerAsset,
    SwapAsset,
    asset_key,
    canonicalize_deal,
    parse_deal,
    serialize_deal,
)
from .validator import validate_deal


def _resolve_receiver(deal: Deal, sender_team: str, asset: PlayerAsset) -> str:
    if asset.to_team:
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != sender_team]
        if other_team:
            return other_team[0]
    raise ValueError("Missing to_team for multi-team deal asset")


def _compute_assets_hash(deal: Deal) -> str:
    ownership_snapshot: Dict[str, Any] = {}
    player_snapshots: list[dict[str, Any]] = []
    league = GAME_STATE.get("league", {})
    db_path = league.get("db_path") if isinstance(league, dict) else None
    if not db_path:
        raise ValueError("db_path is required to compute trade agreement hash")

    # DB SSOT: draft_picks / swap_rights / fixed_assets are no longer reliable in GAME_STATE.
    # Use one DB transaction snapshot and ensure repo is closed to avoid connection leaks.
    with contextlib.closing(LeagueRepo(db_path)) as repo:
        repo.init_db()
        snap = repo.get_trade_assets_snapshot() or {}
        draft_picks = (snap.get("draft_picks") or {}) if isinstance(snap, dict) else {}
        swap_rights = (snap.get("swap_rights") or {}) if isinstance(snap, dict) else {}
        fixed_assets = (snap.get("fixed_assets") or {}) if isinstance(snap, dict) else {}

        for team_id, assets in deal.legs.items():
            for asset in assets:
                asset_key_value = asset_key(asset)
                if isinstance(asset, PlayerAsset):
                    pid = str(normalize_player_id(asset.player_id, strict=False, allow_legacy_numeric=True))
                    from_team_id = str(normalize_team_id(team_id, strict=True))
                    try:
                        current_team_id = repo.get_team_id_by_player(pid)
                    except Exception as exc:
                        raise ValueError(f"Player not found in roster: {asset.player_id}") from exc
                    if current_team_id != from_team_id:
                        raise ValueError(
                            f"Player {asset.player_id} not owned by {from_team_id} (current: {current_team_id})"
                        )
                    to_team_id = str(normalize_team_id(_resolve_receiver(deal, team_id, asset), strict=True))
                    salary_amount = repo.get_salary_amount(pid)
                    player_snapshots.append(
                        {
                            "player_id": pid,
                            "from_team_id": from_team_id,
                            "to_team_id": to_team_id,
                            "salary_amount": int(salary_amount) if salary_amount is not None else None,
                        }
                    )
                elif isinstance(asset, PickAsset):
                    pick = draft_picks.get(asset.pick_id, {})
                    ownership_snapshot[asset_key_value] = {
                        "owner_team": str(pick.get("owner_team", "")).upper(),
                        "protection": pick.get("protection"),
                    }
                elif isinstance(asset, SwapAsset):
                    swap = swap_rights.get(asset.swap_id, {})
                    ownership_snapshot[asset_key_value] = {
                        "owner_team": str(swap.get("owner_team", "")).upper()
                    }
                elif isinstance(asset, FixedAsset):
                    fixed = fixed_assets.get(asset.asset_id, {})
                    ownership_snapshot[asset_key_value] = {
                        "owner_team": str(fixed.get("owner_team", "")).upper()
                    }

        player_snapshots.sort(
            key=lambda row: (row["player_id"], row["from_team_id"], row["to_team_id"])
        )
        ownership_snapshot["players"] = player_snapshots
        payload = {"deal": serialize_deal(deal), "ownership": ownership_snapshot}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_committed_deal(
    deal: Deal,
    valid_days: int = 2,
    current_date: Optional[date] = None,
) -> Dict[str, Any]:
    canonical = canonicalize_deal(deal)
    validate_deal(canonical, current_date=current_date or get_current_date_as_date())
    deal_id = str(uuid4())
    assets_hash = _compute_assets_hash(canonical)
    today = current_date or get_current_date_as_date()
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
            locks[asset_key(asset)] = {"deal_id": deal_id, "expires_at": expires_at}


def verify_committed_deal(deal_id: str, current_date: Optional[date] = None) -> Deal:
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
    today = current_date or get_current_date_as_date()
    if expires_at and today > date.fromisoformat(str(expires_at)):
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
            lock = locks.get(asset_key(asset))
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
    today = current_date or get_current_date_as_date()
    for deal_id, entry in list(agreements.items()):
        if entry.get("status") != "ACTIVE":
            continue
        expires_at = entry.get("expires_at")
        if expires_at and today > date.fromisoformat(str(expires_at)):
            entry["status"] = "EXPIRED"
            release_locks_for_deal(deal_id)
