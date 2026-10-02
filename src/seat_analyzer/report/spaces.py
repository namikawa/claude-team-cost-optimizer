"""複数 workspace の組織だけが持つ出力（スペース別・人別の利用・複数スペースの利用）。

config の workspaces が2つ以上ある組織（OrgAnalysisResult.has_multiple_workspaces）で
だけ使う。単一 workspace の組織の成果物には、ここで作る節・列・注記を一切足さない。

人の層（analyze/persons.py）の判定はここでは変えず、表示の順序と文言だけを持つ。
Markdown（report.md / details.md）と dashboard の両方が同じ行データを読むよう、
行の組み立て（並び・どの行を出すか）と書式（金額の桁）を分けてある。
"""

from __future__ import annotations

from collections.abc import Callable

from ..analyze import (
    CONTINUATION_WATCH,
    CONTINUE,
    PAYOUT_CANDIDATE,
    PAYOUT_NO_EVIDENCE,
    PAYOUT_UNNEEDED,
    PAYOUT_WATCH,
    RETURN_CANDIDATE,
    SEAT_LABELS,
    WAITING,
    OrgAnalysisResult,
    PersonLayer,
)
from .format import (
    _fmt_compact,
    _fmt_tokens,
    _fmt_usd,
    _has_values,
    _is_missing,
    _md_cell,
    _text_value,
)
from .naming import DETAILS
from .text import STATUS_ORDER

# 払い出し判定の表の並び（対応が明確なものから）。「不要」は表に出さず人数だけを書く
PAYOUT_ORDER = (PAYOUT_CANDIDATE, PAYOUT_WATCH, PAYOUT_NO_EVIDENCE)
# 継続判定の表の並び（対応が明確なものから）
CONTINUATION_ORDER = (RETURN_CANDIDATE, CONTINUATION_WATCH, WAITING, CONTINUE)

# 金額の書式（Markdown はセント単位、dashboard は $100 以上を整数にする）
Formatter = Callable[[object], str]


def _people_layer(org: OrgAnalysisResult) -> PersonLayer:
    """人の層（analyze_org が必ず組む。手で組んだ容れ物で欠けていれば止める）。"""
    if org.persons is None:
        raise ValueError(f"組織 {org.org} の人の層がありません（analyze_org の結果が必要です）")
    return org.persons


def _pct(value) -> str:
    """比率の整数パーセント表示（確定しない値は —）。"""
    if _is_missing(value):
        return "—"
    return f"{round(100.0 * float(value))}%"


def _seat(seat: str) -> str:
    return SEAT_LABELS.get(seat, seat)


def space_label(org: OrgAnalysisResult, name: str) -> str:
    """スペース別の表に出す名前（表示名に主・固定シートの印を添える）。"""
    context = org.contexts[name]
    marks = []
    if context.primary:
        marks.append("主")
    if context.fixed_seat:
        marks.append(f"{_seat(context.fixed_seat)} 固定")
    return context.label + (f"（{'・'.join(marks)}）" if marks else "")


def space_list(org: OrgAnalysisResult) -> str:
    """スペースの一覧（表示名（ディレクトリ名・主／副・固定シート））。"""
    parts = []
    for name, context in org.contexts.items():
        marks = [name, "主" if context.primary else "副"]
        if context.fixed_seat:
            marks.append(f"{_seat(context.fixed_seat)} 固定")
        parts.append(f"{context.label}（{'・'.join(marks)}）")
    return " / ".join(parts)


def fixed_seat_labels(org: OrgAnalysisResult) -> list[str]:
    """シート種別を固定した、分析済みの workspace の表示名（config の順）。"""
    return [
        org.contexts[name].label for name in org.workspaces
        if org.contexts[name].fixed_seat
    ]


# 判定の表で、主の行の需要が合算値であることの断り（Markdown の凡例と dashboard の脚注）
MERGED_DEMAND_NOTE = (
    "スペースを複数持つ人の主の行の API換算需要は全スペースの合算（判定に使う値）で、"
    "縦に足すと副の分が二重になる"
)


