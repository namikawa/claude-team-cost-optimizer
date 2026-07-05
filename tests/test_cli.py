"""CLI のマルチ組織対応（組織解決・--org・横断サマリ・旧レイアウト互換）のテスト。"""

from pathlib import Path

from seat_analyzer.cli import main
from seat_analyzer.ingest import discover_orgs

from .conftest import REPO_ROOT, spend_row

CONFIG = str(REPO_ROOT / "config.yaml")


def _run(input_dir: Path, tmp_path: Path, *extra: str) -> tuple[int, Path]:
    output_dir = tmp_path / "reports"
    rc = main([
        "analyze", "--config", CONFIG,
        "--input-dir", str(input_dir), "--output-dir", str(output_dir),
        *extra,
    ])
    return rc, output_dir


def _make_two_orgs(make_input) -> Path:
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    make_input(
        {"2026-06": [spend_row("b@y.jp", 300.0, net=250.0)]},
        members=["b@y.jp,Standard"], org="org-b",
    )
    return input_dir


def test_discover_orgs(make_input):
    input_dir = _make_two_orgs(make_input)
    assert discover_orgs(input_dir) == ["org-a", "org-b"]
    assert discover_orgs(input_dir / "none") == []


def test_all_orgs_analyzed_with_summary(make_input, tmp_path):
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06")
    assert rc == 0
    assert (out / "org-a" / "2026-06" / "report.md").exists()
    assert (out / "org-b" / "2026-06" / "dashboard.html").exists()
    summary = (out / "summary" / "2026-06.md").read_text(encoding="utf-8")
    assert "org-a" in summary and "org-b" in summary and "合計" in summary


def test_org_option_selects_single_org(make_input, tmp_path):
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-b")
    assert rc == 0
    assert (out / "org-b" / "2026-06" / "report.md").exists()
    assert not (out / "org-a").exists()
    # 単一組織のみの分析では横断サマリは作らない
    assert not (out / "summary").exists()


def test_org_name_in_report_title(make_input, tmp_path):
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-a")
    assert rc == 0
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert "org-a — 2026-06" in md.splitlines()[0]


def test_unknown_org_errors(make_input, tmp_path, capsys):
    input_dir = _make_two_orgs(make_input)
    rc, _ = _run(input_dir, tmp_path, "--org", "nope")
    assert rc == 1
    assert "組織が見つかりません" in capsys.readouterr().err


def test_month_missing_in_one_org_is_skipped(make_input, tmp_path, capsys):
    input_dir = _make_two_orgs(make_input)  # org-b は 2026-05 が無い
    rc, out = _run(input_dir, tmp_path, "--month", "2026-05")
    assert rc == 0
    assert (out / "org-a" / "2026-05" / "report.md").exists()
    assert not (out / "org-b").exists()
    assert "スキップした組織: org-b" in capsys.readouterr().out


def test_legacy_flat_layout_still_works(make_input, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"],
    )
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06")
    assert rc == 0
    # 旧レイアウトは reports/<月>/ 直下（組織ディレクトリなし）
    assert (out / "2026-06" / "report.md").exists()


def test_legacy_layout_rejects_org_option(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"],
    )
    rc, _ = _run(input_dir, tmp_path, "--org", "org-a")
    assert rc == 1
    assert "組織ディレクトリがありません" in capsys.readouterr().err


def test_mixed_layout_errors(make_input, tmp_path, capsys):
    input_dir = _make_two_orgs(make_input)
    make_input({"2026-06": [spend_row("c@z.jp", 5.0)]})  # 直下にも spend/ を作る
    rc, _ = _run(input_dir, tmp_path)
    assert rc == 1
    assert "混在" in capsys.readouterr().err
