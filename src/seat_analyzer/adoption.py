"""Claude Code の活用と GitHub の成果を、人・集まり × 月の行へ並べる組み立て（設計書 §27.8）。

1 か月分の分析結果（`analyze.analyze_org` の戻り）と、その月の GitHub の材料を受けて、
その月にシートを持つ人ごとに1行を作る（人 × 月の表）。月をまたいで並べるのは呼び出し側で、
`person_rows` は 1 か月分の行だけを返す。各月の値は、その月を対象月として分析した結果から
取る。

チーム × 月の表は、全月の人 × 月の行を集まり（組織全体・部署・チーム）× 月へ畳んで作る
（`group_rows`）。集まりの値は人 × 月の行の値から出し、新しい集計の規則を作らない。例外は
lead time と PR の大きさで、人ごとの中央値は足し合わせられないので、その集まりの人が作った
PR の集合（`github_metrics.subject_prs`）から出す。前月比などの変化の計算はしない
（レポートの仕事）。

値の出どころは成果物と同じものに限る。需要とリクエスト数は usage-summary と同じ
`product_usage.features`、Claude Code が加えた行は report と同じ users の `loc_with_cc`、
PR とコミットの値は github-summary と同じ `pr_metrics`・`commit_metrics` の結果。ここで
新しい集計の規則を作らない（成果物と表の値が食い違わないようにするため）。

複数スペースの組織は人（email）で1行にまとめ、需要・リクエスト数・LoC はアカウントの値の
和にする。主の users の `api_cost_usd` は、2 アカウントの人では全 workspace の合算値に
置き換わっているので材料にしない（足すと二重に数える）。

確定できない値は None（空欄）にし、0 で埋めない。0 で埋めると「観測した結果が 0 だった」
ことと区別できなくなるため（usage-summary と同じ流儀）。

純粋関数として保つ。ファイル・ネットワーク・現在時刻を参照せず、同じ入力からは常に同じ
行・同じ並びを返す。並びは email の昇順（集まりは種別・名前・月の昇順）で明示的に決め、
集合の反復順には依らせない。

人ごとの値は参考値で、シートの判定には使わない。個人の順位づけに使うものではないので、
量の多い順には並べない（設計書 §15.6）。
"""

from __future__ import annotations

import enum
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

from . import ingest
from .analyze import AnalysisResult, OrgAnalysisResult
from .calendar_months import previous_month
from .github_collect import GithubMemberLink, GithubMembers, month_windows
from .github_metrics import (
    CommitMetrics,
    GithubMetrics,
    SubjectPr,
    summarize_lead_times,
)

# 複数スペースの人のシート種別をつなぐ記号（主を先頭に並べる）
SEAT_SEPARATOR = "+"

# 需要・リクエスト数の列（product_usage.features の列名と同じ名前で持つ）
_FEATURE_FIELDS = ("total_demand_usd", "code_demand_usd", "code_requests")

# Claude Code が加えた行の列（users の列名と同じ）
_LOC_COLUMN = "loc_with_cc"

# members-info から写す所属の列（users の列名と同じ）
_AFFILIATION_FIELDS = ("department", "team", "role")

# PR の集計から写す列と、コミットの集計から写す列
_PR_FIELDS = ("merged_pr_count", "lead_time_median_hours")
_COMMIT_FIELDS = ("commit_count", "lines_added_capped", "large_commits")


@enum.unique
class GithubData(enum.StrEnum):
    """その月の GitHub の材料の状態（`github_data` の列の値）。

    宣言順が判定の順で、上から順に最初に当てはまるものにする（`github_data_state`）。
    月ごとに全員同じ値になる。
    """

    # GitHub 分析を有効にしていない組織（GitHub の列はすべて空欄）
    NOT_CONFIGURED = "not_configured"
    # その月のキャッシュが無い（GitHub の列はすべて空欄）
    NO_CACHE = "no_cache"
    # キャッシュに repository の一覧が無い（GitHub の列はすべて空欄）
    NO_REPOSITORY_LIST = "no_repository_list"
    # キャッシュがコミットを持たない（PR の列だけ書く）
    NO_COMMITS = "no_commits"
    # 直前の月のキャッシュが無いか、コミットを持たない（PR の列だけ書く）
    NO_PREVIOUS_MONTH = "no_previous_month"
    # 上のどれでもない（PR とコミットの列を書く）
    OK = "ok"


# PR の列を書ける状態と、コミットの列も書ける状態
_PR_STATES = frozenset({GithubData.NO_COMMITS, GithubData.NO_PREVIOUS_MONTH, GithubData.OK})
_COMMIT_STATES = frozenset({GithubData.OK})
# キャッシュを読めた状態（収集が完了したかの値を持つ）
_CACHE_STATES = frozenset({GithubData.NO_REPOSITORY_LIST, *_PR_STATES})


@enum.unique
class GithubId(enum.StrEnum):
    """GitHub ID の対応表（members-info の `GitHub ID` 列）でのその人の状態。"""

    # login が記入されている
    LOGIN = "login"
    # 「なし」と記入されている（対象の Organization で開発しない人）
    NONE = "none"
    # 未記入か、対応表に行が無い
    UNSET = "unset"


def _check_flag(name: str, value: object) -> None:
    """真偽値か（型の誤りは TypeError）。"""
    if not isinstance(value, bool):
        raise TypeError(f"{name} には真偽値が必要です: {type(value).__name__}")


def _check_count(name: str, value: object) -> None:
    """0以上の整数か（真偽値と整数でない値は TypeError、負の値は ValueError）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} には整数が必要です: {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} には0以上の整数が必要です: {value!r}")


def _check_optional_number(name: str, value: object) -> None:
    """None か有限な数か（型の誤りは TypeError、NaN・inf は ValueError）。

    空欄は None で表す。NaN を空欄の代わりに受けると、値オブジェクトの比較（==）で同じ行が
    等しくならず、欠損の表し方が2通りになるため。
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} には数か None が必要です: {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"{name} には有限な数か None が必要です: {value!r}")


