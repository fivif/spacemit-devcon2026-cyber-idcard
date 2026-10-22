"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const flow = require("../static/auto-flow.js");

let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}

function load(build) {
  const context = {
    URLSearchParams,
    document: {
      readyState: "loading",
      querySelector: (selector) => selector === 'meta[name="build"]' ? { content: build }
        : selector === 'meta[name="build-guard"]' ? { content: "manual" } : null,
      addEventListener() {},
    },
    fetch: async () => ({ ok: true, json: async () => ({ styles: flow.STATIC_STYLE_FALLBACK }) }),
    Image: class {
      set src(value) { this.url = value; this.onload(); }
    },
  };
  context.window = context;
  vm.createContext(context);
  for (const file of ["js/net.js", "auto-flow.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../static", file), "utf8"), context);
  }
  return context;
}

async function main() {
  const plain = "/static/assets/styles/pixel-person-preview.jpg";
  check(flow.thumbCandidatesFor("pixel-person")[0] === plain, "Node缺少Booth时保持候选图兼容");
  check(flow.thumbCandidatesFor("pixel-person", "pixel-person-preview.jpg").length === 4, "无版本时显式候选仍去重");
  const chibiPlain = "/static/assets/styles/pixel-person-chibi-preview.jpg";
  const legacy = flow.thumbCandidatesFor("pixel-person", "custom.jpg");
  check(JSON.stringify(flow.thumbCandidatesFor("pixel-person", "custom.jpg", "standard")) === JSON.stringify(legacy), "普通模式保留旧候选顺序");
  check(JSON.stringify(flow.thumbCandidatesFor("pixel-person", "custom.jpg", "chibi")) === JSON.stringify([chibiPlain, ...legacy]), "Q图优先，其余按旧普通候选顺序回退");
  const first = load("v1 +中文");
  const second = load("v2-fingerprint");
  for (const context of [first, second]) {
    const build = context.Booth.BUILD;
    const valid = (url) => new URL(url, "http://localhost").searchParams.get("v") === build;
    check(flow.STATIC_STYLE_FALLBACK.every((style) => valid(context.Booth.styleThumb(style.id))), "六款共享预览携带BUILD：" + build);
    const candidates = context.AutoFlowEntry.thumbCandidatesFor("pixel-person");
    check(candidates.length === 4 && candidates.every(valid), "全部候选与回退图携带BUILD：" + build);
    check(candidates[0] === context.Booth.styleThumb("pixel-person"), "手机与共享预览URL一致：" + build);
    const chibi = context.AutoFlowEntry.thumbCandidatesFor("pixel-person", "", "chibi");
    check(chibi.length === 5 && chibi[0] === context.Booth.styleThumb("pixel-person", "chibi") &&
      JSON.stringify(chibi.slice(1)) === JSON.stringify(candidates), "Q版命名、版本及普通回退一致：" + build);
    check(flow.STATIC_STYLE_FALLBACK.every((style) => ["standard", "chibi"].every((mode) =>
      new URL(context.Booth.styleThumb(style.id, mode), "http://localhost").searchParams.get("preview_v") === context.Booth.STYLE_PREVIEW_VERSION)), "12张预览都有独立资源版本：" + build);
    check(context.Booth.styleThumb("pixel-person", "invalid") === context.Booth.styleThumb("pixel-person"), "未识别模式兼容普通预览：" + build);
    const explicit = context.AutoFlowEntry.thumbCandidatesFor("pixel-person", "custom.jpg?size=small&v=old#sample")[0];
    const parsed = new URL(explicit, "http://localhost");
    check(valid(explicit) && parsed.searchParams.getAll("v").length === 1 &&
      parsed.searchParams.get("size") === "small" && parsed.hash === "#sample", "更新旧版本并保留查询与片段：" + build);
    check(context.AutoFlowEntry.thumbCandidatesFor("pixel-person", plain + "?v=old").length === 4, "版本统一后显式预览去重：" + build);
    check(context.Booth.versionedAssetUrl(explicit) === explicit, "重复版本化不改变URL：" + build);
    const remoteUrls = [
      "https://images.example.com/static/preview.jpg?signature=a%2Fb%2Bc&v=signed#sample",
      "http://images.example.com/preview.jpg?signature=a%20b",
      "//images.example.com/static/preview.jpg?signature=a%2Fb",
    ];
    check(remoteUrls.every((url) => context.Booth.versionedAssetUrl(url) === url), "远程签名地址原样保留：" + build);
    check(remoteUrls.every((url) => context.AutoFlowEntry.thumbCandidatesFor("pixel-person", url)[0] === url), "显式远程预览不追加版本：" + build);
    check(["/api/thumb/photo.jpg?token=a%2Fb", "/static-other/preview.jpg", "static/preview.jpg"].every((url) =>
      context.Booth.versionedAssetUrl(url) === url), "仅处理本地static路径：" + build);
    const carousel = await context.Booth.carouselItems();
    check(carousel.length === flow.STATIC_STYLE_FALLBACK.length * 2 && carousel.every((item) => valid(item.src)), "大屏轮播探测12张版本化预览：" + build);
    check(carousel.every((item, index) => {
      const pathname = new URL(item.src, "http://localhost").pathname;
      return item.portrait_mode === (index % 2 ? "chibi" : "standard") &&
        (index % 2 ? pathname.endsWith("-chibi-preview.jpg") :
          pathname.endsWith("-preview.jpg") && !pathname.endsWith("-chibi-preview.jpg"));
    }), "大屏按每款非Q版与Q版成对轮播：" + build);
  }
  check(first.Booth.styleThumb("pixel-person") !== second.Booth.styleThumb("pixel-person"), "BUILD更新会改变缓存键");
  const empty = load("");
  const noBuild = new URL(empty.Booth.styleThumb("pixel-person"), "http://localhost");
  check(!noBuild.searchParams.has("v") && noBuild.searchParams.get("preview_v") === empty.Booth.STYLE_PREVIEW_VERSION &&
    empty.AutoFlowEntry.thumbCandidatesFor("pixel-person")[0] === noBuild.pathname + noBuild.search, "缺少BUILD仍用独立资源版本刷新预览");
  check(empty.Booth.versionedAssetUrl("/static/custom.png") === "/static/custom.png", "缺少BUILD时其他资源保持原样");
  console.log("\n" + checks + " checks passed; no network requests.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
