from __future__ import annotations

from typing import Any, Dict, Optional

from state import GAME_STATE, get_current_date, get_current_date_as_date

from .models import Deal, PlayerAsset, PickAsset


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
        assets_summary[team_id] = {"players": players, "picks": picks}

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

    GAME_STATE.setdefault("transactions", []).append(entry)
    return entry
