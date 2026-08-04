"""CLI のマルチ組織対応（組織解決・--org・横断サマリ・旧レイアウト互換）と doctor のテスト。"""

import json
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
    # message は実行環境に依存しない（入力ディレクトリからの相対表記になる）
    assert str(input_dir) not in out
    assert "spend/spend_2026-06.csv" in out


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


def test_doctor_supports_legacy_flat_layout(make_input, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0)]}, members=["a@x.jp,Premium"],
    )
    assert _doctor(input_dir, "--month", "2026-06", "--format", "json") == 0
    issues = json.loads(capsys.readouterr().out)
    # 旧レイアウトは組織名を持たないため scope に org を入れない
    assert [i["code"] for i in issues] == ["MISSING_HISTORY_MONTH"]
    assert "org" not in issues[0]["scope"]


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


def test_doctor_writes_no_files(make_input, tmp_path):
    input_dir = _clean_org(make_input)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert _doctor(input_dir, "--month", "2026-06") == 0
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after


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
        # members-info.csv はヘッダ行のみの雛形が作られる
        info = input_dir / org / "members-info.csv"
        assert info.read_text(encoding="utf-8") == "email,部署,チーム,職種,追加クレジット上限,備考\n"
    assert discover_orgs(input_dir) == ["org-x", "org-y"]


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
    for bad, fragment in (
        ("summary", "予約"),
        ("a/b", "使えない文字"),
        (".hidden", "不正"),
        ("org|x", "使えない文字"),
    ):
        rc = main([
            "init-org", bad,
            "--input-dir", str(tmp_path / "input"), "--output-dir", str(tmp_path / "reports"),
        ])
        assert rc == 1
        assert fragment in capsys.readouterr().err
    assert not (tmp_path / "input").exists()
