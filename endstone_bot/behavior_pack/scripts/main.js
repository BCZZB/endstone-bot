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

import { register, Tags } from "@minecraft/server-gametest";
import { system, world } from "@minecraft/server";

// 活跃的 GameTest 对象（用于 spawnSimulatedPlayer）
let activeTest = null;

// 已生成的模拟玩家映射: name → SimulatedPlayer
const simulatedPlayers = new Map();

// 待处理的生成请求队列（GameTest 未就绪时排队）
const pendingSpawns = [];

// 鉴权令牌（从 Endstone 插件的 ping 消息中获取）
let bridgeToken = "";

// WebSocket 连接（优先通信通道，scriptevent 作为回退）
let ws = null;
let wsReconnectDelay = 40; // ticks
let wsTickCounter = 0;
const WS_URL = "ws://127.0.0.1:19150";

function wsSend(obj) {
    if (ws && ws.readyState === 1) {
        try { ws.send(JSON.stringify(obj)); } catch (_) {}
    }
}

function wsConnect() {
    try {
        ws = new WebSocket(WS_URL);
        ws.onopen = () => {
            console.log("[EndstoneBot] WebSocket 已连接");
            reply("bot:pong", { names: Array.from(simulatedPlayers.keys()) });
            wsSend({ type: "pong", data: { names: Array.from(simulatedPlayers.keys()), t: bridgeToken } });
        };
        ws.onmessage = (ev) => {
            try { handleWsMessage(JSON.parse(ev.data)); } catch (_) {}
        };
        ws.onclose = () => {
            console.warn("[EndstoneBot] WebSocket 断开，将重连");
            ws = null;
        };
        ws.onerror = () => { try { ws.close(); } catch (_) {} };
    } catch (e) {
        ws = null;
    }
}

function handleWsMessage(msg) {
    const action = msg && msg.type;
    const data = msg && msg.data;
    if (!action || !data) return;
    if (data.t) bridgeToken = data.t;
    if (action === "spawn") {
        doSpawnSimulatedPlayer({ n: data.n, id: data.id, x: data.x, y: data.y, z: data.z, d: data.d, t: data.t });
    } else if (action === "remove") {
        doRemoveSimulatedPlayer(data.n);
    } else if (action === "teleport") {
        doTeleportSimulatedPlayer({ n: data.n, x: data.x, y: data.y, z: data.z, d: data.d });
    } else if (action === "practice_config") {
        setPracticeConfig({ n: data.n, owner: data.owner, follow: data.follow,
            randomMove: data.randomMove, slowFalling: data.slowFalling,
            fireResistance: data.fireResistance, infiniteTotem: data.infiniteTotem, armor: data.armor });
    }
}

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
register("endstone_bot", "sim_spawner", (test) => {
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

/**
 * 监听 scriptevent 命令。
 */
system.afterEvents.scriptEventReceive.subscribe((event) => {
    // 只处理 bot 命名空间
    if (!event.id.startsWith("bot:")) {
        return;
    }

    console.log(`[EndstoneBot] 收到 scriptevent: ${event.id} 源=${event.sourceType}`);
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

    const action = event.id;
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
        // WS 已连接时走 WS，否则退回到 scriptevent
        wsSend({ type: "positions", data: { p: report, t: bridgeToken } });
        reply("bot:positions", { p: report });
    }
}, 100);

console.log("[EndstoneBot] 假人桥接行为包已加载");

// WS 连接重试
system.runInterval(() => {
    wsTickCounter++;
    if (!ws || ws.readyState !== 1) {
        if (wsTickCounter % wsReconnectDelay === 0) {
            wsConnect();
        }
    } else {
        // 已连接：定期用 WS 上报位置（原 scriptevent runInterval 也会走）
    }
}, 1);
