
"""
roster_adapter.py

Reads "완성 로스터.xlsx" (base attributes) and converts into match_engine Player objects
by computing derived abilities using your fixed mapping table.

Usage:
  python roster_adapter.py --team "LAL" --out roster_team.json
or import:
  from roster_adapter import load_team_players
"""

from __future__ import annotations
from dataclasses import asdict
from typing import Dict, List, Any, Optional
import argparse, json
import pandas as pd
import numpy as np

# You can place match_engine.py next to this file.
from match_engine import Player


COL = {
    "CloseShot":"Close Shot",
    "MidRange":"Mid-Range Shot",
    "ThreePoint":"Three-Point Shot",
    "FreeThrow":"Free Throw",
    "ShotIQ":"Shot IQ",
    "OffCons":"Offensive Consistency",
    "Layup":"Layup",
    "StandingDunk":"Standing Dunk",
    "DrivingDunk":"Driving Dunk",
    "PostHook":"Post Hook",
    "PostFade":"Post Fade",
    "PostControl":"Post Control",
    "DrawFoul":"Draw Foul",
    "Hands":"Hands",
    "PassAccuracy":"Pass Accuracy",
    "BallHandle":"Ball Handle",
    "SpeedWithBall":"Speed with Ball",
    "PassIQ":"Pass IQ",
    "PassVision":"Pass Vision",
    "InteriorDef":"Interior Defense",
    "PerimeterDef":"Perimeter Defense",
    "Steal":"Steal",
    "Block":"Block",
    "HelpIQ":"Help Defense IQ",
    "PassPerception":"Pass Perception",
    "DefCons":"Defensive Consistency",
    "OffReb":"Offensive Rebound",
    "DefReb":"Defensive Rebound",
    "Speed":"Speed",
    "Agility":"Agility",
    "Strength":"Strength",
    "Vertical":"Vertical",
    "Stamina":"Stamina",
    "Hustle":"Hustle",
    "Durability":"Overall Durability",
}

def _get(row, key: str, default: float = 50.0) -> float:
    c = COL.get(key)
    if c and c in row and pd.notna(row[c]):
        try:
            return float(row[c])
        except Exception:
            return default
    return default

def _clamp100(x: float) -> float:
    return float(np.clip(x, 0, 100))

