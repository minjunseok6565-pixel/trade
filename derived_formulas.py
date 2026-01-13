"""derived_formulas.py

Shared derived-ability formulas used by both roster_adapter and team_utils.

- Input: a pandas Series (a row from the roster dataframe)
- Output: dict[str, float] with values clamped to [0, 100]
"""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

COL = {
    "CloseShot":"Close Shot",
    "MidRange":"Mid-Range Shot",
    "ThreePoint":"Three-Point Shot",
    "FreeThrow":"Free Throw",
    "ShotIQ":"Shot IQ",
    "OffCons":"Offensive Consistency",
    "Layup":"Layup",
    "StandingDunk":"Standing Dunk",
    "DrivingDunk":"Driving Dunk",
    "PostHook":"Post Hook",
    "PostFade":"Post Fade",
    "PostControl":"Post Control",
    "DrawFoul":"Draw Foul",
    "Hands":"Hands",
    "PassAccuracy":"Pass Accuracy",
    "BallHandle":"Ball Handle",
    "SpeedWithBall":"Speed with Ball",
    "PassIQ":"Pass IQ",
    "PassVision":"Pass Vision",
    "InteriorDef":"Interior Defense",
    "PerimeterDef":"Perimeter Defense",
    "Steal":"Steal",
    "Block":"Block",
    "HelpIQ":"Help Defense IQ",
    "PassPerception":"Pass Perception",
    "DefCons":"Defensive Consistency",
    "OffReb":"Offensive Rebound",
    "DefReb":"Defensive Rebound",
    "Speed":"Speed",
    "Agility":"Agility",
    "Strength":"Strength",
    "Vertical":"Vertical",
    "Stamina":"Stamina",
    "Hustle":"Hustle",
    "Durability":"Overall Durability",
}

def _get(row, key: str, default: float = 50.0) -> float:
    c = COL.get(key)
    if c and c in row and pd.notna(row[c]):
        try:
            return float(row[c])
        except Exception:
            return default
    return default

def _clamp100(x: float) -> float:
    return float(np.clip(x, 0, 100))

