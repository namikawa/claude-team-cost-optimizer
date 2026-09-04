"""GitHub 対応表（input/<組織名>/github-members.csv）のロードと、gh を呼ぶ probe・発見・
merged PR の収集のテスト。

対応表では、取り違えに直結するものを止めること（必須カラムの欠落・email の欠落と重複・
login の重複）と、読めない値で分析を止めないこと（空欄・字句として読めない login を未対応へ
倒して警告する）の両方を固定する。読み取りだけのモジュールなので、同じ入力からは常に同じ
結果と同じ並びになることまで見る（entries は行順・警告も行順・未対応の一覧は昇順）。

probe では、gh を実行できない場合の分類と、応答の解釈（HTTP ステータス・ヘッダ・本文）を
見る。repository の発見では、除外（archived / fork / template）と pagination のほか、
完全な一覧を得られなかった場合に一覧を返さないことを固定する。PR の収集では、保存する
項目が §7.6 の9項目だけであること・repository と number で一意になること・取り切れなかった
窓の結果を採らないことを固定する。実際の gh もネットワークも呼ばない。issue への変換は
tests/test_data_quality.py が見る。

gh へ渡す引数が読み取りだけであること（GitHub 上の情報を変更する呼び出しを作らないこと）は
`assert_read_only` が全入口の呼び出しをまとめて検査する。
"""

import copy
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

import pytest

from seat_analyzer.github_collect import (
    _MAX_ATTEMPTS,
    _MAX_SEARCH_PAGES,
    _PR_ENTRY_KEYS,
    _PR_SEARCH_QUERY,
    _REPOS_PER_PAGE,
    _RETRY_PAUSES,
    _SEARCH_PER_PAGE,
    _SEARCH_RESULT_CAP,
    _TRANSIENT_STATUSES,
    GH_EXIT_AUTH_REQUIRED,
    GITHUB_MEMBERS_FILENAME,
    PR_CACHE_DIRNAME,
    PR_CACHE_SCHEMA,
    CachedPr,
    DateWindow,
    GhFailure,
    GhResult,
    GithubMemberLink,
    GithubMembers,
    PrCache,
    PrCollection,
    RepoDiscovery,
    _parse_response,
    collect_merged_prs,
    discover_repositories,
    gated_orgs,
    is_github_org_name,
    load_github_members,
    load_pr_cache,
    missing_scopes,
    month_windows,
    pr_cache_path,
    probe_github,
    run_gh,
    unmapped_emails,
)

HEADER = "email,github_login"
JP_HEADER = "メールアドレス,githubユーザー名"


def _write(input_dir: Path, rows: list[str], header: str = HEADER,
           encoding: str = "utf-8") -> Path:
    """組織ディレクトリ直下に対応表を1つ置く（行はそのまま書く）。"""
    input_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{row}\n" for row in rows)
    (input_dir / GITHUB_MEMBERS_FILENAME).write_text(
        header + "\n" + body, encoding=encoding, newline="\n"
    )
    return input_dir


# ------------------------------------------------------------------- 入力なし


def test_missing_file_is_not_provided(tmp_path, cfg):
    """対応表が無くてもエラーにしない（GitHub なしでも分析できる）。"""
    result = load_github_members(tmp_path, cfg)

    assert result == GithubMembers(entries=(), source=None, warnings=())
    assert result.provided is False


def test_header_only_file_is_provided_but_empty(tmp_path, cfg):
    """ヘッダだけのファイルは「未提供」と区別する（置いてあるが中身が無い状態）。"""
    _write(tmp_path, [])
    result = load_github_members(tmp_path, cfg)

    assert result.provided is True
    assert result.source == GITHUB_MEMBERS_FILENAME
    assert result.entries == ()
    assert result.warnings == (
        (f"{GITHUB_MEMBERS_FILENAME}: データ行がありません"
         "（ヘッダだけのファイルが置かれています）"),
    )


# --------------------------------------------------------------- 通常の読み取り


def test_entries_keep_the_row_order(tmp_path, cfg):
    """entries は入力の行順を保つ（表を直す人が行番号で辿れるようにする）。"""
    _write(tmp_path, [
        "user3@example.com,third-login",
        "user1@example.com,first-login",
        "user2@example.com,second_login",
    ])
    result = load_github_members(tmp_path, cfg)

    assert result.entries == (
        GithubMemberLink(email="user3@example.com", github_login="third-login"),
        GithubMemberLink(email="user1@example.com", github_login="first-login"),
        GithubMemberLink(email="user2@example.com", github_login="second_login"),
    )
    assert result.warnings == ()


def test_email_is_normalized_but_login_keeps_its_case(tmp_path, cfg):
    """email は突き合わせ用に小文字へ揃え、login は原文の大小を保つ。"""
    _write(tmp_path, ["  User1@Example.COM  ,  Example-User  "])
    result = load_github_members(tmp_path, cfg)

    assert result.entries == (
        GithubMemberLink(email="user1@example.com", github_login="Example-User"),
    )


@pytest.mark.parametrize("header", [
    "email,github_login",
    "Email,GitHub Login",
    "User Email,GitHub Username",
    "email,GitHub User",
    "email,GitHub ID",
    "メールアドレス,githubログイン",
    JP_HEADER,
    "メールアドレス,githubアカウント",
])
def test_alias_headers_are_resolved(tmp_path, cfg, header):
    """既定のエイリアスが、実ファイルに現れる書き方のヘッダを正準名へ写す。

    エイリアスもヘッダも `normalize_header` を通してから照合するので、大小文字・空白・
    アンダースコアの違いは吸収される。日本語のエイリアスは字句がそのまま鍵になる。
    """
    _write(tmp_path, ["user1@example.com,example-user"], header=header)
    assert load_github_members(tmp_path, cfg).entries == (
        GithubMemberLink(email="user1@example.com", github_login="example-user"),
    )


def test_cp932_csv_is_read(tmp_path, cfg):
    """cp932 で保存された CSV も読める（Excel から書き出した表を受けるため）。"""
    _write(tmp_path, ["user1@example.com,example-user"], header=JP_HEADER,
           encoding="cp932")
    assert load_github_members(tmp_path, cfg).entries == (
        GithubMemberLink(email="user1@example.com", github_login="example-user"),
    )


def test_extra_columns_are_ignored(tmp_path, cfg):
    """正準列以外の列（メモ等）があっても読める。"""
    _write(tmp_path, ["user1@example.com,example-user,確認済み"],
           header="email,github_login,備考")
    assert load_github_members(tmp_path, cfg).entries == (
        GithubMemberLink(email="user1@example.com", github_login="example-user"),
    )


def test_loading_twice_gives_the_same_result(tmp_path, cfg):
    """同じ入力からは常に同じ結果（現在時刻も乱数も参照しない）。"""
    _write(tmp_path, [
        "user1@example.com,",
        "user2@example.com,@example-user",
        "user3@example.com,example-user",
    ])
    assert load_github_members(tmp_path, cfg) == load_github_members(tmp_path, cfg)


# ------------------------------------------------------------------ 中止する条件


def test_duplicate_email_is_rejected(tmp_path, cfg):
    """email の重複は中止する（どちらの login が正か決められない）。"""
    _write(tmp_path, [
        "user1@example.com,first-login",
        "  User1@EXAMPLE.com ,second-login",
    ])
    with pytest.raises(ValueError, match="の行が複数あります（1 行目と 2 行目）"):
        load_github_members(tmp_path, cfg)


def test_duplicate_login_is_rejected_case_insensitively(tmp_path, cfg):
    """login の重複は大文字小文字を区別せずに中止する（GitHub の login は大小同一）。"""
    _write(tmp_path, [
        "user1@example.com,Example-User",
        "user2@example.com,example-user",
    ])
    with pytest.raises(ValueError, match="github_login 'example-user' の行が複数あります"):
        load_github_members(tmp_path, cfg)


def test_blank_email_is_rejected(tmp_path, cfg):
    """email の空欄は中止する（誰の login か決められない）。"""
    _write(tmp_path, ["user1@example.com,first-login", " ,second-login"])
    with pytest.raises(ValueError, match="2 行目の email が空です"):
        load_github_members(tmp_path, cfg)


@pytest.mark.parametrize("header", ["email,github_id_x", "user email,login"])
def test_missing_required_column_names_the_file(tmp_path, cfg, header):
    """必須カラムが見つからなければ、ファイル名を挙げて中止する。"""
    _write(tmp_path, ["user1@example.com,example-user"], header=header)
    with pytest.raises(ValueError, match="必須カラムが見つかりません") as excinfo:
        load_github_members(tmp_path, cfg)
    assert GITHUB_MEMBERS_FILENAME in str(excinfo.value)


