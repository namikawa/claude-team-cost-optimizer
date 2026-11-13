"""adoption-people.csv・adoption-teams.csv の書き出し（列の順・書式・エスケープ・並び）のテスト。"""

import csv
import dataclasses
import io
from pathlib import Path

import pytest

from seat_analyzer.adoption import (
    GithubData,
    GithubId,
    GroupMonth,
    GroupScope,
    MonthInputs,
    PersonMonth,
    group_rows,
)
from seat_analyzer.report import (
    ADOPTION_PEOPLE_COLUMNS,
    ADOPTION_TEAMS_COLUMNS,
    write_adoption_people,
    write_adoption_teams,
)

MONTH = "2026-06"


def _row(**changes) -> PersonMonth:
    """login の人の ok の月の1行（changes で列を差し替える）。"""
    base = PersonMonth(
        email="bea@x.jp", month=MONTH, seat="standard+premium", department="架空開発部",
        team="架空チーム", role="エンジニア", spend_complete=True,
        total_demand_usd=123.456, code_demand_usd=100.0, code_requests=12.0,
        loc_with_cc=345, github_id=GithubId.LOGIN, github_data=GithubData.OK,
        github_cache_complete=False, merged_pr_count=4, lead_time_median_hours=2.25,
        commit_count=6, lines_added_capped=300, large_commits=1,
    )
    return dataclasses.replace(base, **changes)


def _blank(**changes) -> PersonMonth:
    """GitHub 分析の無い組織の、値の確定しない1行。"""
    return _row(
        github_id=None, github_data=GithubData.NOT_CONFIGURED, github_cache_complete=None,
        merged_pr_count=None, lead_time_median_hours=None, commit_count=None,
        lines_added_capped=None, large_commits=None, code_demand_usd=None,
        code_requests=None, loc_with_cc=None, **changes,
    )


def _write(tmp_path: Path, rows) -> Path:
    path = tmp_path / "adoption-people.csv"
    write_adoption_people(rows, path)
    return path


def _read(path: Path) -> list[dict[str, str]]:
    """BOM を除いて行ごとの辞書で読む。"""
    text = path.read_bytes().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_columns_follow_the_design_order(tmp_path):
    """列は設計書 §27.8 の表の順。"""
    path = _write(tmp_path, [_row()])
    header = path.read_bytes().decode("utf-8-sig").splitlines()[0]
    assert header.split(",") == list(ADOPTION_PEOPLE_COLUMNS)
    assert ADOPTION_PEOPLE_COLUMNS == (
        "email", "month", "seat", "department", "team", "role", "spend_complete",
        "total_demand_usd", "code_demand_usd", "code_requests", "loc_with_cc",
        "github_id", "github_data", "github_cache_complete", "merged_pr_count",
        "lead_time_median_hours", "commit_count", "lines_added_capped", "large_commits",
        "cost_per_merged_line_usd",
    )


def test_cells_use_the_shared_formats(tmp_path):
    """金額2桁・回数・時間1桁・比率4桁・真偽値・語彙の値で書く。"""
    row = _read(_write(tmp_path, [_row()]))[0]
    assert row == {
        "email": "bea@x.jp", "month": MONTH, "seat": "standard+premium",
        "department": "架空開発部", "team": "架空チーム", "role": "エンジニア",
        "spend_complete": "True", "total_demand_usd": "123.46", "code_demand_usd": "100.00",
        "code_requests": "12", "loc_with_cc": "345", "github_id": "login",
        "github_data": "ok", "github_cache_complete": "False", "merged_pr_count": "4",
        "lead_time_median_hours": "2.2", "commit_count": "6", "lines_added_capped": "300",
        "large_commits": "1", "cost_per_merged_line_usd": "0.3333",
    }


def test_none_is_blank(tmp_path):
    """確定しない値（None）は空欄。"""
    row = _read(_write(tmp_path, [_blank()]))[0]
    for name in ("code_demand_usd", "code_requests", "loc_with_cc", "github_id",
                 "github_cache_complete", "merged_pr_count", "lead_time_median_hours",
                 "commit_count", "lines_added_capped", "large_commits",
                 "cost_per_merged_line_usd"):
        assert row[name] == "", name
    assert row["github_data"] == "not_configured"


def test_input_text_is_escaped_but_numbers_are_not(tmp_path):
    """入力由来の文字列には式のエスケープと改行の正規化を掛け、負の金額には掛けない。"""
    path = _write(tmp_path, [_blank(
        department="=SUM(A1)", team="-ops", role="line1\r\nline2",
        total_demand_usd=-1.5,
    )])
    row = _read(path)[0]
    assert (row["department"], row["team"], row["role"]) == (
        "'=SUM(A1)", "'-ops", "line1\nline2")
    assert row["total_demand_usd"] == "-1.50"
    assert b"\r" not in path.read_bytes()


