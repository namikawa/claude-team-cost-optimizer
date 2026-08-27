"""V2判定のシート変更ルール（昇格・降格）を決める純粋なモジュール。

判断は2つの軸でできている:

- 経済軸: シートを変えたら金額として見合うか。シートの込み枠は product をまたいで
  共通なので、全 product 合算の需要と実課金で評価する
- 分類軸: その需要の中身が Code か。昇格では経済軸を満たした候補を「自動で昇格を推奨する」
  ものと「アサインそのものを人が見直す（REVIEW_ASSIGNMENT）」ものへ振り分けるゲートに
  なり、降格では Code 実務者の席を自動では落とさないための歯止めになる

設定は読み込まない。検証済みの config と、呼び出し側が組み立てた観測（SubjectHistory）
だけを受けて結論を返す（product_usage が policy を引数で受けるのと同じ流儀）。ファイルを
読まず、書かず、現在時刻も参照しないため、同じ入力からは常に同じ結論と同じ理由の並びを
返す。

扱うのはシートの昇格（Standard→Premium）と降格（Premium→Standard）、および昇格側に
重ねる追加クレジット（usage credits）の提案。降格側はクレジットの提案を持たない
（credit_action は常に CreditAction.NONE）。判定できる現シート以外をどう扱うか
（unknown を判定しない等）は呼び出し側の責務なので、ここでは ValueError にする。
分析パイプライン・出力へは未結線。

StrEnum は文字列として等値になり、語彙をまたいだ == が成立する（設計書 §12.1）。型では
混同を防げないので、境界の値オブジェクト DecisionV2 が受け取る語彙を isinstance で
検証する（QualityIssue と同じ流儀）。
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import NamedTuple

from .domain import CreditAction, DecisionStatus, ReasonCode, SeatAction
from .seat_changes import SeatChangeEvent, UnclassifiedObservation

# 判定の対象になる現シート。これ以外は呼び出し側の責務（この関数には持ち込まない）
_STANDARD = "standard"
_PREMIUM = "premium"

# 加入を表す event 種別（seat_changes.EVENT_TYPES の1つ）。加入直後は「直近のシート変更」
# とは別の理由コードで表す
_MEMBER_ADDED = "member_added"

# allowance のシナリオ。V1（analyze.SCENARIOS）と同じ顔ぶれ・同じ意味
_SCENARIOS = ("low", "mid", "high")

# 純モデル判定で候補とみなすのに要るシナリオ数（過半数）
_MIN_AGREEING_SCENARIOS = 2

# SUSTAINED_OVERAGE を付けるのに要る、直近から連続する完全月の数。追加クレジットが
# 無効な組織の昇格に要る継続性（§12.6）にも同じ月数を使う
_SUSTAINED_MONTHS = 2

# 追加クレジット消費の「継続上昇」とみなすのに要る、対象月内の観測点の数
_RISING_MIN_POINTS = 3

# status を RECOMMENDED へ上げるアクション（人が取るべき作業があるもの）。語彙ごとに
# 別の組にする。StrEnum は値が等しければ語彙をまたいで == になるため、1つの組へ混ぜると
# 別の語彙のメンバーが一致してしまう（§12.1）
_ACTIONABLE_SEAT_ACTIONS = (
    SeatAction.UPGRADE_TO_PREMIUM,
    SeatAction.REVIEW_ASSIGNMENT,
)
_ACTIONABLE_CREDIT_ACTIONS = (CreditAction.ENABLE_WITH_CAP, CreditAction.REVIEW)

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
    ReasonCode.SUSTAINED_LOW_CODE_DEMAND,
    ReasonCode.SUSTAINED_LOW_TOTAL_DEMAND,
    ReasonCode.REVIEW_NON_CODE_USAGE,
    ReasonCode.ESTIMATED_STANDARD_OVERAGE,
    ReasonCode.CREDIT_SETTING_UNKNOWN,
    # 補助: 主理由を補強する観測
    ReasonCode.CREDIT_LIMIT_REACHED,
    ReasonCode.SUSTAINED_OVERAGE,
    ReasonCode.PREMIUM_CHEAPER_THAN_STANDARD_WITH_CREDIT,
    ReasonCode.STANDARD_WITH_CREDIT_CHEAPER,
    ReasonCode.CREDIT_CONSUMPTION_RISING,
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
class CreditPoint:
    """追加クレジット消費の観測点1つ。

    taken_on はその値を写した日、mtd_usd はその時点での当月消費。月次でリセットされる
    値なので、2点の比較は同じ月の点どうしでだけ意味を持つ。

    値の由来（管理画面のスナップショット）はこのモジュールでは扱わない。点を組み立てて
    渡すのは呼び出し側の責務で、ここは渡された点だけを見る。

    負の値に対応する状態は無いので受け付けない。累計の消費が負になることは上流の語彙にも
    なく（正準の loader は負の当月消費を不明へ倒す）、受理すると -30→-20→-10 のような
    点列が「継続上昇」になってしまう。減少しうるのは点と点の差で、点そのものではない。
    """

    taken_on: dt.date
    mtd_usd: float

    def __post_init__(self) -> None:
        # 時刻を持つ datetime は比較と月の判定が変わるため受けない（admin_inputs と同じ規則）
        if not isinstance(self.taken_on, dt.date) or isinstance(
            self.taken_on, dt.datetime
        ):
            raise TypeError(
                f"taken_on には datetime.date が必要です: "
                f"{type(self.taken_on).__name__}"
            )
        object.__setattr__(
            self, "mtd_usd", _amount(self.mtd_usd, "mtd_usd", non_negative=True)
        )


@dataclass(frozen=True)
class SubjectHistory:
    """1ユーザぶんの判定材料。months の最後が分析対象月。

    months は月の昇順で、同じ月を2つ持てない（どちらが対象月か決まらないため）。
    seat_events・unclassified はその subject に帰属する分だけを渡す（帰属の解決は
    呼び出し側の責務）。

    credit_limit_usd は追加クレジット上限 κ。None は「設定が分からない」で、0 は
    「従量課金が無効」という別の状態を表す。無制限の設定は Infinity で表せる。負の値に
    対応する状態は無いので受け付けない（黙って「無効」として扱わない）。

    credit_points は追加クレジット消費（当月消費）の観測点で、取得日の昇順・重複なし。
    どの月の点を渡してもよく、判定は対象月の点だけを使う。空（既定）は「点が無い」で、
    消費がゼロだったことではない。
    """

    email: str
    current_seat: str
    credit_limit_usd: float | None
    identity_conflict: bool
    months: tuple[MonthObservation, ...]
    seat_events: tuple[SeatChangeEvent, ...]
    unclassified: tuple[UnclassifiedObservation, ...]
    credit_points: tuple[CreditPoint, ...] = ()

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
        for name in ("months", "seat_events", "unclassified", "credit_points"):
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
        previous_day: dt.date | None = None
        for point in self.credit_points:
            if not isinstance(point, CreditPoint):
                raise TypeError(
                    f"credit_points の要素には CreditPoint が必要です: "
                    f"{type(point).__name__}"
                )
            if previous_day is not None and point.taken_on <= previous_day:
                # 上昇の判定は並び順で行うため、並びと一意性を構築時に確かめる
                raise ValueError(
                    f"credit_points は取得日の昇順で重複なく並べてください: "
                    f"{previous_day} のあとに {point.taken_on}"
                )
            previous_day = point.taken_on

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
    """判定が使う設定値だけを取り出したもの。

    必要履歴と Code 需要の閾値は昇格・降格で別の値なので、どちらの向きの設定か
    名前で分かるようにする（min/max は比較の向きも表す）。
    """

    standard_price_usd: float
    premium_price_usd: float
    standard_allowance_usd: dict[str, float]
    premium_allowance_usd: dict[str, float]
    upgrade_min_complete_months: int
    min_code_demand_usd: float
    downgrade_min_complete_months: int
    max_code_demand_usd: float
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
    downgrade = cfg["decision_v2"]["downgrade"]
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
        upgrade_min_complete_months=int(upgrade["min_complete_months"]),
        min_code_demand_usd=float(upgrade["min_code_demand_usd"]),
        downgrade_min_complete_months=int(downgrade["min_complete_months"]),
        max_code_demand_usd=float(downgrade["max_code_demand_usd"]),
        min_saving_usd=float(cfg["decision_v2"]["min_assignment_saving_usd"]),
        recent_seat_change_days=int(cfg["decision_v2"]["recent_seat_change_days"]),
        cap_tolerance_usd=float(cfg["usage_credits"]["cap_tolerance_usd"]),
    )


def decide_upgrade(subject: SubjectHistory, cfg: Mapping) -> DecisionV2:
    """Standard ユーザ1人ぶんの昇格判定と追加クレジットの提案（§12.4・§12.6・§12.7）。

    上から順に見て、最初に該当したところで確定する:

    1. identity conflict・部分月・履歴不足は判定しない（hard blocker）
    2. 直近のシート変更・加入・分類できない観測に重なるユーザは観察へ倒す
    3. 経済軸（観測・追加クレジット上限到達・純モデルのいずれか）も推定ベースの超過も
       成立しなければ現状維持
    4. どちらかが成立したら、追加クレジット上限 κ の3状態で結論の出し方が変わる（§12.6）
       - 有効（正の有限値・無制限）: 経済軸が成立すれば分類軸へ。推定だけでは動かさない
         （実課金という観測が「枠内に収まっている」と言っているとき、推定で席を変えない）
       - 無効（0）: 実課金が構造的に $0 で観測が候補化の材料にならないため、継続性
         （2完全月連続、または対象月内の消費の継続上昇）を要求する。一時的な成立は席を
         変えず、上限つきクレジットの付与で1ヶ月の課金を実測する
       - 不明（None）: シート判定は経済軸で進めつつ、クレジットは金額を断定せず REVIEW
    5. 分類軸で、Code 主体なら昇格推奨、そうでなければアサインの見直し

    status は「人が取るべきアクションがあるか」で決まる。seat_action が
    UPGRADE_TO_PREMIUM・REVIEW_ASSIGNMENT のとき、または credit_action が
    ENABLE_WITH_CAP・REVIEW のときは RECOMMENDED になる。シート側が観察でも、
    クレジット側に作業（付与・設定の確認）があれば作業として出すため。

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
    if complete_months < settings.upgrade_min_complete_months:
        return _decision(
            DecisionStatus.NO_DECISION,
            SeatAction.NONE,
            [ReasonCode.INSUFFICIENT_HISTORY],
        )

    recent = _recent_reasons(subject, target.end, settings.recent_seat_change_days)
    if recent:
        return _decision(DecisionStatus.OBSERVE, SeatAction.NONE, recent)

    credit_limit = subject.credit_limit_usd
    economics = _premium_economics(credit_limit, target, settings)
    estimated = _estimated_standard_overage(target, settings)
    if not (economics or estimated):
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])

    # 候補になった根拠。結論がどちらへ振り分けられても消さない（結論だけが変わるのであって、
    # 候補化の観測は同じもの。decision-evidence.csv で月をまたいで突き合わせる対象になる）
    evidence: list[ReasonCode] = []
    if estimated:
        # 推定ベースの超過も候補化の観測なので、結論がどこへ振り分けられても残す（κ の状態と
        # 継続性で結論は変わるが、対象月の観測は同じもの）
        evidence.append(ReasonCode.ESTIMATED_STANDARD_OVERAGE)
    if _credit_reached(credit_limit, target, settings):
        evidence.append(ReasonCode.CREDIT_LIMIT_REACHED)
    if _sustained_overage(subject, settings):
        evidence.append(ReasonCode.SUSTAINED_OVERAGE)
    if _premium_cheaper_by_amount(target, settings):
        # 金額差で成立した候補にだけ付ける。上限到達だけで候補になった場合は、上限そのものが
        # 根拠であって「Standard + クレジットより Premium が安い」を立証していない（§12.4）
        evidence.append(ReasonCode.PREMIUM_CHEAPER_THAN_STANDARD_WITH_CREDIT)
    if _model_favors_standard(target, settings):
        evidence.append(ReasonCode.STANDARD_WITH_CREDIT_CHEAPER)
    rising = _credit_consumption_rising(subject, target.month)
    if rising:
        evidence.append(ReasonCode.CREDIT_CONSUMPTION_RISING)

    if credit_limit is None:
        # κ 不明: シート判定は止めない（経済軸は実課金と需要だけで評価できる）。クレジットは
        # 上限も有効・無効も分からず金額を断定できないので、付与ではなく設定の確認へ回す
        if economics:
            status, action, reasons = _classification_axis(target, settings, evidence)
        else:
            status, action, reasons = (
                DecisionStatus.KEEP,
                SeatAction.KEEP,
                list(evidence),
            )
        reasons.append(ReasonCode.CREDIT_SETTING_UNKNOWN)
        return _decision(status, action, reasons, CreditAction.REVIEW)

    if credit_limit == 0.0:
        # κ 無効: 実課金が構造的に $0 になるため、観測は「枠内に収まっている」ことを語らない。
        # §12.6 の継続性ゲートをここに置く（週次スナップショットの継続上昇は継続の同等物）
        sustained = _premium_economics_run(subject, settings) >= _SUSTAINED_MONTHS
        if economics and (sustained or rising):
            status, action, reasons = _classification_axis(target, settings, evidence)
            return _decision(status, action, reasons)
        # 一時的な成立・推定だけの成立では席を変えず、上限つきクレジットで1ヶ月の課金を
        # 実測する。分類軸（Code ゲート）はかけない — 込み枠は product 共通で、上限つきの
        # 付与は可逆な計測手段なので、昇格と同じ強さで Code 主体であることを要求しない
        reasons: list[ReasonCode] = [*evidence]
        if target.supplementary_high:
            reasons.append(ReasonCode.HIGH_SUPPLEMENTARY_USAGE)
        return _decision(
            DecisionStatus.RECOMMENDED,
            SeatAction.KEEP,
            reasons,
            CreditAction.ENABLE_WITH_CAP,
        )

    # κ 有効: 実課金の観測がある。経済軸が成立すれば分類軸へ振り分け、推定だけでは動かさない
    if not economics:
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])
    status, action, reasons = _classification_axis(target, settings, evidence)
    return _decision(status, action, reasons)


