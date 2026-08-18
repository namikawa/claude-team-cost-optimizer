"""ユーザ×月の集計と、シート損益分岐判定（ヒステリシス・感度分析・センサリング）。"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .. import ingest, pricing
from ..product_usage import (
    ProductUsage,
    compute as compute_product_usage,
    find_org_service_prohibited,
)
from .credits import (
    CREDIT_DISABLED as CREDIT_DISABLED,
    CREDIT_ENABLED as CREDIT_ENABLED,
    CREDIT_UNKNOWN as CREDIT_UNKNOWN,
    _attach_credits_mode,
    _compute_e_distribution,
    _credit_integrity_warnings,
    _credit_reach_preview,
    _credit_reached_emails,
    _credit_summary,
    _drop_unused_credit_columns,
    _grant_candidates,
    _usage_credits_cfg,
    credits_mode as credits_mode,
)
from .midmonth import _compute_trend, _diff_active, _midmonth_diffs

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
    org: str  # 組織名（input/<組織名>/ のディレクトリ名。レポートの表題・出力先に使う）
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
    # 込み枠の実測（E = API換算需要 − 実課金）。実課金発生ユーザがいなければ None
    e_distribution: dict | None = None
    # 追加クレジット付与候補（昇格前に上限つきクレジットで課金実測を薦めるユーザ）
    grant_candidates: list = field(default_factory=list)
    # 対象月の product 利用特徴量（費用は全 product・活用は Code。判定には使わない）
    product_usage: ProductUsage | None = None


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
            # groupby の結果は product 昇順。安定ソートなら同率の product はその昇順のまま
            # 残るので、構成比の表示順が実行環境によらず一意に決まる
            by_product = g.groupby("product")["_pw"].sum().sort_values(
                ascending=False, kind="stable")
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
        # モデル利用割合はトークン量（input+output）基準。寄与降順・1%未満は集約。
        # groupby の結果は model 昇順で、安定ソートなら同率のモデルはその昇順のまま残るので、
        # 表示順が実行環境によらず一意に決まる
        by_model = g.groupby("model")["_tok"].sum().sort_values(ascending=False, kind="stable")
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


def _min_saving(cfg: dict) -> float:
    """変更推奨に必要な最小削減額（シート差額 × decision.buffer_ratio）。"""
    seat_diff = (float(cfg["seats"]["premium"]["price_usd"])
                 - float(cfg["seats"]["standard"]["price_usd"]))
    return float(cfg["decision"]["buffer_ratio"]) * seat_diff


def _merge_members_info(users: pd.DataFrame, input_dir: Path, cfg: dict,
                        sources: dict, month: str | None = None) -> list[str]:
    """任意ファイル members-info の department/team/role/note/credit_limit_usd を users に付与する。

    未登録メンバーは空文字列（credit_limit_usd は NaN）。members-info にだけ居るメールは
    行を追加しない。ファイルが読めた場合のみ sources["members_info"] にパスを記録し、
    ロード時の警告（スナップショット解決・不正な上限値）と未登録ユーザの警告を返す。
    未登録の警告はファイルが読めた場合のみ（任意ファイルのため未使用の組織では出さない）。
    """
    info_result = ingest.load_members_info(input_dir, cfg, month=month)
    if info_result is None:
        for col in ("department", "team", "role", "note"):
            users[col] = ""
        users["credit_limit_usd"] = float("nan")
        return []
    sources["members_info"] = str(info_result.source)
    info = info_result.df.set_index("email")
    for col in ("department", "team", "role", "note"):
        users[col] = users["email"].map(info[col]).fillna("") if col in info.columns else ""
    # 部署・チームは兼務（複数所属）を正規化した表示文字列で保持する（集計時に再分割）
    for col in ("department", "team"):
        users[col] = users[col].map(ingest.normalize_affiliations)
    if "credit_limit_usd" in info.columns:
        users["credit_limit_usd"] = users["email"].map(info["credit_limit_usd"]).astype(float)
    else:
        users["credit_limit_usd"] = float("nan")
    # 管理画面へのメンバー追加に members-info の追記が追従していないと部署別サマリの
    # 人数が実態とズレるため、分析対象なのに members-info に行が無いユーザを通知する
    unregistered = sorted(set(users["email"]) - set(info.index))
    if not unregistered:
        return info_result.warnings
    return [
        *info_result.warnings,
        f"members-info.csv に未登録のユーザ {len(unregistered)} 名: {unregistered}"
        "（部署・チーム・職種が空欄で集計されるため追記を推奨）",
    ]


def _merge_code_analytics(users: pd.DataFrame,
                          code_result: ingest.LoadResult | None) -> None:
    """活用度（Claude Code 貢献データ）の列を users に結合する（正式・速報で共通）。

    ファイルが無い組織（code_result が None）や列を持たない CSV では列を足さない。
    列の有無がそのままレポートの LoC 列の出し分けになるので、0 で埋めた列を作らない。
    """
    if code_result is None:
        return
    cc = code_result.df.set_index("email")
    for col in ("prs_with_cc", "loc_with_cc"):
        if col in cc.columns:
            users[col] = users["email"].map(cc[col]).fillna(0).astype(int)


def _detail_columns(row) -> dict:
    """詳細利用状況の列（トークン数・モデル割合・product構成）。利用の無いユーザは 0 と空。"""
    return {
        "prompt_tokens": int(row["prompt_tokens"]) if row is not None else 0,
        "completion_tokens": int(row["completion_tokens"]) if row is not None else 0,
        "product_breakdown": (
            str(row["product_breakdown"])
            if row is not None and "product_breakdown" in row.index else ""
        ),
        # model は必須カラムのため row があれば model_breakdown は常に存在する
        "model_breakdown": str(row["model_breakdown"]) if row is not None else "",
    }


def analyze(input_dir: str | Path, month: str, cfg: dict, org: str) -> AnalysisResult:
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

    # 対象月に時点の違う入力が2つ以上あるなら月中差分を発動する。主データの採用は
    # 現行どおり（期間の広い方）で、重複警告の文言だけ差し替える。
    active = _diff_active(input_dir, month)

    raw: dict[str, pd.DataFrame] = {}
    sources: dict = {"spend": {}}
    for m in months_used:
        result = ingest.load_spend(
            input_dir, m, cfg, snapshot_active=(active.spend and m == month)
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
    usage: ProductUsage | None = None
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
        if m == month:
            # 価格適用済みの明細から一度だけ計算する（後段が spend を読み直すと
            # cost basis や採用ファイルが分析本体と食い違いうるため）
            usage = compute_product_usage(df[is_user], cfg["product_policy"])
            # 禁止 product の観測は判定対象のユーザ行に限らず報告する。特徴量は
            # ユーザ行だけで計算するので、組織サービス利用行の分をここで足す
            org_issue = find_org_service_prohibited(org_df, cfg["product_policy"])
            if org_issue is not None:
                usage = ProductUsage(
                    features=usage.features, issues=[*usage.issues, org_issue])
        monthly[m] = aggregate_month(df[is_user])

    members_result = ingest.load_members(input_dir, month, cfg, snapshot_active=active.members)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)

    code_result = ingest.load_code_analytics(input_dir, month, cfg, snapshot_active=active.code)
    if code_result is not None:
        warnings.extend(code_result.warnings)
        sources["code_analytics"] = str(code_result.source)

    # --- 対象月テーブル: members と spend の全ユーザを対象にする（利用ゼロも含む）---
    target = monthly[month].set_index("email")
    emails = sorted(set(members["email"]) | set(target.index))
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    # 前月からの変化・月次推移（ロード済み monthly から毎回計算・初月は None）
    trend = _compute_trend(monthly, months_used, set(members["email"]), cfg)
    # 月中差分（利用推移・Claude Code 活動・メンバー変動。1つ以下なら None）
    snapshot, code_diff, member_changes, diff_warns = _midmonth_diffs(
        input_dir, month, cfg, seat_by_email
    )
    warnings.extend(diff_warns)

    decision_cfg = cfg["decision"]
    n_hyst = int(decision_cfg["hysteresis_months"])
    censoring_margin = float(decision_cfg["censoring_margin"])
    min_saving = _min_saving(cfg)
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
            **_detail_columns(row),
        })

    users = pd.DataFrame(rows)

    # 部署・職種・備考・追加クレジット上限（任意ファイル members-info）の結合
    warnings.extend(_merge_members_info(users, input_dir, cfg, sources, month))

    # 追加クレジットのモード導出（当月までに実課金が観測されたユーザは enabled と自動確定）
    billed_ever = set()
    for m in months_used:
        dfm = monthly[m]
        billed_ever |= set(dfm.loc[dfm["billed"] > 0.0, "email"])
    _attach_credits_mode(users, billed_ever)

    # 活用度（Claude Code 貢献データ）の結合
    _merge_code_analytics(users, code_result)

    users = users.sort_values(
        ["status", "monthly_saving_usd"], ascending=[True, False]
    ).reset_index(drop=True)

    # spend にいるが members にいないユーザ
    warnings.extend(_warn_orphan_users(users))
    warnings.extend(_warn_active_unassigned(users, "api_cost_usd"))

    # 追加クレジットの整合性・上限到達の警告（表示専用・判定には影響しない）
    reached = _credit_reached_emails(users, cfg, "billed_extra_usd")
    if reached:
        warnings.append(
            f"追加クレジット: 上限到達 {len(reached)} 名: {reached[:10]}"
            "（月後半は枠内のみで稼働した可能性）"
        )
    warnings.extend(_credit_integrity_warnings(users, cfg, "billed_extra_usd"))

    summary = _summarize(users, monthly[month], cfg, months_used, n_hyst)
    summary["org_service_cost_usd"] = org_usage.get("cost_usd", 0.0)
    summary["org_service_by_product"] = org_usage.get("by_product", {})
    # E 分布は cost_basis=computed のときのみ（net_spend 基準では需要=課金で E が無意味）
    e_distribution = _compute_e_distribution(users, cfg) if basis == "computed" else None
    # 付与候補の昇格方向は「実課金で拘束する前の純モデル判定」で見る（無効ユーザは billed=0 で
    # 拘束後は常に Standard 推奨になり本命対象が漏れるため）
    upgrade = users["api_cost_usd"].map(lambda a: _recommend(float(a), "mid", cfg)[0] == "premium")
    grant_candidates = _grant_candidates(users, upgrade, cfg)
    users = _drop_unused_credit_columns(users, summary)
    return AnalysisResult(
        month=month, users=users, summary=summary, org=org,
        warnings=warnings, months_used=months_used, sources=sources,
        trend=trend, snapshot=snapshot, code_diff=code_diff, member_changes=member_changes,
        e_distribution=e_distribution, grant_candidates=grant_candidates,
        product_usage=usage,
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
        "grant_suggested_cap_usd": _usage_credits_cfg(cfg)["grant_suggested_cap_usd"],
        # 補助プロダクトの需要が多いことの閾値（表示用。判定には使わない）。
        # レポート側がしきい値の金額を書けるように設定から運ぶ
        "supplementary_high_usd": float(cfg["product_policy"]["supplementary_high_usd"]),
    })
    summary.update(_credit_summary(users))
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
    org: str
    warnings: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    # 月中の利用推移（同一月の複数スナップショット差分。1つ以下なら None）
    snapshot: dict | None = None
    # 月中の Claude Code 活動（code-analytics スナップショット差分。1つ以下なら None）
    code_diff: dict | None = None
    # 月中のメンバー変動（members 単日スナップショット差分。1つ以下なら None）
    member_changes: dict | None = None
    # 追加クレジット残額ブロック（enabled・有限 κ・実課金>0 のユーザ。対象なしなら None）
    credit_reach: dict | None = None
    # 追加クレジット付与候補（昇格前に上限つきクレジットで課金実測を薦めるユーザ）
    grant_candidates: list = field(default_factory=list)


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
            org: str) -> PreviewResult:
    """部分月データの一次判断。対象月のみ使用し、ヒステリシス・変更推奨は行わない。"""
    input_dir = Path(input_dir)
    warnings: list[str] = []

    year, mon = (int(x) for x in month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    if not 1 <= days_observed <= days_in_month:
        raise ValueError(f"--days は 1〜{days_in_month}（{month} の暦日数）で指定してください")
    factor = days_in_month / days_observed

    # 時点の違う入力が2つ以上あれば月中差分を発動する（重複警告の文言も変える）
    active = _diff_active(input_dir, month)

    spend_result = ingest.load_spend(input_dir, month, cfg, snapshot_active=active.spend)
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

    members_result = ingest.load_members(input_dir, month, cfg, snapshot_active=active.members)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    # 活用度（任意ファイル code-analytics）。速報では詳細利用状況の LoC 列にしか使わない
    # 表示専用のデータで、一次判断には入らない。ロード時の指摘（採用ファイルの選択・
    # 任意カラムの欠落）は同じ入力に対して正式分析が出すため、速報の警告には足さない
    code_result = ingest.load_code_analytics(input_dir, month, cfg, snapshot_active=active.code)
    if code_result is not None:
        sources["code_analytics"] = str(code_result.source)

    # 月中差分（利用推移・Claude Code 活動・メンバー変動。1つ以下なら None）
    snapshot, code_diff, member_changes, diff_warns = _midmonth_diffs(
        input_dir, month, cfg, seat_by_email
    )
    warnings.extend(diff_warns)

    min_saving = _min_saving(cfg)

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
            **_detail_columns(row),
        })
    users = pd.DataFrame(rows)

    # 部署・職種・備考・追加クレジット上限（任意ファイル members-info）の結合
    warnings.extend(_merge_members_info(users, input_dir, cfg, sources, month))
    _merge_code_analytics(users, code_result)
    # クレジットモード（速報は当月の観測実課金のみで billed_ever を判断）
    billed_ever = set(users.loc[users["billed_observed_usd"] > 0.0, "email"])
    _attach_credits_mode(users, billed_ever)

    warnings.extend(_warn_orphan_users(users))
    warnings.extend(_warn_active_unassigned(users, "api_cost_observed_usd"))
    warnings.extend(_credit_integrity_warnings(users, cfg, "billed_observed_usd"))

    summary = _seat_summary(users, cfg)
    summary.update({
        "days_observed": days_observed,
        "days_in_month": days_in_month,
        "total_api_observed_usd": round(float(users["api_cost_observed_usd"].sum()), 2),
        "total_api_projected_usd": round(float(users["api_cost_projected_usd"].sum()), 2),
        "n_billed": int((users["billed_observed_usd"] > 0).sum()),
        "label_counts": users["label"].value_counts().to_dict(),
        "org_service_cost_usd": org_service_obs,
        "grant_suggested_cap_usd": _usage_credits_cfg(cfg)["grant_suggested_cap_usd"],
    })
    summary.update(_credit_summary(users))
    credit_reach = _credit_reach_preview(users, days_observed, days_in_month, cfg, snapshot)
    upgrade = users["label"].isin([LABEL_PREM_CONSIDER, LABEL_HOLD])
    grant_candidates = _grant_candidates(users, upgrade, cfg, demand_col="api_cost_projected_usd")
    users = _drop_unused_credit_columns(users, summary)
    return PreviewResult(
        month=month, users=users, summary=summary,
        days_observed=days_observed, days_in_month=days_in_month,
        org=org, warnings=warnings, sources=sources, snapshot=snapshot,
        code_diff=code_diff, member_changes=member_changes,
        credit_reach=credit_reach, grant_candidates=grant_candidates,
    )
