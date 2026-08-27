"""管理画面の設定を写した手入力 CSV（`input/<org>/admin/`）を読む純粋なモジュール。

管理画面には CSV エクスポートが無いため、組織の契約とユーザごとの追加クレジット設定は
人手で写した小さな表を正準形式として受け取る（設計書 §7.4 / §7.5）。

- `organization-YYYY-MM-DD.csv`: 組織単位の契約（購入席数・支払頻度・契約単価・組織
  クレジット）。1ファイル1行
- `users-YYYY-MM-DD.csv`: ユーザ単位の追加クレジット（有効・無効・上限・当月消費）

読み取りと選択だけを行う。ファイルを書かず、判定もせず、現在時刻も参照しないため、
同じ入力からは常に同じ結果と同じ警告の並びを返す。

値は「不明」を保つ。空欄は None（＝未記入）で、0 や False では埋めない。解釈できない値も
None にし、警告として残す（写し間違いを、分析を止めずに気付ける形にする）。一方で取り違え
そのものに直結するもの——ファイル名の取得日と `snapshot_date` の食い違い、同一種別の同日
重複、organization の複数行、email の欠落・重複——は ValueError で中止する。

as-of の選択は種別ごとに独立で、対象月の月末以前で最新のスナップショットを採る（月末
以前が1つも無ければ最古へフォールバックして強警告）。members の「月末に最も近い（未来を
含む）」とは規則が違う。members は構成のエクスポートで、対象月末時点の構成は翌月初の
ファイルに入っている。admin は設定値の観測なので、月末より後のスナップショットは月末
以降に行われた設定変更を含みうる。したがって未来側へは寄せず月末以前を優先する
（`ingest._resolve_members_info_path` と同じ規則）。

ロード結果は対象月で絞らず全時点を保持する。月内の推移（週次スナップショットでの当月
消費の上昇）を見るには、判定に使う1点だけでは足りないため。
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypeVar

import pandas as pd

from . import ingest

# 入力の置き場所（組織ディレクトリ直下）と、種別を決めるファイル名の接頭辞
ADMIN_SUBDIR = "admin"
_ORGANIZATION = "organization"
_USERS = "users"
_KINDS = (_ORGANIZATION, _USERS)

# 欠損時に NA で補う任意列（正準名）。設計書 §7.4 / §7.5 の必須列以外のすべてで、
# 「列を書かない」ことが手入力の通常の運用にあたるため警告を増やさない
ORGANIZATION_OPTIONAL_COLUMNS = (
    "standard_purchased",
    "premium_purchased",
    "billing_frequency",
    "renewal_date",
    "standard_unit_price_usd",
    "premium_unit_price_usd",
    "org_credit_enabled",
    "org_credit_limit_usd",
)
USERS_OPTIONAL_COLUMNS = (
    "account_uuid",
    "credit_enabled",
    "credit_limit_usd",
    "credit_mtd_usd",
)

# 有効・無効の表記ゆれ。大小文字と前後空白は無視する。ここに無い値は「不明」にして
# 警告する（真偽値として読めない値を False に丸めると、無効と未記入が混ざる）
_TRUE_TOKENS = frozenset({"true", "yes", "on", "1", "enabled", "有効"})
_FALSE_TOKENS = frozenset({"false", "no", "off", "0", "disabled", "無効"})

# 金額セルから取り除く通貨記号と桁区切り。円記号は含めず、`_YEN_RE` で明示的に解釈不能へ
# 倒す（金額は USD 建てなので、通貨の取り違えを金額として通さない。ingest と同じ規則）
_CURRENCY_CHARS = str.maketrans("", "", "$＄,")
_YEN_RE = re.compile(r"[¥￥]")

# 対象月と日付の形式。ASCII の数字だけを認め、全角数字と末尾の余分な文字を通さないため
# 照合には fullmatch を使う（`date.fromisoformat` は "20260801" や週日付も受けるので、
# 形式の判断はこちらで先に閉じる）
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# 取得日つきの正準なファイル名（種別 + 数字を含まない任意の語 + 末尾の取得日）。
# 拡張子を除いた名前の全体と照合する。語の部分は `\D`（全角も含めて数字でない文字）に
# する。ASCII の数字だけを拒むと、全角で書いた日付を語が飲み込んでしまう
_NAMED_DATE_RE = re.compile(
    r"(organization|users)(?:[-_]\D*)?[-_][0-9]{4}[-_][0-9]{2}[-_][0-9]{2}",
    re.IGNORECASE,
)


def _iso_date(text: str) -> dt.date | None:
    """YYYY-MM-DD の文字列を date にする（形式・暦のどちらかが外れれば None）。"""
    if not _ISO_DATE_RE.fullmatch(text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


# ------------------------------------------------------------------ セルの解釈


def _cell_text(cell: object) -> str | None:
    """セルを前後空白を除いた文字列にする。空欄・欠損・空白のみは None。"""
    if cell is None or (not isinstance(cell, str) and pd.isna(cell)):
        return None
    text = str(cell).strip()
    if text == "" or text.lower() == "nan":
        return None
    return text


def _parse_flag(cell: object, column: str) -> tuple[bool | None, str | None]:
    """有効・無効を解釈する。空欄は None（不明）、解釈できない値は None + 警告。"""
    text = _cell_text(cell)
    if text is None:
        return None, None
    token = text.lower()
    if token in _TRUE_TOKENS:
        return True, None
    if token in _FALSE_TOKENS:
        return False, None
    return None, f"{column} を有効・無効として解釈できません: {text!r}（不明として扱います）"


def _parse_amount(cell: object, column: str) -> tuple[float | None, str | None]:
    """金額を解釈する（"$" やカンマを許容）。負値・有限でない値・解釈不能は None + 警告。

    円記号を含むセルは解釈不能に倒す（USD 建ての金額として通すと桁が変わる）。数値として
    受ける字句は `ingest.is_number_text` に従う（数値変換そのものは "1_0" のような
    書き間違いを別の値として受けてしまうため、先に字句を検査する）。
    """
    text = _cell_text(cell)
    if text is None:
        return None, None
    unreadable = f"{column} を金額として解釈できません: {text!r}（不明として扱います）"
    if _YEN_RE.search(text):
        return None, unreadable
    stripped = text.translate(_CURRENCY_CHARS).strip()
    if not ingest.is_number_text(stripped):
        return None, unreadable
    try:
        value = float(stripped)
    except ValueError:
        return None, unreadable
    if not math.isfinite(value):
        return None, unreadable
    if value < 0:
        return None, f"{column} が負値です: {text!r}（不明として扱います）"
    return value, None


def _parse_count(cell: object, column: str) -> tuple[int | None, str | None]:
    """個数を非負整数として解釈する。小数・負値・解釈不能は None + 警告。

    float を経由すると桁の多い整数が近い値へ丸められ、正確に読めていない値をそのまま
    個数として扱ってしまう。Decimal で受けて、有限かつ整数であることを確かめてから
    int へ写す（"7.0" や "1E+2" のような書き方も整数を表すなら受ける）。数値として受ける
    字句は金額と同じ `ingest.is_number_text` に従う。
    """
    text = _cell_text(cell)
    if text is None:
        return None, None
    unreadable = f"{column} を整数として解釈できません: {text!r}（不明として扱います）"
    stripped = text.replace(",", "").strip()
    if not ingest.is_number_text(stripped):
        return None, unreadable
    try:
        value = Decimal(stripped)
    except (InvalidOperation, ValueError):
        return None, unreadable
    # 有限性を先に見る（signaling NaN は比較そのものが例外になる）
    if not value.is_finite() or value != value.to_integral_value():
        return None, unreadable
    if value < 0:
        return None, f"{column} が負値です: {text!r}（不明として扱います）"
    return int(value), None


def _parse_date(cell: object, column: str) -> tuple[dt.date | None, str | None]:
    """日付（YYYY-MM-DD）を解釈する。解釈できない値は None + 警告。"""
    text = _cell_text(cell)
    if text is None:
        return None, None
    value = _iso_date(text)
    if value is None:
        return None, (
            f"{column} を日付として解釈できません: {text!r}"
            "（YYYY-MM-DD で記入してください。不明として扱います）"
        )
    return value, None


def _parse_limit(cell: object, column: str) -> tuple[float | None, str | None]:
    """追加クレジット上限を解釈する（正数=上限あり / 0=無効 / inf=無制限 / None=不明）。

    解釈の規則は members-info の同名列と同じものを使う（`ingest.parse_credit_limit`）。
    そちらの「不明」は NaN だが、ここでは None へ写して V2 判定側の語彙に揃える。
    """
    value, warning = ingest.parse_credit_limit(_cell_text(cell))
    if warning is not None:
        return None, f"{column}: {warning}"
    return (None if math.isnan(value) else value), None


# ------------------------------------------------------- 値オブジェクトの検証


def _date_field(value: object, name: str, *, allow_none: bool = False) -> dt.date | None:
    """日付を検証する。時刻を持つ datetime は比較と表示が変わるため受けない。"""
    if value is None and allow_none:
        return None
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise TypeError(f"{name} には datetime.date が必要です: {type(value).__name__}")
    return value


def _amount_field(
    value: object, name: str, *, allow_infinite: bool = False
) -> float | None:
    """金額を検証して float へ写す（None は「不明」）。

    NaN は比較が常に偽になり判定を黙って変えるため拒否する（decision_v2 と同じ理由）。
    負の金額に対応する状態はここで扱う項目には無いので負値も拒否する。上限が無い
    追加クレジットだけ Infinity を「無制限」の表現として認める。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} には数値が必要です: {type(value).__name__}")
    result = float(value)
    if math.isnan(result) or (not allow_infinite and math.isinf(result)):
        raise ValueError(f"{name} に有限でない数値は指定できません: {value!r}")
    if result < 0.0:
        raise ValueError(f"{name} に負の値は指定できません: {value!r}")
    return result


