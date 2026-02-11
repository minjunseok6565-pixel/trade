from __future__ import annotations

import datetime as _dt
import json
import random
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from league_repo import LeagueRepo

from . import config
from .declarations import declare_probability
from .generation import build_college_teams, generate_freshmen_for_season, generate_initial_world_players, sample_class_strength
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
# schema
# ----------------------------

def ensure_college_schema(repo: LeagueRepo) -> None:
    """
    Ensure college tables exist (idempotent).

    For early integration convenience, this lives in college.service.
    Later, you can move the DDL into LeagueRepo.init_db() by calling this.
    """
    with repo.transaction() as cur:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS college_teams (
                college_team_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                conference TEXT NOT NULL,
                meta_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS college_players (
                player_id TEXT PRIMARY KEY,
                college_team_id TEXT NOT NULL,
                class_year INTEGER NOT NULL,
                entry_season_year INTEGER NOT NULL,
                status TEXT NOT NULL,

                name TEXT NOT NULL,
                pos TEXT NOT NULL,
                age INTEGER NOT NULL,
                height_in INTEGER NOT NULL,
                weight_lb INTEGER NOT NULL,
                ovr INTEGER NOT NULL,
                attrs_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_college_players_team ON college_players(college_team_id);
            CREATE INDEX IF NOT EXISTS idx_college_players_status ON college_players(status);
            CREATE INDEX IF NOT EXISTS idx_college_players_entry ON college_players(entry_season_year);

            CREATE TABLE IF NOT EXISTS college_player_season_stats (
                season_year INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                college_team_id TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                PRIMARY KEY (season_year, player_id)
            );

            CREATE INDEX IF NOT EXISTS idx_college_player_stats_season_team ON college_player_season_stats(season_year, college_team_id);

            CREATE TABLE IF NOT EXISTS college_team_season_stats (
                season_year INTEGER NOT NULL,
                college_team_id TEXT NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                srs REAL NOT NULL,
                pace REAL NOT NULL,
                off_ppg REAL NOT NULL,
                def_ppg REAL NOT NULL,
                meta_json TEXT NOT NULL,
                PRIMARY KEY (season_year, college_team_id)
            );

            CREATE TABLE IF NOT EXISTS college_draft_entries (
                draft_year INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                declared_at TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                PRIMARY KEY (draft_year, player_id)
            );

            CREATE INDEX IF NOT EXISTS idx_college_entries_year ON college_draft_entries(draft_year);

            CREATE TABLE IF NOT EXISTS draft_class_strength (
                draft_year INTEGER PRIMARY KEY,
                strength REAL NOT NULL,
                seed INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


# ----------------------------
# meta helpers
# ----------------------------

def _get_meta(repo: LeagueRepo, key: str) -> Optional[str]:
    row = repo._conn.execute("SELECT value FROM meta WHERE key=?;", (key,)).fetchone()
    if not row:
        return None
    return str(row[0]) if row[0] is not None else None


def _set_meta(repo: LeagueRepo, key: str, value: str) -> None:
    repo._conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
        (key, value),
    )


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


def allocate_player_ids(repo: LeagueRepo, *, count: int) -> List[str]:
    """
    Allocate sequential player_id values: P000001, P000002, ...

    Uses meta('seq_player_id') as the primary source for speed,
    and falls back to scanning tables to ensure collision-free init.
    """
    n = int(count)
    if n <= 0:
        return []

    key = "seq_player_id"
    cur = _get_meta(repo, key)
    if cur is None:
        # initialize from max among players and college_players
        max_n = _compute_max_player_num(repo)
        _set_meta(repo, key, str(max_n))
        cur_n = max_n
    else:
        try:
            cur_n = int(cur)
        except Exception:
            cur_n = _compute_max_player_num(repo)
            _set_meta(repo, key, str(cur_n))

    ids: List[str] = []
    for i in range(n):
        cur_n += 1
        ids.append(f"P{cur_n:06d}")

    _set_meta(repo, key, str(cur_n))
    return ids


# ----------------------------
# class strength
# ----------------------------

def get_or_create_class_strength(repo: LeagueRepo, *, draft_year: int, seed_salt: str) -> float:
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

    repo._conn.execute(
        "INSERT INTO draft_class_strength(draft_year, strength, seed, created_at) VALUES (?, ?, ?, ?);",
        (dy, float(strength), int(seed), _utc_now_iso()),
    )
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
    """
    sy = int(season_year)
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        ensure_college_schema(repo)

        # If already bootstrapped for this season year, skip.
        marker_key = "college_bootstrap_season_year"
        marker = _get_meta(repo, marker_key)
        if marker == str(sy):
            return

        # Ensure teams
        existing_team_count = repo._conn.execute("SELECT COUNT(*) FROM college_teams;").fetchone()[0]
        if int(existing_team_count) <= 0:
            teams = build_college_teams()
            with repo.transaction() as cur:
                for t in teams:
                    cur.execute(
                        "INSERT INTO college_teams(college_team_id, name, conference, meta_json) VALUES (?, ?, ?, ?);",
                        (t.college_team_id, t.name, t.conference, json_dumps(t.meta)),
                    )

        # Load teams (ordered)
        team_rows = repo._conn.execute(
            "SELECT college_team_id, name, conference, meta_json FROM college_teams ORDER BY college_team_id ASC;"
        ).fetchall()
        teams: List[CollegeTeam] = []
        for r in team_rows:
            teams.append(
                CollegeTeam(
                    college_team_id=str(r[0]),
                    name=str(r[1]),
                    conference=str(r[2]),
                    meta=json_loads(str(r[3])) or {},
                )
            )

        # Ensure initial players
        existing_player_count = repo._conn.execute("SELECT COUNT(*) FROM college_players;").fetchone()[0]
        if int(existing_player_count) <= 0:
            # Strength provider: use draft_year = entry_season_year + 1 as the cohort's "expected draft year"
            def strength_for_entry(entry_season_year: int) -> float:
                dy = int(entry_season_year) + 1
                return get_or_create_class_strength(repo, draft_year=dy, seed_salt=f"bootstrap@{sy}")

            rng = random.Random(_stable_seed("college_bootstrap_players", sy))
            tmp_players = generate_initial_world_players(
                rng,
                season_year=sy,
                teams=teams,
                class_strength_for_entry_season=strength_for_entry,
            )

            # Allocate real IDs
            new_ids = allocate_player_ids(repo, count=len(tmp_players))
            players: List[CollegePlayer] = []
            for pid, p in zip(new_ids, tmp_players):
                players.append(
                    CollegePlayer(
                        player_id=pid,
                        name=p.name,
                        pos=p.pos,
                        age=p.age,
                        height_in=p.height_in,
                        weight_lb=p.weight_lb,
                        ovr=p.ovr,
                        college_team_id=p.college_team_id,
                        class_year=p.class_year,
                        entry_season_year=p.entry_season_year,
                        status=p.status,
                        attrs=p.attrs,
                    )
                )

            with repo.transaction() as cur:
                for p in players:
                    cur.execute(
                        """
                        INSERT INTO college_players(
                            player_id, college_team_id, class_year, entry_season_year, status,
                            name, pos, age, height_in, weight_lb, ovr, attrs_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            p.player_id,
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
        ensure_college_schema(repo)

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
                            json_dumps(ps.__dict__),
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
                    (int(dy), tr.player_id, now, json_dumps(tr.__dict__)),
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
    - Increment class_year
    - Graduate/remove players beyond 4
    - Reset DECLARED -> ACTIVE (undrafted return-to-school model; can be expanded later)
    - Generate freshmen for to_season_year and insert
    """
    fy = int(from_season_year)
    ty = int(to_season_year)
    if ty != fy + 1:
        # Keep strict to avoid accidental skipping (commercial stability)
        raise ValueError(f"advance_offseason expects consecutive years: {fy} -> {ty}")

    with LeagueRepo(db_path) as repo:
        repo.init_db()
        ensure_college_schema(repo)

        # Reset DECLARED -> ACTIVE (they returned if not drafted)
        with repo.transaction() as cur:
            cur.execute("UPDATE college_players SET status='ACTIVE' WHERE status='DECLARED';")

        # Graduate (class_year>=4) after increment step:
        # First increment class_year for all ACTIVE
        with repo.transaction() as cur:
            cur.execute(
                "UPDATE college_players SET class_year = class_year + 1 WHERE status='ACTIVE';"
            )

        # Remove those now beyond 4 (graduated)
        with repo.transaction() as cur:
            cur.execute("DELETE FROM college_players WHERE status='ACTIVE' AND class_year > 4;")

        # Load teams
        teams = _load_teams(repo)

        # Create freshmen cohort (strength tied to expected draft_year = entry_season + 1)
        dy = ty + 1
        strength = get_or_create_class_strength(repo, draft_year=dy, seed_salt=f"freshmen@{ty}")
        rng = random.Random(_stable_seed("freshmen_gen", ty))
        tmp_fresh = generate_freshmen_for_season(rng, entry_season_year=ty, teams=teams, class_strength=float(strength))

        # Allocate IDs + insert
        new_ids = allocate_player_ids(repo, count=len(tmp_fresh))
        fresh: List[CollegePlayer] = []
        for pid, p in zip(new_ids, tmp_fresh):
            fresh.append(
                CollegePlayer(
                    player_id=pid,
                    name=p.name,
                    pos=p.pos,
                    age=p.age,
                    height_in=p.height_in,
                    weight_lb=p.weight_lb,
                    ovr=p.ovr,
                    college_team_id=p.college_team_id,
                    class_year=1,
                    entry_season_year=ty,
                    status="ACTIVE",
                    attrs=p.attrs,
                )
            )

        with repo.transaction() as cur:
            for p in fresh:
                cur.execute(
                    """
                    INSERT INTO college_players(
                        player_id, college_team_id, class_year, entry_season_year, status,
                        name, pos, age, height_in, weight_lb, ovr, attrs_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        p.player_id,
                        p.college_team_id,
                        int(p.class_year),
                        int(p.entry_season_year),
                        p.status,
                        p.name,
                        p.pos,
                        int(p.age),
                        int(p.height_in),
                        int(p.weight_lb),
                        int(p.ovr),
                        json_dumps(p.attrs),
                    ),
                )


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
        ensure_college_schema(repo)
        with repo.transaction() as cur:
            cur.execute("DELETE FROM college_player_season_stats WHERE player_id=?;", (pid,))
            cur.execute("DELETE FROM college_draft_entries WHERE player_id=?;", (pid,))
            cur.execute("DELETE FROM college_players WHERE player_id=?;", (pid,))
