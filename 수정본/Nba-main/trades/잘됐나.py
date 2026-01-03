from __future__ import annotations

"""
picks.py (complete module)

Draft-pick utilities for the game:
- Ensure/initialize draft picks in GAME_STATE
- Validate pick-trade legality (7-year window, Stepien rule)
- Transfer ownership (apply stage)

Drop-in compatibility:
- Keeps init_draft_picks_if_needed(...) and transfer_pick(...)

New additions:
- ensure_draft_picks(...)
- validate_pick_year_window(...)
- validate_stepien_rule_after_transfers(...)
- apply_pick_transfers(...)
"""

from typing import Dict, Iterable, List, Optional, Set, Tuple

from .errors import TradeError, PICK_NOT_OWNED
from typing import Any

# These may not exist yet in your errors.py; provide safe fallbacks so the module
# remains importable until you add them centrally.
try:
    from .errors import PICK_TOO_FAR_IN_FUTURE, STEPIEN_RULE_VIOLATION
except Exception:  # pragma: no cover
    PICK_TOO_FAR_IN_FUTURE = "PICK_TOO_FAR_IN_FUTURE"
    STEPIEN_RULE_VIOLATION = "STEPIEN_RULE_VIOLATION"


# -----------------------------
# Helpers
# -----------------------------

def _norm_team(team_id: str) -> str:
    return str(team_id).upper().strip()


def build_pick_id(year: int, round_num: int, original_team: str) -> str:
    """
    Canonical pick_id format used by this module.

    Example: 2028_R1_LAL
    """
    return f"{int(year)}_R{int(round_num)}_{_norm_team(original_team)}"


def _get_pick(game_state: dict, pick_id: str) -> dict:
    draft_picks = game_state.get("draft_picks") or {}
    pick = draft_picks.get(pick_id)
    if not pick:
        raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pick_id})
    return pick


# -----------------------------
# Draft pick creation / ensuring
# -----------------------------

def ensure_draft_picks(
    game_state: dict,
    season_year: int,
    all_team_ids: List[str],
    years_ahead: int = 7,
) -> None:
    """
    Ensure the game_state has draft pick objects for each team for:
      year in [draft_year, draft_year + years_ahead]  (inclusive)
      round in {1, 2}

    Does NOT overwrite existing picks, but will:
    - Create missing pick entries
    - Backfill missing fields in existing entries (year/round/original_team/owner_team)

    Draft year source of truth:
    - Prefer game_state["league"]["draft_year"] when present (e.g., 2025-26 => 2026)
    - Fallback: treat the passed season_year as "season start year" and use (season_year + 1)

    Why this exists:
    - Some older implementations return early if draft_picks is non-empty, which
      prevents creating future picks as seasons advance.
    """
    if years_ahead < 0:
        raise ValueError("years_ahead must be >= 0")

    league = (game_state.get("league") or {})
    draft_year = league.get("draft_year")
    base_year: int
    if draft_year is None:
        # Legacy fallback: season_year is season-start year
        base_year = int(season_year) + 1
    else:
        base_year = int(draft_year)

    draft_picks = game_state.setdefault("draft_picks", {})
    norm_teams = [_norm_team(t) for t in all_team_ids]

    for year in range(int(base_year), int(base_year) + int(years_ahead) + 1):
        for round_num in (1, 2):
            for team_id in norm_teams:
                pick_id = build_pick_id(year, round_num, team_id)
                if pick_id not in draft_picks:
                    draft_picks[pick_id] = {
                        "pick_id": pick_id,
                        "year": year,
                        "round": round_num,
                        "original_team": team_id,
                        "owner_team": team_id,
                    }
                else:
                    p = draft_picks[pick_id]
                    p.setdefault("pick_id", pick_id)
                    p.setdefault("year", year)
                    p.setdefault("round", round_num)
                    p.setdefault("original_team", team_id)
                    p.setdefault("owner_team", team_id)


def init_draft_picks_if_needed(
    game_state: dict,
    season_year: int,
    all_team_ids: List[str],
    years_ahead: int = 7,
) -> None:
    """
    Backward-compatible entry point.

    This version always ensures the required year window exists (based on draft_year).
    """
    ensure_draft_picks(game_state, season_year, all_team_ids, years_ahead)


# -----------------------------
# Ownership transfer (apply-stage)
# -----------------------------

