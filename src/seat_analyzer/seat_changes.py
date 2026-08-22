"""members スナップショットのペア差分から正準のシート変更 event を作る純粋なライブラリ。

シート変更の時刻は観測できない。分かるのは「2つの時点でシートが違った」という事実だけ
なので、event は時点ではなく区間（changed_after..changed_before）で変更を表す。
スナップショットの間隔が広ければ区間も広くなり、後段の判定はその広さをそのまま
「変更時点を絞れていない」という情報として使える。

subject の対応付けは email ではなく identity.resolve_identities に任せる。email が
変わっても account_uuid が同じなら同一人物として扱い、シート変更・追加・削除と誤検出
しない。解決は隣接ペア単位で行う。全スナップショットを一括で解決すると、退職者の email が
別人へ再割当された場合に、時間を隔てた別人どうしが1人へ結合してしまう。

読み取りは行を畳まない（ingest.load_member_rows）。同一 email の行が複数あること・
email を持たない行があることは、それ自体が「その時点の状態を確定できない」という
情報なので、Identity 解決へ渡す前に落とさない。

分類できない観測は event にせず、別のチャネル（UnclassifiedObservation）へ残す。
event が無いことを「変更が無かった」と読み替えられると後段が誤るため、次のものは
理由つきの観測として返す:

- unknown_seat: シート種別を判別できない値が区間の端にある
- identity_conflict: identity が subject を確定できない（同種の stable ID が複数）
- inconsistent_seat: 同一時点で同じ subject の行のシートが食い違う
- unconfirmed_absence: Identity 値を持たない行があり、不在（加入・離脱）を確定できない

データ行が無いスナップショット（失敗したエクスポート等）は観測として扱わず、ペアの
対象にしない。除いた上で残りの隣接ペアを組むので、区間が広がるだけで変更は拾える。

このモジュールは読み込みと導出だけを行う。ファイルを書かず、判定もしない。
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

# 分類できなかった理由。機械可読の確定語彙で、message のような自由文は持たない
UNKNOWN_SEAT = "unknown_seat"
IDENTITY_CONFLICT = "identity_conflict"
INCONSISTENT_SEAT = "inconsistent_seat"
UNCONFIRMED_ABSENCE = "unconfirmed_absence"

UNCLASSIFIED_REASONS = (
    UNKNOWN_SEAT,
    IDENTITY_CONFLICT,
    INCONSISTENT_SEAT,
    UNCONFIRMED_ABSENCE,
)

# Identity 値の種別名。identity 側が組を作るときに使う節点の種別と同じにする
_NODE_KINDS = ("email", "account", "user")

# 一意キー（subject・from・to・区間）。出力順はこの要素の並べ替えなので全順序になる
_Key = tuple[str, str, str, dt.date, dt.date]
_SortKey = tuple[dt.date, dt.date, str, str, str]

# 未分類観測の一意キー。conflict では subject_id が無いため、その組が持つ Identity 値
# （email・account_uuid・user_id のすべて）を鍵に含めて組どうしを取り違えないようにする。
# 末尾はペアそのものについての観測（Identity 値を1つも持たない）だけが使う由来の組
_Identifiers = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
_ObservationKey = tuple[dt.date, dt.date, str, _Identifiers, str, tuple[str, str]]

# 誰のものとも決められない観測に使う、Identity 値を1つも持たない解決結果
_NO_SUBJECT = ResolvedIdentity(
    subject_id=None,
    quality="unresolved",
    conflict=False,
    emails=(),
    account_uuids=(),
    user_ids=(),
)


@dataclass(frozen=True)
class MemberSnapshot:
    """ある時点の members 一覧（差分の1時点）。

    taken_on はファイル名から読み取った時点、members は ingest.load_member_rows と
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
        _check_interval(self.changed_after, self.changed_before)

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


