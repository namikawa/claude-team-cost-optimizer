"""claude.ai からの CSV 取得（`collect --source claude`）の計画・検証・配置と、Chrome の起動・終了。

取得するのは管理画面から人がダウンロードしているのと同じ 3 種の CSV（メンバー一覧・支出
レポート・Claude Code analytics）。ページの操作は専用プロファイルの Google Chrome に読み込んだ
同梱の拡張機能が行い、このモジュールはその前後を受け持つ（設計書 §14）。

1つ目は計画。設定の `organizations.<組織名>.claude_export`（複数スペースの組織は
`workspaces.<workspace名>.claude_export`）を書いた組織／workspace だけを対象として列挙し
（`gated_targets`。書かなければ何も取得しない）、対象月から取得のモードを決め（当月と前月の
2 つだけ。UI にそれ以外の選択肢が無い）、ブラウザのプロファイルごとに 1 回の実行へまとめる。
拡張機能へは、実行内容を JSON にしてトリガー URL（`TRIGGER_PREFIX`）のフラグメントで渡す。

2つ目は結果の受け取り。拡張機能はダウンロードを staging の実行ディレクトリへ振り分け、最後に
manifest.json を書く。manifest の各要素は計画の (配置先, 種別) と突き合わせるだけで、配置先の
パスは常に計画の側から組む（manifest に書かれた場所は信用しない）。ファイルは種別ごとの
ヘッダと、ファイル名の期間が対象月と合うかを確かめてから、元のファイル名のまま入力
ディレクトリへコピーする。合わないものは配置しない（別の月・別の種別の CSV を分析へ
混ぜないため）。配置先の組織ディレクトリが無ければ作らずに止める（設定の綴り違いで新しい
組織ができるのを防ぐ）。

3つ目は Chrome のプロファイル設定（Preferences）とプロセスの扱い。Preferences には
claude.ai からの自動ダウンロードの許可と、ダウンロード先（staging）だけを書き足し、他の
項目はそのまま保つ。OS に依存するのは Chrome の場所・起動・プロセスの列挙と終了だけで、
いずれもここに閉じる。文字列の組み立てと解析は純粋関数にしてテストし、実際にプロセスを
起動・終了させる薄いラッパ（`launch_chrome`・`list_chrome_pids`・`terminate_chrome`）だけを
テストの対象外にする。

staging の実行ディレクトリ（`<staging>/<run_id>/`）には、拡張機能が書く manifest.json・
progress.json（途中経過）・orgs.json（組織一覧）と、コマンドが起動の前に書く run.json
（その実行の計画）が並ぶ。run.json があるので、Chrome の待機が時間切れになった実行も、
拡張機能が manifest を書き終えた後に検証と配置だけをやり直せる（`restore_run`）。

拡張機能の実体はパッケージに同梱した `browser_extension/`（`extension_dir`）。manifest.json の
key に公開鍵を入れて ID を固定している（`EXTENSION_ID`）。

このモジュールは設定（層 20）を import しない。設定値は呼び出し側（cli）が辞書やパスで渡す。
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from . import ingest

# ------------------------------------------------------------------ 対象と計画

# 取得する種別 → 配置先の入力サブディレクトリ（`ingest.INPUT_SUBDIRS` の名前）。並びは
# 表示と拡張機能へ渡す順でもある
KIND_DIRS = {"members": "members", "spend": "spend", "code": "code-analytics"}
KINDS = tuple(KIND_DIRS)

# 種別 → ヘッダの検証に使う columns のセクションと、ヘッダに要る正準列（エイリアスは設定の
# columns から受け取る）。分析の読み込みが必須にする列をそのまま要求する。Claude Code
# analytics は人の列だけが必須なので、月間の LoC の列も要求して支出レポートとの取り違えを
# 止める（claude.ai のエクスポートはこの 2 列で、支出レポートには LoC の列が無い）
_KIND_COLUMNS: dict[str, tuple[str, tuple[str, ...]]] = {
    "members": ("members", tuple(ingest.REQUIRED_COLUMNS["members"])),
    "spend": ("spend", tuple(ingest.REQUIRED_COLUMNS["spend"])),
    "code": (
        "code_analytics", (*ingest.REQUIRED_COLUMNS["code_analytics"], "loc_with_cc"),
    ),
}

# 表示に使う種別の名前
_KIND_LABELS = {
    "members": "メンバー一覧",
    "spend": "支出レポート",
    "code": "Claude Code analytics",
}

# 取得のモード。当月は支出が「月累計」・Claude Code が表示中の月、前月は支出が「先月」・
# Claude Code が月送り 1 回（管理画面の選択肢がこの 2 つしか無い）
MODE_CURRENT = "current"
MODE_PREVIOUS = "previous"

# プロファイル名の規則（profiles_dir 配下のディレクトリ名になる。`.` と `..` は除く）
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def is_profile_name(name: object) -> bool:
    """プロファイル名として使えるか（英数字と . _ - からなり、. と .. ではない）。"""
    return (
        isinstance(name, str)
        and _PROFILE_NAME_RE.fullmatch(name) is not None
        and name not in (".", "..")
    )


def validate_profile_name(name: str) -> None:
    """プロファイル名として使えなければ ValueError（設定の claude_export.profile と同じ規則）。"""
    if not is_profile_name(name):
        raise ValueError(
            f"プロファイル名 '{name}' は使えません（英数字と . _ - からなる名前が必要です。"
            ". と .. は使えません）"
        )


@dataclass(frozen=True)
class ExportTarget:
    """取得の対象 1 つ（1 つの Team スペース）。

    org は入力ディレクトリ直下の組織名、workspace は入れ子レイアウトの workspace 名
    （単一スペースの組織は None）。org_id は claude.ai の組織 UUID（小文字に揃える）。
    """

    org: str
    workspace: str | None
    profile: str
    org_id: str
    kinds: tuple[str, ...]

    @property
    def dir(self) -> str:
        """配置先を表す相対名（"org" か "org/workspace"）。

        区切りは OS によらず "/"。拡張機能へ渡す名前と、manifest の結果を突き合わせる
        キーに使う（ファイルシステム上のパスは `target_dir` が組む）。
        """
        return self.org if self.workspace is None else f"{self.org}/{self.workspace}"


@dataclass(frozen=True)
class ProfileRun:
    """1 つのブラウザプロファイルで行う 1 回の取得。"""

    profile: str
    mode: str                  # MODE_CURRENT | MODE_PREVIOUS
    month: str                 # "YYYY-MM"
    targets: tuple[ExportTarget, ...]


def _target(org: str, workspace: str | None, section: object) -> ExportTarget | None:
    """claude_export の 1 区画から対象を作る。org_id が空（不活性）なら None。

    値の形はロード時の検証（config）が保証している。kinds は記述順によらず `KINDS` の
    並びに揃える（表示と実行の順を設定の書き方で変えない）。
    """
    if not isinstance(section, dict):
        return None
    org_id = section.get("org_id")
    if not isinstance(org_id, str) or not org_id:
        return None
    kinds = section.get("kinds") or []
    return ExportTarget(
        org=org,
        workspace=workspace,
        profile=str(section.get("profile", "")),
        org_id=org_id.lower(),
        kinds=tuple(kind for kind in KINDS if kind in kinds),
    )


def gated_targets(cfg_organizations: dict) -> list[ExportTarget]:
    """claude_export を有効にした対象（設定の記述順）。

    有効なのは org_id を書いた区画だけで、書かない組織・workspace は取得の対象にならない
    （`github_collect.gated_orgs` と同じく、書いた組織だけが対象）。workspaces を持つ組織は
    workspace ごとの区画だけを見る（配置先が workspace ごとに分かれるため。組織直下に
    書いた区画はロード時にエラーになる）。
    """
    targets: list[ExportTarget] = []
    for org, entry in cfg_organizations.items():
        if not isinstance(entry, dict):
            continue
        workspaces = entry.get("workspaces")
        if isinstance(workspaces, dict) and workspaces:
            for workspace, settings in workspaces.items():
                section = settings.get("claude_export") if isinstance(settings, dict) else None
                target = _target(str(org), str(workspace), section)
                if target is not None:
                    targets.append(target)
            continue
        target = _target(str(org), None, entry.get("claude_export"))
        if target is not None:
            targets.append(target)
    return targets


def local_today() -> dt.date:
    """実行機のローカル日付（当月の判定に使う。テストから差し替えられるようにする）。"""
    return dt.datetime.now().astimezone().date()


def _previous_month(today: dt.date) -> str:
    return f"{today.replace(day=1) - dt.timedelta(days=1):%Y-%m}"


def resolve_mode(month: str | None, today: dt.date) -> tuple[str, str]:
    """対象月から取得のモードと月を決める。省略時は当月。

    取得できるのは当月と前月だけで、それ以外の月は ValueError にする（管理画面が
    それより前の月を選ばせないため。手動でダウンロードした CSV を置く運用は変わらない）。
    当月は実行機のローカル日付で決める。
    """
    current = f"{today:%Y-%m}"
    previous = _previous_month(today)
    if month is None or month == current:
        return MODE_CURRENT, current
    if month == previous:
        return MODE_PREVIOUS, previous
    raise ValueError(
        f"--month {month}: 取得できるのは当月と前月だけです（当月 {current}・前月 {previous}）"
    )


def check_target_names(targets: Iterable[ExportTarget]) -> None:
    """配置先が同じディレクトリになりうる名前の組み合わせを ValueError にする。

    大文字小文字や文字の合成の違いだけの組織名は、それを区別しないファイルシステムでは
    同じ入力ディレクトリになり、別のスペースの CSV が1つの組織へ混ざる。組織名どうしと、
    同じ組織の workspace 名どうしを分析と同じ規則（`ingest.check_org_name_collisions`）で
    見る。一部の組織だけを選んだ実行でも、もう一方の配置先と重なりうるので全対象で見る。
    """
    targets = list(targets)
    try:
        ingest.check_org_name_collisions([target.org for target in targets])
    except ValueError as exc:
        raise ValueError(f"claude_export を設定した組織名が衝突しています: {exc}") from None
    workspaces: dict[str, list[str]] = {}
    for target in targets:
        if target.workspace is not None:
            workspaces.setdefault(target.org, []).append(target.workspace)
    for org, names in workspaces.items():
        try:
            ingest.check_org_name_collisions(names)
        except ValueError as exc:
            raise ValueError(
                f"組織 {org} の claude_export を設定した workspace 名が衝突しています: {exc}"
            ) from None


def plan_runs(
    targets: Sequence[ExportTarget],
    *,
    month: str | None,
    today: dt.date,
    orgs: Sequence[str] | None = None,
    profile: str | None = None,
) -> list[ProfileRun]:
    """対象をプロファイルごとの実行へまとめる（プロファイルの初出順・対象は記述順）。

    orgs・profile を渡すとその範囲へ絞る。orgs に claude_export の無い組織名があれば
    ValueError（綴り違いを黙って無視しない）。絞った結果が空なら空のリストを返す
    （案内と終了コードは呼び出し側が決める）。

    大文字小文字や文字の合成の違いだけの組織名（同じ組織の workspace 名どうしも）は、
    絞り込みの前に全対象で ValueError にする（`check_target_names`）。
    """
    check_target_names(targets)
    mode, resolved = resolve_mode(month, today)
    selected = list(targets)
    if orgs:
        known = {target.org for target in targets}
        for name in dict.fromkeys(orgs):
            if name not in known:
                raise ValueError(f"組織 {name} は claude_export が設定されていません")
        wanted = set(orgs)
        selected = [target for target in selected if target.org in wanted]
    if profile is not None:
        selected = [target for target in selected if target.profile == profile]
    grouped: dict[str, list[ExportTarget]] = {}
    for target in selected:
        grouped.setdefault(target.profile, []).append(target)
    return [
        ProfileRun(profile=name, mode=mode, month=resolved, targets=tuple(members))
        for name, members in grouped.items()
    ]


def new_run_id(run: ProfileRun, now: dt.datetime) -> str:
    """実行の識別子（staging の実行ディレクトリ名にもなる）。now はローカル時刻。"""
    return f"{run.profile}-{run.mode}-{now:%Y%m%d-%H%M%S}"


# ------------------------------------------------------------------ 拡張機能へ渡す内容

# 拡張機能が拾うトリガー URL の接頭辞。実行内容はフラグメントに載せるので、claude.ai の
# サーバーへは送られない
TRIGGER_PREFIX = "https://claude.ai/#seat-analyzer-run="


def run_spec(run: ProfileRun, run_id: str) -> dict:
    """拡張機能へ渡す実行内容。組織ごとに切替先の UUID・配置先の相対名・種別を持つ。"""
    return {
        "run_id": run_id,
        "mode": run.mode,
        "orgs": [
            {"uuid": target.org_id, "dir": target.dir, "kinds": list(target.kinds)}
            for target in run.targets
        ],
    }


def trigger_url(spec: dict) -> str:
    """実行内容を URL エンコードした JSON にしてトリガー URL へ載せる。"""
    payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    return TRIGGER_PREFIX + urllib.parse.quote(payload, safe="")


# 組織一覧（`--list-orgs`）の実行内容の action。拡張機能は参加している組織の一覧を
# orgs.json に書いて終わる
ACTION_LIST_ORGS = "list-orgs"


def list_orgs_run_id(profile: str, now: dt.datetime) -> str:
    """組織一覧の実行の識別子（`<profile>-list-orgs-<YYYYmmdd-HHMMSS>`）。now はローカル時刻。"""
    return f"{profile}-{ACTION_LIST_ORGS}-{now:%Y%m%d-%H%M%S}"


def list_orgs_spec(run_id: str) -> dict:
    """組織一覧を拡張機能へ頼む実行内容。"""
    return {"run_id": run_id, "action": ACTION_LIST_ORGS}


# ログインと再ログイン（`--setup`・`--login`）で開くページ
LOGIN_URL = "https://claude.ai/login"

# 同梱の拡張機能の ID。manifest.json の key（公開鍵）から決まるので、読み込んだ場所に
# よらず同じになる
EXTENSION_ID = "bgjnbcmfhbmefbabaecmnolmgeeocejn"


def extension_dir() -> Path:
    """同梱の拡張機能のディレクトリ（`--setup` で Chrome に読み込ませる場所）。"""
    return Path(__file__).parent / "browser_extension"


# ------------------------------------------------------------------ manifest と staging

MANIFEST_NAME = "manifest.json"
# 拡張機能が各手順の後に上書きする途中経過（manifest と同じ形に status が付く）
PROGRESS_NAME = "progress.json"
# 拡張機能が組織一覧の実行で書く一覧
ORGS_NAME = "orgs.json"
# コマンドが起動の前に書くその実行の計画（`--import` が計画を組み直すのに使う）
RUN_RECORD_NAME = "run.json"


@dataclass(frozen=True)
class ExportRecord:
    """manifest の結果 1 件（拡張機能が書いた内容。reason は拡張機能の文言のまま）。"""

    dir: str
    kind: str
    ok: bool
    filename: str | None
    reason: str | None


def run_dir(staging_dir: Path, run_id: str) -> Path:
    """実行ディレクトリ（`<staging>/<run_id>`。拡張機能のダウンロードはすべてこの下に入る）。"""
    return staging_dir / run_id


def manifest_path(staging_dir: Path, run_id: str) -> Path:
    """実行の manifest の置き場所（`<staging>/<run_id>/manifest.json`）。"""
    return run_dir(staging_dir, run_id) / MANIFEST_NAME


def progress_path(staging_dir: Path, run_id: str) -> Path:
    """途中経過の置き場所（`<staging>/<run_id>/progress.json`）。"""
    return run_dir(staging_dir, run_id) / PROGRESS_NAME


def orgs_path(staging_dir: Path, run_id: str) -> Path:
    """組織一覧の置き場所（`<staging>/<run_id>/orgs.json`）。"""
    return run_dir(staging_dir, run_id) / ORGS_NAME


def run_record_path(staging_dir: Path, run_id: str) -> Path:
    """実行の計画の置き場所（`<staging>/<run_id>/run.json`）。"""
    return run_dir(staging_dir, run_id) / RUN_RECORD_NAME


def staged_file(
    staging_dir: Path, run_id: str, target: ExportTarget, kind: str, filename: str
) -> Path:
    """拡張機能がダウンロードを置く場所（`<staging>/<run_id>/<dir>/<kind_dir>/<元ファイル名>`）。

    パスは計画の target から組む（manifest の dir は突き合わせにだけ使う）。
    """
    parts = [target.org] if target.workspace is None else [target.org, target.workspace]
    return staging_dir.joinpath(run_id, *parts, KIND_DIRS[kind], filename)


def _is_plain_name(value: object) -> bool:
    """ディレクトリもドライブも含まない単一のファイル名か（staging の外を指す名前を受け付けない）。

    区切り（`/`・`\\`）と NUL に加えて `:` も拒否する。Windows では `D:x.csv` が D ドライブの
    カレントディレクトリを指し、`x.csv:y` は別のデータストリームになるため。
    """
    if not isinstance(value, str) or value in ("", ".", ".."):
        return False
    if any(sep in value for sep in ("/", "\\", "\x00", ":")):
        return False
    windows = PureWindowsPath(value)
    return not windows.anchor and windows.name == value


def _manifest_problem(data: object) -> str | None:
    """manifest の形が取り決めと違えば、その箇所の説明（ファイル名に続ける文言）。"""
    if not isinstance(data, dict):
        return "の内容がオブジェクトではありません"
    if not (isinstance(data.get("run_id"), str) and isinstance(data.get("mode"), str)):
        return "に run_id と mode がありません"
    results = data.get("results")
    if not isinstance(results, list):
        return "に results の一覧がありません"
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            return f"の results[{index}] がオブジェクトではありません"
        for key in ("dir", "kind"):
            if not isinstance(item.get(key), str):
                return f"の results[{index}].{key} が文字列ではありません"
    return None


def _record(item: dict) -> ExportRecord:
    """manifest の要素 1 つを読む。ok なのに使えるファイル名が無いものは失敗に倒す。"""
    filename = item.get("filename")
    reason = item.get("reason") if isinstance(item.get("reason"), str) else None
    ok = item.get("ok") is True
    if ok and filename in (None, ""):
        ok, reason = False, "filename missing"
    elif ok and not _is_plain_name(filename):
        ok, reason = False, "invalid filename"
    return ExportRecord(
        dir=item["dir"],
        kind=item["kind"],
        ok=ok,
        filename=filename if _is_plain_name(filename) else None,
        reason=None if ok else reason,
    )


def read_manifest(path: Path) -> tuple[str, str, list[ExportRecord]]:
    """manifest.json を読み、(run_id, mode, 結果の一覧) を返す。

    壊れた JSON・results の欠落・辞書でない要素は ValueError（拡張機能との取り決めが
    崩れているので、部分的に読んで進めない）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} を JSON として読めません: {exc}") from None
    problem = _manifest_problem(data)
    if problem is not None:
        raise ValueError(f"{path.name} {problem}")
    records = [_record(item) for item in data["results"]]
    return data["run_id"], data["mode"], records


