"""出力の安全性（HTML エスケープ・CSV formula injection 対策）のテスト。"""

import pandas as pd

from seat_analyzer.analyze import analyze
from seat_analyzer.report import write_csv, write_html

from .conftest import spend_row


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
