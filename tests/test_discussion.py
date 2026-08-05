"""考察の自動執筆（discuss / analyze --with-discussion）のテスト。

実際の Claude CLI は呼ばない。ヘッドレス呼び出しは runner 差し替えか
subprocess.run の monkeypatch で置き換える。
"""

import subprocess
from pathlib import Path

import pytest

from seat_analyzer import discussion, report
from seat_analyzer.cli import main
from seat_analyzer.config import load_config

from .conftest import REPO_ROOT, spend_row

CONFIG = str(REPO_ROOT / "config.yaml")
BODY = "### 変更推奨の妥当性\n\n" + "対象組織の需要は妥当な範囲に収まっている。" * 12


@pytest.fixture
def two_orgs(make_input):
    """org-a（対象）と org-b（他組織）の2組織構成。"""
    input_dir = make_input(
        {"2026-05": [spend_row("alice.morgan@x.jp", 10.0)],
         "2026-06": [spend_row("alice.morgan@x.jp", 12.0)]},
        members=["alice.morgan@x.jp,Premium"], org="org-a",
    )
    make_input(
        {"2026-06": [spend_row("bernard.holloway@y.jp", 300.0, net=250.0)]},
        members=["bernard.holloway@y.jp,Standard"], org="org-b",
    )
    return input_dir


def _analyze(input_dir: Path, tmp_path: Path, *extra: str) -> Path:
    output_dir = tmp_path / "reports"
    rc = main(["analyze", "--config", CONFIG, "--input-dir", str(input_dir),
               "--output-dir", str(output_dir), "--month", "2026-06", *extra])
    assert rc == 0
    return output_dir


# ---------------------------------------------------------------- report.py 側のヘルパ


