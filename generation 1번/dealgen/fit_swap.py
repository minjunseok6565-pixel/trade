from __future__ import annotations

from dataclasses import dataclass
import random
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from ...models import Deal, PlayerAsset
from ...valuation.types import DealVerdict

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import TradeAssetCatalog, TeamOutgoingCatalog, PlayerTradeCandidate

from .types import DealGeneratorConfig, DealGeneratorBudget, DealGeneratorStats, DealProposal, DealCandidate
from .repair import repair_until_valid
from .scoring import evaluate_and_score, _should_discard_prop


@dataclass(frozen=True, slots=True)
class FitSwapResult:
    proposal: Optional[DealProposal]
    validations_used: int
    evaluations_used: int
    candidates_tried: int = 0
    swapped: bool = False


def _has_reason(dec: Any, code: str) -> bool:
    reasons = getattr(dec, "reasons", None) or tuple()
    for r in reasons:
        try:
            if str(getattr(r, "code", "") or "") == code:
                return True
        except Exception:
            continue
    return False


def _extract_fit_failed_incoming_player_ids(dec: Any) -> Set[str]:
    """
    trade.zip decision_policy.py:
      FIT_FAILS.meta = {"failed_count": int, "failed_samples": [{"asset_key":"player:<id>", "ref_id":...}, ...]}
    """
    out: Set[str] = set()
    if dec is None:
        return out

    reasons = getattr(dec, "reasons", None) or tuple()
    for r in reasons:
        try:
            if str(getattr(r, "code", "") or "") != "FIT_FAILS":
                continue
        except Exception:
            continue

        meta: Optional[Mapping[str, Any]] = None
        v = getattr(r, "meta", None)
        if isinstance(v, Mapping):
            meta = v
        if not isinstance(meta, Mapping):
            continue

        samples = meta.get("failed_samples")
        if not isinstance(samples, (list, tuple)):
            continue

        for s in samples:
            if not isinstance(s, Mapping):
                continue
            akey = str(s.get("asset_key") or "").strip()
            if akey.startswith("player:"):
                pid = akey.split("player:", 1)[1].strip()
                if pid:
                    out.add(pid)
                continue
            # fallback: numeric-only ref_id
            rid = str(s.get("ref_id") or "").strip()
            if rid and re.match(r"^[0-9]+$", rid):
                out.add(rid)

    return out


def _team_need_map(tick_ctx: TradeGenerationTickContext, team_id: str) -> Dict[str, float]:
    tid = str(team_id).upper()
    # prefer decision_context.need_map
    try:
        dc = tick_ctx.get_decision_context(tid)
        nm = getattr(dc, "need_map", None)
        if isinstance(nm, dict):
            return {str(k).upper(): float(v) for k, v in nm.items()}
    except Exception:
        pass

    # fallback: team_situation.needs (best-effort)
    try:
        ts = tick_ctx.get_team_situation(tid)
        needs = getattr(ts, "needs", None)
        if isinstance(needs, dict):
            return {str(k).upper(): float(v) for k, v in needs.items()}
    except Exception:
        pass

    return {}


def _need_fit_score(supply: Mapping[str, float], need_map: Mapping[str, float]) -> float:
    # simple weighted overlap; normalized to ~0..1-ish
    if not supply or not need_map:
        return 0.0
    num = 0.0
    den = 0.0
    for tag, w in need_map.items():
        ww = float(w)
        if ww <= 0:
            continue
        den += ww
        num += ww * float(supply.get(tag, 0.0) or 0.0)
    if den <= 0:
        return 0.0
    return float(num / den)


def _outgoing_players_to_receiver(
    deal: Deal,
    *,
    giver_id: str,
    receiver_id: str,
) -> Sequence[PlayerAsset]:
    # 2-team deal 기준: giver leg의 player는 receiver로 간다고 가정.
    # 3-team 확장 시 to_team이 있으면 receiver match만 추린다.
    leg = list(deal.legs.get(str(giver_id).upper(), []) or [])
    out: list[PlayerAsset] = []
    for a in leg:
        if not isinstance(a, PlayerAsset):
            continue
        to_team = getattr(a, "to_team", None)
        if to_team is not None and str(to_team).upper() != str(receiver_id).upper():
            continue
        out.append(a)
    return out


