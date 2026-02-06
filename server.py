from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any, Dict, Optional, List

import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BASE_DIR, ALL_TEAM_IDS
from league_repo import LeagueRepo
from league_service import LeagueService
from schema import normalize_team_id
import state
from sim.league_sim import simulate_single_game, advance_league_until
from playoffs import (
    auto_advance_current_round,
    advance_my_team_one_game,
    build_postseason_field,
    initialize_postseason,
    play_my_team_play_in_game,
    reset_postseason_state,
)
from news_ai import refresh_playoff_news, refresh_weekly_news
from stats_util import compute_league_leaders, compute_playoff_league_leaders
from team_utils import get_conference_standings, get_team_cards, get_team_detail, ui_cache_rebuild_all, ui_cache_refresh_players
from season_report_ai import generate_season_report
from trades.errors import TradeError
from trades.models import canonicalize_deal, parse_deal, serialize_deal
from trades.validator import validate_deal
from trades.apply import apply_deal_to_db
from trades import agreements
from trades import negotiation_store

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# FastAPI 앱 생성 및 기본 설정
# -------------------------------------------------------------------------
app = FastAPI(title="느바 시뮬 GM 서버")

@app.on_event("startup")
def _startup_init_state() -> None:
    # 1) DB init + seed once (per db_path)
    # 2) SSOT state init: season/schedule + cap model
    # 3) repo integrity validate once (per db_path)
    # 4) ingest_turn backfill once (per state instance)
    # 5) UI-only cache bootstrap (derived, non-authoritative)
    db_path = os.environ.get("LEAGUE_DB_PATH")
    if not db_path:
        raise RuntimeError("LEAGUE_DB_PATH is required (no default db_path).")
    state.set_db_path(db_path)

    state.startup_init_state()

    # Explicit UI-only cache bootstrap (derived, non-authoritative).
    # Ensures team/player UI metadata exists from server boot without requiring any read path to "init".
    try:
        ui_cache_rebuild_all()
    except Exception as e:
        raise RuntimeError(f"ui_cache_rebuild_all() failed during startup: {e}") from e

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# static/NBA.html 서빙
static_dir = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """간단한 헬스체크 및 NBA.html 링크 안내."""
    index_path = os.path.join(static_dir, "NBA.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "느바 시뮬 GM 서버입니다. /static/NBA.html 을 확인하세요."}


# -------------------------------------------------------------------------
# Pydantic 모델 정의
# -------------------------------------------------------------------------
class SimGameRequest(BaseModel):
    home_team_id: str
    away_team_id: str
    home_tactics: Optional[Dict[str, Any]] = None
    away_tactics: Optional[Dict[str, Any]] = None
    game_date: Optional[str] = None  # 인게임 날짜 (YYYY-MM-DD)


class ChatMainRequest(BaseModel):
    apiKey: str
    userInput: str = Field(..., alias="userMessage")
    mainPrompt: Optional[str] = ""
    context: Any = ""

    class Config:
        allow_population_by_field_name = True
        allow_population_by_alias = True
        fields = {"userInput": "userMessage"}


class AdvanceLeagueRequest(BaseModel):
    target_date: str  # YYYY-MM-DD, 이 날짜까지 리그를 자동 진행
    user_team_id: Optional[str] = None


class PostseasonSetupRequest(BaseModel):
    my_team_id: str
    use_random_field: bool = False


class EmptyRequest(BaseModel):
    pass


class WeeklyNewsRequest(BaseModel):
    apiKey: str


class ApiKeyRequest(BaseModel):
    apiKey: str


class SeasonReportRequest(BaseModel):
    apiKey: str
    user_team_id: str


class TradeSubmitRequest(BaseModel):
    deal: Dict[str, Any]


class TradeSubmitCommittedRequest(BaseModel):
    deal_id: str


class TradeNegotiationStartRequest(BaseModel):
    user_team_id: str
    other_team_id: str


class TradeNegotiationCommitRequest(BaseModel):
    session_id: str
    deal: Dict[str, Any]

class TradeEvaluateRequest(BaseModel):
    deal: Dict[str, Any]
    team_id: str
    include_breakdown: bool = True


# -------------------------------------------------------------------------
# Contracts / Roster Write API models
# -------------------------------------------------------------------------
class ReleaseToFARequest(BaseModel):
    player_id: str
    released_date: Optional[str] = None  # YYYY-MM-DD (default: in-game date)


class SignFreeAgentRequest(BaseModel):
    team_id: str
    player_id: str
    signed_date: Optional[str] = None  # YYYY-MM-DD (default: in-game date)
    years: int = 1
    salary_by_year: Optional[Dict[int, int]] = None  # {season_year: salary}


class ReSignOrExtendRequest(BaseModel):
    team_id: str
    player_id: str
    signed_date: Optional[str] = None  # YYYY-MM-DD (default: in-game date)
    years: int = 1
    salary_by_year: Optional[Dict[int, int]] = None  # {season_year: salary}


# -------------------------------------------------------------------------
# 유틸: Gemini 응답 텍스트 추출
# -------------------------------------------------------------------------
def extract_text_from_gemini_response(resp: Any) -> str:
    """google-generativeai 응답 객체에서 텍스트만 안전하게 뽑아낸다."""
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
    except Exception:
        pass

    return str(resp)


# -------------------------------------------------------------------------
# 경기 시뮬레이션 API
# -------------------------------------------------------------------------
@app.post("/api/simulate-game")
async def api_simulate_game(req: SimGameRequest):
    """matchengine_v3를 사용해 한 경기를 시뮬레이션한다.

    NOTE (SSOT 계약):
    - Home/Away SSOT는 league_sim.simulate_single_game 내부에서 GameContext로 생성/주입된다.
    - server는 엔진을 직접 호출하지 않으며(직접 호출 금지), 결과는 어댑터+validator 관문을 통과한 V2만 반환한다.
    """
    try:
        result = simulate_single_game(
            home_team_id=req.home_team_id,
            away_team_id=req.away_team_id,
            game_date=req.game_date,
            home_tactics=req.home_tactics,
            away_tactics=req.away_tactics,
        )
        return result
    except ValueError as e:
        # 팀을 찾지 못한 경우 등
        raise HTTPException(status_code=404, detail=str(e))


# -------------------------------------------------------------------------
# 리그 자동 진행 API (다른 팀 경기 일괄 시뮬레이션)
# -------------------------------------------------------------------------
@app.post("/api/advance-league")
async def api_advance_league(req: AdvanceLeagueRequest):
    """target_date까지 (유저 팀 경기를 제외한) 리그 전체 경기를 자동 시뮬레이션."""
    try:
        simulated = advance_league_until(
            target_date_str=req.target_date,
            user_team_id=req.user_team_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "target_date": req.target_date,
        "simulated_count": len(simulated),
        "simulated_games": simulated,
    }


# -------------------------------------------------------------------------
# 리그 리더 / 스탠딩 / 팀 API
# -------------------------------------------------------------------------


@app.get("/api/stats/leaders")
async def api_stats_leaders():
    # The frontend expects a flat object with an uppercase stat key (e.g., PTS)
    # under `data.leaders`. Some previous iterations of the API wrapped this
    # structure under stats.leaderboards with lowercase keys, which caused the
    # UI to break. Normalize here so the client always receives
    # `{ leaders: { PTS: [...], AST: [...], ... }, updated_at: <iso date> }`.
    workflow_state = state.export_workflow_state()
    leaders = compute_league_leaders(workflow_state.get("player_stats") or {})
    current_date = state.get_current_date()
    return {"leaders": leaders, "updated_at": current_date}


@app.get("/api/stats/playoffs/leaders")
async def api_playoff_stats_leaders():
    workflow_state = state.export_workflow_state()
    playoff_stats = (workflow_state.get("phase_results") or {}).get("playoffs", {}).get("player_stats") or {}
    leaders = compute_playoff_league_leaders(playoff_stats)
    current_date = state.get_current_date()
    return {"leaders": leaders, "updated_at": current_date}


@app.get("/api/standings")
async def api_standings():
    return get_conference_standings()


@app.get("/api/teams")
async def api_teams():
    return get_team_cards()


@app.get("/api/team-detail/{team_id}")
async def api_team_detail(team_id: str):
    try:
        return get_team_detail(team_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# -------------------------------------------------------------------------
# 플레이-인 / 플레이오프
# -------------------------------------------------------------------------


@app.get("/api/postseason/field")
async def api_postseason_field():
    return build_postseason_field()


@app.get("/api/postseason/state")
async def api_postseason_state():
    return state.get_postseason_snapshot()


@app.post("/api/postseason/reset")
async def api_postseason_reset():
    return reset_postseason_state()


@app.post("/api/postseason/setup")
async def api_postseason_setup(req: PostseasonSetupRequest):
    try:
        return initialize_postseason(req.my_team_id, use_random_field=req.use_random_field)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/postseason/play-in/my-team-game")
async def api_play_in_my_team_game(req: EmptyRequest):
    try:
        return play_my_team_play_in_game()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/postseason/playoffs/advance-my-team-game")
async def api_playoffs_advance_my_team_game(req: EmptyRequest):
    try:
        return advance_my_team_one_game()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/postseason/playoffs/auto-advance-round")
async def api_playoffs_auto_advance_round(req: EmptyRequest):
    try:
        return auto_advance_current_round()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


 # -------------------------------------------------------------------------
# 시즌 전환 (오프시즌 진입 / 정규시즌 시작)
# -------------------------------------------------------------------------


@app.post("/api/season/enter-offseason")
async def api_enter_offseason(req: EmptyRequest):
    """플레이오프 우승 확정 이후, 다음 시즌으로 전환하고 오프시즌(날짜 구간)으로 진입한다.

    Design notes:
    - SSOT 시즌 전환은 state.start_new_season(...) 하나로만 수행한다.
    - 별도의 state.phase 키를 추가하지 않고, current_date를 오프시즌 날짜(예: 7/1)로 이동해
      UI에서 '오프시즌 상태'를 표현할 수 있게 한다.
    - 이후 오프시즌 세부 기능(드래프트/FA/재계약 등)은 이 구간에 엔드포인트를 추가하면 된다.
    """
    post = state.get_postseason_snapshot() or {}
    champion = post.get("champion")
    if not champion:
        raise HTTPException(status_code=400, detail="Champion not decided yet.")

    league_ctx = state.get_league_context_snapshot() or {}
    try:
        season_year = int(league_ctx.get("season_year") or 0)
    except Exception:
        season_year = 0
    if season_year <= 0:
        raise HTTPException(status_code=500, detail="Invalid season_year in state.")

    next_year = season_year + 1

    # SSOT season transition: contracts/cap/schedule/indices/postseason reset.
    transition = state.start_new_season(
        next_year,
        rebuild_schedule=True,
        run_offseason=True,
    )

    # Skeleton offseason: move to an offseason date window where there are no scheduled games.
    offseason_start = f"{next_year}-07-01"
    state.set_current_date(offseason_start)

    # Best-effort UI cache rebuild (derived, non-authoritative).
    try:
        ui_cache_rebuild_all()
    except Exception:
        pass

    # Re-read league context after transition.
    league_after = state.get_league_context_snapshot() or {}
    return {
        "ok": True,
        "prev_champion": champion,
        "transition": transition,
        "offseason_start": offseason_start,
        "season_start": league_after.get("season_start"),
        "season_year": league_after.get("season_year"),
    }


@app.post("/api/season/start-regular-season")
async def api_start_regular_season(req: EmptyRequest):
    """오프시즌(또는 임의 시점)에서 정규시즌 시작 직전으로 날짜를 이동한다.

    IMPORTANT:
    - advance_league_until()은 current_date+1부터 진행하므로, 개막일 게임을 스킵하지 않게
      season_start '전날'로 세팅한다.
    """
    league_ctx = state.get_league_context_snapshot() or {}
    season_start = league_ctx.get("season_start")
    if not season_start:
        raise HTTPException(status_code=500, detail="season_start is missing. Schedule not initialized?")

    try:
        ss = date.fromisoformat(str(season_start))
    except ValueError:
        raise HTTPException(status_code=500, detail=f"Invalid season_start format: {season_start}")

    start_day_minus_1 = (ss - timedelta(days=1)).isoformat()
    state.set_current_date(start_day_minus_1)

    return {
        "ok": True,
        "current_date": state.get_current_date(),
        "season_start": str(season_start),
    }


# -------------------------------------------------------------------------
# 주간 뉴스 (LLM 요약)
# -------------------------------------------------------------------------


@app.post("/api/news/week")
async def api_news_week(req: WeeklyNewsRequest):
    if not req.apiKey:
        raise HTTPException(status_code=400, detail="apiKey is required")
    try:
        payload = refresh_weekly_news(req.apiKey)

        # Some endpoints previously wrapped the news payload like
        # `{ "news": { "current_date": ..., "items": [...] } }`, which the
        # frontend does not expect. Normalize it back to the raw shape.
        if isinstance(payload, dict) and "news" in payload and isinstance(
            payload["news"], dict
        ):
            payload = payload["news"]

        return payload
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Weekly news generation failed: {e}")


@app.post("/api/news/playoffs")
async def api_playoff_news(req: EmptyRequest):
    try:
        return refresh_playoff_news()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Playoff news generation failed: {e}")


@app.post("/api/season-report")
async def api_season_report(req: SeasonReportRequest):
    """정규 시즌 종료 후, LLM을 이용해 시즌 결산 리포트를 생성한다."""
    if not req.apiKey:
        raise HTTPException(status_code=400, detail="apiKey is required")

    try:
        report_text = generate_season_report(req.apiKey, req.user_team_id)
        return {"report_markdown": report_text}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Season report generation failed: {e}")


@app.post("/api/validate-key")
async def api_validate_key(req: ApiKeyRequest):
    """주어진 Gemini API 키를 간단히 검증한다."""
    if not req.apiKey:
        raise HTTPException(status_code=400, detail="apiKey is required")

    try:
        genai.configure(api_key=req.apiKey)
        # 최소 호출로 키 유효성 확인 (토큰 카운트 호출)
        model = genai.GenerativeModel("gemini-3-pro-preview")
        model.count_tokens("ping")
        return {"valid": True}
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid API key: {e}")


# -------------------------------------------------------------------------
# 메인 LLM (Home 대화) API
# -------------------------------------------------------------------------
@app.post("/api/chat-main")
async def chat_main(req: ChatMainRequest):
    """메인 프롬프트 + 컨텍스트 + 유저 입력을 가지고 Gemini를 호출."""
    if not req.apiKey:
        raise HTTPException(status_code=400, detail="apiKey is required")

    try:
        genai.configure(api_key=req.apiKey)
        model = genai.GenerativeModel(
            model_name="gemini-3-pro-preview",
            system_instruction=req.mainPrompt or "",
        )

        context_text = req.context
        if isinstance(req.context, (dict, list)):
            context_text = json.dumps(req.context, ensure_ascii=False)

        prompt = f"{context_text}\n\n[USER]\n{req.userInput}"
        resp = model.generate_content(prompt)
        text = extract_text_from_gemini_response(resp)
        return {"reply": text, "answer": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini main chat error: {e}")


@app.post("/api/main-llm")
async def chat_main_legacy(req: ChatMainRequest):
    return await chat_main(req)


# -------------------------------------------------------------------------
# 트레이드 API
# -------------------------------------------------------------------------
def _trade_error_response(error: TradeError) -> JSONResponse:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
    }
    return JSONResponse(status_code=400, content=payload)

def _validate_repo_integrity(db_path: str) -> None:
    with LeagueRepo(db_path) as repo:
        # DB schema is guaranteed during server startup (state.startup_init_state()).
        repo.validate_integrity()


def _try_ui_cache_refresh_players(player_ids: List[str], *, context: str) -> None:
    """Best-effort UI cache refresh. Never fails the API call.

    Policy: DB SSOT write APIs should succeed even if UI cache refresh fails.
    """
    try:
        if not player_ids:
            return
        ui_cache_refresh_players(player_ids)
    except Exception:
        logger.warning(
            "UI cache refresh failed (%s): player_ids=%r",
            context,
            player_ids,
            exc_info=True,
        )

# -------------------------------------------------------------------------
# Contracts / Roster Write API
# -------------------------------------------------------------------------
@app.post("/api/contracts/release-to-fa")
async def api_contracts_release_to_fa(req: ReleaseToFARequest):
    """Release a player to free agency (DB write)."""
    try:
        db_path = state.get_db_path()
        in_game_date = state.get_current_date_as_date()
        with LeagueRepo(db_path) as repo:
            svc = LeagueService(repo)
            event = svc.release_player_to_free_agency(
                player_id=req.player_id,
                released_date=req.released_date or in_game_date,
            )
        _validate_repo_integrity(db_path)
        event_dict = event.to_dict()
        affected = event_dict.get("affected_player_ids") or []
        _try_ui_cache_refresh_players(list(affected), context="contracts.release_to_fa")
        return {"ok": True, "event": event_dict}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Release-to-FA failed: {e}")


@app.post("/api/contracts/sign-free-agent")
async def api_contracts_sign_free_agent(req: SignFreeAgentRequest):
    """Sign a free agent (DB write): roster.team_id + contract + active contract."""
    try:
        db_path = state.get_db_path()
        in_game_date = state.get_current_date_as_date()
        with LeagueRepo(db_path) as repo:
            svc = LeagueService(repo)
            event = svc.sign_free_agent(
                team_id=req.team_id,
                player_id=req.player_id,
                signed_date=req.signed_date or in_game_date,
                years=req.years,
                salary_by_year=req.salary_by_year,
            )
        _validate_repo_integrity(db_path)
        event_dict = event.to_dict()
        affected = event_dict.get("affected_player_ids") or []
        _try_ui_cache_refresh_players(list(affected), context="contracts.sign_free_agent")
        return {"ok": True, "event": event_dict}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sign-free-agent failed: {e}")


@app.post("/api/contracts/re-sign-or-extend")
async def api_contracts_re_sign_or_extend(req: ReSignOrExtendRequest):
    """Re-sign / extend a player (DB write): contract + active contract (+ roster salary sync)."""
    try:
        db_path = state.get_db_path()
        in_game_date = state.get_current_date_as_date()
        with LeagueRepo(db_path) as repo:
            svc = LeagueService(repo)
            event = svc.re_sign_or_extend(
                team_id=req.team_id,
                player_id=req.player_id,
                signed_date=req.signed_date or in_game_date,
                years=req.years,
                salary_by_year=req.salary_by_year,
            )
        _validate_repo_integrity(db_path)
        event_dict = event.to_dict()
        affected = event_dict.get("affected_player_ids") or []
        _try_ui_cache_refresh_players(list(affected), context="contracts.re_sign_or_extend")
        return {"ok": True, "event": event_dict}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-sign/extend failed: {e}")


@app.post("/api/trade/submit")
async def api_trade_submit(req: TradeSubmitRequest):
    try:
        in_game_date = state.get_current_date_as_date()
        db_path = state.get_db_path()
        agreements.gc_expired_agreements(current_date=in_game_date)
        deal = canonicalize_deal(parse_deal(req.deal))
        validate_deal(deal, current_date=in_game_date)
        transaction = apply_deal_to_db(
            db_path=db_path,
            deal=deal,
            source="menu",
            deal_id=None,
            trade_date=in_game_date,
            dry_run=False,
        )
        _validate_repo_integrity(db_path)
        moved_ids: List[str] = []
        for mv in (transaction.get("player_moves") or []):
            if isinstance(mv, dict):
                pid = mv.get("player_id")
                if pid:
                    moved_ids.append(str(pid))
        _try_ui_cache_refresh_players(moved_ids, context="trade.submit")
        return {
            "ok": True,
            "deal": serialize_deal(deal),
            "transaction": transaction,
        }
    except TradeError as exc:
        return _trade_error_response(exc)


@app.post("/api/trade/submit-committed")
async def api_trade_submit_committed(req: TradeSubmitCommittedRequest):
    try:
        in_game_date = state.get_current_date_as_date()
        db_path = state.get_db_path()
        agreements.gc_expired_agreements(current_date=in_game_date)
        deal = agreements.verify_committed_deal(req.deal_id, current_date=in_game_date)
        validate_deal(
            deal,
            current_date=in_game_date,
            allow_locked_by_deal_id=req.deal_id,
        )
        transaction = apply_deal_to_db(
            db_path=db_path,
            deal=deal,
            source="negotiation",
            deal_id=req.deal_id,
            trade_date=in_game_date,
            dry_run=False,
        )
        _validate_repo_integrity(db_path)
        agreements.mark_executed(req.deal_id)
        moved_ids: List[str] = []
        for mv in (transaction.get("player_moves") or []):
            if isinstance(mv, dict):
                pid = mv.get("player_id")
                if pid:
                    moved_ids.append(str(pid))
        _try_ui_cache_refresh_players(moved_ids, context="trade.submit_committed")
        return {"ok": True, "deal_id": req.deal_id, "transaction": transaction}
    except TradeError as exc:
        return _trade_error_response(exc)


@app.post("/api/trade/negotiation/start")
async def api_trade_negotiation_start(req: TradeNegotiationStartRequest):
    try:
        session = negotiation_store.create_session(
            user_team_id=req.user_team_id, other_team_id=req.other_team_id
        )
        return {"ok": True, "session": session}
    except TradeError as exc:
        return _trade_error_response(exc)


@app.post("/api/trade/negotiation/commit")
async def api_trade_negotiation_commit(req: TradeNegotiationCommitRequest):
    try:
        in_game_date = state.get_current_date_as_date()
        db_path = state.get_db_path()
        session = negotiation_store.get_session(req.session_id)
        deal = canonicalize_deal(parse_deal(req.deal))
        team_ids = {session["user_team_id"].upper(), session["other_team_id"].upper()}
        if set(deal.teams) != team_ids or len(deal.teams) != 2:
            raise TradeError(
                "DEAL_INVALIDATED",
                "Deal teams must match negotiation session",
                {"session_id": req.session_id, "teams": deal.teams},
            )
        validate_deal(deal, current_date=in_game_date)

        # Hot path: negotiation UI calls this endpoint repeatedly.
        # DB integrity is already guaranteed at startup and after any write APIs.
        # Avoid running full repo integrity check on every offer update.
        validate_deal(deal, current_date=in_game_date, db_path=db_path, integrity_check=False)
        
        # Always persist the latest valid offer payload
        negotiation_store.set_draft_deal(req.session_id, serialize_deal(deal))

        # ------------------------------------------------------------------
        # AI evaluation (other team perspective)
        # NOTE:
        # - legality is already checked by validate_deal above
        # - valuation service will build DecisionContext internally (team_situation + gm profile)
        # ------------------------------------------------------------------
        other_team_id = session["other_team_id"].upper()

        # Local imports to keep integration flexible.
        from trades.valuation.service import evaluate_deal_for_team as eval_service  # type: ignore
        from trades.valuation.types import to_jsonable, DealVerdict  # type: ignore

        decision, evaluation = eval_service(
            deal=deal,
            team_id=other_team_id,
            current_date=in_game_date,
            db_path=db_path,
            include_breakdown=False,   # keep negotiation response light
            include_package_effects=True,
            allow_counter=True,
            validate=False,            # already validated above
        )

        eval_summary = {
            "team_id": other_team_id,
            "incoming_total": float(evaluation.incoming_total),
            "outgoing_total": float(evaluation.outgoing_total),
            "net_surplus": float(evaluation.net_surplus),
            "surplus_ratio": float(evaluation.surplus_ratio),
        }

        # Record AI response in-session for debugging / UI explanations
        try:
            negotiation_store.set_last_counter(
                req.session_id,
                {
                    "verdict": to_jsonable(decision.verdict),
                    "decision": to_jsonable(decision),
                    "evaluation": eval_summary,
                },
            )
        except Exception:
            # Session logging failure should not crash commit flow
            pass

        # Decide action
        verdict = decision.verdict

        if verdict == DealVerdict.ACCEPT:
            committed = agreements.create_committed_deal(
                deal,
                valid_days=2,
                current_date=in_game_date,
                validate=False,   # already validated above
                db_path=db_path,  # keep hash/locking based on the same db snapshot
            )
            negotiation_store.set_committed(req.session_id, committed["deal_id"])
            return {
                "ok": True,
                "accepted": True,
                "deal_id": committed["deal_id"],
                "expires_at": committed["expires_at"],
                "deal": serialize_deal(deal),
                "ai_verdict": to_jsonable(decision.verdict),
                "ai_decision": to_jsonable(decision),
                "ai_evaluation": eval_summary,
            }

        # COUNTER is not implemented yet -> treat as reject (or return explicit marker)
        counter_unimplemented = (verdict == DealVerdict.COUNTER)

        # Build a short reason string for UI
        try:
            reason_lines = []
            for r in (decision.reasons or [])[:4]:
                if isinstance(r, dict):
                    msg = r.get("message") or r.get("code") or ""
                else:
                    msg = getattr(r, "message", None) or getattr(r, "code", None) or ""
                if msg:
                    reason_lines.append(str(msg))
            reason_text = " | ".join(reason_lines) if reason_lines else "AI rejected the offer."
        except Exception:
            reason_text = "AI rejected the offer."

        # Record rejection in session (message + phase)
        try:
            negotiation_store.append_message(
                req.session_id,
                speaker="OTHER_GM",
                text=f"[{other_team_id}] {verdict}: {reason_text}",
            )
            negotiation_store.set_phase(req.session_id, "REJECTED" if not counter_unimplemented else "COUNTER_PENDING")
        except Exception:
            pass

        return {
            "ok": True,
            "accepted": False,
            "counter_unimplemented": bool(counter_unimplemented),
            "deal": serialize_deal(deal),
            "ai_verdict": to_jsonable(decision.verdict),
            "ai_decision": to_jsonable(decision),
            "ai_evaluation": eval_summary,
        }
    except TradeError as exc:
        return _trade_error_response(exc)


@app.post("/api/trade/evaluate")
async def api_trade_evaluate(req: TradeEvaluateRequest):
    """
    Debug endpoint: evaluate a proposed deal from a single team's perspective.
    Flow:
      deal = canonicalize_deal(parse_deal(req.deal))
      validate_deal(deal, current_date=in_game_date)
      trades.valuation.service.evaluate_deal_for_team(...)
      return decision + breakdown
    """
    try:
        in_game_date = state.get_current_date_as_date()
        db_path = state.get_db_path()

        deal = canonicalize_deal(parse_deal(req.deal))
        # Hot path: debug / UI-driven repeated calls.
        # Integrity is checked at startup and after any DB writes.
        validate_deal(deal, current_date=in_game_date, db_path=db_path, integrity_check=False)

        # Local import to avoid hard dependency during incremental integration.
        from trades.valuation.service import evaluate_deal_for_team as eval_service  # type: ignore
        from trades.valuation.types import to_jsonable  # type: ignore

        decision, evaluation = eval_service(
            deal=deal,
            team_id=req.team_id,
            current_date=in_game_date,
            db_path=db_path,
            include_breakdown=bool(req.include_breakdown),
            # We already validated above; avoid duplicate validate_deal in service.
            validate=False,
        )

        return {
            "ok": True,
            "team_id": str(req.team_id).upper(),
            "deal": serialize_deal(deal),
            "decision": to_jsonable(decision),
            "evaluation": to_jsonable(evaluation),
        }
    except TradeError as exc:
        return _trade_error_response(exc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Trade evaluation failed: {exc}")


# -------------------------------------------------------------------------
# 로스터 요약 API (LLM 컨텍스트용)
# -------------------------------------------------------------------------
@app.get("/api/roster-summary/{team_id}")
async def roster_summary(team_id: str):
    """특정 팀의 로스터를 LLM이 보기 좋은 형태로 요약해서 돌려준다."""
    db_path = state.get_db_path()
    team_id = str(normalize_team_id(team_id, strict=True))
    with LeagueRepo(db_path) as repo:
        # DB schema is guaranteed during server startup (state.startup_init_state()).
        roster = repo.get_team_roster(team_id)

    if not roster:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found in roster")

    players: List[Dict[str, Any]] = []
    for row in roster:
        players.append({
            "player_id": row.get("player_id"),
            "name": row.get("name"),
            "pos": str(row.get("pos") or ""),
            "overall": float(row.get("ovr") or 0.0),
        })

    players = sorted(players, key=lambda x: x["overall"], reverse=True)

    return {
        "team_id": team_id,
        "players": players[:12],
    }


# -------------------------------------------------------------------------
# 팀별 시즌 스케줄 조회 API
# -------------------------------------------------------------------------
@app.get("/api/team-schedule/{team_id}")
async def team_schedule(team_id: str):
    """마스터 스케줄 기준으로 특정 팀의 전체 시즌 일정을 반환."""
    team_id = team_id.upper()
    if team_id not in ALL_TEAM_IDS:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found in league")

    # (startup 보장 전제) 마스터 스케줄은 이미 초기화되어 있어야 함
    league = state.export_full_state_snapshot().get("league", {})
    master_schedule = league.get("master_schedule", {})
    games = master_schedule.get("games") or []

    if not games:
        raise HTTPException(
            status_code=500,
            detail="Master schedule is not initialized. Expected server startup_init_state() to run.",
        )
        

    team_games: List[Dict[str, Any]] = [
        g for g in games
        if g.get("home_team_id") == team_id or g.get("away_team_id") == team_id
    ]
    team_games.sort(key=lambda g: (g.get("date"), g.get("game_id")))

    formatted_games: List[Dict[str, Any]] = []
    for g in team_games:
        home_score = g.get("home_score")
        away_score = g.get("away_score")
        result_for_team = None
        if home_score is not None and away_score is not None:
            if team_id == g.get("home_team_id"):
                result_for_team = "W" if home_score > away_score else "L"
            else:
                result_for_team = "W" if away_score > home_score else "L"

        formatted_games.append({
            "game_id": g.get("game_id"),
            "date": g.get("date"),
            "home_team_id": g.get("home_team_id"),
            "away_team_id": g.get("away_team_id"),
            "home_score": home_score,
            "away_score": away_score,
            "result_for_user_team": result_for_team,
        })

    return {
        "team_id": team_id,
        "games": formatted_games,
    }


# -------------------------------------------------------------------------
# STATE 요약 조회 API (프론트/디버그용)
# -------------------------------------------------------------------------

@app.get("/api/state/summary")
async def state_summary():
    workflow_state: Dict[str, Any] = state.export_workflow_state()
    for k in (
        # Trade assets ledger (DB SSOT)
        "draft_picks",
        "swap_rights",
        "fixed_assets",
        # Transactions ledger (DB SSOT)
        "transactions",
        # Contracts/FA ledger (DB SSOT)
        "contracts",
        "player_contracts",
        "active_contract_id_by_player",
        "free_agents",
        # GM profiles (DB SSOT)
        "gm_profiles",
    ):
        workflow_state.pop(k, None)

    # 2) DB snapshot (SSOT). Fail loud on DB path/schema issues.
    db_path = state.get_db_path()
    try:
        with LeagueRepo(db_path) as repo:
            # DB schema is guaranteed during server startup (state.startup_init_state()).
            db_snapshot: Dict[str, Any] = {
                "ok": True,
                "db_path": db_path,
                "trade_assets": repo.get_trade_assets_snapshot(),
                "contracts_ledger": repo.get_contract_ledger_snapshot(),
                "transactions": repo.list_transactions(limit=200),
                "gm_profiles": repo.get_all_gm_profiles(),
            }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "DB snapshot failed",
                "db_path": db_path,
                "error": str(exc),
            },
        )

    return {
        "workflow_state": workflow_state,
        "db_snapshot": db_snapshot,
    }


@app.get("/api/debug/schedule-summary")
async def debug_schedule_summary():
    """마스터 스케줄 생성/검증용 디버그 엔드포인트."""
    return state.get_schedule_summary()















