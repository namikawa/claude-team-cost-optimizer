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
from ..product_usage import ProductUsage
from .format import (
    _detail_rows,
    _fmt_compact,
    _fmt_count,
    _fmt_delta,
    _fmt_delta_int,
    _fmt_setting_usd,
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

# .badge クラス → 「判定の内訳」の横棒の塗り。バッジの文字色と同じ色を棒に使うので、
# 対応表を1つ挟んで両者がずれないようにする。
_BADGE_FILL_CLASS = {
    "b-change": "f-change",
    "b-watch": "f-watch",
    "b-unknown": "f-unknown",
    "b-keep": "f-keep",
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


# 「前月からの変化」の種別（trend のキー, 見出し, バッジのクラス）。並びは表示順。
_TREND_KINDS = (
    ("started", "利用開始", "started"),
    ("stopped", "利用停止", "stopped"),
    ("new_billed", "実課金の新規発生", "billed"),
)


def _trend_view(trend: dict | None) -> dict | None:
    """dashboard.html 用に整形した「前月からの変化」データ（None なら None）。"""
    if not trend:
        return None

    def _people(items: list[dict]) -> list[dict]:
        return [{"email": x["email"], "amount_fmt": _fmt_compact(x["amount"])} for x in items]

    # 月次推移の棒は需要の最大を 100% とし、実課金も同じ物差しで並べる（別々に
    # 正規化すると、実課金の小さい月の棒が需要と同じ長さに見える）。需要が正の月が
    # 1つも無いときは物差しが作れないので、そのまま渡して _bar_pct 側で棒を落とす
    scale = max((float(s["api"]) for s in trend["series"]), default=0.0)
    drawable = _bar_scale_ok(scale)
    return {
        "compare_month": trend["compare_month"],
        "gap_skipped": trend["gap_skipped"],
        "groups": [{"kind": label, "cls": cls, "people": _people(trend[key])}
                   for key, label, cls in _TREND_KINDS],
        "changes": [{"email": c["email"], "prev_fmt": _fmt_compact(c["prev"]),
                     "curr_fmt": _fmt_compact(c["curr"]),
                     "delta_fmt": _fmt_delta(c["delta"], compact=True)}
                    for c in trend["changes"]],
        # billed_pos は「棒に最小幅を効かせてよいか」の印。幅 0% の要素に最小幅が
        # 効くと棒が立って見えるので、実課金が正で、かつ棒を描くと決めた月にだけ付ける
        "series": [{"month": s["month"], "api_fmt": _fmt_compact(s["api"]),
                    "billed_fmt": _fmt_compact(s["billed"]), "active": s["active"],
                    "api_pct": _bar_pct(s["api"], scale),
                    "billed_pct": _bar_pct(s["billed"], scale),
                    "billed_pos": drawable and float(s["billed"]) > 0.0}
                   for s in trend["series"]],
    }


def _delta_class(delta: float, latest_cum: float) -> str:
    """スナップショット増分の強調度（動いていない値は沈め、大きい増分だけ立てる）。"""
    if delta <= 1.0:
        return "dim"
    if latest_cum > 0 and delta > latest_cum * 0.15:
        return "up"
    return "sub"


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
                  "delta_fmt": _fmt_delta(r["latest_delta"], compact=True),
                  "delta_cls": _delta_class(
                      float(r["latest_delta"]), float(r["cum"][-1]) if r["cum"] else 0.0)}
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


def _gain_class(delta) -> str:
    """増分セルの強調度（増えた分だけ立て、0 と不明は沈める）。"""
    return "gain" if delta is not None and delta > 0 else "gain zero"


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
                  "loc_delta_cls": _gain_class(r["loc_delta"]),
                  "prs_delta_fmt": (_fmt_delta_int(r["prs_delta"])
                                    if has_prs and r["prs_delta"] is not None else "—"),
                  "prs_delta_cls": _gain_class(r["prs_delta"] if has_prs else None)}
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


