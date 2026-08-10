"""設定の層構造（パッケージ内の既定 + ワークスペースの差分上書き）のテスト。

プログラムは `uv tool install` で配り、利用者のデータは任意の場所のワークスペースに置く。
単価やカラムの対応表をプログラムの更新で配れるようにするため、既定はパッケージ内に持ち、
ワークスペースの config.yaml には差分だけを書く。ここではその重ね方と、誤記を黙って
無視しないことを見る。ワークスペースの雛形を作る `init` も対象にする。
"""

from pathlib import Path

import pytest
import yaml

from seat_analyzer.cli import WORKSPACE_CONFIG_TEMPLATE, main
from seat_analyzer.config import PACKAGE_CONFIG_PATH, load_config


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _override(tmp_path: Path, text: str) -> Path:
    """カレントに置く上書きファイル（--config 省略時に拾われる名前）。"""
    return _write(tmp_path / "config.yaml", text)


# ------------------------------------------------------- 既定と上書きの重ね方


def test_defaults_load_without_any_override(tmp_path, monkeypatch):
    """上書きファイルが無くても既定だけでロードできる。"""
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg["seats"]["standard"]["price_usd"] == 25.0
    assert cfg["seats"]["premium"]["price_usd"] == 125.0
    # 明示的に既定を指した場合と同じ結果になる（テストが既定を明示するのはこのため）
    assert cfg == load_config(PACKAGE_CONFIG_PATH)


def test_workspace_config_in_cwd_is_applied(tmp_path, monkeypatch):
    """--config 省略時はカレントの config.yaml を上書きとして使う。"""
    monkeypatch.chdir(tmp_path)
    _override(tmp_path, "decision:\n  hysteresis_months: 3\n")
    assert load_config()["decision"]["hysteresis_months"] == 3


def test_dict_override_merges_per_key(tmp_path):
    """辞書はキー単位で再帰マージする（書かなかった項目は既定が残る）。"""
    path = _override(tmp_path, "decision:\n  hysteresis_months: 3\n")
    cfg = load_config(path)
    default = load_config(PACKAGE_CONFIG_PATH)

    assert cfg["decision"]["hysteresis_months"] == 3
    assert cfg["decision"]["buffer_ratio"] == default["decision"]["buffer_ratio"]
    assert cfg["decision"]["censoring_margin"] == default["decision"]["censoring_margin"]
    # 触っていないセクションもそのまま
    assert cfg["columns"] == default["columns"]


def test_deep_dict_override_keeps_siblings(tmp_path):
    """深い階層の上書きでも、兄弟のキーは既定のまま残る。"""
    path = _override(tmp_path, "seats:\n  standard:\n    allowance_usd:\n      mid: 60.0\n")
    cfg = load_config(path)
    default = load_config(PACKAGE_CONFIG_PATH)

    assert cfg["seats"]["standard"]["allowance_usd"]["mid"] == 60.0
    assert cfg["seats"]["standard"]["allowance_usd"]["low"] == \
        default["seats"]["standard"]["allowance_usd"]["low"]
    assert cfg["seats"]["standard"]["price_usd"] == 25.0
    assert cfg["seats"]["premium"] == default["seats"]["premium"]


def test_list_override_replaces_whole_list(tmp_path):
    """リストは丸ごと置換する（マージして並びが混ざると単価表の評価順が壊れる）。"""
    path = _override(
        tmp_path,
        'model_prices:\n'
        '  patterns:\n'
        '    - { match: "sonnet", input: 3.0, output: 15.0 }\n',
    )
    cfg = load_config(path)
    assert cfg["model_prices"]["patterns"] == [
        {"match": "sonnet", "input": 3.0, "output": 15.0}]
    # 同じセクションの他のキーは既定が残る
    assert cfg["model_prices"]["default"] == \
        load_config(PACKAGE_CONFIG_PATH)["model_prices"]["default"]


def test_empty_list_override_is_accepted(tmp_path):
    """空リストは正当な置換（「1件も無い」を表せる必要がある）。"""
    path = _override(tmp_path, "discussion:\n  public_org_names: []\n")
    assert load_config(path)["discussion"]["public_org_names"] == []


def test_comment_only_override_is_a_noop(tmp_path):
    """全行コメントの上書きファイルは既定と同じ結果になる。"""
    path = _override(tmp_path, "# 何も上書きしない\n# decision:\n#   hysteresis_months: 3\n")
    assert load_config(path) == load_config(PACKAGE_CONFIG_PATH)


