"""シート未割当（Unassigned）メンバーの対象外扱いのテスト。

意図的な未割当（別組織でアサイン済み・管理者等）は損益分岐判定を行わず、
「シート不明」（members 更新漏れ疑い）とは区別する。
"""

import pandas as pd

from seat_analyzer.analyze import analyze, preview

from .conftest import spend_row


def _make(make_input, extra_spend=None):
    # a@x.jp は Premium 妥当な水準（需要大・実課金ゼロ）にして変更推奨が出ないようにする
    spend = [spend_row("a@x.jp", 600.0, net=0.0)] + (extra_spend or [])
    return make_input(
        {"2026-05": [spend_row("a@x.jp", 600.0, net=0.0)], "2026-06": spend},
        members=["a@x.jp,Premium", "admin@x.jp,Unassigned"],
    )


def test_unassigned_is_excluded_from_judgment(cfg, make_input):
    result = analyze(_make(make_input), "2026-06", cfg)
    row = result.users.set_index("email").loc["admin@x.jp"]
    assert row["current_seat"] == "unassigned"
    assert row["status"] == "対象外（シート未割当）"
    assert row["confidence"] == "—"
    assert pd.isna(row["monthly_saving_usd"])
    assert pd.isna(row["cost_if_standard_usd"])


def test_unassigned_counted_separately_in_summary(cfg, make_input):
    result = analyze(_make(make_input), "2026-06", cfg)
    s = result.summary
    assert s["n_unassigned"] == 1
    assert s["n_unknown"] == 0
    assert s["seat_cost_now_usd"] == 125.0            # 未割当はシート費用に含まれない
    assert s["n_change_recommended"] == 0
    # 「判別できない」警告（members 更新漏れ疑い）の対象にならない
    assert not any("判別できない" in w for w in result.warnings)


def test_unassigned_with_usage_warns(cfg, make_input):
    input_dir = _make(make_input, extra_spend=[spend_row("admin@x.jp", 5.0)])
    result = analyze(input_dir, "2026-06", cfg)
    assert any("シート未割当なのに利用実績" in w for w in result.warnings)


def test_unassigned_without_usage_does_not_warn(cfg, make_input):
    result = analyze(_make(make_input), "2026-06", cfg)
    assert not any("利用実績" in w for w in result.warnings)


def test_unassigned_in_preview(cfg, make_input):
    result = preview(_make(make_input), "2026-06", cfg, days_observed=10)
    users = result.users.set_index("email")
    assert users.loc["admin@x.jp", "label"] == "対象外（未割当）"
    assert result.summary["n_unassigned"] == 1
