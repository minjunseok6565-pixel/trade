from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, List

import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BASE_DIR, ALL_TEAM_IDS
from league_repo import LeagueRepo
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
from team_utils import get_conference_standings, get_team_cards, get_team_detail
from season_report_ai import generate_season_report
from trades.errors import TradeError
from trades.models import canonicalize_deal, parse_deal, serialize_deal
from trades.validator import validate_deal
from trades.apply import apply_deal_to_db
from trades import agreements
from trades import negotiation_store


# -------------------------------------------------------------------------
# FastAPI 앱 생성 및 기본 설정
# -------------------------------------------------------------------------
app = FastAPI(title="느바 시뮬 GM 서버")

@app.on_event("startup")
def _startup_init_state() -> None:
    # Startup-only bootstraps (agreed policy):
    # 1) DB init + seed once
    # 2) players/teams cache init + player_id normalize once
    # 3) repo integrity validate once
    # 4) ingest_turn backfill once
    state.startup_init_state()

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
    """matchengine_v3를 사용해 한 경기를 시뮬레이션한다."""
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

def _require_db_path() -> str:
    db_path = os.environ.get("LEAGUE_DB_PATH") or state.get_db_path()
    if not db_path:
        raise HTTPException(status_code=500, detail="db_path is required for trade operations")
    state.set_db_path(db_path)
    return db_path

def _validate_repo_integrity(db_path: str) -> None:
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        repo.validate_integrity()


@app.post("/api/trade/submit")
async def api_trade_submit(req: TradeSubmitRequest):
    try:
        in_game_date = state.get_current_date_as_date()
        db_path = _require_db_path()
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
        db_path = _require_db_path()
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
        _require_db_path()
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
        committed = agreements.create_committed_deal(
            deal,
            valid_days=2,
            current_date=in_game_date,
        )
        negotiation_store.set_draft_deal(req.session_id, serialize_deal(deal))
        negotiation_store.set_committed(req.session_id, committed["deal_id"])
        return {
            "ok": True,
            "deal_id": committed["deal_id"],
            "expires_at": committed["expires_at"],
            "deal": serialize_deal(deal),
        }
    except TradeError as exc:
        return _trade_error_response(exc)


# -------------------------------------------------------------------------
# 로스터 요약 API (LLM 컨텍스트용)
# -------------------------------------------------------------------------
@app.get("/api/roster-summary/{team_id}")
async def roster_summary(team_id: str):
    """특정 팀의 로스터를 LLM이 보기 좋은 형태로 요약해서 돌려준다."""
    db_path = _require_db_path()
    team_id = str(normalize_team_id(team_id, strict=True))
    with LeagueRepo(db_path) as repo:
        repo.init_db()
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

    # 마스터 스케줄이 없다면 생성
    state.initialize_master_schedule_if_needed()
    league = state.export_full_state_snapshot().get("league", {})
    master_schedule = league.get("master_schedule", {})
    games = master_schedule.get("games") or []

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
    db_path = _require_db_path()
    try:
        with LeagueRepo(db_path) as repo:
            repo.init_db()
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