def compute_derived(row) -> Dict[str, float]:
    FIN_RIM = 0.35*_get(row,"Layup")+0.20*_get(row,"CloseShot")+0.10*_get(row,"Hands")+0.10*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Strength")+0.05*_get(row,"Vertical")
    FIN_DUNK = 0.30*_get(row,"DrivingDunk")+0.25*_get(row,"StandingDunk")+0.15*_get(row,"Vertical")+0.15*_get(row,"Strength")+0.10*_get(row,"Hands")+0.05*_get(row,"OffCons")
    FIN_CONTACT = 0.30*_get(row,"DrawFoul")+0.20*_get(row,"Strength")+0.15*_get(row,"Vertical")+0.15*_get(row,"Hands")+0.10*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")
    SHOT_MID_CS = 0.65*_get(row,"MidRange")+0.20*_get(row,"ShotIQ")+0.15*_get(row,"OffCons")
    SHOT_3_CS = 0.70*_get(row,"ThreePoint")+0.15*_get(row,"ShotIQ")+0.15*_get(row,"OffCons")
    SHOT_FT = 0.85*_get(row,"FreeThrow")+0.15*_get(row,"ShotIQ")
    SHOT_MID_PU = 0.45*_get(row,"MidRange")+0.20*_get(row,"BallHandle")+0.15*_get(row,"SpeedWithBall")+0.10*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")
    SHOT_3_OD = 0.50*_get(row,"ThreePoint")+0.20*_get(row,"BallHandle")+0.15*_get(row,"SpeedWithBall")+0.10*_get(row,"ShotIQ")+0.05*_get(row,"OffCons")
    SHOT_TOUCH = 0.30*_get(row,"CloseShot")+0.20*_get(row,"ShotIQ")+0.20*_get(row,"Hands")+0.15*_get(row,"OffCons")+0.15*_get(row,"Layup")

    POST_SCORE = 0.25*_get(row,"PostHook")+0.25*_get(row,"PostFade")+0.20*_get(row,"PostControl")+0.10*_get(row,"CloseShot")+0.10*_get(row,"Strength")+0.10*_get(row,"Hands")
    POST_CONTROL = 0.40*_get(row,"PostControl")+0.20*_get(row,"Strength")+0.15*_get(row,"Hands")+0.15*_get(row,"OffCons")+0.10*_get(row,"ShotIQ")
    SEAL_POWER = 0.40*_get(row,"Strength")+0.20*_get(row,"PostControl")+0.15*_get(row,"CloseShot")+0.15*_get(row,"Hustle")+0.10*_get(row,"Hands")

    DRIVE_CREATE = 0.30*_get(row,"SpeedWithBall")+0.25*_get(row,"BallHandle")+0.15*_get(row,"Agility")+0.10*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Strength")
    HANDLE_SAFE = 0.45*_get(row,"BallHandle")+0.20*_get(row,"Hands")+0.15*_get(row,"Strength")+0.10*_get(row,"OffCons")+0.10*_get(row,"PassIQ")
    FIRST_STEP = 0.35*_get(row,"Speed")+0.25*_get(row,"Agility")+0.20*_get(row,"SpeedWithBall")+0.10*_get(row,"BallHandle")+0.10*_get(row,"Stamina")

    PASS_SAFE = 0.35*_get(row,"PassAccuracy")+0.25*_get(row,"PassIQ")+0.20*_get(row,"Hands")+0.20*_get(row,"PassVision")
    PASS_CREATE = 0.30*_get(row,"PassVision")+0.25*_get(row,"PassAccuracy")+0.25*_get(row,"PassIQ")+0.10*_get(row,"BallHandle")+0.10*_get(row,"ShotIQ")
    PNR_READ = 0.35*_get(row,"PassIQ")+0.25*_get(row,"ShotIQ")+0.20*_get(row,"PassVision")+0.10*_get(row,"BallHandle")+0.10*_get(row,"OffCons")
    SHORTROLL_PLAY = 0.35*_get(row,"PassIQ")+0.25*_get(row,"PassVision")+0.20*_get(row,"Hands")+0.10*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")

    DEF_POA = 0.35*_get(row,"PerimeterDef")+0.25*_get(row,"Agility")+0.15*_get(row,"Speed")+0.15*_get(row,"DefCons")+0.10*_get(row,"Strength")
    DEF_HELP = 0.35*_get(row,"HelpIQ")+0.20*_get(row,"PassPerception")+0.15*_get(row,"DefCons")+0.15*_get(row,"InteriorDef")+0.15*_get(row,"Hustle")
    DEF_STEAL = 0.40*_get(row,"Steal")+0.25*_get(row,"PassPerception")+0.15*_get(row,"Agility")+0.10*_get(row,"Hustle")+0.10*_get(row,"DefCons")
    DEF_RIM = 0.30*_get(row,"InteriorDef")+0.25*_get(row,"Block")+0.20*_get(row,"Vertical")+0.15*_get(row,"Strength")+0.10*_get(row,"HelpIQ")
    DEF_POST = 0.35*_get(row,"InteriorDef")+0.25*_get(row,"Strength")+0.15*_get(row,"DefCons")+0.15*_get(row,"HelpIQ")+0.10*_get(row,"Vertical")

    REB_OR = 0.45*_get(row,"OffReb")+0.20*_get(row,"Strength")+0.15*_get(row,"Vertical")+0.10*_get(row,"Hustle")+0.10*_get(row,"Stamina")
    REB_DR = 0.45*_get(row,"DefReb")+0.20*_get(row,"Strength")+0.15*_get(row,"Vertical")+0.10*_get(row,"Hustle")+0.10*_get(row,"Stamina")
    PHYSICAL = 0.45*_get(row,"Strength")+0.20*_get(row,"Durability")+0.20*_get(row,"Hustle")+0.15*_get(row,"Stamina")
    ENDURANCE = 0.55*_get(row,"Stamina")+0.25*_get(row,"Durability")+0.20*_get(row,"Hustle")

    out = dict(
        FIN_RIM=FIN_RIM, FIN_DUNK=FIN_DUNK, FIN_CONTACT=FIN_CONTACT,
        SHOT_MID_CS=SHOT_MID_CS, SHOT_3_CS=SHOT_3_CS, SHOT_FT=SHOT_FT,
        SHOT_MID_PU=SHOT_MID_PU, SHOT_3_OD=SHOT_3_OD, SHOT_TOUCH=SHOT_TOUCH,
        POST_SCORE=POST_SCORE, POST_CONTROL=POST_CONTROL, SEAL_POWER=SEAL_POWER,
        DRIVE_CREATE=DRIVE_CREATE, HANDLE_SAFE=HANDLE_SAFE, FIRST_STEP=FIRST_STEP,
        PASS_SAFE=PASS_SAFE, PASS_CREATE=PASS_CREATE, PNR_READ=PNR_READ, SHORTROLL_PLAY=SHORTROLL_PLAY,
        DEF_POA=DEF_POA, DEF_HELP=DEF_HELP, DEF_STEAL=DEF_STEAL, DEF_RIM=DEF_RIM, DEF_POST=DEF_POST,
        REB_OR=REB_OR, REB_DR=REB_DR, PHYSICAL=PHYSICAL, ENDURANCE=ENDURANCE,
    )
    return {k: _clamp100(v) for k, v in out.items()}