# 計画にあって manifest に結果の無い組み合わせの理由（拡張機能の文言と並べて表示する）
NO_RESULT_REASON = "no result in manifest"


def match_results(
    run: ProfileRun, records: Iterable[ExportRecord]
) -> list[tuple[ExportTarget, str, ExportRecord]]:
    """計画の (対象, 種別) ごとに manifest の結果を引き当てる（計画の順）。

    突き合わせは (dir, kind) で行い、同じ組み合わせが複数あれば後に書かれたものを採る。
    計画にあって結果が無い組み合わせは「結果なし」の失敗として返し、計画に無い結果は
    捨てる（計画に無い場所へは配置しない）。
    """
    found = {(record.dir, record.kind): record for record in records}
    paired = []
    for target in run.targets:
        for kind in target.kinds:
            record = found.get((target.dir, kind)) or ExportRecord(
                dir=target.dir, kind=kind, ok=False, filename=None, reason=NO_RESULT_REASON
            )
            paired.append((target, kind, record))
    return paired


def is_run_id(value: object) -> bool:
    """実行ディレクトリの名前として使えるか（区切りもドライブも含まない単一の名前）。

    `--import` に渡された識別子を staging の外を指す名前として受け付けないために使う。
    """
    return _is_plain_name(value)


def _write_json(path: Path, data: object) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} を JSON として読めません: {exc}") from None


