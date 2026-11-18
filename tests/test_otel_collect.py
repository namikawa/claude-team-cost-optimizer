"""OpenTelemetry の集計の取得・SQL の許可リスト・完全なキャッシュの検査。"""

import copy
import datetime as dt
import json
import os
import re
import stat
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from seat_analyzer import otel_collect as otel
from seat_analyzer.config import load_config

FIXTURES = Path(__file__).parent / "fixtures" / "otel"
SOURCE = otel.TableLocation("example-project", "claude_code_otel", "logs")
IDS = ("00000000-0000-4000-8000-000000000001",)
TARGET = otel.OtelTarget(SOURCE, IDS)
MONTH = "2026-10"
TODAY = dt.date(2026, 11, 2)
NOW = dt.datetime(2026, 11, 2, 3, 4, 5, tzinfo=dt.UTC)
LIMIT = 2_000_000_000


def fixture(name):
    """合成データだけの fixture を UTF-8 で読む。"""
    return (FIXTURES / name).read_text(encoding="utf-8")


def result_rows():
    """テストごとに変更できる結果の配列。"""
    return json.loads(fixture("result.json"))


def reply(value):
    """JSON の成功応答にする。"""
    return otel.BqResult(stdout=json.dumps(value))


def stub(responses=None):
    """呼び出しの順序・引数・SQL・上限を記録し、実機を一度も呼ばない。"""
    responses = responses if responses is not None else [
        otel.BqResult(stdout=fixture(name))
        for name in ("schema.json", "dry-run.json", "result.json")
    ]
    calls = []

    def run(args, sql, timeout):
        calls.append((list(args), sql, timeout))
        assert len(calls) <= len(responses), "予定していない bq の呼び出し"
        return responses[len(calls) - 1]

    return run, calls


def collect(tmp_path, runner, **kwargs):
    """実機と現在日時に依存しない取得。"""
    return otel.collect_events("org-a", MONTH, tmp_path / "otel-cache", TARGET,
                               kwargs.pop("maximum_bytes_billed", LIMIT), runner=runner,
                               today=kwargs.pop("today", TODAY), **kwargs)


def old_cache(tmp_path):
    """失敗しても残すべき既存ファイルを置く。"""
    path = tmp_path / "otel-cache" / "otel-2026-10.json"
    path.parent.mkdir()
    path.write_bytes(b"existing cache\n")
    return path


def renumber(rows):
    """行を増減したテストで、行数以外の検査まで届かせる。"""
    for row in rows:
        row["total_rows"] = str(len(rows))
    return rows


def test_sql_references_only_allowlisted_columns():
    """SQL の列と差し込み口が許可リストだけで、禁止する列を参照しない。"""
    template = (Path(otel.__file__).parent / "sql" / "otel_events.sql").read_text(encoding="utf-8")
    references = set(re.findall(r"\blabels\.[a-z0-9_]+", template.lower()))
    slots = set(re.findall(r"\{\{([^{}]+)\}\}", template))
    assert slots == {"table", *otel.OPTIONAL_ATTRIBUTES}
    assert references == {name for name, required in otel.SQL_COLUMNS.items()
                          if required and name != "timestamp"}
    assert references | {f"labels.{name}" for name in slots - {"table"}} | {"timestamp"} == set(
        otel.SQL_COLUMNS)
    prohibited = (r"\b(?:prompt|prompt_text|response|tool_input|tool_parameters|body|error|"
                  r"workspace_host_paths|cost_usd|request_id|vcs_\w*)\b")
    assert re.search(prohibited, template, re.IGNORECASE) is None
    sql = otel.build_sql(SOURCE)

    # コメント・文字列（raw も含む）・問い合わせの引数を除き、バッククォートの識別子は残す。
    code = re.sub(
        r"""(`[^`]+`)|--[^\n]*|/\*.*?\*/|(?:\b[rb]{1,2})?(?:'(?:\\.|[^'\\])*'"""
        r"""|"(?:\\.|[^"\\])*")|@[a-z_][a-z0-9_]*""",
        lambda match: match[1] or " ", sql, flags=re.IGNORECASE | re.DOTALL,
    ).lower()
    scoped = re.search(r"^with\s+scoped\s+as\s*\((.*?)^\s*\),", code, re.MULTILINE | re.DOTALL)
    assert scoped is not None
    table_read = re.compile(r"\bfrom\s+`([^`]+)`")
    assert table_read.findall(code) == ["example-project.claude_code_otel.logs"]
    expressions, removed = table_read.subn(" ", scoped[1])
    assert removed == 1

    # scoped が直接読む列だけを照合する。以降の CTE は集計用の別名を参照する。
    identifier = r"(?:`[^`]+`|[a-z_][a-z0-9_]*)"
    expressions = re.sub(rf"\bas\s+{identifier}|{identifier}(?=\s*\()", " ", expressions)
    columns = {re.sub(r"\s*\.\s*", ".", token) for token in re.findall(
        rf"{identifier}(?:\s*\.\s*{identifier})*|\*", expressions
    )} - {"select", "where", "and", "or", "is", "not", "null"}
    unexpected = columns - otel.SQL_COLUMNS.keys()
    assert not unexpected, f"scoped に許可されていない列があります: {sorted(unexpected)}"

    assert "metrics" not in sql
    assert "{1,64}$" in sql
    assert "{{" not in sql and "}}" not in sql


