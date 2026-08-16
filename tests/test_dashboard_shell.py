"""ダッシュボードの外枠（自己完結性・タブ・テーマ）のテスト。

tests/test_golden.py は生成物を丸ごと固定するが、固定できるのは「今そうなっている」
ことだけで、守りたい性質そのものは書かれない。ここでは共有物としての条件
（外部参照を持たない・JS が無くても全 section が読める・テーマが両方の経路で効く）を
性質として書く。golden を再生成すれば通ってしまう類の退行を、ここが止める。
"""

import re

import pytest

from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import write_html, write_preview
from seat_analyzer.report.html import (
    _DASHBOARD_CSS,
    _DASHBOARD_JS,
    _credit_bars,
    _judge_counts,
)

from .conftest import spend_row

# 外部への参照。src / href の絶対 URL と、CSS からの取り込みの両方を見る
# （フォントは url() と @import で入りうる）
_EXTERNAL = re.compile(r'(?:src|href)\s*=\s*"https?://|url\(\s*["\']?https?://|@import')


@pytest.fixture
def dashboards(cfg, make_input, tmp_path):
    """正式・速報の両ダッシュボードの HTML（条件つき section が出る組織）。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 300.0, net=50.0),
                     spend_row("b@x.jp", 20.0, net=0.0)],
         "2026-06": [spend_row("a@x.jp", 400.0, net=80.0),
                     spend_row("b@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    full = tmp_path / "dashboard.html"
    write_html(result, full)

    pv = preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a")
    pv_dir = tmp_path / "pv"
    write_preview(pv, pv_dir)
    return (full.read_text(encoding="utf-8"),
            (pv_dir / "2026-06" / "preview-dashboard.html").read_text(encoding="utf-8"))


# --- 自己完結性 ---

def test_dashboards_have_no_external_references(dashboards):
    """フォント・スクリプト・画像を外から取りに行かない（共有される単体 HTML のため）。"""
    for html in dashboards:
        found = _EXTERNAL.findall(html)
        assert not found, f"外部参照があります: {found}"


def test_style_and_script_are_embedded(dashboards):
    """CSS と JS はファイルの中身がそのまま埋まっている（参照ではない）。"""
    for html in dashboards:
        assert "<style>" in html and "<script>" in html
        assert "--accent:" in html                 # デザイントークンの実体
        assert "seatdash-theme" in html            # テーマ保存キー＝JS の実体


def test_css_keeps_its_quoted_font_names(dashboards):
    """CSS の引用符が実体参照へ置き換わらない。

    style 要素の中では HTML の実体参照が解釈されないため、font-family の引用符が
    エスケープされると宣言ごと無効になり、書体の指定が黙って消える。
    """
    for html in dashboards:
        assert '"Hiragino Sans"' in html
        assert "&#34;Hiragino Sans&#34;" not in html


def test_csv_values_are_still_escaped(cfg, make_input, tmp_path):
    """アセットを素通しにしても、CSV 由来の値のエスケープは効いたまま。"""
    evil = '<b>x</b>@x.jp'
    input_dir = make_input(
        {"2026-06": [spend_row(evil, 10.0)]}, members=[f"{evil},Standard"])
    out = tmp_path / "dashboard.html"
    write_html(analyze(input_dir, "2026-06", cfg, org="org-a"), out)
    html = out.read_text(encoding="utf-8")
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


# --- JS が無くても読めること ---

def test_tab_switching_is_scoped_to_the_js_class():
    """タブの出し分けは JS が付けたクラスの下でだけ効く。

    素の .tabpanel を隠す規則が入ると、JS が動かない環境で概要以外の内容が
    丸ごと消える（共有先の閲覧環境はこちらで選べない）。
    """
    assert ".tabpanel { display: block; }" in _DASHBOARD_CSS
    assert "html.js .tabpanel { display: none; }" in _DASHBOARD_CSS
    assert "html.js .tabpanel.is-active { display: block; }" in _DASHBOARD_CSS
    # タブ行とテーマ切替は押せて初めて意味があるので、JS が無い側では出さない
    assert ".tabbar { display: none;" in _DASHBOARD_CSS
    assert "html.js .tabbar { display: flex; }" in _DASHBOARD_CSS
    assert ".theme-switch {\n    display: none;" in _DASHBOARD_CSS
    assert "html.js .theme-switch { display: flex; }" in _DASHBOARD_CSS


def test_every_tab_has_a_panel_and_a_heading(dashboards):
    """タブと section が1対1で対応し、各 section に JS 無しでも読める見出しがある。"""
    full, _ = dashboards
    tabs = re.findall(r'<button type="button" class="tab[^"]*"[^>]*data-tab="([^"]+)"', full)
    panels = re.findall(r'<section class="tabpanel[^"]*" data-tab="([^"]+)"', full)
    assert tabs == ["overview", "actions", "members", "org"]
    assert panels == tabs
    assert full.count('<h2 class="panel-title">') == len(panels)
    # 初期状態はどちらも先頭のタブ（JS が読み込まれる前でも表示が食い違わない）
    assert full.count('class="tabpanel is-active"') == 1
    assert full.count('class="tab is-active"') == 1


def test_preview_has_the_same_shell_without_tabs(dashboards):
    """速報も同じテイストだが、section が少ないためタブでは分けない。"""
    _, pv = dashboards
    assert 'class="theme-switch"' in pv
    assert 'class="tabpanel"' not in pv
    assert 'class="tabbar"' not in pv


# --- テーマ ---

def test_dark_theme_applies_through_both_paths():
    """Dark は明示選択（属性）と OS 設定（メディアクエリ）の両方で効く。

    片方だけだと、Auto のまま OS がダークの環境か、Dark を選んだ環境のどちらかで
    Light の配色が出る。
    """
    assert 'html[data-theme="dark"] {' in _DASHBOARD_CSS
    assert "@media (prefers-color-scheme: dark) {" in _DASHBOARD_CSS
    # Auto（属性なし）の Light 指定を OS のダークが上書きしないよう、明示の Light は除く
    assert 'html:not([data-theme="light"]) {' in _DASHBOARD_CSS


def test_theme_script_only_touches_the_document_element():
    """テーマの JS は属性の付け外しと保存だけを行う（描画データを持たない）。"""
    assert 'root.setAttribute("data-theme", mode)' in _DASHBOARD_JS
    assert 'root.removeAttribute("data-theme")' in _DASHBOARD_JS
    assert "localStorage" in _DASHBOARD_JS


# --- 既存の数値から導く表示（帯・内訳） ---

def test_credit_bars_skip_the_empty_segments():
    """0 名の区分は帯に出さない（最小幅の細い線が「いる」ように見えるため）。"""
    summary = {"credit_shown": True, "credit_enabled_n": 3,
               "credit_disabled_n": 0, "credit_unknown_n": 2}
    assert [(b["cls"], b["n"]) for b in _credit_bars(summary)] == [
        ("c-enabled", 3), ("c-unknown", 2)]
    # 追加クレジットの構成そのものを出さない組織では帯も出ない
    assert _credit_bars({"credit_shown": False}) == []


def test_judge_counts_add_up_to_the_table():
    """判定の内訳は推奨一覧の行を数えたもので、合計は表の行数と一致する。"""
    users = ([{"status": "変更推奨"}] * 2 + [{"status": "現状維持"}] * 3
             + [{"status": "要観察"}])
    rows = _judge_counts(users)
    assert [(r["label"], r["n"]) for r in rows] == [
        ("変更推奨", 2), ("要観察", 1), ("現状維持", 3)]      # 表示順は推奨一覧と同じ
    assert sum(r["n"] for r in rows) == len(users)
    assert rows[0]["pct"] == pytest.approx(100.0 * 2 / 6)
    assert _judge_counts([]) == []
