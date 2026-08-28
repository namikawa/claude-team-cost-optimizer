"""`input/<組織名>/github-members.csv`（email → GitHub login の対応表）を読むモジュール。

GitHub の PR を参考情報として集計するには、スペンドレポートの email と GitHub の login を
突き合わせる表が要る（設計書 §7.3）。この表は管理画面から出力できないため、人手で保守
される小さな CSV を正準形式として受け取る。

読み取りだけを行う。ファイルを書かず、`gh` もネットワークも呼ばず、現在時刻も参照しない
ため、同じ入力からは常に同じ結果と同じ警告の並びを返す。警告にはファイル名だけを載せ、
絶対パスは持たせない（値を実行環境に依存させないため）。

対応表が無くても分析は成立する（設計書 §20.2「GitHub なしでも分析できる」）。ファイルが
無い場合はエラーにせず「未提供」として返し、「ファイルはあるがデータ行が無い」とは区別
できる形にする。GitHub 分析そのものを組織ごとに有効化する判断（config の
`organizations.<組織名>.github_org`）は消費側の責務で、この loader は持たない。

値は「不明」を保つ。login の空欄と、GitHub の login として読めない字句は None（＝未対応）に
して警告に残す（写し間違いを、分析を止めずに気付ける形にする）。一方で取り違えそのものに
直結するもの——必須カラムの欠落、email の欠落・重複、login の重複——は ValueError で中止
する。login の重複は大文字小文字を区別せずに見る（GitHub の login は大小を区別しないため、
`Foo` と `foo` は同じ1人を指す）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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
