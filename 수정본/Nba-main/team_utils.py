from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

import pandas as pd

from config import ROSTER_DF, ALL_TEAM_IDS, TEAM_TO_CONF_DIV
from state import GAME_STATE, _ensure_league_state, initialize_master_schedule_if_needed


def _init_players_and_teams_if_needed() -> None:
    """GAME_STATE["players"], GAME_STATE["teams"]를 초기화한다.

    - players: ROSTER_DF row index를 player_id로 사용
    - teams: 기본 성향/시장규모 등 메타
    """
    if GAME_STATE["players"]:
        return

    players: Dict[int, Dict[str, Any]] = {}
    for idx, row in ROSTER_DF.iterrows():
        team_id = str(row.get("Team", "")).upper()
        if not team_id:
            team_id = ""
        player_name = str(row.get("Name", ""))
        pos = str(row.get("POS", ""))
        age = int(row.get("Age", 0)) if not pd.isna(row.get("Age", None)) else 0
        ovr = float(row.get("OVR", 0.0)) if "OVR" in ROSTER_DF.columns else 0.0
        salary = float(row.get("SalaryAmount", 0.0))
        pot_raw = row.get("Potential", None)

        pot_map = {
            "A+": 1.0, "A": 0.95, "A-": 0.9,
            "B+": 0.85, "B": 0.8, "B-": 0.75,
            "C+": 0.7, "C": 0.65, "C-": 0.6,
            "D+": 0.55, "D": 0.5, "F": 0.4
        }
        if isinstance(pot_raw, str):
            potential = pot_map.get(pot_raw.strip(), 0.6)
        else:
            try:
                potential = float(pot_raw)
            except (TypeError, ValueError):
                potential = 0.6

        players[idx] = {
            "player_id": idx,
            "name": player_name,
            "team_id": team_id,
            "pos": pos,
            "age": age,
            "overall": ovr,
            "salary": salary,
            "potential": potential,
            "signed_date": "1900-01-01",
            "signed_via_free_agency": False,
            "acquired_date": "1900-01-01",
            "acquired_via_trade": False,
        }

    GAME_STATE["players"] = players

    # 팀 메타 기본값
    teams_meta: Dict[str, Dict[str, Any]] = {}
    for tid in ALL_TEAM_IDS:
        info = TEAM_TO_CONF_DIV.get(tid, {})
        teams_meta[tid] = {
            "team_id": tid,
            "conference": info.get("conference"),
            "division": info.get("division"),
            "tendency": "neutral",  # contender / neutral / rebuild
            "window": "now",
            "market": "mid",
            "patience": 0.5,
        }
    GAME_STATE["teams"] = teams_meta


def _position_group(pos: str) -> str:
    """POS 문자열을 guard/wing/big 그룹으로 단순 매핑."""
    p = (pos or "").upper()
    if "G" in p:
        return "guard"
    if "C" in p:
        return "big"
    return "wing"


def _compute_team_payroll(team_id: str) -> float:
    """ROSTER_DF 기반으로 팀 페이롤(달러)을 계산."""
    df = ROSTER_DF[ROSTER_DF["Team"] == team_id]
    if df.empty:
        return 0.0
    return float(df["SalaryAmount"].sum())


def _compute_cap_space(team_id: str) -> float:
    payroll = _compute_team_payroll(team_id)
    league = _ensure_league_state()
    trade_rules = league.get("trade_rules", {})
    salary_cap = float(trade_rules.get("salary_cap") or 0.0)
    return salary_cap - payroll


def _compute_team_records() -> Dict[str, Dict[str, Any]]:
    """master_schedule.games를 기준으로 각 팀의 승/패/득실점 계산.

    반환: {team_id: {"wins":..,"losses":..,"pf":..,"pa":..}}
    """
    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
    master_schedule = league["master_schedule"]
    games = master_schedule.get("games") or []

    records: Dict[str, Dict[str, Any]] = {
        tid: {"wins": 0, "losses": 0, "pf": 0, "pa": 0}
        for tid in ALL_TEAM_IDS
    }

    for g in games:
        if g.get("status") != "final":
            continue
        home_id = g.get("home_team_id")
        away_id = g.get("away_team_id")
        home_score = g.get("home_score")
        away_score = g.get("away_score")
        if home_id not in records or away_id not in records:
            continue
        if home_score is None or away_score is None:
            continue

        records[home_id]["pf"] += home_score
        records[home_id]["pa"] += away_score
        records[away_id]["pf"] += away_score
        records[away_id]["pa"] += home_score

        if home_score > away_score:
            records[home_id]["wins"] += 1
            records[away_id]["losses"] += 1
        elif away_score > home_score:
            records[away_id]["wins"] += 1
            records[home_id]["losses"] += 1

    return records


