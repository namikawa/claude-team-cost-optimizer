"""Markdown 出力の組み立て（report.md / preview.md / 組織横断サマリ）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..analyze import (
    SEAT_LABELS,
    STATUS_CHANGE,
    STATUS_FIXED_SEAT,
    AnalysisResult,
    OrgAnalysisResult,
    PreviewResult,
    single_org_result,
    summarize_org,
)
from . import spaces
from .document import _atomic_write, _preserve_discussion
from .format import (
    _account_rows,
    _detail_rows,
    _fmt_count,
    _fmt_delta,
    _fmt_delta_int,
    _fmt_stat_count,
    _fmt_tokens,
    _fmt_usd,
    _group_summary_rows,
    _has_values,
    _int_cell,
    _is_missing,
    _labeled,
    _md_cell,
    _scope_label,
    _sole_result,
    _sort_for_display,
    _text_value,
)
from .naming import PREVIEW, REPORT
from .stats import KIND_USD, Distribution
from .text import (
    _CREDIT_MODE_LABEL,
    _TEXT,
    PREVIEW_ORDER,
    STATUS_ORDER,
    _cap_legend_supplement,
    _disabled_cost_note,
)


def _user_table_md(users: pd.DataFrame, space: bool = False) -> str:
    """ユーザ（アカウント）単位の判定表。

    space=True かつ workspace_label 列がある（複数 workspace を連結した）表では、
    ユーザの次に「スペース」列を置く。片方の workspace にしか無い列のセルは「—」。
    """
    has_space = space and "workspace_label" in users.columns
    has_cc = "prs_with_cc" in users.columns
    has_loc = "loc_with_cc" in users.columns
    has_dept = _has_values(users, "department")
    has_team = _has_values(users, "team")
    header = (
        "| ユーザ |"
        + (" スペース |" if has_space else "")
        + " 現シート |"
        + (" 部署 |" if has_dept else "")
        + (" チーム |" if has_team else "")
        + " API換算需要 | 実課金(従量) | Standard時 | Premium時 | 推奨 | 削減/月 | 判定 | 確度 |"
        + (" PR(CC) |" if has_cc else "") + (" 行数(CC) |" if has_loc else "")
    )
    sep = "|" + "---|" * (10 + int(has_space) + int(has_dept) + int(has_team)
                          + int(has_cc) + int(has_loc))
    lines = [header, sep]
    for _, r in users.iterrows():
        cap = r["cap_suspected"]
        flag = " ⚠️上限?" if not _is_missing(cap) and bool(cap) else ""
        cells = [r["email"]]
        if has_space:
            cells.append(_text_value(r["workspace_label"]))
        cells.append(SEAT_LABELS.get(r["current_seat"], r["current_seat"]))
        if has_dept:
            cells.append(_text_value(r.get("department", "")))
        if has_team:
            cells.append(_text_value(r.get("team", "")))
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
            cells.append(_int_cell(r.get("prs_with_cc", 0)))
        if has_loc:
            cells.append(_int_cell(r.get("loc_with_cc", 0), thousands=True))
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def _user_legend_md(summary: dict, extra: tuple[str, ...] = ()) -> str:
    """ユーザ表の列の読み方（凡例）。

    report.md のシート変更推奨（表が空でないとき）と details.md の全ユーザ表で共有する。
    どちらも同じ列構成（_user_table_md）なので、読み方の説明も1つにする。
    extra は末尾に足す行（複数 workspace の組織だけが持つ説明。既定では何も足さない）。
    """
    lines = [
        "- **API換算需要**: 当月の全利用量をAPI料金（キャッシュ実効単価込み）に換算した金額。シート込み分を含む「需要」の指標",
        "- **実課金(従量)**: スペンドレポートの net_spend 合計。シート込み利用は $0 で、上限超過の従量課金分のみ計上される",
        "- **Standard時 / Premium時**: そのシートの場合の想定月額。**現シート側はシート料+実課金の観測実績**、変更先側は allowance（込み利用量）モデルによる試算",
        "- **⚠️上限?**: 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ。「実効込み量が推定より大きい」か「上限で停止した」かの要確認",
    ]
    cap_supplement = _cap_legend_supplement(summary.get("credit_shown", False))
    if cap_supplement:
        lines.append(f"- {cap_supplement}")
    lines += [
        "- **確度**: 込み利用量（allowance）の low/mid/high 3シナリオで推奨が一致するか（高=3/3, 中=2/3, 低=1/3）",
        "- **対象外（シート未割当）**: 意図的にシートを割り当てていないメンバー（別組織でアサイン済み・管理者等）。損益分岐判定は行わない",
    ]
    lines += list(extra)
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


def _sensitivity_md(users: pd.DataFrame, label: str | None = None) -> str:
    """「## 感度分析」セクション（allowance の仮定で推奨が変わるユーザ）。"""
    disagree = users[users["confidence"].isin(["中", "低"])]
    table = (_user_table_md(disagree) if not disagree.empty
             else "なし（全ユーザで3シナリオの推奨が一致）。")
    return (
        f"## {_labeled('感度分析', label)}\n\n"
        "allowance（シート込み利用量のUSD換算・非公開のため推定）の仮定によって推奨が変わるユーザ:"
        f"\n\n{table}\n"
    )


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


