"""members スナップショットのペア差分から作るシート変更 event のテスト。

対応付けは email ではなく identity（account_uuid / user_id）に任せる。email が変わっても
同一人物なら変更・加入・離脱として出さない。分類できない遷移（unknown・identity の
conflict・同一時点でのシート食い違い・確定できない不在）は event にせず、理由つきの
未分類観測として残す（「変更なし」と読み違えられないようにするため）。

出力は同じ入力から常に同じ列になること（重複なし・入力順に依らない並び）まで固定する。
"""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from seat_analyzer import seat_changes
from seat_analyzer.seat_changes import (
    EVENT_TYPES,
    IDENTITY_CONFLICT,
    INCONSISTENT_SEAT,
    UNCLASSIFIED_REASONS,
    UNCONFIRMED_ABSENCE,
    UNKNOWN_SEAT,
    MemberSnapshot,
    SeatChangeEvent,
    UnclassifiedObservation,
    detect,
    detect_from_input,
    load_snapshots,
)

# account_uuid 付きの members ヘッダ（stable ID で対応付ける経路を通すため）
HEADER = "Email,Seat Type,Account UUID"


def _snapshot(input_dir: Path, name: str, rows: list[str], header: str = HEADER) -> None:
    """members スナップショット1本を任意のファイル名で置く（行はそのまま書く）。"""
    directory = input_dir / "members"
    directory.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{row}\n" for row in rows)
    (directory / name).write_text(header + "\n" + body, encoding="utf-8")


def _by_date(
    input_dir: Path, snapshots: dict[str, list[str]], header: str = HEADER
) -> Path:
    """{日付 "YYYY-MM-DD": [行, ...]} を日付命名のスナップショットとして置く。"""
    for date, rows in snapshots.items():
        _snapshot(input_dir, f"members-snap-{date}.csv", rows, header)
    return input_dir


def _events(input_dir: Path, cfg: dict) -> list[SeatChangeEvent]:
    return detect_from_input(input_dir, cfg).events


def _unclassified(input_dir: Path, cfg: dict) -> list[UnclassifiedObservation]:
    return detect_from_input(input_dir, cfg).unclassified


def _summary(events: list[SeatChangeEvent]) -> list[tuple[str, str]]:
    """(email, event_type) の列（顔ぶれと種別の突き合わせ用）。"""
    return [(event.email, event.event_type) for event in events]


def _reasons(
    observations: list[UnclassifiedObservation],
) -> list[tuple[str, str | None]]:
    """(理由, subject_id) の列。"""
    return [(item.reason, item.subject_id) for item in observations]


def _assert_unique(changes: seat_changes.SeatChanges) -> None:
    """一意キーの重複が出力に無いこと（event・未分類観測の両方）。"""
    for keys in (
        [event.key for event in changes.events],
        [item.key for item in changes.unclassified],
    ):
        assert len(set(keys)) == len(keys)


