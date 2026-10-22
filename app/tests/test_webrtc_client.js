"use strict";
const fs = require("fs");
const path = require("path");

let checks = 0;
let fails = 0;
function check(condition, label) {
  checks++;
  if (!condition) fails++;
  console.log("  [" + (condition ? "PASS" : "FAIL") + "] " + label);
}
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const classes = new Set();
const video = {
  srcObject: null,
  readyState: 2,
  videoWidth: 1280,
  currentTime: 1,
  paused: false,
  listeners: {},
  classList: {
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
  },
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
  pause() { this.paused = true; },
  play() { this.paused = false; return Promise.resolve(); },
  requestVideoFrameCallback(fn) { this.frameCallback = fn; return 1; },
  cancelVideoFrameCallback() {},
};

const peers = [];
class FakePeer {
  constructor() {
    this.connectionState = "new";
    this.iceGatheringState = "complete";
    peers.push(this);
  }
  addTransceiver() {}
  async createOffer() { return { type: "offer", sdp: "offer" }; }
  async setLocalDescription(value) { this.localDescription = value; }
  async setRemoteDescription() {}
  addEventListener() {}
  removeEventListener() {}
  close() { this.connectionState = "closed"; }
}

global.window = global;
global.RTCPeerConnection = FakePeer;
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  cookie: "",
  body: { appendChild() {} },
  createElement: () => ({ classList: { add() {}, remove() {} }, style: {}, appendChild() {} }),
};
global.fetch = async () => ({ ok: true, text: async () => "answer" });
eval(fs.readFileSync(path.join(__dirname, "..", "static", "js", "net.js"), "utf8"));

async function run() {
  const states = [];
  const watcher = global.Booth.watchWebRTC(video, {
    period: 10000,
    stale: 2,
    firstFrameTimeout: 80,
    disconnectGrace: 30,
    retryDelay: 10,
    onState: (state, detail) => states.push([state, detail]),
  });
  await wait(0);
  const firstPeer = peers[0];
  firstPeer.ontrack({ streams: [{ id: "first" }] });
  check(states.at(-1)[0] !== "live", "收到 track 但未收到视频帧时不报 live");
  video.frameCallback(0, { mediaTime: 0.04 });
  check(states.at(-1)[0] === "live" && classes.has("is-visible"), "收到首个真实视频帧后进入 live");

  firstPeer.connectionState = "disconnected";
  firstPeer.onconnectionstatechange();
  await wait(25);
  firstPeer.connectionState = "connected";
  firstPeer.onconnectionstatechange();
  await wait(40);
  check(peers.length === 1, "短暂 disconnected 在缓冲期内恢复，不重连");

  video.currentTime = 2;
  watcher.beat();
  watcher.beat();
  watcher.beat();
  check(states.at(-1)[0] === "stalled", "检测到冻结后报告 stalled 并开始重连");
  check(video.srcObject && classes.has("is-visible"), "重连过程中保留最后画面，不清空视频层");
  await wait(40);
  check(peers.length >= 2, "冻结后自动创建新的 WHEP 连接");

  watcher.stop();
  check(video.srcObject === null && !classes.has("is-visible"), "stop 后清理视频资源与可见状态");
  const count = peers.length;
  await wait(100);
  check(peers.length === count, "stop 后不再触发重连");
  console.log("\n" + checks + " checks, " + fails + " fail");
  process.exit(fails ? 1 : 0);
}
run().catch((error) => { console.error(error); process.exit(1); });
