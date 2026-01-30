from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple


def compute_recent_signing_banned_until(
    player_state: Dict[str, Any],
    *,
    trade_rules: Optional[Dict[str, Any]],
    season_year: int,
    strict: bool = True,
) -> Tuple[Optional[date], Dict[str, Any]]:
    """Compute banned-until date for a "recent signing / re-sign" restriction.

    Policy (mirrors PlayerEligibilityRule Phase2):
    - Applies if last_contract_action_type in {SIGN_FREE_AGENT, RE_SIGN_OR_EXTEND}
      OR signed_via_free_agency is True.
    - signed_date source: prefer last_contract_action_date, else signed_date.
    - banned_until = max(signed_date + new_fa_sign_ban_days, Dec 15 of season_year)
    """
    tr = trade_rules or {}
    ban_days = _safe_int(tr.get("new_fa_sign_ban_days"), default=90)

    pid = str(player_state.get("player_id") or "")
    ctx = f"player_id={pid or '<unknown>'} recent_signing"

    contract_action_type = player_state.get("last_contract_action_type")
    if contract_action_type is not None and not isinstance(contract_action_type, str):
        if strict:
            raise RuntimeError(f"{ctx}: last_contract_action_type must be str|None, got {type(contract_action_type).__name__}")
        contract_action_type = None

    signed_via_fa = player_state.get("signed_via_free_agency")
    if not isinstance(signed_via_fa, bool):
        if strict:
            raise RuntimeError(f"{ctx}: signed_via_free_agency must be bool, got {type(signed_via_fa).__name__}")
        signed_via_fa = False

    is_recent_signing = contract_action_type in {"SIGN_FREE_AGENT", "RE_SIGN_OR_EXTEND"}
    applies = bool(is_recent_signing or signed_via_fa)

    dec15 = _dec15(season_year, strict=strict, context=ctx)
    evidence: Dict[str, Any] = {
        "applies": applies,
        "signed_date": None,
        "dec15": dec15,
        "ban_days": ban_days,
        "contract_action_type": contract_action_type,
    }
    if not applies:
        return None, evidence

    signed_date_value = (
        player_state.get("last_contract_action_date")
        if player_state.get("last_contract_action_date") is not None
        else player_state.get("signed_date")
    )
    signed_date = _parse_iso_date(signed_date_value, context=f"{ctx} signed_date/last_contract_action_date", strict=strict)
    evidence["signed_date"] = signed_date

    if signed_date is None or dec15 is None:
        # Non-strict mode can end up here; treat as no-ban rather than guessing.
        return None, evidence

    banned_until_days = signed_date + timedelta(days=ban_days)
    banned_until = max(banned_until_days, dec15)
    return banned_until, evidence


def compute_aggregation_banned_until(
    player_state: Dict[str, Any],
    *,
    trade_rules: Optional[Dict[str, Any]],
    strict: bool = True,
) -> Tuple[Optional[date], Dict[str, Any]]:
    """Compute banned-until date for aggregation restriction on recently-traded players.

    Policy (mirrors PlayerEligibilityRule Phase2):
    - Applies if acquired_via_trade is True.
    - acquired_date is required when applies.
    - banned_until = acquired_date + aggregation_ban_days
    """
    tr = trade_rules or {}
    ban_days = _safe_int(tr.get("aggregation_ban_days"), default=60)

    pid = str(player_state.get("player_id") or "")
    ctx = f"player_id={pid or '<unknown>'} aggregation"

    acquired_via_trade = player_state.get("acquired_via_trade")
    if not isinstance(acquired_via_trade, bool):
        if strict:
            raise RuntimeError(f"{ctx}: acquired_via_trade must be bool, got {type(acquired_via_trade).__name__}")
        acquired_via_trade = False

    applies = bool(acquired_via_trade)
    evidence: Dict[str, Any] = {
        "applies": applies,
        "acquired_date": None,
        "ban_days": ban_days,
    }
    if not applies:
        return None, evidence

    acquired_date = _parse_iso_date(player_state.get("acquired_date"), context=f"{ctx} acquired_date", strict=strict)
    evidence["acquired_date"] = acquired_date
    if acquired_date is None:
        return None, evidence
    return acquired_date + timedelta(days=ban_days), evidence


def _dec15(season_year: int, *, strict: bool, context: str) -> Optional[date]:
    try:
        y = int(season_year)
        return date(y, 12, 15)
    except Exception as exc:
        if strict:
            raise RuntimeError(f"{context}: invalid season_year for Dec15: {season_year!r}") from exc
        return None


def _parse_iso_date(value: object, *, context: str, strict: bool) -> Optional[date]:
    """Parse ISO date/datetime string into a date.

    strict=True: missing/unparseable -> RuntimeError (fail-fast)
    strict=False: missing/unparseable -> None
    """
    if value is None:
        if strict:
            raise RuntimeError(f"Required date missing for {context}")
        return None
    s = str(value).strip()
    if len(s) < 10:
        if strict:
            raise RuntimeError(f"Required date invalid for {context}: {value!r}")
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError as exc:
        if strict:
            raise RuntimeError(f"Required date unparseable for {context}: {value!r}") from exc
        return None


def _safe_int(value: object, *, default: int) -> int:
    try:
        v = int(value) if value is not None else int(default)
    except Exception:
        v = int(default)
    if v < 0:
        v = 0
    return v
