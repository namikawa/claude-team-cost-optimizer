"""apply 後の正式成果物の V2 表示と、書き込み前の検査。入力は合成データだけを使う。"""

import csv
import html
import re
from dataclasses import replace

import pytest

from seat_analyzer import decision_evidence, report, seat_changes
from seat_analyzer.analyze import (
    STATUS_CHANGE,
    STATUS_EXCLUDED,
    STATUS_FIXED_SEAT,
    STATUS_SECONDARY,
    analyze,
    analyze_org,
    preview,
)
from seat_analyzer.report.format import _decision_version
from seat_analyzer.report.html import _DASHBOARD_JS, _STATUS_BADGE_CLASS
from seat_analyzer.report.text import STATUS_ORDER

from .conftest import spend_row
from .test_decision_evidence import _with_uuid, _write_info
from .test_persons import _cfg as _workspace_cfg

_NO_CHANGES = seat_changes.SeatChanges(events=[], unclassified=[])
_V1_ONLY = (
    "Standard時", "Premium時", "Std時", "Prem時", "⚠️上限?", "⚠ = 上限到達疑い",
    "上限到達疑い（Standard）", "3シナリオ", "mid シナリオ", "感度分析",
    "モデル超過見込み", "実効込み量の実測候補", "損益分岐判定", "ヒステリシス",
)


def _applied(root, cfg, month="2026-06"):
    """正式分析 → evaluate_org → apply（CLI への結線は次の Step）。"""
    org = analyze_org(root, month, cfg, "org-x", decision_context=True, decision_version="v2")
    changes = seat_changes.detect_from_input(root, cfg)
    evidence = decision_evidence.evaluate_org(org, changes, cfg)
    return decision_evidence.apply(org, evidence, cfg), evidence


def _read_outputs(result, output_dir):
    return {key: path.read_text(encoding="utf-8")
            for key, path in report.write_all(result, output_dir).items()}


def _markdown_rows(source):
    """ユーザ判定表の行を、ヘッダの列名と対応づける。"""
    header = None
    rows = []
    for line in source.splitlines():
        if line.startswith("| ユーザ |"):
            header = [c.strip() for c in line.strip("|").split("|")]
        elif line.startswith("| ") and header and "理由" in header:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == len(header):
                rows.append(dict(zip(header, cells, strict=True)))
        elif line and not line.startswith("|"):
            header = None
    return rows


def _recommendations(source):
    """推奨一覧カードだけを抜き出す。"""
    return source.split("<h2>推奨一覧</h2>", 1)[1].split("</section>", 1)[0]


def _html_rows(source):
    rows = {}
    for row in re.findall(r"<tr>\s*(.*?)\s*</tr>", _recommendations(source), re.DOTALL):
        email = re.search(r'<td class="user" title="([^"]+)">', row)
        if email:
            cells = [html.unescape(re.sub(r"<[^>]*>", "", cell)).strip()
                     for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.DOTALL)]
            rows[html.unescape(email[1])] = cells
    return rows


def _assert_no_v1(outputs):
    for key in ("markdown", "details", "html"):
        for word in _V1_ONLY:
            assert word not in outputs[key], (key, word)


@pytest.fixture
def v2_result(make_input, cfg):
    """昇格・降格・付与・設定確認・現状維持・対象外を含む単一 workspace。"""
    names = ("upgrade", "idle", "grant", "review", "keep", "excluded", "observe")
    emails = [f"{name}@example.com" for name in names]
    seats = ("Standard", "Premium", "Standard", "Standard", "Standard", "Unassigned", "Premium")
    root = make_input({
        "2026-04": [_with_uuid(email, 10, f"id-{i}", net=0) for i, email in enumerate(emails)],
        "2026-05": [_with_uuid(email, 10, f"id-{i}", net=0) for i, email in enumerate(emails)],
        "2026-06": [_with_uuid(email, cost, f"id-{i}", net=net)
                    for i, (email, cost, net) in enumerate(zip(
                        emails, (720, 0, 720, 680, 10, 0, 430), (160, 0, 0, 0, 0, 0, 0), strict=True))],
    }, members=[f"{email},{seat}" for email, seat in zip(emails, seats, strict=True)])
    _write_info(root, [f"{email},,,,{cap}," for email, cap in zip(
        emails, (200, 0, 0, "", 0, 0, 0), strict=True)])
    applied, evidence = _applied(root, cfg)
    return applied, evidence, root


