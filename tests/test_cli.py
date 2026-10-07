"""CLI のマルチ組織対応（組織解決・--org・横断サマリ・旧レイアウトの拒否）と doctor のテスト。"""

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from seat_analyzer import analyze, claude_export, github_collect, ingest, seat_changes
from seat_analyzer.analyze import pipeline
from seat_analyzer.cli import main
from seat_analyzer.github_collect import (
    PR_CACHE_DIRNAME,
    PR_CACHE_SCHEMA,
    month_windows,
)
from seat_analyzer.ingest import discover_orgs
from seat_analyzer.product_usage import FEATURE_COLUMNS
from seat_analyzer.report import (
    DASHBOARD,
    DECISION_EVIDENCE,
    DETAILS,
    GITHUB_SUMMARY,
    PREVIEW,
    PREVIEW_DASHBOARD,
    RECOMMENDATIONS,
    REPORT,
    USAGE_SUMMARY,
)

from .conftest import CONFIG, REPO_ROOT, SPEND_HEADER, out_file, spend_row


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
    assert out_file(out, REPORT).exists()
    assert out_file(out, DASHBOARD, org="org-b").exists()
    summary = (out / "summary" / "2026-06.md").read_text(encoding="utf-8")
    assert "org-a" in summary and "org-b" in summary and "合計" in summary


def test_org_option_selects_single_org(make_input, tmp_path):
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-b")
    assert rc == 0
    assert out_file(out, REPORT, org="org-b").exists()
    assert not (out / "org-a").exists()
    # 単一組織のみの分析では横断サマリは作らない
    assert not (out / "summary").exists()


def test_org_name_in_report_title(make_input, tmp_path):
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-a")
    assert rc == 0
    md = out_file(out, REPORT).read_text(encoding="utf-8")
    assert "org-a — 2026-06" in md.splitlines()[0]


def _usage_rows(path: Path) -> list[list[str]]:
    text = path.read_bytes().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def test_analyze_writes_usage_summary(make_input, tmp_path, cfg, capsys):
    """usage-summary.csv が成果物として生成され、出力一覧に載る。"""
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-a")
    assert rc == 0
    path = out_file(out, USAGE_SUMMARY)
    assert f"usage: {path}" in capsys.readouterr().out

    rows = _usage_rows(path)
    assert rows[0] == ["email", *FEATURE_COLUMNS]
    # 内容は分析結果が持つ特徴量そのもの（CSV 側で読み直し・再計算をしていない）
    features = analyze.analyze(input_dir / "org-a", "2026-06", cfg, org="org-a") \
        .product_usage.features
    assert [r[0] for r in rows[1:]] == list(features.index)
    values = dict(zip(rows[0], rows[1]))
    assert values["total_demand_usd"] == f"{float(features.iloc[0, 0]):.2f}"
    assert values["code_demand_share"] == "1.0000"     # 明細は Claude Code のみ


def _prohibited_config(tmp_path: Path, product: str) -> str:
    """指定した product を禁止扱いにする上書き設定（他のキーは既定のまま）。"""
    path = tmp_path / "config.yaml"
    path.write_text(f'product_policy:\n  prohibited: ["{product}"]\n',
                    encoding="utf-8", newline="\n")
    return str(path)


def _run_with_config(input_dir: Path, tmp_path: Path, config: str) -> tuple[int, Path]:
    output_dir = tmp_path / "reports"
    rc = main([
        "analyze", "--config", config, "--input-dir", str(input_dir),
        "--output-dir", str(output_dir), "--month", "2026-06",
    ])
    return rc, output_dir


# email に "@" を含まない行は、シート判定の対象外の組織サービス利用として扱われる
ORG_SERVICE_EMAIL = "(org service usage)"

# ユーザ向けの警告（人数で数える）と組織サービス向けの警告（行数で数える）の目印
USER_PROHIBITED = "product の利用行があります"
ORG_PROHIBITED = "product の組織サービス利用行があります"


def test_prohibited_product_is_warned(make_input, tmp_path, capsys):
    """禁止指定した product の利用行があれば実行時に警告する。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, product="Chat")]},
        members=["a@x.jp,Standard"], org="org-a",
    )
    assert _run_with_config(input_dir, tmp_path, _prohibited_config(tmp_path, "Chat"))[0] == 0
    out = capsys.readouterr().out
    assert "--- 警告 ---" in out
    assert USER_PROHIBITED in out and "Chat" in out


def test_prohibited_warning_absent_without_observation(make_input, tmp_path, capsys):
    """同じ設定でも、その product の利用行が無ければ警告しない。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, product="Claude Code")]},
        members=["a@x.jp,Standard"], org="org-a",
    )
    assert _run_with_config(input_dir, tmp_path, _prohibited_config(tmp_path, "Chat"))[0] == 0
    assert "禁止指定された product" not in capsys.readouterr().out


def test_prohibited_product_in_org_service_rows_is_warned(make_input, tmp_path, capsys):
    """禁止 product が組織サービス利用の行にしか無くても警告する。

    特徴量はシート判定の対象になるユーザ行だけで計算するため、この行は
    usage-summary.csv には現れない。警告が唯一の経路になる。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, product="Claude Code"),
                     spend_row(ORG_SERVICE_EMAIL, 3.0, product="Code Review")]},
        members=["a@x.jp,Standard"], org="org-a",
    )
    config = _prohibited_config(tmp_path, "Code Review")
    rc, out_dir = _run_with_config(input_dir, tmp_path, config)
    assert rc == 0
    out = capsys.readouterr().out
    assert ORG_PROHIBITED in out and "Code Review" in out and "1 行" in out
    assert USER_PROHIBITED not in out          # ユーザは誰も使っていない

    # ユーザ単位の特徴量は組織サービス利用行の影響を受けない
    rows = _usage_rows(out_file(out_dir, USAGE_SUMMARY))
    assert [r[0] for r in rows[1:]] == ["a@x.jp"]
    assert dict(zip(rows[0], rows[1]))["prohibited_observed"] == "False"


def test_prohibited_product_warned_for_users_and_org_service_separately(
    make_input, tmp_path, capsys
):
    """ユーザ行と組織サービス利用行の両方にあれば、単位の違う警告が両方出る。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, product="Code Review"),
                     spend_row(ORG_SERVICE_EMAIL, 3.0, product="Code Review")]},
        members=["a@x.jp,Standard"], org="org-a",
    )
    config = _prohibited_config(tmp_path, "Code Review")
    assert _run_with_config(input_dir, tmp_path, config)[0] == 0
    out = capsys.readouterr().out
    assert USER_PROHIBITED in out and "1 名" in out
    assert ORG_PROHIBITED in out and "1 行" in out


def test_unknown_org_errors(make_input, tmp_path, capsys):
    input_dir = _make_two_orgs(make_input)
    rc, _ = _run(input_dir, tmp_path, "--org", "nope")
    assert rc == 1
    assert "組織が見つかりません" in capsys.readouterr().err


def test_month_missing_in_one_org_is_skipped(make_input, tmp_path, capsys):
    input_dir = _make_two_orgs(make_input)  # org-b は 2026-05 が無い
    rc, out = _run(input_dir, tmp_path, "--month", "2026-05")
    assert rc == 0
    assert out_file(out, REPORT, month="2026-05").exists()
    assert not (out / "org-b").exists()
    assert "スキップした組織: org-b" in capsys.readouterr().out


def _assert_migration_guidance(err: str) -> None:
    """旧レイアウトを検出したときの案内（何をどこへ移すか）が出ていること。

    spend/ だけを移しても分析は始まらないため、移す対象を漏れなく挙げていることまで
    確かめる（members/ が無い状態は analyze のエラーになる）。
    """
    assert "旧レイアウト" in err
    assert "init-org" in err and "<組織名>" in err
    for item in ("spend/", "members/", "code-analytics/", "members-info"):
        assert item in err
    assert "docs/setup.md" in err


def test_flat_layout_errors_with_migration_guidance(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"],
    )
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06")
    assert rc == 1
    _assert_migration_guidance(capsys.readouterr().err)
    # 黙って無視も部分的な出力もしない
    assert not out.exists()


def test_flat_layout_errors_with_org_option(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"],
    )
    rc, _ = _run(input_dir, tmp_path, "--org", "org-a")
    assert rc == 1
    _assert_migration_guidance(capsys.readouterr().err)


def test_flat_layout_beside_orgs_errors(make_input, tmp_path, capsys):
    input_dir = _make_two_orgs(make_input)
    make_input({"2026-06": [spend_row("c@z.jp", 5.0)]})  # 直下にも spend/ を作る
    rc, out = _run(input_dir, tmp_path)
    assert rc == 1
    # 組織ディレクトリがあっても、直下のデータを黙って無視して分析を進めない
    _assert_migration_guidance(capsys.readouterr().err)
    assert not out.exists()


def test_flat_layout_errors_in_discuss(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Standard"],
    )
    rc = main([
        "discuss", "--config", CONFIG, "--dry-run",
        "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    _assert_migration_guidance(capsys.readouterr().err)


# --- --decision-version（V2 判定の根拠の併記） ---
#
# 判定そのものは tests/test_decision_evidence.py が固定する。ここで確かめるのは
# 「どの実行で decision-evidence を書くか」と「V1 の成果物が変わらないこと」。

# V1 の成果物5種（decision-evidence を書いても1バイトも変わらないこと）
_V1_ARTIFACTS = (REPORT, DETAILS, DASHBOARD, RECOMMENDATIONS, USAGE_SUMMARY)


def _v2_config(tmp_path: Path) -> str:
    """V2 判定を既定で併記する上書き設定（他のキーは既定のまま）。"""
    path = tmp_path / "config.yaml"
    path.write_text("decision_v2:\n  enabled: true\n", encoding="utf-8", newline="\n")
    return str(path)


def _run_with(input_dir: Path, output_dir: Path, config: str, *extra: str) -> int:
    return main([
        "analyze", "--config", config,
        "--input-dir", str(input_dir), "--output-dir", str(output_dir),
        "--month", "2026-06", *extra,
    ])


def test_decision_evidence_is_not_written_by_default(make_input, tmp_path, capsys):
    """既定は V1。V2 判定の根拠は書かず、出力一覧にも載せない。"""
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-a")
    assert rc == 0
    assert not out_file(out, DECISION_EVIDENCE).exists()
    assert "evidence:" not in capsys.readouterr().out


def test_decision_version_v2_writes_the_evidence(make_input, tmp_path, capsys):
    """--decision-version v2 で根拠 CSV を書き、出力一覧と内訳を出す。"""
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-a",
                   "--decision-version", "v2")
    assert rc == 0
    path = out_file(out, DECISION_EVIDENCE)
    assert path.is_file()
    printed = capsys.readouterr().out
    assert f"evidence: {path}" in printed
    assert "V2判定:" in printed


def test_v2_does_not_change_the_v1_artifacts(make_input, tmp_path):
    """V2 の併記は V1 の成果物を1バイトも変えない。"""
    input_dir = _make_two_orgs(make_input)
    v1_out, v2_out = tmp_path / "v1", tmp_path / "v2"
    assert _run_with(input_dir, v1_out, CONFIG, "--org", "org-a") == 0
    assert _run_with(input_dir, v2_out, CONFIG, "--org", "org-a",
                     "--decision-version", "v2") == 0
    for artifact in _V1_ARTIFACTS:
        assert out_file(v2_out, artifact).read_bytes() == \
            out_file(v1_out, artifact).read_bytes()
    # 増えるのは根拠 CSV の1ファイルだけ
    assert {p.name for p in (v2_out / "org-a" / "2026-06").iterdir()} - \
        {p.name for p in (v1_out / "org-a" / "2026-06").iterdir()} == \
        {DECISION_EVIDENCE.name("2026-06", "org-a")}


def test_config_can_enable_the_evidence_and_the_flag_can_turn_it_off(
    make_input, tmp_path
):
    """設定の decision_v2.enabled で併記でき、--decision-version v1 で戻せる。"""
    input_dir = _make_two_orgs(make_input)
    config = _v2_config(tmp_path)
    enabled_out, back_out = tmp_path / "on", tmp_path / "off"
    assert _run_with(input_dir, enabled_out, config, "--org", "org-a") == 0
    assert out_file(enabled_out, DECISION_EVIDENCE).is_file()

    assert _run_with(input_dir, back_out, config, "--org", "org-a",
                     "--decision-version", "v1") == 0
    assert not out_file(back_out, DECISION_EVIDENCE).exists()


def test_v1_run_warns_about_a_leftover_evidence_file(make_input, tmp_path, capsys):
    """V1 で実行したときに前回の根拠 CSV が残っていれば知らせる（消さない）。"""
    input_dir = _make_two_orgs(make_input)
    out = tmp_path / "reports"
    assert _run_with(input_dir, out, CONFIG, "--org", "org-a",
                     "--decision-version", "v2") == 0
    path = out_file(out, DECISION_EVIDENCE)
    before = path.read_bytes()
    capsys.readouterr()

    assert _run_with(input_dir, out, CONFIG, "--org", "org-a") == 0
    printed = capsys.readouterr().out
    assert f"{path.name} は今回の実行では更新されません" in printed
    assert "--decision-version v2" in printed
    assert path.read_bytes() == before      # 旧い成果物はツールが動かさない


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_decision_version_cannot_be_combined_with_preview(
    make_input, tmp_path, capsys, version
):
    """速報モードは V2 判定を行わないので、判定の版を指定させない。"""
    input_dir = _make_two_orgs(make_input)
    rc = _run_with(input_dir, tmp_path / "reports", CONFIG, "--preview", "--days", "10",
                   "--decision-version", version)
    assert rc == 1
    assert "--decision-version" in capsys.readouterr().err


def test_preview_with_the_config_enabled_says_v2_is_skipped(make_input, tmp_path, capsys):
    """設定で有効にした V2 を速報で黙って無視しない（組織ごとに1行知らせる）。"""
    input_dir = _make_two_orgs(make_input)
    out = tmp_path / "reports"
    assert _run_with(input_dir, out, _v2_config(tmp_path), "--org", "org-a",
                     "--preview", "--days", "10") == 0
    assert "速報モードでは V2 判定を行いません" in capsys.readouterr().out
    assert not out_file(out, DECISION_EVIDENCE).exists()


def test_v2_writes_one_evidence_file_per_org(make_input, tmp_path):
    """複数組織の実行では組織ごとに根拠 CSV を書く。"""
    input_dir = _make_two_orgs(make_input)
    out = tmp_path / "reports"
    assert _run_with(input_dir, out, CONFIG, "--decision-version", "v2") == 0
    for org in ("org-a", "org-b"):
        assert out_file(out, DECISION_EVIDENCE, org=org).is_file()


def _counting(monkeypatch, module, name: str) -> list[int]:
    """module.name の呼び出し回数を数える（元の実装はそのまま呼ぶ）。"""
    calls: list[int] = []
    original = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_v1_run_adds_no_extra_computation(make_input, tmp_path, monkeypatch):
    """V1 の実行では対象月の product 特徴量だけを計算し、シート変更の検出もしない。

    V2 の結線で V1 の実行に計算・読み取りが増えていないことを固定する（増えても
    出力は変わらないため、他のテストでは気づけない）。
    """
    input_dir = _make_two_orgs(make_input)      # org-a は 2026-05・2026-06 の2ヶ月
    features = _counting(monkeypatch, pipeline, "compute_product_usage")
    detect = _counting(monkeypatch, seat_changes, "detect_from_input")

    assert _run_with(input_dir, tmp_path / "reports", CONFIG, "--org", "org-a") == 0
    assert len(features) == 1     # 対象月のみ（過去月の特徴量は計算しない）
    assert detect == []


def test_v2_run_computes_features_for_every_month_and_detects_changes(
    make_input, tmp_path, monkeypatch
):
    """V2 の実行では履歴の全月の特徴量を計算し、シート変更の検出を組織ごとに1度行う。"""
    input_dir = _make_two_orgs(make_input)
    features = _counting(monkeypatch, pipeline, "compute_product_usage")
    detect = _counting(monkeypatch, seat_changes, "detect_from_input")

    assert _run_with(input_dir, tmp_path / "reports", CONFIG,
                     "--decision-version", "v2") == 0
    assert len(features) == 3     # org-a の2ヶ月 + org-b の1ヶ月
    assert len(detect) == 2       # 組織ごとに1度


# --- github-summary（GitHub 由来の参考値） ---
#
# 要約そのものは tests/test_github_metrics.py が、CSV の形は tests/test_github_csv.py が
# 固定する。ここで確かめるのは「どの実行で github-summary を書くか」「書けないときに
# 何を伝えるか」「V1 の成果物が変わらないこと」「analyze が gh を呼ばないこと」。

# GitHub 分析を有効にするときの Organization 名（doctor・collect の検査でも使う）
GH_ORG = "example-org"

# キャッシュに載せる repository の一覧（analyze はこれを集計の対象として読む）
CACHE_REPOSITORIES = {"names": ["repo-a"], "excluded": 1}


def _gh_config(tmp_path: Path, **entries: str) -> str:
    """organizations だけを書いた上書き設定を作り、そのパスを返す。"""
    lines = ["organizations:"]
    for org, github_org in entries.items():
        lines += [f"  {org}:", f"    github_org: {github_org}"]
    path = tmp_path / "gh-config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return str(path)


def _mapping(input_dir: Path, org: str, rows: tuple[str, ...]) -> None:
    """email → GitHub login の対応表（members-info の GitHub ID 列）。"""
    (input_dir / org / "members-info.csv").write_text(
        "email,GitHub ID\n" + "".join(f"{row}\n" for row in rows),
        encoding="utf-8", newline="\n",
    )


def _pr_entry(number: int = 12, repository: str = "repo-a", login: str = "alice-dev",
              author_type: str = "User", merged: str = "2026-06-03T12:00:00Z") -> dict:
    """キャッシュに保存された merged PR 1件（9項目）。"""
    return {
        "repository": repository, "number": number,
        "author_login": login, "author_type": author_type,
        "created_at": "2026-06-01T00:00:00Z", "merged_at": merged,
        "additions": 10, "deletions": 2, "is_draft": False,
    }


def _write_pr_cache(input_dir: Path, *, org: str = "org-a", month: str = "2026-06",
                    entries: list[dict] | None = None,
                    repositories: dict | None = CACHE_REPOSITORIES,
                    github_org: str = GH_ORG, text: str | None = None) -> Path:
    """collect が書いたキャッシュ相当のファイルを直接置く。

    analyze は gh もネットワークも呼ばずこのファイルだけを読むので、収集の経路を
    通さずに参考値の出力を組み立てられる。text を渡すとその字句をそのまま書く
    （読めないキャッシュを表すため）。
    """
    path = input_dir / org / PR_CACHE_DIRNAME / f"prs-{month}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8", newline="\n")
        return path
    found = [_pr_entry()] if entries is None else entries
    payload = {
        "schema": PR_CACHE_SCHEMA,
        "github_org": github_org,
        "month": month,
        "complete_windows": [
            [window.start.isoformat(), window.end.isoformat()]
            for window in month_windows(month)
        ],
        "prs": {f"{e['repository']}#{e['number']}": e for e in found},
    }
    if repositories is not None:
        payload["repositories"] = repositories
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    return path