def test_empty_file_is_a_clear_error(tmp_path, cfg):
    """0バイトのファイルも読み込みライブラリの例外ではなく、明確な ValueError にする。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / GITHUB_MEMBERS_FILENAME).write_text("", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="ヘッダ行がありません") as excinfo:
        load_github_members(tmp_path, cfg)
    assert GITHUB_MEMBERS_FILENAME in str(excinfo.value)


def test_ambiguous_header_is_rejected(tmp_path, cfg):
    """1つの正準列に対応するヘッダが2つある表は中止する。"""
    _write(tmp_path, ["user1@example.com,user2@example.com,example-user"],
           header="email,user email,github_login")
    with pytest.raises(ValueError, match="同じ列 email に対応するヘッダが複数あります"):
        load_github_members(tmp_path, cfg)


def test_rows_with_more_fields_than_the_header_are_rejected(tmp_path, cfg):
    """全データ行がヘッダより1列多い表は中止する（列がずれた対応を採用しない）。

    行にメモを足してヘッダを直し忘れると、読み込みが先頭の列を暗黙の行ラベルにして
    残りが1つずつずれる。ずれた値は email としても login としても字句上は通るため、
    止めなければ誤った対応が警告なしで採用される。
    """
    _write(tmp_path, [
        "user1@example.com,user1-login,確認済み",
        "user2@example.com,user2-login,確認済み",
    ])
    with pytest.raises(ValueError, match="列の対応を決められません") as excinfo:
        load_github_members(tmp_path, cfg)
    assert GITHUB_MEMBERS_FILENAME in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)


def test_alias_shared_by_two_canonical_columns_is_an_error(tmp_path, cfg):
    """1つのヘッダが2つの正準列の候補になっている設定は、入力を読む前に中止する。"""
    broken = copy.deepcopy(cfg)
    broken["columns"]["github_members"]["email"] = ["value"]
    broken["columns"]["github_members"]["github_login"] = ["value"]
    _write(tmp_path, ["user1@example.com,example-user"])
    with pytest.raises(ValueError, match="columns.github_members: ヘッダ 'value' が"):
        load_github_members(tmp_path, broken)


# --------------------------------------------------------- 未対応と警告（止めない）


def test_blank_login_is_unmapped_with_a_warning(tmp_path, cfg):
    """login の空欄は未対応として残し、警告する（行そのものは捨てない）。"""
    _write(tmp_path, ["user1@example.com,", "user2@example.com,example-user"])
    result = load_github_members(tmp_path, cfg)

    assert result.entries[0] == GithubMemberLink(
        email="user1@example.com", github_login=None
    )
    assert result.warnings == (
        (f"{GITHUB_MEMBERS_FILENAME}: user1@example.com: "
         "github_login が空欄です（未対応として扱います）"),
    )


@pytest.mark.parametrize("login", [
    "@example-user",           # 先頭の @ は黙って剥がさない
    "-example-user",           # 先頭のハイフン
    "example-user-",           # 末尾のハイフン
    "_example",                # 先頭のアンダースコア
    "example_",                # 末尾のアンダースコア
    "example--user",           # 連続するハイフン
    "example__user",           # 連続するアンダースコア
    "a_b_c",                   # アンダースコアが2個以上
    "example user",            # 空白
    "example.user",            # ドット
    "example/user",            # スラッシュ（URL の写し間違い）
    "サンプル",                  # ASCII 以外
    "a" * 40,                  # 39文字を超える
])
def test_unreadable_login_is_unmapped_with_a_warning(tmp_path, cfg, login):
    """GitHub の login として読めない字句は未対応にして警告する。"""
    _write(tmp_path, [f"user1@example.com,{login}"])
    result = load_github_members(tmp_path, cfg)

    assert result.entries == (
        GithubMemberLink(email="user1@example.com", github_login=None),
    )
    assert len(result.warnings) == 1
    assert "github_login を GitHub のログイン名として解釈できません" in result.warnings[0]


@pytest.mark.parametrize("login", [
    "a",                       # 1文字
    "a" * 39,                  # 39文字
    "example-user",
    "example_user",            # Enterprise Managed Users のアンダースコア区切り
    "jane-doe_acme",           # 同上（ユーザー名側にハイフンを含む形）
    "0123456789",
])
def test_readable_login_forms_are_accepted(tmp_path, cfg, login):
    """英数字で始まり英数字で終わる1〜39文字を login として受ける。

    区切りに使えるのは連続しないハイフンと、高々1個のアンダースコア。
    """
    _write(tmp_path, [f"user1@example.com,{login}"])
    result = load_github_members(tmp_path, cfg)

    assert result.entries == (
        GithubMemberLink(email="user1@example.com", github_login=login),
    )
    assert result.warnings == ()


def test_warnings_follow_the_row_order(tmp_path, cfg):
    """警告の並びは行順（同じ入力から常に同じ並びになる）。"""
    _write(tmp_path, [
        "user1@example.com,@first",
        "user2@example.com,second-login",
        "user3@example.com,",
        "user4@example.com,fourth user",
    ])
    result = load_github_members(tmp_path, cfg)

    assert [warning.split(": ")[1] for warning in result.warnings] == [
        "user1@example.com", "user3@example.com", "user4@example.com",
    ]


def test_warnings_do_not_contain_absolute_paths(tmp_path, cfg):
    """警告にはファイル名だけを載せる（値を実行環境に依存させない）。"""
    _write(tmp_path, ["user1@example.com,@example-user"])
    result = load_github_members(tmp_path, cfg)

    assert result.warnings
    assert all(str(tmp_path) not in warning for warning in result.warnings)


# --------------------------------------------------------------- 未対応の照合


def test_unmapped_emails_lists_members_without_a_login(tmp_path, cfg):
    """login に対応づかないメールを昇順・重複なしで返す。"""
    _write(tmp_path, [
        "user1@example.com,first-login",
        "user2@example.com,",
    ])
    members = load_github_members(tmp_path, cfg)

    assert unmapped_emails(members, [
        "user1@example.com",   # 対応あり
        "user2@example.com",   # login が空欄（未対応）
        " User3@Example.com ", # 対応表に無い
        "user3@example.com",   # 正規化後は同じ（重複しない）
        "",                    # 空のメールは対象にしない
    ]) == ("user2@example.com", "user3@example.com")


def test_unmapped_emails_without_a_mapping_file(tmp_path, cfg):
    """対応表が未提供なら、渡したメールはすべて未対応になる。"""
    members = load_github_members(tmp_path, cfg)

    assert unmapped_emails(members, ["user2@example.com", "user1@example.com"]) == (
        "user1@example.com", "user2@example.com",
    )


def test_unreadable_login_counts_as_unmapped(tmp_path, cfg):
    """字句として読めない login は、対応表に書かれていないのと同じ扱いにする。"""
    _write(tmp_path, ["user1@example.com,@example-user"])
    members = load_github_members(tmp_path, cfg)

    assert unmapped_emails(members, ["user1@example.com"]) == ("user1@example.com",)


# ------------------------------------------------------------ 値オブジェクト


def test_link_normalizes_and_validates_its_values():
    """GithubMemberLink は email を正規化し、読めない login を受け付けない。"""
    assert GithubMemberLink(email=" User1@Example.com ", github_login=None).email == (
        "user1@example.com"
    )
    with pytest.raises(ValueError, match="email は必須です"):
        GithubMemberLink(email="  ", github_login=None)
    with pytest.raises(ValueError, match="github_login として読めない値です"):
        GithubMemberLink(email="user1@example.com", github_login="@example-user")
    with pytest.raises(TypeError, match="github_login には文字列が必要です"):
        GithubMemberLink(email="user1@example.com", github_login=1)


@pytest.mark.parametrize("entries,message", [
    (
        (
            GithubMemberLink(email="user1@example.com", github_login="a-login"),
            GithubMemberLink(email="user1@example.com", github_login="b-login"),
        ),
        "email が重複しています",
    ),
    (
        (
            GithubMemberLink(email="user1@example.com", github_login="A-Login"),
            GithubMemberLink(email="user2@example.com", github_login="a-login"),
        ),
        "github_login が重複しています",
    ),
])
def test_members_rejects_duplicates(entries, message):
    """一意性は構築時にも確かめる（loader を通さずに組み立てた結果も同じ形にする）。"""
    with pytest.raises(ValueError, match=message):
        GithubMembers(entries=entries, source=GITHUB_MEMBERS_FILENAME, warnings=())


def test_members_allows_several_rows_without_a_login():
    """login を持たない行は何行あってもよい（未対応は重複ではない）。"""
    members = GithubMembers(
        entries=(
            GithubMemberLink(email="user1@example.com", github_login=None),
            GithubMemberLink(email="user2@example.com", github_login=None),
        ),
        source=GITHUB_MEMBERS_FILENAME,
        warnings=(),
    )
    assert members.provided is True
    assert unmapped_emails(members, ["user1@example.com"]) == ("user1@example.com",)


# ------------------------------------------------------------ Organization 名


@pytest.mark.parametrize("name", [
    "example",
    "Example-Org",
    "example-org-1",
    "e",
    "a" * 39,
])
def test_readable_organization_names_are_accepted(name):
    assert is_github_org_name(name) is True


@pytest.mark.parametrize("name", [
    "",
    " example",
    "-example",
    "example-",
    "example--org",
    "example_org",      # login と違い Organization 名にアンダースコアは無い
    "example/org",      # 経路を差し替えられる字句を通さない
    "example org",
    "a" * 40,
    None,
    1,
])
def test_unreadable_organization_names_are_rejected(name):
    assert is_github_org_name(name) is False


# ------------------------------------------------------------ 有効化のゲート


def test_gated_orgs_keeps_the_configured_order(cfg):
    config = {**cfg, "organizations": {
        "org-b": {"github_org": "example-two"},
        "org-a": {"github_org": "example-one"},
    }}

    assert gated_orgs(config) == {"org-b": "example-two", "org-a": "example-one"}


def test_gated_orgs_is_empty_by_default(cfg):
    """既定では1組織も有効になっていない（GitHub は組織ごとの opt-in）。"""
    assert gated_orgs(cfg) == {}


# ------------------------------------------------------------ gh の実行


def test_run_gh_reports_a_missing_command(monkeypatch):
    monkeypatch.setattr("seat_analyzer.github_collect.shutil.which", lambda _: None)

    assert run_gh(("auth", "status")) == GhResult(
        ok=False, failure=GhFailure.NOT_FOUND)


def _fake_which(monkeypatch) -> None:
    monkeypatch.setattr("seat_analyzer.github_collect.shutil.which", lambda _: "gh")


def test_run_gh_reports_a_timeout(monkeypatch):
    _fake_which(monkeypatch)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=1)

    monkeypatch.setattr("seat_analyzer.github_collect.subprocess.run", _timeout)

    assert run_gh(("auth", "status")).failure is GhFailure.TIMEOUT


def test_run_gh_reports_a_launch_failure(monkeypatch):
    _fake_which(monkeypatch)

    def _os_error(*args, **kwargs):
        raise PermissionError("実行できません")

    monkeypatch.setattr("seat_analyzer.github_collect.subprocess.run", _os_error)

    assert run_gh(("auth", "status")).failure is GhFailure.ERROR


class _Completed:
    def __init__(self, returncode: int, stdout: bytes):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = "gh: 診断の文言".encode()


def _fake_run(monkeypatch, returncode: int, stdout: bytes) -> dict:
    """subprocess.run を差し替え、渡された kwargs を返す（呼び出しの形を検査できる）。"""
    _fake_which(monkeypatch)
    seen: dict = {}

    def _run(*args, **kwargs):
        seen.update(kwargs)
        return _Completed(returncode, stdout)

    monkeypatch.setattr("seat_analyzer.github_collect.subprocess.run", _run)
    return seen


def test_run_gh_keeps_stdout_and_drops_stderr(monkeypatch):
    """返すのは終了コードと stdout だけ（診断文が message へ写る経路を作らない）。

    stderr は DEVNULL で読み込みすらしないことまで固定する（診断文が Python 側へ入る
    経路を fd の段階で断つ）。
    """
    kwargs = _fake_run(monkeypatch, 0, b"HTTP/2.0 200 OK\r\n\r\n{}\r\n")
    result = run_gh(("api", "-i", "user"))

    assert result.ok is True
    assert result.stdout == "HTTP/2.0 200 OK\n\n{}\n"   # 改行は LF へ揃える
    assert "診断" not in result.stdout
    assert not hasattr(result, "stderr")
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE


def test_run_gh_marks_a_non_zero_exit_without_calling_it_a_launch_failure(monkeypatch):
    """gh は 2xx 以外の応答でも終了コード 1 で終わる。応答は stdout に残る。"""
    _fake_run(monkeypatch, 1, b"HTTP/2.0 404 Not Found\n\n{}\n")
    result = run_gh(("api", "-i", "orgs/example-org"))

    assert result.ok is False
    assert result.failure is None
    assert result.stdout.startswith("HTTP/2.0 404")


def test_run_gh_reports_that_authentication_is_required(monkeypatch):
    """gh が終了コード 4 で「認証が必要」を伝える場合を、応答なしと混ぜない。

    混ぜると、未ログインのまま収集したときに「応答を解釈できない」とだけ出て
    gh auth login の案内へたどり着けない。
    """
    _fake_run(monkeypatch, GH_EXIT_AUTH_REQUIRED, b"")
    result = run_gh(("api", "-i", "user"))

    assert result.ok is False
    assert result.failure is GhFailure.UNAUTHENTICATED


def test_run_gh_rejects_output_that_is_not_utf8(monkeypatch):
    _fake_run(monkeypatch, 0, b"\xff\xfe")

    assert run_gh(("api", "rate_limit")).failure is GhFailure.ERROR


# ------------------------------------------------------------ 応答の解釈


def test_parse_response_lowercases_header_names_and_stops_at_the_body():
    status, headers, body = _parse_response(
        "HTTP/2.0 403 Forbidden\n"
        "X-GitHub-SSO: required; url=https://example.invalid\n"
        "\n"
        "{\"Message\": \"本文はヘッダとして読まない\"}\n"
    )

    assert status == 403
    assert headers == {"x-github-sso": "required; url=https://example.invalid"}
    assert body == "{\"Message\": \"本文はヘッダとして読まない\"}\n"


def test_parse_response_joins_repeated_headers():
    _, headers, _ = _parse_response(
        "HTTP/1.1 200 OK\nLink: <a>\nLink: <b>\n\n{}\n")

    assert headers["link"] == "<a>, <b>"


def test_parse_response_keeps_blank_lines_inside_the_body():
    """本文を分けるのは最初の空行だけ（以降の空行は本文の一部）。"""
    _, _, body = _parse_response("HTTP/2.0 200 OK\n\n[\n\n  {}\n]\n")

    assert body == "[\n\n  {}\n]\n"


@pytest.mark.parametrize("stdout", ["", "なにかの出力\n", "HTTP/2.0 なにか\n\n"])
def test_parse_response_returns_no_status_for_unreadable_output(stdout):
    assert _parse_response(stdout) == (None, {}, "")


# ------------------------------------------------------------ scope の充足


@pytest.mark.parametrize(("granted", "missing"), [
    (("read:org", "repo"), ()),
    (("admin:org", "repo"), ()),          # 上位 scope は下位を含む
    (("write:org", "repo"), ()),
    (("repo",), ("read:org",)),
    (("read:org",), ("repo",)),
    ((), ("read:org", "repo")),
    (("public_repo", "read:org"), ("repo",)),   # private を読めない scope では足りない
])
def test_missing_scopes(granted, missing):
    assert missing_scopes(granted) == missing


# ------------------------------------------------------ repository の発見
#
# gh は一度も呼ばない。記録済みの応答を返す runner を差し込み、リクエストのパスと
# 解釈の両方を確かめる。

GH_ORG = "example-org"


def _repos_path(page: int) -> str:
    """発見が使うリクエストのパス（page だけが変わる）。"""
    return f"orgs/{GH_ORG}/repos?per_page={_REPOS_PER_PAGE}&sort=full_name&page={page}"


def _repo(name: str, archived: bool = False, fork: bool = False,
          is_template: bool = False) -> dict:
    """repository 一覧の1要素（発見が読む項目だけを持つ）。"""
    return {
        "name": name, "archived": archived, "fork": fork, "is_template": is_template,
    }


def _full_page(prefix: str) -> list[dict]:
    """ちょうど1ページ分の件数（次のページを読む条件）。"""
    return [_repo(f"{prefix}-{index:03d}") for index in range(_REPOS_PER_PAGE)]


def _response(body: object, status: int = 200,
              headers: tuple[tuple[str, str], ...] = ()) -> GhResult:
    """`gh api -i` の応答（ステータス行 + ヘッダ + 空行 + 本文）。

    body に文字列を渡すとその字句をそのまま本文にする（壊れた応答を表すため）。
    """
    text = body if isinstance(body, str) else json.dumps(body)
    lines = [f"HTTP/2.0 {status} -", *(f"{name}: {value}" for name, value in headers)]
    return GhResult(
        ok=200 <= status < 300, stdout="\n".join(lines) + f"\n\n{text}\n"
    )


class _FakeGh:
    """記録済みの応答を返す runner。呼び出しの並びを残す。

    キーはリクエストのパス全体なので、ページごとに別の応答を表せる。並びに無いパスを
    引かれたら KeyError で落ちる（想定外の問い合わせをテストが見逃さない）。
    """

    def __init__(self, responses: dict[str, GhResult]):
        self._responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        return self._responses[args[-1]]

    @property
    def paths(self) -> list[str]:
        return [call[-1] for call in self.calls]


class _AlwaysFull:
    """どのページでも満杯を返す runner（終わりの来ない応答）。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, args):
        self.calls += 1
        return _response(_full_page(f"page{self.calls}"))