def decide_downgrade(subject: SubjectHistory, cfg: Mapping) -> DecisionV2:
    """Premium ユーザ1人ぶんの降格判定（設計書 §12.5・§12.7）。

    上から順に見て、最初に該当したところで確定する:

    1. identity conflict・部分月・履歴不足は判定しない（hard blocker）
    2. 直近のシート変更・加入・分類できない観測に重なるユーザは観察へ倒す
    3. 評価窓に実課金のある月があれば現状維持（Premium の込み枠を超えた観測がある）
    4. Code 需要が確定しない月があれば観察、Code 需要が高い月があれば現状維持
    5. Code 需要が低く supplementary が高いならアサインの見直し
    6. 経済軸が評価窓の全月で成立すれば降格推奨、そうでなければ現状維持

    誤った降格は業務を止めるため、判断は対象月だけでなく評価窓（直近の完全月
    `downgrade.min_complete_months` ヶ月）の全月で成立することを要求する。

    current seat が Premium 以外のときは ValueError。unknown・unassigned・standard の
    振り分けは呼び出し側が決める（decide_upgrade と同じ流儀。ここで既定の結論を持つと、
    呼び出し側が分岐を書き忘れたことに気づけない）。
    """
    if subject.current_seat != _PREMIUM:
        raise ValueError(
            f"降格判定の対象は current_seat が {_PREMIUM!r} のユーザだけです: "
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
    complete = [month for month in subject.months if month.complete]
    if len(complete) < settings.downgrade_min_complete_months:
        return _decision(
            DecisionStatus.NO_DECISION,
            SeatAction.NONE,
            [ReasonCode.INSUFFICIENT_HISTORY],
        )

    recent = _recent_reasons(subject, target.end, settings.recent_seat_change_days)
    if recent:
        return _decision(DecisionStatus.OBSERVE, SeatAction.NONE, recent)

    # 評価窓は直近の完全月だけを新しい順に採る（間に部分月が挟まっても飛ばす）。必要な
    # 長さは上の履歴不足の検査で保証されている
    window = complete[-settings.downgrade_min_complete_months:]

    # Premium での実課金は、需要が Premium の込み枠を超えた観測なので降格の候補にしない。
    # 追加クレジット上限への到達は実課金の発生を含意する（_credit_reached と同じ理屈）ため、
    # 上限到達の独立した検査は持たない。負の実課金（返金）は課金なしとして扱う
    if any(month.billed_usd > 0.0 for month in window):
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])

    if any(month.code_demand_usd is None for month in window):
        # Code 需要が低いことを証明できないまま自動で降格しない（昇格側の None と同じ思想）
        return _decision(
            DecisionStatus.OBSERVE, SeatAction.NONE, [ReasonCode.DATA_CONFIDENCE_LOW]
        )
    if any(month.code_demand_usd >= settings.max_code_demand_usd for month in window):
        # Code 実務者の席は自動で落とさない
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])

    if target.supplementary_high:
        # Code が低く supplementary が高いユーザは総需要が大きく、経済軸が成立しないことが
        # 多い。経済軸より先に見るのは、その場合に現状維持で終わらせず「シートではなく
        # アサインを人が見直す」To-Do として出すため（§12.5）
        return _decision(
            DecisionStatus.RECOMMENDED,
            SeatAction.REVIEW_ASSIGNMENT,
            [ReasonCode.REVIEW_NON_CODE_USAGE, ReasonCode.HIGH_SUPPLEMENTARY_USAGE],
        )

    # 評価窓の実課金は 0 と確定しているので、観測実課金による下限拘束（V1 の
    # analyze._costs_for の premium 分岐に相当）は自然に無効になり、需要だけの純モデル
    # 判定になる
    if not all(_model_favors_standard(month, settings) for month in window):
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])

    return _decision(
        DecisionStatus.RECOMMENDED,
        SeatAction.DOWNGRADE_TO_STANDARD,
        [
            ReasonCode.SUSTAINED_LOW_CODE_DEMAND,
            ReasonCode.SUSTAINED_LOW_TOTAL_DEMAND,
            # 5時間枠・週次上限は観測できない。この理由だけで保留にはせず情報として付ける（§12.3）
            ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE,
        ],
    )


