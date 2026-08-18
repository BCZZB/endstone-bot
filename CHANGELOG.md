# 更新日志

本项目的所有显著变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
