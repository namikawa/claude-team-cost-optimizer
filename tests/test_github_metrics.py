"""merged PR をユーザ×月の件数へ畳む計算のテスト。

固定するのは4つ。1つ目は帰属の規則——PR が数えられる月は merge した月で、作成した月では
ない（`created_at` を見ない）。2つ目は突き合わせ——repository 名も login も大文字小文字を
区別せずに照合する（GitHub がそう扱うため、表記の違いで別物として数えない）。3つ目は
区分——1件の PR は必ず1つの区分に入り、合計が常にキャッシュの全件数と一致する。4つ目は
外した分の扱い——Bot・対応表に無い作成者・削除済みのアカウント・対象外 repository は
件数として残し、対応表に無い作成者の login は結果のどこにも現れない。

集計できない入力（別 Organization の repository 一覧・完全でない一覧）で結果を返さない
ことも見る。部分的な一覧で照合すると参考指標が黙って小さく出るため、値ではなくエラーに
する側へ倒している。

gh もファイルも呼ばないモジュールなので、同じ入力からは常に同じ結果になることまで見る
（users は email の昇順・警告の並びも固定）。
"""

import pytest

from seat_analyzer.github_collect import (
    CachedPr,
    GhFailure,
    GithubMemberLink,
    GithubMembers,
    PrCache,
    RepoDiscovery,
    month_windows,
)
from seat_analyzer.github_metrics import (
    BOT_AUTHOR_TYPE,
    GithubMetrics,
    UserPrMetrics,
    merged_pr_counts,
)
from seat_analyzer.ingest import MEMBERS_INFO_FILENAME

ORG = "example-org"
MONTH = "2026-08"


def _pr(**overrides) -> CachedPr:
    """merged PR 1件（項目を上書きできる）。"""
    values = {
        "repository": "repo-a",
        "number": 12,
        "author_login": "octocat",
        "author_type": "User",
        "created_at": "2026-08-01T00:00:00Z",
        "merged_at": "2026-08-03T10:30:00Z",
        "additions": 10,
        "deletions": 2,
        "is_draft": False,
    }
    values.update(overrides)
    return CachedPr(**values)


def _cache(prs: tuple[CachedPr, ...] = (), month: str = MONTH,
           complete: bool = True, github_org: str = ORG) -> PrCache:
    """1組織×1月のキャッシュ（PR はキャッシュの規約どおりの並びに整えて渡す）。"""
    return PrCache(
        github_org=github_org,
        month=month,
        prs=tuple(sorted(prs, key=lambda pr: (pr.repository.lower(), pr.number))),
        complete_windows=month_windows(month) if complete else (),
    )


def _link(email: str, login: str | None = None,
          no_account: bool = False) -> GithubMemberLink:
    """対応表の1行（login を省くと未対応）。"""
    return GithubMemberLink(
        email=email, github_login=login, no_account=no_account
    )


def _members(*entries: GithubMemberLink, source: str | None = MEMBERS_INFO_FILENAME,
             has_column: bool = True,
             warnings: tuple[str, ...] = ()) -> GithubMembers:
    """対応表（行順はそのまま保つ）。"""
    return GithubMembers(
        entries=entries, source=source, has_column=has_column, warnings=warnings
    )


def _repos(*names: str, github_org: str = ORG) -> RepoDiscovery:
    """完全な repository の一覧（名前は一覧の規約どおり小文字比較の昇順で持つ）。"""
    return RepoDiscovery(
        github_org=github_org,
        repos=tuple(sorted(names, key=str.lower)),
        status=200,
    )


# --------------------------------------------------------------------- 帰属


def test_merged_month_comes_from_the_merge_not_the_creation():
    """前月に作られ当月に merge された PR は当月に数える（created_at は見ない）。"""
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, created_at="2026-07-20T09:00:00Z",
                merged_at="2026-08-01T00:00:00Z"),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.month == MONTH
    assert metrics.users == (
        UserPrMetrics(
            email="user1@example.com", github_login="octocat", merged_pr_count=1
        ),
    )
    assert metrics.total_prs == 1
    assert metrics.mapped_prs == 1


