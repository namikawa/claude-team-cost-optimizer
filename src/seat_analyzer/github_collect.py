"""GitHub 由来の入力（email → login の対応表）と、`gh` の状態を調べる読み取り専用の probe。

前半は `input/<組織名>/github-members.csv` の loader。GitHub の PR を参考情報として集計
するには、スペンドレポートの email と GitHub の login を突き合わせる表が要る（設計書
§7.3）。この表は管理画面から出力できないため、人手で保守される小さな CSV を正準形式と
して受け取る。loader はファイルを書かず、`gh` もネットワークも呼ばず、現在時刻も参照
しないため、同じ入力からは常に同じ結果と同じ警告の並びを返す。警告にはファイル名だけを
載せ、絶対パスは持たせない（値を実行環境に依存させないため）。

対応表が無くても分析は成立する（設計書 §20.2「GitHub なしでも分析できる」）。ファイルが
無い場合はエラーにせず「未提供」として返し、「ファイルはあるがデータ行が無い」とは区別
できる形にする。GitHub 分析そのものを組織ごとに有効化する判断（config の
`organizations.<組織名>.github_org`）は消費側の責務で、この loader は持たない。

値は「不明」を保つ。login の空欄と、GitHub の login として読めない字句は None（＝未対応）に
して警告に残す（写し間違いを、分析を止めずに気付ける形にする）。一方で取り違えそのものに
直結するもの——必須カラムの欠落、email の欠落・重複、login の重複——は ValueError で中止
する。login の重複は大文字小文字を区別せずに見る（GitHub の login は大小を区別しないため、
`Foo` と `foo` は同じ1人を指す）。

後半は `gh` を呼ぶ probe（設計書 §15.2）。認証・付与された scope・利用上限の残量・
Organization の参照可否だけを調べ、PR も repository も取得しない。token は Python 側へ
取り出さない（`--show-token` を渡さず、環境変数の token も読まない）。gh の stderr は
読み込まず、返すのは終了コードと stdout だけにする——診断文には実行環境に依存する文字列が
混ざるため、issue の message へ写る経路そのものを作らない。probe は結果を値として返す
だけで、警告にするかどうかと文面は消費側（`data_quality`）が決める。
"""

from __future__ import annotations

import enum
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import ingest

# 組織ディレクトリ直下に置く固定名（日付つきのバリアントは受け付けない）と、
# ヘッダのエイリアスを引く config のセクション名
GITHUB_MEMBERS_FILENAME = "github-members.csv"
COLUMNS_SECTION = "github_members"

# GitHub の login として受ける字句。英数字・ハイフン・アンダースコアの 1〜39 文字で、
# 先頭と末尾は英数字、ハイフンは連続しない。アンダースコアは高々1個だけ認める
# （Enterprise Managed Users の login が `<ユーザー名>_<shortcode>` の形をとるため、
# 区切りの1個は受ける。shortcode の形そのものは検証しない）。
#
# 実在しない形（`foo--bar`・`a_b_c`・`_example`）を通すと、対応づいたつもりの login が
# 後段の突き合わせで静かに何にも一致しない。未対応として警告に出す方が写し間違いに
# 気付ける。長さの上限は先読みで見て、本体の式は文字の並びだけを表す。
_LOGIN_SEGMENT = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
_LOGIN_RE = re.compile(
    rf"(?=[A-Za-z0-9_-]{{1,39}}\Z){_LOGIN_SEGMENT}(?:_{_LOGIN_SEGMENT})?"
)

# GitHub の Organization 名として受ける字句。login と同じ並びだが、アンダースコアは
# 使えない（Organization 名は英数字とハイフンだけ）。設定に書かれた値がこの形かどうかを
# ロード時に確かめ、そのままリクエストのパスへ入れられる状態にしておく。
_ORG_NAME_RE = re.compile(rf"(?=[A-Za-z0-9-]{{1,39}}\Z){_LOGIN_SEGMENT}")


