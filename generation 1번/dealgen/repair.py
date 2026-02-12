from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from ...errors import TradeError
from ...models import PlayerAsset, PickAsset, Asset, resolve_asset_receiver

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import TradeAssetCatalog, BucketId

from .types import DealGeneratorConfig, DealGeneratorBudget, DealGeneratorStats, DealCandidate, RuleFailureKind, parse_trade_error
from .utils import (
    _count_picks,
    _count_players,
    _current_pick_ids,
    _pick_best_pick_id,
    _shape_ok,
)
 

# =============================================================================
# Validate + Repair
# =============================================================================

def repair_until_valid(
    cand: DealCandidate,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    *,
    allow_locked_by_deal_id: Optional[str],
    budget: DealGeneratorBudget,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    stats: DealGeneratorStats,
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
) -> Tuple[bool, Optional[DealCandidate], int]:
    """validate -> 실패 유형에 따라 최대 budget.max_repairs회 repair.

    Returns: (ok, candidate_or_none, validations_used)
    """

    validations_used = 0
    if banned_receivers_by_player is None:
        banned_receivers_by_player = {}

    if not _shape_ok(cand.deal, config=config, catalog=catalog):
        return False, None, validations_used

    for _ in range(int(budget.max_repairs) + 1):
        try:
            tick_ctx.validate_deal(cand.deal, allow_locked_by_deal_id=allow_locked_by_deal_id)
            validations_used += 1
            return True, cand, validations_used
        except TradeError as err:
            validations_used += 1
            failure = parse_trade_error(err)
            stats.bump_failure(str(failure.kind.value))

            if cand.repairs_used >= int(budget.max_repairs):
                _apply_prune_side_effects(failure, banned_asset_keys, banned_players, banned_receivers_by_player)
                return False, None, validations_used

            repaired = repair_once(
                cand,
                failure,
                tick_ctx=tick_ctx,
                catalog=catalog,
                config=config,
                banned_asset_keys=banned_asset_keys,
                banned_players=banned_players,
            )
            if not repaired:
                _apply_prune_side_effects(failure, banned_asset_keys, banned_players, banned_receivers_by_player)
                return False, None, validations_used

            cand.repairs_used += 1
            stats.repairs += 1

            # repair 후 shape check
            if not _shape_ok(cand.deal, config=config, catalog=catalog):
                return False, None, validations_used
        except Exception:
            # 상업용 루프: 예상 못한 예외로 tick이 죽지 않게 방어
            validations_used += 1
            stats.bump_failure("unexpected_exception_validate")
            return False, None, validations_used

    return False, None, validations_used