@dataclass(frozen=True)
class UnclassifiedObservation:
    """変更の実体を分類できなかった観測1件（event にできなかったこと自体の記録）。

    区間と由来は event と同じ形にする。後段が event と同じ「recent 窓との重なり」で
    扱えるようにするため。

    subject_id は identity が subject を確定できた場合だけ入る（conflict では None）。
    emails・account_uuids・user_ids は突き合わせた両時点でその組に現れた Identity 値
    （辞書順）で、人が該当行を探すための値であり、subject_id が無いときに組どうしを
    区別する唯一の手掛かりでもある。どの値も持たない観測は、その区間に誰のものとも
    決められない行があったことを表す。
    """

    reason: str
    subject_id: str | None
    emails: tuple[str, ...]
    account_uuids: tuple[str, ...]
    user_ids: tuple[str, ...]
    changed_after: dt.date
    changed_before: dt.date
    detected_at: dt.date
    previous_source: str
    current_source: str

    def __post_init__(self) -> None:
        if self.reason not in UNCLASSIFIED_REASONS:
            raise ValueError(
                f"reason には {'/'.join(UNCLASSIFIED_REASONS)} のいずれかが必要です: "
                f"{self.reason!r}"
            )
        _check_interval(self.changed_after, self.changed_before)

    @property
    def key(self) -> _ObservationKey:
        """一意キー。並び順にもこれを使う（区間 → subject → Identity 値 → 理由 → 由来）。

        subject_id を確定できない組が同じ区間に複数あっても、持っている Identity 値が
        違えば別の観測として残る。ペアそのものについての観測（subject も Identity 値も
        無い）は「どの2ファイルを比べたか」が同一性なので由来を鍵に含め、同日の別ペアが
        1件に畳まれないようにする。subject に紐づく観測は由来を含めず、同日のチェーン
        から出る同じ観測を1件にまとめる。
        """
        identifiers = (self.emails, self.account_uuids, self.user_ids)
        pair_scoped = self.subject_id is None and not any(identifiers)
        return (
            self.changed_after,
            self.changed_before,
            self.subject_id or "",
            identifiers,
            self.reason,
            (self.previous_source, self.current_source) if pair_scoped else ("", ""),
        )


@dataclass(frozen=True)
class SeatChanges:
    """スナップショットの列から読み取れたシート変更と、分類できなかった観測。

    2つを1つの戻り値にまとめるのは、同じ走査から出る表裏で、片方だけを見ると
    「event が無い」を「変更が無かった」と読み替えてしまうため。
    """

    events: list[SeatChangeEvent]
    unclassified: list[UnclassifiedObservation]


class _Row(NamedTuple):
    """1行分のシート値と Identity 証拠。"""

    seat: str
    evidence: IdentityEvidence


class _Node(NamedTuple):
    """Identity 値1つ（種別と正規化後の値）。行を subject へ引き当てる鍵。"""

    kind: str
    value: str


class _Side(NamedTuple):
    """1時点の行を subject 単位に束ねた結果。

    has_unidentifiable は Identity 値を1つも持たない行があったかどうか。その行が誰の
    ものか決められないため、この時点を根拠にした「不在」は確定できない。
    """

    by_subject: dict[int, list[_Row]]
    has_unidentifiable: bool


def detect(snapshots: Iterable[MemberSnapshot]) -> SeatChanges:
    """時点の列から、隣接ペアごとのシート変更と未分類観測を返す。

    時点（同時点なら source 名）の昇順に並べ、隣り合う2つずつを比べる。月内・月またぎは
    区別しない（区間の広さは間隔がそのまま表す）。並べ替えを内側で行うため、結果は渡した
    順序に依らない。

    データ行が無いスナップショットは観測として扱わず、除いた上で隣接ペアを組む
    （失敗したエクスポートを「全員の離脱」と読まないため。既存の doctor は同じ入力を
    error として報告する）。

    同じ subject の同じ変更が複数のペアから出た場合は key で1件に畳み、最初に現れた
    ものを残す。event・未分類観測はどちらも区間 → subject の順に並べ、同じ入力からは
    常に同じ列を返す（key が一意なので並びも一意に決まる）。

    時点が0個または1個なら、どちらの列も空。
    """
    ordered = [
        snapshot
        for snapshot in sorted(snapshots, key=lambda s: (s.taken_on, s.source))
        if not snapshot.members.empty
    ]
    events: dict[_Key, SeatChangeEvent] = {}
    observations: dict[_ObservationKey, UnclassifiedObservation] = {}
    for previous, current in zip(ordered, ordered[1:], strict=False):
        pair = _pair_changes(previous, current)
        for event in pair.events:
            events.setdefault(event.key, event)
        for observation in pair.unclassified:
            observations.setdefault(observation.key, observation)
    return SeatChanges(
        events=sorted(events.values(), key=_sort_key),
        unclassified=sorted(observations.values(), key=lambda o: o.key),
    )