def test_v2_formal_outputs_and_reason_cells(v2_result, tmp_path):
    result, evidence, _ = v2_result
    outputs = _read_outputs(result, tmp_path / "out")
    _assert_no_v1(outputs)
    expected = {r.email: "; ".join(decision_evidence.REASON_LABELS[code]
                                  for code in r.decision.reason_codes) for r in evidence}
    csv_rows = list(csv.DictReader(outputs["csv"].lstrip("\ufeff").splitlines()))
    assert {r["email"]: r["reason"] for r in csv_rows} == expected
    details_rows = _markdown_rows(outputs["details"])
    assert {r["ユーザ"]: r["理由"] for r in details_rows} == expected
    changed = result.workspaces[result.primary].users
    changed = set(changed.loc[changed.status == STATUS_CHANGE, "email"])
    report_rows = _markdown_rows(outputs["markdown"])
    assert {r["ユーザ"]: r["理由"] for r in report_rows} == {e: expected[e] for e in changed}
    assert {e: cells[-1] for e, cells in _html_rows(outputs["html"]).items()} == expected
    header = next(line for line in outputs["details"].splitlines() if line.startswith("| ユーザ |"))
    assert "削減/月 | 判定 | 確度 | 理由 |" in header
    assert "<th>判定</th><th>理由</th>" in _recommendations(outputs["html"])
    assert '<td class="wrap">' in _recommendations(outputs["html"])
    assert "<!--" not in outputs["html"]
    assert "<script src=" not in outputs["html"]
    # スクリプトは判定セルをクラスで見つけるため、理由列が後に増えても列番号に依存しない。
    assert '.judge .badge' in _DASHBOARD_JS
    assert 'title="upgrade@example.com">upgrade</td>' in outputs["html"]
    for key in ("markdown", "details", "html"):
        assert "方針線（$450/月）" in outputs[key]
        assert "方針線を ±$100" in outputs[key]
    for key in ("markdown", "html"):
        assert "2026-04, 2026-05, 2026-06（判定に要る完全月数の下限: 昇格 1・降格 2）" in outputs[key]
    assert "シート差額 $100/月" in outputs["html"]
    assert "判定は V2 判定で、込み利用量（allowance）の推定は使っていません" in outputs["markdown"]
    assert "変更後の従量課金を含みません（負は費用増）" in outputs["markdown"]
    assert "組織に共通の履歴" in outputs["markdown"]
    assert "組織に共通の履歴" in outputs["html"]


def test_v2_policy_prices_and_month_counts_come_from_config(v2_result, cfg, tmp_path):
    _, _, root = v2_result
    cfg["decision_v2"]["premium_justification_usd"] = 575.25
    cfg["decision_v2"]["upgrade"]["min_complete_months"] = 2
    cfg["decision_v2"]["downgrade"]["min_complete_months"] = 3
    # summary の単価が表示に渡ることも、既定値と異なる値で確かめる。
    cfg["seats"]["standard"]["price_usd"] = 30
    cfg["seats"]["premium"]["price_usd"] = 140
    result, _ = _applied(root, cfg)
    outputs = _read_outputs(result, tmp_path / "settings")
    assert "方針線（$575.25/月）" in outputs["details"]
    assert "方針線（$575.25/月）" in outputs["html"]
    assert "昇格 2・降格 3）" in outputs["markdown"]
    assert "シート単価: Standard $30 / Premium $140（月払い）。シート差額 $110/月。" in outputs["html"]


