"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");

const staticRoot = path.join(__dirname, "..", "static");
const source = fs.readFileSync(path.join(staticRoot, "js", "operator.js"), "utf8");
const html = fs.readFileSync(path.join(staticRoot, "operator.html"), "utf8");
const routes = ["", "queue", "photos", "styles", "models", "settings", "system"];
const prompt = "Preserve the single person and original clothes in a clean portrait.";
const seed = () => ({
  version: 1,
  styles: [
    { id: "style-one", title: "风格一", description: "柔和人像", enabled: true, is_default: true,
      revision: 2, history: [{ revision: 1, title: "旧风格一", updated_at: 1750000000 }],
      model: "gpt-image-1", sizes: ["1024x1536"], backdrop_id: "dynamic-minimal",
      default_orientation: "vertical", wide_prompt: prompt, vertical_prompt: prompt, negative_prompt: prompt },
    { id: "style-two", title: "风格二", description: "明快人像", enabled: true, is_default: false,
      model: "gpt-image-1", sizes: ["1024x1536"], backdrop_id: "dynamic-minimal",
      default_orientation: "vertical", wide_prompt: prompt, vertical_prompt: prompt, negative_prompt: prompt,
      revision: 1, history: [] },
    { id: "style-off", title: "历史画风", description: "", enabled: false, is_default: false, history: [] },
  ],
});
const state = {
  camera: { ok: true, real: true, device: "camera", width: 1920, height: 1080, fps: 30 },
  infer: { running: true, preloaded: true, ep: false }, photos: 0, recent: [],
  disk: { free_gb: 8, total_gb: 32 }, share: { public_base: "http://localhost", source: "lan" },
  counters: {}, build: "test", uptime_s: 100,
};
const config = { configured: "", public_base: "http://localhost", lan_base: "http://localhost",
  source: "lan", secure: false, aigc_global_model: "gpt-image-1" };
