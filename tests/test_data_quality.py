import dataclasses
import json
import random
from pathlib import Path

import pytest

from seat_analyzer.data_quality import (
    _reason,
    github_config_issues,
    inspect_github,
    issue_to_dict,
    issues_to_canonical_json,
    issues_to_json,
    sort_issues,
)
from seat_analyzer.domain import IssueCode, QualityIssue, Severity
from seat_analyzer.github_collect import GhFailure, GhResult, probe_github

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
    "GITHUB_CONFIG_UNMATCHED",
    # Policy
    "PROHIBITED_PRODUCT_OBSERVED",
    "CAPACITY_SIGNAL_UNAVAILABLE",
}


def test_issue_code_vocabulary_is_fixed():
    # __members__はaliasも列挙するため、別名の紛れ込みも検出できる
    members = IssueCode.__members__
    assert set(members) == EXPECTED_CODES
    assert len(members) == 29
    assert len(EXPECTED_CODES) == 29


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


# --- GitHub の検査（config で有効にした組織のみ） ---
#
# gh は一度も呼ばない。記録済みの応答を返す runner を差し込み、probe から issue までを
# 通して確かめる（probe の解釈と message の組み立ては、分かれていても1つの振る舞い）。

ORG = "org-a"
GH_ORG = "example-org"
MONTH = "2026-06"

# 正常な token の scope（PR の収集に必要な read:org と repo を含む）
GRANTED_SCOPES = "gist, read:org, repo, workflow"


def _api(status: int, headers: tuple[tuple[str, str], ...] = ()) -> GhResult:
    """`gh api -i` の応答（ヘッダ + 空行 + 本文）。"""
    lines = [f"HTTP/2.0 {status} -", *(f"{name}: {value}" for name, value in headers)]
    return GhResult(ok=200 <= status < 300, stdout="\n".join(lines) + "\n\n{}\n")


def _rate(core: int = 4999, search: int = 30) -> GhResult:
    payload = {
        "resources": {
            "core": {"limit": 5000, "remaining": core, "reset": 1788233103},
            "search": {"limit": 30, "remaining": search, "reset": 1788230720},
        }
    }
    return GhResult(ok=True, stdout=json.dumps(payload))


class _FakeGh:
    """記録済みの応答を返す runner。呼び出しの並びを残す。"""

    def __init__(self, responses: dict[str, GhResult]):
        self._responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        # 引数の末尾（status / user / rate_limit / orgs/<名前>）で応答を引く
        return self._responses[args[-1]]

    @property
    def commands(self) -> list[str]:
        return [call[-1] for call in self.calls]


def _fake_gh(**overrides: GhResult) -> _FakeGh:
    """すべて正常な応答を返す runner（overrides で1つずつ差し替える）。

    キーは末尾の引数で、Organization は `orgs` で指す。
    """
    responses = {
        "status": GhResult(ok=True),
        "user": _api(200, (("X-Oauth-Scopes", GRANTED_SCOPES),)),
        "rate_limit": _rate(),
        f"orgs/{GH_ORG}": _api(200),
    }
    if "orgs" in overrides:
        responses[f"orgs/{GH_ORG}"] = overrides.pop("orgs")
    responses.update(overrides)
    return _FakeGh(responses)


def _org_input(
    tmp_path: Path,
    members: tuple[str, ...] = ("a@x.jp",),
    mapping: tuple[str, ...] | None = ("a@x.jp,octo-example",),
) -> Path:
    """組織の入力ディレクトリ（メンバー一覧と、任意で対応表）。"""
    base = tmp_path / ORG
    (base / "members").mkdir(parents=True, exist_ok=True)
    (base / "members" / f"members_{MONTH}.csv").write_text(
        "Email,Seat Type\n" + "".join(f"{email},Premium\n" for email in members),
        encoding="utf-8", newline="\n",
    )
    if mapping is not None:
        (base / "github-members.csv").write_text(
            "email,github_login\n" + "".join(f"{row}\n" for row in mapping),
            encoding="utf-8", newline="\n",
        )
    return base


def _inspect(input_dir: Path, cfg: dict, gh: _FakeGh) -> list[QualityIssue]:
    probes = probe_github([GH_ORG], runner=gh)
    return inspect_github(input_dir, MONTH, cfg, ORG, GH_ORG, probes)


def _codes(issues) -> list[str]:
    return [issue.code.value for issue in issues]


