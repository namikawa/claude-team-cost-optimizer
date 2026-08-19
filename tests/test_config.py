"""設定の層構造（パッケージ内の既定 + ワークスペースの差分上書き）のテスト。

プログラムは `uv tool install` で配り、利用者のデータは任意の場所のワークスペースに置く。
単価やカラムの対応表をプログラムの更新で配れるようにするため、既定はパッケージ内に持ち、
ワークスペースの config.yaml には差分だけを書く。ここではその重ね方と、誤記を黙って
無視しないことを見る。入出力ディレクトリの解決（フラグ > 設定 > 組み込み既定）と、
ワークスペースの雛形を作る `init` / `init-org` も対象にする。
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

from .conftest import SPEND_HEADER, requires_symlink, spend_row


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _override(tmp_path: Path, text: str) -> Path:
    """カレントに置く上書きファイル（--config 省略時に拾われる名前）。"""
    return _write(tmp_path / "config.yaml", text)


def _put(path: Path, text: str) -> Path:
    """親ディレクトリごと作ってファイルを書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return _write(path, text)


def _touch(path: Path) -> Path:
    return _put(path, "x\n")


# 入出力先の解決を CLI で確かめるための対象月（入力の中身は判定を見ないので最小でよい）
MONTH = "2026-06"


def _org_input(base: Path, org: str = "org-a", email: str = "alice.morgan@x.jp") -> Path:
    """分析が最後まで通る最小の入力を base/<組織名>/ に置く。戻り値は base。"""
    _put(base / org / "spend" / f"spend_{MONTH}.csv",
         SPEND_HEADER + "\n" + spend_row(email, 12.0) + "\n")
    _put(base / org / "members" / f"members_{MONTH}.csv",
         f"Email,Seat Type\n{email},Premium\n")
    return base


# .gitignore の行が実際に効くかは git に聞くしかない（除外の規則は git 側にある）
requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git を実行できない環境")


def _git_repo(root: Path) -> dict:
    """root を git リポジトリにして、以降の git 呼び出しに渡す環境変数を返す。

    利用者の設定（core.excludesFile 等）は持ち込まない（除外の判定が環境で変わる）。
    """
    missing = str(root / "no-git-config")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": missing, "GIT_CONFIG_SYSTEM": missing}
    subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=True, capture_output=True)
    return env


def _git_ignores(root: Path, env: dict, rel: str) -> bool:
    """git がそのパスを除外するか。"""
    return subprocess.run(
        ["git", "check-ignore", "-q", rel], cwd=root, env=env, capture_output=True,
    ).returncode == 0


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
    path = _override(tmp_path, 'discussion:\n  allow_terms: ["zephyr"]\n')
    assert load_config(path)["discussion"]["allow_terms"] == ["zephyr"]
    path = _override(tmp_path, "discussion:\n  allow_terms: []\n")
    assert load_config(path)["discussion"]["allow_terms"] == []


def test_comment_only_override_is_a_noop(tmp_path):
    """全行コメントの上書きファイルは既定と同じ結果になる。"""
    path = _override(tmp_path, "# 何も上書きしない\n# decision:\n#   hysteresis_months: 3\n")
    assert load_config(path) == load_config(PACKAGE_CONFIG_PATH)


