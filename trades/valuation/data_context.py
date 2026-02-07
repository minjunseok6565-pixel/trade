from __future__ import annotations

"""data_context.py

DB/Repo IO layer for valuation.
- Implements ValuationDataProvider Protocol (types.py) so valuation engines remain pure.
- Reads a consistent snapshot of trade assets (draft_picks/swap_rights/fixed_assets) and
  contract ledger info, and provides lazy/cached snapshot resolvers.

Design goals
------------
1) Valuation engines (market_pricing/team_utility/deal_evaluator/package_effects/decision_policy)
   MUST NOT depend on DB. They only depend on ValuationDataProvider.
2) This module MAY depend on LeagueRepo (sqlite) and performs reads/caching.
3) No team_situation re-evaluation here; this module only provides raw data.

Pick expectations
-----------------
market_pricing can use PickExpectation (expected pick number) if available.
This module provides simple helpers to build expectations from standings order.
"""

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .types import (
    PlayerId,
    TeamId,
    PickId,
    SwapId,
    FixedAssetId,
    PlayerSnapshot,
    PickSnapshot,
    SwapSnapshot,
    FixedAssetSnapshot,
    ContractSnapshot,
    ContractOptionSnapshot,
    PickExpectation,
    PickExpectationMap,
    ValuationDataProvider,
)

# LeagueRepo lives at project root in the main repo.
# Keep import flexible to survive refactors / local package layouts.
try:  # main project layout
    from league_repo import LeagueRepo  # type: ignore
except Exception:  # pragma: no cover
    try:  # package-relative fallback
        from ..league_repo import LeagueRepo  # type: ignore
    except Exception:  # pragma: no cover
        LeagueRepo = None  # type: ignore


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def _safe_float(x: Any, default: float | None = 0.0) -> float | None:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return int(default)
        return int(x)
    except Exception:
        return int(default)


def _as_upper_team_id(x: Any) -> Optional[str]:
    if x is None:
        return None
    try:
        return str(x).upper()
    except Exception:
        return None


def _coerce_salary_by_year(obj: Any) -> Dict[int, float]:
    if not isinstance(obj, Mapping):
        return {}
    out: Dict[int, float] = {}
    for k, v in obj.items():
        try:
            ky = int(k)
        except Exception:
            continue
        out[ky] = float(_safe_float(v, 0.0) or 0.0)
    return out


def _coerce_options(obj: Any) -> List[ContractOptionSnapshot]:
    if not isinstance(obj, list):
        return []
    out: List[ContractOptionSnapshot] = []
    for o in obj:
        if not isinstance(o, Mapping):
            continue
        out.append(
            ContractOptionSnapshot(
                season_year=_safe_int(o.get("season_year") or o.get("year"), 0),
                type=str(o.get("type") or o.get("option_type") or ""),
                status=str(o.get("status") or ""),
                decision_date=(str(o.get("decision_date")) if o.get("decision_date") is not None else None),
            )
        )
    return out


def contract_snapshot_from_dict(d: Mapping[str, Any], *, current_season_year: Optional[int] = None) -> ContractSnapshot:
    """Convert a LeagueRepo contract dict -> ContractSnapshot."""
    salary_by_year = _coerce_salary_by_year(d.get("salary_by_year") or d.get("salary_by_season") or {})
    opts = _coerce_options(d.get("options") or [])
    start_season_year = _safe_int(d.get("start_season_year"), 0)
    years = _safe_int(d.get("years"), 0)

    meta: Dict[str, Any] = dict(d.get("meta") or {})
    # Optional derived helper: remaining_years
    if current_season_year is not None and salary_by_year:
        remaining = sum(
            1
            for y, v in salary_by_year.items()
            if int(y) >= int(current_season_year) and float(_safe_float(v, 0.0) or 0.0) > 0
        )
        meta.setdefault("remaining_years", float(remaining))

    return ContractSnapshot(
        contract_id=str(d.get("contract_id") or ""),
        player_id=str(d.get("player_id") or ""),
        team_id=_as_upper_team_id(d.get("team_id")),
        status=str(d.get("status") or ""),
        signed_date=(str(d.get("signed_date")) if d.get("signed_date") is not None else None),
        start_season_year=start_season_year,
        years=years,
        salary_by_year=salary_by_year,
        options=opts,
        meta=meta,
    )


