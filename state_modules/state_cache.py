from __future__ import annotations

from typing import Any, Dict

def ensure_cached_views_meta(state: dict) -> Dict[str, Any]:
    """cached_views 메타 정보가 없으면 기본값으로 생성한다."""
    cached = state.setdefault("cached_views", {})
    meta = cached.setdefault("_meta", {})
    meta.setdefault("scores", {"built_from_turn": -1, "season_id": None})
    meta.setdefault("schedule", {"built_from_turn_by_team": {}})
    return meta
