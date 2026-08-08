"""レポート共通の書式・整列ユーティリティ（金額・トークン数・並べ替え・集計行）。"""

from __future__ import annotations

import pandas as pd

from ..analyze import STATUS_CHANGE, AnalysisResult
from ..ingest import parse_affiliations


def _fmt_usd(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${v:,.2f}"


def _fmt_delta(v, compact: bool = False) -> str:
    """符号付きの金額（増減表示用）。compact=True はダッシュボードの短縮表記。"""
    if v is None or pd.isna(v):
        return "—"
    body = _fmt_compact(abs(v)) if compact else f"${abs(v):,.2f}"
    return ("+" if v >= 0 else "-") + body


def _sort_for_display(users: pd.DataFrame, label_col: str, order: list[str],
                      value_col: str) -> pd.DataFrame:
    """ラベル列（status/label）を表示順 order で並べ、同順位内は value_col 降順にする。"""
    df = users.copy()
    df["_order"] = df[label_col].map(
        {v: i for i, v in enumerate(order)}
    ).fillna(len(order))
    return df.sort_values(["_order", value_col], ascending=[True, False])


def _billed_bg(billed: float, max_billed: float) -> str:
    """実課金カラムの金額グラデーション背景色。実課金>0 のとき最大額比で警告色の濃さを
    段階的に付け（最小 0.12〜最大 0.60）、0 のユーザは無着色（空文字列）にする。"""
    if billed > 0 and max_billed > 0:
        alpha = 0.12 + 0.48 * (billed / max_billed)
        return f"rgba(192,57,43,{alpha:.2f})"
    return ""


def _scope_label(result: AnalysisResult) -> str:
    """レポートタイトル用の対象表記。組織名があれば「組織 — 月」。"""
    return f"{result.org} — {result.month}" if result.org else result.month


def _org_products(summary: dict) -> str:
    by_product = summary.get("org_service_by_product") or {}
    if not by_product:
        return ""
    detail = " / ".join(f"{k} {_fmt_usd(v)}" for k, v in
                        sorted(by_product.items(), key=lambda kv: -kv[1]))
    return f"（{detail}）"


def _has_values(users: pd.DataFrame, col: str) -> bool:
    """指定カラムに1つでも非空の値があるか（当該軸の列・サマリの表示可否）。"""
    return col in users.columns and users[col].fillna("").astype(str).str.strip().ne("").any()


def _seat_price(seat: str, summary: dict) -> float:
    """シート料金（unassigned/unknown は判定対象外のため $0 扱い）。summary の価格を使う。"""
    if seat == "standard":
        return float(summary.get("seat_price_standard_usd", 0.0))
    if seat == "premium":
        return float(summary.get("seat_price_premium_usd", 0.0))
    return 0.0


def _group_summary_rows(users: pd.DataFrame, summary: dict, col: str,
                        include_unset: bool = True) -> list[dict]:
    """指定軸（col）でのグループ別サマリの行データ。col 非空のユーザがいない場合は空リスト。

    兼務（複数所属）ユーザは所属数 n で 1/n の重みに按分し、各所属グループへ計上する
    （人数・費用・需要・実課金・変更推奨数・削減見込みすべて同じ重み）。所属が空のユーザは
    「（未設定）」へ重み1で計上する。API換算需要の降順、（未設定）は常に最後。

    include_unset=False のとき「（未設定）」行を除外する（例: チーム別サマリでは、
    チーム未設定のユーザは部署も異なる異質な集合のためまとめても意味がない）。
    この場合、縦合計は全体と一致しなくなる（当該軸に所属を持つユーザのみの集計になる）。
    """
    if not _has_values(users, col):
        return []
    has_loc = "loc_with_cc" in users.columns
    # グループ名 → 集計値の accumulator（初期化順は問わない。最後に並べ替える）
    acc: dict[str, dict] = {}
    for _, r in users.iterrows():
        groups = parse_affiliations(r.get(col)) or ["（未設定）"]
        w = 1.0 / len(groups)
        is_change = r["status"] == STATUS_CHANGE
        seat_price = _seat_price(r["current_seat"], summary)
        api = float(r["api_cost_usd"]) if not pd.isna(r["api_cost_usd"]) else 0.0
        billed = float(r["billed_extra_usd"] or 0.0) if not pd.isna(r["billed_extra_usd"]) else 0.0
        saving = float(r["monthly_saving_usd"] or 0.0) if is_change and not pd.isna(r["monthly_saving_usd"]) else 0.0
        loc = float(r["loc_with_cc"]) if has_loc and not pd.isna(r["loc_with_cc"]) else 0.0
        for grp in groups:
            a = acc.setdefault(grp, {"n": 0.0, "seat_cost": 0.0, "api": 0.0,
                                     "billed": 0.0, "n_change": 0.0, "saving": 0.0, "loc": 0.0})
            a["n"] += w
            a["seat_cost"] += seat_price * w
            a["api"] += api * w
            a["billed"] += billed * w
            a["n_change"] += (1.0 * w) if is_change else 0.0
            a["saving"] += saving * w
            a["loc"] += loc * w
    rows = [{"group": grp, "is_unset": grp == "（未設定）", **a} for grp, a in acc.items()]
    if not include_unset:
        rows = [r for r in rows if not r["is_unset"]]
    rows.sort(key=lambda r: (r["is_unset"], -r["api"]))
    return rows


def _fmt_count(v) -> str:
    """按分後の人数・変更推奨数の表示。整数なら「3」、端数は小数1桁「3.5」（末尾ゼロなし）。"""
    r = round(float(v), 1)
    return str(int(r)) if r == int(r) else f"{r:.1f}"


def _fmt_tokens(v) -> str:
    """トークン数を K/M/B 単位で短く表示（6.7e9 → 6.7B、1.2e6 → 1.2M、340e3 → 340K）。"""
    n = float(v or 0)
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def _detail_rows(users: pd.DataFrame) -> tuple[list[dict], bool]:
    """詳細利用状況テーブルの行データ。input+output トークンの降順で返す。"""
    u = users.copy()
    u["_in"] = u["prompt_tokens"].fillna(0)
    u["_out"] = u["completion_tokens"].fillna(0)
    u["_total"] = u["_in"] + u["_out"]
    u = u.sort_values("_total", ascending=False)
    has_loc = "loc_with_cc" in u.columns
    rows = []
    for _, r in u.iterrows():
        api = r["api_cost_usd"]
        rows.append({
            "email": r["email"],
            "in": int(r["_in"]),
            "out": int(r["_out"]),
            "api": float(api) if not pd.isna(api) else 0.0,  # NaN は 0 扱い
            "models": str(r["model_breakdown"] or ""),
            "products": str(r["product_breakdown"] or ""),
            "loc": int(r["loc_with_cc"]) if has_loc else None,
        })
    return rows, has_loc


def _fmt_delta_int(v: int) -> str:
    """整数の増減表示（+/− 符号 + 桁区切り）。"""
    return ("+" if v >= 0 else "-") + f"{abs(v):,}"


def _fmt_compact(v) -> str:
    """テーブル幅節約のため $100 以上は整数、未満はセント表示。"""
    if v is None or pd.isna(v):
        return "—"
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"
