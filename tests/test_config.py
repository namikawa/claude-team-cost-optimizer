"""設定の層構造（パッケージ内の既定 + ワークスペースの差分上書き）のテスト。

プログラムは `uv tool install` で配り、利用者のデータは任意の場所のワークスペースに置く。
単価やカラムの対応表をプログラムの更新で配れるようにするため、既定はパッケージ内に持ち、
ワークスペースの config.yaml には差分だけを書く。ここではその重ね方と、誤記を黙って
無視しないことを見る。ワークスペースの雛形を作る `init` も対象にする。
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from seat_analyzer.cli import WORKSPACE_CONFIG_TEMPLATE, main
from seat_analyzer.config import PACKAGE_CONFIG_PATH, load_config

from .conftest import requires_symlink


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


@pytest.mark.parametrize("text,where,want", [
    ("decision: 3\n", "decision", "辞書"),                        # 既定は辞書
    ("decision:\n  - hysteresis_months\n", "decision", "辞書"),
    ("cost_basis:\n  value: computed\n", "cost_basis", "値"),      # 既定は値
    ("trend:\n  top_changes: []\n", "trend.top_changes", "値"),
    ("model_prices:\n  patterns: 3\n", "model_prices.patterns", "リスト"),
])
def test_value_kind_mismatch_is_rejected(tmp_path, text, where, want):
    """辞書・リスト・値の3種別で既定と食い違う上書きはエラーにする。

    種別を「辞書か否か」だけで見ると、数値の位置に書いたリストが後段の計算まで届き、
    設定の誤りが計算の型エラーとして表に出る。
    """
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=f"'{where}' は{want}で指定してください"):
        load_config(path)


@pytest.mark.parametrize("text,key", [
    # 同じセクションを2回書くと、先に書いた側が丸ごと消える
    ("decision:\n  hysteresis_months: 3\ndecision:\n  buffer_ratio: 0.5\n", "decision"),
    ("decision:\n  hysteresis_months: 3\n  hysteresis_months: 4\n", "hysteresis_months"),
])
def test_duplicate_keys_are_rejected(tmp_path, text, key):
    """重複キーは後勝ちで飲み込まず、キー名を挙げてエラーにする。"""
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=f"キー '{key}' が重複しています"):
        load_config(path)


# アンカー（&std）とマージキー（<<）で allowance を共有する上書き。
# 明示キー high は展開結果より優先される（YAML の仕様）
_MERGE_YAML = """
seats:
  standard:
    allowance_usd: &std
      low: 30.0
      mid: 50.0
      high: 75.0
  premium:
    allowance_usd:
      {order}
