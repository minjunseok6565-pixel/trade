from __future__ import annotations

import os
import re
import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from config import (
    ALL_TEAM_IDS,
    TEAM_TO_CONF_DIV,
    SEASON_START_MONTH,
    SEASON_START_DAY,
    SEASON_LENGTH_DAYS,
    MAX_GAMES_PER_DAY,
    DIVISIONS,
    INITIAL_SEASON_YEAR,
    CAP_BASE_SEASON_YEAR,
    CAP_BASE_SALARY_CAP,
    CAP_BASE_FIRST_APRON,
    CAP_BASE_SECOND_APRON,
    CAP_ANNUAL_GROWTH_RATE,
    CAP_ROUND_UNIT,
)

DEFAULT_TRADE_RULES: Dict[str, Any] = {
    "trade_deadline": None,
    "salary_cap": 0.0,
    "first_apron": 0.0,
    "second_apron": 0.0,
    "cap_auto_update": True,
    "cap_base_season_year": CAP_BASE_SEASON_YEAR,
    "cap_base_salary_cap": CAP_BASE_SALARY_CAP,
    "cap_base_first_apron": CAP_BASE_FIRST_APRON,
    "cap_base_second_apron": CAP_BASE_SECOND_APRON,
    "cap_annual_growth_rate": CAP_ANNUAL_GROWTH_RATE,
    "cap_round_unit": CAP_ROUND_UNIT,
    "match_small_out_max": 7_500_000,
    "match_mid_out_max": 29_000_000,
    "match_mid_add": 7_500_000,
    "match_buffer": 250_000,
    "first_apron_mult": 1.10,
    "second_apron_mult": 1.00,
    "new_fa_sign_ban_days": 90,
    "aggregation_ban_days": 60,
    "max_pick_years_ahead": 7,
    "stepien_lookahead": 7,
}

# -------------------------------------------------------------------------
# 1. 전역 GAME_STATE 및 스케줄/리그 상태 유틸
# -------------------------------------------------------------------------
GAME_STATE: Dict[str, Any] = {
    "schema_version": "2.0",
    "turn": 0,
    "games": [],  # 각 경기의 메타 데이터
    "player_stats": {},  # player_id -> 시즌 누적 스탯
    "team_stats": {},  # team_id -> 시즌 누적 팀 스탯(가공 팀 박스)
    "game_results": {},  # game_id -> 매치엔진 원본 결과(신규 엔진 기준)
    "active_season_id": None,  # 현재 누적이 쌓이는 season_id (예: "2025-26")
    "season_history": {},  # season_id -> {games, player_stats, team_stats, game_results}
    "_migrations": {},
    "cached_views": {
        "scores": {
            "latest_date": None,
            "games": []  # 최근 경기일자 기준 경기 리스트
        },
        "schedule": {
            "teams": {}  # team_id -> {past_games: [], upcoming_games: []}
        },
        "_meta": {
            "scores": {"built_from_turn": -1, "season_id": None},
            "schedule": {"built_from_turn_by_team": {}, "season_id": None},
        },
        "stats": {
            "leaders": None,
        },
        "weekly_news": {
            "last_generated_week_start": None,
            "items": [],
        },
        "playoff_news": {
            "series_game_counts": {},
            "items": [],
        },
    },
    "postseason": {},  # 플레이-인/플레이오프 시뮬레이션 결과 캐시
    "league": {
        "season_year": None,
        "draft_year": None,  # 드래프트 연도(예: 2025-26 시즌이면 2026)
        "season_start": None,  # YYYY-MM-DD
        "current_date": None,  # 마지막으로 리그를 진행한 인게임 날짜
        "master_schedule": {
            "games": [],   # 전체 리그 경기 리스트
            "by_team": {},  # team_id -> [game_id, ...]
            "by_date": {},  # date_str -> [game_id, ...]
        },
        "trade_rules": {**DEFAULT_TRADE_RULES},
        "last_gm_tick_date": None,  # 마지막 AI GM 트레이드 시도 날짜
    },
    "teams": {},      # 팀 성향 / 메타 정보
    "players": {},    # 선수 메타 정보
    "transactions": [],  # 트레이드 등 기록
    "trade_agreements": {},  # deal_id -> committed deal data
    "negotiations": {},  # session_id -> negotiation sessions
    "draft_picks": {},  # Phase 3 later
    "asset_locks": {},  # asset_key -> {deal_id, expires_at}
    "swap_rights": {},
    "fixed_assets": {},
    "trade_market": {
        "last_tick_date": None,
        "listings": {},
        "threads": {},
        "cooldowns": {},
        "events": [],
    },
    "trade_memory": {
        "relationships": {},
    },
    "gm_profiles": {},
}


def get_current_date() -> Optional[str]:
    """Return the league's current in-game date, keeping legacy mirrors in sync."""
    league = _ensure_league_state()
    current = league.get("current_date")
    legacy_current = GAME_STATE.get("current_date")

    if current:
        GAME_STATE["current_date"] = current
        return current

    if legacy_current:
        league["current_date"] = legacy_current
        return legacy_current

    return None


def get_current_date_as_date() -> date:
    """Return the league's current in-game date as a date object."""
    current = get_current_date()
    if current:
        try:
            return date.fromisoformat(str(current))
        except ValueError:
            pass

    league = _ensure_league_state()
    season_start = league.get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass

    return date.today()


def set_current_date(date_str: Optional[str]) -> None:
    """Update the league's current date and mirror it at the legacy location."""
    league = _ensure_league_state()
    league["current_date"] = date_str
    if date_str is None:
        GAME_STATE.pop("current_date", None)
    else:
        GAME_STATE["current_date"] = date_str


