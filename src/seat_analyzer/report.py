"""レポート生成: report.md / dashboard.html / recommendations.csv"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from jinja2 import Template

from .analyze import SEAT_LABELS, AnalysisResult

STATUS_ORDER = ["変更推奨", "要観察", "要観察（データ蓄積待ち）", "シート不明", "現状維持"]


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


def write_csv(result: AnalysisResult, path: Path) -> None:
    result.users.to_csv(path, index=False, encoding="utf-8-sig")


def _fmt_usd(v) -> str:
    if v is None or v != v:
        return "—"
    return f"${v:,.2f}"


def _org_products(summary: dict) -> str:
    by_product = summary.get("org_service_by_product") or {}
    if not by_product:
        return ""
    detail = " / ".join(f"{k} {_fmt_usd(v)}" for k, v in
                        sorted(by_product.items(), key=lambda kv: -kv[1]))
    return f"（{detail}）"


def _user_table_md(users: pd.DataFrame) -> str:
    has_cc = "prs_with_cc" in users.columns
    has_loc = "loc_with_cc" in users.columns
    header = (
        "| ユーザ | 現シート | API換算需要 | 実課金(従量) | Standard時 | Premium時 | 推奨 | 削減/月 | 判定 | 確度 |"
        + (" PR(CC) |" if has_cc else "") + (" 行数(CC) |" if has_loc else "")
    )
    sep = "|" + "---|" * (10 + int(has_cc) + int(has_loc))
    lines = [header, sep]
    for _, r in users.iterrows():
        flag = " ⚠️上限?" if r["cap_suspected"] else ""
        cells = [
            r["email"],
            SEAT_LABELS.get(r["current_seat"], r["current_seat"]),
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
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(lines)


def write_markdown(result: AnalysisResult, path: Path) -> None:
    s = result.summary
    users = result.users.copy()
    users["_order"] = users["status"].map(
        {st: i for i, st in enumerate(STATUS_ORDER)}
    ).fillna(len(STATUS_ORDER))
    users = users.sort_values(["_order", "monthly_saving_usd"], ascending=[True, False])

    changes = users[users["status"] == "変更推奨"]
    sensitivity_disagree = users[users["confidence"] != "高"]

    md = f"""# Claude Team シート最適化レポート — {result.month}

## サマリ

| 指標 | 値 |
|---|---|
| 対象メンバー数 | {s['n_members']} 名（Standard {s['n_standard']} / Premium {s['n_premium']} / 不明 {s['n_unknown']}） |
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


_HTML_TEMPLATE = Template(r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Team シート最適化 — {{ month }}</title>
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
<h1>Claude Team シート最適化ダッシュボード <small>{{ month }}</small></h1>

<div class="cards">
  <div class="card"><div class="v">{{ s.n_members }}</div><div class="l">メンバー（Std {{ s.n_standard }} / Prem {{ s.n_premium }}）</div></div>
  <div class="card"><div class="v">${{ '%.0f' % s.seat_cost_now_usd }}</div><div class="l">現在のシート費用 /月</div></div>
  <div class="card"><div class="v">${{ '%.0f' % s.total_api_cost_usd }}</div><div class="l">API換算利用額 /月</div></div>
  <div class="card hl"><div class="v">${{ '%.0f' % s.est_monthly_saving_usd }}</div><div class="l">削減見込み /月（変更推奨 {{ s.n_change_recommended }} 名）</div></div>
</div>

<h2>ユーザ別 API 換算コスト</h2>
<div class="card">
{% for u in users_sorted %}
  <div class="bar">
    <div class="name" title="{{ u.email }}">{{ u.email.split('@')[0] }}</div>
    <div class="track"><div class="fill" style="width: {{ '%.1f' % (u.api_cost_usd / max_cost * 100) }}%; background: {{ 'var(--prem)' if u.current_seat == 'premium' else 'var(--std)' }};"></div></div>
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
  <td class="judge"><span class="badge {{ 'b-change' if u.status == '変更推奨' else ('b-watch' if u.status.startswith('要観察') else ('b-unknown' if u.status == 'シート不明' else 'b-keep')) }}">{{ u.status }}</span><span class="conf">確度{{ u.confidence }}</span></td>
</tr>
{% endfor %}
</table></div>

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
    html = _HTML_TEMPLATE.render(
        month=result.month,
        s=result.summary,
        users_sorted=users_sorted,
        max_cost=max_cost,
        seat_labels=SEAT_LABELS,
        seat_short={"standard": "Standard", "premium": "Premium", "unknown": "不明"},
    )
    path.write_text(html, encoding="utf-8")


def summary_json(result: AnalysisResult) -> str:
    return json.dumps(result.summary, ensure_ascii=False, indent=2)
