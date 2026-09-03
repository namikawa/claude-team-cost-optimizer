"""V2判定の語彙と、昇格ルール（Standard→Premium）のテスト。

前半は語彙（DecisionStatus・SeatAction・CreditAction・ReasonCode）。値は
decision-evidence.csv にそのまま書かれ、月をまたいだ比較・実変更との照合の
突き合わせキーになる。名前と値の完全な集合をリテラルで固定し、増減・改名・値の
変更がすべてここで落ちるようにする。

後半は decision_v2 の判定関数（decide_upgrade・decide_downgrade・policy_stability）。
判定の分岐と境界を、既定 config の値を前提にした具体的な金額で固定する（前提そのものは
test_default_config_values_the_cases_assume が固定するので、config を変えた場合は
そこが最初に落ちる）。
"""

import copy
import datetime as dt
import json

import pytest

from seat_analyzer import seat_changes
from seat_analyzer.decision_v2 import (
    _REASON_ORDER,
    CreditPoint,
    DecisionV2,
    MonthObservation,
    SubjectHistory,
    _status,
    decide_downgrade,
    decide_upgrade,
    policy_stability,
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
        "TOTAL_DEMAND_ABOVE_PREMIUM_LINE",
        "SUSTAINED_OVERAGE",
        "SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE",
        "CREDIT_LIMIT_REACHED",
        "CREDIT_SETTING_UNKNOWN",
        "STANDARD_BILLING_ABOVE_SEAT_GAP",
        "CREDIT_CONSUMPTION_RISING",
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
# 07-03..07-31 になる。金額は既定 config（Standard $25・Premium $125＝シート差額 $100、
# 観測マージ $20、方針線 $450、Code 需要閾値 $200、上限の許容差 $5）を前提にする。

_MONTH = "2026-07"
_PREVIOUS_MONTH = "2026-06"
_MONTH_END = dt.date(2026, 7, 31)
_WINDOW_START = dt.date(2026, 7, 3)

# 追加クレジット上限 κ の3状態。κ は候補化に使う信号そのものを変えるので（設計書 §12.6）、
# この節の既定は「有効」にして実課金による候補化だけを見る。無効・不明はクレジット比較の
# 節で明示的に渡す。有効な値は $250 で、この節の実課金（$150 以下）では上限へ到達しない
_CREDIT_ENABLED = 250.0
_CREDIT_DISABLED = 0.0
_CREDIT_UNKNOWN = None


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
    credit: float | None = _CREDIT_ENABLED,
    conflict: bool = False,
    events: tuple[SeatChangeEvent, ...] = (),
    unclassified: tuple[UnclassifiedObservation, ...] = (),
    points: tuple[CreditPoint, ...] = (),
) -> SubjectHistory:
    return SubjectHistory(
        email="a@example.com",
        current_seat=seat,
        credit_limit_usd=credit,
        identity_conflict=conflict,
        months=months,
        seat_events=events,
        unclassified=unclassified,
        credit_points=points,
    )


def _point(day: int, mtd: float, *, month: int = 7) -> CreditPoint:
    """対象月（既定）の追加クレジット消費の観測点。"""
    return CreditPoint(taken_on=dt.date(2026, month, day), mtd_usd=mtd)


# 対象月内で狭義単調増加する3点（継続上昇とみなす最小の並び）
_RISING_POINTS = (_point(8, 10.0), _point(15, 30.0), _point(22, 60.0))


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


# 候補化の信号を1つずつ単独で成立させる観測（他の信号は成立しない）
def _observed_path_month(**overrides) -> MonthObservation:
    """観測経路のみ: 実課金 $120 がシート差額 $100 をマージちょうど（$20）上回る。"""
    return _month(**{"total": 130.0, "code": 120.0, "billed": 120.0, **overrides})


def _credit_path_month(**overrides) -> MonthObservation:
    """上限到達経路のみ: κ=$100 に対し実課金 $95（= κ − 許容差）。"""
    return _month(**{"total": 130.0, "code": 120.0, "billed": 95.0, **overrides})


def _code_heavy_month(**overrides) -> MonthObservation:
    """観測経路が成立し、中身も Code 主体の観測: 需要 $500・Code $300・実課金 $150。"""
    return _month(**{"total": 500.0, "code": 300.0, "billed": 150.0, **overrides})


def _above_line_month(**overrides) -> MonthObservation:
    """方針線（$450）以上の需要で実課金なし: 需要 $500・Code $300。

    κ が有効な組織では候補にならない（実課金がない）。方針線を使う κ 無効・不明の
    経路のための観測。
    """
    return _month(**{"total": 500.0, "code": 300.0, **overrides})


def _below_line_month(**overrides) -> MonthObservation:
    """方針線に届かない需要で実課金なし: 需要 $45・Code $20。"""
    return _month(**{"total": 45.0, "code": 20.0, **overrides})


def test_default_config_values_the_cases_assume(cfg):
    """このファイルの金額が前提にしている既定値。変わればここが最初に落ちる。"""
    assert cfg["seats"]["standard"]["price_usd"] == 25.0
    assert cfg["seats"]["premium"]["price_usd"] == 125.0
    assert cfg["decision_v2"]["premium_justification_usd"] == 450.0
    assert cfg["decision_v2"]["observed_billing_margin_usd"] == 20.0
    assert cfg["decision_v2"]["upgrade"]["min_complete_months"] == 1
    assert cfg["decision_v2"]["upgrade"]["min_code_demand_usd"] == 200.0
    assert cfg["decision_v2"]["downgrade"]["min_complete_months"] == 2
    assert cfg["decision_v2"]["downgrade"]["max_code_demand_usd"] == 200.0
    assert cfg["decision_v2"]["recent_seat_change_days"] == 28
    assert cfg["usage_credits"]["cap_tolerance_usd"] == 5.0


def test_every_reason_code_has_a_rank(cfg):
    """理由コードの並び順表に全メンバーがある。

    抜けている値を返す経路に入ると、並べ替えが実行時に KeyError で落ちる（結論そのものは
    正しく出ているのに判定が失敗する形になる）。
    """
    assert set(_REASON_ORDER) == set(ReasonCode)
    assert len(_REASON_ORDER) == len(ReasonCode)