def repair_once(
    cand: DealCandidate,
    failure: RuleFailure,
    *,
    tick_ctx: TradeGenerationTickContext,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
) -> bool:
    """실패 유형에 따라 '최소 수정' 1회 적용.

    True면 cand.deal이 mutate되었음을 의미.
    False면 이 후보는 prune.
    """

    # 구조적으로 수리 의미가 거의 없는 유형
    if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
        return False

    if failure.kind == RuleFailureKind.PLAYER_ELIGIBILITY:
        reason = failure.reason or ""
        pid = failure.player_id
        if not pid:
            return False
        if reason == "recent_contract_signing":
            banned_players.add(pid)
            return False
        if reason == "aggregation_ban":
            # aggregation_ban: 해당 선수는 '트레이드 불가'가 아니라
            # '다른 선수와 묶어서(2+ outgoing) 보낼 수 없음'이므로,
            # 최소 수정은 pid를 유지하고 나머지 outgoing player를 제거하여 1-for-1로 만드는 것이다.
            team_id = str(failure.team_id or "").upper()
            if not team_id or team_id not in cand.deal.legs:
                return False
            assets = list(cand.deal.legs[team_id] or [])
            players = [a for a in assets if isinstance(a, PlayerAsset)]
            if len(players) <= 1:
                return False

            keep_player: Optional[PlayerAsset] = None
            for a in players:
                if a.player_id == pid:
                    keep_player = a
                    break
            if keep_player is None:
                # fallback: pid가 leg에 없으면 첫 번째 player만 남긴다.
                keep_player = players[0]

            non_players: List[Asset] = [a for a in assets if not isinstance(a, PlayerAsset)]
            cand.deal.legs[team_id] = [keep_player] + non_players
            cand.tags.append("repair:aggregation_keep_solo")
            return True
        return False

    if failure.kind == RuleFailureKind.RETURN_TO_TRADING_TEAM:
        return False

    if failure.kind == RuleFailureKind.ROSTER_LIMIT:
        team_id = str(failure.team_id or "").upper()
        if not team_id:
            return False
        return _repair_roster_limit(cand, team_id, catalog, config)

    if failure.kind in (RuleFailureKind.SALARY_MATCHING, RuleFailureKind.SECOND_APRON_ONE_FOR_ONE):
        team_id = str(failure.team_id or "").upper()
        if not team_id:
            return False
        if failure.kind == RuleFailureKind.SECOND_APRON_ONE_FOR_ONE:
            return _repair_second_apron_one_for_one(cand, team_id, catalog)
        return _repair_salary_matching(cand, team_id, catalog, config, failure)

    if failure.kind == RuleFailureKind.PICK_RULES:
        team_id = str(failure.team_id or "").upper()
        return _repair_pick_rules(cand, team_id, catalog, config, failure)

    return False


def _apply_prune_side_effects(
    failure: RuleFailure,
    banned_asset_keys: Set[str],
    banned_players: Set[str],
    banned_receivers_by_player: Optional[Dict[str, Set[str]]] = None,
) -> None:
    # (C) 같은 invalid를 반복 생성하지 않도록 금지 목록에 반영
    if failure.kind in (RuleFailureKind.ASSET_LOCK, RuleFailureKind.OWNERSHIP, RuleFailureKind.DUPLICATE_ASSET):
        if failure.asset_key:
            banned_asset_keys.add(failure.asset_key)

    # ownership에서 플레이어 미소유는 플레이어 후보 자체를 금지하면 효과가 좋다.
    if failure.kind == RuleFailureKind.OWNERSHIP and failure.player_id:
        banned_players.add(failure.player_id)

    if failure.kind == RuleFailureKind.PLAYER_ELIGIBILITY and failure.player_id and failure.reason == "recent_contract_signing":
        banned_players.add(failure.player_id)

    # Return-to-trading-team: 특정 player가 특정 receiver로 못 가는 조합을 학습해서 재발 방지
    # (types.py가 아직 to_team을 보존하지 않는 상태에서도 안전하게 동작하도록 getattr 사용)
    if banned_receivers_by_player is not None and failure.kind == RuleFailureKind.RETURN_TO_TRADING_TEAM:
        pid = getattr(failure, "player_id", None)
        to_team = getattr(failure, "to_team", None)
        if pid and to_team:
            pid_s = str(pid)
            to_u = str(to_team).upper()
            banned_receivers_by_player.setdefault(pid_s, set()).add(to_u)


@dataclass(frozen=True, slots=True)
class SalaryMatchSimResult:
    """SalaryMatchingRule의 핵심 계산을 로컬에서 재현한 결과(달러 단위 정수).

    SSOT(TradeGenerationTickContext.validate_deal)에서 나온 TradeError.details를 기반으로,
    repair 단계에서 '이 filler를 추가하면 통과할 가능성이 있는가?'를 빠르게 판정하기 위해 사용한다.

    주의:
    - 실제 SSOT는 float + math.floor 기반이므로, floor 연산에는 eps를 더해 부동소수 오차를 상쇄한다.
    - BELOW_FIRST_APRON의 large 구간(1.25배)은 정수 연산((out*5)//4)로 정확히 재현한다.
    """

    ok: bool
    status: str
    method: str
    allowed_in_d: int
    payroll_after_d: int
    max_incoming_cap_room_d: Optional[int] = None
    reason: Optional[str] = None