def _decision(
    status: DecisionStatus,
    seat_action: SeatAction,
    reasons: Iterable[ReasonCode],
    credit_action: CreditAction = CreditAction.NONE,
) -> DecisionV2:
    """結論を組み立てる。追加クレジットの提案は既定では持たない（NONE）。"""
    return DecisionV2(
        status=_status(status, seat_action, credit_action),
        seat_action=seat_action,
        credit_action=credit_action,
        reason_codes=_ordered(reasons),
    )


def _status(
    status: DecisionStatus, seat_action: SeatAction, credit_action: CreditAction
) -> DecisionStatus:
    """人が取るべきアクションがあれば RECOMMENDED へ上げる（§12.6）。

    シート側が観察・現状維持でも、クレジット側に作業（付与・設定の確認）があれば作業として
    出す。どちらの側の作業なのかは seat_action と credit_action が示すので、status は
    「作業があるか」だけを表す。
    """
    if (
        seat_action in _ACTIONABLE_SEAT_ACTIONS
        or credit_action in _ACTIONABLE_CREDIT_ACTIONS
    ):
        return DecisionStatus.RECOMMENDED
    return status


def _classification_axis(
    target: MonthObservation, settings: _Settings, evidence: Sequence[ReasonCode]
) -> tuple[DecisionStatus, SeatAction, list[ReasonCode]]:
    """分類軸（§12.4 条件3）: 候補になった需要の中身が Code かで振り分ける。

    Code 需要が確定しなければ観察、閾値未満ならアサインの見直し、閾値以上なら昇格推奨。
    どの結論でも候補化の根拠（evidence）は残す。
    """
    if target.code_demand_usd is None:
        # Code 主体であることを証明できないまま自動で昇格を推奨しない
        return (
            DecisionStatus.OBSERVE,
            SeatAction.NONE,
            [*evidence, ReasonCode.DATA_CONFIDENCE_LOW],
        )

    if target.code_demand_usd < settings.min_code_demand_usd:
        # 費用は見合うが中身が Code ではない。シートの前にアサインを人が見直す
        reasons = [ReasonCode.REVIEW_NON_CODE_USAGE, *evidence]
        if target.supplementary_high:
            reasons.append(ReasonCode.HIGH_SUPPLEMENTARY_USAGE)
        return DecisionStatus.RECOMMENDED, SeatAction.REVIEW_ASSIGNMENT, reasons

    # 判定が直近1完全月の需要に基づくことを、基本理由として常に明示する
    reasons = [ReasonCode.ONE_MONTH_STRONG_CODE_DEMAND, *evidence]
    # 5時間枠・週次上限は観測できない。この理由だけで保留にはせず情報として付ける（§12.3）
    reasons.append(ReasonCode.CAPACITY_SIGNAL_UNAVAILABLE)
    if target.supplementary_high:
        reasons.append(ReasonCode.HIGH_SUPPLEMENTARY_USAGE)
    return DecisionStatus.RECOMMENDED, SeatAction.UPGRADE_TO_PREMIUM, reasons


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


