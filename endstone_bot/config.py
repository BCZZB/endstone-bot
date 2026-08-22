"""全局 AI 配置管理。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from endstone_bot.ai_client import AIClient


class ConfigManager:
    """管理 AI 配置的持久化（baseUrl / apiKey / model）。"""

    def __init__(self, data_folder: Path, logger: Any) -> None:
        self._path = data_folder / "ai_config.json"
        self._logger = logger
        self._data = self._load()
        self._ai = self._build_client()

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {"baseUrl": "", "apiKey": "", "model": ""}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"baseUrl": "", "apiKey": "", "model": ""}
            return {
                "baseUrl": str(data.get("baseUrl", "")),
                "apiKey": str(data.get("apiKey", "")),
                "model": str(data.get("model", "")),
            }
        except Exception as exc:
            self._logger.warning(f"读取 AI 配置失败: {exc}")
            return {"baseUrl": "", "apiKey": "", "model": ""}

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
            try:
                self._path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:
            self._logger.warning(f"保存 AI 配置失败: {exc}")

    def _build_client(self) -> AIClient:
        return AIClient(
            base_url=self._data.get("baseUrl", ""),
            api_key=self._data.get("apiKey", ""),
            model=self._data.get("model", ""),
        )

    @property
    def client(self) -> AIClient:
        return self._ai

    @property
    def config(self) -> dict[str, str]:
        return dict(self._data)

    def update(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> None:
        if base_url is not None:
            self._data["baseUrl"] = base_url.strip()
        if api_key is not None:
            self._data["apiKey"] = api_key.strip()
        if model is not None:
            self._data["model"] = model.strip()
        self._ai = self._build_client()
        self._save()

    def show(self, sender: Any) -> None:
        cfg = self._data
        url = cfg.get("baseUrl", "")
        model = cfg.get("model", "")
        key = cfg.get("apiKey", "")
        key_masked = (key[:8] + "****") if len(key) > 8 else ("****" if key else "（未设置）")
        ready = "§a可用" if self._ai.is_configured() else "§c未配置"
        sender.send_message(
            f"§b===== AI 全局配置 =====\n"
            f"  状态：{ready}\n"
            f"  地址：§7{url or '（未设置）'}\n"
            f"  模型：§7{model or '（未设置）'}\n"
            f"  Key：§7{key_masked}\n"
            f"\n§7配置：/bot ai-config set <baseUrl> <apiKey> <model>"
        )