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
import math
import numbers
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# 組織ディレクトリ直下に置かれる入力サブディレクトリ名。組織の発見処理は「これらを
# 持つディレクトリが組織」という構造判定にこの一覧を参照する。
INPUT_SUBDIRS = ("spend", "members", "code-analytics")

# 各入力ファイルの必須カラム（正準名）。ロード時の required= と、config.py の
# エイリアス定義チェックの両方がこの1箇所を参照する（定義の二重管理を避ける）。
REQUIRED_COLUMNS = {
    "spend": ["email", "model", "prompt_tokens", "completion_tokens"],
    "members": ["email", "seat_type"],
    "code_analytics": ["email"],
    "members_info": ["email"],
    # 管理画面の表を人手で写す admin/ の手入力CSV（読み取りは admin_inputs.py）。
    # 取得日と取得元はどちらの表にも要る（時点を特定できない設定値は判定に使えない）
    "admin_organization": ["snapshot_date", "source"],
    "admin_users": ["snapshot_date", "email", "source"],
    # email → GitHub login の対応表（読み取りは github_collect.py）。対応づけが目的の
    # 表なので、どちらの列も欠けると表そのものが成立しない
    "github_members": ["email", "github_login"],
}

# Step 1で正準化するSpend任意列。既存の任意列は「列なし」を利用する処理があるため、
# 一律には補完せず、この4列だけを欠損時にNAで追加する。
SPEND_OPTIONAL_COLUMNS = (
    "account_uuid",
    "user_id",
    "gross_spend",
    "web_search_count",
)

# 旧形式の公開サンプルにも account_uuid / gross_spend は存在するため、現行形式を
# 識別できる追加列だけをマーカーにする。いずれかがあれば4列の部分欠損を警告する。
SPEND_CURRENT_SCHEMA_MARKERS = ("user_id", "web_search_count")

# Step 2で正準化するMembers任意列。既存CSVとの互換性を保つため、
# 列がない場合は警告を増やさずNAで補完する。
MEMBERS_OPTIONAL_COLUMNS = (
    "account_uuid",
    "user_id",
    "member_status",
)

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


# 追加クレジット上限「無制限」の表記ゆれ（有効・上限なしを表す）。
# 「なし/無し/none」は「上限なし」とも「クレジットなし（無効）」とも読める多義語のため
# 受け付けず、解釈不能の警告に倒す（無効は 0、無制限はこのトークンで明示させる）
_UNLIMITED_TOKENS = {"無制限", "unlimited", "inf", "∞"}

# 円記号（半角・全角）。金額は USD 建てなので、円記号つきのセルは解釈不能に倒す
# （通貨の取り違えは金額の桁が変わる）。除去する記号の一覧に円記号を入れないことでも
# 同じ結果になるが、規則を一覧の中身に依存させないためここで明示的に閉じる
_YEN_RE = re.compile(r"[¥￥]")


def _is_blank(cell) -> bool:
    """未記入（None・NaN・NaT・pd.NA）かどうか。文字列は対象にしない。"""
    if cell is None:
        return True
    if isinstance(cell, str):
        return False
    try:
        missing = pd.isna(cell)
    except ArithmeticError:
        # 欠損かどうかを問うだけで例外を出す値がある（signaling NaN）。未記入ではない
        # ものとして後段へ流し、数値として読めないことを不明+警告で表す
        return False
    # 配列を渡された場合は要素ごとの結果が返る（1つのセルの判定には使えない）
    return missing if isinstance(missing, bool) else False


# 数値として受ける字句（符号・小数点・指数のみ。桁区切りは呼び出し側で除去済み）。
# 数値への変換は字句中の "_" を桁区切りとして無視し、全角数字も受けるため、"1_0" や
# "２５０" のような書き間違いが黙って別の値になる。受ける形をここで先に決める。
# 指数部を4桁までにするのは、値の大きさを業務の上限として決めているのではなく、
# 数値として書かれた字句の形を決めているため（それを超える指数は、整数へ写した後の
# 桁数が実体化と表示に耐えず、不明として扱うほうが「解釈できない値は不明+警告」の
# 約束を保てる）
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")


def is_number_text(text: str) -> bool:
    """数値として読める字句か（桁区切り・通貨記号は呼び出し側で除去してから渡す）。

    数値の解釈を持つ全ての入力（追加クレジット上限・admin の金額と個数）が同じ規則を
    使う。規則を書き写すと、片方だけが緩いまま残る。
    """
    return _NUMBER_RE.fullmatch(text) is not None


