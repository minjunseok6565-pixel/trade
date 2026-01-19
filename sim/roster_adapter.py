from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional

from derived_formulas import compute_derived
from league_repo import LeagueRepo
from matchengine_v3.models import Player, TeamState
from matchengine_v3.tactics import TacticsConfig


def _build_tactics_config(raw: Optional[Dict[str, Any]]) -> TacticsConfig:
    if not raw:
        return TacticsConfig()

    cfg = TacticsConfig(
        offense_scheme=str(raw.get("offense_scheme") or "Spread_HeavyPnR"),
        defense_scheme=str(raw.get("defense_scheme") or "Drop"),
    )

    if "scheme_weight_sharpness" in raw:
        cfg.scheme_weight_sharpness = float(raw["scheme_weight_sharpness"])
    if "scheme_outcome_strength" in raw:
        cfg.scheme_outcome_strength = float(raw["scheme_outcome_strength"])
    if "def_scheme_weight_sharpness" in raw:
        cfg.def_scheme_weight_sharpness = float(raw["def_scheme_weight_sharpness"])
    if "def_scheme_outcome_strength" in raw:
        cfg.def_scheme_outcome_strength = float(raw["def_scheme_outcome_strength"])

    cfg.action_weight_mult = dict(raw.get("action_weight_mult") or {})
    cfg.outcome_global_mult = dict(raw.get("outcome_global_mult") or {})
    cfg.outcome_by_action_mult = dict(raw.get("outcome_by_action_mult") or {})
    cfg.def_action_weight_mult = dict(raw.get("def_action_weight_mult") or {})
    cfg.opp_action_weight_mult = dict(raw.get("opp_action_weight_mult") or {})
    cfg.opp_outcome_global_mult = dict(raw.get("opp_outcome_global_mult") or {})
    cfg.opp_outcome_by_action_mult = dict(raw.get("opp_outcome_by_action_mult") or {})

    pace = raw.get("pace")
    if pace is not None:
        cfg.context["PACE"] = pace

    return cfg


def _build_roles_from_lineup(lineup: List[Player]) -> Dict[str, str]:
    def score(player: Player, keys: List[str]) -> float:
        return sum(float(player.derived.get(k, 50.0)) for k in keys)

    ranked = sorted(
        lineup,
        key=lambda p: score(p, ["DRIVE_CREATE", "HANDLE_SAFE", "PASS_CREATE", "PASS_SAFE", "PNR_READ"]),
        reverse=True,
    )
    ball_handler = ranked[0].pid if ranked else lineup[0].pid
    secondary_handler = ranked[1].pid if len(ranked) > 1 else ball_handler

    screener = max(
        lineup,
        key=lambda p: score(p, ["PHYSICAL", "SEAL_POWER", "SHORTROLL_PLAY"]),
        default=lineup[0],
    ).pid
    rim_runner = max(
        lineup,
        key=lambda p: score(p, ["FIN_DUNK", "FIN_RIM", "FIN_CONTACT"]),
        default=lineup[0],
    ).pid
    post = max(
        lineup,
        key=lambda p: score(p, ["POST_SCORE", "POST_CONTROL", "SEAL_POWER"]),
        default=lineup[0],
    ).pid
    cutter = max(
        lineup,
        key=lambda p: score(p, ["FIN_RIM", "FIN_DUNK", "FIRST_STEP"]),
        default=lineup[0],
    ).pid
    shooter = max(
        lineup,
        key=lambda p: score(p, ["SHOT_3_CS", "SHOT_MID_CS", "SHOT_3_OD"]),
        default=lineup[0],
    ).pid

    return {
        "ball_handler": ball_handler,
        "secondary_handler": secondary_handler,
        "screener": screener,
        "rim_runner": rim_runner,
        "post": post,
        "cutter": cutter,
        "shooter": shooter,
    }


def _select_lineup(
    roster: List[Player],
    starters: Optional[List[str]],
    bench: Optional[List[str]],
    max_players: int,
) -> List[Player]:
    roster_by_pid = {p.pid: p for p in roster}
    chosen: List[Player] = []
    chosen_ids = set()

    for pid in starters or []:
        player = roster_by_pid.get(str(pid))
        if player and player.pid not in chosen_ids:
            chosen.append(player)
            chosen_ids.add(player.pid)

    for pid in bench or []:
        player = roster_by_pid.get(str(pid))
        if player and player.pid not in chosen_ids:
            chosen.append(player)
            chosen_ids.add(player.pid)

    for player in roster:
        if len(chosen) >= max_players:
            break
        if player.pid not in chosen_ids:
            chosen.append(player)
            chosen_ids.add(player.pid)

    if len(chosen) < 5:
        raise ValueError(f"team has fewer than 5 players (got {len(chosen)})")

    return chosen[:max_players]


def load_team_players_from_db(repo: LeagueRepo, team_id: str) -> List[Player]:
    roster_rows = repo.get_team_roster(team_id)
    if not roster_rows:
        raise ValueError(f"Team '{team_id}' not found in roster DB")

    players: List[Player] = []
    for row in roster_rows:
        attrs = row.get("attrs") or {}
        derived = compute_derived(attrs)
        players.append(
            Player(
                pid=str(row.get("player_id")),
                name=str(row.get("name") or attrs.get("Name") or ""),
                pos=str(row.get("pos") or attrs.get("POS") or attrs.get("Position") or "G"),
                derived=derived,
            )
        )
    return players


def build_team_state_from_db(
    *,
    repo: LeagueRepo,
    team_id: str,
    tactics: Optional[Dict[str, Any]] = None,
) -> TeamState:
    lineup_info = tactics.get("lineup", {}) if tactics else {}
    starters = lineup_info.get("starters") or []
    bench = lineup_info.get("bench") or []
    max_players = int((tactics or {}).get("rotation_size") or 10)

    players = load_team_players_from_db(repo, team_id)
    max_players = max(5, min(max_players, len(players)))
    lineup = _select_lineup(players, starters, bench, max_players=max_players)
    roles = _build_roles_from_lineup(lineup[:5])
    tactics_cfg = _build_tactics_config(tactics)

    team_state = TeamState(name=str(team_id).upper(), lineup=lineup, tactics=tactics_cfg, roles=roles)
    minutes = (tactics or {}).get("minutes") or {}
    if isinstance(minutes, dict) and minutes:
        team_state = replace(
            team_state,
            rotation_target_sec_by_pid={
                str(pid): int(float(mins) * 60)
                for pid, mins in minutes.items()
                if pid is not None and mins is not None
            },
        )

    return team_state
