"""既存入力（Spend / Members / GitHub / OpenTelemetry）の検査と、品質issueの整列・JSON直列化。

`inspect_input` は doctor の検査本体で、1組織・1対象月の入力を読み、issueを返す
だけの純粋な関数（出力整形と終了コードは cli が担当）。同じ入力からは常にバイト
一致の出力を得られるよう、messageには絶対パス・時刻・乱数を入れない。

`workspace_issues` は複数のTeamスペースを運用する組織のための構造の検査で、入力
ディレクトリの形と設定の `organizations.<組織名>.workspaces` を突き合わせる。対象月に
依存しないので、読むのはディレクトリの並びと設定だけ。組織ディレクトリごと無い設定は
この検査に現れないので、`workspace_config_issues` が設定側から突き合わせる。

`inspect_github` と `github_config_issues` は GitHub 分析を有効にした組織のための検査で、
gh の実行結果（`github_collect.probe_github` が返す値）と対応表を issue へ写す。gh を
呼ぶのは probe 側だけなので、この2つも同じ入力から常に同じ出力を返す。

`inspect_otel` は OpenTelemetry を有効にした組織の月次キャッシュと spend を読み、
`otel_coverage` が求めた網羅率を issue へ写す。どちらも BigQuery に接続しない。

整列と直列化は別の関心事として分けてある。`issues_to_json` は渡された順をそのまま
保持するため、「同一のissue多重集合なら常に同一の文字列」は `sort_issues` を通した
場合に成立する不変条件であり、直列化関数単体の性質ではない。機械可読出力は整列を
呼び出し側の記述に依存させないよう、正準順序で直列化する
`issues_to_canonical_json` を境界として使う。

`analyze` の文字列warningはこの検査とは独立に従来どおり出る。両者を統合するのは
このStepでは行わない。
"""

from __future__ import annotations

import calendar
import dataclasses
import enum
import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd

from . import calendar_months, github_collect, ingest, otel_collect, pricing
from .domain import IssueCode, QualityIssue, ScopeValue, Severity
from .github_collect import (
    GhAuth,
    GhFailure,
    GhOrgAccess,
    GhRateResource,
    GithubMembers,
    GithubProbes,
)
from .product_usage import normalize_product_name

# Severityの宣言順（ERROR→WARNING）をそのまま整列順に使う
_SEVERITY_ORDER = {severity: index for index, severity in enumerate(Severity)}


def issue_to_dict(issue: QualityIssue) -> dict[str, object]:
    """issueをJSON化可能なdictへ変換する。

    キー順はseverity, code, message, scopeで固定。scopeはキーの辞書順に並べ、
    正準化されたtupleはlistへ戻す。
    """
    scope = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in sorted(issue.scope.items())
    }
    return {
        "severity": issue.severity.value,
        "code": issue.code.value,
        "message": issue.message,
        "scope": scope,
    }


def issues_to_json(issues: Iterable[QualityIssue]) -> str:
    """issue列を反復順のままJSON文字列へ直列化する（入力順の正準化はしない）。

    同値のissueが同じ順序・同じ件数で与えられれば、返る文字列は一致する。並び順を
    入力に依存させたくない場合は issues_to_canonical_json を使う。日本語のmessageは
    エスケープせずそのまま出力し、NaN・inf はJSON側でも拒否する。
    """
    return json.dumps(
        [issue_to_dict(issue) for issue in issues],
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    )


def issues_to_canonical_json(issues: Iterable[QualityIssue]) -> str:
    """issue列を正準順序で直列化する（機械可読出力はこちらを使う）。

    同一のissue多重集合からは、構築順・検出順・scopeキーの挿入順によらず同一の文字列
    が返る。低水準の issues_to_json は指定順を保持するため、整列を呼び出し側の記述に
    依存させないための境界としてこの関数を通す。
    """
    return issues_to_json(sort_issues(issues))


def _sort_key(issue: QualityIssue) -> tuple[int, str, str, str]:
    # scopeはissue_to_dictの時点でキー順が確定しているため、そのまま文字列化する。
    # JSON表現はtrue・1・1.0・-0.0を書き分けるため、QualityIssueの等価性と
    # 同じ粒度になる（等価なissueだけが同じ整列キーを持つ）
    scope_repr = json.dumps(
        issue_to_dict(issue)["scope"],
        ensure_ascii=False,
        allow_nan=False,
    )
    return (
        _SEVERITY_ORDER[issue.severity],
        issue.code.value,
        issue.message,
        scope_repr,
    )


def sort_issues(issues: Iterable[QualityIssue]) -> list[QualityIssue]:
    """severity, code, message, scopeの順で全順序に整列する。

    同値のissueを除く全てのissueに順序が付くため、入力順によらず結果は同一になる。
    """
    return sorted(issues, key=_sort_key)


# --- 既存入力（Spend / Members）の検査 ---

# 数値解釈の失敗を検出する対象列。必須列に限る（任意列は「列が無いためNA補完された」
# 状態と区別できず、欠損を解釈失敗として報告できない）
_NUMERIC_REQUIRED = ("prompt_tokens", "completion_tokens")

# messageへ載せる代表例の上限。総数はscopeで持つ
_MAX_LISTED = 5


def _issue(
    severity: Severity,
    code: IssueCode,
    message: str,
    org: str | None,
    month: str | None = None,
    *,
    workspace: str | None = None,
    **scope: ScopeValue,
) -> QualityIssue:
    """org・month・workspaceを先頭に置いたscopeでissueを作る。Noneのキーは省く。

    org=Noneは、どの組織にも属さないissueに限る（対象組織を解決する前の失敗＝
    input_unavailable_issues と、設定だけの問題＝github_config_issues /
    workspace_config_issues）。
    組織単位の検査は常にorgを持つ。workspaceは複数のTeamスペースを運用する組織
    （入れ子レイアウト）でだけ付く。
    """
    base: dict[str, ScopeValue] = {}
    if org is not None:
        base["org"] = org
    if month is not None:
        base["month"] = month
    if workspace is not None:
        base["workspace"] = workspace
    base.update(scope)
    return QualityIssue(severity=severity, code=code, message=message, scope=base)


_INPUT_DIR_LABEL = "入力ディレクトリ"


def _reason(exc: Exception, input_dir: Path) -> str:
    """例外メッセージを1行かつ実行環境に依存しない文字列へ整える。

    ingestの例外は呼び出し時の入力ディレクトリをそのまま含むため、そのままでは
    messageが実行環境に依存する（domain.QualityIssueの決定性制約）。配下のパスは
    入力ディレクトリからの相対表記へ、入力ディレクトリ自身は固定の語へ置き換える。

    置換対象は絶対パスだけにする。相対指定（`input`・`.`・`a` 等）はそれ自体が実行環境に
    依存せず決定的で、素朴な部分文字列置換だと例外文中の無関係な語（列名・英単語・
    ピリオド）まで壊すため触らない。ただし相対指定でも絶対表記が例外文に現れ得るため、
    symlinkを解決しない `absolute()` と解決後の `resolve()` の両方を候補に入れる
    （相対symlinkや `..` を含む指定で両者は一致しない）。
    """
    flat = " ".join(str(exc).split())
    bases = sorted(
        (
            base for base in {
                str(input_dir), str(input_dir.absolute()), str(input_dir.resolve())
            }
            if Path(base).is_absolute()
        ),
        # 長い候補から置換する（短い候補が長い候補の前方部分になりうる）。同じ長さの
        # 候補は辞書順で確定させる: set の反復順はハッシュシードに依存し、置換は逐次
        # なので、順序が変わると message が実行ごとに変わりうる（symlink 名と実体名が
        # 同じ長さのとき absolute() と resolve() が同長の別パスになる）
        key=lambda base: (-len(base), base),
    )
    # 区切りは os.sep（Windows なら "\\"）と "/" の両方を見る。例外文のパスは OS 由来
    # のものとコード中で "/" を繋いだものが混在し、片方だけだと相対表記へ落ちない。
    # 順序を固定するため set ではなく dict.fromkeys で重複を除く（置換は逐次なので、
    # 反復順が変わると結果が変わりうる。message は同一入力から常にバイト一致にする）
    separators = dict.fromkeys(("/", os.sep))
    for base in bases:
        for sep in separators:
            flat = flat.replace(base + sep, "")
    for base in bases:
        flat = flat.replace(base, _INPUT_DIR_LABEL)
    return " ".join(flat.split()).strip()


