from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from uuid import uuid4

from state import GAME_STATE

from .errors import TradeError, NEGOTIATION_NOT_FOUND
from .models import Deal, canonicalize_deal, parse_deal, serialize_deal


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def create_session(user_team_id: str, other_team_id: str) -> Dict[str, Any]:
    session_id = str(uuid4())
    session = {
        "session_id": session_id,
        "user_team_id": user_team_id.upper(),
        "other_team_id": other_team_id.upper(),
        "messages": [],
        "status": "ACTIVE",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "draft_deal": None,
        "committed_deal_id": None,
    }
    GAME_STATE.setdefault("negotiations", {})[session_id] = session
    return session


def get_session(session_id: str) -> Dict[str, Any]:
    session = (GAME_STATE.get("negotiations") or {}).get(session_id)
    if not session:
        raise TradeError(
            NEGOTIATION_NOT_FOUND,
            "Negotiation session not found",
            {"session_id": session_id},
        )
    return session


def append_message(session_id: str, speaker: str, text: str) -> None:
    session = get_session(session_id)
    session["messages"].append({"speaker": speaker, "text": text, "at": _now_iso()})
    session["updated_at"] = _now_iso()


def set_draft_deal(session_id: str, deal_serialized: dict) -> None:
    session = get_session(session_id)
    deal: Deal = canonicalize_deal(parse_deal(deal_serialized))
    session["draft_deal"] = serialize_deal(deal)
    session["updated_at"] = _now_iso()


def set_committed(session_id: str, deal_id: str) -> None:
    session = get_session(session_id)
    session["committed_deal_id"] = deal_id
    session["updated_at"] = _now_iso()
