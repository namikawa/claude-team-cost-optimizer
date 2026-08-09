"""生成物の改行を LF に固定していることを構文木で検査する。

書き込み経路は既定で改行を OS 任せにする（`Path.write_text` と
`NamedTemporaryFile` は newline=None のとき os.linesep へ変換し、
`DataFrame.to_csv` の lineterminator の既定も os.linesep）。Windows で
だけ生成物が CRLF になり、レポートを共有したときに差分が出る。

macOS / Linux では引数を省いても出力が変わらないため、生成物を見る
テスト（golden を含む）ではこの規約の破れを検出できない。呼び出しに
引数が付いていることを直接見るしかない。
"""

import ast

from .conftest import REPO_ROOT

PACKAGE_DIR = REPO_ROOT / "src" / "seat_analyzer"

# 改行を OS 任せにする書き込み API → LF を固定するために必須のキーワード引数
_REQUIRED_KWARG = {
    "write_text": "newline",
    "to_csv": "lineterminator",
    "NamedTemporaryFile": "newline",
}


def _called_name(node: ast.Call) -> str | None:
    """呼び出し先の名前（`x.write_text(...)` なら "write_text"）。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _violations() -> list[str]:
    found = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            required = _REQUIRED_KWARG.get(name)
            if required is None:
                continue
            if not any(kw.arg == required for kw in node.keywords):
                rel = path.relative_to(REPO_ROOT)
                found.append(f"{rel}:{node.lineno} {name}() に {required}= がありません")
    return found


def test_write_paths_pin_line_endings():
    """パッケージ内の書き込み経路はすべて LF を明示する。"""
    violations = _violations()
    assert not violations, (
        "改行が OS 任せになっている書き込みがあります:\n  " + "\n  ".join(violations)
    )
