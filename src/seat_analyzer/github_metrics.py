"""収集した merged PR を、ユーザ×月の参考指標へ畳む計算（設計書 §15.5）。

受け取るのは3つの値だけ——その月の PR キャッシュ（`github_collect.load_pr_cache`）、
email → GitHub login の対応表（`load_github_members`）、Organization 内の repository の
一覧（`discover_repositories`）。`gh` もネットワークもファイルも現在時刻も参照しないので、
同じ入力からは常に同じ結果と同じ警告の並びを返す。並びは email の昇順で明示的に決め、
集合の反復順には依らせない。

PR が帰属する月は merge した月で、それはキャッシュの形（1ファイル = 1組織 × 1月）が
保証している（`PrCache` は month 以外の月に merge された PR を受けない）。この計算は
`created_at` を見ない。

1件の PR は必ず1つの区分に入る——個人別の件数・対応表に無い作成者・Bot・削除済みの
アカウント・対象外 repository の5つで、先に該当した区分に入れる。区分の合計がキャッシュの
全件数に一致することを値オブジェクトが検査するので、どこかの区分から静かに漏れた PR は
結果を作る時点で落ちる。

Bot が作った PR は個人の実績ではないので個人別の集計から外す（`author_type` が
`BOT_AUTHOR_TYPE` の PR）。対応表に無い作成者の PR も外すが、外した分は件数と人数だけを
残し、login は結果のどこにも持たせない。対応表の外にいる人はこの組織のメンバーとは
限らず、その名前はこの組織の資料に載せる対象ではないため、結果にも警告にも写さない
（設計書 §15.6 の「個人ランキングを作らない」とも整合する）。件数を黙って落とさないよう、
外した分は警告に出す。

repository の一覧が完全でない結果では集計しない（`RepoDiscovery.complete`）。部分的な
一覧で照合すると、一覧に載らなかった repository の PR が「対象外」へ流れ、参考指標が
黙って小さく出る。同じ理由で、収集し切れていないキャッシュ（`PrCache.complete` が False）は
集計するが「部分的な値」であることを警告に残す。
"""

from __future__ import annotations

from dataclasses import dataclass

from .github_collect import (
    GithubMemberLink,
    GithubMembers,
    PrCache,
    RepoDiscovery,
    is_github_org_name,
    month_windows,
)

# 個人の実績から外す作成者の種別（GraphQL の `__typename`）。完全一致で見る——種別は
# 収集の側が字句のまま写すので、ここで表記を寄せると別の種別を Bot として畳みかねない
BOT_AUTHOR_TYPE = "Bot"

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


# ------------------------------------------------------------ 値オブジェクト


@dataclass(frozen=True)
class UserPrMetrics:
    """1人分の merged PR 数。

    email は対応表の鍵（前後空白を除いて小文字へ揃えた表記）、github_login は対応表に
    書かれた原文の表記。どちらも対応表の1行（`GithubMemberLink`）と同じ規則で検証する。
    login を持たない行はこの型にならないので、github_login は必ず値を持つ。
    """

    email: str
    github_login: str
    merged_pr_count: int

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


@dataclass(frozen=True)
class GithubMetrics:
    """1組織×1月の merged PR 数と、集計から外した分の内訳。

    users は対応表に login を持つ全員で、PR が0件の人も行として持つ（0件であることも
    参考情報のため）。email の昇順で、email も login（小文字比較）も重複しない。

    users 以外の件数は集計から外した PR の内訳で、`mapped_prs` と足すと total_prs に
    なる（1件の PR が必ず1つの区分に入ることの機械検査）。unmapped_authors は対応表に
    無い作成者の人数で、login そのものは持たない。

    cache_complete は対象月の収集を読み切ったか（`PrCache.complete`）。False のときの
    件数は部分的な値で、その旨は warnings にも入る。
    """

    github_org: str
    month: str
    users: tuple[UserPrMetrics, ...] = ()
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
        # 対応表に無い作成者は人数と件数を対で持つ。片方だけが立つ結果は、除外の理由を
        # 説明できない件数（または PR を1件も持たない人数）になる
        if (self.unmapped_authors == 0) != (self.unmapped_prs == 0):
            raise ValueError(
                "対応表に無い作成者は人数と件数の両方を持たせてください: "
                f"{self.unmapped_authors} 人 / {self.unmapped_prs} 件"
            )
        if self.unmapped_authors > self.unmapped_prs:
            raise ValueError(
                "対応表に無い作成者の人数が PR の件数を超えています: "
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
                f"{self.total_prs}（個人別 {self.mapped_prs}・対応表に無い作成者 "
                f"{self.unmapped_prs}・Bot {self.bot_prs}・削除済み "
                f"{self.deleted_author_prs}・対象外 repository "
                f"{self.excluded_repository_prs}）"
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
        """個人へ帰属した PR の件数（users の合計）。"""
        return sum(user.merged_pr_count for user in self.users)


# --------------------------------------------------------------------- 公開 API


def merged_pr_counts(
    cache: PrCache, members: GithubMembers, repos: RepoDiscovery
) -> GithubMetrics:
    """その月の merged PR を、ユーザ単位の件数へ畳む（設計書 §15.5）。

    PR は次の順で排他的に1つの区分へ入れる: 対象外 repository → 削除済みのアカウント →
    Bot → 対応表の login と一致する人 → 対応表に無い作成者。repository と login は
    どちらも小文字で突き合わせる（GitHub は repository 名も login も大文字小文字を
    区別しないため、表記の違いで別物として数えない）。

    集計できない入力は結果を返さずに中止する（fail-closed）。repository の一覧が別の
    Organization のものだったり完全でなかったりすると、対象の PR が「対象外」へ流れて
    参考指標が黙って小さく出るため。

    対応表が無い組織でも呼べる（users が空になり、Bot と削除済み以外の PR はすべて
    対応表に無い作成者の分として数える）。
    """
    for name, value, expected in (
        ("cache", cache, PrCache),
        ("members", members, GithubMembers),
        ("repos", repos, RepoDiscovery),
    ):
        if not isinstance(value, expected):
            raise TypeError(
                f"{name} には {expected.__name__} が必要です: {type(value).__name__}"
            )
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
    # login（小文字）→ 対応表の行。行順を保つので、同じ対応表からは常に同じ並びで作れる
    linked = {
        entry.github_login.lower(): entry
        for entry in members.entries
        if entry.github_login is not None
    }
    counts = dict.fromkeys(linked, 0)

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
            counts[login] += 1
        else:
            unmapped_prs += 1
            unmapped_logins.add(login)

    users = tuple(sorted(
        (
            UserPrMetrics(
                email=entry.email,
                github_login=entry.github_login,
                merged_pr_count=counts[login],
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
    if unmapped_prs:
        warnings.append(
            f"対応表に無い作成者 {len(unmapped_logins)} 人による PR {unmapped_prs} 件を"
            "個人別の集計から除外しました"
        )
    if not cache.complete:
        warnings.append(
            f"{cache.month} の収集が完了していません。件数は部分的な値です"
        )

    return GithubMetrics(
        github_org=cache.github_org,
        month=cache.month,
        users=users,
        unmapped_authors=len(unmapped_logins),
        unmapped_prs=unmapped_prs,
        bot_prs=bot_prs,
        deleted_author_prs=deleted_author_prs,
        excluded_repository_prs=excluded_repository_prs,
        total_prs=len(cache.prs),
        cache_complete=cache.complete,
        warnings=tuple(warnings),
    )
