"""V2 判定の結線: 分析の材料と判定エンジンを突き合わせ、出力用の行を作る。

分析（analyze）が持つ月次の観測・Identity 証拠・追加クレジット上限と、判定エンジン
（decision_v2）が要求する SubjectHistory の間を埋める層。判定の規則そのものは持たず、
規則が要る形へ材料を組み替えて呼ぶだけにする。

ここが担う組み替えは4つある。

- 履歴の切り出し: 対象月から古い方へ遡り、暦で連続する月だけを履歴にする。判定エンジンは
  渡された並びの隣接を「連続」と数えるので、暦の隣接は組み立て側が保証する
- 欠けた月の観測: spend に行の無い月は需要ゼロの観測として入れる（利用ゼロは観測であって
  欠損ではない）。逆に product 名が分からず確定できない Code 需要は None のまま渡し、
  0 や False で埋めない
- 加入の考慮: その subject の加入 event があれば、加入前の月を履歴から除き、加入が
  またがる月を完全月に数えない（在籍が月全体に及んでいない月を完全月として扱わない）
- 帰属: シート変更 event と分類できない観測を、event ごとに決めた手掛かりでユーザへ
  割り当てる。subject_id が判定対象ユーザの誰かを指せばその人だけに帰属させ（stable ID は
  email より信頼できるので、再割当された email の新しい持ち主へ前の持ち主の加入 event を
  付け替えない）、指さないときだけ email で引く（members エクスポートに stable ID が無い
  組織では seat_changes の subject_id が email 由来になり、spend から解いた account_uuid
  由来の subject_id と一致しないため、email しか手が無い）。subject を確定できない未分類
  観測は、関係する email を持つ対象ユーザ全員へ帰属させて保留側へ倒す

Identity は対象月の証拠だけで解く。全期間を一括で解くと、退職者の email が再割当された
場合に時間を隔てた別人どうしが1人へ結合する（seat_changes が隣接ペアで解決するのと同じ
理由）。

純粋関数として保つ。ファイルを読まず、書かず、現在時刻も参照しないため、同じ分析結果と
同じ config からは常に同じ行・同じ並びを返す。
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from . import identity
from .analyze import AnalysisResult, DecisionContext
from .decision_v2 import (
    DecisionV2,
    MonthObservation,
    SubjectHistory,
    decide_downgrade,
    decide_upgrade,
    policy_stability,
)
from .domain import CreditAction, DecisionStatus, ReasonCode, SeatAction
from .identity import IdentityEvidence, ResolvedIdentity
from .seat_changes import SeatChangeEvent, SeatChanges, UnclassifiedObservation

# 判定を振り分ける現シートの値（ingest が正規化した seat_type）
_STANDARD = "standard"
_PREMIUM = "premium"
_UNASSIGNED = "unassigned"
_UNKNOWN = "unknown"

# 加入を表す event 種別（seat_changes.EVENT_TYPES の1つ）
_MEMBER_ADDED = "member_added"

# 判定エンジンが要求する Code 需要・supplementary の列名（product_usage の特徴量）
_CODE_DEMAND = "code_demand_usd"
_SUPPLEMENTARY_HIGH = "supplementary_high"

# Identity を解決できなかったユーザの品質（identity.IdentityQuality の1つ）
_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class EvidenceRow:
    """1ユーザぶんの V2 判定と、その判定に使った材料。

    decision-evidence.csv の1行に対応する。月をまたいだ比較の突き合わせ対象になるため、
    判定の結論（decision）だけでなく、結論を出すのに使った観測と方針値も同じ行に持つ。

    確定できなかった値は None にする（0・False で埋めない）。subject_id は identity が
    subject を確定できたときだけ入り、credit_limit_usd は None が「設定が分からない」、
    0 が「従量課金が無効」、inf が「上限なし」を表す。
    """

    email: str
    subject_id: str | None
    identity_quality: str
    current_seat: str
    month: str
    complete: bool
    complete_months: tuple[str, ...]
    total_demand_usd: float
    code_demand_usd: float | None
    supplementary_high: bool | None
    billed_extra_usd: float
    credit_limit_usd: float | None
    premium_justification_usd: float
    suggested_credit_cap_usd: float | None
    decision: DecisionV2
    policy_stability: int | None


def evaluate(
    result: AnalysisResult, changes: SeatChanges, cfg: Mapping
) -> tuple[EvidenceRow, ...]:
    """分析結果とシート変更から、ユーザごとの V2 判定（email 昇順）を返す。

    対象は result.users のユーザ（members ∪ 対象月の spend。V1 の判定テーブルと同じ
    集合）。現シートで判定を振り分け、Standard は昇格・Premium は降格の規則へ渡す。
    未割当は判定対象外（EXCLUDED）、シート不明は判定しない（NO_DECISION）。

    decision_context を持たない分析結果（既定の analyze）は判定の材料が無いので
    ValueError にする。
    """
    shared = _shared(result, changes, cfg)
    rows = [
        _row(str(email), str(seat), _credit_limit(limit), shared)
        for email, seat, limit in _user_rows(result)
    ]
    return tuple(sorted(rows, key=lambda row: row.email))


def _shared(
    result: AnalysisResult, changes: SeatChanges, cfg: Mapping
) -> _Evaluation:
    """全ユーザに共通の材料を1度だけ組む（ユーザごとの判定はこれを読むだけにする）。"""
    context = result.decision_context
    if context is None:
        raise ValueError(
            "V2 判定の材料がありません（analyze(..., decision_context=True) の結果が必要です）"
        )
    subjects = _resolved_by_email(context.identity_rows)
    user_emails = {
        key
        for key in (_email_key(email) for email in result.users["email"])
        if key is not None
    }
    return _Evaluation(
        context=context,
        monthly={
            month: frame.set_index("email")
            for month, frame in context.aggregates.items()
        },
        history_months=_contiguous_months(context.months, result.month),
        changes=changes,
        cfg=cfg,
        subjects=subjects,
        user_subject_ids=frozenset(
            subject_id
            for key in user_emails
            if (subject := subjects.get(key)) is not None
            and (subject_id := subject.subject_id) is not None
        ),
        justification=float(cfg["decision_v2"]["premium_justification_usd"]),
        suggested_cap=float(cfg["usage_credits"]["grant_suggested_cap_usd"]),
    )


def _user_rows(result: AnalysisResult) -> list[tuple[object, object, object]]:
    """判定対象のユーザ（email・現シート・追加クレジット上限）。

    追加クレジット上限の列は、members-info に値が1つも無い組織では落ちている
    （analyze の後方互換）。その場合は全員「設定が分からない」として扱う。
    """
    users = result.users
    limits = (
        users["credit_limit_usd"]
        if "credit_limit_usd" in users.columns
        else pd.Series(float("nan"), index=users.index)
    )
    return list(zip(users["email"], users["current_seat"], limits, strict=True))


@dataclass(frozen=True)
class _Evaluation:
    """1回の判定で全ユーザに共有する、前処理済みの材料と設定値。

    monthly は月 → email を索引にした集計、history_months は履歴に採る月（ユーザに
    依らず対象月と暦の連続だけで決まる）、subjects は email から引く Identity の
    解決結果、user_subject_ids は判定対象ユーザの subject_id（event の subject が
    対象ユーザの誰かを指しているかの判定に使う）。
    """

    context: DecisionContext
    monthly: dict[str, pd.DataFrame]
    history_months: tuple[str, ...]
    changes: SeatChanges
    cfg: Mapping
    subjects: dict[str, ResolvedIdentity]
    user_subject_ids: frozenset[str]
    justification: float
    suggested_cap: float


def _row(
    email: str,
    current_seat: str,
    credit_limit_usd: float | None,
    shared: _Evaluation,
) -> EvidenceRow:
    """ユーザ1人ぶんの判定と、その材料の行。"""
    subject = shared.subjects.get(_email_key(email))
    subject_id = subject.subject_id if subject is not None else None
    events, unclassified = _attributed(shared, _email_key(email), subject_id)
    history = SubjectHistory(
        email=email,
        current_seat=current_seat,
        credit_limit_usd=credit_limit_usd,
        identity_conflict=subject.conflict if subject is not None else False,
        months=_observations(email, shared, events),
        seat_events=events,
        unclassified=unclassified,
    )
    decision, stability = _decide(history, shared.cfg)
    target = history.target
    return EvidenceRow(
        email=email,
        subject_id=subject_id,
        identity_quality=subject.quality if subject is not None else _UNRESOLVED,
        current_seat=current_seat,
        month=target.month,
        complete=target.complete,
        complete_months=tuple(
            month.month for month in history.months if month.complete
        ),
        total_demand_usd=target.total_demand_usd,
        code_demand_usd=target.code_demand_usd,
        supplementary_high=target.supplementary_high,
        billed_extra_usd=target.billed_usd,
        credit_limit_usd=credit_limit_usd,
        premium_justification_usd=shared.justification,
        suggested_credit_cap_usd=(
            shared.suggested_cap
            if decision.credit_action is CreditAction.ENABLE_WITH_CAP
            else None
        ),
        decision=decision,
        policy_stability=stability,
    )


def _decide(
    history: SubjectHistory, cfg: Mapping
) -> tuple[DecisionV2, int | None]:
    """現シートに応じた判定と方針感度。

    方針感度は経済軸を持つ判定（Standard・Premium）でだけ意味を持つので、それ以外は
    None にする。判定できない現シートの扱いをここで決めるのは、decide_* が Standard・
    Premium 以外を ValueError にしているため（振り分けは呼び出し側の責務）。
    """
    seat = history.current_seat
    if seat == _STANDARD:
        return decide_upgrade(history, cfg), policy_stability(history, cfg)
    if seat == _PREMIUM:
        return decide_downgrade(history, cfg), policy_stability(history, cfg)
    if seat == _UNASSIGNED:
        # 意図的な未割当（別組織でアサイン済み・管理者等）はシート判定の対象外
        return (
            DecisionV2(
                status=DecisionStatus.EXCLUDED,
                seat_action=SeatAction.NONE,
                credit_action=CreditAction.NONE,
                reason_codes=(),
            ),
            None,
        )
    if seat == _UNKNOWN:
        # members に行が無い利用者。現シートが分からないので判定しない（§12.7 の
        # hard blocker）。黙って除外すると、その人が判定から漏れたことが出力に残らない
        return (
            DecisionV2(
                status=DecisionStatus.NO_DECISION,
                seat_action=SeatAction.NONE,
                credit_action=CreditAction.NONE,
                reason_codes=(ReasonCode.CURRENT_SEAT_UNKNOWN,),
            ),
            None,
        )
    raise ValueError(f"判定を振り分けられない現シートです: {seat!r}")


def _observations(
    email: str, shared: _Evaluation, events: Sequence[SeatChangeEvent]
) -> tuple[MonthObservation, ...]:
    """履歴の月ごとの観測（昇順・最後が対象月）。

    spend に行の無い月は需要ゼロの観測にする。加入 event があれば、加入前の月を除き、
    加入がまたがる月を不完全月にする。
    """
    target = shared.history_months[-1]
    joined = _joined_event(events)
    observations = []
    for month in shared.history_months:
        complete = bool(shared.context.complete[month])
        if joined is not None and month != target:
            # 対象月には適用しない（対象月の加入は recent 窓が拾う。complete は
            # spend の完全性だけを表す）
            if _month_end(month) <= joined.changed_after:
                continue  # 加入より前の月＝在籍していない
            if _month_start(month) < joined.changed_before:
                complete = False  # 加入がこの月にまたがりうる
        observations.append(_observation(email, month, complete, shared))
    return tuple(observations)


def _observation(
    email: str, month: str, complete: bool, shared: _Evaluation
) -> MonthObservation:
    """1ヶ月ぶんの観測。spend に行が無ければ利用ゼロとして観測する。"""
    frame = shared.monthly[month]
    if email not in frame.index:
        return MonthObservation(
            month=month,
            complete=complete,
            total_demand_usd=0.0,
            code_demand_usd=0.0,
            billed_usd=0.0,
            supplementary_high=False,
        )
    row = frame.loc[email]
    features = shared.context.product_usage[month].features
    return MonthObservation(
        month=month,
        complete=complete,
        total_demand_usd=float(row["api_cost"]),
        code_demand_usd=_number(_feature(features, email, _CODE_DEMAND)),
        billed_usd=float(row["billed"]),
        supplementary_high=_flag(_feature(features, email, _SUPPLEMENTARY_HIGH)),
    )


def _feature(features: pd.DataFrame, email: str, name: str) -> object:
    """product 特徴量の1セル（そのユーザの行が無ければ欠損）。"""
    if email not in features.index:
        return pd.NA
    return features.loc[email, name]


def _number(value: object) -> float | None:
    """欠損を None のまま保つ float（0 で埋めない）。"""
    return None if pd.isna(value) else float(value)


def _flag(value: object) -> bool | None:
    """欠損を None のまま保つ真偽値（False で埋めない）。"""
    return None if pd.isna(value) else bool(value)


def _credit_limit(value: object) -> float | None:
    """追加クレジット上限 κ。未設定（NaN）は None、無制限（inf）はそのまま。"""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _joined_event(events: Sequence[SeatChangeEvent]) -> SeatChangeEvent | None:
    """その subject の加入 event のうち最新のもの（無ければ None）。

    複数回の加入（離脱と再加入）がある場合は、直近の在籍がいつ始まったかが履歴の
    範囲を決めるので、changed_before が最大のものを採る。
    """
    joined = [event for event in events if event.event_type == _MEMBER_ADDED]
    if not joined:
        return None
    return max(joined, key=lambda event: event.changed_before)


def _attributed(
    shared: _Evaluation, email_key: str | None, subject_id: str | None
) -> tuple[tuple[SeatChangeEvent, ...], tuple[UnclassifiedObservation, ...]]:
    """そのユーザに帰属するシート変更 event と、分類できない観測。

    どちらの手掛かりで引くかは event（未分類観測）ごとに決める。

    1. その subject_id が判定対象ユーザの誰かを指しているなら、その人だけに帰属させる
       （email は見ない）。stable ID は email より信頼できる（設計書 §8 の優先順位）
       ので、email が別の人へ再割当されていても、加入 event を新しい持ち主へ付け替え
       ない。付け替えると、stable ID が指す本人から加入の歯止めが外れ、在籍していな
       かった月の利用ゼロが完全月として数えられてしまう
    2. 指していないときだけ email で引く。members エクスポートに stable ID が無い
       組織では seat_changes の subject_id が email 由来（`email:`）になり、対象月の
       spend から解いた subject_id（`account:`）と一致しないため、email しか手が無い

    subject が確定している event は、その subject の人だけに帰属する（email が一致する
    別の人には渡らない）。subject を確定できない未分類観測（identity conflict）は、
    関係する email を持つ対象ユーザ全員へ帰属する。誰のものか決められない観測なので、
    関係者をまとめて保留側へ倒すほうが安全側。

    絞り込みは渡された並びのままなので、帰属の結果も入力と同じ順序になる。
    """

    def mine(subject: str | None, emails: Sequence[str]) -> bool:
        if subject is not None and subject in shared.user_subject_ids:
            return subject == subject_id
        return email_key is not None and any(
            _email_key(value) == email_key for value in emails
        )

    events = tuple(
        event
        for event in shared.changes.events
        if mine(event.subject_id, (event.email,))
    )
    unclassified = tuple(
        observation
        for observation in shared.changes.unclassified
        if mine(observation.subject_id, observation.emails)
    )
    return events, unclassified


def _resolved_by_email(rows: pd.DataFrame) -> dict[str, ResolvedIdentity]:
    """Identity 証拠行を解決し、email（正規化済み）から引ける表にする。

    解決結果の組は互いに素なので、1つの email が2つの結果に属することはない。
    Identity 値を1つも持たない行の結果（unresolved）は email から引けないため、
    表には入らない（引けないユーザは unresolved として扱う）。
    """
    resolved = identity.resolve_identities(
        IdentityEvidence(
            email=row.email, account_uuid=row.account_uuid, user_id=row.user_id
        )
        for row in rows.itertuples(index=False)
    )
    return {
        email: subject for subject in resolved for email in subject.emails
    }


def _email_key(value: object) -> str | None:
    """照合用の email（identity と同じ正規化）。"""
    return identity.normalize_value(
        value, field_name="IdentityEvidence.email", lowercase=True
    )


def _contiguous_months(months: Sequence[str], target: str) -> tuple[str, ...]:
    """対象月から古い方へ遡って、暦で連続する月だけを返す（昇順）。

    2026-05 と 2026-07 があって 06 が無ければ 05 は履歴に入らない。判定エンジンは
    渡された並びの隣接を「連続」と数えるため、欠けた月をまたいだ隣接を作らない。
    """
    ordered = [month for month in months if month <= target]
    if not ordered or ordered[-1] != target:
        raise ValueError(f"対象月 {target} の観測が履歴にありません: {list(months)}")
    kept = [target]
    expected = _previous_month(target)
    for month in reversed(ordered[:-1]):
        if month != expected:
            break
        kept.append(month)
        expected = _previous_month(month)
    return tuple(reversed(kept))


def _previous_month(month: str) -> str:
    """1つ前の月（YYYY-MM）。"""
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year - 1:04d}-12" if mon == 1 else f"{year:04d}-{mon - 1:02d}"


def _month_start(month: str) -> dt.date:
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, 1)


def _month_end(month: str) -> dt.date:
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])
