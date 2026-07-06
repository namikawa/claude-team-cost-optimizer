"""任意ファイル members-info.csv（部署・チーム・職種・備考マッピング）のテスト。"""

from pathlib import Path

from seat_analyzer import analyze as analyze_mod
from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import write_markdown

from .conftest import spend_row


def _write_info(input_dir: Path, org: str | None, text: str) -> None:
    base = input_dir / org if org else input_dir
    (base / "members-info.csv").write_text(text, encoding="utf-8")


def test_japanese_header_merges(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard", "b@example.com,Premium"],
    )
    _write_info(
        input_dir, None,
        "email,部署,チーム,職種,備考\n"
        "a@example.com,開発部,基盤チーム,エンジニア,テスト備考\n"
        "b@example.com,営業部,西日本チーム,マネージャ,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    by_email = result.users.set_index("email")
    assert by_email.loc["a@example.com", "department"] == "開発部"
    assert by_email.loc["a@example.com", "team"] == "基盤チーム"
    assert by_email.loc["a@example.com", "role"] == "エンジニア"
    assert by_email.loc["a@example.com", "note"] == "テスト備考"
    assert by_email.loc["b@example.com", "department"] == "営業部"
    assert by_email.loc["b@example.com", "team"] == "西日本チーム"
    assert result.sources.get("members_info", "").endswith("members-info.csv")


def test_english_header_merges(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,department,team,role,note\n"
        "a@example.com,Platform,Core,Engineer,hello\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    row = result.users.set_index("email").loc["a@example.com"]
    assert row["department"] == "Platform"
    assert row["team"] == "Core"
    assert row["role"] == "Engineer"
    assert row["note"] == "hello"


def test_team_only_without_department(make_input, cfg, tmp_path):
    """チーム列だけ記入・部署未記入でも動く（チーム別サマリのみ出る）。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard", "b@example.com,Premium"],
    )
    _write_info(
        input_dir, None,
        "email,チーム,職種,備考\n"
        "a@example.com,基盤チーム,エンジニア,\n"
        "b@example.com,SREチーム,エンジニア,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    by_email = result.users.set_index("email")
    assert by_email.loc["a@example.com", "team"] == "基盤チーム"
    assert (result.users["department"] == "").all()
    out = tmp_path / "report.md"
    write_markdown(result, out)
    text = out.read_text(encoding="utf-8")
    assert "## チーム別サマリ" in text
    assert "## 部署別サマリ" not in text
    assert "| チーム |" in text  # ユーザ表にチーム列
    assert "| 部署 |" not in text


def test_no_file_no_error(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    # 列は空文字列で存在し、report 生成も落ちない
    assert (result.users["department"] == "").all()
    assert (result.users["team"] == "").all()
    assert "members_info" not in result.sources
    write_markdown(result, input_dir / "report.md")


def test_unmapped_member_is_blank_no_warning(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0), spend_row("c@example.com", 5.0)]},
        members=["a@example.com,Standard", "c@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,部署,チーム,職種,備考\na@example.com,開発部,基盤チーム,,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    by_email = result.users.set_index("email")
    assert by_email.loc["c@example.com", "department"] == ""
    assert by_email.loc["c@example.com", "team"] == ""
    # マッピング漏れは正常系のため members-info 由来の警告は出ない
    assert not any("members-info" in w or "members_info" in w for w in result.warnings)


def test_preview_merges(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,部署,チーム,職種,備考\na@example.com,開発部,基盤チーム,エンジニア,pv備考\n",
    )
    result = preview(input_dir, "2026-06", cfg, days_observed=15)
    row = result.users.set_index("email").loc["a@example.com"]
    assert row["department"] == "開発部"
    assert row["team"] == "基盤チーム"
    assert row["note"] == "pv備考"


def test_markdown_has_both_summaries_and_notes(make_input, cfg, tmp_path):
    """両軸データあり時、部署別サマリとチーム別サマリの両方が出る。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard", "b@example.com,Premium"],
    )
    _write_info(
        input_dir, None,
        "email,部署,チーム,職種,備考\n"
        "a@example.com,開発部,基盤チーム,エンジニア,ヒアリング済み\n"
        "b@example.com,営業部,西日本チーム,マネージャ,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    text = out.read_text(encoding="utf-8")
    assert "## 部署別サマリ" in text
    assert "## チーム別サマリ" in text
    # 表示順は 部署別 → チーム別
    assert text.index("## 部署別サマリ") < text.index("## チーム別サマリ")
    assert "### 備考" in text
    assert "a@example.com: ヒアリング済み" in text
    assert "| 部署 |" in text  # ユーザ表に部署列
    assert "| チーム |" in text  # ユーザ表にチーム列


def test_markdown_no_sections_without_info(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    text = out.read_text(encoding="utf-8")
    assert "## 部署別サマリ" not in text
    assert "## チーム別サマリ" not in text
    assert "### 備考" not in text


def test_legacy_department_only_still_works(make_input, cfg, tmp_path):
    """旧形式（部署のみ・チーム列なし）でも落ちず部署別サマリが出る（後方互換）。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard", "b@example.com,Premium"],
    )
    _write_info(
        input_dir, None,
        "email,部署,職種,備考\n"
        "a@example.com,開発部,エンジニア,\n"
        "b@example.com,営業部,マネージャ,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    by_email = result.users.set_index("email")
    assert by_email.loc["a@example.com", "department"] == "開発部"
    assert (result.users["team"] == "").all()
    out = tmp_path / "report.md"
    write_markdown(result, out)
    text = out.read_text(encoding="utf-8")
    assert "## 部署別サマリ" in text
    assert "## チーム別サマリ" not in text
    assert "| 部署 |" in text
    assert "| チーム |" not in text


def test_info_only_email_not_added(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,部署,チーム,職種,備考\n"
        "a@example.com,開発部,基盤チーム,,\n"
        "ghost@example.com,幽霊部,幽霊チーム,,\n",  # members にも spend にも居ない
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert "ghost@example.com" not in set(result.users["email"])


def test_load_members_info_none_when_absent(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    assert analyze_mod.ingest.load_members_info(input_dir, cfg) is None


# --- 兼務（複数所属）の按分 -----------------------------------------------

from seat_analyzer.report import _group_summary_rows  # noqa: E402


def test_dual_team_split_half_and_half(make_input, cfg):
    """A; B の2所属ユーザは両チームに 0.5 名ずつ、費用・需要も半分ずつ計上される。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 40.0),
                     spend_row("b@example.com", 10.0)]},
        members=["a@example.com,Standard", "b@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,チーム,職種,備考\n"
        "a@example.com,基盤チーム; SREチーム,,\n"
        "b@example.com,基盤チーム,,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    # 表示は正規化された半角セミコロン+スペース区切り
    assert result.users.set_index("email").loc["a@example.com", "team"] == "基盤チーム; SREチーム"
    rows = {r["group"]: r for r in _group_summary_rows(result.users, result.summary, "team")}
    assert set(rows) == {"基盤チーム", "SREチーム"}
    # a は各チームに 0.5 名、b は基盤チームに 1 名
    assert rows["基盤チーム"]["n"] == 1.5
    assert rows["SREチーム"]["n"] == 0.5
    # 費用・需要の縦合計が全体と一致
    total_n = sum(r["n"] for r in rows.values())
    total_api = sum(r["api"] for r in rows.values())
    total_seat = sum(r["seat_cost"] for r in rows.values())
    assert total_n == 2.0
    assert abs(total_api - float(result.users["api_cost_usd"].sum())) < 1e-6
    assert abs(total_seat - result.summary["seat_cost_now_usd"]) < 1e-6
    # a の需要 40 は基盤/SRE に 20 ずつ、b の 10 は基盤へ → 基盤 30 / SRE 20
    assert abs(rows["SREチーム"]["api"] - 20.0) < 1e-6


def test_fullwidth_semicolon_parsed(make_input, cfg):
    """全角セミコロン ； でも同様に分割される。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,チーム,職種,備考\na@example.com,基盤チーム；SREチーム,,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert result.users.set_index("email").loc["a@example.com", "team"] == "基盤チーム; SREチーム"
    rows = {r["group"]: r for r in _group_summary_rows(result.users, result.summary, "team")}
    assert rows["基盤チーム"]["n"] == 0.5
    assert rows["SREチーム"]["n"] == 0.5


def test_single_and_empty_affiliation_unchanged(make_input, cfg):
    """単一所属・空欄のユーザは重み1・（未設定）で従来どおり。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0),
                     spend_row("c@example.com", 5.0)]},
        members=["a@example.com,Standard", "c@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,チーム,職種,備考\na@example.com,基盤チーム,,\n",  # c は未設定
    )
    result = analyze(input_dir, "2026-06", cfg)
    rows = {r["group"]: r for r in _group_summary_rows(result.users, result.summary, "team")}
    assert rows["基盤チーム"]["n"] == 1.0
    assert rows["（未設定）"]["n"] == 1.0
    # （未設定）は常に最後
    assert list(r["group"] for r in _group_summary_rows(result.users, result.summary, "team"))[-1] == "（未設定）"


