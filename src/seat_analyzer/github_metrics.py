"""収集した PR を、ユーザ×月の件数・lead time・コミット行数へ畳む計算（§15.5・§15.7）。

PR 集計が受け取るのは4つの値だけ——その月の PR キャッシュ（`github_collect.load_pr_cache`）、
email → GitHub login の対応表（`load_github_members`）、Organization 内の repository の
一覧（`discover_repositories`）、対象者の email の集合。コミット集計では過去月の
キャッシュと大きな変更の閾値も受け取る。`gh` もネットワークもファイルも現在時刻も
参照しないので、同じ入力からは常に同じ結果と同じ警告の並びを返す。並びは email の
昇順で明示的に決め、集合の反復順には依らせない。

対象者は「その月にこの組織でシートを持つ人」で、誰を対象者にするかは呼び出し側が
決める（定義は設計書 §15.5）。ここは対象者の PR だけを人ごとの行と組織全体の要約に
入れる。対応表に login があっても対象者でない人（シートを持たない人）の PR は、対応表に
無い作成者の PR と同じ区分に入る。

PR が帰属する月は merge した月で、それはキャッシュの形（1ファイル = 1組織 × 1月）が
保証している（`PrCache` は month 以外の月に merge された PR を受けない）。件数の計算は
`created_at` を見ない。lead time は `merged_at − created_at` なので両方を見る。

lead time は時（hours）の実数で、Draft だった期間も含める（設計書 §15.5）。日時は UTC
固定の字句なので UTC のまま解析し、実行環境の timezone には依らせない。要約は median /
P75 / P90 の3点に件数を添えたもので、件数の下限は設けない——1件でも要約し、代表値の
重みは件数で伝える。

1件の PR は必ず1つの区分に入る——個人別の件数・対象者に当たらない作成者・Bot・削除済みの
アカウント・対象外 repository の5つで、先に該当した区分に入れる。区分の合計がキャッシュの
全件数に一致することを値オブジェクトが検査するので、どこかの区分から静かに漏れた PR は
結果を作る時点で落ちる。

Bot が作った PR は個人の実績ではないので個人別の集計から外す（`author_type` が
`BOT_AUTHOR_TYPE` の PR）。対象者に当たらない作成者の PR も外すが、外した分は件数と人数
だけを残し、login は結果のどこにも持たせない。対象者の外にいる人はこの組織のメンバーとは
限らず、その名前はこの組織の資料に載せる対象ではないため、結果にも警告にも写さない
（設計書 §15.6 の「個人ランキングを作らない」とも整合する）。件数を黙って落とさないよう、
外した分は警告に出す。

repository の一覧が完全でない結果では集計しない（`RepoDiscovery.complete`）。部分的な
一覧で照合すると、一覧に載らなかった repository の PR が「対象外」へ流れ、参考指標が
黙って小さく出る。同じ理由で、収集し切れていないキャッシュ（`PrCache.complete` が False）は
集計するが「部分的な値」であることを警告に残す。

行数は対象 repository の PR のコミットを作者の login へ帰属させる（§15.7）。login が
無いときだけ最初に merge された PR の作成者へ戻し、メールは使わない。oid の重複、
マージコミット、PR のマージで新しく作られたコミット、前の月の対象 PR に出た oid は
数えない。Bot・draft・削除済み作成者の PR も材料にする。大きな変更は上限つき追加行の
合計からだけ外し、保存上限を超えたコミットと解決できない作者は件数で警告する。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .github_collect import (
    CachedPr,
    GithubMemberLink,
    GithubMembers,
    PrCache,
    RepoDiscovery,
    is_github_org_name,
    month_windows,
    unmapped_emails,
)

# 個人の実績から外す作成者の種別（GraphQL の `__typename`）。完全一致で見る——種別は
# 収集の側が字句のまま写すので、ここで表記を寄せると別の種別を Bot として畳みかねない
BOT_AUTHOR_TYPE = "Bot"

# `CachedPr` が保証する日時の表記（UTC 固定）
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_SECONDS_PER_HOUR = 3600

# 0以上の整数であることを確かめるフィールド（`GithubMetrics`）。個人別の件数は users の
# 各行が自分で確かめるのでこの表には入れない
_COUNT_FIELDS = (
    "unmapped_authors",
    "unmapped_prs",
    "bot_prs",
    "deleted_author_prs",
    "excluded_repository_prs",
    "total_prs",
)

# 件数・行数として確かめるフィールド（`CommitTotals`）
_COMMIT_TOTAL_FIELDS = (
    "commit_count", "lines_added", "lines_removed", "lines_added_capped", "large_commits",
)

# 時間として確かめるフィールド（`LeadTimeSummary`）
_HOUR_FIELDS = ("median_hours", "p75_hours", "p90_hours")


def _is_count(value: object) -> bool:
    """件数として読める値か（真偽値は int の一種なので除く）。

    規則は `github_collect` の同名の検査と同じ（private なので実装は共有しない）。
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_month(month: object) -> None:
    """対象月が YYYY-MM の形か（形式が外れれば ValueError）。

    検査は PR キャッシュと同じものへ委ねる（`month_windows` が同じ規則で見る）。同じ
    規則の写しを増やすと、キャッシュでは読める月がここで落ちる状態を作りうる。
    """
    month_windows(month)