def run_record(run: ProfileRun, run_id: str, created_at: dt.datetime) -> dict:
    """run.json の内容（その実行のプロファイル・モード・対象月と、拡張機能へ渡した実行内容）。"""
    return {
        "run_id": run_id,
        "profile": run.profile,
        "mode": run.mode,
        "month": run.month,
        "created_at": created_at.isoformat(timespec="seconds"),
        "spec": run_spec(run, run_id),
    }


def write_run_record(path: Path, record: dict) -> None:
    """run.json を書く。"""
    _write_json(path, record)


def read_run_record(path: Path) -> dict:
    """run.json を読む（中身の検査は `restore_run` が行う）。"""
    data = _read_json(path)
    if isinstance(data, dict):
        return data
    raise ValueError(f"{path.name} の内容がオブジェクトではありません")


_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def _spec_item_problem(item: object) -> str | None:
    """run.json の spec.orgs の要素の形が取り決めと違えば、その説明。"""
    if not isinstance(item, dict):
        return "がオブジェクトではありません"
    for key in ("uuid", "dir"):
        if not isinstance(item.get(key), str):
            return f".{key} が文字列ではありません"
    kinds = item.get("kinds")
    if not (isinstance(kinds, list) and kinds and all(kind in KINDS for kind in kinds)):
        return f".kinds は {' / '.join(KINDS)} の一覧が必要です"
    return None


