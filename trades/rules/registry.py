from __future__ import annotations

from typing import Iterable, Optional

from .base import Rule, TradeContext


class RuleRegistry:
    def __init__(self, rules: Optional[Iterable[Rule]] = None) -> None:
        self._rules: dict[str, Rule] = {}
        if rules:
            for rule in rules:
                self.register(rule)

    def register(self, rule: Rule) -> None:
        self._rules[rule.rule_id] = rule

    def unregister(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        rule = self._rules.get(rule_id)
        if rule is not None:
            rule.enabled = enabled

    def list_rules(self) -> list[Rule]:
        return list(self._rules.values())


def validate_all(deal, ctx: TradeContext, registry: Optional[RuleRegistry] = None) -> None:
    registry = registry or get_default_registry()
    enabled_rules = [rule for rule in registry.list_rules() if rule.enabled]
    for rule in sorted(enabled_rules, key=lambda rule: (rule.priority, rule.rule_id)):
        rule.validate(deal, ctx)


def get_default_registry() -> RuleRegistry:
    from .builtin import BUILTIN_RULES

    return RuleRegistry(BUILTIN_RULES)