def _detail_table_md(users: pd.DataFrame, space: bool = False) -> str:
    """詳細利用状況（input/output トークン・モデル割合・LoC）の Markdown 表。

    space=True かつ workspace_label 列がある表では、ユーザの次に「スペース」列を置く。
    """
    rows, has_loc = _detail_rows(users)
    has_space = space and "workspace_label" in users.columns
    header = ("| ユーザ |" + (" スペース |" if has_space else "")
              + " input | output |" + (" LoC |" if has_loc else "")
              + " API換算需要 | モデル割合（トークン基準） | product構成（利用回数） |")
    sep = "|" + "---|" * (6 + int(has_space) + int(has_loc))
    lines = ["## 詳細利用状況", "", header, sep]
    for r in rows:
        cells = [r["email"]]
        if has_space:
            cells.append(r["space"] or "")
        cells += [_fmt_tokens(r["in"]), _fmt_tokens(r["out"])]
        if has_loc:
            cells.append(f"{r['loc']:,}" if r["loc"] is not None else "—")
        cells += [_fmt_usd(r["api"]), r["models"], r["products"]]
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    lines.append("")
    lines.append("- input はキャッシュ読取分を含むため、実入力量より大きく見えることがあります")
    lines.append("- product構成 は利用回数（リクエスト数）基準。Cowork/Chat は API コストが小さく出るため回数で示す")
    return "\n".join(lines) + "\n"


def _stats_md(dists: list[Distribution], label: str | None = None) -> str:
    """「## 組織内の分布（参考値）」セクション（対象がなければ空文字列）。"""
    if not dists:
        return ""
    lines = [f"## {_labeled(_TEXT['h_stats'], label)}", "",
             "| 指標 | n | 平均 | 中央値 | 標準偏差 | p25 | p75 | p90 | 最大 |",
             "|" + "---|" * 9]
    for d in dists:
        fmt = _fmt_usd if d.kind == KIND_USD else _fmt_stat_count
        lines.append("| " + " | ".join([
            d.label, f"{d.n} 名", fmt(d.mean), fmt(d.median), fmt(d.std),
            fmt(d.p25), fmt(d.p75), fmt(d.p90), fmt(d.maximum),
        ]) + " |")
    lines += [
        "",
        f"- {_TEXT['note_stats_population']}",
        f"- {_TEXT['note_stats_skew']}",
        f"- {_TEXT['note_stats_censored']}",
        f"- {_TEXT['note_stats_loc']}",
        f"- {_TEXT['note_stats_scope']}",
    ]
    return "\n".join(lines) + "\n"


def _people_line_md(label: str, items: list[dict]) -> str:
    """「利用開始 N 名: email（金額）, ...」形式の1行（該当なしは「label: なし」）。"""
    if not items:
        return f"- {label}: なし"
    listed = ", ".join(f"{_md_cell(x['email'])}（{_fmt_usd(x['amount'])}）" for x in items)
    return f"- {label} {len(items)} 名: {listed}"


