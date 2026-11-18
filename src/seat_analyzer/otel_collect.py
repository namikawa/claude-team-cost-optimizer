"""Claude Code の OpenTelemetry イベントを BigQuery から月単位で取得する。

Cloud Logging のシンクの形のイベント表だけを読み、人 × 日（UTC）× 区分に SQL で
集計した件数とトークン数を保存する。名前を持ちうる属性は区分か有無に畳み、本文・
応答・ツールの引数・パス・費用は参照しない。セッション ID もローカルへ持ち込まない。
表の定義、読み取り量の見積もり、結果の全件検査の順で進め、失敗時は保存しない。

認証は bq / gcloud に委ね、token は取り出さない。bq の出力は解釈と失敗の表示にだけ
使い、生の応答や SQL をキャッシュへ保存しない。読み戻しでは現在の取得元と UUID を
照合し、異なる設定で取得したキャッシュを完全な結果として使わせない。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

OTEL_CACHE_DIRNAME = "otel-cache"
OTEL_CACHE_SCHEMA = 1

# SQL が参照してよい列。必須・任意の区別もこの表だけで定義する。
SQL_COLUMNS = {
    "timestamp": True,
    "labels.event_name": True,
    "labels.user_email": True,
    "labels.organization_id": True,
    "labels.session_id": True,
    **{f"labels.{name}": False for name in (
        "terminal_type", "model", "query_source", "effort", "agent_name", "skill_name",
        "mcp_server_name", "plugin_name", "tool_name", "tool_source", "decision", "source",
        "permission_mode", "command_source", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
    )},
}
OPTIONAL_ATTRIBUTES = tuple(name.removeprefix("labels.") for name, required in SQL_COLUMNS.items()
                            if not required)

# SQL で畳んだ値の語彙。取得と読み戻しの双方で同じ表を使う。
VOCABULARIES = {
    "terminal": ("interactive", "non_interactive"),
    "origin": ("main", "sdk", "subagent_builtin", "subagent_custom", "other"),
    "effort": ("low", "medium", "high", "xhigh", "max", "none", "other"),
    "tool": ("edit", "shell", "mcp", "agent", "skill", "other"),
    "decision": ("accept", "reject", "other"),
    "source": ("config", "hook", "user_permanent", "user_temporary", "user_abort",
               "user_reject", "other"),
    "permission_mode": ("default", "plan", "acceptEdits", "bypassPermissions", "dontAsk",
                        "auto", "none", "other"),
    "command_source": ("builtin", "custom", "mcp", "none", "other"),
}
BOOL_ATTRIBUTES = ("agent", "skill", "mcp", "plugin")
TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
EVENT_ATTRIBUTES = {
    "api_request": ("model", "origin", "effort", *BOOL_ATTRIBUTES),
    "tool_decision": ("tool", "decision", "source", "permission_mode"),
    "user_prompt": ("command_source",),
}
ATTRIBUTE_COLUMNS = ("terminal", "model", "origin", "effort", *BOOL_ATTRIBUTES,
                     "tool", "decision", "source", "permission_mode", "command_source")
RESULT_COLUMNS = ("kind", "date", "email", "name", *ATTRIBUTE_COLUMNS, "count",
                  *TOKEN_COLUMNS, "total_rows")
EXCLUDED_KINDS = ("organization_empty", "organization_other", "email_empty")
_NAME_RE = re.compile(r"[a-z0-9_]{1,64}")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]+")
_SLOT_RE = re.compile(r"\{\{([^{}]+)\}\}")

UNREADABLE = "bq の出力を解釈できませんでした"
TOO_LARGE = "読み取り量が上限（otel.maximum_bytes_billed）を超えました"
NOT_FOUND = "bq コマンドが見つかりません（Google Cloud SDK を導入して PATH に置いてください）"
TIMEOUT = "bq の応答がありません"


@dataclass(frozen=True)
class TableLocation:
    """取得するイベント表の場所（検査済みの project・dataset・table）。

    各名前は英数字と _・- だけで、SQL に埋め込んでも表名の外へ出ない形に限る。
    メトリクスの表は持たず、1組織につきイベント表1つだけを参照する。
    """

    project: str
    dataset: str
    table: str


@dataclass(frozen=True)
class OtelTarget:
    """有効な組織の取得元と、絞り込みに使う小文字の UUID（設定の記述順）。"""

    source: TableLocation
    organization_ids: tuple[str, ...]


@dataclass(frozen=True)
class EventRow:
    """人 × UTC 日 × 集計名 × 区分の集計（件数は正の整数）。

    attrs はキー順の組で、terminal とその集計名の区分だけを持つ。
    トークンは api_request の行だけが持ち、属性が無い場合の NULL は 0 にそろえる。
    """

    email: str
    date: str
    name: str
    attrs: tuple[tuple[str, str | bool], ...]
    count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


@dataclass(frozen=True)
class SessionRow:
    """人 × UTC 日 × 端末の種類ごとのセッション数（正の整数）。

    日付は月内の最初の api_request の日で、ID 自体は保存しない。
    """

    email: str
    date: str
    terminal: str
    count: int


@dataclass(frozen=True)
class OtelCache:
    """1組織・1か月の完全な集計（schema 1）。

    rows と sessions は一意キー順、除外件数は EXCLUDED_KINDS の順に持つ。
    scanned_events はイベント件数と除外件数の和で、セッション数は含めない。
    取得元・UUID・表に無い属性と、見積もり byte・実行日時 UTC を併記する。
    """

    organization: str
    month: str
    source: TableLocation
    organization_ids: tuple[str, ...]
    scanned_events: int
    excluded_events: tuple[tuple[str, int], ...]
    absent_attributes: tuple[str, ...]
    estimated_bytes: int
    executed_at: str
    rows: tuple[EventRow, ...]
    sessions: tuple[SessionRow, ...]
    schema: int = OTEL_CACHE_SCHEMA


@dataclass(frozen=True)
class BqResult:
    """bq を1回呼んだ結果。生出力は分類と解釈にだけ使い、保存しない。

    failure は起動・待機・UTF-8 の解釈に失敗した理由。起動できた場合の成否は
    returncode で判断し、標準出力と標準エラーの両方から分類する。
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    failure: str | None = None