def test_github_summary_is_written_for_a_gated_org(make_input, tmp_path, capsys):
    """有効にした組織で対象月のキャッシュがあれば参考値を書き、出力一覧と要約を出す。"""
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,alice-dev",))
    _write_pr_cache(input_dir)
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    path = out_file(out, GITHUB_SUMMARY)
    assert path.is_file()
    printed = capsys.readouterr().out
    assert f"github: {path}" in printed
    assert "GitHub（参考値）" in printed
    assert "lead time（組織全体）" in printed


def test_github_summary_does_not_change_the_v1_artifacts(make_input, tmp_path):
    """参考値の併記は V1 の成果物を1バイトも変えない。"""
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,alice-dev",))
    _write_pr_cache(input_dir)
    plain, gated = tmp_path / "plain", tmp_path / "gated"

    assert _run_with(input_dir, plain, CONFIG, "--org", "org-a") == 0
    assert _run_with(input_dir, gated, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    for artifact in _V1_ARTIFACTS:
        assert out_file(gated, artifact).read_bytes() == \
            out_file(plain, artifact).read_bytes()
    # 増えるのは参考値の1ファイルだけ
    assert {p.name for p in (gated / "org-a" / "2026-06").iterdir()} - \
        {p.name for p in (plain / "org-a" / "2026-06").iterdir()} == \
        {GITHUB_SUMMARY.name("2026-06", "org-a")}


def test_an_org_without_the_gate_gets_no_summary_and_no_notice(
    make_input, tmp_path, capsys
):
    """設定に無い組織は GitHub 関連の処理と通知の一切から外れる。"""
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-b", ("b@y.jp,bob-42",))
    _write_pr_cache(input_dir, org="org-b")
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-b") == 0
    assert not out_file(out, GITHUB_SUMMARY, org="org-b").exists()
    printed = capsys.readouterr().out
    assert "github" not in printed and "GitHub" not in printed


def test_a_gated_org_without_a_cache_says_what_to_run(make_input, tmp_path, capsys):
    """キャッシュが無い月は書かずに次の一手を案内する（GitHub 無しでも成功する）。"""
    input_dir = _make_two_orgs(make_input)
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    assert not out_file(out, GITHUB_SUMMARY).exists()
    printed = capsys.readouterr().out
    assert "github-summary は書きません" in printed
    assert "collect --org org-a --source github --month 2026-06" in printed


def test_a_cache_without_a_repository_listing_says_to_collect_again(
    make_input, tmp_path, capsys
):
    """一覧を持たない旧いキャッシュは材料にならない（再収集で一覧が付く）。"""
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,alice-dev",))
    _write_pr_cache(input_dir, repositories=None)
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    assert not out_file(out, GITHUB_SUMMARY).exists()
    printed = capsys.readouterr().out
    assert "repository の一覧が無いため" in printed
    assert "再実行すると一覧が保存されます" in printed


def test_preview_says_the_summary_is_skipped(make_input, tmp_path, capsys):
    """有効にした GitHub 分析を速報で黙って無視しない（組織ごとに1行知らせる）。"""
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,alice-dev",))
    _write_pr_cache(input_dir)
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a",
                     "--preview", "--days", "10") == 0
    assert not out_file(out, GITHUB_SUMMARY).exists()
    assert "速報モードでは github-summary を書きません" in capsys.readouterr().out


def test_a_broken_cache_stops_the_run(make_input, tmp_path, capsys):
    """読めないキャッシュで黙って参考値を落とさない（エラーで止める）。"""
    input_dir = _make_two_orgs(make_input)
    path = _write_pr_cache(input_dir, text='{"schema":')
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}),
                     "--org", "org-a") == 1
    err = capsys.readouterr().err
    assert "JSON として読めません" in err and path.name in err
    assert not out_file(out, GITHUB_SUMMARY).exists()


def test_analyze_never_calls_gh(make_input, tmp_path, monkeypatch, capsys):
    """参考値の材料はキャッシュだけ（オフラインでも同じ結果になる）。"""
    def fail(args):
        raise AssertionError(f"analyze が gh を呼びました: {tuple(args)}")

    monkeypatch.setattr(github_collect, "run_gh", fail)
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,alice-dev",))
    _write_pr_cache(input_dir)
    out = tmp_path / "reports"

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    assert out_file(out, GITHUB_SUMMARY).is_file()
    assert "GitHub（参考値）" in capsys.readouterr().out


def _with_a_leftover_summary(make_input, tmp_path, capsys) -> tuple[Path, Path, bytes]:
    """参考値を1度書いた状態を作る（入力ディレクトリ・出力ディレクトリ・その中身）。"""
    input_dir = _make_two_orgs(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,alice-dev",))
    _write_pr_cache(input_dir)
    out = tmp_path / "reports"
    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    before = out_file(out, GITHUB_SUMMARY).read_bytes()
    capsys.readouterr()
    return input_dir, out, before


def test_a_leftover_summary_is_reported_when_it_cannot_be_written(
    make_input, tmp_path, capsys
):
    """有効な組織で書けなかった実行では、残っている参考値の時点を知らせる（消さない）。"""
    input_dir, out, before = _with_a_leftover_summary(make_input, tmp_path, capsys)
    path = out_file(out, GITHUB_SUMMARY)
    # キャッシュを取り除くと、この実行では参考値を書けない
    (input_dir / "org-a" / PR_CACHE_DIRNAME / "prs-2026-06.json").unlink()

    assert _run_with(input_dir, out, _gh_config(tmp_path, **{"org-a": GH_ORG}), "--org", "org-a") == 0
    printed = capsys.readouterr().out
    assert f"{path.name} は今回の実行では更新されません" in printed
    assert path.read_bytes() == before      # 旧い成果物はツールが動かさない


def test_a_leftover_summary_is_not_mentioned_once_the_gate_is_removed(
    make_input, tmp_path, capsys
):
    """設定から外した組織では、残っている参考値にも触れない（設計書 §15.1）。

    毎月同じ通知が出続けると、対処すべき警告がその中に埋もれる。
    """
    input_dir, out, before = _with_a_leftover_summary(make_input, tmp_path, capsys)
    path = out_file(out, GITHUB_SUMMARY)

    assert _run_with(input_dir, out, CONFIG, "--org", "org-a") == 0
    printed = capsys.readouterr().out
    assert path.name not in printed and "GitHub" not in printed
    assert path.read_bytes() == before      # 触れないが消さない


# --- doctor（既存入力の検査） ---


def _doctor(input_dir: Path, *extra: str) -> int:
    return main(["doctor", "--config", CONFIG, "--input-dir", str(input_dir), *extra])


def _clean_org(make_input) -> Path:
    """問題の無い入力: 対象月とその前月のスペンド + 対象月のメンバー一覧。"""
    return make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )


def test_doctor_reports_nothing_for_clean_input(make_input, capsys):
    input_dir = _clean_org(make_input)
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "問題は見つかりませんでした" in out
    assert "エラー 0 件 / 警告 0 件" in out


def test_doctor_errors_on_missing_spend_month(make_input, capsys):
    input_dir = _clean_org(make_input)
    assert _doctor(input_dir, "--month", "2026-04") == 1
    out = capsys.readouterr().out
    assert "[error] MISSING_SPEND" in out
    assert "2026-05/2026-06" in out  # 存在する月を示す


def test_doctor_errors_on_missing_members(make_input, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-a")
    assert _doctor(input_dir, "--month", "2026-06") == 1
    assert "[error] MISSING_MEMBERS" in capsys.readouterr().out


def test_doctor_errors_on_unreadable_spend_without_leaking_path(make_input, tmp_path, capsys):
    input_dir = _clean_org(make_input)
    # 必須カラム（tokens 列）が無い CSV に差し替える
    (input_dir / "org-a" / "spend" / "spend_2026-06.csv").write_text(
        "Email,Model\na@x.jp,claude-sonnet-4-6\n", encoding="utf-8")
    assert _doctor(input_dir, "--month", "2026-06") == 1
    out = capsys.readouterr().out
    assert "[error] MISSING_SPEND" in out
    assert "必須カラムが見つかりません" in out
    # message は実行環境に依存しない（入力ディレクトリからの相対表記になる）。
    # 区切りはその OS のもの（Windows なら "\"）で、決定性は同一環境での一致を指す
    assert str(input_dir) not in out
    assert os.path.join("spend", "spend_2026-06.csv") in out


def test_doctor_warns_partial_month_and_exits_zero(make_snapshots, capsys):
    input_dir = make_snapshots(
        "2026-06", {"2026-06-15": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    # 警告だけなら exit 0
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] PARTIAL_MONTH" in out
    assert "15日分 / 暦上 30日" in out


def test_doctor_warns_missing_history_month(make_input, capsys):
    input_dir = make_input(
        {"2026-04": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] MISSING_HISTORY_MONTH" in out
    assert "2026-05" in out


def test_doctor_warns_unknown_model(make_input, capsys):
    row = spend_row("a@x.jp", 10.0).replace("claude-sonnet-4-6", "claude-mystery-1")
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [row]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] UNKNOWN_MODEL" in out
    assert "claude-mystery-1" in out


def test_doctor_warns_numeric_parse_failure(make_input, capsys):
    broken = "a@x.jp,uuid-x,Claude Code,claude-sonnet-4-6,claude,10,N/A,1000,0.0,0.0"
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [broken]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] NUMERIC_PARSE_FAILED" in out
    assert "prompt_tokens 1行" in out


def test_doctor_warns_spend_user_missing_from_members(make_input, capsys):
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)],
         "2026-06": [spend_row("a@x.jp", 10.0), spend_row("b@y.jp", 20.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] MEMBER_ROW_MISSING" in out
    assert "b@y.jp" in out


def test_doctor_warns_unrecognized_seat_type(make_input, capsys):
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Enterprise"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    assert "[warning] SEAT_TYPE_UNKNOWN" in capsys.readouterr().out


def test_doctor_warns_unassigned_seat_with_usage(make_input, capsys):
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Unassigned"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] UNASSIGNED_WITH_USAGE" in out
    assert "a@x.jp" in out


def test_doctor_warns_members_month_fallback(make_input, capsys):
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Premium"], members_month="2026-05", org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] MISSING_MEMBERS" in out
    assert "2026-06 月末時点のメンバー一覧が無いため 2026-05 のファイルを使用しています" in out


def test_doctor_members_message_when_target_month_file_exists(
    make_input, write_member_snapshots, capsys
):
    """対象月のファイルが在っても末日から遠ければ別の月を採るので、そう書かない。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        org="org-a",
    )
    write_member_snapshots(input_dir, {
        "2026-06-01": ["a@x.jp,Premium"],   # 対象月のファイルは在る（末日から29日前）
        "2026-07-08": ["a@x.jp,Premium"],   # 採用されるが通常運用の幅の外
    }, org="org-a")
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "2026-06 月末時点のメンバー一覧が無いため 2026-07 のファイルを使用しています" in out
    assert "2026-06 のメンバー一覧が無いため" not in out


@pytest.mark.parametrize(("date", "warns"), [
    ("2026-07-01", False),   # 月末までのデータを翌月の最初の営業日に取得する通常運用
    ("2026-07-07", False),   # 通常運用の幅ちょうど（末日の7日後）
    ("2026-07-08", True),    # 幅を超えると当時の構成と違いうるので従来どおり警告する
])
def test_doctor_members_snapshot_after_month_end(
    make_input, write_member_snapshots, capsys, date, warns
):
    """対象月末より後の members を、通常運用の範囲かどうかで出し分ける。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        org="org-a",
    )
    write_member_snapshots(input_dir, {date: ["a@x.jp,Premium"]}, org="org-a")
    assert _doctor(input_dir, "--month", "2026-06") == 0
    assert ("[warning] MISSING_MEMBERS" in capsys.readouterr().out) is warns


def test_doctor_json_output_is_pure_json(make_input, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0), spend_row("b@y.jp", 20.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    # --month 未指定でも stdout は JSON のみ（対象月の通知は stderr へ）
    assert _doctor(input_dir, "--format", "json") == 0
    captured = capsys.readouterr()
    assert "対象月未指定" in captured.err
    issues = json.loads(captured.out)
    assert {i["code"] for i in issues} == {"MEMBER_ROW_MISSING"}
    for issue in issues:
        assert set(issue) == {"severity", "code", "message", "scope"}
        assert issue["severity"] == "warning"
        assert issue["scope"]["org"] == "org-a"
        assert issue["scope"]["month"] == "2026-06"


def test_doctor_json_covers_all_orgs(make_input, capsys):
    input_dir = _clean_org(make_input)
    make_input({"2026-06": [spend_row("b@y.jp", 20.0)]}, org="org-b")  # members なし
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)
    # org-a は問題なし。org-b は members 欠損（初月より前は欠月にしない）
    assert [(i["scope"]["org"], i["severity"], i["code"]) for i in issues] == [
        ("org-b", "error", "MISSING_MEMBERS"),
    ]


def test_doctor_rejects_flat_layout(make_input, capsys):
    # 使い方の誤りは構造化 issue ではなく stderr + exit 1（doctor 既定の扱い）
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Premium"],
    )
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    captured = capsys.readouterr()
    _assert_migration_guidance(captured.err)
    assert captured.out == ""


def test_doctor_output_is_deterministic(make_input, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0), spend_row("b@y.jp", 20.0)]},
        members=["a@x.jp,Enterprise"], org="org-a",
    )
    outputs = []
    for _ in range(2):
        assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
    assert str(input_dir) not in outputs[0]


def test_doctor_history_gap_message_matches_analyze_behavior(make_input, cfg, capsys):
    # analyze は欠月を飛ばした過去月で連続同推奨を判定するため、欠月があっても
    # 「変更推奨」は出る。doctor が「要観察に留まる」と案内してはいけない
    input_dir = make_input(
        {"2026-04": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 12.0)]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    # analyze 側の実挙動を同じ入力で固定する（将来 analyze が変わればこのテストが落ちる）
    result = analyze.analyze(input_dir / "org-a", "2026-06", cfg, org="org-a")
    assert result.months_used == ["2026-04", "2026-06"]      # 2026-05 は欠月
    assert result.users.iloc[0]["status"] == "変更推奨"

    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] MISSING_HISTORY_MONTH" in out
    assert "要観察" not in out
    assert "変更推奨が出ることがあります" in out


def test_doctor_inspects_org_without_spend_dir(make_input, capsys):
    input_dir = _clean_org(make_input)
    (input_dir / "org-b" / "members").mkdir(parents=True)
    (input_dir / "org-b" / "members" / "members_2026-06.csv").write_text(
        "Email,Seat Type\nb@y.jp,Premium\n", encoding="utf-8")
    # 全組織モードで spend/ の無い組織を黙って除外しない
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)
    assert [(i["scope"]["org"], i["code"]) for i in issues] == [("org-b", "MISSING_SPEND")]
    # --org での明示指定でもエラー終了せず JSON を返す
    assert _doctor(input_dir, "--month", "2026-06", "--org", "org-b", "--format", "json") == 1
    assert json.loads(capsys.readouterr().out)[0]["code"] == "MISSING_SPEND"


