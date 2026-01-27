from __future__ import annotations

import contextlib
from typing import Any, Dict, List

from .errors import PROTECTION_INVALID, SWAP_INVALID, TradeError


def _get_db_path_from_game_state(game_state: dict) -> str:
    if not isinstance(league, dict):
        raise ValueError("game_state['league'] must be a dict and contain 'db_path'")
    db_path = league.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required")
    return str(db_path)


@contextlib.contextmanager
def _open_service(db_path: str):
    from league_service import LeagueService
    svc_or_cm = LeagueService.open(db_path)
    if hasattr(svc_or_cm, "__enter__"):
        with svc_or_cm as svc:
            yield svc
        return
    svc = svc_or_cm
    try:
        yield svc
    finally:
        close = getattr(svc, "close", None)
        if callable(close):
            close()


def _validate_pick_order(pick_order: Dict[str, int], pick_id: str) -> int:
    if pick_id not in pick_order:
        raise TradeError(
            PROTECTION_INVALID,
            "Pick order missing for protected pick",
            {"pick_id": pick_id},
        )
    slot = pick_order.get(pick_id)
    if not isinstance(slot, int):
        raise TradeError(
            PROTECTION_INVALID,
            "Pick order slot must be an integer",
            {"pick_id": pick_id, "slot": slot},
        )
    return slot