def restore_run(record: dict, targets: Sequence[ExportTarget]) -> ProfileRun:
    """run.json の内容から、その実行の計画を組み直す（`--import` が検証・配置に使う）。

    対象は spec の (dir, uuid) を設定の対象（`gated_targets`）と突き合わせて決め、配置先は
    設定の側から組む（run.json に書かれた名前をそのままパスにしない）。設定に無い dir や、
    設定と UUID が違う dir があれば ValueError（設定が変わった後の取り込みで、別の
    スペースの CSV を置かないため）。種別はその実行で頼んだもの（spec の kinds）、対象月は
    その実行のもの（取り込む日の当月ではない）。
    """
    profile, mode, month = record.get("profile"), record.get("mode"), record.get("month")
    spec = record.get("spec")
    if not is_profile_name(profile):
        raise ValueError("run.json の profile がプロファイル名ではありません")
    if mode not in (MODE_CURRENT, MODE_PREVIOUS):
        raise ValueError(f"run.json の mode は {MODE_CURRENT} か {MODE_PREVIOUS} が必要です")
    if not (isinstance(month, str) and _MONTH_RE.fullmatch(month)):
        raise ValueError("run.json の month は YYYY-MM が必要です")
    if not (isinstance(spec, dict) and isinstance(spec.get("orgs"), list) and spec["orgs"]):
        raise ValueError("run.json の spec に対象（orgs）がありません")
    if spec.get("run_id") != record.get("run_id") or spec.get("mode") != mode:
        raise ValueError("run.json の spec が実行の run_id・mode と一致しません")

    by_dir = {target.dir: target for target in targets}
    restored = []
    for index, item in enumerate(spec["orgs"]):
        problem = _spec_item_problem(item)
        if problem is not None:
            raise ValueError(f"run.json の spec.orgs[{index}]{problem}")
        target = by_dir.get(item["dir"])
        if target is None:
            raise ValueError(
                f"run.json の対象 {item['dir']} は claude_export の設定にありません"
                "（設定を変えた後は、取り込まずに取得し直してください）"
            )
        if target.org_id != item["uuid"].lower():
            raise ValueError(
                f"run.json の対象 {item['dir']} の UUID が設定の org_id と違います"
                "（設定を変えた後は、取り込まずに取得し直してください）"
            )
        restored.append(ExportTarget(
            org=target.org,
            workspace=target.workspace,
            profile=profile,
            org_id=target.org_id,
            kinds=tuple(kind for kind in KINDS if kind in item["kinds"]),
        ))
    return ProfileRun(profile=profile, mode=mode, month=month, targets=tuple(restored))


