# 更新日志

本项目的所有显著变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [3.1.1] - 2026-08-19

### 新增

- **@ai AI 对话功能**：玩家在聊天框 `@假人名字` 即可唤醒 AI 与假人对话
  - 唤醒词：假人名字（叫什么就 @ 什么）
  - 权限控制：假人开启 AI + 玩家在白名单（或假人 owner / OP）
  - 新增 `ai_client.py`：OpenAI 兼容格式 AI 客户端（支持任意兼容 API）
  - 新增 `/bot ai <名字> on|off|add|remove|list`：AI 开关和成员管理
  - 新增 `/bot ai-config get|set|test`：全局 AI API 配置
  - AI 配置持久化到 `plugins/endstone_bot/ai_config.json`
  - `FakePlayer` 模型新增 `ai_enabled` / `ai_members` 字段
  - BeforeChatEvent 监听 `@假人名字` 唤醒词

## [3.1.0] - 2026-08-19

### 修复

- `/bot radius <名字> 0` 与 GUI 行为不一致：命令版 0 半径会创建 1×1 区块常加载区域，现改为只移除不创建
- simulated 假人生成时序：`_spawn_simulated_player` 在 `_bots` 注册之前发送，导致 `bot:spawned` 确认丢失、自愈重复重发；注册已提前，生成失败自动回滚
- simulated 假人行为系统失效：行为包侧假人无 actor 引用，idle/station/follow 全部静默无效；新增基于行为包坐标上报 + `bot:teleport` 的行为执行（位置守护 / 站桩 / 跟随）
- 删除 simulated 假人时行为包可能残留"幽灵玩家"：行为包失联期间移除命令记入待补发名单，恢复连接后自动补发
- 行为包未响应日志声称"自动降级为 entity"与实际行为不符，修正文案；README 同步更正
- `_list_bot_tickingareas` 兼容带序号前缀的输出格式（`- 0: bot_x: ...`），避免残留常加载区域漏清理
- 跟随行为跨维度时提前返回，避免跨维度 teleport 失败
- 皮肤命令权限检查提前到类型检查之前，避免非 owner 探测假人类型
- `_extract_behavior_pack` 同版本行为包跳过释放，避免覆盖玩家手动改动
- 清空全部后重置脏标记，防止定时任务回写空库
- 服务器关闭时不再向行为包发送移除命令（行为包随服务停止，SimulatedPlayer 自然消失）
- NBT list 元素类型为 END 但长度非 0 时防御性报错，避免位置指针错乱

## [3.0.0] - 2026-08-18

### 重写

- 假人管理逻辑整体重写，严格参照 [mcbes-manage-script](https://github.com/YueHua46/mcbes-manage-script) 的 fake-player 实现
- **移除旧版自制模拟刷怪系统**：不再包含任何周期性 spawn 怪物代码，`entity` 型假人通过 `tickingarea` 保持区块 ticking，原版刷怪系统自然工作

### 新增

- **Server UI GUI**（`/bot gui`）：主菜单、创建表单（16 款皮肤）、假人列表、管理菜单、行为设置、半径调节滑块、删除/清空确认；右键假人直接打开管理菜单
- **行为包桥接**：内置 `@minecraft/server-gametest` 行为包，支持真正的 `SimulatedPlayer`（simulated 类型假人）
- **自动部署**：首次启动自动释放行为包、注册 `world_behavior_packs.json`、编辑 `level.dat` 开启 Beta APIs 实验功能（自动备份）
- **scriptevent 鉴权**：启动时生成随机令牌，Endstone 与行为包双向消息均需携带，防止玩家伪造
- **simulated 坐标持久化**：行为包每 100 tick 上报坐标，重启后在最后位置恢复
- **自愈闭环**：周期性 ping 行为包，pong 携带玩家列表对照，丢失玩家自动重新生成
- **UUID 所有权**：假人记录新增 `ownerUuid`，优先按 UUID 判定管理权，防止名称重用越权
- **NBT DoS 防护**：读取深度限制 512、列表长度限制 1,000,000
- **脏标记批量落盘**：坐标高频变化只置脏标记，每 600 tick 统一写盘
- **并发保护**：共享状态加 `RLock`，迭代使用快照，消除字典并发修改异常

### 修复

- 持久化加载缺失：`_load_db()` 此前从未被调用，重启后假人丢失
- 名称校验补全字符集白名单（`^[a-zA-Z0-9_-]+$`），防止命令参数注入与 tickingarea 命令破坏
- 名称大小写绕过：索引统一小写化，`Bot` 与 `bot` 视为重名
- `world_behavior_packs.json` 解析异常时不再清空其他行为包注册
- 控制台执行命令时不再回退到第一个在线玩家的位置（隐私问题），改用世界出生点
- `format_date_time_beijing` 改用固定 UTC+8 时区，不受服务器时区影响
- `generate_id` 改用完整 UUID4，消除截断碰撞
- 行为包同名 spawn 改为幂等返回 `existed: true`，消除每 40 tick 的错误日志噪音

## [2.0.0] - 2026-08-17

- 移除自制模拟刷怪系统
- 适配 Endstone Python API 的基础假人框架

## [0.2.5] - 更早

- 旧版：NPC 实体生成 + tickingarea 常加载区域管理 + 模拟刷怪（已废弃）
