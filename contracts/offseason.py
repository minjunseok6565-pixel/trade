"""Offseason contract handling."""

from __future__ import annotations

import os

from league_repo import LeagueRepo
from league_service import LeagueService


def process_offseason(
    game_state: dict,
    from_season_year: int,
    to_season_year: int,
    decision_policy=None,
    draft_pick_order_by_pick_id: dict[str, int] | None = None,
) -> dict:
    from contracts.options_policy import default_option_decision_policy
    from trades.pick_settlement import settle_draft_year
    from trades.errors import TradeError

    """
    Offseason handler (DB SSOT).

    이전 구현은 GAME_STATE의 계약 장부(contracts/active_contract_id_by_player 등)를
    주 데이터로 삼았지만, 마이그레이션 이후 계약 SSOT는 DB다.
    따라서 오프시즌 계약 만료/옵션 처리는 LeagueService로 위임한다.
    """

    if decision_policy is None:
        decision_policy = default_option_decision_policy

    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path") or os.environ.get("LEAGUE_DB_PATH") or "league.db"
    league_state["db_path"] = db_path

    # 1) 계약 만료/옵션 처리: DB에서 처리 (SSOT)
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        svc = LeagueService(repo)
        expire_result = svc.expire_contracts_for_season_transition(
            int(from_season_year),
            int(to_season_year),
            decision_policy=decision_policy,
        )
        repo.validate_integrity()

    # Maintain minimal workflow/UI cache in GAME_STATE for released players
    expired = int(expire_result.get("expired") or 0)
    released = int(expire_result.get("released") or 0)
    released_ids = expire_result.get("released_player_ids") or []
    players_cache = game_state.get("players") or {}
    for pid in released_ids:
        p = players_cache.get(str(pid))
        if isinstance(p, dict):
            p["team_id"] = ""
            # acquired_date/etc are not strictly required for offseason,
            # but keeping prior semantics helps UI/debug
            # (decision date not exposed here; leave existing fields untouched)

    try:
        draft_year_to_settle = int(from_season_year) + 1
    except (TypeError, ValueError):
        draft_year_to_settle = None

    pick_order = None
    if draft_pick_order_by_pick_id:
        pick_order = draft_pick_order_by_pick_id
    else:
        orders = game_state.get("draft_pick_orders") or {}
        if draft_year_to_settle is not None:
            pick_order_candidate = orders.get(draft_year_to_settle) or orders.get(
                str(draft_year_to_settle)
            )
            if isinstance(pick_order_candidate, dict) and pick_order_candidate:
                pick_order = pick_order_candidate

    if draft_year_to_settle is None:
        settlement_result = {
            "draft_year": None,
            "ok": False,
            "skipped": True,
            "reason": "invalid_draft_year",
        }
    elif not pick_order:
        settlement_result = {
            "draft_year": draft_year_to_settle,
            "ok": False,
            "skipped": True,
            "reason": "missing_pick_order",
        }
    else:
        from trades.errors import TradeError

        try:
            events = settle_draft_year(game_state, draft_year_to_settle, pick_order)
        except TradeError as exc:
            settlement_result = {
                "draft_year": draft_year_to_settle,
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        else:
            settlement_result = {
                "draft_year": draft_year_to_settle,
                "ok": True,
                "events_count": len(events),
                "events": events,
            }

    return {"expired": expired, "released": released, "trade_settlement": settlement_result}
