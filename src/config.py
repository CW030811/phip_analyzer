"""配置加载：合并 config.yaml 和 .env"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, config_path: Path | None = None):
        load_dotenv(PROJECT_ROOT / ".env")
        cfg_path = config_path or PROJECT_ROOT / "config.yaml"
        with open(cfg_path, "r", encoding="utf-8") as f:
            self._data: dict[str, Any] = yaml.safe_load(f)

        # 解析路径为绝对路径
        for key in ("output_dir",):
            if key in self._data.get("report", {}):
                p = self._data["report"][key]
                self._data["report"][key] = str(PROJECT_ROOT / p) if not os.path.isabs(p) else p
        for key in ("db_path", "pdf_dir", "cache_dir", "log_dir"):
            if key in self._data.get("storage", {}):
                p = self._data["storage"][key]
                self._data["storage"][key] = str(PROJECT_ROOT / p) if not os.path.isabs(p) else p

    @property
    def anthropic_api_key(self) -> str:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY 未在 .env 中配置")
        return key

    @property
    def log_level(self) -> str:
        return os.getenv("LOG_LEVEL", "INFO")

    @property
    def webhook_url(self) -> str | None:
        url = os.getenv("WEBHOOK_URL", "").strip()
        return url or None

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def get_config() -> Config:
    return Config()
