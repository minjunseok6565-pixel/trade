from __future__ import annotations

from datetime import date
from typing import Optional

import random

from config import ROSTER_DF
from state import GAME_STATE, _ensure_league_state, get_current_date_as_date
from team_utils import _init_players_and_teams_if_needed, get_team_status_map
from trades.apply import apply_deal
from trades.errors import TradeError
from trades.models import canonicalize_deal, parse_deal
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
    _init_players_and_teams_if_needed()
    from state import initialize_master_schedule_if_needed

    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
    draft_picks = GAME_STATE.get("draft_picks", {})
    asset_locks = GAME_STATE.get("asset_locks", {})

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

    for contender in contenders:
        for rebuild in rebuilders:
            if contender == rebuild:
                continue

            roster_reb = ROSTER_DF[ROSTER_DF["Team"] == rebuild]
            roster_cont = ROSTER_DF[ROSTER_DF["Team"] == contender]
            if roster_reb.empty or roster_cont.empty:
                continue

            vet_candidates = roster_reb[roster_reb["Age"] >= 28]
            if vet_candidates.empty:
                continue

            vet_candidates = vet_candidates.sort_values(by="OVR", ascending=False)
            veteran_id = int(vet_candidates.index[0])
            if f"player:{veteran_id}" in asset_locks:
                continue

            pick_candidates = [
                pick
                for pick in draft_picks.values()
                if pick.get("owner_team") == contender
                and pick.get("round") == 2
                and int(pick.get("year", current_year)) >= current_year
                and f"pick:{pick.get('pick_id')}" not in asset_locks
            ]
            if not pick_candidates:
                continue

            pick = sorted(pick_candidates, key=lambda p: (p.get("year", 0), p.get("pick_id")))[0]
            pick_id = pick.get("pick_id")

            payload = {
                "teams": [contender, rebuild],
                "legs": {
                    contender: [{"kind": "pick", "pick_id": pick_id}],
                    rebuild: [{"kind": "player", "player_id": veteran_id}],
                },
            }

            deal = canonicalize_deal(parse_deal(payload))
            trade_date = target_date or get_current_date_as_date()
            validate_deal(deal, current_date=trade_date)
            apply_deal(deal, source="ai_gm", trade_date=trade_date)
            return True

    return False
