from __future__ import annotations

"""dealgen.scoring

Deal scoring mixin.
"""

from dataclasses import dataclass, field
from datetime import date
from math import exp
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import hashlib
import json
import random
import re

from ...errors import (
    DEAL_INVALIDATED,
    TRADE_DEADLINE_PASSED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    DUPLICATE_ASSET,
    TradeError,
)
from ...models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import (
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    TradeAssetCatalog,
    build_trade_asset_catalog,
)

from .utils import (
    _deal_num_assets,
    _deal_num_players_moved,
    _sigmoid,
)


class _ScoringMixin:
    def _score_deal(
        self,
        deal: Deal,
        *,
        buyer_id: str,
        seller_id: str,
        buyer_decision: DealDecision,
        seller_decision: DealDecision,
        buyer_eval: TeamDealEvaluation,
        seller_eval: TeamDealEvaluation,
        budgets: Mapping[str, int],
        opponent_seen: Mapping[str, int],
        target_seen: Mapping[str, int],
    ) -> float:
        scale = float(self.cfg.sigmoid_scale)
        mb = float(buyer_eval.net_surplus) - float(buyer_decision.required_surplus)
        ms = float(seller_eval.net_surplus) - float(seller_decision.required_surplus)

        # verdict handling
        vb = buyer_decision.verdict.value if hasattr(buyer_decision.verdict, "value") else str(buyer_decision.verdict)
        vs = seller_decision.verdict.value if hasattr(seller_decision.verdict, "value") else str(seller_decision.verdict)
        verdict_bonus = 0.0
        if vb == "ACCEPT":
            verdict_bonus += 0.12
        elif vb == "COUNTER":
            verdict_bonus += 0.05
        else:
            verdict_bonus -= 0.10
        if vs == "ACCEPT":
            verdict_bonus += 0.12
        elif vs == "COUNTER":
            verdict_bonus += 0.05
        else:
            verdict_bonus -= 0.10

        accept_score = _sigmoid(mb / scale) + _sigmoid(ms / scale)
        # punish strongly if buyer is far negative (avoid silly overpays)
        overpay_penalty = max(0.0, -mb) / max(4.0, scale)

        num_assets = _deal_num_assets(deal)
        num_players = _deal_num_players_moved(deal)
        complexity_penalty = self.cfg.complexity_asset_penalty * max(0, num_assets - 2) + self.cfg.complexity_player_penalty * max(0, num_players - 2)

        score = float(accept_score) + float(verdict_bonus) - float(complexity_penalty) - float(overpay_penalty)

        # If either side is hard reject, apply floor penalty.
        if vb == "REJECT" or vs == "REJECT":
            score -= float(self.cfg.reject_floor_penalty)

        return float(score)