def account_legend_lines(org: OrgAnalysisResult) -> tuple[str, ...]:
    """アカウントを連結した判定表（全ユーザ・シート変更推奨）の凡例に足す行。"""
    lines = []
    fixed = fixed_seat_labels(org)
    if fixed:
        lines.append(
            f"- **対象外（固定シート）**: 運用方針でシート種別を固定したスペース"
            f"（{'・'.join(fixed)}）のアカウント。Standard / Premium の損益分岐判定は"
            "行わない（続けるか戻すかは「複数スペースの利用」の継続判定）"
        )
    lines.append(f"- **API換算需要（複数スペース）**: {MERGED_DEMAND_NOTE}")
    return tuple(lines)


def notes_lines(org: OrgAnalysisResult) -> list[str]:
    """注意事項に足す行（末尾の句点は使う側で付ける）。"""
    lines = []
    fixed = fixed_seat_labels(org)
    if fixed:
        lines.append(
            f"対象外（固定シート）は運用方針でシート種別を固定したスペース（{'・'.join(fixed)}）の"
            "アカウントで、Standard / Premium の損益分岐判定・感度分析・追加クレジット付与候補の"
            "対象にしていません"
        )
    lines.append(
        "人数以外の件数（変更推奨・要観察・上限到達疑い・一覧の件数）はアカウント単位です"
        "（同じ人が複数のスペースで数えられることがあります）"
    )
    return lines


# --- スペース別（組織の内訳） ---

def _workspace_note(row: dict) -> str:
    if row["skipped"]:
        return "未開始（対象月以前のデータなし）"
    if row["assume_no_usage"]:
        return "需要 0（対象月の spend 無し）"
    return ""


def workspace_rows(org: OrgAnalysisResult, summary: dict) -> list[dict]:
    """スペース別の表の行（config の順＝主が先。summary は summarize_org の戻り）。"""
    rows = []
    for row in summary["workspaces"]:
        rows.append({**row, "space": space_label(org, row["name"]),
                     "note": _workspace_note(row)})
    return rows


def workspaces_md(org: OrgAnalysisResult, summary: dict) -> str:
    """report.md のサマリ直下に置く「### スペース別」の表。"""
    header = ("| スペース | 人数 | Standard | Premium | 未割当 | 不明 | シート費用/月 |"
              " API換算需要/月 | 実課金(従量)/月 | 変更推奨 |")
    lines = ["### スペース別", "", header, "|" + "---|" * 10]
    notes = []
    for row in workspace_rows(org, summary):
        if row["skipped"]:
            cells = [_md_cell(row["space"]), *(["—"] * 9)]
        else:
            cells = [
                _md_cell(row["space"]), f"{row['n_members']} 名",
                str(row["n_standard"]), str(row["n_premium"]),
                str(row["n_unassigned"]), str(row["n_unknown"]),
                _fmt_usd(row["seat_cost_now_usd"]), _fmt_usd(row["total_api_cost_usd"]),
                _fmt_usd(row["total_billed_extra_usd"]),
                f"{row['n_change_recommended']} 名",
            ]
        lines.append("| " + " | ".join(cells) + " |")
        if row["note"]:
            notes.append(f"- {_md_cell(row['space'])}: {row['note']}")
    total = summary["total"]
    lines.append("| " + " | ".join([
        "合計", f"{total['n_members']} 名",
        str(total["n_standard"]), str(total["n_premium"]),
        str(total["n_unassigned"]), str(total["n_unknown"]),
        _fmt_usd(total["seat_cost_now_usd"]), _fmt_usd(total["total_api_cost_usd"]),
        _fmt_usd(total["total_billed_extra_usd"]),
        f"{total['n_change_recommended']} 名",
    ]) + " |")
    lines += [
        "",
        (f"- 人数はそのスペースのアカウント数です。合計は複数のスペースにアカウントを持つ人を"
         f"重複して数えます（人数は {summary['n_persons']} 名）"),
        *notes,
    ]
    return "\n".join(lines)