def test_discovery_lists_the_repositories_of_one_page():
    """名前は API の表記のまま、並びは小文字比較の昇順。"""
    gh = _FakeGh({_repos_path(1): _response([
        _repo("Beta"), _repo("alpha"), _repo("gamma.tools"),
    ])})
    found = discover_repositories(GH_ORG, runner=gh)

    assert found == RepoDiscovery(
        github_org=GH_ORG, repos=("alpha", "Beta", "gamma.tools"), status=200
    )
    assert found.complete is True
    assert gh.calls == [("api", "-i", _repos_path(1))]


@pytest.mark.parametrize("flag", ["archived", "fork", "is_template"])
def test_discovery_excludes_archived_forks_and_templates(flag):
    gh = _FakeGh({_repos_path(1): _response([
        _repo("alpha"), _repo("beta", **{flag: True}),
    ])})
    found = discover_repositories(GH_ORG, runner=gh)

    assert found.repos == ("alpha",)
    assert found.excluded == 1
    assert found.complete is True


def test_discovery_counts_every_excluded_repository():
    gh = _FakeGh({_repos_path(1): _response([
        _repo("alpha"),
        _repo("beta", archived=True),
        _repo("gamma", fork=True),
        _repo("delta", is_template=True),
    ])})
    found = discover_repositories(GH_ORG, runner=gh)

    assert found.repos == ("alpha",)
    assert found.excluded == 3


def test_discovery_needs_no_allowlist_to_find_every_repository():
    """参照できる範囲は token の権限が決める（対象を手で書き並べる引数を持たない）。"""
    gh = _FakeGh({_repos_path(1): _response(_full_page("alpha")[:3])})

    assert discover_repositories(GH_ORG, runner=gh).repos == (
        "alpha-000", "alpha-001", "alpha-002",
    )


def test_discovery_reads_the_next_page_while_a_page_is_full():
    gh = _FakeGh({
        _repos_path(1): _response(_full_page("alpha")),
        _repos_path(2): _response([_repo("beta")]),
    })
    found = discover_repositories(GH_ORG, runner=gh)

    assert gh.paths == [_repos_path(1), _repos_path(2)]
    assert len(found.repos) == _REPOS_PER_PAGE + 1
    assert found.repos[0] == "alpha-000"
    assert found.repos[-1] == "beta"
    assert found.complete is True


def test_discovery_stops_at_an_empty_page_after_a_full_one():
    gh = _FakeGh({
        _repos_path(1): _response(_full_page("alpha")),
        _repos_path(2): _response([]),
    })
    found = discover_repositories(GH_ORG, runner=gh)

    assert gh.paths == [_repos_path(1), _repos_path(2)]
    assert len(found.repos) == _REPOS_PER_PAGE
    assert found.complete is True


def test_discovery_keeps_the_first_of_a_name_that_appears_twice():
    """列挙中の頁ズレで同じ repository が2ページに現れても1件に畳む（大小の違いも同じ）。"""
    first = _full_page("alpha")
    gh = _FakeGh({
        _repos_path(1): _response(first),
        _repos_path(2): _response([
            _repo(first[0]["name"].upper()),   # 1ページ目と同じ repository
            _repo("beta", archived=True),
            _repo("BETA"),                     # 除外した名前も再出現の判定に入れる
            _repo("gamma"),
        ]),
    })
    found = discover_repositories(GH_ORG, runner=gh)

    assert len(found.repos) == _REPOS_PER_PAGE + 1
    assert found.repos[-1] == "gamma"
    assert found.excluded == 1


def test_discovery_of_an_empty_organization_is_complete():
    gh = _FakeGh({_repos_path(1): _response([])})
    found = discover_repositories(GH_ORG, runner=gh)

    assert found == RepoDiscovery(github_org=GH_ORG, status=200)
    assert found.complete is True


@pytest.mark.parametrize("status", [403, 404, 500])
def test_discovery_reports_the_status_without_repositories(status):
    """権限か綴りかの区別は消費側が status で見る（一覧は返さない）。"""
    gh = _FakeGh({_repos_path(1): _response([_repo("alpha")], status=status)})
    found = discover_repositories(GH_ORG, runner=gh)

    assert found == RepoDiscovery(github_org=GH_ORG, status=status)
    assert found.complete is False


@pytest.mark.parametrize("failure", [
    GhFailure.NOT_FOUND, GhFailure.TIMEOUT, GhFailure.ERROR,
])
def test_discovery_reports_a_runner_failure(failure):
    gh = _FakeGh({_repos_path(1): GhResult(ok=False, failure=failure)})
    found = discover_repositories(GH_ORG, runner=gh)

    assert found == RepoDiscovery(github_org=GH_ORG, failure=failure)
    assert found.complete is False


@pytest.mark.parametrize(("second", "expected"), [
    (GhResult(ok=False, failure=GhFailure.TIMEOUT),
     RepoDiscovery(github_org=GH_ORG, failure=GhFailure.TIMEOUT)),
    (_response([_repo("beta")], status=403),
     RepoDiscovery(github_org=GH_ORG, status=403)),
])
def test_discovery_drops_the_first_page_when_a_later_page_fails(second, expected):
    """部分的な一覧は返さない（完全な一覧として集計されると参考指標が黙って小さく出る）。"""
    gh = _FakeGh({
        _repos_path(1): _response(_full_page("alpha")),
        _repos_path(2): second,
    })
    found = discover_repositories(GH_ORG, runner=gh)

    assert found == expected
    assert found.repos == ()
    assert found.excluded == 0


@pytest.mark.parametrize("body", [
    "",                                       # 本文が無い
    "[{\"name\": \"alpha\"",                  # JSON として読めない
    {"repositories": []},                     # list でない
    ["alpha"],                                # 要素が dict でない
    [{"archived": False, "fork": False, "is_template": False}],   # name が無い
    [{"name": 1, "archived": False, "fork": False, "is_template": False}],
    [_repo("")],                              # 空の名前
    [_repo(".")],                             # パスとして意味を持つ名前
    [_repo("..")],
    [_repo("owner/alpha")],                   # 名前ではなく full_name
    [_repo("a" * 101)],                       # 100 文字を超える
    [_repo("サンプル")],                        # ASCII 以外
    [{"name": "alpha", "fork": False, "is_template": False}],       # archived が無い
    [{"name": "alpha", "archived": False, "is_template": False}],   # fork が無い
    [{"name": "alpha", "archived": False, "fork": False}],          # is_template が無い
    [{"name": "alpha", "archived": "false", "fork": False, "is_template": False}],
    [{"name": "alpha", "archived": 0, "fork": False, "is_template": False}],
])
def test_discovery_rejects_a_body_it_cannot_read(body):
    """除外の判断材料が欠けた一覧はそのまま使わない（読めない要素だけを飛ばさない）。"""
    gh = _FakeGh({_repos_path(1): _response(body)})

    assert discover_repositories(GH_ORG, runner=gh) == RepoDiscovery(
        github_org=GH_ORG, failure=GhFailure.ERROR
    )


@pytest.mark.parametrize("stdout", ["", "なにかの出力\n", "HTTP/2.0 なにか\n\n[]\n"])
def test_discovery_rejects_output_that_is_not_a_response(stdout):
    gh = _FakeGh({_repos_path(1): GhResult(ok=True, stdout=stdout)})

    assert discover_repositories(GH_ORG, runner=gh) == RepoDiscovery(
        github_org=GH_ORG, failure=GhFailure.ERROR
    )


def test_discovery_stops_with_an_error_at_the_page_limit(monkeypatch):
    """上限まで満杯が続いたら、切り詰めた一覧ではなくエラーを返す。"""
    monkeypatch.setattr("seat_analyzer.github_collect._MAX_REPO_PAGES", 2)
    gh = _AlwaysFull()
    found = discover_repositories(GH_ORG, runner=gh)

    assert found == RepoDiscovery(github_org=GH_ORG, failure=GhFailure.ERROR)
    assert gh.calls == 2


def test_discovery_twice_gives_the_same_result():
    """同じ応答からは常に同じ結果（並びも set の反復順に依らない）。"""
    gh = _FakeGh({
        _repos_path(1): _response(_full_page("alpha")),
        _repos_path(2): _response([_repo("Beta"), _repo("gamma", fork=True)]),
    })

    assert discover_repositories(GH_ORG, runner=gh) == discover_repositories(
        GH_ORG, runner=gh
    )


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"github_org": " "}, "github_org には Organization 名が必要です"),
    ({"github_org": GH_ORG, "repos": ("alpha", "owner/beta"), "status": 200},
     "repository 名として読めない値です"),
    ({"github_org": GH_ORG, "repos": ("Alpha", "alpha"), "status": 200},
     "repos の repository 名が重複しています"),
    ({"github_org": GH_ORG, "repos": ("beta", "alpha"), "status": 200},
     "repos は小文字比較の昇順で並べてください"),
    ({"github_org": GH_ORG, "excluded": -1, "status": 200},
     "excluded には0以上の件数が必要です"),
    ({"github_org": GH_ORG, "status": "200"},
     "status には HTTP ステータスが必要です"),
    ({"github_org": GH_ORG, "repos": ("alpha",), "status": 404},
     "完全な一覧でない結果に repository を持たせられません"),
    ({"github_org": GH_ORG, "excluded": 1, "status": 200,
      "failure": GhFailure.ERROR},
     "完全な一覧でない結果に repository を持たせられません"),
])
def test_repo_discovery_validates_its_values(kwargs, message):
    """不変条件は構築時にも確かめる（発見を通さずに組み立てた結果も同じ形にする）。"""
    with pytest.raises(ValueError, match=message):
        RepoDiscovery(**kwargs)


def test_repo_discovery_rejects_wrong_types():
    with pytest.raises(TypeError, match="repos には tuple が必要です"):
        RepoDiscovery(github_org=GH_ORG, repos=["alpha"], status=200)
    with pytest.raises(TypeError, match="failure には GhFailure が必要です"):
        RepoDiscovery(github_org=GH_ORG, failure="error")