def test_document_body_and_discussion_body(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    md = path.read_text(encoding="utf-8")

    # 生成直後は未記入プレースホルダなので考察は無い扱い
    assert report.discussion_body(md) is None
    body = report.document_body(md)
    assert "## 考察" not in body
    assert "## データ検証・警告" in body

    report.write_discussion(path, BODY)
    md2 = path.read_text(encoding="utf-8")
    assert report.discussion_body(md2) == BODY.strip()
    # 本文側は変わらない
    assert report.document_body(md2) == body


def test_written_discussion_survives_reanalysis(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    report.write_discussion(path, BODY)
    _analyze(two_orgs, tmp_path)  # 同じ output_dir へ再生成
    assert report.discussion_body(path.read_text(encoding="utf-8")) == BODY.strip()


def test_write_discussion_requires_section(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("# タイトル\n\n本文\n", encoding="utf-8")
    with pytest.raises(ValueError, match="考察"):
        report.write_discussion(path, BODY)


# ---------------------------------------------------------------- 混入チェック


def _terms(*specs: tuple[str, str]) -> tuple[discussion.Term, ...]:
    return tuple(discussion.Term(text, kind) for text, kind in specs)


def _texts(terms) -> set[str]:
    return {t.text for t in terms}


def _hit_terms(hits) -> tuple[str, ...]:
    return tuple(h.term for h in hits)


def test_forbidden_terms_excludes_target_org(two_orgs, tmp_path):
    cfg = load_config(CONFIG)
    terms = discussion.forbidden_terms(
        input_dir=two_orgs, output_dir=tmp_path / "reports", target_org="org-a", cfg=cfg)
    texts = _texts(terms)
    assert "org-b" in texts
    assert "bernard.holloway@y.jp" in texts
    assert "bernard" in texts and "holloway" in texts
    # 組織名は kind=org として常時禁止の扱いになる
    assert discussion.Term("org-b", "org") in terms
    # 対象組織自身の語は禁止語に入らない
    assert "org-a" not in texts
    assert not any("alice" in t for t in texts)


def test_forbidden_terms_splits_short_name_segments(make_input, tmp_path):
    """ドット・アンダースコア区切りの短い姓名も禁止語に含める（実運用の命名に合わせる）。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-a")
    make_input({"2026-06": [spend_row("taro.sato@y.jp", 10.0)]},
               members=["taro.sato@y.jp,Premium", "hana_kato@y.jp,Standard"], org="org-b")
    texts = _texts(discussion.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org="org-a", cfg=load_config(CONFIG)))
    assert {"taro", "sato", "hana", "kato"} <= texts


def test_find_leaks_flags_only_terms_absent_from_source():
    terms = _terms(("org-b", "org"), ("bernard.holloway@y.jp", "address"),
                   ("holloway", "person"), ("架空推進3部", "group"))
    source = "org-a のユーザ alice.morgan@x.jp は Premium。"

    assert _hit_terms(discussion.find_leaks(
        "holloway さんは Standard で足りている。", terms, source=source)) == ("holloway",)
    assert _hit_terms(discussion.find_leaks(
        "架空推進3部の削減余地は小さい。", terms, source=source)) == ("架空推進3部",)
    assert discussion.find_leaks("alice.morgan の需要は妥当。", terms, source=source) == ()


def test_find_leaks_ignores_terms_present_in_source():
    # 対象組織の資料に現れる語は、出力に出てきても混入ではない
    terms = _terms(("holloway", "person"))
    source = "holloway@x.jp は対象組織のユーザ。"
    assert discussion.find_leaks("holloway は Premium 継続。", terms, source=source) == ()


def test_find_leaks_always_forbids_other_org_names():
    """組織名は対象組織の資料に現れても混入として扱う（資料側の混入を許可根拠にしない）。"""
    source = "備考: org-b から異動。"
    assert _hit_terms(discussion.find_leaks(
        "org-b と比べると小さい。", _terms(("org-b", "org")), source=source)) == ("org-b",)
    # 人名・部署名は従来どおり資料に現れれば除外する
    assert discussion.find_leaks(
        "holloway は継続。", _terms(("holloway", "person")), source=source + " holloway@x.jp") == ()


def test_find_leaks_respects_word_boundaries():
    # 英単語の一部として現れる出現は拾わない（detail の中の etai 等の誤検出防止）
    assert discussion.find_leaks("detail を確認する。", _terms(("etai", "person")), source="") == ()
    # メールのローカル部・ドメインの構成要素として現れる出現は拾う
    assert _hit_terms(discussion.find_leaks(
        "bernard.holloway", _terms(("bernard", "person")), source="")) == ("bernard",)
    assert _hit_terms(discussion.find_leaks(
        "holloway@y.jp", _terms(("holloway", "person")), source="")) == ("holloway",)
    # 日本語が隣接する出現は拾う
    assert _hit_terms(discussion.find_leaks(
        "org-b組織では", _terms(("org-b", "org")), source="")) == ("org-b",)


def test_find_leaks_short_japanese_terms_need_non_kanji_boundary():
    """短い日本語の語は漢字・カタカナの連結を語の一部とみなす（一般語の誤検出を防ぐ）。"""
    short = _terms(("開発部", "group"), ("人事", "group"))
    # 無関係な複合語の一部としての出現は拾わない
    assert discussion.find_leaks("製品開発部門の需要が大きい。", short, source="") == ()
    assert discussion.find_leaks("人事評価制度を見直す。", short, source="") == ()
    # 助詞・記号が続く出現は拾う
    assert _hit_terms(discussion.find_leaks("開発部の削減余地は小さい。", short, source="")) \
        == ("開発部",)
    assert _hit_terms(discussion.find_leaks("人事、総務の2部署。", short, source="")) == ("人事",)


def test_find_leaks_long_japanese_terms_match_inside_compounds():
    """長い日本語の語は固有性が高いため、複合語に埋め込まれても検出する。"""
    long_term = _terms(("架空推進3部", "group"))
    assert _hit_terms(discussion.find_leaks(
        "架空推進3部第2チームの需要。", long_term, source="")) == ("架空推進3部",)


def test_find_leaks_reports_context_and_kind():
    hits = discussion.find_leaks(
        "前段の説明。製品開発部の削減余地は小さい。後段の説明。",
        _terms(("製品開発部", "group")), source="")
    assert len(hits) == 1
    assert hits[0].kind == "group"
    assert "製品開発部の削減余地" in hits[0].context


def test_find_leaks_allow_overrides_detection():
    terms = _terms(("開発部", "group"), ("holloway", "person"))
    text = "開発部と holloway について。"
    assert _hit_terms(discussion.find_leaks(text, terms, source="")) == ("holloway", "開発部")
    # 人が確認して無害と判断した語は許可できる（大文字小文字は問わない）
    assert _hit_terms(discussion.find_leaks(
        text, terms, source="", allow=("開発部",))) == ("holloway",)
    assert discussion.find_leaks(text, terms, source="", allow=("開発部", "HOLLOWAY")) == ()


def test_allow_cannot_override_org_names_or_addresses():
    """組織名とメールアドレスは --allow-term の対象外（誤検出の余地が実質なく影響が大きい）。"""
    terms = _terms(("org-b", "org"), ("x@y.jp", "address"), ("y.jp", "domain"))
    text = "org-b の x@y.jp（y.jp）について。"
    hits = discussion.find_leaks(
        text, terms, source="", allow=("org-b", "x@y.jp", "y.jp"))
    assert _hit_terms(hits) == ("org-b", "x@y.jp")
    assert all(h.allowable is False for h in hits)


def test_find_leaks_org_names_use_aggressive_boundary():
    """短い日本語の緩い規則を組織名に適用すると取りこぼす（影響が最大の種類なので例外扱い）。"""
    assert _hit_terms(discussion.find_leaks(
        "東京支社の利用状況。", _terms(("東京", "org")), source="")) == ("東京",)
    # 同じ長さでも部署名なら複合語の一部としては拾わない
    assert discussion.find_leaks("東京支社の利用状況。", _terms(("東京", "group")), source="") == ()


def test_group_names_from_members_info(two_orgs, tmp_path):
    (two_orgs / "org-b" / "members-info.csv").write_text(
        "email,部署,チーム,職種\nbernard.holloway@y.jp,架空推進3部,Nebula-AI,エンジニア\n",
        encoding="utf-8")
    cfg = load_config(CONFIG)
    texts = _texts(discussion.forbidden_terms(
        input_dir=two_orgs, output_dir=tmp_path / "reports", target_org="org-a", cfg=cfg))
    assert "架空推進3部" in texts and "Nebula-AI" in texts
    # 職種は一般語のため禁止語に含めない
    assert "エンジニア" not in texts


def test_org_named_like_input_subdir_is_still_collected(make_input, tmp_path):
    """入力サブディレクトリと同名の組織（組織名として許される）も禁止語を集める。

    名前で除外すると、その組織のユーザ名・部署名が丸ごと照合対象から抜ける。
    """
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-a")
    make_input({"2026-06": [spend_row("bernard.holloway@y.jp", 10.0)]},
               members=["bernard.holloway@y.jp,Premium"], org="members")
    texts = _texts(discussion.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org="org-a", cfg=load_config(CONFIG)))
    assert "members" in texts and "holloway" in texts


def test_forbidden_terms_fails_closed_on_unreadable_input(two_orgs, tmp_path, monkeypatch):
    """収集元が読めない場合、不完全な禁止語集合で通さず中止する。"""
    def boom(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(discussion.DiscussionError, match="混入チェックを保証できません"):
        discussion.forbidden_terms(
            input_dir=two_orgs, output_dir=tmp_path / "reports",
            target_org="org-a", cfg=load_config(CONFIG))


@pytest.mark.parametrize("target", ["input", "org", "subdir"])
def test_forbidden_terms_fails_closed_on_unlistable_dir(two_orgs, tmp_path, target):
    """列挙できないディレクトリも中止扱いにする。

    pathlib の is_dir()/glob() は権限エラーを False や空リストとして飲み込むため、
    それに頼ると「他組織が読めなかった」ことが検出漏れと区別できない。
    """
    victim = {
        "input": two_orgs,
        "org": two_orgs / "org-b",
        "subdir": two_orgs / "org-b" / "spend",
    }[target]
    victim.chmod(0o000)
    try:
        with pytest.raises(discussion.DiscussionError, match="混入チェックを保証できません"):
            discussion.forbidden_terms(
                input_dir=two_orgs, output_dir=tmp_path / "reports",
                target_org="org-a", cfg=load_config(CONFIG))
    finally:
        victim.chmod(0o755)


def test_single_character_org_name_is_detected(make_input, tmp_path):
    """1文字の組織名も禁止語に残す（長さ下限は派生語の誤検出対策で、識別子には適用しない）。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-a")
    make_input({"2026-06": [spend_row("z@y.jp", 10.0)]}, members=["z@y.jp,Premium"], org="A")
    terms = discussion.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org="org-a", cfg=load_config(CONFIG))
    assert discussion.Term("A", "org") in terms
    assert _hit_terms(discussion.find_leaks("A社の状況は…", terms, source="")) == ("A",)


def test_duplicate_text_merges_to_stricter_kind():
    """同一文字列が複数 kind にあるとき、厳しい側（許可できない側）に寄せる。"""
    terms = _terms(("acme", "domain"), ("acme", "org"))
    hits = discussion.find_leaks("acme の状況。", terms, source="")
    assert [(h.kind, h.allowable) for h in hits] == [("org", False)]
    # 許可指定を付けても org として残る（案内と実挙動が一致する）
    assert _hit_terms(discussion.find_leaks(
        "acme の状況。", terms, source="", allow=("acme",))) == ("acme",)


def test_forbidden_terms_fails_closed_on_broken_members_info(two_orgs, tmp_path):
    (two_orgs / "org-b" / "members-info.csv").write_text(
        "部署,チーム\n架空推進3部,Nebula-AI\n", encoding="utf-8")  # email 列が無い
    with pytest.raises(discussion.DiscussionError, match="混入チェックを保証できません"):
        discussion.forbidden_terms(
            input_dir=two_orgs, output_dir=tmp_path / "reports",
            target_org="org-a", cfg=load_config(CONFIG))


def test_legacy_layout_has_no_forbidden_terms(make_input, tmp_path):
    """旧レイアウト（input/spend 直下）では他組織が存在しないため禁止語は空。

    入力サブディレクトリ名を組織名と誤認すると、考察が「spend」「members」に触れる
    だけでブロックされ、自組織のユーザ名まで禁止語に入ってしまう。
    """
    input_dir = make_input({"2026-06": [spend_row("alice.morgan@x.jp", 10.0)]},
                           members=["alice.morgan@x.jp,Premium"])
    terms = discussion.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org=None, cfg=load_config(CONFIG))
    assert terms == ()


# ---------------------------------------------------------------- generate()


def _runner(*bodies: str):
    """呼び出しごとに bodies を順に返す偽 runner。プロンプトも記録する。"""
    calls: list[str] = []

    def run(prompt: str, s: dict) -> str:
        calls.append(prompt)
        return bodies[min(len(calls) - 1, len(bodies) - 1)]

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _generate(two_orgs: Path, out: Path, runner, **kw):
    return discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG), runner=runner, **kw)


