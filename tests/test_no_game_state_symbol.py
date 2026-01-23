from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def _pattern() -> str:
    return r"\b" + "GAME" + "_" + "STATE" + r"\b"


def test_no_state_symbol_in_repo() -> None:
    if os.name != "nt":
        result = subprocess.run(
            [
                "grep",
                "-R",
                "--binary-files=without-match",
                "--exclude-dir=.git",
                "--exclude=*.pyc",
                _pattern(),
                ".",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, result.stdout
        return

    pattern = re.compile(_pattern())
    root = Path(".")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if path.name.startswith(".") or path.suffix == ".pyc":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        assert not pattern.search(content), f"Found state symbol in {path}"
