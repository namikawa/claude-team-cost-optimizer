"""members スナップショットのペア差分から作るシート変更 event のテスト。

対応付けは email ではなく identity（account_uuid / user_id）に任せる。email が変わっても
同一人物なら変更・加入・離脱として出さない。分類できない遷移（unknown・identity の
conflict・同一時点でのシート食い違い）は event を作らない。

出力は同じ入力から常に同じ列になること（重複なし・決定的な並び）まで固定する。
"""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from seat_analyzer import seat_changes
from seat_analyzer.seat_changes import (
    EVENT_TYPES,
    MemberSnapshot,
    SeatChangeEvent,
    detect_events,
    load_events,
    load_snapshots,
)

# account_uuid 付きの members ヘッダ（stable ID で対応付ける経路を通すため）
HEADER = "Email,Seat Type,Account UUID"


def _snapshot(input_dir: Path, name: str, rows: list[str], header: str = HEADER) -> None:
    """members スナップショット1本を任意のファイル名で置く。"""
    directory = input_dir / "members"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        header + "\n" + "\n".join(rows) + "\n", encoding="utf-8"
    )


def _by_date(
    input_dir: Path, snapshots: dict[str, list[str]], header: str = HEADER
) -> Path:
    """{日付 "YYYY-MM-DD": [行, ...]} を日付命名のスナップショットとして置く。"""
    for date, rows in snapshots.items():
        _snapshot(input_dir, f"members-snap-{date}.csv", rows, header)
    return input_dir


def _summary(events: list[SeatChangeEvent]) -> list[tuple[str, str]]:
    """(email, event_type) の列（顔ぶれと種別の突き合わせ用）。"""
    return [(event.email, event.event_type) for event in events]


def _assert_unique(events: list[SeatChangeEvent]) -> None:
    """一意キーの重複が出力に無いこと。"""
    keys = [event.key for event in events]
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
    events = load_events(input_dir, cfg)
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
    events = load_events(input_dir, cfg)
    assert _summary(events) == [
        ("b@x.jp", "member_removed"),
        ("c@x.jp", "member_added"),
    ]
    assert (events[0].from_seat, events[0].to_seat) == ("premium", "absent")
    assert (events[1].from_seat, events[1].to_seat) == ("absent", "standard")
    assert [event.subject_id for event in events] == ["account:acct-2", "account:acct-3"]


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
    events = load_events(input_dir, cfg)
    assert {event.event_type for event in events} == set(EVENT_TYPES)
    _assert_unique(events)


# --------------------------------------------------------------- 分類できない遷移


@pytest.mark.parametrize(
    ("before", "after"),
    [("Guest", "Premium"), ("Premium", "Guest"), ("", "Premium"), ("Standard", "")],
)
def test_unknown_seat_produces_no_event(cfg, tmp_path, before, after):
    """unknown を含む遷移は event にしない（同じペアの他の subject は出る）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": [f"a@x.jp,{before},acct-1", "b@x.jp,Standard,acct-2"],
        "2026-07-16": [f"a@x.jp,{after},acct-1", "b@x.jp,Premium,acct-2"],
    })
    assert _summary(load_events(input_dir, cfg)) == [("b@x.jp", "standard_to_premium")]


def test_identity_conflict_produces_no_event(cfg, tmp_path):
    """同じ email に別の account_uuid が付いた subject は変更の実体を決められない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-2"],
    })
    assert load_events(input_dir, cfg) == []


def test_conflicting_seats_in_one_snapshot_produce_no_event(cfg, tmp_path):
    """同一時点で同じ subject の行のシートが食い違うなら、現在のシートを決められない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "renamed@x.jp,Standard,acct-1"],
    })
    assert load_events(input_dir, cfg) == []


def test_same_seat_on_multiple_rows_is_still_judged(cfg, tmp_path):
    """同じ subject の行が複数あってもシートが一致していれば判定できる。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "renamed@x.jp,Premium,acct-1"],
    })
    assert _summary(load_events(input_dir, cfg)) == [("a@x.jp", "standard_to_premium")]


# ------------------------------------------------------------------ email の変更


def test_renamed_email_is_not_a_change(cfg, tmp_path):
    """account_uuid が同じなら email の変更はシート変更・加入・離脱にしない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["old@x.jp,Standard,acct-1"],
        "2026-07-16": ["new@x.jp,Standard,acct-1"],
    })
    assert load_events(input_dir, cfg) == []


def test_renamed_email_with_seat_change_is_one_event(cfg, tmp_path):
    """email が変わってシートも変わったら、変更1件として現在側の email で出す。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["old@x.jp,Standard,acct-1"],
        "2026-07-16": ["new@x.jp,Premium,acct-1"],
    })
    events = load_events(input_dir, cfg)
    assert _summary(events) == [("new@x.jp", "standard_to_premium")]
    assert events[0].subject_id == "account:acct-1"


def test_user_id_joins_when_account_uuid_is_missing(cfg, tmp_path):
    """stable ID が user_id だけでも同一人物として対応付ける。"""
    input_dir = _by_date(
        tmp_path / "input",
        {
            "2026-07-05": ["old@x.jp,Standard,user-1"],
            "2026-07-16": ["new@x.jp,Premium,user-1"],
        },
        header="Email,Seat Type,User ID",
    )
    events = load_events(input_dir, cfg)
    assert _summary(events) == [("new@x.jp", "standard_to_premium")]
    assert events[0].subject_id == "user:user-1"