def workspace_table_view(org: OrgAnalysisResult, summary: dict) -> dict:
    """dashboard の「スペース別」カード（金額は $100 以上を整数）。"""
    rows = []
    for row in workspace_rows(org, summary):
        rows.append({
            "space": row["space"],
            "skipped": row["skipped"],
            "note": row["note"],
            "n_members": row["n_members"],
            "n_standard": row["n_standard"],
            "n_premium": row["n_premium"],
            "n_unassigned": row["n_unassigned"],
            "n_unknown": row["n_unknown"],
            "seat_cost_fmt": _fmt_compact(row["seat_cost_now_usd"]),
            "api_fmt": _fmt_compact(row["total_api_cost_usd"]),
            "billed_fmt": _fmt_compact(row["total_billed_extra_usd"]),
            "n_change": row["n_change_recommended"],
        })
    total = summary["total"]
    return {
        "rows": rows,
        "total": {
            "n_members": total["n_members"],
            "n_standard": total["n_standard"],
            "n_premium": total["n_premium"],
            "n_unassigned": total["n_unassigned"],
            "n_unknown": total["n_unknown"],
            "seat_cost_fmt": _fmt_compact(total["seat_cost_now_usd"]),
            "api_fmt": _fmt_compact(total["total_api_cost_usd"]),
            "billed_fmt": _fmt_compact(total["total_billed_extra_usd"]),
            "n_change": total["n_change_recommended"],
        },
        "n_persons": summary["n_persons"],
    }


# --- 人別の利用（1人1行） ---

def _held_seat_parts(org: OrgAnalysisResult, person) -> list[str]:
    """保有シートの「表示名: シート種別」の並び（主は常に先頭で、主にアカウントが
    無ければ「—」）。

    副はアカウントのある workspace だけを並べ、未割当のアカウントもそのまま表示する。
    Markdown は「 / 」でつないだ1行（_held_seats）、dashboard は列幅を抑えるため
    スペースごとに改行して並べる。
    """
    by_workspace = {account.workspace: account.seat for account in person.accounts}
    primary_seat = (_seat(by_workspace[org.primary])
                    if org.primary in by_workspace else "—")
    parts = [f"{org.contexts[org.primary].label}: {primary_seat}"]
    for name in org.contexts:
        if name != org.primary and name in by_workspace:
            parts.append(f"{org.contexts[name].label}: {_seat(by_workspace[name])}")
    return parts


def _held_seats(org: OrgAnalysisResult, person) -> str:
    """保有シート（_held_seat_parts を「 / 」でつないだ1行）。"""
    return " / ".join(_held_seat_parts(org, person))


def person_rows(org: OrgAnalysisResult) -> tuple[list[dict], dict]:
    """人別の利用の行と列の有無（判定の表示順 → 需要の降順 → email）。"""
    layer = _people_layer(org)
    frame = layer.frame
    persons = {person.email: person for person in layer.persons}
    order = {status: index for index, status in enumerate(STATUS_ORDER)}
    columns = {
        "dept": _has_values(frame, "department"),
        "team": _has_values(frame, "team"),
        "loc": "loc_with_cc" in frame.columns,
    }
    rows = []
    for record in frame.to_dict("records"):
        email = str(record["email"])
        saving = record.get("monthly_saving_usd")
        rows.append({
            "email": email,
            "seats": _held_seats(org, persons[email]),
            "seat_parts": _held_seat_parts(org, persons[email]),
            "department": _text_value(record.get("department")),
            "team": _text_value(record.get("team")),
            "seat_cost": float(record["seat_cost_usd"]),
            "api": float(record["api_cost_usd"]),
            "primary_api": float(record["primary_api_cost_usd"]),
            "secondary_api": float(record["secondary_api_cost_usd"]),
            # 副を持たない人（副で未割当だけの人を含む）は比率を出さない（副をほぼ
            # 使っていない人の 0% と区別するため。値そのものは人の表のまま）
            "ratio": (record.get("secondary_ratio")
                      if persons[email].has_secondary else None),
            "billed": float(record["billed_extra_usd"]),
            "status": str(record["status"]),
            "saving": None if _is_missing(saving) else float(saving),
            "input": int(record["prompt_tokens"]),
            "output": int(record["completion_tokens"]),
            "loc": record.get("loc_with_cc") if columns["loc"] else None,
        })
    rows.sort(key=lambda r: (order.get(r["status"], len(order)), -r["api"], r["email"]))
    return rows, columns


