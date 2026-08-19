"""AI 客户端模块。

调用 OpenAI 兼容格式的 LLM API，处理假人 @ai 对话。
支持 OpenAI / DeepSeek / Claude（通过兼容网关）/ Ollama 等。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any


logger = logging.getLogger(__name__)


class AIClient:
    """AI 对话客户端。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def is_configured(self) -> bool:
        """是否已配置有效的 API。"""
        if not self.base_url or not self.model:
            return False
        key = self.api_key.strip()
        if not key or key.lower() in ("", "xxx", "changeme", "sk-xxx"):
            return False
        return True

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> str:
        """发送对话请求，返回 AI 回复文本。

        Args:
            messages: [{"role": "user"/"assistant"/"system", "content": "..."}]
            temperature: 随机性
            max_tokens: 最大 token 数

        Returns:
            AI 回复文本，失败返回错误信息
        """
        if not self.is_configured():
            return "AI 未配置，请管理员在 config.json 中配置 API 地址和 Key。"

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                err_msg = err_body.get("error", {}).get("message", str(e))
            except Exception:
                err_msg = str(e)
            logger.warning(f"AI API HTTP 错误: {e.code} {err_msg}")
            return f"AI 请求失败：{err_msg}"
        except urllib.error.URLError as e:
            logger.warning(f"AI API 网络错误: {e.reason}")
            return f"AI 连接失败：{e.reason}"
        except Exception as e:
            logger.warning(f"AI API 未知错误: {e}")
            return f"AI 请求失败：{e}"

        try:
            choices = data.get("choices", [])
            if not choices:
                return "AI 返回为空"
            return choices[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning(f"AI 响应解析错误: {e}")
            return "AI 响应解析失败"

    def list_models(self) -> list[str]:
        """列出可用模型名称。"""
        if not self.base_url:
            return []
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                if mid:
                    models.append(mid)
            return models
        except Exception as e:
            logger.warning(f"获取模型列表失败: {e}")
            return []