def _trend_md(trend: dict | None, label: str | None = None) -> str:
    """「## 前月からの変化」セクション（trend が None なら空文字列）。

    label は複数 workspace の組織で見出しに添える workspace の表示名（以下の
    section 関数も同じ。None なら従来と同じ見出し）。
    """
    if not trend:
        return ""
    cmp_line = f"比較対象: {trend['compare_month']}"
    if trend["gap_skipped"]:
        cmp_line += "（直前月が欠測のため直前の存在月と比較）"
    lines = [f"## {_labeled('前月からの変化', label)}", "", cmp_line, ""]
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


def _snapshot_md(snapshot: dict | None, label: str | None = None) -> str:
    """「## 月中の利用推移（スナップショット差分）」セクション（None なら空文字列）。"""
    if not snapshot:
        return ""
    snap_list = " / ".join(f"{s['label']}（{s['days']}日）" for s in snapshot["snaps"])
    lines = [f"## {_labeled(_TEXT['h_snapshot'], label)}", "",
             f"スナップショット: {snap_list}", ""]
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


def _code_diff_md(code_diff: dict | None, label: str | None = None) -> str:
    """「## 月中の Claude Code 活動（code-analytics 差分）」セクション（None なら空文字列）。"""
    if not code_diff:
        return ""
    labels = code_diff["labels"]
    has_prs = code_diff["has_prs"]
    header = ("| ユーザ | " + " | ".join(labels)
              + " | LoC 増分（最新区間） |" + (" PR 増分 |" if has_prs else ""))
    sep = "|" + "---|" * (len(labels) + 2 + int(has_prs))
    lines = [f"## {_labeled(_TEXT['h_code_diff'], label)}", "", header, sep]
    for r in code_diff["rows"]:
        cums = " | ".join(f"{c:,}" for c in r["loc_cum"])
        cells = f"| {_md_cell(r['email'])} | {cums} | {_fmt_delta_int(r['loc_delta'])} |"
        if has_prs:
            prs = r["prs_delta"]
            cells += f" {_fmt_delta_int(prs) if prs is not None else '—'} |"
        lines.append(cells)
    lines += ["", ("- LoC 増分が止まったユーザは利用の谷や案件の切れ目の可能性もあるため、"
                   "スペンドの停止疑いと合わせて解釈してください")]
    return "\n".join(lines)


