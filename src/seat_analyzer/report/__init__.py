"""レポート生成: report.md / dashboard.html / recommendations.csv"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from jinja2 import Environment

from ..analyze import (
    CREDIT_DISABLED,
    SEAT_LABELS,
    STATUS_CHANGE,
    AnalysisResult,
    PreviewResult,
)
from .csv_out import write_csv
from .document import (
    _atomic_write,
    _preserve_discussion,
    discussion_body as discussion_body,
    document_body as document_body,
    write_discussion as write_discussion,
)
from .format import (
    _billed_bg,
    _detail_rows,
    _fmt_compact,
    _fmt_count,
    _fmt_delta,
    _fmt_delta_int,
    _fmt_tokens,
    _fmt_usd,
    _group_summary_rows,
    _has_values,
    _org_products,
    _scope_label,
    _sort_for_display,
)
from .text import (
    GROUP_AXES,
    PREVIEW_ORDER,
    STATUS_ORDER,
    _CREDIT_MODE_LABEL,
    _PREVIEW_BADGE_CLASS,
    _STATUS_BADGE_CLASS,
    _TEXT,
    _embed_shared_text,
)

# CSV 由来の値（email 等）が HTML/JS として解釈されないよう autoescape を有効化
_HTML_ENV = Environment(autoescape=True)


def write_all(result: AnalysisResult, output_dir: str | Path) -> dict[str, Path]:
    out = Path(output_dir) / result.month
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": out / "recommendations.csv",
        "markdown": out / "report.md",
        "html": out / "dashboard.html",
    }
    write_csv(result, paths["csv"])
    write_markdown(result, paths["markdown"])
    write_html(result, paths["html"])
    return paths


def _md_cell(v) -> str:
    """Markdown 表セル用のエスケープ（表崩れ防止）。パイプ・改行が主な対象。"""
    s = "" if v is None else str(v)
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")
    return s


def _user_table_md(users: pd.DataFrame) -> str:
    has_cc = "prs_with_cc" in users.columns
    has_loc = "loc_with_cc" in users.columns
    has_dept = _has_values(users, "department")
    has_team = _has_values(users, "team")
    header = (
        "| ユーザ | 現シート |"
        + (" 部署 |" if has_dept else "")
        + (" チーム |" if has_team else "")
        + " API換算需要 | 実課金(従量) | Standard時 | Premium時 | 推奨 | 削減/月 | 判定 | 確度 |"
        + (" PR(CC) |" if has_cc else "") + (" 行数(CC) |" if has_loc else "")
    )
    sep = "|" + "---|" * (10 + int(has_dept) + int(has_team) + int(has_cc) + int(has_loc))
    lines = [header, sep]
    for _, r in users.iterrows():
        flag = " ⚠️上限?" if r["cap_suspected"] else ""
        cells = [
            r["email"],
            SEAT_LABELS.get(r["current_seat"], r["current_seat"]),
        ]
        if has_dept:
            cells.append(str(r.get("department", "") or ""))
        if has_team:
            cells.append(str(r.get("team", "") or ""))
        cells += [
            _fmt_usd(r["api_cost_usd"]) + flag,
            _fmt_usd(r.get("billed_extra_usd", 0.0)),
            _fmt_usd(r["cost_if_standard_usd"]),
            _fmt_usd(r["cost_if_premium_usd"]),
            SEAT_LABELS.get(r["recommended_seat"], r["recommended_seat"]),
            _fmt_usd(r["monthly_saving_usd"]),
            r["status"],
            r["confidence"],
        ]
        if has_cc:
            cells.append(str(int(r.get("prs_with_cc", 0))))
        if has_loc:
            cells.append(f"{int(r.get('loc_with_cc', 0)):,}")
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def _notes_md(users: pd.DataFrame) -> str:
    """備考（note）が非空のユーザを「- email: note」の箇条書きにする。無ければ空文字列。"""
    if "note" not in users.columns:
        return ""
    noted = users[users["note"].fillna("").astype(str).str.strip().ne("")]
    if noted.empty:
        return ""
    lines = ["### 備考", ""]
    lines += [f"- {_md_cell(r['email'])}: {_md_cell(str(r['note']).strip())}"
              for _, r in noted.iterrows()]
    return "\n".join(lines) + "\n"


def _group_summary_md(users: pd.DataFrame, summary: dict, col: str, heading: str,
                      include_unset: bool = True) -> str:
    """指定軸（col）のグループ別サマリ表。col 非空のユーザがいる場合のみ生成し、無ければ空文字列。

    heading は見出し文言（例: "部署別サマリ"）で、1列目のヘッダにも流用する。
    include_unset=False で「（未設定）」行を除外する。
    """
    rows = _group_summary_rows(users, summary, col, include_unset=include_unset)
    if not rows:
        return ""
    has_loc = "loc_with_cc" in users.columns
    col_label = heading.replace("別サマリ", "")
    header = (f"| {col_label} | 人数 | シート費用/月 | API換算需要/月 | 実課金(従量)/月 |"
              + (" LoC |" if has_loc else "")
              + " 変更推奨 | 削減見込み/月 |")
    lines = [f"## {heading}", "", header, "|" + "---|" * (7 + int(has_loc))]
    for r in rows:
        cells = [_md_cell(r["group"]), f"{_fmt_count(r['n'])} 名",
                 _fmt_usd(r["seat_cost"]), _fmt_usd(r["api"]), _fmt_usd(r["billed"])]
        if has_loc:
            cells.append(f"{round(r['loc']):,}")
        cells += [f"{_fmt_count(r['n_change'])} 名", _fmt_usd(r["saving"])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _detail_table_md(users: pd.DataFrame) -> str:
    """詳細利用状況（input/output トークン・モデル割合・LoC）の Markdown 表。"""
    rows, has_loc = _detail_rows(users)
    header = ("| ユーザ | input | output |" + (" LoC |" if has_loc else "")
              + " API換算需要 | モデル割合（トークン基準） | product構成（利用回数） |")
    sep = "|" + "---|" * (6 + int(has_loc))
    lines = ["## 詳細利用状況", "", header, sep]
    for r in rows:
        cells = [r["email"], _fmt_tokens(r["in"]), _fmt_tokens(r["out"])]
        if has_loc:
            cells.append(f"{r['loc']:,}")
        cells += [_fmt_usd(r["api"]), r["models"], r["products"]]
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    lines.append("")
    lines.append("- input はキャッシュ読取分を含むため、実入力量より大きく見えることがあります")
    lines.append("- product構成 は利用回数（リクエスト数）基準。Cowork/Chat は API コストが小さく出るため回数で示す")
    return "\n".join(lines) + "\n"


def _people_line_md(label: str, items: list[dict]) -> str:
    """「利用開始 N 名: email（金額）, ...」形式の1行（該当なしは「label: なし」）。"""
    if not items:
        return f"- {label}: なし"
    listed = ", ".join(f"{_md_cell(x['email'])}（{_fmt_usd(x['amount'])}）" for x in items)
    return f"- {label} {len(items)} 名: {listed}"


def _trend_md(trend: dict | None) -> str:
    """「## 前月からの変化」セクション（trend が None なら空文字列）。"""
    if not trend:
        return ""
    cmp_line = f"比較対象: {trend['compare_month']}"
    if trend["gap_skipped"]:
        cmp_line += "（直前月が欠測のため直前の存在月と比較）"
    lines = ["## 前月からの変化", "", cmp_line, ""]
    lines.append(_people_line_md("利用開始", trend["started"]))
    lines.append(_people_line_md("利用停止", trend["stopped"]))
    lines.append(_people_line_md("実課金の新規発生", trend["new_billed"]))
    lines += ["", "### 主な増減", ""]
    if trend["changes"]:
        lines += ["| ユーザ | 前月 | 当月 | 増減 |", "|---|---|---|---|"]
        for c in trend["changes"]:
            lines.append(
                f"| {_md_cell(c['email'])} | {_fmt_usd(c['prev'])} | {_fmt_usd(c['curr'])} "
                f"| {_fmt_delta(c['delta'])} |"
            )
    else:
        lines.append("なし")
    lines += ["", "### 月次推移", "", "| 月 | API換算需要 | 実課金 | アクティブユーザ数 |",
              "|---|---|---|---|"]
    for s in trend["series"]:
        lines.append(
            f"| {s['month']} | {_fmt_usd(s['api'])} | {_fmt_usd(s['billed'])} | {s['active']} 名 |"
        )
    return "\n".join(lines)


