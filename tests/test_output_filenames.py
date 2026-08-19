"""生成物のファイル名（`{種別}-{YYYYMM}-{組織名}.{拡張子}`）と旧名の扱いのテスト。

成果物は共有のためフォルダの外へ出る。ファイル名だけでどの組織のいつの分析かが分かる
ことをここで固定する。名前を組み立てるのは `report.naming` の1箇所だけなので、
規則そのものはリテラルで書いて突き合わせる（ヘルパを使うと同語反復になる）。

旧名（種別だけの report.md 等）で生成済みの成果物は自動で改名も削除もしない。
読み取りだけ「新名があればそちら、無ければ旧名」で解決する。改名後の最初の再生成で、
旧名のファイルに書かれた手書きの考察を見失わないようにするための後方互換。
"""

from pathlib import Path

import pytest

from seat_analyzer import discussion, report
from seat_analyzer.cli import main
from seat_analyzer.config import load_config

from .conftest import CONFIG, run_analyze, spend_row

BODY = "### 引き継がれた考察\n\n" + "旧名のファイルに人が書いた本文。" * 12

# 種別ごとの期待ファイル名（org-a / 2026-06）。正式5・速報2。
FULL_NAMES = {
    "report-202606-org-a.md",
    "details-202606-org-a.md",
    "dashboard-202606-org-a.html",
    "recommendations-202606-org-a.csv",
    "usage-summary-202606-org-a.csv",
}
PREVIEW_NAMES = {
    "preview-202606-org-a.md",
    "preview-dashboard-202606-org-a.html",
}


def _month_dir(out: Path, org: str = "org-a", month: str = "2026-06") -> Path:
    return out / org / month


def test_analyze_writes_the_five_named_artifacts(two_orgs, tmp_path):
    """正式分析の5種すべてが `{種別}-{YYYYMM}-{組織名}` で出る（月ディレクトリ名は従来のまま）。"""
    out = run_analyze(two_orgs, tmp_path)
    assert {p.name for p in _month_dir(out).iterdir()} == FULL_NAMES


def test_preview_writes_the_two_named_artifacts(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path, "--preview", "--days", "10")
    assert {p.name for p in _month_dir(out).iterdir()} == PREVIEW_NAMES


def test_month_in_the_name_has_no_hyphen(two_orgs, tmp_path):
    """月はハイフン無しの YYYYMM。ディレクトリ名（YYYY-MM）とは別の書き方にする。"""
    out = run_analyze(two_orgs, tmp_path)
    assert report.REPORT.name("2026-06", "org-a") == "report-202606-org-a.md"
    assert _month_dir(out).name == "2026-06"


def test_cross_org_summary_keeps_its_name(two_orgs, tmp_path):
    """横断サマリは対象外（担当者へ共有しない内部ファイルで、月は既に名前にある）。"""
    out = run_analyze(two_orgs, tmp_path)
    assert (out / "summary" / "2026-06.md").is_file()
    assert [p.name for p in (out / "summary").iterdir()] == ["2026-06.md"]


def test_summary_links_point_at_the_new_names(two_orgs, tmp_path):
    """横断サマリのリンク先が実在する（名前を変えたらリンクも追従する）。"""
    out = run_analyze(two_orgs, tmp_path)
    summary = out / "summary" / "2026-06.md"
    text = summary.read_text(encoding="utf-8")
    for org in ("org-a", "org-b"):
        rel = f"../{org}/2026-06/report-202606-{org}.md"
        assert f"[{org}]({rel})" in text
        assert (summary.parent / rel).resolve().is_file()


# ---------------------------------------------------------------- 旧名の扱い