PERSON_LEGEND = (
    ("1人1行。API換算需要は全スペースの需要の合計で、主の需要・副の需要はそれぞれのスペースの"
     "アカウント自身の需要"),
    "副の比率 = 副の需要 / API換算需要（副にシートを持たない人と、需要が無い人は —）",
    "シート費/月は保有シートの月額の合計、実課金(従量)は全アカウントの合計",
    "input / output は全アカウントのトークンの合計（input はキャッシュ読取分を含む）",
    ("シート判定はアカウントの V1 判定（どれかのアカウントが変更推奨ならその判定、無ければ主の"
     "アカウントの判定）。シート変更の削減/月は V1 の変更推奨による削減額で、「複数スペースの利用」"
     "の継続判定（戻す候補・削減見込み/月）とは別の値"),
)


def persons_md(org: OrgAnalysisResult) -> str:
    """details.md の「## 人別の利用」（全員1人1行）。"""
    rows, columns = person_rows(org)
    header = (
        "| ユーザ | 保有シート |"
        + (" 部署 |" if columns["dept"] else "")
        + (" チーム |" if columns["team"] else "")
        + " シート費/月 | API換算需要 | 主の需要 | 副の需要 | 副の比率 | 実課金(従量) |"
        + " シート判定 | シート変更の削減/月 | input | output |"
        + (" 行数(CC) |" if columns["loc"] else "")
    )
    n_columns = 12 + int(columns["dept"]) + int(columns["team"]) + int(columns["loc"])
    lines = ["## 人別の利用", "", header, "|" + "---|" * n_columns]
    for r in rows:
        cells = [r["email"], r["seats"]]
        if columns["dept"]:
            cells.append(r["department"])
        if columns["team"]:
            cells.append(r["team"])
        cells += [
            _fmt_usd(r["seat_cost"]), _fmt_usd(r["api"]), _fmt_usd(r["primary_api"]),
            _fmt_usd(r["secondary_api"]), _pct(r["ratio"]), _fmt_usd(r["billed"]),
            r["status"], _fmt_usd(r["saving"]),
            _fmt_tokens(r["input"]), _fmt_tokens(r["output"]),
        ]
        if columns["loc"]:
            cells.append("—" if _is_missing(r["loc"]) else f"{int(r['loc']):,}")
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    lines.append("")
    lines += [f"- {line}" for line in PERSON_LEGEND]
    return "\n".join(lines)


# --- 複数スペースの利用（§26.5 の参考判定） ---

def _monthly_text(pairs, fmt: Formatter) -> str:
    return " / ".join(f"{month} {fmt(value)}" for month, value in pairs)


def _cap_text(judgment) -> str:
    if judgment.cap_reached:
        return "到達"
    # 主の追加クレジットが無効・不明などで判断材料が無い人は、到達の有無も語れない
    return "—" if judgment.status == PAYOUT_NO_EVIDENCE else "未到達"


def payout_rows(org: OrgAnalysisResult, fmt: Formatter) -> tuple[list[dict], int]:
    """払い出し判定の表の行（不要を除く）と、不要の人数。

    並びは 候補 → 観察 → 判断材料なし、同じ判定の中は主の実課金の降順・email。
    """
    layer = _people_layer(org)
    order = {status: index for index, status in enumerate(PAYOUT_ORDER)}
    shown = [j for j in layer.payout if j.status != PAYOUT_UNNEEDED]
    shown.sort(key=lambda j: (order.get(j.status, len(order)), -j.billed_usd, j.email))
    rows = [{
        "email": j.email,
        "status": j.status,
        "billed_fmt": fmt(j.billed_usd),
        "cap": _cap_text(j),
        "streak": j.streak_months,
        "api_fmt": fmt(j.api_cost_usd),
        "code_ratio": _pct(j.code_ratio),
        "loc": "—" if j.loc_with_cc is None else f"{j.loc_with_cc:,}",
        "monthly": _monthly_text(j.monthly_billed, fmt),
        "reason": j.reason,
    } for j in shown]
    unneeded = sum(1 for j in layer.payout if j.status == PAYOUT_UNNEEDED)
    return rows, unneeded


def _continuation_notes(judgment) -> str:
    notes = []
    if judgment.idle:
        notes.append("遊休")
    if judgment.over_primary_cap_months:
        notes.append(f"主の上限超え: {', '.join(judgment.over_primary_cap_months)}")
    return "・".join(notes)


