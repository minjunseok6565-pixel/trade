from __future__ import annotations

from typing import Any, Dict, List


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_dict(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"GameResultV2 invalid: '{path}' must be an object/dict")
    return value


def _require_list(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(f"GameResultV2 invalid: '{path}' must be an array/list")
    return value


def _merge_counter_dict_sum(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """카운터/분해지표를 key별로 합산한다. (중첩 dict는 재귀 합산)"""
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if isinstance(v, dict):
            child = dst.get(k)
            if not isinstance(child, dict):
                child = {}
                dst[k] = child
            _merge_counter_dict_sum(child, v)
        elif _is_number(v):
            try:
                dst[k] = float(dst.get(k, 0.0)) + float(v)
            except (TypeError, ValueError):
                continue
