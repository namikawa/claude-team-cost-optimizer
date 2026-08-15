"""考察の自動執筆（discuss / analyze --with-discussion）のテスト。

実際の Claude CLI は呼ばない。ヘッドレス呼び出しは runner 差し替えか
subprocess.run の monkeypatch で置き換える。
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from seat_analyzer import cli, discussion, leakcheck, report
from seat_analyzer.report import document
from seat_analyzer.cli import main
from seat_analyzer.config import load_config

from .conftest import CONFIG, hit_terms, requires_posix_permissions, run_analyze

BODY = "### 変更推奨の妥当性\n\n" + "対象組織の需要は妥当な範囲に収まっている。" * 12


# ---------------------------------------------------------------- report.py 側のヘルパ


def test_document_body_and_discussion_body(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    md = path.read_text(encoding="utf-8")

    # 生成直後は未記入プレースホルダなので考察は無い扱い
    assert report.discussion_body(md) is None
    body = report.document_body(md)
    assert "## 考察" not in body
    assert "## データ検証・警告" in body

    report.write_discussion(path, BODY)
    md2 = path.read_text(encoding="utf-8")
    assert report.discussion_body(md2) == BODY.strip()
    # 本文側は変わらない
    assert report.document_body(md2) == body


def test_written_discussion_survives_reanalysis(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    report.write_discussion(path, BODY)
    run_analyze(two_orgs, tmp_path)  # 同じ output_dir へ再生成
    assert report.discussion_body(path.read_text(encoding="utf-8")) == BODY.strip()


def test_write_discussion_requires_section(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("# タイトル\n\n本文\n", encoding="utf-8")
    with pytest.raises(ValueError, match="考察"):
        report.write_discussion(path, BODY)


# ---------------------------------------------------------------- generate()


def _runner(*bodies: str):
    """呼び出しごとに bodies を順に返す偽 runner。プロンプトも記録する。"""
    calls: list[str] = []

    def run(prompt: str, s: dict) -> str:
        calls.append(prompt)
        return bodies[min(len(calls) - 1, len(bodies) - 1)]

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _generate(two_orgs: Path, out: Path, runner, **kw):
    return discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG), runner=runner, **kw)


def test_generate_writes_discussion(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "written"
    assert outcome.attempts == 1
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) == BODY.strip()
    # プロンプトには対象組織の資料と執筆の原則が入り、他組織の語は入らない
    prompt = runner.calls[0]
    assert "alice.morgan" in prompt and "org-a" in prompt
    assert "bernard" not in prompt and "org-b" not in prompt
    assert "執筆の原則" in prompt and "変更推奨の妥当性" in prompt


def test_generate_keeps_existing_unless_forced(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    report.write_discussion(out / "org-a" / "2026-06" / "report.md", BODY)

    runner = _runner("### 別の考察\n\n" + "上書きされた本文。" * 20)
    kept = _generate(two_orgs, out, runner)
    assert kept.status == "kept"
    assert not runner.calls  # Claude を呼ばない
    assert report.discussion_body(kept.path.read_text(encoding="utf-8")) == BODY.strip()

    forced = _generate(two_orgs, out, runner, force=True)
    assert forced.status == "written"
    assert "上書きされた本文" in forced.path.read_text(encoding="utf-8")


def test_generate_retries_once_then_accepts(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    runner = _runner(leaky, BODY)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "written"
    assert outcome.attempts == 2
    # 2回目のプロンプトには差し戻し指示と検出語が入る
    assert "差し戻し" in runner.calls[1]
    assert "bernard" in runner.calls[1]
    assert "bernard" not in outcome.path.read_text(encoding="utf-8")


def test_generate_reports_leaks_even_when_rewrite_succeeds(two_orgs, tmp_path):
    """書き直しで解消しても検出語を運用者に残す。

    残さないと、誤検出なら「正当な記述が静かに削られた」ことに気づけず、真の混入なら
    「モデルが他組織名を出そうとした」という兆候の記録が消える。
    """
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    notices: list[str] = []
    outcome = discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        runner=_runner(leaky, BODY), notify=notices.append)

    assert outcome.status == "written"
    joined = "\n".join(notices)
    assert "混入を検出" in joined
    assert "bernard.holloway" in joined
    assert "他組織の bernard.holloway と比べる" in joined  # 一致箇所の文脈も残す


def test_config_allow_terms_apply_to_all_orgs(two_orgs, tmp_path, monkeypatch):
    """config の allow_terms は全組織実行でも効く（--allow-term は単一組織限定のため）。"""
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "holloway 相当の水準にある。" * 10
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)

    import yaml
    cfg = yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8"))
    cfg["discussion"] = {**cfg["discussion"],
                         "allow_terms": ["holloway", "bernard.holloway"]}
    path = tmp_path / "config-allow.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    rc = main(["discuss", "--config", str(path), "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 0


def test_generate_blocks_persistent_leak(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    runner = _runner(leaky, leaky)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "blocked"
    assert outcome.attempts == 2
    leaked = hit_terms(outcome.leaks)
    assert "bernard.holloway" in leaked and "holloway" in leaked
    # 検出語には一致箇所の文脈が付く（誤検出かどうかを人が判断するため）
    assert all(h.context for h in outcome.leaks)
    # 書き込まれていない（プレースホルダのまま）
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) is None


def test_generate_dry_run_does_not_call_claude(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner, dry_run=True)

    assert outcome.status == "dry-run"
    assert not runner.calls
    assert "執筆の原則" in outcome.prompt
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) is None


def test_details_is_passed_as_material(two_orgs, tmp_path):
    """資料は report.md 本文 → details.md → recommendations.csv の順で渡る。

    report.md は考察中心の短い文書になったので、ユーザ単位の表は details.md が
    資料として補う。照合元（混入チェック）にも同じ本文が入る。
    """
    out = run_analyze(two_orgs, tmp_path)
    org_output = out / "org-a"
    details = (org_output / "2026-06" / "details.md").read_text(encoding="utf-8")

    prompt = _generate(two_orgs, out, _runner(BODY), dry_run=True).prompt
    assert "資料2: 分析詳細資料 details.md（2026-06）" in prompt
    assert "資料3: ユーザ別推奨一覧 recommendations.csv（2026-06）" in prompt
    assert "## 全ユーザ" in prompt          # report.md 本体には無い表が資料に入る
    assert details.strip() in prompt

    materials, source = discussion.collect_materials(
        org_output=org_output, month="2026-06", preview=False)
    assert [t for t, _ in materials] == [
        "資料1: 分析レポート本文（2026-06）",
        "資料2: 分析詳細資料 details.md（2026-06）",
        "資料3: ユーザ別推奨一覧 recommendations.csv（2026-06）",
    ]
    assert details in source


def test_missing_details_falls_back_to_the_previous_material_set(two_orgs, tmp_path):
    """details.md が無い月（このステップ以前のレポート）は資料2を省略して動く。"""
    out = run_analyze(two_orgs, tmp_path)
    (out / "org-a" / "2026-06" / "details.md").unlink()

    prompt = _generate(two_orgs, out, _runner(BODY), dry_run=True).prompt
    assert "details.md" not in prompt
    assert "資料2: ユーザ別推奨一覧 recommendations.csv（2026-06）" in prompt

    outcome = _generate(two_orgs, out, _runner(BODY))
    assert outcome.status == "written"


def test_generate_dry_run_shows_prompt_even_when_already_written(two_orgs, tmp_path):
    """--dry-run はプロンプト確認用なので、記入済みでもプロンプトを返す。"""
    out = run_analyze(two_orgs, tmp_path)
    report.write_discussion(out / "org-a" / "2026-06" / "report.md", BODY)
    outcome = _generate(two_orgs, out, _runner(BODY), dry_run=True)
    assert outcome.status == "dry-run"
    assert "執筆の原則" in outcome.prompt


def test_generate_does_not_overwrite_discussion_written_during_generation(two_orgs, tmp_path):
    """生成中に人が考察を書いた場合は上書きしない（判定と書き込みの間の競合）。"""
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    handwritten = "### 手書きの考察\n\n" + "人が書いた内容。" * 20

    def writes_meanwhile(prompt: str, s: dict) -> str:
        report.write_discussion(path, handwritten)
        return BODY

    notices: list[str] = []
    outcome = discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        runner=writes_meanwhile, notify=notices.append)

    assert outcome.status == "kept"
    assert report.discussion_body(path.read_text(encoding="utf-8")) == handwritten.strip()
    assert any("生成中" in n for n in notices)


def test_generate_retries_transient_failure(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    calls: list[str] = []
    notices: list[str] = []

    def flaky(prompt: str, s: dict) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            raise discussion.DiscussionError("API エラー: 529 Overloaded", transient=True)
        return BODY

    cfg = load_config(CONFIG)
    cfg["discussion"] = dict(cfg["discussion"], retry_wait_seconds=0)
    outcome = discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=cfg, runner=flaky, notify=notices.append)

    assert outcome.status == "written"
    assert len(calls) == 2
    assert any("再試行" in n for n in notices)


def test_generate_does_not_retry_permanent_failure(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path)
    calls: list[str] = []

    def broken(prompt: str, s: dict) -> str:
        calls.append(prompt)
        raise discussion.DiscussionError("claude が見つかりません")

    with pytest.raises(discussion.DiscussionError, match="見つかりません"):
        _generate(two_orgs, out, broken)
    assert len(calls) == 1


def test_generate_requires_report(two_orgs, tmp_path):
    out = tmp_path / "reports"
    with pytest.raises(discussion.DiscussionError, match="analyze"):
        _generate(two_orgs, out, _runner(BODY))


def test_preview_materials_use_preview_document(two_orgs, tmp_path):
    out = run_analyze(two_orgs, tmp_path, "--preview", "--days", "10")
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner, preview=True)

    assert outcome.status == "written"
    assert outcome.path.name == "preview.md"
    assert "速報" in runner.calls[0]


def test_previous_month_discussion_is_included(two_orgs, tmp_path):
    out = tmp_path / "reports"
    main(["analyze", "--config", CONFIG, "--input-dir", str(two_orgs),
          "--output-dir", str(out), "--month", "2026-05", "--org", "org-a"])
    prev = "### 前月の所見\n\n" + "前月は Premium 継続と判断した。" * 10
    report.write_discussion(out / "org-a" / "2026-05" / "report.md", prev)

    run_analyze(two_orgs, tmp_path)

    # 既定では前月の考察を渡さない（検証できない人手の文書のため）
    default_runner = _runner(BODY)
    _generate(two_orgs, out, default_runner)
    assert "前月の所見" not in default_runner.calls[0]

    # 明示的に有効化したときだけ渡す
    runner = _runner(BODY)
    _generate(two_orgs, out, runner, include_previous=True, force=True)
    assert "前月の所見" in runner.calls[0]
    assert "2026-05" in runner.calls[0]


def test_previous_month_discussion_with_leak_is_excluded(two_orgs, tmp_path):
    """前月の考察に他組織の語があれば資料に含めない（過去の混入を引き写す経路を塞ぐ）。"""
    out = tmp_path / "reports"
    main(["analyze", "--config", CONFIG, "--input-dir", str(two_orgs),
          "--output-dir", str(out), "--month", "2026-05", "--org", "org-a"])
    report.write_discussion(
        out / "org-a" / "2026-05" / "report.md",
        "### 前月の所見\n\n" + "org-b の bernard.holloway と比べると小さい。" * 6)

    run_analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    notices: list[str] = []
    discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        include_previous=True, runner=runner, notify=notices.append)

    assert "前月の所見" not in runner.calls[0]
    assert "bernard" not in runner.calls[0]
    assert any("前月" in n and "除外" in n for n in notices)


# ---------------------------------------------------------------- run_claude のガード


def _fake_proc(stdout: str | bytes = "", stderr: str | bytes = "", returncode: int = 0):
    """subprocess.run の戻り値を模す。

    run_claude はバイト列で受け取って自分でデコードするため、str で渡された分は
    UTF-8 で符号化する（バイト列をそのまま渡せば壊れた出力も表現できる）。
    """
    def raw(value: str | bytes) -> bytes:
        return value.encode("utf-8") if isinstance(value, str) else value

    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=raw(stdout), stderr=raw(stderr))


@pytest.fixture
def stub_claude(monkeypatch):
    """subprocess.run を差し替えて claude の出力を偽装する。"""
    captured: dict = {}

    def install(**proc_kw):
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _fake_proc(**proc_kw)

        monkeypatch.setattr(discussion.shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(discussion.subprocess, "run", fake_run)
        return captured

    return install


def test_run_claude_returns_body_and_isolates_context(stub_claude):
    captured = stub_claude(stdout=BODY + "\n")
    s = load_config(CONFIG)["discussion"]
    assert discussion.run_claude("prompt", s) == BODY.strip()

    cmd = captured["cmd"]
    # which() が解決した実体を起動する（Windows で claude.cmd を名前だけでは起動できない）
    assert Path(cmd[0]).name in ("claude", "claude.exe", "claude.cmd") and "-p" in cmd
    # プロジェクトの CLAUDE.md・hooks・MCP を読み込ませない
    assert "--safe-mode" in cmd
    # 組み込みツールを空集合にする許可リストが主たる保証
    assert cmd[cmd.index("--tools") + 1] == ""
    # denylist は追加防御として残す
    for tool in ("Read", "Bash", "WebFetch"):
        assert tool in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "xhigh"
    # 空の作業ディレクトリで実行する（リポジトリを起点にさせない）
    assert Path(captured["kwargs"]["cwd"]).name.startswith("seat-analyzer-discuss-")


def test_run_claude_uses_utf8_for_subprocess_io(stub_claude):
    """プロンプトを UTF-8 のバイト列で渡す。

    text=True の既定はロケールの文字コードで、日本語 Windows（cp932）では
    プロンプトに含まれるレポート由来の em dash・⚠️ を送れずに落ちる。text=True に
    encoding を足すだけでは足りない（Windows は出力のデコードを別スレッドで行い、
    失敗が呼び出し元へ伝播しない）ので、変換は自分で行う。
    """
    captured = stub_claude(stdout=BODY + "\n")
    s = load_config(CONFIG)["discussion"]
    prompt = "見出し — ⚠️"
    discussion.run_claude(prompt, s)

    assert captured["kwargs"]["input"] == prompt.encode("utf-8")
    # テキストモードに任せない（ロケール依存とスレッド内デコードの両方を避ける）
    assert not captured["kwargs"].get("text")
    assert "encoding" not in captured["kwargs"]


def test_run_claude_normalizes_crlf_in_output(stub_claude):
    """CRLF の出力を LF に揃える。

    text=True の universal newlines をやめた代わり。レポートの改行は LF 固定で、
    write_text(newline="\\n") は文字列の中の CR をそのまま書くため、ここで落とさないと
    考察セクションだけが CRLF になる（Windows の claude.cmd は CRLF を出しうる）。
    """
    body = "### 見出し\r\n\r\n" + "本文。" * 80   # min_output_chars を超える長さにする
    stub_claude(stdout=body + "\r\n")

    out = discussion.run_claude("prompt", load_config(CONFIG)["discussion"])

    assert "\r" not in out
    assert "### 見出し\n\n" in out


def test_run_claude_aborts_when_output_is_not_utf8(stub_claude):
    """UTF-8 として壊れた出力は、置換して読み進めず中止する。

    replace で読むと他組織名が U+FFFD へ化けて混入チェックに一致しなくなり、
    長さ・見出しの検査を通ってレポートへ書き込まれてしまう。
    「出力が空」（transient）に化けて生成をやり直すことも避ける。
    """
    stub_claude(stdout=b"\xff\xfe broken")

    with pytest.raises(discussion.DiscussionError, match="UTF-8") as e:
        discussion.run_claude("prompt", load_config(CONFIG)["discussion"])
    # 同じ入力の再実行では直らない（CLI が非 UTF-8 を出す設定）
    assert e.value.transient is False


def test_unicode_failure_is_not_retried(monkeypatch):
    """UTF-8 の入出力エラーで CLI を呼び直さない（待ち時間と API 消費を増やさない）。"""
    calls: list[int] = []

    def fake_run(cmd, **kwargs):
        calls.append(1)
        return _fake_proc(stdout=b"\xff\xfe broken")

    monkeypatch.setattr(discussion.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(discussion.subprocess, "run", fake_run)
    monkeypatch.setattr(discussion.time, "sleep", lambda _: None)
    s = load_config(CONFIG)["discussion"]
    assert int(s["retries"]) > 0, "リトライ設定が 0 ならこのテストは何も保証しない"

    with pytest.raises(discussion.DiscussionError):
        discussion._call_with_retry(discussion.run_claude, "prompt", s, lambda _m: None)

    assert len(calls) == 1


def test_discuss_permission_error_shows_hint(two_orgs, tmp_path, monkeypatch, capsys):
    """組織ごとに例外を握る経路でも、開いているファイルの案内を出す。"""
    out = run_analyze(two_orgs, tmp_path)

    def boom(**kwargs):
        raise PermissionError(32, "プロセスはファイルにアクセスできません")

    monkeypatch.setattr(discussion, "generate", boom)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ヒント" in err and "Excel" in err


def test_run_claude_uses_resolved_executable_path(monkeypatch):
    """which() が解決した実体を起動する。

    Windows の CreateProcess は拡張子を補うとき .exe しか試さないため、npm 版の
    claude.cmd は名前だけでは起動できない（which() は PATHEXT を見るので見つかり、
    存在確認のガードだけ通って実行で落ちる）。
    """
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(stdout=BODY + "\n")

    monkeypatch.setattr(discussion.shutil, "which", lambda _: r"C:\npm\claude.cmd")
    monkeypatch.setattr(discussion.subprocess, "run", fake_run)
    discussion.run_claude("prompt", load_config(CONFIG)["discussion"])

    assert captured["cmd"][0] == r"C:\npm\claude.cmd"


def test_run_claude_strips_code_fence(stub_claude):
    stub_claude(stdout="```markdown\n" + BODY + "\n```\n")
    s = load_config(CONFIG)["discussion"]
    assert discussion.run_claude("prompt", s) == BODY.strip()


@pytest.mark.parametrize("stdout", [
    "## 考察\n\n{body}",                        # 先頭にある場合
    "以下が考察です。\n\n## 考察\n\n{body}",      # 前置きの後にある場合
    "##考察\n{body}",                           # 見出し記号の後に空白がない場合
    "## 考察 \n{body}",                         # 行末に空白がある場合
    "```markdown\n## 考察\n\n{body}\n```",      # コードフェンスと併用
])
def test_run_claude_strips_discussion_heading(stub_claude, stdout):
    """出力に「## 考察」が付いてきても差し込み時に H2 が重複しないよう落とす。"""
    stub_claude(stdout=stdout.format(body=BODY))
    s = load_config(CONFIG)["discussion"]
    result = discussion.run_claude("prompt", s)
    assert result.endswith(BODY.strip())
    for heading in ("## 考察", "##考察"):
        assert heading not in result


