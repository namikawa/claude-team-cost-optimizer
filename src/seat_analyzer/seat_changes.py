"""members スナップショットのペア差分から正準のシート変更 event を作る純粋なライブラリ。

シート変更の時刻は観測できない。分かるのは「2つの時点でシートが違った」という事実だけ
なので、event は時点ではなく区間（changed_after..changed_before）で変更を表す。
スナップショットの間隔が広ければ区間も広くなり、後段の判定はその広さをそのまま
「変更時点を絞れていない」という情報として使える。

subject の対応付けは email ではなく identity.resolve_identities に任せる。email が
変わっても account_uuid が同じなら同一人物として扱い、シート変更・追加・削除と誤検出
しない。解決は隣接ペア単位で行う。全スナップショットを一括で解決すると、退職者の email が
別人へ再割当された場合に、時間を隔てた別人どうしが1人へ結合してしまう。

event を作らないのは次の場合。いずれも「変更が無かった」ではなく「変更の実体を分類
できない」ため、無いものとして扱うより黙って出さない方が後段の誤りが少ない:

- from / to のどちらかが unknown（シート種別を判別できない行。読み込み時に警告済み）
- identity が conflict とした subject（同じ組に同種の stable ID が複数ある）
- 同一スナップショット内で同じ subject の行のシートが食い違う

このモジュールは読み込みと event の導出だけを行う。ファイルを書かず、判定もしない。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from . import identity, ingest
from .identity import IdentityEvidence, ResolvedIdentity

# 不在（そのスナップショットに subject の行が無いこと）を表す擬似シート値。ingest の
# シート種別と同じ名前空間に置き、加入・離脱も同じ from / to の組で表せるようにする
ABSENT = "absent"

_STANDARD = "standard"
_PREMIUM = "premium"
_UNASSIGNED = "unassigned"

# from / to に置ける値。unknown は event を作らないため含めない（この一覧に無い値が来たら
# event にしない、という1つの規則で「分類できない」全体を扱う）
SEAT_VALUES = (_STANDARD, _PREMIUM, _UNASSIGNED, ABSENT)

# event の種別。後段の判定・出力が突き合わせる語彙で、これ以外の値は返らない
EVENT_TYPES = (
    "standard_to_premium",
    "premium_to_standard",
    "assigned_to_unassigned",
    "unassigned_to_assigned",
    "member_added",
    "member_removed",
)

# Identity 値の種別名。identity 側が組を作るときに使う節点の種別と同じにする
_NODE_KINDS = ("email", "account", "user")

# 一意キー（subject・from・to・区間）。出力順はこの要素の並べ替えなので全順序になる
_Key = tuple[str, str, str, dt.date, dt.date]
_SortKey = tuple[dt.date, dt.date, str, str, str]


@dataclass(frozen=True)
class MemberSnapshot:
    """ある時点の members 一覧（差分の1時点）。

    taken_on はファイル名から読み取った時点、members は ingest.load_members_file と
    同じ形（email・seat_type が必須。account_uuid・user_id は任意）、source は由来
    ファイルの basename。絶対パスは持たせない（event に載る文字列を実行環境に依存させ
    ないため）。
    """

    taken_on: dt.date
    members: pd.DataFrame
    source: str


@dataclass(frozen=True)
class SeatChangeEvent:
    """2時点の差から確定した1件のシート変更。

    変更は changed_after より後・changed_before 以前に起きた。detected_at は変更を
    観測したスナップショットの時点（＝ changed_before）で、後から古いスナップショットを
    足した場合に区間が狭まっても「いつ気付いたか」は変わらないため別に持つ。

    from_seat / to_seat は SEAT_VALUES のいずれかで、同じ値の組は作れない（変更が無い
    ことを表す event を持たない）。unknown を含む組も作れない。
    """

    subject_id: str
    email: str
    from_seat: str
    to_seat: str
    changed_after: dt.date
    changed_before: dt.date
    detected_at: dt.date
    previous_source: str
    current_source: str

    def __post_init__(self) -> None:
        for name, value in (("from_seat", self.from_seat), ("to_seat", self.to_seat)):
            if value not in SEAT_VALUES:
                raise ValueError(
                    f"{name} には {'/'.join(SEAT_VALUES)} のいずれかが必要です: {value!r}"
                )
        if self.from_seat == self.to_seat:
            raise ValueError(
                f"シートが変わっていない組は event にできません: {self.from_seat!r}"
            )
        if self.changed_before < self.changed_after:
            raise ValueError(
                f"変更区間が逆転しています: {self.changed_after}..{self.changed_before}"
            )

    @property
    def event_type(self) -> str:
        """(from_seat, to_seat) から導く種別（EVENT_TYPES のいずれか）。

        assigned は standard・premium の両方を指す。加入・離脱の判定を先に置くのは、
        不在との組を「未割当への変更」と読み違えないため。
        """
        if self.from_seat == ABSENT:
            return "member_added"
        if self.to_seat == ABSENT:
            return "member_removed"
        if self.to_seat == _UNASSIGNED:
            return "assigned_to_unassigned"
        if self.from_seat == _UNASSIGNED:
            return "unassigned_to_assigned"
        if self.from_seat == _STANDARD:
            return "standard_to_premium"
        return "premium_to_standard"

    @property
    def key(self) -> _Key:
        """重複判定の一意キー（subject・from・to・区間）。

        同じ変更を別のスナップショットの組から二重に拾わないための鍵で、email や
        由来ファイルは含めない（表示のための値であり、変更そのものの同一性ではない）。
        """
        return (
            self.subject_id,
            self.from_seat,
            self.to_seat,
            self.changed_after,
            self.changed_before,
        )


class _Row(NamedTuple):
    """1行分のシート値と Identity 証拠。"""

    seat: str
    evidence: IdentityEvidence


class _Node(NamedTuple):
    """Identity 値1つ（種別と正規化後の値）。行を subject へ引き当てる鍵。"""

    kind: str
    value: str


def detect_events(snapshots: Iterable[MemberSnapshot]) -> list[SeatChangeEvent]:
    """時点の列から、隣接ペアごとのシート変更 event を返す。

    時点の昇順に並べ、隣り合う2つずつを比べる（月内・月またぎを区別しない。区間の
    広さは間隔がそのまま表す）。同じ時点のスナップショットが複数ある場合も渡された順で
    隣接ペアとして扱い、区間の両端が同じ日の event になる。

    同じ subject の同じ変更が複数のペアから出た場合は key で1件に畳み、最初に現れた
    ものを残す。戻り値は区間 → subject → シートの順で並べ、同じ入力からは常に同じ
    列を返す（key が一意なので並びも一意に決まる）。

    時点が0個または1個なら空リスト。
    """
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.taken_on)
    events: dict[_Key, SeatChangeEvent] = {}
    for previous, current in zip(ordered, ordered[1:], strict=False):
        for event in _pair_events(previous, current):
            events.setdefault(event.key, event)
    return sorted(events.values(), key=_sort_key)


def load_snapshots(input_dir: Path, cfg: dict) -> list[MemberSnapshot]:
    """組織の入力ディレクトリから members の単日スナップショットを全月ぶん読む。

    対象月の1本を選ぶ load_members とは別で、時点の列そのものを返す（差分は列を必要と
    するため）。時点が特定できないファイル（kind=month の members_2026-07.csv 等）は
    ingest.member_files が除く。
    """
    return [
        MemberSnapshot(
            taken_on=period.start,
            members=ingest.load_members_file(path, cfg),
            source=path.name,
        )
        for period, path in ingest.member_files(Path(input_dir))
    ]


def load_events(input_dir: Path, cfg: dict) -> list[SeatChangeEvent]:
    """組織の入力ディレクトリからシート変更 event を返す（読み込み + detect_events）。"""
    return detect_events(load_snapshots(input_dir, cfg))


def _sort_key(event: SeatChangeEvent) -> _SortKey:
    """出力順（区間 → subject → シート）。key と同じ要素なので全順序になる。"""
    return (
        event.changed_after,
        event.changed_before,
        event.subject_id,
        event.from_seat,
        event.to_seat,
    )


def _pair_events(
    previous: MemberSnapshot, current: MemberSnapshot
) -> list[SeatChangeEvent]:
    """隣接する2時点の差分。両側の全行を一括で解決してから subject 単位で比べる。"""
    previous_rows = _rows(previous.members)
    current_rows = _rows(current.members)
    resolved = identity.resolve_identities(
        row.evidence for row in (*previous_rows, *current_rows)
    )
    owner = _owner_of_nodes(resolved)
    before_rows = _group(previous_rows, owner)
    after_rows = _group(current_rows, owner)

    events: list[SeatChangeEvent] = []
    for index in sorted(set(before_rows) | set(after_rows)):
        subject = resolved[index]
        if subject.subject_id is None or subject.conflict:
            continue
        before = _seat_of(before_rows.get(index, ()))
        after = _seat_of(after_rows.get(index, ()))
        if before is None or after is None or before == after:
            continue
        if before not in SEAT_VALUES or after not in SEAT_VALUES:
            continue
        events.append(
            SeatChangeEvent(
                subject_id=subject.subject_id,
                email=_representative_email(
                    after_rows.get(index, ()), before_rows.get(index, ())
                ),
                from_seat=before,
                to_seat=after,
                changed_after=previous.taken_on,
                changed_before=current.taken_on,
                detected_at=current.taken_on,
                previous_source=previous.source,
                current_source=current.source,
            )
        )
    return events


def _rows(members: pd.DataFrame) -> list[_Row]:
    """members の各行を、シート値と Identity 証拠の組にする。

    seat_type は ingest が正規化した値（standard / premium / unassigned / unknown）を
    前提とし、ここでは解釈しない。account_uuid・user_id は列そのものが無くてもよい
    （その行に stable ID が無いのと同じ扱いになる）。
    """
    for column in ("email", "seat_type"):
        if column not in members.columns:
            raise ValueError(f"シート変更 event の検出には members の {column} 列が必要です")

    def _column(name: str) -> list:
        if name in members.columns:
            return list(members[name])
        return [None] * len(members)

    return [
        _Row(
            seat=seat if isinstance(seat, str) else str(seat),
            evidence=IdentityEvidence(
                email=email, account_uuid=account_uuid, user_id=user_id
            ),
        )
        for email, seat, account_uuid, user_id in zip(
            list(members["email"]),
            list(members["seat_type"]),
            _column("account_uuid"),
            _column("user_id"),
            strict=True,
        )
    ]


def _owner_of_nodes(
    resolved: Sequence[ResolvedIdentity],
) -> dict[_Node, int]:
    """Identity 値 → その値を含む解決結果の位置。行を subject へ引き当てる逆引き表。

    解決結果の組は互いに素なので、1つの値が2つの組に属することはない。
    """
    owner: dict[_Node, int] = {}
    for index, subject in enumerate(resolved):
        values = (subject.emails, subject.account_uuids, subject.user_ids)
        for kind, kind_values in zip(_NODE_KINDS, values, strict=True):
            for value in kind_values:
                owner[_Node(kind, value)] = index
    return owner


def _group(rows: Sequence[_Row], owner: Mapping[_Node, int]) -> dict[int, list[_Row]]:
    """行を subject（解決結果の位置）ごとに束ねる。

    Identity 値が1つも無い行は他の行との同一性を判断できないため、どの subject にも
    属さない（identity 側も行ごとの unresolved を返す）。
    """
    grouped: dict[int, list[_Row]] = {}
    for row in rows:
        index = _owner_index(row, owner)
        if index is None:
            continue
        grouped.setdefault(index, []).append(row)
    return grouped


def _owner_index(row: _Row, owner: Mapping[_Node, int]) -> int | None:
    """行が属する subject の位置。1行の Identity 値は同じ組に入るため最初の一致で決まる。"""
    for node in _nodes(row.evidence):
        index = owner.get(node)
        if index is not None:
            return index
    return None


def _nodes(evidence: IdentityEvidence) -> tuple[_Node, ...]:
    """1行が持つ Identity 値（identity 側と同じ種別名・同じ正規化）。"""
    values = (
        _text(evidence.email, lowercase=True),
        _text(evidence.account_uuid),
        _text(evidence.user_id),
    )
    return tuple(
        _Node(kind, value)
        for kind, value in zip(_NODE_KINDS, values, strict=True)
        if value is not None
    )


def _text(value: object, *, lowercase: bool = False) -> str | None:
    """Identity 値を照合用の文字列へ揃える（前後空白を除去。email だけ小文字化）。

    identity 側の正規化と同じ規則にする（違うと逆引き表を引けない）。非スカラーは
    resolve_identities が先に拒否するため、ここへは来ない。
    """
    if value is None or not pd.api.types.is_scalar(value) or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower() if lowercase else text


def _seat_of(rows: Sequence[_Row]) -> str | None:
    """その時点の subject のシート。行が無ければ不在、食い違うなら None（分類不能）。

    同じ subject の行が複数あるのは、email 違いの行が stable ID で1人へまとまる場合。
    その行のシートが食い違うなら、どちらが現在のシートかを決められない。
    """
    if not rows:
        return ABSENT
    seats = {row.seat for row in rows}
    if len(seats) > 1:
        return None
    return next(iter(seats))


def _representative_email(
    current_rows: Sequence[_Row], previous_rows: Sequence[_Row]
) -> str:
    """event に載せる email。現在側の行があればそちらを優先し、辞書順の先頭を採る。

    email は人が読むための代表値で、同一性の根拠ではない（それは subject_id が持つ）。
    改名を伴う変更では改名後の姿を出したいので、両側に行があれば現在側を採る。
    """
    for rows in (current_rows, previous_rows):
        emails = sorted(
            value
            for value in (_text(row.evidence.email, lowercase=True) for row in rows)
            if value is not None
        )
        if emails:
            return emails[0]
    return ""
