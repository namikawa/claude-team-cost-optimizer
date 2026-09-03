"""decision-evidence.csv の書き出し（V2 判定とその根拠）。

行の組み立ては decision_evidence が行う。ここは受け取った行を直列化するだけにして、
同じ行から常に同じバイト列が出ることを保つ（後続の snapshot 保存が同じ行を同じ形で
保存できるようにするため）。

確定できなかった値は空欄にする。0 や False で埋めると「観測した結果が 0 だった」ことと
区別できなくなるため、欠損は欠損のまま出す（usage-summary.csv と同じ流儀）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from ..decision_evidence import EvidenceRow
from .csv_out import normalize_cell_newlines, sanitize_csv_cell

# 列の順序（この並びで書く）。値の語彙は設計書 §12 が唯一の源
EVIDENCE_COLUMNS = (
    "email",
    "subject_id",
    "identity_quality",
    "current_seat",
    "month",
    "complete",
    "complete_months",
    "total_demand_usd",
    "code_demand_usd",
    "supplementary_high",
    "billed_extra_usd",
    "credit_limit_usd",
    "premium_justification_usd",
    "status",
    "seat_action",
    "credit_action",
    "reason_codes",
    "policy_stability",
    "suggested_credit_cap_usd",
)

# 複数値を1セルに収める区切り（reason_codes・complete_months）。カンマは CSV の
# 区切りと重なって引用符が必要になるため使わない
_SEPARATOR = ";"


def _text(value: object) -> str:
    """入力由来のテキスト（欠損は空欄）。

    式のエスケープと改行の正規化は、入力由来のテキストにだけ適用する。順序は csv_out と
    同じで式の判定が先（改行を先に均すと、CR で始まるセルに引用符が付かなくなる）。
    数値・真偽値から組み立てた文字列には掛けない（負の金額の "-" が式の先頭文字と
    一致し、引用符が付いて値が壊れるため）。
    """
    if value is None:
        return ""
    return normalize_cell_newlines(sanitize_csv_cell(str(value)))


def _usd(value: float | None) -> str:
    """金額（小数2桁・欠損は空欄）。無制限の追加クレジット上限は "inf" になる。"""
    return "" if value is None else f"{float(value):.2f}"


def _flag(value: bool | None) -> str:
    """真偽値（欠損は空欄。表記は recommendations.csv と同じ）。"""
    if value is None:
        return ""
    return "True" if value else "False"


def _count(value: int | None) -> str:
    """個数（欠損は空欄）。"""
    return "" if value is None else str(int(value))


def _joined(values: Sequence[object]) -> str:
    """複数値を1セルに収める（空なら空欄）。並びは受け取った順のまま。"""
    return _SEPARATOR.join(str(value) for value in values)


def _cells(row: EvidenceRow) -> dict[str, str]:
    """1行分のセル文字列（列は EVIDENCE_COLUMNS の順）。"""
    decision = row.decision
    return {
        "email": _text(row.email),
        "subject_id": _text(row.subject_id),
        "identity_quality": _text(row.identity_quality),
        "current_seat": _text(row.current_seat),
        "month": row.month,
        "complete": _flag(row.complete),
        "complete_months": _joined(row.complete_months),
        "total_demand_usd": _usd(row.total_demand_usd),
        "code_demand_usd": _usd(row.code_demand_usd),
        "supplementary_high": _flag(row.supplementary_high),
        "billed_extra_usd": _usd(row.billed_extra_usd),
        "credit_limit_usd": _usd(row.credit_limit_usd),
        "premium_justification_usd": _usd(row.premium_justification_usd),
        "status": decision.status.value,
        "seat_action": decision.seat_action.value,
        "credit_action": decision.credit_action.value,
        "reason_codes": _joined([code.value for code in decision.reason_codes]),
        "policy_stability": _count(row.policy_stability),
        "suggested_credit_cap_usd": _usd(row.suggested_credit_cap_usd),
    }


def write_decision_evidence(rows: Sequence[EvidenceRow], path: Path) -> None:
    """decision-evidence.csv を書く（行が0件でもヘッダだけ書く）。"""
    table = pd.DataFrame(
        [_cells(row) for row in rows], columns=list(EVIDENCE_COLUMNS)
    )
    table.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
