"""GitHub 由来の入力（email → login の対応表）と、`gh` で読み取るだけの probe・repository の発見。

1つ目は `input/<組織名>/members-info.csv` の `GitHub ID` 列を email → GitHub login の
対応表として読む loader。GitHub の PR を参考情報として集計するには、スペンドレポートの
email と GitHub の login を突き合わせる表が要る（設計書 §7.3）。この対応は GitHub の API
から取れないため人手で保守され、メンバーごとの属性を1つの表にまとめておく。採用する
ファイルは members-info の読み取りと同じ規則で選ぶ（`ingest.members_info_path`）ので、
部署や追加クレジット上限と同じ行から GitHub の login を読む。loader はファイルを書かず、
`gh` もネットワークも呼ばず、現在時刻も参照しないため、同じ入力からは常に同じ結果と同じ
警告の並びを返す。警告にはファイル名だけを載せ、絶対パスは持たせない（値を実行環境に
依存させないため）。

対応表が無くても分析は成立する（設計書 §20.2「GitHub なしでも分析できる」）。ファイルが
無い場合はエラーにせず「未提供」として返し、「ファイルはあるが `GitHub ID` の列が無い」
「列はあるがデータ行が無い」とは区別できる形にする。GitHub 分析そのものを組織ごとに
有効化する判断（config の `organizations.<組織名>.github_org`）は消費側の責務で、この
loader は持たない。

値は「不明」を保つ。login の空欄と、GitHub の login として読めない字句は None（＝未対応）に
して警告に残す（写し間違いを、分析を止めずに気付ける形にする）。`なし`・`none`・`-` は
「GitHub のアカウントを持たない」という記入で、同じく未対応だが警告しない（未記入と、
書く値が無いことを区別する）。一方で取り違えそのものに直結するもの——必須カラムの欠落、
email の欠落・重複、login の重複——は ValueError で中止する。login の重複は大文字小文字を
区別せずに見る（GitHub の login は大小を区別しないため、`Foo` と `foo` は同じ1人を指す）。

2つ目は `gh` を呼ぶ probe（設計書 §15.2）。認証・付与された scope・利用上限の残量・
Organization の参照可否だけを調べ、PR も repository も取得しない。token は Python 側へ
取り出さない（`--show-token` を渡さず、環境変数の token も読まない）。gh の stderr は
読み込まず、返すのは終了コードと stdout だけにする——診断文には実行環境に依存する文字列が
混ざるため、issue の message へ写る経路そのものを作らない。probe は結果を値として返す
だけで、警告にするかどうかと文面は消費側（`data_quality`）が決める。

3つ目は Organization 内の repository の発見（設計書 §15.3）。取るのは repository の
メタデータ（名前と、archived / fork / template の印）だけで、PR も title もコードも
取得しない。対象は参照できる範囲そのままなので、手で書き並べた allowlist を要さない。
返すのは全ページを読み切れた場合の完全な一覧に限り、途中で失敗した場合は名前を返さず
理由だけを返す（部分的な一覧を完全な一覧として集計させないため）。probe と同じく、
結果は値として返すだけで gh の生出力も token も保持しない。

4つ目は merged PR のメタデータの収集とキャッシュ（設計書 §15.4・§7.6）。保存するのは
9項目（repository・PR number・author の login と type・createdAt・mergedAt・additions・
deletions・isDraft）だけで、title・本文・レビュー本文・files・diff・commit message・
コードは取得も保存もしない（設計書 §20.3 の禁止フィールド）。応答からは明示した項目だけを
取り出すので、問い合わせに無い項目が返ってきても保存物には入らない。一意キーは
`repository + "#" + number` で、キャッシュはこのキーの dict として持つ（一意性を構造で
保証する）。

保存先は `input/<組織名>/github-cache/prs-YYYY-MM.json` の1組織×1月で、PR は mergedAt の
UTC 月に帰属させる。収集した時点の repository の一覧も同じファイルへ保存するので、集計の
側は `gh` もネットワークも呼ばずにキャッシュだけで結果を出せる。月は固定の窓（1–7 /
8–14 / 15–21 / 22–28 / 29–月末）に割り、窓ごとに取り切れたかどうかを記録する。
読み切れた窓のうち終端の翌日が過ぎたものだけを「完了」に
するので、今日・昨日を含む窓は毎回取り直される（検索インデックスの反映遅れで、日付境界
直前の merge を取りこぼさないための1日の猶予）。窓の取得に失敗したらそこで止め、それまでの
窓の結果と完了済みの窓を保存して理由を返す——部分的な結果を完全なものとして集計させない
ためで、再実行は完了済みの窓を問い合わせずに続きから進む。
"""

from __future__ import annotations

import calendar
import datetime as dt
import enum
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from . import ingest

# 対応表として読むファイル（members-info）のヘッダのエイリアスを引く config のセクション名と、
# その中で対応表に関係する正準列。members-info には部署・備考など対応表と無関係の列も
# 並ぶため、ヘッダの検査はこの2列に絞る（無関係な列の別名・重複で対応表の読み取りを
# 止めない）
COLUMNS_SECTION = "members_info"
_MAPPING_COLUMNS = ("email", "github_login")

# 対応表を別ファイルで受けていたときの固定名。置かれたままだと、そこに書いた対応が
# 使われていないことに気付けないので、検出したら中止して移し替えを案内する
LEGACY_GITHUB_MEMBERS_FILENAME = "github-members.csv"

# GitHub のアカウントを持たないことを表す記入（前後空白を除いて比較し、`none` は
# 大文字小文字を区別しない）。未記入と区別して、警告の対象から外す
_NO_ACCOUNT_MARKERS = ("なし", "none", "-")

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

# GitHub の repository 名として受ける字句。英数字・ハイフン・アンダースコア・ピリオドの
# 1〜100 文字で、`.` と `..` そのものは受けない。発見した名前は後続の問い合わせで
# リクエストのパスへ入るため、パスとして意味を持つ字句を値の段階で締め出す。
_REPO_NAME_RE = re.compile(r"(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}")


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


def _parse_login(cell: object) -> tuple[str | None, bool, str | None]:
    """GitHub ID のセルを (値, アカウントを持たないか, 警告) に解釈する。

    空欄と、GitHub の login として読めない字句（`_LOGIN_RE`）は None（＝未対応）にして
    警告を返す。先頭の `@` のような余分な文字は黙って取り除かない（写し間違いに気付ける
    形を優先する。取り除くと、別人の login を正しい値として通す余地が残る）。

    `_NO_ACCOUNT_MARKERS` は「アカウントを持たない」と書かれた行で、値は未対応のまま
    警告しない。未記入と同じ扱いにすると、書きようのない人の分だけ毎月同じ警告が残り、
    本当に記入漏れの行が埋もれる。
    """
    text = _cell_text(cell)
    if text is None:
        return None, False, (
            "GitHub ID が空欄です（未対応として扱います。アカウントを持たない人は"
            "「なし」と書いてください）"
        )
    if text.lower() in _NO_ACCOUNT_MARKERS:
        return None, True, None
    if not _LOGIN_RE.fullmatch(text):
        return None, False, (
            f"GitHub ID を GitHub のログイン名として解釈できません: {text!r}"
            "（英数字で始まり英数字で終わる1〜39文字。区切りに使えるのは連続しない"
            "ハイフンと、高々1個のアンダースコアだけです。未対応として扱います）"
        )
    return text, False, None


# ------------------------------------------------------------ 値オブジェクト


@dataclass(frozen=True)
class GithubMemberLink:
    """対応表の1行（email → GitHub login）。

    email は前後空白を除いて小文字へ揃えてから保持する（突き合わせの鍵のため）。
    github_login は入力の原文（前後空白のみ除去）で、None は「未対応」を表す。
    値を持つ場合は必ず GitHub の login として読める字句（`_LOGIN_RE`）になっている。

    no_account は「GitHub のアカウントを持たない」と記入された行。未対応であることは
    同じだが、記入漏れではないので消費側が警告から外せるようにする。login を持つ行は
    アカウントがある行なので、両立する状態は作らない。
    """

    email: str
    github_login: str | None
    no_account: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.email, str):
            raise TypeError(f"email には文字列が必要です: {type(self.email).__name__}")
        email = _normalize_email(self.email)
        if not email:
            raise ValueError("email は必須です")
        object.__setattr__(self, "email", email)
        if not isinstance(self.no_account, bool):
            raise TypeError(
                f"no_account には真偽値が必要です: {type(self.no_account).__name__}"
            )
        if self.github_login is None:
            return
        if self.no_account:
            raise ValueError(
                "アカウントを持たない行に github_login は持たせられません: "
                f"{self.github_login!r}"
            )
        if not isinstance(self.github_login, str):
            raise TypeError(
                f"github_login には文字列が必要です: {type(self.github_login).__name__}"
            )
        if not _LOGIN_RE.fullmatch(self.github_login):
            raise ValueError(f"github_login として読めない値です: {self.github_login!r}")


