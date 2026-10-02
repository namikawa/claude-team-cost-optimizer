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

from ..analyze import AnalysisResult, OrgAnalysisResult, own_demand_users
from . import spaces
from .document import _atomic_write
from .format import _account_rows, _scope_label, _sole_result, _sort_for_display
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
from .naming import DASHBOARD
from .stats import distributions
from .text import _TEXT, GROUP_AXES, STATUS_ORDER


def _intro(result: AnalysisResult | OrgAnalysisResult) -> str:
    """冒頭の一文。同じ数値のダッシュボードをファイル名で示す（共有先で探せるように）。"""
    dashboard = DASHBOARD.name(result.month, result.org)
    return (f"機械生成の詳細資料です。{dashboard} と同じ数値の Markdown 版で、"
            "考察執筆（`seat-analyzer discuss`）へ渡す資料を兼ねます。")


def _sections(result: AnalysisResult) -> list[str]:
    """details.md に載せる section の本文（データが無い section は空文字列）。"""
    s = result.summary
    users = _sort_for_display(result.users, "status", STATUS_ORDER, "monthly_saving_usd")

    blocks = [
        f"## 全ユーザ\n\n{_user_table_md(users)}\n\n{_user_legend_md(s)}",
        _notes_md(users),
        *_group_blocks(users, s),
    ]
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


def _group_blocks(users, summary: dict) -> list[str]:
    """部署別 → チーム別のサマリ（チーム別には縦合計の断りを添える）。"""
    blocks = []
    for col, heading, include_unset in GROUP_AXES:
        block = _group_summary_md(users, summary, col, heading, include_unset=include_unset)
        # 縦合計の断りは、説明対象の表と同じ文書に置く（dashboard は自前の注意に持つ）
        if block and col == "team":
            block += f"\n- {_TEXT['note_team_total']}。"
        blocks.append(block)
    return blocks


def _sections_multi(org: OrgAnalysisResult) -> list[str]:
    """複数 workspace の組織の details.md の section（設計書 §26.7）。

    判定の表（全ユーザ）は全アカウントを連結して主の行の需要を合算値のまま出し、
    観測の表（詳細利用状況・分布）は各アカウント自身の需要で出す。部署別・チーム別
    サマリと備考は人の層から作る（アカウント数を人数として数えない）。workspace
    固有の section は見出しに表示名を添えて主→副の順に並べる。
    """
    results = org.workspaces
    labels = {name: org.contexts[name].label for name in results}
    judged = _account_rows(org, {name: r.users for name, r in results.items()})
    users = _sort_for_display(judged, "status", STATUS_ORDER, "monthly_saving_usd")
    persons = org.persons.frame if org.persons is not None else judged
    persons_sorted = _sort_for_display(persons, "status", STATUS_ORDER, "monthly_saving_usd")
    credit_shown = any(r.summary.get("credit_shown", False) for r in results.values())
    # 人の表はシート費を自前の列で持つので、summary は価格の参照にしか使われない
    summary = next(iter(results.values())).summary
    own = {name: own_demand_users(r) for name, r in results.items()}

    blocks = [
        (f"## 全ユーザ\n\n{_user_table_md(users, space=True)}\n\n"
         f"{_user_legend_md({'credit_shown': credit_shown}, spaces.account_legend_lines(org))}"),
        _notes_md(persons_sorted),
        *_group_blocks(persons, summary),
        spaces.persons_md(org),
        _detail_table_md(_account_rows(org, own), space=True),
    ]
    blocks += [
        _stats_md(distributions(own[name], r.product_usage), labels[name])
        for name, r in results.items()
    ]
    blocks += [_snapshot_md(r.snapshot, labels[name]) for name, r in results.items()]
    blocks += [_code_diff_md(r.code_diff, labels[name]) for name, r in results.items()]
    blocks += [_member_changes_md(r.member_changes, labels[name])
               for name, r in results.items()]
    blocks += [_e_distribution_md(r.e_distribution, labels[name])
               for name, r in results.items()]
    # 固定シートの workspace は損益分岐判定をしていないので、感度分析を出さない
    # （「全員一致」と見せると判定した結果に読める）
    blocks += [
        _sensitivity_md(
            _sort_for_display(r.users, "status", STATUS_ORDER, "monthly_saving_usd"),
            labels[name])
        for name, r in results.items() if not org.contexts[name].fixed_seat
    ]
    return blocks


def write_details(result: AnalysisResult | OrgAnalysisResult, path: Path) -> None:
    """details.md を書き出す（正式分析で常に生成する）。

    OrgAnalysisResult は、複数 workspace の組織なら workspace ごとの section と人の層の
    表を持つ形で書き、そうでなければ唯一の workspace の結果で従来どおりに書く。
    """
    if isinstance(result, OrgAnalysisResult) and result.has_multiple_workspaces:
        blocks = _sections_multi(result)
    else:
        if isinstance(result, OrgAnalysisResult):
            result = _sole_result(result)
        blocks = _sections(result)
    body = "\n\n".join(
        block.strip("\n") for block in blocks if block.strip()
    )
    md = f"# 分析詳細資料 — {_scope_label(result)}\n\n{_intro(result)}\n\n{body}\n"
    # 置換で書く。切り詰めてから書く write_text は、中断・書き込み失敗のときに
    # 途中までの details.md を残す。考察執筆はこのファイルを資料に使うため、
    # 表を欠いたまま残ると気づかれずに材料だけが減る
    _atomic_write(path, md)
