"""組織内の分布（参考値）の計算。

個々のユーザの数値が、その組織の中でどの位置にあるのかを読み手が判断できるように
するための統計量を出す。表示専用で、判定・推奨・警告には一切使わない。

母集団はシート未割当（`current_seat == "unassigned"`）を除いた分析対象ユーザ。
利用ゼロのユーザは含める（除くと中央値が上振れし、遊休の存在が統計から消える）。
シート不明（unknown）も含める（members の更新漏れ疑いであって、シートが割り当てられて
いないとは限らない）。組織サービス利用の行は analyze の段階で分離済みのため元から対象外。

指標ごとに欠損の扱いが違うため n は揃わない。指標ごとに n を持ち、表示側が必ず出す。
値の求まらない指標は行ごと落とす（`distributions()` の戻り値に現れない）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..product_usage import ProductUsage

# 判定対象外のシート（analyze が付ける current_seat の値）。母集団から除く
_UNASSIGNED = "unassigned"

# 値の単位。表示側が書式（金額 / 短縮表記の個数）を選ぶために持つ
KIND_USD = "usd"
KIND_COUNT = "count"

# 指標の識別子（表示ラベルとは別に持つ。ラベルの文言を変えても参照が壊れないよう、
# 棒グラフのガイド線のような「特定の指標を指す」用途はこちらを使う）
KEY_API_COST = "api_cost"
KEY_INPUT = "input"
KEY_OUTPUT = "output"
KEY_LOC = "loc"
KEY_BILLED = "billed"
KEY_REQUESTS = "requests"


@dataclass(frozen=True)
class Distribution:
    """1指標分の分布。

    n は指標ごとに違う（欠損の扱いが指標ごとに違うため）。std は母標準偏差で、
    分位点は線形補間（`_describe` の docstring を参照）。
    """

    key: str
    label: str
    kind: str
    n: int
    mean: float
    median: float
    std: float
    p25: float
    p75: float
    p90: float
    maximum: float


def population(users: pd.DataFrame) -> pd.DataFrame:
    """分布の母集団（シート未割当を除いた分析対象ユーザ）。

    分布・ガイド線・棒グラフの順位はこの1つの定義を共有する。別々に絞ると同じ図の中に
    母集団が2つでき、未割当ユーザに利用実績がある組織で順位と分布が食い違う。
    """
    return users[users["current_seat"] != _UNASSIGNED]


def distributions(users: pd.DataFrame,
                  usage: ProductUsage | None = None) -> list[Distribution]:
    """分析対象ユーザの指標ごとの分布（母集団が空なら空リスト）。

    users は `AnalysisResult.users`、usage は同 `product_usage`。usage は
    リクエスト数にだけ使う（速報は product_usage を持たないため省略できる）。
    """
    pop = population(users)
    if pop.empty:
        return []

    metrics = (
        (KEY_API_COST, "API換算需要", KIND_USD, _zero_filled(pop, "api_cost_usd")),
        (KEY_INPUT, "input", KIND_COUNT, _zero_filled(pop, "prompt_tokens")),
        (KEY_OUTPUT, "output", KIND_COUNT, _zero_filled(pop, "completion_tokens")),
        (KEY_LOC, "LoC", KIND_COUNT, _loc_values(pop)),
        (KEY_BILLED, "実課金", KIND_USD, _zero_filled(pop, "billed_extra_usd")),
        (KEY_REQUESTS, "リクエスト数", KIND_COUNT, _request_values(pop, usage)),
    )
    return [
        _describe(key, label, kind, values)
        for key, label, kind, values in metrics
        if values is not None and not values.empty
    ]


def _zero_filled(pop: pd.DataFrame, column: str) -> pd.Series | None:
    """欠損を 0 として扱う指標の値（列が無ければ None）。

    値が欠けているのは「その月に明細が無かった」＝利用ゼロなので、0 と読んでよい。
    """
    if column not in pop.columns:
        return None
    return pop[column].fillna(0).astype(float)


def _loc_values(pop: pd.DataFrame) -> pd.Series | None:
    """LoC の値（列が無ければ None）。0 のユーザは母集団から除く。

    analyze は code-analytics に行が無いユーザを 0 で埋めるため、users の段階では
    「行が無い」と「0 行」を区別できない。LoC の欠落は「コードを書いていない」を
    意味しないので、0 を母集団に入れず n を併記する。
    """
    if "loc_with_cc" not in pop.columns:
        return None
    values = pop["loc_with_cc"].fillna(0).astype(float)
    return values[values > 0]


def _request_values(pop: pd.DataFrame,
                    usage: ProductUsage | None) -> pd.Series | None:
    """リクエスト数の値（求まらなければ None）。

    users には無い指標なので、product 利用特徴量（index=email）を email で左結合して
    得る。users へ列を足さないのは、recommendations.csv が users の全列をそのまま
    書き出すため（列を足すとあの CSV の列構成が変わる）。

    spend に行が無いユーザは 0（回数ゼロが確定する）。`total_requests` が欠損の
    ユーザは母集団から除く（回数が分からないだけで、0 とは意味が違う）。spend に
    現れたユーザの中に確定値を持つ人が1人もいなければ、残るのは利用ゼロのメンバー
    だけになり全員 0 の退化した行にしかならないので、指標ごと落とす。
    """
    if usage is None or "total_requests" not in usage.features.columns:
        return None
    totals = usage.features["total_requests"]
    listed = pop["email"].isin(totals.index)
    values = pop["email"].map(totals).astype("Float64")
    if not bool((listed & values.notna()).any()):
        return None
    return values.where(listed, 0.0).dropna().astype(float)


def _describe(key: str, label: str, kind: str, values: pd.Series) -> Distribution:
    """値の並びから統計量を作る。

    標準偏差は母標準偏差（`ddof=0`）。母集団の全数を見ているので標本補正をせず、
    n=1 でも未定義にならない。分位点は pandas の `Series.quantile` 既定＝線形補間
    （並べ替えた値の間を按分する）で、標準ライブラリの `statistics.quantiles` とは
    値が異なる。どちらとも取れる書き方をせず、こちらに固定する。
    """
    return Distribution(
        key=key,
        label=label,
        kind=kind,
        n=int(len(values)),
        mean=float(values.mean()),
        median=float(values.median()),
        std=float(values.std(ddof=0)),
        p25=float(values.quantile(0.25)),
        p75=float(values.quantile(0.75)),
        p90=float(values.quantile(0.90)),
        maximum=float(values.max()),
    )
