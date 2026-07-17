"""月中のメンバー変動（members 単日スナップショット差分）のテスト。

発動条件は対象月内の kind=date の members ファイルが2つ以上。kind=month
（members_2026-07.csv）は時点不明のため差分対象外。判定・ヒステリシスには影響しない。
"""

from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import write_markdown, write_preview

from .conftest import spend_row


def _spend(make_snapshots, month="2026-07", org=None):
    """メンバー変動テスト用に、月末までの単一スペンド（差分非発動）を用意する。"""
    return make_snapshots(
        month, {f"{month}-31": [spend_row("a@x.jp", 80.0, net=0.0)]}, org=org,
    )


def test_seat_change_add_remove_detected(cfg, make_snapshots, write_member_snapshots):
    input_dir = _spend(make_snapshots)
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard", "b@x.jp,premium", "d@x.jp,standard"],
        "2026-07-16": ["a@x.jp,premium", "b@x.jp,premium", "c@x.jp,standard"],
    })
    mc = analyze(input_dir, "2026-07", cfg).member_changes
    assert mc is not None
    assert [s["label"] for s in mc["snaps"]] == ["07-05", "07-16"]
    assert mc["seat_changes"] == [
        {"email": "a@x.jp", "from": "standard", "to": "premium",
         "interval_label": "07-05→07-16"}
    ]
    assert [j["email"] for j in mc["joined"]] == ["c@x.jp"]
    assert mc["joined"][0]["seat"] == "standard"
    assert [x["email"] for x in mc["left"]] == ["d@x.jp"]
    assert mc["left"][0]["seat"] == "standard"


def test_no_changes_shows_no_change(cfg, make_snapshots, write_member_snapshots, tmp_path):
    input_dir = _spend(make_snapshots)
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard", "b@x.jp,premium"],
        "2026-07-16": ["a@x.jp,standard", "b@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-07", cfg)
    mc = result.member_changes
    assert mc is not None                       # 変動が無くてもセクションは出す
    assert not (mc["seat_changes"] or mc["joined"] or mc["left"])
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 月中のメンバー変動（スナップショット差分）" in md
    assert "変動なし" in md


def test_seat_change_warning(cfg, make_snapshots, write_member_snapshots):
    input_dir = _spend(make_snapshots)
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard"],
        "2026-07-16": ["a@x.jp,premium"],
    })
    warns = analyze(input_dir, "2026-07", cfg).warnings
    assert any("月中にシート変更を検出" in w and "最新スナップショット時点" in w for w in warns)


def test_month_kind_not_participating(cfg, make_snapshots, write_member_snapshots):
    # month-kind の members_2026-07.csv + date 1件 → date が1つのみで非発動
    input_dir = make_snapshots(
        "2026-07", {"2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)]},
        members=["a@x.jp,standard"],
    )
    write_member_snapshots(input_dir, {"2026-07-05": ["a@x.jp,standard"]})
    assert analyze(input_dir, "2026-07", cfg).member_changes is None


def test_month_kind_excluded_from_snaps(cfg, make_snapshots, write_member_snapshots):
    # month-kind が同居しても差分の時点は date 2件のみ（month は除外）
    input_dir = make_snapshots(
        "2026-07", {"2026-07-31": [spend_row("a@x.jp", 80.0, net=0.0)]},
        members=["a@x.jp,standard"],
    )
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard"],
        "2026-07-16": ["a@x.jp,premium"],
    })
    mc = analyze(input_dir, "2026-07", cfg).member_changes
    assert mc is not None
    assert [s["label"] for s in mc["snaps"]] == ["07-05", "07-16"]


def test_single_snapshot_no_section(cfg, make_snapshots, write_member_snapshots, tmp_path):
    input_dir = _spend(make_snapshots)
    write_member_snapshots(input_dir, {"2026-07-05": ["a@x.jp,standard"]})
    result = analyze(input_dir, "2026-07", cfg)
    assert result.member_changes is None
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "月中のメンバー変動" not in out.read_text(encoding="utf-8")


def test_dedup_warning_reworded_when_active(cfg, make_snapshots, write_member_snapshots):
    input_dir = _spend(make_snapshots)
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard"],
        "2026-07-16": ["a@x.jp,premium"],
    })
    warns = analyze(input_dir, "2026-07", cfg).warnings
    assert any("メンバー変動の検出に" in w for w in warns)
    assert not any("未使用:" in w for w in warns)


def test_markdown_section_order(cfg, make_snapshots, write_member_snapshots, tmp_path):
    input_dir = _spend(make_snapshots)
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard"],
        "2026-07-16": ["a@x.jp,premium"],
    })
    result = analyze(input_dir, "2026-07", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    # サマリより後・シート変更推奨より前・考察より前
    assert md.index("## サマリ") < md.index("## 月中のメンバー変動（スナップショット差分）")
    assert md.index("## 月中のメンバー変動（スナップショット差分）") < md.index("## シート変更推奨")
    assert md.index("## 月中のメンバー変動（スナップショット差分）") < md.index("## 考察")
    assert "07-05→07-16 で Standard → Premium" in md


def test_preview_computes_member_changes(cfg, make_snapshots, write_member_snapshots, tmp_path):
    input_dir = make_snapshots(
        "2026-07", {"2026-07-13": [spend_row("a@x.jp", 40.0, net=0.0)]},
    )
    write_member_snapshots(input_dir, {
        "2026-07-05": ["a@x.jp,standard"],
        "2026-07-13": ["a@x.jp,premium"],
    })
    result = preview(input_dir, "2026-07", cfg, days_observed=13)
    assert result.member_changes is not None
    out = tmp_path / "org"
    write_preview(result, out)
    md = (out / "2026-07" / "preview.md").read_text(encoding="utf-8")
    assert "## 月中のメンバー変動（スナップショット差分）" in md
    # 一次判断テーブルより後・注意事項より前
    assert md.index("## 一次判断テーブル") < md.index("## 月中のメンバー変動（スナップショット差分）")
    assert md.index("## 月中のメンバー変動（スナップショット差分）") < md.index("## 注意事項")
