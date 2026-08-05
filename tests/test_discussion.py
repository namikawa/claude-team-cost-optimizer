"""考察の自動執筆（discuss / analyze --with-discussion）のテスト。

実際の Claude CLI は呼ばない。ヘッドレス呼び出しは runner 差し替えか
subprocess.run の monkeypatch で置き換える。
"""

import subprocess
from pathlib import Path

import pytest

from seat_analyzer import discussion, report
from seat_analyzer.cli import main
from seat_analyzer.config import load_config

from .conftest import REPO_ROOT, spend_row

CONFIG = str(REPO_ROOT / "config.yaml")
BODY = "### 変更推奨の妥当性\n\n" + "対象組織の需要は妥当な範囲に収まっている。" * 12


@pytest.fixture
def two_orgs(make_input):
    """org-a（対象）と org-b（他組織）の2組織構成。"""
    input_dir = make_input(
        {"2026-05": [spend_row("alice.morgan@x.jp", 10.0)],
         "2026-06": [spend_row("alice.morgan@x.jp", 12.0)]},
        members=["alice.morgan@x.jp,Premium"], org="org-a",
    )
    make_input(
        {"2026-06": [spend_row("bernard.holloway@y.jp", 300.0, net=250.0)]},
        members=["bernard.holloway@y.jp,Standard"], org="org-b",
    )
    return input_dir


def _analyze(input_dir: Path, tmp_path: Path, *extra: str) -> Path:
    output_dir = tmp_path / "reports"
    rc = main(["analyze", "--config", CONFIG, "--input-dir", str(input_dir),
               "--output-dir", str(output_dir), "--month", "2026-06", *extra])
    assert rc == 0
    return output_dir


# ---------------------------------------------------------------- report.py 側のヘルパ