def load_team_players(xlsx_path: str, team: str) -> List[Player]:
    df = pd.read_excel(xlsx_path)
    df_team = df[df["Team"] == team].copy()
    players: List[Player] = []
    for i, row in df_team.iterrows():
        pid = f"{team}_{i}"
        derived = compute_derived(row)
        players.append(Player(pid=pid, name=str(row["Name"]), pos=str(row.get("POS","")), derived=derived))
    return players



# -------------------------
# Helpers for match_engine integration
# -------------------------

def select_players(players: List[Player], selectors: Optional[List[str]]) -> List[Player]:
    """Select players by pid or name (case-insensitive). If selectors is None/empty, return players as-is."""
    if not selectors:
        return list(players)
    sel_norm = [s.strip().lower() for s in selectors if s and s.strip()]
    out: List[Player] = []
    used = set()
    # 1) exact pid match
    by_pid = {p.pid.lower(): p for p in players}
    by_name = {}
    for p in players:
        by_name.setdefault(p.name.strip().lower(), []).append(p)

    for s in sel_norm:
        p = by_pid.get(s)
        if p and p.pid not in used:
            out.append(p); used.add(p.pid); continue
        # exact name
        cand = by_name.get(s) or []
        for p2 in cand:
            if p2.pid not in used:
                out.append(p2); used.add(p2.pid); break

    return out

def autofill_roster(all_players: List[Player], chosen: List[Player], min_players: int = 5, max_players: int = 10) -> List[Player]:
    """Ensure at least `min_players` players by filling from all_players order, and cap at `max_players`."""
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
    """Backward-compatible wrapper: ensure exactly 5 players."""
    return autofill_roster(all_players, chosen, min_players=5, max_players=5)

def build_roles_from_derived(lineup: List[Player]) -> Dict[str, str]:
    """Best-effort role assignment based on derived keys used by the engine."""
    def score(p: Player, keys: List[str]) -> float:
        return sum(float(p.derived.get(k, 50.0)) for k in keys)

    # ball handlers
    ranked = sorted(lineup, key=lambda p: score(p, ["DRIVE_CREATE","HANDLE_SAFE","PASS_CREATE","PASS_SAFE","PNR_READ"]), reverse=True)
    bh = ranked[0].pid if ranked else lineup[0].pid
    sh = ranked[1].pid if len(ranked) > 1 else bh

    # screener / rim runner / post
    screener = max(lineup, key=lambda p: score(p, ["PHYSICAL","SEAL_POWER","SHORTROLL_PLAY"]), default=lineup[0]).pid
    rim_runner = max(lineup, key=lambda p: score(p, ["FIN_DUNK","FIN_RIM","FIN_CONTACT"]), default=lineup[0]).pid
    post = max(lineup, key=lambda p: score(p, ["POST_SCORE","POST_CONTROL","SEAL_POWER"]), default=lineup[0]).pid

    # cutter: prefer rim finishing
    cutter = max(lineup, key=lambda p: score(p, ["FIN_RIM","FIN_DUNK","FIRST_STEP"]), default=lineup[0]).pid

    return {
        "ball_handler": bh,
        "secondary_handler": sh,
        "screener": screener,
        "rim_runner": rim_runner,
        "post": post,
        "cutter": cutter,
    }

def build_team_state_from_excel(
    xlsx_path: str,
    team: str,
    lineup_selectors: Optional[List[str]] = None,
    offense_scheme: str = "Spread_HeavyPnR",
    defense_scheme: str = "Drop",
    max_roster_players: int = 10,
):
    """Convenience: Excel -> List[Player] -> TeamState (up to 10-man roster + auto roles + tactics)."""
    from match_engine.models import TeamState
    from match_engine.tactics import TacticsConfig

    all_players = load_team_players(xlsx_path, team)
    chosen = select_players(all_players, lineup_selectors)
    lineup = autofill_roster(all_players, chosen, min_players=5, max_players=max_roster_players)
    # Roles are primarily used while the engine is operating on the on-court 5;
    # assign from starters for stability.
    roles = build_roles_from_derived(lineup[:5])
    tactics = TacticsConfig(offense_scheme=offense_scheme, defense_scheme=defense_scheme)
    return TeamState(name=team, lineup=lineup, tactics=tactics, roles=roles)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default="완성 로스터.xlsx")
    ap.add_argument("--team", required=True)
    ap.add_argument("--out", default=None, help="write derived roster json for that team")
    args = ap.parse_args()

    players = load_team_players(args.xlsx, args.team)
    payload = [{"pid": p.pid, "name": p.name, "pos": p.pos, "derived": p.derived} for p in players]

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    else:
        print(json.dumps(payload[:3], ensure_ascii=False, indent=2))
        print(f"... total players: {len(payload)}")

if __name__ == "__main__":
    main()

