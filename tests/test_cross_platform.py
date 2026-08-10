"""Windows で動かすための取り決めのテスト。

Windows でしか起きない状況（ロケール既定が cp932・パス区切りが "\\"・開いている
ファイルを置換できない）は、ストリーム・`os.sep`・例外を差し替えて macOS / Linux 上でも
検証できる形にしている。POSIX の権限モデルに依存して Windows では実施できない検証は、
`conftest.requires_posix_permissions` / `requires_symlink` で当該テストだけ飛ばす。
"""

import io
import sys

import pytest

from seat_analyzer import cli, data_quality
from seat_analyzer.cli import main
from seat_analyzer.data_quality import _reason

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


def test_force_utf8_output_survives_cp932_locale_default(monkeypatch):
    """ロケール既定が cp932 でもレポート由来の文字を出力できる。

    Windows はコンソール直結のときだけ UTF-8 で書くため、再設定しないと同じコマンドが
    リダイレクトやパイプ経由でだけ UnicodeEncodeError で落ちる。
    """
    buf, stream = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    cli._force_utf8_output()
    print(_UNENCODABLE)
    stream.flush()

    assert _UNENCODABLE in buf.getvalue().decode("utf-8")


def test_force_utf8_output_uses_strict_errors(monkeypatch):
    """置換ではなく strict にする。

    UTF-8 は通常の文字をすべて表現できるので置換の出番は壊れたデータのときだけで、
    doctor --format json は ensure_ascii=False の生の Unicode を出す。改変した内容を
    正常終了で返すより、明示的に失敗させる。
    """
    _, stream = _cp932_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    cli._force_utf8_output()

    assert stream.encoding == "utf-8"
    assert stream.errors == "strict"
    with pytest.raises(UnicodeEncodeError):
        stream.write("\udcff")  # UTF-8 でも表現できない壊れた文字は落とす
        stream.flush()


def test_force_utf8_output_tolerates_streams_without_reconfigure(monkeypatch):
    """reconfigure を持たないストリームに差し替えられていても落ちない。"""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli._force_utf8_output()  # 例外にならなければよい


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
