from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

import state

from .errors import TradeError, NEGOTIATION_NOT_FOUND
from .models import Deal, canonicalize_deal, parse_deal, serialize_deal

NEGOTIATION_BAD_PAYLOAD = "NEGOTIATION_BAD_PAYLOAD"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


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


def _load_session_or_404(session_id: str) -> Dict[str, Any]:
    session = state.negotiation_session_get(session_id)
    if not session:
        raise TradeError(
            NEGOTIATION_NOT_FOUND,
            "Negotiation session not found",
            {"session_id": session_id},
        )
    return _ensure_session_schema(session)


def _atomic_update(session_id: str, patch_fn) -> Dict[str, Any]:
    """Apply a mutation to a single session atomically and persist it."""

    def _mut(session: Dict[str, Any]) -> None:
        _ensure_session_schema(session)
        patch_fn(session)
        session["updated_at"] = _now_iso()

    try:
        return state.negotiation_session_update(session_id, _mut)
    except KeyError:
        raise TradeError(
            NEGOTIATION_NOT_FOUND,
            "Negotiation session not found",
            {"session_id": session_id},
        )


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
    state.negotiation_session_put(session_id, session)
    return session


def get_session(session_id: str) -> Dict[str, Any]:
    return _load_session_or_404(session_id)


def append_message(session_id: str, speaker: str, text: str) -> None:
    at = _now_iso()
    msg = {"speaker": speaker, "text": text, "at": at}

    def _patch(session: Dict[str, Any]) -> None:
        session["messages"].append(msg)

    _atomic_update(session_id, _patch)


def set_draft_deal(session_id: str, deal_serialized: dict) -> None:
    deal: Deal = canonicalize_deal(parse_deal(deal_serialized))
    deal_payload = serialize_deal(deal)

    def _patch(session: Dict[str, Any]) -> None:
        session["draft_deal"] = deal_payload

    _atomic_update(session_id, _patch)


def set_committed(session_id: str, deal_id: str) -> None:
    def _patch(session: Dict[str, Any]) -> None:
        session["committed_deal_id"] = deal_id

    _atomic_update(session_id, _patch)


def set_phase(session_id: str, phase: str) -> None:
    phase_value = phase if isinstance(phase, str) else "INIT"

    def _patch(session: Dict[str, Any]) -> None:
        session["phase"] = phase_value

    _atomic_update(session_id, _patch)


def set_constraints(session_id: str, constraints: dict) -> None:
    constraints_value = constraints if isinstance(constraints, dict) else {}

    def _patch(session: Dict[str, Any]) -> None:
        session["constraints"] = constraints_value

    _atomic_update(session_id, _patch)


def set_valid_until(session_id: str, valid_until_iso: Optional[str]) -> None:
    if valid_until_iso is not None and not isinstance(valid_until_iso, str):
        valid_until_iso = None

    def _patch(session: Dict[str, Any]) -> None:
        session["valid_until"] = valid_until_iso

    _atomic_update(session_id, _patch)


def set_summary(session_id: str, summary: dict) -> None:
    if not isinstance(summary, dict):
        summary = {"text": "", "updated_at": None}
    summary.setdefault("text", "")
    summary.setdefault("updated_at", None)

    def _patch(session: Dict[str, Any]) -> None:
        session["summary"] = summary

    _atomic_update(session_id, _patch)


def bump_fatigue(session_id: str, delta: int = 1) -> None:
    try:
        increment = int(delta)
    except (TypeError, ValueError):
        increment = 1

    def _patch(session: Dict[str, Any]) -> None:
        relationship = session.get("relationship")
        if not isinstance(relationship, dict):
            relationship = {"trust": 0, "fatigue": 0, "promises_broken": 0}
            session["relationship"] = relationship
        relationship["fatigue"] = int(relationship.get("fatigue", 0)) + increment

    _atomic_update(session_id, _patch)


def set_relationship(session_id: str, patch: dict) -> None:
    def _patch(session: Dict[str, Any]) -> None:
        relationship = session.get("relationship")
        if not isinstance(relationship, dict):
            relationship = {"trust": 0, "fatigue": 0, "promises_broken": 0}
            session["relationship"] = relationship
        if isinstance(patch, dict):
            for key in ("trust", "fatigue", "promises_broken"):
                if key in patch:
                    relationship[key] = patch[key]

    _atomic_update(session_id, _patch)


def set_last_offer(session_id: str, payload: Any) -> None:
    try:
        json.dumps(payload)
    except (TypeError, OverflowError):
        raise TradeError(
            NEGOTIATION_BAD_PAYLOAD,
            "last_offer payload must be JSON-serializable",
            {"session_id": session_id},
        )

    def _patch(session: Dict[str, Any]) -> None:
        session["last_offer"] = payload

    _atomic_update(session_id, _patch)


def set_last_counter(session_id: str, payload: Any) -> None:
    try:
        json.dumps(payload)
    except (TypeError, OverflowError):
        raise TradeError(
            NEGOTIATION_BAD_PAYLOAD,
            "last_counter payload must be JSON-serializable",
            {"session_id": session_id},
        )

    def _patch(session: Dict[str, Any]) -> None:
        session["last_counter"] = payload

    _atomic_update(session_id, _patch)