def test_run_claude_strips_every_discussion_heading(stub_claude):
    """1つの出力に複数の考察見出しがあってもすべて落とす。"""
    stub_claude(stdout=f"## 考察\n\n{BODY}\n\n## 考察\n\n{BODY}")
    s = load_config(CONFIG)["discussion"]
    result = discussion.run_claude("prompt", s)
    assert "## 考察" not in result
    assert result.count("### 変更推奨の妥当性") == 2


@pytest.mark.parametrize("proc_kw, message", [
    ({"stdout": ""}, "空"),
    ({"stdout": "API Error: 529 Overloaded."}, "API エラー"),
    ({"stdout": "### 考察\n\n短い。"}, "短すぎます"),
    ({"stdout": "", "stderr": "boom", "returncode": 1}, "異常終了"),
])
def test_run_claude_rejects_bad_output(stub_claude, proc_kw, message):
    stub_claude(**proc_kw)
    s = load_config(CONFIG)["discussion"]
    with pytest.raises(discussion.DiscussionError, match=message):
        discussion.run_claude("prompt", s)


def test_run_claude_rejects_api_error_after_body(stub_claude):
    """API エラーは出力の先頭に限らない（ストリーミング途中で失敗すると本文の後に付く）。"""
    stub_claude(stdout=BODY + "\nAPI Error: 500 Internal Server Error")
    s = load_config(CONFIG)["discussion"]
    with pytest.raises(discussion.DiscussionError, match="API エラー") as exc:
        discussion.run_claude("prompt", s)
    assert exc.value.transient is True


