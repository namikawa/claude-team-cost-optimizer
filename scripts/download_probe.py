"""合成データ専用のダウンロード検証環境を作る。Chrome は起動しない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[1]
EXTENSION = REPO / "src/seat_analyzer/browser_extension"
UUID_A = "00000000-0000-4000-8000-000000000001"
UUID_B = "00000000-0000-4000-8000-000000000002"
CODE_NAME = "claude_code_2026_10_01_to_2026_10_31.csv"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: object) -> None:
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extracted_source() -> tuple[str, dict]:
    """行頭の宣言と閉じ括弧で区切り、一意な原文だけを採る。"""
    raw = (EXTENSION / "run.js").read_bytes()
    source = raw.decode("utf-8")
    patterns = {
        "WAIT": r"^const WAIT = \{.*?^\};$",
        "sleep": r"^const sleep = .*;$",
        "basename": r"^const basename = .*;$",
    }
    for name in ("trackDownload", "waitUntil", "exportWith", "saveJson"):
        patterns[name] = rf"^(?:async )?function {name}\([^\n]*\) \{{.*?^\}}$"
    chunks = {}
    for name, pattern in patterns.items():
        matches = re.findall(pattern, source, re.MULTILINE | re.DOTALL)
        # 一行宣言は DOTALL による他行への拡大を許さない。
        if name in ("sleep", "basename"):
            matches = re.findall(pattern.replace(".*", "[^\\n]*"), source, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError(f"原文抽出が一意ではありません: {name}")
        chunks[name] = matches[0]
    return "\n\n".join(chunks.values()) + "\n", {
        "run.js": digest(raw),
        "background.js": digest((EXTENSION / "background.js").read_bytes()),
        "extracted": {name: digest(text.encode()) for name, text in chunks.items()},
    }


def prepare(port: int, other_port: int) -> Path:
    if port == other_port or not all(1024 <= p <= 65535 for p in (port, other_port)):
        raise ValueError("異なる2つのポート（1024〜65535）を指定してください")
    source, hashes = extracted_source()
    root = Path(mkdtemp(prefix="seat-download-probe-")).resolve()
    for directory in ("extension", "profile/Default", "downloads", "results"):
        (root / directory).mkdir(parents=True)
    config = {"format": "seat-download-probe-v1", "root": str(root),
              "origin": f"http://127.0.0.1:{port}",
              "otherOrigin": f"http://127.0.0.1:{other_port}", "sources": hashes}
    write_json(root / "probe.json", config)
    extension = root / "extension"
    write(extension / "production.js", source)
    (extension / "background.js").write_bytes((EXTENSION / "background.js").read_bytes())
    write(extension / "probe.js", (REPO / "scripts/download_probe.js").read_text(encoding="utf-8"))
    write(extension / "config.js", "const PROBE_CONFIG = " + json.dumps(config) + ";\n")
    write(extension / "worker.js", 'importScripts("background.js");\n'
          'chrome.action.onClicked.addListener(() => {\n'
          '  chrome.tabs.create({url: chrome.runtime.getURL("probe.html")});\n});\n')
    write_json(extension / "manifest.json", {
        "manifest_version": 3, "name": "Synthetic download probe", "version": "0.1.0",
        "permissions": ["downloads", "storage", "tabs", "scripting"],
        "host_permissions": ["http://127.0.0.1/*"],
        "background": {"service_worker": "worker.js"}, "action": {"default_title": "検証を開く"},
        "content_security_policy": {"extension_pages":
            "script-src 'self'; object-src 'self'; connect-src http://127.0.0.1:*"},
    })
    write(extension / "probe.html", '<!doctype html><html lang="ja"><meta charset="utf-8">'
          '<title>合成ダウンロード検証</title><h1>合成ダウンロード検証</h1>'
          '<p>専用プロファイルとloopbackサーバで実行してください。実行中はこのタブを残します。</p>'
          '<button id="start">検証を開始</button><pre id="log"></pre>'
          '<script src="config.js"></script><script src="production.js"></script>'
          '<script src="probe.js"></script></html>\n')
    write_json(root / "profile/Default/Preferences", {
        "download": {"default_directory": str(root / "downloads"), "prompt_for_download": False},
        "profile": {"default_content_setting_values": {"automatic_downloads": 1}},
    })
    command = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               f"--user-data-dir={root / 'profile'}", "--no-first-run", "--no-default-browser-check",
               "chrome://extensions/"]
    print(f"検証root: {root}\n拡張を読み込む絶対パス: {extension}")
    print("サーバ: uv run python scripts/download_probe.py serve " + shlex.quote(str(root)))
    print("macOS Chrome: " + shlex.join(command))
    return root


def read_config(root: Path) -> dict:
    root = root.resolve()
    config = json.loads((root / "probe.json").read_text(encoding="utf-8"))
    if config.get("format") != "seat-download-probe-v1" or config.get("root") != str(root):
        raise ValueError("このツールが生成した検証rootではありません")
    for name in ("extension", "profile", "downloads", "results"):
        if (root / name).is_symlink() or not (root / name).is_dir():
            raise ValueError(f"専用ディレクトリを確認できません: {name}")
    return config


def csv_content(marker: str) -> bytes:
    return f"email,loc_with_cc,probe_marker\nprobe@example.invalid,1,{marker}\n".encode()


def serve(root: Path) -> None:
    config = read_config(root)
    lock = threading.Lock()
    state = {"pending": 0, "requests": [], "counts": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body, content_type="application/json", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/page":
                self.reply(200, b'<!doctype html><meta charset="utf-8"><title>Probe page</title>'
                           b'<p>Synthetic download source</p>', "text/html; charset=utf-8")
            elif parsed.path == "/status":
                with lock:
                    body = json.dumps(state).encode()
                self.reply(200, body)
            elif parsed.path == "/acceptance":
                path = root / "results/acceptance.json"
                body = path.read_bytes() if path.is_file() else b'{"status":"pending"}'
                self.reply(200, body)
            elif parsed.path == "/download":
                query = parse_qs(parsed.query)
                case = query.get("case", [""])[0]
                mode = query.get("mode", ["single"])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", case) or mode not in (
                    "single", "same", "foreign", "late", "next", "parallel"
                ):
                    self.reply(400, b'{}')
                    return
                with lock:
                    number = state["counts"].get(case, 0) + 1
                    state["counts"][case] = number
                    state["pending"] += 1
                    marker = ("UNRELATED" if mode in ("same", "foreign") and number == 1
                              else "LATE_FIRST" if mode == "late" and number == 1
                              else "RETRY" if mode == "late" else "TARGET")
                    delay = (0.8 if mode in ("same", "foreign") and number == 2 else
                             6.5 if mode == "late" and number == 1 else
                             5 if mode == "next" else 0)
                    record = {"case": case, "number": number, "marker": marker,
                              "origin_port": self.server.server_port,
                              "referrer": self.headers.get("Referer"),
                              "started": time.time(), "delay": delay}
                    state["requests"].append(record)
                try:
                    time.sleep(delay)
                    filename = (f"{marker.lower()}_{CODE_NAME}" if mode in ("same", "foreign")
                                else CODE_NAME)
                    self.reply(200, csv_content(marker), "text/csv; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="{filename}"'})
                    with lock:
                        record["response_finished"] = time.time()
                except (BrokenPipeError, ConnectionResetError):
                    with lock:
                        record["connection_closed"] = True
                finally:
                    with lock:
                        state["pending"] -= 1
                        write_json(root / "results/server.json", {
                            "evidence": "loopback_server", **state})
            else:
                self.reply(404, b'{}')

        def do_POST(self):
            if self.path != "/record":
                self.reply(404, b'{}')
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                self.reply(400, b'{}')
                return
            try:
                data = json.loads(self.rfile.read(length))
                if data.get("evidence") != "chrome_live":
                    raise ValueError("evidence")
            except (ValueError, AttributeError):
                self.reply(400, b'{}')
                return
            write_json(root / "results/chrome_live.json", data)
            self.reply(200, b'{"ok":true}')

    servers = []
    started = False
    try:
        for origin in (config["origin"], config["otherOrigin"]):
            server = ThreadingHTTPServer(("127.0.0.1", urlsplit(origin).port), Handler)
            servers.append(server)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        started = True
        print(f"loopbackサーバ稼働: {config['origin']} / {config['otherOrigin']}", flush=True)
        print("専用Chromeで拡張を読み込み、拡張アイコンから検証タブを開きます。終了はCtrl-C。",
              flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            if started:
                server.shutdown()
            server.server_close()


def self_check(root: Path) -> None:
    """CLI の既存検証・配置へ合成ファイルだけを渡す。"""
    from seat_analyzer.claude_export import ExportTarget, place_export, verify_export
    from seat_analyzer.config import PACKAGE_CONFIG_PATH, load_config

    config = read_config(root)
    source, hashes = extracted_source()
    assert config["sources"] == hashes, "準備後に元ソースが変わっています"
    assert (root / "extension/production.js").read_text(encoding="utf-8") == source
    assert (root / "extension/background.js").read_bytes() == (EXTENSION / "background.js").read_bytes()
    columns = load_config(PACKAGE_CONFIG_PATH)["columns"]
    work = Path(mkdtemp(prefix="cli-", dir=root / "results"))
    (work / "input/probe-org").mkdir(parents=True)
    target = ExportTarget("probe-org", None, "probe", UUID_A, ("members", "spend", "code"))
    checks = []
    for kind, name, body in (
        ("members", f"members-{UUID_A}-2026-10-21.csv", "email,seat\nprobe@example.invalid,Standard\n"),
        ("spend", f"spend-{UUID_A}-2026-10-01-to-2026-10-21.csv",
         "email,model,product,prompt_tokens,completion_tokens\nprobe@example.invalid,synthetic,Code,1,1\n"),
        ("code", CODE_NAME, csv_content("TARGET").decode()),
    ):
        path = work / name
        write(path, body)
        verdict = verify_export(path, kind, "2026-10", columns_aliases=columns, org_id=UUID_A)
        assert verdict.ok, verdict.reason
        dest = place_export(path, work / "input", target, kind)
        assert path.read_bytes() == dest.read_bytes()
        checks.append({"kind": kind, "verified": True, "placed_bytes_equal": True})
    for kind, name, body in (
        ("members", f"members-{UUID_B}-2026-10-21.csv", "email,seat\nprobe@example.invalid,Standard\n"),
        ("spend", f"spend-{UUID_B}-2026-10-01-to-2026-10-21.csv",
         "email,model,product,prompt_tokens,completion_tokens\nprobe@example.invalid,synthetic,Code,1,1\n"),
        ("code", CODE_NAME, csv_content("OTHER_ORG").decode()),
    ):
        path = work / name
        write(path, body)
        verdict = verify_export(path, kind, "2026-10", columns_aliases=columns, org_id=UUID_A)
        assert verdict.ok == (kind == "code")
        checks.append({"kind": kind, "other_org_accepted": verdict.ok, "reason": verdict.reason})
    write_json(root / "results/cli_synthetic.json", {
        "evidence": "cli_synthetic", "sources": hashes, "checks": checks,
        "limitation": "同期間のcode CSVは本文の組織を識別しない。ブラウザーの証拠ではない。"})
    print("原文一致・3種CSV検証/配置・別組織相当CSVの現行境界: OK")


def inspect(root: Path) -> None:
    read_config(root)
    downloads = (root / "downloads").resolve()
    files = []
    for path in sorted(downloads.rglob("*")):
        if path.is_symlink():
            raise ValueError("専用downloads内のシンボリックリンクは読みません")
        if path.is_file():
            data = path.read_bytes()
            files.append({"path": str(path.relative_to(downloads)), "bytes": len(data),
                          "sha256": digest(data),
                          "content": data.decode("utf-8", errors="replace")})
    write_json(root / "results/chrome_files.json", {"evidence": "dedicated_download_files", "files": files})
    live_path = root / "results/chrome_live.json"
    if not live_path.is_file():
        result = {"status": "pending", "reason": "実機記録がまだありません"}
    else:
        live = json.loads(live_path.read_text(encoding="utf-8"))
        by_path = {item["path"]: item["content"] for item in files}
        checks = [{"name": "api_run_completed", "ok": live.get("fatal") is None}]
        stamp = live["stamp"]

        def marker(path: str, expected: str) -> bool:
            text = by_path.get(path, "")
            if path.endswith(".json"):
                try:
                    return json.loads(text).get("probe_marker") == expected
                except (ValueError, AttributeError):
                    return False
            return text.endswith(f",{expected}\n")

        checks.append({"name": "normal_csv", "ok": marker(f"{stamp}_normal/{CODE_NAME}", "TARGET")})
        for suffix, name, expected in (
            ("normal", "normal.json", "NORMAL_JSON"),
            ("overwrite", "overwrite.json", "OVERWRITE_SECOND"),
            *((f"json_{i}", "parallel.json", "PARALLEL_JSON") for i in range(1, 4)),
        ):
            case = next((case for case in live["cases"] if case["case"] == f"{stamp}_{suffix}"), None)
            path = f"{stamp}_{suffix}/{name}"
            trackers = [tracker for tracker in (case or {}).get("trackers", [])
                        if tracker.get("known_id") is not None]
            expected_count = 2 if suffix == "overwrite" else 1
            ids_and_paths = len(trackers) == expected_count
            for tracker in trackers:
                returned = tracker.get("returned", {})
                normalized = str(returned.get("filename", "")).replace("\\", "/")
                ids_and_paths &= (returned.get("id") == tracker["known_id"]
                                  and normalized.endswith("/" + path))
            checks.append({"name": suffix + "_json", "path": path,
                           "ok": bool(case and not case.get("error") and ids_and_paths
                                      and marker(path, expected))})
            if suffix == "overwrite":
                siblings = [p for p in by_path if p.startswith(f"{stamp}_overwrite/")]
                checks.append({"name": "overwrite_no_numbered_copy", "ok": siblings == [path]})
            if suffix.startswith("json_"):
                page_items = [item for item in (case or {}).get("downloads", [])
                              if item.get("url", "").startswith("http://")]
                direct = len(page_items) == 1
                for item in page_items:
                    try:
                        relative = Path(item["filename"]).resolve().relative_to(downloads)
                        direct &= len(relative.parts) == 1 and marker(relative.as_posix(), "TARGET")
                    except ValueError:
                        direct = False
                checks.append({"name": suffix + "_csv_unchanged", "ok": bool(direct)})
        result = {"evidence": "dedicated_files_acceptance", "scope": "json_downloads",
                  "status": "passed" if all(check["ok"] for check in checks) else "failed",
                  "checks": checks, "sources": live.get("sources")}
    write_json(root / "results/acceptance.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="新規一時環境を生成し、絶対パスを表示")
    prep.add_argument("--port", type=int, default=8765)
    prep.add_argument("--other-port", type=int, default=8766)
    for command in ("serve", "self-check", "inspect"):
        sub.add_parser(command).add_argument("root", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.port, args.other_port)
    else:
        root = args.root.expanduser().resolve()
        {"serve": serve, "self-check": self_check, "inspect": inspect}[args.command](root)


if __name__ == "__main__":
    main()
