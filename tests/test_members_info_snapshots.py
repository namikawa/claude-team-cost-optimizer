"""members-info の日付スナップショット解決と月中の κ 変更検出のテスト。

固定名 members-info.csv に加え日付つき members-info-*-YYYY-MM-DD.csv を受け付け、
対象月の月末以前で最新を採用する（無ければ最古へフォールバック + 強警告）。
"""

from pathlib import Path

from seat_analyzer.analyze import analyze
from seat_analyzer.report import write_markdown

from .conftest import spend_row


def _write_info(input_dir: Path, text: str) -> None:
    (input_dir / "members-info.csv").write_text(text, encoding="utf-8")


def _write_snapshot(input_dir: Path, date: str, rows: list[str]) -> None:
    (input_dir / f"members-info-snap-{date}.csv").write_text(
        "email,追加クレジット上限\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_latest_on_or_before_month_end(make_input, cfg):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"], members_month="2026-07",
    )
    _write_snapshot(input_dir, "2026-07-05", ["a@x.jp,100"])
    _write_snapshot(input_dir, "2026-07-16", ["a@x.jp,200"])
    result = analyze(input_dir, "2026-07", cfg)
    assert result.sources["members_info"].endswith("2026-07-16.csv")
    assert result.users.set_index("email").loc["a@x.jp", "credit_limit_usd"] == 200.0


def test_fallback_to_oldest_with_strong_warning(make_input, cfg):
    # 対象月 2026-06 の月末以前にスナップショットが無い → 最古(07-05)へフォールバック
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_snapshot(input_dir, "2026-07-05", ["a@x.jp,100"])
    _write_snapshot(input_dir, "2026-07-20", ["a@x.jp,200"])
    result = analyze(input_dir, "2026-06", cfg)
    assert result.sources["members_info"].endswith("2026-07-05.csv")
    assert any("月末以前のスナップショットが無いため" in w for w in result.warnings)


def test_date_snapshots_override_fixed_name(make_input, cfg):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"], members_month="2026-07",
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,999\n")   # 固定名（無視される）
    _write_snapshot(input_dir, "2026-07-10", ["a@x.jp,200"])
    result = analyze(input_dir, "2026-07", cfg)
    assert result.sources["members_info"].endswith("2026-07-10.csv")
    assert result.users.set_index("email").loc["a@x.jp", "credit_limit_usd"] == 200.0
    assert any("固定名" in w and "無視" in w for w in result.warnings)


def test_fixed_name_used_without_snapshots(make_input, cfg):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"], members_month="2026-07",
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,150\n")
    result = analyze(input_dir, "2026-07", cfg)
    assert result.sources["members_info"].endswith("members-info.csv")
    assert result.users.set_index("email").loc["a@x.jp", "credit_limit_usd"] == 150.0


def test_kappa_change_detected_and_rendered(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"], members_month="2026-07",
    )
    _write_snapshot(input_dir, "2026-07-05", ["a@x.jp,100", "b@x.jp,50"])
    _write_snapshot(input_dir, "2026-07-16", ["a@x.jp,200", "b@x.jp,50"])
    result = analyze(input_dir, "2026-07", cfg)
    mc = result.member_changes
    assert mc is not None
    # b は 50→50 で不変、a のみ κ 変更
    assert [c["email"] for c in mc["credit_changes"]] == ["a@x.jp"]
    assert mc["credit_changes"][0]["from"] == "$100"
    assert mc["credit_changes"][0]["to"] == "$200"
    assert any("追加クレジット上限の変更を検出" in w for w in result.warnings)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "追加クレジット上限 $100 → $200" in md


def test_single_snapshot_no_kappa_change(make_input, cfg):
    input_dir = make_input(
        {"2026-07": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"], members_month="2026-07",
    )
    _write_snapshot(input_dir, "2026-07-05", ["a@x.jp,100"])
    # members-info スナップショットが1件のみ・members 差分も無い → member_changes は None
    assert analyze(input_dir, "2026-07", cfg).member_changes is None