def compute_derived(row) -> Dict[str, float]:
    FIN_RIM = 0.35*_get(row,"Layup")+0.20*_get(row,"CloseShot")+0.15*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Strength")+0.10*_get(row,"Vertical")
    FIN_DUNK = 0.30*_get(row,"DrivingDunk")+0.25*_get(row,"StandingDunk")+0.15*_get(row,"Vertical")+0.15*_get(row,"Strength")+0.10*_get(row,"Hands")+0.05*_get(row,"OffCons")
    FIN_CONTACT = 0.35*_get(row,"Strength")+0.25*_get(row,"Vertical")+0.15*_get(row,"DrivingDunk")+0.10*_get(row,"Layup")+0.10*_get(row,"DrawFoul")+0.05*_get(row,"Durability")

    SHOT_MID_CS = 0.45*_get(row,"MidRange")+0.20*_get(row,"CloseShot")+0.15*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Hands")
    SHOT_3_CS = 0.55*_get(row,"ThreePoint")+0.15*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Hands")+0.10*_get(row,"PassVision")
    SHOT_FT = 0.70*_get(row,"FreeThrow")+0.15*_get(row,"ShotIQ")+0.15*_get(row,"OffCons")

    SHOT_MID_PU = 0.40*_get(row,"MidRange")+0.20*_get(row,"BallHandle")+0.15*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Agility")+0.05*_get(row,"SpeedWithBall")
    SHOT_3_OD = 0.50*_get(row,"ThreePoint")+0.20*_get(row,"BallHandle")+0.15*_get(row,"Agility")+0.10*_get(row,"SpeedWithBall")+0.10*_get(row,"ShotIQ")+0.05*_get(row,"OffCons")
    SHOT_TOUCH = 0.30*_get(row,"CloseShot")+0.20*_get(row,"ShotIQ")+0.20*_get(row,"FreeThrow")+0.15*_get(row,"Hands")+0.15*_get(row,"OffCons")+0.15*_get(row,"Layup")

    POST_SCORE = 0.25*_get(row,"PostHook")+0.25*_get(row,"PostFade")+0.20*_get(row,"PostControl")+0.10*_get(row,"CloseShot")+0.10*_get(row,"Strength")+0.10*_get(row,"Hands")
    POST_CONTROL = 0.40*_get(row,"PostControl")+0.20*_get(row,"Strength")+0.15*_get(row,"Hands")+0.15*_get(row,"OffCons")+0.10*_get(row,"ShotIQ")
    SEAL_POWER = 0.40*_get(row,"Strength")+0.20*_get(row,"PostControl")+0.15*_get(row,"CloseShot")+0.15*_get(row,"Hustle")+0.10*_get(row,"Hands")

    DRIVE_CREATE = 0.30*_get(row,"SpeedWithBall")+0.25*_get(row,"BallHandle")+0.15*_get(row,"Agility")+0.10*_get(row,"Layup")+0.10*_get(row,"ShotIQ")+0.10*_get(row,"OffCons")+0.10*_get(row,"Strength")
    HANDLE_SAFE = 0.45*_get(row,"BallHandle")+0.20*_get(row,"Hands")+0.15*_get(row,"Agility")+0.10*_get(row,"Strength")+0.10*_get(row,"OffCons")+0.10*_get(row,"PassIQ")
    FIRST_STEP = 0.35*_get(row,"Speed")+0.25*_get(row,"Agility")+0.15*_get(row,"SpeedWithBall")+0.15*_get(row,"Vertical")+0.10*_get(row,"BallHandle")+0.10*_get(row,"Stamina")

    PASS_SAFE = 0.35*_get(row,"PassAccuracy")+0.25*_get(row,"PassIQ")+0.20*_get(row,"Hands")+0.20*_get(row,"PassVision")
    PASS_CREATE = 0.30*_get(row,"PassVision")+0.25*_get(row,"PassAccuracy")+0.20*_get(row,"PassIQ")+0.10*_get(row,"BallHandle")+0.10*_get(row,"ShotIQ")
    PNR_READ = 0.35*_get(row,"PassIQ")+0.25*_get(row,"ShotIQ")+0.20*_get(row,"PassVision")+0.10*_get(row,"BallHandle")+0.10*_get(row,"OffCons")
    SHORTROLL_PLAY = 0.35*_get(row,"PassIQ")+0.25*_get(row,"PassAccuracy")+0.20*_get(row,"Hands")+0.10*_get(row,"PassVision")+0.10*_get(row,"CloseShot")

    DEF_POA = 0.40*_get(row,"PerimeterDef")+0.20*_get(row,"Agility")+0.15*_get(row,"Speed")+0.10*_get(row,"Steal")+0.10*_get(row,"HelpIQ")+0.05*_get(row,"DefCons")
    DEF_HELP = 0.35*_get(row,"HelpIQ")+0.20*_get(row,"InteriorDef")+0.15*_get(row,"PerimeterDef")+0.10*_get(row,"PassPerception")+0.10*_get(row,"DefCons")+0.10*_get(row,"Hustle")
    DEF_STEAL = 0.45*_get(row,"Steal")+0.20*_get(row,"PassPerception")+0.15*_get(row,"PerimeterDef")+0.10*_get(row,"Agility")+0.10*_get(row,"DefCons")
    DEF_RIM = 0.40*_get(row,"Block")+0.20*_get(row,"InteriorDef")+0.15*_get(row,"Vertical")+0.10*_get(row,"Strength")+0.10*_get(row,"HelpIQ")+0.05*_get(row,"DefCons")
    DEF_POST = 0.40*_get(row,"InteriorDef")+0.25*_get(row,"Strength")+0.15*_get(row,"Block")+0.10*_get(row,"PostControl")+0.10*_get(row,"DefCons")

    REB_OR = 0.45*_get(row,"OffReb")+0.20*_get(row,"Vertical")+0.15*_get(row,"Hustle")+0.10*_get(row,"Strength")+0.10*_get(row,"Hands")
    REB_DR = 0.50*_get(row,"DefReb")+0.15*_get(row,"Vertical")+0.15*_get(row,"Hustle")+0.10*_get(row,"Strength")+0.10*_get(row,"Hands")

    PHYSICAL = 0.45*_get(row,"Strength")+0.20*_get(row,"Durability")+0.20*_get(row,"Hustle")+0.15*_get(row,"Stamina")
    ENDURANCE = 0.55*_get(row,"Stamina")+0.25*_get(row,"Durability")+0.20*_get(row,"Hustle")
    FAT_CAPACITY = _get(row,"Stamina")  # stamina(0-100) == FAT_CAPACITY; engine normalizes (/100) internally

    out = dict(
        FIN_RIM=FIN_RIM, FIN_DUNK=FIN_DUNK, FIN_CONTACT=FIN_CONTACT,
        SHOT_MID_CS=SHOT_MID_CS, SHOT_3_CS=SHOT_3_CS, SHOT_FT=SHOT_FT,
        SHOT_MID_PU=SHOT_MID_PU, SHOT_3_OD=SHOT_3_OD, SHOT_TOUCH=SHOT_TOUCH,
        POST_SCORE=POST_SCORE, POST_CONTROL=POST_CONTROL, SEAL_POWER=SEAL_POWER,
        DRIVE_CREATE=DRIVE_CREATE, HANDLE_SAFE=HANDLE_SAFE, FIRST_STEP=FIRST_STEP,
        PASS_SAFE=PASS_SAFE, PASS_CREATE=PASS_CREATE, PNR_READ=PNR_READ, SHORTROLL_PLAY=SHORTROLL_PLAY,
        DEF_POA=DEF_POA, DEF_HELP=DEF_HELP, DEF_STEAL=DEF_STEAL, DEF_RIM=DEF_RIM, DEF_POST=DEF_POST,
        REB_OR=REB_OR, REB_DR=REB_DR, PHYSICAL=PHYSICAL, ENDURANCE=ENDURANCE, FAT_CAPACITY=FAT_CAPACITY,
    )
    return {k: _clamp100(v) for k, v in out.items()}
