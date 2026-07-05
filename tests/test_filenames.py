"""claude.ai ダウンロード時のファイル名（期間付き・スナップショット日付）対応のテスト。"""

from pathlib import Path

import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.cli import main
from seat_analyzer.ingest import file_period, load_members, load_spend, month_of_file
from seat_analyzer.pricing import price_for_model

from .conftest import REPO_ROOT, SPEND_HEADER, spend_row

CONFIG = str(REPO_ROOT / "config.yaml")
UUID = "bd3f72b4-64ad-4756-bb58-3434644041ec"


# --- ファイル名からの期間解釈 ---

def test_month_of_file_variants():
    cases = {
        f"spend-report-{UUID}-2026-06-01-to-2026-06-30.csv": "2026-06",   # 期間（ハイフン）
        "claude_code_team_2026_06_01_to_2026_06_30.csv": "2026-06",      # 期間（アンダースコア）
        f"members-{UUID}-2026-07-05.csv": "2026-07",                      # スナップショット日付
        "spend_2026-06.csv": "2026-06",                                   # 月のみ（従来形式）
        "notes.csv": None,                                                # 日付なし
    }
    for name, expected in cases.items():
        assert month_of_file(Path(name)) == expected, name


def test_file_period_days():
    p = file_period(Path(f"spend-report-{UUID}-2026-07-01-to-2026-07-04.csv"))
    assert (p.kind, p.days) == ("range", 4)
    assert file_period(Path("spend_2026-07.csv")).days is None


def test_cross_month_range_raises():
    with pytest.raises(ValueError, match="月をまたぐ"):
        file_period(Path("spend-report-2026-06-15-to-2026-07-14.csv"))


# --- 同一月の複数ファイル解決 ---

def _write_spend(input_dir: Path, name: str, rows: list[str]) -> None:
    p = input_dir / "spend" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SPEND_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_containing_range_wins_with_warning(cfg, tmp_path):
    input_dir = tmp_path / "input"
    _write_spend(input_dir, "spend-report-2026-07-01-to-2026-07-04.csv", [spend_row("a@x.jp", 1.0)])
    _write_spend(input_dir, "spend-report-2026-07-01-to-2026-07-31.csv", [spend_row("a@x.jp", 99.0)])
    result = load_spend(input_dir, "2026-07", cfg)
    assert "2026-07-01-to-2026-07-31" in result.source.name   # 広い期間が採用される
    assert any("期間の広い" in w for w in result.warnings)


def test_latest_member_snapshot_wins(cfg, tmp_path):
    input_dir = tmp_path / "input"
    for day, seat in (("05", "Standard"), ("20", "Premium")):
        p = input_dir / "members" / f"members-{UUID}-2026-07-{day}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"Email,Seat Type\na@x.jp,{seat}\n", encoding="utf-8")
    result = load_members(input_dir, "2026-07", cfg)
    assert result.df.iloc[0]["seat_type"] == "premium"        # 最新スナップショット採用
    assert any("最新の" in w for w in result.warnings)


# --- 部分月データの安全装置 ---

def test_partial_month_spend_warns_in_analyze(cfg, tmp_path):
    input_dir = tmp_path / "input"
    _write_spend(input_dir, "spend-report-2026-07-01-to-2026-07-04.csv", [spend_row("a@x.jp", 10.0)])
    p = input_dir / "members" / f"members-{UUID}-2026-07-05.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")
    result = analyze(input_dir, "2026-07", cfg)
    assert any("部分月データ" in w and "--preview" in w for w in result.warnings)


def test_full_month_range_does_not_warn(cfg, tmp_path):
    input_dir = tmp_path / "input"
    _write_spend(input_dir, "spend-report-2026-06-01-to-2026-06-30.csv", [spend_row("a@x.jp", 10.0)])
    p = input_dir / "members" / "members_2026-06.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")
    result = analyze(input_dir, "2026-06", cfg)
    assert not any("部分月データ" in w for w in result.warnings)


# --- preview の観測日数自動判別 ---

def test_preview_days_auto_detected_from_filename(cfg, tmp_path, capsys):
    input_dir = tmp_path / "input" / "org-x"
    _write_spend(input_dir, f"spend-report-{UUID}-2026-07-01-to-2026-07-04.csv",
                 [spend_row("a@x.jp", 40.0, net=0.0)])
    p = input_dir / "members" / f"members-{UUID}-2026-07-05.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")
    rc = main([
        "analyze", "--config", CONFIG, "--preview",
        "--input-dir", str(tmp_path / "input"), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "観測日数 4 日" in out
    md = (tmp_path / "reports" / "org-x" / "2026-07" / "preview.md").read_text(encoding="utf-8")
    assert "4日間の観測データ" in md


def test_preview_without_days_or_range_errors(cfg, make_input, tmp_path, capsys):
    input_dir = make_input({"2026-07": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], members_month="2026-07")
    rc = main([
        "analyze", "--config", CONFIG, "--preview",
        "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    assert "--days" in capsys.readouterr().err


# --- モデル単価（2026-07-05 公式照合） ---

def test_model_price_patterns(cfg):
    assert price_for_model("claude-sonnet-5", cfg) == (2.0, 10.0)          # 導入価格（〜2026-08-31）
    assert price_for_model("claude-sonnet-4-6", cfg) == (3.0, 15.0)
    assert price_for_model("claude-fable-5", cfg) == (10.0, 50.0)
    assert price_for_model("claude-opus-4-8", cfg) == (5.0, 25.0)
    assert price_for_model("claude-opus-4-1-20250805", cfg) == (15.0, 75.0)
    assert price_for_model("claude-haiku-4-5-20251001", cfg) == (1.0, 5.0)