"""
_MERGE_LAST = "<<: *std\n      high: 400.0"
_MERGE_FIRST = "high: 400.0\n      <<: *std"


@pytest.mark.parametrize("order", [_MERGE_LAST, _MERGE_FIRST])
def test_merge_keys_expand_as_yaml_specifies(tmp_path, order):
    """アンカーとマージキーを使った上書きが、素の読み込みと同じ結果になる。

    重複キーの検査はマージキーを対象にしない（マージで来たキーを明示キーで上書き
    するのは YAML の仕様で、書き手の意図どおり）。書く順序でも結果は変わらない。
    """
    text = _MERGE_YAML.format(order=order)
    cfg = load_config(_override(tmp_path, text))
    expanded = {"low": 30.0, "mid": 50.0, "high": 400.0}

    assert cfg["seats"]["premium"]["allowance_usd"] == expanded
    assert yaml.safe_load(text)["seats"]["premium"]["allowance_usd"] == expanded
    # 展開後のキーは既定にあるものだけなので未知キー検査も通り、兄弟キーは既定が残る
    assert cfg["seats"]["premium"]["price_usd"] == 125.0
    assert cfg["seats"]["standard"]["allowance_usd"] == {
        "low": 30.0, "mid": 50.0, "high": 75.0}


def test_duplicate_keys_are_still_detected_with_merge_keys(tmp_path):
    """マージキーがあっても、明示キーどうしの重複は検出する。"""
    text = _MERGE_YAML.format(order="<<: *std\n      high: 400.0\n      high: 500.0")
    with pytest.raises(ValueError, match="キー 'high' が重複しています"):
        load_config(_override(tmp_path, text))


@pytest.mark.parametrize("text,fragment", [
    # 真偽値は int の一種なので、素の isinstance では回数として通ってしまう
    ("decision:\n  hysteresis_months: yes\n", "hysteresis_months"),
    ("seats:\n  standard:\n    price_usd: .nan\n", "price_usd"),
    ("seats:\n  premium:\n    allowance_usd:\n      high: .inf\n", "allowance_usd.high"),
    ("decision:\n  buffer_ratio: .nan\n", "buffer_ratio"),
    ("decision:\n  censoring_margin: .inf\n", "censoring_margin"),
    # 綴り違いの算出基準は auto と同じ扱いで黙って通る
    ("cost_basis: computd\n", "cost_basis"),
    ('model_prices:\n  patterns:\n    - { match: 123, input: 1.0, output: 2.0 }\n',
     "patterns[0]"),
    ("model_prices:\n  default:\n    input: .inf\n", "model_prices.default"),
])
def test_values_that_break_later_stages_are_rejected(tmp_path, text, fragment):
    """種別は合っていても、後段の計算・照合が壊れる値は設定の時点で落とす。

    NaN・Infinity は比較が常に偽になるため、判定を黙って変える。真偽値の回数、
    文字列でない照合パターンも同様に、値としては読めてしまう。
    """
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=re.escape(fragment)):
        load_config(path)


@pytest.mark.parametrize("value", ["computed", "net_spend", "auto", "Computed"])
def test_cost_basis_accepts_its_documented_values(tmp_path, value):
    """列挙の検証は算出基準の正しい値を弾かない（照合は小文字化して行う）。"""
    path = _override(tmp_path, f"cost_basis: {value}\n")
    assert load_config(path)["cost_basis"] == value


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


def test_config_that_is_not_a_regular_file_is_rejected(tmp_path, monkeypatch):
    """カレントの config.yaml がディレクトリなら、既定のみで続行せずエラーにする。

    「無ければ既定のみ」は存在しないときの話で、読めない何かがそこにある状態で
    黙って既定で走ると、上書きしたつもりの設定が効かないまま結果が出る。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").mkdir()
    with pytest.raises(ValueError, match="通常のファイルではありません"):
        load_config()


@requires_symlink
def test_broken_symlink_config_is_rejected(tmp_path, monkeypatch):
    """リンク切れの config.yaml も「存在しない」とは扱わない。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").symlink_to(tmp_path / "missing.yaml")
    with pytest.raises(ValueError, match="通常のファイルではありません"):
        load_config()


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


def test_init_rejects_a_config_that_is_not_a_regular_file(tmp_path, monkeypatch, capsys):
    """ディレクトリを「既存の設定」として扱わない（雛形が作られたと誤解させない）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").mkdir()
    capsys.readouterr()

    assert main(["init"]) == 1
    assert "通常のファイルではありません" in capsys.readouterr().err


def test_init_creates_gitignore(tmp_path, monkeypatch, capsys):
    """ワークスペースが git 管理下でも、実データと組織固有の設定が入らないようにする。"""
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()

    assert main(["init"]) == 0
    raw = (tmp_path / ".gitignore").read_bytes()
    assert b"\r" not in raw
    lines = raw.decode("utf-8").splitlines()
    assert lines[0].startswith("#")
    assert {"/config.yaml", "/input/", "/reports/"} <= set(lines)
    assert ".gitignore" in capsys.readouterr().out


def test_init_gitignore_follows_the_input_dir_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", "data"]) == 0
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/data/" in lines
    assert "/input/" not in lines


def test_init_gitignore_relativizes_an_absolute_input_dir(tmp_path, monkeypatch):
    """ワークスペース配下を絶対パスで指しても、相対の行になる。

    .gitignore のパターンはそれが置かれたディレクトリからの相対でしか書けないため、
    絶対パスをそのまま書くと何にも一致しない行になる。
    """
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", str(tmp_path / "data")]) == 0
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/data/" in lines
    assert not any(line.startswith("//") for line in lines)


