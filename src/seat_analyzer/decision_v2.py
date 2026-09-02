"""V2判定のシート変更ルール（昇格・降格）を決める純粋なモジュール。

判断は2つの軸でできている:

- 経済軸: シートを変える金銭的な根拠があるか。変更先シートの従量課金は観測も推定も
  できないので、変更先のコストは試算しない。使うのは観測できる事実（現シートの実課金・
  追加クレジット上限 κ への到達）と、方針として決めた需要の線
  （`decision_v2.premium_justification_usd`）だけ
- 分類軸: その需要の中身が Code か。昇格では経済軸を満たした候補を「自動で昇格を推奨する」
  ものと「アサインそのものを人が見直す（REVIEW_ASSIGNMENT）」ものへ振り分けるゲートに
  なり、降格では Code 実務者の席を自動では落とさないための歯止めになる

シートの込み枠を USD の月次プールとみなし `max(0, 需要 − allowance)` で変更先の課金を
試算する関数形は、追加クレジットが有効で実課金がセンサーとして効く組織の観測で反証された
（同一シート・同一月で課金の有無を分ける単一のしきい値が存在しない・課金が発生したあとに
需要が伸びても課金は止まる・課金が利用者間で同期しない）。実機構は月より短い周期のレート
制限と見られるが非公開なので、この判定では allowance を使わない（V1 は従来どおり使う）。

昇格と降格を毎月往復しないための歯止めは、Code ゲートとヒステリシスが担う。昇格は対象月の
Code 需要が `upgrade.min_code_demand_usd` 以上であることを要し、降格は評価窓の全完全月で
Code 需要が `downgrade.max_code_demand_usd` 未満であることを要するので、往復するには
実際の利用の変化が必要になる。加えて recent 窓（`recent_seat_change_days`）と降格の評価窓
（`downgrade.min_complete_months` 完全月）が、1ヶ月の振れで席が動くことを防ぐ。

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

# SUSTAINED_OVERAGE を付けるのに要る、直近から連続する完全月の数。追加クレジットが
# 無効な組織の昇格に要る継続性（§12.6）にも同じ月数を使う
_SUSTAINED_MONTHS = 2

# 追加クレジット消費の「継続上昇」とみなすのに要る、対象月内の観測点の数
_RISING_MIN_POINTS = 3

# 方針感度（§12.7）で方針線をずらす幅。シート差額（$100）と同じ大きさにしてある
_POLICY_STABILITY_OFFSET_USD = 100.0

# status を RECOMMENDED へ上げるアクション（人が取るべき作業があるもの）。語彙ごとに
# 別の組にする。StrEnum は値が等しければ語彙をまたいで == になるため、1つの組へ混ぜると
# 別の語彙のメンバーが一致してしまう（§12.1）
_ACTIONABLE_SEAT_ACTIONS = (
    SeatAction.UPGRADE_TO_PREMIUM,
    SeatAction.DOWNGRADE_TO_STANDARD,
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
    ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
    ReasonCode.CREDIT_SETTING_UNKNOWN,
    # 補助: 主理由を補強する観測
    ReasonCode.CREDIT_LIMIT_REACHED,
    ReasonCode.SUSTAINED_OVERAGE,
    ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
    ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP,
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

    シート料はシート差額（Premium − Standard）を出すために持つ。込み枠の推定
    （`seats.*.allowance_usd`）は使わないので取り出さない。
    """

    standard_price_usd: float
    premium_price_usd: float
    premium_justification_usd: float
    upgrade_min_complete_months: int
    min_code_demand_usd: float
    downgrade_min_complete_months: int
    max_code_demand_usd: float
    billing_margin_usd: float
    recent_seat_change_days: int
    cap_tolerance_usd: float


