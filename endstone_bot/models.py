"""假人数据模型。

严格按照 mcbes-manage-script 的 IFakePlayer 接口定义，
适配 Endstone Python 环境。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Literal


# ---------------------------------------------------------------------------
# 常量（同 mcbes-manage-script）
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH = 24
MAX_SKIN_ID = 15
POSITION_GUARD_DISTANCE_SQ = 1.0
STATION_THRESHOLD_SQ = 0.0025
FOLLOW_TELEPORT_DISTANCE_SQ = 36.0
FOLLOW_OFFSET_DISTANCE = 3.0

# scoreboard tag 前缀（同月华 yuehua_fake_player 系列）
FAKE_PLAYER_TAG = "yuehua_fake_player"
TAG_ID_PREFIX = "yuehua_fake_player_id:"
TAG_OWNER_PREFIX = "yuehua_fake_player_owner:"
TAG_SKIN_PREFIX = "yuehua_fake_player_skin_"

# 假人类型（同月华 FakePlayerType）
FakePlayerType = Literal["entity", "simulated"]

# 默认实体类型
LEGACY_ENTITY_TYPE = "minecraft:npc"


# ---------------------------------------------------------------------------
# 皮肤定义（同 mcbes-manage-script fake-player-skins.ts）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkinInfo:
    id: int
    name: str


SKINS: list[SkinInfo] = [
    SkinInfo(0, "初音未来"),
    SkinInfo(1, "平泽唯"),
    SkinInfo(2, "希露菲叶特"),
    SkinInfo(3, "艾莉丝·格雷拉特"),
    SkinInfo(4, "惠惠"),
    SkinInfo(5, "阿尼亚"),
    SkinInfo(6, "洛琪希"),
    SkinInfo(7, "凉宫春日"),
    SkinInfo(8, "长门有希"),
    SkinInfo(9, "艾米莉亚"),
    SkinInfo(10, "蕾姆"),
    SkinInfo(11, "Saber·战斗"),
    SkinInfo(12, "远坂凛"),
    SkinInfo(13, "Saber·日常"),
    SkinInfo(14, "狂三·战斗"),
    SkinInfo(15, "狂三·校服"),
]


def normalize_skin_id(value: Any) -> int:
    try:
        sid = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_SKIN_ID, sid))


def get_skin_name(skin_id: int) -> str:
    for skin in SKINS:
        if skin.id == skin_id:
            return skin.name
    return SKINS[0].name


# ---------------------------------------------------------------------------
# 行为配置（同 mcbes-manage-script FakePlayerBehavior）
# ---------------------------------------------------------------------------

MovementType = Literal["idle", "station", "follow"]
ActionType = Literal["none", "attack", "jump", "interact"]


@dataclass
class BotBehavior:
    """假人行为配置（同月华 FakePlayerBehavior）。

    NPC 能力有限，movement 实现为传送控制；
    action 仅记录触发（NPC 不支持 attack/jump/interact API）。
    """

    movement: MovementType = "idle"
    target_player: str = ""
    speed: float = 1.0
    action: ActionType = "none"
    interval_ticks: int = 20
    hotbar_slot: int = 0
    sneaking: bool = False
    station_x: float | None = None
    station_y: float | None = None
    station_z: float | None = None
    look_at_x: float | None = None
    look_at_y: float | None = None
    look_at_z: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "movement": self.movement,
            "target_player": self.target_player,
            "speed": self.speed,
            "action": self.action,
            "interval_ticks": self.interval_ticks,
            "hotbar_slot": self.hotbar_slot,
            "sneaking": self.sneaking,
            "station_x": self.station_x,
            "station_y": self.station_y,
            "station_z": self.station_z,
            "look_at_x": self.look_at_x,
            "look_at_y": self.look_at_y,
            "look_at_z": self.look_at_z,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BotBehavior:
        if not data:
            return cls()
        valid_m = ("idle", "station", "follow")
        valid_a = ("none", "attack", "jump", "interact")
        return cls(
            movement=data.get("movement", "idle") if data.get("movement") in valid_m else "idle",
            target_player=str(data.get("target_player", "")),
            speed=max(0.1, min(1.0, float(data.get("speed", 1.0)))),
            action=data.get("action", "none") if data.get("action") in valid_a else "none",
            interval_ticks=max(1, min(72000, int(data.get("interval_ticks", 20)))),
            hotbar_slot=max(0, min(8, int(data.get("hotbar_slot", 0)))),
            sneaking=bool(data.get("sneaking", False)),
            station_x=_safe_float(data.get("station_x")),
            station_y=_safe_float(data.get("station_y")),
            station_z=_safe_float(data.get("station_z")),
            look_at_x=_safe_float(data.get("look_at_x")),
            look_at_y=_safe_float(data.get("look_at_y")),
            look_at_z=_safe_float(data.get("look_at_z")),
        )


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# IFakePlayer（同 mcbes-manage-script IFakePlayer 接口）
# ---------------------------------------------------------------------------

@dataclass
class FakePlayer:
    """假人完整状态（同月华 IFakePlayer）。

    运行时字段（actor 等）不参与持久化；
    持久化字段通过 to_record / from_record 转换。
    """

    # === 运行时（不持久化）===
    actor: Any = None
    tickingarea_name: str = ""
    last_area_key: tuple[str, int, int, int] | None = None
    last_action_tick: int = 0
    sim_spawn_confirmed: bool = False  # simulated: 行为包已确认生成
    # simulated: 行为包上报的最新实际位置（idle 守护锚点不被覆盖）
    sim_actual_x: float = 0.0
    sim_actual_y: float = 0.0
    sim_actual_z: float = 0.0
    sim_has_position: bool = False  # simulated: 是否已收到首次坐标上报
    # === AI 功能（同 mcbes-manage-script 扩展）===
    ai_enabled: bool = False  # 该假人是否启用 AI 对话
    ai_members: list[str] = field(default_factory=list)  # 允许使用 @ai 的玩家列表
    # 私人定制分支：pracitse 区域召唤配置
    practice_managed: bool = False
    practice_follow: bool = False
    practice_random_move: bool = False
    practice_slow_falling: bool = False
    practice_fire_resistance: bool = False
    practice_infinite_totem: bool = False
    practice_armor: str = "none"  # none / diamond / netherite

    # === 持久化字段（同月华 IFakePlayer）===
    id: str = ""
    name: str = ""
    owner_name: str = ""
    owner_uuid: str = ""
    location_x: float = 0.5
    location_y: float = 80.0
    location_z: float = 0.5
    dimension: str = "overworld"
    rotation_x: float = 0.0
    rotation_y: float = 0.0
    created: str = ""
    type: FakePlayerType = "entity"
    skin_id: int = 0
    entity_id: str = ""
    behavior: BotBehavior = field(default_factory=BotBehavior)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ownerName": self.owner_name,
            "ownerUuid": self.owner_uuid,
            "location": [round(self.location_x, 2), round(self.location_y, 2), round(self.location_z, 2)],
            "dimension": self.dimension,
            "rotationX": round(self.rotation_x, 2),
            "rotationY": round(self.rotation_y, 2),
            "created": self.created,
            "type": self.type,
            "skinId": int(self.skin_id),
            "entityId": self.entity_id,
            "behavior": self.behavior.to_dict(),
            "aiEnabled": bool(self.ai_enabled),
            "aiMembers": list(self.ai_members),
            "practiceManaged": bool(self.practice_managed),
            "practiceFollow": bool(self.practice_follow),
            "practiceRandomMove": bool(self.practice_random_move),
            "practiceSlowFalling": bool(self.practice_slow_falling),
            "practiceFireResistance": bool(self.practice_fire_resistance),
            "practiceInfiniteTotem": bool(self.practice_infinite_totem),
            "practiceArmor": self.practice_armor,
        }

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> FakePlayer:
        loc = data.get("location", [0.5, 80.0, 0.5])
        if isinstance(loc, list) and len(loc) >= 3:
            lx, ly, lz = float(loc[0]), float(loc[1]), float(loc[2])
        else:
            lx, ly, lz = 0.5, 80.0, 0.5
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            owner_name=str(data.get("ownerName", "")),
            owner_uuid=str(data.get("ownerUuid", "")),
            location_x=lx,
            location_y=ly,
            location_z=lz,
            dimension=str(data.get("dimension", "overworld")),
            rotation_x=float(data.get("rotationX", 0.0)),
            rotation_y=float(data.get("rotationY", 0.0)),
            created=str(data.get("created", "")),
            type=data.get("type", "entity") if data.get("type") in ("entity", "simulated") else "entity",
            skin_id=normalize_skin_id(data.get("skinId", 0)),
            entity_id=str(data.get("entityId", "")),
            behavior=BotBehavior.from_dict(data.get("behavior")),
            ai_enabled=bool(data.get("aiEnabled", False)),
            ai_members=list(data.get("aiMembers", [])) if isinstance(data.get("aiMembers"), list) else [],
            practice_managed=bool(data.get("practiceManaged", False)),
            practice_follow=bool(data.get("practiceFollow", False)),
            practice_random_move=bool(data.get("practiceRandomMove", False)),
            practice_slow_falling=bool(data.get("practiceSlowFalling", False)),
            practice_fire_resistance=bool(data.get("practiceFireResistance", False)),
            practice_infinite_totem=bool(data.get("practiceInfiniteTotem", False)),
            practice_armor=str(data.get("practiceArmor", "none")) if data.get("practiceArmor") in ("none", "diamond", "netherite") else "none",
        )


# ---------------------------------------------------------------------------
# 工具函数（同 mcbes-manage-script）
# ---------------------------------------------------------------------------

def generate_id() -> str:
    """生成假人唯一 ID（同月华 generateId）。

    使用完整 UUID4（128 位），避免截断导致碰撞。
    """
    return uuid.uuid4().hex


_BEIJING_TZ = timezone(timedelta(hours=8))


def format_date_time_beijing() -> str:
    """北京时间格式化（同月华 formatDateTimeBeijing）。

    使用 UTC+8 时区，不受服务器本地时区影响。
    """
    now = datetime.now(_BEIJING_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S")


def build_fake_player_name_tag(item: FakePlayer) -> str:
    """构建假人头顶名称（同月华 buildFakePlayerNameTag）。

    格式：§b{name}\\n§7{owner} 的假人
    """
    return f"§b{item.name}\n§7{item.owner_name} 的假人"


_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_name(
    name: str,
    existing_names: set[str],
    online_player_names: set[str],
) -> str | None:
    """名称校验（同月华 validateName + 字符集限制）。

    返回 None 表示通过，返回字符串表示错误信息。
    """
    name = name.strip()
    if not name:
        return "假人名称不能为空"
    if len(name) > MAX_NAME_LENGTH:
        return f"假人名称不能超过 {MAX_NAME_LENGTH} 个字符"
    if not _NAME_PATTERN.match(name):
        return "假人名称只能包含字母、数字、下划线和短横线"
    lower = name.lower()
    if lower in {n.lower() for n in existing_names}:
        return "已有同名假人，请换一个名称"
    if lower in {n.lower() for n in online_player_names}:
        return "该名称已被在线玩家占用"
    return None
