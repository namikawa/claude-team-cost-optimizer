"""V2判定の昇格ルール（Standard→Premium）を決める純粋なモジュール。

昇格の判断は2つの軸でできている:

- 経済軸: Premium へ変えたら金額として見合うか。シートの込み枠は product をまたいで
  共通なので、全 product 合算の需要と実課金で評価する
- 分類軸: その需要の中身が Code か。経済軸を満たした候補を「自動で昇格を推奨する」ものと
  「アサインそのものを人が見直す（REVIEW_ASSIGNMENT）」ものへ振り分けるゲートにする

設定は読み込まない。検証済みの config と、呼び出し側が組み立てた観測（SubjectHistory）
だけを受けて結論を返す（product_usage が policy を引数で受けるのと同じ流儀）。ファイルを
読まず、書かず、現在時刻も参照しないため、同じ入力からは常に同じ結論と同じ理由の並びを
返す。

扱うのは Standard の昇格だけで、降格・追加クレジットの提案は持たない（credit_action は
常に CreditAction.NONE）。current seat が Standard 以外のときどう扱うか（unknown を
判定しない等）は呼び出し側の責務なので、ここでは ValueError にする。分析パイプライン・
出力へは未結線。

StrEnum は文字列として等値になり、語彙をまたいだ == が成立する（設計書 §12.1）。型では
混同を防げないので、境界の値オブジェクト DecisionV2 が受け取る語彙を isinstance で
検証する（QualityIssue と同じ流儀）。
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from .domain import CreditAction, DecisionStatus, ReasonCode, SeatAction
from .seat_changes import SeatChangeEvent, UnclassifiedObservation

# 判定の対象になる現シート。これ以外は呼び出し側の責務（この関数には持ち込まない）
_STANDARD = "standard"

# 加入を表す event 種別（seat_changes.EVENT_TYPES の1つ）。加入直後は「直近のシート変更」
# とは別の理由コードで表す
_MEMBER_ADDED = "member_added"

# allowance のシナリオ。V1（analyze.SCENARIOS）と同じ顔ぶれ・同じ意味
_SCENARIOS = ("low", "mid", "high")

# 純モデル判定で候補とみなすのに要るシナリオ数（過半数）
_MIN_AGREEING_SCENARIOS = 2

# SUSTAINED_OVERAGE を付けるのに要る、直近から連続する完全月の数
_SUSTAINED_MONTHS = 2

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 理由コードの並び（主理由 → 補助 → 情報）。decision-evidence.csv へそのまま書かれ、
# 月をまたいだ比較の突き合わせ対象になるため、同じ入力からは常に同じ並びで返す
_REASON_ORDER = (
    # 主理由: 結論がその値になった直接の理由
    ReasonCode.IDENTITY_CONFLICT,
    ReasonCode.PARTIAL_MONTH,
    ReasonCode.INSUFFICIENT_HISTORY,
    ReasonCode.RECENT_MEMBER,
    ReasonCode.RECENT_SEAT_CHANGE,
    ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND,
    ReasonCode.REVIEW_NON_CODE_USAGE,
    # 補助: 主理由を補強する観測
    ReasonCode.CREDIT_LIMIT_REACHED,
    ReasonCode.SUSTAINED_OVERAGE,
    # 情報: 結論は変えないが、人が判断するときに要る文脈
    ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
    ReasonCode.HIGH_SUPPLEMENTARY_USAGE,
    ReasonCode.DATA_CONFIDENCE_LOW,
)

# 並べ替えの順位表。引くのは ReasonCode だけ（同名の IssueCode は StrEnum の等値衝突で
# 同じキーに当たってしまうため、この表を引く値は必ず ReasonCode に限る）
_REASON_RANK = {code: index for index, code in enumerate(_REASON_ORDER)}


def _amount(
    value: object,
    name: str,
    *,
    allow_none: bool = False,
    allow_infinite: bool = False,
    non_negative: bool = False,
) -> float | None:
    """金額を検証し、float へ写して返す。

    NaN・Infinity は比較が常に偽になり判定を黙って変えるため、原則として拒否する
    （config の金額検査と同じ理由）。上限が無い追加クレジットだけは Infinity を
    「無制限」の表現として認める。真偽値は int のサブクラスなので数値の前に除く。

    需要・実課金は返金で負になりうるので符号は問わない。non_negative は、負の値に
    対応する状態が定義そのものに無い項目（追加クレジット上限）だけに使う。
    """
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} には数値が必要です: None")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} には数値が必要です: {type(value).__name__}")
    result = float(value)
    if math.isnan(result) or (not allow_infinite and math.isinf(result)):
        raise ValueError(f"{name} に有限でない数値は指定できません: {value!r}")
    if non_negative and result < 0.0:
        raise ValueError(f"{name} に負の値は指定できません: {value!r}")
    return result


def _flag(value: object, name: str, *, allow_none: bool = False) -> bool | None:
    """真偽値を検証する。allow_none のとき None は「分からない」を表す。"""
    if value is None and allow_none:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{name} には真偽値が必要です: {type(value).__name__}")
    return value


@dataclass(frozen=True)
class MonthObservation:
    """1ユーザ・1ヶ月ぶんの観測。

    complete は全月ぶんのデータかどうか（部分月なら False）。total_demand_usd は全
    product の API 換算需要、billed_usd は実課金。

    code_demand_usd と supplementary_high の None は「証明できない」を表す。0・False で
    埋めない（product 名が分からない行があると Code 需要は確定しない。product_usage が
    欠損で返すのと同じ扱い）。
    """

    month: str
    complete: bool
    total_demand_usd: float
    code_demand_usd: float | None
    billed_usd: float
    supplementary_high: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.month, str) or not _MONTH_RE.match(self.month):
            raise ValueError(f"month には YYYY-MM 形式が必要です: {self.month!r}")
        object.__setattr__(self, "complete", _flag(self.complete, "complete"))
        for name in ("total_demand_usd", "billed_usd"):
            object.__setattr__(self, name, _amount(getattr(self, name), name))
        object.__setattr__(
            self,
            "code_demand_usd",
            _amount(self.code_demand_usd, "code_demand_usd", allow_none=True),
        )
        object.__setattr__(
            self,
            "supplementary_high",
            _flag(self.supplementary_high, "supplementary_high", allow_none=True),
        )

    @property
    def end(self) -> dt.date:
        """その月の末日（recent 窓の起点）。"""
        year, month = int(self.month[:4]), int(self.month[5:7])
        return dt.date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class SubjectHistory:
    """1ユーザぶんの判定材料。months の最後が分析対象月。

    months は月の昇順で、同じ月を2つ持てない（どちらが対象月か決まらないため）。
    seat_events・unclassified はその subject に帰属する分だけを渡す（帰属の解決は
    呼び出し側の責務）。

    credit_limit_usd は追加クレジット上限 κ。None は「設定が分からない」で、0 は
    「従量課金が無効」という別の状態を表す。無制限の設定は Infinity で表せる。負の値に
    対応する状態は無いので受け付けない（黙って「無効」として扱わない）。
    """

    email: str
    current_seat: str
    credit_limit_usd: float | None
    identity_conflict: bool
    months: tuple[MonthObservation, ...]
    seat_events: tuple[SeatChangeEvent, ...]
    unclassified: tuple[UnclassifiedObservation, ...]

    def __post_init__(self) -> None:
        for name in ("email", "current_seat"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(
                    f"{name} には str が必要です: {type(getattr(self, name)).__name__}"
                )
        object.__setattr__(
            self,
            "credit_limit_usd",
            _amount(
                self.credit_limit_usd,
                "credit_limit_usd",
                allow_none=True,
                allow_infinite=True,
                non_negative=True,
            ),
        )
        object.__setattr__(
            self, "identity_conflict", _flag(self.identity_conflict, "identity_conflict")
        )
        for name in ("months", "seat_events", "unclassified"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not self.months:
            raise ValueError("months には1ヶ月以上の観測が必要です")
        previous: str | None = None
        for month in self.months:
            if not isinstance(month, MonthObservation):
                raise TypeError(
                    f"months の要素には MonthObservation が必要です: "
                    f"{type(month).__name__}"
                )
            if previous is not None and month.month <= previous:
                raise ValueError(
                    f"months は月の昇順で重複なく並べてください: "
                    f"{previous!r} のあとに {month.month!r}"
                )
            previous = month.month

    @property
    def target(self) -> MonthObservation:
        """分析対象月（months の最後）。"""
        return self.months[-1]


@dataclass(frozen=True)
class DecisionV2:
    """1ユーザに対するV2判定の結論。

    語彙は StrEnum どうしで等値になるため、受け取る値を isinstance で検証する
    （生の文字列・別の語彙のメンバーは、値が同じでも受け付けない）。
    """

    status: DecisionStatus
    seat_action: SeatAction
    credit_action: CreditAction
    reason_codes: tuple[ReasonCode, ...]

    def __post_init__(self) -> None:
        for name, vocabulary in (
            ("status", DecisionStatus),
            ("seat_action", SeatAction),
            ("credit_action", CreditAction),
        ):
            value = getattr(self, name)
            if not isinstance(value, vocabulary):
                raise TypeError(
                    f"{name} には {vocabulary.__name__} が必要です: "
                    f"{value!r} ({type(value).__name__})"
                )
        if not isinstance(self.reason_codes, tuple):
            raise TypeError(
                f"reason_codes には tuple が必要です: "
                f"{type(self.reason_codes).__name__}"
            )
        for code in self.reason_codes:
            if not isinstance(code, ReasonCode):
                raise TypeError(
                    f"reason_codes の要素には ReasonCode が必要です: "
                    f"{code!r} ({type(code).__name__})"
                )


class _Settings(NamedTuple):
    """昇格判定が使う設定値だけを取り出したもの。"""

    standard_price_usd: float
    premium_price_usd: float
    standard_allowance_usd: dict[str, float]
    premium_allowance_usd: dict[str, float]
    min_complete_months: int
    min_code_demand_usd: float
    min_saving_usd: float
    recent_seat_change_days: int
    cap_tolerance_usd: float


def _settings(cfg: Mapping) -> _Settings:
    """検証済み config から必要な値を取り出す（欠けていれば設定の破損として落ちる）。

    decision_v2.enabled はここでは見ない。有効・無効の分岐は結線側の責務で、この関数は
    「呼ばれたら判定する」ことだけを担う。
    """
    seats = cfg["seats"]
    upgrade = cfg["decision_v2"]["upgrade"]
    return _Settings(
        standard_price_usd=float(seats["standard"]["price_usd"]),
        premium_price_usd=float(seats["premium"]["price_usd"]),
        standard_allowance_usd={
            scenario: float(seats["standard"]["allowance_usd"][scenario])
            for scenario in _SCENARIOS
        },
        premium_allowance_usd={
            scenario: float(seats["premium"]["allowance_usd"][scenario])
            for scenario in _SCENARIOS
        },
        min_complete_months=int(upgrade["min_complete_months"]),
        min_code_demand_usd=float(upgrade["min_code_demand_usd"]),
        min_saving_usd=float(cfg["decision_v2"]["min_assignment_saving_usd"]),
        recent_seat_change_days=int(cfg["decision_v2"]["recent_seat_change_days"]),
        cap_tolerance_usd=float(cfg["usage_credits"]["cap_tolerance_usd"]),
    )


def decide_upgrade(subject: SubjectHistory, cfg: Mapping) -> DecisionV2:
    """Standard ユーザ1人ぶんの昇格判定（設計書 §12.4・§12.7）。

    上から順に見て、最初に該当したところで確定する:

    1. identity conflict・部分月・履歴不足は判定しない（hard blocker）
    2. 直近のシート変更・加入・分類できない観測に重なるユーザは観察へ倒す
    3. 経済軸（観測・追加クレジット上限到達・純モデルのいずれか）が成立しなければ現状維持
    4. 成立したら分類軸で、Code 主体なら昇格推奨、そうでなければアサインの見直し

    current seat が Standard 以外のときは ValueError。unknown・unassigned・premium の
    振り分けは呼び出し側が決める（ここで既定の結論を持つと、呼び出し側が分岐を書き
    忘れたことに気づけない）。
    """
    if subject.current_seat != _STANDARD:
        raise ValueError(
            f"昇格判定の対象は current_seat が {_STANDARD!r} のユーザだけです: "
            f"{subject.current_seat!r}"
        )
    settings = _settings(cfg)

    if subject.identity_conflict:
        return _decision(
            DecisionStatus.NO_DECISION, SeatAction.NONE, [ReasonCode.IDENTITY_CONFLICT]
        )
    target = subject.target
    if not target.complete:
        return _decision(
            DecisionStatus.NO_DECISION, SeatAction.NONE, [ReasonCode.PARTIAL_MONTH]
        )
    complete_months = sum(1 for month in subject.months if month.complete)
    if complete_months < settings.min_complete_months:
        return _decision(
            DecisionStatus.NO_DECISION,
            SeatAction.NONE,
            [ReasonCode.INSUFFICIENT_HISTORY],
        )

    recent = _recent_reasons(subject, target.end, settings.recent_seat_change_days)
    if recent:
        return _decision(DecisionStatus.OBSERVE, SeatAction.NONE, recent)

    observed = _observed_overage(target, settings)
    credit_reached = _credit_reached(subject.credit_limit_usd, target, settings)
    model = _model_favors_premium(target, settings)
    if not (observed or credit_reached or model):
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])

    # 候補になった根拠。分類軸がどちらへ振り分けても消さない（結論だけが変わるのであって、
    # 候補化の観測は同じもの。decision-evidence.csv で月をまたいで突き合わせる対象になる）
    evidence: list[ReasonCode] = []
    if credit_reached:
        evidence.append(ReasonCode.CREDIT_LIMIT_REACHED)
    if _sustained_overage(subject, settings):
        evidence.append(ReasonCode.SUSTAINED_OVERAGE)

    if target.code_demand_usd is None:
        # Code 主体であることを証明できないまま自動で昇格を推奨しない
        return _decision(
            DecisionStatus.OBSERVE,
            SeatAction.NONE,
            [*evidence, ReasonCode.DATA_CONFIDENCE_LOW],
        )

    if target.code_demand_usd < settings.min_code_demand_usd:
        # 費用は見合うが中身が Code ではない。シートの前にアサインを人が見直す
        reasons = [ReasonCode.REVIEW_NON_CODE_USAGE, *evidence]
        if target.supplementary_high:
            reasons.append(ReasonCode.HIGH_SUPPLEMENTARY_USAGE)
        return _decision(
            DecisionStatus.RECOMMENDED, SeatAction.REVIEW_ASSIGNMENT, reasons
        )

    # 判定が直近1完全月の需要に基づくことを、基本理由として常に明示する
    reasons = [ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND, *evidence]
    # 5時間枠・週次上限は観測できない。この理由だけで保留にはせず情報として付ける（§12.3）
    reasons.append(ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE)
    if target.supplementary_high:
        reasons.append(ReasonCode.HIGH_SUPPLEMENTARY_USAGE)
    return _decision(
        DecisionStatus.RECOMMENDED, SeatAction.UPGRADE_TO_PREMIUM, reasons
    )


def _decision(
    status: DecisionStatus, seat_action: SeatAction, reasons: Iterable[ReasonCode]
) -> DecisionV2:
    """結論を組み立てる。追加クレジットの提案はこのルールの担当外（常に NONE）。"""
    return DecisionV2(
        status=status,
        seat_action=seat_action,
        credit_action=CreditAction.NONE,
        reason_codes=_ordered(reasons),
    )


def _ordered(reasons: Iterable[ReasonCode]) -> tuple[ReasonCode, ...]:
    """理由コードを固定順に並べ、重複を1つに畳む。"""
    unique = dict.fromkeys(reasons)
    return tuple(sorted(unique, key=_REASON_RANK.__getitem__))


def _recent_reasons(
    subject: SubjectHistory, end: dt.date, days: int
) -> list[ReasonCode]:
    """recent 窓（対象月末から遡る days 日）に重なる観測の理由コード（§12.7）。

    event の区間は「changed_after より後・changed_before 以前」なので、窓 [end-days, end]
    との重なりは changed_before >= end-days かつ changed_after < end で判定する。
    スナップショットの間隔が広く変更時点を絞れないほど区間は広がり、重なりやすくなる
    （保留側へ倒れる）。

    分類できない観測（§10.4）は event が無くても保留側へ倒す。逆に event が1件も無いこと
    自体は発火させない（スナップショットのペアが無いだけの状態を hard blocker にしない）。
    """
    start = end - dt.timedelta(days=days)

    def overlaps(changed_after: dt.date, changed_before: dt.date) -> bool:
        return changed_before >= start and changed_after < end

    reasons: list[ReasonCode] = []
    for event in subject.seat_events:
        if not overlaps(event.changed_after, event.changed_before):
            continue
        reasons.append(
            ReasonCode.RECENT_MEMBER
            if event.event_type == _MEMBER_ADDED
            else ReasonCode.RECENT_SEAT_CHANGE
        )
    for observation in subject.unclassified:
        if overlaps(observation.changed_after, observation.changed_before):
            reasons.append(ReasonCode.DATA_CONFIDENCE_LOW)
    return reasons


def _observed_overage(month: MonthObservation, settings: _Settings) -> bool:
    """観測経路: 実課金を含む現状の費用が、Premium へ変えた場合の試算より高いか。

    現シートの費用は観測値（シート料 + 実課金）、変更先は allowance モデルの試算だが、
    込み量の大小関係（Standard の込み量 ≤ Premium の込み量）から、Premium での超過課金は
    現在の実課金を超えない。V1 の analyze._costs_for（standard 分岐）と同じ拘束。
    """
    overage = max(0.0, month.total_demand_usd - settings.premium_allowance_usd["mid"])
    standard_cost = settings.standard_price_usd + month.billed_usd
    premium_cost = settings.premium_price_usd + min(overage, month.billed_usd)
    return _saving_qualifies(standard_cost - premium_cost, settings)


def _saving_qualifies(saving: float, settings: _Settings) -> bool:
    """削減見込みが候補の条件を満たすか。

    候補条件は「Standard の費用が Premium より高い」こと（§12.4）なので、同額
    （削減見込み 0）は満たさない。閾値を 0 に設定した場合でもこの関係は変わらない。
    """
    return saving > 0.0 and saving >= settings.min_saving_usd


def _credit_reached(
    credit_limit_usd: float | None, month: MonthObservation, settings: _Settings
) -> bool:
    """追加クレジット上限 κ への到達経路。

    κ が分からない（None）・無効（0）・無制限（Infinity）のときは到達を判定できない。
    到達には課金の発生が論理的に必要なので、κ が許容差以下の設定でも実課金ゼロを到達と
    読まない（V1 の analyze.credits と同じガード）。
    """
    if credit_limit_usd is None or not math.isfinite(credit_limit_usd):
        return False
    if credit_limit_usd <= 0.0:
        return False
    return month.billed_usd > 0.0 and (
        month.billed_usd >= credit_limit_usd - settings.cap_tolerance_usd
    )


def _model_favors_premium(month: MonthObservation, settings: _Settings) -> bool:
    """純モデル経路: 実課金による拘束を置かず、需要だけで Premium が有利か。

    low/mid/high のうち複数のシナリオで Premium が安く、かつ mid の削減見込みが閾値以上の
    ときだけ候補にする（1シナリオだけの一致は allowance 推定の誤差と区別できない）。
    """
    agreeing = 0
    mid_saving = 0.0
    for scenario in _SCENARIOS:
        standard_cost = settings.standard_price_usd + max(
            0.0, month.total_demand_usd - settings.standard_allowance_usd[scenario]
        )
        premium_cost = settings.premium_price_usd + max(
            0.0, month.total_demand_usd - settings.premium_allowance_usd[scenario]
        )
        if premium_cost < standard_cost:
            agreeing += 1
        if scenario == "mid":
            mid_saving = standard_cost - premium_cost
    return agreeing >= _MIN_AGREEING_SCENARIOS and _saving_qualifies(
        mid_saving, settings
    )


def _sustained_overage(subject: SubjectHistory, settings: _Settings) -> bool:
    """観測経路が直近から連続する完全月で続いているか。

    対象月から古い方へ遡り、完全月かつ観測経路が成立するあいだ数える。データが無い月は
    履歴に現れないため、連続の判定は暦の隣接ではなく渡された履歴の並びで行う。
    """
    run = 0
    for month in reversed(subject.months):
        if not month.complete or not _observed_overage(month, settings):
            break
        run += 1
    return run >= _SUSTAINED_MONTHS
