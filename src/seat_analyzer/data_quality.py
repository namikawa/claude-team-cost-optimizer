"""既存入力（Spend / Members）の検査と、構造化品質issueの整列・JSON直列化。

`inspect_input` は doctor の検査本体で、1組織・1対象月の入力を読み、issueを返す
だけの純粋な関数（出力整形と終了コードは cli が担当）。同じ入力からは常にバイト
一致の出力を得られるよう、messageには絶対パス・時刻・乱数を入れない。

`analyze` の文字列warningはこの検査とは独立に従来どおり出る。両者を統合するのは
このStepでは行わない。
"""

from __future__ import annotations

import calendar
import json
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from . import ingest, pricing
from .domain import IssueCode, QualityIssue, ScopeValue, Severity

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
    """issue列を決定的なJSON文字列にする。

    並び順は渡された順のまま（整列が必要ならsort_issuesを先に通す）。日本語の
    messageはエスケープせずそのまま出力し、NaN・inf はJSON側でも拒否する。
    """
    return json.dumps(
        [issue_to_dict(issue) for issue in issues],
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    )


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
    **scope: ScopeValue,
) -> QualityIssue:
    """org・monthを先頭に置いたscopeでissueを作る。Noneのキーは省く。"""
    base: dict[str, ScopeValue] = {}
    if org is not None:
        base["org"] = org
    if month is not None:
        base["month"] = month
    base.update(scope)
    return QualityIssue(severity=severity, code=code, message=message, scope=base)


def _reason(exc: Exception, input_dir: Path) -> str:
    """例外メッセージを1行かつ実行環境に依存しない文字列へ整える。

    ingestの例外は呼び出し時の入力ディレクトリをそのまま含むため、そのままでは
    messageが実行環境に依存する（domain.QualityIssueの決定性制約）。
    """
    flat = " ".join(str(exc).split())
    bases = {str(input_dir), str(input_dir.resolve())}
    for base in sorted(bases, key=len, reverse=True):
        flat = flat.replace(base + "/", "")
    return flat.strip()


def _prev_month(month: str) -> str:
    """YYYY-MM の暦上の直前月。"""
    year, mon = (int(x) for x in month.split("-"))
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


def _days_in_month(month: str) -> int:
    year, mon = (int(x) for x in month.split("-"))
    return calendar.monthrange(year, mon)[1]


def _sample(emails: Iterable[str]) -> list[str]:
    """messageへ載せる代表例。入力順に依存しないよう整列してから先頭を採る。"""
    return sorted(emails)[:_MAX_LISTED]


def _partial_month_issues(
    input_dir: Path, month: str, spend_months: list[str], org: str | None
) -> list[QualityIssue]:
    """対象月までの各月で、全月データと確認できないスペンドを警告する。

    kind=range は期間の日数で判定する。kind=date（単日日付の命名）は実際の集計期間が
    ファイル名から分からないため、日数を断定せず「確認できない」として警告する。
    kind=month（spend_YYYY-MM.csv）は全月の指定として扱い警告しない。
    """
    issues: list[QualityIssue] = []
    for m in [x for x in spend_months if x <= month]:
        period = ingest.spend_file_period(input_dir, m)
        if period is None:
            continue
        days_in_month = _days_in_month(m)
        if period.kind == "date":
            issues.append(_issue(
                Severity.WARNING, IssueCode.PARTIAL_MONTH,
                f"{m} のスペンドはファイル名が単日日付（{period.start:%m-%d}）のため、"
                f"暦上 {days_in_month}日の全月データであることを確認できません。"
                "部分月であれば月額前提の判定で需要が過小評価されます"
                "（月中の一次判断は analyze --preview を使ってください）",
                org, m, snapshot_date=f"{period.start:%Y-%m-%d}", days_in_month=days_in_month,
            ))
            continue
        if period.days is None or period.days >= days_in_month:
            continue
        issues.append(_issue(
            Severity.WARNING, IssueCode.PARTIAL_MONTH,
            f"{m} は部分月データです（{period.start:%m-%d}〜{period.end:%m-%d} の "
            f"{period.days}日分 / 暦上 {days_in_month}日）。"
            "月額前提の判定では需要が過小評価されます"
            "（月中の一次判断は analyze --preview を使ってください）",
            org, m, days_observed=period.days, days_in_month=days_in_month,
        ))
    return issues


def _history_gap_issues(
    month: str, spend_months: list[str], cfg: dict, org: str | None
) -> list[QualityIssue]:
    """ヒステリシス窓（対象月を含む直近 N ヶ月）に欠けている月を警告する。"""
    n_hyst = int(cfg["decision"]["hysteresis_months"])
    window: list[str] = []
    cursor = month
    for _ in range(max(n_hyst - 1, 0)):
        cursor = _prev_month(cursor)
        window.append(cursor)
    missing = [m for m in reversed(window) if m not in spend_months]
    if not missing:
        return []
    return [_issue(
        Severity.WARNING, IssueCode.MISSING_HISTORY_MONTH,
        f"ヒステリシス判定に必要な直近 {n_hyst} ヶ月のうち "
        f"{'/'.join(missing)} のスペンドレポートがありません。"
        "暦上の連続性が確認できないため判定の質が下がります"
        "（欠月は飛ばして存在する過去月で連続同推奨を判定するため、"
        "実質1ヶ月の裏付けでも変更推奨が出ることがあります）",
        org, month, missing_months=missing, hysteresis_months=n_hyst,
    )]