# --------------------------------------------------------------------- event の種別


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("Standard", "Premium", "standard_to_premium"),
        ("Premium", "Standard", "premium_to_standard"),
        ("Standard", "Unassigned", "assigned_to_unassigned"),
        ("Premium", "Unassigned", "assigned_to_unassigned"),
        ("Unassigned", "Standard", "unassigned_to_assigned"),
        ("Unassigned", "Premium", "unassigned_to_assigned"),
    ],
)
def test_seat_transition_event_types(cfg, tmp_path, before, after, expected):
    """シートの遷移から種別が決まる（assigned は standard・premium の両方を指す）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [f"a@x.jp,{before},acct-1"],
        "2026-07-16": [f"a@x.jp,{after},acct-1"],
    })
    events = _events(input_dir, cfg)
    assert [event.event_type for event in events] == [expected]
    assert [(event.from_seat, event.to_seat) for event in events] == [
        (before.lower(), after.lower())
    ]


def test_added_and_removed(cfg, tmp_path):
    """片方の時点にしか行が無い subject は加入・離脱になる（不在は absent）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1", "b@x.jp,Premium,acct-2"],
        "2026-07-16": ["a@x.jp,Standard,acct-1", "c@x.jp,Standard,acct-3"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert _summary(changes.events) == [
        ("b@x.jp", "member_removed"),
        ("c@x.jp", "member_added"),
    ]
    assert (changes.events[0].from_seat, changes.events[0].to_seat) == (
        "premium", "absent")
    assert (changes.events[1].from_seat, changes.events[1].to_seat) == (
        "absent", "standard")
    assert [event.subject_id for event in changes.events] == [
        "account:acct-2", "account:acct-3"
    ]
    assert changes.unclassified == []


def test_one_pair_covers_the_whole_vocabulary(cfg, tmp_path):
    """1つのペアから6種すべてが出る（種別の語彙は EVENT_TYPES に閉じる）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [
            "up@x.jp,Standard,acct-1",
            "down@x.jp,Premium,acct-2",
            "off@x.jp,Standard,acct-3",
            "on@x.jp,Unassigned,acct-4",
            "left@x.jp,Premium,acct-5",
        ],
        "2026-07-16": [
            "up@x.jp,Premium,acct-1",
            "down@x.jp,Standard,acct-2",
            "off@x.jp,Unassigned,acct-3",
            "on@x.jp,Premium,acct-4",
            "joined@x.jp,Standard,acct-6",
        ],
    })
    changes = detect_from_input(input_dir, cfg)
    assert {event.event_type for event in changes.events} == set(EVENT_TYPES)
    _assert_unique(changes)


# --------------------------------------------------------------- 分類できない観測


@pytest.mark.parametrize(
    ("before", "after"),
    [("Guest", "Premium"), ("Premium", "Guest"), ("", "Premium"), ("Standard", "")],
)
def test_unknown_seat_is_unclassified(cfg, tmp_path, before, after):
    """unknown を含む遷移は event にせず理由を残す（同じペアの他の subject は出る）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [f"a@x.jp,{before},acct-1", "b@x.jp,Standard,acct-2"],
        "2026-07-16": [f"a@x.jp,{after},acct-1", "b@x.jp,Premium,acct-2"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert _summary(changes.events) == [("b@x.jp", "standard_to_premium")]
    assert _reasons(changes.unclassified) == [(UNKNOWN_SEAT, "account:acct-1")]
    assert changes.unclassified[0].emails == ("a@x.jp",)


def test_unknown_seat_on_both_ends_is_not_reported(cfg, tmp_path):
    """両端が同じ値なら理由を残さない（値の意味は不明でも、変わっていないことは分かる）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Guest,acct-1"],
        "2026-07-16": ["a@x.jp,Guest,acct-1"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert changes.events == []
    assert changes.unclassified == []


def test_identity_conflict_is_unclassified(cfg, tmp_path):
    """同じ email に別の account_uuid が付いた subject は変更の実体を決められない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-2"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert changes.events == []
    assert _reasons(changes.unclassified) == [(IDENTITY_CONFLICT, None)]
    assert changes.unclassified[0].emails == ("a@x.jp",)


def test_conflicting_seats_in_one_snapshot_are_unclassified(cfg, tmp_path):
    """同一時点で同じ subject の行のシートが食い違うなら、現在のシートを決められない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "renamed@x.jp,Standard,acct-1"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert changes.events == []
    assert _reasons(changes.unclassified) == [(INCONSISTENT_SEAT, "account:acct-1")]


def test_same_seat_on_multiple_rows_is_still_judged(cfg, tmp_path):
    """同じ subject の行が複数あってもシートが一致していれば判定できる。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "renamed@x.jp,Premium,acct-1"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert _summary(changes.events) == [("a@x.jp", "standard_to_premium")]
    assert changes.unclassified == []


def test_all_unclassified_reasons_in_one_pair(cfg, tmp_path):
    """4つの理由がそれぞれ独立に出る（語彙は UNCLASSIFIED_REASONS に閉じる）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [
            "u@x.jp,Guest,acct-u",
            "c@x.jp,Standard,acct-c1",
            "i@x.jp,Standard,acct-i",
            ",Standard,",
        ],
        "2026-07-16": [
            "u@x.jp,Premium,acct-u",
            "c@x.jp,Premium,acct-c2",
            "i@x.jp,Premium,acct-i",
            "renamed@x.jp,Standard,acct-i",
            "n@x.jp,Standard,acct-n",
        ],
    })
    changes = detect_from_input(input_dir, cfg)
    assert changes.events == []
    assert {item.reason for item in changes.unclassified} == set(UNCLASSIFIED_REASONS)
    _assert_unique(changes)


def test_unclassified_carries_the_interval_and_sources(cfg, tmp_path):
    """未分類観測は event と同じ区間・由来を持つ（同じ窓の判定に使えるようにする）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-28": ["a@x.jp,Standard,acct-1"],
        "2026-08-04": ["a@x.jp,Premium,acct-2"],
    })
    item = _unclassified(input_dir, cfg)[0]
    assert item.reason == IDENTITY_CONFLICT
    assert item.subject_id is None
    assert item.emails == ("a@x.jp",)
    assert item.changed_after == dt.date(2026, 7, 28)
    assert item.changed_before == dt.date(2026, 8, 4)
    assert item.detected_at == dt.date(2026, 8, 4)
    assert item.previous_source == "members-snap-2026-07-28.csv"
    assert item.current_source == "members-snap-2026-08-04.csv"


