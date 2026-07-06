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
    path = write_preview(result, tmp_path / "reports")
    md = path.read_text(encoding="utf-8")
    assert "⚠️従量あり" in md
    assert "⚠️超過済" not in md.split("## 一次判断テーブル")[1].split("\n\n")[0]
    # 凡例にも従量ありの行が追加されている
    assert "⚠️従量あり: Standard 等で従量課金が発生" in md


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


def test_cli_preview_writes_only_preview_md(make_input, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"], members_month="2026-07", org="org-new",
    )
    rc, out = _run_cli(input_dir, tmp_path, "--preview", "--days", "10")
    assert rc == 0
    report_dir = out / "org-new" / "2026-07"
    assert (report_dir / "preview.md").exists()
    assert not (report_dir / "report.md").exists()       # 正式レポートには触れない
    assert not (report_dir / "dashboard.html").exists()
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