def _member_changes_md(mc: dict | None, label: str | None = None) -> str:
    """「## 月中のメンバー変動（スナップショット差分）」セクション（None なら空文字列）。

    members スナップショット由来のシート変更・追加・削除に加え、members-info スナップショット
    由来の追加クレジット上限 κ の変更も併記する。
    """
    if not mc:
        return ""
    credit_changes = mc.get("credit_changes") or []
    lines = [f"## {_labeled(_TEXT['h_member_changes'], label)}", "",
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


def _credit_summary_md_row(s: dict, label: str | None = None) -> str:
    """サマリ表に差し込む追加クレジットの構成行（credit_shown でなければ空文字列）。"""
    if not s.get("credit_shown"):
        return ""
    return (
        f"| {_labeled('追加クレジット', label)} | 有効 {s['credit_enabled_n']} 名"
        f"（上限計 {_fmt_usd(s['credit_cap_total_usd'])}/月・無制限 {s['credit_unlimited_n']} 名）"
        f" / 無効 {s['credit_disabled_n']} 名 / 不明 {s['credit_unknown_n']} 名 |"
    )


def _e_distribution_md(edist: dict | None, label: str | None = None) -> str:
    """「## シートが吸収した量の実測（E = API換算需要 − 実課金）」セクション（None なら空文字列）。"""
    if not edist:
        return ""
    lines = [f"## {_labeled(_TEXT['h_e_dist'], label)}", ""]
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
        lines.append("")
    lines += [
        "- E は需要のうち実際には課金されなかった額。その月にシートが吸収した量の実測にあたる",
        ("- E はそのユーザの容量の下限を示す。上限は分からないため、E が小さいことは"
         "容量に余裕がないことを意味しない"),
    ]
    return "\n".join(lines)


def _grant_candidates_md(candidates: list, cap_usd, label: str | None = None) -> str:
    """「## 追加クレジット付与候補」セクション（該当なしなら空文字列）。正式・速報で共通。"""
    if not candidates:
        return ""
    lines = [f"## {_labeled(_TEXT['h_grant'], label)}", ""]
    for c in candidates:
        lines.append(
            f"- {_md_cell(c['email'])}（クレジット{_CREDIT_MODE_LABEL.get(c['mode'], c['mode'])}"
            f"・モデル超過見込み {_fmt_usd(c['added'])}/月）"
        )
    lines += [
        "",
        (f"- 昇格の前に、まず上限つき追加クレジット（推奨初期上限 {_fmt_usd(cap_usd)}）を付与し、"
         "1ヶ月の課金実測で判断することを推奨します"),
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


def _org_products(summary: dict) -> str:
    by_product = summary.get("org_service_by_product") or {}
    if not by_product:
        return ""
    detail = " / ".join(f"{k} {_fmt_usd(v)}" for k, v in
                        sorted(by_product.items(), key=lambda kv: -kv[1]))
    return f"（{detail}）"


def write_markdown(result: AnalysisResult | OrgAnalysisResult, path: Path) -> None:
    """report.md（サマリ・推奨・考察を中心にした本文）。

    ユーザ単位の表・月中の推移・分布は details.md が受け持つ（report/details.py）。
    チーム別サマリの縦合計の断りも、説明対象の表と一緒に details.md 側にある。

    OrgAnalysisResult は、複数 workspace の組織なら workspace ごとの節と人の層の節を
    持つ形で書き、そうでなければ唯一の workspace の結果で従来どおりに書く。
    """
    if isinstance(result, OrgAnalysisResult):
        if result.has_multiple_workspaces:
            _write_markdown_multi(result, path)
            return
        result = _sole_result(result)
    s = result.summary
    users = _sort_for_display(result.users, "status", STATUS_ORDER, "monthly_saving_usd")

    changes = users[users["status"] == STATUS_CHANGE]

    nl = "\n"
    warnings_md = nl.join(f"- {w}" for w in result.warnings) if result.warnings else "- なし"

    # サマリ直後に置く追加セクション（前月からの変化 → 追加クレジット付与候補）。
    # 無ければ空文字列（後方互換）
    cap_usd = s["grant_suggested_cap_usd"]
    extra_sections = ""
    for block in (_trend_md(result.trend),
                  _grant_candidates_md(result.grant_candidates, cap_usd)):
        if block:
            extra_sections += nl + block + nl

    # 列の読み方は表があるときだけ添える（「該当なし。」の下に凡例だけが残らないように）
    changes_block = _user_table_md(changes) if not changes.empty else "該当なし。"
    if not changes.empty:
        changes_block += nl + nl + _user_legend_md(s)

    # 追加クレジット関連の凡例・注記（credit_shown / 無効ユーザの有無で条件付き）
    credit_row = _credit_summary_md_row(s)
    credit_row = (credit_row + nl) if credit_row else ""
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

{changes_block}

## 注意事項

- 従量課金（usage credits）が無効の場合、Standardユーザの利用量は上限で頭打ちになるため、
  実際の需要はここに表示された値より大きい可能性があります（センサリング）。
- 「Standard時/Premium時」の従量課金額は allowance の推定値（mid シナリオ）に基づく試算です。{disabled_note_line}
- スペンドデータは前日分まで・過去90日分のみ参照可能です。毎月のエクスポートを忘れずに。

## データ検証・警告

{warnings_md}

## 考察

<!-- /seat-analysis または seat-analyzer discuss 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis` または `seat-analyzer discuss` を実行すると考察が追記されます）
"""
    md = _preserve_discussion(
        md, path, fallback=REPORT.legacy_sibling(path, result.month, result.org))
    # 引き継いだ手書きの考察は他のどこにも無い。切り詰めてから書く write_text では
    # 中断時に本文ごと失うため、考察の差し替えと同じく置換で書く
    _atomic_write(path, md)


def _warning_lines(org: OrgAnalysisResult) -> list[str]:
    """組織単位の警告 → workspace ごとの警告（表示名を前置）の順に並べた警告。"""
    lines = list(org.warnings)
    for name, result in org.workspaces.items():
        label = org.contexts[name].label
        lines += [f"[{label}] {w}" for w in result.warnings]
    return lines


def _write_markdown_multi(org: OrgAnalysisResult, path: Path) -> None:
    """複数 workspace の組織の report.md（設計書 §26.7）。

    合計はアカウント単位の数（変更推奨・要観察など）と人数を分けて書く。workspace
    ごとの節は「見出し（表示名）」の同じ階層で主→副の順に並べる。シート変更推奨は
    全アカウントを連結した判定の表（主の行の需要は合算値）にスペース列を足す。
    """
    nl = "\n"
    summary = summarize_org(org)
    total = summary["total"]
    results = org.workspaces
    labels = {name: org.contexts[name].label for name in results}

    judged = _account_rows(org, {name: r.users for name, r in results.items()})
    users = _sort_for_display(judged, "status", STATUS_ORDER, "monthly_saving_usd")
    changes = users[users["status"] == STATUS_CHANGE]

    credit_rows = "".join(
        row + nl for name, r in results.items()
        if (row := _credit_summary_md_row(r.summary, labels[name]))
    )
    hysteresis = next(iter(results.values())).summary["hysteresis_months"]
    months = " / ".join(
        f"{labels[name]}: {', '.join(r.summary['months_used'])}" for name, r in results.items()
    )
    org_service = {"org_service_by_product": total["org_service_by_product"]}

    extra_sections = ""
    for name, r in results.items():
        block = _trend_md(r.trend, labels[name])
        if block:
            extra_sections += nl + block + nl
    for name, r in results.items():
        if org.contexts[name].fixed_seat:
            continue  # 固定シートの workspace は損益分岐判定をしないので付与候補も無い
        block = _grant_candidates_md(
            r.grant_candidates, r.summary["grant_suggested_cap_usd"], labels[name])
        if block:
            extra_sections += nl + block + nl

    credit_shown = any(r.summary.get("credit_shown", False) for r in results.values())
    changes_block = (_user_table_md(changes, space=True) if not changes.empty
                     else "該当なし。")
    if not changes.empty:
        changes_block += nl + nl + _user_legend_md(
            {"credit_shown": credit_shown}, spaces.account_legend_lines(org))

    # 固定シートのアカウントは「Standard時/Premium時」を試算しないので、無効の注記の
    # 対象に数えない（数えると、試算の無い行のための注記が出る）
    disabled_note = _disabled_cost_note(users[users["status"] != STATUS_FIXED_SEAT])
    note_lines = ""
    if disabled_note:
        note_lines += f"{nl}- {disabled_note}。"
    for line in spaces.notes_lines(org):
        note_lines += f"{nl}- {line}。"

    warnings = _warning_lines(org)
    warnings_md = nl.join(f"- {w}" for w in warnings) if warnings else "- なし"

    md = f"""# Claude Team シート最適化レポート — {_scope_label(org)}

## サマリ

| 指標 | 値 |
|---|---|
| 対象メンバー数 | {summary['n_persons']} 名（アカウント {summary['n_accounts']}） |
| 現在のシート費用 | {_fmt_usd(total['seat_cost_now_usd'])} /月 |
| 全体の API 換算需要（ユーザ帰属分） | {_fmt_usd(total['total_api_cost_usd'])} /月 |
| 実際の従量課金（ユーザ帰属分） | {_fmt_usd(total['total_billed_extra_usd'])} /月 |
| 組織サービス利用（ユーザ非帰属・シート判定対象外） | {_fmt_usd(total['org_service_cost_usd'])} /月{_org_products(org_service)} |
| **変更推奨** | **{total['n_change_recommended']} 名（削減見込み {_fmt_usd(total['est_monthly_saving_usd'])} /月）** |
| 要観察 | {total['n_watching']} 名 |
| 上限到達疑い（Standard） | {total['n_cap_suspected']} 名 |
{credit_rows}| 判定に使用した月 | {months}（ヒステリシス {hysteresis} ヶ月） |

{spaces.workspaces_md(org, summary)}
{extra_sections}
## シート変更推奨

{changes_block}

{spaces.multi_space_md(org)}

## 注意事項

- 従量課金（usage credits）が無効の場合、Standardユーザの利用量は上限で頭打ちになるため、
  実際の需要はここに表示された値より大きい可能性があります（センサリング）。
- 「Standard時/Premium時」の従量課金額は allowance の推定値（mid シナリオ）に基づく試算です。{note_lines}
- スペンドデータは前日分まで・過去90日分のみ参照可能です。毎月のエクスポートを忘れずに。

## データ検証・警告

{warnings_md}

## 考察

<!-- /seat-analysis または seat-analyzer discuss 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis` または `seat-analyzer discuss` を実行すると考察が追記されます）
"""
    md = _preserve_discussion(
        md, path, fallback=REPORT.legacy_sibling(path, org.month, org.org))
    _atomic_write(path, md)


def write_preview_markdown(result: PreviewResult, path: Path) -> None:
    """速報モードの Markdown（preview.md）。"""
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
    cap_usd = s["grant_suggested_cap_usd"]
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
    md = _preserve_discussion(
        md, path, fallback=PREVIEW.legacy_sibling(path, result.month, result.org))
    # 引き継いだ手書きの考察は他のどこにも無い。切り詰めてから書く write_text では
    # 中断時に本文ごと失うため、考察の差し替えと同じく置換で書く
    _atomic_write(path, md)


def _summary_line(result: OrgAnalysisResult) -> tuple[dict, str, int, int]:
    """横断サマリの1組織分（数値・メンバー列の表記・人数・アカウント数）。

    複数 workspace の組織は人数とアカウント数を併記し、数値は組織の合計を使う。
    それ以外は唯一の workspace の summary をそのまま使う（従来と同じ値・同じ表記）。
    """
    if result.has_multiple_workspaces:
        summary = summarize_org(result)
        n_persons, n_accounts = summary["n_persons"], summary["n_accounts"]
        return summary["total"], f"{n_persons} 名（アカウント {n_accounts}）", n_persons, n_accounts
    s = _sole_result(result).summary
    return s, f"{s['n_members']} 名", s["n_members"], s["n_members"]


def write_org_summary(results: list[AnalysisResult | OrgAnalysisResult],
                      output_dir: str | Path) -> Path:
    """複数組織を一括分析したときの横断サマリ（reports/summary/YYYY-MM.md）。

    複数 workspace の組織は、メンバー列を「人数（アカウント数）」にする。変更推奨の
    列はアカウント単位のまま。合計行は人数の和で、複数 workspace の組織が1つでも
    あればアカウント数の和を添える。
    """
    orgs = [r if isinstance(r, OrgAnalysisResult) else single_org_result(r) for r in results]
    month = orgs[0].month
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
        "seat_cost_now_usd", "total_api_cost_usd",
        "total_billed_extra_usd", "org_service_cost_usd",
        "n_change_recommended", "est_monthly_saving_usd",
    )
    totals = dict.fromkeys(keys, 0.0)
    # 合計行の人数は人の和（複数 workspace の組織も人数で数える）。アカウント数の和は
    # 複数 workspace の組織があるときだけ添える
    n_persons_total = 0
    n_accounts_total = 0
    any_multi = False
    for r in orgs:
        s, members, n_persons, n_accounts = _summary_line(r)
        any_multi = any_multi or r.has_multiple_workspaces
        n_persons_total += n_persons
        n_accounts_total += n_accounts
        for k in keys:
            totals[k] += float(s.get(k, 0) or 0)
        lines.append(
            f"| [{r.org}](../{r.org}/{month}/{REPORT.name(month, r.org)}) | {members} "
            f"| {_fmt_usd(s['seat_cost_now_usd'])} | {_fmt_usd(s['total_api_cost_usd'])} "
            f"| {_fmt_usd(s.get('total_billed_extra_usd', 0.0))} | {_fmt_usd(s.get('org_service_cost_usd', 0.0))} "
            f"| {s['n_change_recommended']} 名 | {_fmt_usd(s['est_monthly_saving_usd'])} |"
        )
    members_total = f"{n_persons_total} 名" + (
        f"（アカウント {n_accounts_total}）" if any_multi else "")
    lines += [
        (f"| **合計** | **{members_total}** "
         f"| **{_fmt_usd(totals['seat_cost_now_usd'])}** | **{_fmt_usd(totals['total_api_cost_usd'])}** "
         f"| **{_fmt_usd(totals['total_billed_extra_usd'])}** | **{_fmt_usd(totals['org_service_cost_usd'])}** "
         f"| **{int(totals['n_change_recommended'])} 名** | **{_fmt_usd(totals['est_monthly_saving_usd'])}** |"),
        "",
        f"各組織の詳細は `reports/<組織>/{month}/{REPORT.name(month, '<組織>')}` を参照。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path
