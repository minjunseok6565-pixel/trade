from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

import random

from config import ROSTER_DF, HARD_CAP
from state import GAME_STATE, _ensure_league_state
from team_utils import (
    _init_players_and_teams_if_needed,
    _compute_team_payroll,
    _compute_team_records,
    _evaluate_team_needs,
    _position_group,
    _player_value_for_team,
)


def _would_break_hard_cap(
    team_a_id: str,
    team_b_id: str,
    players_from_a: List[int],
    players_from_b: List[int],
) -> bool:
    """트레이드 후 양 팀의 페이롤이 하드캡을 초과하는지 여부를 계산."""
    payroll_a_before = _compute_team_payroll(team_a_id)
    payroll_b_before = _compute_team_payroll(team_b_id)

    # 이동하는 선수들의 샐러리
    df_a = ROSTER_DF.loc[players_from_a]
    df_b = ROSTER_DF.loc[players_from_b]

    out_a = float(df_a["SalaryAmount"].sum()) if not df_a.empty else 0.0
    out_b = float(df_b["SalaryAmount"].sum()) if not df_b.empty else 0.0

    in_a = out_b
    in_b = out_a

    payroll_a_after = payroll_a_before - out_a + in_a
    payroll_b_after = payroll_b_before - out_b + in_b

    return payroll_a_after > HARD_CAP or payroll_b_after > HARD_CAP


def _execute_trade(
    trade_date: str,
    team_a_id: str,
    team_b_id: str,
    players_from_a: List[int],
    players_from_b: List[int],
) -> None:
    """실제로 트레이드를 적용.

    - ROSTER_DF의 Team 값을 교환
    - GAME_STATE["players"]의 team_id 업데이트
    - GAME_STATE["transactions"], GAME_STATE["cached_views"]["news"]에 기록
    """
    # 먼저 하드캡 체크
    if _would_break_hard_cap(team_a_id, team_b_id, players_from_a, players_from_b):
        return

    # 선수 이동
    for pid in players_from_a:
        if pid in ROSTER_DF.index:
            ROSTER_DF.at[pid, "Team"] = team_b_id
        if pid in GAME_STATE["players"]:
            GAME_STATE["players"][pid]["team_id"] = team_b_id

    for pid in players_from_b:
        if pid in ROSTER_DF.index:
            ROSTER_DF.at[pid, "Team"] = team_a_id
        if pid in GAME_STATE["players"]:
            GAME_STATE["players"][pid]["team_id"] = team_a_id

    # 트랜잭션 로그
    transaction = {
        "date": trade_date,
        "type": "trade",
        "teams_involved": [team_a_id, team_b_id],
        "players_from_a": players_from_a,
        "players_from_b": players_from_b,
    }
    GAME_STATE["transactions"].append(transaction)

    # 뉴스 추가
    news_items = GAME_STATE["cached_views"]["news"]["items"]

    def _player_name(pid: int) -> str:
        pmeta = GAME_STATE["players"].get(pid)
        if pmeta:
            return pmeta.get("name") or f"Player {pid}"
        if pid in ROSTER_DF.index:
            row = ROSTER_DF.loc[pid]
            return str(row.get("Name", f"Player {pid}"))
        return f"Player {pid}"

    names_a = ", ".join(_player_name(pid) for pid in players_from_a) or "무명 선수"
    names_b = ", ".join(_player_name(pid) for pid in players_from_b) or "무명 선수"

    title = f"{team_a_id}, {team_b_id}와 트레이드 단행"
    summary = f"{team_a_id}는 {names_b}를 받고, {team_b_id}는 {names_a}를 영입했습니다."

    news_items.insert(0, {
        "news_id": f"trade_{trade_date}_{team_a_id}_{team_b_id}_{len(GAME_STATE['transactions'])}",
        "date": trade_date,
        "importance": "normal",
        "tags": ["trade"],
        "title": title,
        "summary": summary,
        "related_team_ids": [team_a_id, team_b_id],
        "related_player_ids": players_from_a + players_from_b,
    })


def _run_ai_gm_tick(current_date: date) -> None:
    """리그 전체를 대상으로 AI GM 트레이드를 시도.

    - 트레이드 데드라인 이후에는 아무 것도 하지 않음.
    - 일주일에 한 번 정도 호출되도록 외부에서 제어.
    """
    _init_players_and_teams_if_needed()
    from state import initialize_master_schedule_if_needed  # 지연 import
    initialize_master_schedule_if_needed()
    league = _ensure_league_state()

    # 트레이드 데드라인 체크
    trade_deadline_str = league["trade_rules"].get("trade_deadline")
    if trade_deadline_str:
        try:
            trade_deadline = date.fromisoformat(trade_deadline_str)
            if current_date > trade_deadline:
                return
        except ValueError:
            pass

    # 팀 성적/니즈 계산
    records = _compute_team_records()
    team_needs = _evaluate_team_needs(records)

    contenders = [tid for tid, info in team_needs.items() if info["status"] == "contender"]
    rebuilders = [tid for tid, info in team_needs.items() if info["status"] == "rebuild"]

    if not contenders or not rebuilders:
        return

    random.shuffle(contenders)
    random.shuffle(rebuilders)

    trades_done = 0    # 한 번의 틱에서 최대 3건 정도만
    max_trades = 3

    for cont_id in contenders:
        if trades_done >= max_trades:
            break

        # 파트너 리빌딩 팀 선택
        partner_candidates = rebuilders[:]
        random.shuffle(partner_candidates)
        for rebuild_id in partner_candidates:
            if cont_id == rebuild_id:
                continue

            if _try_trade_between_pair(cont_id, rebuild_id, team_needs, current_date):
                trades_done += 1
                if trades_done >= max_trades:
                    break
        if trades_done >= max_trades:
            break