def _check_optional_count(name: str, value: object) -> None:
    """None か0以上の整数か。"""
    if value is not None:
        _check_count(name, value)


def github_data_state(
    *,
    configured: bool,
    has_cache: bool,
    has_repository_list: bool,
    has_commits: bool,
    previous_readable: bool,
    lookback_months: int,
) -> GithubData:
    """その月の GitHub の材料の状態を、設計書 §27.8 の表の順で決める。

    configured は GitHub 分析を有効にした組織か、has_cache はその月のキャッシュがあるか、
    has_repository_list はキャッシュが repository の一覧を持つか、has_commits はキャッシュが
    コミットを持つか（`github_metrics.has_commits`）、previous_readable は直前の月（暦で1つ前）
    のキャッシュがあってコミットを持つか、lookback_months は設定の
    `github.commit_lookback_months`。前の条件が成り立たない材料は読まれないので、後ろの
    引数はその場合に何を渡しても結果を変えない。

    直前の月が読めない月はコミットの列を空欄にする（月をまたぐ重複を除けず、行数が多く出る
    ため）。lookback_months が0のときは重複を除かない設定なので、この扱いをしない。
    直前の月の収集が完了していないことは「読めた」側に入る（github-summary と同じく重複の
    除外に使える）。2か月前より前の欠けも状態には入れない。

    対象月の収集が完了しているかは状態に入れない（ほかの状態と同時に起きたときに、値が
    部分的であることが読めなくなるため）。真偽値でない引数は TypeError、負の月数は
    ValueError。
    """
    for name, value in (
        ("configured", configured),
        ("has_cache", has_cache),
        ("has_repository_list", has_repository_list),
        ("has_commits", has_commits),
        ("previous_readable", previous_readable),
    ):
        _check_flag(name, value)
    _check_count("lookback_months", lookback_months)
    if not configured:
        return GithubData.NOT_CONFIGURED
    if not has_cache:
        return GithubData.NO_CACHE
    if not has_repository_list:
        return GithubData.NO_REPOSITORY_LIST
    if not has_commits:
        return GithubData.NO_COMMITS
    if lookback_months > 0 and not previous_readable:
        return GithubData.NO_PREVIOUS_MONTH
    return GithubData.OK


@dataclass(frozen=True)
class GithubMonth:
    """その月の GitHub の材料（状態と、その状態で表へ写してよい集計）。

    state は `github_data_state` の結果。members は GitHub ID の対応表で、GitHub 分析の無い
    組織だけ None（キャッシュが無い月も対応表は読むので、github_id の列は状態によらず
    書ける）。metrics は PR の集計で、PR の列を書く状態（no_commits・no_previous_month・ok）
    でだけ持つ。commits はコミットの集計で、ok でだけ持つ。no_previous_month の月に
    github-summary が出すコミットの値は、ここには持たせない（時系列に並べると、その月だけ
    行数が多く見えるため）。

    cache_complete はその月のキャッシュの収集が完了したか（`PrCache.complete`）。
    キャッシュを読めた状態（no_repository_list 以降）でだけ真偽値を持ち、キャッシュが無い月と
    GitHub 分析の無い組織は None。PR の集計を持つときは、その集計の `cache_complete` と一致
    する。

    prs は対象者の PR ごとの値（`github_metrics.subject_prs`）で、チーム × 月の表の PR の
    大きさと lead time の材料。PR の集計を持つ状態でだけ持て、それ以外は None。人ごとの
    件数は metrics の users の `merged_pr_count` と一致しなければならない（別の集計の値を
    混ぜない）。人 × 月の行は prs を使わないので省いて（None で）作れる。チーム × 月の表の
    材料（`MonthInputs`）は、PR の集計を持つ状態で prs が None なら作れない。

    状態と持てる集計の組み合わせは __post_init__ で検査する。状態と食い違う集計を持てると、
    書かない約束の列に値が載るため。型の誤りは TypeError、組み合わせの誤りは ValueError。
    """

    state: GithubData
    members: GithubMembers | None
    metrics: GithubMetrics | None
    commits: CommitMetrics | None
    cache_complete: bool | None
    prs: tuple[SubjectPr, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, GithubData):
            raise TypeError(f"state には GithubData が必要です: {type(self.state).__name__}")
        if self.members is not None and not isinstance(self.members, GithubMembers):
            raise TypeError(
                f"members には GithubMembers か None が必要です: {type(self.members).__name__}"
            )
        if self.metrics is not None and not isinstance(self.metrics, GithubMetrics):
            raise TypeError(
                f"metrics には GithubMetrics か None が必要です: {type(self.metrics).__name__}"
            )
        if self.commits is not None and not isinstance(self.commits, CommitMetrics):
            raise TypeError(
                f"commits には CommitMetrics か None が必要です: {type(self.commits).__name__}"
            )
        if self.cache_complete is not None:
            _check_flag("cache_complete", self.cache_complete)
        if self.prs is not None and (
            not isinstance(self.prs, tuple)
            or not all(isinstance(pr, SubjectPr) for pr in self.prs)
        ):
            raise TypeError("prs には SubjectPr の tuple か None が必要です")

        configured = self.state is not GithubData.NOT_CONFIGURED
        if (self.members is not None) != configured:
            raise ValueError(
                "members は GitHub 分析を有効にした組織でだけ持たせてください: "
                f"{self.state.value}"
            )
        if (self.cache_complete is not None) != (self.state in _CACHE_STATES):
            raise ValueError(
                "cache_complete はキャッシュを読めた状態でだけ持たせてください: "
                f"{self.state.value} / {self.cache_complete!r}"
            )
        if (self.metrics is not None) != (self.state in _PR_STATES):
            raise ValueError(
                f"PR の集計を持てるのは PR の列を書く状態だけです: {self.state.value}"
            )
        if (self.commits is not None) != (self.state in _COMMIT_STATES):
            raise ValueError(
                f"コミットの集計を持てるのは ok の状態だけです: {self.state.value}"
            )
        if self.metrics is not None and self.metrics.cache_complete != self.cache_complete:
            raise ValueError("cache_complete が PR の集計と一致しません")
        # github-summary と同じ条件。別の集計の値を同じ行に混ぜない
        if self.commits is not None and self.metrics is not None and (
            self.commits.github_org != self.metrics.github_org
            or self.commits.month != self.metrics.month
            or tuple(user.email for user in self.commits.users)
            != tuple(user.email for user in self.metrics.users)
        ):
            raise ValueError("PR とコミットの集計の Organization・月・対象者が一致しません")
        if self.prs is not None:
            if self.metrics is None:
                raise ValueError(
                    f"PR ごとの値を持てるのは PR の列を書く状態だけです: {self.state.value}"
                )
            counts = Counter(pr.email for pr in self.prs)
            expected = {
                user.email: user.merged_pr_count
                for user in self.metrics.users if user.merged_pr_count
            }
            if dict(counts) != expected:
                raise ValueError("PR ごとの値の人ごとの件数が PR の集計と一致しません")


