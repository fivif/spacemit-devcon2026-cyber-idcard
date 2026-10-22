"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
  const classes = new Set();
  return {
    dataset: {}, attributes: {}, src: "",
    children: [], style: { setProperty() {} },
    replaceChildren(...children) { this.children = children; },
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
    },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; if (name === "src") this.src = ""; },
  };
}

async function main() {
  const nodes = new Map();
  const steps = Array.from({ length: 3 }, element);
  const images = [];
  const intervals = new Map();
  const listeners = new Map();
  let nextTimer = 0;
  let currentTime = 1000;
  const window = {
    setInterval(callback) { const id = ++nextTimer; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
  };
  const document = {
    hidden: false,
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) { if (listeners.get(name) === callback) listeners.delete(name); },
    createElement() { return element(); },
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, element());
      return nodes.get(selector);
    },
    querySelectorAll() { return steps; },
  };
  class MockImage {
    constructor() { images.push(this); }
    decode() { return this.pending || Promise.resolve(); }
  }
  const code = fs.readFileSync(path.join(__dirname, "../static/js/create-scene.js"), "utf8");
  vm.runInNewContext(code, { window, document, Image: MockImage, performance: { now: () => currentTime } });
  const core = window.BoothCreateCore();
  const select = (selector) => nodes.get(selector);
  const job = { job: "one", stage: "inferring", capture_preview_url: "/one.jpg" };
  core.update(job);
  assert.equal(core.active, true);
  assert.equal(select("#v-core-mode").textContent, "写实版");
  assert.equal(select("#v-core-contour-particles").classList.contains("is-fallback"), true);
  assert.equal(select("#v-core-contour-particles").classList.contains("is-visible"), true);
  assert.equal(select("#v-core-contour-particles").children.length, 44);
  assert.equal(select("#v-core-engine").textContent.includes("AI 核"), false);
  assert.equal(select("#v-core-subject-frame").hidden, true);
  const firstLetters = select("#v-core-caption").children;
  core.update(job);
  assert.equal(select("#v-core-caption").children, firstLetters);
  assert.equal(images.length, 1);
  await images[0].onload();
  assert.equal(select("#v-core-capture").src, "/one.jpg");
  core.update({ ...job, stage: "matting", cutout_url: "/cutout.png", steps: [{ key: "render" }] });
  assert.equal(select("#v-core-caption").attributes["aria-label"], "正在精修人物边缘");
  await images[1].onload();
  assert.equal(select("#v-core-caption").children.map((letter) => letter.textContent).join(""), "人像已分离");
  core.update({ ...job, stage: "matting", cutout_url: "/cutout.png", steps: [{ key: "render" }] });
  assert.equal(select("#v-core-caption").children.map((letter) => letter.textContent).join(""), "人像已分离");
  core.update({ ...job, stage: "stylizing", cutout_url: "/cutout.png", steps: [{ key: "render" }] });
  assert.equal(select("#v-core").dataset.phase, "style");
  assert.notEqual(select("#v-core-caption").children, firstLetters);
  assert.equal(select("#v-core-caption").children.map((letter) => letter.textContent).join(""), "你的风格正在成形");
  assert.equal(steps[1].attributes["aria-current"], "step");
  assert.equal(select("#v-core").classList.contains("has-cutout"), true);
  assert.equal(intervals.size, 1);
  assert.equal(select("#v-core-elapsed").textContent, "已等待 00:00");
  currentTime += 61000;
  [...intervals.values()][0]();
  assert.equal(select("#v-core-elapsed").textContent, "已等待 01:01");
  assert.equal(select("#v-core-caption").attributes["aria-label"], "仍在生成，请稍候");
  core.update({ ...job, stage: "stylizing", cutout_url: "/cutout.png" });
  assert.equal(select("#v-core-caption").attributes["aria-label"], "仍在生成，请稍候");
  assert.equal(intervals.size, 1);
  document.hidden = true;
  listeners.get("visibilitychange")();
  assert.equal(intervals.size, 0);
  assert.equal(select("#v-core").classList.contains("is-paused"), true);
  currentTime += 90000;
  document.hidden = false;
  listeners.get("visibilitychange")();
  assert.equal(intervals.size, 1);
  assert.equal(select("#v-core-elapsed").textContent, "已等待 02:31");
  assert.equal(steps[1].attributes["aria-current"], "step");

  core.update({ ...job, stage: "stylizing", style_preview_url: "/late.jpg" });
  let finishDecode;
  const late = images[2];
  late.pending = new Promise((resolve) => { finishDecode = resolve; });
  const waiting = late.onload();
  core.update({ job: "two", stage: "capturing" });
  assert.equal(intervals.size, 0);
  assert.equal(select("#v-core-elapsed").hidden, true);
  assert.equal(select("#v-core-capture").src, "");
  assert.equal(select("#v-core-cutout").src, "");
  finishDecode();
  await waiting;
  assert.equal(select("#v-core-result").src, "");
  assert.equal(select("#v-core").classList.contains("has-result"), false);

  core.update({ job: "two", stage: "stylizing", style_preview_url: "/two.jpg" });
  images.at(-1).onerror();
  const failedCount = images.length;
  core.update({ job: "two", stage: "stylizing", style_preview_url: "/two.jpg" });
  assert.equal(images.length, failedCount + 1);
  await images.at(-1).onload();
  assert.equal(select("#v-core-result").src, "/two.jpg");
  assert.equal(intervals.size, 0);
  assert.equal(select("#v-core-caption").attributes["aria-label"], "正在完成照片");
  core.update({ job: "two", stage: "failed" });
  assert.equal(core.active, false);
  assert.equal(select("#v-core-result").src, "");
  core.update({ job: "three", stage: "countdown" });
  assert.equal(core.active, false);
  core.update({ job: "three", stage: "inferring", capture_preview_url: "/three.jpg" });
  const lateLoad = images.at(-1).onload;
  core.clear();
  await lateLoad();
  assert.equal(select("#v-core-capture").src, "");
  core.update({ job: "mask", stage: "matting", segmentation: { mask_url: "/actual-subject.png", ep: true } });
  const subjectLoad = images.at(-1);
  assert.equal(select("#v-core").classList.contains("has-segmentation"), false);
  await subjectLoad.onload();
  assert.equal(select("#v-core").classList.contains("has-segmentation"), true);
  assert.equal(select("#v-core-contour-particles").children.length, 44);
  assert.equal(select("#v-core-contour-particles").classList.contains("is-visible"), true);
  assert.equal(select("#v-core-segmentation").style.maskImage, 'url("/actual-subject.png")');
  assert.equal(select("#v-core-particle-field").style.maskImage, 'url("/actual-subject.png")');
  assert.equal(select("#v-core-engine").textContent.includes("K3 AI 核"), true);
  core.update({ job: "next-mask", stage: "matting", segmentation: { mask_url: "/late-subject.png", ep: false } });
  const staleSubject = images.at(-1).onload;
  core.update({ job: "after-mask", stage: "inferring" });
  await staleSubject();
  assert.equal(select("#v-core").classList.contains("has-segmentation"), false);
  assert.equal(select("#v-core-segmentation").style.maskImage, "");
  core.destroy();
  assert.equal(listeners.size, 0);
  assert.equal(intervals.size, 0);
  core.update(job);
  assert.equal(core.active, false);
  console.log("PASS: 图片加载、真实阶段、长时等待、后台暂停、跨任务隔离、失败重试与资源清理");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