@dataclass(frozen=True)
class GithubMembers:
    """members-info の `GitHub ID` 列を対応表として読んだ結果。

    entries は入力の行順を保つ（表を直す人が行番号で辿れるようにする）。email は重複
    なしで、値を持つ github_login も大文字小文字を区別せず重複なし。

    source は由来ファイルの basename で、ファイルが無い場合は None。has_column は
    その表に `GitHub ID` の列があったか。「未提供」「列が無い」「列はあるがデータ行が
    無い」を呼び出し側が区別できるようにするため、行数ではなくこの2項目で表す
    （ファイルが無ければ列も無いので、source が None のとき has_column は False）。
    """

    entries: tuple[GithubMemberLink, ...]
    source: str | None
    has_column: bool
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
        if not isinstance(self.has_column, bool):
            raise TypeError(
                f"has_column には真偽値が必要です: {type(self.has_column).__name__}"
            )
        if self.source is None and self.has_column:
            raise ValueError("ファイルが無い結果に GitHub ID の列は持たせられません")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, str) for warning in self.warnings
        ):
            raise TypeError("warnings には文字列の tuple が必要です")

    @property
    def provided(self) -> bool:
        """対応表として読める状態か（ファイルがあり `GitHub ID` の列がある）。

        データ行の有無とは別。列が無い表は、記入が1行も無い表とは違って「対応表として
        使う準備ができていない」状態なので、ここでは未提供と同じに扱う。
        """
        return self.source is not None and self.has_column


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


def _mapping_aliases(cfg: dict) -> dict:
    """対応表として読む列だけのエイリアス表（email と GitHub ID）。

    members-info には部署・チーム・備考など、対応表と関係のない列も並ぶ。それらの別名や
    重複までここで見ると、GitHub と無関係な列の書き方で対応表の読み取りが止まる
    （読まない列は結果に影響しない）。
    """
    aliases = cfg["columns"][COLUMNS_SECTION]
    return {
        canonical: aliases[canonical]
        for canonical in _MAPPING_COLUMNS
        if canonical in aliases
    }


def _read_table(path: Path, cfg: dict) -> tuple[pd.DataFrame, list[str]]:
    """対応表を1つ読み、カラム名を正準名へ写す。

    login の大文字小文字と email の表記をそのまま保つため文字列で読み、値の解釈は
    セル単位のパーサに任せる。
    """
    aliases = _mapping_aliases(cfg)
    _reject_overlapping_aliases(aliases)
    headers = _raw_header(path)
    if not headers:
        raise ValueError(
            f"{path}: ヘッダ行がありません"
            f"（1行目に {','.join(ingest.REQUIRED_COLUMNS[COLUMNS_SECTION])} を"
            "書いてください）"
        )
    _reject_ambiguous_headers(path, headers, aliases)
    # 書かれた字句のまま受ける（既定では "None" が読み取りの時点で欠損へ変わり、
    # 「アカウントを持たない」という記入と空欄を区別できなくなる）
    df = ingest.read_csv(path, dtype=str, keep_default_na=False)
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


def load_github_members(
    input_dir: Path, cfg: dict, month: str | None = None
) -> GithubMembers:
    """members-info の `GitHub ID` 列を対応表として読む（ファイルが無ければ未提供の結果）。

    input_dir は組織ディレクトリ（`input/<組織名>/`）。読むファイルは `load_members_info`
    と同じ規則で選ぶ（日付つきがあれば対象月の月末以前で最新）。ファイルの選択で出る警告は
    ここでは返さない——members-info の読み取り側が同じ警告を出すため、二重に並べない。

    entries は入力の行順で、login を持たない行（空欄・読めない字句・アカウントを持たない
    記入）も email 付きで残す（表に書かれている人と、そもそも書かれていない人は別の状態
    のため）。

    email の重複はここでは中止する。`load_members_info` は同じファイルを最後の行で畳んで
    読むが、対応表としては「どちらの login が正か」が決まらず、取り違えたまま PR を別人へ
    帰属させることになる。属性の上書きより厳しく見る。
    """
    input_dir = Path(input_dir)
    legacy = input_dir / LEGACY_GITHUB_MEMBERS_FILENAME
    if legacy.is_file():
        raise ValueError(
            f"{LEGACY_GITHUB_MEMBERS_FILENAME} は読まなくなりました。GitHub ID は "
            f"{ingest.MEMBERS_INFO_FILENAME} の GitHub ID 列に移し、このファイルを"
            "削除してください"
        )
    path, _ = ingest.members_info_path(input_dir, month)
    if path is None:
        return GithubMembers(
            entries=(), source=None, has_column=False, warnings=()
        )

    frame, warnings = _read_table(path, cfg)
    if "github_login" not in frame.columns:
        return GithubMembers(
            entries=(), source=path.name, has_column=False, warnings=()
        )
    if frame.empty:
        warnings.append(
            f"{path.name}: データ行がありません"
            "（ヘッダだけのファイルが置かれています）"
        )
        return GithubMembers(
            entries=(), source=path.name, has_column=True, warnings=tuple(warnings)
        )

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

        login, no_account, warning = _parse_login(row.get("github_login"))
        if warning is not None:
            warnings.append(f"{path.name}: {email}: {warning}")
        if login is not None:
            key = login.lower()
            if key in seen_login:
                other_login, other_number = seen_login[key]
                raise ValueError(
                    f"{path}: GitHub ID {login!r} の行が複数あります"
                    f"（{other_number} 行目の {other_login!r} と {number} 行目）。"
                    "GitHub のログイン名は大文字小文字を区別しないため、"
                    "1アカウント1行に整理してください"
                )
            seen_login[key] = (login, number)
        entries.append(GithubMemberLink(
            email=email, github_login=login, no_account=no_account
        ))

    return GithubMembers(
        entries=tuple(entries), source=path.name, has_column=True,
        warnings=tuple(warnings),
    )


