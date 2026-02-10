from __future__ import annotations

import hashlib

from .types import DealGeneratorConfig
from ..generation_tick import TradeGenerationTickContext

def _compute_seed(cfg: DealGeneratorConfig, tick_ctx: TradeGenerationTickContext, team_id: str) -> int:
    """결정적 RNG seed (python hash() 금지)."""

    raw = f"{cfg.deterministic_seed_salt}|{tick_ctx.current_date.isoformat()}|{team_id}"
    h = hashlib.sha256(raw.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=False)


def _compute_sweetener_seed(
    cfg: DealGeneratorConfig,
    tick_ctx: TradeGenerationTickContext,
    *,
    initiator_team_id: str,
    counterparty_team_id: str,
    base_hash: str,
    skeleton_hash: str,
    trial_index: int,
) -> int:
    """결정적 sweetener RNG seed.

    목표
    - 같은 base deal(h_valid)이라도 skeleton/시도 순서에 따라
      다른 sweetener 조합을 시도할 수 있게 하되,
      탐색 순서/전역 RNG 상태에 과도하게 의존하지 않게 한다.
    """

    raw = (
        f"{cfg.deterministic_seed_salt}|sweetener|{tick_ctx.current_date.isoformat()}"
        f"|{str(initiator_team_id).upper()}|{str(counterparty_team_id).upper()}"
        f"|{str(base_hash)}|{str(skeleton_hash)}|{int(trial_index)}"
    )
    h = hashlib.sha256(raw.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=False)


