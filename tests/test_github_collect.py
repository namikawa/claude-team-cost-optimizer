"""GitHub 対応表（input/<組織名>/github-members.csv）のロードのテスト。

取り違えに直結するものを止めること（必須カラムの欠落・email の欠落と重複・login の
重複）と、読めない値で分析を止めないこと（空欄・字句として読めない login を未対応へ
倒して警告する）の両方を固定する。

読み取りだけのモジュールなので、同じ入力からは常に同じ結果と同じ並びになることまで
見る（entries は行順・警告も行順・未対応の一覧は昇順）。
"""

import copy
from pathlib import Path

import pytest

from seat_analyzer.github_collect import (
    GITHUB_MEMBERS_FILENAME,
    GithubMemberLink,
    GithubMembers,
    load_github_members,
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
