"""パッケージ内の依存が一方向であることを固定するテスト。

責務ごとにモジュールを分けても、下位のモジュールが上位を import できてしまうと
呼び出しの向きが双方向になり、分割で得たはずの「ここだけ読めば分かる」性質が消える。
そこでモジュールを層に割り当て、パッケージ内 import は自分より厳密に下の層だけを
指してよいことにする。層の割り当ては表に明示し、表に無いものは失格にする
（新しいモジュールを足すときに、置き場所を決めることを強制するため）。

規則の単位は「パッケージ直下の .py」と「`__init__.py` を持つサブディレクトリ」。
サブパッケージは配下の .py をまとめて1つの単位として扱うので、モジュールを
`report/` のようなパッケージへ分割しても規則が中まで及ぶ。パッケージ内部どうしの
import は分割の内側の話なので対象外にする。
"""

import ast
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

PACKAGE = "seat_analyzer"
PACKAGE_DIR = REPO_ROOT / "src" / PACKAGE

# 小さいほど下位。同じ層どうしの import も認めない（相互参照の芽を残さないため）。
# 番号を 10 刻みにしてあるのは、既存の層の間に新しい層を挿し込むときに
# 後続の番号を振り直さずに済ませるため。
#   10 データと計算の土台（外部入力の読み取り・値の定義）
#   20 設定と分析・検査のエンジン
#   30 出力の組み立て（レポート生成・公開テキストの検査）
#   40 考察の生成（レポートを読み書きする）
#   50 CLI
#   60 パッケージのファサード
LAYERS = {
    "domain": 10,
    "identity": 10,
    "ingest": 10,
    "pricing": 10,
    "config": 20,
    "analyze": 20,
    "data_quality": 20,
    "leakcheck": 20,
    "report": 30,
    "public_text": 30,
    "discussion": 40,
    "cli": 50,
    "__init__": 60,
}