@pytest.mark.parametrize("stdout, message", [
    # 前置き + 捏造されたツール呼び出し + 本文。長さの門は通ってしまう
    ("承知しました。以下が考察です。\n\n<function_calls>\n<invoke name=\"Read\">\n"
     "</function_calls>\n\n" + BODY, "ツール呼び出し"),
    # 小見出しが無い（拒否文・前置きだけ・途中で切れた出力）
    ("申し訳ありませんが、この依頼にはお答えできません。" * 20, "小見出し"),
])
def test_run_claude_rejects_malformed_output(stub_claude, stdout, message):
    """長さだけでは弾けない「形の崩れた出力」を肯定的な検査で落とす。"""
    stub_claude(stdout=stdout)
    s = load_config(CONFIG)["discussion"]
    with pytest.raises(discussion.DiscussionError, match=message) as exc:
        discussion.run_claude("prompt", s)
    assert exc.value.transient is True  # 再試行で救えるため


def test_run_claude_isolates_mcp_and_session(stub_claude):
    """MCP サーバも二重防御で遮断し、レポート全文をトランスクリプトに残さない。"""
    captured = stub_claude(stdout=BODY)
    discussion.run_claude("prompt", load_config(CONFIG)["discussion"])
    assert "--strict-mcp-config" in captured["cmd"]
    assert "--no-session-persistence" in captured["cmd"]


