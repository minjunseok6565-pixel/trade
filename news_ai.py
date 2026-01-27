from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import google.generativeai as genai

from league_repo import LeagueRepo

from state import (
    export_workflow_state,
    get_db_path,
    get_cached_playoff_news_snapshot,
    get_cached_weekly_news_snapshot,
    get_current_date,
    get_league_context_snapshot,
    get_postseason_snapshot,
    set_cached_playoff_news_snapshot,
    set_cached_weekly_news_snapshot,
)
from team_utils import get_conference_standings

logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}

def _warn_limited(code: str, msg: str, *, limit: int = 5) -> None:
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1


def _extract_text_from_gemini_response(resp: Any) -> str:
    text = getattr(resp, "text", None)
    if text:
        return text

    try:
        parts = resp.candidates[0].content.parts
        texts = []
        for p in parts:
            t = getattr(p, "text", None)
            if t:
                texts.append(t)
        if texts:
            return "\n".join(texts)
    except (AttributeError, IndexError, TypeError):
        _warn_limited("GEMINI_RESPONSE_SHAPE_UNEXPECTED", f"resp_type={type(resp).__name__}", limit=3)
        pass

    return str(resp)


def _ensure_playoff_news_cache() -> Dict[str, Any]:
    playoff_news = get_cached_playoff_news_snapshot() or {"series_game_counts": {}, "items": []}
    playoff_news.setdefault("series_game_counts", {})
    playoff_news.setdefault("items", [])
    set_cached_playoff_news_snapshot(playoff_news)
    return playoff_news


def _playoff_round_label(round_name: Optional[str]) -> str:
    mapping = {
        "Conference Quarterfinals": "플레이오프 1라운드",
        "Conference Semifinals": "플레이오프 2라운드",
        "Conference Finals": "컨퍼런스 파이널",
        "NBA Finals": "NBA 파이널",
    }
    if not round_name:
        return "플레이오프"
    return mapping.get(round_name, round_name)


def _get_current_date() -> date:
    league_context = get_league_context_snapshot()
    cur = get_current_date() or league_context.get("season_start") or date.today().isoformat()
    try:
        return date.fromisoformat(cur)
    except (TypeError, ValueError) as e:
        _warn_limited("CURRENT_DATE_PARSE_FAILED", f"cur={cur!r} exc_type={type(e).__name__}", limit=3)
        return date.today()


def _iter_playoff_series(playoffs: Dict[str, Any]) -> List[Dict[str, Any]]:
    bracket = playoffs.get("bracket", {})
    series_list: List[Dict[str, Any]] = []

    for series in bracket.get("east", {}).get("quarterfinals", []) or []:
        if series:
            series_list.append(series)
    for series in bracket.get("west", {}).get("quarterfinals", []) or []:
        if series:
            series_list.append(series)

    for series in bracket.get("east", {}).get("semifinals", []) or []:
        if series:
            series_list.append(series)
    for series in bracket.get("west", {}).get("semifinals", []) or []:
        if series:
            series_list.append(series)

    east_final = bracket.get("east", {}).get("finals")
    if east_final:
        series_list.append(east_final)
    west_final = bracket.get("west", {}).get("finals")
    if west_final:
        series_list.append(west_final)

    finals = bracket.get("finals")
    if finals:
        series_list.append(finals)

    return series_list


def _series_key(series: Dict[str, Any]) -> str:
    return f"{series.get('round')}::{series.get('home_court')}::{series.get('road')}"