# ------------------------------------------------------------ 行を畳まない読み取り


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_email_rows_are_not_folded(cfg, tmp_path, reverse):
    """同一 email の行を畳まないので、食い違いが行順で消えない。"""
    rows = ["a@x.jp,Premium", "a@x.jp,Standard"]
    input_dir = _by_date(
        tmp_path / "input",
        {
            "2026-07-05": ["a@x.jp,Standard"],
            "2026-07-16": list(reversed(rows)) if reverse else rows,
        },
        header="Email,Seat Type",
    )
    changes = detect_from_input(input_dir, cfg)
    assert changes.events == []
    assert _reasons(changes.unclassified) == [(INCONSISTENT_SEAT, "email:a@x.jp")]


def test_duplicate_email_rows_with_the_same_seat_are_judged(cfg, tmp_path):
    """同一 email の行が重複していてもシートが一致していれば通常どおり判定する。"""
    input_dir = _by_date(
        tmp_path / "input",
        {
            "2026-07-05": ["a@x.jp,Standard", "a@x.jp,Standard"],
            "2026-07-16": ["a@x.jp,Premium", "a@x.jp,Premium"],
        },
        header="Email,Seat Type",
    )
    changes = detect_from_input(input_dir, cfg)
    assert _summary(changes.events) == [("a@x.jp", "standard_to_premium")]
    assert changes.unclassified == []