def test_github_reports_nothing_when_everything_is_in_order(tmp_path, cfg):
    assert _inspect(_org_input(tmp_path), cfg, _fake_gh()) == []


def test_github_probes_are_called_once_each_in_a_fixed_order(tmp_path, cfg):
    """認証・scope・利用上限は token 単位なので1度ずつ。Organization は対象の分だけ。"""
    gh = _fake_gh()
    probe_github([GH_ORG, GH_ORG], runner=gh)

    assert gh.commands == ["status", "user", "rate_limit", f"orgs/{GH_ORG}"]


# --- 認証


@pytest.mark.parametrize(("result", "reason", "remedy"), [
    (GhResult(ok=False), "未認証", "gh auth login"),
    (GhResult(ok=False, failure=GhFailure.NOT_FOUND), "gh コマンドが見つかりません",
     "GitHub CLI を導入"),
    (GhResult(ok=False, failure=GhFailure.TIMEOUT), "制限時間内に応答がありません",
     "ネットワークの状態"),
    (GhResult(ok=False, failure=GhFailure.ERROR), "実行に失敗しました", "導入状態"),
])
def test_github_auth_failure_is_an_error(tmp_path, cfg, result, reason, remedy):
    issues = _inspect(_org_input(tmp_path), cfg, _fake_gh(status=result))

    assert _codes(issues) == ["GH_NOT_AUTHENTICATED"]
    assert issues[0].severity is Severity.ERROR
    assert reason in issues[0].message
    assert remedy in issues[0].message
    assert issues[0].scope["github_org"] == GH_ORG


def test_github_auth_failure_stops_the_other_probes(tmp_path, cfg):
    """根本原因1件だけを報告する（派生する参照失敗を並べない）。"""
    gh = _fake_gh(status=GhResult(ok=False))
    issues = _inspect(_org_input(tmp_path), cfg, gh)

    assert gh.commands == ["status"]
    assert _codes(issues) == ["GH_NOT_AUTHENTICATED"]


def test_github_mapping_is_checked_even_when_gh_is_unavailable(tmp_path, cfg):
    """対応表は gh と無関係なローカルの検査なので、認証できなくても見る。"""
    input_dir = _org_input(tmp_path, mapping=None)
    issues = _inspect(input_dir, cfg, _fake_gh(status=GhResult(ok=False)))

    assert _codes(issues) == ["GH_NOT_AUTHENTICATED", "GITHUB_MAPPING_MISSING"]


# --- Organization の参照


def test_github_org_not_found_is_an_error(tmp_path, cfg):
    issues = _inspect(_org_input(tmp_path), cfg, _fake_gh(orgs=_api(404)))

    assert _codes(issues) == ["GH_ORG_NOT_ACCESSIBLE"]
    assert issues[0].severity is Severity.ERROR
    assert "HTTP 404" in issues[0].message
    assert f"organizations.{ORG}.github_org" in issues[0].message


def test_github_org_forbidden_without_sso_header_is_not_accessible(tmp_path, cfg):
    issues = _inspect(_org_input(tmp_path), cfg, _fake_gh(orgs=_api(403)))

    assert _codes(issues) == ["GH_ORG_NOT_ACCESSIBLE"]
    assert "HTTP 403" in issues[0].message


def test_github_org_forbidden_with_sso_header_is_a_permission_issue(tmp_path, cfg):
    """SSO の未承認は権限の不足として区別する（綴りの確認を案内しない）。"""
    gh = _fake_gh(orgs=_api(403, (
        ("X-GitHub-SSO", "required; url=https://github.com/orgs/example-org/sso"),
    )))
    issues = _inspect(_org_input(tmp_path), cfg, gh)

    assert _codes(issues) == ["GH_PERMISSION_INCOMPLETE"]
    assert issues[0].severity is Severity.ERROR
    assert "SAML SSO" in issues[0].message


def test_github_sso_header_on_a_success_is_not_a_permission_issue(tmp_path, cfg):
    """同じヘッダは成功応答に partial-results として付くことがある。"""
    gh = _fake_gh(orgs=_api(200, (
        ("X-GitHub-SSO", "partial-results; organizations=1"),
    )))

    assert _inspect(_org_input(tmp_path), cfg, gh) == []


