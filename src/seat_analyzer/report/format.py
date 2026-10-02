"""レポート共通の書式・整列ユーティリティ（金額・トークン数・並べ替え・集計行）。"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from ..analyze import STATUS_CHANGE, AnalysisResult, OrgAnalysisResult
from ..ingest import parse_affiliations


def _md_cell(v) -> str:
    """Markdown 表セル用のエスケープ（表崩れ防止）。パイプ・改行が主な対象。"""
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _is_missing(v) -> bool:
    """欠損か（None・NaN・pd.NA）。文字列や配列は欠損として扱わない。"""
    if v is None:
        return True
    if isinstance(v, str):
        return False
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _text_value(v) -> str:
    """属性列（部署・チーム等）の表示文字列。欠損と空は空文字にする。

    複数 workspace のアカウントを連結した表では、片方の workspace にしか無い列が
    欠損になる。欠損をそのまま str にすると "nan" と出るため、ここで空にそろえる。
    """
    return "" if _is_missing(v) else str(v or "")


def _int_cell(v, *, thousands: bool = False) -> str:
    """整数のセル。欠損（その workspace に観測が無い）は「—」にする。"""
    if _is_missing(v):
        return "—"
    return f"{int(v):,}" if thousands else str(int(v))


def _labeled(heading: str, label: str | None) -> str:
    """見出しの末尾に workspace の表示名を添える（None なら見出しのまま）。"""
    return heading if label is None else f"{heading}（{label}）"


def _sole_result(org: OrgAnalysisResult) -> AnalysisResult:
    """workspace が1つの組織の、唯一の分析結果。

    複数 workspace の形を取らない組織（従来レイアウト・workspaces が1つ）の成果物は、
    この結果だけから従来どおりに組み立てる（列・節・表を一切足さない）。
    """
    if len(org.workspaces) != 1:
        raise ValueError(
            f"組織 {org.org} の分析結果がありません（分析できた workspace が"
            f" {len(org.workspaces)} 個）"
        )
    return next(iter(org.workspaces.values()))


def _account_rows(org: OrgAnalysisResult,
                  frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """workspace ごとのアカウント行を主→副の順に縦へ連結する。

    frames は workspace 名 → その workspace の行（users・自身の需要の users 等）。
    各行に識別子の workspace（ディレクトリ名）と表示名の workspace_label を足し、
    workspace は email の直後、workspace_label はその次に置く。並びは
    org.workspaces の順（主が先）で、frames の挿入順には依らない。

    片方の workspace にしか無い任意列（code-analytics・追加クレジット由来）は欠損になる。
    整数の列は欠損を持てる型（Int64）に戻し、"12.0" のような表示にしない。
    """
    parts = []
    integer_columns: dict[str, bool] = {}
    for name in org.workspaces:
        frame = frames.get(name)
        if frame is None:
            continue
        part = frame.copy()
        position = list(part.columns).index("email") + 1 if "email" in part.columns else 0
        part.insert(position, "workspace", name)
        part.insert(position + 1, "workspace_label", org.contexts[name].label)
        for column in part.columns:
            is_integer = pd.api.types.is_integer_dtype(part[column].dtype)
            integer_columns[column] = integer_columns.get(column, True) and is_integer
        parts.append(part)
    if not parts:
        return pd.DataFrame(columns=["email", "workspace", "workspace_label"])
    combined = pd.concat(parts, ignore_index=True, sort=False)
    for column, is_integer in integer_columns.items():
        if is_integer and not pd.api.types.is_integer_dtype(combined[column].dtype):
            combined[column] = combined[column].astype("Int64")
    return combined


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


def _scope_label(result: AnalysisResult) -> str:
    """レポートタイトル用の対象表記（「組織 — 月」）。"""
    return f"{result.org} — {result.month}"


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
    「（未設定）」へ重み1で計上する。API換算需要の降順、（未設定）は常に最後。需要が
    同額のグループはグループ名の昇順（入力の行順で並びが変わらないようにする）。

    include_unset=False のとき「（未設定）」行を除外する（例: チーム別サマリでは、
    チーム未設定のユーザは部署も異なる異質な集合のためまとめても意味がない）。
    この場合、縦合計は全体と一致しなくなる（当該軸に所属を持つユーザのみの集計になる）。

    シート費は、行が seat_cost_usd を持つならその値（人の行は複数アカウントの
    シート料の合計）、無ければ現シートの価格を使う。人の表を渡せば、複数 workspace の
    組織でも人数がアカウント数ではなく人数として数えられる。
    """
    if not _has_values(users, col):
        return []
    has_loc = "loc_with_cc" in users.columns
    has_seat_cost = "seat_cost_usd" in users.columns
    # グループ名 → 集計値の accumulator（初期化順は問わない。最後に並べ替える）
    acc: dict[str, dict] = {}
    for _, r in users.iterrows():
        groups = parse_affiliations(r.get(col)) or ["（未設定）"]
        w = 1.0 / len(groups)
        is_change = r["status"] == STATUS_CHANGE
        seat_price = (float(r["seat_cost_usd"]) if has_seat_cost
                      else _seat_price(r["current_seat"], summary))
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
    rows.sort(key=lambda r: (r["is_unset"], -r["api"], r["group"]))
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


