"""通常ブラウザから取得したSpend CSVを検出する一時スクリプト。

実行は `uv run python scripts/spike_download_watcher.py`（直接実行はしない）。
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_URL = "https://claude.ai/"
POLL_INTERVAL_SECONDS = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "通常ブラウザを開き、利用者が手動でExportしたSpend CSVを検出して"
            "一時ディレクトリへコピーします。ブラウザの操作は自動化しません。"
        )
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"通常ブラウザで開くURL（既定: {DEFAULT_URL}）",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path.home() / "Downloads",
        help="ブラウザのダウンロード先（既定: ~/Downloads）",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Spend CSVを待つ秒数（既定: 300）",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        help="コピー先。省略時はOSの一時ディレクトリを新規作成します",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="ブラウザを開かず、ダウンロード監視だけを行います",
    )
    return parser.parse_args()


def _prepare_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _available_destination(directory: Path) -> Path:
    candidate = directory / "spend-report.csv"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"spend-report-{suffix}.csv"
        suffix += 1
    return candidate


def _snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    result: dict[Path, tuple[int, int]] = {}
    for path in directory.glob("*.csv"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        result[path] = (stat.st_mtime_ns, stat.st_size)
    return result


def _normalized_headers(path: Path) -> set[str]:
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        first_row = next(csv.reader(csv_file), [])
    return {
        "".join(character for character in value.casefold() if character.isalnum())
        for value in first_row
    }


def _looks_like_spend_report(path: Path) -> bool:
    try:
        headers = _normalized_headers(path)
    except (OSError, UnicodeError, csv.Error):
        return False

    has_identity = bool(headers & {"email", "accountuuid", "userid"})
    has_product = "product" in headers
    has_usage = bool(
        headers
        & {
            "prompttokens",
            "inputtokens",
            "completiontokens",
            "outputtokens",
            "totalnetspendusd",
            "netspend",
        }
    )
    return has_identity and has_product and has_usage


def _new_spend_csv(
    directory: Path,
    before: dict[Path, tuple[int, int]],
    stable_candidates: dict[Path, tuple[int, int]],
) -> Path | None:
    current = _snapshot(directory)
    for path, signature in sorted(
        current.items(),
        key=lambda item: item[1][0],
        reverse=True,
    ):
        if before.get(path) == signature:
            continue
        if stable_candidates.get(path) != signature:
            stable_candidates[path] = signature
            continue
        if _looks_like_spend_report(path):
            return path
    return None


def _open_normal_browser(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=True)
        return

    import webbrowser

    if not webbrowser.open(url):
        raise RuntimeError("通常ブラウザを開けませんでした")


def run(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0:
        print("--timeout-secondsは1以上にしてください。", file=sys.stderr)
        return 2

    source_dir = args.source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        print("指定されたダウンロード先がありません。", file=sys.stderr)
        return 2

    destination_dir = (
        _prepare_directory(args.download_dir)
        if args.download_dir is not None
        else Path(tempfile.mkdtemp(prefix="claude-spend-download-"))
    )
    before = _snapshot(source_dir)
    stable_candidates: dict[Path, tuple[int, int]] = {}

    print("browser-assisted smoke testを開始します。")
    print("1. 通常ブラウザで対象Organizationを選択してください。")
    print("2. Settings > Analyticsを開いてください。")
    print("3. Spend reportをCSVでExportしてください。")
    print("スクリプトは画面や認証情報に触れず、ダウンロード先だけを監視します。")

    if not args.no_open:
        try:
            _open_normal_browser(args.url)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            print(f"通常ブラウザを開けませんでした: {exc}", file=sys.stderr)
            return 1

    deadline = time.monotonic() + args.timeout_seconds
    source_csv: Path | None = None
    while time.monotonic() < deadline:
        source_csv = _new_spend_csv(source_dir, before, stable_candidates)
        if source_csv is not None:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    if source_csv is None:
        print("制限時間内に新しいSpend CSVを検出できませんでした。", file=sys.stderr)
        return 1

    destination = _available_destination(destination_dir)
    shutil.copy2(source_csv, destination)
    print(f"Spend CSVを一時ディレクトリへコピーしました: {destination}")
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
