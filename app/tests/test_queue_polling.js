"use strict";
const assert = require("node:assert/strict");
const flow = require("../static/auto-flow.js");

let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
  console.log("PASS " + message);
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
function response(status) {
  return { status: 200, payload: { entry: { queue_id: "queue-1", status } } };
}
function fixture() {
  const requests = [];
  const updates = [];
  const errors = [];
  const timers = new Map();
  let timerId = 0;
  const poller = flow.createQueuePoller({
    timers: {
      setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
      clearTimeout(id) { timers.delete(id); },
    },
    fetchEntry() { const pending = deferred(); requests.push(pending); return pending.promise; },
    onUpdate(out) { updates.push(out); },
    onError(out) { errors.push(out); },
  });
  return { poller, requests, updates, errors, timers };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

async function main() {
  for (const oldStatus of ["called", "cancelled", "failed"]) {
    const fixtureState = fixture();
    const { poller, requests, updates, errors, timers } = fixtureState;
    poller.start();
    await flush();
    const pendingTick = poller.tick();
    check(requests.length === 1, "慢请求不叠加轮询：" + oldStatus);
    poller.stop("start");
    requests[0].resolve(response(oldStatus));
    await pendingTick;
    check(updates.length === 0 && errors.length === 0 && timers.size === 0,
      "开始拍摄后忽略旧响应且不重启轮询：" + oldStatus);
    poller.start();
    await flush();
    requests[1].resolve(response("capturing"));
    await flush();
    check(updates.length === 1 && updates[0].entry.status === "capturing" && timers.size === 1,
      "新一轮正确显示拍摄状态：" + oldStatus);
    poller.stop();
  }
  {
    const { poller, requests, updates, errors, timers } = fixture();
    poller.start();
    await flush();
    poller.stop("reset");
    poller.start();
    await flush();
    requests[1].resolve(response("processing"));
    await flush();
    requests[0].reject(new Error("old connection"));
    await flush();
    check(updates.length === 1 && updates[0].entry.status === "processing" && errors.length === 0,
      "重启后旧网络错误不覆盖新状态");
    check(timers.size === 1 && poller.attempts() === 0, "重启仅保留新轮询定时器");
    const nextTick = poller.tick();
    await flush();
    check(timers.size === 0 && requests.length === 3, "手动刷新清理待执行轮询，不产生双链");
    requests[2].resolve(response("ready_for_pickup"));
    await nextTick;
    check(!poller.isRunning() && updates.at(-1).entry.status === "ready_for_pickup" && timers.size === 0,
      "成片就绪仍停止轮询");
  }
  for (const status of ["cancelled", "failed", "expired"]) {
    const { poller, requests, updates, timers } = fixture();
    poller.start();
    await flush();
    requests[0].resolve(response(status));
    await flush();
    check(updates[0].entry.status === status && !poller.isRunning() && timers.size === 0, "真实终态保留：" + status);
    await poller.tick();
    check(requests.length === 1 && updates.length === 1, "终态后手动tick也不请求已清理记录：" + status);
  }
  {
    const { poller, requests, updates, errors, timers } = fixture();
    poller.start();
    await flush();
    requests[0].resolve({ status: 404, payload: { error: { code: "QUEUE_NOT_FOUND" } } });
    await flush();
    await poller.tick();
    check(updates.length === 1 && updates[0].stop && !updates[0].entry && errors.length === 0,
      "真实404只交付一次缺失结果，不伪造failed");
    check(requests.length === 1 && timers.size === 0 && !poller.isRunning() && poller.stoppedReason() === "gone",
      "缺失记录停止轮询，无重复提示或请求");
  }
  {
    const { poller, requests, errors, timers } = fixture();
    poller.start();
    await flush();
    requests[0].reject(new Error("current connection"));
    await flush();
    check(errors.length === 1 && poller.isRunning() && [...timers.values()][0].delay === 4000,
      "当前网络错误正常退避重试");
    poller.stop();
  }
  check(flow.queueStatusView({ status: "failed" }).title === "生成未完成" &&
    flow.queueStatusView({ status: "failed" }).detail.includes("再试"), "failed不依赖原因字段，提示生成失败与重试");
  check(flow.queueStatusView({ status: "cancelled" }).title === "排队已取消", "真实用户取消仍显示排队已取消");
  console.log("\n" + checks + " checks passed; no network requests.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