def test_same_email_with_two_stable_ids_in_one_snapshot(cfg, tmp_path):
    """同一時点に同じ email で別の stable ID の行があれば conflict として残す。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "a@x.jp,Premium,acct-2"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert changes.events == []
    assert _reasons(changes.unclassified) == [(IDENTITY_CONFLICT, None)]


def test_rows_without_email_are_independent_subjects(cfg, tmp_path):
    """email の無い行は互いに結合しない（欠損を1つの値として突き合わせない）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [",Standard,acct-1", ",Premium,acct-2"],
        "2026-07-16": [",Premium,acct-1", ",Standard,acct-2"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert [
        (event.subject_id, event.event_type, event.email) for event in changes.events
    ] == [
        ("account:acct-1", "standard_to_premium", ""),
        ("account:acct-2", "premium_to_standard", ""),
    ]
    assert changes.unclassified == []


# ------------------------------------------------------------ 不完全な観測の扱い


def test_empty_snapshot_is_not_an_observation(cfg, tmp_path):
    """データ行が無いスナップショットは観測にしない（全員の離脱にしない）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": [],
        "2026-07-28": ["a@x.jp,Premium,acct-1"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert [snapshot.source for snapshot in load_snapshots(input_dir, cfg)] == [
        "members-snap-2026-07-05.csv",
        "members-snap-2026-07-16.csv",
        "members-snap-2026-07-28.csv",
    ]
    assert _summary(changes.events) == [("a@x.jp", "standard_to_premium")]
    assert changes.events[0].changed_after == dt.date(2026, 7, 5)
    assert changes.events[0].changed_before == dt.date(2026, 7, 28)
    assert changes.events[0].previous_source == "members-snap-2026-07-05.csv"
    assert changes.unclassified == []


def test_unidentifiable_row_suppresses_added_only(cfg, tmp_path):
    """前の時点に不可識別行があると、加入は確定できない（離脱と変更は出る）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [",Standard,", "a@x.jp,Standard,acct-a", "d@x.jp,Standard,acct-d"],
        "2026-07-16": ["a@x.jp,Premium,acct-a", "c@x.jp,Standard,acct-c"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert _summary(changes.events) == [
        ("a@x.jp", "standard_to_premium"),
        ("d@x.jp", "member_removed"),
    ]
    assert _reasons(changes.unclassified) == [(UNCONFIRMED_ABSENCE, "account:acct-c")]


def test_unidentifiable_row_suppresses_removed_only(cfg, tmp_path):
    """後の時点に不可識別行があると、離脱は確定できない（加入と変更は出る）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-a", "d@x.jp,Standard,acct-d"],
        "2026-07-16": [",Standard,", "a@x.jp,Premium,acct-a", "c@x.jp,Standard,acct-c"],
    })
    changes = detect_from_input(input_dir, cfg)
    assert _summary(changes.events) == [
        ("a@x.jp", "standard_to_premium"),
        ("c@x.jp", "member_added"),
    ]
    assert _reasons(changes.unclassified) == [(UNCONFIRMED_ABSENCE, "account:acct-d")]


# ------------------------------------------------------- スナップショットの選び方


def test_month_kind_files_are_ignored(cfg, tmp_path):
    """kind=month（members_2026-07.csv）は時点不明のため差分の起点にしない。"""
    input_dir = tmp_path / "input"
    _snapshot(input_dir, "members_2026-07.csv", ["a@x.jp,Standard,acct-1"])
    _snapshot(input_dir, "members_2026-08.csv", ["a@x.jp,Premium,acct-1"])
    assert load_snapshots(input_dir, cfg) == []
    assert detect_from_input(input_dir, cfg) == seat_changes.SeatChanges([], [])


def test_month_kind_does_not_join_dated_pairs(cfg, tmp_path):
    """kind=month が同居しても、比べるのは日付つきスナップショットだけ。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Standard,acct-1"],
    })
    _snapshot(input_dir, "members_2026-07.csv", ["a@x.jp,Premium,acct-1"])
    assert [snapshot.source for snapshot in load_snapshots(input_dir, cfg)] == [
        "members-snap-2026-07-05.csv",
        "members-snap-2026-07-16.csv",
    ]
    assert _events(input_dir, cfg) == []


def test_no_snapshot_and_single_snapshot_yield_nothing(cfg, tmp_path):
    """時点が0個・1個なら比べる相手が無いので空。"""
    assert detect_from_input(tmp_path / "empty", cfg).events == []
    single = _by_date(tmp_path / "single", {"2026-07-05": ["a@x.jp,Standard,acct-1"]})
    assert len(load_snapshots(single, cfg)) == 1
    assert _events(single, cfg) == []
    assert detect([]).events == []


