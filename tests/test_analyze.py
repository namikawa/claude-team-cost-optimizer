"""判定ロジックのテスト（config.yaml デフォルト値前提）。

デフォルト: Standard $25 (allowance mid=50) / Premium $125 (allowance mid=250),
hysteresis=2ヶ月, buffer=0.2 → 最低削減額 $20/月, censoring=0.85 → 閾値 $42.5

コスト算定: 現シート = シート料 + 実課金(billed) の観測実績。変更先 = allowance
モデル試算（込み量の大小関係により観測実課金で上下拘束）。
テストでは spend_row(net=0.0) で「実課金ゼロ・需要は tokens×単価」を、
net=<額> で「実課金あり」を表現する（net 省略時は net == API等価額）。
"""

from seat_analyzer.analyze import analyze
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
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
    u = _user(result, "ghost@x.jp")
    assert u["api_cost_usd"] == 0.0
    assert u["recommended_seat"] == "standard"
    assert u["status"] == "変更推奨"


def test_orphan_spend_user_is_unknown_seat(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("orphan@x.jp", 10.0)]},
        members=["someone@x.jp,standard"],
    )
    result = analyze(input_dir, "2026-06", cfg)
    assert _user(result, "orphan@x.jp")["status"] == "シート不明"
    assert any("members に存在しない" in w for w in result.warnings)


def test_single_month_data_is_watch(cfg, make_input):
    input_dir = make_input(
        {"2026-06": [spend_row("light@x.jp", 20.0)]},
        members=["light@x.jp,premium"],
    )
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
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
    result = analyze(input_dir, "2026-06", cfg)
    s = result.summary
    assert s["n_members"] == 2
    assert s["n_premium"] == 1 and s["n_standard"] == 1
    assert s["seat_cost_now_usd"] == 150.0
    assert s["n_change_recommended"] == 1
    assert s["est_monthly_saving_usd"] == 100.0


def test_org_service_rows_excluded_from_seat_table(cfg, make_input, tmp_path):
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
    result = analyze(input_dir, "2026-06", cfg)
    assert "(org service usage)" not in set(result.users["email"])
    assert result.summary["org_service_cost_usd"] == 500.0
    assert result.summary["org_service_by_product"] == {"Code Review": 500.0}
    assert result.summary["total_api_cost_usd"] == 10.0
    assert not any("members に存在しない" in w for w in result.warnings)
