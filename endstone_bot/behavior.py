"""假人行为系统（idle / station / follow）。"""

from __future__ import annotations

import math
from typing import Any

from endstone.level import Location

from endstone_bot.models import (
    FakePlayer, BotBehavior,
    FOLLOW_OFFSET_DISTANCE, FOLLOW_TELEPORT_DISTANCE_SQ,
    POSITION_GUARD_DISTANCE_SQ, STATION_THRESHOLD_SQ,
)


class BehaviorSystem:
    """管理假人的 idle/station/follow 行为。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def apply(self, fp: FakePlayer, force: bool = False) -> None:
        """执行假人行为。"""
        behavior = fp.behavior
        if fp.type == "simulated":
            self._apply_simulated(fp, behavior, force)
            return
        actor = fp.actor
        if not self._is_valid(actor):
            return
        if force or self._plugin._tick_counter % 10 == 0:
            if behavior.movement == "station":
                self._handle_station(fp, behavior)
            elif behavior.movement == "follow":
                self._handle_follow(fp, behavior)
            elif behavior.movement == "idle":
                self._guard_position(fp)

    def _apply_simulated(self, fp: FakePlayer, behavior: BotBehavior, force: bool) -> None:
        if not self._plugin._bridge.active or not fp.sim_has_position:
            return
        if not (force or self._plugin._tick_counter % 10 == 0):
            return
        try:
            if behavior.movement == "station":
                sx, sy, sz = behavior.station_x, behavior.station_y, behavior.station_z
                if sx is None or sy is None or sz is None:
                    return
                dx = fp.sim_actual_x - sx
                dy = fp.sim_actual_y - sy
                dz = fp.sim_actual_z - sz
                if dx * dx + dy * dy + dz * dz > STATION_THRESHOLD_SQ:
                    self._teleport(fp, sx, sy, sz)
            elif behavior.movement == "follow":
                target_name = behavior.target_player.strip()
                if not target_name:
                    return
                target = self._plugin._find_online_player(target_name)
                if target is None:
                    return
                target_loc = target.location
                target_dim = self._plugin._dimension_id(target_loc.dimension)
                if target_dim != fp.dimension:
                    return
                dx = target_loc.x - fp.sim_actual_x
                dy = target_loc.y - fp.sim_actual_y
                dz = target_loc.z - fp.sim_actual_z
                if dx * dx + dy * dy + dz * dz > FOLLOW_TELEPORT_DISTANCE_SQ:
                    yaw = self._player_yaw(target)
                    offset_x = -math.sin(yaw) * FOLLOW_OFFSET_DISTANCE
                    offset_z = math.cos(yaw) * FOLLOW_OFFSET_DISTANCE
                    self._teleport(fp, target_loc.x + offset_x, target_loc.y, target_loc.z + offset_z)
            elif behavior.movement == "idle":
                dx = fp.sim_actual_x - fp.location_x
                dy = fp.sim_actual_y - fp.location_y
                dz = fp.sim_actual_z - fp.location_z
                if dx * dx + dy * dy + dz * dz > POSITION_GUARD_DISTANCE_SQ:
                    self._teleport(fp, fp.location_x, fp.location_y, fp.location_z)
        except Exception as exc:
            self._plugin.logger.debug(f"simulated 行为执行失败 {fp.name}: {exc}")

    def _guard_position(self, fp: FakePlayer) -> None:
        if fp.behavior.movement != "idle":
            return
        try:
            loc = fp.actor.location
            dx = loc.x - fp.location_x
            dy = loc.y - fp.location_y
            dz = loc.z - fp.location_z
            if dx * dx + dy * dy + dz * dz > POSITION_GUARD_DISTANCE_SQ:
                dim = self._plugin._get_dimension(fp.dimension)
                if dim is not None:
                    target = Location(dim, fp.location_x, fp.location_y, fp.location_z, pitch=fp.rotation_x, yaw=fp.rotation_y)
                    self._safe_teleport(fp.actor, target)
        except Exception:
            pass

    def _handle_station(self, fp: FakePlayer, behavior: BotBehavior) -> None:
        if behavior.station_x is None or behavior.station_y is None or behavior.station_z is None:
            return
        try:
            loc = fp.actor.location
            dx = loc.x - behavior.station_x
            dy = loc.y - behavior.station_y
            dz = loc.z - behavior.station_z
            if dx * dx + dy * dy + dz * dz > STATION_THRESHOLD_SQ:
                dim = self._plugin._get_dimension(fp.dimension)
                if dim is not None:
                    self._safe_teleport(fp.actor, Location(dim, behavior.station_x, behavior.station_y, behavior.station_z))
        except Exception:
            pass

    def _handle_follow(self, fp: FakePlayer, behavior: BotBehavior) -> None:
        target_name = behavior.target_player.strip()
        if not target_name:
            return
        target = self._plugin._find_online_player(target_name)
        if target is None:
            return
        try:
            target_loc = target.location
            bot_loc = fp.actor.location
            bot_dim = self._plugin._dimension_id(bot_loc.dimension)
            target_dim = self._plugin._dimension_id(target_loc.dimension)
            if bot_dim != target_dim:
                return
            dx = target_loc.x - bot_loc.x
            dy = target_loc.y - bot_loc.y
            dz = target_loc.z - bot_loc.z
            if dx * dx + dy * dy + dz * dz > FOLLOW_TELEPORT_DISTANCE_SQ:
                yaw = self._player_yaw(target)
                offset_x = -math.sin(yaw) * FOLLOW_OFFSET_DISTANCE
                offset_z = math.cos(yaw) * FOLLOW_OFFSET_DISTANCE
                self._safe_teleport(fp.actor, Location(target_loc.dimension, target_loc.x + offset_x, target_loc.y, target_loc.z + offset_z))
        except Exception as exc:
            self._plugin.logger.debug(f"跟随行为失败 {fp.name}: {exc}")

    def _teleport(self, fp: FakePlayer, x: float, y: float, z: float) -> None:
        self._plugin._bridge.send_bridge("teleport", {
            "n": fp.name, "x": round(float(x), 2), "y": round(float(y), 2), "z": round(float(z), 2), "d": fp.dimension,
        })

    @staticmethod
    def _is_valid(actor: Any) -> bool:
        try:
            return bool(actor and actor.is_valid and not actor.is_dead)
        except Exception:
            return False

    @staticmethod
    def _safe_teleport(actor: Any, loc: Location) -> None:
        try:
            actor.teleport(loc)
        except Exception:
            try:
                actor.teleport(loc.x, loc.y, loc.z)
            except Exception:
                try:
                    actor.set_location(loc)
                except Exception:
                    pass

    @staticmethod
    def _player_yaw(player: Any) -> float:
        try:
            rot = getattr(player, "rotation", None)
            if rot is not None:
                return math.radians(float(rot.y))
        except Exception:
            pass
        return 0.0