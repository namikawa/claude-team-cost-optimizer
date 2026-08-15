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
        ("bob@x.jp", 200.0, 5.0, "Chat"),
    ], product=False), dict(policy, prohibited=["Prototype Console"]))
    features = result.features

    # 全 product の費用・回数は product 列が無くても分かる
    assert features["total_demand_usd"].tolist() == [300.0, 200.0]
    assert features["total_requests"].tolist() == [30.0, 5.0]
    for column in ("code_demand_usd", "code_demand_share", "code_requests",
                   "product_breadth", "supplementary_high", "prohibited_observed"):
        assert features[column].isna().all(), column

    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.code is IssueCode.CAPACITY_SIGNAL_UNAVAILABLE
    assert issue.severity is Severity.WARNING
    assert issue.scope["column"] == "product"
    assert issue.scope["reason"] == "column_missing"
    assert issue.scope["features"] == (
        "code_demand_usd", "code_demand_share", "code_requests",
        "product_breadth", "supplementary_high", "prohibited_observed",
    )


def test_missing_product_column_still_settles_empty_categories(policy):
    # product 名が分からなくても、名前が1つも無い分類には一致しようがない。列が無い
    # 経路でもセル欠けの経路と同じ規則で確定させる
    result = product_usage.compute(detail([
        ("alice@x.jp", 300.0, 30.0, "Claude Code"),
    ], product=False), dict(policy, supplementary=[]))
    features = result.features

    assert policy["prohibited"] == []
    assert not features.loc["alice@x.jp", "prohibited_observed"]
    assert not features.loc["alice@x.jp", "supplementary_high"]
    # 確定値が入った列は「算出できなかった列」ではないので issue に挙げない
    assert result.issues[0].scope["features"] == (
        "code_demand_usd", "code_demand_share", "code_requests", "product_breadth")


def test_missing_product_column_with_zero_threshold_settles_true(policy):
    # supplementary が空なら需要は常に 0。閾値 0 との比較は真で確定する
    features = product_usage.compute(detail([
        ("alice@x.jp", 300.0, 30.0, "Claude Code"),
    ], product=False), dict(policy, supplementary=[], supplementary_high_usd=0.0)).features

    assert features.loc["alice@x.jp", "supplementary_high"]


def test_blank_product_cells_leave_that_user_unconfirmed(policy):
    # 空の行が Code・補助・禁止 product だった可能性を否定できないため、その行しだいで
    # 変わる値は確定しない（空の行の需要 $150 は閾値 $100 をまたぐ）
    features = product_usage.compute(detail([
        ("alice@x.jp", 150.0, 100.0, ""),
        ("alice@x.jp", 5.0, 50.0, "Claude Code"),
        ("bob@x.jp", 20.0, 10.0, "Chat"),
    ]), dict(policy, prohibited=["Prototype Console"])).features

    for column in ("code_demand_usd", "code_demand_share", "code_requests",
                   "product_breadth", "supplementary_high", "prohibited_observed"):
        assert pd.isna(features.loc["alice@x.jp", column]), column
    # product に依存しない特徴量は、空の行の分も含めて確定する
    assert features.loc["alice@x.jp", "total_demand_usd"] == 155.0
    assert features.loc["alice@x.jp", "total_requests"] == 150.0
    # 伝播はユーザ単位。同じ入力に居ても欠損の無いユーザの特徴量は欠損にならない
    assert features.loc["bob@x.jp"].notna().all()


def test_all_blank_product_cells_leave_every_product_feature_unknown(policy):
    features = product_usage.compute(detail([
        ("alice@x.jp", 100.0, 100.0, "   "),
        ("alice@x.jp", 50.0, 50.0, None),
    ]), dict(policy, prohibited=["Prototype Console"])).features

    for column in ("code_demand_usd", "code_demand_share", "code_requests",
                   "product_breadth", "supplementary_high", "prohibited_observed"):
        assert pd.isna(features.loc["alice@x.jp", column]), column
    assert features.loc["alice@x.jp", "total_demand_usd"] == 150.0


def test_empty_category_stays_false_with_blank_product_cells(policy):
    # 既定の prohibited は空。空の行が何であっても一致しようがないので「使っていない」は
    # 確定する。名前のある supplementary は同じ行のせいで確定しない
    features = product_usage.compute(detail([
        ("alice@x.jp", 150.0, 100.0, ""),
        ("alice@x.jp", 5.0, 50.0, "Claude Code"),
    ]), policy).features

    observed = features.loc["alice@x.jp", "prohibited_observed"]
    assert policy["prohibited"] == []
    assert pd.notna(observed) and not observed
    assert pd.isna(features.loc["alice@x.jp", "supplementary_high"])


