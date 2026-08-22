"""行为包通信模块（scriptevent 双向通道 + 心跳活性判定）。"""

from __future__ import annotations

import json
import time
from typing import Any

from endstone.event import ScriptMessageEvent


class BridgeManager:
    """管理插件与行为包的 scriptevent 通信。

    行为包 → 插件：on_script_message 接收 bot:pong / bot:positions / bot:spawned 等
    插件 → 行为包：send() 发送 bot:spawn / bot:remove / bot:teleport 等
    """

    def __init__(self, logger: Any, dispatch_fn: Any) -> None:
        self._logger = logger
        self._dispatch = dispatch_fn
        self._bridge_token = ""
        self._last_pong_at: float = -999.0
        self._behavior_pack_active: bool = False

    @property
    def active(self) -> bool:
        return self._behavior_pack_active

    @property
    def last_pong_at(self) -> float:
        return self._last_pong_at

    def generate_token(self) -> None:
        import secrets
        self._bridge_token = secrets.token_hex(16)

    def send(self, event_id: str, data: dict) -> bool:
        """发送 scriptevent 到行为包。"""
        payload = {**data, "t": self._bridge_token}
        msg = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        command = f"scriptevent {event_id} {msg}"
        try:
            return bool(self._dispatch(command))
        except Exception as exc:
            self._logger.debug(f"发送 scriptevent 失败: {exc}")
            return False

    def send_bridge(self, msg_type: str, data: dict) -> bool:
        """通用命令发送入口。"""
        script_map = {
            "spawn": "bot:spawn", "remove": "bot:remove", "teleport": "bot:teleport",
            "practice_config": "bot:practice_config",
        }
        if msg_type in script_map:
            return self.send(script_map[msg_type], data)
        return False

    def handle_script_message(self, event: ScriptMessageEvent) -> dict | None:
        """处理行为包发回的 scriptevent。返回解析后的消息字典，或 None。"""
        msg_id = event.message_id
        if not msg_id.startswith("bot:"):
            return None
        try:
            data = json.loads(event.message) if event.message else {}
        except Exception:
            data = {}
        token = data.get("t", "")
        if msg_id not in ("bot:pong", "bot:heartbeat"):
            if not self._bridge_token or token != self._bridge_token:
                self._logger.debug(f"忽略未经认证的 scriptevent: {msg_id}")
                return None
        else:
            if not self._bridge_token or token != self._bridge_token:
                self._logger.debug(f"忽略未经认证的 {msg_id}")
                return None
        return {"id": msg_id, "data": data}

    def on_pong(self, data: dict) -> dict | None:
        """处理 pong 心跳。返回 managed_names 列表。"""
        self._last_pong_at = time.monotonic()
        was_active = self._behavior_pack_active
        self._behavior_pack_active = True
        if not was_active:
            self._logger.info("§a行为包已连接，SimulatedPlayer 功能可用。§r")
        return {
            "names": [
                str(n).lower() for n in data.get("names", []) if isinstance(n, str)
            ]
        }

    def on_positions(self, data: dict) -> list[dict]:
        """处理位置上报。"""
        entries = data.get("p", [])
        if not isinstance(entries, list):
            return []
        result = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            result.append({
                "n": str(item.get("n", "")),
                "x": item.get("x"),
                "y": item.get("y"),
                "z": item.get("z"),
                "d": str(item.get("d", "overworld")),
            })
        return result

    def on_remove(self, data: dict) -> str:
        return str(data.get("n", ""))

    def on_error(self, data: dict) -> tuple[str, str]:
        return str(data.get("n", "")), str(data.get("e", ""))

    def check_activity(self, threshold: float = 20.0) -> bool:
        if self._last_pong_at < 0:
            return False
        return time.monotonic() - self._last_pong_at < threshold