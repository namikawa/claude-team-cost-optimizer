"""launchd の雛形（examples/launchd/）の静的検査と、偽コマンドによる動作検査。

全 OS でファイル構成・plist の全キーと値・公開用のパスを調べ、zsh があれば構文も見る。
Windows 以外で zsh があれば、取得・通知・日付を偽物にして本体を実行する。
macOS では偽の launchctl と一時的な HOME、本物の plutil で登録・再登録・解除を見る。
実際の CSV 取得・通知・launchd の操作は行わない。
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

EXAMPLES = REPO_ROOT / "examples" / "launchd"
MODES = ("weekly", "monthly")
ZSH = shutil.which("zsh")
HAS_ZSH = pytest.mark.skipif(ZSH is None, reason="zsh が無い環境では検査しない")
RUN_SHELL = pytest.mark.skipif(
    ZSH is None or sys.platform == "win32",
    reason="zsh が無い環境と Windows では偽コマンドを実行しない")
MACOS = pytest.mark.skipif(
    ZSH is None or sys.platform != "darwin",
    reason="macOS と zsh が必要な登録・解除の検査")


def _expected(mode, home="__HOME__", workspace="__WORKSPACE__"):
    return {
        "Label": f"seat-analyzer.collect-{mode}",
        "ProgramArguments": [
            "/usr/bin/caffeinate", "-is", "/usr/bin/lockf", "-k",
            f"{home}/.seat-analyzer/scheduled/collect.lock", "/bin/zsh", "-f",
            f"{home}/.seat-analyzer/scheduled/collect.zsh", mode],
        "WorkingDirectory": workspace,
        "EnvironmentVariables": {
            "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "SEAT_ANALYZER_CMD": "seat-analyzer",
            "PYTHONUNBUFFERED": "1"},
        "StartCalendarInterval": (
            {"Weekday": 0, "Hour": 2, "Minute": 0} if mode == "weekly"
            else {"Day": 2, "Hour": 2, "Minute": 0}),
        "RunAtLoad": False,
        "StandardOutPath": f"{home}/.seat-analyzer/logs/collect-{mode}.log",
        "StandardErrorPath": f"{home}/.seat-analyzer/logs/collect-{mode}.log",
    }


def _plist(path):
    with path.open("rb") as stream:
        return plistlib.load(stream)


def test_example_files():
    assert sorted(path.name for path in EXAMPLES.iterdir()) == [
        "collect.zsh", "install.zsh", "seat-analyzer.collect-monthly.plist",
        "seat-analyzer.collect-weekly.plist"]


@pytest.mark.parametrize("mode", MODES)
def test_plist_template(mode):
    assert _plist(EXAMPLES / f"seat-analyzer.collect-{mode}.plist") == _expected(mode)


def test_templates_have_only_public_placeholders_and_paths():
    placeholders = set()
    for path in EXAMPLES.iterdir():
        text = path.read_text(encoding="utf-8")
        placeholders.update(re.findall(r"__[A-Z][A-Z_]*__", text))
        assert "/Users/" not in text
        assert "/home/" not in text
    assert placeholders == {"__HOME__", "__WORKSPACE__"}


@HAS_ZSH
@pytest.mark.parametrize("name", ["collect.zsh", "install.zsh"])
def test_shell_syntax(name):
    proc = subprocess.run(
        [ZSH, "-n", str(EXAMPLES / name)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def _executable(path, source):
    path.write_text("#!/bin/sh\n" + source, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _calls(path):
    if not path.exists():
        return []
    return [
        call.split("\0") for call in path.read_bytes().decode("utf-8").split("\0\0")
        if call]


@dataclass
class Sandbox:
    root: Path
    bin: Path
    home: Path
    workspace: Path
    env: dict

    def run(self, script, *args, **variables):
        env = self.env | variables
        # 起動のたびに、実環境の通知・launchd を呼ばないことを確かめる。
        fake = "osascript" if script == "collect.zsh" else "launchctl"
        assert shutil.which(fake, path=env["PATH"]) == str(self.bin / fake)
        return subprocess.run(
            [ZSH, str(EXAMPLES / script), *args], cwd=self.workspace,
            env=env, capture_output=True, text=True, encoding="utf-8", check=False)

    def calls(self, name):
        return _calls(self.root / f"{name}.calls")

    def clear_calls(self):
        for path in self.root.glob("*.calls"):
            path.unlink()


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "H& <example>"
    home.mkdir()
    workspace = tmp_path / "R&D <ws>"
    workspace.mkdir()
    (workspace / "config.yaml").write_text("{}\n", encoding="utf-8", newline="\n")
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "FAKE_COMMAND_LOG": str(tmp_path / "command.calls"),
        "FAKE_NOTIFICATION_LOG": str(tmp_path / "notification.calls"),
        "FAKE_LAUNCHCTL_LOG": str(tmp_path / "launchctl.calls"),
        "FAKE_DATE_LOG": str(tmp_path / "date.calls"),
        "FAKE_CWD_LOG": str(tmp_path / "cwd.calls"),
        "FAKE_YM": "2026-10",
    }
    command_source = """
