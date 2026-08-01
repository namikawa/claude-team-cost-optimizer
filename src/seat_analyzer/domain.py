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


class Severity(enum.StrEnum):
    """issueの深刻度。宣言順がそのまま深刻な順を表す。

    errorとwarningの2段階のみとし、中間段階（info等）は設けない。
    """

    ERROR = "error"
    WARNING = "warning"


class IssueCode(enum.StrEnum):
    """構造化issueの確定語彙。値は名前と同一で、機械可読なキーとして扱う。"""

    # 入力
    MISSING_SPEND = "MISSING_SPEND"
    MISSING_MEMBERS = "MISSING_MEMBERS"
    PARTIAL_MONTH = "PARTIAL_MONTH"
    MISSING_HISTORY_MONTH = "MISSING_HISTORY_MONTH"
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    NUMERIC_PARSE_FAILED = "NUMERIC_PARSE_FAILED"

    # Identity
    IDENTITY_EMAIL_FALLBACK = "IDENTITY_EMAIL_FALLBACK"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    GITHUB_MAPPING_MISSING = "GITHUB_MAPPING_MISSING"
    GITHUB_MAPPING_DUPLICATE = "GITHUB_MAPPING_DUPLICATE"

    # Seat/credit
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
    where = f"scope[{key!r}]" if index is None else f"scope[{key!r}][{index}]"
    # boolはintのサブクラスなので、数値としての検査より先に通す
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}に有限でない数値は指定できません: {value!r}")
        return value
    raise TypeError(f"{where}にはスカラー値が必要です: {type(value).__name__}")


def _normalize_scope(scope: object) -> Mapping[str, _NormalizedValue]:
    """scopeを検証し、変更不能な正準表現へ変換する。

    listとtupleはtupleへ揃え、辞書のネスト・set・非スカラー要素は拒否する。
    """
    if not isinstance(scope, Mapping):
        raise TypeError(f"scopeにはマッピングが必要です: {type(scope).__name__}")

    normalized: dict[str, _NormalizedValue] = {}
    for key, value in scope.items():
        if not isinstance(key, str):
            raise TypeError(
                f"scopeのキーはstrである必要があります: {key!r} ({type(key).__name__})"
            )
        if isinstance(value, (list, tuple)):
            normalized[key] = tuple(
                _normalize_scalar(item, key=key, index=index)
                for index, item in enumerate(value)
            )
        else:
            normalized[key] = _normalize_scalar(value, key=key)
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class QualityIssue:
    """機械可読な品質issue1件。

    scopeは影響範囲（件数・対象email・stable ID等）を表す不変のマッピングで、
    値はスカラーかスカラーの組に限る。構築時に検証と正準化を行う。
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
        object.__setattr__(self, "scope", _normalize_scope(self.scope))

    def __hash__(self) -> int:
        # frozenな値オブジェクトとして集合・辞書キーに使えるようにする。
        # scopeのマッピング自体はハッシュ不可のため、正準化した組で代用する
        return hash(
            (self.severity, self.code, self.message, tuple(sorted(self.scope.items())))
        )
