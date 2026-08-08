"""recommendations.csv の書き出し（表計算ソフトが式と解釈しないためのエスケープ込み）。"""

from __future__ import annotations

from pathlib import Path

from ..analyze import AnalysisResult

# Excel/スプレッドシートで式として解釈されうる先頭文字（formula injection 対策）
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(v):
    if isinstance(v, str) and v.startswith(_FORMULA_PREFIXES):
        return "'" + v
    return v


def write_csv(result: AnalysisResult, path: Path) -> None:
    result.users.map(_sanitize_csv_cell).to_csv(path, index=False, encoding="utf-8-sig")
