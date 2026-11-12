"""人 × 月の表の1か月分の組み立て（adoption.person_rows。設計書 §27.8）のテスト。

入力は合成データだけで組み、`analyze.analyze_org` の結果をそのまま渡して確かめる。
GitHub の材料は値オブジェクト（GithubMembers・GithubMetrics・CommitMetrics）を直接組む。
"""

import dataclasses
from pathlib import Path

import pytest
import yaml

from seat_analyzer import adoption, cli
from seat_analyzer.adoption import (
    GITHUB_NOT_CONFIGURED,
    GithubData,
    GithubId,
    GithubMonth,
    PersonMonth,
    github_data_state,
    person_rows,
    seat_holders,
)
from seat_analyzer.analyze import analyze_org
from seat_analyzer.config import load_config
from seat_analyzer.github_collect import GithubMemberLink, GithubMembers
from seat_analyzer.github_metrics import (
    CommitMetrics,
    CommitTotals,
    GithubMetrics,
    LeadTimeSummary,
    UserCommitMetrics,
    UserPrMetrics,
)

from .conftest import CONFIG, SPEND_HEADER, spend_row

ORG = "org-a"
NESTED = "org-x"
GH_ORG = "example-org"
MONTH = "2026-06"

# 列ごとの空欄の顔ぶれ
_PR_COLUMNS = ("merged_pr_count", "lead_time_median_hours")
_COMMIT_COLUMNS = ("commit_count", "lines_added_capped", "large_commits")


def _single(make_input) -> Path:
    """単一スペースの組織。シートを持つ人3名と、行にならない人2名。

    bea は Code と Chat、cai は Code だけ、dan は spend に居ない。una は未割当、
    kit は members に居ない（シート不明）。
    """
    return make_input(
        {MONTH: [
            spend_row("bea@x.jp", 40.0), spend_row("bea@x.jp", 10.0, product="Chat"),
            spend_row("cai@x.jp", 5.0), spend_row("kit@x.jp", 3.0),
            spend_row("una@x.jp", 2.0),
        ]},
        members=["bea@x.jp,Premium", "cai@x.jp,Standard", "dan@x.jp,Standard",
                 "una@x.jp,Unassigned"],
        org=ORG,
    )


def _analyze(input_dir: Path, cfg: dict | None = None, org: str = ORG, month: str = MONTH):
    """組織の入力を分析した結果（既定の設定）。"""
    return analyze_org(input_dir / org, month, cfg or load_config(CONFIG), org)


def _write_code(base: Path, rows: list[tuple[str, int]], month: str = MONTH) -> None:
    """code-analytics（月のみの命名）を置く。"""
    directory = base / "code-analytics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"cc_{month}.csv").write_text(
        "Email,Lines with CC\n" + "".join(f"{email},{loc}\n" for email, loc in rows),
        encoding="utf-8", newline="\n",
    )


def _nested_cfg(tmp_path: Path) -> dict:
    """2 workspace（main が主・second は Premium 固定）の組織の設定。"""
    path = tmp_path / "nested.yaml"
    path.write_text(yaml.safe_dump({"organizations": {NESTED: {"workspaces": {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    }}}}), encoding="utf-8", newline="\n")
    return load_config(str(path))


def _nested(make_input) -> Path:
    """amy は主で Standard・副で Premium の2アカウント、bo は主だけ、cy は副だけ。"""
    input_dir = make_input(
        {MONTH: [spend_row("amy@x.jp", 30.0), spend_row("bo@x.jp", 7.0)]},
        members=["amy@x.jp,Standard", "bo@x.jp,Premium"], org=NESTED, workspace="main",
    )
    make_input(
        {MONTH: [spend_row("amy@x.jp", 50.0), spend_row("cy@x.jp", 4.0)]},
        members=["amy@x.jp,Premium", "cy@x.jp,Premium"], org=NESTED, workspace="second",
    )
    return input_dir


def _by_email(rows) -> dict[str, PersonMonth]:
    """行を email → 行の辞書にする（並びは行の順のまま）。"""
    return {row.email: row for row in rows}