@pytest.mark.parametrize(
    ("seat_action", "credit_action", "expected"),
    [
        (SeatAction.KEEP, CreditAction.NONE, DecisionStatus.KEEP),
        (SeatAction.KEEP, CreditAction.REVIEW, DecisionStatus.RECOMMENDED),
        (
            SeatAction.DOWNGRADE_TO_STANDARD,
            CreditAction.NONE,
            DecisionStatus.RECOMMENDED,
        ),
    ],
)
def test_status_rises_when_there_is_work(seat_action, credit_action, expected):
    """人が取るべき作業（シート変更・クレジットの付与や確認）があれば RECOMMENDED。"""
    assert _status(DecisionStatus.KEEP, seat_action, credit_action) is expected


# ------------------------------------------------------------- 判定しない経路（1〜4）


@pytest.mark.parametrize("seat", ["premium", "unassigned", "unknown", ""])
def test_non_standard_seat_is_rejected(cfg, seat):
    """Standard 以外の振り分けは呼び出し側の責務で、既定の結論を持たない。"""
    subject = _subject(_observed_path_month(), seat=seat)
    with pytest.raises(ValueError, match="current_seat"):
        decide_upgrade(subject, cfg)


def test_identity_conflict_is_no_decision(cfg):
    subject = _subject(_observed_path_month(), conflict=True)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.IDENTITY_CONFLICT,)


def test_partial_target_month_is_no_decision(cfg):
    """対象月が部分月なら、他の月がそろっていても判定しない。"""
    subject = _subject(
        _observed_path_month(month=_PREVIOUS_MONTH),
        _observed_path_month(complete=False),
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
        _observed_path_month(month=_PREVIOUS_MONTH, complete=False),
        _observed_path_month(),
    )
    decision = decide_upgrade(subject, strict)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)


def test_complete_months_need_not_be_the_recent_ones(cfg):
    """完全月が要求数あれば判定へ進む（既定は1完全月で候補化できる）。"""
    subject = _subject(_observed_path_month())
    assert decide_upgrade(subject, cfg).status is DecisionStatus.RECOMMENDED


# --------------------------------------------------------------- recent 窓（5）


def test_recent_member_addition_falls_back_to_observe(cfg):
    subject = _subject(
        _observed_path_month(),
        events=(_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20),
                       from_seat=seat_changes.ABSENT, to_seat="standard"),),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.RECENT_MEMBER,)