def test_doctor_errors_on_members_with_no_rows(make_input, capsys):
    input_dir = _clean_org(make_input)
    (input_dir / "org-a" / "members" / "members_2026-06.csv").write_text(
        "Email,Seat Type\n", encoding="utf-8")
    assert _doctor(input_dir, "--month", "2026-06") == 1
    out = capsys.readouterr().out
    assert "[error] MISSING_MEMBERS" in out
    assert "データ行がありません" in out
    # 空のメンバー一覧との突き合わせ（全員が「members に居ない」）は行わない
    assert "MEMBER_ROW_MISSING" not in out


def test_doctor_reports_unreadable_csv_as_structured_issue(make_input, capsys):
    input_dir = _clean_org(make_input)
    # .csv という名前のディレクトリ（read_csv が OSError を投げる）
    (input_dir / "org-a" / "spend" / "spend_2026-07.csv").mkdir()
    assert _doctor(input_dir, "--month", "2026-07", "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)  # traceback で落ちず JSON が出る
    assert next((i["severity"], i["code"]) for i in issues) == ("error", "MISSING_SPEND")
    assert "読めません" in issues[0]["message"]


def test_doctor_json_order_is_independent_of_org_option_order(make_input, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Enterprise"], org="org-a",   # warning のみ
    )
    make_input({"2026-06": [spend_row("b@y.jp", 20.0)]}, org="org-b")  # members なし=error
    outputs = []
    for orgs in (("org-a", "org-b"), ("org-b", "org-a")):
        args = [a for org in orgs for a in ("--org", org)]
        assert _doctor(input_dir, "--month", "2026-06", "--format", "json", *args) == 1
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])[0]["severity"] == "error"


def test_doctor_warns_blank_model_cell(make_input, capsys):
    blank = "a@x.jp,uuid-x,Claude Code,,,10,1000000,100000,0.0,0.0"
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [blank]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 0
    issue = next(i for i in json.loads(capsys.readouterr().out) if i["code"] == "UNKNOWN_MODEL")
    assert "model が空の 1行" in issue["message"]
    assert issue["scope"]["blank_model_rows"] == 1
    assert issue["scope"]["models"] == []


def test_doctor_checks_members_even_without_target_month(make_input, tmp_path, capsys):
    input_dir = tmp_path / "input"
    (input_dir / "org-a" / "spend").mkdir(parents=True)   # 空の spend/、members/ なし
    assert _doctor(input_dir, "--format", "json") == 1
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == [
        "MISSING_MEMBERS", "MISSING_SPEND",
    ]


def test_doctor_checks_members_content_without_target_month(tmp_path, capsys):
    # 対象月が決まらない経路でも、ヘッダのみのメンバー一覧を error にする
    input_dir = tmp_path / "input"
    (input_dir / "org-a" / "spend").mkdir(parents=True)
    members = input_dir / "org-a" / "members"
    members.mkdir(parents=True)
    (members / "members_2026-06.csv").write_text("Email,Seat Type\n", encoding="utf-8")
    assert _doctor(input_dir, "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)
    assert [i["code"] for i in issues] == ["MISSING_MEMBERS", "MISSING_SPEND"]
    assert "データ行がありません" in issues[0]["message"]


def test_doctor_uses_latest_month_when_month_is_omitted(make_input, cfg):
    from seat_analyzer import data_quality
    input_dir = _clean_org(make_input)
    # month=None は「最新月を対象にする」意味。月が存在するのに MISSING_SPEND にしない
    issues = data_quality.inspect_input(input_dir / "org-a", None, cfg, org="org-a")
    assert issues == []


def test_doctor_reports_unreadable_input_dir_as_json(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert _doctor(missing, "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)   # stdout は JSON のまま
    assert [i["code"] for i in issues] == ["MISSING_SPEND"]
    assert "org" not in issues[0]["scope"]         # 組織を特定できない
    # 入力ディレクトリの絶対パスを message へ持ち込まない
    assert str(missing) not in issues[0]["message"]


def test_doctor_input_dir_message_is_environment_independent(tmp_path, capsys):
    messages = []
    for name in ("a", "bbbbbbbbbb"):     # 長さの違う別パスでも同じ message になる
        target = tmp_path / name / "input"
        assert _doctor(target, "--format", "json") == 1
        issue = json.loads(capsys.readouterr().out)[0]
        messages.append(issue["message"])
        assert str(target) not in issue["message"]
        assert str(tmp_path) not in issue["message"]
    assert messages[0] == messages[1]


def test_doctor_reports_missing_input_dir_with_org_option(tmp_path, capsys):
    # 入力ディレクトリが無い場合は組織名の検証より先に構造化 issue にする
    assert _doctor(tmp_path / "nope", "--org", "org-a", "--format", "json") == 1
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == ["MISSING_SPEND"]


def test_doctor_reports_input_without_org_dirs_as_issue(tmp_path, capsys):
    # 組織ディレクトリが1つも無い入力（spend/ 以外の残骸だけ）は構造化 issue にする。
    # 検査すべき組織を1つも解決できないので、組織単位の検査結果は出さない
    (tmp_path / "input" / "members").mkdir(parents=True)
    assert _doctor(tmp_path / "input", "--format", "json") == 1
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == ["MISSING_SPEND"]


def test_doctor_heading_without_org_and_month(tmp_path, capsys):
    assert _doctor(tmp_path / "nope") == 1
    assert "=== 入力検査 ===" in capsys.readouterr().out


def test_doctor_rejects_hand_made_org_named_spend(tmp_path, capsys):
    # 組織名 spend は init-org が作らせないが、手で作られたものは実行時に止める
    # （直下 spend/ と区別できないため analyze と同じ旧レイアウト扱いにする）
    org = tmp_path / "input" / "spend"
    (org / "spend").mkdir(parents=True)
    (org / "spend" / "spend_2026-06.csv").write_text(
        "Email,Model,Prompt Tokens,Completion Tokens\na@x.jp,claude-sonnet-4-6,1000,100\n",
        encoding="utf-8")
    assert _doctor(tmp_path / "input", "--month", "2026-06") == 1
    _assert_migration_guidance(capsys.readouterr().err)


def test_doctor_picks_latest_members_snapshot_without_target_month(
    tmp_path, write_member_snapshots, capsys
):
    # 同一月に複数ある場合、ファイル名順ではなくスナップショット日付の新しい方を採る
    input_dir = tmp_path / "input"
    (input_dir / "org-a" / "spend").mkdir(parents=True)
    members = input_dir / "org-a" / "members"
    members.mkdir(parents=True)
    (members / "members-z-2026-06-01.csv").write_text("Email,Seat Type\n", encoding="utf-8")
    (members / "members-a-2026-06-30.csv").write_text(
        "Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")
    assert _doctor(input_dir, "--format", "json") == 1
    # 新しい 06-30 にはデータ行があるため MISSING_MEMBERS は出ない
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == ["MISSING_SPEND"]


def test_doctor_accepts_org_named_like_input_subdir(tmp_path, capsys):
    # 組織名が members でも旧レイアウトと誤認しない（analyze は組織として扱える）
    org = tmp_path / "input" / "members"
    (org / "spend").mkdir(parents=True)
    (org / "spend" / "spend_2026-06.csv").write_text(
        "Email,Model,Prompt Tokens,Completion Tokens\na@x.jp,claude-sonnet-4-6,1000,100\n",
        encoding="utf-8")
    (org / "members").mkdir(parents=True)
    (org / "members" / "members_2026-06.csv").write_text(
        "Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")
    assert _doctor(tmp_path / "input", "--month", "2026-06", "--format", "json") == 0
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == []


def test_doctor_rejects_invalid_org_name_like_analyze(make_input, capsys):
    input_dir = _clean_org(make_input)
    (input_dir / ".hidden" / "spend").mkdir(parents=True)   # 入力構造を持つ不正名
    assert _doctor(input_dir, "--month", "2026-06") == 1
    assert "組織名が不正です" in capsys.readouterr().err


def test_doctor_distinguishes_unresolvable_filenames_from_absence(make_input, capsys):
    input_dir = _clean_org(make_input)
    # 月をまたぐ期間のファイル名（ingest はエラーにする）。--month は省略する
    (input_dir / "org-a" / "spend" / "spend-2026-06-01-to-2026-07-05.csv").write_text(
        "Email,Seat Type\n", encoding="utf-8")
    assert _doctor(input_dir, "--format", "json") == 1
    issue = next(i for i in json.loads(capsys.readouterr().out) if i["code"] == "MISSING_SPEND")
    assert "ファイル名から解決できません" in issue["message"]
    assert "期間が月をまたぐ" in issue["message"]


def test_doctor_warns_single_date_named_spend(make_input, tmp_path, capsys):
    input_dir = _clean_org(make_input)
    spend = input_dir / "org-a" / "spend"
    (spend / "spend_2026-06.csv").rename(spend / "spend-report-2026-06-15.csv")
    assert _doctor(input_dir, "--month", "2026-06") == 0
    out = capsys.readouterr().out
    assert "[warning] PARTIAL_MONTH" in out
    assert "全月データであることを確認できません" in out


def test_doctor_numeric_failure_counts_affected_rows(make_input, capsys):
    both = "a@x.jp,uuid-x,Claude Code,claude-sonnet-4-6,claude,10,N/A,bad,0.0,0.0"
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [both]},
        members=["a@x.jp,Premium"], org="org-a",
    )
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 0
    issue = next(
        i for i in json.loads(capsys.readouterr().out) if i["code"] == "NUMERIC_PARSE_FAILED"
    )
    # 1行で2列とも失敗しても影響行数は1（セル数は別キー）
    assert issue["scope"]["rows"] == 1
    assert issue["scope"]["cells"] == 2


def _tree_state(root: Path) -> list[tuple]:
    """ツリーの状態（パス・種類・サイズ・更新時刻・内容ハッシュ）。読み取り専用の検証用。"""
    state = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        state.append((
            str(path.relative_to(root)), path.is_dir(),
            stat.st_size, stat.st_mtime_ns, digest,
        ))
    return state


def test_doctor_writes_no_files(make_input, tmp_path):
    input_dir = _clean_org(make_input)
    before = _tree_state(tmp_path)
    assert _doctor(input_dir, "--month", "2026-06") == 0
    # ファイルの増減だけでなく、既存ファイルの内容・更新時刻も変わらない
    assert _tree_state(tmp_path) == before


def test_doctor_ignores_leftover_input_subdir_when_orgs_exist(make_input, capsys):
    input_dir = _clean_org(make_input)
    (input_dir / "members").mkdir()          # 移行し損ねた入力サブディレクトリの残骸
    # 直下 spend/ が無ければ analyze は組織を処理する。doctor も同じ入力で止まらない
    assert _doctor(input_dir, "--month", "2026-06") == 0
    assert "問題は見つかりませんでした" in capsys.readouterr().out


def test_doctor_reports_spend_rescan_failure_as_issue(make_input, monkeypatch, capsys):
    input_dir = _clean_org(make_input)

    def _boom(*_args, **_kwargs):
        # 月の一覧を得た後にファイルが差し替わった状況を再現する
        raise ValueError("spend: 2026-06 のCSVが複数あり期間から優先順を判断できません")

    monkeypatch.setattr("seat_analyzer.ingest.spend_file_period", _boom)
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)   # traceback で落ちず JSON が出る
    assert ("error", "MISSING_SPEND") in [(i["severity"], i["code"]) for i in issues]
    assert any("再確認できません" in i["message"] for i in issues)


def test_doctor_reports_vanished_history_month_as_issue(make_input, monkeypatch, capsys):
    input_dir = _clean_org(make_input)
    original = ingest.spend_file_period

    def _vanished(directory, month):
        # 対象月は正常、過去月だけ引き当てられない（検査中に消えた）状況を再現する
        return None if month == "2026-05" else original(directory, month)

    monkeypatch.setattr("seat_analyzer.ingest.spend_file_period", _vanished)
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    errors = [i for i in json.loads(capsys.readouterr().out) if i["severity"] == "error"]
    assert [(i["code"], i["scope"]["month"]) for i in errors] == [
        ("MISSING_SPEND", "2026-05"),
    ]


def test_init_org_creates_scaffold(tmp_path):
    input_dir, output_dir = tmp_path / "input", tmp_path / "reports"
    rc = main([
        "init-org", "org-x", "org-y",
        "--input-dir", str(input_dir), "--output-dir", str(output_dir),
    ])
    assert rc == 0
    for org in ("org-x", "org-y"):
        for sub in ("spend", "members", "code-analytics"):
            assert (input_dir / org / sub).is_dir()
        assert (output_dir / org).is_dir()
        # members-info.csv はヘッダ行のみの雛形が作られる。人が Excel で開くファイルなので
        # BOM 付き（BOM 無しだと Windows の Excel が日本語ヘッダを化けさせる）
        info = input_dir / org / "members-info.csv"
        assert info.read_bytes().startswith(b"\xef\xbb\xbf")
        assert info.read_text(encoding="utf-8-sig") == (
            "email,部署,チーム,職種,追加クレジット上限,備考,GitHub ID\n"
        )
    assert discover_orgs(input_dir) == ["org-x", "org-y"]


def test_init_org_points_out_flat_layout_data(tmp_path, capsys):
    # 旧レイアウトからの移行の入口。analyze が拒否するデータの置き場を雛形作成時に知らせる
    input_dir = tmp_path / "input"
    (input_dir / "spend").mkdir(parents=True)
    assert main(["init-org", "org-x", "--input-dir", str(input_dir),
                 "--output-dir", str(tmp_path / "reports")]) == 0
    out = capsys.readouterr().out
    assert "旧レイアウト" in out and "<組織名>" in out
    for item in ("spend/", "members/", "code-analytics/", "members-info"):
        assert item in out
    assert "docs/setup.md" in out


def test_init_org_rejects_org_named_spend(tmp_path, capsys):
    # 作れてしまうと、雛形が旧レイアウトの目印と重なり分析できないワークスペースになる。
    # 大文字小文字を無視して拒否する（既定の Windows / macOS では同じディレクトリになる）
    input_dir, output_dir = tmp_path / "input", tmp_path / "reports"
    for bad in ("spend", "Spend"):
        rc = main([
            "init-org", bad, "--input-dir", str(input_dir), "--output-dir", str(output_dir),
        ])
        assert rc == 1
        assert "予約" in capsys.readouterr().err
    # 1つでも不正なら1つも作らない（正当な名前と併記した場合も含む）
    assert main([
        "init-org", "org-x", "spend",
        "--input-dir", str(input_dir), "--output-dir", str(output_dir),
    ]) == 1
    assert not input_dir.exists()
    # 正当な組織名は従来どおり作れる
    assert main([
        "init-org", "org-x", "--input-dir", str(input_dir), "--output-dir", str(output_dir),
    ]) == 0
    assert (input_dir / "org-x" / "spend").is_dir()


def test_init_org_does_not_overwrite_filled_members_info(tmp_path):
    input_dir, output_dir = tmp_path / "input", tmp_path / "reports"
    args = ["init-org", "org-x", "--input-dir", str(input_dir), "--output-dir", str(output_dir)]
    assert main(args) == 0
    # ユーザが記入した状態を再 init-org しても上書きしない
    info = input_dir / "org-x" / "members-info.csv"
    info.write_text("email,部署,チーム,職種,備考\na@x.jp,開発,基盤,エンジニア,\n", encoding="utf-8")
    assert main(args) == 0
    assert "a@x.jp" in info.read_text(encoding="utf-8")


def test_init_org_rejects_reserved_and_invalid_names(tmp_path, capsys):
    # summary=予約 / a/b=パス区切り / .hidden=先頭ドット / org|x=Markdown を壊す文字
    # NUL=Windows のデバイス名 / org.=Windows が末尾のドットを落とす / a:b=NTFS で不可
    for bad, fragment in (
        ("summary", "予約"),
        ("a/b", "使えない文字"),
        (".hidden", "不正"),
        ("org|x", "使えない文字"),
        ("NUL", "予約デバイス名"),
        ("org.", "末尾のドット"),
        ("a:b", "使えない文字"),
    ):
        rc = main([
            "init-org", bad,
            "--input-dir", str(tmp_path / "input"), "--output-dir", str(tmp_path / "reports"),
        ])
        assert rc == 1
        assert fragment in capsys.readouterr().err
    assert not (tmp_path / "input").exists()


# --- doctor の GitHub 検査（config で有効にした組織のみ） ---
#
# gh は差し替えて一度も実行しない。ここで見るのは結線（どの組織を対象にするか・
# gh を何回呼ぶか・出力のどこへ出すか）で、issue の中身は tests/test_data_quality.py。


