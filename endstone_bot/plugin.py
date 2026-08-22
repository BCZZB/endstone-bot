"""主插件模块：生命周期、事件注册、命令分发。

模块结构：
- BotPlugin：入口，承载 Endstone 生命周期与事件
- FakeBotManager（manager.py）：假人 CRUD / 持久化 / 自愈
- BehaviorSystem（behavior.py）：idle/station/follow 行为
- BridgeManager（bridge.py)：行为包 scriptevent 通信
- ConfigManager（config.py)：AI 配置持久化
"""

from __future__ import annotations

import json
import math
import secrets
import shlex
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from endstone.command import Command, CommandSender
from endstone.event import (
    ActorDamageEvent,
    ActorKnockbackEvent,
    ActorRemoveEvent,
    EventPriority,
    PlayerChatEvent,
    PlayerInteractActorEvent,
    ScriptMessageEvent,
    event_handler,
)
from endstone.level import Location
from endstone.plugin import Plugin

from endstone_bot.ai_client import AIResponse
from endstone_bot.behavior import BehaviorSystem
from endstone_bot.bridge import BridgeManager
from endstone_bot.config import ConfigManager
from endstone_bot.gui import BotGUI
from endstone_bot.level_dat import enable_experiments, is_experiments_enabled
from endstone_bot.manager import FakeBotManager
from endstone_bot.models import (
    FakePlayer,
    get_skin_name,
    normalize_skin_id,
)


