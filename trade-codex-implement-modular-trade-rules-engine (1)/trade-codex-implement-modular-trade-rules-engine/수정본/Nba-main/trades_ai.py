from __future__ import annotations

from datetime import date
from typing import Optional

import random

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
from state import GAME_STATE, _ensure_league_state, get_current_date_as_date
from team_utils import get_team_status_map
from trades.apply import apply_deal
from trades.errors import TradeError
from trades.models import PickAsset, PlayerAsset, asset_key, canonicalize_deal, parse_deal
from trades.validator import validate_deal
from trades import agreements


def _run_ai_gm_tick_if_needed(target_date: date) -> None:
    league = _ensure_league_state()
    trade_deadline_str = league.get("trade_rules", {}).get("trade_deadline")
    if trade_deadline_str:
        try:
            trade_deadline = date.fromisoformat(trade_deadline_str)
            if target_date > trade_deadline:
                return
        except ValueError:
            return

    last_tick = league.get("last_gm_tick_date")
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
    except TradeError:
        pass
    except Exception:
        pass

    league["last_gm_tick_date"] = target_date.isoformat()


def _attempt_ai_trade(target_date: Optional[date] = None) -> bool:
    from state import initialize_master_schedule_if_needed

    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
    db_path = league.get("db_path")
    if not db_path:
        raise ValueError("db_path is required for AI trade evaluation")
    repo = LeagueRepo(db_path)
    repo.init_db()
    draft_picks = repo.list_picks()

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
                if repo.get_asset_lock(asset_key(PlayerAsset(kind="player", player_id=veteran_id))):
                    continue

                pick_candidates = [
                    pick
                    for pick in draft_picks
                    if pick.get("owner_team") == contender_id
                    and pick.get("round") == 2
                    and int(pick.get("year", current_year)) >= current_year
                    and pick.get("protection") is None
                    and not repo.get_asset_lock(
                        asset_key(PickAsset(kind="pick", pick_id=str(pick.get("pick_id"))))
                    )
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
                apply_deal(deal, source="ai_gm", trade_date=trade_date)
                return True
    finally:
        repo.close()

    return False
