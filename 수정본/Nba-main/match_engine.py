from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd


# -----------------------------
# 유틸: 안전하게 레이팅 가져오기
# -----------------------------
def _get_rating(row: pd.Series, col: str, default: float = 50.0) -> float:
    if col not in row or pd.isna(row[col]):
        return default
    try:
        v = float(row[col])
    except (TypeError, ValueError):
        return default
    return max(1.0, min(99.0, v))


# -----------------------------
# Player / Team
# -----------------------------
@dataclass
class Player:
    player_id: int
    name: str
    team_id: str
    pos: str
    overall: float
    ratings: Dict[str, float]

    # 박스스코어
    stats: Dict[str, float] = field(default_factory=lambda: {
        "MIN": 0.0,
        "PTS": 0.0,
        "REB": 0.0,
        "AST": 0.0,
        "STL": 0.0,
        "BLK": 0.0,
        "TOV": 0.0,
        "FGM": 0.0,
        "FGA": 0.0,
        "3PM": 0.0,
        "3PA": 0.0,
        "FTM": 0.0,
        "FTA": 0.0,
        "PF": 0.0,
    })

    # usage(공격 비중) 기본 가중치
    usage_weight: float = 1.0
    desired_minutes: float = 0.0

    def inc(self, key: str, val: float = 1.0) -> None:
        self.stats[key] = self.stats.get(key, 0.0) + val

    def reset_stats(self) -> None:
        for k in list(self.stats.keys()):
            self.stats[k] = 0.0