def test_recent_seat_change_falls_back_to_observe(cfg):
    subject = _subject(
        _observed_path_month(),
        events=(_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (ReasonCode.RECENT_SEAT_CHANGE,)


def test_unclassified_observation_alone_falls_back_to_observe(cfg):
    """event が無くても、分類できない観測が窓に重なれば保留側へ倒す（§10.4）。"""
    subject = _subject(
        _observed_path_month(),
        unclassified=(_observation(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


def test_recent_reasons_are_reported_together_in_fixed_order(cfg):
    subject = _subject(
        _observed_path_month(),
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
    subject = _subject(_observed_path_month())
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
        _observed_path_month(), events=(_event(changed_after, changed_before),)
    )
    decision = decide_upgrade(subject, cfg)
    assert (decision.status is DecisionStatus.OBSERVE) is recent


def test_old_seat_change_does_not_hold_the_decision(cfg):
    subject = _subject(
        _observed_path_month(),
        events=(_event(dt.date(2026, 5, 1), dt.date(2026, 6, 1)),),
    )
    assert decide_upgrade(subject, cfg).status is DecisionStatus.RECOMMENDED


# ------------------------------------------------- 経済軸: κ が有効な組織（観測で候補化）


@pytest.mark.parametrize(
    ("month", "credit"),
    [
        (_observed_path_month(), _CREDIT_ENABLED),
        (_credit_path_month(), 100.0),
    ],
)
def test_each_observed_signal_alone_makes_a_candidate(cfg, month, credit):
    """実課金がシート差額を上回る経路と、上限到達の経路はそれぞれ単独で候補を作る。

    seat_action は分類軸が決めるのでここでは見ない。この需要規模（$130）では Code 需要が
    閾値（$200）に届かないため、いずれも REVIEW_ASSIGNMENT になる。
    """
    subject = _subject(month, credit=credit)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT


def test_no_signal_keeps_the_current_seat(cfg):
    """どの信号も成立しなければ現状維持。理由コードは付けない。"""
    subject = _subject(_month(total=100.0, code=100.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.reason_codes == ()


def test_code_heavy_billing_month_upgrades(cfg):
    """実課金がシート差額を上回り、中身が Code 主体なら昇格推奨。

    需要が大きくても成立する点が旧規則との違い。旧規則の観測経路は変更先の費用を
    「需要 − 込み枠推定」で試算していたため、需要が大きいほど成立しなくなっていた
    （実課金が同じでも需要 $130 なら候補・$1,500 なら候補にならない）。
    """
    subject = _subject(_code_heavy_month(total=1500.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


@pytest.mark.parametrize("total", [130.0, 5000.0])
def test_the_observed_path_does_not_depend_on_demand(cfg, total):
    """観測経路は実課金とシート差額だけを見る（需要の大小で結論が変わらない）。"""
    subject = _subject(_month(total=total, code=120.0, billed=120.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
    )


@pytest.mark.parametrize(
    ("billed", "candidate"),
    [
        (110.0, False),     # 差額を上回るが $10 でマージに届かない
        (119.99, False),    # マージの1セント下
        (120.0, True),      # マージちょうど
    ],
)
def test_observed_path_margin_boundary(cfg, billed, candidate):
    """シート差額（$100）を観測マージ（$20）以上上回ったときだけ候補にする。"""
    subject = _subject(_month(total=130.0, code=120.0, billed=billed))
    decision = decide_upgrade(subject, cfg)
    assert (decision.status is DecisionStatus.RECOMMENDED) is candidate


@pytest.mark.parametrize(("billed", "candidate"), [(100.0, False), (100.01, True)])
def test_observed_path_needs_to_exceed_the_seat_gap(cfg, billed, candidate):
    """マージを 0 にしても、シート差額とちょうど同額は候補にしない。

    条件は「上回った」ことなので、同額は満たさない。
    """
    permissive = copy.deepcopy(cfg)
    permissive["decision_v2"]["observed_billing_margin_usd"] = 0.0
    subject = _subject(_month(total=130.0, code=120.0, billed=billed))
    decision = decide_upgrade(subject, permissive)
    assert (decision.status is DecisionStatus.RECOMMENDED) is candidate


def test_enabled_credit_does_not_use_the_policy_line(cfg):
    """κ が有効な組織では方針線を使わない（実課金という観測が判定の材料になる）。"""
    subject = _subject(_above_line_month(total=600.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == ()


@pytest.mark.parametrize(
    ("credit", "billed", "reached"),
    [
        (100.0, 95.0, True),     # κ − 許容差ちょうど
        (100.0, 94.99, False),   # 許容差の外
        (None, 95.0, False),     # 設定が分からない
        (0.0, 95.0, False),      # 従量課金が無効（記入と観測の矛盾として不明扱い）
        (3.0, 0.0, False),       # κ が許容差以下でも、課金ゼロを到達と読まない
        (float("inf"), 95.0, False),  # 無制限には到達がない
    ],
)
def test_credit_limit_path_boundaries(cfg, credit, billed, reached):
    """上限へ到達したときだけ、上限到達を根拠として添える。"""
    subject = _subject(_month(total=130.0, code=120.0, billed=billed), credit=credit)
    decision = decide_upgrade(subject, cfg)
    assert (ReasonCode.CREDIT_LIMIT_REACHED in decision.reason_codes) is reached


def test_the_credit_limit_path_alone_does_not_report_the_billing_comparison(cfg):
    """上限到達だけで候補になった場合は、実課金がシート差額を上回ってはいない（§12.4）。"""
    decision = decide_upgrade(_subject(_credit_path_month(), credit=100.0), cfg)
    assert ReasonCode.CREDIT_LIMIT_REACHED in decision.reason_codes
    assert ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP not in decision.reason_codes


def test_both_observed_signals_are_reported_in_fixed_order(cfg):
    """上限到達と実課金の超過は同時に成立しうる（並びは固定）。"""
    subject = _subject(_code_heavy_month(billed=130.0), credit=100.0)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CREDIT_LIMIT_REACHED,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


# ----------------------------------------------------------------- 分類軸（8）


def test_code_demand_at_the_threshold_is_an_upgrade(cfg):
    subject = _subject(_code_heavy_month(code=200.0))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM


def test_low_code_demand_becomes_review_assignment(cfg):
    """費用は見合うが中身が Code でない候補は、人が見直す To-Do として出す。"""
    subject = _subject(_code_heavy_month(code=199.99))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
    )


def test_review_assignment_reports_supplementary_usage(cfg):
    subject = _subject(_observed_path_month(code=10.0, supplementary=True))
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )


def test_unknown_code_demand_falls_back_to_observe(cfg):
    """Code 主体であることを証明できないまま自動で昇格を推奨しない。"""
    subject = _subject(_observed_path_month(code=None))
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.DATA_CONFIDENCE_LOW,
    )


def test_unknown_code_demand_without_a_signal_still_keeps(cfg):
    """候補化の信号が無ければ、Code 需要が分からなくても現状維持のまま。"""
    subject = _subject(_month(total=100.0, code=None))
    assert decide_upgrade(subject, cfg).status is DecisionStatus.KEEP


def test_review_assignment_keeps_the_candidacy_evidence(cfg):
    """分類軸の振り分けで、候補になった根拠を落とさない。

    この観測は上限到達だけで候補になっていて、実課金はシート差額を上回っていない。
    """
    subject = _subject(_credit_path_month(code=50.0), credit=100.0)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.CREDIT_LIMIT_REACHED,
    )


def test_unknown_code_demand_keeps_the_candidacy_evidence(cfg):
    """保留にする場合も、候補になった根拠は記録に残す。"""
    subject = _subject(_credit_path_month(code=None), credit=100.0)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (
        ReasonCode.CREDIT_LIMIT_REACHED,
        ReasonCode.DATA_CONFIDENCE_LOW,
    )


# ----------------------------------------------------------------- 理由コード（10）


def test_upgrade_reasons_are_in_fixed_order(cfg):
    """主理由 → 補助（上限到達・継続超過・実課金の超過）→ 情報（枠不明・混在利用）の順。"""
    subject = _subject(
        _month(month=_PREVIOUS_MONTH, total=250.0, code=200.0, billed=130.0),
        _month(total=250.0, code=200.0, billed=130.0, supplementary=True),
        credit=135.0,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CREDIT_LIMIT_REACHED,
        ReasonCode.SUSTAINED_OVERAGE,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )


def test_upgrade_always_reports_the_base_reason_and_capacity_gap(cfg):
    """1完全月の需要に基づくことと、枠の観測ができないことは常に添える。"""
    decision = decide_upgrade(_subject(_code_heavy_month()), cfg)
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
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
        _subject(_observed_path_month(), conflict=True),
        _subject(_observed_path_month(complete=False)),
        _subject(_observed_path_month(), events=(
            _event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),)),
        _subject(_month(total=100.0, code=100.0)),
        _subject(_observed_path_month()),
        _subject(_observed_path_month(code=1.0)),
    ],
)
def test_credit_action_is_not_proposed_when_the_limit_is_enabled(cfg, subject):
    """κ が有効な組織では、シート側のどの経路でもクレジットの提案を出さない。

    上限が有効なら実課金という観測があり、判定はそれを使える。提案が要るのは κ が
    無効・不明の場合で、クレジット比較の節で扱う。
    """
    assert decide_upgrade(subject, cfg).credit_action is CreditAction.NONE


def test_enabled_flag_is_not_consulted(cfg):
    """有効・無効の分岐は結線側の責務で、この関数は呼ばれたら判定する。"""
    enabled = copy.deepcopy(cfg)
    enabled["decision_v2"]["enabled"] = True
    subject = _subject(_observed_path_month())
    assert decide_upgrade(subject, enabled) == decide_upgrade(subject, cfg)


def test_member_added_event_type_matches_seat_changes(cfg):
    """加入 event の種別名は seat_changes の語彙と同じもの。"""
    added = _event(
        dt.date(2026, 7, 5), dt.date(2026, 7, 20),
        from_seat=seat_changes.ABSENT, to_seat="standard",
    )
    assert added.event_type in seat_changes.EVENT_TYPES
    subject = _subject(_observed_path_month(), events=(added,))
    assert decide_upgrade(subject, cfg).reason_codes == (ReasonCode.RECENT_MEMBER,)


# --------------------------------------------------------------- 降格ルール（Step 16）
#
# 既定の降格は2完全月を要求するので、履歴は 2026-06 + 2026-07 で組む。経済軸は方針線
# （$450）との比較だけで、評価窓の全完全月で需要が線を下回ることを要求する。


def _low_demand_month(**overrides) -> MonthObservation:
    """降格の経済軸が成立する観測: 需要 $100（うち Code $50）・実課金なし。"""
    return _month(**{"total": 100.0, "code": 50.0, **overrides})


def _downgrade_months(**overrides) -> tuple[MonthObservation, MonthObservation]:
    """経済軸が成立する完全月2つ（overrides は両月に効く）。"""
    return (
        _low_demand_month(month=_PREVIOUS_MONTH, **overrides),
        _low_demand_month(**overrides),
    )


def _premium_subject(*months: MonthObservation, **kwargs) -> SubjectHistory:
    return _subject(*months, seat="premium", **kwargs)


# ------------------------------------------------------- 判定しない経路（1〜4）


@pytest.mark.parametrize("seat", ["standard", "unassigned", "unknown", ""])
def test_non_premium_seat_is_rejected(cfg, seat):
    """Premium 以外の振り分けは呼び出し側の責務で、既定の結論を持たない。"""
    subject = _subject(*_downgrade_months(), seat=seat)
    with pytest.raises(ValueError, match="current_seat"):
        decide_downgrade(subject, cfg)


def test_downgrade_identity_conflict_is_no_decision(cfg):
    subject = _premium_subject(*_downgrade_months(), conflict=True)
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.IDENTITY_CONFLICT,)


def test_downgrade_partial_target_month_is_no_decision(cfg):
    """対象月が部分月なら、他の月がそろっていても判定しない。"""
    subject = _premium_subject(
        _low_demand_month(month="2026-05"),
        _low_demand_month(month=_PREVIOUS_MONTH),
        _low_demand_month(complete=False),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.PARTIAL_MONTH,)


def test_downgrade_needs_two_complete_months(cfg):
    """完全月が1つだけなら判定しない（誤った降格は業務を止めるため履歴を重くする）。"""
    subject = _premium_subject(_low_demand_month())
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.NO_DECISION
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)


def test_downgrade_does_not_count_partial_months_as_history(cfg):
    """部分月は完全月の数に入らない（3ヶ月あっても完全月1つでは判定しない）。"""
    subject = _premium_subject(
        _low_demand_month(month="2026-05", complete=False),
        _low_demand_month(month=_PREVIOUS_MONTH, complete=False),
        _low_demand_month(),
    )
    assert decide_downgrade(subject, cfg).reason_codes == (
        ReasonCode.INSUFFICIENT_HISTORY,
    )


def test_downgrade_at_exactly_two_complete_months_decides(cfg):
    """受け入れ確認: 2完全月とも需要が方針線未満・Code 低・実課金なしなら降格推奨。"""
    subject = _premium_subject(*_downgrade_months())
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD
    assert decision.reason_codes == (
        ReasonCode.SUSTAINED_LOW_CODE_DEMAND,
        ReasonCode.SUSTAINED_LOW_TOTAL_DEMAND,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


# ------------------------------------------------------------- recent 窓（5）


def test_downgrade_recent_member_addition_falls_back_to_observe(cfg):
    subject = _premium_subject(
        *_downgrade_months(),
        events=(_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20),
                       from_seat=seat_changes.ABSENT, to_seat="premium"),),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.RECENT_MEMBER,)


def test_downgrade_recent_seat_change_falls_back_to_observe(cfg):
    subject = _premium_subject(
        *_downgrade_months(),
        events=(_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (ReasonCode.RECENT_SEAT_CHANGE,)


def test_downgrade_unclassified_observation_falls_back_to_observe(cfg):
    subject = _premium_subject(
        *_downgrade_months(),
        unclassified=(_observation(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


def test_old_seat_change_does_not_hold_the_downgrade(cfg):
    subject = _premium_subject(
        *_downgrade_months(),
        events=(_event(dt.date(2026, 5, 1), dt.date(2026, 6, 1)),),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


# ------------------------------------------------------------- 実課金（6〜7）


@pytest.mark.parametrize(
    ("older_billed", "target_billed"),
    [
        (0.01, 0.0),    # 窓の古い月だけ
        (0.0, 0.01),    # 対象月だけ
        (25.0, 25.0),   # 両月
    ],
)
def test_any_billing_in_the_window_keeps_the_seat(cfg, older_billed, target_billed):
    """Premium での実課金は込み枠を超えた観測なので、どの月のものでも候補から外す。"""
    subject = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, billed=older_billed),
        _low_demand_month(billed=target_billed),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.reason_codes == ()


def test_refunds_are_not_billing(cfg):
    """負の実課金（返金）は課金なしとして扱う。"""
    subject = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, billed=-12.5),
        _low_demand_month(billed=-1.0),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


def test_billing_outside_the_window_does_not_block_the_downgrade(cfg):
    """評価窓より古い月の実課金は見ない（窓は直近の完全月だけ）。"""
    subject = _premium_subject(
        _low_demand_month(month="2026-05", billed=250.0), *_downgrade_months()
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


@pytest.mark.parametrize("credit", [None, 0.0, 250.0, float("inf")])
def test_downgrade_does_not_depend_on_the_credit_limit(cfg, credit):
    """実課金 0 が確定した窓では、追加クレジット上限の設定は結論を変えない。

    上限への到達は実課金の発生を含意するため、実課金の検査に含まれている。
    """
    subject = _premium_subject(*_downgrade_months(), credit=credit)
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


# ----------------------------------------------------------------- 分類軸（8〜10）


@pytest.mark.parametrize(
    "subject",
    [
        _premium_subject(
            _low_demand_month(month=_PREVIOUS_MONTH, code=None), _low_demand_month()),
        _premium_subject(
            _low_demand_month(month=_PREVIOUS_MONTH), _low_demand_month(code=None)),
    ],
)
def test_unknown_code_demand_in_the_window_falls_back_to_observe(cfg, subject):
    """Code 需要が低いことを証明できないまま自動で降格しない。"""
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


@pytest.mark.parametrize(("code", "downgrade"), [(200.0, False), (199.99, True)])
def test_downgrade_code_demand_threshold_boundary(cfg, code, downgrade):
    """閾値ちょうどの Code 需要は「低い」とみなさない（Code 実務者の席は落とさない）。"""
    subject = _premium_subject(*_downgrade_months(total=300.0, code=code))
    decision = decide_downgrade(subject, cfg)
    assert (decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD) is downgrade


def test_high_code_demand_in_any_window_month_keeps_the_seat(cfg):
    """窓のどの月かで Code 需要が高ければ降格しない。"""
    older = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, total=300.0, code=200.0),
        _low_demand_month(code=1.0),
    )
    assert decide_downgrade(older, cfg).seat_action is SeatAction.KEEP
    target = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, code=1.0),
        _low_demand_month(total=300.0, code=200.0),
    )
    assert decide_downgrade(target, cfg).seat_action is SeatAction.KEEP


def test_high_code_demand_outweighs_high_supplementary_usage(cfg):
    """Code 需要が高いユーザは、supplementary が高くても見直しへは回さない。"""
    subject = _premium_subject(
        *_downgrade_months(total=1000.0, code=300.0, supplementary=True)
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.reason_codes == ()


def test_high_supplementary_usage_becomes_review_assignment(cfg):
    """Code が低く supplementary が高いユーザは、シートではなくアサインを見直す。"""
    subject = _premium_subject(*_downgrade_months(supplementary=True))
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )


def test_high_supplementary_usage_is_reviewed_above_the_policy_line(cfg):
    """需要が方針線を上回って経済軸が成立しない場合も、見直しへ回す。

    現状維持で終わらせると「Premium の枠を非 Code 利用で使っている」状態が誰の
    To-Do にもならないため、分類軸を経済軸より先に見る。
    """
    high_total = _downgrade_months(total=1000.0, code=10.0)
    assert decide_downgrade(
        _premium_subject(*high_total), cfg
    ).seat_action is SeatAction.KEEP    # 経済軸は成立しない（需要が方針線を上回る）

    subject = _premium_subject(
        *_downgrade_months(total=1000.0, code=10.0, supplementary=True)
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT


@pytest.mark.parametrize("supplementary", [False, None])
def test_supplementary_not_high_proceeds_to_the_economic_axis(cfg, supplementary):
    """False も None（分からない）も、見直しへは回さない。"""
    subject = _premium_subject(*_downgrade_months(supplementary=supplementary))
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


def test_only_the_target_month_supplementary_flag_is_consulted(cfg):
    """混在利用は対象月の状態で見る（過去の月の高さは振り分けを変えない）。"""
    subject = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, supplementary=True),
        _low_demand_month(supplementary=False),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


# ----------------------------------------------------------------- 経済軸（11〜12）


@pytest.mark.parametrize(("total", "downgrade"), [(449.99, True), (450.0, False)])
def test_downgrade_policy_line_boundary(cfg, total, downgrade):
    """方針線ちょうどの需要は「未満」ではない（境界は Premium 側に含める）。"""
    subject = _premium_subject(*_downgrade_months(total=total))
    decision = decide_downgrade(subject, cfg)
    assert (decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD) is downgrade


def test_the_policy_line_must_hold_in_every_window_month(cfg):
    """窓のどの月かで需要が方針線を上回れば現状維持（1ヶ月の落ち込みで席を落とさない）。"""
    older_only = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH), _low_demand_month(total=500.0)
    )
    assert decide_downgrade(older_only, cfg).seat_action is SeatAction.KEEP
    target_only = _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, total=500.0), _low_demand_month()
    )
    assert decide_downgrade(target_only, cfg).seat_action is SeatAction.KEEP