def _stub_gh(monkeypatch, *, authenticated: bool = True) -> list[tuple[str, ...]]:
    """gh の呼び出しを記録して固定の応答を返す（実際の gh は呼ばない）。"""
    from seat_analyzer.github_collect import GhResult

    calls: list[tuple[str, ...]] = []
    rate = json.dumps({"resources": {
        "core": {"limit": 5000, "remaining": 4999},
        "graphql": {"limit": 5000, "remaining": 5000},
    }})

    def _run(args):
        args = tuple(args)
        calls.append(args)
        if args[-1] == "status":
            return GhResult(ok=authenticated)
        if args[-1] == "rate_limit":
            return GhResult(ok=True, stdout=rate)
        return GhResult(
            ok=True, stdout="HTTP/2.0 200 OK\nX-Oauth-Scopes: read:org, repo\n\n{}\n")

    monkeypatch.setattr("seat_analyzer.github_collect.run_gh", _run)
    return calls


def _doctor_with(config: str, input_dir: Path, *extra: str) -> int:
    return main(["doctor", "--config", config, "--input-dir", str(input_dir), *extra])


def test_doctor_does_not_touch_gh_without_the_config(make_input, monkeypatch, capsys):
    """github_org を設定していない組織は GitHub の処理と警告から一切除外される。"""
    input_dir = _clean_org(make_input)
    calls = _stub_gh(monkeypatch)

    assert _doctor(input_dir, "--month", "2026-06") == 0

    assert calls == []
    assert "GITHUB" not in capsys.readouterr().out


def test_doctor_checks_github_for_a_configured_org(make_input, tmp_path, monkeypatch, capsys):
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    calls = _stub_gh(monkeypatch)

    # 対応表が無いだけなので warning（exit 0）
    assert _doctor_with(config, input_dir, "--month", "2026-06") == 0

    out = capsys.readouterr().out
    assert "[warning] GITHUB_MAPPING_MISSING" in out
    assert [call[-1] for call in calls] == [
        "status", "user", "rate_limit", f"orgs/{GH_ORG}"]


def test_doctor_reports_nothing_when_the_mapping_is_complete(
    make_input, tmp_path, monkeypatch, capsys
):
    input_dir = _clean_org(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,octo-example",))
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_gh(monkeypatch)

    assert _doctor_with(config, input_dir, "--month", "2026-06") == 0
    assert "問題は見つかりませんでした" in capsys.readouterr().out


def test_doctor_github_error_sets_the_exit_code(make_input, tmp_path, monkeypatch, capsys):
    input_dir = _clean_org(make_input)
    _mapping(input_dir, "org-a", ("a@x.jp,octo-example",))
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_gh(monkeypatch, authenticated=False)

    assert _doctor_with(config, input_dir, "--month", "2026-06") == 1
    assert "[error] GH_NOT_AUTHENTICATED" in capsys.readouterr().out


def test_doctor_probes_gh_once_for_several_orgs(make_input, tmp_path, monkeypatch, capsys):
    """認証・scope・利用上限は組織数ぶん叩かず、1回の結果を使い回す。"""
    input_dir = _clean_org(make_input)
    make_input(
        {"2026-05": [spend_row("b@y.jp", 10.0)], "2026-06": [spend_row("b@y.jp", 12.0)]},
        members=["b@y.jp,Premium"], org="org-b",
    )
    config = _gh_config(tmp_path, **{"org-a": GH_ORG, "org-b": "another-example"})
    calls = _stub_gh(monkeypatch, authenticated=False)

    assert _doctor_with(config, input_dir, "--month", "2026-06") == 1

    assert [call[-1] for call in calls] == ["status"]
    # 認証の失敗は、有効にした各組織の issue として出る（組織別に読んで完結する）
    issues = [line for line in capsys.readouterr().out.splitlines()
              if "GH_NOT_AUTHENTICATED" in line]
    assert len(issues) == 2


def test_doctor_limits_github_checks_to_the_selected_orgs(
    make_input, tmp_path, monkeypatch, capsys
):
    input_dir = _clean_org(make_input)
    make_input(
        {"2026-05": [spend_row("b@y.jp", 10.0)], "2026-06": [spend_row("b@y.jp", 12.0)]},
        members=["b@y.jp,Premium"], org="org-b",
    )
    config = _gh_config(tmp_path, **{"org-b": GH_ORG})
    calls = _stub_gh(monkeypatch)

    assert _doctor_with(config, input_dir, "--month", "2026-06", "--org", "org-a") == 0

    assert calls == []
    assert "GITHUB" not in capsys.readouterr().out


def test_doctor_warns_about_a_config_key_without_an_org(
    make_input, tmp_path, monkeypatch, capsys
):
    """綴り違いで GitHub の検査が黙って全部飛ぶ状態を、独立したセクションで知らせる。"""
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-typo": GH_ORG})
    calls = _stub_gh(monkeypatch)

    assert _doctor_with(config, input_dir, "--month", "2026-06") == 0

    out = capsys.readouterr().out
    assert calls == []
    assert "=== 設定検査 ===" in out
    assert "[warning] GITHUB_CONFIG_UNMATCHED" in out
    assert "org-typo" in out
    # 組織別のセクションの後に出す
    assert out.index("=== org-a") < out.index("=== 設定検査 ===")


def test_doctor_config_warning_uses_all_orgs_not_the_selection(
    make_input, tmp_path, monkeypatch, capsys
):
    """--org で選ばなかった組織を「一致しない」と言わない。"""
    input_dir = _clean_org(make_input)
    make_input(
        {"2026-06": [spend_row("b@y.jp", 12.0)]}, members=["b@y.jp,Premium"], org="org-b")
    config = _gh_config(tmp_path, **{"org-b": GH_ORG})
    _stub_gh(monkeypatch)

    assert _doctor_with(config, input_dir, "--month", "2026-06", "--org", "org-a") == 0
    assert "GITHUB_CONFIG_UNMATCHED" not in capsys.readouterr().out


def test_doctor_config_warning_is_in_the_json_output(
    make_input, tmp_path, monkeypatch, capsys
):
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-typo": GH_ORG})
    _stub_gh(monkeypatch)

    assert _doctor_with(config, input_dir, "--month", "2026-06", "--format", "json") == 0

    issues = json.loads(capsys.readouterr().out)
    assert [i["code"] for i in issues] == ["GITHUB_CONFIG_UNMATCHED"]
    assert issues[0]["scope"] == {"config_org": "org-typo", "known_orgs": ["org-a"]}


def test_doctor_does_not_blame_the_config_when_the_input_is_unreadable(
    tmp_path, monkeypatch, capsys
):
    """入力を読めていないだけの状態で、設定側の綴りを疑わせない。"""
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_gh(monkeypatch)

    assert _doctor_with(config, tmp_path / "nope", "--format", "json") == 1
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == ["MISSING_SPEND"]


# --- collect --source github（PR メタデータの収集） ---
#
# gh は差し替えて一度も実行しない。ここで見るのは結線（opt-in の判定・キャッシュの置き場所・
# 表示と終了コード）で、収集そのものは tests/test_github_collect.py。

COLLECT_MONTH = "2026-08"

# 対象月の全窓が完了する日（月末 + 2日）。収集は「今日」を見るため固定する
COLLECT_TODAY = dt.date(2026, 9, 2)


def _repo(name: str, archived: bool = False) -> dict:
    """repository 一覧の1要素（発見が読む項目だけを持つ）。"""
    return {"name": name, "archived": archived, "fork": False, "is_template": False}


def _graphql(call: tuple[str, ...]) -> bool:
    """その呼び出しが PR 検索（GraphQL）か。repository の発見は REST。"""
    return call[:3] == ("api", "-i", "graphql")


def _stub_search(monkeypatch, response=None, today: dt.date = COLLECT_TODAY,
                 repos: list[dict] | None = None,
                 listing_status: int = 200) -> list[tuple[str, ...]]:
    """repository の発見と PR 検索の応答、そして「今日」を差し替える。

    既定は repository 1件の一覧と 0 件の検索結果。response は PR 検索にだけ適用する
    （発見が先に走るので両方へ適用すると検索の分岐に届かない）。発見そのものを失敗
    させる場合は listing_status を 200 以外にする。

    今日を固定するのは、窓の完了判定が実行日で変わらないようにするため。
    """
    from seat_analyzer.github_collect import GhResult

    payload = json.dumps({"data": {"search": {
        "issueCount": 0,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    }}})
    normal = GhResult(ok=True, stdout=f"HTTP/2.0 200 OK\n\n{payload}\n")
    listing = json.dumps([_repo("repo-a")] if repos is None else repos)
    found = GhResult(
        ok=200 <= listing_status < 300,
        stdout=f"HTTP/2.0 {listing_status} -\n\n{listing}\n",
    )
    calls: list[tuple[str, ...]] = []

    def _run(args):
        args = tuple(args)
        calls.append(args)
        if not _graphql(args):
            return found
        return normal if response is None else response

    monkeypatch.setattr("seat_analyzer.github_collect.run_gh", _run)
    monkeypatch.setattr("seat_analyzer.github_collect._today", lambda: today)
    return calls


def _collect(config: str, input_dir: Path, *extra: str) -> int:
    return main([
        "collect", "--source", "github", "--config", config,
        "--input-dir", str(input_dir), "--month", COLLECT_MONTH, *extra,
    ])


def _cache_path(input_dir: Path, org: str = "org-a") -> Path:
    return input_dir / org / "github-cache" / f"prs-{COLLECT_MONTH}.json"


def test_collect_requires_the_github_opt_in(make_input, tmp_path, monkeypatch, capsys):
    """github_org を設定していない組織では収集しない（gh を呼ばない）。"""
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-b": GH_ORG})
    calls = _stub_search(monkeypatch)

    assert _collect(config, input_dir, "--org", "org-a") == 1

    err = capsys.readouterr().err
    assert "組織 org-a は GitHub 分析が有効ではありません" in err
    assert "organizations.org-a.github_org" in err
    assert calls == []
    assert not _cache_path(input_dir).exists()


def test_collect_writes_the_cache_and_prints_one_line(
    make_input, tmp_path, monkeypatch, capsys
):
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    calls = _stub_search(monkeypatch)

    assert _collect(config, input_dir, "--org", "org-a") == 0

    out = capsys.readouterr().out
    path = _cache_path(input_dir)
    assert out.splitlines() == [
        f"org-a {COLLECT_MONTH}: merged PR 0 件（今回 0 件を更新）→ {path}",
        "  対象 repository 1 件（archived / fork / template を除外 0 件）",
    ]
    assert json.loads(path.read_text(encoding="utf-8"))["github_org"] == GH_ORG
    # 窓の数だけ検索する（対象月は5窓）。その前に repository の一覧を1回読む
    assert len([call for call in calls if _graphql(call)]) == 5
    assert len(calls) == 6


def test_collect_notes_the_window_it_will_refetch(
    make_input, tmp_path, monkeypatch, capsys
):
    """対象月が終わっていない期間は次回も取り直すことを伝える。"""
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_search(monkeypatch, today=dt.date(2026, 8, 10))

    assert _collect(config, input_dir, "--org", "org-a") == 0
    assert "次回の実行で再取得します" in capsys.readouterr().out


def test_collect_reports_an_interrupted_collection(
    make_input, tmp_path, monkeypatch, capsys
):
    from seat_analyzer.github_collect import GhResult

    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_search(monkeypatch, response=GhResult(
        ok=False, stdout="HTTP/2.0 429 Too Many Requests\nRetry-After: 60\n\n{}\n"))

    assert _collect(config, input_dir, "--org", "org-a") == 1

    captured = capsys.readouterr()
    assert "merged PR 0 件" in captured.out
    assert "収集を中断しました: 2026-08-01〜2026-08-07 で" in captured.err
    assert "GitHub API の利用上限に達しました" in captured.err
    assert "再実行すると続きから収集します" in captured.err


def test_collect_names_the_status_of_an_unreadable_response(
    make_input, tmp_path, monkeypatch, capsys
):
    from seat_analyzer.github_collect import GhResult

    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_search(monkeypatch, response=GhResult(
        ok=False, stdout="HTTP/2.0 500 Internal Server Error\n\n{}\n"))

    assert _collect(config, input_dir, "--org", "org-a") == 1

    err = capsys.readouterr().err
    assert "GitHub API の応答を解釈できませんでした（HTTP 500）" in err


def test_collect_does_not_show_the_raw_gh_output(
    make_input, tmp_path, monkeypatch, capsys
):
    """gh の生出力・token・ヘッダの値は表示しない。"""
    from seat_analyzer.github_collect import GhResult

    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_search(monkeypatch, response=GhResult(
        ok=False,
        stdout=("HTTP/2.0 403 Forbidden\n"
                "X-GitHub-SSO: required; url=https://example.invalid\n"
                "\n"
                '{"message": "gh の診断の文言"}\n'),
    ))

    assert _collect(config, input_dir, "--org", "org-a") == 1

    captured = capsys.readouterr()
    assert "診断" not in captured.out + captured.err
    assert "example.invalid" not in captured.out + captured.err


def test_collect_asks_for_a_login_when_gh_is_not_authenticated(
    make_input, tmp_path, monkeypatch, capsys
):
    """gh は動くが未ログイン（終了コード 4）のとき、認証の案内まで届く。

    ここだけ subprocess を差し替えるのは、終了コードの分類から表示までを通すため。
    未ログインは最初の呼び出し（repository の発見）で分かるので、そこで止まる。
    """
    from seat_analyzer.github_collect import GH_EXIT_AUTH_REQUIRED

    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    monkeypatch.setattr("seat_analyzer.github_collect.shutil.which", lambda _: "gh")
    monkeypatch.setattr(
        "seat_analyzer.github_collect.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=GH_EXIT_AUTH_REQUIRED, stdout=b""),
    )
    monkeypatch.setattr("seat_analyzer.github_collect._today", lambda: COLLECT_TODAY)

    assert _collect(config, input_dir, "--org", "org-a") == 1

    err = capsys.readouterr().err
    assert "gh の認証がありません（gh auth login を実行してください）" in err
    assert "repository の一覧を取得できませんでした" in err
    assert not _cache_path(input_dir).exists()


def test_collect_saves_the_repository_listing(
    make_input, tmp_path, monkeypatch, capsys
):
    """収集は PR と一緒に repository の一覧も保存し、対象と除外の件数を出す。"""
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_search(monkeypatch, repos=[_repo("repo-a"), _repo("old", archived=True)])

    assert _collect(config, input_dir, "--org", "org-a") == 0

    assert "対象 repository 1 件（archived / fork / template を除外 1 件）" \
        in capsys.readouterr().out
    payload = json.loads(_cache_path(input_dir).read_text(encoding="utf-8"))
    assert payload["repositories"] == {"names": ["repo-a"], "excluded": 1}


def test_collect_stops_when_the_repository_listing_fails(
    make_input, tmp_path, monkeypatch, capsys
):
    """一覧を得られなければ PR の検索へ進まず、キャッシュも書かない。

    部分的な一覧で集計すると、一覧に載らなかった repository の PR が「対象外」へ
    流れて参考指標が黙って小さく出る。
    """
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    calls = _stub_search(monkeypatch, listing_status=403)

    assert _collect(config, input_dir, "--org", "org-a") == 1

    err = capsys.readouterr().err
    assert "repository の一覧を取得できませんでした（HTTP 403）" in err
    assert not _cache_path(input_dir).exists()
    assert not any(_graphql(call) for call in calls)


def test_collect_rejects_an_unknown_org(make_input, tmp_path, monkeypatch, capsys):
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    _stub_search(monkeypatch)

    assert _collect(config, input_dir, "--org", "org-x") == 1
    assert "組織が見つかりません" in capsys.readouterr().err


@pytest.mark.parametrize("month", ["2026-13", "202608", "2026-8", "../2026-08"])
def test_collect_rejects_a_bad_month(make_input, tmp_path, monkeypatch, capsys, month):
    input_dir = _clean_org(make_input)
    config = _gh_config(tmp_path, **{"org-a": GH_ORG})
    calls = _stub_search(monkeypatch)

    rc = main([
        "collect", "--source", "github", "--config", config,
        "--input-dir", str(input_dir), "--org", "org-a", "--month", month,
    ])

    assert rc == 1
    assert "対象月の形式が不正です" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("args", [
    ["collect", "--org", "org-a", "--month", COLLECT_MONTH],            # --source なし
    ["collect", "--source", "browser", "--org", "org-a",
     "--month", COLLECT_MONTH],                                        # 未対応の収集元
    ["collect", "--source", "github", "--month", COLLECT_MONTH],        # --org なし
    ["collect", "--source", "github", "--org", "org-a"],                # --month なし
    ["collect", "--source", "github", "--org", "org-a", "--org", "org-b",
     "--month", COLLECT_MONTH],                                        # github は1組織ずつ
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--dry-run"],                           # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--profile", "corp"],                   # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--keep-browser"],                      # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--timeout", "5"],                      # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--import", "corp-current-20261007-110009"],  # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--setup", "corp"],                     # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--finish-setup", "corp"],              # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--login", "corp"],                     # claude 専用
    ["collect", "--source", "github", "--org", "org-a",
     "--month", COLLECT_MONTH, "--list-orgs", "corp"],                 # claude 専用
    ["collect", "--source", "claude", "--timeout", "0"],               # 1 分以上
    ["collect", "--source", "claude", "--timeout", "x"],               # 整数
    # --setup・--finish-setup・--login・--list-orgs・--import は単独で使う
    ["collect", "--source", "claude", "--setup", "corp", "--login", "corp"],
    ["collect", "--source", "claude", "--setup", "corp", "--org", "org-a"],
    ["collect", "--source", "claude", "--setup", "corp", "--month", "2026-10"],
    ["collect", "--source", "claude", "--setup", "corp", "--dry-run"],
    ["collect", "--source", "claude", "--setup", "corp", "--keep-browser"],
    ["collect", "--source", "claude", "--setup", "corp", "--timeout", "5"],
    ["collect", "--source", "claude", "--setup", "corp", "--profile", "corp"],
    ["collect", "--source", "claude", "--setup", "corp", "--finish-setup", "corp"],
    ["collect", "--source", "claude", "--finish-setup", "corp", "--org", "org-a"],
    ["collect", "--source", "claude", "--finish-setup", "corp", "--dry-run"],
    ["collect", "--source", "claude", "--finish-setup", "corp", "--keep-browser"],
    ["collect", "--source", "claude", "--finish-setup", "corp", "--timeout", "5"],
    ["collect", "--source", "claude", "--finish-setup", "corp", "--import", "x"],
    ["collect", "--source", "claude", "--login", "corp", "--keep-browser"],
    ["collect", "--source", "claude", "--list-orgs", "corp", "--org", "org-a"],
    ["collect", "--source", "claude", "--list-orgs", "corp", "--dry-run"],
    ["collect", "--source", "claude", "--list-orgs", "corp", "--import", "x"],
    ["collect", "--source", "claude", "--import", "x", "--dry-run"],
    ["collect", "--source", "claude", "--import", "x", "--org", "org-a"],
    ["collect", "--source", "claude", "--import", "x", "--month", "2026-10"],
    ["collect", "--source", "claude", "--import", "x", "--keep-browser"],
])
def test_collect_requires_its_options(args):
    """必須オプションと収集元の選択肢は argparse が弾く（収集元ごとの組み合わせも同じ扱い）。"""
    with pytest.raises(SystemExit) as excinfo:
        main(args)
    assert excinfo.value.code == 2


