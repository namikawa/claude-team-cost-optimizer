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

import pandas as pd

from .. import ingest
from .persons import PersonLayer, build_person_layer, preview_persons
from .pipeline import NO_USAGE_SOURCE, AnalysisResult, analyze
from .preview import PreviewResult, preview


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
    persons は全 workspace のアカウントを email で束ねた人の層（§26.4〜§26.5）。
    first_seen は副 workspace → email → 払い出した月で、人の層と V2 の人ごとの履歴が読む。
    nested は入力が入れ子レイアウト（<組織>/<workspace>/spend/）だったか。主 workspace の
    入力ディレクトリを後段が組み立てるときに使う（レイアウトを検出し直さないため）。
    """

    org: str
    month: str
    primary: str
    workspaces: dict[str, AnalysisResult]
    contexts: dict[str, WorkspaceContext]
    skipped: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    persons: PersonLayer | None = None
    first_seen: dict[str, dict[str, str]] = field(default_factory=dict)
    nested: bool = False

    @property
    def has_multiple_workspaces(self) -> bool:
        """分析を飛ばした workspace も含め、複数 workspace の設定を持つか。"""
        return len(self.contexts) >= 2


@dataclass
class OrgPreviewResult:
    """1組織分の速報（workspace ごとの PreviewResult を束ねたもの）。

    org / month は組織名と対象月、primary は主 workspace の名前。
    workspaces は主が先で、以降は名前の昇順。飛ばした workspace は含まない。
    contexts は全 workspace の運用設定（飛ばしたものも含む）で、skipped は未開始のため
    飛ばした名前（昇順）。warnings は組織単位の警告で、個別の警告は PreviewResult が持つ。
    days_observed / days_in_month は共通の観測日数と暦日数（全 workspace を飛ばした場合の
    暦日数は 0）。persons は複数 workspace の組織の人別需要で、単一なら None。
    nested は入力が入れ子レイアウト（<組織>/<workspace>/spend/）だったか。
    """

    org: str
    month: str
    primary: str
    workspaces: dict[str, PreviewResult]
    contexts: dict[str, WorkspaceContext]
    days_observed: int
    days_in_month: int
    skipped: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    persons: pd.DataFrame | None = None
    nested: bool = False

    @property
    def has_multiple_workspaces(self) -> bool:
        """速報を飛ばした workspace も含め、複数 workspace の設定を持つか。"""
        return len(self.contexts) >= 2


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


def _resolve_contexts(
    org_input: Path, cfg: dict, org: str
) -> tuple[str, dict[str, WorkspaceContext]]:
    """入れ子の workspace を検証し、主を先頭とする運用設定を返す。"""
    found = ingest.discover_workspaces(org_input)
    # 名前はディレクトリ名として発見したものなので、組織名と同じ規則で検証する
    # （出力パスと表示に使う名前が、置いた環境によって壊れないようにする）
    ingest.validate_org_names(found)
    settings = ingest.workspace_settings(cfg, org)
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
    contexts = {name: _context(name, settings[name])
                for name in _ordered(configured, primary)}
    return primary, contexts


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
        single = OrgAnalysisResult(
            org=org, month=month, primary=org,
            workspaces={org: result}, contexts={org: _single_context(org)},
        )
        # 人の層は組織の結果そのものから組むので、容れ物を作ってから載せる
        # （workspace が1つなら人とアカウントは1対1で、§26.5 の判定は行わない）
        single.persons = build_person_layer(single, cfg)
        return single

    primary, contexts = _resolve_contexts(org_input, cfg, org)
    allowed = set(allow_missing)
    analyzed: dict[str, bool] = {}  # 分析する workspace → 対象月を需要 0 として扱うか
    skipped: list[str] = []
    warnings: list[str] = []
    for name in contexts:
        workspace_dir = org_input / name
        months = ingest.discover_months(workspace_dir)
        if not ingest.workspace_started(workspace_dir, month):
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
        analyzed[name] = no_usage
        if no_usage:
            warnings.append(
                f"workspace {name} は {month} のスペンドレポートが無いため需要 0 として"
                "分析しました（--allow-missing-workspace の指定による）"
            )

    def run(name: str, extra_demand: list[dict] | None = None) -> AnalysisResult:
        return analyze(
            org_input / name, month, cfg, org,
            decision_context=decision_context,
            workspace=contexts[name],
            # members-info は人単位の任意入力なので組織直下の1つを全 workspace で読む
            members_info_dir=org_input,
            assume_no_usage=analyzed[name],
            extra_demand=extra_demand or (),
        )

    # 副を先に分析し、その需要を主の判定へ渡す（複数アカウント保有者の主の行は
    # 全 workspace の合算需要で判定する・§26.4）。結果と警告の並びは contexts の順のまま
    secondaries = [name for name in analyzed if name != primary]
    results = {name: run(name) for name in secondaries}
    if primary in analyzed:
        results[primary] = run(primary, [results[name].monthly for name in secondaries])
    result = OrgAnalysisResult(
        org=org, month=month, primary=primary,
        workspaces={name: results[name] for name in contexts if name in results},
        contexts=contexts, skipped=tuple(sorted(skipped)), warnings=warnings,
        nested=True,
    )
    result.first_seen = {
        name: _first_seen(org_input / name, results[name], cfg) for name in secondaries
    }
    result.persons = build_person_layer(result, cfg, result.first_seen)
    return result


def preview_days(org_input: str | Path, month: str) -> int | None:
    """対象月の spend ファイル名から、組織で共通の観測日数を得る。

    org_input は組織の入力ディレクトリ。従来レイアウトでは採用ファイルの期間の日数を
    返し、入れ子では対象月の spend を持つ全 workspace の日数が同じときだけ採る。
    同一月に複数ファイルがある場合も、spend_file_period が採用する1本だけを見る。
    対象月のファイルが無い、または期間を持たない命名があれば None。日数が食い違う
    場合は ValueError（CLI は --days による明示指定を案内する）。
    """
    directory = Path(org_input)
    layout, names = ingest.detect_workspace_layout(directory)
    if layout == ingest.WORKSPACE_LAYOUT_MIXED:
        ingest.workspace_layout(directory, directory.name)
    if layout == ingest.WORKSPACE_LAYOUT_SINGLE:
        period = ingest.spend_file_period(directory, month)
        return period.days if period else None
    days = {}
    for name in names:
        period = ingest.spend_file_period(directory / name, month)
        if period is not None:
            days[name] = period.days
    if not days or any(value is None for value in days.values()):
        return None
    if len(set(days.values())) != 1:
        detail = " / ".join(f"{name}: {value} 日" for name, value in sorted(days.items()))
        raise ValueError(
            f"workspace ごとの観測日数が違います（{detail}）。"
            "--days で観測日数を指定してください"
        )
    return next(iter(days.values()))


def preview_org(
    input_dir: str | Path, month: str, cfg: dict, days_observed: int, org: str
) -> OrgPreviewResult:
    """1組織分の速報。input_dir は組織の入力ディレクトリ。

    従来レイアウトでは preview() を1回呼んで包む。入れ子では config と発見した
    workspace を突き合わせ、days_observed を共通の観測日数として分析する。
    対象月以前に spend が無い workspace は未開始として警告して飛ばす。開始済みで
    対象月の spend が無い workspace は FileNotFoundError で止める（需要 0 にしない）。

    副を先に計算し、各結果の own_demand を email ごとに足した未丸めの需要を、主の
    extra_demand に渡す。主が未開始なら副だけの結果になる。複数 workspace の組織
    では人別需要も組み、単一では従来の出力を保つため持たない。
    """
    org_input = Path(input_dir)
    layout = ingest.workspace_layout(org_input, org)
    settings = ingest.workspace_settings(cfg, org)
    if layout == ingest.WORKSPACE_LAYOUT_SINGLE:
        if settings:
            missing, unexpected = ingest.compare_workspaces([], settings)
            raise ValueError(_mismatch_message(org, missing, unexpected))
        result = preview(org_input, month, cfg, days_observed, org)
        return OrgPreviewResult(
            org, month, org, {org: result}, {org: _single_context(org)},
            days_observed, result.days_in_month,
        )

    primary, contexts = _resolve_contexts(org_input, cfg, org)
    analyzed = []
    skipped = []
    warnings = []
    for name in contexts:
        workspace_dir = org_input / name
        months = ingest.discover_months(workspace_dir)
        if not ingest.workspace_started(workspace_dir, month):
            skipped.append(name)
            warnings.append(
                f"workspace {name} は {month} 以前のスペンドレポートが無いため対象外に"
                "しました（まだ利用が始まっていない workspace として飛ばしています）"
            )
            continue
        if month not in months:
            raise FileNotFoundError(
                f"workspace {name} に {month} のスペンドレポートがありません"
                f"（存在する月: {months}）。速報は欠月の workspace を需要 0 として"
                "扱わないため、始まっている全 workspace に対象月のスペンドレポートが必要です"
            )
        analyzed.append(name)

    secondaries = [name for name in analyzed if name != primary]
    results = {
        name: preview(org_input / name, month, cfg, days_observed, org,
                      workspace=contexts[name], members_info_dir=org_input)
        for name in secondaries
    }
    if primary in analyzed:
        extra: dict[str, float] = {}
        for name in secondaries:
            for email, demand in results[name].own_demand.items():
                extra[email] = extra.get(email, 0.0) + demand
        results[primary] = preview(
            org_input / primary, month, cfg, days_observed, org,
            workspace=contexts[primary], members_info_dir=org_input,
            extra_demand=extra,
        )
    ordered = {name: results[name] for name in contexts if name in results}
    days_in_month = next(iter(ordered.values())).days_in_month if ordered else 0
    result = OrgPreviewResult(
        org, month, primary, ordered, contexts, days_observed, days_in_month,
        skipped=tuple(sorted(skipped)), warnings=warnings, nested=True,
    )
    if result.has_multiple_workspaces:
        result.persons = preview_persons(result, cfg)
    return result


def single_org_result(result: AnalysisResult) -> OrgAnalysisResult:
    """1 workspace の分析結果を、workspace が1つの組織の容れ物に包む。

    組織単位の集計（summarize_org）しか読まない側が、従来の AnalysisResult と
    OrgAnalysisResult を同じ形で扱えるようにするためのもの。人の層は設定が無いと
    組めないので持たない（summarize_org は人数をアカウント数で代える）。
    """
    return OrgAnalysisResult(
        org=result.org, month=result.month, primary=result.org,
        workspaces={result.org: result},
        contexts={result.org: _single_context(result.org)},
    )


# 組織単位の集計で workspace ごとに足し合わせる数値（summary のキー）。
# 件数は人ではなくアカウント単位（同じ人が2つの workspace で変更推奨なら2件）
_SUMMED_KEYS = (
    "n_members", "n_standard", "n_premium", "n_unassigned", "n_unknown",
    "seat_cost_now_usd", "total_api_cost_usd", "total_billed_extra_usd",
    "org_service_cost_usd", "n_change_recommended", "est_monthly_saving_usd",
    "n_watching", "n_cap_suspected",
)
# 金額のキー（足した後に小数2桁へ丸める）
_USD_KEYS = frozenset(key for key in _SUMMED_KEYS if key.endswith("_usd"))


def _assumed_no_usage(result: AnalysisResult) -> bool:
    """対象月の spend を読まずに需要 0 として分析した結果か。"""
    spend = result.sources.get("spend")
    return isinstance(spend, dict) and spend.get(result.month) == NO_USAGE_SOURCE


def summarize_org(org: OrgAnalysisResult) -> dict:
    """組織単位の集計（report と CLI が同じ値を読むための純粋関数）。

    戻り値のキー:
      - n_persons: 人数（人の層の人数。人の層が無ければアカウント数）
      - n_accounts: アカウント数（各 workspace のメンバー数の和）
      - workspaces: config の順（主が先）の行。分析を飛ばした workspace の数値は None
      - total: 数値キーの和と、product ごとに足した org_service_by_product

    需要の合計は各 workspace の total_api_cost_usd（そのアカウント自身の需要）の和なので、
    複数アカウント保有者の需要を二重に数えない。変更推奨・要観察・上限到達疑いの件数は
    アカウント単位で、人数ではない。
    """
    rows = []
    total = dict.fromkeys(_SUMMED_KEYS, 0)
    by_product: dict[str, float] = {}
    for name, context in org.contexts.items():
        result = org.workspaces.get(name)
        row: dict = {
            "name": name,
            "label": context.label,
            "primary": context.primary,
            "fixed_seat": context.fixed_seat,
            "skipped": result is None,
            "assume_no_usage": result is not None and _assumed_no_usage(result),
        }
        if result is None:
            row.update(dict.fromkeys(_SUMMED_KEYS))
            row["months_used"] = None
            rows.append(row)
            continue
        summary = result.summary
        for key in _SUMMED_KEYS:
            value = summary.get(key, 0) or 0
            row[key] = value
            total[key] += value
        row["months_used"] = list(summary.get("months_used", result.months_used))
        for product, value in (summary.get("org_service_by_product") or {}).items():
            by_product[str(product)] = by_product.get(str(product), 0.0) + float(value)
        rows.append(row)
    for key in _USD_KEYS:
        total[key] = round(float(total[key]), 2)
    total["org_service_by_product"] = {
        product: round(value, 2) for product, value in sorted(by_product.items())
    }
    n_accounts = int(total["n_members"])
    persons = org.persons
    return {
        "n_persons": len(persons.persons) if persons is not None else n_accounts,
        "n_accounts": n_accounts,
        "workspaces": rows,
        "total": total,
    }


def _members_evidence_applies(source: Path, month: str) -> bool:
    """その月の在籍の証拠として、採用された members ファイルを使ってよいか。

    load_members は対象月末に最も近いスナップショットを返すので、その月のファイルが
    無ければ後の月のものが返る。それを在籍の証拠にすると払い出した月が実際より早まり、
    完全月を多く数えてシートを外す側（戻す候補）へ倒れるため、末日以前のファイルと、
    月末直後の通常運用の範囲に入るファイルだけを証拠にする（日数の条件は
    ingest.is_near_month_end に閉じる）。ファイル名から期間を解釈できないファイルは
    時点が決まらないので従来どおり証拠として使う。

    ファイルの期間は月をまたがない（またぐ命名は ingest が読んだ時点で止まる）ので、
    末日以前かはそのファイルの月と対象月の比較で決まる。
    """
    period = ingest.file_period(source)
    if period is None:
        return True
    return period.month <= month or ingest.is_near_month_end(source, month)


def _first_seen(workspace_dir: Path, result: AnalysisResult,
                cfg: dict) -> dict[str, str]:
    """その workspace で各 email が最初に現れた月（email → 月）。

    spend に行がある月と、その月の在籍として使える members スナップショットに載って
    いる月の早い方を「払い出した月」とする（利用が無いまま在籍した月も払い出し済みと
    して数える）。ロード時の警告は捨てる（対象月のぶんは分析本体が既に出している）。
    """
    seen: dict[str, str] = {}
    for current_month in result.months_used:
        emails = set(result.monthly[current_month]["email"])
        members = ingest.load_members(workspace_dir, current_month, cfg)
        if _members_evidence_applies(members.source, current_month):
            emails |= set(members.df["email"])
        for email in sorted(emails):
            seen.setdefault(str(email), current_month)
    return seen