def test_the_policy_line_follows_the_configured_value(cfg):
    """方針線は config の値。下げれば同じ需要が「線以上」になる。"""
    lowered = copy.deepcopy(cfg)
    lowered["decision_v2"]["premium_justification_usd"] = 90.0
    subject = _premium_subject(*_downgrade_months())
    assert decide_downgrade(subject, cfg).seat_action is (
        SeatAction.DOWNGRADE_TO_STANDARD
    )
    assert decide_downgrade(subject, lowered).seat_action is SeatAction.KEEP


def test_evaluation_window_skips_partial_months(cfg):
    """評価窓は完全月だけを新しい順に採る（間に挟まった部分月は入らない）。"""
    subject = _premium_subject(
        _low_demand_month(month="2026-05"),
        _low_demand_month(month=_PREVIOUS_MONTH, complete=False,
                          total=1000.0, billed=250.0),
        _low_demand_month(),
    )
    decision = decide_downgrade(subject, cfg)
    assert decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


def test_evaluation_window_length_follows_the_configured_month_count(cfg):
    """窓の長さは downgrade.min_complete_months に従う。"""
    months = (
        _low_demand_month(month="2026-05", total=500.0),
        _low_demand_month(month=_PREVIOUS_MONTH),
        _low_demand_month(),
    )
    assert decide_downgrade(
        _premium_subject(*months), cfg
    ).seat_action is SeatAction.DOWNGRADE_TO_STANDARD   # 直近2完全月だけを見る

    strict = copy.deepcopy(cfg)
    strict["decision_v2"]["downgrade"]["min_complete_months"] = 3
    assert decide_downgrade(
        _premium_subject(*months), strict
    ).seat_action is SeatAction.KEEP


