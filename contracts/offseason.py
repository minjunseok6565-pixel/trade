"""Offseason contract handling."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}


def _warn_limited(code: str, msg: str, *, limit: int = 5) -> None:
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1


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
) -> dict:

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

        # 1) 계약 만료/옵션 처리 (SSOT)
        expire_result = svc.expire_contracts_for_season_transition(
            fy,
            ty,
            decision_policy=decision_policy_to_pass,
        )

        expired = int(expire_result.get("expired") or 0)
        released = int(expire_result.get("released") or 0)
        released_ids = [str(x) for x in (expire_result.get("released_player_ids") or [])]

        # NOTE: UI cache updates are handled outside via state.start_new_season() post-mutation
        # (ui_cache_rebuild_all). Do not mutate legacy game_state["players"] here.

        # 2) 드래프트 정산(보호/스왑)은 draft 엔진(draft.finalize / draft.engine)에서 수행한다.
        # contracts.offseason은 계약/옵션 처리만 담당한다.
        settlement_result: Dict[str, Any] = {
            "draft_year": int(fy + 1),
            "ok": False,
            "skipped": True,
            "reason": "handled_by_draft_engine",
        }

        svc.repo.validate_integrity()

    return {
        "expired": expired,
        "released": released,
        "contracts_transition": expire_result,
        "trade_settlement": settlement_result,
    }
