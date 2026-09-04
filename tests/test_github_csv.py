"""github-summary.csv の書き出しのテスト。

固定するのは4つ。1つ目は形——列の並び、個人行の後に組織全体の1行、内訳6列を持つのは
組織全体行だけ。2つ目は表記——lead time は小数1桁で、要約の無い行は 0 で埋めずに空欄。
3つ目は書かないもの——repository 名・GitHub Organization 名・対応表に無い作成者の login は
1文字も出さない。4つ目は決定性——同じ要約からは常に同じバイト列（BOM つき UTF-8・LF）。

要約の中身そのもの（どの PR がどの区分に入るか）は tests/test_github_metrics.py が固定する。
"""

import csv
import io
from pathlib import Path

import pytest

from seat_analyzer.github_metrics import (
    GithubMetrics,
    LeadTimeSummary,
    UserPrMetrics,
)
from seat_analyzer.report import GITHUB_SUMMARY_COLUMNS, write_github_summary

ORG = "example-org"
MONTH = "2026-08"

# 集計から外した分の内訳（組織全体行だけが値を持つ列）
BREAKDOWN = (
    "unmapped_authors",
    "unmapped_prs",
    "bot_prs",
    "deleted_author_prs",
    "excluded_repository_prs",
    "total_prs",
)
LEAD_TIME = (
    "lead_time_median_hours",
    "lead_time_p75_hours",
    "lead_time_p90_hours",
)


def _summary(count: int, median: float, p75: float, p90: float) -> LeadTimeSummary:
    return LeadTimeSummary(
        count=count, median_hours=median, p75_hours=p75, p90_hours=p90
    )


def _user(email: str, login: str, count: int = 0,
          lead_time: LeadTimeSummary | None = None) -> UserPrMetrics:
    """1人ぶんの要約（件数を省くと PR 0 件＝lead time なし）。"""
    return UserPrMetrics(
        email=email, github_login=login, merged_pr_count=count, lead_time=lead_time
    )


def _metrics(**overrides) -> GithubMetrics:
    """1組織×1月の要約（区分の合計は値オブジェクトが検査するので整合させて渡す）。

    個人へ帰属 2 件・対応表に無い作成者 3 件・Bot 4 件・削除済み 1 件・対象外
    repository 2 件（合計 12 件）で、人の PR は 2 + 3 + 1 = 6 件。
    """
    values = {
        "github_org": ORG,
        "month": MONTH,
        "users": (
            _user("alice@example.com", "alice-dev", 2, _summary(2, 1.24, 3.56, 12.0)),
            _user("bob@example.com", "bob-42"),
        ),
        "lead_time": _summary(6, 2.04, 10.51, 40.06),
        "unmapped_authors": 1,
        "unmapped_prs": 3,
        "bot_prs": 4,
        "deleted_author_prs": 1,
        "excluded_repository_prs": 2,
        "total_prs": 12,
        "cache_complete": True,
    }
    values.update(overrides)
    return GithubMetrics(**values)


def _write(tmp_path: Path, metrics: GithubMetrics) -> Path:
    path = tmp_path / "github-summary.csv"
    write_github_summary(metrics, path)
    return path


def _rows(path: Path) -> list[dict[str, str]]:
    """書き出した内容を行ごとの辞書にする（BOM を除いて読む）。"""
    text = path.read_bytes().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def _header(path: Path) -> list[str]:
    text = path.read_bytes().decode("utf-8-sig")
    return next(csv.reader(io.StringIO(text, newline="")))


# --------------------------------------------------------------------- 形


def test_the_columns_are_written_in_the_declared_order(tmp_path):
    assert _header(_write(tmp_path, _metrics())) == list(GITHUB_SUMMARY_COLUMNS)


def test_the_user_rows_come_first_and_the_organization_row_is_last(tmp_path):
    rows = _rows(_write(tmp_path, _metrics()))

    assert [row["scope"] for row in rows] == ["user", "user", "organization"]
    assert [row["email"] for row in rows] == \
        ["alice@example.com", "bob@example.com", ""]
    assert [row["github_login"] for row in rows] == ["alice-dev", "bob-42", ""]


def test_the_user_rows_keep_the_order_they_were_given(tmp_path):
    """並びは email の昇順のまま（件数の多い順へ並べ替えて順位を作らない）。"""
    metrics = _metrics(
        users=(
            _user("alice@example.com", "alice-dev"),
            _user("bob@example.com", "bob-42", 2, _summary(2, 1.0, 1.0, 1.0)),
        ),
        lead_time=_summary(6, 2.0, 2.0, 2.0),
    )
    rows = _rows(_write(tmp_path, metrics))

    assert [row["email"] for row in rows[:2]] == \
        ["alice@example.com", "bob@example.com"]


def test_the_month_is_on_every_row(tmp_path):
    rows = _rows(_write(tmp_path, _metrics()))

    assert [row["month"] for row in rows] == [MONTH] * 3


def test_the_counts_are_written_as_integers(tmp_path):
    rows = _rows(_write(tmp_path, _metrics()))

    assert [row["merged_pr_count"] for row in rows] == ["2", "0", "6"]