def test_rows_are_sorted_by_email_then_month(tmp_path):
    """行はメール昇順・月昇順（渡した順に依らない）。"""
    rows = [_blank(email="cy@x.jp", month="2026-05"), _blank(email="amy@x.jp", month=MONTH),
            _blank(email="cy@x.jp", month="2026-04"), _blank(email="amy@x.jp", month="2026-05")]
    assert [(r["email"], r["month"]) for r in _read(_write(tmp_path, rows))] == [
        ("amy@x.jp", "2026-05"), ("amy@x.jp", MONTH),
        ("cy@x.jp", "2026-04"), ("cy@x.jp", "2026-05"),
    ]


def test_no_rows_writes_the_header_only(tmp_path):
    """行が0件でも見出しだけの CSV を書く。"""
    path = _write(tmp_path, [])
    assert path.read_bytes().decode("utf-8-sig") == ",".join(ADOPTION_PEOPLE_COLUMNS) + "\n"


def test_same_rows_give_the_same_bytes_with_lf(tmp_path):
    """同じ行の集合からは同じバイト列になり、改行は LF。"""
    rows = [_row(), _blank(email="amy@x.jp")]
    first = _write(tmp_path, rows).read_bytes()
    second = _write(tmp_path, list(reversed(rows))).read_bytes()
    assert first == second
    assert first.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in first and first.endswith(b"\n")


def test_duplicate_person_month_and_wrong_rows_are_rejected(tmp_path):
    """同じ人の同じ月が2行あれば ValueError、行でないものは TypeError。"""
    with pytest.raises(ValueError):
        _write(tmp_path, [_row(), _row()])
    with pytest.raises(TypeError):
        _write(tmp_path, [{"email": "bea@x.jp"}])


# ------------------------------------------------------------ adoption-teams


def _team(**changes) -> GroupMonth:
    """ok の月の部署の1行（changes で列を差し替える）。"""
    base = GroupMonth(
        scope=GroupScope.DEPARTMENT, name="架空開発部", month=MONTH, spend_complete=True,
        github_data=GithubData.OK, github_cache_complete=False, seat_holders=5,
        code_users=4, code_users_3m=3, code_demand_usd=1234.567, code_demand_median_usd=12.345,
        loc_median_of_users=101.5, github_people=3, merged_pr_count=7,
        pr_additions_median=42.25, lead_time_median_hours=2.25, lead_time_p90_hours=10.0,
        lines_added_capped=300, code_demand_usd_of_github_people=100.0,
    )
    return dataclasses.replace(base, **changes)


def _team_blank(**changes) -> GroupMonth:
    """GitHub 分析の無い組織の、値の確定しない組織全体の1行。"""
    return _team(**{
        "scope": GroupScope.ORGANIZATION, "name": "", "github_data": GithubData.NOT_CONFIGURED,
        "github_cache_complete": None, "code_users": None, "code_users_3m": None,
        "code_demand_usd": None, "code_demand_median_usd": None, "loc_median_of_users": None,
        "github_people": None, "merged_pr_count": None, "pr_additions_median": None,
        "lead_time_median_hours": None, "lead_time_p90_hours": None,
        "lines_added_capped": None, "code_demand_usd_of_github_people": None, **changes,
    })


def _write_teams(tmp_path: Path, rows) -> Path:
    path = tmp_path / "adoption-teams.csv"
    write_adoption_teams(rows, path)
    return path


def test_team_columns_follow_the_design_order(tmp_path):
    """チーム × 月の表の列は設計書 §27.8 の表の順。"""
    path = _write_teams(tmp_path, [_team()])
    header = path.read_bytes().decode("utf-8-sig").splitlines()[0]
    assert header.split(",") == list(ADOPTION_TEAMS_COLUMNS)
    assert ADOPTION_TEAMS_COLUMNS == (
        "scope", "name", "month", "spend_complete", "github_data", "github_cache_complete",
        "seat_holders", "code_users", "code_users_3m", "code_demand_usd",
        "code_demand_median_usd", "loc_median_of_users", "github_people", "merged_pr_count",
        "pr_additions_median", "lead_time_median_hours", "lead_time_p90_hours",
        "lines_added_capped", "code_demand_usd_of_github_people", "cost_per_merged_line_usd",
    )