@pytest.mark.parametrize("absent", [(), otel.OPTIONAL_ATTRIBUTES, ("plugin_name",)])
def test_sql_optional_columns(absent):
    """任意属性を全部・一部欠く表でも、存在しない列を参照しない。"""
    schema = json.loads(fixture("schema.json"))
    labels = next(field for field in schema if field["name"] == "labels")
    labels["fields"] = [field for field in labels["fields"] if field["name"] not in absent]
    missing = otel.inspect_schema(schema)
    assert missing == tuple(sorted(absent))
    sql = otel.build_sql(SOURCE, missing)
    assert "{{" not in sql and "}}" not in sql
    for name in otel.OPTIONAL_ATTRIBUTES:
        if name in absent:
            assert f"CAST(NULL AS STRING) AS {name}" in sql
            assert f"labels.{name}" not in sql
        else:
            assert f"labels.{name} AS {name}" in sql


@pytest.mark.parametrize("text", ["{{unknown}}", "{{model", "{{table}} }}"])
def test_sql_rejects_unknown_and_unfinished_slots(monkeypatch, text):
    """SQL の未知の差し込み口と置き換え残しを拒む。"""
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: text)
    with pytest.raises(ValueError):
        otel.build_sql(SOURCE)


@pytest.mark.parametrize("missing", ["timestamp", "labels", "event_name", "user_email",
                                    "organization_id", "session_id"])
def test_missing_required_column_stops_before_query(tmp_path, missing):
    """必須列を1つでも欠く表は dry-run を含む問い合わせを行わない。"""
    schema = json.loads(fixture("schema.json"))
    schema = [field for field in schema if field["name"] != missing]
    for field in schema:
        if field["name"] == "labels":
            field["fields"] = [child for child in field["fields"] if child["name"] != missing]
    run, calls = stub([reply(schema)])
    path = old_cache(tmp_path)
    with pytest.raises(ValueError, match="集計に必須の列が表にありません"):
        collect(tmp_path, run)
    assert len(calls) == 1
    assert path.read_bytes() == b"existing cache\n"


@pytest.mark.parametrize(("column", "wrong_type"), [("timestamp", "STRING"), ("labels", "STRING")])
def test_required_top_level_column_type(tmp_path, column, wrong_type):
    """時刻と labels は最上位の指定型だけを受け付ける。"""
    schema = json.loads(fixture("schema.json"))
    next(field for field in schema if field["name"] == column)["type"] = wrong_type
    run, calls = stub([reply(schema)])
    with pytest.raises(ValueError, match="必須の列"):
        collect(tmp_path, run)
    assert len(calls) == 1