def test_github_org_probe_failure_names_the_classification_only(tmp_path, cfg):
    gh = _fake_gh(**{f"orgs/{GH_ORG}": GhResult(ok=False, failure=GhFailure.TIMEOUT)})
    issues = _inspect(_org_input(tmp_path), cfg, gh)

    assert _codes(issues) == ["GH_ORG_NOT_ACCESSIBLE"]
    assert "制限時間内に応答がありません" in issues[0].message


def test_github_unreadable_response_is_not_accessible(tmp_path, cfg):
    """応答として解釈できない出力を「参照できた」と読まない。"""
    gh = _fake_gh(orgs=GhResult(ok=True, stdout="なにかの出力\n"))
    issues = _inspect(_org_input(tmp_path), cfg, gh)

    assert _codes(issues) == ["GH_ORG_NOT_ACCESSIBLE"]


# --- scope


def test_github_missing_scope_is_an_error(tmp_path, cfg):
    gh = _fake_gh(user=_api(200, (("X-Oauth-Scopes", "gist, workflow"),)))
    issues = _inspect(_org_input(tmp_path), cfg, gh)

    assert _codes(issues) == ["GH_PERMISSION_INCOMPLETE"]
    assert issues[0].severity is Severity.ERROR
    assert "不足: read:org, repo" in issues[0].message
    assert list(issues[0].scope["missing_scopes"]) == ["read:org", "repo"]


def test_github_higher_scope_satisfies_the_required_one(tmp_path, cfg):
    """上位 scope だけを持つ token を権限不足と誤検出しない。"""
    gh = _fake_gh(user=_api(200, (("X-Oauth-Scopes", "admin:org, repo"),)))

    assert _inspect(_org_input(tmp_path), cfg, gh) == []


def test_github_without_the_scope_header_reports_nothing(tmp_path, cfg):
    """fine-grained PAT・GitHub App の token は scope を判定できない。"""
    gh = _fake_gh(user=_api(200))

    assert _inspect(_org_input(tmp_path), cfg, gh) == []


def test_github_empty_scope_header_is_a_permission_issue(tmp_path, cfg):
    """ヘッダが空なのは「scope が1つも無い」で、判定できないのとは別。"""
    gh = _fake_gh(user=_api(200, (("X-Oauth-Scopes", ""),)))

    assert _codes(_inspect(_org_input(tmp_path), cfg, gh)) == [
        "GH_PERMISSION_INCOMPLETE"
    ]


# --- 利用上限


def test_github_rate_limit_is_a_warning(tmp_path, cfg):
    issues = _inspect(_org_input(tmp_path), cfg, _fake_gh(rate_limit=_rate(core=0)))

    assert _codes(issues) == ["GH_RATE_LIMITED"]
    assert issues[0].severity is Severity.WARNING
    assert "core: 残り 0 / 上限 5000" in issues[0].message
    assert list(issues[0].scope["resources"]) == ["core"]


def test_github_rate_limit_message_has_no_reset_time(tmp_path, cfg):
    """回復時刻は実行のたびに変わるので message へ入れない。"""
    issues = _inspect(_org_input(tmp_path), cfg, _fake_gh(rate_limit=_rate(core=0)))

    assert "1788233103" not in issues[0].message
    assert "reset" not in issues[0].message


def test_github_rate_limit_covers_search(tmp_path, cfg):
    issues = _inspect(
        _org_input(tmp_path), cfg, _fake_gh(rate_limit=_rate(core=0, search=0)))

    assert list(issues[0].scope["resources"]) == ["core", "search"]


def test_github_unreadable_rate_status_is_not_reported(tmp_path, cfg):
    """残量だけ読めない状況は通信の不調で、同じ原因が参照の検査で error になる。"""
    gh = _fake_gh(rate_limit=GhResult(ok=True, stdout="{}"))

    assert _inspect(_org_input(tmp_path), cfg, gh) == []


# --- 対応表


def test_github_mapping_file_missing_is_a_warning(tmp_path, cfg):
    issues = _inspect(_org_input(tmp_path, mapping=None), cfg, _fake_gh())

    assert _codes(issues) == ["GITHUB_MAPPING_MISSING"]
    assert issues[0].severity is Severity.WARNING
    assert "github-members.csv" in issues[0].message