def _spend_content_issues(
    spend: pd.DataFrame, cfg: dict, org: str | None, month: str
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
    members: pd.DataFrame, org: str | None, month: str
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
        org, month, members=int(len(unknown)), emails=emails,
    )]


def _join_issues(
    spend: pd.DataFrame, members: pd.DataFrame, cfg: dict, org: str | None, month: str
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


def _members_presence_issues(input_dir: Path, org: str | None) -> list[QualityIssue]:
    """対象月が決まらない場合でも確認できる、メンバー一覧の有無だけを検査する。"""
    directory = input_dir / "members"
    try:
        found = directory.is_dir() and any(
            ingest.file_period(path) is not None
            for path in sorted(directory.glob("*.csv"))
        )
    except (OSError, ValueError) as exc:
        return [_issue(
            Severity.ERROR, IssueCode.MISSING_MEMBERS,
            f"メンバー一覧を確認できません: {_reason(exc, input_dir)}", org,
        )]
    if found:
        return []
    return [_issue(
        Severity.ERROR, IssueCode.MISSING_MEMBERS,
        "members/ にメンバー一覧がありません"
        "（例: members_YYYY-MM.csv。最低限 email,seat_type の2列で可）", org,
    )]


def _no_target_month_issues(input_dir: Path, org: str | None) -> list[QualityIssue]:
    """対象月を決められない場合の検査。Spendの解決可否とMembersの有無は独立に見る。"""
    issues: list[QualityIssue] = []
    try:
        available = ingest.discover_months(input_dir)
    except (OSError, ValueError) as exc:
        issues.append(_issue(
            Severity.ERROR, IssueCode.MISSING_SPEND,
            f"spend/ のCSVをファイル名から解決できないため対象月を特定できません: "
            f"{_reason(exc, input_dir)}", org,
        ))
    else:
        detail = (
            "spend/ にスペンドレポートがありません" if not available
            else f"存在する月: {'/'.join(available)}"
        )
        issues.append(_issue(
            Severity.ERROR, IssueCode.MISSING_SPEND,
            f"対象月を特定できません（{detail}）。"
            "README の月次運用手順に従いエクスポートしてください",
            org, available_months=available,
        ))
    issues.extend(_members_presence_issues(input_dir, org))
    return sort_issues(issues)


def inspect_input(
    input_dir: Path | str, month: str | None, cfg: dict, org: str | None = None
) -> list[QualityIssue]:
    """1組織分の Spend / Members を検査し、整列済みのissueを返す。

    月の存在・部分月・欠月はファイル名から、モデル単価・数値解釈・突き合わせは対象月の
    CSVを読んで判定する。code-analytics・members-info・GitHub・browser・admin設定は
    このStepでは検査しない。判定や既存レポートには一切影響しない読み取り専用の検査。
    """
    input_dir = Path(input_dir)
    if month is None:
        return _no_target_month_issues(input_dir, org)

    issues: list[QualityIssue] = []
    spend_df: pd.DataFrame | None = None
    spend_months: list[str] | None = None
    try:
        spend_months = ingest.discover_months(input_dir)
    except (OSError, ValueError) as exc:
        # 月をまたぐ期間・同一月の重複など、ファイル名から採用ファイルを決められない
        issues.append(_issue(
            Severity.ERROR, IssueCode.MISSING_SPEND,
            f"spend/ のCSVをファイル名から解決できません: {_reason(exc, input_dir)}",
            org, month,
        ))

    if spend_months is not None:
        if month not in spend_months:
            issues.append(_issue(
                Severity.ERROR, IssueCode.MISSING_SPEND,
                f"{month} のスペンドレポートがありません"
                f"（存在する月: {'/'.join(spend_months) if spend_months else 'なし'}）",
                org, month, available_months=spend_months,
            ))
        else:
            issues.extend(_partial_month_issues(input_dir, month, spend_months, org))
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
            if used_month != month:
                issues.append(_issue(
                    Severity.WARNING, IssueCode.MISSING_MEMBERS,
                    f"{month} のメンバー一覧が無いため {used_month} のファイルを使用しています"
                    "（対象月当時のシート構成と異なる可能性があります）",
                    org, month,
                    used_month=used_month, file=members_result.source.name,
                ))
            issues.extend(_seat_type_issues(members_df, org, month))

    if spend_df is not None and members_df is not None:
        issues.extend(_join_issues(spend_df, members_df, cfg, org, month))

    return sort_issues(issues)