def test_generate_writes_discussion(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "written"
    assert outcome.attempts == 1
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) == BODY.strip()
    # プロンプトには対象組織の資料と執筆の原則が入り、他組織の語は入らない
    prompt = runner.calls[0]
    assert "alice.morgan" in prompt and "org-a" in prompt
    assert "bernard" not in prompt and "org-b" not in prompt
    assert "執筆の原則" in prompt and "変更推奨の妥当性" in prompt


def test_generate_keeps_existing_unless_forced(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    report.write_discussion(out / "org-a" / "2026-06" / "report.md", BODY)

    runner = _runner("### 別の考察\n\n" + "上書きされた本文。" * 20)
    kept = _generate(two_orgs, out, runner)
    assert kept.status == "kept"
    assert not runner.calls  # Claude を呼ばない
    assert report.discussion_body(kept.path.read_text(encoding="utf-8")) == BODY.strip()

    forced = _generate(two_orgs, out, runner, force=True)
    assert forced.status == "written"
    assert "上書きされた本文" in forced.path.read_text(encoding="utf-8")


def test_generate_retries_once_then_accepts(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    runner = _runner(leaky, BODY)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "written"
    assert outcome.attempts == 2
    # 2回目のプロンプトには差し戻し指示と検出語が入る
    assert "差し戻し" in runner.calls[1]
    assert "bernard" in runner.calls[1]
    assert "bernard" not in outcome.path.read_text(encoding="utf-8")


def test_generate_blocks_persistent_leak(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べると小さい。" * 6
    runner = _runner(leaky, leaky)
    outcome = _generate(two_orgs, out, runner)

    assert outcome.status == "blocked"
    assert outcome.attempts == 2
    leaked = _hit_terms(outcome.leaks)
    assert "bernard.holloway" in leaked and "holloway" in leaked
    # 検出語には一致箇所の文脈が付く（誤検出かどうかを人が判断するため）
    assert all(h.context for h in outcome.leaks)
    # 書き込まれていない（プレースホルダのまま）
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) is None


def test_generate_allow_term_lets_verified_text_through(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "holloway 相当の水準にある。" * 10
    outcome = _generate(two_orgs, out, _runner(leaky),
                        allow=("holloway", "bernard.holloway"))
    assert outcome.status == "written"


def test_generate_dry_run_does_not_call_claude(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner, dry_run=True)

    assert outcome.status == "dry-run"
    assert not runner.calls
    assert "執筆の原則" in outcome.prompt
    assert report.discussion_body(outcome.path.read_text(encoding="utf-8")) is None


def test_generate_dry_run_shows_prompt_even_when_already_written(two_orgs, tmp_path):
    """--dry-run はプロンプト確認用なので、記入済みでもプロンプトを返す。"""
    out = _analyze(two_orgs, tmp_path)
    report.write_discussion(out / "org-a" / "2026-06" / "report.md", BODY)
    outcome = _generate(two_orgs, out, _runner(BODY), dry_run=True)
    assert outcome.status == "dry-run"
    assert "執筆の原則" in outcome.prompt


def test_generate_does_not_overwrite_discussion_written_during_generation(two_orgs, tmp_path):
    """生成中に人が考察を書いた場合は上書きしない（判定と書き込みの間の競合）。"""
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    handwritten = "### 手書きの考察\n\n" + "人が書いた内容。" * 20

    def writes_meanwhile(prompt: str, s: dict) -> str:
        report.write_discussion(path, handwritten)
        return BODY

    notices: list[str] = []
    outcome = discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        runner=writes_meanwhile, notify=notices.append)

    assert outcome.status == "kept"
    assert report.discussion_body(path.read_text(encoding="utf-8")) == handwritten.strip()
    assert any("生成中" in n for n in notices)


def test_generate_retries_transient_failure(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    calls: list[str] = []
    notices: list[str] = []

    def flaky(prompt: str, s: dict) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            raise discussion.DiscussionError("API エラー: 529 Overloaded", transient=True)
        return BODY

    cfg = load_config(CONFIG)
    cfg["discussion"] = dict(cfg["discussion"], retry_wait_seconds=0)
    outcome = discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=cfg, runner=flaky, notify=notices.append)

    assert outcome.status == "written"
    assert len(calls) == 2
    assert any("再試行" in n for n in notices)


def test_generate_does_not_retry_permanent_failure(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    calls: list[str] = []

    def broken(prompt: str, s: dict) -> str:
        calls.append(prompt)
        raise discussion.DiscussionError("claude が見つかりません")

    with pytest.raises(discussion.DiscussionError, match="見つかりません"):
        _generate(two_orgs, out, broken)
    assert len(calls) == 1


def test_generate_requires_report(two_orgs, tmp_path):
    out = tmp_path / "reports"
    with pytest.raises(discussion.DiscussionError, match="analyze"):
        _generate(two_orgs, out, _runner(BODY))


def test_preview_materials_use_preview_document(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path, "--preview", "--days", "10")
    runner = _runner(BODY)
    outcome = _generate(two_orgs, out, runner, preview=True)

    assert outcome.status == "written"
    assert outcome.path.name == "preview.md"
    assert "速報" in runner.calls[0]


def test_previous_month_discussion_is_included(two_orgs, tmp_path):
    out = tmp_path / "reports"
    main(["analyze", "--config", CONFIG, "--input-dir", str(two_orgs),
          "--output-dir", str(out), "--month", "2026-05", "--org", "org-a"])
    prev = "### 前月の所見\n\n" + "前月は Premium 継続と判断した。" * 10
    report.write_discussion(out / "org-a" / "2026-05" / "report.md", prev)

    _analyze(two_orgs, tmp_path)

    # 既定では前月の考察を渡さない（検証できない人手の文書のため）
    default_runner = _runner(BODY)
    _generate(two_orgs, out, default_runner)
    assert "前月の所見" not in default_runner.calls[0]

    # 明示的に有効化したときだけ渡す
    runner = _runner(BODY)
    _generate(two_orgs, out, runner, include_previous=True, force=True)
    assert "前月の所見" in runner.calls[0]
    assert "2026-05" in runner.calls[0]


def test_previous_month_discussion_with_leak_is_excluded(two_orgs, tmp_path):
    """前月の考察に他組織の語があれば資料に含めない（過去の混入を引き写す経路を塞ぐ）。"""
    out = tmp_path / "reports"
    main(["analyze", "--config", CONFIG, "--input-dir", str(two_orgs),
          "--output-dir", str(out), "--month", "2026-05", "--org", "org-a"])
    report.write_discussion(
        out / "org-a" / "2026-05" / "report.md",
        "### 前月の所見\n\n" + "org-b の bernard.holloway と比べると小さい。" * 6)

    _analyze(two_orgs, tmp_path)
    runner = _runner(BODY)
    notices: list[str] = []
    discussion.generate(
        org="org-a", month="2026-06", input_dir=two_orgs, output_dir=out,
        org_output=out / "org-a", cfg=load_config(CONFIG),
        include_previous=True, runner=runner, notify=notices.append)

    assert "前月の所見" not in runner.calls[0]
    assert "bernard" not in runner.calls[0]
    assert any("前月" in n and "除外" in n for n in notices)


# ---------------------------------------------------------------- run_claude のガード


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


@pytest.fixture
def stub_claude(monkeypatch):
    """subprocess.run を差し替えて claude の出力を偽装する。"""
    captured: dict = {}

    def install(**proc_kw):
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _fake_proc(**proc_kw)

        monkeypatch.setattr(discussion.shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.setattr(discussion.subprocess, "run", fake_run)
        return captured

    return install


def test_run_claude_returns_body_and_isolates_context(stub_claude):
    captured = stub_claude(stdout=BODY + "\n")
    s = discussion.settings(load_config(CONFIG))
    assert discussion.run_claude("prompt", s) == BODY.strip()

    cmd = captured["cmd"]
    assert cmd[0] == "claude" and "-p" in cmd
    # プロジェクトの CLAUDE.md・hooks・MCP を読み込ませない
    assert "--safe-mode" in cmd
    # 組み込みツールを空集合にする許可リストが主たる保証
    assert cmd[cmd.index("--tools") + 1] == ""
    # denylist は追加防御として残す
    for tool in ("Read", "Bash", "WebFetch"):
        assert tool in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "xhigh"
    # 空の作業ディレクトリで実行する（リポジトリを起点にさせない）
    assert Path(captured["kwargs"]["cwd"]).name.startswith("seat-analyzer-discuss-")


def test_run_claude_strips_code_fence(stub_claude):
    stub_claude(stdout="```markdown\n" + BODY + "\n```\n")
    s = discussion.settings(load_config(CONFIG))
    assert discussion.run_claude("prompt", s) == BODY.strip()


@pytest.mark.parametrize("stdout", [
    "## 考察\n\n{body}",                        # 先頭にある場合
    "以下が考察です。\n\n## 考察\n\n{body}",      # 前置きの後にある場合
    "##考察\n{body}",                           # 見出し記号の後に空白がない場合
    "## 考察 \n{body}",                         # 行末に空白がある場合
    "```markdown\n## 考察\n\n{body}\n```",      # コードフェンスと併用
])
def test_run_claude_strips_discussion_heading(stub_claude, stdout):
    """出力に「## 考察」が付いてきても差し込み時に H2 が重複しないよう落とす。"""
    stub_claude(stdout=stdout.format(body=BODY))
    s = discussion.settings(load_config(CONFIG))
    result = discussion.run_claude("prompt", s)
    assert result.endswith(BODY.strip())
    for heading in ("## 考察", "##考察"):
        assert heading not in result


def test_run_claude_strips_every_discussion_heading(stub_claude):
    """1つの出力に複数の考察見出しがあってもすべて落とす。"""
    stub_claude(stdout=f"## 考察\n\n{BODY}\n\n## 考察\n\n{BODY}")
    s = discussion.settings(load_config(CONFIG))
    result = discussion.run_claude("prompt", s)
    assert "## 考察" not in result
    assert result.count("### 変更推奨の妥当性") == 2


@pytest.mark.parametrize("proc_kw, message", [
    ({"stdout": ""}, "空"),
    ({"stdout": "API Error: 529 Overloaded."}, "API エラー"),
    ({"stdout": "### 考察\n\n短い。"}, "短すぎます"),
    ({"stdout": "", "stderr": "boom", "returncode": 1}, "異常終了"),
])
def test_run_claude_rejects_bad_output(stub_claude, proc_kw, message):
    stub_claude(**proc_kw)
    s = discussion.settings(load_config(CONFIG))
    with pytest.raises(discussion.DiscussionError, match=message):
        discussion.run_claude("prompt", s)


@pytest.mark.parametrize("proc_kw, transient", [
    ({"stdout": "API Error: 529 Overloaded."}, True),      # 5xx は再実行で解消しうる
    ({"stdout": "API Error: 429 Rate limited."}, True),
    ({"stdout": "API Error: 401 Unauthorized."}, False),   # 認証は再試行しても同じ
    ({"stdout": "API Error: 400 Bad request."}, False),
    ({"stdout": "API Error: connection reset"}, True),     # 状態不明なら一時扱い
    # claude -p は API エラーでも 0 を返すため、非ゼロ終了は使い方・設定の誤りとみなす
    ({"stdout": "", "stderr": "unknown option", "returncode": 2}, False),
])
def test_run_claude_transient_classification(stub_claude, proc_kw, transient):
    stub_claude(**proc_kw)
    s = discussion.settings(load_config(CONFIG))
    with pytest.raises(discussion.DiscussionError) as exc:
        discussion.run_claude("prompt", s)
    assert exc.value.transient is transient


def test_run_claude_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(discussion.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(discussion.subprocess, "run", fake_run)
    s = discussion.settings(load_config(CONFIG))
    with pytest.raises(discussion.DiscussionError, match="応答しませんでした"):
        discussion.run_claude("prompt", s)


def test_run_claude_missing_command(monkeypatch):
    monkeypatch.setattr(discussion.shutil, "which", lambda _: None)
    s = dict(discussion.settings(load_config(CONFIG)), command="claude-not-installed")
    with pytest.raises(discussion.DiscussionError, match="見つかりません"):
        discussion.run_claude("prompt", s)


# ---------------------------------------------------------------- CLI


def test_cli_discuss_writes_all_orgs(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 0
    for org in ("org-a", "org-b"):
        md = (out / org / "2026-06" / "report.md").read_text(encoding="utf-8")
        assert report.discussion_body(md) == BODY.strip()


def test_cli_discuss_dry_run_prints_prompt(two_orgs, tmp_path, capsys):
    out = _analyze(two_orgs, tmp_path)
    capsys.readouterr()
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a", "--dry-run"])
    assert rc == 0
    assert "執筆の原則" in capsys.readouterr().out
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None


def test_cli_discuss_blocked_returns_nonzero(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べる。" * 8
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"])
    assert rc == 1
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None


def test_cli_discuss_failure_does_not_stop_other_orgs(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    # org-a のレポートだけ消して失敗させる
    (out / "org-a" / "2026-06" / "report.md").unlink()
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06"])
    assert rc == 1
    md = (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) == BODY.strip()


def test_cli_analyze_with_discussion(two_orgs, tmp_path, monkeypatch):
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    out = _analyze(two_orgs, tmp_path, "--with-discussion")
    for org in ("org-a", "org-b"):
        md = (out / org / "2026-06" / "report.md").read_text(encoding="utf-8")
        assert report.discussion_body(md) == BODY.strip()


def test_cli_analyze_without_discussion_leaves_placeholder(two_orgs, tmp_path):
    out = _analyze(two_orgs, tmp_path)
    md = (out / "org-a" / "2026-06" / "report.md").read_text(encoding="utf-8")
    assert report.discussion_body(md) is None
    assert "未記入" in md


def test_cli_discuss_allow_term(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "holloway 相当の水準にある。" * 10
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    args = ["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
            "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"]
    assert main(args) == 1
    assert main([*args, "--allow-term", "holloway",
                 "--allow-term", "bernard.holloway"]) == 0


def test_cli_allow_term_requires_single_org(two_orgs, tmp_path, monkeypatch):
    """許可はその組織の生成物を人が確認した結果なので、全組織へ一括適用させない。"""
    out = _analyze(two_orgs, tmp_path)
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "2026-06", "--allow-term", "holloway"])
    assert rc == 1


def test_cli_blocked_output_includes_context(two_orgs, tmp_path, monkeypatch, capsys):
    out = _analyze(two_orgs, tmp_path)
    leaky = "### 変更推奨の妥当性\n\n" + "他組織の bernard.holloway と比べる。" * 8
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: leaky)
    capsys.readouterr()
    main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
          "--output-dir", str(out), "--month", "2026-06", "--org", "org-a"])
    err = capsys.readouterr().err
    assert "検出語: bernard.holloway" in err
    assert "他組織の bernard.holloway と比べる" in err
    assert "--allow-term" in err


# ---------------------------------------------------------------- 入力検証


@pytest.mark.parametrize("month", [
    "../org-b/2026-06",       # 別組織のレポートへの逸脱
    "/etc/2026-06",
    "2026-13",
    "2026-6",
    "2026-06/../../x",
    "2026-06\n",              # 末尾改行（$ は許してしまう）
    "２０２６-06",              # 全角数字（\d は許してしまう）
])
def test_month_format_is_validated(two_orgs, tmp_path, month):
    """対象月は出力パスの一部になるため、形式外の値を受け付けない。"""
    out = _analyze(two_orgs, tmp_path)
    for command in ("analyze", "discuss"):
        rc = main([command, "--config", CONFIG, "--input-dir", str(two_orgs),
                   "--output-dir", str(out), "--month", month, "--org", "org-a"])
        assert rc == 1


def test_traversal_month_does_not_touch_other_org(two_orgs, tmp_path, monkeypatch):
    out = _analyze(two_orgs, tmp_path)
    before = (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8")
    monkeypatch.setattr(discussion, "run_claude", lambda prompt, s: BODY)
    rc = main(["discuss", "--config", CONFIG, "--input-dir", str(two_orgs),
               "--output-dir", str(out), "--month", "../org-b/2026-06", "--org", "org-a"])
    assert rc == 1
    assert (out / "org-b" / "2026-06" / "report.md").read_text(encoding="utf-8") == before


def test_write_discussion_only_if_unwritten_guard(two_orgs, tmp_path):
    """判定と置換を1回の読み取りに畳む（呼び出し側の事前確認より競合の窓が狭い）。"""
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"

    assert report.write_discussion(path, BODY, only_if_unwritten=True) is True
    # 記入済みになったので2回目は何もしない
    other = "### 別の考察\n\n" + "上書きされるべきではない。" * 20
    assert report.write_discussion(path, other, only_if_unwritten=True) is False
    assert report.discussion_body(path.read_text(encoding="utf-8")) == BODY.strip()
    # only_if_unwritten=False なら上書きする
    assert report.write_discussion(path, other) is True
    assert report.discussion_body(path.read_text(encoding="utf-8")) == other.strip()


def test_write_discussion_aborts_when_file_changes_before_replace(two_orgs, tmp_path,
                                                                  monkeypatch):
    """置換直前に内容が変わっていたら書き込まない（判定〜置換の窓を詰める）。"""
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    handwritten = "### 手書きの考察\n\n" + "人が書いた内容。" * 20

    original_chmod = report.os.chmod
    injected: list[int] = []

    def chmod_with_concurrent_write(target, mode):
        # 一時ファイルを作った後・置換直前の照合より前に、別プロセスの書き込みを模す
        if not injected:
            injected.append(1)
            path.write_text(_with_discussion(path, handwritten), encoding="utf-8")
        return original_chmod(target, mode)

    monkeypatch.setattr(report.os, "chmod", chmod_with_concurrent_write)
    assert report.write_discussion(path, BODY, only_if_unwritten=True) is False
    assert report.discussion_body(path.read_text(encoding="utf-8")) == handwritten.strip()
    assert not list(path.parent.glob("report.md.*.tmp"))


def _with_discussion(path: Path, body: str) -> str:
    md = path.read_text(encoding="utf-8")
    head = md.split("\n## 考察\n", 1)[0]
    return head + "\n## 考察\n\n" + body.strip() + "\n"


def test_atomic_write_preserves_permissions(two_orgs, tmp_path):
    """一時ファイル経由の置換で元ファイルの権限を落とさない（共有用に緩めた権限を守る）。"""
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    path.chmod(0o644)
    report.write_discussion(path, BODY)
    assert path.stat().st_mode & 0o777 == 0o644


def test_atomic_write_leaves_original_on_failure(two_orgs, tmp_path, monkeypatch):
    """書き込みが途中で失敗しても、レポート本体は元の内容が残る。"""
    out = _analyze(two_orgs, tmp_path)
    path = out / "org-a" / "2026-06" / "report.md"
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(report.os, "replace", boom)
    with pytest.raises(OSError):
        report.write_discussion(path, BODY)
    assert path.read_text(encoding="utf-8") == before
    # 一時ファイルを残さない
    assert not list(path.parent.glob("report.md.*.tmp"))


@pytest.mark.parametrize("bad", [
    {"max_attempts": 1.9},
    {"retries": 1.9},
    {"min_output_chars": 100.5},
    {"retry_wait_seconds": float("inf")},
    {"timeout_seconds": float("nan")},
    {"timeout_seconds": 10 ** 4000},  # float 変換で OverflowError になる巨大整数
    {"max_attempts": 0},
    {"retries": -1},
    {"effort": "extreme"},
    {"command": ""},
])
def test_config_rejects_bad_discussion_settings(tmp_path, bad):
    import yaml
    cfg = yaml.safe_load(Path(CONFIG).read_text(encoding="utf-8"))
    cfg["discussion"] = {**cfg["discussion"], **bad}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="discussion"):
        load_config(path)


def test_render_does_not_resubstitute_inserted_values(two_orgs, tmp_path):
    """挿入した値の中の {{KEY}} が再置換されない（組織名には波括弧が使える）。"""
    prompt = discussion.build_prompt(
        org="{{MATERIALS}}", scope="s", materials=[("資料1", "MATERIAL-MARKER")], preview=False)
    assert "{{MATERIALS}}" in prompt                 # ORG 位置にそのまま残る
    assert prompt.count("MATERIAL-MARKER") == 1      # 資料が二重に展開されない
