"""merged PR をユーザ×月の件数と lead time へ畳む計算のテスト。

固定するのは6つ。1つ目は帰属の規則——PR が数えられる月は merge した月で、作成した月では
ない（件数の計算は `created_at` を見ない）。2つ目は突き合わせ——repository 名も login も
大文字小文字を区別せずに照合する（GitHub がそう扱うため、表記の違いで別物として数え
ない）。3つ目は区分——1件の PR は必ず1つの区分に入り、合計が常にキャッシュの全件数と
一致する。4つ目は外した分の扱い——Bot・対象者に当たらない作成者・削除済みのアカウント・
対象外 repository は件数として残し、対象者に当たらない作成者の login は結果のどこにも
現れない。

5つ目は対象者——人ごとの行も組織全体の要約も、対象者（その月にシートを持つ人）の PR
だけで作る。対応表に login があっても対象者でない人の PR は、対応表に無い作成者と同じ
区分に入る。6つ目は lead time——`merged_at − created_at` を時で出し、Draft だった期間も
含める。日時は UTC 固定の字句なので、実行環境の timezone を変えても値が動かないことまで
見る。要約は median / P75 / P90 の線形補間で、母数は個人別なら本人の PR、組織全体なら
対象者の PR。

集計できない入力（別 Organization の repository 一覧・完全でない一覧）で結果を返さない
ことも見る。部分的な一覧で照合すると参考指標が黙って小さく出るため、値ではなくエラーに
する側へ倒している。

gh もファイルも呼ばないモジュールなので、同じ入力からは常に同じ結果になることまで見る
（users は email の昇順・警告の並びも固定）。
"""

import os
import time

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
    LeadTimeSummary,
    UserPrMetrics,
    lead_time_hours,
    pr_metrics,
    summarize_lead_times,
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


def _pr_metrics(cache: PrCache, members: GithubMembers, repos: RepoDiscovery,
                subjects: frozenset[str] | None = None) -> GithubMetrics:
    """pr_metrics を呼ぶ。subjects を省くと対応表の全員を対象者にする。

    対象者の絞り込みそのものを見ないテスト（区分・lead time・警告の並び）では、対応表に
    書いた人がみなシートを持つ状態で確かめる。絞り込みを見るテストは subjects を渡す。
    """
    if subjects is None:
        subjects = frozenset(entry.email for entry in members.entries)
    return pr_metrics(cache, members, repos, subjects)


# --------------------------------------------------------------------- 帰属


def test_merged_month_comes_from_the_merge_not_the_creation():
    """前月に作られ当月に merge された PR は当月に数える（created_at は見ない）。"""
    metrics = _pr_metrics(
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
            email="user1@example.com",
            github_login="octocat",
            merged_pr_count=1,
            # 前月からまたいだ分も lead time に入る（11日15時間）
            lead_time=LeadTimeSummary(
                count=1, median_hours=279.0, p75_hours=279.0, p90_hours=279.0
            ),
        ),
    )
    assert metrics.total_prs == 1
    assert metrics.mapped_prs == 1


def test_login_matches_the_mapping_case_insensitively():
    """login の大文字小文字の違いは同じ1人（GitHub は login の大小を区別しない）。"""
    metrics = _pr_metrics(
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
    metrics = _pr_metrics(_cache(), _members(), _repos())
    assert metrics.github_org == ORG
    assert metrics.cache_complete is True


# ------------------------------------------------------------------- 除外


def test_bot_prs_are_excluded_from_the_per_person_counts():
    """Bot の PR は個人の実績ではないので users に出さず、件数だけを残す。"""
    metrics = _pr_metrics(
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
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="example-bot[bot]", author_type="bot"),)),
        _members(),
        _repos("repo-a"),
    )
    assert metrics.bot_prs == 0
    assert metrics.unmapped_prs == 1