@pytest.mark.parametrize(("today", "dry", "allowed", "message"), [
    (dt.date(2026, 9, 30), True, False, "まだ始まっていません"),
    (dt.date(2026, 9, 30), False, False, "まだ始まっていません"),
    (dt.date(2026, 11, 1), False, False, "2026-11-02（UTC）以降"),
    (dt.date(2026, 10, 15), False, False, "まだ終わっていません"),
    (dt.date(2026, 11, 2), False, True, ""),
    (dt.date(2026, 10, 15), True, True, ""),
])
def test_month_availability(tmp_path, today, dry, allowed, message):
    """月の開始前は見積もらず、取得は翌月2日以降だけに限る。"""
    run, calls = stub()
    if allowed:
        collect(tmp_path, run, today=today, dry_run=dry)
        assert len(calls) == (2 if dry else 3)
        assert (tmp_path / "otel-cache").exists() is (not dry)
    else:
        with pytest.raises(ValueError, match=re.escape(message)):
            collect(tmp_path, run, today=today, dry_run=dry)
        assert calls == []
        assert not (tmp_path / "otel-cache").exists()


def test_bq_arguments_and_sql_stdin(tmp_path):
    """3段の引数・標準入力・待機上限と、利用者設定の抑止を固定する。"""
    run, calls = stub()
    collect(tmp_path, run)
    base = [f"--bigqueryrc={os.devnull}", "--headless", "--quiet"]
    assert calls[0] == ([*base, "--format=json", "show", "--schema",
                          "example-project:claude_code_otel.logs"], None, 60)
    query = [*base, "--project_id=example-project", "query", "--use_legacy_sql=false"]
    parameters = ["--format=json", "--parameter=start:TIMESTAMP:2026-10-01T00:00:00Z",
                  "--parameter=end:TIMESTAMP:2026-11-01T00:00:00Z",
                  f"--parameter=org_ids:STRING:{IDS[0]}"]
    assert calls[1] == ([*query, "--dry_run", *parameters], otel.build_sql(SOURCE), 600)
    assert calls[2] == ([*query, *parameters, f"--maximum_bytes_billed={LIMIT}",
                          "--max_rows=10000000"], otel.build_sql(SOURCE), 600)
    assert all(not re.search(r'''[\s<>"']''', arg) for args, _, _ in calls for arg in args)


def test_december_parameters_and_normalized_ids(tmp_path):
    """12月の終端は翌年1月で、継承した大文字 UUID は小文字のカンマ列にする。"""
    upper = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    ids = otel.organization_ids({"claude_export": {"org_id": upper}, "workspaces": {
        "main": {"claude_export": {"org_id": IDS[0]}}
    }})
    assert ids == (upper.lower(), IDS[0])
    run, calls = stub()
    otel.collect_events("org-a", "2026-12", tmp_path / "otel-cache",
                        otel.OtelTarget(SOURCE, ids), LIMIT, dry_run=True,
                        today=dt.date(2026, 12, 1), runner=run)
    args = calls[1][0]
    assert "--parameter=end:TIMESTAMP:2027-01-01T00:00:00Z" in args
    assert f"--parameter=org_ids:STRING:{upper.lower()},{IDS[0]}" in args


@pytest.mark.parametrize(("estimate", "dry", "success", "calls_expected"), [
    (LIMIT, False, True, 3), (LIMIT + 1, False, False, 2),
    (LIMIT, True, True, 2), (LIMIT + 1, True, False, 2),
])
def test_estimate_limit_and_dry_run(tmp_path, estimate, dry, success, calls_expected):
    """見積もりは上限と同じなら通し、1 byte 超えれば dry-run でも止める。"""
    run, calls = stub([reply(json.loads(fixture("schema.json"))),
                       reply({"statistics": {"totalBytesProcessed": str(estimate)}}),
                       reply(result_rows())])
    if success:
        collect(tmp_path, run, dry_run=dry)
    else:
        with pytest.raises(ValueError, match="読み取り量が上限"):
            collect(tmp_path, run, dry_run=dry)
    assert len(calls) == calls_expected
    if dry or not success:
        assert not (tmp_path / "otel-cache").exists()
        assert not any("query" in args and "--dry_run" not in args for args, _, _ in calls)


@pytest.mark.parametrize("estimate", [None, 12, True, "1.0", "-1", [], {}, "NaN"])
def test_malformed_estimate(tmp_path, estimate):
    """見積もりは数字の文字列だけを受け付け、実行しない。"""
    run, calls = stub([reply(json.loads(fixture("schema.json"))),
                       reply({"statistics": {"totalBytesProcessed": estimate}})])
    with pytest.raises(ValueError, match=otel.UNREADABLE):
        collect(tmp_path, run)
    assert len(calls) == 2