# ------------------------------------------------------------------ 行の顔ぶれ


def test_rows_are_seat_holders_in_email_order(make_input):
    """行はシートを持つ人だけで、メール昇順（未割当・members に居ない人は行にしない）。"""
    rows = person_rows(_analyze(_single(make_input)), GITHUB_NOT_CONFIGURED)
    assert [(row.email, row.seat) for row in rows] == [
        ("bea@x.jp", "premium"), ("cai@x.jp", "standard"), ("dan@x.jp", "standard"),
    ]
    assert all(row.month == MONTH for row in rows)


def test_demand_and_loc_match_the_features_and_users(make_input):
    """需要・リクエスト数は product_usage.features、LoC は users の値と一致する。"""
    input_dir = _single(make_input)
    _write_code(input_dir / ORG, [("bea@x.jp", 120), ("cai@x.jp", 7)])
    result = _analyze(input_dir)
    workspace = result.workspaces[ORG]
    features = workspace.product_usage.features
    users = workspace.users.set_index("email")
    rows = _by_email(person_rows(result, GITHUB_NOT_CONFIGURED))
    for email in ("bea@x.jp", "cai@x.jp"):
        row = rows[email]
        for name in ("total_demand_usd", "code_demand_usd", "code_requests"):
            assert getattr(row, name) == float(features.at[email, name])
        assert row.loc_with_cc == int(users.at[email, "loc_with_cc"])
    # 全 product の需要は Chat を含み、Claude Code の需要は含まない
    assert rows["bea@x.jp"].total_demand_usd > rows["bea@x.jp"].code_demand_usd


def test_a_seat_holder_without_spend_has_zero_demand(make_input):
    """spend に行の無いシート保有者は、需要0・リクエスト数0（空欄にしない）。"""
    row = _by_email(person_rows(_analyze(_single(make_input)), GITHUB_NOT_CONFIGURED))[
        "dan@x.jp"]
    assert (row.total_demand_usd, row.code_demand_usd, row.code_requests) == (0.0, 0.0, 0.0)


def test_spend_without_product_column_leaves_code_values_blank(make_input):
    """product の列が無い spend では Code の需要とリクエスト数が None、全体の需要は値を持つ。"""
    input_dir = _single(make_input)
    spend = input_dir / ORG / "spend" / f"spend_{MONTH}.csv"
    header = SPEND_HEADER.replace("Product,", "")
    lines = [spend_row("bea@x.jp", 40.0).split(","), spend_row("cai@x.jp", 5.0).split(",")]
    spend.write_text(
        header + "\n" + "".join(",".join(f[:2] + f[3:]) + "\n" for f in lines),
        encoding="utf-8", newline="\n",
    )
    row = _by_email(person_rows(_analyze(input_dir), GITHUB_NOT_CONFIGURED))["bea@x.jp"]
    assert row.total_demand_usd == pytest.approx(40.0)
    assert row.code_demand_usd is None
    assert row.code_requests is None


def test_loc_is_none_without_code_analytics_and_zero_without_a_row(make_input):
    """code-analytics の無い月は LoC が None、ファイルがあって行の無い人は0。"""
    input_dir = _single(make_input)
    rows = person_rows(_analyze(input_dir), GITHUB_NOT_CONFIGURED)
    assert all(row.loc_with_cc is None for row in rows)
    _write_code(input_dir / ORG, [("bea@x.jp", 120)])
    rows = _by_email(person_rows(_analyze(input_dir), GITHUB_NOT_CONFIGURED))
    assert rows["bea@x.jp"].loc_with_cc == 120
    assert rows["dan@x.jp"].loc_with_cc == 0


