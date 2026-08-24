"""追加クレジット（usage credits）のユーザ単位対応のテスト。

値パース・モード導出・cap_suspected 抑制・κ 到達/整合性警告・E 分布・付与候補・
構成サマリ・後方互換をカバーする。判定ロジック（推奨・ヒステリシス）の数値は変えない。
"""

import math
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pandas as pd
import pytest

from seat_analyzer.analyze import (
    CREDIT_DISABLED,
    CREDIT_ENABLED,
    CREDIT_UNKNOWN,
    analyze,
    credits_mode,
    preview,
)
from seat_analyzer.analyze.credits import _compute_e_distribution
from seat_analyzer.ingest import (
    is_number_text,
    load_members_info,
    load_members_info_file,
    parse_credit_limit,
)
from seat_analyzer.report import (
    PREVIEW,
    PREVIEW_DASHBOARD,
    write_details,
    write_html,
    write_markdown,
    write_preview,
)

from .conftest import spend_row


def _write_info(input_dir: Path, text: str, org: str | None = None) -> None:
    base = input_dir / org if org else input_dir
    (base / "members-info.csv").write_text(text, encoding="utf-8")


# --- 値パース -------------------------------------------------------------

def test_parse_credit_limit_positive():
    assert parse_credit_limit("250") == (250.0, None)
    assert parse_credit_limit("$1,500") == (1500.0, None)
    assert parse_credit_limit(150) == (150.0, None)


def test_parse_credit_limit_zero_disabled():
    assert parse_credit_limit("0") == (0.0, None)
    assert parse_credit_limit(0) == (0.0, None)


def test_parse_credit_limit_unlimited():
    v, w = parse_credit_limit("無制限")
    assert math.isinf(v) and w is None
    assert math.isinf(parse_credit_limit("unlimited")[0])


def test_parse_credit_limit_blank_is_nan():
    v, w = parse_credit_limit("")
    assert math.isnan(v) and w is None
    assert math.isnan(parse_credit_limit(None)[0])
    assert math.isnan(parse_credit_limit(float("nan"))[0])


def test_parse_credit_limit_invalid_warns():
    v, w = parse_credit_limit("たくさん")
    assert math.isnan(v) and w is not None
    v2, w2 = parse_credit_limit("-100")
    assert math.isnan(v2) and w2 is not None


@pytest.mark.parametrize("cell", ["Infinity", "+inf", "-inf", "1e309", "+nan"])
def test_parse_credit_limit_non_finite_number_warns(cell):
    """「無制限」は語彙で書かれた場合だけ。数値として非有限になる値は解釈不能に倒す。

    ここを通すと、上限の書き間違いが警告なしで正反対の「無制限」になる。
    """
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


@pytest.mark.parametrize("cell", [float("inf"), float("-inf")])
def test_parse_credit_limit_non_finite_float_warns(cell):
    """数値型で渡された無限大も解釈不能に倒す（str(inf) は「無制限」の語彙に一致する）。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


def _scalar(value, dtype: str):
    """固定幅のスカラーを pandas 経由で作る。

    この関数へ実際に渡ってくる値は CSV を読んだ表のセルなので、値の出所をそれに揃える
    （型を直接 import して作ると、実運用で来る型と違うものを検証しかねない）。
    """
    return pd.Series([value], dtype=dtype).iloc[0]


class _FloatLike:
    """float() で無限大になるが、数の階層に属さない型。

    数値経路の入口（数として読める値かの判定）で落ちる。型を問わず float() へ通す実装
    では素通りし、str() の "inf" が「無制限」の語彙に一致してしまう。
    """

    def __float__(self) -> float:
        return float("inf")

    def __str__(self) -> str:
        return "inf"


@pytest.mark.parametrize("cell", [
    Decimal("Infinity"),
    _FloatLike(),
    10 ** 1000,
    _scalar(float("inf"), "float32"),   # 固定幅の浮動小数（数として読める側の無限大）
])
def test_parse_credit_limit_non_float_infinity_warns(cell):
    """float 以外の型の無限大・float へ収まらない巨大整数も、例外にせず不明 + 警告。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