def is_github_org_name(value: object) -> bool:
    """value が GitHub の Organization 名として読める字句か。

    設定の検証から使う（`config._validate_organizations`）。読めない値をそのまま通すと、
    GitHub 側に存在しない名前で参照して「参照できません」とだけ報告することになり、
    設定の書き間違いだと分からない。
    """
    return isinstance(value, str) and _ORG_NAME_RE.fullmatch(value) is not None


def _cell_text(cell: object) -> str | None:
    """セルを前後空白を除いた文字列にする。空欄・欠損・空白のみは None。"""
    if cell is None or (not isinstance(cell, str) and pd.isna(cell)):
        return None
    text = str(cell).strip()
    if text == "" or text.lower() == "nan":
        return None
    return text


def _normalize_email(text: str) -> str:
    """email を突き合わせ用に揃える（前後空白の除去 → 小文字化）。

    規則は入力全体で1つ（`ingest` が spend / members の email 列に施すものと同じ）。
    表記だけが違う2つの行が別人として並ばないようにする。
    """
    return text.strip().lower()


def _parse_login(cell: object) -> tuple[str | None, str | None]:
    """github_login セルを (値, 警告) に解釈する。

    空欄と、GitHub の login として読めない字句（`_LOGIN_RE`）は None（＝未対応）にして
    警告を返す。先頭の `@` のような余分な文字は黙って取り除かない（写し間違いに気付ける
    形を優先する。取り除くと、別人の login を正しい値として通す余地が残る）。
    """
    text = _cell_text(cell)
    if text is None:
        return None, "github_login が空欄です（未対応として扱います）"
    if not _LOGIN_RE.fullmatch(text):
        return None, (
            f"github_login を GitHub のログイン名として解釈できません: {text!r}"
            "（英数字で始まり英数字で終わる1〜39文字。区切りに使えるのは連続しない"
            "ハイフンと、高々1個のアンダースコアだけです。未対応として扱います）"
        )
    return text, None


# ------------------------------------------------------------ 値オブジェクト


@dataclass(frozen=True)
class GithubMemberLink:
    """対応表の1行（email → GitHub login）。

    email は前後空白を除いて小文字へ揃えてから保持する（突き合わせの鍵のため）。
    github_login は入力の原文（前後空白のみ除去）で、None は「未対応」を表す。
    値を持つ場合は必ず GitHub の login として読める字句（`_LOGIN_RE`）になっている。
    """

    email: str
    github_login: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.email, str):
            raise TypeError(f"email には文字列が必要です: {type(self.email).__name__}")
        email = _normalize_email(self.email)
        if not email:
            raise ValueError("email は必須です")
        object.__setattr__(self, "email", email)
        if self.github_login is None:
            return
        if not isinstance(self.github_login, str):
            raise TypeError(
                f"github_login には文字列が必要です: {type(self.github_login).__name__}"
            )
        if not _LOGIN_RE.fullmatch(self.github_login):
            raise ValueError(f"github_login として読めない値です: {self.github_login!r}")


@dataclass(frozen=True)
class GithubMembers:
    """`github-members.csv` の読み取り結果。

    entries は入力の行順を保つ（表を直す人が行番号で辿れるようにする）。email は重複
    なしで、値を持つ github_login も大文字小文字を区別せず重複なし。

    source は由来ファイルの basename で、ファイルが無い場合は None。「未提供」と
    「ファイルはあるがデータ行が無い」を呼び出し側が区別できるようにするため、行数
    ではなくこの項目で表す。
    """

    entries: tuple[GithubMemberLink, ...]
    source: str | None
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or not all(
            isinstance(entry, GithubMemberLink) for entry in self.entries
        ):
            raise TypeError("entries には GithubMemberLink の tuple が必要です")
        emails = [entry.email for entry in self.entries]
        if len(set(emails)) != len(emails):
            raise ValueError(f"entries の email が重複しています: {emails}")
        logins = [
            entry.github_login.lower()
            for entry in self.entries
            if entry.github_login is not None
        ]
        if len(set(logins)) != len(logins):
            raise ValueError(f"entries の github_login が重複しています: {logins}")
        if self.source is not None and (
            not isinstance(self.source, str) or not self.source.strip()
        ):
            raise ValueError(f"source にはファイル名が必要です: {self.source!r}")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, str) for warning in self.warnings
        ):
            raise TypeError("warnings には文字列の tuple が必要です")

    @property
    def provided(self) -> bool:
        """対応表のファイルが置かれていたか（データ行の有無とは別）。"""
        return self.source is not None


