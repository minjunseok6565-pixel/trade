from __future__ import annotations

from dataclasses import dataclass

from ...errors import PROTECTION_INVALID, TradeError
from ...models import PickAsset
from ..base import TradeContext


@dataclass
class PickProtectionSchemaRule:
    rule_id: str = "pick_protection_schema"
    priority: int = 25
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        for assets in deal.legs.values():
            for asset in assets:
                if not isinstance(asset, PickAsset):
                    continue
                if asset.protection is None:
                    continue
                protection = asset.protection
                if not isinstance(protection, dict):
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                protection_type = protection.get("type")
                if not isinstance(protection_type, str):
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                protection_type = protection_type.strip().upper()
                if protection_type != "TOP_N":
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                n_value = protection.get("n")
                if not isinstance(n_value, int) or isinstance(n_value, bool):
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                if n_value < 1 or n_value > 30:
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                compensation = protection.get("compensation")
                if not isinstance(compensation, dict):
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                value = compensation.get("value")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TradeError(
                        PROTECTION_INVALID,
                        "Invalid pick protection payload",
                        {"pick_id": asset.pick_id, "protection": protection},
                    )
                label = compensation.get("label")
                if label is not None:
                    if not isinstance(label, str) or not label.strip():
                        raise TradeError(
                            PROTECTION_INVALID,
                            "Invalid pick protection payload",
                            {"pick_id": asset.pick_id, "protection": protection},
                        )
