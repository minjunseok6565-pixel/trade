from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass, fields
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    Literal,
    runtime_checkable,
)

# NOTE:
# - valuation layer는 trades.models의 Asset/Deal을 "입력 형태"로만 소비한다.
# - valuation layer는 validate_deal / rules를 호출하지 않는다.
from ..models import (
    Asset,
    Deal,
    PlayerAsset,
    PickAsset,
    SwapAsset,
    FixedAsset,
    asset_key,
)

# -----------------------------------------------------------------------------
# 0) JSON-friendly helpers (서버 응답 / 디버그 출력용)
# -----------------------------------------------------------------------------
JsonPrimitive = Union[str, int, float, bool, None]
JsonValue = Union[JsonPrimitive, List["JsonValue"], Dict[str, "JsonValue"]]


def to_jsonable(obj: Any) -> JsonValue:
    """
    dataclass / Enum / Mapping / Sequence 를 JSON 직렬화 가능한 형태로 변환.
    - server.py에서 breakdown을 그대로 반환할 때 유용.
    - types 모듈에 둬서 모든 valuation 모듈이 동일 규칙을 사용하게 함(SSOT).
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        # dataclass(slots 포함) -> dict 로 재귀 변환
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    # fallback
    return str(obj)


# -----------------------------------------------------------------------------
# 1) Core enums / aliases
# -----------------------------------------------------------------------------
TeamId = str
PlayerId = str
PickId = str
SwapId = str
FixedAssetId = str

ValueUnit = float  # "Trade Value Unit" 같은 내부 공통 화폐 단위로 사용 (float)


class AssetKind(str, Enum):
    PLAYER = "player"
    PICK = "pick"
    SWAP = "swap"
    FIXED = "fixed"


def kind_of_asset(asset: Asset) -> AssetKind:
    if isinstance(asset, PlayerAsset):
        return AssetKind.PLAYER
    if isinstance(asset, PickAsset):
        return AssetKind.PICK
    if isinstance(asset, SwapAsset):
        return AssetKind.SWAP
    return AssetKind.FIXED


class ValuationStage(str, Enum):
    """
    valuation 단계 구분(로그/설명용).
    - MARKET: 팀 무관 가격화
    - TEAM: DecisionContext 기반 팀 효용화(니즈/성향/리스크/재정)
    - PACKAGE: 딜 패키지 상호작용 보정
    """
    MARKET = "market"
    TEAM = "team"
    PACKAGE = "package"


class StepMode(str, Enum):
    ADD = "add"  # delta 적용
    MUL = "mul"  # factor 적용(주로 scale/multiplier)


class DealVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    COUNTER = "COUNTER"


# -----------------------------------------------------------------------------
# 2) Value math (now/future split)
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ValueComponents:
    """
    모든 valuation에서 공통으로 쓰는 값 표현.
    - now: 즉시 전력/현재 가치
    - future: 장기 가치(픽, 유망, 계약 효율 등)
    """
    now: ValueUnit = 0.0
    future: ValueUnit = 0.0

    @property
    def total(self) -> ValueUnit:
        return float(self.now) + float(self.future)

    def __add__(self, other: "ValueComponents") -> "ValueComponents":
        return ValueComponents(self.now + other.now, self.future + other.future)

    def __sub__(self, other: "ValueComponents") -> "ValueComponents":
        return ValueComponents(self.now - other.now, self.future - other.future)

    def scale(self, factor: float) -> "ValueComponents":
        f = float(factor)
        return ValueComponents(self.now * f, self.future * f)

    @staticmethod
    def zero() -> "ValueComponents":
        return ValueComponents(0.0, 0.0)


@dataclass(frozen=True, slots=True)
class ValuationStep:
    """
    설명가능성(Explainability)을 위한 공통 로그 단위.
    - add: delta(+, -)
    - mul: factor(예: pick_multiplier, youth_multiplier, fit_scale 등)
    """
    stage: ValuationStage
    mode: StepMode
    code: str  # SSOT: "OVR_BASE", "AGE_CURVE", "CONTRACT_EFF", "NEED_FIT", ...
    label: str  # 사람이 읽는 설명
    delta: ValueComponents = field(default_factory=ValueComponents.zero)
    factor: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# 3) Asset snapshots (DB/Repo에서 읽은 값을 "평가 가능한 형태"로 표준화)
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ContractOptionSnapshot:
    season_year: int
    type: str  # normalize_option_type 결과
    status: str  # PENDING/EXERCISED/DECLINED
    decision_date: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    contract_id: str
    player_id: PlayerId
    team_id: Optional[TeamId]
    status: str  # ACTIVE 등
    signed_date: Optional[str]
    start_season_year: int
    years: int
    salary_by_year: Dict[int, float] = field(default_factory=dict)
    options: List[ContractOptionSnapshot] = field(default_factory=list)

    # NOTE: derived는 계산 모듈에서 만들고 meta로 넣는 것을 권장(타입은 열어둠)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    kind: Literal["player"]
    player_id: PlayerId
    name: Optional[str] = None
    pos: Optional[str] = None  # 예: "PG/SG"
    age: Optional[float] = None
    ovr: Optional[float] = None
    team_id: Optional[TeamId] = None  # 현재 로스터 소속
    salary_amount: Optional[float] = None  # roster.salary_amount
    attrs: Dict[str, Any] = field(default_factory=dict)  # players.attrs_json
    contract: Optional[ContractSnapshot] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PickSnapshot:
    kind: Literal["pick"]
    pick_id: PickId
    year: int
    round: int
    original_team: TeamId
    owner_team: TeamId
    protection: Optional[Dict[str, Any]] = None  # DB draft_picks.protection_json
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SwapSnapshot:
    kind: Literal["swap"]
    swap_id: SwapId
    pick_id_a: PickId
    pick_id_b: PickId
    year: Optional[int]
    round: Optional[int]
    owner_team: TeamId
    active: bool = True
    created_by_deal_id: Optional[str] = None
    created_at: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FixedAssetSnapshot:
    kind: Literal["fixed"]
    asset_id: FixedAssetId
    label: Optional[str]
    value: Optional[float]
    owner_team: TeamId
    source_pick_id: Optional[PickId] = None
    draft_year: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


AssetSnapshot = Union[PlayerSnapshot, PickSnapshot, SwapSnapshot, FixedAssetSnapshot]


# -----------------------------------------------------------------------------
# 4) Pick expectation (픽 예상 순번/분포; market_pricing에 주입)
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PickExpectation:
    """
    pick 시장가 산정에 필요한 "예상 순번" 또는 "예상 가치"를 담는 구조.
    - build_valuation_data_context 단계에서 standings 기반으로 만들어 넣는 게 이상적.
    """
    pick_id: PickId
    expected_pick_number: Optional[float] = None  # 1..30 (None이면 fallback curve)
    expected_percentile: Optional[float] = None  # 0..1 (선택)
    confidence: float = 0.5
    meta: Dict[str, Any] = field(default_factory=dict)


PickExpectationMap = Dict[PickId, PickExpectation]


# -----------------------------------------------------------------------------
# 5) Market valuation / Team valuation / Deal valuation (엔진 전 단계 공통 포맷)
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MarketValuation:
    """
    팀 무관 시장가 결과.
    - market_pricing.py의 표준 출력.
    """
    asset_key: str  # trades.models.asset_key(asset) 기반
    kind: AssetKind
    ref_id: str  # player_id / pick_id / swap_id / asset_id
    value: ValueComponents
    steps: Tuple[ValuationStep, ...] = tuple()
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FitAssessment:
    """
    팀 니즈(need_map)에 대한 "매칭 결과"만 표현한다.
    - 니즈 생성/재평가는 team_situation 영역이므로 여기서는 금지.
    """
    fit_score: float  # 0..1
    threshold: float  # knobs.min_fit_threshold
    passed: bool
    matched_needs: Dict[str, float] = field(default_factory=dict)  # need_tag -> match amount
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeamValuation:
    """
    DecisionContext 기반 팀 효용화 결과.
    - team_utility.py의 표준 출력.
    """
    asset_key: str
    kind: AssetKind
    ref_id: str
    market_value: ValueComponents
    team_value: ValueComponents
    market_steps: Tuple[ValuationStep, ...] = tuple()
    team_steps: Tuple[ValuationStep, ...] = tuple()
    fit: Optional[FitAssessment] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SideTotals:
    """
    incoming/outgoing 합산 결과.
    - ValueComponents로 유지해 now/future split을 끝까지 보존한다.
    """
    value: ValueComponents
    count: int


@dataclass(frozen=True, slots=True)
class TeamSideValuation:
    """
    특정 팀 관점에서 딜을 평가한 결과(딜 한 건의 한 팀 관점).
    - deal_evaluator.py의 표준 출력.
    """
    team_id: TeamId
    incoming: Tuple[TeamValuation, ...]
    outgoing: Tuple[TeamValuation, ...]
    incoming_totals: SideTotals
    outgoing_totals: SideTotals
    package_steps: Tuple[ValuationStep, ...] = tuple()
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeamDealEvaluation:
    """
    TeamSideValuation의 totals를 기반으로 한 최종 수치(결정 모듈 입력).
    - decision_policy.py는 이 타입만 받는 걸 권장(결합도↓).
    """
    team_id: TeamId
    incoming_total: ValueUnit
    outgoing_total: ValueUnit
    net_surplus: ValueUnit
    surplus_ratio: float  # (incoming - outgoing) / max(outgoing, eps)
    side: TeamSideValuation
    meta: Dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# 6) Decision output (accept/reject/counter) + explainability
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionReason:
    code: str
    message: str
    impact: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CounterProposal:
    """
    counter_builder.py가 만들어낼 수 있는 구조를 미리 타입으로 잡아둔다.
    - 아직 미구현이면 None으로 두고 지나가면 됨.
    """
    deal: Optional[Deal] = None  # 내부적으로는 Deal, 외부 응답은 serialize_deal로 변환 권장
    reasons: Tuple[DecisionReason, ...] = tuple()
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DealDecision:
    verdict: DealVerdict
    required_surplus: float  # 절대값 기준(또는 outgoing*ratio를 계산한 값)으로 기록 권장
    overpay_allowed: float
    confidence: float = 0.5
    reasons: Tuple[DecisionReason, ...] = tuple()
    counter: Optional[CounterProposal] = None
    meta: Dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# 7) Data provider protocol (DB/Repo 결합을 types에서 끊기 위한 인터페이스)
# -----------------------------------------------------------------------------
@runtime_checkable
class ValuationDataProvider(Protocol):
    """
    data_context.py가 구현/제공할 인터페이스(SSOT).
    - valuation 엔진은 이 Protocol만 의존(Repo/DB 의존성 차단).
    - get_*의 반환은 "snapshot dict" 또는 이미 표준화된 Snapshot 둘 다 가능.
      (추천: data_context에서 Snapshot으로 만들어서 제공)
    """

    def get_player_snapshot(self, player_id: PlayerId) -> PlayerSnapshot: ...
    def get_pick_snapshot(self, pick_id: PickId) -> PickSnapshot: ...
    def get_swap_snapshot(self, swap_id: SwapId) -> SwapSnapshot: ...
    def get_fixed_asset_snapshot(self, asset_id: FixedAssetId) -> FixedAssetSnapshot: ...

    def get_pick_expectation(self, pick_id: PickId) -> Optional[PickExpectation]: ...

    # 시뮬레이션 날짜/시즌 정보(계약 잔여/픽 할인 등에 사용)
    @property
    def current_season_year(self) -> int: ...
    @property
    def current_date_iso(self) -> str: ...


# -----------------------------------------------------------------------------
# 8) Small convenience: build a stable key/ref for snapshots
# -----------------------------------------------------------------------------
def snapshot_ref_id(snap: AssetSnapshot) -> str:
    if isinstance(snap, PlayerSnapshot):
        return snap.player_id
    if isinstance(snap, PickSnapshot):
        return snap.pick_id
    if isinstance(snap, SwapSnapshot):
        return snap.swap_id
    return snap.asset_id


def snapshot_kind(snap: AssetSnapshot) -> AssetKind:
    if isinstance(snap, PlayerSnapshot):
        return AssetKind.PLAYER
    if isinstance(snap, PickSnapshot):
        return AssetKind.PICK
    if isinstance(snap, SwapSnapshot):
        return AssetKind.SWAP
    return AssetKind.FIXED


def stable_asset_key_from_models(asset: Asset) -> str:
    """
    Deal의 Asset(dataclass)에서 valuation이 쓸 stable key 생성.
    - 내부적으로 trades.models.asset_key와 동일 규칙.
    - valuation layer에서 이 함수를 SSOT로 사용하면 로그/캐시 키가 일관됨.
    """
    return asset_key(asset)