def test_repo_discovery_accepts_a_complete_listing():
    found = RepoDiscovery(
        github_org=GH_ORG, repos=("alpha", "beta.js", "gamma_1"), excluded=2, status=200
    )

    assert found.complete is True


# ---------------------------------------------------- 呼び出しが読み取りだけであること
#
# GitHub 上の情報（repository・PR・設定）を変更する呼び出しを作らないことを、全入口の
# 引数をまとめて検査して固定する。引数を組み立てるのはモジュール側の3箇所だけなので、
# 構造（`_AUTH_STATUS_ARGS` / `_read_args` / `_graphql_args`）とこの検査の両方で守る。

# 件数に許す字句（10進整数だけ）
_COUNT_RE = re.compile(r"[0-9]+")


def _value_after(argument: str, prefix: str, args: tuple[str, ...]) -> str:
    """`名前=値` の値を取り出す（名前が違う・値が空なら失格）。"""
    assert argument.startswith(prefix), (
        f"{prefix} が来る位置に {argument[:30]!r} があります: {args}"
    )
    value = argument[len(prefix):]
    assert value, f"{prefix} の値が空です: {args}"
    return value


def _assert_rest_read(args: tuple[str, ...]) -> None:
    """REST の呼び出しが `api [-i] <パス>` の形そのものであることを確かめる。

    `gh api` はフィールド（`-f` / `-F` / `--input` / `--raw-field` / `--field`）を1つでも
    渡すと既定のメソッドが POST に変わる。パス1つ以外の要素を認めないことで、
    `--method=DELETE` や `-XDELETE` のような結合形式も含めてまとめて締め出す。
    """
    assert len(args) >= 2, f"api にはパスが要ります: {args}"
    rest = args[2:] if args[1] == "-i" else args[1:]
    assert len(rest) == 1, f"api に渡してよいのはパス1つだけです: {args}"
    assert not rest[0].startswith("-"), f"パスがフラグに見えます: {args}"


def _assert_graphql_read(args: tuple[str, ...]) -> None:
    """GraphQL の呼び出しが、読み取り専用の検索1本の形であることを確かめる。

    GraphQL の入口は POST なのでフィールドは要るが、渡してよいのは query 文書・検索
    文字列・件数と、2ページ目以降の cursor だけ。並びと数まで固定して、ほかのフラグや
    2つ目の文書を混ぜられないようにする。
    """
    assert len(args) in (9, 11), f"GraphQL の引数の数が想定と違います: {args}"
    assert args[:4] == ("api", "-i", "graphql", "-f"), f"先頭が想定と違います: {args}"
    assert args[5] == "-f" and args[7] == "-F", f"フラグの並びが想定と違います: {args}"
    document = _value_after(args[4], "query=", args)
    _value_after(args[6], "search=", args)
    count = _value_after(args[8], "first=", args)
    assert _COUNT_RE.fullmatch(count), f"first は10進整数です: {count!r}"
    assert document.lstrip().startswith("query"), (
        f"GraphQL 文書は query で始めます: {document[:40]!r}"
    )
    assert "mutation" not in document, "GraphQL 文書に mutation を含めません"
    if len(args) == 11:
        assert args[9] == "-f", f"cursor は -f で渡します: {args}"
        _value_after(args[10], "after=", args)


def assert_read_only(calls: list[tuple[str, ...]]) -> None:
    """runner に届いた全呼び出しが GitHub を読むだけであることを確かめる。

    禁止するフラグを並べるのではなく、許される形そのものを固定する。禁止リストは
    `--method=DELETE`・`-XDELETE`・`-fname=value` のような結合形式を取りこぼす。
    """
    assert calls, "gh の呼び出しが1件も記録されていません"
    for raw in calls:
        args = tuple(raw)
        assert all(isinstance(arg, str) for arg in args), f"引数は文字列です: {args}"
        if args[:1] == ("auth",):
            # login / refresh / logout は認証情報を書き換える
            assert args == ("auth", "status"), f"auth は status 以外を呼びません: {args}"
            continue
        assert args[:1] == ("api",), f"呼べるのは api と auth だけです: {args}"
        if args[:3] == ("api", "-i", "graphql"):
            _assert_graphql_read(args)
        else:
            _assert_rest_read(args)


def _graphql_call(*extra: str, document: str = "query { viewer { login } }",
                  first: str = "100") -> tuple[str, ...]:
    """形の整った GraphQL の呼び出し（extra で余分な要素を足せる）。"""
    return (
        "api", "-i", "graphql", "-f", f"query={document}",
        "-f", "search=org:example-org is:pr", "-F", f"first={first}", *extra,
    )


@pytest.mark.parametrize("call", [
    ("api", "-X", "DELETE", f"orgs/{GH_ORG}"),          # メソッドの指定
    ("api", "--method", "DELETE", f"orgs/{GH_ORG}"),
    ("api", "--method=DELETE", f"orgs/{GH_ORG}"),       # = で結合した形
    ("api", "-XDELETE", f"orgs/{GH_ORG}"),              # pflag の短い結合形
    ("api", "-i", f"orgs/{GH_ORG}", "-f", "name=x"),    # field を渡すと POST になる
    ("api", "-i", f"orgs/{GH_ORG}", "-fname=x"),        # 同上（結合形）
    ("api", "-i", f"orgs/{GH_ORG}", "--input", "-"),
    ("api", "-i", f"orgs/{GH_ORG}", "--input=file"),
    ("api", "-i", f"orgs/{GH_ORG}", "--raw-field=name=x"),
    ("api", "-i", f"orgs/{GH_ORG}", "extra"),           # 余分な位置引数
    ("api", "-i", "--input=file"),                      # パスの位置にフラグ
    ("api",),
    _graphql_call(document="mutation { addComment { id } }"),
    # query で始まっていても mutation を含む文書は通さない（先頭の検査だけでは落ちない形）
    _graphql_call(document="query { viewer { login } } mutation { addComment { id } }"),
    _graphql_call(document="{ viewer { login } }"),     # query で始まらない
    _graphql_call("--jq", ".data"),                     # 余分なフラグ
    _graphql_call("-X", "POST"),
    _graphql_call("-f", "after=A", "-f", "after=B"),    # cursor が2つ
    _graphql_call(first="1e2"),                         # 件数が10進整数でない
    ("api", "-i", "graphql", "-f", "query=query { viewer { login } }"),   # 形が短い
    ("auth", "login"),
    ("auth", "refresh", "-s", "repo"),
    ("repo", "delete", f"{GH_ORG}/repo-a"),
])
def test_the_read_only_check_rejects_a_write(call):
    """検査そのものが書き込みを見逃さない（違反ゼロという結果が空振りでないこと）。"""
    with pytest.raises(AssertionError):
        assert_read_only([call])


def test_the_read_only_check_accepts_no_calls_as_a_failure():
    with pytest.raises(AssertionError, match="1件も記録されていません"):
        assert_read_only([])


def test_the_read_only_check_accepts_the_cursor_form():
    """2ページ目以降の実際の呼び出し（after 付きの 11 要素形）も読み取り専用として通る。

    収集の記録用 runner は 1 ページで終わるため、この形は `_graphql_args` の出力を直接流して
    確かめる（cursor の付け方を変える退行を、構造の検査が弾かないことを固定する）。
    """
    from seat_analyzer.github_collect import _graphql_args

    assert_read_only([_graphql_args("org:example-org is:pr is:merged", "Y3Vyc29yOjE=")])


# ------------------------------------------------------ PR の収集（共通の道具）

MONTH = "2026-08"

# MONTH の固定の窓（1–7 / 8–14 / 15–21 / 22–28 / 29–月末）
WINDOWS = (
    ("2026-08-01", "2026-08-07"),
    ("2026-08-08", "2026-08-14"),
    ("2026-08-15", "2026-08-21"),
    ("2026-08-22", "2026-08-28"),
    ("2026-08-29", "2026-08-31"),
)

# 全窓が「終端の翌日が過ぎた」状態になる最初の日（月末 + 2日）。既定でこの日を使う
TODAY = dt.date(2026, 9, 2)

# 1番目の窓の途中の日。この窓は完了にならないため毎回取り直される（upsert の検査に使う）。
# 2番目以降の窓はまだ始まっていないので問い合わせられない
TODAY_IN_WINDOW = dt.date(2026, 8, 5)


def _expression(start: str, end: str, org: str = GH_ORG) -> str:
    """収集が使う検索文字列。"""
    return f"org:{org} is:pr is:merged merged:{start}..{end} sort:created-asc"


def _window_expression(index: int) -> str:
    """MONTH の index 番目（1始まり）の窓の検索文字列。"""
    return _expression(*WINDOWS[index - 1])


def _day_expression(day: int) -> str:
    """MONTH の day 日だけを指す検索文字列（上限を超えた窓の割り直し）。"""
    return _expression(f"2026-08-{day:02d}", f"2026-08-{day:02d}")


def _window(index: int) -> DateWindow:
    """MONTH の index 番目（1始まり）の窓。"""
    start, end = WINDOWS[index - 1]
    return DateWindow(start=dt.date.fromisoformat(start), end=dt.date.fromisoformat(end))


def _node(number: int = 12, repository: str = "repo-a", login: str | None = "octocat",
          author_type: str = "User", created: str = "2026-08-01T00:00:00Z",
          merged: str = "2026-08-03T10:30:00Z", additions: int = 10,
          deletions: int = 2, draft: bool = False, **extra) -> dict:
    """検索結果の1要素（収集が読む項目だけを持つ）。

    login=None は author が null の PR（削除済みのアカウント）。extra で項目を足したり
    上書きしたりできる（禁止フィールドを混ぜた応答や、項目の型を崩した応答を作るため）。
    """
    node = {
        "number": number,
        "isDraft": draft,
        "createdAt": created,
        "mergedAt": merged,
        "additions": additions,
        "deletions": deletions,
        "repository": {"name": repository},
        "author": None if login is None else {"login": login, "__typename": author_type},
    }
    node.update(extra)
    return node


def _without(node: dict, key: str) -> dict:
    """node から項目を1つ取り除いた写し。"""
    return {name: value for name, value in node.items() if name != key}


