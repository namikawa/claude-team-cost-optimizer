"""レポート生成物を golden ファイルと丸ごと突き合わせるテスト。

既存のテストは生成物に対する部分文字列 assert しか持っておらず、テンプレートの
組み立てで生じた空白のずれや HTML 断片の欠落を検出できない。出力形式ごとに
モジュールを分けるような移動の前後で「見た目は緑のまま出力が壊れる」ことが
起きうるので、生成物そのものを固定して比較する。

入力は `examples/input/` の合成データ（org-a / org-b）だけを使う。リポジトリ
直下の `input/` は実データなので、golden がそこに依存すると手元の月次運用で
テストが揺れる。CLI の引数は既定値に頼らず毎回明示する。

ケースは2軸で持つ。2組織そろった 2026-06 が土台（組織横断サマリを含む通常の
出力構成）で、org-b の 2026-07 が条件付きの断片を通すためのケース。後者が無いと、
同一月に複数スナップショットがあるときだけ描画される断片（月中の利用推移・
Claude Code 活動・メンバー変動）が golden の経路に一度も乗らず、HTML から
丸ごと消えても誰も気づけない。

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
from typing import NamedTuple

import pytest

from seat_analyzer.cli import main

from .conftest import CONFIG, REPO_ROOT

EXAMPLES_INPUT = REPO_ROOT / "examples" / "input"
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden"


class Case(NamedTuple):
    """golden 1ケース分の analyze 実行条件。辞書のキーが golden ツリーの名前になる。"""

    month: str
    args: tuple[str, ...] = ()


# 速報は --days をファイル名からの自動判別に任せず明示する
# （ファイル名の期間が変わると観測日数が変わり、golden が揺れるため）。
CASES = {
    # 2組織そろった月。組織横断サマリ（summary/<月>.md）が出るのはこの構成だけ。
    "full": Case("2026-06"),
    "preview": Case("2026-06", ("--preview", "--days", "10")),
    # 条件付きの断片を通すケース。同一月に複数スナップショットを持つのは
    # org-b の 2026-07 だけで、org-a にこの月のデータは無いので --org で絞る。
    "full-snapshots": Case("2026-07", ("--org", "org-b")),
    # --days は 31（暦日数と同じ＝月末ペース換算 ×1.0）。この月の spend は期間の
    # 最も広い 07-01-to-07-31 が主データに採用されるので、観測日数もその期間に
    # 合わせる。短い値を書くと、月全体のデータを部分月と偽って割り増した数字が
    # golden に固定される。
    "preview-snapshots": Case("2026-07", ("--org", "org-b", "--preview", "--days", "31")),
}

# 差分は該当箇所が分かれば十分なので、1ファイルあたりこの行数で打ち切る。
_MAX_DIFF_LINES = 60


def _relative_files(root: Path, *, skip_hidden: bool) -> set[str]:
    """root 配下のファイルを相対パスの集合で返す。

    skip_hidden はドットで始まる名前を除く。コミット済みの golden ツリーには Finder が
    .DS_Store を置くことがあるので、除外は golden 側にだけ適用する。生成物側は
    数え漏らさない（隠しファイルであっても、増えたなら「増えた」と言えるべきなので）。
    """
    found = set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if skip_hidden and any(part.startswith(".") for part in rel.parts):
            continue
        found.add(str(rel))
    return found


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


def _generate(tmp_path: Path, case: Case) -> Path:
    output_dir = tmp_path / "out"
    rc = main([
        "analyze", "--config", CONFIG,
        "--input-dir", str(EXAMPLES_INPUT),
        "--month", case.month,
        "--output-dir", str(output_dir),
        *case.args,
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
    expected_files = _relative_files(golden_dir, skip_hidden=True)
    actual_files = _relative_files(output_dir, skip_hidden=False)
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
