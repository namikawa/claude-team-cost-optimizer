"""product 分類ポリシーに基づくユーザ単位の利用特徴量（費用は全 product・活用は Code）。

費用の比較は全 product の需要で行う一方、活用の評価は primary product（開発利用の
主軸）に絞って見る。そのための特徴量を、価格適用済みのスペンド明細から計算する。
分類は呼び出し側から渡される policy だけで決まり、このモジュールは設定を読み込まない
（DataFrame と policy を受けて返す純粋な計算に保つ）。

product 名の照合は正規化（前後空白の除去 → NFC → casefold）後の完全一致で行う。
部分一致・あいまい一致は実装しない。"Code Review" が "Claude Code" に一致するような
取り違えは、費用ではなく活用の評価を歪めるため、表記の近さで拾わない。表記ゆれは
policy の primary・supplementary に名前を並べて吸収する設計とする。

prohibited は primary / supplementary の分類と直交する属性で、同じ product 名を重ねて
指定できる。禁止指定は「その product を使わせない方針である」ことを表すだけで分類を
書き換えないため、prohibited かつ supplementary の需要は supplementary として数える。

観測していないものは 0・False で埋めない。product 名が空の行・requests が欠けた行・
列そのものが無い入力は、どれも「その行の値が分からない」として同じ規則で扱う。分からない
行の寄与を最小・最大に見積もった範囲を出し、結論が動かないときだけ値を確定させる:

- 合計（total_demand_usd・code_demand_usd・回数）は、分からない行の寄与が 0 のときだけ
  確定する。範囲の下限と上限が一致することがその条件になる
- 閾値判定（supplementary_high）は、範囲が閾値のどちら側に収まるかで確定する。寄与は
  正とは限らない（cost_basis=net_spend では返金等で負の値がありうる）ので下限も見る
- 個数（product_breadth）は、分母が全 requests で固定されているため、requests がすべて
  非負なら、分からない行の割り当ては既知 product を増やす方向にしか働かない。その単調性を
  使い、どの割り当てでも顔ぶれが変わらないと言えるときだけ確定する。分からない行を持つ
  ユーザに負の requests があれば保守的に欠損へ倒す
- 存在（prohibited_observed）は、1行でも観測していれば真が確定する。偽の側は、分からない
  行がその product だった可能性が残る限り確定しない
- policy に名前が1つも無い分類は、分からない行が一致しようがないので確定する

伝播はユーザ単位に閉じる（同じ入力に居る他のユーザの特徴量は変わらない）。

金額・回数の合計は浮動小数点の加算で行うため、入力行の順序によって最下位ビットが
変わりうる。閾値ちょうど（supplementary_high の境界・product_breadth の下限）の比較は
その粒度までは保証しない。
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple

import pandas as pd

from .domain import IssueCode, QualityIssue, Severity

# 特徴量の列（features はこの順で並ぶ）
FEATURE_COLUMNS = (
    "total_demand_usd",
    "code_demand_usd",
    "code_demand_share",
    "total_requests",
    "code_requests",
    "product_breadth",
    "supplementary_high",
    "prohibited_observed",
)

# 欠損を表現できる型に揃える。真偽値は bool ではなく boolean（bool は欠損を持てず、
# 「算出できなかった」が False と同じ値になってしまう）。回数は入力の数値列をそのまま
# 合計したもので整数とは限らないため、Int64 へ丸めず Float64 で保つ
_DTYPES = {
    "total_demand_usd": "Float64",
    "code_demand_usd": "Float64",
    "code_demand_share": "Float64",
    "total_requests": "Float64",
    "code_requests": "Float64",
    "product_breadth": "Int64",
    "supplementary_high": "boolean",
    "prohibited_observed": "boolean",
}

# product 名が分かって初めて意味を持つ特徴量（issue へ載せる順序もこの順）
_PRODUCT_FEATURES = (
    "code_demand_usd",
    "code_demand_share",
    "code_requests",
    "product_breadth",
    "supplementary_high",
    "prohibited_observed",
)

# product_breadth に数える product の下限（そのユーザの全 requests に対する比）
_BREADTH_MIN_SHARE = 0.05

# CAPACITY_SIGNAL_UNAVAILABLE の理由。列そのものが無い場合と、列はあるがセルが欠けて
# いる場合で、対象範囲（全ユーザ / 一部のユーザ）も対処も違うため scope で区別する
_REASON_COLUMN_MISSING = "column_missing"
_REASON_VALUE_MISSING = "value_missing"

# message へ載せる代表例の上限。総数と全件は scope で持つ
_MAX_LISTED = 5


@dataclass(frozen=True)
class ProductUsage:
    """ユーザ（email）単位の product 利用特徴量と、算出時に生じた品質issue。

    features は index=email・列=FEATURE_COLUMNS。確定できなかった特徴量は欠損で、
    0 や明細行数では代替しない。
    """

    features: pd.DataFrame
    issues: list[QualityIssue]


class _Bounds(NamedTuple):
    """ある集計について、分からない行の寄与を最小・最大に見積もった範囲。

    low == high なら、分からない行が何であっても結果は total で確定する。
    unbounded なユーザは値そのものが欠損した行を含み、範囲を定められない。
    """

    total: pd.Series
    low: pd.Series
    high: pd.Series
    unbounded: pd.Series


def compute(spend_df: pd.DataFrame, policy: Mapping) -> ProductUsage:
    """価格適用済みのスペンド明細から product 利用特徴量を計算する。

    spend_df は pricing.apply_cost_basis 適用済みのユーザ明細（組織サービス利用の行を
    除いたもの）。email と cost_usd が必須で、product と requests は任意。任意列が無い
    入力は「その列の値が全行で分からない」として、セルが欠けている場合と同じ規則で扱う。

    policy は config で検証済みの product_policy をそのまま渡す前提とする
    （primary・supplementary・prohibited・supplementary_high_usd）。
    """
    for column in ("email", "cost_usd"):
        if column not in spend_df.columns:
            raise ValueError(f"product 利用特徴量の計算には {column} 列が必要です")

    threshold = float(policy["supplementary_high_usd"])
    if not math.isfinite(threshold):
        # 非有限の閾値は比較が常に偽になり、supplementary_high が黙って無効化される
        raise ValueError("product_policy.supplementary_high_usd には有限な数値が必要です")

    # 元の DataFrame の index（複数月・複数ファイルの連結でラベルが重複しうる）に
    # 依存しないよう、必要な列だけを取り出して index を振り直す
    columns = ["email", "cost_usd"] + [
        c for c in ("product", "requests") if c in spend_df.columns
    ]
    work = spend_df[columns].reset_index(drop=True)
    has_product = "product" in work.columns
    has_requests = "requests" in work.columns

    email = work["email"]
    index = pd.Index(email.dropna().unique(), name="email").sort_values()
    all_rows = pd.Series(True, index=work.index)
    no_rows = pd.Series(False, index=work.index)

    # 列が無い入力は「全行のその値が分からない」と同じ。列の有無で経路を分けないことで、
    # 片方だけに確定の規則が入る（列が無いと確定できるものまで欠損になる）のを防ぐ
    demand = work["cost_usd"]
    requests = (
        work["requests"] if has_requests
        else pd.Series(float("nan"), index=work.index)
    )
    names = (
        _display_names(work["product"]) if has_product
        else pd.Series(pd.NA, index=work.index, dtype="string")
    )

    keys = _match_keys(names)
    primary_keys = _category_keys(policy, "primary")
    supplementary_keys = _category_keys(policy, "supplementary")
    prohibited_keys = _category_keys(policy, "prohibited")
    is_code = keys.isin(primary_keys)
    # prohibited と重なる supplementary もここに含める（禁止指定は分類を書き換えない）
    is_supplementary = keys.isin(supplementary_keys)
    is_prohibited = keys.isin(prohibited_keys)

    # product 名が分からない行は、名前のある分類ならどれにも入りうる
    unknown_name = keys.isna()
    maybe_code = _maybe(unknown_name, primary_keys)
    maybe_supplementary = _maybe(unknown_name, supplementary_keys)
    maybe_prohibited = _maybe(unknown_name, prohibited_keys)

    features = pd.DataFrame(index=index)
    features["total_demand_usd"] = _certain_sum(
        _sum_bounds(demand, email, index, certain=all_rows, possible=no_rows))
    features["code_demand_usd"] = _certain_sum(
        _sum_bounds(demand, email, index, certain=is_code, possible=maybe_code))
    # 分母 0 の比は定義できない。0 とは意味が違うため欠損にする
    # （分子が欠損のユーザは、その伝播で share も欠損になる）
    total_demand = features["total_demand_usd"]
    features["code_demand_share"] = (
        features["code_demand_usd"] / total_demand
    ).where(total_demand != 0)

    # 回数は明細行数で代替しない。表示用の構成比（analyze.aggregate_month の
    # product_breakdown）は行数で代替しているが、あちらは目安の表示で、こちらは後段の
    # 判定に効きうる数値なので、観測していない件数を作らない
    all_requests = _sum_bounds(requests, email, index, certain=all_rows, possible=no_rows)
    features["total_requests"] = _certain_sum(all_requests)
    # code_requests が見るのは primary 行と、primary かもしれない行の欠損だけ。分類の
    # はっきりした非 primary 行（Chat 等）の回数が欠けていても、primary の回数は動かない
    features["code_requests"] = _certain_sum(
        _sum_bounds(requests, email, index, certain=is_code, possible=maybe_code))
    # product_breadth は分類ではなく product の粒度で数えるため、名前の分からない行は
    # 名前のある分類かどうかに関係なく効きうる。確定できる範囲の判定は個数の性質を使うので
    # _product_breadth 側に置く。分母にも効くので回数の欠損は全行を見る
    features["product_breadth"] = _product_breadth(
        requests, email, keys, all_requests.total, unknown_name, index
    ).mask(all_requests.unbounded)

    features["supplementary_high"] = _certain_threshold(
        _sum_bounds(demand, email, index,
                    certain=is_supplementary, possible=maybe_supplementary),
        threshold,
    )

    observed = _any_by_email(is_prohibited, email, index)
    # 存在の主張は金額・回数に依らない（禁止 product を $0 で使っていても観測は観測）
    features["prohibited_observed"] = _only_true_is_certain(
        observed, _any_by_email(maybe_prohibited, email, index))

    issues: list[QualityIssue] = []
    if not has_product:
        issues.append(_product_column_issue(_unavailable_features(features)))
    elif bool(unknown_name.any()):
        issues.append(_unknown_name_issue(
            n_users=int(_any_by_email(unknown_name, email, index).sum()),
            n_rows=int(unknown_name.sum())))
    if bool(observed.any()):
        issues.append(_prohibited_issue(names[is_prohibited], int(observed.sum())))

    return ProductUsage(features=_finalize(features), issues=issues)


def _finalize(features: pd.DataFrame) -> pd.DataFrame:
    """列の順序と型を確定させる（列の並びと dtype を呼び出し順に依存させない）。"""
    return features[list(FEATURE_COLUMNS)].astype(_DTYPES)


def _subset_sum(values: pd.Series, email: pd.Series, index: pd.Index,
                rows: pd.Series) -> pd.Series:
    """rows の行だけを email 単位で合計する（行の無いユーザは空和の 0）。

    除外する行を 0 で置き換えるのではなく、実際に選んだ行だけを合計する。0 を挟むと
    加算のブロック割りが「その行だけを合計した場合」と変わり、部分集合の合計として
    正しくない丸めになりうる（複数の部分集合の値が同じ丸め先へ寄り、本来は違うはずの
    合計が一致してしまう）。並びと欠けは index に合わせる。
    """
    selected = values[rows]
    # fill_value は reindex で新しく足すラベル（＝選択行が無いユーザ）にだけ効く。
    # fillna で埋めると、集計の結果として本当に NaN になったユーザ（inf の相殺等）まで
    # 0 で確定してしまう
    return selected.groupby(email[rows]).sum().reindex(index, fill_value=0.0)


def _any_by_email(mask: pd.Series, email: pd.Series, index: pd.Index) -> pd.Series:
    """email 単位で「その条件に当てはまる行が1つでもあるか」。"""
    return mask.groupby(email).any().reindex(index).fillna(False).astype(bool)


def _sum_bounds(values: pd.Series, email: pd.Series, index: pd.Index, *,
                certain: pd.Series, possible: pd.Series) -> _Bounds:
    """email 単位の合計と、分からない行が動かしうる範囲。

    certain は必ず合計に入る行、possible は入るかどうかが決まらない行。possible のうち
    負の値だけを入れた合計が下限、正の値だけを入れた合計が上限になる。下限と上限が一致
    するのは、possible の値がすべて 0 のとき、つまりその行が何であっても結果が変わらない
    ときだけ。値そのものが欠損している行は範囲を定められないため、certain・possible の
    どちらかに入っていれば確定しないものとして扱う。

    3つの値はどれも「実際にありうる行の組をそのまま合計した値」にする。合計を分けて
    足し直したり、除外行に 0 を挟んだりして生まれる計算経路の食い違いは作らない
    （不明な寄与があるのに下限と上限が一致し、欠損の伝播が解ける形になるため）。
    別々の行の組の合計が同じ表現へ丸められること自体は残るが、それはどちらのシナリオでも
    同じ値になるという意味なので確定してよい（docstring 冒頭の免責の範囲）。
    """
    return _Bounds(
        total=_subset_sum(values, email, index, certain),
        low=_subset_sum(values, email, index, certain | (possible & (values < 0))),
        high=_subset_sum(values, email, index, certain | (possible & (values > 0))),
        unbounded=_any_by_email(values.isna() & (certain | possible), email, index),
    )


def _certain_sum(bounds: _Bounds) -> pd.Series:
    """分からない行が動かしえないユーザだけ合計を残す。"""
    return bounds.total.where((bounds.low == bounds.high) & ~bounds.unbounded)


def _certain_threshold(bounds: _Bounds, threshold: float) -> pd.Series:
    """下限が閾値以上なら真、上限が閾値未満なら偽、範囲が閾値をまたぐなら欠損。

    合計に対する閾値判定は「1行でも観測したか」ではないため、存在の主張と同じ扱いには
    できない。分からない行が全部その分類でも届かないなら偽が確定し、分からない行を
    最小に見積もっても超えているなら真が確定する。
    """
    known = ~bounds.unbounded
    result = pd.Series(pd.NA, index=bounds.low.index, dtype="boolean")
    return result.mask((bounds.low >= threshold) & known, True).mask(
        (bounds.high < threshold) & known, False)


def _only_true_is_certain(observed: pd.Series, unknown: pd.Series) -> pd.Series:
    """真はそのまま残し、偽は確定しないユーザで欠損にする。

    観測できた事実（真）は、分からない行が別に何であっても覆らない。観測できなかった
    こと（偽）は、分からない行がその product だった可能性が残るため確定しない。
    """
    return observed.astype("boolean").mask(unknown & ~observed)


def _maybe(unknown_name: pd.Series, category_keys: set[str]) -> pd.Series:
    """product 名の分からない行が、その分類に入りうるか（行単位）。

    policy に名前が1つも無い分類（既定の prohibited 等）は、名前が何であっても一致
    しようがない。この場合だけは寄与が 0 で確定するので、分からない行として扱わない。
    """
    if category_keys:
        return unknown_name
    return pd.Series(False, index=unknown_name.index)


def _display_names(products: pd.Series) -> pd.Series:
    """CSV に書かれていた product 名（前後空白のみ除去。空白だけの値は欠損扱い）。"""
    names = products.astype("string").str.strip()
    return names.mask(names == "")


def _match_key(name: str) -> str | None:
    """照合用の正準形。空文字になるものは product 名として扱わず None を返す。

    正規化の規則（前後空白の除去 → NFC → casefold）は config の product_policy 内
    重複判定と同じ。2箇所に分かれているため、片方を変えるときは両方を揃えること。
    """
    key = unicodedata.normalize("NFC", name.strip()).casefold()
    return key or None


def _match_keys(names: pd.Series) -> pd.Series:
    """product 名の列を照合用の正準形へ揃える（欠損はそのまま）。"""
    return names.map(_match_key, na_action="ignore")


def _category_keys(policy: Mapping, key: str) -> set[str]:
    """policy の分類リストを照合用キーの集合にする（照合は集合の反復順に依存しない）。"""
    names = policy.get(key)
    if not isinstance(names, list):
        return set()
    return {
        normalized
        for name in names
        if isinstance(name, str) and (normalized := _match_key(name)) is not None
    }


def _product_breadth(requests: pd.Series, email: pd.Series, keys: pd.Series,
                     totals: pd.Series, unknown_name: pd.Series,
                     index: pd.Index) -> pd.Series:
    """requests 比が下限以上の product の数（結論が動きうるユーザは欠損）。

    分母はそのユーザの全 requests（product 名の分からない行の分もすでに入っている）。
    product は照合用の正準形で束ねるので、表記ゆれは1つの product として数える。

    分母が動かないため、名前の分からない行をどの product へ割り当てても、既知 product の
    requests は増える方向にしか動かない。つまり顔ぶれが変わりうるのは「下限に届いていない
    product が届くようになる」向きだけで、次の2つが成り立てば、どの割り当てでも結果は同じ
    ＝確定する:
      - 分からない行を全部まとめて新しい product にしても下限に届かない
      - 下限に届いていない既知 product のうち最大のものへ全部注ぎ込んでも届かない
        （最大のものが届かないなら、他のどれも届かない）
    分からない行を持つユーザに負の requests があると確定させない。分からない行の負値は
    単調性そのものを壊す（既知 product を下限未満へ落としうる）。既知の行の負値は、
    2つめの条件が「別々に集計した値の加算」であるために効く: 桁落ちのある product では
    その和が「分からない行をそこへ割り当てたシナリオの直接合計」と食い違い、届かないと
    誤判定しうる。requests は実データでは非負のカウントなので、この保守的な条件が実運用の
    出力を変えることはない。分からない行が無いユーザには適用しない（割り当ての自由度が
    無く、桁落ちがあっても結論は1つに決まるため）。

    この2つは確定の十分条件であって必要条件ではない。崩れても結論が変わらない場合はある
    （例: 下限を超える既知 product が1つも無く、既知が requests 0 の product だけで、
    分からない行をどこへ帰属させても同じ1つの product が下限を超えるとき）が、その判定は
    「分からない行をどう束ねると下限以上の product をいくつ作れるか」という組合せ問題に
    なるため追わず、保守的に欠損へ倒す。確定と言った値が誤ることはない側の保証は保つ。

    requests の合計が 0 のユーザは比を定義できないため欠損にする（「比を計算した結果、
    下限を超える product が無かった」を表す 0 とは意味が違う）。
    """
    safe_totals = totals.where(totals > 0)
    per_product = requests.groupby([email, keys]).sum()
    denominator = pd.Series(
        safe_totals.reindex(per_product.index.get_level_values(0)).to_numpy(),
        index=per_product.index,
    )
    counted = per_product.div(denominator) >= _BREADTH_MIN_SHARE
    counts = counted.groupby(level=0).sum().reindex(index)

    # 下限に届いていない既知 product のうち最大のもの（1つも無ければ 0 として扱う）。
    # 比較は counted と同じ「requests / 全 requests」の形にして、分からない行が 0 の
    # ユーザでは counted の否定とそのまま一致するようにする
    largest_short = (
        per_product.where(~counted).groupby(level=0).max().reindex(index).fillna(0.0)
    )
    unknown_total = _subset_sum(requests, email, index, unknown_name)
    # 負値の検査は、分からない行を持つユーザについては全行に広げる（上記の理由）
    unsafe = (
        _any_by_email(unknown_name, email, index)
        & _any_by_email(requests < 0, email, index)
    )
    settled = (
        (unknown_total.div(safe_totals) < _BREADTH_MIN_SHARE)
        & ((largest_short + unknown_total).div(safe_totals) < _BREADTH_MIN_SHARE)
        & ~unsafe
    )
    return counts.where(settled)


def _unavailable_features(features: pd.DataFrame) -> tuple[str, ...]:
    """1人も確定できなかった特徴量の名前（宣言順）。

    product 名が分からなくても確定するものはある（名前が1つも無い分類・範囲が閾値を
    またがないユーザ等）。値の入った列は「算出できなかった列」ではないので挙げない。
    """
    return tuple(
        name for name in _PRODUCT_FEATURES if bool(features[name].isna().all())
    )


def _product_column_issue(unavailable: tuple[str, ...]) -> QualityIssue:
    """product 列そのものが無いことの警告。"""
    return QualityIssue(
        severity=Severity.WARNING,
        code=IssueCode.CAPACITY_SIGNAL_UNAVAILABLE,
        message=(
            "スペンドに product 列が無いため、product 別の特徴量を算出できません"
            "（Code 利用の分離・利用の広がりが不明）"
        ),
        scope={
            "column": "product",
            "reason": _REASON_COLUMN_MISSING,
            "features": unavailable,
        },
    )


def _unknown_name_issue(n_users: int, n_rows: int) -> QualityIssue:
    """product 名が空の行があり、そのユーザの特徴量を確定できないことの警告。"""
    return QualityIssue(
        severity=Severity.WARNING,
        code=IssueCode.CAPACITY_SIGNAL_UNAVAILABLE,
        message=(
            f"product 名が空の明細行が {n_rows} 行あります（{n_users} 名）。"
            "その行を Code として数えるかが決まらないため、該当ユーザの product 別の"
            "特徴量は確定できるものだけを算出します"
        ),
        scope={
            "column": "product",
            "reason": _REASON_VALUE_MISSING,
            "n_users": n_users,
            "n_rows": n_rows,
        },
    )


def _prohibited_issue(names: pd.Series, n_users: int) -> QualityIssue:
    """禁止指定の product が観測されたことの警告（seat 判定には影響しない）。"""
    # 設定に書かれた名前ではなく、実データに現れた表記を挙げる（該当行を探せるように）
    products = sorted({str(name) for name in names.dropna().unique()})
    listed = "、".join(products[:_MAX_LISTED])
    rest = len(products) - _MAX_LISTED
    more = f" ほか{rest}件" if rest > 0 else ""
    return QualityIssue(
        severity=Severity.WARNING,
        code=IssueCode.PROHIBITED_PRODUCT_OBSERVED,
        message=(
            f"policy で禁止指定された product の利用行があります: {listed}{more}"
            f"（{n_users} 名）。seat 判定には影響しません"
        ),
        scope={"products": tuple(products), "n_users": n_users},
    )
