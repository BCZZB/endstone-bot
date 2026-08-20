/**
 * EndstoneBot 假人桥接行为包脚本。
 *
 * 通过 @minecraft/server-gametest 的 SimulatedPlayer API
 * 为 Endstone 插件提供模拟玩家生成能力。
 *
 * 通信协议（通过 scriptevent 命令）:
 *
 * Endstone → 行为包:
 *   bot:ping          — 检测行为包是否活跃
 *   bot:spawn         — 生成模拟玩家 {"n":"名字","x":1.0,"y":64.0,"z":1.0,"d":"overworld"}
 *   bot:remove        — 移除模拟玩家 {"n":"名字"}
 *   bot:teleport      — 传送模拟玩家 {"n":"名字","x":1.0,"y":64.0,"z":1.0,"d":"overworld"}
 *   bot:list          — 列出所有模拟玩家
 *
 * 行为包 → Endstone:
 *   bot:pong          — 响应 ping
 *   bot:spawned       — 生成完成 {"n":"名字","ok":true}
 *   bot:removed       — 移除完成 {"n":"名字"}
 *   bot:list_result   — 列表结果 {"names":["name1","name2"]}
 *   bot:error         — 错误 {"n":"名字","e":"错误信息"}
 */

import { GameTest, Tags } from "@minecraft/server-gametest";
import { EquipmentSlot, ItemStack, system, world } from "@minecraft/server";

// 活跃的 GameTest 对象（用于 spawnSimulatedPlayer）
let activeTest = null;

// 已生成的模拟玩家映射: name → SimulatedPlayer
const simulatedPlayers = new Map();

// 待处理的生成请求队列（GameTest 未就绪时排队）
const pendingSpawns = [];

// 私人定制运行配置: name -> settings
const practiceConfigs = new Map();
const practiceState = new Map();

// 鉴权令牌（从 Endstone 插件的 ping 消息中获取）
let bridgeToken = "";

/**
 * 发送 scriptevent 回复给 Endstone（携带鉴权令牌）。
 */
function reply(eventId, data) {
    try {
        const payload = { ...data, t: bridgeToken };
        const msg = JSON.stringify(payload);
        world.getDimension("overworld").runCommand(`scriptevent ${eventId} ${msg}`);
    } catch (e) {
        console.warn(`[EndstoneBot] 回复失败: ${e}`);
    }
}

/**
 * 注册 GameTest（永久运行，提供 Test 对象用于 spawnSimulatedPlayer）。
 */
GameTest.register("endstone_bot", "sim_spawner", (test) => {
    activeTest = test;

    // 处理排队的生成请求
    while (pendingSpawns.length > 0) {
        const req = pendingSpawns.shift();
        doSpawnSimulatedPlayer(req);
    }
})
    .structureName("endstone_bot:empty")
    .maxTicks(0x7FFFFFFF)
    .tag(Tags.suiteDefault);

/**
 * 生成模拟玩家。
 */
function doSpawnSimulatedPlayer(req) {
    if (!activeTest) {
        pendingSpawns.push(req);
        return;
    }

    const name = req.n;
    if (simulatedPlayers.has(name)) {
        // 已存在同名玩家：幂等返回成功（existed=true），
        // 避免 Endstone 自愈任务每 40 tick 重发时刷错误日志
        reply("bot:spawned", { n: name, ok: true, existed: true });
        return;
    }

    try {
        const loc = { x: req.x, y: req.y, z: req.z };
        const sim = activeTest.spawnSimulatedPlayer(loc, name);

        if (sim) {
            simulatedPlayers.set(name, sim);
            try {
                sim.addTag("yuehua_fake_player");
                if (req.id) sim.addTag(`yuehua_fake_player_id:${req.id}`);
            } catch (_) {}
            reply("bot:spawned", { n: name, ok: true });

            // 监听玩家离开（SimulatedPlayer 被踢/断开）
            // SimulatedPlayer 会在 world.afterEvents.playerLeave 时被清理
        } else {
            reply("bot:error", { n: name, e: "spawnSimulatedPlayer 返回 null" });
        }
    } catch (e) {
        reply("bot:error", { n: name, e: String(e) });
    }
}