def _premium_economics(
    credit_limit_usd: float | None, month: MonthObservation, settings: _Settings
) -> bool:
    """経済軸（§12.4 条件4）: 3経路のいずれかが成立するか。

    観測（実課金を含む現状の費用）・追加クレジット上限への到達・純モデル判定のどれか1つで
    候補になる。
    """
    return (
        _observed_overage(month, settings)
        or _credit_reached(credit_limit_usd, month, settings)
        or _model_favors_premium(month, settings)
    )


def _premium_cheaper_by_amount(month: MonthObservation, settings: _Settings) -> bool:
    """経済軸のうち、金額差で成立する2経路（観測・純モデル）。

    追加クレジット上限への到達だけは金額差ではなく上限そのものを根拠にするため（§12.4）、
    「Standard + クレジットより Premium が安い」と言えるかはこの2経路で判断する。
    """
    return _observed_overage(month, settings) or _model_favors_premium(month, settings)


def _estimated_standard_overage(month: MonthObservation, settings: _Settings) -> bool:
    """推定ベースの超過: 需要が Standard の込み量推定を超えているか。

    課金の観測を使わず、需要と allowance 推定だけで見る（追加クレジットが無効・不明な
    組織では実課金が構造的に $0 になり、超過が課金として現れないため）。

    複数シナリオでの超過を要求するのは純モデル判定と同じ理由で、1シナリオだけの超過は
    allowance 推定の誤差と区別できない。mid の超過額の閾値は
    `min_assignment_saving_usd` を再利用する（新しい設定キーを設けない。この額に届かない
    超過はクレジット付与の手間に見合わない、という同じ基準で足りる）。
    """
    agreeing = sum(
        1
        for scenario in _SCENARIOS
        if month.total_demand_usd > settings.standard_allowance_usd[scenario]
    )
    mid_overage = month.total_demand_usd - settings.standard_allowance_usd["mid"]
    return (
        agreeing >= _MIN_AGREEING_SCENARIOS and mid_overage >= settings.min_saving_usd
    )


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