def unmapped_emails(members: GithubMembers, emails: Iterable[str]) -> tuple[str, ...]:
    """emails のうち GitHub login に対応づかないものを返す（正規化済み・昇順・重複なし）。

    login を持たない行（空欄・読めない字句）は、対応表に書かれていない人と同じく
    「未対応」として扱う。アカウントを持たないと記入された行は、対応づけようがない人
    なので返さない（記入で解消できる状態だけを挙げる）。空のメールは対象にしない。

    警告にするかどうかは呼び出し側が決める（GitHub 分析の対象でない組織では、未対応が
    いること自体が正常なため）。
    """
    mapped = {
        entry.email for entry in members.entries
        if entry.github_login is not None or entry.no_account
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

# gh が「認証が必要」を表す終了コード（`gh help exit-codes`）。他の失敗と同じ 1 では
# ないので、認証の案内を出すべき状態をこの値で見分けられる
GH_EXIT_AUTH_REQUIRED = 4


@enum.unique
class GhFailure(enum.StrEnum):
    """gh の呼び出しを続けられなかった理由の分類。

    gh の生出力は持たず、この語だけを外へ出す。プロセスを起動できなかった理由
    （認証・導入・応答待ち）と、応答は返ったが続けられない理由（利用上限・検索の
    件数上限・解釈できない応答）を1つの語彙で表す。どちらも呼び出し側の次の一手は
    「原因を除いてから再実行する」で同じなので、区別は表示の文言だけに使う。
    """

    UNAUTHENTICATED = "unauthenticated"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    TOO_MANY_RESULTS = "too_many_results"
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

# 再試行の前に待つ関数の型。テストから記録用の関数へ差し替えられるようにする
# （既定は `time.sleep`。待ちを直接呼ぶとテストが実時間だけ止まる）
Sleeper = Callable[[float], None]


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

    このモジュールは GitHub を読むだけで、書き込む API は呼ばない。引数を組み立てるのは
    `_AUTH_STATUS_ARGS`・`_read_args`・`_graphql_args` の3つだけで、いずれも GET と
    読み取り専用の GraphQL query しか作らない。

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
    if proc.returncode == GH_EXIT_AUTH_REQUIRED:
        # 認証が要ることを gh が終了コードで伝える場合。ここで分類しないと、応答の無い
        # 実行として「解釈できない応答」に落ち、gh auth login の案内へたどり着けない
        return GhResult(ok=False, failure=GhFailure.UNAUTHENTICATED)
    try:
        stdout = _decode(proc.stdout)
    except UnicodeDecodeError:
        return GhResult(ok=False, failure=GhFailure.ERROR)
    return GhResult(ok=proc.returncode == 0, stdout=stdout)


# 認証の有無を見る唯一の呼び出し。`auth` の他のサブコマンド（login・refresh・logout）は
# 認証情報を書き換えるため、このモジュールからは呼ばない
_AUTH_STATUS_ARGS: tuple[str, ...] = ("auth", "status")


def _read_args(path: str, *, headers: bool = True) -> tuple[str, ...]:
    """REST を読むだけの引数（GET）。

    `gh api` はフィールド（`-f` / `-F` / `--input` / `--raw-field` / `--field`）を1つでも
    渡すと既定のメソッドが POST に変わる。読むだけの参照でフィールドを渡す用は無いので、
    ここでは path だけを組み立てる。`-X` も渡さないので、送るのは常に GET になる。

    headers=False は応答ヘッダを付けない形（本文だけを JSON として読む問い合わせ用）。
    """
    return ("api", "-i", path) if headers else ("api", path)


def _graphql_args(search: str, after: str | None) -> tuple[str, ...]:
    """GraphQL を読むだけの引数（`_PR_SEARCH_QUERY` だけを渡す）。

    GraphQL の入口は POST だが、送るのは読み取り専用の query 文書だけで mutation は
    持たない。渡す変数は検索文字列と件数と cursor のみ。`$first` は Int なので `-F`
    （型付き）、文字列は `-f` を使う。

    1ページ目は `after` を渡さない（変数を省くと GraphQL 側で null になる。空文字を
    渡すと cursor として解釈されて 0 件になる）。
    """
    args = [
        "api", "-i", "graphql",
        "-f", f"query={_PR_SEARCH_QUERY}",
        "-f", f"search={search}",
        "-F", f"first={_SEARCH_PER_PAGE}",
    ]
    if after is not None:
        args += ["-f", f"after={after}"]
    return tuple(args)


def _parse_response(stdout: str) -> tuple[int | None, dict[str, str], str]:
    """`gh api -i` の出力を (HTTP ステータス, ヘッダ, 本文) に分ける。

    ヘッダ名は小文字へ揃える（HTTP のヘッダ名は大文字小文字を区別せず、表記は gh の版に
    依存する）。同じ名前が複数回現れたら HTTP の規則どおり "," で連結する。本文は最初の
    空行より後の全行で、字句には手を入れない（JSON として解釈するかどうかは呼び出し側が
    決める）。ステータス行を読めない出力では None と空の本文を返し、呼び出し側が「応答
    として解釈できなかった」として扱う。
    """
    lines = stdout.split("\n")
    if not lines[0].startswith("HTTP/"):
        return None, {}, ""
    parts = lines[0].split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers: dict[str, str] = {}
    body: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if not line.strip():
            body = lines[index + 1:]   # ヘッダ部の終わり（以降は本文）
            break
        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip().lower()
        text = value.strip()
        headers[key] = f"{headers[key]}, {text}" if key in headers else text
    return status, headers, "\n".join(body)


# ------------------------------------------------------------------ probe

# PR の収集に必要な scope と、それを満たす scope。GitHub の scope は上位が下位を含むが、
# 付与済みの一覧には上位だけが載るため、含意を書き下して照合する（含意を見ないと、
# 上位 scope だけを持つ token を「権限不足」と誤検出して収集を止めてしまう）
REQUIRED_SCOPES = ("read:org", "repo")
_SCOPE_ALTERNATIVES = {
    "read:org": ("read:org", "write:org", "admin:org"),
    "repo": ("repo",),
}

# 残量を見る rate limit の区分。repository の列挙が core、PR の検索が graphql を使う
# （検索は GraphQL の search で行うため、REST の search 区分は消費しない）
_RATE_RESOURCES = ("core", "graphql")


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
    result = run(_AUTH_STATUS_ARGS)
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
    result = run(_read_args("user"))
    if result.failure is not None:
        return None
    status, headers, _ = _parse_response(result.stdout)
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
    result = run(_read_args("rate_limit", headers=False))
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
    result = run(_read_args(f"orgs/{github_org}"))
    if result.failure is not None:
        return GhOrgAccess(github_org=github_org, failure=result.failure)
    status, headers, _ = _parse_response(result.stdout)
    if status is None:
        return GhOrgAccess(github_org=github_org, failure=GhFailure.ERROR)
    # SSO の未承認は 403 に `X-GitHub-SSO: required; url=...` が付く。同じヘッダは
    # 一部の成功応答にも `partial-results` として付くため、値の先頭で見分ける
    sso = headers.get("x-github-sso", "").strip().lower().startswith("required")
    return GhOrgAccess(
        github_org=github_org, status=status, sso_required=status == 403 and sso
    )


# ------------------------------------------------------ repository の発見

# 1ページあたりの件数（API の上限）と、読むページ数の上限。上限は 10 万件相当で、
# 超えた場合は一覧を切り詰めずエラーにする
_REPOS_PER_PAGE = 100
_MAX_REPO_PAGES = 1000

# 参考指標の対象から外す repository の印。archived は更新が止まった記録、fork は
# 他所のコードの複製、template は雛形で、いずれも PR の実績として読む対象ではない
_EXCLUDED_FLAGS = ("archived", "fork", "is_template")


@dataclass(frozen=True)
class RepoDiscovery:
    """1つの Organization で発見した repository。

    repos は除外を通過した名前で、表記は API が返したまま。小文字比較で重複がなく、
    小文字比較の昇順に並ぶ（同じ応答からは常に同じ並びになる）。excluded は
    archived / fork / template で除外した件数。

    status は最後に読んだ応答の HTTP ステータス、failure は gh を使えなかった理由。
    完全な一覧を得られなかった場合は repos を空にする（`complete`）。
    """

    github_org: str
    repos: tuple[str, ...] = ()
    excluded: int = 0
    status: int | None = None
    failure: GhFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.github_org, str) or not self.github_org.strip():
            raise ValueError(
                f"github_org には Organization 名が必要です: {self.github_org!r}"
            )
        if not isinstance(self.repos, tuple):
            raise TypeError(f"repos には tuple が必要です: {type(self.repos).__name__}")
        for name in self.repos:
            if not isinstance(name, str) or not _REPO_NAME_RE.fullmatch(name):
                raise ValueError(f"repository 名として読めない値です: {name!r}")
        keys = [name.lower() for name in self.repos]
        if len(set(keys)) != len(keys):
            raise ValueError(f"repos の repository 名が重複しています: {keys}")
        if keys != sorted(keys):
            raise ValueError(f"repos は小文字比較の昇順で並べてください: {keys}")
        if not _is_count(self.excluded):
            raise ValueError(f"excluded には0以上の件数が必要です: {self.excluded!r}")
        if self.status is not None and not _is_count(self.status):
            raise ValueError(f"status には HTTP ステータスが必要です: {self.status!r}")
        if self.failure is not None and not isinstance(self.failure, GhFailure):
            raise TypeError(
                f"failure には GhFailure が必要です: {type(self.failure).__name__}"
            )
        # 完全な一覧でない結果は repository を持たない。部分的な一覧に値が入っていると、
        # 参考指標が黙って小さく出る（消費側が完全な一覧として集計するため）
        if not self.complete and (self.repos or self.excluded):
            raise ValueError(
                "完全な一覧でない結果に repository を持たせられません: "
                f"status={self.status!r} failure={self.failure!r}"
            )

    @property
    def complete(self) -> bool:
        """全ページを読み切った完全な一覧か。False のとき repos は空。"""
        return self.failure is None and self.status == 200