_prev_month = calendar_months.previous_month


def _days_in_month(month: str) -> int:
    year, mon = (int(x) for x in month.split("-"))
    return calendar.monthrange(year, mon)[1]


def _judgment_window(month: str, n_months: int) -> list[str]:
    """対象月を含む暦上の判定窓を昇順で返す。"""
    return list(calendar_months.judgment_window(month, n_months))


def _sample(emails: Iterable[str]) -> list[str]:
    """messageへ載せる代表例。入力順に依存しないよう整列してから先頭を採る。"""
    return sorted(emails)[:_MAX_LISTED]


class _MonthCoverage(enum.Enum):
    """ファイル名の期間から見た spend の月の状態。

    FULL は暦上の全月、PARTIAL は暦の日数に満たない部分月、UNCONFIRMED は
    単日の日付の命名で期間を確認できない月。`_partial_month_issues` の部分月の
    警告と `inspect_otel` の網羅率の検査で同じ判定を使う。
    """

    FULL = enum.auto()
    PARTIAL = enum.auto()
    UNCONFIRMED = enum.auto()


def _month_coverage(period: ingest.FilePeriod, month: str) -> _MonthCoverage:
    """ファイル名が示す期間で、暦上の全月と確かめられるかを返す。"""
    if period.kind == "date":
        return _MonthCoverage.UNCONFIRMED
    if period.days is None or period.days >= _days_in_month(month):
        return _MonthCoverage.FULL
    return _MonthCoverage.PARTIAL


def _partial_month_issues(
    input_dir: Path, month: str, spend_months: list[str], cfg: dict, org: str
) -> list[QualityIssue]:
    """対象月までの各月で、全月データと確認できないスペンドを警告する。

    kind=range は期間の日数で判定し、v1 は判定窓の内外で変更推奨への影響を伝える。
    v2 は対象月の保留と、過去の部分月が完全月に数えられないことを伝える。
    kind=date（単日日付の命名）は実際の集計期間がファイル名から分からないため、
    日数を断定せず「確認できない」として警告する。
    kind=month（spend_YYYY-MM.csv）は全月の指定として扱い警告しない。
    """
    issues: list[QualityIssue] = []
    window = _judgment_window(month, int(cfg["decision"]["hysteresis_months"]))
    for m in [x for x in spend_months if x <= month]:
        try:
            period = ingest.spend_file_period(input_dir, m)
        except (OSError, ValueError) as exc:
            # 月の一覧を得た後にファイルが変化した場合。例外を外へ出さず issue にする
            issues.append(_issue(
                Severity.ERROR, IssueCode.MISSING_SPEND,
                f"{m} のスペンドを再確認できません: {_reason(exc, input_dir)}", org, m,
            ))
            continue
        if period is None:
            # 一覧にあった月が引き当てられない = 検査中にファイルが消えた・名前が変わった。
            # 黙って通すと「問題なし」に見えるため、状態の不整合として報告する
            issues.append(_issue(
                Severity.ERROR, IssueCode.MISSING_SPEND,
                f"{m} のスペンドが検査中に見つからなくなりました"
                "（月の一覧を得た後にファイルが変化した可能性）。再実行してください",
                org, m,
            ))
            continue
        coverage = _month_coverage(period, m)
        if coverage is _MonthCoverage.FULL:
            continue
        days_in_month = _days_in_month(m)
        if coverage is _MonthCoverage.UNCONFIRMED:
            issues.append(_issue(
                Severity.WARNING, IssueCode.PARTIAL_MONTH,
                f"{m} のスペンドはファイル名が単日日付（{period.start:%m-%d}）のため、"
                f"暦上 {days_in_month}日の全月データであることを確認できません。"
                "部分月であれば月額前提の判定で需要が過小評価されます"
                "（月中の一次判断は analyze --preview を使ってください）",
                org, m, snapshot_date=f"{period.start:%Y-%m-%d}", days_in_month=days_in_month,
            ))
            continue
        note = (
            "この月が判定窓に入るあいだは、正式分析で変更推奨を出しません"
            "（月中の一次判断は analyze --preview を使ってください）"
            if m in window else
            "判定窓の外の月なので、変更推奨の判定には使いません"
        )
        if cfg["decision"]["version"] == "v2":
            note = (
                "対象月が部分月なので、正式分析は判定を保留します"
                "（月中の一次判断は analyze --preview を使ってください）"
                if m == month else
                "部分月は完全月に数えません（判定に要る完全月が足りない人は判定を保留します）"
            )
        issues.append(_issue(
            Severity.WARNING, IssueCode.PARTIAL_MONTH,
            f"{m} は部分月データです（{period.start:%m-%d}〜{period.end:%m-%d} の "
            f"{period.days}日分 / 暦上 {days_in_month}日）。{note}",
            org, m, days_observed=period.days, days_in_month=days_in_month,
        ))
    return issues


def _history_gap_issues(
    month: str, spend_months: list[str], cfg: dict, org: str
) -> list[QualityIssue]:
    """ヒステリシス窓（対象月を含む直近 N ヶ月）に欠けている月を警告する。"""
    n_hyst = int(cfg["decision"]["hysteresis_months"])
    window = _judgment_window(month, n_hyst)[:-1]
    oldest = min(spend_months) if spend_months else month
    missing = [m for m in window if oldest <= m and m not in spend_months]
    if not missing:
        return []
    if cfg["decision"]["version"] == "v2":
        message = (
            f"直近 {n_hyst} ヶ月のうち {'/'.join(missing)} のスペンドレポートがありません。"
            "欠月より前の月は判定の履歴に入らないため、判定に要る完全月が足りない人は"
            "「要観察（データ蓄積待ち）」になります"
        )
    else:
        message = (
            f"ヒステリシス判定に必要な直近 {n_hyst} ヶ月のうち "
            f"{'/'.join(missing)} のスペンドレポートがありません。"
            "欠月があるあいだは変更推奨を出さず、推奨が現シートと違うユーザは"
            "「要観察（データ蓄積待ち）」になります"
        )
    return [_issue(
        Severity.WARNING, IssueCode.MISSING_HISTORY_MONTH,
        message,
        org, month, missing_months=missing, hysteresis_months=n_hyst,
    )]


