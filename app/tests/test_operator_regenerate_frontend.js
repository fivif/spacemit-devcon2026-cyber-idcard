"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");
const { NOTICE } = require("../static/auto-flow-consent.js");

const staticRoot = path.join(__dirname, "..", "static");
const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");
const photo = { id: "SM-2026-0001", token: "source-token-1234567890", style: "style-two",
  portrait_mode: "chibi", degraded: true, ai_generated: false, regenerate_available: true, thumb: "/api/thumb/source.jpg" };
const styles = [
  { id: "style-one", title: "风格一", enabled: true, is_default: true },
  { id: "style-two", title: "风格二", enabled: true },
  { id: "style-off", title: "已停用", enabled: false },
];
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}

async function mount(browser, base, options = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const calls = [];
  const errors = [];
  let job = { stage: "stylizing" };
  let failure = options.failure || 0;
  let pollFailure = 0;
  let stylesFailure = Boolean(options.stylesFailure);
  let recent = options.recent || [photo];
  let release = () => {};
  let hold = Boolean(options.hold);
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const method = request.method();
    const multipart = (request.headers()["content-type"] || "").includes("multipart/form-data");
    const payload = method === "POST" ? multipart ? request.postDataBuffer().toString("utf8") : request.postDataJSON() : null;
    calls.push({ path: pathname, method, payload });
    if (pathname.endsWith(".jpg")) return route.fulfill({ contentType: "image/png", body: pixel });
    let status = 200;
    let result = { ok: true };
    if (pathname === "/api/state") result = { recent, camera: {}, infer: {}, build: "test" };
    else if (pathname === "/api/build") result = { build: "test" };
    else if (pathname === "/api/operator/aigc/styles") {
      status = stylesFailure ? 503 : 200;
      result = stylesFailure ? { detail: "读取中断" } : { styles };
    } else if (pathname === "/api/operator/photos/regenerate") {
      if (hold) await new Promise((resolve) => { release = resolve; });
      status = failure || 202;
      result = failure ? { detail: "已有任务处理中" } : options.missingJob ? { ok: true } :
        { ok: true, job: options.stringJob ? "regenerate-job" : { job: "regenerate-job", stage: "queued" } };
    } else if (pathname === "/api/operator/upload") result = { ok: true, job: "upload-job" };
    else if (pathname === "/api/job/upload-job") result = { stage: "done" };
    else if (pathname === "/api/job/regenerate-job") {
      status = pollFailure || 200;
      result = pollFailure ? { detail: "状态暂不可用" } : job;
    }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(result) });
  });
  await page.goto(base + "/operator/photos");
  await page.waitForFunction(() => document.querySelector("#badge-ep").textContent !== "等待加载");
  return { context, page, calls, errors,
    setJob: (value) => { job = value; },
    setRecent: (value) => { recent = value; },
    failPoll: (value) => { pollFailure = value; },
    recover: () => { failure = 0; stylesFailure = false; },
    release: () => { hold = false; release(); },
  };
}

async function open(runtime) {
  await runtime.page.locator("[data-regenerate]").first().click();
  await runtime.page.locator("#regenerate-panel").waitFor();
}
async function submit(runtime) {
  await runtime.page.locator("#regenerate-submit").click();
}
async function badge(runtime, value) {
  await runtime.page.waitForFunction((text) => document.querySelector("#regenerate-badge").textContent === text, value);
}
function submits(runtime) {
  return runtime.calls.filter((call) => call.path === "/api/operator/photos/regenerate");
}