# ------------------------------------------------------------- 理由コード（12）


@pytest.mark.parametrize(
    "subject",
    [
        _premium_subject(*_downgrade_months(), conflict=True),
        _premium_subject(
            _low_demand_month(month=_PREVIOUS_MONTH),
            _low_demand_month(complete=False),
        ),
        _premium_subject(_low_demand_month()),
        _premium_subject(*_downgrade_months(), events=(
            _event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),)),
        _premium_subject(*_downgrade_months(billed=25.0)),
        _premium_subject(*_downgrade_months(code=None)),
        _premium_subject(*_downgrade_months(supplementary=True)),
        _premium_subject(*_downgrade_months(total=1000.0)),
        _premium_subject(*_downgrade_months()),
    ],
)
def test_downgrade_never_proposes_a_credit_action(cfg, subject):
    """追加クレジットの提案はこのルールの担当外（全経路で NONE）。"""
    assert decide_downgrade(subject, cfg).credit_action is CreditAction.NONE


def test_downgrade_does_not_consult_the_enabled_flag(cfg):
    """有効・無効の分岐は結線側の責務で、この関数は呼ばれたら判定する。"""
    enabled = copy.deepcopy(cfg)
    enabled["decision_v2"]["enabled"] = True
    subject = _premium_subject(*_downgrade_months())
    assert decide_downgrade(subject, enabled) == decide_downgrade(subject, cfg)