def _pick_replacement_pool(
    out_cat: TeamOutgoingCatalog,
    *,
    buckets: Tuple[str, ...],
    exclude: Set[str],
    to_team: str,
    outgoing_players_count: int,
) -> Sequence[PlayerTradeCandidate]:
    pool: list[PlayerTradeCandidate] = []
    for b in buckets:
        for pid in out_cat.player_ids_by_bucket.get(b, tuple()):
            p = out_cat.players.get(pid)
            if p is None:
                continue
            if str(pid) in exclude:
                continue
            if bool(getattr(p.lock, "is_locked", False)):
                continue
            # return ban
            if to_team and str(to_team).upper() in set(str(x).upper() for x in (p.return_ban_teams or tuple())):
                continue
            # aggregation solo-only cannot be bundled
            if bool(getattr(p, "aggregation_solo_only", False)) and outgoing_players_count >= 2:
                continue
            pool.append(p)
    return pool


def _team_posture(ts: Any) -> str:
    return str(getattr(ts, "trade_posture", "") or "").upper()


def _team_time_horizon(ts: Any) -> str:
    return str(getattr(ts, "time_horizon", "") or "").upper()


def _team_competitive_tier(ts: Any) -> str:
    return str(getattr(ts, "competitive_tier", "") or "").upper()


def _fit_swap_mode_for_receiver(ts: Any) -> str:
    """Return one of: 'REBUILD', 'WIN_NOW', 'NEUTRAL'."""
    if ts is None:
        return "NEUTRAL"

    horizon = _team_time_horizon(ts)
    posture = _team_posture(ts)
    tier = _team_competitive_tier(ts)

    rebuild_like = (
        horizon in {"REBUILD", "RE_TOOL", "RETOOL"}
        or tier in {"REBUILD", "RESET", "TANK"}
    )
    win_now_like = (
        horizon in {"WIN_NOW", "CONTEND", "COMPETE"}
        or posture in {"AGGRESSIVE_BUY", "SOFT_BUY"}
        or tier in {"CONTENDER", "PLAYOFF_BUYER"}
    )

    if rebuild_like:
        return "REBUILD"
    if win_now_like:
        return "WIN_NOW"
    return "NEUTRAL"


def _fit_swap_youth_score(c: PlayerTradeCandidate, cfg: DealGeneratorConfig) -> float:
    """
    youth = max(0, age_anchor - age) / age_span  +  min(years_cap, remaining_years) / years_span
    (v2 absorption; parameters configurable in DealGeneratorConfig)
    """
    age = None
    try:
        snap = getattr(c, "snap", None)
        if snap is not None and getattr(snap, "age", None) is not None:
            age = float(getattr(snap, "age"))
    except Exception:
        age = None

    try:
        ry = float(getattr(c, "remaining_years", 0.0) or 0.0)
    except Exception:
        ry = 0.0

    age_anchor = float(getattr(cfg, "fit_swap_youth_age_anchor", 30.0) or 30.0)
    age_span = float(getattr(cfg, "fit_swap_youth_age_span", 10.0) or 10.0)
    years_cap = float(getattr(cfg, "fit_swap_youth_years_cap", 4.0) or 4.0)
    years_span = float(getattr(cfg, "fit_swap_youth_years_span", 4.0) or 4.0)

    youth = 0.0
    if age is not None and age_span > 0:
        youth += max(0.0, age_anchor - float(age)) / age_span
    if years_span > 0:
        youth += min(years_cap, max(0.0, float(ry))) / years_span
    return float(youth)


def _fit_swap_primary_score(
    c: PlayerTradeCandidate,
    *,
    fit: float,
    mode: str,
    cfg: DealGeneratorConfig,
) -> float:
    """
    primary_score = w_youth*youth + w_fit*fit + w_market*market_norm
    - if fit_swap_use_horizon_weights is False, fall back to fit-only ranking (v1 behavior)
    """
    if not bool(getattr(cfg, "fit_swap_use_horizon_weights", True)):
        return float(fit)

    market_total = 0.0
    try:
        market_total = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
    except Exception:
        market_total = 0.0

    div = float(getattr(cfg, "fit_swap_market_norm_divisor", 50.0) or 50.0)
    market_norm = (market_total / div) if div > 0 else market_total

    youth = _fit_swap_youth_score(c, cfg)

    mm = str(mode).upper()
    if mm == "REBUILD":
        w = getattr(cfg, "fit_swap_weights_rebuild", (0.55, 0.40, -0.05))
    elif mm == "WIN_NOW":
        w = getattr(cfg, "fit_swap_weights_win_now", (0.05, 0.70, 0.25))
    else:
        w = getattr(cfg, "fit_swap_weights_neutral", (0.20, 0.60, 0.20))

    try:
        wy, wf, wm = float(w[0]), float(w[1]), float(w[2])
    except Exception:
        wy, wf, wm = 0.20, 0.60, 0.20

    return float(wy * youth + wf * float(fit) + wm * float(market_norm))


