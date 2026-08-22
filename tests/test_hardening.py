"""外部レビュー対応（出力の安全性・入力取り違え防止・設定検証）のテスト。"""

import copy
import re

import pandas as pd
import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.cli import main
from seat_analyzer.config import _validate, load_config
from seat_analyzer.ingest import discover_months
from seat_analyzer.pricing import unmatched_models
from seat_analyzer.report import write_csv, write_html
from seat_analyzer.report.html import (
    _DASHBOARD_SECTIONS,
    _HTML_ASSEMBLED,
    _HTML_SOURCE,
    _HTML_TEMPLATE_SRC,
    _PREVIEW_HTML_ASSEMBLED,
    _PREVIEW_HTML_SOURCE,
    _PREVIEW_HTML_TEMPLATE_SRC,
    _PREVIEW_SECTIONS,
    _assemble,
)

from .conftest import CONFIG, SPEND_HEADER, requires_posix_filenames, spend_row


# --- 出力の安全性 ---

def test_html_escapes_script_in_email(cfg, make_input, tmp_path):
    evil = '<script>alert(1)</script>@x.jp'
    input_dir = make_input(
        {"2026-06": [spend_row(evil, 10.0)]}, members=[f"{evil},Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    out = tmp_path / "dashboard.html"
    write_html(result, out)
    html = out.read_text(encoding="utf-8")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


# テンプレート1本分の (名前, 本体, 差し込み表, 差し込み後, 文言解決後)。
_TEMPLATES = (
    ("dashboard.html", _HTML_TEMPLATE_SRC, _DASHBOARD_SECTIONS,
     _HTML_ASSEMBLED, _HTML_SOURCE),
    ("preview-dashboard.html", _PREVIEW_HTML_TEMPLATE_SRC, _PREVIEW_SECTIONS,
     _PREVIEW_HTML_ASSEMBLED, _PREVIEW_HTML_SOURCE),
)

# 断片の差し込み先（HTML コメント形式・大文字とアンダースコアのみ）。共有文言の
# <!--text:キー--> は小文字とコロンを含むので一致しない
_PLACEHOLDER_RE = re.compile(r"<!--[A-Z][A-Z_]*-->")


def test_every_section_placeholder_is_filled_exactly_once():
    """差し込み表の placeholder が本体にちょうど1つあり、組み立て後には残らない。

    placeholder は HTML コメントなので、表と本体が食い違っても画面には何も出ず、
    そのセクションが黙って消える。
    """
    for name, src, sections, assembled, _ in _TEMPLATES:
        assert sections, f"{name}: 差し込み表が空です"
        for placeholder in sections:
            assert src.count(placeholder) == 1, (
                f"{name}: 差し込み表の {placeholder} が本体に "
                f"{src.count(placeholder)} 個あります（ちょうど1個であること）"
            )
            assert placeholder not in assembled, (
                f"{name}: {placeholder} が組み立て後も残っています"
            )


def test_no_section_placeholder_is_left_unfilled():
    """本体にあって差し込み表に無い placeholder が残っていない。

    テンプレートへ placeholder を書いただけでは差し込まれない。表への追記漏れは
    ここで落ちる。
    """
    for name, _, _, assembled, _ in _TEMPLATES:
        left = _PLACEHOLDER_RE.findall(assembled)
        assert not left, f"{name}: 差し込み表に無い placeholder が残っています: {left}"


def test_assemble_requires_exactly_one_destination():
    """差し込み先が0個・2個以上なら組み立てを止める（規則そのものの検査）。"""
    assert _assemble("a<!--X-->b", {"<!--X-->": ("1", "2")}) == "a12b"
    for body in ("ab", "a<!--X-->b<!--X-->"):
        with pytest.raises(ValueError, match="<!--X-->"):
            _assemble(body, {"<!--X-->": ("1",)})


def test_no_unresolved_shared_text_markers_in_templates():
    """md/HTML 共有文言の <!--text:キー--> が組み立て後に1つも残っていない。

    _embed_shared_text は未知のキーを置換せずそのまま残すため、キー名を打ち間違えると
    その注記は HTML コメントになって出力から消える（画面上は何も起きない）。見るのは
    断片ではなく差し込み後のテンプレート本体なので、断片を足しても対象へ自動で入る。
    """
    for name, _, _, _, source in _TEMPLATES:
        assert "<!--text:" not in source, f"{name}: 未解決の共有文言マーカーが残っています"


def test_shared_text_check_is_not_vacuous():
    """共有文言の検査が空振りしていない（置換の対象が実在する）。

    保証するのは差し込み後のテンプレートにマーカーが1つ以上あることだけで、どの断片に
    何個あるかは見ない（個々の断片が入っていることは placeholder 側の検査が受け持つ）。
    """
    for name, _, _, assembled, _ in _TEMPLATES:
        assert "<!--text:" in assembled, f"{name}: 共有文言マーカーが1つもありません"


def test_csv_formula_cells_are_sanitized(tmp_path):
    users = pd.DataFrame([
        {"email": "=HYPERLINK(\"http://evil\")", "monthly_saving_usd": -5.0},
        {"email": "a@x.jp", "monthly_saving_usd": 10.0},
    ])
    result = type("R", (), {"users": users})()
    path = tmp_path / "rec.csv"
    write_csv(result, path)
    text = path.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text          # 文字列セルは ' 付与で無害化
    assert "a@x.jp" in text               # 通常の文字列はそのまま
    assert "-5.0" in text                 # 数値セルは変更しない


def test_csv_cell_newlines_are_normalized(tmp_path):
    # 引用符に囲まれたセルの中の改行は lineterminator の対象外で、入力の CR がそのまま出る。
    # 式のエスケープはセル内改行を均す前に判定するので、CR 始まりのセルにも ' が付く
    users = pd.DataFrame([
        {"email": "a@x.jp", "note": "1行目\r\n2行目\r3行目"},
        {"email": "b@x.jp", "note": "\r=SUM(A1)"},
    ])
    result = type("R", (), {"users": users})()
    path = tmp_path / "rec.csv"
    write_csv(result, path)
    # read_text は改行を正規化して読むため、CR の有無はバイト列で見る
    text = path.read_bytes().decode("utf-8-sig")
    assert "\r" not in text
    assert "1行目\n2行目\n3行目" in text
    assert "'\n=SUM(A1)" in text


# --- 入力ファイルの取り違え防止 ---

def test_duplicate_month_csv_raises(make_input):
    # 月のみの命名同士は期間で優先順を判断できないためエラー
    input_dir = make_input({"2026-06": [spend_row("a@x.jp", 1.0)]})
    dup = input_dir / "spend" / "spend-report_2026-06.csv"
    dup.write_text(SPEND_HEADER + "\n" + spend_row("a@x.jp", 2.0) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="複数あり"):
        discover_months(input_dir)


def test_future_members_fallback_warns_strongly(cfg, make_input):
    input_dir = make_input(
        {"2026-05": [spend_row("a@x.jp", 10.0)], "2026-06": [spend_row("a@x.jp", 10.0)]},
        members=["a@x.jp,Premium"], members_month="2026-07",
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    # 対象月末から 31 日後＝通常運用（翌月初の取得）の幅を超えるので強い注意が付く
    assert any(
        "月末の 31 日後" in w and "参考値として扱ってください" in w
        for w in result.warnings
    )


def test_manually_created_summary_org_rejected(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Standard"], org="summary",
    )
    rc = main([
        "analyze", "--config", CONFIG,
        "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    assert "予約" in capsys.readouterr().err


# --- 未知モデルの警告 ---

def test_unmatched_models_listed(cfg):
    models = ["claude-sonnet-4-6", "brand-new-model-1", float("nan")]
    assert unmatched_models(models, cfg) == ["brand-new-model-1"]


def test_unknown_model_warns_in_analyze(cfg, make_input):
    row = "a@x.jp,uuid-x,Claude Code,mystery-model-9,mystery,10,100000,10000,1.0,1.0"
    input_dir = make_input({"2026-06": [row]}, members=["a@x.jp,Standard"])
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert any("mystery-model-9" in w for w in result.warnings)


# --- config.yaml の検証 ---

def test_config_validation_catches_edit_mistakes(cfg):
    ok = copy.deepcopy(cfg)
    _validate(ok)  # 正常な config は通る

    broken = copy.deepcopy(cfg)
    del broken["seats"]["standard"]["allowance_usd"]["mid"]
    broken["decision"]["hysteresis_months"] = 0
    broken["decision"]["buffer_ratio"] = 1.5
    broken["model_prices"]["patterns"] = []
    with pytest.raises(ValueError) as e:
        _validate(broken)
    msg = str(e.value)
    for fragment in ("allowance_usd.mid", "hysteresis_months", "buffer_ratio", "patterns"):
        assert fragment in msg


def test_decision_v2_defaults_exist(tmp_path):
    """パッケージ既定だけで decision_v2 が揃い、既定では無効であること（設計書 §21）。

    V1 の decision 節とは独立で、この節の追加が既定のヒステリシスを変えないことも
    あわせて固定する。
    """
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8", newline="\n")
    loaded = load_config(path)
    assert loaded["decision_v2"] == {
        "enabled": False,
        "upgrade": {"min_complete_months": 1},
        "downgrade": {"min_complete_months": 2},
        "recent_seat_change_days": 28,
        "min_assignment_saving_usd": 20.0,
    }
    assert loaded["decision"]["hysteresis_months"] == 2


def test_decision_v2_validation_catches_edit_mistakes(cfg):
    """decision_v2 の編集ミスは enabled が偽のままでも実行前に検出される。

    enabled は既定の False のままにする（有効なときだけ値を検査する実装への退行を、
    このテスト自身が見逃さないようにするため）。
    """
    broken = copy.deepcopy(cfg)
    assert broken["decision_v2"]["enabled"] is False
    broken["decision_v2"]["upgrade"]["min_complete_months"] = 0
    broken["decision_v2"]["downgrade"]["min_complete_months"] = 1.5
    broken["decision_v2"]["recent_seat_change_days"] = True
    broken["decision_v2"]["min_assignment_saving_usd"] = -1
    with pytest.raises(ValueError) as e:
        _validate(broken)
    msg = str(e.value)
    for fragment in (
        "decision_v2.upgrade.min_complete_months",
        "decision_v2.downgrade.min_complete_months",
        "decision_v2.recent_seat_change_days",
        "decision_v2.min_assignment_saving_usd",
    ):
        assert fragment in msg


@pytest.mark.parametrize("value", ["yes", 1, 0, None, "false"])
def test_decision_v2_enabled_must_be_boolean(cfg, value):
    """enabled は真偽値に限る（yes や 1 を有効として黙って受理しない）。"""
    broken = copy.deepcopy(cfg)
    broken["decision_v2"]["enabled"] = value
    with pytest.raises(ValueError, match="decision_v2.enabled"):
        _validate(broken)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), True, "20", None, -0.01],
)
def test_decision_v2_saving_threshold_rejects_invalid(cfg, value):
    """削減閾値は 0 以上の有限な数値に限る（非有限値・真偽値・文字列を拒否する）。"""
    broken = copy.deepcopy(cfg)
    broken["decision_v2"]["min_assignment_saving_usd"] = value
    with pytest.raises(ValueError, match="decision_v2.min_assignment_saving_usd"):
        _validate(broken)


@pytest.mark.parametrize("value", [0, 0.0, 20, 1234.5])
def test_decision_v2_saving_threshold_accepts_zero_and_positive(cfg, value):
    """0 は「差額を問わない」の指定として正当なので拒否しない。"""
    ok = copy.deepcopy(cfg)
    ok["decision_v2"]["min_assignment_saving_usd"] = value
    _validate(ok)


def test_config_validation_price_ordering(cfg):
    broken = copy.deepcopy(cfg)
    broken["seats"]["premium"]["price_usd"] = 10.0
    broken["seats"]["premium"]["allowance_usd"] = {"low": 5.0, "mid": 3.0, "high": 8.0}
    with pytest.raises(ValueError) as e:
        _validate(broken)
    assert "standard より大きい" in str(e.value)
    assert "low <= mid <= high" in str(e.value)


def test_config_validation_missing_required_column(cfg):
    broken = copy.deepcopy(cfg)
    del broken["columns"]["spend"]["email"]         # 必須エイリアスを削除
    del broken["columns"]["members"]["seat_type"]
    with pytest.raises(ValueError) as e:
        _validate(broken)
    msg = str(e.value)
    assert "columns.spend.email" in msg
    assert "columns.members.seat_type" in msg


def test_config_validation_missing_spend_optional_alias(cfg):
    broken = copy.deepcopy(cfg)
    del broken["columns"]["spend"]["user_id"]
    with pytest.raises(ValueError) as e:
        _validate(broken)
    assert "columns.spend.user_id" in str(e.value)


def test_config_validation_missing_members_optional_alias(cfg):
    broken = copy.deepcopy(cfg)
    del broken["columns"]["members"]["member_status"]
    with pytest.raises(ValueError) as e:
        _validate(broken)
    assert "columns.members.member_status" in str(e.value)


# --- product policy の検証 ---

def _policy(cfg, **overrides) -> dict:
    """product_policy の一部を差し替えた設定（既定は cfg のまま）。"""
    edited = copy.deepcopy(cfg)
    edited["product_policy"].update(overrides)
    return edited


def test_product_policy_defaults(cfg):
    """既定設定がそのままロードでき、各キーが期待どおりの型で読める。"""
    policy = cfg["product_policy"]
    for key in ("primary", "supplementary", "prohibited"):
        assert isinstance(policy[key], list)
        assert all(isinstance(v, str) and v.strip() for v in policy[key])
    assert policy["primary"] == ["Claude Code"]
    assert "Chat" in policy["supplementary"]
    # 既定に具体的な product を入れない（配布物に特定組織の方針を含めないため）
    assert policy["prohibited"] == []
    assert isinstance(policy["supplementary_high_usd"], float)
    assert policy["supplementary_high_usd"] == 100.0


def test_product_policy_empty_primary_rejected(cfg):
    """primary が空だと「開発利用の主軸」を定義できない。"""
    with pytest.raises(ValueError) as e:
        _validate(_policy(cfg, primary=[]))
    assert "product_policy.primary" in str(e.value)


@pytest.mark.parametrize("primary", [
    ["Claude Code", ""],        # 空文字
    ["Claude Code", "   "],     # 空白のみ
    ["Claude Code", None],      # 値を書き忘れた行
    ["Claude Code", 3],         # 文字列でない
    "Claude Code",              # リストでない
])
def test_product_policy_blank_product_name_rejected(cfg, primary):
    with pytest.raises(ValueError) as e:
        _validate(_policy(cfg, primary=primary))
    assert "product_policy.primary は空でない文字列のリストが必要です" in str(e.value)


@pytest.mark.parametrize("value", [
    -0.01, "100", None, True, float("nan"), float("inf"), float("-inf"),
])
def test_product_policy_threshold_validated(cfg, value):
    with pytest.raises(ValueError) as e:
        _validate(_policy(cfg, supplementary_high_usd=value))
    assert "product_policy.supplementary_high_usd" in str(e.value)


def test_product_policy_zero_threshold_accepted(cfg):
    """0 は正当な境界値。判定は「閾値以上」なので、需要ゼロでも真になる。"""
    _validate(_policy(cfg, supplementary_high_usd=0.0))


@pytest.mark.parametrize("name", ["Chat", "chat", "  CHAT  "])
def test_product_policy_primary_supplementary_overlap_rejected(cfg, name):
    """primary と supplementary は排他。両方にあると、どちらとして数えるか決まらない。

    設定ミスを拾うのが目的なので、前後空白と大小文字の違いは同じ名前として扱う。
    """
    with pytest.raises(ValueError) as e:
        _validate(_policy(cfg, primary=["Claude Code", name]))
    msg = str(e.value)
    assert "primary と supplementary に同じ product 名があります" in msg
    assert name.strip() in msg       # どの product が重なったかを示す


def test_product_policy_duplicate_within_one_list_rejected(cfg):
    """同じリスト内の重複も設定ミスとして拾う（分類は決まるが書き間違いのため）。"""
    for key in ("primary", "supplementary", "prohibited"):
        with pytest.raises(ValueError) as e:
            _validate(_policy(cfg, **{key: ["Example Product", "example product"]}))
        assert f"product_policy.{key} に同じ product 名が複数あります" in str(e.value)


def test_product_policy_duplicate_normalizes_unicode(cfg):
    """合成済みと分解済みの同じ名前は同一とみなす（組織名の衝突判定と同じ規則）。

    ソースの見た目では区別できないのでコードポイントで書く。
    """
    composed, decomposed = "Caf\u00e9", "Cafe\u0301"
    assert composed != decomposed
    with pytest.raises(ValueError) as e:
        _validate(_policy(cfg, primary=[composed, decomposed]))
    assert "product_policy.primary に同じ product 名が複数あります" in str(e.value)


def test_product_policy_prohibited_may_be_empty(cfg):
    """prohibited は「該当なし」を表せる必要がある（既定も空）。"""
    _validate(_policy(cfg, prohibited=[]))
    _validate(_policy(cfg, prohibited=["Example Product"]))


def test_product_policy_prohibited_may_repeat_other_kinds(cfg, tmp_path):
    """prohibited は primary / supplementary と直交する指定なので重ねて書ける。

    workspace-config.yaml が案内する「prohibited だけを上書きする」書き方が通ること
    の回帰テスト。既定の supplementary にある product 名を禁止に指定できないと、
    案内どおりに書いた設定がロードできない（避けるために supplementary から消すと、
    今度は supplementary_high の集計対象が変わってしまう）。
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        'product_policy:\n  prohibited: ["Chat"]\n', encoding="utf-8", newline="\n")
    loaded = load_config(path)
    assert loaded["product_policy"]["prohibited"] == ["Chat"]
    # 分類の側は既定のまま（禁止指定が supplementary の顔ぶれを変えない）
    assert loaded["product_policy"]["supplementary"] == cfg["product_policy"]["supplementary"]
    # primary との重なりも同じ理由で許す
    _validate(_policy(cfg, prohibited=list(cfg["product_policy"]["primary"])))


# --- 組織名バリデーション（共通） ---

def test_org_name_validation():
    from seat_analyzer.ingest import validate_org_name
    validate_org_name("org-a")          # 正常
    validate_org_name("開発本部")        # 日本語は許可
    validate_org_name("config")         # 予約デバイス名に似ているだけの名前は許可
    validate_org_name("org.a")          # 途中のドットは許可（末尾だけが問題）
    validate_org_name("members")        # spend 以外の入力サブディレクトリ名は許可
    bad_names = (
        "summary", ".hidden", "a/b", "org|x", "a[b]", " x", "x ",
        # 大文字小文字を区別しないファイルシステムでは reports/summary と同じ場所になる
        "SUMMARY", "Summary",
        # input/ 直下の spend/（旧レイアウトの目印）と区別できない。大文字小文字を
        # 区別しないファイルシステムでは Spend も同じディレクトリになる
        "spend", "SPEND", "Spend",
        # Windows でディレクトリ名に使えない文字（NTFS の代替データストリーム等）
        "a:b", "a*b", "a?b", 'a"b',
        # 制御文字は 0x00-0x1f 全体が使えない（改行・タブだけではない）
        "org\x01x", "org\x1fx",
        # Windows が末尾のドットを黙って落とすため input/ と reports/ が食い違う
        "org.",
        # Windows のデバイス名。拡張子が付いていても、上付き数字の変種も同じ扱い。
        # コンソールのデバイス名（CONIN$ / CONOUT$）と、拡張子前の空白による回避も塞ぐ
        "CON", "nul", "com1", "LPT9", "aux.csv", "COM0", "LPT0", "COM¹", "LPT³.csv",
        "CONIN$", "conout$.csv", "NUL .txt",
    )
    for bad in bad_names:
        with pytest.raises(ValueError):
            validate_org_name(bad)


def test_org_name_collision_detection():
    """同じ出力先になる組織名の組み合わせを、書き込む前に弾く。"""
    from seat_analyzer.ingest import validate_org_names
    validate_org_names(["org-a", "org-b"])   # 正常
    validate_org_names(["org-a", "org-a"])   # 完全一致は重複指定として許す
    # 大文字小文字だけが違う名前は、既定の Windows / macOS で同じディレクトリになる
    with pytest.raises(ValueError, match="大文字小文字"):
        validate_org_names(["Acme", "acme"])
    # 合成済みの「ガ」と、分解した「カ」＋濁点も同じディレクトリになる（macOS は
    # 正規化を区別しない）。casefold だけでは別物と判定されるため正規化して比較する。
    # ソースの見た目では区別できないのでコードポイントで書く
    composed, decomposed = "\u30ac\u793e", "\u30ab\u3099\u793e"
    assert composed != decomposed
    with pytest.raises(ValueError, match="同じ出力先"):
        validate_org_names([composed, decomposed])
    # 集合の検証でも個々の検証は効く
    with pytest.raises(ValueError, match="予約"):
        validate_org_names(["org-a", "summary"])


@requires_posix_filenames
def test_manually_created_bad_org_dir_rejected(make_input, tmp_path, capsys):
    # spend/ を持つ不正名ディレクトリを手動作成 → 分析時に弾く
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Standard"], org="org|x",
    )
    rc = main([
        "analyze", "--config", CONFIG,
        "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    assert "使えない文字" in capsys.readouterr().err
