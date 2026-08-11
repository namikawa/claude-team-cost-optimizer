"""Windows で動かすための取り決めのテスト。

Windows でしか起きない状況（ロケール既定が cp932・パス区切りが "\\"・開いている
ファイルを置換できない）は、ストリーム・`os.sep`・例外を差し替えて macOS / Linux 上でも
検証できる形にしている。POSIX の権限モデルに依存して Windows では実施できない検証は、
`conftest.requires_posix_permissions` / `requires_symlink` で当該テストだけ飛ばす。
"""

import io
import subprocess
import sys
from pathlib import Path

import pytest

from seat_analyzer import cli, data_quality, discussion
from seat_analyzer.cli import main
from seat_analyzer.data_quality import _reason

from .conftest import requires_symlink

# レポート本文・考察プロンプト・凡例に実際に現れる文字のうち cp932 に無いもの
_UNENCODABLE = "見出し — ⚠️ ≤ ≥"


def _cp932_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """日本語 Windows のロケール既定を模した出力ストリーム。"""
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="cp932", errors="strict")


def test_cp932_stream_cannot_write_report_characters():
    """前提の確認: cp932 のままではレポート由来の文字を書けない。

    この前提が崩れる（cp932 で書けてしまう）と、下の再設定テストが何も保証しなくなる。
    """
    _, stream = _cp932_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(_UNENCODABLE)
        stream.flush()


def test_force_utf8_io_survives_cp932_locale_default(monkeypatch):
    """ロケール既定が cp932 でもレポート由来の文字を出力できる。

    Windows はコンソール直結のときだけ UTF-8 で書くため、再設定しないと同じコマンドが
    リダイレクトやパイプ経由でだけ UnicodeEncodeError で落ちる。
    """
    buf, stream = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    cli._force_utf8_io()
    print(_UNENCODABLE)
    stream.flush()

    assert _UNENCODABLE in buf.getvalue().decode("utf-8")


def test_force_utf8_io_uses_strict_errors(monkeypatch):
    """置換ではなく strict にする。

    UTF-8 は通常の文字をすべて表現できるので置換の出番は壊れたデータのときだけで、
    doctor --format json は ensure_ascii=False の生の Unicode を出す。改変した内容を
    正常終了で返すより、明示的に失敗させる。
    """
    _, stream = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    cli._force_utf8_io()

    assert stream.encoding == "utf-8"
    assert stream.errors == "strict"
    with pytest.raises(UnicodeEncodeError):
        stream.write("\udcff")  # UTF-8 でも表現できない壊れた文字は落とす
        stream.flush()


def test_cp932_stdin_silently_mangles_utf8_input():
    """前提の確認: UTF-8 の日本語は cp932 として例外なく別の語へ化ける。

    これが混入チェックが fail-open する仕組み。エラーになるなら止まるので害は無いが、
    実際には多くの語が黙って通り、禁止語と一致しないまま「検出なし」になる。
    """
    mangled = [w for w in ("開発", "本部", "企画", "経理")
               if _decodes_as_cp932_without_error(w)]
    assert mangled, "cp932 で黙って化ける語が無いなら stdin の再設定は不要"
    for word in mangled:
        assert word not in word.encode("utf-8").decode("cp932")


def _decodes_as_cp932_without_error(word: str) -> bool:
    try:
        word.encode("utf-8").decode("cp932")
    except UnicodeDecodeError:
        return False
    return True


def test_force_utf8_io_reconfigures_stdin(monkeypatch):
    """標準入力も UTF-8 で読む。

    check-text は git diff や公開予定の文章をパイプで受け取る。Windows のパイプは
    ロケール既定で読むため、再設定しないと禁止語を含む入力を「検出なし」で通す。
    """
    stream = io.TextIOWrapper(io.BytesIO("開発本部の件".encode()), encoding="cp932")
    monkeypatch.setattr(sys, "stdin", stream)

    cli._force_utf8_io()

    assert stream.read() == "開発本部の件"


