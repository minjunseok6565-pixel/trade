
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple, Literal

import logging

import schema
from schema import SCHEMA_VERSION, normalize_player_id, normalize_team_id


Phase = Literal["regular", "play_in", "playoffs", "preseason"]


_ALLOWED_PHASES: Tuple[str, ...] = ("regular", "play_in", "playoffs", "preseason")


logger = logging.getLogger(__name__)


# --- utilities -----------------------------------------------------------

def _normalize_team_id_strict(value: Any, *, path: str) -> str:
    try:
        return str(normalize_team_id(value, allow_fa=False, strict=True))
    except Exception as e:
        raise ValueError(f"context invalid: '{path}' invalid team_id={value!r} ({e})")


def _normalize_player_id_strict(value: Any, *, path: str) -> str:
    try:
        return str(normalize_player_id(value, strict=True))
    except Exception as e:
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' invalid player_id={value!r} ({e})")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _require_dict(v: Any, path: str) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' must be a dict")
    return v


def _require_str(v: Any, path: str) -> str:
    if v is None:
        raise ValueError(f"context invalid: missing '{path}'")
    s = str(v)
    if not s:
        raise ValueError(f"context invalid: '{path}' must be a non-empty string")
    return s


def build_context_from_master_schedule_entry(
    entry: Mapping[str, Any],
    league_state: Mapping[str, Any],
    *,
    phase: Phase = "regular",
) -> schema.GameContext:
    game_id = _require_str(entry.get("game_id"), "entry.game_id")
    date_str = _require_str(entry.get("date"), "entry.date")
    home_team_id = _require_str(entry.get("home_team_id"), "entry.home_team_id")
    away_team_id = _require_str(entry.get("away_team_id"), "entry.away_team_id")
 
    if phase not in _ALLOWED_PHASES:
        raise ValueError(f"context invalid: phase must be one of {_ALLOWED_PHASES}, got '{phase}'")

    season_id = _require_str(league_state.get("active_season_id"), "league_state.active_season_id")

    # SSOT: Home/Away is fixed by external schedule context.
    return schema.GameContext.create(
        game_id=game_id,
        date=date_str,
        season_id=season_id,
        phase=phase,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )


def build_context_from_team_ids(
    game_id: str,
    date: str,
    home_team_id: str,
    away_team_id: str,
    league_state: Mapping[str, Any],
    *,
    phase: Phase = "regular",
) -> schema.GameContext:
    return build_context_from_master_schedule_entry(
        entry={
            "game_id": game_id,
            "date": date,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
        },
        league_state=league_state,
        phase=phase,
    )


# --- adapter core --------------------------------------------------------

# Keys in raw team summary that represent breakdown/counter structures.
_RAW_BREAKDOWN_KEYS = {
    "PossessionEndCounts",
    "ShotZoneDetail",
    "OffActionCounts",
    "DefActionCounts",
    "OutcomeCounts",
    "ShotZones",
}

# Keys in raw player box that are derived (percentages)
_RAW_PCT_KEYS = {"FG%", "3P%", "FT%"}


