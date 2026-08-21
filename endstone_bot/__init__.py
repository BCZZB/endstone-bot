"""endstone_bot 假人插件。

严格按照 mcbes-manage-script (https://github.com/YueHua46/mcbes-manage-script)
的假人管理逻辑重新实现，适配 Endstone Python API。

参考项目：
  1. mcbes-manage-script
     - 来源：https://github.com/YueHua46/mcbes-manage-script
     - 许可证：PolyForm Noncommercial License 1.0.0
     - 借鉴：全部假人管理逻辑（两种假人类型、所有者追踪、自愈恢复、
       位置守护、伤害拦截、右键交互、皮肤变体、名称校验、头顶名称格式、
       持久化模型、行为系统）
  2. endstone_bot (原版 0.2.5)
     - 借鉴：NPC 实体生成、tickingarea 常加载区域管理

核心设计（同 mcbes-manage-script）：
  - 两种假人类型：entity（旧版实体）和 simulated（模拟玩家）
  - entity 类型：NPC + tickingarea，区块保持加载，原版刷怪自然工作
  - simulated 类型：Endstone 不支持 spawnSimulatedPlayer，自动降级为 entity
  - 伤害拦截：entity 完全免伤（ActorDamageEvent cancel）
  - 击退清除：ActorKnockbackEvent cancel
  - 右键交互：PlayerInteractActorEvent 拦截后显示假人信息
  - 所有者追踪：scoreboard tag 标记
  - 皮肤变体：0-15，scoreboard tag 标记
  - 自愈恢复：定期检测失效实体并重新生成
  - 位置守护：idle 假人被推离原位自动传送回
  - 持久化：JSON 文件存储
"""

from __future__ import annotations

from endstone_bot.bot_plugin import BotPlugin

__all__ = ["BotPlugin"]
__version__ = "3.2.2"

REFERENCES = {
    "mcbes_manage_script": {
        "url": "https://github.com/YueHua46/mcbes-manage-script",
        "license": "PolyForm Noncommercial License 1.0.0",
        "borrowed": [
            "两种假人类型设计 (entity / simulated)",
            "假人所有者追踪机制 (ownerName + scoreboard tag)",
            "自愈恢复 (ensureAllSpawned / refresh)",
            "位置守护 (guardPosition)",
            "伤害拦截 (entityHurt → ActorDamageEvent cancel)",
            "击退清除 (clearVelocity → ActorKnockbackEvent cancel)",
            "右键交互拦截 (playerInteractWithEntity → PlayerInteractActorEvent)",
            "皮肤变体 (0-15, scoreboard tag)",
            "持久化数据模型设计 (IFakePlayer 字段)",
            "名称校验逻辑 (validateName)",
            "头顶名称格式 (buildFakePlayerNameTag)",
            "行为系统 (idle / station / follow)",
            "移动到操作者 (moveToOperator)",
            "清除全部 (deleteAll)",
        ],
    },
}
