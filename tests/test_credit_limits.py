"""追加クレジット（usage credits）のユーザ単位対応のテスト。

値パース・モード導出・cap_suspected 抑制・κ 到達/整合性警告・E 分布・付与候補・
構成サマリ・後方互換をカバーする。判定ロジック（推奨・ヒステリシス）の数値は変えない。
"""

import math
from pathlib import Path

from seat_analyzer.analyze import (
    CREDIT_DISABLED,
    CREDIT_ENABLED,
    CREDIT_UNKNOWN,
    analyze,
    credits_mode,
    preview,
)
from seat_analyzer.ingest import parse_credit_limit
from seat_analyzer.report import write_markdown, write_preview

from .conftest import spend_row


def _write_info(input_dir: Path, text: str, org: str | None = None) -> None:
    base = input_dir / org if org else input_dir
    (base / "members-info.csv").write_text(text, encoding="utf-8")


# --- 値パース -------------------------------------------------------------

def test_parse_credit_limit_positive():
    assert parse_credit_limit("250") == (250.0, None)
    assert parse_credit_limit("$1,500") == (1500.0, None)
    assert parse_credit_limit(150) == (150.0, None)


def test_parse_credit_limit_zero_disabled():
    assert parse_credit_limit("0") == (0.0, None)
    assert parse_credit_limit(0) == (0.0, None)


def test_parse_credit_limit_unlimited():
    v, w = parse_credit_limit("無制限")
    assert math.isinf(v) and w is None
    assert math.isinf(parse_credit_limit("unlimited")[0])


def test_parse_credit_limit_blank_is_nan():
    v, w = parse_credit_limit("")
    assert math.isnan(v) and w is None
    assert math.isnan(parse_credit_limit(None)[0])
    assert math.isnan(parse_credit_limit(float("nan"))[0])


def test_parse_credit_limit_invalid_warns():
    v, w = parse_credit_limit("たくさん")
    assert math.isnan(v) and w is not None
    v2, w2 = parse_credit_limit("-100")
    assert math.isnan(v2) and w2 is not None


# --- モード導出 -----------------------------------------------------------

def test_credits_mode_by_kappa():
    assert credits_mode(250.0, False) == CREDIT_ENABLED
    assert credits_mode(float("inf"), False) == CREDIT_ENABLED
    assert credits_mode(0.0, False) == CREDIT_DISABLED
    assert credits_mode(float("nan"), False) == CREDIT_UNKNOWN


def test_credits_mode_auto_enable_from_billing():
    # κ 未設定でも実課金が観測されていれば enabled と自動確定
    assert credits_mode(float("nan"), True) == CREDIT_ENABLED


# --- cap_suspected の抑制 --------------------------------------------------

def test_cap_suspected_suppressed_for_enabled_kept_for_disabled(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 45.0, net=0.0),
                     spend_row("b@x.jp", 45.0, net=0.0),
                     spend_row("c@x.jp", 45.0, net=0.0)]},
        members=["a@x.jp,Standard", "b@x.jp,Standard", "c@x.jp,Standard"],
    )
    _write_info(
        input_dir,
        "email,追加クレジット上限\na@x.jp,無制限\nb@x.jp,0\nc@x.jp,\n",
    )
    by = analyze(input_dir, "2026-06", cfg).users.set_index("email")
    # 需要 45 >= 0.85*50 で実課金 0 の Standard → 本来 cap_suspected
    assert bool(by.loc["a@x.jp", "cap_suspected"]) is False   # enabled → 抑制
    assert bool(by.loc["b@x.jp", "cap_suspected"]) is True    # disabled → 維持
    assert bool(by.loc["c@x.jp", "cap_suspected"]) is True    # unknown → 維持
    assert by.loc["a@x.jp", "credits_mode"] == CREDIT_ENABLED
    assert by.loc["b@x.jp", "credits_mode"] == CREDIT_DISABLED
    assert by.loc["c@x.jp", "credits_mode"] == CREDIT_UNKNOWN


# --- κ 到達・整合性警告 ----------------------------------------------------

