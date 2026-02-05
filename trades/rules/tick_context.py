from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, Optional, TYPE_CHECKING

from league_repo import LeagueRepo
from schema import normalize_player_id

from . import rule_player_meta

if TYPE_CHECKING:
    from .base import Rule

class _ReadOnlyDict(dict):
    """
    A dict subtype that raises on mutation.
    Used to enforce tick snapshot immutability while preserving isinstance(x, dict) checks.
    """

    def _ro(self, *a, **k):
        raise TypeError("TradeRuleTickContext snapshot is read-only")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _ro


def _canonical_player_id(value: object) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


@dataclass
class TradeRuleTickContext:
    db_path: str
    current_date: date
    repo: LeagueRepo
    owns_repo: bool = True
    ctx_state_base: dict
    assets_snapshot: dict
    season_year: int

    players_meta_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    integrity_validated: bool = False
    # Prepared once per tick: enabled rules sorted by (priority, rule_id)
    prepared_rules: list["Rule"] = field(default_factory=list)

    def ensure_players_meta(self, player_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        canonical: list[str] = []
        seen: set[str] = set()
        for pid in player_ids:
            if pid is None:
                continue
            s = str(pid).strip()
            if not s:
                continue
            cid = _canonical_player_id(s)
            if cid in seen:
                continue
            seen.add(cid)
            canonical.append(cid)

        if not canonical:
            return {}

        missing = [pid for pid in canonical if pid not in self.players_meta_cache]
        if missing:
            built = rule_player_meta.build_rule_players_meta(
                self.repo,
                missing,
                season_year=self.season_year,
                as_of_date=self.current_date,
            )
            self.players_meta_cache.update(built or {})

        # 반환은 “요청한 canonical ids” 범위로 제한
        return {pid: self.players_meta_cache[pid] for pid in canonical if pid in self.players_meta_cache}

    def close(self) -> None:
        if not self.owns_repo:
            return
        try:
            self.repo.close()
        except Exception:
            pass

    def __enter__(self) -> "TradeRuleTickContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def build_trade_rule_tick_context(
    *,
    current_date: Optional[date] = None,
    db_path: Optional[str] = None,
    validate_integrity: bool = True,
    repo: Optional[LeagueRepo] = None,
) -> TradeRuleTickContext:
    import state

    from .registry import get_default_registry

    resolved_current_date = current_date or state.get_current_date_as_date()

    owns_repo = True
    if repo is not None:
        if db_path is not None and str(db_path) != str(getattr(repo, "db_path", "")):
            raise ValueError(
                f"build_trade_rule_tick_context: db_path mismatch (db_path={db_path!r}, repo.db_path={getattr(repo, 'db_path', None)!r})"
            )
        resolved_db_path = str(getattr(repo, "db_path", ""))
        owns_repo = False
    else:
        resolved_db_path = db_path or state.get_db_path()
        repo = LeagueRepo(resolved_db_path)
        owns_repo = True

    integrity_validated = False
    if validate_integrity:
        repo.validate_integrity()
        integrity_validated = True

    # Use the shared repo for tick snapshots to avoid reopening connections.
    ctx_state_base = state.export_trade_context_snapshot(repo=repo) or {}
    assets_snapshot = state.export_trade_assets_snapshot(repo=repo) or {}

    # Enforce immutability for known-mutable submaps to avoid cross-deal contamination.
    # (Rules should be pure; maintenance/cleanup happens at tick boundaries.)
    try:
        al = ctx_state_base.get("asset_locks")
        if isinstance(al, dict) and not isinstance(al, _ReadOnlyDict):
            ctx_state_base["asset_locks"] = _ReadOnlyDict(al)
    except Exception:
        pass

    league = (ctx_state_base or {}).get("league")
    if not isinstance(league, dict):
        if owns_repo:
            repo.close()
        raise RuntimeError("Invalid trade context snapshot: missing league dict")

    y = league.get("season_year")
    if y is None:
        if owns_repo:
            repo.close()
        raise RuntimeError("Invalid trade context snapshot: league.season_year missing")

    try:
        season_year = int(y)
    except (TypeError, ValueError) as exc:
        if owns_repo:
            repo.close()
        raise RuntimeError(f"Invalid trade context snapshot: league.season_year invalid: {y!r}") from exc

    # Prepare sorted enabled rules once (avoid per-deal registry build + sort)
    registry = get_default_registry()
    enabled = [r for r in registry.list_rules() if getattr(r, "enabled", False)]
    prepared_rules = sorted(enabled, key=lambda r: (getattr(r, "priority", 0), getattr(r, "rule_id", "")))

    
    return TradeRuleTickContext(
        db_path=str(resolved_db_path),
        current_date=resolved_current_date,
        repo=repo,
        owns_repo=owns_repo,
        ctx_state_base=ctx_state_base,
        assets_snapshot=assets_snapshot,
        season_year=season_year,
        players_meta_cache={},
        integrity_validated=integrity_validated,
        prepared_rules=prepared_rules,
    )
