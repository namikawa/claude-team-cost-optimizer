"""モデル単価テーブルによる API 換算コスト計算（spend 列の検証・fallback 用）。"""

from __future__ import annotations

import pandas as pd


def price_for_model(model: str, cfg: dict) -> tuple[float, float]:
    """(input, output) 単価 [USD per 1M tokens] を返す。部分一致・上から順に評価。"""
    name = str(model).lower()
    for pat in cfg["model_prices"]["patterns"]:
        if pat["match"].lower() in name:
            return float(pat["input"]), float(pat["output"])
    default = cfg["model_prices"]["default"]
    return float(default["input"]), float(default["output"])


def unmatched_models(models, cfg: dict) -> list[str]:
    """単価表のどのパターンにも一致せず default 単価が適用されるモデル名の一覧。

    新モデルの登場や表記変更に気づかず誤った単価で試算し続けるのを防ぐため、
    呼び出し側で警告に載せる。
    """
    patterns = [str(p["match"]).lower() for p in cfg["model_prices"]["patterns"]]
    names = {str(m) for m in models if m == m and m is not None}
    return sorted(n for n in names if not any(p in n.lower() for p in patterns))


CACHE_COLS = ("uncached_input_tokens", "cache_read_tokens",
              "cache_write_5m_tokens", "cache_write_1h_tokens")


def add_computed_cost(spend_df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """行ごとの tokens×単価 コスト computed_cost_usd を付与する。

    キャッシュ内訳列があれば実効単価（read 0.1x / write 1.25x / 2.0x）で計算する。
    prompt_tokens はキャッシュ読取を含むため、内訳なしで全量×入力単価にすると過大になる。
    需要指標 cost_usd と実課金 billed_usd の採用は apply_cost_basis に一本化する。
    """
    df = spend_df.copy()
    prices = df["model"].map(lambda m: price_for_model(m, cfg))
    in_price = prices.map(lambda p: p[0])
    out_price = prices.map(lambda p: p[1])

    if all(c in df.columns for c in CACHE_COLS):
        mult = cfg.get("cache_multipliers", {})
        read_m = float(mult.get("read", 0.1))
        w5m_m = float(mult.get("write_5m", 1.25))
        w1h_m = float(mult.get("write_1h", 2.0))
        input_cost = (
            df["uncached_input_tokens"].fillna(0) / 1e6 * in_price
            + df["cache_read_tokens"].fillna(0) / 1e6 * in_price * read_m
            + df["cache_write_5m_tokens"].fillna(0) / 1e6 * in_price * w5m_m
            + df["cache_write_1h_tokens"].fillna(0) / 1e6 * in_price * w1h_m
        )
    else:
        input_cost = df["prompt_tokens"].fillna(0) / 1e6 * in_price
    df["computed_cost_usd"] = (
        input_cost + df["completion_tokens"].fillna(0) / 1e6 * out_price
    )
    return df


def resolve_cost_basis(df: pd.DataFrame, cfg: dict) -> tuple[str, list[str]]:
    """api_cost の算出基準（computed / net_spend）を決定する。

    auto の場合、net_spend 合計が computed 合計の 50% 未満なら「spend列は実課金額
    （シート込み分が $0）」とみなし computed を採用する。
    """
    configured = str(cfg.get("cost_basis", "auto")).lower()
    if configured in ("computed", "net_spend"):
        return configured, []
    if "net_spend" not in df.columns or df["net_spend"].isna().all():
        return "computed", ["spend列が無いため tokens×単価 の計算値を需要指標に使用。"]
    net_total = float(df["net_spend"].fillna(0).sum())
    comp_total = float(df["computed_cost_usd"].sum())
    if comp_total > 0 and net_total < 0.5 * comp_total:
        return "computed", [
            f"cost_basis=auto: net_spend 合計 (${net_total:,.0f}) が API等価計算 "
            f"(${comp_total:,.0f}) の50%未満のため、spend列を実課金額と判定し "
            "computed（tokens×単価）を需要指標に採用しました。"
        ]
    return "net_spend", []


def apply_cost_basis(df: pd.DataFrame, basis: str) -> pd.DataFrame:
    """cost_usd（需要指標）と billed_usd（実課金）を basis に応じて設定する。"""
    df = df.copy()
    has_net = "net_spend" in df.columns
    if basis == "computed":
        df["cost_usd"] = df["computed_cost_usd"]
    elif has_net:
        df["cost_usd"] = df["net_spend"].where(df["net_spend"].notna(), df["computed_cost_usd"])
    else:
        df["cost_usd"] = df["computed_cost_usd"]
    df["billed_usd"] = df["net_spend"].fillna(0) if has_net else 0.0
    return df


def validate_spend(df: pd.DataFrame, cfg: dict) -> list[str]:
    """net_spend と computed_cost の乖離をユーザ単位で検証し、警告リストを返す。"""
    warnings: list[str] = []
    if "net_spend" not in df.columns or df["net_spend"].isna().all():
        warnings.append(
            "spend列（net_spend）が無いためモデル単価表による計算値を使用中。"
            "config.yaml > model_prices が最新か確認してください。"
        )
        return warnings

    threshold = float(cfg.get("spend_validation", {}).get("deviation_warn_ratio", 0.10))
    per_user = df.groupby("email")[["net_spend", "computed_cost_usd"]].sum()
    per_user = per_user[per_user["net_spend"] > 1.0]  # 少額ユーザはノイズが大きいので除外
    if per_user.empty:
        return warnings
    deviation = (per_user["net_spend"] - per_user["computed_cost_usd"]).abs() / per_user["net_spend"]
    outliers = deviation[deviation > threshold]
    if not outliers.empty:
        worst = outliers.sort_values(ascending=False).head(5)
        detail = ", ".join(f"{email} ({ratio:.0%})" for email, ratio in worst.items())
        warnings.append(
            f"spend突合: net_spend と tokens×単価 の乖離が {threshold:.0%} を超えるユーザが "
            f"{len(outliers)} 名います（例: {detail}）。ディスカウント適用・単価表の陳腐化・"
            "spend列の意味（実課金 vs API等価見積り）を確認してください。"
        )
    return warnings