def _units(package_dir: Path) -> dict[str, list[Path]]:
    """層の割り当て単位 → その単位に属する .py の一覧。

    直下の .py はそれ自身が単位。`__init__.py` を持つサブディレクトリは配下すべてで
    1単位（分割の内側は1つの責務とみなす）。`__init__.py` を持たないディレクトリ
    （テンプレート・プロンプト等の資材置き場）は単位にならない。

    同名の単位が2つできる（`report.py` と `report/` が並ぶ）場合は失格にする。
    片方が黙って上書きされると、その中身が検査対象から丸ごと外れるため。
    """
    units: dict[str, list[Path]] = {}
    for entry in sorted(package_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".py":
            name, paths = entry.stem, [entry]
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            name, paths = entry.name, sorted(entry.rglob("*.py"))
        else:
            continue
        if name in units:
            raise AssertionError(
                f"同名の単位が2つあります: {name}（{name}.py と {name}/）。"
                "\n.py と同名パッケージの共存は Python 側でも .py が無視される状態です。"
                "どちらかへ寄せてください"
            )
        units[name] = paths
    return units


def _module_parts(path: Path, package_dir: Path) -> list[str]:
    """package_dir からの相対モジュール名（`__init__` は畳む）。"""
    parts = list(path.relative_to(package_dir).with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def _imported_units(path: Path, package_dir: Path, package: str) -> set[str]:
    """path がパッケージ内から import している単位の名前。

    `from . import x` / `from ..x import y` の相対形と、`from seat_analyzer.x import y` /
    `import seat_analyzer.x` の絶対形の両方を拾い、先頭の要素（＝単位名）へ畳む。
    """
    module_parts = _module_parts(path, package_dir)
    # 相対 import の起点。`__init__.py` はそのパッケージ自身が起点になる
    base = module_parts if path.name == "__init__.py" else module_parts[:-1]

    targets: list[list[str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # from . / from .. / from .x
                up = node.level - 1
                if up > len(base):
                    continue  # パッケージの外を指している（ここでは扱わない）
                here = base[:len(base) - up]
                targets.extend(
                    [here + node.module.split(".")] if node.module
                    else [here + [a.name] for a in node.names]
                )
            elif node.module and node.module.split(".")[0] == package:
                parts = node.module.split(".")[1:]
                targets.extend(
                    [parts] if parts else [[a.name] for a in node.names])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == package and len(parts) > 1:
                    targets.append(parts[1:])

    own = module_parts[0] if module_parts else None
    return {t[0] for t in targets if t and t[0] != own}


def _violations(package_dir: Path, package: str, layers: dict[str, int]) -> list[str]:
    """層の規則に反するパッケージ内 import の一覧（表示用の文字列）。"""
    def label(name: str) -> str:
        layer = layers.get(name)
        return f"層{layer}" if layer is not None else "層の割り当てなし"

    found = []
    for unit, paths in _units(package_dir).items():
        src = layers.get(unit)
        for path in paths:
            for dst in sorted(_imported_units(path, package_dir, package)):
                target = layers.get(dst)
                if src is None or target is None or src <= target:
                    found.append(f"{unit}({label(unit)}) → {dst}({label(dst)})")
    return sorted(set(found))


def test_every_unit_has_a_layer():
    """層の表と実体を一致させる（追加・削除の取りこぼしを防ぐ）。"""
    assert set(_units(PACKAGE_DIR)) == set(LAYERS)


def test_intra_package_imports_point_downward():
    """パッケージ内 import は自分より厳密に下の層だけを指す。"""
    violations = _violations(PACKAGE_DIR, PACKAGE, LAYERS)
    assert not violations, (
        "パッケージ内の依存が下向きになっていません:\n  " + "\n  ".join(violations)
        + "\n（依存を下向きに直すか、層の割り当てを見直してください）"
    )


# ------------------------------------------------------- 規則そのものの検査（合成パッケージ）


FAKE_LAYERS = {"low": 10, "pack": 20, "high": 30, "__init__": 60}

FAKE_FILES = {
    "__init__.py": "",
    "low.py": "",
    "high.py": (
        "from .low import a\n"          # 下向き: 違反しない
        "from .orphan import b\n"       # 層の割り当てが無い相手: 違反
    ),
    "orphan.py": "",
    "pack/__init__.py": "from .inner import c\n",   # 自パッケージ内: 対象外
    "pack/inner.py": (
        "from .sibling import d\n"      # 自パッケージ内: 対象外
        "from ..low import e\n"         # 下向き: 違反しない
        "from ..high import f\n"        # 上向き: 違反（サブパッケージの中まで規則が及ぶ）
        "import fake.high\n"            # 絶対形も同じ違反として拾う
    ),
    "pack/sibling.py": "",
    "assets/helper.py": "from ..high import g\n",   # __init__.py が無いので単位にならない
}


@pytest.fixture
def fake_package(tmp_path):
    root = tmp_path / "fake"
    for name, body in FAKE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_units_ignore_directories_without_init(fake_package):
    """`__init__.py` を持たないディレクトリ（資材置き場）は単位にしない。"""
    assert set(_units(fake_package)) == {"__init__", "low", "high", "orphan", "pack"}


def test_rule_reaches_into_subpackages(fake_package):
    """サブパッケージの中の import にも規則が及び、内部どうしの import は見逃す。"""
    assert _violations(fake_package, "fake", FAKE_LAYERS) == [
        "high(層30) → orphan(層の割り当てなし)",
        "pack(層20) → high(層30)",
    ]


COLLIDING_FILES = {
    "__init__.py": "",
    "low.py": "",
    "dup.py": "",
    "dup/__init__.py": "from ..low import a\n",
    "dup/inner.py": "from ..low import b\n",
}


@pytest.fixture
def colliding_package(tmp_path):
    """`dup.py` と `dup/` が並ぶ、単位名が衝突するパッケージ。"""
    root = tmp_path / "collide"
    for name, body in COLLIDING_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_units_reject_module_and_package_with_the_same_name(colliding_package):
    """.py と同名パッケージが並んだら失格にする。

    片方が黙って上書きされると、その中の import が検査対象から丸ごと外れ、
    上向きの依存があってもテストは緑のままになる。
    """
    with pytest.raises(AssertionError, match="同名の単位が2つあります: dup"):
        _units(colliding_package)