def _try_trade_between_pair(
    cont_id: str,
    rebuild_id: str,
    team_needs: Dict[str, Dict[str, Any]],
    current_date: date,
) -> bool:
    """컨텐더 팀과 리빌딩 팀 사이에 단순 1:1 트레이드를 시도.

    - 컨텐더는 리빌딩 팀의 베테랑 스타를 노리고
    - 리빌딩은 컨텐더의 젊은 유망주를 노린다.
    - 하드캡 및 기본 밸런스를 만족하는 경우에만 트레이드 실행.
    """
    roster_cont = ROSTER_DF[ROSTER_DF["Team"] == cont_id]
    roster_reb = ROSTER_DF[ROSTER_DF["Team"] == rebuild_id]
    if roster_cont.empty or roster_reb.empty:
        return False

    needs_cont = team_needs.get(cont_id, {})
    cont_need_positions = needs_cont.get("need_positions", [])

    # 리빌딩 팀에서 "팔려는" 베테랑 스타 후보
    cand_reb = roster_reb.copy()
    cand_reb["pos_group"] = cand_reb["POS"].apply(_position_group)
    cand_reb = cand_reb[
        (cand_reb["OVR"] >= 80) &
        (cand_reb["Age"] >= 26)
    ]
    if cont_need_positions:
        cand_reb = cand_reb[cand_reb["pos_group"].isin(cont_need_positions)]
    if cand_reb.empty:
        return False

    # 컨텐더 팀에서 "내보낼" 젊은 유망주 후보
    cand_cont = roster_cont.copy()
    cand_cont = cand_cont[
        (cand_cont["Age"] <= 26) &
        (cand_cont["OVR"] >= 72) &
        (cand_cont["OVR"] <= 86)
    ]
    if cand_cont.empty:
        return False

    # 가치 계산
    cand_reb = cand_reb.copy()
    cand_reb["value_for_cont"] = cand_reb.apply(
        lambda row: _player_value_for_team(row, "contender"), axis=1
    )
    cand_reb = cand_reb.sort_values("value_for_cont", ascending=False)

    cand_cont = cand_cont.copy()
    cand_cont["value_for_reb"] = cand_cont.apply(
        lambda row: _player_value_for_team(row, "rebuild"), axis=1
    )
    cand_cont = cand_cont.sort_values("value_for_reb", ascending=False)

    # 상위 몇 명 안에서만 조합 시도
    TOP_N = 5
    cand_reb = cand_reb.head(TOP_N).reset_index().rename(columns={"index": "player_id"})
    cand_cont = cand_cont.head(TOP_N).reset_index().rename(columns={"index": "player_id"})

    for _, star_row in cand_reb.iterrows():
        star_pid = int(star_row["player_id"])
        star_value_for_cont = float(star_row["value_for_cont"])

        for _, prospect_row in cand_cont.iterrows():
            prospect_pid = int(prospect_row["player_id"])
            prospect_value_for_reb = float(prospect_row["value_for_reb"])

            if star_pid == prospect_pid:
                continue

            # 컨텐더/리빌딩 입장에서 가치 개선 여부 간단 체크
            value_gain_cont = star_value_for_cont - _player_value_for_team(
                ROSTER_DF.loc[prospect_pid], "contender"
            )
            value_gain_reb = prospect_value_for_reb - _player_value_for_team(
                ROSTER_DF.loc[star_pid], "rebuild"
            )

            if value_gain_cont <= 0.5 or value_gain_reb <= 0.5:
                continue

            # 하드캡 룰 체크
            if _would_break_hard_cap(
                cont_id, rebuild_id,
                players_from_a=[prospect_pid],
                players_from_b=[star_pid],
            ):
                continue

            trade_date_str = current_date.isoformat()
            _execute_trade(
                trade_date=trade_date_str,
                team_a_id=cont_id,
                team_b_id=rebuild_id,
                players_from_a=[prospect_pid],
                players_from_b=[star_pid],
            )
            return True

    return False


def _run_ai_gm_tick_if_needed(current_date: date) -> None:
    """league.last_gm_tick_date 기준으로 7일 이상 지났으면 _run_ai_gm_tick을 호출."""
    league = _ensure_league_state()
    last_str = league.get("last_gm_tick_date")
    if last_str:
        try:
            last_date = date.fromisoformat(last_str)
        except ValueError:
            last_date = None
    else:
        last_date = None

    if last_date is not None:
        if (current_date - last_date).days < 7:
            return

    # 트레이드 데드라인 지난 경우 _run_ai_gm_tick 내부에서 조용히 리턴됨
    _run_ai_gm_tick(current_date)
    league["last_gm_tick_date"] = current_date.isoformat()
