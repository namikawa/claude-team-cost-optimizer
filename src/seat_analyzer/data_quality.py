"""構造化品質issueの整列とJSON直列化。

同じissue集合からは常にバイト一致の出力を得ることだけを担う。issueの検出
（doctor本体）や既存の文字列warningからの変換はここでは扱わない。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .domain import QualityIssue, Severity

# Severityの宣言順（ERROR→WARNING）をそのまま整列順に使う
_SEVERITY_ORDER = {severity: index for index, severity in enumerate(Severity)}


def issue_to_dict(issue: QualityIssue) -> dict[str, object]:
    """issueをJSON化可能なdictへ変換する。

    キー順はseverity, code, message, scopeで固定。scopeはキーの辞書順に並べ、
    正準化されたtupleはlistへ戻す。
    """
    scope = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in sorted(issue.scope.items())
    }
    return {
        "severity": issue.severity.value,
        "code": issue.code.value,
        "message": issue.message,
        "scope": scope,
    }


def issues_to_json(issues: Iterable[QualityIssue]) -> str:
    """issue列を決定的なJSON文字列にする。

    並び順は渡された順のまま（整列が必要ならsort_issuesを先に通す）。日本語の
    messageはエスケープせずそのまま出力し、NaN・inf はJSON側でも拒否する。
    """
    return json.dumps(
        [issue_to_dict(issue) for issue in issues],
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    )


def _sort_key(issue: QualityIssue) -> tuple[int, str, str, str]:
    # scopeはissue_to_dictの時点でキー順が確定しているため、そのまま文字列化する。
    # JSON表現はtrue・1・1.0・-0.0を書き分けるため、QualityIssueの等価性と
    # 同じ粒度になる（等価なissueだけが同じ整列キーを持つ）
    scope_repr = json.dumps(
        issue_to_dict(issue)["scope"],
        ensure_ascii=False,
        allow_nan=False,
    )
    return (
        _SEVERITY_ORDER[issue.severity],
        issue.code.value,
        issue.message,
        scope_repr,
    )


def sort_issues(issues: Iterable[QualityIssue]) -> list[QualityIssue]:
    """severity, code, message, scopeの順で全順序に整列する。

    同値のissueを除く全てのissueに順序が付くため、入力順によらず結果は同一になる。
    """
    return sorted(issues, key=_sort_key)