def _settings(cfg: Mapping) -> _Settings:
    """検証済み config から必要な値を取り出す（欠けていれば設定の破損として落ちる）。

    decision_v2.enabled はここでは見ない。有効・無効の分岐は結線側の責務で、この関数は
    「呼ばれたら判定する」ことだけを担う。
    """
    seats = cfg["seats"]
    decision = cfg["decision_v2"]
    upgrade = decision["upgrade"]
    downgrade = decision["downgrade"]
    return _Settings(
        standard_price_usd=float(seats["standard"]["price_usd"]),
        premium_price_usd=float(seats["premium"]["price_usd"]),
        premium_justification_usd=float(decision["premium_justification_usd"]),
        upgrade_min_complete_months=int(upgrade["min_complete_months"]),
        min_code_demand_usd=float(upgrade["min_code_demand_usd"]),
        downgrade_min_complete_months=int(downgrade["min_complete_months"]),
        max_code_demand_usd=float(downgrade["max_code_demand_usd"]),
        billing_margin_usd=float(decision["observed_billing_margin_usd"]),
        recent_seat_change_days=int(decision["recent_seat_change_days"]),
        cap_tolerance_usd=float(cfg["usage_credits"]["cap_tolerance_usd"]),
    )


def decide_upgrade(subject: SubjectHistory, cfg: Mapping) -> DecisionV2:
    """Standard ユーザ1人ぶんの昇格判定と追加クレジットの提案（§12.4・§12.6・§12.7）。

    上から順に見て、最初に該当したところで確定する:

    1. identity conflict・部分月・履歴不足は判定しない（hard blocker）
    2. 直近のシート変更・加入・分類できない観測に重なるユーザは観察へ倒す
    3. 追加クレジット上限 κ の3状態で、候補化に使う信号と結論の出し方が変わる（§12.6）
    4. 候補になったら分類軸へ。Code 主体なら昇格推奨、そうでなければアサインの見直し

    候補化に使う信号は3つある。「Standard の実課金がシート差額をマージ以上上回った」
    （観測）、「追加クレジット上限 κ へ到達した」（観測）、「全 product 需要が方針線
    `premium_justification_usd` 以上」（方針）。3 の内訳:

    - 有効（正の有限値・無制限）: 実課金という観測がある。実課金または κ 到達で候補にし、
      方針線は使わない
    - 無効（0）: 実課金が構造的に $0 で観測が候補化の材料にならないため、方針線で候補に
      する。継続性（2完全月連続、または対象月内の消費の継続上昇）があれば分類軸へ進み、
      一時的な成立では席を変えず、上限つきクレジットの付与で1ヶ月の課金を実測する
    - 不明（None）: 席を動かす判断は実課金だけで行う（有効か無効かが分からないので、
      実課金 $0 が「枠内に収まっている」のか「そもそも課金されない設定」なのか決められ
      ない）。方針線を上回っていれば席は動かさず、クレジット設定の確認へ回す

    κ が 0（無効）と記入されているのに対象月の実課金が正のときは、記入と観測が矛盾して
    いる（記入ミス、または月中の設定変更。V1 の analyze.credits も同じ状況を警告する）。
    観測を捨てるほうが影響が大きいので、この判定では κ 不明として扱う。見るのは対象月の
    実課金だけで、前月の課金は設定変更より前のものでありうるため参照しない。

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
    return _upgrade(subject, _settings(cfg))


def _upgrade(subject: SubjectHistory, settings: _Settings) -> DecisionV2:
    """昇格の規則そのもの（設定値を受ける形）。

    方針感度（`policy_stability`）が同じ規則を別の方針線で評価するため、config からの
    取り出しと現シートの検査だけを `decide_upgrade` に残して分けている。
    """
    blocked = _pre_checks(subject, settings, settings.upgrade_min_complete_months)
    if blocked is not None:
        return blocked

    target = subject.target
    credit_limit = subject.credit_limit_usd
    if credit_limit == 0.0 and target.billed_usd > 0.0:
        # 「無効」の記入と実課金の観測が矛盾している（記入ミスか月中の設定変更）。実課金と
        # いう観測を捨てないため κ 不明として扱う（対象月の実課金だけを見る）
        credit_limit = None

    billing = _standard_billing_exceeds_seat_gap(target, settings)
    reached = _credit_reached(credit_limit, target, settings)
    above = _above_policy_line(target, settings)
    rising = _credit_consumption_rising(subject, target.month)

    if credit_limit is None:
        # κ 不明: 席を動かす判断は実課金だけで行う。クレジット側は上限も有効・無効も
        # 分からず金額を断定できないので、付与ではなく設定の確認へ回す
        if billing:
            status, action, reasons = _classification_axis(
                target,
                settings,
                _observed_evidence(
                    subject, settings, billing=billing, reached=reached, rising=rising
                ),
            )
            reasons.append(ReasonCode.CREDIT_SETTING_UNKNOWN)
            return _decision(status, action, reasons, CreditAction.REVIEW)
        if above:
            # 需要は Premium を正当化する水準だが、κ が分からないと実課金 $0 の意味が
            # 決まらない。席は動かさず、設定の確認だけを作業として出す
            return _decision(
                DecisionStatus.KEEP,
                SeatAction.KEEP,
                [
                    ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE,
                    ReasonCode.CREDIT_SETTING_UNKNOWN,
                ],
                CreditAction.REVIEW,
            )
        # 設定の不明だけで人へ回すと、確認すべき相手が組織の全員になる
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])

    if credit_limit == 0.0:
        # κ 無効: 実課金が構造的に $0 になるため、観測は候補化の材料にならない。§12.6 の
        # 継続性ゲートをここに置く（週次スナップショットの継続上昇は継続の同等物）
        if not above:
            return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])
        # 候補になった根拠。結論がどこへ振り分けられても消さない（結論だけが変わるので
        # あって、候補化の観測は同じもの。月をまたいだ突き合わせの対象になる）
        evidence = [ReasonCode.TOTAL_DEMAND_ABOVE_PREMIUM_LINE]
        sustained = _policy_line_run(subject, settings) >= _SUSTAINED_MONTHS
        if sustained:
            evidence.append(ReasonCode.SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE)
        if rising:
            evidence.append(ReasonCode.CREDIT_CONSUMPTION_RISING)
        if sustained or rising:
            status, action, reasons = _classification_axis(target, settings, evidence)
            return _decision(status, action, reasons)
        # 一時的な成立では席を変えず、上限つきクレジットで1ヶ月の課金を実測する。分類軸
        # （Code ゲート）はかけない — 込み枠は product 共通で、上限つきの付与は可逆な
        # 計測手段なので、昇格と同じ強さで Code 主体であることを要求しない
        reasons = [*evidence]
        if target.supplementary_high:
            reasons.append(ReasonCode.HIGH_SUPPLEMENTARY_USAGE)
        return _decision(
            DecisionStatus.RECOMMENDED,
            SeatAction.KEEP,
            reasons,
            CreditAction.ENABLE_WITH_CAP,
        )

    # κ 有効: 実課金の観測がある。方針線は使わない（観測が語れる組織で方針で上書きしない）
    if not (billing or reached):
        return _decision(DecisionStatus.KEEP, SeatAction.KEEP, [])
    status, action, reasons = _classification_axis(
        target,
        settings,
        _observed_evidence(
            subject, settings, billing=billing, reached=reached, rising=rising
        ),
    )
    return _decision(status, action, reasons)


def decide_downgrade(subject: SubjectHistory, cfg: Mapping) -> DecisionV2:
    """Premium ユーザ1人ぶんの降格判定（設計書 §12.5・§12.7）。

    上から順に見て、最初に該当したところで確定する:

    1. identity conflict・部分月・履歴不足は判定しない（hard blocker）
    2. 直近のシート変更・加入・分類できない観測に重なるユーザは観察へ倒す
    3. 評価窓に実課金のある月があれば現状維持（Premium の込み枠を超えた観測がある）
    4. Code 需要が確定しない月があれば観察、Code 需要が高い月があれば現状維持
    5. Code 需要が低く supplementary が高いならアサインの見直し
    6. 評価窓の全月で全 product 需要が方針線を下回れば降格推奨、そうでなければ現状維持

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
    return _downgrade(subject, _settings(cfg))