def _grant_candidates_view(candidates: list) -> list[dict]:
    """dashboard.html / preview-dashboard.html 用の付与候補（モードを表示ラベルに）。"""
    return [{"email": c["email"], "mode_label": _CREDIT_MODE_LABEL.get(c["mode"], c["mode"]),
             "added_fmt": _fmt_compact(c["added"])}
            for c in candidates]


# 追加クレジットの状態の帯（サマリのキー, 見出し, 帯のクラス）。並びは表示順。
_CREDIT_SEGMENTS = (
    ("credit_enabled_n", "有効", "c-enabled"),
    ("credit_disabled_n", "無効", "c-disabled"),
    ("credit_unknown_n", "不明", "c-unknown"),
)


def _credit_bars(summary: dict) -> list[dict]:
    """「追加クレジットの状態」の人数比の帯（該当なしなら空リスト）。

    同じカードに出している人数をそのまま横幅の比にするだけで、集計は増やさない。
    0 名の区画は帯に出さない（最小幅で細い線として残ると、いない区分がいるように見える）。
    """
    if not summary.get("credit_shown"):
        return []
    bars = []
    for key, label, cls in _CREDIT_SEGMENTS:
        n = int(summary.get(key, 0) or 0)
        if n > 0:
            bars.append({"cls": cls, "n": n, "label": f"{label} {n} 名"})
    return bars


def _judge_counts(users_sorted: list[dict]) -> list[dict]:
    """「判定の内訳」の横棒（推奨一覧に現れた判定ごとの件数）。

    数えるのは推奨一覧に並んでいる行そのもので、母集団も並び順も同じ表に閉じている。
    行に1つも無い判定は棒を作らない（0 件の棒は読み手に何も渡さない）。
    """
    total = len(users_sorted)
    if total == 0:
        return []
    # 表示順は推奨一覧と同じ。表に無い判定は末尾へ、行に現れた順で足す
    seen = list(dict.fromkeys(u["status"] for u in users_sorted))
    order = [s for s in STATUS_ORDER if s in seen] + [s for s in seen if s not in STATUS_ORDER]
    rows = []
    for status in order:
        n = sum(1 for u in users_sorted if u["status"] == status)
        badge = _STATUS_BADGE_CLASS.get(status, "b-keep")
        rows.append({"label": status, "n": n, "pct": 100.0 * n / total,
                     "fill_class": _BADGE_FILL_CLASS.get(badge, "f-keep")})
    return rows


