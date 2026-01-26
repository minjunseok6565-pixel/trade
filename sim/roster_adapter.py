from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from functools import lru_cache
from typing import Any, Dict, List, Optional

from derived_formulas import compute_derived
from league_repo import LeagueRepo
from matchengine_v3.models import Player, TeamState
from matchengine_v3.tactics import TacticsConfig


logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}


def _warn_limited(code: str, msg: str, *, limit: int = 3) -> None:
    """Log warning with traceback, but cap repeats per code."""
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1

_SIM_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SIM_DIR)


def _find_json_path(filename: str) -> Optional[str]:
    """Find a config json file in common locations.

    Search order (first hit wins):
      1) project root: <project>/<filename>
      2) project data dir: <project>/data/<filename>
      3) project config dir: <project>/config/<filename>
      4) sim dir: <project>/sim/<filename>
    """
    candidates = [
        os.path.join(_PROJECT_DIR, filename),
        os.path.join(_PROJECT_DIR, "data", filename),
        os.path.join(_PROJECT_DIR, "config", filename),
        os.path.join(_SIM_DIR, filename),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=1)
def _load_team_coach_preset_map() -> Dict[str, str]:
    """Load team->preset mapping from team_coach_presets.json (optional).

    Expected format:
      {
        "version": "1.0",
        "teams": { "LAL": "Playoff Tight", ... }
      }
    Also accepts a plain dict {"LAL": "..."} for flexibility.
    Missing file or parse errors -> empty dict (safe no-op).
    """
    path = _find_json_path("team_coach_presets.json")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("teams"), dict):
            return {str(k).upper(): str(v) for k, v in data["teams"].items()}
        if isinstance(data, dict):
            # allow flat map
            return {str(k).upper(): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}
        return {}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        _warn_limited("ROSTER_PRESET_LOAD_FAILED", f"path={path!r}")
        return {}


@lru_cache(maxsize=1)
def _load_coach_presets_raw() -> Dict[str, Dict[str, Any]]:
    """Load coach preset definitions from coach_presets.json (optional).

    Expected format:
      {
        "version": "1.0",
        "presets": {
          "Balanced": { ... },
          "Playoff Tight": { ... }
        }
      }

    Also accepts a flat map {"Balanced": {...}, ...} for flexibility.
    Missing file or parse errors -> empty dict (safe no-op).
    """
    path = _find_json_path("coach_presets.json")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and isinstance(data.get("presets"), dict):
            presets = data["presets"]
        elif isinstance(data, dict):
            presets = data
        else:
            return {}

        out: Dict[str, Dict[str, Any]] = {}
        for k, v in presets.items():
            if isinstance(k, str) and isinstance(v, dict):
                out[k] = v
        return out
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        _warn_limited("ROSTER_PRESET_LOAD_FAILED", f"path={path!r}")
        return {}


def _apply_coach_preset_tactics(
    team_id: str,
    cfg: TacticsConfig,
    raw_tactics: Optional[Dict[str, Any]],
) -> None:
    """Apply tactics values from coach_presets.json based on cfg.context['COACH_PRESET'].

    Rules:
      - If USER_COACH is enabled in context, do nothing (user controls tactics).
      - Never override values explicitly provided by the caller in raw_tactics.
      - Supports preset fields either at top-level or under a nested "tactics" dict.
      - (A안) Reads scheme_weight_sharpness + scheme_outcome_strength.
    """
    if not isinstance(getattr(cfg, "context", None), dict):
        return
    if cfg.context.get("USER_COACH"):
        return

    preset_name = cfg.context.get("COACH_PRESET")
    if not preset_name:
        return

    presets = _load_coach_presets_raw()
    if not presets:
        return

    key = str(preset_name).strip()
    preset = presets.get(key)
    if preset is None:
        # case-insensitive fallback
        lower_map = {k.lower(): k for k in presets.keys() if isinstance(k, str)}
        canon = lower_map.get(key.lower())
        preset = presets.get(canon) if canon else None
    if not isinstance(preset, dict):
        return

    src = preset.get("tactics") if isinstance(preset.get("tactics"), dict) else preset

    raw = raw_tactics or {}

    # Do not override explicit caller inputs.
    if "offense_scheme" not in raw and "offense_scheme" in src:
        cfg.offense_scheme = str(src.get("offense_scheme") or cfg.offense_scheme)
    if "defense_scheme" not in raw and "defense_scheme" in src:
        cfg.defense_scheme = str(src.get("defense_scheme") or cfg.defense_scheme)

    # Strength knobs: treat offense+defense as a pair.
    caller_set_sharp = ("scheme_weight_sharpness" in raw) or ("def_scheme_weight_sharpness" in raw)
    if not caller_set_sharp:
        if "scheme_weight_sharpness" in src:
            v = float(src["scheme_weight_sharpness"])
            cfg.scheme_weight_sharpness = v
            # If preset doesn't specify defense separately, mirror offense value.
            if "def_scheme_weight_sharpness" not in src:
                cfg.def_scheme_weight_sharpness = v
        if "def_scheme_weight_sharpness" in src:
            cfg.def_scheme_weight_sharpness = float(src["def_scheme_weight_sharpness"])

    caller_set_outcome = ("scheme_outcome_strength" in raw) or ("def_scheme_outcome_strength" in raw)
    if not caller_set_outcome:
        if "scheme_outcome_strength" in src:
            v = float(src["scheme_outcome_strength"])
            cfg.scheme_outcome_strength = v
            if "def_scheme_outcome_strength" not in src:
                cfg.def_scheme_outcome_strength = v
        if "def_scheme_outcome_strength" in src:
            cfg.def_scheme_outcome_strength = float(src["def_scheme_outcome_strength"])


def _apply_default_coach_preset(team_id: str, cfg: TacticsConfig) -> None:
    """Inject COACH_PRESET into tactics.context if not explicitly provided."""
    if not isinstance(getattr(cfg, "context", None), dict):
        return
    # Respect explicit preset from caller/tactics input.
    if "COACH_PRESET" in cfg.context:
        return

    mapping = _load_team_coach_preset_map()
    preset = mapping.get(str(team_id).upper())
    if preset:
        cfg.context["COACH_PRESET"] = str(preset)


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

    # Allow caller to pass arbitrary context (e.g., USER_COACH, ROTATION_POOL_PIDS, etc.)
    raw_ctx = raw.get("context")
    if isinstance(raw_ctx, dict) and raw_ctx:
        cfg.context.update(raw_ctx)
        
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
    _apply_default_coach_preset(team_id, tactics_cfg)
    _apply_coach_preset_tactics(team_id, tactics_cfg, tactics)

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