def _downgrade(subject: SubjectHistory, settings: _Settings) -> DecisionV2:
    """降格の規則そのもの（設定値を受ける形。`_upgrade` と同じ理由で分けている）。"""
    blocked = _pre_checks(subject, settings, settings.downgrade_min_complete_months)
    if blocked is not None:
        return blocked

    target = subject.target
    # 評価窓は直近の完全月だけを新しい順に採る（間に部分月が挟まっても飛ばす）。必要な
    # 長さは `_pre_checks` の履歴不足の検査で保証されている
    complete = [month for month in subject.months if month.complete]
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
        # Code が低く supplementary が高いユーザは総需要が大きく、方針線を上回って
        # 経済軸が成立しないことが多い。経済軸より先に見るのは、その場合に現状維持で
        # 終わらせず「シートではなくアサインを人が見直す」To-Do として出すため（§12.5）
        return _decision(
            DecisionStatus.RECOMMENDED,
            SeatAction.REVIEW_ASSIGNMENT,
            [ReasonCode.REVIEW_NON_CODE_USAGE, ReasonCode.HIGH_SUPPLEMENTARY_USAGE],
        )

    if any(_above_policy_line(month, settings) for month in window):
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


def policy_stability(subject: SubjectHistory, cfg: Mapping) -> int | None:
    """方針線の感度（§12.7）: 方針線をずらしても同じ結論になるか。

    方針線を ±`_POLICY_STABILITY_OFFSET_USD` ずらした3通り（−・基準・+ の順）で判定し、
    基準と同じ seat_action になった数を返す（基準自身を含むので 1〜3）。3 は「線の置き場所
    に結論が依存していない」、1 は「線をどこへ引くかで結論が変わる」を表す。

    経済軸に到達しない判定では None を返す（hard blocker、または recent 窓で観察へ倒れた
    場合）。「3/3 で安定」と「方針線が判定に関与しなかった」を区別するため。

    current seat が Standard なら昇格、Premium なら降格の規則で評価する。それ以外は
    ValueError（decide_* と同じ流儀）。渡された config は書き換えない。
    """
    settings = _settings(cfg)
    if subject.current_seat == _STANDARD:
        decide, min_complete_months = _upgrade, settings.upgrade_min_complete_months
    elif subject.current_seat == _PREMIUM:
        decide, min_complete_months = _downgrade, settings.downgrade_min_complete_months
    else:
        raise ValueError(
            f"方針感度の対象は current_seat が {_STANDARD!r} か {_PREMIUM!r} の"
            f"ユーザだけです: {subject.current_seat!r}"
        )

    if _pre_checks(subject, settings, min_complete_months) is not None:
        return None

    # 方針線だけを差し替えた設定で −・基準・+ の順に1度ずつ評価する（NamedTuple の
    # _replace。入力の cfg には触れない）。ずらした値が 0 以下になっても需要との比較は
    # 定義されるので、そのまま使う
    actions = [
        decide(
            subject,
            settings._replace(
                premium_justification_usd=settings.premium_justification_usd + offset
            ),
        ).seat_action
        for offset in (-_POLICY_STABILITY_OFFSET_USD, 0.0, _POLICY_STABILITY_OFFSET_USD)
    ]
    baseline = actions[1]
    return sum(1 for action in actions if action is baseline)


