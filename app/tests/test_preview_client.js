// test_preview_client.js —— 前端取景流守护（net.js 的 watchStream）行为测试。
//
// 为什么单独测这一段：MJPEG 连接中途被掐断**不会**触发 error，页面会永远停在
// 最后一帧。大屏无人值守，只能靠这段 JS 自己发现「画面不动了」并重连，同时把
// 状态如实告诉页面（不这样做，现场的人只会以为相机坏了在干等）。
//
// 需要 node（开发机/CI 用；K3 上没有 node，所以这一条是可选用例）：
//   node app/tests/test_preview_client.js
"use strict";
const fs = require("fs");
const path = require("path");

let checks = 0, fails = 0;
function check(cond, name, detail) {
  checks++;
  const ok = !!cond;
  if (!ok) fails++;
  console.log("  [" + (ok ? "PASS" : "FAIL") + "] " + name + (detail ? " | " + detail : ""));
}

let pixel = 1;
const ctx = {
  drawImage() {},
  getImageData() { return { data: new Uint8ClampedArray(24 * 24 * 4).fill(pixel) }; },
};
function makeImg() {
  return {
    naturalWidth: 0, src: "", _ls: {},
    addEventListener(name, fn) { (this._ls[name] = this._ls[name] || []).push(fn); },
    removeEventListener(name, fn) { this._ls[name] = (this._ls[name] || []).filter((listener) => listener !== fn); },
    fire(name, event) { (this._ls[name] || []).forEach((fn) => fn(event || {})); },
  };
}

const timers = [];
global.window = global;
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  cookie: "",
  body: { appendChild() {} },
  createElement: () => ({ width: 0, height: 0, getContext: () => ctx, appendChild() {}, style: {},
                          classList: { add() {}, remove() {} } }),
};
global.fetch = async () => { throw new Error("测试里不发请求"); };
global.setInterval = (fn) => { timers.push(fn); return timers.length; };
global.clearInterval = () => {};

const src = fs.readFileSync(path.join(__dirname, "..", "static", "js", "net.js"), "utf8");
eval(src);  // net.js 是 IIFE，执行后挂在 window.Booth 上

const { watchStream } = global.Booth;
const img = makeImg();
const states = [];
const watcher = watchStream(img, { onState: (s, d) => states.push([s, d || ""]), period: 50, stale: 3 });

check(typeof watcher.beat === "function", "watchStream 暴露 beat()（便于测试与手动触发）");

// 1) 一个帧都没有
img.naturalWidth = 0;
watcher.beat();
check(states.length === 0, "a) 偶尔没帧不算断（不轻易报状态）", JSON.stringify(states));
watcher.beat();
check(states.length === 1 && states[0][0] === "stalled" && states[0][1] === "no-frame",
      "a) 连续两次没帧 → 报 stalled(no-frame)", JSON.stringify(states[0] || null));
check(img.src.indexOf("/api/preview.mjpg?t=") === 0, "a) 并且真的重新拉流（src 带时间戳换新）", img.src);

// 2) 画面回来了
img.naturalWidth = 1280;
img.fire("load");
check(states.length === 2 && states[1][0] === "live", "b) 收到帧 → 报 live", JSON.stringify(states[1] || null));

// 3) 画面卡住（指纹连续 3 次不变）
pixel = 5;
watcher.beat();
watcher.beat();
check(states.length === 2, "c) 只卡 1 次不误判", JSON.stringify(states[states.length - 1]));
watcher.beat();
watcher.beat();
watcher.beat();
check(states.length === 3 && states[2][0] === "stalled" && states[2][1] === "frozen",
      "c) 连续 3 次一模一样 → 报 stalled(frozen) 并重连", JSON.stringify(states[2] || null));

// 4) 重连后又活过来
img.fire("load");
check(states.length === 4 && states[3][0] === "live", "d) 重连收到帧 → 回到 live", JSON.stringify(states[3] || null));

// 5) 状态不重复刷（避免页面反复闪）
const before = states.length;
img.fire("load");
img.fire("load");
check(states.length === before, "e) 状态没变就不重复通知", "states=" + states.length + " (before=" + before + ")");

watcher.stop();
img.src = "/api/capture-preview/random-token.jpg";
img.naturalWidth = 0;
const stoppedStates = states.length;
watcher.beat();
watcher.beat();
watcher.reopen("frozen");
img.fire("load");
check(img.src === "/api/capture-preview/random-token.jpg", "停止守护后，旧回调不能把抓拍照片切回视频");
check(states.length === stoppedStates, "停止守护后，静态图加载不触发流状态通知");
check(img._ls.load.length === 0, "停止守护时卸载加载监听器");
const nextWatcher = watchStream(img, { onState: (state) => states.push([state, ""]) });
check(img._ls.load.length === 1, "下一轮仅注册一个监听器");
img.naturalWidth = 1280;
img.fire("load");
check(states.length === stoppedStates + 1 && states.at(-1)[0] === "live", "下一轮取景正常恢复");
nextWatcher.stop();

console.log("\n" + checks + " checks, " + fails + " fail");
process.exit(fails ? 1 : 0);
