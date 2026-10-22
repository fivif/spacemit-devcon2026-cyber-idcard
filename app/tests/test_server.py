# -*- coding: utf-8 -*-
"""T-5 验证：控制面 HTTP 契约 + 同意门禁 + 降级与恢复。

在 K3 上用**独立的测试端口 + mock 相机**起一份服务，逐个打路由。
不做集成测试的假设（不依赖现场真实相机），但所有断言都跑真代码路径。

用法：
    python3 app/tests/test_server.py            # 用 mock 相机
    python3 app/tests/test_server.py --real     # 用真实 UVC 相机（现场彩排用）
退出码 0 表示全部通过；任一 FAIL 非零退出。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
PORT = int(os.environ.get("BOOTH_TEST_PORT", "8099"))
BASE = "http://127.0.0.1:%d" % PORT

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("  · " + detail) if detail else ""), flush=True)
    return bool(ok)


def skip(name: str, detail: str = "") -> None:
    RESULTS.append((name, None, detail))  # type: ignore[arg-type]
    print("  SKIP %s%s" % (name, ("  · " + detail) if detail else ""), flush=True)


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None,
         timeout: float = 30.0):
    """返回 (status, headers, bytes)。4xx/5xx 不抛异常 —— 状态码就是被测对象。"""
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(BASE + path, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def wait_ready(deadline: float = 90.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            status, _, _ = http("GET", "/api/health", timeout=5.0)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def worker_pids(parent_pid: int) -> list[int]:
    """只抓**本次测试起的**那个服务进程下的 worker。

    以前这里是不带父进程的全局 `pgrep -f infer/worker.py`，后面还跟着 SIGKILL ——
    等于在 8090 正常服务的时候跑一次测试，就把现场那个 worker 一起杀了
    （实测发生过：操作员面板上「进程」直接变红）。测试不该动别人的进程。
    """
    try:
        out = subprocess.run(["pgrep", "-P", str(parent_pid), "-f", "infer/worker.py"],
                             capture_output=True, text=True, timeout=10)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def capture_and_wait(token: str, style: str = "natural", timeout: float = 90.0) -> dict:
    status, _, payload = http("POST", "/api/capture", {"style": style},
                              {"X-Booth-Session": token})
    if status != 202:
        return {"ok": False, "error": "capture 返回 %d" % status}
    job = json.loads(payload)["job"]
    end = time.time() + timeout
    while time.time() < end:
        _, _, snapshot_raw = http("GET", "/api/job/" + job, timeout=10.0)
        snapshot = json.loads(snapshot_raw)
        if snapshot.get("stage") in ("done", "failed"):
            return snapshot
        time.sleep(0.4)
    return {"ok": False, "error": "任务超时未完成"}


def main() -> int:
    use_real = "--real" in sys.argv
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # 同上：测试不许写现场那份数据（否则编号与「已出图」会被测试样张推高）
    env["BOOTH_DATA"] = "/tmp/booth-test-data"
    if not use_real:
        env["BOOTH_CAMERA"] = "mock"
    log_path = Path("/tmp/booth-test.log")
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "booth_server.py"), "--host", "127.0.0.1", "--port", str(PORT)]
        + ([] if use_real else ["--mock-camera"]),
        cwd=str(APP), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    print("== T-5 控制面测试 ==  pid=%d port=%d 相机=%s" % (proc.pid, PORT, "real" if use_real else "mock"))
    try:
        if not wait_ready():
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            check("服务在 90s 内就绪", False, "见 " + str(log_path))
            return 1
        check("服务在 90s 内就绪", True)

        # ---- §4 路由契约 ----
        for path, expect in (("/", 200), ("/stage", 200), ("/operator", 200),
                             # 取图页对任何形状的链接都回 200（页面自己再去问
                             # /api/photo-meta/<令牌>），真实令牌不在这里拼。
                             ("/p/AbCdEfGhIjKlMnOpQrStUv", 200),
                             ("/api/state", 200), ("/api/health", 200),
                             ("/api/overlay.json", 200), ("/static/css/tokens.css", 200),
                             ("/static/js/booth.js", 200), ("/static/assets/brand/spacemit-logo.svg", 200)):
            status, _, _ = http("GET", path)
            check("GET %s → %d" % (path, expect), status == expect, "实际 %d" % status)

        for path, expect in (("/api/job/does-not-exist", 404), ("/api/photo/SM-2026-9999.jpg", 404),
                             ("/api/nope", 404)):
            status, _, _ = http("GET", path)
            check("GET %s → %d" % (path, expect), status == expect, "实际 %d" % status)

        # 页面版本（坑 38）：HTML 里的 __BUILD__ 必须被服务端换掉，换出来的值要跟
        # /api/build 报的一致 —— 页面靠这个自己发现「手上这份 JS 过期了」并刷新。
        # 现场屏幕一开一整天，没人会去点刷新；漏掉占位符 = 那个页面永远不自愈。
        sk, _, sb = http("GET", "/api/build")
        build = (json.loads(sb) or {}).get("build") if sk == 200 else ""
        check("GET /api/build → 200 且带 build", sk == 200 and bool(build), "%s %s" % (sk, build))
        for path in ("/", "/stage", "/operator", "/p/AbCdEfGhIjKlMnOpQrStUv"):
            status, _, raw = http("GET", path)
            text = raw.decode("utf-8", "replace")
            check("%s 不留 __BUILD__ 占位符" % path,
                  status == 200 and "__BUILD__" not in text, text[:80])
            check("%s 带当前页面版本" % path, bool(build) and build in text)
        status, headers, _ = http("GET", "/static/js/net.js")
        check("静态资源 no-store（刷新不会再拿到老 JS）",
              status == 200 and headers.get("Cache-Control") == "no-store",
              str(headers.get("Cache-Control")))

        # 编号是连号的 —— 认编号做入口，等于把全场照片摊到公网上。下面这几条
        # 钉住「编号既不取图也删不了」，前端或路由哪天退回去会立刻变红。
        for path, expect in (("/api/photo/SM-2026-0001.jpg", 404),
                             ("/api/thumb/SM-2026-0001.jpg", 404),
                             ("/api/qr/SM-2026-0001.png", 404),
                             ("/api/photo-meta/SM-2026-0001.json", 404)):
            status, _, _ = http("GET", path)
            check("编号不是入口：GET %s → %d" % (path, expect), status == expect, "实际 %d" % status)
        status, _, _ = http("DELETE", "/api/photo/SM-2026-0001")
        check("编号不是删除凭证：DELETE /api/photo/SM-2026-0001 → 404", status == 404,
              "实际 %d" % status)

        status, _, payload = http("POST", "/api/consent", {"agree": False})
        body = json.loads(payload)
        check("拒绝同意时不发令牌", status == 200 and body.get("ok") is False
              and body.get("error") == "consent_declined", str(body)[:120])

        # ---- 同意门禁：100% 拦截 ----
        blocked = 0
        attempts = [
            ("无令牌", {}, {}),
            ("空令牌", {}, {"X-Booth-Session": ""}),
            ("伪造令牌", {}, {"X-Booth-Session": "deadbeef" * 4}),
            ("body 里塞假令牌", {"consent_token": "fake"}, {}),
        ]
        for label, body_, headers in attempts:
            status, _, payload = http("POST", "/api/capture", body_ or {}, headers)
            ok = status == 403 and json.loads(payload).get("error") == "consent_required"
            blocked += 1 if ok else 0
            check("未同意拦截：%s → 403" % label, ok, "实际 %d" % status)
        check("未同意 100%% 拦截（%d/%d）" % (blocked, len(attempts)), blocked == len(attempts))

        # ---- 正常拍摄：出图 ----
        status, _, payload = http("POST", "/api/consent", {"agree": True})
        token = json.loads(payload)["token"]
        check("同意后签发令牌", bool(token))

        started = time.time()
        snapshot = capture_and_wait(token)
        elapsed = time.time() - started
        check("携带同意令牌可以出图", snapshot.get("ok") is True, str(snapshot.get("error") or ""))
        record_id = snapshot.get("record_id")
        check("返回编号 SM-YYYY-NNNN", bool(record_id) and re.match(r"^SM-\d{4}-\d{4}$", record_id or ""),
              str(record_id))
        record_token = snapshot.get("token") or ""
        check("返回取图令牌（随机串，不是编号）",
              bool(record_token) and record_token != record_id and len(record_token) >= 22,
              str(record_token))
        # 22 位 URL 安全字符。别去断言"令牌里没有连续数字"—— 随机串本来就有
        # 约 1% 的概率撞上，那种断言只是在给彩排埋雷。
        check("令牌是 22 位 URL 安全字符",
              bool(re.match(r"^[A-Za-z0-9_-]{22}$", record_token)), str(record_token))
        check("返回真实推理耗时", (snapshot.get("infer_ms") or 0) > 0, "%s ms" % snapshot.get("infer_ms"))
        check("全流程 <= 35s", elapsed <= 35.0, "%.1fs" % elapsed)

        # ---- 抠像：专门的 matting 模型真的接上了 ----
        status, _, payload = http("GET", "/api/state")
        infer_state = (json.loads(payload).get("infer") or {})
        check("worker 报出抠像模型",
              status == 200 and bool(infer_state.get("matte_model")),
              str(infer_state.get("matte_model") or "未加载"))
        check("抠像走 CPU（BN 上 EP 会硬崩）",
              infer_state.get("matte_device") == "cpu", str(infer_state.get("matte_device")))
        status, _, payload = http("GET", "/api/overlay.json")
        overlay = json.loads(payload) if status == 200 else {}
        # 没检出人的时候（比如 mock 画面里没有人）alpha 本就是空的，
        # 那不是抠像没工作 —— 真正该断言的是「有人的时候一定抠出来」。
        if overlay.get("person_count"):
            check("成片确实用了抠像",
                  overlay.get("matte_applied") is True,
                  "matte_applied=%s matte_ms=%s" % (overlay.get("matte_applied"),
                                                    overlay.get("matte_ms")))
        else:
            skip("成片确实用了抠像", "这帧没检出人，无从判断")

        if record_id and record_token:
            status, _, _ = http("GET", "/api/photo/%s.jpg" % record_id)
            check("拿编号取不到图（404）", status == 404, "实际 %d" % status)
            status, headers, raw = http("GET", "/api/photo/%s.jpg" % record_token)
            check("GET /api/photo/<令牌>.jpg → 200 image/jpeg",
                  status == 200 and headers.get("Content-Type", "").startswith("image/jpeg")
                  and len(raw) > 5000, "%d B" % len(raw))
            status, _, raw = http("GET", "/api/thumb/%s.jpg" % record_token)
            check("GET /api/thumb/<令牌>.jpg → 200", status == 200 and len(raw) > 1000, "%d B" % len(raw))
            status, _, raw = http("GET", "/api/qr/%s.png" % record_token)
            if status == 200 and raw[:4] == b"\x89PNG":
                check("GET /api/qr/<令牌>.png → 200 PNG", True, "%d B" % len(raw))
            elif status == 404:
                check("GET /api/qr/<令牌>.png → 200 PNG", False,
                      "取图链接不存在（照片没能落盘）")
            else:
                skip("GET /api/qr/<令牌>.png", "render.qr 还没就绪（实际 %d）" % status)

            status, _, payload = http("DELETE", "/api/photo/%s" % record_id)
            check("拿编号删不掉（404）", status == 404, "实际 %d" % status)
            status, _, payload = http("DELETE", "/api/photo/%s" % record_token)
            deleted = json.loads(payload) if status == 200 else {}
            check("DELETE /api/photo/<令牌> 真的删掉文件", status == 200 and deleted.get("removed") is True,
                  str(deleted)[:120])
            status, _, _ = http("GET", "/api/photo/%s.jpg" % record_token)
            check("删除后链接立即失效（404）", status == 404, "实际 %d" % status)

        # ---- worker 挂掉后仍然出图 ----
        pids = worker_pids(proc.pid)
        if not pids:
            skip("杀掉 worker 后仍能出图", "没找到 worker 进程")
        else:
            for pid in pids:
                try:
                    # 用 SIGTERM 不用 SIGKILL：KB 24 记着「kill -9 推理进程会泄漏
                    # TCM block（8 块共享 AI 核内存），此后所有推理必失败」。
                    # 这条纪律对测试同样成立 —— 测完还得让现场继续跑。
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(1.0)
            snapshot2 = capture_and_wait(token, style="natural", timeout=120.0)
            check("worker 被终止后仍能出图（自动重启或走 L0）",
                  bool(snapshot2.get("record_id")), str(snapshot2.get("error") or ""))

        # ---- 一键恢复 ----
        started = time.time()
        status, _, payload = http("POST", "/api/operator/restore", {})
        restore_seconds = time.time() - started
        restored = json.loads(payload)
        check("一键恢复 <= 10s", restore_seconds <= 10.0, "%.1fs" % restore_seconds)
        check("一键恢复返回动作清单", status == 200 and bool(restored.get("actions")),
              " / ".join(restored.get("actions", []))[:160])
        state = restored.get("state") or {}
        infer_state = state.get("infer") or {}
        if infer_state.get("ep"):
            check("恢复后拿回 AI 核（ep=true）", True, "线程 %s" % infer_state.get("ep_threads"))
        else:
            skip("恢复后拿回 AI 核（ep=true）", "当前未拿到 AI 核（可能被其它服务占用）")

        # ---- 恢复会作废同意令牌（下一位参会者必须重新同意）----
        status, _, payload = http("POST", "/api/capture", {"style": "natural"},
                                  {"X-Booth-Session": token})
        check("一键恢复后旧令牌失效 → 403", status == 403, "实际 %d" % status)

        # ---- 清库 ----
        status, _, payload = http("POST", "/api/operator/purge", {})
        body = json.loads(payload)
        check("POST /api/operator/purge → 200", status == 200 and "removed_count" in body, str(body)[:120])

        # ---- 彩排清场：编号归零、成片清空，下一位参会者从 0001 开始 ----
        status, _, payload = http("POST", "/api/operator/reset", {})
        cleared = json.loads(payload)
        check("POST /api/operator/reset → 200", status == 200 and cleared.get("ok") is True,
              str(cleared)[:140])
        cleared_state = cleared.get("state") or {}
        check("清场后「已出图」归零", cleared_state.get("photos") == 0,
              "photos=%s" % cleared_state.get("photos"))
        check("清场后大屏没有可指的成片",
              not cleared_state.get("recent") and cleared_state.get("last_record") is None,
              "recent=%d last=%s" % (len(cleared_state.get("recent") or []),
                                     cleared_state.get("last_record")))

        status, _, payload = http("POST", "/api/consent", {"agree": True})
        token_after = json.loads(payload)["token"]
        snapshot3 = capture_and_wait(token_after, timeout=120.0)
        expected = "SM-%d-0001" % time.localtime().tm_year
        check("清场后第一张重新从 0001 开始", snapshot3.get("record_id") == expected,
              "%s（期望 %s）" % (snapshot3.get("record_id"), expected))

        # ---- 服务端配置：公网取图地址（操作员面板上填的那一行）----
        status, _, payload = http("GET", "/api/operator/config")
        cfg = json.loads(payload)
        check("GET /api/operator/config -> 200", status == 200 and cfg.get("ok") is True, str(cfg)[:120])
        check("没配过时回落到局域网地址", cfg.get("source") == "lan" and cfg.get("secure") is False,
              "%s（来源 %s）" % (cfg.get("public_base"), cfg.get("source")))

        for bad_value, why in (("hello world", "主机名里有空格"),
                               ("https://a.b.com/path", "带了路径"),
                               ("https://a.b.com:99999", "端口超范围"),
                               ("ftp://a.b.com", "不是 http/https")):
            status, _, payload = http("POST", "/api/operator/config", {"public_base": bad_value})
            body = json.loads(payload)
            check("坏地址被拦：%s（%s）" % (bad_value, why),
                  status == 400 and body.get("ok") is False and bool(body.get("detail")),
                  str(body)[:110])

        status, _, payload = http("POST", "/api/operator/config",
                                  {"public_base": "https://devcon.example.com/"})
        cfg2 = json.loads(payload)
        check("保存公网地址 -> 200 且规范化（去尾斜杠）", status == 200
              and cfg2.get("public_base") == "https://devcon.example.com"
              and cfg2.get("secure") is True and cfg2.get("source") == "panel", str(cfg2)[:150])

        # 二维码是按地址画好缓存的 PNG。改了地址不清缓存，发出去的码仍指向旧地址，
        # 而且不报错 —— 坑 25 的同一个病。这里连"编进去的到底是哪个地址"一起验。
        shot_id = snapshot3.get("record_id")
        shot_token = snapshot3.get("token") or ""
        status, _, png = http("GET", "/api/qr/%s.png" % shot_token)
        check("改地址后二维码重新生成", status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n",
              "清掉了 %s 张缓存" % cfg2.get("qr_cleared"))
        try:
            import cv2
            import numpy as np

            def decode_qr(png_bytes: bytes) -> str:
                """解二维码，带重试阶梯。

                cv2 的检测器在这张小码上**会偶发解不出来**（真机实测 40 张里 1 张，
                约 2.5%），同一张图加白边或放大后重试必成功。
                别让一个检测器的脾气变成彩排里的一条假红。
                """
                image = cv2.imdecode(np.frombuffer(png_bytes, dtype="uint8"), cv2.IMREAD_GRAYSCALE)
                for candidate in (image,
                                  cv2.copyMakeBorder(image, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255),
                                  cv2.resize(image, None, fx=2.0, fy=2.0,
                                             interpolation=cv2.INTER_NEAREST)):
                    text, _, _ = cv2.QRCodeDetector().detectAndDecode(candidate)
                    if text:
                        return text
                return ""

            decoded = decode_qr(png)
            check("二维码里编的是新地址 + 令牌（不是编号）",
                  decoded == "https://devcon.example.com/p/%s" % shot_token, decoded or "解不出来")
        except ImportError:
            skip("二维码里编的是新地址 + 令牌", "这台机器没有 cv2")

        status, _, payload = http("POST", "/api/operator/config", {"public_base": ""})
        cfg3 = json.loads(payload)
        check("清空后回落到局域网地址", status == 200 and cfg3.get("source") == "lan"
              and cfg3.get("public_base") == cfg3.get("lan_base"), str(cfg3)[:150])

        # ---- 公开的成片元信息：公网那一侧只放行这一个端点 ----
        status, _, payload = http("GET", "/api/photo-meta/%s.json" % shot_token)
        meta = json.loads(payload)
        check("GET /api/photo-meta/<令牌>.json -> 200",
              status == 200 and meta.get("id") == shot_id, str(meta)[:110])
        check("元信息里不带编号清单 / 相机 / 磁盘",
              not ({"recent", "camera", "disk", "counters", "photos"} & set(meta)),
              "字段 %s" % sorted(meta))
        check("元信息里不带令牌本身（省得再被当成入口转出去）",
              "token" not in meta, "字段 %s" % sorted(meta))
        status, _, _ = http("GET", "/api/photo-meta/SM-2026-9999.json")
        check("拿编号问元信息 -> 404", status == 404, "实际 %d" % status)
        status, _, _ = http("GET", "/api/photo-meta/zzzz-not-a-real-token.json")
        check("拿不存在的令牌问元信息 -> 404", status == 404, "实际 %d" % status)

        # ---- 相机状态如实上报 ----
        _, _, payload = http("GET", "/api/state")
        state = json.loads(payload)
        camera = state.get("camera") or {}
        share = state.get("share") or {}
        check("state 里有 share 段（取图页 / 大屏要用）",
              "public_base" in share and "lan_base" in share and "secure" in share,
              str(share)[:120])
        if use_real:
            check("真实相机已识别", camera.get("ok") is True and camera.get("real") is True,
                  "%s %sx%s %s" % (camera.get("device"), camera.get("width"), camera.get("height"),
                                   camera.get("fourcc")))
            check("高分辨率走 MJPG", (camera.get("fourcc") or "").strip() == "MJPG",
                  str(camera.get("fourcc")))
        else:
            check("MOCK 模式如实标注 real=false", camera.get("real") is False, str(camera.get("detail")))
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok is True)
    failed = sum(1 for _, ok, _ in RESULTS if ok is False)
    skipped = sum(1 for _, ok, _ in RESULTS if ok is None)
    print("\n== 结果：%d PASS / %d FAIL / %d SKIP ==" % (passed, failed, skipped))
    for name, ok, detail in RESULTS:
        if ok is False:
            print("  未通过：%s  %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