def _spend_content_issues(
    spend: pd.DataFrame, cfg: dict, org: str, month: str
) -> list[QualityIssue]:
    """対象月スペンドの中身（モデル単価・数値解釈）を検査する。"""
    issues: list[QualityIssue] = []
    # 空のmodelセルは str(nan) として単価表に一致せず default 単価が当たるが、
    # unmatched_models は欠損を一覧から除くため、空セルは行数として別に数える
    stripped = spend["model"].astype("string").str.strip()
    blank_model = (stripped.isna() | (stripped == "")).fillna(True).astype(bool)
    blank_rows = int(blank_model.sum())
    unknown_models = pricing.unmatched_models(spend.loc[~blank_model, "model"].unique(), cfg)
    if unknown_models or blank_rows:
        detail = "、".join(
            part for part in (
                "/".join(unknown_models),
                f"model が空の {blank_rows}行" if blank_rows else "",
            ) if part
        )
        issues.append(_issue(
            Severity.WARNING, IssueCode.UNKNOWN_MODEL,
            f"単価表に一致せず default 単価が適用されます: {detail}。"
            "config.yaml > model_prices にパターンを追記してください"
            "（model が空の行は入力側の欠損を確認してください）",
            org, month, models=unknown_models, blank_model_rows=blank_rows,
        ))
    failed = {
        column: int(spend[column].isna().sum())
        for column in _NUMERIC_REQUIRED
        if column in spend.columns
    }
    failed = {column: count for column, count in failed.items() if count}
    if failed:
        detail = ", ".join(f"{column} {count}行" for column, count in sorted(failed.items()))
        # 1行で複数列が失敗し得るため、影響行数はセル数の合計と別に数える
        rows = int(spend[sorted(failed)].isna().any(axis=1).sum())
        issues.append(_issue(
            Severity.WARNING, IssueCode.NUMERIC_PARSE_FAILED,
            f"数値として解釈できない値があります（{detail} / 影響 {rows}行）。"
            "0 として集計されるため需要が過小評価されます",
            org, month, columns=sorted(failed), rows=rows, cells=sum(failed.values()),
        ))
    return issues


def _seat_type_issues(
    members: pd.DataFrame, org: str, month: str
) -> list[QualityIssue]:
    """シート種別を判別できないメンバーを警告する。"""
    unknown = members.loc[members["seat_type"] == "unknown", "email"]
    if unknown.empty:
        return []
    emails = _sample(unknown)
    return [_issue(
        Severity.WARNING, IssueCode.SEAT_TYPE_UNKNOWN,
        f"シート種別を判別できないメンバーが {len(unknown)} 名います"
        f"（値に premium/standard/unassigned を含まない。例: {', '.join(emails)}）。"
        "シート不明として集計され、シート判定ができません",
        org, month, members=len(unknown), emails=emails,
    )]


def _join_issues(
    spend: pd.DataFrame, members: pd.DataFrame, cfg: dict, org: str, month: str
) -> list[QualityIssue]:
    """Spendとメンバー一覧の突き合わせ不整合を検査する。"""
    issues: list[QualityIssue] = []
    users = spend[spend["email"].str.contains("@", na=False)]
    cost_by_email = (
        pricing.add_computed_cost(users, cfg).groupby("email")["computed_cost_usd"].sum()
    )
    member_emails = set(members["email"])

    orphan = sorted(set(cost_by_email.index) - member_emails)
    if orphan:
        issues.append(_issue(
            Severity.WARNING, IssueCode.MEMBER_ROW_MISSING,
            f"スペンドに行があるがメンバー一覧に居ないユーザが {len(orphan)} 名います"
            f"（例: {', '.join(orphan[:_MAX_LISTED])}）。"
            "シート不明として集計され、シート判定ができません"
            "（メンバー一覧のエクスポート漏れ、または月中の退去の可能性）",
            org, month, users=len(orphan), emails=orphan[:_MAX_LISTED],
        ))

    unassigned = set(members.loc[members["seat_type"] == "unassigned", "email"])
    active = set(cost_by_email[cost_by_email > 0].index)
    active_unassigned = sorted(unassigned & active)
    if active_unassigned:
        issues.append(_issue(
            Severity.WARNING, IssueCode.UNASSIGNED_WITH_USAGE,
            f"シート未割当なのに利用実績があるユーザが {len(active_unassigned)} 名います"
            f"（例: {', '.join(active_unassigned[:_MAX_LISTED])}）。"
            "メンバー一覧の更新漏れ、または月中のシート解除の可能性があります",
            org, month,
            users=len(active_unassigned), emails=active_unassigned[:_MAX_LISTED],
        ))
    return issues


def input_unavailable_issues(input_dir: Path | str, exc: OSError) -> list[QualityIssue]:
    """入力ディレクトリ自体を読めない場合のissue（対象組織を解決する前の失敗）。

    組織が特定できないためscopeにorgを持たない。cli が対象解決の失敗をJSONへ載せるために使う。
    """
    return [_issue(
        Severity.ERROR, IssueCode.MISSING_SPEND,
        f"入力データを検査できません: {_reason(exc, Path(input_dir))}", None,
    )]


def _members_presence_issues(
    input_dir: Path, cfg: dict, org: str
) -> list[QualityIssue]:
    """対象月が決まらない場合の、メンバー一覧の有無と可読性の検査。

    最新月の候補を実際にロードし、対象月ありの経路と同じ「読めない」「データ行が無い」を
    errorにする（この経路だけ検査が緩くならないようにする）。同一月に複数ある場合の採用は
    ingest.load_members に委ねて、対象月ありの経路と同じ規則で選ぶ。
    """
    directory = input_dir / "members"
    try:
        months = sorted({
            period.month
            for path in sorted(directory.glob("*.csv"))
            if path.is_file() and (period := ingest.file_period(path)) is not None
        }) if directory.is_dir() else []
    except (OSError, ValueError) as exc:
        return [_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            f"メンバー一覧を確認できません: {_reason(exc, input_dir)}", org,
        )]
    if not months:
        return [_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            "members/ にメンバー一覧がありません"
            "（例: members_YYYY-MM.csv。最低限 email,seat_type の2列で可）", org,
        )]
    try:
        result = ingest.load_members(input_dir, months[-1], cfg)
    except (OSError, ValueError) as exc:
        return [_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            f"メンバー一覧を読めません: {_reason(exc, input_dir)}", org,
        )]
    if result.df.empty:
        return [_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            f"メンバー一覧 {result.source.name} にデータ行がありません"
            "（全ユーザがシート不明になり、シート判定ができません）",
            org, file=result.source.name,
        )]
    return []


def _no_spend_month_issues(
    input_dir: Path, cfg: dict, org: str, reason: str | None
) -> list[QualityIssue]:
    """対象月を確定できない場合の検査。Spendの状況とMembersの有無を独立に見る。

    reason はSpendのファイル名を解決できなかった理由（Noneなら1件も無い）。
    """
    detail = (
        f"spend/ のCSVをファイル名から解決できません: {reason}" if reason
        else "spend/ にスペンドレポートがありません"
        "（docs/usage.md の月次運用手順に従いエクスポートしてください）"
    )
    issues = [_issue(
        Severity.ERROR, IssueCode.MISSING_SPEND,
        f"対象月を確定できないため月単位の検査を行えません（{detail}）", org,
    )]
    issues.extend(_members_presence_issues(input_dir, cfg, org))
    return sort_issues(issues)


# --- 複数workspaceの構造（設計書§26.8） ---
#
# 対象月に依存しない構造の検査なので、scopeにmonthは持たせない。ディレクトリの名前は
# 入力の配置そのものなので、列挙は常に昇順（ingest.discover_workspaces が整列済みで返す）。


def _configured_primary(workspaces: dict[str, dict]) -> str | None:
    """primary: true がちょうど1つならその名前。

    0個・2個以上は設定のロードが止めるため、ここでは主が決まらなかったこととして
    Noneを返す（同じ条件を二重に報告しない）。
    """
    primaries = sorted(
        str(name) for name, settings in workspaces.items()
        if isinstance(settings, dict) and settings.get("primary") is True
    )
    return primaries[0] if len(primaries) == 1 else None


