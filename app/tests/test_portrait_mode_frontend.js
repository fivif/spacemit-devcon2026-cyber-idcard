"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");
const flow = require("../static/auto-flow.js");

const staticRoot = path.join(__dirname, "..", "static");
const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");
const styles = flow.STATIC_STYLE_FALLBACK.map((style, index) => ({ ...style,
  title: style.id === "pixel-person" ? "Q版像素风" : style.title,
  description: style.id === "pixel-person" ? "全身小人，大头小身体" : style.description,
  enabled: true, is_default: index === 0, default_orientation: "vertical", history: [],
}));
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}
async function mount(browser, base, route = "/join", options = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: options.reducedMotion || "reduce" });
  const page = await context.newPage();
  const calls = [];
  const errors = [];
  let entry = options.entry || null;
  let queueAttempts = 0;
  let captureAttempts = 0;
  let queueGets = 0;
  let releaseQueueResponse = null;
  let staleQueueDelivered = false;
  let job = null;
  page.on("pageerror", (error) => errors.push(error.message));
  if (options.session || options.preference) await page.addInitScript(({ session, preference }) => {
    if (session && !sessionStorage.getItem("autoflow.queue.v1")) sessionStorage.setItem("autoflow.queue.v1", JSON.stringify(session));
    if (preference) localStorage.setItem("booth.portrait_mode.v1", preference);
  }, { session: options.session, preference: options.preference });
  await context.route("**/api/**", async (request) => {
    const pathname = new URL(request.request().url()).pathname;
    const method = request.request().method();
    const body = request.request().postDataJSON();
    calls.push({ path: pathname, method, body, observedAt: Date.now() });
    if (/\.(jpg|png|mjpg)$/.test(pathname)) return request.fulfill({ contentType: "image/png", body: pixel });
    let status = 200;
    let payload = { ok: true };
    if (pathname === "/api/build") payload = { build: "test" };
    else if (pathname === "/api/public/status") payload = {
      ok: true, queue_enabled: options.queueEnabled !== false, aigc_enabled: options.aigcEnabled !== false,
    };
    else if (pathname === "/api/state") payload = {
      aigc_enabled: true, camera: { ok: true, real: true, width: 1920, height: 1080, fps: 30 },
      infer: { running: true, preloaded: true }, styles, default_style: "pixel-person",
      disk: { free_gb: 10, total_gb: 32 }, photos: 1, counters: {},
      last_record: route === "/stage" ? "SM-2026-0001" : null,
      recent: [{ id: "SM-2026-0001", token: "photo-token", thumb: "/api/thumb/photo-token.jpg", portrait_mode: options.photoMode }],
    };
    else if (pathname === "/api/styles" || pathname === "/api/aigc/styles" || pathname === "/api/operator/aigc/styles") payload = { styles: options.styles || styles };
    else if (pathname === "/api/consent") payload = { ok: true, token: "consent-token", consent_id: "consent-token", purpose: "capture" };
    else if (pathname === "/api/operator/queue") payload = { active: 1, unlimited: true,
      entries: [{ queue_id: "queue-1", status: "queued", style_id: "pixel-person", portrait_mode: options.photoMode }] };
    else if (pathname === "/api/queue" && body && body.request_id) {
      queueAttempts++;
      if ((options.optionalRetry || options.networkRetry || options.consentRetry) && queueAttempts === 1) {
        status = options.optionalRetry ? 400 : options.consentRetry ? 403 : 503;
        payload = { error: { code: options.consentRetry ? "CONSENT_REQUIRED" : options.optionalRetry ? "INVALID_REQUEST" : "INTERNAL_ERROR" } };
      } else {
        entry = { queue_id: "queue-1", status: "queued", display_number: "Q001", style_id: body.style_id,
          portrait_mode: body.portrait_mode, request_id: body.request_id };
        status = 201;
        payload = { ok: true, entry, session: "queue-session" };
      }
    } else if (pathname === "/api/queue") {
      status = 400;
      payload = { error: { code: "INVALID_REQUEST" } };
    } else if (pathname === "/api/queue/queue-1/start") {
      entry = { ...entry, status: "processing" };
      status = 202;
      payload = { entry, gesture_enabled: false };
    } else if (pathname === "/api/queue/queue-1") {
      queueGets++;
      if (options.queueStartRace && queueGets === 2) {
        await new Promise((resolve) => { releaseQueueResponse = resolve; });
        await request.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify({ entry: { ...entry, status: "cancelled" } }) });
        staleQueueDelivered = true;
        return;
      }
      payload = { entry };
    }
    else if (pathname === "/api/capture") {
      captureAttempts++;
      if (options.captureRetry && captureAttempts === 1) { status = 403; payload = { detail: "请重新确认拍摄告知" }; }
      else {
        job = { ok: true, stage: "done", job: "job-1", record_id: "SM-2026-0001", token: "photo-token",
          portrait_mode: body.portrait_mode, steps: [], person_count: 1,
          capture_preview_url: "/api/capture-preview/photo-token.jpg" };
        payload = job;
      }
    } else if (pathname.startsWith("/api/job/")) payload = job;
    else if (pathname.startsWith("/api/photo-meta/")) payload = { ok: true, id: "SM-2026-0001",
      style: "pixel-person", portrait_mode: options.photoMode, print_available: false };
    else if (pathname === "/api/overlay.json") payload = { record_id: "SM-2026-0001", persons: [], person_count: 1 };
    await request.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.goto(base + route);
  if (route === "/join") await page.waitForFunction((count) => document.querySelectorAll(".af-style-card").length === count, styles.length);
  if (route === "/") await page.waitForFunction((count) => document.querySelector("#af-style").options.length === count, styles.length);
  return { context, page, calls, errors,
    hasPendingQueueResponse: () => Boolean(releaseQueueResponse),
    releaseQueueResponse: () => releaseQueueResponse(),
    staleQueueDelivered: () => staleQueueDelivered,
  };
}
async function consent(page, selector = "#af-join-consent") {
  await page.locator(selector).getByRole("button", { name: "同意", exact: true }).click();
}
async function submitJoin(page) {
  await consent(page);
  await page.locator("#af-join-submit").click();
  await page.locator("#af-join-status").waitFor({ state: "visible" });
}
function submissions(calls) { return calls.filter((call) => call.path === "/api/queue" && call.body?.request_id); }
async function main() {
  check(flow.buildQueueSubmission({ requestId: "r", styleId: "pixel-person", consentId: "c" }).body.portrait_mode === "standard", "缺省提交为非Q版");
  check(flow.normalizeEntry({ queue_id: "q", status: "queued" }).portraitMode === "standard", "旧排队记录默认为非Q版");
  const customPixel = { ...styles.find((style) => style.id === "pixel-person"), title: "我的像素画风", description: "自定义霓虹配色与侧光",
    model: "gpt-image-1", sizes: ["1024x1536"], backdrop_id: "dynamic-minimal",
    wide_prompt: "Preserve the single person and original clothes in a clean portrait.",
    vertical_prompt: "Preserve the single person and original clothes in a clean portrait.", negative_prompt: "No extra people, generated text or borders." };
  const normalizedCustom = flow.normalizeStyleList([customPixel])[0];
  check(normalizedCustom.title === customPixel.title && normalizedCustom.description === customPixel.description, "报名目录保留自定义像素名称与说明");
  check(flow.normalizeStyleList([{ ...customPixel, title: "Q版像素风（自定义）" }])[0].title === "Q版像素风（自定义）", "像素标题仅精确匹配旧名称");
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const documentName = pathname === "/" ? "index.html" : pathname === "/join" ? "join.html"
      : pathname.startsWith("/operator") ? "operator.html" : pathname.startsWith("/p/") ? "pick.html" : pathname === "/stage" ? "stage.html" : null;
    const file = documentName ? path.join(staticRoot, documentName) : path.resolve(staticRoot, "." + pathname.replace(/^\/static/, ""));
    if ((!documentName && !pathname.startsWith("/static/")) || !file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)) {
      response.statusCode = 404;
      return response.end();
    }
    const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".png": "image/png" };
    response.setHeader("Content-Type", (types[path.extname(file)] || "application/octet-stream") + "; charset=utf-8");
    response.end(documentName ? fs.readFileSync(file, "utf8").replaceAll("__BUILD__", "test") : fs.readFileSync(file));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = "http://127.0.0.1:" + server.address().port;
  let browser;
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.FRONTEND_BROWSER_CHANNEL || "chrome" });
    {
      const { context, page, calls, errors } = await mount(browser, base, "/join");
      await page.locator("#af-join-form").waitFor({ state: "visible" });
      check(calls.some((call) => call.path === "/api/public/status") &&
        !calls.some((call) => call.path === "/api/state"), "手机报名只读取精简公开状态");
      check(!calls.some((call) => call.path === "/api/queue" && !call.body?.request_id),
        "手机报名不再用空请求探测队列");
      check(errors.length === 0, "精简公开状态初始化无运行错误");
      await context.close();
    }
    {
      const { context, calls, errors } = await mount(browser, base, "/p/photo-token/");
      check(calls.some((call) => call.path === "/api/photo-meta/photo-token.json") &&
        !calls.some((call) => call.path === "/api/photo-meta/.json"), "取图页尾斜杠仍解析正确令牌");
      check(errors.length === 0, "取图页尾斜杠无运行错误");
      await context.close();
    }
    for (const style of styles) for (const mode of ["standard", "chibi"]) {
      const { context, page, calls, errors } = await mount(browser, base);
      await page.locator('[data-style-id="' + style.id + '"]').click();
      check(await page.locator('[name="join_portrait_mode"][value="chibi"]').isChecked(), style.id + "手机页默认Q版");
      await page.locator('[name="join_portrait_mode"][value="' + mode + '"]').check();
      await submitJoin(page);
      const body = submissions(calls)[0].body;
      check(body.style_id === style.id && body.portrait_mode === mode && !body.optional_fields, style.id + "顶层提交" + mode);
      check((await page.locator("#af-join-detail").textContent()).includes(flow.portraitModeLabel(mode)), "排队显示" + mode);
      check(errors.length === 0, "报名页无运行错误");
      await context.close();
    }
    {
      const { context, page } = await mount(browser, base);
      check((await page.locator('[data-style-id="pixel-person"]').textContent()).includes("复古像素头像") &&
        !(await page.locator("#af-join-form").textContent()).includes("Q版像素风"), "旧目录的像素标题与小人说明已修正");
      check((await page.locator("#af-join-form").textContent()).includes("图片仅示意画风"), "旧预览不承诺人像模式");
      for (const styleId of ["pixel-person"]) {
        const image = page.locator('[data-style-id="' + styleId + '"] img');
        await image.waitFor({ state: "attached" });
        check(new URL(await image.getAttribute("src"), base).searchParams.get("v") === "test", "手机预览携带BUILD：" + styleId);
      }
      await page.locator('[name="join_portrait_mode"][value="chibi"]').check();
      await page.locator('[data-style-id="american-comic"]').click();
      check(await page.locator('[name="join_portrait_mode"][value="chibi"]').isChecked(), "切换画风保持Q版模式");
      await page.reload();
      await page.locator("#af-join-portrait-mode").waitFor({ state: "visible" });
      check(await page.locator('[name="join_portrait_mode"][value="chibi"]').isChecked(), "刷新从localStorage恢复模式");
      if (process.env.FRONTEND_SCREENSHOT_PATH) await page.screenshot({ path: process.env.FRONTEND_SCREENSHOT_PATH, fullPage: true });
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "390px页面不横向溢出");
      check(await page.locator(".portrait-mode-options label").first().evaluate((node) => node.getBoundingClientRect().height >= 48), "手机模式选项触控高度至少48px");
      await page.locator('[name="join_portrait_mode"][value="standard"]').focus();
      await page.keyboard.press("ArrowRight");
      check(await page.locator('[name="join_portrait_mode"][value="chibi"]').isChecked(), "键盘方向键切换模式");
      await submitJoin(page);
      await page.reload();
      await page.locator("#af-join-status").waitFor({ state: "visible" });
      check((await page.locator("#af-join-detail").textContent()).includes("Q版大头像"), "已报名刷新恢复队列模式");
      check(await page.evaluate(() => JSON.parse(sessionStorage.getItem("autoflow.queue.v1")).portraitMode === "chibi" &&
        !Object.keys(localStorage).some((key) => /queue|consent/.test(key))), "模式保存且排队凭据未写入localStorage");
      await context.close();
    }
    for (const failure of ["optionalRetry", "networkRetry", "consentRetry"]) {
      const { context, page, calls } = await mount(browser, base, "/join", { [failure]: true });
      await page.locator('[name="join_portrait_mode"][value="chibi"]').check();
      if (failure === "optionalRetry") { await page.locator("#af-join-optional").evaluate((node) => { node.open = true; }); await page.locator("#af-join-name").fill("测试昵称"); }
      await consent(page);
      await page.locator("#af-join-submit").click();
      if (failure !== "optionalRetry") {
        await page.waitForFunction(() => ["failed", "needs_consent"].includes(window.AutoFlowEntryUi.state().controller.phase));
        if (failure === "consentRetry") await consent(page);
        await page.locator("#af-join-submit").click();
      }
      await page.locator("#af-join-status").waitFor({ state: "visible" });
      const bodies = submissions(calls).map((call) => call.body);
      check(bodies.length === 2 && bodies.every((body) => body.portrait_mode === "chibi"), failure + "保持模式");
      if (failure === "optionalRetry") check(bodies[0].request_id === bodies[1].request_id && bodies[0].optional_fields && !bodies[1].optional_fields, "自动去除选填字段仅重试一次且保持幂等编号");
      await context.close();
    }
    {
      const entry = { queue_id: "queue-1", status: "queued", style_id: "pixel-person", display_number: "Q001" };
      const { context, page } = await mount(browser, base, "/join", { entry, preference: "chibi", session: {
        queueId: "queue-1", session: "queue-session", styleId: "pixel-person", status: "queued", entry,
      } });
      check((await page.locator("#af-join-detail").textContent()).includes("写实版"), "旧队列恢复不被新的Q版偏好覆盖");
      await context.close();
    }
    for (const route of ["/join", "/"]) {
      const entry = { queue_id: "queue-1", status: "called", style_id: "pixel-person", display_number: "Q001" };
      const mounted = await mount(browser, base, route, { entry, queueStartRace: true, session: {
        queueId: "queue-1", session: "queue-session", consentId: "consent-token",
        styleId: "pixel-person", status: "called", entry,
      } });
      const { context, page, calls, errors } = mounted;
      const titleSelector = route === "/join" ? "#af-join-state" : "#af-queue-status";
      const startSelector = route === "/join" ? "#af-join-start" : "#af-start";
      while (!mounted.hasPendingQueueResponse()) await page.waitForTimeout(50);
      await page.evaluate(async () => {
        await Promise.all([window.AutoFlowEntryUi.startCapture(), window.AutoFlowEntryUi.startCapture()]);
      });
      await page.waitForFunction((selector) => document.querySelector(selector).textContent === "拍摄已完成", titleSelector);
      check(calls.filter((call) => call.path.endsWith("/start")).length === 1, route + "开始拍摄去重且即时显示返回状态");
      mounted.releaseQueueResponse();
      while (!mounted.staleQueueDelivered()) await page.waitForTimeout(50);
      await page.waitForTimeout(100);
      check(await page.locator(titleSelector).textContent() === "拍摄已完成" &&
        !(await page.locator(startSelector).isVisible()), route + "迟到cancelled不会覆盖正常拍摄");
      check(await page.evaluate(() => JSON.parse(sessionStorage.getItem("autoflow.queue.v1")).status === "processing"),
        route + "当前状态保存到会话");
      await page.reload();
      await page.waitForFunction((selector) => document.querySelector(selector).textContent === "拍摄已完成", titleSelector);
      check(await page.locator(titleSelector).textContent() === "拍摄已完成", route + "刷新恢复拍摄状态");
      for (const status of ["failed", "cancelled"]) {
        if (status === "failed") {
          await page.route("**/api/queue/queue-1", (request) => request.abort());
          const hintSelector = route === "/join" ? "#af-join-hint" : "#af-queue-hint";
          await page.waitForFunction((selector) => document.querySelector(selector).textContent.includes("网络连接不稳定"), hintSelector);
        }
        await page.route("**/api/queue/queue-1", (request) => request.fulfill({ contentType: "application/json",
          body: JSON.stringify({ entry: { ...entry, status } }) }));
        await page.evaluate(() => {
          const stored = JSON.parse(sessionStorage.getItem("autoflow.queue.v1"));
          stored.status = "processing";
          stored.entry.status = "processing";
          sessionStorage.setItem("autoflow.queue.v1", JSON.stringify(stored));
        });
        if (status !== "failed") await page.reload();
        const expected = status === "failed" ? "生成未完成" : "排队已取消";
        await page.waitForFunction(({ selector, expected }) => document.querySelector(selector).textContent === expected,
          { selector: titleSelector, expected });
        check(await page.locator(titleSelector).textContent() === expected, route + "真实" + status + "如实呈现");
        if (status === "failed") check(!(await page.locator(route === "/join" ? "#af-join-status" : "#af-queue").textContent()).includes("网络连接不稳定"),
          route + "真实failed清除上一轮网络提示");
        const terminalQueueGets = calls.filter((call) => call.path === "/api/queue/queue-1").length;
        let missingGets = 0;
        await page.route("**/api/queue/queue-1", (request) => {
          missingGets++;
          return request.fulfill({ status: 404, contentType: "application/json",
            body: JSON.stringify({ error: { code: "QUEUE_NOT_FOUND" } }) });
        });
        await page.waitForTimeout(2200);
        check(missingGets === 0 && calls.filter((call) => call.path === "/api/queue/queue-1").length === terminalQueueGets,
          route + "收到" + status + "后停止轮询，不再请求被清理记录");
        await page.reload();
        const detailSelector = route === "/join" ? "#af-join-detail" : "#af-queue-hint";
        const hintText = await page.locator(route === "/join" ? "#af-join-hint" : "#af-queue-hint").textContent();
        check(await page.locator(titleSelector).textContent() === expected &&
          (await page.locator(detailSelector).textContent()).includes("重新报名") &&
          !/不存在|不可用|访问已失效|服务可能已重启/.test(hintText) && missingGets === 0,
          route + "刷新保留" + status + "与重试说明，不叠加缺失提示");
        check(await page.locator(route === "/join" ? "#af-join-reset" : "#af-reset").isVisible() &&
          !(await page.locator(startSelector).isVisible()) &&
          !(await page.locator(route === "/join" ? "#af-join-abandon" : "#af-abandon").isVisible()) &&
          submissions(calls).length === 0, route + "终态仅提供手动重新报名，无自动报名");
      }
      check(errors.length === 0, route + "开始与迟到响应无运行错误");
      await context.close();
    }
    for (const route of ["/join", "/"]) {
      for (const status of ["queued", "called", "processing"]) {
        const entry = { queue_id: "queue-1", status, style_id: "pixel-person", display_number: "Q001" };
        const { context, page, calls, errors } = await mount(browser, base, route, { entry, session: {
          queueId: "queue-1", session: "queue-session", consentId: "consent-token",
          styleId: "pixel-person", status, entry,
        } });
        let missingGets = 0;
        await page.route("**/api/queue/queue-1", (request) => {
          missingGets++;
          return request.fulfill({ status: 404, contentType: "application/json",
            body: JSON.stringify({ error: { code: "QUEUE_NOT_FOUND" } }) });
        });
        await page.reload();
        const titleSelector = route === "/join" ? "#af-join-state" : "#af-queue-status";
        await page.waitForFunction((selector) => document.querySelector(selector).textContent === "排队访问已失效", titleSelector);
        const cardText = await page.locator(route === "/join" ? "#af-join-status" : "#af-queue").textContent();
        check(cardText.includes("本页已无法访问这次排队记录") && !/拍摄中断|排队已取消|服务可能已重启|记录不存在|正在生成|到号了|已排队/.test(cardText),
          route + "真实404替换" + status + "标题及说明，不猜测失败或重启");
        check(await page.locator(route === "/join" ? "#af-join-reset" : "#af-reset").isVisible() &&
          !(await page.locator(route === "/join" ? "#af-join-start" : "#af-start").isVisible()) &&
          !(await page.locator(route === "/join" ? "#af-join-abandon" : "#af-abandon").isVisible()) &&
          missingGets === 1, route + "缺失记录只处理一次并移除旧操作");
        await page.locator(route === "/join" ? "#af-join-reset" : "#af-reset").click();
        check(await page.evaluate(() => sessionStorage.getItem("autoflow.queue.v1") === null) &&
          submissions(calls).length === 0 && errors.length === 0, route + "缺失记录手动重置不自动报名");
        await context.close();
      }
    }
    for (const mode of ["standard", "chibi"]) {
      const { context, page, calls, errors } = await mount(browser, base, "/");
      await page.locator("#af-inline").evaluate((node) => { node.open = true; });
      await page.locator('[name="capture_portrait_mode"][value="' + mode + '"]').check();
      check(await page.locator('[name="queue_portrait_mode"][value="' + mode + '"]').isChecked(), "现场两入口同步" + mode);
      await page.locator("#af-style").selectOption("hand-doodle");
      await consent(page, "#af-consent");
      await page.locator("#af-submit").click();
      await page.locator("#af-queue").waitFor({ state: "visible" });
      check(submissions(calls)[0].body.portrait_mode === mode, "屏幕报名提交" + mode);
      check((await page.locator("#af-queue-hint").textContent()).includes(flow.portraitModeLabel(mode)), "屏幕排队显示" + mode);
      check(errors.length === 0, "屏幕报名无运行错误");
      await context.close();
    }
    for (const mode of ["standard", "chibi"]) {
      const { context, page, calls, errors } = await mount(browser, base, "/", {
        captureRetry: mode === "chibi", reducedMotion: mode === "standard" ? "no-preference" : "reduce",
      });
      await page.waitForFunction((count) => document.querySelectorAll("#styles .style-card").length === count, styles.length);
      if (mode === "standard") check(await page.locator("#styles .style-card img").evaluateAll((images) =>
        images.length === window.AutoFlowEntry.STATIC_STYLE_FALLBACK.length && images.every((image) => new URL(image.src).searchParams.get("v") === window.Booth.BUILD)), "现场风格预览携带BUILD");
      await page.locator('[name="capture_portrait_mode"][value="' + mode + '"]').check();
      await page.locator(".switchline .track").click();
      await page.locator('[data-legal-action="agree"]').click();
      await page.waitForFunction(() => !document.querySelector("#fire").disabled && document.querySelector("#hint").textContent.includes("8 秒"));
      check((await page.locator("#hint").textContent()).includes("8 秒"), "拍摄提示与8秒倒计时一致：" + mode);
      const countdownStartedAt = Date.now();
      await page.locator("#fire").click();
      check(await page.locator("#countdown").textContent() === "8" &&
        !calls.some((call) => call.path === "/api/capture"), "拍摄前显示8秒并等待：" + mode);
      await page.locator("#result.is-open").waitFor({ state: "visible", timeout: 15000 });
      const captures = calls.filter((call) => call.path === "/api/capture");
      check(captures[0].observedAt - countdownStartedAt >= 7900, "完整8秒等待，减少动态不跳过：" + mode);
      check(captures.length === (mode === "chibi" ? 2 : 1) && captures.every((call) => call.body.portrait_mode === mode), "直接拍摄及授权重试提交" + mode);
      check((await page.locator("#result-portrait-mode").textContent()) === flow.portraitModeLabel(mode), "直接成片显示" + mode);
      check((await page.locator("#preview").getAttribute("src")).includes("capture-preview"), "成片仍保持抓帧定格");
      check(errors.length === 0, "直接拍摄无运行错误");
      await context.close();
    }
    for (const photoMode of [undefined, "chibi"]) {
      for (const route of ["/p/photo-token", "/operator/queue", "/operator/photos", "/stage"]) {
        const { context, page, errors } = await mount(browser, base, route, { photoMode });
        const selector = route.startsWith("/p/") ? "#photo-portrait-mode" : route.endsWith("queue") ? "#queue-admin-list" : route === "/stage" ? "#badge-now" : "#shots";
        await page.waitForFunction(({ selector, label }) => document.querySelector(selector)?.textContent.includes(label), { selector, label: flow.portraitModeLabel(photoMode) });
        check((await page.locator(selector).textContent()).includes(flow.portraitModeLabel(photoMode)), route + "显示" + (photoMode || "旧照片非Q版"));
        check(errors.length === 0, route + "无运行错误");
        await context.close();
      }
    }
    {
      const { context, page } = await mount(browser, base, "/operator/styles");
      await page.waitForFunction(() => document.querySelector("#aigc-styles").textContent.includes("复古像素头像"));
      check(!(await page.locator("#aigc-styles").textContent()).includes("全身小人"), "后台画风列表与模式说明分离");
      await context.close();
    }
    {
      const customStyles = [customPixel, ...styles.filter((style) => style.id !== "pixel-person")];
      const { context, page, calls, errors } = await mount(browser, base, "/operator/styles", { styles: customStyles });
      const row = page.locator(".style-row").filter({ hasText: customPixel.title });
      await row.waitFor();
      check((await row.textContent()).includes(customPixel.title), "后台列表保留自定义像素名称");
      check(await page.evaluate((style) => {
        const exact = window.Booth.styleDisplay({ ...style, title: "Q版像素风" });
        const suffix = window.Booth.styleDisplay({ ...style, title: "Q版像素风（自定义）" });
        const legacy = window.Booth.styleDisplay({ ...style, description: "大头小身体的软萌像素小人，保留原衣服。" });
        return exact.title === "复古像素头像" && suffix.title === "Q版像素风（自定义）" &&
          exact.description === style.description && legacy.description === "像素色块与清晰轮廓，保留原衣服。";
      }, customPixel), "共享显示规则仅替换旧精确标题与旧小人措辞");
      await page.locator("#style-search").fill(customPixel.title);
      check(await page.locator(".style-row").count() === 1, "后台可搜索自定义像素名称");
      await row.getByRole("button", { name: "编辑", exact: true }).click();
      check(await page.getByLabel("风格名称", { exact: true }).inputValue() === customPixel.title &&
        await page.getByLabel("给参与者的简短说明", { exact: true }).inputValue() === customPixel.description, "编辑保留自定义像素字段");
      await page.getByRole("button", { name: "保存风格", exact: true }).click();
      await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
      check(calls.some((call) => call.path.endsWith("/pixel-person") && call.body.title === customPixel.title &&
        call.body.description === customPixel.description), "编辑保存不覆盖自定义名称与说明");
      await row.getByText("更多", { exact: true }).click();
      await row.getByRole("button", { name: "复制为新风格" }).click();
      check(await page.getByLabel("风格名称", { exact: true }).inputValue() === customPixel.title + "（副本）" &&
        await page.getByLabel("给参与者的简短说明", { exact: true }).inputValue() === customPixel.description, "复制保留自定义名称与说明");
      check(errors.length === 0, "自定义像素管理无运行错误");
      await context.close();
      const joined = await mount(browser, base, "/join", { styles: customStyles });
      check((await joined.page.locator('[data-style-id="pixel-person"]').textContent()).includes(customPixel.title) &&
        (await joined.page.locator("#af-join-style-choice").textContent()).includes(customPixel.description), "报名展示后台自定义像素文案");
      await joined.context.close();
    }
    console.log("\n" + checks + " checks passed; all API calls intercepted, no production writes.");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