def _repo_page(body: str) -> list[tuple[str, bool]] | None:
    """repository 一覧の応答本文を (名前, 除外するか) の並びにする（読めなければ None）。

    要素を1つでも解釈できなければ、そのページ全体を読めなかったものとして扱う。読めない
    要素だけを飛ばすと、除外の印を読めなかった archived や fork の repository が対象へ
    混ざる（応答の形が変わった兆候でもあるので、静かに続けない）。
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    entries: list[tuple[str, bool]] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or not _REPO_NAME_RE.fullmatch(name):
            return None
        flags = [item.get(flag) for flag in _EXCLUDED_FLAGS]
        if not all(isinstance(flag, bool) for flag in flags):
            return None
        entries.append((name, any(flags)))
    return entries


def discover_repositories(
    github_org: str, runner: Runner | None = None
) -> RepoDiscovery:
    """Organization 内の参照できる repository を列挙する（設計書 §15.3）。

    取るのは repository のメタデータ（名前と除外の印）だけで、PR もコードも取得しない。
    参照できる範囲は token の権限がそのまま決めるため、対象を手で書き並べた allowlist は
    要らない。archived / fork / template は除外し、件数だけを残す。

    返すのは全ページを読み切れた場合の完全な一覧に限る。途中で失敗した場合は repos を
    空にして理由（status か failure）だけを返す——部分的な一覧を完全な一覧として集計
    されると、参考指標が黙って小さく出るため。ページ数の上限を超えた場合も、切り詰めた
    一覧を返さずエラーにする。

    github_org は設定のロード時に字句を検証済みの Organization 名（`is_github_org_name`）。
    runner を渡すと gh の呼び出しを差し替えられる（テスト用）。
    """
    run = run_gh if runner is None else runner
    seen: set[str] = set()      # 小文字の名前（除外した分も含む）
    kept: dict[str, str] = {}   # 小文字の名前 → API の表記
    excluded = 0
    for page in range(1, _MAX_REPO_PAGES + 1):
        # 並びを full_name で固定する（既定の created の降順では、列挙中に作られた
        # repository が先頭へ入り、以降のページが1件ずつずれる）
        path = (
            f"orgs/{github_org}/repos"
            f"?per_page={_REPOS_PER_PAGE}&sort=full_name&page={page}"
        )
        result = run(_read_args(path))
        if result.failure is not None:
            return RepoDiscovery(github_org=github_org, failure=result.failure)
        status, _, body = _parse_response(result.stdout)
        if status is None:
            return RepoDiscovery(github_org=github_org, failure=GhFailure.ERROR)
        if status != 200:
            # 403 と 404 の区別（権限か綴りか）は消費側が status で見る
            return RepoDiscovery(github_org=github_org, status=status)
        entries = _repo_page(body)
        if entries is None:
            return RepoDiscovery(github_org=github_org, failure=GhFailure.ERROR)
        for name, drop in entries:
            key = name.lower()
            if key in seen:
                continue   # 列挙中の頁ズレで、同じ repository が2ページに現れうる
            seen.add(key)
            if drop:
                excluded += 1
            else:
                kept[key] = name
        if len(entries) < _REPOS_PER_PAGE:
            return RepoDiscovery(
                github_org=github_org,
                repos=tuple(kept[key] for key in sorted(kept)),
                excluded=excluded,
                status=200,
            )
    # 上限までのページがすべて満杯だった場合。読み切れていない一覧を完全な一覧として
    # 返さない（この規模の Organization は想定外なので、値ではなくエラーで知らせる）
    return RepoDiscovery(github_org=github_org, failure=GhFailure.ERROR)


# ------------------------------------------------- PR の検索と raw cache

# キャッシュの置き場所（組織ディレクトリ直下）と、書き出す形式の版。
# 版は読み側が「この形として読める」ことを確かめるためだけに使う（互換の変換はしない）
PR_CACHE_DIRNAME = "github-cache"
PR_CACHE_SCHEMA = 1

# 対象月の形。ファイル名に入るため ASCII 数字のみ・全体一致で見る（`\d` は全角数字にも
# 一致する）。cli の同名の検査と同じ規則で、層をまたげないため実装は共有しない
_CACHE_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")

# GitHub が返す UTC の日時表記（秒まで・Z 終端）。字句のまま保存し、日時への変換は
# 指標計算の側（Step 37）が行う
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

# キャッシュに書く暦日の表記。`date.fromisoformat` は "20260801" のような区切りの無い
# 形も受けるため、保存した形そのものかどうかは字句で確かめる
_ISO_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# author の種別（GraphQL の `__typename`）。User / Bot / Organization / Mannequin /
# EnterpriseUserAccount のどれかだが、既知の集合には閉じない（新しい種別が増えたときに
# 収集そのものが止まるのは割に合わない）。字句だけを確かめて写す
_AUTHOR_TYPE_RE = re.compile(r"[A-Za-z]+")

# author の login に許す長さの上限。Bot・Mannequin の login は `_LOGIN_RE` とは別の形を
# とるため字句の規則までは課さず、長さと空白・制御文字の不在だけを見る
_MAX_AUTHOR_LOGIN_LENGTH = 100

# 1ページあたりの件数（GraphQL の search の上限）と、1クエリで辿れる件数の上限。
# 上限は GitHub 側の仕様で、超える期間は日単位へ割って取り直す
_SEARCH_PER_PAGE = 100
_SEARCH_RESULT_CAP = 1000
_MAX_SEARCH_PAGES = _SEARCH_RESULT_CAP // _SEARCH_PER_PAGE

# 一時的な障害として再試行する HTTP ステータス。規模の大きい Organization では GraphQL の
# search が散発的に 5xx を返すことがあり、同じページを呼び直せば通る
_TRANSIENT_STATUSES = (502, 503, 504)

# 再試行の前に待つ秒数（1回目の再試行の前に 2 秒、2回目の前に 5 秒）と、そこから決まる
# 1ページあたりの試行回数。回数を待ちの数から導いて、両者が食い違わないようにする
_RETRY_PAUSES = (2.0, 5.0)
_MAX_ATTEMPTS = 1 + len(_RETRY_PAUSES)

# 置換用の一時ファイルの接頭辞（`_write_cache`）。最終ファイル名に依らない固定長にする
_TMP_PREFIX = ".seat-tmp-"

# 検索に使う GraphQL 文書。取るのは §7.6 の9項目に対応するフィールドだけで、title・
# 本文・files・commits・comments・reviews は問い合わせそのものに書かない（保存側で
# 落とすのではなく、そもそも受け取らない）。
#
# REST の search/issues は additions / deletions を返さず PR ごとの追加呼び出しが要る
# ため使わない。並びは `sort:created-asc` で固定する（既定の best match は cursor で
# 辿る間に順位が動き、取りこぼしが黙って起きる）。
_PR_SEARCH_QUERY = """\
query($search: String!, $first: Int!, $after: String) {
  search(query: $search, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        isDraft
        createdAt
        mergedAt
        additions
        deletions
        repository { name }
        author { login __typename }
      }
    }
  }
}
"""


def _month_parts(month: object) -> tuple[int, int]:
    """対象月を (年, 月) にする（形式が外れれば ValueError）。"""
    if not isinstance(month, str) or not _CACHE_MONTH_RE.fullmatch(month):
        raise ValueError(f"month には YYYY-MM 形式が必要です: {month!r}")
    return int(month[:4]), int(month[5:7])


def pr_cache_path(cache_dir: Path, month: str) -> Path:
    """その月の PR キャッシュのパス（`<cache_dir>/prs-YYYY-MM.json`）。

    month はファイル名の一部になるため形式を厳密に検証する（検証しないと、対象月の
    指定で別のディレクトリのファイルを読み書きできてしまう）。
    """
    _month_parts(month)
    return Path(cache_dir) / f"prs-{month}.json"


def _today() -> dt.date:
    """現在の UTC 日付。

    窓を完了とみなすかどうかだけがこの値に依る。時計への依存をこの1箇所に閉じ、
    `collect_merged_prs` の today 引数と合わせて差し替えられるようにする。
    """
    return dt.datetime.now(dt.UTC).date()


def _is_plain_date(value: object) -> bool:
    """暦日として受ける値か（時刻を持つ datetime は含めない）。

    datetime を混ぜると比較と月の判定が変わるため受けない（admin_inputs と同じ規則）。
    """
    return isinstance(value, dt.date) and not isinstance(value, dt.datetime)


def _is_timestamp(value: object) -> bool:
    """GitHub が返す UTC の日時表記として読めるか（字句と暦の両方を見る）。"""
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        # 字句の後に暦と時刻の範囲まで見る（"2026-02-30T25:00:00Z" は形は通る）
        dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _utc_day(timestamp: str) -> dt.date:
    """日時表記の UTC 日付（`_is_timestamp` を通した値にだけ使う）。"""
    return dt.date.fromisoformat(timestamp[:10])


def _is_author_login(value: object) -> bool:
    """author の login として受ける字句か。

    `_LOGIN_RE` は課さない。Bot は `<名前>[bot]`、Mannequin は移行元の表記を残した
    login をとるため、人の login の規則で弾くと保存できなくなる。取り違えに直結するのは
    空白・制御文字が混ざった値（後続の突き合わせで静かに一致しなくなる）と、想定外に
    長い値なので、そこだけを見る。
    """
    return (
        isinstance(value, str)
        and value != ""
        and len(value) <= _MAX_AUTHOR_LOGIN_LENGTH
        and value.isprintable()
        and not any(char.isspace() for char in value)
    )


# ------------------------------------------------------------ 値オブジェクト


@dataclass(frozen=True)
class DateWindow:
    """両端を含む日付の区間（取得の単位）。"""

    start: dt.date
    end: dt.date

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            if not _is_plain_date(getattr(self, name)):
                raise TypeError(
                    f"{name} には datetime.date が必要です: "
                    f"{type(getattr(self, name)).__name__}"
                )
        if self.start > self.end:
            raise ValueError(
                f"start は end 以前にしてください: {self.start} 〜 {self.end}"
            )

    def contains(self, day: dt.date) -> bool:
        """day がこの区間に入るか（両端を含む）。"""
        return self.start <= day <= self.end

    @property
    def days(self) -> tuple[DateWindow, ...]:
        """1日ずつに割った区間（件数が上限を超えた窓を割り直すのに使う）。"""
        days = (
            self.start + dt.timedelta(days=offset)
            for offset in range((self.end - self.start).days + 1)
        )
        return tuple(DateWindow(start=day, end=day) for day in days)


@dataclass(frozen=True)
class CachedPr:
    """キャッシュへ保存する merged PR 1件（設計書 §7.6 の9項目だけ）。

    日時は GitHub が返した UTC の字句のまま持つ（保存物を再現可能にするため。日時への
    変換は指標計算の責務）。author_login と author_type は対で、author が null＝削除済みの
    アカウントのときだけどちらも None になる（片方だけ不明という状態は作らない）。
    """

    repository: str
    number: int
    author_login: str | None
    author_type: str | None
    created_at: str
    merged_at: str
    additions: int
    deletions: int
    is_draft: bool

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not _REPO_NAME_RE.fullmatch(
            self.repository
        ):
            raise ValueError(
                f"repository 名として読めない値です: {self.repository!r}"
            )
        if not _is_count(self.number) or self.number < 1:
            raise ValueError(f"number には1以上の整数が必要です: {self.number!r}")
        if self.author_login is not None and not _is_author_login(self.author_login):
            raise ValueError(
                f"author_login として読めない値です: {self.author_login!r}"
            )
        if self.author_type is not None and (
            not isinstance(self.author_type, str)
            or not _AUTHOR_TYPE_RE.fullmatch(self.author_type)
        ):
            raise ValueError(f"author_type として読めない値です: {self.author_type!r}")
        for name in ("created_at", "merged_at"):
            if not _is_timestamp(getattr(self, name)):
                raise ValueError(
                    f"{name} には UTC の日時表記（YYYY-MM-DDTHH:MM:SSZ）が必要です: "
                    f"{getattr(self, name)!r}"
                )
        # 字句は固定長なので文字列の比較で時系列の比較になる
        if self.created_at > self.merged_at:
            raise ValueError(
                f"created_at は merged_at 以前にしてください: "
                f"{self.created_at} 〜 {self.merged_at}"
            )
        for name in ("additions", "deletions"):
            if not _is_count(getattr(self, name)):
                raise ValueError(
                    f"{name} には0以上の件数が必要です: {getattr(self, name)!r}"
                )
        # login と種別は同じ author から一緒に読む。片方だけ欠けた値を受けると、
        # 「種別の分からない作成者」と「作成者の分からない PR」が混ざって後段で区別できない
        if (self.author_login is None) != (self.author_type is None):
            raise ValueError(
                "author_login と author_type は両方に値を持たせるか、両方とも不明に"
                f"してください: {self.author_login!r} / {self.author_type!r}"
            )
        if not isinstance(self.is_draft, bool):
            raise TypeError(
                f"is_draft には真偽値が必要です: {type(self.is_draft).__name__}"
            )

    @property
    def key(self) -> str:
        """一意キー（設計書 §7.6）。"""
        return f"{self.repository}#{self.number}"

    @property
    def merged_day(self) -> dt.date:
        """mergedAt の UTC 日付（帰属する月を決める値）。"""
        return _utc_day(self.merged_at)


def _pr_order(pr: CachedPr) -> tuple[str, int]:
    """保存と比較の並び。repository は小文字で見る（GitHub は大小を区別しない）。"""
    return (pr.repository.lower(), pr.number)


def month_windows(month: str) -> tuple[DateWindow, ...]:
    """対象月を固定の窓に割る（1–7 / 8–14 / 15–21 / 22–28 / 29–月末）。

    窓は月をまたがない。29日以降が無い月（閏年でない2月）は4窓になる。境界を月の長さで
    動かさないのは、同じ月を何度収集しても窓の集合が変わらないようにするため（変わると
    完了済みの記録が意味を持たなくなる）。
    """
    year, mon = _month_parts(month)
    last = calendar.monthrange(year, mon)[1]
    return tuple(
        DateWindow(
            start=dt.date(year, mon, first),
            end=dt.date(year, mon, min(first + 6, last)),
        )
        for first in (1, 8, 15, 22, 29)
        if first <= last
    )


@dataclass(frozen=True)
class PrCache:
    """1組織×1月の PR キャッシュの内容。

    prs は `(repository の小文字, number)` の昇順で重複なし。complete_windows は
    「読み切れて、かつ終端の翌日が過ぎた」窓で、`month_windows(month)` の要素だけを
    昇順・重複なしで持つ。

    repositories は収集した時点の repository の一覧（`discover_repositories` の結果）で、
    持たないキャッシュでは None。持つ場合は同じ Organization の完全な一覧に限る——
    部分的な一覧を載せられると、集計の側がそれを完全な一覧として扱い、一覧に載らなかった
    repository の PR が「対象外」へ流れて参考指標が黙って小さく出る。
    """

    github_org: str
    month: str
    prs: tuple[CachedPr, ...] = ()
    complete_windows: tuple[DateWindow, ...] = ()
    repositories: RepoDiscovery | None = None

    def __post_init__(self) -> None:
        if not is_github_org_name(self.github_org):
            raise ValueError(
                f"github_org には Organization 名が必要です: {self.github_org!r}"
            )
        _month_parts(self.month)
        if not isinstance(self.prs, tuple) or not all(
            isinstance(pr, CachedPr) for pr in self.prs
        ):
            raise TypeError("prs には CachedPr の tuple が必要です")
        keys = [_pr_order(pr) for pr in self.prs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"prs の PR が重複しています: {sorted(set(keys))}")
        if keys != sorted(keys):
            raise ValueError(
                "prs は repository（小文字）と number の昇順で並べてください"
            )
        # 1ファイル = 1組織 × 1月。収集経路では窓の内側しか採らないので起きないが、
        # 読み側でも確かめる（別の月の PR が混ざったキャッシュを集計へ渡さないため）
        for pr in self.prs:
            if pr.merged_at[:7] != self.month:
                raise ValueError(
                    f"prs に {self.month} 以外の月に merge された PR があります: "
                    f"{pr.key}（merged_at={pr.merged_at}）"
                )
        if not isinstance(self.complete_windows, tuple) or not all(
            isinstance(window, DateWindow) for window in self.complete_windows
        ):
            raise TypeError("complete_windows には DateWindow の tuple が必要です")
        known = month_windows(self.month)
        for window in self.complete_windows:
            if window not in known:
                raise ValueError(
                    f"complete_windows に {self.month} の窓でない区間があります: "
                    f"{window.start} 〜 {window.end}"
                )
        starts = [window.start for window in self.complete_windows]
        if len(set(starts)) != len(starts):
            raise ValueError(f"complete_windows が重複しています: {starts}")
        if starts != sorted(starts):
            raise ValueError("complete_windows は開始日の昇順で並べてください")
        if self.repositories is None:
            return
        if not isinstance(self.repositories, RepoDiscovery):
            raise TypeError(
                "repositories には RepoDiscovery か None が必要です: "
                f"{type(self.repositories).__name__}"
            )
        if self.repositories.github_org != self.github_org:
            raise ValueError(
                "repository の一覧とキャッシュの Organization が違います: "
                f"{self.repositories.github_org!r} / {self.github_org!r}"
            )
        if not self.repositories.complete:
            raise ValueError(
                "完全でない repository の一覧はキャッシュに持たせられません"
                f"（status={self.repositories.status!r} "
                f"failure={self.repositories.failure!r}）"
            )

    @property
    def complete(self) -> bool:
        """対象月の全窓を読み切ったか。"""
        return self.complete_windows == month_windows(self.month)


@dataclass(frozen=True)
class PrCollection:
    """`collect_merged_prs` の結果（保存した内容の要約）。

    upserted は今回追加または内容が変わった PR の件数、total は保存後の全件数。
    stopped は取得を中断した窓で、status はそのとき最後に読んだ HTTP ステータス
    （応答として解釈できなければ None）。中断したかどうかは failure と stopped の
    両方で表す（片方だけが立つ状態を作らない）。

    repository_count は集計の対象にした repository の数、excluded_repository_count は
    archived / fork / template として除外した数（どちらも渡された一覧そのままの値）。
    """

    github_org: str
    month: str
    path: Path
    upserted: int = 0
    total: int = 0
    complete: bool = False
    stopped: DateWindow | None = None
    status: int | None = None
    failure: GhFailure | None = None
    repository_count: int = 0
    excluded_repository_count: int = 0

    def __post_init__(self) -> None:
        if not is_github_org_name(self.github_org):
            raise ValueError(
                f"github_org には Organization 名が必要です: {self.github_org!r}"
            )
        _month_parts(self.month)
        if not isinstance(self.path, Path):
            raise TypeError(f"path には Path が必要です: {type(self.path).__name__}")
        for name in (
            "upserted", "total", "repository_count", "excluded_repository_count",
        ):
            if not _is_count(getattr(self, name)):
                raise ValueError(
                    f"{name} には0以上の件数が必要です: {getattr(self, name)!r}"
                )
        if self.upserted > self.total:
            raise ValueError(
                f"upserted は total 以下です: {self.upserted} / {self.total}"
            )
        if not isinstance(self.complete, bool):
            raise TypeError(
                f"complete には真偽値が必要です: {type(self.complete).__name__}"
            )
        if self.stopped is not None and not isinstance(self.stopped, DateWindow):
            raise TypeError(
                f"stopped には DateWindow が必要です: {type(self.stopped).__name__}"
            )
        if self.status is not None and not _is_count(self.status):
            raise ValueError(f"status には HTTP ステータスが必要です: {self.status!r}")
        if self.failure is not None and not isinstance(self.failure, GhFailure):
            raise TypeError(
                f"failure には GhFailure が必要です: {type(self.failure).__name__}"
            )
        # 中断の事実は理由と窓の両方で表す。片方だけだと、消費側が「どこから再開すれば
        # よいか」または「なぜ止まったか」のどちらかを持たない結果を受け取ることになる
        if (self.failure is None) != (self.stopped is None):
            raise ValueError(
                "中断した結果は failure と stopped の両方を持たせてください: "
                f"failure={self.failure!r} stopped={self.stopped!r}"
            )
        if self.complete and self.failure is not None:
            raise ValueError(
                f"全窓を読み切った結果に中断の理由は付きません: {self.failure!r}"
            )


# ------------------------------------------------------------ キャッシュの読み書き

# キャッシュの形。読み側は未知のキーを拒否する（禁止フィールドが混ざったキャッシュを
# 黙って使わないため。手で足した項目も同じ扱いにする）。
#
# repositories だけは任意にする。一覧を保存する前に作られたキャッシュを読めなくすると、
# 収集済みの PR ごと取り直すことになる（一覧は収集を再実行すれば付く）。形式の版は
# 上げない——任意のキーの追加は読み側の互換を壊さないため。
_CACHE_REQUIRED_KEYS = ("complete_windows", "github_org", "month", "prs", "schema")
_CACHE_OPTIONAL_KEYS = ("repositories",)
_CACHE_KEYS = tuple(sorted(_CACHE_REQUIRED_KEYS + _CACHE_OPTIONAL_KEYS))

# repository の一覧を保存する形（`RepoDiscovery` のうち、完全な一覧が持つ2項目だけ）
_REPOSITORIES_KEYS = ("excluded", "names")

_PR_ENTRY_KEYS = (
    "additions",
    "author_login",
    "author_type",
    "created_at",
    "deletions",
    "is_draft",
    "merged_at",
    "number",
    "repository",
)


def _pr_entry(pr: CachedPr) -> dict:
    """PR 1件を保存する形にする（明示した9項目だけを写す）。"""
    return {
        "repository": pr.repository,
        "number": pr.number,
        "author_login": pr.author_login,
        "author_type": pr.author_type,
        "created_at": pr.created_at,
        "merged_at": pr.merged_at,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "is_draft": pr.is_draft,
    }


def _cache_payload(cache: PrCache) -> dict:
    """キャッシュを JSON へ直列化する形にする。

    repository の一覧を持たないキャッシュではキーそのものを書かない（空の一覧と
    「一覧が無い」を区別できる形にする）。
    """
    payload = {
        "schema": PR_CACHE_SCHEMA,
        "github_org": cache.github_org,
        "month": cache.month,
        "complete_windows": [
            [window.start.isoformat(), window.end.isoformat()]
            for window in cache.complete_windows
        ],
        "prs": {pr.key: _pr_entry(pr) for pr in cache.prs},
    }
    if cache.repositories is not None:
        payload["repositories"] = {
            "names": list(cache.repositories.repos),
            "excluded": cache.repositories.excluded,
        }
    return payload


def _default_file_mode() -> int:
    """umask を反映した新規ファイルの権限（open の既定と同じ意味にする）。

    規則は `report/document._atomic_write` と同じ（層をまたげないため実装は共有しない）。
    os.umask に読み取り専用の API が無いため 0 を設定して即戻す。
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _write_cache(path: Path, cache: PrCache) -> None:
    """キャッシュを書く（同一ディレクトリの一時ファイル経由で置換する）。

    直接書くと、書き込みの途中で中断したときに収集済みの内容ごと失われる（次の実行が
    完了済みの窓を知らないまま全部取り直すことになる）。置換なら失敗しても元の内容が残る。
    一時ファイルは mkstemp 由来で 0600 になるため、既存ファイルはその権限を引き継がせ、
    新規作成時は umask 既定を使う。

    同じ内容からは常に同じバイト列になる形で書く（キーを並べ替え、改行を LF に固定する）。
    """
    text = json.dumps(
        _cache_payload(cache), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    # 作るのはキャッシュのディレクトリだけで、その親（組織ディレクトリ）が無ければ作らない
    # （綴りの違う場所を渡されたとき、静かに新しい場所へ保存しないため）
    path.parent.mkdir(exist_ok=True)
    mode = (path.stat().st_mode & 0o7777) if path.exists() else _default_file_mode()
    tmp: Path | None = None
    try:
        # 名前を close 後に os.replace で使うため with で開かない（直後に with f: で閉じる）
        f = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=_TMP_PREFIX, suffix=".tmp", delete=False,
        )
        tmp = Path(f.name)
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _cache_error(path: Path, detail: str) -> ValueError:
    """キャッシュを読めないことを表す例外（メッセージはパスと理由の組）。

    型の不一致も「入力として不正」の側に寄せて ValueError にする。ファイルの内容は
    利用者が置いたデータで、CLI は ValueError を「エラー: <理由>」と終了コード 1 に
    写す（他の loader と同じ扱い。例外の種類を変えると表示と終了コードが変わる）。
    """
    return ValueError(f"{path}: {detail}")