def test_v2_two_credit_lists(v2_result, tmp_path):
    result, _, _ = v2_result
    outputs = _read_outputs(result, tmp_path / "out")
    md = outputs["markdown"]
    assert "## 追加クレジットの付与候補と設定確認" in md
    assert "### 上限つき付与" in md and "### 設定確認" in md
    grant = result.workspaces[result.primary].grant_candidates[0]
    review = result.workspaces[result.primary].credit_reviews[0]
    assert grant["email"] == "grant@example.com"
    assert review["email"] == "review@example.com"
    assert f"| grant@example.com | ${grant['demand_usd']:,.2f} | $150 |" in md
    assert f"| review@example.com | ${review['demand_usd']:,.2f} | $0.00 |" in md
    assert 'title="grant@example.com">grant</span>（需要 $720・推奨初期上限 $150）' in outputs["html"]
    assert 'title="review@example.com">review</span>（需要 $680・実課金 $0.00）' in outputs["html"]
    assert "管理画面で設定を確かめ、members-info.csv" in md
    assert md.index("## 前月からの変化") < md.index("## 追加クレジットの付与候補と設定確認")


@pytest.mark.parametrize("cap, cost, present", [(0, 10, None), (0, 720, "上限つき付与"),
                                               ("", 720, "設定確認")])
def test_v2_credit_empty_sections(make_input, cfg, tmp_path, cap, cost, present):
    root = make_input({"2026-06": [spend_row("user@example.com", cost, net=0)]},
                      members=["user@example.com,Standard"])
    _write_info(root, [f"user@example.com,,,,{cap},"])
    result, _ = _applied(root, cfg)
    outputs = _read_outputs(result, tmp_path / "out")
    if present is None:
        assert "## 追加クレジットの付与候補と設定確認" not in outputs["markdown"]
        assert outputs["html"].count('class="empty">該当者なし。</div>') == 2
    else:
        assert outputs["markdown"].count("該当なし。") >= 1
        empty = "設定確認" if present == "上限つき付与" else "上限つき付与"
        assert "該当なし。" in outputs["markdown"].split(f"### {empty}", 1)[1].split("## ", 1)[0]
    assert "<h2>追加クレジットの付与候補と設定確認</h2>" in outputs["html"]
    assert "判定不能" not in outputs["html"]


def test_v2_multi_workspace_labels_and_cards(make_input, tmp_path):
    root = make_input({"2026-05": [spend_row("user@example.com", 10, net=0)],
                       "2026-06": [spend_row("user@example.com", 720, net=0)]},
                      members=["user@example.com,Standard", "off@example.com,Unassigned"],
                      org="org-x", workspace="main")
    for name in ("fixed", "second"):
        make_input({"2026-05": [spend_row("user@example.com", 10, net=0)],
                    "2026-06": [spend_row("user@example.com", 10, net=0)]},
                   members=["user@example.com,Premium"], org="org-x", workspace=name)
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True, "label": "主拠点"},
                                    "fixed": {"fixed_seat": "premium", "label": "固定拠点"},
                                    "second": {"label": "副拠点"}})
    _write_info(root / "org-x", ["user@example.com,,,,0,", "off@example.com,,,,0,"])
    result, _ = _applied(root / "org-x", cfg)
    outputs = _read_outputs(result, tmp_path / "out")
    _assert_no_v1(outputs)
    assert STATUS_ORDER.index(STATUS_FIXED_SEAT) + 1 == STATUS_ORDER.index(STATUS_SECONDARY)
    assert STATUS_ORDER.index(STATUS_SECONDARY) + 1 == STATUS_ORDER.index(STATUS_EXCLUDED)
    assert _STATUS_BADGE_CLASS[STATUS_SECONDARY] == "b-keep"
    rows = _markdown_rows(outputs["details"])
    statuses = [r["判定"] for r in rows]
    assert statuses.index(STATUS_FIXED_SEAT) < statuses.index(STATUS_SECONDARY) < statuses.index(STATUS_EXCLUDED)
    assert '<span class="badge b-keep">対象外（副スペース）</span>' in outputs["html"]
    for key in ("details", "html"):
        assert "アカウントの V2 判定" in outputs[key]
        assert "V1 判定" not in outputs[key]
    assert "Standard / Premium のシート判定は行わない" in outputs["details"]
    assert "副スペース（副拠点）" in outputs["details"]
    assert "シート判定と追加クレジットの付与候補・設定確認の対象にしていません" in outputs["markdown"]
    assert "人数以外の件数（変更推奨・要観察・一覧の件数）" in outputs["html"]
    assert outputs["html"].count("<h2>追加クレジットの付与候補と設定確認") == 1
    assert "<h2>追加クレジットの付与候補と設定確認（主拠点）</h2>" in outputs["html"]
    assert "主拠点: 2026-05, 2026-06（判定に要る完全月数の下限" in outputs["markdown"]
    assert "副拠点: 2026" not in outputs["markdown"]