# ------------------------------------------------------------ ヘッダの突き合わせ


def _candidates(canonical: str, alias_list: object) -> tuple[str, ...]:
    """正準列 canonical に写る、正規化済みヘッダの候補（並びは決定的）。

    照合の規則は `ingest.map_columns` と同じ（エイリアスと正準名そのもの）。
    """
    aliases = alias_list if isinstance(alias_list, list) else []
    return tuple(sorted(
        {ingest.normalize_header(alias) for alias in aliases}
        | {ingest.normalize_header(canonical)}
    ))


def _reject_overlapping_aliases(aliases: dict) -> None:
    """1つのヘッダが2つの正準列の候補になっている設定を拒否する。

    正準名へ写す対応は正準列ごとに決めるため、同じヘッダが2つの正準列の候補に入って
    いると、写る先が定義の並び順で決まり、もう一方の正準列は黙って NA になる。入力の
    中身に依らない設定側の誤りなので、突き合わせより前に止める。

    規則は `admin_inputs` の同名の検査と同じ（層をまたげないため実装は共有しない）。
    """
    owner: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        for candidate in _candidates(canonical, alias_list):
            other = owner.setdefault(candidate, canonical)
            if other != canonical:
                raise ValueError(
                    f"columns.{COLUMNS_SECTION}: ヘッダ {candidate!r} が {other} と "
                    f"{canonical} の両方の候補になっています。どちらの列に写すか"
                    "決まらないため、片方のエイリアスから取り除いてください"
                )


def _reject_ambiguous_headers(path: Path, headers: list[str], aliases: dict) -> None:
    """1つの正準列に対応する実ヘッダが2つ以上ある表を中止する。

    正準名へ写すのは最初に一致したヘッダだけなので、`GitHub Login` と `github_login` の
    ように両方が並ぶ表では、写した後に同名の列が2つ残ってセルの取得が曖昧になる（並びが
    逆なら片方が黙って捨てられる）。どちらが正かは決められないので、列名を挙げて止める。

    見るのは正準列に写るヘッダだけで、読まない列の重複（表計算が行末に付ける空の
    ヘッダなど）は結果に影響しないので止めない。規則は `admin_inputs` の同名の検査と
    同じ（層をまたげないため実装は共有しない）。
    """
    normalized = [(column, ingest.normalize_header(column)) for column in headers]
    for canonical, alias_list in aliases.items():
        candidates = _candidates(canonical, alias_list)
        matched = [column for column, norm in normalized if norm in candidates]
        if len(matched) > 1:
            raise ValueError(
                f"{path}: 同じ列 {canonical} に対応するヘッダが複数あります: {matched}。"
                "どちらが正か判断できないため、1つに絞ってください"
            )


def _raw_header(path: Path) -> list[str]:
    """CSV の先頭行を、列名を畳まずそのまま読む（列の無いファイルは空リスト）。

    読み込みは同名の列を `Email` / `Email.1` へ改名するため、読み込み後の列名では
    「同じヘッダが2つある」ことが分からない。ヘッダの曖昧さは生の先頭行で判断する。
    文字コードの判別は `ingest.read_csv` と同じ2種で行う。

    先頭行はデータ行として読むため、既定では `NA` や `N/A` のようなヘッダが欠損へ
    変わってしまう（列名として書かれた語が消える）。`na_filter=False` で字句のまま受け、
    空のセルは空文字列で表す。
    """
    for encoding in ("utf-8-sig", "cp932"):
        try:
            row = pd.read_csv(
                path, encoding=encoding, header=None, nrows=1, dtype=str, na_filter=False
            )
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError:
            return []
        if row.empty:
            return []
        return [str(value) for value in row.iloc[0]]
    raise ValueError(f"{path}: 文字コードを判別できません（utf-8 / cp932 を試行）")