def workspace_issues(
    org_input: Path | str, cfg: dict, org: str
) -> list[QualityIssue]:
    """組織の入力レイアウトと、configのworkspacesとの整合を検査する（整列済み）。

    ディレクトリと設定が食い違う場合は黙って片方を採らずに報告する。置き忘れや綴りの
    違いを無視すると、そのworkspaceの利用がまるごと集計から抜けたまま完走するため。
    """
    org_input = Path(org_input)
    layout, found = ingest.detect_workspace_layout(org_input)
    if layout == ingest.WORKSPACE_LAYOUT_MIXED:
        # レイアウトが確定しないので、configとの突き合わせもworkspace別の検査も行わない
        return [_issue(
            Severity.ERROR, IssueCode.WORKSPACE_LAYOUT_MIXED,
            "入力レイアウトが混在しています（組織直下の spend/ と、spend/ を持つ"
            f"子ディレクトリ {'/'.join(found)} の両方があります）。"
            "どちらか一方に寄せてください",
            org, workspaces=found,
        )]

    settings = ingest.workspace_settings(cfg, org)
    configured = sorted(settings)
    issues: list[QualityIssue] = []
    if layout == ingest.WORKSPACE_LAYOUT_NESTED and not configured:
        issues.append(_issue(
            Severity.ERROR, IssueCode.WORKSPACE_CONFIG_MISMATCH,
            f"workspace ごとの spend/ がありますが、config.yaml > organizations.{org}"
            f".workspaces がありません（見つかった workspace: {'/'.join(found)}）。"
            "各 workspace を書き、primary: true をちょうど1つ付けてください",
            org, found=found,
        ))
        return sort_issues(issues)

    missing, unexpected = ingest.compare_workspaces(found, configured)
    if missing or unexpected:
        detail = "、".join(part for part in (
            f"config にあるがディレクトリが無い: {'/'.join(missing)}" if missing else "",
            f"ディレクトリがあるが config に無い: {'/'.join(unexpected)}" if unexpected
            else "",
        ) if part)
        issues.append(_issue(
            Severity.ERROR, IssueCode.WORKSPACE_CONFIG_MISMATCH,
            f"config.yaml > organizations.{org}.workspaces と入力ディレクトリが"
            f"一致しません（{detail}）。どちらかを直してください",
            org, configured=configured, found=found,
            missing=missing, unexpected=unexpected,
        ))

    primary = _configured_primary(settings)
    if configured and primary is not None and primary not in set(found):
        issues.append(_issue(
            Severity.ERROR, IssueCode.WORKSPACE_PRIMARY_INVALID,
            f"主 workspace に指定された {primary} のディレクトリがありません"
            f"（見つかった workspace: {'/'.join(found) if found else 'なし'}）。"
            "primary: true は実在する workspace に付けてください",
            org, primary=primary, found=found,
        ))
    return sort_issues(issues)


def unstarted_workspace_issue(org: str, month: str, workspace: str) -> QualityIssue:
    """まだ始まっていない workspace の入力を検査しなかったことの warning。

    対象月以前にスペンドレポートが1つも無い workspace は、分析（analyze_org）が警告して
    飛ばす（設計書 §26.6）。入力の検査も同じ規則で行わず、その旨だけを1件報告する
    （始まる前の月を欠損として error にしない）。
    """
    return _issue(
        Severity.WARNING, IssueCode.MISSING_SPEND,
        f"workspace {workspace} は {month} 以前のスペンドレポートが無いため、"
        "まだ始まっていない workspace として検査しませんでした（分析でも対象外になります）",
        org, month, workspace=workspace,
    )


def person_issues(
    org_input: Path | str, month: str | None, cfg: dict, org: str
) -> list[QualityIssue]:
    """開始済みの副のシート保持者が主のメンバー一覧にも居るか検査する。"""
    if month is None:
        return []
    org_input = Path(org_input)
    layout, found = ingest.detect_workspace_layout(org_input)
    if layout != ingest.WORKSPACE_LAYOUT_NESTED:
        return []
    primary = _configured_primary(ingest.workspace_settings(cfg, org))
    if primary is None or primary not in found:
        return []
    try:
        if not ingest.workspace_started(org_input / primary, month):
            return []
        main = ingest.load_members(org_input / primary, month, cfg).df
    except (FileNotFoundError, OSError, ValueError):
        return []
    primary_emails = set(main["email"])
    issues = []
    for name in sorted(found):
        if name == primary:
            continue
        try:
            if not ingest.workspace_started(org_input / name, month):
                continue
            secondary = ingest.load_members(org_input / name, month, cfg).df
        except (FileNotFoundError, OSError, ValueError):
            continue
        seated = set(secondary.loc[
            secondary["seat_type"].isin(("standard", "premium")), "email"
        ])
        missing = sorted(seated - primary_emails)
        if missing:
            examples = missing[:_MAX_LISTED]
            issues.append(_issue(
                Severity.WARNING, IssueCode.SECONDARY_ONLY_ACCOUNT,
                f"主 workspace（{primary}）のメンバー一覧に居ない人が {len(missing)} 名います"
                f"（例: {', '.join(examples)}）。主の払い出し漏れか、メールアドレスの"
                "相違の可能性があります",
                org, month, workspace=name, users=len(missing), emails=examples,
            ))
    return issues


def members_info_issues(
    org_input: Path | str, month: str | None, cfg: dict, org: str,
    workspace_dirs: Iterable[Path | str],
) -> list[QualityIssue]:
    """組織直下の members-info と対象月に分析するユーザの対応を検査する。"""
    if month is None:
        return []
    org_input = Path(org_input)
    try:
        info = ingest.load_members_info(org_input, cfg, month)
    except (OSError, ValueError) as exc:
        return [_issue(
            Severity.ERROR, IssueCode.MEMBERS_INFO_UNREADABLE,
            f"members-info.csv を読めません: {_reason(exc, org_input)}"
            "（部署・チーム・追加クレジット上限を結合できないため、分析も止まります）",
            org, month,
        )]
    if info is None:
        return []
    users: set[str] = set()
    for raw_dir in workspace_dirs:
        directory = Path(raw_dir)
        try:
            users.update(ingest.load_members(directory, month, cfg).df["email"])
        except (FileNotFoundError, OSError, ValueError):
            pass
        try:
            spend = ingest.load_spend(directory, month, cfg).df
            users.update(spend.loc[spend["email"].str.contains("@", na=False), "email"])
        except (FileNotFoundError, OSError, ValueError):
            pass
    missing = sorted(users - set(info.df["email"]))
    if not missing:
        return []
    examples = missing[:_MAX_LISTED]
    return [_issue(
        Severity.WARNING, IssueCode.MEMBERS_INFO_UNREGISTERED,
        f"members-info.csv に未登録の分析対象ユーザが {len(missing)} 名います"
        f"（例: {', '.join(examples)}）。部署・チーム・職種が空欄で集計されます"
        "（管理画面へのメンバー追加に追記が追従しているか確認してください）",
        org, month, users=len(missing), emails=examples,
    )]


def inspect_input(
    input_dir: Path | str, month: str | None, cfg: dict, org: str,
    workspace: str | None = None,
) -> list[QualityIssue]:
    """1組織分の Spend / Members を検査し、整列済みのissueを返す。

    月の存在・部分月・欠月はファイル名から、モデル単価・数値解釈・突き合わせは対象月の
    CSVを読んで判定する。month=None は「スペンドの最新月を対象にする」意味で、月を
    確定できない場合だけ月単位の検査を省く。code-analytics・members-info・GitHub・
    browser・admin設定はこのStepでは検査しない。
    判定や既存レポートには一切影響しない読み取り専用の検査。

    入れ子レイアウトの組織では、workspaceのディレクトリを input_dir に、その名前を
    workspace に渡してworkspaceごとに呼ぶ。検査の中身は同じで、どのスペースのissueかを
    scopeに足すだけなので、名前の付与は検査本体ではなくここに閉じる（workspaceを渡さ
    ない従来レイアウトの出力は変わらない）。
    """
    issues = _input_issues(Path(input_dir), month, cfg, org)
    if workspace is None:
        return issues
    return sort_issues([
        dataclasses.replace(issue, scope={**issue.scope, "workspace": workspace})
        for issue in issues
    ])


