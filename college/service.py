from __future__ import annotations

import datetime as _dt
import json
import random
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from league_repo import LeagueRepo

from . import config
from .declarations import declare_probability
from .generation import build_college_teams, generate_initial_world_players, generate_players_for_team_class, sample_class_strength
from .sim import simulate_college_season
from .types import (
    CollegePlayer,
    CollegeSeasonStats,
    CollegeTeam,
    CollegeTeamSeasonStats,
    DraftEntryDecisionTrace,
    json_dumps,
    json_loads,
)

# ----------------------------
# time/json helpers
# ----------------------------

def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _stable_seed(*parts: object) -> int:
    """
    Stable seed across runs, independent of Python's hash randomization.
    """
    s = "|".join(str(p) for p in parts)
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return int(h)


# ----------------------------
# meta helpers
# ----------------------------

def _get_meta(repo: LeagueRepo, key: str) -> Optional[str]:
    row = repo._conn.execute("SELECT value FROM meta WHERE key=?;", (key,)).fetchone()
    if not row:
        return None
    return str(row[0]) if row[0] is not None else None


def _set_meta(repo: LeagueRepo, key: str, value: str, cur=None) -> None:
    """Write meta within an explicit transaction cursor.

    IMPORTANT: Avoid repo._conn.execute() for writes because sqlite3 will start an implicit
    transaction that can conflict with LeagueRepo.transaction()'s BEGIN.

    Policy:
      - If cur is provided, execute using that cursor.
      - Else if the connection is already inside a transaction, execute via a fresh cursor
        (do NOT start a nested BEGIN).
      - Else open a short repo.transaction() and execute inside.
    """

    sql = (
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value;"
    )
    params = (key, value)

    if cur is not None:
        cur.execute(sql, params)
        return

    # If caller already started a transaction but didn't pass cur, avoid nested BEGIN.
    if bool(getattr(repo._conn, "in_transaction", False)):
        c = repo._conn.cursor()
        try:
            c.execute(sql, params)
        finally:
            c.close()
        return

    with repo.transaction() as cur2:
        cur2.execute(sql, params)


# ----------------------------
# player_id allocation (DB-aware, collision-safe)
# ----------------------------

def _compute_max_player_num(repo: LeagueRepo) -> int:
    """
    Max numeric suffix among Pxxxxxx ids across BOTH NBA players and college_players.
    This avoids collisions even before we refactor draft/apply allocator.
    """
    # Ensure tables exist (players is created by repo.init_db; college by ensure_college_schema)
    max_n = 0

    # players table
    rows = repo._conn.execute("SELECT player_id FROM players WHERE player_id LIKE 'P%';").fetchall()
    for (pid,) in rows:
        s = str(pid)
        if len(s) >= 2 and s[1:].isdigit():
            max_n = max(max_n, int(s[1:]))

    # college_players table
    rows = repo._conn.execute("SELECT player_id FROM college_players WHERE player_id LIKE 'P%';").fetchall()
    for (pid,) in rows:
        s = str(pid)
        if len(s) >= 2 and s[1:].isdigit():
            max_n = max(max_n, int(s[1:]))

    return int(max_n)


def allocate_player_ids(repo: LeagueRepo, *, count: int, cur=None) -> List[str]:
    """
    Allocate sequential player_id values: P000001, P000002, ...

    Uses meta('seq_player_id') as the primary source for speed,
    and falls back to scanning tables to ensure collision-free init.
    """
    n = int(count)
    if n <= 0:
        return []

    key = "seq_player_id"

    def _allocate_player_ids_in_tx(*, count: int, cur) -> List[str]:
        """Allocate ids and update meta within the provided transaction cursor."""
        cur_val = _get_meta(repo, key)
        if cur_val is None:
            # initialize from max among players and college_players
            max_n = _compute_max_player_num(repo)
            _set_meta(repo, key, str(max_n), cur=cur)
            cur_n = max_n
        else:
            try:
                cur_n = int(cur_val)
            except Exception:
                cur_n = _compute_max_player_num(repo)
                _set_meta(repo, key, str(cur_n), cur=cur)

        ids: List[str] = []
        for _ in range(int(count)):
            cur_n += 1
            ids.append(f"P{cur_n:06d}")

        _set_meta(repo, key, str(cur_n), cur=cur)
        return ids

    # If caller provides a cursor, we are already inside an explicit transaction.
    if cur is not None:
        return _allocate_player_ids_in_tx(count=n, cur=cur)

    # If caller already started a transaction but didn't pass cur, avoid nested BEGIN.
    if bool(getattr(repo._conn, "in_transaction", False)):
        c = repo._conn.cursor()
        try:
            return _allocate_player_ids_in_tx(count=n, cur=c)
        finally:
            c.close()

    with repo.transaction() as cur2:
        return _allocate_player_ids_in_tx(count=n, cur=cur2)