def test_init_reports_an_input_dir_outside_the_workspace(tmp_path, monkeypatch, capsys):
    """ワークスペースの外を指した入力ディレクトリは行にできないので、そう伝える。

    書けない行を書いたことにすると、保護されていないのに保護したように見える。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    capsys.readouterr()

    assert main(["init", "--input-dir", str(tmp_path / "outside")]) == 0
    lines = (workspace / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"/config.yaml", "/reports/"} <= set(lines)
    assert not any("outside" in line for line in lines)
    assert ".gitignore の対象外" in capsys.readouterr().out


def test_init_gitignore_escapes_pattern_characters(tmp_path, monkeypatch):
    """名前に含まれる `*` や `[` はパターンとして解釈されないようにする。"""
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", "in[1]put"]) == 0
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/in\\[1\\]put/" in lines


def test_gitignore_entries_are_effective_in_git(tmp_path, monkeypatch):
    """書いた行が git に意図どおり解釈される（除外の実効を git 自身に確かめる）。

    エスケープの規則は git の側にあるので、生成した文字列を見るだけでは
    「保護したつもり」で終わりうる。
    """
    if shutil.which("git") is None:
        pytest.skip("git を実行できない環境")
    # 利用者の設定（core.excludesFile 等）を持ち込まない
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "no-config"),
           "GIT_CONFIG_SYSTEM": str(tmp_path / "no-config")}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True,
                   capture_output=True)
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", "in[1]put"]) == 0
    for rel in ("in[1]put/spend.csv", "i1put/other.csv", "config.yaml"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8", newline="\n")

    def ignored(rel: str) -> bool:
        return subprocess.run(
            ["git", "check-ignore", "-q", rel], cwd=tmp_path, env=env,
            capture_output=True,
        ).returncode == 0

    assert ignored("in[1]put/spend.csv")     # 名前どおりに一致する
    assert ignored("config.yaml")
    assert not ignored("i1put/other.csv")    # 文字クラスとして解釈されていない


def test_init_rejects_a_gitignore_that_is_not_a_regular_file(tmp_path, monkeypatch, capsys):
    """ディレクトリの .gitignore は書ける形ではない。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").mkdir()
    capsys.readouterr()

    assert main(["init"]) == 1
    assert "通常のファイルではありません" in capsys.readouterr().err


@requires_symlink
def test_init_rejects_a_symlinked_gitignore(tmp_path, monkeypatch, capsys):
    """symlink の .gitignore は、リンク先が通常ファイルでも拒否する。

    git は symlink の .gitignore を除外設定として読まないため、追記できても保護に
    ならない。成功したように見せない。
    """
    monkeypatch.chdir(tmp_path)
    target = _write(tmp_path / "ignore-rules", "/input/\n")
    (tmp_path / ".gitignore").symlink_to(target)
    capsys.readouterr()

    assert main(["init"]) == 1
    assert "通常のファイルではありません" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "/input/\n"  # リンク先も書き換えない


def test_init_appends_only_the_missing_gitignore_entries(tmp_path, monkeypatch):
    """既存の .gitignore は残し、足りない行だけを足す（行の照合は前後空白を無視する）。"""
    monkeypatch.chdir(tmp_path)
    head = "# 利用者が書いた行\n.venv/\n  /input/  \n"
    path = _write(tmp_path / ".gitignore", head)

    assert main(["init"]) == 0
    text = path.read_text(encoding="utf-8")
    assert text.startswith(head)
    lines = [line.strip() for line in text.splitlines()]
    assert lines.count("/input/") == 1
    assert "/config.yaml" in lines and "/reports/" in lines


def test_init_leaves_a_complete_gitignore_untouched(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = _write(tmp_path / ".gitignore", "/config.yaml\n/input/\n/reports/\n")
    before = path.read_bytes()
    capsys.readouterr()

    assert main(["init"]) == 0
    assert path.read_bytes() == before
    assert "変更なし" in capsys.readouterr().out


def test_init_appends_lf_lines_to_a_crlf_gitignore(tmp_path, monkeypatch):
    """既存の改行の形は変えず、足す行は LF で書く。"""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".gitignore"
    path.write_bytes(b"/input/\r\n")

    assert main(["init"]) == 0
    raw = path.read_bytes()
    assert raw.startswith(b"/input/\r\n")
    assert raw.count(b"\r\n") == 1


def test_init_appends_to_a_gitignore_without_a_trailing_newline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".gitignore"
    path.write_bytes(b"/input/")

    assert main(["init"]) == 0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "/input/"          # 行が連結されない
    assert "/config.yaml" in lines


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
