from __future__ import annotations

from typing import Any

from .types import DealGeneratorConfig, DealGeneratorBudget

# =============================================================================
# Budget scaling
# =============================================================================


def _scale_budget(cfg: DealGeneratorConfig, team_situation: Any) -> DealGeneratorBudget:
    posture = str(getattr(team_situation, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    urgency = float(getattr(team_situation, "urgency", 0.0) or 0.0)
    deadline = 0.0
    try:
        deadline = float(getattr(getattr(team_situation, "constraints", None), "deadline_pressure", 0.0) or 0.0)
    except Exception:
        deadline = 0.0

    posture_scale = {
        "AGGRESSIVE_BUY": 1.25,
        "SOFT_BUY": 1.00,
        "SELL": 1.05,
        "SOFT_SELL": 0.95,
        "STAND_PAT": 0.55,
    }.get(posture, 0.75)

    # urgency/deadline (0..1) -> intensity (0.85..1.35)
    u = max(0.0, min(1.0, urgency))
    d = max(0.0, min(1.0, deadline))
    intensity = 0.85 + 0.35 * u + 0.25 * d
    scale = posture_scale * intensity

    def _cap(val: int, hard: int) -> int:
        return max(1, min(int(val), int(hard)))

    return DealGeneratorBudget(
        max_targets=_cap(int(cfg.base_max_targets * scale), cfg.max_targets_hard),
        beam_width=_cap(int(cfg.base_beam_width * scale), 24),
        max_attempts_per_target=_cap(int(cfg.base_max_attempts_per_target * scale), cfg.max_attempts_per_target_hard),
        max_validations=_cap(int(cfg.base_max_validations * scale), cfg.max_validations_hard),
        max_evaluations=_cap(int(cfg.base_max_evaluations * scale), cfg.max_evaluations_hard),
        max_repairs=_cap(int(cfg.base_max_repairs), 3),
    )


