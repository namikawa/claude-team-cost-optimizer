"""複数 workspace の組織の成果物（設計書 §26.7・Step 47）。

golden（tests/golden/full-all/org-c/）はバイト列を固定するだけなので、ここでは
「どの列がどこにあるか」「どの節を出し、どの節を出さないか」「並びの規則」
「単一 workspace の組織には何も足さないこと」を性質として書く。

入力は examples/input/org-c（2つの Team スペースを運用する合成組織。設定は
examples/config.yaml）と、tests の合成データだけを使う。
"""

import csv
import io
import re
from pathlib import Path

import pandas as pd
import pytest
import yaml

from seat_analyzer.analyze import (
    CONTINUATION_WATCH,
    CONTINUE,
    PAYOUT_CANDIDATE,
    PAYOUT_NO_EVIDENCE,
    PAYOUT_UNNEEDED,
    PAYOUT_WATCH,
    RETURN_CANDIDATE,
    STATUS_CHANGE,
    STATUS_FIXED_SEAT,
    WAITING,
    analyze,
    analyze_org,
    single_org_result,
    summarize_org,
)
from seat_analyzer.cli import main
from seat_analyzer.config import load_config
from seat_analyzer.report import (
    DASHBOARD,
    DETAILS,
    RECOMMENDATIONS,
    REPORT,
    USAGE_SUMMARY,
    write_all,
    write_decision_evidence,
    write_org_summary,
)
from seat_analyzer.report.format import _account_rows, _group_summary_rows
from seat_analyzer.report.spaces import CONTINUATION_ORDER, PAYOUT_ORDER

from .conftest import REPO_ROOT, spend_row

EXAMPLES_INPUT = REPO_ROOT / "examples" / "input"
EXAMPLES_CONFIG = str(REPO_ROOT / "examples" / "config.yaml")
ORG, MONTH = "org-c", "2026-08"


@pytest.fixture(scope="module")
def examples_cfg() -> dict:
    return load_config(EXAMPLES_CONFIG)


@pytest.fixture(scope="module")
def org_c(examples_cfg):
    """org-c 2026-08 の組織単位の分析結果（V2 の材料つき）。"""
    return analyze_org(EXAMPLES_INPUT / ORG, MONTH, examples_cfg, ORG, decision_context=True)


@pytest.fixture(scope="module")
def outputs(tmp_path_factory):
    """org-c 2026-08 を CLI で書いた成果物（種別 → 本文）。"""
    output_dir = tmp_path_factory.mktemp("spaces") / "reports"
    rc = main([
        "analyze", "--config", EXAMPLES_CONFIG, "--input-dir", str(EXAMPLES_INPUT),
        "--output-dir", str(output_dir), "--org", ORG, "--month", MONTH,
        "--decision-version", "v2",
    ])
    assert rc == 0
    org_out = output_dir / ORG
    return {
        "report": REPORT.path(org_out, MONTH, ORG).read_text(encoding="utf-8"),
        "details": DETAILS.path(org_out, MONTH, ORG).read_text(encoding="utf-8"),
        "dashboard": DASHBOARD.path(org_out, MONTH, ORG).read_text(encoding="utf-8"),
        "recommendations": RECOMMENDATIONS.path(org_out, MONTH, ORG)
        .read_text(encoding="utf-8-sig"),
        "usage": USAGE_SUMMARY.path(org_out, MONTH, ORG).read_text(encoding="utf-8-sig"),
        "evidence": (org_out / MONTH / f"decision-evidence-202608-{ORG}.csv")
        .read_text(encoding="utf-8-sig"),
    }


def _headings(md: str, level: str = "##") -> list[str]:
    return re.findall(rf"^{level} (.+)$", md, re.MULTILINE)


