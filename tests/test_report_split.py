"""report.md（アクションと考察）と details.md（詳細資料）の分担のテスト。

report.md から details.md へ移した section が過不足なく存在すること、つまり移動で
あって削除・改変ではないことを固定する。golden はバイト列を固定するだけなので、
「どちらの文書にどの section が載るか」という規則はここで持つ。

入力は examples/input の合成データ（条件つき section がすべて出る org-b の 2026-08）。
リポジトリ直下の input/ は実データなので使わない。
"""

import re

import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.cli import main
from seat_analyzer.report import write_all
from seat_analyzer.report.markdown import (
    _code_diff_md,
    _detail_table_md,
    _e_distribution_md,
    _grant_candidates_md,
    _group_summary_md,
    _member_changes_md,
    _notes_md,
    _sensitivity_md,
    _snapshot_md,
    _stats_md,
    _trend_md,
    _user_legend_md,
    _user_table_md,
)
from seat_analyzer.report.format import _sort_for_display
from seat_analyzer.report.stats import distributions
from seat_analyzer.report.text import GROUP_AXES, STATUS_ORDER

from .conftest import CONFIG, REPO_ROOT, spend_row

EXAMPLES_INPUT = REPO_ROOT / "examples" / "input"
ORG, MONTH = "org-b", "2026-08"

# report.md に残す section（この順）。dashboard が数値を担い、report.md は
# アクションと考察を読むための文書にする
REPORT_SECTIONS = [
    "サマリ",
    "前月からの変化",
    "追加クレジット付与候補",
    "シート変更推奨",
    "注意事項",
    "データ検証・警告",
    "考察",
]

# details.md へ移した section（この順）
DETAILS_SECTIONS = [
    "全ユーザ",
    "部署別サマリ",
    "チーム別サマリ",
    "詳細利用状況",
    "組織内の分布（参考値）",
    "月中の利用推移（スナップショット差分）",
    "月中の Claude Code 活動（code-analytics 差分）",
    "月中のメンバー変動（スナップショット差分）",
    "込み枠の実測（E = API換算需要 − 実課金）",
    "感度分析",
]


def _headings(md: str) -> list[str]:
    return re.findall(r"^## (.+)$", md, re.MULTILINE)


@pytest.fixture
def all_in(tmp_path, cfg):
    """全部入りサンプル（org-b 2026-08）の分析結果と生成物。"""
    output_dir = tmp_path / "reports"
    rc = main([
        "analyze", "--config", CONFIG, "--input-dir", str(EXAMPLES_INPUT),
        "--month", MONTH, "--org", ORG, "--output-dir", str(output_dir),
    ])
    assert rc == 0
    out = output_dir / ORG / MONTH
    result = analyze(EXAMPLES_INPUT / ORG, MONTH, cfg, org=ORG)
    return result, out / "report.md", out / "details.md"


def test_analyze_writes_details(all_in):
    """details.md が正式分析の成果物として生成される。"""
    _, report_path, details_path = all_in
    assert report_path.is_file() and details_path.is_file()
    assert details_path.read_text(encoding="utf-8").startswith(
        f"# 分析詳細資料 — {ORG} — {MONTH}")