def _search_response(nodes: list[dict], total: int | None = None,
                     has_next: bool = False, cursor: str | None = None,
                     status: int = 200,
                     headers: tuple[tuple[str, str], ...] = ()) -> GhResult:
    """検索の応答（total を省くと nodes の件数）。"""
    payload = {"data": {"search": {
        "issueCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }}}
    return _response(payload, status=status, headers=headers)


def _argument(args: tuple[str, ...], name: str) -> str | None:
    """gh の引数から `name=値` の値を取り出す（無ければ None）。"""
    prefix = f"{name}="
    return next((a[len(prefix):] for a in args if a.startswith(prefix)), None)


class _FakeSearch:
    """記録済みの検索応答を返す runner。キーは (検索文字列, cursor)。

    default を渡さない場合、並びに無い問い合わせは KeyError で落ちる（想定外の
    問い合わせをテストが見逃さない）。
    """

    def __init__(self, responses: dict, default: GhResult | None = None):
        self._responses = responses
        self._default = default
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        key = (_argument(args, "search"), _argument(args, "after"))
        if key not in self._responses and self._default is not None:
            return self._default
        return self._responses[key]

    @property
    def expressions(self) -> list[str]:
        """呼ばれた検索文字列の並び。"""
        return [_argument(call, "search") for call in self.calls]


def _windows_returning(nodes_by_window: dict[int, list[dict]]) -> _FakeSearch:
    """窓ごとに node を返す runner（指定しなかった窓は 0 件）。"""
    return _FakeSearch(
        {
            (_window_expression(index), None): _search_response(nodes)
            for index, nodes in nodes_by_window.items()
        },
        default=_search_response([]),
    )


class _Pauses:
    """再試行の待ちを記録するだけの sleep（テストは実時間を待たない）。"""

    def __init__(self):
        self.seconds: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def _collect(tmp_path: Path, gh, *, today: dt.date = TODAY, month: str = MONTH,
             org: str = GH_ORG, sleep: _Pauses | None = None) -> PrCollection:
    """収集を1回実行する（キャッシュは tmp_path 配下の組織ディレクトリ相当に置く）。

    待ちは常に差し替える（既定の `time.sleep` を通すと、再試行するケースでテストが
    実時間だけ止まる）。
    """
    return collect_merged_prs(
        org, month, tmp_path / PR_CACHE_DIRNAME,
        runner=gh, today=today, sleep=sleep or _Pauses(),
    )


def _cache_file(tmp_path: Path, month: str = MONTH) -> Path:
    return tmp_path / PR_CACHE_DIRNAME / f"prs-{month}.json"


def _payload(tmp_path: Path, month: str = MONTH) -> dict:
    """書き出したキャッシュの内容。"""
    return json.loads(_cache_file(tmp_path, month).read_text(encoding="utf-8"))


def _entries(tmp_path: Path, month: str = MONTH) -> dict:
    """書き出したキャッシュの prs（キー → 内容）。"""
    return _payload(tmp_path, month)["prs"]


# ---------------------------------------------------------------- 月の窓


@pytest.mark.parametrize(("month", "expected"), [
    ("2026-08", (("01", "07"), ("08", "14"), ("15", "21"), ("22", "28"), ("29", "31"))),
    ("2026-09", (("01", "07"), ("08", "14"), ("15", "21"), ("22", "28"), ("29", "30"))),
    ("2028-02", (("01", "07"), ("08", "14"), ("15", "21"), ("22", "28"), ("29", "29"))),
    ("2026-02", (("01", "07"), ("08", "14"), ("15", "21"), ("22", "28"))),
])
def test_month_windows_are_fixed_and_stay_inside_the_month(month, expected):
    """境界は月の長さで動かさない（29日が無い月だけ窓が1つ少なくなる）。"""
    assert month_windows(month) == tuple(
        DateWindow(
            start=dt.date.fromisoformat(f"{month}-{start}"),
            end=dt.date.fromisoformat(f"{month}-{end}"),
        )
        for start, end in expected
    )


@pytest.mark.parametrize("month", [
    "", "2026-8", "2026-13", "2026-00", "202608", "2026-08-01", "2026-０８", None, 202608,
])
def test_month_windows_rejects_an_unreadable_month(month):
    with pytest.raises(ValueError, match="month には YYYY-MM 形式が必要です"):
        month_windows(month)


def test_pr_cache_path_uses_the_month_in_the_file_name(tmp_path):
    assert pr_cache_path(tmp_path, MONTH) == tmp_path / "prs-2026-08.json"


@pytest.mark.parametrize("month", ["2026-13", "../2026-08", "2026-08/../.."])
def test_pr_cache_path_rejects_an_unreadable_month(tmp_path, month):
    """月はファイル名に入るため、別の場所を指せる字句を通さない。"""
    with pytest.raises(ValueError, match="month には YYYY-MM 形式が必要です"):
        pr_cache_path(tmp_path, month)


def test_date_window_days_splits_into_one_day_windows():
    assert _window(5).days == (
        DateWindow(start=dt.date(2026, 8, 29), end=dt.date(2026, 8, 29)),
        DateWindow(start=dt.date(2026, 8, 30), end=dt.date(2026, 8, 30)),
        DateWindow(start=dt.date(2026, 8, 31), end=dt.date(2026, 8, 31)),
    )


def test_date_window_validates_its_values():
    with pytest.raises(ValueError, match="start は end 以前にしてください"):
        DateWindow(start=dt.date(2026, 8, 2), end=dt.date(2026, 8, 1))
    with pytest.raises(TypeError, match="start には datetime.date が必要です"):
        DateWindow(start=dt.datetime(2026, 8, 1, 12, tzinfo=dt.UTC), end=dt.date(2026, 8, 2))


# ------------------------------------------------------------ 値オブジェクト


def _pr(**overrides) -> CachedPr:
    """保存する PR 1件（項目を上書きできる）。"""
    values = {
        "repository": "repo-a",
        "number": 12,
        "author_login": "octocat",
        "author_type": "User",
        "created_at": "2026-08-01T00:00:00Z",
        "merged_at": "2026-08-03T10:30:00Z",
        "additions": 10,
        "deletions": 2,
        "is_draft": False,
    }
    values.update(overrides)
    return CachedPr(**values)


def test_cached_pr_key_is_the_repository_and_number():
    assert _pr().key == "repo-a#12"
    assert _pr(repository="Repo-A", number=7).key == "Repo-A#7"


def test_cached_pr_keeps_the_timestamps_as_text():
    """日時は GitHub が返した字句のまま持つ（変換は指標計算の責務）。"""
    pr = _pr()
    assert pr.merged_at == "2026-08-03T10:30:00Z"
    assert pr.merged_day == dt.date(2026, 8, 3)


@pytest.mark.parametrize("login", [
    "octocat", "dependabot[bot]", "a" * 100, "jane-doe_acme", "旧アカウント",
])
def test_cached_pr_accepts_author_logins_that_are_not_user_logins(login):
    """Bot・Mannequin の login は人の login の規則に収まらない（弾くと保存できない）。"""
    assert _pr(author_login=login).author_login == login


@pytest.mark.parametrize(("overrides", "message"), [
    ({"repository": "owner/repo-a"}, "repository 名として読めない値です"),
    ({"repository": ".."}, "repository 名として読めない値です"),
    ({"repository": ""}, "repository 名として読めない値です"),
    ({"number": 0}, "number には1以上の整数が必要です"),
    ({"number": True}, "number には1以上の整数が必要です"),
    ({"number": "12"}, "number には1以上の整数が必要です"),
    ({"author_login": ""}, "author_login として読めない値です"),
    ({"author_login": "octo cat"}, "author_login として読めない値です"),
    ({"author_login": "octo\ncat"}, "author_login として読めない値です"),
    ({"author_login": "a" * 101}, "author_login として読めない値です"),
    ({"author_type": "User "}, "author_type として読めない値です"),
    ({"author_type": ""}, "author_type として読めない値です"),
    ({"created_at": "2026-08-03"}, "created_at には UTC の日時表記"),
    ({"merged_at": "2026-08-03T10:30:00+09:00"}, "merged_at には UTC の日時表記"),
    ({"merged_at": "2026-02-30T10:30:00Z"}, "merged_at には UTC の日時表記"),
    ({"merged_at": "2026-08-03T25:30:00Z"}, "merged_at には UTC の日時表記"),
    ({"created_at": "2026-08-04T00:00:00Z"}, "created_at は merged_at 以前"),
    ({"additions": -1}, "additions には0以上の件数が必要です"),
    ({"deletions": 1.5}, "deletions には0以上の件数が必要です"),
])
def test_cached_pr_validates_its_values(overrides, message):
    """不変条件は構築時にも確かめる（収集を通さずに組み立てた値も同じ形にする）。"""
    with pytest.raises(ValueError, match=message):
        _pr(**overrides)


def test_cached_pr_rejects_a_non_boolean_draft_flag():
    with pytest.raises(TypeError, match="is_draft には真偽値が必要です"):
        _pr(is_draft="false")


def test_cached_pr_allows_a_deleted_account():
    pr = _pr(author_login=None, author_type=None)
    assert (pr.author_login, pr.author_type) == (None, None)


@pytest.mark.parametrize("overrides", [
    {"author_login": None},   # 種別だけ分かっている
    {"author_type": None},    # login だけ分かっている
])
def test_cached_pr_requires_the_author_fields_to_agree(overrides):
    """login と種別は対で持つ（片方だけ不明という状態を作らない）。"""
    with pytest.raises(ValueError, match="両方に値を持たせるか、両方とも不明に"):
        _pr(**overrides)


def test_pr_cache_rejects_a_pr_merged_in_another_month():
    """1ファイル = 1組織 × 1月（別の月の PR が混ざったキャッシュを集計へ渡さない）。"""
    other = _pr(created_at="2026-07-01T00:00:00Z", merged_at="2026-07-31T00:00:00Z")
    with pytest.raises(ValueError, match="2026-08 以外の月に merge された PR"):
        PrCache(github_org=GH_ORG, month=MONTH, prs=(other,))


def test_pr_cache_is_complete_only_with_every_window():
    assert PrCache(github_org=GH_ORG, month=MONTH).complete is False
    partial = PrCache(
        github_org=GH_ORG, month=MONTH, complete_windows=(_window(1), _window(2)))
    assert partial.complete is False
    full = PrCache(
        github_org=GH_ORG, month=MONTH,
        complete_windows=tuple(_window(index) for index in range(1, 6)),
    )
    assert full.complete is True


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"github_org": "example/org", "month": MONTH},
     "github_org には Organization 名が必要です"),
    ({"github_org": GH_ORG, "month": "2026-13"}, "month には YYYY-MM 形式が必要です"),
    ({"github_org": GH_ORG, "month": MONTH,
      "prs": (_pr(repository="Repo-A"), _pr(repository="repo-a"))},
     "prs の PR が重複しています"),
    ({"github_org": GH_ORG, "month": MONTH,
      "prs": (_pr(number=20), _pr(number=3))},
     "prs は repository（小文字）と number の昇順で並べてください"),
    ({"github_org": GH_ORG, "month": MONTH,
      "complete_windows": (DateWindow(dt.date(2026, 8, 2), dt.date(2026, 8, 8)),)},
     "complete_windows に 2026-08 の窓でない区間があります"),
    ({"github_org": GH_ORG, "month": MONTH,
      "complete_windows": (_window(1), _window(1))},
     "complete_windows が重複しています"),
    ({"github_org": GH_ORG, "month": MONTH,
      "complete_windows": (_window(2), _window(1))},
     "complete_windows は開始日の昇順で並べてください"),
])
def test_pr_cache_validates_its_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        PrCache(**kwargs)


def test_pr_cache_rejects_wrong_types():
    with pytest.raises(TypeError, match="prs には CachedPr の tuple が必要です"):
        PrCache(github_org=GH_ORG, month=MONTH, prs=[_pr()])
    with pytest.raises(
        TypeError, match="complete_windows には DateWindow の tuple が必要です"
    ):
        PrCache(github_org=GH_ORG, month=MONTH, complete_windows=(("a", "b"),))


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"failure": GhFailure.ERROR},
     "中断した結果は failure と stopped の両方を持たせてください"),
    ({"stopped": _window(1)},
     "中断した結果は failure と stopped の両方を持たせてください"),
    ({"complete": True, "stopped": _window(1), "failure": GhFailure.ERROR},
     "全窓を読み切った結果に中断の理由は付きません"),
    ({"upserted": 2, "total": 1}, "upserted は total 以下です"),
    ({"status": -1}, "status には HTTP ステータスが必要です"),
])
def test_pr_collection_validates_its_values(tmp_path, kwargs, message):
    """中断の事実は理由と窓の両方で表す（片方だけが立つ結果を作らない）。"""
    with pytest.raises(ValueError, match=message):
        PrCollection(
            github_org=GH_ORG, month=MONTH, path=tmp_path / "prs-2026-08.json", **kwargs
        )


def test_pr_collection_rejects_a_path_that_is_not_a_path():
    with pytest.raises(TypeError, match="path には Path が必要です"):
        PrCollection(github_org=GH_ORG, month=MONTH, path="prs-2026-08.json")


# ------------------------------------------------------------ 禁止フィールド


def test_the_search_query_asks_for_no_content():
    """title・本文・files・commits・comments・reviews は問い合わせに書かない。

    保存側で落とすのではなく、そもそも受け取らない形にしておく（設計書 §20.3）。
    """
    for forbidden in (
        "title", "body", "bodyText", "files", "commits", "comments", "reviews",
    ):
        assert forbidden not in _PR_SEARCH_QUERY