# -----------------------------------------------------------------------------
# Pick expectation helpers
# -----------------------------------------------------------------------------

def build_pick_expectations_from_standings(
    draft_picks_map: Mapping[str, Mapping[str, Any]],
    *,
    standings_order_worst_to_best: Sequence[TeamId],
    confidence: float = 0.65,
    year_filter: Optional[int] = None,
) -> PickExpectationMap:
    """Simple expectation builder.

    - standings_order_worst_to_best: [WAS, DET, ... , BOS] length should be ~30.
    - For each pick, expected_pick_number is rank within round: 1..30 (worst->best).
    - Ignores lottery variance; that's for later.
    """
    index: Dict[str, int] = {str(t).upper(): i for i, t in enumerate(standings_order_worst_to_best)}
    out: PickExpectationMap = {}
    for pick_id, p in draft_picks_map.items():
        try:
            year = int(p.get("year"))
        except Exception:
            year = None
        if year_filter is not None and year is not None and int(year) != int(year_filter):
            continue

        org = str(p.get("original_team") or "").upper()
        if org not in index:
            continue
        expected = float(index[org] + 1)  # 1..30
        out[str(pick_id)] = PickExpectation(
            pick_id=str(pick_id),
            expected_pick_number=expected,
            confidence=float(confidence),
            meta={"method": "standings_order", "original_team": org},
        )
    return out


# -----------------------------------------------------------------------------
# Data context (Provider)
# -----------------------------------------------------------------------------