def _snapshot_md(snapshot: dict | None) -> str:
    """「## 月中の利用推移（スナップショット差分）」セクション（None なら空文字列）。"""
    if not snapshot:
        return ""
    snap_list = " / ".join(f"{s['label']}（{s['days']}日）" for s in snapshot["snaps"])
    lines = [f"## {_TEXT['h_snapshot']}", "", f"スナップショット: {snap_list}", ""]
    if not snapshot["judged"]:
        lines.append(
            f"- 最新区間が {snapshot['latest_interval_days']} 日と短いため停止判定は行っていません"
        )
        lines.append("")
    labels = snapshot["labels"]
    lines.append("| ユーザ | " + " | ".join(labels) + " | 最新区間の増分 | 判定 |")
    lines.append("|" + "---|" * (len(labels) + 3))
    for r in snapshot["rows"]:
        cums = " | ".join(_fmt_usd(c) for c in r["cum"])
        judge = "⚠️停止疑い" if r["stall"] else ""
        lines.append(
            f"| {_md_cell(r['email'])} | {cums} | {_fmt_delta(r['latest_delta'])} | {judge} |"
        )
    lines.append("")
    for x in snapshot["stalled_capped"]:
        note = x.get("loc_note")
        extra = f"。{note}" if note else ""
        lines.append(
            f"- {_md_cell(x['email'])}: 上限停止の可能性。停止時点の累積 "
            f"{_fmt_usd(x['cum_at_stall'])} は実効込み量の実測候補{extra}"
        )
    if snapshot["billed_emerged"]:
        lines += ["", "### この区間で込み量を消化（実課金が発生）", ""]
        for x in snapshot["billed_emerged"]:
            lines.append(
                f"- {_md_cell(x['email'])}: {x['interval_label']} で従量課金 "
                f"{_fmt_usd(x['billed'])} が発生（実効込み量は累積需要 "
                f"{_fmt_usd(x['prev_cum'])}〜{_fmt_usd(x['curr_cum'])} の間）"
            )
    lines += ["", f"- {_TEXT['note_stall_caveat']}"]
    return "\n".join(lines)


