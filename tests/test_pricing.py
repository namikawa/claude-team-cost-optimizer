import copy

import pandas as pd

from seat_analyzer import ingest, pricing


def test_spend_optional_columns_do_not_overlap_cache_columns():
    assert set(ingest.SPEND_OPTIONAL_COLUMNS).isdisjoint(pricing.CACHE_COLS)


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
    # cost_usd の採用（net_spend 優先・欠損は計算値）は apply_cost_basis に一本化
    out = pricing.apply_cost_basis(out, "net_spend")
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


# --- キャッシュ読取の倍率（モデル別・2026-09-02 公式照合） ---

def _cache_read_only(models: list[str], tokens: int = 1_000_000) -> pd.DataFrame:
    """キャッシュ読取だけを持つ行（読取の倍率だけがコストに現れる）。"""
    n = len(models)
    return pd.DataFrame({
        "email": [f"u{i}@x.jp" for i in range(n)],
        "model": models,
        "prompt_tokens": [tokens] * n,
        "completion_tokens": [0] * n,
        "uncached_input_tokens": [0] * n,
        "cache_read_tokens": [tokens] * n,
        "cache_write_5m_tokens": [0] * n,
        "cache_write_1h_tokens": [0] * n,
    })


def test_cache_read_multiplier_is_per_model(cfg):
    """Fable 5.1 / Mythos 5.1 のキャッシュ読取は 0.025 倍で、無印は既定の 0.1 倍のまま。

    倍率を全モデル共通の1値で掛けると、キャッシュ読取が入力の大半を占めるユーザの
    需要が4倍に膨らむ。パターンの評価順（5-1 が汎用より上）もここで固定する。
    """
    models = ["claude-fable-5-1", "claude-fable-5", "claude-mythos-5-1", "claude-mythos-5"]
    out = pricing.add_computed_cost(_cache_read_only(models), cfg)

    # 基本入力単価は4モデルとも $10/1M なので、差は読取の倍率だけ
    assert out["computed_cost_usd"].round(4).tolist() == [0.25, 1.00, 0.25, 1.00]


def test_cache_read_multiplier_falls_back_to_cache_multipliers(cfg):
    """cache_read を持たないパターンは cache_multipliers.read を使う。"""
    cfg = copy.deepcopy(cfg)
    cfg["cache_multipliers"]["read"] = 0.5
    models = ["claude-sonnet-4-6", "claude-fable-5", "claude-fable-5-1", "brand-new-model"]

    assert [pricing.cache_read_multiplier_for_model(m, cfg) for m in models] == \
        [0.5, 0.5, 0.025, 0.5]
    out = pricing.add_computed_cost(_cache_read_only(models), cfg)
    # 入力単価 3.0 / 10.0 / 10.0 / 5.0（default）× 読取の倍率
    assert out["computed_cost_usd"].round(4).tolist() == [1.5, 5.0, 0.25, 2.5]


def test_unmatched_models_covers_new_patterns(cfg):
    """新しい単価パターンのモデル名は「単価表に無い」警告の対象にしない。"""
    models = ["claude-fable-5-1", "claude-mythos-5-1", "claude-mythos-5", "brand-new-model"]

    assert pricing.unmatched_models(models, cfg) == ["brand-new-model"]
