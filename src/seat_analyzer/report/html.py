"""HTML 出力の組み立て（dashboard.html / preview-dashboard.html）。"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from jinja2 import Environment

from ..analyze import (
    LABEL_EXCLUDED,
    LABEL_HOLD,
    LABEL_IDLE,
    LABEL_PREM_CONSIDER,
    LABEL_PREM_OK,
    LABEL_STD_CAND,
    LABEL_STD_OK,
    SEAT_LABELS,
    STATUS_CHANGE,
    STATUS_EXCLUDED,
    STATUS_KEEP,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    STATUS_WATCH_WAIT,
    AnalysisResult,
    PreviewResult,
)
from .format import (
    _detail_rows,
    _fmt_compact,
    _fmt_count,
    _fmt_delta,
    _fmt_delta_int,
    _fmt_stat_count,
    _fmt_tokens,
    _group_summary_rows,
    _has_values,
    _scope_label,
    _sort_for_display,
)
from .stats import KEY_API_COST, KIND_USD, Distribution, distributions, population
from .text import (
    GROUP_AXES,
    PREVIEW_ORDER,
    STATUS_ORDER,
    _CREDIT_MODE_LABEL,
    _cap_legend_supplement,
    _disabled_cost_note,
    _embed_shared_text,
)

# CSV 由来の値（email 等）が HTML/JS として解釈されないよう autoescape を有効化
_HTML_ENV = Environment(autoescape=True)

# 判定ステータス → .badge クラス（速報側 _PREVIEW_BADGE_CLASS と同じ設計。
# 未知の値は現状維持相当の b-keep に倒す）。
_STATUS_BADGE_CLASS = {
    STATUS_CHANGE: "b-change",
    STATUS_WATCH: "b-watch",
    STATUS_WATCH_WAIT: "b-watch",
    STATUS_UNKNOWN: "b-unknown",
    STATUS_KEEP: "b-keep",
    STATUS_EXCLUDED: "b-keep",
}

# 速報の一次判断ラベル → 既存 .badge クラス。PREVIEW_ORDER に無いラベルは b-keep に倒す。
_PREVIEW_BADGE_CLASS = {
    LABEL_STD_CAND: "b-change", LABEL_PREM_CONSIDER: "b-change",   # アクション候補（緑）
    LABEL_IDLE: "b-watch", LABEL_HOLD: "b-watch",                 # 要観察・保留（橙）
    STATUS_UNKNOWN: "b-unknown",                                  # データ不整合（赤）
    LABEL_PREM_OK: "b-keep", LABEL_STD_OK: "b-keep",             # 現状妥当（グレー）
    LABEL_EXCLUDED: "b-keep",
}


def _billed_bg(billed: float, max_billed: float) -> str:
    """実課金カラムの金額グラデーション背景色。実課金>0 のとき最大額比で警告色の濃さを
    段階的に付け（最小 0.12〜最大 0.60）、0 のユーザは無着色（空文字列）にする。"""
    if billed > 0 and max_billed > 0:
        alpha = 0.12 + 0.48 * (billed / max_billed)
        return f"rgba(192,57,43,{alpha:.2f})"
    return ""


def _apply_billed_bg(rows: list[dict], col: str) -> None:
    """各行に実課金カラムの背景色 billed_bg を付ける（正式・速報で共通）。

    最大額の算出と各行の着色は同じ列を見ていないと濃さの基準がずれるため、
    列名を1箇所でだけ受け取る（正式は billed_extra_usd、速報は billed_observed_usd）。
    """
    max_billed = max((float(r.get(col) or 0.0) for r in rows), default=0.0)
    for r in rows:
        r["billed_bg"] = _billed_bg(float(r.get(col) or 0.0), max_billed)


def _trend_view(trend: dict | None) -> dict | None:
    """dashboard.html 用に整形した「前月からの変化」データ（None なら None）。"""
    if not trend:
        return None

    def _people(items: list[dict]) -> list[dict]:
        return [{"email": x["email"], "amount_fmt": _fmt_compact(x["amount"])} for x in items]

    return {
        "compare_month": trend["compare_month"],
        "gap_skipped": trend["gap_skipped"],
        "started": _people(trend["started"]),
        "stopped": _people(trend["stopped"]),
        "new_billed": _people(trend["new_billed"]),
        "changes": [{"email": c["email"], "prev_fmt": _fmt_compact(c["prev"]),
                     "curr_fmt": _fmt_compact(c["curr"]),
                     "delta_fmt": _fmt_delta(c["delta"], compact=True)}
                    for c in trend["changes"]],
        "series": [{"month": s["month"], "api_fmt": _fmt_compact(s["api"]),
                    "billed_fmt": _fmt_compact(s["billed"]), "active": s["active"]}
                   for s in trend["series"]],
    }


def _snapshot_view(snapshot: dict | None) -> dict | None:
    """dashboard.html / preview-dashboard.html 用に整形したスナップショット差分（None なら None）。"""
    if not snapshot:
        return None
    return {
        "labels": snapshot["labels"],
        "snap_list": " / ".join(f"{s['label']}（{s['days']}日）" for s in snapshot["snaps"]),
        "judged": snapshot["judged"],
        "latest_interval_days": snapshot["latest_interval_days"],
        "rows": [{"email": r["email"], "stall": r["stall"],
                  "cum_fmt": [_fmt_compact(c) for c in r["cum"]],
                  "delta_fmt": _fmt_delta(r["latest_delta"], compact=True)}
                 for r in snapshot["rows"]],
        "stalled_capped": [{"email": x["email"], "cum_fmt": _fmt_compact(x["cum_at_stall"]),
                            "loc_note": x.get("loc_note", "")}
                           for x in snapshot["stalled_capped"]],
        "billed_emerged": [{"email": x["email"], "interval_label": x["interval_label"],
                            "prev_fmt": _fmt_compact(x["prev_cum"]),
                            "curr_fmt": _fmt_compact(x["curr_cum"]),
                            "billed_fmt": _fmt_compact(x["billed"])}
                           for x in snapshot["billed_emerged"]],
    }


def _code_diff_view(code_diff: dict | None) -> dict | None:
    """dashboard.html / preview-dashboard.html 用に整形した code-analytics 差分（None なら None）。"""
    if not code_diff:
        return None
    has_prs = code_diff["has_prs"]
    return {
        "labels": code_diff["labels"],
        "has_prs": has_prs,
        "rows": [{"email": r["email"],
                  "loc_cum_fmt": [f"{c:,}" for c in r["loc_cum"]],
                  "loc_delta_fmt": _fmt_delta_int(r["loc_delta"]),
                  "prs_delta_fmt": (_fmt_delta_int(r["prs_delta"])
                                    if has_prs and r["prs_delta"] is not None else "—")}
                 for r in code_diff["rows"]],
    }


def _member_changes_view(mc: dict | None) -> dict | None:
    """dashboard.html / preview-dashboard.html 用に整形したメンバー変動（None なら None）。"""
    if not mc:
        return None
    credit_changes = mc.get("credit_changes") or []
    return {
        "snap_list": " / ".join(mc["labels"]),
        "empty": mc["empty"],
        "seat_changes": [{"email": c["email"], "interval_label": c["interval_label"],
                          "from_label": SEAT_LABELS.get(c["from"], c["from"]),
                          "to_label": SEAT_LABELS.get(c["to"], c["to"])}
                         for c in mc["seat_changes"]],
        "joined": [{"email": j["email"], "interval_label": j["interval_label"],
                    "seat_label": SEAT_LABELS.get(j["seat"], j["seat"])}
                   for j in mc["joined"]],
        "left": [{"email": x["email"], "interval_label": x["interval_label"],
                  "seat_label": SEAT_LABELS.get(x["seat"], x["seat"])}
                 for x in mc["left"]],
        "credit_changes": [{"email": c["email"], "interval_label": c["interval_label"],
                            "from": c["from"], "to": c["to"]} for c in credit_changes],
    }


def _e_distribution_view(edist: dict | None) -> dict | None:
    """dashboard.html 用に整形した込み枠の実測 E 分布（None なら None）。"""
    if not edist:
        return None
    groups = [{
        "seat_label": SEAT_LABELS.get(g["seat"], g["seat"]),
        "count": g["count"],
        "median_fmt": _fmt_compact(g["median"]),
        "min_fmt": _fmt_compact(g["min"]),
        "max_fmt": _fmt_compact(g["max"]),
        # config allowance(mid) との倍率（standard/premium のみ。それ以外は None）
        "ratio": g.get("ratio"),
        "allowance_mid_fmt": _fmt_compact(g["allowance_mid"]) if g.get("allowance_mid") else "",
        "rows": [{"email": r["email"], "demand_fmt": _fmt_compact(r["demand"]),
                  "billed_fmt": _fmt_compact(r["billed"]), "e_fmt": _fmt_compact(r["e"])}
                 for r in g["rows"]],
    } for g in edist["groups"]]
    return {"groups": groups}


def _grant_candidates_view(candidates: list) -> list[dict]:
    """dashboard.html / preview-dashboard.html 用の付与候補（モードを表示ラベルに）。"""
    return [{"email": c["email"], "mode_label": _CREDIT_MODE_LABEL.get(c["mode"], c["mode"]),
             "added_fmt": _fmt_compact(c["added"])}
            for c in candidates]


def _stats_view(dists: list[Distribution]) -> list[dict]:
    """dashboard.html 用に整形した分布表（対象がなければ空リスト）。"""
    view = []
    for d in dists:
        fmt = _fmt_compact if d.kind == KIND_USD else _fmt_stat_count
        view.append({
            "label": d.label, "n": d.n,
            "mean_fmt": fmt(d.mean), "median_fmt": fmt(d.median), "std_fmt": fmt(d.std),
            "p25_fmt": fmt(d.p25), "p75_fmt": fmt(d.p75), "p90_fmt": fmt(d.p90),
            "max_fmt": fmt(d.maximum),
        })
    return view


def _cost_guide(dists: list[Distribution], max_demand: float) -> dict | None:
    """ユーザ別 API 換算コストの棒に引く中央値・平均のガイド線（引けなければ None）。

    位置は棒の長さと同じ基準（値 / 最大需要）で出す。棒の並びは判定ステータス順で
    金額順ではないため、ガイド線は行の位置ではなく金額軸の上の点を指す。

    max_demand は棒の幅計算に使うスケール（0 除算を避けるため 1.0 に倒したもの）では
    なく、観測された最大需要そのものを受け取る。倒した値で判定すると、全員の需要が
    ゼロの組織（導入直後の月）で $0.00 のガイド線が2本重なって描かれる。線を引くのは
    最大需要が正で、中央値・平均がその範囲（0 以上・最大以下）に収まるときだけにする。
    """
    demand = next((d for d in dists if d.key == KEY_API_COST), None)
    if demand is None or not math.isfinite(max_demand) or max_demand <= 0:
        return None
    if not all(math.isfinite(v) and 0.0 <= v <= max_demand
               for v in (demand.median, demand.mean)):
        return None
    return {
        "median_pct": 100.0 * demand.median / max_demand,
        "mean_pct": 100.0 * demand.mean / max_demand,
        "median_fmt": _fmt_compact(demand.median),
        "mean_fmt": _fmt_compact(demand.mean),
    }


def _cost_ranks(users: pd.DataFrame) -> dict[str, int]:
    """email → API 換算需要の降順順位（同額は同順位）。

    母集団は分布・ガイド線と同じ（`stats.population`＝シート未割当を除く分析対象
    ユーザ）。同じ図の中に母集団を2つ作らないため、未割当のユーザはキーを持たない
    （＝順位を付けない）。棒の並びは判定ステータス順なので行番号は順位にならず、
    値から計算する。
    """
    judged = population(users)
    ranks = judged["api_cost_usd"].fillna(0).rank(method="min", ascending=False)
    return dict(zip(judged["email"], ranks.astype(int), strict=True))


def _credit_reach_view(cr: dict | None) -> dict | None:
    """preview-dashboard.html 用に整形した追加クレジット残額ブロック（None なら None）。"""
    if not cr:
        return None
    rows = []
    for r in cr["rows"]:
        eta = "" if r["reached"] else (f"{r['eta_day']}日頃" if r["eta_day"] is not None else "—")
        rows.append({"email": r["email"], "billed_fmt": _fmt_compact(r["billed"]),
                     "kappa_fmt": _fmt_compact(r["kappa"]),
                     "remaining_fmt": _fmt_compact(r["remaining"]),
                     "reached": r["reached"], "eta": eta})
    return {"rows": rows}


# ダッシュボードの CSS と HTML 断片は templates/ 以下のファイルが実体（prompts/ と同じ流儀）。
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _asset(name: str) -> str:
    """templates/ のアセットを読む。先頭・末尾の改行も出力の一部なので加工しない。"""
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")


# dashboard.html / preview-dashboard.html で共有する CSS（二重メンテを避けるため定数化）。
# 速報専用の追加スタイル（バナー等）は速報テンプレート側で足す。
_DASHBOARD_CSS = _asset("dashboard.css")

# 「前月からの変化」の HTML 断片（正式ダッシュボードのみ）。二重メンテを避けるため
# テンプレート本体には placeholder を置き、from_string 前に差し込む。
_TREND_HTML = _asset("partials/trend.html.j2")

# 「月中の利用推移（スナップショット差分）」の HTML 断片（正式・速報の両ダッシュボードで共有）。
_SNAPSHOT_HTML = _asset("partials/snapshot.html.j2")

# 「月中の Claude Code 活動（code-analytics 差分）」の HTML 断片（正式・速報の両方で共有）。
_CODE_DIFF_HTML = _asset("partials/code-diff.html.j2")

# 「月中のメンバー変動（スナップショット差分）」の HTML 断片（正式・速報の両方で共有）。
_MEMBER_CHANGES_HTML = _asset("partials/member-changes.html.j2")

# 「込み枠の実測（E 分布）」の HTML 断片（正式ダッシュボードのみ）。
_E_DIST_HTML = _asset("partials/e-dist.html.j2")

# 「追加クレジット付与候補」の HTML 断片（正式・速報の両方で共有）。
_GRANT_HTML = _asset("partials/grant.html.j2")

# 「追加クレジット残額」の HTML 断片（速報ダッシュボードのみ）。
_CREDIT_REACH_HTML = _asset("partials/credit-reach.html.j2")

# 「追加クレジット構成」の HTML 断片（サマリカード直下・正式/速報で共有）。
_CREDIT_COMPOSITION_HTML = _asset("partials/credit-composition.html.j2")

# 「組織内の分布（参考値）」の HTML 断片（正式ダッシュボードのみ。速報は product 利用
# 特徴量を持たず、日割り換算した値の分布も意味が変わるため出さない）。
_STATS_HTML = _asset("partials/stats.html.j2")


_HTML_TEMPLATE_SRC = _asset("dashboard.html.j2")

# テンプレートは2段階で組み立てる。断片を差し込んだ _ASSEMBLED と、共有文言
# （<!--text:キー-->）まで解決した _SOURCE を定数として持つのは、未解決マーカーの検査
# （tests/test_hardening.py）が組み立て済みのソースそのものを見られるようにするため。
# 検査が断片を手で列挙しなくて済み、partial を足しても対象へ自動で入る。
_HTML_ASSEMBLED = (
    _HTML_TEMPLATE_SRC.replace("<!--TREND_SECTION-->", _TREND_HTML)
    .replace("<!--SNAPSHOT_SECTION-->", _SNAPSHOT_HTML + _CODE_DIFF_HTML + _MEMBER_CHANGES_HTML)
    .replace("<!--CREDIT_COMPOSITION-->", _CREDIT_COMPOSITION_HTML)
    .replace("<!--CREDIT_SECTION-->", _E_DIST_HTML + _GRANT_HTML)
    .replace("<!--STATS_SECTION-->", _STATS_HTML)
)
_HTML_SOURCE = _embed_shared_text(_HTML_ASSEMBLED)

_HTML_TEMPLATE = _HTML_ENV.from_string(_HTML_SOURCE)


_PREVIEW_HTML_TEMPLATE_SRC = _asset("preview-dashboard.html.j2")

_PREVIEW_HTML_ASSEMBLED = (
    _PREVIEW_HTML_TEMPLATE_SRC.replace(
        "<!--SNAPSHOT_SECTION-->", _SNAPSHOT_HTML + _CODE_DIFF_HTML + _MEMBER_CHANGES_HTML)
    .replace("<!--CREDIT_COMPOSITION-->", _CREDIT_COMPOSITION_HTML)
    .replace("<!--CREDIT_REACH-->", _CREDIT_REACH_HTML)
    .replace("<!--GRANT_SECTION-->", _GRANT_HTML)
)
_PREVIEW_HTML_SOURCE = _embed_shared_text(_PREVIEW_HTML_ASSEMBLED)

_PREVIEW_HTML_TEMPLATE = _HTML_ENV.from_string(_PREVIEW_HTML_SOURCE)


def write_preview_html(result: PreviewResult, path: Path) -> None:
    """速報ダッシュボード（preview-dashboard.html）。preview.md のミラー。"""
    users_sorted = _sort_for_display(
        result.users, "label", PREVIEW_ORDER, "api_cost_projected_usd"
    ).to_dict("records")

    has_dept = _has_values(result.users, "department")
    has_team = _has_values(result.users, "team")

    _apply_billed_bg(users_sorted, "billed_observed_usd")
    for u in users_sorted:
        u["obs_fmt"] = _fmt_compact(u["api_cost_observed_usd"])
        u["proj_fmt"] = _fmt_compact(u["api_cost_projected_usd"])
        u["billed_fmt"] = _fmt_compact(u.get("billed_observed_usd", 0.0))
        u["department"] = str(u.get("department", "") or "") if has_dept else ""
        u["team"] = str(u.get("team", "") or "") if has_team else ""
        u["badge_class"] = _PREVIEW_BADGE_CLASS.get(u["label"], "b-keep")
        # billed_flag は速報固有（正式ダッシュボードには無い上限/従量の注記）
        billed = float(u.get("billed_observed_usd") or 0.0)
        u["billed_flag"] = ("⚠️超過済" if u["current_seat"] == "premium" else "⚠️従量あり") if billed > 0 else ""
    max_proj = max((u["api_cost_projected_usd"] for u in users_sorted), default=0) or 1.0

    # 一次判断の内訳（PREVIEW_ORDER 順、0名は省略）
    counts = result.summary["label_counts"]
    label_counts = [
        {"label": lb, "n": counts[lb], "cls": _PREVIEW_BADGE_CLASS.get(lb, "b-keep")}
        for lb in PREVIEW_ORDER if counts.get(lb)
    ]
    factor = result.days_in_month / result.days_observed

    cap_usd = result.summary["grant_suggested_cap_usd"]
    html = _PREVIEW_HTML_TEMPLATE.render(
        dashboard_css=_DASHBOARD_CSS,
        scope=_scope_label(result),
        s=result.summary,
        snapshot=_snapshot_view(result.snapshot),
        code_diff=_code_diff_view(result.code_diff),
        member_changes=_member_changes_view(result.member_changes),
        credit_reach=_credit_reach_view(result.credit_reach),
        grant_candidates=_grant_candidates_view(result.grant_candidates),
        grant_cap_fmt=_fmt_compact(cap_usd),
        disabled_note=_disabled_cost_note(result.users),
        users_sorted=users_sorted,
        label_counts=label_counts,
        has_dept=has_dept,
        has_team=has_team,
        obs_label=f"観測需要({result.days_observed}日)",
        days_observed=result.days_observed,
        days_in_month=result.days_in_month,
        factor=factor,
        total_obs_fmt=_fmt_compact(result.summary["total_api_observed_usd"]),
        total_proj_fmt=_fmt_compact(result.summary["total_api_projected_usd"]),
        max_proj=max_proj,
        seat_short=SEAT_LABELS,
    )
    path.write_text(html, encoding="utf-8", newline="\n")


def write_html(result: AnalysisResult, path: Path) -> None:
    users_sorted = _sort_for_display(
        result.users, "status", STATUS_ORDER, "api_cost_usd"
    ).to_dict("records")
    _apply_billed_bg(users_sorted, "billed_extra_usd")
    for u in users_sorted:
        u["api_cost_fmt"] = _fmt_compact(u["api_cost_usd"])
        u["billed_fmt"] = _fmt_compact(u.get("billed_extra_usd", 0.0))
        u["std_fmt"] = _fmt_compact(u["cost_if_standard_usd"])
        u["prem_fmt"] = _fmt_compact(u["cost_if_premium_usd"])
        u["saving_fmt"] = _fmt_compact(u.get("monthly_saving_usd"))
        u["badge_class"] = _STATUS_BADGE_CLASS.get(u["status"], "b-keep")
    # 観測された最大需要と、棒の幅の除算に使うスケールを分ける。スケールは 0 除算を
    # 避けるため 1.0 に倒すが、ガイド線の可否は倒す前の値で決める（_cost_guide 参照）
    max_demand = max((u["api_cost_usd"] for u in users_sorted), default=0.0)
    max_cost = max_demand or 1.0
    # 順位は分布・ガイド線と同じ母集団。未割当のユーザには順位を付けない
    ranks = _cost_ranks(result.users)
    for u in users_sorted:
        rank = ranks.get(u["email"])
        u["rank_fmt"] = "—" if rank is None else f"#{rank}"
    dists = distributions(result.users, result.product_usage)
    # 部署別 → チーム別の順で、データがある軸のみサマリ表を出す
    group_summaries = []
    for col, heading, include_unset in GROUP_AXES:
        rows = _group_summary_rows(result.users, result.summary, col, include_unset=include_unset)
        if not rows:
            continue
        for t in rows:
            t["seat_cost_fmt"] = _fmt_compact(t["seat_cost"])
            t["api_fmt"] = _fmt_compact(t["api"])
            t["n_fmt"] = _fmt_count(t["n"])
            t["n_change_fmt"] = _fmt_count(t["n_change"])
            t["loc_fmt"] = f"{round(t['loc']):,}"
        group_summaries.append({
            "heading": heading,
            "col_label": heading.replace("別サマリ", ""),
            "rows": rows,
            "has_loc": "loc_with_cc" in result.users.columns,
        })
    detail_rows, detail_has_loc = _detail_rows(result.users)
    for d in detail_rows:
        d["in_fmt"] = _fmt_tokens(d["in"])
        d["out_fmt"] = _fmt_tokens(d["out"])
        d["api_fmt"] = _fmt_compact(d["api"])
        d["loc_fmt"] = f"{d['loc']:,}" if d["loc"] is not None else ""
    cap_usd = result.summary["grant_suggested_cap_usd"]
    html = _HTML_TEMPLATE.render(
        dashboard_css=_DASHBOARD_CSS,
        scope=_scope_label(result),
        s=result.summary,
        trend=_trend_view(result.trend),
        snapshot=_snapshot_view(result.snapshot),
        code_diff=_code_diff_view(result.code_diff),
        member_changes=_member_changes_view(result.member_changes),
        e_distribution=_e_distribution_view(result.e_distribution),
        grant_candidates=_grant_candidates_view(result.grant_candidates),
        grant_cap_fmt=_fmt_compact(cap_usd),
        cap_supplement=_cap_legend_supplement(result.summary.get("credit_shown", False)),
        disabled_note=_disabled_cost_note(result.users),
        users_sorted=users_sorted,
        group_summaries=group_summaries,
        has_team_summary=any(g["heading"] == "チーム別サマリ" for g in group_summaries),
        detail_rows=detail_rows,
        detail_has_loc=detail_has_loc,
        stats=_stats_view(dists),
        cost_guide=_cost_guide(dists, max_demand),
        max_cost=max_cost,
        seat_short=SEAT_LABELS,
    )
    path.write_text(html, encoding="utf-8", newline="\n")