def test_deleted_authors_are_counted_without_a_warning():
    """削除済みのアカウントは対応表の記入では解消しないので警告しない。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login=None, author_type=None),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.deleted_author_prs == 1
    assert metrics.unmapped_authors == 0
    assert metrics.unmapped_prs == 0
    assert metrics.warnings == ()


def test_prs_of_repositories_outside_the_list_are_excluded():
    metrics = _pr_metrics(
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
    metrics = _pr_metrics(
        _cache((_pr(number=1, repository="Repo-A"),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.excluded_repository_prs == 0
    assert metrics.users[0].merged_pr_count == 1


def test_the_repository_check_comes_before_the_author_check():
    """対象外 repository の PR は作成者を問わず対象外として数える。"""
    metrics = _pr_metrics(
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


# --------------------------------------------- 対象者に当たらない作成者


def test_unmapped_authors_are_counted_per_person():
    """同じ login の複数 PR は1人として数える（人数と件数は別の値）。"""
    metrics = _pr_metrics(
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
        (
            "シートを持つ人の login に当たらない作成者 2 人による PR 3 件を集計から除外"
            "しました（人ごとの行・組織全体の件数とも。対応表に無い人と、対応表にあっても"
            "この組織でシートを持たない人を含みます）"
        ),
    )


def test_unmapped_logins_never_appear_in_the_result():
    """外した作成者の login は結果のどこにも残さない（個人を名前で並べない）。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="ghost-writer"),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert "ghost-writer" not in repr(metrics)
    assert not any("ghost-writer" in warning for warning in metrics.warnings)


# ----------------------------------------------------------------- users


def test_members_without_prs_get_a_zero_row():
    """PR が0件の人も行として出す（0件であることも参考情報のため）。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="octocat"),)),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
        ),
        _repos("repo-a"),
    )
    assert metrics.users == (
        UserPrMetrics(
            email="user1@example.com",
            github_login="octocat",
            merged_pr_count=1,
            lead_time=LeadTimeSummary(
                count=1, median_hours=58.5, p75_hours=58.5, p90_hours=58.5
            ),
        ),
        UserPrMetrics(
            email="user2@example.com",
            github_login="example-user",
            merged_pr_count=0,
            lead_time=None,      # 0件の人は要約を持たない
        ),
    )


def test_rows_without_a_login_are_not_listed():
    """login を持たない行（未記入・「なし」）は users に出さない。"""
    metrics = _pr_metrics(
        _cache(),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com"),                     # 空欄
            _link("user3@example.com", no_account=True),    # 「なし」
        ),
        _repos("repo-a"),
    )
    assert [user.email for user in metrics.users] == ["user1@example.com"]


def test_users_are_sorted_by_email():
    """並びは対応表の行順ではなく email の昇順にする。"""
    metrics = _pr_metrics(
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
    metrics = _pr_metrics(
        _cache(),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
        ),
        _repos("repo-a"),
    )
    assert [user.merged_pr_count for user in metrics.users] == [0, 0]
    assert [user.lead_time for user in metrics.users] == [None, None]
    assert metrics.total_prs == 0
    assert metrics.mapped_prs == 0
    assert metrics.lead_time is None
    assert metrics.warnings == ()


# ------------------------------------------------------------------ 対象者


def test_a_mapped_login_without_a_seat_is_not_a_user_row():
    """対応表に login があっても対象者でない人は、行を持たず作成者の除外へ入る。

    シートを持たない人（未割当・シート不明・メンバー一覧に居ない）を誰にするかは呼び出し
    側が決めるので、ここでは「対象者の集合に入っていない」ことだけで確かめる。
    """
    metrics = _pr_metrics(
        _cache((
            _pr(number=1, author_login="octocat"),
            _pr(number=2, author_login="no-seat-user"),
            _pr(number=3, author_login="No-Seat-User"),
        )),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "no-seat-user"),
        ),
        _repos("repo-a"),
        frozenset({"user1@example.com"}),
    )
    assert [user.email for user in metrics.users] == ["user1@example.com"]
    assert metrics.mapped_prs == 1
    assert (metrics.unmapped_authors, metrics.unmapped_prs) == (1, 2)
    assert "no-seat-user" not in repr(metrics).lower()


def test_a_month_without_subject_prs_still_lists_the_subjects():
    """対象外の作成者の PR しかない月: 対象者は0件の行で並び、組織全体も0件になる。"""
    metrics = _pr_metrics(
        _cache((
            _pr(number=1, author_login="no-seat-user"),
            _pr(number=2, author_login="ghost-writer"),
        )),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
            _link("user3@example.com", "no-seat-user"),
        ),
        _repos("repo-a"),
        frozenset({"user1@example.com", "user2@example.com"}),
    )
    assert [(user.email, user.merged_pr_count) for user in metrics.users] == [
        ("user1@example.com", 0), ("user2@example.com", 0)
    ]
    assert metrics.mapped_prs == 0
    assert metrics.lead_time is None
    assert (metrics.unmapped_authors, metrics.unmapped_prs) == (2, 2)
    assert metrics.total_prs == 2


def test_without_subjects_nothing_falls_back_to_the_mapping():
    """対象者が0人なら行も0件（対応表の全員を代わりに並べない）。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="octocat"),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
        frozenset(),
    )
    assert metrics.users == ()
    assert metrics.lead_time is None
    assert (metrics.unmapped_authors, metrics.unmapped_prs) == (1, 1)


