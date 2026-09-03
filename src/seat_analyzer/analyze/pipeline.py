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
    _attach_credits_mode,
    _compute_e_distribution,
    _credit_integrity_warnings,
    _credit_reached_emails,
    _credit_summary,
    _drop_unused_credit_columns,
    _grant_candidates,
    _usage_credits_cfg,
)
from .midmonth import _compute_trend, _diff_active, _midmonth_diffs

# モデル名を表示用に短縮する（claude-opus-4-8 → Opus 4.8, claude-fable-5 → Fable 5）
_MODEL_SHORT_RE = re.compile(r"(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?", re.IGNORECASE)


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

# 判定ステータス文字列（report パッケージの表示順・バッジ分岐と結合。値は変更しないこと）。
STATUS_CHANGE = "変更推奨"
STATUS_WATCH = "要観察"
STATUS_WATCH_WAIT = "要観察（データ蓄積待ち）"
STATUS_KEEP = "現状維持"
STATUS_UNKNOWN = "シート不明"
STATUS_EXCLUDED = "対象外（シート未割当）"

# 速報モードの一次判断ラベル（report パッケージの表示順・バッジ分岐と結合。値は変更しないこと）。
LABEL_IDLE = "遊休候補"
LABEL_STD_CAND = "Standard候補"
LABEL_PREM_CONSIDER = "Premium検討"
LABEL_HOLD = "判断保留"
LABEL_PREM_OK = "Premium妥当"
LABEL_STD_OK = "Standard妥当"
LABEL_EXCLUDED = "対象外（未割当）"

# 速報モード: 観測需要がこの額 [USD] 未満なら「遊休候補」（数日〜半月でこの水準は実質未使用）
PREVIEW_IDLE_OBS_USD = 1.0

# Identity 証拠として取り出す列。spend・members のどちらも ingest が任意列を NA で
# 補完するため、入力 CSV に列が無くても常に存在する
_IDENTITY_COLUMNS = ("email", "account_uuid", "user_id")


@dataclass(frozen=True)
class DecisionContext:
    """V2 判定の材料。ロード済みの明細から analyze() が組む（後段が spend を読み直さないため）。

    後段が spend を読み直すと、採用ファイルの選択と cost basis の解決が分析本体と
    食い違いうる。判定に使う月次の値は、この文脈に載せて1度の読み込みから配る。

    months は months_used（昇順・最後が対象月）、complete は月 → その月の spend が
    全月データか、aggregates は月 → aggregate_month の結果、product_usage は月 →
    product 特徴量、identity_rows は対象月の Identity 証拠行（email・account_uuid・
    user_id の3列）。
    """

    months: tuple[str, ...]
    complete: dict[str, bool]
    aggregates: dict[str, pd.DataFrame]
    product_usage: dict[str, ProductUsage]
    identity_rows: pd.DataFrame


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
    # シートが吸収した量の実測（E = API換算需要 − 実課金）。実課金発生ユーザがいなければ None
    e_distribution: dict | None = None
    # 追加クレジット付与候補（昇格前に上限つきクレジットで課金実測を薦めるユーザ）
    grant_candidates: list = field(default_factory=list)
    # 対象月の product 利用特徴量（費用は全 product・活用は Code。判定には使わない）
    product_usage: ProductUsage | None = None
    # V2 判定の材料（decision_context=True で分析したときだけ入る）
    decision_context: DecisionContext | None = None


def _seat_cost(api_cost: float, seat: str, scenario: str, cfg: dict) -> float:
    seat_cfg = cfg["seats"][seat]
    allowance = float(seat_cfg["allowance_usd"][scenario])
    return float(seat_cfg["price_usd"]) + max(0.0, api_cost - allowance)


def _recommend(api_cost: float, scenario: str, cfg: dict) -> tuple[str, float, float]:
    cost_std = _seat_cost(api_cost, "standard", scenario, cfg)
    cost_prem = _seat_cost(api_cost, "premium", scenario, cfg)
    rec = "standard" if cost_std <= cost_prem else "premium"
    return rec, cost_std, cost_prem


