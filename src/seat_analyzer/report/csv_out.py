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


def _normalize_cell_newlines(v):
    """セル内の改行を LF に揃える。

    lineterminator が持つのはレコード区切りだけで、引用符に囲まれたセルの中の改行は
    入力に入っていたものがそのまま出る。
    """
    if isinstance(v, str) and "\r" in v:
        return v.replace("\r\n", "\n").replace("\r", "\n")
    return v


def write_csv(result: AnalysisResult, path: Path) -> None:
    # 式のエスケープを先に判定する。改行を先に均すと、CR で始まるセルが
    # _FORMULA_PREFIXES に一致しなくなり引用符が付かないまま出る
    cells = result.users.map(_sanitize_csv_cell).map(_normalize_cell_newlines)
    cells.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
