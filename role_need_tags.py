from __future__ import annotations

from typing import Dict, Final, Tuple

NeedTag = str
RoleName = str
NeedLabelKo = str

"""
SSOT for mapping role-fit role names -> (need_tag, Korean label).

- team_situation.py: needs 산출 시 role gap -> need_tag로 변환할 때 사용
- team_utility.py: player supply(역할 기반) 태그 생성 시 사용

IMPORTANT:
- 이 모듈은 "매핑 표준"만 제공한다.
- 팀 상황(니즈 생성), 가치 평가(유틸리티/가격화) 로직은 여기로 들어오면 안 된다.
"""


# Canonical 12 roles (role_fit_data.ROLE_FIT_WEIGHTS) + legacy aliases
ROLE_TO_NEED_TAG_AND_LABEL: Final[Dict[RoleName, Tuple[NeedTag, NeedLabelKo]]] = {
    # Canonical 12 roles (match role_fit_data.py)
    "Initiator_Primary": ("PRIMARY_INITIATOR", "1옵션 볼핸들러"),
    "Initiator_Secondary": ("SECONDARY_CREATOR", "세컨더리 크리에이터"),
    "Transition_Handler": ("TRANSITION_ENGINE", "트랜지션 핸들러"),
    "Shot_Creator": ("SHOT_CREATION", "샷 크리에이터"),
    "Rim_Attacker": ("RIM_PRESSURE", "림 어택/드라이브 자원"),
    "Spacer_CatchShoot": ("SPACING", "캐치&슛 스페이서"),
    "Spacer_Movement": ("MOVEMENT_SHOOTING", "무브먼트 슈터"),
    "Connector_Playmaker": ("CONNECTOR_PLAY", "커넥터 플레이메이커"),
    "Roller_Finisher": ("ROLL_THREAT", "롤/림런 피니셔"),
    "ShortRoll_Playmaker": ("SHORT_ROLL_PLAY", "숏롤 플레이메이커"),
    "Pop_Spacer_Big": ("POP_BIG", "팝 스페이서 빅"),
    "Post_Hub": ("POST_HUB", "포스트 허브"),
    # Legacy/alias safety
    "Spotup_Shooter": ("SPACING", "스팟업 슈터"),
    "Movement_Shooter": ("MOVEMENT_SHOOTING", "오프볼 슈터"),
    "Cutter_Slasher": ("RIM_PRESSURE", "림 어택/컷터"),
    "Roller": ("ROLL_THREAT", "롤 위협"),
    "ShortRoll": ("SHORT_ROLL_PLAY", "숏롤"),
    "Post_Anchor": ("POST_HUB", "포스트 옵션"),
}

# Convenience: role -> tag only
ROLE_TO_NEED_TAG: Final[Dict[RoleName, NeedTag]] = {
    role: tag_label[0] for role, tag_label in ROLE_TO_NEED_TAG_AND_LABEL.items()
}


def role_to_need_tag(role: str) -> Tuple[NeedTag, NeedLabelKo]:
    """
    Map a role-fit role name to a stable need tag + Korean label.

    This is intentionally identical to team_situation._role_to_need_tag() behavior.
    """
    if role in ROLE_TO_NEED_TAG_AND_LABEL:
        return ROLE_TO_NEED_TAG_AND_LABEL[role]

    # fallback heuristic (safety)
    if "Def" in role or "def" in role:
        return ("DEFENSE", "수비 자원")
    if "Shooter" in role:
        return ("SPACING", "슈팅")
    if "Rim" in role or "Roll" in role:
        return ("RIM_PRESSURE", "림 근처 위협")
    return ("ROLE_GAP", "역할")


def role_to_need_tag_only(role: str) -> NeedTag:
    """Convenience wrapper when you only need the tag."""
    return role_to_need_tag(role)[0]