def _cache_window(value: object, path: Path, month: str) -> DateWindow:
    """complete_windows の1要素を DateWindow にする。"""
    if not isinstance(value, list) or len(value) != 2:
        raise _cache_error(
            path, f"complete_windows の要素は [開始日, 終了日] の2要素です: {value!r}"
        )
    if not all(isinstance(day, str) and _ISO_DAY_RE.fullmatch(day) for day in value):
        raise _cache_error(
            path,
            "complete_windows の日付は YYYY-MM-DD の文字列で書いてください: "
            f"{value!r}",
        )
    try:
        start, end = (dt.date.fromisoformat(day) for day in value)
        return DateWindow(start=start, end=end)
    except (TypeError, ValueError) as exc:
        raise _cache_error(
            path,
            f"complete_windows の要素を {month} の区間として読めません: "
            f"{value!r}（{exc}）",
        ) from exc


def _cache_repositories(
    value: object, path: Path, github_org: str
) -> RepoDiscovery:
    """repositories の内容を RepoDiscovery にする。

    読み方は prs と同じ厳しさにする。名前の字句・重複・並びは値オブジェクトが見るので、
    ここで見るのは JSON としての形（オブジェクトか・キーが2つそろっているか・names が
    文字列の配列か・excluded が0以上の整数か）だけにする。
    """
    if not isinstance(value, dict):
        raise _cache_error(
            path, f"repositories が repository の一覧ではありません: {value!r}"
        )
    unknown = sorted(set(value) - set(_REPOSITORIES_KEYS))
    if unknown:
        raise _cache_error(
            path,
            f"repositories に未知のキーがあります: {unknown}"
            f"（書ける項目は {', '.join(_REPOSITORIES_KEYS)} だけです）",
        )
    missing = sorted(set(_REPOSITORIES_KEYS) - set(value))
    if missing:
        raise _cache_error(path, f"repositories に項目がありません: {missing}")
    names, excluded = value["names"], value["excluded"]
    if not isinstance(names, list) or not all(
        isinstance(name, str) for name in names
    ):
        raise _cache_error(
            path, f"repositories.names が文字列の配列ではありません: {names!r}"
        )
    if not _is_count(excluded):
        raise _cache_error(
            path, f"repositories.excluded には0以上の件数が必要です: {excluded!r}"
        )
    try:
        return RepoDiscovery(
            github_org=github_org, repos=tuple(names), excluded=excluded, status=200
        )
    except (TypeError, ValueError) as exc:
        raise _cache_error(path, f"repositories を読めません: {exc}") from exc