@pytest.mark.parametrize("cell,expected", [
    (250, 250.0), (250.0, 250.0), (0, 0.0), (Decimal("125.5"), 125.5),
    (Fraction(1, 2), 0.5),
    (_scalar(1.5, "float32"), 1.5),
    (_scalar(3, "int64"), 3.0),
])
def test_parse_credit_limit_numeric_types_are_read(cell, expected):
    """実数として扱える有限値は型を問わず従来どおり読む。"""
    assert parse_credit_limit(cell) == (expected, None)


@pytest.mark.parametrize("cell", [250 + 7j, _scalar(250 + 7j, "complex64")])
def test_parse_credit_limit_complex_warns(cell):
    """虚部を持つ値は金額として読まない（float() が虚部を黙って捨てる）。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


def test_parse_credit_limit_signaling_nan_warns():
    """欠損かどうかを問うだけで例外になる値も、落ちずに不明 + 警告になる。"""
    v, w = parse_credit_limit(Decimal("sNaN"))
    assert math.isnan(v) and w is not None


@pytest.mark.parametrize("cell", [True, False])
def test_parse_credit_limit_bool_warns(cell):
    """真偽値は上限として読まない（float(True) = 1.0 を κ にしない）。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


class _BoolLike:
    """float() で 1.0 になるが数ではない型。

    読み取りライブラリが返す真偽値型は builtin の bool のサブクラスでないことがあり、
    bool を除くだけでは落ちない。数として書かれていない値を金額にしないこと。
    """

    def __float__(self) -> float:
        return 1.0

    def __str__(self) -> str:
        return "True"


@pytest.mark.parametrize("cell", [
    b"250",
    _BoolLike(),
    pd.Series([True]).to_numpy()[0],   # 固定幅の真偽値（bool のサブクラスではない）
])
def test_parse_credit_limit_non_number_objects_warn(cell):
    """数として書かれていない値は金額にしない（bytes・真偽値風の型）。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


@pytest.mark.parametrize("cell", [None, float("nan"), pd.NA, pd.NaT, Decimal("NaN")])
def test_parse_credit_limit_missing_values_are_unknown_without_warning(cell):
    """未記入は型を問わず不明（警告なし）。0 や無制限へは倒さない。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is None