def _code_diff_md(code_diff: dict | None) -> str:
    """「## 月中の Claude Code 活動（code-analytics 差分）」セクション（None なら空文字列）。"""
    if not code_diff:
        return ""
    labels = code_diff["labels"]
    has_prs = code_diff["has_prs"]
    header = ("| ユーザ | " + " | ".join(labels)
              + " | LoC 増分（最新区間） |" + (" PR 増分 |" if has_prs else ""))
    sep = "|" + "---|" * (len(labels) + 2 + int(has_prs))
    lines = [f"## {_TEXT['h_code_diff']}", "", header, sep]
    for r in code_diff["rows"]:
        cums = " | ".join(f"{c:,}" for c in r["loc_cum"])
        cells = f"| {_md_cell(r['email'])} | {cums} | {_fmt_delta_int(r['loc_delta'])} |"
        if has_prs:
            prs = r["prs_delta"]
            cells += f" {_fmt_delta_int(prs) if prs is not None else '—'} |"
        lines.append(cells)
    lines += ["", "- LoC 増分が止まったユーザは利用の谷や案件の切れ目の可能性もあるため、"
              "スペンドの停止疑いと合わせて解釈してください"]
    return "\n".join(lines)


def _member_changes_md(mc: dict | None) -> str:
    """「## 月中のメンバー変動（スナップショット差分）」セクション（None なら空文字列）。

    members スナップショット由来のシート変更・追加・削除に加え、members-info スナップショット
    由来の追加クレジット上限 κ の変更も併記する。
    """
    if not mc:
        return ""
    credit_changes = mc.get("credit_changes") or []
    lines = [f"## {_TEXT['h_member_changes']}", "",
             f"スナップショット時点: {' / '.join(mc['labels'])}", ""]
    if mc["empty"]:
        lines.append("- 変動なし")
        return "\n".join(lines)
    for c in mc["seat_changes"]:
        lines.append(
            f"- {_md_cell(c['email'])}: {c['interval_label']} で "
            f"{SEAT_LABELS.get(c['from'], c['from'])} → {SEAT_LABELS.get(c['to'], c['to'])}"
        )
    for j in mc["joined"]:
        lines.append(
            f"- {_md_cell(j['email'])}: {j['interval_label']} で追加"
            f"（{SEAT_LABELS.get(j['seat'], j['seat'])}）"
        )
    for x in mc["left"]:
        lines.append(
            f"- {_md_cell(x['email'])}: {x['interval_label']} で削除"
            f"（{SEAT_LABELS.get(x['seat'], x['seat'])}）"
        )
    for c in credit_changes:
        lines.append(
            f"- {_md_cell(c['email'])}: {c['interval_label']} で 追加クレジット上限 "
            f"{c['from']} → {c['to']}（members-info スナップショット由来）"
        )
    if credit_changes:
        lines.append(f"- {_TEXT['note_credit_change']}")
    return "\n".join(lines)


def _credit_summary_md_row(s: dict) -> str:
    """サマリ表に差し込む追加クレジットの構成行（credit_shown でなければ空文字列）。"""
    if not s.get("credit_shown"):
        return ""
    return (
        f"| 追加クレジット | 有効 {s['credit_enabled_n']} 名"
        f"（上限計 {_fmt_usd(s['credit_cap_total_usd'])}/月・無制限 {s['credit_unlimited_n']} 名）"
        f" / 無効 {s['credit_disabled_n']} 名 / 不明 {s['credit_unknown_n']} 名 |"
    )