def test_force_utf8_io_tolerates_streams_without_reconfigure(monkeypatch):
    """reconfigure を持たないストリームに差し替えられていても落ちない。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli._force_utf8_io()  # 例外にならなければよい


def test_org_name_collision_is_detected_before_org_selection(tmp_path):
    """--org で片方だけ選んでも、同じ出力先になる組織があれば止める。

    大文字小文字を区別しないファイルシステムでは両方のディレクトリを同時に作れない
    ため、発見済みの組織一覧を直接渡して判定させる。
    """
    with pytest.raises(ValueError, match="大文字小文字"):
        cli._resolve_targets(
            tmp_path / "input", tmp_path / "reports", ["Acme"], orgs=["Acme", "acme"],
        )


def test_permission_error_adds_hint_about_open_files(monkeypatch, capsys, tmp_path):
    """置換できないときは原因の見当が付く案内を足す。

    Windows は他プロセスが開いているファイルを書き換え・置換できない。CSV を Excel で
    開いたまま再分析したときの WinError 32 が最も多い経路で、素の例外文からは
    「閉じれば直る」ことが読み取れない。
    """
    def boom(args):
        raise PermissionError(32, "プロセスはファイルにアクセスできません")

    monkeypatch.setattr(cli, "_run_init_org", boom)
    assert main(["init-org", "org-x", "--input-dir", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "ヒント" in err and "Excel" in err


def test_real_subprocess_delivers_bytes_for_our_own_decoding():
    """実際の子プロセスを起動して「バイト列で受け取り自分でデコードする」契約を確かめる。

    text=True に encoding を渡す形は Windows で壊れる。デコードがリーダースレッドで走り、
    UnicodeDecodeError がスレッドの中で死んで stdout が None になるため、run_claude が
    それを「出力が空」と誤認して生成をやり直していた。バイト列で受ける限りこの差は無い。
    スタブではなく実プロセスで見るのは、この前提が壊れたときに気づくため（CI は Windows でも
    回るので、そこで実際の Windows 実装に対して検査される）。
    """
    code = r"import sys; sys.stdout.buffer.write('あ\r\n'.encode() + b'\xff\xfe')"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True)

    assert isinstance(proc.stdout, bytes)
    # 壊れた部分も欠けずに届く（スレッド内でデコードされると None になる）
    assert proc.stdout.endswith(b"\xff\xfe")
    # デコードの失敗は自分のコードで起きるので、例外として扱える
    with pytest.raises(UnicodeDecodeError):
        discussion._decode_output(proc.stdout)
    # 壊れていない範囲では CRLF が LF に揃う（universal newlines の代替）
    assert discussion._decode_output("あ\r\nい\rう".encode()) == "あ\nい\nう"


@requires_symlink
def test_reason_is_deterministic_for_same_length_base_candidates(tmp_path, monkeypatch):
    """入力ディレクトリの候補が同じ長さでも message が一定になる。

    候補は set から作るため反復順がハッシュシードに依存し、置換は逐次実行される。
    長さだけで並べると同長の候補の順序が決まらず、doctor の message が実行ごとに
    変わりうる（message の決定性は QualityIssue の不変条件）。
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "link").symlink_to("data", target_is_directory=True)  # 名前が同じ長さ
    monkeypatch.chdir(tmp_path)
    base = Path("link")
    assert len(str(base.absolute())) == len(str(base.resolve()))

    reason = _reason(ValueError(f"{base.absolute()}/spend: 読めません"), base)

    assert reason == "spend: 読めません"


def test_reason_relativizes_windows_style_paths(tmp_path, monkeypatch):
    """区切りが "\\" でも入力ディレクトリ配下を相対表記へ落とす。

    doctor の message は実行環境に依存してはならない（domain.QualityIssue の決定性）。
    区切りを "/" 決め打ちにすると Windows で相対化が効かない。
    """
    monkeypatch.setattr(data_quality.os, "sep", "\\")
    base = tmp_path / "input"

    reason = _reason(ValueError(f"{base}\\spend\\spend_2026-06.csv: 必須カラムなし"), base)

    assert reason == "spend\\spend_2026-06.csv: 必須カラムなし"
    assert str(base) not in reason