async function main() {
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const filename = pathname.startsWith("/operator") ? path.join(staticRoot, "operator.html") :
      path.resolve(staticRoot, "." + pathname.slice("/static".length));
    if (!filename.startsWith(staticRoot + path.sep) || !fs.existsSync(filename)) {
      response.statusCode = 404;
      return response.end();
    }
    response.setHeader("Content-Type", filename.endsWith(".html") ? "text/html; charset=utf-8" :
      filename.endsWith(".js") ? "application/javascript" : filename.endsWith(".css") ? "text/css" : "image/svg+xml");
    response.end(filename.endsWith(".html") ? fs.readFileSync(filename, "utf8").replaceAll("__BUILD__", "test") : fs.readFileSync(filename));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  const contexts = [];
  try {
    browser = await chromium.launch({ headless: true, channel: process.env.OPERATOR_TEST_BROWSER || "chrome" });
    const base = "http://127.0.0.1:" + server.address().port;
    async function scenario(options) {
      const runtime = await mount(browser, base, options);
      contexts.push(runtime);
      return runtime;
    }
    const normal = await scenario({ hold: true });
    const notice = await normal.page.locator(".notice .print").textContent();
    check([NOTICE.introduction, ...NOTICE.lines, NOTICE.contact, NOTICE.confirmation].every((line) => notice.includes(line)), "后台拍摄告知与共享实际文案一致");
    check(normal.calls.every((call) => call.method === "GET"), "进入成片页不创建任务");
    check(await normal.page.locator(".shot-recovery").isVisible() &&
      await normal.page.locator("[data-regenerate].btn-primary").isVisible(), "备用原片有明显重新生成入口");
    await open(normal);
    check(await normal.page.locator("#regenerate-style").inputValue() === "style-two" &&
      await normal.page.locator("#regenerate-portrait").inputValue() === "chibi", "沿用照片风格和人像模式");
    check(await normal.page.locator("#regenerate-ai-watermark").isChecked(), "重新生成默认勾选 AI 生成标识");
    check(await normal.page.locator("#regenerate-style option").count() === 2, "复用上传的已启用风格目录");
    check(await normal.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "窄屏配置面板不横向溢出");
    await normal.page.locator("#regenerate-cancel").click();
    check(await normal.page.locator("#regenerate-panel").isHidden() && submits(normal).length === 0 &&
      await normal.page.locator("[data-regenerate]").evaluate((node) => node === document.activeElement), "取消配置不提交并恢复焦点");
    await open(normal);
    await normal.page.locator("#regenerate-style").selectOption("style-one");
    await normal.page.locator("#regenerate-portrait").selectOption("standard");
    await normal.page.locator("#regenerate-ai-watermark").uncheck();
    await submit(normal);
    await normal.page.waitForFunction(() => document.querySelector("#regenerate-badge").textContent === "提交中");
    await normal.page.locator("#regenerate-form").evaluate((node) => {
      node.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      node.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    check(submits(normal).length === 1, "提交中重复操作只创建一个任务");
    assert.deepEqual(submits(normal)[0].payload, { token: photo.token, style: "style-one", portrait_mode: "standard", ai_watermark: false });
    check(true, "提交约定四字段，布尔标识为 false，无渠道模型覆盖");
    normal.release();
    await badge(normal, "生成风格");
    check(await normal.page.locator("#regenerate-submit").isDisabled() &&
      await normal.page.locator("[data-regenerate]").isDisabled(), "任务处理中锁定重复生成入口");
    check(await normal.page.evaluate(() => Boolean(sessionStorage.getItem("booth-operator-regenerate-job"))), "处理中的任务保留恢复信息");
    normal.failPoll(503);
    await normal.page.getByText("状态暂未读取成功，正在重试，请勿重复提交。", { exact: true }).waitFor();
    check(await normal.page.locator("#regenerate-submit").isDisabled() && submits(normal).length === 1, "读取失败只重试查询，不重复生成");
    normal.failPoll(0);
    await normal.page.reload();
    await badge(normal, "生成风格");
    check(submits(normal).length === 1, "刷新后复用上传任务路径恢复进度，不重新提交");
    check(await normal.page.locator("#regenerate-style").inputValue() === "style-one" &&
      await normal.page.locator("#regenerate-portrait").inputValue() === "standard" &&
      !await normal.page.locator("#regenerate-ai-watermark").isChecked(), "刷新恢复生成配置与标识选择");
    normal.setRecent([{ ...photo, id: "SM-2026-0002", token: "new-token-1234567890123", degraded: false }, photo]);
    normal.setJob({ stage: "done", ok: true });
    await badge(normal, "已完成");
    await normal.page.waitForFunction(() => document.querySelectorAll(".shot-card").length === 2);
    check(await normal.page.evaluate(() => sessionStorage.getItem("booth-operator-regenerate-job") === null), "成功清除恢复信息，新旧成片并列");
    check(!normal.calls.some((call) => call.method === "DELETE" || call.path.includes("/print")), "全流程不删除原片、不自动打印");

    const conflict = await scenario({ failure: 409, stringJob: true });
    await open(conflict);
    await submit(conflict);
    await badge(conflict, "暂不可提交");
    check(await conflict.page.locator("#regenerate-result").evaluate((node) => node.classList.contains("is-error")) &&
      await conflict.page.locator("#regenerate-submit").isEnabled(), "409 展示错误且允许稍后重试");
    conflict.recover();
    conflict.setJob({ stage: "failed", ok: false, error: "生成服务中断" });
    await submit(conflict);
    await badge(conflict, "未完成");
    check((await conflict.page.locator("#regenerate-result").textContent()).includes("生成服务中断") &&
      await conflict.page.locator("#regenerate-submit").isEnabled(), "任务失败显示原因并可再次生成");
    conflict.setJob({ stage: "done", ok: false, error: "生成未完成" });
    await submit(conflict);
    await badge(conflict, "未完成");
    check((await conflict.page.locator("#regenerate-result").textContent()).includes("生成未完成"), "完成状态仍尊重失败标记");
    conflict.setJob({ stage: "done", degraded: true });
    await submit(conflict);
    await badge(conflict, "备用处理");
    check((await conflict.page.locator("#regenerate-result").textContent()).includes("原片保留") &&
      await conflict.page.locator("#regenerate-submit").isEnabled(), "备用结果不误报成功并保留重试入口");
    check(submits(conflict).slice(1).every((call) => call.payload.ai_watermark === true), "重试保持标识默认 true 并支持字符串任务号");

    const missing = await scenario({ missingJob: true });
    await open(missing);
    await submit(missing);
    await badge(missing, "提交未确认");
    check((await missing.page.locator("#regenerate-result").textContent()).includes("检查最近成片") &&
      await missing.page.locator("#regenerate-submit").isEnabled(), "无任务号时提示核对成片再重试");

    const rejected = await scenario({ failure: 400 });
    await open(rejected);
    await submit(rejected);
    await badge(rejected, "无法生成");
    check((await rejected.page.locator("#regenerate-result").textContent()).includes("检查照片是否仍有效") &&
      !rejected.calls.some((call) => call.path === "/api/job/regenerate-job"), "400 明确提示照片不可用且不轮询不存在的任务");

    const expired = await scenario({ recent: [
      photo, { ...photo, token: "expired", expires_at: Date.now() / 1000 - 1 },
      { ...photo, token: "deleted", deleted: true }, { ...photo, token: "tombstone", extra: { tombstone: true } },
      { ...photo, token: "flag-expired", expired: true }, { ...photo, token: "missing-capture", capture_available: false },
      { ...photo, token: "unavailable", regenerate_available: false },
    ] });
    check(await expired.page.locator("[data-regenerate]").count() === 1, "已过期已删除和无原片照片无生成入口");
    await open(expired);
    expired.setRecent([]);
    await expired.page.waitForFunction(() => document.querySelectorAll(".shot-card").length === 0);
    await submit(expired);
    await expired.page.getByText("这张照片已失效或不可用，请刷新成片列表。", { exact: true }).waitFor();
    check(submits(expired).length === 0, "配置打开后照片失效也不能提交");

    const recovery = await scenario({ stylesFailure: true });
    await open(recovery);
    check(await recovery.page.locator("#regenerate-submit").isDisabled(), "风格读取失败不允许提交");
    recovery.recover();
    await recovery.page.locator("#regenerate-reload").click();
    await recovery.page.waitForFunction(() => !document.querySelector("#regenerate-submit").disabled);
    check(await recovery.page.locator("#regenerate-submit").isEnabled(), "风格可单独重试读取");
    await submit(recovery);
    await badge(recovery, "生成风格");
    recovery.failPoll(404);
    await badge(recovery, "状态不可用");
    check(await recovery.page.locator("#regenerate-submit").isEnabled(), "任务失效解除锁定并提示核对成片");

    const uploads = await scenario();
    check(await uploads.page.locator("#upload-ai-watermark").isChecked(), "上传默认勾选 AI 生成标识");
    await uploads.page.locator("#upload-image").setInputFiles({ name: "portrait.png", mimeType: "image/png", buffer: pixel });
    await uploads.page.locator("#upload-submit").click();
    await uploads.page.waitForFunction(() => document.querySelector("#upload-badge").textContent === "已完成");
    await uploads.page.locator("#upload-ai-watermark").uncheck();
    await uploads.page.locator("#upload-mode").selectOption("original");
    await uploads.page.locator("#upload-submit").click();
    await uploads.page.waitForFunction(() => document.querySelector("#upload-badge").textContent === "已完成");
    const uploadCalls = uploads.calls.filter((call) => call.path === "/api/operator/upload");
    check(uploadCalls[0].payload.includes('name="ai_watermark"\r\n\r\ntrue') &&
      uploadCalls[1].payload.includes('name="ai_watermark"\r\n\r\nfalse'), "上传显式发送 true 与未勾选的 false");
    check(contexts.every((runtime) => runtime.errors.length === 0), "所有模拟场景无前端运行异常");
    console.log("PASS " + checks + " checks");
  } finally {
    for (const runtime of contexts) await runtime.context.close();
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