def test_full_config_as_override_is_accepted(tmp_path):
    """既定と同内容の完全版を上書きに指定しても通る（従来の使い方の後方互換）。

    入出力先だけは、上書きファイルに書かれた値としてその置き場所を基準に解決される
    （相対パスの基準は下の「入出力ディレクトリ」の節を参照）。
    """
    path = _write(tmp_path / "full.yaml", PACKAGE_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg, default = load_config(path), load_config(PACKAGE_CONFIG_PATH)

    assert _without_paths(cfg) == _without_paths(default)
    assert cfg["paths"] == {
        "input": str(tmp_path / "input"), "output": str(tmp_path / "reports")}


def _without_paths(cfg: dict) -> dict:
    return {key: value for key, value in cfg.items() if key != "paths"}


# ------------------------------------------------------- 誤記を黙って無視しない


@pytest.mark.parametrize("text,where", [
    ("decisions:\n  hysteresis_months: 3\n", "decisions"),
    ("decision:\n  hysteresis_month: 3\n", "decision.hysteresis_month"),
    ('columns:\n  spend:\n    emial: ["email"]\n', "columns.spend.emial"),
    ("paths:\n  inputs: data\n", "paths.inputs"),
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
    ("paths:\n  input: [data]\n", "paths.input", "値"),
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


# 複数の基底を1つのマージキーにリストで並べた上書き。リスト形式では先に書いた基底が
# 勝つ（YAML の仕様）。単価表の項目はリストの中なので、既定のキー集合の検査は掛からない
_MULTI_BASE_YAML = """
model_prices:
  patterns:
    - &sonnet
      match: "sonnet"
      input: 3.0
      output: 15.0
    - &opus
      match: "opus"
      input: 5.0
      output: 25.0
    - {merge}
      match: "special"
"""


def test_merge_key_list_form_keeps_the_first_base(tmp_path):
    """複数の基底はリスト形式で書く。先に書いた基底の値が残る。"""
    text = _MULTI_BASE_YAML.format(merge="<<: [*sonnet, *opus]")
    patterns = load_config(_override(tmp_path, text))["model_prices"]["patterns"]

    assert patterns[2] == {"match": "special", "input": 3.0, "output": 15.0}
    assert patterns[2] == yaml.safe_load(text)["model_prices"]["patterns"][2]


def test_duplicate_merge_keys_are_rejected(tmp_path):
    """1つのマッピングにマージキーを2つ並べるのは許さない。

    リスト形式と優先が逆になるため、同じ並びで書いても展開の結果が形によって変わる。
    """
    text = _MULTI_BASE_YAML.format(merge="<<: *sonnet\n      <<: *opus")
    with pytest.raises(ValueError, match="キー '<<' が重複しています"):
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
    # 非有限の閾値は上限到達の判定を黙って変え、設定値の金額表示も壊す
    ("usage_credits:\n  cap_tolerance_usd: .nan\n", "cap_tolerance_usd"),
    ("usage_credits:\n  grant_suggested_cap_usd: .inf\n", "grant_suggested_cap_usd"),
    ("usage_credits:\n  grant_suggested_cap_usd: -1\n", "grant_suggested_cap_usd"),
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


# ------------------------------------------------------- 入出力ディレクトリ（paths）


def test_paths_default_to_the_builtin_directories(tmp_path, monkeypatch):
    """設定を書かなければ、従来どおりカレントからの input / reports。"""
    monkeypatch.chdir(tmp_path)
    assert load_config()["paths"] == {"input": "input", "output": "reports"}


def test_paths_in_the_override_are_resolved_from_its_location(tmp_path):
    """上書きファイルに書いた相対パスは、そのファイルの置き場所が基準になる。

    ワークスペースをどのディレクトリから使っても同じ場所を指すようにするため。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    path = _write(ws / "config.yaml", "paths:\n  input: data\n  output: out\n")

    assert load_config(path)["paths"] == {
        "input": str(ws / "data"), "output": str(ws / "out")}


def test_absolute_paths_in_the_override_are_kept(tmp_path):
    """絶対パスはそのまま使う（設定の置き場所で書き換えない）。"""
    target = tmp_path / "elsewhere" / "data"
    path = _override(tmp_path, f'paths:\n  input: "{target.as_posix()}"\n')

    assert load_config(path)["paths"]["input"] == str(target)


def test_home_relative_paths_in_the_override_are_expanded(tmp_path):
    """`~` はホームディレクトリに展開する。

    設定ファイルはシェルを介さないため、展開しないと `~` という名前のディレクトリを
    黙って指す（init-org ならそれを実際に作る）。
    """
    path = _override(tmp_path, "paths:\n  input: ~/seat-analysis-data/input\n")

    assert load_config(path)["paths"]["input"] == str(
        Path.home() / "seat-analysis-data" / "input")


def test_the_packaged_default_keeps_current_directory_paths(tmp_path, monkeypatch):
    """--config で組み込み既定そのものを指しても、入出力先はカレント基準のまま。

    ここだけ「設定ファイルの隣」を基準にすると、プログラムのインストール先を入力元に
    してしまう。既定を明示的に指す形はテスト全体が使っている（conftest の CONFIG）。
    """
    monkeypatch.chdir(tmp_path)
    assert load_config(PACKAGE_CONFIG_PATH)["paths"] == {
        "input": "input", "output": "reports"}

    # 書き方（相対指定）で判定が変わらない
    monkeypatch.chdir(PACKAGE_CONFIG_PATH.parent.parent)
    relative = Path(PACKAGE_CONFIG_PATH.parent.name) / PACKAGE_CONFIG_PATH.name
    assert load_config(relative)["paths"] == {"input": "input", "output": "reports"}


@pytest.mark.parametrize("text,where", [
    ('paths:\n  input: ""\n', "paths.input"),
    ("paths:\n  output: 3\n", "paths.output"),
])
def test_paths_that_are_not_usable_directories_are_rejected(tmp_path, text, where):
    """空文字や数値はディレクトリにならない（空文字はカレントとして解決されてしまう）。"""
    path = _override(tmp_path, text)
    with pytest.raises(ValueError, match=f"{where} は空でない文字列が必要です"):
        load_config(path)


@pytest.mark.parametrize("override", [None, "decision:\n  hysteresis_months: 3\n"])
def test_cli_falls_back_to_the_current_directory(tmp_path, monkeypatch, override):
    """paths を書かない限り、入出力はカレントの input/ と reports/ のまま。"""
    ws = tmp_path / "ws"
    _org_input(ws / "input")
    if override is not None:
        _override(ws, override)
    monkeypatch.chdir(ws)

    assert main(["analyze", "--month", MONTH]) == 0
    assert (ws / "reports" / "org-a" / MONTH / "report.md").is_file()


def test_cli_uses_the_paths_from_the_workspace_config(tmp_path, monkeypatch):
    """フラグを省いたら config.yaml の paths を使う。"""
    ws = tmp_path / "ws"
    _org_input(ws / "data")
    _override(ws, "paths:\n  input: data\n  output: out\n")
    monkeypatch.chdir(ws)

    assert main(["analyze", "--month", MONTH]) == 0
    assert (ws / "out" / "org-a" / MONTH / "report.md").is_file()
    assert not (ws / "reports").exists()


def test_cli_dir_flags_win_over_the_workspace_config(tmp_path, monkeypatch):
    """フラグを明示したらそちらが勝つ（設定より優先）。"""
    ws = tmp_path / "ws"
    _org_input(ws / "data", org="org-a")
    _org_input(tmp_path / "given", org="org-b")
    _override(ws, "paths:\n  input: data\n  output: out\n")
    monkeypatch.chdir(ws)

    assert main([
        "analyze", "--month", MONTH,
        "--input-dir", str(tmp_path / "given"), "--output-dir", str(tmp_path / "given-out"),
    ]) == 0
    assert (tmp_path / "given-out" / "org-b" / MONTH / "report.md").is_file()
    assert not (ws / "out").exists()


def test_doctor_uses_the_paths_from_the_workspace_config(tmp_path, monkeypatch, capsys):
    """出力を書かないコマンドも同じ規則で入力先を決める。"""
    ws = tmp_path / "ws"
    _org_input(ws / "data")
    _override(ws, "paths:\n  input: data\n")
    monkeypatch.chdir(ws)
    capsys.readouterr()

    assert main(["doctor", "--month", MONTH]) == 0
    assert "org-a" in capsys.readouterr().out


def test_cli_resolves_config_paths_from_the_config_location(tmp_path, monkeypatch):
    """--config で別の場所の設定を読んでも、相対パスはその設定の隣を指す。"""
    ws = tmp_path / "ws"
    _org_input(ws / "data")
    config_path = _override(ws, "paths:\n  input: data\n  output: out\n")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "data").mkdir(parents=True)   # カレント側の同名ディレクトリは見ない
    monkeypatch.chdir(elsewhere)

    assert main(["analyze", "--config", str(config_path), "--month", MONTH]) == 0
    assert (ws / "out" / "org-a" / MONTH / "report.md").is_file()
    assert not (elsewhere / "out").exists()


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


def test_init_follows_the_paths_in_an_existing_config(tmp_path, monkeypatch):
    """記入済みの config.yaml があれば、その paths のディレクトリを作って除外する。

    雛形の作成先と、以降の分析が読み書きする場所を食い違わせない。
    """
    monkeypatch.chdir(tmp_path)
    _override(tmp_path, "paths:\n  input: data\n  output: out\n")

    assert main(["init"]) == 0
    assert (tmp_path / "data").is_dir()
    assert not (tmp_path / "input").exists()
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"/config.yaml", "/data/", "/out/"} <= set(lines)
    assert "/input/" not in lines and "/reports/" not in lines


def test_init_input_dir_flag_wins_over_the_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _override(tmp_path, "paths:\n  input: data\n  output: out\n")

    assert main(["init", "--input-dir", "given"]) == 0
    assert (tmp_path / "given").is_dir()
    assert not (tmp_path / "data").exists()
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"/given/", "/out/"} <= set(lines)
    assert "/data/" not in lines


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


def test_init_reports_an_output_dir_outside_the_workspace(tmp_path, monkeypatch, capsys):
    """出力先も、ワークスペースの外を指していれば行にできないと伝える。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = (tmp_path / "outside").as_posix()
    _write(workspace / "config.yaml", f'paths:\n  output: "{outside}"\n')
    monkeypatch.chdir(workspace)
    capsys.readouterr()

    assert main(["init"]) == 0
    lines = (workspace / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"/config.yaml", "/input/"} <= set(lines)
    assert not any("outside" in line for line in lines)
    out = capsys.readouterr().out
    assert "出力ディレクトリ" in out and ".gitignore の対象外" in out


def test_init_gitignore_escapes_pattern_characters(tmp_path, monkeypatch):
    """名前に含まれる `*` や `[` はパターンとして解釈されないようにする。"""
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", "in[1]put"]) == 0
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/in\\[1\\]put/" in lines


@requires_git
def test_gitignore_entries_are_effective_in_git(tmp_path, monkeypatch):
    """書いた行が git に意図どおり解釈される（除外の実効を git 自身に確かめる）。

    エスケープの規則は git の側にあるので、生成した文字列を見るだけでは
    「保護したつもり」で終わりうる。
    """
    env = _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--input-dir", "in[1]put"]) == 0
    for rel in ("in[1]put/spend.csv", "i1put/other.csv", "config.yaml"):
        _touch(tmp_path / rel)

    assert _git_ignores(tmp_path, env, "in[1]put/spend.csv")   # 名前どおりに一致する
    assert _git_ignores(tmp_path, env, "config.yaml")
    # 文字クラスとして解釈されていない
    assert not _git_ignores(tmp_path, env, "i1put/other.csv")


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
    """既存の .gitignore は残し、足りない行だけを足す（照合は行の完全一致）。"""
    monkeypatch.chdir(tmp_path)
    head = "# 利用者が書いた行\n.venv/\n/input/\n"
    path = _write(tmp_path / ".gitignore", head)

    assert main(["init"]) == 0
    text = path.read_text(encoding="utf-8")
    assert text.startswith(head)
    lines = text.splitlines()
    assert lines.count("/input/") == 1
    assert "/config.yaml" in lines and "/reports/" in lines


@requires_git
def test_init_adds_an_effective_line_beside_an_ineffective_one(tmp_path, monkeypatch, capsys):
    """git が除外として読まない行は「設定済み」と数えない。

    先頭の空白はパターンの一部になるため `  /input/` は input/ を除外しない。
    見た目の同じ行を数えると、保護がないまま「そろっている」と表示してしまう。
    """
    env = _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".gitignore", "  /input/\n")
    capsys.readouterr()

    assert main(["init"]) == 0
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "  /input/" in lines   # 利用者が書いた行は消さない
    assert "/input/" in lines     # 効く行を足す
    assert "行を追記" in capsys.readouterr().out

    _touch(tmp_path / "input" / "spend.csv")
    assert _git_ignores(tmp_path, env, "input/spend.csv")


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


def test_init_org_follows_the_paths_in_the_workspace_config(tmp_path, monkeypatch):
    """組織の雛形も config.yaml の paths の下に作る（分析が読む場所と揃える）。"""
    monkeypatch.chdir(tmp_path)
    _override(tmp_path, "paths:\n  input: data\n  output: out\n")

    assert main(["init-org", "org-x"]) == 0
    assert (tmp_path / "data" / "org-x" / "spend").is_dir()
    assert (tmp_path / "out" / "org-x").is_dir()
    assert not (tmp_path / "input").exists()


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