# 変えるのは1項目ずつ。行数は別のケース以外では合わせ、個々の検査まで到達させる。
INVALID_RESULTS = [
    ("empty", None, None, None),
    ("no_scanned", None, None, None),
    ("two_scanned", None, None, None),
    ("row_count", 0, "total_rows", "17"),
    ("inconsistent_row_count", 1, "total_rows", "15"),
    ("sum", 13, "count", "84"),
    ("origin", 0, "origin", "private_agent"),
    ("tool", 2, "tool", "PrivateTool"),
    ("source", 2, "source", "unknown"),
    ("permission_mode", 2, "permission_mode", "private_mode"),
    ("command_source", 4, "command_source", "private_command"),
    ("terminal", 0, "terminal", "unknown"),
    ("model", 0, "model", "invalid model"),
    ("model_length", 0, "model", "a" * 101),
    ("date", 0, "date", "2026-09-30"),
    ("bad_date", 0, "date", "2026-10-32"),
    ("email", 0, "email", ""),
    ("duplicate_event", None, None, None),
    ("duplicate_session", None, None, None),
    ("zero", 0, "count", "0"),
    ("negative", 0, "count", "-1"),
    ("integer_count", 0, "count", 12),
    ("bool_count", 0, "count", True),
    ("unknown_kind", 0, "kind", "unknown"),
    ("missing_column", None, None, None),
    ("extra_column", 0, "private", "value"),
    ("session_extra", 14, "origin", "main"),
    ("session_count", 14, "count", "0"),
    ("session_terminal", 14, "terminal", "unknown"),
    ("excluded_extra", 10, "email", "alice@example.com"),
    ("excluded_zero", 10, "count", "0"),
    ("scanned_extra", 13, "name", "api_request"),
    ("scanned_negative", 13, "count", "-1"),
    ("scanned_null", 13, "count", None),
    ("token_negative", 0, "input_tokens", "-1"),
    ("non_api_token", 2, "input_tokens", "1"),
    ("bool", 0, "agent", True),
    ("bool_word", 0, "agent", "yes"),
    ("wrong_event_attr", 6, "command_source", "custom"),
    ("name", 0, "name", "PrivateName"),
    ("name_type", 0, "name", []),
    ("kind_type", 0, "kind", []),
    ("not_json", None, None, None),
    ("not_array", None, None, None),
    ("not_row", None, None, None),
]


@pytest.mark.parametrize(("case", "index", "key", "value"), INVALID_RESULTS,
                         ids=[item[0] for item in INVALID_RESULTS])
def test_invalid_result_preserves_existing_cache(tmp_path, case, index, key, value):
    """不正な値・語彙・列・件数・一意キーがあれば、既存キャッシュを変更しない。"""
    rows = result_rows()
    if case == "empty":
        rows = []
    elif case == "no_scanned":
        rows.pop(13)
    elif case == "two_scanned":
        rows.append(copy.deepcopy(rows[13]))
    elif case == "duplicate_event":
        rows.append(copy.deepcopy(rows[0]))
        rows[13]["count"] = "95"
    elif case == "duplicate_session":
        rows.append(copy.deepcopy(rows[14]))
    elif case == "missing_column":
        del rows[0]["origin"]
    elif index is not None:
        rows[index][key] = value
    if case not in ("row_count", "inconsistent_row_count"):
        renumber(rows)
    output = reply(rows)
    if case == "not_json":
        output = otel.BqResult(stdout="not json")
    elif case == "not_array":
        output = reply({})
    elif case == "not_row":
        output = reply([None])
    run, calls = stub([reply(json.loads(fixture("schema.json"))),
                       otel.BqResult(stdout=fixture("dry-run.json")), output])
    path = old_cache(tmp_path)
    with pytest.raises(ValueError):
        collect(tmp_path, run)
    assert len(calls) == 3
    assert path.read_bytes() == b"existing cache\n"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(("name", "expected"), [
    ("notable", "project・dataset・表"), ("nodataset", "project・dataset・表"),
    ("noproject", "project・dataset・表"), ("notable_dry", "project・dataset・表"),
    ("accessdenied", "project・dataset・表"), ("badfield_dry", "bq の実行に失敗しました"),
    ("toolarge", otel.TOO_LARGE), ("reauth-stderr", "bq の認証がありません"),
])
def test_error_fixture_classification(name, expected):
    """折り返しを含む8つの失敗の文を指定の順に分類する。"""
    text = fixture(f"error-{name}.txt")
    result = otel.BqResult(1, stderr=text) if name == "reauth-stderr" else otel.BqResult(2, text)
    assert otel.failure_reason(result).startswith(expected)
    if name == "badfield_dry":
        assert otel.failure_reason(result).splitlines()[1:] == text.splitlines()[:3]