def _ensure_schedule_team(team_id: str) -> Dict[str, Any]:
    """GAME_STATE.cached_views.schedule에 팀 엔트리가 없으면 생성."""
    schedule = GAME_STATE["cached_views"]["schedule"]
    teams = schedule.setdefault("teams", {})
    if team_id not in teams:
        teams[team_id] = {
            "past_games": [],
            "upcoming_games": [],
        }
    return teams[team_id]


def _ensure_cached_views_meta() -> Dict[str, Any]:
    """cached_views 메타 정보가 없으면 기본값으로 생성한다."""
    cached = GAME_STATE.setdefault("cached_views", {})
    meta = cached.setdefault("_meta", {})
    meta.setdefault("scores", {"built_from_turn": -1, "season_id": None})
    meta.setdefault("schedule", {"built_from_turn_by_team": {}, "season_id": None})
    return meta


def _mark_views_dirty() -> None:
    """캐시된 뷰를 다시 계산하도록 무효화한다."""
    meta = _ensure_cached_views_meta()
    meta["scores"]["built_from_turn"] = -1
    meta["schedule"]["built_from_turn_by_team"] = {}


def normalize_player_ids(game_state: dict, *, allow_legacy_numeric: bool = True) -> dict:
    """
    Normalize GAME_STATE identifiers so *player_id is always a string* and unique.

    Why:
    - Prevents "12"(str) vs 12(int) from becoming two different players.
    - Makes it safe to join boxscore/trades/contracts/roster by the same key.

    Policy:
    - Keys of GAME_STATE["players"] are canonical player_id strings.
    - player_meta["player_id"] must exist and must match the dict key.
    - free_agents (if present) is a list[str] of canonical player_id.
    """
    try:
        from schema import normalize_player_id, normalize_team_id
    except Exception as e:
        raise ImportError(f"schema.py is required for ID normalization: {e}")

    report = {
        "converted_player_keys_count": 0,
        "converted_free_agents_count": 0,
        "conflicts_count": 0,
        "invalid_keys_count": 0,
    }
    players = game_state.get("players")
    if not isinstance(players, dict):
        return report

    new_players: Dict[str, Any] = {}
    key_sources: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    invalid: List[Any] = []

    for raw_key, raw_meta in players.items():
        try:
            pid = str(
                normalize_player_id(
                    raw_key, strict=False, allow_legacy_numeric=allow_legacy_numeric
                )
            )
        except Exception:
            invalid.append(raw_key)
            continue

        if pid != str(raw_key):
            report["converted_player_keys_count"] += 1

        meta = raw_meta if isinstance(raw_meta, dict) else {"value": raw_meta}
        if "player_id" not in meta:
            meta["player_id"] = pid
        else:
            try:
                meta_pid = str(
                    normalize_player_id(
                        meta.get("player_id"),
                        strict=False,
                        allow_legacy_numeric=allow_legacy_numeric,
                    )
                )
            except Exception:
                raise ValueError(
                    f"GAME_STATE.players[{raw_key!r}].player_id is invalid: {meta.get('player_id')!r}"
                )
            if meta_pid != pid:
                raise ValueError(
                    "Inconsistent player_id: dict key and meta disagree: "
                    f"key={raw_key!r} -> {pid}, meta.player_id={meta.get('player_id')!r} -> {meta_pid}"
                )
            meta["player_id"] = meta_pid

        if "team_id" in meta and meta.get("team_id") not in (None, ""):
            try:
                meta["team_id"] = str(normalize_team_id(meta["team_id"], strict=False))
            except Exception:
                meta.setdefault("_warnings", []).append(
                    f"team_id not normalized: {meta.get('team_id')!r}"
                )

        if pid in new_players:
            conflicts.append(
                {"player_id": pid, "first_key": key_sources[pid], "dup_key": raw_key}
            )
            continue

        new_players[pid] = meta
        key_sources[pid] = raw_key

    if conflicts:
        report["conflicts_count"] = len(conflicts)
        report["conflicts"] = conflicts[:10]
        raise ValueError(
            "Duplicate player_id keys detected while normalizing GAME_STATE['players'].\n"
            f"Examples: {conflicts[:5]!r}\n"
            "Fix: migrate all IDs to a single canonical player_id (string) and remove duplicates."
        )

    if invalid:
        report["invalid_keys_count"] = len(invalid)
        report["invalid_keys"] = invalid[:10]
        raise ValueError(
            "Invalid player keys detected in GAME_STATE['players'] while normalizing.\n"
            f"Examples: {invalid[:10]!r}"
        )

    game_state["players"] = new_players

    if "free_agents" in game_state:
        fa = game_state.get("free_agents")
        if not isinstance(fa, list):
            game_state["free_agents"] = []
        else:
            out: List[str] = []
            seen: set[str] = set()
            for item in fa:
                pid = str(
                    normalize_player_id(
                        item, strict=False, allow_legacy_numeric=allow_legacy_numeric
                    )
                )
                if pid != str(item):
                    report["converted_free_agents_count"] += 1
                if pid in seen:
                    continue
                out.append(pid)
                seen.add(pid)
            game_state["free_agents"] = out

    debug = game_state.setdefault("debug", {})
    debug.setdefault("normalization", []).append(report)
    return report


def normalize_player_keys(game_state: dict) -> dict:
    """Backward-compatible alias."""
    return normalize_player_ids(game_state)


