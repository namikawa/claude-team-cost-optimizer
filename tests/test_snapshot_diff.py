"""スナップショット差分（同一月の複数エクスポートによる月中推移・停止検出）のテスト。

デフォルト閾値（config.yaml > snapshot_diff）: stall_max_delta=1.0 /
min_cumulative=10.0 / min_interval_days=3。需要基準は computed 固定。
"""

from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import write_markdown

from .conftest import spend_row


def test_stall_detected_and_deltas(cfg, make_snapshots):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("heavy@x.jp", 80.0, net=0.0),
                           spend_row("stall@x.jp", 40.0, net=0.0)],
            "2026-07-13": [spend_row("heavy@x.jp", 210.0, net=0.0),
                           spend_row("stall@x.jp", 45.0, net=0.0)],
            "2026-07-31": [spend_row("heavy@x.jp", 470.0, net=0.0),
                           spend_row("stall@x.jp", 45.4, net=0.0)],
        },
        members=["heavy@x.jp,premium", "stall@x.jp,standard"],
    )
    snap = analyze(input_dir, "2026-07", cfg).snapshot
    assert snap is not None
    assert snap["labels"] == ["〜07-05", "〜07-13", "〜07-31"]
    assert snap["judged"] is True
    rows = {r["email"]: r for r in snap["rows"]}
    assert rows["heavy@x.jp"]["stall"] is False
    assert rows["stall@x.jp"]["stall"] is True
    assert rows["stall@x.jp"]["latest_delta"] == 0.4
    # 累積降順で並ぶ
    assert [r["email"] for r in snap["rows"]] == ["heavy@x.jp", "stall@x.jp"]
    # 停止疑い ∩ Standard ∩ 実課金0 → 実効込み量の実測候補
    assert [x["email"] for x in snap["stalled_capped"]] == ["stall@x.jp"]
    assert snap["stalled_capped"][0]["cum_at_stall"] == 45.4


def test_low_cumulative_not_stall(cfg, make_snapshots):
    # 累積が min_cumulative(10) 未満のユーザは横ばいでも停止疑いにしない（遊休との区別）
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("idle@x.jp", 5.0, net=0.0)],
            "2026-07-31": [spend_row("idle@x.jp", 9.0, net=0.0)],
        },
        members=["idle@x.jp,standard"],
    )
    snap = analyze(input_dir, "2026-07", cfg).snapshot
    rows = {r["email"]: r for r in snap["rows"]}
    assert rows["idle@x.jp"]["stall"] is False
    assert snap["stalled_capped"] == []


def test_short_interval_not_judged(cfg, make_snapshots, tmp_path):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 40.0, net=0.0)],
            "2026-07-06": [spend_row("a@x.jp", 40.5, net=0.0)],   # 最新区間 1 日
        },
        members=["a@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-07", cfg)
    snap = result.snapshot
    assert snap["judged"] is False
    assert snap["latest_interval_days"] == 1
    assert all(not r["stall"] for r in snap["rows"])
    assert snap["stalled_capped"] == []
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 月中の利用推移（スナップショット差分）" in md
    assert "短いため停止判定は行っていません" in md


def test_cumulative_decrease_warns(cfg, make_snapshots):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 50.0, net=0.0)],
            "2026-07-13": [spend_row("a@x.jp", 40.0, net=0.0)],   # 累積が減少
        },
        members=["a@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-07", cfg)
    assert any("累積需要が減少" in w for w in result.warnings)


def test_non_month_start_range_excluded(cfg, make_snapshots):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 40.0, net=0.0)],
            "2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)],
        },
        members=["a@x.jp,standard"],
        extra_files={
            "spend-report-2026-07-10-to-2026-07-20.csv": [spend_row("a@x.jp", 5.0, net=0.0)],
        },
    )
    result = analyze(input_dir, "2026-07", cfg)
    assert result.snapshot is not None    # 月初開始 range が2つ → 発動
    assert any("月初開始でないため差分分析から除外" in w for w in result.warnings)


def test_billed_emerged_interval(cfg, make_snapshots):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 60.0, net=0.0)],
            "2026-07-13": [spend_row("a@x.jp", 150.0, net=20.0)],   # 0→正 に転じる
            "2026-07-31": [spend_row("a@x.jp", 260.0, net=90.0)],
        },
        members=["a@x.jp,standard"],
    )
    be = analyze(input_dir, "2026-07", cfg).snapshot["billed_emerged"]
    assert len(be) == 1
    assert be[0]["email"] == "a@x.jp"
    assert be[0]["prev_cum"] == 60.0 and be[0]["curr_cum"] == 150.0
    assert be[0]["billed"] == 20.0
    assert "07-05" in be[0]["interval_label"] and "07-13" in be[0]["interval_label"]


def test_single_snapshot_no_section(cfg, make_snapshots, tmp_path):
    input_dir = make_snapshots(
        "2026-07",
        {"2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)]},
        members=["a@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-07", cfg)
    assert result.snapshot is None
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "月中の利用推移" not in md
    # 単一スペンドなので重複警告も出ない（既存出力と同一）
    assert not any("スナップショット差分" in w for w in result.warnings)


def test_duplicate_warning_reworded_when_active(cfg, make_snapshots):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 40.0, net=0.0)],
            "2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)],
        },
        members=["a@x.jp,standard"],
    )
    warns = analyze(input_dir, "2026-07", cfg).warnings
    assert any("主データには期間の広い" in w and "スナップショット差分に" in w for w in warns)
    assert not any("未使用:" in w for w in warns)


def test_preview_computes_snapshot(cfg, make_snapshots):
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 40.0, net=0.0)],
            "2026-07-13": [spend_row("a@x.jp", 45.0, net=0.0)],
        },
        members=["a@x.jp,standard"],
    )
    result = preview(input_dir, "2026-07", cfg, days_observed=13)
    assert result.snapshot is not None
    assert result.snapshot["labels"] == ["〜07-05", "〜07-13"]
