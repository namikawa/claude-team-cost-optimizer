"""追加クレジット（usage credits）: モード導出・上限到達・E 分布・付与候補。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# クレジットモード（追加クレジット=usage credits の有効/無効）。値は report の表示と結合。
CREDIT_ENABLED = "enabled"    # κ>0 or 無制限、または実課金の観測から自動確定
CREDIT_DISABLED = "disabled"  # κ==0
CREDIT_UNKNOWN = "unknown"    # 未設定かつ実課金の観測なし


def credits_mode(credit_limit_usd: float, billed_ever: bool) -> str:
    """ユーザの追加クレジット（usage credits）モードを導出する。

    κ>0 or inf → enabled / κ==0 → disabled /
    κ=NaN（未設定）→ 当月までに実課金が観測されていれば enabled と自動確定（無効なら
    課金は構造的に発生し得ないため論理的に確実）、それ以外は unknown。
    """
    kappa = credit_limit_usd
    if kappa is not None and not pd.isna(kappa):
        return CREDIT_ENABLED if kappa > 0 else CREDIT_DISABLED
    return CREDIT_ENABLED if billed_ever else CREDIT_UNKNOWN


def _usage_credits_cfg(cfg: dict) -> dict:
    """追加クレジットの表示閾値（config.yaml > usage_credits。無くてもデフォルトで動く）。"""
    u = cfg.get("usage_credits") or {}
    return {
        "cap_tolerance_usd": float(u.get("cap_tolerance_usd", 5.0)),
        "grant_suggested_cap_usd": float(u.get("grant_suggested_cap_usd", 150)),
    }


def _credit_display(kappa) -> str:
    """追加クレジット上限 κ の表示文字列（未設定/無効/無制限/$金額）。"""
    if kappa is None or pd.isna(kappa):
        return "未設定"
    if math.isinf(kappa):
        return "無制限"
    if kappa == 0:
        return "無効"
    return f"${kappa:,.0f}" if kappa >= 100 else f"${kappa:,.2f}"


def _kappa_equal(a, b) -> bool:
    """κ の等価判定（NaN 同士は等しい・inf 同士は等しいとみなす）。"""
    a_nan = a is None or pd.isna(a)
    b_nan = b is None or pd.isna(b)
    if a_nan or b_nan:
        return a_nan and b_nan
    return a == b


def _drop_unused_credit_columns(users: pd.DataFrame, summary: dict) -> pd.DataFrame:
    """追加クレジット情報が無い入力では credit 列を落とす（出力を従来と一致させる後方互換）。"""
    if summary.get("credit_shown"):
        return users
    return users.drop(columns=["credit_limit_usd", "credits_mode"], errors="ignore")


def _attach_credits_mode(users: pd.DataFrame, billed_ever: set[str]) -> None:
    """credit_limit_usd と billed_ever から credits_mode 列を付け、enabled の cap_suspected を抑制する。

    enabled のユーザは実課金がセンサーとして働く（billed=0 なら枠内と判断できる）ため
    上限到達フラグ（cap_suspected）を立てない。disabled / unknown は現行どおり。
    """
    users["credits_mode"] = [
        credits_mode(k, e in billed_ever)
        for k, e in zip(users["credit_limit_usd"], users["email"], strict=False)
    ]
    if "cap_suspected" in users.columns:
        users.loc[users["credits_mode"] == CREDIT_ENABLED, "cap_suspected"] = False


def _credit_shown(users: pd.DataFrame) -> bool:
    """members-info に credit_limit_usd の非空値が1つでもあるか（構成行・付与候補の表示可否）。"""
    return "credit_limit_usd" in users.columns and bool(users["credit_limit_usd"].notna().any())


def _credit_summary(users: pd.DataFrame) -> dict:
    """追加クレジットの構成（サマリ行のデータ）。非空の credit 値が無ければ credit_shown=False。"""
    if not _credit_shown(users):
        return {"credit_shown": False}
    modes = users["credits_mode"]
    caps = users.loc[modes == CREDIT_ENABLED, "credit_limit_usd"]
    n_unlimited = int(caps.map(lambda v: pd.notna(v) and math.isinf(v)).sum())
    cap_total = float(caps.map(lambda v: v if (pd.notna(v) and not math.isinf(v)) else 0.0).sum())
    return {
        "credit_shown": True,
        "credit_enabled_n": int((modes == CREDIT_ENABLED).sum()),
        "credit_disabled_n": int((modes == CREDIT_DISABLED).sum()),
        "credit_unknown_n": int((modes == CREDIT_UNKNOWN).sum()),
        "credit_unlimited_n": n_unlimited,
        "credit_cap_total_usd": round(cap_total, 2),
    }


def _compute_e_distribution(users: pd.DataFrame) -> dict | None:
    """実課金発生ユーザの E（=API換算需要 − 実課金）をシート種別ごとに集計する。

    E は需要のうち課金されなかった額＝その月にシートが吸収した量の実測で、そのユーザの
    容量の下限を与える（上限は分からない）。billers（実課金>0）がいなければ None。
    cost_basis=computed 前提（net_spend 基準では需要=課金となり E が意味を持たない）。
    """
    billers = users[users["billed_extra_usd"].fillna(0.0) > 0.0].copy()
    if billers.empty:
        return None
    billers["_e"] = billers["api_cost_usd"].fillna(0.0) - billers["billed_extra_usd"].fillna(0.0)
    groups = []
    for seat in ("premium", "standard", "unknown", "unassigned"):
        g = billers[billers["current_seat"] == seat]
        if g.empty:
            continue
        # email をタイブレークに置き、E が同点のユーザでも行順が一意に決まるようにする
        # （単一列の sort_values は安定ソートではなく、同点行の並びが実行環境で変わりうる）
        rows = [
            {"email": r["email"], "demand": round(float(r["api_cost_usd"]), 2),
             "billed": round(float(r["billed_extra_usd"]), 2), "e": round(float(r["_e"]), 2)}
            for _, r in g.sort_values(["_e", "email"], ascending=[False, True]).iterrows()
        ]
        es = g["_e"]
        groups.append({
            "seat": seat, "rows": rows, "count": len(g),
            "median": round(float(es.median()), 2),
            "min": round(float(es.min()), 2),
            "max": round(float(es.max()), 2),
        })
    return {"groups": groups}


def _grant_candidates(users: pd.DataFrame, upgrade_mask: pd.Series, cfg: dict,
                      demand_col: str = "api_cost_usd") -> list[dict]:
    """付与候補: credits_mode が disabled/unknown かつ 昇格方向のユーザ。

    upgrade_mask は呼び出し側で作る昇格方向の真偽列（正式=拘束前の純モデル判定で premium、
    速報=Premium検討/判断保留）。各候補に mid シナリオのモデル超過見込み
    max(0, 需要 − Standard allowance(mid)) を added として持たせ、その降順で返す
    （超過見込みが大きいほど付与の優先度が高い）。
    """
    if not _credit_shown(users):
        return []
    allowance_mid = float(cfg["seats"]["standard"]["allowance_usd"]["mid"])
    mask = users["credits_mode"].isin([CREDIT_DISABLED, CREDIT_UNKNOWN]) \
        & (users["current_seat"] == "standard") & upgrade_mask
    cands = [
        {"email": r["email"], "mode": r["credits_mode"],
         "added": round(max(0.0, float(r[demand_col]) - allowance_mid), 2)}
        for _, r in users[mask].iterrows()
    ]
    cands.sort(key=lambda c: -c["added"])
    return cands


def _credit_integrity_warnings(users: pd.DataFrame, cfg: dict, billed_col: str) -> list[str]:
    """整合性検証の警告: (a) 実課金が上限 κ 超過（上限値が古い）、(b) κ==0 なのに課金発生。"""
    if "credit_limit_usd" not in users.columns:
        return []
    tol = _usage_credits_cfg(cfg)["cap_tolerance_usd"]
    over_cap, disabled_billed = [], []
    for _, r in users.iterrows():
        kappa = r["credit_limit_usd"]
        billed = float(r[billed_col] or 0.0)
        if pd.isna(kappa):
            continue
        if kappa == 0.0:
            if billed > 0.0:
                disabled_billed.append(r["email"])
        elif not math.isinf(kappa) and billed > kappa + tol:
            over_cap.append(r["email"])
    warnings = []
    if over_cap:
        warnings.append(
            f"追加クレジット: 実課金が上限 κ を超過 {len(over_cap)} 名: {over_cap[:10]}"
            "（members-info の上限値が実態より古い可能性）"
        )
    if disabled_billed:
        warnings.append(
            f"追加クレジット: 無効（κ=0）と記載されているが課金が発生 {len(disabled_billed)} 名: "
            f"{disabled_billed[:10]}（記入ミス or 月中の設定変更）"
        )
    return warnings


def _credit_reached_emails(users: pd.DataFrame, cfg: dict, billed_col: str) -> list[str]:
    """上限到達（billed > 0 かつ billed ≥ κ − tolerance）の enabled・有限 κ ユーザの一覧。"""
    if "credit_limit_usd" not in users.columns:
        return []
    tol = _usage_credits_cfg(cfg)["cap_tolerance_usd"]
    reached = []
    for _, r in users.iterrows():
        kappa = r["credit_limit_usd"]
        if pd.isna(kappa) or math.isinf(kappa) or kappa <= 0.0:
            continue
        billed = float(r[billed_col] or 0.0)
        # 到達には課金の発生が論理的に必要。κ ≤ tolerance の設定でも実課金ゼロを到達と誤判定しない
        # （_credit_reach_preview と同じガード）
        if billed > 0.0 and billed >= kappa - tol:
            reached.append(r["email"])
    return reached


@dataclass(frozen=True)
class _CreditReachContext:
    """速報の追加クレジット残額行で共有する観測期間と課金増分。"""

    tolerance: float
    days_observed: int
    days_in_month: int
    interval_days: int
    billed_delta: dict[str, float]


def _credit_eta_day(
    email: str,
    kappa: float,
    billed: float,
    remaining: float,
    context: _CreditReachContext,
) -> int | None:
    """最新区間、または月初からの平均ペースで月内の上限到達日を見積もる。"""
    if email in context.billed_delta and context.interval_days > 0:
        # 直近区間の課金ペース: 区間レートが 0 のユーザは予測せず None（直近は課金なし）
        rate = context.billed_delta[email] / context.interval_days
        estimated = context.days_observed + remaining / rate if rate > 0 else None
    elif context.days_observed > 0:
        # 月初からの平均ペースへフォールバック（billed > 0 は呼び出し側で保証済み）
        estimated = kappa * context.days_observed / billed
    else:
        estimated = None
    # 「月内に収まる」を満たすことを条件にする。否定形（月末超えなら None）で書くと、
    # NaN は比較が常に偽になるため月内扱いで math.ceil へ落ちて例外になる
    if estimated is None or not estimated <= context.days_in_month:
        return None
    return math.ceil(estimated)


def _credit_reach_row(
    row: pd.Series,
    context: _CreditReachContext,
) -> dict | None:
    """速報の追加クレジット残額行を、対象外なら None を返して構築する。"""
    if row.get("credits_mode") != CREDIT_ENABLED:
        return None
    kappa = row["credit_limit_usd"]
    if pd.isna(kappa) or math.isinf(kappa) or kappa <= 0.0:
        return None
    billed = float(row["billed_observed_usd"] or 0.0)
    if billed <= 0.0:
        return None

    kappa = float(kappa)
    remaining = kappa - billed
    reached = billed >= kappa - context.tolerance
    eta_day = None
    if not reached:
        eta_day = _credit_eta_day(
            row["email"],
            kappa,
            billed,
            remaining,
            context,
        )
    return {
        "email": row["email"],
        "billed": round(billed, 2),
        "kappa": round(kappa, 2),
        "remaining": round(remaining, 2),
        "reached": reached,
        "eta_day": eta_day,
    }


def _credit_reach_preview(users: pd.DataFrame, days_observed: int, days_in_month: int,
                          cfg: dict, snapshot: dict | None = None) -> dict | None:
    """速報の追加クレジット残額ブロック（enabled・有限 κ・実課金>0 のユーザ）。

    到達済み（billed ≥ κ − tolerance）は「⚠️上限到達」。未到達の到達予測は次のレートで外挿:
      - スナップショットが2つ以上あるユーザ: 最新区間の課金増分 ÷ 区間日数を現在レートとし、
        観測末日 + 残額/現在レート（区間レートが 0 なら予測せず None）
      - 無ければ月初からの平均ペース（billed / days_observed）で外挿
    月内に到達しない見込みなら None。課金は非線形（込み枠消化後にのみ発生）なので予測は目安。
    対象ユーザがいなければ None。
    """
    tol = _usage_credits_cfg(cfg)["cap_tolerance_usd"]
    interval_days = snapshot.get("latest_interval_days", 0) if snapshot else 0
    # email → 最新区間の課金増分（スナップショットがあるユーザのみ）
    billed_delta = {r["email"]: float(r.get("billed_delta", 0.0))
                    for r in (snapshot.get("rows", []) if snapshot else [])}
    context = _CreditReachContext(
        tolerance=tol,
        days_observed=days_observed,
        days_in_month=days_in_month,
        interval_days=interval_days,
        billed_delta=billed_delta,
    )
    rows = [
        reach
        for _, row in users.iterrows()
        if (reach := _credit_reach_row(row, context)) is not None
    ]
    if not rows:
        return None
    rows.sort(key=lambda x: x["remaining"])
    return {"rows": rows}