@pytest.mark.parametrize("proc_kw, transient", [
    ({"stdout": "API Error: 529 Overloaded."}, True),      # 5xx は再実行で解消しうる
    ({"stdout": "API Error: 429 Rate limited."}, True),
    ({"stdout": "API Error: 401 Unauthorized."}, False),   # 認証は再試行しても同じ
    ({"stdout": "API Error: 400 Bad request."}, False),
    ({"stdout": "API Error: connection reset"}, True),     # 状態不明なら一時扱い
    # claude -p は API エラーでも 0 を返すため、非ゼロ終了は使い方・設定の誤りとみなす
    ({"stdout": "", "stderr": "unknown option", "returncode": 2}, False),
])
def test_run_claude_transient_classification(stub_claude, proc_kw, transient):
    stub_claude(**proc_kw)
    s = load_config(CONFIG)["discussion"]
    with pytest.raises(discussion.DiscussionError) as exc:
        discussion.run_claude("prompt", s)
    assert exc.value.transient is transient


def test_run_claude_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(discussion.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(discussion.subprocess, "run", fake_run)
    s = load_config(CONFIG)["discussion"]
    with pytest.raises(discussion.DiscussionError, match="応答しませんでした"):
        discussion.run_claude("prompt", s)


def test_run_claude_missing_command(monkeypatch):
    monkeypatch.setattr(discussion.shutil, "which", lambda _: None)
    s = dict(load_config(CONFIG)["discussion"], command="claude-not-installed")
    with pytest.raises(discussion.DiscussionError, match="見つかりません"):
        discussion.run_claude("prompt", s)


# ---------------------------------------------------------------- CLI


@pytest.mark.parametrize("exc", [
    leakcheck.LeakCheckError("照合を続行できません"),
    discussion.DiscussionError("考察を生成できません"),
])
def test_cli_reports_both_error_kinds_identically(exc, tmp_path, monkeypatch, capsys):
    """照合エンジンと考察生成で例外クラスが分かれても、ユーザから見える結果は同じ。

    どちらも traceback を出さず「エラー: <本文>」を stderr へ出して終了コード 1 になる。
    例外の分離は内部の整理であって、CLI の振る舞いを変えるものではない。
    どのサブコマンドでも main() の同じ except に合流するため、代表として discuss で見る。
    """
    def boom(_path):
        raise exc

    monkeypatch.setattr(cli, "load_config", boom)
    capsys.readouterr()
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(tmp_path)])
    assert rc == 1
    assert capsys.readouterr().err.strip() == f"エラー: {exc}"


