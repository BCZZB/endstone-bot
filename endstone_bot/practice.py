"""私人 Bot 定制功能（pracitse 分支）。"""

from __future__ import annotations

from typing import Any

from endstone_bot.models import FakePlayer, generate_id, format_date_time_beijing


def practice_default_profile() -> dict[str, Any]:
    return {
        "follow": False, "randomMove": False, "slowFalling": False,
        "fireResistance": False, "infiniteTotem": False, "armor": "none",
    }


def practice_profile_from_fp(fp: FakePlayer) -> dict[str, Any]:
    return {
        "follow": fp.practice_follow,
        "randomMove": fp.practice_random_move,
        "slowFalling": fp.practice_slow_falling,
        "fireResistance": fp.practice_fire_resistance,
        "infiniteTotem": fp.practice_infinite_totem,
        "armor": fp.practice_armor,
    }


def practice_send_config(plugin: Any, fp: FakePlayer) -> None:
    if not fp.practice_managed:
        return
    plugin._bridge.send_bridge("practice_config", {
        "n": fp.name, "owner": fp.owner_name,
        "follow": fp.practice_follow, "randomMove": fp.practice_random_move,
        "slowFalling": fp.practice_slow_falling,
        "fireResistance": fp.practice_fire_resistance,
        "infiniteTotem": fp.practice_infinite_totem,
        "armor": fp.practice_armor,
    })


def profile_key(player: Any) -> str:
    uid = str(getattr(player, "unique_id", "") or "")
    return uid or str(getattr(player, "name", "") or "").strip().lower()