def test_supplementary_high_is_false_when_the_unknown_rows_cannot_reach(policy):
    # 空の行が全部 supplementary でも $10 で閾値 $100 に届かない → 偽が確定する
    features = product_usage.compute(detail([
        ("alice@x.jp", 0.0, 1.0, "Claude Code"),
        ("alice@x.jp", 10.0, 1.0, ""),
    ]), policy).features

    high = features.loc["alice@x.jp", "supplementary_high"]
    assert pd.notna(high) and not high


def test_supplementary_high_is_true_when_the_lower_bound_reaches(policy):
    # 既知の補助需要だけで閾値を超えていれば、空の行が何であっても覆らない
    features = product_usage.compute(detail([
        ("alice@x.jp", 150.0, 1.0, "Cowork"),
        ("alice@x.jp", 10.0, 1.0, ""),
    ]), policy).features

    assert features.loc["alice@x.jp", "supplementary_high"]


def test_supplementary_high_is_unknown_when_negative_cost_can_drop_it(policy):
    # cost_basis=net_spend では返金等で負の値がありうる。空の行が補助 product なら
    # 合計は $50 で閾値を割るため、上限だけを見て真を残さない
    features = product_usage.compute(detail([
        ("alice@x.jp", 150.0, 1.0, "Cowork"),
        ("alice@x.jp", -100.0, 1.0, ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "supplementary_high"])


def test_blank_product_cells_with_no_contribution_stay_certain(policy):
    # 需要も回数も 0 の行は、どの product であっても結果を動かさない
    features = product_usage.compute(detail([
        ("alice@x.jp", 100.0, 10.0, "Claude Code"),
        ("alice@x.jp", 0.0, 0.0, ""),
        # 需要だけ 0（回数は動く）→ 費用側は確定・回数側は確定しない
        ("bob@x.jp", 100.0, 10.0, "Claude Code"),
        ("bob@x.jp", 0.0, 5.0, ""),
        # 回数だけ 0（需要は動く）→ 回数側は確定・費用側は確定しない
        ("carol@x.jp", 100.0, 10.0, "Claude Code"),
        ("carol@x.jp", 5.0, 0.0, ""),
    ]), policy).features

    assert features.loc["alice@x.jp", "code_demand_usd"] == 100.0
    assert features.loc["alice@x.jp", "code_demand_share"] == 1.0
    assert features.loc["alice@x.jp", "code_requests"] == 10.0
    assert features.loc["alice@x.jp", "product_breadth"] == 1

    assert features.loc["bob@x.jp", "code_demand_usd"] == 100.0
    assert pd.isna(features.loc["bob@x.jp", "code_requests"])
    assert pd.isna(features.loc["bob@x.jp", "product_breadth"])

    assert pd.isna(features.loc["carol@x.jp", "code_demand_usd"])
    assert features.loc["carol@x.jp", "code_requests"] == 10.0
    assert features.loc["carol@x.jp", "product_breadth"] == 1


def test_prohibited_observed_ignores_amounts(policy):
    # 禁止 product を $0・0 回で使っていても、観測した事実は変わらない
    features = product_usage.compute(detail([
        ("alice@x.jp", 0.0, 0.0, "Prototype Console"),
        ("alice@x.jp", 10.0, 10.0, "Claude Code"),
    ]), dict(policy, prohibited=["Prototype Console"])).features

    assert features.loc["alice@x.jp", "prohibited_observed"]


def test_observed_prohibited_stays_true_with_blank_product_cells(policy):
    # 存在の主張（1行でも観測した）は、他の行が分類できなくても覆らない
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 5.0, "Prototype Console"),
        ("alice@x.jp", 5.0, 5.0, ""),
    ]), dict(policy, prohibited=["Prototype Console"])).features

    assert features.loc["alice@x.jp", "prohibited_observed"]
    assert pd.isna(features.loc["alice@x.jp", "code_demand_usd"])


