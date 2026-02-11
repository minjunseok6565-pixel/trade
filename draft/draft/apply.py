from __future__ import annotations

"""Apply a drafted rookie to DB (players/roster/contracts).

MVP behavior:
  - allocate a new player_id (P000001-style) by scanning existing players
  - insert/update players row
  - insert/update roster row (team_id = drafting_team)
  - create a rookie contract (simple scale placeholder) and upsert to contracts table
  - rebuild derived contract indices
  - append a transactions_log entry

The goal is to make the draft end-to-end playable. More realism (rookie scale,
options, cap holds, signing date rules, etc.) can be layered later.
"""

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol

from league_repo import LeagueRepo
from contracts.models import new_contract_id, make_contract_record

from .pool import Prospect
from .types import DraftTurn, TeamId, norm_team_id


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def allocate_new_player_id(repo: LeagueRepo) -> str:
    """Allocate next sequential player_id like P000001."""
    cur = repo._conn.cursor()
    row = cur.execute("SELECT player_id FROM players ORDER BY player_id DESC LIMIT 1;").fetchone()
    last = None
    if row and row[0]:
        last = str(row[0])
    n = 0
    if last and last.startswith("P") and last[1:].isdigit():
        try:
            n = int(last[1:])
        except Exception:
            n = 0
    return f"P{n + 1:06d}"


class RookieContractPolicy(Protocol):
    def build_salary_by_year(self, *, draft_year: int, overall_no: int, years: int = 4) -> Dict[int, int]:
        ...


class SimpleRookieScalePolicy:
    """Very simple rookie salary curve (placeholder).

    Produces annual salaries in dollars; intentionally rough.
    """

    def __init__(self, *, base_top1: int = 10_000_000, floor_late1: int = 1_000_000, floor_2nd: int = 800_000):
        self.base_top1 = int(base_top1)
        self.floor_late1 = int(floor_late1)
        self.floor_2nd = int(floor_2nd)

    def build_salary_by_year(self, *, draft_year: int, overall_no: int, years: int = 4) -> Dict[int, int]:
        dy = int(draft_year)
        ov = int(overall_no)
        yrs = max(1, int(years))

        if ov <= 30:
            # linear drop from #1 to #30
            t = (ov - 1) / 29.0 if ov > 1 else 0.0
            s0 = int(round(self.base_top1 * (1.0 - 0.85 * t)))
            s0 = max(s0, self.floor_late1)
        else:
            # second round low guarantees
            s0 = self.floor_2nd

        # small raises year-to-year (approx)
        out: Dict[int, int] = {}
        for i in range(yrs):
            out[dy + i] = int(round(s0 * (1.0 + 0.05 * i)))
        return out


@dataclass(frozen=True, slots=True)
class ApplyPickResult:
    player_id: str
    contract_id: str
    team_id: TeamId
    tx_entry: Dict[str, Any]


def apply_pick_to_db(
    *,
    db_path: str,
    turn: DraftTurn,
    prospect: Prospect,
    draft_year: int,
    contract_policy: Optional[RookieContractPolicy] = None,
    contract_years: int = 4,
    tx_date_iso: Optional[str] = None,
    source: str = "draft",
) -> ApplyPickResult:
    """Persist a drafted rookie to DB."""
    dbp = str(db_path)
    team_id = norm_team_id(turn.drafting_team)
    dy = int(draft_year)

    policy = contract_policy or SimpleRookieScalePolicy()
    salary_by_year = policy.build_salary_by_year(draft_year=dy, overall_no=int(turn.overall_no), years=int(contract_years))

    signed_date_iso = str(tx_date_iso or _dt.date.today().isoformat())
    contract_id = new_contract_id()

    with LeagueRepo(dbp) as repo:
        repo.init_db()
        player_id = allocate_new_player_id(repo)
        now = _utc_now_iso()

        # Upsert players/roster
        attrs = dict(prospect.attrs) if isinstance(prospect.attrs, dict) else {}
        attrs.setdefault("draft", {})
        attrs["draft"] = {
            "draft_year": dy,
            "overall_no": int(turn.overall_no),
            "round": int(turn.round),
            "slot": int(turn.slot),
            "pick_id": str(turn.pick_id),
            "original_team": str(turn.original_team),
            "drafting_team": str(team_id),
            "prospect_temp_id": str(prospect.temp_id),
        }

        cur = repo._conn.cursor()
        cur.execute(
            """
            INSERT INTO players(player_id, name, pos, age, height_in, weight_lb, ovr, attrs_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                name=excluded.name,
                pos=excluded.pos,
                age=excluded.age,
                height_in=excluded.height_in,
                weight_lb=excluded.weight_lb,
                ovr=excluded.ovr,
                attrs_json=excluded.attrs_json,
                updated_at=excluded.updated_at;
            """,
            (
                player_id,
                str(prospect.name),
                str(prospect.pos),
                int(prospect.age),
                int(prospect.height_in),
                int(prospect.weight_lb),
                int(prospect.ovr),
                _json_dumps(attrs),
                now,
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO roster(player_id, team_id, salary_amount, status, updated_at)
            VALUES (?, ?, ?, 'active', ?)
            ON CONFLICT(player_id) DO UPDATE SET
                team_id=excluded.team_id,
                salary_amount=excluded.salary_amount,
                status=excluded.status,
                updated_at=excluded.updated_at;
            """,
            (player_id, team_id, int(list(salary_by_year.values())[0]) if salary_by_year else 0, now),
        )
        repo._conn.commit()

        # Upsert contract
        contract = make_contract_record(
            contract_id=contract_id,
            player_id=player_id,
            team_id=team_id,
            signed_date_iso=signed_date_iso,
            start_season_year=dy,
            years=int(contract_years),
            salary_by_year={int(k): int(v) for k, v in salary_by_year.items()},
            options=[],
            status="ACTIVE",
        )
        repo.upsert_contract_records({contract_id: contract})
        repo.rebuild_contract_indices()

        tx_entry = {
            "type": "draft_pick_applied",
            "source": str(source),
            "date": signed_date_iso,
            "season_year": dy - 1,  # drafted after season dy-1
            "teams": [team_id],
            "draft_year": dy,
            "pick": {
                "overall_no": int(turn.overall_no),
                "round": int(turn.round),
                "slot": int(turn.slot),
                "pick_id": str(turn.pick_id),
                "original_team": str(turn.original_team),
                "drafting_team": str(team_id),
            },
            "player": {
                "player_id": player_id,
                "name": str(prospect.name),
                "pos": str(prospect.pos),
                "age": int(prospect.age),
                "ovr": int(prospect.ovr),
            },
            "contract": {
                "contract_id": contract_id,
                "start_season_year": dy,
                "years": int(contract_years),
                "salary_by_year": {str(k): int(v) for k, v in salary_by_year.items()},
            },
        }
        repo.insert_transactions([tx_entry])

    return ApplyPickResult(
        player_id=player_id,
        contract_id=contract_id,
        team_id=team_id,
        tx_entry=tx_entry,
    )