def _cache_pr(key: object, value: object, path: Path) -> CachedPr:
    """prs の1要素を CachedPr にする（キーと内容の一致まで確かめる）。"""
    if not isinstance(value, dict):
        raise _cache_error(path, f"prs.{key} が PR の内容ではありません: {value!r}")
    unknown = sorted(set(value) - set(_PR_ENTRY_KEYS))
    if unknown:
        raise _cache_error(
            path,
            f"prs.{key} に未知のキーがあります: {unknown}"
            f"（保存する項目は {', '.join(_PR_ENTRY_KEYS)} だけです）",
        )
    missing = sorted(set(_PR_ENTRY_KEYS) - set(value))
    if missing:
        raise _cache_error(path, f"prs.{key} に項目がありません: {missing}")
    try:
        pr = CachedPr(**value)
    except (TypeError, ValueError) as exc:
        raise _cache_error(path, f"prs.{key} を読めません: {exc}") from exc
    if pr.key != key:
        raise _cache_error(
            path,
            f"prs のキー {key!r} が内容の repository#number（{pr.key}）と"
            "一致しません",
        )
    return pr


def load_pr_cache(cache_dir: Path, month: str, github_org: str) -> PrCache:
    """その月の PR キャッシュを読む（ファイルが無ければ空の内容）。

    読み方は厳密にする。形式の版・組織・月の不一致、未知のキー、キーと内容の食い違い、
    重複はすべて ValueError にして中止する。緩く読むと、別の組織のキャッシュや、手で
    項目を足したファイルをそのまま集計へ渡すことになる（禁止フィールドが混ざった
    キャッシュを黙って使わないための検査でもある）。
    """
    if not is_github_org_name(github_org):
        raise ValueError(
            f"github_org には Organization 名が必要です: {github_org!r}"
        )
    path = pr_cache_path(cache_dir, month)
    if not path.is_file():
        return PrCache(github_org=github_org, month=month)

    # 改行は読み取りで LF へ揃える（書き込み側が LF を固定するのと合わせて1つの規約）
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise _cache_error(path, f"JSON として読めません（{exc}）") from exc
    if not isinstance(payload, dict):
        raise _cache_error(path, "JSON のオブジェクトではありません")
    unknown = sorted(set(payload) - set(_CACHE_KEYS))
    if unknown:
        raise _cache_error(
            path,
            f"未知のキーがあります: {unknown}"
            f"（書ける項目は {', '.join(_CACHE_KEYS)} だけです）",
        )
    missing = sorted(set(_CACHE_REQUIRED_KEYS) - set(payload))
    if missing:
        raise _cache_error(path, f"項目がありません: {missing}")
    # 真偽値と浮動小数点は版として受けない（Python では True == 1・1.0 == 1 が成り立ち、
    # JSON の true / 1.0 が版 1 のキャッシュとして通ってしまう）
    schema = payload["schema"]
    if not _is_count(schema) or schema != PR_CACHE_SCHEMA:
        raise _cache_error(
            path,
            f"対応していない形式です（schema={schema!r}・"
            f"読めるのは {PR_CACHE_SCHEMA}）",
        )
    if payload["github_org"] != github_org:
        raise _cache_error(
            path,
            f"別の Organization（{payload['github_org']!r}）のキャッシュです。"
            f"今回の対象は {github_org!r} なので、このキャッシュを別の場所へ"
            "移してから再実行してください",
        )
    if payload["month"] != month:
        raise _cache_error(
            path,
            f"別の月（{payload['month']!r}）のキャッシュです。"
            f"今回の対象は {month!r} です",
        )

    windows = payload["complete_windows"]
    if not isinstance(windows, list):
        raise _cache_error(path, "complete_windows が配列ではありません")
    prs = payload["prs"]
    if not isinstance(prs, dict):
        raise _cache_error(path, "prs がオブジェクトではありません")
    repositories = (
        _cache_repositories(payload["repositories"], path, github_org)
        if "repositories" in payload else None
    )

    found = [_cache_pr(key, value, path) for key, value in prs.items()]
    seen: dict[tuple[str, int], str] = {}
    for pr in found:
        other = seen.setdefault(_pr_order(pr), pr.key)
        if other != pr.key:
            raise _cache_error(
                path,
                f"同じ PR が2つのキーで入っています（{other} と {pr.key}）。"
                "GitHub の repository 名は大文字小文字を区別しません",
            )
    try:
        return PrCache(
            github_org=github_org,
            month=month,
            prs=tuple(sorted(found, key=_pr_order)),
            complete_windows=tuple(sorted(
                (_cache_window(window, path, month) for window in windows),
                key=lambda window: window.start,
            )),
            repositories=repositories,
        )
    except (TypeError, ValueError) as exc:
        raise _cache_error(path, f"キャッシュの内容が不正です（{exc}）") from exc