def _validate_protection(pick_id: str, protection: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(protection, dict):
        raise TradeError(
            PROTECTION_INVALID,
            "Protection must be an object",
            {"pick_id": pick_id, "protection": protection},
        )
    protection_type = protection.get("type")
    if not isinstance(protection_type, str):
        raise TradeError(
            PROTECTION_INVALID,
            "Protection type is required",
            {"pick_id": pick_id, "protection": protection},
        )
    protection_type = protection_type.strip().upper()
    if protection_type != "TOP_N":
        raise TradeError(
            PROTECTION_INVALID,
            "Unsupported protection type",
            {"pick_id": pick_id, "protection": protection},
        )
    raw_n = protection.get("n")
    try:
        n_value = int(raw_n)
    except (TypeError, ValueError):
        raise TradeError(
            PROTECTION_INVALID,
            "Protection n must be an integer",
            {"pick_id": pick_id, "protection": protection},
        )
    if n_value < 1 or n_value > 30:
        raise TradeError(
            PROTECTION_INVALID,
            "Protection n out of range",
            {"pick_id": pick_id, "protection": protection},
        )
    compensation = protection.get("compensation")
    if not isinstance(compensation, dict):
        raise TradeError(
            PROTECTION_INVALID,
            "Protection compensation must be an object",
            {"pick_id": pick_id, "protection": protection},
        )
    value = compensation.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TradeError(
            PROTECTION_INVALID,
            "Protection compensation value must be numeric",
            {"pick_id": pick_id, "protection": protection},
        )
    label = compensation.get("label")
    if not isinstance(label, str) or not label.strip():
        label = "Protected pick compensation"

    return {"type": protection_type, "n": n_value, "compensation": {"label": label, "value": value}}


def settle_draft_year(
    game_state: dict, draft_year: int, pick_order: Dict[str, int]
) -> List[Dict[str, Any]]:
    db_path = _get_db_path_from_game_state(game_state)
    year_i = int(draft_year)
    pick_order_i: Dict[str, int] = {}
    for k, v in dict(pick_order).items():
        try:
            pick_order_i[str(k)] = int(v)
        except Exception:
            continue
    with _open_service(db_path) as svc:
        return svc.settle_draft_year(year_i, pick_order_i)


def settle_draft_year_in_memory(
    game_state: dict, draft_year: int, pick_order: Dict[str, int]
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    draft_picks = game_state.get("draft_picks", {})
    fixed_assets = game_state.setdefault("fixed_assets", {})
    swap_rights = game_state.setdefault("swap_rights", {})

    for pick_id, pick in draft_picks.items():
        if pick.get("year") != draft_year:
            continue
        protection = pick.get("protection")
        if protection is None:
            continue
        protection = _validate_protection(pick_id, protection)
        slot = _validate_pick_order(pick_order, pick_id)
        protected = slot <= protection["n"]
        owner_team = str(pick.get("owner_team", "")).upper()
        original_team = str(pick.get("original_team", "")).upper()
        compensation_asset_id = None

        if protected and owner_team and original_team and owner_team != original_team:
            pick["owner_team"] = original_team
            compensation_asset_id = f"FIXED_COMP_{pick_id}"
            if compensation_asset_id not in fixed_assets:
                fixed_assets[compensation_asset_id] = {
                    "asset_id": compensation_asset_id,
                    "label": protection["compensation"]["label"],
                    "value": protection["compensation"]["value"],
                    "owner_team": owner_team,
                    "source_pick_id": pick_id,
                    "draft_year": draft_year,
                }

        final_owner_team = str(pick.get("owner_team", "")).upper()
        events.append(
            {
                "type": "pick_protection_settled",
                "pick_id": pick_id,
                "draft_year": draft_year,
                "slot": slot,
                "protected": protected,
                "original_team": original_team,
                "owner_team": owner_team,
                "final_owner_team": final_owner_team,
                "compensation_asset_id": compensation_asset_id,
            }
        )
        pick["protection"] = None

    for swap_id, swap in swap_rights.items():
        if not swap.get("active", True):
            continue
        if swap.get("year") != draft_year:
            continue
        pick_id_a = swap.get("pick_id_a")
        pick_id_b = swap.get("pick_id_b")
        if not pick_id_a or not pick_id_b:
            raise TradeError(
                SWAP_INVALID,
                "Swap right missing pick ids",
                {"swap_id": swap_id},
            )
        pick_a = draft_picks.get(pick_id_a)
        pick_b = draft_picks.get(pick_id_b)
        if not pick_a or not pick_b:
            raise TradeError(
                SWAP_INVALID,
                "Swap picks must exist",
                {"swap_id": swap_id, "pick_id_a": pick_id_a, "pick_id_b": pick_id_b},
            )
        if pick_a.get("year") != draft_year or pick_b.get("year") != draft_year:
            raise TradeError(
                SWAP_INVALID,
                "Swap picks must match draft year",
                {
                    "swap_id": swap_id,
                    "pick_id_a": pick_id_a,
                    "pick_id_b": pick_id_b,
                    "draft_year": draft_year,
                },
            )
        if pick_a.get("round") != pick_b.get("round"):
            raise TradeError(
                SWAP_INVALID,
                "Swap picks must match round",
                {
                    "swap_id": swap_id,
                    "pick_id_a": pick_id_a,
                    "pick_id_b": pick_id_b,
                },
            )
        if pick_id_a not in pick_order or pick_id_b not in pick_order:
            raise TradeError(
                SWAP_INVALID,
                "Pick order missing for swap picks",
                {"swap_id": swap_id, "pick_id_a": pick_id_a, "pick_id_b": pick_id_b},
            )
        slot_a = pick_order[pick_id_a]
        slot_b = pick_order[pick_id_b]
        if not isinstance(slot_a, int) or not isinstance(slot_b, int):
            raise TradeError(
                SWAP_INVALID,
                "Swap pick order slots must be integers",
                {
                    "swap_id": swap_id,
                    "pick_id_a": pick_id_a,
                    "pick_id_b": pick_id_b,
                    "slot_a": slot_a,
                    "slot_b": slot_b,
                },
            )
        owner_team = str(swap.get("owner_team", "")).upper()
        if not owner_team:
            raise TradeError(
                SWAP_INVALID,
                "Swap right missing owner_team",
                {"swap_id": swap_id},
            )
        owner_a = str(pick_a.get("owner_team", "")).upper()
        owner_b = str(pick_b.get("owner_team", "")).upper()
        exercisable = owner_team == owner_a or owner_team == owner_b
        if not exercisable:
            swap["active"] = False
            events.append(
                {
                    "type": "swap_unexercisable",
                    "swap_id": swap_id,
                    "draft_year": draft_year,
                    "owner_team": owner_team,
                    "pick_id_a": pick_id_a,
                    "pick_id_b": pick_id_b,
                    "owner_a": owner_a,
                    "owner_b": owner_b,
                    "slot_a": slot_a,
                    "slot_b": slot_b,
                    "swap_executed": False,
                }
            )
            continue

        if slot_a == slot_b:
            swap["active"] = False
            events.append(
                {
                    "type": "swap_settled",
                    "swap_id": swap_id,
                    "draft_year": draft_year,
                    "pick_id_a": pick_id_a,
                    "pick_id_b": pick_id_b,
                    "slot_a": slot_a,
                    "slot_b": slot_b,
                    "chosen_pick_id": pick_id_a,
                    "owner_team": owner_team,
                    "swap_executed": False,
                }
            )
            continue

        if slot_a < slot_b:
            better_pick = pick_a
            worse_pick = pick_b
            chosen_pick_id = pick_id_a
        else:
            better_pick = pick_b
            worse_pick = pick_a
            chosen_pick_id = pick_id_b

        if owner_a == owner_b:
            other_owner = owner_a
        else:
            other_owner = owner_a if owner_a != owner_team else owner_b

        better_pick["owner_team"] = owner_team
        worse_pick["owner_team"] = other_owner
        swap["active"] = False

        owner_a_after = str(pick_a.get("owner_team", "")).upper()
        owner_b_after = str(pick_b.get("owner_team", "")).upper()
        events.append(
            {
                "type": "swap_settled",
                "swap_id": swap_id,
                "draft_year": draft_year,
                "pick_id_a": pick_id_a,
                "pick_id_b": pick_id_b,
                "slot_a": slot_a,
                "slot_b": slot_b,
                "chosen_pick_id": chosen_pick_id,
                "owner_team": owner_team,
                "other_owner_team": other_owner,
                "owner_a_before": owner_a,
                "owner_b_before": owner_b,
                "owner_a_after": owner_a_after,
                "owner_b_after": owner_b_after,
                "swap_executed": True,
            }
        )

    return events