def test_credit_reach_warning(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=48.0)]},
        members=["a@x.jp,Premium"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,50\n")
    warns = analyze(input_dir, "2026-06", cfg).warnings
    assert any("上限到達" in w and "a@x.jp" in w for w in warns)


def test_integrity_over_cap(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 300.0, net=120.0)]},
        members=["a@x.jp,Premium"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,50\n")
    warns = analyze(input_dir, "2026-06", cfg).warnings
    assert any("上限 κ を超過" in w and "a@x.jp" in w for w in warns)


def test_integrity_disabled_but_billed(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=80.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")
    warns = analyze(input_dir, "2026-06", cfg).warnings
    assert any("無効（κ=0）" in w and "a@x.jp" in w for w in warns)


# --- E 分布 ---------------------------------------------------------------

def test_e_distribution_present_with_billers(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=60.0),
                     spend_row("b@x.jp", 100.0, net=0.0)]},
        members=["a@x.jp,Premium", "b@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    ed = result.e_distribution
    assert ed is not None
    prem = next(g for g in ed["groups"] if g["seat"] == "premium")
    row = next(r for r in prem["rows"] if r["email"] == "a@x.jp")
    assert row["e"] == 140.0   # 200 需要 − 60 実課金
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 込み枠の実測（E = API換算需要 − 実課金）" in md
    # 実課金ゼロの b は billers に含めない
    assert "b@x.jp" not in md.split("込み枠の実測")[1].split("## ")[0]


def test_e_distribution_absent_without_billers(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 50.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert result.e_distribution is None
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "込み枠の実測" not in out.read_text(encoding="utf-8")


def test_e_distribution_none_for_net_spend_basis(make_input, cfg):
    # 修正1: cost_basis=net_spend では需要=課金となり E が無意味なので算出しない
    cfg_net = {**cfg, "cost_basis": "net_spend"}
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=60.0)]},
        members=["a@x.jp,Premium"],
    )
    assert analyze(input_dir, "2026-06", cfg_net).e_distribution is None


def test_e_distribution_ratio_comparison(make_input, cfg, tmp_path):
    # 改善5: シート種別ごとに実測 E 中央値と config allowance(mid) の倍率を添える
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 150.0, net=50.0)]},   # E=100, premium
        members=["a@x.jp,Premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    g = next(x for x in result.e_distribution["groups"] if x["seat"] == "premium")
    assert g["median"] == 100.0
    assert g["allowance_mid"] == 250.0
    assert g["ratio"] == 0.4    # 100 / 250
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "config の allowance（mid $250.00）の 0.4 倍" in out.read_text(encoding="utf-8")


# --- 付与候補 -------------------------------------------------------------

def test_grant_candidate_formal(make_input, cfg, tmp_path):
    # disabled の Standard ユーザで純モデル判定が premium 方向 → 付与候補（超過見込みつき）
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 260.0, net=130.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")
    result = analyze(input_dir, "2026-06", cfg)
    assert [c["email"] for c in result.grant_candidates] == ["a@x.jp"]
    assert result.grant_candidates[0]["added"] == 210.0   # max(0, 260 − 50)
    out = tmp_path / "report.md"
    write_markdown(result, out)
    md = out.read_text(encoding="utf-8")
    assert "## 追加クレジット付与候補" in md
    assert "モデル超過見込み $210.00/月" in md


def test_grant_candidate_disabled_zero_billed_high_demand(make_input, cfg):
    # 修正2の本命: 無効・実課金ゼロ・高需要。実課金拘束後は Standard 推奨だが、
    # 拘束前の純モデル判定は premium 方向のため付与候補に入る（旧ロジックでは漏れていた層）
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 200.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\na@x.jp,0\n")
    result = analyze(input_dir, "2026-06", cfg)
    by = result.users.set_index("email")
    assert by.loc["a@x.jp", "recommended_seat"] == "standard"   # 拘束後は Standard 推奨
    assert [c["email"] for c in result.grant_candidates] == ["a@x.jp"]   # でも付与候補
    assert result.grant_candidates[0]["added"] == 150.0


def test_grant_candidates_sorted_by_overage(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("low@x.jp", 160.0, net=0.0),
                     spend_row("high@x.jp", 240.0, net=0.0)]},
        members=["low@x.jp,Standard", "high@x.jp,Standard"],
    )
    _write_info(input_dir, "email,追加クレジット上限\nlow@x.jp,0\nhigh@x.jp,0\n")
    cands = analyze(input_dir, "2026-06", cfg).grant_candidates
    # モデル超過見込みの降順
    assert [c["email"] for c in cands] == ["high@x.jp", "low@x.jp"]
    assert cands[0]["added"] == 190.0 and cands[1]["added"] == 110.0