def test_collect_failure_text_covers_every_failure():
    """中断の理由はどの GhFailure でも表示の文言を持つ（語彙が増えたとき引き当てで落ちない）。"""
    from seat_analyzer.cli import _COLLECT_FAILURE_TEXT
    from seat_analyzer.github_collect import GhFailure

    assert set(_COLLECT_FAILURE_TEXT) == set(GhFailure)


# --- collect --source claude --dry-run（claude.ai の CSV 取得の計画） ---
#
# ブラウザは起動しない。ここで見るのは opt-in の判定・絞り込み・計画の表示と終了コードで、
# 計画そのものは tests/test_claude_export.py。

CLAUDE_UUID1 = "00000000-0000-4000-8000-000000000001"
CLAUDE_UUID2 = "00000000-0000-4000-8000-000000000002"
CLAUDE_UUID3 = "00000000-0000-4000-8000-000000000003"
CLAUDE_UUID4 = "00000000-0000-4000-8000-000000000004"

# 当月 2026-10・前月 2026-09 になる「今日」
CLAUDE_TODAY = dt.date(2026, 10, 7)


def _claude_config(tmp_path: Path, *, organizations: str | None = None,
                   chrome: Path | None = None) -> str:
    """Chrome・プロファイル・staging を tmp_path に向けた上書き設定を作り、そのパスを返す。

    organizations を省くと、単一スペースの example（corp）・org-a（group）と、
    入れ子レイアウトの example2（main・second とも corp）を書く。
    """
    if organizations is None:
        organizations = (
            "organizations:\n"
            "  example:\n"
            "    claude_export:\n"
            "      profile: corp\n"
            f"      org_id: {CLAUDE_UUID1}\n"
            "  org-a:\n"
            "    claude_export:\n"
            "      profile: group\n"
            f"      org_id: {CLAUDE_UUID4}\n"
            "      kinds: [spend]\n"
            "  example2:\n"
            "    workspaces:\n"
            "      main:\n"
            "        primary: true\n"
            "        claude_export:\n"
            "          profile: corp\n"
            f"          org_id: {CLAUDE_UUID2}\n"
            "      second:\n"
            "        claude_export:\n"
            "          profile: corp\n"
            f"          org_id: {CLAUDE_UUID3}\n"
        )
    chrome = tmp_path / "chrome-bin" if chrome is None else chrome
    path = tmp_path / "claude-config.yaml"
    path.write_text(
        organizations
        + "claude_export:\n"
        f"  chrome_path: {json.dumps(str(chrome))}\n"
        f"  profiles_dir: {json.dumps(str(tmp_path / 'profiles'))}\n"
        f"  staging_dir: {json.dumps(str(tmp_path / 'exports'))}\n",
        encoding="utf-8", newline="\n",
    )
    return str(path)


def _claude_input(tmp_path: Path) -> Path:
    """example・org-a・example2/main の組織ディレクトリだけを作る（second は未作成）。"""
    input_dir = tmp_path / "input"
    for rel in ("example", "org-a", "example2/main"):
        (input_dir / rel).mkdir(parents=True)
    return input_dir


def _dry_run(config: str, input_dir: Path, monkeypatch, *extra: str) -> int:
    monkeypatch.setattr(
        "seat_analyzer.claude_export.local_today", lambda: CLAUDE_TODAY)
    return main([
        "collect", "--source", "claude", "--dry-run", "--config", config,
        "--input-dir", str(input_dir), *extra,
    ])


def test_collect_claude_dry_run_prints_the_plan(tmp_path, monkeypatch, capsys):
    chrome = tmp_path / "chrome-bin"
    chrome.write_bytes(b"")
    config = _claude_config(tmp_path, chrome=chrome)
    input_dir = _claude_input(tmp_path)
    (tmp_path / "profiles" / "group").mkdir(parents=True)

    assert _dry_run(config, input_dir, monkeypatch, "--month", "2026-09") == 0

    sep = os.sep
    all_dirs = "{members,spend,code-analytics}"
    missing = "  組織ディレクトリがありません（init-org で作成）"
    assert capsys.readouterr().out.splitlines() == [
        f"Chrome: {chrome}",
        f"staging: {tmp_path / 'exports'}",
        (f"profile corp: 前月モード（2026-09）  プロファイル {tmp_path / 'profiles' / 'corp'}"
         "（未作成。--setup corp が必要）"),
        (f"  example          {CLAUDE_UUID1}  members, spend, code"
         f"  → {input_dir / 'example'}{sep}{all_dirs}{sep}"),
        (f"  example2/main    {CLAUDE_UUID2}  members, spend, code"
         f"  → {input_dir / 'example2' / 'main'}{sep}{all_dirs}{sep}"),
        (f"  example2/second  {CLAUDE_UUID3}  members, spend, code"
         f"  → {input_dir / 'example2' / 'second'}{sep}{all_dirs}{sep}{missing}"),
        f"profile group: 前月モード（2026-09）  プロファイル {tmp_path / 'profiles' / 'group'}",
        f"  org-a            {CLAUDE_UUID4}  spend  → {input_dir / 'org-a'}{sep}spend{sep}",
    ]
    # 計画を表示するだけで、何も作らない
    assert not (tmp_path / "exports").exists()
    assert not (tmp_path / "profiles" / "corp").exists()
    assert not (input_dir / "example" / "spend").exists()


def test_collect_claude_dry_run_defaults_to_the_current_month(tmp_path, monkeypatch, capsys):
    config = _claude_config(tmp_path)
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch) == 0
    assert "profile corp: 当月モード（2026-10）" in capsys.readouterr().out


def test_collect_claude_dry_run_narrows_by_org_and_profile(tmp_path, monkeypatch, capsys):
    config = _claude_config(tmp_path)
    input_dir = _claude_input(tmp_path)

    assert _dry_run(config, input_dir, monkeypatch, "--org", "example2",
                    "--org", "org-a") == 0
    out = capsys.readouterr().out
    assert "example2/main" in out and "example2/second" in out and "org-a" in out
    assert "  example  " not in out and CLAUDE_UUID1 not in out

    assert _dry_run(config, input_dir, monkeypatch, "--profile", "group") == 0
    out = capsys.readouterr().out
    assert "profile group:" in out and "profile corp:" not in out


def test_collect_claude_dry_run_names_a_missing_chrome(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "no-chrome"
    config = _claude_config(tmp_path, chrome=missing)
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        f"Chrome: {missing} が見つかりません（claude_export.chrome_path を確認してください）")


def test_collect_claude_requires_the_opt_in(tmp_path, monkeypatch, capsys):
    """claude_export を書いた組織が無ければ何もせず終了コード 1。"""
    config = _claude_config(tmp_path, organizations="")
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "claude_export を設定した組織がありません" in captured.err
    assert "organizations.<組織名>.claude_export" in captured.err


def test_collect_claude_rejects_an_org_without_the_opt_in(tmp_path, monkeypatch, capsys):
    config = _claude_config(tmp_path)
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch, "--org", "org-x") == 1
    assert "組織 org-x は claude_export が設定されていません" in capsys.readouterr().err


def test_collect_claude_rejects_an_unknown_profile(tmp_path, monkeypatch, capsys):
    config = _claude_config(tmp_path)
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch,
                    "--org", "org-a", "--profile", "corp") == 1
    assert "--profile corp を使う claude_export の対象がありません" in capsys.readouterr().err


def test_collect_claude_rejects_org_names_that_differ_only_in_case(
    tmp_path, monkeypatch, capsys
):
    """大文字小文字だけが違う組織名は計画の段階で止め、何も表示しない。"""
    config = _claude_config(tmp_path, organizations=(
        "organizations:\n"
        "  org-a:\n"
        "    claude_export:\n"
        "      profile: corp\n"
        f"      org_id: {CLAUDE_UUID1}\n"
        "  Org-A:\n"
        "    claude_export:\n"
        "      profile: corp\n"
        f"      org_id: {CLAUDE_UUID2}\n"
    ))
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "claude_export を設定した組織名が衝突しています" in captured.err


@pytest.mark.parametrize("month,fragment", [
    ("2026-08", "取得できるのは当月と前月だけです（当月 2026-10・前月 2026-09）"),
    ("2026-9", "対象月の形式が不正です"),
])
def test_collect_claude_rejects_a_month_it_cannot_fetch(
    tmp_path, monkeypatch, capsys, month, fragment
):
    config = _claude_config(tmp_path)
    assert _dry_run(config, _claude_input(tmp_path), monkeypatch, "--month", month) == 1
    assert fragment in capsys.readouterr().err


# --- collect --source claude（Chrome の起動 → manifest の待機 → 検証・配置 → 終了） ---
#
# 実際の Chrome は起動しない。起動・プロセスの列挙・終了の関数を差し替え、偽の拡張機能
# （_FakeExtension）が起動のコマンドから実行内容を読んで staging に結果を書く。待機の時計と
# sleep も差し替え、待ち時間を実時間に依らせない。

CLAUDE_PID = 4242
CLAUDE_HEADERS = {
    "members": "Email,Seat Tier,Status",
    "spend": "user_email,model,product,total_prompt_tokens,total_completion_tokens",
    "code": "User,Lines this month,PRs with CC",
}
CLAUDE_KIND_DIRS = {"members": "members", "spend": "spend", "code": "code-analytics"}


def _claude_filename(kind: str, uuid: str) -> str:
    """当月（2026-10・今日 2026-10-07）に取得したときの元のファイル名。"""
    return {
        "members": f"members-{uuid}-2026-10-07.csv",
        "spend": f"spend-report-{uuid}-2026-10-01-to-2026-10-06.csv",
        "code": f"claude-code-{uuid}-2026-10-01-to-2026-10-31.csv",
    }[kind]


class _FakeClock:
    """待機に使う時計。sleep で進み、そのたびに登録した処理を呼ぶ。"""

    def __init__(self):
        self.now = 0.0
        self.on_sleep = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in list(self.on_sleep):
            hook()