# ----------------------------
# class strength
# ----------------------------

def get_or_create_class_strength(repo: LeagueRepo, *, draft_year: int, seed_salt: str, cur=None) -> float:
    """
    Fetch class strength from DB or create it deterministically if missing.
    """
    dy = int(draft_year)
    row = repo._conn.execute("SELECT strength FROM draft_class_strength WHERE draft_year=?;", (dy,)).fetchone()
    if row and row[0] is not None:
        return float(row[0])

    seed = _stable_seed("class_strength", dy, seed_salt)
    rng = random.Random(seed)
    strength = float(sample_class_strength(rng))
    lo, hi = config.CLASS_STRENGTH_CLAMP
    strength = float(max(lo, min(hi, strength)))

    sql = "INSERT INTO draft_class_strength(draft_year, strength, seed, created_at) VALUES (?, ?, ?, ?);"
    params = (dy, float(strength), int(seed), _utc_now_iso())
    if cur is not None:
        cur.execute(sql, params)
    elif bool(getattr(repo._conn, "in_transaction", False)):
        c = repo._conn.cursor()
        try:
            c.execute(sql, params)
        finally:
            c.close()
    else:
        with repo.transaction() as cur2:
            cur2.execute(sql, params)
    return float(strength)


# ----------------------------
# world bootstrap
# ----------------------------