def test_cli_discuss_writes_all_orgs(two_orgs, tmp_path, monkeypatch):
    out = run_analyze(two_orgs, tmp_path)
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 0
    for org in ("org-a", "org-b"):
        md = (out / org / "2026-06" / "report.md").read_text(encoding="utf-8")
        assert report.discussion_body(md) == BODY.strip()


def test_cli_discuss_dry_run_prints_prompt(two_orgs, tmp_path, capsys):
    out = run_analyze(two_orgs, tmp_path)
    capsys.readouterr()
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a", "--dry-run"])
    assert rc == 0
    assert "執筆の原則" in capsys.readouterr().out
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None


def test_cli_dry_run_keeps_stdout_to_prompt_only(two_orgs, tmp_path, capsys):
    """--dry-run の stdout はプロンプトだけに保つ（ファイルへ落として確認する使い方のため）。"""
    out = run_analyze(two_orgs, tmp_path)
    (out / "org-a" / "2026-06" / "report.md").unlink()  # スキップ通知を発生させる
    capsys.readouterr()
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "スキップした組織" in captured.err
    assert "スキップした組織" not in captured.out
    assert "執筆の原則" in captured.out


def test_cli_discuss_blocked_returns_nonzero(two_orgs, tmp_path, monkeypatch):
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べる。" * 8
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"])
    assert rc == 1
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None


