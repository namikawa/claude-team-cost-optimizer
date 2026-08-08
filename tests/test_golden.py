"""レポート生成物を golden ファイルと丸ごと突き合わせるテスト。

既存のテストは生成物に対する部分文字列 assert しか持っておらず、テンプレートの
組み立てで生じた空白のずれや HTML 断片の欠落を検出できない。出力形式ごとに
モジュールを分けるような移動の前後で「見た目は緑のまま出力が壊れる」ことが
起きうるので、生成物そのものを固定して比較する。

入力は `examples/input/` の合成データ（org-a / org-b）だけを使う。リポジトリ
直下の `input/` は実データなので、golden がそこに依存すると手元の月次運用で
テストが揺れる。CLI の引数は既定値に頼らず毎回明示する。

golden の更新手順:

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
    git diff tests/golden/

出力が変わるのが正しい変更（`config.yaml` の料金改定、レポート項目の追加、
テンプレートの意図的な修正など）のときだけ再生成する。再生成した diff は
必ず目視で意図どおりか確認すること。golden を機械的に更新して緑にするのは、
このテストが持つ唯一の保証を捨てるのと同じになる。
"""

import difflib
import os
import shutil
import warnings
from pathlib import Path

import pytest

from seat_analyzer.cli import main

from .conftest import CONFIG, REPO_ROOT

EXAMPLES_INPUT = REPO_ROOT / "examples" / "input"
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden"
MONTH = "2026-06"

# 速報は --days をファイル名からの自動判別に任せず明示する
# （ファイル名の期間が変わると観測日数が変わり、golden が揺れるため）。
CASES = {
    "full": (),
    "preview": ("--preview", "--days", "10"),
}

# 差分は該当箇所が分かれば十分なので、1ファイルあたりこの行数で打ち切る。
_MAX_DIFF_LINES = 60


def _files(root: Path) -> set[str]:
    """root 配下のファイルを相対パスの集合で返す。

    ドットで始まる名前は除く。ツール側は隠しファイルを出力しないので、これは
    コミット済みの golden ツリーに Finder が置く .DS_Store 等を無視するためだけの
    規則になる（生成物側は毎回新しい tmp_path なので混入しない）。
    """
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    }


def _diff_lines(text: str) -> list[str]:
    """差分表示用の行分割。末尾改行の有無を明示行として残す。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 末尾改行あり: 最後の空要素は行ではない
    else:
        lines.append(r"\ 末尾に改行なし")
    return lines


def _diff(rel: str, expected: str, actual: str) -> str:
    """golden と生成物の unified diff。長い場合は先頭で打ち切る。"""
    lines = list(difflib.unified_diff(
        _diff_lines(expected), _diff_lines(actual),
        fromfile=f"golden/{rel}", tofile=f"actual/{rel}", lineterm="", n=2,
    ))
    shown = lines[:_MAX_DIFF_LINES]
    if len(lines) > _MAX_DIFF_LINES:
        shown.append(f"... 以下 {len(lines) - _MAX_DIFF_LINES} 行省略")
    return "\n".join(shown)


def _read(path: Path) -> str:
    """バイト列として読んでから復号する。

    read_text() は改行コードを変換して読むため、CRLF と LF の違いが消える。
    空白・改行の差を見つけるのがこのテストの目的なので変換させない。
    """
    return path.read_bytes().decode("utf-8")


def _generate(tmp_path: Path, extra: tuple[str, ...]) -> Path:
    output_dir = tmp_path / "out"
    rc = main([
        "analyze", "--config", CONFIG,
        "--input-dir", str(EXAMPLES_INPUT),
        "--month", MONTH,
        "--output-dir", str(output_dir),
        *extra,
    ])
    assert rc == 0
    return output_dir


def _update_golden(golden_dir: Path, output_dir: Path, case: str) -> None:
    """golden を生成物で置き換える。

    消してから丸ごとコピーする。上書きだけだと、出力されなくなったファイルが
    golden に残り続けて「生成が止まった」ことを検出できなくなる。
    """
    if golden_dir.exists():
        shutil.rmtree(golden_dir)
    shutil.copytree(output_dir, golden_dir)
    # 黙って緑になると、意図しない更新にも更新忘れにも気づけない。
    # pytest の warnings summary は -s なしでも出る。
    warnings.warn(
        f"UPDATE_GOLDEN=1: tests/golden/{case}/ を生成物で上書きしました。"
        f"git diff tests/golden/ を目視で確認してください",
        UserWarning, stacklevel=2,
    )


@pytest.mark.parametrize("case", sorted(CASES))
def test_golden_report_outputs(case: str, tmp_path: Path) -> None:
    output_dir = _generate(tmp_path, CASES[case])
    golden_dir = GOLDEN_ROOT / case

    if os.environ.get("UPDATE_GOLDEN") == "1":
        _update_golden(golden_dir, output_dir, case)

    assert golden_dir.is_dir(), (
        f"golden がありません: {golden_dir}"
        "（UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py で生成）"
    )

    # まずファイル構成を突き合わせる。個々の内容だけを比べると、生成されなく
    # なったファイルも、増えたファイルも見逃す。
    expected_files = _files(golden_dir)
    actual_files = _files(output_dir)
    assert actual_files == expected_files, (
        f"[{case}] 生成ファイルの構成が golden と違います\n"
        f"  golden に無い（増えた）: {sorted(actual_files - expected_files)}\n"
        f"  生成されなかった: {sorted(expected_files - actual_files)}"
    )

    diffs = []
    for rel in sorted(expected_files):
        expected = _read(golden_dir / rel)
        actual = _read(output_dir / rel)
        if actual != expected:
            diffs.append(_diff(f"{case}/{rel}", expected, actual))
    assert not diffs, (
        f"[{case}] 生成物が golden と一致しません（{len(diffs)} ファイル）\n"
        + "\n\n".join(diffs)
        + "\n\nこの変更が意図どおりなら UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"
    )