# GitHub 分析を有効にしていない組織の材料（どの月も同じ）
GITHUB_NOT_CONFIGURED = GithubMonth(
    state=GithubData.NOT_CONFIGURED, members=None, metrics=None, commits=None,
    cache_complete=None,
)


@dataclass(frozen=True)
class PersonMonth:
    """人 × 月の表の1行（1人の1か月）。

    email は分析結果の email（小文字）、month は対象月（YYYY-MM）。seat はその月のシート
    種別を、主の workspace を先頭に `SEAT_SEPARATOR` でつないだもの（行になるのはシートを
    持つ人だけなので、standard / premium のどちらかだけでできている）。department・team・
    role はその月の分析が採用した members-info の値で、欠損は空文字。

    spend_complete は分析した全 workspace の spend がその月の全月データか。
    total_demand_usd・code_demand_usd・code_requests は全 product の需要・Claude Code の
    需要とリクエスト数（アカウントの和）、loc_with_cc は Claude Code が加えた行（アカウントの
    和）。確定できない値と、LoC の列を持つファイルがどの workspace にも無い月の LoC は None。

    github_id は対応表でのその人の状態で、GitHub 分析の無い組織は None。github_data は
    その月の材料の状態、github_cache_complete はその月のキャッシュの収集が完了したか
    （キャッシュが無い月と GitHub 分析の無い組織は None）。

    GitHub の値の列は、github_id が login の人だけが、状態が書くと決めた列（PR の列は
    no_commits・no_previous_month・ok、コミットの列は ok）でだけ値を持ち、それ以外は None。
    PR が0件の人の lead time は None で、1件以上なら値を持つ。

    比率（`cost_per_merged_line_usd`）は保存せず、分子と分母から出す（元の値を失わない
    ため）。不変条件は __post_init__ で検査する。型の誤りは TypeError、値の誤りは
    ValueError。空欄は NaN ではなく None で表すので、行どうしは == で比べられる。
    """

    email: str
    month: str
    seat: str
    department: str
    team: str
    role: str
    spend_complete: bool
    total_demand_usd: float | None
    code_demand_usd: float | None
    code_requests: float | None
    loc_with_cc: int | None
    github_id: GithubId | None
    github_data: GithubData
    github_cache_complete: bool | None
    merged_pr_count: int | None
    lead_time_median_hours: float | None
    commit_count: int | None
    lines_added_capped: int | None
    large_commits: int | None

    def __post_init__(self) -> None:
        for name in ("email", "month", "seat", *_AFFILIATION_FIELDS):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} には文字列が必要です: {type(value).__name__}")
        if not self.email:
            raise ValueError("email は必須です")
        month_windows(self.month)  # 形式の検査は PR キャッシュと同じものへ委ねる
        if not self.seat or any(
            part not in ingest.ASSIGNED_SEAT_TYPES
            for part in self.seat.split(SEAT_SEPARATOR)
        ):
            raise ValueError(f"seat にはシートの種別が必要です: {self.seat!r}")
        _check_flag("spend_complete", self.spend_complete)
        for name in (*_FEATURE_FIELDS, "lead_time_median_hours"):
            _check_optional_number(name, getattr(self, name))
        for name in ("loc_with_cc", "merged_pr_count", *_COMMIT_FIELDS):
            _check_optional_count(name, getattr(self, name))
        if self.lead_time_median_hours is not None and self.lead_time_median_hours < 0:
            raise ValueError(
                f"lead_time_median_hours には0以上の時間が必要です: {self.lead_time_median_hours!r}"
            )
        self._check_github()

    def _check_github(self) -> None:
        """GitHub の列が、状態と対応表の状態から決まる顔ぶれに収まっているか。"""
        if not isinstance(self.github_data, GithubData):
            raise TypeError(
                f"github_data には GithubData が必要です: {type(self.github_data).__name__}"
            )
        if self.github_id is not None and not isinstance(self.github_id, GithubId):
            raise TypeError(
                f"github_id には GithubId か None が必要です: {type(self.github_id).__name__}"
            )
        if self.github_cache_complete is not None:
            _check_flag("github_cache_complete", self.github_cache_complete)
        configured = self.github_data is not GithubData.NOT_CONFIGURED
        if (self.github_id is not None) != configured:
            raise ValueError(
                "github_id は GitHub 分析を有効にした組織でだけ持たせてください: "
                f"{self.github_data.value}"
            )
        if (self.github_cache_complete is not None) != (self.github_data in _CACHE_STATES):
            raise ValueError(
                "github_cache_complete はキャッシュを読めた状態でだけ持たせてください: "
                f"{self.github_data.value}"
            )
        login = self.github_id is GithubId.LOGIN
        for fields, states in ((_PR_FIELDS, _PR_STATES), (_COMMIT_FIELDS, _COMMIT_STATES)):
            expected = login and self.github_data in states
            for name in fields:
                if name == "lead_time_median_hours":
                    continue  # PR の件数で決まる（下で見る）
                if (getattr(self, name) is not None) != expected:
                    raise ValueError(
                        f"{name} は login の人で、その列を書く状態のときだけ値を持ちます: "
                        f"{self.github_id!r} / {self.github_data.value}"
                    )
        has_prs = self.merged_pr_count is not None and self.merged_pr_count > 0
        if (self.lead_time_median_hours is not None) != has_prs:
            raise ValueError(
                "lead_time_median_hours は PR が1件以上ある人だけが持ちます: "
                f"{self.merged_pr_count!r} 件 / {self.lead_time_median_hours!r}"
            )

    @property
    def cost_per_merged_line_usd(self) -> float | None:
        """Claude Code の需要 ÷ 上限つきのマージ行（分母が0・どちらかが None なら None）。"""
        if self.code_demand_usd is None or not self.lines_added_capped:
            return None
        return self.code_demand_usd / self.lines_added_capped


