# pracitse 私人定制分支

分支：`feat/pracitise-private-bot`。该分支不合并到 `main`。

## 召唤

玩家必须拥有 scoreboard tag：`pracitse`。

```mcfunction
/tag 玩家名 add pracitse
```

玩家输入 `/bot`：

- 无 `pracitse`：提示 `§c该指令在此区域已被禁用`
- 有标签：在当前位置生成专属 SimulatedPlayer Bot
- 提示：`§i下蹲右键以编辑`
- 同一玩家再次召唤时替换旧 Bot，并继承上次配置

原管理 GUI 仍可通过 `/bot gui` 打开。

## 下蹲右键设置

标题：`§a§lbot设置界面`

六项：跟随、随机移动、缓降、抗火、无限图腾、盔甲。

- 跟随：保持在召唤者附近
- 随机移动：自身/玩家中心约 5×5 范围随机移动
- 缓降：slow_falling II 持续刷新
- 抗火：fire_resistance 255 持续刷新
- 无限图腾：主副手每 tick 检查并立即补图腾
- 盔甲：钻石套或下界合金套持续维护

配置保存在 `practice_profiles.json`，按玩家 UUID 继承。

## 平台限制

Bedrock Script API 的 `SimulatedPlayer` 没有运行时复制玩家 Skin 的接口，因此“皮肤完全复制召唤者”受当前 Endstone/GameTest API 限制。其余功能通过行为包实现。
