from __future__ import annotations

import os
import contextlib
from typing import Any, Dict, Optional
from typing import Mapping, Iterator

from state import GAME_STATE, get_current_date, get_current_date_as_date

from .models import Deal, FixedAsset, PickAsset, PlayerAsset, SwapAsset


def _get_db_path() -> str:
    league = GAME_STATE.get("league", {})
    if isinstance(league, dict):
        db_path = league.get("db_path")
        if db_path:
            return str(db_path)
    return os.environ.get("LEAGUE_DB_PATH", "league.db")


@contextlib.contextmanager
def _open_service(db_path: str) -> Iterator[Any]:
    """
    Open LeagueService using the project's canonical entrypoint: LeagueService.open(db_path).
    Supports both:
      - open() returning a context manager (preferred)
      - open() returning a service object with optional .close()
    """
    from league_service import LeagueService  # local import to avoid cycles

    svc_or_cm = LeagueService.open(db_path)  # canonical style
    if hasattr(svc_or_cm, "__enter__"):
        with svc_or_cm as svc:
            yield svc
        return

    svc = svc_or_cm
    try:
        yield svc
    finally:
        close = getattr(svc, "close", None)
        if callable(close):
            close()


def _persist_transaction(entry: Mapping[str, Any]) -> None:
    """
    Persist to DB (SSOT). No more GAME_STATE['transactions'] ledger.
    """
    db_path = _get_db_path()
    with _open_service(db_path) as svc:
        if hasattr(svc, "append_transaction"):
            svc.append_transaction(dict(entry))
            return
        if hasattr(svc, "append_transactions"):
            svc.append_transactions([dict(entry)])
            return
        raise RuntimeError("LeagueService missing append_transaction(s) API")


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

    # DB is SSOT for transactions_log after migration.
    # Keep this function as a legacy adapter: build the entry and persist to DB.
    _persist_transaction(entry)
    return entry