def test_premium_member_added_event_type_matches_seat_changes(cfg):
    """加入 event の種別名は seat_changes の語彙と同じもの（Premium での加入）。"""
    added = _event(
        dt.date(2026, 7, 5), dt.date(2026, 7, 20),
        from_seat=seat_changes.ABSENT, to_seat="premium",
    )
    assert added.event_type in seat_changes.EVENT_TYPES
    subject = _premium_subject(*_downgrade_months(), events=(added,))
    assert decide_downgrade(subject, cfg).reason_codes == (ReasonCode.RECENT_MEMBER,)


# ----------------------------------------------------- 追加クレジット比較（Step 18）
#
# 追加クレジット上限 κ の3状態（有効・無効・不明）で、候補化に使う信号と結論の出し方が
# 変わる（設計書 §12.6）。上の昇格の節は κ を有効にして実課金の経路だけを見ているので、
# ここでは無効（$0）・不明（None）を明示的に渡す。


# ------------------------------------------------------ κ 無効（方針線と継続性のゲート）


def test_one_month_above_the_line_becomes_a_credit_candidate(cfg):
    """受け入れ確認: 一時的な需要は credit 候補（席を変えず課金の実測を先に取る）。

    追加クレジットが無効な組織では実課金が構造的に $0 で、課金を待っていても昇格の根拠に
    なる観測は永久に得られない。上限つきクレジットの付与はその行き詰まりを解く可逆な
    計測手段なので、1完全月の成立ではこちらを出す。
    """
    subject = _subject(_above_line_month(), credit=_CREDIT_DISABLED)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.ENABLE_WITH_CAP
    assert decision.reason_codes == (ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,)


def test_two_months_above_the_line_become_an_upgrade(cfg):
    """受け入れ確認: 継続需要は Premium 候補（2完全月連続で方針線以上）。"""
    subject = _subject(
        _above_line_month(month=_PREVIOUS_MONTH),
        _above_line_month(),
        credit=_CREDIT_DISABLED,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


def test_two_months_above_the_line_without_code_evidence_is_reviewed(cfg):
    """継続していて中身が Code でなければアサインの見直し（分類軸は昇格側と同じ）。"""
    subject = _subject(
        _above_line_month(month=_PREVIOUS_MONTH, code=1.0),
        _above_line_month(code=1.0),
        credit=_CREDIT_DISABLED,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
    )


def test_two_months_above_the_line_without_a_code_figure_observes(cfg):
    """継続していても Code 需要が確定しなければ観察（分類軸は昇格側と同じ）。"""
    subject = _subject(
        _above_line_month(month=_PREVIOUS_MONTH),
        _above_line_month(code=None),
        credit=_CREDIT_DISABLED,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.OBSERVE
    assert decision.seat_action is SeatAction.NONE
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.DATA_CONFIDENCE_LOW,
    )


def test_below_the_line_keeps_the_seat_and_proposes_nothing(cfg):
    """需要が方針線に届かなければ、クレジットの付与も提案しない。"""
    subject = _subject(_below_line_month(), credit=_CREDIT_DISABLED)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == ()


@pytest.mark.parametrize(("total", "candidate"), [(450.0, True), (449.99, False)])
def test_disabled_credit_policy_line_boundary(cfg, total, candidate):
    """方針線ちょうどの需要は候補、$0.01 足りなければ候補にしない。"""
    subject = _subject(_month(total=total, code=10.0), credit=_CREDIT_DISABLED)
    decision = decide_upgrade(subject, cfg)
    assert (decision.credit_action is CreditAction.ENABLE_WITH_CAP) is candidate


@pytest.mark.parametrize("code", [None, 0.0, 99.0])
def test_the_credit_candidate_does_not_pass_through_the_code_gate(cfg, code):
    """クレジットの付与に分類軸はかけない（込み枠は product 共通で、付与は可逆）。"""
    subject = _subject(_above_line_month(code=code), credit=_CREDIT_DISABLED)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.ENABLE_WITH_CAP
    assert ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE in decision.reason_codes


def test_credit_candidate_reasons_are_in_fixed_order(cfg):
    """主理由（方針線）→ 情報（混在利用）の順。"""
    subject = _subject(
        _above_line_month(supplementary=True), credit=_CREDIT_DISABLED
    )
    assert decide_upgrade(subject, cfg).reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )


