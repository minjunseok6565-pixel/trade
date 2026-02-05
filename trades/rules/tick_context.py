from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id

from . import rule_player_meta


def _canonical_player_id(value: object) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


@dataclass
class TradeRuleTickContext:
    db_path: str
    current_date: date
    repo: LeagueRepo
    ctx_state_base: dict
    assets_snapshot: dict
    season_year: int

    players_meta_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    integrity_validated: bool = False

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
) -> TradeRuleTickContext:
    import state

    resolved_db_path = db_path or state.get_db_path()
    resolved_current_date = current_date or state.get_current_date_as_date()

    repo = LeagueRepo(resolved_db_path)
    integrity_validated = False
    if validate_integrity:
        repo.validate_integrity()
        integrity_validated = True

    ctx_state_base = state.export_trade_context_snapshot(db_path=resolved_db_path)
    assets_snapshot = state.export_trade_assets_snapshot(db_path=resolved_db_path)

    league = (ctx_state_base or {}).get("league")
    if not isinstance(league, dict):
        repo.close()
        raise RuntimeError("Invalid trade context snapshot: missing league dict")

    y = league.get("season_year")
    if y is None:
        repo.close()
        raise RuntimeError("Invalid trade context snapshot: league.season_year missing")

    try:
        season_year = int(y)
    except (TypeError, ValueError) as exc:
        repo.close()
        raise RuntimeError(f"Invalid trade context snapshot: league.season_year invalid: {y!r}") from exc

    return TradeRuleTickContext(
        db_path=str(resolved_db_path),
        current_date=resolved_current_date,
        repo=repo,
        ctx_state_base=ctx_state_base or {},
        assets_snapshot=assets_snapshot or {},
        season_year=season_year,
        players_meta_cache={},
        integrity_validated=integrity_validated,
    )
