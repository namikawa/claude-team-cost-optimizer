"""ダッシュボードの対話機能（列ソート・検索・判定フィルタ・高さ変更）のテスト。

対話の挙動そのものはブラウザが要るのでここでは動かせない。代わりに、その挙動が
成立するための前提を HTML と CSS の側で固定する。守りたいのは次の4点で、どれも
壊れても画面は出たままになる（見た目では気づけない）:

- 対話用の UI は JS が付けたクラスの下でしか現れない。JS が無効な環境に、押しても
  何も起きない部品が残らない
- 一覧は常に全行が出る。サーバが行を削ると読み手には欠けたことが分からず、絞り込みが
  効いているのか元から無いのかも区別できない
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

# 「サーバが全行を書き出す」ことの検査が空振りしない人数にする。数人だと行が欠けても
# 数え間違いに見え、判定も1種類に潰れてフィルタの選択肢の検査が成立しない。
_USERS = [f"user{i:02d}@x.jp" for i in range(1, 23)]


@pytest.fixture
def dashboard(cfg, make_input, tmp_path):
    """人が並ぶ一覧が十分な行数を持つダッシュボード HTML。"""
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


def _js_function(name: str, source: str | None = None) -> str:
    """JS の関数 name の本体（外側の波かっこの中身）。省略時は dashboard.js 全体から探す。

    「その呼び出しがファイルのどこかにある」ことを見るだけの検査は、呼び出しを別の
    関数へ移す変更を捕まえられない。本体を切り出してから見ることで、呼ぶ場所そのものを
    固定する。位置の前後関係も同じ本体の中で比べる。

    切り出しはかっこの対応だけで行う。文字列やコメントに波かっこを書くと切り出しに
    失敗するが、そのときは黙って通るのではなくこの関数が落ちる。

    既定値として _DASHBOARD_JS を束縛しない（既定引数は定義時に値を捕まえるため、
    差し替えた JS で検査そのものを試せなくなる）。
    """
    source = _DASHBOARD_JS if source is None else source
    head = source.find(f"function {name}(")
    assert head >= 0, f"関数が見つかりません: {name}"
    start = source.index("{", head)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:i]
    raise AssertionError(f"関数の波かっこが閉じていません: {name}")


def _css_rule(selector: str, source: str | None = None) -> str:
    """CSS 規則 selector の宣言部（波かっこの中身）。

    トークンの濃さは tests/test_dashboard_shell.py が実測するが、それだけだと
    「そのトークンを実際に使っている」ことは誰も見ていない。使う側をここで固定する。
    """
    source = _DASHBOARD_CSS if source is None else source
    found = re.search(rf"(?m)^[ \t]*{re.escape(selector)}\s*\{{(.*?)\}}", source, re.S)
    assert found, f"CSS 規則が見つかりません: {selector}"
    return found.group(1)


def test_the_css_rule_extractor_matches_the_whole_selector():
    """規則の切り出しそのものの検査（前方一致で別の規則を掴まないこと）。"""
    src = "  .chip { border: 1px; }\n  .chip-n { color: red; }\n  .chip:hover { color: blue; }\n"
    assert _css_rule(".chip", src).strip() == "border: 1px;"
    assert _css_rule(".chip-n", src).strip() == "color: red;"
    assert _css_rule(".chip:hover", src).strip() == "color: blue;"
    with pytest.raises(AssertionError):
        _css_rule(".nothing", src)


def test_the_search_and_filter_have_an_edge_that_can_be_seen():
    """検索欄と判定フィルタは、触る前の状態で輪郭が見える（枠線は --dim）。

    面の色どうしが近いデザインなので、塗りの差ではカード面と見分けられない
    （--surface-2 は --surface に対して 1.1:1 ほどしかない）。枠線が唯一の境界になる。
    空の入力欄は輪郭が無いと見出しの一部と区別できず、絞り込めること自体に気づけない。
    濃さそのものは tests/test_dashboard_shell.py が両テーマで実測する。
    """
    rule = _css_rule(".search, .filter")
    assert "border: 1px solid var(--dim);" in rule
    # フォーカス時は accent へ動かす（色相が変わるので、触っている間と区別が付く）
    assert "border-color: var(--accent);" in _css_rule(".search:focus, .filter:focus")


def test_the_selected_theme_segment_has_an_edge_that_can_be_seen():
    """テーマ切替は、選択中の区画が面と影だけでなく枠線でも分かる。

    面の差は切替の地に対して 1.1:1 ほどしかなく、影は Dark で効きが弱い。どちらも
    無くなったわけではないので、枠線はその上に足す。切替の外枠は変えない。
    """
    # 枠線の場所は全区画で確保しておく（選択のたびに幅が変わって区画が動かないように）
    assert "border: 1px solid transparent;" in _css_rule(".theme-btn")
    active = _css_rule(".theme-btn.is-active")
    assert "border-color: var(--dim);" in active
    assert "background: var(--surface);" in active and "box-shadow: var(--shadow);" in active
    # 外枠は据え置き（ヘッダーで常時見えるので、囲いまで濃くすると主張が強くなる）
    assert "border: 1px solid var(--line);" in _css_rule(".theme-switch")


def test_the_grab_bar_edge_is_left_alone_on_purpose():
    """グラブバーの帯の上線は細いまま。掴めることはグリップ（--dim）が伝える。

    上線を濃くすると表と脚注の間に強い横線が増え、情報より罫線が目立つ。据え置きが
    意図であることを、グリップ側の色と併せてここで固定する。
    """
    assert "border-top: 1px solid var(--line);" in _css_rule("html.js .grab.is-on")
    assert "background: var(--dim);" in _css_rule(".grab span")


def test_the_function_extractor_reads_one_body_only():
    """本体の切り出しそのものの検査（この道具が壊れると下の検査が空振りする）。"""
    src = "function a() {\n  x();\n  if (y) { z(); }\n}\nfunction b() { w(); }\n"
    assert _js_function("a", src).strip() == "x();\n  if (y) { z(); }"
    assert _js_function("b", src).strip() == "w();"
    with pytest.raises(AssertionError):
        _js_function("missing", src)


# --- JS が無い環境に操作できない UI を残さない ---

# JS が作る/使う部品と、それを隠す土台の規則。どれも html.js の下でだけ表示する
_GUARDED = ("toolbar", "grab")


def test_interactive_ui_only_appears_under_the_js_class():
    """ツールバーとグラブバーは JS が動く環境でだけ表示する。

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
    for cls in ("grab", "list-count", "sort-ind", "sortable"):
        assert f'class="{cls}"' not in body
    # ボタンはタブとテーマ切替だけ（どちらも 8D-1 から html.js の下にある）
    classes = re.findall(r'<button[^>]*class="([^"]+)"', body)
    assert classes and all(c.startswith(("tab", "theme-btn")) for c in classes)
    # select はツールバーの中にしか無い（ツールバーごと html.js の下で出す）
    inside = re.findall(r'<div class="toolbar">(.*?)</div>', body, re.S)
    assert body.count("<select") == sum(chunk.count("<select") for chunk in inside) == 1


