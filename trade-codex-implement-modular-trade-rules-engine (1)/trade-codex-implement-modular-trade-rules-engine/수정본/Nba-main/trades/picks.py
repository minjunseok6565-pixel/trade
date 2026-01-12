from __future__ import annotations

from typing import List

from .errors import TradeError, PICK_NOT_OWNED
from league_repo import LeagueRepo


def _get_db_path(game_state: dict) -> str:
    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required to manage draft picks")
    return db_path


def init_draft_picks_if_needed(
    game_state: dict,
    draft_year: int,
    all_team_ids: List[str],
    years_ahead: int = 7,
) -> None:
    # NOTE:
    # 기존 구현은 draft_picks가 조금이라도 있으면 return 하여,
    # 규정(예: Stepien lookahead) 변경이나 버그 수정으로 "미래 연도 픽"이 더 필요해져도
    # 기존 세이브 상태에서는 생성 범위를 확장할 수 없었다.
    # 아래는 "누락된 픽만" 추가 생성하는 방식(idempotent)이라 기존 상태를 깨지 않으면서 확장 가능하다.
    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            picks: list[dict] = []
            for year in range(draft_year, draft_year + years_ahead + 1):
                for round_num in (1, 2):
                    for team_id in all_team_ids:
                        pick_id = f"{year}_R{round_num}_{team_id}"
                        picks.append(
                            {
                                "pick_id": pick_id,
                                "year": int(year),
                                "round": int(round_num),
                                "original_team": str(team_id).upper(),
                                "owner_team": str(team_id).upper(),
                                "protection": None,
                            }
                        )
            repo.upsert_picks(picks, cursor=cur)


def transfer_pick(game_state: dict, pick_id: str, from_team: str, to_team: str) -> None:
    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            pick = repo.get_pick(pick_id)
            if not pick:
                raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pick_id})
            owner_team = str(pick.get("owner_team") or "").upper()
            if owner_team != from_team.upper():
                raise TradeError(
                    PICK_NOT_OWNED,
                    "Pick not owned by team",
                    {"pick_id": pick_id, "team_id": from_team},
                )
            repo.update_pick_owner(pick_id, to_team.upper(), cursor=cur)