def test_month_boundary_pair(cfg, tmp_path):
    """隣接ペアは月をまたいでもよい（月内・月またぎを区別しない）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-28": ["a@x.jp,Standard,acct-1"],
        "2026-08-04": ["a@x.jp,Premium,acct-1"],
    })
    events = _events(input_dir, cfg)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "standard_to_premium"
    assert event.changed_after == dt.date(2026, 7, 28)
    assert event.changed_before == dt.date(2026, 8, 4)
    assert event.detected_at == dt.date(2026, 8, 4)
    assert event.previous_source == "members-snap-2026-07-28.csv"
    assert event.current_source == "members-snap-2026-08-04.csv"


def test_sources_are_basenames(cfg, tmp_path):
    """source は basename に限る（絶対パスを event に載せない）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-1"],
    })
    event = _events(input_dir, cfg)[0]
    for source in (event.previous_source, event.current_source):
        assert Path(source).name == source
        assert str(tmp_path) not in source


# ------------------------------------------------------------- 重複と並びの決まり


def _same_day_input(tmp_path: Path) -> Path:
    """同じ日付で往復するシート変更を4本のスナップショットに分けて置く。"""
    input_dir = tmp_path / "input"
    for name, seat in (
        ("members-a-2026-07-05.csv", "Standard"),
        ("members-b-2026-07-05.csv", "Premium"),
        ("members-c-2026-07-05.csv", "Standard"),
        ("members-d-2026-07-05.csv", "Premium"),
    ):
        _snapshot(input_dir, name, [f"a@x.jp,{seat},acct-1"])
    return input_dir


def test_same_day_snapshots_are_adjacent_pairs(cfg, tmp_path):
    """同じ日付のスナップショットもファイル名順の隣接ペアとして比べる。

    区間の両端が同じ日になるため、往復した変更は一意キーが衝突する。重複は1件へ畳み、
    最初に現れたペア（＝古いファイル名の組）の由来を残す。
    """
    changes = detect_from_input(_same_day_input(tmp_path), cfg)
    _assert_unique(changes)
    assert [event.event_type for event in changes.events] == [
        "premium_to_standard",
        "standard_to_premium",
    ]
    for event in changes.events:
        assert event.changed_after == event.changed_before == dt.date(2026, 7, 5)
    upgrade = next(e for e in changes.events if e.event_type == "standard_to_premium")
    assert upgrade.previous_source == "members-a-2026-07-05.csv"
    assert upgrade.current_source == "members-b-2026-07-05.csv"


def test_same_day_order_is_independent_of_input_order(cfg, tmp_path):
    """同じ日付のスナップショットを逆順で渡しても、由来まで含めて同じ結果になる。"""
    snapshots = load_snapshots(_same_day_input(tmp_path), cfg)
    assert detect(reversed(snapshots)) == detect(snapshots)


def test_output_order_is_interval_then_subject(cfg, tmp_path):
    """並びは区間 → subject → シートの順（同じ入力から同じ列になる）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["z@x.jp,Standard,acct-z"],
        "2026-07-16": ["z@x.jp,Premium,acct-z", "a@x.jp,Standard,acct-a",
                       "m@x.jp,Premium,acct-m"],
        "2026-07-28": ["z@x.jp,Premium,acct-z", "a@x.jp,Premium,acct-a",
                       "m@x.jp,Standard,acct-m"],
    })
    events = _events(input_dir, cfg)
    assert [
        (event.changed_after.day, event.subject_id) for event in events
    ] == [
        (5, "account:acct-a"),
        (5, "account:acct-m"),
        (5, "account:acct-z"),
        (16, "account:acct-a"),
        (16, "account:acct-m"),
    ]


def test_same_input_gives_the_same_result(cfg, tmp_path):
    """同じ入力で2回実行しても同じ列。時点の並べ替えでも結果は変わらない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1", "b@x.jp,Premium,acct-2",
                       "u@x.jp,Guest,acct-3"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "c@x.jp,Standard,acct-4",
                       "u@x.jp,Premium,acct-3"],
        "2026-08-04": ["a@x.jp,Standard,acct-1", "c@x.jp,Standard,acct-4"],
    })
    first = detect_from_input(input_dir, cfg)
    assert first == detect_from_input(input_dir, cfg)
    _assert_unique(first)
    assert first.unclassified  # 未分類観測の側も並びを固定する

    snapshots = load_snapshots(input_dir, cfg)
    assert detect(reversed(snapshots)) == first


