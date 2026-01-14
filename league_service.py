from __future__ import annotations

"""league_service.py

Write-oriented orchestration layer.

Design goals:
- Keep LeagueRepo as the standard DB access interface.
- Put *scenario/command* writes (multi-table updates + validation + logging) here.
- Prefer idempotent / safe operations for boot/seed/migration actions.

This file intentionally starts with a small, safe subset of write APIs. More complex
commands (trade commit, draft settlement, contract lifecycle) can be added incrementally.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from league_repo import LeagueRepo


def _today_iso() -> str:
    return date.today().isoformat()


def _coerce_iso(d: date | str | None) -> str:
    if d is None:
        return _today_iso()
    if isinstance(d, str):
        return d
    return d.isoformat()


def _extract_team_ids_from_deal(deal: Any) -> List[str]:
    """Best-effort extraction of team ids from various deal shapes.

    Supports:
    - deal.teams (iterable)
    - dict with 'teams'
    - dict with 'legs' (keys are team ids)
    """
    try:
        teams = getattr(deal, "teams", None)
        if teams:
            return [str(t) for t in list(teams)]
    except Exception:
        pass

    if isinstance(deal, dict):
        teams = deal.get("teams")
        if teams:
            try:
                return [str(t) for t in list(teams)]
            except Exception:
                return [str(teams)]
        legs = deal.get("legs")
        if isinstance(legs, dict) and legs:
            return [str(t) for t in legs.keys()]

    return []


@dataclass(frozen=True)
class ServiceEvent:
    """Small, stable event envelope for write APIs."""

    type: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, **self.payload}


class LeagueService:
    """High-level write API layer built on LeagueRepo."""

    def __init__(self, repo: LeagueRepo):
        self.repo = repo

    # ----------------------------
    # Lifecycle / context helpers
    # ----------------------------
    @classmethod
    @contextmanager
    def open(cls, db_path: str):
        """Open a repo and yield a service bound to it."""
        with LeagueRepo(db_path) as repo:
            # Make all service calls safe even if caller forgot to init explicitly.
            repo.init_db()
            yield cls(repo)

    # ----------------------------
    # (A) Boot / Migration / Seed
    # ----------------------------
    def init_or_migrate_db(self) -> None:
        """Create tables and apply light-weight migrations (safe to call repeatedly)."""
        self.repo.init_db()

    def ensure_gm_profiles_seeded(self, team_ids: Sequence[str]) -> None:
        """Ensure gm_profiles has at least an empty profile row for each team."""
        self.repo.ensure_gm_profiles_seeded(list(team_ids))

    def ensure_draft_picks_seeded(self, draft_year: int, team_ids: Sequence[str], years_ahead: int) -> None:
        """Ensure draft_picks have baseline rows for validation/lookahead."""
        self.repo.ensure_draft_picks_seeded(int(draft_year), list(team_ids), years_ahead=int(years_ahead))

    def ensure_contracts_bootstrapped_from_roster(self, season_year: int) -> None:
        """Ensure roster players have at least a minimal active contract entry."""
        self.repo.ensure_contracts_bootstrapped_from_roster(int(season_year))

    def import_roster_from_excel(
        self,
        excel_path: str,
        *,
        mode: str = "replace",
        sheet_name: Optional[str] = None,
        strict_ids: bool = True,
    ) -> None:
        """Admin import: Excel roster -> SQLite."""
        self.repo.import_roster_excel(
            excel_path,
            mode=mode,
            sheet_name=sheet_name,
            strict_ids=bool(strict_ids),
        )

    # ----------------------------
    # (L) Transactions log
    # ----------------------------
    def append_transaction(self, entry: Mapping[str, Any]) -> Dict[str, Any]:
        """Insert a single transaction entry into transactions_log."""
        d = dict(entry)
        self.repo.insert_transactions([d])
        return d

    def append_transactions(self, entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Insert multiple transaction entries."""
        payloads = [dict(e) for e in entries]
        self.repo.insert_transactions(payloads)
        return payloads

    def log_trade_transaction(
        self,
        deal: Any,
        *,
        source: str,
        trade_date: date | str | None = None,
        deal_id: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Minimal trade log writer (DB).

        This intentionally does *not* assume a specific Deal model shape.
        The raw deal object is stored under payload.deal for traceability.
        """
        entry: Dict[str, Any] = {
            "type": "trade",
            "date": _coerce_iso(trade_date),
            "source": source or "",
            "teams": _extract_team_ids_from_deal(deal),
            "deal_id": deal_id,
            "meta": dict(meta) if meta else {},
            "deal": deal if isinstance(deal, dict) else None,
        }
        # Remove noisy keys if empty
        if entry.get("deal_id") is None:
            entry.pop("deal_id", None)
        if not entry.get("teams"):
            entry.pop("teams", None)
        if not entry.get("meta"):
            entry.pop("meta", None)
        if entry.get("deal") is None:
            entry.pop("deal", None)

        self.repo.insert_transactions([entry])
        return entry

    # ----------------------------
    # (G) GM profile write
    # ----------------------------
    def upsert_gm_profile(self, team_id: str, profile_dict: Mapping[str, Any] | None) -> None:
        self.repo.upsert_gm_profile(team_id, profile_dict)

    def upsert_gm_profiles(self, profiles_by_team: Mapping[str, Mapping[str, Any] | None]) -> None:
        self.repo.upsert_gm_profiles(profiles_by_team)

    # ----------------------------
    # (C) Small contract/roster writes (safe subset)
    # ----------------------------
    def set_player_salary(self, player_id: str, salary_amount: int) -> None:
        """Direct roster salary update."""
        self.repo.set_salary(player_id, int(salary_amount))

    def release_player_to_free_agency(self, player_id: str, released_date: date | str | None = None) -> ServiceEvent:
        """Release player to FA by moving roster.team_id to 'FA'.

        free_agents is derived from roster.team_id == 'FA' by default (SSOT),
        so this method only needs to update the roster (and optionally contracts team sync).
        """
        # released_date currently used only for logging; the roster update is date-agnostic.
        # repo.release_to_free_agency -> repo.trade_player already runs in a transaction.
        self.repo.release_to_free_agency(player_id)

        event = ServiceEvent(
            type="release_to_free_agency",
            payload={
                "date": _coerce_iso(released_date),
                "player_id": str(player_id),
            },
        )
        # Optional: also log it (caller can decide; keeping it explicit for now).
        return event

    # ----------------------------
    # (T / S / C complex) Planned APIs (stubs)
    # ----------------------------
    def execute_trade(
        self,
        deal: Any,
        *,
        source: str,
        trade_date: date | str | None = None,
        deal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Commit a trade to the DB (players + picks/swaps/fixed assets + log).

        Not implemented in this initial safe drop.
        """
        raise NotImplementedError(
            "execute_trade is not implemented yet in LeagueService. "
            "Add full DB commit (players + draft_picks + swap_rights + fixed_assets + transactions_log) incrementally."
        )

    def settle_draft_year(self, draft_year: int, pick_order_by_pick_id: Mapping[str, int]) -> List[Dict[str, Any]]:
        """Settle protections and swap rights for a given draft year (DB)."""
        raise NotImplementedError("settle_draft_year is not implemented yet in LeagueService.")

    def sign_free_agent(
        self,
        team_id: str,
        player_id: str,
        *,
        signed_date: date | str | None = None,
        years: int = 1,
        salary_by_year: Optional[Mapping[int, int]] = None,
    ) -> ServiceEvent:
        """Sign an FA (DB): roster.team_id + contracts + active contract + salary."""
        raise NotImplementedError("sign_free_agent is not implemented yet in LeagueService.")

    def re_sign_or_extend(
        self,
        team_id: str,
        player_id: str,
        *,
        signed_date: date | str | None = None,
        years: int = 1,
        salary_by_year: Optional[Mapping[int, int]] = None,
    ) -> ServiceEvent:
        """Re-sign / extend a player (DB): contracts + active contract + salary."""
        raise NotImplementedError("re_sign_or_extend is not implemented yet in LeagueService.")

    def apply_contract_option_decision(
        self,
        contract_id: str,
        *,
        season_year: int,
        decision: str,
        decision_date: date | str | None = None,
    ) -> ServiceEvent:
        """Apply team/player option decision (DB)."""
        raise NotImplementedError("apply_contract_option_decision is not implemented yet in LeagueService.")

    def expire_contracts_for_season_transition(
        self,
        from_year: int,
        to_year: int,
        *,
        decision_policy: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Expire contracts and optionally release players (DB)."""
        raise NotImplementedError("expire_contracts_for_season_transition is not implemented yet in LeagueService.")


# ----------------------------
# Convenience module-level APIs
# ----------------------------
def init_or_migrate_db(db_path: str) -> None:
    with LeagueService.open(db_path) as svc:
        svc.init_or_migrate_db()


def ensure_gm_profiles_seeded(db_path: str, team_ids: Sequence[str]) -> None:
    with LeagueService.open(db_path) as svc:
        svc.ensure_gm_profiles_seeded(team_ids)


def ensure_draft_picks_seeded(db_path: str, draft_year: int, team_ids: Sequence[str], years_ahead: int) -> None:
    with LeagueService.open(db_path) as svc:
        svc.ensure_draft_picks_seeded(draft_year, team_ids, years_ahead)


def ensure_contracts_bootstrapped_from_roster(db_path: str, season_year: int) -> None:
    with LeagueService.open(db_path) as svc:
        svc.ensure_contracts_bootstrapped_from_roster(season_year)
