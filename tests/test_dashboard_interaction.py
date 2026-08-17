"""ダッシュボードの対話機能（列ソート・検索・判定フィルタ・折りたたみ・高さ変更）のテスト。

対話の挙動そのものはブラウザが要るのでここでは動かせない。代わりに、その挙動が
成立するための前提を HTML と CSS の側で固定する。守りたいのは次の4点で、どれも
壊れても画面は出たままになる（見た目では気づけない）:

- 対話用の UI は JS が付けたクラスの下でしか現れない。JS が無効な環境に、押しても
  何も起きない部品が残らない
- 折りたたみはブラウザ側の仕事で、サーバは常に全行を書き出す。サーバ側で行を削ると
  JS が無効な環境で内容そのものが欠ける
- 判定フィルタの選択肢は、その表に実際に現れた判定だけ。固定リストにすると、出ない
  判定を選べて空の表になる
- CSV 由来の値が JS のコンテキストへ入らない。script の中身は静的アセットと一致し、
  並べ替えの材料も DOM のテキストだけで、値を別に埋め込まない
"""

import re

import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.report import write_html
from seat_analyzer.report.html import (
    _DASHBOARD_CSS,
    _DASHBOARD_JS,
    _HTML_TEMPLATE_SRC,
)
from seat_analyzer.report.text import STATUS_ORDER

from .conftest import spend_row

# 表の初期表示（15行）と棒の初期表示（20本）をどちらも超える人数にする。
# 超えていないと「サーバが全行を書き出す」ことの検査が空振りする。
_USERS = [f"user{i:02d}@x.jp" for i in range(1, 23)]


@pytest.fixture
def dashboard(cfg, make_input, tmp_path):
    """折りたたみの上限を超える人数のダッシュボード HTML。"""
    rows, members = [], []
    for i, email in enumerate(_USERS):
        # 需要の大小と実課金の有無を混ぜ、判定が1種類に潰れないようにする
        rows.append(spend_row(email, 5.0 if i % 2 else 20.0 * (i + 1)))
        members.append(f"{email},{'Premium' if i % 2 else 'Standard'}")
    input_dir = make_input({"2026-06": rows}, members=members)
    out = tmp_path / "dashboard.html"
    write_html(analyze(input_dir, "2026-06", cfg, org="org-a"), out)
    return out.read_text(encoding="utf-8")


def _body(html: str) -> str:
    """本文だけを取り出す（CSS と JS の実体には同じクラス名が並ぶため）。"""
    head, _, body = html.partition("</head>")
    assert body, "head と body を切り分けられません"
    return body


def _card(body: str, heading: str) -> str:
    """見出しで指したカード1枚分の HTML（カードは入れ子にならない）。"""
    found = re.search(rf"<h2>{re.escape(heading)}</h2>.*?</section>", body, re.S)
    assert found, f"カードが見つかりません: {heading}"
    return found.group(0)


# --- JS が無い環境に操作できない UI を残さない ---

# JS が作る/使う部品と、それを隠す土台の規則。どれも html.js の下でだけ表示する
_GUARDED = ("toolbar", "grab", "listmore")


def test_interactive_ui_only_appears_under_the_js_class():
    """ツールバー・グラブバー・展開ボタンは JS が動く環境でだけ表示する。

    素の状態で表示する規則を書くと、JS が無効な環境に押しても何も起きない部品が並ぶ。
    """
    for name in _GUARDED:
        assert f".{name} {{ display: none; }}" in _DASHBOARD_CSS
        assert f"html.js .{name}" in _DASHBOARD_CSS


def test_the_html_ships_no_control_that_the_script_did_not_place(dashboard):
    """HTML に置くのは判定フィルタだけで、他の対話部品は JS が作る。

    データから作る必要がある選択肢だけをサーバ側が描き（実データに現れた判定に
    限るため）、残りは JS が作る。JS が作る部品が HTML に混ざると、JS が無効な環境で
    それだけが取り残される。
    """
    body = _body(dashboard)
    assert "<input" not in body                      # 検索欄は JS が作る
    for cls in ("grab", "listmore", "list-count", "sort-ind", "sortable", "more"):
        assert f'class="{cls}"' not in body
    # ボタンはタブとテーマ切替だけ（どちらも 8D-1 から html.js の下にある）
    classes = re.findall(r'<button[^>]*class="([^"]+)"', body)
    assert classes and all(c.startswith(("tab", "theme-btn")) for c in classes)
    # select はツールバーの中にしか無い（ツールバーごと html.js の下で出す）
    inside = re.findall(r'<div class="toolbar">(.*?)</div>', body, re.S)
    assert body.count("<select") == sum(chunk.count("<select") for chunk in inside) == 1


# --- 折りたたみはブラウザ側の仕事（サーバは全行を書き出す） ---

def test_every_row_is_written_out_even_beyond_the_collapse_limit(dashboard):
    """初期表示の上限を超える分もサーバは書き出す（JS 無効でも全員読める）。"""
    body = _body(dashboard)
    table = _card(body, "推奨一覧")
    assert table.count('<td class="user"') == len(_USERS)
    bars = _card(body, "ユーザ別 API 換算コスト")
    assert bars.count('<div class="bar">') == len(_USERS)
    for email in _USERS:
        assert f'title="{email}"' in table and f'title="{email}"' in bars


def test_no_parallel_values_are_embedded_for_sorting(dashboard):
    """並べ替えの材料は画面に出ている文字列だけ（値を別に埋め込まない）。

    data-* に数値を持たせると、表示と並べ替えで別々の値を持つことになり、書式を
    変えたときに片方だけが追従しなくなる。
    """
    body = _body(dashboard)
    for attr in ("data-sort", "data-value", "data-num", "data-key"):
        assert attr not in body
    assert "textContent" in _DASHBOARD_JS