@dataclass(frozen=True)
class OrgEntry:
    """組織一覧の 1 件（claude.ai の値のまま。無い項目は None）。"""

    uuid: str | None
    name: str | None
    rate_limit_tier: str | None
    plan: str | None


def _org_value(item: dict, key: str) -> str | None:
    value = item.get(key)
    return None if value is None else str(value)


def read_orgs(path: Path) -> tuple[list[OrgEntry], str | None]:
    """orgs.json を読み、(組織の一覧, 拡張機能が報告した失敗の理由) を返す。

    拡張機能は取得できれば一覧（配列）を、できなければ `{"error": 理由}` を書く。壊れた
    JSON やそれ以外の形は ValueError。
    """
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return [], data["error"]
    if not (isinstance(data, list) and all(isinstance(item, dict) for item in data)):
        raise ValueError(f"{path.name} の内容が組織の一覧ではありません")
    entries = [
        OrgEntry(*(_org_value(item, key) for key in ("uuid", "name", "rate_limit_tier", "plan")))
        for item in data
    ]
    return entries, None


# ------------------------------------------------------------------ 検証と配置

@dataclass(frozen=True)
class Verdict:
    """ダウンロードした CSV の検証結果。ok でなければ reason が最初に外れた理由。"""

    ok: bool
    reason: str | None


# ヘッダとして読む先頭行の上限（1 行目に改行が無い巨大なファイルを丸ごと読まない）。
# これを超えるヘッダは途中で切れたものとして検証を失敗にする
_HEADER_LIMIT = 64 * 1024


# ヘッダを読めなかった理由（_read_header の戻り）
_HEADER_TOO_LONG = "too_long"
_HEADER_UNDECODABLE = "undecodable"


def _read_header(path: Path) -> tuple[list[str], str | None]:
    """先頭行を CSV の 1 行として読み、各セルの引用符と前後の空白を落とす。

    戻りは (セルの一覧, 読めなかった理由)。上限まで読んでも行が終わらなければ
    `_HEADER_TOO_LONG`（途中で切れたヘッダで照合しない）、UTF-8 として読めなければ
    `_HEADER_UNDECODABLE`。本文は読まない（種別の判定に要るのはヘッダだけ）。
    """
    with path.open("rb") as f:
        raw = f.readline(_HEADER_LIMIT)
    if len(raw) >= _HEADER_LIMIT and not raw.endswith(b"\n"):
        return [], _HEADER_TOO_LONG
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], _HEADER_UNDECODABLE
    row = next(csv.reader([text.rstrip("\r\n")]), [])
    return [cell.strip().strip('"').strip() for cell in row], None


def _month_bounds(month: str) -> tuple[dt.date, dt.date]:
    first = dt.date.fromisoformat(f"{month}-01")
    next_first = (first + dt.timedelta(days=32)).replace(day=1)
    return first, next_first - dt.timedelta(days=1)


def _period_reason(
    period: ingest.FilePeriod, kind: str, month: str
) -> str | None:
    """ファイル名の期間が種別ごとの規則を満たさなければ、その理由。"""
    first, last = _month_bounds(month)
    label = _KIND_LABELS[kind]
    if kind == "members":
        if period.kind != "date":
            return f"{label}のファイル名にスナップショットの日付がありません"
        if period.start < first:
            return f"{label}の日付 {period.start} が対象月 {month} より前です"
        return None
    if period.kind != "range":
        return f"{label}のファイル名に期間（開始日 to 終了日）がありません"
    if period.start != first:
        return (
            f"{label}の期間 {period.start}〜{period.end} が対象月 {month} の1日から"
            "始まっていません"
        )
    # Claude Code analytics の終了日は部分月でも月末日になるので、支出レポートだけ見る
    if kind == "spend" and period.end > last:
        return f"{label}の期間 {period.start}〜{period.end} が対象月 {month} を越えています"
    return None


def verify_export(
    path: Path, kind: str, month: str, *, columns_aliases: dict
) -> Verdict:
    """ダウンロードした CSV が種別と対象月に合うかを確かめる（最初に外れた理由を返す）。

    確かめる順は、中身があること → ヘッダ（先頭行。`_HEADER_LIMIT` 以内）に種別ごとの
    正準列（`_KIND_COLUMNS`）がすべてあること（欠けていれば最初の 1 列を理由にする） →
    ファイル名の期間が対象月に合うこと。
    ヘッダの照合は分析の読み込み（`ingest.map_columns`）と同じ正規化で行う。
    columns_aliases は設定の columns。
    """
    if kind not in _KIND_COLUMNS:
        raise ValueError(f"未知の種別です: {kind}")
    label = _KIND_LABELS[kind]
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return Verdict(False, f"{label}のファイルがありません")
    if not path.is_file():
        return Verdict(False, f"{label}が通常のファイルではありません")
    if size == 0:
        return Verdict(False, f"{label}のファイルが空です")

    header, problem = _read_header(path)
    if problem == _HEADER_TOO_LONG:
        return Verdict(
            False, f"{label}のヘッダが長すぎます（{_HEADER_LIMIT // 1024} KiB 以内）")
    if problem == _HEADER_UNDECODABLE:
        return Verdict(False, f"{label}のヘッダを UTF-8 として読めません")
    section, required = _KIND_COLUMNS[kind]
    present = {ingest.normalize_header(cell) for cell in header}
    for canonical in required:
        aliases = (columns_aliases.get(section) or {}).get(canonical) or []
        candidates = {ingest.normalize_header(alias) for alias in aliases}
        candidates.add(ingest.normalize_header(canonical))
        if not present & candidates:
            return Verdict(
                False,
                f"{label}のヘッダに {canonical} に当たる列がありません"
                f"（columns.{section}.{canonical} のエイリアスと一致しません）",
            )

    try:
        period = ingest.file_period(path)
    except ValueError as exc:
        return Verdict(False, str(exc))
    if period is None:
        return Verdict(False, f"{label}のファイル名から期間を読み取れません")
    reason = _period_reason(period, kind, month)
    return Verdict(reason is None, reason)