def _model_favors_standard(month: MonthObservation, settings: _Settings) -> bool:
    """純モデル経路の鏡像: 需要だけで Standard が有利か（降格の経済軸）。

    条件の形は _model_favors_premium と同じで、比較の向きだけが逆。複数のシナリオで
    Standard が安く、かつ mid の削減見込みが閾値以上のときだけ候補にする。
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
        if standard_cost < premium_cost:
            agreeing += 1
        if scenario == "mid":
            mid_saving = premium_cost - standard_cost
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


def _premium_economics_run(subject: SubjectHistory, settings: _Settings) -> int:
    """経済軸が直近から連続して成立する完全月の数。

    走査の規則は `_sustained_overage` と同じ（不完全月・不成立で打ち切り、連続の判定は
    暦の隣接ではなく渡された履歴の並びで行う）。見る条件が観測経路だけか経済軸の3経路かが
    違う。追加クレジットが無効な組織では観測経路が成立しないため、継続性の判定には
    こちらを使う（§12.6）。
    """
    run = 0
    for month in reversed(subject.months):
        if not month.complete or not _premium_economics(
            subject.credit_limit_usd, month, settings
        ):
            break
        run += 1
    return run


def _credit_consumption_rising(subject: SubjectHistory, month: str) -> bool:
    """対象月内の追加クレジット消費が継続上昇しているか（§12.6）。

    当月消費は月次でリセットされる値なので、比較は対象月の点だけで行う（月をまたいだ差は
    増減を表さない）。点が `_RISING_MIN_POINTS` 個以上あり、取得日順に狭義単調増加して
    いることを要求する。横ばい（同額）は上昇と読まない — 消費が止まった状態と区別
    できないため。点は構築時に取得日の昇順で検証してあるので、絞り込んでも並びは保たれる。
    """
    values = [
        point.mtd_usd
        for point in subject.credit_points
        if _month_key(point.taken_on) == month
    ]
    if len(values) < _RISING_MIN_POINTS:
        return False
    return all(later > earlier for earlier, later in pairwise(values))


def _month_key(day: dt.date) -> str:
    """その日が属する月（YYYY-MM）。MonthObservation.month と同じ形にそろえる。"""
    return f"{day.year:04d}-{day.month:02d}"
