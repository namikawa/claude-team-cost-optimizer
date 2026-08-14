import pandas as pd
import pytest

from seat_analyzer import product_usage
from seat_analyzer.analyze import analyze
from seat_analyzer.domain import IssueCode, Severity

from .conftest import run_analyze, spend_row

# 明細の列（apply_cost_basis 適用後のうち product_usage が見る列）
COLUMNS = ["email", "cost_usd", "requests", "product"]


def detail(rows: list[tuple], *, product: bool = True, requests: bool = True) -> pd.DataFrame:
    """(email, cost_usd, requests, product) の明細を組み立てる。

    product=False / requests=False で、その任意カラムが無い入力を作る。
    """
    df = pd.DataFrame(rows, columns=COLUMNS)
    drop = [c for c, keep in (("product", product), ("requests", requests)) if not keep]
    return df.drop(columns=drop)


@pytest.fixture
def policy(cfg) -> dict:
    """既定設定の product_policy（primary=Claude Code・閾値 $100・prohibited なし）。"""
    return cfg["product_policy"]


def test_features_match_the_definitions(policy):
    # alice: Code 300/900req・Chat 60/60req・Cowork 40/40req
    # bob:   Chat 10/5req のみ
    # carol: 分類のどれにも無い product のみ（費用には乗るが Code でも補助でもない）
    features = product_usage.compute(detail([
        ("alice@x.jp", 300.0, 900.0, "Claude Code"),
        ("alice@x.jp", 60.0, 60.0, "Chat"),
        ("alice@x.jp", 40.0, 40.0, "Cowork"),
        ("bob@x.jp", 10.0, 5.0, "Chat"),
        ("carol@x.jp", 200.0, 100.0, "Prototype Console"),
    ]), policy).features

    assert list(features.index) == ["alice@x.jp", "bob@x.jp", "carol@x.jp"]
    assert features["total_demand_usd"].tolist() == [400.0, 10.0, 200.0]
    assert features["code_demand_usd"].tolist() == [300.0, 0.0, 0.0]
    assert features["code_demand_share"].tolist() == [0.75, 0.0, 0.0]
    assert features["total_requests"].tolist() == [1000.0, 5.0, 100.0]
    assert features["code_requests"].tolist() == [900.0, 0.0, 0.0]
    # alice の requests 比は Code 90% / Chat 6% / Cowork 4% → 5% 以上は2つ
    assert features["product_breadth"].tolist() == [2, 1, 1]
    # alice の補助 product 需要は 60+40=100 で閾値 $100 以上、bob は $10
    assert features["supplementary_high"].tolist() == [True, False, False]
    assert features["prohibited_observed"].tolist() == [False, False, False]


def test_features_have_exactly_the_defined_columns(policy):
    result = product_usage.compute(
        detail([("alice@x.jp", 1.0, 1.0, "Claude Code")]), policy)

    assert list(result.features.columns) == list(product_usage.FEATURE_COLUMNS)
    assert len(product_usage.FEATURE_COLUMNS) == 8
    assert result.issues == []


def test_primary_accepts_multiple_names(policy):
    # 表記ゆれは primary に名前を並べて吸収する（あいまい一致は実装しない）
    features = product_usage.compute(detail([
        ("alice@x.jp", 30.0, 3.0, "Claude Code"),
        ("alice@x.jp", 70.0, 7.0, "Claude Code (CLI)"),
    ]), dict(policy, primary=["Claude Code", "Claude Code (CLI)"])).features

    assert features.loc["alice@x.jp", "code_demand_usd"] == 100.0
    assert features.loc["alice@x.jp", "code_requests"] == 10.0
    assert features.loc["alice@x.jp", "code_demand_share"] == 1.0


def test_matching_is_exact_and_not_partial(policy):
    # "Code Review" は "Claude Code" にも "Code" にも一致させない（別 product のため）
    features = product_usage.compute(detail([
        ("alice@x.jp", 40.0, 4.0, "Code Review"),
        ("alice@x.jp", 60.0, 6.0, "Claude Code"),
    ]), policy).features

    assert features.loc["alice@x.jp", "code_demand_usd"] == 60.0
    assert features.loc["alice@x.jp", "code_demand_share"] == 0.6


def test_matching_ignores_spacing_case_and_unicode_form(policy):
    # 明細は NFD（e + 結合アクセント U+0301）、設定は NFC。同じ product 名として扱う
    nfd, nfc = "Cafe\u0301 Console", "Caf\u00e9 Console"
    assert nfd != nfc
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 1.0, "  claude code  "),
        ("alice@x.jp", 20.0, 2.0, nfd),
    ]), dict(policy, primary=["Claude Code", nfc])).features

    assert features.loc["alice@x.jp", "code_demand_usd"] == 30.0
    assert features.loc["alice@x.jp", "code_demand_share"] == 1.0