def _e_distribution_md(edist: dict | None) -> str:
    """「## 込み枠の実測（E = API換算需要 − 実課金）」セクション（None なら空文字列）。"""
    if not edist:
        return ""
    lines = [f"## {_TEXT['h_e_dist']}", ""]
    for g in edist["groups"]:
        seat_label = SEAT_LABELS.get(g["seat"], g["seat"])
        lines += [f"### {seat_label}（実課金発生 {g['count']} 名）", "",
                  "| ユーザ | 需要 | 実課金 | E |", "|---|---|---|---|"]
        for r in g["rows"]:
            lines.append(
                f"| {_md_cell(r['email'])} | {_fmt_usd(r['demand'])} | "
                f"{_fmt_usd(r['billed'])} | {_fmt_usd(r['e'])} |"
            )
        lines.append(
            f"- 件数 {g['count']} 名 / 中央値 {_fmt_usd(g['median'])} / "
            f"最小 {_fmt_usd(g['min'])} / 最大 {_fmt_usd(g['max'])}"
        )
        if g.get("ratio") is not None:
            lines.append(
                f"- 参考: 実測 E の中央値は config の allowance"
                f"（mid {_fmt_usd(g['allowance_mid'])}）の {g['ratio']:.1f} 倍"
            )
        lines.append("")
    lines += [
        "- E は各ユーザが込み枠から実際に引き出せた量の実測。引き出せる量は利用の形"
        "（毎日安定かバーストか）に依存するため個人差が大きい",
        "- config.yaml > seats.*.allowance_usd のシナリオ見直しの材料になる"
        "（バースト型ユーザでは過小評価になる点に注意）",
    ]
    return "\n".join(lines)


def _grant_candidates_md(candidates: list, cap_usd) -> str:
    """「## 追加クレジット付与候補」セクション（該当なしなら空文字列）。正式・速報で共通。"""
    if not candidates:
        return ""
    lines = [f"## {_TEXT['h_grant']}", ""]
    for c in candidates:
        lines.append(
            f"- {_md_cell(c['email'])}（クレジット{_CREDIT_MODE_LABEL.get(c['mode'], c['mode'])}"
            f"・モデル超過見込み {_fmt_usd(c['added'])}/月）"
        )
    lines += [
        "",
        f"- 昇格の前に、まず上限つき追加クレジット（推奨初期上限 {_fmt_usd(cap_usd)}）を付与し、"
        "1ヶ月の課金実測で判断することを推奨します",
    ]
    return "\n".join(lines)


def _credit_reach_md(cr: dict | None) -> str:
    """速報の「## 追加クレジット残額」セクション（None なら空文字列）。"""
    if not cr:
        return ""
    lines = [f"## {_TEXT['h_credit_reach']}", "",
             "| ユーザ | 実課金(観測) | 上限 κ | 残額 | 到達見込み |", "|---|---|---|---|---|"]
    for r in cr["rows"]:
        if r["reached"]:
            eta = "⚠️上限到達"
        elif r["eta_day"] is not None:
            eta = f"{r['eta_day']}日頃"
        else:
            eta = "—"
        lines.append(
            f"| {_md_cell(r['email'])} | {_fmt_usd(r['billed'])} | {_fmt_usd(r['kappa'])} "
            f"| {_fmt_usd(r['remaining'])} | {eta} |"
        )
    lines += ["", f"- {_TEXT['note_credit_eta']}"]
    return "\n".join(lines)


def _disabled_cost_note(users: pd.DataFrame) -> str:
    """クレジット無効ユーザのコスト列の意味注記（無ければ空文字列）。"""
    if "credits_mode" not in users.columns:
        return ""
    judged = users[users["current_seat"].isin(("standard", "premium"))]
    n_disabled = int((judged["credits_mode"] == CREDIT_DISABLED).sum())
    if n_disabled == 0:
        return ""
    if n_disabled == len(judged):
        return ("追加クレジットが無効のため、「Standard時/Premium時」の枠超過分は実際には"
                "請求されません（絞り負担のドル換算＝需要が上限で抑えられる分の目安）")
    return ("クレジット無効のユーザについては、「Standard時/Premium時」の枠超過分は実際には"
            "請求されず、絞り負担のドル換算（需要が上限で抑えられる分の目安）です")


def _cap_legend_supplement(credit_shown: bool) -> str:
    """⚠️上限? 凡例の補足（credit_shown のときのみ。無ければ空文字列）。"""
    if not credit_shown:
        return ""
    return ("追加クレジットが有効なユーザは実課金がセンサーになるため、"
            "実課金ゼロなら枠内と判断でき ⚠️上限? を付けません")


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


