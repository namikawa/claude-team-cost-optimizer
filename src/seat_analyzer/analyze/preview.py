"""部分月データから一次判断を作る速報パイプライン。"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .. import ingest, pricing
from .credits import (
    _attach_credits_mode,
    _credit_integrity_warnings,
    _credit_reach_preview,
    _credit_summary,
    _drop_unused_credit_columns,
    _grant_candidates,
    _usage_credits_cfg,
)
from .midmonth import _diff_active, _midmonth_diffs
from .pipeline import (
    LABEL_EXCLUDED,
    LABEL_HOLD,
    LABEL_IDLE,
    LABEL_PREM_CONSIDER,
    LABEL_PREM_OK,
    LABEL_STD_CAND,
    LABEL_STD_OK,
    PREVIEW_IDLE_OBS_USD,
    SCENARIOS,
    STATUS_UNKNOWN,
    _code_asof,
    _detail_columns,
    _merge_code_analytics,
    _merge_members_info,
    _min_saving,
    _recommend,
    _seat_summary,
    _warn_active_unassigned,
    _warn_orphan_users,
    _warn_unknown_models,
    aggregate_month,
)


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
    # LoC（code-analytics）の観測時点 "YYYY-MM-DD"（表示専用）。spend の観測期間と
    # ずれることがあるため、詳細利用状況の脚注に添える。時点が読めない場合は None
    code_asof: str | None = None


def _preview_label(
    seat: str,
    api_obs: float,
    api_proj: float,
    cfg: dict,
    min_saving: float,
) -> tuple[str, str]:
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
    recommendations = {
        scenario: _recommend(api_proj, scenario, cfg) for scenario in SCENARIOS
    }
    rec_mid, cost_std, cost_prem = recommendations["mid"]
    agree = sum(
        recommendations[scenario][0] == rec_mid for scenario in ("low", "high")
    )
    confidence = {2: "高", 1: "中", 0: "低"}[agree]
    if rec_mid == seat:
        return (LABEL_PREM_OK if seat == "premium" else LABEL_STD_OK), confidence
    saving = (cost_prem - cost_std) if seat == "premium" else (cost_std - cost_prem)
    if agree == 2 and saving >= min_saving:
        return (
            LABEL_STD_CAND if seat == "premium" else LABEL_PREM_CONSIDER
        ), confidence
    return LABEL_HOLD, confidence


def _preview_rows(
    members: pd.DataFrame,
    aggregate: pd.DataFrame,
    seat_by_email: dict[str, str],
    factor: float,
    cfg: dict,
) -> pd.DataFrame:
    """速報の全ユーザ行を構築する。"""
    min_saving = _min_saving(cfg)
    rows = []
    for email in sorted(set(members["email"]) | set(aggregate.index)):
        seat = seat_by_email.get(email, "unknown")
        row = aggregate.loc[email] if email in aggregate.index else None
        api_observed = float(row["api_cost"]) if row is not None else 0.0
        # billed は aggregate_month が常に付与するため row があれば必ず存在する
        billed_observed = float(row["billed"]) if row is not None else 0.0
        api_projected = api_observed * factor
        label, confidence = _preview_label(
            seat, api_observed, api_projected, cfg, min_saving
        )
        rows.append(
            {
                "email": email,
                "current_seat": seat,
                "api_cost_observed_usd": round(api_observed, 2),
                "api_cost_projected_usd": round(api_projected, 2),
                "billed_observed_usd": round(billed_observed, 2),
                "label": label,
                "confidence": confidence,
                **_detail_columns(row),
            }
        )
    return pd.DataFrame(rows)


def preview(
    input_dir: str | Path,
    month: str,
    cfg: dict,
    days_observed: int,
    org: str,
) -> PreviewResult:
    """部分月データの一次判断。対象月のみ使用し、ヒステリシス・変更推奨は行わない。"""
    input_dir = Path(input_dir)
    warnings: list[str] = []

    year, mon = (int(part) for part in month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    if not 1 <= days_observed <= days_in_month:
        raise ValueError(
            f"--days は 1〜{days_in_month}（{month} の暦日数）で指定してください"
        )
    factor = days_in_month / days_observed

    # 時点の違う入力が2つ以上あれば月中差分を発動する（重複警告の文言も変える）
    active = _diff_active(input_dir, month)
    spend_result = ingest.load_spend(
        input_dir, month, cfg, snapshot_active=active.spend
    )
    warnings.extend(spend_result.warnings)
    sources = {"spend": {month: str(spend_result.source)}}
    df = pricing.add_computed_cost(spend_result.df, cfg)
    warnings.extend(_warn_unknown_models(df["model"].unique(), cfg))

    is_user = df["email"].str.contains("@", na=False)
    basis, basis_notes = pricing.resolve_cost_basis(df[is_user], cfg)
    warnings.extend(basis_notes)
    df = pricing.apply_cost_basis(df, basis)
    org_service_observed = round(float(df[~is_user]["billed_usd"].sum()), 2)
    aggregate = aggregate_month(df[is_user]).set_index("email")

    members_result = ingest.load_members(
        input_dir, month, cfg, snapshot_active=active.members
    )
    warnings.extend(members_result.warnings)
    members = members_result.df
    sources["members"] = str(members_result.source)
    seat_by_email = members.set_index("email")["seat_type"].to_dict()

    # 活用度（任意ファイル code-analytics）。速報では詳細利用状況の LoC 列にしか使わない
    # 表示専用のデータで、一次判断には入らない。ロード時の指摘（採用ファイルの選択・
    # 任意カラムの欠落）は同じ入力に対して正式分析が出すため、速報の警告には足さない
    code_result = ingest.load_code_analytics(
        input_dir, month, cfg, snapshot_active=active.code
    )
    code_asof = None
    if code_result is not None:
        sources["code_analytics"] = str(code_result.source)
        code_asof = _code_asof(code_result.source)

    snapshot, code_diff, member_changes, diff_warnings = _midmonth_diffs(
        input_dir, month, cfg, seat_by_email
    )
    warnings.extend(diff_warnings)

    users = _preview_rows(members, aggregate, seat_by_email, factor, cfg)
    warnings.extend(_merge_members_info(users, input_dir, cfg, sources, month))
    _merge_code_analytics(users, code_result)
    # クレジットモード（速報は当月の観測実課金のみで billed_ever を判断）
    billed_ever = set(users.loc[users["billed_observed_usd"] > 0.0, "email"])
    _attach_credits_mode(users, billed_ever)

    warnings.extend(_warn_orphan_users(users))
    warnings.extend(_warn_active_unassigned(users, "api_cost_observed_usd"))
    warnings.extend(_credit_integrity_warnings(users, cfg, "billed_observed_usd"))

    summary = _seat_summary(users, cfg)
    summary.update(
        {
            "days_observed": days_observed,
            "days_in_month": days_in_month,
            "total_api_observed_usd": round(
                float(users["api_cost_observed_usd"].sum()), 2
            ),
            "total_api_projected_usd": round(
                float(users["api_cost_projected_usd"].sum()), 2
            ),
            "n_billed": int((users["billed_observed_usd"] > 0).sum()),
            "label_counts": users["label"].value_counts().to_dict(),
            "org_service_cost_usd": org_service_observed,
            "grant_suggested_cap_usd": _usage_credits_cfg(cfg)[
                "grant_suggested_cap_usd"
            ],
        }
    )
    summary.update(_credit_summary(users))
    credit_reach = _credit_reach_preview(
        users, days_observed, days_in_month, cfg, snapshot
    )
    upgrade = users["label"].isin([LABEL_PREM_CONSIDER, LABEL_HOLD])
    grant_candidates = _grant_candidates(
        users, upgrade, cfg, demand_col="api_cost_projected_usd"
    )
    users = _drop_unused_credit_columns(users, summary)
    return PreviewResult(
        month=month,
        users=users,
        summary=summary,
        days_observed=days_observed,
        days_in_month=days_in_month,
        org=org,
        warnings=warnings,
        sources=sources,
        snapshot=snapshot,
        code_diff=code_diff,
        member_changes=member_changes,
        credit_reach=credit_reach,
        grant_candidates=grant_candidates,
        code_asof=code_asof,
    )