def test_numeric_columns_are_marked_for_the_sorter(dashboard):
    """数値列の印（.num）が付いている。ソートの初期方向はこれで決まる。

    金額列から .num が落ちると右寄せが崩れるだけでなく、初回のソートが昇順になり
    「クリックしたのに大きい順にならない」形で静かに変わる。
    """
    head = re.search(r"<h2>推奨一覧</h2>.*?<thead><tr>(.*?)</tr>",
                     _body(dashboard), re.S).group(1)
    cells = re.findall(r"<th([^>]*)>([^<]+)</th>", head)
    numeric = {label for attrs, label in cells if 'class="num"' in attrs}
    assert numeric == {"API換算需要", "実課金", "Std時", "Prem時", "削減/月"}
    assert {"ユーザ", "判定"} <= {label for attrs, label in cells if 'class="num"' not in attrs}


# --- 判定フィルタ ---

def test_the_judge_filter_offers_only_the_judgements_in_the_table(dashboard):
    """選択肢は推奨一覧に実際に並んだ判定だけ（出ない判定は選べない）。"""
    body = _body(dashboard)
    options = re.findall(r'<option value="([^"]*)">', body)
    assert options[0] == ""                          # 先頭は絞り込みなし
    judged = re.findall(r'<td class="judge"><span class="badge [^"]*">([^<]+)</span>', body)
    assert len(set(judged)) > 1, "判定が1種類しか出ておらず、選択肢の検査が空振りします"
    assert set(options[1:]) == set(judged)
    # 並びは「判定の内訳」と同じ（どちらも同じ集計から作る）
    assert options[1:] == re.findall(r'<span class="label">([^<]+)</span>', body)
    assert set(options[1:]) <= set(STATUS_ORDER)


def test_the_judge_filter_is_absent_when_there_is_nothing_to_filter():
    """行が1つも無ければフィルタごと出さない（選んでも何も起きない部品を出さない）。"""
    assert '{% if judge_counts %}<div class="toolbar">' in _HTML_TEMPLATE_SRC


# --- CSV 由来の値を JS のコンテキストへ渡さない ---

def test_the_script_block_is_exactly_the_static_asset(dashboard):
    """埋め込まれた script は templates/dashboard.js そのもの。

    等しいことが、この HTML の JS にデータが1文字も混ざっていないことの検査になる。
    """
    assert re.findall(r"<script>(.*?)</script>", dashboard, re.S) == [_DASHBOARD_JS]


def test_hostile_values_do_not_reach_the_script(cfg, make_input, tmp_path):
    """引用符やタグを含むメールアドレスでも script の中身は変わらない。"""
    evil = '</script>"x@x.jp'
    input_dir = make_input({"2026-06": [spend_row(evil, 10.0)]},
                           members=[f"{evil},Standard"])
    out = tmp_path / "dashboard.html"
    write_html(analyze(input_dir, "2026-06", cfg, org="org-a"), out)
    html = out.read_text(encoding="utf-8")
    assert re.findall(r"<script>(.*?)</script>", html, re.S) == [_DASHBOARD_JS]
    assert "</script>\"x@x.jp" not in html            # 本文側もエスケープされている


# --- デザイン仕様の数値 ---

def test_the_design_numbers_are_pinned():
    """初期表示の行数・寸法・最小高さをデザイン仕様の値で固定する。

    どれも「少し違っても画面は成立する」種類の値なので、変わったことに気づけるよう
    ここに書き出しておく。
    """
    assert "var TABLE_ROWS = 15;" in _DASHBOARD_JS          # 表の初期表示行数
    assert "var BAR_ROWS = 20;" in _DASHBOARD_JS            # 棒の初期表示本数
    assert "var MIN_BOX_H = 180;" in _DASHBOARD_JS          # スクロール領域の最小高さ
    assert ".search { width: 200px; }" in _DASHBOARD_CSS
    assert "padding: 7px 11px; background: var(--surface-2);" in _DASHBOARD_CSS
    assert "gap: 3px; height: 18px;" in _DASHBOARD_CSS      # グラブバーの高さ
    assert ".grab span { width: 28px; height: 3px;" in _DASHBOARD_CSS
    # 初期高さは 8D-1 から据え置き（推奨一覧・詳細利用状況が 620px、他は 540px）
    assert ".tablebox { overflow: auto; max-height: 540px; }" in _DASHBOARD_CSS
    assert ".tablebox.tall { max-height: 620px; }" in _DASHBOARD_CSS


def test_the_grab_bar_replaces_the_browser_resize_corner():
    """高さ変更は自前のグラブバーで行い、縦だけを変える。"""
    assert "cursor: ns-resize;" in _DASHBOARD_CSS
    assert "resize:" not in _DASHBOARD_CSS                  # 標準の resize は使わない
    assert "body.resizing { user-select: none;" in _DASHBOARD_CSS
    assert "box.style.maxHeight" in _DASHBOARD_JS
    assert "clientX" not in _DASHBOARD_JS                   # 横は見ない


def test_the_hidden_row_rule_comes_after_the_row_display_rules():
    """行を隠す規則が、行そのものの表示規則より後ろにある。

    .bar は display: flex を持つので、詳細度が同じこの規則が先に来ると絞り込みが
    表にだけ効き、棒は隠れない（同じ操作で片方だけ反応しない形になる）。
    """
    assert (_DASHBOARD_CSS.index(".is-out { display: none; }")
            > _DASHBOARD_CSS.index(".bar { display: flex"))
