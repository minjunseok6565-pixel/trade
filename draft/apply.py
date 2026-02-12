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
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol

from league_repo import LeagueRepo
from contracts.models import new_contract_id, make_contract_record
from college.service import allocate_player_ids as allocate_player_ids_shared

from .pool import Prospect
from .types import DraftTurn, TeamId, norm_team_id


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


_PLAYER_ID_RE = re.compile(r"^P\d{6}$")


def _looks_like_player_id(x: Any) -> bool:
    s = str(x or "")
    return bool(_PLAYER_ID_RE.match(s))


def _is_college_player_id(repo: LeagueRepo, player_id: str) -> bool:
    row = repo._conn.execute(
        "SELECT 1 FROM college_players WHERE player_id=? LIMIT 1;",
        (str(player_id),),
    ).fetchone()
    return bool(row)


def allocate_new_player_id(repo: LeagueRepo, *, cur=None) -> str:
    """Allocate next sequential player_id like P000001 (collision-free across NBA+college)."""
    ids = allocate_player_ids_shared(repo, count=1, cur=cur)
    return ids[0] if ids else "P000001"


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
    promoted_from_college: bool


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

    with LeagueRepo(dbp) as repo:
        repo.init_db()
        now = _utc_now_iso()
        pick_id = str(turn.pick_id)

        # Idempotency guard: if this pick_id was already applied, return the recorded result.
        # This prevents "restart from scratch" from getting stuck after partial progress.
        row_applied = repo._conn.execute(
            """
            SELECT pick_id, drafting_team, prospect_temp_id, player_id, contract_id, meta_json
            FROM draft_results
            WHERE pick_id=? LIMIT 1;
            """,
            (pick_id,),
        ).fetchone()
        if row_applied:
            if str(row_applied["drafting_team"]) != str(team_id) or str(row_applied["prospect_temp_id"]) != str(prospect.temp_id):
                raise RuntimeError(
                    "draft_results already contains this pick_id but does not match current inputs: "
                    f"pick_id={pick_id} drafting_team(db={row_applied['drafting_team']!r}, cur={team_id!r}) "
                    f"prospect_temp_id(db={row_applied['prospect_temp_id']!r}, cur={prospect.temp_id!r})"
                )
            meta0: Dict[str, Any] = {}
            try:
                mj = row_applied["meta_json"]
                if mj:
                    meta0 = json.loads(str(mj))
            except Exception:
                meta0 = {}

            # Keep tx_entry shape stable; mark as already applied.
            tx_entry = {
                "type": "draft_pick_applied",
                "source": str(source),
                "date": signed_date_iso,
                "season_year": dy - 1,
                "teams": [team_id],
                "draft_year": dy,
                "prospect_temp_id": str(prospect.temp_id),
                "prospect_source": str(meta0.get("prospect_source") or "unknown"),
                "college_promoted": bool(meta0.get("college_promoted") or False),
                "already_applied": True,
                "pick": {
                    "overall_no": int(turn.overall_no),
                    "round": int(turn.round),
                    "slot": int(turn.slot),
                    "pick_id": pick_id,
                    "original_team": str(turn.original_team),
                    "drafting_team": str(team_id),
                },
                "player": {
                    "player_id": str(row_applied["player_id"]),
                    "name": str(prospect.name),
                    "pos": str(prospect.pos),
                    "age": int(prospect.age),
                    "ovr": int(prospect.ovr),
                },
                "contract": {
                    "contract_id": str(row_applied["contract_id"]),
                },
            }
            return ApplyPickResult(
                player_id=str(row_applied["player_id"]),
                contract_id=str(row_applied["contract_id"]),
                team_id=team_id,
                tx_entry=tx_entry,
                promoted_from_college=bool(meta0.get("college_promoted") or False),
            )

        contract_id = new_contract_id()

        # Determine whether we are promoting an existing college player_id.
        temp_id = str(prospect.temp_id)
        promoted_from_college = False
        if _looks_like_player_id(temp_id):
            # Treat P000123-style temp_id as a real player_id intended for promotion.
            if not _is_college_player_id(repo, temp_id):
                raise ValueError(
                    f"prospect.temp_id looks like a player_id but no college_players row found: {temp_id}"
                )
            row = repo._conn.execute(
                "SELECT 1 FROM players WHERE player_id=? LIMIT 1;",
                (temp_id,),
            ).fetchone()
            if row:
                raise ValueError(f"cannot promote college player_id already exists in players: {temp_id}")
            player_id = temp_id
            promoted_from_college = True
        else:
            # Allocate within the same pick transaction (cursor passed below).
            player_id = ""

        # Upsert players/roster
        attrs = dict(prospect.attrs) if isinstance(prospect.attrs, dict) else {}
        attrs.setdefault("draft", {})
        prospect_source = "college" if promoted_from_college else "generated_pool"
        attrs["draft"] = {
            "draft_year": dy,
            "overall_no": int(turn.overall_no),
            "round": int(turn.round),
            "slot": int(turn.slot),
            "pick_id": str(turn.pick_id),
            "original_team": str(turn.original_team),
            "drafting_team": str(team_id),
            "prospect_temp_id": str(prospect.temp_id),
            "prospect_source": prospect_source,
            "player_id_source": "college" if promoted_from_college else "seq_player_id",
            "college_promoted": bool(promoted_from_college),
            **({"college_player_id": str(player_id)} if promoted_from_college else {}),
        }

        # Pick = one atomic DB transaction (players/roster/contracts/indices/tx/college cleanup + draft_results)
        with repo.transaction() as cur:
            if not promoted_from_college:
                player_id = allocate_new_player_id(repo, cur=cur)
                attrs["draft"]["player_id_source"] = "seq_player_id"
                attrs["draft"]["college_player_id"] = None

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

            # Upsert contract (nested SAVEPOINT inside outer pick transaction)
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

            # If this was a promoted college player, remove college records now that NBA records exist.
            if promoted_from_college:
                cur.execute("DELETE FROM college_player_season_stats WHERE player_id=?;", (player_id,))
                cur.execute("DELETE FROM college_draft_entries WHERE player_id=?;", (player_id,))
                cur.execute("DELETE FROM college_players WHERE player_id=?;", (player_id,))

            meta_json = _json_dumps(
                {
                    "source": str(source),
                    "prospect_source": str(prospect_source),
                    "college_promoted": bool(promoted_from_college),
                }
            )

            tx_entry = {
                "type": "draft_pick_applied",
                "source": str(source),
                "date": signed_date_iso,
                "season_year": dy - 1,  # drafted after season dy-1
                "teams": [team_id],
                "draft_year": dy,
                "prospect_temp_id": str(prospect.temp_id),
                "prospect_source": prospect_source,
                "college_promoted": bool(promoted_from_college),
                "pick": {
                    "overall_no": int(turn.overall_no),
                    "round": int(turn.round),
                    "slot": int(turn.slot),
                    "pick_id": pick_id,
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

            # Transactions log (nested SAVEPOINT inside outer pick transaction)
            repo.insert_transactions([tx_entry])

            # Draft SSOT record (must commit with the same pick transaction)
            cur.execute(
                """
                INSERT INTO draft_results(
                    pick_id, draft_year, overall_no, "round", slot,
                    original_team, drafting_team,
                    prospect_temp_id, player_id, contract_id,
                    applied_at, source, meta_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pick_id) DO UPDATE SET
                    draft_year=excluded.draft_year,
                    overall_no=excluded.overall_no,
                    "round"=excluded."round",
                    slot=excluded.slot,
                    original_team=excluded.original_team,
                    drafting_team=excluded.drafting_team,
                    prospect_temp_id=excluded.prospect_temp_id,
                    player_id=excluded.player_id,
                    contract_id=excluded.contract_id,
                    applied_at=excluded.applied_at,
                    source=excluded.source,
                    meta_json=excluded.meta_json,
                    updated_at=excluded.updated_at;
                """,
                (
                    pick_id,
                    dy,
                    int(turn.overall_no),
                    int(turn.round),
                    int(turn.slot),
                    str(turn.original_team),
                    str(team_id),
                    str(prospect.temp_id),
                    str(player_id),
                    str(contract_id),
                    now,
                    str(source),
                    meta_json,
                    now,
                    now,
                ),
            )

    return ApplyPickResult(
        player_id=player_id,
        contract_id=contract_id,
        team_id=team_id,
        tx_entry=tx_entry,
        promoted_from_college=bool(promoted_from_college),
    )