def test_missing_product_column_leaves_code_features_unknown(policy):
    result = product_usage.compute(detail([
        ("alice@x.jp", 300.0, 30.0, "Claude Code"),
        ("bob@x.jp", 10.0, 5.0, "Chat"),
    ], product=False), policy)
    features = result.features

    # 全 product の費用・回数は product 列が無くても分かる
    assert features["total_demand_usd"].tolist() == [300.0, 10.0]
    assert features["total_requests"].tolist() == [30.0, 5.0]
    for column in ("code_demand_usd", "code_demand_share", "code_requests",
                   "product_breadth", "supplementary_high", "prohibited_observed"):
        assert features[column].isna().all(), column

    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.code is IssueCode.CAPACITY_SIGNAL_UNAVAILABLE
    assert issue.severity is Severity.WARNING
    assert issue.scope["column"] == "product"


def test_missing_requests_column_does_not_fall_back_to_row_count(policy):
    result = product_usage.compute(detail([
        ("alice@x.jp", 300.0, 30.0, "Claude Code"),
        ("alice@x.jp", 100.0, 10.0, "Chat"),
    ], requests=False), policy)
    features = result.features

    # 回数を明細行数（2行）で代替しない。判定に効きうる数値を観測せずに作らないため
    for column in ("total_requests", "code_requests", "product_breadth"):
        assert features[column].isna().all(), column
    # 費用側の特徴量は requests が無くても算出する
    assert features.loc["alice@x.jp", "code_demand_usd"] == 300.0
    assert features.loc["alice@x.jp", "code_demand_share"] == 0.75
    assert result.issues == []


def test_share_is_unknown_when_total_demand_is_zero(policy):
    features = product_usage.compute(detail([
        ("alice@x.jp", 0.0, 3.0, "Claude Code"),
    ]), policy).features

    assert features.loc["alice@x.jp", "total_demand_usd"] == 0.0
    assert features.loc["alice@x.jp", "code_demand_usd"] == 0.0
    assert pd.isna(features.loc["alice@x.jp", "code_demand_share"])


def test_prohibited_supplementary_still_counts_as_supplementary(policy):
    # prohibited は分類と直交する指定。禁止されていても補助 product の需要には数える
    features = product_usage.compute(detail([
        ("alice@x.jp", 150.0, 15.0, "Cowork"),
    ]), dict(policy, prohibited=["Cowork"])).features

    assert features.loc["alice@x.jp", "supplementary_high"]
    assert features.loc["alice@x.jp", "prohibited_observed"]


def test_prohibited_products_are_reported(policy):
    result = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 1.0, "Prototype Console"),
        ("bob@x.jp", 20.0, 2.0, "prototype console"),
        ("carol@x.jp", 30.0, 3.0, "Claude Code"),
    ]), dict(policy, prohibited=["Prototype Console"]))
    features = result.features

    assert features["prohibited_observed"].tolist() == [True, True, False]
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.code is IssueCode.PROHIBITED_PRODUCT_OBSERVED
    assert issue.severity is Severity.WARNING
    # 実データに現れた表記を挙げる（該当行を探せるようにするため）
    assert issue.scope["products"] == ("Prototype Console", "prototype console")
    assert issue.scope["n_users"] == 2


def test_no_prohibited_product_by_default(policy):
    result = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 1.0, "Claude Code"),
        ("alice@x.jp", 20.0, 2.0, "Cowork"),
    ]), policy)

    assert policy["prohibited"] == []
    assert result.features["prohibited_observed"].tolist() == [False]
    assert result.issues == []


def test_result_does_not_depend_on_row_order(policy):
    rows = [
        ("bob@x.jp", 10.0, 5.0, "Chat"),
        ("alice@x.jp", 300.0, 900.0, "Claude Code"),
        ("alice@x.jp", 60.0, 60.0, "Cowork"),
        ("bob@x.jp", 40.0, 40.0, "Prototype Console"),
    ]
    prohibited = dict(policy, prohibited=["Prototype Console"])

    first = product_usage.compute(detail(rows), prohibited)
    again = product_usage.compute(detail(rows), prohibited)
    shuffled = product_usage.compute(detail(list(reversed(rows))), prohibited)

    pd.testing.assert_frame_equal(first.features, again.features)
    pd.testing.assert_frame_equal(first.features, shuffled.features)
    assert first.issues == again.issues == shuffled.issues


def test_analyze_attaches_product_usage(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("alice.morgan@x.jp", 120.0),
                     spend_row("alice.morgan@x.jp", 40.0, product="Chat")]},
        members=["alice.morgan@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")

    features = result.product_usage.features
    assert result.product_usage.issues == []
    assert features.loc["alice.morgan@x.jp", "total_demand_usd"] == pytest.approx(160.0)
    assert features.loc["alice.morgan@x.jp", "code_demand_usd"] == pytest.approx(120.0)
    assert features.loc["alice.morgan@x.jp", "code_demand_share"] == pytest.approx(0.75)


def test_product_usage_does_not_change_reports(two_orgs, tmp_path):
    # 特徴量の計算は保持までで、レポート成果物には出さない（Step 8 以降の担当）
    output_dir = run_analyze(two_orgs, tmp_path)
    report = (output_dir / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")

    assert "product_breadth" not in report
    assert "code_demand" not in report