def test_supplementary_high_stays_true_with_blank_product_cells(policy):
    features = product_usage.compute(detail([
        ("alice@x.jp", 150.0, 5.0, "Cowork"),
        ("alice@x.jp", 5.0, 5.0, ""),
    ]), dict(policy, prohibited=["Prototype Console"])).features

    # 閾値を超えたことは、空の行が何であっても覆らない
    assert features.loc["alice@x.jp", "supplementary_high"]
    # 不在の主張（偽）は確定しない。空の行が禁止 product だった可能性が残る
    assert pd.isna(features.loc["alice@x.jp", "prohibited_observed"])


def test_blank_product_cells_are_reported_and_distinguishable(policy):
    value_missing = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 5.0, ""),
        ("alice@x.jp", 5.0, 5.0, "Claude Code"),
        ("bob@x.jp", 5.0, 5.0, None),
        ("carol@x.jp", 5.0, 5.0, "Chat"),
    ]), policy)
    column_missing = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 5.0, "Claude Code"),
    ], product=False), policy)

    assert len(value_missing.issues) == 1
    issue = value_missing.issues[0]
    assert issue.code is IssueCode.CAPACITY_SIGNAL_UNAVAILABLE
    assert issue.severity is Severity.WARNING
    assert issue.scope["n_users"] == 2
    assert issue.scope["n_rows"] == 2
    # 列ごと無い場合とは対象範囲も対処も違うので、同じ code でも scope で区別できる
    assert issue.scope["reason"] != column_missing.issues[0].scope["reason"]


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


def test_missing_requests_column_still_settles_users_without_code_rows(policy):
    # primary の行が1つも無ければ、回数が全く分からなくても Code の回数は 0 で確定する
    # （行数からの代替ではなく「primary の行が無い」という観測から出る値）
    features = product_usage.compute(detail([
        ("alice@x.jp", 100.0, 0.0, "Chat"),
    ], requests=False), policy).features

    assert pd.isna(features.loc["alice@x.jp", "total_requests"])
    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])
    assert features.loc["alice@x.jp", "code_requests"] == 0.0


def test_missing_requests_cells_are_treated_like_a_missing_column(policy):
    # 合計は欠損を読み飛ばすため、全件欠損なら 0、一部欠損なら観測できた分だけが返る。
    # どちらも「回数」として渡さない
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, float("nan"), "Claude Code"),
        ("bob@x.jp", 10.0, float("nan"), "Claude Code"),
        ("bob@x.jp", 5.0, 20.0, "Chat"),
        ("carol@x.jp", 5.0, 20.0, "Chat"),
    ]), policy).features

    for email in ("alice@x.jp", "bob@x.jp"):
        for column in ("total_requests", "code_requests", "product_breadth"):
            assert pd.isna(features.loc[email, column]), (email, column)
    # 費用側の特徴量と、欠損の無いユーザは影響を受けない
    assert features.loc["bob@x.jp", "code_demand_usd"] == 10.0
    assert features.loc["carol@x.jp", "total_requests"] == 20.0
    assert features.loc["carol@x.jp", "product_breadth"] == 1


def test_missing_requests_on_a_classified_non_primary_row(policy):
    # 欠けているのが Chat と分かっている行なら、primary の回数は動かない。分母には
    # 効くので total_requests と product_breadth は確定しない
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 10.0, "Claude Code"),
        ("alice@x.jp", 5.0, float("nan"), "Chat"),
    ]), policy).features

    assert features.loc["alice@x.jp", "code_requests"] == 10.0
    assert pd.isna(features.loc["alice@x.jp", "total_requests"])
    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_missing_requests_on_a_primary_row_leaves_code_requests_unknown(policy):
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, float("nan"), "Claude Code"),
        ("alice@x.jp", 5.0, 10.0, "Chat"),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "code_requests"])


def test_missing_requests_on_a_blank_product_row_leaves_code_requests_unknown(policy):
    # 名前も回数も分からない行は primary だった可能性があるため確定しない
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 10.0, "Claude Code"),
        ("alice@x.jp", 5.0, float("nan"), ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "code_requests"])


def test_breadth_is_unknown_when_total_requests_is_zero(policy):
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 0.0, "Claude Code"),
    ]), policy).features

    assert features.loc["alice@x.jp", "total_requests"] == 0.0
    assert features.loc["alice@x.jp", "code_requests"] == 0.0
    # 比を定義できない状態。「下限を超える product が無かった」を表す 0 とは区別する
    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_breadth_is_settled_when_the_unknown_rows_cannot_change_the_lineup(policy):
    # 不明行は1リクエスト（全体の 1%）。新しい product にしても既知 product へ足しても
    # 5% の顔ぶれは変わらないので、breadth=1 が確定する
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 99.0, "Claude Code"),
        ("alice@x.jp", 0.5, 1.0, ""),
    ]), policy).features

    assert features.loc["alice@x.jp", "product_breadth"] == 1


