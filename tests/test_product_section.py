"""dashboard の「Codeと他プロダクトの需要」（ビューモデルと表示条件）。

このセクションは product 利用特徴量を並べ替えて書式を付けるだけで、金額を計算し
直さない。固定するのは次の3つ。

- 確定できなかった値を 0 や空文字で埋めず — にすること（usage-summary.csv と同じ
  規則。0 で埋めると「観測した結果が 0 だった」ことと区別できなくなる）
- 組織サマリの合計が、Code と全需要の両方が確定した行だけを足していること
- 読み手が得るもののない組織（Code の需要が1人も確定しない）ではセクションを出さない

テンプレート側の描画は tests/test_partials.py と golden が受け持つ。
"""

import pandas as pd
import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.product_usage import FEATURE_COLUMNS, ProductUsage, compute
from seat_analyzer.report import write_html
from seat_analyzer.report.html import _product_view

from .conftest import SPEND_HEADER, spend_row

# features の dtype。compute の出力に揃っていることは
# test_feature_dtypes_match_compute が確かめる
FEATURE_DTYPES = {
    "total_demand_usd": "Float64",
    "code_demand_usd": "Float64",
    "code_demand_share": "Float64",
    "total_requests": "Float64",
    "code_requests": "Float64",
    "product_breadth": "Int64",
    "supplementary_high": "boolean",
    "prohibited_observed": "boolean",
}

# 表示に使う閾値（既定と同じ額。額そのものの出どころは
# test_threshold_amount_comes_from_the_config が確かめる）
THRESHOLD = 100.0

_UNSET = object()


def person(total, code, *, share=_UNSET, breadth=2, high=False) -> tuple:
    """1人分の特徴量。

    share は省略すると compute と同じ規則（code / total・分母 0 は欠損）で埋める。
    回数と禁止指定はこのセクションが読まない列なので固定値にする。
    """
    if share is _UNSET:
        undefined = pd.isna(total) or pd.isna(code) or total == 0
        share = pd.NA if undefined else code / total
    return (total, code, share, 100.0, 80.0, breadth, high, False)


def features(rows: dict[str, tuple]) -> pd.DataFrame:
    """email → 8 特徴量の組 から features を組み立てる。"""
    df = pd.DataFrame.from_dict(rows, orient="index", columns=list(FEATURE_COLUMNS))
    df.index.name = "email"
    return df.astype(FEATURE_DTYPES)


def view(rows: dict[str, tuple], threshold: float = THRESHOLD) -> dict | None:
    return _product_view(ProductUsage(features=features(rows), issues=[]), threshold)


def column(v: dict, key: str) -> list:
    return [r[key] for r in v["rows"]]


def test_feature_dtypes_match_compute(cfg):
    """テストが組み立てる features の型が compute の出力と同じであること。"""
    computed = compute(
        pd.DataFrame([("a@x.jp", 1.0, 1.0, "Claude Code")],
                     columns=["email", "cost_usd", "requests", "product"]),
        cfg["product_policy"],
    )
    assert dict(computed.features.dtypes.astype(str)) == FEATURE_DTYPES


# --- 並び ---

def test_rows_are_ordered_by_demand_with_email_tiebreak():
    """需要（計）の降順・同額は email 昇順。需要の分からない行は末尾へ回す。"""
    v = view({
        "b@x.jp": person(100.0, 50.0),
        "a@x.jp": person(100.0, 50.0),
        "c@x.jp": person(300.0, 150.0),
        "z@x.jp": person(pd.NA, 10.0),
        "y@x.jp": person(pd.NA, 20.0),
    })
    assert column(v, "email") == [
        "c@x.jp", "a@x.jp", "b@x.jp", "y@x.jp", "z@x.jp"]


# --- 確定できなかった値の扱い ---

def test_unconfirmed_values_are_dashes_not_zeros():
    """欠けた特徴量は列でも値ラベルでも —（0 で埋めない）。"""
    v = view({
        "a@x.jp": person(200.0, pd.NA, breadth=pd.NA),   # 需要は分かるが内訳が不明
        "b@x.jp": person(pd.NA, 40.0),                   # 需要そのものが不明
    })
    unknown_split, unknown_total = v["rows"]
    assert unknown_split["email"] == "a@x.jp"
    assert (unknown_split["total_fmt"], unknown_split["code_fmt"]) == ("$200", "—")
    assert (unknown_split["share_fmt"], unknown_split["other_fmt"]) == ("—", "—")
    assert unknown_split["breadth_fmt"] == "—"
    assert unknown_split["val_fmt"] == "$200 (Code —)"
    assert unknown_total["total_fmt"] == "—"
    assert unknown_total["code_fmt"] == "$40.00"        # 確定した値はそのまま出す
    assert unknown_total["val_fmt"] == "—"


def test_other_demand_is_the_difference_of_confirmed_values():
    """他product需要は需要（計） − Code需要。片方でも欠ければ —。"""
    v = view({"a@x.jp": person(250.0, 100.0), "b@x.jp": person(200.0, pd.NA)})
    assert column(v, "other_fmt") == ["$150", "—"]


def test_share_is_shown_as_a_whole_percent():
    """Code比率は整数パーセント。"""
    v = view({"a@x.jp": person(400.0, 302.0)})          # 75.5%
    assert column(v, "share_fmt") == ["76%"]


# --- 補助プロダクトの印 ---

def test_flag_marks_only_confirmed_true():
    """⚑ は真のときだけ。偽と「分からない」はどちらも無印。"""
    v = view({
        "a@x.jp": person(300.0, 10.0, high=True),
        "b@x.jp": person(200.0, 10.0, high=False),
        "c@x.jp": person(100.0, 10.0, high=pd.NA),
    })
    assert column(v, "flag") == [True, False, False]