def _workspace_order(result: OrgAnalysisResult) -> tuple[str, ...]:
    """主の workspace を先頭に、残りを result.workspaces の順で並べた名前。"""
    names = list(result.workspaces)
    if result.primary in result.workspaces:
        names.remove(result.primary)
        names.insert(0, result.primary)
    return tuple(names)


def seat_holders(result: OrgAnalysisResult) -> dict[str, tuple[str, ...]]:
    """その月にこの組織でシートを持つ人の email → シート種別の並び（email の昇順）。

    シートを持つ人は、分析した workspace のどれかで `current_seat` が
    `ingest.ASSIGNED_SEAT_TYPES` の人（GitHub の参考値の対象者と同じ定義）。分析から飛ばした
    workspace は見ない。未割当・シート不明（members に居ない人を含む）は入れない。

    シート種別の並びは、主の workspace を先頭に、残りは `result.workspaces` の順で、
    シートを持つ workspace の分だけ並べる。
    """
    if not isinstance(result, OrgAnalysisResult):
        raise TypeError(
            f"result には OrgAnalysisResult が必要です: {type(result).__name__}"
        )
    seats: dict[str, list[str]] = {}
    for name in _workspace_order(result):
        users = result.workspaces[name].users
        held = users.loc[users["current_seat"].isin(ingest.ASSIGNED_SEAT_TYPES)]
        for email, seat in zip(held["email"], held["current_seat"], strict=True):
            seats.setdefault(str(email), []).append(str(seat))
    return {email: tuple(seats[email]) for email in sorted(seats)}


def _feature_sum(
    workspaces: list[AnalysisResult], email: str, name: str,
) -> float | None:
    """各 workspace の product 利用特徴量の、その人の値の和（確定できない値があれば None）。

    行の無い workspace は 0（spend に現れない＝利用なしの観測）。
    """
    total = 0.0
    for workspace in workspaces:
        usage = workspace.product_usage
        if usage is None:
            raise ValueError("分析結果に product 利用特徴量がありません")
        features = usage.features
        if email not in features.index:
            continue
        value = features.at[email, name]
        if pd.isna(value):
            return None
        total += float(value)
    return total


def _loc_sum(workspaces: list[AnalysisResult], email: str) -> int | None:
    """各 workspace の users の loc_with_cc の和（列を持つ workspace が無ければ None）。

    列がある workspace で行の無い人は 0（report の LoC 列と同じ値）。
    """
    with_column = [ws.users for ws in workspaces if _LOC_COLUMN in ws.users.columns]
    if not with_column:
        return None
    total = 0
    for users in with_column:
        values = users.loc[users["email"] == email, _LOC_COLUMN]
        total += sum(int(value) for value in values)
    return total


def _affiliation(workspaces: list[AnalysisResult], email: str) -> dict[str, str]:
    """所属の3列（主の workspace の行を優先し、無ければ最初に見つかった行）。"""
    for workspace in workspaces:
        rows = workspace.users.loc[workspace.users["email"] == email]
        if rows.empty:
            continue
        row = rows.iloc[0]
        return {
            name: "" if name not in row.index or pd.isna(row[name]) else str(row[name])
            for name in _AFFILIATION_FIELDS
        }
    return dict.fromkeys(_AFFILIATION_FIELDS, "")


def _member_key(email: str) -> str:
    """email を対応表の鍵と同じ表記へ揃える（規則の写しをここに置かず、対応表の1行へ委ねる）。"""
    return GithubMemberLink(email=email, github_login=None).email


def _github_id(members: GithubMembers | None, email: str) -> GithubId | None:
    """対応表でのその人の状態（GitHub 分析の無い組織は None）。"""
    if members is None:
        return None
    key = _member_key(email)
    for entry in members.entries:
        if entry.email != key:
            continue
        if entry.github_login is not None:
            return GithubId.LOGIN
        return GithubId.NONE if entry.no_account else GithubId.UNSET
    return GithubId.UNSET


