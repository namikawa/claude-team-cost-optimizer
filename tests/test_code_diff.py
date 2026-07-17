"""月中の Claude Code 活動（code-analytics スナップショット差分）のテスト。

発動条件は対象月内の kind=date/range の code-analytics ファイルが2つ以上。
kind=month（cc_2026-07.csv）は時点不明のため差分対象外。judge/ヒステリシスには影響しない。
spend の停止疑いとの突合（LoC 増分の傍証/食い違い）もここで検証する。
"""

from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import write_markdown

from .conftest import spend_row


def _spend_single(make_snapshots, org=None, members=None):
    """code-analytics 差分テスト用に、月末までの単一スペンド（spend 差分非発動）を用意する。"""
    return make_snapshots(
        "2026-07", {"2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)]},
        members=members or ["a@x.jp,standard"], org=org,
    )


def test_cumulative_and_delta(cfg, make_snapshots, write_code_snapshots):
    input_dir = _spend_single(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("heavy@x.jp", 1000, 5), ("light@x.jp", 100, 1), ("zero@x.jp", 0, 0)],
        "2026-07-16": [("heavy@x.jp", 3000, 12), ("light@x.jp", 100, 1), ("zero@x.jp", 0, 0)],
    })
    cd = analyze(input_dir, "2026-07", cfg).code_diff
    assert cd is not None
    assert cd["labels"] == ["〜07-05", "〜07-16"]
    assert cd["has_prs"] is True
    rows = {r["email"]: r for r in cd["rows"]}
    assert "zero@x.jp" not in rows                      # 全時点ゼロは省く
    assert rows["heavy@x.jp"]["loc_cum"] == [1000, 3000]
    assert rows["heavy@x.jp"]["loc_delta"] == 2000
    assert rows["heavy@x.jp"]["prs_delta"] == 7
    assert rows["light@x.jp"]["loc_delta"] == 0
    # 最新累積 LoC の降順
    assert [r["email"] for r in cd["rows"]] == ["heavy@x.jp", "light@x.jp"]


def test_no_prs_column(cfg, make_snapshots, write_code_snapshots):
    input_dir = _spend_single(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("heavy@x.jp", 1000)],
        "2026-07-16": [("heavy@x.jp", 2500)],
    }, with_prs=False)
    cd = analyze(input_dir, "2026-07", cfg).code_diff
    assert cd["has_prs"] is False
    assert cd["rows"][0]["prs_delta"] is None
    assert cd["rows"][0]["loc_delta"] == 1500


def test_month_kind_not_participating(cfg, make_snapshots, write_code_snapshots):
    # month-kind の cc_2026-07.csv 相当は差分対象外。date が1件のみでは非発動
    input_dir = _spend_single(make_snapshots)
    write_code_snapshots(input_dir, {"2026-07-05": [("heavy@x.jp", 1000, 5)]})
    # 追加で month-kind ファイルを置いても date は1件のまま
    (input_dir / "code-analytics").mkdir(parents=True, exist_ok=True)
    (input_dir / "code-analytics" / "cc_2026-07.csv").write_text(
        "Email,Lines with CC,PRs with CC\nheavy@x.jp,9999,9\n", encoding="utf-8")
    assert analyze(input_dir, "2026-07", cfg).code_diff is None


def test_single_snapshot_no_section(cfg, make_snapshots, write_code_snapshots, tmp_path):
    input_dir = _spend_single(make_snapshots)
    write_code_snapshots(input_dir, {"2026-07-05": [("heavy@x.jp", 1000, 5)]})
    result = analyze(input_dir, "2026-07", cfg)
    assert result.code_diff is None
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "月中の Claude Code 活動" not in out.read_text(encoding="utf-8")


def test_dedup_warning_reworded_when_active(cfg, make_snapshots, write_code_snapshots):
    input_dir = _spend_single(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("heavy@x.jp", 1000, 5)],
        "2026-07-16": [("heavy@x.jp", 3000, 12)],
    })
    warns = analyze(input_dir, "2026-07", cfg).warnings
    assert any("Claude Code 活動の差分に" in w for w in warns)
    assert not any("未使用:" in w for w in warns)