def write_markdown(result: AnalysisResult, path: Path) -> None:
    s = result.summary
    users = _sort_for_display(result.users, "status", STATUS_ORDER, "monthly_saving_usd")

    changes = users[users["status"] == STATUS_CHANGE]
    sensitivity_disagree = users[users["confidence"].isin(["中", "低"])]

    nl = "\n"
    notes_block = _notes_md(users)
    group_md = ""
    has_team_summary = False
    for col, heading, include_unset in GROUP_AXES:
        block = _group_summary_md(users, s, col, heading, include_unset=include_unset)
        if block:
            group_md += nl + block
            if col == "team":
                has_team_summary = True
    team_note = f"\n- {_TEXT['note_team_total']}。" if has_team_summary else ""
    detail_block = _detail_table_md(users)
    warnings_md = nl.join(f"- {w}" for w in result.warnings) if result.warnings else "- なし"

    # サマリ直後に置く追加セクション（前月からの変化 → 月中の利用推移 → Claude Code 活動
    # → メンバー変動 → 込み枠の実測 → 追加クレジット付与候補）。無ければ空文字列で
    # 従来出力と完全一致（後方互換。E 分布は実課金発生ユーザがいるときのみ現れる）
    cap_usd = s.get("grant_suggested_cap_usd", 150)
    extra_sections = ""
    for block in (_trend_md(result.trend), _snapshot_md(result.snapshot),
                  _code_diff_md(result.code_diff), _member_changes_md(result.member_changes),
                  _e_distribution_md(result.e_distribution),
                  _grant_candidates_md(result.grant_candidates, cap_usd)):
        if block:
            extra_sections += nl + block + nl

    # 追加クレジット関連の凡例・注記（credit_shown / 無効ユーザの有無で条件付き）
    credit_row = _credit_summary_md_row(s)
    credit_row = (credit_row + nl) if credit_row else ""
    cap_supplement = _cap_legend_supplement(s.get("credit_shown", False))
    cap_supplement_line = f"{nl}- {cap_supplement}" if cap_supplement else ""
    disabled_note = _disabled_cost_note(users)
    disabled_note_line = f"{nl}- {disabled_note}。" if disabled_note else ""

    md = f"""# Claude Team シート最適化レポート — {_scope_label(result)}

## サマリ

| 指標 | 値 |
|---|---|
| 対象メンバー数 | {s['n_members']} 名（Standard {s['n_standard']} / Premium {s['n_premium']} / 未割当 {s.get('n_unassigned', 0)} / 不明 {s['n_unknown']}） |
| 現在のシート費用 | {_fmt_usd(s['seat_cost_now_usd'])} /月 |
| 全体の API 換算需要（ユーザ帰属分） | {_fmt_usd(s['total_api_cost_usd'])} /月 |
| 実際の従量課金（ユーザ帰属分） | {_fmt_usd(s.get('total_billed_extra_usd', 0.0))} /月 |
| 組織サービス利用（ユーザ非帰属・シート判定対象外） | {_fmt_usd(s.get('org_service_cost_usd', 0.0))} /月{_org_products(s)} |
| **変更推奨** | **{s['n_change_recommended']} 名（削減見込み {_fmt_usd(s['est_monthly_saving_usd'])} /月）** |
| 要観察 | {s['n_watching']} 名 |
| 上限到達疑い（Standard） | {s['n_cap_suspected']} 名 |
{credit_row}| 判定に使用した月 | {', '.join(s['months_used'])}（ヒステリシス {s['hysteresis_months']} ヶ月） |
{extra_sections}
## シート変更推奨

{_user_table_md(changes) if not changes.empty else "該当なし。"}

## 全ユーザ

{_user_table_md(users)}

- **API換算需要**: 当月の全利用量をAPI料金（キャッシュ実効単価込み）に換算した金額。シート込み分を含む「需要」の指標
- **実課金(従量)**: スペンドレポートの net_spend 合計。シート込み利用は $0 で、上限超過の従量課金分のみ計上される
- **Standard時 / Premium時**: そのシートの場合の想定月額。**現シート側はシート料+実課金の観測実績**、変更先側は allowance（込み利用量）モデルによる試算
- **⚠️上限?**: 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ。「実効込み量が推定より大きい」か「上限で停止した」かの要確認{cap_supplement_line}
- **確度**: 込み利用量（allowance）の low/mid/high 3シナリオで推奨が一致するか（高=3/3, 中=2/3, 低=1/3）
- **対象外（シート未割当）**: 意図的にシートを割り当てていないメンバー（別組織でアサイン済み・管理者等）。損益分岐判定は行わない
{(nl + notes_block) if notes_block else ''}{group_md}
{detail_block}
## 感度分析

allowance（シート込み利用量のUSD換算・非公開のため推定）の仮定によって推奨が変わるユーザ:

{_user_table_md(sensitivity_disagree) if not sensitivity_disagree.empty else "なし（全ユーザで3シナリオの推奨が一致）。"}

## 注意事項

- 従量課金（usage credits）が無効の場合、Standardユーザの利用量は上限で頭打ちになるため、
  実際の需要はここに表示された値より大きい可能性があります（センサリング）。
- 「Standard時/Premium時」の従量課金額は allowance の推定値（mid シナリオ）に基づく試算です。{disabled_note_line}
- スペンドデータは前日分まで・過去90日分のみ参照可能です。毎月のエクスポートを忘れずに。{team_note}

## データ検証・警告

{warnings_md}

## 考察

<!-- /seat-analysis または seat-analyzer discuss 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis` または `seat-analyzer discuss` を実行すると考察が追記されます）
"""
    md = _preserve_discussion(md, path)
    # 引き継いだ手書きの考察は他のどこにも無い。切り詰めてから書く write_text では
    # 中断時に本文ごと失うため、考察の差し替えと同じく置換で書く
    _atomic_write(path, md)


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


