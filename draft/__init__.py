"""Draft system package.

Modules:
  - types      : core domain dataclasses (TeamRecord, DraftOrderPlan, DraftTurn, ...)
  - standings  : compute regular-season records and ranking (pure)
  - lottery    : NBA-style top-4 lottery for bottom-14 teams (pure)
  - order      : round1/round2 original-order computation (pure)
  - finalize   : DB settlement + turns building (integrates LeagueService/LeagueRepo)
  - pool       : prospect pool representation (in-memory)
  - session    : draft session state machine (in-memory)
  - ai         : simple AI draft policy (MVP)
  - apply      : persist a drafted rookie into DB tables (players/roster/contracts)
  - engine     : orchestration helpers to run a draft end-to-end
"""

from __future__ import annotations

from .types import TeamRecord, DraftOrderPlan, DraftTurn, LotteryResult

__all__ = [
    "TeamRecord",
    "DraftOrderPlan",
    "DraftTurn",
    "LotteryResult",
]
