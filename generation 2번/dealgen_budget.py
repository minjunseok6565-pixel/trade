from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import hashlib
import json
import math
import random
from datetime import date

try:
    from schema import normalize_team_id  # type: ignore
except Exception:  # pragma: no cover
    def normalize_team_id(x: str, strict: bool = False) -> str:  # type: ignore
        return str(x or "").upper()

from ..errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    SWAP_NOT_FOUND,
    SWAP_INVALID,
    DUPLICATE_ASSET,
)

from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, TeamDealEvaluation, DealVerdict

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    IncomingPlayerRef,
    StepienHelper,
)

from .dealgen_types import DealGeneratorConfig, _Budgets
from .dealgen_utils import _clamp01

def _compute_budgets(cfg: DealGeneratorConfig, ts: Any, *, max_results: int) -> _Budgets:
    posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    urgency = float(getattr(ts, "urgency", 0.5) or 0.5)
    deadline_pressure = 0.0
    try:
        deadline_pressure = float(getattr(getattr(ts, "constraints", None), "deadline_pressure", 0.0) or 0.0)
    except Exception:
        deadline_pressure = 0.0

    base_targets = int(cfg.posture_targets.get(posture, 10))
    base_beam = int(cfg.posture_beam.get(posture, cfg.beam_width))

    # scale: urgency & deadline add a bit
    scale = 0.75 + 0.55 * _clamp01(urgency) + 0.35 * _clamp01(deadline_pressure)
    # stand pat stays tight
    if posture == "STAND_PAT":
        scale = min(scale, 0.9)

    max_targets = min(cfg.max_targets, max(0, int(round(base_targets * scale))))
    beam_width = max(2, min(cfg.beam_width, max(2, int(round(base_beam * (0.8 + 0.35 * _clamp01(urgency)))))))

    # budgets scale slightly with desired results
    res_scale = 0.8 + 0.06 * max(0, min(20, int(max_results)))

    return _Budgets(
        max_targets=max_targets,
        beam_width=beam_width,
        max_attempts_per_target=max(10, min(cfg.max_attempts_per_target, int(round(cfg.max_attempts_per_target * scale)))),
        max_validations=max(80, min(cfg.max_validations, int(round(cfg.max_validations * scale * res_scale)))),
        max_evaluations=max(60, min(cfg.max_evaluations, int(round(cfg.max_evaluations * scale * res_scale)))),
        max_repairs=max(0, min(cfg.max_repairs, 3)),
    )


