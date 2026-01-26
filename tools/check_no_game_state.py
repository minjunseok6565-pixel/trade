#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    matches = []
    needle = "GAME" + "_STATE"
    for path in root.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if needle in line:
                matches.append((path, idx, line.rstrip("\n")))
    if matches:
        for path, idx, line in matches:
            print(f"{path}:{idx}:{line}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
