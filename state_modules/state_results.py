from __future__ import annotations

from typing import Any, Dict, List, Optional

from state_store import _ALLOWED_PHASES, _META_PLAYER_KEYS
from state_utils import _is_number, _merge_counter_dict_sum, _require_dict, _require_list


def _validate_game_result_v2(game_result: Dict[str, Any]) -> None:
    if not isinstance(game_result, dict):
        raise ValueError("GameResultV2 invalid: result must be a dict")

    if game_result.get("schema_version") != "2.0":
        raise ValueError("GameResultV2 invalid: schema_version must be '2.0'")

    game = _require_dict(game_result.get("game"), "game")

    required_game_keys = [
        "game_id",
        "date",
        "season_id",
        "phase",
        "home_team_id",
        "away_team_id",
        "overtime_periods",
        "possessions_per_team",
    ]
    for k in required_game_keys:
        if k not in game:
            raise ValueError(f"GameResultV2 invalid: missing game.{k}")

    if game["phase"] not in _ALLOWED_PHASES:
        raise ValueError(f"GameResultV2 invalid: unsupported phase '{game['phase']}'")

    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])

    final = _require_dict(game_result.get("final"), "final")
    if home_id not in final or away_id not in final:
        raise ValueError("GameResultV2 invalid: final must include both home and away team ids")

    teams = _require_dict(game_result.get("teams"), "teams")
    if home_id not in teams or away_id not in teams:
        raise ValueError("GameResultV2 invalid: teams must include both home and away team ids")

    for tid in (home_id, away_id):
        team_obj = _require_dict(teams.get(tid), f"teams.{tid}")
        totals = _require_dict(team_obj.get("totals"), f"teams.{tid}.totals")
        if "PTS" not in totals:
            raise ValueError(f"GameResultV2 invalid: teams.{tid}.totals.PTS is required")

        players = _require_list(team_obj.get("players"), f"teams.{tid}.players")
        for idx, row in enumerate(players):
            if not isinstance(row, dict):
                raise ValueError(f"GameResultV2 invalid: teams.{tid}.players[{idx}] must be a dict")
            if "PlayerID" not in row:
                raise ValueError(f"GameResultV2 invalid: teams.{tid}.players[{idx}].PlayerID is required")
            if "TeamID" not in row:
                raise ValueError(f"GameResultV2 invalid: teams.{tid}.players[{idx}].TeamID is required")
            if str(row["TeamID"]) != tid:
                raise ValueError(
                    f"GameResultV2 invalid: teams.{tid}.players[{idx}].TeamID must match team id '{tid}'"
                )

        breakdowns = team_obj.get("breakdowns", {})
        if breakdowns is not None and not isinstance(breakdowns, dict):
            raise ValueError(f"GameResultV2 invalid: teams.{tid}.breakdowns must be a dict if present")


def validate_v2_game_result(game_result: Dict[str, Any]) -> None:
    """Public validator: raises ValueError if the v2 contract is violated."""
    _validate_game_result_v2(game_result)


def _accumulate_player_rows(
    rows: List[Dict[str, Any]],
    season_player_stats: Dict[str, Any],
) -> None:
    """players(list[row])를 시즌 누적 player_stats에 반영한다."""
    for row in rows:
        player_id = str(row["PlayerID"])
        team_id = str(row["TeamID"])

        entry = season_player_stats.setdefault(
            player_id,
            {"player_id": player_id, "name": row.get("Name"), "team_id": team_id, "games": 0, "totals": {}},
        )
        entry["name"] = row.get("Name", entry.get("name"))
        entry["team_id"] = team_id
        entry["games"] = int(entry.get("games", 0) or 0) + 1

        totals = entry.setdefault("totals", {})
        for k, v in row.items():
            if k in _META_PLAYER_KEYS:
                continue
            if _is_number(v):
                try:
                    totals[k] = float(totals.get(k, 0.0)) + float(v)
                except (TypeError, ValueError):
                    continue


def _accumulate_team_game_result(
    team_id: str,
    team_game: Dict[str, Any],
    season_team_stats: Dict[str, Any],
) -> None:
    """팀 1경기 결과를 시즌 누적 team_stats에 반영한다."""
    totals_src = _require_dict(team_game.get("totals"), f"teams.{team_id}.totals")
    breakdowns_src = team_game.get("breakdowns") or {}
    extra_totals = team_game.get("extra_totals") or {}
    extra_breakdowns = team_game.get("extra_breakdowns") or {}

    entry = season_team_stats.setdefault(team_id, {"team_id": team_id, "games": 0, "totals": {}, "breakdowns": {}})
    entry["games"] = int(entry.get("games", 0) or 0) + 1

    totals = entry.setdefault("totals", {})
    for k, v in {**totals_src, **extra_totals}.items():
        if _is_number(v):
            try:
                totals[k] = float(totals.get(k, 0.0)) + float(v)
            except (TypeError, ValueError):
                continue

    breakdowns = entry.setdefault("breakdowns", {})
    if isinstance(breakdowns_src, dict):
        _merge_counter_dict_sum(breakdowns, breakdowns_src)
    if isinstance(extra_breakdowns, dict):
        _merge_counter_dict_sum(breakdowns, extra_breakdowns)


def build_game_obj_from_result(game_result: Dict[str, Any], game_date: Optional[str] = None) -> Dict[str, Any]:
    game = _require_dict(game_result.get("game"), "game")
    season_id = str(game["season_id"])
    phase = str(game["phase"])

    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])
    final = _require_dict(game_result.get("final"), "final")

    game_date_str = str(game_date) if game_date else str(game["date"])
    game_id = str(game["game_id"])

    home_score = int(final[home_id])
    away_score = int(final[away_id])

    return {
        "game_id": game_id,
        "date": game_date_str,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "status": "final",
        "is_overtime": int(game.get("overtime_periods", 0) or 0) > 0,
        "phase": phase,
        "season_id": season_id,
        "schema_version": "2.0",
    }