def test_a_partial_month_breaks_the_run_and_leaves_a_credit_candidate(cfg):
    """継続の走査は不完全月で打ち切る（`SUSTAINED_OVERAGE` の走査と同じ規則）。"""
    subject = _subject(
        _above_line_month(month="2026-05"),
        _above_line_month(month=_PREVIOUS_MONTH, complete=False),
        _above_line_month(),
        credit=_CREDIT_DISABLED,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.ENABLE_WITH_CAP
    assert ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE not in (
        decision.reason_codes
    )


def test_a_month_below_the_line_breaks_the_run(cfg):
    """前月が方針線未満なら継続にならない（1ヶ月のスパイクで席を変えない）。"""
    subject = _subject(
        _below_line_month(month=_PREVIOUS_MONTH),
        _above_line_month(),
        credit=_CREDIT_DISABLED,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.ENABLE_WITH_CAP


# --------------------------------------------------- 月内の消費の継続上昇（κ 無効）


def test_rising_credit_consumption_counts_as_continuity(cfg):
    """対象月内の消費が継続上昇していれば、1完全月でも継続として扱う（§12.6）。"""
    subject = _subject(
        _above_line_month(), credit=_CREDIT_DISABLED, points=_RISING_POINTS
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CREDIT_CONSUMPTION_RISING,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    "points",
    [
        _RISING_POINTS[:2],
        (_point(8, 10.0), _point(15, 30.0), _point(22, 30.0)),
        (_point(8, 10.0), _point(15, 30.0), _point(22, 20.0)),
        (_point(8, 10.0, month=6), _point(15, 30.0), _point(22, 60.0)),
    ],
    ids=["点が2つ", "横ばいを含む", "下がる区間を含む", "対象月の点が2つ"],
)
def test_credit_consumption_is_not_rising(cfg, points):
    """上昇と数えない並び。

    3点未満は傾向と呼べず、横ばいは消費が止まった状態と区別できない。当月消費は月次で
    リセットされる値なので、前月の点は対象月の並びに入れない。
    """
    subject = _subject(_above_line_month(), credit=_CREDIT_DISABLED, points=points)
    decision = decide_upgrade(subject, cfg)
    assert decision.credit_action is CreditAction.ENABLE_WITH_CAP
    assert ReasonCode.CREDIT_CONSUMPTION_RISING not in decision.reason_codes


def test_rising_credit_consumption_alone_is_not_a_candidate(cfg):
    """消費が上昇していても、需要が方針線に届かなければ現状維持のまま。"""
    subject = _subject(
        _below_line_month(), credit=_CREDIT_DISABLED, points=_RISING_POINTS
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == ()


# ------------------------------------------- κ 無効の記入と実課金の観測が矛盾する場合


def test_disabled_credit_with_billing_is_treated_as_unknown(cfg):
    """κ=0 の記入と実課金の観測が矛盾していれば、κ 不明として扱う。

    記入ミスか月中の設定変更なので、実課金という観測を捨てない。見るのは対象月の
    実課金だけ（前月の課金は設定変更より前のものでありうる）。
    """
    subject = _subject(
        _month(total=300.0, code=300.0, billed=130.0), credit=_CREDIT_DISABLED
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


def test_disabled_credit_with_small_billing_is_treated_as_unknown(cfg):
    """矛盾しているときは方針線での候補化も κ 不明の規則に従う（席は動かさない）。"""
    subject = _subject(
        _month(total=500.0, code=300.0, billed=5.0), credit=_CREDIT_DISABLED
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
    )


def test_disabled_credit_with_billing_only_in_a_previous_month_stays_disabled(cfg):
    """矛盾の判定に使うのは対象月の実課金だけ（前月の課金は設定変更より前のものでありうる）。

    前月に課金があっても対象月が $0 なら κ 無効の規則のまま進み、2完全月連続の方針線超えで
    昇格候補になる。REVIEW も CREDIT_SETTING_UNKNOWN も付かない。
    """
    subject = _subject(
        _month(month=_PREVIOUS_MONTH, total=500.0, code=300.0, billed=130.0),
        _month(total=500.0, code=300.0),
        credit=_CREDIT_DISABLED,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


# ------------------------------------------------------------- κ 不明（設定の確認）


def test_unknown_credit_setting_above_the_line_asks_for_a_review(cfg):
    """受け入れ確認: credit 設定が不明なら金額を断定せず REVIEW にする。

    上限も有効・無効も分からないので、付与（ENABLE_WITH_CAP）は出さない。実課金 $0 が
    「枠内に収まっている」のか「そもそも課金されない設定」なのか決められないため、席は
    動かさず設定の確認だけを作業として出す。
    """
    subject = _subject(_above_line_month(total=600.0), credit=_CREDIT_UNKNOWN)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
    )


@pytest.mark.parametrize("code", [300.0, 0.0, None])
def test_unknown_credit_setting_above_the_line_does_not_consult_code(cfg, code):
    """方針線だけで候補になった場合、Code 需要は結論を変えない（席を動かさないため）。"""
    subject = _subject(_above_line_month(code=code), credit=_CREDIT_UNKNOWN)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
    )


def test_unknown_credit_setting_below_the_line_asks_for_nothing(cfg):
    """需要が方針線に届かないユーザには REVIEW を出さない。

    設定の不明だけで人へ回すと、確認すべき相手が組織の全員になる。
    """
    subject = _subject(
        _month(total=449.99, code=10.0), credit=_CREDIT_UNKNOWN
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.KEEP
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.NONE
    assert decision.reason_codes == ()


def test_unknown_credit_setting_moves_the_seat_on_observed_billing(cfg):
    """実課金がシート差額を上回っていれば、κ が不明でも席の判定へ進む。

    方針線（$450）を上回る需要でもあるが、候補化の信号になったのは実課金なので
    `TOTAL_DEMAND_ABOVE_PREMIUM_LINE` は添えない。
    """
    subject = _subject(_code_heavy_month(), credit=_CREDIT_UNKNOWN)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )


def test_unknown_credit_setting_reviews_the_assignment_for_low_code(cfg):
    """実課金で候補になり中身が Code でなければ、見直しとクレジット確認の両方を出す。"""
    subject = _subject(
        _month(total=200.0, code=10.0, billed=120.0), credit=_CREDIT_UNKNOWN
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.REVIEW_ASSIGNMENT
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.REVIEW_NON_CODE_USAGE,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
    )


def test_unknown_credit_setting_with_billing_below_the_gap(cfg):
    """実課金がシート差額に届かなければ席は動かさず、方針線での確認だけになる。"""
    subject = _subject(_above_line_month(billed=50.0), credit=_CREDIT_UNKNOWN)
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.KEEP
    assert decision.credit_action is CreditAction.REVIEW
    assert decision.reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
    )


@pytest.mark.parametrize("billed", [0.0, 95.0, 120.0, 1000.0])
def test_unknown_credit_setting_never_reports_a_reached_limit(cfg, billed):
    """上限が分からなければ到達も判定できない（実課金がいくらでも添えない）。"""
    subject = _subject(
        _above_line_month(billed=billed), credit=_CREDIT_UNKNOWN
    )
    decision = decide_upgrade(subject, cfg)
    assert ReasonCode.CREDIT_LIMIT_REACHED not in decision.reason_codes


def test_status_is_recommended_when_only_the_credit_side_has_work(cfg):
    """シート側が観察でも、クレジット側に作業があれば status は RECOMMENDED になる。

    κ が不明で実課金の候補だが Code 需要が確定しないユーザは、シート判定は保留
    （seat_action は NONE で DATA_CONFIDENCE_LOW）だが、設定を確認する作業が残る。
    """
    subject = _subject(_code_heavy_month(code=None), credit=_CREDIT_UNKNOWN)
    decision = decide_upgrade(subject, cfg)
    assert decision.status is DecisionStatus.RECOMMENDED
    assert decision.seat_action is SeatAction.NONE
    assert decision.credit_action is CreditAction.REVIEW
    assert ReasonCode.DATA_CONFIDENCE_LOW in decision.reason_codes


def test_credit_reasons_are_in_fixed_order(cfg):
    """主理由（設定不明）→ 補助（継続超過・実課金の超過・消費上昇）→ 情報 の順。"""
    subject = _subject(
        _month(month=_PREVIOUS_MONTH, total=250.0, code=200.0, billed=130.0),
        _month(total=250.0, code=200.0, billed=130.0, supplementary=True),
        credit=_CREDIT_UNKNOWN,
        points=_RISING_POINTS,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.reason_codes == (
        ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
        ReasonCode.SUSTAINED_OVERAGE,
        ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
        ReasonCode.CREDIT_CONSUMPTION_RISING,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
        ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    )
    # 同じコードを2度出さない（月をまたいだ突き合わせのキーになるため）
    assert len(set(decision.reason_codes)) == len(decision.reason_codes)


@pytest.mark.parametrize("credit", [_CREDIT_UNKNOWN, _CREDIT_DISABLED])
@pytest.mark.parametrize(
    ("conflict", "complete", "events"),
    [
        (True, True, ()),
        (False, False, ()),
        (False, True, (_event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),)),
    ],
    ids=["identity conflict", "部分月", "直近のシート変更"],
)
def test_hard_blockers_are_not_overridden_by_the_credit_comparator(
    cfg, credit, conflict, complete, events
):
    """hard blocker と recent 窓は κ の状態に依らず先に確定する（判定順は不変）。"""
    subject = _subject(
        _above_line_month(complete=complete),
        credit=credit,
        conflict=conflict,
        events=events,
    )
    decision = decide_upgrade(subject, cfg)
    assert decision.seat_action is SeatAction.NONE
    assert decision.credit_action is CreditAction.NONE
    assert decision.status in (DecisionStatus.NO_DECISION, DecisionStatus.OBSERVE)


# ------------------------------------------------------------------- 方針感度（F4）
#
# 方針線を ±$100 ずらした3通りで判定し、基準と同じ seat_action になった数を返す
# （基準を含むので 1〜3）。結線は Step 19 で、いまは公開関数として置いてある。


def _stability_subject(total: float, **kwargs) -> SubjectHistory:
    """降格側の方針感度を見る履歴（2完全月・Code 低・実課金なし・混在利用なし）。"""
    return _premium_subject(
        _low_demand_month(month=_PREVIOUS_MONTH, total=total),
        _low_demand_month(total=total),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("total", "agreeing"),
    [
        (200.0, 3),   # ずらした3通りとも線を下回る
        (400.0, 2),   # −$100（線 $350）だけ線以上になる
        (500.0, 2),   # +$100（線 $550）だけ線を下回る
    ],
)
def test_policy_stability_counts_agreeing_lines(cfg, total, agreeing):
    assert policy_stability(_stability_subject(total), cfg) == agreeing


def test_policy_stability_works_for_the_upgrade_side(cfg):
    """Standard の履歴では昇格の規則で評価する。

    需要 $400・κ 無効の2完全月は、基準（線 $450）と +$100 では現状維持、−$100（線 $350）
    では2完全月連続の成立で昇格候補になる。
    """
    subject = _subject(
        _month(month=_PREVIOUS_MONTH, total=400.0, code=200.0),
        _month(total=400.0, code=200.0),
        credit=_CREDIT_DISABLED,
    )
    assert policy_stability(subject, cfg) == 2


@pytest.mark.parametrize(
    "subject",
    [
        _premium_subject(*_downgrade_months(), conflict=True),
        _premium_subject(
            _low_demand_month(month=_PREVIOUS_MONTH),
            _low_demand_month(complete=False),
        ),
        _premium_subject(_low_demand_month()),
        _premium_subject(*_downgrade_months(), events=(
            _event(dt.date(2026, 7, 5), dt.date(2026, 7, 20)),)),
    ],
    ids=["identity conflict", "部分月", "履歴不足", "直近のシート変更"],
)
def test_policy_stability_is_none_when_the_line_is_not_consulted(cfg, subject):
    """経済軸に到達しない判定では None（「3/3 で安定」と区別する）。"""
    assert policy_stability(subject, cfg) is None


@pytest.mark.parametrize("seat", ["unassigned", "unknown", ""])
def test_policy_stability_rejects_other_seats(cfg, seat):
    """判定できる現シート以外の振り分けは呼び出し側の責務（decide_* と同じ流儀）。"""
    subject = _subject(*_downgrade_months(), seat=seat)
    with pytest.raises(ValueError, match="current_seat"):
        policy_stability(subject, cfg)


def test_policy_stability_is_pure(cfg):
    """同じ入力からは同じ値を返し、渡された config を書き換えない。"""
    before = copy.deepcopy(cfg)
    subject = _stability_subject(400.0)
    assert policy_stability(subject, cfg) == policy_stability(subject, cfg)
    assert cfg == before


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


def test_credit_points_default_to_empty():
    """点を渡さない履歴は空（消費がゼロだった、ではない）。"""
    assert _subject(_month()).credit_points == ()


def test_credit_points_are_stored_as_a_tuple():
    assert _subject(_month(), points=[_point(8, 10.0)]).credit_points == (
        _point(8, 10.0),
    )


@pytest.mark.parametrize(
    "points",
    [
        (_point(15, 10.0), _point(8, 20.0)),   # 降順
        (_point(8, 10.0), _point(8, 20.0)),    # 同じ取得日
    ],
)
def test_credit_points_require_ascending_unique_dates(points):
    """上昇の判定は並び順で行うので、並びと一意性を構築時に確かめる。"""
    with pytest.raises(ValueError, match="credit_points"):
        _subject(_month(), points=points)


def test_credit_points_reject_other_element_types():
    with pytest.raises(TypeError, match="credit_points"):
        _subject(_month(), points=(dt.date(2026, 7, 8),))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_credit_point_rejects_non_finite_amounts(value):
    """非有限値は比較を黙って偽にし、上昇の判定を変えてしまう。"""
    with pytest.raises(ValueError, match="mtd_usd"):
        CreditPoint(taken_on=dt.date(2026, 7, 8), mtd_usd=value)


@pytest.mark.parametrize("value", [-0.01, -30.0])
def test_credit_point_rejects_a_negative_amount(value):
    """累計の当月消費が負になる状態は無い（上流の正準 loader も負値を不明へ倒す）。

    受理すると -30→-20→-10 のような点列が「継続上昇」になる。減少しうるのは点と点の差で、
    点そのものではない。
    """
    with pytest.raises(ValueError, match="mtd_usd"):
        CreditPoint(taken_on=dt.date(2026, 7, 8), mtd_usd=value)


@pytest.mark.parametrize(
    "taken_on",
    [dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.UTC), "2026-07-08", None],
)
def test_credit_point_requires_a_date(taken_on):
    """時刻を持つ datetime は比較と月の判定が変わるため受けない。"""
    with pytest.raises(TypeError, match="taken_on"):
        CreditPoint(taken_on=taken_on, mtd_usd=10.0)
