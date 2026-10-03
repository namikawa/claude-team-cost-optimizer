"""部分月データから一次判断を作る速報パイプライン。"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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
    STATUS_FIXED_SEAT,
    STATUS_UNKNOWN,
    _code_asof,
    _detail_columns,
    _merge_code_analytics,
    _merge_members_info,
    _min_saving,
    _recommend,
    _seat_summary,
    _warn_active_unassigned,
    _warn_fixed_seat_mismatch,
    _warn_orphan_users,
    _warn_unknown_models,
    aggregate_month,
)

if TYPE_CHECKING:
    from .workspaces import WorkspaceContext


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
    # 自身の未丸めの観測需要（email → USD）。extra_demand の有無によらず、spend に
    # 行の無い人は含まない（読む側では需要 0 として扱う）
    own_demand: dict[str, float] = field(default_factory=dict)


def _preview_label(
    seat: str,
    api_obs: float,
    api_proj: float,
    cfg: dict,
    min_saving: float,
    fixed_seat: str | None = None,
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
    if fixed_seat and seat in ("standard", "premium"):
        return STATUS_FIXED_SEAT, "—"
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
    extra_demand: Mapping[str, float] | None = None,
    fixed_seat: str | None = None,
) -> pd.DataFrame:
    """速報の全ユーザ行を構築する。"""
    min_saving = _min_saving(cfg)
    rows = []
    for email in sorted(set(members["email"]) | set(aggregate.index)):
        seat = seat_by_email.get(email, "unknown")
        row = aggregate.loc[email] if email in aggregate.index else None
        own_observed = float(row["api_cost"]) if row is not None else 0.0
        api_observed = own_observed + (extra_demand or {}).get(email, 0.0)
        # billed は aggregate_month が常に付与するため row があれば必ず存在する
        billed_observed = float(row["billed"]) if row is not None else 0.0
        api_projected = api_observed * factor
        label, confidence = _preview_label(
            seat, api_observed, api_projected, cfg, min_saving, fixed_seat
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
    workspace: WorkspaceContext | None = None,
    members_info_dir: str | Path | None = None,
    extra_demand: Mapping[str, float] | None = None,
) -> PreviewResult:
    """1 workspace 分の速報。対象月だけを使い、ヒステリシス・変更推奨は行わない。

    input_dir は spend/ 等を直下に持つ入力ディレクトリ。days_observed は全 workspace
    で共通の観測日数（対象月の暦日数以内）で、需要を月末ペースに換算するために使う。
    任意引数の既定値では従来レイアウトの速報と同じ挙動になる。

    workspace はその workspace の運用設定（κ の既定値・固定シート）。
    members_info_dir は members-info を置くディレクトリ（既定は input_dir。入れ子
    レイアウトでは組織直下で、人単位の情報は workspace ごとに分けない）。
    extra_demand は他 workspace の未丸めの観測需要（email → USD）。主の行の判定需要
    にだけ足し、月末ペースへ換算する。members と spend に無い email の行は作らない。
    """
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
    own_demand = {str(email): float(value) for email, value in
                  aggregate["api_cost"].items()}

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

    # κ の月中変更は、そのアカウントの κ を members-info が決める workspace
    # （単一 workspace の組織と主）だけを検出の対象にする
    info_dir = Path(members_info_dir) if members_info_dir is not None else input_dir
    credit_limit_dir = info_dir if workspace is None or workspace.primary else None
    snapshot, code_diff, member_changes, diff_warnings = _midmonth_diffs(
        input_dir, month, cfg, seat_by_email, credit_limit_dir=credit_limit_dir
    )
    warnings.extend(diff_warnings)

    users = _preview_rows(members, aggregate, seat_by_email, factor, cfg,
                          extra_demand, workspace.fixed_seat if workspace else None)
    warnings.extend(_merge_members_info(users, info_dir, cfg, sources, month,
                                        workspace=workspace))
    _merge_code_analytics(users, code_result)
    # クレジットモード（速報は当月の観測実課金のみで billed_ever を判断）
    billed_ever = set(users.loc[users["billed_observed_usd"] > 0.0, "email"])
    _attach_credits_mode(users, billed_ever)

    warnings.extend(_warn_orphan_users(users))
    own_users = _own_demand_users(users, own_demand, factor)
    warnings.extend(_warn_active_unassigned(own_users, "api_cost_observed_usd"))
    warnings.extend(_warn_fixed_seat_mismatch(users, workspace))
    warnings.extend(_credit_integrity_warnings(users, cfg, "billed_observed_usd"))

    summary = _seat_summary(users, cfg)
    summary.update(
        {
            "days_observed": days_observed,
            "days_in_month": days_in_month,
            "total_api_observed_usd": round(
                float(own_users["api_cost_observed_usd"].sum()), 2
            ),
            "total_api_projected_usd": round(
                float(own_users["api_cost_projected_usd"].sum()), 2
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
        users, upgrade & (users["label"] != STATUS_FIXED_SEAT), cfg,
        demand_col="api_cost_projected_usd"
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
        own_demand=own_demand,
    )


def _own_demand_users(users: pd.DataFrame, own_demand: Mapping[str, float],
                      factor: float) -> pd.DataFrame:
    """判定用の需要列だけを自身の観測需要と換算需要に差し替える。"""
    users = users.copy()
    users["api_cost_observed_usd"] = users["email"].map(
        lambda email: round(own_demand.get(email, 0.0), 2)
    )
    users["api_cost_projected_usd"] = users["email"].map(
        lambda email: round(own_demand.get(email, 0.0) * factor, 2)
    )
    return users


def preview_own_demand_users(result: PreviewResult) -> pd.DataFrame:
    """判定用の合算需要を各アカウント自身の観測需要に戻した表示用の複製。"""
    return _own_demand_users(
        result.users, result.own_demand,
        result.days_in_month / result.days_observed,
    )
