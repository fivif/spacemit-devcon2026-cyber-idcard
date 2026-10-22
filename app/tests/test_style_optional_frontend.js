"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");
const { mount } = require("./test_operator_frontend");
const root = path.join(__dirname, "../static");

async function main() {
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    const file = pathname.startsWith("/operator") ? path.join(root, "operator.html")
      : path.resolve(root, "." + pathname.slice("/static".length));
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) {
      response.statusCode = 404;
      return response.end();
    }
    response.setHeader("Content-Type", file.endsWith(".html") ? "text/html; charset=utf-8"
      : file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "image/svg+xml");
    response.end(fs.readFileSync(file, "utf8").replaceAll("__BUILD__", "test"));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const runtime = await mount(browser, "http://127.0.0.1:" + server.address().port, "styles");
    const { page, calls, errors } = runtime;
    for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await page.getByRole("button", { name: "新增风格", exact: true }).click();
      assert.equal(await page.getByLabel("竖版生成说明", { exact: true }).isVisible(), true);
      assert.equal(await page.getByLabel("横版生成说明", { exact: true }).isVisible(), false);
      assert.equal(await page.getByLabel("统一避免内容（选填）", { exact: true }).inputValue(), "");
      const name = "竖版风格" + viewport.width;
      await page.getByLabel("风格名称", { exact: true }).fill(name);
      await page.getByRole("button", { name: "保存风格", exact: true }).click();
      assert.equal(await page.getByRole("dialog").isVisible(), true);
      const prompt = "Create a clean portrait with natural proportions, soft lighting and hand-painted details.";
      await page.getByLabel("竖版生成说明", { exact: true }).fill(prompt);
      await page.getByRole("button", { name: "保存风格", exact: true }).click();
      await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
      const saved = calls.find((call) => call.method === "POST" && call.payload.title === name).payload;
      assert.equal(saved.vertical_prompt, prompt);
      assert.equal(saved.wide_prompt, "");
      assert.equal(saved.negative_prompt, "");
      assert.deepEqual(saved.sizes, ["1024x1536"]);
      const row = page.locator(".style-row").filter({ has: page.getByText(name, { exact: true }) });
      await row.getByRole("button", { name: "编辑", exact: true }).click();
      assert.equal(await page.getByLabel("竖版生成说明", { exact: true }).inputValue(), prompt);
      assert.equal(await page.getByLabel("统一避免内容（选填）", { exact: true }).inputValue(), "");
      await page.getByLabel("统一避免内容（选填）", { exact: true }).fill("不要花纹");
      await page.getByRole("button", { name: "保存风格", exact: true }).click();
      await page.waitForFunction(() => !document.querySelector("#style-dialog").open);
      assert.ok(calls.some((call) => call.payload && call.payload.title === name && call.payload.negative_prompt === "不要花纹"));
    }
    assert.deepEqual(errors, []);
    await runtime.context.close();
    console.log("PASS: desktop/mobile portrait-only create, edit, reopen, optional short negative and required portrait validation");
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
