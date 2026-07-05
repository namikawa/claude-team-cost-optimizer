"""ユーザ×月の集計と、シート損益分岐判定（ヒステリシス・感度分析・センサリング）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import ingest, pricing

SCENARIOS = ("low", "mid", "high")
SEAT_LABELS = {"standard": "Standard", "premium": "Premium", "unknown": "不明"}


@dataclass
class AnalysisResult:
    month: str
    users: pd.DataFrame
    summary: dict
    org: str | None = None  # 組織名（input/<org>/ レイアウト時。旧レイアウトは None）
    warnings: list[str] = field(default_factory=list)
    months_used: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)


def _seat_cost(api_cost: float, seat: str, scenario: str, cfg: dict) -> float:
    seat_cfg = cfg["seats"][seat]
    allowance = float(seat_cfg["allowance_usd"][scenario])
    return float(seat_cfg["price_usd"]) + max(0.0, api_cost - allowance)


def _recommend(api_cost: float, scenario: str, cfg: dict) -> tuple[str, float, float]:
    cost_std = _seat_cost(api_cost, "standard", scenario, cfg)
    cost_prem = _seat_cost(api_cost, "premium", scenario, cfg)
    rec = "standard" if cost_std <= cost_prem else "premium"
    return rec, cost_std, cost_prem


def aggregate_month(spend_df: pd.DataFrame) -> pd.DataFrame:
    """スペンド明細（cost_usd 付与済み）→ ユーザ単位の月次集計。"""
    agg_spec = {
        "api_cost": ("cost_usd", "sum"),
        "computed_cost": ("computed_cost_usd", "sum"),
        "prompt_tokens": ("prompt_tokens", "sum"),
        "completion_tokens": ("completion_tokens", "sum"),
    }
    if "billed_usd" in spend_df.columns:
        agg_spec["billed"] = ("billed_usd", "sum")
    grouped = spend_df.groupby("email").agg(**agg_spec)
    if "requests" in spend_df.columns:
        grouped["requests"] = spend_df.groupby("email")["requests"].sum()

    if "product" in spend_df.columns:
        def breakdown(g: pd.DataFrame) -> str:
            by_product = g.groupby("product")["cost_usd"].sum().sort_values(ascending=False)
            total = by_product.sum()
            if total <= 0:
                return ""
            return " / ".join(f"{p} {v / total:.0%}" for p, v in by_product.items() if v / total >= 0.01)

        grouped["product_breakdown"] = (
            spend_df.groupby("email")[["product", "cost_usd"]].apply(breakdown)
        )
    return grouped.reset_index()


def analyze(input_dir: str | Path, month: str, cfg: dict, org: str | None = None) -> AnalysisResult:
    """1組織分の分析。input_dir はその組織の入力ディレクトリ（spend/ 等を直下に持つ）。"""
    input_dir = Path(input_dir)
    warnings: list[str] = []

    # --- 対象月まで（ヒステリシス判定に必要な過去月含む）のスペンドをロード ---
    available = ingest.discover_months(input_dir)
    months_used = [m for m in available if m <= month]
    if month not in available:
        raise FileNotFoundError(
            f"{month} のスペンドレポートがありません。存在する月: {available or 'なし'}"
        )

    raw: dict[str, pd.DataFrame] = {}
    sources: dict = {"spend": {}}
    for m in months_used:
        result = ingest.load_spend(input_dir, m, cfg)
        warnings.extend(result.warnings)
        sources["spend"][m] = str(result.source)
        raw[m] = pricing.add_computed_cost(result.df, cfg)

    unknown_models = pricing.unmatched_models(
        pd.concat([df["model"] for df in raw.values()]).unique(), cfg
    )
    if unknown_models:
        warnings.append(
            f"model_prices: 単価表に一致しないモデルに default 単価を適用: {unknown_models}。"
            "config.yaml > model_prices にパターンを追記してください"
        )

    # 需要指標の基準（computed / net_spend）を対象月のユーザ帰属行から決定し、全月に適用
    target_user_rows = raw[month][raw[month]["email"].str.contains("@", na=False)]
    basis, basis_notes = pricing.resolve_cost_basis(target_user_rows, cfg)
    warnings.extend(basis_notes)

    monthly: dict[str, pd.DataFrame] = {}
    org_usage: dict = {}
    for m, df_raw in raw.items():
        df = pricing.apply_cost_basis(df_raw, basis)
        if m == month and basis == "net_spend":
            warnings.extend(pricing.validate_spend(df, cfg))
        # ユーザ非帰属の組織利用（例: "(org service usage)" の Code Review 等）は
        # シート判定の対象外として分離し、別枠で計上する
        is_user = df["email"].str.contains("@", na=False)
        org_df = df[~is_user]
        if m == month and not org_df.empty:
            org_usage = {
                "cost_usd": round(float(org_df["billed_usd"].sum()), 2),
                "by_product": {
                    str(k): round(float(v), 2)
                    for k, v in org_df.groupby("product")["billed_usd"].sum().items()
                } if "product" in org_df.columns else {},
            }
        monthly[m] = aggregate_month(df[is_user])

    members_result = ingest.load_members(input_dir, month, cfg)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)

    code_result = ingest.load_code_analytics(input_dir, month, cfg)
    if code_result is not None:
        warnings.extend(code_result.warnings)
        sources["code_analytics"] = str(code_result.source)

    # --- 対象月テーブル: members と spend の全ユーザを対象にする（利用ゼロも含む）---
    target = monthly[month].set_index("email")
    emails = sorted(set(members["email"]) | set(target.index))
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    decision_cfg = cfg["decision"]
    n_hyst = int(decision_cfg["hysteresis_months"])
    buffer_ratio = float(decision_cfg["buffer_ratio"])
    censoring_margin = float(decision_cfg["censoring_margin"])
    seat_diff = float(cfg["seats"]["premium"]["price_usd"]) - float(cfg["seats"]["standard"]["price_usd"])
    min_saving = buffer_ratio * seat_diff
    s_allowance_mid = float(cfg["seats"]["standard"]["allowance_usd"]["mid"])

    def _costs_for(seat: str, api_cost: float, billed: float, scenario: str) -> tuple[str, float, float]:
        """現シートは観測実績（シート料+実課金）、変更先は allowance モデルで試算する。

        従量課金が有効な組織では billed（実課金）が「そのシートでの実コスト」の
        観測値であり、allowance 推定より信頼できる。変更先のコストは観測できない
        ため allowance モデルで試算するが、込み量の大小関係
        （Standard の込み量 ≤ Premium の込み量）から観測値で上下に拘束する:
          - Standard ユーザ → Premium に変えた場合の超過課金 ≤ 現在の実課金
          - Premium ユーザ → Standard に変えた場合の超過課金 ≥ 現在の実課金
        """
        std_price = float(cfg["seats"]["standard"]["price_usd"])
        prem_price = float(cfg["seats"]["premium"]["price_usd"])
        cost_std = _seat_cost(api_cost, "standard", scenario, cfg)
        cost_prem = _seat_cost(api_cost, "premium", scenario, cfg)
        if seat == "standard":
            cost_std = std_price + billed
            cost_prem = prem_price + min(cost_prem - prem_price, billed)
        elif seat == "premium":
            cost_prem = prem_price + billed
            cost_std = std_price + max(cost_std - std_price, billed)
        rec = "standard" if cost_std <= cost_prem else "premium"
        return rec, cost_std, cost_prem

    rows = []
    for email in emails:
        seat = seat_by_email.get(email, "unknown")
        row = target.loc[email] if email in target.index else None
        api_cost = float(row["api_cost"]) if row is not None else 0.0
        billed = (
            float(row["billed"]) if row is not None and "billed" in row.index else 0.0
        )

        recs = {s: _costs_for(seat, api_cost, billed, s) for s in SCENARIOS}
        rec_mid, cost_std, cost_prem = recs["mid"]

        # 現シートでのコスト（観測実績）と、推奨シートに変えた場合の削減額（mid）
        if seat == "standard":
            cost_current, saving = cost_std, cost_std - min(cost_std, cost_prem)
        elif seat == "premium":
            cost_current, saving = cost_prem, cost_prem - min(cost_std, cost_prem)
        else:
            cost_current = float("nan")
            saving = float("nan")

        # ヒステリシス: 直近 n_hyst ヶ月すべてで同じ推奨・削減額がバッファ以上か
        if seat == "unknown":
            status = "シート不明"
        elif rec_mid == seat:
            status = "現状維持"
        else:
            recent = months_used[-n_hyst:]
            checks = []
            for m in recent:
                mdf = monthly[m].set_index("email")
                if email in mdf.index:
                    m_cost = float(mdf.loc[email, "api_cost"])
                    m_billed = float(mdf.loc[email, "billed"]) if "billed" in mdf.columns else 0.0
                else:
                    m_cost, m_billed = 0.0, 0.0
                m_rec, m_std, m_prem = _costs_for(seat, m_cost, m_billed, "mid")
                m_current = m_std if seat == "standard" else m_prem
                m_saving = m_current - min(m_std, m_prem)
                checks.append(m_rec == rec_mid and m_saving >= min_saving)
            if len(months_used) < n_hyst:
                status = "要観察（データ蓄積待ち）"
            elif all(checks):
                status = "変更推奨"
            else:
                status = "要観察"

        # 感度: low/high シナリオが mid の推奨と一致するか
        agree = sum(1 for s in ("low", "high") if recs[s][0] == rec_mid)
        confidence = {2: "高", 1: "中", 0: "低"}[agree]

        # 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ:
        # 「実効込み量が推定より大きい」か「上限で止められた」かの要確認フラグ
        censored = (
            seat == "standard"
            and billed == 0.0
            and api_cost >= censoring_margin * s_allowance_mid
        )

        rows.append({
            "email": email,
            "current_seat": seat,
            "api_cost_usd": round(api_cost, 2),
            "cost_if_standard_usd": round(cost_std, 2),
            "cost_if_premium_usd": round(cost_prem, 2),
            "cost_current_usd": round(cost_current, 2) if cost_current == cost_current else None,
            "recommended_seat": rec_mid,
            "monthly_saving_usd": round(saving, 2) if saving == saving else None,
            "status": status,
            "confidence": confidence,
            "rec_low": recs["low"][0],
            "rec_high": recs["high"][0],
            "cap_suspected": censored,
            "billed_extra_usd": round(billed, 2),
            "prompt_tokens": int(row["prompt_tokens"]) if row is not None else 0,
            "completion_tokens": int(row["completion_tokens"]) if row is not None else 0,
            "product_breakdown": (
                str(row["product_breakdown"]) if row is not None and "product_breakdown" in row.index else ""
            ),
        })

    users = pd.DataFrame(rows)

    # 活用度（Claude Code 貢献データ）の結合
    if code_result is not None:
        cc = code_result.df.set_index("email")
        for col in ("prs_with_cc", "loc_with_cc"):
            if col in cc.columns:
                users[col] = users["email"].map(cc[col]).fillna(0).astype(int)

    users = users.sort_values(
        ["status", "monthly_saving_usd"], ascending=[True, False]
    ).reset_index(drop=True)

    # spend にいるが members にいないユーザ
    orphan = users[users["current_seat"] == "unknown"]["email"].tolist()
    if orphan:
        warnings.append(
            f"members に存在しない利用ユーザ {len(orphan)} 名（シート不明として集計）: {orphan[:5]}"
        )

    summary = _summarize(users, monthly[month], cfg, months_used, n_hyst)
    summary["org_service_cost_usd"] = org_usage.get("cost_usd", 0.0)
    summary["org_service_by_product"] = org_usage.get("by_product", {})
    return AnalysisResult(
        month=month, users=users, summary=summary, org=org,
        warnings=warnings, months_used=months_used, sources=sources,
    )


def _summarize(users: pd.DataFrame, month_agg: pd.DataFrame, cfg: dict,
               months_used: list[str], n_hyst: int) -> dict:
    seats = users["current_seat"].value_counts().to_dict()
    std_price = float(cfg["seats"]["standard"]["price_usd"])
    prem_price = float(cfg["seats"]["premium"]["price_usd"])
    seat_cost_now = seats.get("standard", 0) * std_price + seats.get("premium", 0) * prem_price

    to_change = users[users["status"] == "変更推奨"]
    watching = users[users["status"].str.startswith("要観察")]
    savings = float(to_change["monthly_saving_usd"].fillna(0).sum())

    return {
        "month": users_month(months_used),
        "n_members": int(len(users)),
        "n_standard": int(seats.get("standard", 0)),
        "n_premium": int(seats.get("premium", 0)),
        "n_unknown": int(seats.get("unknown", 0)),
        "seat_cost_now_usd": round(seat_cost_now, 2),
        "total_api_cost_usd": round(float(month_agg["api_cost"].sum()), 2),
        "n_change_recommended": int(len(to_change)),
        "n_watching": int(len(watching)),
        "n_cap_suspected": int(users["cap_suspected"].sum()),
        "total_billed_extra_usd": round(
            float(users["billed_extra_usd"].fillna(0).sum())
            if "billed_extra_usd" in users.columns else 0.0, 2),
        "est_monthly_saving_usd": round(savings, 2),
        "months_used": months_used,
        "hysteresis_months": n_hyst,
    }


def users_month(months_used: list[str]) -> str:
    return months_used[-1] if months_used else ""
