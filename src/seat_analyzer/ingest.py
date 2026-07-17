"""入力 CSV（スペンドレポート / メンバー一覧 / Claude Code 分析）のロード。

ヘッダは正規化（小文字化・空白/アンダースコア統一）してから config.yaml の
エイリアス表と照合するため、実ファイルのカラム名差異はエイリアス追記で吸収できる。

ファイル名は claude.ai からダウンロードしたままの命名（期間付き
`...-2026-06-01-to-2026-06-30.csv`、アンダースコア区切り `..._2026_06_01_to_...`、
スナップショット日付 `members-...-2026-07-05.csv`）と、簡略名 `spend_2026-06.csv`
のいずれも受け付ける。
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# 各入力ファイルの必須カラム（正準名）。ロード時の required= と、config.py の
# エイリアス定義チェックの両方がこの1箇所を参照する（定義の二重管理を避ける）。
REQUIRED_COLUMNS = {
    "spend": ["email", "model", "prompt_tokens", "completion_tokens"],
    "members": ["email", "seat_type"],
    "code_analytics": ["email"],
    "members_info": ["email"],
}

_DAY = r"(?:0[1-9]|[12]\d|3[01])"
_RANGE_RE = re.compile(
    rf"(20\d{{2}})[-_](0[1-9]|1[0-2])[-_]({_DAY})[-_]to[-_](20\d{{2}})[-_](0[1-9]|1[0-2])[-_]({_DAY})"
)
_DATE_RE = re.compile(rf"(20\d{{2}})[-_](0[1-9]|1[0-2])[-_]({_DAY})")
MONTH_RE = re.compile(r"(20\d{2})[-_](0[1-9]|1[0-2])")

# 兼務（複数所属）の区切り: 半角セミコロン / 全角セミコロン
_AFFIL_SEP_RE = re.compile(r"[;；]")


def parse_affiliations(cell) -> list[str]:
    """部署・チームのセル文字列を所属リストへ分割する（兼務対応）。

    半角/全角セミコロンで区切り、各要素を strip、空要素は捨てる。
    空セル・欠損は空リストを返す（＝所属未設定）。
    """
    if cell is None or pd.isna(cell):
        return []
    parts = (p.strip() for p in _AFFIL_SEP_RE.split(str(cell)))
    return [p for p in parts if p]


def normalize_affiliations(cell) -> str:
    """所属セルを正規化した表示文字列にする（半角セミコロン+スペース区切り）。空なら空文字列。"""
    return "; ".join(parse_affiliations(cell))


@dataclass(frozen=True)
class FilePeriod:
    """ファイル名から読み取った対象期間。kind: range=期間 / date=単日スナップショット / month=月のみ。"""

    month: str
    kind: str
    start: dt.date | None = None
    end: dt.date | None = None

    @property
    def days(self) -> int | None:
        """期間の日数（range のみ。date/month は None）。"""
        if self.kind == "range" and self.start and self.end:
            return (self.end - self.start).days + 1
        return None

    def interval(self) -> tuple[dt.date, dt.date]:
        """包含判定用の区間。month は暦上の全月として扱う。"""
        if self.kind == "month":
            year, mon = (int(x) for x in self.month.split("-"))
            return dt.date(year, mon, 1), dt.date(year, mon, calendar.monthrange(year, mon)[1])
        return self.start, self.end


def file_period(path: Path | str) -> FilePeriod | None:
    """ファイル名から対象期間を解釈する。月をまたぐ期間はエラー。"""
    name = Path(path).name
    m = _RANGE_RE.search(name)
    if m:
        start = dt.date(int(m[1]), int(m[2]), int(m[3]))
        end = dt.date(int(m[4]), int(m[5]), int(m[6]))
        if (start.year, start.month) != (end.year, end.month) or end < start:
            raise ValueError(
                f"{name}: 期間が月をまたぐ（または逆転している）エクスポートは扱えません"
                f"（{start}〜{end}）。月単位（1日〜末日）でエクスポートし直してください"
            )
        return FilePeriod(month=f"{start:%Y-%m}", kind="range", start=start, end=end)
    m = _DATE_RE.search(name)
    if m:
        d = dt.date(int(m[1]), int(m[2]), int(m[3]))
        return FilePeriod(month=f"{d:%Y-%m}", kind="date", start=d, end=d)
    m = MONTH_RE.search(name)
    if m:
        return FilePeriod(month=f"{m[1]}-{m[2]}", kind="month")
    return None


@dataclass
class LoadResult:
    """1ファイル分のロード結果。warnings はレポートに転記する。"""

    df: pd.DataFrame
    source: Path
    warnings: list[str] = field(default_factory=list)


def normalize_header(name: str) -> str:
    s = str(name).strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def map_columns(
    df: pd.DataFrame,
    aliases: dict[str, list[str]],
    required: list[str],
    source: Path,
) -> tuple[pd.DataFrame, list[str]]:
    """正規化ヘッダをエイリアス表で正準名にリネームする。"""
    warnings: list[str] = []
    normalized = {col: normalize_header(col) for col in df.columns}
    rename: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        candidates = {normalize_header(a) for a in alias_list} | {normalize_header(canonical)}
        for col, norm in normalized.items():
            if norm in candidates and canonical not in rename.values():
                rename[col] = canonical
                break
    out = df.rename(columns=rename)

    missing = [c for c in required if c not in out.columns]
    if missing:
        raise ValueError(
            f"{source}: 必須カラムが見つかりません: {missing}\n"
            f"  実ファイルのヘッダ: {list(df.columns)}\n"
            f"  config.yaml > columns にエイリアスを追記してください"
        )
    optional_missing = [c for c in aliases if c not in out.columns and c not in required]
    if optional_missing:
        warnings.append(f"{source.name}: 任意カラムなし: {optional_missing}")
    return out, warnings


def month_of_file(path: Path) -> str | None:
    period = file_period(path)
    return period.month if period else None


def _resolve_duplicates(
    directory: Path, month: str, entries: list[tuple[FilePeriod, Path]],
    snapshot_note: str | None = None,
) -> tuple[Path, str]:
    """同一月に複数ファイルがある場合の解決。

    - 全て単日スナップショット（members 等）→ 最新日付を採用
    - 期間の包含関係が一意（例: 全月分が部分月分を包含）→ 広い方を採用
    - どちらでもない → エラー（取り違え防止）

    snapshot_note を渡すと、主データに採らなかったファイルを「未使用」ではなく
    「<snapshot_note>に ... も使用」という文言にする（月中推移の差分分析が発動する場合）。
    差分の種類ごとに文言を変えるため、呼び出し側が説明句（例: "スナップショット差分" /
    "メンバー変動の検出"）を渡す。
    """
    if all(p.kind == "date" for p, _ in entries):
        period, path = max(entries, key=lambda e: e[0].end)
        others = ", ".join(f.name for p, f in entries if f != path)
        tail = f"（{snapshot_note}に {others} も使用）" if snapshot_note else f"（未使用: {others}）"
        return path, (
            f"{directory.name}: {month} のスナップショットが複数あるため"
            f"最新の {path.name} を使用{tail}"
        )

    intervals = [(p.interval(), path) for p, path in entries]
    containing = [
        (iv, path) for iv, path in intervals
        if all(iv[0] <= o[0] and o[1] <= iv[1] for o, _ in intervals)
    ]
    # 最大区間が一意のときだけ自動解決する（同一区間が複数なら取り違えの可能性）
    if len(containing) == 1:
        (start, end), path = containing[0]
        others = ", ".join(f.name for _, f in intervals if f != path)
        if snapshot_note:
            return path, (
                f"{directory.name}: {month} のファイルが複数あるため主データには期間の広い "
                f"{path.name}（{start:%m-%d}〜{end:%m-%d}）を使用"
                f"（{snapshot_note}に {others} も使用）"
            )
        return path, (
            f"{directory.name}: {month} のファイルが複数あるため期間の広い "
            f"{path.name}（{start:%m-%d}〜{end:%m-%d}）を使用（未使用: {others}）"
        )
    raise ValueError(
        f"{directory}: {month} のCSVが複数あり期間から優先順を判断できません"
        f"（{', '.join(f.name for _, f in entries)}）。対象月のファイルを1つに絞ってください"
    )


def _files_by_month(
    directory: Path, snapshot_month: str | None = None, snapshot_note: str | None = None
) -> tuple[dict[str, Path], dict[str, str]]:
    """月→ファイルの対応と、同一月の重複を自動解決した際の警告（月別）を返す。

    snapshot_month を渡すと、その月の重複解決の警告文言を snapshot_note の説明句で
    「差分にも使う」向けに切り替える（月中推移の差分分析が発動する月のみ）。
    """
    by_month: dict[str, list[tuple[FilePeriod, Path]]] = {}
    if not directory.exists():
        return {}, {}
    for p in sorted(directory.glob("*.csv")):
        period = file_period(p)
        if period:
            by_month.setdefault(period.month, []).append((period, p))
    result: dict[str, Path] = {}
    warns: dict[str, str] = {}
    for month, entries in by_month.items():
        if len(entries) == 1:
            result[month] = entries[0][1]
        else:
            result[month], warns[month] = _resolve_duplicates(
                directory, month, entries,
                snapshot_note=snapshot_note if month == snapshot_month else None,
            )
    return result, warns


def discover_months(input_dir: Path) -> list[str]:
    """スペンドレポートが存在する月の一覧（昇順）。"""
    files, _ = _files_by_month(Path(input_dir) / "spend")
    return sorted(files)


def spend_file_period(input_dir: Path, month: str) -> FilePeriod | None:
    """対象月のスペンドレポートのファイル名期間（--preview の観測日数自動判別用）。"""
    files, _ = _files_by_month(Path(input_dir) / "spend")
    return file_period(files[month]) if month in files else None


def discover_orgs(input_dir: Path) -> list[str]:
    """input_dir 直下の組織サブディレクトリ（spend/ を持つもの）の一覧（昇順）。"""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        return []
    return sorted(
        p.name for p in input_dir.iterdir() if p.is_dir() and (p / "spend").is_dir()
    )


# 組織名は出力パスと Markdown リンクに使うため、それらを壊す文字を禁止する。
# 日本語などの名前は許可し、パス区切り・Markdown/HTML を壊す文字のみ拒否する。
_ORG_NAME_BAD_CHARS = re.compile(r"[/\\|\[\]()<>\r\n\t]")


def validate_org_name(org: str) -> None:
    """組織名（input/ 直下のディレクトリ名）の妥当性検証。不正なら ValueError。

    init-org でユーザが指定する名前と、既存ディレクトリから発見した組織名の両方で使う。
    """
    if org == "summary":
        raise ValueError(
            "組織名 'summary' は横断サマリの出力先（reports/summary/）として予約されています"
        )
    if not org or org != org.strip() or org.startswith("."):
        raise ValueError(
            f"組織名が不正です: {org!r}（空・先頭のドット・前後空白は使えません）"
        )
    if _ORG_NAME_BAD_CHARS.search(org):
        raise ValueError(
            f"組織名に使えない文字が含まれます: {org!r}"
            "（パス区切りや | [ ] ( ) < > 改行・タブは使えません）"
        )


def _read_csv(path: Path) -> pd.DataFrame:
    # utf-8-sig は BOM 無しの UTF-8 も読めるため、utf-8-sig と cp932 の2種で足りる
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: 文字コードを判別できません（utf-8 / cp932 を試行）")


def _to_numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(
                df[c].astype(str).str.replace(",", "").str.replace("$", ""),
                errors="coerce",
            )


def _read_spend_df(path: Path, month: str, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """1つのスペンド CSV を読み、カラム正規化・数値化・email 正規化を施す。"""
    df = _read_csv(path)
    df, warnings = map_columns(
        df,
        cfg["columns"]["spend"],
        required=REQUIRED_COLUMNS["spend"],
        source=path,
    )
    _to_numeric(df, [
        "requests", "prompt_tokens", "completion_tokens", "net_spend",
        "uncached_input_tokens", "cache_read_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
    ])
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df["month"] = month
    return df, warnings


def load_spend(
    input_dir: Path, month: str, cfg: dict, snapshot_active: bool = False
) -> LoadResult:
    """対象月の主スペンドをロードする。

    snapshot_active=True は、同一月に複数の月初開始スペンドがあり月中推移の差分分析が
    発動する場合で、重複解決の警告文言を「スナップショット差分にも使う」向けにする。
    """
    files, file_warns = _files_by_month(
        Path(input_dir) / "spend",
        snapshot_month=month if snapshot_active else None,
        snapshot_note="スナップショット差分",
    )
    if month not in files:
        raise FileNotFoundError(
            f"{input_dir}/spend/ に {month} のスペンドレポートがありません"
            f"（例: spend_{month}.csv）。存在する月: {sorted(files) or 'なし'}"
        )
    path = files[month]
    df, warnings = _read_spend_df(path, month, cfg)
    if month in file_warns:
        warnings.append(file_warns[month])
    return LoadResult(df=df, source=path, warnings=warnings)


def load_spend_file(path: Path, month: str, cfg: dict) -> pd.DataFrame:
    """指定パスのスペンド CSV を1つだけ読む（スナップショット差分用・重複解決や警告なし）。"""
    df, _ = _read_spend_df(Path(path), month, cfg)
    return df


def spend_snapshots(
    input_dir: Path, month: str
) -> tuple[list[tuple[FilePeriod, Path]], list[str]]:
    """対象月の月初開始（1日〜）の累積スペンドを end 昇順に返す（月中推移の差分分析用）。

    戻り値: (entries, excluded)。entries は月初開始 range の (FilePeriod, パス)、
    excluded は「月初開始でない range のため差分対象から外したファイル名」。
    kind=month / kind=date のファイルは対象外（区間差分の起点にならない）。
    """
    directory = Path(input_dir) / "spend"
    entries: list[tuple[FilePeriod, Path]] = []
    excluded: list[str] = []
    if directory.exists():
        for p in sorted(directory.glob("*.csv")):
            period = file_period(p)
            if period is None or period.month != month or period.kind != "range":
                continue
            if period.start is None or period.start.day != 1:
                excluded.append(p.name)
                continue
            entries.append((period, p))
    entries.sort(key=lambda e: e[0].end)
    return entries, excluded


def member_snapshots(input_dir: Path, month: str) -> list[tuple[FilePeriod, Path]]:
    """対象月の単日スナップショット members を日付昇順で返す（月中のメンバー変動の差分用）。

    kind=date のファイルのみ対象（時点が特定できる）。kind=month（members_2026-07.csv）は
    時点不明のため差分の起点にならず除外する。
    """
    directory = Path(input_dir) / "members"
    entries: list[tuple[FilePeriod, Path]] = []
    if directory.exists():
        for p in sorted(directory.glob("*.csv")):
            period = file_period(p)
            if period is None or period.month != month or period.kind != "date":
                continue
            entries.append((period, p))
    entries.sort(key=lambda e: e[0].start)
    return entries


def _normalize_seat(value: str) -> str:
    s = str(value).strip().lower()
    if "premium" in s:
        return "premium"
    if "standard" in s:
        return "standard"
    # 意図的な未割当（別組織でアサイン済み・管理者等）。判定対象外として扱う
    if "unassigned" in s:
        return "unassigned"
    return "unknown"


def _read_members_df(path: Path, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """1つの members CSV を読み、カラム正規化・email/seat 正規化・重複解決を施す。"""
    df = _read_csv(path)
    df, warnings = map_columns(
        df,
        cfg["columns"]["members"],
        required=REQUIRED_COLUMNS["members"],
        source=path,
    )
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df["seat_type"] = df["seat_type"].map(_normalize_seat)
    unknown = df[df["seat_type"] == "unknown"]
    if not unknown.empty:
        warnings.append(
            f"members: シート種別を判別できないユーザ {len(unknown)} 名"
            f"（値に premium/standard を含まない）: {unknown['email'].head(5).tolist()}"
        )
    df = df.drop_duplicates(subset="email", keep="last")
    return df, warnings


def load_members_file(path: Path, cfg: dict) -> pd.DataFrame:
    """指定パスの members CSV を1つだけ読む（メンバー変動の差分用・重複解決や警告なし）。"""
    df, _ = _read_members_df(Path(path), cfg)
    return df


def load_members(input_dir: Path, month: str, cfg: dict, snapshot_active: bool = False) -> LoadResult:
    """対象月のメンバー一覧。無ければ直近の過去月にフォールバック（警告付き）。

    snapshot_active=True は、対象月に単日スナップショットが複数ありメンバー変動の差分分析が
    発動する場合で、重複解決の警告文言を「メンバー変動の検出にも使う」向けにする。
    """
    files, file_warns = _files_by_month(
        Path(input_dir) / "members",
        snapshot_month=month if snapshot_active else None,
        snapshot_note="メンバー変動の検出",
    )
    warnings: list[str] = []
    if not files:
        raise FileNotFoundError(
            f"{input_dir}/members/ にメンバー一覧がありません"
            f"（例: members_{month}.csv。最低限 email,seat_type の2列で可）"
        )
    if month in files:
        path = files[month]
    else:
        earlier = [m for m in sorted(files) if m <= month]
        if earlier:
            path = files[earlier[-1]]
            warnings.append(
                f"members: {month} のファイルが無いため {path.name} を使用（シート構成が最新でない可能性）"
            )
        else:
            # 過去分析（バックフィル）で当時の members が無いケース。未来月しか無い旨を明示する
            path = files[sorted(files)[0]]
            warnings.append(
                f"members: {month} 以前のファイルが無いため未来月の {path.name} を使用。"
                "対象月当時のシート構成と異なる可能性が高いため、判定は参考値として扱ってください"
            )
    used_month = month_of_file(path)
    if used_month in file_warns:
        warnings.append(file_warns[used_month])
    df, w = _read_members_df(path, cfg)
    warnings.extend(w)
    return LoadResult(df=df, source=path, warnings=warnings)


def load_members_info(input_dir: Path, cfg: dict) -> LoadResult | None:
    """部署・チーム・職種・備考のマッピング（任意ファイル members-info.csv）。無ければ None。

    org の入力ディレクトリ直下にファイル名固定で置く（月情報なし・手動メンテ）。
    email 列のみ必須。department/team/role/note が無くても警告は出さず、空文字列列で補完する。
    """
    path = Path(input_dir) / "members-info.csv"
    if not path.exists():
        return None
    df = _read_csv(path)
    # department/team/role/note が無い場合の「任意カラムなし」警告は捨てる（完全に任意のため）
    df, _ = map_columns(
        df,
        cfg["columns"]["members_info"],
        required=REQUIRED_COLUMNS["members_info"],
        source=path,
    )
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    for col in ("department", "team", "role", "note"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    df = df.drop_duplicates(subset="email", keep="last")
    return LoadResult(df=df, source=path, warnings=[])


def _read_code_df(path: Path, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """1つの code-analytics CSV を読み、カラム正規化・数値化・email 正規化を施す。"""
    df = _read_csv(path)
    df, warnings = map_columns(
        df,
        cfg["columns"]["code_analytics"],
        required=REQUIRED_COLUMNS["code_analytics"],
        source=path,
    )
    _to_numeric(df, ["prs_with_cc", "prs_total", "loc_with_cc", "loc_total"])
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    df = df.drop_duplicates(subset="email", keep="last")
    return df, warnings


def load_code_analytics_file(path: Path, month: str, cfg: dict) -> pd.DataFrame:
    """指定パスの code-analytics CSV を1つだけ読む（活動の差分用・重複解決や警告なし）。

    month は呼び出し側との整合のために受け取るが、code-analytics は月列を持たないため
    読み込み自体には用いない（差分側でファイル名の期間から時点を判別する）。
    """
    df, _ = _read_code_df(Path(path), cfg)
    return df


def code_snapshots(input_dir: Path, month: str) -> list[tuple[FilePeriod, Path]]:
    """対象月の期間/単日スナップショット code-analytics を end 昇順で返す（活動の差分用）。

    kind=date（時点=当日）または kind=range（時点=期間末）を対象にする。
    kind=month（cc_2026-07.csv）は時点不明のため差分の起点にならず除外する。
    """
    directory = Path(input_dir) / "code-analytics"
    entries: list[tuple[FilePeriod, Path]] = []
    if directory.exists():
        for p in sorted(directory.glob("*.csv")):
            period = file_period(p)
            if period is None or period.month != month or period.kind not in ("date", "range"):
                continue
            entries.append((period, p))
    entries.sort(key=lambda e: e[0].end)
    return entries


def load_code_analytics(
    input_dir: Path, month: str, cfg: dict, snapshot_active: bool = False
) -> LoadResult | None:
    """Claude Code 貢献データ（任意）。無ければ None。

    snapshot_active=True は、対象月にスナップショットが複数あり活動の差分分析が発動する
    場合で、重複解決の警告文言を「Claude Code 活動の差分にも使う」向けにする。
    """
    files, file_warns = _files_by_month(
        Path(input_dir) / "code-analytics",
        snapshot_month=month if snapshot_active else None,
        snapshot_note="Claude Code 活動の差分",
    )
    if month not in files:
        return None
    path = files[month]
    df, warnings = _read_code_df(path, cfg)
    if month in file_warns:
        warnings.append(file_warns[month])
    return LoadResult(df=df, source=path, warnings=warnings)