def test_breadth_is_settled_when_every_known_product_already_counts(policy):
    # 下限に届いていない既知 product が無く、不明行だけでは 5% に届かない
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 50.0, "Claude Code"),
        ("alice@x.jp", 1.0, 49.0, "Chat"),
        ("alice@x.jp", 1.0, 1.0, ""),
    ]), policy).features

    assert features.loc["alice@x.jp", "product_breadth"] == 2


def test_breadth_is_unknown_when_a_short_product_can_be_pushed_over(policy):
    # Chat は 4%（未カウント）。不明行の 2 リクエストを注ぎ込むと 6% で届いてしまう
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 94.0, "Claude Code"),
        ("alice@x.jp", 1.0, 4.0, "Chat"),
        ("alice@x.jp", 1.0, 2.0, ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_breadth_is_unknown_when_the_unknown_rows_can_form_a_product(policy):
    # 不明行だけで 10%。新しい product として数えられると顔ぶれが変わる
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 90.0, "Claude Code"),
        ("alice@x.jp", 1.0, 10.0, ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_breadth_is_unknown_when_an_unknown_row_has_negative_requests(policy):
    # 負の値があると「既知 product は増える方向にしか動かない」が崩れ、下限を割りうる
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 101.0, "Claude Code"),
        ("alice@x.jp", 1.0, -1.0, ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_breadth_is_conservatively_unknown_when_unknown_rows_dominate(policy):
    """確定できるケースでも、確定条件を満たさなければ欠損に倒す（既知の保守性）。

    下限を超える既知 product が1つも無く、既知は requests 0 の Chat だけなので、分からない
    行をどこへ帰属させても（新しい product にしても Chat に足しても）下限を超える product は
    同じ1つで breadth=1 になる。それでも「分からない行の比が下限未満」を満たさないので
    欠損になる。正確に判定するには束ね方の組合せを解く必要があるため、_product_breadth の
    docstring のとおり保守的な欠損を仕様としている。
    """
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 0.0, "Chat"),
        ("alice@x.jp", 1.0, 100.0, ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_breadth_is_unknown_when_unknown_rows_can_be_split(policy):
    """分からない行の束ね方で結果が変わるケース（こちらは本当に不確定）。

    2行を同じ product とみなせば 1、別々の product とみなせば 2 になる。上の保守的な
    欠損と違い、ここは確定させてはいけない側の例。
    """
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 0.0, "Chat"),
        ("alice@x.jp", 1.0, 50.0, ""),
        ("alice@x.jp", 1.0, 50.0, ""),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])


def test_breadth_is_unknown_when_known_requests_can_cancel_out(policy):
    """分からない行を持つユーザに負の requests があれば確定させない。

    ここでは既知 product の合計が桁落ちで 0 になる一方、分からない行をその product へ
    割り当てたシナリオの正確な合計は 2（全体の 6.7%）で、下限を超えて breadth が 2 に
    なりうる。別々に集計した値の加算では届かないと誤判定するため保守的に倒す。
    分からない行を持たないユーザには適用しない（割り当ての自由度が無い）。
    """
    features = product_usage.compute(detail([
        ("alice@x.jp", 1.0, 1e16, "Prototype Console"),
        ("alice@x.jp", 1.0, 1.0, ""),
        ("alice@x.jp", 1.0, 1.0, "Prototype Console"),
        ("alice@x.jp", 1.0, -1e16, "Prototype Console"),
        ("alice@x.jp", 1.0, 28.0, "Other"),
        ("bob@x.jp", 1.0, -1.0, "Chat"),
        ("bob@x.jp", 1.0, 100.0, "Claude Code"),
    ]), policy).features

    assert pd.isna(features.loc["alice@x.jp", "product_breadth"])
    assert features.loc["bob@x.jp", "product_breadth"] == 1


