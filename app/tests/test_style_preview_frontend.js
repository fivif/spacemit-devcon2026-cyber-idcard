"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");
const flow = require("../static/auto-flow.js");

const staticRoot = path.join(__dirname, "..", "static");
const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");
const styles = flow.STATIC_STYLE_FALLBACK.map((style) => ({ ...style, enabled: true }));
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}

async function mount(browser, base, route, options = {}) {
  const context = await browser.newContext({ reducedMotion: "reduce", viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const calls = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const NativeImage = window.Image;
    const source = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
    window.previewProbeControl = { mode: "", pending: [] };
    window.Image = function (...args) {
      const image = new NativeImage(...args);
      Object.defineProperty(image, "src", {
        get() { return source.get.call(image); },
        set(value) {
          const pathname = new URL(value, location.href).pathname;
          const mode = pathname.endsWith("-chibi-preview.jpg") ? "chibi" : "standard";
          if (pathname.endsWith("-preview.jpg") && mode === window.previewProbeControl.mode) {
            window.previewProbeControl.pending.push(() => source.set.call(image, value));
          } else source.set.call(image, value);
        },
      });
      return image;
    };
  });
  if (options.preference) await page.addInitScript((mode) => localStorage.setItem("booth.portrait_mode.v1", mode), options.preference);
  await context.route("**/*", async (request) => {
    const url = new URL(request.request().url());
    assert.equal(url.origin, base, "只访问隔离的本地测试页面");
    const pathname = url.pathname;
    if (/^\/static\/assets\/styles\/.+-preview\.jpg$/.test(pathname)) {
      const mode = pathname.endsWith("-chibi-preview.jpg") ? "chibi" : "standard";
      if (options.missingChibi && mode === "chibi") return request.fulfill({ status: 404, body: "" });
      return request.fulfill({ contentType: "image/png", body: pixel });
    }
    if (!pathname.startsWith("/api/")) return request.continue();
    calls.push({ path: pathname, method: request.request().method(), body: request.request().postDataJSON() });
    if (/\.(jpg|png|mjpg)$/.test(pathname)) return request.fulfill({ contentType: "image/png", body: pixel });
    let payload = { ok: true };
    if (pathname === "/api/build") payload = { build: "test" };
    else if (pathname === "/api/public/status") payload = { queue_enabled: true, aigc_enabled: true };
    else if (pathname === "/api/styles" || pathname === "/api/aigc/styles") payload = { styles };
    else if (pathname === "/api/recovery/current") payload = { ok: false };
    else if (pathname === "/api/consent") payload = { ok: true, token: "test-consent", consent_id: "test-consent" };
    else if (pathname === "/api/state") payload = { aigc_enabled: true, styles, default_style: "pixel-person",
      camera: { ok: true, real: true }, infer: {}, disk: { free_gb: 10, total_gb: 32 }, counters: {}, recent: [] };
    return request.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.goto(base + route);
  if (route === "/join") {
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#af-join-watermark").uncheck();
    await page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).click();
  }
  return { context, page, errors, calls,
    hold: (mode) => page.evaluate((value) => { window.previewProbeControl.mode = value; }, mode),
    release: () => page.evaluate(() => {
      window.previewProbeControl.mode = "";
      window.previewProbeControl.pending.splice(0).forEach((resolve) => resolve());
    }),
  };
}

async function previews(page, route, mode) {
  await page.waitForFunction(({ route, mode }) => {
    const selector = route === "/join" ? ".af-style-card img" : "#styles .style-card img";
    const images = Array.from(document.querySelectorAll(selector));
    const matches = (image) => image.naturalWidth > 0 && new URL(image.src).pathname.endsWith(
      mode === "chibi" ? "-chibi-preview.jpg" : "-preview.jpg") &&
      new URL(image.src).pathname.includes("-chibi-preview.jpg") === (mode === "chibi") &&
      new URL(image.src).searchParams.get("preview_v") === window.Booth.STYLE_PREVIEW_VERSION;
    return images.length === 6 && images.every(matches) && (route === "/join" ||
      (document.querySelectorAll(".af-carousel-dot").length === 6 && matches(document.querySelector("#af-carousel-img"))));
  }, { route, mode }).catch(async (error) => {
    console.error(await page.locator(".af-style-card img, #styles .style-card img, #af-carousel-img").evaluateAll((images) =>
      images.map((image) => ({ src: image.src, width: image.naturalWidth }))));
    throw error;
  });
}

async function main() {
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const documentName = pathname === "/" ? "index.html" : pathname === "/join" ? "join.html" : null;
    const file = documentName ? path.join(staticRoot, documentName) : path.resolve(staticRoot, "." + pathname.replace(/^\/static/, ""));
    if ((!documentName && !pathname.startsWith("/static/")) || !file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)) {
      response.statusCode = 404;
      return response.end();
    }
    const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
    response.setHeader("Content-Type", types[path.extname(file)] || "application/octet-stream");
    response.end(documentName ? fs.readFileSync(file, "utf8").replaceAll("__BUILD__", "test") : fs.readFileSync(file));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = "http://127.0.0.1:" + server.address().port;
  let browser;
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.FRONTEND_BROWSER_CHANNEL || "chrome" });
    for (const route of ["/join", "/"]) {
      const mounted = await mount(browser, base, route);
      const { page } = mounted;
      await previews(page, route, "standard");
      check(true, route + "初始6张普通预览携带资源版本");
      if (route === "/join") {
        await page.locator('[data-style-id="american-comic"]').click();
        await page.locator("#af-join-optional").evaluate((node) => { node.open = true; });
        await page.locator("#af-join-name").fill("保留昵称");
        await page.locator("#af-join-handle").fill("保留账号");
      } else {
        await page.locator("#styles .style-card").nth(1).click();
        await page.locator("#af-inline").evaluate((node) => { node.open = true; });
        await page.locator(".direct-capture .optional-info").evaluate((node) => { node.open = true; });
        await page.locator("#name").fill("保留昵称");
        await page.locator("#handle").fill("保留账号");
        await page.locator("#af-style").selectOption("hand-doodle");
        await page.locator(".af-carousel-dot").nth(3).click();
      }
      const selector = route === "/join" ? '[name="join_portrait_mode"]' : '[name="capture_portrait_mode"]';
      const selected = route === "/join" ? ".af-style-card.is-selected" : "#styles .style-card.is-selected";
      const originalCard = await page.locator(selected).elementHandle();
      await page.locator(selector + '[value="chibi"]').check();
      await previews(page, route, "chibi");
      check(await originalCard.evaluate((node) => node.isConnected && node.classList.contains("is-selected")), route + "切Q版只换图，保留选中卡片节点");
      const prefix = route === "/join" ? "#af-join-" : "#";
      check(await page.locator(prefix + "name").inputValue() === "保留昵称" &&
        await page.locator(prefix + "handle").inputValue() === "保留账号", route + "保留填写信息");
      if (route === "/join") check(!(await page.locator("#af-join-watermark").isChecked()) &&
        !(await page.locator("#af-join-submit").isDisabled()), "切模式保留水印选择与拍摄同意");
      else check(await page.locator("#af-style").inputValue() === "hand-doodle" &&
        await page.locator("#af-carousel").getAttribute("data-af-style-id") === "professional-suit" &&
        await page.locator('[name="queue_portrait_mode"][value="chibi"]').isChecked(), "首页模式同步，保留轮播当前风格及报名选择");
      for (const delayedMode of ["standard", "chibi"]) {
        const finalMode = delayedMode === "chibi" ? "standard" : "chibi";
        await page.locator(selector + '[value="' + finalMode + '"]').check();
        await previews(page, route, finalMode);
        await mounted.hold(delayedMode);
        await page.locator(selector + '[value="' + delayedMode + '"]').check();
        await page.waitForFunction(() => window.previewProbeControl.pending.length > 0);
        check(true, route + "已挂起" + delayedMode + "图片加载");
        await page.locator(selector + '[value="' + finalMode + '"]').check();
        await previews(page, route, finalMode);
        await mounted.release();
        await page.waitForLoadState("networkidle");
        await previews(page, route, finalMode);
        check(true, route + "迟到的" + delayedMode + "图片不会覆盖最终模式");
      }
      check(mounted.errors.length === 0 && !mounted.calls.some((call) => call.path === "/api/capture" ||
        (call.path === "/api/queue" && call.body?.request_id)), route + "切预览无运行错误且未拍摄或报名：" + mounted.errors.join("; "));
      await mounted.context.close();
      const fallback = await mount(browser, base, route, { preference: "chibi", missingChibi: true });
      await previews(fallback.page, route, "standard");
      check(await fallback.page.locator(selector + '[value="chibi"]').isChecked() && fallback.errors.length === 0,
        route + "Q图缺失时回退普通图，仍保留Q版选择");
      await fallback.context.close();
    }
    console.log("\n" + checks + " checks passed; mocked images and APIs, no production writes.");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