def test_collect_saves_only_the_nine_fields(tmp_path):
    """応答に禁止フィールドが載っていても、保存する項目は9つだけ。"""
    gh = _windows_returning({1: [_node(
        title="機密のタイトル",
        body="機密の本文",
        bodyText="機密の本文",
        url="https://github.example.invalid/repo-a/pull/12",
        headRefName="feature/secret",
        files={"nodes": [{"path": "src/secret.py"}]},
        commits={"nodes": [{"message": "機密のコミットメッセージ"}]},
        comments={"nodes": [{"body": "機密のコメント"}]},
        reviews={"nodes": [{"body": "機密のレビュー"}]},
    )]})
    _collect(tmp_path, gh)

    entry = _entries(tmp_path)["repo-a#12"]
    assert set(entry) == set(_PR_ENTRY_KEYS)
    assert len(_PR_ENTRY_KEYS) == 9
    assert "機密" not in _cache_file(tmp_path).read_text(encoding="utf-8")


def test_collect_writes_the_expected_shape(tmp_path):
    gh = _windows_returning({1: [_node()]})
    result = _collect(tmp_path, gh)

    assert _payload(tmp_path) == {
        "schema": PR_CACHE_SCHEMA,
        "github_org": GH_ORG,
        "month": MONTH,
        "complete_windows": [list(window) for window in WINDOWS],
        "prs": {"repo-a#12": {
            "repository": "repo-a",
            "number": 12,
            "author_login": "octocat",
            "author_type": "User",
            "created_at": "2026-08-01T00:00:00Z",
            "merged_at": "2026-08-03T10:30:00Z",
            "additions": 10,
            "deletions": 2,
            "is_draft": False,
        }},
    }
    assert (result.total, result.upserted, result.complete) == (1, 1, True)
    assert result.path == _cache_file(tmp_path)


# ------------------------------------------------------------ 一意性と upsert


def test_collect_searches_every_window_in_order(tmp_path):
    gh = _windows_returning({})
    _collect(tmp_path, gh)

    assert gh.expressions == [_window_expression(index) for index in range(1, 6)]
    assert _argument(gh.calls[0], "first") == str(_SEARCH_PER_PAGE)
    assert _argument(gh.calls[0], "after") is None   # 1ページ目は after を渡さない


def test_collect_folds_a_pr_that_a_refetched_window_returns_again(tmp_path):
    """完了していない窓は毎回取り直されるが、同じ PR は1件にとどまる（upsert）。"""
    _collect(tmp_path, _windows_returning({1: [_node(number=12)]}),
             today=TODAY_IN_WINDOW)
    result = _collect(tmp_path, _windows_returning({
        1: [_node(number=12), _node(number=13)],
    }), today=TODAY_IN_WINDOW)

    assert sorted(_entries(tmp_path)) == ["repo-a#12", "repo-a#13"]
    assert (result.total, result.upserted) == (2, 1)


def test_collect_keeps_a_pr_that_a_later_window_no_longer_returns(tmp_path):
    """取り直しで応答から消えた PR は落とさない（upsert は消さない）。"""
    _collect(tmp_path, _windows_returning({1: [_node(number=12)]}),
             today=TODAY_IN_WINDOW)
    result = _collect(tmp_path, _windows_returning({1: []}), today=TODAY_IN_WINDOW)

    assert list(_entries(tmp_path)) == ["repo-a#12"]
    assert (result.total, result.upserted) == (1, 0)


def test_collect_folds_a_pr_that_appears_on_two_pages(tmp_path):
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response(
            [_node(number=12)], total=2, has_next=True, cursor="CURSOR-1"),
        (_window_expression(1), "CURSOR-1"): _search_response([_node(number=12)], total=2),
    }, default=_search_response([]))
    result = _collect(tmp_path, gh)

    assert list(_entries(tmp_path)) == ["repo-a#12"]
    assert result.total == 1


def test_collect_replaces_a_repository_name_that_changed_case(tmp_path):
    """repository 名の大小は同じ1つを指す（新しい表記に置き換える）。"""
    _collect(tmp_path, _windows_returning({1: [_node(repository="repo-a")]}),
             today=TODAY_IN_WINDOW)
    result = _collect(tmp_path, _windows_returning({1: [_node(repository="Repo-A")]}),
                      today=TODAY_IN_WINDOW)

    assert list(_entries(tmp_path)) == ["Repo-A#12"]
    assert (result.total, result.upserted) == (1, 1)


def test_collect_counts_only_what_changed(tmp_path):
    """upserted は今回追加または内容が変わった件数（変わらない PR は数えない）。"""
    _collect(tmp_path, _windows_returning({
        1: [_node(number=12), _node(number=13)],
    }), today=TODAY_IN_WINDOW)
    result = _collect(tmp_path, _windows_returning({
        1: [_node(number=12),                    # 変わらない
            _node(number=13, additions=99),      # 内容が変わった
            _node(number=14)],                   # 新規
    }), today=TODAY_IN_WINDOW)

    assert (result.total, result.upserted) == (3, 2)


def test_collect_updates_a_pr_whose_content_changed(tmp_path):
    _collect(tmp_path, _windows_returning({1: [_node(additions=10)]}),
             today=TODAY_IN_WINDOW)
    result = _collect(tmp_path, _windows_returning({1: [_node(additions=42)]}),
                      today=TODAY_IN_WINDOW)

    assert _entries(tmp_path)["repo-a#12"]["additions"] == 42
    assert result.upserted == 1


def test_collect_twice_writes_the_same_bytes(tmp_path):
    """同じ応答・同じ日付からは常に同じバイト列（set の反復順に依らせない）。"""
    nodes = {
        1: [_node(number=3, repository="repo-b"), _node(number=12)],
        3: [_node(number=7, repository="Repo-C", merged="2026-08-17T00:00:00Z")],
    }
    _collect(tmp_path, _windows_returning(nodes))
    first = _cache_file(tmp_path).read_bytes()
    _collect(tmp_path, _windows_returning(nodes))

    assert _cache_file(tmp_path).read_bytes() == first


def test_collect_keeps_a_deleted_account_as_unknown(tmp_path):
    """author が null（削除済みのアカウント）は login も種別も不明として保存する。"""
    gh = _windows_returning({1: [_node(login=None)]})
    _collect(tmp_path, gh)

    entry = _entries(tmp_path)["repo-a#12"]
    assert (entry["author_login"], entry["author_type"]) == (None, None)


def test_collect_keeps_the_author_type_it_was_given(tmp_path):
    """既知の種別の集合には閉じない（新しい種別で収集が止まらないようにする）。"""
    gh = _windows_returning({1: [_node(login="renovate[bot]", author_type="Bot")]})
    _collect(tmp_path, gh)

    entry = _entries(tmp_path)["repo-a#12"]
    assert (entry["author_login"], entry["author_type"]) == ("renovate[bot]", "Bot")


# ------------------------------------------------------------ 窓と完了の判定


def test_collect_does_not_query_a_future_window(tmp_path):
    """まだ始まっていない窓は問い合わせず、完了にもしない。"""
    gh = _windows_returning({})
    result = _collect(tmp_path, gh, today=dt.date(2026, 8, 10))

    assert gh.expressions == [_window_expression(1), _window_expression(2)]
    assert _payload(tmp_path)["complete_windows"] == [list(WINDOWS[0])]
    assert result.complete is False


def test_collect_refetches_the_windows_of_today_and_yesterday(tmp_path):
    """終端の翌日が過ぎていない窓は取得するが完了にしない（1日の猶予）。"""
    gh = _windows_returning({})
    result = _collect(tmp_path, gh, today=dt.date(2026, 8, 15))

    assert gh.expressions == [_window_expression(index) for index in (1, 2, 3)]
    # 2番目は昨日（08-14）を、3番目は今日（08-15）を含むのでどちらも完了にしない
    assert _payload(tmp_path)["complete_windows"] == [list(WINDOWS[0])]
    assert result.complete is False


def test_collect_completes_a_window_the_day_after_it_ends(tmp_path):
    gh = _windows_returning({})
    _collect(tmp_path, gh, today=dt.date(2026, 8, 16))

    assert _payload(tmp_path)["complete_windows"] == [list(WINDOWS[0]), list(WINDOWS[1])]


def test_collect_skips_the_windows_that_are_already_complete(tmp_path):
    """完了済みの窓は問い合わせない（応答を持たない runner でも通る）。"""
    _collect(tmp_path, _windows_returning({}), today=dt.date(2026, 8, 16))
    # 3番目以降の応答しか持たない runner（1・2番目を引いたら KeyError で落ちる）
    gh = _FakeSearch({
        (_window_expression(index), None): _search_response([]) for index in (3, 4, 5)
    })
    result = _collect(tmp_path, gh)

    assert gh.expressions == [_window_expression(index) for index in (3, 4, 5)]
    assert result.complete is True


# ------------------------------------------------------------ 中断と再開


def _rate_limited() -> GhResult:
    return _search_response([], status=429, headers=(("Retry-After", "60"),))


def test_collect_stops_at_the_window_it_could_not_read(tmp_path):
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response([_node(number=12)]),
        (_window_expression(2), None): _search_response(
            [_node(number=13, merged="2026-08-10T00:00:00Z")]),
        (_window_expression(3), None): _rate_limited(),
    })
    result = _collect(tmp_path, gh)

    # 4・5番目の窓は問い合わせない（直列・停止）
    assert gh.expressions == [_window_expression(index) for index in (1, 2, 3)]
    assert result.failure is GhFailure.RATE_LIMITED
    assert result.stopped == _window(3)
    assert result.status == 429
    assert result.complete is False
    # 読み切れた窓の結果と完了済みの記録は残す
    assert sorted(_entries(tmp_path)) == ["repo-a#12", "repo-a#13"]
    assert _payload(tmp_path)["complete_windows"] == [list(WINDOWS[0]), list(WINDOWS[1])]


def test_collect_resumes_from_the_window_it_stopped_at(tmp_path):
    _collect(tmp_path, _FakeSearch({
        (_window_expression(1), None): _search_response([_node(number=12)]),
        (_window_expression(2), None): _search_response([]),
        (_window_expression(3), None): _rate_limited(),
    }))
    # 1・2番目を引いたら KeyError で落ちる runner で、続きからだけ取ることを確かめる
    gh = _FakeSearch({
        (_window_expression(3), None): _search_response(
            [_node(number=20, merged="2026-08-16T00:00:00Z")]),
        (_window_expression(4), None): _search_response([]),
        (_window_expression(5), None): _search_response([]),
    })
    result = _collect(tmp_path, gh)

    assert gh.expressions == [_window_expression(index) for index in (3, 4, 5)]
    assert sorted(_entries(tmp_path)) == ["repo-a#12", "repo-a#20"]
    assert (result.total, result.upserted, result.complete) == (2, 1, True)
    assert result.failure is None


@pytest.mark.parametrize("response", [
    _search_response([], status=429),
    _search_response([], status=429, headers=(("Retry-After", "60"),)),
    _search_response([], status=403, headers=(("Retry-After", "60"),)),
    _search_response([], status=403, headers=(("X-RateLimit-Remaining", "0"),)),
    _response(
        {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}),
])
def test_collect_reports_a_rate_limit(tmp_path, response):
    """二次上限（429）・一次上限（403 + ヘッダ）・GraphQL の errors を同じ分類にする。"""
    gh = _FakeSearch({(_window_expression(1), None): response})
    result = _collect(tmp_path, gh)

    assert result.failure is GhFailure.RATE_LIMITED
    assert result.stopped == _window(1)


def test_collect_does_not_call_a_permission_problem_a_rate_limit(tmp_path):
    """ヘッダの無い 403 は権限の問題（「時間をおいて再実行」と案内しない）。"""
    gh = _FakeSearch({(_window_expression(1), None): _search_response([], status=403)})
    result = _collect(tmp_path, gh)

    assert result.failure is GhFailure.ERROR
    assert result.status == 403


# ---------------------------------------------- 一時的な失敗の再試行


class _Flaky:
    """同じページに対して、決めた応答を順に返す runner。

    キーは (検索文字列, cursor) で、値は応答の並び。並びを使い切ったら最後の応答を
    返し続ける（呼び出し回数は calls で数える）。並びに無い問い合わせは fallback。
    """

    def __init__(self, sequences: dict, fallback: GhResult | None = None):
        self._sequences = sequences
        self._fallback = fallback or _search_response([])
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        key = (_argument(args, "search"), _argument(args, "after"))
        sequence = self._sequences.get(key)
        if sequence is None:
            return self._fallback
        seen = sum(
            1 for call in self.calls[:-1]
            if (_argument(call, "search"), _argument(call, "after")) == key
        )
        return sequence[min(seen, len(sequence) - 1)]

    def attempts(self, expression: str, cursor: str | None = None) -> int:
        """その検索文字列（と cursor）を何回呼んだか。"""
        return sum(
            1 for call in self.calls
            if (_argument(call, "search"), _argument(call, "after"))
            == (expression, cursor)
        )


