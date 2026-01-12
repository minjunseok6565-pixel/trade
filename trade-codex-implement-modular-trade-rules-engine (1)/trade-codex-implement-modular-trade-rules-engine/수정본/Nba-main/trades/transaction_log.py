from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from league_repo import LeagueRepo
from state import get_current_date, get_current_date_as_date

from .models import Deal, FixedAsset, PickAsset, PlayerAsset, SwapAsset


def append_trade_transaction(
    deal: Deal,
    source: str,
    deal_id: Optional[str] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current_date = get_current_date() or get_current_date_as_date().isoformat()
    assets_summary: Dict[str, Dict[str, list]] = {}
    for team_id, assets in deal.legs.items():
        players = [asset.player_id for asset in assets if isinstance(asset, PlayerAsset)]
        picks = [asset.pick_id for asset in assets if isinstance(asset, PickAsset)]
        pick_protections = [
            {
                "pick_id": asset.pick_id,
                "protection": asset.protection,
                "to_team": asset.to_team,
            }
            for asset in assets
            if isinstance(asset, PickAsset) and asset.protection is not None
        ]
        swaps = [
            {
                "swap_id": asset.swap_id,
                "pick_id_a": asset.pick_id_a,
                "pick_id_b": asset.pick_id_b,
                "to_team": asset.to_team,
            }
            for asset in assets
            if isinstance(asset, SwapAsset)
        ]
        fixed_assets = [
            {"asset_id": asset.asset_id, "to_team": asset.to_team}
            for asset in assets
            if isinstance(asset, FixedAsset)
        ]
        assets_summary[team_id] = {
            "players": players,
            "picks": picks,
            "pick_protections": pick_protections,
            "swaps": swaps,
            "fixed_assets": fixed_assets,
        }

    entry: Dict[str, Any] = {
        "type": "trade",
        "date": current_date,
        "teams": list(deal.teams),
        "assets": assets_summary,
        "source": source,
    }
    if deal_id:
        entry["deal_id"] = deal_id
    if extra_meta:
        entry["meta"] = dict(extra_meta)

    db_path = os.environ.get("LEAGUE_DB_PATH") or "league.db"
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            entry["transaction_id"] = repo.log_transaction(
                current_date,
                json.dumps(entry),
                cursor=cur,
            )
        repo.validate_integrity()
    return entry