/**
 * 移除模拟玩家。
 */
function doRemoveSimulatedPlayer(name) {
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:error", { n: name, e: "模拟玩家不存在" });
        return;
    }

    try {
        sim.disconnect();
    } catch (e) {
        // disconnect 可能抛异常，忽略
    }

    simulatedPlayers.delete(name);
    reply("bot:removed", { n: name });
}

/**
 * 传送模拟玩家。
 */
function doTeleportSimulatedPlayer(req) {
    const name = req.n;
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:error", { n: name, e: "模拟玩家不存在" });
        return;
    }

    try {
        sim.teleport({ x: req.x, y: req.y, z: req.z }, { dimension: world.getDimension(req.d || "overworld") });
        reply("bot:teleported", { n: name });
    } catch (e) {
        reply("bot:error", { n: name, e: String(e) });
    }
}

function setPracticeConfig(req) {
    if (!req || !req.n) return;
    practiceConfigs.set(req.n, {
        owner: String(req.owner || ""),
        follow: !!req.follow,
        randomMove: !!req.randomMove,
        slowFalling: !!req.slowFalling,
        fireResistance: !!req.fireResistance,
        infiniteTotem: !!req.infiniteTotem,
        armor: ["diamond", "netherite"].includes(req.armor) ? req.armor : "none",
    });
    if (!practiceState.has(req.n)) practiceState.set(req.n, { angle: 0, nextMove: 0 });
}

function findOwner(name) {
    if (!name) return null;
    return world.getAllPlayers().find((p) => p.name === name) || null;
}

function equipItem(sim, slot, typeId, armor = false) {
    try {
        const equipment = sim.getComponent("minecraft:equippable");
        if (!equipment) return;
        const current = equipment.getEquipment(slot);
        if (current && current.typeId === typeId) return;
        const item = new ItemStack(typeId, 1);
        if (armor) {
            try {
                const ench = item.getComponent("minecraft:enchantable");
                ench?.addEnchantment({ type: "protection", level: 10 });
                ench?.addEnchantment({ type: "unbreaking", level: 32767 });
            } catch (_) {}
        }
        equipment.setEquipment(slot, item);
    } catch (_) {}
}

function maintainEquipment(sim, cfg) {
    if (cfg.infiniteTotem) {
        equipItem(sim, EquipmentSlot.Mainhand, "minecraft:totem_of_undying");
        equipItem(sim, EquipmentSlot.Offhand, "minecraft:totem_of_undying");
    }
    if (cfg.armor !== "none") {
        const p = cfg.armor === "netherite" ? "netherite" : "diamond";
        equipItem(sim, EquipmentSlot.Head, `minecraft:${p}_helmet`, true);
        equipItem(sim, EquipmentSlot.Chest, `minecraft:${p}_chestplate`, true);
        equipItem(sim, EquipmentSlot.Legs, `minecraft:${p}_leggings`, true);
        equipItem(sim, EquipmentSlot.Feet, `minecraft:${p}_boots`, true);
    }
}