def test_markdown_escapes_pipe_and_newline(make_input, cfg, tmp_path):
    """note にパイプ・改行、チーム名にパイプが含まれても表・箇条書きが崩れない。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,チーム,職種,備考\n"
        'a@example.com,"基盤|チーム",,"1行目|注記\n2行目"\n',
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    text = out.read_text(encoding="utf-8")
    # パイプはエスケープされ、生の | として表・箇条書きに残らない
    assert "基盤\\|チーム" in text
    assert "1行目\\|注記" in text
    # 改行は <br> に置換され、備考の箇条書きが1行に収まる
    assert "<br>2行目" in text
    # 表の各行のセル数（区切り | の数）が揃っている＝崩れていないことの確認
    for line in text.splitlines():
        if line.startswith("| a@example.com |"):
            assert line.count("|") - line.count("\\|") >= 2


def test_dual_team_display_and_fraction_in_md(make_input, cfg, tmp_path):
    """ユーザ表に A; B 形式で表示され、サマリに端数人数（0.5 名）が出る。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@example.com", 10.0)]},
        members=["a@example.com,Standard"],
    )
    _write_info(
        input_dir, None,
        "email,チーム,職種,備考\na@example.com,基盤チーム; SREチーム,,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    text = out.read_text(encoding="utf-8")
    assert "基盤チーム; SREチーム" in text  # ユーザ表の表示
    assert "0.5 名" in text  # 按分後の端数人数
