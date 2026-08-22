"""V2判定の語彙と、昇格ルール（Standard→Premium）のテスト。

前半は語彙（DecisionStatus・SeatAction・CreditAction・ReasonCode）。値は
decision-evidence.csv にそのまま書かれ、月をまたいだ比較・実変更との照合の
突き合わせキーになる。名前と値の完全な集合をリテラルで固定し、増減・改名・値の
変更がすべてここで落ちるようにする。

後半は decision_v2.decide_upgrade。判定の分岐と境界を、既定 config の値を前提にした
具体的な金額で固定する（前提そのものは test_default_config_values_the_cases_assume が
固定するので、config を変えた場合はそこが最初に落ちる）。
"""

import copy
import datetime as dt
import json

import pytest

from seat_analyzer import seat_changes
from seat_analyzer.decision_v2 import (
    DecisionV2,
    MonthObservation,
    SubjectHistory,
    decide_upgrade,
)
from seat_analyzer.domain import (
    CreditAction,
    DecisionStatus,
    IssueCode,
    ReasonCode,
    SeatAction,
)
from seat_analyzer.seat_changes import SeatChangeEvent, UnclassifiedObservation

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


# --------------------------------------------------------------- 昇格ルール（Step 15）
#
# 対象月は 2026-07（末日 07-31）。既定の recent_seat_change_days=28 なので、recent 窓は
# 07-03..07-31 になる。金額は既定 config（Standard $25・込み 30/50/75、Premium $125・
# 込み 150/250/375、削減閾値 $20、Code 需要閾値 $100、上限の許容差 $5）を前提にする。

_MONTH = "2026-07"
_PREVIOUS_MONTH = "2026-06"
_MONTH_END = dt.date(2026, 7, 31)
_WINDOW_START = dt.date(2026, 7, 3)


def _month(
    month: str = _MONTH,
    *,
    complete: bool = True,
    total: float = 0.0,
    code: float | None = 0.0,
    billed: float = 0.0,
    supplementary: bool | None = False,
) -> MonthObservation:
    return MonthObservation(
        month=month,
        complete=complete,
        total_demand_usd=total,
        code_demand_usd=code,
        billed_usd=billed,
        supplementary_high=supplementary,
    )


def _subject(
    *months: MonthObservation,
    seat: str = "standard",
    credit: float | None = None,
    conflict: bool = False,
    events: tuple[SeatChangeEvent, ...] = (),
    unclassified: tuple[UnclassifiedObservation, ...] = (),
) -> SubjectHistory:
    return SubjectHistory(
        email="a@example.com",
        current_seat=seat,
        credit_limit_usd=credit,
        identity_conflict=conflict,
        months=months,
        seat_events=events,
        unclassified=unclassified,
    )


def _event(
    changed_after: dt.date,
    changed_before: dt.date,
    *,
    from_seat: str = "standard",
    to_seat: str = "premium",
) -> SeatChangeEvent:
    return SeatChangeEvent(
        subject_id="account:0001",
        email="a@example.com",
        from_seat=from_seat,
        to_seat=to_seat,
        changed_after=changed_after,
        changed_before=changed_before,
        detected_at=changed_before,
        previous_source="members-2026-06-30.csv",
        current_source="members-2026-07-31.csv",
    )


def _observation(
    changed_after: dt.date, changed_before: dt.date
) -> UnclassifiedObservation:
    return UnclassifiedObservation(
        reason=seat_changes.UNKNOWN_SEAT,
        subject_id="account:0001",
        emails=("a@example.com",),
        account_uuids=(),
        user_ids=(),
        changed_after=changed_after,
        changed_before=changed_before,
        detected_at=changed_before,
        previous_source="members-2026-06-30.csv",
        current_source="members-2026-07-31.csv",
    )


# 経済軸の3経路を1つずつ単独で成立させる観測（他の2経路は成立しない）
def _observed_path_month(**overrides) -> MonthObservation:
    """観測経路のみ: 実課金 $120 が Premium の試算を $20 上回る（境界ちょうど）。"""
    return _month(**{"total": 130.0, "code": 120.0, "billed": 120.0, **overrides})


def _credit_path_month(**overrides) -> MonthObservation:
    """上限到達経路のみ: κ=$100 に対し実課金 $95（= κ − 許容差）。"""
    return _month(**{"total": 130.0, "code": 120.0, "billed": 95.0, **overrides})


