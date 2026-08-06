"""config.yaml のロード。"""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from .ingest import MEMBERS_OPTIONAL_COLUMNS, REQUIRED_COLUMNS, SPEND_OPTIONAL_COLUMNS

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

    def _int(v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    def _finite(v) -> bool:
        # 巨大な int は float 変換で OverflowError になる。設定ミスとして扱う
        try:
            return _num(v) and math.isfinite(v)
        except OverflowError:
            return False

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

    # discussion は任意セクション（未指定なら discussion.DEFAULTS が使われる）
    disc = cfg.get("discussion")
    if disc is not None:
        if not isinstance(disc, dict):
            errors.append("discussion セクションが辞書ではありません")
        else:
            for key in ("command", "model", "effort"):
                if key in disc and not (isinstance(disc[key], str) and disc[key].strip()):
                    errors.append(f"discussion.{key} は空でない文字列が必要です")
            efforts = ("low", "medium", "high", "xhigh", "max")
            if "effort" in disc and disc["effort"] not in efforts:
                errors.append(f"discussion.effort は {'/'.join(efforts)} のいずれかが必要です")
            # 回数は int() で黙って切り捨てられると意図と違う挙動になるため整数を要求する。
            # 秒数は inf/NaN を弾く（time.sleep(inf) は OverflowError で実行を止める）
            for key in ("max_attempts", "min_output_chars", "retries"):
                if key in disc and not _int(disc[key]):
                    errors.append(f"discussion.{key} は整数が必要です")
            if "max_attempts" in disc and _int(disc["max_attempts"]) and disc["max_attempts"] < 1:
                errors.append("discussion.max_attempts は 1 以上が必要です")
            if "min_output_chars" in disc and _int(disc["min_output_chars"]) \
                    and disc["min_output_chars"] < 1:
                errors.append("discussion.min_output_chars は 1 以上が必要です")
            if "retries" in disc and _int(disc["retries"]) and disc["retries"] < 0:
                errors.append("discussion.retries は 0 以上が必要です")
            if "timeout_seconds" in disc and (
                not _finite(disc["timeout_seconds"]) or disc["timeout_seconds"] <= 0
            ):
                errors.append("discussion.timeout_seconds は正の有限な数値が必要です")
            if "retry_wait_seconds" in disc and (
                not _finite(disc["retry_wait_seconds"]) or disc["retry_wait_seconds"] < 0
            ):
                errors.append("discussion.retry_wait_seconds は 0 以上の有限な数値が必要です")
            for key in ("allow_terms", "public_org_names"):
                values = disc.get(key)
                if values is not None and not (
                    isinstance(values, list)
                    and all(isinstance(v, str) and v.strip() for v in values)
                ):
                    errors.append(f"discussion.{key} は空でない文字列のリストが必要です")

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

    # 入力処理が参照するカラムエイリアスが columns セクションに定義されているか。
    # 任意列は入力CSV上では省略可能だが、正準化の設定自体は必須とする。
    columns = cfg["columns"]
    if not isinstance(columns, dict):
        errors.append("columns セクションが辞書ではありません")
    else:
        for section, required in REQUIRED_COLUMNS.items():
            sec = columns.get(section)
            if not isinstance(sec, dict):
                errors.append(f"columns.{section} がありません")
                continue
            configured_columns = list(required)
            if section == "spend":
                configured_columns.extend(SPEND_OPTIONAL_COLUMNS)
            elif section == "members":
                configured_columns.extend(MEMBERS_OPTIONAL_COLUMNS)
            for canonical in configured_columns:
                aliases = sec.get(canonical)
                if not isinstance(aliases, list) or not aliases:
                    errors.append(f"columns.{section}.{canonical} のエイリアス定義がありません")

    if errors:
        raise ValueError("config.yaml の設定に問題があります:\n  - " + "\n  - ".join(errors))