def test_nested_org_sums_accounts_in_one_row(make_input, tmp_path):
    """2アカウントの人は1行で、需要・リクエスト数・LoC が和、seat は主が先頭。

    主の users の需要は2アカウントの合算に置き換わっているので、それを足した値には
    ならない（二重に数えない）。片方の workspace だけ LoC の列がある月は、ある方の和。
    """
    input_dir = _nested(make_input)
    cfg = _nested_cfg(tmp_path)
    _write_code(input_dir / NESTED / "main", [("amy@x.jp", 30), ("bo@x.jp", 5)])
    result = _analyze(input_dir, cfg, NESTED)
    rows = _by_email(person_rows(result, GITHUB_NOT_CONFIGURED))
    assert [(row.email, row.seat) for row in rows.values()] == [
        ("amy@x.jp", "standard+premium"), ("bo@x.jp", "premium"), ("cy@x.jp", "premium"),
    ]
    main, second = (result.workspaces[name] for name in ("main", "second"))
    amy = rows["amy@x.jp"]
    for name in ("total_demand_usd", "code_demand_usd", "code_requests"):
        assert getattr(amy, name) == (
            float(main.product_usage.features.at["amy@x.jp", name])
            + float(second.product_usage.features.at["amy@x.jp", name])
        )
    doubled = (
        float(main.users.set_index("email").at["amy@x.jp", "api_cost_usd"])
        + float(second.users.set_index("email").at["amy@x.jp", "api_cost_usd"])
    )
    assert amy.total_demand_usd == pytest.approx(80.0)
    assert amy.total_demand_usd != pytest.approx(doubled)
    assert amy.loc_with_cc == 30
    assert rows["cy@x.jp"].loc_with_cc == 0

    _write_code(input_dir / NESTED / "second", [("amy@x.jp", 12), ("cy@x.jp", 4)])
    rows = _by_email(person_rows(_analyze(input_dir, cfg, NESTED), GITHUB_NOT_CONFIGURED))
    assert (rows["amy@x.jp"].loc_with_cc, rows["cy@x.jp"].loc_with_cc) == (42, 4)


def test_seat_holders_are_the_github_subjects(make_input, tmp_path, monkeypatch):
    """シートを持つ人の判定は GitHub の参考値の対象者と同じ関数で決まる。"""
    result = _analyze(_nested(make_input), _nested_cfg(tmp_path), NESTED)
    assert seat_holders(result) == {
        "amy@x.jp": ("standard", "premium"), "bo@x.jp": ("premium",), "cy@x.jp": ("premium",),
    }
    assert cli._github_subjects(result) == frozenset(seat_holders(result))
    monkeypatch.setattr(adoption, "seat_holders", lambda _: {"only@x.jp": ("premium",)})
    assert cli._github_subjects(result) == frozenset({"only@x.jp"})


# ---------------------------------------------------------------- GitHub の列


def _members() -> GithubMembers:
    """bea は login・cai は「なし」・dan は対応表に行が無い。"""
    return GithubMembers(
        entries=(
            GithubMemberLink(email="bea@x.jp", github_login="bea-dev"),
            GithubMemberLink(email="cai@x.jp", github_login=None, no_account=True),
        ),
        source="members-info.csv", has_column=True, warnings=(),
    )


def _metrics(complete: bool = True, prs: int = 2) -> GithubMetrics:
    """bea の PR（prs 件）だけを持つ PR の集計。"""
    lead = None if prs == 0 else LeadTimeSummary(
        count=prs, median_hours=1.5, p75_hours=2.0, p90_hours=2.5)
    return GithubMetrics(
        github_org=GH_ORG, month=MONTH,
        users=(UserPrMetrics(email="bea@x.jp", github_login="bea-dev",
                             merged_pr_count=prs, lead_time=lead),),
        lead_time=lead, total_prs=prs, cache_complete=complete,
    )


def _commits() -> CommitMetrics:
    """bea のコミット3件（うち大きな変更1件）の集計。"""
    return CommitMetrics(
        github_org=GH_ORG, month=MONTH,
        users=(UserCommitMetrics(email="bea@x.jp", totals=CommitTotals(
            commit_count=3, lines_added=50, lines_removed=4, lines_added_capped=20,
            large_commits=1)),),
        unresolved_author_commits=0, counted_commits=3,
    )


