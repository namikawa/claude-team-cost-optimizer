"""ユーザ×月の集計と、シート損益分岐判定（ヒステリシス・感度分析・センサリング）。"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import ingest, pricing

# モデル名を表示用に短縮する（claude-opus-4-8 → Opus 4.8, claude-fable-5 → Fable 5）
_MODEL_SHORT_RE = re.compile(r"(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?", re.I)


def _short_model(name: str) -> str:
    """API モデル名を表示用の短縮形にする。判別できない場合は元の文字列を返す。"""
    m = _MODEL_SHORT_RE.search(str(name))
    if not m:
        return str(name)
    family = m.group(1).capitalize()
    version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"{family} {version}"

SCENARIOS = ("low", "mid", "high")
SEAT_LABELS = {"standard": "Standard", "premium": "Premium",
               "unassigned": "未割当", "unknown": "不明"}

# 速報モード: 観測需要がこの額 [USD] 未満なら「遊休候補」（数日〜半月でこの水準は実質未使用）
PREVIEW_IDLE_OBS_USD = 1.0


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

    if "model" in spend_df.columns:
        tmp = spend_df.assign(
            _tok=spend_df["prompt_tokens"].fillna(0) + spend_df["completion_tokens"].fillna(0)
        )

        def model_bd(g: pd.DataFrame) -> str:
            # モデル利用割合はトークン量（input+output）基準。寄与降順・1%未満は集約
            by_model = g.groupby("model")["_tok"].sum().sort_values(ascending=False)
            total = by_model.sum()
            if total <= 0:
                return ""
            return " / ".join(
                f"{_short_model(m)} {v / total:.0%}"
                for m, v in by_model.items() if v / total >= 0.01
            )

        grouped["model_breakdown"] = (
            tmp.groupby("email")[["model", "_tok"]].apply(model_bd)
        )
    return grouped.reset_index()


def _merge_members_info(users: pd.DataFrame, input_dir: Path, cfg: dict, sources: dict) -> None:
    """任意ファイル members-info.csv の department/team/role/note を email で users に付与する。

    未登録メンバーは空文字列。members-info にだけ居るメールは行を追加しない。
    ファイルが読めた場合のみ sources["members_info"] にパスを記録する。
    """
    info_result = ingest.load_members_info(input_dir, cfg)
    if info_result is None:
        for col in ("department", "team", "role", "note"):
            users[col] = ""
        return
    sources["members_info"] = str(info_result.source)
    info = info_result.df.set_index("email")
    for col in ("department", "team", "role", "note"):
        users[col] = users["email"].map(info[col]).fillna("") if col in info.columns else ""
    # 部署・チームは兼務（複数所属）を正規化した表示文字列で保持する（集計時に再分割）
    for col in ("department", "team"):
        users[col] = users[col].map(ingest.normalize_affiliations)


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
        # ファイル名の期間が全月に満たない場合、月額前提の判定が歪む（過小評価）
        period = ingest.file_period(result.source)
        if period is not None and period.days is not None:
            year, mon = (int(x) for x in m.split("-"))
            if period.days < calendar.monthrange(year, mon)[1]:
                warnings.append(
                    f"{result.source.name}: {m} は部分月データ"
                    f"（{period.start:%m-%d}〜{period.end:%m-%d} の {period.days}日分）ですが"
                    "全月として扱っています。月中の一次判断には --preview を利用してください"
                )

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

        if seat == "unassigned":
            # 意図的な未割当（別組織でアサイン済み・管理者等）は損益分岐判定の対象外。
            # シート料 $0 の現状が最安のため、推奨もコスト試算も行わない
            nan = float("nan")
            rec_mid, cost_std, cost_prem = "unassigned", nan, nan
            rec_low = rec_high = "unassigned"
            cost_current, saving = nan, nan
            status, confidence = "対象外（シート未割当）", "—"
            censored = False
        else:
            recs = {s: _costs_for(seat, api_cost, billed, s) for s in SCENARIOS}
            rec_mid, cost_std, cost_prem = recs["mid"]
            rec_low, rec_high = recs["low"][0], recs["high"][0]

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
            "rec_low": rec_low,
            "rec_high": rec_high,
            "cap_suspected": censored,
            "billed_extra_usd": round(billed, 2),
            "prompt_tokens": int(row["prompt_tokens"]) if row is not None else 0,
            "completion_tokens": int(row["completion_tokens"]) if row is not None else 0,
            "product_breakdown": (
                str(row["product_breakdown"]) if row is not None and "product_breakdown" in row.index else ""
            ),
            "model_breakdown": (
                str(row["model_breakdown"]) if row is not None and "model_breakdown" in row.index else ""
            ),
        })

    users = pd.DataFrame(rows)

    # 部署・職種・備考（任意ファイル members-info.csv）の結合
    _merge_members_info(users, input_dir, cfg, sources)

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
    warnings.extend(_warn_active_unassigned(users, "api_cost_usd"))

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
        "n_unassigned": int(seats.get("unassigned", 0)),
        "n_unknown": int(seats.get("unknown", 0)),
        "seat_cost_now_usd": round(seat_cost_now, 2),
        "seat_price_standard_usd": std_price,
        "seat_price_premium_usd": prem_price,
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


def _warn_active_unassigned(users: pd.DataFrame, cost_col: str) -> list[str]:
    """シート未割当なのに利用実績があるユーザの警告（データ不整合・月中解約の手がかり）。"""
    active = users[
        (users["current_seat"] == "unassigned") & (users[cost_col] > 0)
    ]["email"].tolist()
    if not active:
        return []
    return [
        f"シート未割当なのに利用実績があるユーザ {len(active)} 名: {active[:5]}"
        "（members の更新漏れ、または月中のシート解除の可能性）"
    ]


# --- 速報モード（部分月データからの一次判断） ---

@dataclass
class PreviewResult:
    month: str
    users: pd.DataFrame
    summary: dict
    days_observed: int
    days_in_month: int
    org: str | None = None
    warnings: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)


def _preview_label(seat: str, api_obs: float, api_proj: float, cfg: dict,
                   min_saving: float) -> tuple[str, str]:
    """月末ペース換算需要を allowance モデルにかけた一次判断ラベルと確度。

    実課金の観測は部分月では非線形（込み量を使い切るまで $0）で月額に換算できない
    ため、正式分析と違い観測実課金による拘束は行わず、純粋なモデル判定のみ。
    境界付近（3シナリオ不一致 or 削減見込みがバッファ未満）は「判断保留」に倒す。
    """
    if seat == "unassigned":
        return "対象外（未割当）", "—"
    if seat == "unknown":
        return "シート不明", "—"
    if api_obs < PREVIEW_IDLE_OBS_USD:
        return "遊休候補", "—"
    recs = {s: _recommend(api_proj, s, cfg) for s in SCENARIOS}
    rec_mid, cost_std, cost_prem = recs["mid"]
    agree = sum(1 for s in ("low", "high") if recs[s][0] == rec_mid)
    confidence = {2: "高", 1: "中", 0: "低"}[agree]
    if rec_mid == seat:
        return ("Premium妥当" if seat == "premium" else "Standard妥当"), confidence
    saving = (cost_prem - cost_std) if seat == "premium" else (cost_std - cost_prem)
    if agree == 2 and saving >= min_saving:
        return ("Standard候補" if seat == "premium" else "Premium検討"), confidence
    return "判断保留", confidence


def preview(input_dir: str | Path, month: str, cfg: dict, days_observed: int,
            org: str | None = None) -> PreviewResult:
    """部分月データの一次判断。対象月のみ使用し、ヒステリシス・変更推奨は行わない。"""
    input_dir = Path(input_dir)
    warnings: list[str] = []

    year, mon = (int(x) for x in month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    if not 1 <= days_observed <= days_in_month:
        raise ValueError(f"--days は 1〜{days_in_month}（{month} の暦日数）で指定してください")
    factor = days_in_month / days_observed

    spend_result = ingest.load_spend(input_dir, month, cfg)
    warnings.extend(spend_result.warnings)
    sources = {"spend": {month: str(spend_result.source)}}
    df = pricing.add_computed_cost(spend_result.df, cfg)

    unknown_models = pricing.unmatched_models(df["model"].unique(), cfg)
    if unknown_models:
        warnings.append(
            f"model_prices: 単価表に一致しないモデルに default 単価を適用: {unknown_models}。"
            "config.yaml > model_prices にパターンを追記してください"
        )

    is_user = df["email"].str.contains("@", na=False)
    basis, basis_notes = pricing.resolve_cost_basis(df[is_user], cfg)
    warnings.extend(basis_notes)
    df = pricing.apply_cost_basis(df, basis)
    org_service_obs = round(float(df[~is_user]["billed_usd"].sum()), 2)
    agg = aggregate_month(df[is_user]).set_index("email")

    members_result = ingest.load_members(input_dir, month, cfg)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    seat_diff = float(cfg["seats"]["premium"]["price_usd"]) - float(cfg["seats"]["standard"]["price_usd"])
    min_saving = float(cfg["decision"]["buffer_ratio"]) * seat_diff

    rows = []
    for email in sorted(set(members["email"]) | set(agg.index)):
        seat = seat_by_email.get(email, "unknown")
        row = agg.loc[email] if email in agg.index else None
        api_obs = float(row["api_cost"]) if row is not None else 0.0
        billed_obs = float(row["billed"]) if row is not None and "billed" in row.index else 0.0
        api_proj = api_obs * factor
        label, confidence = _preview_label(seat, api_obs, api_proj, cfg, min_saving)
        rows.append({
            "email": email,
            "current_seat": seat,
            "api_cost_observed_usd": round(api_obs, 2),
            "api_cost_projected_usd": round(api_proj, 2),
            "billed_observed_usd": round(billed_obs, 2),
            "label": label,
            "confidence": confidence,
        })
    users = pd.DataFrame(rows)

    # 部署・職種・備考（任意ファイル members-info.csv）の結合
    _merge_members_info(users, input_dir, cfg, sources)

    orphan = users[users["current_seat"] == "unknown"]["email"].tolist()
    if orphan:
        warnings.append(
            f"members に存在しない利用ユーザ {len(orphan)} 名（シート不明として集計）: {orphan[:5]}"
        )
    warnings.extend(_warn_active_unassigned(users, "api_cost_observed_usd"))

    seats = users["current_seat"].value_counts().to_dict()
    std_price = float(cfg["seats"]["standard"]["price_usd"])
    prem_price = float(cfg["seats"]["premium"]["price_usd"])
    summary = {
        "month": month,
        "days_observed": days_observed,
        "days_in_month": days_in_month,
        "n_members": int(len(users)),
        "n_standard": int(seats.get("standard", 0)),
        "n_premium": int(seats.get("premium", 0)),
        "n_unassigned": int(seats.get("unassigned", 0)),
        "n_unknown": int(seats.get("unknown", 0)),
        "seat_cost_now_usd": round(
            seats.get("standard", 0) * std_price + seats.get("premium", 0) * prem_price, 2),
        "total_api_observed_usd": round(float(users["api_cost_observed_usd"].sum()), 2),
        "total_api_projected_usd": round(float(users["api_cost_projected_usd"].sum()), 2),
        "n_billed": int((users["billed_observed_usd"] > 0).sum()),
        "label_counts": users["label"].value_counts().to_dict(),
        "org_service_cost_usd": org_service_obs,
    }
    return PreviewResult(
        month=month, users=users, summary=summary,
        days_observed=days_observed, days_in_month=days_in_month,
        org=org, warnings=warnings, sources=sources,
    )