def test_email_only_snapshots_join_by_email(cfg, tmp_path):
    """ID 列そのものが無い members でも email で対応付けて変更を出す。"""
    input_dir = _by_date(
        tmp_path / "input",
        {"2026-07-05": ["a@x.jp,Standard"], "2026-07-16": ["a@x.jp,Premium"]},
        header="Email,Seat Type",
    )
    events = load_events(input_dir, cfg)
    assert _summary(events) == [("a@x.jp", "standard_to_premium")]
    assert events[0].subject_id == "email:a@x.jp"


# ------------------------------------------------------- スナップショットの選び方


def test_month_kind_files_are_ignored(cfg, tmp_path):
    """kind=month（members_2026-07.csv）は時点不明のため差分の起点にしない。"""
    input_dir = tmp_path / "input"
    _snapshot(input_dir, "members_2026-07.csv", ["a@x.jp,Standard,acct-1"])
    _snapshot(input_dir, "members_2026-08.csv", ["a@x.jp,Premium,acct-1"])
    assert load_snapshots(input_dir, cfg) == []
    assert load_events(input_dir, cfg) == []


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
    assert load_events(input_dir, cfg) == []


def test_no_snapshot_and_single_snapshot_yield_nothing(cfg, tmp_path):
    """時点が0個・1個なら比べる相手が無いので空リスト。"""
    empty = tmp_path / "empty"
    assert load_events(empty, cfg) == []
    single = _by_date(tmp_path / "single", {"2026-07-05": ["a@x.jp,Standard,acct-1"]})
    assert len(load_snapshots(single, cfg)) == 1
    assert load_events(single, cfg) == []
    assert detect_events([]) == []


def test_month_boundary_pair(cfg, tmp_path):
    """隣接ペアは月をまたいでもよい（月内・月またぎを区別しない）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-28": ["a@x.jp,Standard,acct-1"],
        "2026-08-04": ["a@x.jp,Premium,acct-1"],
    })
    events = load_events(input_dir, cfg)
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
    event = load_events(input_dir, cfg)[0]
    for source in (event.previous_source, event.current_source):
        assert Path(source).name == source
        assert str(tmp_path) not in source


# ------------------------------------------------------------- 重複と並びの決まり


def test_same_day_snapshots_are_adjacent_pairs(cfg, tmp_path):
    """同じ日付のスナップショットもファイル名順の隣接ペアとして比べる。

    区間の両端が同じ日になるため、往復した変更は一意キーが衝突する。重複は1件へ畳み、
    最初に現れたペア（＝古いファイル名の組）の由来を残す。
    """
    input_dir = tmp_path / "input"
    for name, seat in (
        ("members-a-2026-07-05.csv", "Standard"),
        ("members-b-2026-07-05.csv", "Premium"),
        ("members-c-2026-07-05.csv", "Standard"),
        ("members-d-2026-07-05.csv", "Premium"),
    ):
        _snapshot(input_dir, name, [f"a@x.jp,{seat},acct-1"])

    events = load_events(input_dir, cfg)
    _assert_unique(events)
    assert [event.event_type for event in events] == [
        "premium_to_standard",
        "standard_to_premium",
    ]
    for event in events:
        assert event.changed_after == event.changed_before == dt.date(2026, 7, 5)
    upgrade = next(e for e in events if e.event_type == "standard_to_premium")
    assert upgrade.previous_source == "members-a-2026-07-05.csv"
    assert upgrade.current_source == "members-b-2026-07-05.csv"


def test_output_order_is_interval_then_subject(cfg, tmp_path):
    """並びは区間 → subject → シートの順（同じ入力から同じ列になる）。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["z@x.jp,Standard,acct-z"],
        "2026-07-16": ["z@x.jp,Premium,acct-z", "a@x.jp,Standard,acct-a",
                       "m@x.jp,Premium,acct-m"],
        "2026-07-28": ["z@x.jp,Premium,acct-z", "a@x.jp,Premium,acct-a",
                       "m@x.jp,Standard,acct-m"],
    })
    events = load_events(input_dir, cfg)
    assert [
        (event.changed_after.day, event.subject_id) for event in events
    ] == [
        (5, "account:acct-a"),
        (5, "account:acct-m"),
        (5, "account:acct-z"),
        (16, "account:acct-a"),
        (16, "account:acct-m"),
    ]


def test_same_input_gives_the_same_events(cfg, tmp_path):
    """同じ入力で2回実行しても同じ列。時点の並べ替えでも結果は変わらない。"""
    input_dir = _by_date(tmp_path / "input", {
        "2026-07-05": ["a@x.jp,Standard,acct-1", "b@x.jp,Premium,acct-2"],
        "2026-07-16": ["a@x.jp,Premium,acct-1", "c@x.jp,Standard,acct-3"],
        "2026-08-04": ["a@x.jp,Standard,acct-1", "c@x.jp,Standard,acct-3"],
    })
    first = load_events(input_dir, cfg)
    assert first == load_events(input_dir, cfg)
    _assert_unique(first)

    snapshots = load_snapshots(input_dir, cfg)
    assert detect_events(reversed(snapshots)) == first


# ------------------------------------------------------------------- 純関数の契約


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_detect_events_requires_seat_type():
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
        detect_events(snapshots)


def test_detect_events_accepts_frames_without_id_columns():
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
    assert [event.event_type for event in detect_events(snapshots)] == [
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


def test_seat_changes_module_has_no_write_calls():
    """このモジュールは読み込みと導出だけを行う（出力は後続 Step の領分）。"""
    source = Path(seat_changes.__file__).read_text(encoding="utf-8")
    assert "write_text" not in source
    assert "to_csv" not in source
