# roster_adapter.py (DB-backed)
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Optional
import argparse, json

from league_repo import LeagueRepo
from schema import normalize_team_id, normalize_player_id

from derived_formulas import compute_derived

# 엔진 Player가 있으면 사용(네 새 엔진이 match_engine.Player 를 제공한다는 가정)
try:
    from match_engine import Player  # type: ignore
except Exception:
    # 엔진이 아직 연결 안 된 상태에서도 roster_adapter 자체는 동작하도록 fallback
    @dataclass
    class Player:  # type: ignore
        pid: str
        name: str
        pos: str
        derived: Dict[str, Any]


def load_team_players(db_path: str, team_id: str) -> List[Player]:
    """
    DB -> List[Player]
    - 엑셀 직접 읽지 않음
    - pid 생성 금지: player_id를 그대로 pid로 사용
    """
    tid = str(normalize_team_id(team_id, strict=True))

    with LeagueRepo(db_path) as repo:
        rows = repo.get_team_roster(tid)

    players: List[Player] = []
    for r in rows:
        # 1) pid는 DB의 player_id 그대로
        pid = str(normalize_player_id(r["player_id"], strict=False))
        name = str(r.get("name") or "")
        pos = str(r.get("pos") or "")

        # 2) compute_derived가 예전 엑셀 컬럼명을 기대할 수 있으니,
        #    canonical + legacy 키를 같이 제공(안전장치)
        attrs = r.get("attrs") or {}
        row_for_derived: Dict[str, Any] = dict(attrs)
        row_for_derived.update(
            {
                "player_id": pid,
                "team_id": tid,
                # legacy 호환(derived_formulas가 예전 키를 보면 여기서 받음)
                "Team": tid,
                "Name": name,
                "POS": pos,
                "Age": r.get("age"),
                "HT": r.get("height_in"),     # 필요시 변환해서 문자열로 바꿔도 됨
                "WT": r.get("weight_lb"),
                "Salary": r.get("salary_amount"),
                "OVR": r.get("ovr"),
            }
        )

        derived = compute_derived(row_for_derived)
        players.append(Player(pid=pid, name=name, pos=pos, derived=derived))

    return players


# -------------------------
# Helpers for engine integration (기존 로직 유지)
# -------------------------

def select_players(players: List[Player], selectors: Optional[List[str]]) -> List[Player]:
    """Select players by pid(player_id) or name (case-insensitive)."""
    if not selectors:
        return list(players)
    sel_norm = [s.strip().lower() for s in selectors if s and s.strip()]
    out: List[Player] = []
    used = set()
    by_pid = {p.pid.lower(): p for p in players}
    by_name: Dict[str, List[Player]] = {}
    for p in players:
        by_name.setdefault(p.name.strip().lower(), []).append(p)

    for s in sel_norm:
        p = by_pid.get(s)
        if p and p.pid not in used:
            out.append(p); used.add(p.pid); continue
        cand = by_name.get(s) or []
        for p2 in cand:
            if p2.pid not in used:
                out.append(p2); used.add(p2.pid); break

    return out


def autofill_roster(all_players: List[Player], chosen: List[Player], min_players: int = 5, max_players: int = 10) -> List[Player]:
    if max_players < min_players:
        max_players = min_players
    out = list(chosen)
    used = {p.pid for p in out}
    for p in all_players:
        if len(out) >= max_players:
            break
        if p.pid not in used:
            out.append(p)
            used.add(p.pid)
    if len(out) < min_players:
        raise ValueError(f"team has fewer than {min_players} players (got {len(out)})")
    return out[:max_players]


def autofill_to_five(all_players: List[Player], chosen: List[Player]) -> List[Player]:
    return autofill_roster(all_players, chosen, min_players=5, max_players=5)


def build_roles_from_derived(lineup: List[Player]) -> Dict[str, str]:
    def score(p: Player, keys: List[str]) -> float:
        return sum(float(p.derived.get(k, 50.0)) for k in keys)

    ranked = sorted(lineup, key=lambda p: score(p, ["DRIVE_CREATE","HANDLE_SAFE","PASS_CREATE","PASS_SAFE","PNR_READ"]), reverse=True)
    bh = ranked[0].pid if ranked else lineup[0].pid
    sh = ranked[1].pid if len(ranked) > 1 else bh

    screener = max(lineup, key=lambda p: score(p, ["PHYSICAL","SEAL_POWER","SHORTROLL_PLAY"]), default=lineup[0]).pid
    rim_runner = max(lineup, key=lambda p: score(p, ["FIN_DUNK","FIN_RIM","FIN_CONTACT"]), default=lineup[0]).pid
    post = max(lineup, key=lambda p: score(p, ["POST_SCORE","POST_CONTROL","SEAL_POWER"]), default=lineup[0]).pid
    cutter = max(lineup, key=lambda p: score(p, ["FIN_RIM","FIN_DUNK","FIRST_STEP"]), default=lineup[0]).pid

    return {
        "ball_handler": bh,
        "secondary_handler": sh,
        "screener": screener,
        "rim_runner": rim_runner,
        "post": post,
        "cutter": cutter,
    }


def build_team_state_from_db(
    db_path: str,
    team_id: str,
    lineup_selectors: Optional[List[str]] = None,
    offense_scheme: str = "Spread_HeavyPnR",
    defense_scheme: str = "Drop",
    max_roster_players: int = 10,
):
    """
    (선택) 엔진이 TeamState를 사용한다면 DB 기반으로 교체.
    기존 build_team_state_from_excel(...)를 DB 버전으로 바꾼 것.
    """
    from match_engine.models import TeamState  # type: ignore
    from match_engine.tactics import TacticsConfig  # type: ignore

    all_players = load_team_players(db_path, team_id)
    chosen = select_players(all_players, lineup_selectors)
    lineup = autofill_roster(all_players, chosen, min_players=5, max_players=max_roster_players)
    roles = build_roles_from_derived(lineup[:5])
    tactics = TacticsConfig(offense_scheme=offense_scheme, defense_scheme=defense_scheme)
    return TeamState(name=str(normalize_team_id(team_id)), lineup=lineup, tactics=tactics, roles=roles)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="league.db")
    ap.add_argument("--team_id", required=True)
    ap.add_argument("--out", default=None, help="write derived roster json for that team")
    args = ap.parse_args()

    players = load_team_players(args.db, args.team_id)
    payload = [{"pid": p.pid, "name": p.name, "pos": p.pos, "derived": p.derived} for p in players]

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    else:
        print(json.dumps(payload[:3], ensure_ascii=False, indent=2))
        print(f"... total players: {len(payload)}")


if __name__ == "__main__":
    main()
