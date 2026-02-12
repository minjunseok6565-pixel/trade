from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from . import config
from .types import CollegePlayer, CollegeTeam, json_dumps

# ----------------------------
# Name generation (simple + fast)
# ----------------------------

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Chris", "Devin", "Cameron", "Morgan", "Jaden", "Casey", "Riley",
    "Marcus", "Darius", "Ethan", "Noah", "Liam", "Aiden", "Kai", "Miles", "Zion", "Logan",
    "Trevor", "Isaiah", "Aaron", "Damon", "Bryce", "Julian", "Cole", "Grant", "Reed", "Cyrus",
]
_LAST_NAMES = [
    "Walker", "Johnson", "Williams", "Brown", "Miller", "Davis", "Anderson", "Moore", "Taylor", "Thomas",
    "Jackson", "White", "Harris", "Martin", "Thompson", "Garcia", "Martinez", "Robinson", "Clark", "Lewis",
    "Young", "Allen", "King", "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Carter",
]

def _pick_weighted(rng: random.Random, items: Sequence[Tuple[str, float]]) -> str:
    total = sum(w for _, w in items)
    r = rng.random() * total
    acc = 0.0
    for v, w in items:
        acc += w
        if r <= acc:
            return v
    return items[-1][0]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _round_int(x: float) -> int:
    return int(round(x))


@dataclass(frozen=True, slots=True)
class PlayerProfile:
    name: str
    pos: str
    age: int
    height_in: int
    weight_lb: int
    ovr: int
    attrs: Dict


def sample_class_strength(rng: random.Random) -> float:
    """Sample a class_strength ~ N(0, std) clipped to clamp."""
    lo, hi = config.CLASS_STRENGTH_CLAMP
    x = rng.gauss(0.0, config.CLASS_STRENGTH_STD)
    return float(_clamp(x, lo, hi))


def generate_player_profile(
    rng: random.Random,
    *,
    class_strength: float,
    class_year: int,
) -> PlayerProfile:
    """
    Generate a single player profile.

    Philosophy:
    - Most players cluster around mid OVR.
    - Tail probabilities (true stars / weak classes) are controlled by class_strength.
    """
    # Name
    name = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"

    # Position
    pos = _pick_weighted(rng, list(config.POS_WEIGHTS.items()))

    # Size (rough by position)
    # Keep this light; detailed body types can be layered later.
    if pos in ("PG", "SG"):
        h_mu = 74.0 if pos == "PG" else 76.0
        w_mu = 185.0 if pos == "PG" else 200.0
    elif pos == "SF":
        h_mu, w_mu = 79.0, 215.0
    elif pos == "PF":
        h_mu, w_mu = 82.0, 235.0
    else:  # C
        h_mu, w_mu = 84.0, 255.0

    height_in = _round_int(_clamp(rng.gauss(h_mu, 1.8), config.HEIGHT_IN_RANGE[0], config.HEIGHT_IN_RANGE[1]))
    weight_lb = _round_int(_clamp(rng.gauss(w_mu, 18.0), config.WEIGHT_LB_RANGE[0], config.WEIGHT_LB_RANGE[1]))

    # Age: base by class year + small jitter
    base_age = config.BASE_AGE_BY_CLASS_YEAR.get(class_year, 18 + (class_year - 1))
    age = int(_clamp(base_age + rng.choice([0, 0, 0, 1]), 18, 24))

    # Talent core
    # Base latent talent
    t = rng.gauss(0.0, 1.0)

    # Elite tail probability rises with class_strength
    elite_prob = 1.0 / (1.0 + math.exp(-(-2.0 + 0.9 * class_strength)))  # ~0.12 at 0, ~0.27 at +2
    bust_prob = 1.0 / (1.0 + math.exp(-(-1.0 - 0.8 * class_strength)))   # ~0.27 at 0, ~0.13 at +2

    u = rng.random()
    if u < elite_prob:
        t += rng.gauss(2.2 + 0.5 * max(0.0, class_strength), 0.45)
    elif u < elite_prob + bust_prob:
        t += rng.gauss(-1.3, 0.45)

    # Map latent talent to OVR
    # Center around ~62; cap within college range
    ovr = _round_int(62.0 + 7.5 * t)
    ovr = int(_clamp(ovr, config.COLLEGE_OVR_RANGE[0], config.COLLEGE_OVR_RANGE[1]))

    # Potential: younger players tend to have higher upside; upper tail influenced by class strength
    pot_noise = rng.gauss(0.0, 5.5)
    pot = _round_int(ovr + 10.0 + pot_noise + 2.0 * max(0.0, class_strength))
    # Older class years have less remaining upside on average
    pot -= (class_year - 1) * rng.choice([1, 2])
    pot = int(_clamp(pot, ovr, 97))

    # Simple skill factors used by sim/declarations (can be expanded later)
    shooting = float(_clamp(0.40 + 0.006 * (ovr - 55) + rng.gauss(0.0, 0.06), 0.25, 0.85))
    athletic = float(_clamp(0.45 + 0.007 * (ovr - 55) + rng.gauss(0.0, 0.08), 0.25, 0.90))
    iq = float(_clamp(0.40 + 0.006 * (ovr - 55) + rng.gauss(0.0, 0.07), 0.25, 0.90))
    work_ethic = float(_clamp(rng.gauss(0.58, 0.14), 0.20, 0.95))

    attrs = {
        "potential": int(pot),
        "talent_z": float(t),
        "skill": {
            "shooting": shooting,
            "athleticism": athletic,
            "iq": iq,
        },
        "traits": {
            "work_ethic": work_ethic,
        },
        "meta": {
            "class_strength": float(class_strength),
        },
    }

    return PlayerProfile(
        name=name,
        pos=pos,
        age=age,
        height_in=height_in,
        weight_lb=weight_lb,
        ovr=ovr,
        attrs=attrs,
    )