def ensure_world_bootstrapped(db_path: str, season_year: int) -> None:
    """
    Ensure college teams + initial (1~4 class years) players exist.

    Intended call site:
      state.startup_init_state() after NBA season/year is established.

    Idempotent: safe to call multiple times.

    Upgrade-safe:
      - If COLLEGE_TEAM_COUNT increases, missing teams will be inserted.
      - If some teams exist but have no players (e.g., newly inserted teams),
        bootstrap players will be created only for those teams.
    """
    sy = int(season_year)
    with LeagueRepo(db_path) as repo:
        repo.init_db()

        marker_key = "college_bootstrap_season_year"
        marker = _get_meta(repo, marker_key)

        # Ensure teams (insert missing). Do NOT gate this on marker/meta so that
        # increasing COLLEGE_TEAM_COUNT is naturally supported.
        seed_teams = build_college_teams()
        with repo.transaction() as cur:
            for t in seed_teams:
                cur.execute(
                    "INSERT OR IGNORE INTO college_teams(college_team_id, name, conference, meta_json) VALUES (?, ?, ?, ?);",
                    (t.college_team_id, t.name, t.conference, json_dumps(t.meta)),
                )

        # Load teams (ordered)
        teams = _load_teams(repo)
        if not teams:
            # Defensive: schema exists but teams are missing for some reason.
            return

        # Fast skip: if marker matches AND every team has at least one player.
        if marker == str(sy):
            missing = repo._conn.execute(
                """
                SELECT t.college_team_id
                FROM college_teams t
                LEFT JOIN (SELECT DISTINCT college_team_id FROM college_players) p
                  ON p.college_team_id = t.college_team_id
                WHERE p.college_team_id IS NULL
                LIMIT 1;
                """
            ).fetchone()
            if missing is None:
                return

        # Strength provider: use draft_year = entry_season_year + 1 as the cohort's "expected draft year"
        def strength_for_entry(entry_season_year: int) -> float:
            dy = int(entry_season_year) + 1
            return get_or_create_class_strength(repo, draft_year=dy, seed_salt=f"bootstrap@{sy}")

        # Ensure initial players
        existing_player_count = repo._conn.execute("SELECT COUNT(*) FROM college_players;").fetchone()[0]
        created_players = False
        if int(existing_player_count) <= 0:
            rng = random.Random(_stable_seed("college_bootstrap_players", sy))
            tmp_players = generate_initial_world_players(
                rng,
                season_year=sy,
                teams=teams,
                class_strength_for_entry_season=strength_for_entry,
            )

            with repo.transaction() as cur:
                new_ids = allocate_player_ids(repo, count=len(tmp_players), cur=cur)
                for pid, p in zip(new_ids, tmp_players):
                    cur.execute(
                        """
                        INSERT INTO college_players(
                            player_id, college_team_id, class_year, entry_season_year, status,
                            name, pos, age, height_in, weight_lb, ovr, attrs_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            pid,
                            p.college_team_id,
                            int(p.class_year),
                            int(p.entry_season_year),
                            str(p.status),
                            p.name,
                            p.pos,
                            int(p.age),
                            int(p.height_in),
                            int(p.weight_lb),
                            int(p.ovr),
                            json_dumps(p.attrs),
                        ),
                    )

                _set_meta(repo, marker_key, str(sy), cur=cur)
                created_players = True

        else:
            # Only bootstrap players for teams that currently have none.
            rows = repo._conn.execute(
                """
                SELECT t.college_team_id
                FROM college_teams t
                LEFT JOIN (SELECT DISTINCT college_team_id FROM college_players) p
                  ON p.college_team_id = t.college_team_id
                WHERE p.college_team_id IS NULL
                ORDER BY t.college_team_id ASC;
                """
            ).fetchall()
            missing_team_ids = [str(r[0]) for r in rows]

            if missing_team_ids:
                team_by_id = {t.college_team_id: t for t in teams}
                tmp_players = []
                for tid in missing_team_ids:
                    t = team_by_id.get(tid)
                    if t is None:
                        continue
                    rng = random.Random(_stable_seed("college_bootstrap_players", sy, tid))
                    tmp_players.extend(
                        generate_initial_world_players(
                            rng,
                            season_year=sy,
                            teams=[t],
                            class_strength_for_entry_season=strength_for_entry,
                        )
                    )

                if tmp_players:
                    with repo.transaction() as cur:
                        new_ids = allocate_player_ids(repo, count=len(tmp_players), cur=cur)
                        for pid, p in zip(new_ids, tmp_players):
                            cur.execute(
                                """
                                INSERT INTO college_players(
                                    player_id, college_team_id, class_year, entry_season_year, status,
                                    name, pos, age, height_in, weight_lb, ovr, attrs_json
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                                """,
                                (
                                    pid,
                                    p.college_team_id,
                                    int(p.class_year),
                                    int(p.entry_season_year),
                                    str(p.status),
                                    p.name,
                                    p.pos,
                                    int(p.age),
                                    int(p.height_in),
                                    int(p.weight_lb),
                                    int(p.ovr),
                                    json_dumps(p.attrs),
                                ),
                            )

                        _set_meta(repo, marker_key, str(sy), cur=cur)
                        created_players = True
        # If players already existed but marker was missing/outdated, set it now.
        if not created_players:
            _set_meta(repo, marker_key, str(sy))


# ----------------------------
# season finalize + draft entries
# ----------------------------

def _load_active_players(repo: LeagueRepo) -> List[CollegePlayer]:
    rows = repo._conn.execute(
        """
        SELECT
            player_id, name, pos, age, height_in, weight_lb, ovr,
            college_team_id, class_year, entry_season_year, status, attrs_json
        FROM college_players
        WHERE status IN ('ACTIVE','DECLARED')
        ORDER BY college_team_id ASC, class_year ASC, ovr DESC, player_id ASC;
        """
    ).fetchall()
    out: List[CollegePlayer] = []
    for r in rows:
        out.append(
            CollegePlayer(
                player_id=str(r[0]),
                name=str(r[1]),
                pos=str(r[2]),
                age=int(r[3]),
                height_in=int(r[4]),
                weight_lb=int(r[5]),
                ovr=int(r[6]),
                college_team_id=str(r[7]),
                class_year=int(r[8]),
                entry_season_year=int(r[9]),
                status=str(r[10]),
                attrs=json_loads(str(r[11])) or {},
            )
        )
    return out


def _load_teams(repo: LeagueRepo) -> List[CollegeTeam]:
    rows = repo._conn.execute(
        "SELECT college_team_id, name, conference, meta_json FROM college_teams ORDER BY college_team_id ASC;"
    ).fetchall()
    teams: List[CollegeTeam] = []
    for r in rows:
        teams.append(
            CollegeTeam(
                college_team_id=str(r[0]),
                name=str(r[1]),
                conference=str(r[2]),
                meta=json_loads(str(r[3])) or {},
            )
        )
    return teams


def finalize_season_and_generate_entries(db_path: str, season_year: int, draft_year: int) -> None:
    """
    1) Simulate college season stats for season_year (fast)
    2) Persist team/player season stats
    3) Generate draft declarations for draft_year (usually season_year+1)

    Idempotent behavior:
    - If season stats already exist for season_year, we skip re-sim.
    - If draft entries already exist for draft_year, we skip re-generate.
    """
    sy = int(season_year)
    dy = int(draft_year)

    with LeagueRepo(db_path) as repo:
        repo.init_db()

        # Ensure class strength exists for this draft year
        strength = get_or_create_class_strength(repo, draft_year=dy, seed_salt=f"entries@{sy}")

        # Check if season stats already exist
        season_stats_exist = repo._conn.execute(
            "SELECT 1 FROM college_team_season_stats WHERE season_year=? LIMIT 1;", (sy,)
        ).fetchone() is not None

        players = _load_active_players(repo)
        teams = _load_teams(repo)

        if not season_stats_exist:
            rng = random.Random(_stable_seed("college_season_sim", sy))
            team_stats, player_stats = simulate_college_season(rng, season_year=sy, teams=teams, players=players)

            with repo.transaction() as cur:
                for ts in team_stats:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO college_team_season_stats(
                            season_year, college_team_id, wins, losses, srs, pace, off_ppg, def_ppg, meta_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            int(ts.season_year),
                            ts.college_team_id,
                            int(ts.wins),
                            int(ts.losses),
                            float(ts.srs),
                            float(ts.pace),
                            float(ts.off_ppg),
                            float(ts.def_ppg),
                            json_dumps(ts.meta),
                        ),
                    )

                for ps in player_stats:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO college_player_season_stats(
                            season_year, player_id, college_team_id, stats_json
                        ) VALUES (?, ?, ?, ?);
                        """,
                        (
                            int(ps.season_year),
                            ps.player_id,
                            ps.college_team_id,
                            json_dumps(ps.to_json_dict()),
                        ),
                    )

        # Check if entries already exist
        entries_exist = repo._conn.execute(
            "SELECT 1 FROM college_draft_entries WHERE draft_year=? LIMIT 1;", (dy,)
        ).fetchone() is not None
        if entries_exist:
            return

        # Load season stats map (for declarations)
        rows = repo._conn.execute(
            "SELECT player_id, stats_json FROM college_player_season_stats WHERE season_year=?;",
            (sy,),
        ).fetchall()
        stats_by_pid: Dict[str, CollegeSeasonStats] = {}
        for pid, sjson in rows:
            d = json_loads(str(sjson)) or {}
            # stats_json에는 직렬화 버전 키(__v)가 포함될 수 있으므로,
            # dataclass 생성 전에 제거해서 slots/필드 불일치로 인한 TypeError를 방지한다.
            if isinstance(d, dict):
                d.pop("__v", None)
            else:
                d = {}
            stats_by_pid[str(pid)] = CollegeSeasonStats(**d)

        # Decide declarations
        now = _utc_now_iso()
        declared: List[DraftEntryDecisionTrace] = []

        for p in players:
            # Eligibility gate (simple; can be extended)
            if int(p.age) < config.MIN_DRAFT_ELIGIBLE_AGE:
                continue

            potential = int(p.attrs.get("potential", max(p.ovr, 65)))
            season_stats = stats_by_pid.get(p.player_id)

            # stable per-player RNG for reproducibility
            rng = random.Random(_stable_seed("declare", dy, p.player_id))

            trace = declare_probability(
                rng,
                player_id=p.player_id,
                draft_year=dy,
                ovr=int(p.ovr),
                age=int(p.age),
                class_year=int(p.class_year),
                potential=int(potential),
                season_stats=season_stats,
                class_strength=float(strength),
                projected_pick=None,
            )
            if trace.declared:
                declared.append(trace)

        # Persist entries and update statuses
        with repo.transaction() as cur:
            for tr in declared:
                cur.execute(
                    """
                    INSERT INTO college_draft_entries(draft_year, player_id, declared_at, decision_json)
                    VALUES (?, ?, ?, ?);
                    """,
                    (int(dy), tr.player_id, now, json_dumps(tr.to_json_dict())),
                )
                cur.execute(
                    "UPDATE college_players SET status='DECLARED' WHERE player_id=?;",
                    (tr.player_id,),
                )