def load_snapshots(input_dir: Path, cfg: dict) -> list[MemberSnapshot]:
    """組織の入力ディレクトリから members の単日スナップショットを全月ぶん読む。

    対象月の1本を選ぶ load_members とは別で、時点の列そのものを返す（差分は列を必要と
    するため）。時点が特定できないファイル（kind=month の members_2026-07.csv 等）は
    ingest.member_files が除く。データ行の有無は見ない（観測として扱うかを決めるのは
    detect の側）。
    """
    return [
        MemberSnapshot(
            taken_on=period.start,
            members=ingest.load_member_rows(path, cfg),
            source=path.name,
        )
        for period, path in ingest.member_files(Path(input_dir))
    ]


def detect_from_input(input_dir: Path, cfg: dict) -> SeatChanges:
    """組織の入力ディレクトリからシート変更を検出する（読み込み + detect）。"""
    return detect(load_snapshots(input_dir, cfg))


def _check_interval(changed_after: dt.date, changed_before: dt.date) -> None:
    """区間の向きを検査する（前の時点が後の時点より新しい組を作らせない）。"""
    if changed_before < changed_after:
        raise ValueError(f"変更区間が逆転しています: {changed_after}..{changed_before}")


def _sort_key(event: SeatChangeEvent) -> _SortKey:
    """出力順（区間 → subject → シート）。key と同じ要素なので全順序になる。"""
    return (
        event.changed_after,
        event.changed_before,
        event.subject_id,
        event.from_seat,
        event.to_seat,
    )