# ---------------------------------------------------------------- lead time


def _as_hours(name: str, value: object) -> float:
    """時間（hours）として読める値を float にする。

    真偽値と数でない値は TypeError、負の値と NaN / inf は ValueError にする（読めない型と
    読めない値を別の失敗として出す）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{name} には時間（float）が必要です: {type(value).__name__}"
        )
    hours = float(value)
    if not math.isfinite(hours) or hours < 0:
        raise ValueError(f"{name} には0以上の有限な時間が必要です: {value!r}")
    return hours


def _parse_utc(timestamp: str) -> dt.datetime:
    """UTC の日時表記を aware な datetime にする（`CachedPr` を通した値にだけ使う）。"""
    return dt.datetime.strptime(timestamp, _TIMESTAMP_FORMAT).replace(tzinfo=dt.UTC)


def lead_time_hours(pr: CachedPr) -> float:
    """1件の PR の lead time（`merged_at − created_at`）を時（hours）で返す。

    日時は UTC 固定の字句（`CachedPr` の不変条件）なので UTC のまま解析する。実行環境の
    timezone を参照しないので、どの端末で流しても同じ値になる。Draft だった期間も含める
    ため `is_draft` は見ない（設計書 §15.5）。丸めもしない（表示の丸めは出力側の責務）。
    """
    if not isinstance(pr, CachedPr):
        raise TypeError(f"pr には CachedPr が必要です: {type(pr).__name__}")
    delta = _parse_utc(pr.merged_at) - _parse_utc(pr.created_at)
    return delta.total_seconds() / _SECONDS_PER_HOUR


def _percentile(values: Sequence[float], p: float) -> float:
    """昇順に並んだ値の百分位（線形補間・numpy / pandas / Excel と同じ type 7）。

    位置 h = (n−1)·p の前後を線形に補間する。`statistics.quantiles` を使わないのは、
    要素が1件のときに例外になるため（この計算は1件でも要約する）。
    """
    position = (len(values) - 1) * p
    lower = math.floor(position)
    frac = position - lower
    if frac == 0.0 or lower + 1 >= len(values):
        return float(values[lower])
    return float(values[lower] + frac * (values[lower + 1] - values[lower]))


# ------------------------------------------------------------ 値オブジェクト


@dataclass(frozen=True)
class LeadTimeSummary:
    """lead time の要約（median / P75 / P90 と、要約した PR の件数）。

    3つの値は0以上の有限な時間で median ≤ P75 ≤ P90 の順、count は1件以上（1件も無い
    ときは要約そのものを作らず None にする）。件数を値の隣に置くのは、1件でも要約する
    ため——代表値だけでは何件から出た値か分からない。
    """

    count: int
    median_hours: float
    p75_hours: float
    p90_hours: float

    def __post_init__(self) -> None:
        if not _is_count(self.count) or self.count < 1:
            raise ValueError(f"count には1以上の件数が必要です: {self.count!r}")
        for name in _HOUR_FIELDS:
            object.__setattr__(self, name, _as_hours(name, getattr(self, name)))
        if not self.median_hours <= self.p75_hours <= self.p90_hours:
            raise ValueError(
                "百分位は median ≤ P75 ≤ P90 の順にしてください: "
                f"{self.median_hours} / {self.p75_hours} / {self.p90_hours}"
            )


@dataclass(frozen=True)
class UserPrMetrics:
    """1人分の merged PR 数と lead time の要約。

    email は対応表の鍵（前後空白を除いて小文字へ揃えた表記）、github_login は対応表に
    書かれた原文の表記。どちらも対応表の1行（`GithubMemberLink`）と同じ規則で検証する。
    login を持たない人はこの型にならないので、github_login は必ず値を持つ。

    lead_time は PR が1件以上あるときだけ値を持ち、その count は merged_pr_count と一致
    する（0件の人は None）。既定値を置かないのは、件数と要約が食い違う行を書けなく
    するため。
    """

    email: str
    github_login: str
    merged_pr_count: int
    lead_time: LeadTimeSummary | None

    def __post_init__(self) -> None:
        if self.github_login is None:
            raise ValueError(
                "github_login は必須です（対応表に login を持つ人だけが行になります）"
            )
        # 字句の検査は対応表の1行へ委ねる。ここに写しを置くと、対応表では通る login が
        # 集計の側で落ちる（またはその逆の）状態を作りうる
        link = GithubMemberLink(email=self.email, github_login=self.github_login)
        object.__setattr__(self, "email", link.email)
        if not _is_count(self.merged_pr_count):
            raise ValueError(
                f"merged_pr_count には0以上の件数が必要です: {self.merged_pr_count!r}"
            )
        if self.lead_time is not None and not isinstance(
            self.lead_time, LeadTimeSummary
        ):
            raise TypeError(
                "lead_time には LeadTimeSummary か None が必要です: "
                f"{type(self.lead_time).__name__}"
            )
        # 件数と要約は同じ PR の集合から出る。片方だけが立つ行は、要約の母数を説明できない
        if (self.merged_pr_count == 0) != (self.lead_time is None):
            raise ValueError(
                "merged_pr_count が0の人だけ lead_time を None にしてください: "
                f"{self.merged_pr_count} 件 / {self.lead_time!r}"
            )
        if self.lead_time is not None and self.lead_time.count != self.merged_pr_count:
            raise ValueError(
                "lead_time の件数が merged_pr_count と一致しません: "
                f"{self.lead_time.count} / {self.merged_pr_count}"
            )


@dataclass(frozen=True)
class GithubMetrics:
    """1組織×1月の merged PR 数・lead time と、集計から外した分の内訳。

    users は対象者のうち対応表に login を持つ全員で、PR が0件の人も行として持つ（0件で
    あることも参考情報のため）。email の昇順で、email も login（小文字比較）も重複しない。

    users 以外の件数は集計から外した PR の内訳で、`mapped_prs` と足すと total_prs に
    なる（1件の PR が必ず1つの区分に入ることの機械検査）。unmapped_authors / unmapped_prs
    は対象者の login に当たらない作成者（対応表に無い人と、対応表にあっても対象者でない
    人）の人数と件数で、login そのものは持たない。

    lead_time は対象者の PR（`mapped_prs` の分）の要約で、組織全体の値になる。削除済みの
    アカウントと対象者に当たらない作成者の PR は件数だけを残し、要約には入れない。
    `mapped_prs` が0件なら None で、値があるときの count は `mapped_prs` と一致する。

    cache_complete は対象月の収集を読み切ったか（`PrCache.complete`）。False のときの
    件数は部分的な値で、その旨は warnings にも入る。
    """

    github_org: str
    month: str
    users: tuple[UserPrMetrics, ...] = ()
    lead_time: LeadTimeSummary | None = None
    unmapped_authors: int = 0
    unmapped_prs: int = 0
    bot_prs: int = 0
    deleted_author_prs: int = 0
    excluded_repository_prs: int = 0
    total_prs: int = 0
    cache_complete: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_github_org_name(self.github_org):
            raise ValueError(
                f"github_org には Organization 名が必要です: {self.github_org!r}"
            )
        _check_month(self.month)
        if not isinstance(self.users, tuple) or not all(
            isinstance(user, UserPrMetrics) for user in self.users
        ):
            raise TypeError("users には UserPrMetrics の tuple が必要です")
        emails = [user.email for user in self.users]
        if len(set(emails)) != len(emails):
            raise ValueError(f"users の email が重複しています: {emails}")
        if emails != sorted(emails):
            raise ValueError(f"users は email の昇順で並べてください: {emails}")
        logins = [user.github_login.lower() for user in self.users]
        if len(set(logins)) != len(logins):
            raise ValueError(f"users の github_login が重複しています: {logins}")
        for name in _COUNT_FIELDS:
            if not _is_count(getattr(self, name)):
                raise ValueError(
                    f"{name} には0以上の件数が必要です: {getattr(self, name)!r}"
                )
        # 対象者に当たらない作成者は人数と件数を対で持つ。片方だけが立つ結果は、除外の
        # 理由を説明できない件数（または PR を1件も持たない人数）になる
        if (self.unmapped_authors == 0) != (self.unmapped_prs == 0):
            raise ValueError(
                "対象者に当たらない作成者は人数と件数の両方を持たせてください: "
                f"{self.unmapped_authors} 人 / {self.unmapped_prs} 件"
            )
        if self.unmapped_authors > self.unmapped_prs:
            raise ValueError(
                "対象者に当たらない作成者の人数が PR の件数を超えています: "
                f"{self.unmapped_authors} 人 / {self.unmapped_prs} 件"
            )
        # 1件の PR は必ず1つの区分に入る。合計が合わない結果は、どこかの区分から
        # 静かに漏れた PR がある（参考指標が黙って小さく出る）
        counted = (
            self.mapped_prs
            + self.unmapped_prs
            + self.bot_prs
            + self.deleted_author_prs
            + self.excluded_repository_prs
        )
        if counted != self.total_prs:
            raise ValueError(
                f"区分ごとの件数の合計が全件数と一致しません: {counted} / "
                f"{self.total_prs}（個人別 {self.mapped_prs}・対象者に当たらない作成者 "
                f"{self.unmapped_prs}・Bot {self.bot_prs}・削除済み "
                f"{self.deleted_author_prs}・対象外 repository "
                f"{self.excluded_repository_prs}）"
            )
        if self.lead_time is not None and not isinstance(
            self.lead_time, LeadTimeSummary
        ):
            raise TypeError(
                "lead_time には LeadTimeSummary か None が必要です: "
                f"{type(self.lead_time).__name__}"
            )
        # 要約の母数は対象者の PR。件数と食い違う要約は、何を代表した値か説明できない
        if (self.mapped_prs == 0) != (self.lead_time is None):
            raise ValueError(
                "対象者の PR が1件も無いときだけ lead_time を None にしてください: "
                f"{self.mapped_prs} 件 / {self.lead_time!r}"
            )
        if self.lead_time is not None and self.lead_time.count != self.mapped_prs:
            raise ValueError(
                "lead_time の件数が対象者の PR の件数と一致しません: "
                f"{self.lead_time.count} / {self.mapped_prs}"
            )
        if not isinstance(self.cache_complete, bool):
            raise TypeError(
                f"cache_complete には真偽値が必要です: "
                f"{type(self.cache_complete).__name__}"
            )
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, str) for warning in self.warnings
        ):
            raise TypeError("warnings には文字列の tuple が必要です")

    @property
    def mapped_prs(self) -> int:
        """対象者へ帰属した PR の件数（users の合計で、lead_time の母数）。"""
        return sum(user.merged_pr_count for user in self.users)


# --------------------------------------------------------------------- 公開 API


def summarize_lead_times(hours: Iterable[float]) -> LeadTimeSummary | None:
    """lead time の並びを要約する（1件も無ければ None）。

    渡す順には依らない（内部で昇順へ並べ直す）。件数の下限は設けないので、1件のときは
    3つの値がその1件と等しくなる。何件から出た値かは count が伝える。
    """
    values = sorted(_as_hours("lead_time_hours", value) for value in hours)
    if not values:
        return None
    return LeadTimeSummary(
        count=len(values),
        median_hours=_percentile(values, 0.5),
        p75_hours=_percentile(values, 0.75),
        p90_hours=_percentile(values, 0.9),
    )


def _subject_emails(subjects: object) -> frozenset[str]:
    """対象者の email の集合を、対応表と同じ表記（前後空白を除いて小文字）へ揃える。

    集合で受けるのは、対象者に順序が無いことを型で表すため（並びは結果の側で email の
    昇順に決める）。正規化は対応表の1行（`GithubMemberLink`）へ委ねる——規則は ingest が
    spend / members の email 列に施すものと同じで、ここに写しを置くと、メンバー一覧では
    同じ人が対応表の側で別人になる状態を作りうる。空の email は ValueError。
    """
    if not isinstance(subjects, frozenset) or not all(
        isinstance(email, str) for email in subjects
    ):
        raise TypeError(
            "subjects には email（文字列）の frozenset が必要です: "
            f"{type(subjects).__name__}"
        )
    return frozenset(
        GithubMemberLink(email=email, github_login=None).email for email in subjects
    )


def _linked_inputs(
    cache: PrCache, members: GithubMembers, repos: RepoDiscovery,
    subjects: frozenset[str],
) -> tuple[set[str], dict[str, GithubMemberLink], frozenset[str]]:
    """集計の入力を検査し、対象 repository と対象者の対応表を返す。"""
    for name, value, expected in (
        ("cache", cache, PrCache),
        ("members", members, GithubMembers),
        ("repos", repos, RepoDiscovery),
    ):
        if not isinstance(value, expected):
            raise TypeError(
                f"{name} には {expected.__name__} が必要です: {type(value).__name__}"
            )
    subject_emails = _subject_emails(subjects)
    if repos.github_org != cache.github_org:
        raise ValueError(
            "repository の一覧と PR キャッシュの Organization が違います: "
            f"{repos.github_org!r} / {cache.github_org!r}"
        )
    if not repos.complete:
        raise ValueError(
            "repository の一覧が完全でないため集計できません"
            f"（status={repos.status!r} failure={repos.failure!r}）。"
            "一覧を取り直してから集計してください"
        )

    known_repos = {name.lower() for name in repos.repos}
    # login（小文字）→ 対応表の行（対象者の行だけ）。行順を保つので、同じ対応表からは
    # 常に同じ並びで作れる
    linked = {
        entry.github_login.lower(): entry
        for entry in members.entries
        if entry.github_login is not None and entry.email in subject_emails
    }
    return known_repos, linked, subject_emails


def pr_metrics(
    cache: PrCache,
    members: GithubMembers,
    repos: RepoDiscovery,
    subjects: frozenset[str],
) -> GithubMetrics:
    """その月の merged PR を、ユーザ単位の件数と lead time へ畳む（設計書 §15.5）。

    subjects は対象者（その月にこの組織でシートを持つ人）の email の集合。人ごとの行は
    対象者のうち対応表に login を持つ人だけで、組織全体の lead time も対象者の PR だけで
    要約する。対象者が0人でも対応表全体へ戻すことはしない（対象者の PR が0件の結果になる）。

    PR は次の順で排他的に1つの区分へ入れる: 対象外 repository → 削除済みのアカウント →
    Bot → 対象者の login と一致する人 → それ以外（対応表に無い作成者と、対応表にあっても
    対象者でない人）。repository と login はどちらも小文字で突き合わせる（GitHub は
    repository 名も login も大文字小文字を区別しないため、表記の違いで別物として数えない）。

    集計できない入力は結果を返さずに中止する（fail-closed）。repository の一覧が別の
    Organization のものだったり完全でなかったりすると、対象の PR が「対象外」へ流れて
    参考指標が黙って小さく出るため。

    対応表が無い組織でも呼べる（users が空になり、Bot と削除済み以外の PR はすべて
    対象者に当たらない作成者の分として数える）。
    """
    known_repos, linked, subject_emails = _linked_inputs(cache, members, repos, subjects)
    counts = dict.fromkeys(linked, 0)
    # 個人別の lead time（login → 時の並び）と、対象者の PR 全件の lead time
    user_hours: dict[str, list[float]] = {login: [] for login in linked}
    mapped_hours: list[float] = []

    unmapped_logins: set[str] = set()   # 人数を数えるためだけに持つ（結果へは残さない）
    unmapped_prs = bot_prs = deleted_author_prs = excluded_repository_prs = 0
    for pr in cache.prs:
        if pr.repository.lower() not in known_repos:
            excluded_repository_prs += 1
            continue
        if pr.author_login is None:
            # 削除済みのアカウント（`CachedPr` の不変条件で author_type も None）。
            # 誰の実績かを知る手立てが無いので、対応表の記入では解消しない＝警告しない
            deleted_author_prs += 1
            continue
        if pr.author_type == BOT_AUTHOR_TYPE:
            bot_prs += 1
            continue
        login = pr.author_login.lower()
        if login in counts:
            hours = lead_time_hours(pr)
            counts[login] += 1
            user_hours[login].append(hours)
            mapped_hours.append(hours)
        else:
            unmapped_prs += 1
            unmapped_logins.add(login)

    users = tuple(sorted(
        (
            UserPrMetrics(
                email=entry.email,
                github_login=entry.github_login,
                merged_pr_count=counts[login],
                lead_time=summarize_lead_times(user_hours[login]),
            )
            for login, entry in linked.items()
        ),
        key=lambda user: user.email,
    ))

    warnings: list[str] = []
    if not members.provided:
        warnings.append(
            "GitHub ID の対応表がありません（members-info.csv に GitHub ID 列が"
            "無い）。PR を個人に帰属できません"
        )
    else:
        # 対応表が無いときは全員が該当するので並べない（上の警告が同じことを伝える）
        unlinked = unmapped_emails(members, subject_emails)
        if unlinked:
            warnings.append(
                f"シートを持つ人のうち GitHub login に対応づかない人 {len(unlinked)} 名: "
                f"{list(unlinked)}（この人の PR は人ごとの行にも組織全体の件数にも"
                "入りません。members-info.csv の GitHub ID 列に login を書くか、対象の "
                "GitHub Organization で開発しない人は「なし」と書いてください）"
            )
    if unmapped_prs:
        warnings.append(
            f"シートを持つ人の login に当たらない作成者 {len(unmapped_logins)} 人による "
            f"PR {unmapped_prs} 件を集計から除外しました（人ごとの行・組織全体の件数とも。"
            "対応表に無い人と、対応表にあってもこの組織でシートを持たない人を含みます）"
        )
    if not cache.complete:
        warnings.append(
            f"{cache.month} の収集が完了していません。件数は部分的な値です"
        )

    return GithubMetrics(
        github_org=cache.github_org,
        month=cache.month,
        users=users,
        lead_time=summarize_lead_times(mapped_hours),
        unmapped_authors=len(unmapped_logins),
        unmapped_prs=unmapped_prs,
        bot_prs=bot_prs,
        deleted_author_prs=deleted_author_prs,
        excluded_repository_prs=excluded_repository_prs,
        total_prs=len(cache.prs),
        cache_complete=cache.complete,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class CommitTotals:
    """対象者へ帰属したコミットの件数と行数。

    commit_count は数えたコミットの件数、lines_added / lines_removed は追加行・削除行の
    合計で、いずれも大きな変更を含む。lines_added_capped は大きな変更を除いた追加行、
    large_commits は大きな変更の件数。すべて0以上の整数（真偽値は不可）で、大きな変更の
    件数は全件数を、上限つき追加行は追加行の合計を超えない。

    コミット0件なら行数も0、大きな変更が0件なら追加行と上限つき合計が等しく、全件が
    大きな変更なら上限つき合計は0にする。集計した件数では説明できない行数を持つ結果を
    防ぐための条件。追加行が0で削除行だけのコミットもあるので、コミットがあれば追加行が
    あるとは限らない。
    """

    commit_count: int
    lines_added: int
    lines_removed: int
    lines_added_capped: int
    large_commits: int

    def __post_init__(self) -> None:
        for name in _COMMIT_TOTAL_FIELDS:
            if not _is_count(getattr(self, name)):
                raise ValueError(f"{name} には0以上の整数が必要です")
        if self.large_commits > self.commit_count:
            raise ValueError("large_commits が commit_count を超えています")
        if self.lines_added_capped > self.lines_added:
            raise ValueError("lines_added_capped が lines_added を超えています")
        if self.commit_count == 0 and (self.lines_added or self.lines_removed):
            raise ValueError("コミット0件の行数は0にしてください")
        if self.large_commits == 0 and self.lines_added != self.lines_added_capped:
            raise ValueError("大きな変更が無いときは追加行の合計を一致させてください")
        if (self.commit_count > 0 and self.large_commits == self.commit_count
                and self.lines_added_capped != 0):
            raise ValueError("すべて大きな変更のときは上限つき合計を0にしてください")


@dataclass(frozen=True)
class UserCommitMetrics:
    """対応表の email と、1人分のコミット集計。

    email は対応表の鍵と同じ表記（前後空白を除いて小文字へ揃える）で、正規化と字句の
    検査は GithubMemberLink に委ねる。同じ人を表記の違いで別の行へ帰属させないため。
    totals は CommitTotals で、コミットが0件の人も0の合計を持つ。
    """

    email: str
    totals: CommitTotals

    def __post_init__(self) -> None:
        link = GithubMemberLink(email=self.email, github_login=None)
        object.__setattr__(self, "email", link.email)
        if not isinstance(self.totals, CommitTotals):
            raise TypeError("totals には CommitTotals が必要です")


@dataclass(frozen=True)
class CommitMetrics:
    """1組織×1月のコミット集計と、帰属できない作者の件数。

    github_org は Organization 名、month は対象月（YYYY-MM）。users は pr_metrics の
    users と同じ人・同じ並び（email の昇順・重複なし）で、0件の人も含む。github-summary
    の個人行に並べて書くため、コミットの件数で人や並びを変えない。

    unresolved_author_commits は対象者に帰属できなかったコミットの件数、counted_commits
    は除外規則を通ったコミットの総数。どちらも0以上の整数（真偽値は不可）で、対象者の
    件数と unresolved の件数の合計は counted_commits に一致する。1件が帰属先から漏れたり、
    対象者と unresolved の両方に入ったりした結果を作れないようにする検査。
    totals は対象者だけの合計で、unresolved の行数は含まない。

    warnings は集計の制約を伝える文字列の tuple で、呼び出し側がその順で通知できる。
    """

    github_org: str
    month: str
    users: tuple[UserCommitMetrics, ...]
    unresolved_author_commits: int
    counted_commits: int
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_github_org_name(self.github_org):
            raise ValueError("github_org には Organization 名が必要です")
        _check_month(self.month)
        if not isinstance(self.users, tuple) or not all(
            isinstance(user, UserCommitMetrics) for user in self.users
        ):
            raise TypeError("users には UserCommitMetrics の tuple が必要です")
        emails = [user.email for user in self.users]
        if emails != sorted(set(emails)):
            raise ValueError("users は email の昇順・重複なしにしてください")
        for name in ("unresolved_author_commits", "counted_commits"):
            if not _is_count(getattr(self, name)):
                raise ValueError(f"{name} には0以上の整数が必要です")
        if self.totals.commit_count + self.unresolved_author_commits != self.counted_commits:
            raise ValueError("対象者と unresolved の合計が counted_commits と一致しません")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, str) for warning in self.warnings
        ):
            raise TypeError("warnings には文字列の tuple が必要です")

    @property
    def totals(self) -> CommitTotals:
        """対象者の合計（unresolved の行数は含めない）。"""
        return CommitTotals(**{
            name: sum(getattr(user.totals, name) for user in self.users)
            for name in _COMMIT_TOTAL_FIELDS
        })


def has_commits(cache: PrCache) -> bool:
    """schema 2 で、全 PR が commits を持つキャッシュか。

    PR が0件の schema 2 は True、schema 1 は PR が0件でも False を返す。
    """
    return cache.schema == 2 and all(pr.commits is not None for pr in cache.prs)


def commit_metrics(
    cache: PrCache,
    members: GithubMembers,
    repos: RepoDiscovery,
    subjects: frozenset[str],
    *,
    earlier: tuple[PrCache, ...] = (),
    large_commit_lines: int,
) -> CommitMetrics | None:
    """キャッシュのコミットを作者ごとの行数へ畳む（設計書 §15.7）。

    数える規則と作者の解決はモジュールの docstring を参照。対象月のキャッシュが schema 2
    でないか、commits が None の PR を含むときは None を返す（has_commits の判定）。
    PR が0件の schema 2 は全員0の結果を返す。

    earlier は対象月より前の、コミットを持つキャッシュ。月の重複は許さず、渡す順には
    依らない。large_commit_lines は設定から受け取る大きな変更の閾値で、既定値を持たない。

    入力の型（earlier の tuple と各要素の PrCache を含む）が違えば TypeError。
    Organization・対象者の email・repository 一覧の完全性・earlier の月やコミットの材料が
    不正なら ValueError。閾値が1以上の整数でない場合も ValueError にする（真偽値は不可）。
    ファイルも現在時刻も参照しない。
    """
    known_repos, linked, _ = _linked_inputs(cache, members, repos, subjects)
    if not _is_count(large_commit_lines) or large_commit_lines < 1:
        raise ValueError("large_commit_lines には1以上の整数が必要です")
    if not has_commits(cache):
        return None
    if not isinstance(earlier, tuple):
        raise TypeError("earlier には PrCache の tuple が必要です")
    months: set[str] = set()
    for previous in earlier:
        if not isinstance(previous, PrCache):
            raise TypeError("earlier の各要素には PrCache が必要です")
        if (previous.github_org != cache.github_org or previous.month >= cache.month
                or previous.month in months):
            raise ValueError("earlier は同じ Organization の重複しない過去月にしてください")
        if not has_commits(previous):
            raise ValueError("earlier のキャッシュにはコミットが必要です")
        months.add(previous.month)

    generated = {
        pr.merge_commit_oid
        for item in (cache, *earlier)
        for pr in item.prs
        if pr.merge_commit_oid is not None
        and pr.merge_commit_oid not in {commit.oid for commit in pr.commits}
    }
    seen = {
        commit.oid
        for item in earlier
        for pr in item.prs if pr.repository.lower() in known_repos
        for commit in pr.commits
    }
    targets = sorted(
        (pr for pr in cache.prs if pr.repository.lower() in known_repos),
        key=lambda pr: (pr.merged_at, pr.repository.lower(), pr.number),
    )
    totals = {login: dict.fromkeys(_COMMIT_TOTAL_FIELDS, 0) for login in linked}
    unresolved = counted = 0
    for pr in targets:
        for commit in pr.commits:
            if commit.oid in seen:
                continue
            seen.add(commit.oid)
            if commit.is_merge or commit.oid in generated:
                continue
            counted += 1
            login = commit.author_login if commit.author_login is not None else pr.author_login
            if login is None or login.lower() not in linked:
                unresolved += 1
                continue
            row = totals[login.lower()]
            row["commit_count"] += 1
            row["lines_added"] += commit.additions
            row["lines_removed"] += commit.deletions
            if commit.additions > large_commit_lines:
                row["large_commits"] += 1
            else:
                row["lines_added_capped"] += commit.additions

    warnings: list[str] = []
    truncated = sum(pr.commit_count > len(pr.commits) for pr in targets)
    if truncated:
        warnings.append(
            f"コミットが 250 件を超える PR が {truncated} 本あり、"
            "超えた分のコミットは数えていません"
        )
    if unresolved:
        warnings.append(
            f"シートを持つ人に帰属できなかったコミット {unresolved} 件を行数の集計から"
            "除外しました（作者の login がシートを持つ人に当たらないコミットと、作者の"
            " GitHub アカウントが分からず PR の作成者もシートを持つ人でないコミット）"
        )
    return CommitMetrics(
        github_org=cache.github_org,
        month=cache.month,
        users=tuple(sorted(
            (UserCommitMetrics(entry.email, CommitTotals(**totals[login]))
             for login, entry in linked.items()),
            key=lambda user: user.email,
        )),
        unresolved_author_commits=unresolved,
        counted_commits=counted,
        warnings=tuple(warnings),
    )