def _backfill_ingest_turns_once() -> None:
    """Backfill missing ingest_turn values across stored games."""
    all_games: List[Dict[str, Any]] = []

    regular_games = GAME_STATE.get("games", [])
    if isinstance(regular_games, list):
        all_games.extend([g for g in regular_games if isinstance(g, dict)])

    postseason = GAME_STATE.get("postseason", {})
    if isinstance(postseason, dict):
        for container in postseason.values():
            if not isinstance(container, dict):
                continue
            games = container.get("games", [])
            if isinstance(games, list):
                all_games.extend([g for g in games if isinstance(g, dict)])

    season_history = GAME_STATE.get("season_history", {})
    if isinstance(season_history, dict):
        for record in season_history.values():
            if not isinstance(record, dict):
                continue
            games = record.get("games", [])
            if isinstance(games, list):
                all_games.extend([g for g in games if isinstance(g, dict)])

    used_turns = {
        int(game["ingest_turn"])
        for game in all_games
        if isinstance(game.get("ingest_turn"), int)
        and not isinstance(game.get("ingest_turn"), bool)
        and int(game.get("ingest_turn")) > 0
    }

    missing_games = [
        game
        for game in all_games
        if not (
            isinstance(game.get("ingest_turn"), int)
            and not isinstance(game.get("ingest_turn"), bool)
            and int(game.get("ingest_turn")) > 0
        )
    ]

    def _missing_sort_key(game: Dict[str, Any]) -> tuple[date, str, str, str]:
        raw_date = game.get("date")
        try:
            parsed_date = date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            parsed_date = date.min
        return (
            parsed_date,
            str(game.get("season_id") or ""),
            str(game.get("phase") or ""),
            str(game.get("game_id") or ""),
        )

    missing_games.sort(key=_missing_sort_key)

    next_turn = 1
    for game in missing_games:
        while next_turn in used_turns:
            next_turn += 1
        game["ingest_turn"] = next_turn
        used_turns.add(next_turn)
        next_turn += 1

    max_turn = max(used_turns) if used_turns else 0
    if int(GAME_STATE.get("turn", 0) or 0) < max_turn:
        GAME_STATE["turn"] = max_turn


def _ensure_ingest_turn_backfilled() -> None:
    """Ensure ingest_turn backfill runs once per GAME_STATE instance."""
    migrations = GAME_STATE.setdefault("_migrations", {})
    if migrations.get("ingest_turn_backfilled") is True:
        return
    _backfill_ingest_turns_once()
    migrations["ingest_turn_backfilled"] = True


def _ensure_league_state() -> Dict[str, Any]:
    """GAME_STATE 안에 league 상태 블록을 보장한다."""
    league = GAME_STATE.setdefault("league", {})
    master_schedule = league.setdefault("master_schedule", {})
    master_schedule.setdefault("games", [])
    master_schedule.setdefault("by_team", {})
    master_schedule.setdefault("by_date", {})
    master_schedule.setdefault("by_id", {})
    trade_rules = league.setdefault("trade_rules", {})
    for key, value in DEFAULT_TRADE_RULES.items():
        trade_rules.setdefault(key, value)
    league.setdefault("season_year", None)
    league.setdefault("draft_year", None)
    league.setdefault("season_start", None)
    league.setdefault("current_date", None)
    db_path = league.get("db_path") or os.environ.get("LEAGUE_DB_PATH") or "league.db"
    league["db_path"] = db_path
    league.setdefault("last_gm_tick_date", None)
    from league_repo import LeagueRepo

    with LeagueRepo(db_path) as repo:
        repo.init_db()
    season_year = league.get("season_year")
    salary_cap = trade_rules.get("salary_cap")
    if season_year:
        try:
            salary_cap_value = float(salary_cap or 0)
        except (TypeError, ValueError):
            salary_cap_value = 0
        if salary_cap_value <= 0:
            # Fix legacy saves so SalaryMatchingRule doesn't treat cap/aprons as zero.
            _apply_cap_model_for_season(league, int(season_year))
    _ensure_trade_state()
    from contracts.store import ensure_contract_state

    ensure_contract_state(GAME_STATE)
    from team_utils import _init_players_and_teams_if_needed

    _init_players_and_teams_if_needed()
    # Normalize player IDs to canonical strings (prevents "12" vs 12 splits).
    normalize_player_ids(GAME_STATE)

    # Contracts bootstrap: prefer DB-based bootstrap if available (Step 2),
    # fall back to legacy Excel bootstrap for older saves.
    try:
        from contracts.bootstrap import bootstrap_contracts_from_repo as _bootstrap_contracts
    except ImportError:
        from contracts.bootstrap import bootstrap_contracts_from_roster_excel as _bootstrap_contracts
 

    _bootstrap_contracts(GAME_STATE, overwrite=False)
    with LeagueRepo(db_path) as repo:
        repo.validate_integrity()
    _ensure_ingest_turn_backfilled()
    return league

# -------------------------------------------------------------------------
# 1B. 인터페이스 계약(Contract) 검증 유틸
#  - validate_v2_game_result: ingest_game_result()가 기대하는 v2 스키마
#  - validate_master_schedule_entry: master_schedule.games[*] 최소 엔트리 스키마
# -------------------------------------------------------------------------

_ALLOWED_SCHEDULE_STATUSES = {"scheduled", "final", "in_progress", "canceled"}


