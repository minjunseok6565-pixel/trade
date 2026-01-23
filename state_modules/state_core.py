from __future__ import annotations


from schema import season_id_from_year as _schema_season_id_from_year


def season_id_from_year(season_year: int) -> str:
    """시즌 시작 연도(int) -> season_id 문자열로 변환. 예: 2025 -> '2025-26'"""
    return str(_schema_season_id_from_year(int(season_year)))