def test_full_config_as_override_is_accepted(tmp_path):
    """既定と同内容の完全版を上書きに指定しても通る（従来の使い方の後方互換）。"""
    path = _write(tmp_path / "full.yaml", PACKAGE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert load_config(path) == load_config(PACKAGE_CONFIG_PATH)


# ------------------------------------------------------- 誤記を黙って無視しない


@pytest.mark.parametrize("text,where", [
    ("decisions:\n  hysteresis_months: 3\n", "decisions"),
    ("decision:\n  hysteresis_month: 3\n", "decision.hysteresis_month"),
    ('columns:\n  spend:\n    emial: ["email"]\n', "columns.spend.emial"),
])
def test_unknown_key_is_rejected_with_its_path(tmp_path, text, where):
    """既定に無いキーはフルパス付きでエラーにする。

    綴り違いを黙って無視すると、上書きしたつもりの値が効かないまま分析が完走する。
    """
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=f"'{where}' は既定に存在しないキー"):
        load_config(path)


@pytest.mark.parametrize("text,where", [
    ("decision: 3\n", "decision"),                       # 既定は辞書
    ("cost_basis:\n  value: computed\n", "cost_basis"),   # 既定は値
])
def test_type_mismatch_is_rejected(tmp_path, text, where):
    """辞書と値・リストの取り違えはエラーにする。"""
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=f"'{where}' は"):
        load_config(path)


@pytest.mark.parametrize("text,where", [
    ("cost_basis:\n", "cost_basis"),
    ("decision:\n  hysteresis_months:\n", "decision.hysteresis_months"),
])
def test_null_value_is_rejected(tmp_path, text, where):
    """値を書き忘れた項目（YAML の空値）はエラーにする。

    None を既定へ被せると、以降の検証や計算が「設定した覚えのない値」で動く。
    """
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=f"'{where}' の値が空です"):
        load_config(path)


def test_non_mapping_override_is_rejected(tmp_path):
    """トップレベルが辞書でない上書きファイルはエラーにする。"""
    path = _override(tmp_path, "- decision\n- seats\n")
    with pytest.raises(ValueError, match="設定のマッピングではありません"):
        load_config(path)


def test_explicit_missing_config_errors(tmp_path):
    """--config で明示したファイルが無ければ従来どおりエラー（黙って既定で走らせない）。"""
    with pytest.raises(FileNotFoundError, match="設定ファイルが見つかりません"):
        load_config(tmp_path / "nonexistent.yaml")


def test_cli_reads_the_workspace_config(tmp_path, monkeypatch, capsys):
    """CLI も --config 省略時にカレントの config.yaml を読む（誤記はそこで落ちる）。"""
    monkeypatch.chdir(tmp_path)
    _override(tmp_path, "decision:\n  hysteresis_month: 3\n")
    (tmp_path / "input").mkdir()
    capsys.readouterr()

    assert main(["doctor", "--input-dir", "input"]) == 1
    assert "既定に存在しないキー" in capsys.readouterr().err


# ------------------------------------------------------- ワークスペースの雛形（init）


def test_init_creates_workspace(tmp_path, monkeypatch, capsys):
    """init は入力ディレクトリと、差分だけを書く設定ファイルの雛形を作る。"""
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()

    assert main(["init"]) == 0
    assert (tmp_path / "input").is_dir()
    config_path = tmp_path / "config.yaml"
    assert config_path.is_file()
    # 雛形は全行コメント（＝上書きなし）なので、そのままでも既定と同じ結果になる
    assert load_config() == load_config(PACKAGE_CONFIG_PATH)
    assert b"\r" not in config_path.read_bytes()
    out = capsys.readouterr().out
    assert "init-org" in out and "analyze" in out


def test_init_custom_input_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", "data"]) == 0
    assert (tmp_path / "data").is_dir()


def test_init_is_repeatable_and_keeps_existing_config(tmp_path, monkeypatch, capsys):
    """既存の設定ファイルは上書きしない（記入済みの組織固有設定を消さない）。"""
    monkeypatch.chdir(tmp_path)
    written = _override(tmp_path, "decision:\n  hysteresis_months: 3\n")
    capsys.readouterr()

    assert main(["init"]) == 0
    assert written.read_text(encoding="utf-8") == "decision:\n  hysteresis_months: 3\n"
    assert "既存のため変更しません" in capsys.readouterr().out


def test_workspace_template_has_no_effective_settings():
    """雛形は全行コメント（値を書いた行が無い）。

    値が入っていると、init しただけで既定を上書きした状態になり、プログラムの更新で
    配った単価がその項目だけ効かなくなる。
    """
    assert yaml.safe_load(WORKSPACE_CONFIG_TEMPLATE.read_text(encoding="utf-8")) is None


# ------------------------------------------------------- バージョン表示


def test_version_flag_prints_version(capsys):
    """--version はバージョン文字列を出して終了コード 0 で終わる。"""
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "seat-analyzer" in capsys.readouterr().out