def validate_master_schedule_entry(entry: Dict[str, Any], *, path: str = "master_schedule.entry") -> None:
    """
    master_schedule.games[*]에서 실제로 "사용되는 필드만" 최소 계약으로 고정한다.

    Required:
      - game_id: str (non-empty)
      - home_team_id: str (non-empty)
      - away_team_id: str (non-empty)
      - status: str (allowed set)

    Optional (if present, must be correct type):
      - date: str (ISO-like recommended)
      - season_id: str
      - phase: str
      - home_score/away_score: int|None
      - home_tactics/away_tactics/tactics: dict|None  (프로젝트별로 사용하는 키가 달라도 안전하게 수용)
    """
    if not isinstance(entry, dict):
        raise ValueError(f"MasterScheduleEntry invalid: '{path}' must be a dict")

    for k in ("game_id", "home_team_id", "away_team_id", "status"):
        if k not in entry:
            raise ValueError(f"MasterScheduleEntry invalid: missing {path}.{k}")

    game_id = entry.get("game_id")
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError(f"MasterScheduleEntry invalid: {path}.game_id must be a non-empty string")

    for k in ("home_team_id", "away_team_id"):
        v = entry.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{k} must be a non-empty string")

    status = entry.get("status")
    if not isinstance(status, str) or status not in _ALLOWED_SCHEDULE_STATUSES:
        raise ValueError(
            f"MasterScheduleEntry invalid: {path}.status must be one of {sorted(_ALLOWED_SCHEDULE_STATUSES)}"
        )

    # Optional: tactics payload(s)
    for tk in ("tactics", "home_tactics", "away_tactics"):
        if tk in entry and entry[tk] is not None and not isinstance(entry[tk], dict):
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{tk} must be a dict if present")

    # Optional: date (string)
    if "date" in entry and entry["date"] is not None and not isinstance(entry["date"], str):
        raise ValueError(f"MasterScheduleEntry invalid: {path}.date must be a string if present")

    # Optional: scores
    for sk in ("home_score", "away_score"):
        if sk in entry and entry[sk] is not None and not isinstance(entry[sk], int):
            raise ValueError(f"MasterScheduleEntry invalid: {path}.{sk} must be int or None if present")


def validate_v2_game_result(game_result: Dict[str, Any]) -> None:
    """Public validator: raises ValueError if the v2 contract is violated."""
    _validate_game_result_v2(game_result)


def _ensure_master_schedule_indices() -> None:
    """Legacy state를 위해 master_schedule의 by_id 인덱스를 보장한다."""
    league = _ensure_league_state()
    master_schedule = league.get("master_schedule") or {}
    games = master_schedule.get("games") or []
    # Contract check: master_schedule entries must satisfy the minimal schema.
    for i, g in enumerate(games):
        validate_master_schedule_entry(g, path=f"master_schedule.games[{i}]")
    by_id = master_schedule.get("by_id")
    if not isinstance(by_id, dict) or len(by_id) != len(games):
        master_schedule["by_id"] = {
            g.get("game_id"): g for g in games if g.get("game_id")
        }


def _apply_cap_model_for_season(league: Dict[str, Any], season_year: int) -> None:
    """Apply the season-specific cap/apron values to trade rules."""
    trade_rules = league.setdefault("trade_rules", {})
    if trade_rules.get("cap_auto_update") is False:
        return
    try:
        base_season_year = int(
            trade_rules.get("cap_base_season_year", CAP_BASE_SEASON_YEAR)
        )
    except (TypeError, ValueError):
        base_season_year = CAP_BASE_SEASON_YEAR
    try:
        base_salary_cap = float(
            trade_rules.get("cap_base_salary_cap", CAP_BASE_SALARY_CAP)
        )
    except (TypeError, ValueError):
        base_salary_cap = float(CAP_BASE_SALARY_CAP)
    try:
        base_first_apron = float(
            trade_rules.get("cap_base_first_apron", CAP_BASE_FIRST_APRON)
        )
    except (TypeError, ValueError):
        base_first_apron = float(CAP_BASE_FIRST_APRON)
    try:
        base_second_apron = float(
            trade_rules.get("cap_base_second_apron", CAP_BASE_SECOND_APRON)
        )
    except (TypeError, ValueError):
        base_second_apron = float(CAP_BASE_SECOND_APRON)
    try:
        annual_growth_rate = float(
            trade_rules.get("cap_annual_growth_rate", CAP_ANNUAL_GROWTH_RATE)
        )
    except (TypeError, ValueError):
        annual_growth_rate = float(CAP_ANNUAL_GROWTH_RATE)
    try:
        round_unit = int(trade_rules.get("cap_round_unit", CAP_ROUND_UNIT) or 1)
    except (TypeError, ValueError):
        round_unit = CAP_ROUND_UNIT
    if round_unit <= 0:
        round_unit = CAP_ROUND_UNIT or 1

    years_passed = season_year - base_season_year
    multiplier = (1.0 + annual_growth_rate) ** years_passed

    def _round_to_unit(value: float) -> int:
        return int(round(value / round_unit) * round_unit)

    salary_cap = _round_to_unit(base_salary_cap * multiplier)
    first_apron = _round_to_unit(base_first_apron * multiplier)
    second_apron = _round_to_unit(base_second_apron * multiplier)

    if first_apron < salary_cap:
        first_apron = salary_cap
    if second_apron < first_apron:
        second_apron = first_apron

    trade_rules["salary_cap"] = salary_cap
    trade_rules["first_apron"] = first_apron
    trade_rules["second_apron"] = second_apron


_DEFAULT_TRADE_MARKET: Dict[str, Any] = {
    "last_tick_date": None,
    "listings": {},
    "threads": {},
    "cooldowns": {},
    "events": [],
}

_DEFAULT_TRADE_MEMORY: Dict[str, Any] = {
    "relationships": {},
}