def _github(state: GithubData, complete: bool = True) -> GithubMonth:
    """状態ごとに、持ってよい集計だけを載せた材料。"""
    if state is GithubData.NOT_CONFIGURED:
        return GITHUB_NOT_CONFIGURED
    pr = state in (GithubData.NO_COMMITS, GithubData.NO_PREVIOUS_MONTH, GithubData.OK)
    return GithubMonth(
        state=state, members=_members(),
        metrics=_metrics(complete) if pr else None,
        commits=_commits() if state is GithubData.OK else None,
        cache_complete=None if state is GithubData.NO_CACHE else complete,
    )


# 状態 → (PR の列を書くか, コミットの列を書くか, キャッシュの完了の値を持つか)
_STATE_COLUMNS = {
    GithubData.NOT_CONFIGURED: (False, False, False),
    GithubData.NO_CACHE: (False, False, False),
    GithubData.NO_REPOSITORY_LIST: (False, False, True),
    GithubData.NO_COMMITS: (True, False, True),
    GithubData.NO_PREVIOUS_MONTH: (True, False, True),
    GithubData.OK: (True, True, True),
}


@pytest.mark.parametrize("state", list(GithubData))
def test_each_github_state_blanks_the_specified_columns(make_input, state):
    """github_data の6つの状態のそれぞれで、設計書の表のとおりの列だけが None になる。"""
    result = _analyze(_single(make_input))
    bea = _by_email(person_rows(result, _github(state)))["bea@x.jp"]
    pr, commit, cache = _STATE_COLUMNS[state]
    assert bea.github_data is state
    assert all((getattr(bea, name) is not None) is pr for name in _PR_COLUMNS)
    assert all((getattr(bea, name) is not None) is commit for name in _COMMIT_COLUMNS)
    assert (bea.github_cache_complete is not None) is cache
    if pr:
        assert (bea.merged_pr_count, bea.lead_time_median_hours) == (2, 1.5)
    if commit:
        assert (bea.commit_count, bea.lines_added_capped, bea.large_commits) == (3, 20, 1)
    expected_id = None if state is GithubData.NOT_CONFIGURED else GithubId.LOGIN
    assert bea.github_id is expected_id


@pytest.mark.parametrize(("flags", "expected"), [
    ({"configured": False}, GithubData.NOT_CONFIGURED),
    ({"has_cache": False}, GithubData.NO_CACHE),
    ({"has_repository_list": False}, GithubData.NO_REPOSITORY_LIST),
    ({"has_commits": False}, GithubData.NO_COMMITS),
    ({"previous_readable": False}, GithubData.NO_PREVIOUS_MONTH),
    ({"previous_readable": False, "lookback_months": 0}, GithubData.OK),
    ({}, GithubData.OK),
    # 上の条件が先に当てはまる（表の上から順）
    ({"has_cache": False, "has_commits": False}, GithubData.NO_CACHE),
    ({"has_commits": False, "previous_readable": False}, GithubData.NO_COMMITS),
])
def test_github_data_state_follows_the_table_order(flags, expected):
    """状態は表の上から順に最初に当てはまるもので、月数0なら直前の月を問わない。"""
    materials = {
        "configured": True, "has_cache": True, "has_repository_list": True,
        "has_commits": True, "previous_readable": True, "lookback_months": 3, **flags,
    }
    assert github_data_state(**materials) is expected


def test_incomplete_cache_month_keeps_its_state(make_input):
    """収集が未完了の月は github_cache_complete が False で、状態はほかの条件で決まる。"""
    result = _analyze(_single(make_input))
    for state in (GithubData.OK, GithubData.NO_PREVIOUS_MONTH, GithubData.NO_REPOSITORY_LIST):
        bea = _by_email(person_rows(result, _github(state, complete=False)))["bea@x.jp"]
        assert (bea.github_data, bea.github_cache_complete) == (state, False)
    bea = _by_email(person_rows(result, _github(GithubData.NO_CACHE)))["bea@x.jp"]
    assert bea.github_cache_complete is None