def build_college_teams() -> List[CollegeTeam]:
    """Build the full list of college teams.

    Uses configured seeds for the first N teams, and deterministically auto-generates
    additional teams if COLLEGE_TEAM_COUNT > len(COLLEGE_TEAMS).
    """
    teams: List[CollegeTeam] = []

    seeds_by_id = {s.college_team_id: s for s in config.COLLEGE_TEAMS}
    confs = list(getattr(config, "COLLEGE_CONFERENCES", None) or ["North", "South", "East", "West"])

    for i in range(1, int(config.COLLEGE_TEAM_COUNT) + 1):
        tid = f"COL_{i:03d}"
        seed = seeds_by_id.get(tid)
        if seed is not None:
            name = seed.name
            conf = seed.conference
        else:
            conf = confs[(i - 1) % len(confs)]
            name = _auto_team_name(i, conf)

        teams.append(
            CollegeTeam(
                college_team_id=tid,
                name=str(name),
                conference=str(conf),
                meta={},
            )
        )

    return teams


def _auto_team_name(i: int, conference: str) -> str:
    """Deterministically generate additional fictional team names.

    Avoid randomness at import time; output must be stable across runs.
    """
    prefixes = [
        "Great", "Iron", "Pine", "Cedar", "Metro", "Summit", "Blue", "Stone", "Coastal", "Bayou",
        "Sun", "Desert", "Gulf", "Magnolia", "River", "Capitol", "Atlantic", "Crown", "Palisade", "Pacific",
        "Redstone", "Sierra", "Canyon", "Golden", "Frontier", "Highland", "Sequoia", "Prairie", "Lakeside", "Harbor",
    ]
    suffixes = ["State", "University", "College", "Tech", "A&M", "Institute", "Poly", "Academy"]

    p = prefixes[(i - 1) % len(prefixes)]
    s = suffixes[((i - 1) // len(prefixes)) % len(suffixes)]
    # Add numeric tail to guarantee uniqueness at high team counts.
    return f"{p} {conference} {s} {i:03d}"


def generate_initial_world_players(
    rng: random.Random,
    *,
    season_year: int,
    teams: Sequence[CollegeTeam],
    class_strength_for_entry_season: Callable[[int], float],
) -> List[CollegePlayer]:
    """
    Generate a full college world at game start:
    - Players for class_year 1..4 across all teams
    - entry_season_year is aligned such that class_year=1 corresponds to entry_season_year==season_year
    """
    players: List[CollegePlayer] = []
    # deterministic team ordering
    team_ids = [t.college_team_id for t in teams]

    # We don't allocate player_id here; service does it in DB-aware way.
    # We'll use temporary placeholders and let service rewrite IDs.
    tmp_id_counter = 0

    for college_team_id in team_ids:
        for class_year, n in config.BOOTSTRAP_CLASS_YEAR_COUNTS_PER_TEAM.items():
            entry_season = int(season_year - (class_year - 1))
            cs = float(class_strength_for_entry_season(entry_season))

            for _ in range(int(n)):
                prof = generate_player_profile(rng, class_strength=cs, class_year=class_year)
                tmp_id_counter += 1
                tmp_pid = f"TMP{tmp_id_counter:06d}"

                players.append(
                    CollegePlayer(
                        player_id=tmp_pid,
                        name=prof.name,
                        pos=prof.pos,
                        age=prof.age,
                        height_in=prof.height_in,
                        weight_lb=prof.weight_lb,
                        ovr=prof.ovr,
                        college_team_id=college_team_id,
                        class_year=int(class_year),
                        entry_season_year=entry_season,
                        status="ACTIVE",
                        attrs=prof.attrs,
                    )
                )

    return players


def generate_players_for_team_class(
    rng: random.Random,
    *,
    college_team_id: str,
    class_year: int,
    entry_season_year: int,
    class_strength: float,
    count: int,
) -> List[CollegePlayer]:
    """Generate players for a single team and a single class year.

    This is primarily used by the deficit-fill offseason logic. The service layer
    rewrites player_id using DB-allocated ids.
    """
    out: List[CollegePlayer] = []
    n = int(count)
    if n <= 0:
        return out

    cy = int(class_year)
    esy = int(entry_season_year)

    # Temporary ids only need to be unique within this batch.
    for k in range(n):
        prof = generate_player_profile(rng, class_strength=float(class_strength), class_year=cy)
        tmp_pid = f"TMPF{esy}{cy}{k + 1:05d}"
        out.append(
            CollegePlayer(
                player_id=tmp_pid,
                name=prof.name,
                pos=prof.pos,
                age=prof.age,
                height_in=prof.height_in,
                weight_lb=prof.weight_lb,
                ovr=prof.ovr,
                college_team_id=str(college_team_id),
                class_year=cy,
                entry_season_year=esy,
                status="ACTIVE",
                attrs=prof.attrs,
            )
        )

    return out


def generate_freshmen_for_season(
    rng: random.Random,
    *,
    entry_season_year: int,
    teams: Sequence[CollegeTeam],
    class_strength: float,
) -> List[CollegePlayer]:
    """Generate only freshmen for a new season and distribute across teams."""
    players: List[CollegePlayer] = []
    team_ids = [t.college_team_id for t in teams]
    tmp_id_counter = 0

    for college_team_id in team_ids:
        for _ in range(int(config.FRESHMEN_PER_TEAM_PER_YEAR)):
            prof = generate_player_profile(rng, class_strength=float(class_strength), class_year=1)
            tmp_id_counter += 1
            tmp_pid = f"TMPF{entry_season_year}{tmp_id_counter:05d}"

            players.append(
                CollegePlayer(
                    player_id=tmp_pid,
                    name=prof.name,
                    pos=prof.pos,
                    age=prof.age,
                    height_in=prof.height_in,
                    weight_lb=prof.weight_lb,
                    ovr=prof.ovr,
                    college_team_id=college_team_id,
                    class_year=1,
                    entry_season_year=int(entry_season_year),
                    status="ACTIVE",
                    attrs=prof.attrs,
                )
            )
    return players
