from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from league_repo import LeagueRepo
from state import GAME_STATE
from trades.models import canonicalize_deal, parse_deal
from trades.validator import validate_deal


def _seed_minimal_roster(repo: LeagueRepo) -> None:
    now = "2025-01-01T00:00:00Z"
    players = [
        ("P000001", "Player One", "G", 25, 75, 180, 75, "{}", now, now),
        ("P000002", "Player Two", "F", 26, 80, 210, 76, "{}", now, now),
    ]
    roster = [
        ("P000001", "ATL", 1_000_000, "active", now),
        ("P000002", "BOS", 1_000_000, "active", now),
    ]
    with repo.transaction() as cur:
        cur.executemany(
            """
            INSERT INTO players(
                player_id, name, pos, age, height_in, weight_lb, ovr, attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            players,
        )
        cur.executemany(
            """
            INSERT INTO roster(player_id, team_id, salary_amount, status, updated_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            roster,
        )


def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        with LeagueRepo(db_path) as repo:
            repo.init_db()
            _seed_minimal_roster(repo)
            repo.validate_integrity()

        league = GAME_STATE.setdefault("league", {})
        league["db_path"] = db_path
        league["draft_year"] = date.today().year
        league["season_year"] = date.today().year
        trade_rules = league.setdefault("trade_rules", {})
        trade_rules["stepien_lookahead"] = "0"
        trade_payload = {
            "teams": ["ATL", "BOS"],
            "legs": {
                "ATL": [{"kind": "player", "player_id": "P000001"}],
                "BOS": [{"kind": "player", "player_id": "P000002"}],
            },
        }
        deal = canonicalize_deal(parse_deal(trade_payload))
        validate_deal(deal, current_date=date.today(), db_path=db_path)
    except Exception as exc:
        print(f"Smoke test failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass

    print("OK: smoke_test")


if __name__ == "__main__":
    main()