# ------------------------------------------------------------ 検索と収集


class _Page(NamedTuple):
    """検索の1ページの読み取り結果。

    prs が None なら「読めなかった」で、理由は status と failure が表す。読めた場合の
    total はその検索条件でヒットした総件数（ページの件数ではない）。
    """

    prs: tuple[CachedPr, ...] | None
    total: int = 0
    has_next: bool = False
    cursor: str | None = None
    status: int | None = None
    failure: GhFailure | None = None


class _Collected(NamedTuple):
    """1つの窓の収集結果（prs が None なら取り切れていない）。"""

    prs: tuple[CachedPr, ...] | None
    status: int | None = None
    failure: GhFailure | None = None


def _search_expression(github_org: str, window: DateWindow) -> str:
    """検索文字列（Organization・merged の期間・並び）。期間は両端を含む UTC の日付。"""
    return (
        f"org:{github_org} is:pr is:merged "
        f"merged:{window.start.isoformat()}..{window.end.isoformat()} "
        "sort:created-asc"
    )


def _is_rate_limited(status: int, headers: dict[str, str]) -> bool:
    """応答が利用上限によるものか。

    GitHub は二次上限を 429 で、一次上限を 403 + `Retry-After` か
    `X-RateLimit-Remaining: 0` で返す。権限不足の 403 と区別するため、403 は
    ヘッダを見てから上限と判断する（区別しないと、権限の問題を「時間をおいて再実行」と
    案内し続けることになる）。
    """
    if status == 429:
        return True
    return status == 403 and (
        "retry-after" in headers
        or headers.get("x-ratelimit-remaining", "").strip() == "0"
    )


def _pr_author(author: object) -> tuple[str | None, str | None] | None:
    """author を (login, 種別) にする（読めなければ None）。

    author 自体の null は削除済みのアカウントで、(None, None) として受ける。dict では
    あるのに login か種別を読めない場合は、node 全体を読めなかったものとして扱う
    （返り値の None がその合図で、(None, None) とは別の意味）。
    """
    if author is None:
        return (None, None)
    if not isinstance(author, dict):
        return None
    login, kind = author.get("login"), author.get("__typename")
    if not _is_author_login(login):
        return None
    if not isinstance(kind, str) or not _AUTHOR_TYPE_RE.fullmatch(kind):
        return None
    return (login, kind)


def _pr_node(node: object, window: DateWindow) -> CachedPr | None:
    """検索結果の1要素を CachedPr にする（読めなければ None）。

    読むのは §7.6 の9項目に対応するフィールドだけで、node に余分な項目（title 等）が
    あっても触らない。必須フィールドの欠落・型不一致・repository 名として読めない値・
    要求した期間の外の mergedAt・createdAt が mergedAt より後、はいずれも「読めない」に
    倒す（期間の外が混ざると、月をまたいだ PR が別の月のキャッシュへ入る）。
    """
    if not isinstance(node, dict) or "author" not in node:
        return None
    repository = node.get("repository")
    name = repository.get("name") if isinstance(repository, dict) else None
    number = node.get("number")
    created_at, merged_at = node.get("createdAt"), node.get("mergedAt")
    additions, deletions = node.get("additions"), node.get("deletions")
    is_draft = node.get("isDraft")
    if not isinstance(name, str) or not _REPO_NAME_RE.fullmatch(name):
        return None
    if not _is_count(number) or number < 1:
        return None
    if not _is_timestamp(created_at) or not _is_timestamp(merged_at):
        return None
    if not _is_count(additions) or not _is_count(deletions):
        return None
    if not isinstance(is_draft, bool):
        return None
    if created_at > merged_at:
        return None
    if not window.contains(_utc_day(merged_at)):
        return None
    author = _pr_author(node["author"])
    if author is None:
        return None
    login, kind = author
    return CachedPr(
        repository=name,
        number=number,
        author_login=login,
        author_type=kind,
        created_at=created_at,
        merged_at=merged_at,
        additions=additions,
        deletions=deletions,
        is_draft=is_draft,
    )


def _search_body(body: str, window: DateWindow, status: int) -> _Page:
    """検索の応答本文を1ページの結果にする。

    要素を1つでも解釈できなければページ全体を読めなかったものとして扱う（読めない要素
    だけを飛ばすと、その PR が黙って集計から落ちる。応答の形が変わった兆候でもあるので
    静かに続けない）。規則は repository の発見（`_repo_page`）と同じ。
    """
    unreadable = _Page(None, status=status, failure=GhFailure.ERROR)
    try:
        payload = json.loads(body)
    except ValueError:
        return unreadable
    if not isinstance(payload, dict):
        return unreadable
    errors = payload.get("errors")
    if errors is not None:
        # GraphQL は上限も 200 + errors で返す。再実行で解消するかどうかが違うので分ける
        rate_limited = isinstance(errors, list) and any(
            isinstance(error, dict) and error.get("type") == "RATE_LIMITED"
            for error in errors
        )
        return _Page(
            None, status=status,
            failure=GhFailure.RATE_LIMITED if rate_limited else GhFailure.ERROR,
        )
    data = payload.get("data")
    search = data.get("search") if isinstance(data, dict) else None
    if not isinstance(search, dict):
        return unreadable
    total = search.get("issueCount")
    page_info = search.get("pageInfo")
    nodes = search.get("nodes")
    if not _is_count(total) or not isinstance(page_info, dict):
        return unreadable
    has_next, cursor = page_info.get("hasNextPage"), page_info.get("endCursor")
    if not isinstance(has_next, bool):
        return unreadable
    if cursor is not None and not isinstance(cursor, str):
        return unreadable
    if not isinstance(nodes, list):
        return unreadable
    found: list[CachedPr] = []
    for node in nodes:
        pr = _pr_node(node, window)
        if pr is None:
            return unreadable
        found.append(pr)
    return _Page(
        tuple(found), total=total, has_next=has_next, cursor=cursor, status=status
    )