def _github_values(
    github: GithubMonth, email: str, github_id: GithubId | None,
) -> dict[str, int | float | None]:
    """PR とコミットの列（login の人で、状態が書くと決めた列だけが値を持つ）。"""
    values: dict[str, int | float | None] = dict.fromkeys((*_PR_FIELDS, *_COMMIT_FIELDS))
    if github_id is not GithubId.LOGIN:
        return values
    key = _member_key(email)
    if github.metrics is not None:
        user = next((u for u in github.metrics.users if u.email == key), None)
        if user is None:
            raise ValueError(f"PR の集計に login のある対象者の行がありません: {email}")
        values["merged_pr_count"] = user.merged_pr_count
        values["lead_time_median_hours"] = (
            None if user.lead_time is None else user.lead_time.median_hours
        )
    if github.commits is not None:
        totals = next((u.totals for u in github.commits.users if u.email == key), None)
        if totals is None:
            raise ValueError(f"コミットの集計に login のある対象者の行がありません: {email}")
        for name in _COMMIT_FIELDS:
            values[name] = getattr(totals, name)
    return values


def _spend_complete(result: OrgAnalysisResult) -> bool:
    """分析した全 workspace の spend が、その月の全月データか。"""
    return all(
        workspace.complete.get(result.month) is True
        for workspace in result.workspaces.values()
    )


def person_rows(
    result: OrgAnalysisResult, github: GithubMonth,
) -> tuple[PersonMonth, ...]:
    """1 か月分の人 × 月の行（シートを持つ人だけ・email の昇順）。

    result はその月を対象月として分析した結果、github はその月の GitHub の材料で、PR と
    コミットの集計は同じ月の同じ対象者（`seat_holders` の email）で作ったもの。集計の月が
    result の月と違えば ValueError。型の誤りは TypeError。
    """
    if not isinstance(result, OrgAnalysisResult):
        raise TypeError(
            f"result には OrgAnalysisResult が必要です: {type(result).__name__}"
        )
    if not isinstance(github, GithubMonth):
        raise TypeError(f"github には GithubMonth が必要です: {type(github).__name__}")
    for metrics in (github.metrics, github.commits):
        if metrics is not None and metrics.month != result.month:
            raise ValueError(
                f"GitHub の集計の月が分析の月と違います: {metrics.month} / {result.month}"
            )
    holders = seat_holders(result)
    workspaces = [result.workspaces[name] for name in _workspace_order(result)]
    spend_complete = _spend_complete(result)
    rows = []
    for email, seats in holders.items():
        github_id = _github_id(github.members, email)
        rows.append(PersonMonth(
            email=email,
            month=result.month,
            seat=SEAT_SEPARATOR.join(seats),
            **_affiliation(workspaces, email),
            spend_complete=spend_complete,
            **{name: _feature_sum(workspaces, email, name) for name in _FEATURE_FIELDS},
            loc_with_cc=_loc_sum(workspaces, email),
            github_id=github_id,
            github_data=github.state,
            github_cache_complete=github.cache_complete,
            **_github_values(github, email, github_id),
        ))
    return tuple(rows)


# ------------------------------------------------------------ 集まり × 月


# 定着を見る連続の月数（その月を含む、暦で連続する月の数。設計書 §27.5）
_STREAK_MONTHS = 3


@enum.unique
class GroupScope(enum.StrEnum):
    """集まりの種別（`scope` の列の値）。宣言順がチーム × 月の表の並びの順。"""

    # 組織全体（その月にシートを持つ人の全員。名前は空文字）
    ORGANIZATION = "organization"
    # 部署（members-info の部署の列）
    DEPARTMENT = "department"
    # チーム（members-info のチームの列）
    TEAM = "team"


# 種別 → 並びの位置（宣言順）
_SCOPE_ORDER = {scope: index for index, scope in enumerate(GroupScope)}

# 部署・チームの種別と、人 × 月の行の所属の列
_AFFILIATION_SCOPES = ((GroupScope.DEPARTMENT, "department"), (GroupScope.TEAM, "team"))


@dataclass(frozen=True)
class MonthInputs:
    """チーム × 月の表の1か月分の材料（その月の値と、人 × 月の行と、PR ごとの値）。

    month は対象月（YYYY-MM）。spend_complete・github_data・github_cache_complete はその月の
    値で、人 × 月の行の同じ列と同じ意味。rows はその月の人 × 月の行（`person_rows` の結果。
    email の昇順で重複しない）。prs は対象者の PR ごとの値（`GithubMonth.prs`）で、PR の列を
    書く状態（no_commits・no_previous_month・ok）でだけ持ち、それ以外は None。

    月の値を行とは別に持つのは、シートを持つ人が0人の月でも組織全体の行を作るため（行から
    月の値を読むと、行の無い月の値が決まらない）。

    不変条件は __post_init__ で検査する: 行の month・spend_complete・github_data・
    github_cache_complete が月の値と一致する／行は email の昇順で重複しない／PR ごとの値の
    email は、その月の行のうち github_id が login の人のもので、人ごとの件数がその人の
    merged_pr_count と一致する。行と PR ごとの値が別の月・別の集計から来ると、集まりの
    PR の値が人ごとの件数と食い違うため。型の誤りは TypeError、値の誤りは ValueError。
    """

    month: str
    spend_complete: bool
    github_data: GithubData
    github_cache_complete: bool | None
    rows: tuple[PersonMonth, ...]
    prs: tuple[SubjectPr, ...] | None

    def __post_init__(self) -> None:
        if not isinstance(self.month, str):
            raise TypeError(f"month には文字列が必要です: {type(self.month).__name__}")
        month_windows(self.month)
        _check_flag("spend_complete", self.spend_complete)
        if not isinstance(self.github_data, GithubData):
            raise TypeError(
                f"github_data には GithubData が必要です: {type(self.github_data).__name__}"
            )
        if self.github_cache_complete is not None:
            _check_flag("github_cache_complete", self.github_cache_complete)
        if not isinstance(self.rows, tuple) or not all(
            isinstance(row, PersonMonth) for row in self.rows
        ):
            raise TypeError("rows には PersonMonth の tuple が必要です")
        if self.prs is not None and (
            not isinstance(self.prs, tuple)
            or not all(isinstance(pr, SubjectPr) for pr in self.prs)
        ):
            raise TypeError("prs には SubjectPr の tuple か None が必要です")

        if (self.github_cache_complete is not None) != (self.github_data in _CACHE_STATES):
            raise ValueError(
                "github_cache_complete はキャッシュを読めた状態でだけ持たせてください: "
                f"{self.github_data.value}"
            )
        for row in self.rows:
            if (
                row.month != self.month
                or row.spend_complete != self.spend_complete
                or row.github_data is not self.github_data
                or row.github_cache_complete != self.github_cache_complete
            ):
                raise ValueError(f"行の月の値が材料の月の値と一致しません: {row.email}")
        emails = [row.email for row in self.rows]
        if emails != sorted(set(emails)):
            raise ValueError("行は email の昇順で、重複させないでください")
        if (self.prs is not None) != (self.github_data in _PR_STATES):
            raise ValueError(
                f"PR ごとの値は PR の列を書く状態でだけ持たせてください: {self.github_data.value}"
            )
        if self.prs is not None:
            expected = {
                _member_key(row.email): row.merged_pr_count
                for row in self.rows
                if row.github_id is GithubId.LOGIN and row.merged_pr_count
            }
            if dict(Counter(pr.email for pr in self.prs)) != expected:
                raise ValueError(
                    "PR ごとの値の人ごとの件数が、login の人の merged_pr_count と一致しません"
                )


