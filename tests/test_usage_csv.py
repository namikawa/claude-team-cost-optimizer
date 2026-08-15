"""usage-summary.csv の書き出し（列構成・セルの書式・欠損の扱い）。"""

import csv
import io
from pathlib import Path

import pandas as pd
import pytest

from seat_analyzer.analyze import AnalysisResult
from seat_analyzer.product_usage import FEATURE_COLUMNS, ProductUsage, compute
from seat_analyzer.report import write_usage_csv

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

# 全 8 特徴量が確定した1人分の値（書式の検証で使う基準行）
FILLED = (400.0, 300.0, 0.75, 1000.0, 900.0, 2, True, False)
# 1つも確定しなかった1人分の値
ALL_MISSING = (pd.NA,) * 8


def features(rows: dict[str, tuple]) -> pd.DataFrame:
    """email → 8 特徴量の組 から features を組み立てる。"""
    df = pd.DataFrame.from_dict(rows, orient="index", columns=list(FEATURE_COLUMNS))
    df.index.name = "email"
    return df.astype(FEATURE_DTYPES)


def result(rows: dict[str, tuple], users: list[str] | None = None) -> AnalysisResult:
    """features だけを持つ分析結果（他の項目は書き出しに関与しない）。"""
    return AnalysisResult(
        month="2026-06",
        users=pd.DataFrame({"email": users or list(rows)}),
        summary={},
        org="org-a",
        product_usage=ProductUsage(features=features(rows), issues=[]),
    )


def write(res: AnalysisResult, tmp_path: Path) -> Path:
    path = tmp_path / "usage-summary.csv"
    write_usage_csv(res, path)
    return path


def read_rows(path: Path) -> list[list[str]]:
    """CSV を行ごとのセル列として読む（BOM を外し、改行は原文のまま渡す）。"""
    text = path.read_bytes().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def cells(path: Path, email: str) -> dict[str, str]:
    rows = read_rows(path)
    header = rows[0]
    row = next(r for r in rows[1:] if r[0] == email)
    return dict(zip(header, row))


def test_header_is_email_plus_feature_columns(tmp_path):
    path = write(result({"alice@x.jp": FILLED}), tmp_path)
    assert read_rows(path)[0] == ["email", *FEATURE_COLUMNS]


def test_feature_dtypes_match_compute(cfg):
    """テストが組み立てる features の型が compute の出力と同じであること。"""
    computed = compute(
        pd.DataFrame([("alice@x.jp", 1.0, 1.0, "Claude Code")],
                     columns=["email", "cost_usd", "requests", "product"]),
        cfg["product_policy"],
    ).features
    assert {c: str(t) for c, t in computed.dtypes.items()} == FEATURE_DTYPES


def test_rows_follow_features_without_adding_or_dropping(tmp_path):
    """行は features のまま。利用ゼロのメンバー（明細に無いユーザ）は行を持たない。"""
    res = result(
        {"alice@x.jp": FILLED, "bob@x.jp": ALL_MISSING},
        users=["alice@x.jp", "bob@x.jp", "carol@x.jp"],   # 判定対象には3人いる
    )
    rows = read_rows(write(res, tmp_path))
    assert [r[0] for r in rows[1:]] == ["alice@x.jp", "bob@x.jp"]


def test_missing_values_are_blank(tmp_path):
    """確定できなかった値は空欄（0 や False で埋めない）。"""
    path = write(result({"alice@x.jp": ALL_MISSING}), tmp_path)
    row = cells(path, "alice@x.jp")
    assert [row[c] for c in FEATURE_COLUMNS] == [""] * 8


def test_number_and_flag_formats(tmp_path):
    path = write(result({"alice@x.jp": FILLED}), tmp_path)
    row = cells(path, "alice@x.jp")
    assert row["total_demand_usd"] == "400.00"       # 金額は小数2桁
    assert row["code_demand_usd"] == "300.00"
    assert row["code_demand_share"] == "0.7500"      # 構成比は小数4桁
    assert row["total_requests"] == "1000"           # 整数の回数に .0 を付けない
    assert row["code_requests"] == "900"
    assert row["product_breadth"] == "2"
    assert row["supplementary_high"] == "True"
    assert row["prohibited_observed"] == "False"