def _normalize_team_totals_from_raw(team_summary: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (totals, extra_totals) for v2.

    - totals: additive/counted metrics.
    - extra_totals: numeric metrics that are still additive but are not part of the canonical totals list.

    Note: AvgFatigue is treated as extra_totals (store sum; compute average in views).
    """

    # Canonical totals we want to expose consistently.
    canonical: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}

    def _put(dst: Dict[str, Any], k: str, v: Any) -> None:
        if _is_number(v):
            dst[k] = float(v)

    # Straight mappings
    for k in (
        "PTS",
        "FGM",
        "FGA",
        "FTM",
        "FTA",
        "TOV",
        "ORB",
        "DRB",
        "Possessions",
        "AST",
        "PITP",
        "FastbreakPTS",
        "SecondChancePTS",
        "PointsOffTOV",
    ):
        _put(canonical, k, team_summary.get(k, 0))

    # 3PT normalization
    _put(canonical, "3PM", team_summary.get("3PM", 0))
    _put(canonical, "3PA", team_summary.get("3PA", 0))

    # Collect other numeric top-level keys (excluding breakdowns/player blobs)
    for k, v in team_summary.items():
        if k in canonical or k in _RAW_BREAKDOWN_KEYS:
            continue
        if k in ("Players", "PlayerBox"):
            continue
        # Avoid derived percent-like keys (raw team summary does not include them today)
        if isinstance(k, str) and k.endswith("%"):
            continue

        if _is_number(v):
            # AvgFatigue is numeric but semantically an average.
            if k == "AvgFatigue":
                _put(extra, k, v)
            else:
                _put(extra, k, v)

    return canonical, extra


def _normalize_breakdowns_from_raw(team_summary: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (breakdowns, extra_breakdowns) for v2."""
    breakdowns: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}

    for k in _RAW_BREAKDOWN_KEYS:
        v = team_summary.get(k)
        if isinstance(v, dict):
            breakdowns[k] = v

    # Extra breakdowns: any other dict-of-numbers we don't explicitly recognize.
    # We intentionally ignore 'Players' and 'PlayerBox'.
    for k, v in team_summary.items():
        if k in _RAW_BREAKDOWN_KEYS or k in ("Players", "PlayerBox"):
            continue
        if isinstance(v, dict):
            # Heuristic: keep only if values are numeric or nested dicts of numeric.
            extra[k] = v

    # Remove any keys that are already in breakdowns
    for k in list(extra.keys()):
        if k in breakdowns:
            extra.pop(k, None)

    return breakdowns, extra


def _normalize_replay_events_from_raw(
    obj: Any,
    *,
    path: str,
) -> List[Dict[str, Any]]:
    """Normalize replay_events (list[dict]) from raw matchengine result.

    We intentionally keep this validation light:
      - Require list
      - Require each element is a dict

    Full schema validation belongs to the matchengine itself (emit_event / validation config).
    """
    if obj is None:
        return []
    if not isinstance(obj, list):
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' must be a list")
    out: List[Dict[str, Any]] = []
    for i, ev in enumerate(obj):
        if not isinstance(ev, dict):
            raise ValueError(f"raw matchengine_v3 result invalid: '{path}[{i}]' must be a dict")
        out.append(ev)
    return out


def _int_like(value: Any, *, path: str) -> int:
    if value is None:
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' is None (expected int-like)")
    if isinstance(value, bool):
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' is bool (expected int-like)")
    try:
        return int(value)
    except Exception:
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' value={value!r} not int-like")


def _float_like(value: Any, *, path: str) -> float:
    if value is None:
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' is None (expected float-like)")
    if isinstance(value, bool):
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' is bool (expected float-like)")
    try:
        return float(value)
    except Exception:
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' value={value!r} not float-like")


def _normalize_player_keyed_map(
    obj: Any,
    *,
    allowed_player_ids: "set[str]",
    path: str,
    value_kind: Literal["int", "float", "any"] = "any",
) -> Dict[str, Any]:
    """
    Normalize a dict keyed by player_id and validate:
      - keys are canonical player_id
      - key exists in allowed_player_ids (== PlayerBox players for that team)
      - no duplicates after normalization
    """
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' must be a dict")

    out: Dict[str, Any] = {}
    for k, v in obj.items():
        pid_raw = str(k)
        pid = _normalize_player_id_strict(pid_raw, path=f"{path}.<player_id>")
        # "no silent guessing": if engine gave non-canonical string, fail instead of rewriting
        if pid != pid_raw.strip():
            raise ValueError(
                f"raw matchengine_v3 result invalid: '{path}' player key must already be canonical; got {pid_raw!r}, expected {pid!r}"
            )
        if pid not in allowed_player_ids:
            raise ValueError(
                f"raw matchengine_v3 result invalid: '{path}' references unknown/wrong-team player_id='{pid}'"
            )
        if pid in out:
            raise ValueError(f"raw matchengine_v3 result invalid: '{path}' duplicate player_id='{pid}'")

        if value_kind == "int":
            out[pid] = _int_like(v, path=f"{path}[{pid}]")
        elif value_kind == "float":
            out[pid] = _float_like(v, path=f"{path}[{pid}]")
        else:
            out[pid] = v

    return out