def _input_issues(
    input_dir: Path, month: str | None, cfg: dict, org: str
) -> list[QualityIssue]:
    """inspect_input の検査本体（1つの入力ディレクトリを対象に整列済みのissueを返す）。"""
    issues: list[QualityIssue] = []
    spend_df: pd.DataFrame | None = None
    spend_months: list[str] | None = None
    try:
        spend_months = ingest.discover_months(input_dir)
    except (OSError, ValueError) as exc:
        # 月をまたぐ期間・同一月の重複など、ファイル名から採用ファイルを決められない
        reason = _reason(exc, input_dir)
        if month is None:
            return _no_spend_month_issues(input_dir, cfg, org, reason)
        issues.append(_issue(
            Severity.ERROR, IssueCode.MISSING_SPEND,
            f"spend/ のCSVをファイル名から解決できません: {reason}", org, month,
        ))

    if month is None:
        if not spend_months:
            return _no_spend_month_issues(input_dir, cfg, org, None)
        month = spend_months[-1]

    if spend_months is not None:
        if month not in spend_months:
            issues.append(_issue(
                Severity.ERROR, IssueCode.MISSING_SPEND,
                f"{month} のスペンドレポートがありません"
                f"（存在する月: {'/'.join(spend_months) if spend_months else 'なし'}）",
                org, month, available_months=spend_months,
            ))
        else:
            issues.extend(_partial_month_issues(input_dir, month, spend_months, cfg, org))
            issues.extend(_history_gap_issues(month, spend_months, cfg, org))
            try:
                spend_df = ingest.load_spend(input_dir, month, cfg).df
            except (OSError, ValueError) as exc:
                issues.append(_issue(
                    Severity.ERROR, IssueCode.MISSING_SPEND,
                    f"{month} のスペンドレポートを読めません: {_reason(exc, input_dir)}",
                    org, month,
                ))
            else:
                issues.extend(_spend_content_issues(spend_df, cfg, org, month))

    members_df: pd.DataFrame | None = None
    try:
        members_result = ingest.load_members(input_dir, month, cfg)
    except FileNotFoundError as exc:
        # ingest が「1件も無い」ときに投げる。読み取り失敗は次節のOSErrorで扱う
        issues.append(_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            f"メンバー一覧がありません: {_reason(exc, input_dir)}", org, month,
        ))
    except (OSError, ValueError) as exc:
        issues.append(_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            f"メンバー一覧を読めません: {_reason(exc, input_dir)}", org, month,
        ))
    else:
        if members_result.df.empty:
            # ヘッダのみ・空行のみ。全ユーザがシート不明になり判定が成立しない
            issues.append(_issue(
                Severity.ERROR, IssueCode.MISSING_MEMBERS,
                f"メンバー一覧 {members_result.source.name} にデータ行がありません"
                "（全ユーザがシート不明になり、シート判定ができません）",
                org, month, file=members_result.source.name,
            ))
        else:
            members_df = members_result.df
            used_month = ingest.month_of_file(members_result.source)
            # 対象月末の直後のファイルは通常の運用経路（月末までのデータを翌月の最初の
            # 営業日に取得する）なので警告しない。ここで鳴ると毎月必ず出て、本当の異常が
            # 埋もれる。過去月・末日から離れた未来月は当時の構成と違いうるので従来どおり
            if used_month != month and not ingest.is_near_month_end(
                members_result.source, month
            ):
                issues.append(_issue(
                    Severity.WARNING, IssueCode.MISSING_MEMBERS,
                    # 対象月のファイルが在っても末日から遠ければ別の月を採るため、
                    # 「対象月のファイルが無い」ではなく「月末時点のものが無い」と書く
                    f"{month} 月末時点のメンバー一覧が無いため {used_month} のファイルを"
                    "使用しています（対象月当時のシート構成と異なる可能性があります）",
                    org, month,
                    used_month=used_month, file=members_result.source.name,
                ))
            issues.extend(_seat_type_issues(members_df, org, month))

    if spend_df is not None and members_df is not None:
        issues.extend(_join_issues(spend_df, members_df, cfg, org, month))

    return sort_issues(issues)


# --- GitHub（config で有効にした組織のみ） ---
#
# GitHub 分析は組織ごとのopt-in（設計書§15.1）。ここに来るのは
# `organizations.<組織名>.github_org` を設定した組織だけで、未設定の組織は呼び出し側が
# 除外する。したがって「対応表が無い」ことをここでは正常と扱わない。
#
# GitHubの検査結果は対象月に依存しないため、scopeにmonthを持たせない。gh の生出力も
# 持ち込まず、分類語と数値だけでmessageを組み立てる（同じprobe結果からは常に同じmessage）。

# ghを使えなかった理由の表示語
_GH_FAILURE_TEXT = {
    GhFailure.UNAUTHENTICATED: "未認証",
    GhFailure.NOT_FOUND: "gh コマンドが見つかりません",
    GhFailure.TIMEOUT: "制限時間内に応答がありません",
    GhFailure.ERROR: "実行に失敗しました",
}

# 認証を確認できなかったときの対処。原因ごとに次の一手が違う
_GH_AUTH_REMEDY = {
    GhFailure.UNAUTHENTICATED: "gh auth login で認証してください",
    GhFailure.NOT_FOUND: "GitHub CLI を導入して PATH に置いてください",
    GhFailure.TIMEOUT: "ネットワークの状態を確認して再実行してください",
    GhFailure.ERROR: "GitHub CLI の導入状態を確認してください",
}


def _github_auth_issues(auth: GhAuth, org: str, github_org: str) -> list[QualityIssue]:
    """ghの認証を確認できない場合のissue（収集の前提が無いのでerror）。"""
    if auth.ok:
        return []
    return [_issue(
        Severity.ERROR, IssueCode.GH_NOT_AUTHENTICATED,
        f"GitHub CLI の認証を確認できません（{_GH_FAILURE_TEXT[auth.failure]}）。"
        f"{_GH_AUTH_REMEDY[auth.failure]}",
        org, github_org=github_org, reason=auth.failure.value,
    )]


def _github_scope_issues(
    scopes: tuple[str, ...] | None, org: str, github_org: str
) -> list[QualityIssue]:
    """tokenのscopeが足りない場合のissue。

    scopes=None は「判定できないtoken」（fine-grained PAT・GitHub App）で、不足を報告
    しない。参照できるかどうかはOrganizationの検査が実地で確かめる。
    """
    if scopes is None:
        return []
    missing = github_collect.missing_scopes(scopes)
    if not missing:
        return []
    return [_issue(
        Severity.ERROR, IssueCode.GH_PERMISSION_INCOMPLETE,
        f"GitHub token の権限が足りません（不足: {', '.join(missing)}）。"
        f"gh auth refresh {' '.join(f'-s {scope}' for scope in missing)} で"
        "追加してください",
        org, github_org=github_org, missing_scopes=list(missing),
    )]