def target_dir(input_dir: Path, target: ExportTarget) -> Path:
    """配置先の組織ディレクトリ（入れ子レイアウトなら workspace のディレクトリ）。"""
    base = input_dir / target.org
    return base if target.workspace is None else base / target.workspace


def place_export(src: Path, input_dir: Path, target: ExportTarget, kind: str) -> Path:
    """検証済みの CSV を元のファイル名のまま入力ディレクトリへコピーし、配置先を返す。

    配置先は計画の target から組む。組織ディレクトリ（入れ子なら workspace のディレクトリ）が
    無ければ作らずに ValueError（設定の綴り違いで新しい組織ができるのを防ぐ）。種別の
    ディレクトリは無ければ作る。同じディレクトリに一時名で書いてから置き換えるので、途中で
    失敗しても半端なファイルが入力に残らない。同名は上書きする（同じ日の再取得は新しい
    スナップショット）。
    """
    if kind not in KIND_DIRS:
        raise ValueError(f"未知の種別です: {kind}")
    # 名前の規則は分析と同じ（パス区切りや予約名で入力ディレクトリの外を指させない）
    ingest.validate_org_name(target.org)
    if target.workspace is not None:
        ingest.validate_org_name(target.workspace)
    base = target_dir(input_dir, target)
    if not base.is_dir():
        command = f"seat-analyzer init-org {target.org}"
        if target.workspace is not None:
            command += f" --workspaces {target.workspace}"
        raise ValueError(
            f"配置先のディレクトリがありません: {base}（{command} で作成してから"
            "再実行してください）"
        )
    kind_dir = base / KIND_DIRS[kind]
    kind_dir.mkdir(exist_ok=True)
    dest = kind_dir / src.name
    tmp = kind_dir / f".{src.name}.tmp"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return dest


# ------------------------------------------------------------------ Chrome のプロファイル設定

# 自動ダウンロードを許可するサイトのパターン（Chrome の content settings の書式）
_DOWNLOAD_PATTERN = "https://claude.ai:443,*"
# 許可の内容。last_modified 以外がこの値なら書き換えない（冪等にするため）
_ALLOW_DOWNLOADS = {"expiration": "0", "model": 0, "setting": 1}
# Chrome の時刻の起点（1601-01-01）から Unix 時刻の起点までの秒数
_CHROME_EPOCH_OFFSET = 11644473600


def profile_path(profiles_dir: Path, profile: str) -> Path:
    """プロファイル 1 つの user data ディレクトリ（`<profiles_dir>/<profile>`）。"""
    return profiles_dir / profile


def profile_preferences_path(profiles_dir: Path, profile: str) -> Path:
    """プロファイルの Preferences（`<profiles_dir>/<profile>/Default/Preferences`）。"""
    return profile_path(profiles_dir, profile) / "Default" / "Preferences"


def chrome_time_us(now: float) -> int:
    """Unix 時刻（秒）を Chrome の時刻（1601-01-01 からのマイクロ秒）にする。"""
    return int((now + _CHROME_EPOCH_OFFSET) * 1_000_000)


def _same(a: object, b: object) -> bool:
    """型まで同じ値か（0 と False を同じとみなさない）。"""
    return type(a) is type(b) and a == b


def _child(parent: dict, key: str) -> dict:
    """parent[key] の辞書（無ければ作る）。辞書でない値があれば ValueError。"""
    value = parent.get(key)
    if value is None:
        value = parent[key] = {}
    elif not isinstance(value, dict):
        raise ValueError(f"Preferences の {key} がオブジェクトではありません")
    return value


def update_preferences(prefs: dict, *, staging_dir: Path, now_chrome_us: int) -> bool:
    """Preferences の辞書へ、自動ダウンロードの許可とダウンロード先を書く。変更があれば True。

    書くのは claude.ai の自動ダウンロードの許可と、ダウンロードの確認を出さないこと・
    ダウンロード先を staging にすることだけで、他のキーはすべて保つ。許可が
    setting / model / expiration まで同じなら触らない（last_modified の違いだけで
    書き換えない）。
    """
    changed = False
    exceptions = _child(_child(_child(prefs, "profile"), "content_settings"), "exceptions")
    downloads = _child(exceptions, "automatic_downloads")
    current = downloads.get(_DOWNLOAD_PATTERN)
    if not (
        isinstance(current, dict)
        and all(_same(current.get(key), value) for key, value in _ALLOW_DOWNLOADS.items())
    ):
        downloads[_DOWNLOAD_PATTERN] = {
            "expiration": _ALLOW_DOWNLOADS["expiration"],
            "last_modified": str(now_chrome_us),
            "model": _ALLOW_DOWNLOADS["model"],
            "setting": _ALLOW_DOWNLOADS["setting"],
        }
        changed = True
    download = _child(prefs, "download")
    for key, value in (
        ("prompt_for_download", False),
        ("default_directory", str(staging_dir)),
        ("directory_upgrade", True),
    ):
        if not _same(download.get(key), value):
            download[key] = value
            changed = True
    return changed