def _count_field(value: object, name: str) -> int | None:
    """個数を検証する（None は「不明」）。負の購入数に対応する状態は無いので拒否する。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} には整数が必要です: {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} に負の値は指定できません: {value!r}")
    return value


def _flag_field(value: object, name: str) -> bool | None:
    """真偽値を検証する（None は「不明」）。"""
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"{name} には真偽値が必要です: {type(value).__name__}")


def _text_field(value: object, name: str, *, required: bool) -> str | None:
    """文字列を検証する。required の項目は空文字列を認めない。"""
    if value is None:
        if required:
            raise ValueError(f"{name} は必須です")
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} には文字列が必要です: {type(value).__name__}")
    if required and not value.strip():
        raise ValueError(f"{name} は必須です")
    return value


def _ascending_dates(items: Sequence, name: str) -> tuple:
    """取得日の昇順・重複なしを確かめる（as-of 選択が末尾を最新として引くため）。"""
    if not isinstance(items, tuple):
        raise TypeError(f"{name} には tuple が必要です: {type(items).__name__}")
    dates = [item.taken_on for item in items]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError(f"{name} は取得日の昇順・重複なしで渡してください: {dates}")
    return items


# ------------------------------------------------------------ 値オブジェクト


@dataclass(frozen=True)
class OrganizationSnapshot:
    """ある時点の組織の契約（`admin/organization-YYYY-MM-DD.csv` の1行）。

    taken_on はファイル名と `snapshot_date` 列が一致した取得日。collected_via は CSV の
    source 列（browser / manual / invoice 等。語彙は強制せず入力値を保持する）、source は
    由来ファイルの basename（絶対パスは持たせない。値を実行環境に依存させないため）。

    None は「不明」（未記入、または解釈できなかった値）で、0 や False とは区別する。
    org_credit_limit_usd の inf は「無制限」、0.0 は「無効」。

    billing_frequency は語彙を強制せず入力値を保持する（設計書 §7.2 の member_status と
    同じ規則。monthly / annual との比較が要る場合は利用側で正規化する）。数値・真偽値・
    日付の列とは扱いが違い、未知の語だからといって不明へ倒さない。
    """

    taken_on: dt.date
    standard_purchased: int | None
    premium_purchased: int | None
    billing_frequency: str | None
    renewal_date: dt.date | None
    standard_unit_price_usd: float | None
    premium_unit_price_usd: float | None
    org_credit_enabled: bool | None
    org_credit_limit_usd: float | None
    collected_via: str
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "taken_on", _date_field(self.taken_on, "taken_on"))
        object.__setattr__(
            self,
            "renewal_date",
            _date_field(self.renewal_date, "renewal_date", allow_none=True),
        )
        for name in ("standard_purchased", "premium_purchased"):
            object.__setattr__(self, name, _count_field(getattr(self, name), name))
        for name in ("standard_unit_price_usd", "premium_unit_price_usd"):
            object.__setattr__(self, name, _amount_field(getattr(self, name), name))
        object.__setattr__(
            self,
            "org_credit_limit_usd",
            _amount_field(
                self.org_credit_limit_usd, "org_credit_limit_usd", allow_infinite=True
            ),
        )
        object.__setattr__(
            self, "org_credit_enabled", _flag_field(self.org_credit_enabled, "org_credit_enabled")
        )
        object.__setattr__(
            self,
            "billing_frequency",
            _text_field(self.billing_frequency, "billing_frequency", required=False),
        )
        for name in ("collected_via", "source"):
            object.__setattr__(
                self, name, _text_field(getattr(self, name), name, required=True)
            )


@dataclass(frozen=True)
class UserCreditRecord:
    """1ユーザぶんの追加クレジット設定（`admin/users-YYYY-MM-DD.csv` の1行）。

    credit_limit_usd は正数が上限 κ、0.0 が無効、inf が無制限、None が不明。
    credit_enabled と credit_limit_usd は別の列で、片方だけ記入された表も受ける
    （どちらが正かを決めるのは判定側の責務）。collected_via は行ごとに持つ
    （テンプレートを部分的に埋めた表では取得元が行によって違いうる）。

    email は前後空白を除いて小文字へ揃えてから保持する（突き合わせの鍵なので、
    表記だけが違う2つの行が別人として並ばないようにする）。
    """

    email: str
    account_uuid: str | None
    credit_enabled: bool | None
    credit_limit_usd: float | None
    credit_mtd_usd: float | None
    collected_via: str

    def __post_init__(self) -> None:
        email = _text_field(self.email, "email", required=True)
        object.__setattr__(self, "email", email.strip().lower())
        object.__setattr__(
            self, "account_uuid", _text_field(self.account_uuid, "account_uuid", required=False)
        )
        object.__setattr__(
            self, "credit_enabled", _flag_field(self.credit_enabled, "credit_enabled")
        )
        object.__setattr__(
            self,
            "credit_limit_usd",
            _amount_field(self.credit_limit_usd, "credit_limit_usd", allow_infinite=True),
        )
        object.__setattr__(
            self, "credit_mtd_usd", _amount_field(self.credit_mtd_usd, "credit_mtd_usd")
        )
        object.__setattr__(
            self, "collected_via", _text_field(self.collected_via, "collected_via", required=True)
        )


@dataclass(frozen=True)
class UserCreditSnapshot:
    """ある時点のユーザ別追加クレジット設定（1ファイル分）。

    records は email の昇順で重複なし（同じ入力から常に同じ並びを返すため、並びと
    一意性を構築時に確かめる）。source は由来ファイルの basename。
    """

    taken_on: dt.date
    records: tuple[UserCreditRecord, ...]
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "taken_on", _date_field(self.taken_on, "taken_on"))
        object.__setattr__(self, "source", _text_field(self.source, "source", required=True))
        if not isinstance(self.records, tuple):
            raise TypeError(f"records には tuple が必要です: {type(self.records).__name__}")
        emails = [record.email for record in self.records]
        if emails != sorted(emails) or len(set(emails)) != len(emails):
            raise ValueError(f"records は email の昇順・重複なしで渡してください: {emails}")


@dataclass(frozen=True)
class AdminInputs:
    """`admin/` の全スナップショット（種別ごとに取得日の昇順）と警告。"""

    organization: tuple[OrganizationSnapshot, ...]
    users: tuple[UserCreditSnapshot, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "organization", _ascending_dates(self.organization, "organization")
        )
        object.__setattr__(self, "users", _ascending_dates(self.users, "users"))
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(w, str) for w in self.warnings
        ):
            raise TypeError("warnings には文字列の tuple が必要です")


@dataclass(frozen=True)
class AdminAsOf:
    """対象月の判定に使う as-of 選択の結果（該当が無い種別は None）。"""

    organization: OrganizationSnapshot | None
    users: UserCreditSnapshot | None
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.organization is not None and not isinstance(
            self.organization, OrganizationSnapshot
        ):
            raise TypeError("organization には OrganizationSnapshot が必要です")
        if self.users is not None and not isinstance(self.users, UserCreditSnapshot):
            raise TypeError("users には UserCreditSnapshot が必要です")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(w, str) for w in self.warnings
        ):
            raise TypeError("warnings には文字列の tuple が必要です")


# --------------------------------------------------------------- ファイルの発見


def _classify(name: str) -> str | None:
    """ファイル名から種別を決める（接頭辞。どちらでもなければ None）。"""
    lowered = name.lower()
    return next((kind for kind in _KINDS if lowered.startswith(kind)), None)


def _reject_same_date(directory: Path, kind: str, found: list[tuple[dt.date, Path]]) -> None:
    """同一種別・同一取得日のファイルを拒否する（どちらが正かを決められないため）。"""
    seen: dict[dt.date, Path] = {}
    for taken_on, path in found:
        if taken_on in seen:
            raise ValueError(
                f"{directory}: {kind} の取得日 {taken_on} の CSV が複数あります"
                f"（{seen[taken_on].name}, {path.name}）。"
                "どちらが正か判断できないため、その日のファイルを1つに絞ってください"
            )
        seen[taken_on] = path


def _is_canonical_name(path: Path) -> bool:
    """ファイル名が取得日つきの正準な形か（拡張子を除いた全体を1つの規則で見る）。

    規則は2つだけ:

    - 取得日は名前の末尾に置く。後ろに語が続く名前（`users-2026-08-01-final`）は、
      どの数字が取得日なのかが名前から決まらない
    - 種別と取得日の間に置ける語は数字を含まないものだけ（`users-manual-export-...` は
      可、`users-v2-...` は不可）。語に数字を許すと、期間の始まりや2つ目の日付を語として
      飲み込んでしまい（`users-2026-8-01-to-2026-08-31` の前半など）、末尾だけが取得日に
      見える名前が通る。数字は全角も含めて拒む（`users-v２-...` も不可。ASCII だけを
      拒んでも、全角で書いた日付が語として通れば同じことが起きる）

    この2つを1つの正規表現（`_NAMED_DATE_RE`）で見るので、桁を多く書いた名前
    （`users-12026-08-01`・`users-2026-08-011`）も日付を2つ並べた名前も同時に落ちる。
    日付そのものの妥当性（暦として存在する日か）は `ingest.file_period` が見る。

    種別を決める `_classify` とは役割が違う（あちらは organization と users のどちらの
    表かを決めるだけで、取得日の配置は見ない）。
    """
    return _NAMED_DATE_RE.fullmatch(path.stem) is not None


def _discover(directory: Path) -> tuple[dict[str, list[tuple[dt.date, Path]]], list[str]]:
    """`admin/` 直下の CSV を種別ごとに取得日の昇順で集める。

    ディレクトリが無い・CSV が無い場合は空（警告も出さない。admin を運用していない
    組織で毎回警告を出さないため）。読めないファイル名は黙って捨てず、除外した旨を
    警告に残す（書き間違いに気付けるようにする）。
    """
    entries: dict[str, list[tuple[dt.date, Path]]] = {kind: [] for kind in _KINDS}
    warnings: list[str] = []
    if not directory.is_dir():
        return entries, warnings
    for path in sorted(directory.glob("*.csv")):
        kind = _classify(path.name)
        if kind is None:
            warnings.append(
                f"admin: {path.name} は organization / users のどちらの CSV か"
                "判別できないため読み込みません"
            )
            continue
        try:
            period = ingest.file_period(path)
        except ValueError:
            period = None
        if period is None or period.kind != "date" or not _is_canonical_name(path):
            warnings.append(
                f"admin: {path.name} はファイル名から取得日（YYYY-MM-DD の単日）を"
                "読み取れないため読み込みません"
            )
            continue
        entries[kind].append((period.start, path))
    for kind, found in entries.items():
        _reject_same_date(directory, kind, found)
        found.sort(key=lambda item: (item[0], item[1].name))
    return entries, warnings


# ------------------------------------------------------------ ファイルの読み取り


def _candidates(canonical: str, alias_list: object) -> tuple[str, ...]:
    """正準列 canonical に写る、正規化済みヘッダの候補（並びは決定的）。

    照合の規則は `ingest.map_columns` と同じ（エイリアスと正準名そのもの）。同じ規則を
    2度書かないよう、ヘッダの曖昧さの検査もこの1箇所を使う。
    """
    aliases = alias_list if isinstance(alias_list, list) else []
    return tuple(sorted(
        {ingest.normalize_header(alias) for alias in aliases}
        | {ingest.normalize_header(canonical)}
    ))


def _raw_header(path: Path) -> list[str]:
    """CSV の先頭行を、列名を畳まずそのまま読む。

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
        if row.empty:
            return []
        return [str(value) for value in row.iloc[0]]
    raise ValueError(f"{path}: 文字コードを判別できません（utf-8 / cp932 を試行）")


