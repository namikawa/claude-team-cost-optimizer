"""レポート生成物を golden ファイルと丸ごと突き合わせるテスト。

既存のテストは生成物に対する部分文字列 assert しか持っておらず、テンプレートの
組み立てで生じた空白のずれや HTML 断片の欠落を検出できない。出力形式ごとに
モジュールを分けるような移動の前後で「見た目は緑のまま出力が壊れる」ことが
起きうるので、生成物そのものを固定して比較する。

入力は `examples/input/` の合成データ（org-a / org-b / org-c）と、それに合わせた
上書き設定 `examples/config.yaml`（org-c の workspace の設定だけを持ち、org-a /
org-b には効かない）だけを使う。リポジトリ直下の `input/` は実データなので、golden が
そこに依存すると手元の月次運用でテストが揺れる。入出力と月の CLI 引数は既定値に頼らず
毎回明示する。判定の版は、既定（v2）の経路を固定する full・full-snapshots・full-all では
指定せず、V1 の不変を固定する full-v1・full-snapshots-v1・full-all-v1 では
--decision-version v1 を明示する。

ケースは3軸で持つ。2組織そろった 2026-06 が土台（組織横断サマリを含む通常の
出力構成）で、org-b の 2026-07 が条件付きの断片を通すためのケース。後者が無いと、
同一月に複数スナップショットがあるときだけ描画される断片（月中の利用推移・
Claude Code 活動・メンバー変動）が golden の経路に一度も乗らず、HTML から
丸ごと消えても誰も気づけない。

3つ目の org-b 2026-07 の翌月（2026-08）は、条件つき section がすべて出る
「全部入り」のケース。部署・チーム別サマリと月中差分が同時に出る組み合わせは
他のケースに無く、レポートの見た目を作り直すときの入力にもなる。同じ月に、2つの
Team スペース（workspace）を運用する org-c を並べ、複数 workspace の組織だけが持つ
成果物の形（スペース列・workspace ごとの節・人の層・複数スペースのタブ）と、人数と
アカウント数を併記する組織横断サマリを固定する。full-all はこの入力の V2 の全部入り、
full-all-v1 は同じ入力の V1 の主成果物で、複数 workspace の形で V1 の不変を検査する。
full は 2026-06 の2組織を既定の V2 で分析した主成果物・decision-evidence・組織横断サマリ。
V1 の3ケースは、切り替え前の成果物（単一 workspace・月中差分・複数 workspace）を
固定したもので再生成しない。full-v1・full-snapshots-v1 は切り替え前の full・
full-snapshots を名前替えで残したもの。

org-c は 2026-07 と 2026-08 のデータしか持たないので、2026-06 のケース（full /
full-v1 / preview）では対象月のデータが無い組織として飛ばされ、出力は org-a / org-b のまま。
速報の複数スペースも org-c の 2026-08 を独立ケースで固定する。

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

from .conftest import REPO_ROOT

EXAMPLES_INPUT = REPO_ROOT / "examples" / "input"
# examples の合成データ用の上書き設定（org-c の workspace の設定）。既定設定に重ねて使う
EXAMPLES_CONFIG = str(REPO_ROOT / "examples" / "config.yaml")
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden"


class Case(NamedTuple):
    """golden 1ケース分の analyze 実行条件。辞書のキーが golden ツリーの名前になる。"""

    month: str
    args: tuple[str, ...] = ()


# 速報は --days をファイル名からの自動判別に任せず明示する
# （ファイル名の期間が変わると観測日数が変わり、golden が揺れるため）。
CASES = {
    # 切り替え前の full を名前替えした V1 の基準。再生成しない。
    "full-v1": Case("2026-06", ("--decision-version", "v1")),
    # 同じ入力の既定（V2）の主成果物・decision-evidence・組織横断サマリ。
    "full": Case("2026-06"),
    "preview": Case("2026-06", ("--preview", "--days", "10")),
    # 条件付きの断片を通すケース。同一月に複数スナップショットを持つのは
    # org-b の 2026-07 だけで、org-a にこの月のデータは無いので --org で絞る。
    # 版を省略して既定（V2）を固定する。V1 は旧 full-snapshots の名前替えで、再生成しない。
    "full-snapshots": Case("2026-07", ("--org", "org-b")),
    "full-snapshots-v1": Case("2026-07", ("--org", "org-b", "--decision-version", "v1")),
    # --days は 31（暦日数と同じ＝月末ペース換算 ×1.0）。この月の spend は期間の
    # 最も広い 07-01-to-07-31 が主データに採用されるので、観測日数もその期間に
    # 合わせる。短い値を書くと、月全体のデータを部分月と偽って割り増した数字が
    # golden に固定される。
    "preview-snapshots": Case("2026-07", ("--org", "org-b", "--preview", "--days", "31")),
    "preview-multi": Case("2026-08", ("--org", "org-c", "--preview", "--days", "31")),
    # 条件つき section がすべて出る月（examples/generate_sample_data.py の全部入りサンプル）。
    # 同じ入力の V1 は full-all-v1 で固定し、再生成せず複数 workspace の不変を検査する。
    # full-all は版を省略した既定（V2）の全部入り。2つの Team スペースを運用する org-c を同じ実行に並べ、
    # 複数 workspace の組織の6種と組織横断サマリも固定する。
    "full-all-v1": Case(
        "2026-08", ("--org", "org-b", "--org", "org-c", "--decision-version", "v1")),
    "full-all": Case(
        "2026-08", ("--org", "org-b", "--org", "org-c")),
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
        "analyze", "--config", EXAMPLES_CONFIG,
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
