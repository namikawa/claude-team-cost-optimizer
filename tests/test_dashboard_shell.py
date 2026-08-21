"""ダッシュボードの外枠（自己完結性・タブ・テーマ）のテスト。

tests/test_golden.py は生成物を丸ごと固定するが、固定できるのは「今そうなっている」
ことだけで、守りたい性質そのものは書かれない。ここでは共有物としての条件
（外部参照を持たない・JS が無くても全 section が読める・テーマが両方の経路で効く）を
性質として書く。golden を再生成すれば通ってしまう類の退行を、ここが止める。
"""

import re

import pytest

from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import PREVIEW_DASHBOARD, write_html, write_preview
from seat_analyzer.report.html import (
    _DASHBOARD_CSS,
    _DASHBOARD_JS,
    _credit_bars,
    _judge_counts,
    _org_tab_count,
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
            PREVIEW_DASHBOARD.path(pv_dir, "2026-06", "org-a").read_text(encoding="utf-8"))


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
    assert tabs == ["overview", "actions", "members", "org", "notes"]
    assert panels == tabs
    assert full.count('<h2 class="panel-title">') == len(panels)
    # 初期状態はどちらも先頭のタブ（JS が読み込まれる前でも表示が食い違わない）
    assert full.count('class="tabpanel is-active"') == 1
    assert full.count('class="tab is-active"') == 1


def test_tab_counts_show_what_the_tab_contains(dashboards):
    """件数バッジはそのタブの中身の数。数えるものが無いタブには付けない。

    この組織は部署もチームも持たないため、組織タブにはサマリ表そのものが無い。
    軸のある組織で数え間違えないことは test_org_tab_count_follows_the_drawn_axis が見る。
    前提と注意は数えるものが無いタブで、常にバッジを持たない。
    """
    full, _ = dashboards
    labels = dict(re.findall(r'data-tab="([^"]+)">(.*?)</button>', full))
    counts = {key: re.findall(r'<span class="tab-count">(\d+)</span>', body)
              for key, body in labels.items()}
    assert counts == {"overview": ["2"], "actions": ["2"], "members": ["2"],
                      "org": [], "notes": []}
    assert "別サマリ" not in full


def test_org_tab_count_follows_the_drawn_axis():
    """組織タブの件数は、実際に描画されている軸の行数を数える。

    軸は部署とチームの2つで、片方しか値を持たない組織がある。部署の行だけを数えると、
    チーム別サマリが出ている組織で 0 名の表が並んでいるように見える。
    """
    dept = {"heading": "部署別サマリ", "rows": [{}, {}, {}]}
    team = {"heading": "チーム別サマリ", "rows": [{}, {}]}
    assert _org_tab_count([dept, team]) == 3      # 両方あれば先に出る部署を数える
    assert _org_tab_count([team]) == 2            # 部署が無い組織はチームを数える
    assert _org_tab_count([]) == 0                # どちらも無ければバッジを出さない


# --- タブの中身の並び ---

def _panels(html: str) -> dict[str, str]:
    """data-tab → タブパネル1枚分の HTML。

    切り出しはパネルの開始位置での分割で行う（終了タグで探すと、中に差し込まれた
    断片の </section> を先に掴む）。末尾のパネルには </main> 以降も含まれるが、
    見出しの前後関係を見るぶんには差し支えない。
    """
    panels = {}
    for part in re.split(r'(?=<section class="tabpanel)', html):
        found = re.match(r'<section class="tabpanel[^"]*" data-tab="([^"]+)"', part)
        if found:
            panels[found.group(1)] = part
    return panels


def test_actions_tab_leads_with_the_summary_cards(dashboards):
    """推奨アクションは要約2枚（判定サマリ・付与候補）を先に置き、推奨一覧を下に出す。

    表が先頭にあると、読み手は数十行をスクロールし切ってから要約に出会う。並びが
    入れ替わっても画面は成立するので、順序をここで固定する。
    """
    full, _ = dashboards
    actions = _panels(full)["actions"]
    assert (actions.index("<h2>判定サマリ</h2>")
            < actions.index("<h2>追加クレジット付与候補</h2>")
            < actions.index("<h2>推奨一覧</h2>"))
    assert "<h2>判定の内訳</h2>" not in full        # 旧名が残っていない