def _normalize_player_rows_from_player_box(
    *,
    player_box: Mapping[str, Any],
    team_id: str,
) -> List[Dict[str, Any]]:
    """Convert raw PlayerBox ({pid: row}) to v2 players list."""
    if not isinstance(player_box, dict):
        raise ValueError("raw matchengine_v3 result invalid: team_summary.PlayerBox must be a dict")

    out: List[Dict[str, Any]] = []
    for pid, raw_row in player_box.items():
        if not isinstance(raw_row, dict):
            continue
        pid_raw = str(pid)
        pid_norm = _normalize_player_id_strict(pid_raw, path="raw.teams[].PlayerBox.<player_id>")
        # "no silent guessing": require the dict key itself is canonical (don't auto-fix)
        if pid_norm != pid_raw.strip():
            raise ValueError(
                f"raw matchengine_v3 result invalid: PlayerBox key must already be canonical; got {pid_raw!r}, expected {pid_norm!r}"
            )

        # If engine also embeds PlayerID/TeamID inside row, they MUST match.
        if "PlayerID" in raw_row and str(raw_row.get("PlayerID")).strip() != pid_raw.strip():
            raise ValueError(
                f"raw matchengine_v3 result invalid: PlayerBox row PlayerID mismatch for key={pid_raw!r} row.PlayerID={raw_row.get('PlayerID')!r}"
            )
        if "TeamID" in raw_row:
            row_tid = str(raw_row.get("TeamID")).strip()
            row_tid_norm = _normalize_team_id_strict(row_tid, path="raw.teams[].PlayerBox[].TeamID")
            if row_tid_norm != team_id:
                raise ValueError(
                    f"raw matchengine_v3 result invalid: PlayerBox row TeamID mismatch for player_id={pid_raw!r} row.TeamID={row_tid!r} expected team_id={team_id!r}"
                )

        row: Dict[str, Any] = {
            "PlayerID": pid_raw.strip(),
            "TeamID": str(team_id),
        }

        # Copy common non-numeric fields
        if "Name" in raw_row:
            row["Name"] = raw_row.get("Name")

        # Minutes
        if _is_number(raw_row.get("MIN")):
            row["MIN"] = float(raw_row["MIN"])

        # Map counted stats directly, excluding pct fields.
        derived: Dict[str, Any] = {}
        for k, v in raw_row.items():
            if k in ("Name", "MIN"):
                continue
            if k in _RAW_PCT_KEYS:
                # Store derived percentages under derived.
                if _is_number(v):
                    if k == "FG%":
                        derived["FG_PCT"] = float(v)
                    elif k == "3P%":
                        derived["3P_PCT"] = float(v)
                    elif k == "FT%":
                        derived["FT_PCT"] = float(v)
                continue

            # Normalize 3PT naming
            if k == "3PM" and _is_number(v):
                row["3PM"] = float(v)
                continue
            if k == "3PA" and _is_number(v):
                row["3PA"] = float(v)
                continue

            if _is_number(v):
                # Keep as additive/counted.
                row[k] = float(v)

        if derived:
            row["derived"] = derived

        out.append(row)

    return out