def test_markdown_section_and_order(cfg, make_snapshots, write_code_snapshots, tmp_path):
    input_dir = _spend_single(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("heavy@x.jp", 1000, 5)],
        "2026-07-16": [("heavy@x.jp", 3000, 12)],
    })
    result = analyze(input_dir, "2026-07", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 月中の Claude Code 活動（code-analytics 差分）" in md
    assert "LoC 増分（最新区間）" in md
    assert "+2,000" in md                              # 桁区切り + 符号
    assert md.index("## サマリ") < md.index("## 月中の Claude Code 活動（code-analytics 差分）")
    assert md.index("## 月中の Claude Code 活動（code-analytics 差分）") < md.index("## 考察")


# --- spend 停止疑いとの突合（LoC 増分の傍証 / 食い違い）---

def _stall_input(make_snapshots):
    """Standard・実課金0・停止疑いになる spend スナップショットを組む。"""
    return make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("s@x.jp", 40.0, net=0.0)],
            "2026-07-31": [spend_row("s@x.jp", 40.5, net=0.0)],   # 停止・cum≥10・billed0
        },
        members=["s@x.jp,standard"],
    )


def test_stall_corroborated_by_flat_loc(cfg, make_snapshots, write_code_snapshots, tmp_path):
    input_dir = _stall_input(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("s@x.jp", 1000, 5)],
        "2026-07-16": [("s@x.jp", 1000, 5)],   # LoC 横ばい → 停止の傍証
    })
    result = analyze(input_dir, "2026-07", cfg)
    sc = result.snapshot["stalled_capped"]
    assert sc and sc[0]["email"] == "s@x.jp"
    assert sc[0]["loc_note"] == "LoC 増分も 0（停止の傍証）"
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "停止の傍証" in out.read_text(encoding="utf-8")


def test_stall_contradicted_by_growing_loc(cfg, make_snapshots, write_code_snapshots):
    input_dir = _stall_input(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("s@x.jp", 1000, 5)],
        "2026-07-16": [("s@x.jp", 1500, 8)],   # LoC 増加 → 食い違い
    })
    result = analyze(input_dir, "2026-07", cfg)
    sc = result.snapshot["stalled_capped"]
    assert sc[0]["loc_note"] == (
        "一方で LoC は +500 行 増加（利用継続の形跡あり。スペンドとの食い違いは要確認）"
    )


def test_stall_absent_in_code_diff_is_corroboration(cfg, make_snapshots, write_code_snapshots):
    # 停止疑いユーザが code diff に不在（LoC 全ゼロで省かれる等）→ 傍証扱い
    input_dir = _stall_input(make_snapshots)
    write_code_snapshots(input_dir, {
        "2026-07-05": [("other@x.jp", 500, 2)],
        "2026-07-16": [("other@x.jp", 900, 4)],
    })
    result = analyze(input_dir, "2026-07", cfg)
    sc = result.snapshot["stalled_capped"]
    assert sc[0]["loc_note"] == "LoC 増分も 0（停止の傍証）"


def test_no_code_diff_no_loc_note(cfg, make_snapshots):
    # code-analytics スナップショットが無ければ注記は付かない（後方互換）
    input_dir = _stall_input(make_snapshots)
    result = analyze(input_dir, "2026-07", cfg)
    assert result.code_diff is None
    assert result.snapshot["stalled_capped"][0].get("loc_note", "") == ""


def test_preview_computes_code_diff(cfg, make_snapshots, write_code_snapshots):
    input_dir = make_snapshots(
        "2026-07", {"2026-07-13": [spend_row("a@x.jp", 40.0, net=0.0)]},
        members=["a@x.jp,standard"],
    )
    write_code_snapshots(input_dir, {
        "2026-07-05": [("heavy@x.jp", 1000, 5)],
        "2026-07-13": [("heavy@x.jp", 2200, 9)],
    })
    result = preview(input_dir, "2026-07", cfg, days_observed=13)
    assert result.code_diff is not None
    assert result.code_diff["rows"][0]["loc_delta"] == 1200