def _pre_checks(
    subject: SubjectHistory, settings: _Settings, min_complete_months: int
) -> DecisionV2 | None:
    """昇格・降格に共通の前段（hard blocker と recent 窓）。

    該当すればその結論を返し、経済軸へ進める状態なら None を返す。方針感度も同じ関数を
    使うので、「判定が経済軸に到達したか」の定義が1箇所になる。
    """
    if subject.identity_conflict:
        return _decision(
            DecisionStatus.NO_DECISION, SeatAction.NONE, [ReasonCode.IDENTITY_CONFLICT]
        )
    target = subject.target
    if not target.complete:
        return _decision(
            DecisionStatus.NO_DECISION, SeatAction.NONE, [ReasonCode.PARTIAL_MONTH]
        )
    if sum(1 for month in subject.months if month.complete) < min_complete_months:
        return _decision(
            DecisionStatus.NO_DECISION,
            SeatAction.NONE,
            [ReasonCode.INSUFFICIENT_HISTORY],
        )
    recent = _recent_reasons(subject, target.end, settings.recent_seat_change_days)
    if recent:
        return _decision(DecisionStatus.OBSERVE, SeatAction.NONE, recent)
    return None


def _observed_evidence(
    subject: SubjectHistory,
    settings: _Settings,
    *,
    billing: bool,
    reached: bool,
    rising: bool,
) -> list[ReasonCode]:
    """実課金の観測で候補になったときの根拠（κ が有効・不明の経路で共通）。

    上限到達だけで候補になった場合は `STANDARD_BILLING_ABOVE_SEAT_GAP` を付けない。
    上限そのものが根拠であって、実課金がシート差額を上回ったことは立証していない（§12.4）。
    """
    evidence: list[ReasonCode] = []
    if reached:
        evidence.append(ReasonCode.CREDIT_LIMIT_REACHED)
    if _sustained_billing(subject, settings):
        evidence.append(ReasonCode.SUSTAINED_OVERAGE)
    if billing:
        evidence.append(ReasonCode.STANDARD_BILLING_ABOVE_SEAT_GAP)
    if rising:
        evidence.append(ReasonCode.CREDIT_CONSUMPTION_RISING)
    return evidence


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


