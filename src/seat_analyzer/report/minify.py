"""HTML へ埋め込む CSS / JS からコメントを落とす。

templates/ の実体ファイルには設計の意図をコメントで残す。埋め込む直前にここを通す
ことで、配布される HTML には実体ファイル側の説明を持ち込まず、閲覧に要る中身だけを
載せる。

落とすのはコメントだけで、空白の圧縮も改行の畳み込みもしない。生成物はバイト単位で
比較されるため、変わる範囲を「コメントとその行」に閉じておく。

走査は文字列・正規表現リテラルを解釈する。素朴な置換だと、正規表現リテラルの中の
`/*` や文字列の中の `//` をコメントの始まりと読み違え、そこから先を丸ごと落とす。
終端の見つからないコメント・文字列・正規表現は ValueError で止める（残りを黙って
落とすと、壊れた CSS / JS がそのまま配布物になる）。
"""

from __future__ import annotations

# 直後の `/` が正規表現リテラルの始まりになる語。直前の文字が識別子文字でも除算に
# しない（`return /re/` の形）。
_REGEX_WORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "do", "else", "case", "yield", "await",
})

# 値の終わりを表す文字。この直後の `/` は除算とみなす。
_VALUE_END = ")]}"

# 隣り合うと1つのトークンになる2文字。コメントを落として並ぶと、演算子が別の演算子に
# なったり（`+` `+` → `++`）、残りが丸ごとコメントになったり（`/` `/`・`/` `*`）する。
_JOINED = frozenset({"++", "--", "//", "/*", "*/"})


def strip_css_comments(text: str) -> str:
    """CSS から `/* ... */` を落とした CSS。

    保証すること:

    - 文字列リテラル（`"..."` / `'...'`）の中身は1文字も変えない
    - `//` は CSS のコメントではないので残す
    - 引用符で囲まない `url(...)` の中は保護しない（`url("...")` と書けば保護される）
    - 終端のない `/*` は ValueError（残り全部を落とすと出力が壊れる）
    """
    return _remove(text, _css_comments(text))


def strip_js_comments(text: str) -> str:
    """JS から `//` 行コメントと `/* ... */` ブロックコメントを落とした JS。

    保証すること:

    - 文字列・テンプレートリテラル（`${ ... }` の入れ子を含む）・正規表現リテラルの
      中身は1文字も変えない
    - `)` `]` `}` の直後の `/` は常に除算とみなす（`if (x) /re/.test(y)` や
      `function f() {} /re/.test(s)` の形は扱わない）
    - 終端のないコメント・文字列・正規表現リテラルは ValueError
    """
    return _remove(text, _js_comments(text))


# ------------------------------------------------------------------ 行の後始末


def _remove(text: str, spans: list[tuple[int, int]]) -> str:
    """コメントの区間を落とし、行の後始末をした文字列。

    後始末は2つ:

    - コメント直前の空白は、その行の行頭からコメントまでに空白以外があるときだけ
      一緒に落とす。行頭からコメントまでが空白だけならインデントとして残し、
      代わりにコメント直後の空白を落とす
    - 落とした結果その行が空白だけになったら、改行ごと落とす

    行末の空白を一律に落とすことはしない（テンプレートリテラルの中では意味を持つ）。
    後始末の対象は、コメントを落とした行だけ。

    コメントが区切りを兼ねていた場所（`key/* c */in`・`1/* c */2`・`a +/* c */+b`）には
    区切りとして空白を1つ残す。判定は落とした結果として隣り合う2文字だけで行う。
    """
    out: list[str] = []
    cur = ""          # まだ改行に届いていない行の中身
    touched = False   # その行からコメントを落としたか
    pos = 0
    for start, end in spans:
        cur, touched = _feed(text[pos:start], cur, touched, out)
        if cur.strip():
            cur = cur.rstrip(" \t")
        else:
            end = _skip_spaces(text, end)
        if cur and end < len(text) and _joins(cur[-1], text[end]):
            cur += " "
        touched = True
        pos = end
    cur, touched = _feed(text[pos:], cur, touched, out)
    if cur and not (touched and not cur.strip()):
        out.append(cur)
    return "".join(out)


def _feed(segment: str, cur: str, touched: bool, out: list[str]) -> tuple[str, bool]:
    """segment を現在の行へ足し、改行に届いた行を out へ書き出す。

    戻り値は（まだ改行に届いていない行の中身, その行からコメントを落としたか）。
    """
    parts = segment.split("\n")
    for part in parts[:-1]:
        line = cur + part
        if not (touched and not line.strip()):
            out.append(line + "\n")
        cur, touched = "", False
    return cur + parts[-1], touched


def _joins(left: str, right: str) -> bool:
    """その2文字が隣り合うと1つのトークンに化けるか（＝区切りが要るか）。"""
    return (_is_ident(left) and _is_ident(right)) or left + right in _JOINED


def _skip_spaces(text: str, i: int) -> int:
    """i から続く空白（改行は含まない）の次の位置。"""
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


# ------------------------------------------------------------------------ CSS


def _css_comments(text: str) -> list[tuple[int, int]]:
    """CSS のコメントの区間（開始位置, 終了位置）。"""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("/*", i):
            i = _block_comment(text, i, spans)
        elif c in "\"'":
            i = _skip_string(text, i)
        else:
            i += 1
    return spans


# ------------------------------------------------------------------------- JS


def _js_comments(text: str) -> list[tuple[int, int]]:
    """JS のコメントの区間（開始位置, 終了位置）。"""
    spans: list[tuple[int, int]] = []
    _scan_js(text, 0, spans, in_substitution=False)
    return spans