@contextmanager
def _break_generation_for_org_a(two_orgs, monkeypatch):
    """org-a の考察生成だけを失敗させる（DiscussionError）。"""
    def fail_for_org_a(prompt: str, s: dict) -> str:
        if "org-a" in prompt:
            raise discussion.DiscussionError("claude が見つかりません")
        return BODY

    monkeypatch.setattr(discussion, "run_claude", fail_for_org_a)
    yield


@contextmanager
def _break_leakcheck_for_org_a(two_orgs, monkeypatch):
    """org-a の混入チェックだけを失敗させる（LeakCheckError）。

    禁止語は「対象組織以外」から集めるため、org-b の入力を読めなくすると
    止まるのは org-a の生成であって org-b 自身の生成ではない。
    """
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    victim = two_orgs / "org-b" / "spend"
    victim.chmod(0o000)
    try:
        yield
    finally:
        victim.chmod(0o755)


@pytest.mark.parametrize("break_org_a", [
    _break_generation_for_org_a,
    # 混入チェックの失敗は chmod で他組織を読めなくして起こす
    pytest.param(_break_leakcheck_for_org_a, marks=requires_posix_permissions),
])
def test_cli_discuss_failure_does_not_stop_other_orgs(
    break_org_a, two_orgs, tmp_path, monkeypatch,
):
    """1組織の失敗で他組織を止めない。考察生成の失敗と混入チェックの失敗の両方で成り立つ。"""
    out = run_analyze(two_orgs, tmp_path)
    with break_org_a(two_orgs, monkeypatch):
        rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
                   "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 1
    assert report.discussion_body(
        (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")) is None
    md = (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) == BODY.strip()


def test_cli_discuss_skips_orgs_without_report(two_orgs, tmp_path, monkeypatch, capsys):
    """レポートが無い組織はスキップする（組織ごとに spend の月がずれるため）。"""
    out = run_analyze(two_orgs, tmp_path)
    (out / "org-a" / "2026-06" / "report.md").unlink()
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    capsys.readouterr()
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 0
    assert "スキップした組織: org-a" in capsys.readouterr().out
    md = (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) == BODY.strip()


def test_cli_discuss_single_org_without_report_is_an_error(two_orgs, tmp_path, monkeypatch):
    """単一組織指定でレポートが無い場合は理由を示して失敗する（黙って何もしない、にしない）。"""
    out = run_analyze(two_orgs, tmp_path)
    (out / "org-a" / "2026-06" / "report.md").unlink()
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"])
    assert rc == 1


def test_cli_analyze_with_discussion(two_orgs, tmp_path, monkeypatch):
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    out = run_analyze(two_orgs, tmp_path, "--with-discussion")
    for org in ("org-a", "org-b"):
        md = (out / org / "2026-06" / "report.md").read_text(encoding="utf-8")
        assert report.discussion_body(md) == BODY.strip()


def test_cli_discuss_allow_term(two_orgs, tmp_path, monkeypatch):
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "holloway 相当の水準にある。" * 10
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    args = ["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
            "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"]
    assert main(args) == 1
    assert main([*args, "--allow-term", "holloway",
                 "--allow-term", "bernard.holloway"]) == 0


def test_cli_allow_term_requires_single_org(two_orgs, tmp_path, monkeypatch):
    """許可はその組織の生成物を人が確認した結果なので、全組織へ一括適用させない。"""
    out = run_analyze(two_orgs, tmp_path)
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--allow-term", "holloway"])
    assert rc == 1


def test_analyze_rejects_allow_term_before_running(two_orgs, tmp_path, capsys):
    """使い方の誤りは分析を走らせる前に落とす（全組織の分析を完走してから失敗させない）。"""
    out = tmp_path / "reports"
    capsys.readouterr()
    rc = main(["analyze", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06",
               "--with-discussion", "--allow-term", "holloway"])
    assert rc == 1
    # レポートを1つも作らずに落ちている
    assert not (out / "org-a" / "2026-06" / "report.md").exists()
    assert "分析結果" not in capsys.readouterr().out


def test_cli_blocked_output_includes_context(two_orgs, tmp_path, monkeypatch, capsys):
    out = run_analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べる。" * 8
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    capsys.readouterr()
    main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
          "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"])
    err = capsys.readouterr().err
    assert "検出語: bernard.holloway" in err
    assert "他組織の bernard.holloway と比べる" in err
    assert "--allow-term" in err


