"""Cấu hình huấn luyện (TOML, đọc bằng tomllib của thư viện chuẩn)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "default.toml"


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    with open(DEFAULT_CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    if path:
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    if overrides:
        _merge(cfg, overrides)
    return cfg


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
