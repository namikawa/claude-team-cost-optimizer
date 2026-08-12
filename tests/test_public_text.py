"""公開テキストの検査（check-text）のテスト。

リポジトリの外に出る文章・差分を、入力データから集めた禁止語と突き合わせる経路が対象。
baseline（すでに公開されている内容）はテスト側で用意したものを使う。
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from seat_analyzer import leakcheck, public_text
from seat_analyzer.cli import main
from seat_analyzer.config import load_config

from .conftest import CONFIG, REPO_ROOT, spend_row


@pytest.fixture
def publish_input(make_input, tmp_path):
    """公開テキスト検査用の入力と、テストが制御する baseline。

    baseline（すでに公開されている内容）には tests/ も含まれるため、実リポジトリを
    ルートにするとテスト自身に書いた固有名が「公開済み」と判定されてしまう。
    テストでは --repo-root で空の baseline を指し、公開済みとみなす内容を明示する。
    """
    input_dir = make_input(
        {"2026-06": [spend_row("quillon.marsden@zz.example", 10.0)]},
        members=["quillon.marsden@zz.example,Premium"], org="zephyr-holdings")
    (input_dir / "zephyr-holdings" / "members-info.csv").write_text(
        "email,部署,チーム\nquillon.marsden@zz.example,増枠推進室,ZTeamX\n", encoding="utf-8")
    (tmp_path / "baseline").mkdir()
    return input_dir


def _check(text: str, publish_input: Path, tmp_path: Path, *extra: str) -> int:
    return main(["check-text", "--config", CONFIG, "--input-dir", str(publish_input),
                 "--output-dir", str(tmp_path / "reports"),
                 "--repo-root", str(tmp_path / "baseline"), "--text", text, *extra])


def test_check_text_detects_org_and_group_names(publish_input, tmp_path, capsys):
    """公開テキストに組織名・部署名・人名が含まれていれば検出する。"""
    capsys.readouterr()
    assert _check("zephyr-holdings の team 列を直した", publish_input, tmp_path) == 1
    err = capsys.readouterr().err
    assert "zephyr-holdings（org・--allow-term では許可できません）" in err
    # 許可できない語しか無いときに許可の案内を出さない（実挙動と食い違うため）
    assert "--allow-term <語> で許可できます" not in err

    for text, term in [("増枠推進室 の削減余地", "増枠推進室"),
                       ("ZTeamX チームの需要", "ZTeamX"),
                       ("marsden さんの利用", "marsden")]:
        assert _check(text, publish_input, tmp_path) == 1
        out = capsys.readouterr().err
        assert term in out
        assert "--allow-term <語> で許可できます" in out  # こちらは許可できる種類


def test_check_text_reports_term_count(publish_input, tmp_path, capsys):
    """成功時にも照合語数を出す（検査が退化していないことを目視できるように）。"""
    capsys.readouterr()
    assert _check("業務情報を含まない文章です", publish_input, tmp_path) == 0
    assert "語と照合" in capsys.readouterr().out


def test_check_text_passes_text_without_business_info(publish_input, tmp_path, capsys):
    capsys.readouterr()
    assert _check(
        "ある組織の team 列に短い英字略称が含まれており、誤検出することを再現した。",
        publish_input, tmp_path) == 0
    assert "検出されませんでした" in capsys.readouterr().out


def test_check_text_ignores_already_public_names(publish_input, tmp_path):
    """すでに公開されている内容（examples/ の合成データ等）に現れる語は検出しない。

    合成サンプルの人名は実在の姓と偶然一致しうるが、その文字列は公開済みなので
    公開テキストに書いても新たな開示にはあたらない。
    """
    baseline = tmp_path / "baseline" / "examples"
    baseline.mkdir(parents=True)
    (baseline / "members-info.csv").write_text(
        "email\nquillon.marsden@zz.example\n", encoding="utf-8")
    assert _check("marsden 相当の利用水準だった", publish_input, tmp_path) == 0
    # 公開済みでない部署名は引き続き検出する
    assert _check("ZTeamX の需要", publish_input, tmp_path) == 1


def test_check_text_uses_repo_baseline_by_default(
    publish_input, tmp_path, capsys, monkeypatch,
):
    """--repo-root 省略時はカレントディレクトリを baseline とする。"""
    monkeypatch.chdir(REPO_ROOT)
    capsys.readouterr()
    # 実リポジトリの examples/ にある合成データの人名は検出されない
    rc = main(["check-text", "--config", CONFIG, "--input-dir", str(publish_input),
               "--output-dir", str(tmp_path / "reports"),
               "--text", "対象組織の watanabe@... は他組織の tanabe を部分文字列として含む"])
    assert rc == 0
    assert "検出されませんでした" in capsys.readouterr().out


def test_check_text_rejects_an_unrelated_cwd_as_baseline(
    publish_input, tmp_path, capsys, monkeypatch,
):
    """--repo-root 省略時、カレントがこのリポジトリでなければ検査を始めない。

    別のリポジトリを baseline にすると、その HEAD に現れる語が「公開済み」として
    素通りする。どの語が抜けたかは出力に現れないので、fail-closed にする。
    """
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    rc = main(["check-text", "--config", CONFIG, "--input-dir", str(publish_input),
               "--output-dir", str(tmp_path / "reports"),
               "--text", "業務情報を含まない文章です"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "リポジトリのルートではありません" in err
    assert "--repo-root" in err


def test_check_text_does_not_check_an_explicit_repo_root(publish_input, tmp_path, monkeypatch):
    """--repo-root を明示したときは同一性を確かめない（利用者の意識的な選択）。"""
    monkeypatch.chdir(tmp_path)  # カレントはこのリポジトリではない
    assert _check("業務情報を含まない文章です", publish_input, tmp_path) == 0


def test_validate_baseline_root_accepts_this_repository():
    public_text.validate_baseline_root(REPO_ROOT)


def test_validate_baseline_root_rejects_other_roots(tmp_path):
    """目印は pyproject.toml の project.name（別プロジェクト・壊れた TOML も拒否）。"""
    with pytest.raises(leakcheck.LeakCheckError, match="リポジトリのルートではありません"):
        public_text.validate_baseline_root(tmp_path)

    other = tmp_path / "pyproject.toml"
    other.write_text('[project]\nname = "other-tool"\n', encoding="utf-8")
    with pytest.raises(leakcheck.LeakCheckError):
        public_text.validate_baseline_root(tmp_path)

    other.write_text("[project\n", encoding="utf-8")
    with pytest.raises(leakcheck.LeakCheckError):
        public_text.validate_baseline_root(tmp_path)


def test_check_text_allow_term(publish_input, tmp_path):
    assert _check("ZTeamX の需要", publish_input, tmp_path) == 1
    assert _check("ZTeamX の需要", publish_input, tmp_path, "--allow-term", "ZTeamX") == 0


class _FakeStdin:
    """バイト列を .buffer から読ませる標準入力の代用（テキストラッパーを持たない）。"""

    def __init__(self, raw: bytes):
        self.buffer = io.BytesIO(raw)


def test_decode_candidates_returns_every_readable_interpretation():
    """cp932 のバイト列が UTF-8 としても読めるとき、両方の解釈を検査対象にする。

    片方だけを見ると、別の文字列として読めた側で禁止語を取りこぼす。
    """
    raw = "燿テ".encode("cp932")
    readings = dict(public_text.decode_candidates(raw))

    assert readings["cp932"] == "燿テ"
    # UTF-8 としても妥当なので例外にならず、別の文字列として読めてしまう
    assert readings["utf-8-sig"] != "燿テ"


def test_decode_candidates_rejects_undecodable_bytes():
    """どの文字コードでも読めない入力は、素通りさせず失敗させる。"""
    # 0x81 は cp932 の2バイト文字の1バイト目で、空白は2バイト目になれない
    with pytest.raises(ValueError, match="文字コードを判別できません"):
        public_text.decode_candidates(b"\x81\x20")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_decode_candidates_rejects_utf16_and_utf32(encoding):
    """UTF-16 / UTF-32 の入力は素通りさせない。

    どちらも utf-8 や cp932 として「読めてしまう」ことがある（cp932 は 0xFD-0xFF を
    私用領域へ写すため BOM も通り、ASCII 中心の UTF-16 は NUL 混じりの UTF-8 として
    読める）。化けたテキストでは禁止語に一致せず検査が素通りする。
    PowerShell の Out-File や旧 Notepad の「Unicode」保存で作った文書をファイル引数で
    渡すと、この形のバイト列がそのまま届く。
    """
    raw = "zephyr-holdings の team 列\n".encode(encoding)

    with pytest.raises(ValueError, match="UTF-16 / UTF-32|NUL バイト"):
        public_text.decode_candidates(raw)


@pytest.mark.parametrize("encoding", ["utf-8", "cp932"])
def test_check_text_detects_terms_regardless_of_input_encoding(
    publish_input, tmp_path, monkeypatch, capsys, encoding,
):
    """標準入力の文字コードによらず禁止語を検出する。

    Windows PowerShell はネイティブコマンドへのパイプをロケール既定（cp932）で流すため、
    UTF-8 のバイト列が届くとは限らない。
    """
    monkeypatch.setattr(sys, "stdin", _FakeStdin("増枠推進室 の削減余地\n".encode(encoding)))
    capsys.readouterr()

    assert main(["check-text", "--config", CONFIG, "--input-dir", str(publish_input),
                 "--output-dir", str(tmp_path / "reports"),
                 "--repo-root", str(tmp_path / "baseline"), "-"]) == 1
    assert "(標準入力)" in capsys.readouterr().err


def test_check_text_reads_file_and_stdin(publish_input, tmp_path, monkeypatch, capsys):
    path = tmp_path / "comment.md"
    path.write_text("zephyr-holdings の team 列\n", encoding="utf-8")
    args = ["check-text", "--config", CONFIG, "--input-dir", str(publish_input),
            "--output-dir", str(tmp_path / "reports"),
            "--repo-root", str(tmp_path / "baseline")]
    assert main([*args, str(path)]) == 1

    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("増枠推進室 の削減余地\n"))
    capsys.readouterr()
    assert main([*args, "-"]) == 1
    assert "(標準入力)" in capsys.readouterr().err


def test_check_text_checks_every_input(publish_input, tmp_path, capsys):
    clean = tmp_path / "clean.md"
    clean.write_text("ある組織のレポートを生成した\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text("ZTeamX の需要\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["check-text", "--config", CONFIG, "--input-dir", str(publish_input),
                 "--output-dir", str(tmp_path / "reports"),
                 "--repo-root", str(tmp_path / "baseline"), str(clean), str(dirty)]) == 1
    captured = capsys.readouterr()
    assert "clean.md: 業務情報は検出されませんでした" in captured.out
    assert "ZTeamX" in captured.err


def test_check_text_fails_closed_when_no_terms(tmp_path, capsys, monkeypatch):
    """禁止語を1件も集められない状態では成功させない。

    --input-dir が解決できないと照合が空振りし、何を渡しても「検出なし」になる。
    青信号にしか見えないので、fail-closed でエラー終了する。
    """
    monkeypatch.chdir(REPO_ROOT)  # --repo-root 省略時の既定（カレント）を有効にする
    capsys.readouterr()
    rc = main(["check-text", "--config", CONFIG,
               "--input-dir", str(tmp_path / "nonexistent"),
               "--output-dir", str(tmp_path / "reports"), "--text", "何かの文章"])
    assert rc == 1
    assert "入力ディレクトリがありません" in capsys.readouterr().err

    # 入力ディレクトリはあるが組織が無い場合も同様
    (tmp_path / "empty-input").mkdir()
    rc = main(["check-text", "--config", CONFIG,
               "--input-dir", str(tmp_path / "empty-input"),
               "--output-dir", str(tmp_path / "reports"), "--text", "何かの文章"])
    assert rc == 1
    assert "禁止語を1件も収集できませんでした" in capsys.readouterr().err


def _git_repo(root: Path):
    """テスト用の git リポジトリを作り、git コマンドを実行するヘルパを返す。"""
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                            "PATH": os.environ.get("PATH", ""), "HOME": str(root)})
    git("init", "-q")
    return git


def test_public_baseline_uses_committed_content(tmp_path):
    """baseline は HEAD の内容。作業ツリーの状態は一切見ない。

    未追跡ファイルや gitignore 済みファイルはもちろん、**追跡ファイルの未コミット編集**も
    baseline に入ってはいけない。入ると「テストに実データを書いた状態で公開文章を検査する」
    という、検査を無意味にする状態を素通りさせる。
    """
    git = _git_repo(tmp_path)
    (tmp_path / "tracked.md").write_text("tracked-name\n", encoding="utf-8")
    git("add", "tracked.md")
    git("commit", "-q", "-m", "init")

    (tmp_path / "untracked.md").write_text("untracked-name\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("ignored-name\n", encoding="utf-8")
    # 追跡ファイルへの未コミット追記
    (tmp_path / "tracked.md").write_text("tracked-name\nuncommitted-name\n", encoding="utf-8")

    baseline = public_text.public_baseline(tmp_path)
    assert "tracked-name" in baseline
    assert "uncommitted-name" not in baseline
    assert "untracked-name" not in baseline
    assert "ignored-name" not in baseline


def test_public_baseline_errors_when_git_unusable(tmp_path, monkeypatch):
    """git 管理下なのに git を実行できない場合は黙って弱くならずエラーにする。"""
    git = _git_repo(tmp_path)
    (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
    git("add", "a.md")
    git("commit", "-q", "-m", "init")

    monkeypatch.setattr(public_text, "_git_bytes", lambda *a, **kw: None)
    with pytest.raises(leakcheck.LeakCheckError, match="git を実行できません"):
        public_text.public_baseline(tmp_path)

    # .git が無い（--repo-root に非 git を明示指定）ならフォールバックしてよい
    plain = tmp_path / "plain"
    (plain / "examples").mkdir(parents=True)
    (plain / "examples" / "s.csv").write_text("public-name\n", encoding="utf-8")
    assert "public-name" in public_text.public_baseline(plain)


def test_public_baseline_excludes_checked_file_itself(tmp_path):
    """検査対象のファイル自身は baseline から除く（自分を根拠に素通りさせない）。"""
    draft = tmp_path / "draft.md"
    draft.write_text("draft-name\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "s.csv").write_text("public-name\n", encoding="utf-8")

    assert "draft-name" in public_text.public_baseline(tmp_path, ("draft.md", "examples"))
    excluded = public_text.public_baseline(
        tmp_path, ("draft.md", "examples"), exclude=(draft,))
    assert "draft-name" not in excluded
    assert "public-name" in excluded


def test_check_text_diff_mode_ignores_removed_lines(publish_input, tmp_path):
    """--diff は追加される内容だけを見る。削除行の語で落とさない。

    「全部削除行だから問題ない」という目視判断は見落としやすいので機械化する。
    """
    removal_only = (
        "diff --git a/x.md b/x.md\n"
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "@@ -1,2 +1,2 @@\n"
        "-  ZTeamX の需要\n"
        "+  ある組織の需要\n"
    )
    assert _check(removal_only, publish_input, tmp_path) == 1            # 素の検査は落ちる
    assert _check(removal_only, publish_input, tmp_path, "--diff") == 0  # 追加分はクリーン

    # 追加行に業務情報があれば --diff でも落ちる
    adds = removal_only.replace("+  ある組織の需要", "+  増枠推進室 の需要")
    assert _check(adds, publish_input, tmp_path, "--diff") == 1


def test_diff_added_text_keeps_new_paths(publish_input, tmp_path):
    """新規追加ファイルのパス自体に業務情報がある場合も拾う。"""
    diff = ("diff --git a/ZTeamX.md b/ZTeamX.md\n"
            "--- /dev/null\n"
            "+++ b/ZTeamX.md\n"
            "+内容\n")
    extract = public_text.diff_added_text(diff)
    assert "b/ZTeamX.md" in extract.text
    assert "/dev/null" not in extract.text
    assert (extract.n_added_lines, extract.n_paths) == (1, 1)
    assert _check(diff, publish_input, tmp_path, "--diff") == 1


def test_diff_added_text_keeps_rename_targets(publish_input, tmp_path):
    """内容変更を伴わない rename は +++ 行を持たないため rename to から拾う。"""
    diff = ("diff --git a/old.md b/ZTeamX.md\n"
            "similarity index 100%\n"
            "rename from old.md\n"
            "rename to ZTeamX.md\n")
    assert "ZTeamX.md" in public_text.diff_added_text(diff).text
    assert _check(diff, publish_input, tmp_path, "--diff") == 1


def test_diff_mode_rejects_non_diff_input(publish_input, tmp_path, capsys):
    """差分でない入力に --diff を付けたら素通りさせずエラーにする。

    抽出結果が空になって「N 語と照合したが検出なし」と出ると、完全な青信号に見える。
    フラグの取り違えは現実に起きるので、入力側でも fail-closed にする。
    """
    text = "zephyr-holdings の ZTeamX について"
    assert _check(text, publish_input, tmp_path) == 1              # 素の検査は検出する
    capsys.readouterr()
    assert _check(text, publish_input, tmp_path, "--diff") == 1    # 素通りさせない
    assert "unified diff ではありません" in capsys.readouterr().err


def test_diff_mode_reports_extraction_size(publish_input, tmp_path, capsys):
    """成功時に抽出量を出す（追加行 0 なら検査対象が無かったと分かる）。"""
    empty_diff = "diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1 +0,0 @@\n-消す行\n"
    capsys.readouterr()
    assert _check(empty_diff, publish_input, tmp_path, "--diff") == 0
    out = capsys.readouterr().out
    assert "追加行 0" in out and "対象パス 1" in out


def test_check_text_cannot_exempt_org_names(publish_input, tmp_path):
    """組織名は常時禁止。--allow-term でも設定でも除外できない。"""
    assert _check("zephyr-holdings の話", publish_input, tmp_path) == 1
    assert _check("zephyr-holdings の話", publish_input, tmp_path,
                  "--allow-term", "zephyr-holdings") == 1

    # 組織名を除外する設定項目は存在しない（書いても未知のキーとして拒否される）
    import yaml
    cfg = yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8"))
    cfg["discussion"] = {**cfg["discussion"], "public_org_names": ["zephyr-holdings"]}
    path = tmp_path / "config-public-org.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="discussion.public_org_names"):
        load_config(path)


def test_public_baseline_excludes_local_only_paths(tmp_path):
    """gitignore 対象（input/・reports/・CLAUDE.md）は baseline に含めない。"""
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "sample.csv").write_text("public-name\n", encoding="utf-8")
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "secret.csv").write_text("secret-name\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("local-only-name\n", encoding="utf-8")
    baseline = public_text.public_baseline(tmp_path)
    assert "public-name" in baseline
    assert "secret-name" not in baseline
    assert "local-only-name" not in baseline


def test_public_baseline_includes_template_assets(tmp_path):
    """.py の外にある資材（templates/ の .css・.j2）も baseline に含める。

    ダッシュボードのスタイルと HTML 断片はコードではなくテンプレートとして置かれている。
    拡張子の網羅が実体からずれると、公開済みの語が baseline から抜けたまま検査が走る。
    """
    templates = tmp_path / "src" / "seat_analyzer" / "templates"
    (templates / "partials").mkdir(parents=True)
    (templates / "dashboard.css").write_text(
        ".seat { color: #333; }  /* css-name */\n", encoding="utf-8")
    (templates / "dashboard.html.j2").write_text("<h1>j2-name</h1>\n", encoding="utf-8")
    (templates / "partials" / "trend.html.j2").write_text(
        "<p>partial-name</p>\n", encoding="utf-8")

    baseline = public_text.public_baseline(tmp_path)
    assert "css-name" in baseline
    assert "j2-name" in baseline
    assert "partial-name" in baseline