def continuation_rows(org: OrgAnalysisResult, fmt: Formatter) -> list[dict]:
    """継続判定の表の行。

    並びは 戻す候補 → 観察 → データ蓄積待ち → 継続、同じ判定の中は副の需要の昇順・email。
    """
    layer = _people_layer(org)
    order = {status: index for index, status in enumerate(CONTINUATION_ORDER)}
    judgments = sorted(
        layer.continuation,
        key=lambda j: (order.get(j.status, len(order)), j.api_cost_usd, j.email, j.workspace),
    )
    return [{
        "email": j.email,
        "space": org.contexts[j.workspace].label,
        "seat": _seat(j.seat),
        "status": j.status,
        "api_fmt": fmt(j.api_cost_usd),
        "breakeven_fmt": fmt(j.breakeven_usd),
        "evaluation_months": j.evaluation_months,
        "complete_months": j.complete_months,
        "streak_months": j.streak_months,
        "saving_fmt": "—" if j.saving_usd is None else fmt(j.saving_usd),
        "monthly": _monthly_text(j.monthly_demand, fmt),
        "notes": _continuation_notes(j),
    } for j in judgments]


def billed_rows(org: OrgAnalysisResult, fmt: Formatter) -> list[dict]:
    """副を持ちながら主で実課金が発生した人の行（email 昇順）。"""
    layer = _people_layer(org)
    return [{
        "email": row.email,
        "billed_fmt": fmt(row.billed_usd),
        "secondary_fmt": fmt(row.secondary_api_cost_usd),
        "monthly": "; ".join(
            f"{month} {fmt(billed)} / {fmt(demand)}" for month, billed, demand in row.monthly),
    } for row in layer.billed_with_secondary]


def overview_lines(org: OrgAnalysisResult, fmt: Formatter) -> list[str]:
    """節の先頭に置く前提（スペースの一覧・払い出し判定の対象と閾値）。"""
    layer = _people_layer(org)
    lines = [f"スペース: {space_list(org)}"]
    if layer.payout_workspace is not None and layer.breakeven_usd is not None:
        lines.append(
            f"払い出し判定の対象: {org.contexts[layer.payout_workspace].label}"
            f"（設定された損益分岐 {fmt(layer.breakeven_usd)}・必要な連続月数"
            f" {layer.evaluation_months} か月）"
        )
    return lines


# 判定の読み方。機序の説明はこの1箇所に置き、Markdown と dashboard が同じ文を出す
JUDGMENT_LEGEND = (
    ("払い出し判定は、副にシートを持たず（副で未割当の人を含む）主のシートが Standard / Premium の"
     "人について、主の実課金（副を足さずに追加クレジットへ払っている額）を設定された損益分岐と"
     "比べます。対象月に主の追加クレジット上限へ到達していれば1か月で候補、損益分岐以上の月が"
     "必要な連続月数続けば候補、候補に当たらず主の実課金が 0 より大きければ観察、0 なら不要です"),
    ("主の追加クレジットが無効の人と、有効かどうか分からない人（上限が未記入で実課金も観測されて"
     "いない）は、実課金が上限到達を語らないので判断材料なしです。主のシートが払い出すシート種別と"
     "違う人も判断材料なしです"),
    ("継続判定は、副にシートを持つアカウントの対象月の需要（API 換算。同じ利用を追加クレジットで"
     "賄った場合の課金額）を、設定された損益分岐（secondary_breakeven_usd。既定は副のシート料＝"
     "払い出すシート種別の価格）と比べます。損益分岐以上なら継続、払い出し後の月数が必要月数に"
     "満たなければデータ蓄積待ち、直近の必要月数がすべて損益分岐未満なら戻す候補、それ以外は"
     "観察です"),
    ("払い出し後の月数は、払い出した月（そのスペースの spend か members に初めて現れた月）より後の"
     "観測月の数で、払い出した月は数えません。部分月の警告がある月も含むので、データ検証・警告と"
     "併せて読んでください。損益分岐未満が続いた月数も同じ月の数え方です"),
    ("削減見込み/月は副のシート料 − 副の需要（戻した場合に追加クレジットへ回る分を差し引いた額）"
     "で、戻す候補にだけ出します"),
    ("遊休は副の需要が設定 trend.idle_usd 未満、主の上限超えは副の需要が主の追加クレジット上限を"
     "超えた月（クレジットでは賄えなかった量が観測された月）です"),
    ("副を持ちながら主で実課金が発生した人は判定ではなく事実の一覧です。副に切り替えずに"
     "クレジットを使っているのか、両方の枠を使い切っているのかを、月別の副の需要と並べて読みます"),
)