printf '%s\\0' "$@" >> "$FAKE_COMMAND_LOG"
printf '\\0' >> "$FAKE_COMMAND_LOG"
pwd >> "$FAKE_CWD_LOG"
for arg in "$@"; do
    if [ "$arg" = --check-login ]; then
        exit "${FAKE_CHECK_EXIT:-0}"
    fi
done
printf '%s' "${FAKE_COLLECT_OUT:-}"
exit "${FAKE_COLLECT_EXIT:-0}"
"""
    for name in ("seat-analyzer", "fake-uv"):
        _executable(bin_dir / name, command_source)
    _executable(bin_dir / "osascript", """
printf '%s\\0' "$@" >> "$FAKE_NOTIFICATION_LOG"
printf '\\0' >> "$FAKE_NOTIFICATION_LOG"
exit "${FAKE_NOTIFICATION_EXIT:-0}"
""")
    _executable(bin_dir / "date", """
if [ "$1" = '+%Y-%m' ]; then
    printf '%s\\0\\0' "$1" >> "$FAKE_DATE_LOG"
    printf '%s\\n' "$FAKE_YM"
else
    /bin/date "$@"
fi
""")
    _executable(bin_dir / "launchctl", """
printf '%s\\0' "$@" >> "$FAKE_LAUNCHCTL_LOG"
printf '\\0' >> "$FAKE_LAUNCHCTL_LOG"
case "$1" in
    bootout) exit "${FAKE_BOOTOUT_EXIT:-1}" ;;
    bootstrap) exit "${FAKE_BOOTSTRAP_EXIT:-0}" ;;
    *) exit 2 ;;
