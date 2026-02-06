from __future__ import annotations

"""Stepien rule policy.

This module centralizes Stepien logic so that validation rules and
generation/AI systems can share identical behavior.

Policy mirrors the behavior currently implemented in `PickRulesRule`:

- Stepien is evaluated over consecutive (year, year+1) pairs.
- The evaluation window is:
    start = current_draft_year + 1
    end   = current_draft_year + lookahead
  and `end` is clamped so that (year+1) remains within the range of years
  actually represented by the first-round pick data (to avoid false
  violations when pick data is incomplete).
- A violation occurs if, for any checked year, the team owns *zero* first-
  round picks in both `year` and `year+1` after applying ownership changes.
"""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple


def normalize_team_id(value: object) -> str:
    """Normalize team ids for comparisons.

    This mirrors the normalization used by PickRulesRule where owner_team="lal"
    should match receiver="LAL".
    """

    s = str(value).strip() if value is not None else ""
    return s.upper() if s else ""


@dataclass(frozen=True, slots=True)
class StepienViolation:
    team_id: str
    year: int
    next_year: int
    count_year: int
    count_next_year: int


def compute_max_first_round_year_in_data(
    draft_picks: Mapping[str, Mapping[str, Any]],
) -> int:
    """Return the maximum `year` among first-round picks in the provided data.

    If the data is missing/partial, returns 0.
    """

    max_year = 0
    for pick in draft_picks.values():
        try:
            if int(pick.get("round") or 0) != 1:
                continue
            year_val = int(pick.get("year") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if year_val > max_year:
            max_year = year_val
    return max_year


def check_stepien_violation(
    *,
    team_id: str,
    draft_picks: Mapping[str, Mapping[str, Any]],
    current_draft_year: int,
    lookahead: int,
    owner_after: Optional[Mapping[str, str]] = None,
    strict: bool = True,
) -> Optional[StepienViolation]:
    """Check Stepien compliance.

    Args:
        team_id: Team to evaluate.
        draft_picks: Mapping pick_id -> pick state dict (must include year/round).
        current_draft_year: League draft year (used as base for lookahead window).
        lookahead: Stepien lookahead years (<=0 disables check).
        owner_after: Optional mapping pick_id -> owner team id after trade.
            When omitted, it is derived from `draft_picks[pick_id]["owner_team"]`.
            Values are normalized before comparison.
        strict: If True, invalid types for required numeric inputs raise.

    Returns:
        StepienViolation if a violation is detected, else None.
    """

    if lookahead <= 0:
        return None

    try:
        base_year = int(current_draft_year)
        la = int(lookahead)
    except (TypeError, ValueError) as exc:
        if strict:
            raise RuntimeError(
                f"stepien_policy: invalid current_draft_year/lookahead: {current_draft_year!r}/{lookahead!r}"
            ) from exc
        return None

    if base_year <= 0:
        if strict:
            raise RuntimeError("stepien_policy: current_draft_year must be positive")
        return None

    normalized_team = normalize_team_id(team_id)
    if not normalized_team:
        if strict:
            raise RuntimeError("stepien_policy: team_id must be non-empty")
        return None

    # Build normalized owner_after mapping.
    if owner_after is None:
        owner_map: Dict[str, str] = {
            pick_id: normalize_team_id(pick.get("owner_team"))
            for pick_id, pick in draft_picks.items()
        }
    else:
        # Normalize provided mapping defensively.
        owner_map = {pick_id: normalize_team_id(v) for pick_id, v in owner_after.items()}

    max_first_round_year_in_data = compute_max_first_round_year_in_data(draft_picks)

    start = base_year + 1
    end = base_year + la
    # Clamp end so that (year+1) is still within available pick data.
    # If max_first_round_year_in_data is 0, we can't clamp safely (no data), so keep original end.
    if max_first_round_year_in_data > 0:
        end = min(end, max_first_round_year_in_data - 1)
    if end < start:
        return None

    for year in range(start, end + 1):  # Inclusive to check (end, end + 1) pair.
        count_year = _count_first_round_picks_for_year(draft_picks, owner_map, normalized_team, year)
        count_next = _count_first_round_picks_for_year(draft_picks, owner_map, normalized_team, year + 1)
        if count_year == 0 and count_next == 0:
            return StepienViolation(
                team_id=team_id,
                year=year,
                next_year=year + 1,
                count_year=count_year,
                count_next_year=count_next,
            )

    return None


def check_stepien_violation_with_evidence(
    *,
    team_id: str,
    draft_picks: Mapping[str, Mapping[str, Any]],
    current_draft_year: int,
    lookahead: int,
    owner_after: Optional[Mapping[str, str]] = None,
    strict: bool = True,
) -> Tuple[Optional[StepienViolation], Dict[str, Any]]:
    """Same as `check_stepien_violation`, but returns an evidence payload."""

    violation = check_stepien_violation(
        team_id=team_id,
        draft_picks=draft_picks,
        current_draft_year=current_draft_year,
        lookahead=lookahead,
        owner_after=owner_after,
        strict=strict,
    )

    max_first_round_year_in_data = compute_max_first_round_year_in_data(draft_picks)
    try:
        base_year = int(current_draft_year)
        la = int(lookahead)
    except (TypeError, ValueError):
        base_year = 0
        la = 0
    start = base_year + 1 if base_year > 0 else 0
    end = base_year + la if base_year > 0 else 0
    if max_first_round_year_in_data > 0:
        end = min(end, max_first_round_year_in_data - 1)

    evidence: Dict[str, Any] = {
        "team_id": team_id,
        "team_id_normalized": normalize_team_id(team_id),
        "current_draft_year": current_draft_year,
        "lookahead": lookahead,
        "start": start,
        "end": end,
        "data_max_first_round_year": max_first_round_year_in_data,
        "has_owner_after_override": owner_after is not None,
        "violation": None,
    }
    if violation is not None:
        evidence["violation"] = {
            "year": violation.year,
            "next_year": violation.next_year,
            "count_year": violation.count_year,
            "count_next_year": violation.count_next_year,
        }
    return violation, evidence


def _count_first_round_picks_for_year(
    draft_picks: Mapping[str, Mapping[str, Any]],
    owner_after: Mapping[str, str],
    normalized_team_id: str,
    year: int,
) -> int:
    count = 0
    for pick_id, pick in draft_picks.items():
        try:
            if int(pick.get("year") or 0) != int(year):
                continue
            if int(pick.get("round") or 0) != 1:
                continue
        except (TypeError, ValueError, AttributeError):
            continue
        if owner_after.get(pick_id) == normalized_team_id:
            count += 1
    return count