def _strip_currency(text: str) -> str:
    """通貨記号（$・全角＄）と桁区切りを取り除く。円記号は対象にしない。"""
    return text.replace("$", "").replace("＄", "").replace(",", "").strip()


def _as_number(cell) -> object | None:
    """数として書かれた値だけを返す（そうでなければ None ＝ 解釈不能）。

    契約: 金額として読むのは、数の階層に属し実数として扱える値だけ（真偽値を除く）。

    虚部を持つ値は読まない（float() が虚部を黙って捨てるため、金額が別の値になる）。
    bytes や `__float__` だけを持つ型のように数の階層に属さない値も読まない（型を問わず
    float() へ通すと、金額として書かれていない値が上限になる）。真偽値は
    float(True) = 1.0 として読めてしまうため、数ではあっても除く（読み取りライブラリが
    返す真偽値型のように bool のサブクラスでないものは、数の階層に属さないので前の
    規則で落ちる）。
    """
    if isinstance(cell, bool) or not isinstance(cell, numbers.Number):
        return None
    if isinstance(cell, numbers.Complex) and not isinstance(cell, numbers.Real):
        return None
    return cell


def parse_credit_limit(cell) -> tuple[float, str | None]:
    """追加クレジット上限セルを (値, 警告) に解釈する。

    値の意味は「クレジットモード」の導出（analyze.credits_mode）で使う:
      - 正の数値（"$" やカンマ許容）→ その金額（有効・上限 κ）
      - 0 → 0.0（無効）
      - 無制限 / unlimited → inf（有効・上限なし）
      - 空欄 / NaN → NaN（不明）
      - 負値・解釈不能な値 → NaN + 警告（不明扱い。データ入力ミスの検出用）

    「無制限」の語彙を認めるのは文字列として書かれた場合だけとする。数値の無限大は
    str() で "inf" になるため、文字列へ写してから語彙を見ると両者を区別できない。
    数（_as_number）は型を問わず float() の1経路に通し、変換できない値・有限でない値・
    負値を不明へ倒す。文字列は `is_number_text` の字句規則を通ったものだけを数値として
    読む。上限は $ 建てなので、円記号を含むセルも解釈不能に倒す（通貨の取り違えを金額と
    して通さない）。
    """
    if _is_blank(cell):
        return float("nan"), None
    if isinstance(cell, str):
        shown = cell.strip()
        if shown == "" or shown.lower() == "nan":
            return float("nan"), None
        if shown.lower() in _UNLIMITED_TOKENS:
            return float("inf"), None
        number = None if _YEN_RE.search(shown) else _strip_currency(shown)
        if number is not None and not is_number_text(number):
            number = None
    else:
        shown = cell
        number = _as_number(cell)
    unreadable = f"追加クレジット上限を解釈できません: {shown!r}（不明として扱います）"
    if number is None:
        return float("nan"), unreadable
    try:
        value = float(number)
    except (TypeError, ValueError, OverflowError):
        return float("nan"), unreadable
    if not math.isfinite(value):
        return float("nan"), unreadable
    if value < 0:
        return float("nan"), f"追加クレジット上限が負値です: {shown!r}（不明として扱います）"
    return value, None


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
    return re.sub(r"\s+", " ", s)


def map_columns(
    df: pd.DataFrame,
    aliases: dict[str, list[str]],
    required: list[str],
    source: Path,
    fill_na_columns: tuple[str, ...] = (),
    partial_schema_markers: tuple[str, ...] = (),
) -> tuple[pd.DataFrame, list[str]]:
    """正規化ヘッダを正準名にリネームし、指定された任意列をNAで補完する。

    partial_schema_markers のいずれかが入力にあれば、fill_na_columns の部分欠損も
    警告する。マーカーがない旧形式は、後方互換のため補完だけ行い警告しない。
    """
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
    mapped_columns = set(out.columns)

    missing = [c for c in required if c not in mapped_columns]
    if missing:
        raise ValueError(
            f"{source}: 必須カラムが見つかりません: {missing}\n"
            f"  実ファイルのヘッダ: {list(df.columns)}\n"
            f"  config.yaml > columns にエイリアスを追記してください"
        )
    fill_missing = [c for c in fill_na_columns if c not in mapped_columns]
    optional_missing = [
        c
        for c in aliases
        if c not in mapped_columns and c not in required and c not in fill_na_columns
    ]
    if any(marker in mapped_columns for marker in partial_schema_markers):
        optional_missing.extend(fill_missing)
    for column in fill_missing:
        out[column] = pd.NA
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
        _, path = max(entries, key=lambda e: e[0].end)
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


