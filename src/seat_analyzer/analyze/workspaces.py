"""複数 workspace の組織を、workspace ごとの分析へ分けて束ねる（設計書 §26）。

1つの組織が複数の Team スペース（workspace）を運用する場合、集計・判定・ヒステリシス
はアカウント層＝(email, workspace) ごとに従来どおり行う。ここはその分割と、組織単位の
容れ物への束ね方だけを受け持つ。workspace をまたいだ人（email）単位の結合は後段の担当。

単一 workspace の組織は「workspace が1つの組織」として同じ経路を通り、中身の
AnalysisResult は analyze() の戻りと完全に同じ（成果物をバイト一致に保つため、
束ねる側は既存の分析に手を入れない）。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from .. import ingest
from .pipeline import AnalysisResult, analyze


@dataclass(frozen=True)
class WorkspaceContext:
    """1 workspace の運用設定（config の organizations.<組織>.workspaces から組む）。

    label は表示名で、config に書かれていなければディレクトリ名そのもの（読む側が
    「空なら名前」の分岐を持たなくて済むよう、ここで解決しておく）。fixed_seat は
    シート種別が運用方針で固定されている場合の種別、credit_limit_default_usd は
    そのアカウントの追加クレジット上限 κ の既定、evaluation_months は人の層の判定に
    必要な連続月数。いずれも未指定は None。
    """

    name: str
    primary: bool
    label: str
    fixed_seat: str | None
    credit_limit_default_usd: float | None
    evaluation_months: int | None


@dataclass
class OrgAnalysisResult:
    """1組織分の分析結果（workspace ごとの AnalysisResult を束ねたもの）。

    workspaces は主が先で、以降は名前の昇順。飛ばした workspace は含まない。
    contexts は config に書かれた全 workspace の運用設定（飛ばしたものも含む）で、
    skipped はまだ始まっていないため飛ばした workspace の名前（昇順）。
    warnings は組織単位の警告で、workspace ごとの警告は各 AnalysisResult が持つ。
    """

    org: str
    month: str
    primary: str
    workspaces: dict[str, AnalysisResult]
    contexts: dict[str, WorkspaceContext]
    skipped: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)


def _context(name: str, settings: dict) -> WorkspaceContext:
    """config の1エントリから WorkspaceContext を組む（値の検査はロード時に済み）。"""
    label = str(settings.get("label") or "")
    fixed_seat = str(settings.get("fixed_seat") or "")
    limit = settings.get("credit_limit_default_usd")
    months = settings.get("evaluation_months")
    return WorkspaceContext(
        name=name,
        primary=settings.get("primary") is True,
        label=label or name,
        fixed_seat=fixed_seat or None,
        credit_limit_default_usd=None if limit is None else float(limit),
        evaluation_months=None if months is None else int(months),
    )


def _single_context(org: str) -> WorkspaceContext:
    """従来レイアウト（workspace が1つ）の運用設定。名前は組織名で、常に主。"""
    return WorkspaceContext(
        name=org, primary=True, label=org, fixed_seat=None,
        credit_limit_default_usd=None, evaluation_months=None,
    )


def _primary_name(settings: dict[str, dict]) -> str | None:
    """primary: true がちょうど1つならその名前（0個・2個以上は None）。"""
    primaries = sorted(
        str(name) for name, entry in settings.items()
        if isinstance(entry, dict) and entry.get("primary") is True
    )
    return primaries[0] if len(primaries) == 1 else None


def _mismatch_message(org: str, missing: list[str], unexpected: list[str]) -> str:
    """config と入力ディレクトリの食い違いの説明（doctor の構造検査と同じ語）。"""
    detail = "、".join(part for part in (
        f"config にあるがディレクトリが無い: {'/'.join(missing)}" if missing else "",
        f"ディレクトリがあるが config に無い: {'/'.join(unexpected)}" if unexpected else "",
    ) if part)
    return (
        f"config.yaml > organizations.{org}.workspaces と入力ディレクトリが"
        f"一致しません（{detail}）。どちらかを直してください"
    )


def _ordered(names: list[str], primary: str) -> list[str]:
    """主 workspace を先頭に、以降を名前の昇順で並べる。"""
    return [primary, *[name for name in sorted(names) if name != primary]]


def analyze_org(
    input_dir: str | Path,
    month: str,
    cfg: dict,
    org: str,
    *,
    decision_context: bool = False,
    allow_missing: Collection[str] = (),
) -> OrgAnalysisResult:
    """1組織分の分析。input_dir はその組織の入力ディレクトリ。

    従来レイアウト（直下に spend/）では workspace を1つだけ持つ容れ物になり、中身は
    analyze() の戻りと完全に同じ。入れ子レイアウト（<workspace>/spend/）では config の
    workspaces と発見した名前を突き合わせてから workspace ごとに analyze() を呼ぶ。

    allow_missing は「対象月の spend が無くても需要 0 として続行してよい」workspace の
    名前（CLI の --allow-missing-workspace）。まだ始まっていない workspace（対象月
    以前に spend が1つも無い）は指定が無くても飛ばす（過去月の再生成を止めないため）。
    """
    org_input = Path(input_dir)
    # 混在レイアウト（直下と子の両方に spend/）はここで止まる
    layout = ingest.workspace_layout(org_input, org)
    settings = ingest.workspace_settings(cfg, org)

    if layout == ingest.WORKSPACE_LAYOUT_SINGLE:
        if settings:
            missing, unexpected = ingest.compare_workspaces([], settings)
            raise ValueError(_mismatch_message(org, missing, unexpected))
        result = analyze(
            org_input, month, cfg, org, decision_context=decision_context
        )
        return OrgAnalysisResult(
            org=org, month=month, primary=org,
            workspaces={org: result}, contexts={org: _single_context(org)},
        )

    found = ingest.discover_workspaces(org_input)
    # 名前はディレクトリ名として発見したものなので、組織名と同じ規則で検証する
    # （出力パスと表示に使う名前が、置いた環境によって壊れないようにする）
    ingest.validate_org_names(found)
    configured = sorted(settings)
    if not configured:
        raise ValueError(
            f"workspace ごとの spend/ がありますが、config.yaml > organizations.{org}"
            f".workspaces がありません（見つかった workspace: {'/'.join(found)}）。"
            "各 workspace を書き、primary: true をちょうど1つ付けてください"
        )
    missing, unexpected = ingest.compare_workspaces(found, configured)
    if missing or unexpected:
        raise ValueError(_mismatch_message(org, missing, unexpected))

    primary = _primary_name(settings)
    if primary is None:
        # 設定のロードが通常は止める条件。workspace_settings を直接組んだ場合でも、
        # どのシートを「その人の主アカウント」と読むかが決まらないまま進めない
        raise ValueError(
            f"config.yaml > organizations.{org}.workspaces で主 workspace が1つに"
            "決まりません（primary: true をちょうど1つ付けてください）"
        )

    contexts = {name: _context(name, settings[name]) for name in _ordered(configured, primary)}
    allowed = set(allow_missing)
    results: dict[str, AnalysisResult] = {}
    skipped: list[str] = []
    warnings: list[str] = []
    for name, context in contexts.items():
        workspace_dir = org_input / name
        months = ingest.discover_months(workspace_dir)
        if not [m for m in months if m <= month]:
            # まだ始まっていない workspace。過去月の再生成を止めないため飛ばす
            skipped.append(name)
            warnings.append(
                f"workspace {name} は {month} 以前のスペンドレポートが無いため対象外に"
                "しました（まだ利用が始まっていない workspace として飛ばしています）"
            )
            continue
        no_usage = month not in months
        if no_usage and name not in allowed:
            raise FileNotFoundError(
                f"workspace {name} に {month} のスペンドレポートがありません"
                f"（存在する月: {months}）。利用が無くエクスポートしなかった月なら "
                f"--allow-missing-workspace {name} を付けると需要 0 として続行できます"
            )
        results[name] = analyze(
            workspace_dir, month, cfg, org,
            decision_context=decision_context,
            workspace=context,
            # members-info は人単位の任意入力なので組織直下の1つを全 workspace で読む
            members_info_dir=org_input,
            assume_no_usage=no_usage,
        )
        if no_usage:
            warnings.append(
                f"workspace {name} は {month} のスペンドレポートが無いため需要 0 として"
                "分析しました（--allow-missing-workspace の指定による）"
            )
    return OrgAnalysisResult(
        org=org, month=month, primary=primary, workspaces=results,
        contexts=contexts, skipped=tuple(sorted(skipped)), warnings=warnings,
    )