@pytest.mark.parametrize("text", ["Reauthentication", "gcloud auth login",
                                  "refreshing your current auth tokens",
                                  "do not currently have an active account"])
def test_authentication_classification_has_priority(text):
    """認証の失敗は見つからない・権限の文より先に分類する。"""
    assert otel.failure_reason(otel.BqResult(1, text + " Access Denied")).startswith(
        "bq の認証がありません")


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize("failure", [
    otel.BqResult(1, stderr="Reauthentication failed"),
    otel.BqResult(2, "Not found: Table"),
    otel.BqResult(1, "Permission denied"),
    otel.BqResult(1, "unknown error"),
    otel.BqResult(failure=otel.NOT_FOUND),
    otel.BqResult(failure=otel.TIMEOUT),
    otel.BqResult(stdout="not json"),
    reply(None),
])
def test_failed_stage_stops_and_preserves_cache(tmp_path, stage, failure):
    """各段の失敗時は後続を呼ばず、既存のキャッシュを保存し直さない。"""
    responses = [otel.BqResult(stdout=fixture(name))
                 for name in ("schema.json", "dry-run.json", "result.json")]
    responses[stage] = failure
    run, calls = stub(responses)
    path = old_cache(tmp_path)
    with pytest.raises(ValueError):
        collect(tmp_path, run)
    assert len(calls) == stage + 1
    assert path.read_bytes() == b"existing cache\n"


@pytest.mark.parametrize(("failure", "expected"), [
    (FileNotFoundError(), otel.NOT_FOUND),
    (subprocess.TimeoutExpired("bq", 60), otel.TIMEOUT),
    (OSError(), "bq の実行に失敗しました"),
])
def test_run_bq_process_failures(monkeypatch, failure, expected):
    """起動後に起きる欠落・応答待ち上限・OS の失敗を理由にする。"""
    monkeypatch.setattr(otel.shutil, "which", lambda _: "/synthetic/bq")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(otel.subprocess, "run", fail)
    assert otel.run_bq([], None, 60).failure == expected


def test_run_bq_missing_command(monkeypatch):
    """PATH に bq が無い場合は起動しない。"""
    monkeypatch.setattr(otel.shutil, "which", lambda _: None)
    assert otel.run_bq([], None, 60).failure == otel.NOT_FOUND


@pytest.mark.parametrize("bad_stream", [None, "stdout", "stderr"])
def test_run_bq_uses_resolved_executable_and_strict_utf8(monkeypatch, bad_stream):
    """実体を起動し、SQL を UTF-8 の標準入力、出力を strict で扱う。"""
    monkeypatch.setattr(otel.shutil, "which", lambda _: "/synthetic/bq.cmd")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        streams = {"stdout": b"[]", "stderr": b""}
        if bad_stream:
            streams[bad_stream] = b"\xff"
        return SimpleNamespace(returncode=0, **streams)

    monkeypatch.setattr(otel.subprocess, "run", run)
    result = otel.run_bq(["query"], "SELECT '例'", 600)
    assert calls == [(["/synthetic/bq.cmd", "query"], {
        "input": "SELECT '例'".encode(), "capture_output": True, "timeout": 600, "check": False,
    })]
    if bad_stream:
        assert result.failure == otel.UNREADABLE
    else:
        assert result.stdout == "[]" and result.returncode == 0