def _github_rate_issues(
    rate: tuple[GhRateResource, ...], org: str, github_org: str
) -> list[QualityIssue]:
    """利用上限に達している場合のissue（再実行で解消するのでwarning）。

    回復時刻はmessageへ入れない（実行のたびに変わる値をmessageに持ち込まない）。
    """
    exhausted = [resource for resource in rate if resource.remaining == 0]
    if not exhausted:
        return []
    detail = "、".join(
        f"{resource.name}: 残り 0 / 上限 {resource.limit}" for resource in exhausted
    )
    return [_issue(
        Severity.WARNING, IssueCode.GH_RATE_LIMITED,
        f"GitHub API の利用上限に達しています（{detail}）。"
        "上限が回復してから再実行してください",
        org, github_org=github_org,
        resources=[resource.name for resource in exhausted],
    )]


def _github_org_issues(
    access: GhOrgAccess | None, org: str, github_org: str
) -> list[QualityIssue]:
    """Organizationを参照できない場合のissue（収集の前提が無いのでerror）。"""
    if access is None or access.accessible:
        return []
    if access.failure is not None:
        return [_issue(
            Severity.ERROR, IssueCode.GH_ORG_NOT_ACCESSIBLE,
            f"GitHub の Organization {github_org} を参照できません"
            f"（{_GH_FAILURE_TEXT[access.failure]}）。"
            "ネットワークと GitHub CLI の状態を確認して再実行してください",
            org, github_org=github_org, reason=access.failure.value,
        )]
    if access.sso_required:
        return [_issue(
            Severity.ERROR, IssueCode.GH_PERMISSION_INCOMPLETE,
            f"GitHub の Organization {github_org} は SAML SSO の承認が必要です"
            "（HTTP 403）。GitHub の設定画面で token をこの Organization 向けに"
            "承認してください",
            org, github_org=github_org, status=access.status,
        )]
    return [_issue(
        Severity.ERROR, IssueCode.GH_ORG_NOT_ACCESSIBLE,
        f"GitHub の Organization {github_org} を参照できません"
        f"（HTTP {access.status}）。config.yaml > organizations.{org}.github_org の"
        "綴りと、token に付与された権限を確認してください",
        org, github_org=github_org, status=access.status,
    )]


def _github_unmapped_issues(
    month: str | None, cfg: dict, org: str, github_org: str,
    members: GithubMembers, workspace_dirs: Iterable[Path | str],
) -> list[QualityIssue]:
    """シートを持つメンバーのうち GitHub login に対応づかない人を警告する。

    対象は analyze の GitHub の参考値と同じく、メンバー一覧で standard / premium の人
    （`ingest.ASSIGNED_SEAT_TYPES`）。workspace_dirs は members-info の検査と同じ
    workspace の一覧で、その和集合を照合する。メンバー一覧を読めない workspace は静かに
    飛ばす（同じ原因を inspect_input が MISSING_MEMBERS として報告するため、ここで二重に
    出さない）。
    """
    if month is None:
        return []
    seated: set[str] = set()
    for raw_dir in workspace_dirs:
        try:
            frame = ingest.load_members(Path(raw_dir), month, cfg).df
        except (OSError, ValueError):
            continue
        seated.update(
            frame.loc[frame["seat_type"].isin(ingest.ASSIGNED_SEAT_TYPES), "email"]
        )
    unmapped = github_collect.unmapped_emails(members, seated)
    if not unmapped:
        return []
    emails = list(unmapped[:_MAX_LISTED])
    return [_issue(
        Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING,
        f"シートを持つメンバーのうち GitHub login に対応づかない人が {len(unmapped)} 名"
        f"います（例: {', '.join(emails)}）。"
        f"{ingest.MEMBERS_INFO_FILENAME} の GitHub ID 列に記入してください"
        "（対象の GitHub Organization で開発しない人は「なし」と書くとこの警告から"
        "外れます。対応づかない人の PR は、人ごとの行にも組織全体の件数にも入りません）",
        org, github_org=github_org, members=len(unmapped), emails=emails,
    )]


def _github_mapping_issues(
    input_dir: Path, month: str | None, cfg: dict, org: str, github_org: str,
    workspace_dirs: Iterable[Path | str],
) -> list[QualityIssue]:
    """members-info の GitHub ID 列（email → GitHub login の対応表）を検査する。

    読めない対応表はerrorにする（fail-closed）。取り違えに直結する不備（email・login の
    重複、必須カラムの欠落、列ずれ等）と読み取り失敗をまとめて GITHUB_MAPPING_DUPLICATE
    で表す。壊れた対応表を使うと、別人のPRを帰属させたまま集計が完走する。
    """
    filename = ingest.MEMBERS_INFO_FILENAME
    try:
        members = github_collect.load_github_members(input_dir, cfg, month)
    except (OSError, ValueError) as exc:
        return [_issue(
            Severity.ERROR, IssueCode.GITHUB_MAPPING_DUPLICATE,
            # 前置に固定名を使わない。読んだのは日付つきのファイルかもしれず、旧ファイルの
            # 検出はそもそも members-info を読む前に止まる（実ファイル名は理由の側に残る）
            f"GitHub ID の対応表を読めません: {_reason(exc, input_dir)}",
            org, github_org=github_org,
        )]
    if members.source is None:
        return [_issue(
            Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING,
            f"GitHub 分析が有効な組織ですが {filename} がありません"
            "（GitHub ID の列で email → GitHub login の対応表を持ちます。組織"
            "ディレクトリ直下に置いてください。無いあいだ PR はどのユーザにも"
            "帰属しません）",
            org, github_org=github_org,
        )]
    if not members.has_column:
        return [_issue(
            Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING,
            f"{members.source} に GitHub ID の列がありません"
            "（email → GitHub login の対応表。列を足して各メンバーの GitHub login を"
            "記入してください。対象の GitHub Organization で開発しない人は「なし」）",
            org, github_org=github_org,
        )]
    issues = [
        _issue(
            Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING, warning,
            org, github_org=github_org,
        )
        for warning in members.warnings
    ]
    issues.extend(_github_unmapped_issues(
        month, cfg, org, github_org, members, workspace_dirs))
    return issues


def inspect_github(
    input_dir: Path | str, month: str | None, cfg: dict, org: str,
    github_org: str, probes: GithubProbes, workspace_dirs: Iterable[Path | str],
) -> list[QualityIssue]:
    """GitHub分析を有効にした1組織分の検査（整列済み）。

    probes は gh の実行結果（`github_collect.probe_github`）で、この関数自身は gh も
    ネットワークも呼ばない。monthは突き合わせるメンバー一覧の選択にだけ使い、scopeには
    持たせない（GitHubの検査結果は対象月に依存しないため）。

    input_dir は組織ディレクトリ（members-info の対応表を読む場所）。workspace_dirs は
    メンバー一覧を読む workspace のディレクトリで、members_info_issues と同じもの
    （入れ子なら開始済みの workspace、単一なら組織直下）を渡す。組織直下にメンバー一覧を
    持たない入れ子の組織でも、対応表の突き合わせが黙って飛ばないようにするため。

    認証できていない場合、そこから派生する scope・rate・Organization の検査結果は意味を
    持たないので probe 側が実行せず、ここでも報告しない。対応表の検査はghと無関係な
    ローカルの検査なので、認証の可否によらず常に行う。
    """
    input_dir = Path(input_dir)
    issues = _github_auth_issues(probes.auth, org, github_org)
    if probes.auth.ok:
        issues.extend(_github_scope_issues(probes.scopes, org, github_org))
        issues.extend(_github_rate_issues(probes.rate, org, github_org))
        issues.extend(_github_org_issues(probes.org(github_org), org, github_org))
    issues.extend(_github_mapping_issues(
        input_dir, month, cfg, org, github_org, workspace_dirs))
    return sort_issues(issues)


