"""前月からの変化・月次推移（正式分析の「## 前月からの変化」）のテスト。

デフォルト閾値（config.yaml > trend）: idle=1.0 / min_activity=10.0 /
change_min=50.0 / top_changes=5。cost_basis=computed のため需要は tokens×単価。
"""

from seat_analyzer.analyze import analyze
from seat_analyzer.report import write_markdown

from .conftest import spend_row


def test_started_and_stopped(cfg, make_input):
    input_dir = make_input(
        {
            "2026-05": [spend_row("stop@x.jp", 40.0, net=0.0),
                        spend_row("keep@x.jp", 100.0, net=0.0)],
            "2026-06": [spend_row("start@x.jp", 30.0, net=0.0),
                        spend_row("keep@x.jp", 100.0, net=0.0)],
        },
        members=["stop@x.jp,standard", "start@x.jp,standard", "keep@x.jp,premium"],
    )
    t = analyze(input_dir, "2026-06", cfg).trend
    assert t["compare_month"] == "2026-05"
    assert t["gap_skipped"] is False
    assert [x["email"] for x in t["started"]] == ["start@x.jp"]
    assert t["started"][0]["amount"] == 30.0
    assert [x["email"] for x in t["stopped"]] == ["stop@x.jp"]
    assert t["stopped"][0]["amount"] == 40.0
    # 利用開始/停止したユーザは「主な増減」に重複掲載しない
    assert not any(c["email"] in {"start@x.jp", "stop@x.jp"} for c in t["changes"])


def test_changes_signed_and_top_limited(cfg, make_input):
    # 6人が >=50 変化 → 絶対値降順で top_changes=5 に絞られ、符号付きで並ぶ
    input_dir = make_input(
        {
            "2026-05": [
                spend_row("u1@x.jp", 10.0, net=0.0), spend_row("u2@x.jp", 200.0, net=0.0),
                spend_row("u3@x.jp", 10.0, net=0.0), spend_row("u4@x.jp", 170.0, net=0.0),
                spend_row("u5@x.jp", 10.0, net=0.0), spend_row("u6@x.jp", 10.0, net=0.0),
            ],
            "2026-06": [
                spend_row("u1@x.jp", 210.0, net=0.0), spend_row("u2@x.jp", 10.0, net=0.0),
                spend_row("u3@x.jp", 190.0, net=0.0), spend_row("u4@x.jp", 10.0, net=0.0),
                spend_row("u5@x.jp", 160.0, net=0.0), spend_row("u6@x.jp", 60.0, net=0.0),
            ],
        },
        members=[f"u{i}@x.jp,premium" for i in range(1, 7)],
    )
    t = analyze(input_dir, "2026-06", cfg).trend
    assert len(t["changes"]) == 5                     # top_changes で6→5
    assert t["changes"][0]["email"] == "u1@x.jp"      # |+200| が最大
    assert t["changes"][0]["delta"] == 200.0
    assert any(c["delta"] < 0 for c in t["changes"])  # 符号付き（u2/u4 は減少）
    assert "u6@x.jp" not in [c["email"] for c in t["changes"]]  # |+50| は下位で落ちる


def test_new_billed_detected(cfg, make_input):
    input_dir = make_input(
        {
            "2026-05": [spend_row("nb@x.jp", 300.0, net=0.0)],
            "2026-06": [spend_row("nb@x.jp", 300.0, net=120.0)],
        },
        members=["nb@x.jp,standard"],
    )
    t = analyze(input_dir, "2026-06", cfg).trend
    assert [x["email"] for x in t["new_billed"]] == ["nb@x.jp"]
    assert t["new_billed"][0]["amount"] == 120.0


def test_gap_skipped(cfg, make_input):
    # 2026-05 が欠測 → 直前の存在月 2026-04 と比較し gap_skipped=True
    input_dir = make_input(
        {
            "2026-04": [spend_row("a@x.jp", 100.0, net=0.0)],
            "2026-06": [spend_row("a@x.jp", 100.0, net=0.0)],
        },
        members=["a@x.jp,premium"],
    )
    t = analyze(input_dir, "2026-06", cfg).trend
    assert t["compare_month"] == "2026-04"
    assert t["gap_skipped"] is True


def test_initial_month_has_no_trend(cfg, make_input, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 100.0, net=0.0)]},
        members=["a@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert result.trend is None
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "## 前月からの変化" not in out.read_text(encoding="utf-8")


def test_monthly_series_active_count(cfg, make_input):
    input_dir = make_input(
        {
            "2026-05": [spend_row("a@x.jp", 100.0, net=0.0),
                        spend_row("b@x.jp", 0.5, net=0.0)],   # 0.5 < idle 1.0 → 非アクティブ
            "2026-06": [spend_row("a@x.jp", 150.0, net=20.0)],
        },
        members=["a@x.jp,premium", "b@x.jp,standard"],
    )
    series = analyze(input_dir, "2026-06", cfg).trend["series"]
    assert [s["month"] for s in series] == ["2026-05", "2026-06"]
    assert series[0]["active"] == 1              # a のみ（b は idle 未満）
    assert series[0]["api"] == 100.5
    assert series[1]["active"] == 1
    assert series[1]["billed"] == 20.0


def test_series_limited_to_six_months(cfg, make_input):
    months = [f"2026-{m:02d}" for m in range(1, 8)]   # 1〜7月の7ヶ月
    spend = {m: [spend_row("a@x.jp", 100.0, net=0.0)] for m in months}
    input_dir = make_input(spend, members=["a@x.jp,premium"])
    series = analyze(input_dir, "2026-07", cfg).trend["series"]
    assert len(series) == 6                        # 直近6ヶ月まで
    assert series[0]["month"] == "2026-02"
    assert series[-1]["month"] == "2026-07"


def test_trend_section_rendered_in_markdown(cfg, make_input, tmp_path):
    input_dir = make_input(
        {
            "2026-05": [spend_row("a@x.jp", 100.0, net=0.0)],
            "2026-06": [spend_row("a@x.jp", 100.0, net=0.0)],
        },
        members=["a@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    # サマリの直後・シート変更推奨の前に置かれる
    assert "## 前月からの変化" in md
    assert md.index("## サマリ") < md.index("## 前月からの変化") < md.index("## シート変更推奨")
    assert "比較対象: 2026-05" in md
    assert "### 月次推移" in md
