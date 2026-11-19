"""OpenTelemetry の月次キャッシュから数える、人ごとの使い方のテスト。"""

from dataclasses import fields

import pytest

from seat_analyzer import otel_collect
from seat_analyzer.otel_metrics import OtelUsage, person_usage

MONTH = "2026-10"
ALICE = "alice@example.com"
TARGET = otel_collect.OtelTarget(
    otel_collect.TableLocation("example-project", "claude_code_otel", "logs"),
    ("00000000-0000-4000-8000-000000000001",),
)


def _event(name, count, *, email=ALICE, day="01", **overrides):
    """集計名に合った属性の合成イベントを作る。"""
    attrs = {"terminal": "interactive"}
    tokens = ()
    if name == "api_request":
        attrs.update(model="claude-sonnet-4-6", origin="main", effort="none",
                     agent=False, skill=False, mcp=False, plugin=False)
        tokens = (11, 2, 0, 0)
    elif name == "tool_decision":
        attrs.update(tool="shell", decision="accept", source="config", permission_mode="default")
    elif name == "user_prompt":
        attrs["command_source"] = "none"
    attrs.update(overrides)
    return otel_collect.EventRow(
        email, f"{MONTH}-{day}", name, tuple(sorted(attrs.items())), count, *tokens,
    )


def _read_cache(tmp_path, *rows, sessions=()):
    """実際の読み書き経路で、合成のキャッシュを往復させる。"""
    cache = otel_collect.OtelCache(
        "org-a", MONTH, TARGET.source, TARGET.organization_ids, sum(row.count for row in rows),
        tuple((kind, 0) for kind in otel_collect.EXCLUDED_KINDS), (), 100,
        "2026-11-01T00:00:00Z", rows, sessions,
    )
    path = otel_collect.otel_cache_path(tmp_path / otel_collect.OTEL_CACHE_DIRNAME, MONTH)
    otel_collect._write_cache(path, cache)
    return otel_collect.load_otel_cache(path.parent, MONTH, "org-a", TARGET)


def test_requests_by_origin_skill_and_distinct_days(tmp_path):
    """origin の内訳・スキルのフラグ・リクエストのあった日のみを数える。"""
    cache = _read_cache(
        tmp_path,
        _event("api_request", 2),
        _event("api_request", 3, origin="sdk", terminal="non_interactive"),
        _event("api_request", 5, origin="subagent_builtin", day="02"),
        _event("api_request", 7, origin="subagent_custom", skill=True, day="02"),
        _event("api_request", 11, origin="other", skill=True, day="03"),
    )
    assert person_usage(cache) == {ALICE: OtelUsage(
        requests=17, subagent_requests=12, custom_subagent_requests=7,
        skill_requests=7, active_days=2,
    )}


@pytest.mark.parametrize(("tool", "decisions", "mcp"), [
    ("edit", 2, 0), ("shell", 2, 0), ("mcp", 2, 2),
    ("agent", 0, 0), ("skill", 0, 0), ("other", 0, 0),
])
def test_interactive_decisions_by_tool(tmp_path, tool, decisions, mcp):
    """端末の対話のうち編集・コマンド・MCP だけを確認の対象にする。"""
    cache = _read_cache(tmp_path, _event("tool_decision", 2, tool=tool))
    assert person_usage(cache) == {ALICE: OtelUsage(
        interactive_decisions=decisions, mcp_decisions=mcp,
    )}


@pytest.mark.parametrize(("source", "asked"), [
    ("user_permanent", 3), ("user_temporary", 3), ("user_reject", 3), ("user_abort", 3),
    ("config", 0), ("hook", 0), ("other", 0),
])
def test_asked_decisions_by_source(tmp_path, source, asked):
    """確認の画面で人が答えた4つの source を数える。"""
    cache = _read_cache(tmp_path, _event("tool_decision", 3, source=source, decision="reject"))
    assert person_usage(cache) == {ALICE: OtelUsage(
        interactive_decisions=3, interactive_decisions_asked=asked,
    )}


def test_non_interactive_mcp_is_counted_only_as_mcp(tmp_path):
    """非対話では、人が答えた区分でも確認の対象に入れず、MCP の件数には入れる。"""
    cache = _read_cache(tmp_path, _event(
        "tool_decision", 5, tool="mcp", terminal="non_interactive", source="user_temporary",
    ))
    assert person_usage(cache) == {ALICE: OtelUsage(mcp_decisions=5)}


@pytest.mark.parametrize(("source", "custom"), [
    ("builtin", 0), ("custom", 10), ("mcp", 0), ("none", 0), ("other", 0),
])
def test_prompts_by_command_source_and_terminal(tmp_path, source, custom):
    """端末を問わず指示を数え、自作のコマンドの指示だけ内訳にも入れる。"""
    cache = _read_cache(
        tmp_path, _event("user_prompt", 3, command_source=source),
        _event("user_prompt", 7, command_source=source, terminal="non_interactive"),
    )
    assert person_usage(cache) == {ALICE: OtelUsage(prompts=10, custom_command_prompts=custom)}


def test_usage_keys_are_normalized_sorted_and_from_events_only(tmp_path):
    """メールをそろえて昇順にし、対象外イベントだけの人は0、セッションだけの人は除く。"""
    bob = "bob@example.com"
    cache = _read_cache(
        tmp_path,
        _event("api_error", 11, email=" BOB@EXAMPLE.COM "),
        _event("user_prompt", 2, email=" ALICE@EXAMPLE.COM "),
        _event("user_prompt", 3),
        sessions=(
            otel_collect.SessionRow(ALICE, f"{MONTH}-01", "interactive", 7),
            otel_collect.SessionRow("carol@example.com", f"{MONTH}-01", "interactive", 5),
        ),
    )
    people = person_usage(cache)
    assert list(people) == [ALICE, bob]
    assert people == {ALICE: OtelUsage(prompts=5), bob: OtelUsage()}


@pytest.mark.parametrize(("values", "field"), [
    ({"mcp_decisions": -1}, "mcp_decisions"),
    ({"requests": 1, "active_days": 1, "subagent_requests": 2}, "subagent_requests"),
    ({"custom_subagent_requests": 1}, "custom_subagent_requests"),
    ({"skill_requests": 1}, "skill_requests"),
    ({"interactive_decisions_asked": 1}, "interactive_decisions_asked"),
    ({"custom_command_prompts": 1}, "custom_command_prompts"),
    ({"requests": 1}, "active_days"),
    ({"active_days": 1}, "active_days"),
    ({"requests": 1, "active_days": 2}, "active_days"),
    ({"requests": 32, "active_days": 32}, "active_days"),
])
def test_usage_rejects_invalid_counts(values, field):
    """負の件数、全体を超える内訳、リクエストと日数の不整合を拒む。"""
    with pytest.raises(ValueError, match=field):
        OtelUsage(**values)


@pytest.mark.parametrize("field", [f.name for f in fields(OtelUsage)])
@pytest.mark.parametrize("value", [True, 1.5])
def test_usage_requires_integer_counts(field, value):
    """全項目で真偽値と小数を整数として受け付けない。"""
    with pytest.raises(TypeError, match=field):
        OtelUsage(**{field: value})


def test_person_usage_requires_an_otel_cache():
    """キャッシュの値オブジェクトを要求する。"""
    with pytest.raises(TypeError, match="OtelCache"):
        person_usage({})
