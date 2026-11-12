"""adoption-people.csv の書き出し（人 × 月の表。設計書 §27.8）。

行の組み立ては adoption が行う。ここは受け取った行を直列化するだけにして、同じ行の集合
から常に同じバイト列が出ることを保つ。

行はメールの昇順・月の昇順に並べる。受け取った順には依らせず、量の多い順にも並べない
（人ごとの値は参考値で、個人の順位づけに使うものではないため。設計書 §15.6）。

書式は既存の CSV と同じ関数を使う（金額と比率は usage-summary、時間・真偽値・入力由来の
テキストは github-summary）。同じ値が成果物ごとに違う字句で出ないようにするため。
確定できなかった値（None）は空欄にする。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from ..adoption import PersonMonth
from .github_csv import _flag, _hours, _text
from .usage_csv import _count, _share, _usd

# 列の順序（この並びで書く。設計書 §27.8 の表の順）
ADOPTION_PEOPLE_COLUMNS = (
    "email",
    "month",
    "seat",
    "department",
    "team",
    "role",
    "spend_complete",
    "total_demand_usd",
    "code_demand_usd",
    "code_requests",
    "loc_with_cc",
    "github_id",
    "github_data",
    "github_cache_complete",
    "merged_pr_count",
    "lead_time_median_hours",
    "commit_count",
    "lines_added_capped",
    "large_commits",
    "cost_per_merged_line_usd",
)


def _whole(value: int) -> str:
    """件数・行数（整数の字句）。"""
    return str(int(value))


def _label(value) -> str:
    """語彙の値（StrEnum の値そのもの）。"""
    return str(value.value)


# 列ごとの書式。入力由来のテキスト（email・部署・チーム・職種）には式のエスケープと改行の
# 正規化を掛け、数値と語彙から作った文字列には掛けない（負の金額の "-" が式の先頭文字と
# 一致し、引用符が付いて値が壊れるため）。比率の小数4桁は usage-summary の構成比と同じ書式
_FORMATTERS = {
    "email": _text,
    "month": str,
    "seat": str,
    "department": _text,
    "team": _text,
    "role": _text,
    "spend_complete": _flag,
    "total_demand_usd": _usd,
    "code_demand_usd": _usd,
    "code_requests": _count,
    "loc_with_cc": _whole,
    "github_id": _label,
    "github_data": _label,
    "github_cache_complete": _flag,
    "merged_pr_count": _whole,
    "lead_time_median_hours": _hours,
    "commit_count": _whole,
    "lines_added_capped": _whole,
    "large_commits": _whole,
    "cost_per_merged_line_usd": _share,
}


def _cells(row: PersonMonth) -> dict[str, str]:
    """1行分のセル文字列（None は空欄）。"""
    cells = {}
    for name in ADOPTION_PEOPLE_COLUMNS:
        value = getattr(row, name)
        cells[name] = "" if value is None else _FORMATTERS[name](value)
    return cells


def write_adoption_people(rows: Iterable[PersonMonth], path: Path) -> None:
    """adoption-people.csv を書く（行が0件でも見出しだけの CSV を書く）。

    行はメールの昇順・月の昇順に並べ替えてから書く。同じ人の同じ月の行が2つあれば、
    書く前に ValueError にする（どちらの値が正か決まらないため）。PersonMonth でない
    行は TypeError。
    """
    ordered = list(rows)
    for row in ordered:
        if not isinstance(row, PersonMonth):
            raise TypeError(f"rows には PersonMonth が必要です: {type(row).__name__}")
    ordered.sort(key=lambda row: (row.email, row.month))
    keys = [(row.email, row.month) for row in ordered]
    if len(set(keys)) != len(keys):
        raise ValueError("同じ人の同じ月の行が複数あります")
    table = pd.DataFrame(
        [_cells(row) for row in ordered], columns=list(ADOPTION_PEOPLE_COLUMNS)
    )
    table.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
