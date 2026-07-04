"""config.yaml のロード。"""

from __future__ import annotations

from pathlib import Path

import yaml

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
    return cfg
