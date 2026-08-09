r"""生成物の改行を LF に固定していることを構文木で検査する。

書き込み経路は既定で改行を OS 任せにする（`Path.write_text`・`Path.open`・
`NamedTemporaryFile` は newline=None のとき os.linesep へ変換し、
`DataFrame.to_csv` の lineterminator の既定も os.linesep）。Windows でだけ
生成物が CRLF になり、レポートを共有したときに差分が出る。

macOS / Linux では引数を省いても出力が変わらないため、生成物を見るテスト
（golden を含む）ではこの規約の破れを検出できない。呼び出しに引数が付いていること
と、その値が LF であることを直接見るしかない。

この検査が及ぶ範囲は `src/seat_analyzer/` 配下だけで、リポジトリ全体の規約では
ない。`examples/generate_sample_data.py` はコミット対象の合成 CSV を CRLF で
書き出すが（`csv.writer` の excel dialect が全 OS で CRLF。これが正しい）、
ここでは対象にしない。

保証するのは「書き込みのときに OS 依存の変換をしない」ことだけで、出力が LF に
なることそのものではない。`newline="\n"` は「変換しない」の意味なので、渡した
文字列に `\r\n` が入っていればそのまま書き出される。実際に LF が保たれているのは
読み取り側が揃って正規化しているためで、両側で1つの不変条件になっている
（`report/html.py` の `_asset()` と `report/document.py` は `read_text()` ＝
universal newlines、Jinja の `newline_sequence` の既定は LF、`discussion.py` の
`subprocess.run(..., text=True)`）。読み取りを `read_bytes().decode()` に
替えると、CRLF でチェックアウトされたテンプレートがこの検査を素通りして出力に載る。
"""

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

from .conftest import REPO_ROOT

PACKAGE_DIR = REPO_ROOT / "src" / "seat_analyzer"

# 改行を OS 任せにする書き込み API → 固定に必須のキーワード引数と、その許容値。
# write_text / open / NamedTemporaryFile は "" も「変換しない」の意味なので許す。
# to_csv だけは "" を許さない（pandas が `lineterminator or os.linesep` と解決するため、
# 空文字を渡すと黙って os.linesep に落ちる）。
_REQUIRED_KWARG = {
    "write_text": ("newline", ("\n", "")),
    "to_csv": ("lineterminator", ("\n",)),
    "NamedTemporaryFile": ("newline", ("\n", "")),
    "open": ("newline", ("\n", "")),
}


class _Call(NamedTuple):
    """規約の対象になった書き込み呼び出し1件。"""

    path: str          # パッケージディレクトリからの相対パス
    lineno: int
    api: str
    problem: str | None    # 違反の説明。None なら規約を満たしている


def _called_name(node: ast.Call) -> str | None:
    """呼び出し先の名前（`x.write_text(...)` なら "write_text"）。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _str_literal(node: ast.expr | None) -> str | None:
    """定数の文字列リテラルなら値、そうでなければ None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in node.keywords if kw.arg == name), None)


def _opens_for_write(node: ast.Call) -> bool | None:
    """open 系の呼び出しが改行変換の対象になるか（判定できないときは None）。

    mode は `path.open("w")` なら第1位置引数、組み込みの `open(path, "w")` なら
    第2位置引数で、`mode=` キーワードでも来る。省略時は読み取りなので対象外。
    バイナリは改行変換をしないので同じく対象外。
    """
    index = 0 if isinstance(node.func, ast.Attribute) else 1
    mode_node = node.args[index] if len(node.args) > index else None
    by_keyword = _keyword(node, "mode")
    if by_keyword is not None:
        mode_node = by_keyword
    if mode_node is None:
        return False
    mode = _str_literal(mode_node)
    if mode is None:
        return None
    return any(c in mode for c in "wax") and "b" not in mode


def _scan(package_dir: Path) -> tuple[list[Path], list[_Call]]:
    """走査した .py と、そのうち規約の対象になった書き込み呼び出し。

    対象になった呼び出しは適合・違反の別なく返す。検査が本当にコードへ届いている
    ことを、違反ゼロという結果とは別に確かめられるようにするため。
    """
    paths = sorted(package_dir.rglob("*.py"))
    calls: list[_Call] = []
    for path in paths:
        rel = path.relative_to(package_dir).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            spec = _REQUIRED_KWARG.get(name)
            if spec is None:
                continue
            if name == "open":
                writes = _opens_for_write(node)
                if writes is False:
                    continue
                if writes is None:
                    calls.append(_Call(rel, node.lineno, name,
                                       "open() の mode が定数ではなく書き込みか判定できません"))
                    continue
            required, allowed = spec
            value = _keyword(node, required)
            if value is None:
                problem = f"{name}() に {required}= がありません"
            elif (literal := _str_literal(value)) is None:
                problem = f"{name}() の {required}= が定数の文字列ではありません"
            elif literal not in allowed:
                problem = f"{name}() の {required}={literal!r} は LF になりません"
            else:
                problem = None
            calls.append(_Call(rel, node.lineno, name, problem))
    return paths, calls