def _csv_periods(directory: Path) -> list[tuple[FilePeriod, Path]]:
    """ディレクトリ内の CSV のうち期間を解釈できたものを、ファイル名の昇順で返す。"""
    if not directory.exists():
        return []
    entries: list[tuple[FilePeriod, Path]] = []
    for p in sorted(directory.glob("*.csv")):
        period = file_period(p)
        if period:
            entries.append((period, p))
    return entries


def _files_by_month(
    directory: Path, snapshot_month: str | None = None, snapshot_note: str | None = None
) -> tuple[dict[str, Path], dict[str, str]]:
    """月→ファイルの対応と、同一月の重複を自動解決した際の警告（月別）を返す。

    snapshot_month を渡すと、その月の重複解決の警告文言を snapshot_note の説明句で
    「差分にも使う」向けに切り替える（月中推移の差分分析が発動する月のみ）。
    """
    by_month: dict[str, list[tuple[FilePeriod, Path]]] = {}
    for period, p in _csv_periods(directory):
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
# 日本語などの名前は許可し、パス区切り・Markdown/HTML を壊す文字と、Windows が
# ファイル名に使えない文字（: * ? " と制御文字 0x00-0x1f）のみ拒否する。
_ORG_NAME_BAD_CHARS = re.compile(r"[/\\|\[\]()<>:*?\"\x00-\x1f]")

# Windows がデバイスとして特別扱いする名前（拡張子が付いていても同じ）。ディレクトリを
# 作れない・作れても開けないため、ある OS で用意したデータをそのまま別の OS へ持ち込める
# よう全 OS で拒否する。上付き数字の変種まで含めて Microsoft の命名規則に合わせる。
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{dev}{i}" for dev in ("COM", "LPT") for i in range(10)]
    + [f"{dev}{sup}" for dev in ("COM", "LPT") for sup in "¹²³"]
)