# --- 一覧は常に全行が出る ---

def test_the_server_writes_out_every_row(dashboard):
    """サーバ側は一覧に載るべき人を全員書き出す（表も棒も、行を間引かない）。

    ここが見るのは HTML の中身だけで、ブラウザ側で隠されないことは下の2件が見る。
    """
    body = _body(dashboard)
    table = _card(body, "推奨一覧")
    assert table.count('<td class="user"') == len(_USERS)
    bars = _card(body, "ユーザ別 API 換算コスト")
    assert bars.count('<div class="bar">') == len(_USERS)
    for email in _USERS:
        assert f'title="{email}"' in table and f'title="{email}"' in bars


def test_the_script_hides_rows_only_by_the_filters():
    """行の表示は絞り込みの結果だけで決まる（行数の上限では隠さない）。

    上限で隠すと、読み手は一覧の一部しか見ていないことに気づけない。隠れた行を出す部品に
    気づかなければ、そこで全体を確認したつもりになる。表はスクロール領域と高さ変更で
    読む量を調整できるので、行数を絞る必要もない。
    """
    body = _js_function("apply")
    assert "show(row, ok);" in body
    # 上限・展開状態のどちらも持たない（片方でも残ると隠す条件を組み直せる）
    for token in ("limit", "expanded"):
        assert token not in body, f"apply が {token} を見ています"


def test_no_row_limit_machinery_is_left_in_the_assets():
    """行数を絞る部品が JS / CSS のどちらにも無い。

    上限の定数や展開ボタンが戻ると、絞り込みとは無関係に行が消える状態に戻る。apply の
    中身だけを見ていても、別の関数で隠す作りに変わったときに気づけないので、部品の側も見る。
    """
    for token in ("TABLE_ROWS", "BAR_ROWS", "addMore", "listmore"):
        assert token not in _DASHBOARD_JS, f"JS に {token} が残っています"
    for selector in (".listmore", ".more"):
        assert selector not in _DASHBOARD_CSS, f"CSS に {selector} が残っています"


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
    """寸法・最小高さをデザイン仕様の値で固定する。

    どれも「少し違っても画面は成立する」種類の値なので、変わったことに気づけるよう
    ここに書き出しておく。
    """
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