@pytest.mark.parametrize("cell", ["￥1,500", "¥1500", "1500￥"])
def test_parse_credit_limit_yen_warns(cell):
    """円記号つきのセルは USD の金額として通さない（通貨の取り違えは桁が変わる）。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


def test_parse_credit_limit_dollar_signs_still_accepted():
    """$ と ＄ の表記は従来どおり許容する。"""
    assert parse_credit_limit("＄250") == (250.0, None)
    assert parse_credit_limit("$250") == (250.0, None)


@pytest.mark.parametrize("cell", ["1_0", "1__0", "_10", "10_", "1e+_2"])
def test_parse_credit_limit_underscore_warns(cell):
    """数値の字句に "_" を書いた値は読まない（数値変換は桁区切りとして無視してしまう）。"""
    v, w = parse_credit_limit(cell)
    assert math.isnan(v) and w is not None


@pytest.mark.parametrize("text,ok", [
    ("250", True), ("7.0", True), ("1E+2", True), (".5", True), ("1500.", True),
    ("1.5e-3", True),
    ("-1", True),          # 字句としては数値（負値の判定は呼び出し側）
    ("1E+9999", True),     # 指数は4桁まで受ける
    ("1E+99999", False),   # 5桁以上は受けない（整数へ写すと桁数が実体化と表示に耐えない）
    ("1E+999999999", False),
    ("1_0", False), ("1__0", False), ("_10", False), ("10_", False), ("1e+_2", False),
    ("２５０", False),      # 全角数字（数値変換は受けてしまう）
    ("1 0", False), ("", False), ("inf", False), ("1e", False),
])
def test_number_text_rule(text, ok):
    """数値として受ける字句の規則（3つの解釈経路が共有する1つの規則）。"""
    assert is_number_text(text) is ok


def _load_limits(tmp_path: Path, rows: str, cfg: dict) -> tuple[dict, list[str]]:
    """members-info を1本置いて (email→κ, 警告) を返す（CSV から読む経路の検証用）。"""
    (tmp_path / "members-info.csv").write_text(
        "email,追加クレジット上限\n" + rows, encoding="utf-8", newline="\n")
    result = load_members_info(tmp_path, cfg)
    limits = dict(zip(result.df["email"], result.df["credit_limit_usd"], strict=True))
    return limits, result.warnings


@pytest.mark.parametrize("cell", ["Infinity", "1e309", "-Infinity"])
def test_members_info_non_finite_limit_is_unknown_with_warning(tmp_path, cfg, cell):
    """上限列が数値だけの表でも、非有限の値は「無制限」にならず不明 + 警告になる。

    読み取りで数値へ変換されると、この値は parse_credit_limit に届く前に無限大へ変わり
    「無制限」と区別できなくなる。CSV から読む経路でそうなっていないことを見る。
    """
    limits, warns = _load_limits(tmp_path, f"a@x.jp,{cell}\nb@x.jp,{cell}\n", cfg)

    assert math.isnan(limits["a@x.jp"]) and math.isnan(limits["b@x.jp"])
    assert len(warns) == 2
    assert all("解釈できません" in w for w in warns)


def test_members_info_explicit_unlimited_and_amounts_are_unchanged(tmp_path, cfg):
    """明示語彙の無制限と通常の金額は従来どおり（文字列で読む変更の副作用がないこと）。"""
    limits, warns = _load_limits(
        tmp_path, "a@x.jp,inf\nb@x.jp,無制限\nc@x.jp,250\nd@x.jp,0\ne@x.jp,\n", cfg)

    assert math.isinf(limits["a@x.jp"]) and math.isinf(limits["b@x.jp"])
    assert limits["c@x.jp"] == 250.0
    assert limits["d@x.jp"] == 0.0
    assert math.isnan(limits["e@x.jp"])
    assert warns == []


def test_members_info_keeps_text_cells_verbatim(tmp_path, cfg):
    """部署・備考などの文字列列は字句のまま保つ（数値として読むと先頭ゼロが落ちる）。"""
    (tmp_path / "members-info.csv").write_text(
        "email,部署,備考\na@x.jp,0123,0250\n", encoding="utf-8", newline="\n")
    row = load_members_info(tmp_path, cfg).df.set_index("email").loc["a@x.jp"]

    assert row["department"] == "0123"
    assert row["note"] == "0250"


@pytest.mark.parametrize("cell,expected_unknown", [("Infinity", True), ("inf", False)])
def test_members_info_file_reads_limits_the_same_way(tmp_path, cfg, cell, expected_unknown):
    """月中の κ 差分で使う単ファイル読みも同じ規則で読む。"""
    path = tmp_path / "members-info-2026-08-01.csv"
    path.write_text(f"email,追加クレジット上限\na@x.jp,{cell}\n",
                    encoding="utf-8", newline="\n")
    value = load_members_info_file(path, cfg).set_index("email").loc["a@x.jp",
                                                                    "credit_limit_usd"]

    assert math.isnan(value) is expected_unknown
    assert math.isinf(value) is not expected_unknown


def test_members_info_yen_limit_is_unknown_with_warning(make_input, cfg):
    """members-info に円記号つきの上限が書かれていたら不明として扱い、警告に出す。

    κ が有効（$1,500）として通ると ⚠️上限フラグが抑制される。不明のままなら残るので、
    フラグの側から「金額として通っていない」ことを見る。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 45.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,￥1500\n")
    result = analyze(input_dir, "2026-06", cfg, org="org-a")

    assert any("解釈できません" in w and "a@x.jp" in w for w in result.warnings)
    by = result.users.set_index("email")
    assert bool(by.loc["a@x.jp", "cap_suspected"]) is True


# --- モード導出 -----------------------------------------------------------

def test_credits_mode_by_kappa():
    assert credits_mode(250.0, False) == CREDIT_ENABLED
    assert credits_mode(float("inf"), False) == CREDIT_ENABLED
    assert credits_mode(0.0, False) == CREDIT_DISABLED
    assert credits_mode(float("nan"), False) == CREDIT_UNKNOWN


def test_credits_mode_auto_enable_from_billing():
    # κ 未設定でも実課金が観測されていれば enabled と自動確定
    assert credits_mode(float("nan"), True) == CREDIT_ENABLED


# --- cap_suspected の抑制 --------------------------------------------------

