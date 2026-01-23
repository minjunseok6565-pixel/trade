#!/usr/bin/env python3
import argparse
import inspect
import json
from typing import Any, Dict, List


def load_baseline(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_symbol_signature(symbol: Any) -> str:
    return str(inspect.signature(symbol))


def build_workflow_state_schema(state_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    keys = list(state_snapshot.keys())
    types = {key: type(state_snapshot[key]).__name__ for key in keys}
    nested_keys = {}
    for key in ["league", "cached_views", "trade_market", "trade_memory", "postseason"]:
        value = state_snapshot.get(key)
        if isinstance(value, dict):
            nested_keys[key] = list(value.keys())
        else:
            nested_keys[key] = None
    return {"keys": keys, "types": types, "nested_keys": nested_keys}


def compare_symbols(state_module: Any, baseline: Dict[str, Any]) -> List[str]:
    errors = []
    for name, info in baseline["symbols"].items():
        if not hasattr(state_module, name):
            errors.append(f"Missing symbol: {name}")
            continue
        if info["kind"] == "function":
            current_sig = get_symbol_signature(getattr(state_module, name))
            if current_sig != info["signature"]:
                errors.append(
                    f"Signature mismatch for {name}: expected {info['signature']}, got {current_sig}"
                )
    return errors


def compare_workflow_state_schema(state_module: Any, baseline: Dict[str, Any]) -> List[str]:
    errors = []
    current = build_workflow_state_schema(state_module.export_workflow_state())
    expected = baseline["workflow_state_schema"]

    if current["keys"] != expected["keys"]:
        errors.append(
            "State keys/order mismatch: "
            f"expected {expected['keys']}, got {current['keys']}"
        )
    if current["types"] != expected["types"]:
        errors.append(
            "State types mismatch: "
            f"expected {expected['types']}, got {current['types']}"
        )
    if current["nested_keys"] != expected["nested_keys"]:
        errors.append(
            "State nested keys mismatch: "
            f"expected {expected['nested_keys']}, got {current['nested_keys']}"
        )
    return errors


def compare_imports(state_module: Any, baseline: Dict[str, Any]) -> List[str]:
    errors = []
    for entry in baseline.get("imports", []):
        file_path = entry.get("file")
        line = entry.get("line")
        importable = entry.get("importable", {})
        for symbol in entry.get("symbols", []):
            if symbol == "*":
                continue
            if importable and importable.get(symbol) is False:
                continue
            if not hasattr(state_module, symbol):
                errors.append(
                    f"Missing importable symbol '{symbol}' referenced in {file_path}:{line}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check state refactor against baseline.")
    parser.add_argument(
        "--baseline",
        default="state_refactor_baseline.json",
        help="Path to baseline JSON.",
    )
    args = parser.parse_args()

    baseline = load_baseline(args.baseline)
    try:
        import state
    except Exception as exc:
        print(f"Failed to import state module: {exc}")
        return 1

    errors: List[str] = []
    errors.extend(compare_symbols(state, baseline))
    errors.extend(compare_workflow_state_schema(state, baseline))
    errors.extend(compare_imports(state, baseline))

    if errors:
        print("State refactor check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("State refactor check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