def test_the_grab_bar_only_appears_where_dragging_changes_something():
    """動かせる余地のない表（内容が最小高さに収まる表・0行の表）には出さない。

    掴んで動かせる範囲は最小高さから内容の高さまでで、max-height は内容を引き伸ばさない。
    内容がその最小高さ以下なら、どこへ動かしても表示は変わらない。押しても何も起きない
    部品を出さないという点で、ポインタ操作を扱えない環境でバーを作らないのと同じ規則。
    """
    # 表示は is-on の下だけ。素の .grab を出す規則を持たない（＝既定は出さない）
    assert ".grab { display: none; }" in _DASHBOARD_CSS
    assert "html.js .grab.is-on {" in _DASHBOARD_CSS
    assert "html.js .grab {" not in _DASHBOARD_CSS
    # 付ける側と外す側の向きまで固定する（入れ替えると短い表にだけバーが出る）
    body = _js_function("updateGrab")
    taller = body.index("list.box.scrollHeight > MIN_BOX_H")
    added = body.index('list.grab.classList.add("is-on")')
    removed = body.index('list.grab.classList.remove("is-on")')
    assert taller < added < removed
    assert "else" in body[added:removed]


def test_the_grab_bar_decision_is_refreshed_when_it_can_change():
    """要否の判定を測り直す契機が3つとも、それぞれの呼び元の中に残っている。

    どれが欠けても静かにずれる。行数の側が欠けると、絞り込んで短くなった表に効かない
    バーが残る。タブの側が欠けると、隠れている間の測定値（0）のまま固定され、初期表示
    以外のタブでバーが出ないままになる。幅の側が欠けると、折り返しが変わって境界を
    跨いだときに追従しない。
    """
    # 行数が変わる操作（検索・判定フィルタ）はどちらも apply を通る
    assert "updateGrab(list);" in _js_function("apply")

    show = _js_function("showTab")
    assert "refreshGrabs();" in show
    # パネルの表示を切り替えた「あと」に測る。前に測ると隠れている側の 0 を掴む
    assert show.index("refreshGrabs();") > show.index('panel.classList.add("is-active")')

    # 幅の変化は箱そのものを見る。監視の登録は一覧を組み立てたあとでないと空振りする
    init = _js_function("init")
    assert init.index("watchSize();") > init.index("setUpTables();")


def test_the_grab_bar_follows_a_change_of_window_size():
    """幅が変わって内容の高さが最小高さの境界を跨いだときも追従する。

    窓の寸法変化はどの環境でも拾えるので、これを条件なしで土台に置く。ResizeObserver の
    側だけに寄せると、非対応の環境でそこだけ「動かせないバーが残る」状態に戻る。
    """
    watch = _js_function("watchSize")
    assert 'window.addEventListener("resize", scheduleRefresh);' in watch
    assert "new ResizeObserver(scheduleRefresh)" in watch
    assert "observer.observe(list.box)" in watch
    # 窓の監視は分岐の外（ResizeObserver の有無を見るより前）に置く
    assert watch.index('addEventListener("resize"') < watch.index("window.ResizeObserver")
    # 連続発火はまとめる。待ちにはタイマーを使う（描画が止まっている間もフレームと
    # 違って必ず動くので、まとめ待ちの印が立ったまま戻らなくなることがない）
    schedule = _js_function("scheduleRefresh")
    assert "refreshTimer" in schedule and "setTimeout" in schedule
    assert "requestAnimationFrame" not in schedule
    assert "refreshGrabs();" in schedule
    assert "refreshTimer = 0;" in schedule                  # 待ちの印は必ず戻す
    # まとめた先が全部の一覧を測り直す（1つだけ測ると他が取り残される）
    assert "each(lists, updateGrab);" in _js_function("refreshGrabs")


def test_the_hidden_row_rule_comes_after_the_row_display_rules():
    """行を隠す規則が、行そのものの表示規則より後ろにある。

    .bar は display: flex を持つので、詳細度が同じこの規則が先に来ると絞り込みが
    表にだけ効き、棒は隠れない（同じ操作で片方だけ反応しない形になる）。
    """
    assert (_DASHBOARD_CSS.index(".is-out { display: none; }")
            > _DASHBOARD_CSS.index(".bar { display: flex"))
