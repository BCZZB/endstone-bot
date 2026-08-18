"""level.dat 实验功能编辑器（二进制补丁方式）。

通过二进制补丁修改 level.dat，只修改/插入需要变更的字节，
不重新序列化整个 NBT 树，确保不破坏世界种子、出生点、时间等数据。

参考实际已启用 Beta APIs 的 level.dat 文件结构:
  experiments Compound 内包含 3 个 BYTE 标签:
    - gametest: 1  → "Beta APIs" 开关
    - experiments_ever_used: 1  → 标记曾使用实验功能
    - saved_with_toggled_experiments: 1  → 标记保存时实验已开启

level.dat 格式（Bedrock 小端 NBT）:
  8 字节头: version (uint32_le) + length (uint32_le)
  后接 NBT 数据（小端，无压缩）
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from endstone_bot.nbt import (
    find_byte_field_value_offset,
    find_compound_end_offset,
    find_compound_field_offset,
    get_root_body_offset,
    make_byte_tag_bytes,
    make_compound_tag_bytes,
    read_bedrock_nbt,
)


# experiments Compound 内需要设置的 BYTE 标签
EXPERIMENTS_FIELDS = {
    "gametest": 1,                         # "Beta APIs"
    "experiments_ever_used": 1,            # 标记曾使用实验功能
    "saved_with_toggled_experiments": 1,   # 标记保存时实验已开启
}


def enable_experiments(level_dat_path: Path) -> bool:
    """在 level.dat 中启用实验功能（二进制补丁）。

    只修改/插入实验功能相关的字节，不触碰其他任何数据。

    Args:
        level_dat_path: level.dat 文件路径

    Returns:
        True 表示成功修改了文件，False 表示无需修改或修改失败
    """
    if not level_dat_path.exists():
        return False

    raw = level_dat_path.read_bytes()
    if len(raw) < 8:
        return False

    # 解析头部
    version, nbt_length = struct.unpack("<II", raw[:8])

    # 获取根 Compound body 偏移量
    try:
        root_body = get_root_body_offset(raw, has_header=True)
    except Exception:
        return False

    patches: list[tuple[int, int, bytes]] = []  # (offset, delete_len, insert_bytes)
    modified = False

    # 查找 experiments Compound
    exp_body = find_compound_field_offset(raw, root_body, "experiments")

    if exp_body is not None:
        # experiments Compound 已存在，逐个检查/设置 byte 标签
        for tag_name, tag_value in EXPERIMENTS_FIELDS.items():
            val_offset = find_byte_field_value_offset(raw, exp_body, tag_name)
            if val_offset is not None:
                # 标签已存在，检查值
                current = struct.unpack("<b", raw[val_offset:val_offset + 1])[0]
                if current != tag_value:
                    # 修改值（原地替换 1 字节，不改变长度）
                    patches.append((val_offset, 1, struct.pack("<b", tag_value)))
                    modified = True
            else:
                # 标签不存在，在 experiments Compound 的 TAG_END 前插入
                exp_end = find_compound_end_offset(raw, exp_body)
                new_tag = make_byte_tag_bytes(tag_name, tag_value)
                patches.append((exp_end, 0, new_tag))
                modified = True
    else:
        # experiments Compound 不存在，在根 Compound 的 TAG_END 前插入整个 Compound
        root_end = find_compound_end_offset(raw, root_body)
        exp_body_bytes = b""
        for tag_name, tag_value in EXPERIMENTS_FIELDS.items():
            exp_body_bytes += make_byte_tag_bytes(tag_name, tag_value)
        exp_compound = make_compound_tag_bytes("experiments", exp_body_bytes)
        patches.append((root_end, 0, exp_compound))
        modified = True

    if not modified:
        return False

    # 应用补丁（从后往前应用，避免偏移量变化）
    patches.sort(key=lambda p: p[0], reverse=True)

    result = bytearray(raw)
    for offset, delete_len, insert_bytes in patches:
        result[offset:offset + delete_len] = insert_bytes

    # 更新头部长度（如果插入了字节）
    new_nbt_length = len(result) - 8
    if new_nbt_length != nbt_length:
        struct.pack_into("<I", result, 4, new_nbt_length)

    # 备份原文件
    backup = level_dat_path.with_suffix(".dat.bak")
    if not backup.exists():
        shutil.copy2(level_dat_path, backup)

    # 写回
    try:
        level_dat_path.write_bytes(bytes(result))
        return True
    except Exception:
        return False


def is_experiments_enabled(level_dat_path: Path) -> bool:
    """检查 level.dat 中是否已启用实验功能。

    Args:
        level_dat_path: level.dat 文件路径

    Returns:
        True 表示已启用
    """
    if not level_dat_path.exists():
        return False

    try:
        raw = level_dat_path.read_bytes()
        data = read_bedrock_nbt(raw, has_header=True)
        experiments = data.get("experiments", {})
        return experiments.get("gametest", 0) == 1
    except Exception:
        return False