def test_cap_suspected_suppressed_for_enabled_kept_for_disabled(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 45.0, net=0.0),
                     spend_row("b@x.jp", 45.0, net=0.0),
                     spend_row("c@x.jp", 45.0, net=0.0)]},
        members=["a@x.jp,Standard", "b@x.jp,Standard", "c@x.jp,Standard"],
    )
    _write_info(
        input_dir,
        "email,追加クレジット上限\na@x.jp,無制限\nb@x.jp,0\nc@x.jp,\n",
    )
    by = analyze(input_dir, "2026-06", cfg, org="org-a").users.set_index("email")
    # 需要 45 >= 0.85*50 で実課金 0 の Standard → 本来 cap_suspected
    assert bool(by.loc["a@x.jp", "cap_suspected"]) is False   # enabled → 抑制
    assert bool(by.loc["b@x.jp", "cap_suspected"]) is True    # disabled → 維持
    assert bool(by.loc["c@x.jp", "cap_suspected"]) is True    # unknown → 維持
    assert by.loc["a@x.jp", "credits_mode"] == CREDIT_ENABLED
    assert by.loc["b@x.jp", "credits_mode"] == CREDIT_DISABLED
    assert by.loc["c@x.jp", "credits_mode"] == CREDIT_UNKNOWN


# --- κ 到達・整合性警告 ----------------------------------------------------

def test_credit_reach_warning(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=48.0)]},
        members=["a@x.jp,Premium"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,50\n")
    warns = analyze(input_dir, "2026-06", cfg, org="org-a").warnings
    assert any("上限到達" in w and "a@x.jp" in w for w in warns)


def test_credit_reach_requires_billing(make_input, cfg):
    # κ ≤ cap_tolerance_usd だと billed ≥ κ − tol が実課金ゼロでも成立してしまう。
    # 到達には課金の発生が必要なので、実課金 $0 は κ の大小によらず到達扱いにしない
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,5\n")
    warns = analyze(input_dir, "2026-06", cfg, org="org-a").warnings
    assert not any("上限到達" in w for w in warns)


def test_credit_reach_small_cap_billed(make_input, cfg):
    # κ ≤ tolerance でも課金が実際に κ へ達していれば検出する（billed>0 ガードの偽陰性がないこと）
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 30.0, net=5.0)]},
        members=["a@x.jp,Premium"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,5\n")
    warns = analyze(input_dir, "2026-06", cfg, org="org-a").warnings
    assert any("上限到達" in w and "a@x.jp" in w for w in warns)


def test_integrity_over_cap(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 300.0, net=120.0)]},
        members=["a@x.jp,Premium"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,50\n")
    warns = analyze(input_dir, "2026-06", cfg, org="org-a").warnings
    assert any("上限 κ を超過" in w and "a@x.jp" in w for w in warns)


def test_integrity_disabled_but_billed(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=80.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")
    warns = analyze(input_dir, "2026-06", cfg, org="org-a").warnings
    assert any("無効（κ=0）" in w and "a@x.jp" in w for w in warns)


# --- E 分布 ---------------------------------------------------------------

def test_e_distribution_present_with_billers(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=60.0),
                     spend_row("b@x.jp", 100.0, net=0.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    ed = result.e_distribution
    assert ed is not None
    prem = next(g for g in ed["groups"] if g["seat"] == "premium")
    row = next(r for r in prem["rows"] if r["email"] == "a@x.jp")
    assert row["e"] == 140.0   # 200 需要 − 60 実課金
    out = tmp_path / "details.md"
    write_details(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 込み枠の実測（E = API換算需要 − 実課金）" in md
    # 実課金ゼロの b は billers に含めない
    assert "b@x.jp" not in md.split("込み枠の実測")[1].split("## ")[0]


def test_e_distribution_absent_without_billers(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 50.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert result.e_distribution is None
    out = tmp_path / "details.md"
    write_details(result, out)
    assert "込み枠の実測" not in out.read_text(encoding="utf-8")


def test_e_distribution_none_for_net_spend_basis(make_input, cfg):
    # 修正1: cost_basis=net_spend では需要=課金となり E が無意味なので算出しない
    cfg_net = {**cfg, "cost_basis": "net_spend"}
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=60.0)]},
        members=["a@x.jp,Premium"],
    )
    assert analyze(input_dir, "2026-06", cfg_net, org="org-a").e_distribution is None


def test_e_distribution_ratio_comparison(make_input, cfg, tmp_path):
    # 改善5: シート種別ごとに実測 E 中央値と config allowance(mid) の倍率を添える
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 150.0, net=50.0)]},   # E=100, premium
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    g = next(x for x in result.e_distribution["groups"] if x["seat"] == "premium")
    assert g["median"] == 100.0
    assert g["allowance_mid"] == 250.0
    assert g["ratio"] == 0.4    # 100 / 250
    out = tmp_path / "details.md"
    write_details(result, out)
    assert "config の allowance（mid $250.00）の 0.4 倍" in out.read_text(encoding="utf-8")


