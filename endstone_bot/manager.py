"""假人管理器：CRUD、持久化、自愈、tickingarea、命令处理。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from endstone.command import CommandSender
from endstone.level import Location

from endstone_bot.models import (
    SKINS,
    FakePlayer,
    build_fake_player_name_tag,
    format_date_time_beijing,
    generate_id,
    get_skin_name,
    normalize_skin_id,
    validate_name,
)


class FakeBotManager:
    """管理假人的创建、删除、持久化、自愈和 tick 行为。"""

    def __init__(self, plugin: Any, data_folder: Path, bridge: Any, logger: Any) -> None:
        self._plugin = plugin
        self._bridge = bridge
        self._logger = logger
        self._db_path = data_folder / "bots.json"
        self.bots: dict[str, FakePlayer] = {}
        self.name_index: dict[str, str] = {}
        self.pending_spawns: list[FakePlayer] = []
        self.pending_removes: set[str] = set()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self) -> None:
        try:
            tmp = self._db_path.with_suffix(".json.tmp")
            data = {"version": 4, "bots": [fp.to_record() for fp in self.bots.values()]}
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._db_path)
            self._plugin._db_dirty = False
        except Exception as exc:
            self._logger.warning(f"保存 bots.json 失败: {exc}")

    def restore(self) -> None:
        if not self._db_path.exists():
            return
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._logger.warning(f"读取 bots.json 失败: {exc}")
            return
        restored = 0
        for item in data.get("bots", []):
            try:
                fp = FakePlayer.from_record(item)
            except Exception as exc:
                self._logger.warning(f"解析假人记录失败，跳过: {exc}")
                continue
            key = fp.name.lower()
            if key in self.name_index:
                self._logger.warning(f"跳过重复的假人记录: {fp.name}")
                continue
            self.bots[fp.id] = fp
            self.name_index[key] = fp.id
            restored += 1
        if restored:
            self._logger.info(f"从磁盘恢复了 {restored} 个假人。")

    def clear_runtime(self) -> None:
        for fp in list(self.bots.values()):
            if fp.type == "simulated":
                fp.actor = None
                continue
            self.remove_managed(fp)
        self.bots.clear()
        self.name_index.clear()

    # ------------------------------------------------------------------
    # 查找 / 权限
    # ------------------------------------------------------------------

    def get_by_name(self, name: str) -> FakePlayer | None:
        fp_id = self.name_index.get(name.strip().lower())
        if not fp_id:
            return None
        return self.bots.get(fp_id)

    def get_by_actor(self, actor: Any) -> FakePlayer | None:
        try:
            tags = list(actor.scoreboard_tags or [])
        except Exception:
            return None
        for tag in tags:
            if tag.startswith("yuehua_fake_player_id:"):
                fp_id = tag[len("yuehua_fake_player_id:"):]
                return self.bots.get(fp_id)
        return None

    def is_fake_player_actor(self, actor: Any) -> bool:
        try:
            tags = actor.scoreboard_tags or []
            return "yuehua_fake_player" in tags
        except Exception:
            return False

    @staticmethod
    def is_actor_valid(actor: Any) -> bool:
        try:
            return bool(actor and actor.is_valid and not actor.is_dead)
        except Exception:
            return False

    def can_manage(self, sender: Any, fp: FakePlayer) -> bool:
        if fp.owner_uuid:
            sender_uuid = str(getattr(sender, "unique_id", "") or "")
            if sender_uuid and sender_uuid == fp.owner_uuid:
                return True
        elif fp.owner_name == str(getattr(sender, "name", "") or ""):
            return True
        return self._plugin._is_admin(sender)

    # ------------------------------------------------------------------
    # 生成 / 移除
    # ------------------------------------------------------------------

    def spawn_simulated(self, fp: FakePlayer) -> None:
        self._bridge.send_bridge("spawn", {
            "n": fp.name, "id": fp.id,
            "x": fp.location_x, "y": fp.location_y, "z": fp.location_z,
            "d": fp.dimension,
        })

    def remove_simulated(self, fp: FakePlayer) -> None:
        if not self._bridge.send_bridge("remove", {"n": fp.name}):
            self.pending_removes.add(fp.name)
        fp.actor = None
        fp.entity_id = ""
        fp.sim_has_position = False

    def remove_managed(self, fp: FakePlayer) -> None:
        self.remove_simulated(fp)

    def ensure_all_spawned(self) -> None:
        for fp in list(self.bots.values()):
            if fp.type == "simulated":
                if not self._bridge.active:
                    if fp not in self.pending_spawns:
                        self.pending_spawns.append(fp)
                    continue
                if fp.sim_spawn_confirmed:
                    continue
                self._logger.info(f"模拟假人 §b{fp.name}§r 未确认生成，重发...")
                self.spawn_simulated(fp)
                continue
            if self.is_actor_valid(fp.actor):
                continue
            self._logger.info(f"假人 {fp.name} 实体失效，尝试自愈...")
            fp.actor = None
            fp.entity_id = ""
            fp.last_area_key = None
            actor = self._spawn_npc(fp)
            if actor is not None:
                fp.actor = actor
                fp.entity_id = str(getattr(actor, "unique_id", "") or "")
                self._prepare_npc(fp)
                self._update_tickingarea(fp)
                self.save()
                self._logger.info(f"假人 {fp.name} 自愈成功。")

    def _spawn_npc(self, fp: FakePlayer) -> Any:
        dimension = self._plugin._get_dimension(fp.dimension)
        if dimension is None:
            return None
        try:
            loc = Location(dimension, fp.location_x, fp.location_y, fp.location_z)
            return dimension.spawn_actor(loc, "minecraft:npc")
        except Exception:
            return None

    def _prepare_npc(self, fp: FakePlayer) -> None:
        actor = fp.actor
        if actor is None:
            return
        try:
            actor.name_tag = build_fake_player_name_tag(fp)
            actor.is_name_tag_visible = True
            actor.add_scoreboard_tag("yuehua_fake_player")
            actor.add_scoreboard_tag(f"yuehua_fake_player_id:{fp.id}")
            actor.add_scoreboard_tag(f"yuehua_fake_player_owner:{fp.owner_name}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # tickingarea
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_coords(x: float, z: float) -> tuple[int, int]:
        return math.floor(x) >> 4, math.floor(z) >> 4

    @staticmethod
    def _tickingarea_bounds(chunk_x: int, chunk_z: int, radius: int) -> tuple[int, int, int, int]:
        start_x = (chunk_x - radius) * 16
        start_z = (chunk_z - radius) * 16
        end_x = (chunk_x + radius + 1) * 16 - 1
        end_z = (chunk_z + radius + 1) * 16 - 1
        return start_x, start_z, end_x, end_z

    def _update_tickingarea(self, fp: FakePlayer) -> bool:
        if not self.is_actor_valid(fp.actor):
            return False
        try:
            loc = fp.actor.location
        except Exception:
            return False
        chunk_x, chunk_z = self._chunk_coords(loc.x, loc.z)
        dim_id = self._plugin._dimension_id(loc.dimension)
        radius = self._plugin.DEFAULT_CHUNK_RADIUS
        area_key = (dim_id, chunk_x, chunk_z, radius)
        if fp.last_area_key == area_key:
            return True
        self.remove_tickingarea(fp)
        self.create_tickingarea(fp, radius, chunk_x, chunk_z, dim_id)
        return True

    def create_tickingarea(self, fp: FakePlayer, radius: int, chunk_x: int | None = None, chunk_z: int | None = None, dim_id: str | None = None) -> bool:
        if chunk_x is None or chunk_z is None or dim_id is None:
            if not self.is_actor_valid(fp.actor):
                return False
            try:
                loc = fp.actor.location
            except Exception:
                return False
            chunk_x, chunk_z = self._chunk_coords(loc.x, loc.z)
            dim_id = self._plugin._dimension_id(loc.dimension)
        start_x, start_z, end_x, end_z = self._tickingarea_bounds(chunk_x, chunk_z, radius)
        command = (
            f"tickingarea add {start_x} {self._plugin.MIN_Y} {start_z} "
            f"{end_x} {self._plugin.MAX_Y} {end_z} {fp.tickingarea_name} true"
        )
        ok = self._plugin._dispatch(f"execute in {dim_id} run {command}")
        if ok:
            fp.last_area_key = (dim_id, chunk_x, chunk_z, radius)
        return ok

    def remove_tickingarea(self, fp: FakePlayer) -> None:
        if not fp.tickingarea_name or fp.last_area_key is None:
            return
        dim_id = fp.last_area_key[0] if fp.last_area_key else "overworld"
        self._plugin._dispatch(f"execute in {dim_id} run tickingarea remove {fp.tickingarea_name}")
        fp.last_area_key = None

    def remove_all_residual_tickingareas(self) -> int:
        removed = 0
        for dim_id in ("overworld", "nether", "the_end"):
            names = self._list_bot_tickingareas(dim_id)
            for area_name in names:
                if self._plugin._dispatch(f"execute in {dim_id} run tickingarea remove {area_name}"):
                    removed += 1
        return removed

    def _list_bot_tickingareas(self, dimension_id: str) -> set[str]:
        from endstone.command import CommandSenderWrapper

        lines: list[str] = []

        def capture(message: Any) -> None:
            lines.append(str(message))

        try:
            wrapper = CommandSenderWrapper(
                self._plugin.server.command_sender, on_message=capture, on_error=capture
            )
            self._plugin.server.dispatch_command(wrapper, f"execute in {dimension_id} run tickingarea list")
        except Exception:
            pass

        names: set[str] = set()
        for line in lines:
            text = str(line).strip()
            if text.startswith("-"):
                text = text[1:].strip()
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

    # ------------------------------------------------------------------
    # 命令处理
    # ------------------------------------------------------------------

    def cmd_spawn(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot spawn <名字>")
            return True
        if hasattr(sender, "scoreboard_tags"):
            try:
                tags = set(sender.scoreboard_tags or [])
            except Exception:
                tags = set()
            if "pracitse" in tags:
                return self._practice_spawn(sender, args[0])
        raw_name = args[0]
        online_names = {p.name for p in self._plugin.server.online_players}
        error = validate_name(raw_name, set(self.name_index.keys()), online_names)
        if error:
            sender.send_error_message(error)
            return True
        name = raw_name.strip()
        location = getattr(sender, "location", None)
        if location is None:
            sender.send_error_message("无法确定生成位置，请由玩家执行该命令。")
            return True
        owner = str(getattr(sender, "name", "") or "console")
        owner_uuid = str(getattr(sender, "unique_id", "") or "")
        fp = FakePlayer(
            id=generate_id(), name=name, owner_name=owner, owner_uuid=owner_uuid,
            location_x=round(float(location.x), 2),
            location_y=round(float(location.y), 2),
            location_z=round(float(location.z), 2),
            dimension=self._plugin._dimension_id(location.dimension),
            created=format_date_time_beijing(), type="simulated",
            tickingarea_name=f"bot_{name}",
        )
        self.bots[fp.id] = fp
        self.name_index[fp.name.lower()] = fp.id
        self.spawn_simulated(fp)
        self.save()
        sender.send_message(f"已创建模拟假人 §b{name}§r（所有者：{owner}），正在通过行为包生成...")
        return True

    def _practice_spawn(self, player: Any, name_arg: str) -> bool:
        owner_uuid = str(getattr(player, "unique_id", "") or "")
        online_names = {p.name for p in self._plugin.server.online_players}
        error = validate_name(name_arg, set(self.name_index.keys()), online_names)
        if error:
            player.send_error_message(error)
            return True
        name = name_arg.strip()
        for old in list(self.bots.values()):
            if old.practice_managed and old.owner_uuid == owner_uuid:
                self.remove_managed(old)
                self.bots.pop(old.id, None)
                self.name_index.pop(old.name.lower(), None)
        profile = self._plugin.get_practice_profile(player)
        loc = player.location
        fp = FakePlayer(
            id=generate_id(), name=name, owner_name=str(player.name), owner_uuid=owner_uuid,
            location_x=round(float(loc.x), 2), location_y=round(float(loc.y), 2),
            location_z=round(float(loc.z), 2),
            dimension=self._plugin._dimension_id(loc.dimension),
            created=format_date_time_beijing(), type="simulated",
            tickingarea_name=f"bot_{name}", practice_managed=True,
            practice_follow=bool(profile.get("follow", False)),
            practice_random_move=bool(profile.get("randomMove", False)),
            practice_slow_falling=bool(profile.get("slowFalling", False)),
            practice_fire_resistance=bool(profile.get("fireResistance", False)),
            practice_infinite_totem=bool(profile.get("infiniteTotem", False)),
            practice_armor=str(profile.get("armor", "none")),
        )
        fp.behavior.movement = "follow" if fp.practice_follow else "idle"
        fp.behavior.target_player = str(player.name) if fp.practice_follow else ""
        self.bots[fp.id] = fp
        self.name_index[fp.name.lower()] = fp.id
        self.save()
        self.spawn_simulated(fp)
        self._plugin.send_practice_config(fp)
        player.send_message("§i下蹲右键以编辑")
        return True

    def cmd_remove(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot remove <名字>")
            return True
        name = args[0]
        fp = self.get_by_name(name)
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self.can_manage(sender, fp):
            sender.send_error_message("无权删除该假人。")
            return True
        self.remove_managed(fp)
        self.bots.pop(fp.id, None)
        self.name_index.pop(fp.name.lower(), None)
        self.save()
        sender.send_message(f"已删除假人 §b{name}§r。")
        return True

    def cmd_clearall(self, sender: CommandSender) -> bool:
        if not self._plugin._is_admin(sender):
            sender.send_error_message("无权删除全服假人。")
            return True
        deleted = 0
        for fp in list(self.bots.values()):
            self.remove_managed(fp)
            deleted += 1
        self.bots.clear()
        self.name_index.clear()
        self._plugin._db_dirty = False
        residual = self.remove_all_residual_tickingareas()
        try:
            self._db_path.unlink(missing_ok=True)
        except Exception:
            pass
        sender.send_message(f"已清理 {deleted} 个假人，移除 {residual} 个残留常加载区域。")
        return True

    def cmd_list(self, sender: CommandSender) -> bool:
        if not self.bots:
            sender.send_message("当前没有假人。")
            return True
        parts = []
        for fp in self.bots.values():
            alive = "在线" if self.is_actor_valid(fp.actor) else "离线"
            skin_name = get_skin_name(fp.skin_id)
            parts.append(f"§b{fp.name}§r({fp.owner_name}, {alive}, {skin_name}#{fp.skin_id})")
        sender.send_message("假人列表：" + ", ".join(parts))
        return True

    def cmd_info(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot info <名字>")
            return True
        fp = self.get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        alive = "在线" if self.is_actor_valid(fp.actor) else "离线(等待自愈)"
        sender.send_message(
            f"§b===== 假人 §a{fp.name} §b=====\n"
            f"  所有者：{fp.owner_name}\n"
            f"  状态：{alive}\n"
            f"  类型：{fp.type}\n"
            f"  位置：({fp.location_x:.1f}, {fp.location_y:.1f}, {fp.location_z:.1f}) {fp.dimension}\n"
            f"  皮肤：{get_skin_name(fp.skin_id)}(#{fp.skin_id})\n"
            f"  行为：{fp.behavior.movement}"
        )
        return True

    def cmd_skin(self, sender: CommandSender, args: list[str]) -> bool:
        if len(args) < 2:
            sender.send_error_message("用法：/bot skin <名字> <0-15>")
            return True
        fp = self.get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self.can_manage(sender, fp):
            sender.send_error_message("无权修改该假人。")
            return True
        try:
            skin_id = int(args[1])
        except ValueError:
            sender.send_error_message("皮肤编号必须是 0-15 的整数。")
            return True
        skin_id = normalize_skin_id(skin_id)
        fp.skin_id = skin_id
        self.save()
        sender.send_message(f"已把 §b{fp.name}§r 的皮肤设为 {get_skin_name(skin_id)}(#{skin_id})。")
        return True

    def cmd_skins(self, sender: CommandSender) -> bool:
        lines = ["§b===== 可用皮肤列表 =====§r"]
        for skin in SKINS:
            lines.append(f"  #{skin.id:2d}  {skin.name}")
        sender.send_message("\n".join(lines))
        return True

    def cmd_behavior(self, sender: CommandSender, args: list[str]) -> bool:
        if len(args) < 2:
            sender.send_error_message("用法：/bot behavior <名字> <idle|station|follow> [目标玩家]")
            return True
        fp = self.get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self.can_manage(sender, fp):
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
            if self._plugin._find_online_player(target_name) is None:
                sender.send_error_message("跟随目标当前不在线或名称不正确。")
                return True
            behavior.target_player = target_name
            sender.send_message(f"假人 §b{fp.name}§r 现在跟随 §e{target_name}§r。")
        elif movement == "station":
            loc = getattr(sender, "location", None)
            if loc is not None:
                behavior.station_x = round(float(loc.x), 2)
                behavior.station_y = round(float(loc.y), 2)
                behavior.station_z = round(float(loc.z), 2)
            sender.send_message(f"假人 §b{fp.name}§r 已锁定到你当前位置。")
        else:
            behavior.target_player = ""
            sender.send_message(f"假人 §b{fp.name}§r 行为设为原地待命。")
        self.save()
        return True

    def cmd_movehere(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            sender.send_error_message("用法：/bot movehere <名字>")
            return True
        fp = self.get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self.can_manage(sender, fp):
            sender.send_error_message("无权移动该假人。")
            return True
        loc = getattr(sender, "location", None)
        if loc is None:
            sender.send_error_message("无法确定你的位置。")
            return True
        self.remove_managed(fp)
        fp.location_x = round(float(loc.x), 2)
        fp.location_y = round(float(loc.y), 2)
        fp.location_z = round(float(loc.z), 2)
        fp.dimension = self._plugin._dimension_id(loc.dimension)
        fp.entity_id = ""
        fp.actor = None
        fp.last_area_key = None
        self.save()
        self.spawn_simulated(fp)
        sender.send_message(f"已将模拟假人 §b{fp.name}§r 移动到你当前位置。")
        return True

    def cmd_radius(self, sender: CommandSender, args: list[str]) -> bool:
        if len(args) < 2:
            sender.send_error_message("用法：/bot radius <名字> <0-4>")
            return True
        fp = self.get_by_name(args[0])
        if fp is None:
            sender.send_error_message("假人不存在。")
            return True
        if not self.can_manage(sender, fp):
            sender.send_error_message("无权修改该假人。")
            return True
        try:
            radius = int(args[1])
        except ValueError:
            sender.send_error_message("半径必须是整数。")
            return True
        radius = max(0, min(4, radius))
        if self.is_actor_valid(fp.actor):
            try:
                loc = fp.actor.location
                fp.location_x = round(float(loc.x), 2)
                fp.location_y = round(float(loc.y), 2)
                fp.location_z = round(float(loc.z), 2)
            except Exception:
                pass
        self.remove_tickingarea(fp)
        if radius > 0:
            self.create_tickingarea(fp, radius)
        self.save()
        sender.send_message(f"已把 §b{fp.name}§r 的常加载半径设为 {radius} 区块。")
        return True