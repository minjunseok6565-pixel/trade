"""Offseason contract handling."""

from __future__ import annotations

from typing import Any, Dict, Optional

from league_service import LeagueService

def _get_db_path(game_state: dict) -> str:
    # Fail-fast: safest behavior is to never silently write to a default DB.
    league_state = game_state.get("league")
    if not isinstance(league_state, dict):
        raise ValueError("game_state['league'] must be a dict and contain 'db_path'")
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required")
    return str(db_path)


def process_offseason(
    game_state: dict,
    from_season_year: int,
    to_season_year: int,
    decision_policy=None,
    draft_pick_order_by_pick_id: dict[str, int] | None = None,
) -> dict:

    """
    Offseason handler (DB SSOT).
    - 계약 SSOT는 DB (contracts/active_contracts/player_contracts/free_agents 포함).
    - 이 함수는 GAME_STATE의 레거시 계약 장부를 읽거나 생성하지 않는다.
    - 오프시즌 계약 만료/옵션 처리:
        LeagueService.expire_contracts_for_season_transition(...)
    - (선택) 드래프트 픽/스왑/보호 정산:
        LeagueService.settle_draft_year(...)
    - GAME_STATE는 workflow/UI 캐시만 best-effort로 갱신한다.
    """

    from trades.errors import TradeError
    from contracts.options_policy import default_option_decision_policy

    # Validate years early (avoid partial writes with invalid inputs).
    fy = int(from_season_year)
    ty = int(to_season_year)
    if fy <= 0 or ty <= 0:
        raise ValueError("from_season_year and to_season_year must be positive ints")
 
    # Fail-fast DB path acquisition (no env/default fallback).
    db_path = _get_db_path(game_state)

    # Wrap policy so it can see the real game_state even if LeagueService passes a stub.
    if decision_policy is None:
        decision_policy = default_option_decision_policy
    if callable(decision_policy):
        user_policy = decision_policy

        def _wrapped_policy(option: dict, player_id: str, contract: dict, _stub_state: dict):
            return user_policy(option, player_id, contract, game_state)

        decision_policy_to_pass = _wrapped_policy
    else:
        decision_policy_to_pass = None

    # Run DB writes in a single service context.
    with LeagueService.open(db_path) as svc:
        svc.repo.init_db()

        # 1) 계약 만료/옵션 처리 (SSOT)
        expire_result = svc.expire_contracts_for_season_transition(
            fy,
            ty,
            decision_policy=decision_policy_to_pass,
        )

        expired = int(expire_result.get("expired") or 0)
        released = int(expire_result.get("released") or 0)
        released_ids = [str(x) for x in (expire_result.get("released_player_ids") or [])]

        # Maintain minimal workflow/UI cache in GAME_STATE for released players (best-effort)
        try:
            players_cache = game_state.get("players")
            if isinstance(players_cache, dict):
                for pid in released_ids:
                    p = players_cache.get(pid)
                    if isinstance(p, dict):
                        # Keep cache consistent with DB semantics: FA team id is "FA".
                        p["team_id"] = "FA"
        except Exception:
            pass

        # 2) (선택) 드래프트 정산 (SSOT)
        draft_year_to_settle: Optional[int]
        try:
            draft_year_to_settle = fy + 1
        except Exception:
            draft_year_to_settle = None

        pick_order: Optional[Dict[str, int]] = None
        if isinstance(draft_pick_order_by_pick_id, dict) and draft_pick_order_by_pick_id:
            pick_order = draft_pick_order_by_pick_id
        else:
            orders = game_state.get("draft_pick_orders")
            if isinstance(orders, dict) and draft_year_to_settle is not None:
                pick_order_candidate = orders.get(draft_year_to_settle) or orders.get(str(draft_year_to_settle))
                if isinstance(pick_order_candidate, dict) and pick_order_candidate:
                    pick_order = pick_order_candidate

        if draft_year_to_settle is None:
            settlement_result: Dict[str, Any] = {
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
            # sanitize pick_order values to int to avoid downstream type surprises
            pick_order_i: Dict[str, int] = {}
            for k, v in dict(pick_order).items():
                try:
                    pick_order_i[str(k)] = int(v)
                except Exception:
                    continue
            try:
                events = svc.settle_draft_year(int(draft_year_to_settle), pick_order_i)
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
            except Exception as exc:
                settlement_result = {
                    "draft_year": draft_year_to_settle,
                    "ok": False,
                    "error": {
                        "code": "SETTLEMENT_ERROR",
                        "message": str(exc),
                        "details": {},
                    },
                }
            else:
                settlement_result = {
                    "draft_year": draft_year_to_settle,
                    "ok": True,
                    "events_count": len(events),
                    "events": events,
                }

        svc.repo.validate_integrity()

    return {
        "expired": expired,
        "released": released,
        "contracts_transition": expire_result,
        "trade_settlement": settlement_result,
    }