def _reject_overlapping_aliases(section: str, aliases: dict) -> None:
    """1つのヘッダが2つの正準列の候補になっている設定を拒否する。

    正準名へ写す対応は正準列ごとに決めるため、同じヘッダが2つの正準列の候補に入って
    いると、写る先が定義の並び順で決まり、もう一方の正準列は黙って NA になる。入力の
    中身に依らない設定側の誤りなので、突き合わせより前に止める。
    """
    owner: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        for candidate in _candidates(canonical, alias_list):
            other = owner.setdefault(candidate, canonical)
            if other != canonical:
                raise ValueError(
                    f"columns.{section}: ヘッダ {candidate!r} が {other} と {canonical} の"
                    "両方の候補になっています。どちらの列に写すか決まらないため、"
                    "片方のエイリアスから取り除いてください"
                )


def _reject_ambiguous_headers(path: Path, headers: list[str], aliases: dict) -> None:
    """1つの正準列に対応する実ヘッダが2つ以上ある表を中止する。

    正準名へ写すのは最初に一致したヘッダだけなので、`Snapshot Date` と `snapshot_date` の
    ように両方が並ぶ表では、写した後に同名の列が2つ残ってセルの取得が曖昧になる（並びが
    逆なら片方が黙って捨てられる）。完全に同名のヘッダが2つある表も、読み込みの改名で
    見た目には1つずつになるだけで同じことが起きる。どちらが正かは決められないので、
    列名を挙げて止める。

    見るのは正準列に写るヘッダだけで、読まない列の重複（`Comment,Comment` や、表計算が
    行末に付ける空のヘッダ）は結果に影響しないので止めない。
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


def _read_table(
    path: Path, section: str, optional: tuple[str, ...], cfg: dict
) -> tuple[pd.DataFrame, list[str]]:
    """admin CSV を1つ読み、カラム名を正準名へ写す。

    ID の先頭ゼロと数値の表記をそのまま保つため文字列で読み、値の解釈はセル単位の
    パーサに任せる。任意列は欠損時に NA で補い、「任意カラムなし」の警告は出さない。
    """
    df = ingest.read_csv(path, dtype=str)
    _reject_overlapping_aliases(section, cfg["columns"][section])
    _reject_ambiguous_headers(path, _raw_header(path), cfg["columns"][section])
    return ingest.map_columns(
        df,
        cfg["columns"][section],
        required=ingest.REQUIRED_COLUMNS[section],
        source=path,
        fill_na_columns=optional,
    )


def _check_snapshot_date(
    path: Path, cell: object, taken_on: dt.date, where: str = ""
) -> None:
    """`snapshot_date` 列がファイル名の取得日と一致することを確かめる。

    取得日は as-of 選択の鍵で、ファイルの取り違えや写し間違いは判定の材料を丸ごと別の
    時点のものに変える。読めない値・食い違う値はここで止める。
    """
    prefix = f"{where}の " if where else ""
    text = _cell_text(cell)
    if text is None:
        raise ValueError(
            f"{path}: {prefix}snapshot_date が空です"
            f"（ファイル名の取得日 {taken_on} と同じ日付を記入してください）"
        )
    value = _iso_date(text)
    if value is None:
        raise ValueError(
            f"{path}: {prefix}snapshot_date を日付として解釈できません: {text!r}"
            "（YYYY-MM-DD で記入してください）"
        )
    if value != taken_on:
        raise ValueError(
            f"{path}: {prefix}snapshot_date {value} がファイル名の取得日 {taken_on} と"
            "一致しません（取り違えの可能性があるため中止します）"
        )


def _required_source(path: Path, cell: object, where: str = "") -> str:
    """`source` 列（取得元）を必須として読む。語彙は強制せず入力値を保持する。"""
    text = _cell_text(cell)
    if text is None:
        prefix = f"{where}の " if where else ""
        raise ValueError(
            f"{path}: {prefix}source が空です"
            "（browser / manual / invoice など取得元を記入してください）"
        )
    return text


def _read_organization(
    path: Path, taken_on: dt.date, cfg: dict
) -> tuple[OrganizationSnapshot, list[str]]:
    """organization CSV を1つ読む（1ファイル1行。行数が違えば中止）。"""
    df, warnings = _read_table(path, "admin_organization", ORGANIZATION_OPTIONAL_COLUMNS, cfg)
    if len(df) != 1:
        raise ValueError(
            f"{path}: organization スナップショットは1ファイル1行です"
            f"（データ行 {len(df)} 行）。時点ごとにファイルを分けてください"
        )
    row = df.iloc[0]
    _check_snapshot_date(path, row.get("snapshot_date"), taken_on)

    def take(parse, column: str):
        value, warning = parse(row.get(column), column)
        if warning is not None:
            warnings.append(f"admin: {path.name}: {warning}")
        return value

    snapshot = OrganizationSnapshot(
        taken_on=taken_on,
        standard_purchased=take(_parse_count, "standard_purchased"),
        premium_purchased=take(_parse_count, "premium_purchased"),
        # 語彙を強制せず入力値を保持する（member_status と同じ規則。正規化は利用側）
        billing_frequency=_cell_text(row.get("billing_frequency")),
        renewal_date=take(_parse_date, "renewal_date"),
        standard_unit_price_usd=take(_parse_amount, "standard_unit_price_usd"),
        premium_unit_price_usd=take(_parse_amount, "premium_unit_price_usd"),
        org_credit_enabled=take(_parse_flag, "org_credit_enabled"),
        org_credit_limit_usd=take(_parse_limit, "org_credit_limit_usd"),
        collected_via=_required_source(path, row.get("source")),
        source=path.name,
    )
    return snapshot, warnings


def _read_users(
    path: Path, taken_on: dt.date, cfg: dict
) -> tuple[UserCreditSnapshot, list[str]]:
    """users CSV を1つ読む（email は必須・一意。データ行が無い場合は警告して空で返す）。"""
    df, warnings = _read_table(path, "admin_users", USERS_OPTIONAL_COLUMNS, cfg)
    if df.empty:
        warnings.append(
            f"admin: {path.name}: データ行がありません"
            "（テンプレートのまま置かれている可能性があります）"
        )
        return UserCreditSnapshot(taken_on=taken_on, records=(), source=path.name), warnings

    records: list[UserCreditRecord] = []
    seen: dict[str, int] = {}
    for number, (_, row) in enumerate(df.iterrows(), start=1):
        where = f"{number} 行目"
        _check_snapshot_date(path, row.get("snapshot_date"), taken_on, where=where)
        text = _cell_text(row.get("email"))
        if text is None:
            raise ValueError(
                f"{path}: {where}の email が空です（1メール1行で記入してください）"
            )
        email = text.lower()
        if email in seen:
            raise ValueError(
                f"{path}: email {email!r} の行が複数あります"
                f"（{seen[email]} 行目と {number} 行目）。1メール1行に整理してください"
            )
        seen[email] = number

        def take(parse, column: str, row=row, email=email):
            value, warning = parse(row.get(column), column)
            if warning is not None:
                warnings.append(f"admin: {path.name}: {email}: {warning}")
            return value

        records.append(
            UserCreditRecord(
                email=email,
                account_uuid=_cell_text(row.get("account_uuid")),
                credit_enabled=take(_parse_flag, "credit_enabled"),
                credit_limit_usd=take(_parse_limit, "credit_limit_usd"),
                credit_mtd_usd=take(_parse_amount, "credit_mtd_usd"),
                collected_via=_required_source(path, row.get("source"), where=where),
            )
        )
    records.sort(key=lambda record: record.email)
    return (
        UserCreditSnapshot(taken_on=taken_on, records=tuple(records), source=path.name),
        warnings,
    )


# --------------------------------------------------------------------- 公開 API


def load_admin_inputs(input_dir: Path, cfg: dict) -> AdminInputs:
    """`input_dir/admin/` の全スナップショットを読む（入力が無ければ空の結果）。

    種別ごとに取得日の昇順で返す。対象月では絞らない（月内の推移を見るには全時点が
    必要で、判定に使う1点を決めるのは `as_of` の役割）。
    """
    entries, warnings = _discover(Path(input_dir) / ADMIN_SUBDIR)
    organization: list[OrganizationSnapshot] = []
    for taken_on, path in entries[_ORGANIZATION]:
        snapshot, warns = _read_organization(path, taken_on, cfg)
        organization.append(snapshot)
        warnings.extend(warns)
    users: list[UserCreditSnapshot] = []
    for taken_on, path in entries[_USERS]:
        user_snapshot, warns = _read_users(path, taken_on, cfg)
        users.append(user_snapshot)
        warnings.extend(warns)
    return AdminInputs(
        organization=tuple(organization), users=tuple(users), warnings=tuple(warnings)
    )


def _month_end(month: str) -> dt.date:
    """対象月（YYYY-MM）の末日。"""
    year, mon = (int(part) for part in month.split("-"))
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])


_Snapshot = TypeVar("_Snapshot", OrganizationSnapshot, UserCreditSnapshot)


def _select(
    snapshots: Sequence[_Snapshot], month: str, month_end: dt.date
) -> tuple[_Snapshot | None, str | None]:
    """対象月の月末以前で最新を選ぶ。無ければ最古へフォールバックして警告する。"""
    on_or_before = [s for s in snapshots if s.taken_on <= month_end]
    if on_or_before:
        return on_or_before[-1], None
    if not snapshots:
        return None, None
    oldest = snapshots[0]
    return oldest, (
        f"admin: {month} 月末以前のスナップショットが無いため最古の {oldest.source} を使用。"
        "対象月当時の設定と異なる可能性があります"
    )


def as_of(inputs: AdminInputs, month: str) -> AdminAsOf:
    """対象月の判定に使う1時点を種別ごとに独立に選ぶ。

    organization と users は取得日が揃っていなくてよい（別の表で、更新の頻度も違う）。
    片方だけがある場合はその片方だけを返す。
    """
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        raise ValueError(f"month には YYYY-MM 形式が必要です: {month!r}")
    month_end = _month_end(month)
    organization, org_warning = _select(inputs.organization, month, month_end)
    users, users_warning = _select(inputs.users, month, month_end)
    warnings = [w for w in (org_warning, users_warning) if w is not None]
    return AdminAsOf(organization=organization, users=users, warnings=tuple(warnings))
