"""組織内の分布（参考値）のテスト。

統計量そのもの（中央値・母標準偏差・分位点）と、指標ごとに違う母集団の決め方を固定する。
分布は表示専用なので、判定・推奨・警告が変わらないことも併せて検査する。
"""

import pandas as pd
import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.product_usage import ProductUsage
from seat_analyzer.report import write_html, write_markdown
from seat_analyzer.report.format import _fmt_stat_count
from seat_analyzer.report.html import _cost_guide
from seat_analyzer.report.stats import (
    KEY_API_COST,
    KEY_LOC,
    KEY_REQUESTS,
    _describe,
    distributions,
)

from .conftest import spend_row

# users の固定カラム（analyze が必ず付ける列）。テストは指標に効く列だけを上書きする
_DEFAULTS = {
    "current_seat": "standard",
    "api_cost_usd": 0.0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "billed_extra_usd": 0.0,
}


def _users(*rows: dict) -> pd.DataFrame:
    """users DataFrame を組み立てる（email は連番で振る）。"""
    return pd.DataFrame([
        {"email": f"u{i}@x.jp", **_DEFAULTS, **r} for i, r in enumerate(rows)
    ])


def _usage(totals: dict) -> ProductUsage:
    """product 利用特徴量（email → total_requests）。値 None は欠損。"""
    return ProductUsage(
        features=pd.DataFrame(
            {"total_requests": pd.array(list(totals.values()), dtype="Float64")},
            index=pd.Index(list(totals), name="email"),
        ),
        issues=[],
    )


def _by_key(dists) -> dict:
    return {d.key: d for d in dists}


# --- 統計量そのもの（既知の小配列） ---

def test_describe_matches_hand_computed_values():
    """平均・中央値・母標準偏差・分位点・最大を既知の値で固定する。

    分位点は線形補間（p90 は 3 と 4 の間を 0.6:0.4 で按分する）。標準ライブラリの
    statistics.quantiles とは値が異なるため、こちらの定義であることを明示する。
    """
    d = _describe("k", "ラベル", "count", pd.Series([0.0, 1.0, 2.0, 3.0, 4.0]))
    assert (d.key, d.label, d.kind, d.n) == ("k", "ラベル", "count", 5)
    assert d.mean == 2.0
    assert d.median == 2.0
    assert d.std == pytest.approx(2.0 ** 0.5)   # 母標準偏差（ddof=0）: sqrt(10/5 - 4)
    assert d.p25 == 1.0
    assert d.p75 == 3.0
    assert d.p90 == pytest.approx(3.6)
    assert d.maximum == 4.0


def test_describe_interpolates_between_values_for_even_n():
    """要素数が偶数のときは中央値も分位点も隣り合う2値の間を取る。"""
    d = _describe("k", "ラベル", "count", pd.Series([1.0, 2.0, 3.0, 4.0]))
    assert d.median == 2.5
    assert d.p25 == pytest.approx(1.75)
    assert d.p75 == pytest.approx(3.25)
    assert d.p90 == pytest.approx(3.7)
    assert d.std == pytest.approx(1.25 ** 0.5)


def test_describe_single_value_has_zero_population_std():
    """n=1 でも未定義にならない（標本補正をしないため）。"""
    d = _describe("k", "ラベル", "usd", pd.Series([5.0]))
    assert d.n == 1
    assert d.std == 0.0
    assert (d.mean, d.median, d.p25, d.p90, d.maximum) == (5.0, 5.0, 5.0, 5.0, 5.0)


def test_fmt_stat_count_switches_units_by_magnitude():
    assert _fmt_stat_count(7_420_580_000) == "7.42B"   # 十億単位は B（_fmt_tokens と同じ刻み）
    assert _fmt_stat_count(1_000_000_000) == "1.00B"
    assert _fmt_stat_count(999_999_999) == "1000.00M"
    assert _fmt_stat_count(1_234_567) == "1.23M"
    assert _fmt_stat_count(1_000_000) == "1.00M"
    assert _fmt_stat_count(999_999) == "1000.0K"
    assert _fmt_stat_count(25_000) == "25.0K"
    assert _fmt_stat_count(9_999) == "9,999"
    assert _fmt_stat_count(0) == "0"


# --- 母集団の決め方 ---

def test_population_excludes_unassigned_and_keeps_idle_and_unknown():
    """未割当は判定対象外なので除き、利用ゼロのユーザとシート不明は残す。"""
    dists = _by_key(distributions(_users(
        {"current_seat": "premium", "api_cost_usd": 100.0},
        {"current_seat": "standard", "api_cost_usd": 0.0},     # 利用ゼロ: 含める
        {"current_seat": "unknown", "api_cost_usd": 20.0},     # シート不明: 含める
        {"current_seat": "unassigned", "api_cost_usd": 999.0},  # 未割当: 除く
    )))
    demand = dists[KEY_API_COST]
    assert demand.n == 3
    assert demand.maximum == 100.0            # 未割当の 999 は入らない
    assert demand.median == 20.0