def test_github_columns_are_only_for_login_people(make_input):
    """github_id は login・none・unset。login でない人は状態が ok でも GitHub の列が None。"""
    rows = _by_email(person_rows(_analyze(_single(make_input)), _github(GithubData.OK)))
    assert [rows[email].github_id for email in rows] == [
        GithubId.LOGIN, GithubId.NONE, GithubId.UNSET]
    for email in ("cai@x.jp", "dan@x.jp"):
        assert all(getattr(rows[email], name) is None
                   for name in (*_PR_COLUMNS, *_COMMIT_COLUMNS))
        assert rows[email].github_cache_complete is True


def test_a_login_person_without_prs_has_no_lead_time(make_input):
    """PR が0件の login の人は、件数0で lead time が None。"""
    github = GithubMonth(
        state=GithubData.NO_COMMITS, members=_members(), metrics=_metrics(prs=0),
        commits=None, cache_complete=True,
    )
    bea = _by_email(person_rows(_analyze(_single(make_input)), github))["bea@x.jp"]
    assert (bea.merged_pr_count, bea.lead_time_median_hours) == (0, None)


# ------------------------------------------------------------------ 比率・完全性


def _row(**changes) -> PersonMonth:
    """login の人の ok の月の1行（changes で列を差し替える）。"""
    base = PersonMonth(
        email="bea@x.jp", month=MONTH, seat="premium", department="", team="", role="",
        spend_complete=True, total_demand_usd=50.0, code_demand_usd=40.0,
        code_requests=10.0, loc_with_cc=None, github_id=GithubId.LOGIN,
        github_data=GithubData.OK, github_cache_complete=True, merged_pr_count=2,
        lead_time_median_hours=1.5, commit_count=3, lines_added_capped=20, large_commits=1,
    )
    return dataclasses.replace(base, **changes)


def test_cost_per_merged_line():
    """比率は需要 ÷ 上限つきの行で、分母0・どちらかが None なら None。"""
    assert _row().cost_per_merged_line_usd == pytest.approx(2.0)
    assert _row(lines_added_capped=0).cost_per_merged_line_usd is None
    assert _row(code_demand_usd=None).cost_per_merged_line_usd is None
    blank = _row(github_data=GithubData.NO_PREVIOUS_MONTH, commit_count=None,
                 lines_added_capped=None, large_commits=None)
    assert blank.cost_per_merged_line_usd is None


def _write_partial(base: Path, rows: list[str]) -> None:
    """月の前半だけの spend（ファイル名の期間が全月に届かない）に置き換える。"""
    spend = base / "spend"
    for path in spend.glob(f"*{MONTH}*"):
        path.unlink()
    (spend / f"spend-report-uuid-{MONTH}-01-to-{MONTH}-15.csv").write_text(
        SPEND_HEADER + "\n" + "".join(f"{row}\n" for row in rows),
        encoding="utf-8", newline="\n",
    )


def test_spend_complete_is_false_for_a_partial_month(make_input, tmp_path):
    """部分月は False。複数スペースで片方だけ部分月でも False。"""
    input_dir = _single(make_input)
    assert all(row.spend_complete for row in person_rows(_analyze(input_dir),
                                                         GITHUB_NOT_CONFIGURED))
    _write_partial(input_dir / ORG, [spend_row("bea@x.jp", 20.0)])
    assert not any(row.spend_complete for row in person_rows(_analyze(input_dir),
                                                             GITHUB_NOT_CONFIGURED))

    nested = _nested(make_input)
    cfg = _nested_cfg(tmp_path)
    assert all(row.spend_complete
               for row in person_rows(_analyze(nested, cfg, NESTED), GITHUB_NOT_CONFIGURED))
    _write_partial(nested / NESTED / "second", [spend_row("amy@x.jp", 25.0)])
    assert not any(row.spend_complete
                   for row in person_rows(_analyze(nested, cfg, NESTED), GITHUB_NOT_CONFIGURED))


# ------------------------------------------------------------------ 不変条件