def multi_space_md(org: OrgAnalysisResult) -> str:
    """report.md の「## 複数スペースの利用」（判定・スペース一覧・閾値）。"""
    details = DETAILS.name(org.month, org.org)
    lines = ["## 複数スペースの利用", ""]
    lines += [f"- {_md_cell(line)}" for line in overview_lines(org, _fmt_usd)]
    lines.append(f"- 人別の利用（全員1人1行）は {details} の「人別の利用」にあります")

    payout, unneeded = payout_rows(org, _fmt_usd)
    lines += ["", "### 払い出し判定（副を持たない人）", ""]
    if payout:
        lines += [
            ("| ユーザ | 判定 | 主の実課金 | 上限到達 | 損益分岐以上が続いた月数 | API換算需要 |"
             " Code比率 | 行数(CC) | 月別の実課金 | 理由 |"),
            "|" + "---|" * 10,
        ]
        for r in payout:
            cells = [r["email"], r["status"], r["billed_fmt"], r["cap"], str(r["streak"]),
                     r["api_fmt"], r["code_ratio"], r["loc"], r["monthly"], r["reason"]]
            lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    else:
        lines.append("該当なし。")
    if unneeded:
        lines += ["", f"- 不要 {unneeded} 名（主の実課金 $0）"]

    continuation = continuation_rows(org, _fmt_usd)
    lines += ["", "### 継続判定（副にシートを持つ人）", ""]
    if continuation:
        lines += [
            ("| ユーザ | スペース | シート | 判定 | 副の需要 | 損益分岐 | 必要月数 |"
             " 払い出し後の月数 | 損益分岐未満が続いた月数 | 削減見込み/月 | 月別の副の需要 |"
             " 注記 |"),
            "|" + "---|" * 12,
        ]
        for r in continuation:
            cells = [r["email"], r["space"], r["seat"], r["status"], r["api_fmt"],
                     r["breakeven_fmt"], str(r["evaluation_months"]),
                     str(r["complete_months"]), str(r["streak_months"]), r["saving_fmt"],
                     r["monthly"], r["notes"]]
            lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    else:
        lines.append("該当なし。")

    billed = billed_rows(org, _fmt_usd)
    lines += ["", "### 副を持ちながら主で実課金が発生した人", ""]
    if billed:
        lines += ["| ユーザ | 主の実課金 | 副の需要 | 月別（主の実課金 / 副の需要） |",
                  "|---|---|---|---|"]
        for r in billed:
            cells = [r["email"], r["billed_fmt"], r["secondary_fmt"], r["monthly"]]
            lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    else:
        lines.append("該当なし。")

    lines += ["", "### 判定の読み方", ""]
    lines += [f"- {line}" for line in JUDGMENT_LEGEND]
    return "\n".join(lines)


def spaces_view(org: OrgAnalysisResult) -> dict:
    """dashboard の「複数スペース」タブ（金額は $100 以上を整数）。"""
    rows, columns = person_rows(org)
    persons = [{
        **r,
        "seat_cost_fmt": _fmt_compact(r["seat_cost"]),
        "api_fmt": _fmt_compact(r["api"]),
        "primary_fmt": _fmt_compact(r["primary_api"]),
        "secondary_fmt": _fmt_compact(r["secondary_api"]),
        "ratio_fmt": _pct(r["ratio"]),
        "billed_fmt": _fmt_compact(r["billed"]),
        "saving_fmt": _fmt_compact(r["saving"]),
        "input_fmt": _fmt_tokens(r["input"]),
        "output_fmt": _fmt_tokens(r["output"]),
        "loc_fmt": "—" if _is_missing(r["loc"]) else f"{int(r['loc']):,}",
    } for r in rows]
    payout, unneeded = payout_rows(org, _fmt_compact)
    return {
        "overview": overview_lines(org, _fmt_compact),
        "persons": persons,
        "person_columns": columns,
        "person_legend": PERSON_LEGEND,
        "payout": payout,
        "unneeded": unneeded,
        "continuation": continuation_rows(org, _fmt_compact),
        "billed": billed_rows(org, _fmt_compact),
        "legend": JUDGMENT_LEGEND,
    }
