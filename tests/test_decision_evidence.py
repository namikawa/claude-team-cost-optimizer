"""V2 判定の結線（decision_evidence.evaluate）と decision-evidence.csv のテスト。

判定の規則そのものは tests/test_decision_v2.py が固定する。ここで確かめるのは、分析結果
から判定エンジンへ渡す材料の組み立て（履歴の切り出し・欠けた月の観測・加入の考慮・
帰属・Identity の解決）と、その結果を CSV へ直列化するときの書式。

入力は conftest のヘルパで組み立てた合成データだけを使う。
"""

import csv
import math
from pathlib import Path

import pytest

from seat_analyzer import decision_evidence, seat_changes
from seat_analyzer.analyze import analyze
from seat_analyzer.decision_v2 import DecisionV2
from seat_analyzer.domain import CreditAction, DecisionStatus, ReasonCode, SeatAction
from seat_analyzer.report.evidence_csv import EVIDENCE_COLUMNS, write_decision_evidence

from .conftest import spend_row

# 追加クレジット上限を書く members-info のヘッダ（init-org が作る列構成と同じ）
_INFO_HEADER = "email,部署,チーム,職種,追加クレジット上限,備考"

# シート変更を検出できない組織（members スナップショットのペアが無い）の空の結果
_NO_CHANGES = seat_changes.SeatChanges(events=[], unclassified=[])


def _write_info(input_dir: Path, rows: list[str]) -> None:
    """追加クレジット上限だけを記入した members-info.csv を置く。"""
    (input_dir / "members-info.csv").write_text(
        _INFO_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8",
    )


def _with_uuid(email: str, cost: float, uuid: str, **kwargs) -> str:
    """Account UUID を差し替えたスペンド行（conftest の行はどれも同じ UUID を持つ）。"""
    return spend_row(email, cost, **kwargs).replace("uuid-x", uuid, 1)