def _costs_for_current_seat(
    seat: str,
    api_cost: float,
    billed: float,
    scenario: str,
    cfg: dict,
) -> tuple[str, float, float]:
    """現シートは観測実績、変更先は allowance モデルで試算する。

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


@dataclass(frozen=True)
class _SpendHistory:
    """分析用に読み込み・価格適用を済ませたスペンド履歴。

    complete は月 → その月の spend が全月データか（ファイル名の期間が暦日数に届いて
    いれば全月。期間の無い命名は従来どおり全月として扱う）。

    monthly_product_usage と identity_rows は V2 判定の材料で、要求されたときだけ
    埋まる（V1 の実行に過去月の product 特徴量という新しい計算経路を足さないため）。
    """

    monthly: dict[str, pd.DataFrame]
    sources: dict[str, str]
    warnings: list[str]
    basis: str
    org_usage: dict
    product_usage: ProductUsage
    complete: dict[str, bool]
    monthly_product_usage: dict[str, ProductUsage]
    identity_rows: pd.DataFrame | None


def _load_spend_history(
    input_dir: Path,
    month: str,
    months_used: list[str],
    cfg: dict,
    *,
    snapshot_active: bool,
    decision_context: bool = False,
) -> _SpendHistory:
    """対象月までのスペンドを読み、全月へ同じ需要基準を適用する。

    decision_context=True のときだけ、V2 判定の材料（全月の product 特徴量と対象月の
    Identity 証拠行）も併せて組み立てる。
    """
    warnings: list[str] = []
    raw: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    complete: dict[str, bool] = {}
    for current_month in months_used:
        result = ingest.load_spend(
            input_dir,
            current_month,
            cfg,
            snapshot_active=snapshot_active and current_month == month,
        )
        warnings.extend(result.warnings)
        sources[current_month] = str(result.source)
        raw[current_month] = pricing.add_computed_cost(result.df, cfg)

        # ファイル名の期間が全月に満たない場合、月額前提の判定が歪む（過小評価）
        complete[current_month] = True
        period = ingest.file_period(result.source)
        if period is not None and period.days is not None:
            year, mon = (int(part) for part in current_month.split("-"))
            if period.days < calendar.monthrange(year, mon)[1]:
                complete[current_month] = False
                warnings.append(
                    f"{result.source.name}: {current_month} は部分月データ"
                    f"（{period.start:%m-%d}〜{period.end:%m-%d} の {period.days}日分）ですが"
                    "全月として扱っています。月中の一次判断には --preview を利用してください"
                )

    warnings.extend(
        _warn_unknown_models(
            pd.concat([df["model"] for df in raw.values()]).unique(), cfg
        )
    )

    target_user_rows = raw[month][raw[month]["email"].str.contains("@", na=False)]
    basis, basis_notes = pricing.resolve_cost_basis(target_user_rows, cfg)
    warnings.extend(basis_notes)

    monthly: dict[str, pd.DataFrame] = {}
    org_usage: dict = {}
    product_usage: ProductUsage | None = None
    monthly_product_usage: dict[str, ProductUsage] = {}
    for current_month, raw_df in raw.items():
        df = pricing.apply_cost_basis(raw_df, basis)
        if current_month == month and basis == "net_spend":
            warnings.extend(pricing.validate_spend(df, cfg))

        # ユーザ非帰属の組織利用（例: "(org service usage)" の Code Review 等）は
        # シート判定の対象外として分離し、別枠で計上する
        is_user = df["email"].str.contains("@", na=False)
        org_df = df[~is_user]
        if current_month == month and not org_df.empty:
            org_usage = {
                "cost_usd": round(float(org_df["billed_usd"].sum()), 2),
                "by_product": (
                    {
                        str(key): round(float(value), 2)
                        for key, value in org_df.groupby("product")["billed_usd"]
                        .sum()
                        .items()
                    }
                    if "product" in org_df.columns
                    else {}
                ),
            }
        if current_month == month:
            # 価格適用済みの明細から一度だけ計算する（後段が spend を読み直すと
            # cost basis や採用ファイルが分析本体と食い違いうるため）
            product_usage = compute_product_usage(
                df[is_user], cfg["product_policy"]
            )
            # 禁止 product の観測は判定対象のユーザ行に限らず報告する。特徴量は
            # ユーザ行だけで計算するので、組織サービス利用行の分をここで足す
            org_issue = find_org_service_prohibited(org_df, cfg["product_policy"])
            if org_issue is not None:
                product_usage = ProductUsage(
                    features=product_usage.features,
                    issues=[*product_usage.issues, org_issue],
                )
            if decision_context:
                monthly_product_usage[current_month] = product_usage
        elif decision_context:
            # 過去月の特徴量は V2 判定の履歴にだけ使う（issues は対象月のものだけを扱う）
            monthly_product_usage[current_month] = compute_product_usage(
                df[is_user], cfg["product_policy"]
            )
        monthly[current_month] = aggregate_month(df[is_user])

    # 対象月の存在は呼び出し側で検証済みなので、通常は到達しない内部不変条件。
    if product_usage is None:  # pragma: no cover
        raise AssertionError("対象月の product usage が構築されていません")
    return _SpendHistory(
        monthly=monthly,
        sources=sources,
        warnings=warnings,
        basis=basis,
        org_usage=org_usage,
        product_usage=product_usage,
        complete=complete,
        monthly_product_usage=monthly_product_usage,
        identity_rows=(
            target_user_rows[list(_IDENTITY_COLUMNS)] if decision_context else None
        ),
    )


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
        (f"members-info.csv に未登録のユーザ {len(unregistered)} 名: {unregistered}"
         "（部署・チーム・職種が空欄で集計されるため追記を推奨）"),
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


def _code_asof(path: Path) -> str | None:
    """採用した code-analytics ファイルの観測時点 "YYYY-MM-DD"（読めなければ None）。

    LoC は累積値で、その累積がいつまでの分かはファイル名の期間からしか分からない。
    単日スナップショット（kind=date）はその日、期間（kind=range）は末日が時点になる。
    月のみの命名（kind=month）は時点が決まらないので None を返す（推測して日付を
    書くと、実際より新しい・古い時点を断定することになる）。
    """
    period = ingest.file_period(path)
    if period is None or period.end is None:
        return None
    return f"{period.end:%Y-%m-%d}"


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


@dataclass(frozen=True)
class _AnalysisContext:
    """ユーザごとの正式判定で共有する、前処理済みの設定と月次表。"""

    cfg: dict
    months_used: tuple[str, ...]
    monthly_by_email: dict[str, pd.DataFrame]
    hysteresis_months: int
    censoring_margin: float
    min_saving: float
    standard_allowance_mid: float


def _hysteresis_status(
    email: str,
    seat: str,
    recommendation: str,
    context: _AnalysisContext,
) -> str:
    """直近月の推奨と削減額から正式判定のステータスを返す。"""
    if seat == "unknown":
        return STATUS_UNKNOWN
    if recommendation == seat:
        return STATUS_KEEP

    checks = []
    for current_month in context.months_used[-context.hysteresis_months :]:
        monthly = context.monthly_by_email[current_month]
        if email in monthly.index:
            api_cost = float(monthly.loc[email, "api_cost"])
            billed = float(monthly.loc[email, "billed"])
        else:
            api_cost, billed = 0.0, 0.0
        rec, cost_std, cost_prem = _costs_for_current_seat(
            seat, api_cost, billed, "mid", context.cfg
        )
        current_cost = cost_std if seat == "standard" else cost_prem
        saving = current_cost - min(cost_std, cost_prem)
        checks.append(rec == recommendation and saving >= context.min_saving)

    if len(context.months_used) < context.hysteresis_months:
        return STATUS_WATCH_WAIT
    return STATUS_CHANGE if all(checks) else STATUS_WATCH


def _analysis_row(
    email: str,
    seat: str,
    monthly_row: pd.Series | None,
    context: _AnalysisContext,
) -> dict:
    """正式分析のユーザ1名ぶんの出力行を組み立てる。

    この行から作る users DataFrame は固定カラム（下記キー）を常に持つ。任意なのは
    code-analytics 由来の prs_with_cc / loc_with_cc のみ。
    """
    api_cost = float(monthly_row["api_cost"]) if monthly_row is not None else 0.0
    # billed は aggregate_month が常に付与するため row があれば必ず存在する
    billed = float(monthly_row["billed"]) if monthly_row is not None else 0.0

    if seat == "unassigned":
        # 意図的な未割当（別組織でアサイン済み・管理者等）は損益分岐判定の対象外。
        # シート料 $0 の現状が最安のため、推奨もコスト試算も行わない
        nan = float("nan")
        recommendation, cost_std, cost_prem = "unassigned", nan, nan
        rec_low = rec_high = "unassigned"
        current_cost, saving = nan, nan
        status, confidence = STATUS_EXCLUDED, "—"
        censored = False
    else:
        recommendations = {
            scenario: _costs_for_current_seat(
                seat, api_cost, billed, scenario, context.cfg
            )
            for scenario in SCENARIOS
        }
        recommendation, cost_std, cost_prem = recommendations["mid"]
        rec_low, rec_high = recommendations["low"][0], recommendations["high"][0]

        if seat == "standard":
            current_cost = cost_std
        elif seat == "premium":
            current_cost = cost_prem
        else:
            current_cost = float("nan")
        saving = current_cost - min(cost_std, cost_prem)

        status = _hysteresis_status(email, seat, recommendation, context)
        agree = sum(
            recommendations[scenario][0] == recommendation
            for scenario in ("low", "high")
        )
        confidence = {2: "高", 1: "中", 0: "低"}[agree]
        # 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ:
        # 「実効込み量が推定より大きい」か「上限で止められた」かの要確認フラグ
        censored = (
            seat == "standard"
            and billed == 0.0
            and api_cost
            >= context.censoring_margin * context.standard_allowance_mid
        )

    return {
        "email": email,
        "current_seat": seat,
        "api_cost_usd": round(api_cost, 2),
        "cost_if_standard_usd": round(cost_std, 2),
        "cost_if_premium_usd": round(cost_prem, 2),
        "cost_current_usd": (
            round(current_cost, 2) if not pd.isna(current_cost) else None
        ),
        "recommended_seat": recommendation,
        "monthly_saving_usd": round(saving, 2) if not pd.isna(saving) else None,
        "status": status,
        "confidence": confidence,
        "rec_low": rec_low,
        "rec_high": rec_high,
        "cap_suspected": censored,
        "billed_extra_usd": round(billed, 2),
        **_detail_columns(monthly_row),
    }


def _build_analysis_users(
    monthly: dict[str, pd.DataFrame],
    months_used: list[str],
    emails: list[str],
    seat_by_email: dict[str, str],
    cfg: dict,
) -> tuple[pd.DataFrame, int]:
    """月次表を索引化し、正式分析の全ユーザ行を構築する。"""
    decision_cfg = cfg["decision"]
    hysteresis_months = int(decision_cfg["hysteresis_months"])
    monthly_by_email = {
        current_month: frame.set_index("email")
        for current_month, frame in monthly.items()
    }
    context = _AnalysisContext(
        cfg=cfg,
        months_used=tuple(months_used),
        monthly_by_email=monthly_by_email,
        hysteresis_months=hysteresis_months,
        censoring_margin=float(decision_cfg["censoring_margin"]),
        min_saving=_min_saving(cfg),
        standard_allowance_mid=float(
            cfg["seats"]["standard"]["allowance_usd"]["mid"]
        ),
    )
    target = monthly_by_email[months_used[-1]]
    rows = [
        _analysis_row(
            email,
            seat_by_email.get(email, "unknown"),
            target.loc[email] if email in target.index else None,
            context,
        )
        for email in emails
    ]
    return pd.DataFrame(rows), hysteresis_months


def analyze(
    input_dir: str | Path,
    month: str,
    cfg: dict,
    org: str,
    *,
    decision_context: bool = False,
) -> AnalysisResult:
    """1組織分の分析。input_dir はその組織の入力ディレクトリ（spend/ 等を直下に持つ）。

    decision_context=True のとき、V2 判定の材料（DecisionContext）も組んで結果に載せる。
    既定では組まない（V1 の実行に過去月の product 特徴量という新しい計算経路を足さない
    ため）。V1 の users・summary・warnings はどちらでも同じ。
    """
    input_dir = Path(input_dir)

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
    history = _load_spend_history(
        input_dir,
        month,
        months_used,
        cfg,
        snapshot_active=active.spend,
        decision_context=decision_context,
    )
    warnings = history.warnings
    monthly = history.monthly
    sources: dict = {"spend": history.sources}

    members_result = ingest.load_members(input_dir, month, cfg, snapshot_active=active.members)
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)

    code_result = ingest.load_code_analytics(input_dir, month, cfg, snapshot_active=active.code)
    if code_result is not None:
        warnings.extend(code_result.warnings)
        sources["code_analytics"] = str(code_result.source)

    # --- 対象月テーブル: members と spend の全ユーザを対象にする（利用ゼロも含む）---
    emails = sorted(set(members["email"]) | set(monthly[month]["email"]))
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    # 前月からの変化・月次推移（ロード済み monthly から毎回計算・初月は None）
    trend = _compute_trend(monthly, months_used, set(members["email"]), cfg)
    # 月中差分（利用推移・Claude Code 活動・メンバー変動。1つ以下なら None）
    snapshot, code_diff, member_changes, diff_warns = _midmonth_diffs(
        input_dir, month, cfg, seat_by_email
    )
    warnings.extend(diff_warns)

    users, n_hyst = _build_analysis_users(
        monthly, months_used, emails, seat_by_email, cfg
    )

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
    summary["org_service_cost_usd"] = history.org_usage.get("cost_usd", 0.0)
    summary["org_service_by_product"] = history.org_usage.get("by_product", {})
    # E 分布は cost_basis=computed のときのみ（net_spend 基準では需要=課金で E が無意味）
    e_distribution = (
        _compute_e_distribution(users) if history.basis == "computed" else None
    )
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
        product_usage=history.product_usage,
        decision_context=(
            _decision_context(history, months_used, members_result.df)
            if decision_context else None
        ),
    )


def _decision_context(
    history: _SpendHistory, months_used: list[str], members: pd.DataFrame
) -> DecisionContext:
    """ロード済みの明細から V2 判定の材料を組む。

    identity_rows は members → 対象月の spend の順に縦へ積み、同じ証拠の重複行だけを
    畳む（並びを入力の行順で決めることで、同じ入力からは常に同じ解決結果になる）。
    """
    # 材料つきで読み込んだ履歴からのみ組める（呼び出し側で保証済みの内部不変条件）
    if history.identity_rows is None:  # pragma: no cover
        raise AssertionError("V2 判定の材料が読み込まれていません")
    identity_rows = pd.concat(
        [members[list(_IDENTITY_COLUMNS)], history.identity_rows],
        ignore_index=True,
    ).drop_duplicates(ignore_index=True)
    return DecisionContext(
        months=tuple(months_used),
        complete=dict(history.complete),
        aggregates=dict(history.monthly),
        product_usage=dict(history.monthly_product_usage),
        identity_rows=identity_rows,
    )


def _seat_summary(users: pd.DataFrame, cfg: dict) -> dict:
    """シート種別ごとの人数と現在のシート費用（analyze/preview サマリの共通部）。"""
    seats = users["current_seat"].value_counts().to_dict()
    std_price = float(cfg["seats"]["standard"]["price_usd"])
    prem_price = float(cfg["seats"]["premium"]["price_usd"])
    n_standard = int(seats.get("standard", 0))
    n_premium = int(seats.get("premium", 0))
    return {
        "n_members": len(users),
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
        (f"model_prices: 単価表に一致しないモデルに default 単価を適用: {unknown_models}。"
         "config.yaml > model_prices にパターンを追記してください")
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
        "n_change_recommended": len(to_change),
        "n_watching": len(watching),
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
        (f"シート未割当なのに利用実績があるユーザ {len(active)} 名: {active[:5]}"
         "（members の更新漏れ、または月中のシート解除の可能性）")
    ]