@pytest.mark.parametrize(("state", "changes"), [
    (GithubData.NO_COMMITS, {"commits": _commits()}),
    (GithubData.NO_PREVIOUS_MONTH, {"commits": _commits()}),
    (GithubData.OK, {"metrics": None}),
    (GithubData.NO_CACHE, {"cache_complete": True}),
    (GithubData.NO_REPOSITORY_LIST, {"cache_complete": None}),
    (GithubData.NO_REPOSITORY_LIST, {"metrics": _metrics()}),
    (GithubData.OK, {"members": None}),
    (GithubData.OK, {"cache_complete": False}),  # PR の集計の完了と食い違う
])
def test_github_month_rejects_columns_its_state_does_not_write(state, changes):
    """状態と持てる集計の組み合わせが食い違う材料は作れない（ValueError）。"""
    with pytest.raises(ValueError):
        dataclasses.replace(_github(state), **changes)


def test_github_month_and_rows_reject_wrong_types():
    """型の誤りは TypeError（状態の文字列・真偽値でない完全性・数でない需要）。"""
    with pytest.raises(TypeError):
        GithubMonth(state="ok", members=_members(), metrics=_metrics(),
                    commits=_commits(), cache_complete=True)
    with pytest.raises(TypeError):
        dataclasses.replace(_github(GithubData.NO_CACHE), cache_complete=None,
                            members="members-info.csv")
    with pytest.raises(TypeError):
        _row(spend_complete="True")
    with pytest.raises(TypeError):
        _row(total_demand_usd="40")
    with pytest.raises(TypeError):
        _row(merged_pr_count=True)
    with pytest.raises(TypeError):
        _row(github_data="ok")
    with pytest.raises(TypeError):
        github_data_state(configured=1, has_cache=True, has_repository_list=True,
                          has_commits=True, previous_readable=True, lookback_months=3)


@pytest.mark.parametrize("changes", [
    {"seat": "gold"},
    {"seat": ""},
    {"email": ""},
    {"month": "2026-13"},
    {"total_demand_usd": float("nan")},
    {"loc_with_cc": -1},
    # login でない人・コミットを書かない状態に GitHub の値
    {"github_id": GithubId.UNSET},
    {"github_data": GithubData.NO_COMMITS},
    # GitHub 分析の無い組織に対応表の状態
    {"github_data": GithubData.NOT_CONFIGURED, "github_cache_complete": None,
     "merged_pr_count": None, "lead_time_median_hours": None, "commit_count": None,
     "lines_added_capped": None, "large_commits": None},
    # PR の件数と lead time の食い違い
    {"merged_pr_count": 0},
    {"lead_time_median_hours": None},
    # キャッシュの無い月に完了の値
    {"github_data": GithubData.NO_CACHE, "merged_pr_count": None,
     "lead_time_median_hours": None, "commit_count": None, "lines_added_capped": None,
     "large_commits": None},
])
def test_person_month_rejects_wrong_values(changes):
    """値の誤り（不明なシート・空の email・NaN・負の行数・状態と食い違う列）は ValueError。"""
    with pytest.raises(ValueError):
        _row(**changes)


def test_github_data_state_rejects_a_negative_lookback():
    """設定の月数が負なら ValueError。"""
    with pytest.raises(ValueError):
        github_data_state(configured=True, has_cache=True, has_repository_list=True,
                          has_commits=True, previous_readable=True, lookback_months=-1)


def test_rows_reject_metrics_of_another_month(make_input):
    """分析の月と違う月の集計を渡すと ValueError。"""
    result = _analyze(_single(make_input))
    other = dataclasses.replace(_metrics(), month="2026-05")
    github = GithubMonth(state=GithubData.NO_COMMITS, members=_members(), metrics=other,
                         commits=None, cache_complete=True)
    with pytest.raises(ValueError):
        person_rows(result, github)


def test_same_input_gives_the_same_rows(make_input, tmp_path):
    """同じ入力からは同じ行・同じ並びになる。"""
    input_dir = _single(make_input)
    first = person_rows(_analyze(input_dir), _github(GithubData.OK))
    second = person_rows(_analyze(input_dir), _github(GithubData.OK))
    assert first == second
    nested = _nested(make_input)
    cfg = _nested_cfg(tmp_path)
    assert person_rows(_analyze(nested, cfg, NESTED), GITHUB_NOT_CONFIGURED) == \
        person_rows(_analyze(nested, cfg, NESTED), GITHUB_NOT_CONFIGURED)
