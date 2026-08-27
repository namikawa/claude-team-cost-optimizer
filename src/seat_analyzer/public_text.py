"""公開テキストの検査（`seat-analyzer check-text`）。

PR 本文・コミットメッセージ・差分のように、リポジトリの外に出る文章へ業務情報が
含まれていないかを `leakcheck` の照合で検査する。レポートの混入チェックと同じ規則を、
同じ道具で公開面の文章にも適用するための入口。

「すでに公開されている内容」の基準は HEAD 時点の git 管理下のファイル。作業ツリーを
読むと、未追跡のドラフト・gitignore 済みのファイル・追跡ファイルの未コミット編集が
公開済み扱いになり、検査が黙って素通りする。
"""

from __future__ import annotations

import codecs
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .leakcheck import (
    LeakCheckError,
    LeakHit,
    files_under,
    find_leaks,
    forbidden_terms,
)

# baseline の読み取り元がこのリポジトリかを見分けるための名前（pyproject.toml の
# project.name と突き合わせる）
PROJECT_NAME = "seat-analyzer"

# git が使えないとき（テストの --repo-root 等）に走査する対象。
# 通常は git 管理下のファイル一覧を使う（下記 _tracked_files）。
# ワークスペースの config.yaml は入れない。利用者の手元だけにある上書き設定で、
# そこに書かれた語（誤検出を許可した部署名等）は公開済みではない
PUBLIC_BASELINE_PATHS = (
    "README.md", "pyproject.toml", "docs", "examples", "src", "tests", ".claude",
)
# 公開済みの内容はコードだけでなく資材にもある（templates/ の .css・.j2 等）。
# 拡張子の網羅が実体からずれると baseline が縮み、公開済みの語を新たな開示として扱う
_TEXT_SUFFIXES = (
    ".md", ".py", ".csv", ".yaml", ".yml", ".txt", ".html", ".toml", ".json",
    ".css", ".js", ".j2",
)


# 公開テキストの入力に想定する文字コード。utf-8-sig は BOM 無しの UTF-8 も読めるため、
# ingest.read_csv と同じ2種で足りる
_INPUT_ENCODINGS = ("utf-8-sig", "cp932")

# 扱わない文字コードの BOM。UTF-16 の BOM は UTF-32 の BOM の前方部分でもあるが、
# ここでは「該当したら拒否」しかしないので判定順は問わない
_UNSUPPORTED_BOMS = (
    codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE,
    codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE,
)


def _reject_unsupported_encoding(raw: bytes) -> None:
    """UTF-16 / UTF-32 と判別できる入力を拒否する。

    これらは utf-8 や cp932 として「読めてしまう」ことがあり、化けたテキストでは禁止語に
    一致せず検査が素通りする（cp932 は 0xFD-0xFF を私用領域へ写すため BOM も通り、
    ASCII 中心の UTF-16 は NUL 混じりの UTF-8 として読める）。テキストに NUL バイトが
    正当に現れることは無いので、BOM が無い場合の手がかりに使う。

    「BOM 無し・NUL 無し・日本語だけ」の UTF-16 は判別できず cp932 の誤解釈として通る。
    改行や空白が1文字でもあれば NUL が現れるため、実用上の入力は捕まえられる。
    """
    if raw.startswith(_UNSUPPORTED_BOMS):
        raise ValueError(
            "入力が UTF-16 / UTF-32 です（BOM を検出）。"
            f"{' / '.join(_INPUT_ENCODINGS)} のいずれかで渡してください"
        )
    if b"\x00" in raw:
        raise ValueError(
            "入力に NUL バイトが含まれます（UTF-16 等の非対応の文字コードの可能性）。"
            f"{' / '.join(_INPUT_ENCODINGS)} のいずれかで渡してください"
        )


def decode_candidates(raw: bytes) -> list[tuple[str, str]]:
    """バイト列の解釈候補を (文字コード名, 本文) で返す。読めた解釈をすべて返す。

    公開テキストが UTF-8 で届くとは限らない。Windows PowerShell はネイティブコマンドへの
    パイプをロケール既定（日本語環境では cp932）で流すため、cp932 のバイト列が来る。
    さらに cp932 のバイト列が UTF-8 としても妥当になることがあり（文字をまたぐ位置で
    3バイトの列が成立する。例: 「燿テ」= e0 a0 83 65 は UTF-8 では別の2文字に読める）、
    片方の解釈だけを見ると禁止語を取りこぼす。
    照合は取りこぼしより誤検出に倒す方針なので、読めた解釈はすべて検査対象にする。
    """
    _reject_unsupported_encoding(raw)
    out: list[tuple[str, str]] = []
    for encoding in _INPUT_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if all(text != seen for _, seen in out):
            out.append((encoding, text))
    if not out:
        raise ValueError(
            f"入力の文字コードを判別できません（{' / '.join(_INPUT_ENCODINGS)} を試行）"
        )
    return out