@dataclass(slots=True)
class RepoValuationDataContext(ValuationDataProvider):
    """Concrete ValuationDataProvider backed by LeagueRepo.

    Notes
    -----
    - Uses lazy loading for players (deal evaluation touches a small subset).
    - Uses pre-loaded trade_assets snapshot maps for picks/swaps/fixed assets.
    - Uses contract ledger snapshot maps to attach active contract info to PlayerSnapshot.
    """

    db_path: str
    current_season_year: int
    current_date_iso: str

    # optional shared repo (tick-level context can keep one repo open)
    # NOTE: caller owns lifecycle; provider must not close it.
    repo: Optional["LeagueRepo"] = field(default=None, repr=False)

    # snapshot maps (SSOT: repo.get_trade_assets_snapshot())
    draft_picks_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    swap_rights_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    fixed_assets_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # contract ledger snapshot maps (SSOT: repo.get_contract_ledger_snapshot())
    contracts_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    active_contract_id_by_player: Dict[str, str] = field(default_factory=dict)

    # optional pick expectations
    pick_expectations: PickExpectationMap = field(default_factory=dict)

    # caches
    _player_cache: Dict[str, PlayerSnapshot] = field(default_factory=dict)
    _pick_cache: Dict[str, PickSnapshot] = field(default_factory=dict)
    _swap_cache: Dict[str, SwapSnapshot] = field(default_factory=dict)
    _fixed_cache: Dict[str, FixedAssetSnapshot] = field(default_factory=dict)

    # ---- provider API ----
    def get_player_snapshot(self, player_id: PlayerId) -> PlayerSnapshot:
        pid = str(player_id)
        if pid in self._player_cache:
            return self._player_cache[pid]

        if LeagueRepo is None:  # pragma: no cover
            raise ImportError("LeagueRepo import failed; cannot build player snapshot")

        if self.repo is not None:
            repo_obj = self.repo
            p = repo_obj.get_player(pid)
            # roster team & salary may not exist for FAs
            team_id: Optional[str]
            try:
                team_id = repo_obj.get_team_id_by_player(pid)
            except Exception:
                team_id = None

            try:
                salary_amount = repo_obj.get_salary_amount(pid)
            except Exception:
                salary_amount = None
        else:
            with LeagueRepo(self.db_path) as repo_obj:
                p = repo_obj.get_player(pid)
                # roster team & salary may not exist for FAs
                team_id: Optional[str]
                try:
                    team_id = repo_obj.get_team_id_by_player(pid)
                except Exception:
                    team_id = None

                try:
                    salary_amount = repo_obj.get_salary_amount(pid)
                except Exception:
                    salary_amount = None

        attrs = p.get("attrs") if isinstance(p, dict) else {}
        if not isinstance(attrs, dict):
            attrs = {}

        # active contract attach (if known)
        contract: Optional[ContractSnapshot] = None
        cid = self.active_contract_id_by_player.get(pid)
        if cid and cid in self.contracts_map:
            try:
                contract = contract_snapshot_from_dict(
                    self.contracts_map[cid],
                    current_season_year=self.current_season_year,
                )
            except Exception:
                contract = None

        snap = PlayerSnapshot(
            kind="player",
            player_id=pid,
            name=(str(p.get("name")) if p.get("name") is not None else None),
            pos=(str(p.get("pos")) if p.get("pos") is not None else None),
            age=_safe_float(p.get("age"), None),
            ovr=_safe_float(p.get("ovr"), None),
            team_id=_as_upper_team_id(team_id),
            salary_amount=_safe_float(salary_amount, None),
            attrs=attrs,
            contract=contract,
            meta={},
        )
        self._player_cache[pid] = snap
        return snap

    def get_pick_snapshot(self, pick_id: PickId) -> PickSnapshot:
        pid = str(pick_id)
        if pid in self._pick_cache:
            return self._pick_cache[pid]
        d = self.draft_picks_map.get(pid)
        if not d:
            raise KeyError(f"pick not found in snapshot: {pick_id}")
        prot = d.get("protection")
        snap = PickSnapshot(
            kind="pick",
            pick_id=pid,
            year=_safe_int(d.get("year"), 0),
            round=_safe_int(d.get("round"), 0),
            original_team=str(d.get("original_team") or "").upper(),
            owner_team=str(d.get("owner_team") or "").upper(),
            protection=(prot if isinstance(prot, (dict, type(None))) else None),
            meta={},
        )
        self._pick_cache[pid] = snap
        return snap

    def get_swap_snapshot(self, swap_id: SwapId) -> SwapSnapshot:
        sid = str(swap_id)
        if sid in self._swap_cache:
            return self._swap_cache[sid]
        d = self.swap_rights_map.get(sid)
        if not d:
            raise KeyError(f"swap not found in snapshot: {swap_id}")
        snap = SwapSnapshot(
            kind="swap",
            swap_id=sid,
            pick_id_a=str(d.get("pick_id_a") or ""),
            pick_id_b=str(d.get("pick_id_b") or ""),
            year=(_safe_int(d.get("year"), 0) if d.get("year") is not None else None),
            round=(_safe_int(d.get("round"), 0) if d.get("round") is not None else None),
            owner_team=str(d.get("owner_team") or "").upper(),
            active=bool(d.get("active", True)),
            created_by_deal_id=(str(d.get("created_by_deal_id")) if d.get("created_by_deal_id") else None),
            created_at=(str(d.get("created_at")) if d.get("created_at") else None),
            meta={},
        )
        self._swap_cache[sid] = snap
        return snap

    def get_fixed_asset_snapshot(self, asset_id: FixedAssetId) -> FixedAssetSnapshot:
        aid = str(asset_id)
        if aid in self._fixed_cache:
            return self._fixed_cache[aid]
        d = self.fixed_assets_map.get(aid)
        if not d:
            raise KeyError(f"fixed asset not found in snapshot: {asset_id}")
        attrs = d.get("attrs") if isinstance(d.get("attrs"), dict) else {}
        snap = FixedAssetSnapshot(
            kind="fixed",
            asset_id=aid,
            label=(str(d.get("label")) if d.get("label") is not None else None),
            value=_safe_float(d.get("value"), None),
            owner_team=str(d.get("owner_team") or "").upper(),
            source_pick_id=(str(d.get("source_pick_id")) if d.get("source_pick_id") else None),
            draft_year=(_safe_int(d.get("draft_year"), 0) if d.get("draft_year") is not None else None),
            attrs=attrs,
            meta={},
        )
        self._fixed_cache[aid] = snap
        return snap

    def get_pick_expectation(self, pick_id: PickId) -> Optional[PickExpectation]:
        return self.pick_expectations.get(str(pick_id))

    # ------------------------------------------------------------------
    # Optional utilities
    # ------------------------------------------------------------------
    def preload_players(self, player_ids: Iterable[PlayerId]) -> None:
        """Warm player cache for a list of IDs (optional optimization)."""
        ids = [str(pid) for pid in player_ids]
        missing = [pid for pid in ids if pid not in self._player_cache]
        if not missing:
            return
        if LeagueRepo is None:  # pragma: no cover
            return

        if self.repo is not None:
            repo_obj = self.repo
            for pid in missing:
                try:
                    p = repo_obj.get_player(pid)
                    try:
                        team_id = repo_obj.get_team_id_by_player(pid)
                    except Exception:
                        team_id = None
                    try:
                        salary_amount = repo_obj.get_salary_amount(pid)
                    except Exception:
                        salary_amount = None

                    attrs = p.get("attrs") if isinstance(p, dict) else {}
                    if not isinstance(attrs, dict):
                        attrs = {}

                    contract = None
                    cid = self.active_contract_id_by_player.get(pid)
                    if cid and cid in self.contracts_map:
                        try:
                            contract = contract_snapshot_from_dict(
                                self.contracts_map[cid],
                                current_season_year=self.current_season_year,
                            )
                        except Exception:
                            contract = None

                    self._player_cache[pid] = PlayerSnapshot(
                        kind="player",
                        player_id=pid,
                        name=(str(p.get("name")) if p.get("name") is not None else None),
                        pos=(str(p.get("pos")) if p.get("pos") is not None else None),
                        age=_safe_float(p.get("age"), None),
                        ovr=_safe_float(p.get("ovr"), None),
                        team_id=_as_upper_team_id(team_id),
                        salary_amount=_safe_float(salary_amount, None),
                        attrs=attrs,
                        contract=contract,
                        meta={},
                    )
                except Exception:
                    continue
        else:
            with LeagueRepo(self.db_path) as repo_obj:
                for pid in missing:
                    try:
                        p = repo_obj.get_player(pid)
                        try:
                            team_id = repo_obj.get_team_id_by_player(pid)
                        except Exception:
                            team_id = None
                        try:
                            salary_amount = repo_obj.get_salary_amount(pid)
                        except Exception:
                            salary_amount = None

                        attrs = p.get("attrs") if isinstance(p, dict) else {}
                        if not isinstance(attrs, dict):
                            attrs = {}

                        contract = None
                        cid = self.active_contract_id_by_player.get(pid)
                        if cid and cid in self.contracts_map:
                            try:
                                contract = contract_snapshot_from_dict(
                                    self.contracts_map[cid],
                                    current_season_year=self.current_season_year,
                                )
                            except Exception:
                                contract = None

                        self._player_cache[pid] = PlayerSnapshot(
                            kind="player",
                            player_id=pid,
                            name=(str(p.get("name")) if p.get("name") is not None else None),
                            pos=(str(p.get("pos")) if p.get("pos") is not None else None),
                            age=_safe_float(p.get("age"), None),
                            ovr=_safe_float(p.get("ovr"), None),
                            team_id=_as_upper_team_id(team_id),
                            salary_amount=_safe_float(salary_amount, None),
                            attrs=attrs,
                            contract=contract,
                            meta={},
                        )
                    except Exception:
                        continue



