from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, runtime_checkable

JsonDict = Dict[str, Any]


# ----------------------------
# Protocols (avoid import cycles with college.types)
# ----------------------------

@runtime_checkable
class _HasSeasonStats(Protocol):
    season_year: int
    player_id: str
    college_team_id: str

    games: int
    mpg: float

    pts: float
    reb: float
    ast: float
    stl: float
    blk: float
    tov: float
    pf: float

    fg_pct: float
    tp_pct: float
    ft_pct: float

    usg: float
    ts_pct: float
    pace: float

    meta: Mapping[str, Any]


@runtime_checkable
class _HasDecisionTrace(Protocol):
    player_id: str
    draft_year: int
    declared: bool
    declare_prob: float
    projected_pick: Optional[int]
    factors: Mapping[str, Any]
    notes: Mapping[str, Any]


# ----------------------------
# Public API
# ----------------------------

STATS_JSON_VERSION = 1
DECISION_JSON_VERSION = 1


def season_stats_to_json(ps: _HasSeasonStats) -> JsonDict:
    """
    Stable, explicit JSON shape for CollegeSeasonStats.

    Key goals:
    - Do NOT rely on dataclass __dict__ (slots-safe).
    - Keep DB payload compact and stable across field additions.
    - Ensure JSON-serializable output (best-effort sanitation).
    """
    out: JsonDict = {
        "__v": STATS_JSON_VERSION,
        "season_year": int(ps.season_year),
        "player_id": str(ps.player_id),
        "college_team_id": str(ps.college_team_id),
        "games": int(ps.games),
        # UI-friendly per-game style numbers (already per-game in your model).
        "mpg": _rf(ps.mpg, 3),
        "pts": _rf(ps.pts, 3),
        "reb": _rf(ps.reb, 3),
        "ast": _rf(ps.ast, 3),
        "stl": _rf(ps.stl, 3),
        "blk": _rf(ps.blk, 3),
        "tov": _rf(ps.tov, 3),
        "pf": _rf(ps.pf, 3),
        # Splits / efficiency (0..1 or 0..100 depending on your generator; keep as-is but rounded).
        "fg_pct": _rf(ps.fg_pct, 4),
        "tp_pct": _rf(ps.tp_pct, 4),
        "ft_pct": _rf(ps.ft_pct, 4),
        "usg": _rf(ps.usg, 4),
        "ts_pct": _rf(ps.ts_pct, 4),
        "pace": _rf(ps.pace, 3),
        "meta": _sanitize_json(ps.meta),
    }
    return out


def decision_trace_to_json(tr: _HasDecisionTrace) -> JsonDict:
    """
    Stable, explicit JSON shape for DraftEntryDecisionTrace.

    Stores "why declared" telemetry for tuning.
    """
    out: JsonDict = {
        "__v": DECISION_JSON_VERSION,
        "player_id": str(tr.player_id),
        "draft_year": int(tr.draft_year),
        "declared": bool(tr.declared),
        "declare_prob": _rf(tr.declare_prob, 6),
        "projected_pick": (int(tr.projected_pick) if tr.projected_pick is not None else None),
        "factors": _sanitize_json(tr.factors),
        "notes": _sanitize_json(tr.notes),
    }
    return out


def to_json_dict(obj: Any) -> JsonDict:
    """
    Convenience dispatcher (optional).

    Use this if you want a single entrypoint in service layer:
      json_dumps(to_json_dict(ps))
    """
    if isinstance(obj, _HasSeasonStats):
        return season_stats_to_json(obj)
    if isinstance(obj, _HasDecisionTrace):
        return decision_trace_to_json(obj)
    raise TypeError(f"Unsupported object for college serialization: {type(obj)!r}")


# ----------------------------
# Internals
# ----------------------------

def _rf(x: Any, ndigits: int) -> float:
    """Round-float helper. Always returns a float (JSON-safe)."""
    try:
        return float(round(float(x), ndigits))
    except Exception:
        # Last-resort: stringify to keep DB write alive, but this should be rare.
        try:
            return float(x)
        except Exception:
            return float("nan")


def _sanitize_json(obj: Any) -> Any:
    """
    Best-effort conversion to JSON-serializable structures:
    - dict keys to str
    - recursively sanitize lists/tuples/sets
    - primitives stay as-is
    - fallback to str() for unknown objects
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            out[str(k)] = _sanitize_json(v)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_json(v) for v in obj]
    # For other iterables (rare), convert to list safely
    if _is_iterable(obj):
        try:
            return [_sanitize_json(v) for v in obj]  # type: ignore[assignment]
        except Exception:
            pass
    return str(obj)


def _is_iterable(obj: Any) -> bool:
    if isinstance(obj, (str, bytes, bytearray)):
        return False
    try:
        iter(obj)  # type: ignore[arg-type]
        return True
    except Exception:
        return False