const channelSeed = () => ({ ok: true, model_presets: [
  { model: "gemini-2.5-flash-image", name: "Gemini 3.1 Flash Image",
    base_url: "https://api.example.com/v1", timeout: 180 },
  { model: "gpt-image-1", name: "GPT Image 2",
    base_url: "https://api.example.com/v1", timeout: 240 },
], active_channel_id: "primary", channels: [
  { id: "primary", name: "主服务", base_url: "https://primary.example/v1", health_url: "",
    model: "current-model", models: ["legacy-model"], key_configured: true, enabled: true,
    priority: 1, timeout: 120, health: "available", routing: "primary", detail: "连接正常" },
  { id: "backup", name: "候补服务", base_url: "https://backup.example/v1", health_url: "",
    models: ["backup-model"], key_configured: true, enabled: true,
    priority: 2, timeout: 120, health: "unknown", routing: "standby", detail: "尚未检测" },
] });
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}
async function settle(page) {
  await page.waitForFunction(() => document.querySelector("#badge-ep").textContent !== "等待加载");
}
async function mount(browser, base, route, options = {}) {
  const context = await browser.newContext({ viewport: options.viewport || { width: 1440, height: 900 } });
  const page = await context.newPage();
  const calls = [];
  const errors = [];
  let catalog = seed();
  let failStyles = Boolean(options.failStyles);
  let releaseDelete = () => {};
  let channels = channelSeed();
  let failChannels = Boolean(options.failChannels);
  let malformedSave = Boolean(options.malformedSave);
  let releaseChannelTest = () => {};
  let uploadCount = 0;
  let uploadPollCount = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (request) => {
    const url = new URL(request.request().url());
    const method = request.request().method();
    const contentType = request.request().headers()["content-type"] || "";
    const payload = contentType.includes("application/json")
      ? request.request().postDataJSON()
      : request.request().postDataBuffer();
    calls.push({ path: url.pathname, method, payload, contentType });
    let status = 200;
    let result;
    if (url.pathname === "/api/build") result = { build: "test" };
    else if (url.pathname === "/api/state") result = state;
    else if (url.pathname === "/api/operator/config") result = config;
    else if (url.pathname === "/api/operator/aigc/channels" && method === "GET") {
      if (failChannels) { status = 503; result = { ok: false, detail: "渠道连接中断" }; }
      else result = channels;
    } else if (url.pathname === "/api/operator/aigc/channels" && method === "POST") {
      if (malformedSave) result = { ok: true };
      else {
        channels.channels = payload.channels.map((item) => {
          const previous = channels.channels.find((saved) => saved.id === item.id);
          const saved = { ...item, name: item.name + "（服务端）", model: item.model + "-saved",
            models: [item.model + "-saved"], key_configured: Boolean(item.api_key || previous?.key_configured),
            routing: item.enabled ? "standby" : "disabled", health: "unknown", detail: "已保存" };
          delete saved.api_key;
          return saved;
        }).reverse();
        result = channels;
      }
    } else if (url.pathname === "/api/operator/aigc/channels/check") {
      result = channels;
    } else if (url.pathname === "/api/operator/aigc/channels/test") {
      if (options.holdChannelTest) await new Promise((resolve) => { releaseChannelTest = resolve; });
      result = { ok: true, model: "current-model", elapsed_ms: 100,
        image: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=" };
    }
    else if (url.pathname === "/api/operator/queue") {
      result = { unlimited: true, active: 1, entries: [{ queue_id: "queue-1", display_number: "Q001",
        style_id: "style-one", status: "queued", created_at: "2026-10-12T10:00:00Z" }] };
    } else if (url.pathname.endsWith("/delete")) {
      if (options.holdDelete) await new Promise((resolve) => { releaseDelete = resolve; });
      status = 409;
      result = { ok: false, detail: "风格正在使用，请稍后重试" };
    } else if (url.pathname.endsWith("/rollback")) {
      result = catalog;
    } else if (url.pathname === "/api/operator/aigc/styles" && method === "GET") {
      if (failStyles) { status = 503; result = { ok: false, detail: "连接中断" }; }
      else result = catalog;
    } else if (url.pathname === "/api/operator/aigc/styles" && method === "POST") {
      if (payload.styles) catalog.styles = payload.styles;
      else catalog.styles.push({ ...payload, revision: 1, history: [] });
      result = catalog;
    } else if (url.pathname.startsWith("/api/operator/aigc/styles/") && method === "POST") {
      const id = decodeURIComponent(url.pathname.split("/").pop());
      catalog.styles = catalog.styles.map((style) => style.id === id ? { ...style, ...payload } : style);
      result = catalog;
    } else if (url.pathname === "/api/operator/upload" && method === "POST") {
      uploadCount++;
      uploadPollCount = 0;
      result = { ok: true, job: { job: "upload-job-" + uploadCount, stage: "queued" } };
    } else if (url.pathname.startsWith("/api/job/upload-job-") && method === "GET") {
      const uploadJobs = options.uploadJobs || [{ stage: "done", degraded: false }];
      const uploadStatus = uploadJobs[Math.min(uploadPollCount++, uploadJobs.length - 1)];
      result = { ok: true, job: url.pathname.split("/").pop(), ...uploadStatus };
    } else result = { ok: true };
    await request.fulfill({ status, contentType: "application/json", body: JSON.stringify(result) });
  });
  await page.goto(base + "/operator" + (route ? "/" + route : ""));
  await settle(page);
  return { context, page, calls, errors, recover: () => { failStyles = false; failChannels = false; },
    releaseDelete: () => releaseDelete(), releaseChannelTest: () => releaseChannelTest(),
    setChannelHealth: (value) => { channels = value; }, validChannelSave: () => { malformedSave = false; } };
}
async function main() {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/operator" || url.pathname.startsWith("/operator/")) {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      return response.end(html.replaceAll("__BUILD__", "test"));
    }
    if (!url.pathname.startsWith("/static/")) { response.statusCode = 404; return response.end(); }
    const file = path.resolve(staticRoot, "." + url.pathname.slice("/static".length));
    if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)) { response.statusCode = 404; return response.end(); }
    response.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" :
      file.endsWith(".css") ? "text/css" : file.endsWith(".svg") ? "image/svg+xml" : "application/octet-stream");
    response.end(fs.readFileSync(file));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = "http://127.0.0.1:" + server.address().port;
  const browser = await chromium.launch({ headless: true, channel: process.env.OPERATOR_TEST_BROWSER || "chrome" });
  try {
    check(!html.includes("ui-polish.css"), "后台不加载旧视觉覆盖");
    for (const route of routes) {
      const runtime = await mount(browser, base, route);
      const { page, calls, errors } = runtime;
      const expected = route || "overview";
      check(await page.locator("[data-page]:visible").count() === 1, expected + "只显示一页");
      check(await page.locator('[data-page="' + expected + '"]').isVisible(), expected + "独立 URL");
      check(await page.locator('[aria-current="page"]').getAttribute("data-page-link") === expected, expected + "导航高亮");
      const apiCalls = calls.filter((call) => call.path !== "/api/build");
      const wanted = route === "styles" ? ["/api/operator/aigc/styles"] :
        route === "models" ? ["/api/operator/aigc/channels", "/api/state"] :
        route === "queue" ? ["/api/operator/queue"] :
        route === "settings" ? ["/api/operator/config", "/api/state"] :
        route === "photos" ? ["/api/operator/aigc/styles", "/api/state"] : ["/api/state"];
      check(JSON.stringify(apiCalls.map((call) => call.path)) === JSON.stringify(wanted), expected + "只请求当前页数据");
      check(apiCalls.every((call) => call.method === "GET"), expected + "进入页面不写配置");
      if (!route) {
        check(await page.locator("#badge-ep").textContent() === "正常运行", "总览不展示 CPU 技术文案");
        check(await page.locator("video, img[src*='preview.mjpg']").count() === 0, "总览无视频流");
      }
      check(await page.locator("#aigc-model-input, #aigc-model-save, #aigc-model-clear").count() === 0, "无全局模型面板");
      if (route === "models") {
        check(await page.locator('[data-page-link="models"]').textContent() === await page.locator("#page-title").textContent() &&
          await page.locator("#models-title").textContent() === "生图渠道", "渠道导航与页面标题一致");
        check(!calls.some((call) => call.path === "/api/operator/config"), "渠道页不读取全局配置");
        check(await page.locator("#channel-add-presets").isEnabled(), "渠道页提供模型预设入口");
      }
      await page.setViewportSize({ width: 390, height: 844 });
      check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), expected + "移动端不横向溢出");
      check(errors.length === 0, expected + "无前端运行异常");
      await runtime.context.close();
    }

    const uploads = await mount(browser, base, "photos");
    const uploadPage = uploads.page;
    const image = { name: "portrait.png", mimeType: "image/png",
      buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64") };
    const channelRuntime = await mount(browser, base, "models", { holdChannelTest: true, malformedSave: true });
    const channelPage = channelRuntime.page;
    const primary = channelPage.locator('[data-channel-id="primary"]');
    const backup = channelPage.locator('[data-channel-id="backup"]');
    check(await primary.getByLabel("生成模型", { exact: true }).inputValue() === "current-model", "渠道 model 优先于兼容 models");
    check(await backup.getByLabel("生成模型", { exact: true }).inputValue() === "backup-model", "兼容旧单元素 models");
    check(await primary.locator("datalist option").count() === 2 && await primary.locator("select").count() === 0,
      "模型为可自由输入的单值字段并提供旧值建议");
    await channelPage.locator("#channel-add-presets").click();
    check(await channelPage.locator(".channel-row").count() === 4 &&
      await channelPage.locator('[data-channel-id="example-gemini-2-5-flash-image"]').count() === 1 &&
      await channelPage.locator('[data-channel-id="example-gpt-image-1"]').count() === 1,
      "模型预设可批量加入且不覆盖已有渠道");
    check(await channelPage.locator(".channel-advanced[open], .channel-test[open]").count() === 0, "高级设置与测试默认折叠");
    await primary.locator(".channel-test summary").click();
    await primary.getByLabel("测试图片", { exact: true }).setInputFiles(image);
    await primary.getByRole("button", { name: "测试生成", exact: true }).click();
    await channelPage.waitForFunction(() => document.querySelector(".channel-test[aria-busy='true']"));
    check(await channelPage.locator("#channel-save").isDisabled() && await channelPage.locator("#channel-add").isDisabled() &&
      await channelPage.locator("#channel-check").isDisabled() && await primary.getByLabel("生成模型", { exact: true }).isDisabled() &&
      await backup.getByRole("button", { name: "移除", exact: true }).isDisabled(), "测试期间锁定编辑保存检查增删");
    await primary.locator(".channel-test-button").evaluate((node) => node.dispatchEvent(new Event("click")));
    check(channelRuntime.calls.filter((call) => call.path.endsWith("/channels/test")).length === 1, "测试双击不会重复请求");
    const testBody = channelRuntime.calls.find((call) => call.path.endsWith("/channels/test")).payload.toString("utf8");
    check(testBody.includes('name="id"') && testBody.includes('name="image"') && !testBody.includes('name="model"'),
      "测试只提交 id 和 image，没有额外模型覆盖");
    channelRuntime.releaseChannelTest();
    await primary.locator(".channel-test-result img").waitFor();
    await primary.getByLabel("生成模型", { exact: true }).fill("自由输入/新模型");
    await primary.getByLabel("API Key", { exact: true }).fill("unsaved-secret");
    await primary.getByLabel("渠道名称", { exact: true }).fill("未保存主服务");
    await primary.getByLabel("服务 URL", { exact: true }).fill("https://changed.example/v1");
    await primary.getByLabel("优先级 · 数字越小越先用", { exact: true }).fill("7");
    await primary.getByLabel("启用渠道", { exact: true }).uncheck();
    await primary.locator(".channel-advanced summary").click();
    await primary.getByLabel("健康检查 URL（可选）", { exact: true }).fill("https://changed.example/health");
    await primary.getByLabel("生成超时（秒）", { exact: true }).fill("240");
    const draft = await primary.locator("[data-field]").evaluateAll((nodes) => nodes.map((node) => [node.dataset.field, node.value, node.checked]));
    await channelPage.locator("#channel-add").click();
    const added = channelPage.locator(".channel-row").last();
    channelPage.once("dialog", (message) => message.accept());
    await added.getByRole("button", { name: "移除", exact: true }).click();
    check(JSON.stringify(await primary.locator("[data-field]").evaluateAll((nodes) => nodes.map((node) => [node.dataset.field, node.value, node.checked]))) === JSON.stringify(draft),
      "新增移除保留所有未保存输入及密钥");
    check(await primary.locator(".channel-test-result img").count() === 1 && await primary.getByLabel("测试图片").evaluate((node) => node.files.length) === 1,
      "新增移除保留测试图片结果与折叠状态");
    const changedHealth = channelSeed();
    changedHealth.active_channel_id = "backup";
    changedHealth.channels[0].routing = "cooldown";
    changedHealth.channels[1].routing = "primary";
    channelRuntime.setChannelHealth(changedHealth);
    await channelPage.waitForFunction(() => document.querySelector("#channel-active").textContent.includes("候补服务"));
    check(JSON.stringify(await primary.locator("[data-field]").evaluateAll((nodes) => nodes.map((node) => [node.dataset.field, node.value, node.checked]))) === JSON.stringify(draft) &&
      await primary.locator(".channel-test-result img").count() === 1, "健康轮询更新主用状态且不重置草稿与结果");
    check((await channelPage.locator("#channel-dirty").textContent()).includes("未保存") &&
      await primary.locator(".channel-test-button").isDisabled(), "未保存提示且禁止用旧配置测试");
    await channelPage.locator("#channel-check").click();
    await channelPage.waitForFunction(() => document.querySelector("#channel-result").textContent.includes("未保存输入已保留"));
    check(await primary.getByLabel("API Key", { exact: true }).inputValue() === "unsaved-secret", "立即检查保留密钥草稿");
    await channelPage.locator("#channel-save").click();
    await channelPage.waitForFunction(() => document.querySelector("#channel-result").textContent.includes("数据不完整"));
    check(await primary.getByLabel("API Key", { exact: true }).inputValue() === "unsaved-secret" &&
      await channelPage.locator(".channel-row").count() === 2, "缺失保存数据时保留草稿而非清空渠道");
    channelRuntime.validChannelSave();
    await channelPage.locator("#channel-save").click();
    await channelPage.waitForFunction(() => document.querySelector("#channel-result").textContent.includes("配置已保存"));
    const channelSave = channelRuntime.calls.filter((call) => call.path === "/api/operator/aigc/channels" && call.method === "POST").pop();
    check(channelSave.payload.channels[0].model === "自由输入/新模型" &&
      JSON.stringify(channelSave.payload.channels[0].models) === JSON.stringify(["自由输入/新模型"]), "保存 model 与兼容单元素 models");
    check(await primary.getByLabel("生成模型", { exact: true }).inputValue() === "自由输入/新模型-saved" &&
      await primary.getByLabel("渠道名称", { exact: true }).inputValue() === "未保存主服务（服务端）" &&
      await channelPage.locator(".channel-row").first().getAttribute("data-channel-id") === "backup", "保存后只采用服务端值与排序");
    check(await primary.getByLabel("API Key", { exact: true }).inputValue() === "" && await channelPage.locator("#channel-save").isDisabled(),
      "保存后清空密钥输入并清除未保存标记");
    for (const width of [1440, 390, 320]) {
      await channelPage.setViewportSize({ width, height: 844 });
      await primary.locator(".channel-test summary").click();
      check(await channelPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), width + "像素渠道页不横向溢出");
      await primary.locator(".channel-test summary").click();
    }
    check(channelRuntime.errors.length === 0, "渠道全流程无前端异常");
    await channelRuntime.context.close();
    const brokenChannels = await mount(browser, base, "models", { failChannels: true });
    check(await brokenChannels.page.locator("#channel-save").isDisabled() && await brokenChannels.page.locator("#channel-add").isDisabled(),
      "渠道加载失败时禁止空保存与新增");
    check(!brokenChannels.calls.some((call) => call.method === "POST"), "渠道加载失败不发送写请求");
    brokenChannels.recover();
    await brokenChannels.page.locator("#channel-reload").click();
    await brokenChannels.page.waitForFunction(() => !document.querySelector("#channel-add").disabled);
    check(await brokenChannels.page.locator(".channel-row").count() === 2, "渠道读取失败可重试恢复");
    await brokenChannels.context.close();
    await uploadPage.locator("#upload-image").setInputFiles(image);
    check(await uploadPage.locator("#upload-preview").isVisible(), "选择图片后显示预览");
    await uploadPage.locator("#upload-style").selectOption("style-two");
    await uploadPage.locator('[name="portrait_mode"]').selectOption("chibi");
    await uploadPage.getByLabel("姓名（可选）", { exact: true }).fill("李维");
    await uploadPage.getByLabel("开发者账号（可选）", { exact: true }).fill("@developer");
    await uploadPage.getByRole("button", { name: "提交图片", exact: true }).click();
    await uploadPage.waitForFunction(() => document.querySelector("#upload-badge").textContent === "已完成");
    check(await uploadPage.locator("#upload-result").textContent() === "成片已保存，可在下方下载或开始打印。" &&
      !await uploadPage.locator("#upload-result").evaluate((node) => node.classList.contains("is-error")), "正常完成显示成功提示");
    const aigcUpload = uploads.calls.find((call) => call.path === "/api/operator/upload");
    const aigcBody = aigcUpload.payload.toString("utf8");
    check(aigcUpload.contentType.includes("multipart/form-data"), "AIGC 上传使用图片表单");
    check(aigcBody.includes('name="mode"\r\n\r\naigc') &&
      aigcBody.includes('name="style"\r\n\r\nstyle-two') &&
      aigcBody.includes('name="portrait_mode"\r\n\r\nchibi'), "AIGC 上传提交风格与人像模式");
    check(await uploadPage.evaluate(() => sessionStorage.getItem("booth-operator-upload-job") === null), "完成后清除待处理状态");

    await uploadPage.locator("#upload-mode").selectOption("original");
    check(await uploadPage.locator("#upload-style-wrap").isHidden() &&
      await uploadPage.locator("#upload-portrait-wrap").isHidden(), "原图模式隐藏无关选项");
    await uploadPage.getByRole("button", { name: "提交图片", exact: true }).click();
    await uploadPage.waitForFunction(() => document.querySelector("#upload-badge").textContent === "已完成");
    const originalUpload = uploads.calls.filter((call) => call.path === "/api/operator/upload")[1];
    check(originalUpload.payload.toString("utf8").includes('name="mode"\r\n\r\noriginal'), "原图模式提交到独立处理路径");
    await uploadPage.setViewportSize({ width: 390, height: 844 });
    check(await uploadPage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "图片上传区移动端不横向溢出");
    check(uploads.errors.length === 0, "图片上传全流程无前端异常");
    await uploads.context.close();

    const uploadCases = [
      { name: "单独分割失败", job: { degraded: true, degraded_reason: "infer_failed: timeout", person_count: 0 },
        message: "人像处理未完成，已保留原图，请重试。", badge: "已保存原图", error: true },
      { name: "旧记录分割失败优先于零人生成失败", job: { degraded: true, person_count: 0,
        degraded_reason: "infer_failed: timeout; aigc_failed: 画面里没有检出人像，跳过风格化" },
        message: "人像处理未完成，已保留原图，请重试。", badge: "已保存原图", error: true },
      { name: "分割失败不受标记顺序影响", job: { degraded: true,
        degraded_reason: "aigc_failed: 人像抠像不可用; matte_failed: unavailable; infer_failed: timeout" },
        message: "人像处理未完成，已保留原图，请重试。", badge: "已保存原图", error: true },
      { name: "明确未检出人像", job: { degraded: true, person_count: 0,
        degraded_reason: "aigc_failed: 画面里没有检出人像，跳过风格化" },
        message: "未检测到人像，已保留原图，请更换图片后重试。", badge: "已保存原图", error: true },
      { name: "抠像失败标记", job: { degraded: true,
        degraded_reason: "matte_failed: unavailable; aigc_failed: generation skipped" },
        message: "抠像未完成，已保留原图，请重试。", badge: "已保存原图", error: true },
      { name: "旧记录抠像不可用", job: { degraded: true, degraded_reason: "aigc_failed: 人像抠像不可用" },
        message: "抠像未完成，已保留原图，请重试。", badge: "已保存原图", error: true },
      { name: "零人字段不误判生成失败", job: { degraded: true, person_count: 0,
        degraded_reason: "aigc_failed: upstream unavailable" },
        message: "风格生成未完成，已保留原图。请查看下方成片。", badge: "已保存原图", error: true },
      { name: "其他降级不宣称保留原图", job: { degraded: true, degraded_reason: "infer_timeout>2.5s" },
        message: "成片已保存，可在下方下载或开始打印。", badge: "已完成", error: false },
      { name: "硬失败不宣称已保存", job: { stage: "failed", ok: false, error: "保存失败", degraded: true,
        degraded_reason: "infer_failed: timeout; aigc_failed: 画面里没有检出人像，跳过风格化" },
        message: "保存失败", badge: "未完成", error: true },
      { name: "硬失败缺省提示", job: { stage: "failed", ok: false },
        message: "处理未完成，请重新提交", badge: "未完成", error: true },
      { name: "完成状态仍尊重失败标记", job: { ok: false, error: "处理失败", degraded: true,
        degraded_reason: "aigc_failed: upstream unavailable" },
        message: "处理失败", badge: "未完成", error: true },
    ];
    for (const uploadCase of uploadCases) {
      const scenario = await mount(browser, base, "photos", { uploadJobs: [
        { stage: "inferring" }, { stage: "done", degraded: false, ...uploadCase.job },
      ] });
      const scenarioPage = scenario.page;
      await scenarioPage.locator("#upload-image").setInputFiles(image);
      await scenarioPage.getByRole("button", { name: "提交图片", exact: true }).click();
      await scenarioPage.waitForFunction(() => document.querySelector("#upload-badge").textContent === "分离人像");
      check(await scenarioPage.locator("#upload-result").textContent() === "分离人像，完成后自动显示在下方。" &&
        await scenarioPage.locator("#upload-submit").isDisabled() &&
        await scenarioPage.evaluate(() => sessionStorage.getItem("booth-operator-upload-job") === "upload-job-1"),
      uploadCase.name + "处理中锁定表单并记住任务");
      await scenarioPage.waitForFunction((message) => document.querySelector("#upload-result").textContent === message,
        uploadCase.message);
      check(await scenarioPage.locator("#upload-badge").textContent() === uploadCase.badge &&
        await scenarioPage.locator("#upload-result").evaluate((node) => node.classList.contains("is-error")) === uploadCase.error,
      uploadCase.name + "轮询显示正确文案与徽标");
      check(await scenarioPage.locator("#upload-submit").isEnabled() &&
        await scenarioPage.locator("#operator-upload-form").getAttribute("aria-busy") === "false" &&
        await scenarioPage.evaluate(() => sessionStorage.getItem("booth-operator-upload-job") === null),
      uploadCase.name + "终态解锁并清除待处理状态");
      check(scenario.calls.filter((call) => call.path.startsWith("/api/job/upload-job-")).length === 2 &&
        scenario.calls.filter((call) => call.path === "/api/operator/upload").length === 1 && scenario.errors.length === 0,
      uploadCase.name + "通过定时轮询完成且不重复提交");
      await scenario.context.close();
    }

    const runtime = await mount(browser, base, "styles", { holdDelete: true });
    const { page, calls } = runtime;
    await page.getByLabel("搜索风格").fill("柔和");
    check(await page.locator(".style-row").count() === 1, "按风格说明搜索");
    await page.getByLabel("搜索风格").fill("");
    await page.getByLabel("显示", { exact: true }).selectOption("disabled");
    check(await page.locator(".style-row").count() === 1, "筛选停用风格");
    check(await page.getByRole("button", { name: "设为默认", exact: true }).isDisabled(), "停用风格不能设默认");
    await page.getByLabel("显示", { exact: true }).selectOption("all");
    const second = page.locator(".style-row").filter({ hasText: "风格二" });
    await second.getByRole("button", { name: "设为默认", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#aigc-result").textContent.includes("默认风格已更新"));
    const defaultSave = calls.find((call) => call.method === "POST" && call.payload.styles);
    check(defaultSave.payload.styles.filter((style) => style.is_default).length === 1 &&
      defaultSave.payload.styles.find((style) => style.id === "style-two").is_default, "切换默认清除旧默认");

    await second.getByRole("button", { name: "停用", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#aigc-result").textContent.includes("风格已停用"));
    const disabledSave = calls.find((call) => call.path.endsWith("/style-two") && call.method === "POST");
    check(disabledSave.payload.enabled === false && disabledSave.payload.is_default === false, "停用默认同时清除默认标记");

    const first = page.locator(".style-row").filter({ hasText: "风格一" });
    await first.getByRole("button", { name: "编辑", exact: true }).click();
    check(await page.getByRole("dialog").isVisible(), "编辑使用独立对话框");
    check(await page.locator(".editor-advanced").getAttribute("open") === null &&
      await page.getByLabel("竖版生成说明", { exact: true }).isVisible(), "竖版说明直接显示，横版选填默认折叠");
    check(await page.locator(".editor-advanced input").count() === 0 &&
      await page.locator(".editor-advanced textarea").count() === 1, "横版设置仅保留横版说明，无模型输入");
    await page.getByLabel("风格名称", { exact: true }).fill("风格一已编辑");
    await page.getByRole("button", { name: "保存风格", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
    check(calls.some((call) => call.path.endsWith("/style-one") && call.payload.title === "风格一已编辑"), "编辑保存真实字段");
    check(calls.filter((call) => call.path.startsWith("/api/operator/aigc/styles") && call.method === "POST")
      .every((call) => !Object.hasOwn(call.payload, "model") && (!call.payload.styles || call.payload.styles.every((item) => !Object.hasOwn(item, "model")))),
    "风格编辑与默认设置不提交模型覆盖");
    const edited = page.locator(".style-row").filter({ has: page.getByText("风格一已编辑", { exact: true }) });
    await edited.getByText("更多", { exact: true }).click();
    await edited.getByRole("button", { name: "复制为新风格" }).click();
    check(await page.getByLabel("风格标识", { exact: true }).inputValue() === "style-one-copy", "复制使用新标识");
    check(!await page.getByLabel("启用，供参与者选择").isChecked(), "副本默认停用");
    await page.getByLabel("启用，供参与者选择").check();
    await page.getByRole("button", { name: "保存风格", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
    const copiedSave = calls.find((call) => call.method === "POST" && call.payload.id === "style-one-copy");
    check(copiedSave.payload.enabled && !copiedSave.payload.is_default, "副本可以保存启用且不继承默认");

    await edited.getByText("更多", { exact: true }).click();
    await edited.getByRole("button", { name: "恢复历史" }).click();
    page.once("dialog", (message) => message.accept());
    await page.getByRole("button", { name: "恢复这一版" }).click();
    await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
    check(calls.some((call) => call.path.endsWith("/style-one/rollback") && call.payload.revision === 1), "历史恢复发送选定版本");

    const deleteCount = () => calls.filter((call) => call.path.endsWith("/delete")).length;
    await edited.getByText("更多", { exact: true }).click();
    page.once("dialog", (message) => message.dismiss());
    await edited.getByRole("button", { name: "删除风格", exact: true }).click();
    check(deleteCount() === 0, "取消确认不发送删除");
    page.once("dialog", (message) => message.accept());
    await edited.getByRole("button", { name: "删除风格", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#aigc-styles").getAttribute("aria-busy") === "true");
    check((await page.locator("#aigc-result").textContent()).includes("正在删除风格"), "删除等待时显示对应操作反馈");
    check(await page.getByRole("button", { name: "新增风格", exact: true }).isDisabled() &&
      await page.getByRole("button", { name: "刷新目录", exact: true }).isDisabled(), "删除等待时锁定新增与刷新");
    check(await page.locator(".style-list button:enabled").count() === 0, "删除等待时锁定所有风格操作");
    runtime.releaseDelete();
    await page.waitForFunction(() => document.querySelector("#aigc-result").textContent.includes("风格正在使用"));
    check(deleteCount() === 1, "删除使用约定 POST 路径");
    check(await edited.isVisible(), "后端拒绝删除时保留风格并显示原因");
    check(await page.locator("#aigc-result").evaluate((node) => node.classList.contains("is-error")), "删除错误使用醒目错误反馈");
    check(await page.getByRole("button", { name: "刷新目录", exact: true }).isEnabled() &&
      await edited.getByRole("button", { name: "编辑", exact: true }).isEnabled(), "删除失败后解锁操作可继续编辑");

    await page.setViewportSize({ width: 390, height: 844 });
    await edited.getByRole("button", { name: "编辑", exact: true }).click();
    check(await page.evaluate(() => {
      const bounds = document.querySelector("#style-dialog").getBoundingClientRect();
      return bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight;
    }), "移动端编辑框在视口内");
    await page.getByLabel("风格名称", { exact: true }).fill("尚未保存");
    page.once("dialog", (message) => message.dismiss());
    await page.getByRole("button", { name: "取消", exact: true }).click();
    check(await page.getByRole("dialog").isVisible(), "取消放弃确认保留编辑内容");
    page.once("dialog", (message) => message.accept());
    await page.getByRole("button", { name: "取消", exact: true }).click();
    check(!await page.getByRole("dialog").isVisible(), "确认放弃才关闭编辑");
    await page.getByRole("button", { name: "新增风格", exact: true }).click();
    check(!await page.getByLabel("启用，供参与者选择").isChecked(), "新增风格默认停用");
    await page.getByRole("button", { name: "保存风格", exact: true }).click();
    check(await page.getByLabel("竖版生成说明", { exact: true }).evaluate((node) => !node.checkValidity()), "竖版说明必填");
    await page.getByLabel("风格名称", { exact: true }).fill("新增可选风格");
    await page.getByLabel("竖版生成说明", { exact: true }).fill(prompt);
    check(await page.getByLabel("统一避免内容（选填）", { exact: true }).inputValue() === "", "避免内容可留空");
    check(!await page.getByLabel("横版生成说明", { exact: true }).isVisible(), "横版选填且默认收起");
    await page.getByLabel("启用，供参与者选择").check();
    await page.getByRole("button", { name: "保存风格", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
    check(calls.some((call) => call.payload && call.payload.title === "新增可选风格" && call.payload.enabled && !call.payload.is_default), "新增风格可启用保存");
    check(calls.some((call) => call.payload && call.payload.title === "新增可选风格" && call.payload.vertical_prompt === prompt && call.payload.wide_prompt === "" && call.payload.negative_prompt === ""), "仅填写竖版说明即可提交保存");
    check(runtime.errors.length === 0, "风格全流程无前端异常");
    await runtime.context.close();

    const broken = await mount(browser, base, "styles", { failStyles: true });
    check(await broken.page.getByRole("button", { name: "新增风格", exact: true }).isDisabled(), "目录读取失败时阻止误新增");
    check((await broken.page.locator("#aigc-result").textContent()).includes("连接中断"), "目录错误显示原因");
    broken.recover();
    await broken.page.getByRole("button", { name: "刷新目录", exact: true }).click();
    await broken.page.waitForFunction(() => !document.querySelector("#aigc-add").disabled);
    check(await broken.page.locator(".style-row").count() === 3, "错误后可刷新恢复");
    await broken.context.close();

    const polling = await mount(browser, base, "queue");
    await polling.page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, value: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const before = polling.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 4300));
    check(polling.calls.filter((call) => call.path === "/api/operator/queue").length === 1, "隐藏标签页不继续轮询排队");
    check(polling.calls.slice(before).every((call) => call.path === "/api/build"), "隐藏页不加载额外业务接口");
    await polling.page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, value: false });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await polling.page.waitForFunction(() => document.querySelector("#badge-ep").textContent === "名单已更新");
    await new Promise((resolve) => setTimeout(resolve, 100));
    check(polling.calls.filter((call) => call.path === "/api/operator/queue").length === 2, "返回标签页立即刷新");
    await polling.context.close();
    check(!source.includes("setInterval(refresh"), "页面轮询不叠加旧全页定时器");
    console.log(checks + " checks passed");
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
module.exports = { mount };
if (require.main === module) main().catch((error) => { console.error(error); process.exitCode = 1; });