def validate_org_name(org: str) -> None:
    """組織名（input/ 直下のディレクトリ名）の妥当性検証。不正なら ValueError。

    init-org でユーザが指定する名前と、既存ディレクトリから発見した組織名の両方で使う。
    """
    # 大文字小文字を無視して比較する。既定の Windows / macOS のファイルシステムでは
    # reports/SUMMARY が reports/summary と同じ場所になり、横断サマリを上書きする
    if org.casefold() == "summary":
        raise ValueError(
            "組織名 'summary' は横断サマリの出力先（reports/summary/）として予約されています"
        )
    # input/ 直下の spend/ は旧レイアウトの目印として拒否されるため、この名前の組織は
    # 作った時点で分析できない。大文字小文字を無視して比較するのは、既定の
    # Windows / macOS のファイルシステムでは input/Spend も input/spend になるため
    # （名前の可否がファイルシステムによって変わらないようにする）
    if org.casefold() == "spend":
        raise ValueError(
            f"組織名 {org!r} は旧レイアウトの目印（input/ 直下の spend/）と"
            "区別できないため予約されています"
        )
    if not org or org != org.strip() or org.startswith("."):
        raise ValueError(
            f"組織名が不正です: {org!r}（空・先頭のドット・前後空白は使えません）"
        )
    if _ORG_NAME_BAD_CHARS.search(org):
        raise ValueError(
            f"組織名に使えない文字が含まれます: {org!r}"
            "（パス区切りや | [ ] ( ) < > : * ? \" 改行・タブは使えません）"
        )
    # 末尾のドットは Windows が黙って落とすため、input/ と reports/ の名前が食い違う
    if org.endswith("."):
        raise ValueError(
            f"組織名が不正です: {org!r}（末尾のドットは Windows で無視されます）"
        )
    # 拡張子より前だけを見る（Windows はデバイス名を拡張子付きでも同じ扱いにする）。
    # 末尾の空白も落ちるため、"NUL .txt" のような書き方で抜けないよう除去して比較する
    if org.partition(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(
            f"組織名 {org!r} は Windows の予約デバイス名のため使えません"
        )


def check_org_name_collisions(orgs: list[str]) -> None:
    """同じ出力先になる組織名の組み合わせを拒否する。

    大文字小文字だけが違う名前は、既定の Windows / macOS のファイルシステムでは
    同じディレクトリになり、後に書いた組織が前の組織の成果物を上書きする。入力側が
    大文字小文字を区別する環境（Linux・ネットワーク共有）なら両方が独立に存在
    しうるため、1文字も書き込む前に止める。
    Unicode の正規化形式だけが違う名前（合成済みの「ガ」と、分解した「カ」＋濁点）も
    同様に衝突する。macOS の既定は正規化を区別しないため、比較の前に NFC へ揃える。
    完全一致は重複指定として許す（同じ組織を2回指定しても害はない）。
    """
    seen: dict[str, str] = {}
    for org in orgs:
        key = unicodedata.normalize("NFC", org).casefold()
        first = seen.setdefault(key, org)
        if first != org:
            raise ValueError(
                f"組織名 {first!r} と {org!r} は大文字小文字や文字の合成の違いだけなので、"
                "同じ出力先になる環境があります。どちらかを改名してください"
            )


def validate_org_names(orgs: list[str]) -> None:
    """組織名の集合としての妥当性検証。個々の検証に加えて名前の衝突を見る。"""
    for org in orgs:
        validate_org_name(org)
    check_org_name_collisions(orgs)


def read_csv(path: Path, *, dtype=None) -> pd.DataFrame:
    # utf-8-sig は BOM 無しの UTF-8 も読めるため、utf-8-sig と cp932 の2種で足りる
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return pd.read_csv(path, encoding=encoding, dtype=dtype)
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


def _clean_string_columns(df: pd.DataFrame, columns: tuple[str, ...]) -> None:
    """文字列列の前後空白を除去し、空白だけの値を欠損へ統一する。"""
    for column in columns:
        values = df[column].astype("string").str.strip()
        df[column] = values.mask(values == "")


def _read_spend_df(path: Path, month: str, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """1つのスペンド CSV を読み、カラム正規化・数値化・email 正規化を施す。"""
    # IDの先頭ゼロや、欠損混在時の ".0" 付与を防ぐため、Spendはまず文字列で読む。
    # 数値列は正準化後に _to_numeric で明示的に変換する。
    df = read_csv(path, dtype=str)
    df, warnings = map_columns(
        df,
        cfg["columns"]["spend"],
        required=REQUIRED_COLUMNS["spend"],
        source=path,
        fill_na_columns=SPEND_OPTIONAL_COLUMNS,
        partial_schema_markers=SPEND_CURRENT_SCHEMA_MARKERS,
    )
    _to_numeric(df, [
        "requests", "prompt_tokens", "completion_tokens", "gross_spend", "net_spend",
        "web_search_count",
        "uncached_input_tokens", "cache_read_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
    ])
    _clean_string_columns(df, ("account_uuid", "user_id"))
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


def member_files(input_dir: Path) -> list[tuple[FilePeriod, Path]]:
    """members の単日スナップショット全件を日付昇順で返す（全月）。

    kind=date のファイルのみ対象（時点が特定できる）。kind=month（members_2026-07.csv）は
    時点不明のため差分の起点にならず除外する。

    同じ日付のファイルが複数ある場合はすべて残し、ファイル名の昇順で並べる（採用する1本を
    選ぶのは load_members の役割で、差分は時点の列そのものを必要とするため）。
    """
    directory = Path(input_dir) / "members"
    entries: list[tuple[FilePeriod, Path]] = []
    if directory.exists():
        for p in sorted(directory.glob("*.csv")):
            period = file_period(p)
            if period is None or period.kind != "date":
                continue
            entries.append((period, p))
    entries.sort(key=lambda e: e[0].start)
    return entries


def member_snapshots(input_dir: Path, month: str) -> list[tuple[FilePeriod, Path]]:
    """対象月の単日スナップショット members を日付昇順で返す（月中のメンバー変動の差分用）。"""
    return [(p, path) for p, path in member_files(input_dir) if p.month == month]


def member_info_files(input_dir: Path) -> list[tuple[FilePeriod, Path]]:
    """members-info-*.csv の日付つきスナップショット全件を日付昇順で返す（全月）。

    固定名 members-info.csv（日付なし）は kind!=date のため除外される。
    月末以前で最新の採用・フォールバックは load_members_info が担う。
    """
    directory = Path(input_dir)
    entries: list[tuple[FilePeriod, Path]] = []
    if directory.exists():
        for p in sorted(directory.glob("members-info*.csv")):
            period = file_period(p)
            if period is None or period.kind != "date":
                continue
            entries.append((period, p))
    entries.sort(key=lambda e: e[0].start)
    return entries


def member_info_snapshots(input_dir: Path, month: str) -> list[tuple[FilePeriod, Path]]:
    """対象月の日付つき members-info スナップショットを日付昇順で返す（月中の κ 変更の差分用）。"""
    return [(p, path) for p, path in member_info_files(input_dir) if p.month == month]


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


def _normalize_emails(values: pd.Series, *, keep_missing: bool) -> pd.Series:
    """email 列を突き合わせ用に揃える（前後空白の除去 → 小文字化）。

    keep_missing=True は欠損セル・空白だけのセルを欠損のまま残す。既定は文字列へ落とし、
    email をキーに1行1ユーザへ畳む経路がそのまま使える形にする。
    """
    if keep_missing:
        normalized = values.astype("string").str.strip().str.lower()
        return normalized.mask(normalized == "")
    return values.astype(str).str.strip().str.lower()


def _read_members_df(
    path: Path, cfg: dict, *, keep_rows: bool = False
) -> tuple[pd.DataFrame, list[str]]:
    """1つの members CSV を読み、カラム正規化・email/seat 正規化・重複解決を施す。

    keep_rows=True は行をそのまま保つ。email での畳み込みをせず、email を持たない行も
    落とさない（欠損は文字列にせず欠損のまま）。同一 email の複数行や email の無い行が
    あることそのものが判断材料になる、Identity 解決へ渡す形。
    """
    # 数値形式IDの先頭ゼロを保持するため、Membersはまず文字列で読む。
    df = read_csv(path, dtype=str)
    df, warnings = map_columns(
        df,
        cfg["columns"]["members"],
        required=REQUIRED_COLUMNS["members"],
        source=path,
        fill_na_columns=MEMBERS_OPTIONAL_COLUMNS,
    )
    df["email"] = _normalize_emails(df["email"], keep_missing=keep_rows)
    _clean_string_columns(df, MEMBERS_OPTIONAL_COLUMNS)
    df["seat_type"] = df["seat_type"].map(_normalize_seat)
    unknown = df[df["seat_type"] == "unknown"]
    if not unknown.empty:
        warnings.append(
            f"members: シート種別を判別できないユーザ {len(unknown)} 名"
            f"（値に premium/standard を含まない）: {unknown['email'].head(5).tolist()}"
        )
    if not keep_rows:
        df = df.drop_duplicates(subset="email", keep="last")
    return df, warnings


def load_members_file(path: Path, cfg: dict) -> pd.DataFrame:
    """指定パスの members CSV を1つだけ読む（メンバー変動の差分用・重複解決や警告なし）。"""
    df, _ = _read_members_df(Path(path), cfg)
    return df


def load_member_rows(path: Path, cfg: dict) -> pd.DataFrame:
    """指定パスの members CSV を、行を畳まずに1つだけ読む（シート変更 event の検出用）。

    正規化の規則は load_members_file と同じで、同一 email の行を1つに畳まない点と、
    email を持たない行を欠損のまま残す点だけが違う。同一時点に矛盾する行があること・
    email の無い行があること自体を Identity 解決の側で扱えるようにするため、ここで
    行を失わせない。
    """
    df, _ = _read_members_df(Path(path), cfg, keep_rows=True)
    return df


# 対象月末より後のスナップショットを「通常運用の範囲」とみなす日数。月末までのデータは
# 翌月の最初の営業日に取得するのが通常運用で、祝祭日でその日が数日ずれても同じ運用に
# あたる。この幅を超えて離れたファイルだけ、対象月当時の構成と異なる旨の強い注意を付ける。
_MONTH_END_NEAR_DAYS = 7


def _month_end(month: str) -> dt.date:
    """対象月（YYYY-MM）の末日。"""
    year, mon = (int(x) for x in month.split("-"))
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])


