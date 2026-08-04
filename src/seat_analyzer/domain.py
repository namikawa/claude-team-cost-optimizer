"""品質issueの語彙（severity・code）と値オブジェクトを定義する純粋なドメイン。

ここではissueの表現だけを扱い、検出・整形・出力は担当しない。

QualityIssue.messageは決定的な文字列に限る。タイムスタンプ・乱数・実行環境依存値
（絶対パス・ホスト名等）を含めない。同じ入力からは常に同じ文字列になることを、
テストと差分レビューの前提とする。
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@enum.unique
class Severity(enum.StrEnum):
    """issueの深刻度。宣言順がそのまま深刻な順を表す。

    errorとwarningの2段階のみとし、中間段階（info等）は設けない。
    """

    ERROR = "error"
    WARNING = "warning"


@enum.unique
class IssueCode(enum.StrEnum):
    """構造化issueの確定語彙。値は名前と同一で、機械可読なキーとして扱う。"""

    # 入力
    MISSING_SPEND = "MISSING_SPEND"
    MISSING_MEMBERS = "MISSING_MEMBERS"
    PARTIAL_MONTH = "PARTIAL_MONTH"
    MISSING_HISTORY_MONTH = "MISSING_HISTORY_MONTH"
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    NUMERIC_PARSE_FAILED = "NUMERIC_PARSE_FAILED"
    MEMBER_ROW_MISSING = "MEMBER_ROW_MISSING"

    # Identity
    IDENTITY_EMAIL_FALLBACK = "IDENTITY_EMAIL_FALLBACK"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    GITHUB_MAPPING_MISSING = "GITHUB_MAPPING_MISSING"
    GITHUB_MAPPING_DUPLICATE = "GITHUB_MAPPING_DUPLICATE"

    # Seat/credit
    SEAT_TYPE_UNKNOWN = "SEAT_TYPE_UNKNOWN"
    UNASSIGNED_WITH_USAGE = "UNASSIGNED_WITH_USAGE"
    SEAT_CHANGE_DETECTED = "SEAT_CHANGE_DETECTED"
    RECENT_SEAT_CHANGE = "RECENT_SEAT_CHANGE"
    CREDIT_SETTING_UNKNOWN = "CREDIT_SETTING_UNKNOWN"
    ADMIN_SNAPSHOT_STALE = "ADMIN_SNAPSHOT_STALE"

    # Browser
    BROWSER_LOGIN_REQUIRED = "BROWSER_LOGIN_REQUIRED"
    ADMIN_PAGE_CHANGED = "ADMIN_PAGE_CHANGED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    DUPLICATE_DOWNLOAD = "DUPLICATE_DOWNLOAD"

    # GitHub
    GH_NOT_AUTHENTICATED = "GH_NOT_AUTHENTICATED"
    GH_ORG_NOT_ACCESSIBLE = "GH_ORG_NOT_ACCESSIBLE"
    GH_PERMISSION_INCOMPLETE = "GH_PERMISSION_INCOMPLETE"
    GH_RATE_LIMITED = "GH_RATE_LIMITED"
    GH_PARTIAL_RESULT = "GH_PARTIAL_RESULT"

    # Policy
    PROHIBITED_PRODUCT_OBSERVED = "PROHIBITED_PRODUCT_OBSERVED"
    CAPACITY_SIGNAL_UNAVAILABLE = "CAPACITY_SIGNAL_UNAVAILABLE"


# scopeに置ける値。スカラーと、スカラーのみからなる列挙に限る
ScopeScalar = str | int | float | bool | None
ScopeValue = ScopeScalar | list[ScopeScalar] | tuple[ScopeScalar, ...]

_NormalizedValue = ScopeScalar | tuple[ScopeScalar, ...]


def _normalize_scalar(
    value: object,
    *,
    key: str,
    index: int | None = None,
) -> ScopeScalar:
    """スカラー値を検証し、組み込み型の値へ写して返す。

    許可型のサブクラス（ハッシュ不可のint等）をそのまま保持すると等価性や
    ハッシュが定義側の実装に左右されるため、必ず組み込み型へコピーする。
    """
    where = f"scope[{key!r}]" if index is None else f"scope[{key!r}][{index}]"
    if value is None:
        return None
    # boolはintのサブクラスなので、数値としての検査より先に判定する
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        # サブクラスの__float__が別の値を返し得るため、変換後の値を検査して格納する
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{where}に有限でない数値は指定できません: {result!r}")
        return result
    if isinstance(value, str):
        return str(value)
    raise TypeError(f"{where}にはスカラー値が必要です: {type(value).__name__}")


def _normalize_scope(scope: object) -> Mapping[str, _NormalizedValue]:
    """scopeを検証し、変更不能な正準表現へ変換する。

    listとtupleはtupleへ揃え、辞書のネスト・set・非スカラー要素は拒否する。
    """
    if not isinstance(scope, Mapping):
        raise TypeError(f"scopeにはマッピングが必要です: {type(scope).__name__}")

    normalized: dict[str, _NormalizedValue] = {}
    for raw_key, value in scope.items():
        if not isinstance(raw_key, str):
            raise TypeError(
                f"scopeのキーはstrである必要があります: "
                f"{raw_key!r} ({type(raw_key).__name__})"
            )
        key = str(raw_key)
        if isinstance(value, (list, tuple)):
            normalized[key] = tuple(
                _normalize_scalar(item, key=key, index=index)
                for index, item in enumerate(value)
            )
        else:
            normalized[key] = _normalize_scalar(value, key=key)
    return MappingProxyType(normalized)


def _scalar_key(value: ScopeScalar) -> tuple[str, str]:
    """スカラーの正準キー。型タグを付けてTrue・1・1.0・"1"を区別する。"""
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, int):
        return ("int", repr(value))
    if isinstance(value, float):
        # reprは0.0と-0.0を区別する
        return ("float", repr(value))
    return ("str", value)


def _value_key(value: _NormalizedValue) -> tuple[str, object]:
    if isinstance(value, tuple):
        return ("list", tuple(_scalar_key(item) for item in value))
    return ("scalar", _scalar_key(value))


def _scope_key(scope: Mapping[str, _NormalizedValue]) -> tuple:
    """scope全体の正準キー。キーの辞書順で並べ、値は型を区別して表現する。"""
    return tuple((key, _value_key(value)) for key, value in sorted(scope.items()))


@dataclass(frozen=True, eq=False)
class QualityIssue:
    """機械可読な品質issue1件。

    scopeは影響範囲（件数・対象email・stable ID等）を表す不変のマッピングで、
    値はスカラーかスカラーの組に限る。構築時に検証と正準化を行う。

    等価性とハッシュは正準表現で定義する（2つのissueが等価であることと、
    直列化した表現が一致することを同義にする）。したがって値の型が違えば
    別のissueとして扱い、True・1・1.0や0.0・-0.0は区別する。
    """

    severity: Severity
    code: IssueCode
    message: str
    scope: Mapping[str, ScopeValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.severity, Severity):
            raise TypeError(
                f"severityにはSeverityが必要です: {type(self.severity).__name__}"
            )
        if not isinstance(self.code, IssueCode):
            raise TypeError(f"codeにはIssueCodeが必要です: {type(self.code).__name__}")
        if not isinstance(self.message, str):
            raise TypeError(f"messageにはstrが必要です: {type(self.message).__name__}")
        object.__setattr__(self, "message", str(self.message))
        object.__setattr__(self, "scope", _normalize_scope(self.scope))

    def _key(self) -> tuple:
        """等価性・ハッシュの基準となる正準キー。"""
        return (
            self.severity.value,
            self.code.value,
            self.message,
            _scope_key(self.scope),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, QualityIssue):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        # frozenな値オブジェクトとして集合・辞書キーに使えるようにする
        # （scopeのマッピング自体はハッシュ不可のため正準キーで代用する）
        return hash(self._key())