def _scan_js(text: str, i: int, spans: list[tuple[int, int]], *,
             in_substitution: bool) -> int:
    """i から JS を走査し、見つけたコメントの区間を spans へ足す。

    in_substitution が真のときはテンプレートリテラルの `${ ... }` の中を見ているので、
    対応する `}` の次の位置を返す。偽のときは末尾まで走査して len(text) を返す。
    """
    n = len(text)
    depth = 0     # `${ ... }` の中で開いたままの `{` の数
    prev = ""     # 直前の意味のあるトークン（空白とコメントは飛ばす。記号は `...` 以外1文字）
    word = ""     # 直前の識別子。正規表現リテラルを許す語の判定に使う
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("/*", i):
            i = _block_comment(text, i, spans)
            continue                      # コメントは区切りでしかないので prev は据え置き
        if c == "/" and text.startswith("//", i):
            end = text.find("\n", i + 2)
            end = n if end < 0 else end
            spans.append((i, end))
            i = end
            continue
        if c == "/":
            if _starts_regex(prev, word):
                i = _skip_regex(text, i)
                prev = ")"                # リテラルが終わった位置（次の `/` は除算）
            else:
                i += 1
                prev = "/"
            word = ""
            continue
        if c in "\"'":
            i = _skip_string(text, i)
            prev, word = ")", ""          # リテラルの直後なので、次の `/` は除算
            continue
        if c == "`":
            i = _skip_template(text, i, spans)
            prev, word = ")", ""
            continue
        if _is_ident(c):
            start = i
            while i < n and _is_ident(text[i]):
                i += 1
            # `.` の直後はプロパティ名。`obj.return` の return はキーワードではないので、
            # 正規表現リテラルを許す語としては数えない
            word = "" if prev == "." else text[start:i]
            prev = text[i - 1]
            continue
        if c.isspace():
            i += 1
            continue                      # 空白も区切りなので prev は据え置き
        if c == "." and text.startswith("...", i):
            # スプレッドはプロパティアクセスではないので、`.` とは別のトークンにする
            # （直後の語はキーワードのまま・直後には値が来る）
            prev, word = "...", ""
            i += 3
            continue
        if c in "+-" and text.startswith(c * 2, i):
            # `++` / `--` は1つのトークン。後置なら値の終わり（次の `/` は除算）、
            # 前置ならこれから値が来る（次の `/` は正規表現）
            prev, word = (")" if _ends_value(prev) else c), ""
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
            elif in_substitution:
                return i + 1
        prev, word = c, ""
        i += 1
    if in_substitution:
        raise ValueError(_unterminated("テンプレートリテラルの ${", text, n))
    return n


def _starts_regex(prev: str, word: str) -> bool:
    """その位置の `/` が正規表現リテラルの始まりか（偽なら除算）。

    直前が値の終わりなら除算、それ以外は正規表現。ただし `return` のような語の直後は、
    識別子文字で終わっていても正規表現にする。
    """
    if word in _REGEX_WORDS:
        return True
    return not _ends_value(prev)


def _ends_value(prev: str) -> bool:
    """直前のトークンが値の終わりか（`)` `]` `}` か識別子文字）。

    多文字の記号トークン（`...`）と、走査の先頭（トークンなし）はどちらも偽。
    """
    return len(prev) == 1 and (prev in _VALUE_END or _is_ident(prev))


def _is_ident(c: str) -> bool:
    """識別子に使える文字か。"""
    return c.isalnum() or c in "_$"


# --------------------------------------------------------- リテラルとコメントの走査


def _block_comment(text: str, i: int, spans: list[tuple[int, int]]) -> int:
    """`/*` から始まるコメントを spans へ足し、その次の位置を返す。"""
    end = text.find("*/", i + 2)
    if end < 0:
        raise ValueError(_unterminated("コメント", text, i))
    spans.append((i, end + 2))
    return end + 2


def _skip_string(text: str, i: int) -> int:
    """`"` / `'` で始まる文字列リテラルの次の位置。"""
    quote, n = text[i], len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2                        # 直後の1文字（行継続の改行を含む）を読み飛ばす
            continue
        if c == quote:
            return j + 1
        if c == "\n":
            break                         # 素の改行は閉じ忘れ（行継続なら \ が付く）
        j += 1
    raise ValueError(_unterminated("文字列リテラル", text, i))


def _skip_regex(text: str, i: int) -> int:
    """`/` で始まる正規表現リテラル（フラグを含む）の次の位置。"""
    n = len(text)
    j = i + 1
    in_class = False                      # 文字クラス `[...]` の中では `/` は普通の文字
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            break                         # 正規表現リテラルは行をまたげない
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            j += 1
            while j < n and _is_ident(text[j]):
                j += 1                    # フラグ（g・i 等）
            return j
        j += 1
    raise ValueError(_unterminated("正規表現リテラル", text, i))


def _skip_template(text: str, i: int, spans: list[tuple[int, int]]) -> int:
    """バッククォートで始まるテンプレートリテラルの次の位置。

    `${ ... }` の中は JS なので走査に戻す（そこに書かれたコメントも落とす対象）。
    """
    n = len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1
        if c == "$" and text.startswith("${", j):
            j = _scan_js(text, j + 2, spans, in_substitution=True)
            continue
        j += 1
    raise ValueError(_unterminated("テンプレートリテラル", text, i))


def _unterminated(kind: str, text: str, i: int) -> str:
    """終端が見つからなかったことを、行番号つきで伝えるメッセージ。"""
    line = text.count("\n", 0, i) + 1
    return f"{kind}が閉じていません（{line} 行目）"