# -----------------------------------------------------------------------------
# Builder
# -----------------------------------------------------------------------------

def build_repo_valuation_data_context(
    *,
    db_path: str,
    current_season_year: int,
    current_date_iso: str,
    standings_order_worst_to_best: Optional[Sequence[TeamId]] = None,
    pick_expectations: Optional[PickExpectationMap] = None,
    expectation_confidence: float = 0.65,
    expectation_year_filter: Optional[int] = None,
    repo: Optional["LeagueRepo"] = None,
    assets_snapshot: Optional[Dict[str, Any]] = None,
    contract_ledger: Optional[Dict[str, Any]] = None,
) -> RepoValuationDataContext:
    """Build RepoValuationDataContext from sqlite snapshot.

    Parameters
    ----------
    standings_order_worst_to_best:
        If provided and pick_expectations is None, builds PickExpectationMap automatically.
    pick_expectations:
        If provided, used as-is.
    expectation_year_filter:
        If provided, only generates expectations for picks in that year.
    """
    if LeagueRepo is None:  # pragma: no cover
        raise ImportError("LeagueRepo import failed; cannot build valuation data context")

    # Snapshot inputs can be injected by a tick-level context to avoid repeated IO.
    # Also support sharing a single open repo (caller owns lifecycle; provider must not close it).
    if repo is not None:
        # Fail-fast if caller accidentally passes a repo connected to a different DB.
        repo_db_path = getattr(repo, "db_path", None)
        if repo_db_path is not None and str(repo_db_path) and str(repo_db_path) != str(db_path):
            raise ValueError(
                f"build_repo_valuation_data_context: db_path mismatch (db_path={db_path!r}, repo.db_path={repo_db_path!r})"
            )

    assets: Optional[Dict[str, Any]] = dict(assets_snapshot) if assets_snapshot is not None else None
    ledger: Optional[Dict[str, Any]] = dict(contract_ledger) if contract_ledger is not None else None

    if repo is not None:
        # Use shared repo without opening/closing.
        if assets is None:
            assets = repo.get_trade_assets_snapshot() or {}
        if ledger is None:
            ledger = repo.get_contract_ledger_snapshot() or {}
    else:
        # Avoid opening the DB twice: open once if either snapshot is missing.
        if assets is None or ledger is None:
            with LeagueRepo(db_path) as repo_obj:
                if assets is None:
                    assets = repo_obj.get_trade_assets_snapshot() or {}
                if ledger is None:
                    ledger = repo_obj.get_contract_ledger_snapshot() or {}

    if assets is None:  # pragma: no cover
        assets = {}
    if ledger is None:  # pragma: no cover
        ledger = {}

    draft_picks_map = dict(assets.get("draft_picks") or {})
    swap_rights_map = dict(assets.get("swap_rights") or {})
    fixed_assets_map = dict(assets.get("fixed_assets") or {})

    contracts_map = dict((ledger.get("contracts") or {}))
    active_contract_id_by_player = dict((ledger.get("active_contract_id_by_player") or {}))

    pe: PickExpectationMap = {}
    if pick_expectations is not None:
        pe = dict(pick_expectations)
    elif standings_order_worst_to_best is not None:
        pe = build_pick_expectations_from_standings(
            draft_picks_map,
            standings_order_worst_to_best=standings_order_worst_to_best,
            confidence=float(expectation_confidence),
            year_filter=expectation_year_filter,
        )

    # Normalize PickExpectations: ensure meta carries current_season_year so
    # market_pricing can apply pick year discount consistently.
    if pe:
        pe_norm: PickExpectationMap = {}
        for pick_id, exp in pe.items():
            # Do not mutate exp.meta in-place; it might be shared/reused across contexts.
            try:
                meta = dict(exp.meta) if isinstance(exp.meta, dict) else {}
            except Exception:
                meta = {}

            # market_pricing expects this key to exist (not just be truthy).
            if meta.get("current_season_year") is None or meta.get("current_season_year") == "":
                meta["current_season_year"] = int(current_season_year)

            pe_norm[pick_id] = replace(exp, meta=meta)
        pe = pe_norm

    return RepoValuationDataContext(
        db_path=str(db_path),
        current_season_year=int(current_season_year),
        current_date_iso=str(current_date_iso),
        repo=repo,
        draft_picks_map=draft_picks_map,
        swap_rights_map=swap_rights_map,
        fixed_assets_map=fixed_assets_map,
        contracts_map=contracts_map,
        active_contract_id_by_player=active_contract_id_by_player,
        pick_expectations=pe,
    )