def _wins_through_game(series: Dict[str, Any], game_index: int) -> Dict[str, int]:
    wins: Dict[str, int] = {}
    for game in (series.get("games", []) or [])[: game_index + 1]:
        winner = game.get("winner")
        if not winner:
            continue
        wins[winner] = wins.get(winner, 0) + 1
    return wins


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _as_date(value: Any) -> Optional[date]:
    """Parse ISO date or ISO datetime-like strings into a date."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Accept YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS...
    if len(s) >= 10:
        s = s[:10]
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        _warn_limited("DATE_PARSE_FAILED", f"value={value!r}", limit=3)
        return None


def build_week_summary_context() -> str:
    current_date = _get_current_date()
    week_start = current_date - timedelta(days=6)
    snapshot = export_workflow_state()

    lines: List[str] = []
    lines.append(f"Current league date: {current_date.isoformat()}")
    lines.append(f"Coverage window: {week_start.isoformat()} ~ {current_date.isoformat()}")

    games = []
    for g in snapshot.get("games", []):
        try:
            g_date = date.fromisoformat(g.get("date"))
        except (TypeError, ValueError):
            _warn_limited("GAME_DATE_PARSE_FAILED", f"date={g.get('date')!r}", limit=3)
            continue
        if week_start <= g_date <= current_date:
            games.append(g)

    games_sorted = sorted(games, key=lambda x: x.get("date"))
    lines.append("\n[Games]")
    if not games_sorted:
        lines.append("No games played in this window.")
    else:
        for g in games_sorted:
            lines.append(
                f"{g.get('date')}: {g.get('home_team_id')} {g.get('home_score')} - "
                f"{g.get('away_team_id')} {g.get('away_score')}"
            )

    transactions: List[Dict[str, Any]] = []
    # Transactions are stored in SQLite (SSOT). The workflow snapshot excludes them by default.
    tx_rows: List[Dict[str, Any]] = []
    repo: Optional[LeagueRepo] = None
    db_path = get_db_path()
    try:
        repo = LeagueRepo(db_path)
        repo.init_db()
        tx_rows = repo.list_transactions(limit=500, since_date=week_start.isoformat())
    except Exception as e:
        _warn_limited("NEWS_TX_DB_FAILED", f"db_path={db_path!r} exc_type={type(e).__name__}", limit=3)
        tx_rows = []
    finally:
        try:
            if repo:
                repo.close()
        except Exception as e:
            _warn_limited("NEWS_TX_DB_CLOSE_FAILED", f"db_path={db_path!r} exc_type={type(e).__name__}", limit=1)
            pass

    for t in tx_rows:
        if not isinstance(t, dict):
            continue
        t_date = t.get("date") or t.get("created_at")
        t_d = _as_date(t_date)
        if not t_d:
            continue
        if week_start <= t_d <= current_date:
            transactions.append(t)

    # Present in chronological order
    transactions.sort(key=lambda x: str(x.get("date") or x.get("created_at") or ""))

    lines.append("\n[Transactions]")
    if not transactions:
        lines.append("No trades or transactions recorded.")
    else:
        for t in transactions:
            summary = t.get("summary") or t.get("title") or str(t)
            lines.append(f"{t.get('date', '')}: {summary}")

    standings = get_conference_standings()
    lines.append("\n[Top Teams]")
    for conf_key, teams in [("East", standings.get("east", [])), ("West", standings.get("west", []))]:
        top3 = teams[:3]
        if not top3:
            lines.append(f"{conf_key}: no games yet.")
            continue
        for t in top3:
            lines.append(
                f"{conf_key} #{t.get('rank')}: {t.get('team_id')} ({t.get('wins')}-{t.get('losses')})"
            )

    return "\n".join(lines)


def generate_weekly_news(api_key: str) -> List[Dict[str, Any]]:
    if not api_key:
        raise ValueError("apiKey is required")

    context = build_week_summary_context()
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3-pro-preview")

    prompt = (
        "You are an NBA beat writer. Summarize the past week into 3-6 news articles. "
        "Return ONLY a JSON array. Each item must have keys: "
        "title, summary, tags (array of strings), related_team_ids (array of team IDs), "
        "related_player_names (array of strings)."
        "Keep summaries concise (<=60 words)."
        "Context:\n" + context
    )

    resp = model.generate_content(prompt)
    raw_text = _extract_text_from_gemini_response(resp)

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 3:
            cleaned = parts[1].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        _warn_limited("WEEKLY_NEWS_JSON_DECODE_FAILED", f"text_preview={cleaned[:120]!r}", limit=3)
        return []

    if not isinstance(data, list):
        return []

    articles: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        articles.append(
            {
                "title": item.get("title"),
                "summary": item.get("summary"),
                "tags": item.get("tags") or [],
                "related_team_ids": item.get("related_team_ids") or [],
                "related_player_names": item.get("related_player_names") or [],
            }
        )

    return articles


def refresh_weekly_news(api_key: str) -> Dict[str, Any]:
    current_date = _get_current_date()
    week_key = _week_start(current_date).isoformat()
    cache = get_cached_weekly_news_snapshot() or {}

    if cache.get("last_generated_week_start") == week_key and cache.get("items"):
        return {"current_date": current_date.isoformat(), "items": cache.get("items", [])}

    items = generate_weekly_news(api_key)
    cache["last_generated_week_start"] = week_key
    cache["items"] = items
    set_cached_weekly_news_snapshot(cache)

    return {"current_date": current_date.isoformat(), "items": items}


# ---------------------------------------------------------------------------
# 플레이오프 모드 뉴스 (각 경기마다 갱신)
# ---------------------------------------------------------------------------


def _build_playoff_game_article(series: Dict[str, Any], game_index: int) -> Optional[Dict[str, Any]]:
    games = series.get("games") or []
    if game_index >= len(games):
        return None

    game = games[game_index]
    home_id = series.get("home_court")
    road_id = series.get("road")
    winner = game.get("winner")
    if not home_id or not road_id or not winner:
        return None

    loser = road_id if winner == home_id else home_id
    wins = _wins_through_game(series, game_index)
    home_wins = wins.get(home_id, 0)
    road_wins = wins.get(road_id, 0)
    series_score = f"{home_wins}-{road_wins}"

    round_label = _playoff_round_label(series.get("round"))
    game_number = game_index + 1
    score_str = f"{game.get('home_score', 0)}-{game.get('away_score', 0)}"

    title = f"{round_label} G{game_number}: {winner} 승리"
    summary = (
        f"{round_label}에서 {winner}가 {loser}을 상대로 {score_str}로 승리하며 "
        f"시리즈를 {series_score}로 만들었다."
    )

    return {
        "title": title,
        "summary": summary,
        "tags": ["playoffs", "game_result", series.get("round")],
        "related_team_ids": [home_id, road_id],
        "related_player_names": [],
    }


def refresh_playoff_news() -> Dict[str, Any]:
    postseason = get_postseason_snapshot()
    playoffs = postseason.get("playoffs")
    if not playoffs:
        raise ValueError("플레이오프 진행 중이 아닙니다.")

    cache = _ensure_playoff_news_cache()
    series_counts = cache.setdefault("series_game_counts", {})
    items = cache.setdefault("items", [])

    new_items: List[Dict[str, Any]] = []
    for series in _iter_playoff_series(playoffs):
        if not series:
            continue
        key = _series_key(series)
        prev_count = series_counts.get(key, 0)
        games = series.get("games") or []
        for idx in range(prev_count, len(games)):
            article = _build_playoff_game_article(series, idx)
            if article:
                items.append(article)
                new_items.append(article)
        series_counts[key] = len(games)

    cache["items"] = items
    cache["series_game_counts"] = series_counts
    set_cached_playoff_news_snapshot(cache)

    return {"items": items, "new_items": new_items}
