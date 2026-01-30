# team_situation.py
# -*- coding: utf-8 -*-
"""Team situation evaluation (team context -> quantitative signals + needs).

This module is designed to plug into the existing project structure:
- state.py provides workflow snapshots and league context
- league_repo.py is SSOT for roster/contracts/picks
- matchengine v2 results are accumulated into workflow_state["team_stats"][tid]

Outputs are intended to be consumed by later trade logic:
- Competitive tier (contender/rebuild...)
- Trade posture (buy/sell)
- Preference weights (win-now vs picks vs cap-flex)
- Needs list (tag, weight, reason, evidence)

All logic is defensive: missing data -> safe fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple, Literal

import logging
import math

from schema import normalize_team_id, normalize_player_id
import state
from league_repo import LeagueRepo
from derived_formulas import compute_derived
from team_utils import get_conference_standings

logger = logging.getLogger(__name__)
_WARN_COUNTS: Dict[str, int] = {}


def _warn_limited(code: str, msg: str, *, limit: int = 5) -> None:
    n = _WARN_COUNTS.get(code, 0)
    if n < limit:
        logger.warning("%s %s", code, msg, exc_info=True)
    _WARN_COUNTS[code] = n + 1


CompetitiveTier = Literal["CONTENDER", "PLAYOFF_BUYER", "FRINGE", "RESET", "REBUILD", "TANK"]
TradePosture = Literal["AGGRESSIVE_BUY", "SOFT_BUY", "STAND_PAT", "SOFT_SELL", "SELL"]
TimeHorizon = Literal["WIN_NOW", "RE_TOOL", "REBUILD"]


@dataclass(frozen=True, slots=True)
class TeamNeed:
    tag: str
    weight: float
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeamConstraints:
    payroll: float
    cap_space: float
    apron_status: Literal["BELOW_CAP", "OVER_CAP", "ABOVE_1ST_APRON", "ABOVE_2ND_APRON"]
    hard_flags: Dict[str, bool] = field(default_factory=dict)
    locks_count: int = 0
    deadline_pressure: float = 0.0


@dataclass(frozen=True, slots=True)
class TeamSituationSignals:
    win_pct: float
    conf_rank: Optional[int]
    gb: Optional[float]
    point_diff_pg: float
    last10_win_pct: float
    trend: float
    net_rating: float
    star_power: float
    depth: float
    core_age: float
    young_core: float
    asset_score: float
    flexibility: float
    style_3_rate: float
    style_rim_rate: float
    role_fit_health: float


@dataclass(frozen=True, slots=True)
class TeamSituation:
    team_id: str
    competitive_tier: CompetitiveTier
    trade_posture: TradePosture
    time_horizon: TimeHorizon
    urgency: float
    preferences: Dict[str, float]
    constraints: TeamConstraints
    needs: List[TeamNeed]
    signals: TeamSituationSignals
    reasons: List[str]


@dataclass(frozen=True, slots=True)
class TeamSituationContext:
    current_date: date
    league_ctx: Dict[str, Any]
    workflow_state: Dict[str, Any]
    trade_state: Dict[str, Any]
    assets_snapshot: Dict[str, Any]
    contract_ledger: Dict[str, Any]
    standings: Dict[str, List[Dict[str, Any]]]
    records_index: Dict[str, Dict[str, Any]]
    team_stats: Dict[str, Any]
    player_stats: Dict[str, Any]
    trade_market: Dict[str, Any]
    trade_memory: Dict[str, Any]
    negotiations: Dict[str, Any]
    asset_locks: Dict[str, Any]


# ------------------------------------------------------------
# Context builder
# ------------------------------------------------------------

def build_team_situation_context(
    *,
    db_path: Optional[str] = None,
    current_date: Optional[date] = None,
) -> TeamSituationContext:
    """Build a reusable snapshot for many team evaluations.

    It reads state + DB once, so evaluating 30 teams is cheap.
    """
    # Make sure core models exist (cap & schedule).
    try:
        state.ensure_cap_model_populated_if_needed()
    except Exception:
        _warn_limited("ENSURE_CAP_MODEL_FAILED", "state.ensure_cap_model_populated_if_needed() failed")

    try:
        state.initialize_master_schedule_if_needed()
    except Exception:
        _warn_limited("ENSURE_SCHEDULE_FAILED", "state.initialize_master_schedule_if_needed() failed")

    if current_date is None:
        try:
            current_date = state.get_current_date_as_date()
        except Exception:
            current_date = date.today()

    league_ctx = {}
    try:
        league_ctx = state.get_league_context_snapshot() or {}
    except Exception:
        _warn_limited("LEAGUE_CTX_SNAPSHOT_FAILED", "get_league_context_snapshot failed")
        league_ctx = {}

    workflow_state = {}
    try:
        workflow_state = state.export_workflow_state() or {}
    except Exception:
        _warn_limited("WORKFLOW_SNAPSHOT_FAILED", "export_workflow_state failed")
        workflow_state = {}

    trade_state = {}
    try:
        trade_state = state.export_trade_context_snapshot() or {}
    except Exception:
        _warn_limited("TRADE_CTX_SNAPSHOT_FAILED", "export_trade_context_snapshot failed")
        trade_state = {}

    resolved_db_path = db_path or _safe_get_db_path()

    assets_snapshot: Dict[str, Any] = {}
    contract_ledger: Dict[str, Any] = {}

    if resolved_db_path:
        try:
            with LeagueRepo(resolved_db_path) as repo:
                repo.init_db()
                assets_snapshot = repo.get_trade_assets_snapshot() or {}
                contract_ledger = repo.get_contract_ledger_snapshot() or {}
        except Exception:
            _warn_limited("DB_SNAPSHOT_FAILED", f"db_path={resolved_db_path!r}")
            assets_snapshot = {}
            contract_ledger = {}

    try:
        standings = get_conference_standings()
    except Exception:
        _warn_limited("STANDINGS_FAILED", "get_conference_standings failed")
        standings = {"east": [], "west": []}

    records_index = _build_records_index_from_master_schedule(
        (workflow_state.get("league", {}) or {}).get("master_schedule", {})
    )

    return TeamSituationContext(
        current_date=current_date,
        league_ctx=_to_plain(league_ctx),
        workflow_state=_to_plain(workflow_state),
        trade_state=_to_plain(trade_state),
        assets_snapshot=_to_plain(assets_snapshot),
        contract_ledger=_to_plain(contract_ledger),
        standings=_to_plain(standings),
        records_index=records_index,
        team_stats=_to_plain(workflow_state.get("team_stats", {}) or {}),
        player_stats=_to_plain(workflow_state.get("player_stats", {}) or {}),
        trade_market=_to_plain(workflow_state.get("trade_market", {}) or {}),
        trade_memory=_to_plain(workflow_state.get("trade_memory", {}) or {}),
        negotiations=_to_plain(workflow_state.get("negotiations", {}) or {}),
        asset_locks=_to_plain(trade_state.get("asset_locks", {}) or {}),
    )


def _safe_get_db_path() -> Optional[str]:
    try:
        return state.get_db_path()
    except Exception:
        return None


def _to_plain(v: Any) -> Any:
    # state._to_plain isn't public. We mirror minimal behavior.
    if isinstance(v, dict):
        return {k: _to_plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_plain(x) for x in v]
    return v


# ------------------------------------------------------------
# Evaluator
# ------------------------------------------------------------


class TeamSituationEvaluator:
    def __init__(self, *, ctx: TeamSituationContext, db_path: Optional[str] = None):
        self.ctx = ctx
        self.db_path = db_path or _safe_get_db_path()

    def evaluate_team(self, team_id: str) -> TeamSituation:
        tid = str(normalize_team_id(team_id, strict=True))

        perf = self._compute_performance(tid)
        roster = self._load_roster_with_derived(tid)
        roster_sig = self._compute_roster_signals(tid, roster)
        asset_sig = self._compute_asset_signals(tid)
        constraints = self._compute_constraints(tid, roster, perf)
        style_sig = self._compute_style_signals(tid)
        role_sig, role_needs = self._compute_role_fit_and_needs(tid, roster)

        signals = TeamSituationSignals(
            win_pct=float(perf["win_pct"]),
            conf_rank=perf.get("rank"),
            gb=perf.get("gb"),
            point_diff_pg=float(perf["point_diff_pg"]),
            last10_win_pct=float(perf["last10_win_pct"]),
            trend=float(perf["trend"]),
            net_rating=float(perf["net_rating"]),
            star_power=float(roster_sig["star_power"]),
            depth=float(roster_sig["depth"]),
            core_age=float(roster_sig["core_age"]),
            young_core=float(roster_sig["young_core"]),
            asset_score=float(asset_sig["asset_score"]),
            flexibility=float(asset_sig["flexibility"]),
            style_3_rate=float(style_sig["three_rate"]),
            style_rim_rate=float(style_sig["rim_rate"]),
            role_fit_health=float(role_sig["role_fit_health"]),
        )

        tier, posture, horizon, urgency, prefs, needs, reasons = self._classify_and_build_outputs(
            tid=tid,
            signals=signals,
            constraints=constraints,
            role_needs=role_needs,
            roster_sig=roster_sig,
            asset_sig=asset_sig,
            style_sig=style_sig,
        )

        return TeamSituation(
            team_id=tid,
            competitive_tier=tier,
            trade_posture=posture,
            time_horizon=horizon,
            urgency=urgency,
            preferences=prefs,
            constraints=constraints,
            needs=needs,
            signals=signals,
            reasons=reasons,
        )

    def evaluate_all(self, team_ids: Optional[List[str]] = None) -> Dict[str, TeamSituation]:
        ids = team_ids or _active_team_ids_from_ctx(self.ctx)
        out: Dict[str, TeamSituation] = {}
        for tid in ids:
            try:
                out[tid] = self.evaluate_team(tid)
            except Exception:
                _warn_limited("EVALUATE_TEAM_FAILED", f"team_id={tid!r}")
        return out

    # ------------------------
    # Internals
    # ------------------------

    def _compute_performance(self, team_id: str) -> Dict[str, Any]:
        rec = self.ctx.records_index.get(team_id, {}) or {}
        wins = int(rec.get("wins", 0) or 0)
        losses = int(rec.get("losses", 0) or 0)
        gp = wins + losses
        win_pct = (wins / gp) if gp else 0.0

        pf = float(rec.get("pf", 0) or 0)
        pa = float(rec.get("pa", 0) or 0)
        point_diff_pg = ((pf - pa) / gp) if gp else 0.0

        last10 = rec.get("last10", []) or []
        if isinstance(last10, list) and last10:
            last10_wins = sum(1 for x in last10 if x == 1)
            last10_win_pct = last10_wins / len(last10)
        else:
            last10_win_pct = win_pct

        trend = float(last10_win_pct) - float(win_pct)

        # rank/gb from standings
        rank = None
        gb = None
        for row in (self.ctx.standings.get("east", []) + self.ctx.standings.get("west", [])):
            if str(row.get("team_id", "")).upper() == team_id:
                rank = row.get("rank")
                gb = row.get("gb")
                break

        # net rating approx
        net_rating = None
        ts = (self.ctx.team_stats.get(team_id, {}) or {})
        totals = (ts.get("totals", {}) or {}) if isinstance(ts, dict) else {}
        poss = _safe_float(totals.get("Possessions"), 0.0)
        pts = _safe_float(totals.get("PTS"), pf)
        if poss > 1e-6 and gp > 0:
            ortg = pts / poss * 100.0
            drtg = pa / poss * 100.0
            net_rating = ortg - drtg
        else:
            # fallback: scale point diff
            net_rating = point_diff_pg * 2.1

        return {
            "wins": wins,
            "losses": losses,
            "gp": gp,
            "win_pct": float(win_pct),
            "pf": pf,
            "pa": pa,
            "point_diff_pg": float(point_diff_pg),
            "last10_win_pct": float(last10_win_pct),
            "trend": float(trend),
            "rank": int(rank) if rank is not None else None,
            "gb": float(gb) if gb is not None else None,
            "net_rating": float(net_rating),
            "season_progress": _clamp(gp / 82.0, 0.0, 1.0),
        }

    def _load_roster_with_derived(self, team_id: str) -> List[Dict[str, Any]]:
        if not self.db_path:
            return []
        out: List[Dict[str, Any]] = []
        try:
            with LeagueRepo(self.db_path) as repo:
                repo.init_db()
                rows = repo.get_team_roster(team_id) or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    pid_raw = row.get("player_id")
                    if not pid_raw:
                        continue
                    pid = str(normalize_player_id(pid_raw, strict=False, allow_legacy_numeric=True))
                    attrs = row.get("attrs") or {}
                    if not isinstance(attrs, dict):
                        attrs = {}
                    try:
                        derived = compute_derived(attrs)
                    except Exception:
                        _warn_limited("DERIVED_COMPUTE_FAILED", f"team_id={team_id} player_id={pid}")
                        derived = {}
                    out.append(
                        {
                            "player_id": pid,
                            "name": row.get("name") or attrs.get("Name") or "",
                            "pos": row.get("pos") or attrs.get("POS") or attrs.get("Position") or "",
                            "age": int(row.get("age") or attrs.get("Age") or 0),
                            "ovr": float(row.get("ovr") or attrs.get("OVR") or 0.0),
                            "salary": float(row.get("salary_amount") or 0.0),
                            "potential": _parse_potential(attrs.get("Potential")),
                            "attrs": attrs,
                            "derived": derived,
                        }
                    )
        except Exception:
            _warn_limited("LOAD_ROSTER_FAILED", f"team_id={team_id!r}")
            return []

        out.sort(key=lambda r: (-(r.get("ovr") or 0.0), str(r.get("player_id") or "")))
        return out

    def _compute_roster_signals(self, team_id: str, roster: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not roster:
            return {
                "star_power": 0.0,
                "depth": 0.0,
                "core_age": 0.0,
                "young_core": 0.0,
                "top3_avg": 0.0,
                "top8_avg": 0.0,
                "pos_counts": {},
                "salary_buckets": {},
                "rotation": [],
            }

        top3 = roster[:3]
        top8 = roster[:8]
        top3_avg = _avg([_safe_float(p.get("ovr"), 0.0) for p in top3])
        top8_avg = _avg([_safe_float(p.get("ovr"), 0.0) for p in top8])

        star_power = _clamp(top3_avg / 100.0, 0.0, 1.0)
        depth = _clamp(top8_avg / 100.0, 0.0, 1.0)

        core_age = _weighted_avg(
            values=[_safe_float(p.get("age"), 0.0) for p in top3],
            weights=[_safe_float(p.get("ovr"), 0.0) for p in top3],
            default=_avg([_safe_float(p.get("age"), 0.0) for p in top3]),
        )

        # young core: U24 with good ovr/potential
        young_score = 0.0
        for p in roster:
            age = _safe_float(p.get("age"), 30.0)
            if age > 24.5:
                continue
            ovr = _safe_float(p.get("ovr"), 0.0)
            pot = _safe_float(p.get("potential"), 0.6)
            # emphasize real contributors
            young_score += _clamp((ovr - 60.0) / 30.0, 0.0, 1.0) * (0.55 + 0.45 * pot)
        young_core = _clamp(young_score / 4.0, 0.0, 1.0)

        pos_counts = _count_positions(top8)
        salary_buckets = _salary_buckets(top8)

        return {
            "star_power": float(star_power),
            "depth": float(depth),
            "core_age": float(core_age),
            "young_core": float(young_core),
            "top3_avg": float(top3_avg),
            "top8_avg": float(top8_avg),
            "pos_counts": pos_counts,
            "salary_buckets": salary_buckets,
            "rotation": top8,
        }

    def _compute_asset_signals(self, team_id: str) -> Dict[str, Any]:
        assets = self.ctx.assets_snapshot or {}
        draft_picks = assets.get("draft_picks", {}) or {}
        swaps = assets.get("swap_rights", {}) or {}
        fixed_assets = assets.get("fixed_assets", {}) or {}

        # Determine draft window (default 7 years ahead)
        trade_rules = (self.ctx.league_ctx.get("trade_rules", {}) or {})
        max_years = int(trade_rules.get("max_pick_years_ahead") or 7)

        season_year = _safe_int(self.ctx.league_ctx.get("season_year"), None)
        base_year = (season_year + 1) if season_year else (self.ctx.current_date.year + 1)

        standings_map = _standings_winpct_map(self.ctx.standings)

        firsts = 0
        seconds = 0
        score = 0.0

        for pick in draft_picks.values():
            if not isinstance(pick, dict):
                continue
            if str(pick.get("owner_team", "")).upper() != team_id:
                continue
            yr = _safe_int(pick.get("year"), None)
            rnd = _safe_int(pick.get("round"), None)
            if yr is None or rnd is None:
                continue
            if yr < base_year or yr > base_year + max_years:
                continue

            # Base by round
            if rnd == 1:
                base = 1.00
                firsts += 1
            else:
                base = 0.35
                seconds += 1

            # Original team strength proxy -> pick quality
            orig = str(pick.get("original_team", "")).upper()
            orig_wp = standings_map.get(orig)
            if orig_wp is None:
                quality_mult = 1.0
            else:
                # worse team => more valuable
                quality_mult = _clamp(1.0 + (0.55 - orig_wp), 0.70, 1.35)

            # Protection discount
            prot = pick.get("protection")
            prot_mult = 1.0
            if prot is not None:
                prot_mult = 0.80
                if isinstance(prot, dict):
                    # heavier protection (top-10 etc) => more discount
                    top_n = prot.get("top") or prot.get("top_n") or prot.get("protected_top")
                    try:
                        top_n_i = int(top_n)
                        if top_n_i >= 10:
                            prot_mult = 0.70
                        elif top_n_i >= 5:
                            prot_mult = 0.78
                    except Exception:
                        pass

            score += base * quality_mult * prot_mult

        swap_count = 0
        swap_score = 0.0
        for s in swaps.values():
            if not isinstance(s, dict):
                continue
            if str(s.get("owner_team", "")).upper() != team_id:
                continue
            if not bool(s.get("active", True)):
                continue
            yr = _safe_int(s.get("year"), None)
            if yr is not None and (yr < base_year or yr > base_year + max_years):
                continue
            swap_count += 1
            swap_score += 0.25

        fixed_score = 0.0
        fixed_count = 0
        for a in fixed_assets.values():
            if not isinstance(a, dict):
                continue
            if str(a.get("owner_team", "")).upper() != team_id:
                continue
            fixed_count += 1
            v = _safe_float(a.get("value"), 0.0)
            # scale into pick-like units
            fixed_score += _clamp(v / 10.0, 0.0, 1.5) * 0.6

        score_total = score + swap_score + fixed_score
        # Normalize: typical range 0~8
        asset_score = _clamp(score_total / 6.5, 0.0, 1.0)

        # Flexibility: cap space + expiring + medium contracts
        flex = self._compute_flexibility(team_id)

        return {
            "firsts": firsts,
            "seconds": seconds,
            "swaps": swap_count,
            "fixed_assets": fixed_count,
            "asset_score_raw": float(score_total),
            "asset_score": float(asset_score),
            "flexibility": float(flex),
            "base_year": base_year,
            "max_years": max_years,
        }

    def _compute_flexibility(self, team_id: str) -> float:
        # cap space (normalized)
        trade_rules = (self.ctx.league_ctx.get("trade_rules", {}) or {})
        salary_cap = _safe_float(trade_rules.get("salary_cap"), 0.0)

        payroll = self._compute_payroll_from_contracts_or_roster(team_id)
        cap_space = salary_cap - payroll

        # expiring count and "matchable" mid salaries
        season_year = _safe_int(self.ctx.league_ctx.get("season_year"), None)
        expiring = 0
        matchable = 0
        if season_year is not None:
            for pid in self._team_player_ids_from_contracts(team_id):
                rem = self._remaining_years_for_player(pid, season_year)
                if rem == 1:
                    expiring += 1
        # from roster salaries if DB accessible
        for p in self._load_roster_with_derived(team_id)[:12]:
            sal = _safe_float(p.get("salary"), 0.0)
            if 5_000_000 <= sal <= 25_000_000:
                matchable += 1

        # Normalize components
        cap_component = 0.0
        if salary_cap > 1e-6:
            cap_component = _clamp((cap_space / salary_cap) * 1.2 + 0.3, 0.0, 1.0)

        exp_component = _clamp(expiring / 6.0, 0.0, 1.0)
        match_component = _clamp(matchable / 6.0, 0.0, 1.0)

        flex = 0.55 * cap_component + 0.25 * exp_component + 0.20 * match_component
        return _clamp(flex, 0.0, 1.0)

    def _compute_constraints(self, team_id: str, roster: List[Dict[str, Any]], perf: Dict[str, Any]) -> TeamConstraints:
        trade_rules = (self.ctx.league_ctx.get("trade_rules", {}) or {})
        salary_cap = _safe_float(trade_rules.get("salary_cap"), 0.0)
        first_apron = _safe_float(trade_rules.get("first_apron"), 0.0)
        second_apron = _safe_float(trade_rules.get("second_apron"), 0.0)

        payroll = self._compute_payroll_from_contracts_or_roster(team_id, roster)
        cap_space = salary_cap - payroll

        # apron classification
        apron_status: TeamConstraints.__annotations__["apron_status"] = "OVER_CAP"  # type: ignore
        if salary_cap > 0 and payroll < salary_cap:
            apron_status = "BELOW_CAP"
        elif first_apron > 0 and payroll >= first_apron:
            apron_status = "ABOVE_1ST_APRON"
        if second_apron > 0 and payroll >= second_apron:
            apron_status = "ABOVE_2ND_APRON"

        hard_flags: Dict[str, bool] = {}
        if apron_status == "ABOVE_2ND_APRON":
            hard_flags.update(
                {
                    "NO_AGGREGATION": True,
                    "NO_INCOMING_MORE_SALARY": True,
                    "NO_CASH": True,
                }
            )
        elif apron_status == "ABOVE_1ST_APRON":
            hard_flags.update({"LIMITED_MATCHING": True})

        # Recent signing / aggregation bans (if we can infer dates)
        new_fa_ban_days = _safe_int(trade_rules.get("new_fa_sign_ban_days"), 90)
        agg_ban_days = _safe_int(trade_rules.get("aggregation_ban_days"), 60)

        signed_ban = self._count_recently_signed_players(team_id, new_fa_ban_days)
        acquired_ban = self._count_recently_acquired_players(team_id, agg_ban_days)
        if signed_ban > 0:
            hard_flags["NEW_FA_TRADE_BAN"] = True
        if acquired_ban > 0:
            hard_flags["AGGREGATION_BAN"] = True

        # asset locks that touch this team
        locks_count = self._count_team_related_locks(team_id)

        # deadline pressure
        deadline_pressure = _deadline_pressure(self.ctx.current_date, trade_rules.get("trade_deadline"))

        return TeamConstraints(
            payroll=float(payroll),
            cap_space=float(cap_space),
            apron_status=apron_status,
            hard_flags=hard_flags,
            locks_count=int(locks_count),
            deadline_pressure=float(deadline_pressure),
        )

    def _compute_style_signals(self, team_id: str) -> Dict[str, Any]:
        ts = (self.ctx.team_stats.get(team_id, {}) or {})
        totals = (ts.get("totals", {}) or {}) if isinstance(ts, dict) else {}
        breakdowns = (ts.get("breakdowns", {}) or {}) if isinstance(ts, dict) else {}

        fga = _safe_float(totals.get("FGA"), 0.0)
        tpa = _safe_float(totals.get("3PA"), 0.0)
        tov = _safe_float(totals.get("TOV"), 0.0)
        poss = _safe_float(totals.get("Possessions"), 0.0)

        three_rate = (tpa / fga) if fga > 1e-6 else 0.0

        # rim attempts from ShotZoneDetail if present
        rim_fga = None
        szd = breakdowns.get("ShotZoneDetail") if isinstance(breakdowns, dict) else None
        if isinstance(szd, dict):
            ra = szd.get("Restricted_Area")
            if isinstance(ra, dict):
                rim_fga = _safe_float(ra.get("FGA"), None)

        if rim_fga is None:
            # fallback: from ShotZones maybe
            sz = breakdowns.get("ShotZones") if isinstance(breakdowns, dict) else None
            if isinstance(sz, dict):
                # heuristic keys might contain 'Rim' or 'Paint'
                rim_candidates = [
                    _safe_float(v, 0.0)
                    for k, v in sz.items()
                    if isinstance(k, str) and ("Rim" in k or "Paint" in k or "RA" in k)
                ]
                if rim_candidates:
                    rim_fga = float(sum(rim_candidates))

        rim_rate = (float(rim_fga) / fga) if (rim_fga is not None and fga > 1e-6) else 0.0

        tov_rate = (tov / poss) if poss > 1e-6 else 0.0

        off_actions = breakdowns.get("OffActionCounts") if isinstance(breakdowns, dict) else None
        pnr_rate = drive_rate = dho_rate = post_rate = trans_rate = set_rate = iso_rate = 0.0
        if isinstance(off_actions, dict):
            total_actions = float(sum(_safe_float(v, 0.0) for v in off_actions.values()))
            if total_actions > 1e-6:
                # Canonical action bases used by matchengine_v2 (builders.get_action_base / game_cfg scheme keys)
                pnr = _safe_float(off_actions.get("PnR"), 0.0)
                drive = _safe_float(off_actions.get("Drive"), 0.0)
                dho = _safe_float(off_actions.get("DHO"), 0.0)
                post = _safe_float(off_actions.get("PostUp"), 0.0)
                trans = _safe_float(off_actions.get("TransitionEarly"), 0.0)
                setplays = _safe_float(off_actions.get("HornsSet"), 0.0) + _safe_float(off_actions.get("ElbowHub"), 0.0)

                # Some configs may still log "ISO" etc; keep for backwards compatibility.
                iso = _safe_float(off_actions.get("ISO"), 0.0)

                pnr_rate = pnr / total_actions
                drive_rate = drive / total_actions
                dho_rate = dho / total_actions
                post_rate = post / total_actions
                trans_rate = trans / total_actions
                set_rate = setplays / total_actions
                iso_rate = iso / total_actions

        return {
            "three_rate": float(_clamp(three_rate, 0.0, 1.0)),
            "rim_rate": float(_clamp(rim_rate, 0.0, 1.0)),
            "tov_rate": float(_clamp(tov_rate, 0.0, 1.0)),
            "pnr_rate": float(_clamp(pnr_rate, 0.0, 1.0)),
            "drive_rate": float(_clamp(drive_rate, 0.0, 1.0)),
            "dho_rate": float(_clamp(dho_rate, 0.0, 1.0)),
            "post_rate": float(_clamp(post_rate, 0.0, 1.0)),
            "transition_rate": float(_clamp(trans_rate, 0.0, 1.0)),
            "setplay_rate": float(_clamp(set_rate, 0.0, 1.0)),
            "iso_rate": float(_clamp(iso_rate, 0.0, 1.0)),
            "has_breakdowns": bool(isinstance(breakdowns, dict) and len(breakdowns) > 0),
        }

    def _compute_role_fit_and_needs(self, team_id: str, roster: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[TeamNeed]]:
        # Evaluate 12 roles using role_fit tables.
        try:
            from role_fit_data import ROLE_FIT_WEIGHTS  # type: ignore
            from role_fit import role_fit_score, role_fit_grade  # type: ignore
        except Exception:
            # If role fit data isn't available, return neutral.
            return ({"role_fit_health": 0.5, "role_best": {}}, [])

        roles = [r for r in ROLE_FIT_WEIGHTS.keys()]
        if not roles or not roster:
            return ({"role_fit_health": 0.5, "role_best": {}}, [])

        rotation = roster[:8]

        class _P:
            def __init__(self, derived: Dict[str, Any]):
                self._d = derived or {}
            def get(self, key: str) -> Any:
                # role_fit expects 0..100; missing -> 50 baseline
                try:
                    v = self._d.get(key, 50.0)
                    return float(v) if v is not None else 50.0
                except Exception:
                    return 50.0

        role_best: Dict[str, Dict[str, Any]] = {}
        grades: List[str] = []
        needs: List[TeamNeed] = []

        for role in roles:
            best_fit = -1.0
            best_pid = None
            for p in rotation:
                d = p.get("derived") if isinstance(p, dict) else None
                if not isinstance(d, dict):
                    continue
                fit = float(role_fit_score(_P(d), role))
                if fit > best_fit:
                    best_fit = fit
                    best_pid = p.get("player_id")
            g = role_fit_grade(role, best_fit)
            grades.append(g)
            role_best[role] = {"fit": float(best_fit), "grade": g, "best_pid": best_pid}

            # needs for weak roles
            if g in ("C", "D"):
                tag, label = _role_to_need_tag(role)
                weight = _clamp((62.0 - best_fit) / 25.0, 0.15, 1.0) if best_fit < 62.0 else 0.15
                needs.append(
                    TeamNeed(
                        tag=tag,
                        weight=float(weight),
                        reason=f"{label} 역할 커버리지가 약함(베스트 핏 {best_fit:.0f}, 등급 {g}).",
                        evidence={"role": role, "best_fit": best_fit, "grade": g},
                    )
                )

        # health: S/A/B roles proportion with weighting
        grade_points = {"S": 1.0, "A": 0.85, "B": 0.70, "C": 0.50, "D": 0.30}
        health = _avg([grade_points.get(g, 0.6) for g in grades])

        return ({"role_fit_health": float(_clamp(health, 0.0, 1.0)), "role_best": role_best}, needs)

    def _classify_and_build_outputs(
        self,
        *,
        tid: str,
        signals: TeamSituationSignals,
        constraints: TeamConstraints,
        role_needs: List[TeamNeed],
        roster_sig: Dict[str, Any],
        asset_sig: Dict[str, Any],
        style_sig: Dict[str, Any],
    ) -> Tuple[CompetitiveTier, TradePosture, TimeHorizon, float, Dict[str, float], List[TeamNeed], List[str]]:
        # 1) competitive score
        season_progress = _safe_float(self.ctx.records_index.get(tid, {}).get("season_progress"), 0.0)

        perf_score = _compute_perf_score(signals, season_progress)
        roster_score = 0.62 * signals.star_power + 0.38 * signals.depth
        composite = _lerp(roster_score, perf_score, _clamp(season_progress, 0.15, 0.85))

        # 2) tier
        tier: CompetitiveTier
        rank = signals.conf_rank
        wp = signals.win_pct
        nr = signals.net_rating
        tr = signals.trend

        # "reset" special: strong roster but bad record
        if composite >= 0.62 and wp < 0.46 and signals.star_power >= 0.70:
            tier = "RESET"
        elif (rank is not None and rank <= 4 and wp >= 0.58) or (wp >= 0.64 and nr >= 1.0):
            tier = "CONTENDER"
        elif (rank is not None and rank <= 8 and wp >= 0.50) or (wp >= 0.54 and nr >= 0.0):
            tier = "PLAYOFF_BUYER"
        elif (rank is not None and rank <= 12 and wp >= 0.42) or (wp >= 0.45 and season_progress > 0.2):
            tier = "FRINGE"
        else:
            # bottom teams: distinguish rebuild vs tank
            if wp <= 0.34 and (signals.young_core < 0.35) and (signals.star_power < 0.55):
                tier = "TANK"
            else:
                tier = "REBUILD"

        # 3) horizon
        core_age = signals.core_age
        if tier in ("CONTENDER", "PLAYOFF_BUYER"):
            horizon = "WIN_NOW"
        elif tier in ("REBUILD", "TANK"):
            horizon = "REBUILD"
        else:
            # fringe/reset
            if core_age >= 28.5 and signals.star_power >= 0.65:
                horizon = "RE_TOOL"
            elif signals.young_core >= 0.55:
                horizon = "REBUILD"
            else:
                horizon = "RE_TOOL"

        # 4) needs: merge role + style + roster gaps
        needs: List[TeamNeed] = []
        needs.extend(_dedupe_needs(role_needs))
        needs.extend(_style_to_needs(tid, signals, style_sig))
        needs.extend(_roster_gap_needs(tid, roster_sig, signals))
        needs = _merge_and_clip_needs(needs)

        need_intensity = _avg([n.weight for n in needs]) if needs else 0.0

        # 5) trade posture
        patience = _safe_float(((self.ctx.trade_state.get("teams", {}) or {}).get(tid, {}) or {}).get("patience"), 0.5)
        patience = _clamp(patience, 0.0, 1.0)
        deadline_pressure = constraints.deadline_pressure

        # baseline buy/sell
        if tier == "CONTENDER":
            posture = "AGGRESSIVE_BUY" if (asset_sig.get("asset_score", 0.0) >= 0.55 and constraints.apron_status != "ABOVE_2ND_APRON") else "SOFT_BUY"
        elif tier == "PLAYOFF_BUYER":
            posture = "SOFT_BUY" if asset_sig.get("asset_score", 0.0) >= 0.40 else "STAND_PAT"
        elif tier == "FRINGE":
            posture = "STAND_PAT" if tr >= -0.06 else "SOFT_SELL"
        elif tier == "RESET":
            posture = "SOFT_BUY" if tr >= 0.04 else "SOFT_SELL"
        elif tier in ("REBUILD", "TANK"):
            posture = "SELL"
        else:
            posture = "STAND_PAT"

        # tighten around deadline
        if deadline_pressure >= 0.65 and tier in ("CONTENDER", "PLAYOFF_BUYER"):
            posture = "AGGRESSIVE_BUY" if posture in ("SOFT_BUY", "STAND_PAT") else posture
        if deadline_pressure >= 0.65 and tier in ("FRINGE", "RESET"):
            posture = "SOFT_SELL" if posture == "STAND_PAT" else posture
        if deadline_pressure >= 0.65 and tier in ("REBUILD", "TANK"):
            posture = "SELL"

        # constraints soften
        if constraints.apron_status == "ABOVE_2ND_APRON" and posture in ("AGGRESSIVE_BUY", "SOFT_BUY"):
            posture = "STAND_PAT"

        # patience modifies extremeness
        if patience >= 0.70 and posture == "AGGRESSIVE_BUY":
            posture = "SOFT_BUY"
        if patience >= 0.70 and posture == "SELL":
            posture = "SOFT_SELL"
        if patience <= 0.30 and posture == "SOFT_BUY":
            posture = "AGGRESSIVE_BUY"
        if patience <= 0.30 and posture == "SOFT_SELL":
            posture = "SELL"

        # 6) preferences (win-now vs picks vs cap-flex)
        prefs = _compute_preferences(tier, horizon, signals, constraints, asset_sig)

        # 7) urgency (0~1)
        urgency = _compute_urgency(
            tier=tier,
            horizon=horizon,
            deadline_pressure=deadline_pressure,
            patience=patience,
            trend=signals.trend,
            need_intensity=need_intensity,
            apron_status=constraints.apron_status,
        )

        # 8) reasons (Korean, for player-facing realism)
        reasons = _build_reasons(tid, tier, horizon, posture, signals, constraints, roster_sig, asset_sig, style_sig, prefs, needs)

        return tier, posture, horizon, float(urgency), prefs, needs, reasons

    # ------------------------
    # Contracts helpers
    # ------------------------

    def _team_player_ids_from_contracts(self, team_id: str) -> List[str]:
        ledger = self.ctx.contract_ledger or {}
        contracts = ledger.get("contracts", {}) or {}
        out: List[str] = []
        for c in contracts.values():
            if not isinstance(c, dict):
                continue
            if str(c.get("team_id", "")).upper() != team_id:
                continue
            pid = c.get("player_id")
            if pid:
                out.append(str(pid))
        return out

    def _remaining_years_for_player(self, player_id: str, season_year: int) -> Optional[int]:
        ledger = self.ctx.contract_ledger or {}
        active_by_player = ledger.get("active_contract_id_by_player", {}) or {}
        contracts = ledger.get("contracts", {}) or {}

        cid = active_by_player.get(str(player_id))
        if not cid:
            return None
        c = contracts.get(str(cid))
        if not isinstance(c, dict):
            return None

        start = _safe_int(c.get("start_season_year") or c.get("start_year"), None)
        years = _safe_int(c.get("years"), None)
        salary_by_year = c.get("salary_by_year") or {}

        end_year = None
        if start is not None and years is not None:
            end_year = start + years - 1
        else:
            try:
                keys = [int(k) for k in salary_by_year.keys()]
                end_year = max(keys) if keys else None
            except Exception:
                end_year = None

        if end_year is None:
            return None

        if season_year > end_year:
            return 0
        return int(end_year - season_year + 1)

    def _compute_payroll_from_contracts_or_roster(self, team_id: str, roster: Optional[List[Dict[str, Any]]] = None) -> float:
        # Prefer contract ledger if available (SSOT)
        ledger = self.ctx.contract_ledger or {}
        contracts = ledger.get("contracts", {}) or {}
        if isinstance(contracts, dict) and contracts:
            season_year = _safe_int(self.ctx.league_ctx.get("season_year"), None)
            total = 0.0
            for c in contracts.values():
                if not isinstance(c, dict):
                    continue
                if str(c.get("team_id", "")).upper() != team_id:
                    continue
                # pick current season salary
                sal = None
                sby = c.get("salary_by_year") or {}
                if season_year is not None and isinstance(sby, dict):
                    if str(season_year) in sby:
                        sal = sby.get(str(season_year))
                    elif season_year in sby:
                        sal = sby.get(season_year)
                if sal is None:
                    # fallback: try any numeric, take max
                    try:
                        sal = max(float(v) for v in sby.values() if v is not None)
                    except Exception:
                        sal = 0.0
                total += _safe_float(sal, 0.0)
            return float(total)

        # Fallback: roster salaries
        if roster is None:
            roster = []
            try:
                roster = self._load_roster_with_derived(team_id)
            except Exception:
                roster = []
        return float(sum(_safe_float(p.get("salary"), 0.0) for p in roster if isinstance(p, dict)))

    def _count_recently_signed_players(self, team_id: str, ban_days: int) -> int:
        # Uses state.players cache if present
        try:
            players = state.players_get() or {}
        except Exception:
            return 0
        if not isinstance(players, dict) or not players:
            return 0
        today = self.ctx.current_date
        n = 0
        for p in players.values():
            if not isinstance(p, dict):
                continue
            if str(p.get("team_id", "")).upper() != team_id:
                continue
            signed = p.get("signed_date")
            if not signed or str(signed).startswith("1900"):
                continue
            try:
                d = date.fromisoformat(str(signed))
                if (today - d).days < int(ban_days):
                    n += 1
            except Exception:
                continue
        return n

    def _count_recently_acquired_players(self, team_id: str, ban_days: int) -> int:
        try:
            players = state.players_get() or {}
        except Exception:
            return 0
        if not isinstance(players, dict) or not players:
            return 0
        today = self.ctx.current_date
        n = 0
        for p in players.values():
            if not isinstance(p, dict):
                continue
            if str(p.get("team_id", "")).upper() != team_id:
                continue
            acquired = p.get("acquired_date")
            if not acquired or str(acquired).startswith("1900"):
                continue
            try:
                d = date.fromisoformat(str(acquired))
                if (today - d).days < int(ban_days):
                    n += 1
            except Exception:
                continue
        return n

    def _count_team_related_locks(self, team_id: str) -> int:
        locks = self.ctx.asset_locks or {}
        if not isinstance(locks, dict) or not locks:
            return 0

        # Build quick membership sets
        roster_pids = {p.get("player_id") for p in self._load_roster_with_derived(team_id)}
        roster_pids = {str(x) for x in roster_pids if x}

        pick_ids_owned = set()
        for pick in (self.ctx.assets_snapshot.get("draft_picks", {}) or {}).values():
            if not isinstance(pick, dict):
                continue
            if str(pick.get("owner_team", "")).upper() != team_id:
                continue
            if pick.get("pick_id"):
                pick_ids_owned.add(str(pick.get("pick_id")))

        n = 0
        for key in locks.keys():
            if not isinstance(key, str):
                continue
            if key.startswith("player:"):
                pid = key.split(":", 1)[1]
                if pid in roster_pids:
                    n += 1
            elif key.startswith("pick:"):
                pid = key.split(":", 1)[1]
                if pid in pick_ids_owned:
                    n += 1
        return n


# ------------------------------------------------------------
# Public helper for legacy code
# ------------------------------------------------------------

def get_team_status_map_v2(ctx: Optional[TeamSituationContext] = None) -> Dict[str, str]:
    """Compatibility helper for existing AI code.

    Returns a minimal status map:
      - "contender": contenders + playoff buyers
      - "rebuild": rebuild + tank
      - "neutral": others
    """
    if ctx is None:
        ctx = build_team_situation_context()
    ev = TeamSituationEvaluator(ctx=ctx)
    out: Dict[str, str] = {}
    for tid, s in ev.evaluate_all().items():
        if s.competitive_tier in ("CONTENDER", "PLAYOFF_BUYER"):
            out[tid] = "contender"
        elif s.competitive_tier in ("REBUILD", "TANK"):
            out[tid] = "rebuild"
        else:
            out[tid] = "neutral"
    return out


# ------------------------------------------------------------
# Records index
# ------------------------------------------------------------

def _build_records_index_from_master_schedule(master_schedule: Any) -> Dict[str, Dict[str, Any]]:
    games = (master_schedule.get("games") if isinstance(master_schedule, dict) else None) or []
    if not isinstance(games, list):
        return {}

    # Collect per team list of (date, is_win, pf, pa)
    per_team: Dict[str, List[Tuple[str, int, int, int]]] = {}

    for g in games:
        if not isinstance(g, dict):
            continue
        if g.get("status") != "final":
            continue
        hid = str(g.get("home_team_id") or "").upper()
        aid = str(g.get("away_team_id") or "").upper()
        hs = g.get("home_score")
        as_ = g.get("away_score")
        if not hid or not aid or hs is None or as_ is None:
            continue
        try:
            hs_i = int(hs)
            as_i = int(as_)
        except Exception:
            continue
        d = str(g.get("date") or g.get("game_date") or "")
        if not d:
            # fallback stable order using game_id
            d = str(g.get("game_id") or "")

        home_win = 1 if hs_i > as_i else 0
        away_win = 1 if as_i > hs_i else 0

        per_team.setdefault(hid, []).append((d, home_win, hs_i, as_i))
        per_team.setdefault(aid, []).append((d, away_win, as_i, hs_i))

    out: Dict[str, Dict[str, Any]] = {}

    for tid, rows in per_team.items():
        # sort by date string (ISO works; game_id fallback still stable)
        rows_sorted = sorted(rows, key=lambda x: x[0])
        wins = sum(r[1] for r in rows_sorted)
        losses = len(rows_sorted) - wins
        pf = sum(r[2] for r in rows_sorted)
        pa = sum(r[3] for r in rows_sorted)
        last10 = [r[1] for r in rows_sorted[-10:]]
        last5 = [r[1] for r in rows_sorted[-5:]]

        out[tid] = {
            "wins": wins,
            "losses": losses,
            "pf": pf,
            "pa": pa,
            "last10": last10,
            "last5": last5,
            "season_progress": _clamp((wins + losses) / 82.0, 0.0, 1.0),
        }

    return out


def _active_team_ids_from_ctx(ctx: TeamSituationContext) -> List[str]:
    teams = ctx.trade_state.get("teams", {}) or {}
    ids = [str(k).upper() for k in teams.keys() if str(k).upper() != "FA"]
    if not ids:
        ids = [r.get("team_id") for r in (ctx.standings.get("east", []) + ctx.standings.get("west", []))]
        ids = [str(x).upper() for x in ids if x]
    return sorted(set(ids))


# ------------------------------------------------------------
# Scoring & mapping helpers
# ------------------------------------------------------------

def _parse_potential(pot_raw: Any) -> float:
    pot_map = {
        "A+": 1.0, "A": 0.95, "A-": 0.9,
        "B+": 0.85, "B": 0.8, "B-": 0.75,
        "C+": 0.7, "C": 0.65, "C-": 0.6,
        "D+": 0.55, "D": 0.5, "F": 0.4,
    }
    if isinstance(pot_raw, str):
        return float(pot_map.get(pot_raw.strip(), 0.6))
    try:
        return float(pot_raw)
    except Exception:
        return 0.6


def _safe_float(v: Any, default: Any = 0.0) -> float:
    if v is None:
        return float(default) if default is not None else 0.0
    if isinstance(v, bool):
        return float(default) if default is not None else 0.0
    try:
        return float(v)
    except Exception:
        return float(default) if default is not None else 0.0


def _safe_int(v: Any, default: Optional[int] = 0) -> Optional[int]:
    if v is None:
        return default
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except Exception:
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        xf = float(x)
    except Exception:
        return lo
    return lo if xf < lo else hi if xf > hi else xf


def _avg(xs: List[float]) -> float:
    xs2 = [float(x) for x in xs if x is not None]
    if not xs2:
        return 0.0
    return float(sum(xs2) / len(xs2))


def _weighted_avg(values: List[float], weights: List[float], default: float = 0.0) -> float:
    if not values or not weights or len(values) != len(weights):
        return float(default)
    s = 0.0
    w = 0.0
    for v, a in zip(values, weights):
        try:
            s += float(v) * float(a)
            w += float(a)
        except Exception:
            continue
    if w <= 1e-9:
        return float(default)
    return float(s / w)


def _lerp(a: float, b: float, t: float) -> float:
    tt = _clamp(t, 0.0, 1.0)
    return float(a * (1.0 - tt) + b * tt)


def _standings_winpct_map(standings: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in (standings.get("east", []) + standings.get("west", [])):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("team_id", "")).upper()
        if not tid:
            continue
        out[tid] = _safe_float(row.get("win_pct"), None)
    return out


def _compute_perf_score(sig: TeamSituationSignals, season_progress: float) -> float:
    # win% is primary; point diff + net rating as stabilizers
    wp = _clamp(sig.win_pct, 0.0, 1.0)
    pd = sig.point_diff_pg
    pd_norm = 0.5 + 0.5 * math.tanh(pd / 8.0)
    nr_norm = 0.5 + 0.5 * math.tanh(sig.net_rating / 8.0)

    # when season is young, trust roster more; still include trend
    trend = _clamp(sig.trend, -0.3, 0.3)
    trend_norm = 0.5 + (trend / 0.6)

    score = 0.58 * wp + 0.20 * pd_norm + 0.12 * nr_norm + 0.10 * trend_norm
    return float(_clamp(score, 0.0, 1.0))


def _deadline_pressure(today: date, trade_deadline: Any) -> float:
    if not trade_deadline:
        return 0.0
    try:
        d = date.fromisoformat(str(trade_deadline))
    except Exception:
        return 0.0
    days = (d - today).days
    if days <= 0:
        return 0.0
    # 45 days window ramp
    pressure = _clamp((45.0 - float(days)) / 45.0, 0.0, 1.0)
    if days <= 10:
        pressure = _clamp(pressure + 0.20, 0.0, 1.0)
    return float(pressure)


def _role_to_need_tag(role: str) -> Tuple[str, str]:
    """Map internal role-fit role names to stable need tags and Korean labels.

    role_fit_data.ROLE_FIT_WEIGHTS defines the canonical 12 roles:
      - Connector_Playmaker, Initiator_Primary, Initiator_Secondary, Pop_Spacer_Big,
        Post_Hub, Rim_Attacker, Roller_Finisher, ShortRoll_Playmaker, Shot_Creator,
        Spacer_CatchShoot, Spacer_Movement, Transition_Handler

    We keep a few legacy aliases too (for safety).
    """
    mapping = {
        # Canonical 12 roles (match role_fit_data.py)
        "Initiator_Primary": ("PRIMARY_INITIATOR", "1옵션 볼핸들러"),
        "Initiator_Secondary": ("SECONDARY_CREATOR", "세컨더리 크리에이터"),
        "Transition_Handler": ("TRANSITION_ENGINE", "트랜지션 핸들러"),
        "Shot_Creator": ("SHOT_CREATION", "샷 크리에이터"),
        "Rim_Attacker": ("RIM_PRESSURE", "림 어택/드라이브 자원"),
        "Spacer_CatchShoot": ("SPACING", "캐치&슛 스페이서"),
        "Spacer_Movement": ("MOVEMENT_SHOOTING", "무브먼트 슈터"),
        "Connector_Playmaker": ("CONNECTOR_PLAY", "커넥터 플레이메이커"),
        "Roller_Finisher": ("ROLL_THREAT", "롤/림런 피니셔"),
        "ShortRoll_Playmaker": ("SHORT_ROLL_PLAY", "숏롤 플레이메이커"),
        "Pop_Spacer_Big": ("POP_BIG", "팝 스페이서 빅"),
        "Post_Hub": ("POST_HUB", "포스트 허브"),

        # Legacy/alias safety
        "Spotup_Shooter": ("SPACING", "스팟업 슈터"),
        "Movement_Shooter": ("MOVEMENT_SHOOTING", "오프볼 슈터"),
        "Cutter_Slasher": ("RIM_PRESSURE", "림 어택/컷터"),
        "Roller": ("ROLL_THREAT", "롤 위협"),
        "ShortRoll": ("SHORT_ROLL_PLAY", "숏롤"),
        "Post_Anchor": ("POST_HUB", "포스트 옵션"),
    }
    if role in mapping:
        return mapping[role]
    # fallback heuristic
    if "Def" in role or "def" in role:
        return ("DEFENSE", "수비 자원")
    if "Shooter" in role:
        return ("SPACING", "슈팅")
    if "Rim" in role or "Roll" in role:
        return ("RIM_PRESSURE", "림 근처 위협")
    return ("ROLE_GAP", "역할")


def _dedupe_needs(needs: List[TeamNeed]) -> List[TeamNeed]:
    # keep max weight per tag
    best: Dict[str, TeamNeed] = {}
    for n in needs:
        if not n.tag:
            continue
        prev = best.get(n.tag)
        if prev is None or n.weight > prev.weight:
            best[n.tag] = n
    return list(best.values())


def _style_to_needs(team_id: str, sig: TeamSituationSignals, style_sig: Dict[str, Any]) -> List[TeamNeed]:
    needs: List[TeamNeed] = []

    three_rate = _safe_float(style_sig.get("three_rate"), 0.0)
    rim_rate = _safe_float(style_sig.get("rim_rate"), 0.0)
    tov_rate = _safe_float(style_sig.get("tov_rate"), 0.0)
    pnr_rate = _safe_float(style_sig.get("pnr_rate"), 0.0)

    # Baselines (NBA-ish feel)
    if three_rate < 0.32:
        w = _clamp((0.34 - three_rate) / 0.12, 0.25, 1.0)
        needs.append(
            TeamNeed(
                tag="SPACING",
                weight=float(w),
                reason=f"3점 시도 비중이 낮음({three_rate:.0%}) → 스페이싱/슈터 보강 필요.",
                evidence={"three_rate": three_rate},
            )
        )

    if rim_rate < 0.26:
        w = _clamp((0.28 - rim_rate) / 0.12, 0.25, 1.0)
        label = "림 어택" if pnr_rate >= 0.22 else "컷/전환 공격"
        needs.append(
            TeamNeed(
                tag="RIM_PRESSURE",
                weight=float(w),
                reason=f"림 공격 비중이 낮음({rim_rate:.0%}) → {label} 자원 필요.",
                evidence={"rim_rate": rim_rate, "pnr_rate": pnr_rate},
            )
        )

    if tov_rate > 0.155:
        w = _clamp((tov_rate - 0.155) / 0.08, 0.25, 1.0)
        needs.append(
            TeamNeed(
                tag="BALL_SECURITY",
                weight=float(w),
                reason=f"턴오버 비중이 높음({tov_rate:.1%}) → 안정적인 볼핸들/패스 필요.",
                evidence={"tov_rate": tov_rate},
            )
        )

    # if PnR heavy but no rim pressure / spacing -> prioritize initiator or roller
    if pnr_rate >= 0.28 and (three_rate < 0.34 or rim_rate < 0.28):
        w = _clamp((pnr_rate - 0.28) / 0.25, 0.20, 0.80)
        needs.append(
            TeamNeed(
                tag="PNR_ENGINE",
                weight=float(w),
                reason=f"PnR 의존도가 높음({pnr_rate:.0%}) → 핸들러/롤러의 질을 끌어올릴 필요.",
                evidence={"pnr_rate": pnr_rate, "three_rate": three_rate, "rim_rate": rim_rate},
            )
        )

    return needs


def _roster_gap_needs(team_id: str, roster_sig: Dict[str, Any], sig: TeamSituationSignals) -> List[TeamNeed]:
    needs: List[TeamNeed] = []
    pos_counts = roster_sig.get("pos_counts", {}) or {}

    g = int(pos_counts.get("G", 0) or 0)
    w = int(pos_counts.get("W", 0) or 0)
    b = int(pos_counts.get("B", 0) or 0)

    if g <= 1:
        needs.append(
            TeamNeed(
                tag="GUARD_DEPTH",
                weight=0.55,
                reason="가드 로테이션이 얇음 → 볼 운반/수비 가드 보강 필요.",
                evidence={"pos_counts": pos_counts},
            )
        )
    if w <= 1:
        needs.append(
            TeamNeed(
                tag="WING_DEPTH",
                weight=0.55,
                reason="윙 로테이션이 얇음 → 3&D/수비 윙 보강 필요.",
                evidence={"pos_counts": pos_counts},
            )
        )
    if b <= 1:
        needs.append(
            TeamNeed(
                tag="BIG_DEPTH",
                weight=0.55,
                reason="빅맨 로테이션이 얇음 → 리바운드/림프로텍션 자원 보강 필요.",
                evidence={"pos_counts": pos_counts},
            )
        )

    # "star + no depth" classic
    top3_avg = _safe_float(roster_sig.get("top3_avg"), 0.0)
    top8_avg = _safe_float(roster_sig.get("top8_avg"), 0.0)
    if top3_avg >= 84.0 and (top3_avg - top8_avg) >= 8.0:
        ww = _clamp((top3_avg - top8_avg) / 16.0, 0.35, 1.0)
        needs.append(
            TeamNeed(
                tag="BENCH_DEPTH",
                weight=float(ww),
                reason="상위 전력 대비 벤치/뎁스 격차가 큼 → 즉전 롤플레이어 확보 필요.",
                evidence={"top3_avg": top3_avg, "top8_avg": top8_avg},
            )
        )

    # aging core can trigger cap-flex need
    if sig.core_age >= 30.5 and sig.win_pct < 0.52:
        needs.append(
            TeamNeed(
                tag="CAP_FLEX",
                weight=0.45,
                reason="코어가 노장화되는 반면 성적이 애매함 → 계약 정리/유연성 확보가 중요.",
                evidence={"core_age": sig.core_age, "win_pct": sig.win_pct},
            )
        )

    return needs


def _merge_and_clip_needs(needs: List[TeamNeed]) -> List[TeamNeed]:
    # merge same tag by max weight and combine evidence
    merged: Dict[str, TeamNeed] = {}
    for n in needs:
        tag = str(n.tag or "")
        if not tag:
            continue
        w = float(_clamp(n.weight, 0.0, 1.0))
        if tag not in merged:
            merged[tag] = TeamNeed(tag=tag, weight=w, reason=n.reason, evidence=dict(n.evidence or {}))
        else:
            prev = merged[tag]
            if w > prev.weight:
                merged[tag] = TeamNeed(tag=tag, weight=w, reason=n.reason, evidence={**(prev.evidence or {}), **(n.evidence or {})})
            else:
                # keep prev reason; still merge evidence
                merged[tag] = TeamNeed(tag=tag, weight=prev.weight, reason=prev.reason, evidence={**(prev.evidence or {}), **(n.evidence or {})})

    # sort by weight
    out = sorted(merged.values(), key=lambda x: (-x.weight, x.tag))
    return out[:10]


def _compute_preferences(
    tier: CompetitiveTier,
    horizon: TimeHorizon,
    sig: TeamSituationSignals,
    constraints: TeamConstraints,
    asset_sig: Dict[str, Any],
) -> Dict[str, float]:
    # Base win-now by tier
    base_win = {
        "CONTENDER": 0.90,
        "PLAYOFF_BUYER": 0.78,
        "FRINGE": 0.55,
        "RESET": 0.50,
        "REBUILD": 0.25,
        "TANK": 0.15,
    }.get(tier, 0.50)

    # Adjust by horizon and deadline
    base_win += 0.10 if horizon == "WIN_NOW" else -0.05 if horizon == "REBUILD" else 0.0
    base_win += 0.08 * constraints.deadline_pressure
    base_win += 0.05 * _clamp(sig.trend, -0.2, 0.2)
    win_now = _clamp(base_win, 0.0, 1.0)

    # Picks preference
    base_picks = {
        "CONTENDER": 0.20,
        "PLAYOFF_BUYER": 0.30,
        "FRINGE": 0.45,
        "RESET": 0.55,
        "REBUILD": 0.80,
        "TANK": 0.90,
    }.get(tier, 0.50)

    # If already has lots of assets, slightly reduce pick craving (they may trade them)
    asset_score = _safe_float(asset_sig.get("asset_score"), 0.0)
    base_picks -= 0.10 * asset_score if tier in ("CONTENDER", "PLAYOFF_BUYER") else 0.0

    # Young core increases pick/dev focus
    base_picks += 0.10 * sig.young_core if horizon == "REBUILD" else 0.0

    picks = _clamp(base_picks, 0.0, 1.0)

    # Cap flexibility
    cap_flex = 0.35
    if constraints.apron_status == "ABOVE_2ND_APRON":
        cap_flex += 0.35
    elif constraints.apron_status == "ABOVE_1ST_APRON":
        cap_flex += 0.20
    if constraints.cap_space < 0:
        cap_flex += _clamp((-constraints.cap_space) / max(1.0, abs(constraints.payroll)), 0.0, 0.25)

    # If rebuilding, cap flex is often also valued
    if horizon == "REBUILD":
        cap_flex += 0.10

    cap_flex = _clamp(cap_flex, 0.0, 1.0)

    # Normalize to be interpretable but not forced sum=1 (later logic can use them independently)
    return {
        "WIN_NOW": float(win_now),
        "PICKS": float(picks),
        "CAP_FLEX": float(cap_flex),
    }


def _compute_urgency(
    *,
    tier: CompetitiveTier,
    horizon: TimeHorizon,
    deadline_pressure: float,
    patience: float,
    trend: float,
    need_intensity: float,
    apron_status: str,
) -> float:
    dp = _clamp(deadline_pressure, 0.0, 1.0)
    patience = _clamp(patience, 0.0, 1.0)

    tier_base = {
        "CONTENDER": 0.55,
        "PLAYOFF_BUYER": 0.45,
        "FRINGE": 0.40,
        "RESET": 0.45,
        "REBUILD": 0.35,
        "TANK": 0.30,
    }.get(tier, 0.40)

    # Contenders feel more urgency near deadline; rebuilders also feel some for selling vets
    deadline_weight = 0.30 if tier in ("CONTENDER", "PLAYOFF_BUYER") else 0.22
    need_weight = 0.25
    patience_weight = 0.18
    trend_weight = 0.12
    apron_weight = 0.10

    tr = _clamp(trend, -0.25, 0.25)
    trend_push = 0.0
    if tier in ("CONTENDER", "PLAYOFF_BUYER"):
        # negative trend pushes urgency
        trend_push = _clamp(-tr * 1.2, 0.0, 0.25)
    elif tier in ("FRINGE", "RESET"):
        trend_push = _clamp(abs(tr) * 0.8, 0.0, 0.20)

    apron_push = 0.0
    if apron_status == "ABOVE_2ND_APRON":
        apron_push = 0.18
    elif apron_status == "ABOVE_1ST_APRON":
        apron_push = 0.10

    u = tier_base
    u += deadline_weight * dp
    u += need_weight * _clamp(need_intensity, 0.0, 1.0)
    u += patience_weight * (1.0 - patience)
    u += trend_weight * trend_push
    u += apron_weight * apron_push

    # Horizon adjustments
    if horizon == "WIN_NOW":
        u += 0.05
    elif horizon == "REBUILD":
        u -= 0.03

    return float(_clamp(u, 0.0, 1.0))


def _count_positions(players: List[Dict[str, Any]]) -> Dict[str, int]:
    # G/W/B coarse buckets, based on pos strings
    counts = {"G": 0, "W": 0, "B": 0}
    for p in players:
        pos = str(p.get("pos") or "").upper()
        if not pos:
            continue
        if "PG" in pos or "SG" in pos or pos in ("G",):
            counts["G"] += 1
        elif "SF" in pos or pos in ("F", "WF", "WG") or "W" in pos:
            counts["W"] += 1
        elif "PF" in pos or "C" in pos or pos in ("B",):
            counts["B"] += 1
        else:
            # fallback by last char
            if pos.endswith("G"):
                counts["G"] += 1
            elif pos.endswith("F"):
                counts["W"] += 1
            elif pos.endswith("C"):
                counts["B"] += 1
    return counts


def _salary_buckets(players: List[Dict[str, Any]]) -> Dict[str, int]:
    # rough buckets for realism
    out = {"MIN": 0, "MID": 0, "BIG": 0, "MAX": 0}
    for p in players:
        s = _safe_float(p.get("salary"), 0.0)
        if s <= 2_500_000:
            out["MIN"] += 1
        elif s <= 10_000_000:
            out["MID"] += 1
        elif s <= 25_000_000:
            out["BIG"] += 1
        else:
            out["MAX"] += 1
    return out


def _build_reasons(
    tid: str,
    tier: CompetitiveTier,
    horizon: TimeHorizon,
    posture: TradePosture,
    sig: TeamSituationSignals,
    constraints: TeamConstraints,
    roster_sig: Dict[str, Any],
    asset_sig: Dict[str, Any],
    style_sig: Dict[str, Any],
    prefs: Dict[str, float],
    needs: List[TeamNeed],
) -> List[str]:
    r: List[str] = []

    # performance line
    rank = sig.conf_rank
    gb = sig.gb
    if rank is not None:
        if gb is not None:
            r.append(f"현재 컨퍼런스 {rank}위(승률 {sig.win_pct:.3f}, GB {gb:.1f}), 최근 10경기 승률 {sig.last10_win_pct:.0%}.")
        else:
            r.append(f"현재 컨퍼런스 {rank}위(승률 {sig.win_pct:.3f}), 최근 10경기 승률 {sig.last10_win_pct:.0%}.")
    else:
        r.append(f"현재 승률 {sig.win_pct:.3f}, 최근 10경기 승률 {sig.last10_win_pct:.0%}.")

    # trend
    if sig.trend >= 0.05:
        r.append("최근 흐름이 상승세라 로테이션 보강 시 '한 단계 상승' 기대치가 큼.")
    elif sig.trend <= -0.05:
        r.append("최근 흐름이 하락세라 문제 포지션/역할을 빠르게 보완할 필요가 있음.")

    # roster quality
    r.append(f"탑3 평균 OVR {roster_sig.get('top3_avg', 0.0):.1f}, 탑8 평균 OVR {roster_sig.get('top8_avg', 0.0):.1f} (스타파워 {sig.star_power:.2f}, 뎁스 {sig.depth:.2f}).")
    if sig.core_age > 0:
        r.append(f"코어 평균 나이 {sig.core_age:.1f}세, 유망주/젊은코어 지표 {sig.young_core:.2f}.")

    # cap / apron
    cap_m = constraints.cap_space / 1_000_000.0
    pay_m = constraints.payroll / 1_000_000.0
    r.append(f"팀 샐러리 {pay_m:.1f}M, 캡 스페이스 {cap_m:.1f}M, 상태: {constraints.apron_status}.")
    if constraints.hard_flags:
        keys = ", ".join(sorted(constraints.hard_flags.keys()))
        r.append(f"룰/제약 플래그: {keys} → 트레이드 설계가 까다로울 수 있음.")
    if constraints.locks_count > 0:
        r.append(f"현재 협상/락 걸린 자산이 {constraints.locks_count}개 있어 선택지가 일부 제한됨.")

    # assets
    r.append(
        f"향후 {asset_sig.get('max_years', 7)}년 자산: 1R {asset_sig.get('firsts', 0)}장, 2R {asset_sig.get('seconds', 0)}장, 스왑 {asset_sig.get('swaps', 0)}개 (자산점수 {sig.asset_score:.2f})."
    )

    # style
    r.append(f"공격 성향: 3점 비중 {sig.style_3_rate:.0%}, 림 공격 비중 {sig.style_rim_rate:.0%}.")

    # headline decision
    r.append(f"상황 평가: {tier} / {horizon} / 트레이드 스탠스 {posture}.")

    # prefs
    r.append(f"선호도(0~1): 즉전감 {prefs.get('WIN_NOW', 0.0):.2f}, 픽/유망주 {prefs.get('PICKS', 0.0):.2f}, 캡유연성 {prefs.get('CAP_FLEX', 0.0):.2f}.")

    # top needs summary
    if needs:
        top = sorted(needs, key=lambda n: -n.weight)[:3]
        top_str = ", ".join([f"{n.tag}({n.weight:.2f})" for n in top])
        r.append(f"우선 니즈: {top_str}.")

    return r[:12]


