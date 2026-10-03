"""速報モード（部分月データの一次判断）のテスト。

2026-06 は暦30日。--days 10 なら月末ペース換算は ×3.0。
損益分岐（mid: S_allowance=50, P_allowance=250）は換算需要 150 が境界。
"""

import pytest

from seat_analyzer.analyze import (
    OrgPreviewResult,
    WorkspaceContext,
    preview,
    preview_own_demand_users,
    preview_persons,
)
from seat_analyzer.cli import main
from seat_analyzer.report import (
    DASHBOARD,
    DETAILS,
    PREVIEW,
    PREVIEW_DASHBOARD,
    RECOMMENDATIONS,
    REPORT,
)

from .conftest import CONFIG, spend_row


def _label_of(result, email: str) -> str:
    return result.users.set_index("email").loc[email, "label"]


def test_projection_and_labels(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [
            spend_row("idle@x.jp", 0.5, net=0.0),      # ほぼ未使用 → 遊休候補
            spend_row("light@x.jp", 5.0, net=0.0),     # 換算15 → Standard内 → Standard候補
            spend_row("heavy@x.jp", 300.0, net=0.0),   # 換算900 → Premium妥当
            spend_row("edge@x.jp", 50.0, net=0.0),     # 換算150 = 分岐点 → 判断保留
        ]},
        members=["idle@x.jp,Premium", "light@x.jp,Premium",
                 "heavy@x.jp,Premium", "edge@x.jp,Premium", "zero@x.jp,Premium"],
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a")
    assert result.days_in_month == 30
    users = result.users.set_index("email")
    assert users.loc["light@x.jp", "api_cost_projected_usd"] == pytest.approx(15.0)
    assert _label_of(result, "idle@x.jp") == "遊休候補"
    assert _label_of(result, "zero@x.jp") == "遊休候補"   # spend に居ない members も対象
    assert _label_of(result, "light@x.jp") == "Standard候補"
    assert _label_of(result, "heavy@x.jp") == "Premium妥当"
    assert _label_of(result, "edge@x.jp") == "判断保留"
    assert result.summary["label_counts"]["遊休候補"] == 2


def test_standard_user_upgrade_direction(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("s-heavy@x.jp", 150.0, net=0.0)]},  # 換算450 → Premium検討
        members=["s-heavy@x.jp,Standard"],
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a")
    assert _label_of(result, "s-heavy@x.jp") == "Premium検討"


def test_billed_premium_counted(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("over@x.jp", 400.0, net=120.0)]},
        members=["over@x.jp,Premium"],
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a")
    assert result.summary["n_billed"] == 1
    assert result.users.set_index("email").loc["over@x.jp", "billed_observed_usd"] == 120.0


def test_days_out_of_range_raises(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Premium"],
    )
    with pytest.raises(ValueError, match="暦日数"):
        preview(input_dir, "2026-06", cfg, days_observed=31, org="org-a")


def _context(name: str, primary: bool, fixed: str | None = None) -> WorkspaceContext:
    return WorkspaceContext(name, primary, name, fixed, None, None)


def test_preview_extra_demand_changes_primary_judgment_but_not_own_totals(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Premium"],
    )
    alone = preview(input_dir, "2026-06", cfg, 10, "org-a")
    merged = preview(input_dir, "2026-06", cfg, 10, "org-a",
                     extra_demand={"a@x.jp": 300.0, "c@y.jp": 999.0})
    assert _label_of(alone, "a@x.jp") == "Standard候補"
    assert _label_of(merged, "a@x.jp") == "Premium妥当"
    assert "c@y.jp" not in set(merged.users["email"])
    assert merged.summary["total_api_observed_usd"] == alone.summary["total_api_observed_usd"]
    own = preview_own_demand_users(merged).set_index("email")
    assert own.loc["a@x.jp", "api_cost_projected_usd"] == alone.users.set_index("email").loc[
        "a@x.jp", "api_cost_projected_usd"]
    assert own.loc["b@x.jp", "api_cost_observed_usd"] == 0


def test_preview_combines_unrounded_demand_before_idle_check(cfg, make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 0.994)]},
                           members=["a@x.jp,Premium"])
    result = preview(input_dir, "2026-06", cfg, 10, "org-a",
                     extra_demand={"a@x.jp": 0.005})
    assert result.users.iloc[0]["api_cost_observed_usd"] == 1.0
    assert _label_of(result, "a@x.jp") == "遊休候補"


def test_preview_fixed_seat_keeps_idle_signal_and_excludes_grants(cfg, make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 40.0),
                                       spend_row("b@x.jp", 0.2)]},
                           members=["a@x.jp,Premium", "b@x.jp,Standard"])
    result = preview(input_dir, "2026-06", cfg, 10, "org-a",
                     workspace=_context("second", False, "premium"))
    assert _label_of(result, "a@x.jp") == "対象外（固定シート）"
    assert _label_of(result, "b@x.jp") == "遊休候補"
    assert result.grant_candidates == []