def test_the_attempt_count_matches_the_pauses():
    """試行回数は待ちの数から導く（両者が食い違わない）。"""
    assert _MAX_ATTEMPTS == 1 + len(_RETRY_PAUSES)
    assert _MAX_ATTEMPTS == 3
    assert _RETRY_PAUSES == (2.0, 5.0)


@pytest.mark.parametrize("status", _TRANSIENT_STATUSES)
def test_collect_retries_a_transient_status(tmp_path, status):
    """一時的な障害（5xx）は少し待って同じページを呼び直す。"""
    pauses = _Pauses()
    gh = _Flaky({(_window_expression(1), None): [
        _search_response([], status=status),
        _search_response([_node()]),
    ]})
    result = _collect(tmp_path, gh, sleep=pauses)

    assert gh.attempts(_window_expression(1)) == 2
    assert pauses.seconds == [_RETRY_PAUSES[0]]
    assert list(_entries(tmp_path)) == ["repo-a#12"]
    assert (result.failure, result.complete) == (None, True)


def test_collect_retries_a_timeout(tmp_path):
    """応答待ちの打ち切りも呼び直しで解消しうる。"""
    pauses = _Pauses()
    gh = _Flaky({(_window_expression(1), None): [
        GhResult(ok=False, failure=GhFailure.TIMEOUT),
        _search_response([_node()]),
    ]})
    result = _collect(tmp_path, gh, sleep=pauses)

    assert gh.attempts(_window_expression(1)) == 2
    assert pauses.seconds == [_RETRY_PAUSES[0]]
    assert result.failure is None


def test_collect_stops_after_the_last_attempt(tmp_path):
    """試行を使い切ったら、最後の失敗の種類でその窓を中断する。"""
    pauses = _Pauses()
    gh = _Flaky({(_window_expression(1), None): [_search_response([], status=502)]})
    result = _collect(tmp_path, gh, sleep=pauses)

    assert gh.attempts(_window_expression(1)) == _MAX_ATTEMPTS
    assert pauses.seconds == list(_RETRY_PAUSES)
    assert (result.failure, result.status) == (GhFailure.ERROR, 502)
    assert result.stopped == _window(1)
    assert _entries(tmp_path) == {}
    # 中断した窓より後は問い合わせない
    assert gh.attempts(_window_expression(2)) == 0


def test_collect_stops_after_repeated_timeouts(tmp_path):
    pauses = _Pauses()
    gh = _Flaky({(_window_expression(1), None): [
        GhResult(ok=False, failure=GhFailure.TIMEOUT)]})
    result = _collect(tmp_path, gh, sleep=pauses)

    assert gh.attempts(_window_expression(1)) == _MAX_ATTEMPTS
    assert pauses.seconds == list(_RETRY_PAUSES)
    assert (result.failure, result.status) == (GhFailure.TIMEOUT, None)


def test_collect_retries_a_later_page_too(tmp_path):
    """再試行は窓の1ページ目に限らない（cursor 付きのページも同じ引数で呼び直す）。"""
    pauses = _Pauses()
    gh = _Flaky({
        (_window_expression(1), None): [_search_response(
            [_node(number=1)], total=2, has_next=True, cursor="CURSOR-1")],
        (_window_expression(1), "CURSOR-1"): [
            _search_response([], status=503),
            _search_response([_node(number=2)], total=2),
        ],
    })
    result = _collect(tmp_path, gh, sleep=pauses)

    assert gh.attempts(_window_expression(1), "CURSOR-1") == 2
    assert pauses.seconds == [_RETRY_PAUSES[0]]
    assert sorted(_entries(tmp_path)) == ["repo-a#1", "repo-a#2"]
    assert result.failure is None


@pytest.mark.parametrize("response", [
    _search_response([], status=429),                                   # 利用上限
    _search_response([], status=403, headers=(("Retry-After", "60"),)),
    _search_response([], status=500),                                   # 5xx でも対象外
    _search_response([], status=404),
    _response("{"),                                                     # 解釈できない本文
    _search_response([_node(merged="2026-08-09T00:00:00Z")]),           # 読めない node
    _response({"errors": [{"type": "FORBIDDEN"}]}),
    GhResult(ok=False, failure=GhFailure.UNAUTHENTICATED),
    GhResult(ok=False, failure=GhFailure.NOT_FOUND),
    GhResult(ok=False, failure=GhFailure.ERROR),
    GhResult(ok=True, stdout="なにかの出力\n"),
])
def test_collect_does_not_retry_what_a_retry_cannot_fix(tmp_path, response):
    """利用上限・権限・解釈不能は呼び直さない（上限をさらに消費せず、即座に止める）。"""
    pauses = _Pauses()
    gh = _Flaky({(_window_expression(1), None): [response]})
    result = _collect(tmp_path, gh, sleep=pauses)

    assert gh.attempts(_window_expression(1)) == 1
    assert pauses.seconds == []
    assert result.failure is not None
    assert result.stopped == _window(1)


def test_a_retried_window_is_saved_like_one_that_succeeded_at_once(tmp_path):
    """再試行は保存物に影を落とさない（同じ応答なら同じバイト列）。"""
    straight = tmp_path / "straight"
    flaky = tmp_path / "flaky"
    for base in (straight, flaky):
        base.mkdir()
    _collect(straight, _windows_returning({1: [_node()]}))
    _collect(flaky, _Flaky({(_window_expression(1), None): [
        _search_response([], status=502),
        _search_response([], status=502),
        _search_response([_node()]),
    ]}))

    assert _cache_file(flaky).read_bytes() == _cache_file(straight).read_bytes()


# ------------------------------------------------------------ 件数の上限と分割


def test_collect_splits_a_window_that_exceeds_the_result_cap(tmp_path):
    """窓のヒット数が上限を超えたら日単位で取り直す（1ページ目の結果は捨てる）。"""
    responses = {
        (_window_expression(1), None): _search_response(
            [_node(number=99)], total=_SEARCH_RESULT_CAP + 1),
    }
    for day in range(1, 8):
        responses[(_day_expression(day), None)] = _search_response([])
    responses[(_day_expression(3), None)] = _search_response(
        [_node(number=7, merged="2026-08-03T00:00:00Z")])
    gh = _FakeSearch(responses, default=_search_response([]))
    result = _collect(tmp_path, gh)

    assert gh.expressions[:8] == [
        _window_expression(1), *(_day_expression(day) for day in range(1, 8))]
    assert list(_entries(tmp_path)) == ["repo-a#7"]
    assert result.total == 1
    assert result.complete is True


def test_collect_stops_when_a_single_day_exceeds_the_cap(tmp_path):
    """日単位でも上限を超える場合は、切り詰めた結果を返さず中断する。"""
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response(
            [], total=_SEARCH_RESULT_CAP + 1),
        (_day_expression(1), None): _search_response(
            [_node(number=1, merged="2026-08-01T12:00:00Z")]),
        (_day_expression(2), None): _search_response(
            [], total=_SEARCH_RESULT_CAP + 1),
    })
    result = _collect(tmp_path, gh)

    assert result.failure is GhFailure.TOO_MANY_RESULTS
    assert result.stopped == _window(1)
    # 取り切れなかった窓の PR は採らない（窓の粒度で all-or-nothing）
    assert (result.total, result.upserted) == (0, 0)
    assert _entries(tmp_path) == {}


def test_collect_does_not_split_a_window_at_the_cap(tmp_path):
    """ちょうど上限の件数は1クエリで辿れる（分割しない）。"""
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response(
            [_node()], total=_SEARCH_RESULT_CAP),
    }, default=_search_response([]))
    _collect(tmp_path, gh)

    assert _day_expression(1) not in gh.expressions
    assert list(_entries(tmp_path)) == ["repo-a#12"]


# ------------------------------------------------------------ ページング


def test_collect_follows_the_cursor_to_the_next_page(tmp_path):
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response(
            [_node(number=1)], total=2, has_next=True, cursor="CURSOR-1"),
        (_window_expression(1), "CURSOR-1"): _search_response(
            [_node(number=2)], total=2),
    }, default=_search_response([]))
    result = _collect(tmp_path, gh)

    assert _argument(gh.calls[1], "after") == "CURSOR-1"
    assert sorted(_entries(tmp_path)) == ["repo-a#1", "repo-a#2"]
    assert result.total == 2


class _AlwaysNext:
    """どのページでも「次がある」と答える runner（終わりの来ない応答）。"""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        self.calls.append(tuple(args))
        page = len(self.calls)
        return _search_response(
            [_node(number=page)], total=_SEARCH_RESULT_CAP,
            has_next=True, cursor=f"CURSOR-{page}",
        )


def test_collect_stops_with_an_error_at_the_page_limit(tmp_path):
    """上限までページが続いたら、切り詰めた結果ではなくエラーにする。"""
    gh = _AlwaysNext()
    result = _collect(tmp_path, gh)

    assert len(gh.calls) == _MAX_SEARCH_PAGES
    assert result.failure is GhFailure.ERROR
    assert result.stopped == _window(1)
    assert _entries(tmp_path) == {}


def test_collect_rejects_a_next_page_without_a_cursor(tmp_path):
    """次があると言いながら cursor が無い応答は読み切れたことにしない。"""
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response(
            [_node()], total=2, has_next=True, cursor=None),
    })
    result = _collect(tmp_path, gh)

    assert result.failure is GhFailure.ERROR
    assert _entries(tmp_path) == {}


# ------------------------------------------------------------ 解釈できない応答


@pytest.mark.parametrize(("response", "failure", "status"), [
    # node を1つでも読めなければページ全体を読めなかったものとして扱う
    (_search_response([_node(merged="2026-08-09T00:00:00Z")]), GhFailure.ERROR, 200),
    (_search_response([_without(_node(), "additions")]), GhFailure.ERROR, 200),
    (_search_response([_without(_node(), "mergedAt")]), GhFailure.ERROR, 200),
    (_search_response([_without(_node(), "author")]), GhFailure.ERROR, 200),
    (_search_response([_without(_node(), "repository")]), GhFailure.ERROR, 200),
    (_search_response([_node(repository="owner/repo-a")]), GhFailure.ERROR, 200),
    (_search_response([_node(repository="..")]), GhFailure.ERROR, 200),
    (_search_response([_node(number=0)]), GhFailure.ERROR, 200),
    (_search_response([_node(draft="false")]), GhFailure.ERROR, 200),
    (_search_response([_node(additions="10")]), GhFailure.ERROR, 200),
    (_search_response(
        [_node(created="2026-08-04T00:00:00Z", merged="2026-08-03T10:30:00Z")]),
     GhFailure.ERROR, 200),
    (_search_response([_node(merged="2026-08-03")]), GhFailure.ERROR, 200),
    (_search_response(["repo-a#12"]), GhFailure.ERROR, 200),
    (_search_response([_node(author={"__typename": "User"})]), GhFailure.ERROR, 200),
    (_search_response([_node(author={"login": "octocat"})]), GhFailure.ERROR, 200),
    (_search_response([_node(author="octocat")]), GhFailure.ERROR, 200),
    (_search_response([_node(login="octo cat")]), GhFailure.ERROR, 200),
    # search の枠が読めない応答
    (_response({"data": {}}), GhFailure.ERROR, 200),
    (_response({"data": {"search": []}}), GhFailure.ERROR, 200),
    (_response({"data": {"search": {
        "issueCount": "1", "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": []}}}), GhFailure.ERROR, 200),
    (_response({"data": {"search": {"issueCount": 0, "nodes": []}}}),
     GhFailure.ERROR, 200),
    (_response({"data": {"search": {
        "issueCount": 0, "pageInfo": {"hasNextPage": "no", "endCursor": None},
        "nodes": []}}}), GhFailure.ERROR, 200),
    (_response({"data": {"search": {
        "issueCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": 1},
        "nodes": []}}}), GhFailure.ERROR, 200),
    (_response({"data": {"search": {
        "issueCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": {}}}}), GhFailure.ERROR, 200),
    # 本文そのものが読めない応答
    (_response("{"), GhFailure.ERROR, 200),
    (_response("[]"), GhFailure.ERROR, 200),
    (_response(""), GhFailure.ERROR, 200),
    # GraphQL の errors（上限以外）と HTTP のエラー
    (_response({"errors": [{"type": "FORBIDDEN", "message": "禁止"}]}),
     GhFailure.ERROR, 200),
    (_response({"errors": "壊れた errors"}), GhFailure.ERROR, 200),
    (_search_response([], status=500), GhFailure.ERROR, 500),
    (_search_response([], status=404), GhFailure.ERROR, 404),
    (GhResult(ok=True, stdout="なにかの出力\n"), GhFailure.ERROR, None),
    (GhResult(ok=False, failure=GhFailure.TIMEOUT), GhFailure.TIMEOUT, None),
    (GhResult(ok=False, failure=GhFailure.NOT_FOUND), GhFailure.NOT_FOUND, None),
])
def test_collect_stops_when_it_cannot_read_the_response(
    tmp_path, response, failure, status
):
    """読めない応答は、その窓の PR を採らずに中断する（読めない要素だけを飛ばさない）。"""
    gh = _FakeSearch({(_window_expression(1), None): response})
    result = _collect(tmp_path, gh)

    assert (result.failure, result.status) == (failure, status)
    assert result.stopped == _window(1)
    assert _entries(tmp_path) == {}