def _model_path_month(**overrides) -> MonthObservation:
    """純モデル経路のみ: 需要 $170 で low・mid が Premium 有利、mid の削減が $20。"""
    return _month(**{"total": 170.0, "code": 120.0, **overrides})


def test_default_config_values_the_cases_assume(cfg):
    """このファイルの金額が前提にしている既定値。変わればここが最初に落ちる。"""
    assert cfg["seats"]["standard"]["price_usd"] == 25.0
    assert cfg["seats"]["premium"]["price_usd"] == 125.0
    assert cfg["seats"]["standard"]["allowance_usd"] == {
        "low": 30.0, "mid": 50.0, "high": 75.0}
    assert cfg["seats"]["premium"]["allowance_usd"] == {
        "low": 150.0, "mid": 250.0, "high": 375.0}
    assert cfg["decision_v2"]["upgrade"]["min_complete_months"] == 1
    assert cfg["decision_v2"]["upgrade"]["min_code_demand_usd"] == 100.0
    assert cfg["decision_v2"]["min_assignment_saving_usd"] == 20.0
    assert cfg["decision_v2"]["recent_seat_change_days"] == 28
    assert cfg["usage_credits"]["cap_tolerance_usd"] == 5.0


# ------------------------------------------------------------- 判定しない経路（1〜4）


@pytest.mark.parametrize("seat", ["premium", "unassigned", "unknown", ""])
def test_non_standard_seat_is_rejected(cfg, seat):
    """Standard 以外の振り分けは呼び出し側の責務で、既定の結論を持たない。"""
    subject = _subject(_model_path_month(), seat=seat)
    with pytest.raises(ValueError, match="current_seat"):
        decide_upgrade(subject, cfg)


def test_identity_conflict_is_no_decision(cfg):
    subject = _subject(_model_path_month(), conflict=True)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.IDENTITY_CONFLICT,)


def test_partial_target_month_is_no_decision(cfg):
    """対象月が部分月なら、他の月がそろっていても判定しない。"""
    subject = _subject(
        _model_path_month(month=_PREVIOUS_MONTH),
        _model_path_month(complete=False),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.PARTIAL_MONTH,)


def test_insufficient_complete_months_is_no_decision(cfg):
    """必要な完全月数に足りなければ判定しない（部分月は数に入らない）。"""
    strict = copy.deepcopy(cfg)
    strict["decision_v2"]["upgrade"]["min_complete_months"] = 2
    subject = _subject(
        _model_path_month(month=_PREVIOUS_MONTH, complete=False),
        _model_path_month(),
    )
    decision = decide_upgrade(subject, strict)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)


def test_complete_months_need_not_be_the_recent_ones(cfg):
    """完全月が要求数あれば判定へ進む（既定は1完全月で候補化できる）。"""
    subject = _subject(_model_path_month())
    assert decide_upgrade(subject, cfg).status is DecisionStatus.RECOMMENDED


# --------------------------------------------------------------- recent 窓（5）


def test_recent_member_addition_falls_back_to_observe(cfg):
    subject = _subject(
        _model_path_month(),
        events=(_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20),
                       from_seat=seat_changes.ABSENT, to_seat="standard"),),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.RECENT_MEMBER,)