def test_github_members_without_a_login_are_warned_with_a_sample(tmp_path, cfg):
    input_dir = _org_input(
        tmp_path, members=("a@x.jp", "b@x.jp", "c@x.jp"),
        mapping=("a@x.jp,octo-example",),
    )
    issues = _inspect(input_dir, cfg, _fake_gh())

    assert _codes(issues) == ["GITHUB_MAPPING_MISSING"]
    assert "2 名います" in issues[0].message
    assert list(issues[0].scope["emails"]) == ["b@x.jp", "c@x.jp"]


def test_github_loader_warning_becomes_an_issue(tmp_path, cfg):
    """login の空欄・読めない字句は loader の警告として上がってくる。"""
    input_dir = _org_input(
        tmp_path, members=("a@x.jp",), mapping=("a@x.jp,@octo-example",))
    issues = _inspect(input_dir, cfg, _fake_gh())

    assert _codes(issues) == ["GITHUB_MAPPING_MISSING", "GITHUB_MAPPING_MISSING"]
    assert any("解釈できません" in issue.message for issue in issues)
    assert all("github-members.csv" in issue.message for issue in issues)


def test_github_broken_mapping_is_an_error_without_absolute_paths(tmp_path, cfg):
    """取り違えに直結する不備は fail-closed（読めない対応表で集計を完走させない）。"""
    input_dir = _org_input(
        tmp_path, mapping=("a@x.jp,octo-example", "a@x.jp,other-example"))
    issues = _inspect(input_dir, cfg, _fake_gh())

    assert _codes(issues) == ["GITHUB_MAPPING_DUPLICATE"]
    assert issues[0].severity is Severity.ERROR
    assert str(input_dir) not in issues[0].message


def test_github_unmapped_check_is_skipped_when_members_are_unreadable(tmp_path, cfg):
    """メンバー一覧の不備は inspect_input が報告するので、ここでは二重に出さない。"""
    input_dir = _org_input(tmp_path)
    (input_dir / "members" / f"members_{MONTH}.csv").write_text(
        "Seat Type\nPremium\n", encoding="utf-8", newline="\n")

    assert _inspect(input_dir, cfg, _fake_gh()) == []


def test_github_unmapped_check_is_skipped_without_a_target_month(tmp_path, cfg):
    input_dir = _org_input(tmp_path, members=("a@x.jp", "b@x.jp"))
    probes = probe_github([GH_ORG], runner=_fake_gh())

    assert inspect_github(input_dir, None, cfg, ORG, GH_ORG, probes) == []


# --- 決定性


def test_github_issues_are_identical_across_runs(tmp_path, cfg):
    """同じ probe 結果からは常に同じ JSON（絶対パス・時刻・乱数を含めない）。"""
    input_dir = _org_input(tmp_path, members=("a@x.jp", "b@x.jp"), mapping=())
    gh = _fake_gh(orgs=_api(404), rate_limit=_rate(core=0))

    first = issues_to_canonical_json(_inspect(input_dir, cfg, gh))
    second = issues_to_canonical_json(_inspect(input_dir, cfg, gh))

    assert first == second
    assert str(tmp_path) not in first


def test_github_issues_have_no_month_in_scope(tmp_path, cfg):
    """GitHub の検査結果は対象月に依存しない。"""
    issues = _inspect(_org_input(tmp_path, mapping=None), cfg, _fake_gh(orgs=_api(404)))

    assert issues
    for issue in issues:
        assert "month" not in issue.scope
        assert issue.scope["org"] == ORG


# --- 設定のキーと組織ディレクトリの突き合わせ


def _cfg_with_organizations(cfg: dict, **entries: str) -> dict:
    return {**cfg, "organizations": {
        org: {"github_org": github_org} for org, github_org in entries.items()
    }}


def test_config_key_without_a_matching_org_is_a_warning(cfg):
    issues = github_config_issues(
        _cfg_with_organizations(cfg, org_typo=GH_ORG), ["org-a", "org-b"])

    assert _codes(issues) == ["GITHUB_CONFIG_UNMATCHED"]
    assert issues[0].severity is Severity.WARNING
    assert "org_typo" in issues[0].message
    assert "org-a/org-b" in issues[0].message
    assert issues[0].scope["config_org"] == "org_typo"
    assert "org" not in issues[0].scope


def test_config_key_that_matches_an_org_is_silent(cfg):
    assert github_config_issues(
        _cfg_with_organizations(cfg, **{"org-a": GH_ORG}), ["org-a", "org-b"]) == []


def test_config_without_organizations_is_silent(cfg):
    assert github_config_issues(cfg, ["org-a"]) == []
