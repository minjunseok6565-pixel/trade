"""Offseason contract handling."""

from __future__ import annotations

import json
import os

from league_repo import LeagueRepo
from schema import season_id_from_year


def _parse_season_year(season_id: str | None) -> int | None:
    if not season_id:
        return None
    try:
        return int(str(season_id).split("-")[0])
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_contract_payload(raw_json: str | None) -> tuple[dict, list, bool]:
    if not raw_json:
        return {}, [], False
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return {}, [], False
    if isinstance(payload, dict) and (
        "salary_by_year" in payload or "options" in payload
    ):
        salary_by_year = payload.get("salary_by_year") or {}
        options = payload.get("options") or []
        return salary_by_year, options, True
    if isinstance(payload, dict):
        return payload, [], False
    return {}, [], False


def process_offseason(
    game_state: dict,
    from_season_year: int,
    to_season_year: int,
    decision_policy=None,
    draft_pick_order_by_pick_id: dict[str, int] | None = None,
) -> dict:
    from contracts.options import (
        apply_option_decision,
        get_pending_options_for_season,
        normalize_option_record,
        recompute_contract_years_from_salary,
    )
    from contracts.options_policy import default_option_decision_policy
    from contracts.store import get_current_date_iso
    from contracts.ops import release_to_free_agents
    expired = 0
    released = 0
    decision_date_iso = get_current_date_iso(game_state)
    if decision_policy is None:
        decision_policy = default_option_decision_policy

    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path") or os.environ.get("LEAGUE_DB_PATH") or "league.db"
    league_state["db_path"] = db_path
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            rows = cur.execute(
                """
                SELECT contract_id, player_id, team_id, start_season_id, end_season_id,
                       salary_by_season_json, is_active
                FROM contracts
                WHERE is_active=1;
                """
            ).fetchall()
            for row in rows:
                contract_id = row["contract_id"]
                player_id_str = str(row["player_id"])
                start_year = _parse_season_year(row["start_season_id"])
                end_year = _parse_season_year(row["end_season_id"])

                salary_by_year, options, has_payload_meta = _parse_contract_payload(
                    row["salary_by_season_json"]
                )
                contract = {
                    "contract_id": contract_id,
                    "player_id": player_id_str,
                    "team_id": row["team_id"],
                    "start_season_year": start_year or 0,
                    "salary_by_year": salary_by_year,
                    "options": options,
                    "status": "ACTIVE",
                }

                try:
                    contract["options"] = [
                        normalize_option_record(option) for option in (options or [])
                    ]
                except ValueError:
                    contract["options"] = []

                try:
                    player_id = int(player_id_str)
                except (TypeError, ValueError):
                    player_id = None

                pending = get_pending_options_for_season(contract, to_season_year)
                if pending:
                    for option_index, option in enumerate(contract["options"]):
                        if option.get("season_year") != to_season_year:
                            continue
                        if option.get("status") != "PENDING":
                            continue
                        decision = decision_policy(option, player_id, contract, game_state)
                        apply_option_decision(
                            contract,
                            option_index,
                            decision,
                            decision_date_iso,
                        )
                    recompute_contract_years_from_salary(contract)

                    if start_year:
                        end_year = start_year + int(contract.get("years") or 0) - 1
                    payload = contract.get("salary_by_year", {})
                    if has_payload_meta or contract.get("options"):
                        payload = {
                            "salary_by_year": contract.get("salary_by_year", {}),
                            "options": contract.get("options", []),
                        }
                    cur.execute(
                        """
                        UPDATE contracts
                        SET salary_by_season_json=?, end_season_id=?, updated_at=?
                        WHERE contract_id=?;
                        """,
                        (
                            json.dumps(payload),
                            season_id_from_year(end_year) if end_year else None,
                            decision_date_iso,
                            contract_id,
                        ),
                    )

                if end_year is None and start_year and salary_by_year:
                    try:
                        end_year = max(int(year) for year in salary_by_year.keys())
                    except (TypeError, ValueError):
                        end_year = start_year

                if end_year is not None and to_season_year >= end_year + 1:
                    release_to_free_agents(
                        game_state,
                        player_id_str,
                        released_date=None,
                        repo=repo,
                        cursor=cur,
                        validate=False,
                    )
                    expired += 1
                    released += 1
        repo.validate_integrity()

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
        from trades.pick_settlement import settle_draft_year

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
