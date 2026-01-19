"""Roster DataFrame utilities for import/export tooling only.

NOT for runtime gameplay. Runtime code must use LeagueRepo/SQLite.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROSTER_PATH = os.path.join(BASE_DIR, "완성 로스터.xlsx")


def _parse_salary(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return 0.0
        return float(value)
    s = str(value).strip()
    if not s or s == "--":
        return 0.0
    for ch in ["$", ","]:
        s = s.replace(ch, "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def load_roster_df_from_excel(path: Optional[str] = None, sheet: Optional[str] = None) -> pd.DataFrame:
    """Load roster Excel for import/export tooling.

    Args:
        path: Optional path to Excel roster. Defaults to DEFAULT_ROSTER_PATH.
        sheet: Optional sheet name to read.
    """
    roster_path = path or DEFAULT_ROSTER_PATH
    if not os.path.exists(roster_path):
        raise RuntimeError(f"로스터 엑셀 파일을 찾을 수 없습니다: {roster_path}")
    df = pd.read_excel(roster_path, sheet_name=sheet)
    if "Salary" in df.columns:
        df["SalaryAmount"] = df["Salary"].apply(_parse_salary)
    else:
        df["SalaryAmount"] = 0.0
    return df
