"""速報モード（部分月データの一次判断）のテスト。

2026-06 は暦30日。--days 10 なら月末ペース換算は ×3.0。
損益分岐（mid: S_allowance=50, P_allowance=250）は換算需要 150 が境界。
"""

import pytest

from seat_analyzer.analyze import preview
from seat_analyzer.cli import main

from .conftest import REPO_ROOT, spend_row

CONFIG = str(REPO_ROOT / "config.yaml")


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
    result = preview(input_dir, "2026-06", cfg, days_observed=10)
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
    result = preview(input_dir, "2026-06", cfg, days_observed=10)
    assert _label_of(result, "s-heavy@x.jp") == "Premium検討"


def test_billed_premium_counted(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("over@x.jp", 400.0, net=120.0)]},
        members=["over@x.jp,Premium"],
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=10)
    assert result.summary["n_billed"] == 1
    assert result.users.set_index("email").loc["over@x.jp", "billed_observed_usd"] == 120.0


def test_preview_standard_billed_flag(cfg, make_input, tmp_path):
    """Standard ユーザに実課金があると一次判断テーブルに ⚠️従量あり が出る。"""
    from seat_analyzer.report import write_preview

    input_dir = make_input(
        {"2026-06": [spend_row("s-over@x.jp", 60.0, net=40.0)]},
        members=["s-over@x.jp,Standard"],
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=10)
    paths = write_preview(result, tmp_path / "reports")
    md = paths["md"].read_text(encoding="utf-8")
    assert "⚠️従量あり" in md
    assert "⚠️超過済" not in md.split("## 一次判断テーブル")[1].split("\n\n")[0]
    # 凡例にも従量ありの行が追加されている
    assert "⚠️従量あり: Standard 等で従量課金が発生" in md


def test_preview_dashboard_html(cfg, make_input, tmp_path):
    """速報ダッシュボード HTML の主要要素を検証する。"""
    from seat_analyzer.report import write_preview

    input_dir = make_input(
        {"2026-06": [
            spend_row("s-over@x.jp", 60.0, net=40.0),   # Standard 実課金あり → ⚠️従量あり
            spend_row("p-heavy@x.jp", 300.0, net=0.0),  # Premium 妥当
        ]},
        members=["s-over@x.jp,Standard", "p-heavy@x.jp,Premium"],
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=10)
    paths = write_preview(result, tmp_path / "reports")
    assert paths["html"].name == "preview-dashboard.html"
    html = paths["html"].read_text(encoding="utf-8")
    # (a) タイトルに「速報」
    assert "速報" in html
    assert "<title>Claude Team シート速報プレビュー" in html
    # (b) 換算係数 ×3.0（30日/10日）表記
    assert "×3.0" in html
    # (c) メールのローカル部と一次判断ラベル
    assert "s-over" in html
    assert "p-heavy" in html
    assert "Premium妥当" in html
    # (d) 実課金ありの Standard ユーザに ⚠️従量あり
    assert "⚠️従量あり" in html


def test_days_out_of_range_raises(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Premium"],
    )
    with pytest.raises(ValueError, match="暦日数"):
        preview(input_dir, "2026-06", cfg, days_observed=31)


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
    report_dir = out / "org-new" / "2026-07"
    assert (report_dir / "preview.md").exists()
    assert (report_dir / "preview-dashboard.html").exists()
    assert not (report_dir / "report.md").exists()        # 正式レポートには触れない
    assert not (report_dir / "dashboard.html").exists()
    assert not (report_dir / "recommendations.csv").exists()
    md = (report_dir / "preview.md").read_text(encoding="utf-8")
    assert "org-new — 2026-07" in md.splitlines()[0]
    assert "×3.1" in md                                   # 31日/10日 の換算係数


def test_cli_preview_preserves_discussion(make_input, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"], members_month="2026-07", org="org-new",
    )
    rc, out = _run_cli(input_dir, tmp_path, "--preview", "--days", "10")
    path = out / "org-new" / "2026-07" / "preview.md"
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
    from seat_analyzer.report import _preserve_discussion

    path = tmp_path / "preview.md"
    _write_preview_with_discussion(path, "- 部署未記入のメンバーがいるため整備が必要\n\n### 評価\n本格運用中")
    new_md = "# 見出し\n\n本文（再生成）\n\n## 考察\n\n（未記入 — `/seat-analysis` を実行すると考察が追記されます）\n"
    merged = _preserve_discussion(new_md, path)
    assert "部署未記入のメンバーがいるため整備が必要" in merged
    assert "本文（再生成）" in merged                    # 本文側は再生成版で置き換わる
    assert "（未記入 —" not in merged                     # プレースホルダは残らない


def test_preserve_discussion_replaces_placeholder(tmp_path):
    """未記入プレースホルダのままなら新規 md（プレースホルダ入り）で差し替える。"""
    from seat_analyzer.report import _preserve_discussion

    path = tmp_path / "preview.md"
    _write_preview_with_discussion(
        path, "<!-- コメント -->\n（未記入 — `/seat-analysis preview <日数>` を実行すると考察が追記されます）")
    new_md = "# 見出し\n\n新本文\n\n## 考察\n\n新プレースホルダ本文\n"
    assert _preserve_discussion(new_md, path) == new_md


def test_preserve_discussion_no_marker_returns_new(tmp_path):
    """既存ファイルに「## 考察」marker が無ければ新規 md をそのまま返す。"""
    from seat_analyzer.report import _preserve_discussion

    path = tmp_path / "preview.md"
    path.write_text("# 見出し\n\n本文だけで考察セクションが無い\n", encoding="utf-8")
    new_md = "# 見出し\n\n新本文\n\n## 考察\n\n本文\n"
    assert _preserve_discussion(new_md, path) == new_md


def test_cli_days_requires_preview(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Premium"],
    )
    rc, _ = _run_cli(input_dir, tmp_path, "--days", "10")
    assert rc == 1
    assert "--preview 専用" in capsys.readouterr().err
    rc, _ = _run_cli(input_dir, tmp_path, "--preview")
    assert rc == 1
    assert "--days" in capsys.readouterr().err