def month_inputs(result: OrgAnalysisResult, github: GithubMonth) -> MonthInputs:
    """1か月分の材料を作る（人 × 月の行は `person_rows` で作る）。

    result はその月を対象月として分析した結果、github はその月の GitHub の材料で、PR の
    集計を持つ状態では PR ごとの値（`GithubMonth.prs`）も持つもの。型の誤りは TypeError、
    PR ごとの値が欠けているなど材料の不整合は ValueError。
    """
    rows = person_rows(result, github)  # 型の検査もここで行う
    return MonthInputs(
        month=result.month,
        spend_complete=_spend_complete(result),
        github_data=github.state,
        github_cache_complete=github.cache_complete,
        rows=rows,
        prs=github.prs,
    )


def _check_optional_nonnegative(name: str, value: object) -> None:
    """None か0以上の有限な数か（行数・時間の中央値）。"""
    _check_optional_number(name, value)
    if value is not None and value < 0:
        raise ValueError(f"{name} には0以上の値が必要です: {value!r}")


@dataclass(frozen=True)
class GroupMonth:
    """チーム × 月の表の1行（1つの集まりの1か月）。

    scope は集まりの種別、name は部署・チームの名前（組織全体は空文字）、month は対象月。
    spend_complete・github_data・github_cache_complete はその月の値（人 × 月の表と同じ）。

    seat_holders は集まりの人数（その月にシートを持つ人）、code_users はその月に Claude
    Code のリクエストがあった人の数、code_users_3m はその月を含む暦で連続する3か月とも
    使った人の数（その月の集まりの人で数える）。code_demand_usd は Claude Code の需要の
    合計、code_demand_median_usd は集まりの全員（0 の人を含む）の需要の中央値、
    loc_median_of_users は使った人の Claude Code が加えた行の中央値。

    github_people は GitHub ID が login の人の数（GitHub 分析の無い組織は None）。
    merged_pr_count は login の人の PR の件数の和、pr_additions_median は集まりの login の
    人が作った PR の追加行の中央値、lead_time_median_hours・lead_time_p90_hours は同じ PR の
    集合の lead time の中央値と P90。lines_added_capped は login の人の上限つきの追加行の和、
    code_demand_usd_of_github_people は login の人の需要の和（行数が0の人も含める）。

    合計と中央値は丸める前の値から出す。材料の人の値が1人でも確定できなければ、その列は
    None（分からない値を0として数えない）。GitHub の値の列は、PR の列（件数・大きさ・
    lead time）が PR の列を書く状態、行数と需要の和が ok の状態でだけ値を持ち、login の
    人が0人の集まりではすべて None（対象の人がいないことと、成果が0件だったことを区別する
    ため）。PR の大きさと lead time は PR が1件以上のときだけ値を持つ。

    比率（`cost_per_merged_line_usd`）は保存せず、分子と分母から出す（元の値を失わない
    ため）。不変条件は __post_init__ で検査する。型の誤りは TypeError、値の誤りは
    ValueError。空欄は NaN ではなく None で表すので、行どうしは == で比べられる。
    """

    scope: GroupScope
    name: str
    month: str
    spend_complete: bool
    github_data: GithubData
    github_cache_complete: bool | None
    seat_holders: int
    code_users: int | None
    code_users_3m: int | None
    code_demand_usd: float | None
    code_demand_median_usd: float | None
    loc_median_of_users: float | None
    github_people: int | None
    merged_pr_count: int | None
    pr_additions_median: float | None
    lead_time_median_hours: float | None
    lead_time_p90_hours: float | None
    lines_added_capped: int | None
    code_demand_usd_of_github_people: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, GroupScope):
            raise TypeError(f"scope には GroupScope が必要です: {type(self.scope).__name__}")
        for name in ("name", "month"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(
                    f"{name} には文字列が必要です: {type(getattr(self, name)).__name__}"
                )
        month_windows(self.month)
        # 組織全体は名前を持たず、部署・チームは必ず名前を持つ（行の鍵が種別と名前のため）
        if (self.name == "") != (self.scope is GroupScope.ORGANIZATION):
            raise ValueError(
                f"名前が空なのは組織全体の行だけです: {self.scope.value} / {self.name!r}"
            )
        _check_flag("spend_complete", self.spend_complete)
        if not isinstance(self.github_data, GithubData):
            raise TypeError(
                f"github_data には GithubData が必要です: {type(self.github_data).__name__}"
            )
        if self.github_cache_complete is not None:
            _check_flag("github_cache_complete", self.github_cache_complete)
        _check_count("seat_holders", self.seat_holders)
        for name in ("code_users", "code_users_3m", "github_people", "merged_pr_count",
                     "lines_added_capped"):
            _check_optional_count(name, getattr(self, name))
        for name in ("code_demand_usd", "code_demand_median_usd",
                     "code_demand_usd_of_github_people"):
            _check_optional_number(name, getattr(self, name))
        for name in ("loc_median_of_users", "pr_additions_median", "lead_time_median_hours",
                     "lead_time_p90_hours"):
            _check_optional_nonnegative(name, getattr(self, name))
        if (self.github_cache_complete is not None) != (self.github_data in _CACHE_STATES):
            raise ValueError(
                "github_cache_complete はキャッシュを読めた状態でだけ持たせてください: "
                f"{self.github_data.value}"
            )
        self._check_people()
        self._check_github()

    def _check_people(self) -> None:
        """人数の大小関係と、人数で決まる空欄。"""
        # 部署・チームの行は、その月に1人以上いる集まりだけ作る
        if self.scope is not GroupScope.ORGANIZATION and self.seat_holders < 1:
            raise ValueError(f"部署・チームの行には1人以上が必要です: {self.name!r}")
        if self.code_users is not None and self.code_users > self.seat_holders:
            raise ValueError(
                f"code_users が seat_holders を超えています: "
                f"{self.code_users} / {self.seat_holders}"
            )
        if self.code_users_3m is not None and (
            self.code_users is None or self.code_users_3m > self.code_users
        ):
            raise ValueError(
                "code_users_3m は code_users 以下の値で、code_users が空欄なら空欄です: "
                f"{self.code_users_3m} / {self.code_users!r}"
            )
        if self.seat_holders == 0 and self.code_demand_median_usd is not None:
            raise ValueError("シートを持つ人が0人の集まりは需要の中央値を持ちません")
        if self.loc_median_of_users is not None and not self.code_users:
            raise ValueError("使った人が0人・空欄の集まりは加えた行の中央値を持ちません")

    def _check_github(self) -> None:
        """GitHub の列が、状態と github_people から決まる顔ぶれに収まっているか。"""
        configured = self.github_data is not GithubData.NOT_CONFIGURED
        if (self.github_people is not None) != configured:
            raise ValueError(
                "github_people は GitHub 分析を有効にした組織でだけ持たせてください: "
                f"{self.github_data.value}"
            )
        if self.github_people is not None and self.github_people > self.seat_holders:
            raise ValueError(
                f"github_people が seat_holders を超えています: "
                f"{self.github_people} / {self.seat_holders}"
            )
        has_people = bool(self.github_people)
        pr_expected = has_people and self.github_data in _PR_STATES
        if (self.merged_pr_count is not None) != pr_expected:
            raise ValueError(
                "merged_pr_count は login の人がいて PR の列を書く状態のときだけ値を持ちます: "
                f"{self.github_people!r} 人 / {self.github_data.value}"
            )
        commit_expected = has_people and self.github_data in _COMMIT_STATES
        if (self.lines_added_capped is not None) != commit_expected:
            raise ValueError(
                "lines_added_capped は login の人がいて ok の状態のときだけ値を持ちます: "
                f"{self.github_people!r} 人 / {self.github_data.value}"
            )
        # 需要の和は、login の誰かの需要が空欄なら空欄になりうる（逆向きだけを見る）
        if self.code_demand_usd_of_github_people is not None and not commit_expected:
            raise ValueError(
                "code_demand_usd_of_github_people は login の人がいて ok の状態のときだけ"
                f"値を持ちます: {self.github_people!r} 人 / {self.github_data.value}"
            )
        has_prs = self.merged_pr_count is not None and self.merged_pr_count > 0
        for name in ("pr_additions_median", "lead_time_median_hours", "lead_time_p90_hours"):
            if (getattr(self, name) is not None) != has_prs:
                raise ValueError(
                    f"{name} は PR が1件以上ある集まりだけが持ちます: "
                    f"{self.merged_pr_count!r} 件 / {getattr(self, name)!r}"
                )
        if has_prs and self.lead_time_median_hours > self.lead_time_p90_hours:
            raise ValueError(
                "lead time は中央値 ≤ P90 にしてください: "
                f"{self.lead_time_median_hours} / {self.lead_time_p90_hours}"
            )

    @property
    def cost_per_merged_line_usd(self) -> float | None:
        """login の人の需要の和 ÷ 上限つきのマージ行の和（分母が0・どちらかが None なら None）。"""
        if self.code_demand_usd_of_github_people is None or not self.lines_added_capped:
            return None
        return self.code_demand_usd_of_github_people / self.lines_added_capped


def group_order(row: GroupMonth) -> tuple[int, str, str]:
    """チーム × 月の表の並びの鍵（種別の宣言順・名前・月の昇順）。"""
    return (_SCOPE_ORDER[row.scope], row.name, row.month)


def _groups(
    rows: tuple[PersonMonth, ...],
) -> list[tuple[GroupScope, str, tuple[PersonMonth, ...]]]:
    """その月の集まりと、そこに入る人の行（組織全体・部署・チームの順、名前の昇順）。

    組織全体は行の全員（0人でも作る）。部署・チームはセルを `ingest.parse_affiliations` で
    分けた名前ごとで、兼務の人はそれぞれの集まりに1人として入る（按分しない）。同じ名前が
    1つのセルに2度書かれていても1度だけ入れる。未記入の人は組織全体にだけ入る。
    """
    groups: list[tuple[GroupScope, str, tuple[PersonMonth, ...]]] = [
        (GroupScope.ORGANIZATION, "", rows)
    ]
    for scope, column in _AFFILIATION_SCOPES:
        members: dict[str, list[PersonMonth]] = {}
        for row in rows:
            for name in dict.fromkeys(ingest.parse_affiliations(getattr(row, column))):
                members.setdefault(name, []).append(row)
        groups += [(scope, name, tuple(members[name])) for name in sorted(members)]
    return groups


def _sum_or_none(values: list[float | None]) -> float | None:
    """値の和（1つでも None なら None。0件なら 0.0）。"""
    if any(value is None for value in values):
        return None
    return float(sum(values))


def _median_or_none(values: list[float | int | None]) -> float | None:
    """値の中央値（1つでも None か、0件なら None）。"""
    if not values or any(value is None for value in values):
        return None
    return float(statistics.median(values))


def _code_users_3m(
    month: str,
    people: tuple[PersonMonth, ...],
    by_month: Mapping[str, Mapping[str, PersonMonth]],
) -> int | None:
    """その月を含む暦で連続する3か月とも Claude Code を使った人の数。

    数えるのはその月の集まりの人（前の月の所属は問わない）。前の月に行が無い人（その月に
    シートを持たない）は数えない。3か月のどれかが材料に無いか、集まりの人の行がある月の
    code_requests が None なら None。
    """
    window = [month]
    for _ in range(_STREAK_MONTHS - 1):
        window.append(previous_month(window[-1]))
    if any(each not in by_month for each in window):
        return None
    count = 0
    for person in people:
        used = True
        for each in window:
            row = by_month[each].get(person.email)
            if row is None:
                used = False
                continue
            if row.code_requests is None:
                return None
            if row.code_requests <= 0:
                used = False
        count += used
    return count


def _group_month(
    scope: GroupScope,
    name: str,
    material: MonthInputs,
    people: tuple[PersonMonth, ...],
    by_month: Mapping[str, Mapping[str, PersonMonth]],
) -> GroupMonth:
    """1つの集まりの1か月の行（列ごとの出し方は設計書 §27.5）。"""
    requests = [person.code_requests for person in people]
    code_users = (
        None if any(value is None for value in requests)
        else sum(1 for value in requests if value > 0)
    )
    users = [person for person in people if person.code_requests]
    loc_median = (
        None if code_users is None else _median_or_none([u.loc_with_cc for u in users])
    )
    demands = [person.code_demand_usd for person in people]

    state = material.github_data
    linked = [person for person in people if person.github_id is GithubId.LOGIN]
    github_people = None if state is GithubData.NOT_CONFIGURED else len(linked)
    merged_pr_count = pr_additions = lead_median = lead_p90 = None
    lines = demand_of_people = None
    if github_people and state in _PR_STATES:
        merged_pr_count = sum(person.merged_pr_count for person in linked)
        keys = {_member_key(person.email) for person in linked}
        prs = [pr for pr in material.prs if pr.email in keys]
        pr_additions = _median_or_none([pr.additions for pr in prs])
        lead_time = summarize_lead_times(pr.lead_time_hours for pr in prs)
        if lead_time is not None:
            lead_median, lead_p90 = lead_time.median_hours, lead_time.p90_hours
    if github_people and state in _COMMIT_STATES:
        lines = sum(person.lines_added_capped for person in linked)
        demand_of_people = _sum_or_none([person.code_demand_usd for person in linked])

    return GroupMonth(
        scope=scope,
        name=name,
        month=material.month,
        spend_complete=material.spend_complete,
        github_data=state,
        github_cache_complete=material.github_cache_complete,
        seat_holders=len(people),
        code_users=code_users,
        code_users_3m=_code_users_3m(material.month, people, by_month),
        code_demand_usd=_sum_or_none(demands),
        code_demand_median_usd=_median_or_none(demands),
        loc_median_of_users=loc_median,
        github_people=github_people,
        merged_pr_count=merged_pr_count,
        pr_additions_median=pr_additions,
        lead_time_median_hours=lead_median,
        lead_time_p90_hours=lead_p90,
        lines_added_capped=lines,
        code_demand_usd_of_github_people=demand_of_people,
    )


def group_rows(months: Iterable[MonthInputs]) -> tuple[GroupMonth, ...]:
    """全月の材料から、集まり × 月の行を作る（種別の宣言順・名前・月の昇順）。

    months は1か月分の材料の並びで、渡す順には依らない。同じ月が2つあれば ValueError
    （どちらの値が正か決まらないため）、MonthInputs でない要素は TypeError。

    組織全体の行は材料のある月ごとに必ず1行作り（シートを持つ人が0人の月も、飛ばした月と
    区別するため）、部署・チームの行はその月に1人以上いる集まりだけ作る。部署とチームで
    同じ名前があっても、種別が違えば別の行にする。

    lead time と PR の大きさは、人ごとの中央値を平均したり中央値を取り直したりせず、
    その集まりの login の人が作った PR の集合から出す（人ごとの中央値は足し合わせられない
    ため）。前月比などの変化の計算はしない（レポートの仕事）。
    """
    materials = list(months)
    for material in materials:
        if not isinstance(material, MonthInputs):
            raise TypeError(
                f"months には MonthInputs が必要です: {type(material).__name__}"
            )
    by_month: dict[str, MonthInputs] = {}
    for material in materials:
        if material.month in by_month:
            raise ValueError(f"同じ月の材料が複数あります: {material.month}")
        by_month[material.month] = material
    people_by_month = {
        month: {row.email: row for row in material.rows}
        for month, material in by_month.items()
    }
    rows = [
        _group_month(scope, name, by_month[month], people, people_by_month)
        for month in sorted(by_month)
        for scope, name, people in _groups(by_month[month].rows)
    ]
    return tuple(sorted(rows, key=group_order))
