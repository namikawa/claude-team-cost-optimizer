"""他組織情報の混入チェック（leakcheck）のテスト。

禁止語の収集（forbidden_terms）と、語境界規則を含む照合（find_leaks）が対象。
考察の執筆側からこのエンジンをどう使うかは test_discussion.py が見る。
"""

import shutil
from pathlib import Path

import pytest

from seat_analyzer import leakcheck
from seat_analyzer.config import load_config

from .conftest import CONFIG, hit_terms, run_analyze, spend_row


def _terms(*specs: tuple[str, str]) -> tuple[leakcheck.Term, ...]:
    return tuple(leakcheck.Term(text, kind) for text, kind in specs)


def _texts(terms) -> set[str]:
    return {t.text for t in terms}


def test_forbidden_terms_excludes_target_org(two_orgs, tmp_path):
    cfg = load_config(CONFIG)
    terms = leakcheck.forbidden_terms(
        input_dir=two_orgs, output_dir=tmp_path / "reports", target_org="org-a", cfg=cfg)
    texts = _texts(terms)
    assert "org-b" in texts
    assert "bernard.holloway@y.jp" in texts
    assert "bernard" in texts and "holloway" in texts
    # 組織名は kind=org として常時禁止の扱いになる
    assert leakcheck.Term("org-b", "org") in terms
    # 対象組織自身の語は禁止語に入らない
    assert "org-a" not in texts
    assert not any("alice" in t for t in texts)