def _ensure_trade_state() -> None:
    """트레이드 관련 GAME_STATE 키를 보장한다."""
    GAME_STATE.setdefault("trade_market", dict(_DEFAULT_TRADE_MARKET))
    GAME_STATE.setdefault("trade_memory", dict(_DEFAULT_TRADE_MEMORY))
    GAME_STATE.setdefault("gm_profiles", {})

    GAME_STATE.setdefault("trade_agreements", {})
    GAME_STATE.setdefault("negotiations", {})
    GAME_STATE.setdefault("draft_picks", {})
    GAME_STATE.setdefault("asset_locks", {})
    GAME_STATE.setdefault("swap_rights", {})
    GAME_STATE.setdefault("fixed_assets", {})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reset_cached_views_for_new_season() -> None:
    """정규시즌 누적이 새로 시작될 때 cached_views도 함께 초기화한다."""
    cached = GAME_STATE.setdefault("cached_views", {})
    scores = cached.setdefault("scores", {})
    scores["latest_date"] = None
    scores["games"] = []
    schedule = cached.setdefault("schedule", {})
    schedule["teams"] = {}
    meta = _ensure_cached_views_meta()
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


def _season_id_from_year(season_year: int) -> str:
    """시즌 시작 연도(int) -> season_id 문자열로 변환. 예: 2025 -> '2025-26'"""
    yy = str(int(season_year) + 1)[-2:]
    return f"{int(season_year)}-{yy}"


def _archive_and_reset_season_accumulators(
    previous_season_id: Optional[str],
    next_season_id: Optional[str],
) -> None:
    """시즌이 바뀔 때 정규시즌 누적 데이터를 history로 옮기고 초기화한다."""
    if previous_season_id:
        history = GAME_STATE.setdefault("season_history", {})
        history[str(previous_season_id)] = {
            "games": GAME_STATE.get("games", []),
            "player_stats": GAME_STATE.get("player_stats", {}),
            "team_stats": GAME_STATE.get("team_stats", {}),
            "game_results": GAME_STATE.get("game_results", {}),
        }

    GAME_STATE["games"] = []
    GAME_STATE["player_stats"] = {}
    GAME_STATE["team_stats"] = {}
    GAME_STATE["game_results"] = {}
    GAME_STATE["postseason"] = {}

    GAME_STATE["active_season_id"] = next_season_id
    _reset_cached_views_for_new_season()
    _ensure_trade_state()


def _ensure_active_season_id(season_id: str) -> None:
    """리그 시즌과 누적 시즌이 불일치하면 새 시즌 누적으로 전환한다."""
    if not season_id:
        return
    active = GAME_STATE.get("active_season_id")
    if active is None:
        GAME_STATE["active_season_id"] = str(season_id)
        _ensure_trade_state()
        return
    if str(active) != str(season_id):
        _archive_and_reset_season_accumulators(str(active), str(season_id))