def test_preview_persons_uses_prices_and_secondary_seat_rule(cfg, make_input):
    root = make_input({"2026-06": [spend_row("a@x.jp", 10.0),
                                   spend_row("b@x.jp", 20.0)]},
                      members=["a@x.jp,Premium", "b@x.jp,Standard",
                               "c@y.jp,Standard"], org="org-x", workspace="main")
    make_input({"2026-06": [spend_row("a@x.jp", 30.0),
                            spend_row("b@x.jp", 5.0)]},
               members=["a@x.jp,Premium", "b@x.jp,Unassigned", "c@y.jp,Premium"],
               org="org-x", workspace="second")
    main = preview(root / "org-x" / "main", "2026-06", cfg, 10, "org-x",
                   extra_demand={"a@x.jp": 30.0, "b@x.jp": 5.0})
    second = preview(root / "org-x" / "second", "2026-06", cfg, 10, "org-x")
    org = OrgPreviewResult("org-x", "2026-06", "main", {"main": main, "second": second},
                           {"main": _context("main", True),
                            "second": _context("second", False)}, 10, 30)
    cfg["seats"]["standard"]["price_usd"] = 37
    cfg["seats"]["premium"]["price_usd"] = 143
    persons = preview_persons(org, cfg)
    assert list(persons["email"]) == ["a@x.jp", "b@x.jp", "c@y.jp"]
    a, b, c = (persons.set_index("email").loc[email] for email in
               ("a@x.jp", "b@x.jp", "c@y.jp"))
    assert a["seats"] == (("main", "premium"), ("second", "premium"))
    assert a["seat_cost_usd"] == 286
    assert a["secondary_ratio"] == pytest.approx(0.75)
    assert b["seat_cost_usd"] == 37 and b["secondary_ratio"] is None
    assert c["secondary_ratio"] is None


def _run_cli(input_dir, tmp_path, *extra):
    output_dir = tmp_path / "reports"
    rc = main([
        "analyze", "--config", CONFIG,
        "--input-dir", str(input_dir), "--output-dir", str(output_dir),
        *extra,
    ])
    return rc, output_dir


def test_cli_preview_writes_preview_files_only(make_input, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"], members_month="2026-07", org="org-new",
    )
    rc, out = _run_cli(input_dir, tmp_path, "--preview", "--days", "10")
    assert rc == 0
    org_out = out / "org-new"
    assert PREVIEW.path(org_out, "2026-07", "org-new").exists()
    assert PREVIEW_DASHBOARD.path(org_out, "2026-07", "org-new").exists()
    # 正式レポートには触れない
    for artifact in (REPORT, DETAILS, DASHBOARD, RECOMMENDATIONS):
        assert not artifact.path(org_out, "2026-07", "org-new").exists()
    md = PREVIEW.path(org_out, "2026-07", "org-new").read_text(encoding="utf-8")
    assert "org-new — 2026-07" in md.splitlines()[0]
    assert "×3.1" in md                                   # 31日/10日 の換算係数


def test_cli_preview_preserves_discussion(make_input, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"], members_month="2026-07", org="org-new",
    )
    rc, out = _run_cli(input_dir, tmp_path, "--preview", "--days", "10")
    path = PREVIEW.path(out / "org-new", "2026-07", "org-new")
    md = path.read_text(encoding="utf-8")
    path.write_text(md.split("\n## 考察\n")[0] + "\n## 考察\n\n記入済みの考察テキスト\n", encoding="utf-8")
    rc, _ = _run_cli(input_dir, tmp_path, "--preview", "--days", "12")
    assert rc == 0
    assert "記入済みの考察テキスト" in path.read_text(encoding="utf-8")


def _write_preview_with_discussion(path, body: str) -> None:
    """テスト用: 「## 考察」以降に body を持つ最小の preview.md を書く。"""
    path.write_text(f"# 見出し\n\n本文\n\n## 考察\n\n{body}\n", encoding="utf-8")


def test_preserve_discussion_keeps_filled_text_containing_placeholder_word(tmp_path):
    """考察本文に「未記入」という語（例: 部署未記入）を含んでも記入済みとして保持する。"""
    from seat_analyzer.report.document import _preserve_discussion

    path = tmp_path / "preview.md"
    _write_preview_with_discussion(path, "- 部署未記入のメンバーがいるため整備が必要\n\n### 評価\n本格運用中")
    new_md = "# 見出し\n\n本文（再生成）\n\n## 考察\n\n（未記入 — `/seat-analysis` を実行すると考察が追記されます）\n"
    merged = _preserve_discussion(new_md, path)
    assert "部署未記入のメンバーがいるため整備が必要" in merged
    assert "本文（再生成）" in merged                    # 本文側は再生成版で置き換わる
    assert "（未記入 —" not in merged                     # プレースホルダは残らない


def test_preserve_discussion_replaces_placeholder(tmp_path):
    """未記入プレースホルダのままなら新規 md（プレースホルダ入り）で差し替える。"""
    from seat_analyzer.report.document import _preserve_discussion

    path = tmp_path / "preview.md"
    _write_preview_with_discussion(
        path, "<!-- コメント -->\n（未記入 — `/seat-analysis preview <日数>` を実行すると考察が追記されます）")
    new_md = "# 見出し\n\n新本文\n\n## 考察\n\n新プレースホルダ本文\n"
    assert _preserve_discussion(new_md, path) == new_md


def test_preserve_discussion_no_marker_returns_new(tmp_path):
    """既存ファイルに「## 考察」marker が無ければ新規 md をそのまま返す。"""
    from seat_analyzer.report.document import _preserve_discussion

    path = tmp_path / "preview.md"
    path.write_text("# 見出し\n\n本文だけで考察セクションが無い\n", encoding="utf-8")
    new_md = "# 見出し\n\n新本文\n\n## 考察\n\n本文\n"
    assert _preserve_discussion(new_md, path) == new_md


def test_cli_days_requires_preview(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Premium"], org="org-x",
    )
    rc, _ = _run_cli(input_dir, tmp_path, "--days", "10")
    assert rc == 1
    assert "--preview 専用" in capsys.readouterr().err
    rc, _ = _run_cli(input_dir, tmp_path, "--preview")
    assert rc == 1
    assert "--days" in capsys.readouterr().err