def _to_int_dollars(x: Any) -> int:
    """float/int/str 등을 달러 단위 정수로 안전하게 변환."""
    try:
        return int(round(float(x)))
    except Exception:
        return 0


def _simulate_salary_matching(
    *,
    payroll_before_d: int,
    outgoing_salary_d: int,
    incoming_salary_d: int,
    trade_rules: Mapping[str, Any],
    eps: float = 1e-6,
) -> SalaryMatchSimResult:
    """SalaryMatchingRule.validate()의 계산을 달러 정수로 재현.

    Returns:
        SalaryMatchSimResult(ok, status, method, allowed_in_d, payroll_after_d, ...)
    """

    # defaults: trade/trades/rules/builtin/salary_matching_rule.py 와 동일
    salary_cap_d = _to_int_dollars(trade_rules.get("salary_cap") or 0.0)
    first_apron_d = _to_int_dollars(trade_rules.get("first_apron") or 0.0)
    second_apron_d = _to_int_dollars(trade_rules.get("second_apron") or 0.0)

    match_small_out_max_d = _to_int_dollars(trade_rules.get("match_small_out_max") or 7_500_000)
    match_mid_out_max_d = _to_int_dollars(trade_rules.get("match_mid_out_max") or 29_000_000)
    match_mid_add_d = _to_int_dollars(trade_rules.get("match_mid_add") or 7_500_000)
    match_buffer_d = _to_int_dollars(trade_rules.get("match_buffer") or 250_000)

    first_apron_mult = float(trade_rules.get("first_apron_mult") or 1.10)
    second_apron_mult = float(trade_rules.get("second_apron_mult") or 1.00)

    payroll_after_d = int(payroll_before_d - outgoing_salary_d + incoming_salary_d)

    if payroll_after_d >= second_apron_d:
        status = "SECOND_APRON"
    elif payroll_after_d >= first_apron_d:
        status = "FIRST_APRON"
    else:
        status = "BELOW_FIRST_APRON"

    # cap-room exception (SSOT와 동일한 위치/순서)
    if payroll_before_d < salary_cap_d:
        cap_room_d = salary_cap_d - payroll_before_d
        max_incoming_d = cap_room_d + outgoing_salary_d
        if incoming_salary_d <= max_incoming_d:
            return SalaryMatchSimResult(
                ok=True,
                status=status,
                method="cap_room",
                allowed_in_d=max_incoming_d,
                payroll_after_d=payroll_after_d,
                max_incoming_cap_room_d=max_incoming_d,
                reason="cap_room_ok",
            )

    if outgoing_salary_d <= 0:
        return SalaryMatchSimResult(
            ok=False,
            status=status,
            method="outgoing_required",
            allowed_in_d=0,
            payroll_after_d=payroll_after_d,
            reason="outgoing_required",
        )

    # NOTE: SECOND_APRON one-for-one 제약은 여기서는 다루지 않는다.
    # (generator가 SECOND_APRON에 대해 별도 repair 루트를 가지며, 이 helper는 'allowed_in' 계산 목적)

    if status == "SECOND_APRON":
        allowed_in_d = int(math.floor(outgoing_salary_d * second_apron_mult + eps))
        method = "outgoing_second_apron"
    elif status == "FIRST_APRON":
        allowed_in_d = int(math.floor(outgoing_salary_d * first_apron_mult + eps))
        method = "outgoing_first_apron"
    else:
        if outgoing_salary_d <= match_small_out_max_d:
            allowed_in_d = int(2 * outgoing_salary_d + match_buffer_d)
        elif outgoing_salary_d <= match_mid_out_max_d:
            allowed_in_d = int(outgoing_salary_d + match_mid_add_d)
        else:
            allowed_in_d = int((outgoing_salary_d * 5) // 4 + match_buffer_d)
        method = "outgoing_below_first_apron"

    if incoming_salary_d > allowed_in_d:
        return SalaryMatchSimResult(
            ok=False,
            status=status,
            method=method,
            allowed_in_d=allowed_in_d,
            payroll_after_d=payroll_after_d,
            reason="incoming_gt_allowed_in",
        )

    return SalaryMatchSimResult(
        ok=True,
        status=status,
        method=method,
        allowed_in_d=allowed_in_d,
        payroll_after_d=payroll_after_d,
        reason="ok",
    )


def _repair_salary_matching(
    cand: DealCandidate,
    failing_team: str,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    failure: RuleFailure,
) -> bool:
    """SalaryMatchingRule 실패 수리.

    가장 안전한 수리:
    - failing_team outgoing에 filler 1명을 추가(FILLER_CHEAP -> EXPIRING -> FILLER_BAD_CONTRACT)

    단, failure.details.status == SECOND_APRON이면 multi-player가 2nd apron one-for-one을
    촉발할 가능성이 매우 높으므로 여기서 추가 수리를 시도하지 않는다.
    """

    status = str(failure.status or "")
    method = str(failure.method or "")
    if status == "SECOND_APRON":
        # second_apron_one_for_one은 RuleFailureKind.SECOND_APRON_ONE_FOR_ONE로 별도 수리된다.
        if method == "outgoing_second_apron":
            return _repair_second_apron_salary_mismatch(cand, failing_team, catalog, config, failure)
        return False

    out_catalog = catalog.outgoing_by_team.get(failing_team)
    if out_catalog is None:
        return False

    # max_players_per_side guard
    if _count_players(cand.deal, failing_team) >= int(config.max_players_per_side):
        return False

    # aggregation_solo_only가 이미 포함되면 추가 player를 붙이면 바로 다시 실패할 확률이 큼
    for a in cand.deal.legs.get(failing_team, []):
        if isinstance(a, PlayerAsset):
            c = out_catalog.players.get(a.player_id)
            if c is not None and bool(getattr(c, "aggregation_solo_only", False)):
                return False

    # receiver team(상대팀) 계산: return-ban 프리필터에 사용
    other = [t for t in cand.deal.teams if str(t).upper() != str(failing_team).upper()]
    receiver_team = str(other[0]).upper() if other else None

    # SalaryMatchingRule failure.details는 달러(float) 기반이므로, 달러 정수로 변환해 사용한다.
    # 이 숫자들은 SSOT(validate_deal)의 '현재 딜 상태' 기준이며, filler 추가 후 상태는 여기서 재시뮬레이션한다.
    payroll_before_d = _to_int_dollars(failure.details.get("payroll_before"))
    outgoing_salary_d0 = _to_int_dollars(failure.details.get("outgoing_salary"))
    incoming_salary_d0 = _to_int_dollars(failure.details.get("incoming_salary"))

    if incoming_salary_d0 <= 0:
        return False

    # max_players_per_side guard는 이미 위에서 통과했으므로, 여기서는 후보 스캔/선정만 한다.
    already = {a.player_id for a in cand.deal.legs.get(failing_team, []) if isinstance(a, PlayerAsset)}
    # aggregation_solo_only는 "묶음(2+ outgoing) 금지"이므로,
    # 현재 outgoing이 0명(=단독 트레이드)인 경우에만 허용한다.
    allow_solo_only = (len(already) == 0)

    # 후보 filler를 버킷에서 전수 스캔하고, "salary matching을 실제로 통과시키는" 후보만 남긴다.
    buckets: Tuple[BucketId, ...] = ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT")
    seen: Set[str] = set()
    passing: List[Tuple[int, float, str]] = []  # (salary_d, market_total, player_id)

    trade_rules = catalog.trade_rules or {}

    for b in buckets:
        for pid in out_catalog.player_ids_by_bucket.get(b, tuple()):
            pid = str(pid)
            if pid in seen or pid in already:
                continue
            seen.add(pid)

            c = out_catalog.players.get(pid)
            if c is None:
                continue

            # return-ban / aggregation-solo-only 필터 (기존 _pick_bucket_player와 동일한 의도)
            if receiver_team and receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                continue
            # solo-only는 단독 outgoing이면 허용, 이미 outgoing이 있으면 추가 outgoing으로 붙이지 않음
            if bool(getattr(c, "aggregation_solo_only", False)) and not allow_solo_only:
                continue

            filler_salary_d = int(round(float(c.salary_m) * 1_000_000.0))
            if filler_salary_d <= 0:
                continue

            sim = _simulate_salary_matching(
                payroll_before_d=payroll_before_d,
                outgoing_salary_d=outgoing_salary_d0 + filler_salary_d,
                incoming_salary_d=incoming_salary_d0,
                trade_rules=trade_rules,
            )
            if not sim.ok:
                continue

            mkt = float(getattr(c.market, "total", 0.0))
            passing.append((filler_salary_d, mkt, pid))

    if not passing:
        return False

    # "필요 샐러리를 충족하는 최소 salary" 우선, 그 안에서 market.total 최소
    passing.sort(key=lambda t: (t[0], t[1], t[2]))
    filler = passing[0][2]

    cand.deal.legs[failing_team].append(PlayerAsset(kind="player", player_id=filler))
    cand.tags.append("repair:add_filler_salary")
    return True


def _repair_second_apron_salary_mismatch(
    cand: DealCandidate,
    failing_team: str,
    catalog: TradeAssetCatalog,
    config: DealGeneratorConfig,
    failure: RuleFailure,
) -> bool:
    """SECOND_APRON + method=outgoing_second_apron salary mismatch 수리.

    원칙:
    - one-for-one 형태는 유지(양쪽 leg에서 PlayerAsset 1명씩인 케이스만)
    - focal_player_id(타깃)는 가능하면 바꾸지 않는다.
      * failing_team outgoing이 focal이 아니면: failing_team outgoing을 더 비싼 선수로 교체(outgoing↑)
      * failing_team outgoing이 focal이면: 상대팀 outgoing(=failing_team incoming)을 더 싼 선수로 교체(incoming↓)
    """

    team = str(failing_team).upper()
    others = [t for t in cand.deal.teams if str(t).upper() != team]
    if not others:
        return False
    other = str(others[0]).upper()

    # --- one-for-one 형태만 다룬다(안전/비용 제한)
    out_players = [a for a in cand.deal.legs.get(team, []) if isinstance(a, PlayerAsset)]
    if len(out_players) != 1:
        return False

    incoming_players: List[PlayerAsset] = []
    for a in cand.deal.legs.get(other, []) or []:
        if not isinstance(a, PlayerAsset):
            continue
        recv = str(resolve_asset_receiver(cand.deal, other, a)).upper()
        if recv == team:
            incoming_players.append(a)
    if len(incoming_players) != 1:
        return False

    incoming_salary = float(failure.details.get("incoming_salary") or 0.0)
    outgoing_salary = float(failure.details.get("outgoing_salary") or 0.0)
    if incoming_salary <= 0.0 or outgoing_salary <= 0.0:
        return False
    if incoming_salary <= outgoing_salary:
        return False

    # dollars 기반 비교: validate(SSOT)와 정렬해 float/rounding으로 인한 재실패를 줄인다.
    # 상업용 기본값: 0.001M(=1,000달러) 수준의 최소 여유
    EPS_M = 0.001
    eps_d = int(round(EPS_M * 1_000_000.0))

    incoming_d = int(round(incoming_salary))
    outgoing_d = int(round(outgoing_salary))

    all_pids = {
        a.player_id
        for leg in cand.deal.legs.values()
        for a in (leg or [])
        if isinstance(a, PlayerAsset)
    }

    out_pid = str(out_players[0].player_id)
    focal_pid = str(cand.focal_player_id or "")

    # =========================================================
    # Case A: failing_team outgoing이 focal이 아니면 -> outgoing을 올리는 교체
    # =========================================================
    if out_pid != focal_pid:
        out_cat = catalog.outgoing_by_team.get(team)
        if out_cat is None:
            return False

        receiver_team = other
        required_out_d = incoming_d + eps_d  # SECOND_APRON: incoming <= outgoing(달러) 목표

        best_pid: Optional[str] = None
        best_key: Optional[Tuple[int, float, int]] = None  # (overshoot_d, market, salary_d)

        scan_buckets: Tuple[BucketId, ...] = (
            "FILLER_BAD_CONTRACT",
            "EXPIRING",
            "FILLER_CHEAP",
            "CONSOLIDATE",
            "SURPLUS_REDUNDANT",
            "SURPLUS_LOW_FIT",
            "VETERAN_SALE",
        )

        for b in scan_buckets:
            for pid in out_cat.player_ids_by_bucket.get(b, tuple()):
                if pid in all_pids:
                    continue
                c = out_cat.players.get(pid)
                if c is None:
                    continue
                if receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                    continue

                sal_d = int(round(float(c.salary_m) * 1_000_000.0))
                if sal_d < required_out_d:
                    continue

                overshoot_d = sal_d - required_out_d
                mkt = float(c.market.total)
                key = (overshoot_d, mkt, sal_d)
                if best_key is None or key < best_key:
                    best_key = key
                    best_pid = str(pid)

        if not best_pid:
            return False

        # failing_team leg에서 out_pid를 best_pid로 치환
        new_leg = []
        for a in cand.deal.legs.get(team, []) or []:
            if isinstance(a, PlayerAsset) and str(a.player_id) == out_pid:
                new_leg.append(PlayerAsset(kind="player", player_id=best_pid))
            else:
                new_leg.append(a)
        cand.deal.legs[team] = new_leg
        cand.tags.append("repair:second_apron_swap_out_up")
        return True

    # =========================================================
    # Case B: failing_team outgoing이 focal이면 -> incoming을 내리는 교체(상대팀 leg 교체)
    # =========================================================
    other_cat = catalog.outgoing_by_team.get(other)
    if other_cat is None:
        return False

    receiver_team = team
    max_in_d = outgoing_d - eps_d  # incoming <= outgoing(달러) 목표
    if max_in_d < 0:
        return False

    best_pid: Optional[str] = None
    best_key: Optional[Tuple[int, float]] = None  # (slack_d, market)

    scan_buckets2: Tuple[BucketId, ...] = (
        "FILLER_CHEAP",
        "EXPIRING",
        "FILLER_BAD_CONTRACT",
        "SURPLUS_REDUNDANT",
        "SURPLUS_LOW_FIT",
        "CONSOLIDATE",
        "VETERAN_SALE",
    )

    for b in scan_buckets2:
        for pid in other_cat.player_ids_by_bucket.get(b, tuple()):
            if pid in all_pids:
                continue
            c = other_cat.players.get(pid)
            if c is None:
                continue
            if receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                continue
                
            sal_d = int(round(float(c.salary_m) * 1_000_000.0))
            if sal_d > max_in_d:
                continue

            slack_d = max_in_d - sal_d  # 0에 가까울수록(outgoing에 가까울수록) 좋음
            mkt = float(c.market.total)
            key = (slack_d, mkt)
            if best_key is None or key < best_key:
                best_key = key
                best_pid = str(pid)

    if not best_pid:
        return False

    old_in_pid = str(incoming_players[0].player_id)

    # other leg에서 old_in_pid(=failing_team으로 가는 incoming player)를 best_pid로 치환
    new_leg = []
    for a in cand.deal.legs.get(other, []) or []:
        if isinstance(a, PlayerAsset) and str(a.player_id) == old_in_pid:
            recv = str(resolve_asset_receiver(cand.deal, other, a)).upper()
            if recv == team:
                new_leg.append(PlayerAsset(kind="player", player_id=best_pid))
            else:
                new_leg.append(a)
        else:
            new_leg.append(a)

    cand.deal.legs[other] = new_leg
    cand.tags.append("repair:second_apron_swap_in_down")
    return True


def _repair_second_apron_one_for_one(cand: DealCandidate, failing_team: str, catalog: TradeAssetCatalog) -> bool:
    """2nd apron one-for-one 위반: failing_team의 in/out player count를 1로 낮춘다.

    - market 기반으로 "가치가 낮아 보이는"(대개 filler) 플레이어를 우선 제거
    - 단, deal shape가 더 망가지면 prune(상위에서 재시도하게)
    """

    team = str(failing_team).upper()

    # outgoing trim (failing_team leg)
    out_assets = list(cand.deal.legs.get(team, []))
    out_players = [a for a in out_assets if isinstance(a, PlayerAsset)]
    if len(out_players) > 1:
        out_cat = catalog.outgoing_by_team.get(team)
        if out_cat is not None:
            def market(pid: str) -> float:
                c = out_cat.players.get(pid)
                return float(c.market.total) if c is not None else 0.0
            # keep the highest market (core-like), drop the rest
            keep = sorted(out_players, key=lambda a: market(a.player_id), reverse=True)[0]
        else:
            keep = out_players[0]

        cand.deal.legs[team] = [a for a in out_assets if not (isinstance(a, PlayerAsset) and a.player_id != keep.player_id)]
        cand.tags.append("repair:second_apron_trim_out")
        return True

    # incoming trim (other leg players are incoming to failing_team)
    other = [t for t in cand.deal.teams if str(t).upper() != team]
    if not other:
        return False
    other_team = str(other[0]).upper()

    other_assets = list(cand.deal.legs.get(other_team, []))
    other_players = [a for a in other_assets if isinstance(a, PlayerAsset)]
    if len(other_players) > 1:
        other_out = catalog.outgoing_by_team.get(other_team)
        if other_out is not None:
            def market(pid: str) -> float:
                c = other_out.players.get(pid)
                return float(c.market.total) if c is not None else 0.0
            # remove the lowest market (filler-like)
            pid_remove = sorted([p.player_id for p in other_players], key=market)[0]
        else:
            pid_remove = other_players[-1].player_id

        cand.deal.legs[other_team] = [a for a in other_assets if not (isinstance(a, PlayerAsset) and a.player_id == pid_remove)]
        cand.tags.append("repair:second_apron_trim_in")
        return True

    return False


def _repair_roster_limit(cand: DealCandidate, problem_team: str, catalog: TradeAssetCatalog, config: DealGeneratorConfig) -> bool:
    """ROSTER_LIMIT 수리."""

    other = [t for t in cand.deal.teams if str(t).upper() != problem_team]
    if not other:
        return False
    other_team = str(other[0]).upper()

    # 1) remove an incoming player to problem_team (player asset in other_team leg)
    other_assets = list(cand.deal.legs.get(other_team, []))
    player_ids = [a.player_id for a in other_assets if isinstance(a, PlayerAsset)]
    if len(player_ids) >= 2:
        other_out = catalog.outgoing_by_team.get(other_team)
        if other_out is not None:
            def market(pid: str) -> float:
                c = other_out.players.get(pid)
                return float(c.market.total) if c is not None else 0.0
            pid_remove = sorted(player_ids, key=market)[0]
        else:
            pid_remove = player_ids[-1]
        cand.deal.legs[other_team] = [a for a in other_assets if not (isinstance(a, PlayerAsset) and a.player_id == pid_remove)]
        cand.tags.append("repair:roster_remove_in")
        return True

    # 2) add outgoing from problem_team to reduce net incoming
    prob_out = catalog.outgoing_by_team.get(problem_team)
    if prob_out is None:
        return False

    if _count_players(cand.deal, problem_team) >= int(config.max_players_per_side):
        return False

    already = {a.player_id for a in cand.deal.legs.get(problem_team, []) if isinstance(a, PlayerAsset)}
    # aggregation_solo_only는 "묶음(2+ outgoing) 금지"이므로,
    # 현재 outgoing이 0명인 경우에만 solo-only를 허용한다.
    allow_solo_only = (len(already) == 0)

    # 기존 outgoing에 solo-only가 포함되어 있으면(=단독만 허용),
    # 여기서 outgoing을 추가하면 aggregation_ban으로 재실패할 가능성이 높으므로 수리하지 않는다.
    for pid0 in already:
        c0 = prob_out.players.get(pid0)
        if c0 is not None and bool(getattr(c0, "aggregation_solo_only", False)):
            return False

    receiver_team = other_team

    # 낮은 market을 우선으로 보내되, return-ban / solo-only 조건을 반영해서 후보를 고른다.
    best_pid: Optional[str] = None
    best_key: Optional[Tuple[float, float, str]] = None  # (market_total, salary_m, pid)
    for b in ("FILLER_CHEAP", "EXPIRING", "FILLER_BAD_CONTRACT"):
        for pid in prob_out.player_ids_by_bucket.get(b, tuple()):
            pid = str(pid)
            if pid in already:
                continue
            c = prob_out.players.get(pid)
            if c is None:
                continue
            if receiver_team and receiver_team in set(getattr(c, "return_ban_teams", None) or ()):
                continue
            if bool(getattr(c, "aggregation_solo_only", False)) and not allow_solo_only:
                continue
            mkt = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
            sal = float(getattr(c, "salary_m", 0.0) or 0.0)
            key = (mkt, sal, pid)
            if best_key is None or key < best_key:
                best_key = key
                best_pid = pid
    filler = best_pid
    if not filler:
        return False
    cand.deal.legs[problem_team].append(PlayerAsset(kind="player", player_id=filler))
    cand.tags.append("repair:roster_send_out")
    return True


def _repair_pick_rules(cand: DealCandidate, team_id: str, catalog: TradeAssetCatalog, config: DealGeneratorConfig, failure: RuleFailure) -> bool:
    """PickRulesRule 실패(stepien/pick_too_far 등) 수리."""

    if not team_id or team_id not in cand.deal.legs:
        return False

    reason = failure.reason or ""
    if reason == "pick_too_far" and failure.pick_id:
        pid = str(failure.pick_id)
        cand.deal.legs[team_id] = [a for a in cand.deal.legs[team_id] if not (isinstance(a, PickAsset) and a.pick_id == pid)]
        cand.tags.append("repair:pick_remove_far")
        return True

    out_cat = catalog.outgoing_by_team.get(team_id)
    if out_cat is None:
        return False

    picks_out = [a for a in cand.deal.legs[team_id] if isinstance(a, PickAsset)]
    if not picks_out:
        return False

    sensitive_set = set(out_cat.pick_ids_by_bucket.get("FIRST_SENSITIVE", tuple()))
    safe_set = set(out_cat.pick_ids_by_bucket.get("FIRST_SAFE", tuple()))

    pid_remove: Optional[str] = None
    for a in picks_out:
        if a.pick_id in sensitive_set:
            pid_remove = a.pick_id
            break
    if pid_remove is None:
        for a in picks_out:
            if a.pick_id in safe_set:
                pid_remove = a.pick_id
                break
    if pid_remove is None:
        pid_remove = picks_out[-1].pick_id

    cand.deal.legs[team_id] = [a for a in cand.deal.legs[team_id] if not (isinstance(a, PickAsset) and a.pick_id == pid_remove)]
    cand.tags.append("repair:stepien_remove_pick")

    # optional replacement: first -> second
    if pid_remove in safe_set or pid_remove in sensitive_set:
        if _count_picks(cand.deal, team_id) >= int(config.max_picks_per_side):
            return True
        replacement = _pick_best_pick_id(out_cat, bucket="SECOND", excluded=_current_pick_ids(cand.deal, team_id))
        if replacement:
            cand.deal.legs[team_id].append(out_cat.picks[replacement].as_asset())
            cand.tags.append("repair:stepien_replace_second")

    return True


