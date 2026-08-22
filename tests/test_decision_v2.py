"""V2判定の語彙（DecisionStatus・SeatAction・CreditAction・ReasonCode）のテスト。

値は decision-evidence.csv にそのまま書かれ、月をまたいだ比較・実変更との照合の
突き合わせキーになる。名前と値の完全な集合をリテラルで固定し、増減・改名・値の
変更がすべてここで落ちるようにする。
"""

import json

from seat_analyzer.domain import (
    CreditAction,
    DecisionStatus,
    IssueCode,
    ReasonCode,
    SeatAction,
)

_ALL_VOCABULARIES = (DecisionStatus, SeatAction, CreditAction, ReasonCode)


def test_decision_status_names_and_values_are_stable():
    assert {member.name: member.value for member in DecisionStatus} == {
        "RECOMMENDED": "recommended",
        "OBSERVE": "observe",
        "NO_DECISION": "no_decision",
        "KEEP": "keep",
        "EXCLUDED": "excluded",
    }


def test_seat_action_names_and_values_are_stable():
    assert {member.name: member.value for member in SeatAction} == {
        "KEEP": "keep",
        "UPGRADE_TO_PREMIUM": "upgrade_to_premium",
        "DOWNGRADE_TO_STANDARD": "downgrade_to_standard",
        "REVIEW_ASSIGNMENT": "review_assignment",
        "NONE": "none",
    }


def test_credit_action_names_and_values_are_stable():
    assert {member.name: member.value for member in CreditAction} == {
        "KEEP": "keep",
        "ENABLE_WITH_CAP": "enable_with_cap",
        "REVIEW": "review",
        "NONE": "none",
    }


def test_reason_code_set_is_stable_in_design_order():
    """§12.2 の一覧と同じ顔ぶれ・同じ順序であること。値は名前と同一。"""
    assert [member.name for member in ReasonCode] == [
        "ONE_MONTH_STRONG_CODE_DEMAND",
        "SUSTAINED_LOW_CODE_DEMAND",
        "SUSTAINED_LOW_TOTAL_DEMAND",
        "SUSTAINED_OVERAGE",
        "CREDIT_LIMIT_REACHED",
        "CREDIT_SETTING_UNKNOWN",
        "PREMIUM_CHEAPER_THAN_STANDARD_WITH_CREDIT",
        "STANDARD_WITH_CREDIT_CHEAPER",
        "HIGH_SUPPLEMENTARY_USAGE",
        "REVIEW_NON_CODE_USAGE",
        "RECENT_MEMBER",
        "RECENT_SEAT_CHANGE",
        "PARTIAL_MONTH",
        "INSUFFICIENT_HISTORY",
        "IDENTITY_CONFLICT",
        "CAPACITY_SIGNAL_UNAVAILABLE",
        "DATA_CONFIDENCE_LOW",
    ]
    assert all(member.value == member.name for member in ReasonCode)


def test_values_serialize_as_plain_strings():
    """StrEnum の値は文字列としてそのまま直列化される（CSV・JSON に書ける形）。"""
    assert json.dumps(DecisionStatus.OBSERVE) == '"observe"'
    assert json.dumps(ReasonCode.RECENT_SEAT_CHANGE) == '"RECENT_SEAT_CHANGE"'
    assert str(SeatAction.UPGRADE_TO_PREMIUM) == "upgrade_to_premium"
    assert f"{CreditAction.ENABLE_WITH_CAP}" == "enable_with_cap"


def test_values_round_trip_from_serialized_form():
    """直列化した文字列から同じメンバーへ戻れる（照合の突き合わせが成立する）。"""
    for vocabulary in _ALL_VOCABULARIES:
        for member in vocabulary:
            assert vocabulary(member.value) is member
            assert vocabulary(str(member)) is member


def test_vocabularies_do_not_share_members_by_identity():
    """4つの語彙は別の型で、同名でも同一メンバーにはならない。

    IssueCode と ReasonCode の同名メンバー（RECENT_SEAT_CHANGE 等）は意図的な
    重なりで、入力品質と判定根拠という別の文脈を表す。重なり自体が消えると
    片方の文脈の記録が突き合わせできなくなるため、存在まで固定する。
    """
    shared = {
        "RECENT_SEAT_CHANGE",
        "CREDIT_SETTING_UNKNOWN",
        "PARTIAL_MONTH",
        "IDENTITY_CONFLICT",
        "CAPACITY_SIGNAL_UNAVAILABLE",
    }
    assert shared <= {member.name for member in ReasonCode}
    assert shared <= {member.name for member in IssueCode}
    assert ReasonCode.RECENT_SEAT_CHANGE is not IssueCode.RECENT_SEAT_CHANGE
    assert ReasonCode.RECENT_SEAT_CHANGE == IssueCode.RECENT_SEAT_CHANGE.value


def test_cross_vocabulary_equality_exists_and_is_not_a_type_guard():
    """StrEnum は文字列として等値になるため、語彙をまたいだ == が成立する。

    この衝突は StrEnum の仕様で、型では混同を防げない（set・dict では同じキーに
    畳まれる）。存在する事実をここで固定し、混同の防止は V2 の値オブジェクト・
    関数境界の isinstance 検証（QualityIssue と同じ流儀）が担う（設計書 §12.1）。
    """
    assert DecisionStatus.KEEP == SeatAction.KEEP == CreditAction.KEEP
    assert len({DecisionStatus.KEEP, SeatAction.KEEP, CreditAction.KEEP}) == 1
    assert ReasonCode.RECENT_SEAT_CHANGE == IssueCode.RECENT_SEAT_CHANGE
    assert not isinstance(IssueCode.RECENT_SEAT_CHANGE, ReasonCode)
