from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from league_repo import LeagueRepo

from .errors import TradeError, NEGOTIATION_NOT_FOUND
from .models import Deal, canonicalize_deal, parse_deal, serialize_deal


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _get_db_path() -> str:
    return os.environ.get("LEAGUE_DB_PATH") or "league.db"


def _load_session_row(session_id: str) -> dict:
    db_path = _get_db_path()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        row = repo.get_negotiation(session_id)
        if not row:
            raise TradeError(
                NEGOTIATION_NOT_FOUND,
                "Negotiation session not found",
                {"session_id": session_id},
            )
        payload = json.loads(row["session_json"] or "{}")
        if not isinstance(payload, dict):
            raise TradeError(
                NEGOTIATION_NOT_FOUND,
                "Negotiation session not found",
                {"session_id": session_id},
            )
        return payload


def _persist_session(session: Dict[str, Any]) -> None:
    db_path = _get_db_path()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            repo.update_negotiation(
                session_id=session["session_id"],
                session_json=json.dumps(session),
                status=session.get("status", "ACTIVE"),
                updated_at=session.get("updated_at") or _now_iso(),
                cursor=cur,
            )
        repo.validate_integrity()


def _ensure_session_schema(session: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure negotiation sessions include new schema defaults."""
    default_relationship = {"trust": 0, "fatigue": 0, "promises_broken": 0}
    default_summary = {"text": "", "updated_at": None}

    messages = session.get("messages")
    if not isinstance(messages, list):
        session["messages"] = []

    session.setdefault("phase", "INIT")
    if not isinstance(session.get("phase"), str):
        session["phase"] = "INIT"

    session.setdefault("last_offer", None)
    session.setdefault("last_counter", None)

    session.setdefault("constraints", {})
    if not isinstance(session.get("constraints"), dict):
        session["constraints"] = {}

    session.setdefault("valid_until", None)
    valid_until = session.get("valid_until")
    if valid_until is not None and not isinstance(valid_until, str):
        session["valid_until"] = None

    session.setdefault("summary", dict(default_summary))
    summary = session.get("summary")
    if not isinstance(summary, dict):
        summary = dict(default_summary)
        session["summary"] = summary
    summary.setdefault("text", "")
    summary.setdefault("updated_at", None)

    session.setdefault("relationship", dict(default_relationship))
    relationship = session.get("relationship")
    if not isinstance(relationship, dict):
        relationship = dict(default_relationship)
        session["relationship"] = relationship
    relationship.setdefault("trust", 0)
    relationship.setdefault("fatigue", 0)
    relationship.setdefault("promises_broken", 0)

    session.setdefault("market_context", {})
    if not isinstance(session.get("market_context"), dict):
        session["market_context"] = {}

    return session


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
        "phase": "INIT",  # negotiation FSM phase
        "last_offer": None,  # last deal payload offered
        "last_counter": None,  # last counter-offer payload
        "constraints": {},  # negotiation constraints metadata
        "valid_until": None,  # ISO expiry or None
        "summary": {"text": "", "updated_at": None},  # session summary metadata
        "relationship": {"trust": 0, "fatigue": 0, "promises_broken": 0},
        "market_context": {},  # trade market context snapshot
    }
    db_path = _get_db_path()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            repo.save_negotiation(
                session_id=session_id,
                session_json=json.dumps(session),
                status=session["status"],
                created_at=session["created_at"],
                updated_at=session["updated_at"],
                cursor=cur,
            )
        repo.validate_integrity()
    return session


def get_session(session_id: str) -> Dict[str, Any]:
    session = _load_session_row(session_id)
    return _ensure_session_schema(session)


def append_message(session_id: str, speaker: str, text: str) -> None:
    session = get_session(session_id)
    session["messages"].append({"speaker": speaker, "text": text, "at": _now_iso()})
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_draft_deal(session_id: str, deal_serialized: dict) -> None:
    session = get_session(session_id)
    deal: Deal = canonicalize_deal(parse_deal(deal_serialized))
    session["draft_deal"] = serialize_deal(deal)
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_committed(session_id: str, deal_id: str) -> None:
    session = get_session(session_id)
    session["committed_deal_id"] = deal_id
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_phase(session_id: str, phase: str) -> None:
    session = get_session(session_id)
    session["phase"] = phase if isinstance(phase, str) else "INIT"
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_constraints(session_id: str, constraints: dict) -> None:
    session = get_session(session_id)
    session["constraints"] = constraints if isinstance(constraints, dict) else {}
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_valid_until(session_id: str, valid_until_iso: Optional[str]) -> None:
    session = get_session(session_id)
    if valid_until_iso is not None and not isinstance(valid_until_iso, str):
        valid_until_iso = None
    session["valid_until"] = valid_until_iso
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_summary(session_id: str, summary: dict) -> None:
    session = get_session(session_id)
    if not isinstance(summary, dict):
        summary = {"text": "", "updated_at": None}
    summary.setdefault("text", "")
    summary.setdefault("updated_at", None)
    session["summary"] = summary
    session["updated_at"] = _now_iso()
    _persist_session(session)


def bump_fatigue(session_id: str, delta: int = 1) -> None:
    session = get_session(session_id)
    try:
        increment = int(delta)
    except (TypeError, ValueError):
        increment = 1
    relationship = session.get("relationship")
    if not isinstance(relationship, dict):
        relationship = {"trust": 0, "fatigue": 0, "promises_broken": 0}
        session["relationship"] = relationship
    relationship["fatigue"] = int(relationship.get("fatigue", 0)) + increment
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_relationship(session_id: str, patch: dict) -> None:
    session = get_session(session_id)
    relationship = session.get("relationship")
    if not isinstance(relationship, dict):
        relationship = {"trust": 0, "fatigue": 0, "promises_broken": 0}
        session["relationship"] = relationship
    if isinstance(patch, dict):
        for key in ("trust", "fatigue", "promises_broken"):
            if key in patch:
                relationship[key] = patch[key]
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_last_offer(session_id: str, payload: Any) -> None:
    session = get_session(session_id)
    session["last_offer"] = payload
    session["updated_at"] = _now_iso()
    _persist_session(session)


def set_last_counter(session_id: str, payload: Any) -> None:
    session = get_session(session_id)
    session["last_counter"] = payload
    session["updated_at"] = _now_iso()
    _persist_session(session)
