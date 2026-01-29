from __future__ import annotations

from datetime import date
from typing import Optional

import logging
import random

logger = logging.getLogger(__name__)

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
from state import (
    asset_locks_get,
    export_full_state_snapshot,
    export_workflow_state,
    get_current_date_as_date,
    get_db_path,
    get_league_context_snapshot,
    set_last_gm_tick_date,
)
from team_utils import _init_players_and_teams_if_needed, get_team_status_map
from trades.apply import apply_deal_to_db
from trades.errors import TradeError
from trades.models import PickAsset, PlayerAsset, asset_key, canonicalize_deal, parse_deal
from trades.validator import validate_deal
from trades import agreements


def _run_ai_gm_tick_if_needed(target_date: date) -> None:
    league_context = get_league_context_snapshot()
    trade_deadline_str = league_context.get("trade_rules", {}).get("trade_deadline")
    if trade_deadline_str:
        try:
            trade_deadline = date.fromisoformat(trade_deadline_str)
            if target_date > trade_deadline:
                return
        except ValueError:
            return

    last_tick = export_full_state_snapshot().get("league", {}).get("last_gm_tick_date")
    if last_tick:
        try:
            last_date = date.fromisoformat(str(last_tick))
            if (target_date - last_date).days < 7:
                return
        except ValueError:
            pass

    agreements.gc_expired_agreements(current_date=target_date)

    try:
        _attempt_ai_trade(target_date)
    except TradeError as exc:
        logger.warning(
            "[AI_GM_TRADE_ERROR] TradeError during AI GM tick (target_date=%s): %s",
            target_date.isoformat(),
            str(exc),
            exc_info=True,
        )
    except Exception as exc:
        logger.exception(
            "[AI_GM_TICK_FAILED] Unexpected error during AI GM tick (target_date=%s)",
            target_date.isoformat(),
        )
        raise

    set_last_gm_tick_date(target_date.isoformat())


def _attempt_ai_trade(target_date: Optional[date] = None) -> bool:
    _init_players_and_teams_if_needed()
    league = export_full_state_snapshot().get("league", {})
    db_path = get_db_path()
    repo = LeagueRepo(db_path)
    repo.init_db()
    # Trade assets are stored in SQLite (SSOT). The workflow snapshot excludes them by default.
    try:
        assets_snapshot = repo.get_trade_assets_snapshot()
        draft_picks = assets_snapshot.get("draft_picks", {}) or {}
    except Exception:
        # Fallback for degraded environments/tests.
        draft_picks = export_workflow_state(exclude_keys=()).get("draft_picks", {}) or {}
    asset_locks = asset_locks_get()

    team_status = get_team_status_map()
    contenders = [tid for tid, status in team_status.items() if status == "contender"]
    rebuilders = [tid for tid, status in team_status.items() if status == "rebuild"]

    if not contenders or not rebuilders:
        return False

    random.shuffle(contenders)
    random.shuffle(rebuilders)

    current_year = league.get("draft_year")
    if not current_year:
        base_year = target_date.year if target_date else get_current_date_as_date().year
        current_year = base_year + 1

    try:
        for contender in contenders:
            for rebuild in rebuilders:
                if contender == rebuild:
                    continue

                contender_id = str(normalize_team_id(contender, strict=True))
                rebuild_id = str(normalize_team_id(rebuild, strict=True))
                roster_reb = repo.get_team_roster(rebuild_id)
                roster_cont = repo.get_team_roster(contender_id)
                if not roster_reb or not roster_cont:
                    continue

                vet_candidates = [
                    row for row in roster_reb if (row.get("age") or 0) >= 28
                ]
                if not vet_candidates:
                    continue

                vet_candidates.sort(
                    key=lambda row: (-(row.get("ovr") or 0), row.get("player_id", ""))
                )
                veteran_id = str(
                    normalize_player_id(
                        vet_candidates[0].get("player_id"),
                        strict=False,
                        allow_legacy_numeric=True,
                    )
                )
                if asset_key(PlayerAsset(kind="player", player_id=veteran_id)) in asset_locks:
                    continue

                pick_candidates = [
                    pick
                    for pick in draft_picks.values()
                    if pick.get("owner_team") == contender_id
                    and pick.get("round") == 2
                    and int(pick.get("year", current_year)) >= current_year
                    and pick.get("protection") is None
                    and asset_key(PickAsset(kind="pick", pick_id=str(pick.get("pick_id"))))
                    not in asset_locks
                ]
                if not pick_candidates:
                    continue

                pick = sorted(pick_candidates, key=lambda p: (p.get("year", 0), p.get("pick_id")))[0]
                pick_id = pick.get("pick_id")

                payload = {
                    "teams": [contender_id, rebuild_id],
                    "legs": {
                        contender_id: [{"kind": "pick", "pick_id": pick_id}],
                        rebuild_id: [{"kind": "player", "player_id": veteran_id}],
                    },
                }

                deal = canonicalize_deal(parse_deal(payload))
                trade_date = target_date or get_current_date_as_date()
                validate_deal(deal, current_date=trade_date)
                apply_deal_to_db(
                    db_path=get_db_path(),
                    deal=deal,
                    source="ai_gm",
                    deal_id=None,
                    trade_date=trade_date,
                    dry_run=False,
                )
                return True
    finally:
        repo.close()

    return False