function tickPractice(name, sim, cfg, tick) {
    const owner = findOwner(cfg.owner);
    const state = practiceState.get(name) || { angle: 0, nextMove: 0 };
    practiceState.set(name, state);
    try {
        if (cfg.slowFalling) sim.addEffect("slow_falling", 240, { amplifier: 1, showParticles: false });
        if (cfg.fireResistance) sim.addEffect("fire_resistance", 240, { amplifier: 254, showParticles: false });
    } catch (_) {}
    maintainEquipment(sim, cfg);

    if (tick < state.nextMove) return;
    state.nextMove = tick + 10;
    if (!cfg.follow && !cfg.randomMove) return;
    state.angle += (Math.random() - 0.5) * 1.2;
    let center = sim.location;
    if (cfg.follow && owner) center = owner.location;
    let x = center.x, y = center.y, z = center.z;
    if (cfg.randomMove) {
        x += Math.cos(state.angle) * (1 + Math.random() * 1.5);
        z += Math.sin(state.angle) * (1 + Math.random() * 1.5);
    } else if (cfg.follow && owner) {
        // 保持在玩家身后约 2 格，不使用疾跑速度。
        const yaw = Number(owner.getRotation()?.y || 0) * Math.PI / 180;
        x -= Math.sin(yaw) * 2;
        z += Math.cos(yaw) * 2;
    }
    try {
        const dx = x - sim.location.x, dz = z - sim.location.z;
        const distanceSq = dx * dx + dz * dz;
        if (distanceSq > 144) {
            sim.teleport({ x, y, z }, { dimension: owner?.dimension || sim.dimension });
        } else if (distanceSq > 0.25) {
            // GameTest SimulatedPlayer 原生寻路，速度参数 1 对应正常步行而非疾跑。
            sim.moveToLocation({ x, y, z }, 1);
        }
    } catch (_) {}
}

/**
 * 监听 scriptevent 命令。
 */
system.afterEvents.scriptEventReceive.subscribe((event) => {
    // 只处理 bot 命名空间
    if (!event.id.startsWith("bot:")) return;

    const action = event.id;
    let data = {};
    try {
        if (event.message && event.message.length > 0) {
            data = JSON.parse(event.message);
        }
    } catch (e) {
        console.warn(`[EndstoneBot] JSON 解析失败: ${event.message}`);
        return;
    }

    // 提取鉴权令牌（所有来自 Endstone 的消息都携带 t 字段）
    if (data.t) {
        bridgeToken = data.t;
    }

    switch (action) {
        case "bot:ping":
            // pong 携带当前管理的玩家列表，Endstone 用于对照重置确认状态
            reply("bot:pong", { names: Array.from(simulatedPlayers.keys()) });
            break;

        case "bot:spawn":
            doSpawnSimulatedPlayer(data);
            break;

        case "bot:remove":
            doRemoveSimulatedPlayer(data.n);
            break;

        case "bot:teleport":
            doTeleportSimulatedPlayer(data);
            break;

        case "bot:practice_config":
            setPracticeConfig(data);
            break;

        case "bot:list":
            reply("bot:list_result", { names: Array.from(simulatedPlayers.keys()) });
            break;

        default:
            break;
    }
});

/**
 * 每 100 tick：清理失效的模拟玩家 + 上报所有模拟玩家坐标。
 * 坐标上报使 Endstone 侧能持久化 simulated 假人位置（重启后恢复）。
 */
let practiceTick = 0;
system.runInterval(() => {
    practiceTick += 1;
    for (const [name, sim] of simulatedPlayers) {
        try {
            const cfg = practiceConfigs.get(name);
            if (cfg && sim.isValid) tickPractice(name, sim, cfg, practiceTick);
        } catch (_) {}
    }
}, 1);

system.runInterval(() => {
    if (simulatedPlayers.size === 0) return;

    const report = [];
    for (const [name, sim] of simulatedPlayers) {
        try {
            if (!sim.isValid) {
                simulatedPlayers.delete(name);
                continue;
            }
            const loc = sim.location;
            const dim = sim.dimension ? sim.dimension.id : "overworld";
            report.push({
                n: name,
                x: Math.round(loc.x * 100) / 100,
                y: Math.round(loc.y * 100) / 100,
                z: Math.round(loc.z * 100) / 100,
                d: dim,
            });
        } catch (e) {
            simulatedPlayers.delete(name);
        }
    }

    // 未完成鉴权握手（Endstone 尚未 ping）时不上报，避免无效消息
    if (report.length > 0 && bridgeToken) {
        reply("bot:positions", { p: report });
    }
}, 100);

console.log("[EndstoneBot] 假人桥接行为包已加载");
