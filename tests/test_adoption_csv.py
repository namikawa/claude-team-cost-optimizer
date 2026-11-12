"""adoption-people.csv の書き出し（列の順・書式・エスケープ・並び）のテスト。"""

import csv
import dataclasses
import io
from pathlib import Path

import pytest

from seat_analyzer.adoption import GithubData, GithubId, PersonMonth
from seat_analyzer.report import ADOPTION_PEOPLE_COLUMNS, write_adoption_people

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