def test_fixture_cache_and_roundtrip(tmp_path, monkeypatch):
    """見本を区分だけのキャッシュにし、NULL トークンを0にして同じ値を読み戻す。"""
    monkeypatch.setattr(otel, "_now", lambda: NOW)
    run, _ = stub()
    cache = collect(tmp_path, run)
    path = otel.otel_cache_path(tmp_path / "otel-cache", MONTH)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert cache.scanned_events == 83
    assert dict(cache.excluded_events) == {"organization_empty": 3, "organization_other": 5,
                                           "email_empty": 2}
    assert len(cache.rows) == 10 and len(cache.sessions) == 2
    assert sum(row.count for row in cache.rows) == 73
    assert payload["query"] == {"estimated_bytes": 343675820,
                                "executed_at": "2026-11-02T03:04:05Z"}
    null_tokens = next(row for row in payload["rows"] if row["attrs"].get("model") == "other")
    assert all(null_tokens[column] == 0 for column in otel.TOKEN_COLUMNS)
    assert all(isinstance(row["attrs"]["agent"], bool) for row in payload["rows"]
               if row["name"] == "api_request")
    assert all(set(row["attrs"]) == {"terminal", *otel.EVENT_ATTRIBUTES.get(row["name"], ())}
               for row in payload["rows"])
    assert otel.load_otel_cache(path.parent, MONTH, "org-a", TARGET) == cache
    assert path.read_bytes().endswith(b"\n") and b"\r" not in path.read_bytes()
    original = path.read_bytes()
    run, _ = stub([reply(json.loads(fixture("schema.json"))),
                   otel.BqResult(stdout=fixture("dry-run.json")),
                   reply(list(reversed(result_rows())))])
    assert collect(tmp_path, run) == cache
    assert path.read_bytes() == original
    with pytest.raises(FrozenInstanceError):
        cache.month = "2026-09"


def test_cache_roundtrip_with_uppercase_uuid_in_config(tmp_path):
    """大文字 UUID の設定から得た同じ取得対象で、取得と読み戻しが通る。"""
    uuid = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    path = tmp_path / "config.yaml"
    path.write_text(f"""organizations:
  org-a:
    otel:
      bigquery:
        project: example-project
        dataset: claude_code_otel
        logs_table: logs
      organization_ids: [{uuid}]
""", encoding="utf-8", newline="\n")
    cfg = load_config(path)
    target = otel.gated_orgs(cfg)["org-a"]
    cache_dir = tmp_path / "otel-cache"
    run, _ = stub()
    cache = otel.collect_events("org-a", MONTH, cache_dir, target, LIMIT, runner=run, today=TODAY)
    assert cfg["organizations"]["org-a"]["otel"]["organization_ids"] == [uuid]
    assert cache.organization_ids == (uuid.lower(),)
    assert otel.load_otel_cache(cache_dir, MONTH, "org-a", target) == cache


def test_empty_month_and_replacement(tmp_path, monkeypatch):
    """scanned だけの空の月を保存し、同じ月の再取得で置き換える。"""
    monkeypatch.setattr(otel, "_now", lambda: NOW)
    path = old_cache(tmp_path)
    scanned = result_rows()[13]
    scanned.update(count="0", total_rows="1")
    run, _ = stub([reply(json.loads(fixture("schema.json"))),
                   otel.BqResult(stdout=fixture("dry-run.json")), reply([scanned])])
    cache = collect(tmp_path, run)
    assert cache.rows == cache.sessions == ()
    assert cache.scanned_events == 0
    assert set(dict(cache.excluded_events).values()) == {0}
    assert otel.load_otel_cache(path.parent, MONTH, "org-a", TARGET) == cache
    run, _ = stub()
    collect(tmp_path, run)
    assert json.loads(path.read_text(encoding="utf-8"))["scanned_events"] == 83