def preferences_ready(prefs: dict, staging_dir: Path) -> bool:
    """Preferences のダウンロード先が staging を指しているか（`--finish-setup` が済んでいるか）。

    比較は区切りの重なりや `.` を畳んでから行う（Windows は大文字小文字も区別しない）。
    """
    download = prefs.get("download")
    value = download.get("default_directory") if isinstance(download, dict) else None
    if not isinstance(value, str) or not value:
        return False

    def key(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    return key(value) == key(str(staging_dir))


def read_preferences(path: Path) -> dict:
    """プロファイルの Preferences を読む（無ければ FileNotFoundError、壊れていれば ValueError）。"""
    return _parse_preferences(path, path.read_bytes())


def secure_preferences_path(profiles_dir: Path, profile: str) -> Path:
    """プロファイルの Secure Preferences（`<profiles_dir>/<profile>/Default/Secure Preferences`）。

    Chrome は拡張機能の設定をこちらに書くことがある（`extension_install_state` が読む）。
    """
    return profile_path(profiles_dir, profile) / "Default" / "Secure Preferences"


# 拡張機能の読み込みの状態（`extension_install_state` の戻り）
EXTENSION_OK = "ok"
EXTENSION_OTHER_PATH = "other_path"
EXTENSION_MISSING = "missing"


def extension_install_state(prefs_dicts: Iterable[object], expected_dir: Path) -> str:
    """同梱の拡張機能がプロファイルに読み込まれているか。

    prefs_dicts は Secure Preferences と Preferences の内容（読めなかったものは渡さない）。
    どちらかの `extensions.settings[EXTENSION_ID].path` が expected_dir（同梱の拡張機能の
    場所）と一致すれば `EXTENSION_OK`、同じ ID が別の場所から読み込まれていれば
    `EXTENSION_OTHER_PATH`、どちらにも無ければ `EXTENSION_MISSING`。パスの比較は区切りの
    重なりや `.` を畳んでから行う（Windows は大文字小文字も区別しない）。
    """
    def key(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    found_elsewhere = False
    for prefs in prefs_dicts:
        extensions = prefs.get("extensions") if isinstance(prefs, dict) else None
        settings = extensions.get("settings") if isinstance(extensions, dict) else None
        entry = settings.get(EXTENSION_ID) if isinstance(settings, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path:
            continue
        if key(path) == key(str(expected_dir)):
            return EXTENSION_OK
        found_elsewhere = True
    return EXTENSION_OTHER_PATH if found_elsewhere else EXTENSION_MISSING


def _parse_preferences(path: Path, raw: bytes) -> dict:
    """Preferences の中身を辞書として読む（JSON のオブジェクトでなければ ValueError）。"""
    try:
        prefs = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} を JSON として読めません: {exc}") from None
    if isinstance(prefs, dict):
        return prefs
    raise ValueError(f"{path} の内容がオブジェクトではありません")


def write_preferences(path: Path, *, staging_dir: Path, now: float) -> bool:
    """プロファイルの Preferences を更新する。変更があれば True（Chrome の終了中に呼ぶ）。

    変更があるときだけ書き、書く前に元の内容を `Preferences.bak` へ残す。ファイルが
    無い（一度も起動していないプロファイル）なら作らずに ValueError にする。Chrome が初回の
    起動で書く既定の項目を欠いた Preferences をこちらで作ると、その状態で起動したときの
    振る舞いが分からないため、プロファイルは Chrome に作らせる。
    """
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(
            f"{path} がありません。プロファイルを Chrome で一度起動してから実行してください"
        ) from None
    prefs = _parse_preferences(path, original)
    if not update_preferences(prefs, staging_dir=staging_dir, now_chrome_us=chrome_time_us(now)):
        return False
    path.with_name(path.name + ".bak").write_bytes(original)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(prefs, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8", newline="\n",
    )
    os.replace(tmp, path)
    return True


# ------------------------------------------------------------------ Chrome の場所・起動・終了

# OS ごとの既定の場所（設定の chrome_path が空のとき順に探す）
_DARWIN_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_WINDOWS_CHROME_BASES = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
_WINDOWS_CHROME_PARTS = ("Google", "Chrome", "Application", "chrome.exe")
_UNIX_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def expand_setting_path(value: str) -> Path:
    """設定に書かれたパス。`~` を展開する。

    上書きファイルに書いた profiles_dir・staging_dir の相対パスは、ロード時に設定ファイルの
    置き場所を基準に解決済み（config の `_rebase_paths`）。chrome_path は絶対パスか空文字に
    限る（ロード時に検査する）。
    """
    try:
        return Path(value).expanduser()
    except RuntimeError:
        raise ValueError(f"'{value}' の '~' を展開できません（ホームディレクトリが不明です）") from None


def find_chrome(
    configured: str,
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    is_file: Callable[[Path], bool] = Path.is_file,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """Chrome の実行ファイル。見つからなければ None。

    configured（設定の chrome_path）が空でなければそれだけを見る（存在しなければ None）。
    空なら OS ごとの既定の場所を順に探す: macOS は /Applications、Windows は
    ProgramFiles・ProgramFiles(x86)・LOCALAPPDATA の各 Google\\Chrome\\Application、
    それ以外は PATH 上の google-chrome・google-chrome-stable・chromium・chromium-browser。
    """
    if configured:
        path = expand_setting_path(configured)
        return path if is_file(path) else None
    if platform == "darwin":
        candidates = [_DARWIN_CHROME]
    elif platform == "win32":
        candidates = [
            Path(base, *_WINDOWS_CHROME_PARTS)
            for var in _WINDOWS_CHROME_BASES
            if (base := environ.get(var))
        ]
    else:
        for name in _UNIX_CHROME_NAMES:
            found = which(name)
            if found:
                return Path(found)
        return None
    return next((path for path in candidates if is_file(path)), None)


def launch_command(chrome: Path, profile_dir: Path, url: str) -> list[str]:
    """専用プロファイルで Chrome を起動し、トリガー URL を開くコマンド。"""
    return [
        str(chrome),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]


def launch_chrome(command: Sequence[str], *, platform: str = sys.platform) -> None:
    """Chrome を切り離して起動する（終了を待たない。出力も読まない）。

    Windows では新しいプロセスグループの切り離されたプロセスとして、それ以外では新しい
    セッションとして起動する（このコマンドの終了や Ctrl+C を Chrome へ伝えないため）。
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(list(command), **kwargs)


def process_listing_command(platform: str) -> list[str]:
    """全プロセスの pid とコマンドラインを列挙するコマンド。

    Windows は出力の文字コードを UTF-8 にしてから列挙する（読む側は UTF-8 で読むので、
    ASCII 以外を含むプロファイルのパスも照合できるようにする）。
    """
    if platform == "win32":
        return [
            "powershell", "-NoProfile", "-Command",
            (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                " Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine"
                " | ConvertTo-Csv -NoTypeInformation"
            ),
        ]
    return ["ps", "-axo", "pid=,command="]


def _unix_rows(listing: str) -> list[tuple[int, str]]:
    """ps の出力（"pid コマンドライン" の行）を (pid, コマンドライン) にする。"""
    rows = []
    for line in listing.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    return rows


def _windows_rows(listing: str) -> list[tuple[int, str]]:
    """PowerShell の CSV（ヘッダ行あり・値は引用符つき）を (pid, コマンドライン) にする。"""
    rows = []
    pid_at = command_at = None
    for row in csv.reader(io.StringIO(listing)):
        if not row:
            continue
        if pid_at is None:
            if "ProcessId" in row and "CommandLine" in row:
                pid_at, command_at = row.index("ProcessId"), row.index("CommandLine")
            continue
        if len(row) <= max(pid_at, command_at) or not row[pid_at].strip().isdigit():
            continue
        rows.append((int(row[pid_at].strip()), row[command_at]))
    return rows


def _uses_profile(command: str, profile: str) -> bool:
    """コマンドラインが `--user-data-dir=<profile>` をその引数として含むか。

    パスの直後が引数の終わりであることまで見る（`corp` のプロファイルで `corp2` や
    `corp backup` を拾わないため）。引数の終わりとみなすのは次の場合だけ:

    - 値を引用符で囲む形（`--user-data-dir="<path>"`）と、引数全体を引用符で囲む形
      （`"--user-data-dir=<path>"`。Windows で空白を含む引数を渡すとこの形になる）は、
      直後が閉じの `"` のとき
    - 引用符の無い形は、直後が行末か、空白の後に次のフラグ（`-`）が続くとき。空白を含む
      パスは needle に含まれるので、このままで一致する
    """
    for needle, value_quoted in (
        (f'--user-data-dir="{profile}', True),
        (f"--user-data-dir={profile}", False),
    ):
        start = command.find(needle)
        while start != -1:
            end = start + len(needle)
            if value_quoted or (start > 0 and command[start - 1] == '"'):
                if command[end:end + 1] == '"':
                    return True
            else:
                rest = command[end:]
                if not rest or (rest[0].isspace() and rest.lstrip().startswith("-")):
                    return True
            start = command.find(needle, start + 1)
    return False


def chrome_pids(listing: str, profile_dir: PurePath | str, *, platform: str) -> list[int]:
    """プロセスの一覧から、そのプロファイルで動く Chrome の親プロセスの pid を拾う。

    子プロセス（レンダラ等。コマンドラインに `--type=` を持つ）は除く。親を終了させれば
    子も終わる。Windows はパスの大文字小文字を区別しない。
    """
    windows = platform == "win32"
    rows = _windows_rows(listing) if windows else _unix_rows(listing)
    profile = str(profile_dir)
    if windows:
        profile = profile.lower()
    return [
        pid for pid, command in rows
        if "--type=" not in command
        and _uses_profile(command.lower() if windows else command, profile)
    ]


def terminate_commands(
    pids: Sequence[int], *, platform: str, force: bool
) -> list[list[str]] | None:
    """プロセスを終了させるコマンド。Unix は None（シグナルを送る側が os.kill を使う）。"""
    if platform != "win32":
        return None
    extra = ["/F"] if force else []
    return [["taskkill", "/PID", str(pid), *extra] for pid in pids]


def list_chrome_pids(profile_dir: Path, *, platform: str = sys.platform) -> list[int]:
    """そのプロファイルで動いている Chrome の親プロセスの pid（無ければ空）。"""
    proc = subprocess.run(
        process_listing_command(platform),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return chrome_pids(
        proc.stdout.decode("utf-8", errors="replace"), profile_dir, platform=platform
    )


def _signal(pids: Iterable[int], *, platform: str, force: bool) -> None:
    """終了の要求（force なら強制終了）を送る。既に終わったプロセスは無視する。"""
    pids = list(pids)
    commands = terminate_commands(pids, platform=platform, force=force)
    if commands is not None:
        for command in commands:
            subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            )
        return
    sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def _alive(pids: Sequence[int], *, platform: str) -> list[int]:
    """pids のうちまだ動いているもの。"""
    if platform == "win32":
        proc = subprocess.run(
            process_listing_command(platform),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        listed = {pid for pid, _ in _windows_rows(proc.stdout.decode("utf-8", errors="replace"))}
        return [pid for pid in pids if pid in listed]
    alive = []
    for pid in pids:
        # 自分が起動した子なら回収する（回収されないまま終わったプロセスは、シグナルを
        # 送れる状態のまま残って「動いている」に見える）
        with contextlib.suppress(ChildProcessError, OSError):
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        alive.append(pid)
    return alive


def terminate_chrome(
    pids: Sequence[int], *, grace_seconds: float = 15.0, platform: str = sys.platform
) -> None:
    """Chrome を終了させる。穏当に終了を求め、grace_seconds 待って残っていれば強制する。

    pids が空なら何もしない。
    """
    if not pids:
        return
    _signal(pids, platform=platform, force=False)
    deadline = time.monotonic() + grace_seconds
    remaining = list(pids)
    while True:
        remaining = _alive(remaining, platform=platform)
        if not remaining:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    _signal(remaining, platform=platform, force=True)
