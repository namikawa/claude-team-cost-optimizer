"""既存入力（Spend / Members / GitHub）の検査と、構造化品質issueの整列・JSON直列化。

`inspect_input` は doctor の検査本体で、1組織・1対象月の入力を読み、issueを返す
だけの純粋な関数（出力整形と終了コードは cli が担当）。同じ入力からは常にバイト
一致の出力を得られるよう、messageには絶対パス・時刻・乱数を入れない。

`inspect_github` と `github_config_issues` は GitHub 分析を有効にした組織のための検査で、
gh の実行結果（`github_collect.probe_github` が返す値）と対応表を issue へ写す。gh を
呼ぶのは probe 側だけなので、この2つも同じ入力から常に同じ出力を返す。

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
import json
import os
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from . import github_collect, ingest, pricing
from .domain import IssueCode, QualityIssue, ScopeValue, Severity
from .github_collect import (
    GhAuth,
    GhFailure,
    GhOrgAccess,
    GhRateResource,
    GithubMembers,
    GithubProbes,
)

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
    **scope: ScopeValue,
) -> QualityIssue:
    """org・monthを先頭に置いたscopeでissueを作る。Noneのキーは省く。

    org=Noneは、どの組織にも属さないissueに限る（対象組織を解決する前の失敗＝
    input_unavailable_issues と、設定だけの問題＝github_config_issues）。
    組織単位の検査は常にorgを持つ。
    """
    base: dict[str, ScopeValue] = {}
    if org is not None:
        base["org"] = org
    if month is not None:
        base["month"] = month
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
    input_dir: Path, month: str, spend_months: list[str], org: str
) -> list[QualityIssue]:
    """対象月までの各月で、全月データと確認できないスペンドを警告する。

    kind=range は期間の日数で判定する。kind=date（単日日付の命名）は実際の集計期間が
    ファイル名から分からないため、日数を断定せず「確認できない」として警告する。
    kind=month（spend_YYYY-MM.csv）は全月の指定として扱い警告しない。
    """
    issues: list[QualityIssue] = []
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
    month: str, spend_months: list[str], cfg: dict, org: str
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


def inspect_input(
    input_dir: Path | str, month: str | None, cfg: dict, org: str
) -> list[QualityIssue]:
    """1組織分の Spend / Members を検査し、整列済みのissueを返す。

    月の存在・部分月・欠月はファイル名から、モデル単価・数値解釈・突き合わせは対象月の
    CSVを読んで判定する。month=None は「スペンドの最新月を対象にする」意味で、月を
    確定できない場合だけ月単位の検査を省く。code-analytics・members-info・GitHub・
    browser・admin設定はこのStepでは検査しない。
    判定や既存レポートには一切影響しない読み取り専用の検査。
    """
    input_dir = Path(input_dir)
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
    input_dir: Path, month: str | None, cfg: dict, org: str, github_org: str,
    members: GithubMembers,
) -> list[QualityIssue]:
    """メンバー一覧のうち GitHub login に対応づかない人を警告する。

    メンバー一覧を読めない場合は静かに飛ばす（同じ原因を inspect_input が
    MISSING_MEMBERS として報告するため、ここで二重に出さない）。
    """
    if month is None:
        return []
    try:
        result = ingest.load_members(input_dir, month, cfg)
    except (OSError, ValueError):
        return []
    unmapped = github_collect.unmapped_emails(members, result.df["email"])
    if not unmapped:
        return []
    emails = list(unmapped[:_MAX_LISTED])
    return [_issue(
        Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING,
        f"GitHub login に対応づかないメンバーが {len(unmapped)} 名います"
        f"（例: {', '.join(emails)}）。"
        f"{github_collect.GITHUB_MEMBERS_FILENAME} に github_login を追記してください"
        "（対応づかない人の PR はどのユーザにも帰属しません）",
        org, github_org=github_org, members=len(unmapped), emails=emails,
    )]


def _github_mapping_issues(
    input_dir: Path, month: str | None, cfg: dict, org: str, github_org: str
) -> list[QualityIssue]:
    """email → GitHub login の対応表を検査する。

    読めない対応表はerrorにする（fail-closed）。取り違えに直結する不備（email・login の
    重複、必須カラムの欠落、列ずれ等）と読み取り失敗をまとめて GITHUB_MAPPING_DUPLICATE
    で表す。壊れた対応表を使うと、別人のPRを帰属させたまま集計が完走する。
    """
    filename = github_collect.GITHUB_MEMBERS_FILENAME
    try:
        members = github_collect.load_github_members(input_dir, cfg)
    except (OSError, ValueError) as exc:
        return [_issue(
            Severity.ERROR, IssueCode.GITHUB_MAPPING_DUPLICATE,
            f"{filename} を読めません: {_reason(exc, input_dir)}",
            org, github_org=github_org,
        )]
    if not members.provided:
        return [_issue(
            Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING,
            f"GitHub 分析が有効な組織ですが {filename} がありません"
            "（email → GitHub login の対応表）。組織ディレクトリ直下に置いてください"
            "（無いあいだ PR はどのユーザにも帰属しません）",
            org, github_org=github_org,
        )]
    issues = [
        _issue(
            Severity.WARNING, IssueCode.GITHUB_MAPPING_MISSING, warning,
            org, github_org=github_org,
        )
        for warning in members.warnings
    ]
    issues.extend(
        _github_unmapped_issues(input_dir, month, cfg, org, github_org, members)
    )
    return issues


def inspect_github(
    input_dir: Path | str, month: str | None, cfg: dict, org: str,
    github_org: str, probes: GithubProbes,
) -> list[QualityIssue]:
    """GitHub分析を有効にした1組織分の検査（整列済み）。

    probes は gh の実行結果（`github_collect.probe_github`）で、この関数自身は gh も
    ネットワークも呼ばない。monthは突き合わせるメンバー一覧の選択にだけ使い、scopeには
    持たせない（GitHubの検査結果は対象月に依存しないため）。

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
    issues.extend(_github_mapping_issues(input_dir, month, cfg, org, github_org))
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