esac
""")
    return Sandbox(tmp_path, bin_dir, home, workspace, env)


CHECK = ["collect", "--source", "claude", "--check-login"]


@RUN_SHELL
def test_weekly(sandbox):
    proc = sandbox.run("collect.zsh", "weekly")
    assert proc.returncode == 0, proc.stderr
    assert sandbox.calls("command") == [["collect", "--source", "claude"], CHECK]
    assert not sandbox.calls("notification")
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} === weekly 開始 ===", proc.stdout)
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} === weekly 終了: 正常 ===", proc.stdout)
    assert (sandbox.root / "cwd.calls").read_text(encoding="utf-8").splitlines() == [
        str(sandbox.workspace.resolve())] * 2


@RUN_SHELL
@pytest.mark.parametrize(("ym", "previous"), [
    ("2027-01", "2026-12"), ("2026-09", "2026-08"),
    ("2026-10", "2026-09"), ("2026-12", "2026-11")])
def test_monthly(sandbox, ym, previous):
    proc = sandbox.run("collect.zsh", "monthly", FAKE_YM=ym)
    assert proc.returncode == 0, proc.stderr
    assert sandbox.calls("command") == [
        ["collect", "--source", "claude", "--month", previous], CHECK]
    assert sandbox.calls("date") == [["+%Y-%m"]]
    assert not sandbox.calls("notification")
    assert "=== monthly 開始 ===" in proc.stdout
    assert "=== monthly 終了: 正常 ===" in proc.stdout


@RUN_SHELL
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("collect_exit", "check_exit"), [("1", "0"), ("0", "1"), ("1", "1")])
def test_failures_notify_once_and_still_check_login(sandbox, mode, collect_exit, check_exit):
    proc = sandbox.run(
        "collect.zsh", mode, FAKE_COLLECT_EXIT=collect_exit, FAKE_CHECK_EXIT=check_exit)
    assert proc.returncode == 1
    collect = ["collect", "--source", "claude"]
    if mode == "monthly":
        collect += ["--month", "2026-09"]
    assert sandbox.calls("command") == [collect, CHECK]
    notifications = sandbox.calls("notification")
    assert len(notifications) == 1
    assert notifications[0][:-1] == [
        "-e", "on run argv", "-e",
        'display notification (item 1 of argv) with title "seat-analyzer 定期取得" sound name "Basso"',
        "-e", "end run"]
    names = []
    if collect_exit == "1":
        names.append("当月の取得" if mode == "weekly" else "前月（2026-09）の取得")
    if check_exit == "1":
        names.append("ログインの確認")
    joined = "・".join(names)
    assert notifications[0][-1] == f"要対応: {joined}。ログは ~/.seat-analyzer/logs/ にあります"
    assert f"=== {mode} 終了: 要対応（{joined}）===" in proc.stdout


@RUN_SHELL
def test_weekly_not_ready_is_success(sandbox):
    reason = "当月モード（2026-11）はまだ取得できません。取得できる時刻まで待ってください。\n"
    proc = sandbox.run("collect.zsh", "weekly", FAKE_COLLECT_OUT=reason)
    assert proc.returncode == 0, proc.stderr
    assert sandbox.calls("command") == [["collect", "--source", "claude"], CHECK]
    assert reason in proc.stdout
    assert "終了: 正常" in proc.stdout
    assert not sandbox.calls("notification")
    assert not (sandbox.workspace / "input").exists()


@RUN_SHELL
@pytest.mark.parametrize("spacing", [" ", "   "])
def test_multiple_word_command(sandbox, spacing):
    command = spacing + str(sandbox.bin / "fake-uv") + spacing + "run" + spacing + "seat-analyzer" + spacing
    proc = sandbox.run("collect.zsh", "weekly", SEAT_ANALYZER_CMD=command)
    assert proc.returncode == 0, proc.stderr
    assert sandbox.calls("command") == [
        ["run", "seat-analyzer", "collect", "--source", "claude"],
        ["run", "seat-analyzer", *CHECK]]
    assert not sandbox.calls("notification")


@RUN_SHELL
@pytest.mark.parametrize("mode", MODES)
def test_input_dir_only_applies_to_collection(sandbox, mode):
    input_dir = str(sandbox.root / "temporary input")
    proc = sandbox.run("collect.zsh", mode, SEAT_ANALYZER_INPUT_DIR=input_dir)
    assert proc.returncode == 0, proc.stderr
    collect = ["collect", "--source", "claude"]
    if mode == "monthly":
        collect += ["--month", "2026-09"]
    assert sandbox.calls("command") == [collect + ["--input-dir", input_dir], CHECK]
    assert not sandbox.calls("notification")


@RUN_SHELL
def test_empty_command_uses_default(sandbox):
    proc = sandbox.run("collect.zsh", "weekly", SEAT_ANALYZER_CMD="", SEAT_ANALYZER_INPUT_DIR="")
    assert proc.returncode == 0, proc.stderr
    assert sandbox.calls("command") == [["collect", "--source", "claude"], CHECK]


@RUN_SHELL
def test_missing_command_notifies(sandbox):
    proc = sandbox.run("collect.zsh", "weekly", SEAT_ANALYZER_CMD="no-such-example-command")
    assert proc.returncode == 1
    assert not sandbox.calls("command")
    assert len(sandbox.calls("notification")) == 1
    assert "当月の取得・ログインの確認" in sandbox.calls("notification")[0][-1]


@RUN_SHELL
def test_notification_failure_is_logged(sandbox):
    proc = sandbox.run("collect.zsh", "weekly", FAKE_CHECK_EXIT="1", FAKE_NOTIFICATION_EXIT="1")
    assert proc.returncode == 1
    assert len(sandbox.calls("notification")) == 1
    assert "通知に失敗" in proc.stdout


@RUN_SHELL
@pytest.mark.parametrize("args", [[], ["daily"], ["weekly", "monthly"]])
def test_collect_invalid_arguments(sandbox, args):
    proc = sandbox.run("collect.zsh", *args)
    assert proc.returncode == 2
    assert "使い方:" in proc.stderr
    assert not sandbox.calls("command")
    assert not sandbox.calls("notification")


def _install_calls(sandbox):
    domain = f"gui/{os.getuid()}"
    return [
        call for mode in MODES for call in (
            ["bootout", f"{domain}/seat-analyzer.collect-{mode}"],
            ["bootstrap", domain, str(
                sandbox.home / "Library/LaunchAgents" / f"seat-analyzer.collect-{mode}.plist")])]


def _assert_installed(sandbox):
    agents = sandbox.home / "Library/LaunchAgents"
    for mode in MODES:
        actual = _plist(agents / f"seat-analyzer.collect-{mode}.plist")
        assert actual == _expected(mode, str(sandbox.home.resolve()), str(sandbox.workspace.resolve()))
        assert not re.search(r"__[A-Z][A-Z_]*__", repr(actual))
    assert (sandbox.home / ".seat-analyzer/scheduled/collect.zsh").read_bytes() == (
        EXAMPLES / "collect.zsh").read_bytes()
    assert (sandbox.home / ".seat-analyzer/logs").is_dir()
    assert sorted(path.name for path in agents.iterdir()) == [
        "seat-analyzer.collect-monthly.plist", "seat-analyzer.collect-weekly.plist"]
    assert [p.name for p in (sandbox.home / ".seat-analyzer/scheduled").iterdir()] == ["collect.zsh"]


@MACOS
def test_install(sandbox):
    # 相対パスも受け付け、実行場所は雛形の置き場と異なる。
    proc = sandbox.run("install.zsh", ".")
    assert proc.returncode == 0, proc.stderr
    _assert_installed(sandbox)
    assert sandbox.calls("launchctl") == _install_calls(sandbox)
    for mode in MODES:
        label = f"seat-analyzer.collect-{mode}"
        assert label in proc.stdout
        assert f"launchctl print gui/{os.getuid()}/{label}" in proc.stdout
        assert f"launchctl kickstart gui/{os.getuid()}/{label}" in proc.stdout
    assert str(sandbox.home / ".seat-analyzer/logs") in proc.stdout


@MACOS
def test_reinstall_replaces_workspace(sandbox):
    assert sandbox.run("install.zsh", str(sandbox.workspace)).returncode == 0
    sandbox.clear_calls()
    sandbox.workspace = sandbox.root / "another R&D <ws>"
    sandbox.workspace.mkdir()
    (sandbox.workspace / "config.yaml").write_text("{}\n", encoding="utf-8", newline="\n")
    proc = sandbox.run("install.zsh", str(sandbox.workspace), FAKE_BOOTOUT_EXIT="0")
    assert proc.returncode == 0, proc.stderr
    _assert_installed(sandbox)
    assert sandbox.calls("launchctl") == _install_calls(sandbox)


@MACOS
@pytest.mark.parametrize("installed", [False, True])
def test_uninstall_leaves_script_and_logs(sandbox, installed):
    if installed:
        assert sandbox.run("install.zsh", str(sandbox.workspace)).returncode == 0
        sandbox.clear_calls()
    proc = sandbox.run("install.zsh", "--uninstall")
    assert proc.returncode == 0, proc.stderr
    assert sandbox.calls("launchctl") == [
        ["bootout", f"gui/{os.getuid()}/seat-analyzer.collect-{mode}"] for mode in MODES]
    for mode in MODES:
        assert not (sandbox.home / f"Library/LaunchAgents/seat-analyzer.collect-{mode}.plist").exists()
    if installed:
        assert (sandbox.home / ".seat-analyzer/scheduled/collect.zsh").is_file()
        assert (sandbox.home / ".seat-analyzer/logs").is_dir()
    assert "残しています" in proc.stdout


@MACOS
@pytest.mark.parametrize("missing", ["workspace", "config"])
def test_install_requires_workspace_and_config(sandbox, missing):
    workspace = sandbox.root / "nonexistent" if missing == "workspace" else sandbox.workspace
    if missing == "config":
        (workspace / "config.yaml").unlink()
    proc = sandbox.run("install.zsh", str(workspace))
    assert proc.returncode != 0
    assert not (sandbox.home / "Library/LaunchAgents").exists()
    assert not (sandbox.home / ".seat-analyzer").exists()
    assert not sandbox.calls("launchctl")


@MACOS
def test_install_bootstrap_failure(sandbox):
    proc = sandbox.run("install.zsh", str(sandbox.workspace), FAKE_BOOTSTRAP_EXIT="1")
    assert proc.returncode == 1
    assert "登録に失敗" in proc.stderr
    assert sandbox.calls("launchctl") == _install_calls(sandbox)[:2]


@MACOS
def test_install_rejects_invalid_plist_and_removes_temporary_file(sandbox):
    templates = sandbox.root / "templates"
    shutil.copytree(EXAMPLES, templates)
    plist = templates / "seat-analyzer.collect-weekly.plist"
    plist.write_text(
        "<invalid>\n", encoding="utf-8", newline="\n")
    proc = sandbox.run(str(templates / "install.zsh"), str(sandbox.workspace))
    assert proc.returncode == 1
    assert "plist の検査に失敗" in proc.stderr
    assert not sandbox.calls("launchctl")
    assert list((sandbox.home / "Library/LaunchAgents").iterdir()) == []


@MACOS
@pytest.mark.parametrize("args", [[], ["one", "two"], ["--unknown"]])
def test_install_invalid_arguments(sandbox, args):
    proc = sandbox.run("install.zsh", *args)
    assert proc.returncode == 2
    assert "使い方:" in proc.stderr
    assert not sandbox.calls("launchctl")
    assert not (sandbox.home / ".seat-analyzer").exists()
