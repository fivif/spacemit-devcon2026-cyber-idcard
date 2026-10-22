"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
  const classes = new Set();
  return {
    src: "", srcObject: null, currentTime: 0, videoWidth: 1280, readyState: 4,
    dataset: {}, children: [], style: {}, width: 0, height: 0,
    classList: {
      add(...values) { values.forEach((value) => classes.add(value)); },
      remove(...values) { values.forEach((value) => classes.delete(value)); },
      contains(value) { return classes.has(value); },
      toggle(value, on) { if (on) classes.add(value); else classes.delete(value); },
    },
    setAttribute() {},
    play() { return Promise.resolve(); },
    pause() {},
    removeAttribute(name) { if (name === "src") this.src = ""; },
    appendChild(child) { this.children.push(child); },
    getBoundingClientRect() { return { width: 100, height: 160 }; },
    getContext() { return { scale() {}, clearRect() {} }; },
  };
}

async function main() {
  const nodes = new Map();
  const select = (key) => {
    if (!nodes.has(key)) nodes.set(key, element());
    return nodes.get(key);
  };
  select("#idle-slide").parentElement = element();
  const intervals = [];
  const timeouts = new Map();
  const watchers = [];
  const images = [];
  const requests = [];
  let now = Date.now();
  const core = { active: false, update() {}, clear() { this.active = false; } };
  let nextTimer = 0;
  let current = { camera: { ok: true }, active_job: null, queue_active: 0,
    queue_capture_active: 0, queue_camera_active: 0 };
  let completed = null;
  const window = {
    BoothCreateCore: () => core,
    devicePixelRatio: 1,
    setTimeout(fn, delay) { const timer = ++nextTimer; timeouts.set(timer, { fn, delay }); return timer; },
    Booth: {
      $: select, fmtMs: String, reducedMotion: () => true,
      carouselItems: async () => [],
      api: { get: async (url) => {
        requests.push(url);
        if (url === "/api/state") return current;
        if (url.startsWith("/api/job/")) return completed;
        throw new Error("unexpected request " + url);
      } },
      watchWebRTC() {
        const watcher = { stopped: false, stop() { this.stopped = true; } };
        watchers.push(watcher);
        return watcher;
      },
    },
  };
  const context = {
    window, document: { createElement: element }, Date: { now: () => now }, console,
    Image: function () { const image = element(); images.push(image); return image; },
    setInterval(fn) { intervals.push(fn); },
    clearTimeout(timer) { timeouts.delete(timer); },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/js/vertical.js"), "utf8"), context);
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  await flush();
  const tick = async () => { await intervals[0](); await flush(); };
  const preview = select("#preview");
  assert.equal(preview.src, "");
  current.queue_active = 1;
  current.queue_capture_active = 1;
  current.queue_slot = { status: "queued", number: "1" };
  await tick();
  assert.equal(preview.src, "");
  assert.equal(watchers.length, 0);
  current.queue_slot.status = "called";
  await tick();
  assert.equal(preview.src, "");
  assert.equal(watchers.length, 0);
  current.queue_slot.status = "ready";
  current.queue_camera_active = 1;
  await tick();
  assert.equal(preview.src, "");
  assert.equal(select("#preview-stream").classList.contains("is-visible"), true);
  assert.equal(watchers.length, 1);
  current.queue_camera_active = 0;
  current.active_job = { job: "first", stage: "countdown", countdown_remaining: 8 };
  await tick();
  const countdown = select("#countdown");
  assert.equal(countdown.textContent, "8");
  assert.equal(countdown.classList.contains("is-show"), true);
  await tick();
  await tick();
  assert.equal(countdown.classList.contains("is-show"), true);
  assert.equal([...timeouts.values()].some((timer) => timer.delay === 700), false);
  current.active_job.countdown_remaining = 7;
  await tick();
  assert.equal(countdown.textContent, "7");
  assert.equal(countdown.classList.contains("is-show"), true);
  current.active_job.countdown_remaining = 0;
  await tick();
  assert.equal(countdown.classList.contains("is-show"), false);
  current.active_job.countdown_remaining = 1;
  await tick();
  assert.equal(countdown.classList.contains("is-show"), true);
  assert.equal(preview.src, "");
  const captureUrl = "/api/capture-preview/ABCDEFGHIJKLMNOPQRSTUV.jpg";
  current.active_job = { job: "first", stage: "inferring", capture_preview_url: captureUrl };
  await tick();
  assert.equal(preview.src, captureUrl);
  assert.equal(countdown.classList.contains("is-show"), false);
  assert.equal(watchers[0].stopped, true);
  for (const stage of ["matting", "stylizing", "rendering", "storing"]) {
    current.active_job.stage = stage;
    await tick();
    assert.equal(preview.src, captureUrl);
    assert.equal(watchers.length, 1);
  }
  const cutoutUrl = "/api/mask/SM-2026-0001-cutout.png";
  current.active_job.stage = "matting";
  current.active_job.cutout_url = cutoutUrl;
  await tick();
  assert.equal(images.length, 1);
  assert.equal(images[0].src, cutoutUrl);
  assert.equal(preview.src, captureUrl);
  images[0].onload();
  assert.equal(preview.src, cutoutUrl);
  assert.equal(select("#view").classList.contains("has-cutout"), true);
  for (const stage of ["stylizing", "rendering", "storing"]) {
    current.active_job.stage = stage;
    await tick();
    assert.equal(preview.src, cutoutUrl);
    assert.equal(images.length, 1);
    assert.equal(watchers.length, 1);
  }
  current.active_job = null;
  current.queue_active = 1;
  completed = { job: "first", stage: "done", record_id: "SM-2026-0001", token: "photo-token" };
  await tick();
  assert.equal(preview.src, cutoutUrl);
  assert.equal(select("#vresult").classList.contains("is-on"), false);
  select("#vresult-shot").onload();
  assert.equal(select("#vresult").classList.contains("is-on"), true);
  assert.equal(select("#vresult-portrait-mode").textContent, "写实版");
  assert.equal(select("#vresult-serial").textContent, "0001");
  await tick();
  assert.equal(watchers.length, 1);
  const resultTimer = [...timeouts.values()].find((timer) => timer.delay === 8000);
  assert.ok(resultTimer);
  resultTimer.fn();
  await tick();
  assert.equal(preview.src, "");
  assert.equal(select("#view").classList.contains("has-cutout"), false);
  assert.equal(watchers.length, 1);
  current.active_job = { job: "second", stage: "inferring", capture_preview_url: captureUrl + "?second" };
  await tick();
  current.active_job = null;
  completed = { job: "second", stage: "failed" };
  await tick();
  assert.equal(preview.src, "");
  current.active_job = { job: "third", stage: "inferring", capture_preview_url: captureUrl + "?third" };
  await tick();
  current.active_job.cutout_url = "/api/mask/SM-2026-0003-cutout.png";
  await tick();
  images[1].onerror();
  assert.equal(preview.src, captureUrl + "?third");
  await tick();
  assert.equal(images.length, 3);
  current.active_job = { job: "fourth", stage: "countdown", countdown_remaining: 8 };
  await tick();
  images[2].onload();
  assert.equal(preview.src, "");
  assert.equal(select("#view").classList.contains("has-cutout"), false);
  assert.equal(countdown.textContent, "8");
  current.active_job = null;
  completed = { job: "fourth", stage: "failed" };
  await tick();
  assert.equal(countdown.classList.contains("is-show"), false);
  current.active_job = { job: "fourth", stage: "inferring", capture_preview_url: captureUrl + "?fourth" };
  await tick();
  current.active_job = null;
  completed = { job: "fourth", stage: "done", record_id: "SM-2026-0004", token: "photo-token-4", portrait_mode: "chibi" };
  await tick();
  select("#vresult-shot").onerror();
  await tick();
  select("#vresult-shot").onload();
  assert.equal(select("#vresult-portrait-mode").textContent, "Q版大头像");
  assert.equal(preview.src, captureUrl + "?fourth");
  current.active_job = { job: "fifth", stage: "inferring", capture_preview_url: captureUrl + "?fifth" };
  await tick();
  current.active_job = null;
  completed = { job: "fifth", stage: "done", record_id: "SM-2026-0005", token: "photo-token-5" };
  await tick();
  const lateResult = select("#vresult-shot").onload;
  current.active_job = { job: "sixth", stage: "countdown", countdown_remaining: 8 };
  await tick();
  lateResult();
  assert.equal(select("#vresult").classList.contains("is-on"), false);
  current.presentation_mode = "handoff";
  current.active_job = { job: "background", stage: "stylizing", matte_applied: true,
    capture_preview_url: captureUrl + "?background" };
  core.active = true;
  await tick();
  assert.equal(select("#view").classList.contains("is-core"), true);
  current.active_job = null;
  current.handoff_notice = { job: "background" };
  current.queue_active = 1;
  current.queue_processing = 1;
  current.queue_capture_active = 0;
  await tick();
  assert.equal(preview.src, "");
  assert.equal(core.active, false);
  assert.equal(select("#view").classList.contains("is-core"), false);
  assert.equal(select("#view").classList.contains("is-handoff-notice"), true);
  assert.equal(select("#vprompt-title").textContent, "照片正在后台生成");
  assert.equal(select("#vprompt").classList.contains("is-on"), true);
  current.last_record = "SM-2026-0007";
  current.recent = [{ id: current.last_record, token: "background-token" }];
  current.queue_processing = 0;
  current.queue_active = 0;
  current.handoff_notice = null;
  const oldResultSrc = select("#vresult-shot").src;
  requests.length = 0;
  await tick();
  assert.equal(select("#view").classList.contains("is-handoff-notice"), true);
  now += 4100;
  await tick();
  assert.equal(select("#view").classList.contains("is-handoff-notice"), false);
  assert.equal(select("#view").classList.contains("is-idle"), true);
  assert.equal(select("#idle-show").classList.contains("hide"), false);
  assert.equal(select("#vresult-shot").src, oldResultSrc);
  assert.equal(requests.some((url) => url.includes("overlay") || url.startsWith("/api/job/")), false);
  current.queue_capture_active = 1;
  current.queue_slot = { status: "called", number: "SM-2026-0244" };
  await tick();
  assert.equal(select("#vprompt-kicker").textContent, "0244");
  assert.equal(select("#vresult").classList.contains("is-on"), false);
  console.log("PASS: 拍摄定格、抠像替换、跨作业隔离、居中后台提示、后台完成不弹图及简短编号");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
