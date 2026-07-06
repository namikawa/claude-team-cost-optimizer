"""レポート生成: report.md / dashboard.html / recommendations.csv"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from jinja2 import Environment

from .analyze import SEAT_LABELS, AnalysisResult, PreviewResult
from .ingest import parse_affiliations

STATUS_ORDER = ["変更推奨", "要観察", "要観察（データ蓄積待ち）", "シート不明", "現状維持",
                "対象外（シート未割当）"]

# 速報の一次判断ラベルの表示順（対応アクションが明確なものから）
PREVIEW_ORDER = ["遊休候補", "Standard候補", "Premium検討", "判断保留",
                 "シート不明", "Premium妥当", "Standard妥当", "対象外（未割当）"]

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


# Excel/スプレッドシートで式として解釈されうる先頭文字（formula injection 対策）
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(v):
    if isinstance(v, str) and v.startswith(_FORMULA_PREFIXES):
        return "'" + v
    return v


def write_csv(result: AnalysisResult, path: Path) -> None:
    result.users.map(_sanitize_csv_cell).to_csv(path, index=False, encoding="utf-8-sig")


def _fmt_usd(v) -> str:
    if v is None or v != v:
        return "—"
    return f"${v:,.2f}"


def _md_cell(v) -> str:
    """Markdown 表セル用のエスケープ（表崩れ防止）。パイプ・改行が主な対象。"""
    s = "" if v is None else str(v)
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")
    return s


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


def _seat_price(seat: str, summary: dict) -> float:
    """シート料金（unassigned/unknown は判定対象外のため $0 扱い）。summary の価格を使う。"""
    if seat == "standard":
        return float(summary.get("seat_price_standard_usd", 0.0))
    if seat == "premium":
        return float(summary.get("seat_price_premium_usd", 0.0))
    return 0.0


def _group_summary_rows(users: pd.DataFrame, summary: dict, col: str) -> list[dict]:
    """指定軸（col）でのグループ別サマリの行データ。col 非空のユーザがいない場合は空リスト。

    兼務（複数所属）ユーザは所属数 n で 1/n の重みに按分し、各所属グループへ計上する
    （人数・費用・需要・実課金・変更推奨数・削減見込みすべて同じ重み）。所属が空のユーザは
    「（未設定）」へ重み1で計上する。よって各グループの縦合計は常に全体と一致する。
    API換算需要の降順、（未設定）は常に最後。
    """
    if not _has_values(users, col):
        return []
    has_billed = "billed_extra_usd" in users.columns
    # グループ名 → 集計値の accumulator（初期化順は問わない。最後に並べ替える）
    acc: dict[str, dict] = {}
    for _, r in users.iterrows():
        groups = parse_affiliations(r.get(col)) or ["（未設定）"]
        w = 1.0 / len(groups)
        is_change = r["status"] == "変更推奨"
        seat_price = _seat_price(r["current_seat"], summary)
        api = float(r["api_cost_usd"]) if r["api_cost_usd"] == r["api_cost_usd"] else 0.0
        billed = float(r["billed_extra_usd"] or 0.0) if has_billed and r["billed_extra_usd"] == r["billed_extra_usd"] else 0.0
        saving = float(r["monthly_saving_usd"] or 0.0) if is_change and r["monthly_saving_usd"] == r["monthly_saving_usd"] else 0.0
        for grp in groups:
            a = acc.setdefault(grp, {"n": 0.0, "seat_cost": 0.0, "api": 0.0,
                                     "billed": 0.0, "n_change": 0.0, "saving": 0.0})
            a["n"] += w
            a["seat_cost"] += seat_price * w
            a["api"] += api * w
            a["billed"] += billed * w
            a["n_change"] += (1.0 * w) if is_change else 0.0
            a["saving"] += saving * w
    rows = []
    for grp, a in acc.items():
        rows.append({
            "group": grp,
            "is_unset": grp == "（未設定）",
            "n": a["n"],
            "seat_cost": a["seat_cost"],
            "api": a["api"],
            "billed": a["billed"],
            "n_change": a["n_change"],
            "saving": a["saving"],
        })
    rows.sort(key=lambda r: (r["is_unset"], -r["api"]))
    return rows


def _fmt_count(v) -> str:
    """按分後の人数・変更推奨数の表示。整数なら「3」、端数は小数1桁「3.5」（末尾ゼロなし）。"""
    r = round(float(v), 1)
    return str(int(r)) if r == int(r) else f"{r:.1f}"


def _group_summary_md(users: pd.DataFrame, summary: dict, col: str, heading: str) -> str:
    """指定軸（col）のグループ別サマリ表。col 非空のユーザがいる場合のみ生成し、無ければ空文字列。

    heading は見出し文言（例: "部署別サマリ"）で、1列目のヘッダにも流用する。
    """
    rows = _group_summary_rows(users, summary, col)
    if not rows:
        return ""
    col_label = heading.replace("別サマリ", "")
    lines = [
        f"## {heading}",
        "",
        f"| {col_label} | 人数 | シート費用/月 | API換算需要/月 | 実課金(従量)/月 | 変更推奨 | 削減見込み/月 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {_md_cell(r['group'])} | {_fmt_count(r['n'])} 名 | {_fmt_usd(r['seat_cost'])} "
            f"| {_fmt_usd(r['api'])} | {_fmt_usd(r['billed'])} "
            f"| {_fmt_count(r['n_change'])} 名 | {_fmt_usd(r['saving'])} |"
        )
    return "\n".join(lines) + "\n"


def write_markdown(result: AnalysisResult, path: Path) -> None:
    s = result.summary
    users = result.users.copy()
    users["_order"] = users["status"].map(
        {st: i for i, st in enumerate(STATUS_ORDER)}
    ).fillna(len(STATUS_ORDER))
    users = users.sort_values(["_order", "monthly_saving_usd"], ascending=[True, False])

    changes = users[users["status"] == "変更推奨"]
    sensitivity_disagree = users[users["confidence"].isin(["中", "低"])]

    nl = "\n"
    notes_block = _notes_md(users)
    dept_block = _group_summary_md(users, s, "department", "部署別サマリ")
    team_block = _group_summary_md(users, s, "team", "チーム別サマリ")

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
| 判定に使用した月 | {', '.join(s['months_used'])}（ヒステリシス {s['hysteresis_months']} ヶ月） |

## シート変更推奨

{_user_table_md(changes) if not changes.empty else "該当なし。"}

## 全ユーザ

{_user_table_md(users)}

- **API換算需要**: 当月の全利用量をAPI料金（キャッシュ実効単価込み）に換算した金額。シート込み分を含む「需要」の指標
- **実課金(従量)**: スペンドレポートの net_spend 合計。シート込み利用は $0 で、上限超過の従量課金分のみ計上される
- **Standard時 / Premium時**: そのシートの場合の想定月額。**現シート側はシート料+実課金の観測実績**、変更先側は allowance（込み利用量）モデルによる試算
- **⚠️上限?**: 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ。「実効込み量が推定より大きい」か「上限で停止した」かの要確認
- **確度**: 込み利用量（allowance）の low/mid/high 3シナリオで推奨が一致するか（高=3/3, 中=2/3, 低=1/3）
- **対象外（シート未割当）**: 意図的にシートを割り当てていないメンバー（別組織でアサイン済み・管理者等）。損益分岐判定は行わない
{(nl + notes_block) if notes_block else ''}{(nl + dept_block) if dept_block else ''}{(nl + team_block) if team_block else ''}
## 感度分析

allowance（シート込み利用量のUSD換算・非公開のため推定）の仮定によって推奨が変わるユーザ:

{_user_table_md(sensitivity_disagree) if not sensitivity_disagree.empty else "なし（全ユーザで3シナリオの推奨が一致）。"}

## 注意事項

- 従量課金（usage credits）が無効の場合、Standardユーザの利用量は上限で頭打ちになるため、
  実際の需要はここに表示された値より大きい可能性があります（センサリング）。
- 「Standard時/Premium時」の従量課金額は allowance の推定値（mid シナリオ）に基づく試算です。
- スペンドデータは前日分まで・過去90日分のみ参照可能です。毎月のエクスポートを忘れずに。

## データ検証・警告

{chr(10).join(f'- {w}' for w in result.warnings) if result.warnings else '- なし'}

## 考察

<!-- /seat-analysis 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis` を実行すると考察が追記されます）
"""
    md = _preserve_discussion(md, path)
    path.write_text(md, encoding="utf-8")


