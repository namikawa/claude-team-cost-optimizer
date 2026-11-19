"""OpenTelemetry の月次キャッシュから、人ごとの使い方の件数を数える（設計書 §27.5）。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, fields

from .otel_collect import OtelCache

_API_REQUEST = "api_request"
_TOOL_DECISION = "tool_decision"
_USER_PROMPT = "user_prompt"
_CUSTOM_SUBAGENT = "subagent_custom"
_SUBAGENT_ORIGINS = frozenset({"subagent_builtin", _CUSTOM_SUBAGENT})
_REQUEST_ORIGINS = frozenset({"main", "sdk", *_SUBAGENT_ORIGINS})
_MCP = "mcp"
_INTERACTIVE = "interactive"
_INTERACTIVE_TOOLS = frozenset({"edit", "shell", _MCP})
_ASKED_SOURCES = frozenset({"user_permanent", "user_temporary", "user_reject", "user_abort"})
_CUSTOM_COMMAND = "custom"


@dataclass(frozen=True)
class OtelUsage:
    """1人の1か月の件数。件数と日数は0以上の整数で、内訳は全体を超えない。

    requests は補助の呼び出し（origin が other）を除いたリクエストの件数。
    subagent_requests・custom_subagent_requests・skill_requests は、そのうちサブエージェント
    経由・自作のサブエージェント経由・スキルのリクエストの件数。active_days はリクエストの
    あった UTC 日の日数。prompts は指示の回数、custom_command_prompts はそのうち自作の
    コマンドによる回数（プラグインのコマンドも含む）。

    interactive_decisions は端末の対話で編集・コマンド・MCP の実行を許可・却下した判断の
    件数で、interactive_decisions_asked はそのうち確認の画面で人が答えた件数。読み取りなど
    確認なしで通るツールは除く。Claude Code の OpenTelemetry リファレンスの source の
    説明によると、端末の対話では user_permanent・user_temporary・user_reject は人が答えた
    回に付き、同じ許可に当たる後の実行は config になる。SDK と非対話の実行では後の実行にも
    同じ値が付くので、人が答えた回数を数えるため端末の対話に限る。
    mcp_decisions は MCP の判断の件数。確認の対象とその内訳以外は端末の種類を問わない。
    """

    requests: int = 0
    subagent_requests: int = 0
    custom_subagent_requests: int = 0
    skill_requests: int = 0
    active_days: int = 0
    prompts: int = 0
    custom_command_prompts: int = 0
    interactive_decisions: int = 0
    interactive_decisions_asked: int = 0
    mcp_decisions: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field.name} には整数が必要です: {type(value).__name__}")
            if value < 0:
                raise ValueError(f"{field.name} には0以上の整数が必要です: {value!r}")
        for part, whole in (
            ("subagent_requests", "requests"),
            ("custom_subagent_requests", "subagent_requests"),
            ("skill_requests", "requests"),
            ("interactive_decisions_asked", "interactive_decisions"),
            ("custom_command_prompts", "prompts"),
            ("active_days", "requests"),
        ):
            if getattr(self, part) > getattr(self, whole):
                raise ValueError(f"{part} は {whole} 以下にしてください")
        if self.requests and not self.active_days:
            raise ValueError("requests が1件以上なら active_days も1日以上です")
        if self.active_days > 31:
            raise ValueError("active_days は31日以下にしてください")


def person_usage(cache: OtelCache) -> dict[str, OtelUsage]:
    """イベントのある人の件数をメールの昇順で返す（セッションの行は使わない）。

    メールの前後の空白と大文字・小文字の違いを吸収する。イベント行があっても、
    数えるイベントが無い人の件数は0。キャッシュは検査済みの OtelCache を渡す。
    """
    if not isinstance(cache, OtelCache):
        raise TypeError("OpenTelemetry の集計は OtelCache が必要です")
    people: dict[str, Counter[str]] = defaultdict(Counter)
    days: dict[str, set[str]] = defaultdict(set)
    for row in cache.rows:
        email = row.email.strip().lower()
        counts = people[email]
        attrs = dict(row.attrs)
        if row.name == _API_REQUEST:
            origin = attrs["origin"]
            if origin not in _REQUEST_ORIGINS:
                continue
            counts["requests"] += row.count
            days[email].add(row.date)
            if origin in _SUBAGENT_ORIGINS:
                counts["subagent_requests"] += row.count
            if origin == _CUSTOM_SUBAGENT:
                counts["custom_subagent_requests"] += row.count
            if attrs["skill"] is True:
                counts["skill_requests"] += row.count
        elif row.name == _TOOL_DECISION:
            tool = attrs["tool"]
            if tool == _MCP:
                counts["mcp_decisions"] += row.count
            if attrs["terminal"] == _INTERACTIVE and tool in _INTERACTIVE_TOOLS:
                counts["interactive_decisions"] += row.count
                if attrs["source"] in _ASKED_SOURCES:
                    counts["interactive_decisions_asked"] += row.count
        elif row.name == _USER_PROMPT:
            counts["prompts"] += row.count
            if attrs["command_source"] == _CUSTOM_COMMAND:
                counts["custom_command_prompts"] += row.count
    return {
        email: OtelUsage(active_days=len(days[email]), **counts)
        for email, counts in sorted(people.items())
    }
