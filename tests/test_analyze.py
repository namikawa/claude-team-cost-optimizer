"""判定ロジックのテスト（config.yaml デフォルト値前提）。

デフォルト: Standard $25 (allowance mid=50) / Premium $125 (allowance mid=250),
hysteresis=2ヶ月, buffer=0.2 → 最低削減額 $20/月, censoring=0.85 → 閾値 $42.5

コスト算定: 現シート = シート料 + 実課金(billed) の観測実績。変更先 = allowance
モデル試算（込み量の大小関係により観測実課金で上下拘束）。
テストでは spend_row(net=0.0) で「実課金ゼロ・需要は tokens×単価」を、
net=<額> で「実課金あり」を表現する（net 省略時は net == API等価額）。
"""

import math
from pathlib import Path

import pytest
import yaml
from pandas.testing import assert_frame_equal

from seat_analyzer.analyze import (
    STATUS_EXCLUDED,
    STATUS_FIXED_SEAT,
    WorkspaceContext,
    analyze,
    analyze_org,
)
from seat_analyzer.analyze.pipeline import credit_limit_for
from seat_analyzer.config import load_config
from tests.conftest import spend_row


def _user(result, email):
    return result.users.set_index("email").loc[email]


def test_premium_light_user_downgrade_recommended(cfg, make_input):
    # 実課金ゼロ・需要が小さい Premium ユーザ → Standard へ（差額まるごと削減）
    input_dir = make_input(
        {
            "2026-05": [spend_row("light@x.jp", 20.0, net=0.0)],
            "2026-06": [spend_row("light@x.jp", 22.0, net=0.0)],
        },
        members=["light@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    u = _user(result, "light@x.jp")
    assert u["recommended_seat"] == "standard"
    assert u["status"] == "変更推奨"
    assert u["monthly_saving_usd"] == 100.0  # 観測 125 → 試算 25（allowance内）


def test_premium_single_low_month_is_watch(cfg, make_input):
    # 先月は需要大（Standard 試算が高くつく）、今月だけ低利用 → 要観察止まり
    input_dir = make_input(
        {
            "2026-05": [spend_row("spiky@x.jp", 500.0, net=0.0)],
            "2026-06": [spend_row("spiky@x.jp", 20.0, net=0.0)],
        },
        members=["spiky@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "spiky@x.jp")["status"] == "要観察"


def test_standard_heavy_billed_user_upgrade_recommended(cfg, make_input):
    # 実課金 $250/月 が2ヶ月継続 → Premium（観測 275 vs 試算 125）
    input_dir = make_input(
        {
            "2026-05": [spend_row("heavy@x.jp", 300.0, model="claude-opus-4-8", net=250.0)],
            "2026-06": [spend_row("heavy@x.jp", 300.0, model="claude-opus-4-8", net=250.0)],
        },
        members=["heavy@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    u = _user(result, "heavy@x.jp")
    assert u["recommended_seat"] == "premium"
    assert u["status"] == "変更推奨"
    # 需要は computed 基準 (cost_basis: computed) = 300
    # std 観測: 25 + 実課金250 = 275
    # prem 試算: 125 + min(モデル超過 300-250=50, 実課金250) = 175
    assert u["cost_if_standard_usd"] == 275.0
    assert u["cost_if_premium_usd"] == 175.0
    assert u["monthly_saving_usd"] == 100.0


def test_standard_billed_zero_never_upgraded(cfg, make_input):
    # 需要が大きくても実課金ゼロなら Standard の実コストは $25 → 昇格推奨しない
    input_dir = make_input(
        {
            "2026-05": [spend_row("free@x.jp", 300.0, net=0.0)],
            "2026-06": [spend_row("free@x.jp", 300.0, net=0.0)],
        },
        members=["free@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    u = _user(result, "free@x.jp")
    assert u["cost_if_standard_usd"] == 25.0
    assert u["recommended_seat"] == "standard"
    assert u["status"] == "現状維持"


def test_standard_near_cap_flagged(cfg, make_input):
    # 実課金ゼロ & 需要が込み量推定(mid=50)の85%超 → 上限到達疑いフラグ
    input_dir = make_input(
        {
            "2026-05": [spend_row("cap@x.jp", 45.0, net=0.0)],
            "2026-06": [spend_row("cap@x.jp", 45.0, net=0.0)],
        },
        members=["cap@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    u = _user(result, "cap@x.jp")
    assert bool(u["cap_suspected"]) is True
    assert u["status"] == "現状維持"


def test_zero_usage_premium_member_included(cfg, make_input):
    input_dir = make_input(
        {
            "2026-05": [spend_row("other@x.jp", 5.0)],
            "2026-06": [spend_row("other@x.jp", 5.0)],
        },
        members=["ghost@x.jp,premium", "other@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    u = _user(result, "ghost@x.jp")
    assert u["api_cost_usd"] == 0.0
    assert u["recommended_seat"] == "standard"
    assert u["status"] == "変更推奨"


def test_orphan_spend_user_is_unknown_seat(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("orphan@x.jp", 10.0)]},
        members=["someone@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "orphan@x.jp")["status"] == "シート不明"
    assert any("members に存在しない" in w for w in result.warnings)


def test_single_month_data_is_watch(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("light@x.jp", 20.0)]},
        members=["light@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert _user(result, "light@x.jp")["status"] == "要観察（データ蓄積待ち）"


def test_break_even_boundary_is_not_recommended(cfg, make_input):
    # 需要150・実課金0: std 試算 = 25+100 = 125, prem 観測 = 125 → 同額。削減0はバッファ未満。
    input_dir = make_input(
        {
            "2026-05": [spend_row("even@x.jp", 150.0, net=0.0)],
            "2026-06": [spend_row("even@x.jp", 150.0, net=0.0)],
        },
        members=["even@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    u = _user(result, "even@x.jp")
    assert u["cost_if_standard_usd"] == u["cost_if_premium_usd"] == 125.0
    assert u["status"] != "変更推奨"


def test_summary_counts(cfg, make_input):
    input_dir = make_input(
        {
            "2026-05": [spend_row("light@x.jp", 20.0, net=0.0), spend_row("std@x.jp", 10.0, net=0.0)],
            "2026-06": [spend_row("light@x.jp", 20.0, net=0.0), spend_row("std@x.jp", 10.0, net=0.0)],
        },
        members=["light@x.jp,premium", "std@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    s = result.summary
    assert s["n_members"] == 2
    assert s["n_premium"] == 1 and s["n_standard"] == 1
    assert s["seat_cost_now_usd"] == 150.0
    assert s["n_change_recommended"] == 1
    assert s["est_monthly_saving_usd"] == 100.0


def test_org_service_rows_excluded_from_seat_table(cfg, make_input):
    # "(org service usage)" のような @ を含まない行はシート判定から除外し別枠計上
    input_dir = make_input(
        {
            "2026-05": [spend_row("a@x.jp", 10.0)],
            "2026-06": [
                spend_row("a@x.jp", 10.0),
                spend_row("(org service usage)", 500.0, product="Code Review"),
            ],
        },
        members=["a@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, org="org-a")
    assert "(org service usage)" not in set(result.users["email"])
    assert result.summary["org_service_cost_usd"] == 500.0
    assert result.summary["org_service_by_product"] == {"Code Review": 500.0}
    assert result.summary["total_api_cost_usd"] == 10.0
    assert not any("members に存在しない" in w for w in result.warnings)


# --- 複数 workspace（設計書 §26。組織 = workspace 1つ以上）--------------------
#
# アカウント層＝(email, workspace) の集計・判定は従来どおり workspace ごとに行い、
# analyze_org はその分割と束ね方だけを受け持つ。人（email）単位の結合は後段の担当。

ORG = "org-x"
COLUMNS_FROM_SPEND = ["email", "api_cost_usd", "billed_extra_usd",
                      "prompt_tokens", "completion_tokens", "product_breakdown"]


def _workspace_cfg(tmp_path: Path, workspaces: dict, org: str = ORG, **org_keys) -> dict:
    """組織の workspaces を書いた上書き設定をロードする（既定設定に重ねる）。"""
    path = tmp_path / "config-workspaces.yaml"
    path.write_text(
        yaml.safe_dump(
            {"organizations": {org: {"workspaces": workspaces, **org_keys}}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return load_config(str(path))


def _write_members_info(input_dir: Path, text: str, org: str = ORG) -> None:
    """組織直下の members-info.csv（人単位の任意入力）を置く。"""
    (input_dir / org / "members-info.csv").write_text(text, encoding="utf-8")


def _two_workspace_input(make_input) -> Path:
    """main / second の2 workspace を持つ入れ子レイアウトの組織を組む。"""
    rows = {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]}
    members = ["alice@example.com,standard"]
    input_dir = make_input(rows, members=members, org=ORG, workspace="main")
    make_input(rows, members=members, org=ORG, workspace="second")
    return input_dir


def test_analyze_org_single_layout_matches_analyze(cfg, make_input):
    # 従来レイアウトは「workspace が1つの組織」。中身は analyze() の戻りと完全に同じ
    input_dir = make_input(
        {
            "2026-05": [spend_row("alice@example.com", 20.0, net=0.0)],
            "2026-06": [spend_row("alice@example.com", 22.0, net=0.0)],
        },
        members=["alice@example.com,premium"], org=ORG,
    )
    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    expected = analyze(input_dir / ORG, "2026-06", cfg, ORG)

    assert list(result.workspaces) == [ORG]
    assert result.primary == ORG
    assert result.skipped == ()
    assert result.warnings == []
    only = result.workspaces[ORG]
    assert_frame_equal(only.users, expected.users)
    assert only.summary == expected.summary
    assert only.warnings == expected.warnings
    assert only.months_used == expected.months_used
    assert only.sources == expected.sources
    context = result.contexts[ORG]
    assert (context.primary, context.label, context.fixed_seat) == (True, ORG, None)
    assert context.credit_limit_default_usd is None


def test_single_layout_with_configured_workspaces_is_error(tmp_path, make_input):
    # 設定だけ入れ子レイアウトの想定になっている状態。黙って片方を採らない
    input_dir = make_input(
        {"2026-06": [spend_row("alice@example.com", 20.0, net=0.0)]},
        members=["alice@example.com,premium"], org=ORG,
    )
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "config にあるがディレクトリが無い: main/second" in str(exc.value)


def test_nested_single_workspace_matches_flat_layout(tmp_path, make_input):
    # workspaces を1つだけ書いた組織は、同じファイルを従来レイアウトに置いた結果と一致
    rows = {
        "2026-05": [spend_row("alice@example.com", 20.0, net=0.0)],
        "2026-06": [spend_row("alice@example.com", 22.0, net=0.0),
                    spend_row("bob@example.com", 300.0, net=0.0)],
    }
    members = ["alice@example.com,premium", "bob@example.com,standard"]
    input_dir = make_input(rows, members=members, org=ORG, workspace="main")
    make_input(rows, members=members, org="org-flat")
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}})

    nested = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    flat = analyze(input_dir / "org-flat", "2026-06", cfg, "org-flat")
    assert nested.primary == "main"
    assert_frame_equal(nested.workspaces["main"].users, flat.users)
    assert nested.workspaces["main"].summary == flat.summary


def test_two_workspaces_match_standalone_runs(tmp_path, make_input):
    # 各 workspace の集計値は、その workspace を単独の組織として実行した結果と一致する
    input_dir = make_input(
        {
            "2026-05": [spend_row("alice@example.com", 100.0, net=0.0)],
            "2026-06": [spend_row("alice@example.com", 120.0, net=0.0),
                        spend_row("bob@example.com", 30.0, net=0.0, product="Claude Chat")],
        },
        members=["alice@example.com,premium", "bob@example.com,standard"],
        org=ORG, workspace="main",
    )
    make_input(
        {"2026-06": [spend_row("alice@example.com", 40.0, net=0.0,
                               model="claude-opus-4-8")]},
        members=["alice@example.com,standard"], org=ORG, workspace="second",
    )
    _write_members_info(
        input_dir,
        "email,部署\nalice@example.com,推進部\nbob@example.com,基盤部\n",
    )
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert list(result.workspaces) == ["main", "second"]
    for name in ("main", "second"):
        standalone = analyze(input_dir / ORG / name, "2026-06", cfg, ORG)
        assert_frame_equal(
            result.workspaces[name].users[COLUMNS_FROM_SPEND],
            standalone.users[COLUMNS_FROM_SPEND],
        )
    # members-info は人単位なので、組織直下の1つを全 workspace のアカウントが読む
    main_dept = result.workspaces["main"].users.set_index("email")["department"]
    second_dept = result.workspaces["second"].users.set_index("email")["department"]
    assert main_dept["alice@example.com"] == "推進部"
    assert main_dept["bob@example.com"] == "基盤部"
    assert second_dept["alice@example.com"] == "推進部"


def test_primary_workspace_comes_first(tmp_path, make_input):
    # 並びは主が先、以降は名前の昇順（名前の昇順だけなら alpha が先になる構成）
    rows = {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]}
    members = ["alice@example.com,standard"]
    input_dir = make_input(rows, members=members, org=ORG, workspace="alpha")
    make_input(rows, members=members, org=ORG, workspace="beta")
    cfg = _workspace_cfg(tmp_path, {"alpha": {}, "beta": {"primary": True}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert list(result.workspaces) == ["beta", "alpha"]
    assert list(result.contexts) == ["beta", "alpha"]
    assert result.primary == "beta"


def test_credit_limit_column_is_read_for_primary_only(tmp_path, make_input):
    # 主は members-info の列（空欄なら既定値）、副は列を読まず workspace の既定値
    rows = {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]}
    members = ["alice@example.com,standard", "bob@example.com,standard"]
    input_dir = make_input(rows, members=members, org=ORG, workspace="main")
    make_input(rows, members=members, org=ORG, workspace="second")
    _write_members_info(
        input_dir,
        "email,追加クレジット上限\nalice@example.com,250\nbob@example.com,\n",
    )
    cfg = _workspace_cfg(tmp_path, {
        "main": {"primary": True, "credit_limit_default_usd": 100},
        "second": {"credit_limit_default_usd": 50},
    })

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    main = result.workspaces["main"].users.set_index("email")["credit_limit_usd"]
    second = result.workspaces["second"].users.set_index("email")["credit_limit_usd"]
    assert main["alice@example.com"] == 250.0
    assert main["bob@example.com"] == 100.0
    assert second["alice@example.com"] == 50.0
    assert second["bob@example.com"] == 50.0


def test_secondary_without_default_credit_limit_is_unknown(tmp_path, make_input):
    # 既定値の無い副は κ 不明。誰も分からない workspace では従来どおり列ごと出さない
    rows = {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]}
    members = ["alice@example.com,standard"]
    input_dir = make_input(rows, members=members, org=ORG, workspace="main")
    make_input(rows, members=members, org=ORG, workspace="second")
    _write_members_info(
        input_dir, "email,追加クレジット上限\nalice@example.com,250\n")
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert result.workspaces["main"].users["credit_limit_usd"].tolist() == [250.0]
    assert "credit_limit_usd" not in result.workspaces["second"].users.columns
    assert result.workspaces["second"].summary["credit_shown"] is False


def test_credit_limit_for_resolves_account_limit():
    # κ の解決は1関数に閉じる（副の人ごとの上書きを入れるときもここで解決する）
    def context(primary: bool, default):
        return WorkspaceContext(
            name="w", primary=primary, label="w", fixed_seat=None,
            credit_limit_default_usd=default, evaluation_months=None,
        )

    nan = float("nan")
    assert credit_limit_for(250.0, context(True, 100.0)) == 250.0
    assert credit_limit_for(nan, context(True, 100.0)) == 100.0
    assert math.isnan(credit_limit_for(nan, context(True, None)))
    assert credit_limit_for(250.0, context(False, 50.0)) == 50.0
    assert math.isnan(credit_limit_for(250.0, context(False, None)))
    # 単一 workspace の組織（workspace を渡さない従来の呼び出し）は列がそのまま κ
    assert credit_limit_for(250.0, None) == 250.0
    assert math.isnan(credit_limit_for(nan, None))


def _fixed_seat_input(make_input):
    """副に Premium の遊休ユーザと Standard の高需要ユーザが居る構成を組む。"""
    input_dir = make_input(
        {
            "2026-05": [spend_row("alice@example.com", 5.0, net=0.0)],
            "2026-06": [spend_row("alice@example.com", 5.0, net=0.0)],
        },
        members=["alice@example.com,standard"], org=ORG, workspace="main",
    )
    make_input(
        {
            "2026-05": [spend_row("carol@example.com", 10.0, net=0.0),
                        spend_row("erin@example.com", 300.0, net=0.0)],
            "2026-06": [spend_row("carol@example.com", 12.0, net=0.0),
                        spend_row("erin@example.com", 320.0, net=0.0)],
        },
        members=["carol@example.com,premium", "erin@example.com,standard",
                 "dave@example.com,unassigned"],
        org=ORG, workspace="second",
    )
    return input_dir


def test_without_fixed_seat_secondary_is_judged_as_before(tmp_path, make_input):
    # 固定シートを書かない副は従来どおり V1 判定（下のテストとの対照）
    input_dir = _fixed_seat_input(make_input)
    cfg = _workspace_cfg(tmp_path, {
        "main": {"primary": True}, "second": {"credit_limit_default_usd": 0},
    })
    second = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).workspaces["second"]
    users = second.users.set_index("email")

    assert users.loc["carol@example.com", "status"] == "変更推奨"
    assert users.loc["carol@example.com", "monthly_saving_usd"] == 100.0
    assert bool(users.loc["erin@example.com", "cap_suspected"]) is True
    assert [c["email"] for c in second.grant_candidates] == ["erin@example.com"]
    assert second.summary["n_change_recommended"] == 1
    assert second.summary["n_cap_suspected"] == 1


def test_fixed_seat_workspace_is_excluded_from_v1_decision(tmp_path, make_input):
    # 運用方針でシート種別が決まっている副は、損益分岐で選び直す対象にしない
    input_dir = _fixed_seat_input(make_input)
    cfg = _workspace_cfg(tmp_path, {
        "main": {"primary": True},
        "second": {"fixed_seat": "premium", "credit_limit_default_usd": 0},
    })
    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    second = result.workspaces["second"]
    users = second.users.set_index("email")

    carol = users.loc["carol@example.com"]
    assert carol["status"] == STATUS_FIXED_SEAT
    assert carol["recommended_seat"] == "premium"
    assert carol["monthly_saving_usd"] is None
    assert math.isnan(carol["cost_if_standard_usd"])
    assert math.isnan(carol["cost_if_premium_usd"])
    assert carol["confidence"] == "—"
    # 現状費用（シート料＋実課金）は組織のシート費として残す
    assert carol["cost_current_usd"] == 125.0

    erin = users.loc["erin@example.com"]
    assert erin["status"] == STATUS_FIXED_SEAT
    assert bool(erin["cap_suspected"]) is False
    assert second.grant_candidates == []
    # シート未割当は固定シートの workspace でも従来どおりの対象外
    assert users.loc["dave@example.com", "status"] == STATUS_EXCLUDED

    assert second.summary["n_change_recommended"] == 0
    assert second.summary["n_watching"] == 0
    assert second.summary["n_cap_suspected"] == 0
    # シート内訳とシート費は費用の実態なので数える
    assert second.summary["n_premium"] == 1
    assert second.summary["n_standard"] == 1
    assert second.summary["seat_cost_now_usd"] == 150.0
    # 主 workspace の判定は従来どおり
    assert result.workspaces["main"].users.set_index("email").loc[
        "alice@example.com", "status"] == "現状維持"


def test_not_started_workspace_is_skipped(tmp_path, make_input):
    # 対象月以前に spend が1つも無い副は、過去月の再生成を止めないよう飛ばす
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 10.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 12.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="main",
    )
    make_input(
        {"2026-07": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="second",
    )
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert list(result.workspaces) == ["main"]
    assert result.skipped == ("second",)
    assert list(result.contexts) == ["main", "second"]
    assert len(result.warnings) == 1
    assert "workspace second" in result.warnings[0]
    assert "対象外" in result.warnings[0]


def _missing_month_input(make_input) -> Path:
    """副が 2026-05 で始まっているのに対象月 2026-06 の spend が無い構成を組む。"""
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 10.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 12.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="main",
    )
    make_input(
        {"2026-05": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,premium", "bob@example.com,standard"],
        org=ORG, workspace="second",
    )
    return input_dir


def test_missing_target_month_needs_allow_missing(tmp_path, make_input):
    # 始まっているのに対象月が無い副は、指定が無ければ止める
    input_dir = _missing_month_input(make_input)
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    with pytest.raises(FileNotFoundError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "workspace second" in str(exc.value)
    assert "--allow-missing-workspace second" in str(exc.value)


def test_allow_missing_workspace_is_analyzed_as_zero_usage(tmp_path, make_input):
    # 利用が無くエクスポートしなかった月の逃げ道。シートは従来どおり数える
    input_dir = _missing_month_input(make_input)
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG,
                         allow_missing={"second"})
    second = result.workspaces["second"]
    assert second.months_used == ["2026-05", "2026-06"]
    assert second.sources["spend"]["2026-06"] == "(利用なしとして扱う)"
    assert second.users["api_cost_usd"].tolist() == [0.0, 0.0]
    assert second.summary["total_api_cost_usd"] == 0.0
    assert second.summary["n_premium"] == 1 and second.summary["n_standard"] == 1
    assert second.summary["seat_cost_now_usd"] == 150.0
    assert sum("需要 0" in w for w in second.warnings) == 1
    assert [w for w in result.warnings if "workspace second" in w] != []


def test_zero_usage_month_keeps_decision_context(tmp_path, make_input):
    # V2 の材料も需要 0 の月を含めて組める（対象月の集計と特徴量は行ゼロで並ぶ）
    input_dir = _missing_month_input(make_input)
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG,
                         decision_context=True, allow_missing={"second"})
    context = result.workspaces["second"].decision_context
    assert context.months == ("2026-05", "2026-06")
    assert context.complete == {"2026-05": True, "2026-06": True}
    assert context.aggregates["2026-06"].empty
    assert context.product_usage["2026-06"].features.empty
    assert list(context.identity_rows.columns) == ["email", "account_uuid", "user_id"]


def test_workspaces_missing_from_config_is_error(tmp_path, make_input):
    input_dir = _two_workspace_input(make_input)
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}})
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "ディレクトリがあるが config に無い: second" in str(exc.value)


def test_workspace_directory_missing_is_error(tmp_path, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="main",
    )
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "third": {}})
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "config にあるがディレクトリが無い: third" in str(exc.value)


def test_nested_layout_without_config_workspaces_is_error(cfg, make_input):
    input_dir = _two_workspace_input(make_input)
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "workspaces がありません" in str(exc.value)
    assert "main/second" in str(exc.value)


def test_primary_workspace_must_be_decided(cfg, make_input):
    # 設定のロードが通常は止める条件。設定を直接組んだ場合でも分析を始めない
    input_dir = _two_workspace_input(make_input)
    cfg["organizations"] = {ORG: {"workspaces": {"main": {}, "second": {}}}}
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "主 workspace が1つに決まりません" in str(exc.value)


def test_invalid_workspace_directory_name_is_error(cfg, make_input):
    # workspace 名はディレクトリ名として発見するので、組織名と同じ規則で検証する
    input_dir = make_input(
        {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="main",
    )
    make_input(
        {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="summary",
    )
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "summary" in str(exc.value)


def test_mixed_layout_is_error(cfg, make_input):
    # 直下と子の両方に spend/ がある状態は、どちらのレイアウトとしても読まない
    input_dir = make_input(
        {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG,
    )
    make_input(
        {"2026-06": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="second",
    )
    with pytest.raises(ValueError) as exc:
        analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert "混在" in str(exc.value)


def _write_kappa_snapshot(base: Path, date: str, rows: list[str]) -> None:
    """日付つき members-info（追加クレジット上限 κ のスナップショット）を置く。"""
    (base / f"members-info-snap-{date}.csv").write_text(
        "email,追加クレジット上限\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_kappa_changes_are_detected_for_primary_workspace_only(tmp_path, make_input):
    # κ は組織直下の members-info の設定。主はその月中変更を検出し、副は検出しない
    rows = {"2026-07": [spend_row("alice@example.com", 10.0, net=0.0)]}
    members = ["alice@example.com,Standard"]
    input_dir = make_input(rows, members=members, members_month="2026-07",
                           org=ORG, workspace="main")
    make_input(rows, members=members, members_month="2026-07",
               org=ORG, workspace="second")
    make_input(rows, members=members, members_month="2026-07", org="org-flat")
    for base in (input_dir / ORG, input_dir / "org-flat"):
        _write_kappa_snapshot(base, "2026-07-05", ["alice@example.com,50"])
        _write_kappa_snapshot(base, "2026-07-20", ["alice@example.com,250"])
    cfg = _workspace_cfg(tmp_path, {
        "main": {"primary": True}, "second": {"credit_limit_default_usd": 100},
    })

    result = analyze_org(input_dir / ORG, "2026-07", cfg, ORG)
    flat = analyze(input_dir / "org-flat", "2026-07", cfg, "org-flat")
    main = result.workspaces["main"]
    # 主の検出内容は、同じファイルを従来レイアウトに置いた場合と同じ
    assert main.member_changes["credit_changes"] == flat.member_changes["credit_changes"]
    assert [(c["from"], c["to"]) for c in main.member_changes["credit_changes"]] == [
        ("$50.00", "$250")]
    assert sum("追加クレジット上限の変更を検出" in w for w in main.warnings) == 1
    assert main.users.set_index("email").loc["alice@example.com", "credit_limit_usd"] == 250.0

    # 副のアカウントの κ は workspace の既定値なので、この変更としては読まない
    second = result.workspaces["second"]
    assert second.member_changes is None
    assert not any("追加クレジット上限の変更を検出" in w for w in second.warnings)
    assert second.users.set_index("email").loc["alice@example.com", "credit_limit_usd"] == 100.0


def test_allow_missing_workspace_without_members_rows(tmp_path, make_input):
    # 需要 0 の代替かつ members が0行でも、人数・費用 0 の結果として完走する
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 10.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 12.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="main",
    )
    make_input(
        {"2026-05": [spend_row("alice@example.com", 10.0, net=0.0)]},
        members=[], org=ORG, workspace="second",
    )
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG,
                         allow_missing={"second"})
    second = result.workspaces["second"]
    assert len(second.users) == 0
    # 列と並びは通常の結果と同じ（後段が列を前提にしているため）
    assert list(second.users.columns) == list(result.workspaces["main"].users.columns)
    assert second.summary["n_members"] == 0
    assert second.summary["n_standard"] == 0 and second.summary["n_premium"] == 0
    assert second.summary["seat_cost_now_usd"] == 0.0
    assert second.summary["total_api_cost_usd"] == 0.0
    assert second.summary["n_change_recommended"] == 0
    assert second.summary["n_cap_suspected"] == 0
    assert second.e_distribution is None
    assert second.grant_candidates == []


def _auto_basis_cfg(tmp_path: Path) -> dict:
    """需要基準の自動判定（cost_basis: auto）を有効にした設定。"""
    path = tmp_path / "config-auto-basis.yaml"
    path.write_text("cost_basis: auto\n", encoding="utf-8")
    return load_config(str(path))


def test_zero_usage_month_keeps_cost_basis_of_past_months(tmp_path, make_input):
    # 空の明細から基準を決めると、過去月の需要指標まで取り違えたまま全月へ適用される
    cfg = _auto_basis_cfg(tmp_path)
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 100.0, net=80.0)]},
        members=["alice@example.com,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, "org-x", assume_no_usage=True)
    # net_spend 基準（80）が全月に適用される。computed 基準に倒れると 100 になる
    assert result.monthly["2026-05"]["api_cost"].tolist() == [80.0]
    assert not any("spend列" in w for w in result.warnings)


def test_zero_usage_month_counts_as_a_month_of_no_demand(cfg, make_input):
    # 欠月は「需要 0・実課金 0 の月」として数える（データ蓄積待ちにはならない）
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 20.0, net=0.0)]},
        members=["alice@example.com,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg, "org-x", assume_no_usage=True)
    user = result.users.set_index("email").loc["alice@example.com"]
    assert result.months_used == ["2026-05", "2026-06"]
    assert user["api_cost_usd"] == 0.0
    assert user["billed_extra_usd"] == 0.0
    assert user["status"] == "変更推奨"
    assert user["monthly_saving_usd"] == 100.0


def test_monthly_history_is_on_the_result(cfg, make_input):
    # 月次の履歴（判定に使った表そのもの）を結果から読める
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 20.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 22.0, net=0.0),
                     spend_row("bob@example.com", 5.0, net=0.0)]},
        members=["alice@example.com,premium", "bob@example.com,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg, "org-x")
    assert set(result.monthly) == {"2026-05", "2026-06"}
    assert list(result.monthly["2026-06"].columns) == [
        "email", "api_cost", "prompt_tokens", "completion_tokens", "billed",
        "product_breakdown", "model_breakdown",
    ]
    latest = result.monthly["2026-06"].set_index("email")["api_cost"]
    assert round(float(latest["alice@example.com"]), 2) == 22.0
    assert round(float(latest["bob@example.com"]), 2) == 5.0
    assert result.users.set_index("email").loc[
        "alice@example.com", "api_cost_usd"] == 22.0


def test_monthly_history_is_per_workspace(tmp_path, make_input):
    # ヒステリシスの履歴は workspace ごと（副が始まる前の月を需要 0 として数えない）
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 20.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 22.0, net=0.0)]},
        members=["alice@example.com,premium"], org=ORG, workspace="main",
    )
    make_input(
        {"2026-06": [spend_row("alice@example.com", 8.0, net=0.0)]},
        members=["alice@example.com,standard"], org=ORG, workspace="second",
    )
    cfg = _workspace_cfg(tmp_path, {"main": {"primary": True}, "second": {}})

    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    assert set(result.workspaces["main"].monthly) == {"2026-05", "2026-06"}
    assert set(result.workspaces["second"].monthly) == {"2026-06"}
    second = result.workspaces["second"].monthly["2026-06"].set_index("email")
    assert round(float(second.loc["alice@example.com", "api_cost"]), 2) == 8.0
