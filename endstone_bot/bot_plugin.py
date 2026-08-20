"""endstone_bot 主插件。

严格按照 mcbes-manage-script (https://github.com/YueHua46/mcbes-manage-script)
的假人管理逻辑实现，适配 Endstone Python API。

同款逻辑（对应月华 fake-player.ts）：
  - create()          → _cmd_spawn
  - delete()           → _cmd_remove
  - deleteAll()        → _cmd_clearall
  - refresh()          → _tick_self_heal (ensureAllSpawned)
  - moveToOperator()   → _cmd_movehere
  - setLegacySkin()    → _cmd_skin
  - setBehavior()      → _cmd_behavior
  - tickBehaviors()    → _tick_behavior
  - guardPosition()    → _guard_position
  - prepareLegacyEntity() → _prepare_entity
  - spawnLegacyEntity()   → _spawn_entity
  - removeManagedPlayer() → _remove_managed_player
  - 伤害拦截              → _on_actor_damage (entityHurt)
  - 击退清除              → _on_actor_knockback
  - 右键交互              → _on_player_interact_actor
  - buildFakePlayerNameTag → models.build_fake_player_name_tag
  - validateName         → models.validate_name
"""

from __future__ import annotations

import json
import math
import secrets
import threading
from pathlib import Path
from typing import Any