def test_forbidden_terms_splits_short_name_segments(make_input, tmp_path):
    """ドット・アンダースコア区切りの短い姓名も禁止語に含める（実運用の命名に合わせる）。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-a")
    make_input({"2026-06": [spend_row("taro.sato@y.jp", 10.0)]},
               members=["taro.sato@y.jp,Premium", "hana_kato@y.jp,Standard"], org="org-b")
    texts = _texts(leakcheck.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org="org-a", cfg=load_config(CONFIG)))
    assert {"taro", "sato", "hana", "kato"} <= texts


def test_find_leaks_flags_only_terms_absent_from_source():
    terms = _terms(("org-b", "org"), ("bernard.holloway@y.jp", "address"),
                   ("holloway", "person"), ("架空推進3部", "group"))
    source = "org-a のユーザ alice.morgan@x.jp は Premium。"

    assert hit_terms(leakcheck.find_leaks(
        "holloway さんは Standard で足りている。", terms, source=source)) == ("holloway",)
    assert hit_terms(leakcheck.find_leaks(
        "架空推進3部の削減余地は小さい。", terms, source=source)) == ("架空推進3部",)
    assert leakcheck.find_leaks("alice.morgan の需要は妥当。", terms, source=source) == ()


def test_find_leaks_ignores_terms_present_in_source():
    # 対象組織の資料に現れる語は、出力に出てきても混入ではない
    terms = _terms(("holloway", "person"))
    source = "holloway@x.jp は対象組織のユーザ。"
    assert leakcheck.find_leaks("holloway は Premium 継続。", terms, source=source) == ()


def test_find_leaks_always_forbids_other_org_names():
    """組織名は対象組織の資料に現れても混入として扱う（資料側の混入を許可根拠にしない）。"""
    source = "備考: org-b から異動。"
    assert hit_terms(leakcheck.find_leaks(
        "org-b と比べると小さい。", _terms(("org-b", "org")), source=source)) == ("org-b",)
    # 人名・部署名は従来どおり資料に現れれば除外する
    assert leakcheck.find_leaks(
        "holloway は継続。", _terms(("holloway", "person")), source=source + " holloway@x.jp") == ()


def test_find_leaks_respects_word_boundaries():
    # 英単語の一部として現れる出現は拾わない（detail の中の etai 等の誤検出防止）
    assert leakcheck.find_leaks("detail を確認する。", _terms(("etai", "person")), source="") == ()
    # メールのローカル部・ドメインの構成要素として現れる出現は拾う
    assert hit_terms(leakcheck.find_leaks(
        "bernard.holloway", _terms(("bernard", "person")), source="")) == ("bernard",)
    assert hit_terms(leakcheck.find_leaks(
        "holloway@y.jp", _terms(("holloway", "person")), source="")) == ("holloway",)
    # 日本語が隣接する出現は拾う
    assert hit_terms(leakcheck.find_leaks(
        "org-b組織では", _terms(("org-b", "org")), source="")) == ("org-b",)


def test_find_leaks_short_japanese_terms_need_non_kanji_boundary():
    """短い日本語の語は漢字・カタカナの連結を語の一部とみなす（一般語の誤検出を防ぐ）。"""
    short = _terms(("開発部", "group"), ("人事", "group"))
    # 無関係な複合語の一部としての出現は拾わない
    assert leakcheck.find_leaks("製品開発部門の需要が大きい。", short, source="") == ()
    assert leakcheck.find_leaks("人事評価制度を見直す。", short, source="") == ()
    # 助詞・記号が続く出現は拾う
    assert hit_terms(leakcheck.find_leaks("開発部の削減余地は小さい。", short, source="")) \
        == ("開発部",)
    assert hit_terms(leakcheck.find_leaks("人事、総務の2部署。", short, source="")) == ("人事",)


def test_find_leaks_long_japanese_terms_match_inside_compounds():
    """長い日本語の語は固有性が高いため、複合語に埋め込まれても検出する。"""
    long_term = _terms(("架空推進3部", "group"))
    assert hit_terms(leakcheck.find_leaks(
        "架空推進3部第2チームの需要。", long_term, source="")) == ("架空推進3部",)


def test_find_leaks_reports_context_and_kind():
    hits = leakcheck.find_leaks(
        "前段の説明。製品開発部の削減余地は小さい。後段の説明。",
        _terms(("製品開発部", "group")), source="")
    assert len(hits) == 1
    assert hits[0].kind == "group"
    assert "製品開発部の削減余地" in hits[0].context


def test_find_leaks_allow_overrides_detection():
    terms = _terms(("開発部", "group"), ("holloway", "person"))
    text = "開発部と holloway について。"
    assert hit_terms(leakcheck.find_leaks(text, terms, source="")) == ("holloway", "開発部")
    # 人が確認して無害と判断した語は許可できる（大文字小文字は問わない）
    assert hit_terms(leakcheck.find_leaks(
        text, terms, source="", allow=("開発部",))) == ("holloway",)
    assert leakcheck.find_leaks(text, terms, source="", allow=("開発部", "HOLLOWAY")) == ()


def test_allow_cannot_override_org_names_or_addresses():
    """組織名とメールアドレスは --allow-term の対象外（誤検出の余地が実質なく影響が大きい）。"""
    terms = _terms(("org-b", "org"), ("x@y.jp", "address"), ("y.jp", "domain"))
    text = "org-b の x@y.jp（y.jp）について。"
    hits = leakcheck.find_leaks(
        text, terms, source="", allow=("org-b", "x@y.jp", "y.jp"))
    assert hit_terms(hits) == ("org-b", "x@y.jp")
    assert all(h.allowable is False for h in hits)


def test_find_leaks_org_names_use_aggressive_boundary():
    """短い日本語の緩い規則を組織名に適用すると取りこぼす（影響が最大の種類なので例外扱い）。"""
    assert hit_terms(leakcheck.find_leaks(
        "東京支社の利用状況。", _terms(("東京", "org")), source="")) == ("東京",)
    # 同じ長さでも部署名なら複合語の一部としては拾わない
    assert leakcheck.find_leaks("東京支社の利用状況。", _terms(("東京", "group")), source="") == ()


def test_group_names_from_members_info(two_orgs, tmp_path):
    (two_orgs / "org-b" / "members-info.csv").write_text(
        "email,部署,チーム,職種\nbernard.holloway@y.jp,架空推進3部,Nebula-AI,エンジニア\n",
        encoding="utf-8")
    cfg = load_config(CONFIG)
    texts = _texts(leakcheck.forbidden_terms(
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
    texts = _texts(leakcheck.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org="org-a", cfg=load_config(CONFIG)))
    assert "members" in texts and "holloway" in texts


def test_forbidden_terms_fails_closed_on_unreadable_input(two_orgs, tmp_path, monkeypatch):
    """収集元が読めない場合、不完全な禁止語集合で通さず中止する。"""
    def boom(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(leakcheck.LeakCheckError, match="混入チェックを保証できません"):
        leakcheck.forbidden_terms(
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
        with pytest.raises(leakcheck.LeakCheckError, match="混入チェックを保証できません"):
            leakcheck.forbidden_terms(
                input_dir=two_orgs, output_dir=tmp_path / "reports",
                target_org="org-a", cfg=load_config(CONFIG))
    finally:
        victim.chmod(0o755)


def test_single_character_org_name_is_detected(make_input, tmp_path):
    """1文字の組織名も禁止語に残す（長さ下限は派生語の誤検出対策で、識別子には適用しない）。"""
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 10.0)]},
                           members=["a@x.jp,Premium"], org="org-a")
    make_input({"2026-06": [spend_row("z@y.jp", 10.0)]}, members=["z@y.jp,Premium"], org="A")
    terms = leakcheck.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org="org-a", cfg=load_config(CONFIG))
    assert leakcheck.Term("A", "org") in terms
    assert hit_terms(leakcheck.find_leaks("A社の状況は…", terms, source="")) == ("A",)


def test_duplicate_text_merges_to_stricter_kind():
    """同一文字列が複数 kind にあるとき、厳しい側（許可できない側）に寄せる。"""
    terms = _terms(("acme", "domain"), ("acme", "org"))
    hits = leakcheck.find_leaks("acme の状況。", terms, source="")
    assert [(h.kind, h.allowable) for h in hits] == [("org", False)]
    # 許可指定を付けても org として残る（案内と実挙動が一致する）
    assert hit_terms(leakcheck.find_leaks(
        "acme の状況。", terms, source="", allow=("acme",))) == ("acme",)


def test_forbidden_terms_fails_closed_on_broken_members_info(two_orgs, tmp_path):
    (two_orgs / "org-b" / "members-info.csv").write_text(
        "部署,チーム\n架空推進3部,Nebula-AI\n", encoding="utf-8")  # email 列が無い
    with pytest.raises(leakcheck.LeakCheckError, match="混入チェックを保証できません"):
        leakcheck.forbidden_terms(
            input_dir=two_orgs, output_dir=tmp_path / "reports",
            target_org="org-a", cfg=load_config(CONFIG))


def test_forbidden_terms_harvested_from_reports_only_org(two_orgs, tmp_path):
    """入力が無く reports にだけ残っている組織からも語を集める。

    集めないと組織名1件だけの禁止語になり、その組織のユーザ名が素通りする。
    """
    out = run_analyze(two_orgs, tmp_path)
    # org-b の入力を消し、生成済みレポートだけ残す
    shutil.rmtree(two_orgs / "org-b")
    texts = _texts(leakcheck.forbidden_terms(
        input_dir=two_orgs, output_dir=out, target_org="org-a", cfg=load_config(CONFIG)))
    assert "org-b" in texts
    assert "bernard.holloway@y.jp" in texts and "holloway" in texts


def test_legacy_layout_has_no_forbidden_terms(make_input, tmp_path):
    """旧レイアウト（input/spend 直下）では他組織が存在しないため禁止語は空。

    入力サブディレクトリ名を組織名と誤認すると、考察が「spend」「members」に触れる
    だけでブロックされ、自組織のユーザ名まで禁止語に入ってしまう。
    """
    input_dir = make_input({"2026-06": [spend_row("alice.morgan@x.jp", 10.0)]},
                           members=["alice.morgan@x.jp,Premium"])
    terms = leakcheck.forbidden_terms(
        input_dir=input_dir, output_dir=tmp_path / "reports",
        target_org=None, cfg=load_config(CONFIG))
    assert terms == ()
