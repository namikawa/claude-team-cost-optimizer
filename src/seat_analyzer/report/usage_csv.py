"""usage-summary.csv の書き出し（ユーザ単位の product 利用特徴量）。

分析時に計算済みの特徴量をそのまま出力する。行の追加・削除・再計算をしないため、
行の範囲は「対象月のスペンド明細に現れたユーザ」になる。利用ゼロのメンバーは行を
持たず、判定テーブル（recommendations.csv）とは対象が一致しない。

確定できなかった値は空欄にする。0 や False で埋めると「観測した結果が 0 だった」
ことと区別できなくなるため、欠損は欠損のまま出す。
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from ..analyze import AnalysisResult
from ..product_usage import FEATURE_COLUMNS
from .csv_out import normalize_cell_newlines, sanitize_csv_cell


def _usd(value) -> str:
    """金額（小数2桁）。"""
    return f"{float(value):.2f}"


def _share(value) -> str:
    """構成比（小数4桁）。"""
    return f"{float(value):.4f}"


def _count(value) -> str:
    """回数。入力の数値列の合計なので整数とは限らない。

    整数で表せる値には ".0" を付けない。表せない値は repr で出し、桁を落とさない。
    """
    number = float(value)
    if math.isfinite(number) and number == int(number):
        return str(int(number))
    return repr(number)


def _whole(value) -> str:
    """個数。"""
    return str(int(value))


def _flag(value) -> str:
    """真偽値（recommendations.csv と同じ表記）。"""
    return "True" if bool(value) else "False"


# 特徴量ごとの書式。列の顔ぶれは FEATURE_COLUMNS が持つので、ここは書式だけを決める
_FORMATTERS = {
    "total_demand_usd": _usd,
    "code_demand_usd": _usd,
    "code_demand_share": _share,
    "total_requests": _count,
    "code_requests": _count,
    "product_breadth": _whole,
    "supplementary_high": _flag,
    "prohibited_observed": _flag,
}


def _column(values: pd.Series, name: str) -> list[str]:
    """1列分のセル文字列（確定できなかった値は空欄）。"""
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        raise ValueError(f"usage-summary.csv に書式の決まっていない列があります: {name}")
    return ["" if pd.isna(v) else formatter(v) for v in values]


def write_usage_csv(result: AnalysisResult, path: Path) -> None:
    """usage-summary.csv を書く。

    列は email + FEATURE_COLUMNS、行は features の並び（email 昇順）のまま。
    """
    usage = result.product_usage
    if usage is None:
        raise ValueError(
            "usage-summary.csv を書けません: 分析結果に product 利用特徴量がありません"
        )
    features = usage.features

    # 式のエスケープと改行の正規化は email だけに適用する。対象は入力由来のテキストで、
    # 数値・真偽値から組み立てた文字列には掛けない（負の金額の "-" が式の先頭文字と
    # 一致し、引用符が付いて値が壊れるため）。順序は csv_out と同じで式の判定が先
    emails = [
        normalize_cell_newlines(sanitize_csv_cell(str(email)))
        for email in features.index
    ]
    table = pd.DataFrame({
        "email": emails,
        **{name: _column(features[name], name) for name in FEATURE_COLUMNS},
    })
    table.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
