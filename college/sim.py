from __future__ import annotations

import math
import random
from typing import Dict, Iterable, List, Sequence, Tuple

from . import config
from .types import CollegePlayer, CollegeSeasonStats, CollegeTeam, CollegeTeamSeasonStats


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _softmax(xs: List[float]) -> List[float]:
    if not xs:
        return []
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    s = sum(exps)
    if s <= 0:
        return [1.0 / len(xs)] * len(xs)
    return [e / s for e in exps]


def simulate_college_season(
    rng: random.Random,
    *,
    season_year: int,
    teams: Sequence[CollegeTeam],
    players: Sequence[CollegePlayer],
) -> Tuple[List[CollegeTeamSeasonStats], List[CollegeSeasonStats]]:
    """
    Lightweight season simulator:
    - Generates team W/L based on roster strength
    - Generates per-player season stats based on role/minutes/skills

    This is *not* a possession engine; it’s a fast scouting-friendly generator.
    """
    # group players by team
    by_team: Dict[str, List[CollegePlayer]] = {}
    for p in players:
        by_team.setdefault(p.college_team_id, []).append(p)

    # league strength baseline
    team_strengths: Dict[str, float] = {}
    for t in teams:
        roster = by_team.get(t.college_team_id, [])
        if not roster:
            team_strengths[t.college_team_id] = 0.0
            continue
        # weight top players more (star impact)
        ovr_sorted = sorted((p.ovr for p in roster), reverse=True)
        w = [1.0, 0.9, 0.85, 0.8, 0.75, 0.65, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30]
        s = 0.0
        for i, o in enumerate(ovr_sorted[: len(w)]):
            s += float(o) * w[i]
        team_strengths[t.college_team_id] = s / sum(w[: min(len(ovr_sorted), len(w))])

    league_avg_strength = sum(team_strengths.values()) / max(1, len(team_strengths))

    # pace (possession proxy) per team
    team_pace: Dict[str, float] = {}
    for t in teams:
        team_pace[t.college_team_id] = float(_clamp(rng.gauss(config.POSSESSIONS_MEAN, config.POSSESSIONS_STD), 58.0, 78.0))

    team_season: List[CollegeTeamSeasonStats] = []
    player_season: List[CollegeSeasonStats] = []

    # determine win probabilities + sample wins
    for t in teams:
        tid = t.college_team_id
        strength = team_strengths.get(tid, league_avg_strength)
        # Map strength diff to win prob; tuned for plausible spread
        diff = (strength - league_avg_strength) / 6.0
        win_p = float(_clamp(_sigmoid(diff), 0.10, 0.90))

        games = int(config.COLLEGE_SEASON_GAMES_PER_TEAM)
        wins = sum(1 for _ in range(games) if rng.random() < win_p)
        losses = games - wins

        # Team PPG baseline anchored to talent + noise
        off_ppg = float(_clamp(rng.gauss(config.TEAM_PPG_MEAN + 0.35 * diff * 10.0, config.TEAM_PPG_STD), 58.0, 92.0))
        def_ppg = float(_clamp(rng.gauss(config.TEAM_PPG_MEAN - 0.30 * diff * 10.0, config.TEAM_PPG_STD), 58.0, 92.0))

        # SRS-like number
        srs = (off_ppg - def_ppg) + rng.gauss(0.0, 1.8)

        team_season.append(
            CollegeTeamSeasonStats(
                season_year=int(season_year),
                college_team_id=tid,
                wins=int(wins),
                losses=int(losses),
                srs=float(srs),
                pace=float(team_pace[tid]),
                off_ppg=float(off_ppg),
                def_ppg=float(def_ppg),
                meta={},
            )
        )

    # per-player stats
    for t in teams:
        tid = t.college_team_id
        roster = by_team.get(tid, [])
        if not roster:
            continue

        # Role score: OVR + class_year bonus + IQ bonus
        role_scores: List[float] = []
        for p in roster:
            iq = float(p.attrs.get("skill", {}).get("iq", 0.55))
            role_scores.append(float(p.ovr) + 1.5 * (p.class_year - 1) + 5.0 * (iq - 0.55))

        shares = _softmax([rs / 7.5 for rs in role_scores])  # soften
        # Convert shares to minutes per game; starters get more, bench less
        mpgs: List[float] = []
        for sh in shares:
            mpg = 6.0 + sh * 160.0  # average ~ 6 + 160/12 ~ 19.3
            mpgs.append(float(_clamp(mpg, 4.0, 34.0)))

        # normalize to team minutes (200)
        total = sum(mpgs)
        if total > 0:
            scale = config.COLLEGE_TEAM_MINUTES_PER_GAME / total
            mpgs = [float(_clamp(m * scale, 3.0, 36.0)) for m in mpgs]

        games = int(config.COLLEGE_SEASON_GAMES_PER_TEAM)
        pace = float(team_pace[tid])

        # team scoring target
        team_ppg = next((ts.off_ppg for ts in team_season if ts.college_team_id == tid), config.TEAM_PPG_MEAN)

        # allocate team points to players by usage (usage depends on ovr/pos/shooting)
        usage_scores: List[float] = []
        for i, p in enumerate(roster):
            shoot = float(p.attrs.get("skill", {}).get("shooting", 0.55))
            ath = float(p.attrs.get("skill", {}).get("athleticism", 0.55))
            pos_boost = 0.0
            if p.pos in ("PG", "SG"):
                pos_boost = 0.6
            elif p.pos == "SF":
                pos_boost = 0.4
            elif p.pos == "PF":
                pos_boost = 0.25
            else:
                pos_boost = 0.15
            u = float(p.ovr) + 6.0 * (shoot - 0.55) + 3.5 * (ath - 0.55) + 2.0 * pos_boost
            # minutes influence usage
            u += 0.25 * mpgs[i]
            usage_scores.append(u)

        usage_shares = _softmax([u / 10.0 for u in usage_scores])
        # per-player ppg base
        ppgs = [max(0.0, float(team_ppg) * us * rng.uniform(0.90, 1.10)) for us in usage_shares]

        for i, p in enumerate(roster):
            shoot = float(p.attrs.get("skill", {}).get("shooting", 0.55))
            iq = float(p.attrs.get("skill", {}).get("iq", 0.55))
            ath = float(p.attrs.get("skill", {}).get("athleticism", 0.55))

            mpg = float(mpgs[i])
            usg = float(_clamp(0.10 + 0.55 * usage_shares[i], 0.08, 0.38))

            # shooting splits
            fg = float(_clamp(rng.gauss(config.FG_PCT_MEAN + 0.10 * (shoot - 0.55), config.FG_PCT_STD), 0.36, 0.60))
            tp = float(_clamp(rng.gauss(config.TP_PCT_MEAN + 0.18 * (shoot - 0.55), config.TP_PCT_STD), 0.22, 0.48))
            ft = float(_clamp(rng.gauss(config.FT_PCT_MEAN + 0.22 * (shoot - 0.55), config.FT_PCT_STD), 0.50, 0.92))

            # counting stats proxies (per game)
            pts = float(_clamp(ppgs[i], 0.5, 30.0))
            reb = float(_clamp((0.8 + 0.12 * (p.height_in - 75) + 5.0 * (ath - 0.55)) * (mpg / 30.0), 0.5, 13.0))
            ast = float(_clamp((2.2 + (1.8 if p.pos == "PG" else 0.8 if p.pos == "SG" else 0.4) + 4.5 * (iq - 0.55)) * (mpg / 30.0), 0.2, 9.5))
            stl = float(_clamp((0.7 + 2.0 * (ath - 0.55) + 1.2 * (iq - 0.55)) * (mpg / 30.0), 0.2, 2.6))
            blk = float(_clamp((0.4 + (0.9 if p.pos in ("PF", "C") else 0.2) + 2.2 * (ath - 0.55)) * (mpg / 30.0), 0.1, 3.0))
            tov = float(_clamp((1.1 + 4.8 * usg - 1.2 * (iq - 0.55)) * (mpg / 30.0), 0.3, 4.2))
            pf = float(_clamp((1.6 + 0.8 * (mpg / 30.0) + rng.gauss(0.0, 0.2)), 0.8, 3.8))

            # TS% proxy: combine splits + usg penalty
            ts = float(_clamp(0.49 + 0.18 * (shoot - 0.55) - 0.05 * (usg - 0.20) + rng.gauss(0.0, 0.02), 0.42, 0.68))

            player_season.append(
                CollegeSeasonStats(
                    season_year=int(season_year),
                    player_id=p.player_id,
                    college_team_id=tid,
                    games=int(games),
                    mpg=float(mpg),
                    pts=float(pts),
                    reb=float(reb),
                    ast=float(ast),
                    stl=float(stl),
                    blk=float(blk),
                    tov=float(tov),
                    pf=float(pf),
                    fg_pct=float(fg),
                    tp_pct=float(tp),
                    ft_pct=float(ft),
                    usg=float(usg),
                    ts_pct=float(ts),
                    pace=float(pace),
                    meta={},
                )
            )

    return team_season, player_season
