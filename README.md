# Endstone Bot

Minecraft 基岩版（BDS）假人管理插件，基于 [Endstone](https://github.com/EndstoneMC/endstone) 插件框架。

假人管理逻辑严格参照开源项目 [mcbes-manage-script](https://github.com/YueHua46/mcbes-manage-script) 实现，并提供原版所没有的 **Server UI GUI**、**行为包自动部署** 与 **NBT 实验功能自动开启** 能力。

---

## 功能特性

- **两种假人类型**（同 mcbes-manage-script）
  - `entity` — NPC 实体 + `tickingarea` 常加载区块，区块保持 ticking，原版刷怪系统自然工作（**不含任何自制刷怪代码**）
  - `simulated` — 通过内置行为包调用 `@minecraft/server-gametest` 的 `SimulatedPlayer`（原版同款模拟玩家），行为包未就绪时**新建**假人自动降级为 `entity`（已有 simulated 假人保持类型，行为包恢复连接后自动重建）
- **GUI 界面** — `/bot gui` 打开表单菜单：创建 / 列表 / 皮肤 / 行为 / 半径 / 移动 / 删除全部可视化操作；右键假人直接打开管理菜单
- **自动部署** — whl 放入 `plugins/` 后首次启动自动完成：
  - 释放内置行为包到 `worlds/<world>/behavior_packs/`
  - 注册到 `world_behavior_packs.json`（异常时不覆盖已有内容）
  - 直接编辑 `level.dat` 开启 Beta APIs 实验功能（保留世界数据，自动备份）
- **同款管理逻辑** — 所有者追踪、自愈恢复、位置守护、伤害拦截、击退清除、右键交互、16 款皮肤变体、行为系统（待机 / 原地驻守 / 跟随）
- **安全加固** — scriptevent 鉴权令牌、UUID 所有权判定、名称字符集白名单、NBT 读取深度/长度限制、原子写入

## 环境要求

| 依赖 | 版本 |
|------|------|
| Bedrock Dedicated Server | 1.21+ |
| Endstone | 0.11+ |
| Python | 3.10+ |

## 安装

### 方式一：从 whl 安装（推荐）

```bash
pip install endstone_bot-3.0.0-py3-none-any.whl
```

或将 whl 文件直接放入 BDS 的 `plugins/` 目录。

### 方式二：从源码构建

```bash
git clone https://github.com/BCZZB/endstone-bot.git
cd endstone-bot
pip install build
python -m build
pip install dist/*.whl
```

### 首次启动

启动服务器后插件会自动：

1. 释放行为包并注册到世界
2. 在 `level.dat` 中开启 Beta APIs 实验功能（自动备份原文件）
3. **重启服务器**使实验功能与行为包生效

日志出现 `§a行为包已连接` 即表示 simulated 类型就绪。

## 快速上手

```text
/bot gui                       # 打开 GUI 主菜单（玩家）
/bot spawn Steve entity        # 在脚下生成 NPC 型假人
/bot spawn Alex simulated      # 生成模拟玩家型假人（行为包）
/bot list                      # 查看所有假人
/bot skin Steve 3              # 切换皮肤变体（0-15）
/bot behavior Steve follow     # 设置跟随行为
/bot radius Steve 4            # 常加载区域半径（0-4 区块）
/bot movehere Steve            # 传送到自己身边
/bot remove Steve              # 删除假人
/bot clearall                  # 删除全部假人
```

| 命令 | 说明 |
|------|------|
| `/bot` | 玩家执行直接打开 GUI；控制台显示用法 |
| `/bot spawn <name> [type] [skin]` | 生成假人，type 为 `entity` / `simulated`，skin 为 0-15 |
| `/bot remove <name>` | 删除指定假人 |
| `/bot list` `/bots` | 列出所有假人及状态 |
| `/bot info <name>` | 查看假人详细信息 |
| `/bot skin <name> <0-15>` / `/bot skins` | 切换 / 查看皮肤变体 |
| `/bot behavior <name> <idle\|station\|follow> [target]` | 设置行为 |
| `/bot radius <name> <0-4>` | 调整常加载区域半径 |
| `/bot movehere <name>` | 将假人移动到执行者位置 |
| `/bot clearall` | 删除全部假人（带确认） |
| `/bot ping` | 检测行为包连接状态 |
| `/bot credits` | 查看参考项目致谢 |

默认权限为 OP，可在权限配置中调整 `endstone_bot.command`。

## 行为系统

| 行为 | 说明 |
|------|------|
| `idle` | 待机，位置守护生效（被推离后自动回到原位） |
| `station` | 原地驻守，同 idle 但不参与跟随判定 |
| `follow <target>` | 距离大于阈值传送至目标，否则保持跟随偏移 |

## 架构说明

```text
endstone_bot/
├── __init__.py              # 入口、致谢信息
├── bot_plugin.py            # 主插件：命令、事件、自愈、守护、行为、行为包桥接
├── models.py                # FakePlayer / BotBehavior 数据模型与校验
├── gui.py                   # Server UI 表单（ActionForm / ModalForm）
├── nbt.py                   # Bedrock 小端 NBT 读写（带 DoS 防护）
├── level_dat.py             # level.dat 实验功能编辑器
└── behavior_pack/           # 内置桥接行为包（随 whl 分发）
    ├── manifest.json        # 依赖 @minecraft/server-gametest (beta)
    ├── scripts/main.js      # SimulatedPlayer 生成 / 移除 / 传送 / 坐标上报
    └── structures/endstone_bot/empty.mcstructure
```

### Endstone ↔ 行为包桥接

Endstone 无法直接调用 `@minecraft/server-gametest`（独立脚本运行时），插件通过 `scriptevent` 命令桥接：

```text
Endstone (Python)                    行为包 (JavaScript)
     │                                     │
     │── scriptevent bot:ping  {t:token} ──▶│
     │◀──── scriptevent bot:pong {names} ───│
     │── scriptevent bot:spawn {n,x,y,z} ──▶│ spawnSimulatedPlayer()
     │◀──── scriptevent bot:spawned {ok} ───│
     │◀──── scriptevent bot:positions {p} ──│ 每 100 tick 坐标上报
     │── scriptevent bot:remove {n} ──────▶│ disconnect()
```

双向消息均携带随机鉴权令牌（每次启动重新生成），防止玩家伪造 scriptevent 干扰状态机。

### 刷怪说明

本插件**不包含任何自制刷怪逻辑**。`entity` 型假人通过 `tickingarea` 命令保持所在区块 ticking，Minecraft 原版刷怪系统在这些区块中按原版规则自然工作——与原版 `tickingarea` 行为完全一致，遵守 mob cap 与亮度判定。

## 数据与持久化

- 假人数据存储于 `plugins/endstone_bot/bots.json`（原子写入，脏标记批量落盘）
- `level.dat` 修改前自动备份为 `level.dat.bak`（仅 `experiments` 字段，世界种子/出生点等不受影响）
- 服务器重启后自动恢复全部假人（含 simulated 类型，依据行为包上报的持久化坐标）

## 文档

- [用户指南（HTML）](docs/guide/endstone-bot-guide.html) — GUI 与命令完整说明
- [更新日志](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)

## 致谢

本项目假人管理逻辑严格参照以下开源项目实现：

- **[mcbes-manage-script](https://github.com/YueHua46/mcbes-manage-script)** by YueHua46 — 两种假人类型、所有者追踪、自愈恢复、位置守护、伤害拦截、右键交互、皮肤变体、行为系统（PolyForm Noncommercial License 1.0.0）
- **[Endstone](https://github.com/EndstoneMC/endstone)** — 插件框架

游戏内执行 `/bot credits` 可查看致谢信息。

## 许可证

[PolyForm Noncommercial License 1.0.0](LICENSE)

因参考项目 mcbes-manage-script 采用该许可，本衍生项目依法采用相同许可：允许任何**非商业目的**的使用、修改与分发。

文档字体（JetBrains Mono / Work Sans / Pixelify Sans）遵循各自的 [SIL OFL 1.1](docs/guide/_shared/fonts/) 许可。
