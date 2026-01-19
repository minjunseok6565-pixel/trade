"""Lightweight draft settlement runner (pick protections + swaps only).

This script is a minimal, standalone entrypoint to settle a single draft year
using a pick order mapping. It does not run the full draft selection flow and
is intended for smoke tests and integration hooks.

Example usage:
  python draft_runner.py --draft-year 2026 --state-in state.json --pick-order pick_order_2026.json --events-out events.json
  python draft_runner.py --draft-year 2026 --state-in state.json --pick-order pick_order_2026.json --init-picks --years-ahead 0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from typing import Any, Dict, Iterable, List, Tuple


def load_game_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"State file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"State file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("State file must contain a JSON object at the top level.")
    return data


def load_pick_order(path: str) -> Dict[str, int]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"Pick order file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Pick order file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("Pick order must be a JSON object mapping pick_id -> slot.")

    errors: List[Tuple[Any, Any]] = []
    for pick_id, slot in data.items():
        if not isinstance(pick_id, str) or not pick_id.strip():
            errors.append((pick_id, slot))
            continue
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
            errors.append((pick_id, slot))
            continue

    if errors:
        preview = errors[:3]
        raise ValueError(f"Invalid pick order entries: {preview}")

    return {str(pick_id): int(slot) for pick_id, slot in data.items()}


def _ensure_state_containers(game_state: dict) -> None:
    game_state.setdefault("draft_picks", {})
    game_state.setdefault("swap_rights", {})
    game_state.setdefault("fixed_assets", {})


def _resolve_team_ids(game_state: dict) -> List[str]:
    teams = game_state.get("teams")
    if isinstance(teams, dict) and teams:
        return sorted([str(team_id) for team_id in teams.keys()])
    if game_state.get("draft_picks"):
        return []
    try:
        from config import ALL_TEAM_IDS
    except Exception as exc:
        raise ValueError(
            "Cannot init picks: team list unavailable; provide state with teams or install roster file."
        ) from exc
    return list(ALL_TEAM_IDS)


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)


def _safe_overwrite(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temp_path = handle.name
    os.replace(temp_path, path)


def _summarize_events(draft_year: int, events: Iterable[dict]) -> None:
    events_list = list(events)
    counts: Dict[str, int] = {}
    for event in events_list:
        event_type = event.get("type", "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    print(f"Settled draft year {draft_year}: {len(events_list)} events")
    for event_type, count in sorted(counts.items()):
        print(f"  {event_type}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run draft pick settlement.")
    parser.add_argument("--draft-year", type=int, required=True)
    parser.add_argument("--pick-order", required=True)
    parser.add_argument("--state-in")
    parser.add_argument("--state-out")
    parser.add_argument("--events-out")
    parser.add_argument("--print-events", action="store_true")
    parser.add_argument("--init-picks", action="store_true")
    parser.add_argument("--years-ahead", type=int, default=0)
    args = parser.parse_args()

    try:
        if args.state_in:
            game_state = load_game_state(args.state_in)
        else:
            from state import GAME_STATE

            game_state = GAME_STATE

        _ensure_state_containers(game_state)

        pick_order = load_pick_order(args.pick_order)
        draft_year = int(args.draft_year)

        if args.init_picks:
            team_ids = _resolve_team_ids(game_state)
            if not team_ids:
                raise ValueError(
                    "Cannot init picks: team list unavailable; provide state with teams or install roster file."
                )
            from trades.picks import init_draft_picks_if_needed

            init_draft_picks_if_needed(
                game_state, draft_year, team_ids, years_ahead=args.years_ahead
            )

        from trades.errors import TradeError
        from trades.pick_settlement import settle_draft_year

        events = settle_draft_year(game_state, draft_year, pick_order)
        _summarize_events(draft_year, events)

        if args.print_events:
            print(json.dumps(events, ensure_ascii=False, indent=2))

        if args.events_out:
            _write_json(args.events_out, events)

        if args.state_out:
            _write_json(args.state_out, game_state)
        elif args.state_in:
            _safe_overwrite(args.state_in, game_state)

        return 0
    except TradeError as exc:
        print(f"Settlement failed: {exc.code} {exc.message}")
        if exc.details is not None:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2))
        return 2
    except ValueError as exc:
        print(str(exc))
        return 2
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