def test_subject_emails_are_matched_after_normalization():
    """対象者の email は対応表と同じ規則（前後空白を除いて小文字）で突き合わせる。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="octocat"),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
        frozenset({" User1@Example.COM "}),
    )
    assert [user.email for user in metrics.users] == ["user1@example.com"]
    assert metrics.mapped_prs == 1
    assert metrics.warnings == ()


def test_subjects_without_a_login_are_warned_by_email():
    """login に対応づかない対象者は人数と email（昇順）で知らせる。「なし」の人は除く。"""
    metrics = _pr_metrics(
        _cache(),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user3@example.com"),                    # 空欄（未記入）
            _link("user2@example.com", no_account=True),   # 「なし」
            _link("user9@example.com"),                    # 対象者でない空欄
        ),
        _repos("repo-a"),
        frozenset({
            "user1@example.com", "user2@example.com", "user3@example.com",
            "user4@example.com",                           # 対応表に行が無い
        }),
    )
    assert metrics.warnings == (
        (
            "シートを持つ人のうち GitHub login に対応づかない人 2 名: "
            "['user3@example.com', 'user4@example.com']（この人の PR は人ごとの行にも"
            "組織全体の件数にも入りません。members-info.csv の GitHub ID 列に login を"
            "書くか、対象の GitHub Organization で開発しない人は「なし」と書いてください）"
        ),
    )


def test_subjects_with_logins_are_not_warned():
    metrics = _pr_metrics(
        _cache(),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", no_account=True),
        ),
        _repos("repo-a"),
        frozenset({"user1@example.com", "user2@example.com"}),
    )
    assert metrics.warnings == ()


# ------------------------------------------------------------- lead time


@pytest.fixture
def local_timezone():
    """ローカル timezone を差し替える（テストの後で必ず元へ戻す）。

    戻し忘れると後続のテストが別の timezone で動くので、環境変数の復元と `tzset` を
    このフィクスチャに閉じる。
    """
    original = os.environ.get("TZ")

    def use(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield use
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_lead_time_is_the_hours_from_creation_to_merge():
    """lead time は merged_at − created_at を時で表した実数（丸めない）。"""
    assert lead_time_hours(
        _pr(created_at="2026-08-01T00:00:00Z", merged_at="2026-08-03T10:30:00Z")
    ) == 58.5


def test_a_pr_merged_at_the_moment_it_was_created_has_no_lead_time():
    assert lead_time_hours(
        _pr(created_at="2026-08-01T00:00:00Z", merged_at="2026-08-01T00:00:00Z")
    ) == 0.0


@pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="time.tzset が無い環境（Windows）"
)
@pytest.mark.parametrize("zone", ["UTC", "Asia/Tokyo", "America/Los_Angeles"])
def test_the_lead_time_does_not_depend_on_the_local_timezone(local_timezone, zone):
    """日時は UTC のまま解釈する（実行する端末の timezone で値が動かない）。"""
    local_timezone(zone)
    assert lead_time_hours(
        _pr(created_at="2026-08-01T00:00:00Z", merged_at="2026-08-03T10:30:00Z")
    ) == 58.5


def test_a_lead_time_can_cross_a_month_boundary():
    """前月に作られた PR も merge した月に数え、lead time は月をまたいだ実時間になる。"""
    metrics = _pr_metrics(
        _cache((
            _pr(number=1, created_at="2026-07-31T23:30:00Z",
                merged_at="2026-08-01T00:30:00Z"),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.month == MONTH
    assert metrics.users[0].lead_time == LeadTimeSummary(
        count=1, median_hours=1.0, p75_hours=1.0, p90_hours=1.0
    )


@pytest.mark.parametrize("is_draft", [True, False])
def test_draft_periods_are_included_in_the_lead_time(is_draft):
    """Draft だった期間も含める（設計書 §15.5）——`is_draft` で式を変えない。"""
    assert lead_time_hours(_pr(is_draft=is_draft)) == 58.5


def test_a_pr_without_a_merge_cannot_be_cached():
    """unmerged の PR は構造で入らない（キャッシュは merge 済みの PR しか持てない）。"""
    with pytest.raises(ValueError, match="merged_at には UTC の日時表記"):
        _pr(merged_at=None)


def test_the_summary_interpolates_between_the_values():
    """百分位は線形補間（numpy / pandas / Excel と同じ type 7）で、median は P50。"""
    assert summarize_lead_times([1.0, 2.0, 3.0, 4.0]) == LeadTimeSummary(
        count=4, median_hours=2.5, p75_hours=3.25, p90_hours=3.7
    )


def test_a_single_value_becomes_all_three_percentiles():
    """1件でも要約する（件数の下限を設けない代わりに count で重みを伝える）。"""
    assert summarize_lead_times([12.5]) == LeadTimeSummary(
        count=1, median_hours=12.5, p75_hours=12.5, p90_hours=12.5
    )


def test_two_values_are_interpolated():
    assert summarize_lead_times([10.0, 20.0]) == LeadTimeSummary(
        count=2, median_hours=15.0, p75_hours=17.5, p90_hours=19.0
    )


def test_the_order_of_the_values_does_not_change_the_summary():
    assert summarize_lead_times([4.0, 1.0, 3.0, 2.0]) == summarize_lead_times(
        [1.0, 2.0, 3.0, 4.0]
    )


def test_without_values_there_is_no_summary():
    assert summarize_lead_times([]) is None


def _hours_pr(number: int, hours: int, **overrides) -> CachedPr:
    """lead time が hours 時間ちょうどの PR（1日の中に収める）。"""
    return _pr(
        number=number,
        created_at="2026-08-02T00:00:00Z",
        merged_at=f"2026-08-02T{hours:02d}:00:00Z",
        **overrides,
    )


def test_the_lead_time_of_a_person_covers_only_their_own_prs():
    metrics = _pr_metrics(
        _cache((
            _hours_pr(1, 1),
            _hours_pr(2, 2),
            _hours_pr(3, 3),
            _hours_pr(4, 20, author_login="example-user"),
        )),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
            _link("user3@example.com", "idle-user"),
        ),
        _repos("repo-a"),
    )
    assert metrics.users[0].merged_pr_count == 3
    assert metrics.users[0].lead_time == LeadTimeSummary(
        count=3, median_hours=2.0, p75_hours=2.5, p90_hours=2.8
    )
    assert metrics.users[1].lead_time == LeadTimeSummary(
        count=1, median_hours=20.0, p75_hours=20.0, p90_hours=20.0
    )
    assert metrics.users[2].lead_time is None   # PR が0件の人は要約を持たない


def test_the_organization_lead_time_covers_only_the_subjects_prs():
    """組織全体の要約は対象者の PR だけ（件数の列に残る他の区分は入れない）。

    対象者でない作成者・削除済みのアカウント・Bot・対象外 repository の PR を桁違いの
    lead time にしておき、どれか1件でも入れば要約が動くことで確かめる。
    """
    metrics = _pr_metrics(
        _cache((
            _hours_pr(1, 1),
            _hours_pr(2, 2),
            _hours_pr(3, 3),
            _hours_pr(4, 20, author_login="ghost-writer"),
            _hours_pr(5, 21, author_login="no-seat-user"),
            _hours_pr(6, 22, author_login=None, author_type=None),
            _pr(number=7, author_login="example-bot[bot]",
                author_type=BOT_AUTHOR_TYPE,
                created_at="2026-08-01T00:00:00Z",
                merged_at="2026-08-25T00:00:00Z"),
            _pr(number=8, repository="repo-b",
                created_at="2026-08-01T00:00:00Z",
                merged_at="2026-08-28T00:00:00Z"),
        )),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "no-seat-user"),
        ),
        _repos("repo-a"),
        frozenset({"user1@example.com"}),
    )
    assert metrics.mapped_prs == 3
    assert metrics.lead_time == LeadTimeSummary(
        count=3, median_hours=2.0, p75_hours=2.5, p90_hours=2.8
    )
    # 外した分は件数の列にだけ残る
    assert (metrics.unmapped_prs, metrics.deleted_author_prs, metrics.bot_prs,
            metrics.excluded_repository_prs) == (2, 1, 1, 1)


def test_the_organization_lead_time_is_the_lead_time_of_the_user_rows():
    """組織全体の要約は、人ごとの行が数えた PR を合わせたものと一致する。"""
    metrics = _pr_metrics(
        _cache((
            _hours_pr(1, 1),
            _hours_pr(2, 4, author_login="example-user"),
            _hours_pr(3, 9, author_login="example-user"),
        )),
        _members(
            _link("user1@example.com", "octocat"),
            _link("user2@example.com", "example-user"),
        ),
        _repos("repo-a"),
    )
    assert metrics.lead_time == summarize_lead_times([1.0, 4.0, 9.0])
    assert metrics.lead_time.count == sum(
        user.merged_pr_count for user in metrics.users
    )


def test_without_subject_prs_there_is_no_organization_lead_time():
    """Bot・対象外 repository・削除済みのアカウントしか無い月は要約を作らない。"""
    metrics = _pr_metrics(
        _cache((
            _pr(number=1, author_login="example-bot[bot]",
                author_type=BOT_AUTHOR_TYPE),
            _pr(number=2, repository="repo-b"),
            _pr(number=3, author_login=None, author_type=None),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert (metrics.bot_prs, metrics.excluded_repository_prs,
            metrics.deleted_author_prs) == (1, 1, 1)
    assert metrics.mapped_prs == 0
    assert metrics.lead_time is None
    assert metrics.users[0].merged_pr_count == 0
    assert metrics.warnings == ()


def test_a_deleted_account_counts_only_in_its_column():
    """削除済みのアカウントの PR は件数の列には入り、lead time には入らない。"""
    metrics = _pr_metrics(
        _cache((
            _hours_pr(1, 2),
            _hours_pr(2, 23, author_login=None, author_type=None),
        )),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.deleted_author_prs == 1
    assert metrics.total_prs == 2
    assert metrics.lead_time == LeadTimeSummary(
        count=1, median_hours=2.0, p75_hours=2.0, p90_hours=2.0
    )


# --------------------------------------------------------------- 警告


@pytest.mark.parametrize("members", [
    _members(source=None, has_column=False),   # ファイルが無い
    _members(has_column=False),                # ファイルはあるが GitHub ID の列が無い
])
def test_without_a_mapping_no_pr_is_attributed(members):
    """対応表が無いと個人へ帰属できない（Bot と削除済み以外はすべて未対応の分）。"""
    metrics = _pr_metrics(
        _cache((
            _pr(number=1, author_login="octocat"),
            _pr(number=2, author_login="example-bot[bot]",
                author_type=BOT_AUTHOR_TYPE),
            _pr(number=3, author_login=None, author_type=None),
        )),
        members,
        _repos("repo-a"),
        frozenset({"user1@example.com"}),
    )
    assert metrics.users == ()
    assert (metrics.unmapped_authors, metrics.unmapped_prs) == (1, 1)
    # 対応表が無いときは「login に対応づかない対象者」を並べない（全員が該当するため）
    assert metrics.warnings == (
        (
            "GitHub ID の対応表がありません（members-info.csv に GitHub ID 列が無い）。"
            "PR を個人に帰属できません"
        ),
        (
            "シートを持つ人の login に当たらない作成者 1 人による PR 1 件を集計から除外"
            "しました（人ごとの行・組織全体の件数とも。対応表に無い人と、対応表にあっても"
            "この組織でシートを持たない人を含みます）"
        ),
    )


def test_a_mapping_with_no_rows_is_still_provided():
    """列がある表は、記入が1行も無くても「対応表がありません」とは言わない。"""
    metrics = _pr_metrics(_cache(), _members(), _repos("repo-a"))
    assert metrics.warnings == ()


def test_an_incomplete_cache_is_flagged_as_partial():
    metrics = _pr_metrics(
        _cache((_pr(number=1),), complete=False),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.cache_complete is False
    assert metrics.warnings == (
        "2026-08 の収集が完了していません。件数は部分的な値です",
    )


def test_a_complete_cache_has_no_partial_warning():
    metrics = _pr_metrics(
        _cache((_pr(number=1),)),
        _members(_link("user1@example.com", "octocat")),
        _repos("repo-a"),
    )
    assert metrics.cache_complete is True
    assert metrics.warnings == ()


def test_warnings_follow_a_fixed_order():
    """対応表なし → 対象者に当たらない作成者 → 収集の未完了 の順に並べる。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="ghost-writer"),), complete=False),
        _members(source=None, has_column=False),
        _repos("repo-a"),
        frozenset({"user1@example.com"}),
    )
    assert len(metrics.warnings) == 3
    assert metrics.warnings[0].startswith("GitHub ID の対応表がありません")
    assert metrics.warnings[1].startswith("シートを持つ人の login に当たらない作成者 1 人")
    assert metrics.warnings[2].startswith("2026-08 の収集が完了していません")


