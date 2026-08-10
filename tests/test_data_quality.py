import dataclasses
import json
import random
from pathlib import Path

import pytest

from seat_analyzer.data_quality import (
    _reason,
    issue_to_dict,
    issues_to_canonical_json,
    issues_to_json,
    sort_issues,
)
from seat_analyzer.domain import IssueCode, QualityIssue, Severity

from .conftest import requires_symlink

EXPECTED_CODES = {
    # 入力
    "MISSING_SPEND",
    "MISSING_MEMBERS",
    "PARTIAL_MONTH",
    "MISSING_HISTORY_MONTH",
    "UNKNOWN_MODEL",
    "NUMERIC_PARSE_FAILED",
    "MEMBER_ROW_MISSING",
    # Identity
    "IDENTITY_EMAIL_FALLBACK",
    "IDENTITY_CONFLICT",
    "GITHUB_MAPPING_MISSING",
    "GITHUB_MAPPING_DUPLICATE",
    # Seat/credit
    "SEAT_TYPE_UNKNOWN",
    "UNASSIGNED_WITH_USAGE",
    "SEAT_CHANGE_DETECTED",
    "RECENT_SEAT_CHANGE",
    "CREDIT_SETTING_UNKNOWN",
    "ADMIN_SNAPSHOT_STALE",
    # Browser
    "BROWSER_LOGIN_REQUIRED",
    "ADMIN_PAGE_CHANGED",
    "DOWNLOAD_FAILED",
    "DUPLICATE_DOWNLOAD",
    # GitHub
    "GH_NOT_AUTHENTICATED",
    "GH_ORG_NOT_ACCESSIBLE",
    "GH_PERMISSION_INCOMPLETE",
    "GH_RATE_LIMITED",
    "GH_PARTIAL_RESULT",
    # Policy
    "PROHIBITED_PRODUCT_OBSERVED",
    "CAPACITY_SIGNAL_UNAVAILABLE",
}


def test_issue_code_vocabulary_is_fixed():
    # __members__はaliasも列挙するため、別名の紛れ込みも検出できる
    members = IssueCode.__members__
    assert set(members) == EXPECTED_CODES
    assert len(members) == 28
    assert len(EXPECTED_CODES) == 28


def test_issue_code_value_equals_name():
    for name, member in IssueCode.__members__.items():
        assert name == member.name
        assert name == member.value


def test_severity_has_only_error_and_warning():
    members = Severity.__members__
    assert list(members) == ["ERROR", "WARNING"]
    assert len(members) == 2
    for name, member in members.items():
        assert name == member.name
    assert [severity.value for severity in Severity] == ["error", "warning"]


def _issue(**kwargs):
    params = {
        "severity": Severity.WARNING,
        "code": IssueCode.PARTIAL_MONTH,
        "message": "部分月のデータです",
    }
    params.update(kwargs)
    return QualityIssue(**params)


def test_scope_is_optional_and_defaults_to_empty():
    assert dict(_issue().scope) == {}


def test_scope_rejects_nested_dict():
    with pytest.raises(TypeError) as excinfo:
        _issue(scope={"detail": {"count": 1}})

    assert "detail" in str(excinfo.value)
    assert "dict" in str(excinfo.value)


def test_scope_rejects_set():
    with pytest.raises(TypeError) as excinfo:
        _issue(scope={"emails": {"user1@example.com"}})

    assert "emails" in str(excinfo.value)
    assert "set" in str(excinfo.value)


def test_scope_rejects_non_scalar_element_in_list():
    with pytest.raises(TypeError) as excinfo:
        _issue(scope={"emails": ["user1@example.com", ["user2@example.com"]]})

    assert "emails" in str(excinfo.value)
    assert "list" in str(excinfo.value)


