from __future__ import annotations

from typing import Any, Dict

import state as state_facade
from state_modules.state_store import get_state_ref
from state_modules.state_schema import default_cached_views_state


def _ensure_cached_views_meta() -> Dict[str, Any]:
    """cached_views 메타 정보가 없으면 기본값으로 생성한다."""
    cached_views = get_state_ref().get("cached_views", {})
    meta = cached_views.get("_meta")
    return meta if isinstance(meta, dict) else {}


def _mark_views_dirty() -> None:
    """캐시된 뷰를 다시 계산하도록 무효화한다."""
    state_facade.mark_cache_dirty_scores()
    cached_views = get_state_ref().get("cached_views", {})
    schedule_meta = (
        cached_views.get("_meta", {}).get("schedule", {}).get("built_from_turn_by_team")
    )
    team_ids = list(schedule_meta.keys()) if isinstance(schedule_meta, dict) else []
    state_facade.mark_cache_dirty_schedule_for_teams(team_ids)


def _reset_cached_views_for_new_season() -> None:
    """정규시즌 누적이 새로 시작될 때 cached_views도 함께 초기화한다."""
    get_state_ref()["cached_views"] = default_cached_views_state()
