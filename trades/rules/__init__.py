"""Trade rules engine.

How to add a new rule:
1) Create a new rule file in trades/rules/builtin (e.g., my_rule.py).
2) Implement a Rule with rule_id, priority, enabled, and validate().
3) Register the rule in trades/rules/builtin/__init__.py BUILTIN_RULES.

Developer checks:
- Run smoke test: python scripts/trade_smoke_test.py
- Force deadline to yesterday in state and confirm validate_deal fails.
"""

from .base import TradeContext, build_trade_context
from .tick_context import TradeRuleTickContext, build_trade_rule_tick_context
from .registry import RuleRegistry, get_default_registry, validate_all

__all__ = [
    "TradeContext",
    "build_trade_context",
    "TradeRuleTickContext",
    "build_trade_rule_tick_context",
    "RuleRegistry",
    "get_default_registry",
    "validate_all",
]