def test_details_path_is_listed_in_output(make_input, tmp_path, capsys):
    """CLI の出力一覧に details.md のパスが載る。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Standard"], org="org-a",
    )
    output_dir = tmp_path / "reports"
    rc = main(["analyze", "--config", CONFIG, "--input-dir", str(input_dir),
               "--output-dir", str(output_dir), "--month", "2026-06"])
    assert rc == 0
    path = output_dir / "org-a" / "2026-06" / "details.md"
    assert f"details: {path}" in capsys.readouterr().out


def test_report_keeps_only_the_action_sections(all_in):
    """report.md の section 構成（順序込み）。条件つき section がすべて出る月で見る。"""
    _, report_path, _ = all_in
    assert _headings(report_path.read_text(encoding="utf-8")) == REPORT_SECTIONS


def test_details_carries_the_moved_sections(all_in):
    """details.md の section 構成（順序込み）。"""
    _, _, details_path = all_in
    assert _headings(details_path.read_text(encoding="utf-8")) == DETAILS_SECTIONS


def test_two_documents_have_no_overlapping_sections(all_in):
    """同じ section が両方に出ない（どちらを読めばよいかが一意に決まる）。"""
    _, report_path, details_path = all_in
    in_report = set(_headings(report_path.read_text(encoding="utf-8")))
    in_details = set(_headings(details_path.read_text(encoding="utf-8")))
    assert not (in_report & in_details)


def _moved_blocks(result) -> dict[str, str]:
    """details.md へ移した section の本文（section 名 → 本文）。"""
    s = result.summary
    users = _sort_for_display(result.users, "status", STATUS_ORDER, "monthly_saving_usd")
    blocks = {
        "全ユーザ": _user_table_md(users),
        "凡例": _user_legend_md(s),
        "備考": _notes_md(users),
        "詳細利用状況": _detail_table_md(users),
        "組織内の分布": _stats_md(distributions(result.users, result.product_usage)),
        "月中の利用推移": _snapshot_md(result.snapshot),
        "月中の Claude Code 活動": _code_diff_md(result.code_diff),
        "月中のメンバー変動": _member_changes_md(result.member_changes),
        "込み枠の実測": _e_distribution_md(result.e_distribution),
        "感度分析": _sensitivity_md(users),
    }
    for col, heading, include_unset in GROUP_AXES:
        blocks[heading] = _group_summary_md(
            users, s, col, heading, include_unset=include_unset)
    return blocks


def test_moved_sections_are_present_without_loss(all_in):
    """移した section の本文が details.md にそのまま（数値も書式も変えずに）存在する。

    移動の前後で内容が欠けたり書き換わったりしていないことの検査。組み立て関数の
    出力そのものと突き合わせるので、表の行が落ちればここで落ちる。
    """
    result, _, details_path = all_in
    details = details_path.read_text(encoding="utf-8")
    for name, block in _moved_blocks(result).items():
        assert block.strip(), f"{name}: 全部入りサンプルなのに空です（検査が空振りする）"
        assert block.strip() in details, f"{name} が details.md にありません"


def test_report_keeps_only_the_legend_of_the_moved_blocks(all_in):
    """report.md 側に残るのは凡例だけ（表そのものは持たない）。"""
    result, report_path, _ = all_in
    report = report_path.read_text(encoding="utf-8")
    blocks = _moved_blocks(result)
    assert blocks["凡例"].strip() in report      # 変更推奨の表の読み方として残す
    for name, block in blocks.items():
        if name == "凡例":
            continue
        assert block.strip() not in report, f"{name} が report.md にも残っています"


def test_trend_and_grant_stay_in_the_report(all_in):
    """前月からの変化と付与候補は report.md 側（アクションに直結するため）。"""
    result, report_path, details_path = all_in
    report = report_path.read_text(encoding="utf-8")
    details = details_path.read_text(encoding="utf-8")
    for block in (_trend_md(result.trend),
                  _grant_candidates_md(result.grant_candidates,
                                       result.summary["grant_suggested_cap_usd"])):
        assert block.strip() and block.strip() in report
        assert block.strip() not in details


# --- シート変更推奨の凡例（表が空でないときだけ添える） ---

def _no_change_result(make_input, cfg):
    """変更推奨が1人も出ない分析結果（Standard・低需要のみ）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 5.0, net=0.0)],
         "2026-06": [spend_row("a@x.jp", 6.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    return analyze(input_dir, "2026-06", cfg, org="org-a")


def test_legend_only_when_recommendation_table_has_rows(make_input, cfg, tmp_path):
    result = _no_change_result(make_input, cfg)
    assert result.users["status"].eq("変更推奨").sum() == 0
    out = tmp_path / "reports"
    write_all(result, out)
    report = (out / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert "## シート変更推奨\n\n該当なし。\n" in report
    assert "**API換算需要**" not in report          # 表が無いので凡例も出さない
    # details.md 側の全ユーザ表には（表があるので）凡例が付く
    details = (out / "2026-06" / "details.md").read_text(encoding="utf-8")
    assert "**API換算需要**" in details


def test_details_is_written_for_every_analysis(make_input, cfg, tmp_path):
    """条件つき section が1つも無い最小の組織でも details.md は出る。"""
    result = _no_change_result(make_input, cfg)
    out = tmp_path / "reports"
    write_all(result, out)
    details = (out / "2026-06" / "details.md").read_text(encoding="utf-8")
    # データのある section だけが並ぶ（部署・月中の推移などは省略される）
    assert _headings(details) == ["全ユーザ", "詳細利用状況", "組織内の分布（参考値）", "感度分析"]


def test_details_has_no_discussion_section(all_in):
    """考察は report.md にだけ置く（考察の保全の対象を1つに保つ）。"""
    _, _, details_path = all_in
    assert "## 考察" not in details_path.read_text(encoding="utf-8")


def test_details_uses_lf_and_utf8(all_in):
    """改行は LF 固定（レポートの他の成果物と同じ規約）。"""
    _, _, details_path = all_in
    raw = details_path.read_bytes()
    assert b"\r" not in raw
    raw.decode("utf-8")
