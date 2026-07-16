"""config.yaml のロード。"""

from __future__ import annotations

from pathlib import Path

import yaml

from .ingest import REQUIRED_COLUMNS

DEFAULT_CONFIG_PATH = Path("config.yaml")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    for key in ("seats", "decision", "model_prices", "columns"):
        if key not in cfg:
            raise ValueError(f"config.yaml に '{key}' セクションがありません")
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    """料金改定などで config.yaml を編集した際のミスを実行前に検出する。"""
    errors: list[str] = []

    def _num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for seat in ("standard", "premium"):
        s = cfg["seats"].get(seat)
        if not isinstance(s, dict):
            errors.append(f"seats.{seat} がありません")
            continue
        if not _num(s.get("price_usd")) or s["price_usd"] < 0:
            errors.append(f"seats.{seat}.price_usd は 0 以上の数値が必要です")
        allowance = s.get("allowance_usd")
        if not isinstance(allowance, dict):
            errors.append(f"seats.{seat}.allowance_usd がありません")
        else:
            for scenario in ("low", "mid", "high"):
                v = allowance.get(scenario)
                if not _num(v) or v < 0:
                    errors.append(f"seats.{seat}.allowance_usd.{scenario} は 0 以上の数値が必要です")
            if all(_num(allowance.get(k)) for k in ("low", "mid", "high")) and not (
                allowance["low"] <= allowance["mid"] <= allowance["high"]
            ):
                errors.append(f"seats.{seat}.allowance_usd は low <= mid <= high が必要です")
    std, prem = cfg["seats"].get("standard"), cfg["seats"].get("premium")
    if (
        isinstance(std, dict) and isinstance(prem, dict)
        and _num(std.get("price_usd")) and _num(prem.get("price_usd"))
        and prem["price_usd"] <= std["price_usd"]
    ):
        errors.append("seats.premium.price_usd は standard より大きい必要があります")

    d = cfg["decision"]
    if not isinstance(d.get("hysteresis_months"), int) or d["hysteresis_months"] < 1:
        errors.append("decision.hysteresis_months は 1 以上の整数が必要です")
    if not _num(d.get("buffer_ratio")) or not 0 <= d["buffer_ratio"] <= 1:
        errors.append("decision.buffer_ratio は 0〜1 の数値が必要です")
    if not _num(d.get("censoring_margin")) or d["censoring_margin"] <= 0:
        errors.append("decision.censoring_margin は正の数値が必要です")

    patterns = cfg["model_prices"].get("patterns")
    if not isinstance(patterns, list) or not patterns:
        errors.append("model_prices.patterns が空です")
    else:
        for i, pat in enumerate(patterns):
            if not isinstance(pat, dict) or not pat.get("match") \
                    or not _num(pat.get("input")) or not _num(pat.get("output")):
                errors.append(f"model_prices.patterns[{i}] には match/input/output が必要です")
    default = cfg["model_prices"].get("default")
    if not isinstance(default, dict) or not _num(default.get("input")) or not _num(default.get("output")):
        errors.append("model_prices.default には input/output の数値が必要です")

    # 入力処理に必須のカラムエイリアスが columns セクションに定義されているか。
    # 欠けていると実行時に KeyError / 必須カラム未検出になるため起動時に検出する。
    columns = cfg["columns"]
    if not isinstance(columns, dict):
        errors.append("columns セクションが辞書ではありません")
    else:
        for section, required in REQUIRED_COLUMNS.items():
            sec = columns.get(section)
            if not isinstance(sec, dict):
                errors.append(f"columns.{section} がありません")
                continue
            for canonical in required:
                aliases = sec.get(canonical)
                if not isinstance(aliases, list) or not aliases:
                    errors.append(f"columns.{section}.{canonical} のエイリアス定義がありません")

    if errors:
        raise ValueError("config.yaml の設定に問題があります:\n  - " + "\n  - ".join(errors))
