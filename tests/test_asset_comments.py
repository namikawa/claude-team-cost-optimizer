"""配布する HTML にコメントが残らないことのテスト。

templates/ の実体ファイル（dashboard.css・dashboard.js）には設計の意図をコメントで
残すが、共有される HTML はその説明を持ち込まない。落とす側の規則（何を消し、何を
消さないか）と、生成物にコメントが1つも無いことの両方をここで固定する。

コメントを落とす走査そのものは、素朴な置換にすると正規表現リテラルの中の `/*` や
文字列の中の `//` を読み違えて、そこから先を丸ごと消す。壊れても画面は出たままに
なる（JS が途中で切れても HTML は表示される）ので、リテラルの保護を1件ずつ書く。
"""

import re

import pytest

from seat_analyzer.analyze import analyze, preview
from seat_analyzer.report import PREVIEW_DASHBOARD, write_html, write_preview
from seat_analyzer.report.html import _asset
from seat_analyzer.report.minify import strip_css_comments, strip_js_comments

from .conftest import spend_row

# --- 行の後始末（CSS と JS で同じ振る舞い） ---

# (入力, 期待, 何を保証するか)。ブロックコメントの扱いは両方の走査で共通なので、
# 同じ表を両方へ通す。
_CLEANUP = [
    ("a;\n/* c */\nb;\n", "a;\nb;\n", "コメントだけの行は行ごと消える"),
    ("a; /* c */\nb;\n", "a;\nb;\n", "行末コメントは直前の空白ごと消える"),
    ("a;\n/* c1\n   c2 */\nb;\n", "a;\nb;\n", "複数行にまたがるコメントも行ごと消える"),
    ("a;\n\nb;\n", "a;\n\nb;\n", "元からある空行は保つ（畳み込みはしない）"),
    ("  /* c */ a;\n", "  a;\n", "行頭からコメントまでが空白ならインデントは残す"),
    ("a; /* c1 */ b; /* c2 */\n", "a; b;\n", "同じ行に2つあっても後始末は同じ"),
]


@pytest.mark.parametrize("strip", [strip_css_comments, strip_js_comments])
@pytest.mark.parametrize(("source", "expected", "guarantee"), _CLEANUP)
def test_comment_removal_cleans_up_the_line(strip, source, expected, guarantee):
    assert strip(source) == expected, guarantee


# コメントが区切りを兼ねていた形。落とすだけだと隣り合う2つが1つのトークンに化けるので、
# 空白を1つ残す。区切りの要否は隣り合うことになる2文字で決まるので、CSS と JS の
# どちらでも同じ結果になる。
_TOKEN_JOIN = [
    ("const ok = key/* c */in obj;\n", "const ok = key in obj;\n",
     "識別子どうしが繋がると別の名前になる"),
    ("var x = 1/* c */2;\n", "var x = 1 2;\n",
     "数値どうしが繋がると構文は通ったまま値だけ変わる"),
    ("var x = a +/* c */+b;\n", "var x = a + +b;\n", "`+` が2つ並ぶと別の演算子になる"),
    ("var x = a -/* c */-b;\n", "var x = a - -b;\n", "`-` も同じ"),
    ("var x = a / /* c *//re/;\n", "var x = a / /re/;\n",
     "`/` が2つ並ぶと、そこから行末までが丸ごとコメントになる"),
    ("foo(/* c */bar);\n", "foo(bar);\n", "区切りが残る側には空白を足さない"),
]


@pytest.mark.parametrize("strip", [strip_css_comments, strip_js_comments])
@pytest.mark.parametrize(("source", "expected", "guarantee"), _TOKEN_JOIN)
def test_removal_does_not_join_two_tokens(strip, source, expected, guarantee):
    assert strip(source) == expected, guarantee


def test_text_without_comments_is_returned_unchanged():
    """コメントが無ければ1文字も変えない（空白の圧縮も改行の畳み込みもしない）。"""
    source = "  .a {\n    b: 1px;\n  }\n\n\n  .c { d: 2px; }\n"
    assert strip_css_comments(source) == source
    assert strip_js_comments(source) == source


# --- CSS ---

def test_css_keeps_double_slash():
    """CSS に行コメントは無いので `//` は消さない（消すと値が変わる）。"""
    source = ".a { b: 1px; } // これはコメントではない\n"
    assert strip_css_comments(source) == source


def test_css_does_not_look_inside_strings():
    """文字列リテラルの中の `/*` はコメントの始まりではない。"""
    for source in ('.a { content: "/* x */"; }\n', ".a { content: '/* x */'; }\n"):
        assert strip_css_comments(source) == source


def test_css_string_escapes_are_honoured():
    """`\\` で逃がした引用符は文字列を閉じない。"""
    source = '.a { content: "\\"/* x */"; }\n'
    assert strip_css_comments(source) == source


# --- JS のリテラル保護 ---

_JS_UNCHANGED = [
    ('var s = "/* x */ // y";\n', "文字列（二重引用符）"),
    ("var s = '/* x */ // y';\n", "文字列（単引用符）"),
    ("var re = /[/*]/;\n", "正規表現リテラルの文字クラスの中の `/`"),
    ("var re = /a\\/b/g.test(s);\n", "正規表現リテラルの中の逃がした `/`"),
    ("var t = `a /* b */ ${ c } // d`;\n", "テンプレートリテラル"),
    ("var t = `${ `${ x }` }`;\n", "入れ子のテンプレートリテラル"),
    ("var s = 'a\\\\';\nvar u = 1;\n", "文字列の末尾の逃がした `\\`"),
]


