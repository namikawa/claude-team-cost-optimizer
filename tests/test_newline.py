r"""生成物の改行を LF に固定していることを構文木で検査する。

書き込み経路は既定で改行を OS 任せにする（`Path.write_text`・`Path.open`・
`os.fdopen`・`NamedTemporaryFile` は newline=None のとき os.linesep へ変換し、
`DataFrame.to_csv` の lineterminator の既定も os.linesep）。Windows でだけ
生成物が CRLF になり、レポートを共有したときに差分が出る。

macOS / Linux では引数を省いても出力が変わらないため、生成物を見るテスト
（golden を含む）ではこの規約の破れを検出できない。呼び出しに引数が付いていること
と、その値が LF であることを直接見るしかない。

この検査が及ぶ範囲は `src/seat_analyzer/` 配下の、`_WRITE_APIS` に挙げた API を
呼ぶ箇所だけで、リポジトリ全体の規約ではない。次は対象外になる。

- `examples/generate_sample_data.py`。コミット対象の合成 CSV を CRLF で書き出すが
  （`csv.writer` の excel dialect が全 OS で CRLF）、それが正しい
- `io.TextIOWrapper`・`tempfile.TemporaryFile`・`SpooledTemporaryFile`
- `print()` と `sys.stdout`。`cli.py` の `--format json` は Windows では CRLF で出る
- `path.open("w", newline="")` に `csv.writer` を重ねた書き込み。規則は満たすが、
  レコード区切りを書くのは csv 側なので出力は全 OS で CRLF になる
- mode を定数で書いていない `open` 系。書き込みかどうかを構文木から決められない。
  黙って外れないよう `_scan` が3つ目の戻り値として数え、ゼロであることを別に主張する

照合は呼び出し先の名前だけで行い、レシーバは見ない。mode を取る API では mode 文字列
らしさ（`_MODE_CHARS`）まで確かめてから対象にするが、次の取り違えは残る。

- ファイル名が mode に使える文字だけでできている `open` は、ファイル名を mode と読む。
  `open("rb", "w")` は読み取りに見えて外れ、`open("a")` は書き込みに見えて拾われる
- `zipfile.ZipFile.open` や `tarfile.TarFile.open` のように、名前が衝突していて
  `newline=` を取らない API を書き込みで呼ぶと違反として出る。抑制する手段は無い

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
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from .conftest import REPO_ROOT

PACKAGE_DIR = REPO_ROOT / "src" / "seat_analyzer"

# mode 文字列に現れうる文字。これ以外を含む文字列は mode ではないとみなす
# （`webbrowser.open("http://...")` のような同名の別 API を対象にしないため）
_MODE_CHARS = set("rwaxbt+")


class _Api(NamedTuple):
    """改行を OS 任せにする書き込み API 1つ分の規則。"""

    kwarg: str                        # LF を固定するために要るキーワード引数
    allowed: tuple[str, ...]          # その引数に許す値
    mode_at: tuple[int, ...] | None   # mode が来る位置引数の候補。None は mode を取らない API


# 検査する API 名 → 規則。
#
# mode を取る API はテキストの書き込みのときだけ対象にする（binary には newline= を
# 渡せず ValueError になり、読み取りには意味が無いため）。mode の位置は AST の形では
# なく API ごとに持つ。`from tempfile import NamedTemporaryFile` のように import の
# 書き方で呼び出しの形が変わっても、mode の位置は変わらないため。
#
# 許容値の "" は newline と同じく「変換しない」の意味。to_csv だけは "" を許さない
# （pandas が `lineterminator or os.linesep` と解決するので、空文字は os.linesep に落ちる）。
_WRITE_APIS = {
    "write_text": _Api("newline", ("\n", ""), None),
    "to_csv": _Api("lineterminator", ("\n",), None),
    "open": _Api("newline", ("\n", ""), (0, 1)),        # path.open(mode) / open(path, mode)
    "fdopen": _Api("newline", ("\n", ""), (1,)),        # os.fdopen(fd, mode)
    "NamedTemporaryFile": _Api("newline", ("\n", ""), (0,)),
}


class _Call(NamedTuple):
    """検査が見た書き込み呼び出し1件。"""

    path: str              # パッケージディレクトリからの相対パス
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


def _mode_candidates(node: ast.Call, positions: tuple[int, ...]) -> list[ast.expr]:
    """mode が書かれている可能性のある引数（`mode=` キーワードと位置引数の候補）。"""
    by_keyword = _keyword(node, "mode")
    args = [node.args[i] for i in positions if len(node.args) > i]
    return ([by_keyword] if by_keyword is not None else []) + args


def _has_star_kwargs(node: ast.Call) -> bool:
    """`**opts` で引数を渡しているか（何が入っているか構文木からは分からない）。"""
    return any(kw.arg is None for kw in node.keywords)


def _mode_literal(candidates: list[ast.expr]) -> str | None:
    """候補から mode の文字列を取る（見つからなければ None）。

    mode に使えない文字を含む文字列は mode ではないとみなし、候補の先を探す。
    """
    for candidate in candidates:
        mode = _str_literal(candidate)
        if mode and set(mode) <= _MODE_CHARS:
            return mode
    return None


def _writes_text(mode: str | None) -> bool:
    """その mode がテキストの書き込みか（＝改行変換が働くか）。"""
    return mode is not None and any(c in mode for c in "wax") and "b" not in mode


def _scan(package_dir: Path) -> tuple[list[Path], list[_Call], list[_Call]]:
    """走査した .py と、規約の対象になった呼び出しと、mode を読めなかった呼び出し。

    2つ目は適合・違反の別なく返す。検査が本当にコードへ届いていることを、違反ゼロと
    いう結果とは別に確かめられるようにするため。

    3つ目は、mode らしい文字列が見つからず、かつ mode が隠れうる書き方（候補の中の
    非リテラル、`**opts`）が残っている呼び出し。書き込みかどうかを決められないので
    対象から外すしかなく、外した事実を数として残す。`open` は mode の位置が呼び出しの
    形で変わるため、位置引数を1つだけ渡した読み取り（`open(path)`）も、mode ではない
    文字列と非リテラルが並ぶ呼び出し（`tarfile.open(path, "w:gz")`）もここに入る。
    """
    paths = sorted(package_dir.rglob("*.py"))
    calls: list[_Call] = []
    unreadable: list[_Call] = []
    for path in paths:
        rel = path.relative_to(package_dir).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            api = _WRITE_APIS.get(name)
            if api is None:
                continue
            if api.mode_at is not None:
                candidates = _mode_candidates(node, api.mode_at)
                mode = _mode_literal(candidates)
                if not _writes_text(mode):
                    unread = _has_star_kwargs(node) or any(
                        _str_literal(c) is None for c in candidates)
                    if mode is None and unread:
                        unreadable.append(_Call(rel, node.lineno, name, None))
                    continue
            value = _keyword(node, api.kwarg)
            if value is None:
                problem = f"{name}() に {api.kwarg}= がありません"
            elif (literal := _str_literal(value)) is None:
                problem = f"{name}() の {api.kwarg}= が定数の文字列ではありません"
            elif literal not in api.allowed:
                problem = f"{name}() の {api.kwarg}={literal!r} は LF になりません"
            else:
                problem = None
            calls.append(_Call(rel, node.lineno, name, problem))
    return paths, calls, unreadable


def _at(calls: list[_Call]) -> list[tuple[str, int]]:
    """呼び出しの位置（ファイル・行）を並べ替えて返す。"""
    return sorted((c.path, c.lineno) for c in calls)


def _violations(package_dir: Path) -> list[str]:
    """LF が固定されていない書き込みの一覧（表示用の文字列）。"""
    _, calls, _ = _scan(package_dir)
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
    paths, calls, _ = _scan(PACKAGE_DIR)
    assert paths, f"{PACKAGE_DIR} に .py が1つも見つかりません"
    found = {c.api for c in calls}
    assert {"write_text", "to_csv", "NamedTemporaryFile"} <= found, (
        "パッケージにある書き込み API を検出できていません（検出できたのは "
        + (", ".join(sorted(found)) or "なし") + "）"
    )


def test_no_write_call_hides_its_mode():
    """mode を読めずに対象から外した呼び出しが無い。

    外した呼び出しは以降どう書き換えても検査に掛からない。数がゼロでなくなったら、
    その時点で見えるようにする。
    """
    _, _, unreadable = _scan(PACKAGE_DIR)
    assert not unreadable, (
        "mode を構文木から読めない書き込み呼び出しがあります:\n  "
        + "\n  ".join(f"{c.path}:{c.lineno} {c.api}()" for c in unreadable)
        + "\n（mode を定数で書くか、`path.open(...)` の形に寄せてください。"
        "変数のパスを読むだけなら `open(p, \"r\", ...)` と mode を明示すれば通ります。"
        "`tarfile.open` のように名前が衝突していて `newline=` を取れない API なら、"
        "この検査の想定の外なので `_WRITE_APIS` の作りから見直してください）"
    )


# ------------------------------------------------------- 規則そのものの検査（合成ソース）

# 各行の `# want:` が期待値。違反するケースは違反の説明を、しないケースは ok を、
# mode を読めず対象から外すケースは unreadable と書く。期待値はこのマーカーから
# 導出するので、ケースの追加も並べ替えも1箇所で済む。
_OK = "ok"
_UNREADABLE = "unreadable"

FAKE_SOURCES = {
    "ok.py": r'''
path.write_text(text, encoding="utf-8", newline="\n")             # want: ok
path.write_text(text, newline="")                                 # want: ok
df.to_csv(path, index=False, lineterminator="\n")                 # want: ok
tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n")  # want: ok
# import の書き方が変わっても mode の位置は変わらない
NamedTemporaryFile(mode="w", newline="")                          # want: ok
# mode を省いた NamedTemporaryFile は binary（newline= を渡すと実行時に落ちる）
tempfile.NamedTemporaryFile(dir=d, delete=False)                  # want: ok
path.open("w", encoding="utf-8", newline="\n")                    # want: ok
path.open(mode="a", newline="")                                   # want: ok
open(path, "x", newline="\n")                                     # want: ok
io.open(path, "w", newline="\n")                                  # want: ok
os.fdopen(fd, "w", newline="\n")                                  # want: ok
# 読み取りとバイナリは改行変換をしない
path.open(encoding="utf-8")                                       # want: ok
os.fdopen(fd)                                                     # want: ok
path.open("rb")                                                   # want: ok
path.open("wb")                                                   # want: ok
# mode に使えない文字を含む引数は mode ではない
webbrowser.open("http://www.example.com")                         # want: ok
open("a.txt")                                                     # want: ok
# ファイル名が mode 文字だけでできていると mode と取り違える。承知のうえで見逃す
open("rb", "w")                                                   # want: ok
json.dumps(payload)                                               # want: ok
# mode を決められない呼び出しは、対象から外したことを数える
path.open(chosen_mode)                                            # want: unreadable
open(path, chosen_mode)                                           # want: unreadable
open("/tmp/x", chosen_mode)                                       # want: unreadable
open(path)                                                        # want: unreadable
tarfile.open(path, "w:gz")                                        # want: unreadable
# ** で渡されると mode も newline も構文木からは見えない
path.open(**opts)                                                 # want: unreadable
open(path, **opts)                                                # want: unreadable
os.fdopen(fd, **opts)                                             # want: unreadable
tempfile.NamedTemporaryFile(**opts)                               # want: unreadable
''',
    "bad.py": r'''
path.write_text(text, encoding="utf-8")         # want: write_text() に newline= がありません
path.write_text(text, newline="\r\n")           # want: write_text() の newline='\r\n' は LF になりません
path.write_text(text, newline=None)             # want: write_text() の newline= が定数の文字列ではありません
path.write_text(text, newline=sep)              # want: write_text() の newline= が定数の文字列ではありません
df.to_csv(path, lineterminator="")              # want: to_csv() の lineterminator='' は LF になりません
df.to_csv(path, index=False)                    # want: to_csv() に lineterminator= がありません
tempfile.NamedTemporaryFile("w", delete=False)  # want: NamedTemporaryFile() に newline= がありません
NamedTemporaryFile("w", newline="\r\n")         # want: NamedTemporaryFile() の newline='\r\n' は LF になりません
path.open("w", encoding="utf-8")                # want: open() に newline= がありません
path.open(mode="w", encoding="utf-8")           # want: open() に newline= がありません
open("out.csv", "w")                            # want: open() に newline= がありません
open(path, "a", newline="\r\n")                 # want: open() の newline='\r\n' は LF になりません
os.fdopen(fd, "w")                              # want: fdopen() に newline= がありません
open(path, "x")                                 # want: open() に newline= がありません
path.open("wt", encoding="utf-8")               # want: open() に newline= がありません
path.open("w+", encoding="utf-8")               # want: open() に newline= がありません
open("rb", mode="w")                            # want: open() に newline= がありません
path.write_text(text, newline=3)                # want: write_text() の newline= が定数の文字列ではありません
''',
    "nested/deep.py": r'''
path.write_text(text)                           # want: write_text() に newline= がありません
''',
}


def _markers(sources: dict[str, str]) -> list[tuple[str, int, str]]:
    """合成ソースの `# want:` を（ファイル・行・期待値）にする。"""
    return sorted(
        (name, lineno, marker)
        for name, body in sources.items()
        for lineno, line in enumerate(body.splitlines(), start=1)
        if (marker := line.partition("# want:")[2].strip())
    )


def _expected_violations(sources: dict[str, str]) -> list[str]:
    return [
        f"{name}:{lineno} {marker}"
        for name, lineno, marker in _markers(sources)
        if marker not in (_OK, _UNREADABLE)
    ]


def _expected_unreadable(sources: dict[str, str]) -> list[tuple[str, int]]:
    return [
        (name, lineno)
        for name, lineno, marker in _markers(sources)
        if marker == _UNREADABLE
    ]


@pytest.fixture
def fake_package(tmp_path):
    root = tmp_path / "fake"
    for name, body in FAKE_SOURCES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_every_case_carries_an_expected_marker():
    """合成ソースの全ケースにマーカーが付いていて、3種の期待値が揃っている。

    期待値をマーカーから導出しているので、マーカーの無い行は黙って検査から外れる。
    """
    markers = []
    for name, body in FAKE_SOURCES.items():
        for lineno, line in enumerate(body.splitlines(), start=1):
            code = line.strip()
            if not code or code.startswith("#"):
                continue
            marker = line.partition("# want:")[2].strip()
            assert marker, f"{name}:{lineno} に # want: マーカーがありません"
            markers.append(marker)
    assert _OK in markers, "適合するケースがありません"
    assert _UNREADABLE in markers, "mode を読めないケースがありません"
    assert [m for m in markers if m not in (_OK, _UNREADABLE)], "違反するケースがありません"


def test_checker_reports_exactly_the_marked_breaks(fake_package):
    """引数の欠落・LF 以外の値・判定できない値をマーカーどおりに違反として返す。

    適合している呼び出し・読み取り・バイナリ・mode を取らない同名 API は返さない。
    """
    assert _violations(fake_package) == _expected_violations(FAKE_SOURCES)


def test_checker_records_the_modes_it_cannot_read(fake_package):
    """mode を読めなかった呼び出しをマーカーどおりに数える。

    文字列リテラルが1つでもあれば（mode ではない文字列でも）読めなかったとはしない。
    """
    _, _, unreadable = _scan(fake_package)
    assert _at(unreadable) == _expected_unreadable(FAKE_SOURCES)


# ------------------------------------------------------- チェックアウト側の規約（.gitattributes）

# 単位が違う（構文木ではなく git の属性）ので節を分ける。守る対象は同じ不変条件で、
# こちらは「ワークツリーへ出す側」の改行を見る。

# CR を含む改行。git は index と作業ツリーのそれぞれについてこの形を報告する
# （`none` は改行なし、空文字は作業ツリーに実体が無いファイル）
_EOL_WITH_CR = ("crlf", "mixed")


def test_files_with_cr_are_excluded_from_normalization():
    """CR を含むファイルには `-text` が付いている。

    `.gitattributes` の `* text=auto eol=lf` は、除外しないファイルの CR をコミット時に
    落とす。CRLF のまま扱うファイル（合成サンプルの CSV）を後から足したときに、除外の
    追記を忘れると index の中身が黙って LF に変わり、作業ツリーだけが CRLF で残る。
    index と作業ツリーのどちらかに CR があれば拾えるので、両方を見る。

    見るのは CR の有無だけで、index と作業ツリーが揃っているかは見ない。作業ツリーに
    実体が無いファイル（削除・移動の途中）や、改行の無いファイルに1行足した状態でも
    片側だけが変わるが、どちらもこの規約とは関係がない。
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--eol"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git を実行できない環境")
    assert proc.stdout.strip(), "git ls-files --eol が何も返しません"

    unexpected = []
    for line in proc.stdout.splitlines():
        info, _, path = line.partition("\t")
        fields = info.split(maxsplit=2)
        index_eol = fields[0].removeprefix("i/")
        work_eol = fields[1].removeprefix("w/")
        attrs = info.partition("attr/")[2].split()
        if "-text" in attrs:
            continue
        if index_eol in _EOL_WITH_CR or work_eol in _EOL_WITH_CR:
            unexpected.append(f"{path}（index {index_eol} / 作業ツリー {work_eol}）")
    assert not unexpected, (
        "`eol=lf` の対象なのに CR を含むファイルがあります:\n  "
        + "\n  ".join(unexpected)
        + "\n（CRLF のまま扱うなら .gitattributes に -text を足し、"
        "そうでなければ内容を LF に直してください）"
    )