def maybe_apply_fit_swap(
    base_prop: DealProposal,
    *,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    budget: DealGeneratorBudget,
    allow_locked_by_deal_id: Optional[str],
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    banned_receivers_by_player: Dict[str, Set[str]],
    protected_player_id: Optional[str],
    opponent_repeat_count: int,
    rng: random.Random,
    validations_remaining: int,
    evaluations_remaining: int,
    stats: DealGeneratorStats,
) -> Optional[FitSwapResult]:
    """
    FIT_FAILS(=받는 쪽 fit 불만)일 때,
    보내는 쪽 outgoing player 1명을 '더 맞는 선수'로 교체해 COUNTER를 시도.
    """

    if not bool(getattr(config, "fit_swap_enabled", True)):
        return None
    # evaluate_and_score consumes 2 evaluations (buyer + seller)
    if validations_remaining <= 0 or evaluations_remaining < 2:
        return None

    buyer_id = str(base_prop.buyer_id).upper()
    seller_id = str(base_prop.seller_id).upper()

    # 어떤 쪽이 FIT_FAILS로 불만인가? (기본: seller 우선, 필요시 buyer도 지원)
    receiver_id: Optional[str] = None
    giver_id: Optional[str] = None
    receiver_dec = None

    if base_prop.seller_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and _has_reason(base_prop.seller_decision, "FIT_FAILS"):
        receiver_id, giver_id, receiver_dec = seller_id, buyer_id, base_prop.seller_decision
    elif base_prop.buyer_decision.verdict in (DealVerdict.REJECT, DealVerdict.COUNTER) and _has_reason(base_prop.buyer_decision, "FIT_FAILS"):
        receiver_id, giver_id, receiver_dec = buyer_id, seller_id, base_prop.buyer_decision
    else:
        return None

    if receiver_id is None or giver_id is None or receiver_dec is None:
        return None

    receiver_need_map = _team_need_map(tick_ctx, receiver_id)
    if not receiver_need_map:
        return None

    # receiver(=FIT_FAILS 낸 팀)의 타임라인/포스처 기반 선호 모드 (v2 absorption)
    receiver_ts = None
    try:
        receiver_ts = tick_ctx.get_team_situation(receiver_id)
    except Exception:
        receiver_ts = None
    fit_swap_mode = _fit_swap_mode_for_receiver(receiver_ts)

    giver_out = catalog.outgoing_by_team.get(giver_id)
    if giver_out is None:
        return None

    outgoing_players = list(_outgoing_players_to_receiver(base_prop.deal, giver_id=giver_id, receiver_id=receiver_id))
    if not outgoing_players:
        return None

    protected: Set[str] = set()
    if protected_player_id:
        protected.add(str(protected_player_id))
    # (sell-mode 보호용) deal meta에 보호 리스트가 있으면 포함하고 싶다면 여기서 확장 가능.

    failed_pids = _extract_fit_failed_incoming_player_ids(receiver_dec)

    # swap-out 대상: failed_pids에 걸린 애 우선, 없으면 전체 중 fit 가장 낮은 애
    def _fit_pid(pid: str) -> float:
        c = giver_out.players.get(pid)
        if c is None:
            return 0.0
        return _need_fit_score(getattr(c, "supply", None) or {}, receiver_need_map)

    outgoing_pids = [str(a.player_id) for a in outgoing_players]
    swap_out_pool = [pid for pid in outgoing_pids if pid in failed_pids and pid not in protected]
    if not swap_out_pool:
        swap_out_pool = [pid for pid in outgoing_pids if pid not in protected]

    if not swap_out_pool:
        return None

    worst_pid = None
    worst_fit = 1e9
    worst_salary = 0.0
    worst_market = 0.0

    for pid in swap_out_pool:
        f = _fit_pid(pid)
        if f < worst_fit:
            worst_fit = f
            worst_pid = pid
            c = giver_out.players.get(pid)
            if c is not None:
                worst_salary = float(c.salary_m)
                worst_market = float(c.market.total)

    if worst_pid is None:
        return None

    # replacement 후보 풀
    exclude = set(outgoing_pids) | set(protected)
    buckets = ("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "CONSOLIDATE", "EXPIRING", "FILLER_CHEAP")
    pool = _pick_replacement_pool(
        giver_out,
        buckets=buckets,
        exclude=exclude,
        to_team=receiver_id,
        outgoing_players_count=len(outgoing_players),
    )
    if not pool:
        return None

    max_pool = int(getattr(config, "fit_swap_candidate_pool", 0) or 0)
    if max_pool > 0 and len(pool) > max_pool:
        pool = list(rng.sample(list(pool), max_pool))

    # fit-swap counter에서는 repair budget을 더 보수적으로(기본 1회)
    fit_max_repairs = int(getattr(config, "fit_swap_max_repairs", budget.max_repairs) or budget.max_repairs)
    fit_budget = DealGeneratorBudget(
        max_targets=budget.max_targets,
        beam_width=budget.beam_width,
        max_attempts_per_target=budget.max_attempts_per_target,
        max_validations=budget.max_validations,
        max_evaluations=budget.max_evaluations,
        # Even with 0 repairs, repair_until_valid will validate at least once.
        # Cap repairs so we can't exceed remaining validations.
        max_repairs=min(int(budget.max_repairs), max(0, int(fit_max_repairs)), max(0, int(validations_remaining) - 1)),
    )

    max_salary_diff = float(getattr(config, "fit_swap_max_salary_diff_m", 3.5) or 3.5)
    min_improve = float(getattr(config, "fit_swap_min_fit_improvement", 0.03) or 0.03)

    # 후보 랭킹:
    # - (유지) 최소 fit 개선 + salary diff 제한 + market 급변 억제
    # - (추가) receiver 타임라인/포스처에 따라 youth/fit/market 가중치로 primary_score를 계산 (v2 absorption)
    ranked: list[Tuple[float, float, float, str, float]] = []
    for c in pool:
        new_fit = _need_fit_score(getattr(c, "supply", None) or {}, receiver_need_map)
        if float(new_fit) <= float(worst_fit) + float(min_improve):
            continue
        sal = float(getattr(c, "salary_m", 0.0) or 0.0)
        if abs(sal - float(worst_salary)) > max_salary_diff:
            continue
        mkt = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)

        primary = _fit_swap_primary_score(
            c,
            fit=float(new_fit),
            mode=str(fit_swap_mode).upper(),
            cfg=config,
        )

        # primary desc, salary diff asc, market diff asc, pid asc
        ranked.append((float(primary), abs(sal - float(worst_salary)), abs(mkt - float(worst_market)), str(c.player_id), float(new_fit)))

    if not ranked:
        return None

    ranked.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))

    # v2처럼 상위 N개만(폭발 방지)
    max_tries = int(getattr(config, "fit_swap_try_top_n", 6) or 6)
    max_tries = max(1, min(12, max_tries))

    validations_used = 0
    evaluations_used = 0
    candidates_tried = 0

    for _, __, ___, new_pid, new_fit in ranked[:max_tries]:
        candidates_tried += 1
        if validations_used >= validations_remaining or evaluations_used >= evaluations_remaining:
            break

        # deal clone + replace
        new_deal = Deal(
            teams=list(base_prop.deal.teams),
            legs={k: list(v) for k, v in base_prop.deal.legs.items()},
            meta=dict(base_prop.deal.meta or {}),
        )
        leg = list(new_deal.legs.get(giver_id, []) or [])
        replaced = False
        for i in range(len(leg)):
            if isinstance(leg[i], PlayerAsset) and str(leg[i].player_id) == str(worst_pid):
                leg[i] = PlayerAsset(kind="player", player_id=str(new_pid))
                replaced = True
                break
        if not replaced:
            continue
        new_deal.legs[giver_id] = leg

        cand = DealCandidate(
            deal=new_deal,
            buyer_id=buyer_id,
            seller_id=seller_id,
            focal_player_id=str(protected_player_id) if protected_player_id else "",
            archetype="fit_swap",
            tags=list(base_prop.tags) + ["counter:fit_swap", f"fit_swap_mode:{str(fit_swap_mode).lower()}"],
            repairs_used=0,
        )

        ok, cand2, v_used = repair_until_valid(
            cand,
            tick_ctx,
            catalog,
            config,
            allow_locked_by_deal_id=allow_locked_by_deal_id,
            budget=fit_budget,
            banned_asset_keys=banned_asset_keys,
            banned_players=banned_players,
            banned_receivers_by_player=banned_receivers_by_player,
            stats=stats,
        )
        validations_used += int(v_used)
        if not ok or cand2 is None:
            continue

        prop2, e_used = evaluate_and_score(
            cand2.deal,
            buyer_id=buyer_id,
            seller_id=seller_id,
            tick_ctx=tick_ctx,
            config=config,
            tags=tuple(cand2.tags),
            opponent_repeat_count=int(opponent_repeat_count),
            stats=stats,
        )
        evaluations_used += int(e_used)
        if prop2 is None:
            continue

        if _should_discard_prop(prop2, config):
            continue

        return FitSwapResult(
            proposal=prop2,
            validations_used=int(validations_used),
            evaluations_used=int(evaluations_used),
            candidates_tried=int(candidates_tried),
            swapped=True,
        )

    if candidates_tried > 0 or validations_used > 0 or evaluations_used > 0:
        return FitSwapResult(
            proposal=None,
            validations_used=int(validations_used),
            evaluations_used=int(evaluations_used),
            candidates_tried=int(candidates_tried),
            swapped=False,
        )
    return None