def test_e_distribution_row_order_is_independent_of_input_row_order(make_input, cfg):
    """E が完全に同点のユーザがいても、行順が入力の行順に依存しないこと。

    タイブレークが無いと同点行の並びが入力順のまま残り、レポートの行順が実行環境で
    変わりうる。email 昇順で一意に決まることを固定する。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("tie-b@x.jp", 200.0, net=60.0),
                     spend_row("tie-a@x.jp", 200.0, net=60.0)]},   # E=140 で同点
        members=["tie-a@x.jp,Premium", "tie-b@x.jp,Premium"],
    )
    users = analyze(input_dir, "2026-06", cfg, org="org-a").users
    orders = (users, users.iloc[::-1],
              users.sort_values("email"), users.sort_values("email", ascending=False))
    for frame in orders:
        groups = _compute_e_distribution(frame, cfg)["groups"]
        prem = next(g for g in groups if g["seat"] == "premium")
        assert [r["e"] for r in prem["rows"]] == [140.0, 140.0]   # 同点の前提を確認
        assert [r["email"] for r in prem["rows"]] == ["tie-a@x.jp", "tie-b@x.jp"]


# --- 付与候補 -------------------------------------------------------------

def test_grant_candidate_formal(make_input, cfg, tmp_path):
    # disabled の Standard ユーザで純モデル判定が premium 方向 → 付与候補（超過見込みつき）
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 260.0, net=130.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert [c["email"] for c in result.grant_candidates] == ["a@x.jp"]
    assert result.grant_candidates[0]["added"] == 210.0   # max(0, 260 − 50)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 追加クレジット付与候補" in md
    assert "モデル超過見込み $210.00/月" in md


def test_grant_cap_legend_keeps_decimals_in_html(make_input, cfg, tmp_path):
    """凡例の推奨初期上限は $100 以上でも小数部を落とさない（正式・速報とも）。

    上限は設定値で、その額との比較の意味を持つ。_fmt_compact の「$100 以上は整数」で
    丸めると、$150.50 の設定が凡例で $150 になり実際の付与額と食い違う。
    """
    cfg["usage_credits"]["grant_suggested_cap_usd"] = 150.5
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 260.0, net=130.0)]},
        members=["a@x.jp,Standard"],
        members_month="2026-06",
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")

    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    out = tmp_path / "dashboard.html"
    write_html(result, out)
    assert "推奨初期上限 $150.50" in out.read_text(encoding="utf-8")

    pv = preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a")
    pv_dir = tmp_path / "org"
    write_preview(pv, pv_dir)
    html = PREVIEW_DASHBOARD.path(pv_dir, "2026-06", "org-a").read_text(encoding="utf-8")
    assert "推奨初期上限 $150.50" in html


def test_grant_candidate_disabled_zero_billed_high_demand(make_input, cfg):
    # 修正2の本命: 無効・実課金ゼロ・高需要。実課金拘束後は Standard 推奨だが、
    # 拘束前の純モデル判定は premium 方向のため付与候補に入る（旧ロジックでは漏れていた層）
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    by = result.users.set_index("email")
    assert by.loc["a@x.jp", "recommended_seat"] == "standard"   # 拘束後は Standard 推奨
    assert [c["email"] for c in result.grant_candidates] == ["a@x.jp"]   # でも付与候補
    assert result.grant_candidates[0]["added"] == 150.0


def test_grant_candidates_sorted_by_overage(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("low@x.jp", 160.0, net=0.0),
                     spend_row("high@x.jp", 240.0, net=0.0)]},
        members=["low@x.jp,Standard", "high@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\nlow@x.jp,0\nhigh@x.jp,0\n")
    cands = analyze(input_dir, "2026-06", cfg, org="org-a").grant_candidates
    # モデル超過見込みの降順
    assert [c["email"] for c in cands] == ["high@x.jp", "low@x.jp"]
    assert cands[0]["added"] == 190.0 and cands[1]["added"] == 110.0


def test_grant_candidate_absent_without_credit_column(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 260.0, net=130.0)]},
        members=["a@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert result.grant_candidates == []
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "## 追加クレジット付与候補" not in out.read_text(encoding="utf-8")


# --- 構成サマリ行 ---------------------------------------------------------

def test_credit_summary_composition(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, net=0.0),
                     spend_row("b@x.jp", 10.0, net=0.0),
                     spend_row("c@x.jp", 10.0, net=0.0),
                     spend_row("d@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard", "b@x.jp,Standard",
                 "c@x.jp,Standard", "d@x.jp,Standard"],
    )
    _write_info(
        input_dir,
        "email,追加クレジット上限\na@x.jp,200\nb@x.jp,無制限\nc@x.jp,0\nd@x.jp,\n",
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    s = result.summary
    assert s["credit_shown"] is True
    assert s["credit_enabled_n"] == 2      # a(200) + b(無制限)
    assert s["credit_cap_total_usd"] == 200.0
    assert s["credit_unlimited_n"] == 1
    assert s["credit_disabled_n"] == 1
    assert s["credit_unknown_n"] == 1
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "| 追加クレジット | 有効 2 名" in out.read_text(encoding="utf-8")


# --- 後方互換 -------------------------------------------------------------

def test_no_credit_column_no_mode_column(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    # credit 情報が無い入力では credits_mode / credit_limit_usd 列を出力しない
    assert "credits_mode" not in result.users.columns
    assert "credit_limit_usd" not in result.users.columns
    assert result.summary.get("credit_shown") is False


# --- 速報の残額ブロック・付与候補 -----------------------------------------

def test_preview_credit_reach_and_grant(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("hit@x.jp", 200.0, net=48.0),
                     spend_row("far@x.jp", 200.0, net=20.0),
                     spend_row("cand@x.jp", 400.0, net=0.0)]},
        members=["hit@x.jp,Premium", "far@x.jp,Premium", "cand@x.jp,Standard"],
        members_month="2026-07",
    )
    _write_info(
        input_dir,
        "email,追加クレジット上限\nhit@x.jp,50\nfar@x.jp,300\ncand@x.jp,0\n",
    )
    result = preview(input_dir, "2026-07", cfg, days_observed=10, org="org-a")
    cr = result.credit_reach
    assert cr is not None
    by = {r["email"]: r for r in cr["rows"]}
    assert by["hit@x.jp"]["reached"] is True         # 48 >= 50-5
    assert by["far@x.jp"]["reached"] is False        # 20 < 295
    # cand は κ=0（disabled）なので残額ブロックには載らない
    assert "cand@x.jp" not in by
    # 付与候補: disabled の Standard で昇格方向（Premium検討/判断保留）
    assert "cand@x.jp" in [c["email"] for c in result.grant_candidates]
    out = tmp_path / "org"
    write_preview(result, out)
    md = PREVIEW.path(out, "2026-07", "org-a").read_text(encoding="utf-8")
    assert "## 追加クレジット残額" in md
    assert "## 追加クレジット付与候補" in md


def test_credit_reach_interval_rate(make_snapshots, cfg):
    # 修正3: スナップショットがあるユーザは「最新区間の課金増分 ÷ 区間日数」を現在レートに
    # 使い、到達予測 = 観測末日 + 残額/レート とする
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 60.0, net=0.0)],
            "2026-07-13": [spend_row("a@x.jp", 150.0, net=100.0)],   # 区間課金 +100 / 8日
        },
        members=["a@x.jp,Premium"], members_month="2026-07",
    )
    (input_dir / "members-info.csv").write_text(
        "email,追加クレジット上限\na@x.jp,200\n", encoding="utf-8")
    result = preview(input_dir, "2026-07", cfg, days_observed=13, org="org-a")
    row = next(r for r in result.credit_reach["rows"] if r["email"] == "a@x.jp")
    assert row["reached"] is False
    # レート 100/8=12.5/日 → 13 + (200-100)/12.5 = 21 日頃
    assert row["eta_day"] == 21