def test_the_recommendation_table_repeats_the_column_legend(dashboards):
    """推奨一覧の列の読み方は表の脚注にも置き、前提と注意と同じ文を使う。

    同じ説明を2箇所に書き下すと片方だけ直る。文言の定義は report/text.py の _TEXT
    ひとつで、脚注と前提と注意の両方がそこを参照していることを重なりで確かめる。
    """
    full, _ = dashboards
    panels = _panels(full)
    footer = re.search(r'<div class="card-ft">(.*?)</div>', panels["actions"], re.S)
    assert footer, "推奨一覧に脚注がありません"
    for shared in (
        "「Std時 / Prem時」= そのシートの場合の想定月額",
        "確度 = 込み利用量の low/mid/high 3シナリオ推定での判定一致度",
        "⚠ = 実課金ゼロなのに需要が込み量推定に迫る Standard ユーザ",
    ):
        assert shared in footer.group(1)
        assert shared in panels["notes"]


def test_the_assumptions_have_their_own_tab(dashboards):
    """前提と注意は右端の専用タブに置き、組織タブには残さない。

    組織タブは部署・チーム別サマリと分布の場所。読み方の断りが表の下に続いていると、
    サマリを読みに来た人が延々とスクロールすることになる。
    """
    full, _ = dashboards
    panels = _panels(full)
    assert '<h2 class="panel-title">前提と注意</h2>' in panels["notes"]
    assert "<h2>前提と注意</h2>" in panels["notes"]
    assert "ヶ月ヒステリシス" in panels["notes"]      # カードの中身ごと移っている
    assert "前提と注意" not in panels["org"]
    assert "ヶ月ヒステリシス" not in panels["org"]


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


def test_color_scheme_follows_the_selected_theme():
    """ネイティブ部品（スクロールバー・フォーム）の配色も選んだテーマに揃う。

    light dark の両方を宣言したままだと、OS がダークの環境で Light を選んだときに
    スクロールバーだけダークで残る（逆も同じ）。
    """
    assert "color-scheme: light;" in _DASHBOARD_CSS
    assert "color-scheme: light dark" not in _DASHBOARD_CSS
    # 明示 Dark（属性）と Auto の Dark（メディアクエリ）の2箇所
    assert _DASHBOARD_CSS.count("color-scheme: dark;") == 2


# --- コントラスト ---

# 文字色 → 実際に載る背景。同じ文字色が複数の背景に載る場合は、比が最も低くなる
# 背景を含める（テーマによって surface-2 と hover のどちらが効くかが入れ替わるため
# 両方を並べる）。バッジの文字は各 *-soft の上にしか出ない。
#
# 速報だけに出る組み合わせも対象。規則そのものが preview-dashboard.html.j2 の中にあっても、
# 色トークンを定義しているのは dashboard.css なので比はここで実測できる。
_TEXT_ON = [
    ("muted", "surface-2", "th / タブの件数 / 現状維持バッジ / テーマ切替 / 検索欄の placeholder"),
    ("ink", "surface-2", "検索欄・判定フィルタの入力文字"),
    ("accent", "surface-2", "ソート中の列の矢印"),
    ("muted", "surface", "カードの副題・脚注・凡例・KPI のラベル"),
    ("dim", "hover", "行ホバー中の増分・矢印・確度"),
    ("dim", "surface", "順位・箇条書きの—・未割当シート"),
    ("accent", "accent-soft", "変更推奨バッジ / 利用開始"),
    ("accent", "hover", "行ホバー中の削減額・Code 列"),
    ("amber", "amber-soft", "要観察バッジ / 実課金の新規発生"),
    ("warn", "warn-soft", "シート不明バッジ / 利用停止"),
    ("warn", "hover", "行ホバー中の上限フラグ"),
    ("std", "hover", "行ホバー中の Standard 表記"),
    ("prem", "hover", "行ホバー中の Premium 表記"),
    ("ink-2", "hover", "行ホバー中の補助テキスト"),
    ("ink-2", "amber-soft", "速報の注意バナー（規則は preview-dashboard.html.j2 側）"),
    ("ink", "accent-soft", "callout"),
]

