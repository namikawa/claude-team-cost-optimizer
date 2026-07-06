"""詳細利用状況（input/output トークン・モデル割合・LoC）のテスト。"""

import pandas as pd

from seat_analyzer.analyze import _short_model, analyze
from seat_analyzer.report import (
    _detail_rows,
    _detail_table_md,
    _fmt_tokens,
    write_html,
    write_markdown,
)

from .conftest import spend_row


def test_short_model():
    assert _short_model("claude-opus-4-8") == "Opus 4.8"
    assert _short_model("claude-fable-5") == "Fable 5"
    assert _short_model("claude-sonnet-5") == "Sonnet 5"
    assert _short_model("claude-sonnet-4-6") == "Sonnet 4.6"
    assert _short_model("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert _short_model("mystery-model") == "mystery-model"  # 判別不能はそのまま


def test_fmt_tokens():
    assert _fmt_tokens(6_720_200_000) == "6.7B"
    assert _fmt_tokens(1_200_000) == "1.2M"
    assert _fmt_tokens(340_000) == "340K"
    assert _fmt_tokens(500) == "500"
    assert _fmt_tokens(0) == "0"


def test_model_breakdown_is_token_basis(cfg, make_input):
    # opus と haiku を同額（コスト同じ）で計上。トークン基準なら安価な haiku の比率が大きい
    input_dir = make_input(
        {"2026-06": [
            spend_row("a@x.jp", 50.0, model="claude-opus-4-8", net=0.0),
            spend_row("a@x.jp", 50.0, model="claude-haiku-4-5", net=0.0),
        ]},
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    bd = result.users.set_index("email").loc["a@x.jp", "model_breakdown"]
    # コスト基準なら 50/50 だが、トークン基準では haiku が先頭（大きい）
    assert bd.startswith("Haiku 4.5")
    assert "Opus 4.8" in bd


def test_detail_rows_sort_and_loc_absence(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [
            spend_row("small@x.jp", 5.0, net=0.0),
            spend_row("big@x.jp", 500.0, net=0.0),
        ]},
        members=["small@x.jp,Premium", "big@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    rows, has_loc = _detail_rows(result.users)
    assert has_loc is False  # code-analytics なしなら LoC 列なし
    assert rows[0]["email"] == "big@x.jp"  # input+output 降順


def test_detail_table_md_with_loc():
    users = pd.DataFrame([
        {"email": "a@x.jp", "prompt_tokens": 1_200_000, "completion_tokens": 100_000,
         "api_cost_usd": 234.5, "model_breakdown": "Opus 4.8 100%", "loc_with_cc": 5200},
    ])
    md = _detail_table_md(users)
    assert "## 詳細利用状況" in md
    assert "LoC" in md and "5,200" in md
    assert "1.2M" in md
    assert "API換算需要" in md and "$234.50" in md
    assert "キャッシュ読取分を含む" in md


def test_detail_section_in_markdown_and_html(cfg, make_input, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, model="claude-opus-4-8", net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    md_path = tmp_path / "report.md"
    html_path = tmp_path / "dashboard.html"
    write_markdown(result, md_path)
    write_html(result, html_path)
    md = md_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    assert "## 詳細利用状況" in md and "Opus 4.8" in md
    assert "詳細利用状況" in html and "モデル割合" in html


def test_group_summary_includes_prorated_loc():
    from seat_analyzer.report import _group_summary_md, _group_summary_rows
    users = pd.DataFrame([
        {"email": "a@x.jp", "current_seat": "premium", "status": "現状維持",
         "api_cost_usd": 100.0, "billed_extra_usd": 0.0, "monthly_saving_usd": None,
         "team": "基盤", "loc_with_cc": 1000},
        {"email": "b@x.jp", "current_seat": "premium", "status": "現状維持",
         "api_cost_usd": 50.0, "billed_extra_usd": 0.0, "monthly_saving_usd": None,
         "team": "基盤; SRE", "loc_with_cc": 400},  # 兼務 → LoC も 1/2 ずつ按分
    ])
    summary = {"seat_price_standard_usd": 25.0, "seat_price_premium_usd": 125.0}
    by = {r["group"]: r for r in _group_summary_rows(users, summary, "team")}
    assert round(by["基盤"]["loc"]) == 1200   # 1000 + 400*0.5
    assert round(by["SRE"]["loc"]) == 200     # 400*0.5
    md = _group_summary_md(users, summary, "team", "チーム別サマリ")
    assert "LoC" in md and "1,200" in md


def test_billed_gradient_in_dashboard(cfg, make_input, tmp_path):
    # 実課金あり(premium)は背景色が付き、実課金ゼロは無着色
    input_dir = make_input(
        {"2026-06": [
            spend_row("over@x.jp", 400.0, net=200.0),
            spend_row("zero@x.jp", 30.0, net=0.0),
        ]},
        members=["over@x.jp,Premium", "zero@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    html_path = tmp_path / "dashboard.html"
    write_html(result, html_path)
    html = html_path.read_text(encoding="utf-8")
    assert "rgba(192,57,43," in html  # 実課金ありのセルに警告色グラデーション


def test_detail_html_escapes_model_field(cfg, make_input, tmp_path):
    # モデル割合セルは autoescape 経由。悪意ある値でも HTML として解釈されない
    input_dir = make_input(
        {"2026-06": [spend_row("<script>@x.jp", 10.0, net=0.0)]},
        members=["<script>@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    html_path = tmp_path / "dashboard.html"
    write_html(result, html_path)
    html = html_path.read_text(encoding="utf-8")
    assert "<script>@x.jp" not in html
    assert "&lt;script&gt;" in html
