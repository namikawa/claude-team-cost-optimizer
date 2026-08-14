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

観測していないものは 0・False で埋めない。列が無い場合だけでなく、product 名が空で
分類できない行や requests が欠けた行があるユーザでも、その行の内容しだいで変わる特徴量は
欠損にする。例外は「1行でも観測した」という存在の主張（prohibited_observed と
supplementary_high の真）で、他の行が何であれ確定するため残す。確定しないのは不在の主張
（偽）の側だけ、という非対称をそのまま特徴量に持たせる。

金額・回数の合計は浮動小数点の加算で行うため、入力行の順序によって最下位ビットが
変わりうる。閾値ちょうど（supplementary_high の境界・product_breadth の下限）の比較は
その粒度までは保証しない。
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

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

# product 列が無いと算出できない特徴量
_PRODUCT_DEPENDENT = (
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

    features は index=email・列=FEATURE_COLUMNS。算出できなかった特徴量は欠損で、
    0 や明細行数では代替しない。
    """

    features: pd.DataFrame
    issues: list[QualityIssue]


def compute(spend_df: pd.DataFrame, policy: Mapping) -> ProductUsage:
    """価格適用済みのスペンド明細から product 利用特徴量を計算する。

    spend_df は pricing.apply_cost_basis 適用済みのユーザ明細（組織サービス利用の行を
    除いたもの）。email と cost_usd が必須で、product と requests は任意。任意列が無い
    場合と、その列のセルが欠けている場合のどちらでも、確定できない特徴量は欠損にする
    （セル欠けの伝播はそのユーザの行だけを見るので、他のユーザの特徴量は変わらない）。

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
    issues: list[QualityIssue] = []
    features = pd.DataFrame(index=index)

    features["total_demand_usd"] = _sum_by_email(work["cost_usd"], email, index)

    # requests は任意列。無い場合に明細行数で代替しない。表示用の構成比
    # （analyze.aggregate_month の product_breakdown）は行数で代替しているが、あちらは
    # 目安の表示で、こちらは後段の判定に効きうる数値なので、観測していない件数を作らない。
    # セルが欠けている場合も同じ扱いにする（合計は欠損を読み飛ばすため、全件欠損でも 0、
    # 一部欠損でも観測できた分だけの値が「回数」として返ってしまう）。欠損が1つでもあれば
    # requests 由来の特徴量をすべて欠損にする。primary 行の欠損だけを code_requests へ
    # 効かせる切り分けは、分類できない行の requests が primary だった可能性を排除できず
    # 正確にならないため採らない
    if has_requests:
        unknown_requests = _any_by_email(work["requests"].isna(), email, index)
        request_totals = _sum_by_email(work["requests"], email, index)
        features["total_requests"] = request_totals.mask(unknown_requests)
    else:
        unknown_requests = None
        request_totals = None
        features["total_requests"] = _missing(index, "Float64")

    if not has_product:
        for column in _PRODUCT_DEPENDENT:
            features[column] = _missing(index, _DTYPES[column])
        issues.append(QualityIssue(
            severity=Severity.WARNING,
            code=IssueCode.CAPACITY_SIGNAL_UNAVAILABLE,
            message=(
                "スペンドに product 列が無いため、product 別の特徴量を算出できません"
                "（Code 利用の分離・利用の広がりが不明）"
            ),
            scope={
                "column": "product",
                "reason": _REASON_COLUMN_MISSING,
                "features": _PRODUCT_DEPENDENT,
            },
        ))
        return ProductUsage(features=_finalize(features), issues=issues)

    names = _display_names(work["product"])
    keys = _match_keys(names)
    primary_keys = _category_keys(policy, "primary")
    supplementary_keys = _category_keys(policy, "supplementary")
    prohibited_keys = _category_keys(policy, "prohibited")
    is_code = keys.isin(primary_keys)
    # prohibited と重なる supplementary もここに含める（禁止指定は分類を書き換えない）
    is_supplementary = keys.isin(supplementary_keys)
    is_prohibited = keys.isin(prohibited_keys)

    # product 名が空の行は分類できない。その行が Code だった可能性を否定できないため、
    # 同じユーザの product 由来の特徴量を確定させない（伝播はユーザ単位に閉じる）
    unclassified = keys.isna()
    unknown_product = _any_by_email(unclassified, email, index)
    unknown_code = _uncertain(unknown_product, primary_keys)
    if bool(unknown_product.any()):
        issues.append(_unclassified_issue(
            n_users=int(unknown_product.sum()), n_rows=int(unclassified.sum())))

    features["code_demand_usd"] = _sum_by_email(
        work["cost_usd"].where(is_code, 0.0), email, index).mask(unknown_code)
    # 分母 0 の比は定義できない。0 とは意味が違うため欠損にする
    # （分子が欠損のユーザは、その伝播で share も欠損になる）
    total_demand = features["total_demand_usd"]
    features["code_demand_share"] = (
        features["code_demand_usd"] / total_demand
    ).where(total_demand != 0)

    if has_requests:
        features["code_requests"] = _sum_by_email(
            work["requests"].where(is_code, 0.0), email, index
        ).mask(unknown_code | unknown_requests)
        # product_breadth は分類ではなく product の粒度で数えるので、policy の中身に
        # かかわらず分類できない行があれば確定しない（その行が既存の product の比を
        # 押し上げたのか、別の product だったのかが決まらない）
        features["product_breadth"] = _product_breadth(
            work["requests"], email, keys, request_totals, index
        ).mask(unknown_product | unknown_requests)
    else:
        features["code_requests"] = _missing(index, "Float64")
        features["product_breadth"] = _missing(index, "Int64")

    # 存在の主張（1行でも観測した）は、分類できない行があっても確定する。不在の主張は
    # 確定しない（空の行が禁止 product・補助 product だった可能性が残るため）
    supplementary_demand = _sum_by_email(
        work["cost_usd"].where(is_supplementary, 0.0), email, index)
    features["supplementary_high"] = _only_true_is_certain(
        supplementary_demand >= threshold,
        _uncertain(unknown_product, supplementary_keys))

    observed = _any_by_email(is_prohibited, email, index)
    features["prohibited_observed"] = _only_true_is_certain(
        observed, _uncertain(unknown_product, prohibited_keys))
    if bool(observed.any()):
        issues.append(_prohibited_issue(names[is_prohibited], int(observed.sum())))

    return ProductUsage(features=_finalize(features), issues=issues)


def _finalize(features: pd.DataFrame) -> pd.DataFrame:
    """列の順序と型を確定させる（列の並びと dtype を呼び出し順に依存させない）。"""
    return features[list(FEATURE_COLUMNS)].astype(_DTYPES)


def _sum_by_email(values: pd.Series, email: pd.Series, index: pd.Index) -> pd.Series:
    """email 単位の合計。並びと欠けを index に合わせる（groupby の結果順に依存させない）。"""
    return values.groupby(email).sum().reindex(index)


def _any_by_email(mask: pd.Series, email: pd.Series, index: pd.Index) -> pd.Series:
    """email 単位で「その条件に当てはまる行が1つでもあるか」。"""
    return mask.groupby(email).any().reindex(index).fillna(False).astype(bool)


def _uncertain(unknown_product: pd.Series, category_keys: set[str]) -> pd.Series:
    """分類できない行が、その分類についての結論を変えうるか。

    policy に名前が1つも無い分類（既定の prohibited 等）は、空の行が何であっても
    一致しようがない。この場合だけは「使っていない」が確定するので伝播させない。
    """
    if category_keys:
        return unknown_product
    return pd.Series(False, index=unknown_product.index)


def _only_true_is_certain(observed: pd.Series, unknown: pd.Series) -> pd.Series:
    """真はそのまま残し、偽は確定しないユーザで欠損にする。

    観測できた事実（真）は、分類できない行が別に何であっても覆らない。観測できなかった
    こと（偽）は、分類できない行がその product だった可能性が残るため確定しない。
    """
    return observed.astype("boolean").mask(unknown & ~observed)


def _missing(index: pd.Index, dtype: str) -> pd.Series:
    """欠損で埋めた列。算出できない特徴量を 0 や行数で代替しないことを型で表す。"""
    return pd.Series(pd.NA, index=index, dtype=dtype)


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
                     totals: pd.Series, index: pd.Index) -> pd.Series:
    """requests 比が下限以上の product の数（比を定義できないユーザは欠損）。

    分母はそのユーザの全 requests（product 名が空の行の分も含む）。product は照合用の
    正準形で束ねるので、表記ゆれは1つの product として数える。requests の合計が 0 の
    ユーザは比を定義できないため欠損にする（「比を計算した結果、下限を超える product が
    無かった」を表す 0 とは意味が違う）。
    """
    per_product = requests.groupby([email, keys]).sum()
    denominator = pd.Series(
        totals.reindex(per_product.index.get_level_values(0)).to_numpy(),
        index=per_product.index,
    )
    share = per_product.div(denominator.where(denominator > 0))
    counts = (share >= _BREADTH_MIN_SHARE).groupby(level=0).sum()
    return counts.reindex(index).where(totals > 0)


def _unclassified_issue(n_users: int, n_rows: int) -> QualityIssue:
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
