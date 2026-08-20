"""OpenAI 兼容 AI 客户端与结构化动作解析。"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {
    "idle", "station", "follow", "movehere", "stop", "say",
}


@dataclass
class AIResponse:
    ok: bool
    reply: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从纯 JSON 或 Markdown 代码块中提取第一个 JSON 对象。"""
    value = str(text or "").strip()
    if not value:
        return None
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(value[start:end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def parse_ai_response(text: str) -> AIResponse:
    """解析并严格过滤模型返回的 reply/actions。"""
    data = _extract_json_object(text)
    if data is None:
        # 兼容只回复文本的模型，不执行动作。
        return AIResponse(ok=True, reply=str(text or "").strip()[:200])

    reply = str(data.get("reply", "") or "").strip()[:200]
    raw_actions = data.get("actions", [])
    actions: list[dict[str, Any]] = []
    if isinstance(raw_actions, list):
        for item in raw_actions[:4]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            if name not in ALLOWED_ACTIONS:
                continue
            args = item.get("args", {})
            if not isinstance(args, dict):
                args = {}
            clean: dict[str, Any] = {}
            if name == "follow":
                target = str(args.get("target", "") or "").strip()[:32]
                if target:
                    clean["target"] = target
                else:
                    continue
            elif name == "say":
                message = str(args.get("message", "") or "").strip()[:180]
                if message:
                    clean["message"] = message
                else:
                    continue
            actions.append({"name": name, "args": clean})
    return AIResponse(ok=True, reply=reply, actions=actions)


class AIClient:
    """同步 HTTP 客户端；调用方应在线程池中使用。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = max(3.0, min(120.0, float(timeout)))

    def is_configured(self) -> bool:
        key = self.api_key.strip()
        return bool(
            self.base_url and self.model and key
            and key.lower() not in {"xxx", "changeme", "sk-xxx"}
        )

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.3, max_tokens: int = 512) -> AIResponse:
        if not self.is_configured():
            return AIResponse(ok=False, error="AI 未配置")
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": max(0.0, min(2.0, float(temperature))),
            "max_tokens": max(16, min(4096, int(max_tokens))),
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices", [])
            if not choices:
                return AIResponse(ok=False, error="模型返回为空")
            content = choices[0].get("message", {}).get("content", "")
            result = parse_ai_response(content)
            if not result.reply and not result.actions:
                return AIResponse(ok=False, error="模型响应中没有有效内容")
            return result
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
                detail = str(body.get("error", {}).get("message", ""))
            except Exception:
                detail = ""
            logger.warning("AI API HTTP %s: %s", exc.code, detail or exc.reason)
            return AIResponse(ok=False, error=f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            logger.warning("AI API 网络错误: %s", exc.reason)
            return AIResponse(ok=False, error="网络连接失败")
        except TimeoutError:
            return AIResponse(ok=False, error="请求超时")
        except Exception as exc:
            logger.warning("AI API 异常: %s", exc)
            return AIResponse(ok=False, error="请求异常")

    def list_models(self) -> list[str]:
        if not self.base_url:
            return []
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout, 15.0)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [str(x.get("id")) for x in data.get("data", []) if isinstance(x, dict) and x.get("id")]
        except Exception as exc:
            logger.warning("获取模型列表失败: %s", exc)
            return []