def _write_uuid_snapshots(input_dir: Path, snapshots: dict[str, list[str]]) -> None:
    """Account UUID 列つきの members 単日スナップショットを置く。

    conftest の `write_member_snapshots` は `Email,Seat Type` 固定なので、members 側にも
    stable ID がある組織（email の改名を追跡できる形）はここで組む。
    """
    directory = input_dir / "members"
    directory.mkdir(parents=True, exist_ok=True)
    for date, rows in snapshots.items():
        (directory / f"members-snap-{date}.csv").write_text(
            "Email,Account UUID,Seat Type\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )


def _evaluate(input_dir: Path, month: str, cfg: dict):
    """analyze（材料つき）→ シート変更の検出 → 判定。行と email 索引を返す。"""
    result = analyze(input_dir, month, cfg, org="org-a", decision_context=True)
    changes = seat_changes.detect_from_input(input_dir, cfg)
    rows = decision_evidence.evaluate(result, changes, cfg)
    return rows, {row.email: row for row in rows}


# ------------------------------------------------------------------ 対象と振り分け


def test_rows_cover_members_and_spend_in_email_order(make_input, cfg):
    """行の集合は members ∪ 対象月の spend（V1 の判定テーブルと同じ）で email 昇順。"""
    input_dir = make_input(
        {"2026-06": [spend_row("used@x.jp", 10.0), spend_row("orphan@x.jp", 8.0)]},
        members=["used@x.jp,Standard", "idle@x.jp,Premium", "off@x.jp,Unassigned"],
    )
    rows, by_email = _evaluate(input_dir, "2026-06", cfg)

    assert set(by_email) == {"idle@x.jp", "off@x.jp", "orphan@x.jp", "used@x.jp"}
    assert [row.email for row in rows] == sorted(by_email)
    # 利用ゼロの Premium メンバーにも行がある（遊休の最有力候補なので落とさない）
    assert by_email["idle@x.jp"].total_demand_usd == 0.0

    orphan = by_email["orphan@x.jp"]   # members に居ない利用者
    assert orphan.current_seat == "unknown"
    assert orphan.decision.status is DecisionStatus.NO_DECISION
    assert orphan.decision.reason_codes == (ReasonCode.CURRENT_SEAT_UNKNOWN,)
    assert orphan.policy_stability is None

    off = by_email["off@x.jp"]         # 意図的な未割当は判定対象外
    assert off.decision.status is DecisionStatus.EXCLUDED
    assert off.decision.seat_action is SeatAction.NONE
    assert off.decision.reason_codes == ()
    assert off.policy_stability is None


def test_an_unexpected_current_seat_is_an_error(make_input, cfg):
    """振り分けられない現シートは黙って除外せずエラーにする。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"])
    result = analyze(input_dir, "2026-06", cfg, org="org-a", decision_context=True)
    result.users.loc[0, "current_seat"] = "gold"
    with pytest.raises(ValueError, match="判定を振り分けられない現シート"):
        decision_evidence.evaluate(result, _NO_CHANGES, cfg)


def test_evaluate_requires_the_decision_context(make_input, cfg):
    """材料を組んでいない分析結果（既定の analyze）では判定しない。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert result.decision_context is None
    with pytest.raises(ValueError, match="V2 判定の材料がありません"):
        decision_evidence.evaluate(result, _NO_CHANGES, cfg)


def test_evaluate_is_deterministic(make_input, cfg):
    """同じ入力からは同じ行・同じ並びを返す。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)],
         "2026-06": [spend_row("a@x.jp", 12.0), spend_row("b@x.jp", 500.0, net=0.0)]},
        members=["a@x.jp,Standard", "b@x.jp,Premium", "c@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a", decision_context=True)
    changes = seat_changes.detect_from_input(input_dir, cfg)
    assert decision_evidence.evaluate(result, changes, cfg) == \
        decision_evidence.evaluate(result, changes, cfg)


def test_policy_line_comes_from_the_config(make_input, cfg):
    """判定に使った方針線を全行が持つ（あとから線の位置を突き合わせられるように）。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"])
    rows, _ = _evaluate(input_dir, "2026-06", cfg)
    expected = float(cfg["decision_v2"]["premium_justification_usd"])
    assert [row.premium_justification_usd for row in rows] == [expected]


# ------------------------------------------------------------------ 履歴の組み立て


def test_idle_premium_over_two_complete_months_is_downgraded(make_input, cfg):
    """2完全月とも需要ゼロの Premium は降格推奨（方針線の置き場所に依存しない）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("busy@x.jp", 10.0)],
         "2026-06": [spend_row("busy@x.jp", 12.0)]},
        members=["busy@x.jp,Standard", "idle@x.jp,Premium"],
    )
    _, by_email = _evaluate(input_dir, "2026-06", cfg)
    row = by_email["idle@x.jp"]
    assert row.complete is True
    assert row.complete_months == ("2026-05", "2026-06")
    assert row.decision.status is DecisionStatus.RECOMMENDED
    assert row.decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD
    assert row.decision.reason_codes == (
        ReasonCode.SUSTAINED_LOW_CODE_DEMAND,
        ReasonCode.SUSTAINED_LOW_TOTAL_DEMAND,
        ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    )
    assert row.policy_stability == 3


def test_month_without_a_spend_row_counts_as_zero_usage(make_input, cfg):
    """spend に行の無い月は「利用ゼロの観測」として履歴に入る（欠損にしない）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("busy@x.jp", 10.0)],
         "2026-06": [spend_row("busy@x.jp", 12.0), spend_row("late@x.jp", 30.0, net=0.0)]},
        members=["busy@x.jp,Standard", "late@x.jp,Premium"],
    )
    _, by_email = _evaluate(input_dir, "2026-06", cfg)
    row = by_email["late@x.jp"]
    # 2026-05 は行が無いが完全月として数える（数えないと履歴不足で判定が止まる）
    assert row.complete_months == ("2026-05", "2026-06")
    assert row.total_demand_usd == pytest.approx(30.0, abs=0.01)
    assert row.billed_extra_usd == 0.0
    assert row.decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


