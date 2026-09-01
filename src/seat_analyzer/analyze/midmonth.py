"""月中・前月との差分（利用推移・メンバー変動・Claude Code 活動）の計算。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .. import ingest, pricing
from .credits import _credit_display, _kappa_equal


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
        # 最新区間の実課金増分（追加クレジット到達予測で「現在の課金ペース」に使う）
        bills = [s["billed"].get(email, 0.0) for s in snaps]
        rows.append({
            "email": email,
            "cum": [round(c, 2) for c in cums],
            "latest_delta": round(latest_delta, 2),
            "stall": stall,
            "seat": seat_by_email.get(email, "unknown"),
            "billed_latest": round(bills[-1], 2),
            "billed_delta": round(bills[-1] - bills[-2], 2),
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


def _compute_credit_changes(input_dir: Path, month: str, cfg: dict) -> tuple[list[dict], list[dict]]:
    """対象月の members-info 日付スナップショット（2つ以上）の隣接差分から κ 変更を検出する。

    戻り値は (credit_changes, credit_snaps)。1つ以下なら ([], []) を返す。
    """
    entries = ingest.member_info_snapshots(input_dir, month)
    if len(entries) < 2:
        return [], []
    snaps = []
    for period, path in entries:
        df = ingest.load_members_info_file(path, cfg)
        kappa_by = {e: k for e, k in zip(df["email"], df["credit_limit_usd"], strict=False)}
        snaps.append({"label": f"{period.start:%m-%d}", "kappa": kappa_by})
    changes = []
    for i in range(1, len(snaps)):
        prev, curr = snaps[i - 1], snaps[i]
        interval_label = f"{prev['label']}→{curr['label']}"
        for email in sorted(set(prev["kappa"]) & set(curr["kappa"])):
            if not _kappa_equal(prev["kappa"][email], curr["kappa"][email]):
                changes.append({
                    "email": email,
                    "from": _credit_display(prev["kappa"][email]),
                    "to": _credit_display(curr["kappa"][email]),
                    "interval_label": interval_label,
                })
    return changes, [{"label": s["label"]} for s in snaps]


def _unique_emails(changes: list[dict]) -> list[str]:
    """変更イベントの列からユーザのメールを重複なく取り出す（出現順を保つ）。

    seat_changes / credit_changes は区間ごとのイベントなので、月内に複数回変わった
    ユーザは複数回現れる。件数ではなく人数を数える箇所で使う。set を使わないのは
    反復順が実行ごとに変わるためで、dict.fromkeys で構築順（区間順・区間内はメール
    昇順）をそのまま保つ。
    """
    return list(dict.fromkeys(c["email"] for c in changes))


def _compute_member_changes(input_dir: Path, month: str, cfg: dict) -> tuple[dict | None, list[str]]:
    """対象月の単日スナップショット members（2つ以上）の隣接差分から月中の変動を検出する。

    シート変更・追加・削除を時系列順に列挙し、members-info の日付スナップショットが2つ以上
    あれば追加クレジット上限 κ の変更も併記する。変動が1件も無くてもセクションは出す
    （スナップショットを取って変動が無かったこと自体に情報価値があるため）。members・members-info
    のどちらのスナップショットも1つ以下なら (None, []) を返す（既存出力と同一）。

    判定ロジック・ヒステリシスには一切影響しない表示専用の情報。シート変更が1件以上
    あれば、当月判定は最新スナップショット時点のシートで行う旨の参考警告を返す。
    """
    entries = ingest.member_snapshots(input_dir, month)
    credit_changes, credit_snaps = _compute_credit_changes(input_dir, month, cfg)
    if len(entries) < 2 and not credit_snaps:
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

    # 人数（ユニークなユーザ）と件数（区間ごとのイベント）は一致しないので両方出す。
    # 例示リストも人単位（同一ユーザが並ばないように重複を除いてから先頭5件）。
    warnings: list[str] = []
    if seat_changes:
        emails = _unique_emails(seat_changes)
        warnings.append(
            f"月中にシート変更を検出した ユーザ {len(emails)} 名"
            f"（変更 {len(seat_changes)} 件）: {emails[:5]}"
            "（当月の損益分岐判定は最新スナップショット時点のシートで行うため参考値）"
        )
    if credit_changes:
        emails = _unique_emails(credit_changes)
        warnings.append(
            f"月中に追加クレジット上限の変更を検出 {len(emails)} 名"
            f"（変更 {len(credit_changes)} 件）: {emails[:5]}"
            "（変更月の課金は部分月のため、上限に基づく判定は翌月から行ってください）"
        )

    # 表示用の時点ラベルと「変動なし」判定は md / HTML の両方が必要とするため、
    # 表示側で二重に導出せずここで確定させる（members が無ければ members-info 側で代替）
    labels = [s["label"] for s in snaps] or [s["label"] for s in credit_snaps]
    return {
        "snaps": [{"label": s["label"]} for s in snaps],
        "labels": labels,
        "empty": not (seat_changes or joined or left or credit_changes),
        "seat_changes": seat_changes,
        "joined": joined,
        "left": left,
        "credit_snaps": credit_snaps,
        "credit_changes": credit_changes,
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
        df = ingest.load_code_analytics_file(path, cfg)
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
        loc_cum = [round(s["loc"].get(email, 0.0)) for s in snaps]
        if all(c == 0 for c in loc_cum):
            continue   # 全時点で LoC 0 のユーザは省く
        loc_delta = loc_cum[-1] - loc_cum[-2]
        prs_delta = None
        if has_prs:
            prs_cum = [round(s["prs"].get(email, 0.0)) for s in snaps]
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


@dataclass(frozen=True)
class _DiffActive:
    """月中差分が発動するか（同一月に時点の違う入力が2つ以上あるか）のフラグ。

    ロード側（load_spend / load_members / load_code_analytics）に渡し、同一月の重複解決の
    警告文言を「差分にも使う」向けへ切り替えるために使う。
    """

    spend: bool
    members: bool
    code: bool


def _diff_active(input_dir: Path, month: str) -> _DiffActive:
    """対象月の月中差分の発動条件（各入力のスナップショットが2つ以上あるか）を判定する。"""
    spend_entries, _ = ingest.spend_snapshots(input_dir, month)
    return _DiffActive(
        spend=len(spend_entries) >= 2,
        members=len(ingest.member_snapshots(input_dir, month)) >= 2,
        code=len(ingest.code_snapshots(input_dir, month)) >= 2,
    )


def _midmonth_diffs(
    input_dir: Path, month: str, cfg: dict, seat_by_email: dict
) -> tuple[dict | None, dict | None, dict | None, list[str]]:
    """月中差分（利用推移・Claude Code 活動・メンバー変動）をまとめて計算する。

    正式分析と速報が同じ手順（3種の差分を取り、最後に spend の停止疑いへ LoC 増分の
    傍証/食い違いを注記する）を踏むため1箇所に集約する。差分の種類を増やすときも
    ここだけを直せば両方に反映される。各差分はスナップショットが1つ以下なら None。
    戻り値は (snapshot, code_diff, member_changes, warnings)。
    """
    snapshot, snap_warns = _compute_snapshot_diff(input_dir, month, cfg, seat_by_email)
    code_diff, code_warns = _compute_code_diff(input_dir, month, cfg)
    member_changes, member_warns = _compute_member_changes(input_dir, month, cfg)
    _attach_loc_corroboration(snapshot, code_diff)
    return snapshot, code_diff, member_changes, [*snap_warns, *code_warns, *member_warns]