def test_recent_seat_change_falls_back_to_observe(cfg):
    subject = _subject(
        _model_path_month(),
        events=(_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (ReasonCode.RECENT_SEAT_CHANGE,)


def test_unclassified_observation_alone_falls_back_to_observe(cfg):
    """event が無くても、分類できない観測が窓に重なれば保留側へ倒す（§10.4）。"""
    subject = _subject(
        _model_path_month(),
        unclassified=(_observation(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


def test_recent_reasons_are_reported_together_in_fixed_order(cfg):
    subject = _subject(
        _model_path_month(),
        events=(
            _event(dt.date(2026, 7, 10), dt.date(2026, 7, 20)),
            _event(dt.date(2026, 7, 5), dt.date(2026, 7, 8),
                   from_seat=seat_changes.ABSENT, to_seat="standard"),
        ),
        unclassified=(_observation(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    assert decide_upgrade(subject, cfg).reason_codes == (
        ReasonCode.RECENT_MEMBER,
        ReasonCode.RECENT_SEAT_CHANGE,
        ReasonCode.DATA_CONFIDENCE_LOW,
    )


def test_absence_of_events_does_not_hold_the_decision(cfg):
    """スナップショットのペアが無く event を作れない状態は hard blocker にしない。"""
    subject = _subject(_model_path_month())
    assert decide_upgrade(subject, cfg).status is DecisionStatus.RECOMMENDED


@pytest.mark.parametrize(
    ("changed_after", "changed_before", "recent"),
    [
        (dt.date(2026, 6, 1), _WINDOW_START, True),                 # 窓の始端に接する
        (dt.date(2026, 6, 1), _WINDOW_START - dt.timedelta(1), False),  # その前日
        (_MONTH_END - dt.timedelta(1), dt.date(2026, 8, 5), True),  # 末日の前日から
        (_MONTH_END, dt.date(2026, 8, 5), False),                   # 末日以後の変更
    ],
)
def test_recent_window_boundaries(cfg, changed_after, changed_before, recent):
    """窓は [対象月末 − days, 対象月末]。区間が重なるかどうかで判定する。"""
    subject = _subject(
        _model_path_month(), events=(_event(changed_after, changed_before),)
    )
    decision = decide_upgrade(subject, cfg)
    assert (decision.status is DecisionStatus.OBSERVE) is recent


def test_old_seat_change_does_not_hold_the_decision(cfg):
    subject = _subject(
        _model_path_month(),
        events=(_event(dt.date(2026, 5, 1), dt.date(2026, 6, 1)),),
    )
    assert decide_upgrade(subject, cfg).status is DecisionStatus.RECOMMENDED


# ----------------------------------------------------------------- 経済軸（6〜7）


@pytest.mark.parametrize(
    ("month", "credit"),
    [
        (_observed_path_month(), None),
        (_credit_path_month(), 100.0),
        (_model_path_month(), None),
    ],
)
def test_each_economic_path_alone_makes_a_candidate(cfg, month, credit):
    subject = _subject(month, credit=credit)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM


def test_no_economic_path_keeps_the_current_seat(cfg):
    """どの経路も成立しなければ現状維持。理由コードは付けない。"""
    subject = _subject(_month(total=100.0, code=100.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.reason_codes == ()


def test_observed_path_needs_the_saving_threshold(cfg):
    """実課金が $1 少ないだけで削減見込みが閾値を割り、候補にならない。"""
    subject = _subject(_month(total=130.0, code=120.0, billed=119.0))
    assert decide_upgrade(subject, cfg).status is DecisionStatus.KEEP


def test_observed_path_needs_a_positive_saving(cfg):
    """閾値を 0 にしても、Standard と Premium が同額なら候補にしない。

    候補条件は「Standard の費用が Premium より高い」ことなので、同額は満たさない。
    """
    permissive = copy.deepcopy(cfg)
    permissive["decision_v2"]["min_assignment_saving_usd"] = 0.0
    # 実課金 $100 で Standard $125 / Premium $125 の同額になる
    equal = _subject(_month(total=130.0, code=120.0, billed=100.0))
    assert decide_upgrade(equal, permissive).status is DecisionStatus.KEEP

    higher = _subject(_month(total=130.0, code=120.0, billed=100.01))
    assert decide_upgrade(higher, permissive).status is DecisionStatus.RECOMMENDED


@pytest.mark.parametrize(
    ("credit", "billed", "reached"),
    [
        (100.0, 95.0, True),     # κ − 許容差ちょうど
        (100.0, 94.99, False),   # 許容差の外
        (None, 95.0, False),     # 設定が分からない
        (0.0, 95.0, False),      # 従量課金が無効
        (3.0, 0.0, False),       # κ が許容差以下でも、課金ゼロを到達と読まない
        (float("inf"), 95.0, False),  # 無制限には到達がない
    ],
)
def test_credit_limit_path_boundaries(cfg, credit, billed, reached):
    subject = _subject(_month(total=130.0, code=120.0, billed=billed), credit=credit)
    decision = decide_upgrade(subject, cfg)
    assert (decision.status is DecisionStatus.RECOMMENDED) is reached
    assert (ReasonCode.CREDIT_LIMIT_REACHED in decision.reason_codes) is reached


def test_model_path_rejects_a_demand_below_every_scenario(cfg):
    """需要 $140 では low しか Premium 有利にならず、mid の削減見込みも足りない。"""
    subject = _subject(_month(total=140.0, code=120.0))
    assert decide_upgrade(subject, cfg).status is DecisionStatus.KEEP


def test_model_path_needs_two_agreeing_scenarios(cfg):
    """一致が1シナリオだけなら、mid の削減見込みが十分でも候補にしない。

    既定の allowance では low が mid より先に Premium 有利へ倒れるため、シナリオ数の
    規則だけが効く場面を作るには allowance を変える必要がある。ここでは Premium の
    low だけを動かし、一致が1シナリオか2シナリオかの違いに絞る（どちらも mid の
    削減見込みは $50 で閾値を満たす）。
    """
    tuned = copy.deepcopy(cfg)
    tuned["seats"]["standard"]["allowance_usd"] = {
        "low": 30.0, "mid": 50.0, "high": 100.0}
    subject = _subject(_month(total=200.0, code=120.0))

    tuned["seats"]["premium"]["allowance_usd"] = {
        "low": 99.0, "mid": 250.0, "high": 375.0}    # 一致は mid だけ
    assert decide_upgrade(subject, tuned).status is DecisionStatus.KEEP

    tuned["seats"]["premium"]["allowance_usd"] = {
        "low": 150.0, "mid": 250.0, "high": 375.0}   # low も一致する
    assert decide_upgrade(subject, tuned).status is DecisionStatus.RECOMMENDED


def test_model_path_needs_a_positive_saving(cfg):
    """純モデル経路でも、mid が同額なら閾値 0 の設定で候補にしない。

    既定の allowance では mid が同額になる需要でシナリオ一致が1つしかないため、
    ここでも allowance を変えて削減見込みの符号だけに絞る（Premium の mid を $1 動かすと
    同額から $1 の削減になる）。
    """
    permissive = copy.deepcopy(cfg)
    permissive["decision_v2"]["min_assignment_saving_usd"] = 0.0
    permissive["seats"]["standard"]["allowance_usd"] = {
        "low": 30.0, "mid": 50.0, "high": 50.0}
    subject = _subject(_month(total=200.0, code=120.0))

    permissive["seats"]["premium"]["allowance_usd"] = {
        "low": 150.0, "mid": 150.0, "high": 375.0}   # mid は Standard と同額
    assert decide_upgrade(subject, permissive).status is DecisionStatus.KEEP

    permissive["seats"]["premium"]["allowance_usd"] = {
        "low": 150.0, "mid": 151.0, "high": 375.0}   # mid が $1 安い
    assert decide_upgrade(subject, permissive).status is DecisionStatus.RECOMMENDED


def test_model_path_saving_threshold_boundary(cfg):
    """2シナリオが一致していても、mid の削減見込みが閾値未満なら候補にしない。"""
    assert decide_upgrade(
        _subject(_month(total=170.0, code=120.0)), cfg
    ).status is DecisionStatus.RECOMMENDED
    assert decide_upgrade(
        _subject(_month(total=169.0, code=120.0)), cfg
    ).status is DecisionStatus.KEEP


# ----------------------------------------------------------------- 分類軸（8）


def test_code_demand_at_the_threshold_is_an_upgrade(cfg):
    subject = _subject(_model_path_month(code=100.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM


def test_low_code_demand_becomes_review_assignment(cfg):
    """費用は見合うが中身が Code でない候補は、人が見直す To-Do として出す。"""
    subject = _subject(_model_path_month(code=99.99))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (ReasonCode.REVIEW_NON_CODE_USAGE,)


def test_review_assignment_reports_supplementary_usage(cfg):
    subject = _subject(_model_path_month(code=10.0, supplementary=True))
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )


def test_unknown_code_demand_falls_back_to_observe(cfg):
    """Code 主体であることを証明できないまま自動で昇格を推奨しない。"""
    subject = _subject(_model_path_month(code=None))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


def test_unknown_code_demand_without_economic_path_still_keeps(cfg):
    """経済軸が成立しなければ、Code 需要が分からなくても現状維持のまま。"""
    subject = _subject(_month(total=100.0, code=None))
    assert decide_upgrade(subject, cfg).status is DecisionStatus.KEEP


def test_review_assignment_keeps_the_economic_evidence(cfg):
    """分類軸の振り分けで、候補になった根拠を落とさない。"""
    subject = _subject(_credit_path_month(code=50.0), credit=100.0)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.CREDIT_LIMIT_REACHED,
    )


def test_review_assignment_keeps_the_sustained_overage_evidence(cfg):
    subject = _subject(
        _observed_path_month(month=_PREVIOUS_MONTH, code=10.0),
        _observed_path_month(code=10.0),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.SUSTAINED_OVERAGE,
    )


def test_unknown_code_demand_keeps_the_economic_evidence(cfg):
    """保留にする場合も、候補になった根拠は記録に残す。"""
    subject = _subject(_credit_path_month(code=None), credit=100.0)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (
        ReasonCode.CREDIT_LIMIT_REACHED,
        ReasonCode.DATA_CONFIDENCE_LOW,
    )


def test_unknown_code_demand_keeps_sustained_overage(cfg):
    """継続超過の根拠も、保留の分岐で落とさない（上限到達と同じ扱い）。"""
    subject = _subject(
        _observed_path_month(month=_PREVIOUS_MONTH),
        _observed_path_month(code=None),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (
        ReasonCode.SUSTAINED_OVERAGE,
        ReasonCode.DATA_CONFIDENCE_LOW,
    )


# ----------------------------------------------------------------- 理由コード（10）


def test_upgrade_reasons_are_in_fixed_order(cfg):
    """主理由 → 補助（上限到達・継続超過）→ 情報（枠不明・混在利用）の順。"""
    subject = _subject(
        _month(month=_PREVIOUS_MONTH, total=200.0, code=150.0, billed=130.0),
        _month(total=200.0, code=150.0, billed=130.0, supplementary=True),
        credit=135.0,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CREDIT_LIMIT_REACHED,
        ReasonCode.SUSTAINED_OVERAGE,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )


def test_upgrade_always_reports_the_base_reason_and_capacity_gap(cfg):
    """1完全月の需要に基づくことと、枠の観測ができないことは常に添える。"""
    decision = decide_upgrade(_subject(_model_path_month()), cfg)
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


def test_sustained_overage_needs_two_consecutive_complete_months(cfg):
    """観測経路が前月にも成立していれば継続超過として添える。"""
    subject = _subject(
        _observed_path_month(month=_PREVIOUS_MONTH), _observed_path_month()
    )
    assert ReasonCode.SUSTAINED_OVERAGE in decide_upgrade(subject, cfg).reason_codes


def test_sustained_overage_absent_when_previous_month_had_no_overage(cfg):
    subject = _subject(
        _month(month=_PREVIOUS_MONTH, total=130.0, code=120.0), _observed_path_month()
    )
    assert ReasonCode.SUSTAINED_OVERAGE not in decide_upgrade(subject, cfg).reason_codes


def test_sustained_overage_absent_when_the_run_hits_a_partial_month(cfg):
    """部分月は継続の根拠にしない（連続が途切れる）。"""
    subject = _subject(
        _observed_path_month(month="2026-05"),
        _observed_path_month(month=_PREVIOUS_MONTH, complete=False),
        _observed_path_month(),
    )
    assert ReasonCode.SUSTAINED_OVERAGE not in decide_upgrade(subject, cfg).reason_codes


@pytest.mark.parametrize(
    "subject",
    [
        _subject(_model_path_month(), conflict=True),
        _subject(_model_path_month(complete=False)),
        _subject(_model_path_month(), events=(
            _event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),)),
        _subject(_month(total=100.0, code=100.0)),
        _subject(_model_path_month()),
        _subject(_model_path_month(code=1.0)),
    ],
)
def test_credit_action_is_never_proposed_here(cfg, subject):
    """追加クレジットの提案はこのルールの担当外（全経路で NONE）。"""
    assert decide_upgrade(subject, cfg).credit_action is CreditAction.NONE


def test_enabled_flag_is_not_consulted(cfg):
    """有効・無効の分岐は結線側の責務で、この関数は呼ばれたら判定する。"""
    enabled = copy.deepcopy(cfg)
    enabled["decision_v2"]["enabled"] = True
    subject = _subject(_model_path_month())
    assert decide_upgrade(subject, enabled) == decide_upgrade(subject, cfg)


def test_member_added_event_type_matches_seat_changes(cfg):
    """加入 event の種別名は seat_changes の語彙と同じもの。"""
    added = _event(
        dt.date(2026, 7, 5), dt.date(2026, 7, 20),
        from_seat=seat_changes.ABSENT, to_seat="standard",
    )
    assert added.event_type in seat_changes.EVENT_TYPES
    subject = _subject(_model_path_month(), events=(added,))
    assert decide_upgrade(subject, cfg).reason_codes == (ReasonCode.RECENT_MEMBER,)


# ------------------------------------------------------------- 値オブジェクトの検証


def _decision(**overrides) -> DecisionV2:
    fields = {
        "status": DecisionStatus.KEEP,
        "seat_action": SeatAction.KEEP,
        "credit_action": CreditAction.NONE,
        "reason_codes": (),
    }
    return DecisionV2(**{**fields, **overrides})


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": SeatAction.KEEP},            # 値は "keep" で等しいが別の語彙
        {"status": "keep"},                     # 生の文字列
        {"seat_action": DecisionStatus.KEEP},
        {"seat_action": "keep"},
        {"credit_action": SeatAction.NONE},
        {"credit_action": "none"},
        {"reason_codes": (IssueCode.PARTIAL_MONTH,)},
        {"reason_codes": ("PARTIAL_MONTH",)},
        {"reason_codes": [ReasonCode.PARTIAL_MONTH]},   # tuple ではない
    ],
)
def test_decision_rejects_values_from_other_vocabularies(overrides):
    """StrEnum の等値衝突は型では防げないので、境界で isinstance 検証する（§12.1）。"""
    with pytest.raises((TypeError, ValueError)):
        _decision(**overrides)


def test_decision_accepts_its_own_vocabularies():
    decision = _decision(
        status=DecisionStatus.RECOMMENDED,
        seat_action=SeatAction.UPGRADE_TO_PREMIUM,
        reason_codes=(ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,),
    )
    assert decision.reason_codes == (ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,)


def test_history_requires_at_least_one_month():
    with pytest.raises(ValueError, match="months"):
        _subject()


@pytest.mark.parametrize(
    "months",
    [
        (_month(month="2026-07"), _month(month="2026-06")),   # 降順
        (_month(month="2026-07"), _month(month="2026-07")),   # 重複
    ],
)
def test_history_requires_ascending_unique_months(months):
    with pytest.raises(ValueError, match="昇順"):
        _subject(*months)


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "202607", "", "2026-07-01"])
def test_month_observation_requires_a_month_key(month):
    with pytest.raises(ValueError, match="YYYY-MM"):
        _month(month=month)


@pytest.mark.parametrize(
    "field",
    ["total_demand_usd", "code_demand_usd", "billed_usd"],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_month_observation_rejects_non_finite_amounts(field, value):
    """非有限値は比較を黙って偽にし、判定を変えてしまう。"""
    amounts = {"total_demand_usd": 0.0, "code_demand_usd": 0.0, "billed_usd": 0.0}
    amounts[field] = value
    with pytest.raises(ValueError, match=field):
        MonthObservation(
            month=_MONTH, complete=True, supplementary_high=False, **amounts
        )


@pytest.mark.parametrize(
    "field",
    ["total_demand_usd", "code_demand_usd", "billed_usd"],
)
def test_month_observation_accepts_negative_amounts(field):
    """返金があると需要・実課金は負になりうるので、有限な負値は拒否しない（κ とは別の契約）。"""
    amounts = {"total_demand_usd": 0.0, "code_demand_usd": 0.0, "billed_usd": 0.0}
    amounts[field] = -12.5
    observation = MonthObservation(
        month=_MONTH, complete=True, supplementary_high=False, **amounts
    )
    assert getattr(observation, field) == -12.5


def test_credit_limit_accepts_unlimited_but_not_nan():
    """無制限は Infinity で表せる。NaN は「分からない」の表現（None）ではない。"""
    assert _subject(_month(), credit=float("inf")).credit_limit_usd == float("inf")
    with pytest.raises(ValueError, match="credit_limit_usd"):
        _subject(_month(), credit=float("nan"))


@pytest.mark.parametrize("credit", [-0.01, -5.0, float("-inf")])
def test_credit_limit_rejects_negative_values(credit):
    """負の上限に対応する状態は無い（黙って「無効」「未到達」として扱わない）。"""
    with pytest.raises(ValueError, match="credit_limit_usd"):
        _subject(_month(), credit=credit)


@pytest.mark.parametrize("credit", [None, 0.0, 0, 250.0])
def test_credit_limit_accepts_the_defined_states(credit):
    """不明（None）・無効（0）・上限ありの正数はそのまま受け取る。"""
    assert _subject(_month(), credit=credit).credit_limit_usd == credit
