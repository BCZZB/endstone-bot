"""GUI 表单模块。

参考 mcbes-manage-script 的 UI 表单设计（fake-player.ts），
适配 Endstone Python Form API。

表单层级：
  主菜单 (ActionForm)
    ├── 创建假人 (ModalForm: 名称+皮肤)
    ├── 假人列表 (ActionForm: 每个假人一个按钮)
    │     └── 假人管理 (ActionForm)
    │           ├── 查看信息
    │           ├── 切换皮肤 (ActionForm: 16个皮肤)
    │           ├── 设置行为 (ModalForm: 模式+目标)
    │           ├── 调整半径 (ModalForm: 滑块)
    │           ├── 移动到当前位置
    │           └── 删除假人
    ├── 清除全部
    └── 致谢信息

使用方式：
  - 命令 /bot gui 打开主菜单
  - /bot 不带参数也打开主菜单
  - 右键假人直接打开假人管理菜单
"""

from __future__ import annotations

import json
from typing import Any, Callable

from endstone.form import (
    ActionForm,
    Dropdown,
    Label,
    ModalForm,
    Slider,
    TextInput,
    Toggle,
)

from endstone_bot.models import (
    SKINS,
    FakePlayer,
    get_skin_name,
    normalize_skin_id,
)


class BotGUI:
    """假人 GUI 管理器，封装所有表单逻辑。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    # ==================================================================
    # 主菜单（同月华 openFakePlayerManageForm）
    # ==================================================================

    def open_main_menu(self, player: Any) -> None:
        """打开主菜单。"""
        bot_count = len(self._plugin._bots)

        form = ActionForm(
            title="§l§b假人管理",
            content=f"§7当前共有 §a{bot_count} §7个假人\n§7请选择操作：",
        )

        form.add_button(
            "§a创建假人\n§7点击在此位置生成假人",
            on_click=lambda p: self.open_create_form(p),
        )

        form.add_button(
            f"§e假人列表 §7({bot_count})\n§7查看和管理已有假人",
            on_click=lambda p: self.open_bot_list(p),
        )

        if self._plugin._is_admin(player):
            form.add_button(
                "§5AI 模型配置\n§7设置 API 地址、Key 与模型",
                on_click=lambda p: self.open_ai_global_form(p),
            )
            form.add_button(
                "§c清除全部假人\n§7删除所有假人和常加载区域",
                on_click=lambda p: self.open_clearall_confirm(p),
            )

        form.add_button(
            "§9致谢信息\n§7查看参考的开源项目",
            on_click=lambda p: self.open_credits(p),
        )

        player.send_form(form)

    # ==================================================================
    # 创建假人表单（同月华 openCreateFakePlayerForm）
    # ==================================================================

    def open_create_form(self, player: Any) -> None:
        """打开创建假人表单。"""
        skin_options = [f"#{s.id:2d} {s.name}" for s in SKINS]

        form = ModalForm(
            title="§l§a创建假人",
            controls=[
                TextInput(
                    label="假人名称",
                    placeholder="字母/数字/下划线/短横线，最长24字符",
                ),
                Dropdown(
                    label="皮肤选择",
                    options=skin_options,
                    default_index=0,
                ),
            ],
            submit_button="创建",
        )

        def on_submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                p.send_message("§c表单数据解析失败。§r")
                return
            if not data or len(data) < 2:
                return

            name = str(data[0]).strip() if data[0] else ""
            skin_index = int(data[1]) if data[1] is not None else 0
            skin_id = normalize_skin_id(skin_index)

            if not name:
                p.send_message("§c假人名称不能为空。§r")
                return

            # 调用插件的 spawn 逻辑
            self._plugin._gui_spawn(p, name, skin_id)

        form.on_submit = on_submit
        player.send_form(form)

    # ==================================================================
    # 假人列表（同月华 openFakePlayerListForm）
    # ==================================================================

    def open_bot_list(self, player: Any) -> None:
        """打开假人列表。"""
        bots = list(self._plugin._bots.values())
        if not bots:
            form = ActionForm(
                title="§l§e假人列表",
                content="§7当前没有假人。",
            )
            form.add_button("§a创建假人", on_click=lambda p: self.open_create_form(p))
            form.add_button("§7返回主菜单", on_click=lambda p: self.open_main_menu(p))
            player.send_form(form)
            return

        form = ActionForm(
            title="§l§e假人列表",
            content=f"§7共 §a{len(bots)} §7个假人，点击进行管理：",
        )

        for fp in bots:
            alive = "§a在线" if self._plugin._is_actor_valid(fp.actor) else "§c离线"
            skin_name = get_skin_name(fp.skin_id)
            can_manage = self._plugin._can_manage(player, fp)
            manage_tag = "" if can_manage else " §7(无权)"
            form.add_button(
                f"§b{fp.name}\n§7{fp.owner_name} | {alive} | {skin_name}#{fp.skin_id}{manage_tag}",
                on_click=(lambda f=fp: lambda p: self.open_bot_manage(p, f))(),
            )

        form.add_button("§7返回主菜单", on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    # ==================================================================
    # 假人管理菜单（同月华 openFakePlayerManageForm）
    # ==================================================================

    def open_bot_manage(self, player: Any, fp: FakePlayer) -> None:
        """打开假人管理菜单。"""
        alive = "§a在线" if self._plugin._is_actor_valid(fp.actor) else "§c离线(等待自愈)"
        skin_name = get_skin_name(fp.skin_id)
        behavior = fp.behavior

        content = (
            f"§b名称：§f{fp.name}\n"
            f"§b所有者：§f{fp.owner_name}\n"
            f"§b状态：§f{alive}\n"
            f"§b位置：§f({fp.location_x:.1f}, {fp.location_y:.1f}, {fp.location_z:.1f})\n"
            f"§b维度：§f{fp.dimension}\n"
            f"§b皮肤：§f{skin_name}(#{fp.skin_id})\n"
            f"§b行为：§f移动={behavior.movement} 动作={behavior.action}\n"
            f"§b创建：§f{fp.created}"
        )

        form = ActionForm(
            title=f"§l§b{fp.name}",
            content=content,
        )

        can_manage = self._plugin._can_manage(player, fp)

        # 所有人都可以查看的操作
        form.add_button(
            "§d切换皮肤\n§7选择 0-15 号皮肤变体",
            on_click=lambda p: self.open_skin_select(p, fp) if can_manage else self._deny(p),
        )

        form.add_button(
            "§6设置行为\n§7idle / station / follow",
            on_click=lambda p: self.open_behavior_form(p, fp) if can_manage else self._deny(p),
        )

        form.add_button(
            "§e调整半径\n§7设置常加载区域大小 0-4",
            on_click=lambda p: self.open_radius_form(p, fp) if can_manage else self._deny(p),
        )

        form.add_button(
            "§5AI 设置\n§7开关 AI 与管理授权成员",
            on_click=lambda p: self.open_ai_bot_form(p, fp) if can_manage else self._deny(p),
        )

        form.add_button(
            "§b移动到当前位置\n§7将假人传送到你身边",
            on_click=lambda p: self._do_movehere(p, fp) if can_manage else self._deny(p),
        )

        form.add_button(
            "§c删除假人\n§7移除假人及常加载区域",
            on_click=lambda p: self.open_remove_confirm(p, fp) if can_manage else self._deny(p),
        )

        form.add_button("§7返回列表", on_click=lambda p: self.open_bot_list(p))
        player.send_form(form)

    # ==================================================================
    # 私人定制 Bot 设置界面
    # ==================================================================

    def open_practice_menu(self, player: Any, fp: FakePlayer) -> None:
        form = ActionForm(title="§a§lbot设置界面", content="§e§l请设置你的bot。")
        form.add_button("§6§l跟随", icon="textures/ui/empty_armor_slot_boots.png", on_click=lambda p: self.open_practice_toggle(p, fp, "follow"))
        form.add_button("§a§l随机移动", icon="textures/ui/comment.png", on_click=lambda p: self.open_practice_toggle(p, fp, "randomMove"))
        form.add_button("§l缓降", icon="textures/gui/newgui/mob_effects/levitation_effect.png", on_click=lambda p: self.open_practice_toggle(p, fp, "slowFalling"))
        form.add_button("§c§l抗火", icon="textures/gui/newgui/mob_effects/fire_resistance_effect.png", on_click=lambda p: self.open_practice_toggle(p, fp, "fireResistance"))
        form.add_button("§e§l无限图腾", icon="textures/items/totem.png", on_click=lambda p: self.open_practice_toggle(p, fp, "infiniteTotem"))
        form.add_button("§5§l盔甲", icon="textures/ui/backup_noline.png", on_click=lambda p: self.open_practice_armor(p, fp))
        player.send_form(form)

    def open_practice_toggle(self, player: Any, fp: FakePlayer, field: str) -> None:
        mapping = {
            "follow": ("§6§l跟随", fp.practice_follow),
            "randomMove": ("§e§l随机移动", fp.practice_random_move),
            "slowFalling": ("§l缓降", fp.practice_slow_falling),
            "fireResistance": ("§c§l抗火", fp.practice_fire_resistance),
            "infiniteTotem": ("§e§l无限图腾", fp.practice_infinite_totem),
        }
        title, current = mapping[field]
        form = ModalForm(
            title=title,
            controls=[Label(text="§a请编辑"), Toggle(label="§e§l是否开启？", default_value=current)],
            submit_button="保存",
        )
        def on_submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
                enabled = bool(data[-1])
            except Exception:
                p.send_message("§c设置保存失败")
                return
            if field == "follow":
                fp.practice_follow = enabled
                fp.behavior.movement = "follow" if enabled else "idle"
                fp.behavior.target_player = fp.owner_name if enabled else ""
            elif field == "randomMove": fp.practice_random_move = enabled
            elif field == "slowFalling": fp.practice_slow_falling = enabled
            elif field == "fireResistance": fp.practice_fire_resistance = enabled
            elif field == "infiniteTotem": fp.practice_infinite_totem = enabled
            self._plugin._save_practice_fp(fp)
            p.send_message("§a设置已保存")
        form.on_submit = on_submit
        player.send_form(form)

    def open_practice_armor(self, player: Any, fp: FakePlayer) -> None:
        form = ActionForm(title="§5§l盔甲", content="§a请选择")
        def choose(p: Any, armor: str) -> None:
            fp.practice_armor = armor
            self._plugin._save_practice_fp(fp)
            p.send_message("§a盔甲设置已保存")
        form.add_button("§b§l钻石套", on_click=lambda p: choose(p, "diamond"))
        form.add_button("§i§l合金套", on_click=lambda p: choose(p, "netherite"))
        player.send_form(form)

    # ==================================================================
    # 皮肤选择（同月华 openLegacyFakePlayerSkinForm）
    # ==================================================================

    def open_skin_select(self, player: Any, fp: FakePlayer) -> None:
        """打开皮肤选择菜单。"""
        form = ActionForm(
            title=f"§l§d{fp.name} - 皮肤选择",
            content=f"§7当前皮肤：§b{get_skin_name(fp.skin_id)}(#{fp.skin_id})",
        )

        for skin in SKINS:
            current = " §a←" if skin.id == fp.skin_id else ""
            form.add_button(
                f"#{skin.id:2d} {skin.name}{current}",
                on_click=(lambda s=skin: lambda p: self._do_set_skin(p, fp, s.id))(),
            )

        form.add_button("§7返回管理", on_click=lambda p: self.open_bot_manage(p, fp))
        player.send_form(form)

    # ==================================================================
    # 行为设置（同月华 openFakePlayerBehaviorForm）
    # ==================================================================

    def open_behavior_form(self, player: Any, fp: FakePlayer) -> None:
        """打开行为设置表单。"""
        behavior = fp.behavior
        movement_options = ["idle - 原地待命（位置守护）", "station - 锁定当前位置", "follow - 跟随玩家"]
        current_index = {"idle": 0, "station": 1, "follow": 2}.get(behavior.movement, 0)

        controls = [
            Dropdown(
                label="移动模式",
                options=movement_options,
                default_index=current_index,
            ),
            TextInput(
                label="跟随目标玩家名（仅 follow 模式需要）",
                placeholder="输入在线玩家名",
                default_value=behavior.target_player if behavior.movement == "follow" else "",
            ),
        ]

        form = ModalForm(
            title=f"§l§6{fp.name} - 行为设置",
            controls=controls,
            submit_button="应用",
        )

        def on_submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return
            if not data or len(data) < 2:
                return

            mode_index = int(data[0]) if data[0] is not None else 0
            target = str(data[1]).strip() if data[1] else ""

            modes = ["idle", "station", "follow"]
            mode = modes[mode_index] if 0 <= mode_index < len(modes) else "idle"

            self._plugin._gui_set_behavior(p, fp, mode, target)

        form.on_submit = on_submit
        player.send_form(form)

    # ==================================================================
    # 半径调整（同月华 radius 设置）
    # ==================================================================

    def open_radius_form(self, player: Any, fp: FakePlayer) -> None:
        """打开半径调整表单。"""
        form = ModalForm(
            title=f"§l§e{fp.name} - 常加载半径",
            controls=[
                Slider(
                    label="区块半径（0=不加载，4=最大）",
                    min=0,
                    max=4,
                    step=1,
                    default_value=float(fp.last_area_key[3] if fp.last_area_key else 4),
                ),
            ],
            submit_button="应用",
        )

        def on_submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return
            if not data:
                return
            radius = int(float(data[0])) if data[0] is not None else 4
            self._plugin._gui_set_radius(p, fp, radius)

        form.on_submit = on_submit
        player.send_form(form)

    # ==================================================================
    # AI 设置
    # ==================================================================

    def open_ai_bot_form(self, player: Any, fp: FakePlayer) -> None:
        """管理单个假人的 AI 开关和成员白名单。"""
        form = ModalForm(
            title=f"§l§5{fp.name} - AI 设置",
            controls=[
                Toggle(label="启用 AI 对话", default_value=bool(fp.ai_enabled)),
                TextInput(
                    label="授权玩家（英文逗号分隔）",
                    placeholder="PlayerA,PlayerB",
                    default_value=",".join(fp.ai_members),
                ),
                Label(text=f"唤醒方式：@{fp.name} <指令>"),
            ],
            submit_button="保存",
        )

        def on_submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                p.send_message("§c表单数据解析失败。")
                return
            enabled = bool(data[0]) if data else False
            members = []
            if len(data) > 1 and data[1]:
                for value in str(data[1]).split(","):
                    name = value.strip()
                    if name and name.lower() not in {x.lower() for x in members}:
                        members.append(name)
            fp.ai_enabled = enabled
            fp.ai_members = members
            self._plugin._save_db()
            p.send_message(f"§a{fp.name} AI 设置已保存。")

        form.on_submit = on_submit
        player.send_form(form)

    def open_ai_global_form(self, player: Any) -> None:
        """管理员配置 OpenAI 兼容 API。"""
        cfg = self._plugin._ai_config
        form = ModalForm(
            title="§l§5AI 模型配置",
            controls=[
                TextInput(label="API 地址", placeholder="https://api.openai.com/v1", default_value=cfg.get("baseUrl", "")),
                TextInput(label="API Key（留空则保留原 Key）", placeholder="sk-...", default_value=""),
                TextInput(label="模型名称", placeholder="gpt-4o-mini", default_value=cfg.get("model", "")),
            ],
            submit_button="保存",
        )

        def on_submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                p.send_message("§c表单数据解析失败。")
                return
            base_url = str(data[0] or "").strip()
            api_key = str(data[1] or "").strip() if len(data) > 1 else ""
            model = str(data[2] or "").strip() if len(data) > 2 else ""
            self._plugin._update_ai_config(
                base_url=base_url,
                api_key=api_key or None,
                model=model,
            )
            p.send_message("§aAI 模型配置已保存。可执行 /bot ai-config test 测试连接。")

        form.on_submit = on_submit
        player.send_form(form)

    # ==================================================================
    # 删除确认（同月华 delete 确认）
    # ==================================================================

    def open_remove_confirm(self, player: Any, fp: FakePlayer) -> None:
        """打开删除确认。"""
        from endstone.form import MessageForm

        form = MessageForm(
            title=f"§l§c删除 {fp.name}",
            content=(
                f"§c确定要删除假人 §b{fp.name} §c吗？\n\n"
                f"§7所有者：{fp.owner_name}\n"
                f"§7位置：({fp.location_x:.1f}, {fp.location_y:.1f}, {fp.location_z:.1f})\n"
                f"§7此操作不可撤销！"
            ),
            button1="§c确认删除",
            button2="§7取消",
        )

        def on_submit(p: Any, button: int) -> None:
            if button == 0:
                self._plugin._gui_remove(p, fp)

        form.on_submit = on_submit
        player.send_form(form)

    # ==================================================================
    # 清除全部确认
    # ==================================================================

    def open_clearall_confirm(self, player: Any) -> None:
        """打开清除全部确认。"""
        from endstone.form import MessageForm

        count = len(self._plugin._bots)
        form = MessageForm(
            title="§l§c清除全部假人",
            content=(
                f"§c确定要清除全部 §e{count} §c个假人吗？\n\n"
                f"§7这将删除所有假人实体和常加载区域。\n"
                f"§7此操作不可撤销！"
            ),
            button1="§c确认清除",
            button2="§7取消",
        )

        def on_submit(p: Any, button: int) -> None:
            if button == 0:
                self._plugin._gui_clearall(p)

        form.on_submit = on_submit
        player.send_form(form)

    # ==================================================================
    # 致谢信息
    # ==================================================================

    def open_credits(self, player: Any) -> None:
        """打开致谢信息。"""
        form = ActionForm(
            title="§l§9致谢与参考",
            content=(
                "§b===== endstone_bot 致谢 =====§r\n\n"
                "§a1. mcbes-manage-script§r\n"
                "   §9https://github.com/YueHua46/mcbes-manage-script§r\n"
                "   许可证：PolyForm Noncommercial License 1.0.0\n"
                "   借鉴：全部假人管理逻辑\n\n"
                "§a2. endstone_bot (原版 0.2.5)§r\n"
                "   借鉴：NPC 实体生成、tickingarea\n\n"
                "§7本项目在上述思路基础上重新实现，\n"
                "适配 Endstone Python API。感谢原作者。§r"
            ),
        )
        form.add_button("§7返回主菜单", on_click=lambda p: self.open_main_menu(p))
        player.send_form(form)

    # ==================================================================
    # 操作执行（委托给插件）
    # ==================================================================

    def _deny(self, player: Any) -> None:
        player.send_message("§c你没有权限管理该假人。§r")

    def _do_set_skin(self, player: Any, fp: FakePlayer, skin_id: int) -> None:
        self._plugin._gui_set_skin(player, fp, skin_id)

    def _do_movehere(self, player: Any, fp: FakePlayer) -> None:
        self._plugin._gui_movehere(player, fp)
