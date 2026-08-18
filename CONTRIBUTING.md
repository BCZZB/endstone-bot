# 贡献指南

感谢你愿意为本项目做贡献！

## 开发环境搭建

```bash
# 克隆仓库
git clone https://github.com/BCZZB/endstone-bot.git
cd endstone-bot

# 创建虚拟环境（Python 3.10+）
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装构建工具
pip install build
```

## 构建与测试

```bash
# 构建 whl
python -m build

# 语法检查
python -m py_compile endstone_bot/*.py

# 安装到 BDS 测试
pip install dist/*.whl
```

行为包脚本修改后需检查 JS 语法：

```bash
node --check endstone_bot/behavior_pack/scripts/main.js
```

## 代码规范

- **Python**：类型注解齐全（`from __future__ import annotations` 风格）、模块/类/方法带中文 docstring、遵循现有命名约定（私有方法 `_` 前缀）
- **JavaScript**（行为包）：无构建步骤，直接使用 `@minecraft/server` beta API，保持与 Endstone 侧 `scriptevent` 协议同步
- **协议变更**：任何 `bot:*` scriptevent 消息格式变更必须同步修改两端并更新 README 的桥接图
- **兼容性**：`bots.json` 与 `level.dat` 的读写必须保持向后兼容，破坏性变更需在 CHANGELOG 标注

## 提交规范

提交信息使用中文或英文，格式：

```text
<类型>: <简述>

类型：
  feat     新功能
  fix      修复
  refactor 重构
  docs     文档
  chore    构建/工具
```

## 提交 Pull Request

1. Fork 仓库并创建特性分支：`git checkout -b feat/your-feature`
2. 提交改动并推送
3. 创建 PR 并描述改动内容与测试情况
4. 确保所有命令与 GUI 路径在真实 BDS 环境中测试通过

## 许可证

提交贡献即表示你同意将贡献以 [PolyForm Noncommercial License 1.0.0](LICENSE) 授权。

## 行为准则

- 保持友善与专业
- 对事不对人
- 尊重不同水平的贡献者