def _section(md: str, heading: str) -> str:
    """「## heading」から次の「## 」までの本文。"""
    start = md.index(f"## {heading}\n")
    rest = md[start + 3:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def _csv_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


# --- 合成組織の期待判定（設計 §5。判定規則は Step 45 のまま） ---

def test_sample_judgments_match_the_expected_spread(org_c):
    layer = org_c.persons
    payout = {j.email.split("@")[0]: j.status for j in layer.payout}
    assert payout == {
        "kimura": PAYOUT_CANDIDATE, "nishi": PAYOUT_WATCH,
        "ota": PAYOUT_UNNEEDED, "morita": PAYOUT_UNNEEDED,
        "sakai": PAYOUT_NO_EVIDENCE, "takagi": PAYOUT_NO_EVIDENCE,
    }
    continuation = {j.email.split("@")[0]: j for j in layer.continuation}
    assert {name: j.status for name, j in continuation.items()} == {
        "fujii": CONTINUE, "kubota": CONTINUE, "hoshino": RETURN_CANDIDATE,
        "ueda": RETURN_CANDIDATE, "yagi": WAITING,
    }
    assert continuation["hoshino"].saving_usd == 105.0
    assert continuation["hoshino"].complete_months == 1
    assert continuation["ueda"].saving_usd == 124.5
    assert continuation["ueda"].idle is True
    assert continuation["yagi"].complete_months == 0
    assert continuation["fujii"].over_primary_cap_months == ("2026-07", "2026-08")
    assert continuation["kubota"].over_primary_cap_months == ("2026-08",)
    assert [r.email for r in layer.billed_with_secondary] == ["fujii@example.co.jp"]

    main_users = org_c.workspaces["main"].users.set_index("email")
    assert list(main_users.index[main_users["status"] == STATUS_CHANGE]) == [
        "morita@example.co.jp"]
    # 主が Standard で副が Premium の人: 主の行は合算 $380 で判定して現状維持
    assert main_users.loc["kubota@example.co.jp", "api_cost_usd"] == 380.0
    assert main_users.loc["takagi@example.co.jp", "confidence"] == "中"
    second = org_c.workspaces["second"].users
    assert set(second["status"]) == {STATUS_FIXED_SEAT}
    assert len(second) == 5


def test_person_rows_hold_both_accounts(org_c):
    frame = org_c.persons.frame.set_index("email")
    fujii = frame.loc["fujii@example.co.jp"]
    assert fujii["api_cost_usd"] == 900.0
    assert round(fujii["secondary_ratio"], 2) == 0.33
    kubota = frame.loc["kubota@example.co.jp"]
    assert (kubota["primary_api_cost_usd"], kubota["secondary_api_cost_usd"]) == (80.0, 300.0)
    yagi = frame.loc["yagi@example.co.jp"]
    assert yagi["primary_seat"] == "" and yagi["primary_api_cost_usd"] == 0.0


# --- 組織単位の集計 ---

def test_summarize_org_counts_people_and_accounts(org_c):
    summary = summarize_org(org_c)
    assert (summary["n_persons"], summary["n_accounts"]) == (11, 15)
    assert [row["name"] for row in summary["workspaces"]] == ["main", "second"]
    main_row, second_row = summary["workspaces"]
    assert main_row["primary"] is True and second_row["fixed_seat"] == "premium"
    total = summary["total"]
    # 需要は各アカウント自身の需要の和（合算値を二重に数えない）
    assert total["total_api_cost_usd"] == round(
        main_row["total_api_cost_usd"] + second_row["total_api_cost_usd"], 2)
    assert total["seat_cost_now_usd"] == 1675.0
    assert total["n_change_recommended"] == 1


def test_summarize_org_marks_a_skipped_workspace(make_input, tmp_path):
    """まだ始まっていない workspace の行は数値を持たない（None）。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x", workspace="main")
    make_input({"2026-07": [spend_row("a@x.jp", 10.0)]},
               members=["a@x.jp,Premium"], org="org-x", workspace="second")
    cfg = _workspace_cfg(tmp_path)
    org = analyze_org(input_dir / "org-x", "2026-06", cfg, "org-x")
    summary = summarize_org(org)
    second = summary["workspaces"][1]
    assert second["skipped"] is True
    assert second["n_members"] is None and second["months_used"] is None
    assert summary["n_accounts"] == 1


def test_summarize_org_without_a_person_layer_counts_accounts(cfg, make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium", "b@x.jp,Standard"])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    summary = summarize_org(single_org_result(result))
    assert (summary["n_persons"], summary["n_accounts"]) == (2, 2)


def _workspace_cfg(tmp_path: Path, **second) -> dict:
    path = tmp_path / "config-spaces.yaml"
    path.write_text(yaml.safe_dump({"organizations": {"org-x": {"workspaces": {
        "main": {"primary": True, "label": "主スペース"},
        "second": {"label": "副スペース", "fixed_seat": "premium", **second},
    }}}}, allow_unicode=True), encoding="utf-8")
    return load_config(str(path))


# --- 列（スペース列の仕様表 §3.5） ---

def test_csv_workspace_column_follows_email(outputs):
    for kind in ("recommendations", "usage", "evidence"):
        header = next(csv.reader(io.StringIO(outputs[kind])))
        assert header[:2] == ["email", "workspace"], kind
    recommendations = _csv_rows(outputs["recommendations"])
    assert {r["workspace"] for r in recommendations} == {"main", "second"}
    # 識別子はディレクトリ名（表示名は CSV に出さない）
    assert "workspace_label" not in recommendations[0]
    # 連結で欠けた任意列（code-analytics は main にだけある）は空欄
    second = [r for r in recommendations if r["workspace"] == "second"]
    assert {r["loc_with_cc"] for r in second} == {""}
    main_rows = [r for r in recommendations if r["workspace"] == "main"]
    assert {r["loc_with_cc"] for r in main_rows if r["email"].startswith("fujii")} == {"4200"}
    # usage-summary は workspace ごとに email 昇順で、主が先
    usage = _csv_rows(outputs["usage"])
    workspaces = [r["workspace"] for r in usage]
    assert workspaces == sorted(workspaces, key=["main", "second"].index)
    for name in ("main", "second"):
        emails = [r["email"] for r in usage if r["workspace"] == name]
        assert emails == sorted(emails)


def test_csv_workspace_column_exists_before_the_second_space_starts(make_input, tmp_path):
    """副が始まっていない月でも列の形を変えない（月をまたいだ突き合わせのため）。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x", workspace="main")
    make_input({"2026-07": [spend_row("a@x.jp", 10.0)]},
               members=["a@x.jp,Premium"], org="org-x", workspace="second")
    cfg = _workspace_cfg(tmp_path)
    org = analyze_org(input_dir / "org-x", "2026-06", cfg, "org-x", decision_context=True)
    assert list(org.workspaces) == ["main"] and org.has_multiple_workspaces
    out = tmp_path / "reports"
    paths = write_all(org, out)
    for key in ("csv", "usage"):
        header = next(csv.reader(io.StringIO(paths[key].read_text(encoding="utf-8-sig"))))
        assert header[:2] == ["email", "workspace"], key
    evidence = tmp_path / "evidence.csv"
    write_decision_evidence((), evidence, workspace_column=org.has_multiple_workspaces)
    assert evidence.read_text(encoding="utf-8-sig").startswith("email,workspace,")
    # 未開始の workspace はスペース別の表に注記つきで残る
    report_text = paths["markdown"].read_text(encoding="utf-8")
    assert "副スペース（Premium 固定）: 未開始（対象月以前のデータなし）" in report_text


def test_markdown_tables_put_the_space_label_after_the_user(outputs):
    details = outputs["details"]
    for heading in ("全ユーザ", "詳細利用状況"):
        header = _section(details, heading).split("\n")[2]
        assert header.startswith("| ユーザ | スペース |"), heading
    report_text = outputs["report"]
    assert _section(report_text, "シート変更推奨").split("\n")[2].startswith(
        "| ユーザ | スペース |")
    continuation = _section(report_text, "複数スペースの利用")
    assert "| ユーザ | スペース | シート | 判定 |" in continuation
    # 表示名だけを出し、ディレクトリ名と併記しない
    assert "| fujii@example.co.jp | 副スペース | Premium | 継続 |" in continuation


def test_missing_optional_cells_show_a_dash(outputs):
    rows = [line for line in _section(outputs["details"], "全ユーザ").split("\n")
            if line.startswith("| yagi@example.co.jp |")]
    assert rows and rows[0].endswith("| — | — |")
    detail = [line for line in _section(outputs["details"], "詳細利用状況").split("\n")
              if line.startswith("| yagi@example.co.jp |")]
    assert detail and "| 副スペース |" in detail[0] and "| — |" in detail[0]


def test_account_rows_keep_integer_columns(org_c):
    combined = _account_rows(org_c, {n: r.users for n, r in org_c.workspaces.items()})
    columns = list(combined.columns)
    assert columns[columns.index("email") + 1:columns.index("email") + 3] == [
        "workspace", "workspace_label"]
    assert isinstance(combined["loc_with_cc"].dtype, pd.Int64Dtype)
    assert combined["workspace"].tolist() == (
        ["main"] * len(org_c.workspaces["main"].users)
        + ["second"] * len(org_c.workspaces["second"].users))


# --- 節（workspace ごと・固定シート） ---

def test_report_sections_per_workspace(outputs):
    headings = _headings(outputs["report"])
    assert headings == [
        "サマリ",
        "前月からの変化（主スペース）",
        "前月からの変化（副スペース）",
        "シート変更推奨",
        "複数スペースの利用",
        "注意事項",
        "データ検証・警告",
        "考察",
    ]
    assert "### スペース別" in outputs["report"]
    assert "| 対象メンバー数 | 11 名（アカウント 15） |" in outputs["report"]
    assert "| 追加クレジット（主スペース） |" in outputs["report"]
    assert ("| 判定に使用した月 | 主スペース: 2026-07, 2026-08 / 副スペース: 2026-07, 2026-08"
            in outputs["report"])


def test_fixed_seat_space_has_no_sensitivity_or_grant(outputs):
    """固定シートの workspace は損益分岐判定をしていないので、その派生物を出さない。"""
    details_headings = _headings(outputs["details"])
    assert "感度分析（主スペース）" in details_headings
    assert not any(h.startswith("感度分析（副スペース") for h in details_headings)
    assert "追加クレジット付与候補（副スペース）" not in outputs["dashboard"]
    assert "追加クレジット付与候補（主スペース）" in outputs["dashboard"]
    # 感度分析は判定していない状態を「全員一致」と見せない
    sensitivity = _section(outputs["details"], "感度分析（主スペース）")
    assert "takagi@example.co.jp" in sensitivity


def test_legends_explain_fixed_seats_and_merged_demand(outputs):
    for text in (_section(outputs["report"], "シート変更推奨"),
                 _section(outputs["details"], "全ユーザ")):
        assert "**対象外（固定シート）**" in text
        assert "主の行の API換算需要は全スペースの合算" in text
    notes = _section(outputs["report"], "注意事項")
    assert "対象外（固定シート）は運用方針でシート種別を固定したスペース" in notes
    assert "人数以外の件数" in notes and "アカウント単位" in notes


def test_report_and_details_split_the_person_layer(outputs):
    """判定は report、人の表（全員1人1行）は details に置く。"""
    assert "## 人別の利用" in outputs["details"]
    assert "## 人別の利用" not in outputs["report"]
    assert "details-202608-org-c.md の「人別の利用」" in outputs["report"]


# --- 並び ---

def _table_rows(section: str, heading: str) -> list[list[str]]:
    start = section.index(f"### {heading}\n")
    rows = []
    for line in section[start:].split("\n")[4:]:
        if not line.startswith("| "):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_payout_table_order_and_unneeded_count(outputs):
    section = _section(outputs["report"], "複数スペースの利用")
    rows = _table_rows(section, "払い出し判定（副を持たない人）")
    assert [(r[0].split("@")[0], r[1]) for r in rows] == [
        ("kimura", PAYOUT_CANDIDATE), ("nishi", PAYOUT_WATCH),
        ("sakai", PAYOUT_NO_EVIDENCE), ("takagi", PAYOUT_NO_EVIDENCE),
    ]
    assert "- 不要 2 名（主の実課金 $0）" in section
    assert PAYOUT_ORDER == (PAYOUT_CANDIDATE, PAYOUT_WATCH, PAYOUT_NO_EVIDENCE)
    assert ("主のシート（Standard）が払い出すシート種別（Premium）と違う"
            in rows[2][-1])


def test_continuation_table_order(outputs):
    section = _section(outputs["report"], "複数スペースの利用")
    rows = _table_rows(section, "継続判定（副にシートを持つ人）")
    assert [(r[0].split("@")[0], r[3]) for r in rows] == [
        ("ueda", RETURN_CANDIDATE), ("hoshino", RETURN_CANDIDATE),
        ("yagi", WAITING), ("fujii", CONTINUE), ("kubota", CONTINUE),
    ]
    assert CONTINUATION_ORDER == (RETURN_CANDIDATE, CONTINUATION_WATCH, WAITING, CONTINUE)
    notes = {r[0].split("@")[0]: r[-1] for r in rows}
    assert notes["ueda"] == "遊休"
    assert notes["fujii"] == "主の上限超え: 2026-07, 2026-08"
    assert notes["kubota"] == "主の上限超え: 2026-08"


def test_person_table_order_and_held_seats(outputs):
    section = _section(outputs["details"], "人別の利用")
    lines = [line for line in section.split("\n")[3:] if line.startswith("| ")]
    emails = [line.split("|")[1].strip().split("@")[0] for line in lines]
    # 判定の表示順（変更推奨が先）→ 需要の降順 → email
    assert emails[0] == "morita"
    assert emails.index("fujii") < emails.index("kimura")  # 同額 $900 は email 順
    assert emails[-1] == "yagi"  # 固定シートだけを持つ人は判定の表示順で後ろ
    yagi = lines[-1]
    assert "| 主スペース: — / 副スペース: Premium |" in yagi
    fujii = next(line for line in lines if line.startswith("| fujii@"))
    assert "| 主スペース: Premium / 副スペース: Premium |" in fujii
    assert "| 33% |" in fujii
    # 副にアカウントが無い人の比率は —（副をほぼ使っていない人の 0% と区別する）
    cells = {line.split("|")[1].strip().split("@")[0]: [c.strip() for c in line.split("|")]
             for line in lines}
    ratio = cells["fujii"].index("33%")
    assert cells["kimura"][ratio] == "—"
    assert cells["ueda"][ratio] == "0%"


def test_group_summary_breaks_ties_by_group_name():
    users = pd.DataFrame([
        {"email": "b@x.jp", "department": "B部", "status": "現状維持",
         "current_seat": "standard", "api_cost_usd": 10.0, "billed_extra_usd": 0.0,
         "monthly_saving_usd": None},
        {"email": "a@x.jp", "department": "A部", "status": "現状維持",
         "current_seat": "standard", "api_cost_usd": 10.0, "billed_extra_usd": 0.0,
         "monthly_saving_usd": None},
    ])
    rows = _group_summary_rows(users, {"seat_price_standard_usd": 25.0}, "department")
    assert [r["group"] for r in rows] == ["A部", "B部"]


# --- dashboard ---

def test_dashboard_spaces_tab_and_cards(outputs):
    html = outputs["dashboard"]
    assert re.findall(r'data-tab="([a-z]+)">', html)[:6] == [
        "overview", "actions", "members", "org", "spaces", "notes"]
    assert '<div class="v">11</div><div class="l">人（アカウント 15）</div>' in html
    for title in ("スペース別", "人別の利用", "払い出し判定（副を持たない人）",
                  "継続判定（副にシートを持つ人）", "副を持ちながら主で実課金が発生した人",
                  "ユーザ別 API 換算コスト（主スペース）",
                  "ユーザ別 API 換算コスト（副スペース）"):
        assert f"<h2>{title}</h2>" in html, title
    assert "<th>ユーザ</th><th>スペース</th>" in html
    # 判定の読み方は前提と注意に置く
    notes = html[html.index('data-tab="notes" role="tabpanel"'):]
    assert "払い出し判定は、副にアカウントを持たず" in notes
    assert "判定に使用した月: 主スペース: 2026-07, 2026-08 / 副スペース: 2026-07, 2026-08" in notes


def test_dashboard_shows_only_local_parts_of_emails(outputs):
    html = outputs["dashboard"]
    shown = re.sub(r'title="[^"]*"', "", html)
    assert "@example.co.jp" not in shown


# --- 単一 workspace の組織には何も足さない ---

def test_single_workspace_org_writes_the_same_bytes(cfg, tmp_path):
    """OrgAnalysisResult を渡しても、workspace が1つなら AnalysisResult と同じ出力。"""
    org_input = EXAMPLES_INPUT / "org-b"
    direct = analyze(org_input, "2026-08", cfg, org="org-b")
    wrapped = analyze_org(org_input, "2026-08", cfg, "org-b")
    assert not wrapped.has_multiple_workspaces
    a = write_all(direct, tmp_path / "a")
    b = write_all(wrapped, tmp_path / "b")
    for key in a:
        assert a[key].read_bytes() == b[key].read_bytes(), key
    s1 = write_org_summary([direct, direct], tmp_path / "s1")
    s2 = write_org_summary([wrapped, wrapped], tmp_path / "s2")
    assert s1.read_bytes() == s2.read_bytes()
    assert "アカウント" not in s1.read_text(encoding="utf-8")


def test_org_summary_counts_people_and_accounts(org_c, cfg, tmp_path):
    other = analyze(EXAMPLES_INPUT / "org-b", "2026-08", cfg, org="org-b")
    path = write_org_summary([other, org_c], tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "| 11 名（アカウント 15） |" in text
    n_b = len(other.users)
    assert f"**{n_b + 11} 名（アカウント {n_b + 15}）**" in text
