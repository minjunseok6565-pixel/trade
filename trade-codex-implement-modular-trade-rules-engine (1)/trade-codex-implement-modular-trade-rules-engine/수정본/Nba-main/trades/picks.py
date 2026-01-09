from __future__ import annotations

from typing import List

from .errors import TradeError, PICK_NOT_OWNED


def init_draft_picks_if_needed(
    game_state: dict,
    draft_year: int,
    all_team_ids: List[str],
    years_ahead: int = 7,
) -> None:
    draft_picks = game_state.setdefault("draft_picks", {})
    # NOTE:
    # 기존 구현은 draft_picks가 조금이라도 있으면 return 하여,
    # 규정(예: Stepien lookahead) 변경이나 버그 수정으로 "미래 연도 픽"이 더 필요해져도
    # 기존 세이브 상태에서는 생성 범위를 확장할 수 없었다.
    # 아래는 "누락된 픽만" 추가 생성하는 방식(idempotent)이라 기존 상태를 깨지 않으면서 확장 가능하다.

    for year in range(draft_year, draft_year + years_ahead + 1):
        for round_num in (1, 2):
            for team_id in all_team_ids:
                pick_id = f"{year}_R{round_num}_{team_id}"
                if pick_id in draft_picks:
                    continue
                draft_picks[pick_id] = {
                    "pick_id": pick_id,
                    "year": year,
                    "round": round_num,
                    "original_team": team_id,
                    "owner_team": team_id,
                    "protection": None,
                }


def transfer_pick(game_state: dict, pick_id: str, from_team: str, to_team: str) -> None:
    draft_picks = game_state.get("draft_picks") or {}
    pick = draft_picks.get(pick_id)
    if not pick:
        raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pick_id})
    if str(pick.get("owner_team", "")).upper() != from_team.upper():
        raise TradeError(
            PICK_NOT_OWNED,
            "Pick not owned by team",
            {"pick_id": pick_id, "team_id": from_team},
        )
    pick["owner_team"] = to_team.upper()
