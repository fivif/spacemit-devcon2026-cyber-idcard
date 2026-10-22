"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");

const root = path.join(__dirname, "..", "static");
const photo = fs.readFileSync(path.join(__dirname, "..", "assets", "styles", "chinese-elegance-preview.jpg"));
const token = "abcdefghijklmnopqrstuv";
const markedPath = "/api/photo/" + token + ".jpg";
const cleanPath = "/api/photo/" + token + ".clean.jpg";
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
}

async function mount(browser, base, options = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 },
    userAgent: options.userAgent, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  const mutations = [];
  let failClean = false;
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (request) => {
    const pathname = new URL(request.request().url()).pathname;
    if (request.request().method() !== "GET") mutations.push(pathname);
    if (pathname === markedPath || pathname === cleanPath) {
      if (failClean && pathname === cleanPath) return request.fulfill({ status: 404, body: "missing" });
      return request.continue();
    }
    if (pathname.startsWith("/api/qr/")) return request.fulfill({ status: 404, body: "" });
    return request.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true,
      id: "SM-2026-0001", style: "chinese-elegance", portrait_mode: "standard",
      download_clean_available: options.cleanAvailable !== false, print_available: false, build: "test" }) });
  });
  await page.goto(base + "/p/" + token + (options.trailingSlash ? "/" : ""));
  await page.waitForFunction(() => document.querySelector("#serial").textContent === "0001");
  await page.waitForFunction(() => document.querySelector("#shot").naturalWidth > 0);
  return { context, page, errors, mutations, failClean: () => { failClean = true; } };
}

async function readyImage(page, pathname) {
  await page.waitForFunction((expected) => {
    const image = document.querySelector("#save-preview-image");
    return !image.hidden && image.complete && image.naturalWidth > 0 && new URL(image.src).pathname === expected;
  }, pathname);
}