def _preserve_discussion(md: str, path: Path) -> str:
    """再生成時、既存 report.md の記入済み「## 考察」セクションを引き継ぐ。"""
    marker = "\n## 考察\n"
    if not path.exists():
        return md
    existing = path.read_text(encoding="utf-8")
    if marker not in existing:
        return md
    tail = existing.split(marker, 1)[1]
    if "未記入" in tail:
        return md
    return md.split(marker, 1)[0] + marker + tail


_HTML_TEMPLATE = _HTML_ENV.from_string(r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Team シート最適化 — {{ scope }}</title>
<style>
  :root { --std:#4a90d9; --prem:#d97a4a; --ok:#2e8b57; --warn:#c0392b; }
  * { box-sizing: border-box; }
  body { font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; margin:0; background:#f6f7f9; color:#1f2933; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  .cards { display:grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
  .card { background:#fff; border-radius:10px; padding:14px 16px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .card .v { font-size:1.5rem; font-weight:700; } .card .l { font-size:.78rem; color:#6b7280; }
  .card.hl .v { color: var(--ok); }
  .tablebox { overflow-x:auto; background:#fff; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  table { border-collapse: collapse; width:100%; font-size:.8rem; }
  th, td { padding:6px 8px; text-align:left; border-bottom:1px solid #eceef1; }
  th { background:#f0f2f5; position:sticky; top:0; white-space:nowrap; }
  td.num, th.num { text-align:right; font-variant-numeric: tabular-nums; white-space:nowrap; }
  td.user { max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  td.seat, td.judge { white-space:nowrap; }
  .conf { color:#9aa3ad; font-size:.7rem; margin-left:4px; }
  .badge { display:inline-block; border-radius:999px; padding:2px 8px; font-size:.72rem; }
  .b-change { background:#e8f7ee; color:var(--ok); font-weight:700; }
  .b-watch { background:#fdf3e0; color:#b7791f; }
  .b-keep { background:#eef0f3; color:#6b7280; }
  .b-unknown { background:#fbe9e9; color:var(--warn); }
  .seat-standard { color:var(--std); } .seat-premium { color:var(--prem); }
  .seat-unassigned, .seat-unknown { color:#9aa3ad; }
  .bar { display:flex; align-items:center; gap:8px; margin:3px 0; }
  .bar .name { width:220px; font-size:.78rem; overflow:hidden; text-overflow:ellipsis; }
  .bar .track { flex:1; background:#eceef1; border-radius:4px; height:16px; position:relative; }
  .bar .fill { height:100%; border-radius:4px; }
  .bar .val { width:80px; text-align:right; font-size:.78rem; font-variant-numeric:tabular-nums; }
  .cap { color:var(--warn); font-weight:700; }
  .note { font-size:.8rem; color:#6b7280; line-height:1.7; }
</style>
</head>
<body><div class="wrap">
<h1>Claude Team シート最適化ダッシュボード <small>{{ scope }}</small></h1>

<div class="cards">
  <div class="card"><div class="v">{{ s.n_members }}</div><div class="l">メンバー（Std {{ s.n_standard }} / Prem {{ s.n_premium }}{% if s.n_unassigned %} / 未割当 {{ s.n_unassigned }}{% endif %}）</div></div>
  <div class="card"><div class="v">${{ '%.0f' % s.seat_cost_now_usd }}</div><div class="l">現在のシート費用 /月</div></div>
  <div class="card"><div class="v">${{ '%.0f' % s.total_api_cost_usd }}</div><div class="l">API換算利用額 /月</div></div>
  <div class="card hl"><div class="v">${{ '%.0f' % s.est_monthly_saving_usd }}</div><div class="l">削減見込み /月（変更推奨 {{ s.n_change_recommended }} 名）</div></div>
</div>

<h2>ユーザ別 API 換算コスト</h2>
<div class="card">
{% for u in users_sorted %}
  <div class="bar">
    <div class="name" title="{{ u.email }}">{{ u.email.split('@')[0] }}</div>
    <div class="track"><div class="fill" style="width: {{ '%.1f' % (u.api_cost_usd / max_cost * 100) }}%; background: {{ 'var(--prem)' if u.current_seat == 'premium' else ('#9aa3ad' if u.current_seat in ('unassigned', 'unknown') else 'var(--std)') }};"></div></div>
    <div class="val">${{ '%.2f' % u.api_cost_usd }}{% if u.cap_suspected %}<span class="cap"> ⚠</span>{% endif %}</div>
  </div>
{% endfor %}
  <div class="note">棒の色: <span class="seat-standard">■ Standard</span> / <span class="seat-premium">■ Premium</span>　⚠ = 上限到達疑い</div>
</div>

<h2>推奨一覧</h2>
<div class="tablebox"><table>
<tr><th>ユーザ</th><th>シート（現→推奨）</th><th class="num">API換算需要</th><th class="num">実課金</th><th class="num">Std時</th><th class="num">Prem時</th><th class="num">削減/月</th><th>判定</th></tr>
{% for u in users_sorted %}
<tr>
  <td class="user" title="{{ u.email }}">{{ u.email.split('@')[0] }}</td>
  <td class="seat">
    <span class="seat-{{ u.current_seat }}">{{ seat_short.get(u.current_seat, '?') }}</span>
    {%- if u.recommended_seat != u.current_seat %} → <span class="seat-{{ u.recommended_seat }}"><b>{{ seat_short.get(u.recommended_seat, '?') }}</b></span>{% endif %}
  </td>
  <td class="num">{{ u.api_cost_fmt }}{% if u.cap_suspected %} <span class="cap">⚠</span>{% endif %}</td>
  <td class="num">{{ u.billed_fmt }}</td>
  <td class="num">{{ u.std_fmt }}</td>
  <td class="num">{{ u.prem_fmt }}</td>
  <td class="num">{{ u.saving_fmt }}</td>
  <td class="judge"><span class="badge {{ 'b-change' if u.status == '変更推奨' else ('b-watch' if u.status.startswith('要観察') else ('b-unknown' if u.status == 'シート不明' else 'b-keep')) }}">{{ u.status }}</span>{% if u.confidence != '—' %}<span class="conf">確度{{ u.confidence }}</span>{% endif %}</td>
</tr>
{% endfor %}
</table></div>

{% for grp in group_summaries %}
<h2>{{ grp.heading }}</h2>
<div class="tablebox"><table>
<tr><th>{{ grp.col_label }}</th><th class="num">人数</th><th class="num">シート費用</th><th class="num">API換算需要</th><th class="num">変更推奨</th></tr>
{% for t in grp.rows %}
<tr>
  <td>{{ t.group }}</td>
  <td class="num">{{ t.n_fmt }}</td>
  <td class="num">{{ t.seat_cost_fmt }}</td>
  <td class="num">{{ t.api_fmt }}</td>
  <td class="num">{{ t.n_change_fmt }}</td>
</tr>
{% endfor %}
</table></div>
{% endfor %}

<h2>前提と注意</h2>
<div class="card note">
  <ul>
    <li>シート単価: Standard $25 / Premium $125（月払い）。損益分岐の基準差額 $100/月。「Std時 / Prem時」列の Std/Prem は Standard/Premium の略。</li>
    <li>「Std時 / Prem時」= そのシートの場合の想定月額。現シート側はシート料+実課金の観測実績、変更先側は込み利用量（推定値）モデルの試算。</li>
    <li>込み利用量は非公開のため low/mid/high 3シナリオの感度分析付き（判定横の「確度」）。</li>
    <li>⚠ = 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ（上限到達の可能性）。</li>
    <li>判定に使用した月: {{ s.months_used | join(', ') }}（{{ s.hysteresis_months }}ヶ月ヒステリシス）。</li>
  </ul>
</div>

</div></body></html>
""")


def _fmt_compact(v) -> str:
    """テーブル幅節約のため $100 以上は整数、未満はセント表示。"""
    if v is None or v != v:
        return "—"
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def write_html(result: AnalysisResult, path: Path) -> None:
    users = result.users.copy()
    users["_order"] = users["status"].map(
        {st: i for i, st in enumerate(STATUS_ORDER)}
    ).fillna(len(STATUS_ORDER))
    users_sorted = users.sort_values(
        ["_order", "api_cost_usd"], ascending=[True, False]
    ).to_dict("records")
    for u in users_sorted:
        u["api_cost_fmt"] = _fmt_compact(u["api_cost_usd"])
        u["billed_fmt"] = _fmt_compact(u.get("billed_extra_usd", 0.0))
        u["std_fmt"] = _fmt_compact(u["cost_if_standard_usd"])
        u["prem_fmt"] = _fmt_compact(u["cost_if_premium_usd"])
        u["saving_fmt"] = _fmt_compact(u.get("monthly_saving_usd"))
    max_cost = max((u["api_cost_usd"] for u in users_sorted), default=0) or 1.0
    # 部署別 → チーム別の順で、データがある軸のみサマリ表を出す
    group_summaries = []
    for col, heading in (("department", "部署別サマリ"), ("team", "チーム別サマリ")):
        rows = _group_summary_rows(result.users, result.summary, col)
        if not rows:
            continue
        for t in rows:
            t["seat_cost_fmt"] = _fmt_compact(t["seat_cost"])
            t["api_fmt"] = _fmt_compact(t["api"])
            t["n_fmt"] = _fmt_count(t["n"])
            t["n_change_fmt"] = _fmt_count(t["n_change"])
        group_summaries.append({
            "heading": heading,
            "col_label": heading.replace("別サマリ", ""),
            "rows": rows,
        })
    html = _HTML_TEMPLATE.render(
        scope=_scope_label(result),
        s=result.summary,
        users_sorted=users_sorted,
        group_summaries=group_summaries,
        max_cost=max_cost,
        seat_labels=SEAT_LABELS,
        seat_short={"standard": "Standard", "premium": "Premium",
                    "unassigned": "未割当", "unknown": "不明"},
    )
    path.write_text(html, encoding="utf-8")


def summary_json(result: AnalysisResult) -> str:
    return json.dumps(result.summary, ensure_ascii=False, indent=2)


def write_preview(result: PreviewResult, output_dir: str | Path) -> Path:
    """速報モードの出力（reports/<組織>/<月>/preview.md のみ。正式レポートには触れない）。"""
    out = Path(output_dir) / result.month
    out.mkdir(parents=True, exist_ok=True)
    path = out / "preview.md"
    s = result.summary

    users = result.users.copy()
    users["_order"] = users["label"].map(
        {lb: i for i, lb in enumerate(PREVIEW_ORDER)}
    ).fillna(len(PREVIEW_ORDER))
    users = users.sort_values(["_order", "api_cost_projected_usd"], ascending=[True, False])

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

    counts = s["label_counts"]
    count_line = " / ".join(
        f"{lb} {counts[lb]} 名" for lb in PREVIEW_ORDER if counts.get(lb)
    ) or "対象なし"
    factor = result.days_in_month / result.days_observed

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

## 一次判断テーブル

{table}
{(nl + notes_block) if notes_block else ''}
- 一次判断: 月末ペース換算需要を損益分岐モデル（allowance 3シナリオ）にかけた参考判定。
  境界付近（3シナリオ不一致 or 削減見込みがバッファ未満）は「判断保留」に倒しています
- 遊休候補: 観測期間中の利用がほぼゼロ。解約前にオンボーディング状況のヒアリングを推奨
- ⚠️超過済: Premium の込み量を観測期間中にすでに超過し実課金が発生（明確なヘビー層）
- ⚠️従量あり: Standard 等で従量課金が発生（Premium 検討の重要シグナル）
- 対象外（未割当）: 意図的にシートを割り当てていないメンバー（別組織でアサイン済み・管理者等）

## 注意事項

- 日割り換算（×{factor:.1f}）は利用の偏り（曜日・導入直後の立ち上がり・プロジェクト山谷）を補正しません
- 実課金は込み量を使い切ってから発生する非線形な値のため、月末ペース換算していません
- 変更推奨・ヒステリシス判定は行いません。確定判断は全月データ2ヶ月分での正式分析（`analyze`）で行ってください

## データ検証・警告

{chr(10).join(f'- {w}' for w in result.warnings) if result.warnings else '- なし'}

## 考察

<!-- /seat-analysis 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis preview <日数>` を実行すると考察が追記されます）
"""
    md = _preserve_discussion(md, path)
    path.write_text(md, encoding="utf-8")
    return path


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