@dataclass(frozen=True)
class PublicCheckResult:
    """公開テキスト検査の結果。"""

    hits: tuple[LeakHit, ...]
    n_terms: int  # 照合した禁止語の数（0 なら検査が退化しているので失敗させる）


def validate_baseline_root(root: Path) -> None:
    """baseline の読み取り元が、このツールのリポジトリのルートであることを確かめる。

    root を取り違えると、そこにある別のリポジトリの内容が「すでに公開されている」と
    みなされ、その語は業務情報でも検出されなくなる。省略された root（カレント
    ディレクトリ）に対して確かめ、明示された root には適用しない。

    目印は pyproject.toml の project.name。git のリモート URL は付け替えられ、
    ディレクトリ名は複製で変わるため、リポジトリの中身そのものを見る。
    """
    try:
        with (root / "pyproject.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        # 読めない・TOML として壊れている・UTF-8 でない（後の2つは ValueError の一種）。
        # いずれもこのリポジトリだと確かめられないので同じ扱いにする
        data = {}
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if name != PROJECT_NAME:
        raise LeakCheckError(
            f"{root} は {PROJECT_NAME} のリポジトリのルートではありません。"
            "すでに公開されている内容を確定できないため中止します"
            "（--repo-root にこのリポジトリのルートを指定してください）"
        )


def _git_bytes(root: Path, args: list[str], stdin: bytes | None = None) -> bytes | None:
    """git コマンドの標準出力（バイト列）。失敗したら None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], input=stdin,
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _head_files(root: Path) -> list[tuple[str, str]] | None:
    """HEAD 時点の追跡ファイルの (パス, 内容)。git が使えない場合は None。

    パス一覧だけを git から取って内容を作業ツリーから読むと、追跡ファイルの未コミット
    編集が baseline に入る。それでは「テストに実データを書いた状態で公開文章を検査する」
    という、検査を無意味にする状態を素通りさせてしまう。
    内容も HEAD から読む。
    """
    names = _git_bytes(root, ["ls-tree", "-r", "-z", "--name-only", "HEAD"])
    if names is None:
        return None
    paths = [p for p in names.split(b"\0") if p]
    if not paths:
        return []
    out = _git_bytes(
        root, ["cat-file", "--batch"],
        stdin=b"".join(b"HEAD:" + p + b"\n" for p in paths),
    )
    if out is None:
        return None

    files: list[tuple[str, str]] = []
    pos, index = 0, 0
    while pos < len(out) and index < len(paths):
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].split(b" ")
        path = paths[index].decode("utf-8", errors="replace")
        index += 1
        # `<sha> missing` 等、blob 以外は本文が続かない（サブモジュールも該当）
        if len(header) < 3 or header[1] != b"blob":
            pos = end + 1
            continue
        size = int(header[2])
        files.append((path, out[end + 1:end + 1 + size].decode("utf-8", errors="replace")))
        pos = end + 1 + size + 1  # 本文の後の改行
    return files


def public_baseline(
    root: Path, paths: tuple[str, ...] = PUBLIC_BASELINE_PATHS,
    *, exclude: tuple[Path, ...] = (),
) -> str:
    """すでに公開されている内容を連結したテキスト。

    ここに現れる語は公開済みなので、公開テキストに書いても新たな開示にはあたらない
    （例: examples/ の合成データの人名が実在の姓と偶然一致していても、その文字列は
    既にリポジトリにある）。

    対象は **HEAD 時点の git 管理下のファイル**（パスも内容も git から読む）。作業ツリーを
    読むと、未追跡のドラフト・gitignore 済みのファイル・追跡ファイルの未コミット編集が
    「公開済み」になり、検査が黙って素通りする。
    exclude には検査対象のファイル自身を渡す（自分自身を根拠に素通りさせないため）。

    root が git 管理下（.git がある）なのに git を実行できない場合はエラーにする。
    ゲートが黙って弱くなるのを避けるため。.git が無い場合（--repo-root に非 git の
    ディレクトリを明示指定した場合）だけ、既知のディレクトリ走査へフォールバックする。
    """
    skip = {p.resolve() for p in exclude}
    head = _head_files(root)
    if head is not None:
        return "\n".join(
            content for name, content in head
            if (root / name).resolve() not in skip
        )
    if (root / ".git").exists():
        raise LeakCheckError(
            f"{root} は git 管理下ですが git を実行できませんでした。"
            "公開済みの内容を確定できないため中止します"
        )

    # 非 git ディレクトリ向けのフォールバック
    files: list[Path] = []
    for name in paths:
        target = root / name
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(files_under(target, _TEXT_SUFFIXES))
    chunks: list[str] = []
    for path in files:
        try:
            if path.resolve() in skip:
                continue
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


@dataclass(frozen=True)
class DiffExtract:
    """差分から取り出した「追加される内容」。"""

    text: str
    n_added_lines: int
    n_paths: int


# unified diff であることの目印。1つも無い入力を diff として扱うと、抽出結果が
# ほぼ空になり何も検査されないまま成功する
_DIFF_MARKER_RE = re.compile(
    r"^(?:diff --git |@@ |\+\+\+ |--- |rename to |copy to )", re.MULTILINE)


def diff_added_text(diff: str) -> DiffExtract:
    """unified diff から「追加される内容」だけを取り出す。

    削除行にはまさに今取り除こうとしている業務情報が現れるため、差分をそのまま検査すると
    必ず落ちる。「全部削除行だから問題ない」という目視判断は見落としやすいので、
    追加行と追加先のパスだけを対象にして機械的に判定できるようにする。

    差分でない入力を渡された場合はエラーにする。抽出結果が空になって「照合したが
    検出なし」と表示され、完全な青信号に見えてしまうため（フラグの取り違えは起きる）。
    """
    if diff.strip() and not _DIFF_MARKER_RE.search(diff):
        raise LeakCheckError(
            "--diff を指定しましたが、入力が unified diff ではありません"
            "（差分でない文章は --diff を外して検査してください）"
        )
    added: list[str] = []
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++"):
            path = line[3:].strip()
            if path and path != "/dev/null":
                paths.append(path)
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith(("rename to ", "copy to ")):
            # 内容変更を伴わない rename/copy は +++ 行を持たないため、ここで拾う
            paths.append(line.split(" to ", 1)[1].strip())
    return DiffExtract("\n".join(paths + added), len(added), len(paths))


def check_public_text(
    text: str, *, input_dir: Path, output_dir: Path, cfg: dict,
    root: Path = Path("."), allow: tuple[str, ...] = (),
    exclude: tuple[Path, ...] = (),
) -> PublicCheckResult:
    """公開予定のテキストに業務情報が含まれないか検査する。

    PR 本文・PR コメント・コミットメッセージなど、リポジトリの外に出る文章が対象。
    レポートの混入チェックと違い「対象組織」という概念がない（どの組織の情報も書けない）
    ため、全組織の語を禁止語として集める。組織名は常時禁止で、allow にも設定にも
    除外の経路を持たない。

    禁止語が1件も集まらない場合は LeakCheckError にする。--input-dir が解決できない
    等で検査が退化していると、何を渡しても「検出なし」になり、青信号にしか見えないため。

    レポートの混入チェックと同じ規則を、リポジトリの外に出る文章にも適用するための
    入口。規則の適用範囲をレポートに限定すると、公開面が検査から漏れるため、道具の
    側で範囲を揃えている。
    """
    if not input_dir.is_dir():
        raise LeakCheckError(
            f"入力ディレクトリがありません: {input_dir}"
            "（--input-dir を確認してください。検査が退化するため中止します）"
        )
    terms = forbidden_terms(
        input_dir=input_dir, output_dir=output_dir, target_org=None, cfg=cfg)
    if not terms:
        raise LeakCheckError(
            "禁止語を1件も収集できませんでした（--input-dir / --output-dir を確認して"
            "ください）。検査が成立しないため中止します"
        )
    hits = find_leaks(
        text, terms, source=public_baseline(root, exclude=exclude), allow=allow)
    return PublicCheckResult(hits, len(terms))
