#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if {".git", "venv", "__pycache__"} & parts:
            continue
        files.append(path)
    return files


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    token = "GAME" + "_STATE"
    forbidden_module = "state_modules.state_store"
    forbidden_symbol = "get_state_ref"
    violations: list[str] = []

    for path in _iter_python_files(root):
        rel_path = path.relative_to(root)
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if token in line:
                    violations.append(f"{rel_path}:{line_no}: contains {token}")
                if (
                    "from " in line
                    and forbidden_module in line
                    and forbidden_symbol in line
                    and line.lstrip().startswith("from ")
                ):
                    if rel_path != Path("state.py") and "state_modules" not in rel_path.parts:
                        violations.append(
                            f"{rel_path}:{line_no}: forbidden import of {forbidden_symbol}"
                        )

    if violations:
        for entry in violations:
            print(entry)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