def _build_master_schedule(season_year: int) -> None:
    """30개 팀 전체에 대한 마스터 스케줄(정규시즌)을 생성한다.

    - 실제 NBA 규칙을 근사하여 **항상 1230경기, 팀당 82경기**가 되도록
      경기 수를 결정한다.
    - 같은 디비전: 4경기
    - 같은 컨퍼런스 다른 디비전: 한 팀당 6개 팀과는 4경기, 4개 팀과는 3경기
      (규칙적인 회전 매핑으로 결정)
    - 다른 컨퍼런스: 2경기
    - 홈/원정은 누적 홈 경기 수를 고려해 최대한 41/41에 가깝게 분배한다.
    - 시즌 기간(SEASON_LENGTH_DAYS) 동안 날짜를 랜덤 배정하되
      * 하루 최대 MAX_GAMES_PER_DAY 경기
      * 한 팀은 하루에 최대 1경기
    """
    league = _ensure_league_state()
    from trades.picks import init_draft_picks_if_needed

    # season_year는 "시즌 시작 연도" (예: 2025-26 시즌이면 2025)
    # draft_year는 "드래프트 연도" (예: 2025-26 시즌이면 2026)
    # 픽 생성/Stepien/7년 룰은 draft_year를 기준으로 맞추기 위해 미리 저장해 둔다.
    previous_season_year = league.get("season_year")
    league["season_year"] = season_year
    league["draft_year"] = season_year + 1
    _apply_cap_model_for_season(league, season_year)

    # Stepien 룰은 (year, year+1) 쌍을 검사하기 때문에,
    # lookahead=N이면 draft_year+N+1까지 "픽 데이터가 존재"해야 데이터 결측으로 인한 오판을 피할 수 있다.
    trade_rules = league.get("trade_rules") or {}
    try:
        max_pick_years_ahead = int(trade_rules.get("max_pick_years_ahead") or 7)
    except (TypeError, ValueError):
        max_pick_years_ahead = 7
    try:
        stepien_lookahead = int(trade_rules.get("stepien_lookahead") or 7)
    except (TypeError, ValueError):
        stepien_lookahead = 7

    years_ahead = max(max_pick_years_ahead, stepien_lookahead + 1)
    init_draft_picks_if_needed(
        GAME_STATE, league["draft_year"], list(ALL_TEAM_IDS), years_ahead=years_ahead
    )

    season_start = date(season_year, SEASON_START_MONTH, SEASON_START_DAY)
    teams = list(ALL_TEAM_IDS)
    season_id = _season_id_from_year(season_year)
    phase = "regular"

    # 팀별 컨퍼런스/디비전 정보 캐시
    team_info: Dict[str, Dict[str, Optional[str]]] = {}
    for tid in teams:
        info = TEAM_TO_CONF_DIV.get(tid, {"conference": None, "division": None})
        team_info[tid] = {
            "conference": info.get("conference"),
            "division": info.get("division"),
        }

    # 컨퍼런스 내 다른 디비전 4경기 매칭을 결정하는 헬퍼 (5x5 회전 매핑)
    def _four_game_pairs_for_conf(conf_name: str) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        conf_divs = DIVISIONS.get(conf_name, {})
        div_list = list(conf_divs.values())
        if len(div_list) < 2:
            return pairs

        for i in range(len(div_list)):
            for j in range(i + 1, len(div_list)):
                a_div = div_list[i]
                b_div = div_list[j]
                if not a_div or not b_div:
                    continue
                for idx, a_team in enumerate(a_div):
                    for delta in range(3):  # 각 팀이 상대 디비전 팀 3명에게 4경기 배정
                        b_team = b_div[(idx + delta) % len(b_div)]
                        pair = tuple(sorted((a_team, b_team)))
                        pairs.add(pair)
        return pairs

    four_game_pairs_east = _four_game_pairs_for_conf("East")
    four_game_pairs_west = _four_game_pairs_for_conf("West")
    four_game_pairs = four_game_pairs_east | four_game_pairs_west

    # 1) 팀 쌍별로 경기 수 결정 + 홈/원정 분배
    pair_games: List[Dict[str, Any]] = []
    home_counts: Dict[str, int] = {tid: 0 for tid in teams}

    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            t1 = teams[i]
            t2 = teams[j]
            info1 = team_info[t1]
            info2 = team_info[t2]

            conf1, div1 = info1["conference"], info1["division"]
            conf2, div2 = info2["conference"], info2["division"]

            if conf1 is None or conf2 is None:
                num_games = 2
            elif conf1 != conf2:
                num_games = 2  # 다른 컨퍼런스는 2경기 고정
            elif div1 == div2:
                num_games = 4  # 같은 디비전
            else:
                # 같은 컨퍼런스 다른 디비전
                pair_key = tuple(sorted((t1, t2)))
                num_games = 4 if pair_key in four_game_pairs else 3

            # 홈/원정 분배 (3경기일 때는 현재 홈 수가 적은 팀에 추가 배정)
            home_for_t1 = num_games // 2
            home_for_t2 = num_games // 2
            if num_games % 2 == 1:
                if home_counts[t1] <= home_counts[t2]:
                    home_for_t1 += 1
                else:
                    home_for_t2 += 1

            for _ in range(home_for_t1):
                pair_games.append({
                    "home_team_id": t1,
                    "away_team_id": t2,
                })
            for _ in range(home_for_t2):
                pair_games.append({
                    "home_team_id": t2,
                    "away_team_id": t1,
                })

            home_counts[t1] += home_for_t1
            home_counts[t2] += home_for_t2

    # 2) 날짜 배정
    random.shuffle(pair_games)

    by_date: Dict[str, List[str]] = {}
    teams_per_date: Dict[str, set] = {}
    scheduled_games: List[Dict[str, Any]] = []

    for game in pair_games:
        home_id = game["home_team_id"]
        away_id = game["away_team_id"]

        assigned = False
        for _ in range(100):
            day_index = random.randint(0, SEASON_LENGTH_DAYS - 1)
            game_date = season_start + timedelta(days=day_index)
            date_str = game_date.isoformat()

            teams_today = teams_per_date.setdefault(date_str, set())
            games_today = by_date.setdefault(date_str, [])

            if len(games_today) >= MAX_GAMES_PER_DAY:
                continue
            if home_id in teams_today or away_id in teams_today:
                continue

            teams_today.add(home_id)
            teams_today.add(away_id)

            game_id = f"{date_str}_{home_id}_{away_id}"
            scheduled_games.append({
                "game_id": game_id,
                "date": date_str,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "status": "scheduled",
                "home_score": None,
                "away_score": None,
                "season_id": season_id,
                "phase": phase,
            })
            games_today.append(game_id)
            assigned = True
            break

        if not assigned:
            day_index = random.randint(0, SEASON_LENGTH_DAYS - 1)
            game_date = season_start + timedelta(days=day_index)
            date_str = game_date.isoformat()
            teams_today = teams_per_date.setdefault(date_str, set())
            games_today = by_date.setdefault(date_str, [])
            teams_today.add(home_id)
            teams_today.add(away_id)
            game_id = f"{date_str}_{home_id}_{away_id}"
            scheduled_games.append({
                "game_id": game_id,
                "date": date_str,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "status": "scheduled",
                "home_score": None,
                "away_score": None,
                "season_id": season_id,
                "phase": phase,
            })
            games_today.append(game_id)

    # 3) by_team 인덱스 생성
    by_team: Dict[str, List[str]] = {tid: [] for tid in teams}
    for g in scheduled_games:
        by_team[g["home_team_id"]].append(g["game_id"])
        by_team[g["away_team_id"]].append(g["game_id"])

    master_schedule = league["master_schedule"]
    master_schedule["games"] = scheduled_games
    master_schedule["by_team"] = by_team
    master_schedule["by_date"] = by_date
    master_schedule["by_id"] = {g["game_id"]: g for g in scheduled_games}

    league["season_year"] = season_year
    league["draft_year"] = season_year + 1
    league["season_start"] = season_start.isoformat()
    trade_deadline_date = date(season_year + 1, 2, 5)
    league["trade_rules"]["trade_deadline"] = trade_deadline_date.isoformat()
    set_current_date(None)
    league["last_gm_tick_date"] = None
    try:
        previous_season = int(previous_season_year or 0)
    except (TypeError, ValueError):
        previous_season = 0
    try:
        next_season = int(season_year or 0)
    except (TypeError, ValueError):
        next_season = 0
    # 누적 스탯(정규시즌)은 시즌 단위로 끊는다.
    # - 시즌이 바뀌면 기존 누적은 history로 보관하고, 새 시즌 누적을 새로 시작한다.
    if previous_season and next_season and previous_season != next_season:
        from contracts.offseason import process_offseason

        process_offseason(GAME_STATE, previous_season, next_season)
        _archive_and_reset_season_accumulators(_season_id_from_year(previous_season), _season_id_from_year(next_season))
    else:
        _ensure_active_season_id(_season_id_from_year(int(next_season or season_year)))