def test_the_organization_row_counts_every_human_pr(tmp_path):
    """組織全体の件数は Bot 以外の PR 全件（対応表の記入状況で母数が動かない）。"""
    rows = _rows(_write(tmp_path, _metrics()))
    organization = rows[-1]

    # 個人へ帰属 2 + 対応表に無い作成者 3 + 削除済み 1 = 6（Bot と対象外は入らない）
    assert organization["merged_pr_count"] == "6"
    assert organization["total_prs"] == "12"


def test_the_breakdown_is_only_on_the_organization_row(tmp_path):
    rows = _rows(_write(tmp_path, _metrics()))

    for row in rows[:2]:
        assert [row[name] for name in BREAKDOWN] == [""] * len(BREAKDOWN)
    assert [rows[-1][name] for name in BREAKDOWN] == ["1", "3", "4", "1", "2", "12"]


@pytest.mark.parametrize("complete", [True, False])
def test_cache_complete_is_written_on_every_row(tmp_path, complete):
    """部分的な値かどうかは、どの行を見ても分かるようにする。"""
    rows = _rows(_write(tmp_path, _metrics(cache_complete=complete)))

    assert [row["cache_complete"] for row in rows] == [str(complete)] * 3


def test_the_organization_row_is_written_without_a_mapping(tmp_path):
    """対応表が無く個人行が0件でも、組織全体の1行は書く。

    PR を個人へ帰属できないことと、PR そのものが無いことは別の状態のため。
    """
    metrics = _metrics(users=(), lead_time=_summary(4, 1.0, 2.0, 3.0), total_prs=10)
    rows = _rows(_write(tmp_path, metrics))

    assert [row["scope"] for row in rows] == ["organization"]
    assert rows[0]["merged_pr_count"] == "4"      # 対応表に無い作成者 3 + 削除済み 1


# --------------------------------------------------------------- lead time


def test_lead_times_are_written_with_one_decimal(tmp_path):
    rows = _rows(_write(tmp_path, _metrics()))

    assert [rows[0][name] for name in LEAD_TIME] == ["1.2", "3.6", "12.0"]
    assert [rows[-1][name] for name in LEAD_TIME] == ["2.0", "10.5", "40.1"]


def test_a_user_without_prs_has_no_lead_time(tmp_path):
    """0 で埋めない（即時 merge された PR があることと区別できなくなるため）。"""
    rows = _rows(_write(tmp_path, _metrics()))

    assert [rows[1][name] for name in LEAD_TIME] == ["", "", ""]


def test_the_organization_row_has_no_lead_time_without_human_prs(tmp_path):
    """人の PR が1件も無い月は組織全体の lead time も空欄。"""
    metrics = _metrics(
        users=(_user("alice@example.com", "alice-dev"),),
        lead_time=None, unmapped_authors=0, unmapped_prs=0, bot_prs=4,
        deleted_author_prs=0, excluded_repository_prs=2, total_prs=6,
    )
    rows = _rows(_write(tmp_path, metrics))

    assert [rows[-1][name] for name in LEAD_TIME] == ["", "", ""]
    assert rows[-1]["merged_pr_count"] == "0"


# ------------------------------------------------------------ 書かないもの


def test_no_organization_name_appears_in_the_file(tmp_path):
    """GitHub の Organization 名はレポートへ出さない（要約が持っていても書かない）。"""
    name = "gh-org-that-must-not-appear"
    path = _write(tmp_path, _metrics(github_org=name))

    assert name not in path.read_bytes().decode("utf-8-sig")


def test_repositories_appear_only_as_a_count(tmp_path):
    """repository 名を持つ列は無く、対象外の分は件数だけを載せる。"""
    header = _header(_write(tmp_path, _metrics()))

    assert [name for name in header if "repositor" in name] == \
        ["excluded_repository_prs"]


def test_no_unmapped_author_login_appears_in_the_file(tmp_path):
    """対応表に無い作成者は人数と件数だけを載せる（login は出さない）。"""
    rows = _rows(_write(tmp_path, _metrics()))
    logins = {row["github_login"] for row in rows}

    assert logins == {"alice-dev", "bob-42", ""}


# --------------------------------------------------------------- 決定性と字句


def test_the_file_starts_with_a_bom_and_uses_lf(tmp_path):
    raw = _write(tmp_path, _metrics()).read_bytes()

    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_writing_the_same_metrics_twice_gives_the_same_bytes(tmp_path):
    metrics = _metrics()
    first = _write(tmp_path, metrics).read_bytes()

    assert _write(tmp_path, metrics).read_bytes() == first


def test_an_email_that_looks_like_a_formula_is_escaped(tmp_path):
    """表計算ソフトが式として解釈しうるセルは引用符で始める（recommendations と同じ）。"""
    metrics = _metrics(
        users=(_user("=1+1@example.com", "alice-dev", 2, _summary(2, 1.0, 1.0, 1.0)),),
        lead_time=_summary(6, 1.0, 1.0, 1.0),
    )
    rows = _rows(_write(tmp_path, metrics))

    assert rows[0]["email"] == "'=1+1@example.com"


def test_a_newline_inside_an_email_becomes_lf(tmp_path):
    """セルの中の改行も LF に揃える（レコード区切りだけでは CR が残る）。"""
    metrics = _metrics(
        users=(_user("a\r\nb@example.com", "alice-dev"),),
        lead_time=_summary(4, 1.0, 1.0, 1.0), total_prs=10,
    )
    path = _write(tmp_path, metrics)

    assert b"\r" not in path.read_bytes()
    assert _rows(path)[0]["email"] == "a\nb@example.com"