def test_document_body_and_discussion_body(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
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
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    report.write_discussion(path, BODY)
    _analyze(two_orgs, tmp_path)  # 同じ output_dir へ再生成
    assert report.discussion_body(path.read_text(encoding="utf-8")) == BODY.strip()


def test_write_discussion_requires_section(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("# タイトル\n\n本文\n", encoding="utf-8")
    with pytest.raises(ValueError, match="考察"):
        report.write_discussion(path, BODY)


# ---------------------------------------------------------------- 混入チェック


def test_forbidden_terms_excludes_target_org(two_orgs, tmp_path):
    cfg = load_config(CONFIG)
    terms = discussion.forbidden_terms(
        input_dir=two_orgs, output_dir=tmp_path / "reports", target_org="org-a", cfg=cfg)
    assert "org-b" in terms
    assert "bernard.holloway@y.jp" in terms
    assert "bernard" in terms and "holloway" in terms
    # 対象組織自身の語は禁止語に入らない
    assert "org-a" not in terms
    assert not any("alice" in t for t in terms)


def test_find_leaks_flags_only_terms_absent_from_source():
    terms = ("org-b", "bernard.holloway@y.jp", "holloway", "架空推進3部")
    source = "org-a のユーザ alice.morgan@x.jp は Premium。"

    assert discussion.find_leaks(
        "holloway さんは Standard で足りている。", terms, source=source) == ("holloway",)
    assert discussion.find_leaks(
        "架空推進3部の削減余地は小さい。", terms, source=source) == ("架空推進3部",)
    assert discussion.find_leaks("alice.morgan の需要は妥当。", terms, source=source) == ()


def test_find_leaks_ignores_terms_present_in_source():
    # 対象組織の資料に現れる語は、出力に出てきても混入ではない
    terms = ("holloway",)
    source = "holloway@x.jp は対象組織のユーザ。"
    assert discussion.find_leaks("holloway は Premium 継続。", terms, source=source) == ()


def test_find_leaks_respects_word_boundaries():
    # 英単語の一部として現れる出現は拾わない（detail の中の etai 等の誤検出防止）
    assert discussion.find_leaks("detail を確認する。", ("etai",), source="") == ()
    # メールのローカル部・ドメインの構成要素として現れる出現は拾う
    assert discussion.find_leaks("bernard.holloway", ("bernard",), source="") == ("bernard",)
    assert discussion.find_leaks("holloway@y.jp", ("holloway",), source="") == ("holloway",)
    # 日本語が隣接する出現は拾う
    assert discussion.find_leaks("org-b組織では", ("org-b",), source="") == ("org-b",)


def test_group_names_from_members_info(two_orgs, tmp_path):
    (two_orgs / "org-b" / "members-info.csv").write_text(
        "email,部署,チーム,職種\nbernard.holloway@y.jp,架空推進3部,Nebula-AI,エンジニア\n",
        encoding="utf-8")
    cfg = load_config(CONFIG)
    terms = discussion.forbidden_terms(
        input_dir=two_orgs, output_dir=tmp_path / "reports", target_org="org-a", cfg=cfg)
    assert "架空推進3部" in terms and "Nebula-AI" in terms
    # 職種は一般語のため禁止語に含めない
    assert "エンジニア" not in terms


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
    out = _analyze(two_orgs, tmp_path)
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
    out = _analyze(two_orgs, tmp_path)
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
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    runner = _runner(leaky, BODY)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "written"
    assert outcome.attempts == 2
    # 2回目のプロンプトには差し戻し指示と検出語が入る
    assert "差し戻し" in runner.calls[1]
    assert "bernard" in runner.calls[1]
    assert "bernard" not in outcome.path.read_text(encoding="utf-8")


def test_generate_blocks_persistent_leak(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    runner = _runner(leaky, leaky)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "blocked"
    assert outcome.attempts == 2
    assert "bernard.holloway" in outcome.leaks and "holloway" in outcome.leaks
    # 書き込まれていない（プレースホルダのまま）
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) is None


def test_generate_dry_run_does_not_call_claude(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner, dry_run=True)

    assert outcome.status == "dry-run"
    assert not runner.calls
    assert "執筆の原則" in outcome.prompt
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) is None


def test_generate_retries_transient_failure(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
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
    out = _analyze(two_orgs, tmp_path)
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
    out = _analyze(two_orgs, tmp_path, "--preview", "--days", "10")
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

    _analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    _generate(two_orgs, out, runner)
    assert "前月の所見" in runner.calls[0]
    assert "2026-05" in runner.calls[0]


# ---------------------------------------------------------------- run_claude のガード


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


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
    s = discussion.settings(load_config(CONFIG))
    assert discussion.run_claude("prompt", s) == BODY.strip()

    cmd = captured["cmd"]
    assert cmd[0] == "claude" and "-p" in cmd
    # プロジェクトの CLAUDE.md・hooks・MCP を読み込ませない
    assert "--safe-mode" in cmd
    # ツールを与えない（ファイル・Web 読み取りが混入経路になるため）
    for tool in ("Read", "Bash", "WebFetch"):
        assert tool in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "xhigh"
    # 空の作業ディレクトリで実行する（リポジトリを起点にさせない）
    assert Path(captured["kwargs"]["cwd"]).name.startswith("seat-analyzer-discuss-")


def test_run_claude_strips_code_fence(stub_claude):
    stub_claude(stdout="```markdown\n" + BODY + "\n```\n")
    s = discussion.settings(load_config(CONFIG))
    assert discussion.run_claude("prompt", s) == BODY.strip()


@pytest.mark.parametrize("proc_kw, message", [
    ({"stdout": ""}, "空"),
    ({"stdout": "API Error: 529 Overloaded."}, "API エラー"),
    ({"stdout": "### 考察\n\n短い。"}, "短すぎます"),
    ({"stdout": "", "stderr": "boom", "returncode": 1}, "異常終了"),
])
def test_run_claude_rejects_bad_output(stub_claude, proc_kw, message):
    stub_claude(**proc_kw)
    s = discussion.settings(load_config(CONFIG))
    with pytest.raises(discussion.DiscussionError, match=message):
        discussion.run_claude("prompt", s)


def test_run_claude_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(discussion.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(discussion.subprocess, "run", fake_run)
    s = discussion.settings(load_config(CONFIG))
    with pytest.raises(discussion.DiscussionError, match="応答しませんでした"):
        discussion.run_claude("prompt", s)


def test_run_claude_missing_command(monkeypatch):
    monkeypatch.setattr(discussion.shutil, "which", lambda _: None)
    s = dict(discussion.settings(load_config(CONFIG)), command="claude-not-installed")
    with pytest.raises(discussion.DiscussionError, match="見つかりません"):
        discussion.run_claude("prompt", s)


# ---------------------------------------------------------------- CLI


def test_cli_discuss_writes_all_orgs(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 0
    for org in ("org-a", "org-b"):
        md = (out / org / "2026-06" / "report.md").read_text(encoding="utf-8")
        assert report.discussion_body(md) == BODY.strip()


def test_cli_discuss_dry_run_prints_prompt(two_orgs, tmp_path, capsys):
    out = _analyze(two_orgs, tmp_path)
    capsys.readouterr()
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a", "--dry-run"])
    assert rc == 0
    assert "執筆の原則" in capsys.readouterr().out
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None


def test_cli_discuss_blocked_returns_nonzero(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べる。" * 8
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"])
    assert rc == 1
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None


def test_cli_discuss_failure_does_not_stop_other_orgs(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    # org-a のレポートだけ消して失敗させる
    (out / "org-a" / "2026-06" / "report.md").unlink()
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 1
    md = (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) == BODY.strip()


def test_cli_analyze_with_discussion(two_orgs, tmp_path, monkeypatch):
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    out = _analyze(two_orgs, tmp_path, "--with-discussion")
    for org in ("org-a", "org-b"):
        md = (out / org / "2026-06" / "report.md").read_text(encoding="utf-8")
        assert report.discussion_body(md) == BODY.strip()


def test_cli_analyze_without_discussion_leaves_placeholder(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None
    assert "未記入" in md
