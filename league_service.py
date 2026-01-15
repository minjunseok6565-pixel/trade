from __future__ import annotations

import contextlib
import datetime as _dt
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Literal

from league_repo import LeagueRepo


class LeagueServiceError(RuntimeError):
    """Base error for service-layer operations."""


class IdempotencyError(LeagueServiceError):
    """Raised when an operation violates idempotency constraints."""


class AlreadyCommittedError(IdempotencyError):
    """Raised when a deal_id (idempotency key) was already committed."""


def _utc_today_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).date().isoformat()


def _normalize_date(value: Any) -> Optional[str]:
    """Normalize date-like inputs to ISO date string (YYYY-MM-DD)."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    # Best-effort string conversion
    s = str(value).strip()
    return s or None


class LeagueService:
    """Service-layer orchestrator.

    - Owns transaction boundaries for multi-table writes.
    - Delegates SQL persistence to LeagueRepo.
    - Central place for idempotency / validation / logging.
    """

    def __init__(self, db_path: str | Path, repo_factory=LeagueRepo):
        self.db_path = str(db_path)
        self.repo_factory = repo_factory

    # -------------------------
    # Repo / Transaction helpers
    # -------------------------

    @contextlib.contextmanager
    def _repo(self) -> Iterator[LeagueRepo]:
        repo = self.repo_factory(self.db_path)
        try:
            yield repo
        finally:
            repo.close()

    @contextlib.contextmanager
    def _tx(self, repo: LeagueRepo, *, write: bool = True):
        # Service owns the transaction boundary.
        with repo.transaction(write=write) as cur:
            yield cur

    # -------------------------
    # Common utilities
    # -------------------------

    def _now_date(self) -> str:
        return _utc_today_iso()

    def _deal_id_exists(self, cur, deal_id: str) -> bool:
        did = (deal_id or "").strip()
        if not did:
            return False
        row = cur.execute(
            "SELECT 1 FROM transactions_log WHERE deal_id = ? LIMIT 1;",
            (did,),
        ).fetchone()
        return row is not None

    def _guard_deal_id(
        self,
        cur,
        deal_id: Optional[str],
        *,
        mode: Literal["raise", "skip"] = "raise",
    ) -> bool:
        """Guard against duplicate commits for the same deal_id.

        Returns True if the caller should skip the operation (mode='skip' and deal exists).
        Raises AlreadyCommittedError if mode='raise' and deal exists.
        """
        did = (deal_id or "").strip()
        if not did:
            return False
        if self._deal_id_exists(cur, did):
            if mode == "raise":
                raise AlreadyCommittedError(f"deal_id already committed: {did}")
            return True
        return False

    def _append_transaction(self, repo: LeagueRepo, cur, entry_dict: Mapping[str, Any]) -> None:
        if not isinstance(entry_dict, Mapping):
            return
        entry = self._normalize_tx_entry(dict(entry_dict))
        repo.insert_transactions([entry], cur=cur)

    def _append_transactions(self, repo: LeagueRepo, cur, entries: Sequence[Mapping[str, Any]]) -> None:
        normalized: List[Dict[str, Any]] = [
            self._normalize_tx_entry(dict(e)) for e in entries if isinstance(e, Mapping)
        ]
        if not normalized:
            return
        repo.insert_transactions(normalized, cur=cur)

    def _normalize_tx_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize transaction entry to a stable minimal shape.

        Repo stores the full payload_json (this dict), but also denormalizes:
        - type  -> tx_type
        - date  -> tx_date
        - deal_id, source, teams -> columns

        So we guarantee these keys exist with sane defaults.
        """
        # type
        tx_type = entry.get("type")
        if tx_type is None or str(tx_type).strip() == "":
            entry["type"] = "unknown"
        else:
            entry["type"] = str(tx_type)

        # date (ISO preferred)
        if entry.get("date") is None or str(entry.get("date")).strip() == "":
            entry["date"] = self._now_date()
        else:
            entry["date"] = _normalize_date(entry.get("date")) or self._now_date()

        # teams must be JSON-serializable list
        teams = entry.get("teams")
        if teams is None:
            entry["teams"] = []
        elif isinstance(teams, list):
            # ok
            pass
        elif isinstance(teams, (tuple, set)):
            entry["teams"] = list(teams)
        else:
            entry["teams"] = [teams]

        # deal_id/source are kept as-is (repo will stringify); don't force required here.
        return entry

    def append_transaction(self, entry_dict: Mapping[str, Any]) -> None:
        """Public: append a single transaction entry."""
        with self._repo() as repo:
            repo.init_db()
            with self._tx(repo, write=True) as cur:
                self._append_transaction(repo, cur, entry_dict)

    def append_transactions(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """Public: append multiple transaction entries."""
        with self._repo() as repo:
            repo.init_db()
            with self._tx(repo, write=True) as cur:
                self._append_transactions(repo, cur, entries)

    # -------------------------
    # Phase L: 트랜잭션 로그 (L1)
    # -------------------------

    def _build_trade_transaction_entry(
        self,
        deal: Any,
        *,
        source: str,
        trade_date: Any = None,
        deal_id: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Best-effort trade transaction entry builder.

        - deal can be a dict-like payload or an object; we store a serializable snapshot.
        - teams is extracted if possible; otherwise empty list (still valid).
        """
        # Extract teams
        teams: List[Any] = []
        if hasattr(deal, "teams"):
            try:
                teams = list(getattr(deal, "teams"))  # type: ignore[arg-type]
            except Exception:
                teams = []
        elif isinstance(deal, Mapping):
            for key in ("teams", "team_ids", "teamIds"):
                if key in deal:
                    try:
                        teams = list(deal.get(key) or [])
                    except Exception:
                        teams = []
                    break

        # Snapshot deal payload
        if isinstance(deal, Mapping):
            deal_payload: Any = dict(deal)
        elif hasattr(deal, "to_dict"):
            try:
                deal_payload = deal.to_dict()  # type: ignore[attr-defined]
            except Exception:
                deal_payload = str(deal)
        elif hasattr(deal, "dict"):
            try:
                deal_payload = deal.dict()  # type: ignore[attr-defined]
            except Exception:
                deal_payload = str(deal)
        else:
            deal_payload = str(deal)

        entry: Dict[str, Any] = {
            "type": "trade",
            "date": _normalize_date(trade_date) or self._now_date(),
            "teams": teams,
            "source": source,
            "deal": deal_payload,
        }
        if deal_id:
            entry["deal_id"] = str(deal_id)
        if meta:
            entry["meta"] = dict(meta)
        return entry

    def log_trade_transaction(
        self,
        deal: Any,
        *,
        source: str,
        trade_date: Any = None,
        deal_id: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Wrapper: build trade log entry then append it.

        IMPORTANT: By design, this wrapper calls append_transaction only.
        (Atomicity with other writes should be handled by the calling command API,
         which should use _append_transaction within its own transaction.)
        """
        entry = self._build_trade_transaction_entry(
            deal,
            source=source,
            trade_date=trade_date,
            deal_id=deal_id,
            meta=meta,
        )
        self.append_transaction(entry)
        return entry

    # -------------------------
    # Phase A: 운영/부팅성 Write
    # -------------------------

    @classmethod
    def init_or_migrate_db(cls, db_path: str | Path, repo_factory=LeagueRepo) -> None:
        """Initialize DB schema and apply in-place migrations.

        Currently delegates to LeagueRepo.init_db(), which is idempotent and performs
        column backfills and index creation.
        """
        repo = repo_factory(str(db_path))
        try:
            repo.init_db()
        finally:
            repo.close()

    def ensure_gm_profiles_seeded(
        self,
        team_ids: Iterable[str],
        *,
        default_profile: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self._repo() as repo:
            repo.init_db()
            with self._tx(repo, write=True) as cur:
                repo.ensure_gm_profiles_seeded(team_ids, default_profile=default_profile, cur=cur)

    def ensure_draft_picks_seeded(
        self,
        draft_year: int,
        team_ids: Iterable[str],
        years_ahead: int,
    ) -> None:
        with self._repo() as repo:
            repo.init_db()
            with self._tx(repo, write=True) as cur:
                repo.ensure_draft_picks_seeded(
                    int(draft_year),
                    team_ids,
                    years_ahead=int(years_ahead),
                    cur=cur,
                )

    def ensure_contracts_bootstrapped_from_roster(self, season_year: int) -> None:
        with self._repo() as repo:
            repo.init_db()
            with self._tx(repo, write=True) as cur:
                repo.ensure_contracts_bootstrapped_from_roster(int(season_year), cur=cur)

    def import_roster_from_excel(
        self,
        excel_path: str | Path,
        *,
        mode: str = "replace",
        sheet_name: Optional[str] = None,
        strict_ids: bool = True,
    ) -> None:
        """Admin import of roster data from Excel.

        Note: schema init must happen outside the import transaction because init_db() runs executescript().
        """
        with self._repo() as repo:
            repo.init_db()
            with self._tx(repo, write=True) as cur:
                repo.import_roster_excel(
                    str(excel_path),
                    sheet_name=sheet_name,
                    mode=mode,
                    strict_ids=bool(strict_ids),
                    cur=cur,
                )