def test_v2_stall_observation_survives(make_snapshots, cfg, tmp_path):
    root = make_snapshots("2026-07", {
        "2026-07-13": [spend_row("stall@example.com", 45, net=0)],
        "2026-07-31": [spend_row("stall@example.com", 45.4, net=0)],
    }, members=["stall@example.com,Standard"])
    result, _ = _applied(root, cfg, "2026-07")
    outputs = _read_outputs(result, tmp_path / "out")
    _assert_no_v1(outputs)
    for key in ("details", "html"):
        assert "⚠️停止疑い" in outputs[key]
        assert "上限停止の可能性。停止時点の累積 $45.40" in outputs[key]
        assert "本人確認が必要です" in outputs[key]


def test_midmonth_warning_version_and_preview(make_snapshots, write_member_snapshots, cfg):
    root = make_snapshots("2026-07", {
        "2026-07-15": [spend_row("user@example.com", 10)],
        "2026-07-31": [spend_row("user@example.com", 20)],
    }, members=["user@example.com,Premium"])
    write_member_snapshots(root, {"2026-07-05": ["user@example.com,Standard"],
                                  "2026-07-31": ["user@example.com,Premium"]})
    v1 = analyze(root, "2026-07", cfg, org="org-x")
    v2 = analyze(root, "2026-07", cfg, org="org-x", decision_version="v2")
    pv = preview(root, "2026-07", cfg, org="org-x", days_observed=31)
    prefix = "月中にシート変更を検出した ユーザ 1 名（変更 1 件）: ['user@example.com']"
    tail = "（当月の損益分岐判定は最新スナップショット時点のシートで行うため参考値）"
    assert prefix in v2.warnings
    assert prefix + tail in v1.warnings
    assert prefix + tail in pv.warnings
    assert all(tail not in w for w in v2.warnings)


@pytest.mark.parametrize("writer", [report.write_all, report.write_org_summary])
@pytest.mark.parametrize("shape", ["org", "single"])
def test_unapplied_v2_fails_before_creating_output(make_input, cfg, tmp_path, writer, shape):
    root = make_input({"2026-06": [spend_row("user@example.com", 10, net=0)]},
                      members=["user@example.com,Standard"])
    result = analyze_org(root, "2026-06", cfg, "org-x", decision_context=True, decision_version="v2")
    result = result if shape == "org" else result.workspaces[result.primary]
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match=r"decision_evidence\.apply"):
        writer([result] if writer is report.write_org_summary else result, output)
    assert not output.exists()


@pytest.mark.parametrize("invalid", ["reason", "v1_column", "summary"])
def test_v2_shape_guard_checks_all_invariants(v2_result, tmp_path, invalid):
    result, _, _ = v2_result
    ws = result.workspaces[result.primary]
    if invalid == "reason":
        ws.users.drop(columns="reason", inplace=True)
    elif invalid == "v1_column":
        ws.users["rec_low"] = "standard"
    else:
        ws.summary["n_cap_suspected"] = 0
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match=r"decision_evidence\.apply"):
        report.write_all(result, output)
    assert not output.exists()


def test_empty_reasons_are_valid(make_input, cfg, tmp_path):
    root = make_input({"2026-06": [spend_row("user@example.com", 10, net=0)]},
                      members=["user@example.com,Standard"])
    _write_info(root, ["user@example.com,,,,0,"])
    result, _ = _applied(root, cfg)
    assert result.workspaces[result.primary].users.reason.tolist() == [""]
    _read_outputs(result, tmp_path / "out")
    assert report.write_org_summary([result], tmp_path / "summary-out").exists()