def _write_legacy(out: Path, artifact, text: str, org: str = "org-a",
                  month: str = "2026-06") -> Path:
    """旧名（種別だけ）のファイルを月ディレクトリへ置く。"""
    path = artifact.legacy_path(out / org, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _legacy_only(out: Path, org: str = "org-a", month: str = "2026-06") -> None:
    """新名で生成済みの成果物を旧名へ改名する（改名前に生成した月を再現する）。"""
    for path in list((out / org / month).iterdir()):
        stem, _, rest = path.name.partition(f"-{month.replace('-', '')}-{org}")
        assert rest == path.suffix, path.name
        path.rename(path.with_name(stem + path.suffix))


def test_regeneration_carries_the_discussion_from_the_old_name(two_orgs, tmp_path):
    """旧名のレポートに書かれた考察を、改名後の初回再生成で引き継ぐ。

    手書きの考察は他のどこにも無いので、ここを読み落とすと失われる。
    """
    out = run_analyze(two_orgs, tmp_path)
    report.write_discussion(report.REPORT.path(out / "org-a", "2026-06", "org-a"), BODY)
    _legacy_only(out)

    run_analyze(two_orgs, tmp_path)   # 同じ出力先へ再生成
    new_path = report.REPORT.path(out / "org-a", "2026-06", "org-a")
    assert report.discussion_body(new_path.read_text(encoding="utf-8")) == BODY.strip()


def test_regeneration_carries_the_preview_discussion_from_the_old_name(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path, "--preview", "--days", "10")
    report.write_discussion(report.PREVIEW.path(out / "org-a", "2026-06", "org-a"), BODY)
    _legacy_only(out)

    run_analyze(two_orgs, tmp_path, "--preview", "--days", "10")
    new_path = report.PREVIEW.path(out / "org-a", "2026-06", "org-a")
    assert report.discussion_body(new_path.read_text(encoding="utf-8")) == BODY.strip()


def test_regeneration_leaves_the_old_files_alone(two_orgs, tmp_path):
    """旧名のファイルは消さない・改名しない（意図的に残された成果物を壊さないため）。"""
    out = run_analyze(two_orgs, tmp_path)
    _legacy_only(out)
    legacy = {p.name: p.read_bytes() for p in _month_dir(out).iterdir()}

    run_analyze(two_orgs, tmp_path)
    after = {p.name for p in _month_dir(out).iterdir()}
    assert after == set(legacy) | FULL_NAMES     # 新旧が併存する
    for name, content in legacy.items():
        assert (_month_dir(out) / name).read_bytes() == content


def test_the_new_name_wins_when_both_exist(two_orgs, tmp_path):
    """新旧が併存する月では新名を読む（考察の引き継ぎも discuss の資料も）。"""
    out = run_analyze(two_orgs, tmp_path)
    org_output = out / "org-a"
    report.write_discussion(report.REPORT.path(org_output, "2026-06", "org-a"), BODY)
    _write_legacy(out, report.REPORT, "# 旧\n\n## 考察\n\n### 旧名の考察\n\n古い本文。\n")

    assert discussion.document_path(org_output, "2026-06", False, "org-a") == \
        report.REPORT.path(org_output, "2026-06", "org-a")

    run_analyze(two_orgs, tmp_path)
    merged = report.REPORT.path(org_output, "2026-06", "org-a").read_text(encoding="utf-8")
    assert report.discussion_body(merged) == BODY.strip()
    assert "旧名の考察" not in merged


def test_discuss_reads_the_materials_from_the_old_names(two_orgs, tmp_path, capsys):
    """旧名だけの月でも discuss が資料（本文・詳細資料・推奨一覧）を読める。"""
    out = run_analyze(two_orgs, tmp_path)
    _legacy_only(out)
    capsys.readouterr()

    outcome = discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        runner=lambda prompt, s: BODY, dry_run=True,
    )
    prompt = outcome.prompt
    assert "資料2: 分析詳細資料 details.md（2026-06）" in prompt
    assert "資料3: ユーザ別推奨一覧 recommendations.csv（2026-06）" in prompt
    assert "## 全ユーザ" in prompt          # 詳細資料の表が資料に入っている


