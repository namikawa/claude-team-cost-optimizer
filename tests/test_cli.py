"""CLI のマルチ組織対応（組織解決・--org・横断サマリ・旧レイアウトの拒否）と doctor のテスト。"""

import csv
import hashlib
import io
import json
import os
from pathlib import Path

from seat_analyzer import analyze, ingest
from seat_analyzer.cli import main
from seat_analyzer.ingest import discover_orgs
from seat_analyzer.product_usage import FEATURE_COLUMNS

from .conftest import CONFIG, spend_row


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


def _usage_rows(path: Path) -> list[list[str]]:
    text = path.read_bytes().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def test_analyze_writes_usage_summary(make_input, tmp_path, cfg, capsys):
    """usage-summary.csv が成果物として生成され、出力一覧に載る。"""
    input_dir = _make_two_orgs(make_input)
    rc, out = _run(input_dir, tmp_path, "--month", "2026-06", "--org", "org-a")
    assert rc == 0
    path = out / "org-a" / "2026-06" / "usage-summary.csv"
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
    rows = _usage_rows(out_dir / "org-a" / "2026-06" / "usage-summary.csv")
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
    assert (out / "org-a" / "2026-05" / "report.md").exists()
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
    assert "[warning] MISSING_MEMBERS" in capsys.readouterr().out


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
    assert [(i["severity"], i["code"]) for i in issues][0] == ("error", "MISSING_SPEND")
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
        assert info.read_text(
            encoding="utf-8-sig") == "email,部署,チーム,職種,追加クレジット上限,備考\n"
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