# 引数・標準入力の SQL・秒単位の応答待ち上限を受ける（テストでは実機を呼ばない）。
Runner = Callable[[Sequence[str], str | None, int], BqResult]


def organization_ids(entry: dict) -> tuple[str, ...]:
    """絞り込みの UUID を小文字で返す（検査済みの組織設定を受ける）。

    otel の明示指定を優先し、空なら組織直下・各 workspace の claude_export.org_id を
    記述順に集める。継承時の重複は除き、明示指定の重複の検出は config に委ねる。
    """
    explicit = entry.get("otel", {}).get("organization_ids", [])
    if explicit:
        return tuple(value.lower() for value in explicit)
    sections = [entry.get("claude_export", {})]
    sections.extend(value.get("claude_export", {})
                    for value in entry.get("workspaces", {}).values())
    return tuple(dict.fromkeys(section["org_id"].lower() for section in sections
                               if section.get("org_id")))


def is_bigquery_name(value: object) -> bool:
    """SQL に埋め込める表の名前か（英数字と _・- の1字以上）。"""
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def gated_orgs(cfg: dict) -> dict[str, OtelTarget]:
    """OpenTelemetry の取得を有効にした組織 → 表と UUID（設定の記述順）。

    3つの表名が空の組織は対象外。名前の字句と UUID の有無はロード時に検査済み。
    """
    targets = {}
    for org, entry in cfg["organizations"].items():
        bq = entry.get("otel", {}).get("bigquery", {})
        if bq.get("project"):
            targets[org] = OtelTarget(
                TableLocation(bq["project"], bq["dataset"], bq["logs_table"]),
                organization_ids(entry),
            )
    return targets


