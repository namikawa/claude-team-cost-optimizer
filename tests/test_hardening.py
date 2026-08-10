"""外部レビュー対応（出力の安全性・入力取り違え防止・設定検証）のテスト。"""

import copy

import pandas as pd
import pytest

from seat_analyzer.analyze import analyze
from seat_analyzer.cli import main
from seat_analyzer.config import _validate
from seat_analyzer.ingest import discover_months
from seat_analyzer.pricing import unmatched_models
from seat_analyzer.report import write_csv, write_html
from seat_analyzer.report.html import (
    _CODE_DIFF_HTML,
    _CREDIT_REACH_HTML,
    _E_DIST_HTML,
    _GRANT_HTML,
    _HTML_TEMPLATE_SRC,
    _MEMBER_CHANGES_HTML,
    _PREVIEW_HTML_TEMPLATE_SRC,
    _SNAPSHOT_HTML,
)
from seat_analyzer.report.text import _embed_shared_text

from .conftest import SPEND_HEADER, spend_row


# --- 出力の安全性 ---

def test_html_escapes_script_in_email(cfg, make_input, tmp_path):
    evil = '<script>alert(1)</script>@x.jp'
    input_dir = make_input(
        {"2026-06": [spend_row(evil, 10.0)]}, members=[f"{evil},Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    out = tmp_path / "dashboard.html"
    write_html(result, out)
    html = out.read_text(encoding="utf-8")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_no_unresolved_shared_text_markers_in_templates():
    # md/HTML 共有文言の <!--text:キー--> は組み立て時に全て置換されること。
    # キー名を打ち間違えるとマーカーがそのままダッシュボードに出るため、機械的に防ぐ
    for src in (_embed_shared_text(_HTML_TEMPLATE_SRC),
                _embed_shared_text(_PREVIEW_HTML_TEMPLATE_SRC),
                _embed_shared_text(_SNAPSHOT_HTML + _CODE_DIFF_HTML + _MEMBER_CHANGES_HTML
                                   + _E_DIST_HTML + _GRANT_HTML + _CREDIT_REACH_HTML)):
        assert "<!--text:" not in src


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
    result = analyze(input_dir, "2026-06", cfg)
    assert any("未来月" in w for w in result.warnings)


def test_manually_created_summary_org_rejected(make_input, tmp_path, capsys):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Standard"], org="summary",
    )
    rc = main([
        "analyze", "--config", "config.yaml",
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
    result = analyze(input_dir, "2026-06", cfg)
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


# --- 組織名バリデーション（共通） ---

def test_org_name_validation():
    from seat_analyzer.ingest import validate_org_name
    validate_org_name("org-a")          # 正常
    validate_org_name("開発本部")        # 日本語は許可
    validate_org_name("config")         # 予約デバイス名に似ているだけの名前は許可
    validate_org_name("org.a")          # 途中のドットは許可（末尾だけが問題）
    bad_names = (
        "summary", ".hidden", "a/b", "org|x", "a[b]", " x", "x ",
        # Windows でディレクトリ名に使えない文字（NTFS の代替データストリーム等）
        "a:b", "a*b", "a?b", 'a"b',
        # Windows が末尾のドットを黙って落とすため input/ と reports/ が食い違う
        "org.",
        # Windows のデバイス名。拡張子が付いていても同じ扱いになる
        "CON", "nul", "com1", "LPT9", "aux.csv",
    )
    for bad in bad_names:
        with pytest.raises(ValueError):
            validate_org_name(bad)


def test_manually_created_bad_org_dir_rejected(make_input, tmp_path, capsys):
    # spend/ を持つ不正名ディレクトリを手動作成 → 分析時に弾く
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 1.0)]}, members=["a@x.jp,Standard"], org="org|x",
    )
    rc = main([
        "analyze", "--config", "config.yaml",
        "--input-dir", str(input_dir), "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    assert "使えない文字" in capsys.readouterr().err