async function main() {
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname === markedPath || pathname === cleanPath) {
      response.setHeader("Content-Type", "image/jpeg");
      response.setHeader("Cache-Control", "no-store");
      return response.end(photo);
    }
    const assetRoot = pathname.startsWith("/static/assets/") ? path.join(__dirname, "..", "assets") : root;
    const file = pathname.startsWith("/p/") ? path.join(root, "pick.html")
      : path.resolve(assetRoot, "." + pathname.replace(/^\/static\/assets(?=\/)|^\/static/, ""));
    if ((!pathname.startsWith("/p/") && !pathname.startsWith("/static/")) ||
        !file.startsWith(assetRoot + path.sep) || !fs.existsSync(file)) {
      response.statusCode = 404;
      return response.end();
    }
    const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml" };
    response.setHeader("Content-Type", types[path.extname(file)] || "application/octet-stream");
    response.end(file.endsWith(".html") ? fs.readFileSync(file, "utf8").replaceAll("__BUILD__", "test") : fs.readFileSync(file));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, channel: "chrome" });
    const base = "http://127.0.0.1:" + server.address().port;
    for (const system of ["iPhone; CPU iPhone OS 18_0 like Mac OS X", "Linux; Android 14"]) {
      const { context, page, errors, mutations, failClean } = await mount(browser, base,
        { userAgent: "Mozilla/5.0 (" + system + ") AppleWebKit/537.36 Mobile MicroMessenger/8.0.60", trailingSlash: true });
      let downloads = 0;
      page.on("download", () => { downloads++; });
      check(await page.locator("#save-hint").isVisible(), "微信显示长按引导");
      check(await page.locator("#save").getAttribute("download") === null, "微信不依赖download");
      await page.locator("#save").click();
      await readyImage(page, markedPath);
      check((await page.locator("#save-preview-title").textContent()) === "保存带标识版", "带标识版本明确");
      check(await page.locator("#save-open-image").getAttribute("href") === markedPath, "完整图链接对应带标识版");
      check(await page.locator("#save-open-image").getAttribute("download") === null, "完整图链接可内联打开");
      await page.locator("#save-version-clean").click();
      await readyImage(page, cleanPath);
      check(await page.locator("#save-version-clean").getAttribute("aria-pressed") === "true", "无标识版选中状态");
      await page.locator("#save-preview-close").click();
      check(await page.locator("#save-preview").isHidden(), "面板可收起");
      check(await page.evaluate(() => document.activeElement.id) === "save", "收起恢复触发按钮焦点");
      await page.locator("#save-clean").click();
      await readyImage(page, cleanPath);
      if (process.env.PICK_SAVE_SCREENSHOT && system.startsWith("iPhone")) {
        await page.locator(".mark").evaluate((image) => image.decode());
        await page.screenshot({ path: process.env.PICK_SAVE_SCREENSHOT, fullPage: true });
      }
      check((await page.locator("#save-preview-title").textContent()) === "保存无标识版", "无标识按钮打开正确版本");
      check(downloads === 0 && new URL(page.url()).pathname === "/p/" + token + "/", "微信无下载事件且停留取图页");
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "手机没有横向溢出");
      await page.evaluate(() => {
        Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
        document.execCommand = () => false;
      });
      await page.locator("#save-copy-link").click();
      await page.locator("#save-link").waitFor({ state: "visible" });
      check(await page.locator("#save-link").inputValue() === base + "/p/" + token, "复制被拒时可长按取图链接");
      await page.locator("#save-version-marked").click();
      await readyImage(page, markedPath);
      failClean();
      await page.locator("#save-version-clean").click();
      await page.waitForFunction(() => document.querySelector("#save-preview-status").textContent.includes("加载失败"));
      check(await page.locator("#save-preview-image").isHidden(), "无标识加载失败不展示旧版图片");
      check(await page.locator("#save-open-image").isHidden(), "加载失败隐藏完整图片入口");
      check(!(await page.locator("#pick").getAttribute("class")).includes("is-gone"), "缺少无标识图不误报整张照片删除");
      await page.locator("#save-version-marked").click();
      await readyImage(page, markedPath);
      await page.evaluate(() => document.querySelector("#pick").classList.add("is-gone"));
      await page.waitForFunction(() => document.querySelector("#save-preview").hidden);
      check(await page.locator("#save-preview-image").getAttribute("src") === null, "删除状态清除保存面板图片");
      check(errors.length === 0 && mutations.length === 0, "微信无页面错误与写入操作");
      await context.close();
    }
    for (const userAgent of [undefined, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"]) {
      const { context, page, errors } = await mount(browser, base, { userAgent });
      check(await page.locator("#save-hint").isHidden(), "普通浏览器不显示微信提示");
      for (const [selector, suffix] of [["#save", "带标识"], ["#save-clean", "无标识"]]) {
        const pending = page.waitForEvent("download");
        await page.locator(selector).click();
        const download = await pending;
        check(download.suggestedFilename() === "0001-" + suffix + ".jpg", "下载文件名仅保留有效编号");
        check(await download.failure() === null, "普通下载成功");
        check(fs.readFileSync(await download.path()).equals(photo), "下载包含真实图片字节");
      }
      await page.locator("#save-help").click();
      await readyImage(page, markedPath);
      await page.keyboard.press("Escape");
      check(await page.locator("#save-preview").isHidden(), "普通浏览器长按兜底支持键盘关闭");
      check(errors.length === 0, "普通浏览器无页面错误");
      await context.close();
    }
    {
      const { context, page } = await mount(browser, base, { cleanAvailable: false,
        userAgent: "Mozilla/5.0 MicroMessenger/8.0.60" });
      check(await page.locator("#save-clean").isHidden(), "没有无标识版时不显示按钮");
      await page.locator("#save").click();
      await readyImage(page, markedPath);
      check(await page.locator("#save-version-clean").isHidden(), "没有无标识版时不允许切换");
      await context.close();
    }
    console.log(checks + " checks passed; 微信系统长按菜单与相册落盘仍需手机验收。");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
