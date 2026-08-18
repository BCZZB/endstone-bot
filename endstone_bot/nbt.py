"""Bedrock Edition NBT 二进制补丁模块。

采用二进制补丁方式修改 level.dat，只修改需要变更的字节，
不重新序列化整个 NBT 树，确保不破坏任何其他数据。

支持 Bedrock 小端 NBT 格式（用于 level.dat 和 .mcstructure）。
- level.dat: 8 字节头（version uint32_le + length uint32_le）+ NBT 数据
- .mcstructure: 纯 NBT 数据，无头

参考:
  - https://wiki.bedrock.dev/nbt/enabling-experiments
  - Bedrock NBT 使用小端字节序
"""

from __future__ import annotations

import struct
from typing import Any

# NBT 标签类型
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

# DoS 防护：限制递归深度和列表长度
MAX_NBT_DEPTH = 512
MAX_LIST_LENGTH = 1_000_000


class NBTReader:
    """小端 NBT 读取器，支持跳过 payload 和记录偏移量。"""

    def __init__(self, data: bytes, base_offset: int = 0, depth: int = 0) -> None:
        self._data = data
        self._pos = base_offset
        self._depth = depth

    @property
    def position(self) -> int:
        return self._pos

    def _read(self, n: int) -> bytes:
        result = self._data[self._pos : self._pos + n]
        if len(result) < n:
            raise EOFError("NBT 数据意外结束")
        self._pos += n
        return result

    def _read_ubyte(self) -> int:
        return struct.unpack("<B", self._read(1))[0]

    def _read_ushort(self) -> int:
        return struct.unpack("<H", self._read(2))[0]

    def _read_int(self) -> int:
        return struct.unpack("<i", self._read(4))[0]

    def _read_string(self) -> str:
        length = self._read_ushort()
        return self._read(length).decode("utf-8", errors="replace")

    def skip_payload(self, tag_type: int) -> None:
        """跳过指定类型的 payload，不解析内容。"""
        if tag_type == TAG_BYTE:
            self._pos += 1
        elif tag_type == TAG_SHORT:
            self._pos += 2
        elif tag_type == TAG_INT:
            self._pos += 4
        elif tag_type == TAG_LONG:
            self._pos += 8
        elif tag_type == TAG_FLOAT:
            self._pos += 4
        elif tag_type == TAG_DOUBLE:
            self._pos += 8
        elif tag_type == TAG_BYTE_ARRAY:
            length = self._read_int()
            if length < 0 or length > MAX_LIST_LENGTH:
                raise ValueError(f"NBT byte array 长度超限: {length}")
            self._pos += length
        elif tag_type == TAG_STRING:
            length = self._read_ushort()
            self._pos += length
        elif tag_type == TAG_LIST:
            element_type = self._read_ubyte()
            count = self._read_int()
            if count < 0 or count > MAX_LIST_LENGTH:
                raise ValueError(f"NBT list 长度超限: {count}")
            if element_type == TAG_END or count == 0:
                return
            self._depth += 1
            if self._depth > MAX_NBT_DEPTH:
                raise ValueError(f"NBT 递归深度超限: {self._depth}")
            for _ in range(count):
                self.skip_payload(element_type)
            self._depth -= 1
        elif tag_type == TAG_COMPOUND:
            self._depth += 1
            if self._depth > MAX_NBT_DEPTH:
                raise ValueError(f"NBT 递归深度超限: {self._depth}")
            while True:
                child_type = self._read_ubyte()
                if child_type == TAG_END:
                    break
                self._read_string()  # skip name
                self.skip_payload(child_type)
            self._depth -= 1
        elif tag_type == TAG_INT_ARRAY:
            length = self._read_int()
            if length < 0 or length > MAX_LIST_LENGTH:
                raise ValueError(f"NBT int array 长度超限: {length}")
            self._pos += length * 4
        elif tag_type == TAG_LONG_ARRAY:
            length = self._read_int()
            if length < 0 or length > MAX_LIST_LENGTH:
                raise ValueError(f"NBT long array 长度超限: {length}")
            self._pos += length * 8

    def read_root(self) -> dict[str, Any]:
        """读取根 Compound 标签。"""
        tag_type = self._read_ubyte()
        if tag_type != TAG_COMPOUND:
            raise ValueError(f"根标签不是 Compound (got {tag_type})")
        self._read_string()  # 根名称
        return self._read_compound_body()

    def _read_payload(self, tag_type: int) -> Any:
        if tag_type == TAG_BYTE:
            return struct.unpack("<b", self._read(1))[0]
        elif tag_type == TAG_SHORT:
            return struct.unpack("<h", self._read(2))[0]
        elif tag_type == TAG_INT:
            return struct.unpack("<i", self._read(4))[0]
        elif tag_type == TAG_LONG:
            return struct.unpack("<q", self._read(8))[0]
        elif tag_type == TAG_FLOAT:
            return struct.unpack("<f", self._read(4))[0]
        elif tag_type == TAG_DOUBLE:
            return struct.unpack("<d", self._read(8))[0]
        elif tag_type == TAG_BYTE_ARRAY:
            length = self._read_int()
            if length < 0 or length > MAX_LIST_LENGTH:
                raise ValueError(f"NBT byte array 长度超限: {length}")
            return list(struct.unpack(f"<{length}b", self._read(length)))
        elif tag_type == TAG_STRING:
            return self._read_string()
        elif tag_type == TAG_LIST:
            element_type = self._read_ubyte()
            count = self._read_int()
            if count < 0 or count > MAX_LIST_LENGTH:
                raise ValueError(f"NBT list 长度超限: {count}")
            if element_type == TAG_END or count == 0:
                return []
            self._depth += 1
            if self._depth > MAX_NBT_DEPTH:
                raise ValueError(f"NBT 递归深度超限: {self._depth}")
            result = [self._read_payload(element_type) for _ in range(count)]
            self._depth -= 1
            return result
        elif tag_type == TAG_COMPOUND:
            self._depth += 1
            if self._depth > MAX_NBT_DEPTH:
                raise ValueError(f"NBT 递归深度超限: {self._depth}")
            result = self._read_compound_body()
            self._depth -= 1
            return result
        elif tag_type == TAG_INT_ARRAY:
            length = self._read_int()
            if length < 0 or length > MAX_LIST_LENGTH:
                raise ValueError(f"NBT int array 长度超限: {length}")
            return list(struct.unpack(f"<{length}i", self._read(length * 4)))
        elif tag_type == TAG_LONG_ARRAY:
            length = self._read_int()
            if length < 0 or length > MAX_LIST_LENGTH:
                raise ValueError(f"NBT long array 长度超限: {length}")
            return list(struct.unpack(f"<{length}q", self._read(length * 8)))
        else:
            raise ValueError(f"未知 NBT 标签类型: {tag_type}")

    def _read_compound_body(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            tag_type = self._read_ubyte()
            if tag_type == TAG_END:
                break
            name = self._read_string()
            result[name] = self._read_payload(tag_type)
        return result


# ------------------------------------------------------------------
# 二进制补丁辅助函数
# ------------------------------------------------------------------


def get_root_body_offset(data: bytes, has_header: bool = False) -> int:
    """获取根 Compound body 在原始数据中的绝对偏移量。"""
    offset = 8 if has_header else 0
    reader = NBTReader(data, offset)
    reader._read_ubyte()  # TAG_COMPOUND
    reader._read_string()  # 根名称
    return reader.position


def find_byte_field_value_offset(
    data: bytes, compound_body_offset: int, field_name: str
) -> int | None:
    """在 Compound body 中查找 BYTE 字段的值偏移量。

    Returns:
        值字节的绝对偏移量，或 None（未找到或不是 BYTE 类型）
    """
    reader = NBTReader(data, compound_body_offset)
    while True:
        tag_type = reader._read_ubyte()
        if tag_type == TAG_END:
            return None
        name = reader._read_string()
        value_pos = reader.position
        if name == field_name:
            if tag_type == TAG_BYTE:
                return value_pos
            return None
        reader.skip_payload(tag_type)


def find_compound_field_offset(
    data: bytes, compound_body_offset: int, field_name: str
) -> int | None:
    """在 Compound body 中查找子 Compound 的 body 偏移量。

    Returns:
        子 Compound body 的绝对偏移量，或 None
    """
    reader = NBTReader(data, compound_body_offset)
    while True:
        tag_type = reader._read_ubyte()
        if tag_type == TAG_END:
            return None
        name = reader._read_string()
        body_offset = reader.position
        if name == field_name and tag_type == TAG_COMPOUND:
            return body_offset
        reader.skip_payload(tag_type)


def find_compound_end_offset(
    data: bytes, compound_body_offset: int
) -> int:
    """查找 Compound body 的 TAG_END 偏移量。

    Returns:
        TAG_END 字节的绝对偏移量
    """
    reader = NBTReader(data, compound_body_offset)
    while True:
        tag_type = reader._read_ubyte()
        if tag_type == TAG_END:
            return reader.position - 1
        reader._read_string()  # skip name
        reader.skip_payload(tag_type)


def make_byte_tag_bytes(name: str, value: int) -> bytes:
    """构造 Compound 内一个 BYTE 字段的完整字节（类型+名称长度+名称+值）。"""
    encoded_name = name.encode("utf-8")
    return (
        struct.pack("<B", TAG_BYTE)
        + struct.pack("<H", len(encoded_name))
        + encoded_name
        + struct.pack("<b", value)
    )


def make_compound_tag_bytes(name: str, body: bytes) -> bytes:
    """构造一个命名的 COMPOUND 标签的完整字节。"""
    encoded_name = name.encode("utf-8")
    return (
        struct.pack("<B", TAG_COMPOUND)
        + struct.pack("<H", len(encoded_name))
        + encoded_name
        + body
        + struct.pack("<B", TAG_END)
    )


# ------------------------------------------------------------------
# 高级读写接口（用于读取检测，不用于写回）
# ------------------------------------------------------------------


def read_bedrock_nbt(data: bytes, has_header: bool = False) -> dict[str, Any]:
    """读取 Bedrock NBT 数据（仅用于检测，不用于写回 level.dat）。"""
    if has_header:
        if len(data) < 8:
            raise ValueError("数据太短，无法包含头")
        data = data[8:]
    reader = NBTReader(data)
    return reader.read_root()