def _org_tab_count(group_summaries: list[dict]) -> int:
    """組織タブの件数バッジ（実際に描画されている軸の行数）。

    軸は GROUP_AXES の2つ（部署・チーム）だが、片方しか値を持たない組織がある。
    group_summaries には行のある軸だけが GROUP_AXES の順で入るので、その先頭
    （部署があれば部署、無ければチーム）を数える。軸を持たない組織は 0 を返し、
    テンプレート側でバッジそのものが出ない。
    """
    return len(group_summaries[0]["rows"]) if group_summaries else 0


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

    位置は棒の長さと同じ基準（値 / max_demand）で出す。棒の並びは判定ステータス順で
    金額順ではないため、ガイド線は行の位置ではなく金額軸の上の点を指す。

    線を引くかどうかは分布そのもの（母集団の最大が正か）で決める。max_demand は棒と
    座標を揃えるためのスケールで、母集団の外にいる未割当ユーザの需要も、0 除算を
    避けるために 1.0 へ倒した値も入りうるため、有無の判定には使えない
    （どちらで判定しても、母集団の需要が全員ゼロの組織で $0.00 の線が2本重なる）。
    座標が棒の中に収まること（0 以上・スケール以下）は別に確かめる。
    """
    demand = next((d for d in dists if d.key == KEY_API_COST), None)
    if demand is None:
        return None
    if not all(math.isfinite(v)
               for v in (max_demand, demand.maximum, demand.median, demand.mean)):
        return None
    if max_demand <= 0 or demand.maximum <= 0:
        return None
    if not all(0.0 <= v <= max_demand for v in (demand.median, demand.mean)):
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


def _fmt_share_pct(value) -> str:
    """構成比の整数パーセント表示（確定できない値は —）。"""
    if value is None or pd.isna(value):
        return "—"
    return f"{round(100.0 * float(value))}%"


def _bar_scale_ok(scale: float) -> bool:
    """そのスケールで棒を描けるか（正の数か）。

    需要は cost_basis によっては負になりうる（返金等）ため、最大が 0 以下の月が
    ありうる。負のスケールで割ると符号が打ち消され、値が小さいほど長い棒になる
    （全ての値が負の月は全部が最長になる）ので、そのときは棒そのものを描かない。

    幅を出す側（_bar_pct）と、棒の見せ方を決める側（最小幅の印）で同じ条件を
    見るための1箇所。別々に書くと片方だけが「描かない」と判断し、幅 0% の棒に
    最小幅が効いて、描かないはずの棒が立つ。
    """
    return not math.isnan(scale) and scale > 0.0


def _bar_pct(value, scale: float) -> float:
    """棒の幅（スケールに対する %）。スケールの外へ出る値は端へ寄せる。

    負の幅や 100% 超の幅をそのまま書くと CSS 側で宣言ごと捨てられ、棒の長さが
    他の行と比較できない値（幅の指定なし＝中身なりの長さ）になるため、描ける
    範囲へ丸める。描けるスケールが無いときは 0%（_bar_scale_ok 参照）。
    """
    if not _bar_scale_ok(scale):
        return 0.0
    return min(100.0, max(0.0, 100.0 * float(value) / scale))


def _product_summary_line(totals: pd.Series, codes: pd.Series) -> str | None:
    """「Code と他プロダクトの需要」の組織サマリ1行（出せなければ None）。

    合計は Code と全需要の両方が確定した行だけを足す。片方だけ確定した行を混ぜると、
    分子と分母で対象ユーザが違う比率になる。確定した行が1人も無ければ行ごと出さない。
    """
    both = totals.notna() & codes.notna()
    n_confirmed = int(both.sum())
    if n_confirmed == 0:
        return None
    code_sum = float(codes[both].sum())
    total_sum = float(totals[both].sum())
    line = f"Code需要 {_fmt_compact(code_sum)} / 全需要 {_fmt_compact(total_sum)}"
    # 分母が 0（または負）だと比率が意味を持たないため、そのときは比率を出さない
    if total_sum > 0:
        line += f"（{round(100.0 * code_sum / total_sum)}%）"
    line += f"・対象 {len(totals)}名"
    if n_confirmed < len(totals):
        line += f"・金額は内訳の確定した {n_confirmed}名の合計"
    return line


def _product_view(usage: ProductUsage | None, threshold_usd: float) -> dict | None:
    """dashboard.html 用に整形した「Code と他プロダクトの需要」（出さないなら None）。

    データ源は product 利用特徴量だけで金額を計算し直さない。行は features の行
    （対象月のスペンドに明細のあるユーザ）で、利用ゼロのメンバーと組織サービス利用は
    含まれない。確定できなかった値は — にして 0 で埋めない（usage-summary.csv と同じ
    規則。0 で埋めると「観測した結果が 0 だった」ことと区別できなくなる）。

    Code の需要が1人も確定しない組織ではセクションごと出さない（棒も比率も全部 —
    になり、読み手が得るものが無い）。product 列が無いことの説明は CLI の
    CAPACITY_SIGNAL_UNAVAILABLE 警告が担う。
    """
    if usage is None:
        return None
    features = usage.features
    needed = {"total_demand_usd", "code_demand_usd", "code_demand_share"}
    if features.empty or not needed <= set(features.columns):
        return None
    totals, codes = features["total_demand_usd"], features["code_demand_usd"]
    if not bool(codes.notna().any()):
        return None

    # 棒のスケールは確定した需要の最大値。1人も確定しない場合と 0 以下の場合は 1.0 に
    # 倒す（0 除算を避けるための値で、棒の長さの基準としての意味は持たない）
    max_total = totals.max()
    scale = float(max_total) if pd.notna(max_total) and max_total > 0 else 1.0

    ordered = features.assign(
        _email=features.index,
        _other=totals - codes,      # どちらかが欠損なら欠損が伝播する
    ).sort_values(["total_demand_usd", "_email"],
                  ascending=[False, True], na_position="last")

    rows = []
    for email, r in ordered.iterrows():
        total, share, breadth = (
            r["total_demand_usd"], r["code_demand_share"], r["product_breadth"])
        total_fmt, share_fmt = _fmt_compact(total), _fmt_share_pct(share)
        if pd.isna(total):
            bar_kind = "none"                       # 長さが決まらない: 棒を描かない
        elif pd.isna(r["code_demand_usd"]):
            bar_kind = "unknown"                    # 長さは決まるが内訳が不明: 斜線
        else:
            bar_kind = "split"
        rows.append({
            "email": str(email),
            "total_fmt": total_fmt,
            "code_fmt": _fmt_compact(r["code_demand_usd"]),
            "share_fmt": share_fmt,
            "other_fmt": _fmt_compact(r["_other"]),
            # 個数は欠損を持てる型（Int64）なので、0 と「分からない」を取り違えない
            "breadth_fmt": "—" if pd.isna(breadth) else str(int(breadth)),
            # 真だけを印にする（偽と「分からない」はどちらも無印）
            "flag": bool(pd.notna(r["supplementary_high"]) and r["supplementary_high"]),
            "val_fmt": "—" if pd.isna(total) else f"{total_fmt} (Code {share_fmt})",
            "bar_kind": bar_kind,
            "bar_pct": 0.0 if pd.isna(total) else _bar_pct(total, scale),
            # 棒の中での色の切り替え位置（比なので全幅が 1.0）。比が定義できない
            # （需要 0）ときは全部 Code 以外に倒すが、その棒の長さは 0 で画面に出ない
            "split_pct": 0.0 if pd.isna(share) else _bar_pct(share, 1.0),
        })
    return {
        "summary_line": _product_summary_line(totals, codes),
        "rows": rows,
        "threshold_fmt": _fmt_setting_usd(threshold_usd),
    }


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

# 同じく共有する JS（タブとテーマの切替だけ）。CSV 由来の値はここへ渡さない。
_DASHBOARD_JS = _asset("dashboard.js")

# 「前月からの変化」の HTML 断片（正式ダッシュボードのみ）。二重メンテを避けるため
# テンプレート本体には placeholder を置き、from_string 前に差し込む。
_TREND_HTML = _asset("partials/trend.html.j2")

# 「月次推移」「主な増減」の HTML 断片（正式ダッシュボードのみ）。どちらも前月からの
# 変化と同じ trend から作るが、概要タブの中では別のカードとして離れた場所に置く。
_MONTHLY_HTML = _asset("partials/monthly.html.j2")
_DELTAS_HTML = _asset("partials/deltas.html.j2")

# 「月中の利用推移（スナップショット差分）」の HTML 断片（正式・速報の両ダッシュボードで共有）。
_SNAPSHOT_HTML = _asset("partials/snapshot.html.j2")

# 「月中の Claude Code 活動（code-analytics 差分）」の HTML 断片（正式・速報の両方で共有）。
_CODE_DIFF_HTML = _asset("partials/code-diff.html.j2")

# 「月中のメンバー変動（スナップショット差分）」の HTML 断片（正式・速報の両方で共有）。
_MEMBER_CHANGES_HTML = _asset("partials/member-changes.html.j2")

# 「追加クレジット付与候補」の HTML 断片（正式・速報の両方で共有）。
_GRANT_HTML = _asset("partials/grant.html.j2")

# 「追加クレジット残額」の HTML 断片（速報ダッシュボードのみ）。
_CREDIT_REACH_HTML = _asset("partials/credit-reach.html.j2")

# 「追加クレジット構成」の HTML 断片（サマリカード直下・正式/速報で共有）。
_CREDIT_COMPOSITION_HTML = _asset("partials/credit-composition.html.j2")

# 「Codeと他プロダクトの需要（API換算）」の HTML 断片（正式ダッシュボードのみ。
# 速報は product 利用特徴量を持たない）。
_PRODUCT_HTML = _asset("partials/product.html.j2")

# 「組織内の分布（参考値）」の HTML 断片（正式ダッシュボードのみ。速報は product 利用
# 特徴量を持たず、日割り換算した値の分布も意味が変わるため出さない）。
_STATS_HTML = _asset("partials/stats.html.j2")


_HTML_TEMPLATE_SRC = _asset("dashboard.html.j2")
_PREVIEW_HTML_TEMPLATE_SRC = _asset("preview-dashboard.html.j2")

# テンプレート本体の placeholder → そこへ順に差し込む断片。組み立てはこの表だけから
# 行う（.replace() を手で並べない）。並べる形だと、断片を足すときに「差し込みの追加」と
# 「検査への追加」が別々の作業になり、片方を忘れても静かに通る。
_DASHBOARD_SECTIONS = {
    "<!--CREDIT_COMPOSITION-->": (_CREDIT_COMPOSITION_HTML,),
    "<!--MONTHLY_SECTION-->": (_MONTHLY_HTML,),
    "<!--TREND_SECTION-->": (_TREND_HTML,),
    "<!--MEMBER_CHANGES-->": (_MEMBER_CHANGES_HTML,),
    "<!--DELTAS_SECTION-->": (_DELTAS_HTML,),
    "<!--SNAPSHOT_SECTION-->": (_SNAPSHOT_HTML, _CODE_DIFF_HTML),
    "<!--CREDIT_SECTION-->": (_GRANT_HTML,),
    "<!--PRODUCT_SECTION-->": (_PRODUCT_HTML,),
    "<!--STATS_SECTION-->": (_STATS_HTML,),
}

_PREVIEW_SECTIONS = {
    "<!--CREDIT_COMPOSITION-->": (_CREDIT_COMPOSITION_HTML,),
    "<!--CREDIT_REACH-->": (_CREDIT_REACH_HTML,),
    "<!--SNAPSHOT_SECTION-->": (_SNAPSHOT_HTML, _CODE_DIFF_HTML, _MEMBER_CHANGES_HTML),
    "<!--GRANT_SECTION-->": (_GRANT_HTML,),
}


def _assemble(src: str, sections: dict[str, tuple[str, ...]]) -> str:
    """テンプレート本体へ断片を差し込む（共有文言 <!--text:キー--> はまだ解決しない）。

    差し込み先がちょうど1つあることを確かめてから置換する。placeholder は HTML
    コメントなので、綴り違いや本体からの消失で差し込みが空振りしても画面には何も
    現れず、そのセクションが黙って消える。
    """
    for placeholder, parts in sections.items():
        found = src.count(placeholder)
        if found != 1:
            raise ValueError(
                f"テンプレートの差し込み先 {placeholder} が {found} 個あります"
                "（ちょうど1個であること）"
            )
        src = src.replace(placeholder, "".join(parts))
    return src


# 組み立ては2段階。断片を差し込んだ _ASSEMBLED と、共有文言まで解決した _SOURCE を
# 定数として持つのは、テンプレートの検査（tests/test_hardening.py）が組み立て済みの
# ソースそのものを見られるようにするため。
_HTML_ASSEMBLED = _assemble(_HTML_TEMPLATE_SRC, _DASHBOARD_SECTIONS)
_HTML_SOURCE = _embed_shared_text(_HTML_ASSEMBLED)

_HTML_TEMPLATE = _HTML_ENV.from_string(_HTML_SOURCE)

_PREVIEW_HTML_ASSEMBLED = _assemble(_PREVIEW_HTML_TEMPLATE_SRC, _PREVIEW_SECTIONS)
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
        dashboard_js=_DASHBOARD_JS,
        scope=_scope_label(result),
        org=result.org,
        month=result.month,
        s=result.summary,
        credit_bars=_credit_bars(result.summary),
        snapshot=_snapshot_view(result.snapshot),
        code_diff=_code_diff_view(result.code_diff),
        member_changes=_member_changes_view(result.member_changes),
        credit_reach=_credit_reach_view(result.credit_reach),
        grant_candidates=_grant_candidates_view(result.grant_candidates),
        grant_cap_fmt=_fmt_setting_usd(cap_usd),
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
        saving = u.get("monthly_saving_usd")
        u["saving_positive"] = bool(pd.notna(saving) and float(saving or 0.0) > 0)
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
            # （未設定）を落とした軸は縦合計が全体と一致しないので、その断りを表に添える
            "include_unset": include_unset,
        })
    detail_rows, detail_has_loc = _detail_rows(result.users)
    for d in detail_rows:
        d["in_fmt"] = _fmt_tokens(d["in"])
        d["out_fmt"] = _fmt_tokens(d["out"])
        d["api_fmt"] = _fmt_compact(d["api"])
        d["loc_fmt"] = f"{d['loc']:,}" if d["loc"] is not None else ""
    cap_usd = result.summary["grant_suggested_cap_usd"]
    # タブの件数バッジは、そのタブの中身の件数をそのまま出す（集計はしない）。概要は
    # 先頭の KPI と同じメンバー数、推奨アクションとメンバー別はそれぞれの表の行数、
    # 組織は描画された軸の行数。0 のときはテンプレート側でバッジを出さない
    tabs = [
        {"key": "overview", "label": "概要", "count": result.summary["n_members"]},
        {"key": "actions", "label": "推奨アクション", "count": len(users_sorted)},
        {"key": "members", "label": "メンバー別", "count": len(detail_rows)},
        {"key": "org", "label": "組織", "count": _org_tab_count(group_summaries)},
    ]
    html = _HTML_TEMPLATE.render(
        dashboard_css=_DASHBOARD_CSS,
        dashboard_js=_DASHBOARD_JS,
        scope=_scope_label(result),
        org=result.org,
        month=result.month,
        tabs=tabs,
        s=result.summary,
        # ヘッダーの削減見込みは SAVING の KPI カードと同じ値・同じ書式で出す
        saving_fmt=f"${result.summary['est_monthly_saving_usd']:.0f}",
        credit_bars=_credit_bars(result.summary),
        judge_counts=_judge_counts(users_sorted),
        trend=_trend_view(result.trend),
        snapshot=_snapshot_view(result.snapshot),
        code_diff=_code_diff_view(result.code_diff),
        member_changes=_member_changes_view(result.member_changes),
        grant_candidates=_grant_candidates_view(result.grant_candidates),
        grant_cap_fmt=_fmt_setting_usd(cap_usd),
        cap_supplement=_cap_legend_supplement(result.summary.get("credit_shown", False)),
        disabled_note=_disabled_cost_note(result.users),
        users_sorted=users_sorted,
        group_summaries=group_summaries,
        has_team_summary=any(g["heading"] == "チーム別サマリ" for g in group_summaries),
        detail_rows=detail_rows,
        detail_has_loc=detail_has_loc,
        product=_product_view(result.product_usage,
                              result.summary["supplementary_high_usd"]),
        stats=_stats_view(dists),
        cost_guide=_cost_guide(dists, max_demand),
        max_cost=max_cost,
        seat_short=SEAT_LABELS,
    )
    path.write_text(html, encoding="utf-8", newline="\n")
