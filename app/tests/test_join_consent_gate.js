"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");
const flow = require("../static/auto-flow.js");
const consent = require("../static/auto-flow-consent.js");

const staticRoot = path.join(__dirname, "..", "static");
const recoveryToken = "r7T3vW9xQ2mN5bL8kD1sF6hJ0aC4uE_z";
const recoveryUrl = "/join#restore=" + recoveryToken;
const photoToken = "V3f2O3CT2bl_Yt8hov2LLQ";
const entry = { queue_id: "queue-1", display_number: "Q001", status: "queued", style_id: "pixel-person", portrait_mode: "standard" };
const session = { queueId: entry.queue_id, session: "queue-session", consentId: "capture-consent", styleId: entry.style_id,
  displayNumber: entry.display_number, status: entry.status, portraitMode: entry.portrait_mode, entry };
const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}
function memoryStorage(initial = {}) {
  const memory = new Map(Object.entries(initial));
  return { getItem: (key) => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value), removeItem: (key) => memory.delete(key) };
}
async function logicChecks() {
  for (const value of [undefined, null, "false", 0, true, false]) {
    const built = flow.buildQueueSubmission({ requestId: "r", styleId: "pixel-person", consentId: "c", ai_watermark: value });
    check(built.body.ai_watermark === (value !== false) && typeof built.body.ai_watermark === "boolean", "标识参数严格布尔且缺省为true：" + value);
  }
  for (const value of [true, false]) {
    const storage = memoryStorage();
    const posts = [];
    const controller = flow.createSubmitController({ storage, getConsentId: () => "c", request: async (endpoint, body) => {
      posts.push(body);
      return posts.length === 1 ? { status: 400, payload: { error: { code: "INVALID_REQUEST" } } }
        : { status: 201, payload: { entry, session: "s" } };
    } });
    await controller.submit({ styleId: "pixel-person", ai_watermark: value, optionalFields: { display_name: "测试" } });
    check(posts.length === 2 && posts.every((body) => body.ai_watermark === value) && !posts[1].optional_fields,
      "去除选填字段重试仍保留顶层标识：" + value);
    check(flow.readQueueSession(storage).ai_watermark === value, "提交后标识选择持久化：" + value);
  }
  check(flow.readQueueSession(memoryStorage({ [flow.STORE_KEY]: JSON.stringify(session) })).ai_watermark === true, "旧会话缺省保留标识");
  check(flow.readQueueSession(memoryStorage({ [flow.STORE_KEY]: JSON.stringify({ ...session, entry: { ...entry, ai_watermark: false } }) })).ai_watermark === false,
    "兼容嵌套会话中的false");
  let sequence = 0;
  const requests = [];
  const controller = flow.createSubmitController({ reuseUncertainRequest: true, getConsentId: () => "c", newRequestId: () => "r" + ++sequence,
    request: async (endpoint, body) => { requests.push(body); return { status: 0 }; } });
  await controller.submit({ styleId: "pixel-person", ai_watermark: false });
  await controller.submit({ styleId: "pixel-person", ai_watermark: false });
  await controller.submit({ styleId: "pixel-person", ai_watermark: true });
  check(requests[0].request_id === requests[1].request_id && requests[1].request_id !== requests[2].request_id,
    "未确认重试复用请求编号，改变标识选择后换编号");
  const notice = consent.noticeText();
  check(notice.includes("链接自生成之日起【1】日内有效") && notice.includes("仅作为排队标识"), "告知保留用户指定的期限与信息用途原文");
  check(notice.includes("单独弹出一次同意提示") && notice.includes("第三方的名称及处理方式"), "告知保留用户指定的第三方处理原文");
  check(notice.startsWith("拍摄告知（请阅读后点击“同意”）") && notice.endsWith("同意后您将获取专属排队号码并加入排队。"), "告知标题与确认语使用用户原文");
}
async function mount(browser, base, options = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const calls = [];
  const errors = [];
  const state = { entry: { ...entry, ...options.entry }, consentFailure: options.consentFailure, current: options.current };
  page.on("pageerror", (error) => errors.push(error.message));
  if (options.session) await page.addInitScript((value) => {
    if (!sessionStorage.getItem("autoflow.queue.v1")) sessionStorage.setItem("autoflow.queue.v1", JSON.stringify(value));
  }, options.session);
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    assert.equal(url.origin, base, "测试不能访问生产或外部地址");
    const pathname = url.pathname;
    if (!pathname.startsWith("/api/")) return route.continue();
    const body = request.postDataJSON();
    calls.push({ path: pathname, body, headers: request.headers() });
    let status = 200;
    let payload = { ok: true };
    if (/\.(png|jpg)$/.test(pathname)) return route.fulfill({ contentType: "image/png", body: pixel });
    if (pathname === "/api/public/status") payload = { queue_enabled: true, aigc_enabled: true };
    else if (pathname === "/api/state") payload = { ok: true, mode: "idle", camera: {}, inference: {}, storage: {}, queue: {}, consent: {} };
    else if (pathname === "/api/styles") payload = { styles: flow.STATIC_STYLE_FALLBACK };
    else if (pathname === "/api/consent") {
      status = state.consentFailure ? 503 : 200;
      payload = state.consentFailure ? { error: { message: "同意登记暂不可用，请重试。" } } : { ok: true, token: "capture-consent", purpose: "capture" };
    } else if (pathname === "/api/recovery/current") {
      payload = state.current ? { ok: true, read_only: false, session: "queue-session", consent_id: "capture-consent", entry: state.entry,
        ...options.currentPayload } : { ok: false };
    } else if (pathname === "/api/recovery/" + recoveryToken) {
      payload = { ok: true, read_only: true, entry: state.entry, recovery_url: recoveryUrl,
        pickup_url: state.entry.status === "ready_for_pickup" ? "/p/" + photoToken : "" };
    } else if (pathname === "/api/recovery/attach") payload = { ok: true, recovery_url: recoveryUrl };
    else if (pathname === "/api/queue") {
      state.entry = { ...state.entry, style_id: body.style_id, ai_watermark: body.ai_watermark };
      status = 201;
      payload = { ok: true, entry: state.entry, session: "queue-session" };
    } else if (pathname.endsWith("/start")) {
      status = options.startConsentFailure ? 403 : 202;
      payload = options.startConsentFailure ? { error: { code: "CONSENT_REQUIRED" } } : { entry: { ...state.entry, status: "processing" } };
    } else if (pathname === "/api/queue/queue-1") payload = { entry: options.pollWithoutWatermark ? entry : state.entry };
    else if (pathname === "/api/build") payload = { build: "test" };
    else { status = 404; payload = { ok: false }; }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.goto(base + (options.url || "/join"));
  if (!options.url || options.url.startsWith("/join")) {
    await page.waitForFunction(() => window.AutoFlowEntryUi && window.AutoFlowEntryUi.styles().length > 0);
    await page.waitForFunction(() => !document.querySelector("#af-join-message").textContent.includes("正在找回"));
  }
  return { context, page, calls, errors, state };
}
async function grant(page) {
  await page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).click();
  await page.locator("#af-join-form").waitFor({ state: "visible" });
}
async function stored(page) {
  return page.evaluate(() => JSON.parse(sessionStorage.getItem("autoflow.queue.v1")));
}
async function main() {
  await logicChecks();
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const documentName = pathname === "/join" ? "join.html" : pathname === "/" ? "index.html" : pathname === "/operator" ? "operator.html" : null;
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
    const result = await mount(browser, base, options);
    cases.push(result);
    return result;
  }
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.FRONTEND_BROWSER_CHANNEL || "chrome" });
    const normal = await open();
    const { page } = normal;
    check(await page.locator("#af-join-gate").isVisible() && !await page.locator("#af-join-form").isVisible(), "首次进入只有告知门禁，表单隐藏");
    check(await page.locator("#af-join-watermark").isChecked(), "AI生成标识默认勾选");
    check((await page.locator("#af-join-watermark-note").textContent()).includes("无活动水印的下载图也会保留 AI 标识"), "说明AI来源标识独立于活动水印");
    await page.evaluate(() => window.AutoFlowEntryUi.submit());
    check(!normal.calls.some((call) => call.path === "/api/queue" || call.path === "/api/consent"), "未同意不能通过程序提交，也不自动申请同意");
    await page.locator("#af-join-consent").getByRole("button", { name: "暂不拍摄" }).click();
    check(!await page.locator("#af-join-form").isVisible(), "拒绝拍摄继续停留门禁");
    const watermarkBox = await page.locator("#af-join-watermark-note").boundingBox();
    const agreeBox = await page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).boundingBox();
    check(watermarkBox.y + watermarkBox.height <= agreeBox.y, "AI说明位于告知之后、同意按钮之前");
    const dialogTexts = [];
    page.once("dialog", async (dialog) => { dialogTexts.push(dialog.message()); await dialog.dismiss(); });
    await page.locator("#af-join-watermark").click();
    check(await page.locator("#af-join-watermark").isChecked() && dialogTexts[0].includes("主动说明"), "取消关闭确认时恢复勾选，并说明分享责任");
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#af-join-watermark").click();
    check(!await page.locator("#af-join-watermark").isChecked(), "确认关闭后取消勾选");
    await page.reload();
    await page.waitForFunction(() => window.AutoFlowEntryUi && window.AutoFlowEntryUi.styles().length > 0);
    check(await page.locator("#af-join-gate").isVisible() && !await page.locator("#af-join-form").isVisible()
      && await page.locator("#af-join-watermark").isChecked(), "未提交刷新需重新同意，标识回到默认勾选");
    await grant(page);
    check(!await page.locator("#af-join-gate").isVisible() && !normal.calls.some((call) => call.path === "/api/queue"), "同意后告知消失、表单展示，同意本身不入队");
    await page.locator("#af-join-submit").click();
    await page.locator("#af-join-status").waitFor({ state: "visible" });
    check(normal.calls.find((call) => call.path === "/api/queue").body.ai_watermark === true && (await stored(page)).ai_watermark === true,
      "默认标识在queue顶层提交并持久化true");
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#af-join-abandon").click();
    check(await page.locator("#af-join-gate").isVisible() && !await page.locator("#af-join-form").isVisible(), "放弃后重新报名回到门禁");
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#af-join-watermark").click();
    await grant(page);
    await page.locator("#af-join-submit").click();
    await page.locator("#af-join-status").waitFor({ state: "visible" });
    check((await stored(page)).ai_watermark === false && normal.calls.filter((call) => call.path === "/api/queue").at(-1).body.ai_watermark === false,
      "确认取消后顶层提交并持久化false");
    await page.reload();
    await page.locator("#af-join-status").waitFor({ state: "visible" });
    check(!await page.locator("#af-join-gate").isVisible() && !await page.locator("#af-join-form").isVisible()
      && (await stored(page)).ai_watermark === false, "排队后刷新直接恢复进度与false，不被门禁挡住");
    await page.evaluate(() => window.AutoFlowEntryUi.resetFlow());
    check(await page.locator("#af-join-gate").isVisible() && await page.locator("#af-join-watermark").isChecked(), "重新报名要求重新同意且恢复默认标识");
    for (const saved of [session, { ...session, ai_watermark: false }]) {
      const restored = await open({ session: saved, pollWithoutWatermark: true });
      await restored.page.locator("#af-join-status").waitFor({ state: "visible" });
      check(!await restored.page.locator("#af-join-gate").isVisible() && !restored.calls.some((call) => call.path === "/api/consent"), "旧本地会话直接恢复，不重复申请同意");
      await restored.page.waitForFunction(() => document.querySelector("#af-join-state").textContent === "已排队");
      check((await restored.page.evaluate(() => window.AutoFlowEntry.readQueueSession(sessionStorage))).ai_watermark === (saved.ai_watermark !== false),
        "无标识字段的轮询不会丢失本地选择");
    }
    for (const value of [undefined, false, true]) {
      const recovered = await open({ current: true, entry: value === undefined ? {} : { ai_watermark: value } });
      await recovered.page.locator("#af-join-status").waitFor({ state: "visible" });
      check(!await recovered.page.locator("#af-join-gate").isVisible() && (await stored(recovered.page)).ai_watermark === (value !== false),
        "浏览器会话恢复绕过门禁并恢复标识：" + value);
    }
    const topLevel = await open({ current: true, currentPayload: { ai_watermark: false } });
    check((await stored(topLevel.page)).ai_watermark === false, "恢复响应顶层false可持久化");
    const readOnly = await open({ url: recoveryUrl, entry: { status: "ready_for_pickup", ai_watermark: false } });
    await readOnly.page.locator("#af-join-pickup").waitFor({ state: "visible" });
    check(!await readOnly.page.locator("#af-join-gate").isVisible() && !await readOnly.page.locator("#af-join-watermark").isChecked()
      && !readOnly.calls.some((call) => call.path === "/api/consent" || call.path === "/api/queue"), "只读恢复链接直接取图，无需同意或重新入队");
    const failure = await open({ consentFailure: true });
    await failure.page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).click();
    await failure.page.waitForFunction(() => document.querySelector("#af-join-consent").textContent.includes("登记暂不可用"));
    check(await failure.page.locator("#af-join-gate").isVisible() && !await failure.page.locator("#af-join-form").isVisible(), "登记失败保留门禁和可见错误");
    failure.state.consentFailure = false;
    await grant(failure.page);
    const expired = await open({ session: { ...session, status: "called", entry: { ...entry, status: "called" } },
      entry: { status: "called" }, startConsentFailure: true });
    await expired.page.locator("#af-join-start").click();
    await expired.page.locator("#af-join-gate").waitFor({ state: "visible" });
    check(await expired.page.locator("#af-join-status").isVisible() && !await expired.page.locator("#af-join-form").isVisible(),
      "开始拍摄时授权失效可重新同意，保留原排队进度");
    await expired.page.locator("#af-join-consent").getByRole("button", { name: "同意", exact: true }).click();
    await expired.page.locator("#af-join-gate").waitFor({ state: "hidden" });
    const shared = await page.evaluate(() => {
      const container = document.createElement("div");
      document.body.appendChild(container);
      window.AutoFlowConsent.mountConsentPanel(container, { request: async () => ({ status: 200, payload: { ok: true, token: "shared" } }) });
      const result = { buttons: [...container.querySelectorAll("button")].map((button) => button.textContent),
        watermark: !!container.querySelector("input[type=checkbox]"), hidden: container.hidden };
      container.remove();
      return result;
    });
    check(shared.buttons.join(",") === "同意,暂不拍摄" && !shared.watermark && !shared.hidden, "共享告知默认保持原有两按钮，不注入手机标识或门禁");
    const index = await open({ url: "/" });
    const indexNotice = await index.page.evaluate(() => {
      const panel = document.querySelector("#legal .legal");
      const notice = window.AutoFlowConsent.NOTICE;
      const expected = [notice.title, notice.introduction, ...notice.lines, notice.contact, notice.confirmation];
      return { actual: [...panel.querySelectorAll("h2, p, li")].map((node) => node.textContent), expected,
        sharedLines: [...document.querySelectorAll("#af-consent li")].map((node) => node.textContent),
        titleId: panel.querySelector("h2").id, buttons: panel.querySelectorAll("[data-legal-action]").length };
    });
    check(JSON.stringify(indexNotice.actual) === JSON.stringify(indexNotice.expected)
      && JSON.stringify(indexNotice.sharedLines) === JSON.stringify(consent.noticeLines()), "首页静态弹层与动态告知均使用共享真实文案");
    check(indexNotice.titleId === "legal-title" && indexNotice.buttons === 2, "同步首页文案保留弹层可访问标题和原操作按钮");
    await index.page.locator("#open-legal").click();
    await index.page.locator("#legal [data-legal-action=decline]").click();
    check(await index.page.locator("#legal").getAttribute("aria-hidden") === "true", "同步文案后首页原告知事件仍有效");
    const operator = await open({ url: "/operator" });
    check(await operator.page.locator("#operator-notice").textContent() === consent.noticeText(), "operator 告知全文与 shared 源逐字一致");
    await page.evaluate(() => window.AutoFlowEntryUi.resetFlow());
    for (const width of [320, 390, 768]) {
      await page.setViewportSize({ width, height: 844 });
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "门禁无横向溢出：" + width);
    }
    if (process.env.JOIN_GATE_PREVIEW) {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: process.env.JOIN_GATE_PREVIEW, fullPage: true });
    }
    check(cases.every((item) => item.errors.length === 0), "所有浏览器场景无脚本异常");
    console.log("PASS " + checks + " checks");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