# ---------------------------------------------------------------- 入力検証


@pytest.mark.parametrize("month", [
    "../org-b/2026-06",       # 別組織のレポートへの逸脱
    "/etc/2026-06",
    "2026-13",
    "2026-6",
    "2026-06/../../x",
    "2026-06\n",              # 末尾改行（$ は許してしまう）
    "２０２６-06",              # 全角数字（\d は許してしまう）
])
def test_month_format_is_validated(two_orgs, tmp_path, month):
    """対象月は出力パスの一部になるため、形式外の値を受け付けない。"""
    out = run_analyze(two_orgs, tmp_path)
    for command in ("analyze", "discuss"):
        rc = main([command, "--config", CONFIG, "--input-dir", str(two_orgs),
                   "--output-dir", str(out), "--month", month, "--org", "org-a"])
        assert rc == 1


def test_traversal_month_does_not_touch_other_org(two_orgs, tmp_path, monkeypatch):
    out = run_analyze(two_orgs, tmp_path)
    before = (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8")
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "../org-b/2026-06", "--org", "org-a"])
    assert rc == 1
    assert (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8") == before


def test_write_discussion_only_if_unwritten_guard(two_orgs, tmp_path):
    """判定と置換を1回の読み取りに畳む（呼び出し側の事前確認より競合の窓が狭い）。"""
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"

    assert report.write_discussion(path, BODY, only_if_unwritten=True) is True
    # 記入済みになったので2回目は何もしない
    other = "### 別の考察\n\n" + "上書きされるべきではない。" * 20
    assert report.write_discussion(path, other, only_if_unwritten=True) is False
    assert report.discussion_body(path.read_text(encoding="utf-8")) == BODY.strip()
    # only_if_unwritten=False なら上書きする
    assert report.write_discussion(path, other) is True
    assert report.discussion_body(path.read_text(encoding="utf-8")) == other.strip()


def test_write_discussion_aborts_when_file_changes_before_replace(two_orgs, tmp_path,
                                                                  monkeypatch):
    """置換直前に内容が変わっていたら書き込まない（判定〜置換の窓を詰める）。"""
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    handwritten = "### 手書きの考察\n\n" + "人が書いた内容。" * 20

    original_chmod = document.os.chmod
    injected: list[int] = []

    def chmod_with_concurrent_write(target, mode):
        # 一時ファイルを作った後・置換直前の照合より前に、別プロセスの書き込みを模す
        if not injected:
            injected.append(1)
            path.write_text(_with_discussion(path, handwritten), encoding="utf-8")
        return original_chmod(target, mode)

    monkeypatch.setattr(document.os, "chmod", chmod_with_concurrent_write)
    assert report.write_discussion(path, BODY, only_if_unwritten=True) is False
    assert report.discussion_body(path.read_text(encoding="utf-8")) == handwritten.strip()
    assert not list(path.parent.glob("report.md.*.tmp"))


def _with_discussion(path: Path, body: str) -> str:
    md = path.read_text(encoding="utf-8")
    head = md.split("\n## 考察\n", 1)[0]
    return head + "\n## 考察\n\n" + body.strip() + "\n"


@requires_posix_permissions
def test_atomic_write_preserves_permissions(two_orgs, tmp_path):
    """一時ファイル経由の置換で元ファイルの権限を落とさない（共有用に緩めた権限を守る）。"""
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    path.chmod(0o644)
    report.write_discussion(path, BODY)
    assert path.stat().st_mode & 0o777 == 0o644


def test_new_report_permissions_match_other_outputs(two_orgs, tmp_path):
    """新規作成される report.md / preview.md も他の成果物と同じ権限になる。

    一時ファイルは mkstemp 由来で 0600 なので、新規作成時に umask 既定を適用しないと
    レポートだけ dashboard.html より狭い権限になる（共有できなくなる）。
    """
    out = run_analyze(two_orgs, tmp_path)
    d = out / "org-a" / "2026-06"
    reference = (d / "dashboard.html").stat().st_mode & 0o777
    assert (d / "report.md").stat().st_mode & 0o777 == reference

    pv = run_analyze(two_orgs, tmp_path / "pv", "--preview", "--days", "10")
    pvd = pv / "org-a" / "2026-06"
    assert (pvd / "preview.md").stat().st_mode & 0o777 == (
        pvd / "preview-dashboard.html").stat().st_mode & 0o777


def test_atomic_write_leaves_original_on_failure(two_orgs, tmp_path, monkeypatch):
    """書き込みが途中で失敗しても、レポート本体は元の内容が残る。"""
    out = run_analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(document.os, "replace", boom)
    with pytest.raises(OSError):
        report.write_discussion(path, BODY)
    assert path.read_text(encoding="utf-8") == before
    # 一時ファイルを残さない
    assert not list(path.parent.glob("report.md.*.tmp"))


@pytest.mark.parametrize("bad", [
    {"max_attempts": 1.9},
    {"retries": 1.9},
    {"min_output_chars": 100.5},
    {"retry_wait_seconds": float("inf")},
    {"timeout_seconds": float("nan")},
    {"timeout_seconds": 10 ** 4000},  # float 変換で OverflowError になる巨大整数
    {"max_attempts": 0},
    {"retries": -1},
    {"effort": "extreme"},
    {"command": ""},
])
def test_config_rejects_bad_discussion_settings(tmp_path, bad):
    import yaml
    cfg = yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8"))
    cfg["discussion"] = {**cfg["discussion"], **bad}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="discussion"):
        load_config(path)


def test_render_does_not_resubstitute_inserted_values(two_orgs, tmp_path):
    """挿入した値の中の {{KEY}} が再置換されない（組織名には波括弧が使える）。"""
    prompt = discussion.build_prompt(
        org="{{MATERIALS}}", scope="s", materials=[("資料1", "MATERIAL-MARKER")], preview=False)
    assert "{{MATERIALS}}" in prompt                 # ORG 位置にそのまま残る
    assert prompt.count("MATERIAL-MARKER") == 1      # 資料が二重に展開されない