@dataclass
class Team:
    team_id: str
    players: List[Player]
    rotation_players: List[Player]
    tactics: Dict[str, Any]

    def __init__(self, team_id: str, team_df: pd.DataFrame, tactics: Optional[Dict[str, Any]] = None):
        self.team_id = team_id

        # 1) 전체 로스터 → Player 객체 생성
        all_players: List[Player] = []
        for pid, row in team_df.iterrows():
            ratings = self._build_ratings(row)
            p = Player(
                player_id=int(pid),
                name=str(row.get("Name", f"Player {pid}")),
                team_id=team_id,
                pos=str(row.get("POS", "")),
                overall=float(row.get("OVR", row.get("Overall", ratings.get("Overall", 75.0)))),
                ratings=ratings,
            )
            all_players.append(p)

        # 전반적으로 OVR 순으로 정렬
        all_players.sort(key=lambda p: p.overall, reverse=True)

        # 2) 전술 기본값 설정
        base_tactics = {
            "pace": 0,                         # -2 ~ +2
            "offense_scheme": "pace_space",    # 6개 중 하나
            "offense_secondary_scheme": "pace_space",
            "offense_primary_weight": 5,
            "offense_secondary_weight": 5,
            "defense_scheme": "drop_coverage", # 6개 중 하나
            "defense_secondary_scheme": "drop_coverage",
            "defense_primary_weight": 5,
            "defense_secondary_weight": 5,
            "rotation_size": 9,                # 6~10
            "lineup": {
                "starters": [],
                "bench": [],
            },
            "minutes": {},
            "fatigue_factor": 1.0,             # 팀 컨디션 (0.9 ~ 1.05 정도)
        }
        if tactics:
            base_tactics.update(tactics)
        self.tactics = base_tactics

        # 3) 로테이션 구성 (스타팅 5 + 벤치)
        lineup = self.tactics.get("lineup") or {}
        starter_ids = lineup.get("starters") or []
        bench_ids = lineup.get("bench") or []
        rotation_size = int(self.tactics.get("rotation_size", 9))
        rotation_size = max(6, min(10, rotation_size))

        by_id = {p.player_id: p for p in all_players}

        starters: List[Player] = []
        for pid in starter_ids:
            if pid in by_id and by_id[pid] not in starters:
                starters.append(by_id[pid])

        # 스타터 부족하면 OVR 순으로 채우기
        for p in all_players:
            if len(starters) >= 5:
                break
            if p not in starters:
                starters.append(p)

        bench: List[Player] = []
        for pid in bench_ids:
            if pid in by_id and by_id[pid] not in starters and by_id[pid] not in bench:
                bench.append(by_id[pid])

        # 벤치 부족하면 OVR 순으로 채우기
        for p in all_players:
            if len(bench) >= rotation_size - 5:
                break
            if p not in starters and p not in bench:
                bench.append(p)

        rotation_players = starters + bench
        rotation_players = rotation_players[:rotation_size]

        # usage 기본 가중치 세팅 (에이스/스타터 우대)
        for p in rotation_players:
            base = 1.0
            if p in starters:
                base = 1.3
            # 에이스: overall + Outside/Inside Scoring 높으면 더 가중
            score = max(p.ratings.get("Outside Scoring", 70.0),
                        p.ratings.get("Inside Scoring", 70.0))
            base *= 1.0 + (score - 80.0) / 100.0  # 80 이상이면 플러스
            p.usage_weight = max(0.3, base)

        self._apply_minutes(starters, bench, rotation_players, rotation_size)
        self.players = all_players
        self.rotation_players = rotation_players

    def _apply_minutes(self, starters: List[Player], bench: List[Player], rotation_players: List[Player], rotation_size: int) -> None:
        minutes_cfg = self.tactics.get("minutes") or {}
        defaults = self._default_minutes(rotation_size)

        def _get_minutes(pid: int, default_val: float) -> float:
            for key in (pid, str(pid)):
                if key in minutes_cfg:
                    try:
                        return float(minutes_cfg[key])
                    except (TypeError, ValueError):
                        continue
            return float(default_val)

        for p in starters:
            p.desired_minutes = _get_minutes(p.player_id, defaults["starter"])
        for p in bench:
            p.desired_minutes = _get_minutes(p.player_id, defaults["bench"])
        for p in rotation_players:
            if p.desired_minutes < 0:
                p.desired_minutes = 0.0

    def _default_minutes(self, rotation_size: int) -> Dict[str, float]:
        mapping = {
            6: {"starter": 41.0, "bench": 35.0},
            7: {"starter": 36.0, "bench": 30.0},
            8: {"starter": 33.0, "bench": 25.0},
            9: {"starter": 28.0, "bench": 25.0},
            10: {"starter": 25.0, "bench": 23.0},
        }
        return mapping.get(rotation_size, {"starter": 32.0, "bench": 22.0})

    # 능력치 집계
    def _build_ratings(self, row: pd.Series) -> Dict[str, float]:
        r: Dict[str, float] = {}

        # Outside
        for col in [
            "Close Shot", "Mid-Range Shot", "Three-Point Shot", "Free Throw",
            "Shot IQ", "Offensive Consistency",
        ]:
            r[col] = _get_rating(row, col)

        # Inside
        for col in [
            "Layup", "Standing Dunk", "Driving Dunk",
            "Post Hook", "Post Fade", "Post Control",
            "Draw Foul", "Hands",
        ]:
            r[col] = _get_rating(row, col)

        # Playmaking
        for col in [
            "Pass Accuracy", "Ball Handle", "Speed with Ball",
            "Pass IQ", "Pass Vision",
        ]:
            r[col] = _get_rating(row, col)

        # Defense
        for col in [
            "Interior Defense", "Perimeter Defense", "Steal", "Block",
            "Help Defense IQ", "Pass Perception", "Defensive Consistency",
        ]:
            r[col] = _get_rating(row, col)

        # Rebounding
        for col in ["Offensive Rebound", "Defensive Rebound"]:
            r[col] = _get_rating(row, col)

        # Athleticism
        for col in ["Speed", "Agility", "Strength", "Vertical", "Stamina", "Hustle"]:
            r[col] = _get_rating(row, col)

        # 기타
        for col in ["Overall Durability", "Intangibles", "Potential"]:
            r[col] = _get_rating(row, col)

        # 집계
        def mean_of(keys: List[str], fallback: str) -> float:
            vals = [r[k] for k in keys if k in r]
            if not vals:
                return _get_rating(row, fallback, 75.0)
            return sum(vals) / len(vals)

        r["Outside Scoring"] = mean_of(
            ["Close Shot", "Mid-Range Shot", "Three-Point Shot", "Free Throw",
             "Shot IQ", "Offensive Consistency"],
            "Outside Scoring",
        )
        r["Inside Scoring"] = mean_of(
            ["Layup", "Standing Dunk", "Driving Dunk", "Post Hook",
             "Post Fade", "Post Control", "Draw Foul", "Hands"],
            "Inside Scoring",
        )
        r["Playmaking"] = mean_of(
            ["Pass Accuracy", "Ball Handle", "Speed with Ball", "Pass IQ", "Pass Vision"],
            "Playmaking",
        )
        r["Defense"] = mean_of(
            ["Interior Defense", "Perimeter Defense", "Steal", "Block",
             "Help Defense IQ", "Pass Perception", "Defensive Consistency"],
            "Defense",
        )
        r["Rebounding"] = mean_of(
            ["Offensive Rebound", "Defensive Rebound"],
            "Rebounding",
        )
        r["Athleticism"] = mean_of(
            ["Speed", "Agility", "Strength", "Vertical", "Stamina", "Hustle"],
            "Athleticism",
        )

        if "OVR" in row and not pd.isna(row["OVR"]):
            r["Overall"] = float(row["OVR"])
        elif "Overall" in row and not pd.isna(row["Overall"]):
            r["Overall"] = float(row["Overall"])
        else:
            r["Overall"] = sum(r.values()) / max(1, len(r))

        return r

    def avg(self, key: str) -> float:
        vals = [p.ratings.get(key, 50.0) for p in self.rotation_players]
        return sum(vals) / max(1, len(vals))