def _map_side_keyed_dict_to_team_ids(
    *,
    obj: Any,
    home_team_id: str,
    away_team_id: str,
    path: str,
) -> Dict[str, Any]:
    """Map {'home': X, 'away': Y} -> {home_team_id: X, away_team_id: Y}.

    If already keyed by team ids, pass through.
    Otherwise raise ValueError.
    """
    if obj is None:
        return {home_team_id: {}, away_team_id: {}}
    if not isinstance(obj, dict):
        raise ValueError(f"raw matchengine_v3 result invalid: '{path}' must be a dict")

    keys = set(obj.keys())
    if keys.issubset({"home", "away"}):
        if keys != {"home", "away"}:
            raise ValueError(
                f"raw matchengine_v3 result invalid: '{path}' must include both 'home' and 'away' keys; got keys={list(obj.keys())!r}"
            )
        return {
            home_team_id: obj.get("home", {}),
            away_team_id: obj.get("away", {}),
        }

    # Already keyed by team ids?
    if home_team_id in obj and away_team_id in obj:
        if set(obj.keys()) != {home_team_id, away_team_id}:
            raise ValueError(
                f"raw matchengine_v3 result invalid: '{path}' must have exactly two team_id keys "
                f"['{home_team_id}','{away_team_id}']; got keys={list(obj.keys())!r}"
            )
        return {
            home_team_id: obj.get(home_team_id, {}),
            away_team_id: obj.get(away_team_id, {}),
        }

    raise ValueError(
        f"raw matchengine_v3 result invalid: cannot map '{path}' keys to team ids; "
        f"got keys={list(obj.keys())!r}, expected either ['home','away'] or team ids ['{home_team_id}','{away_team_id}']"
    )