def test_non_integer_requests_keep_their_value(tmp_path):
    """回数は入力の数値列の合計なので整数とは限らない。値を丸めずに出す。"""
    path = write(result({"alice@x.jp": (1.0, 1.0, 1.0, 2.5, 0.5, 1, False, False)}),
                 tmp_path)
    row = cells(path, "alice@x.jp")
    assert row["total_requests"] == "2.5"
    assert row["code_requests"] == "0.5"


def test_negative_amount_is_written_as_is(tmp_path):
    """負の金額に式のエスケープを掛けない（掛けると値が壊れる）。"""
    path = write(result({"alice@x.jp": (-12.5, -12.5, 1.0, 1.0, 1.0, 1, False, False)}),
                 tmp_path)
    row = cells(path, "alice@x.jp")
    assert row["total_demand_usd"] == "-12.50"
    assert row["code_demand_usd"] == "-12.50"


@pytest.mark.parametrize(
    "email", ["=cmd@x.jp", "+cmd@x.jp", "-cmd@x.jp", "@cmd@x.jp", "\tcmd@x.jp"])
def test_email_that_looks_like_a_formula_is_escaped(tmp_path, email):
    path = write(result({email: FILLED}), tmp_path)
    assert read_rows(path)[1][0] == "'" + email


def test_email_newlines_are_normalized_to_lf(tmp_path):
    """セル内の改行も LF に揃える（レコード区切りの指定だけでは CR が残る）。"""
    path = write(result({"alice@x.jp\r\nbob@x.jp\rcarol@x.jp": FILLED}), tmp_path)
    assert b"\r" not in path.read_bytes()
    assert read_rows(path)[1][0] == "alice@x.jp\nbob@x.jp\ncarol@x.jp"


def test_email_starting_with_cr_is_escaped_before_normalizing(tmp_path):
    """CR 始まりのセルは式のエスケープが先に効く。

    改行を先に均すと式の先頭文字と一致しなくなり、引用符が付かないまま出る。
    """
    path = write(result({"\ralice@x.jp": FILLED}), tmp_path)
    assert b"\r" not in path.read_bytes()
    assert read_rows(path)[1][0] == "'\nalice@x.jp"


def test_file_has_bom_and_lf_newlines(tmp_path):
    path = write(result({"alice@x.jp": FILLED, "bob@x.jp": ALL_MISSING}), tmp_path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")   # Excel が UTF-8 として開けるように
    assert b"\r" not in raw
    assert raw.count(b"\n") == 3             # ヘッダ + 2行（末尾改行あり）


def test_empty_features_writes_header_only(tmp_path):
    """明細に1人も現れなかった月でもヘッダだけのファイルを作る。"""
    path = write(result({}), tmp_path)
    assert read_rows(path) == [["email", *FEATURE_COLUMNS]]


def test_missing_product_usage_raises(tmp_path):
    """特徴量を持たない分析結果は黙って飛ばさずエラーにする（常に生成するため）。"""
    res = AnalysisResult(month="2026-06", users=pd.DataFrame(), summary={}, org="org-a")
    with pytest.raises(ValueError) as e:
        write_usage_csv(res, tmp_path / "usage-summary.csv")
    assert "product 利用特徴量" in str(e.value)


def test_features_without_product_column_are_blank(cfg, tmp_path):
    """product 列の無い入力では、product に依る特徴量が空欄になる。"""
    usage = compute(
        pd.DataFrame([("alice@x.jp", 40.0, 4.0)],
                     columns=["email", "cost_usd", "requests"]),
        cfg["product_policy"],
    )
    res = AnalysisResult(month="2026-06", users=pd.DataFrame(), summary={}, org="org-a",
                         product_usage=usage)
    row = cells(write(res, tmp_path), "alice@x.jp")
    # 全 product の需要・回数は product 名に依らないので確定する
    assert row["total_demand_usd"] == "40.00"
    assert row["total_requests"] == "4"
    assert row["code_demand_usd"] == ""
    assert row["code_demand_share"] == ""
    assert row["code_requests"] == ""
    assert row["product_breadth"] == ""