def test_discuss_writes_into_the_old_named_document(two_orgs, tmp_path, monkeypatch):
    """旧名だけの月では、その旧名のレポートに考察を書き込む（読み先と書き先を揃える）。"""
    out = run_analyze(two_orgs, tmp_path)
    _legacy_only(out, org="org-a")
    _legacy_only(out, org="org-b")
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)

    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 0
    for org in ("org-a", "org-b"):
        legacy = report.REPORT.legacy_path(out / org, "2026-06")
        assert report.discussion_body(legacy.read_text(encoding="utf-8")) == BODY.strip()
        assert not report.REPORT.path(out / org, "2026-06", org).exists()


def test_previous_month_discussion_is_read_from_the_old_name(two_orgs, tmp_path):
    """前月の考察も旧名から読む（`--with-previous-discussion` の資料）。"""
    out = tmp_path / "reports"
    assert main(["analyze", "--config", CONFIG, "--input-dir", str(two_orgs),
                 "--output-dir", str(out), "--month", "2026-05", "--org", "org-a"]) == 0
    prev = "### 前月の所見\n\n" + "前月は Premium 継続と判断した。" * 10
    report.write_discussion(report.REPORT.path(out / "org-a", "2026-05", "org-a"), prev)
    _legacy_only(out, month="2026-05")

    run_analyze(two_orgs, tmp_path)
    calls: list[str] = []
    discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        runner=lambda prompt, s: calls.append(prompt) or BODY,
        include_previous=True,
    )
    assert "前月の所見" in calls[0]


# ---------------------------------------------------------------- 命名ヘルパ


@pytest.mark.parametrize("artifact,expected", [
    (report.REPORT, "report-202607-acme.md"),
    (report.DETAILS, "details-202607-acme.md"),
    (report.DASHBOARD, "dashboard-202607-acme.html"),
    (report.RECOMMENDATIONS, "recommendations-202607-acme.csv"),
    (report.USAGE_SUMMARY, "usage-summary-202607-acme.csv"),
    (report.PREVIEW, "preview-202607-acme.md"),
    (report.PREVIEW_DASHBOARD, "preview-dashboard-202607-acme.html"),
])
def test_artifact_names(artifact, expected):
    """種別名は従来のベース名のまま（ハイフンを含む種別もそのまま使う）。"""
    assert artifact.name("2026-07", "acme") == expected
    assert artifact.legacy_name == expected.replace("-202607-acme", "")


def test_existing_path_falls_back_only_when_the_new_name_is_absent(tmp_path):
    org_output = tmp_path / "acme"
    (org_output / "2026-07").mkdir(parents=True)
    new = report.REPORT.path(org_output, "2026-07", "acme")
    legacy = report.REPORT.legacy_path(org_output, "2026-07")

    # どちらも無ければ新名（「無い」ことの扱いは呼び出し側に任せる）
    assert report.REPORT.existing_path(org_output, "2026-07", "acme") == new
    legacy.write_text("旧\n", encoding="utf-8", newline="\n")
    assert report.REPORT.existing_path(org_output, "2026-07", "acme") == legacy
    new.write_text("新\n", encoding="utf-8", newline="\n")
    assert report.REPORT.existing_path(org_output, "2026-07", "acme") == new


def test_org_name_goes_into_the_filename_verbatim(make_input, tmp_path):
    """組織名はディレクトリ名と同じ文字列をそのまま使う（加工・置換をしない）。

    ドットやアンダースコアを含む名前も `ingest.validate_org_name` を通っており、
    そのままファイル名に使える。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Standard"], org="org.x_1",
    )
    output_dir = tmp_path / "reports"
    assert main(["analyze", "--config", CONFIG, "--input-dir", str(input_dir),
                 "--output-dir", str(output_dir), "--month", "2026-06"]) == 0
    assert (output_dir / "org.x_1" / "2026-06" / "report-202606-org.x_1.md").is_file()