def test_a_gap_in_the_history_cuts_the_older_months(make_input, cfg):
    """暦で連続しない月は履歴に入れない（欠けた月をまたいだ隣接を作らない）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("busy@x.jp", 10.0)],
         "2026-07": [spend_row("busy@x.jp", 12.0)]},
        members=["busy@x.jp,Standard", "idle@x.jp,Premium"],
        members_month="2026-07",
    )
    _, by_email = _evaluate(input_dir, "2026-07", cfg)
    row = by_email["idle@x.jp"]
    assert row.complete_months == ("2026-07",)
    assert row.decision.status is DecisionStatus.NO_DECISION
    assert row.decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)
    assert row.policy_stability is None


def test_partial_target_month_blocks_the_decision(make_snapshots, cfg):
    """対象月が部分月なら判定しない（月額前提の判定が歪むため）。"""
    input_dir = make_snapshots(
        "2026-08",
        {"2026-08-15": [spend_row("std@x.jp", 300.0, net=0.0),
                        spend_row("prem@x.jp", 10.0, net=0.0)]},
        members=["std@x.jp,Standard", "prem@x.jp,Premium"],
    )
    rows, _ = _evaluate(input_dir, "2026-08", cfg)
    assert [row.current_seat for row in rows] == ["premium", "standard"]
    for row in rows:
        assert row.complete is False
        assert row.complete_months == ()
        assert row.decision.status is DecisionStatus.NO_DECISION
        assert row.decision.reason_codes == (ReasonCode.PARTIAL_MONTH,)
        assert row.policy_stability is None


def test_a_join_before_the_target_month_shortens_the_history(
    make_input, write_member_snapshots, cfg
):
    """加入 event があれば、加入前の月は履歴から外し、加入がまたがる月は完全月に数えない。

    members スナップショットに Account UUID が無いため、seat_changes が返す subject_id は
    email 由来（`email:bob@x.jp`）になる。ここでの加入者は spend に行が無くこちら側の
    subject_id も同じ email 由来なので、subject_id で同じ人に当たる（spend に行がある
    加入者は `account:` で解けて一致しないため email へ落ちる。それは次のテストで固定する）。
    """
    input_dir = make_input({
        "2026-05": [spend_row("alice@x.jp", 5.0, net=0.0)],
        "2026-06": [spend_row("alice@x.jp", 5.0, net=0.0)],
        "2026-07": [spend_row("alice@x.jp", 5.0, net=0.0)],
    })
    write_member_snapshots(input_dir, {
        "2026-06-10": ["alice@x.jp,Premium"],
        "2026-06-20": ["alice@x.jp,Premium", "bob@x.jp,Premium"],
        "2026-07-05": ["alice@x.jp,Premium", "bob@x.jp,Premium"],
        "2026-07-20": ["alice@x.jp,Premium", "bob@x.jp,Premium", "carol@x.jp,Standard"],
    })
    _, by_email = _evaluate(input_dir, "2026-07", cfg)

    alice = by_email["alice@x.jp"]     # 加入 event が無い在籍者は全月が履歴に入る
    assert alice.complete_months == ("2026-05", "2026-06", "2026-07")
    assert alice.decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD

    # 06-10→06-20 の加入: 2026-05 は履歴から外れ、2026-06 は不完全月になる。
    # どちらか片方でも完全月として残れば降格の評価窓（2完全月）が成立してしまう
    bob = by_email["bob@x.jp"]
    assert bob.complete_months == ("2026-07",)
    assert bob.decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)

    carol = by_email["carol@x.jp"]    # 対象月内の加入は recent 窓が拾う
    assert carol.decision.status is DecisionStatus.OBSERVE
    assert carol.decision.reason_codes == (ReasonCode.RECENT_MEMBER,)
    assert carol.policy_stability is None


def test_a_join_on_the_last_day_of_the_previous_month_drops_that_month(
    make_input, write_member_snapshots, cfg
):
    """`end(M) <= changed_after` の等号側: 前月の末日に不在なら、その月は履歴に入らない。

    履歴から外した月と不完全月にした月は、判定に効く範囲では同じ（どちらも完全月に
    数えない）ため行の列では区別できない。組み立てた観測そのものを見て等号側を固定する。
    """
    input_dir = make_input({
        "2026-05": [spend_row("anchor@x.jp", 5.0, net=0.0)],
        "2026-06": [spend_row("anchor@x.jp", 5.0, net=0.0)],
    })
    write_member_snapshots(input_dir, {
        "2026-05-31": ["anchor@x.jp,Premium"],
        "2026-06-05": ["anchor@x.jp,Premium", "bob@x.jp,Premium"],
    })
    result = analyze(input_dir, "2026-06", cfg, org="org-a", decision_context=True)
    changes = seat_changes.detect_from_input(input_dir, cfg)
    rows = {row.email: row for row in decision_evidence.evaluate(result, changes, cfg)}
    bob = rows["bob@x.jp"]

    shared = decision_evidence._shared(result, changes, cfg)
    events, _ = decision_evidence._attributed(shared, bob.email, bob.subject_id)
    months = decision_evidence._observations(bob.email, shared, events)
    assert [month.month for month in months] == ["2026-06"]
    assert bob.complete_months == ("2026-06",)


def test_a_join_ending_on_the_first_day_keeps_that_month_complete(
    make_input, write_member_snapshots, cfg
):
    """`start(M) < changed_before` の等号側: 加入区間が月初日で終わる月は完全月のまま。"""
    input_dir = make_input({
        "2026-05": [spend_row("anchor@x.jp", 5.0, net=0.0)],
        "2026-06": [spend_row("anchor@x.jp", 5.0, net=0.0)],
        "2026-07": [spend_row("anchor@x.jp", 5.0, net=0.0)],
    })
    write_member_snapshots(input_dir, {
        "2026-05-25": ["anchor@x.jp,Premium"],
        "2026-06-01": ["anchor@x.jp,Premium", "bob@x.jp,Premium"],
        "2026-07-20": ["anchor@x.jp,Premium", "bob@x.jp,Premium"],
    })
    _, by_email = _evaluate(input_dir, "2026-07", cfg)
    bob = by_email["bob@x.jp"]
    # 2026-06 は加入が月初日までに完了しているので完全月。2026-05 は加入区間が
    # またがるため不完全月になり、完全月には数えない
    assert bob.complete_months == ("2026-06", "2026-07")
    assert bob.decision.seat_action is SeatAction.DOWNGRADE_TO_STANDARD


def test_an_event_without_a_stable_id_is_attributed_by_email(
    make_input, write_member_snapshots, cfg
):
    """members に stable ID が無い組織では email で帰属させる。

    その場合 seat_changes の subject_id は email 由来（`email:bob@x.jp`）になり、対象月の
    spend から解いたこちらの subject_id（`account:` 由来）とは一致しないため、
    subject_id では引けない。
    """
    input_dir = make_input({
        "2026-05": [spend_row("alice@x.jp", 5.0, net=0.0)],
        "2026-06": [spend_row("alice@x.jp", 5.0, net=0.0)],
        "2026-07": [spend_row("alice@x.jp", 5.0, net=0.0),
                    spend_row("bob@x.jp", 5.0, net=0.0)],
    })
    write_member_snapshots(input_dir, {
        "2026-06-10": ["alice@x.jp,Premium"],
        "2026-06-20": ["alice@x.jp,Premium", "bob@x.jp,Premium"],
        "2026-07-20": ["alice@x.jp,Premium", "bob@x.jp,Premium"],
    })
    result = analyze(input_dir, "2026-07", cfg, org="org-a", decision_context=True)
    changes = seat_changes.detect_from_input(input_dir, cfg)
    assert [event.subject_id for event in changes.events] == ["email:bob@x.jp"]
    by_email = {
        row.email: row for row in decision_evidence.evaluate(result, changes, cfg)
    }

    bob = by_email["bob@x.jp"]
    assert bob.subject_id.startswith("account:")   # spend 側の stable ID で解けている
    # email で加入 event に当たり、2026-05 は履歴から外れ 2026-06 は完全月に数えない
    assert bob.complete_months == ("2026-07",)
    assert bob.decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)
    # 他人の event は付かない（同じ stable ID を共有していても email で分かれる）
    assert by_email["alice@x.jp"].complete_months == (
        "2026-05", "2026-06", "2026-07")


# --------------------------------------------------- 帰属と Identity（stable ID つき）


def test_an_event_falls_back_to_the_subject_id_when_the_email_is_gone(make_input, cfg):
    """改名前の email が対象ユーザに無い event は subject_id で帰属させる。"""
    input_dir = make_input({
        "2026-05": [_with_uuid("anchor@x.jp", 5.0, "uuid-0", net=0.0)],
        "2026-06": [_with_uuid("anchor@x.jp", 5.0, "uuid-0", net=0.0)],
        "2026-07": [_with_uuid("anchor@x.jp", 5.0, "uuid-0", net=0.0),
                    _with_uuid("new@x.jp", 5.0, "uuid-9", net=0.0)],
    })
    _write_uuid_snapshots(input_dir, {
        "2026-06-10": ["anchor@x.jp,uuid-0,Premium"],
        "2026-06-20": ["anchor@x.jp,uuid-0,Premium", "old@x.jp,uuid-9,Premium"],
        "2026-07-20": ["anchor@x.jp,uuid-0,Premium", "new@x.jp,uuid-9,Premium"],
    })
    _, by_email = _evaluate(input_dir, "2026-07", cfg)

    assert "old@x.jp" not in by_email        # 改名前の email は対象月の members に無い
    row = by_email["new@x.jp"]
    assert row.subject_id == "account:uuid-9"
    # 加入 event の email（old@）は対象ユーザに無いので subject_id で帰属し、加入前の
    # 2026-05 が履歴から外れ、加入がまたがる 2026-06 は完全月に数えない
    assert row.complete_months == ("2026-07",)
    assert row.decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)


def test_a_stable_id_wins_over_a_reused_email(make_input, cfg):
    """stable ID が対象ユーザを指す event は、email が再割当されていてもその人へ帰属する。

    email で引くと、加入 event が新しい持ち主へ付け替わり、stable ID が指す本人から
    加入の歯止めが外れる（在籍していなかった月の利用ゼロが完全月として数えられる）。
    1つの event が帰属するのは1人だけであることも併せて固定する。
    """
    input_dir = make_input({
        "2026-05": [_with_uuid("anchor@x.jp", 5.0, "uuid-0", net=0.0)],
        "2026-06": [_with_uuid("anchor@x.jp", 5.0, "uuid-0", net=0.0)],
        "2026-07": [_with_uuid("anchor@x.jp", 5.0, "uuid-0", net=0.0),
                    _with_uuid("renamed@x.jp", 5.0, "uuid-old", net=0.0),
                    _with_uuid("reused@x.jp", 5.0, "uuid-new", net=0.0)],
    })
    _write_uuid_snapshots(input_dir, {
        "2026-06-10": ["anchor@x.jp,uuid-0,Premium"],
        "2026-06-20": ["anchor@x.jp,uuid-0,Premium", "reused@x.jp,uuid-old,Premium"],
        "2026-07-05": ["anchor@x.jp,uuid-0,Premium", "renamed@x.jp,uuid-old,Premium"],
        "2026-07-20": ["anchor@x.jp,uuid-0,Premium", "renamed@x.jp,uuid-old,Premium",
                       "reused@x.jp,uuid-new,Standard"],
    })
    result = analyze(input_dir, "2026-07", cfg, org="org-a", decision_context=True)
    changes = seat_changes.detect_from_input(input_dir, cfg)
    by_email = {
        row.email: row for row in decision_evidence.evaluate(result, changes, cfg)
    }

    # 06-10→06-20 の加入 event は email が reused@ だが subject は account:uuid-old。
    # 現在その stable ID を持つのは renamed@ なので、加入はこちらに帰属する
    renamed = by_email["renamed@x.jp"]
    assert renamed.subject_id == "account:uuid-old"
    assert renamed.complete_months == ("2026-07",)
    assert renamed.decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)

    reused = by_email["reused@x.jp"]         # 再割当された側は自分の加入だけを受け取る
    assert reused.subject_id == "account:uuid-new"
    assert reused.decision.status is DecisionStatus.OBSERVE
    assert reused.decision.reason_codes == (ReasonCode.RECENT_MEMBER,)

    # それぞれの event が帰属するのは1人だけ（同じ event が2人に付かない）
    shared = decision_evidence._shared(result, changes, cfg)
    for row in (renamed, reused):
        events, _ = decision_evidence._attributed(shared, row.email, row.subject_id)
        assert [event.subject_id for event in events] == [row.subject_id]


def test_an_unclassified_observation_is_attributed_by_email(make_input, cfg):
    """同一時点でシートが食い違う観測は、event が無くても観察へ倒す。"""
    input_dir = make_input({
        "2026-06": [_with_uuid("u@x.jp", 5.0, "uuid-u", net=0.0)],
        "2026-07": [_with_uuid("u@x.jp", 5.0, "uuid-u", net=0.0)],
    })
    _write_uuid_snapshots(input_dir, {
        "2026-07-05": ["u@x.jp,uuid-u,Premium"],
        "2026-07-20": ["u@x.jp,uuid-u,Premium", "u@x.jp,uuid-u,Standard"],
    })
    _, by_email = _evaluate(input_dir, "2026-07", cfg)
    row = by_email["u@x.jp"]
    assert row.current_seat == "standard"    # 同じ email の行は最後の1行へ畳まれる
    assert row.decision.status is DecisionStatus.OBSERVE
    assert row.decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


def test_a_conflicting_observation_is_attributed_to_everyone_involved(make_input, cfg):
    """subject を確定できない観測は、関係する email を持つ全員へ帰属させる。

    誰のものか決められない観測なので、関係者をまとめて保留側へ倒す（意図した保守側の
    挙動）。ここでは2時点で Account UUID が入れ替わっており、両方の時点をまとめて解くと
    2つの stable ID が1つの組に入って conflict になる。対象月の Identity は採用した
    members スナップショットと spend の行だけで解くので、そちらは conflict にならない。
    """
    input_dir = make_input({
        "2026-06": [_with_uuid("p@x.jp", 5.0, "uuid-2", net=0.0),
                    _with_uuid("q@x.jp", 5.0, "uuid-1", net=0.0)],
        "2026-07": [_with_uuid("p@x.jp", 5.0, "uuid-2", net=0.0),
                    _with_uuid("q@x.jp", 5.0, "uuid-1", net=0.0)],
    })
    _write_uuid_snapshots(input_dir, {
        "2026-07-05": ["p@x.jp,uuid-1,Premium", "q@x.jp,uuid-2,Premium"],
        "2026-07-20": ["p@x.jp,uuid-2,Premium", "q@x.jp,uuid-1,Premium"],
    })
    changes = seat_changes.detect_from_input(input_dir, cfg)
    assert [observation.reason for observation in changes.unclassified] == [
        "identity_conflict"]
    assert changes.unclassified[0].emails == ("p@x.jp", "q@x.jp")

    _, by_email = _evaluate(input_dir, "2026-07", cfg)
    for email, subject_id in (("p@x.jp", "account:uuid-2"), ("q@x.jp", "account:uuid-1")):
        row = by_email[email]
        assert row.subject_id == subject_id      # 対象月の解決では conflict にならない
        assert row.decision.status is DecisionStatus.OBSERVE
        assert row.decision.reason_codes == (ReasonCode.DATA_CONFIDENCE_LOW,)


def test_a_different_uuid_in_an_earlier_month_is_not_a_conflict(make_input, cfg):
    """Identity は対象月の証拠だけで解く（過去月の別 UUID を conflict にしない）。"""
    input_dir = make_input(
        {"2026-06": [_with_uuid("a@x.jp", 10.0, "uuid-1", net=0.0)],
         "2026-07": [_with_uuid("a@x.jp", 10.0, "uuid-2", net=0.0)]},
        members=["a@x.jp,Standard"], members_month="2026-07",
    )
    _, by_email = _evaluate(input_dir, "2026-07", cfg)
    row = by_email["a@x.jp"]
    assert row.subject_id == "account:uuid-2"
    assert row.identity_quality == "stable"
    assert ReasonCode.IDENTITY_CONFLICT not in row.decision.reason_codes


# ------------------------------------------------------------------ 判定の材料


def test_standard_upgrade_paths_by_credit_setting(make_input, cfg):
    """追加クレジット上限 κ の3状態で、候補化の信号と結論の出し方が変わる。"""
    input_dir = make_input(
        {"2026-06": [
            spend_row("billed@x.jp", 250.0, net=150.0),
            spend_row("unset@x.jp", 500.0, net=0.0),
            spend_row("disabled@x.jp", 500.0, net=0.0),
        ]},
        members=["billed@x.jp,Standard", "unset@x.jp,Standard",
                 "disabled@x.jp,Standard"],
    )
    _write_info(input_dir, ["billed@x.jp,,,,250,", "disabled@x.jp,,,,0,"])
    _, by_email = _evaluate(input_dir, "2026-06", cfg)

    billed = by_email["billed@x.jp"]   # κ 有効: 実課金の観測で候補になり Code 主体
    assert billed.credit_limit_usd == 250.0
    assert billed.code_demand_usd == pytest.approx(250.0, abs=0.01)
    assert billed.decision.seat_action is SeatAction.UPGRADE_TO_PREMIUM
    assert ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP in billed.decision.reason_codes
    assert billed.suggested_credit_cap_usd is None

    unset = by_email["unset@x.jp"]     # κ 不明: 席は動かさず設定の確認へ回す
    assert unset.credit_limit_usd is None
    assert unset.decision.seat_action is SeatAction.KEEP
    assert unset.decision.credit_action is CreditAction.REVIEW
    assert unset.decision.reason_codes == (
        ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
        ReasonCode.CREDIT_SETTING_UNKNOWN,
    )
    assert unset.suggested_credit_cap_usd is None

    disabled = by_email["disabled@x.jp"]   # κ 無効: 上限つき付与で1ヶ月の課金を実測する
    assert disabled.credit_limit_usd == 0.0
    assert disabled.decision.credit_action is CreditAction.ENABLE_WITH_CAP
    assert disabled.suggested_credit_cap_usd == \
        float(cfg["usage_credits"]["grant_suggested_cap_usd"])


def test_unknown_product_leaves_the_code_demand_undetermined(make_input, cfg):
    """product 名が分からない行しか無いユーザの Code 需要は確定しない（0 で埋めない）。"""
    input_dir = make_input(
        {"2026-06": [spend_row("blank@x.jp", 250.0, net=150.0, product="")]},
        members=["blank@x.jp,Standard"],
    )
    _write_info(input_dir, ["blank@x.jp,,,,250,"])
    _, by_email = _evaluate(input_dir, "2026-06", cfg)
    row = by_email["blank@x.jp"]
    assert row.code_demand_usd is None
    assert row.supplementary_high is None
    # 実課金の観測で候補にはなるが、Code 主体を証明できないので自動で昇格させない
    assert row.decision.status is DecisionStatus.OBSERVE
    assert row.decision.seat_action is SeatAction.NONE
    assert ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP in row.decision.reason_codes
    assert ReasonCode.DATA_CONFIDENCE_LOW in row.decision.reason_codes


def test_identity_conflict_blocks_the_decision(make_input, cfg):
    """同じ email に別の stable ID が付いていたら判定しない（波及もさせない）。"""
    input_dir = make_input(
        {"2026-06": [_with_uuid("split@x.jp", 100.0, "uuid-1", net=0.0),
                     _with_uuid("split@x.jp", 100.0, "uuid-2", net=0.0),
                     _with_uuid("solo@x.jp", 10.0, "uuid-3", net=0.0)]},
        members=["split@x.jp,Standard", "solo@x.jp,Standard"],
    )
    _, by_email = _evaluate(input_dir, "2026-06", cfg)

    split = by_email["split@x.jp"]
    assert split.subject_id is None
    assert split.identity_quality == "conflict"
    assert split.decision.status is DecisionStatus.NO_DECISION
    assert split.decision.reason_codes == (ReasonCode.IDENTITY_CONFLICT,)

    solo = by_email["solo@x.jp"]
    assert solo.subject_id == "account:uuid-3"
    assert solo.identity_quality == "stable"
    assert solo.decision.status is DecisionStatus.KEEP


def test_a_member_without_spend_rows_is_unresolved(make_input, cfg):
    """spend に現れないメンバーは stable ID が無いので email 由来の subject になる。"""
    input_dir = make_input(
        {"2026-06": [spend_row("used@x.jp", 10.0)]},
        members=["used@x.jp,Standard", "idle@x.jp,Premium"],
    )
    _, by_email = _evaluate(input_dir, "2026-06", cfg)
    assert by_email["idle@x.jp"].subject_id == "email:idle@x.jp"
    assert by_email["idle@x.jp"].identity_quality == "email_fallback"


# ------------------------------------------------------------------ CSV の書き出し


def _row(**overrides) -> decision_evidence.EvidenceRow:
    """書式の検査用の1行（既定は欠損・無制限・複数値を含む形）。"""
    fields = {
        "email": "user@x.jp",
        "subject_id": "account:uuid-1",
        "identity_quality": "stable",
        "current_seat": "standard",
        "month": "2026-06",
        "complete": True,
        "complete_months": ("2026-05", "2026-06"),
        "total_demand_usd": 123.456,
        "code_demand_usd": None,
        "supplementary_high": None,
        "billed_extra_usd": 0.0,
        "credit_limit_usd": math.inf,
        "premium_justification_usd": 450.0,
        "suggested_credit_cap_usd": None,
        "decision": DecisionV2(
            status=DecisionStatus.OBSERVE,
            seat_action=SeatAction.NONE,
            credit_action=CreditAction.NONE,
            reason_codes=(ReasonCode.PARTIAL_MONTH, ReasonCode.DATA_CONFIDENCE_LOW),
        ),
        "policy_stability": None,
    }
    fields.update(overrides)
    return decision_evidence.EvidenceRow(**fields)


def _cells(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_csv_header_is_the_declared_column_order(tmp_path):
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([_row()], path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle)) == list(EVIDENCE_COLUMNS)


def test_csv_writes_missing_values_as_blank_cells(tmp_path):
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([_row()], path)
    cells = _cells(path)[0]
    assert cells["code_demand_usd"] == ""
    assert cells["supplementary_high"] == ""
    assert cells["policy_stability"] == ""
    assert cells["suggested_credit_cap_usd"] == ""


def test_csv_formats_amounts_flags_and_lists(tmp_path):
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence(
        [_row(code_demand_usd=12.0, supplementary_high=False, policy_stability=2,
              suggested_credit_cap_usd=150.0)],
        path,
    )
    cells = _cells(path)[0]
    assert cells["total_demand_usd"] == "123.46"
    assert cells["code_demand_usd"] == "12.00"
    assert cells["credit_limit_usd"] == "inf"      # 無制限の追加クレジット上限
    assert cells["complete"] == "True"
    assert cells["supplementary_high"] == "False"
    assert cells["policy_stability"] == "2"
    assert cells["suggested_credit_cap_usd"] == "150.00"
    assert cells["complete_months"] == "2026-05;2026-06"
    # 理由コードは判定が返した並びのまま（CSV 側で並べ替えない）
    assert cells["reason_codes"] == "PARTIAL_MONTH;DATA_CONFIDENCE_LOW"
    assert cells["status"] == "observe"
    assert cells["seat_action"] == "none"
    assert cells["credit_action"] == "none"


@pytest.mark.parametrize(
    "email", ["=cmd@x.jp", "+cmd@x.jp", "-cmd@x.jp", "@cmd@x.jp", "\tcmd@x.jp"])
def test_csv_escapes_a_cell_that_looks_like_a_formula(tmp_path, email):
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([_row(email=email)], path)
    assert _cells(path)[0]["email"] == "'" + email


def test_csv_keeps_negative_amounts_unquoted(tmp_path):
    """金額には式のエスケープを掛けない（負の値の "-" が式の先頭文字と一致するため）。"""
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence(
        [_row(total_demand_usd=-1.0, billed_extra_usd=-12.5, code_demand_usd=-0.5)],
        path,
    )
    cells = _cells(path)[0]
    assert cells["total_demand_usd"] == "-1.00"
    assert cells["billed_extra_usd"] == "-12.50"
    assert cells["code_demand_usd"] == "-0.50"
    assert "'" not in path.read_text(encoding="utf-8-sig")


def test_csv_normalizes_newlines_inside_a_cell(tmp_path):
    """セル内の改行も LF に揃える（レコード区切りの指定だけでは CR が残る）。"""
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([_row(email="a@x.jp\r\nb@x.jp\rc@x.jp")], path)
    assert b"\r" not in path.read_bytes()
    assert _cells(path)[0]["email"] == "a@x.jp\nb@x.jp\nc@x.jp"


def test_csv_escapes_a_cr_leading_cell_before_normalizing(tmp_path):
    """CR 始まりのセルは式のエスケープが先に効く。

    改行を先に均すと式の先頭文字と一致しなくなり、引用符が付かないまま出る。
    """
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([_row(email="\r=cmd@x.jp")], path)
    assert b"\r" not in path.read_bytes()
    assert _cells(path)[0]["email"] == "'\n=cmd@x.jp"


def test_csv_is_utf8_sig_with_lf(tmp_path):
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([_row()], path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw and raw.endswith(b"\n")


def test_csv_with_no_rows_still_has_the_header(tmp_path):
    path = tmp_path / "decision-evidence.csv"
    write_decision_evidence([], path)
    assert _cells(path) == []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.reader(handle)) == list(EVIDENCE_COLUMNS)