def test_login_matches_the_mapping_case_insensitively():
    """login の大文字小文字の違いは同じ1人（GitHub は login の大小を区別しない）。"""
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, author_login="OctoCat"),
            _pr(number=2, author_login="octocat"),
            _pr(number=3, author_login="OCTOCAT"),
        )),
        _members(_link("user1@example.com", "Octocat")),
        _repos("repo-a"),
    )
    assert metrics.users[0].merged_pr_count == 3
    assert metrics.users[0].github_login == "Octocat"   # 表記は対応表のまま
    assert metrics.unmapped_prs == 0


def test_the_org_of_the_result_comes_from_the_cache():
    metrics = merged_pr_counts(_cache(), _members(), _repos())
    assert metrics.github_org == ORG
    assert metrics.cache_complete is True


# ------------------------------------------------------------------- 除外


def test_bot_prs_are_excluded_from_the_per_person_counts():
    """Bot の PR は個人の実績ではないので users に出さず、件数だけを残す。"""
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, author_login="octocat"),
            _pr(number=2, author_login="example-bot[bot]",
                author_type=BOT_AUTHOR_TYPE),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.bot_prs == 1
    assert metrics.users[0].merged_pr_count == 1
    assert metrics.unmapped_prs == 0
    assert metrics.warnings == ()


def test_the_bot_type_is_matched_exactly():
    """種別は完全一致で見る（似た字句を Bot として畳まない）。"""
    metrics = merged_pr_counts(
        _cache((_pr(number=1, author_login="example-bot[bot]", author_type="bot"),)),
        _members(),
        _repos("repo-a"),
    )
    assert metrics.bot_prs == 0
    assert metrics.unmapped_prs == 1