@enum.unique
class OtelCoverageStatus(enum.StrEnum):
    """人 × 月で OpenTelemetry が届いている割合を確かめた状態。"""

    UNKNOWN = "unknown"
    NOT_JUDGED = "not_judged"
    MISSING = "missing"
    LOW = "low"
    OK = "ok"


@dataclasses.dataclass(frozen=True)
class OtelUserCoverage:
    """正規化したメールアドレスと、その人の月の状態・トークン数・比。

    spend の和は対象 product だけを数え、判定不能なら None。OTel の和は
    api_request だけを数え、キャッシュに属性が無ければ None。比は判定した人
    だけが持ち、イベントが無い人（MISSING）の比は 0.0。
    """

    email: str
    status: OtelCoverageStatus
    spend_output_tokens: int | float | None
    otel_output_tokens: int | None
    ratio: float | None


@dataclasses.dataclass(frozen=True)
class OtelCoverage:
    """1か月の人別・組織全体の網羅率と、キャッシュの出力トークン属性の有無。

    users はメールアドレスの昇順。組織のトークン数は、判定不能を除いた spend の
    出力が正の人だけを分子と分母の両方に数える。該当者がいなければ比は None。
    """

    month: str
    users: tuple[OtelUserCoverage, ...]
    output_tokens_absent: bool
    spend_output_tokens: int | float
    otel_output_tokens: int
    ratio: float | None


def _sum_output_tokens(values: Sequence[int | float]) -> int | float:
    """整数は正確に足し、小数を含むときも明細の順序による丸めの差を抑える。"""
    if any(isinstance(value, float) for value in values):
        return math.fsum(values)
    return sum(values)


def _spend_output_tokens(
    spends: Iterable[pd.DataFrame], month: str, products: Sequence[str],
) -> tuple[dict[str, list[int | float]], set[str]]:
    """利用者ごとに対象の出力トークンを集め、分母が不明な人も返す。"""
    target = {normalize_product_name(product) for product in products}
    required = {"email", "completion_tokens", "month"}
    tokens: dict[str, list[int | float]] = defaultdict(list)
    unknown: set[str] = set()
    for spend in spends:
        if not isinstance(spend, pd.DataFrame):
            raise TypeError("spend の明細は DataFrame が必要です")
        if missing := required.difference(spend.columns):
            raise ValueError(f"spend の明細に必要な列がありません: {', '.join(sorted(missing))}")
        if not spend["month"].eq(month).fillna(False).all():
            raise ValueError(f"spend の明細にキャッシュと異なる月があります: {month}")

        emails = spend["email"].astype("string").str.strip().str.lower()
        users = emails.str.contains("@", na=False)
        products_in_spend = (
            spend.loc[users, "product"].astype("string").map(normalize_product_name,
                                                           na_action="ignore")
            if "product" in spend else [None] * int(users.sum())
        )
        for email, product, value in zip(
            emails[users], products_in_spend, spend.loc[users, "completion_tokens"],
        ):
            amounts = tokens[email]
            if pd.isna(product):
                unknown.add(email)
            elif product in target:
                if pd.isna(value) or not math.isfinite(value):
                    unknown.add(email)
                else:
                    # pandas は欠損が混じる整数列を float にすることがある。
                    integer = int(value)
                    amounts.append(integer if integer == value else float(value))
    return tokens, unknown


def _otel_user_coverage(
    email: str, spend: float, output: int, observed: bool, unknown: bool,
    output_absent: bool, warning_ratio: float, minimum: int,
) -> OtelUserCoverage:
    """対象の利用量とイベントの有無を、判定の優先順に当てはめる。"""
    if output_absent or unknown:
        return OtelUserCoverage(email, OtelCoverageStatus.UNKNOWN, None,
                                None if output_absent else output, None)
    if spend == 0 or spend < minimum:
        return OtelUserCoverage(email, OtelCoverageStatus.NOT_JUDGED, spend, output, None)
    if not observed:
        return OtelUserCoverage(email, OtelCoverageStatus.MISSING, spend, output, 0.0)
    ratio = output / spend
    status = OtelCoverageStatus.LOW if ratio < warning_ratio else OtelCoverageStatus.OK
    return OtelUserCoverage(email, status, spend, output, ratio)


def otel_coverage(
    spends: Iterable[pd.DataFrame], cache: otel_collect.OtelCache, products: Sequence[str],
    warning_ratio: float, min_output_tokens: int,
) -> OtelCoverage:
    """1か月の spend とキャッシュから、人別・組織全体の網羅率を計算する。

    spends には各 workspace の load_spend と同じ形の明細を渡す。月が異なる明細は
    取り違えとして拒む。対象 product と閾値は検査済みの設定値を渡す。
    ファイルは読まず、キャッシュのイベント行だけを観測に使う。
    """
    if not isinstance(cache, otel_collect.OtelCache):
        raise TypeError("OpenTelemetry の集計は OtelCache が必要です")
    spent, unknown = _spend_output_tokens(spends, cache.month, products)
    observed: set[str] = set()
    output: dict[str, int] = defaultdict(int)
    for row in cache.rows:
        email = row.email.strip().lower()
        observed.add(email)
        if row.name == "api_request":
            output[email] += row.output_tokens or 0
    absent = "output_tokens" in cache.absent_attributes
    users = []
    org_spent = []
    org_output = 0
    for email in sorted(spent.keys() | observed):
        spend_tokens = _sum_output_tokens(spent.get(email, ()))
        output_tokens = output.get(email, 0)
        user = _otel_user_coverage(
            email, spend_tokens, output_tokens, email in observed, email in unknown,
            absent, warning_ratio, min_output_tokens,
        )
        users.append(user)
        if user.status is not OtelCoverageStatus.UNKNOWN and spend_tokens > 0:
            org_spent.append(spend_tokens)
            org_output += output_tokens
    org_total = _sum_output_tokens(org_spent)
    return OtelCoverage(cache.month, tuple(users), absent, org_total, org_output,
                        org_output / org_total if org_total else None)