def adapt_matchengine_result_to_v2(

    raw = _require_dict(raw_result, "raw")
    meta = _require_dict(raw.get("meta"), "raw.meta")
    teams_obj = _require_dict(raw.get("teams"), "raw.teams")
    gs = _require_dict(raw.get("game_state"), "raw.game_state")

    game_id = _require_str(getattr(context, "game_id", None), "context.game_id")
    date_str = _require_str(getattr(context, "date", None), "context.date")
    season_id = _require_str(getattr(context, "season_id", None), "context.season_id")
    phase = _require_str(getattr(context, "phase", None), "context.phase")
    if phase not in _ALLOWED_PHASES:
        raise ValueError(f"context invalid: phase must be one of {_ALLOWED_PHASES}, got '{phase}'")

    home_team_id = _normalize_team_id_strict(getattr(context, "home_team_id", None), path="context.home_team_id")
    away_team_id = _normalize_team_id_strict(getattr(context, "away_team_id", None), path="context.away_team_id")

    if home_team_id == away_team_id:
        raise ValueError(f"context invalid: home_team_id == away_team_id == {home_team_id!r} (game_id={game_id!r})")

    # Raw result MUST be keyed by team_id only (no side keys, no extra teams).
    _require_team_id_keyed_two_team_map(
        obj=teams_obj,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        path="raw.teams",
        game_id=game_id,
    )

    home_summary = _require_dict(teams_obj.get(home_team_id), f"raw.teams[{home_team_id}]")
    away_summary = _require_dict(teams_obj.get(away_team_id), f"raw.teams[{away_team_id}]")

    # Build team results
    v2_teams: Dict[str, Any] = {}
    for tid, summary in ((home_team_id, home_summary), (away_team_id, away_summary)):
        totals, extra_totals = _normalize_team_totals_from_raw(summary)
        breakdowns, extra_breakdowns = _normalize_breakdowns_from_raw(summary)

        player_box = summary.get("PlayerBox")
        players = _normalize_player_rows_from_player_box(player_box=player_box or {}, team_id=tid)

        team_game: Dict[str, Any] = {
            "team_id": tid,
            "totals": totals,
            "breakdowns": breakdowns,
            "players": players,
        }
        if extra_totals:
            team_game["extra_totals"] = extra_totals
        # Only include extra_breakdowns if it contains something meaningful beyond known breakdowns.
        if extra_breakdowns:
            team_game["extra_breakdowns"] = extra_breakdowns

        # Sanity: ensure PTS exists (required by state.py validator)
        if "PTS" not in team_game["totals"]:
            # Try raw PTS if missing
            if _is_number(summary.get("PTS")):
                team_game["totals"]["PTS"] = float(summary["PTS"])
            else:
                raise ValueError(f"raw matchengine_v3 result invalid: team '{tid}' missing PTS")

        v2_teams[tid] = team_game

    # --- FINAL GATEKEEPER: validate player identity integrity ----------------
    # 1) Each team.players[] PlayerID must be unique
    # 2) Same player_id must NOT appear in both teams
    team_player_ids: Dict[str, set[str]] = {}
    all_pids: set[str] = set()
    for tid in (home_team_id, away_team_id):
        players_list = v2_teams[tid].get("players") or []
        if not isinstance(players_list, list):
            raise ValueError(f"raw matchengine_v3 result invalid: teams['{tid}'].players must be a list")
        seen: set[str] = set()
        for p in players_list:
            if not isinstance(p, dict):
                raise ValueError(f"raw matchengine_v3 result invalid: teams['{tid}'].players contains non-dict")
            pid = _normalize_player_id_strict(p.get("PlayerID"), path=f"teams['{tid}'].players[].PlayerID")
            if pid in seen:
                raise ValueError(f"raw matchengine_v3 result invalid: duplicate player_id='{pid}' inside team '{tid}'")
            if pid in all_pids:
                raise ValueError(f"raw matchengine_v3 result invalid: duplicate player_id='{pid}' across teams")
            if str(p.get("TeamID")) != tid:
                raise ValueError(
                    f"raw matchengine_v3 result invalid: player_id='{pid}' has TeamID={p.get('TeamID')!r} but is listed under team '{tid}'"
                )
            seen.add(pid)
            all_pids.add(pid)
        team_player_ids[tid] = seen

    # Final score from team totals (PTS)
    final = {
        home_team_id: int(float(v2_teams[home_team_id]["totals"].get("PTS", 0))),
        away_team_id: int(float(v2_teams[away_team_id]["totals"].get("PTS", 0))),
    }

    # game_state: team-keyed dicts MUST be team_id only (no 'home'/'away' side keys).
    team_fouls = _require_team_id_keyed_two_team_map(
        obj=_require_dict(gs.get("team_fouls"), "raw.game_state.team_fouls"),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        path="raw.game_state.team_fouls",
        game_id=game_id,
    )
    player_fouls = _require_team_id_keyed_two_team_map(
        obj=_require_dict(gs.get("player_fouls"), "raw.game_state.player_fouls"),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        path="raw.game_state.player_fouls",
        game_id=game_id,
    )
    fatigue = _require_team_id_keyed_two_team_map(
        obj=_require_dict(gs.get("fatigue"), "raw.game_state.fatigue"),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        path="raw.game_state.fatigue",
        game_id=game_id,
    )
    minutes_played_sec = _require_team_id_keyed_two_team_map(
        obj=_require_dict(gs.get("minutes_played_sec"), "raw.game_state.minutes_played_sec"),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        path="raw.game_state.minutes_played_sec",
        game_id=game_id,
    )

    # replay_events: new single source-of-truth play-by-play stream.
    # Preferred location (engine patch): raw['replay_events'].
    # Fallback: raw['game_state']['replay_events'] (if some caller embeds it there).
    replay_events: List[Dict[str, Any]] = []
    if include_replay_events:
        replay_events_obj = raw.get("replay_events", None)
        replay_events_path = "raw.replay_events"
        if replay_events_obj is None and isinstance(gs, dict) and "replay_events" in gs:
            replay_events_obj = gs.get("replay_events")
            replay_events_path = "raw.game_state.replay_events"
        replay_events = _normalize_replay_events_from_raw(replay_events_obj, path=replay_events_path)
        
    # --- Normalize & validate game_state player-keyed dicts ------------------
    # Ensure player keys are canonical player_id AND belong to the correct team
    # ("존재하지 않는 선수", "다른 팀 선수", "중복 선수" => 즉시 에러)
    team_fouls_norm = {
        home_team_id: _int_like(team_fouls.get(home_team_id, 0), path=f"raw.game_state.team_fouls[{home_team_id}]"),
        away_team_id: _int_like(team_fouls.get(away_team_id, 0), path=f"raw.game_state.team_fouls[{away_team_id}]"),
    }

    player_fouls_norm = {
        home_team_id: _normalize_player_keyed_map(
            player_fouls.get(home_team_id, {}),
            allowed_player_ids=team_player_ids[home_team_id],
            path=f"raw.game_state.player_fouls[{home_team_id}]",
            value_kind="int",
        ),
        away_team_id: _normalize_player_keyed_map(
            player_fouls.get(away_team_id, {}),
            allowed_player_ids=team_player_ids[away_team_id],
            path=f"raw.game_state.player_fouls[{away_team_id}]",
            value_kind="int",
        ),
    }
    fatigue_norm = {
        home_team_id: _normalize_player_keyed_map(
            fatigue.get(home_team_id, {}),
            allowed_player_ids=team_player_ids[home_team_id],
            path=f"raw.game_state.fatigue[{home_team_id}]",
            value_kind="float",
        ),
        away_team_id: _normalize_player_keyed_map(
            fatigue.get(away_team_id, {}),
            allowed_player_ids=team_player_ids[away_team_id],
            path=f"raw.game_state.fatigue[{away_team_id}]",
            value_kind="float",
        ),
    }
    minutes_played_sec_norm = {
        home_team_id: _normalize_player_keyed_map(
            minutes_played_sec.get(home_team_id, {}),
            allowed_player_ids=team_player_ids[home_team_id],
            path=f"raw.game_state.minutes_played_sec[{home_team_id}]",
            value_kind="int",
        ),
        away_team_id: _normalize_player_keyed_map(
            minutes_played_sec.get(away_team_id, {}),
            allowed_player_ids=team_player_ids[away_team_id],
            path=f"raw.game_state.minutes_played_sec[{away_team_id}]",
            value_kind="int",
        ),
    }

    # Required integer fields
    try:
        overtime_periods = int(meta.get("overtime_periods", 0) or 0)
    except (TypeError, ValueError):
        logger.warning("V2_ADAPTER_INT_COERCE_FAILED overtime_periods=%r", meta.get("overtime_periods"), exc_info=True)
        overtime_periods = 0

    try:
        possessions_per_team = int(raw.get("possessions_per_team", 0) or 0)
    except (TypeError, ValueError):
        logger.warning("V2_ADAPTER_INT_COERCE_FAILED possessions_per_team=%r", raw.get("possessions_per_team"), exc_info=True)
        possessions_per_team = 0

    game = {
        "game_id": game_id,
        "date": date_str,
        "season_id": season_id,
        "phase": phase,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "overtime_periods": overtime_periods,
        "possessions_per_team": possessions_per_team,
    }

    v2_meta = {
        "engine_name": engine_name,
        "engine_version": str(meta.get("engine_version", "")),
        "era": str(meta.get("era", "")),
        "era_version": str(meta.get("era_version", "")),
        "replay_token": str(meta.get("replay_token", "")),
    }

    # Optional debug (preserve validation/internal_debug if present)
    debug: Dict[str, Any] = {}
    if "validation" in meta:
        debug["validation"] = meta.get("validation")
    if "internal_debug" in meta:
        debug["internal_debug"] = meta.get("internal_debug")

    out: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "game": game,
        "final": final,
        "teams": v2_teams,
        "game_state": {
            "team_fouls": team_fouls_norm,
            "player_fouls": player_fouls_norm,
            "fatigue": fatigue_norm,
            "minutes_played_sec": minutes_played_sec_norm,
        },
        "meta": v2_meta,
    }

    if debug:
        out["debug"] = debug

    if include_replay_events and replay_events:
        out["replay_events"] = replay_events

    # Include raw only if caller asks for it.
    # Note: state.py stores the entire game_result dict into state['game_results'].
    # If you always include raw here, you'll duplicate the raw payload inside the stored v2.
    if include_raw:
        out["raw"] = raw

    # --- FINAL CONTRACT GATE: v2 validator MUST pass -----------------------
    # Lazy import to avoid circular imports at module import time.
    from state_modules.state_results import validate_v2_game_result

    validate_v2_game_result(out)
    return out
