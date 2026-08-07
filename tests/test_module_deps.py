"""パッケージ内の依存が一方向であることを固定するテスト。

責務ごとにモジュールを分けても、下位のモジュールが上位を import できてしまうと
呼び出しの向きが双方向になり、分割で得たはずの「ここだけ読めば分かる」性質が消える。
そこでモジュールを層に割り当て、パッケージ内 import は自分より厳密に下の層だけを
指してよいことにする。層の割り当ては表に明示し、表に無いモジュールは失格にする
（新しいモジュールを足すときに、置き場所を決めることを強制するため）。
"""

import ast
from pathlib import Path

from .conftest import REPO_ROOT

PACKAGE = "seat_analyzer"
PACKAGE_DIR = REPO_ROOT / "src" / PACKAGE

# 小さいほど下位。同じ層どうしの import も認めない（相互参照の芽を残さないため）。
#   0 データと計算の土台（外部入力の読み取り・値の定義）
#   1 設定と分析・検査のエンジン
#   2 出力の組み立て（レポート生成・公開テキストの検査）
#   3 考察の生成（レポートを読み書きする）
#   4 CLI
#   5 パッケージのファサード
LAYERS = {
    "domain": 0,
    "identity": 0,
    "ingest": 0,
    "pricing": 0,
    "config": 1,
    "analyze": 1,
    "data_quality": 1,
    "leakcheck": 1,
    "report": 2,
    "public_text": 2,
    "discussion": 3,
    "cli": 4,
    "__init__": 5,
}


def _modules() -> list[Path]:
    return sorted(PACKAGE_DIR.glob("*.py"))


def _intra_imports(path: Path) -> set[str]:
    """path がパッケージ内から import しているモジュール名。

    `from . import x` / `from .x import y` の相対形と、`from seat_analyzer.x import y` /
    `import seat_analyzer.x` の絶対形の両方を拾う。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # from . / from .x
                names.update(
                    {node.module.split(".")[0]} if node.module
                    else {a.name for a in node.names}
                )
            elif node.module and node.module.split(".")[0] == PACKAGE:
                parts = node.module.split(".")
                names.update(
                    {parts[1]} if len(parts) > 1 else {a.name for a in node.names})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == PACKAGE and len(parts) > 1:
                    names.add(parts[1])
    return {n for n in names if n != path.stem}


def test_every_module_has_a_layer():
    """層の表とモジュールの実体を一致させる（追加・削除の取りこぼしを防ぐ）。"""
    assert {p.stem for p in _modules()} == set(LAYERS)


def test_intra_package_imports_point_downward():
    """パッケージ内 import は自分より厳密に下の層だけを指す。"""
    violations = []
    for path in _modules():
        src = path.stem
        for dst in sorted(_intra_imports(path)):
            if LAYERS[src] <= LAYERS[dst]:
                violations.append(f"{src}(層{LAYERS[src]}) → {dst}(層{LAYERS[dst]})")
    assert not violations, (
        "パッケージ内の依存が下向きになっていません:\n  " + "\n  ".join(violations)
        + "\n（依存を下向きに直すか、層の割り当てを見直してください）"
    )
