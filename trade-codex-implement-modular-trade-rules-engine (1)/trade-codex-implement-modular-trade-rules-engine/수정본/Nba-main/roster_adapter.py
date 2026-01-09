
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

# You can place match_engine.py next to this file.
from match_engine import Player

from derived_formulas import compute_derived


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

