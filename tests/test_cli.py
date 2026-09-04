"""CLI のマルチ組織対応（組織解決・--org・横断サマリ・旧レイアウトの拒否）と doctor のテスト。"""

import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from seat_analyzer import analyze, github_collect, ingest, seat_changes
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
    RECOMMENDATIONS,
    REPORT,
    USAGE_SUMMARY,
)

from .conftest import CONFIG, out_file, spend_row


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
    assert {i["code"] for i in issues} == {"MISSING_HISTORY_MONTH", "MEMBER_ROW_MISSING"}
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
    # org-a は問題なし。org-b は members 欠損（error）と履歴月欠落（warning）
    assert [(i["scope"]["org"], i["severity"], i["code"]) for i in issues] == [
        ("org-b", "error", "MISSING_MEMBERS"),
        ("org-b", "warning", "MISSING_HISTORY_MONTH"),
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
    assert [i["code"] for i in json.loads(capsys.readouterr().out)] == [
        "MISSING_HISTORY_MONTH",
    ]


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
])
def test_collect_requires_its_options(args):
    """必須オプションと収集元の選択肢は argparse が弾く。"""
    with pytest.raises(SystemExit) as excinfo:
        main(args)
    assert excinfo.value.code == 2


def test_collect_failure_text_covers_every_failure():
    """中断の理由はどの GhFailure でも表示の文言を持つ（語彙が増えたとき引き当てで落ちない）。"""
    from seat_analyzer.cli import _COLLECT_FAILURE_TEXT
    from seat_analyzer.github_collect import GhFailure

    assert set(_COLLECT_FAILURE_TEXT) == set(GhFailure)