def transfer_pick(game_state: dict, pick_id: str, from_team: str, to_team: str) -> None:
    """
    Apply-stage mutation:
    - pick must exist
    - from_team must be the current owner
    - owner_team updated to to_team
    """
    pick = _get_pick(game_state, pick_id)

    if _norm_team(pick.get("owner_team", "")) != _norm_team(from_team):
        raise TradeError(
            PICK_NOT_OWNED,
            "Pick not owned by team",
            {"pick_id": pick_id, "team_id": _norm_team(from_team)},
        )

    pick["owner_team"] = _norm_team(to_team)


# -----------------------------
# Validations for trade rules
# -----------------------------

def validate_pick_year_window(
    game_state: dict,
    pick_ids: Iterable[str],
    current_season_year: int,
    max_years_ahead: int = 7,
) -> None:
    """
    7-year rule:
    You may not trade a pick with pick.year > current_season_year + max_years_ahead.
    """
    if max_years_ahead < 0:
        raise ValueError("max_years_ahead must be >= 0")

    limit_year = int(current_season_year) + int(max_years_ahead)
    draft_picks = game_state.get("draft_picks") or {}

    for pick_id in pick_ids:
        pick = draft_picks.get(pick_id)
        if not pick:
            raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pick_id})

        try:
            year = int(pick.get("year"))
        except Exception:
            raise TradeError(
                PICK_TOO_FAR_IN_FUTURE,
                "Pick year is invalid",
                {"pick_id": pick_id, "year": pick.get("year")},
            )

        if year > limit_year:
            raise TradeError(
                PICK_TOO_FAR_IN_FUTURE,
                "Pick is too far in the future",
                {"pick_id": pick_id, "pick_year": year, "limit_year": limit_year},
            )


def validate_stepien_rule_after_transfers(
    game_state: dict,
    pick_transfers: List[Tuple[str, str, str]],
    current_season_year: int,
    lookahead_years: int = 7,
    teams_to_check: Optional[Set[str]] = None,
) -> None:
    """
    Stepien Rule (simplified, game-ready):

    After applying the pick transfers, no team may have ZERO 1st-round picks in two
    consecutive draft years within the lookahead window.

    - "Having a 1st-round pick" means owning ANY 1st-round pick that year, regardless of origin.
    - Validation is performed on the simulated "after" ownership.
    """
    if lookahead_years < 2:
        return

    draft_picks = game_state.get("draft_picks") or {}

    # Snapshot of owner after trade (start with current owners)
    owner_after: Dict[str, str] = {
        pid: _norm_team(pick.get("owner_team", ""))
        for pid, pick in draft_picks.items()
    }

    affected_teams: Set[str] = set()

    # Apply transfers to snapshot
    for pick_id, from_team, to_team in pick_transfers:
        pick = draft_picks.get(pick_id)
        if not pick:
            raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pick_id})

        from_t = _norm_team(from_team)
        to_t = _norm_team(to_team)

        if owner_after.get(pick_id, "") != from_t:
            raise TradeError(
                PICK_NOT_OWNED,
                "Pick not owned by team",
                {"pick_id": pick_id, "team_id": from_t},
            )

        owner_after[pick_id] = to_t
        affected_teams.add(from_t)
        affected_teams.add(to_t)

    teams: Set[str]
    if teams_to_check is None:
        teams = affected_teams
    else:
        teams = {_norm_team(t) for t in teams_to_check}

    if not teams:
        return  # no picks moved => no Stepien impact

    start_year = int(current_season_year) + 1
    end_year = int(current_season_year) + int(lookahead_years)  # inclusive

    for team in teams:
        first_round_count: Dict[int, int] = {y: 0 for y in range(start_year, end_year + 1)}

        for pid, pick in draft_picks.items():
            try:
                rnd = int(pick.get("round"))
                year = int(pick.get("year"))
            except Exception:
                continue

            if rnd != 1:
                continue
            if year < start_year or year > end_year:
                continue

            if owner_after.get(pid, "") == team:
                first_round_count[year] += 1

        for y in range(start_year, end_year):
            if first_round_count[y] == 0 and first_round_count[y + 1] == 0:
                raise TradeError(
                    STEPIEN_RULE_VIOLATION,
                    "Stepien Rule violation: no 1st-round pick in consecutive years",
                    {
                        "team_id": team,
                        "year_a": y,
                        "year_b": y + 1,
                        "lookahead_years": int(lookahead_years),
                    },
                )


def apply_pick_transfers(
    game_state: dict,
    pick_transfers: List[Tuple[str, str, str]],
) -> None:
    """
    Convenience helper for apply-stage:
      pick_transfers = [(pick_id, from_team, to_team), ...]
    """
    for pick_id, from_team, to_team in pick_transfers:
        transfer_pick(game_state, pick_id, from_team, to_team)