def run_bq(args: Sequence[str], sql: str | None, timeout: int) -> BqResult:
    """PATH 上の bq の実体を1回実行し、UTF-8 strict で両方の出力を読む。

    認証は gcloud に任せ、token も利用者の設定も読み出さない。SQL は標準入力で渡す。
    利用者の .bigqueryrc を抑止する引数は呼び出し側で毎回組み立てる。
    """
    resolved = shutil.which("bq")
    if resolved is None:
        return BqResult(failure=NOT_FOUND)
    try:
        proc = subprocess.run(
            [resolved, *args], input=None if sql is None else sql.encode("utf-8"),
            capture_output=True, timeout=timeout, check=False,
        )
        return BqResult(proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8"))
    except FileNotFoundError:
        return BqResult(failure=NOT_FOUND)
    except subprocess.TimeoutExpired:
        return BqResult(failure=TIMEOUT)
    except UnicodeError:
        return BqResult(failure=UNREADABLE)
    except OSError:
        return BqResult(failure="bq の実行に失敗しました")


def failure_reason(result: BqResult) -> str:
    """失敗の文を空白を畳んで上から分類し、未知の場合だけ先頭3行を添える。"""
    if result.failure is not None:
        return result.failure
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    collapsed = " ".join(output.split())
    patterns = (
        (("Reauthentication", "gcloud auth login", "refreshing your current auth tokens",
          "do not currently have an active account"),
         "bq の認証がありません（gcloud auth login を実行してください）"),
        (("Not found:", "is not found", "Access Denied", "Permission denied", "does not have"),
         "project・dataset・表が見つからないか、読む権限がありません"),
        (("exceeded limit for bytes billed",), TOO_LARGE),
    )
    for phrases, reason in patterns:
        if any(phrase in collapsed for phrase in phrases):
            return reason
    first = "\n".join(output.splitlines()[:3])
    return "bq の実行に失敗しました" + (f"\n{first}" if first else "")


def _output_error(detail: str = UNREADABLE) -> ValueError:
    """利用者からの出力の型・形の誤りを CLI が扱える例外にそろえる。"""
    return ValueError(detail)


def _request(run: Runner, args: Sequence[str], sql: str | None, timeout: int) -> object:
    result = run(args, sql, timeout)
    if result.failure is not None or result.returncode != 0:
        raise _output_error(failure_reason(result))
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise _output_error() from exc


def inspect_schema(value: object) -> tuple[str, ...]:
    """表の必須列を確認し、無い任意属性の名前を昇順で返す。

    timestamp は最上位の TIMESTAMP、labels は最上位の RECORD に限る。
    関係のない列は参照せず、SQL の許可リストにある名前だけを拾う。
    """
    if not isinstance(value, list) or not all(isinstance(field, dict) for field in value):
        raise _output_error()
    top = {field.get("name"): field for field in value if isinstance(field.get("name"), str)}
    present = set()
    if top.get("timestamp", {}).get("type") == "TIMESTAMP":
        present.add("timestamp")
    labels = top.get("labels", {})
    if labels.get("type") == "RECORD":
        fields = labels.get("fields")
        if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
            raise _output_error()
        present.update(f"labels.{field['name']}" for field in fields
                       if isinstance(field.get("name"), str))
    missing = sorted(name for name, required in SQL_COLUMNS.items()
                     if required and name not in present)
    if missing:
        raise _output_error("集計に必須の列が表にありません: " + ", ".join(missing))
    return tuple(sorted(name for name in OPTIONAL_ATTRIBUTES if f"labels.{name}" not in present))


def build_sql(source: TableLocation, absent_attributes: Sequence[str] = ()) -> str:
    """同梱の SQL の表名と任意属性だけを置換する。

    正規表現の波括弧や $ はそのままにし、未知の差し込み口と置換残しは拒否する。
    値は問い合わせの引数で渡すため、この関数には月も UUID も渡さない。
    """
    if not all(is_bigquery_name(value)
               for value in (source.project, source.dataset, source.table)):
        raise ValueError("BigQuery の表名が不正です")
    if not set(absent_attributes) <= set(OPTIONAL_ATTRIBUTES):
        raise ValueError("未知の任意属性があります")
    replacements = {"table": f"{source.project}.{source.dataset}.{source.table}"}
    replacements.update({name: "CAST(NULL AS STRING)" if name in absent_attributes
                         else f"labels.{name}" for name in OPTIONAL_ATTRIBUTES})
    template = (Path(__file__).parent / "sql" / "otel_events.sql").read_text(encoding="utf-8")

    def replace(match: re.Match) -> str:
        if match[1] not in replacements:
            raise ValueError(f"SQL に未知の差し込み口があります: {match[1]}")
        return replacements[match[1]]

    sql = _SLOT_RE.sub(replace, template)
    if "{{" in sql or "}}" in sql:
        raise ValueError("SQL の差し込み口が残っています")
    return sql


def _integer(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _bq_integer(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise _output_error()
    try:
        return int(value)
    except ValueError as exc:
        raise _output_error() from exc


def _keys(value: object, expected: Sequence[str]) -> None:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise _output_error()


def _month_bounds(month: str) -> tuple[dt.date, dt.date]:
    if not isinstance(month, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) is None:
        raise ValueError("対象月は YYYY-MM の形で指定してください")
    try:
        start = dt.date.fromisoformat(month + "-01")
        end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return start, end
    except (ValueError, OverflowError) as exc:
        raise ValueError("対象月が不正です") from exc


def _person_day(row: dict, month: str) -> None:
    start, end = _month_bounds(month)
    value = row["date"]
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise _output_error()
    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise _output_error() from exc
    if not start <= day < end or not isinstance(row["email"], str) or not row["email"].strip():
        raise _output_error()
    if row["terminal"] not in VOCABULARIES["terminal"]:
        raise _output_error()


def _group(row: dict, month: str) -> EventRow | SessionRow | int:
    """型をそろえた1行を検査する（取得結果とキャッシュの読み戻しで共用）。"""
    kind, count = row["kind"], row["count"]
    allowed = {"kind", "count", "total_rows"}
    if kind == "scanned" or kind in tuple(f"excluded_{name}" for name in EXCLUDED_KINDS):
        if not _integer(count, 0 if kind == "scanned" else 1):
            raise _output_error()
        result = count
    elif kind in ("event", "session"):
        _person_day(row, month)
        if not _integer(count, 1):
            raise _output_error()
        allowed.update(("date", "email", "terminal"))
        if kind == "session":
            result = SessionRow(row["email"], row["date"], row["terminal"], count)
        else:
            name = row["name"]
            if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
                raise _output_error()
            attrs = ("terminal", *EVENT_ATTRIBUTES.get(name, ()))
            allowed.update(("name", *attrs))
            for attr in attrs:
                value = row[attr]
                if attr in BOOL_ATTRIBUTES:
                    valid = isinstance(value, bool)
                elif attr == "model":
                    valid = isinstance(value, str) and _MODEL_RE.fullmatch(value)
                else:
                    valid = value in VOCABULARIES[attr]
                if not valid:
                    raise _output_error()
            tokens = {}
            if name == "api_request":
                allowed.update(TOKEN_COLUMNS)
                for column in TOKEN_COLUMNS:
                    value = row[column]
                    if value is not None and not _integer(value):
                        raise _output_error()
                    tokens[column] = 0 if value is None else value
            result = EventRow(row["email"], row["date"], name,
                              tuple(sorted((attr, row[attr]) for attr in attrs)), count, **tokens)
    else:
        raise _output_error()
    if any(value is not None for key, value in row.items() if key not in allowed):
        raise _output_error()
    return result


def _event_order(row: EventRow) -> tuple[str, str, str, str]:
    return row.email, row.date, row.name, json.dumps(dict(row.attrs), sort_keys=True)


def _session_order(row: SessionRow) -> tuple[str, str, str]:
    return row.email, row.date, row.terminal


def _totals(rows: Sequence[EventRow], excluded: dict[str, int], scanned: int) -> None:
    if sum(row.count for row in rows) + sum(excluded.values()) != scanned:
        raise _output_error("イベントの件数の合計が合いません")


def _unique(rows: Sequence, key: Callable) -> None:
    keys = [key(row) for row in rows]
    if len(set(keys)) != len(keys):
        raise _output_error()


def parse_result(value: object, month: str) -> tuple[
    tuple[EventRow, ...], tuple[SessionRow, ...], int, tuple[tuple[str, int], ...]
]:
    """bq の全結果を検査し、並びを固定したイベント・セッション・件数を返す。

    列の過不足、型、語彙、月、一意キー、返された行数と件数の合計をすべて検査する。
    不正な行や途中までの結果を返さず、キャッシュへ部分結果が流れないようにする。
    """
    if not isinstance(value, list) or not value:
        raise _output_error()
    totals = []
    for row in value:
        _keys(row, RESULT_COLUMNS)
        totals.append(_bq_integer(row["total_rows"]))
    if len(set(totals)) != 1 or totals[0] != len(value):
        raise _output_error(
            "受け取った行数が、問い合わせの返した行数と合いません"
            f"（{len(value)} / {totals[0]}）"
        )
    rows, sessions, counts = [], [], {}
    for raw in value:
        row = dict(raw)
        for column in ("count", *TOKEN_COLUMNS):
            if row[column] is not None:
                row[column] = _bq_integer(row[column])
        for column in BOOL_ATTRIBUTES:
            if row[column] is not None:
                if not isinstance(row[column], str) or row[column] not in ("true", "false"):
                    raise _output_error()
                row[column] = row[column] == "true"
        result = _group(row, month)
        if isinstance(result, EventRow):
            rows.append(result)
        elif isinstance(result, SessionRow):
            sessions.append(result)
        else:
            if row["kind"] in counts:
                raise _output_error()
            counts[row["kind"]] = result
    if "scanned" not in counts:
        raise _output_error()
    excluded = {name: counts.get(f"excluded_{name}", 0) for name in EXCLUDED_KINDS}
    _totals(rows, excluded, counts["scanned"])
    _unique(rows, _event_order)
    _unique(sessions, _session_order)
    return (tuple(sorted(rows, key=_event_order)), tuple(sorted(sessions, key=_session_order)),
            counts["scanned"], tuple(excluded.items()))


def _cache_payload(cache: OtelCache) -> dict:
    rows = []
    for row in cache.rows:
        entry = {"email": row.email, "date": row.date, "name": row.name,
                 "attrs": dict(row.attrs), "count": row.count}
        if row.name == "api_request":
            entry.update({column: getattr(row, column) for column in TOKEN_COLUMNS})
        rows.append(entry)
    return {
        "schema": cache.schema, "organization": cache.organization, "month": cache.month,
        "source": {"project": cache.source.project, "dataset": cache.source.dataset,
                   "table": cache.source.table},
        "organization_ids": list(cache.organization_ids), "scanned_events": cache.scanned_events,
        "excluded_events": dict(cache.excluded_events),
        "absent_attributes": list(cache.absent_attributes),
        "query": {"estimated_bytes": cache.estimated_bytes, "executed_at": cache.executed_at},
        "rows": rows,
        "sessions": [{"email": row.email, "date": row.date, "terminal": row.terminal,
                      "count": row.count} for row in cache.sessions],
    }


def otel_cache_path(cache_dir: Path, month: str) -> Path:
    """月の字句を検査して otel-YYYY-MM.json のパスを返す。"""
    _month_bounds(month)
    return cache_dir / f"otel-{month}.json"


def _write_cache(path: Path, cache: OtelCache) -> None:
    """全件検査済みの結果を同じディレクトリの一時ファイルから原子的に置換する。

    UTF-8・LF・キー順・末尾改行を固定する。既存ファイルの権限を引き継ぎ、新規作成は
    umask を反映する。組織ディレクトリは作らず、キャッシュのディレクトリだけを作る。
    置換に失敗した場合も元のファイルを残し、一時ファイルを取り除く。
    """
    text = json.dumps(_cache_payload(cache), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        mode = path.stat().st_mode & 0o7777
    else:
        current = os.umask(0)
        os.umask(current)
        mode = 0o666 & ~current
    tmp = None
    try:
        f = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=".otel-", suffix=".tmp", delete=False,
        )
        tmp = Path(f.name)
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _read_cache(value: object, organization: str, month: str, target: OtelTarget) -> OtelCache:
    _keys(value, ("schema", "organization", "month", "source", "organization_ids",
                  "scanned_events", "excluded_events", "absent_attributes", "query",
                  "rows", "sessions"))
    if (not _integer(value["schema"]) or value["schema"] != OTEL_CACHE_SCHEMA
            or value["organization"] != organization or value["month"] != month):
        raise _output_error()
    _keys(value["source"], ("project", "dataset", "table"))
    actual = TableLocation(**value["source"])
    if actual != target.source:
        raise _output_error("取得元の表が今の設定と違います")
    org_ids = value["organization_ids"]
    if (not isinstance(org_ids, list) or not org_ids
            or not all(isinstance(item, str) for item in org_ids)
            or any(item != item.lower() for item in org_ids)
            or len(set(org_ids)) != len(org_ids) or set(org_ids) != set(target.organization_ids)):
        raise _output_error("組織 UUID が今の設定と違います")
    absent = value["absent_attributes"]
    if (not isinstance(absent, list) or not all(isinstance(item, str) for item in absent)
            or not set(absent) <= set(OPTIONAL_ATTRIBUTES) or len(set(absent)) != len(absent)):
        raise _output_error()
    _keys(value["query"], ("estimated_bytes", "executed_at"))
    query = value["query"]
    if not _integer(query["estimated_bytes"]) or not isinstance(query["executed_at"], str):
        raise _output_error()
    try:
        dt.datetime.strptime(query["executed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError as exc:
        raise _output_error() from exc
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                        query["executed_at"]):
        raise _output_error()
    _keys(value["excluded_events"], EXCLUDED_KINDS)
    excluded = value["excluded_events"]
    if not _integer(value["scanned_events"]) or not all(_integer(n) for n in excluded.values()):
        raise _output_error()
    rows, sessions = [], []
    for kind, entries in (("event", value["rows"]), ("session", value["sessions"])):
        if not isinstance(entries, list):
            raise _output_error()
        for entry in entries:
            if not isinstance(entry, dict):
                raise _output_error()
            row = dict.fromkeys(RESULT_COLUMNS)
            row.update(kind=kind)
            if kind == "session":
                _keys(entry, ("email", "date", "terminal", "count"))
                row.update(entry)
                sessions.append(_group(row, month))
            else:
                name = entry.get("name")
                token_keys = TOKEN_COLUMNS if name == "api_request" else ()
                _keys(entry, ("email", "date", "name", "attrs", "count", *token_keys))
                if not isinstance(name, str):
                    raise _output_error()
                _keys(entry["attrs"], ("terminal", *EVENT_ATTRIBUTES.get(name, ())))
                row.update({key: entry[key] for key in ("email", "date", "name", "count",
                                                       *token_keys)})
                if any(not _integer(entry[key]) for key in token_keys):
                    raise _output_error()
                row.update(entry["attrs"])
                rows.append(_group(row, month))
    _unique(rows, _event_order)
    _unique(sessions, _session_order)
    _totals(rows, excluded, value["scanned_events"])
    return OtelCache(organization, month, actual, tuple(org_ids), value["scanned_events"],
                     tuple((key, excluded[key]) for key in EXCLUDED_KINDS), tuple(sorted(absent)),
                     query["estimated_bytes"], query["executed_at"],
                     tuple(sorted(rows, key=_event_order)),
                     tuple(sorted(sessions, key=_session_order)))


def load_otel_cache(cache_dir: Path, month: str, organization: str,
                    target: OtelTarget) -> OtelCache | None:
    """現在の設定に一致する完全なキャッシュを読む（無ければ None）。

    target には、取得時と同じく `gated_orgs` が返す組織の値（取得元と小文字の UUID）を渡す。
    schema・形・語彙・一意キー・件数の和を取得時と同じ規則で検査する。取得元か UUID の
    集合が違う場合も ValueError で取り直しを案内し、古い対象の値を分析に使わせない。
    """
    path = otel_cache_path(cache_dir, month)
    if not path.exists():
        return None
    try:
        return _read_cache(json.loads(path.read_text(encoding="utf-8")), organization, month,
                           target)
    except (ValueError, UnicodeError) as exc:
        raise _cache_error(path, str(exc)) from exc


def _cache_error(path: Path, detail: str) -> ValueError:
    """利用者のキャッシュの型・形の誤りを、取り直しの案内つきの例外にする。"""
    return ValueError(f"{path}: キャッシュを読めません（{detail}）。"
                      "collect --source otel で取り直してください")


def _today() -> dt.date:
    return _now().date()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def collect_events(
    organization: str, month: str, cache_dir: Path, target: OtelTarget,
    maximum_bytes_billed: int, *, dry_run: bool = False, runner: Runner | None = None,
    today: dt.date | None = None,
    on_schema: Callable[[tuple[str, ...]], None] | None = None,
    on_estimate: Callable[[int], None] | None = None,
) -> OtelCache | None:
    """表の定義・見積もり・結果の検査を順に行い、成功時だけ月の集計を保存する。

    翌月2日（UTC）以降だけ取得する。dry_run は始まった月の見積もりまでで、保存せず
    None を返す。runner と today はテスト用の差し替え、コールバックは進捗表示用。
    bq の失敗と結果の誤りは ValueError とし、既存キャッシュを変更しない。
    """
    start, end = _month_bounds(month)
    day = _today() if today is None else today
    if day < start:
        raise ValueError(f"{month} はまだ始まっていません")
    available = end + dt.timedelta(days=1)
    if not dry_run and day < available:
        raise ValueError(f"{month} はまだ終わっていません。{available}（UTC）以降に取得できます")
    run = run_bq if runner is None else runner
    source = target.source
    base = [f"--bigqueryrc={os.devnull}", "--headless", "--quiet"]
    schema = _request(run, [*base, "--format=json", "show", "--schema",
                           f"{source.project}:{source.dataset}.{source.table}"], None, 60)
    absent = inspect_schema(schema)
    if on_schema is not None:
        on_schema(absent)
    sql = build_sql(source, absent)
    query = [*base, f"--project_id={source.project}", "query", "--use_legacy_sql=false"]
    parameters = ["--format=json", f"--parameter=start:TIMESTAMP:{start}T00:00:00Z",
                  f"--parameter=end:TIMESTAMP:{end}T00:00:00Z",
                  f"--parameter=org_ids:STRING:{','.join(target.organization_ids)}"]
    estimate = _request(run, [*query, "--dry_run", *parameters], sql, 600)
    if not isinstance(estimate, dict) or not isinstance(estimate.get("statistics"), dict):
        raise _output_error()
    estimated_bytes = _bq_integer(estimate["statistics"].get("totalBytesProcessed"))
    if on_estimate is not None:
        on_estimate(estimated_bytes)
    if estimated_bytes > maximum_bytes_billed:
        raise _output_error(TOO_LARGE)
    if dry_run:
        return None
    value = _request(run, [*query, *parameters, f"--maximum_bytes_billed={maximum_bytes_billed}",
                          "--max_rows=10000000"], sql, 600)
    rows, sessions, scanned, excluded = parse_result(value, month)
    cache = OtelCache(organization, month, source, target.organization_ids, scanned, excluded,
                      absent, estimated_bytes, _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                      rows, sessions)
    _write_cache(otel_cache_path(cache_dir, month), cache)
    return cache