def test_absent_attributes_recorded_in_cache(tmp_path):
    """任意属性の無い表では NULL 式に置換し、その名前をキャッシュへ記録する。"""
    schema = json.loads(fixture("schema.json"))
    labels = next(field for field in schema if field["name"] == "labels")
    labels["fields"] = [field for field in labels["fields"]
                        if field["name"] not in ("plugin_name", "input_tokens")]
    rows = result_rows()
    for row in rows:
        if row["kind"] == "event" and row["name"] == "api_request":
            row["input_tokens"] = None
    run, calls = stub([reply(schema), otel.BqResult(stdout=fixture("dry-run.json")), reply(rows)])
    cache = collect(tmp_path, run)
    assert cache.absent_attributes == ("input_tokens", "plugin_name")
    assert "CAST(NULL AS STRING) AS plugin_name" in calls[1][1]
    assert all(dict(row.attrs)["plugin"] is False and row.input_tokens == 0
               for row in cache.rows if row.name == "api_request")


CACHE_MUTATIONS = [
    ("schema", 2), ("schema", True), ("month", "2026-09"), ("organization", "org-b"),
    ("scanned_events", 84), ("scanned_events", True), ("rows", {}), ("sessions", {}),
    ("absent_attributes", ["private"]), ("absent_attributes", [{}]),
    ("organization_ids", ["00000000-0000-4000-8000-000000000002"]),
    ("source", {"project": "other-project", "dataset": SOURCE.dataset, "table": SOURCE.table}),
    ("source", []), ("query", {"estimated_bytes": "12", "executed_at": "2026-11-02T03:04:05Z"}),
    ("query", {"estimated_bytes": 12, "executed_at": "2026-11-02T03:04:05"}),
    ("excluded_events", {"organization_empty": -1, "organization_other": 5, "email_empty": 6}),
]


@pytest.mark.parametrize(("key", "value"), CACHE_MUTATIONS)
def test_invalid_cache_requires_refetch(tmp_path, key, value):
    """schema・形・取得元・UUID・件数が不正なキャッシュは取り直しを案内する。"""
    run, _ = stub()
    collect(tmp_path, run)
    path = tmp_path / "otel-cache" / "otel-2026-10.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[key] = value
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="collect --source otel で取り直してください"):
        otel.load_otel_cache(path.parent, MONTH, "org-a", TARGET)


@pytest.mark.parametrize(("section", "key", "value"), [
    ("rows", "attrs", {"terminal": "interactive", "private": "name"}),
    ("rows", "count", 0), ("rows", "date", "2026-09-30"), ("rows", "count", True),
    ("rows", "name", []), ("rows", "email", ""),
    ("sessions", "terminal", "private"), ("sessions", "count", 0),
])
def test_invalid_cached_groups(tmp_path, section, key, value):
    """保存済み行も取得結果と同じ語彙・日付・型で検査する。"""
    run, _ = stub()
    collect(tmp_path, run)
    path = tmp_path / "otel-cache" / "otel-2026-10.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[section][0][key] = value
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="取り直してください"):
        otel.load_otel_cache(path.parent, MONTH, "org-a", TARGET)


def test_cache_uuid_order_is_ignored(tmp_path):
    """現在の UUID は集合で比較し、設定の並びだけが違っても読める。"""
    other = "00000000-0000-4000-8000-000000000002"
    run, _ = stub()
    otel.collect_events("org-a", MONTH, tmp_path / "otel-cache",
                        otel.OtelTarget(SOURCE, (*IDS, other)), LIMIT, runner=run, today=TODAY)
    target = otel.OtelTarget(SOURCE, (other, *IDS))
    assert otel.load_otel_cache(tmp_path / "otel-cache", MONTH, "org-a", target)


def test_missing_cache(tmp_path):
    """キャッシュが無い場合は None で未提供を表す。"""
    assert otel.load_otel_cache(tmp_path, MONTH, "org-a", TARGET) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX のファイル権限を検査する")
def test_atomic_write_preserves_mode(tmp_path):
    """既存ファイルの権限を置換後も引き継ぐ。"""
    path = old_cache(tmp_path)
    path.chmod(0o640)
    run, _ = stub()
    collect(tmp_path, run)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_failed_replacement_leaves_original_and_removes_temp(tmp_path, monkeypatch):
    """原子的な置換に失敗しても元のファイルを保ち、一時ファイルは残さない。"""
    path = old_cache(tmp_path)

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(otel.os, "replace", fail)
    run, _ = stub()
    with pytest.raises(OSError, match="synthetic failure"):
        collect(tmp_path, run)
    assert path.read_bytes() == b"existing cache\n"
    assert list(path.parent.iterdir()) == [path]