_HTML_TEMPLATE_SRC = _asset("dashboard.html.j2")

_HTML_TEMPLATE = _HTML_ENV.from_string(
    _embed_shared_text(
        _HTML_TEMPLATE_SRC.replace("<!--TREND_SECTION-->", _TREND_HTML)
        .replace("<!--SNAPSHOT_SECTION-->", _SNAPSHOT_HTML + _CODE_DIFF_HTML + _MEMBER_CHANGES_HTML)
        .replace("<!--CREDIT_COMPOSITION-->", _CREDIT_COMPOSITION_HTML)
        .replace("<!--CREDIT_SECTION-->", _E_DIST_HTML + _GRANT_HTML)
    )
)


_PREVIEW_HTML_TEMPLATE_SRC = _asset("preview-dashboard.html.j2")

_PREVIEW_HTML_TEMPLATE = _HTML_ENV.from_string(
    _embed_shared_text(
        _PREVIEW_HTML_TEMPLATE_SRC.replace(
            "<!--SNAPSHOT_SECTION-->", _SNAPSHOT_HTML + _CODE_DIFF_HTML + _MEMBER_CHANGES_HTML)
        .replace("<!--CREDIT_COMPOSITION-->", _CREDIT_COMPOSITION_HTML)
        .replace("<!--CREDIT_REACH-->", _CREDIT_REACH_HTML)
        .replace("<!--GRANT_SECTION-->", _GRANT_HTML)
    )
)


def write_preview_html(result: PreviewResult, path: Path) -> None:
    """速報ダッシュボード（preview-dashboard.html）。preview.md のミラー。"""
    users_sorted = _sort_for_display(
        result.users, "label", PREVIEW_ORDER, "api_cost_projected_usd"
    ).to_dict("records")

    has_dept = _has_values(result.users, "department")
    has_team = _has_values(result.users, "team")

    # 実課金カラムの金額グラデーション（正式 write_html と同じ最大額比の警告色）
    max_billed = max((float(u.get("billed_observed_usd") or 0.0) for u in users_sorted), default=0.0)
    for u in users_sorted:
        u["obs_fmt"] = _fmt_compact(u["api_cost_observed_usd"])
        u["proj_fmt"] = _fmt_compact(u["api_cost_projected_usd"])
        u["billed_fmt"] = _fmt_compact(u.get("billed_observed_usd", 0.0))
        u["department"] = str(u.get("department", "") or "") if has_dept else ""
        u["team"] = str(u.get("team", "") or "") if has_team else ""
        u["badge_class"] = _PREVIEW_BADGE_CLASS.get(u["label"], "b-keep")
        billed = float(u.get("billed_observed_usd") or 0.0)
        u["billed_bg"] = _billed_bg(billed, max_billed)
        # billed_flag は速報固有（正式ダッシュボードには無い上限/従量の注記）
        u["billed_flag"] = ("⚠️超過済" if u["current_seat"] == "premium" else "⚠️従量あり") if billed > 0 else ""
    max_proj = max((u["api_cost_projected_usd"] for u in users_sorted), default=0) or 1.0

    # 一次判断の内訳（PREVIEW_ORDER 順、0名は省略）
    counts = result.summary["label_counts"]
    label_counts = [
        {"label": lb, "n": counts[lb], "cls": _PREVIEW_BADGE_CLASS.get(lb, "b-keep")}
        for lb in PREVIEW_ORDER if counts.get(lb)
    ]
    factor = result.days_in_month / result.days_observed

    cap_usd = result.summary.get("grant_suggested_cap_usd", 150)
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
    path.write_text(html, encoding="utf-8")


