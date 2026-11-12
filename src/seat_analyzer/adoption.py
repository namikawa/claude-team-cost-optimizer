"""Claude Code の活用と GitHub の成果を、人 × 月の行へ並べる組み立て（設計書 §27.8）。

1 か月分の分析結果（`analyze.analyze_org` の戻り）と、その月の GitHub の材料を受けて、
その月にシートを持つ人ごとに1行を作る。月をまたいで並べるのは呼び出し側で、ここは
1 か月分の行だけを返す。各月の値は、その月を対象月として分析した結果から取る。

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
行・同じ並びを返す。並びは email の昇順で明示的に決め、集合の反復順には依らせない。

人ごとの値は参考値で、シートの判定には使わない。個人の順位づけに使うものではないので、
量の多い順には並べない（設計書 §15.6）。
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

import pandas as pd

from . import ingest
from .analyze import AnalysisResult, OrgAnalysisResult
from .github_collect import GithubMemberLink, GithubMembers, month_windows
from .github_metrics import CommitMetrics, GithubMetrics

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

    状態と持てる集計の組み合わせは __post_init__ で検査する。状態と食い違う集計を持てると、
    書かない約束の列に値が載るため。型の誤りは TypeError、組み合わせの誤りは ValueError。
    """

    state: GithubData
    members: GithubMembers | None
    metrics: GithubMetrics | None
    commits: CommitMetrics | None
    cache_complete: bool | None

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


def _github_id(members: GithubMembers | None, email: str) -> GithubId | None:
    """対応表でのその人の状態（GitHub 分析の無い組織は None）。"""
    if members is None:
        return None
    # email の表記は対応表の鍵と同じ規則で揃える（規則の写しをここに置かない）
    key = GithubMemberLink(email=email, github_login=None).email
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
    key = GithubMemberLink(email=email, github_login=None).email
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
    spend_complete = all(
        workspace.complete.get(result.month) is True for workspace in workspaces
    )
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

