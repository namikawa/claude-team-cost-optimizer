"""recommendations.csv の書き出し（表計算ソフトが式と解釈しないためのエスケープ込み）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..analyze import AnalysisResult, OrgAnalysisResult
from .format import _account_rows, _sole_result

# Excel/スプレッドシートで式として解釈されうる先頭文字（formula injection 対策）
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_cell(v):
    """式として解釈されうるセルの先頭に引用符を付ける（同パッケージの CSV 出力で共有）。"""
    if isinstance(v, str) and v.startswith(_FORMULA_PREFIXES):
        return "'" + v
    return v


def normalize_cell_newlines(v):
    """セル内の改行を LF に揃える。

    lineterminator が持つのはレコード区切りだけで、引用符に囲まれたセルの中の改行は
    入力に入っていたものがそのまま出る。
    """
    if isinstance(v, str) and "\r" in v:
        return v.replace("\r\n", "\n").replace("\r", "\n")
    return v


def _recommendation_rows(result) -> pd.DataFrame:
    """recommendations.csv の行。

    複数 workspace の組織は全アカウントを主→副の順に連結し、email の次に workspace 列
    （ディレクトリ名）を置く（主の行の需要は判定に使った合算値のまま）。片方の
    workspace にしか無い列は空欄になる。それ以外は唯一の分析結果の users をそのまま
    使う（users だけを持つ代用物も従来どおり受け付ける）。
    """
    if not isinstance(result, OrgAnalysisResult):
        return result.users
    if not result.has_multiple_workspaces:
        return _sole_result(result).users
    rows = _account_rows(result, {name: r.users for name, r in result.workspaces.items()})
    rows = rows.drop(columns=["workspace_label"])
    # 欠損を持てる整数列（Int64）は、セルごとの変換（DataFrame.map）で float に戻されて
    # "12.0" と出る。値の型を保ったまま渡すため、汎用の列にしてから変換に通す
    integer = [col for col in rows.columns if isinstance(rows[col].dtype, pd.Int64Dtype)]
    return rows.astype(dict.fromkeys(integer, object)) if integer else rows


def write_csv(result: AnalysisResult | OrgAnalysisResult, path: Path) -> None:
    """recommendations.csv を書く。

    改行を均すのはセルの値だけで、ヘッダは通さない。列名は analyze が付ける正準名
    しか来ないので CR を含みえない。
    """
    # 式のエスケープを先に判定する。改行を先に均すと、CR で始まるセルが
    # _FORMULA_PREFIXES に一致しなくなり引用符が付かないまま出る
    cells = _recommendation_rows(result).map(sanitize_csv_cell).map(normalize_cell_newlines)
    cells.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