def is_near_month_end(source: Path | str, month: str) -> bool:
    """ファイルが対象月末の直後（＝通常のエクスポート運用の範囲）かどうか。

    対象月末より後のファイルを「当時の構成として扱ってよいか」の判断を1箇所に閉じる
    ための述語。載せる警告の強さをここで揃えるので、日数の条件を呼び出し側へ書き写さない
    こと。末日以前（対象月内・過去月）と、ファイル名から期間を解釈できない場合は False
    （この述語が答えるのは「末日より後だが通常運用の範囲か」だけ）。
    """
    period = file_period(source)
    if period is None:
        return False
    return 0 < (period.interval()[1] - _month_end(month)).days <= _MONTH_END_NEAR_DAYS


def _nearest_to_month_end(
    entries: list[tuple[FilePeriod, Path]], month: str
) -> tuple[FilePeriod, Path, int]:
    """対象月の末日に最も近いファイルと、その末日からの日数（負なら末日以前）を返す。

    代表日はファイルの期間の終わり（単日スナップショットはその日、月のみはその月の
    末日）を使い、3種の命名を1つの式で扱う。同距離のときは末日以前を優先し（対象月
    より後の変更を含みえないため）、なお同じなら期間の広い方・ファイル名の昇順で決める
    （選択がディレクトリの列挙順に依存しないようにする）。
    """
    month_end = _month_end(month)

    def rank(entry: tuple[FilePeriod, Path]) -> tuple:
        period, path = entry
        start, end = period.interval()
        delta = (end - month_end).days
        return (abs(delta), delta > 0, -(end - start).days, path.name)

    period, path = min(entries, key=rank)
    return period, path, (period.interval()[1] - month_end).days