# ----------------------------
# offseason advance (grade bump + freshmen)
# ----------------------------

def advance_offseason(db_path: str, from_season_year: int, to_season_year: int) -> None:
    """
    Advance college world from from_season_year -> to_season_year:
    - Reset DECLARED -> ACTIVE (undrafted return-to-school model; can be expanded later)
    - Increment class_year (+ age)
    - Graduate/remove players beyond 4
    - Deficit-fill to reach COLLEGE_ROSTER_SIZE using TARGET_CLASS_YEAR_COUNTS_PER_TEAM

    Idempotent safety:
      - Guard against double-running the same to_season_year.
    """
    fy = int(from_season_year)
    ty = int(to_season_year)
    if ty != fy + 1:
        # Keep strict to avoid accidental skipping (commercial stability)
        raise ValueError(f"advance_offseason expects consecutive years: {fy} -> {ty}")

    with LeagueRepo(db_path) as repo:
        repo.init_db()

        meta_key = f"college_advanced_to_{ty}"
        if _get_meta(repo, meta_key) == "1":
            return

        # Load teams (ordered)
        teams = _load_teams(repo)
        team_ids = [t.college_team_id for t in teams]

        roster_size = int(config.COLLEGE_ROSTER_SIZE)
        target_dist = dict(getattr(config, "TARGET_CLASS_YEAR_COUNTS_PER_TEAM", {}) or {})

        if sum(int(v) for v in target_dist.values()) != roster_size:
            raise ValueError("TARGET_CLASS_YEAR_COUNTS_PER_TEAM must sum to COLLEGE_ROSTER_SIZE")

        with repo.transaction() as cur:
            # (1) Reset DECLARED -> ACTIVE (they returned if not drafted)
            cur.execute("UPDATE college_players SET status='ACTIVE' WHERE status='DECLARED';")

            # (2) Grade bump: increment class_year/age for all ACTIVE
            cur.execute("UPDATE college_players SET class_year = class_year + 1, age = age + 1 WHERE status='ACTIVE';")

            # (3) Graduate: remove those now beyond 4
            cur.execute("DELETE FROM college_players WHERE status='ACTIVE' AND class_year > 4;")

            # (4) Safety trim: if any team is over roster_size, trim lowest OVR first.
            for tid in team_ids:
                row = repo._conn.execute(
                    "SELECT COUNT(*) FROM college_players WHERE status='ACTIVE' AND college_team_id=?;",
                    (tid,),
                ).fetchone()
                total = int(row[0] or 0)
                if total <= roster_size:
                    continue
                excess = int(total - roster_size)
                pid_rows = repo._conn.execute(
                    """
                    SELECT player_id
                    FROM college_players
                    WHERE status='ACTIVE' AND college_team_id=?
                    ORDER BY ovr ASC, player_id ASC
                    LIMIT ?;
                    """,
                    (tid, excess),
                ).fetchall()
                pids = [str(r[0]) for r in pid_rows]
                for pid in pids:
                    cur.execute("DELETE FROM college_player_season_stats WHERE player_id=?;", (pid,))
                    cur.execute("DELETE FROM college_players WHERE player_id=?;", (pid,))

            # (5) Aggregate current counts by team/class
            rows = repo._conn.execute(
                """
                SELECT college_team_id, class_year, COUNT(*) AS cnt
                FROM college_players
                WHERE status='ACTIVE'
                GROUP BY college_team_id, class_year;
                """
            ).fetchall()

            counts: Dict[str, Dict[int, int]] = {tid: {1: 0, 2: 0, 3: 0, 4: 0} for tid in team_ids}
            for team_id, class_year, cnt in rows:
                tid = str(team_id)
                cy = int(class_year)
                if tid not in counts:
                    counts[tid] = {1: 0, 2: 0, 3: 0, 4: 0}
                if cy in (1, 2, 3, 4):
                    counts[tid][cy] = int(cnt)

            # (6) Compute deficit-fill plan toward target distribution (adds only)
            plan: Dict[str, Dict[int, int]] = {}
            for tid in team_ids:
                cur_counts = counts.get(tid) or {1: 0, 2: 0, 3: 0, 4: 0}
                total = int(sum(int(v) for v in cur_counts.values()))
                slots = int(roster_size - total)
                if slots <= 0:
                    continue

                need = {cy: max(0, int(target_dist.get(cy, 0)) - int(cur_counts.get(cy, 0))) for cy in (1, 2, 3, 4)}
                p = {1: 0, 2: 0, 3: 0, 4: 0}
                remaining = int(slots)

                # Fill the biggest deficits first; ties prefer lower class year.
                for cy, nneed in sorted(need.items(), key=lambda kv: (-kv[1], kv[0])):
                    if remaining <= 0:
                        break
                    take = int(min(int(nneed), remaining))
                    if take > 0:
                        p[cy] += take
                        remaining -= take

                # If roster is below cap but already over target in all classes (rare), fill with 1st-years.
                if remaining > 0:
                    p[1] += int(remaining)

                plan[tid] = p

            # (7) Generate deficit-fill players (may include transfers/upperclassmen)
            tmp_new: List[CollegePlayer] = []
            strength_cache: Dict[int, float] = {}

            for tid, by_class in plan.items():
                for cy in (1, 2, 3, 4):
                    n_new = int(by_class.get(cy, 0) or 0)
                    if n_new <= 0:
                        continue

                    entry_season_year = int(ty - (cy - 1))
                    draft_year = int(entry_season_year) + 1

                    if draft_year not in strength_cache:
                        strength_cache[draft_year] = float(
                            get_or_create_class_strength(repo, draft_year=draft_year, seed_salt=f"fill@{ty}", cur=cur)
                        )

                    rng = random.Random(_stable_seed("college_deficit_fill", ty, tid, cy))
                    tmp_new.extend(
                        generate_players_for_team_class(
                            rng,
                            college_team_id=tid,
                            class_year=cy,
                            entry_season_year=entry_season_year,
                            class_strength=float(strength_cache[draft_year]),
                            count=n_new,
                        )
                    )

            # (8) Insert new players (single id allocation for collision safety)
            if tmp_new:
                new_ids = allocate_player_ids(repo, count=len(tmp_new), cur=cur)
                for pid, p in zip(new_ids, tmp_new):
                    cur.execute(
                        """
                        INSERT INTO college_players(
                            player_id, college_team_id, class_year, entry_season_year, status,
                            name, pos, age, height_in, weight_lb, ovr, attrs_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            pid,
                            p.college_team_id,
                            int(p.class_year),
                            int(p.entry_season_year),
                            "ACTIVE",
                            p.name,
                            p.pos,
                            int(p.age),
                            int(p.height_in),
                            int(p.weight_lb),
                            int(p.ovr),
                            json_dumps(p.attrs),
                        ),
                    )

            _set_meta(repo, meta_key, "1", cur=cur)


# ----------------------------
# draft promotion cleanup
# ----------------------------

def remove_drafted_player(db_path: str, player_id: str) -> None:
    """
    Called at the moment a player is drafted into NBA.
    College stats are ephemeral by design; we remove college-side records.

    This prevents duplicated player records and keeps DB lean.
    """
    pid = str(player_id)
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            cur.execute("DELETE FROM college_player_season_stats WHERE player_id=?;", (pid,))
            cur.execute("DELETE FROM college_draft_entries WHERE player_id=?;", (pid,))
            cur.execute("DELETE FROM college_players WHERE player_id=?;", (pid,))