def test_skipped_primary_and_version_mismatch(make_input, tmp_path):
    root = make_input({"2026-07": [spend_row("user@example.com", 720, net=0)]},
                      members=["user@example.com,Standard"], org="org-x", workspace="main")
    make_input({"2026-06": [spend_row("user@example.com", 10, net=0)]},
               members=["user@example.com,Premium"], org="org-x", workspace="second")
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True, "label": "主拠点"}, "second": {}})
    result, evidence = _applied(root / "org-x", cfg)
    assert evidence == ()
    outputs = _read_outputs(result, tmp_path / "out")
    _assert_no_v1(outputs)
    assert "主拠点: —（判定に要る完全月数の下限" in outputs["markdown"]
    assert "<h2>追加クレジットの付与候補と設定確認" not in outputs["html"]
    assert report.write_org_summary([result], tmp_path / "summary-out").exists()
    other = replace(next(iter(result.workspaces.values())),
                    summary={"decision_version": "v1"})
    mixed = replace(result, workspaces={**result.workspaces, "other": other})
    with pytest.raises(ValueError, match="workspace 間"):
        report.write_all(mixed, tmp_path / "mixed")
    assert not (tmp_path / "mixed").exists()
    assert _decision_version(replace(result, workspaces={})) == "v1"


def test_org_summary_note_only_with_v2(v2_result, cfg, tmp_path):
    result, _, root = v2_result
    v1 = analyze_org(root, "2026-06", cfg, "org-x")
    note = "削減見込みはシート差額ベースの比較額で、変更後の従量課金を含みません（負は費用増）。"
    for index, results in enumerate(([v1], [result], [v1, result])):
        text = report.write_org_summary(results, tmp_path / f"summary-{index}").read_text(encoding="utf-8")
        assert (note in text) is (index > 0)
        if index:
            assert text.index("| **合計**") < text.index(note) < text.index("各組織の詳細は")


@pytest.mark.parametrize("multiple", [False, True])
def test_primary_fixed_seat_has_no_v2_grant_card(make_input, tmp_path, multiple):
    root = make_input({"2026-06": [spend_row("user@example.com", 720, net=0)]},
                      members=["user@example.com,Premium"], org="org-x", workspace="main")
    workspaces = {"main": {"primary": True, "fixed_seat": "premium"}}
    if multiple:
        make_input({"2026-06": [spend_row("user@example.com", 10, net=0)]},
                   members=["user@example.com,Standard"], org="org-x", workspace="second")
        workspaces["second"] = {}
    cfg = _workspace_cfg(tmp_path, workspaces)
    result, _ = _applied(root / "org-x", cfg)
    outputs = _read_outputs(result, tmp_path / "out")
    assert "<h2>追加クレジットの付与候補と設定確認" not in outputs["html"]


def test_v2_contiguous_history_and_negative_saving(make_input, cfg, tmp_path):
    root = make_input({
        "2026-02": [spend_row("user@example.com", 720, net=0)],
        "2026-05": [spend_row("user@example.com", 720, net=0)],
        "2026-06": [spend_row("user@example.com", 720, net=0)],
    }, members=["user@example.com,Standard"])
    _write_info(root, ["user@example.com,,,,0,"])
    result, _ = _applied(root, cfg)
    outputs = _read_outputs(result, tmp_path / "out")
    for key in ("markdown", "html"):
        assert "2026-05, 2026-06（判定に要る完全月数の下限" in outputs[key]
        assert "2026-02, 2026-05" not in outputs[key]
    assert "$-100.00" in outputs["markdown"]
    assert _html_rows(outputs["html"])["user@example.com"][4] == "$-100"
    assert "負は費用増" in outputs["markdown"]


@pytest.mark.parametrize("column", ["cost_if_standard_usd", "cost_if_premium_usd", "rec_low",
                                    "rec_high", "cap_suspected"])
def test_each_v1_column_is_rejected(v2_result, tmp_path, column):
    result, _, _ = v2_result
    result.workspaces[result.primary].users[column] = 0
    with pytest.raises(ValueError, match=r"decision_evidence\.apply"):
        report.write_org_summary([result], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()