def _fmt_stat_count(v) -> str:
    """分布表のトークン・LoC・回数（7.4e9 → 7.42B、1.2e6 → 1.23M、25e3 → 25.0K）。

    単位の刻みは詳細利用状況の input/output（_fmt_tokens）と揃える。B が無いと
    十億単位のトークンが 7420.58M のような桁で並び、列が読めなくなる。

    桁は _fmt_tokens より1つ多く残す。統計量は同じ列に平均・中央値・分位点が並び、
    1.99B と 2.04B が同じ 2.0B に潰れると分布の広がりが読めなくなるため。詳細利用
    状況の LoC の桁区切り整数はそのまま（あちらは1ユーザ1行で比較の対象が縦に並ばない）。
    """
    n = float(v or 0)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e4:
        return f"{n / 1e3:.1f}K"
    return f"{n:,.0f}"


def _detail_rows(users: pd.DataFrame,
                 cost_col: str = "api_cost_usd") -> tuple[list[dict], bool]:
    """詳細利用状況テーブルの行データ。input+output トークンの降順で返す。

    cost_col は需要の列名。正式分析は月額の api_cost_usd、速報は観測実績の
    api_cost_observed_usd を渡す（速報は月末ペース換算せず観測値のまま並べる）。

    LoC 列はあるがその行に観測が無い（複数 workspace を連結して片方にだけ
    code-analytics がある）場合、loc は None になる。表示側は「—」にする。
    workspace_label 列があれば各行の space に表示名を入れる（無ければ None）。
    """
    u = users.copy()
    u["_in"] = u["prompt_tokens"].fillna(0)
    u["_out"] = u["completion_tokens"].fillna(0)
    u["_total"] = u["_in"] + u["_out"]
    # email をタイブレークに置き、トークン数が同点のユーザでも行順が一意に決まるようにする
    # （単一列の sort_values は安定ソートではなく、同点行の並びが実行環境で変わりうる）
    u = u.sort_values(["_total", "email"], ascending=[False, True])
    has_loc = "loc_with_cc" in u.columns
    rows = []
    for _, r in u.iterrows():
        api = r[cost_col]
        rows.append({
            "email": r["email"],
            "in": int(r["_in"]),
            "out": int(r["_out"]),
            "api": float(api) if not pd.isna(api) else 0.0,  # NaN は 0 扱い
            "models": str(r["model_breakdown"] or ""),
            "products": str(r["product_breakdown"] or ""),
            "loc": (int(r["loc_with_cc"])
                    if has_loc and not _is_missing(r["loc_with_cc"]) else None),
            "space": (str(r["workspace_label"])
                      if "workspace_label" in u.columns else None),
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


def _fmt_setting_usd(v: float) -> str:
    """設定値の金額表示（整数なら整数・小数なら2桁）。

    しきい値・上限のような設定値は、その額との比較の意味を持つ。_fmt_compact の
    「$100 以上は整数」で丸めると、$100.49 のようなしきい値が凡例で $100 になり、
    実際の判定と食い違う文章になるため、セント単位まで保つ（セント未満の桁は他の
    金額表示と同じく表示せず、設定もその粒度を想定する）。
    """
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"