def test_deleted_authors_are_counted_without_a_warning():
    """削除済みのアカウントは対応表の記入では解消しないので警告しない。"""
    metrics = merged_pr_counts(
        _cache((_pr(number=1, author_login=None, author_type=None),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.deleted_author_prs == 1
    assert metrics.unmapped_authors == 0
    assert metrics.unmapped_prs == 0
    assert metrics.warnings == ()


def test_prs_of_repositories_outside_the_list_are_excluded():
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, repository="repo-a"),
            _pr(number=2, repository="repo-b"),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.excluded_repository_prs == 1
    assert metrics.users[0].merged_pr_count == 1


def test_repository_names_match_case_insensitively():
    """repository 名の大小違いは同じ repository（GitHub は名前の大小を区別しない）。"""
    metrics = merged_pr_counts(
        _cache((_pr(number=1, repository="Repo-A"),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.excluded_repository_prs == 0
    assert metrics.users[0].merged_pr_count == 1


def test_the_repository_check_comes_before_the_author_check():
    """対象外 repository の PR は作成者を問わず対象外として数える。"""
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, repository="repo-b", author_login="example-bot[bot]",
                author_type=BOT_AUTHOR_TYPE),
            _pr(number=2, repository="repo-b", author_login=None, author_type=None),
            _pr(number=3, repository="repo-b", author_login="ghost-writer"),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.excluded_repository_prs == 3
    assert (metrics.bot_prs, metrics.deleted_author_prs, metrics.unmapped_prs) == (
        0, 0, 0
    )
    assert metrics.warnings == ()


# --------------------------------------------------- 対応表に無い作成者


def test_unmapped_authors_are_counted_per_person():
    """同じ login の複数 PR は1人として数える（人数と件数は別の値）。"""
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, author_login="ghost-writer"),
            _pr(number=2, author_login="Ghost-Writer"),
            _pr(number=3, author_login="passer-by"),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.unmapped_authors == 2
    assert metrics.unmapped_prs == 3
    assert metrics.users[0].merged_pr_count == 0
    assert metrics.warnings == (
        "対応表に無い作成者 2 人による PR 3 件を個人別の集計から除外しました",
    )


def test_unmapped_logins_never_appear_in_the_result():
    """外した作成者の login は結果のどこにも残さない（個人を名前で並べない）。"""
    metrics = merged_pr_counts(
        _cache((_pr(number=1, author_login="ghost-writer"),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert "ghost-writer" not in repr(metrics)
    assert not any("ghost-writer" in warning for warning in metrics.warnings)


# ----------------------------------------------------------------- users


def test_members_without_prs_get_a_zero_row():
    """PR が0件の人も行として出す（0件であることも参考情報のため）。"""
    metrics = merged_pr_counts(
        _cache((_pr(number=1, author_login="octocat"),)),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
        ),
        _repos("repo-a"),
    )
    assert metrics.users == (
        UserPrMetrics(
            email="user1@example.com", github_login="octocat", merged_pr_count=1
        ),
        UserPrMetrics(
            email="user2@example.com", github_login="example-user", merged_pr_count=0
        ),
    )


def test_rows_without_a_login_are_not_listed():
    """login を持たない行（未記入・アカウントなし）は users に出さない。"""
    metrics = merged_pr_counts(
        _cache(),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com"),                     # 空欄
            _link("user3@example.com", no_account=True),    # アカウントを持たない
        ),
        _repos("repo-a"),
    )
    assert [user.email for user in metrics.users] == ["user1@example.com"]


def test_users_are_sorted_by_email():
    """並びは対応表の行順ではなく email の昇順にする。"""
    metrics = merged_pr_counts(
        _cache(),
        _members(
            _link("user3@example.com", "login-c"),
            _link("user1@example.com", "login-a"),
            _link("user2@example.com", "login-b"),
        ),
        _repos("repo-a"),
    )
    assert [user.email for user in metrics.users] == [
        "user1@example.com", "user2@example.com", "user3@example.com"
    ]


def test_an_empty_cache_still_lists_every_member():
    """PR が1件も無い月でも成立する（全員が0件の行になる）。"""
    metrics = merged_pr_counts(
        _cache(),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
        ),
        _repos("repo-a"),
    )
    assert [user.merged_pr_count for user in metrics.users] == [0, 0]
    assert metrics.total_prs == 0
    assert metrics.mapped_prs == 0
    assert metrics.warnings == ()


# --------------------------------------------------------------- 警告


@pytest.mark.parametrize("members", [
    _members(source=None, has_column=False),   # ファイルが無い
    _members(has_column=False),                # ファイルはあるが GitHub ID の列が無い
])
def test_without_a_mapping_no_pr_is_attributed(members):
    """対応表が無いと個人へ帰属できない（Bot と削除済み以外はすべて未対応の分）。"""
    metrics = merged_pr_counts(
        _cache((
            _pr(number=1, author_login="octocat"),
            _pr(number=2, author_login="example-bot[bot]",
                author_type=BOT_AUTHOR_TYPE),
            _pr(number=3, author_login=None, author_type=None),
        )),
        members,
        _repos("repo-a"),
    )
    assert metrics.users == ()
    assert (metrics.unmapped_authors, metrics.unmapped_prs) == (1, 1)
    assert metrics.warnings == (
        (
            "GitHub ID の対応表がありません（members-info.csv に GitHub ID 列が無い）。"
            "PR を個人に帰属できません"
        ),
        "対応表に無い作成者 1 人による PR 1 件を個人別の集計から除外しました",
    )


def test_a_mapping_with_no_rows_is_still_provided():
    """列がある表は、記入が1行も無くても「対応表がありません」とは言わない。"""
    metrics = merged_pr_counts(_cache(), _members(), _repos("repo-a"))
    assert metrics.warnings == ()


def test_an_incomplete_cache_is_flagged_as_partial():
    metrics = merged_pr_counts(
        _cache((_pr(number=1),), complete=False),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.cache_complete is False
    assert metrics.warnings == (
        "2026-08 の収集が完了していません。件数は部分的な値です",
    )


def test_a_complete_cache_has_no_partial_warning():
    metrics = merged_pr_counts(
        _cache((_pr(number=1),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.cache_complete is True
    assert metrics.warnings == ()


def test_warnings_follow_a_fixed_order():
    """対応表なし → 対応表に無い作成者 → 収集の未完了 の順に並べる。"""
    metrics = merged_pr_counts(
        _cache((_pr(number=1, author_login="ghost-writer"),), complete=False),
        _members(source=None, has_column=False),
        _repos("repo-a"),
    )
    assert len(metrics.warnings) == 3
    assert metrics.warnings[0].startswith("GitHub ID の対応表がありません")
    assert metrics.warnings[1].startswith("対応表に無い作成者 1 人")
    assert metrics.warnings[2].startswith("2026-08 の収集が完了していません")


def test_mapping_warnings_are_not_copied():
    """対応表の読み取りで出た警告は転記しない（doctor が同じ内容を出すため）。"""
    metrics = merged_pr_counts(
        _cache(),
        _members(_link("user1@example.com", "octocat"),
                 warnings=("GitHub ID が空欄です",)),
        _repos("repo-a"),
    )
    assert metrics.warnings == ()


# ------------------------------------------------------- 集計できない入力


@pytest.mark.parametrize("repos", [
    RepoDiscovery(github_org=ORG, status=403),
    RepoDiscovery(github_org=ORG, status=404),
    RepoDiscovery(github_org=ORG, failure=GhFailure.TIMEOUT),
])
def test_an_incomplete_repository_list_is_rejected(repos):
    """部分的な一覧で集計しない（対象の PR が対象外へ流れ、件数が黙って小さく出る）。"""
    with pytest.raises(ValueError, match="repository の一覧が完全でない"):
        merged_pr_counts(_cache((_pr(),)), _members(), repos)


def test_a_repository_list_of_another_organization_is_rejected():
    with pytest.raises(ValueError, match="Organization が違います"):
        merged_pr_counts(_cache(), _members(), _repos("repo-a", github_org="other-org"))


@pytest.mark.parametrize("args,message", [
    (("not a cache", _members(), _repos()), "cache には PrCache が必要です"),
    ((_cache(), "not members", _repos()), "members には GithubMembers が必要です"),
    ((_cache(), _members(), None), "repos には RepoDiscovery が必要です"),
])
def test_wrong_argument_types_are_rejected(args, message):
    with pytest.raises(TypeError, match=message):
        merged_pr_counts(*args)


# ------------------------------------------------------------------ 決定性


def _inputs() -> tuple[PrCache, GithubMembers, RepoDiscovery]:
    return (
        _cache((
            _pr(number=1, author_login="octocat"),
            _pr(number=2, author_login="ghost-writer"),
            _pr(number=3, repository="repo-b", author_login="example-user"),
            _pr(number=4, author_login=None, author_type=None),
        )),
        _members(
            _link("user2@example.com", "example-user"),
            _link("user1@example.com", "octocat"),
        ),
        _repos("repo-a"),
    )


def test_the_same_input_gives_the_same_result():
    cache, members, repos = _inputs()
    assert merged_pr_counts(cache, members, repos) == merged_pr_counts(
        cache, members, repos
    )


def test_the_row_order_of_the_mapping_does_not_change_the_result():
    """対応表の行順は結果に影響しない（PR の並びはキャッシュの不変条件で固定済み）。"""
    cache, members, repos = _inputs()
    reversed_members = _members(*reversed(members.entries))
    assert merged_pr_counts(cache, reversed_members, repos) == merged_pr_counts(
        cache, members, repos
    )


def test_every_pr_falls_into_exactly_one_bucket():
    """区分の合計は常に全件数（値オブジェクトの検査を結果の側でも確かめる）。"""
    metrics = merged_pr_counts(*_inputs())
    assert (
        metrics.mapped_prs,
        metrics.unmapped_prs,
        metrics.bot_prs,
        metrics.deleted_author_prs,
        metrics.excluded_repository_prs,
    ) == (1, 1, 0, 1, 1)
    assert metrics.total_prs == 4


# ------------------------------------------------------------ 値オブジェクト


def test_user_metrics_normalizes_and_validates_its_values():
    """email は対応表と同じ規則で正規化し、読めない login は受け付けない。"""
    user = UserPrMetrics(
        email=" User1@Example.com ", github_login="octocat", merged_pr_count=0
    )
    assert user.email == "user1@example.com"
    with pytest.raises(ValueError, match="email は必須です"):
        UserPrMetrics(email="  ", github_login="octocat", merged_pr_count=0)
    with pytest.raises(ValueError, match="github_login として読めない値です"):
        UserPrMetrics(
            email="user1@example.com", github_login="@octocat", merged_pr_count=0
        )
    with pytest.raises(ValueError, match="github_login は必須です"):
        UserPrMetrics(email="user1@example.com", github_login=None, merged_pr_count=0)


@pytest.mark.parametrize("count", [-1, True, 1.0, "1"])
def test_user_metrics_rejects_a_count_that_is_not_a_number_of_prs(count):
    with pytest.raises(ValueError, match="merged_pr_count には0以上の件数"):
        UserPrMetrics(
            email="user1@example.com", github_login="octocat", merged_pr_count=count
        )


def _user(email: str, login: str, count: int = 0) -> UserPrMetrics:
    return UserPrMetrics(email=email, github_login=login, merged_pr_count=count)


def _metrics(**overrides) -> GithubMetrics:
    values = {"github_org": ORG, "month": MONTH}
    values.update(overrides)
    return GithubMetrics(**values)


def test_metrics_accepts_a_consistent_result():
    metrics = _metrics(
        users=(_user("user1@example.com", "octocat", 2),),
        unmapped_authors=1,
        unmapped_prs=1,
        bot_prs=1,
        deleted_author_prs=1,
        excluded_repository_prs=1,
        total_prs=6,
        cache_complete=True,
    )
    assert metrics.mapped_prs == 2


@pytest.mark.parametrize("overrides,message", [
    ({"total_prs": 1}, "区分ごとの件数の合計が全件数と一致しません"),
    (
        {"users": (_user("user1@example.com", "octocat", 2),), "total_prs": 1},
        "区分ごとの件数の合計が全件数と一致しません",
    ),
    ({"bot_prs": -1}, "bot_prs には0以上の件数が必要です"),
    ({"total_prs": True}, "total_prs には0以上の件数が必要です"),
    ({"bot_prs": 1.0, "total_prs": 1}, "bot_prs には0以上の件数が必要です"),
    (
        {"unmapped_authors": 1},
        "対応表に無い作成者は人数と件数の両方を持たせてください",
    ),
    (
        {"unmapped_prs": 1, "total_prs": 1},
        "対応表に無い作成者は人数と件数の両方を持たせてください",
    ),
    (
        {"unmapped_authors": 2, "unmapped_prs": 1, "total_prs": 1},
        "対応表に無い作成者の人数が PR の件数を超えています",
    ),
    ({"github_org": "not an org"}, "github_org には Organization 名が必要です"),
    ({"month": "2026-13"}, "month には YYYY-MM 形式が必要です"),
    (
        {"users": (
            _user("user2@example.com", "login-b"),
            _user("user1@example.com", "login-a"),
        )},
        "users は email の昇順で並べてください",
    ),
    (
        {"users": (
            _user("user1@example.com", "login-a"),
            _user("user1@example.com", "login-b"),
        )},
        "users の email が重複しています",
    ),
    (
        {"users": (
            _user("user1@example.com", "Octocat"),
            _user("user2@example.com", "octocat"),
        )},
        "users の github_login が重複しています",
    ),
])
def test_metrics_rejects_an_inconsistent_result(overrides, message):
    with pytest.raises(ValueError, match=message):
        _metrics(**overrides)


@pytest.mark.parametrize("overrides,message", [
    ({"users": [_user("user1@example.com", "octocat")]},
     "users には UserPrMetrics の tuple が必要です"),
    ({"users": ("user1@example.com",)},
     "users には UserPrMetrics の tuple が必要です"),
    ({"cache_complete": "yes"}, "cache_complete には真偽値が必要です"),
    ({"warnings": ["注意"]}, "warnings には文字列の tuple が必要です"),
    ({"warnings": (1,)}, "warnings には文字列の tuple が必要です"),
])
def test_metrics_rejects_wrong_types(overrides, message):
    with pytest.raises(TypeError, match=message):
        _metrics(**overrides)