def _violations(package_dir: Path) -> list[str]:
    """LF が固定されていない書き込みの一覧（表示用の文字列）。"""
    _, calls = _scan(package_dir)
    broken = sorted((c for c in calls if c.problem), key=lambda c: (c.path, c.lineno))
    return [f"{c.path}:{c.lineno} {c.problem}" for c in broken]


def test_write_paths_pin_line_endings():
    """パッケージ内の書き込み経路はすべて LF を明示する。"""
    violations = _violations(PACKAGE_DIR)
    assert not violations, (
        "改行が OS 任せになっている書き込みがあります（パスは src/seat_analyzer/ から）:\n  "
        + "\n  ".join(violations)
    )


def test_checker_reaches_the_package():
    """検査がパッケージの実体に届いている（走査もパターン照合も空振りしていない）。

    違反ゼロという結果だけでは、検査が何も見ていない状態と区別できない。
    """
    paths, calls = _scan(PACKAGE_DIR)
    assert paths, f"{PACKAGE_DIR} に .py が1つも見つかりません"
    found = {c.api for c in calls}
    assert {"write_text", "to_csv", "NamedTemporaryFile"} <= found, (
        "パッケージにある書き込み API を検出できていません（検出できたのは "
        + (", ".join(sorted(found)) or "なし") + "）"
    )


# ------------------------------------------------------- 規則そのものの検査（合成ソース）

# 期待値に行番号を含めるので、ケースを足すときは各ファイルの末尾に足すこと。
FAKE_SOURCES = {
    "ok.py": r'''
path.write_text(text, encoding="utf-8", newline="\n")
path.write_text(text, newline="")
df.to_csv(path, index=False, lineterminator="\n")
tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False)
tempfile.NamedTemporaryFile(mode="w", newline="")
path.open("w", encoding="utf-8", newline="\n")
path.open(mode="a", newline="")
open(path, "x", newline="\n")
path.open(encoding="utf-8")
open(path)
path.open("rb")
path.open("wb")
json.dumps(payload)
''',
    "bad.py": r'''
path.write_text(text, encoding="utf-8")
path.write_text(text, newline="\r\n")
path.write_text(text, newline=None)
path.write_text(text, newline=sep)
df.to_csv(path, lineterminator="")
df.to_csv(path, index=False)
tempfile.NamedTemporaryFile("w", newline="\r\n")
path.open("w", encoding="utf-8")
open(path, "a", newline="\r\n")
path.open(mode)
''',
    "nested/deep.py": r'''
path.write_text(text)
''',
}


@pytest.fixture
def fake_package(tmp_path):
    root = tmp_path / "fake"
    for name, body in FAKE_SOURCES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_checker_reports_every_kind_of_break(fake_package):
    """引数の欠落・LF 以外の値・判定できない値をそれぞれ違反として返す。

    適合している呼び出し・読み取りの open・バイナリの open は返さない。
    """
    assert _violations(fake_package) == [
        "bad.py:2 write_text() に newline= がありません",
        r"bad.py:3 write_text() の newline='\r\n' は LF になりません",
        "bad.py:4 write_text() の newline= が定数の文字列ではありません",
        "bad.py:5 write_text() の newline= が定数の文字列ではありません",
        "bad.py:6 to_csv() の lineterminator='' は LF になりません",
        "bad.py:7 to_csv() に lineterminator= がありません",
        r"bad.py:8 NamedTemporaryFile() の newline='\r\n' は LF になりません",
        "bad.py:9 open() に newline= がありません",
        r"bad.py:10 open() の newline='\r\n' は LF になりません",
        "bad.py:11 open() の mode が定数ではなく書き込みか判定できません",
        "nested/deep.py:2 write_text() に newline= がありません",
    ]


def test_empty_newline_is_allowed_except_for_to_csv(fake_package):
    """`newline=""` は変換しない指定なので許すが、to_csv の空文字は os.linesep に落ちる。"""
    _, calls = _scan(fake_package)
    problem = {(c.path, c.lineno): c.problem for c in calls}
    assert problem[("ok.py", 3)] is None           # write_text(newline="")
    assert problem[("ok.py", 6)] is None           # NamedTemporaryFile(newline="")
    assert problem[("ok.py", 8)] is None           # path.open(mode="a", newline="")
    assert problem[("bad.py", 6)] is not None      # to_csv(lineterminator="")