def initialize_master_schedule_if_needed() -> None:
    """master_schedule이 비어 있으면 현재 연도를 기준으로 한 번 생성한다."""
    league = _ensure_league_state()
    master_schedule = league["master_schedule"]
    if master_schedule.get("games"):
        _ensure_master_schedule_indices()
        return

    # season_year는 "시즌 시작 연도" (예: 2025-26 시즌이면 2025)
    season_year = INITIAL_SEASON_YEAR
    _build_master_schedule(season_year)
    _ensure_master_schedule_indices()


def _mark_master_schedule_game_final(
    game_id: str,
    game_date_str: str,
    home_id: str,
    away_id: str,
    home_score: int,
    away_score: int,
) -> None:
    """마스터 스케줄에 동일한 game_id가 있으면 결과를 반영한다."""
    league = _ensure_league_state()
    master_schedule = league.setdefault("master_schedule", {})
    games = master_schedule.get("games") or []
    by_id = master_schedule.setdefault("by_id", {})
    if not isinstance(by_id, dict):
        by_id = {}
        master_schedule["by_id"] = by_id
    entry = by_id.get(game_id)
    if entry:
        entry["status"] = "final"
        entry["date"] = game_date_str
        entry["home_score"] = home_score
        entry["away_score"] = away_score
        return

    for g in games:
        if g.get("game_id") == game_id:
            g["status"] = "final"
            g["date"] = game_date_str
            g["home_score"] = home_score
            g["away_score"] = away_score
            by_id[game_id] = g
            return


# -------------------------------------------------------------------------
# 2. 경기 결과(v2 스키마)를 상태에 반영 / STATE 업데이트 유틸
# -------------------------------------------------------------------------

_ALLOWED_PHASES = {"regular", "play_in", "playoffs", "preseason"}


def _require_dict(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"GameResultV2 invalid: '{path}' must be an object/dict")
    return value