def test_non_finite_values_are_not_settled_as_zero(policy):
    """相殺して NaN になった合計を 0 として確定しない。

    行が1つも無いユーザの空和（0）と、集計の結果が NaN になったユーザを混同すると、
    観測していない 0 を確定値として出すことになる。
    """
    features = product_usage.compute(detail([
        ("alice@x.jp", float("inf"), 1.0, "Claude Code"),
        ("alice@x.jp", float("-inf"), 1.0, "Claude Code"),
        ("bob@x.jp", 10.0, 1.0, "Claude Code"),
    ]), policy).features

    for column in ("total_demand_usd", "code_demand_usd", "code_demand_share"):
        assert pd.isna(features.loc["alice@x.jp", column]), column
    assert features.loc["bob@x.jp", "code_demand_usd"] == 10.0


def test_excluded_rows_do_not_settle_a_nonzero_unknown_contribution(policy):
    """不明行に非ゼロの寄与があるユーザを確定させない。

    除外する行を 0 で埋めて合計すると加算のブロック割りが「その行だけを合計した場合」と
    変わり、確定分の合計と全部入りの合計が同じ丸め先へ落ちて欠損の伝播が解けることがある。
    行の組をそのまま合計していれば2つのシナリオは別の値になり、確定しない。
    """
    frame = detail([
        ("alice@x.jp", 0.0, 1.0, "Other"),
        ("alice@x.jp", 1e-16, 1.0, "Claude Code"),
        ("alice@x.jp", 0.0, 1.0, "Other"),
        ("alice@x.jp", 1e-16, 1.0, "Claude Code"),
        ("alice@x.jp", 1e-16, 1.0, "Claude Code"),
        ("alice@x.jp", 0.0, 1.0, "Other"),
        ("alice@x.jp", 1.0, 1.0, "Claude Code"),
        ("alice@x.jp", 1e-16, 1.0, ""),
        ("alice@x.jp", 1e-17, 1.0, ""),
        ("alice@x.jp", 0.0, 1.0, "Other"),
    ])
    is_code = frame["product"] == "Claude Code"
    unknown = frame["product"] == ""
    lowest = frame[is_code].groupby("email")["cost_usd"].sum()["alice@x.jp"]
    highest = frame[is_code | unknown].groupby("email")["cost_usd"].sum()["alice@x.jp"]
    assert lowest != highest

    features = product_usage.compute(frame, policy).features
    assert pd.isna(features.loc["alice@x.jp", "code_demand_usd"])


def test_certain_sums_come_from_a_direct_scenario_sum(policy):
    """確定値は、実際にありうる行の組をそのまま合計した値と一致する。

    確定分と不明分を別々に合計してから足し直すと、2段の丸めが直接合計と食い違い、
    どのシナリオでも出ない値を確定しうる。ここでは「不明行を入れない合計」と「全部
    入れた合計」が倍精度で別の値になる並びを使い、確定させないことを固定する。
    """
    frame = detail([
        ("alice@x.jp", 1e-17, 1.0, ""),
        ("alice@x.jp", 5e-17, 1.0, ""),
        ("alice@x.jp", 1e-16, 1.0, "Claude Code"),
        ("alice@x.jp", 1.0, 1.0, "Claude Code"),
    ])
    is_code = frame["product"] == "Claude Code"
    lowest = frame[is_code].groupby("email")["cost_usd"].sum()["alice@x.jp"]
    highest = frame.groupby("email")["cost_usd"].sum()["alice@x.jp"]
    assert lowest != highest

    features = product_usage.compute(frame, policy).features
    assert pd.isna(features.loc["alice@x.jp", "code_demand_usd"])


def test_breadth_counts_products_at_the_lower_bound(policy):
    # 下限ちょうど（5%）は数える。分母・分子とも整数で丸めが除算1回に閉じる値を使う
    features = product_usage.compute(detail([
        ("alice@x.jp", 10.0, 950.0, "Claude Code"),
        ("alice@x.jp", 10.0, 50.0, "Chat"),
        ("bob@x.jp", 10.0, 960.0, "Claude Code"),
        ("bob@x.jp", 10.0, 40.0, "Chat"),
    ]), policy).features

    assert features.loc["alice@x.jp", "product_breadth"] == 2
    assert features.loc["bob@x.jp", "product_breadth"] == 1


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


def test_result_is_stable_across_repeated_runs(policy):
    """同じ入力からは常に同じ結果になる（実行間の決定性）。

    行の順序を入れ替えた場合も比較しているが、これは合計が整数どうしの加算に収まり
    丸めが起きない値だけを使っているから成立するもので、一般の入力に対する保証では
    ない。金額・回数の合計は浮動小数点の加算のため、順序で最下位ビットが変わりうる。
    """
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