def test_warnings_with_a_mapping_follow_a_fixed_order():
    """対応表があるとき: login に対応づかない対象者 → 対象者に当たらない作成者 → 未完了。"""
    metrics = _pr_metrics(
        _cache((_pr(number=1, author_login="ghost-writer"),), complete=False),
        _members(_link("user1@example.com", "octocat"), _link("user2@example.com")),
        _repos("repo-a"),
        frozenset({"user1@example.com", "user2@example.com"}),
    )
    assert len(metrics.warnings) == 3
    assert metrics.warnings[0].startswith(
        "シートを持つ人のうち GitHub login に対応づかない人 1 名")
    assert metrics.warnings[1].startswith("シートを持つ人の login に当たらない作成者 1 人")
    assert metrics.warnings[2].startswith("2026-08 の収集が完了していません")


def test_mapping_warnings_are_not_copied():
    """対応表の読み取りで出た警告は転記しない（doctor が同じ内容を出すため）。"""
    metrics = _pr_metrics(
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
        _pr_metrics(_cache((_pr(),)), _members(), repos)


def test_a_repository_list_of_another_organization_is_rejected():
    with pytest.raises(ValueError, match="Organization が違います"):
        _pr_metrics(_cache(), _members(), _repos("repo-a", github_org="other-org"))


SUBJECTS = frozenset({"user1@example.com"})


@pytest.mark.parametrize("args,message", [
    (("not a cache", _members(), _repos(), SUBJECTS), "cache には PrCache が必要です"),
    ((_cache(), "not members", _repos(), SUBJECTS),
     "members には GithubMembers が必要です"),
    ((_cache(), _members(), None, SUBJECTS), "repos には RepoDiscovery が必要です"),
    ((_cache(), _members(), _repos(), ["user1@example.com"]),
     "subjects には email（文字列）の frozenset が必要です"),
    ((_cache(), _members(), _repos(), {"user1@example.com"}),
     "subjects には email（文字列）の frozenset が必要です"),
    ((_cache(), _members(), _repos(), frozenset({1})),
     "subjects には email（文字列）の frozenset が必要です"),
    ((_cache(), _members(), _repos(), None),
     "subjects には email（文字列）の frozenset が必要です"),
])
def test_wrong_argument_types_are_rejected(args, message):
    with pytest.raises(TypeError, match=message):
        pr_metrics(*args)


def test_subjects_are_required():
    """対象者を省いて呼べない（対応表全体へ黙って戻さない）。"""
    with pytest.raises(TypeError):
        pr_metrics(_cache(), _members(), _repos())   # type: ignore[call-arg]


def test_an_empty_subject_email_is_rejected():
    with pytest.raises(ValueError, match="email は必須です"):
        pr_metrics(_cache(), _members(), _repos(), frozenset({"  "}))


# ------------------------------------------------------------------ 決定性


def _inputs() -> tuple[PrCache, GithubMembers, RepoDiscovery, frozenset[str]]:
    return (
        # lead time は PR ごとに変える（同じ値ばかりだと要約の並びの違いが見えない）
        _cache((
            _hours_pr(1, 1, author_login="octocat"),
            _hours_pr(2, 5, author_login="ghost-writer"),
            _hours_pr(3, 9, repository="repo-b", author_login="example-user"),
            _hours_pr(4, 13, author_login=None, author_type=None),
            _hours_pr(5, 17, author_login="example-user"),
            _hours_pr(6, 21, author_login="no-seat-user"),
        )),
        _members(
            _link("user2@example.com", "example-user"),
            _link("user1@example.com", "octocat"),
            _link("user3@example.com", "no-seat-user"),
            _link("user5@example.com"),
            _link("user4@example.com"),
        ),
        _repos("repo-a"),
        frozenset({
            "user1@example.com", "user2@example.com", "user4@example.com",
            "user5@example.com",
        }),
    )


def test_the_same_input_gives_the_same_result():
    cache, members, repos, subjects = _inputs()
    assert pr_metrics(cache, members, repos, subjects) == pr_metrics(
        cache, members, repos, subjects
    )


def test_the_row_order_of_the_mapping_does_not_change_the_result():
    """対応表の行順は結果に影響しない（PR の並びはキャッシュの不変条件で固定済み）。"""
    cache, members, repos, subjects = _inputs()
    reversed_members = _members(*reversed(members.entries))
    assert pr_metrics(cache, reversed_members, repos, subjects) == pr_metrics(
        cache, members, repos, subjects
    )


def test_the_way_the_subjects_were_built_does_not_change_the_result():
    """対象者の集合の作り方（挿入の順）で結果と警告の並びが変わらない。"""
    cache, members, repos, subjects = _inputs()
    forward = frozenset(sorted(subjects))
    backward = frozenset(sorted(subjects, reverse=True))
    assert pr_metrics(cache, members, repos, forward) == pr_metrics(
        cache, members, repos, backward
    )
    # login に対応づかない対象者は email の昇順で並べる
    assert "['user4@example.com', 'user5@example.com']" in pr_metrics(
        cache, members, repos, backward
    ).warnings[0]


def test_every_pr_falls_into_exactly_one_bucket():
    """区分の合計は常に全件数（値オブジェクトの検査を結果の側でも確かめる）。"""
    metrics = pr_metrics(*_inputs())
    assert (
        metrics.mapped_prs,
        metrics.unmapped_prs,
        metrics.bot_prs,
        metrics.deleted_author_prs,
        metrics.excluded_repository_prs,
    ) == (2, 2, 0, 1, 1)
    assert metrics.total_prs == 6


# ------------------------------------------------------------ 値オブジェクト


def _summary(count: int = 1, hours: float = 1.0) -> LeadTimeSummary:
    """count 件ぶんの要約（3つの値そのものが検査の対象でない箇所で使う）。"""
    return LeadTimeSummary(
        count=count, median_hours=hours, p75_hours=hours, p90_hours=hours
    )


def test_lead_time_summary_keeps_its_values_as_floats():
    """整数で渡した時間も float に揃える（出力の書式を型で揺らさない）。"""
    summary = LeadTimeSummary(count=1, median_hours=2, p75_hours=2, p90_hours=2)
    hours = (summary.median_hours, summary.p75_hours, summary.p90_hours)
    assert hours == (2.0, 2.0, 2.0)
    assert all(isinstance(value, float) for value in hours)


@pytest.mark.parametrize("overrides,message", [
    ({"count": 0}, "count には1以上の件数が必要です"),
    ({"count": -1}, "count には1以上の件数が必要です"),
    ({"count": 1.0}, "count には1以上の件数が必要です"),
    ({"count": True}, "count には1以上の件数が必要です"),
    ({"median_hours": -0.5}, "median_hours には0以上の有限な時間"),
    ({"median_hours": float("nan")}, "median_hours には0以上の有限な時間"),
    ({"p90_hours": float("inf")}, "p90_hours には0以上の有限な時間"),
    ({"median_hours": 5.0}, "百分位は median ≤ P75 ≤ P90 の順"),
    ({"p75_hours": 9.0, "p90_hours": 2.0}, "百分位は median ≤ P75 ≤ P90 の順"),
])
def test_lead_time_summary_rejects_inconsistent_values(overrides, message):
    values = {"count": 1, "median_hours": 1.0, "p75_hours": 1.0, "p90_hours": 1.0}
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        LeadTimeSummary(**values)


@pytest.mark.parametrize("overrides,message", [
    ({"median_hours": "1.0"}, "median_hours には時間（float）が必要です"),
    ({"p75_hours": None}, "p75_hours には時間（float）が必要です"),
    ({"p90_hours": True}, "p90_hours には時間（float）が必要です"),
])
def test_lead_time_summary_rejects_wrong_types(overrides, message):
    values = {"count": 1, "median_hours": 1.0, "p75_hours": 1.0, "p90_hours": 1.0}
    values.update(overrides)
    with pytest.raises(TypeError, match=message):
        LeadTimeSummary(**values)


def test_user_metrics_normalizes_and_validates_its_values():
    """email は対応表と同じ規則で正規化し、読めない login は受け付けない。"""
    user = UserPrMetrics(
        email=" User1@Example.com ",
        github_login="octocat",
        merged_pr_count=0,
        lead_time=None,
    )
    assert user.email == "user1@example.com"
    with pytest.raises(ValueError, match="email は必須です"):
        UserPrMetrics(
            email="  ", github_login="octocat", merged_pr_count=0, lead_time=None
        )
    with pytest.raises(ValueError, match="github_login として読めない値です"):
        UserPrMetrics(
            email="user1@example.com",
            github_login="@octocat",
            merged_pr_count=0,
            lead_time=None,
        )
    with pytest.raises(ValueError, match="github_login は必須です"):
        UserPrMetrics(
            email="user1@example.com",
            github_login=None,
            merged_pr_count=0,
            lead_time=None,
        )


@pytest.mark.parametrize("count", [-1, True, 1.0, "1"])
def test_user_metrics_rejects_a_count_that_is_not_a_number_of_prs(count):
    with pytest.raises(ValueError, match="merged_pr_count には0以上の件数"):
        UserPrMetrics(
            email="user1@example.com",
            github_login="octocat",
            merged_pr_count=count,
            lead_time=None,
        )


@pytest.mark.parametrize("count,lead_time,message", [
    (0, _summary(1), "merged_pr_count が0の人だけ lead_time を None"),
    (2, None, "merged_pr_count が0の人だけ lead_time を None"),
    (2, _summary(3), "lead_time の件数が merged_pr_count と一致しません"),
])
def test_user_metrics_keeps_the_count_and_the_summary_together(
    count, lead_time, message
):
    """件数と要約は同じ PR の集合から出る（片方だけが立つ行を作らせない）。"""
    with pytest.raises(ValueError, match=message):
        UserPrMetrics(
            email="user1@example.com",
            github_login="octocat",
            merged_pr_count=count,
            lead_time=lead_time,
        )


def test_user_metrics_rejects_a_lead_time_of_another_type():
    with pytest.raises(TypeError, match="lead_time には LeadTimeSummary か None"):
        UserPrMetrics(
            email="user1@example.com",
            github_login="octocat",
            merged_pr_count=1,
            lead_time=1.0,
        )


def _user(email: str, login: str, count: int = 0,
          lead_time: LeadTimeSummary | None = None) -> UserPrMetrics:
    """users の1行（件数が正なら要約も件数に合わせて作る）。"""
    if count and lead_time is None:
        lead_time = _summary(count)
    return UserPrMetrics(
        email=email, github_login=login, merged_pr_count=count, lead_time=lead_time
    )


def _metrics(**overrides) -> GithubMetrics:
    values = {"github_org": ORG, "month": MONTH}
    values.update(overrides)
    return GithubMetrics(**values)


def test_metrics_accepts_a_consistent_result():
    metrics = _metrics(
        users=(_user("user1@example.com", "octocat", 2),),
        lead_time=_summary(2),      # 対象者の PR だけを要約する
        unmapped_authors=1,
        unmapped_prs=1,
        bot_prs=1,
        deleted_author_prs=1,
        excluded_repository_prs=1,
        total_prs=6,
        cache_complete=True,
    )
    assert metrics.mapped_prs == 2
    assert not hasattr(metrics, "human_prs")


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
        "対象者に当たらない作成者は人数と件数の両方を持たせてください",
    ),
    (
        {"unmapped_prs": 1, "total_prs": 1},
        "対象者に当たらない作成者は人数と件数の両方を持たせてください",
    ),
    (
        {"unmapped_authors": 2, "unmapped_prs": 1, "total_prs": 1},
        "対象者に当たらない作成者の人数が PR の件数を超えています",
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
    (
        {"lead_time": _summary(1)},
        "対象者の PR が1件も無いときだけ lead_time を None",
    ),
    (
        # 対象者の PR が無い月に、他の区分の PR で要約を作らせない
        {"unmapped_authors": 1, "unmapped_prs": 1, "deleted_author_prs": 1,
         "total_prs": 2, "lead_time": _summary(2)},
        "対象者の PR が1件も無いときだけ lead_time を None",
    ),
    (
        {"users": (_user("user1@example.com", "octocat", 2),), "total_prs": 2},
        "対象者の PR が1件も無いときだけ lead_time を None",
    ),
    (
        {"users": (_user("user1@example.com", "octocat", 2),), "total_prs": 2,
         "lead_time": _summary(3)},
        "lead_time の件数が対象者の PR の件数と一致しません",
    ),
    (
        # 対象者に当たらない作成者・削除済みの分まで要約に入れた結果は受けない
        {"users": (_user("user1@example.com", "octocat", 2),),
         "unmapped_authors": 1, "unmapped_prs": 1, "deleted_author_prs": 1,
         "total_prs": 4, "lead_time": _summary(4)},
        "lead_time の件数が対象者の PR の件数と一致しません",
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
    ({"lead_time": 1.0}, "lead_time には LeadTimeSummary か None が必要です"),
    ({"warnings": ["注意"]}, "warnings には文字列の tuple が必要です"),
    ({"warnings": (1,)}, "warnings には文字列の tuple が必要です"),
])
def test_metrics_rejects_wrong_types(overrides, message):
    with pytest.raises(TypeError, match=message):
        _metrics(**overrides)