def load_members(input_dir: Path, month: str, cfg: dict, snapshot_active: bool = False) -> LoadResult:
    """対象月のメンバー一覧。対象月の末日に最も近いスナップショットを採用する（警告付き）。

    月末までのデータは翌月の最初の営業日に取得することが多く、対象月末時点の構成は翌月初の
    ファイルに入っている。そのため月単位で1つに畳んでから月を選ぶのではなく、ファイル単位で
    末日との距離が最小のものを採る。

    同一月の重複解決は採用したファイルの月にだけ掛ける。全月に掛けると、採用候補と無関係な
    月の曖昧な重複だけで分析全体が止まる。採用した月が曖昧なら従来どおり ValueError で
    止める（取り違え防止）。

    snapshot_active=True は、対象月に単日スナップショットが複数ありメンバー変動の差分分析が
    発動する場合で、重複解決の警告文言を「メンバー変動の検出にも使う」向けにする。
    """
    directory = Path(input_dir) / "members"
    entries = _csv_periods(directory)
    warnings: list[str] = []
    if not entries:
        raise FileNotFoundError(
            f"{input_dir}/members/ にメンバー一覧がありません"
            f"（例: members_{month}.csv。最低限 email,seat_type の2列で可）"
        )
    used, path, delta = _nearest_to_month_end(entries, month)
    used_month = used.month
    if used_month < month:
        warnings.append(
            f"members: {month} のファイルが無いため {path.name} を使用（シート構成が最新でない可能性）"
        )
    elif used_month > month:
        # 対象月末より後のファイル。同じ月の採らなかったファイルだけを未使用として挙げる
        # （対象月内のスナップショットは月中のメンバー変動の検出に使うため未使用ではない）
        others = ", ".join(
            p.name for period, p in entries if p != path and period.month == used_month
        )
        note = "" if is_near_month_end(path, month) else (
            "。対象月当時のシート構成と異なる可能性が高いため、判定は参考値として扱ってください"
        )
        warnings.append(
            f"members: {month} 月末時点のスナップショットが無いため "
            f"{path.name}（月末の {delta} 日後）を使用"
            + (f"（未使用: {others}）" if others else "") + note
        )
    in_used_month = [entry for entry in entries if entry[0].month == used_month]
    if len(in_used_month) > 1:
        resolved, dup_warn = _resolve_duplicates(
            directory, used_month, in_used_month,
            snapshot_note=(
                "メンバー変動の検出" if snapshot_active and used_month == month else None
            ),
        )
        # 対象月末より後の月は上の新しい文言に一本化する（重複解決は「最新を使用」と言うため
        # 採用ファイルと食い違う）。解決結果と採用ファイルが一致する場合だけ転記する
        if used_month <= month and resolved == path:
            warnings.append(dup_warn)
    df, w = _read_members_df(path, cfg)
    warnings.extend(w)
    return LoadResult(df=df, source=path, warnings=warnings)