def test_collect_drops_the_first_page_when_a_later_page_fails(tmp_path):
    """窓の粒度で all-or-nothing（読めたページだけを採らない）。"""
    gh = _FakeSearch({
        (_window_expression(1), None): _search_response(
            [_node(number=1)], total=2, has_next=True, cursor="CURSOR-1"),
        (_window_expression(1), "CURSOR-1"): _search_response([], status=500),
    })
    result = _collect(tmp_path, gh)

    assert (result.failure, result.status) == (GhFailure.ERROR, 500)
    assert _entries(tmp_path) == {}


# ------------------------------------------------------------ 引数の検証


@pytest.mark.parametrize("org", ["", "example/org", "example_org", None])
def test_collect_rejects_an_unreadable_organization_name(tmp_path, org):
    with pytest.raises(ValueError, match="github_org には Organization 名が必要です"):
        collect_merged_prs(org, MONTH, tmp_path, runner=_windows_returning({}))


def test_collect_rejects_an_unreadable_month(tmp_path):
    with pytest.raises(ValueError, match="month には YYYY-MM 形式が必要です"):
        collect_merged_prs(GH_ORG, "2026-13", tmp_path, runner=_windows_returning({}))


def test_collect_rejects_a_datetime_as_today(tmp_path):
    with pytest.raises(TypeError, match="today には datetime.date が必要です"):
        collect_merged_prs(
            GH_ORG, MONTH, tmp_path,
            runner=_windows_returning({}), today=dt.datetime(2026, 9, 2, 12, tzinfo=dt.UTC),
        )


# ------------------------------------------------------------ キャッシュの読み取り


def _write_payload(tmp_path: Path, payload: object, month: str = MONTH) -> Path:
    """キャッシュのファイルを直接書く（壊れた内容を表すため字句も渡せる）。"""
    path = _cache_file(tmp_path, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _valid_payload(**overrides) -> dict:
    """読める最小のキャッシュ（項目を上書きできる）。"""
    payload = {
        "schema": PR_CACHE_SCHEMA,
        "github_org": GH_ORG,
        "month": MONTH,
        "complete_windows": [list(WINDOWS[0])],
        "prs": {"repo-a#12": {
            "repository": "repo-a",
            "number": 12,
            "author_login": "octocat",
            "author_type": "User",
            "created_at": "2026-08-01T00:00:00Z",
            "merged_at": "2026-08-03T10:30:00Z",
            "additions": 10,
            "deletions": 2,
            "is_draft": False,
        }},
    }
    payload.update(overrides)
    return payload


def test_load_pr_cache_of_a_missing_file_is_empty(tmp_path):
    cache = load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)

    assert cache == PrCache(github_org=GH_ORG, month=MONTH)
    assert cache.complete is False


def test_load_pr_cache_reads_what_collect_wrote(tmp_path):
    _collect(tmp_path, _windows_returning({
        1: [_node(number=12), _node(number=3, repository="repo-b")],
    }))
    cache = load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)

    assert cache.complete is True
    assert [pr.key for pr in cache.prs] == ["repo-a#12", "repo-b#3"]
    assert cache.prs[0] == _pr()


def test_load_pr_cache_sorts_the_prs_numerically(tmp_path):
    """並びは number の数値順（キーの文字列順では 12 が 3 の前に来る）。"""
    payload = _valid_payload()
    payload["prs"]["repo-a#3"] = {**payload["prs"]["repo-a#12"], "number": 3}
    _write_payload(tmp_path, payload)
    cache = load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)

    assert [pr.key for pr in cache.prs] == ["repo-a#3", "repo-a#12"]


@pytest.mark.parametrize(("payload", "message"), [
    ("{", "JSON として読めません"),
    ("[]", "JSON のオブジェクトではありません"),
    (_valid_payload(schema=2), "対応していない形式です"),
    # Python では True == 1・1.0 == 1 なので、版の比較は int の1だけを受ける
    (_valid_payload(schema=True), "対応していない形式です"),
    (_valid_payload(schema=1.0), "対応していない形式です"),
    (_valid_payload(schema="1"), "対応していない形式です"),
    (_valid_payload(github_org="another-example"), "別の場所へ移してから再実行"),
    (_valid_payload(month="2026-07"), "別の月"),
    (_valid_payload(note="手で足したメモ"), "未知のキーがあります"),
    ({"schema": 1, "github_org": GH_ORG, "month": MONTH}, "項目がありません"),
    (_valid_payload(prs=[]), "prs がオブジェクトではありません"),
    (_valid_payload(complete_windows={}), "complete_windows が配列ではありません"),
    (_valid_payload(complete_windows=[["2026-08-02", "2026-08-08"]]),
     "2026-08 の窓でない区間があります"),
    (_valid_payload(complete_windows=[["2026-08-01"]]),
     "complete_windows の要素は"),
    # `date.fromisoformat` は区切りの無い形も数値も受けるので、字句で締める
    (_valid_payload(complete_windows=[[20260801, 20260807]]),
     "YYYY-MM-DD の文字列で書いてください"),
    (_valid_payload(complete_windows=[["20260801", "20260807"]]),
     "YYYY-MM-DD の文字列で書いてください"),
    (_valid_payload(complete_windows=[["2026-08-07", "2026-08-01"]]),
     "区間として読めません"),
    (_valid_payload(complete_windows=[list(WINDOWS[0]), list(WINDOWS[0])]),
     "complete_windows が重複しています"),
])
def test_load_pr_cache_rejects_a_file_it_cannot_read(tmp_path, payload, message):
    path = _write_payload(tmp_path, payload)
    with pytest.raises(ValueError, match=message) as excinfo:
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)
    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize(("prs", "message"), [
    ({"repo-a#12": {"title": "機密のタイトル"}}, "未知のキーがあります"),
    ({"repo-a#12": {"repository": "repo-a", "number": 12}}, "項目がありません"),
    ({"repo-a#12": "repo-a#12"}, "PR の内容ではありません"),
    ({"repo-b#12": None}, "PR の内容ではありません"),
])
def test_load_pr_cache_rejects_an_entry_it_cannot_read(tmp_path, prs, message):
    """未知のキーを拒むのは、禁止フィールドが混ざったキャッシュを黙って使わないため。"""
    _write_payload(tmp_path, _valid_payload(prs=prs))
    with pytest.raises(ValueError, match=message):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)


@pytest.mark.parametrize("overrides", [
    {"author_login": None},
    {"author_type": None},
])
def test_load_pr_cache_rejects_an_entry_with_a_half_known_author(tmp_path, overrides):
    payload = _valid_payload()
    payload["prs"]["repo-a#12"].update(overrides)
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError, match="両方に値を持たせるか、両方とも不明に"):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)


def test_load_pr_cache_rejects_a_pr_merged_in_another_month(tmp_path):
    payload = _valid_payload()
    payload["prs"]["repo-a#12"].update(
        created_at="2026-07-01T00:00:00Z", merged_at="2026-07-31T00:00:00Z")
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError, match="2026-08 以外の月に merge された PR"):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)


def test_load_pr_cache_rejects_an_entry_whose_values_are_wrong(tmp_path):
    payload = _valid_payload()
    payload["prs"]["repo-a#12"]["number"] = "12"
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError, match="prs.repo-a#12 を読めません"):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)


def test_load_pr_cache_rejects_a_key_that_does_not_match_its_entry(tmp_path):
    payload = _valid_payload()
    payload["prs"] = {"repo-b#99": payload["prs"]["repo-a#12"]}
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError, match="が内容の repository#number"):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)


def test_load_pr_cache_rejects_the_same_pr_under_two_keys(tmp_path):
    payload = _valid_payload()
    entry = payload["prs"]["repo-a#12"]
    payload["prs"]["Repo-A#12"] = {**entry, "repository": "Repo-A"}
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError, match="同じ PR が2つのキーで入っています"):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, GH_ORG)


@pytest.mark.parametrize("org", ["", "example/org", None])
def test_load_pr_cache_rejects_an_unreadable_organization_name(tmp_path, org):
    with pytest.raises(ValueError, match="github_org には Organization 名が必要です"):
        load_pr_cache(tmp_path / PR_CACHE_DIRNAME, MONTH, org)


def test_collect_writes_nothing_when_the_cache_is_broken(tmp_path):
    """壊れたキャッシュの上に upsert しない（取りこぼしを抱えて「完了」になりうる）。"""
    path = _write_payload(tmp_path, _valid_payload(schema=2))
    before = path.read_bytes()
    gh = _windows_returning({1: [_node()]})

    with pytest.raises(ValueError, match="対応していない形式です"):
        _collect(tmp_path, gh)

    assert path.read_bytes() == before
    assert gh.calls == []   # 読めない時点で止める（gh を呼ばない）


# ---------------------------------------------- 全入口の呼び出しが読み取りだけであること


class _RecordAll:
    """すべての呼び出しを記録し、最小限の正常応答を返す runner。"""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        if args[0] == "auth":
            return GhResult(ok=True)
        if args[:3] == ("api", "-i", "graphql"):
            return _search_response([])
        if args[-1] == "rate_limit":
            return GhResult(ok=True, stdout=json.dumps({"resources": {}}))
        if args[-1].startswith(f"orgs/{GH_ORG}/repos"):
            return _response([])
        return _response({})


def test_collect_does_not_create_a_missing_parent_directory(tmp_path):
    """キャッシュの親（組織ディレクトリ相当）が無ければ作らず、そこで止める。

    綴りの違う場所を渡されたとき、静かに新しい場所へ保存しないため。
    """
    missing = tmp_path / "no-such-org" / PR_CACHE_DIRNAME
    with pytest.raises(FileNotFoundError):
        collect_merged_prs(
            GH_ORG, MONTH, missing, runner=_windows_returning({}), today=TODAY
        )
    assert not (tmp_path / "no-such-org").exists()


def test_every_gh_call_only_reads(tmp_path):
    """probe・repository の発見・PR の収集のすべてが、読むだけの呼び出しを出す。

    GitHub 上の情報（repository・PR・設定）を変更しないことを、引数の形で固定する。
    """
    gh = _RecordAll()
    probe_github([GH_ORG], runner=gh)
    discover_repositories(GH_ORG, runner=gh)
    _collect(tmp_path, gh)

    # 3つの入口がどれも記録されている（検査が空振りしていない）
    assert ("auth", "status") in gh.calls
    assert any(call[-1].startswith(f"orgs/{GH_ORG}/repos") for call in gh.calls)
    assert any(call[:3] == ("api", "-i", "graphql") for call in gh.calls)
    assert_read_only(gh.calls)