def _truncate_coverage_ratio(output: int, spend: float) -> float:
    """トークン数から比を小数2桁で切り捨て、message と scope の値をそろえる。"""
    numerator, denominator = spend.as_integer_ratio()
    return ((output * 100 * denominator) // numerator) / 100


def _otel_low_issue(
    users: list[OtelUserCoverage], coverage: OtelCoverage, org: str, products: str,
    warning_ratio: float,
) -> QualityIssue:
    """比の小さい人から例を選び、表示だけを小数2桁に切り捨てる。"""
    examples = sorted(users, key=lambda user: (user.ratio, user.email))[:_MAX_LISTED]
    ratios = [
        _truncate_coverage_ratio(user.otel_output_tokens, user.spend_output_tokens)
        for user in examples
    ]
    sample = ", ".join(f"{user.email}（{ratio:.2f}）" for user, ratio in zip(examples, ratios))
    org_ratio = _truncate_coverage_ratio(coverage.otel_output_tokens, coverage.spend_output_tokens)
    return _issue(
        Severity.WARNING, IssueCode.OTEL_COVERAGE_LOW,
        f"OpenTelemetry に届いている割合が低い人が {len(users)} 名います（例: {sample}）。"
        f"出力トークンの比（OpenTelemetry ÷ spend の {products}）が {warning_ratio!s} 未満で、"
        "この人の OpenTelemetry 由来の値は、利用の一部しか表していません"
        f"（組織全体の比は {org_ratio:.2f}）。"
        "テレメトリの設定が入っていない端末で使っていないかを確かめてください",
        org, coverage.month, users=len(users), emails=[user.email for user in examples],
        ratios=ratios, warning_ratio=warning_ratio, organization_ratio=org_ratio,
    )


def _otel_coverage_issues(
    coverage: OtelCoverage, org: str, products: Sequence[str], warning_ratio: float,
) -> list[QualityIssue]:
    """判定不能の理由と警告対象の例を、組織ごとの issue にする。"""
    month = coverage.month
    if coverage.output_tokens_absent:
        return [_issue(
            Severity.WARNING, IssueCode.OTEL_COVERAGE_UNAVAILABLE,
            "OpenTelemetry のキャッシュに出力トークンが無いため、"
            "届いている割合を確かめられません"
            "（取得したイベントの表に、出力トークンの属性の列がありませんでした）",
            org, month, reason="output_tokens_absent",
        )]
    product_names = "・".join(products)
    by_status: dict[OtelCoverageStatus, list[OtelUserCoverage]] = defaultdict(list)
    for user in coverage.users:
        by_status[user.status].append(user)
    issues = []
    if unknown := by_status[OtelCoverageStatus.UNKNOWN]:
        emails = [user.email for user in unknown[:_MAX_LISTED]]
        issues.append(_issue(
            Severity.WARNING, IssueCode.OTEL_COVERAGE_UNAVAILABLE,
            f"spend に、product 名が空の行か、{product_names} の行で出力トークンを"
            f"読めない行がある人が {len(unknown)} 名いて、"
            f"OpenTelemetry に届いている割合を確かめられません（例: {', '.join(emails)}）",
            org, month, reason="spend_rows_unreadable", users=len(unknown), emails=emails,
        ))
    if low := by_status[OtelCoverageStatus.LOW]:
        issues.append(_otel_low_issue(low, coverage, org, product_names, warning_ratio))
    if missing := by_status[OtelCoverageStatus.MISSING]:
        emails = [user.email for user in missing[:_MAX_LISTED]]
        org_ratio = _truncate_coverage_ratio(
            coverage.otel_output_tokens, coverage.spend_output_tokens,
        )
        issues.append(_issue(
            Severity.WARNING, IssueCode.OTEL_USER_MISSING,
            f"spend に {product_names} の利用があるのに、"
            f"OpenTelemetry にイベントが1件も無い人が {len(missing)} 名います"
            f"（例: {', '.join(emails)}。組織全体の比は {org_ratio:.2f}）。"
            "テレメトリの設定が端末に入っているか、利用者のメールアドレスの属性が"
            "届いているかを確かめてください",
            org, month, users=len(missing), emails=emails, organization_ratio=org_ratio,
        ))
    return sort_issues(issues)


def inspect_otel(
    input_dir: Path | str, month: str | None, cfg: dict, org: str,
    target: otel_collect.OtelTarget, workspace_dirs: Iterable[Path | str],
) -> list[QualityIssue]:
    """OpenTelemetry を有効にした1組織分の網羅率の検査（整列済み）。

    input_dir は組織のキャッシュがある場所。workspace_dirs には単一レイアウトの
    組織直下か、入れ子レイアウトの未開始を除いた全 workspace を渡す。
    全月の spend がそろった月だけキャッシュを読み、BigQuery には接続しない。
    """
    if month is None:
        return []
    spends = []
    for directory in workspace_dirs:
        try:
            result = ingest.load_spend(Path(directory), month, cfg)
            period = ingest.file_period(result.source)
        except (OSError, ValueError):
            return []
        if period is None or _month_coverage(period, month) is not _MonthCoverage.FULL:
            return []
        spends.append(result.df)
    if not spends:
        return []

    input_dir = Path(input_dir)
    cache_dir = input_dir / otel_collect.OTEL_CACHE_DIRNAME
    try:
        cache = otel_collect.load_otel_cache(cache_dir, month, org, target)
    except (ValueError, OSError) as exc:
        if isinstance(exc, ValueError):
            reason = _reason(exc, input_dir)
        else:
            filename = otel_collect.otel_cache_path(cache_dir, month).name
            reason = (f"{otel_collect.OTEL_CACHE_DIRNAME}/{filename} を開けません"
                      "（権限とファイルの状態を確かめてください）")
        return [_issue(
            Severity.WARNING, IssueCode.OTEL_COVERAGE_UNAVAILABLE,
            f"OpenTelemetry のキャッシュを読めないため、届いている割合を確かめられません: {reason}",
            org, month, reason="cache_unreadable",
        )]
    if cache is None:
        return []
    settings = cfg["otel"]
    products = settings["coverage_products"]
    warning_ratio = settings["coverage_warning_ratio"]
    coverage = otel_coverage(spends, cache, products, warning_ratio,
                             settings["coverage_min_output_tokens"])
    return _otel_coverage_issues(coverage, org, products, warning_ratio)


def workspace_config_issues(cfg: dict, orgs: Iterable[str]) -> list[QualityIssue]:
    """configのworkspacesのうち、入力に組織ディレクトリが無いものを報告する。

    組織ごとの構造検査（workspace_issues）は発見した組織だけを回るため、ディレクトリ
    ごと無い組織に書いた設定はどの検査にも現れない。綴り違いや置き場所の誤りを黙って
    無視しないよう、設定だけの問題としてここで報告する（GitHubの有効化とは独立）。
    どの組織にも属さないissueなのでscopeにorgは持たせず、書かれたキーをconfig_orgと
    して持つ（github_config_issues と同じ扱い）。

    orgsは`--org`の選択ではなく、入力ディレクトリで見つかった組織すべて。
    """
    known = sorted(orgs)
    found = set(known)
    listed = "/".join(known) if known else "なし"
    organizations = cfg.get("organizations")
    names = sorted(
        name for name in organizations if isinstance(name, str)
    ) if isinstance(organizations, dict) else []

    issues: list[QualityIssue] = []
    for name in names:
        configured = sorted(ingest.workspace_settings(cfg, name))
        if not configured or name in found:
            continue
        issues.append(_issue(
            Severity.ERROR, IssueCode.WORKSPACE_CONFIG_MISMATCH,
            f"config.yaml > organizations の '{name}' に workspaces"
            f"（{'/'.join(configured)}）が書かれていますが、一致する組織ディレクトリが"
            f"ありません（存在する組織: {listed}）。組織名の綴りと入力ディレクトリの"
            "配置を確認してください",
            None, config_org=name, workspaces=configured, known_orgs=known,
        ))
    return sort_issues(issues)


def github_config_issues(cfg: dict, orgs: Iterable[str]) -> list[QualityIssue]:
    """configのorganizationsのキーのうち、入力に組織ディレクトリが無いものを警告する。

    綴り違いは「GitHubの検査がまるごと飛ぶ」形で表に出ないため、有効にしたつもりの組織が
    黙って対象外になる。どの組織にも属さない設定の問題なので、scopeにorgは持たせず、
    書かれたキーをconfig_orgとして持つ（実在する組織のorgと区別する）。

    orgsは`--org`の選択ではなく、入力ディレクトリで見つかった組織すべて。
    """
    known = sorted(orgs)
    found = set(known)
    listed = "/".join(known) if known else "なし"
    return sort_issues([
        _issue(
            Severity.WARNING, IssueCode.GITHUB_CONFIG_UNMATCHED,
            f"config.yaml > organizations の '{name}' に一致する組織ディレクトリが"
            f"ありません（存在する組織: {listed}）。この設定は GitHub の検査に"
            "使われません。組織名の綴りを確認してください",
            None, config_org=name, known_orgs=known,
        )
        for name in github_collect.gated_orgs(cfg)
        if name not in found
    ])