def test_grant_candidate_absent_without_credit_column(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 260.0, net=130.0)]},
        members=["a@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert result.grant_candidates == []
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "## 追加クレジット付与候補" not in out.read_text(encoding="utf-8")


# --- 構成サマリ行 ---------------------------------------------------------

def test_credit_summary_composition(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, net=0.0),
                     spend_row("b@x.jp", 10.0, net=0.0),
                     spend_row("c@x.jp", 10.0, net=0.0),
                     spend_row("d@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard", "b@x.jp,Standard",
                 "c@x.jp,Standard", "d@x.jp,Standard"],
    )
    _write_info(
        input_dir,
        "email,追加クレジット上限\na@x.jp,200\nb@x.jp,無制限\nc@x.jp,0\nd@x.jp,\n",
    )
    result = analyze(input_dir, "2026-06", cfg)
    s = result.summary
    assert s["credit_shown"] is True
    assert s["credit_enabled_n"] == 2      # a(200) + b(無制限)
    assert s["credit_cap_total_usd"] == 200.0
    assert s["credit_unlimited_n"] == 1
    assert s["credit_disabled_n"] == 1
    assert s["credit_unknown_n"] == 1
    out = tmp_path / "report.md"
    write_markdown(result, out)
    assert "| 追加クレジット | 有効 2 名" in out.read_text(encoding="utf-8")


# --- 後方互換 -------------------------------------------------------------

def test_no_credit_column_no_mode_column(make_input, cfg):
    input_dir = make_input(
        {"2026-06": [spend_row("a@x.jp", 10.0, net=0.0)]},
        members=["a@x.jp,Standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    # credit 情報が無い入力では credits_mode / credit_limit_usd 列を出力しない
    assert "credits_mode" not in result.users.columns
    assert "credit_limit_usd" not in result.users.columns
    assert result.summary.get("credit_shown") is False


# --- 速報の残額ブロック・付与候補 -----------------------------------------

def test_preview_credit_reach_and_grant(make_input, cfg, tmp_path):
    input_dir = make_input(
        {"2026-07": [spend_row("hit@x.jp", 200.0, net=48.0),
                     spend_row("far@x.jp", 200.0, net=20.0),
                     spend_row("cand@x.jp", 400.0, net=0.0)]},
        members=["hit@x.jp,Premium", "far@x.jp,Premium", "cand@x.jp,Standard"],
        members_month="2026-07",
    )
    _write_info(
        input_dir,
        "email,追加クレジット上限\nhit@x.jp,50\nfar@x.jp,300\ncand@x.jp,0\n",
    )
    result = preview(input_dir, "2026-07", cfg, days_observed=10)
    cr = result.credit_reach
    assert cr is not None
    by = {r["email"]: r for r in cr["rows"]}
    assert by["hit@x.jp"]["reached"] is True         # 48 >= 50-5
    assert by["far@x.jp"]["reached"] is False        # 20 < 295
    # cand は κ=0（disabled）なので残額ブロックには載らない
    assert "cand@x.jp" not in by
    # 付与候補: disabled の Standard で昇格方向（Premium検討/判断保留）
    assert "cand@x.jp" in [c["email"] for c in result.grant_candidates]
    out = tmp_path / "org"
    write_preview(result, out)
    md = (out / "2026-07" / "preview.md").read_text(encoding="utf-8")
    assert "## 追加クレジット残額" in md
    assert "## 追加クレジット付与候補" in md


def test_credit_reach_interval_rate(make_snapshots, cfg):
    # 修正3: スナップショットがあるユーザは「最新区間の課金増分 ÷ 区間日数」を現在レートに
    # 使い、到達予測 = 観測末日 + 残額/レート とする
    input_dir = make_snapshots(
        "2026-07",
        {
            "2026-07-05": [spend_row("a@x.jp", 60.0, net=0.0)],
            "2026-07-13": [spend_row("a@x.jp", 150.0, net=100.0)],   # 区間課金 +100 / 8日
        },
        members=["a@x.jp,Premium"], members_month="2026-07",
    )
    (input_dir / "members-info.csv").write_text(
        "email,追加クレジット上限\na@x.jp,200\n", encoding="utf-8")
    result = preview(input_dir, "2026-07", cfg, days_observed=13)
    row = next(r for r in result.credit_reach["rows"] if r["email"] == "a@x.jp")
    assert row["reached"] is False
    # レート 100/8=12.5/日 → 13 + (200-100)/12.5 = 21 日頃
    assert row["eta_day"] == 21
