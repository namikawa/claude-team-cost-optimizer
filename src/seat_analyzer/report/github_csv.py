"""github-summary.csv の書き出し（GitHub 由来の参考値）。

行の組み立ては github_metrics が行う。ここは受け取った要約を直列化するだけにして、
同じ要約から常に同じバイト列が出ることを保つ。

個人行の並びは受け取った順（email の昇順）のままにして、件数の多い順へ並べ替えない。
参考値は個人の順位づけに使うものではないため（設計書 §15.6）。

repository 名・Organization 名・対応表に無い作成者の login は書かない。要約がこれらを
持たないので、この CSV に出る余地は無い。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..github_metrics import GithubMetrics, LeadTimeSummary, UserPrMetrics
from .csv_out import normalize_cell_newlines, sanitize_csv_cell

# 列の順序（この並びで書く）。scope が行の単位を表し、内訳6列は組織全体行だけが持つ
GITHUB_SUMMARY_COLUMNS = (
    "scope",
    "email",
    "github_login",
    "month",
    "merged_pr_count",
    "lead_time_median_hours",
    "lead_time_p75_hours",
    "lead_time_p90_hours",
    "unmapped_authors",
    "unmapped_prs",
    "bot_prs",
    "deleted_author_prs",
    "excluded_repository_prs",
    "total_prs",
    "cache_complete",
)

# scope の値（個人1人ぶんの行と、組織全体の1行）
_USER_SCOPE = "user"
_ORGANIZATION_SCOPE = "organization"

# lead time の3列と、集計から外した分の内訳6列
_LEAD_TIME_COLUMNS = (
    "lead_time_median_hours",
    "lead_time_p75_hours",
    "lead_time_p90_hours",
)
_BREAKDOWN_COLUMNS = (
    "unmapped_authors",
    "unmapped_prs",
    "bot_prs",
    "deleted_author_prs",
    "excluded_repository_prs",
    "total_prs",
)


def _text(value: str) -> str:
    """入力由来のテキスト（対応表に書かれた email と login）。

    式のエスケープと改行の正規化は、入力由来のテキストにだけ適用する。順序は csv_out と
    同じで式の判定が先（改行を先に均すと、CR で始まるセルに引用符が付かなくなる）。
    数値から組み立てた文字列には掛けない。
    """
    return normalize_cell_newlines(sanitize_csv_cell(value))


def _hours(value: float) -> str:
    """時間（小数1桁）。"""
    return f"{float(value):.1f}"


def _flag(value: bool) -> str:
    """真偽値（表記は recommendations.csv と同じ）。"""
    return "True" if value else "False"


def _lead_time_cells(summary: LeadTimeSummary | None) -> dict[str, str]:
    """lead time の3列（1件も無ければ空欄）。

    0 で埋めると「すべての PR が即時 merge された」ことと区別できなくなるため、
    要約の無い行は欠損のまま出す（usage-summary.csv と同じ流儀）。
    """
    if summary is None:
        return dict.fromkeys(_LEAD_TIME_COLUMNS, "")
    return {
        "lead_time_median_hours": _hours(summary.median_hours),
        "lead_time_p75_hours": _hours(summary.p75_hours),
        "lead_time_p90_hours": _hours(summary.p90_hours),
    }


def _user_cells(user: UserPrMetrics, metrics: GithubMetrics) -> dict[str, str]:
    """個人1人ぶんの行（内訳6列は空欄）。"""
    return {
        "scope": _USER_SCOPE,
        "email": _text(user.email),
        "github_login": _text(user.github_login),
        "month": metrics.month,
        "merged_pr_count": str(user.merged_pr_count),
        **_lead_time_cells(user.lead_time),
        **dict.fromkeys(_BREAKDOWN_COLUMNS, ""),
        "cache_complete": _flag(metrics.cache_complete),
    }


def _organization_cells(metrics: GithubMetrics) -> dict[str, str]:
    """組織全体の行（email と login は持たない）。

    件数は Bot 以外の PR 全件（`human_prs`）で、対応表の記入状況で母数が動かない。
    """
    return {
        "scope": _ORGANIZATION_SCOPE,
        "email": "",
        "github_login": "",
        "month": metrics.month,
        "merged_pr_count": str(metrics.human_prs),
        **_lead_time_cells(metrics.lead_time),
        **{name: str(getattr(metrics, name)) for name in _BREAKDOWN_COLUMNS},
        "cache_complete": _flag(metrics.cache_complete),
    }


def write_github_summary(metrics: GithubMetrics, path: Path) -> None:
    """github-summary.csv を書く（個人行の後に組織全体の1行）。

    対応表を持たない組織では個人行が0件になるが、組織全体の行は必ず書く（PR を個人へ
    帰属できないことと、PR そのものが無いことは別のため）。
    """
    rows = [_user_cells(user, metrics) for user in metrics.users]
    rows.append(_organization_cells(metrics))
    table = pd.DataFrame(rows, columns=list(GITHUB_SUMMARY_COLUMNS))
    table.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