def _pair_changes(previous: MemberSnapshot, current: MemberSnapshot) -> SeatChanges:
    """隣接する2時点の差分。両側の全行を一括で解決してから subject 単位で比べる。

    どちらの側にも識別できる行が1つも無いペアは、subject が立たないので per-subject の
    観測を作れない。その区間の状態を何も言えないことが消えないよう、ペア単位の観測を
    1件だけ残す（識別できる subject が1人でもいれば、そちらの観測で足りるので作らない）。
    """
    previous_rows = _rows(previous.members)
    current_rows = _rows(current.members)
    resolved = identity.resolve_identities(
        row.evidence for row in (*previous_rows, *current_rows)
    )
    owner = _owner_of_nodes(resolved)
    before_side = _side(previous_rows, owner)
    after_side = _side(current_rows, owner)

    events: list[SeatChangeEvent] = []
    unclassified: list[UnclassifiedObservation] = []
    subjects = set(before_side.by_subject) | set(after_side.by_subject)
    for index in sorted(subjects):
        subject = resolved[index]
        before = _seat_of(before_side.by_subject.get(index, ()))
        after = _seat_of(after_side.by_subject.get(index, ()))
        reason = _unclassified_reason(subject, before, after, before_side, after_side)
        if reason is not None:
            unclassified.append(_observation(reason, subject, previous, current))
            continue
        if before == after:
            continue
        events.append(
            SeatChangeEvent(
                subject_id=subject.subject_id,
                email=_representative_email(
                    after_side.by_subject.get(index, ()),
                    before_side.by_subject.get(index, ()),
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
    nothing_identifiable = (
        before_side.has_unidentifiable or after_side.has_unidentifiable
    )
    if not subjects and nothing_identifiable:
        unclassified.append(
            _observation(UNCONFIRMED_ABSENCE, _NO_SUBJECT, previous, current)
        )
    return SeatChanges(events=events, unclassified=unclassified)


def _observation(
    reason: str,
    subject: ResolvedIdentity,
    previous: MemberSnapshot,
    current: MemberSnapshot,
) -> UnclassifiedObservation:
    """未分類観測1件。区間と由来は event と同じ規則で埋める。"""
    return UnclassifiedObservation(
        reason=reason,
        subject_id=subject.subject_id,
        emails=subject.emails,
        account_uuids=subject.account_uuids,
        user_ids=subject.user_ids,
        changed_after=previous.taken_on,
        changed_before=current.taken_on,
        detected_at=current.taken_on,
        previous_source=previous.source,
        current_source=current.source,
    )


def _unclassified_reason(
    subject: ResolvedIdentity,
    before: str | None,
    after: str | None,
    before_side: _Side,
    after_side: _Side,
) -> str | None:
    """その subject を event にできない理由（できるなら None）。

    理由は1つに絞り、根本的なものを先に見る。identity が subject を確定できないなら
    シートの比較そのものが成り立たず、時点内で食い違っていればどちらが現在のシートかを
    決められない。

    両端の値が同じときに理由を残さないのは、identity が subject を確定できた場合に
    限る。その場合は unknown どうしであっても「その subject のシートはその区間で変わって
    いない」ことは分かる（値の意味が分からないだけで、変更の見落としにはならない）。
    conflict はシートが同じに見えても、同一 email の再割当による別人への入れ替わりと
    区別できないため、シート値によらず残す。

    不在は「行が無い」ことでしか判断できないため、その時点に誰のものとも決められない行が
    あれば確定できない。抑えるのは不在を根拠とする側だけで、両側に行がある subject の
    シート変更は影響を受けない。
    """
    # subject_id が無いのは conflict のときだけ（Identity 値を持たない行は subject に
    # 属さないため、ここへ来る組は必ず1つ以上の Identity 値を持つ）
    if subject.subject_id is None or subject.conflict:
        return IDENTITY_CONFLICT
    if before is None or after is None:
        return INCONSISTENT_SEAT
    if before == after:
        return None
    if before not in SEAT_VALUES or after not in SEAT_VALUES:
        return UNKNOWN_SEAT
    if before == ABSENT and before_side.has_unidentifiable:
        return UNCONFIRMED_ABSENCE
    if after == ABSENT and after_side.has_unidentifiable:
        return UNCONFIRMED_ABSENCE
    return None


def _rows(members: pd.DataFrame) -> list[_Row]:
    """members の各行を、シート値と Identity 証拠の組にする。

    seat_type は ingest が正規化した値（standard / premium / unassigned / unknown）を
    前提とし、ここでは解釈しない。account_uuid・user_id は列そのものが無くてもよい
    （その行に stable ID が無いのと同じ扱いになる）。行は畳まない。
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


def _owner_of_nodes(resolved: Sequence[ResolvedIdentity]) -> dict[_Node, int]:
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


def _side(rows: Sequence[_Row], owner: Mapping[_Node, int]) -> _Side:
    """行を subject（解決結果の位置）ごとに束ね、不可識別行の有無を併せて返す。

    Identity 値が1つも無い行は他の行との同一性を判断できないため、どの subject にも
    属さない（identity 側も行ごとの unresolved を返す）。落としたことを忘れないよう、
    そういう行があったかどうかを一緒に持つ。
    """
    by_subject: dict[int, list[_Row]] = {}
    has_unidentifiable = False
    for row in rows:
        index = _owner_index(row, owner)
        if index is None:
            has_unidentifiable = True
            continue
        by_subject.setdefault(index, []).append(row)
    return _Side(by_subject=by_subject, has_unidentifiable=has_unidentifiable)


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

    同じ subject の行が複数あるのは、同一 email の行が重複している場合と、email 違いの
    行が stable ID で1人へまとまる場合。その行のシートが食い違うなら、どちらが現在の
    シートかを決められない。
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