# -----------------------------
# MatchEngine
# -----------------------------
class MatchEngine:
    def __init__(self, home: Team, away: Team, seed: Optional[int] = None):
        self.home = home
        self.away = away
        self.rng = random.Random(seed)

    def simulate_game(self) -> Dict[str, Any]:
        """Reset all player stats and simulate one full, independent game."""
        for team in (self.home, self.away):
            for p in team.players:
                p.reset_stats()

        poss = self._estimate_possessions()

        offense = self.home
        defense = self.away

        game_minutes = 48.0
        minutes_per_possession = game_minutes * 5.0 / poss

        minute_shares = {
            self.home.team_id: self._build_minute_shares(self.home),
            self.away.team_id: self._build_minute_shares(self.away),
        }

        for i in range(poss):
            next_offense = self._simulate_possession(offense, defense)

            # 사전에 지정한 출전 시간을 비율로 환산하여 분배
            for team in (offense, defense):
                shares = minute_shares.get(team.team_id) or {}
                fallback = 1.0 / len(team.rotation_players) if team.rotation_players else 0
                for p in team.rotation_players:
                    share = shares.get(p.player_id, fallback)
                    p.inc("MIN", minutes_per_possession * share)

            offense = next_offense
            defense = self.away if next_offense is self.home else self.home

        home_score = sum(p.stats["PTS"] for p in self.home.rotation_players)
        away_score = sum(p.stats["PTS"] for p in self.away.rotation_players)

        final_score = {
            self.home.team_id: int(round(home_score)),
            self.away.team_id: int(round(away_score)),
        }

        boxscore = {
            self.home.team_id: [self._box_row(p) for p in self.home.rotation_players],
            self.away.team_id: [self._box_row(p) for p in self.away.rotation_players],
        }

        return {
            "final_score": final_score,
            "boxscore": boxscore,
            "meta": {
                "possessions": poss,
            },
        }

    # -----------------------------
    # 포제션 수 추정 (pace + 체력)
    # -----------------------------
    def _estimate_possessions(self) -> int:
        base = 96  # 평균
        pace_factor = 1.0 + 0.04 * self.home.tactics.get("pace", 0) + 0.04 * self.away.tactics.get("pace", 0)

        stam_home = self.home.avg("Stamina")
        stam_away = self.away.avg("Stamina")
        stamina_factor = 1.0 + (stam_home + stam_away - 150.0) / 400.0  # 둘 합 150 기준

        # 풀코트 프레스는 pace↑
        for team in (self.home, self.away):
            if team.tactics.get("defense_scheme") == "full_court_press":
                stamina_factor += 0.05

        poss = int(base * pace_factor * stamina_factor)
        return max(80, min(120, poss))

    def _build_minute_shares(self, team: Team) -> Dict[int, float]:
        total_minutes = sum(max(0.0, p.desired_minutes) for p in team.rotation_players)
        if total_minutes <= 0:
            if not team.rotation_players:
                return {}
            uniform = 1.0 / len(team.rotation_players)
            return {p.player_id: uniform for p in team.rotation_players}
        return {p.player_id: max(0.0, p.desired_minutes) / total_minutes for p in team.rotation_players}

    def _pick_scheme(self, team: Team, kind: str) -> str:
        primary_key = f"{kind}_scheme"
        secondary_key = f"{kind}_secondary_scheme"
        primary_default = "pace_space" if kind == "offense" else "drop_coverage"

        primary = team.tactics.get(primary_key, primary_default)
        secondary = team.tactics.get(secondary_key)
        if not secondary or secondary in ("none", ""):
            return primary

        prim_w = max(0.0, float(team.tactics.get(f"{kind}_primary_weight", 10.0)))
        sec_w = max(0.0, float(team.tactics.get(f"{kind}_secondary_weight", 0.0)))
        if prim_w < sec_w:
            prim_w = sec_w

        total = prim_w + sec_w
        if total <= 0:
            return primary

        r = self.rng.random() * total
        return primary if r < prim_w else secondary

    # -----------------------------
    # 포제션 1개 시뮬
    # -----------------------------
    def _simulate_possession(self, offense: Team, defense: Team) -> Team:
        off_scheme = self._pick_scheme(offense, "offense")
        def_scheme = self._pick_scheme(defense, "defense")

        # 1) 초기 턴오버 (프레스, 트랩, 핸들링)
        if self._maybe_early_turnover(offense, defense, def_scheme):
            return defense

        # 2) 플레이 타입 선택 (iso / pnr / post / drive_kick / motion / generic)
        play_type = self._pick_play_type(offense, defense, off_scheme)

        # 3) 주 공격수(에이스, 볼 핸들러, 롤맨 등) 선택
        shooter, secondary = self._pick_actors(offense, play_type, off_scheme)

        # 4) 샷 타입 선별 (rim/mid/three) + 성공 여부 + 파울 여부
        shot_type = self._pick_shot_type(offense, defense, play_type, off_scheme, def_scheme)
        made, is_three, foul_drawn, ft_count = self._resolve_shot(
            offense, defense, shooter, secondary, play_type, shot_type, def_scheme
        )

        # 5) 자유투
        if ft_count > 0 and foul_drawn:
            self._simulate_free_throws(shooter, ft_count)

        # 6) 득점/리바운드/어시스트
        if made:
            pts = 3 if is_three else 2
            shooter.inc("PTS", pts)
            shooter.inc("FGM", 1)
            if is_three:
                shooter.inc("3PM", 1)
        shooter.inc("FGA", 1)
        if is_three:
            shooter.inc("3PA", 1)

        if made:
            self._maybe_assist(offense, defense, shooter, play_type, off_scheme)
            return defense
        if foul_drawn:
            return defense

        reb_team = self._resolve_rebound(offense, defense, def_scheme)
        return reb_team

    # -----------------------------
    # 초기 턴오버 (프레스/트랩 등)
    # -----------------------------
    def _maybe_early_turnover(self, offense: Team, defense: Team, def_scheme: str) -> bool:
        off_pm = offense.avg("Playmaking")
        def_def = defense.avg("Defense")

        base_tov = 0.11  # 기본 11%
        pm_factor = (off_pm - 75.0) / 250.0
        def_factor = (def_def - 75.0) / 250.0

        tov_prob = base_tov - pm_factor + def_factor

        # 수비 전술 효과
        if def_scheme == "full_court_press":
            tov_prob += 0.06  # 프레스면 턴오버↑
        elif def_scheme == "blitz_pnr":
            tov_prob += 0.03  # 적극적인 트랩
        elif def_scheme == "switch_all":
            tov_prob += 0.0
        elif def_scheme == "zone_2_3":
            tov_prob -= 0.01  # 온볼 압박은 덜 하니까

        tov_prob = max(0.05, min(0.25, tov_prob))

        if self.rng.random() < tov_prob:
            # 스틸 or 헛패스
            def _weighted_pick(players: List[Player], weights: List[float]) -> Player:
                total_w = sum(weights)
                r = self.rng.random() * total_w
                acc = 0.0
                for pl, w in zip(players, weights):
                    acc += w
                    if r <= acc:
                        return pl
                return players[-1]

            bh_weights = []
            for p in offense.rotation_players:
                handle = p.ratings.get("Ball Handle", 70.0)
                w = p.usage_weight * max(0.0, 110.0 - handle)
                bh_weights.append(max(0.1, w))

            stl_weights = []
            for p in defense.rotation_players:
                rating = p.ratings.get("Steal", 70.0) * 0.7 + p.ratings.get("Perimeter Defense", 70.0) * 0.3
                stl_weights.append(max(0.1, rating))

            ballhandler = _weighted_pick(offense.rotation_players, bh_weights)
            stealer = _weighted_pick(defense.rotation_players, stl_weights)

            stealer.inc("STL", 1)
            ballhandler.inc("TOV", 1)
            return True
        return False

    # -----------------------------
    # 플레이 타입 선택
    # -----------------------------
    def _pick_play_type(self, offense: Team, defense: Team, scheme: str) -> str:
        # 기본 가중치
        w = {
            "iso": 0.10,
            "pnr": 0.25,
            "post": 0.10,
            "drive_kick": 0.20,
            "motion": 0.20,
            "generic": 0.15,
        }
        if scheme == "pace_space":
            w.update({
                "drive_kick": 0.30,
                "motion": 0.25,
                "pnr": 0.20,
                "iso": 0.05,
                "post": 0.05,
                "generic": 0.15,
            })
        elif scheme == "five_out_motion":
            w.update({
                "motion": 0.35,
                "drive_kick": 0.25,
                "pnr": 0.20,
                "iso": 0.05,
                "post": 0.05,
                "generic": 0.10,
            })
        elif scheme == "pnr_heavy":
            w.update({
                "pnr": 0.45,
                "drive_kick": 0.15,
                "motion": 0.15,
                "post": 0.10,
                "iso": 0.10,
                "generic": 0.05,
            })
        elif scheme == "post_up_focus":
            w.update({
                "post": 0.40,
                "drive_kick": 0.10,
                "pnr": 0.15,
                "motion": 0.10,
                "iso": 0.15,
                "generic": 0.10,
            })
        elif scheme == "iso_heavy":
            w.update({
                "iso": 0.40,
                "pnr": 0.15,
                "drive_kick": 0.15,
                "post": 0.10,
                "motion": 0.10,
                "generic": 0.10,
            })
        elif scheme == "drive_kick":
            w.update({
                "drive_kick": 0.40,
                "pnr": 0.20,
                "motion": 0.15,
                "iso": 0.10,
                "post": 0.05,
                "generic": 0.10,
            })

        total = sum(w.values())
        r = self.rng.random() * total
        acc = 0.0
        for k, val in w.items():
            acc += val
            if r <= acc:
                return k
        return "generic"

    # -----------------------------
    # 공격수 / 세컨더리 액터 선택
    # -----------------------------
    def _pick_actors(self, offense: Team, play_type: str, scheme: str) -> (Player, Optional[Player]):
        players = offense.rotation_players

        # usage 기반 기본 가중치
        weights = []
        for p in players:
            w = p.usage_weight

            if play_type == "pnr" or play_type == "drive_kick":
                # 볼 핸들러 우선: Ball Handle + Speed with Ball
                bh = p.ratings.get("Ball Handle", 70.0)
                swb = p.ratings.get("Speed with Ball", 70.0)
                w *= 1.0 + (bh + swb - 140.0) / 200.0
            elif play_type == "post":
                # 포스트 옵션: Post Control + Strength
                pc = p.ratings.get("Post Control", 70.0)
                st = p.ratings.get("Strength", 70.0)
                w *= 1.0 + (pc + st - 140.0) / 200.0
            elif play_type == "iso":
                # 에이스 중심: Outside/Inside Scoring + Shot IQ
                out = p.ratings.get("Outside Scoring", 70.0)
                ins = p.ratings.get("Inside Scoring", 70.0)
                iq = p.ratings.get("Shot IQ", 70.0)
                score = max(out, ins) + iq
                w *= 1.0 + (score - 150.0) / 200.0

            weights.append(max(0.1, w))

        total = sum(weights)
        r = self.rng.random() * total
        acc = 0.0
        shooter = players[0]
        for p, w in zip(players, weights):
            acc += w
            if r <= acc:
                shooter = p
                break

        secondary: Optional[Player] = None

        if play_type == "pnr":
            # 롤맨 선택
            weights2 = []
            for p in players:
                if p is shooter:
                    weights2.append(0.0)
                    continue
                dd = p.ratings.get("Driving Dunk", 70.0)
                sd = p.ratings.get("Standing Dunk", 70.0)
                hands = p.ratings.get("Hands", 70.0)
                st = p.ratings.get("Strength", 70.0)
                w2 = max(dd, sd) * 0.5 + hands * 0.3 + st * 0.2
                weights2.append(max(0.1, w2))
            total2 = sum(weights2)
            if total2 > 0:
                r2 = self.rng.random() * total2
                acc2 = 0.0
                for p, w2 in zip(players, weights2):
                    acc2 += w2
                    if r2 <= acc2:
                        secondary = p
                        break

        return shooter, secondary

    # -----------------------------
    # 샷 타입 선택 (rim/mid/three)
    # -----------------------------
    def _pick_shot_type(self, offense: Team, defense: Team, play_type: str, off_scheme: str, def_scheme: str) -> str:

        # 기본 분포
        dist = {"rim": 0.35, "mid": 0.25, "three": 0.40}

        # 공격 전술 영향
        if off_scheme == "pace_space":
            dist["three"] += 0.10
            dist["mid"] -= 0.05
            dist["rim"] -= 0.05
        elif off_scheme == "five_out_motion":
            dist["three"] += 0.08
            dist["rim"] += 0.05
            dist["mid"] -= 0.13
        elif off_scheme == "pnr_heavy":
            if play_type == "pnr":
                # 롤맨 림, 핸들러 풀업
                dist["rim"] += 0.10
                dist["mid"] += 0.05
                dist["three"] -= 0.15
        elif off_scheme == "post_up_focus":
            dist["rim"] += 0.10
            dist["mid"] += 0.10
            dist["three"] -= 0.20
        elif off_scheme == "iso_heavy":
            dist["mid"] += 0.05
            dist["rim"] += 0.05
            dist["three"] -= 0.10
        elif off_scheme == "drive_kick":
            dist["rim"] += 0.10
            dist["three"] += 0.05
            dist["mid"] -= 0.15

        # 수비 전술 영향
        if def_scheme == "drop_coverage":
            dist["rim"] -= 0.05
            dist["mid"] += 0.05
        elif def_scheme == "switch_all":
            dist["three"] -= 0.05
            dist["rim"] += 0.05
        elif def_scheme == "zone_2_3":
            dist["rim"] -= 0.08
            dist["post"] = dist.get("post", 0.0) - 0.05
            dist["three"] += 0.13
        elif def_scheme == "full_court_press":
            # 트랜지션에서 림/3 둘 다 늘어나는 느낌
            dist["rim"] += 0.05
            dist["three"] += 0.05
            dist["mid"] -= 0.10

        # 정규화
        for k, v in list(dist.items()):
            dist[k] = max(0.0, v)
        total = sum(dist.values())
        if total <= 0:
            dist = {"rim": 0.4, "mid": 0.2, "three": 0.4}
            total = 1.0

        r = self.rng.random() * total
        acc = 0.0
        for k, v in dist.items():
            acc += v
            if r <= acc:
                return k
        return "rim"

    # -----------------------------
    # 샷 성공/파울 판정
    # -----------------------------
    def _resolve_shot(
        self,
        offense: Team,
        defense: Team,
        shooter: Player,
        secondary: Optional[Player],
        play_type: str,
        shot_type: str,
        def_scheme: str,
    ) -> (bool, bool, bool, int):

        # 공격 레이팅
        if shot_type == "three":
            att = shooter.ratings.get("Three-Point Shot", 70.0)
            def_rating = defense.avg("Perimeter Defense")
        elif shot_type == "mid":
            att = shooter.ratings.get("Mid-Range Shot", 70.0)
            def_rating = defense.avg("Perimeter Defense")
        else:  # rim
            att = max(
                shooter.ratings.get("Layup", 70.0),
                shooter.ratings.get("Driving Dunk", 70.0),
                shooter.ratings.get("Close Shot", 70.0),
            )
            def_rating = defense.avg("Interior Defense")

        # 플레이 타입 보정 (PnR, Post, Iso, Drive&Kick 등)
        if play_type == "pnr" and secondary is not None:
            # 롤맨 점유일 가능성이 높다고 보고, 롤맨의 Inside Scoring을 살짝 추가
            roll_ins = secondary.ratings.get("Inside Scoring", 70.0)
            att = (att * 0.6 + roll_ins * 0.4)
        elif play_type == "post":
            post_skill = shooter.ratings.get("Post Control", 70.0)
            hook = shooter.ratings.get("Post Hook", 70.0)
            fade = shooter.ratings.get("Post Fade", 70.0)
            att = (att * 0.3 + post_skill * 0.4 + max(hook, fade) * 0.3)
        elif play_type == "iso":
            iq = shooter.ratings.get("Shot IQ", 70.0)
            att += (iq - 70.0) * 0.5

        # 수비 전술 보정
        if def_scheme == "drop_coverage":
            if shot_type == "rim":
                def_rating += 6
            elif shot_type == "mid":
                def_rating -= 3
        elif def_scheme == "switch_all":
            if shot_type == "three":
                def_rating += 5
        elif def_scheme == "zone_2_3":
            if shot_type == "rim":
                def_rating += 6
            elif shot_type == "three":
                def_rating -= 4
        elif def_scheme == "hedge_recover":
            if play_type == "pnr" and shot_type in ("mid", "three"):
                def_rating += 4
        elif def_scheme == "blitz_pnr":
            if play_type == "pnr":
                def_rating += 3  # 온볼 압박

        # 피지컬/피로 보정
        off_ath = shooter.ratings.get("Athleticism", 75.0)
        def_ath = defense.avg("Athleticism")
        ath_delta = (off_ath - def_ath) / 20.0

        off_fat = offense.tactics.get("fatigue_factor", 1.0)
        def_fat = defense.tactics.get("fatigue_factor", 1.0)

        rating_diff = att - def_rating
        base_prob = 0.45 + rating_diff / 150.0
        base_prob += ath_delta * 0.05
        base_prob += (off_fat - 1.0) * 0.08
        base_prob -= (def_fat - 1.0) * 0.05

        # 샷 타입 고유 난이도
        if shot_type == "three":
            base_prob -= 0.08
        elif shot_type == "mid":
            base_prob -= 0.03
        elif shot_type == "rim":
            base_prob += 0.05

        prob = max(0.20, min(0.80, base_prob))

        # 파울 유도 확률
        draw = shooter.ratings.get("Draw Foul", 70.0)
        hands = defense.avg("Hands")
        def_agg = 1
        if def_scheme in ("full_court_press", "blitz_pnr", "hedge_recover"):
            def_agg = 2

        foul_base = 0.10 + (draw - 70.0) / 350.0 + (def_agg - 1) * 0.03
        foul_base -= (hands - 70.0) / 400.0

        # 드라이브 기반 전술/플레이면 파울 조금↑
        if play_type in ("drive_kick", "iso") and shot_type == "rim":
            foul_base += 0.03

        foul_prob = max(0.05, min(0.30, foul_base))

        foul_drawn = self.rng.random() < foul_prob
        made = self.rng.random() < prob

        if foul_drawn and defense.rotation_players:
            weights = []
            for p in defense.rotation_players:
                rating = (
                    p.ratings.get("Perimeter Defense", 70.0) * 0.4
                    + p.ratings.get("Defense", 70.0) * 0.4
                    + p.ratings.get("Steal", 70.0) * 0.2
                )
                weights.append(max(0.1, rating))

            total = sum(weights)
            r = self.rng.random() * total
            acc = 0.0
            for p, w in zip(defense.rotation_players, weights):
                acc += w
                if r <= acc:
                    p.inc("PF", 1)
                    break

        is_three = (shot_type == "three")
        ft_count = 0
        if foul_drawn:
            if made:
                ft_count = 1
            else:
                ft_count = 3 if is_three else 2

        return made, is_three, foul_drawn, ft_count

    # -----------------------------
    # 자유투
    # -----------------------------
    def _simulate_free_throws(self, shooter: Player, n: int) -> None:
        ft = shooter.ratings.get("Free Throw", 75.0)
        prob = 0.75 + (ft - 75.0) / 200.0
        prob = max(0.55, min(0.95, prob))
        for _ in range(n):
            shooter.inc("FTA", 1)
            if self.rng.random() < prob:
                shooter.inc("FTM", 1)
                shooter.inc("PTS", 1)

    # -----------------------------
    # 리바운드
    # -----------------------------
    def _resolve_rebound(self, offense: Team, defense: Team, def_scheme: str) -> Team:
        off_reb = offense.avg("Offensive Rebound")
        def_reb = defense.avg("Defensive Rebound")

        # 기본: 수비 75%
        base_def_share = 0.75 + (def_reb - off_reb) / 400.0

        # 수비 전술 보정
        if def_scheme in ("drop_coverage", "zone_2_3"):
            base_def_share += 0.03  # 빅이 안쪽에 있음
        elif def_scheme == "switch_all":
            base_def_share -= 0.02  # 스몰볼 라인업이 많다고 가정

        base_def_share = max(0.60, min(0.90, base_def_share))

        if self.rng.random() < base_def_share:
            reb_team = defense
            key = "DEF"
        else:
            reb_team = offense
            key = "OFF"

        # 누가 잡는가
        weights = []
        for p in reb_team.rotation_players:
            if key == "OFF":
                r = p.ratings.get("Offensive Rebound", 70.0)
            else:
                r = p.ratings.get("Defensive Rebound", 70.0)
            r += p.ratings.get("Vertical", 70.0) * 0.3
            r += p.ratings.get("Hustle", 70.0) * 0.2
            weights.append(max(1.0, r))

        total = sum(weights)
        r = self.rng.random() * total
        acc = 0.0
        for p, w in zip(reb_team.rotation_players, weights):
            acc += w
            if r <= acc:
                p.inc("REB", 1)
                break

        return reb_team

    # -----------------------------
    # 어시스트
    # -----------------------------
    def _maybe_assist(self, offense: Team, defense: Team, shooter: Player, play_type: str, scheme: str) -> None:
        pm_team = offense.avg("Playmaking")
        help_def = defense.avg("Help Defense IQ")

        base_ast = 0.55 + (pm_team - help_def) / 300.0

        # 전술 보정
        if scheme in ("pace_space", "five_out_motion", "drive_kick", "pnr_heavy"):
            base_ast += 0.08
        elif scheme == "iso_heavy":
            base_ast -= 0.15
        elif scheme == "post_up_focus":
            base_ast -= 0.05

        base_ast = max(0.15, min(0.85, base_ast))

        if self.rng.random() > base_ast:
            return

        candidates = [p for p in offense.rotation_players if p is not shooter]
        if not candidates:
            return

        weights = []
        for p in candidates:
            pa = p.ratings.get("Pass Accuracy", 70.0)
            pv = p.ratings.get("Pass Vision", 70.0)
            piq = p.ratings.get("Pass IQ", 70.0)
            w = pa * 0.5 + pv * 0.3 + piq * 0.2
            weights.append(max(1.0, w))

        total = sum(weights)
        r = self.rng.random() * total
        acc = 0.0
        for p, w in zip(candidates, weights):
            acc += w
            if r <= acc:
                p.inc("AST", 1)
                break

    # -----------------------------
    # 박스스코어 포맷
    # -----------------------------
    def _box_row(self, p: Player) -> Dict[str, Any]:
        s = p.stats
        return {
            "PlayerID": p.player_id,
            "Name": p.name,
            "Team": p.team_id,
            "MIN": round(s.get("MIN", 0.0), 1),
            "PTS": round(s.get("PTS", 0.0), 1),
            "REB": round(s.get("REB", 0.0), 1),
            "AST": round(s.get("AST", 0.0), 1),
            "STL": round(s.get("STL", 0.0), 1),
            "BLK": round(s.get("BLK", 0.0), 1),
            "TOV": round(s.get("TOV", 0.0), 1),

            "FGM": int(s.get("FGM", 0.0)),
            "FGA": int(s.get("FGA", 0.0)),
            "3PM": int(s.get("3PM", 0.0)),
            "3PA": int(s.get("3PA", 0.0)),
            "FTM": int(s.get("FTM", 0.0)),
            "FTA": int(s.get("FTA", 0.0)),
            "PF": int(s.get("PF", 0.0)),
        }