def test_no_distributions_when_population_is_empty():
    """未割当しかいない組織では指標を1つも出さない（セクションごと出ない）。"""
    assert distributions(_users({"current_seat": "unassigned"})) == []


def test_all_zero_metric_is_still_reported():
    """全員ゼロの指標は 0 の分布として出す（行を落とすと n が読めなくなる）。"""
    billed = _by_key(distributions(_users(
        {"api_cost_usd": 10.0}, {"api_cost_usd": 20.0},
    )))["billed"]
    assert billed.n == 2
    assert (billed.mean, billed.maximum, billed.std) == (0.0, 0.0, 0.0)


def test_loc_excludes_zeros_and_is_absent_without_the_column():
    """LoC は 0 を母集団から除く（行が無いのか 0 行なのか区別できないため）。"""
    dists = _by_key(distributions(_users(
        {"loc_with_cc": 1000}, {"loc_with_cc": 0}, {"loc_with_cc": 300},
    )))
    assert dists[KEY_LOC].n == 2
    assert dists[KEY_LOC].median == 650.0
    # 列そのものが無い（code-analytics を持たない組織）なら指標ごと出さない
    assert KEY_LOC not in _by_key(distributions(_users({"api_cost_usd": 1.0})))


def test_requests_join_treats_absent_users_as_zero():
    """spend に行が無いユーザは回数ゼロが確定する（欠損ではない）。"""
    users = _users({"api_cost_usd": 10.0}, {"api_cost_usd": 0.0})
    d = _by_key(distributions(users, _usage({"u0@x.jp": 40.0})))[KEY_REQUESTS]
    assert d.n == 2            # u1 は features に居ない → 0
    assert d.mean == 20.0
    assert d.maximum == 40.0


def test_requests_drops_users_whose_count_is_unknown():
    """total_requests が欠損のユーザは母集団から除く（0 とは意味が違う）。"""
    users = _users({"api_cost_usd": 10.0}, {"api_cost_usd": 20.0}, {"api_cost_usd": 30.0})
    d = _by_key(distributions(users, _usage(
        {"u0@x.jp": 100.0, "u1@x.jp": None, "u2@x.jp": 200.0},
    )))[KEY_REQUESTS]
    assert d.n == 2
    assert (d.mean, d.maximum) == (150.0, 200.0)


def test_requests_absent_when_no_user_has_a_certain_count():
    """spend に現れたユーザが1人も確定値を持たなければ行ごと落とす。

    残るのは利用ゼロのメンバーだけで、全員 0 の退化した行にしかならないため。
    requests 列を持たない spend（total_requests が全欠損）がこの経路に入る。
    """
    users = _users({"api_cost_usd": 10.0}, {"api_cost_usd": 0.0})
    dists = _by_key(distributions(users, _usage({"u0@x.jp": None})))
    assert KEY_REQUESTS not in dists
    # product 利用特徴量そのものが無い場合（速報など）も同じ
    assert KEY_REQUESTS not in _by_key(distributions(users))


# --- analyze の結果との突合（別経路の再計算になっていないこと） ---