def test_scope_rejects_non_str_key():
    with pytest.raises(TypeError) as excinfo:
        _issue(scope={1: "one"})

    assert "int" in str(excinfo.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_scope_rejects_non_finite_float(value):
    with pytest.raises(ValueError) as excinfo:
        _issue(scope={"ratio": value})

    assert "ratio" in str(excinfo.value)


def test_scope_rejects_non_finite_float_in_list():
    with pytest.raises(ValueError) as excinfo:
        _issue(scope={"ratios": [0.5, float("nan")]})

    assert "ratios" in str(excinfo.value)


class _LyingFloat(float):
    """基底値は有限だが、変換フックが非有限値を返すfloatサブクラス。"""

    def __float__(self):
        return float("nan")


def test_scope_rejects_non_finite_float_from_conversion_hook():
    with pytest.raises(ValueError) as excinfo:
        _issue(scope={"ratio": _LyingFloat(2.5)})

    assert "ratio" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        _issue(scope={"ratios": [0.5, _LyingFloat(2.5)]})

    assert "ratios" in str(excinfo.value)


def test_scope_accepts_scalars_including_bool():
    issue = _issue(scope={"count": 3, "ratio": 0.5, "ok": True, "note": None})

    assert dict(issue.scope) == {"count": 3, "ratio": 0.5, "ok": True, "note": None}


def test_severity_and_code_must_be_enum_members():
    with pytest.raises(TypeError):
        _issue(severity="warning")
    with pytest.raises(TypeError):
        _issue(code="PARTIAL_MONTH")


def test_issue_is_frozen_and_scope_is_immutable():
    issue = _issue(scope={"emails": ["user1@example.com", "user2@example.com"]})

    assert dataclasses.is_dataclass(issue)
    assert issue.__dataclass_params__.frozen is True
    # listはtupleへ正準化される
    assert issue.scope["emails"] == ("user1@example.com", "user2@example.com")

    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.message = "書き換え"
    with pytest.raises(TypeError):
        issue.scope["emails"] = ()
    with pytest.raises(TypeError):
        del issue.scope["emails"]


def test_source_mapping_change_does_not_affect_issue():
    source = {"emails": ["user1@example.com"]}
    issue = _issue(scope=source)
    source["emails"].append("user2@example.com")
    source["extra"] = 1

    assert dict(issue.scope) == {"emails": ("user1@example.com",)}


def test_equal_issues_are_hashable_and_equal():
    left = _issue(scope={"count": 2, "emails": ["user1@example.com"]})
    right = _issue(scope={"emails": ("user1@example.com",), "count": 2})

    assert left == right
    assert hash(left) == hash(right)
    assert len({left, right}) == 1


def _typed_scalar_issues(values):
    return [
        QualityIssue(
            severity=Severity.WARNING,
            code=IssueCode.NUMERIC_PARSE_FAILED,
            message="値の型で区別する",
            scope={"value": value},
        )
        for value in values
    ]


def test_scalar_types_are_distinguished_by_equality():
    true_issue, one_issue, float_issue = _typed_scalar_issues([True, 1, 1.0])

    assert true_issue != one_issue
    assert one_issue != float_issue
    assert true_issue != float_issue
    assert len({true_issue, one_issue, float_issue}) == 3


def test_dedup_by_set_keeps_json_deterministic():
    first = set(_typed_scalar_issues([True, 1, 1.0]))
    second = set(_typed_scalar_issues([1.0, 1, True]))

    text = issues_to_json(sort_issues(first))

    assert text == issues_to_json(sort_issues(second))
    # 型を明示して比較する（True == 1 == 1.0 のため値だけの比較では退行を検出できない）。
    # 並びはscopeのJSON文字列の辞書順で決まる（1.0 → 1 → true）
    decoded = [item["scope"]["value"] for item in json.loads(text)]
    assert [(type(value), value) for value in decoded] == [
        (float, 1.0),
        (int, 1),
        (bool, True),
    ]


def test_signed_zero_is_distinguished():
    positive, negative = _typed_scalar_issues([0.0, -0.0])

    assert positive != negative
    assert issues_to_json([positive]) != issues_to_json([negative])
    assert '"value": -0.0' in issues_to_json([negative])


class _UnhashableInt(int):
    __hash__ = None


class _TaggedStr(str):
    pass


def test_subclass_values_are_copied_to_builtin_types():
    issue = QualityIssue(
        severity=Severity.WARNING,
        code=IssueCode.NUMERIC_PARSE_FAILED,
        message=_TaggedStr("サブクラスのメッセージ"),
        scope={
            _TaggedStr("count"): _UnhashableInt(3),
            "values": [_UnhashableInt(1), _TaggedStr("x")],
        },
    )

    assert type(issue.message) is str
    assert [type(key) for key in issue.scope] == [str, str]
    assert type(issue.scope["count"]) is int
    assert [type(item) for item in issue.scope["values"]] == [int, str]
    # 組み込み型へ写しているので、ハッシュとJSON化が定義側の実装に左右されない
    assert isinstance(hash(issue), int)
    assert json.loads(issues_to_json([issue]))[0]["scope"] == {
        "count": 3,
        "values": [1, "x"],
    }


def test_issue_to_dict_key_order_and_scope_sorting():
    issue = _issue(
        severity=Severity.ERROR,
        code=IssueCode.IDENTITY_CONFLICT,
        scope={"emails": ["user2@example.com"], "affected": 1},
    )

    payload = issue_to_dict(issue)

    assert list(payload) == ["severity", "code", "message", "scope"]
    assert payload["severity"] == "error"
    assert payload["code"] == "IDENTITY_CONFLICT"
    assert list(payload["scope"]) == ["affected", "emails"]
    # tupleはJSON化のためlistへ戻す
    assert payload["scope"]["emails"] == ["user2@example.com"]


def _sample_issues(reverse_scope_keys=False):
    def scope(items):
        pairs = list(items.items())
        if reverse_scope_keys:
            pairs.reverse()
        return dict(pairs)

    return [
        QualityIssue(
            severity=Severity.WARNING,
            code=IssueCode.PARTIAL_MONTH,
            message="部分月のデータです",
            scope=scope({"month": "2026-06", "observed_days": 13}),
        ),
        QualityIssue(
            severity=Severity.ERROR,
            code=IssueCode.MISSING_SPEND,
            message="スペンドレポートがありません",
            scope=scope({"month": "2026-06", "org": "org-a"}),
        ),
        QualityIssue(
            severity=Severity.WARNING,
            code=IssueCode.PARTIAL_MONTH,
            message="部分月のデータです",
            scope=scope({"month": "2026-05", "observed_days": 20}),
        ),
        QualityIssue(
            severity=Severity.ERROR,
            code=IssueCode.IDENTITY_CONFLICT,
            message="Identityが競合しています",
            scope=scope({"affected": 2, "subject_ids": ["account:a1", "account:a2"]}),
        ),
    ]


def test_json_output_is_byte_identical_regardless_of_input_order():
    first = _sample_issues()
    second = list(reversed(_sample_issues(reverse_scope_keys=True)))

    assert issues_to_json(sort_issues(first)) == issues_to_json(sort_issues(second))


def test_canonical_json_is_identical_regardless_of_input_order():
    first = _sample_issues()
    second = list(reversed(_sample_issues(reverse_scope_keys=True)))

    # 呼び出し側が整列しなくても、正準出力APIは入力順に依存しない
    assert issues_to_canonical_json(first) == issues_to_canonical_json(second)
    assert issues_to_canonical_json(first) == issues_to_json(sort_issues(first))


def test_issues_to_json_preserves_input_order():
    ordered = sort_issues(_sample_issues())
    reversed_issues = list(reversed(ordered))

    payload = json.loads(issues_to_json(reversed_issues))

    assert [item["code"] for item in payload] == [
        issue.code.value for issue in reversed_issues
    ]
    assert payload == [issue_to_dict(issue) for issue in reversed_issues]
    assert payload != json.loads(issues_to_json(ordered))


def test_sort_issues_is_stable_against_shuffling():
    expected = sort_issues(_sample_issues())
    rng = random.Random(0)

    for _ in range(10):
        shuffled = _sample_issues()
        rng.shuffle(shuffled)
        assert sort_issues(shuffled) == expected


def test_sort_issues_orders_error_before_warning():
    ordered = sort_issues(_sample_issues())

    assert [issue.severity for issue in ordered] == [
        Severity.ERROR,
        Severity.ERROR,
        Severity.WARNING,
        Severity.WARNING,
    ]
    # 同じseverityではcodeの文字列順、同じcodeではmessage・scopeの順になる
    assert [issue.code.value for issue in ordered] == [
        "IDENTITY_CONFLICT",
        "MISSING_SPEND",
        "PARTIAL_MONTH",
        "PARTIAL_MONTH",
    ]
    assert [issue.scope["month"] for issue in ordered[2:]] == ["2026-05", "2026-06"]


def test_json_keeps_japanese_message_unescaped():
    text = issues_to_json([_issue()])

    assert "部分月のデータです" in text
    assert "\\u" not in text


def test_identity_conflict_scope_round_trips_through_json():
    issue = QualityIssue(
        severity=Severity.ERROR,
        code=IssueCode.IDENTITY_CONFLICT,
        message="同一identityに複数のstable IDがあります",
        scope={
            "affected_count": 2,
            "emails": ["user1@example.com", "user2@example.com"],
            "subject_ids": ["account:a1", "user:u1"],
        },
    )

    payload = json.loads(issues_to_json([issue]))

    assert payload == [
        {
            "severity": "error",
            "code": "IDENTITY_CONFLICT",
            "message": "同一identityに複数のstable IDがあります",
            "scope": {
                "affected_count": 2,
                "emails": ["user1@example.com", "user2@example.com"],
                "subject_ids": ["account:a1", "user:u1"],
            },
        }
    ]


# --- 例外メッセージの正規化（doctor の message 決定性） ---


@pytest.mark.parametrize(
    ("input_dir", "text"),
    [
        (".", "data.csv: 列がありません"),
        ("a", "cannot parse header"),
        ("input", "spend.csv: 実ファイルのヘッダ: ['input tokens']"),
        ("入力", "入力ディレクトリの扱いを確認してください"),
    ],
)
def test_reason_keeps_relative_input_dir_text_intact(input_dir, text):
    # 相対指定はそれ自体が実行環境に依存しないため置換しない
    # （素朴な部分文字列置換だと無関係な語・ピリオドまで壊れる）
    assert _reason(ValueError(text), Path(input_dir)) == text


def test_reason_relativizes_absolute_paths(tmp_path):
    base = tmp_path / "input"
    reason = _reason(
        ValueError(f"{base}/spend/spend_2026-06.csv: 必須カラムが見つかりません"),
        base,
    )

    assert reason == "spend/spend_2026-06.csv: 必須カラムが見つかりません"
    assert str(base) not in reason


def test_reason_replaces_bare_absolute_path_with_fixed_label(tmp_path):
    base = tmp_path / "input"
    reason = _reason(FileNotFoundError(f"{base} に入力データがありません"), base)

    assert reason == "入力ディレクトリ に入力データがありません"


def test_reason_is_identical_across_different_absolute_locations(tmp_path):
    def _render(name: str) -> str:
        base = tmp_path / name / "input"
        return _reason(ValueError(f"{base}/spend: 読めません"), base)

    # 置換後に固定語が再置換されないことも兼ねて確認する
    assert _render("a") == _render("bbbbbbbbbb") == "spend: 読めません"


def test_reason_flattens_multiline_messages():
    assert _reason(ValueError("1行目\n  2行目\t3行目"), Path("input")) == "1行目 2行目 3行目"


@requires_symlink
def test_reason_relativizes_lexical_absolute_path_of_symlinked_input(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "link").symlink_to("data", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    base = Path("link")
    # 相対symlinkでは absolute()（未解決）と resolve()（解決後）が一致しない
    assert str(base.absolute()) != str(base.resolve())

    for variant in (base.absolute(), base.resolve()):
        reason = _reason(ValueError(f"{variant}/spend/x.csv: 必須カラムなし"), base)
        assert reason == "spend/x.csv: 必須カラムなし"
        assert str(tmp_path) not in reason


def test_reason_relativizes_parent_relative_input_dir(tmp_path, monkeypatch):
    (tmp_path / "input").mkdir()
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    base = Path("../input")

    reason = _reason(ValueError(f"{base.absolute()}/spend: 読めません"), base)

    assert reason == "spend: 読めません"
    assert str(tmp_path) not in reason