def _standard_billing_exceeds_seat_gap(
    month: MonthObservation, settings: _Settings
) -> bool:
    """観測経路: Standard の実課金がシート差額をマージ以上上回っているか。

    見るのは実課金とシート差額（Premium − Standard）だけで、需要も込み枠の推定も使わない。
    変更先（Premium）の従量課金は観測できず推定もしないため、比較をこの差額に閉じている。

    マージを 0 に設定した場合でも、差額とちょうど同額は候補にしない（条件は「上回った」
    ことなので、同額は満たさない）。
    """
    seat_gap = settings.premium_price_usd - settings.standard_price_usd
    excess = month.billed_usd - seat_gap
    return excess > 0.0 and excess >= settings.billing_margin_usd


def _above_policy_line(month: MonthObservation, settings: _Settings) -> bool:
    """方針経路: 全 product 需要が方針線（`premium_justification_usd`）以上か。

    方針線は「この水準の需要なら Premium シートが正当化される」という決めた値で、込み枠の
    推定ではない。境界（ちょうど同額）は「以上」として満たす側に含める。
    """
    return month.total_demand_usd >= settings.premium_justification_usd


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


def _sustained_billing(subject: SubjectHistory, settings: _Settings) -> bool:
    """観測経路が直近から連続する完全月で続いているか。

    対象月から古い方へ遡り、完全月かつ観測経路が成立するあいだ数える。データが無い月は
    履歴に現れないため、連続の判定は暦の隣接ではなく渡された履歴の並びで行う。
    """
    run = 0
    for month in reversed(subject.months):
        if not month.complete or not _standard_billing_exceeds_seat_gap(
            month, settings
        ):
            break
        run += 1
    return run >= _SUSTAINED_MONTHS


def _policy_line_run(subject: SubjectHistory, settings: _Settings) -> int:
    """方針線を上回る月が直近から連続している数。

    走査の規則は `_sustained_billing` と同じ（不完全月・不成立で打ち切り、連続の判定は
    暦の隣接ではなく渡された履歴の並びで行う）。見る条件が実課金か需要かが違う。追加
    クレジットが無効な組織では実課金が語らないため、継続性の判定にはこちらを使う（§12.6）。
    """
    run = 0
    for month in reversed(subject.months):
        if not month.complete or not _above_policy_line(month, settings):
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