def _require_list(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(f"GameResultV2 invalid: '{path}' must be an array/list")
    return value


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


def _merge_counter_dict_sum(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """카운터/분해지표를 key별로 합산한다. (중첩 dict는 재귀 합산)"""
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if isinstance(v, dict):
            child = dst.get(k)
            if not isinstance(child, dict):
                child = {}
                dst[k] = child
            _merge_counter_dict_sum(child, v)
        elif _is_number(v):
            try:
                dst[k] = float(dst.get(k, 0.0)) + float(v)
            except (TypeError, ValueError):
                continue


_META_PLAYER_KEYS = {"PlayerID", "TeamID", "Name", "Pos", "Position"}


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


def _get_phase_container(phase: str) -> Dict[str, Any]:
    """phase별 누적 컨테이너를 반환한다."""
    if phase == "regular":
        return GAME_STATE
    postseason = GAME_STATE.setdefault("postseason", {})
    return postseason.setdefault(phase, {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}})


def ingest_game_result(
    *,
    game_result: Dict[str, Any],
    game_date: Optional[str] = None,
    store_raw_result: bool = True,
) -> Dict[str, Any]:
    """정식 GameResultV2 스키마 결과를 GAME_STATE에 반영한다."""
    validate_v2_game_result(game_result))

    game = _require_dict(game_result["game"], "game")
    season_id = str(game["season_id"])
    phase = str(game["phase"])

    _ensure_active_season_id(season_id)
    container = _get_phase_container(phase)

    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])
    final = _require_dict(game_result["final"], "final")

    game_date_str = str(game_date) if game_date else str(game["date"])
    game_id = str(game["game_id"])

    home_score = int(final[home_id])
    away_score = int(final[away_id])

    game_obj = {
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

    GAME_STATE["turn"] = int(GAME_STATE.get("turn", 0) or 0) + 1
    current_turn = int(GAME_STATE.get("turn", 0) or 0)
    game_obj["ingest_turn"] = current_turn
    container.setdefault("games", []).append(game_obj)

    if store_raw_result:
        container.setdefault("game_results", {})[game_id] = game_result

    teams = _require_dict(game_result["teams"], "teams")
    season_player_stats = container.setdefault("player_stats", {})
    season_team_stats = container.setdefault("team_stats", {})

    for tid in (home_id, away_id):
        team_game = _require_dict(teams[tid], f"teams.{tid}")
        _accumulate_team_game_result(tid, team_game, season_team_stats)
        rows = _require_list(team_game.get("players"), f"teams.{tid}.players")
        _accumulate_player_rows(rows, season_player_stats)

    _mark_master_schedule_game_final(
        game_id=game_id,
        game_date_str=game_date_str,
        home_id=home_id,
        away_id=away_id,
        home_score=home_score,
        away_score=away_score,
    )
    _mark_views_dirty()

    return game_obj


def get_scores_view(season_id: str, limit: int = 20) -> Dict[str, Any]:
    """Return cached or rebuilt scores view for the given season."""
    _ensure_ingest_turn_backfilled()
    cached = GAME_STATE.setdefault("cached_views", {})
    scores_view = cached.setdefault("scores", {"latest_date": None, "games": []})
    meta = _ensure_cached_views_meta()
    current_turn = int(GAME_STATE.get("turn", 0) or 0)

    if (
        meta["scores"].get("built_from_turn") == current_turn
        and str(meta["scores"].get("season_id")) == str(season_id)
    ):
        games = scores_view.get("games") or []
        limited_games = [] if limit <= 0 else games[:limit]
        return {"latest_date": scores_view.get("latest_date"), "games": limited_games}

    games: List[Dict[str, Any]] = []
    active_season_id = GAME_STATE.get("active_season_id")
    if active_season_id is not None and str(active_season_id) == str(season_id):
        games.extend(GAME_STATE.get("games") or [])
    else:
        history = GAME_STATE.get("season_history") or {}
        season_history = history.get(str(season_id)) or {}
        games.extend(season_history.get("games") or [])

    postseason = GAME_STATE.get("postseason") or {}
    for container in postseason.values():
        if not isinstance(container, dict):
            continue
        for game_obj in container.get("games") or []:
            if str(game_obj.get("season_id")) == str(season_id):
                games.append(game_obj)

    def _ingest_turn_key(game_obj: Dict[str, Any]) -> int:
        try:
            return int(game_obj.get("ingest_turn") or 0)
        except (TypeError, ValueError):
            return 0

    games_sorted = sorted(games, key=_ingest_turn_key, reverse=True)
    latest_date = games_sorted[0].get("date") if games_sorted else None

    scores_view["games"] = games_sorted
    scores_view["latest_date"] = latest_date
    meta["scores"]["built_from_turn"] = current_turn
    meta["scores"]["season_id"] = season_id

    limited_games = [] if limit <= 0 else games_sorted[:limit]
    return {"latest_date": latest_date, "games": limited_games}


def get_team_schedule_view(
    team_id: str,
    season_id: str,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Return cached or rebuilt schedule view for a team."""
    active_season_id = GAME_STATE.get("active_season_id")
    if active_season_id is not None and str(season_id) != str(active_season_id):
        return {"past_games": [], "upcoming_games": []}

    initialize_master_schedule_if_needed()
    _ensure_master_schedule_indices()
    league = _ensure_league_state()
    master_schedule = league.get("master_schedule") or {}
    by_team = master_schedule.get("by_team") or {}
    by_id = master_schedule.get("by_id") or {}

    cached = GAME_STATE.setdefault("cached_views", {})
    schedule = cached.setdefault("schedule", {})
    teams_cache = schedule.setdefault("teams", {})
    meta = _ensure_cached_views_meta()
    current_turn = int(GAME_STATE.get("turn", 0) or 0)

    if (
        meta["schedule"].get("season_id") == season_id
        and meta["schedule"].get("built_from_turn_by_team", {}).get(team_id) == current_turn
        and team_id in teams_cache
    ):
        return teams_cache[team_id]

    game_ids = by_team.get(team_id, [])
    past_games: List[Dict[str, Any]] = []
    upcoming_games: List[Dict[str, Any]] = []

    for game_id in game_ids:
        entry = by_id.get(game_id) if isinstance(by_id, dict) else None
        if not entry:
            continue
        status = entry.get("status")
        home_id = str(entry.get("home_team_id"))
        away_id = str(entry.get("away_team_id"))
        is_home = home_id == team_id
        opponent_team_id = away_id if is_home else home_id
        home_score = entry.get("home_score")
        away_score = entry.get("away_score")
        is_final = status == "final" and isinstance(home_score, int) and isinstance(away_score, int)
        if is_final:
            my_score = home_score if is_home else away_score
            opp_score = away_score if is_home else home_score
            result_for_user_team = "W" if my_score > opp_score else "L"
        else:
            my_score = None
            opp_score = None
            result_for_user_team = None

        row = {
            "game_id": str(entry.get("game_id")),
            "date": str(entry.get("date")),
            "status": str(status),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_score": home_score if isinstance(home_score, int) else None,
            "away_score": away_score if isinstance(away_score, int) else None,
            "opponent_team_id": opponent_team_id,
            "is_home": is_home,
            "my_score": my_score,
            "opp_score": opp_score,
            "result_for_user_team": result_for_user_team,
        }

        if status == "final":
            past_games.append(row)
        else:
            upcoming_games.append(row)

    def _schedule_date_sort_key(entry: Dict[str, Any]) -> tuple[int, Any]:
        raw_date = entry.get("date")
        try:
            return (0, date.fromisoformat(str(raw_date)))
        except (TypeError, ValueError):
            return (1, str(raw_date))

    past_games.sort(key=_schedule_date_sort_key, reverse=True)
    upcoming_games.sort(key=_schedule_date_sort_key)

    teams_cache[team_id] = {"past_games": past_games, "upcoming_games": upcoming_games}
    meta["schedule"]["built_from_turn_by_team"][team_id] = current_turn
    meta["schedule"]["season_id"] = season_id
    return teams_cache[team_id]


def get_schedule_summary() -> Dict[str, Any]:
    """마스터 스케줄 통계 요약을 반환한다.

    - 총 경기 수, 상태별 경기 수
    - 팀별 총 경기 수(82 보장 여부)와 홈/원정 분배
    """
    initialize_master_schedule_if_needed()
    league = _ensure_league_state()
    master = league.get("master_schedule") or {}
    games = master.get("games") or []
    by_team = master.get("by_team") or {}

    status_counts: Dict[str, int] = {}
    home_away: Dict[str, Dict[str, int]] = {
        tid: {"home": 0, "away": 0} for tid in ALL_TEAM_IDS
    }

    for g in games:
        status = g.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        home_id = g.get("home_team_id")
        away_id = g.get("away_team_id")
        if home_id in home_away:
            home_away[home_id]["home"] += 1
        if away_id in home_away:
            home_away[away_id]["away"] += 1

    team_breakdown: Dict[str, Dict[str, Any]] = {}
    for tid in ALL_TEAM_IDS:
        team_breakdown[tid] = {
            "games": len(by_team.get(tid, [])),
            "home": home_away.get(tid, {}).get("home", 0),
            "away": home_away.get(tid, {}).get("away", 0),
        }
        team_breakdown[tid]["home_away_diff"] = (
            team_breakdown[tid]["home"] - team_breakdown[tid]["away"]
        )

    return {
        "total_games": len(games),
        "status_counts": status_counts,
        "team_breakdown": team_breakdown,
    }






