from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def _pattern() -> str:
    return r"\\b" + "GAME" + "_STATE" + r"\\b"


def _scan_files(root: Path) -> list[str]:
    pattern = re.compile(_pattern())
    matches: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if pattern.search(text):
            matches.append(str(path))
    return matches


def test_no_game_state_symbol_present():
    root = Path(__file__).resolve().parents[1]
    if os.name == "nt":
        matches = _scan_files(root)
        assert not matches, f"Found forbidden symbol in: {matches}"
        return

    pattern = _pattern()
    result = subprocess.run(
        ["grep", "-R", pattern, "."],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"Forbidden symbol found:\n{result.stdout}"
