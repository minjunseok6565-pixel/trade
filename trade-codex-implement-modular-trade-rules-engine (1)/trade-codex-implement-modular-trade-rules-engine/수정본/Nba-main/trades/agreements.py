from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
from state import get_current_date_as_date

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


def _get_db_path() -> str:
    return os.environ.get("LEAGUE_DB_PATH") or "league.db"


def _compute_assets_hash(deal: Deal) -> str:
    ownership_snapshot: Dict[str, Any] = {}
    player_snapshots: list[dict[str, Any]] = []
    repo = LeagueRepo(_get_db_path())
    repo.init_db()
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
                ownership_snapshot[asset_key_value] = {
                    "pick_id": asset.pick_id,
                    "protection": asset.protection,
                }
            elif isinstance(asset, SwapAsset):
                ownership_snapshot[asset_key_value] = {
                    "swap_id": asset.swap_id,
                    "pick_id_a": asset.pick_id_a,
                    "pick_id_b": asset.pick_id_b,
                }
            elif isinstance(asset, FixedAsset):
                ownership_snapshot[asset_key_value] = {"asset_id": asset.asset_id}

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

    db_path = _get_db_path()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            cur.execute(
                """
                INSERT INTO trade_agreements(
                    deal_id,
                    deal_json,
                    assets_hash,
                    created_at,
                    expires_at,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    deal_id,
                    json.dumps(entry["deal"]),
                    assets_hash,
                    entry["created_at"],
                    entry["expires_at"],
                    entry["status"],
                ),
            )
            _lock_assets_for_deal(cur, canonical, deal_id, entry["expires_at"])
        repo.validate_integrity()
    return entry


def _lock_assets_for_deal(cursor, deal: Deal, deal_id: str, expires_at: str) -> None:
    for assets in deal.legs.values():
        for asset in assets:
            cursor.execute(
                """
                INSERT OR REPLACE INTO asset_locks(asset_key, deal_id, expires_at)
                VALUES (?, ?, ?);
                """,
                (asset_key(asset), deal_id, expires_at),
            )


def verify_committed_deal(deal_id: str, current_date: Optional[date] = None) -> Deal:
    db_path = _get_db_path()
    today = current_date or get_current_date_as_date()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        entry_row = repo._conn.execute(
            "SELECT * FROM trade_agreements WHERE deal_id=?;",
            (deal_id,),
        ).fetchone()
        if not entry_row:
            raise TradeError(DEAL_INVALIDATED, "Committed deal not found")

        entry = dict(entry_row)
        status = entry.get("status")
        if status and status != "ACTIVE":
            if status == "EXECUTED":
                raise TradeError(DEAL_ALREADY_EXECUTED, "Deal already executed")
            if status == "EXPIRED":
                raise TradeError(DEAL_EXPIRED, "Deal expired")
            raise TradeError(DEAL_INVALIDATED, "Deal invalidated")

        expires_at = entry.get("expires_at")
        if expires_at and today > date.fromisoformat(str(expires_at)):
            with repo.transaction() as cur:
                cur.execute(
                    "UPDATE trade_agreements SET status=? WHERE deal_id=?;",
                    ("EXPIRED", deal_id),
                )
                release_locks_for_deal(deal_id, cursor=cur)
            raise TradeError(DEAL_EXPIRED, "Deal expired")

        deal_payload = json.loads(entry.get("deal_json") or "{}")
        deal = canonicalize_deal(parse_deal(deal_payload))

        if entry.get("assets_hash") != _compute_assets_hash(deal):
            with repo.transaction() as cur:
                cur.execute(
                    "UPDATE trade_agreements SET status=? WHERE deal_id=?;",
                    ("INVALIDATED", deal_id),
                )
                release_locks_for_deal(deal_id, cursor=cur)
            raise TradeError(DEAL_INVALIDATED, "Deal assets have changed")

        for assets in deal.legs.values():
            for asset in assets:
                lock = repo._conn.execute(
                    "SELECT deal_id FROM asset_locks WHERE asset_key=?;",
                    (asset_key(asset),),
                ).fetchone()
                if not lock or lock["deal_id"] != deal_id:
                    with repo.transaction() as cur:
                        cur.execute(
                            "UPDATE trade_agreements SET status=? WHERE deal_id=?;",
                            ("INVALIDATED", deal_id),
                        )
                        release_locks_for_deal(deal_id, cursor=cur)
                    raise TradeError(DEAL_INVALIDATED, "Asset lock missing")

        return deal


def mark_executed(deal_id: str) -> None:
    db_path = _get_db_path()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            cur.execute(
                "UPDATE trade_agreements SET status=? WHERE deal_id=?;",
                ("EXECUTED", deal_id),
            )
            release_locks_for_deal(deal_id, cursor=cur)
        repo.validate_integrity()


def release_locks_for_deal(deal_id: str, *, cursor=None) -> None:
    if cursor is None:
        db_path = _get_db_path()
        with LeagueRepo(db_path) as repo:
            repo.init_db()
            with repo.transaction() as cur:
                cur.execute("DELETE FROM asset_locks WHERE deal_id=?;", (deal_id,))
            repo.validate_integrity()
        return
    cursor.execute("DELETE FROM asset_locks WHERE deal_id=?;", (deal_id,))


def gc_expired_agreements(current_date: Optional[date] = None) -> None:
    today = current_date or get_current_date_as_date()
    db_path = _get_db_path()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        rows = repo._conn.execute(
            "SELECT deal_id, expires_at FROM trade_agreements WHERE status='ACTIVE';"
        ).fetchall()
        for row in rows:
            expires_at = row["expires_at"]
            if expires_at and today > date.fromisoformat(str(expires_at)):
                deal_id = row["deal_id"]
                with repo.transaction() as cur:
                    cur.execute(
                        "UPDATE trade_agreements SET status=? WHERE deal_id=?;",
                        ("EXPIRED", deal_id),
                    )
                    release_locks_for_deal(deal_id, cursor=cur)
        repo.validate_integrity()
