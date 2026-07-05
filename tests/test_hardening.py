"""出力の安全性・入力ファイルの取り違え防止のテスト。"""

import pandas as pd
import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.cli import main
from seat_analyzer.ingest import discover_months
from seat_analyzer.report import write_csv, write_html

from .conftest import SPEND_HEADER, spend_row


def test_html_escapes_script_in_email(cfg, make_input, tmp_path):
    evil = '<script>alert(1)</script>@x.jp'
    input_dir = make_input(
        {"2026-06": [spend_row(evil, 10.0)]}, members=[f"{evil},Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "dashboard.html"
    write_html(result, out)
    html = out.read_text(encoding="utf-8")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_csv_formula_cells_are_sanitized(tmp_path):
    users = pd.DataFrame([
        {"email": "=HYPERLINK(\"http://evil\")", "monthly_saving_usd": -5.0},
        {"email": "a@x.jp", "monthly_saving_usd": 10.0},
    ])
    result = type("R", (), {"users": users})()
    path = tmp_path / "rec.csv"
    write_csv(result, path)
    text = path.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text          # 文字列セルは ' 付与で無害化
    assert "a@x.jp" in text               # 通常の文字列はそのまま
    assert "-5.0" in text                 # 数値セルは変更しない


def test_duplicate_month_csv_raises(make_input):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 1.0)]})
    dup = input_dir / "spend" / "spend-report_2026-06.csv"
    dup.write_text(SPEND_HEADER + "\n" + spend_row("a@x.jp", 2.0) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="複数あります"):
        discover_months(input_dir)


def test_future_members_fallback_warns_strongly(cfg, make_input):
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Premium"], members_month="2026-07",
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert any("未来月" in w for w in result.warnings)


def test_manually_created_summary_org_rejected(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Standard"], org="summary",
    )
    rc = main([
        "analyze", "--config", "config.yaml",
        "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    assert "予約" in capsys.readouterr().err
