"""ユーザ×月の集計と、シート損益分岐判定（ヒステリシス・感度分析・センサリング）。"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import ingest, pricing

# モデル名を表示用に短縮する（claude-opus-4-8 → Opus 4.8, claude-fable-5 → Fable 5）
_MODEL_SHORT_RE = re.compile(r"(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?", re.I)


def _short_model(name: str) -> str:
    """API モデル名を表示用の短縮形にする。判別できない場合は元の文字列を返す。"""
    m = _MODEL_SHORT_RE.search(str(name))
    if not m:
        return str(name)
    family = m.group(1).capitalize()
    version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"{family} {version}"

SCENARIOS = ("low", "mid", "high")
SEAT_LABELS = {"standard": "Standard", "premium": "Premium",
               "unassigned": "未割当", "unknown": "不明"}

# 判定ステータス文字列（report.py の表示順・バッジ分岐と結合。値は変更しないこと）。
STATUS_CHANGE = "変更推奨"
STATUS_WATCH = "要観察"
STATUS_WATCH_WAIT = "要観察（データ蓄積待ち）"
STATUS_KEEP = "現状維持"
STATUS_UNKNOWN = "シート不明"
STATUS_EXCLUDED = "対象外（シート未割当）"

# 速報モードの一次判断ラベル（report.py の表示順・バッジ分岐と結合。値は変更しないこと）。
LABEL_IDLE = "遊休候補"
LABEL_STD_CAND = "Standard候補"
LABEL_PREM_CONSIDER = "Premium検討"
LABEL_HOLD = "判断保留"
LABEL_PREM_OK = "Premium妥当"
LABEL_STD_OK = "Standard妥当"
LABEL_EXCLUDED = "対象外（未割当）"

# 速報モード: 観測需要がこの額 [USD] 未満なら「遊休候補」（数日〜半月でこの水準は実質未使用）
PREVIEW_IDLE_OBS_USD = 1.0


@dataclass
class AnalysisResult:
    month: str
    users: pd.DataFrame
    summary: dict
    org: str | None = None  # 組織名（input/<org>/ レイアウト時。旧レイアウトは None）
    warnings: list[str] = field(default_factory=list)
    months_used: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    # 前月からの変化・月次推移（初月は None）。report が「## 前月からの変化」を描画する
    trend: dict | None = None
    # 月中の利用推移（同一月の複数スナップショット差分。1つ以下なら None）
    snapshot: dict | None = None
    # 月中の Claude Code 活動（code-analytics スナップショット差分。1つ以下なら None）
    code_diff: dict | None = None
    # 月中のメンバー変動（members 単日スナップショット差分。1つ以下なら None）
    member_changes: dict | None = None


def _seat_cost(api_cost: float, seat: str, scenario: str, cfg: dict) -> float:
    seat_cfg = cfg["seats"][seat]
    allowance = float(seat_cfg["allowance_usd"][scenario])
    return float(seat_cfg["price_usd"]) + max(0.0, api_cost - allowance)


def _recommend(api_cost: float, scenario: str, cfg: dict) -> tuple[str, float, float]:
    cost_std = _seat_cost(api_cost, "standard", scenario, cfg)
    cost_prem = _seat_cost(api_cost, "premium", scenario, cfg)
    rec = "standard" if cost_std <= cost_prem else "premium"
    return rec, cost_std, cost_prem


def aggregate_month(spend_df: pd.DataFrame) -> pd.DataFrame:
    """スペンド明細（apply_cost_basis 適用済み・billed_usd 必須）→ ユーザ単位の月次集計。"""
    agg_spec = {
        "api_cost": ("cost_usd", "sum"),
        "prompt_tokens": ("prompt_tokens", "sum"),
        "completion_tokens": ("completion_tokens", "sum"),
        "billed": ("billed_usd", "sum"),
    }
    grouped = spend_df.groupby("email").agg(**agg_spec)

    if "product" in spend_df.columns:
        # product 構成比は「利用回数（リクエスト数）」基準。Cowork/Chat は API コストが
        # 小さくコスト基準だと埋もれるため、回数で見えるようにする。requests が無ければ
        # 明細行数で代替する
        weight_col = "requests" if "requests" in spend_df.columns else None
        tmp = spend_df.assign(_pw=spend_df[weight_col].fillna(0) if weight_col else 1.0)

        def product_bd(g: pd.DataFrame) -> str:
            by_product = g.groupby("product")["_pw"].sum().sort_values(ascending=False)
            total = by_product.sum()
            if total <= 0:
                return ""
            return " / ".join(f"{p} {v / total:.0%}" for p, v in by_product.items() if v / total >= 0.01)

        grouped["product_breakdown"] = (
            tmp.groupby("email")[["product", "_pw"]].apply(product_bd)
        )

    # model は load_spend の必須カラムのため常に存在する
    tmp = spend_df.assign(
        _tok=spend_df["prompt_tokens"].fillna(0) + spend_df["completion_tokens"].fillna(0)
    )

    def model_bd(g: pd.DataFrame) -> str:
        # モデル利用割合はトークン量（input+output）基準。寄与降順・1%未満は集約
        by_model = g.groupby("model")["_tok"].sum().sort_values(ascending=False)
        total = by_model.sum()
        if total <= 0:
            return ""
        return " / ".join(
            f"{_short_model(m)} {v / total:.0%}"
            for m, v in by_model.items() if v / total >= 0.01
        )

    grouped["model_breakdown"] = (
        tmp.groupby("email")[["model", "_tok"]].apply(model_bd)
    )
    return grouped.reset_index()


def _prev_calendar_month(month: str) -> str:
    """YYYY-MM の暦上の直前月を返す（欠月判定用）。"""
    year, mon = (int(x) for x in month.split("-"))
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


def _trend_thresholds(cfg: dict) -> dict:
    """「前月からの変化」の表示閾値（config.yaml > trend。無くてもデフォルトで動く）。"""
    t = cfg.get("trend") or {}
    return {
        "idle_usd": float(t.get("idle_usd", 1.0)),
        "min_activity_usd": float(t.get("min_activity_usd", 10.0)),
        "change_min_usd": float(t.get("change_min_usd", 50.0)),
        "top_changes": int(t.get("top_changes", 5)),
    }


def _compute_trend(monthly: dict[str, pd.DataFrame], months_used: list[str],
                   member_emails: set[str], cfg: dict) -> dict | None:
    """前月（欠月は飛ばした直前の存在月）との比較と月次推移を計算する。

    monthly はユーザ別月次集計（api_cost / billed）。追加のストレージは持たず、
    ロード済みデータから毎回計算する（input/ の CSV が恒久アーカイブという前提）。
    直前の存在月が無い初月は None（report 側でセクションを出さない）。
    """
    if len(months_used) < 2:
        return None
    th = _trend_thresholds(cfg)
    month, prev = months_used[-1], months_used[-2]
    m_df = monthly[month].set_index("email")
    p_df = monthly[prev].set_index("email")
    emails = sorted(set(m_df.index) | set(p_df.index) | set(member_emails))

    def _val(df: pd.DataFrame, email: str, col: str) -> float:
        return float(df.loc[email, col]) if email in df.index else 0.0

    started, stopped, new_billed, changes = [], [], [], []
    for email in emails:
        d_m, d_p = _val(m_df, email, "api_cost"), _val(p_df, email, "api_cost")
        b_m, b_p = _val(m_df, email, "billed"), _val(p_df, email, "billed")
        is_started = d_p < th["idle_usd"] and d_m >= th["min_activity_usd"]
        is_stopped = d_p >= th["min_activity_usd"] and d_m < th["idle_usd"]
        if is_started:
            started.append({"email": email, "amount": round(d_m, 2)})
        if is_stopped:
            stopped.append({"email": email, "amount": round(d_p, 2)})
        if b_p <= 0.0 and b_m > 0.0:
            new_billed.append({"email": email, "amount": round(b_m, 2)})
        # 利用開始/停止は主な増減に重複掲載しない（別項目で列挙済み）
        if not is_started and not is_stopped and abs(d_m - d_p) >= th["change_min_usd"]:
            changes.append({"email": email, "prev": round(d_p, 2),
                            "curr": round(d_m, 2), "delta": round(d_m - d_p, 2)})
    started.sort(key=lambda x: -x["amount"])
    stopped.sort(key=lambda x: -x["amount"])
    new_billed.sort(key=lambda x: -x["amount"])
    changes.sort(key=lambda c: -abs(c["delta"]))

    # 月次推移は直近6ヶ月まで（アクティブ = 需要が idle_usd 以上のユーザ数）
    series = []
    for m in months_used[-6:]:
        df = monthly[m]
        series.append({
            "month": m,
            "api": round(float(df["api_cost"].sum()), 2),
            "billed": round(float(df["billed"].sum()), 2),
            "active": int((df["api_cost"] >= th["idle_usd"]).sum()),
        })

    return {
        "compare_month": prev,
        "gap_skipped": prev != _prev_calendar_month(month),
        "started": started,
        "stopped": stopped,
        "new_billed": new_billed,
        "changes": changes[: th["top_changes"]],
        "series": series,
    }


def _snapshot_thresholds(cfg: dict) -> dict:
    """スナップショット差分の閾値（config.yaml > snapshot_diff。無くてもデフォルトで動く）。"""
    s = cfg.get("snapshot_diff") or {}
    return {
        "stall_max_delta_usd": float(s.get("stall_max_delta_usd", 1.0)),
        "min_cumulative_usd": float(s.get("min_cumulative_usd", 10.0)),
        "min_interval_days": int(s.get("min_interval_days", 3)),
    }


def _compute_snapshot_diff(input_dir: Path, month: str, cfg: dict,
                           seat_by_email: dict) -> tuple[dict | None, list[str]]:
    """同一月の月初開始スナップショット（2つ以上）の差分から月中推移・停止を検出する。

    需要基準は computed（tokens×単価）固定。区間増分が止まった Standard ユーザや、
    込み量を消化して実課金が発生したユーザを、allowance 実測の材料として抽出する。
    スナップショットが1つ以下なら (None, 除外警告) を返す（既存出力と同一）。
    """
    entries, excluded = ingest.spend_snapshots(input_dir, month)
    warnings = [f"{name}: 月初開始でないため差分分析から除外" for name in excluded] \
        if len(entries) >= 2 else []
    if len(entries) < 2:
        return None, warnings

    th = _snapshot_thresholds(cfg)
    snaps = []
    for period, path in entries:
        df = pricing.add_computed_cost(ingest.load_spend_file(path, month, cfg), cfg)
        u = df[df["email"].str.contains("@", na=False)]
        cum = {e: float(v) for e, v in u.groupby("email")["computed_cost_usd"].sum().items()}
        if "net_spend" in u.columns:
            net = u.assign(_n=u["net_spend"].fillna(0.0)).groupby("email")["_n"].sum()
            billed = {e: float(v) for e, v in net.items()}
        else:
            billed = {}
        snaps.append({"label": f"〜{period.end:%m-%d}", "days": period.days,
                      "end": period.end, "cum": cum, "billed": billed})

    labels = [s["label"] for s in snaps]
    emails = sorted({e for s in snaps for e in s["cum"]})
    latest_interval_days = (snaps[-1]["end"] - snaps[-2]["end"]).days
    judged = latest_interval_days >= th["min_interval_days"]

    rows, decreased = [], False
    for email in emails:
        cums = [s["cum"].get(email, 0.0) for s in snaps]
        latest_delta = cums[-1] - cums[-2]
        if latest_delta < -0.01:
            decreased = True
        stall = (judged and latest_delta < th["stall_max_delta_usd"]
                 and cums[-1] >= th["min_cumulative_usd"])
        rows.append({
            "email": email,
            "cum": [round(c, 2) for c in cums],
            "latest_delta": round(latest_delta, 2),
            "stall": stall,
            "seat": seat_by_email.get(email, "unknown"),
            "billed_latest": round(snaps[-1]["billed"].get(email, 0.0), 2),
        })
    rows.sort(key=lambda r: -r["cum"][-1])
    if decreased:
        warnings.append("累積需要が減少しています（ファイルの取り違えの可能性）")

    # 停止疑い ∩ Standard ∩ 実課金ゼロ: 停止時点の累積が実効込み量の実測候補
    stalled_capped = [
        {"email": r["email"], "cum_at_stall": r["cum"][-1]}
        for r in rows if r["stall"] and r["seat"] == "standard" and r["billed_latest"] <= 0.0
    ]

    # 実課金が 0→正 に転じた最初の区間（実効込み量の消化ポイント）
    billed_emerged = []
    for email in emails:
        bills = [s["billed"].get(email, 0.0) for s in snaps]
        cums = [s["cum"].get(email, 0.0) for s in snaps]
        for i in range(1, len(snaps)):
            if bills[i - 1] <= 0.0 and bills[i] > 0.0:
                billed_emerged.append({
                    "email": email,
                    "interval_label": f"{snaps[i - 1]['label']}→{snaps[i]['label']}",
                    "prev_cum": round(cums[i - 1], 2),
                    "curr_cum": round(cums[i], 2),
                    "billed": round(bills[i], 2),
                })
                break

    snapshot = {
        "labels": labels,
        "snaps": [{"label": s["label"], "days": s["days"]} for s in snaps],
        "latest_interval_days": latest_interval_days,
        "judged": judged,
        "min_interval_days": th["min_interval_days"],
        "rows": rows,
        "stalled_capped": stalled_capped,
        "billed_emerged": billed_emerged,
    }
    return snapshot, warnings


def _compute_member_changes(input_dir: Path, month: str, cfg: dict) -> tuple[dict | None, list[str]]:
    """対象月の単日スナップショット members（2つ以上）の隣接差分から月中の変動を検出する。

    シート変更・追加・削除を時系列順に列挙する。変動が1件も無くてもセクションは出す
    （スナップショットを取って変動が無かったこと自体に情報価値があるため）。
    スナップショットが1つ以下なら (None, []) を返す（既存出力と同一）。

    判定ロジック・ヒステリシスには一切影響しない表示専用の情報。シート変更が1件以上
    あれば、当月判定は最新スナップショット時点のシートで行う旨の参考警告を返す。
    """
    entries = ingest.member_snapshots(input_dir, month)
    if len(entries) < 2:
        return None, []

    snaps = []
    for period, path in entries:
        df = ingest.load_members_file(path, cfg)
        seat_by = {e: s for e, s in zip(df["email"], df["seat_type"], strict=False)}
        snaps.append({"label": f"{period.start:%m-%d}", "seat_by": seat_by})

    seat_changes, joined, left = [], [], []
    for i in range(1, len(snaps)):
        prev, curr = snaps[i - 1], snaps[i]
        interval_label = f"{prev['label']}→{curr['label']}"
        prev_emails, curr_emails = set(prev["seat_by"]), set(curr["seat_by"])
        for email in sorted(prev_emails & curr_emails):
            if prev["seat_by"][email] != curr["seat_by"][email]:
                seat_changes.append({
                    "email": email, "from": prev["seat_by"][email],
                    "to": curr["seat_by"][email], "interval_label": interval_label,
                })
        for email in sorted(curr_emails - prev_emails):
            joined.append({"email": email, "seat": curr["seat_by"][email],
                           "interval_label": interval_label})
        for email in sorted(prev_emails - curr_emails):
            left.append({"email": email, "seat": prev["seat_by"][email],
                         "interval_label": interval_label})

    warnings: list[str] = []
    if seat_changes:
        emails = [c["email"] for c in seat_changes]
        warnings.append(
            f"月中にシート変更を検出した ユーザ {len(emails)} 名: {emails[:5]}"
            "（当月の損益分岐判定は最新スナップショット時点のシートで行うため参考値）"
        )

    return {
        "snaps": [{"label": s["label"]} for s in snaps],
        "seat_changes": seat_changes,
        "joined": joined,
        "left": left,
    }, warnings


def _compute_code_diff(input_dir: Path, month: str, cfg: dict) -> tuple[dict | None, list[str]]:
    """対象月の code-analytics スナップショット（2つ以上）から累積 LoC の月中推移を計算する。

    各時点のユーザ別累積 loc_with_cc（あれば prs_with_cc も）を取り、最新区間の増分を出す。
    全時点で LoC が 0 のユーザは表から省く。スナップショットが1つ以下なら (None, [])。
    表示専用で判定・ヒステリシスには影響しない。
    """
    entries = ingest.code_snapshots(input_dir, month)
    if len(entries) < 2:
        return None, []

    snaps = []
    for period, path in entries:
        df = ingest.load_code_analytics_file(path, month, cfg)
        # 欠損セル（NaN）は 0 として扱う（累積・増分計算で int 化できるように）
        loc = ({e: float(v) for e, v in zip(df["email"], df["loc_with_cc"].fillna(0.0), strict=False)}
               if "loc_with_cc" in df.columns else {})
        prs = ({e: float(v) for e, v in zip(df["email"], df["prs_with_cc"].fillna(0.0), strict=False)}
               if "prs_with_cc" in df.columns else None)
        snaps.append({"label": f"〜{period.end:%m-%d}", "loc": loc, "prs": prs})

    has_prs = all(s["prs"] is not None for s in snaps)
    emails = sorted({e for s in snaps for e in s["loc"]})
    rows = []
    for email in emails:
        loc_cum = [int(round(s["loc"].get(email, 0.0))) for s in snaps]
        if all(c == 0 for c in loc_cum):
            continue   # 全時点で LoC 0 のユーザは省く
        loc_delta = loc_cum[-1] - loc_cum[-2]
        prs_delta = None
        if has_prs:
            prs_cum = [int(round(s["prs"].get(email, 0.0))) for s in snaps]
            prs_delta = prs_cum[-1] - prs_cum[-2]
        rows.append({"email": email, "loc_cum": loc_cum,
                     "loc_delta": loc_delta, "prs_delta": prs_delta})
    rows.sort(key=lambda r: -r["loc_cum"][-1])

    return {
        "labels": [s["label"] for s in snaps],
        "rows": rows,
        "has_prs": has_prs,
    }, []


def _attach_loc_corroboration(snapshot: dict | None, code_diff: dict | None) -> None:
    """spend の停止疑いに、code-analytics の LoC 増分で傍証/食い違いの注記を付ける（email 突合）。

    最新区間の LoC 増分が 0（または code diff に不在）なら「停止の傍証」、正なら
    「利用継続の形跡あり（食い違い）」。spend と code のスナップショット日付は一致していなくてよい。
    どちらかが無ければ何もしない（後方互換）。
    """
    if not snapshot or not code_diff:
        return
    delta_by_email = {r["email"]: r["loc_delta"] for r in code_diff["rows"]}

    def note_for(email: str) -> str:
        delta = delta_by_email.get(email)
        if delta is None or delta <= 0:
            return "LoC 増分も 0（停止の傍証）"
        return f"一方で LoC は +{delta:,} 行 増加（利用継続の形跡あり。スペンドとの食い違いは要確認）"

    for r in snapshot.get("rows", []):
        if r.get("stall"):
            r["loc_note"] = note_for(r["email"])
    for x in snapshot.get("stalled_capped", []):
        x["loc_note"] = note_for(x["email"])


def _merge_members_info(users: pd.DataFrame, input_dir: Path, cfg: dict, sources: dict) -> None:
    """任意ファイル members-info.csv の department/team/role/note を email で users に付与する。

    未登録メンバーは空文字列。members-info にだけ居るメールは行を追加しない。
    ファイルが読めた場合のみ sources["members_info"] にパスを記録する。
    """
    info_result = ingest.load_members_info(input_dir, cfg)
    if info_result is None:
        for col in ("department", "team", "role", "note"):
            users[col] = ""
        return
    sources["members_info"] = str(info_result.source)
    info = info_result.df.set_index("email")
    for col in ("department", "team", "role", "note"):
        users[col] = users["email"].map(info[col]).fillna("") if col in info.columns else ""
    # 部署・チームは兼務（複数所属）を正規化した表示文字列で保持する（集計時に再分割）
    for col in ("department", "team"):
        users[col] = users[col].map(ingest.normalize_affiliations)


def analyze(input_dir: str | Path, month: str, cfg: dict, org: str | None = None) -> AnalysisResult:
    """1組織分の分析。input_dir はその組織の入力ディレクトリ（spend/ 等を直下に持つ）。"""
    input_dir = Path(input_dir)
    warnings: list[str] = []

    # --- 対象月まで（ヒステリシス判定に必要な過去月含む）のスペンドをロード ---
    available = ingest.discover_months(input_dir)
    months_used = [m for m in available if m <= month]
    if month not in available:
        raise FileNotFoundError(
            f"{month} のスペンドレポートがありません。存在する月: {available or 'なし'}"
        )

    # 対象月に月初開始スナップショットが2つ以上あるなら月中推移の差分分析を発動する。
    # 主データの採用は現行どおり（期間の広い方）で、重複警告の文言だけ差し替える。
    snap_entries, _ = ingest.spend_snapshots(input_dir, month)
    snapshot_active = len(snap_entries) >= 2
    # members / code-analytics の月中差分（発動時は重複警告の文言を差し替える）
    member_diff_active = len(ingest.member_snapshots(input_dir, month)) >= 2
    code_diff_active = len(ingest.code_snapshots(input_dir, month)) >= 2

    raw: dict[str, pd.DataFrame] = {}
    sources: dict = {"spend": {}}
    for m in months_used:
        result = ingest.load_spend(
            input_dir, m, cfg, snapshot_active=(snapshot_active and m == month)
        )
        warnings.extend(result.warnings)
        sources["spend"][m] = str(result.source)
        raw[m] = pricing.add_computed_cost(result.df, cfg)
        # ファイル名の期間が全月に満たない場合、月額前提の判定が歪む（過小評価）
        period = ingest.file_period(result.source)
        if period is not None and period.days is not None:
            year, mon = (int(x) for x in m.split("-"))
            if period.days < calendar.monthrange(year, mon)[1]:
                warnings.append(
                    f"{result.source.name}: {m} は部分月データ"
                    f"（{period.start:%m-%d}〜{period.end:%m-%d} の {period.days}日分）ですが"
                    "全月として扱っています。月中の一次判断には --preview を利用してください"
                )

    warnings.extend(_warn_unknown_models(
        pd.concat([df["model"] for df in raw.values()]).unique(), cfg
    ))

    # 需要指標の基準（computed / net_spend）を対象月のユーザ帰属行から決定し、全月に適用
    target_user_rows = raw[month][raw[month]["email"].str.contains("@", na=False)]
    basis, basis_notes = pricing.resolve_cost_basis(target_user_rows, cfg)
    warnings.extend(basis_notes)

    monthly: dict[str, pd.DataFrame] = {}
    org_usage: dict = {}
    for m, df_raw in raw.items():
        df = pricing.apply_cost_basis(df_raw, basis)
        if m == month and basis == "net_spend":
            warnings.extend(pricing.validate_spend(df, cfg))
        # ユーザ非帰属の組織利用（例: "(org service usage)" の Code Review 等）は
        # シート判定の対象外として分離し、別枠で計上する
        is_user = df["email"].str.contains("@", na=False)
        org_df = df[~is_user]
        if m == month and not org_df.empty:
            org_usage = {
                "cost_usd": round(float(org_df["billed_usd"].sum()), 2),
                "by_product": {
                    str(k): round(float(v), 2)
                    for k, v in org_df.groupby("product")["billed_usd"].sum().items()
                } if "product" in org_df.columns else {},
            }
        monthly[m] = aggregate_month(df[is_user])

    members_result = ingest.load_members(input_dir, month, cfg, snapshot_active=member_diff_active)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)

    code_result = ingest.load_code_analytics(input_dir, month, cfg, snapshot_active=code_diff_active)
    if code_result is not None:
        warnings.extend(code_result.warnings)
        sources["code_analytics"] = str(code_result.source)

    # --- 対象月テーブル: members と spend の全ユーザを対象にする（利用ゼロも含む）---
    target = monthly[month].set_index("email")
    emails = sorted(set(members["email"]) | set(target.index))
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    # 前月からの変化・月次推移（ロード済み monthly から毎回計算・初月は None）
    trend = _compute_trend(monthly, months_used, set(members["email"]), cfg)
    # 月中の利用推移（同一月の複数スナップショット差分・1つ以下なら None）
    snapshot, snap_warns = _compute_snapshot_diff(input_dir, month, cfg, seat_by_email)
    warnings.extend(snap_warns)
    # 月中の Claude Code 活動・メンバー変動（スナップショット差分・1つ以下なら None）
    code_diff, code_warns = _compute_code_diff(input_dir, month, cfg)
    warnings.extend(code_warns)
    member_changes, member_warns = _compute_member_changes(input_dir, month, cfg)
    warnings.extend(member_warns)
    # spend の停止疑いに LoC 増分の傍証/食い違いを注記（email 突合・両方揃ったときのみ）
    _attach_loc_corroboration(snapshot, code_diff)

    decision_cfg = cfg["decision"]
    n_hyst = int(decision_cfg["hysteresis_months"])
    buffer_ratio = float(decision_cfg["buffer_ratio"])
    censoring_margin = float(decision_cfg["censoring_margin"])
    seat_diff = float(cfg["seats"]["premium"]["price_usd"]) - float(cfg["seats"]["standard"]["price_usd"])
    min_saving = buffer_ratio * seat_diff
    s_allowance_mid = float(cfg["seats"]["standard"]["allowance_usd"]["mid"])

    def _costs_for(seat: str, api_cost: float, billed: float, scenario: str) -> tuple[str, float, float]:
        """現シートは観測実績（シート料+実課金）、変更先は allowance モデルで試算する。

        従量課金が有効な組織では billed（実課金）が「そのシートでの実コスト」の
        観測値であり、allowance 推定より信頼できる。変更先のコストは観測できない
        ため allowance モデルで試算するが、込み量の大小関係
        （Standard の込み量 ≤ Premium の込み量）から観測値で上下に拘束する:
          - Standard ユーザ → Premium に変えた場合の超過課金 ≤ 現在の実課金
          - Premium ユーザ → Standard に変えた場合の超過課金 ≥ 現在の実課金
        """
        std_price = float(cfg["seats"]["standard"]["price_usd"])
        prem_price = float(cfg["seats"]["premium"]["price_usd"])
        cost_std = _seat_cost(api_cost, "standard", scenario, cfg)
        cost_prem = _seat_cost(api_cost, "premium", scenario, cfg)
        if seat == "standard":
            cost_std = std_price + billed
            cost_prem = prem_price + min(cost_prem - prem_price, billed)
        elif seat == "premium":
            cost_prem = prem_price + billed
            cost_std = std_price + max(cost_std - std_price, billed)
        rec = "standard" if cost_std <= cost_prem else "premium"
        return rec, cost_std, cost_prem

    # 以下の rows から作る users DataFrame は固定カラム（下記キー）を常に持つ。
    # 任意なのは code-analytics 由来の prs_with_cc / loc_with_cc のみ。
    rows = []
    for email in emails:
        seat = seat_by_email.get(email, "unknown")
        row = target.loc[email] if email in target.index else None
        api_cost = float(row["api_cost"]) if row is not None else 0.0
        # billed は aggregate_month が常に付与するため row があれば必ず存在する
        billed = float(row["billed"]) if row is not None else 0.0

        if seat == "unassigned":
            # 意図的な未割当（別組織でアサイン済み・管理者等）は損益分岐判定の対象外。
            # シート料 $0 の現状が最安のため、推奨もコスト試算も行わない
            nan = float("nan")
            rec_mid, cost_std, cost_prem = "unassigned", nan, nan
            rec_low = rec_high = "unassigned"
            cost_current, saving = nan, nan
            status, confidence = STATUS_EXCLUDED, "—"
            censored = False
        else:
            recs = {s: _costs_for(seat, api_cost, billed, s) for s in SCENARIOS}
            rec_mid, cost_std, cost_prem = recs["mid"]
            rec_low, rec_high = recs["low"][0], recs["high"][0]

            # 現シートでのコスト（観測実績）と、推奨シートに変えた場合の削減額（mid）
            if seat == "standard":
                cost_current, saving = cost_std, cost_std - min(cost_std, cost_prem)
            elif seat == "premium":
                cost_current, saving = cost_prem, cost_prem - min(cost_std, cost_prem)
            else:
                cost_current = float("nan")
                saving = float("nan")

            # ヒステリシス: 直近 n_hyst ヶ月すべてで同じ推奨・削減額がバッファ以上か
            if seat == "unknown":
                status = STATUS_UNKNOWN
            elif rec_mid == seat:
                status = STATUS_KEEP
            else:
                recent = months_used[-n_hyst:]
                checks = []
                for m in recent:
                    mdf = monthly[m].set_index("email")
                    if email in mdf.index:
                        m_cost = float(mdf.loc[email, "api_cost"])
                        m_billed = float(mdf.loc[email, "billed"])
                    else:
                        m_cost, m_billed = 0.0, 0.0
                    m_rec, m_std, m_prem = _costs_for(seat, m_cost, m_billed, "mid")
                    m_current = m_std if seat == "standard" else m_prem
                    m_saving = m_current - min(m_std, m_prem)
                    checks.append(m_rec == rec_mid and m_saving >= min_saving)
                if len(months_used) < n_hyst:
                    status = STATUS_WATCH_WAIT
                elif all(checks):
                    status = STATUS_CHANGE
                else:
                    status = STATUS_WATCH

            # 感度: low/high シナリオが mid の推奨と一致するか
            agree = sum(1 for s in ("low", "high") if recs[s][0] == rec_mid)
            confidence = {2: "高", 1: "中", 0: "低"}[agree]

            # 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ:
            # 「実効込み量が推定より大きい」か「上限で止められた」かの要確認フラグ
            censored = (
                seat == "standard"
                and billed == 0.0
                and api_cost >= censoring_margin * s_allowance_mid
            )

        rows.append({
            "email": email,
            "current_seat": seat,
            "api_cost_usd": round(api_cost, 2),
            "cost_if_standard_usd": round(cost_std, 2),
            "cost_if_premium_usd": round(cost_prem, 2),
            "cost_current_usd": round(cost_current, 2) if not pd.isna(cost_current) else None,
            "recommended_seat": rec_mid,
            "monthly_saving_usd": round(saving, 2) if not pd.isna(saving) else None,
            "status": status,
            "confidence": confidence,
            "rec_low": rec_low,
            "rec_high": rec_high,
            "cap_suspected": censored,
            "billed_extra_usd": round(billed, 2),
            "prompt_tokens": int(row["prompt_tokens"]) if row is not None else 0,
            "completion_tokens": int(row["completion_tokens"]) if row is not None else 0,
            "product_breakdown": (
                str(row["product_breakdown"]) if row is not None and "product_breakdown" in row.index else ""
            ),
            # model は必須カラムのため row があれば model_breakdown は常に存在する
            "model_breakdown": str(row["model_breakdown"]) if row is not None else "",
        })

    users = pd.DataFrame(rows)

    # 部署・職種・備考（任意ファイル members-info.csv）の結合
    _merge_members_info(users, input_dir, cfg, sources)

    # 活用度（Claude Code 貢献データ）の結合
    if code_result is not None:
        cc = code_result.df.set_index("email")
        for col in ("prs_with_cc", "loc_with_cc"):
            if col in cc.columns:
                users[col] = users["email"].map(cc[col]).fillna(0).astype(int)

    users = users.sort_values(
        ["status", "monthly_saving_usd"], ascending=[True, False]
    ).reset_index(drop=True)

    # spend にいるが members にいないユーザ
    warnings.extend(_warn_orphan_users(users))
    warnings.extend(_warn_active_unassigned(users, "api_cost_usd"))

    summary = _summarize(users, monthly[month], cfg, months_used, n_hyst)
    summary["org_service_cost_usd"] = org_usage.get("cost_usd", 0.0)
    summary["org_service_by_product"] = org_usage.get("by_product", {})
    return AnalysisResult(
        month=month, users=users, summary=summary, org=org,
        warnings=warnings, months_used=months_used, sources=sources,
        trend=trend, snapshot=snapshot, code_diff=code_diff, member_changes=member_changes,
    )


def _seat_summary(users: pd.DataFrame, cfg: dict) -> dict:
    """シート種別ごとの人数と現在のシート費用（analyze/preview サマリの共通部）。"""
    seats = users["current_seat"].value_counts().to_dict()
    std_price = float(cfg["seats"]["standard"]["price_usd"])
    prem_price = float(cfg["seats"]["premium"]["price_usd"])
    n_standard = int(seats.get("standard", 0))
    n_premium = int(seats.get("premium", 0))
    return {
        "n_members": int(len(users)),
        "n_standard": n_standard,
        "n_premium": n_premium,
        "n_unassigned": int(seats.get("unassigned", 0)),
        "n_unknown": int(seats.get("unknown", 0)),
        "seat_cost_now_usd": round(n_standard * std_price + n_premium * prem_price, 2),
    }


def _warn_unknown_models(models, cfg: dict) -> list[str]:
    """単価表に一致せず default 単価が適用されるモデルの警告（無ければ空リスト）。"""
    unknown_models = pricing.unmatched_models(models, cfg)
    if not unknown_models:
        return []
    return [
        f"model_prices: 単価表に一致しないモデルに default 単価を適用: {unknown_models}。"
        "config.yaml > model_prices にパターンを追記してください"
    ]


def _warn_orphan_users(users: pd.DataFrame) -> list[str]:
    """members に居ないが spend に居る利用者（seat=unknown）の警告（無ければ空リスト）。"""
    orphan = users[users["current_seat"] == "unknown"]["email"].tolist()
    if not orphan:
        return []
    return [
        f"members に存在しない利用ユーザ {len(orphan)} 名（シート不明として集計）: {orphan[:5]}"
    ]


def _summarize(users: pd.DataFrame, month_agg: pd.DataFrame, cfg: dict,
               months_used: list[str], n_hyst: int) -> dict:
    to_change = users[users["status"] == STATUS_CHANGE]
    watching = users[users["status"].str.startswith(STATUS_WATCH)]
    savings = float(to_change["monthly_saving_usd"].fillna(0).sum())

    summary = _seat_summary(users, cfg)
    summary.update({
        "seat_price_standard_usd": float(cfg["seats"]["standard"]["price_usd"]),
        "seat_price_premium_usd": float(cfg["seats"]["premium"]["price_usd"]),
        "total_api_cost_usd": round(float(month_agg["api_cost"].sum()), 2),
        "n_change_recommended": int(len(to_change)),
        "n_watching": int(len(watching)),
        "n_cap_suspected": int(users["cap_suspected"].sum()),
        "total_billed_extra_usd": round(
            float(users["billed_extra_usd"].fillna(0).sum())
            if "billed_extra_usd" in users.columns else 0.0, 2),
        "est_monthly_saving_usd": round(savings, 2),
        "months_used": months_used,
        "hysteresis_months": n_hyst,
    })
    return summary


def _warn_active_unassigned(users: pd.DataFrame, cost_col: str) -> list[str]:
    """シート未割当なのに利用実績があるユーザの警告（データ不整合・月中解約の手がかり）。"""
    active = users[
        (users["current_seat"] == "unassigned") & (users[cost_col] > 0)
    ]["email"].tolist()
    if not active:
        return []
    return [
        f"シート未割当なのに利用実績があるユーザ {len(active)} 名: {active[:5]}"
        "（members の更新漏れ、または月中のシート解除の可能性）"
    ]


# --- 速報モード（部分月データからの一次判断） ---

@dataclass
class PreviewResult:
    month: str
    users: pd.DataFrame
    summary: dict
    days_observed: int
    days_in_month: int
    org: str | None = None
    warnings: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    # 月中の利用推移（同一月の複数スナップショット差分。1つ以下なら None）
    snapshot: dict | None = None
    # 月中の Claude Code 活動（code-analytics スナップショット差分。1つ以下なら None）
    code_diff: dict | None = None
    # 月中のメンバー変動（members 単日スナップショット差分。1つ以下なら None）
    member_changes: dict | None = None


def _preview_label(seat: str, api_obs: float, api_proj: float, cfg: dict,
                   min_saving: float) -> tuple[str, str]:
    """月末ペース換算需要を allowance モデルにかけた一次判断ラベルと確度。

    実課金の観測は部分月では非線形（込み量を使い切るまで $0）で月額に換算できない
    ため、正式分析と違い観測実課金による拘束は行わず、純粋なモデル判定のみ。
    境界付近（3シナリオ不一致 or 削減見込みがバッファ未満）は「判断保留」に倒す。
    """
    if seat == "unassigned":
        return LABEL_EXCLUDED, "—"
    if seat == "unknown":
        return STATUS_UNKNOWN, "—"
    if api_obs < PREVIEW_IDLE_OBS_USD:
        return LABEL_IDLE, "—"
    recs = {s: _recommend(api_proj, s, cfg) for s in SCENARIOS}
    rec_mid, cost_std, cost_prem = recs["mid"]
    agree = sum(1 for s in ("low", "high") if recs[s][0] == rec_mid)
    confidence = {2: "高", 1: "中", 0: "低"}[agree]
    if rec_mid == seat:
        return (LABEL_PREM_OK if seat == "premium" else LABEL_STD_OK), confidence
    saving = (cost_prem - cost_std) if seat == "premium" else (cost_std - cost_prem)
    if agree == 2 and saving >= min_saving:
        return (LABEL_STD_CAND if seat == "premium" else LABEL_PREM_CONSIDER), confidence
    return LABEL_HOLD, confidence


def preview(input_dir: str | Path, month: str, cfg: dict, days_observed: int,
            org: str | None = None) -> PreviewResult:
    """部分月データの一次判断。対象月のみ使用し、ヒステリシス・変更推奨は行わない。"""
    input_dir = Path(input_dir)
    warnings: list[str] = []

    year, mon = (int(x) for x in month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    if not 1 <= days_observed <= days_in_month:
        raise ValueError(f"--days は 1〜{days_in_month}（{month} の暦日数）で指定してください")
    factor = days_in_month / days_observed

    # 月初開始スナップショットが2つ以上あれば月中推移の差分分析を発動する（重複警告の文言も変える）
    snap_entries, _ = ingest.spend_snapshots(input_dir, month)
    snapshot_active = len(snap_entries) >= 2
    member_diff_active = len(ingest.member_snapshots(input_dir, month)) >= 2

    spend_result = ingest.load_spend(input_dir, month, cfg, snapshot_active=snapshot_active)
    warnings.extend(spend_result.warnings)
    sources = {"spend": {month: str(spend_result.source)}}
    df = pricing.add_computed_cost(spend_result.df, cfg)

    warnings.extend(_warn_unknown_models(df["model"].unique(), cfg))

    is_user = df["email"].str.contains("@", na=False)
    basis, basis_notes = pricing.resolve_cost_basis(df[is_user], cfg)
    warnings.extend(basis_notes)
    df = pricing.apply_cost_basis(df, basis)
    org_service_obs = round(float(df[~is_user]["billed_usd"].sum()), 2)
    agg = aggregate_month(df[is_user]).set_index("email")

    members_result = ingest.load_members(input_dir, month, cfg, snapshot_active=member_diff_active)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    # 月中の利用推移（同一月の複数スナップショット差分・1つ以下なら None）
    snapshot, snap_warns = _compute_snapshot_diff(input_dir, month, cfg, seat_by_email)
    warnings.extend(snap_warns)
    # 月中の Claude Code 活動・メンバー変動（スナップショット差分・1つ以下なら None）
    code_diff, code_warns = _compute_code_diff(input_dir, month, cfg)
    warnings.extend(code_warns)
    member_changes, member_warns = _compute_member_changes(input_dir, month, cfg)
    warnings.extend(member_warns)
    _attach_loc_corroboration(snapshot, code_diff)

    seat_diff = float(cfg["seats"]["premium"]["price_usd"]) - float(cfg["seats"]["standard"]["price_usd"])
    min_saving = float(cfg["decision"]["buffer_ratio"]) * seat_diff

    rows = []
    for email in sorted(set(members["email"]) | set(agg.index)):
        seat = seat_by_email.get(email, "unknown")
        row = agg.loc[email] if email in agg.index else None
        api_obs = float(row["api_cost"]) if row is not None else 0.0
        # billed は aggregate_month が常に付与するため row があれば必ず存在する
        billed_obs = float(row["billed"]) if row is not None else 0.0
        api_proj = api_obs * factor
        label, confidence = _preview_label(seat, api_obs, api_proj, cfg, min_saving)
        rows.append({
            "email": email,
            "current_seat": seat,
            "api_cost_observed_usd": round(api_obs, 2),
            "api_cost_projected_usd": round(api_proj, 2),
            "billed_observed_usd": round(billed_obs, 2),
            "label": label,
            "confidence": confidence,
        })
    users = pd.DataFrame(rows)

    # 部署・職種・備考（任意ファイル members-info.csv）の結合
    _merge_members_info(users, input_dir, cfg, sources)

    warnings.extend(_warn_orphan_users(users))
    warnings.extend(_warn_active_unassigned(users, "api_cost_observed_usd"))

    summary = _seat_summary(users, cfg)
    summary.update({
        "days_observed": days_observed,
        "days_in_month": days_in_month,
        "total_api_observed_usd": round(float(users["api_cost_observed_usd"].sum()), 2),
        "total_api_projected_usd": round(float(users["api_cost_projected_usd"].sum()), 2),
        "n_billed": int((users["billed_observed_usd"] > 0).sum()),
        "label_counts": users["label"].value_counts().to_dict(),
        "org_service_cost_usd": org_service_obs,
    })
    return PreviewResult(
        month=month, users=users, summary=summary,
        days_observed=days_observed, days_in_month=days_in_month,
        org=org, warnings=warnings, sources=sources, snapshot=snapshot,
        code_diff=code_diff, member_changes=member_changes,
    )
