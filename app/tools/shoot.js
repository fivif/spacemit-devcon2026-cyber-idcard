#!/usr/bin/env node
/**
 * shoot.js —— 用 CDP 给现场界面逐页截图（零依赖，Node 22+ 自带 WebSocket）
 *
 * 为什么不用 `chrome --screenshot`：那个没法点、没法等、`--window-size=390`
 * 还会被 chromium 的 518px 最小窗宽夹住（见 knowledge/04 坑 30），窄屏页会
 * 拿到「按 518 排版只截 390」的假图。走 CDP 才能用
 * Emulation.setDeviceMetricsOverride 真按手机视口渲染。
 *
 * 用法：
 *   node shoot.js <输出目录> [只跑某个页面的 key]
 * 前置：Chrome 已用 --remote-debugging-port=9222 启动（脚本会自己保证）。
 */

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const PORT = 9222;
const BASE = process.env.BOOTH_BASE || "http://192.0.2.10:8090";
const CHROME =
  process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

const outDir = process.argv[2] || "shots";
const only = process.argv[3] || null;

// ---- 要截的页面：key / URL / 视口 / 截图前要做的动作 ----
const PAGES = [
  { key: "stage", url: "/stage", w: 1920, h: 1080, wait: 3500,
    note: "大屏算力视图（待机轮播）" },
  { key: "vertical", url: "/vertical", w: 1080, h: 1920, wait: 3500,
    note: "竖屏预览（待机）" },
  { key: "operator", url: "/operator", w: 1440, h: 900, wait: 3000,
    note: "操作员面板" },
  { key: "booth", url: "/", w: 1440, h: 900, wait: 3000,
    note: "拍摄位" },
  { key: "join", url: "/join", w: 390, h: 844, wait: 3000,
    note: "手机报名页" },
];

const get = (p) =>
  new Promise((res, rej) => {
    http
      .get(p, (r) => {
        let d = "";
        r.on("data", (c) => (d += c));
        r.on("end", () => res(d));
      })
      .on("error", rej);
  });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function connect() {
  // 复用已在跑的调试实例；没有就自己起一个
  try {
    await get(`http://127.0.0.1:${PORT}/json/version`);
  } catch {
    const profile = path.join(process.env.TEMP || ".", "cdp-booth-shot");
    fs.mkdirSync(profile, { recursive: true });
    const proc = spawn(
      CHROME,
      [
        "--headless=new",
        `--remote-debugging-port=${PORT}`,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        `--user-data-dir=${profile}`,
        "about:blank",
      ],
      { detached: true, stdio: "ignore" }
    );
    proc.unref();
    console.log(`已启动 Chrome（pid=${proc.pid}）`);
    for (let i = 0; i < 40; i++) {
      await sleep(250);
      try {
        await get(`http://127.0.0.1:${PORT}/json/version`);
        break;
      } catch {}
    }
  }
  const list = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/list`));
  // 必须挑 type==="page"：列表里还有 background_page / browser_ui，取 [0] 会连错目标
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("没找到可用的 page 目标");
  return page.webSocketDebuggerUrl;
}

async function shoot() {
  fs.mkdirSync(outDir, { recursive: true });
  const wsUrl = await connect();
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const events = new Map();
  const send = (method, params = {}) =>
    new Promise((res) => {
      const i = ++id;
      pending.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
    });
  const once = (ev) => new Promise((res) => events.set(ev, res));
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      const r = pending.get(msg.id);
      pending.delete(msg.id);
      r(msg.result || msg.error || {});
    } else if (msg.method && events.has(msg.method)) {
      const r = events.get(msg.method);
      events.delete(msg.method);
      r(msg.params);
    }
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");

  const rows = [];
  for (const p of PAGES) {
    if (only && p.key !== only) continue;
    // 真按目标视口渲染（窄屏必须走这条路，否则被 518px 最小窗宽夹住）
    await send("Emulation.setDeviceMetricsOverride", {
      width: p.w, height: p.h, deviceScaleFactor: 1, mobile: p.w < 600,
    });
    const loaded = once("Page.loadEventFired");
    await send("Page.navigate", { url: BASE + p.url });
    await loaded;
    await sleep(p.wait); // 等 JS 轮询 / 轮播 / 字体落定
    const shot = await send("Page.captureScreenshot", { format: "png" });
    if (!shot.data) {
      console.error(`✗ ${p.key} 截图失败`, shot);
      continue;
    }
    const file = path.join(outDir, `${p.key}-${p.w}x${p.h}.png`);
    fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
    const kb = Math.round(fs.statSync(file).size / 1024);
    console.log(`✓ ${p.key.padEnd(9)} ${p.w}x${p.h}  ${kb} KB  ${p.note}`);
    rows.push({ key: p.key, file, w: p.w, h: p.h });
  }
  ws.close();
  return rows;
}

shoot()
  .then((rows) => {
    console.log(`\n共 ${rows.length} 张，输出到 ${path.resolve(outDir)}`);
    process.exit(0);
  })
  .catch((e) => {
    console.error("失败：", e);
    process.exit(1);
  });
