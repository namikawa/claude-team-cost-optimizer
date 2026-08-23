"""report.md / preview.md の考察セクションの読み書きと、原子的なファイル置換。"""

from __future__ import annotations

import os
import re
import tempfile
from enum import Enum
from pathlib import Path

# 考察セクションの開始位置。本文の分割・差し替えはすべてこの文字列を境界に行う。
_DISCUSSION_MARKER = "\n## 考察\n"

# 未記入プレースホルダ行の判定。考察本文に「未記入」という語（例: 「部署未記入」）が
# 含まれても誤判定しないよう、行アンカーで「（未記入 — ...）」形式の行のみを対象にする。
_DISCUSSION_PLACEHOLDER_RE = re.compile(r"^（未記入 — .*）$")

# 置換用の一時ファイルの接頭辞（`_atomic_write`）。最終ファイル名に依らない固定長にする。
# 先頭のドットは一時物であることの目印で、`.tmp` の拡張子と合わせて掃除の目印にもなる。
_TMP_PREFIX = ".seat-tmp-"


class WriteResult(Enum):
    """考察の書き込みの結末。

    「書かなかった」を1つの偽値で表すと、設計どおりの no-op（記入済みなので触らない）と、
    仕事が終わっていない状態（置換の直前に内容が変わっていて書けなかった）が区別できない。
    後者は利用者の再実行が要るので、呼び出し側が別々に扱えるようにする。
    """

    WRITTEN = "written"    # 書き込んだ
    KEPT = "kept"          # 記入済みの考察を見つけたので触らなかった
    CONFLICT = "conflict"  # 置換の直前に内容が変わっていたので書かなかった


def _is_placeholder_discussion(tail: str) -> bool:
    """考察 tail が未記入プレースホルダか。プレースホルダ行が1行でもあれば True。"""
    return any(_DISCUSSION_PLACEHOLDER_RE.match(line.strip()) for line in tail.splitlines())


def _preserve_discussion(md: str, path: Path, *, fallback: Path | None = None) -> str:
    """再生成時、既存レポートの記入済み「## 考察」セクションを引き継ぐ。

    fallback は種別だけの旧名（report.md / preview.md）。新名のファイルがまだ無い月
    では旧名から引き継ぐ。手書きの考察は他のどこにも無いため、名前が変わった最初の
    再生成で読み落とすと失われる。
    """
    source = path if path.exists() else fallback
    if source is None or not source.exists():
        return md
    existing = source.read_text(encoding="utf-8")
    if _DISCUSSION_MARKER not in existing:
        return md
    tail = existing.split(_DISCUSSION_MARKER, 1)[1]
    if _is_placeholder_discussion(tail):
        return md
    return md.split(_DISCUSSION_MARKER, 1)[0] + _DISCUSSION_MARKER + tail


def document_body(md: str) -> str:
    """考察セクションを除いたレポート本文。考察執筆へ渡す資料はこの範囲に限る。"""
    return md.split(_DISCUSSION_MARKER, 1)[0] if _DISCUSSION_MARKER in md else md


def discussion_body(md: str) -> str | None:
    """記入済みの考察本文。セクションが無い / 未記入プレースホルダのままなら None。"""
    if _DISCUSSION_MARKER not in md:
        return None
    tail = md.split(_DISCUSSION_MARKER, 1)[1]
    if _is_placeholder_discussion(tail):
        return None
    return tail.strip() or None


def write_discussion(
    path: Path, body: str, *, only_if_unwritten: bool = False,
) -> WriteResult:
    """考察セクションの中身を body に差し替えて書き戻す。本文側は一切変更しない。

    only_if_unwritten=True なら、記入済みの考察を見つけた時点で何もせず KEPT を返す。
    判定と書き込みを1回の読み取りに畳み、さらに置換の直前に内容が変わっていないかを
    確認する（生成に時間がかかる間に人が考察を書いた場合や、並行する analyze が本文を
    更新した場合に、それを巻き戻さないための保護）。この競合は CONFLICT で返す。
    """
    md = path.read_text(encoding="utf-8")
    if _DISCUSSION_MARKER not in md:
        raise ValueError(f"{path} に「## 考察」セクションがありません")
    if only_if_unwritten and discussion_body(md) is not None:
        return WriteResult.KEPT
    head = md.split(_DISCUSSION_MARKER, 1)[0]
    written = _atomic_write(
        path, head + _DISCUSSION_MARKER + "\n" + body.strip() + "\n",
        expect=md if only_if_unwritten else None,
    )
    return WriteResult.WRITTEN if written else WriteResult.CONFLICT


def _default_file_mode() -> int:
    """umask を反映した新規ファイルの権限。write_text（open の既定）と同じ意味にする。

    os.umask には読み取り専用の API が無いため 0 を設定して即戻す。単一スレッドの
    CLI 前提の手法（この2行の間に別スレッドがファイルを作ると 0666 になる）。
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _atomic_write(path: Path, text: str, *, expect: str | None = None) -> bool:
    """同一ディレクトリの一時ファイル経由で置換する。

    write_text は書き込み前にファイルを切り詰めるため、ディスク不足や中断で
    手書きの考察だけでなくレポート本文まで失われる。置換なら失敗しても元の内容が残る。
    一時ファイルは mkstemp 由来で 0600 になるため、既存ファイルはその権限を引き継がせ、
    新規作成時は umask 既定を使う（どちらも行わないとレポートだけ dashboard.html 等より
    狭い権限になる）。

    expect を渡すと、置換の直前に現在の内容と一致するかを確認し、変わっていれば
    置換せず False を返す。判定から置換までの窓を詰めるための照合で、厳密な排他ではない
    （照合と os.replace の間に書き込まれた場合は検出できない）。単一の実行者が使う前提で、
    ロックは導入していない。

    一時ファイルの名前は最終ファイル名から独立させる（`_TMP_PREFIX` + ランダム）。
    最終名を接頭辞にすると一時名だけが名前長の上限を超えることがあり、「最終名は収まるのに
    書き込みだけ失敗する」という見えない余白ができる。クラッシュで残った場合の出所は、
    置換先と同じディレクトリに `.seat-tmp-*.tmp` として残ることで分かる。
    """
    mode = (path.stat().st_mode & 0o7777) if path.exists() else _default_file_mode()
    tmp: Path | None = None
    try:
        # 名前を close 後に os.replace で使うため with で開かない（直後に with f: で閉じる）
        f = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=_TMP_PREFIX, suffix=".tmp", delete=False,
        )
        tmp = Path(f.name)
        with f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if expect is not None and path.read_text(encoding="utf-8") != expect:
            return False
        os.replace(tmp, path)
        tmp = None
        return True
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