def _read_table(path: Path, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """対応表を1つ読み、カラム名を正準名へ写す。

    login の大文字小文字と email の表記をそのまま保つため文字列で読み、値の解釈は
    セル単位のパーサに任せる。
    """
    aliases = cfg["columns"][COLUMNS_SECTION]
    _reject_overlapping_aliases(aliases)
    headers = _raw_header(path)
    if not headers:
        raise ValueError(
            f"{path}: ヘッダ行がありません"
            f"（1行目に {','.join(ingest.REQUIRED_COLUMNS[COLUMNS_SECTION])} を"
            "書いてください）"
        )
    _reject_ambiguous_headers(path, headers, aliases)
    df = ingest.read_csv(path, dtype=str)
    # 全データ行がヘッダより1列多い表では、読み込みが先頭の列を暗黙の行ラベルにする。
    # 残りの列が1つずつずれ、login の位置に来たメモも email の位置に来た login も字句
    # としては通るため、ずれた対応がそのまま採用されてしまう。取り違えに直結するので、
    # 正常な読み取りなら（データ行が無くても）必ず付く RangeIndex でなければ中止する。
    # 見ているのは行ラベルの型だが、報告するのは入力の不正なので ValueError にする
    # （種類を変えると CLI のメッセージと終了コードが変わる）。
    if not isinstance(df.index, pd.RangeIndex):
        raise ValueError(  # noqa: TRY004
            f"{path.name}: ヘッダの列数よりデータ行の列数が多いため、列の対応を"
            "決められません（ヘッダとデータ行の列数を揃えてください）"
        )
    return ingest.map_columns(
        df,
        aliases,
        required=ingest.REQUIRED_COLUMNS[COLUMNS_SECTION],
        source=path,
    )


# --------------------------------------------------------------------- 公開 API


def load_github_members(input_dir: Path, cfg: dict) -> GithubMembers:
    """`input_dir/github-members.csv` を読む（ファイルが無ければ未提供の結果）。

    input_dir は組織ディレクトリ（`input/<組織名>/`）。entries は入力の行順で、
    login を持たない行（空欄・読めない字句）も email 付きで残す（対応表に書かれて
    いる人と、そもそも書かれていない人は別の状態のため）。
    """
    path = Path(input_dir) / GITHUB_MEMBERS_FILENAME
    if not path.is_file():
        return GithubMembers(entries=(), source=None, warnings=())

    frame, warnings = _read_table(path, cfg)
    if frame.empty:
        warnings.append(
            f"{path.name}: データ行がありません"
            "（ヘッダだけのファイルが置かれています）"
        )
        return GithubMembers(entries=(), source=path.name, warnings=tuple(warnings))

    entries: list[GithubMemberLink] = []
    seen_email: dict[str, int] = {}
    seen_login: dict[str, tuple[str, int]] = {}
    for number, (_, row) in enumerate(frame.iterrows(), start=1):
        text = _cell_text(row.get("email"))
        if text is None:
            raise ValueError(
                f"{path}: {number} 行目の email が空です（1メール1行で記入してください）"
            )
        email = _normalize_email(text)
        if email in seen_email:
            raise ValueError(
                f"{path}: email {email!r} の行が複数あります"
                f"（{seen_email[email]} 行目と {number} 行目）。1メール1行に整理して"
                "ください"
            )
        seen_email[email] = number

        login, warning = _parse_login(row.get("github_login"))
        if warning is not None:
            warnings.append(f"{path.name}: {email}: {warning}")
        if login is not None:
            key = login.lower()
            if key in seen_login:
                other_login, other_number = seen_login[key]
                raise ValueError(
                    f"{path}: github_login {login!r} の行が複数あります"
                    f"（{other_number} 行目の {other_login!r} と {number} 行目）。"
                    "GitHub のログイン名は大文字小文字を区別しないため、"
                    "1アカウント1行に整理してください"
                )
            seen_login[key] = (login, number)
        entries.append(GithubMemberLink(email=email, github_login=login))

    return GithubMembers(
        entries=tuple(entries), source=path.name, warnings=tuple(warnings)
    )


def unmapped_emails(members: GithubMembers, emails: Iterable[str]) -> tuple[str, ...]:
    """emails のうち GitHub login に対応づかないものを返す（正規化済み・昇順・重複なし）。

    login を持たない行（空欄・読めない字句）は、対応表に書かれていない人と同じく
    「未対応」として扱う。空のメールは対象にしない。

    警告にするかどうかは呼び出し側が決める（GitHub 分析の対象でない組織では、未対応が
    いること自体が正常なため）。
    """
    mapped = {
        entry.email for entry in members.entries if entry.github_login is not None
    }
    unmapped = set()
    for value in emails:
        text = _cell_text(value)
        if text is None:
            continue
        email = _normalize_email(text)
        if email not in mapped:
            unmapped.add(email)
    return tuple(sorted(unmapped))


def gated_orgs(cfg: dict) -> dict[str, str]:
    """GitHub 分析を有効にした組織 → その GitHub Organization 名（設定の記述順）。

    キーは入力ディレクトリ直下の組織名、値は GitHub の Organization 名で、両者は一致
    しない前提の対応表として扱う。ここに書かれていない組織は GitHub 関連の処理と警告の
    一切から除外される（設計書 §15.1）。値の字句は設定のロード時に検証済み。
    """
    organizations = cfg["organizations"]
    return {
        str(org): entry["github_org"]
        for org, entry in organizations.items()
        if isinstance(entry, dict) and isinstance(entry.get("github_org"), str)
    }


# ------------------------------------------------------------------ gh の実行

# 実行する GitHub CLI と、1回あたりの応答待ち上限。probe は直列に呼ぶので、待ち時間は
# そのまま doctor の所要時間になる
GH_COMMAND = "gh"
GH_TIMEOUT_SECONDS = 30.0


@enum.unique
class GhFailure(enum.StrEnum):
    """gh を使えなかった理由の分類。gh の生出力は持たず、この語だけを外へ出す。"""

    UNAUTHENTICATED = "unauthenticated"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class GhResult:
    """gh を1回実行した結果。

    failure が None なら「プロセスは動いた」（終了コードは ok が表す）。gh は 2xx 以外の
    応答でも終了コード 1 で終わるため、両者を分けて持つ。stderr は保持しない。
    """

    ok: bool
    stdout: str = ""
    failure: GhFailure | None = None


# gh を1回呼ぶ関数の型。テストから記録済みの応答へ差し替えられるようにする
Runner = Callable[[Sequence[str]], GhResult]


def _decode(raw: bytes | None) -> str:
    """gh の出力を UTF-8 として読み、改行を LF へ揃える。

    改行を揃えるのは、応答ヘッダを行単位で解釈するため（Windows の gh は CRLF を出す）。
    errors は既定の strict にする。置換して読み進めると、壊れた出力を正しい応答として
    解釈しかねない。
    """
    if not raw:
        return ""
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def run_gh(args: Sequence[str]) -> GhResult:
    """gh を1回実行する（読み取りだけを行う参照に限って使う）。

    token を Python 側へ取り出さない。`--show-token` を渡さず、環境変数の token も読まず、
    出力から token を取り出しもしない（認証情報の管理は gh に委ねる）。
    """
    # which() が返した実体を起動する（Windows の CreateProcess は拡張子を補うとき .exe
    # しか試さないため、名前だけでは起動できない配布形式がある）
    resolved = shutil.which(GH_COMMAND)
    if resolved is None:
        return GhResult(ok=False, failure=GhFailure.NOT_FOUND)
    try:
        # stderr は DEVNULL で読み込みすらしない（診断文が Python 側へ入る経路を
        # fd の段階で断つ。使うのは stdout と終了コードだけ）
        proc = subprocess.run(
            [resolved, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=GH_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return GhResult(ok=False, failure=GhFailure.TIMEOUT)
    except FileNotFoundError:
        # which() の後に実体が消えた場合。導入されていない状態と同じ案内でよい
        return GhResult(ok=False, failure=GhFailure.NOT_FOUND)
    except OSError:
        return GhResult(ok=False, failure=GhFailure.ERROR)
    try:
        stdout = _decode(proc.stdout)
    except UnicodeDecodeError:
        return GhResult(ok=False, failure=GhFailure.ERROR)
    return GhResult(ok=proc.returncode == 0, stdout=stdout)


def _parse_response(stdout: str) -> tuple[int | None, dict[str, str]]:
    """`gh api -i` の出力を (HTTP ステータス, ヘッダ) に分ける。

    ヘッダ名は小文字へ揃える（HTTP のヘッダ名は大文字小文字を区別せず、表記は gh の版に
    依存する）。同じ名前が複数回現れたら HTTP の規則どおり "," で連結する。本文は使わない
    ので返さない。ステータス行を読めない出力では None を返し、呼び出し側が「応答として
    解釈できなかった」として扱う。
    """
    lines = stdout.split("\n")
    if not lines[0].startswith("HTTP/"):
        return None, {}
    parts = lines[0].split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            break   # ヘッダ部の終わり（以降は本文）
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip().lower()
        text = value.strip()
        headers[key] = f"{headers[key]}, {text}" if key in headers else text
    return status, headers


# ------------------------------------------------------------------ probe

# PR の収集に必要な scope と、それを満たす scope。GitHub の scope は上位が下位を含むが、
# 付与済みの一覧には上位だけが載るため、含意を書き下して照合する（含意を見ないと、
# 上位 scope だけを持つ token を「権限不足」と誤検出して収集を止めてしまう）
REQUIRED_SCOPES = ("read:org", "repo")
_SCOPE_ALTERNATIVES = {
    "read:org": ("read:org", "write:org", "admin:org"),
    "repo": ("repo",),
}

# 残量を見る rate limit の区分（PR の検索が使うのはこの2つ）
_RATE_RESOURCES = ("core", "search")


@dataclass(frozen=True)
class GhAuth:
    """`gh auth status` の結果。"""

    ok: bool
    failure: GhFailure | None = None


@dataclass(frozen=True)
class GhRateResource:
    """rate limit の1区分の残量。reset 時刻は持たない（実行のたびに変わるため）。"""

    name: str
    remaining: int
    limit: int


@dataclass(frozen=True)
class GhOrgAccess:
    """1つの Organization を参照できるか。

    status は HTTP のステータス（応答として解釈できなければ None）、sso_required は
    SAML SSO の承認が要る 403 かどうか。
    """

    github_org: str
    status: int | None = None
    sso_required: bool = False
    failure: GhFailure | None = None

    @property
    def accessible(self) -> bool:
        return self.status == 200


@dataclass(frozen=True)
class GithubProbes:
    """doctor が使う gh の状態一式。

    scopes は付与された scope（`None` は「判定できない token」）、rate は読めた区分だけ、
    orgs は問い合わせた Organization の分だけを持つ。認証に失敗した場合は auth 以外が
    空になる（同じ原因から派生する失敗を並べないため、後続を実行しない）。
    """

    auth: GhAuth
    scopes: tuple[str, ...] | None = None
    rate: tuple[GhRateResource, ...] = ()
    orgs: tuple[GhOrgAccess, ...] = ()

    def org(self, github_org: str) -> GhOrgAccess | None:
        """github_org の参照可否（問い合わせていなければ None）。"""
        for access in self.orgs:
            if access.github_org == github_org:
                return access
        return None


def probe_github(
    github_orgs: Iterable[str], runner: Runner | None = None
) -> GithubProbes:
    """gh の認証・scope・rate 残量と、各 Organization の参照可否を調べる（設計書 §15.2）。

    読み取りしか行わない（PR も repository も取得しない）。認証・scope・rate は token 単位
    なので実行1回につき1度だけ問い合わせ、Organization ごとの問い合わせは重複を除いた
    名前の分だけ行う。認証に失敗したら後続を実行せず、根本原因1件だけを返す。

    github_orgs は設定のロード時に字句を検証済みの Organization 名（`is_github_org_name`）。
    runner を渡すと gh の呼び出しを差し替えられる（テスト用）。
    """
    run = run_gh if runner is None else runner
    auth = _probe_auth(run)
    if not auth.ok:
        return GithubProbes(auth=auth)
    return GithubProbes(
        auth=auth,
        scopes=_probe_scopes(run),
        rate=_probe_rate(run),
        orgs=tuple(_probe_org(run, name) for name in dict.fromkeys(github_orgs)),
    )


def _probe_auth(run: Runner) -> GhAuth:
    """認証の有無を `gh auth status` の終了コードだけで見る。

    出力は解釈しない。表示は gh の版で変わるうえ、伏字とはいえ token の断片を含む。
    """
    result = run(("auth", "status"))
    if result.ok:
        return GhAuth(ok=True)
    return GhAuth(ok=False, failure=result.failure or GhFailure.UNAUTHENTICATED)


def _probe_scopes(run: Runner) -> tuple[str, ...] | None:
    """token に付与された OAuth scope（判定できなければ None）。

    出所は応答ヘッダ `X-OAuth-Scopes` の1つだけにする。fine-grained PAT と GitHub App の
    token はこのヘッダを持たないため、そこでは scope の充足を判定できない。判定できない
    ことと「scope が1つも無い」ことは別なので、前者は None を返して報告させない
    （誤検出より不判定に倒す。実際に参照できるかは Organization の検査が確かめる）。
    """
    result = run(("api", "-i", "user"))
    if result.failure is not None:
        return None
    status, headers = _parse_response(result.stdout)
    if status != 200 or "x-oauth-scopes" not in headers:
        return None
    return tuple(sorted({
        scope for raw in headers["x-oauth-scopes"].split(",") if (scope := raw.strip())
    }))


def missing_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """必要な scope のうち付与されていないもの（REQUIRED_SCOPES の並び順）。"""
    granted = set(scopes)
    return tuple(
        required for required in REQUIRED_SCOPES
        if granted.isdisjoint(_SCOPE_ALTERNATIVES[required])
    )


def _is_count(value: object) -> bool:
    """件数として読める値か（真偽値は int の一種なので除く）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _probe_rate(run: Runner) -> tuple[GhRateResource, ...]:
    """rate limit の残量（読めない区分は落とす）。

    読めなかった場合は空を返す。この問い合わせだけが失敗する状況は通信の不調なので、
    同じ原因が Organization の参照でエラーとして出る（ここで二重に報告しない）。
    """
    result = run(("api", "rate_limit"))
    if not result.ok:
        return ()
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return ()
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return ()
    found = []
    for name in _RATE_RESOURCES:
        entry = resources.get(name)
        if not isinstance(entry, dict):
            continue
        remaining, limit = entry.get("remaining"), entry.get("limit")
        if _is_count(remaining) and _is_count(limit):
            found.append(GhRateResource(name=name, remaining=remaining, limit=limit))
    return tuple(found)


def _probe_org(run: Runner, github_org: str) -> GhOrgAccess:
    """Organization を参照できるかを HTTP のステータスで見る。"""
    result = run(("api", "-i", f"orgs/{github_org}"))
    if result.failure is not None:
        return GhOrgAccess(github_org=github_org, failure=result.failure)
    status, headers = _parse_response(result.stdout)
    if status is None:
        return GhOrgAccess(github_org=github_org, failure=GhFailure.ERROR)
    # SSO の未承認は 403 に `X-GitHub-SSO: required; url=...` が付く。同じヘッダは
    # 一部の成功応答にも `partial-results` として付くため、値の先頭で見分ける
    sso = headers.get("x-github-sso", "").strip().lower().startswith("required")
    return GhOrgAccess(
        github_org=github_org, status=status, sso_required=status == 403 and sso
    )