def test_does_not_create_missing_organization(tmp_path):
    """キャッシュの親の組織ディレクトリは自動作成しない。"""
    run, _ = stub()
    with pytest.raises(FileNotFoundError):
        otel.collect_events("org-a", MONTH, tmp_path / "missing" / "otel-cache", TARGET,
                            LIMIT, runner=run, today=TODAY)
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(("attr", "value"), [
    (attr, value) for attr, values in otel.VOCABULARIES.items() for value in values
])
def test_every_declared_vocabulary_is_accepted(attr, value):
    """宣言した全区分を、取得と読み戻しの共通検査が受け付ける。"""
    index = (2 if attr in otel.EVENT_ATTRIBUTES["tool_decision"]
             else 4 if attr == "command_source" else 0)
    row = result_rows()[index]
    row[attr] = value
    scanned = result_rows()[13]
    scanned["count"] = row["count"]
    assert otel.parse_result(renumber([row, scanned]), MONTH)


@pytest.mark.parametrize("model", ["a", "a" * 100, "claude-model_1.0:[test]", "other"])
def test_model_lexical_boundaries(model):
    """モデル名の境界長と許可する記号を受け付ける。"""
    rows = result_rows()
    rows[0]["model"] = model
    assert otel.parse_result(rows, MONTH)


def test_explicit_uuid_resolution_is_lowercase():
    """取得先を組み立てる共通関数も明示 UUID を小文字にする。"""
    assert otel.organization_ids({"otel": {"organization_ids": [
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]}}) == (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",)


def test_dry_run_leaves_existing_cache_unchanged(tmp_path):
    """見積もりだけの実行で既存キャッシュのバイト列を変更しない。"""
    path = old_cache(tmp_path)
    run, calls = stub()
    assert collect(tmp_path, run, dry_run=True) is None
    assert len(calls) == 2
    assert path.read_bytes() == b"existing cache\n"


@pytest.mark.parametrize("column", ["project", "dataset", "table"])
def test_cache_requires_current_source(tmp_path, column):
    """取得元の3つの名前はいずれの変更も読み戻しで検出する。"""
    run, _ = stub()
    collect(tmp_path, run)
    source = {"project": SOURCE.project, "dataset": SOURCE.dataset, "table": SOURCE.table}
    source[column] = "other"
    target = otel.OtelTarget(otel.TableLocation(**source), IDS)
    with pytest.raises(ValueError, match="取得元の表が今の設定と違います"):
        otel.load_otel_cache(tmp_path / "otel-cache", MONTH, "org-a", target)


@pytest.mark.parametrize("data", [b"not json", b"\xff", b"[]", b"null"])
def test_unreadable_cache_requires_refetch(tmp_path, data):
    """JSON・文字コード・最上位の形の誤りで取り直しを案内する。"""
    path = otel.otel_cache_path(tmp_path, MONTH)
    path.write_bytes(data)
    with pytest.raises(ValueError, match="取り直してください"):
        otel.load_otel_cache(tmp_path, MONTH, "org-a", TARGET)


@pytest.mark.parametrize("section", ["rows", "sessions"])
def test_cache_duplicate_key_requires_refetch(tmp_path, section):
    """イベントとセッションの両方で同じキーのキャッシュ行を拒否する。"""
    run, _ = stub()
    collect(tmp_path, run)
    path = tmp_path / "otel-cache" / "otel-2026-10.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[section].append(copy.deepcopy(payload[section][0]))
    if section == "rows":
        payload["scanned_events"] += payload[section][0]["count"]
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="取り直してください"):
        otel.load_otel_cache(path.parent, MONTH, "org-a", TARGET)


@pytest.mark.parametrize("month", ["../2026-10", "2026-1", "2026-13", "0000-01", "9999-12"])
def test_invalid_month_never_calls_bq(tmp_path, month):
    """不正な月と表現範囲を越える終端で traceback を出さず、問い合わせもしない。"""
    run, calls = stub()
    with pytest.raises(ValueError):
        otel.collect_events("org-a", month, tmp_path / "otel-cache", TARGET, LIMIT,
                            runner=run, today=TODAY)
    assert calls == []