@pytest.mark.parametrize(("source", "guarantee"), _JS_UNCHANGED)
def test_js_literals_are_left_alone(source, guarantee):
    """リテラルの中身は1文字も変えない。"""
    assert strip_js_comments(source) == source, guarantee


_JS_STRIPPED = [
    ("x = a / b; // c\n", "x = a / b;\n", "識別子の直後の `/` は除算"),
    ("x = f(a) / 2; // c\n", "x = f(a) / 2;\n", "`)` の直後の `/` は除算"),
    ("x = a[0] / 2; // c\n", "x = a[0] / 2;\n", "`]` の直後の `/` は除算"),
    ("return /a\\/b/.test(s); // c\n", "return /a\\/b/.test(s);\n",
     "`return` の直後は識別子文字で終わっていても正規表現"),
    ("var t = `${ x /* c */ }`;\n", "var t = `${ x }`;\n",
     "テンプレートの ${ } の中のコメントも落とす"),
    ("var re = /[/*]/; // c\n", "var re = /[/*]/;\n",
     "正規表現リテラルを跨いだ先の行コメントは落とす"),
    ("var r = count++ / total; // c\n", "var r = count++ / total;\n",
     "後置 `++` の直後の `/` は除算"),
    ("var r = count-- / total; // c\n", "var r = count-- / total;\n",
     "後置 `--` も同じ"),
    ("var r = +/re/.source; // c\n", "var r = +/re/.source;\n",
     "前置の `+` の直後の `/` は正規表現"),
    ("var r = obj.return / total; // c\n", "var r = obj.return / total;\n",
     "`.` の直後の return はプロパティ名であってキーワードではない"),
    ("var r = obj?.return / total; // c\n", "var r = obj?.return / total;\n",
     "`?.` の直後もプロパティ名"),
    ("const x = [...typeof /[/*]/]; // c\n", "const x = [...typeof /[/*]/];\n",
     "スプレッドはプロパティアクセスではないので、直後の語はキーワードのまま"),
    ("var r = [...a].length / 2; // c\n", "var r = [...a].length / 2;\n",
     "スプレッドを跨いだ先の `/` は除算"),
]


@pytest.mark.parametrize(("source", "expected", "guarantee"), _JS_STRIPPED)
def test_js_comments_are_stripped_around_literals(source, expected, guarantee):
    assert strip_js_comments(source) == expected, guarantee


# --- 終端が無いときは止まる ---

# 残りを黙って落とすと、壊れた CSS / JS がそのまま配布物になる。
_UNTERMINATED_CSS = [
    (".a { b: 1px; }\n/* c\n", "コメント"),
    ('.a { content: "x;\n}\n', "文字列"),
]

_UNTERMINATED_JS = [
    ("var a = 1;\n/* c\n", "コメント"),
    ('var s = "abc\n', "文字列"),
    ("var re = /abc\n", "正規表現リテラル"),
    ("var t = `abc\n", "テンプレートリテラル"),
    ("var t = `${ x + 1\n", "テンプレートの ${"),
]


@pytest.mark.parametrize(("source", "kind"), _UNTERMINATED_CSS)
def test_css_unterminated_is_rejected(source, kind):
    with pytest.raises(ValueError, match="閉じていません"):
        strip_css_comments(source)


@pytest.mark.parametrize(("source", "kind"), _UNTERMINATED_JS)
def test_js_unterminated_is_rejected(source, kind):
    with pytest.raises(ValueError, match="閉じていません"):
        strip_js_comments(source)


# --- 生成物 ---

@pytest.fixture
def dashboards(cfg, make_input, tmp_path):
    """正式・速報の両ダッシュボードの HTML。"""
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 300.0, net=50.0)],
         "2026-06": [spend_row("a@x.jp", 400.0, net=80.0),
                     spend_row("b@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Standard"],
    )
    full = tmp_path / "dashboard.html"
    write_html(analyze(input_dir, "2026-06", cfg, org="org-a"), full)

    pv_dir = tmp_path / "pv"
    write_preview(preview(input_dir, "2026-06", cfg, days_observed=10, org="org-a"), pv_dir)
    return (full.read_text(encoding="utf-8"),
            PREVIEW_DASHBOARD.path(pv_dir, "2026-06", "org-a").read_text(encoding="utf-8"))


def test_the_assets_that_get_embedded_do_have_comments():
    """検査が空振りしていないこと（実体ファイルの側にはコメントがある）。"""
    assert "/*" in _asset("dashboard.css")
    assert "/*" in _asset("dashboard.js")


def test_dashboards_have_no_html_comments(dashboards):
    """HTML コメントを1つも出さない。

    テンプレートの `<!--...-->` は断片と共有文言の差し込み先だけで、どれも描画前に
    解決される。生の HTML コメントを書くとここで落ちる（テンプレートに注記を書く
    ときは、出力に出ない Jinja の {# #} を使う）。
    """
    for html in dashboards:
        assert "<!--" not in html


def test_embedded_css_and_js_have_no_comments(dashboards):
    """埋め込んだ CSS と JS にコメントが残らない。

    `/*` は本文テキストにも現れうるので、HTML 全体ではなく style / script の中だけを
    見る。行コメントは JS にしかない（CSS の `//` はコメントではない）。
    """
    for html in dashboards:
        styles = re.findall(r"<style>(.*?)</style>", html, re.DOTALL)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        assert styles and scripts, "style / script が見つかりません"
        for block in styles + scripts:
            assert "/*" not in block
        for block in scripts:
            assert "//" not in block
