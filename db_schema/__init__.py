"""db_schema package.

This package contains SQLite DDL + migrations split out of league_repo.py.

Public API:
- apply_schema(...)

NOTE: Pure refactor (no functional changes).
"""

from .init import apply_schema  # noqa: F401

__all__ = ["apply_schema"]