def _is_transient(page: _Page) -> bool:
    """その失敗が呼び直しで解消しうるか。

    対象は上流の一時的な障害（`_TRANSIENT_STATUSES`）と応答待ちの打ち切りだけにする。
    利用上限を呼び直すと上限をさらに消費し、本文の解釈不能・認証と権限の不足・gh の
    不在は何度呼んでも同じ結果になるため、いずれも即座に止める側へ残す。
    """
    if page.prs is not None:
        return False
    if page.failure is GhFailure.TIMEOUT:
        return True
    return page.failure is GhFailure.ERROR and page.status in _TRANSIENT_STATUSES


def _search_page(
    run: Runner, github_org: str, window: DateWindow, cursor: str | None,
    sleep: Sleeper,
) -> _Page:
    """検索を1ページ読む（一時的な失敗は少し待って数回まで呼び直す）。

    呼び直すのは同じ引数の同じページなので、成功したときの結果は1回で成功した場合と
    変わらない。試行を使い切ったら最後の失敗をそのまま返す（呼び出し側はその窓で止める）。
    """
    page = _search_once(run, github_org, window, cursor)
    for pause in _RETRY_PAUSES:
        if not _is_transient(page):
            return page
        sleep(pause)
        page = _search_once(run, github_org, window, cursor)
    return page


def _search_once(
    run: Runner, github_org: str, window: DateWindow, cursor: str | None
) -> _Page:
    """検索を1回呼んで1ページ分の結果にする。"""
    result = run(_graphql_args(_search_expression(github_org, window), cursor))
    if result.failure is not None:
        return _Page(None, failure=result.failure)
    status, headers, body = _parse_response(result.stdout)
    if status is None:
        return _Page(None, failure=GhFailure.ERROR)
    if _is_rate_limited(status, headers):
        return _Page(None, status=status, failure=GhFailure.RATE_LIMITED)
    if status != 200:
        return _Page(None, status=status, failure=GhFailure.ERROR)
    return _search_body(body, window, status)


def _collect_window(
    run: Runner, github_org: str, window: DateWindow, *,
    sleep: Sleeper, split: bool = True,
) -> _Collected:
    """1つの窓の merged PR を全ページ読む（読み切れなければ prs=None）。

    ヒット数が検索の上限を超える窓は、split が真なら1日ずつに割って取り直す（上限は
    1クエリあたりなので、期間を細かくすれば読み切れる）。割った先でも超える場合は、
    切り詰めた結果を返さず中断する。
    """
    first = _search_page(run, github_org, window, None, sleep)
    if first.prs is None:
        return _Collected(None, first.status, first.failure)
    if first.total > _SEARCH_RESULT_CAP:
        if not split:
            return _Collected(None, first.status, GhFailure.TOO_MANY_RESULTS)
        # 1ページ目の結果は捨てる（同じ期間を日単位で取り直すため）
        found: dict[tuple[str, int], CachedPr] = {}
        for day in window.days:
            collected = _collect_window(run, github_org, day, sleep=sleep, split=False)
            if collected.prs is None:
                return collected
            found.update((_pr_order(pr), pr) for pr in collected.prs)
        return _Collected(tuple(found[key] for key in sorted(found)), first.status)

    found = {_pr_order(pr): pr for pr in first.prs}
    has_next, cursor, pages = first.has_next, first.cursor, 1
    while has_next:
        pages += 1
        if pages > _MAX_SEARCH_PAGES or cursor is None:
            # 上限までページが続く（＝上限を超える件数を辿れている）のも、次があるのに
            # cursor が無いのも、読み切れたことにしてよい状態ではない
            return _Collected(None, first.status, GhFailure.ERROR)
        page = _search_page(run, github_org, window, cursor, sleep)
        if page.prs is None:
            return _Collected(None, page.status, page.failure)
        found.update((_pr_order(pr), pr) for pr in page.prs)
        has_next, cursor = page.has_next, page.cursor
    return _Collected(tuple(found[key] for key in sorted(found)), first.status)


def collect_merged_prs(
    github_org: str, month: str, cache_dir: Path, *, repos: RepoDiscovery,
    runner: Runner | None = None, today: dt.date | None = None,
    sleep: Sleeper | None = None,
) -> PrCollection:
    """対象月の merged PR のメタデータを収集して cache_dir へ保存する（設計書 §15.4）。

    取るのは §7.6 の9項目だけで、title・本文・レビュー本文・files・diff・commit
    message・コードは取得も保存もしない。月を固定の窓に割って順に取り、既に完了済みの
    窓と、まだ始まっていない窓は問い合わせない。窓の取得に失敗したらそこで止め、それ
    までの結果と完了済みの窓を保存して理由を返す（再実行は続きから進む）。

    窓を完了とするのは、読み切れて、かつ終端の翌日が過ぎている場合だけ。今日・昨日を
    含む窓は毎回取り直され、同じ PR は upsert されるので結果は変わらない。

    repos は `discover_repositories` で得た同じ Organization の完全な一覧で、PR と一緒に
    キャッシュへ保存する。集計の側（`github_metrics`）はこの一覧を使うので、収集した
    時点の一覧をキャッシュが持つことで、集計が gh もネットワークも呼ばずに済む。
    別の Organization の一覧・完全でない一覧は何も書かずに中止する（一覧に載らなかった
    repository の PR が「対象外」へ流れ、参考指標が黙って小さく出るため）。

    一時的な障害（`_TRANSIENT_STATUSES`・応答待ちの打ち切り）は、同じページを少し待って
    `_MAX_ATTEMPTS` 回まで呼び直す。それでも通らなければ従来どおりその窓で止める。

    today を渡すと「今日」を、runner を渡すと gh の呼び出しを、sleep を渡すと再試行の
    待ちを差し替えられる（いずれもテスト用。sleep の既定は `time.sleep`）。
    """
    if not is_github_org_name(github_org):
        raise ValueError(
            f"github_org には Organization 名が必要です: {github_org!r}"
        )
    if not isinstance(repos, RepoDiscovery):
        raise TypeError(
            f"repos には RepoDiscovery が必要です: {type(repos).__name__}"
        )
    if repos.github_org != github_org:
        raise ValueError(
            "repository の一覧と収集の Organization が違います: "
            f"{repos.github_org!r} / {github_org!r}"
        )
    if not repos.complete:
        raise ValueError(
            "repository の一覧が完全でないため収集できません"
            f"（status={repos.status!r} failure={repos.failure!r}）。"
            "一覧を取り直してから収集してください"
        )
    if today is not None and not _is_plain_date(today):
        raise TypeError(
            f"today には datetime.date が必要です: {type(today).__name__}"
        )
    path = pr_cache_path(cache_dir, month)
    # 壊れたキャッシュは読めた時点で中止する（何も書かない）。不完全な状態のうえに
    # upsert すると、取りこぼしを抱えたまま「完了」になりうる
    cache = load_pr_cache(cache_dir, month, github_org)
    run = run_gh if runner is None else runner
    pause = time.sleep if sleep is None else sleep
    day = _today() if today is None else today

    before = {_pr_order(pr): pr for pr in cache.prs}
    entries = dict(before)
    done = set(cache.complete_windows)
    windows = month_windows(month)
    stopped: DateWindow | None = None
    status: int | None = None
    failure: GhFailure | None = None
    for window in windows:
        if window in done or window.start > day:
            # 完了済みと、まだ始まっていない窓は問い合わせない（未来の窓は完了にもしない）
            continue
        collected = _collect_window(run, github_org, window, sleep=pause)
        if collected.prs is None:
            # 取り切れなかった窓の PR は採らない（窓の粒度で all-or-nothing）
            stopped, status, failure = window, collected.status, collected.failure
            break
        entries.update((_pr_order(pr), pr) for pr in collected.prs)
        if window.end < day - dt.timedelta(days=1):
            done.add(window)

    written = PrCache(
        github_org=github_org,
        month=month,
        prs=tuple(entries[key] for key in sorted(entries)),
        # 並びは窓の定義順から作る（set の反復順に依らせない）
        complete_windows=tuple(window for window in windows if window in done),
        repositories=repos,
    )
    _write_cache(path, written)
    return PrCollection(
        github_org=github_org,
        month=month,
        path=path,
        upserted=sum(1 for key, pr in entries.items() if before.get(key) != pr),
        total=len(entries),
        complete=written.complete,
        stopped=stopped,
        status=status,
        failure=failure,
        repository_count=len(repos.repos),
        excluded_repository_count=repos.excluded,
    )
