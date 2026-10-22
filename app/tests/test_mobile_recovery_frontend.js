"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");
const flow = require("../static/auto-flow.js");

const staticRoot = path.join(__dirname, "..", "static");
const token = "r7T3vW9xQ2mN5bL8kD1sF6hJ0aC4uE_z";
const photoToken = "V3f2O3CT2bl_Yt8hov2LLQ";
const recoveryUrl = "/join#restore=" + token;
const pickupUrl = "/p/" + photoToken;
const entry = { queue_id: "queue-old", display_number: "Q001", status: "queued", style_id: "pixel-person", portrait_mode: "chibi" };
const session = { queueId: entry.queue_id, session: "old-session", consentId: "capture-consent", styleId: entry.style_id,
  displayNumber: entry.display_number, status: entry.status, portraitMode: entry.portrait_mode, entry };
const styles = flow.STATIC_STYLE_FALLBACK.map((style) => ({ ...style, enabled: true }));
const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function eventually(predicate) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.fail("等待模拟请求或页面状态超时");
}
async function stored(page) {
  return page.evaluate(() => JSON.parse(sessionStorage.getItem("autoflow.queue.v1")));
}
async function visibility(page, hidden = false, pageShow = false) {
  await page.evaluate(({ hidden, pageShow }) => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    if (pageShow) window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    else document.dispatchEvent(new Event("visibilitychange"));
  }, { hidden, pageShow });
}
async function mount(browser, base, options = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const calls = [];
  const errors = [];
  const state = { entry: { ...entry, ...options.entry }, queueFailure: false, currentGets: 0, submits: 0, jobGets: 0,
    currentDelivered: 0, attachDelivered: 0, linkStatus: options.linkStatus || 200 };
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(({ localSession, copySuccess }) => {
    if (localSession && !sessionStorage.getItem("recovery-test-seeded")) {
      sessionStorage.setItem("autoflow.queue.v1", JSON.stringify(localSession));
      sessionStorage.setItem("recovery-test-seeded", "1");
    }
    window.testCopies = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: (value) => {
        window.testCopies.push(value);
        return copySuccess ? Promise.resolve() : Promise.reject(new Error("clipboard denied"));
      },
    } });
  }, { localSession: options.session, copySuccess: options.copySuccess });
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    assert.equal(url.origin, base, "测试不能访问生产或外部地址");
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const call = { path: url.pathname, method: request.method(), body: request.postDataJSON(), headers: request.headers() };
    calls.push(call);
    if (/\.(jpg|png)$/.test(call.path)) return route.fulfill({ contentType: "image/png", body: pixel });
    let status = 200;
    let payload = { ok: true };
    if (call.path === "/api/public/status") payload = { ok: true, queue_enabled: !options.featuresOff, aigc_enabled: !options.featuresOff };
    else if (call.path === "/api/styles" || call.path === "/api/aigc/styles") payload = { styles };
    else if (call.path === "/api/consent") payload = { ok: true, token: "new-consent", consent_id: "new-consent", purpose: "capture" };
    else if (call.path === "/api/recovery/current") {
      state.currentGets++;
      if (options.currentGate) await options.currentGate.promise;
      if (options.currentAbort || (options.currentAbortFirst && state.currentGets === 1)) return route.abort();
      status = options.currentStatus ?? (options.current || options.recoverLost && state.submits ? 200 : 404);
      payload = options.current || { ok: true, entry: state.entry, session: "recovered-session", consent_id: null, submission_consent_id: "capture-consent",
        recovery_url: recoveryUrl, pickup_url: options.currentPickup || null };
    } else if (call.path === "/api/recovery/attach") {
      if (options.attachGate) await options.attachGate.promise;
      if (options.attachAbort) return route.abort();
      status = options.attachStatus || 200;
      payload = { ok: true, recovery_url: options.attachUrl || recoveryUrl };
    } else if (call.path === "/api/recovery/" + token) {
      if (options.linkGate) await options.linkGate.promise;
      if (options.linkAbort) return route.abort();
      status = state.linkStatus;
      payload = { ok: true, read_only: true, entry: {
        display_number: state.entry.display_number, status: state.entry.status, style_id: state.entry.style_id,
        portrait_mode: state.entry.portrait_mode, updated_at: "2026-10-13T00:00:00Z", start_deadline: "2026-10-13T01:00:00Z",
      }, recovery_url: options.linkUrl || recoveryUrl, pickup_url: options.linkPickup || null, session: "must-ignore-session" };
    } else if (call.path === "/api/queue" && call.method === "POST") {
      state.submits++;
      state.entry = { ...state.entry, queue_id: "queue-new", display_number: "Q002", status: "queued",
        style_id: call.body.style_id, portrait_mode: call.body.portrait_mode, request_id: call.body.request_id };
      if (options.submitGate) await options.submitGate.promise;
      if (options.loseSubmit && state.submits === 1) return route.abort();
      payload = { ok: true, entry: state.entry, session: "new-session", recovery_url: recoveryUrl };
      if (options.submitWithoutRecovery) delete payload.recovery_url;
      status = 201;
    } else if (/^\/api\/queue\/[\w-]+\/start$/.test(call.path)) {
      state.entry.status = "ready";
      payload = { ok: true, entry: state.entry, gesture_enabled: true };
      status = 202;
    } else if (/^\/api\/queue\/[\w-]+$/.test(call.path)) {
      if (options.queueGate) await options.queueGate.promise;
      if (state.queueFailure) return route.abort();
      status = options.queueStatus || 200;
      payload = { ok: true, entry: { ...state.entry } };
    } else if (call.path.startsWith("/api/job/")) {
      state.jobGets++;
      if (options.jobGate) await options.jobGate.promise;
      payload = { job: { photo_token: options.jobToken || photoToken } };
    } else if (call.path === "/api/build") payload = { build: "test" };
    else { status = 404; payload = { ok: false }; }
    if (status === 503 && call.path.startsWith("/api/recovery/")) {
      payload = { ok: false, error: "recovery_unavailable", detail: "恢复服务暂不可用。" };
    }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
    if (call.path === "/api/recovery/current") state.currentDelivered++;
    if (call.path === "/api/recovery/attach") state.attachDelivered++;
  });
  await page.goto(base + (options.url || "/join"));
  if (options.url === "/stage") await page.waitForFunction(() => document.readyState === "complete");
  else await page.waitForFunction(() => window.AutoFlowEntryUi);
  return { context, page, state, calls, errors };
}
async function grant(page) {
  await page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("#af-join-submit").disabled);
}
async function submit(page) {
  await grant(page);
  await page.locator("#af-join-submit").click();
  await page.locator("#af-join-status").waitFor({ state: "visible" });
}
async function nodeChecks() {
  check(flow.recoveryPath(recoveryUrl) === recoveryUrl && flow.pickupPath(pickupUrl) === pickupUrl, "只接受32位恢复令牌与22位取图令牌");
  for (const value of ["https://evil.example" + recoveryUrl, "//evil.example" + recoveryUrl, "/join?restore=" + token,
    recoveryUrl + "&session=secret", "/join#restore=short", "/join/../join#restore=" + token + "?x"]) {
    check(flow.recoveryPath(value) === "", "拒绝不安全恢复链接 " + value);
  }
  for (const value of ["javascript:alert(1)", "https://evil.example" + pickupUrl, pickupUrl + "?x=1", pickupUrl + "#x", "/p/SM-2026-0001", "/p/photo-token"]) {
    check(flow.pickupPath(value) === "", "拒绝不安全取图链接 " + value);
  }
  const memory = new Map();
  const storage = { getItem: (key) => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value), removeItem: (key) => memory.delete(key) };
  const posts = [];
  let sequence = 0;
  const controller = flow.createSubmitController({ storage, getConsentId: () => "consent", newRequestId: () => "request-" + ++sequence,
    reuseUncertainRequest: true, request: async (endpoint, body) => {
      posts.push(body);
      return posts.length === 1 ? { status: 0 } : { status: 201, payload: { entry, session: "control", recovery_url: recoveryUrl, pickup_url: pickupUrl } };
    } });
  const input = { styleId: "pixel-person", portraitMode: "standard" };
  await controller.submit(input);
  await controller.submit(input);
  check(posts[0].request_id === posts[1].request_id && sequence === 1, "同一未确认提交重试保持同一个request_id");
  check(flow.readQueueSession(storage).recoverUrl === recoveryUrl && flow.readQueueSession(storage).pickupUrl === pickupUrl, "提交响应的恢复与取图字段持久化");
  const gate = deferred();
  let slowPosts = 0;
  const slow = flow.createSubmitController({ storage, getConsentId: () => "consent", request: async () => {
    slowPosts++;
    await gate.promise;
    return { status: 201, payload: { entry, session: "obsolete" } };
  } });
  memory.clear();
  const pending = slow.submit(input);
  await slow.submit(input);
  check(slowPosts === 1, "提交处理中重复点击不会重复请求");
  slow.reset();
  gate.resolve();
  await pending;
  check(slow.snapshot().phase === "idle" && flow.readQueueSession(storage) === null, "reset后旧提交响应不能恢复过期控制会话");
}
async function main() {
  await nodeChecks();
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const documentName = pathname === "/join" ? "join.html" : pathname === "/" ? "index.html" : pathname === "/stage" ? "stage.html" : null;
    const servedRoot = pathname.startsWith("/static/assets/") ? path.resolve(staticRoot, "..", "assets") : staticRoot;
    const file = documentName ? path.join(staticRoot, documentName) : path.resolve(servedRoot, "." + pathname.replace(/^\/static(?:\/assets)?/, ""));
    if ((!documentName && !pathname.startsWith("/static/")) || !file.startsWith(servedRoot + path.sep) || !fs.existsSync(file)) {
      response.statusCode = 404;
      return response.end();
    }
    const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg" };
    response.setHeader("Content-Type", (types[path.extname(file)] || "application/octet-stream") + "; charset=utf-8");
    response.end(documentName ? fs.readFileSync(file, "utf8").replaceAll("__BUILD__", "test") : fs.readFileSync(file));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = "http://127.0.0.1:" + server.address().port;
  let browser;
  const cases = [];
  async function open(options) {
    const mounted = await mount(browser, base, options);
    cases.push(mounted);
    return mounted;
  }
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.FRONTEND_BROWSER_CHANNEL || "chrome" });
    const normal = await open({ currentAbort: true, attachAbort: true });
    await submit(normal.page);
    check(normal.state.submits === 1 && normal.page.url() === base + "/join", "恢复服务失败仍可原页报名，保存链接非必选");
    if (process.env.MOBILE_RECOVERY_PREVIEW) {
      await normal.page.locator("#af-join-recovery-copy").waitFor({ state: "visible" });
      await normal.page.screenshot({ path: process.env.MOBILE_RECOVERY_PREVIEW, fullPage: true });
    }
    normal.state.entry.status = "called";
    await visibility(normal.page);
    await normal.page.locator("#af-join-start").waitFor({ state: "visible" });
    await normal.page.locator("#af-join-start").click();
    await eventually(() => normal.calls.some((call) => call.path.endsWith("/start")));
    check(normal.calls.find((call) => call.path.endsWith("/start")).headers["x-queue-session"] === "new-session", "原开始动作继续使用X-Queue-Session");
    for (const status of ["ready", "capturing", "processing", "ready_for_pickup"]) {
      normal.state.entry.status = status;
      await visibility(normal.page);
      await normal.page.waitForFunction((status) => window.AutoFlowEntryUi.state().queue.status === status, status);
    }
    await normal.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    check((await stored(normal.page)).pickupUrl === pickupUrl, "原页手势等待、拍摄、生成与取图流程保持，取图地址落盘");
    await normal.page.reload();
    await normal.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    check(await normal.page.locator("#af-join-pickup").getAttribute("href") === pickupUrl, "完成后刷新仍能取图");
    await normal.context.close();

    const uninterrupted = await open({ currentStatus: 503, submitWithoutRecovery: true, attachStatus: 503 });
    await uninterrupted.page.evaluate(() => {
      window.uninterruptedDocument = document;
      window.uninterruptedEvents = { visibility: 0, pageshow: 0 };
      document.addEventListener("visibilitychange", () => window.uninterruptedEvents.visibility++);
      window.addEventListener("pageshow", () => window.uninterruptedEvents.pageshow++);
    });
    await submit(uninterrupted.page);
    await eventually(() => uninterrupted.state.attachDelivered === 1);
    check(uninterrupted.state.currentDelivered === 1 && uninterrupted.state.submits === 1 &&
      !(await stored(uninterrupted.page)).recoverUrl && await uninterrupted.page.locator("#af-join-recovery-copy").isHidden(),
    "current返回503、报名不带保险链接且attach返回503仍正常报名");
    uninterrupted.state.entry.status = "called";
    await uninterrupted.page.locator("#af-join-start").waitFor({ state: "visible" });
    check(!uninterrupted.calls.some((call) => call.path.endsWith("/start")), "原页自然轮询到called，不自动开始");
    await uninterrupted.page.locator("#af-join-start").click();
    await uninterrupted.page.waitForFunction(() => window.AutoFlowEntryUi.state().queue.status === "ready");
    check((await uninterrupted.page.locator("#af-join-message").textContent()).includes("OK 手势"),
      "用户开始后原页进入ready，保留手势等待提示");
    for (const status of ["capturing", "processing", "ready_for_pickup"]) {
      const queueGets = uninterrupted.calls.filter((call) => call.path === "/api/queue/queue-new").length;
      uninterrupted.state.entry.status = status;
      await uninterrupted.page.waitForFunction((status) => window.AutoFlowEntryUi.state().queue.status === status, status);
      check(uninterrupted.calls.filter((call) => call.path === "/api/queue/queue-new").length > queueGets,
        "不刷新、不触发visibility，原页自然轮询进入" + status);
    }
    await uninterrupted.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    const uninterruptedSession = await stored(uninterrupted.page);
    check(await uninterrupted.page.locator("#af-join-pickup").getAttribute("href") === pickupUrl &&
      uninterruptedSession.pickupUrl === pickupUrl && uninterruptedSession.session === "new-session" &&
      uninterrupted.calls.find((call) => call.path === "/api/job/queue-new").headers["x-queue-session"] === "new-session",
    "保险全部失败仍经原job授权取得并保存成片链接");
    check(uninterrupted.state.submits === 1 && uninterrupted.calls.filter((call) => call.path.endsWith("/start")).length === 1 &&
      uninterrupted.calls.find((call) => call.path.endsWith("/start")).headers["x-queue-session"] === "new-session" &&
      uninterrupted.state.currentGets === 1 && uninterrupted.state.attachDelivered === 1 &&
      await uninterrupted.page.evaluate(() => window.uninterruptedDocument === document &&
        window.uninterruptedEvents.visibility === 0 && window.uninterruptedEvents.pageshow === 0) &&
      uninterrupted.page.url() === base + "/join",
    "完整原页流程仅报名与用户开始各一次，无恢复成功、跳页或生命周期事件依赖");
    await uninterrupted.context.close();

    const gate = deferred();
    const recovering = await open({ currentGate: gate, current: { ok: true, entry: { ...entry, status: "called" },
      session: "recovered-session", consent_id: "capture-consent", recovery_url: recoveryUrl, pickup_url: null } });
    await recovering.page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).click();
    await recovering.page.waitForFunction(() => document.querySelectorAll(".af-style-card").length > 0);
    check(await recovering.page.locator("#af-join-submit").isDisabled(), "恢复完成前即使已同意也禁止重复报名");
    recovering.state.entry.status = "called";
    gate.resolve();
    await recovering.page.locator("#af-join-start").waitFor({ state: "visible" });
    check((await stored(recovering.page)).session === "recovered-session" && !recovering.calls.some((call) => call.path.endsWith("/start")), "Cookie恢复控制会话但绝不自动开始");
    await recovering.context.close();

    const ready = await open({ current: { ok: true, entry: { ...entry, status: "ready_for_pickup" }, session: "recovered-session",
      consent_id: "capture-consent", recovery_url: recoveryUrl, pickup_url: null }, entry: { status: "ready_for_pickup" } });
    await ready.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    check(ready.state.jobGets > 0 && (await stored(ready.page)).pickupUrl === pickupUrl, "恢复已完成记录时也查询job并保存取图地址");
    await ready.context.close();

    for (const status of ["failed", "cancelled", "expired"]) {
      const terminalEntry = { ...entry, status };
      const expected = flow.queueStatusView(flow.normalizeEntry(terminalEntry)).title;
      for (const source of ["session", "current", "link"]) {
        const terminal = await open({ queueStatus: 404, entry: terminalEntry,
          ...(source === "session" ? { session: { ...session, status, entry: terminalEntry, recoverUrl: recoveryUrl } }
            : source === "link" ? { url: recoveryUrl, session }
              : { current: { ok: true, entry: terminalEntry, session: "recovered-session",
                submission_consent_id: "capture-consent", recovery_url: recoveryUrl, pickup_url: null } }),
        });
        await terminal.page.waitForFunction((expected) => document.querySelector("#af-join-state").textContent === expected, expected);
        const linkGets = terminal.calls.filter((call) => call.path === "/api/recovery/" + token).length;
        await visibility(terminal.page, true);
        await visibility(terminal.page, false, true);
        await visibility(terminal.page);
        await terminal.page.waitForTimeout(100);
        check(await terminal.page.locator("#af-join-state").textContent() === expected &&
          (await terminal.page.locator("#af-join-detail").textContent()).includes("重新报名") &&
          await terminal.page.locator("#af-join-reset").isVisible() &&
          !terminal.calls.some((call) => call.path.startsWith("/api/queue/")) &&
          terminal.calls.filter((call) => call.path === "/api/recovery/" + token).length === linkGets,
        source + "恢复" + status + "后pageshow与可见不重查404、不覆盖终态");
        await terminal.page.reload();
        await terminal.page.waitForFunction((expected) => document.querySelector("#af-join-state").textContent === expected, expected);
        check(!terminal.calls.some((call) => call.path.startsWith("/api/queue/") || call.method === "POST") &&
          !(await terminal.page.locator("#af-join-hint").textContent()).includes("失效"),
        source + "终态" + status + "刷新保留结果、不自动报名或开始");
        await terminal.context.close();
      }
    }

    const legacy = await open({ session, attachAbort: true, currentStatus: 200 });
    await eventually(() => legacy.calls.some((call) => call.path === "/api/recovery/attach"));
    check(!legacy.calls.some((call) => call.path === "/api/recovery/current"), "旧session优先，不用Cookie覆盖");
    legacy.state.entry.status = "called";
    await visibility(legacy.page);
    await legacy.page.locator("#af-join-start").waitFor({ state: "visible" });
    check(await legacy.page.locator("#af-join-start").isEnabled(), "attach失败不阻塞开始拍摄");
    legacy.state.queueFailure = true;
    await visibility(legacy.page);
    await eventually(() => legacy.calls.filter((call) => call.path === "/api/queue/queue-old").length >= 3);
    check((await stored(legacy.page)).session === "old-session" && !(await legacy.page.locator("#af-join-state").innerText()).includes("取消"), "网络错误不抹会话、不伪报取消");
    legacy.state.queueFailure = false;
    await visibility(legacy.page, true);
    const hiddenCount = legacy.calls.filter((call) => call.path.startsWith("/api/queue/")).length;
    await new Promise((resolve) => setTimeout(resolve, 2200));
    check(legacy.calls.filter((call) => call.path.startsWith("/api/queue/")).length === hiddenCount, "模拟锁屏后暂停队列轮询");
    legacy.state.entry.status = "processing";
    await visibility(legacy.page, false, true);
    await legacy.page.waitForFunction(() => window.AutoFlowEntryUi.state().queue.status === "processing");
    check(true, "模拟pageshow可见立即从服务同步最新状态");
    await legacy.context.close();

    const copy = await open({ session });
    await copy.page.locator("#af-join-recovery-copy").waitFor({ state: "visible" });
    await copy.page.locator("#af-join-recovery-copy").click();
    await copy.page.waitForFunction(() => document.querySelector("#af-join-recovery-details").open);
    check(await copy.page.locator("#af-join-recovery-link").inputValue() === base + recoveryUrl, "剪贴板失败可展开只读链接手动复制");
    check(await copy.page.locator("#af-join-recovery-link").getAttribute("readonly") !== null, "手动复制字段只读");
    await copy.context.close();

    const copied = await open({ session, copySuccess: true });
    await copied.page.locator("#af-join-recovery-copy").click();
    await copied.page.waitForFunction(() => document.querySelector("#af-join-recovery-message").textContent.includes("已复制"));
    check(await copied.page.evaluate(() => window.testCopies[0]) === base + recoveryUrl, "复制只包含同源hash恢复链接");
    await copied.context.close();

    const readOnly = await open({ url: recoveryUrl, session, entry: { status: "called" }, featuresOff: true });
    await readOnly.page.waitForFunction(() => document.querySelector("#af-join-state").textContent === "到号了！");
    await readOnly.page.evaluate(() => { window.AutoFlowEntryUi.startCapture(); window.AutoFlowEntryUi.submit(); });
    check(await readOnly.page.locator("#af-join-start").isHidden() && readOnly.calls.every((call) => call.method === "GET"), "链接页优先只读，不采用本机控制会话、不发POST");
    check(!readOnly.calls.some((call) => /^\/api\/(queue|job|recovery\/current)/.test(call.path)), "只读链接不访问控制队列、job或current");
    check((await readOnly.page.locator("#af-join-detail").innerText()).includes("现场确认"), "只读到号提示现场确认或原报名页继续");
    check((await stored(readOnly.page)).session === "old-session", "只读响应即使夹带session也不能写入存储");
    check(readOnly.calls.every((call) => !(call.headers.referer || "").includes(token)), "hash令牌不会出现在请求referrer");
    await readOnly.context.close();

    const transientLink = await open({ url: recoveryUrl, session, entry: { status: "called" },
      linkStatus: 503, linkPickup: pickupUrl });
    await transientLink.page.waitForFunction(() => document.querySelector("#af-join-hint").textContent.includes("自动重试"));
    check(await transientLink.page.locator("#af-join-state").textContent() === "暂时无法连接" &&
      !(await transientLink.page.locator("#af-join-detail").textContent()).includes("失效"),
    "只读链接首次503提示暂时无法连接与重试，不误判失效");
    transientLink.state.linkStatus = 200;
    await transientLink.page.waitForFunction(() => document.querySelector("#af-join-state").textContent === "到号了！");
    check(transientLink.calls.filter((call) => call.path === "/api/recovery/" + token).length >= 2 &&
      (await transientLink.page.locator("#af-join-detail").textContent()).includes("现场确认"),
    "服务恢复时定时重试找回只读状态，无刷新或visibility触发");
    transientLink.state.linkStatus = 503;
    await transientLink.page.waitForFunction(() => document.querySelector("#af-join-hint").textContent.includes("自动重试"));
    check(await transientLink.page.locator("#af-join-state").textContent() === "到号了！" &&
      !(await transientLink.page.locator("#af-join-detail").textContent()).includes("失效"),
    "已有只读状态后临时503保留进度，不误报链接失效");
    transientLink.state.entry.status = "ready_for_pickup";
    transientLink.state.linkStatus = 200;
    await transientLink.page.waitForFunction(() => document.querySelector("#af-join-state").textContent === "成片已就绪");
    await transientLink.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    check(await transientLink.page.locator("#af-join-pickup").getAttribute("href") === pickupUrl &&
      await transientLink.page.locator("#af-join-hint").textContent() === "",
    "只读503再次恢复后显示原取图链接并清除网络提示");
    check(transientLink.calls.every((call) => call.method === "GET") &&
      !transientLink.calls.some((call) => /^\/api\/(queue|job|recovery\/current)/.test(call.path)) &&
      (await stored(transientLink.page)).session === "old-session" && await transientLink.page.locator("#af-join-start").isHidden(),
    "只读503重试全程无控制请求、不采用或修改本机旧session");
    await transientLink.context.close();

    const linkReady = await open({ url: recoveryUrl, entry: { status: "ready_for_pickup" }, linkPickup: pickupUrl });
    await linkReady.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    check(await linkReady.page.locator("#af-join-pickup").getAttribute("href") === pickupUrl, "只读链接使用同源原取图页");
    check((await linkReady.page.locator("#af-join-control-note").innerText()).includes("删除或申请一次打印"), "取图权限说明保留原页面删除与一次打印能力");
    await linkReady.context.close();

    const unsafe = await open({ url: recoveryUrl, entry: { status: "ready_for_pickup" },
      linkPickup: "https://evil.example" + pickupUrl, linkUrl: "https://evil.example" + recoveryUrl });
    await unsafe.page.waitForFunction(() => document.querySelector("#af-join-state").textContent === "成片已就绪");
    check(await unsafe.page.locator("#af-join-pickup").isHidden() && await unsafe.page.locator("#af-join-recovery-copy").isHidden(), "恶意跨源复制与取图地址不展示");
    await unsafe.context.close();

    const invalid = await open({ url: recoveryUrl, linkStatus: 404, session });
    await invalid.page.waitForFunction(() => document.querySelector("#af-join-state").textContent === "拍摄链接已失效");
    await invalid.page.locator("#af-join-reset").click();
    await invalid.page.reload();
    await invalid.page.waitForFunction(() => document.querySelectorAll(".af-style-card").length > 0);
    check(invalid.page.url() === base + "/join" && !invalid.calls.some((call) => call.path === "/api/recovery/current"), "失效页主动重新报名移除hash，刷新也不强制恢复旧Cookie");
    await submit(invalid.page);
    check(invalid.state.submits === 1, "失效恢复链接可主动重新报名");
    await invalid.context.close();

    const malformed = await open({ url: "/join#restore=bad", session });
    check(await malformed.page.locator("#af-join-reset").isVisible() && !malformed.calls.some((call) => call.path.startsWith("/api/recovery/")), "格式错误链接明确失效，不降级采用本机会话");
    await malformed.context.close();

    const attachGate = deferred();
    const stale = await open({ session, attachGate });
    await eventually(() => stale.calls.some((call) => call.path === "/api/recovery/attach"));
    await stale.page.evaluate(() => window.AutoFlowEntryUi.resetFlow());
    await submit(stale.page);
    attachGate.resolve();
    await eventually(() => stale.state.attachDelivered === 1);
    await stale.page.waitForTimeout(100);
    check((await stored(stale.page)).session === "new-session", "旧attach返回不能污染reset后新提交会话");
    await stale.context.close();

    const currentGate = deferred();
    const staleCurrent = await open({ currentGate, current: { ok: true, entry,
      session: "obsolete-session", submission_consent_id: "old-consent", recovery_url: recoveryUrl, pickup_url: null } });
    await eventually(() => staleCurrent.state.currentGets > 0);
    await staleCurrent.page.evaluate(() => window.AutoFlowEntryUi.resetFlow());
    await submit(staleCurrent.page);
    currentGate.resolve();
    await eventually(() => staleCurrent.state.currentDelivered === 1);
    await staleCurrent.page.waitForTimeout(100);
    check((await stored(staleCurrent.page)).session === "new-session", "旧current响应不能覆盖主动新报名");
    await staleCurrent.context.close();

    const lost = await open({ loseSubmit: true, recoverLost: true });
    await submit(lost.page);
    check((await stored(lost.page)).session === "recovered-session" && lost.state.submits === 1, "报名响应丢失后GET current找回，未重复报名");
    check(!lost.calls.some((call) => call.path.endsWith("/start")), "响应丢失恢复也不会自动start");
    await lost.context.close();

    const retry = await open({ loseSubmit: true, currentStatus: 404 });
    await grant(retry.page);
    await retry.page.locator("#af-join-submit").click();
    await retry.page.waitForFunction(() => document.querySelector("#af-join-message").textContent.includes("无法确认"));
    await retry.page.locator("#af-join-submit").click();
    await retry.page.locator("#af-join-status").waitFor({ state: "visible" });
    const attempts = retry.calls.filter((call) => call.path === "/api/queue" && call.method === "POST");
    check(attempts.length === 2 && attempts[0].body.request_id === attempts[1].body.request_id, "current失败后的用户重试仍使用原request_id");
    await retry.context.close();

    const oldCookie = await open({ loseSubmit: true, currentAbortFirst: true, current: {
      ok: true, entry: { ...entry, request_id: "old-request" }, session: "old-cookie-session",
      consent_id: null, submission_consent_id: "old-consent", recovery_url: recoveryUrl, pickup_url: null,
    } });
    await grant(oldCookie.page);
    await oldCookie.page.locator("#af-join-submit").click();
    await oldCookie.page.waitForFunction(() => document.querySelector("#af-join-message").textContent.includes("无法确认"));
    check(await stored(oldCookie.page) === null && await oldCookie.page.locator("#af-join-form").isVisible(), "新报名网络失败时不恢复request_id不匹配的旧Cookie报名");
    for (const pageShow of [false, true]) {
      const delivered = oldCookie.state.currentDelivered;
      await visibility(oldCookie.page, false, pageShow);
      await eventually(() => oldCookie.state.currentDelivered > delivered);
      await oldCookie.page.waitForTimeout(100);
      check(await stored(oldCookie.page) === null && await oldCookie.page.locator("#af-join-form").isVisible(),
        (pageShow ? "pageshow" : "visibility") + "不绕过本次request_id去恢复旧Cookie");
    }
    await oldCookie.page.locator("#af-join-submit").click();
    await oldCookie.page.locator("#af-join-status").waitFor({ state: "visible" });
    const oldCookieAttempts = oldCookie.calls.filter((call) => call.path === "/api/queue" && call.method === "POST");
    check(oldCookieAttempts.length === 2 && oldCookieAttempts[0].body.request_id === oldCookieAttempts[1].body.request_id &&
      (await stored(oldCookie.page)).session === "new-session", "旧Cookie不吞本次重试，仍用同一个request_id完成新报名");
    await oldCookie.context.close();

    const abandon = await open({ session: { ...session, recoverUrl: recoveryUrl } });
    abandon.page.on("dialog", (dialog) => dialog.accept());
    await abandon.page.locator("#af-join-abandon").click();
    await visibility(abandon.page);
    await abandon.page.reload();
    await abandon.page.waitForFunction(() => window.AutoFlowEntryUi);
    check(!abandon.calls.some((call) => call.path === "/api/recovery/current") && await stored(abandon.page) === null, "放弃后可见和刷新不强制恢复旧报名");
    await abandon.context.close();

    for (const pageUrl of ["/", "/stage"]) {
      const onsite = await open({ url: pageUrl });
      await visibility(onsite.page, true);
      await visibility(onsite.page, false, true);
      check(!onsite.calls.some((call) => call.path.startsWith("/api/recovery/")), "现场页面不运行手机恢复生命周期 " + pageUrl);
      await onsite.context.close();
    }
    check(cases.every((testCase) => testCase.errors.length === 0), "所有场景无前端运行错误");
    console.log("\n" + checks + " checks passed; isolated local HTTP and API mocks, no production registration.");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
