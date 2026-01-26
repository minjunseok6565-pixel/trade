from __future__ import annotations

from typing import Any, Dict


def _ensure_cached_views_meta(state: dict) -> Dict[str, Any]:
    """cached_views 메타 정보가 없으면 기본값으로 생성한다."""
    cached = state["cached_views"]
    if not isinstance(cached, dict):
        raise ValueError("cached_views must be a dict")
    meta = cached["_meta"]
    if not isinstance(meta, dict):
        raise ValueError("cached_views._meta must be a dict")
    return meta


def _mark_views_dirty(state: dict) -> None:
    """캐시된 뷰를 다시 계산하도록 무효화한다."""
    meta = _ensure_cached_views_meta(state)
    meta["scores"]["built_from_turn"] = -1
    meta["schedule"]["built_from_turn_by_team"] = {}
    state["cached_views"]["stats"]["leaders"] = None


def _reset_cached_views_for_new_season(state: dict) -> None:
    """정규시즌 누적이 새로 시작될 때 cached_views도 함께 초기화한다."""
    cached = state["cached_views"]
    if not isinstance(cached, dict):
        raise ValueError("cached_views must be a dict")
    scores = cached["scores"]
    scores["latest_date"] = None
    scores["games"] = []
    schedule = cached["schedule"]
    schedule["teams"] = {}
    meta = _ensure_cached_views_meta(state)
    meta["scores"]["built_from_turn"] = -1
    meta["scores"]["season_id"] = None
    meta["schedule"]["built_from_turn_by_team"] = {}
    meta["schedule"]["season_id"] = None
    stats = cached["stats"]
    stats["leaders"] = None
    weekly = cached["weekly_news"]
    weekly["last_generated_week_start"] = None
    weekly["items"] = []
    playoff_news = cached["playoff_news"]
    playoff_news["series_game_counts"] = {}
    playoff_news["items"] = []