# 小さい文字（本文サイズ）の下限。KPI の 32px のような大きい文字は 3:1 でよいが、
# 上の組み合わせはすべて本文サイズで出るため一律この値で見る。
_MIN_CONTRAST = 4.5

# 境界に使う色 → その境界が載る面。文字ではなく「押せる部品の輪郭」なので下限が違う。
# 面の色どうしが 1.1:1 ほどしか離れていないデザインなので、塗りの差では境界にならず、
# 輪郭が薄いと押せるものがそこにあること自体に気づけない。触れる部品の輪郭は
# 両テーマとも --dim に寄せてあり、同じ役割のトークンなので濃さも揃う。
#
# グラブバーの帯の上線（--line）はここに入れない。掴めることを伝えているのは
# グリップの方で、上線は表と脚注を分ける罫線として意図的に細いままにしている。
_EDGE_ON = [
    ("dim", "surface", "検索欄 / 判定フィルタ の枠線"),
    ("dim", "surface-2", "グラブバーのグリップ / テーマ切替の選択中の枠線"),
    ("accent", "surface", "検索欄・判定フィルタのフォーカス枠"),
]

# 部品の境界の下限。文字より低いが、しきい値ぎりぎりで止めないことは
# test_control_edges_look_the_same_in_both_themes が別に見る。
_MIN_EDGE_CONTRAST = 3.0


def _relative_luminance(hex_color: str) -> float:
    """sRGB の相対輝度（WCAG 2.1 の定義）。"""
    channels = []
    for i in (1, 3, 5):
        c = int(hex_color[i:i + 2], 16) / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _palettes() -> dict[str, dict[str, str]]:
    """テーマごとの色トークン。Dark は Light を土台に上書きを重ねた実効値。

    読み取りには3つの落とし穴があり、どれも「Light を2回検査して全部通る」形で
    静かに壊れる:

    - コメントに [data-theme="dark"] のような文字列が出てくる。素朴な文字列検索で
      ブロックの先頭を決めると Light 側のコメントを掴むので、先にコメントを落とす
    - Dark は @media の中とセレクタ側の2箇所にあり、片方だけ読むと取りこぼす
    - Dark ブロックで定義されていないトークンは Light の値のまま効くので、
      上書きだけを見ると実際の配色にならない

    テーマの判別は color-scheme 宣言で行う（両テーマとも同じトークン名を並べるので
    宣言の中身では区別できず、選択子の文字列も上のとおり当てにならない）。
    """
    light, dark_blocks = _color_blocks()
    dark: dict[str, str] = dict(light)
    for block in dark_blocks:
        dark.update(block)
    return {"light": light, "dark": dark}


def _color_blocks() -> tuple[dict[str, str], list[dict[str, str]]]:
    """(Light のトークン, Dark ブロックごとのトークン)。判別は color-scheme 宣言。"""
    css = re.sub(r"/\*.*?\*/", "", _DASHBOARD_CSS, flags=re.S)
    light: dict[str, str] = {}
    dark_blocks: list[dict[str, str]] = []
    # 入れ子の無いブロック（宣言だけを持つ塊）を拾う。@media の外枠は中に { を含む
    # ため一致せず、その中身のブロックが直接拾われる
    for body in re.findall(r"\{([^{}]+)\}", css):
        colors = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})\s*;", body))
        if not colors:
            continue
        if "color-scheme: light;" in body:
            light.update(colors)
        elif "color-scheme: dark;" in body:
            dark_blocks.append(colors)
    assert light, "Light のトークンが読めません（:root の書式が変わった可能性）"
    assert dark_blocks, "Dark のトークンが読めません（Dark ブロックの書式変更）"
    return light, dark_blocks


def test_both_dark_paths_declare_the_same_colors():
    """明示 Dark（属性）と Auto の Dark（メディアクエリ）が同じ色を並べる。

    同じ表を2箇所に書いているため、片方だけ直すと OS 設定に任せた環境と Dark を
    選んだ環境で配色が食い違う（片方の画面でしか再現しない）。
    """
    _light, dark_blocks = _color_blocks()
    assert len(dark_blocks) == 2
    assert dark_blocks[0] == dark_blocks[1]


