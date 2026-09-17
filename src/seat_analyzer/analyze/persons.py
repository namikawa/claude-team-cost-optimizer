"""人（email）の層と、副スペースの払い出し・継続の参考判定（設計書 §26.4〜§26.5）。

アカウント層＝(email, workspace) の集計と V1 判定は analyze() が workspace ごとに
済ませている。ここはその結果を email で束ね直し、人の行（保有シート・シート費合計・
需要合計・主と副の内訳）と、副スペースについての2つの仮説（払い出すべきか・戻す
べきか）の参考判定を作る。V1 の主判定（変更推奨）はここでは変えない。

共通の物差しは副スペースの損益分岐 secondary_breakeven_usd（既定＝副の fixed_seat の
価格）。追加クレジットは API 等価単価で課金されるので、副アカウントの需要は「同じ
利用を副なしでクレジットに払った場合の額」であり、副のシート料と直接比較できる。

I/O は持たない純粋関数だけを置く（members スナップショットの読み取りが要る
「払い出した月」は workspaces.analyze_org が解決して first_seen として渡す）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from .credits import CREDIT_DISABLED, CREDIT_ENABLED, CREDIT_UNKNOWN, credit_reached
from .pipeline import SEAT_LABELS, STATUS_CHANGE, AnalysisResult

if TYPE_CHECKING:  # 実行時の import は循環する（workspaces がこのモジュールを呼ぶため）
    from .workspaces import OrgAnalysisResult, WorkspaceContext

# 払い出し判定（副を持たない人に副を払い出すべきか）のステータス。report の表示と結合する。
PAYOUT_CANDIDATE = "候補"
PAYOUT_WATCH = "観察"
PAYOUT_UNNEEDED = "不要"
PAYOUT_NO_EVIDENCE = "判断材料なし"

# 継続判定（副を持つ人が副を活用しているか）のステータス。report の表示と結合する。
CONTINUE = "継続"
RETURN_CANDIDATE = "戻す候補"
WAITING = "データ蓄積待ち"
CONTINUATION_WATCH = "観察"

# 人の表の列（person_frame はこの順で並ぶ。loc_with_cc はデータがあるときだけ足す）
PERSON_COLUMNS = (
    "email", "department", "team", "role", "note",
    "n_accounts", "seats", "primary_seat", "seat_cost_usd",
    "api_cost_usd", "billed_extra_usd", "cost_current_usd",
    "primary_api_cost_usd", "primary_billed_usd",
    "secondary_api_cost_usd", "secondary_billed_usd", "secondary_ratio",
    "status", "monthly_saving_usd",
)

# シート費を数えるシート種別（未割当・不明は費用が発生しないので数えない）
_PRICED_SEATS = ("standard", "premium")


def _number(value, default: float = 0.0) -> float:
    """欠損を既定値に倒した float（None・NaN・pd.NA を同じに扱う）。"""
    if value is None or pd.isna(value):
        return default
    return float(value)


def _optional_number(value) -> float | None:
    """欠損は None のままにする float（コスト・削減額のように「無い」が意味を持つ列）。"""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _text(value) -> str:
    """欠損を空文字に倒した文字列（部署・チームのような属性列）。"""
    if value is None or pd.isna(value):
        return ""
    return str(value)


@dataclass(frozen=True)
class Account:
    """1人の1 workspace 分のアカウント（V1 の users 行のうち人の層が読む値）。

    api_cost_usd はそのアカウント自身の需要。主の users の需要列は複数アカウント
    保有者では合算値になるため、ここは月次表（AnalysisResult.monthly）から取る。
    """

    workspace: str
    seat: str
    api_cost_usd: float
    billed_usd: float
    cost_current_usd: float | None
    status: str
    monthly_saving_usd: float | None
    credit_limit_usd: float
    credits_mode: str
    loc_with_cc: int | None


@dataclass(frozen=True)
class Person:
    """人（email）1人ぶん。accounts は主が先で、以降は workspace の並び順。

    部署・チーム・職種・備考は人の属性なので、最初のアカウントの行から取る
    （members-info は組織直下の1つを全 workspace が読むため全アカウントで同じ）。
    primary_workspace は主 workspace の名前、seat_prices はシート種別 → 月額で、
    どちらもシート費と主／副の内訳を人の側で決めるために持つ。
    """

    email: str
    department: str
    team: str
    role: str
    note: str
    accounts: tuple[Account, ...]
    primary_workspace: str
    seat_prices: Mapping[str, float]

    @property
    def primary(self) -> Account | None:
        """主 workspace のアカウント（副にだけアカウントがある人は None）。"""
        for account in self.accounts:
            if account.workspace == self.primary_workspace:
                return account
        return None

    @property
    def secondaries(self) -> tuple[Account, ...]:
        """主以外のアカウント（workspace の並び順）。"""
        return tuple(a for a in self.accounts if a.workspace != self.primary_workspace)

    @property
    def n_accounts(self) -> int:
        return len(self.accounts)

    @property
    def api_cost_usd(self) -> float:
        """全アカウントの需要合計（どのくらい利活用しているかは人の合算で見る）。"""
        return round(sum(a.api_cost_usd for a in self.accounts), 2)

    @property
    def billed_usd(self) -> float:
        return round(sum(a.billed_usd for a in self.accounts), 2)

    @property
    def seat_cost_usd(self) -> float:
        """保有シートの月額合計（未割当・不明は $0）。"""
        return round(
            sum(float(self.seat_prices.get(a.seat, 0.0)) for a in self.accounts), 2)

    @property
    def cost_current_usd(self) -> float | None:
        """現状費用の合計（どのアカウントも試算対象外なら None）。"""
        values = [a.cost_current_usd for a in self.accounts if a.cost_current_usd is not None]
        return round(sum(values), 2) if values else None

    @property
    def primary_api_cost_usd(self) -> float:
        account = self.primary
        return account.api_cost_usd if account is not None else 0.0

    @property
    def primary_billed_usd(self) -> float:
        account = self.primary
        return account.billed_usd if account is not None else 0.0

    @property
    def secondary_api_cost_usd(self) -> float:
        return round(sum(a.api_cost_usd for a in self.secondaries), 2)

    @property
    def secondary_billed_usd(self) -> float:
        return round(sum(a.billed_usd for a in self.secondaries), 2)

    @property
    def secondary_ratio(self) -> float | None:
        """需要のうち副で使った割合（需要が無い人は None）。"""
        total = self.api_cost_usd
        if total <= 0.0:
            return None
        return round(self.secondary_api_cost_usd / total, 4)


@dataclass(frozen=True)
class PayoutJudgment:
    """副を持たない人への払い出し判定（§26.5-1）。

    streak_months は対象月を末尾とする「主の実課金 ≥ 損益分岐」の連続月数、
    cap_reached は対象月に主の追加クレジット上限へ到達したか。monthly_billed は
    主の月別実課金（月の昇順）で、code_ratio と loc_with_cc は「さらに仕事が進むか」を
    読むための材料。
    """

    email: str
    status: str
    reason: str
    streak_months: int
    cap_reached: bool
    billed_usd: float
    api_cost_usd: float
    monthly_billed: tuple[tuple[str, float], ...]
    code_ratio: float | None
    loc_with_cc: int | None


@dataclass(frozen=True)
class ContinuationJudgment:
    """副にシートを持つアカウントの継続判定（§26.5-2）。

    complete_months は払い出した月より後の月数（払い出した月は不完全月として数えない）、
    streak_months は直近の完全月から遡って「副の需要 < 損益分岐」が続いた月数。
    saving_usd は戻す候補のときの削減見込み（副のシート料 − 副の需要）で、
    over_primary_cap_months は副の需要が主の追加クレジット上限を超えた月＝
    クレジットでは賄えなかった量が観測された月。
    """

    email: str
    workspace: str
    seat: str
    status: str
    complete_months: int
    streak_months: int
    api_cost_usd: float
    breakeven_usd: float
    saving_usd: float | None
    idle: bool
    over_primary_cap_months: tuple[str, ...]
    monthly_demand: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class MultiAccountBilling:
    """副を持ちながら主で実課金が発生した人（§26.5-3。判定ではなく事実の一覧）。

    monthly は主の月別の (月, 主の実課金, 副の需要合計)。副に切り替えずクレジットを
    使っている運用か、両方の枠を使い切っているかを並べて読むための材料。
    """

    email: str
    billed_usd: float
    secondary_api_cost_usd: float
    monthly: tuple[tuple[str, float, float], ...]


@dataclass(frozen=True)
class PersonLayer:
    """人の層の一式（人の行と §26.5 の参考判定）。

    breakeven_usd と evaluation_months は払い出し判定に使った損益分岐と連続月数。
    副 workspace が1つも無い組織では判定を行わず、どちらも None になる。
    """

    persons: tuple[Person, ...]
    frame: pd.DataFrame
    payout: tuple[PayoutJudgment, ...]
    continuation: tuple[ContinuationJudgment, ...]
    billed_with_secondary: tuple[MultiAccountBilling, ...]
    breakeven_usd: float | None
    evaluation_months: int | None


@dataclass(frozen=True)
class _Policy:
    """§26.5 の判定に効く設定（組織ごとに1度だけ解決する）。"""

    cfg: dict
    contexts: Mapping[str, WorkspaceContext]
    settings: Mapping[str, object]
    payout_workspace: str
    idle_usd: float
    tolerance: float

    def breakeven_for(self, workspace: str) -> float:
        """その workspace の損益分岐（USD/月）。

        組織の secondary_breakeven_usd が最優先で、無ければその workspace の
        fixed_seat の価格、それも無ければ Premium の価格を使う。
        """
        configured = self.settings.get("secondary_breakeven_usd")
        if configured is not None:
            return float(configured)
        context = self.contexts.get(workspace)
        seat = context.fixed_seat if context is not None else None
        seats = self.cfg["seats"]
        if seat is not None and seat in seats:
            return float(seats[seat]["price_usd"])
        return float(seats["premium"]["price_usd"])

    def evaluation_months_for(self, workspace: str) -> int:
        """その workspace の判定に必要な連続月数（既定は decision.hysteresis_months）。"""
        context = self.contexts.get(workspace)
        if context is not None and context.evaluation_months is not None:
            return int(context.evaluation_months)
        return int(self.cfg["decision"]["hysteresis_months"])

    def seat_for(self, workspace: str) -> str:
        """その workspace が払い出すシート種別（fixed_seat。無ければ premium）。"""
        context = self.contexts.get(workspace)
        if context is not None and context.fixed_seat:
            return context.fixed_seat
        return "premium"


def _policy(org: OrgAnalysisResult, cfg: dict) -> _Policy | None:
    """§26.5 の設定を解決する（副 workspace が1つも無い組織では None）。

    副は「主でない workspace」で、対象月に飛ばした workspace も含める（判定に使う
    のは設定と主の観測なので、その月のデータが無くても払い出しの是非は問える）。
    """
    secondaries = [name for name in org.contexts if name != org.primary]
    if not secondaries:
        return None
    organizations = cfg.get("organizations") or {}
    settings = organizations.get(org.org) or {}
    trend = cfg.get("trend") or {}
    usage_credits = cfg.get("usage_credits") or {}
    return _Policy(
        cfg=cfg,
        contexts=org.contexts,
        settings=settings if isinstance(settings, dict) else {},
        payout_workspace=secondaries[0],
        idle_usd=float(trend.get("idle_usd", 1.0)),
        tolerance=float(usage_credits.get("cap_tolerance_usd", 5.0)),
    )


def _seat_prices(org: OrgAnalysisResult) -> dict[str, float]:
    """シート種別 → 月額（どの workspace も同じ設定なので主の summary から取る）。"""
    result = org.workspaces.get(org.primary)
    if result is None:
        result = next(iter(org.workspaces.values()), None)
    if result is None:
        return {}
    return {
        "standard": float(result.summary.get("seat_price_standard_usd", 0.0)),
        "premium": float(result.summary.get("seat_price_premium_usd", 0.0)),
    }


def _account(workspace: str, row: pd.Series, api_cost_usd: float) -> Account:
    """users の1行からアカウントを組む（列が無い任意項目は不明として扱う）。"""
    return Account(
        workspace=workspace,
        seat=str(row["current_seat"]),
        api_cost_usd=api_cost_usd,
        billed_usd=round(_number(row.get("billed_extra_usd")), 2),
        cost_current_usd=_optional_number(row.get("cost_current_usd")),
        status=str(row["status"]),
        monthly_saving_usd=_optional_number(row.get("monthly_saving_usd")),
        credit_limit_usd=_number(row.get("credit_limit_usd"), float("nan")),
        credits_mode=str(row.get("credits_mode") or CREDIT_UNKNOWN),
        loc_with_cc=(
            int(row["loc_with_cc"])
            if "loc_with_cc" in row.index and not pd.isna(row["loc_with_cc"]) else None
        ),
    )


def _own_demand(result: AnalysisResult) -> pd.Series:
    """対象月のアカウント自身の需要（email → api_cost）。"""
    return result.monthly[result.month].set_index("email")["api_cost"]


def _column_by_email(frame: pd.DataFrame, column: str) -> dict[str, float]:
    """月次表の1列を email → 値の辞書にする（月別の履歴を引くため）。"""
    return {
        str(email): float(value)
        for email, value in zip(frame["email"], frame[column], strict=False)
    }


def build_persons(org: OrgAnalysisResult) -> tuple[Person, ...]:
    """全 workspace のアカウント行を email で束ねる（email 昇順）。

    副にだけアカウントがある人も1人として数える（その人の primary は None）。
    単一 workspace の組織では人とアカウントが1対1になる。
    """
    prices = _seat_prices(org)
    accounts: dict[str, list[Account]] = {}
    attributes: dict[str, tuple[str, str, str, str]] = {}
    for name, result in org.workspaces.items():
        own = _own_demand(result)
        for _, row in result.users.iterrows():
            email = str(row["email"])
            demand = round(float(own[email]), 2) if email in own.index else 0.0
            accounts.setdefault(email, []).append(_account(name, row, demand))
            attributes.setdefault(email, tuple(
                _text(row.get(col)) for col in ("department", "team", "role", "note")
            ))
    return tuple(
        Person(
            email=email,
            department=attributes[email][0],
            team=attributes[email][1],
            role=attributes[email][2],
            note=attributes[email][3],
            accounts=tuple(accounts[email]),
            primary_workspace=org.primary,
            seat_prices=prices,
        )
        for email in sorted(accounts)
    )


def person_frame(persons: Sequence[Person],
                 contexts: Mapping[str, WorkspaceContext]) -> pd.DataFrame:
    """人の表（1人1行）。列は PERSON_COLUMNS（+ データがあれば loc_with_cc）。

    部署別・チーム別サマリはこの表から数える（アカウント数を人数として数えない）。
    status は「どれかのアカウントが変更推奨ならその値、無ければ主のアカウントの値」、
    monthly_saving_usd は変更推奨のアカウントの削減額の合計で、いずれも
    report の集計（_group_summary_rows）がそのまま読める形にしてある。
    seats は保有シートを「<workspace>=<シート種別>」で並べた文字列（表示の整形は
    レポート側の担当）。
    """
    order = {name: index for index, name in enumerate(contexts)}
    has_loc = any(a.loc_with_cc is not None for p in persons for a in p.accounts)
    columns = [*PERSON_COLUMNS, *(["loc_with_cc"] if has_loc else [])]
    rows = []
    for person in persons:
        accounts = sorted(person.accounts,
                          key=lambda a: order.get(a.workspace, len(order)))
        changing = [a for a in person.accounts if a.status == STATUS_CHANGE]
        fallback = person.primary or (person.accounts[0] if person.accounts else None)
        row = {
            "email": person.email,
            "department": person.department,
            "team": person.team,
            "role": person.role,
            "note": person.note,
            "n_accounts": person.n_accounts,
            "seats": "; ".join(f"{a.workspace}={a.seat}" for a in accounts),
            "primary_seat": person.primary.seat if person.primary is not None else "",
            "seat_cost_usd": person.seat_cost_usd,
            "api_cost_usd": person.api_cost_usd,
            "billed_extra_usd": person.billed_usd,
            "cost_current_usd": person.cost_current_usd,
            "primary_api_cost_usd": person.primary_api_cost_usd,
            "primary_billed_usd": person.primary_billed_usd,
            "secondary_api_cost_usd": person.secondary_api_cost_usd,
            "secondary_billed_usd": person.secondary_billed_usd,
            "secondary_ratio": person.secondary_ratio,
            "status": STATUS_CHANGE if changing else (
                fallback.status if fallback is not None else ""),
            "monthly_saving_usd": (
                round(sum(_number(a.monthly_saving_usd) for a in changing), 2)
                if changing else None
            ),
        }
        if has_loc:
            values = [a.loc_with_cc for a in person.accounts if a.loc_with_cc is not None]
            row["loc_with_cc"] = sum(values)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def _code_shares(result: AnalysisResult) -> dict[str, float]:
    """email → Code の需要比率（確定できない人は入れない）。"""
    usage = result.product_usage
    if usage is None or usage.features.empty:
        return {}
    shares = usage.features["code_demand_share"]
    return {
        str(email): float(value)
        for email, value in shares.items() if not pd.isna(value)
    }


def payout_judgments(persons: Sequence[Person], org: OrgAnalysisResult,
                     cfg: dict) -> tuple[PayoutJudgment, ...]:
    """副を持たない人への払い出し判定（§26.5-1。email 昇順）。

    対象は主にアカウントを持ち副に持たない人のうち、シートが standard / premium の
    もの。主の実課金が「副を足さずクレジットへ払っている額」なので、副の損益分岐と
    比べて候補・観察・不要を分け、実課金が上限到達を語らない状態（κ が無効・不明）と
    払い出すシート種別と主のシートが違う状態は判断材料なしにする。
    """
    policy = _policy(org, cfg)
    primary_result = org.workspaces.get(org.primary)
    if policy is None or primary_result is None:
        return ()
    breakeven = policy.breakeven_for(policy.payout_workspace)
    evaluation = policy.evaluation_months_for(policy.payout_workspace)
    expected_seat = policy.seat_for(policy.payout_workspace)
    billed_by_month = {
        month: _column_by_email(primary_result.monthly[month], "billed")
        for month in primary_result.months_used
    }
    shares = _code_shares(primary_result)

    judgments = []
    for person in persons:
        account = person.primary
        if account is None or person.secondaries:
            continue
        if account.seat not in _PRICED_SEATS:
            continue
        monthly_billed = tuple(
            (month, round(billed_by_month[month].get(person.email, 0.0), 2))
            for month in primary_result.months_used
        )
        billed = monthly_billed[-1][1] if monthly_billed else 0.0
        streak = 0
        for _, value in reversed(monthly_billed):
            if value < breakeven:
                break
            streak += 1
        cap_reached = credit_reached(account.credit_limit_usd, billed, policy.tolerance)

        reason = ""
        if account.seat != expected_seat:
            status = PAYOUT_NO_EVIDENCE
            reason = f"主が {SEAT_LABELS[account.seat]} のため V1 の昇格判定が先"
        elif account.credits_mode != CREDIT_ENABLED:
            status = PAYOUT_NO_EVIDENCE
            reason = ("主の追加クレジットが無効"
                      if account.credits_mode == CREDIT_DISABLED
                      else "主の追加クレジット上限が不明")
        elif cap_reached or streak >= evaluation:
            status = PAYOUT_CANDIDATE
        elif billed > 0.0:
            status = PAYOUT_WATCH
        else:
            status = PAYOUT_UNNEEDED
        judgments.append(PayoutJudgment(
            email=person.email,
            status=status,
            reason=reason,
            streak_months=streak,
            cap_reached=cap_reached,
            billed_usd=billed,
            api_cost_usd=person.api_cost_usd,
            monthly_billed=monthly_billed,
            code_ratio=shares.get(person.email),
            loc_with_cc=account.loc_with_cc,
        ))
    return tuple(judgments)


def _first_month(monthly_demand: Sequence[tuple[str, float]], default: str) -> str:
    """first_seen が渡されていないときの代替（需要が観測された最初の月）。"""
    for month, demand in monthly_demand:
        if demand > 0.0:
            return month
    return default


def continuation_judgments(
    persons: Sequence[Person],
    org: OrgAnalysisResult,
    cfg: dict,
    first_seen: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[ContinuationJudgment, ...]:
    """副のアカウントごとの継続判定（§26.5-2。email 昇順・同一人物内は workspace 順）。

    first_seen は workspace → email → 払い出した月（spend か members にその email が
    最初に現れた月）。払い出した月は不完全月として数えないため、連続月数の起点に使う。
    """
    policy = _policy(org, cfg)
    if policy is None:
        return ()
    seen = first_seen or {}
    demand_by_workspace = {
        name: {
            month: _column_by_email(result.monthly[month], "api_cost")
            for month in result.months_used
        }
        for name, result in org.workspaces.items()
    }

    judgments = []
    for person in persons:
        primary_cap = person.primary.credit_limit_usd if person.primary else float("nan")
        capped = (not pd.isna(primary_cap)
                  and not math.isinf(primary_cap) and primary_cap > 0.0)
        for account in person.secondaries:
            result = org.workspaces.get(account.workspace)
            if result is None or account.seat not in ("standard", "premium", "unknown"):
                continue
            by_month = demand_by_workspace[account.workspace]
            monthly_demand = tuple(
                (month, round(by_month[month].get(person.email, 0.0), 2))
                for month in result.months_used
            )
            breakeven = policy.breakeven_for(account.workspace)
            evaluation = policy.evaluation_months_for(account.workspace)
            start = seen.get(account.workspace, {}).get(person.email)
            if start is None:
                start = _first_month(monthly_demand, org.month)
            complete = [(m, v) for m, v in monthly_demand if m > start]
            streak = 0
            for _, value in reversed(complete):
                if value >= breakeven:
                    break
                streak += 1

            demand = account.api_cost_usd
            if demand >= breakeven:
                status = CONTINUE
            elif len(complete) < evaluation:
                status = WAITING
            elif all(v < breakeven for _, v in complete[-evaluation:]):
                status = RETURN_CANDIDATE
            else:
                status = CONTINUATION_WATCH
            saving = None
            if status == RETURN_CANDIDATE and account.seat in _PRICED_SEATS:
                saving = round(float(person.seat_prices.get(account.seat, 0.0)) - demand, 2)
            judgments.append(ContinuationJudgment(
                email=person.email,
                workspace=account.workspace,
                seat=account.seat,
                status=status,
                complete_months=len(complete),
                streak_months=streak,
                api_cost_usd=demand,
                breakeven_usd=breakeven,
                saving_usd=saving,
                idle=demand < policy.idle_usd,
                over_primary_cap_months=tuple(
                    m for m, v in monthly_demand if capped and v > primary_cap),
                monthly_demand=monthly_demand,
            ))
    return tuple(judgments)


def billed_with_secondary(persons: Sequence[Person],
                          org: OrgAnalysisResult) -> tuple[MultiAccountBilling, ...]:
    """副を持ちながら主で実課金が発生した人の一覧（§26.5-3。email 昇順）。"""
    primary_result = org.workspaces.get(org.primary)
    if primary_result is None:
        return ()
    billed_by_month = {
        month: _column_by_email(primary_result.monthly[month], "billed")
        for month in primary_result.months_used
    }
    demand_by_workspace = {
        name: {
            month: _column_by_email(result.monthly[month], "api_cost")
            for month in result.months_used
        }
        for name, result in org.workspaces.items() if name != org.primary
    }

    rows = []
    for person in persons:
        account = person.primary
        if account is None or not person.secondaries or account.billed_usd <= 0.0:
            continue
        workspaces = [a.workspace for a in person.secondaries]
        monthly = tuple(
            (
                month,
                round(billed_by_month[month].get(person.email, 0.0), 2),
                round(sum(
                    demand_by_workspace[name].get(month, {}).get(person.email, 0.0)
                    for name in workspaces
                ), 2),
            )
            for month in primary_result.months_used
        )
        rows.append(MultiAccountBilling(
            email=person.email,
            billed_usd=account.billed_usd,
            secondary_api_cost_usd=person.secondary_api_cost_usd,
            monthly=monthly,
        ))
    return tuple(rows)


def build_person_layer(
    org: OrgAnalysisResult,
    cfg: dict,
    first_seen: Mapping[str, Mapping[str, str]] | None = None,
) -> PersonLayer:
    """人の層の一式を組む（人の表と §26.5 の参考判定）。

    副 workspace が1つも無い組織では判定を行わず、人の表だけを持つ層になる
    （単一 workspace の組織では人とアカウントが1対1なので users と同じ内容になる）。
    """
    persons = build_persons(org)
    frame = person_frame(persons, org.contexts)
    policy = _policy(org, cfg)
    if policy is None:
        return PersonLayer(
            persons=persons, frame=frame, payout=(), continuation=(),
            billed_with_secondary=(), breakeven_usd=None, evaluation_months=None,
        )
    return PersonLayer(
        persons=persons,
        frame=frame,
        payout=payout_judgments(persons, org, cfg),
        continuation=continuation_judgments(persons, org, cfg, first_seen),
        billed_with_secondary=billed_with_secondary(persons, org),
        breakeven_usd=policy.breakeven_for(policy.payout_workspace),
        evaluation_months=policy.evaluation_months_for(policy.payout_workspace),
    )