def _resolve_members_info_path(input_dir: Path, month: str | None) -> tuple[Path | None, list[str]]:
    """採用する members-info ファイルとロード警告を決める。

    日付つきスナップショットが1つでもあれば固定名は無視し、対象月 M の月末以前で最新の
    日付を採用する。月末以前に無ければ最古へフォールバックして強警告を出す。
    日付つきが無ければ従来どおり固定名 members-info.csv を使う。
    """
    input_dir = Path(input_dir)
    snapshots = member_info_files(input_dir)
    fixed = input_dir / "members-info.csv"
    warnings: list[str] = []
    if snapshots and month is not None:
        month_end = _month_end(month)
        on_or_before = [(p, path) for p, path in snapshots if p.start <= month_end]
        if on_or_before:
            path = on_or_before[-1][1]
        else:
            path = snapshots[0][1]
            warnings.append(
                f"members-info: {month} 月末以前のスナップショットが無いため最古の "
                f"{path.name} を使用。対象月当時の設定と異なる可能性があります"
            )
        if fixed.exists():
            warnings.append(
                f"members-info: 日付つきスナップショットがあるため固定名 {fixed.name} は無視し "
                f"{path.name} を使用します"
            )
        return path, warnings
    if fixed.exists():
        return fixed, warnings
    return None, warnings


def load_members_info(input_dir: Path, cfg: dict, month: str | None = None) -> LoadResult | None:
    """部署・チーム・職種・備考・追加クレジット上限のマッピング（任意ファイル）。無ければ None。

    固定名 members-info.csv に加え、日付つき members-info-*-YYYY-MM-DD.csv も受け付ける
    （対象月の月末以前で最新を採用）。月情報なしの手動メンテファイルで email 列のみ必須。
    department/team/role/note が無くても警告は出さず空文字列列で補完し、credit_limit_usd 列は
    parse_credit_limit で float 化する（列が無ければ全 NaN で付与）。

    セルの字句を保つため文字列で読む。数値として読ませると、上限列が数値だけの場合に
    "Infinity" や "1e309" が読み取りの時点で無限大へ変わり、parse_credit_limit が受け取る
    前に「無制限」と区別できなくなる。
    """
    path, warnings = _resolve_members_info_path(Path(input_dir), month)
    if path is None:
        return None
    df = read_csv(path, dtype=str)
    # department/team/role/note/credit_limit_usd が無い場合の「任意カラムなし」警告は捨てる
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
    if "credit_limit_usd" in df.columns:
        parsed = df["credit_limit_usd"].map(parse_credit_limit)
        for email, (_, warn) in zip(df["email"], parsed, strict=False):
            if warn:
                warnings.append(f"{email}: {warn}")
        df["credit_limit_usd"] = parsed.map(lambda pair: pair[0]).astype(float)
    else:
        df["credit_limit_usd"] = float("nan")
    df = df.drop_duplicates(subset="email", keep="last")
    return LoadResult(df=df, source=path, warnings=warnings)


def load_members_info_file(path: Path, cfg: dict) -> pd.DataFrame:
    """指定パスの members-info を1つ読む（月中の κ 変更の差分用・スナップショット解決なし）。

    email と credit_limit_usd（無ければ全 NaN）だけを正規化して返す。
    読み取りは load_members_info と同じく文字列で行う（上限列の字句を保つため）。
    """
    df = read_csv(Path(path), dtype=str)
    df, _ = map_columns(
        df,
        cfg["columns"]["members_info"],
        required=REQUIRED_COLUMNS["members_info"],
        source=Path(path),
    )
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    if "credit_limit_usd" in df.columns:
        df["credit_limit_usd"] = df["credit_limit_usd"].map(
            lambda c: parse_credit_limit(c)[0]
        ).astype(float)
    else:
        df["credit_limit_usd"] = float("nan")
    return df.drop_duplicates(subset="email", keep="last")


def _read_code_df(path: Path, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """1つの code-analytics CSV を読み、カラム正規化・数値化・email 正規化を施す。"""
    df = read_csv(path)
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


def load_code_analytics_file(path: Path, cfg: dict) -> pd.DataFrame:
    """指定パスの code-analytics CSV を1つだけ読む（活動の差分用・重複解決や警告なし）。

    code-analytics は月列を持たないため対象月は受け取らない（差分側でファイル名の期間から
    時点を判別する）。
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
