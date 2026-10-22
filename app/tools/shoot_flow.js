#!/usr/bin/env node
/**
 * 全流程截图：用无头 Chrome 走一遍「大屏 → 报名 → 排队 → 取图」，逐页存图。
 *
 * 为什么不用 puppeteer：本机没装，而且要现场可复跑。Node 24 自带 WebSocket，
 * 直连 Chrome DevTools Protocol 就够了 —— 零依赖，现场改一行就能重跑。
 *
 * 用法（在仓库根目录）：
 *   node app/tools/shoot_flow.js [基线地址] [输出目录]
 *   默认 http://192.0.2.10:8090 与 ./shots
 *
 * 坑（都是实测踩过的）：
 *  · 窄屏必须用 Emulation.setDeviceMetricsOverride，不能用 --window-size：
 *    chromium 窗口最小宽度 518px，--window-size=390 会被夹住，
 *    结果拿到的是"按 518 排版只截 390"的图。
 *  · 退出要用记下的 PID 关，别 taskkill /IM chrome.exe —— 那会把机器上
 *    所有 Chrome 窗口一起关掉。
 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");

const BASE = (process.argv[2] || "http://192.0.2.10:8090").replace(/\/$/, "");
const OUT = path.resolve(process.argv[3] || "shots");
const CHROME = process.env.CHROME_BIN ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

// 页面清单：每一项 = 一页 × 一个视口。宽高按现场实际设备给。
const SHOTS = [
  { name: "01-index-desktop", url: "/", w: 1440, h: 900, wait: 3500 },
  { name: "02-index-phone", url: "/", w: 390, h: 844, wait: 3000 },
  { name: "03-stage", url: "/stage", w: 1920, h: 1080, wait: 4000 },
  { name: "04-vertical", url: "/vertical", w: 1080, h: 1920, wait: 4000 },
  { name: "05-operator", url: "/operator", w: 1440, h: 900, wait: 3500 },
  { name: "06-join", url: "/join", w: 390, h: 844, wait: 3000 },
];

let PICK_TOKEN = "";

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchJson(url) {
  const res = await fetch(url);
  return res.json();
}

/** 从最近一条记录里取一个可用令牌（取图页需要真实令牌才有内容）。 */
async function findPickToken() {
  try {
    const state = await fetchJson(`${BASE}/api/state`);
    if (state && state.last_token) return state.last_token;
  } catch (err) { /* 继续 */ }
  return "";
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    });
  }
  send(method, params) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} 超时`));
        }
      }, 30000);
    });
  }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  PICK_TOKEN = await findPickToken();
  if (PICK_TOKEN) {
    SHOTS.push({ name: "07-pick-phone", url: `/p/${PICK_TOKEN}`, w: 390, h: 844, wait: 3500 });
    SHOTS.push({ name: "08-pick-desktop", url: `/p/${PICK_TOKEN}`, w: 1440, h: 900, wait: 3500 });
    console.log(`取图页令牌：${PICK_TOKEN}`);
  } else {
    console.log("没有可用取图令牌（last_token 为空），跳过取图页");
  }

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "devcon-shots-"));
  const port = 9222 + Math.floor(Math.random() * 400);
  const child = spawn(CHROME, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--hide-scrollbars",
    "about:blank",
  ], { stdio: "ignore" });

  const pid = child.pid;
  console.log(`Chrome 已启动 pid=${pid} 端口=${port}`);

  let target = null;
  for (let i = 0; i < 40; i++) {
    await sleep(300);
    try {
      const list = await fetchJson(`http://127.0.0.1:${port}/json/list`);
      target = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (target) break;
    } catch (err) { /* 还没起来 */ }
  }
  if (!target) {
    console.error("连不上 Chrome 调试端口");
    try { process.kill(pid); } catch (e) {}
    process.exit(1);
  }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const cdp = new CDP(ws);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");

  const results = [];
  for (const shot of SHOTS) {
    const url = BASE + shot.url;
    // 窄屏必须走 deviceMetricsOverride（见文件头注释）
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: shot.w, height: shot.h, deviceScaleFactor: 1, mobile: shot.w < 600,
    });
    await cdp.send("Page.navigate", { url });
    await sleep(shot.wait);

    // 收集页面上的报错，截图之外也给一份文字证据。
    // 断图的判据必须是"设了 src 却加载失败"：空的 <img> 占位（等拍摄回填的
    // #shot、等生成的 #qr、等推理的 #mask）天然是 complete+naturaWidth=0，
    // 按 naturalWidth 判会把每一页的占位元素都误报成断图。
    const probe = await cdp.send("Runtime.evaluate", {
      expression: `JSON.stringify({
        title: document.title,
        theme: document.documentElement.getAttribute("data-theme"),
        bodyBg: getComputedStyle(document.body).backgroundColor,
        imgCount: document.images.length,
        brokenImgs: Array.from(document.images).filter(i => {
          const s = i.getAttribute("src");
          return s && s.trim() !== "" && i.complete && i.naturalWidth === 0;
        }).length,
        text: (document.body.innerText || "").slice(0, 160)
      })`,
      returnByValue: true,
    });

    const png = await cdp.send("Page.captureScreenshot", { format: "png" });
    const file = path.join(OUT, `${shot.name}.png`);
    fs.writeFileSync(file, Buffer.from(png.data, "base64"));

    let info = {};
    try { info = JSON.parse(probe.result.value); } catch (e) {}
    results.push({ shot: shot.name, w: shot.w, h: shot.h, url: shot.url, ...info });
    console.log(`${shot.name}  ${shot.w}x${shot.h}  主题=${info.theme}  断图=${info.brokenImgs}`);
  }

  fs.writeFileSync(path.join(OUT, "report.json"),
    JSON.stringify(results, null, 2), "utf-8");

  ws.close();
  try { process.kill(pid); } catch (e) {}
  // 临时 profile 删不掉是常事：Chrome 退出后还会攥着几个文件（Windows 上必现
  // EPERM）。这只是个临时目录，删不掉不该让整轮截图算失败 —— 忽略即可。
  try {
    fs.rmSync(profile, { recursive: true, force: true });
  } catch (err) {
    console.log(`（临时 profile 未能删除，可忽略：${profile}）`);
  }

  console.log(`\n完成，${SHOTS.length} 张图与 report.json 在 ${OUT}`);

  const broken = results.filter((r) => r.brokenImgs > 0);
  if (broken.length) {
    console.log("有断图的页面：");
    broken.forEach((r) => console.log(`  ${r.shot}: ${r.brokenImgs} 张`));
    process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error("失败：", err);
  process.exit(1);
});