def write_html(result: AnalysisResult, path: Path) -> None:
    users_sorted = _sort_for_display(
        result.users, "status", STATUS_ORDER, "api_cost_usd"
    ).to_dict("records")
    # 実課金カラムの金額グラデーション: 実課金>0 のユーザだけ、最大額に対する比で
    # 警告色（--warn）の濃さを段階的に付ける（0 のユーザは無着色）
    max_billed = max((float(u.get("billed_extra_usd") or 0.0) for u in users_sorted), default=0.0)
    for u in users_sorted:
        u["api_cost_fmt"] = _fmt_compact(u["api_cost_usd"])
        u["billed_fmt"] = _fmt_compact(u.get("billed_extra_usd", 0.0))
        u["std_fmt"] = _fmt_compact(u["cost_if_standard_usd"])
        u["prem_fmt"] = _fmt_compact(u["cost_if_premium_usd"])
        u["saving_fmt"] = _fmt_compact(u.get("monthly_saving_usd"))
        u["billed_bg"] = _billed_bg(float(u.get("billed_extra_usd") or 0.0), max_billed)
        u["badge_class"] = _STATUS_BADGE_CLASS.get(u["status"], "b-keep")
    max_cost = max((u["api_cost_usd"] for u in users_sorted), default=0) or 1.0
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
    cap_usd = result.summary.get("grant_suggested_cap_usd", 150)
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
        max_cost=max_cost,
        seat_short=SEAT_LABELS,
    )
    path.write_text(html, encoding="utf-8")


def write_preview(result: PreviewResult, output_dir: str | Path) -> dict[str, Path]:
    """速報モードの出力（reports/<組織>/<月>/preview.md と preview-dashboard.html）。

    正式レポート（report.md / dashboard.html / recommendations.csv）には触れない。
    戻り値は正式側 write_all と同様の paths dict（keys: "markdown", "html"）。
    """
    out = Path(output_dir) / result.month
    out.mkdir(parents=True, exist_ok=True)
    path = out / "preview.md"
    s = result.summary

    users = _sort_for_display(result.users, "label", PREVIEW_ORDER, "api_cost_projected_usd")

    has_dept = _has_values(users, "department")
    has_team = _has_values(users, "team")
    obs_label = f"観測需要({result.days_observed}日)"
    lines = [
        "| ユーザ | 現シート |"
        + (" 部署 |" if has_dept else "")
        + (" チーム |" if has_team else "")
        + f" {obs_label} | 月末ペース換算 | 実課金(観測) | 一次判断 | 確度 |",
        "|" + "---|" * (7 + int(has_dept) + int(has_team)),
    ]
    for _, r in users.iterrows():
        if r["billed_observed_usd"] > 0:
            billed_flag = " ⚠️超過済" if r["current_seat"] == "premium" else " ⚠️従量あり"
        else:
            billed_flag = ""
        dept_cell = f" {_md_cell(r.get('department', '') or '')} |" if has_dept else ""
        team_cell = f" {_md_cell(r.get('team', '') or '')} |" if has_team else ""
        lines.append(
            f"| {_md_cell(r['email'])} | {_md_cell(SEAT_LABELS.get(r['current_seat'], r['current_seat']))} |"
            f"{dept_cell}"
            f"{team_cell}"
            f" {_fmt_usd(r['api_cost_observed_usd'])} | {_fmt_usd(r['api_cost_projected_usd'])} "
            f"| {_fmt_usd(r['billed_observed_usd'])}{billed_flag} | {_md_cell(r['label'])} | {_md_cell(r['confidence'])} |"
        )
    table = "\n".join(lines)
    nl = "\n"
    notes_block = _notes_md(users)
    warnings_md = nl.join(f"- {w}" for w in result.warnings) if result.warnings else "- なし"

    counts = s["label_counts"]
    count_line = " / ".join(
        f"{lb} {counts[lb]} 名" for lb in PREVIEW_ORDER if counts.get(lb)
    ) or "対象なし"
    factor = result.days_in_month / result.days_observed
    # 一次判断テーブルの後に置くセクション（追加クレジット残額 → 月中推移 → 付与候補）。
    # 無ければ空文字列で従来出力と一致
    cap_usd = s.get("grant_suggested_cap_usd", 150)
    snap_section = ""
    for block in (_credit_reach_md(result.credit_reach),
                  _snapshot_md(result.snapshot), _code_diff_md(result.code_diff),
                  _member_changes_md(result.member_changes),
                  _grant_candidates_md(result.grant_candidates, cap_usd)):
        if block:
            snap_section += nl + nl + block

    credit_row = _credit_summary_md_row(s)
    credit_row = (credit_row + nl) if credit_row else ""
    disabled_note = _disabled_cost_note(users)
    disabled_note_line = f"{nl}- {disabled_note}。" if disabled_note else ""

    md = f"""# Claude Team シート速報プレビュー — {_scope_label(result)}

{result.days_observed}日間の観測データ（{result.month}、暦{result.days_in_month}日、月末ペース換算 ×{factor:.1f}）に基づく一次判断です。
シート変更の確定判断には使わず、ヒアリング・観察対象の絞り込みに使ってください。

## サマリ

| 指標 | 値 |
|---|---|
| 対象メンバー数 | {s['n_members']} 名（Standard {s['n_standard']} / Premium {s['n_premium']} / 未割当 {s.get('n_unassigned', 0)} / 不明 {s['n_unknown']}） |
| 現在のシート費用 | {_fmt_usd(s['seat_cost_now_usd'])} /月 |
| 観測需要 → 月末ペース換算 | {_fmt_usd(s['total_api_observed_usd'])} → {_fmt_usd(s['total_api_projected_usd'])} |
| 一次判断の内訳 | {count_line} |
| 実課金発生 | {s['n_billed']} 名 |
{credit_row}
## 一次判断テーブル

{table}
{(nl + notes_block) if notes_block else ''}
- 一次判断: 月末ペース換算需要を損益分岐モデル（allowance 3シナリオ）にかけた参考判定。
  境界付近（3シナリオ不一致 or 削減見込みがバッファ未満）は「判断保留」に倒しています
- {_TEXT['legend_idle']}
- {_TEXT['legend_over']}
- {_TEXT['legend_billed']}
- {_TEXT['legend_excluded']}{snap_section}

## 注意事項

- 日割り換算（×{factor:.1f}）は利用の偏り（曜日・導入直後の立ち上がり・プロジェクト山谷）を補正しません
- {_TEXT['note_billed_nonlinear']}
- 変更推奨・ヒステリシス判定は行いません。確定判断は全月データ2ヶ月分での正式分析（`analyze`）で行ってください{disabled_note_line}

## データ検証・警告

{warnings_md}

## 考察

<!-- /seat-analysis または seat-analyzer discuss --preview 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis preview <日数>` または `seat-analyzer discuss --preview` を実行すると考察が追記されます）
"""
    md = _preserve_discussion(md, path)
    # 引き継いだ手書きの考察は他のどこにも無い。切り詰めてから書く write_text では
    # 中断時に本文ごと失うため、考察の差し替えと同じく置換で書く
    _atomic_write(path, md)

    html_path = out / "preview-dashboard.html"
    write_preview_html(result, html_path)
    return {"markdown": path, "html": html_path}