# ------------------------------------------------------------------- 純関数の契約


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_detect_requires_seat_type():
    """seat_type の無い members を渡したら黙って空にせずエラーにする。"""
    snapshots = [
        MemberSnapshot(
            taken_on=dt.date(2026, 7, day),
            members=_frame([{"email": "a@x.jp"}]),
            source=f"members-{day}.csv",
        )
        for day in (5, 16)
    ]
    with pytest.raises(ValueError, match="seat_type"):
        detect(snapshots)


def test_detect_accepts_frames_without_id_columns():
    """account_uuid・user_id の列が無い members でも email で対応付ける。"""
    snapshots = [
        MemberSnapshot(
            taken_on=dt.date(2026, 7, 5),
            members=_frame([{"email": "a@x.jp", "seat_type": "standard"}]),
            source="members-05.csv",
        ),
        MemberSnapshot(
            taken_on=dt.date(2026, 7, 16),
            members=_frame([{"email": "a@x.jp", "seat_type": "premium"}]),
            source="members-16.csv",
        ),
    ]
    assert [event.event_type for event in detect(snapshots).events] == [
        "standard_to_premium"
    ]


def _event(**overrides) -> SeatChangeEvent:
    fields = {
        "subject_id": "account:acct-1",
        "email": "a@x.jp",
        "from_seat": "standard",
        "to_seat": "premium",
        "changed_after": dt.date(2026, 7, 5),
        "changed_before": dt.date(2026, 7, 16),
        "detected_at": dt.date(2026, 7, 16),
        "previous_source": "members-05.csv",
        "current_source": "members-16.csv",
    }
    return SeatChangeEvent(**{**fields, **overrides})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"from_seat": "unknown"}, "from_seat"),
        ({"to_seat": "unknown"}, "to_seat"),
        ({"to_seat": "standard"}, "シートが変わっていない"),
        ({"changed_before": dt.date(2026, 7, 1)}, "変更区間が逆転"),
    ],
)
def test_event_rejects_values_it_cannot_classify(overrides, message):
    """event の値域を型の側で保証する（unknown・変更なし・区間の逆転を作れない）。"""
    with pytest.raises(ValueError, match=message):
        _event(**overrides)


def test_event_key_ignores_email_and_source():
    """一意キーは subject・シート・区間だけで決まる（表示用の値を含めない）。"""
    assert _event().key == _event(
        email="renamed@x.jp", previous_source="other.csv"
    ).key


def _observation(**overrides) -> UnclassifiedObservation:
    fields = {
        "reason": IDENTITY_CONFLICT,
        "subject_id": None,
        "emails": ("a@x.jp",),
        "changed_after": dt.date(2026, 7, 5),
        "changed_before": dt.date(2026, 7, 16),
        "detected_at": dt.date(2026, 7, 16),
        "previous_source": "members-05.csv",
        "current_source": "members-16.csv",
    }
    return UnclassifiedObservation(**{**fields, **overrides})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"reason": "something_else"}, "reason"),
        ({"changed_before": dt.date(2026, 7, 1)}, "変更区間が逆転"),
    ],
)
def test_observation_rejects_values_outside_the_vocabulary(overrides, message):
    """未分類観測の理由は確定語彙に閉じる（自由文の理由を作らせない）。"""
    with pytest.raises(ValueError, match=message):
        _observation(**overrides)


def test_seat_changes_module_has_no_write_calls():
    """このモジュールは読み込みと導出だけを行う（出力は後続 Step の領分）。"""
    source = Path(seat_changes.__file__).read_text(encoding="utf-8")
    assert "write_text" not in source
    assert "to_csv" not in source
