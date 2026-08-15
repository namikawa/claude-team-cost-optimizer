"""分析詳細資料（details.md）の組み立て。

report.md はサマリ・推奨・考察の短い文書に絞り、ユーザ単位の表・月中の推移・分布は
この資料が受け持つ。dashboard.html と同じ数値の Markdown 版で、考察執筆（discuss）へ
渡す資料も兼ねる（report.md 本体だけを渡すと、表を削った分の材料が消えるため）。

section の中身は report.md にあったものと同じで、組み立て関数も markdown.py の
ものをそのまま使う（数値・表の形式は変えない）。データが無い section は従来どおり
省略する。載せるのは対象組織のデータだけ（レポート成果物の組織分離ルールと同じ）。
"""

from __future__ import annotations

from pathlib import Path

from ..analyze import AnalysisResult
from .format import _scope_label, _sort_for_display
from .markdown import (
    _code_diff_md,
    _detail_table_md,
    _e_distribution_md,
    _group_summary_md,
    _member_changes_md,
    _notes_md,
    _sensitivity_md,
    _snapshot_md,
    _stats_md,
    _user_legend_md,
    _user_table_md,
)
from .stats import distributions
from .text import GROUP_AXES, STATUS_ORDER, _TEXT

_INTRO = ("機械生成の詳細資料です。dashboard.html と同じ数値の Markdown 版で、"
          "考察執筆（`seat-analyzer discuss`）へ渡す資料を兼ねます。")


def _sections(result: AnalysisResult) -> list[str]:
    """details.md に載せる section の本文（データが無い section は空文字列）。"""
    s = result.summary
    users = _sort_for_display(result.users, "status", STATUS_ORDER, "monthly_saving_usd")

    blocks = [
        f"## 全ユーザ\n\n{_user_table_md(users)}\n\n{_user_legend_md(s)}",
        _notes_md(users),
    ]
    for col, heading, include_unset in GROUP_AXES:
        block = _group_summary_md(users, s, col, heading, include_unset=include_unset)
        # 縦合計の断りは、説明対象の表と同じ文書に置く（dashboard は自前の注意に持つ）
        if block and col == "team":
            block += f"\n- {_TEXT['note_team_total']}。"
        blocks.append(block)
    # 分布は詳細利用状況の直後（個々の数値を見た直後に位置を確かめられる）
    blocks += [
        _detail_table_md(users),
        _stats_md(distributions(result.users, result.product_usage)),
        _snapshot_md(result.snapshot),
        _code_diff_md(result.code_diff),
        _member_changes_md(result.member_changes),
        _e_distribution_md(result.e_distribution),
        _sensitivity_md(users),
    ]
    return blocks


def write_details(result: AnalysisResult, path: Path) -> None:
    """details.md を書き出す（正式分析で常に生成する）。"""
    body = "\n\n".join(
        block.strip("\n") for block in _sections(result) if block.strip()
    )
    md = f"# 分析詳細資料 — {_scope_label(result)}\n\n{_INTRO}\n\n{body}\n"
    path.write_text(md, encoding="utf-8", newline="\n")