from endstone.command import Command, CommandSender
from endstone.event import (
    ActorDamageEvent,
    ActorDeathEvent,
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

from endstone_bot.ai_client import AIClient
from endstone_bot.gui import BotGUI
from endstone_bot.level_dat import enable_experiments, is_experiments_enabled
from endstone_bot.models import (
    FAKE_PLAYER_TAG,
    FOLLOW_OFFSET_DISTANCE,
    FOLLOW_TELEPORT_DISTANCE_SQ,
    LEGACY_ENTITY_TYPE,
    POSITION_GUARD_DISTANCE_SQ,
    SKINS,
    STATION_THRESHOLD_SQ,
    TAG_ID_PREFIX,
    TAG_OWNER_PREFIX,
    TAG_SKIN_PREFIX,
    BotBehavior,
    FakePlayer,
    build_fake_player_name_tag,
    format_date_time_beijing,
    generate_id,
    get_skin_name,
    normalize_skin_id,
    validate_name,
)


class BotPlugin(Plugin):
    """假人插件（同 mcbes-manage-script 逻辑）。

    两种假人类型：
      - entity: NPC + tickingarea，区块保持加载，原版刷怪自然工作
      - simulated: 通过行为包调用 @minecraft/server-gametest 的 SimulatedPlayer

    行为包桥接：
      - 首次启动自动释放行为包到世界目录
      - 自动在 level.dat 中开启 Beta APIs 实验功能
      - 通过 scriptevent 命令与行为包通信
      - 行为包通过 GameTest 的 spawnSimulatedPlayer 生成模拟玩家
    """

    api_version = "0.11"
    description = (
        "假人插件（同 mcbes-manage-script 逻辑）："
        "两种假人类型、所有者追踪、自愈恢复、位置守护、"
        "伤害拦截、右键交互、皮肤变体、行为系统。"
        "参考项目：mcbes-manage-script。"
        "使用 /bot credits 查看致谢。"
    )

    # tickingarea 参数
    MIN_Y = -64
    MAX_Y = 320
    DEFAULT_CHUNK_RADIUS = 4

    commands = {
        "bot": {
            "description": "管理假人（同 mcbes-manage-script 逻辑）。",
            "usages": [
                "/bot gui",
                "/bot spawn <name> [type] [skin: 0-15]",
                "/bot remove <name>",
                "/bot list",
                "/bot radius <name> <0-4>",
                "/bot info <name>",
                "/bot skin <name> <0-15>",
                "/bot skins",
                "/bot behavior <name> <idle|station|follow> [target]",
                "/bot movehere <name>",
                "/bot clearall",
                "/bot credits",
                "/bot ai <name> on|off|add|remove|list [玩家]",
                "/bot ai-config get|set <baseUrl> <apiKey> <model>",
            ],
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
            "description": "允许管理假人。",
            "default": "op",
        }
    }

    # 行为包常量
    BEHAVIOR_PACK_UUID = "a3f7c2e1-8b4d-4f6a-9c3e-1d2b3c4d5e6f"
    BEHAVIOR_PACK_VERSION = [3, 0, 0]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_enable(self) -> None:
        # 同月华构造函数：初始化 db、注册事件、注册定时任务
        self._bots: dict[str, FakePlayer] = {}  # id → FakePlayer
        self._name_index: dict[str, str] = {}  # name.lower() → id（大小写不敏感）
        self._tick_counter: int = 0
        self._gui = BotGUI(self)
        self._lock = threading.RLock()
        self._db_dirty: bool = False  # 脏标记：高频变化只置标记，定时统一落盘

        # 行为包状态
        self._behavior_pack_active: bool = False
        self._pending_sim_spawns: list[FakePlayer] = []
        self._pending_sim_removes: set[str] = set()  # B4：失联期间待补发的移除名单
        self._bridge_token: str = ""  # 行为包鉴权令牌
        self._pong_received: bool = False  # 本轮 ping 是否收到 pong

        # AI 配置持久化
        self._ai_config_path = self.data_folder / "ai_config.json"
        self._ai_config = self._load_ai_config()
        self._ai = AIClient(
            base_url=self._ai_config.get("baseUrl", ""),
            api_key=self._ai_config.get("apiKey", ""),
            model=self._ai_config.get("model", ""),
        )
        if self._ai.is_configured():
            self.logger.info(f"AI 已配置：{self._ai.model} @ {self._ai.base_url}")
        else:
            self.logger.info("AI 未配置，假人 @ai 功能不可用。使用 /bot ai-config set 来配置。")

    def _load_ai_config(self) -> dict:
        """从 ai_config.json 加载 AI 配置。"""
        path = self._ai_config_path
        if not path.exists():
            return {"baseUrl": "", "apiKey": "", "model": ""}
        try:
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"baseUrl": "", "apiKey": "", "model": ""}
            return {
                "baseUrl": str(data.get("baseUrl", "")),
                "apiKey": str(data.get("apiKey", "")),
                "model": str(data.get("model", "")),
            }
        except Exception:
            return {"baseUrl": "", "apiKey": "", "model": ""}

    def _save_ai_config(self) -> None:
        """保存 AI 配置到 ai_config.json。"""
        import json as _json
        try:
            tmp = self._ai_config_path.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(self._ai_config, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._ai_config_path)
        except Exception as exc:
            self.logger.warning(f"保存 AI 配置失败: {exc}")

    def _update_ai_config(self, base_url: str = "", api_key: str = "", model: str = "") -> None:
        """更新 AI 配置并持久化。"""
        if base_url is not None:
            self._ai_config["baseUrl"] = base_url.strip()
        if api_key is not None:
            self._ai_config["apiKey"] = api_key.strip()
        if model is not None:
            self._ai_config["model"] = model.strip()
        self._ai = AIClient(
            base_url=self._ai_config.get("baseUrl", ""),
            api_key=self._ai_config.get("apiKey", ""),
            model=self._ai_config.get("model", ""),
        )
        self._save_ai_config()

        self._db_path = self.data_folder / "bots.json"
        self.data_folder.mkdir(parents=True, exist_ok=True)

        # 行为包管理：首次启动自动释放行为包 + 开启实验 API
        self._setup_behavior_pack()

        # 清理上次异常退出残留的 tickingarea
        residual = self._remove_all_residual_tickingareas()
        if residual:
            self.logger.info(f"清理了 {residual} 个残留常加载区域。")

        # 从磁盘恢复假人（同月华 ensureAllSpawned 前置：load）
        bots = self._load_db()
        restored = 0
        for fp in bots:
            key = fp.name.lower()
            if key in self._name_index:
                self.logger.warning(f"跳过重复的假人记录: {fp.name}")
                continue
            self._bots[fp.id] = fp
            self._name_index[key] = fp.id
            restored += 1
        if restored:
            self.logger.info(f"从磁盘恢复了 {restored} 个假人。")

        # 生成行为包鉴权令牌
        self._bridge_token = secrets.token_hex(16)

        # 从磁盘恢复假人（同月华 ensureAllSpawned）
        self._ensure_all_spawned()

        # 注册定时任务（同月华 system.runInterval + taskScheduler）
        scheduler = self.server.scheduler
        scheduler.run_task(self, self._tick_behaviors, delay=1, period=1)
        scheduler.run_task(self, self._ensure_all_spawned, delay=40, period=40)
        scheduler.run_task(self, self._persist_positions, delay=600, period=600)

        # 周期性 ping 行为包（每 600 tick）：
        # 1. 检测行为包是否活跃  2. pong 携带玩家列表用于对照自愈
        scheduler.run_task(self, self._ping_behavior_pack, delay=60, period=600)

        self.logger.info(
            "BotPlugin 已启用（同 mcbes-manage-script 逻辑）："
            "两种假人类型 + 伤害拦截 + 自愈 + 位置守护 + 行为系统。"
        )

    def on_disable(self) -> None:
        # 同月华析构：保存数据
        with self._lock:
            self._save_db()
            for fp in list(self._bots.values()):
                if fp.type == "simulated":
                    # B13：服务器关闭时行为包一并停止，SimulatedPlayer 自然消失，
                    # 无需（也无法）发送移除命令；直接清引用即可
                    fp.actor = None
                    continue
                self._remove_managed_player(fp)
            self._bots.clear()
            self._name_index.clear()

    # ------------------------------------------------------------------
    # 命令入口
    # ------------------------------------------------------------------

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        name = command.name.lower()
        if name == "bots":
            return self._cmd_list(sender)
        if name != "bot":
            return False
        if not args:
            # 玩家无参数时打开 GUI，控制台显示用法
            if hasattr(sender, "send_form"):
                self._gui.open_main_menu(sender)
                return True
            self._send_usage(sender)
            return True

        sub = args[0].lower()
        rest = args[1:]

        handlers = {
            "gui": lambda: self._cmd_gui(sender),
            "spawn": lambda: self._cmd_spawn(sender, rest),
            "remove": lambda: self._cmd_remove(sender, rest),
            "list": lambda: self._cmd_list(sender),
            "radius": lambda: self._cmd_radius(sender, rest),
            "info": lambda: self._cmd_info(sender, rest),
            "skin": lambda: self._cmd_skin(sender, rest),
            "skins": lambda: self._cmd_skins(sender),
            "behavior": lambda: self._cmd_behavior(sender, rest),
            "movehere": lambda: self._cmd_movehere(sender, rest),
            "clearall": lambda: self._cmd_clearall(sender),
            "credits": lambda: self._cmd_credits(sender),
            "ai": lambda: self._cmd_ai(sender, rest),
            "ai-config": lambda: self._cmd_ai_config(sender, rest),
        }

        handler = handlers.get(sub)
        if handler is None:
            self._send_usage(sender)
            return True
        return handler()

    # ------------------------------------------------------------------
    # create() → _cmd_spawn（同月华 create 函数）
    # ------------------------------------------------------------------

    def _cmd_spawn(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot spawn <名字> [类型] [皮肤 0-15]")
            return True

        raw_name = args[0]
        # 名称校验（同月华 validateName）
        online_names = {p.name for p in self.server.online_players}
        error = validate_name(raw_name, set(self._name_index.keys()), online_names)
        if error:
            sender.send_error_message(error)
            return True

        name = raw_name.strip()

        # 确定类型
        input_type = args[1].lower() if len(args) >= 2 else "entity"
        if input_type not in ("entity", "simulated"):
            sender.send_error_message(f"未知假人类型：{input_type}，可用：entity, simulated")
            return True

        # simulated 类型需要行为包支持
        if input_type == "simulated" and not self._behavior_pack_active:
            sender.send_message("§e行为包尚未就绪，自动降级为旧版实体假人。§r")
            input_type = "entity"
        fp_type = input_type

        # 皮肤
        skin_id = 0
        if len(args) >= 3:
            try:
                skin_id = normalize_skin_id(int(args[2]))
            except ValueError:
                sender.send_error_message("皮肤编号必须是 0-15 的整数。")
                return True

        # 获取生成位置和所有者
        location = self._sender_location(sender)
        if location is None:
            sender.send_error_message("无法确定生成位置，请由玩家执行该命令。")
            return True

        owner = self._sender_name(sender)
        owner_uuid = self._sender_uuid(sender)
        rotation_x, rotation_y = self._sender_rotation(sender)

        # 构造 IFakePlayer（同月华 create 第 8 步）
        fp = FakePlayer(
            id=generate_id(),
            name=name,
            owner_name=owner,
            owner_uuid=owner_uuid,
            location_x=round(float(location.x), 2),
            location_y=round(float(location.y), 2),
            location_z=round(float(location.z), 2),
            dimension=self._dimension_id(location.dimension),
            rotation_x=round(rotation_x, 2),
            rotation_y=round(rotation_y, 2),
            created=format_date_time_beijing(),
            type=fp_type,
            skin_id=skin_id,
            behavior=BotBehavior(),
            tickingarea_name=f"bot_{name}",
        )

        # 先注册到索引（B2：保证行为包 scriptevent 响应能立即查到该假人）
        self._bots[fp.id] = fp
        self._name_index[fp.name.lower()] = fp.id

        # 生成实体（同月华 spawnForType）
        if fp_type == "simulated":
            # 通过行为包生成 SimulatedPlayer
            self._spawn_simulated_player(fp)
            sender.send_message(
                f"已创建模拟假人 §b{name}§r（所有者：{owner}），"
                f"正在通过行为包生成..."
            )
        else:
            actor = self._spawn_legacy_entity(fp, location.dimension)
            if actor is None:
                # 生成失败：回滚注册
                self._bots.pop(fp.id, None)
                self._name_index.pop(fp.name.lower(), None)
                sender.send_error_message("创建假人失败，请确认当前区块已加载。")
                return True
            fp.actor = actor
            fp.entity_id = self._actor_id(actor)
            self._prepare_legacy_entity(fp)
            self._update_tickingarea(fp)
            skin_name = get_skin_name(skin_id)
            sender.send_message(
                f"已创建假人 §b{name}§r（所有者：{owner}，类型：{fp_type}），"
                f"皮肤：{skin_name}(#{skin_id})。"
            )

        # 持久化
        self._save_db()
        return True

    # ------------------------------------------------------------------
    # delete() → _cmd_remove（同月华 delete 函数）
    # ------------------------------------------------------------------

    def _cmd_remove(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot remove <名字>")
            return True
        name = args[0]
        fp = self._get_by_name(name)
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self._can_manage(sender, fp):
            sender.send_error_message("无权删除该假人。")
            return True
        # 同月华 removeManagedPlayer + db.delete
        self._remove_managed_player(fp)
        self._bots.pop(fp.id, None)
        self._name_index.pop(fp.name.lower(), None)
        self._save_db()
        sender.send_message(f"已删除假人 §b{name}§r。")
        return True

    # ------------------------------------------------------------------
    # deleteAll() → _cmd_clearall（同月华 deleteAll 函数）
    # ------------------------------------------------------------------

    def _cmd_clearall(self, sender: CommandSender) -> bool:
        if not self._is_admin(sender):
            sender.send_error_message("无权删除全服假人。")
            return True

        deleted = 0
        kicked = 0

        # 同月华：遍历数据库删除已知假人
        for fp in list(self._bots.values()):
            if self._is_actor_valid(fp.actor):
                kicked += 1
            self._remove_managed_player(fp)
            deleted += 1

        self._bots.clear()
        self._name_index.clear()
        self._db_dirty = False  # B6：防止后续 _persist_positions 回写空库

        # 同月华：扫描残留 tickingarea
        residual = self._remove_all_residual_tickingareas()

        # 删除持久化文件
        try:
            self._db_path.unlink(missing_ok=True)
        except Exception:
            pass

        sender.send_message(
            f"已清理 {deleted} 个假人，"
            f"移除 {kicked} 个活跃实体，"
            f"额外删除 {residual} 个残留常加载区域。"
        )
        return True

    # ------------------------------------------------------------------
    # list → _cmd_list
    # ------------------------------------------------------------------

    def _cmd_list(self, sender: CommandSender) -> bool:
        if not self._bots:
            sender.send_message("当前没有假人。")
            return True
        parts = []
        for fp in self._bots.values():
            alive = "在线" if self._is_actor_valid(fp.actor) else "离线"
            skin_name = get_skin_name(fp.skin_id)
            parts.append(
                f"§b{fp.name}§r({fp.owner_name}, {alive}, {skin_name}#{fp.skin_id})"
            )
        sender.send_message("假人列表：" + ", ".join(parts))
        return True

    # ------------------------------------------------------------------
    # radius → _cmd_radius
    # ------------------------------------------------------------------

    def _cmd_radius(self, sender: CommandSender, args: list[str]) -> bool:
        if len(args) < 2:
            sender.send_error_message("用法：/bot radius <名字> <0-4>")
            return True
        fp = self._get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self._can_manage(sender, fp):
            sender.send_error_message("无权修改该假人。")
            return True
        try:
            radius = int(args[1])
        except ValueError:
            sender.send_error_message("半径必须是整数。")
            return True
        radius = max(0, min(4, radius))

        # 更新 tickingarea
        dimension = self._get_dimension(fp.dimension)
        if dimension is not None and self._is_actor_valid(fp.actor):
            loc = fp.actor.location
            fp.location_x = round(float(loc.x), 2)
            fp.location_y = round(float(loc.y), 2)
            fp.location_z = round(float(loc.z), 2)

        # 重新创建 tickingarea（radius=0 表示取消常加载，只移除不创建）
        self._remove_tickingarea(fp)
        if radius > 0:
            self._create_tickingarea(fp, radius)
        self._save_db()
        sender.send_message(f"已把 §b{fp.name}§r 的常加载半径设为 {radius} 区块。")
        return True

    # ------------------------------------------------------------------
    # info → _cmd_info
    # ------------------------------------------------------------------

    def _cmd_info(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot info <名字>")
            return True
        fp = self._get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        alive = "在线" if self._is_actor_valid(fp.actor) else "离线(等待自愈)"
        behavior = fp.behavior
        skin_name = get_skin_name(fp.skin_id)
        sender.send_message(
            f"§b{fp.name}§r 信息：\n"
            f"  所有者：{fp.owner_name}\n"
            f"  类型：{fp.type}\n"
            f"  状态：{alive}\n"
            f"  位置：({fp.location_x:.1f}, {fp.location_y:.1f}, {fp.location_z:.1f}) "
            f"维度={fp.dimension}\n"
            f"  朝向：pitch={fp.rotation_x:.1f} yaw={fp.rotation_y:.1f}\n"
            f"  皮肤：{skin_name}(#{fp.skin_id})\n"
            f"  行为：移动={behavior.movement} 动作={behavior.action}\n"
            f"  创建时间：{fp.created}\n"
            f"  ID：{fp.id}"
        )
        return True

    # ------------------------------------------------------------------
    # setLegacySkin() → _cmd_skin（同月华 setLegacySkin 函数）
    # ------------------------------------------------------------------

    def _cmd_skin(self, sender: CommandSender, args: list[str]) -> bool:
        if len(args) < 2:
            sender.send_error_message("用法：/bot skin <名字> <0-15>")
            return True
        fp = self._get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self._can_manage(sender, fp):
            sender.send_error_message("无权修改该假人。")
            return True
        if fp.type != "entity":
            sender.send_error_message("新版模拟玩家不支持二次元皮肤。")
            return True
        try:
            skin_id = int(args[1])
        except ValueError:
            sender.send_error_message("皮肤编号必须是 0-15 的整数。")
            return True
        skin_id = normalize_skin_id(skin_id)
        fp.skin_id = skin_id
        # 同月华 applyLegacySkin：触发皮肤切换
        self._apply_legacy_skin(fp)
        self._save_db()
        skin_name = get_skin_name(skin_id)
        sender.send_message(f"已把 §b{fp.name}§r 的皮肤设为 {skin_name}(#{skin_id})。")
        return True

    def _cmd_skins(self, sender: CommandSender) -> bool:
        lines = ["§b===== 可用皮肤列表 =====§r"]
        for skin in SKINS:
            lines.append(f"  #{skin.id:2d}  {skin.name}")
        sender.send_message("\n".join(lines))
        return True

    # ------------------------------------------------------------------
    # setBehavior() → _cmd_behavior（同月华 setBehavior 函数）
    # ------------------------------------------------------------------

    def _cmd_behavior(self, sender: CommandSender, args: list[str]) -> bool:
        if len(args) < 2:
            sender.send_error_message(
                "用法：/bot behavior <名字> <idle|station|follow> [目标玩家]"
            )
            return True
        fp = self._get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self._can_manage(sender, fp):
            sender.send_error_message("无权控制该假人。")
            return True

        movement = args[1].lower()
        if movement not in ("idle", "station", "follow"):
            sender.send_error_message("行为模式必须是 idle、station 或 follow。")
            return True

        behavior = fp.behavior
        behavior.movement = movement
        behavior.action = "none"
        fp.last_action_tick = 0

        if movement == "follow":
            if len(args) < 3:
                sender.send_error_message("跟随模式需要指定目标玩家名。")
                return True
            target_name = args[2].strip()
            target = self._find_online_player(target_name)
            if target is None:
                sender.send_error_message("跟随目标当前不在线或名称不正确。")
                return True
            behavior.target_player = target_name
            sender.send_message(f"假人 §b{fp.name}§r 现在跟随 §e{target_name}§r。")
        elif movement == "station":
            # 以发送者当前位置作为锁定点（同月华 stationLocation）
            loc = self._sender_location(sender)
            if loc is not None:
                behavior.station_x = round(float(loc.x), 2)
                behavior.station_y = round(float(loc.y), 2)
                behavior.station_z = round(float(loc.z), 2)
                behavior.look_at_x = behavior.station_x
                behavior.look_at_y = behavior.station_y
                behavior.look_at_z = behavior.station_z + 5.0
            sender.send_message(f"假人 §b{fp.name}§r 已锁定到你当前位置。")
        else:
            behavior.target_player = ""
            sender.send_message(f"假人 §b{fp.name}§r 行为设为原地待命。")

        self._save_db()
        return True

    # ------------------------------------------------------------------
    # moveToOperator() → _cmd_movehere（同月华 moveToOperator 函数）
    # ------------------------------------------------------------------

    def _cmd_movehere(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot movehere <名字>")
            return True
        fp = self._get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self._can_manage(sender, fp):
            sender.send_error_message("无权移动该假人。")
            return True
        loc = self._sender_location(sender)
        if loc is None:
            sender.send_error_message("无法确定你的位置。")
            return True

        # 同月华：移除原实体 → 更新坐标 → 重新生成
        self._remove_managed_player(fp)

        fp.location_x = round(float(loc.x), 2)
        fp.location_y = round(float(loc.y), 2)
        fp.location_z = round(float(loc.z), 2)
        fp.dimension = self._dimension_id(loc.dimension)
        rx, ry = self._sender_rotation(sender)
        fp.rotation_x = round(rx, 2)
        fp.rotation_y = round(ry, 2)
        fp.entity_id = ""
        fp.actor = None
        fp.last_area_key = None

        self._save_db()

        # 重新生成
        if fp.type == "simulated":
            # simulated 类型：通过行为包重新生成
            self._spawn_simulated_player(fp)
            sender.send_message(f"已将模拟假人 §b{fp.name}§r 移动到你当前位置。")
            return True

        dimension = self._get_dimension(fp.dimension)
        if dimension is not None:
            actor = self._spawn_legacy_entity(fp, dimension)
            if actor is not None:
                fp.actor = actor
                fp.entity_id = self._actor_id(actor)
                self._prepare_legacy_entity(fp)
                self._update_tickingarea(fp)
                self._save_db()
                sender.send_message(f"已将假人 §b{fp.name}§r 移动到你当前位置。")
                return True

        sender.send_message(
            "§e新位置所在区块未加载，数据已保存，稍后会自动生成。§r"
        )
        return True

    # ------------------------------------------------------------------
    # credits → _cmd_credits
    # ------------------------------------------------------------------

    def _cmd_credits(self, sender: CommandSender) -> bool:
        sender.send_message(
            "§b===== endstone_bot 致谢与参考声明 =====§r\n"
            "\n"
            "§a1. mcbes-manage-script§r\n"
            "   来源：§9https://github.com/YueHua46/mcbes-manage-script§r\n"
            "   许可证：PolyForm Noncommercial License 1.0.0\n"
            "   借鉴内容（全部假人管理逻辑）：\n"
            "     - 两种假人类型设计 (entity / simulated)\n"
            "     - 所有者追踪机制\n"
            "     - 自愈恢复 (ensureAllSpawned)\n"
            "     - 位置守护 (guardPosition)\n"
            "     - 伤害拦截 (entityHurt)\n"
            "     - 击退清除 (clearVelocity)\n"
            "     - 右键交互拦截 (playerInteractWithEntity)\n"
            "     - 皮肤变体 (0-15)\n"
            "     - 持久化数据模型 (IFakePlayer)\n"
            "     - 名称校验 (validateName)\n"
            "     - 头顶名称格式 (buildFakePlayerNameTag)\n"
            "     - 行为系统 (idle / station / follow)\n"
            "     - 移动到操作者 (moveToOperator)\n"
            "     - 清除全部 (deleteAll)\n"
            "\n"
            "§7本项目在上述思路基础上重新实现，适配 Endstone Python API。\n"
            "感谢原作者的开源贡献。§r"
        )
        return True

    # ------------------------------------------------------------------
    # GUI 命令入口和操作方法
    # ------------------------------------------------------------------

    def _cmd_gui(self, sender: CommandSender) -> bool:
        """打开 GUI 主菜单。"""
        if not hasattr(sender, "send_form"):
            sender.send_error_message("GUI 仅限玩家使用。")
            return True
        self._gui.open_main_menu(sender)
        return True

    def _gui_spawn(self, player: Any, name: str, skin_id: int) -> None:
        """GUI 创建假人。"""
        args = [name, "entity", str(skin_id)]
        self._cmd_spawn(player, args)

    def _gui_set_skin(self, player: Any, fp: FakePlayer, skin_id: int) -> None:
        """GUI 切换皮肤。"""
        if not self._can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        fp.skin_id = normalize_skin_id(skin_id)
        self._apply_legacy_skin(fp)
        self._save_db()
        skin_name = get_skin_name(fp.skin_id)
        player.send_message(f"已把 §b{fp.name}§r 的皮肤设为 {skin_name}(#{fp.skin_id})。§r")

    def _gui_set_behavior(self, player: Any, fp: FakePlayer, mode: str, target: str) -> None:
        """GUI 设置行为。"""
        if not self._can_manage(player, fp):
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
            target_player = self._find_online_player(target)
            if target_player is None:
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
                behavior.look_at_x = behavior.station_x
                behavior.look_at_y = behavior.station_y
                behavior.look_at_z = behavior.station_z + 5.0
            player.send_message(f"假人 §b{fp.name}§r 已锁定到你当前位置。§r")
        else:
            behavior.target_player = ""
            player.send_message(f"假人 §b{fp.name}§r 行为设为原地待命。§r")

        self._save_db()

    def _gui_set_radius(self, player: Any, fp: FakePlayer, radius: int) -> None:
        """GUI 调整常加载半径。"""
        if not self._can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        radius = max(0, min(4, radius))

        # 同步当前实体位置
        if self._is_actor_valid(fp.actor):
            try:
                loc = fp.actor.location
                fp.location_x = round(float(loc.x), 2)
                fp.location_y = round(float(loc.y), 2)
                fp.location_z = round(float(loc.z), 2)
            except Exception:
                pass

        # 重新创建 tickingarea
        self._remove_tickingarea(fp)
        if radius > 0:
            self._create_tickingarea(fp, radius)
        self._save_db()
        player.send_message(f"已把 §b{fp.name}§r 的常加载半径设为 {radius} 区块。§r")

    def _gui_movehere(self, player: Any, fp: FakePlayer) -> None:
        """GUI 移动到当前位置。"""
        if not self._can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        self._cmd_movehere(player, [fp.name])

    def _gui_remove(self, player: Any, fp: FakePlayer) -> None:
        """GUI 删除假人。"""
        if not self._can_manage(player, fp):
            player.send_message("§c你没有权限管理该假人。§r")
            return
        self._remove_managed_player(fp)
        self._bots.pop(fp.id, None)
        self._name_index.pop(fp.name.lower(), None)
        self._save_db()
        player.send_message(f"已删除假人 §b{fp.name}§r。§r")

    def _gui_clearall(self, player: Any) -> None:
        """GUI 清除全部。"""
        self._cmd_clearall(player)

    # ==================================================================
    # 事件处理（同月华 4 个事件订阅器）
    # ==================================================================

    # ------------------------------------------------------------------
    # @ai 聊天事件（玩家 @假人名字 唤醒 AI 对话）
    # ------------------------------------------------------------------

    @event_handler(priority=EventPriority.HIGHEST)
    def on_player_chat(self, event: PlayerChatEvent) -> None:
        """监听玩家聊天，检测 @假人名字 唤醒 AI 对话。"""
        player = event.player
        message = event.message.strip()

        if not message.startswith("@"):
            return

        # 提取唤醒词（@假人名）
        parts = message[1:].split(maxsplit=1)
        if not parts:
            return
        bot_name = parts[0].strip()
        query = parts[1].strip() if len(parts) > 1 else ""

        # 查找匹配的假人
        fp = self._get_by_name(bot_name)
        if fp is None:
            return  # 没有这个假人，不拦截

        # 检查该假人 AI 是否开启
        if not fp.ai_enabled:
            return

        player_name = str(getattr(player, "name", "") or "").strip()
        if not player_name:
            return

        # 权限检查：owner 或白名单成员
        is_owner = (
            (fp.owner_uuid and str(getattr(player, "unique_id", "") or "") == fp.owner_uuid)
            or (fp.owner_name and player_name.lower() == fp.owner_name.lower())
        )
        is_member = player_name.lower() in {n.lower() for n in fp.ai_members}

        if not (is_owner or is_member):
            # 未授权玩家：取消事件，不公开聊天
            event.cancel()
            return

        # 权限通过，拦截聊天，交给 AI 处理
        event.cancel()
        # 用线程处理网络 IO，避免阻塞服务器主线程
        threading.Thread(
            target=self._handle_ai_mention,
            args=(fp, player, query),
            daemon=True,
        ).start()

    def _handle_ai_mention(
        self, fp: FakePlayer, player: Any, query: str
    ) -> None:
        """处理 @假人名字 AI 对话。"""
        if not self._ai.is_configured():
            try:
                player.send_message(f"§c假人 §b{fp.name} §c的 AI 未配置，请联系管理员。")
            except Exception:
                pass
            return

        if not query:
            try:
                player.send_message(
                    f"§e用法：@{fp.name} <指令>\n"
                    f"§7例如：@{fp.name} 去砍树"
                )
            except Exception:
                pass
            return

        player_name = str(getattr(player, "name", "") or "")
        self.logger.info(f"[AI] §b{fp.name}§r 收到 §e{player_name}§r: {query}")

        # 构建系统 prompt
        system_prompt = (
            f"你是 Minecraft 服务器中的假人「{fp.name}」。\n"
            f"玩家 {player_name} 通过 @ 指令向你提问。\n"
            f"你只能通过发送聊天消息回复玩家（Minecraft 公开聊天）。\n"
            f"请用简短、自然的中文回复玩家的指令，\n"
            f"如果需要执行动作则直接说明你在做什么。\n"
            f"不要假设你有脚 location 或背包信息，只能基于玩家指令回复。\n"
            f"保持角色设定：假人助手，友好、有耐心。\n"
            f"回复长度控制在 200 字符以内。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        try:
            reply = self._ai.chat(messages, temperature=0.7, max_tokens=300)
            # 发到公开聊天
            if reply:
                reply = str(reply).strip()[:200]
                # 去掉可能的 Markdown 符号
                reply = reply.replace("**", "").replace("*", "").replace("_", "")
                self.server.broadcast_message(
                    f"§b[{fp.name}]§r {reply}"
                )
                self.logger.info(f"[AI] §b{fp.name}§r 回复: {reply}")
            else:
                player.send_message(f"§c假人 §b{fp.name} §c返回为空，请稍后重试。")
        except Exception as exc:
            self.logger.warning(f"[AI] §b{fp.name}§r 处理失败: {exc}")
            try:
                player.send_message(f"§cAI 处理失败：{exc}。请稍后重试。")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # AI 管理命令：/bot ai <name> on|off|add|remove|list [玩家]
    # ------------------------------------------------------------------

    def _cmd_ai(self, sender: CommandSender, args: list[str]) -> bool:
        """AI 管理命令：开启/关闭/成员管理/查看 AI 配置。"""
        if not args:
            sender.send_error_message(
                "用法：/bot ai <假人名> on|off|add|remove|list [玩家]"
            )
            return True

        name = args[0]
        fp = self._get_by_name(name)
        if fp is None:
            sender.send_error_message(f"假人 §b{name}§r 不存在。")
            return True

        # 权限检查：owner 或 OP
        if not self._can_manage(sender, fp):
            sender.send_error_message("无权管理该假人的 AI。")
            return True

        if len(args) < 2:
            self._ai_show(fp, sender)
            return True

        action = args[1].lower()

        if action in ("on", "enable", "开启"):
            fp.ai_enabled = True
            self._save_db()
            sender.send_message(
                f"假人 §b{fp.name}§r 的 AI 已开启。"
                f"玩家可通过 §e@{fp.name} <指令>§r 与 AI 对话。"
            )
            self.logger.info(f"假人 {fp.name} AI 已开启")
            return True

        if action in ("off", "disable", "关闭"):
            fp.ai_enabled = False
            self._save_db()
            sender.send_message(f"假人 §b{fp.name}§r 的 AI 已关闭。")
            self.logger.info(f"假人 {fp.name} AI 已关闭")
            return True

        if action in ("add", "添加", "+"):
            if len(args) < 3:
                sender.send_error_message("用法：/bot ai <名字> add <玩家>")
                return True
            target = args[2].strip()
            if not target:
                sender.send_error_message("玩家名不能为空。")
                return True
            target_lower = target.lower()
            if target_lower in {n.lower() for n in fp.ai_members}:
                sender.send_message(f"§e{target}§r 已在授权列表中。")
                return True
            fp.ai_members.append(target)
            self._save_db()
            sender.send_message(
                f"已授权 §e{target}§r 使用假人 §b{fp.name}§r 的 AI。"
            )
            self.logger.info(f"假人 {fp.name} 授权 {target} 使用 AI")
            return True

        if action in ("remove", "del", "删除", "-"):
            if len(args) < 3:
                sender.send_error_message("用法：/bot ai <名字> remove <玩家>")
                return True
            target = args[2].strip()
            target_lower = target.lower()
            for i, m in enumerate(fp.ai_members):
                if m.lower() == target_lower:
                    fp.ai_members.pop(i)
                    self._save_db()
                    sender.send_message(
                        f"已移除 §e{target}§r 的 AI 授权。"
                    )
                    self.logger.info(f"假人 {fp.name} 移除 {target} AI 授权")
                    return True
            sender.send_message(f"§e{target}§r 不在授权列表中。")
            return True

        if action == "list" or action == "列表":
            self._ai_show(fp, sender)
            return True

        sender.send_error_message(
            "用法：/bot ai <名字> on|off|add|remove|list [玩家]"
        )
        return True

    def _ai_show(self, fp: FakePlayer, sender: CommandSender) -> None:
        """展示假人的 AI 配置。"""
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

    # ------------------------------------------------------------------
    # AI 全局配置命令：/bot ai-config get|set
    # ------------------------------------------------------------------

    def _cmd_ai_config(self, sender: CommandSender, args: list[str]) -> bool:
        """查看或设置全局 AI 配置（API 地址、Key、模型）。"""
        if not args:
            self._ai_config_show(sender)
            return True

        action = args[0].lower()
        if action == "get":
            self._ai_config_show(sender)
            return True

        if action == "set":
            if len(args) < 4:
                sender.send_error_message(
                    "用法：/bot ai-config set <baseUrl> <apiKey> <model>\n"
                    "示例：/bot ai-config set https://api.openai.com/v1 sk-xxx gpt-4o-mini"
                )
                return True
            base_url = args[1].strip()
            api_key = args[2].strip()
            model = args[3].strip()
            self._update_ai_config(base_url=base_url, api_key=api_key, model=model)
            sender.send_message(
                f"§aAI 配置已更新：\n"
                f"  地址：{base_url}\n"
                f"  模型：{model}\n"
                f"  状态：{'§a可用' if self._ai.is_configured() else '§c配置无效，请检查参数'}"
            )
            self.logger.info(f"AI 配置已更新 by {getattr(sender, 'name', 'console')}: {base_url} {model}")
            return True

        if action == "test":
            if not self._ai.is_configured():
                sender.send_error_message("AI 未配置，请先 /bot ai-config set")
                return True
            sender.send_message("§e正在测试 AI 连接...")
            test_reply = self._ai.chat([{"role": "user", "content": "你好，返回 OK"}], temperature=0.1, max_tokens=20)
            if test_reply and "OK" in test_reply.upper():
                sender.send_message(f"§aAI 测试成功：{test_reply.strip()}")
            else:
                sender.send_error_message(f"§cAI 测试失败：{test_reply}")
            return True

        sender.send_error_message("用法：/bot ai-config get|set|test")
        return True

    def _ai_config_show(self, sender: CommandSender) -> None:
        """展示当前 AI 全局配置。"""
        cfg = self._ai_config
        url = cfg.get("baseUrl", "")
        model = cfg.get("model", "")
        key_masked = cfg.get("apiKey", "")
        if key_masked:
            key_masked = key_masked[:8] + "****" if len(key_masked) > 8 else "****"
        ready = "§a可用" if self._ai.is_configured() else "§c未配置"
        sender.send_message(
            f"§b===== AI 全局配置 =====\n"
            f"  状态：{ready}\n"
            f"  地址：§7{url or '（未设置）'}\n"
            f"  模型：§7{model or '（未设置）'}\n"
            f"  Key：§7{key_masked or '（未设置）'}\n"
            f"\n§7配置：/bot ai-config set <baseUrl> <apiKey> <model>"
        )

    # ------------------------------------------------------------------
    # 伤害拦截（同月华 entityHurt → ActorDamageEvent）
    # ------------------------------------------------------------------

    @event_handler(priority=EventPriority.HIGH)
    def on_actor_damage(self, event: ActorDamageEvent) -> None:
        """同月华 entityHurt：假人完全免伤（entity 类型）。"""
        actor = event.actor
        if not self._is_fake_player_actor(actor):
            return
        # entity 类型：完全免伤（cancel 所有伤害）
        event.cancel()

    # ------------------------------------------------------------------
    # 击退清除（同月华 clearVelocity → ActorKnockbackEvent）
    # ------------------------------------------------------------------

    @event_handler(priority=EventPriority.HIGH)
    def on_actor_knockback(self, event: ActorKnockbackEvent) -> None:
        """同月华 clearVelocity：取消假人受到的击退。"""
        actor = event.actor
        if not self._is_fake_player_actor(actor):
            return
        # 同月华：clearVelocity 清除击退
        event.cancel()

    # ------------------------------------------------------------------
    # 右键交互（同月华 playerInteractWithEntity → PlayerInteractActorEvent）
    # ------------------------------------------------------------------

    @event_handler(priority=EventPriority.HIGH)
    def on_player_interact_actor(self, event: PlayerInteractActorEvent) -> None:
        """同月华 playerInteractWithEntity：拦截右键，打开 GUI 管理菜单。"""
        actor = event.actor
        if not self._is_fake_player_actor(actor):
            return

        player = event.player
        # 同月华：取消原版交互
        event.cancel()

        fp = self._get_by_actor(actor)
        if fp is None:
            player.send_message("§c这个假人的数据不存在。§r")
            return

        # 右键直接打开 GUI 假人管理菜单
        self._gui.open_bot_manage(player, fp)

    # ------------------------------------------------------------------
    # 实体移除（同月华 entityRemove → ActorRemoveEvent）
    # ------------------------------------------------------------------

    @event_handler
    def on_actor_remove(self, event: ActorRemoveEvent) -> None:
        """同月华 entityRemove：实体被移除时清除运行时引用。"""
        actor = event.actor
        if not self._is_fake_player_actor(actor):
            return
        fp = self._get_by_actor(actor)
        if fp is None:
            return
        # 清除运行时引用，等待自愈任务重新生成
        fp.actor = None
        fp.entity_id = ""
        fp.last_area_key = None

    # ==================================================================
    # 定时任务（同月华 tickBehaviors + ensureAllSpawned）
    # ==================================================================

    # ------------------------------------------------------------------
    # tickBehaviors() → _tick_behaviors（同月华每 tick 行为调度）
    # ------------------------------------------------------------------

    def _tick_behaviors(self) -> None:
        """同月华 tickBehaviors：每 tick 执行假人行为 + 每 100 tick 同步坐标到内存。

        坐标变化只置脏标记，由 _persist_positions（每 600 tick）统一落盘，
        避免高频全量写盘。
        """
        self._tick_counter += 1

        # 快照迭代，避免迭代中字典被修改（RuntimeError）
        with self._lock:
            bots_snapshot = list(self._bots.values())

        for fp in bots_snapshot:
            if not self._is_actor_valid(fp.actor):
                continue

            behavior = fp.behavior

            # 同月华：idle + none + 不蹲下 → 仅位置守护
            if behavior.movement == "idle" and behavior.action == "none" and not behavior.sneaking:
                self._guard_position(fp)
                continue

            self._apply_behavior(fp, force=False)

            # 同月华：每 100 tick 同步 entity 类型坐标到内存（simulated 由行为包上报）
            if self._tick_counter % 100 == 0 and fp.type == "entity":
                try:
                    loc = fp.actor.location
                    new_x = round(float(loc.x), 2)
                    new_y = round(float(loc.y), 2)
                    new_z = round(float(loc.z), 2)
                    if (
                        fp.location_x != new_x
                        or fp.location_y != new_y
                        or fp.location_z != new_z
                    ):
                        fp.location_x = new_x
                        fp.location_y = new_y
                        fp.location_z = new_z
                        self._db_dirty = True
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # ensureAllSpawned() → _ensure_all_spawned（同月华自愈）
    # ------------------------------------------------------------------

    def _ensure_all_spawned(self) -> None:
        """同月华 ensureAllSpawned：检测失效假人并重新生成。"""
        for fp in list(self._bots.values()):
            # simulated 类型：通过行为包重新生成
            if fp.type == "simulated":
                if not self._behavior_pack_active:
                    # 行为包未就绪，加入待处理队列
                    if fp not in self._pending_sim_spawns:
                        self._pending_sim_spawns.append(fp)
                    continue
                if fp.sim_spawn_confirmed:
                    # 行为包已确认生成，不再重发
                    continue
                # 未确认：重发生成命令，行为包同名会幂等跳过
                self.logger.info(f"模拟假人 §b{fp.name}§r 未确认生成，重发生成命令...")
                self._spawn_simulated_player(fp)
                continue

            # entity 类型：检测实体是否失效
            if self._is_actor_valid(fp.actor):
                continue
            self.logger.info(f"假人 {fp.name} 实体失效，尝试自愈...")
            fp.actor = None
            fp.entity_id = ""
            fp.last_area_key = None

            dimension = self._get_dimension(fp.dimension)
            if dimension is None:
                self.logger.warning(
                    f"假人 {fp.name} 自愈失败：维度 {fp.dimension} 不可用。"
                )
                continue

            actor = self._spawn_legacy_entity(fp, dimension)
            if actor is not None:
                fp.actor = actor
                fp.entity_id = self._actor_id(actor)
                self._prepare_legacy_entity(fp)
                self._update_tickingarea(fp)
                self._save_db()
                self.logger.info(f"假人 {fp.name} 自愈成功。")
            else:
                self.logger.warning(
                    f"假人 {fp.name} 自愈失败，所在区块可能未加载。"
                )

    # ------------------------------------------------------------------
    # 持久化位置同步
    # ------------------------------------------------------------------

    def _persist_positions(self) -> None:
        """定期把内存中的坐标/脏数据落盘（每 600 tick 一次）。

        - entity 类型：从 actor 引用同步坐标（供重启后恢复）
        - simulated 类型：坐标已由 bot:positions 上报更新到内存
        - 有脏标记或坐标变化时才写盘
        """
        changed = False

        # 快照迭代，避免迭代中字典被修改
        with self._lock:
            bots_snapshot = list(self._bots.values())
            dirty = self._db_dirty

        for fp in bots_snapshot:
            if fp.type == "entity" and self._is_actor_valid(fp.actor):
                try:
                    loc = fp.actor.location
                    new_x = round(float(loc.x), 2)
                    new_y = round(float(loc.y), 2)
                    new_z = round(float(loc.z), 2)
                    if (
                        fp.location_x != new_x
                        or fp.location_y != new_y
                        or fp.location_z != new_z
                    ):
                        fp.location_x = new_x
                        fp.location_y = new_y
                        fp.location_z = new_z
                        changed = True
                except Exception:
                    pass

        if changed or dirty:
            self._save_db()

    # ==================================================================
    # 核心逻辑（同月华各内部函数）
    # ==================================================================

    # ------------------------------------------------------------------
    # spawnLegacyEntity() → _spawn_legacy_entity（同月华生成旧版实体）
    # ------------------------------------------------------------------

    def _spawn_legacy_entity(self, fp: FakePlayer, dimension: Any) -> Any:
        """同月华 spawnLegacyEntity：在存储坐标处生成实体。"""
        try:
            loc = Location(dimension, fp.location_x, fp.location_y, fp.location_z)
            actor = dimension.spawn_actor(loc, LEGACY_ENTITY_TYPE)
            return actor
        except Exception as exc:
            self.logger.debug(f"生成假人实体失败 {fp.name}: {exc}")
            return None

    # ------------------------------------------------------------------
    # prepareLegacyEntity() → _prepare_legacy_entity（同月华绑定元数据）
    # ------------------------------------------------------------------

    def _prepare_legacy_entity(self, fp: FakePlayer) -> None:
        """同月华 prepareLegacyEntity：设置 nameTag、tag、皮肤、位置。"""
        actor = fp.actor
        if actor is None:
            return
        try:
            # 同月华 buildFakePlayerNameTag
            actor.name_tag = build_fake_player_name_tag(fp)
            actor.is_name_tag_visible = True
            actor.is_name_tag_always_visible = True
            # 同月华 addTag
            actor.add_scoreboard_tag(FAKE_PLAYER_TAG)
            actor.add_scoreboard_tag(f"{TAG_ID_PREFIX}{fp.id}")
            actor.add_scoreboard_tag(f"{TAG_OWNER_PREFIX}{fp.owner_name}")
        except Exception:
            pass
        # 同月华 applyLegacySkin
        self._apply_legacy_skin(fp)

    # ------------------------------------------------------------------
    # applyLegacySkin() → _apply_legacy_skin（同月华触发皮肤事件）
    # ------------------------------------------------------------------

    def _apply_legacy_skin(self, fp: FakePlayer) -> None:
        """同月华 applyLegacySkin：通过 tag 标记皮肤变体。"""
        actor = fp.actor
        if actor is None:
            return
        try:
            # 移除旧 skin tag
            for tag in list(actor.scoreboard_tags or []):
                if tag.startswith(TAG_SKIN_PREFIX):
                    actor.remove_scoreboard_tag(tag)
            # 添加新 skin tag（同月华 yuehua:set_skin_{id}）
            actor.add_scoreboard_tag(f"{TAG_SKIN_PREFIX}{fp.skin_id}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # removeManagedPlayer() → _remove_managed_player（同月华移除实体）
    # ------------------------------------------------------------------

    def _remove_managed_player(self, fp: FakePlayer) -> None:
        """同月华 removeManagedPlayer：移除实体 + tickingarea。"""
        # simulated 类型：通过行为包移除
        if fp.type == "simulated":
            self._remove_simulated_player(fp)
            return

        # entity 类型：移除 NPC + tickingarea
        self._remove_tickingarea(fp)
        if self._is_actor_valid(fp.actor):
            try:
                fp.actor.remove()
            except Exception as exc:
                self.logger.debug(f"移除假人实体失败 {fp.name}: {exc}")
        fp.actor = None
        fp.entity_id = ""
        fp.last_area_key = None

    # ------------------------------------------------------------------
    # guardPosition() → _guard_position（同月华位置守护）
    # ------------------------------------------------------------------

    def _guard_position(self, fp: FakePlayer) -> None:
        """同月华 guardPosition：idle 假人被推离原位时传送回。"""
        if fp.behavior.movement != "idle":
            return
        try:
            loc = fp.actor.location
            dx = loc.x - fp.location_x
            dy = loc.y - fp.location_y
            dz = loc.z - fp.location_z
            dist_sq = dx * dx + dy * dy + dz * dz
            if dist_sq <= POSITION_GUARD_DISTANCE_SQ:
                # 在原位，不需要传送
                return
            # 被推离原位，传送回存储位置并恢复朝向
            dimension = self._get_dimension(fp.dimension)
            if dimension is not None:
                target = Location(
                    dimension,
                    fp.location_x,
                    fp.location_y,
                    fp.location_z,
                    pitch=fp.rotation_x,
                    yaw=fp.rotation_y,
                )
                self._safe_teleport(fp.actor, target)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # applyBehavior() → _apply_behavior（同月华行为执行）
    # ------------------------------------------------------------------

    def _apply_behavior(self, fp: FakePlayer, force: bool) -> None:
        """同月华 applyBehavior：执行假人行为。"""
        behavior = fp.behavior

        # B3：simulated 假人的 actor 在行为包侧（插件无引用），
        # 行为通过「行为包上报坐标 + scriptevent bot:teleport」实现
        if fp.type == "simulated":
            self._apply_simulated_behavior(fp, behavior, force)
            return

        actor = fp.actor
        if not self._is_actor_valid(actor):
            return

        # force=true 时停止所有动作（同月华 stopMoving 等）
        # NPC 没有这些 API，跳过

        # 移动逻辑（同月华：每 10 tick 或 force 时执行）
        if force or self._tick_counter % 10 == 0:
            if behavior.movement == "station":
                self._handle_station(fp, behavior)
            elif behavior.movement == "follow":
                self._handle_follow(fp, behavior)
            elif behavior.movement == "idle":
                self._guard_position(fp)

        # 动作逻辑（NPC 不支持 attack/jump/interact，仅记录周期）
        if behavior.action != "none":
            interval = behavior.interval_ticks
            if force or self._tick_counter - fp.last_action_tick >= interval:
                fp.last_action_tick = self._tick_counter
                self.logger.debug(
                    f"假人 {fp.name} 行为 {behavior.action} 已触发（NPC 能力受限）"
                )

    def _apply_simulated_behavior(
        self, fp: FakePlayer, behavior: BotBehavior, force: bool
    ) -> None:
        """simulated 假人行为：基于行为包上报坐标 + scriptevent 传送实现。

        - idle：位置守护，被推离存储锚点后传送回锚点
        - station：锁定站桩点，偏离后传送回站桩点
        - follow：与目标玩家距离过大时传送到目标身后
        """
        if not self._behavior_pack_active:
            return
        if not fp.sim_has_position:
            # 尚未收到行为包首次坐标上报，跳过本次行为计算
            return
        if not (force or self._tick_counter % 10 == 0):
            return

        try:
            if behavior.movement == "station":
                sx, sy, sz = behavior.station_x, behavior.station_y, behavior.station_z
                if sx is None or sy is None or sz is None:
                    return
                dx = fp.sim_actual_x - sx
                dy = fp.sim_actual_y - sy
                dz = fp.sim_actual_z - sz
                if dx * dx + dy * dy + dz * dz > STATION_THRESHOLD_SQ:
                    self._teleport_simulated_player(fp, sx, sy, sz)
            elif behavior.movement == "follow":
                target_name = behavior.target_player.strip()
                if not target_name:
                    return
                target = self._find_online_player(target_name)
                if target is None:
                    return
                target_loc = target.location
                target_dim = self._dimension_id(target_loc.dimension)
                if target_dim != fp.dimension:
                    return  # 跨维度不跟随
                dx = target_loc.x - fp.sim_actual_x
                dy = target_loc.y - fp.sim_actual_y
                dz = target_loc.z - fp.sim_actual_z
                if dx * dx + dy * dy + dz * dz > FOLLOW_TELEPORT_DISTANCE_SQ:
                    yaw = self._player_yaw(target)
                    offset_x = -math.sin(yaw) * FOLLOW_OFFSET_DISTANCE
                    offset_z = math.cos(yaw) * FOLLOW_OFFSET_DISTANCE
                    self._teleport_simulated_player(
                        fp,
                        target_loc.x + offset_x,
                        target_loc.y,
                        target_loc.z + offset_z,
                    )
            elif behavior.movement == "idle":
                dx = fp.sim_actual_x - fp.location_x
                dy = fp.sim_actual_y - fp.location_y
                dz = fp.sim_actual_z - fp.location_z
                if dx * dx + dy * dy + dz * dz > POSITION_GUARD_DISTANCE_SQ:
                    self._teleport_simulated_player(
                        fp, fp.location_x, fp.location_y, fp.location_z
                    )
        except Exception as exc:
            self.logger.debug(f"simulated 行为执行失败 {fp.name}: {exc}")

    def _teleport_simulated_player(
        self, fp: FakePlayer, x: float, y: float, z: float
    ) -> None:
        """通过行为包传送 simulated 假人。"""
        self._send_scriptevent("bot:teleport", {
            "n": fp.name,
            "x": round(float(x), 2),
            "y": round(float(y), 2),
            "z": round(float(z), 2),
            "d": fp.dimension,
        })

    def _handle_station(self, fp: FakePlayer, behavior: BotBehavior) -> None:
        """同月华 station 模式：锁定到指定坐标。"""
        if behavior.station_x is None or behavior.station_y is None or behavior.station_z is None:
            return
        try:
            loc = fp.actor.location
            dx = loc.x - behavior.station_x
            dy = loc.y - behavior.station_y
            dz = loc.z - behavior.station_z
            if dx * dx + dy * dy + dz * dz > STATION_THRESHOLD_SQ:
                dimension = self._get_dimension(fp.dimension)
                if dimension is not None:
                    target = Location(
                        dimension,
                        behavior.station_x,
                        behavior.station_y,
                        behavior.station_z,
                    )
                    self._safe_teleport(fp.actor, target)
        except Exception:
            pass

    def _handle_follow(self, fp: FakePlayer, behavior: BotBehavior) -> None:
        """同月华 follow 模式：跟随指定玩家。

        NPC 不支持 navigateToEntity，改用传送实现。
        """
        target_name = behavior.target_player.strip()
        if not target_name:
            return
        target = self._find_online_player(target_name)
        if target is None:
            return
        try:
            target_loc = target.location
            bot_loc = fp.actor.location
            # B11：跨维度不跟随（Endstone 跨维度 teleport 可能失败/拒绝）
            bot_dim = self._dimension_id(bot_loc.dimension)
            target_dim = self._dimension_id(target_loc.dimension)
            if bot_dim != target_dim:
                return
            dx = target_loc.x - bot_loc.x
            dy = target_loc.y - bot_loc.y
            dz = target_loc.z - bot_loc.z
            dist_sq = dx * dx + dy * dy + dz * dz
            if dist_sq > FOLLOW_TELEPORT_DISTANCE_SQ:
                # 在目标身后生成
                yaw = self._player_yaw(target)
                offset_x = -math.sin(yaw) * FOLLOW_OFFSET_DISTANCE
                offset_z = math.cos(yaw) * FOLLOW_OFFSET_DISTANCE
                follow_loc = Location(
                    target_loc.dimension,
                    target_loc.x + offset_x,
                    target_loc.y,
                    target_loc.z + offset_z,
                )
                self._safe_teleport(fp.actor, follow_loc)
        except Exception as exc:
            self.logger.debug(f"跟随行为失败 {fp.name}: {exc}")

    # ==================================================================
    # tickingarea 管理（替代月华 minecraft:tick_world 组件）
    # ==================================================================

    def _update_tickingarea(self, fp: FakePlayer) -> bool:
        """创建或更新假人的常加载区域。"""
        if not self._is_actor_valid(fp.actor):
            return False
        try:
            loc = fp.actor.location
        except Exception:
            return False
        chunk_x, chunk_z = self._chunk_coords(loc.x, loc.z)
        dim_id = self._dimension_id(loc.dimension)
        radius = self.DEFAULT_CHUNK_RADIUS
        area_key = (dim_id, chunk_x, chunk_z, radius)
        if fp.last_area_key == area_key:
            return True

        self._remove_tickingarea(fp)
        self._create_tickingarea(fp, radius, chunk_x, chunk_z, dim_id)
        return True

    def _create_tickingarea(
        self, fp: FakePlayer, radius: int,
        chunk_x: int | None = None, chunk_z: int | None = None, dim_id: str | None = None,
    ) -> bool:
        if chunk_x is None or chunk_z is None or dim_id is None:
            if not self._is_actor_valid(fp.actor):
                return False
            try:
                loc = fp.actor.location
                chunk_x, chunk_z = self._chunk_coords(loc.x, loc.z)
                dim_id = self._dimension_id(loc.dimension)
            except Exception:
                return False

        start_x, start_z, end_x, end_z = self._tickingarea_bounds(chunk_x, chunk_z, radius)
        command = (
            f"tickingarea add {start_x} {self.MIN_Y} {start_z} "
            f"{end_x} {self.MAX_Y} {end_z} {fp.tickingarea_name} true"
        )
        ok = self._dispatch(f"execute in {dim_id} run {command}")
        if ok:
            fp.last_area_key = (dim_id, chunk_x, chunk_z, radius)
        return ok

    def _remove_tickingarea(self, fp: FakePlayer) -> None:
        if not fp.tickingarea_name or fp.last_area_key is None:
            return
        dim_id = fp.last_area_key[0] if fp.last_area_key else "overworld"
        self._dispatch(f"execute in {dim_id} run tickingarea remove {fp.tickingarea_name}")
        fp.last_area_key = None

    def _remove_all_residual_tickingareas(self) -> int:
        removed = 0
        for dim_id in ("overworld", "nether", "the_end"):
            names = self._list_bot_tickingareas(dim_id)
            for area_name in names:
                if self._dispatch(f"execute in {dim_id} run tickingarea remove {area_name}"):
                    removed += 1
        return removed

    def _list_bot_tickingareas(self, dimension_id: str) -> set[str]:
        from endstone.command import CommandSenderWrapper

        lines: list[str] = []

        def capture(message: Any) -> None:
            lines.append(str(message))

        try:
            wrapper = CommandSenderWrapper(
                self.server.command_sender,
                on_message=capture,
                on_error=capture,
            )
            self.server.dispatch_command(wrapper, f"execute in {dimension_id} run tickingarea list")
        except Exception:
            pass

        names: set[str] = set()
        for line in lines:
            text = str(line).strip()
            if text.startswith("-"):
                text = text[1:].strip()
            # B7：兼容带序号前缀的格式，如 "- 0: bot_x: (0,0,0) to (16,0,16)"
            if ":" in text:
                head = text.split(":", 1)[0].strip()
                if head.isdigit():
                    text = text.split(":", 1)[1].strip()
            if not text.startswith("bot_") or ":" not in text:
                continue
            name = text.split(":", 1)[0].strip()
            if name:
                names.add(name)
        return names

    # ==================================================================
    # 持久化（同月华 db.set / db.save / db.load）
    # ==================================================================

    def _save_db(self) -> None:
        with self._lock:
            data = {
                "version": 4,
                "bots": [fp.to_record() for fp in self._bots.values()],
            }
            try:
                tmp = self._db_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self._db_path)
                self._db_dirty = False
            except Exception as exc:
                self.logger.warning(f"保存 bots.json 失败: {exc}")

    def _load_db(self) -> list[FakePlayer]:
        with self._lock:
            if not self._db_path.exists():
                return []
            try:
                data = json.loads(self._db_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.logger.warning(f"读取 bots.json 失败: {exc}")
                return []
            records = []
            for item in data.get("bots", []):
                try:
                    records.append(FakePlayer.from_record(item))
                except Exception as exc:
                    self.logger.warning(f"解析假人记录失败，跳过: {exc}")
            return records

    # ==================================================================
    # 工具方法
    # ==================================================================

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
        try:
            return bool(sender.is_op)
        except Exception:
            return False

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
            "用法：/bot gui|spawn|remove|list|radius|info|skin|skins|behavior|movehere|clearall|credits，或 /bots\n"
            "§7提示：玩家直接输入 /bot 可打开 GUI 管理界面。§r"
        )

    # ==================================================================
    # 行为包管理（自动释放、注册、开启实验 API）
    # ==================================================================

    def _find_world_dir(self) -> Path | None:
        """查找当前世界目录。"""
        # 尝试通过 server.level 获取世界名称
        try:
            level = self.server.level
            name = level.name
            for base in [Path("worlds"), Path.cwd() / "worlds"]:
                world_dir = base / name
                if (world_dir / "level.dat").exists():
                    return world_dir.resolve()
        except Exception:
            pass

        # 搜索 worlds/ 目录下含 level.dat 的文件夹
        for worlds_base in [Path("worlds"), Path.cwd() / "worlds"]:
            if not worlds_base.exists():
                continue
            for child in worlds_base.iterdir():
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
        """
        import shutil

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

        # 复制所有文件
        for item in source_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(source_dir)
                dst = target_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst)

        self.logger.info(f"行为包已释放到 {target_dir}")

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

    # ==================================================================
    # 行为包通信（scriptevent）
    # ==================================================================

    def _send_scriptevent(self, event_id: str, data: dict) -> bool:
        """发送 scriptevent 到行为包（携带鉴权令牌）。"""
        data_with_token = {**data, "t": self._bridge_token}
        msg = json.dumps(data_with_token, ensure_ascii=False, separators=(",", ":"))
        command = f"scriptevent {event_id} {msg}"
        return self._dispatch(command)

    def _ping_behavior_pack(self) -> None:
        """周期性 ping 行为包：检测活跃状态 + 对照管理的玩家列表。"""
        self._pong_received = False
        self._send_scriptevent("bot:ping", {})
        # 5 秒后检查响应
        self.server.scheduler.run_task(
            self, self._check_ping_response, delay=100
        )

    def _check_ping_response(self) -> None:
        """检查 ping 响应：失联时重置确认状态以便恢复后重新生成。"""
        if self._pong_received:
            return
        if self._behavior_pack_active:
            self._behavior_pack_active = False
            self.logger.warning("§e行为包失去响应，模拟假人自愈暂停，将持续重试。§r")
        else:
            self.logger.warning(
                "§e行为包未响应。可能原因：\n"
                "  1. 服务器未重启（首次安装后需重启）\n"
                "  2. Beta APIs 实验功能未启用\n"
                "  3. 行为包未正确加载\n"
                "已有 simulated 假人保持类型不变，行为包恢复连接后自动重建。§r"
            )
        # 失联后重置确认状态，恢复连接时重新生成
        for fp in list(self._bots.values()):
            if fp.type == "simulated":
                fp.sim_spawn_confirmed = False

    @event_handler
    def on_script_message(self, event: ScriptMessageEvent) -> None:
        """处理行为包通过 scriptevent 发回的消息。

        通过鉴权令牌验证消息来源，防止恶意玩家伪造 scriptevent。
        """
        msg_id = event.message_id
        if not msg_id.startswith("bot:"):
            return

        try:
            data = json.loads(event.message) if event.message else {}
        except Exception:
            data = {}

        # 鉴权：验证令牌（pong 消息用于初始握手，需要特殊处理）
        token = data.get("t", "")
        if msg_id != "bot:pong":
            # 非 pong 消息必须携带正确令牌
            if not self._bridge_token or token != self._bridge_token:
                self.logger.debug(
                    f"忽略未经认证的 scriptevent: {msg_id} (token mismatch)"
                )
                return
        else:
            # pong 消息：首次握手时验证令牌
            if not self._bridge_token or token != self._bridge_token:
                self.logger.debug(
                    f"忽略未经认证的 bot:pong (token mismatch)"
                )
                return

        if msg_id == "bot:pong":
            self._pong_received = True
            was_active = self._behavior_pack_active
            self._behavior_pack_active = True
            if not was_active:
                # 仅在状态从失联→连接时提示，避免周期性 ping 刷日志
                self.logger.info("§a行为包已连接，SimulatedPlayer 功能可用。§r")

            # 对照行为包管理的玩家列表，重置已丢失的确认状态
            managed = {str(n).lower() for n in data.get("names", []) if isinstance(n, str)}
            for fp in list(self._bots.values()):
                if fp.type == "simulated" and fp.name.lower() not in managed:
                    fp.sim_spawn_confirmed = False

            # 处理待生成的模拟玩家
            with self._lock:
                pending = list(self._pending_sim_spawns)
                self._pending_sim_spawns.clear()
            for fp in pending:
                self._spawn_simulated_player(fp)

            # B4：补发失联期间积压的移除命令
            if self._pending_sim_removes:
                for name in list(self._pending_sim_removes):
                    self._send_scriptevent("bot:remove", {"n": name})
                    self._pending_sim_removes.discard(name)

        elif msg_id == "bot:spawned":
            # 生成确认：标记 confirmed，避免自愈任务反复重发
            name = str(data.get("n", ""))
            ok = bool(data.get("ok", False))
            fp = self._get_by_name(name)
            if fp is not None:
                fp.sim_spawn_confirmed = ok
            if ok:
                if bool(data.get("existed", False)):
                    self.logger.debug(f"模拟玩家 §b{name}§r 已存在，幂等跳过。")
                else:
                    self.logger.info(f"模拟玩家 §b{name}§r 生成成功。")
            else:
                self.logger.warning(f"模拟玩家 §b{name}§r 生成失败。")

        elif msg_id == "bot:positions":
            # 行为包定期上报 simulated 玩家坐标 → 更新内存并置脏标记
            entries = data.get("p", [])
            if not isinstance(entries, list):
                return
            for item in entries:
                if not isinstance(item, dict):
                    continue
                fp = self._get_by_name(str(item.get("n", "")))
                if fp is None or fp.type != "simulated":
                    continue
                try:
                    # 实际位置始终记录（供行为系统计算）
                    fp.sim_actual_x = round(float(item.get("x", fp.sim_actual_x)), 2)
                    fp.sim_actual_y = round(float(item.get("y", fp.sim_actual_y)), 2)
                    fp.sim_actual_z = round(float(item.get("z", fp.sim_actual_z)), 2)
                    fp.sim_has_position = True
                    # idle 假人不覆盖存储锚点（B3：位置守护需要锚点），
                    # 其余模式用实际位置作为持久化位置
                    if fp.behavior.movement != "idle":
                        fp.location_x = fp.sim_actual_x
                        fp.location_y = fp.sim_actual_y
                        fp.location_z = fp.sim_actual_z
                    fp.dimension = str(item.get("d", fp.dimension)) or fp.dimension
                    self._db_dirty = True
                except (TypeError, ValueError):
                    continue

        elif msg_id == "bot:removed":
            name = str(data.get("n", ""))
            self.logger.info(f"模拟玩家 §b{name}§r 已移除。")

        elif msg_id == "bot:error":
            name = data.get("n", "")
            err = data.get("e", "未知错误")
            self.logger.warning(f"行为包错误 [{name}]: {err}")

        elif msg_id == "bot:list_result":
            names = data.get("names", [])
            self.logger.info(f"行为包管理的模拟玩家: {', '.join(names)}")

    def _spawn_simulated_player(self, fp: FakePlayer) -> None:
        """通过行为包生成 SimulatedPlayer。"""
        self._send_scriptevent("bot:spawn", {
            "n": fp.name,
            "x": fp.location_x,
            "y": fp.location_y,
            "z": fp.location_z,
            "d": fp.dimension,
        })

    def _remove_simulated_player(self, fp: FakePlayer) -> None:
        """通过行为包移除 SimulatedPlayer。

        行为包失联时记入待移除名单，恢复连接后（pong 分支）补发，
        避免行为包侧残留"幽灵玩家"。
        """
        if self._behavior_pack_active:
            self._send_scriptevent("bot:remove", {"n": fp.name})
            self._pending_sim_removes.discard(fp.name)
        else:
            self._pending_sim_removes.add(fp.name)
        fp.actor = None
        fp.entity_id = ""
        fp.sim_has_position = False