# --- 組織サマリ1行 ---

def test_summary_line_totals_confirmed_rows():
    v = view({"a@x.jp": person(300.0, 150.0), "b@x.jp": person(100.0, 50.0)})
    assert v["summary_line"] == "Code需要 $200 / 全需要 $400（50%）・対象 2名"


def test_summary_line_notes_how_many_rows_the_total_covers():
    """内訳の確定しない行があるときは、合計が何名分かを併記する。"""
    v = view({"a@x.jp": person(300.0, 150.0), "b@x.jp": person(100.0, pd.NA)})
    assert v["summary_line"] == (
        "Code需要 $150 / 全需要 $300（50%）・対象 2名・金額は内訳の確定した 1名の合計"
    )


def test_summary_line_is_absent_when_no_row_has_both_values():
    """足せる行が1つも無ければサマリ行そのものを出さない（表と棒は出す）。"""
    v = view({"a@x.jp": person(pd.NA, 40.0)})
    assert v["summary_line"] is None
    assert column(v, "email") == ["a@x.jp"]


def test_summary_line_omits_the_ratio_without_demand():
    """分母が 0 の比率は定義できないので出さない。"""
    v = view({"a@x.jp": person(0.0, 0.0)})
    assert v["summary_line"] == "Code需要 $0.00 / 全需要 $0.00・対象 1名"


# --- 棒 ---

def test_bars_are_scaled_to_the_largest_confirmed_demand():
    """棒の長さは需要（計）／確定した最大需要。色の切り替え位置は Code比率。"""
    v = view({"a@x.jp": person(400.0, 100.0), "b@x.jp": person(100.0, 100.0)})
    assert column(v, "bar_pct") == [100.0, 25.0]
    assert column(v, "split_pct") == [25.0, 100.0]
    assert column(v, "bar_kind") == ["split", "split"]


def test_bar_kind_falls_back_when_values_are_missing():
    """内訳が不明なら斜線、需要そのものが不明なら棒を描かない。"""
    v = view({"a@x.jp": person(200.0, pd.NA), "b@x.jp": person(pd.NA, 40.0)})
    assert column(v, "bar_kind") == ["unknown", "none"]
    assert column(v, "bar_pct") == [100.0, 0.0]         # 斜線の長さは需要どおり


def test_negative_demand_does_not_draw_a_backwards_bar():
    """需要が負のユーザ（返金等）でも描ける幅に丸める。"""
    v = view({"a@x.jp": person(200.0, 100.0), "b@x.jp": person(-50.0, -50.0)})
    assert column(v, "bar_pct") == [100.0, 0.0]


def test_bar_scale_does_not_divide_by_zero():
    """需要が全員 0 でも落ちない（棒は出ないだけ）。"""
    v = view({"a@x.jp": person(0.0, 0.0), "b@x.jp": person(0.0, 0.0)})
    assert column(v, "bar_pct") == [0.0, 0.0]


# --- 表示条件 ---

@pytest.mark.parametrize("rows, label", [
    ({}, "利用のあるユーザが1人もいない"),
    ({"a@x.jp": person(200.0, pd.NA)}, "Code の需要が1人も確定しない"),
])
def test_section_is_omitted_when_it_would_say_nothing(rows, label):
    assert view(rows) is None, label


def test_section_is_omitted_without_product_features():
    """速報や product 特徴量を持たない結果ではセクションを出さない。"""
    assert _product_view(None, THRESHOLD) is None


# --- 設定から表示までの経路 ---

def test_threshold_amount_comes_from_the_config(cfg, make_input, tmp_path):
    """⚑ の凡例に出る金額は設定 → summary → テンプレートの順に運ぶ。"""
    cfg["product_policy"]["supplementary_high_usd"] = 42.0
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 300.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert result.summary["supplementary_high_usd"] == 42.0

    out = tmp_path / "dashboard.html"
    write_html(result, out)
    html = out.read_text(encoding="utf-8")
    assert "<h2>Codeと他プロダクトの需要（API換算）</h2>" in html
    assert "の需要が $42.00 以上" in html


def test_dashboard_renders_without_a_product_column(cfg, tmp_path):
    """product 列の無いスペンドでは、セクションが出ないだけで他は崩れない。

    列が無いと Code の需要は誰の分も確定しないため、棒も比率も全部 — になる。
    その状態のセクションは読み手に何も渡さないので出さない（列が無い理由は CLI の
    CAPACITY_SIGNAL_UNAVAILABLE 警告が担う）。
    """
    def _drop_product(line: str) -> str:
        cells = line.split(",")
        return ",".join(cells[:2] + cells[3:])

    input_dir = tmp_path / "input"
    spend = input_dir / "spend" / "spend_2026-06.csv"
    spend.parent.mkdir(parents=True, exist_ok=True)
    spend.write_text(
        _drop_product(SPEND_HEADER) + "\n"
        + _drop_product(spend_row("a@x.jp", 300.0, net=0.0)) + "\n",
        encoding="utf-8",
    )
    members = input_dir / "members" / "members_2026-06.csv"
    members.parent.mkdir(parents=True, exist_ok=True)
    members.write_text("Email,Seat Type\na@x.jp,Premium\n", encoding="utf-8")

    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    out = tmp_path / "dashboard.html"
    write_html(result, out)
    html = out.read_text(encoding="utf-8")
    assert "Codeと他プロダクトの需要" not in html
    assert "<h2>推奨一覧</h2>" in html          # 他のセクションはそのまま出る