def test_team_cells_use_the_shared_formats(tmp_path):
    """金額2桁・人数と件数は整数・行数の中央値と時間は1桁・比率4桁・真偽値・語彙の値。"""
    row = _read(_write_teams(tmp_path, [_team()]))[0]
    assert row == {
        "scope": "department", "name": "架空開発部", "month": MONTH,
        "spend_complete": "True", "github_data": "ok", "github_cache_complete": "False",
        "seat_holders": "5", "code_users": "4", "code_users_3m": "3",
        "code_demand_usd": "1234.57", "code_demand_median_usd": "12.35",
        "loc_median_of_users": "101.5", "github_people": "3", "merged_pr_count": "7",
        "pr_additions_median": "42.2", "lead_time_median_hours": "2.2",
        "lead_time_p90_hours": "10.0", "lines_added_capped": "300",
        "code_demand_usd_of_github_people": "100.00", "cost_per_merged_line_usd": "0.3333",
    }


def test_team_none_is_blank(tmp_path):
    """確定しない値（None）は空欄で、組織全体の名前も空欄。"""
    row = _read(_write_teams(tmp_path, [_team_blank()]))[0]
    for name in ("name", "github_cache_complete", "code_users", "code_users_3m",
                 "code_demand_usd", "code_demand_median_usd", "loc_median_of_users",
                 "github_people", "merged_pr_count", "pr_additions_median",
                 "lead_time_median_hours", "lead_time_p90_hours", "lines_added_capped",
                 "code_demand_usd_of_github_people", "cost_per_merged_line_usd"):
        assert row[name] == "", name
    assert (row["scope"], row["github_data"], row["seat_holders"]) == (
        "organization", "not_configured", "5")


def test_team_names_are_escaped(tmp_path):
    """集まりの名前には式のエスケープと改行の正規化を掛ける。"""
    path = _write_teams(tmp_path, [_team(name="=SUM(A1)"),
                                   _team(scope=GroupScope.TEAM, name="line1\r\nline2")])
    rows = _read(path)
    assert [row["name"] for row in rows] == ["'=SUM(A1)", "line1\nline2"]
    assert b"\r" not in path.read_bytes()


def test_team_rows_are_sorted_by_scope_name_and_month(tmp_path):
    """行は種別（組織全体・部署・チームの順）・名前・月の昇順（渡した順に依らない）。"""
    rows = [
        _team(scope=GroupScope.TEAM, name="Vega-Team"),
        _team(name="架空営業部", month="2026-05"),
        _team_blank(month="2026-05"),
        _team(name="架空営業部"),
        _team(name="架空開発部"),
        _team_blank(),
    ]
    assert [(r["scope"], r["name"], r["month"]) for r in _read(_write_teams(tmp_path, rows))] \
        == [("organization", "", "2026-05"), ("organization", "", MONTH),
            ("department", "架空営業部", "2026-05"), ("department", "架空営業部", MONTH),
            ("department", "架空開発部", MONTH), ("team", "Vega-Team", MONTH)]


def test_team_no_rows_writes_the_header_only(tmp_path):
    """行が0件でも見出しだけの CSV を書く。"""
    path = _write_teams(tmp_path, [])
    assert path.read_bytes().decode("utf-8-sig") == ",".join(ADOPTION_TEAMS_COLUMNS) + "\n"


def test_team_same_rows_give_the_same_bytes_with_lf(tmp_path):
    """同じ行の集合からは同じバイト列になり、改行は LF。"""
    rows = [_team(), _team_blank(), _team(scope=GroupScope.TEAM, name="Vega-Team")]
    first = _write_teams(tmp_path, rows).read_bytes()
    second = _write_teams(tmp_path, list(reversed(rows))).read_bytes()
    assert first == second
    assert first.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in first and first.endswith(b"\n")


def test_team_duplicates_and_wrong_rows_are_rejected(tmp_path):
    """同じ（種別・名前・月）が2行あれば ValueError、型の違う行は TypeError。"""
    with pytest.raises(ValueError):
        _write_teams(tmp_path, [_team(), _team(seat_holders=6)])
    with pytest.raises(TypeError):
        _write_teams(tmp_path, [_row()])


def test_team_sums_are_rounded_after_adding(tmp_path):
    """需要の合計は丸める前の値の和を最後に丸める（1人ずつ丸めた表の和とは違いうる）。"""
    people = [
        dataclasses.replace(_blank(email=email), code_demand_usd=0.0144, code_requests=1.0)
        for email in ("amy@x.jp", "bea@x.jp")
    ]
    material = MonthInputs(
        month=MONTH, spend_complete=True, github_data=GithubData.NOT_CONFIGURED,
        github_cache_complete=None, rows=tuple(people), prs=None,
    )
    team_rows = _read(_write_teams(tmp_path, group_rows([material])))
    people_rows = _read(_write(tmp_path, people))
    assert [row["code_demand_usd"] for row in people_rows] == ["0.01", "0.01"]
    assert team_rows[0]["code_demand_usd"] == "0.03"
