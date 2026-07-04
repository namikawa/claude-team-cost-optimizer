import pandas as pd

from seat_analyzer import pricing


def test_price_for_model_matching(cfg):
    assert pricing.price_for_model("claude-opus-4-8", cfg) == (5.0, 25.0)
    assert pricing.price_for_model("claude-sonnet-4-6", cfg) == (3.0, 15.0)
    assert pricing.price_for_model("unknown-model", cfg) == (5.0, 25.0)  # default


def test_add_computed_cost_prefers_net_spend(cfg):
    df = pd.DataFrame({
        "email": ["a@x.jp", "b@x.jp"],
        "model": ["claude-sonnet-4-6", "claude-sonnet-4-6"],
        "prompt_tokens": [1_000_000, 1_000_000],
        "completion_tokens": [100_000, 100_000],
        "net_spend": [9.99, None],  # b は欠損 → 計算値 fallback
    })
    out = pricing.add_computed_cost(df, cfg)
    # computed = 1.0*3 + 0.1*15 = 4.5
    assert out["computed_cost_usd"].round(2).tolist() == [4.5, 4.5]
    assert out["cost_usd"].round(2).tolist() == [9.99, 4.5]


def test_validate_spend_warns_on_deviation(cfg):
    df = pd.DataFrame({
        "email": ["a@x.jp"],
        "model": ["claude-sonnet-4-6"],
        "prompt_tokens": [10_000_000],
        "completion_tokens": [1_000_000],
        "net_spend": [100.0],  # computed = 45.0 → 乖離 55%
    })
    df = pricing.add_computed_cost(df, cfg)
    warnings = pricing.validate_spend(df, cfg)
    assert any("乖離" in w for w in warnings)


def test_validate_spend_ok_when_consistent(cfg):
    df = pd.DataFrame({
        "email": ["a@x.jp"],
        "model": ["claude-sonnet-4-6"],
        "prompt_tokens": [10_000_000],
        "completion_tokens": [1_000_000],
        "net_spend": [45.0],
    })
    df = pricing.add_computed_cost(df, cfg)
    assert pricing.validate_spend(df, cfg) == []


def test_cache_aware_computed_cost(cfg):
    # 実スペンドレポートの実測行（opus-4-7, 2026-06）で検算:
    # 18108×5 + 761,608,431×5×0.1 + 38,726,201×5×2.0 (1h) + 6,380,679×25 ≒ 927.67
    df = pd.DataFrame({
        "email": ["a@x.jp"],
        "model": ["claude-opus-4-7"],
        "prompt_tokens": [800_352_740],
        "completion_tokens": [6_380_679],
        "uncached_input_tokens": [18_108],
        "cache_read_tokens": [761_608_431],
        "cache_write_5m_tokens": [0],
        "cache_write_1h_tokens": [38_726_201],
        "net_spend": [927.67],
    })
    out = pricing.add_computed_cost(df, cfg)
    assert abs(out["computed_cost_usd"].iloc[0] - 927.67) < 1.0
    assert pricing.validate_spend(out, cfg) == []
