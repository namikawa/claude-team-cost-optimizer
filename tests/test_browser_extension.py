"""同梱の拡張機能（browser_extension/）の静的な検査。

ブラウザは起動しない。見るのは、JavaScript の構文（node があれば `node --check`）、
manifest.json の権限と参照するファイル、key（公開鍵）から導いた拡張機能の ID、拡張機能と
コマンドの取り決め（トリガー URL・staging のファイル名・種別のディレクトリ）が両側で
揃っていること、拡張機能の通信先が claude.ai だけであること。
"""

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from seat_analyzer import claude_export

EXT = claude_export.extension_dir()
MANIFEST = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
SCRIPTS = sorted(EXT.glob("*.js"))


def _text(name: str) -> str:
    return (EXT / name).read_text(encoding="utf-8")


def test_extension_dir_is_inside_the_package():
    assert EXT == Path(claude_export.__file__).parent / "browser_extension"
    assert (EXT / "manifest.json").is_file()


def test_extension_ships_only_its_own_files():
    """同梱するのは拡張機能の 5 ファイルだけ（鍵などを置かない）。"""
    assert sorted(path.name for path in EXT.iterdir()) == [
        "background.js", "manifest.json", "run.html", "run.js", "trigger.js"]
    for path in EXT.iterdir():
        assert "PRIVATE KEY" not in path.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い環境では構文を検査しない")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(script):
    proc = subprocess.run(
        ["node", "--check", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_manifest():
    assert MANIFEST["manifest_version"] == 3
    assert MANIFEST["name"] == "seat-analyzer export"
    assert MANIFEST["version"] == "1.0"
    assert MANIFEST["key"]
    assert set(MANIFEST["permissions"]) == {
        "cookies", "downloads", "tabs", "storage", "scripting", "webRequest"}
    assert MANIFEST["host_permissions"] == ["https://claude.ai/*"]
    assert MANIFEST["background"] == {"service_worker": "background.js"}
    assert MANIFEST["content_scripts"] == [{
        "matches": ["https://claude.ai/*"], "js": ["trigger.js"], "run_at": "document_start"}]


def test_manifest_refers_to_existing_files():
    referenced = [MANIFEST["background"]["service_worker"]]
    for script in MANIFEST["content_scripts"]:
        referenced += script["js"]
    for resource in MANIFEST["web_accessible_resources"]:
        referenced += resource["resources"]
    assert referenced
    for name in referenced:
        assert (EXT / name).is_file(), name
    # 実行ページが読み込むスクリプトもある
    assert '<script src="run.js"></script>' in _text("run.html")


def test_manifest_key_gives_the_fixed_extension_id():
    """ID は key の公開鍵（DER）の SHA-256 の先頭 32 桁を a〜p に写したもの。"""
    digest = hashlib.sha256(base64.b64decode(MANIFEST["key"], validate=True)).hexdigest()[:32]
    extension_id = "".join(chr(ord("a") + int(digit, 16)) for digit in digest)
    assert extension_id == claude_export.EXTENSION_ID


def test_extension_talks_only_to_claude_ai():
    """コードに現れる URL はすべて claude.ai（外部への通信を持たない）。"""
    for path in EXT.iterdir():
        for url in re.findall(r"https?://[^\s\"'`)]+", path.read_text(encoding="utf-8")):
            assert url.startswith("https://claude.ai"), f"{path.name}: {url}"


def test_trigger_marker_matches_the_cli():
    """コマンドが開くトリガー URL のフラグメントを、拡張機能の両方の経路が拾う。"""
    marker = "#" + claude_export.TRIGGER_PREFIX.split("#", 1)[1]
    assert marker == "#seat-analyzer-run="
    assert f'const marker = "{marker}";' in _text("trigger.js")
    assert re.escape("https://claude.ai/").replace("/", "\\/") in _text("background.js")
    assert f"{marker}(.+)$/" in _text("background.js")


def test_staging_names_match_the_cli():
    """拡張機能が書くファイル名・種別のディレクトリ・モードと action がコマンドと揃っている。"""
    run_js = _text("run.js")
    for name in (claude_export.MANIFEST_NAME, claude_export.PROGRESS_NAME,
                 claude_export.ORGS_NAME):
        assert f'"{name}"' in run_js
    kind_dirs = ", ".join(f'{kind}: "{name}"' for kind, name in claude_export.KIND_DIRS.items())
    assert f"const KIND_DIRS = {{ {kind_dirs} }};" in run_js
    kinds = ", ".join(f'"{kind}"' for kind in claude_export.KINDS)
    assert f"const KINDS = [{kinds}];" in run_js
    assert (f'const MODE_TEXT = {{ {claude_export.MODE_CURRENT}: "当月", '
            f'{claude_export.MODE_PREVIOUS}: "前月" }};') in run_js
    assert f'spec.action === "{claude_export.ACTION_LIST_ORGS}"' in run_js