def test_population_demand_total_matches_summary(cfg, make_input):
    """母集団の需要合計 = サマリの全体需要 − 未割当ユーザ分。"""
    input_dir = make_input(
        {"2026-06": [
            spend_row("heavy@x.jp", 500.0, net=0.0),
            spend_row("light@x.jp", 12.34, net=0.0),
            spend_row("off@x.jp", 40.0, net=0.0),      # 未割当なのに利用実績あり
        ]},
        members=["heavy@x.jp,Premium", "light@x.jp,Standard",
                 "off@x.jp,Unassigned", "idle@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    unassigned = result.users[result.users["current_seat"] == "unassigned"]
    expected = result.summary["total_api_cost_usd"] - float(unassigned["api_cost_usd"].sum())

    demand = _by_key(distributions(result.users, result.product_usage))[KEY_API_COST]
    assert demand.n == 3                       # heavy / light / idle（未割当は除く）
    # ユーザごとの丸め（小数2桁）の積み上がりを許容する
    assert demand.mean * demand.n == pytest.approx(expected, abs=0.01 * len(result.users))


def test_report_sections_do_not_change_the_judgement(cfg, make_input, tmp_path):
    """分布は表示専用（判定・推奨・警告に触れない）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0, net=0.0)],
         "2026-06": [spend_row("a@x.jp", 12.0, net=0.0)]},
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    before = (result.users.copy(), dict(result.summary), list(result.warnings))
    distributions(result.users, result.product_usage)
    write_markdown(result, tmp_path / "report.md")
    write_html(result, tmp_path / "dashboard.html")
    pd.testing.assert_frame_equal(result.users, before[0])
    assert result.summary == before[1]
    assert result.warnings == before[2]


def test_markdown_and_html_carry_the_section(cfg, make_input, tmp_path):
    """report.md は詳細利用状況と感度分析の間に、dashboard は表とガイド線を出す。"""
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 300.0, net=50.0),
                     spend_row("b@x.jp", 20.0, net=0.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    write_markdown(result, tmp_path / "report.md")
    write_html(result, tmp_path / "dashboard.html")

    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert md.index("## 詳細利用状況") < md.index("## 組織内の分布（参考値）") < md.index("## 感度分析")
    assert "| API換算需要 | 2 名 |" in md
    assert "| リクエスト数 | 2 名 |" in md

    html = (tmp_path / "dashboard.html").read_text(encoding="utf-8")
    assert "<h2>組織内の分布（参考値）</h2>" in html
    assert 'class="guide g-median"' in html and 'class="guide g-mean"' in html
    assert '<div class="rank">#1</div>' in html   # 需要の降順順位（行の順番ではない）


def test_rank_uses_the_same_population_as_the_distribution(cfg, make_input, tmp_path):
    """順位は未割当を除いた母集団で付ける（未割当は順位なしの「—」）。

    未割当ユーザに利用実績がある組織では、順位の母集団が分布・ガイド線と違うと同じ図の
    中に母集団が2つできる（未割当が上位を占め、他のユーザの順位が1つずつずれる）。
    """
    input_dir = make_input(
        {"2026-06": [
            spend_row("off@x.jp", 900.0, net=0.0),     # 未割当なのに最大の利用実績
            spend_row("top@x.jp", 300.0, net=0.0),
            spend_row("mid@x.jp", 100.0, net=0.0),
        ]},
        members=["off@x.jp,Unassigned", "top@x.jp,Premium", "mid@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    write_html(result, tmp_path / "dashboard.html")
    html = (tmp_path / "dashboard.html").read_text(encoding="utf-8")

    # 未割当を数えていれば top は #2 になる。母集団が揃っていれば #1
    assert '<div class="name" title="top@x.jp">top</div>' in html
    assert '<div class="rank">#1</div>' in html
    assert '<div class="rank">#2</div>' in html
    assert '<div class="rank">#3</div>' not in html    # 母集団は2名
    assert '<div class="rank">—</div>' in html         # 未割当は順位を付けない

    # ガイド線の母集団も同じ（未割当の 900 は最大にも中央値にも入らない）
    demand = _by_key(distributions(result.users, result.product_usage))[KEY_API_COST]
    assert demand.n == 2 and demand.maximum == 300.0


def test_no_guide_lines_when_every_user_is_idle(cfg, make_input, tmp_path):
    """母集団の需要が全員ゼロならガイド線を引かない（$0 の線が2本重なるため）。

    線の有無は分布そのもの（母集団の最大）で決める。棒と座標を揃えるためのスケールは
    ガイド線の判定には使えない。0 除算を避けて 1.0 へ倒した値にも、母集団の外にいる
    未割当ユーザの需要にもなるためで、どちらで判定しても $0 の線が出てしまう。
    分布の表自体は 0 の分布として残す。
    """
    for label, rows, members in (
        ("割当済みのみ・全員ゼロ",
         [spend_row("a@x.jp", 0.0, net=0.0)],
         ["a@x.jp,Premium", "b@x.jp,Standard"]),
        # 未割当だけに需要がある組織。スケール（棒の最大）は 900 になるが母集団は全員ゼロ
        ("未割当だけ需要あり",
         [spend_row("a@x.jp", 0.0, net=0.0), spend_row("off@x.jp", 900.0, net=0.0)],
         ["a@x.jp,Premium", "b@x.jp,Standard", "off@x.jp,Unassigned"]),
    ):
        input_dir = make_input({"2026-06": rows}, members=members)
        result = analyze(input_dir, "2026-06", cfg, org="org-a")
        dists = distributions(result.users, result.product_usage)
        assert _by_key(dists)[KEY_API_COST].maximum == 0.0, label
        # スケールに何を渡しても線は引かない（倒した 1.0 も、未割当を含む最大 900 も）
        for scale in (0.0, 1.0, 900.0):
            assert _cost_guide(dists, scale) is None, f"{label}: scale={scale}"

        out = tmp_path / f"dashboard-{len(rows)}.html"
        write_html(result, out)
        html = out.read_text(encoding="utf-8")
        assert 'class="guide g-median"' not in html, label
        assert "縦線:" not in html, label
        assert "<h2>組織内の分布（参考値）</h2>" in html, label   # 分布の表は出る


def test_unassigned_only_org_renders_without_the_section(cfg, make_input, tmp_path):
    """未割当しかいない組織でも落ちず、分布のセクションが出ないだけ。"""
    input_dir = make_input(
        {"2026-06": [spend_row("off@x.jp", 40.0, net=0.0)]},
        members=["off@x.jp,Unassigned", "off2@x.jp,Unassigned"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert distributions(result.users, result.product_usage) == []

    write_markdown(result, tmp_path / "report.md")
    write_html(result, tmp_path / "dashboard.html")
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    html = (tmp_path / "dashboard.html").read_text(encoding="utf-8")
    assert "組織内の分布（参考値）" not in md
    assert "組織内の分布（参考値）" not in html
    assert 'class="guide g-median"' not in html   # ガイド線も引かない