def get_conference_standings() -> Dict[str, List[Dict[str, Any]]]:
    """컨퍼런스별 스탠딩을 계산한다."""
    records = _compute_team_records()

    standings = {"east": [], "west": []}

    for tid, rec in records.items():
        info = TEAM_TO_CONF_DIV.get(tid, {})
        conf = info.get("conference")
        if not conf:
            continue

        wins = rec.get("wins", 0)
        losses = rec.get("losses", 0)
        games_played = wins + losses
        win_pct = wins / games_played if games_played else 0.0
        pf = rec.get("pf", 0)
        pa = rec.get("pa", 0)
        point_diff = pf - pa

        entry = {
            "team_id": tid,
            "conference": conf,
            "division": info.get("division"),
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "games_played": games_played,
            "point_diff": point_diff,
        }

        if conf.lower() == "east":
            standings["east"].append(entry)
        else:
            standings["west"].append(entry)

    def sort_and_gb(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows_sorted = sorted(
            rows,
            key=lambda r: (r.get("win_pct", 0), r.get("point_diff", 0)),
            reverse=True,
        )
        if not rows_sorted:
            return rows_sorted

        leader = rows_sorted[0]
        leader_w, leader_l = leader.get("wins", 0), leader.get("losses", 0)
        for r in rows_sorted:
            gb = ((leader_w - r.get("wins", 0)) + (r.get("losses", 0) - leader_l)) / 2
            r["gb"] = gb
        for idx, r in enumerate(rows_sorted, start=1):
            r["rank"] = idx
        return rows_sorted

    standings["east"] = sort_and_gb(standings["east"])
    standings["west"] = sort_and_gb(standings["west"])

    return standings


def get_team_cards() -> List[Dict[str, Any]]:
    """팀 카드(요약 정보) 리스트를 반환한다."""
    _init_players_and_teams_if_needed()
    records = _compute_team_records()

    team_cards: List[Dict[str, Any]] = []
    for tid in ALL_TEAM_IDS:
        meta = GAME_STATE["teams"].get(tid, {})
        rec = records.get(tid, {})
        wins = rec.get("wins", 0)
        losses = rec.get("losses", 0)
        gp = wins + losses
        win_pct = wins / gp if gp else 0.0
        card = {
            "team_id": tid,
            "conference": meta.get("conference"),
            "division": meta.get("division"),
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "tendency": meta.get("tendency"),
            "payroll": _compute_team_payroll(tid),
            "cap_space": _compute_cap_space(tid),
        }
        team_cards.append(card)

    return team_cards


def get_team_detail(team_id: str) -> Dict[str, Any]:
    """특정 팀의 상세 정보 + 로스터를 반환한다."""
    _init_players_and_teams_if_needed()
    tid = team_id.upper()

    records = _compute_team_records()
    standings = get_conference_standings()
    rank_map = {r["team_id"]: r for r in standings.get("east", []) + standings.get("west", [])}

    meta = GAME_STATE["teams"].get(tid)
    if not meta:
        raise ValueError(f"Team '{tid}' not found")

    rec = records.get(tid, {})
    rank_entry = rank_map.get(tid, {})
    wins = rec.get("wins", 0)
    losses = rec.get("losses", 0)
    gp = wins + losses
    win_pct = wins / gp if gp else 0.0
    pf = rec.get("pf", 0)
    pa = rec.get("pa", 0)
    point_diff = pf - pa

    summary = {
        "team_id": tid,
        "conference": meta.get("conference"),
        "division": meta.get("division"),
        "wins": wins,
        "losses": losses,
        "win_pct": win_pct,
        "point_diff": point_diff,
        "rank": rank_entry.get("rank"),
        "gb": rank_entry.get("gb"),
        "tendency": meta.get("tendency"),
        "payroll": _compute_team_payroll(tid),
        "cap_space": _compute_cap_space(tid),
    }

    roster_rows = ROSTER_DF[ROSTER_DF["Team"] == tid]
    season_stats = GAME_STATE.get("player_stats", {})
    roster: List[Dict[str, Any]] = []
    for pid, row in roster_rows.iterrows():
        p_stats = season_stats.get(pid, {})
        games = p_stats.get("games", 0) or 0
        totals = p_stats.get("totals", {}) or {}
        def per_game_val(key: str) -> float:
            try:
                return float(totals.get(key, 0.0)) / games if games else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                return 0.0

        roster.append(
            {
                "player_id": pid,
                "name": row.get("Name"),
                "pos": row.get("POS"),
                "ovr": float(row.get("OVR", 0.0)) if "OVR" in roster_rows.columns else 0.0,
                "age": int(row.get("Age", 0)) if not pd.isna(row.get("Age", None)) else 0,
                "salary": float(row.get("SalaryAmount", 0.0)),
                "pts": per_game_val("PTS"),
                "ast": per_game_val("AST"),
                "reb": per_game_val("REB"),
                "three_pm": per_game_val("3PM"),
            }
        )

    roster_sorted = sorted(roster, key=lambda r: r.get("ovr", 0), reverse=True)

    return {
        "summary": summary,
        "roster": roster_sorted,
    }


def _evaluate_team_needs(records: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """팀별로 컨텐더/리빌딩/중간, 필요/잉여 포지션을 계산해 team_needs 반환."""
    team_needs: Dict[str, Dict[str, Any]] = {}

    for tid in ALL_TEAM_IDS:
        rec = records.get(tid, {"wins": 0, "losses": 0})
        wins = rec.get("wins", 0)
        losses = rec.get("losses", 0)
        gp = wins + losses
        win_pct = wins / gp if gp > 0 else 0.0

        roster = ROSTER_DF[ROSTER_DF["Team"] == tid]
        if roster.empty:
            team_needs[tid] = {
                "team_id": tid,
                "status": "neutral",
                "win_pct": 0.0,
                "need_positions": [],
                "surplus_positions": [],
            }
            continue

        avg_ovr = float(roster["OVR"].mean()) if "OVR" in roster.columns else 75.0
        avg_age = float(roster["Age"].mean()) if "Age" in roster.columns else 26.0

        # status 결정
        if win_pct >= 0.6 and avg_ovr >= 80:
            status = "contender"
        elif win_pct <= 0.35 and avg_age >= 26:
            status = "rebuild"
        else:
            status = "neutral"

        # 포지션 그룹별 평균 OVR
        roster = roster.copy()
        roster["pos_group"] = roster["POS"].apply(_position_group)
        guard_df = roster[roster["pos_group"] == "guard"]
        wing_df = roster[roster["pos_group"] == "wing"]
        big_df = roster[roster["pos_group"] == "big"]

        def avg_or_default(df_sub) -> float:
            if df_sub.empty:
                return avg_ovr - 5  # 없는 포지션은 약한 걸로
            return float(df_sub["OVR"].mean())

        guard_avg = avg_or_default(guard_df)
        wing_avg = avg_or_default(wing_df)
        big_avg = avg_or_default(big_df)

        need_positions: List[str] = []
        surplus_positions: List[str] = []

        # 팀 평균 대비 3 이상 떨어지면 부족, 2 이상 높으면 잉여
        for g_name, g_avg in [("guard", guard_avg), ("wing", wing_avg), ("big", big_avg)]:
            if g_avg <= avg_ovr - 3:
                need_positions.append(g_name)
            elif g_avg >= avg_ovr + 2:
                surplus_positions.append(g_name)

        team_needs[tid] = {
            "team_id": tid,
            "status": status,
            "win_pct": win_pct,
            "need_positions": need_positions,
            "surplus_positions": surplus_positions,
        }

        # GAME_STATE["teams"]에 성향을 약간 반영
        team_meta = GAME_STATE["teams"].get(tid, {})
        team_meta["tendency"] = status
        GAME_STATE["teams"][tid] = team_meta

    return team_needs


def _player_value_for_team(player_row: pd.Series, team_status: str) -> float:
    """간단한 선수 가치 함수.

    team_status에 따라 잠재력/현재능력/나이/연봉 비중을 조정.
    """
    ovr = float(player_row.get("OVR", 0.0))
    age = int(player_row.get("Age", 0)) if not pd.isna(player_row.get("Age", None)) else 0
    salary = float(player_row.get("SalaryAmount", 0.0))
    pot_raw = player_row.get("Potential", None)

    pot_map = {
        "A+": 1.0, "A": 0.95, "A-": 0.9,
        "B+": 0.85, "B": 0.8, "B-": 0.75,
        "C+": 0.7, "C": 0.65, "C-": 0.6,
        "D+": 0.55, "D": 0.5, "F": 0.4
    }
    if isinstance(pot_raw, str):
        potential = pot_map.get(pot_raw.strip(), 0.6)
    else:
        try:
            potential = float(pot_raw)
        except (TypeError, ValueError):
            potential = 0.6

    # 기본: 현재 능력 위주
    value = ovr

    # 컨텐더: 지금 능력 > 잠재력
    if team_status == "contender":
        value += potential * 5.0
        value -= max(0, age - 28) * 0.7
    # 리빌딩: 잠재력/나이 위주
    elif team_status == "rebuild":
        value += potential * 8.0
        value -= max(0, age - 24) * 0.9
    else:
        value += potential * 6.0
        value -= max(0, age - 26) * 0.8

    # 연봉 패널티 (10M당 -1 정도)
    value -= (salary / 10_000_000.0)

    return float(value)


def get_team_status_map() -> Dict[str, str]:
    """팀별 상태(contender/rebuild/neutral)를 반환."""
    records = _compute_team_records()
    team_needs = _evaluate_team_needs(records)
    return {tid: info.get("status", "neutral") for tid, info in team_needs.items()}


def estimate_player_value(player_id: int, team_id: str) -> float:
    """team_id 기준으로 player_id의 대략적인 가치를 계산."""
    team_status_map = get_team_status_map()
    status = team_status_map.get(team_id, "neutral")
    if player_id not in ROSTER_DF.index:
        return 0.0
    row = ROSTER_DF.loc[player_id]
    return _player_value_for_team(row, status)