class BotPlugin(Plugin):
    """端末石假人插件主类。"""

    api_version = "0.11"
    description = "假人插件：SimulatedPlayer 假人 + AI 对话 + 行为系统。使用 /bot credits 查看致谢。"

    commands = {
        "bot": {
            "description": "管理假人（SimulatedPlayer / AI 对话）。",
            "usages": ["/bot [args: message]"],
            "permissions": ["endstone_bot.command"],
        },
        "bots": {
            "description": "列出所有假人。",
            "usages": ["/bots"],
            "permissions": ["endstone_bot.command"],
        },
    }

    permissions = {
        "endstone_bot.command": {
            "description": "允许使用 /bot。",
            "default": True,
        }
    }

    BEHAVIOR_PACK_UUID = "a3f7c2e1-8b4d-4f6a-9c3e-1d2b3c4d5e6f"
    BEHAVIOR_PACK_VERSION = [3, 0, 0]

    MIN_Y = -64
    MAX_Y = 320
    DEFAULT_CHUNK_RADIUS = 4

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_enable(self) -> None:
        self._tick_counter: int = 0
        self._lock = threading.RLock()
        self._db_dirty: bool = False

        self.data_folder.mkdir(parents=True, exist_ok=True)

        # 行为包桥接
        self._bridge = BridgeManager(self.logger, lambda cmd: self._dispatch(cmd))

        # 假人管理器
        self.manager = FakeBotManager(self, self.data_folder, self._bridge, self.logger)
        self._bots = self.manager.bots
        self._name_index = self.manager.name_index

        # AI 配置
        self._ai_config_manager = ConfigManager(self.data_folder, self.logger)
        self._ai = self._ai_config_manager.client

        # AI 运行时
        self._ai_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="endstone-bot-ai")
        self._ai_busy: set[str] = set()
        self._ai_last_request: dict[tuple[str, str], float] = {}
        self._ai_history: dict[tuple[str, str], deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=12)
        )
        self._ai_runtime_lock = threading.RLock()

        # GUI / 行为系统
        self._gui = BotGUI(self)
        self._behavior = BehaviorSystem(self)

        # 私人 Bot 配置
        self._practice_profiles: dict[str, dict[str, Any]] = {}
        self._load_practice_profiles()

        # 行为包部署
        self._setup_behavior_pack()

        # 自愈与调度
        self._bridge.generate_token()
        self.manager.restore()
        scheduler = self.server.scheduler
        scheduler.run_task(self, self._tick_behaviors, delay=1, period=1)
        scheduler.run_task(self, self.manager.ensure_all_spawned, delay=40, period=40)
        scheduler.run_task(self, self._persist_positions, delay=600, period=600)
        scheduler.run_task(self, self._ping_behavior_pack, delay=60, period=600)

        if self._ai.is_configured():
            self.logger.info(f"AI 已配置：{self._ai.model} @ {self._ai.base_url}")
        else:
            self.logger.info("AI 未配置，使用 /bot ai-config set 配置。")

        self.logger.info("BotPlugin 已启用（SimulatedPlayer + AI 对话 + 行为系统）。")

    def on_disable(self) -> None:
        if hasattr(self, "_ai_executor"):
            self._ai_executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            self.manager.save()
            self.manager.clear_runtime()

    # ------------------------------------------------------------------
    # 调度任务
    # ------------------------------------------------------------------

    def _tick_behaviors(self) -> None:
        self._tick_counter += 1
        with self._lock:
            bots_snapshot = list(self._bots.values())
        for fp in bots_snapshot:
            self._behavior.apply(fp, force=False)

    def _persist_positions(self) -> None:
        changed = False
        with self._lock:
            bots_snapshot = list(self._bots.values())
            dirty = self._db_dirty
        for fp in bots_snapshot:
            if fp.type == "simulated" or not self.manager.is_actor_valid(fp.actor):
                continue
            try:
                loc = fp.actor.location
                new_x = round(float(loc.x), 2)
                new_y = round(float(loc.y), 2)
                new_z = round(float(loc.z), 2)
                if (fp.location_x, fp.location_y, fp.location_z) != (new_x, new_y, new_z):
                    fp.location_x, fp.location_y, fp.location_z = new_x, new_y, new_z
                    changed = True
            except Exception:
                pass
        if changed or dirty:
            self.manager.save()

    def _ping_behavior_pack(self) -> None:
        self._bridge.send("bot:ping", {})
        self.server.scheduler.run_task(self, self._check_ping_response, delay=300)

    def _check_ping_response(self) -> None:
        if self._bridge.check_activity(threshold=20.0):
            return
        if self._bridge.active:
            self.logger.warning("§e行为包失去响应，将持续重试。§r")
        else:
            self.logger.warning(
                "§e行为包未响应。可能原因：\n"
                "  1. 服务器未重启（首次安装后需重启）\n"
                "  2. Beta APIs 实验功能未启用\n"
                "  3. 行为包未正确加载\n"
                "行为包恢复连接后自动重建。§r"
            )
        for fp in list(self._bots.values()):
            if fp.type == "simulated":
                fp.sim_spawn_confirmed = False



    # ------------------------------------------------------------------
    # 事件：聊天（@假人 AI 对话）
    # ------------------------------------------------------------------

    @event_handler(priority=EventPriority.HIGHEST)
    def on_player_chat(self, event: PlayerChatEvent) -> None:
        message = str(event.message or "").strip()
        if not message.startswith("@"):
            return
        parts = message[1:].split(maxsplit=1)
        if not parts:
            return
        fp = self.manager.get_by_name(parts[0])
        if fp is None or not fp.ai_enabled:
            return
        event.cancel()
        player = event.player
        player_name = str(getattr(player, "name", "") or "").strip()
        player_uuid = str(getattr(player, "unique_id", "") or "")
        query = parts[1].strip() if len(parts) > 1 else ""
        is_owner = bool(
            (fp.owner_uuid and player_uuid == fp.owner_uuid)
            or (fp.owner_name and player_name.lower() == fp.owner_name.lower())
        )
        is_member = player_name.lower() in {x.lower() for x in fp.ai_members}
        if not (is_owner or is_member or self._is_admin(player)):
            player.send_message(f"§c你没有使用假人 §b{fp.name}§c AI 的权限。")
            return
        if not query:
            player.send_message(f"§e用法：@{fp.name} <指令>，例如：@{fp.name} 跟着我")
            return
        if not self._ai.is_configured():
            player.send_message("§cAI 尚未配置，请管理员使用 /bot ai-config set。")
            return

        key = (fp.id, player_uuid or player_name.lower())
        now = time.monotonic()
        with self._ai_runtime_lock:
            last = self._ai_last_request.get(key, 0.0)
            if now - last < 4.0:
                player.send_message("§e请求太快，请等待几秒后再试。")
                return
            if fp.id in self._ai_busy:
                player.send_message(f"§e{fp.name} 正在处理上一条指令，请稍候。")
                return
            self._ai_last_request[key] = now
            self._ai_busy.add(fp.id)

        player.send_message(f"§7{fp.name} 正在思考...")
        history = list(self._ai_history[key])
        self._ai_executor.submit(
            self._request_ai, fp.id, player_uuid, player_name, query, key, history
        )

    def _request_ai(self, fp_id: str, player_uuid: str, player_name: str, query: str, key: tuple[str, str], history: list[dict[str, str]]) -> None:
        fp = self._bots.get(fp_id)
        if fp is None:
            with self._ai_runtime_lock:
                self._ai_busy.discard(fp_id)
            return
        system = (
            f"你是 Minecraft 基岩版服务器中的假人 {fp.name}。"
            '你必须只返回一个 JSON 对象，不要使用 Markdown。格式：'
            '{"reply":"简短中文回复","actions":[{"name":"动作","args":{}}]}。'
            "允许动作仅有：idle、station、follow、movehere、stop、say。"
            "follow 的 args.target 填玩家名；say 的 args.message 填聊天内容。"
            f"当前发令玩家是 {player_name}。跟着我表示 follow target={player_name}；"
            "过来表示 movehere；停下表示 stop；原地驻守表示 station。"
        )
        messages = [{"role": "system", "content": system}, *history]
        messages.append({"role": "user", "content": query})
        result = self._ai.chat(messages, temperature=0.2, max_tokens=400)

        def finish() -> None:
            try:
                current = self._bots.get(fp_id)
                player = self._find_online_player_by_uuid_or_name(player_uuid, player_name)
                if current is None:
                    return
                if not result.ok:
                    if player is not None:
                        player.send_message("§cAI 暂时不可用，请稍后重试。")
                    self.logger.warning(f"[AI] {current.name} 请求失败: {result.error}")
                    return
                self._ai_history[key].append({"role": "user", "content": query[:500]})
                self._ai_history[key].append({"role": "assistant", "content": result.reply[:500]})
                if result.reply:
                    self.server.broadcast_message(f"§b[{current.name}]§r {result.reply}")
                self._execute_ai_actions(current, player, player_name, result)
            finally:
                with self._ai_runtime_lock:
                    self._ai_busy.discard(fp_id)

        try:
            self.server.scheduler.run_task(self, finish, delay=0)
        except Exception as exc:
            self.logger.warning(f"提交 AI 主线程回调失败: {exc}")
            with self._ai_runtime_lock:
                self._ai_busy.discard(fp_id)

    def _execute_ai_actions(self, fp: FakePlayer, player: Any | None, player_name: str, result: AIResponse) -> None:
        for action in result.actions:
            name = action["name"]
            args = action.get("args", {})
            if name in ("idle", "stop"):
                fp.behavior.movement = "idle"
                fp.behavior.target_player = ""
                self._behavior.apply(fp, force=True)
            elif name == "station" and player is not None:
                loc = player.location
                fp.behavior.movement = "station"
                fp.behavior.station_x = round(float(loc.x), 2)
                fp.behavior.station_y = round(float(loc.y), 2)
                fp.behavior.station_z = round(float(loc.z), 2)
            elif name == "follow":
                target = str(args.get("target", "") or player_name).strip()
                if self._find_online_player(target) is not None:
                    fp.behavior.movement = "follow"
                    fp.behavior.target_player = target
            elif name == "movehere" and player is not None:
                self._cmd_movehere(player, [fp.name])
            elif name == "say":
                msg = str(args.get("message", "") or "")[:180]
                if msg:
                    self.server.broadcast_message(f"§b[{fp.name}]§r {msg}")
        self.manager.save()

    # ------------------------------------------------------------------
    # 事件：交互 / 伤害 / 移除
    # ------------------------------------------------------------------

    @event_handler(priority=EventPriority.HIGH)
    def on_actor_damage(self, event: ActorDamageEvent) -> None:
        if self.manager.is_fake_player_actor(event.actor):
            event.cancel()

    @event_handler(priority=EventPriority.HIGH)
    def on_actor_knockback(self, event: ActorKnockbackEvent) -> None:
        if self.manager.is_fake_player_actor(event.actor):
            event.cancel()

    @event_handler(priority=EventPriority.HIGH)
    def on_player_interact_actor(self, event: PlayerInteractActorEvent) -> None:
        actor = event.actor
        if not self.manager.is_fake_player_actor(actor):
            return
        player = event.player
        event.cancel()
        fp = self.manager.get_by_actor(actor)
        if fp is None:
            player.send_message("§c这个假人的数据不存在。§r")
            return
        if fp.practice_managed:
            if not bool(getattr(player, "is_sneaking", False)):
                return
            if not self.manager.can_manage(player, fp):
                player.send_message("§c你不能编辑其他玩家的 bot。")
                return
            self._gui.open_practice_menu(player, fp)
            return
        self._gui.open_bot_manage(player, fp)

    @event_handler
    def on_actor_remove(self, event: ActorRemoveEvent) -> None:
        actor = event.actor
        if not self.manager.is_fake_player_actor(actor):
            return
        fp = self.manager.get_by_actor(actor)
        if fp is None:
            return
        fp.actor = None
        fp.entity_id = ""

    @event_handler
    def on_script_message(self, event: ScriptMessageEvent) -> None:
        parsed = self._bridge.handle_script_message(event)
        if parsed is None:
            return
        msg_id = parsed["id"]
        data = parsed["data"]
        if msg_id in ("bot:pong", "bot:heartbeat"):
            names = self._bridge.on_pong(data)
            if names:
                managed = set(names["names"])
                for fp in list(self._bots.values()):
                    if fp.type == "simulated" and fp.name.lower() not in managed:
                        fp.sim_spawn_confirmed = False
                with self._lock:
                    pending = list(self.manager.pending_spawns)
                    self.manager.pending_spawns.clear()
                for fp in pending:
                    self.manager.spawn_simulated(fp)
                if self.manager.pending_removes:
                    for name in list(self.manager.pending_removes):
                        self._bridge.send_bridge("remove", {"n": name})
                    self.manager.pending_removes.clear()
        elif msg_id == "bot:spawned":
            name = str(data.get("n", ""))
            ok = bool(data.get("ok", False))
            fp = self.manager.get_by_name(name)
            if fp is not None:
                fp.sim_spawn_confirmed = ok
            if ok:
                self.logger.info(f"模拟玩家 §b{name}§r 生成成功。")
            else:
                self.logger.warning(f"模拟玩家 §b{name}§r 生成失败。")
        elif msg_id == "bot:positions":
            for item in self._bridge.on_positions(data):
                fp = self.manager.get_by_name(item["n"])
                if fp is None or fp.type != "simulated":
                    continue
                try:
                    fp.sim_actual_x = round(float(item["x"]), 2)
                    fp.sim_actual_y = round(float(item["y"]), 2)
                    fp.sim_actual_z = round(float(item["z"]), 2)
                    fp.sim_has_position = True
                    if fp.behavior.movement != "idle":
                        fp.location_x = fp.sim_actual_x
                        fp.location_y = fp.sim_actual_y
                        fp.location_z = fp.sim_actual_z
                    fp.dimension = item["d"] or fp.dimension
                    self._db_dirty = True
                except (TypeError, ValueError):
                    continue
        elif msg_id == "bot:removed":
            self.logger.info(f"模拟玩家 §b{self._bridge.on_remove(data)}§r 已移除。")
        elif msg_id == "bot:error":
            name, err = self._bridge.on_error(data)
            self.logger.warning(f"行为包错误 [{name}]: {err}")
        elif msg_id == "bot:list_result":
            names = data.get("names", [])
            self.logger.info(f"行为包管理的模拟玩家: {', '.join(str(x) for x in names)}")

    # ------------------------------------------------------------------
    # 命令入口
    # ------------------------------------------------------------------

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if len(args) == 1 and isinstance(args[0], str) and " " in args[0].strip():
            try:
                args = shlex.split(args[0])
            except ValueError:
                args = args[0].split()
        name = command.name.lower()
        if name == "bots":
            return self._cmd_list(sender)
        if name != "bot":
            return False
        if not args:
            if hasattr(sender, "send_form"):
                self._gui.open_main_menu(sender)
                return True
            self._send_usage(sender)
            return True
        sub = args[0].lower()
        rest = args[1:]
        handlers = {
            "gui": lambda: self._cmd_gui(sender),
            "spawn": lambda: self.manager.cmd_spawn(sender, rest),
            "remove": lambda: self.manager.cmd_remove(sender, rest),
            "list": lambda: self.manager.cmd_list(sender),
            "radius": lambda: self.manager.cmd_radius(sender, rest),
            "info": lambda: self.manager.cmd_info(sender, rest),
            "skin": lambda: self.manager.cmd_skin(sender, rest),
            "skins": lambda: self.manager.cmd_skins(sender),
            "behavior": lambda: self.manager.cmd_behavior(sender, rest),
            "movehere": lambda: self.manager.cmd_movehere(sender, rest),
            "clearall": lambda: self.manager.cmd_clearall(sender),
            "credits": lambda: self._cmd_credits(sender),
            "ai": lambda: self._cmd_ai(sender, rest),
            "ai-config": lambda: self._cmd_ai_config(sender, rest),
        }
        handler = handlers.get(sub)
        if handler is None:
            self._send_usage(sender)
            return True
        return handler()

    def _cmd_gui(self, sender: CommandSender) -> bool:
        if not hasattr(sender, "send_form"):
            sender.send_error_message("GUI 仅限玩家使用。")
            return True
        self._gui.open_main_menu(sender)
        return True

    def _cmd_credits(self, sender: CommandSender) -> bool:
        sender.send_message(
            "§b===== endstone_bot 致谢与参考声明 =====§r\n"
            "\n"
            "§a1. mcbes-manage-script§r\n"
            "   来源：§9https://github.com/YueHua46/mcbes-manage-script§r\n"
            "   许可证：PolyForm Noncommercial License 1.0.0\n"
            "   借鉴内容（全部假人管理逻辑）：\n"
            "     - SimulatedPlayer 假人\n"
            "     - 所有者追踪机制\n"
            "     - 自愈恢复\n"
            "     - 位置守护\n"
            "     - 行为系统 (idle / station / follow)\n"
            "     - 移动到操作者\n"
            "     - 清除全部\n"
            "\n"
            "§7本项目在上述思路基础上重新实现，适配 Endstone Python API。\n"
            "感谢原作者的开源贡献。§r"
        )
        return True



    # ------------------------------------------------------------------
    # AI 管理命令
    # ------------------------------------------------------------------

    def _cmd_ai(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot ai <假人名> on|off|add|remove|list [玩家]")
            return True
        name = args[0]
        fp = self.manager.get_by_name(name)
        if fp is None:
            sender.send_error_message(f"假人 §b{name}§r 不存在。")
            return True
        if not self.manager.can_manage(sender, fp):
            sender.send_error_message("无权管理该假人的 AI。")
            return True
        if len(args) < 2:
            self._ai_show(fp, sender)
            return True
        action = args[1].lower()
        if action in ("on", "enable", "开启"):
            fp.ai_enabled = True
            self.manager.save()
            sender.send_message(f"假人 §b{fp.name}§r 的 AI 已开启。可通过 §e@{fp.name} <指令>§r 对话。")
            return True
        if action in ("off", "disable", "关闭"):
            fp.ai_enabled = False
            self.manager.save()
            sender.send_message(f"假人 §b{fp.name}§r 的 AI 已关闭。")
            return True
        if action in ("add", "添加", "+"):
            if len(args) < 3:
                sender.send_error_message("用法：/bot ai <名字> add <玩家>")
                return True
            target = args[2].strip()
            if target.lower() in {n.lower() for n in fp.ai_members}:
                sender.send_message(f"§e{target}§r 已在授权列表中。")
                return True
            fp.ai_members.append(target)
            self.manager.save()
            sender.send_message(f"已授权 §e{target}§r 使用假人 §b{fp.name}§r 的 AI。")
            return True
        if action in ("remove", "del", "删除", "-"):
            if len(args) < 3:
                sender.send_error_message("用法：/bot ai <名字> remove <玩家>")
                return True
            target = args[2].strip()
            for i, m in enumerate(fp.ai_members):
                if m.lower() == target.lower():
                    fp.ai_members.pop(i)
                    self.manager.save()
                    sender.send_message(f"已移除 §e{target}§r 的 AI 授权。")
                    return True
            sender.send_message(f"§e{target}§r 不在授权列表中。")
            return True
        if action == "list" or action == "列表":
            self._ai_show(fp, sender)
            return True
        sender.send_error_message("用法：/bot ai <名字> on|off|add|remove|list [玩家]")
        return True

    def _ai_show(self, fp: FakePlayer, sender: CommandSender) -> None:
        status = "§a开启" if fp.ai_enabled else "§c关闭"
        members = ", ".join(f"§e{m}§r" for m in fp.ai_members) or "§7（空）"
        ai_ready = "§a已配置" if self._ai.is_configured() else "§c未配置"
        sender.send_message(
            f"§b===== 假人 §a{fp.name} §bAI 配置 =====\n"
            f"  AI 状态：{status}\n"
            f"  API：{ai_ready}"
            + (f" §7({self._ai.model})" if self._ai.model else "")
            + f"\n  授权成员：{members}"
        )

    def _cmd_ai_config(self, sender: CommandSender, args: list[str]) -> bool:
        if not self._is_admin(sender):
            sender.send_error_message("只有 OP 可以管理全局 AI 模型配置。")
            return True
        if not args:
            self._ai_config_manager.show(sender)
            return True
        action = args[0].lower()
        if action == "get":
            self._ai_config_manager.show(sender)
            return True
        if action == "set":
            if len(args) < 4:
                sender.send_error_message("用法：/bot ai-config set <baseUrl> <apiKey> <model>")
                return True
            base_url = args[1].strip()
            api_key = args[2].strip()
            model = args[3].strip()
            self._ai_config_manager.update(base_url=base_url, api_key=api_key, model=model)
            self._ai = self._ai_config_manager.client
            sender.send_message(
                f"§aAI 配置已更新：\n  地址：{base_url}\n  模型：{model}\n"
                f"  状态：{'§a可用' if self._ai.is_configured() else '§c配置无效'}"
            )
            return True
        if action == "test":
            if not self._ai.is_configured():
                sender.send_error_message("AI 未配置，请先 /bot ai-config set")
                return True
            sender.send_message("§e正在后台测试 AI 连接...")
            sender_name = str(getattr(sender, "name", "") or "")

            def test_request() -> None:
                result = self._ai.chat(
                    [{"role": "user", "content": '只返回 JSON：{"reply":"OK","actions":[]}'}],
                    temperature=0.1, max_tokens=80,
                )

                def finish() -> None:
                    target = self._find_online_player(sender_name) or sender
                    if result.ok:
                        target.send_message(f"§aAI 测试成功：{result.reply or 'OK'}")
                    else:
                        target.send_error_message(f"§cAI 测试失败：{result.error}")
                self.server.scheduler.run_task(self, finish, delay=0)

            self._ai_executor.submit(test_request)
            return True
        if action == "models":
            if not self._ai.base_url:
                sender.send_error_message("请先配置 API 地址。")
                return True
            sender.send_message("§e正在获取模型列表...")

            def list_request() -> None:
                models = self._ai.list_models()

                def finish() -> None:
                    target = self._find_online_player(str(getattr(sender, "name", "") or "")) or sender
                    text = ", ".join(models[:20]) if models else "未获取到模型"
                    target.send_message(f"§b可用模型：§r{text}")
                self.server.scheduler.run_task(self, finish, delay=0)
            self._ai_executor.submit(list_request)
            return True
        if action == "clear":
            self._ai_config_manager.update(base_url="", api_key="", model="")
            self._ai = self._ai_config_manager.client
            sender.send_message("§aAI 配置已清除。")
            return True
        sender.send_error_message("用法：/bot ai-config get|set|test|models|clear")
        return True

    # ------------------------------------------------------------------
    # GUI 操作委托
    # ------------------------------------------------------------------

    def _gui_spawn(self, player: Any, name: str, skin_id: int) -> None:
        self._cmd_spawn(player, [name, "simulated", str(skin_id)])

    def _gui_set_skin(self, player: Any, fp: FakePlayer, skin_id: int) -> None:
        if not self.manager.can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        fp.skin_id = normalize_skin_id(skin_id)
        self.manager.save()
        player.send_message(f"已把 §b{fp.name}§r 的皮肤设为 {get_skin_name(fp.skin_id)}(#{fp.skin_id})。§r")

    def _gui_set_behavior(self, player: Any, fp: FakePlayer, mode: str, target: str) -> None:
        if not self.manager.can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        if mode not in ("idle", "station", "follow"):
            player.send_message("§c无效的行为模式。§r")
            return
        behavior = fp.behavior
        behavior.movement = mode
        behavior.action = "none"
        fp.last_action_tick = 0
        if mode == "follow":
            if not target:
                player.send_message("§c跟随模式需要指定目标玩家。§r")
                return
            if self._find_online_player(target) is None:
                player.send_message("§c目标玩家不在线。§r")
                return
            behavior.target_player = target
            player.send_message(f"假人 §b{fp.name}§r 现在跟随 §e{target}§r。§r")
        elif mode == "station":
            loc = self._sender_location(player)
            if loc is not None:
                behavior.station_x = round(float(loc.x), 2)
                behavior.station_y = round(float(loc.y), 2)
                behavior.station_z = round(float(loc.z), 2)
            player.send_message(f"假人 §b{fp.name}§r 已锁定到你当前位置。§r")
        else:
            behavior.target_player = ""
            player.send_message(f"假人 §b{fp.name}§r 行为设为原地待命。§r")
        self.manager.save()

    def _gui_set_radius(self, player: Any, fp: FakePlayer, radius: int) -> None:
        if not self.manager.can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        radius = max(0, min(4, radius))
        if self.manager.is_actor_valid(fp.actor):
            try:
                loc = fp.actor.location
                fp.location_x = round(float(loc.x), 2)
                fp.location_y = round(float(loc.y), 2)
                fp.location_z = round(float(loc.z), 2)
            except Exception:
                pass
        self.manager.remove_tickingarea(fp)
        if radius > 0:
            self.manager.create_tickingarea(fp, radius)
        self.manager.save()
        player.send_message(f"已把 §b{fp.name}§r 的常加载半径设为 {radius} 区块。§r")

    def _gui_movehere(self, player: Any, fp: FakePlayer) -> None:
        if not self.manager.can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        self.manager.cmd_movehere(player, [fp.name])

    def _gui_remove(self, player: Any, fp: FakePlayer) -> None:
        if not self.manager.can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        self.manager.remove_managed(fp)
        self._bots.pop(fp.id, None)
        self._name_index.pop(fp.name.lower(), None)
        self.manager.save()
        player.send_message(f"已删除假人 §b{fp.name}§r。§r")

    def _gui_clearall(self, player: Any) -> None:
        self._cmd_clearall(player)

    # ------------------------------------------------------------------
    # 私人 Bot 配置（practice）
    # ------------------------------------------------------------------

    def get_practice_profile(self, player: Any) -> dict[str, Any]:
        key = str(getattr(player, "unique_id", "") or "") or str(getattr(player, "name", "") or "").strip().lower()
        return self._practice_profiles.get(key, {
            "follow": False, "randomMove": False, "slowFalling": False,
            "fireResistance": False, "infiniteTotem": False, "armor": "none",
        })

    def save_practice_profile(self, player: Any, profile: dict[str, Any]) -> None:
        key = str(getattr(player, "unique_id", "") or "") or str(getattr(player, "name", "") or "").strip().lower()
        self._practice_profiles[key] = profile
        self._save_practice_profiles()

    def _load_practice_profiles(self) -> None:
        path = self.data_folder / "practice_profiles.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._practice_profiles = data
        except Exception:
            pass

    def _save_practice_profiles(self) -> None:
        try:
            path = self.data_folder / "practice_profiles.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._practice_profiles, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            self.logger.warning(f"保存私人 Bot 配置失败: {exc}")

    def send_practice_config(self, fp: FakePlayer) -> None:
        if not fp.practice_managed:
            return
        self._bridge.send_bridge("practice_config", {
            "n": fp.name, "owner": fp.owner_name,
            "follow": fp.practice_follow, "randomMove": fp.practice_random_move,
            "slowFalling": fp.practice_slow_falling,
            "fireResistance": fp.practice_fire_resistance,
            "infiniteTotem": fp.practice_infinite_totem,
            "armor": fp.practice_armor,
        })

    def _cmd_clearall(self, sender: CommandSender) -> bool:
        return self.manager.cmd_clearall(sender)

    def _cmd_spawn(self, sender: CommandSender, args: list[str]) -> bool:
        return self.manager.cmd_spawn(sender, args)

    def _cmd_movehere(self, sender: CommandSender, args: list[str]) -> bool:
        return self.manager.cmd_movehere(sender, args)

    def _get_by_name(self, name: str) -> FakePlayer | None:
        """按名称查找假人（大小写不敏感）。"""
        fp_id = self._name_index.get(name.strip().lower())
        if not fp_id:
            return None
        return self._bots.get(fp_id)

    def _get_by_actor(self, actor: Any) -> FakePlayer | None:
        try:
            tags = list(actor.scoreboard_tags or [])
        except Exception:
            return None
        for tag in tags:
            if tag.startswith(TAG_ID_PREFIX):
                fp_id = tag[len(TAG_ID_PREFIX):]
                return self._bots.get(fp_id)
        return None

    def _is_fake_player_actor(self, actor: Any) -> bool:
        try:
            tags = actor.scoreboard_tags or []
            return FAKE_PLAYER_TAG in tags
        except Exception:
            return False

    def _can_manage(self, sender: CommandSender, fp: FakePlayer) -> bool:
        """同月华 canManage：owner 或 admin。

        优先使用 UUID 判定所有权，避免名称重用导致越权。
        旧数据无 owner_uuid 时回退到名称匹配。
        """
        if fp.owner_uuid:
            sender_uuid = self._sender_uuid(sender)
            if sender_uuid and sender_uuid == fp.owner_uuid:
                return True
        elif fp.owner_name == self._sender_name(sender):
            # 旧数据兼容：无 UUID 时用名称匹配
            return True
        return self._is_admin(sender)

    def _is_admin(self, sender: CommandSender) -> bool:
        """OP 或服务器控制台视为管理员。"""
        try:
            is_op = getattr(sender, "is_op", None)
            if is_op is not None:
                return bool(is_op)
        except Exception:
            pass
        # 控制台没有玩家 unique_id/location；允许其管理全局配置。
        return not hasattr(sender, "unique_id") and not hasattr(sender, "location")

    def _find_online_player(self, name: str) -> Any | None:
        for player in self.server.online_players:
            try:
                if player.name == name:
                    return player
            except Exception:
                continue
        return None

    def _sender_name(self, sender: CommandSender) -> str:
        try:
            return str(getattr(sender, "name", "") or "console")
        except Exception:
            return "console"

    def _sender_uuid(self, sender: CommandSender) -> str:
        """获取发送者的唯一标识（UUID）。

        控制台返回空字符串，玩家返回 unique_id。
        """
        try:
            uid = getattr(sender, "unique_id", None)
            if uid is not None:
                return str(uid)
        except Exception:
            pass
        return ""

    def _sender_location(self, sender: CommandSender) -> Location | None:
        """获取发送者位置。

        控制台无位置时返回世界出生点，不使用在线玩家位置（隐私安全）。
        """
        loc = getattr(sender, "location", None)
        if loc is not None:
            return loc
        # 控制台：使用世界出生点，不窥探在线玩家位置
        try:
            overworld = self.server.level.get_dimension("overworld")
            return Location(overworld, 0.5, 80.0, 0.5)
        except Exception:
            return None

    def _sender_rotation(self, sender: CommandSender) -> tuple[float, float]:
        try:
            rotation = getattr(sender, "rotation", None)
            if rotation is not None:
                return float(rotation.x), float(rotation.y)
        except Exception:
            pass
        return 0.0, 0.0

    def _get_dimension(self, dimension_id: str) -> Any | None:
        normalized = dimension_id.lower().replace("minecraft:", "")
        aliases = {
            "overworld": ("overworld", "minecraft:overworld"),
            "nether": ("nether", "the_nether", "minecraft:nether"),
            "the_end": ("the_end", "end", "minecraft:the_end"),
        }
        for candidate in aliases.get(normalized, (normalized,)):
            try:
                dim = self.server.level.get_dimension(candidate)
                if dim is not None:
                    return dim
            except Exception:
                continue
        return None

    @staticmethod
    def _dimension_id(dimension: Any) -> str:
        try:
            name = str(getattr(dimension, "name", "")).lower()
            dtype = str(getattr(dimension, "type", "")).lower()
        except Exception:
            name = ""
            dtype = ""
        text = f"{name} {dtype}"
        if "nether" in text:
            return "nether"
        if "end" in text and "stone" not in text:
            return "the_end"
        return "overworld"

    @staticmethod
    def _is_actor_valid(actor: Any) -> bool:
        try:
            return bool(actor and actor.is_valid and not actor.is_dead)
        except Exception:
            return False

    @staticmethod
    def _actor_id(actor: Any) -> str:
        try:
            return str(actor.unique_id)
        except Exception:
            return ""

    @staticmethod
    def _player_yaw(player: Any) -> float:
        try:
            rotation = getattr(player, "rotation", None)
            if rotation is not None:
                return math.radians(float(rotation.y))
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _chunk_coords(x: float, z: float) -> tuple[int, int]:
        return math.floor(x) >> 4, math.floor(z) >> 4

    @staticmethod
    def _tickingarea_bounds(
        chunk_x: int, chunk_z: int, radius: int
    ) -> tuple[int, int, int, int]:
        start_x = (chunk_x - radius) * 16
        start_z = (chunk_z - radius) * 16
        end_x = (chunk_x + radius + 1) * 16 - 1
        end_z = (chunk_z + radius + 1) * 16 - 1
        return start_x, start_z, end_x, end_z

    @staticmethod
    def _safe_teleport(actor: Any, loc: Location) -> None:
        try:
            actor.teleport(loc)
            return
        except Exception:
            pass
        try:
            actor.teleport(loc.x, loc.y, loc.z)
            return
        except Exception:
            pass
        try:
            actor.set_location(loc)
        except Exception:
            pass

    def _dispatch(self, command: str) -> bool:
        try:
            return bool(self.server.dispatch_command(self.server.command_sender, command))
        except Exception as exc:
            self.logger.debug(f"命令执行失败 /{command}: {exc}")
            return False

    @staticmethod
    def _send_usage(sender: CommandSender) -> None:
        sender.send_message(
            "用法：/bot gui|spawn|remove|list|radius|info|skin|skins|behavior|movehere|ai|ai-config|clearall|credits，或 /bots\n"
            "§7提示：玩家直接输入 /bot 可打开 GUI 管理界面。§r"
        )

    # ==================================================================
    # 行为包管理（自动释放、注册、开启实验 API）
    # ==================================================================

    def _find_world_dir(self) -> Path | None:
        """查找当前世界目录（兼容 Endstone 0.11.x 的目录结构）。"""
        # 尝试通过 server.level 获取世界名称（在 ServerLoadEvent 后可用）
        try:
            level = self.server.level
            name = level.name
            # 检查 BDS 子目录和 CWD 下的多个可能路径
            for base in [
                Path("bedrock_server") / "worlds",
                Path.cwd() / "worlds",
                Path.cwd() / "bedrock_server" / "worlds",
            ]:
                world_dir = base / name
                if (world_dir / "level.dat").exists():
                    return world_dir.resolve()
        except Exception:
            pass

        # 搜索常见 BDS 世界目录
        for base in [
            Path("bedrock_server") / "worlds",
            Path.cwd() / "worlds",
            Path.cwd() / "bedrock_server" / "worlds",
        ]:
            if not base.exists():
                continue
            for child in base.iterdir():
                if child.is_dir() and (child / "level.dat").exists():
                    return child.resolve()
        return None

    def _setup_behavior_pack(self) -> None:
        """首次启动：释放行为包 + 注册 + 开启实验 API。"""
        world_dir = self._find_world_dir()
        if world_dir is None:
            self.logger.warning("未找到世界目录，跳过行为包安装。")
            return

        level_dat = world_dir / "level.dat"
        bp_dir = world_dir / "behavior_packs" / "endstone_bot_bridge"

        # 1. 释放行为包文件
        if not bp_dir.exists():
            self.logger.info("首次启动：释放行为包到世界目录...")
            self._extract_behavior_pack(bp_dir)
        else:
            # 检查是否需要更新
            manifest = bp_dir / "manifest.json"
            if not manifest.exists():
                self.logger.info("行为包文件不完整，重新释放...")
                self._extract_behavior_pack(bp_dir)

        # 2. 注册到 world_behavior_packs.json
        self._register_in_world_packs(world_dir)

        # 3. 开启实验 API
        if level_dat.exists():
            if is_experiments_enabled(level_dat):
                self.logger.info("Beta APIs 实验功能已启用。")
            else:
                self.logger.info("首次启动：在 level.dat 中启用 Beta APIs 实验功能...")
                if enable_experiments(level_dat):
                    self.logger.warning(
                        "§e已启用 Beta APIs 实验功能！请重启服务器使行为包生效。§r"
                    )
                else:
                    self.logger.error(
                        "启用实验功能失败，请手动在世界设置中开启 Beta APIs。"
                    )

    def _extract_behavior_pack(self, target_dir: Path) -> None:
        """从 whl 包数据释放行为包文件。

        目标目录已有同版本行为包时跳过，避免覆盖玩家手动改动。
        内置散文件存在时直接复制；否则从 .mcpack 压缩包解压。
        """
        import shutil
        import zipfile as _zipfile

        source_dir = Path(__file__).parent / "behavior_pack"
        if not source_dir.exists():
            self.logger.error(f"行为包源文件不存在: {source_dir}")
            return

        # B9：版本一致则跳过释放
        try:
            src_manifest = json.loads(
                (source_dir / "manifest.json").read_text(encoding="utf-8")
            )
            src_version = src_manifest.get("header", {}).get("version")
            dst_manifest_path = target_dir / "manifest.json"
            if src_version and dst_manifest_path.exists():
                dst_manifest = json.loads(dst_manifest_path.read_text(encoding="utf-8"))
                if dst_manifest.get("header", {}).get("version") == src_version:
                    self.logger.info("行为包版本一致，跳过释放（保留现有文件）。")
                    return
        except Exception:
            pass

        target_dir.mkdir(parents=True, exist_ok=True)

        # 优先使用内置散文件（官方推荐目录形式）
        if (source_dir / "manifest.json").exists():
            for item in source_dir.rglob("*"):
                if item.is_file() and item.suffix != ".mcpack":
                    rel = item.relative_to(source_dir)
                    dst = target_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dst)
            self.logger.info(f"行为包已释放到 {target_dir}")
            return

        # 回退：从 .mcpack 压缩包解压
        mcpack = source_dir / "endstone_bot_bridge.mcpack"
        if mcpack.exists():
            try:
                with _zipfile.ZipFile(mcpack) as zf:
                    zf.extractall(target_dir)
                self.logger.info(f"已从 {mcpack.name} 解压行为包到 {target_dir}")
                return
            except Exception as exc:
                self.logger.error(f"从 mcpack 解压失败: {exc}")

        self.logger.error("找不到可用的行为包文件（散文件或 mcpack）。")

    def _register_in_world_packs(self, world_dir: Path) -> None:
        """在 world_behavior_packs.json 中注册行为包。

        解析异常时保留原文件内容，不覆盖其他行为包注册。
        """
        packs_file = world_dir / "world_behavior_packs.json"

        try:
            if packs_file.exists():
                raw_text = packs_file.read_text(encoding="utf-8")
                data = json.loads(raw_text)
                if not isinstance(data, list):
                    self.logger.warning(
                        "world_behavior_packs.json 格式异常（非数组），"
                        "跳过注册以避免覆盖。请手动添加行为包。"
                    )
                    return
            else:
                data = []
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning(
                f"world_behavior_packs.json 解析失败: {exc}，"
                "跳过注册以避免覆盖其他行为包。请手动添加行为包。"
            )
            return

        # 检查是否已注册
        for entry in data:
            if isinstance(entry, dict) and entry.get("pack_id") == self.BEHAVIOR_PACK_UUID:
                return

        data.append({
            "pack_id": self.BEHAVIOR_PACK_UUID,
            "version": self.BEHAVIOR_PACK_VERSION,
        })

        try:
            tmp = packs_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(packs_file)
            self.logger.info("行为包已注册到 world_behavior_packs.json")
        except Exception as exc:
            self.logger.error(f"注册行为包失败: {exc}")