def test_the_two_palettes_are_read_as_different_tables():
    """Light と Dark で別の値を検査していることを、検査そのものの前に確かめる。

    コントラストの検査は、両テーマとも同じ表（Light）を読んでいても全部通る。
    読み違いをこの1件で切り分けられるようにしておく。
    """
    light, dark = _palettes()["light"], _palettes()["dark"]
    differing = {k for k in light if light[k] != dark[k]}
    used = {token for fg, bg, _use in _TEXT_ON for token in (fg, bg)}
    assert used <= differing, (
        f"Light と同じ値のまま検査しているトークン: {sorted(used - differing)}")
    # 明暗の向きも逆であること（本文の色は Light で暗く Dark で明るい）
    assert _relative_luminance(light["ink"]) < _relative_luminance(dark["ink"])
    assert _relative_luminance(light["surface"]) > _relative_luminance(dark["surface"])


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_text_meets_the_contrast_minimum(theme):
    """文字色が、実際に載る背景に対して読める濃さである（両テーマ）。

    背景と文字の差は色を1段動かすだけで詰まる。色を変えたときにここで気づけるよう、
    組み合わせごとに実測する。
    """
    tokens = _palettes()[theme]
    low = [(fg, bg, use, _contrast(tokens[fg], tokens[bg]))
           for fg, bg, use in _TEXT_ON
           if _contrast(tokens[fg], tokens[bg]) < _MIN_CONTRAST]
    assert not low, f"[{theme}] コントラストが不足しています: " + ", ".join(
        f"--{fg} on --{bg}（{use}）= {r:.2f}:1" for fg, bg, use, r in low)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_control_edges_meet_the_boundary_minimum(theme):
    """押せる部品の輪郭が、載っている面から見分けられる（両テーマ）。

    文字と違って、輪郭が薄くても画面は成立してしまう（読めるが押せると分からない）。
    面の色を1段動かすとここが真っ先に詰まるので、組み合わせごとに実測する。
    """
    tokens = _palettes()[theme]
    low = [(fg, bg, use, _contrast(tokens[fg], tokens[bg]))
           for fg, bg, use in _EDGE_ON
           if _contrast(tokens[fg], tokens[bg]) < _MIN_EDGE_CONTRAST]
    assert not low, f"[{theme}] 境界のコントラストが不足しています: " + ", ".join(
        f"--{fg} on --{bg}（{use}）= {r:.2f}:1" for fg, bg, use, r in low)


# 触れる部品の輪郭に使う組み合わせ（載る面はカード面か、切替の中の地）。
_CONTROL_EDGES = [("dim", "surface"), ("dim", "surface-2")]


@pytest.mark.parametrize(("fg", "bg"), _CONTROL_EDGES)
def test_control_edges_look_the_same_in_both_themes(fg, bg):
    """触れる部品の輪郭が、両テーマで同じくらいはっきり見える。

    見えることの下限（3:1）を満たすだけでは「探さないと見つからない」は解けないので、
    余裕のある濃さを別に要求する。加えて両テーマの比を近づける。片方だけ濃いと、
    テーマを切り替えたときに部品の目立ち方が変わる。
    """
    ratios = {theme: _contrast(tokens[fg], tokens[bg])
              for theme, tokens in _palettes().items()}
    assert min(ratios.values()) >= 4.0, f"--{fg} on --{bg} の輪郭が薄すぎます: {ratios}"
    # 主要な操作ではないので、周囲より前に出るほど濃くもしない
    assert max(ratios.values()) <= 7.0, f"--{fg} on --{bg} の輪郭が濃すぎます: {ratios}"
    assert abs(ratios["light"] - ratios["dark"]) <= 0.75, (
        f"--{fg} on --{bg} はテーマによって目立ち方が変わります: {ratios}")


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
    """判定サマリは推奨一覧の行を数えたもので、合計は表の行数と一致する。"""
    users = ([{"status": "変更推奨"}] * 2 + [{"status": "現状維持"}] * 3
             + [{"status": "要観察"}])
    rows = _judge_counts(users)
    assert [(r["label"], r["n"]) for r in rows] == [
        ("変更推奨", 2), ("要観察", 1), ("現状維持", 3)]      # 表示順は推奨一覧と同じ
    assert sum(r["n"] for r in rows) == len(users)
    assert rows[0]["pct"] == pytest.approx(100.0 * 2 / 6)
    assert _judge_counts([]) == []
