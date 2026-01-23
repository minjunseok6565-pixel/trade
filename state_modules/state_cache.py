from __future__ import annotations

from typing import Any, Dict


def _ensure_cached_views_meta(cached: Dict[str, Any]) -> Dict[str, Any]:
    """cached_views 메타 정보가 없으면 기본값으로 생성한다."""
    meta = cached.setdefault("_meta", {})
    meta.setdefault("scores", {"built_from_turn": -1, "season_id": None})
    meta.setdefault("schedule", {"built_from_turn_by_team": {}, "season_id": None})
    return meta


def _mark_views_dirty(cached: Dict[str, Any]) -> None:
    """캐시된 뷰를 다시 계산하도록 무효화한다."""
    meta = _ensure_cached_views_meta(cached)
    meta["scores"]["built_from_turn"] = -1
    meta["schedule"]["built_from_turn_by_team"] = {}


def _reset_cached_views_for_new_season(cached: Dict[str, Any]) -> None:
    """정규시즌 누적이 새로 시작될 때 cached_views도 함께 초기화한다."""
    scores = cached.setdefault("scores", {})
    scores["latest_date"] = None
    scores["games"] = []
    schedule = cached.setdefault("schedule", {})
    schedule["teams"] = {}
    meta = _ensure_cached_views_meta(cached)
    meta["scores"]["built_from_turn"] = -1
    meta["scores"]["season_id"] = None
    meta["schedule"]["built_from_turn_by_team"] = {}
    meta["schedule"]["season_id"] = None
    stats = cached.setdefault("stats", {})
    stats["leaders"] = None
    weekly = cached.setdefault("weekly_news", {})
    weekly["last_generated_week_start"] = None
    weekly["items"] = []
    playoff_news = cached.setdefault("playoff_news", {})
    playoff_news.setdefault("series_game_counts", {})
    playoff_news["series_game_counts"] = {}
    playoff_news["items"] = []