def write_org_summary(results: list[AnalysisResult], output_dir: str | Path) -> Path:
    """複数組織を一括分析したときの横断サマリ（reports/summary/YYYY-MM.md）。"""
    month = results[0].month
    out = Path(output_dir) / "summary"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{month}.md"

    lines = [
        f"# Claude Team シート最適化 組織横断サマリ — {month}",
        "",
        "| 組織 | メンバー | シート費用/月 | API換算需要/月 | 実課金(従量)/月 | 組織サービス/月 | 変更推奨 | 削減見込み/月 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    keys = (
        "n_members", "seat_cost_now_usd", "total_api_cost_usd",
        "total_billed_extra_usd", "org_service_cost_usd",
        "n_change_recommended", "est_monthly_saving_usd",
    )
    totals = dict.fromkeys(keys, 0.0)
    for r in results:
        s = r.summary
        for k in keys:
            totals[k] += float(s.get(k, 0) or 0)
        lines.append(
            f"| [{r.org}](../{r.org}/{month}/report.md) | {s['n_members']} 名 "
            f"| {_fmt_usd(s['seat_cost_now_usd'])} | {_fmt_usd(s['total_api_cost_usd'])} "
            f"| {_fmt_usd(s.get('total_billed_extra_usd', 0.0))} | {_fmt_usd(s.get('org_service_cost_usd', 0.0))} "
            f"| {s['n_change_recommended']} 名 | {_fmt_usd(s['est_monthly_saving_usd'])} |"
        )
    lines += [
        f"| **合計** | **{int(totals['n_members'])} 名** "
        f"| **{_fmt_usd(totals['seat_cost_now_usd'])}** | **{_fmt_usd(totals['total_api_cost_usd'])}** "
        f"| **{_fmt_usd(totals['total_billed_extra_usd'])}** | **{_fmt_usd(totals['org_service_cost_usd'])}** "
        f"| **{int(totals['n_change_recommended'])} 名** | **{_fmt_usd(totals['est_monthly_saving_usd'])}** |",
        "",
        "各組織の詳細は `reports/<組織>/" + month + "/report.md` を参照。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