class _FakeExtension:
    """偽の拡張機能。起動のコマンドの URL から実行内容を読み、staging に結果を書く。

    outcomes は (dir, kind) → "ok"（正しい CSV）・{"ok": False, "reason": ...}（拡張機能の
    失敗）・{"header": ...}（ok だが中身の違う CSV）。manifest="later" なら最初の sleep で
    manifest を書き（起動時は progress だけ）、manifest=None なら書かない。
    """

    def __init__(self, staging: Path, clock: _FakeClock):
        self.staging = staging
        self.clock = clock
        self.commands: list[list[str]] = []
        self.specs: list[dict] = []
        self.outcomes: dict = {}
        self.manifest: str | None = "now"
        self.manifest_run_id: str | None = None
        self.orgs: object = None

    def launch(self, command, **kwargs) -> None:
        self.commands.append(list(command))
        url = command[-1]
        if not url.startswith(claude_export.TRIGGER_PREFIX):
            return
        spec = json.loads(urllib.parse.unquote(url[len(claude_export.TRIGGER_PREFIX):]))
        self.specs.append(spec)
        run_dir = self.staging / spec["run_id"]
        if spec.get("action") == "list-orgs":
            if self.orgs is not None:
                (run_dir / "orgs.json").write_text(json.dumps(self.orgs), encoding="utf-8")
            return
        results = [self._export(run_dir, org, kind) for org in spec["orgs"] for kind in org["kinds"]]
        body = {"run_id": self.manifest_run_id or spec["run_id"], "mode": spec["mode"],
                "finished_at": "2026-10-07T02:00:49.000Z", "results": results, "log": []}
        (run_dir / "progress.json").write_text(
            json.dumps({**body, "status": "running"}), encoding="utf-8")
        if self.manifest == "now":
            (run_dir / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
        elif self.manifest == "later":
            def finish():
                (run_dir / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
                self.clock.on_sleep.remove(finish)
            self.clock.on_sleep.append(finish)

    def _export(self, run_dir: Path, org: dict, kind: str) -> dict:
        outcome = self.outcomes.get((org["dir"], kind), "ok")
        if isinstance(outcome, dict) and outcome.get("ok") is False:
            return {"dir": org["dir"], "kind": kind, **outcome}
        header = outcome["header"] if isinstance(outcome, dict) else CLAUDE_HEADERS[kind]
        name = _claude_filename(kind, org["uuid"])
        target = run_dir.joinpath(*org["dir"].split("/"), CLAUDE_KIND_DIRS[kind], name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{header}\nuser1@example.com,x,y\n", encoding="utf-8")
        return {"dir": org["dir"], "kind": kind, "ok": True, "filename": name}


@pytest.fixture
def claude_env(tmp_path, monkeypatch):
    """合成の設定・入力・プロファイル（corp は設定済み）と、偽の Chrome・拡張機能。"""
    chrome = tmp_path / "chrome-bin"
    chrome.write_bytes(b"")
    config = _claude_config(tmp_path, chrome=chrome)
    input_dir = _claude_input(tmp_path)
    staging = tmp_path / "exports"
    profiles = tmp_path / "profiles"
    _write_preferences(profiles / "corp", staging)

    clock = _FakeClock()
    ext = _FakeExtension(staging, clock)
    env = SimpleNamespace(
        tmp_path=tmp_path, chrome=chrome, config=config, input_dir=input_dir,
        staging=staging, profiles=profiles, clock=clock, ext=ext,
        pids=[CLAUDE_PID], listed=[], terminated=[],
    )

    def list_pids(profile_dir, **kwargs):
        env.listed.append(Path(profile_dir))
        return list(env.pids) if not callable(env.pids) else env.pids()

    monkeypatch.setattr("seat_analyzer.claude_export.local_today", lambda: CLAUDE_TODAY)
    monkeypatch.setattr("seat_analyzer.claude_export.launch_chrome", ext.launch)
    monkeypatch.setattr("seat_analyzer.claude_export.list_chrome_pids", list_pids)
    monkeypatch.setattr("seat_analyzer.claude_export.terminate_chrome",
                        lambda pids, **kwargs: env.terminated.append(list(pids)))
    monkeypatch.setattr("seat_analyzer.cli._monotonic", clock.monotonic)
    monkeypatch.setattr("seat_analyzer.cli._sleep", clock.sleep)
    return env


def _write_preferences(profile_dir: Path, staging: Path) -> None:
    """--setup を済ませたプロファイルの Preferences（ダウンロード先が staging）。"""
    prefs = profile_dir / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(
        json.dumps({"download": {"default_directory": str(staging)}, "other": 1}),
        encoding="utf-8",
    )


def _collect_claude(env, *extra: str) -> int:
    return main([
        "collect", "--source", "claude", "--config", env.config,
        "--input-dir", str(env.input_dir), *extra,
    ])


def _placed(env, rel: str, kind: str) -> Path:
    uuid = {"example": CLAUDE_UUID1, "example2/main": CLAUDE_UUID2,
            "example2/second": CLAUDE_UUID3, "org-a": CLAUDE_UUID4}[rel]
    return env.input_dir.joinpath(*rel.split("/"), CLAUDE_KIND_DIRS[kind],
                                  _claude_filename(kind, uuid))


def test_collect_claude_places_every_file_and_closes_chrome(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--org", "example") == 0

    # 起動: 専用プロファイルの Chrome にトリガー URL を渡す
    [command] = env.ext.commands
    [spec] = env.ext.specs
    run_id = spec["run_id"]
    assert command[:2] == [str(env.chrome), f"--user-data-dir={env.profiles / 'corp'}"]
    assert command[-1].startswith(claude_export.TRIGGER_PREFIX)
    assert re.fullmatch(r"corp-current-\d{8}-\d{6}", run_id)
    assert spec == {"run_id": run_id, "mode": "current", "orgs": [
        {"uuid": CLAUDE_UUID1, "dir": "example", "kinds": ["members", "spend", "code"]}]}

    # run.json: --import が計画を組み直すための記録
    record = json.loads((env.staging / run_id / "run.json").read_text(encoding="utf-8"))
    assert {key: record[key] for key in ("run_id", "profile", "mode", "month", "spec")} == {
        "run_id": run_id, "profile": "corp", "mode": "current", "month": "2026-10",
        "spec": spec}
    assert dt.datetime.fromisoformat(record["created_at"]).tzinfo is not None

    # 配置: 元のファイル名のまま入力の種別ディレクトリへ
    for kind in ("members", "spend", "code"):
        assert _placed(env, "example", kind).read_text(encoding="utf-8").startswith(
            CLAUDE_HEADERS[kind])
    # 終了: そのプロファイルの Chrome だけを終了させる
    assert env.listed == [env.profiles / "corp"]
    assert env.terminated == [[CLAUDE_PID]]

    out = capsys.readouterr().out
    assert "profile corp: 当月モード（2026-10）" in out
    assert f"  配置: {_placed(env, 'example', 'spend')}" in out.splitlines()
    assert "  配置 3 件・失敗 0 件" in out
    assert "Chrome を終了しました" in out


def test_collect_claude_reports_each_failure_and_places_the_rest(claude_env, capsys):
    env = claude_env
    env.ext.outcomes = {
        ("example", "spend"): {"ok": False, "reason": "spend report unavailable"},
        # 支出レポートのヘッダの CSV を Claude Code analytics として受け取った
        ("example", "code"): {"header": CLAUDE_HEADERS["spend"]},
    }
    # example2/second の組織ディレクトリは無い（配置先を作らない）
    assert _collect_claude(env, "--profile", "corp") == 1

    run_id = env.ext.specs[0]["run_id"]
    run_dir = env.staging / run_id
    staged_code = run_dir / "example" / "code-analytics" / _claude_filename("code", CLAUDE_UUID1)
    staged_second = (run_dir / "example2" / "second" / "members"
                     / _claude_filename("members", CLAUDE_UUID3))
    lines = capsys.readouterr().out.splitlines()
    assert f"  失敗: example spend spend report unavailable（{run_dir}）" in lines
    [code_line] = [line for line in lines if line.startswith("  失敗: example code ")]
    assert "Claude Code analyticsのヘッダに loc_with_cc に当たる列がありません" in code_line
    assert code_line.endswith(f"（{staged_code}）")
    [second_line] = [line for line in lines
                     if line.startswith("  失敗: example2/second members ")]
    assert "配置先のディレクトリがありません" in second_line
    assert "  配置 4 件・失敗 5 件" in lines

    # 通ったものは配置し、落ちたものは配置せず staging に残す
    assert _placed(env, "example", "members").is_file()
    assert _placed(env, "example2/main", "code").is_file()
    assert not _placed(env, "example", "spend").exists()
    assert not _placed(env, "example", "code").exists()
    assert staged_code.is_file() and staged_second.is_file()
    assert not (env.input_dir / "example2" / "second").exists()
    # 失敗があっても取得は終わっているので Chrome は終了させる
    assert env.terminated == [[CLAUDE_PID]]


def test_collect_claude_runs_each_profile_in_turn(claude_env, capsys):
    """プロファイルごとに順に起動し、設定の済んでいないプロファイルはその実行だけ失敗にする。"""
    env = claude_env
    assert _collect_claude(env) == 1
    # corp だけが起動し、group は Preferences が無いので起動しない
    assert [spec["run_id"].split("-")[0] for spec in env.ext.specs] == ["corp"]
    captured = capsys.readouterr()
    assert "profile group: 当月モード（2026-10）" in captured.out
    assert ("プロファイル group の設定が済んでいません。先に collect --source claude "
            "--setup group を実行し、ブラウザの操作の後に --finish-setup group を"
            "実行してください") in captured.err


def test_collect_claude_shows_progress_while_waiting(claude_env, capsys):
    env = claude_env
    env.ext.manifest = "later"
    env.ext.outcomes = {("example", "spend"): {"ok": False, "reason": "spend report unavailable"}}
    assert _collect_claude(env, "--org", "example") == 1

    lines = capsys.readouterr().out.splitlines()
    progress = ["  example members: ok", "  example spend: 失敗 spend report unavailable",
                "  example code: ok"]
    assert [line for line in lines if line in progress] == progress
    # 途中経過は配置の前に出る
    assert lines.index(progress[-1]) < next(
        index for index, line in enumerate(lines) if line.startswith("  配置: "))


def test_collect_claude_rejects_a_manifest_of_another_run(claude_env, capsys):
    env = claude_env
    env.ext.manifest_run_id = "corp-current-20000101-000000"
    assert _collect_claude(env, "--org", "example") == 1
    err = capsys.readouterr().err
    assert "manifest の run_id（corp-current-20000101-000000）がこの実行" in err
    assert not (env.input_dir / "example" / "members").exists()


def test_collect_claude_times_out_and_leaves_chrome_open(claude_env, capsys):
    env = claude_env
    env.ext.manifest = None
    assert _collect_claude(env, "--org", "example", "--timeout", "2") == 1

    run_id = env.ext.specs[0]["run_id"]
    captured = capsys.readouterr()
    assert "待機中（1 分経過）: ブラウザに「要操作」の表示が出ていないか確認してください" \
        in captured.out
    assert "2 分待っても取得が終わりませんでした" in captured.err
    assert f"collect --source claude --import {run_id} で配置できます" in captured.err
    # 人の操作で取得が続くかもしれないので、Chrome は終了させない
    assert env.listed == [] and env.terminated == []
    assert env.clock.now >= 120
    assert not (env.input_dir / "example" / "members").exists()


def test_collect_claude_does_not_prompt_while_the_export_progresses(claude_env, capsys):
    """進捗（結果の増加）が続いている間は、ブラウザの表示を確かめる案内を出さない。

    結果は 120 秒まで 20 秒ごとに 1 件増え、そこで止まる。案内は最後の進捗から 60 秒が
    過ぎた 180 秒に初めて出る。
    """
    env = claude_env
    env.ext.manifest = None

    def grow():
        if env.clock.now > 120 or env.clock.now % 20:
            return
        path = env.staging / env.ext.specs[0]["run_id"] / "progress.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        body["results"].append(body["results"][0])
        path.write_text(json.dumps(body), encoding="utf-8")

    env.clock.on_sleep.append(grow)
    assert _collect_claude(env, "--org", "example", "--timeout", "4") == 1
    notices = [line for line in capsys.readouterr().out.splitlines()
               if line.startswith("  待機中（")]
    assert notices == [
        "  待機中（3 分経過）: ブラウザに「要操作」の表示が出ていないか確認してください"]


def test_collect_claude_waits_for_the_configured_minutes(claude_env):
    """--timeout を省くと claude_export.timeout_minutes（既定 15 分）まで待つ。"""
    env = claude_env
    env.ext.manifest = None
    assert _collect_claude(env, "--org", "example") == 1
    assert 15 * 60 <= env.clock.now < 15 * 60 + 5


def test_collect_claude_keep_browser_leaves_chrome_running(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--org", "example", "--keep-browser") == 0
    assert env.listed == [] and env.terminated == []
    assert "Chrome を終了しました" not in capsys.readouterr().out


def test_collect_claude_warns_when_chrome_has_already_gone(claude_env, capsys):
    """終了させる Chrome が見つからなくても、配置が済んでいれば成功のまま。"""
    env = claude_env
    env.pids = []
    assert _collect_claude(env, "--org", "example") == 0
    assert env.terminated == []
    assert "このプロファイルの Chrome のプロセスが見つかりませんでした" in capsys.readouterr().err


@pytest.mark.parametrize("prefs", [
    None,                                                     # --setup をしていない
    {"download": {"default_directory": "/somewhere/else"}},   # 別のダウンロード先
    "{broken",
])
def test_collect_claude_requires_the_setup(claude_env, capsys, prefs):
    env = claude_env
    path = env.profiles / "corp" / "Default" / "Preferences"
    if prefs is None:
        path.unlink()
    else:
        path.write_text(prefs if isinstance(prefs, str) else json.dumps(prefs), encoding="utf-8")

    assert _collect_claude(env, "--org", "example") == 1
    assert env.ext.commands == []
    assert ("先に collect --source claude --setup corp を実行し、ブラウザの操作の後に "
            "--finish-setup corp を実行してください") in capsys.readouterr().err


def test_collect_claude_requires_chrome(claude_env, capsys):
    env = claude_env
    env.chrome.unlink()
    assert _collect_claude(env, "--org", "example") == 1
    assert env.ext.commands == []
    assert f"Chrome が見つかりません: {env.chrome}" in capsys.readouterr().err


def test_collect_claude_import_places_a_run_that_timed_out(claude_env, capsys):
    """時間切れの後に拡張機能が書き終えた manifest を、Chrome に触れずに配置する。"""
    env = claude_env
    env.ext.manifest = None
    assert _collect_claude(env, "--org", "example", "--timeout", "1") == 1
    run_id = env.ext.specs[0]["run_id"]
    run_dir = env.staging / run_id
    body = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
    (run_dir / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    capsys.readouterr()

    assert _collect_claude(env, "--import", run_id) == 0
    assert len(env.ext.commands) == 1          # 起動しない
    assert env.listed == [] and env.terminated == []
    for kind in ("members", "spend", "code"):
        assert _placed(env, "example", kind).is_file()
    out = capsys.readouterr().out
    assert f"profile corp: 当月モード（2026-10）run_id {run_id}" in out
    assert "  配置 3 件・失敗 0 件" in out


def test_collect_claude_import_verifies_against_the_month_of_the_run(claude_env, capsys):
    """取り込む日の当月ではなく、run.json に記録したその実行の対象月で検証する。"""
    env = claude_env
    env.ext.manifest = None
    assert _collect_claude(env, "--org", "example", "--timeout", "1") == 1
    run_id = env.ext.specs[0]["run_id"]
    run_dir = env.staging / run_id
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    (run_dir / "run.json").write_text(json.dumps({**record, "month": "2026-09"}),
                                      encoding="utf-8")
    body = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
    (run_dir / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    capsys.readouterr()

    assert _collect_claude(env, "--import", run_id) == 1
    out = capsys.readouterr().out
    assert "支出レポートの期間 2026-10-01〜2026-10-06 が対象月 2026-09 の1日から" in out


def test_collect_claude_import_without_a_run_record(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--import", "corp-current-20261007-110009") == 1
    err = capsys.readouterr().err
    assert str(env.staging / "corp-current-20261007-110009" / "run.json") in err
    assert "がありません" in err


def test_collect_claude_import_without_a_manifest(claude_env, capsys):
    env = claude_env
    env.ext.manifest = None
    assert _collect_claude(env, "--org", "example", "--timeout", "1") == 1
    run_id = env.ext.specs[0]["run_id"]
    capsys.readouterr()
    assert _collect_claude(env, "--import", run_id) == 1
    assert "manifest.json がまだありません" in capsys.readouterr().err


@pytest.mark.parametrize("run_id", ["../corp-current-20261007-110009", "a/b", ".."])
def test_collect_claude_import_rejects_a_path(claude_env, capsys, run_id):
    assert _collect_claude(claude_env, "--import", run_id) == 1
    assert "staging の実行ディレクトリ名ではありません" in capsys.readouterr().err


def test_collect_claude_import_rejects_a_target_no_longer_configured(claude_env, capsys):
    env = claude_env
    env.ext.manifest = None
    assert _collect_claude(env, "--org", "example", "--timeout", "1") == 1
    run_id = env.ext.specs[0]["run_id"]
    run_dir = env.staging / run_id
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    record["spec"]["orgs"][0]["dir"] = "org-x"
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    capsys.readouterr()

    assert _collect_claude(env, "--import", run_id) == 1
    assert "run.json の対象 org-x は claude_export の設定にありません" in capsys.readouterr().err


def test_collect_claude_setup_launches_chrome_and_returns(claude_env, capsys):
    """--setup はプロファイルと staging を作り、Chrome を起動して手順を表示したら待たずに終わる。"""
    env = claude_env
    profile_dir = env.profiles / "new"
    assert _collect_claude(env, "--setup", "new") == 0

    assert env.ext.commands == [[
        str(env.chrome), f"--user-data-dir={profile_dir}", "--no-first-run",
        "--no-default-browser-check", "https://claude.ai/login"]]
    assert profile_dir.is_dir() and env.staging.is_dir()
    # Chrome の終了を待たず、設定も書かない（--finish-setup が書く）
    assert env.listed == [] and env.terminated == []
    assert not (profile_dir / "Default" / "Preferences").exists()

    lines = capsys.readouterr().out.splitlines()
    assert f"     {claude_export.extension_dir().resolve()}" in lines
    assert any("chrome://extensions" in line for line in lines)
    assert any("collect --source claude --finish-setup new を実行する" in line for line in lines)


@pytest.mark.parametrize("name", ["..", "a/b", "corp main"])
def test_collect_claude_setup_rejects_a_bad_profile_name(claude_env, capsys, name):
    assert _collect_claude(claude_env, "--setup", name) == 1
    assert f"プロファイル名 '{name}' は使えません" in capsys.readouterr().err
    assert claude_env.ext.commands == []


def _write_raw_preferences(profile_dir: Path, prefs: dict,
                           name: str = "Preferences") -> None:
    path = profile_dir / "Default" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs), encoding="utf-8")


def _loaded_extension(path: Path | str) -> dict:
    """拡張機能を path から読み込んだプロファイルの設定（Chrome が書く形の一部）。"""
    return {"extensions": {"settings": {claude_export.EXTENSION_ID: {"path": str(path)}}}}


def _prepared_profile(env, name: str = "new") -> Path:
    """--setup の後、人がログインと拡張機能の読み込みを済ませたプロファイル。"""
    profile_dir = env.profiles / name
    _write_raw_preferences(profile_dir, {"keep": True})
    _write_raw_preferences(profile_dir, _loaded_extension(claude_export.extension_dir().resolve()),
                           "Secure Preferences")
    return profile_dir


def test_collect_claude_finish_setup_stops_chrome_and_writes_preferences(claude_env, capsys):
    env = claude_env
    profile_dir = _prepared_profile(env)
    # Chrome が動いていて、終了を求めると次の見回りで消える
    listing = iter([[CLAUDE_PID], [CLAUDE_PID]])
    env.pids = lambda: next(listing, [])

    assert _collect_claude(env, "--finish-setup", "new") == 0

    assert env.terminated == [[CLAUDE_PID]]
    assert env.ext.commands == []             # Chrome は起動しない
    prefs = json.loads((profile_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["keep"] is True
    assert prefs["download"]["default_directory"] == str(env.staging)
    assert prefs["download"]["prompt_for_download"] is False
    assert prefs["profile"]["content_settings"]["exceptions"]["automatic_downloads"][
        "https://claude.ai:443,*"]["setting"] == 1
    out = capsys.readouterr().out
    assert "Chrome を終了しました" in out
    assert "プロファイル new の設定を書きました" in out
    assert "collect --source claude --dry-run で計画を確認できます" in out

    # 再実行しても同じ設定のまま（変更なし）
    before = (profile_dir / "Default" / "Preferences").read_bytes()
    assert _collect_claude(env, "--finish-setup", "new") == 0
    assert (profile_dir / "Default" / "Preferences").read_bytes() == before
    assert "プロファイル new の設定は済んでいます（変更なし）" in capsys.readouterr().out


def test_collect_claude_finish_setup_when_chrome_is_not_running(claude_env, capsys):
    env = claude_env
    profile_dir = _prepared_profile(env)
    env.pids = []
    assert _collect_claude(env, "--finish-setup", "new") == 0
    assert env.terminated == []
    prefs = json.loads((profile_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["download"]["default_directory"] == str(env.staging)
    assert "Chrome を終了しました" not in capsys.readouterr().out


def test_collect_claude_finish_setup_reads_the_extension_from_preferences(claude_env):
    """拡張機能の設定は Secure Preferences と Preferences のどちらにあってもよい。"""
    env = claude_env
    profile_dir = env.profiles / "new"
    _write_raw_preferences(
        profile_dir, _loaded_extension(claude_export.extension_dir().resolve()))
    env.pids = []
    assert _collect_claude(env, "--finish-setup", "new") == 0


@pytest.mark.parametrize("secure,fragment", [
    (None, "拡張機能の読み込みを確認できませんでした"),
    ("{broken", "拡張機能の読み込みを確認できませんでした"),
    (_loaded_extension("/somewhere/else/browser_extension"),
     "拡張機能が別の場所から読み込まれています"),
])
def test_collect_claude_finish_setup_requires_the_extension(claude_env, capsys, secure, fragment):
    env = claude_env
    profile_dir = env.profiles / "new"
    _write_raw_preferences(profile_dir, {"keep": True})
    if isinstance(secure, dict):
        _write_raw_preferences(profile_dir, secure, "Secure Preferences")
    elif secure is not None:
        (profile_dir / "Default" / "Secure Preferences").write_text(secure, encoding="utf-8")
    env.pids = []

    assert _collect_claude(env, "--finish-setup", "new") == 1
    err = capsys.readouterr().err
    assert fragment in err
    assert str(claude_export.extension_dir().resolve()) in err
    # 設定は書かない
    assert json.loads((profile_dir / "Default" / "Preferences")
                      .read_text(encoding="utf-8")) == {"keep": True}


def test_collect_claude_finish_setup_without_preferences(claude_env, capsys):
    """Preferences が無い（Chrome が一度も起動していない）ときは書かずに失敗する。"""
    env = claude_env
    profile_dir = env.profiles / "new"
    _write_raw_preferences(profile_dir, _loaded_extension(claude_export.extension_dir().resolve()),
                           "Secure Preferences")
    env.pids = []
    assert _collect_claude(env, "--finish-setup", "new") == 1
    assert "プロファイルを Chrome で一度起動してから実行してください" in capsys.readouterr().err
    assert not (profile_dir / "Default" / "Preferences").exists()


def test_collect_claude_finish_setup_when_chrome_does_not_exit(claude_env, capsys):
    """Chrome が終了しきらなければ設定を書かない（動いている間に書くと上書きされる）。"""
    env = claude_env
    profile_dir = _prepared_profile(env)
    assert _collect_claude(env, "--finish-setup", "new") == 1
    assert env.terminated == [[CLAUDE_PID]]
    assert "30 秒待っても Chrome が終了しませんでした" in capsys.readouterr().err
    assert json.loads((profile_dir / "Default" / "Preferences")
                      .read_text(encoding="utf-8")) == {"keep": True}
    assert 30 <= env.clock.now < 35


def test_collect_claude_finish_setup_requires_the_profile(claude_env, capsys):
    assert _collect_claude(claude_env, "--finish-setup", "new") == 1
    assert "先に collect --source claude --setup new を実行してください" \
        in capsys.readouterr().err


def test_collect_claude_login_opens_the_login_page(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--login", "corp") == 0
    assert env.ext.commands == [[
        str(env.chrome), f"--user-data-dir={env.profiles / 'corp'}", "--no-first-run",
        "--no-default-browser-check", "https://claude.ai/login"]]
    assert env.listed == []           # 終了は待たない
    assert "ログインしたら Chrome を閉じてください" in capsys.readouterr().out


def test_collect_claude_login_requires_the_profile(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--login", "group") == 1
    assert env.ext.commands == []
    assert "先に collect --source claude --setup group を実行してください" \
        in capsys.readouterr().err


def test_collect_claude_list_orgs_prints_a_table(claude_env, capsys):
    env = claude_env
    env.ext.orgs = [
        {"uuid": CLAUDE_UUID1, "name": "Example Org", "rate_limit_tier": "tier_a",
         "plan": "Team"},
        {"uuid": CLAUDE_UUID2, "name": "Sample", "rate_limit_tier": None, "plan": None},
    ]
    assert _collect_claude(env, "--list-orgs", "corp") == 0

    [spec] = env.ext.specs
    assert spec == {"run_id": spec["run_id"], "action": "list-orgs"}
    assert re.fullmatch(r"corp-list-orgs-\d{8}-\d{6}", spec["run_id"])
    lines = capsys.readouterr().out.splitlines()
    table = lines[lines.index(next(line for line in lines if line.startswith("uuid"))):][:3]
    assert table == [
        "uuid                                  name         rate_limit_tier  plan",
        f"{CLAUDE_UUID1}  Example Org  tier_a           Team",
        f"{CLAUDE_UUID2}  Sample       -                -",
    ]
    assert env.terminated == [[CLAUDE_PID]]


def test_collect_claude_list_orgs_reports_the_extension_error(claude_env, capsys):
    env = claude_env
    env.ext.orgs = {"error": "HTTP 403"}
    assert _collect_claude(env, "--list-orgs", "corp", "--keep-browser") == 1
    assert "組織の一覧を取得できませんでした: HTTP 403" in capsys.readouterr().err
    assert env.terminated == []


def test_collect_claude_list_orgs_times_out(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--list-orgs", "corp", "--timeout", "1") == 1
    assert "1 分待っても組織の一覧が届きませんでした" in capsys.readouterr().err
    assert env.terminated == []


def test_collect_claude_list_orgs_requires_the_setup(claude_env, capsys):
    env = claude_env
    assert _collect_claude(env, "--list-orgs", "group") == 1
    assert env.ext.commands == []
    assert "--setup group" in capsys.readouterr().err


# --- 複数 workspace のレイアウト ---


def _nested_org(make_input, org: str = "org-x") -> Path:
    """main / second の2 workspace を持つ組織（members は main にだけ置く）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 8.0)], "2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Premium"], org=org, workspace="main")
    make_input(
        {"2026-05": [spend_row("a@x.jp", 18.0)], "2026-06": [spend_row("a@x.jp", 20.0)]},
        org=org, workspace="second")
    return input_dir


def _workspace_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "workspaces.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def test_init_org_creates_nested_scaffold(tmp_path, capsys):
    input_dir, output_dir = tmp_path / "input", tmp_path / "reports"
    rc = main([
        "init-org", "org-x", "--workspaces", "main, second",
        "--input-dir", str(input_dir), "--output-dir", str(output_dir),
    ])
    assert rc == 0
    for workspace in ("main", "second"):
        for sub in ("spend", "members", "code-analytics"):
            assert (input_dir / "org-x" / workspace / sub).is_dir()
    # 直下に spend/ を作らない（作ると混在レイアウトになる）
    assert not (input_dir / "org-x" / "spend").exists()
    # members-info は人単位なので組織直下に1つだけ
    assert (input_dir / "org-x" / "members-info.csv").is_file()
    assert ingest.discover_workspaces(input_dir / "org-x") == ["main", "second"]
    assert discover_orgs(input_dir) == ["org-x"]

    out = capsys.readouterr().out
    assert "organizations.<組織名>.workspaces" in out
    assert "primary: true" in out
    assert "main, second" in out


def test_init_org_rejects_workspaces_for_existing_flat_data(make_input, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x")
    rc = main(["init-org", "org-x", "--workspaces", "main",
               "--input-dir", str(input_dir), "--output-dir", str(input_dir.parent / "r")])
    assert rc == 1
    assert "spend/" in capsys.readouterr().err
    assert not (input_dir / "org-x" / "main").exists()


@pytest.mark.parametrize("value", ["", "  ", "a/b", ".hidden", "main,summary"])
def test_init_org_rejects_invalid_workspace_names(tmp_path, capsys, value):
    input_dir = tmp_path / "input"
    rc = main(["init-org", "org-x", "--workspaces", value,
               "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports")])
    assert rc == 1
    assert capsys.readouterr().err
    assert not input_dir.exists()   # 1つでも不正なら1つも作らない


# 主・副の2 workspace を持つ組織の設定（副は Premium 固定）
_NESTED_CONFIG = (
    "organizations:\n"
    "  {org}:\n"
    "    workspaces:\n"
    "      main:\n"
    "        primary: true\n"
    "        label: 主スペース\n"
    "      second:\n"
    "        label: 副スペース\n"
    "        fixed_seat: premium\n"
)


def _nested_ready(make_input, tmp_path: Path, org: str = "org-x",
                  second_months: tuple[str, ...] = ("2026-05", "2026-06")) -> tuple[Path, str]:
    """分析できる2 workspace の組織（両方に members）と、その設定ファイルのパス。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 8.0)], "2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Premium"], org=org, workspace="main")
    make_input(
        {month: [spend_row("a@x.jp", 20.0)] for month in second_months},
        members=["a@x.jp,Premium"], org=org, workspace="second")
    path = tmp_path / f"nested-{org}.yaml"
    path.write_text(_NESTED_CONFIG.format(org=org), encoding="utf-8", newline="\n")
    return input_dir, str(path)


def _analyze_nested(config: str, input_dir: Path, tmp_path: Path, *extra: str) -> int:
    return main(["analyze", "--config", config, "--input-dir", str(input_dir),
                 "--output-dir", str(tmp_path / "reports"), *extra])


def test_analyze_accepts_nested_layout(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06") == 0

    out = capsys.readouterr().out
    assert "人数: 2 名（アカウント 3）" in out
    assert "[主スペース] メンバー 2 名" in out
    assert "[副スペース] メンバー 1 名" in out
    assert "複数スペース: 払い出し候補" in out
    report_text = out_file(tmp_path / "reports", REPORT, org="org-x").read_text(encoding="utf-8")
    assert "## 複数スペースの利用" in report_text
    assert "| 対象メンバー数 | 2 名（アカウント 3） |" in report_text


def test_analyze_nested_v2_writes_the_workspace_column(make_input, tmp_path):
    input_dir, config = _nested_ready(make_input, tmp_path)
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--decision-version", "v2") == 0
    path = out_file(tmp_path / "reports", DECISION_EVIDENCE, org="org-x")
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert list(rows[0])[:2] == ["email", "workspace"]
    # 複数アカウント保有者も主の行1本（副の行は作らない）
    assert [(r["email"], r["workspace"]) for r in rows] == [
        ("a@x.jp", "main"), ("b@x.jp", "main")]


def test_analyze_preview_writes_nested_and_single_orgs(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    make_input({"2026-06": [spend_row("c@y.jp", 10.0)]}, members=["c@y.jp,Premium"],
               org="org-a")
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                         "--preview", "--days", "10")
    assert rc == 0
    out = capsys.readouterr().out
    assert "人数: 2 名（アカウント 3）" in out
    assert out_file(tmp_path / "reports", PREVIEW).is_file()
    assert out_file(tmp_path / "reports", PREVIEW, org="org-x").is_file()


def test_analyze_preview_writes_single_nested_org(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                         "--preview", "--days", "10")
    assert rc == 0
    assert "人数: 2 名（アカウント 3）" in capsys.readouterr().out
    md = out_file(tmp_path / "reports", PREVIEW, org="org-x").read_text(encoding="utf-8")
    html = out_file(tmp_path / "reports", PREVIEW_DASHBOARD, org="org-x").read_text(
        encoding="utf-8")
    assert "### スペース別" in md and "## 人別の需要（スペース合算）" in md
    assert "<th>スペース</th>" in html


def test_preview_multi_without_optional_sections_has_one_blank_line(make_input, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 5.0, net=0.0)]},
        members=["a@x.jp,Premium"], org="org-x", workspace="main")
    make_input(
        {"2026-06": [spend_row("a@x.jp", 5.0, net=0.0)]},
        members=["a@x.jp,Premium"], org="org-x", workspace="second")
    config = _workspace_config(tmp_path, _NESTED_CONFIG.format(org="org-x"))
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 0
    md = out_file(tmp_path / "reports", PREVIEW, org="org-x").read_text(encoding="utf-8")
    headings = [line for line in md.splitlines() if line.startswith("## ")]
    assert headings == ["## サマリ", "## 一次判断テーブル", "## 人別の需要（スペース合算）",
                        "## 注意事項", "## データ検証・警告", "## 考察"]
    assert "\n\n\n" not in md


def test_analyze_preview_writes_all_nested_orgs(
    make_input, tmp_path, capsys
):
    """複数の入れ子組織を同じ実行で書く。"""
    input_dir, _ = _nested_ready(make_input, tmp_path, org="org-x")
    _nested_ready(make_input, tmp_path, org="org-y")
    path = tmp_path / "two-nested.yaml"
    path.write_text(
        _NESTED_CONFIG.format(org="org-x")
        + _NESTED_CONFIG.format(org="org-y").removeprefix("organizations:\n"),
        encoding="utf-8", newline="\n")
    rc = _analyze_nested(str(path), input_dir, tmp_path, "--month", "2026-06",
                         "--preview", "--days", "10")
    assert rc == 0
    assert capsys.readouterr().out.count("人数: 2 名（アカウント 3）") == 2
    assert out_file(tmp_path / "reports", PREVIEW, org="org-x").is_file()
    assert out_file(tmp_path / "reports", PREVIEW, org="org-y").is_file()


@pytest.mark.parametrize("month", ["2026-07", "2026-08"])
def test_single_nested_preview_matches_flat_bytes(tmp_path, month):
    source = REPO_ROOT / "examples" / "input" / "org-b"
    flat_root = tmp_path / "flat"
    nested_root = tmp_path / "nested"
    shutil.copytree(source, flat_root / "org-b")
    for name in ("spend", "members", "code-analytics"):
        shutil.copytree(source / name, nested_root / "org-b" / "main" / name)
    for path in source.glob("members-info*.csv"):
        shutil.copy2(path, nested_root / "org-b" / path.name)
    config = tmp_path / "nested.yaml"
    config.write_text("organizations:\n  org-b:\n    workspaces:\n      main:\n"
                      "        primary: true\n", encoding="utf-8", newline="\n")
    outputs = []
    for root, setting in ((flat_root, CONFIG), (nested_root, str(config))):
        output = tmp_path / f"out-{root.name}"
        assert main(["analyze", "--config", setting, "--input-dir", str(root),
                     "--output-dir", str(output), "--org", "org-b", "--month", month,
                     "--preview", "--days", "31"]) == 0
        outputs.append(output)
    for artifact in (PREVIEW, PREVIEW_DASHBOARD):
        assert (artifact.path(outputs[0] / "org-b", month, "org-b").read_bytes()
                == artifact.path(outputs[1] / "org-b", month, "org-b").read_bytes())


def test_preview_days_uses_selected_period_and_explicit_days_overrides(make_input, tmp_path,
                                                                        capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    for name, ends in (("main", ("05", "10")), ("second", ("09",))):
        spend_dir = input_dir / "org-x" / name / "spend"
        (spend_dir / "spend_2026-06.csv").unlink()
        for end in ends:
            (spend_dir / f"spend-report-uuid-2026-06-01-to-2026-06-{end}.csv").write_text(
                SPEND_HEADER + "\n" + spend_row("a@x.jp", 10.0) + "\n",
                encoding="utf-8", newline="\n")
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview") == 1
    assert "main: 10 日 / second: 9 日" in capsys.readouterr().err
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 0
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "0") == 1
    assert "--days は 1〜30" in capsys.readouterr().err


def test_preview_started_missing_workspace_stops_multi_org_run(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path, second_months=("2026-05",))
    make_input({"2026-06": [spend_row("c@y.jp", 10.0)]},
               members=["c@y.jp,Premium"], org="org-a")
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 1
    assert "速報は欠月の workspace を需要 0 として扱わない" in capsys.readouterr().err


def test_preview_unstarted_secondary_is_skipped(make_input, tmp_path, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x", workspace="main")
    make_input({"2026-07": [spend_row("a@x.jp", 20.0)]},
               members=["a@x.jp,Premium"], members_month="2026-07",
               org="org-x", workspace="second")
    config = tmp_path / "nested.yaml"
    config.write_text(_NESTED_CONFIG.format(org="org-x"), encoding="utf-8", newline="\n")
    assert _analyze_nested(str(config), input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 0
    assert "まだ利用が始まっていない" in capsys.readouterr().out
    md = out_file(tmp_path / "reports", PREVIEW, org="org-x").read_text(encoding="utf-8")
    assert "未開始（対象月以前のデータなし）" in md


def test_preview_snapshot_section_binds_secondary_only(make_input, tmp_path):
    input_dir, config = _nested_ready(make_input, tmp_path)
    spend_dir = input_dir / "org-x" / "second" / "spend"
    (spend_dir / "spend_2026-06.csv").unlink()
    for end in ("05", "10"):
        (spend_dir / f"spend-report-uuid-2026-06-01-to-2026-06-{end}.csv").write_text(
            SPEND_HEADER + "\n" + spend_row("a@x.jp", 20.0) + "\n",
            encoding="utf-8", newline="\n")
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 0
    md = out_file(tmp_path / "reports", PREVIEW, org="org-x").read_text(encoding="utf-8")
    html = out_file(tmp_path / "reports", PREVIEW_DASHBOARD, org="org-x").read_text(
        encoding="utf-8")
    assert md.count("## 月中の利用推移（スナップショット差分）（副スペース）") == 1
    assert "## 月中の利用推移（スナップショット差分）（主スペース）" not in md
    assert html.count("月中の利用推移（スナップショット差分）（副スペース）") == 1
    assert "月中の利用推移（スナップショット差分）（主スペース）" not in html


def test_doctor_json_keeps_other_issues_when_members_info_is_unreadable(
    make_input, tmp_path, capsys
):
    input_dir = make_input({"2026-06": [spend_row("b@y.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x")
    (input_dir / "org-x" / "members-info.csv").write_text(
        "department\n架空部\n", encoding="utf-8", newline="\n")
    assert main(["doctor", "--config", CONFIG, "--input-dir", str(input_dir),
                 "--month", "2026-06", "--format", "json"]) == 1
    issues = json.loads(capsys.readouterr().out)
    assert {item["code"] for item in issues} == {
        "MEMBERS_INFO_UNREADABLE", "MEMBER_ROW_MISSING"}
    unreadable = next(item for item in issues if item["code"] == "MEMBERS_INFO_UNREADABLE")
    assert str(input_dir) not in unreadable["message"]


def test_doctor_members_info_ignores_unstarted_workspace(make_input, tmp_path, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x", workspace="main")
    make_input({"2026-07": [spend_row("b@y.jp", 10.0)]},
               members=["b@y.jp,Premium"], members_month="2026-07",
               org="org-x", workspace="second")
    (input_dir / "org-x" / "members-info.csv").write_text(
        "email\na@x.jp\n", encoding="utf-8", newline="\n")
    config = tmp_path / "nested.yaml"
    config.write_text(_NESTED_CONFIG.format(org="org-x"), encoding="utf-8", newline="\n")
    assert main(["doctor", "--config", str(config), "--input-dir", str(input_dir),
                 "--month", "2026-06", "--format", "json"]) == 0
    codes = [item["code"] for item in json.loads(capsys.readouterr().out)]
    assert "MEMBERS_INFO_UNREGISTERED" not in codes
    assert "SECONDARY_ONLY_ACCOUNT" not in codes


@pytest.mark.parametrize("unstarted", ["main", "second"])
def test_doctor_skips_unstarted_workspace_with_one_warning(
    make_input, tmp_path, capsys, unstarted
):
    for name in ("main", "second"):
        month = "2026-07" if name == unstarted else "2026-06"
        input_dir = make_input(
            {month: [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Premium"],
            members_month=month, org="org-x", workspace=name)
    config = _workspace_config(tmp_path, _NESTED_CONFIG.format(org="org-x"))
    args = ["doctor", "--config", config, "--input-dir", str(input_dir),
            "--month", "2026-06", "--format", "json"]
    assert main(args) == 0
    output = capsys.readouterr().out
    issues = json.loads(output)
    assert len(issues) == 1
    assert issues[0]["code"] == "MISSING_SPEND"
    assert issues[0]["severity"] == "warning"
    assert issues[0]["scope"] == {"org": "org-x", "month": "2026-06", "workspace": unstarted}
    assert "まだ始まっていない workspace として検査しませんでした" in issues[0]["message"]
    assert str(input_dir) not in issues[0]["message"]
    assert main(args) == 0
    assert capsys.readouterr().out == output


def test_doctor_errors_for_started_workspace_with_missing_month(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path, second_months=("2026-05",))
    assert main(["doctor", "--config", config, "--input-dir", str(input_dir),
                 "--month", "2026-06", "--format", "json"]) == 1
    issues = json.loads(capsys.readouterr().out)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    assert [(issue["code"], issue["scope"]["workspace"]) for issue in errors] == [
        ("MISSING_SPEND", "second")]


def test_doctor_nested_unresolvable_filename_keeps_input_error(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    (input_dir / "org-x" / "second" / "spend" /
     "spend-2026-06-01-to-2026-07-05.csv").write_text(
        SPEND_HEADER + "\n", encoding="utf-8", newline="\n")
    assert main(["doctor", "--config", config, "--input-dir", str(input_dir),
                 "--month", "2026-06", "--format", "json"]) == 1
    issues = json.loads(capsys.readouterr().out)
    issue = next(item for item in issues if item["code"] == "MISSING_SPEND")
    assert issue["severity"] == "error"
    assert issue["scope"]["workspace"] == "second"
    assert "ファイル名から解決できません" in issue["message"]


def test_doctor_workspace_start_oserror_still_inspects_input(
    make_input, tmp_path, capsys, monkeypatch
):
    input_dir, config = _nested_ready(make_input, tmp_path)
    (input_dir / "org-x" / "second" / "members" / "members_2026-06.csv").unlink()
    original = ingest.workspace_started

    def fail_for_second(directory, month):
        if directory.name == "second":
            raise OSError("読み取り失敗")
        return original(directory, month)

    monkeypatch.setattr(ingest, "workspace_started", fail_for_second)
    assert main(["doctor", "--config", config, "--input-dir", str(input_dir),
                 "--month", "2026-06", "--format", "json"]) == 1
    issues = json.loads(capsys.readouterr().out)
    assert [(item["code"], item["scope"]["workspace"]) for item in issues] == [
        ("MISSING_MEMBERS", "second")]


def test_doctor_reports_secondary_only_account_with_workspace_scope(
    make_input, tmp_path, capsys
):
    input_dir, config = _nested_ready(make_input, tmp_path)
    (input_dir / "org-x" / "second" / "members" / "members_2026-06.csv").write_text(
        "Email,Seat Type\nc@y.jp,Premium\n", encoding="utf-8", newline="\n")
    assert main(["doctor", "--config", config, "--input-dir", str(input_dir),
                 "--month", "2026-06", "--format", "json"]) == 0
    issues = json.loads(capsys.readouterr().out)
    person = next(item for item in issues if item["code"] == "SECONDARY_ONLY_ACCOUNT")
    assert person["scope"]["workspace"] == "second"
    assert person["scope"]["emails"] == ["c@y.jp"]


def test_allow_missing_workspace_rejects_an_unknown_name(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                         "--allow-missing-workspace", "secnod")
    assert rc == 1
    assert "secnod" in capsys.readouterr().err
    assert not (tmp_path / "reports" / "org-x" / "2026-06").exists()


def test_allow_missing_workspace_cannot_be_used_with_preview(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06", "--preview",
                         "--allow-missing-workspace", "second")
    assert rc == 1
    assert "速報は欠月の workspace を需要 0 として扱いません" in capsys.readouterr().err


def test_allow_missing_workspace_must_name_one_org(make_input, tmp_path, capsys):
    """同じ workspace 名を持つ組織が複数あると、どの組織の欠月かが決まらない。"""
    input_dir, _ = _nested_ready(make_input, tmp_path, org="org-x")
    _nested_ready(make_input, tmp_path, org="org-y")
    path = tmp_path / "two-nested.yaml"
    path.write_text(
        _NESTED_CONFIG.format(org="org-x")
        + _NESTED_CONFIG.format(org="org-y").removeprefix("organizations:\n"),
        encoding="utf-8", newline="\n")
    rc = _analyze_nested(str(path), input_dir, tmp_path, "--month", "2026-06",
                         "--allow-missing-workspace", "second")
    assert rc == 1
    err = capsys.readouterr().err
    assert "org-x" in err and "org-y" in err and "--org" in err
    # --org で1つに絞れば受け付ける
    assert _analyze_nested(str(path), input_dir, tmp_path, "--month", "2026-06",
                           "--org", "org-x", "--allow-missing-workspace", "second") == 0


def test_missing_month_of_a_started_workspace_stops_without_the_option(
    make_input, tmp_path, capsys
):
    input_dir, config = _nested_ready(make_input, tmp_path, second_months=("2026-05",))
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06")
    assert rc == 1
    assert "--allow-missing-workspace second" in capsys.readouterr().err


def test_allow_missing_workspace_analyzes_the_month_as_no_usage(
    make_input, tmp_path, capsys
):
    input_dir, config = _nested_ready(make_input, tmp_path, second_months=("2026-05",))
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                         "--allow-missing-workspace", "second")
    assert rc == 0
    report_text = out_file(tmp_path / "reports", REPORT, org="org-x").read_text(encoding="utf-8")
    # 指定した workspace の欠月は需要 0 として扱い、その旨がレポートの警告に残る
    assert "workspace second は 2026-06 のスペンドレポートが無いため需要 0 として" in report_text
    assert "副スペース（Premium 固定）: 需要 0（対象月の spend 無し）" in report_text


def test_allow_missing_workspace_for_every_workspace_completes(make_input, tmp_path):
    """全 workspace の欠月を許可した月も、欠月のスキップで止めずに分析する。"""
    input_dir, config = _nested_ready(make_input, tmp_path)
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-07",
                         "--allow-missing-workspace", "main",
                         "--allow-missing-workspace", "second")
    assert rc == 0
    path = out_file(tmp_path / "reports", REPORT, org="org-x", month="2026-07")
    text = path.read_text(encoding="utf-8")
    assert "workspace main は 2026-07 のスペンドレポートが無いため需要 0" in text
    assert "workspace second は 2026-07 のスペンドレポートが無いため需要 0" in text


def test_org_without_any_started_workspace_is_treated_as_missing_data(
    make_input, tmp_path, capsys
):
    """分析できた workspace が1つも無い組織は、対象月のデータが無い組織と同じ扱い。"""
    input_dir, config = _nested_ready(make_input, tmp_path)
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-04",
                         "--allow-missing-workspace", "main")
    assert rc == 1
    assert "2026-04" in capsys.readouterr().err
    # 複数組織の実行ではスキップして他の組織を書く
    make_input({"2026-04": [spend_row("c@y.jp", 10.0)]}, members=["c@y.jp,Premium"],
               org="org-a", members_month="2026-04")
    rc = _analyze_nested(config, input_dir, tmp_path, "--month", "2026-04",
                         "--allow-missing-workspace", "main")
    assert rc == 0
    assert "スキップした組織: org-x" in capsys.readouterr().out
    assert not (tmp_path / "reports" / "org-x" / "2026-04").exists()


def test_analyze_rejects_mixed_layout(make_input, tmp_path, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x")
    make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x", workspace="second")
    rc = main(["analyze", "--config", CONFIG, "--input-dir", str(input_dir),
               "--output-dir", str(tmp_path / "reports"), "--month", "2026-06"])
    assert rc == 1
    assert "混在" in capsys.readouterr().err


def test_collect_accepts_nested_layout(make_input, tmp_path, monkeypatch, capsys):
    """キャッシュは組織直下に置くので、workspace の分け方に依らず収集できる。"""
    input_dir, _ = _nested_ready(make_input, tmp_path)
    path = tmp_path / "nested-github.yaml"
    path.write_text(
        _NESTED_CONFIG.format(org="org-x") + f"    github_org: {GH_ORG}\n",
        encoding="utf-8", newline="\n")
    _stub_search(monkeypatch)
    assert _collect(str(path), input_dir, "--org", "org-x") == 0
    assert _cache_path(input_dir, "org-x").is_file()


def test_doctor_inspects_each_workspace_of_a_nested_org(make_input, tmp_path, capsys):
    input_dir = _nested_org(make_input)
    config_path = _workspace_config(tmp_path, (
        "organizations:\n"
        "  org-x:\n"
        "    workspaces:\n"
        "      main:\n"
        "        primary: true\n"
        "      second: {}\n"
    ))
    rc = main(["doctor", "--config", config_path, "--input-dir", str(input_dir),
               "--month", "2026-06", "--format", "json"])
    assert rc == 1
    issues = json.loads(capsys.readouterr().out)
    # 構造は整合しているので、残るのは workspace ごとの入力の検査だけ
    assert [i["code"] for i in issues] == ["MISSING_MEMBERS"]
    assert issues[0]["scope"]["workspace"] == "second"
    assert issues[0]["scope"]["org"] == "org-x"


def test_doctor_reports_nested_layout_without_config(make_input, capsys):
    input_dir = _nested_org(make_input)
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)
    codes = [i["code"] for i in issues]
    assert codes.count("WORKSPACE_CONFIG_MISMATCH") == 1
    # 構造の問題を報告しても、workspace ごとの入力の検査は続ける
    assert "MISSING_MEMBERS" in codes


def test_doctor_text_output_names_the_workspace(make_input, capsys):
    """workspace ごとに同じ文言が並ぶので、どのスペースの話かを読めるようにする。"""
    input_dir = _nested_org(make_input)
    _doctor(input_dir, "--month", "2026-06")
    assert "MISSING_MEMBERS（second）" in capsys.readouterr().out


def test_doctor_text_output_is_unchanged_for_a_single_workspace_org(
    make_input, capsys
):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x")
    _doctor(input_dir, "--month", "2026-06")
    assert "[error] MISSING_MEMBERS: " in capsys.readouterr().out


def test_doctor_reports_mixed_layout(make_input, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x")
    make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x", workspace="second")

    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 1
    issues = json.loads(capsys.readouterr().out)
    # レイアウトが確定しないので、中身の検査は行わず構造だけを報告する
    assert [i["code"] for i in issues] == ["WORKSPACE_LAYOUT_MIXED"]


def test_doctor_uses_the_latest_month_across_workspaces(make_input, capsys):
    input_dir = make_input({"2026-05": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-x", workspace="main")
    make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
               members=["a@x.jp,Premium"], org="org-x", workspace="second")
    _doctor(input_dir, "--format", "json")
    assert "最新月を使用: 2026-06" in capsys.readouterr().err


def test_discuss_accepts_nested_layout(make_input, tmp_path, capsys):
    input_dir, config = _nested_ready(make_input, tmp_path)
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06") == 0
    capsys.readouterr()
    rc = main(["discuss", "--config", config, "--input-dir", str(input_dir),
               "--output-dir", str(tmp_path / "reports"), "--month", "2026-06", "--dry-run"])
    assert rc == 0
    prompt = capsys.readouterr().out
    assert "## 複数スペースの利用" in prompt
    assert "## 人別の利用" in prompt


def test_discuss_preview_follows_the_analyze_rule(make_input, tmp_path, capsys):
    """速報の考察は入れ子の組織でも dry-run のプロンプトだけを出す。"""
    input_dir, config = _nested_ready(make_input, tmp_path)
    base = ["discuss", "--config", config, "--input-dir", str(input_dir),
            "--output-dir", str(tmp_path / "reports"), "--month", "2026-06",
            "--preview", "--dry-run"]
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 0
    capsys.readouterr()
    assert main(base) == 0
    prompt = capsys.readouterr().out
    assert "## 人別の需要（スペース合算）" in prompt
    assert "複数 workspace" not in prompt

    make_input({"2026-06": [spend_row("c@y.jp", 10.0)]}, members=["c@y.jp,Premium"],
               org="org-a")
    assert _analyze_nested(config, input_dir, tmp_path, "--month", "2026-06",
                           "--preview", "--days", "10") == 0
    capsys.readouterr()
    assert main(base) == 0
    prompt = capsys.readouterr().out
    assert "## 人別の需要（スペース合算）" in prompt
    assert "# Claude Team シート速報プレビュー" in prompt


def test_doctor_reports_configured_workspaces_without_an_org_directory(
    make_input, tmp_path, capsys
):
    """組織ディレクトリごと無い設定を黙って無視しない（config と実体の突き合わせ）。"""
    input_dir = _clean_org(make_input)
    config_path = _workspace_config(tmp_path, (
        "organizations:\n"
        "  org-missing:\n"
        "    workspaces:\n"
        "      main:\n"
        "        primary: true\n"
    ))
    rc = main(["doctor", "--config", config_path, "--input-dir", str(input_dir),
               "--month", "2026-06", "--format", "json"])
    assert rc == 1
    issues = json.loads(capsys.readouterr().out)
    assert [i["code"] for i in issues] == ["WORKSPACE_CONFIG_MISMATCH"]
    assert issues[0]["scope"]["config_org"] == "org-missing"


def test_doctor_prints_workspace_config_issues_in_the_settings_section(
    make_input, tmp_path, capsys
):
    input_dir = _clean_org(make_input)
    config_path = _workspace_config(tmp_path, (
        "organizations:\n"
        "  org-missing:\n"
        "    workspaces:\n"
        "      main:\n"
        "        primary: true\n"
    ))
    assert main(["doctor", "--config", config_path, "--input-dir", str(input_dir),
                 "--month", "2026-06"]) == 1
    out = capsys.readouterr().out
    assert "=== 設定検査 ===" in out
    assert "org-missing" in out
    assert "エラー 1 件" in out


def test_init_org_without_workspaces_refuses_a_nested_org(tmp_path, capsys):
    """入れ子レイアウトの組織へ再実行しても、組織直下に雛形を作って混在にしない。"""
    input_dir, output_dir = tmp_path / "input", tmp_path / "reports"
    args = ["--input-dir", str(input_dir), "--output-dir", str(output_dir)]
    assert main(["init-org", "org-x", "--workspaces", "main,second", *args]) == 0
    before = sorted(p.relative_to(input_dir).as_posix() for p in input_dir.rglob("*"))

    assert main(["init-org", "org-x", *args]) == 1

    err = capsys.readouterr().err
    assert "org-x" in err and "main/second" in err and "--workspaces" in err
    assert sorted(
        p.relative_to(input_dir).as_posix() for p in input_dir.rglob("*")) == before


def test_init_org_without_workspaces_refuses_a_mixed_org(make_input, capsys):
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x")
    make_input({"2026-06": [spend_row("a@x.jp", 10.0)]}, org="org-x", workspace="second")
    assert main(["init-org", "org-x", "--input-dir", str(input_dir),
                 "--output-dir", str(input_dir.parent / "reports")]) == 1
    assert "second" in capsys.readouterr().err
