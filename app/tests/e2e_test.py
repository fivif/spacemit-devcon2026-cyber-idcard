# -*- coding: utf-8 -*-
"""T-11 现场彩排：连拍成功率、时延、离网自足、降级诚实性、删除链路。

这是「开场前必须跑一遍」的脚本，跑的是**真实服务 + 真实相机**（默认）。
用法：
    python3 app/tests/e2e_test.py --n 20            # 连拍 20 次（真实相机）
    python3 app/tests/e2e_test.py --n 20 --mock     # 没有相机时用合成源
    python3 app/tests/e2e_test.py --n 100 --json    # 压测并输出机器可读结果

退出码 0 = 全部达标；非零 = 有未达标项（会逐条列出）。
"""

from __future__ import annotations

import io
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
APP = HERE.parent
DOCS = APP / "docs"
PORT = int(os.environ.get("BOOTH_E2E_PORT", "8098"))
PORT_DEGRADED = PORT + 1
BASE = "http://127.0.0.1:%d" % PORT

TARGET_SUCCESS = 0.98     # 成功率 >= 98%
TARGET_MEDIAN_S = 35.0    # 中位 <= 35s / 人
TARGET_PER_HOUR = 100     # >= 100 人 / 小时 / 工位

RESULTS: list[dict] = []


def record(name: str, ok, detail: str = "", extra: dict | None = None) -> None:
    entry = {"name": name, "ok": ok, "detail": detail}
    if extra:
        entry.update(extra)
    RESULTS.append(entry)
    flag = "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")
    print("  %s %s%s" % (flag, name, ("  · " + detail) if detail else ""), flush=True)


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None,
         base: str = BASE, timeout: float = 60.0):
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(base + path, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def wait_ready(base: str, deadline: float = 120.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            status, _, _ = http("GET", "/api/health", base=base, timeout=5.0)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_preloaded(base: str, deadline: float = 300.0) -> dict:
    """等后台预热真正结束，返回那时读到的 /api/state。

    服务的 HTTP 端口比预热线程先就绪，所以 wait_ready 通过 ≠ 模型已热。
    预热没完就开拍有两宗罪：① 第一个参会者替所有人付冷启动的钱
    （实测首帧 ~1.1s vs 热态 ~0.2s）；② 此刻 ep / warmup_ms 还是 None，
    于是「有没有拿到 AI 核」变成谎报。
    """
    end = time.time() + deadline
    state: dict = {}
    while time.time() < end:
        try:
            status, _, payload = http("GET", "/api/state", base=base, timeout=5.0)
            if status == 200:
                state = json.loads(payload)
                if (state.get("counters") or {}).get("warmup_ms") is not None:
                    return state
        except Exception:
            pass
        time.sleep(0.5)
    return state


def start_server(port: int, extra_env: dict | None = None, mock: bool = False) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # 测试永远用自己的数据目录。共用现场那份的后果是：彩排跑几轮测试，编号和
    # 「已出图」就被测试样张顶上去了，大屏上的数字就不再是活动的真实成绩。
    env["BOOTH_DATA"] = "/tmp/booth-e2e-data-%d" % port
    env.update(extra_env or {})
    args = [sys.executable, str(APP / "booth_server.py"), "--host", "127.0.0.1", "--port", str(port)]
    if mock:
        args.append("--mock-camera")
        env["BOOTH_CAMERA"] = "mock"
    log = open("/tmp/booth-e2e-%d.log" % port, "wb")
    return subprocess.Popen(args, cwd=str(APP), env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)


def stop(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def consent_token(base: str = BASE) -> str:
    _, _, payload = http("POST", "/api/consent", {"agree": True}, base=base)
    return json.loads(payload)["token"]


def one_shot(token: str, style: str | None = None, base: str = BASE, timeout: float = 90.0) -> dict:
    """拍一次并等到出图。返回 {ok, seconds, snapshot, error}。"""
    started = time.time()
    status, _, payload = http("POST", "/api/capture", {"style": style} if style else {},
                              {"X-Booth-Session": token}, base=base)
    if status != 202:
        return {"ok": False, "seconds": time.time() - started, "error": "capture HTTP %d" % status}
    job = json.loads(payload)["job"]
    end = started + timeout
    while time.time() < end:
        _, _, raw = http("GET", "/api/job/" + job, base=base, timeout=15.0)
        snapshot = json.loads(raw)
        if snapshot.get("stage") in ("done", "failed"):
            return {"ok": bool(snapshot.get("ok")), "seconds": time.time() - started,
                    "snapshot": snapshot, "error": snapshot.get("error")}
        time.sleep(0.25)
    return {"ok": False, "seconds": time.time() - started, "error": "任务超时"}


def external_refs() -> list[str]:
    """把四个页面及其静态资源扫一遍，找**必需的外部依赖**（断网就会白屏的那种）。"""
    # 取图页认令牌；这里只是查"页面本身有没有外链"，任何形状都会被回 200。
    pages = ["/", "/stage", "/operator", "/p/PLACEHOLDER-TOKEN"]
    seen: set[str] = set()
    offenders: list[str] = []
    pattern = re.compile(r"""(?:src|href)\s*=\s*["'](https?://[^"']+)["']""", re.I)
    css_url = re.compile(r"""url\(\s*["']?(https?://[^)"']+)""", re.I)
    for page in pages:
        status, _, raw = http("GET", page)
        if status != 200:
            offenders.append("%s → HTTP %d" % (page, status))
            continue
        html = raw.decode("utf-8", "replace")
        for match in pattern.findall(html):
            offenders.append("%s → %s" % (page, match))
        for asset in re.findall(r"""(?:src|href)\s*=\s*["'](/static/[^"']+)["']""", html):
            if asset in seen:
                continue
            seen.add(asset)
            status, _, payload = http("GET", asset)
            if status != 200:
                offenders.append("%s → HTTP %d" % (asset, status))
                continue
            body = payload.decode("utf-8", "replace")
            for match in pattern.findall(body) + css_url.findall(body):
                offenders.append("%s → %s" % (asset, match))
    return offenders


def main() -> int:
    count = 20
    if "--n" in sys.argv:
        count = int(sys.argv[sys.argv.index("--n") + 1])
    mock = "--mock" in sys.argv
    as_json = "--json" in sys.argv
    styles = ["natural"]          # 当前只有原色一档（STYLES 是唯一清单）

    print("== T-11 彩排 == 连拍 %d 次 · 相机=%s · 端口 %d" % (count, "mock" if mock else "real", PORT))
    proc = start_server(PORT, mock=mock)
    summary: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": count, "mock": mock}
    try:
        if not wait_ready(BASE):
            record("服务在 120s 内就绪", False, "见 /tmp/booth-e2e-%d.log" % PORT)
            return 1
        record("服务在 120s 内就绪", True)

        state = wait_preloaded(BASE)
        warmup_ms = (state.get("counters") or {}).get("warmup_ms")
        infer_ep = (state.get("infer") or {}).get("ep")
        record("预热完成（首个参会者不必等模型冷启动）", warmup_ms is not None,
               "预热 %.0f ms · AI 核 ep=%s" % (warmup_ms or 0, infer_ep),
               {"warmup_ms": warmup_ms, "ep": infer_ep})
        camera = state.get("camera") or {}
        if mock:
            record("相机状态如实上报（MOCK）", camera.get("real") is False, str(camera.get("detail")))
        else:
            record("真实相机已识别", camera.get("ok") is True and camera.get("real") is True,
                   "%s %sx%s %s" % (camera.get("device"), camera.get("width"), camera.get("height"),
                                    camera.get("fourcc")))

        # ---- 连拍 ----
        token = consent_token()
        durations: list[float] = []
        failures: list[str] = []
        degraded = 0
        ep_hits = 0
        infer_ms: list[float] = []
        record_ids: list[str] = []
        record_tokens: list[str] = []
        print("  ... 连拍进行中（每次含抓帧 + 推理 + 合成 + 入库）")
        for index in range(count):
            result = one_shot(token, styles[index % len(styles)])
            if result["ok"]:
                durations.append(result["seconds"])
                snapshot = result["snapshot"]
                if snapshot.get("degraded"):
                    degraded += 1
                if snapshot.get("ep"):
                    ep_hits += 1
                if snapshot.get("infer_ms"):
                    infer_ms.append(snapshot["infer_ms"])
                if snapshot.get("record_id"):
                    record_ids.append(snapshot["record_id"])
                if snapshot.get("token"):
                    record_tokens.append(snapshot["token"])
            else:
                failures.append("第 %d 次：%s" % (index + 1, result.get("error") or "未知"))
            if (index + 1) % 5 == 0:
                print("      %d/%d 完成（成功 %d）" % (index + 1, count, len(durations)), flush=True)

        success_rate = len(durations) / count if count else 0.0
        record("连拍成功率 >= %.0f%%" % (TARGET_SUCCESS * 100), success_rate >= TARGET_SUCCESS,
               "%d/%d = %.1f%%" % (len(durations), count, success_rate * 100),
               {"success_rate": success_rate, "failures": failures})

        if durations:
            median = statistics.median(durations)
            p95 = sorted(durations)[max(0, int(len(durations) * 0.95) - 1)]
            per_hour = 3600.0 / median if median > 0 else 0
            record("中位耗时 <= %.0fs / 人" % TARGET_MEDIAN_S, median <= TARGET_MEDIAN_S,
                   "中位 %.2fs · P95 %.2fs · 最快 %.2fs · 最慢 %.2fs"
                   % (median, p95, min(durations), max(durations)),
                   {"median_s": median, "p95_s": p95, "min_s": min(durations), "max_s": max(durations)})
            record("理论吞吐 >= %d 人/小时/工位" % TARGET_PER_HOUR, per_hour >= TARGET_PER_HOUR,
                   "%.0f 人/小时（按中位耗时折算）" % per_hour, {"per_hour": per_hour})
        else:
            record("中位耗时 <= %.0fs / 人" % TARGET_MEDIAN_S, False, "一次都没成功")
            record("理论吞吐 >= %d 人/小时" % TARGET_PER_HOUR, False, "一次都没成功")

        if infer_ms:
            record("推理耗时来自 worker 的真实读数", True,
                   "中位 %.0f ms · 区间 %.0f–%.0f ms"
                   % (statistics.median(infer_ms), min(infer_ms), max(infer_ms)),
                   {"infer_ms_median": statistics.median(infer_ms), "infer_ms_min": min(infer_ms),
                    "infer_ms_max": max(infer_ms)})
        record("AI 核命中率（拿了多少张走 EP）", ep_hits >= len(infer_ms) * 0.98 if infer_ms else None,
               "%d/%d 张走 EP SpaceMIT · %d 张降级" % (ep_hits, len(durations), degraded),
               {"ep_hits": ep_hits, "degraded": degraded})
        summary.update({"success_rate": success_rate, "median_s": statistics.median(durations) if durations else None,
                        "ep_hits": ep_hits, "degraded": degraded, "failures": failures,
                        "infer_ms_median": statistics.median(infer_ms) if infer_ms else None,
                        "warmup_ms": warmup_ms})

        # ---- 成片与取图链路 ----
        if record_tokens:
            last = record_ids[-1]
            last_token = record_tokens[-1]
            # 编号连号、可枚举，所以入口一律用令牌。这几条把"编号不再是入口"钉死：
            # 路由或前端哪天退回去，彩排立刻红。
            for path in ("/api/photo/%s.jpg" % last, "/api/thumb/%s.jpg" % last,
                         "/api/qr/%s.png" % last, "/api/photo-meta/%s.json" % last):
                status, _, _ = http("GET", path)
                record("编号取不到图：GET %s → 404" % path, status == 404, "实际 %d" % status)
            status, headers, raw = http("GET", "/api/photo/%s.jpg" % last_token)
            record("成片可下载（image/jpeg）",
                   status == 200 and headers.get("Content-Type", "").startswith("image/jpeg")
                   and len(raw) > 20000, "%s · %d B" % (last_token, len(raw)))
            # 成片只有官方主视觉这一档（16:9）。早期还多出一张 4:5 竖版，
            # 下半部分那块信息区被判定多余、已去掉 —— 这条断言兜住「比例悄悄变回去」。
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    shape = image.size
            except Exception:
                shape = None
            record("成片就是官方主视觉 16:9（1600x900）", shape == (1600, 900),
                   "%s · %s" % (last, shape))
            status, _, raw = http("GET", "/p/%s" % last_token)
            record("取图页可打开", status == 200 and b"SpacemiT" in raw)
            status, _, raw = http("GET", "/api/qr/%s.png" % last_token)
            record("二维码 PNG 可生成", status == 200 and raw[:4] == b"\x89PNG", "%d B" % len(raw))
            _, _, payload = http("GET", "/api/state")
            link_base = json.loads(payload).get("link_base") or ""
            record("取图链接指向局域网地址（不依赖外网）",
                   bool(link_base) and ("127.0.0.1" in link_base or "10." in link_base or "192.168." in link_base),
                   link_base)

        # ---- 离网自足：不能有任何断网就白屏的外部依赖 ----
        offenders = external_refs()
        record("四个页面 + 静态资源零外部依赖（断网可用）", not offenders,
               "扫描 4 个页面与其静态资源" if not offenders else "发现外部引用：" + "; ".join(offenders[:6]),
               {"external_refs": offenders})

        # ---- 删除链路 ----
        if record_tokens:
            target = record_ids[-1]
            target_token = record_tokens[-1]
            status, _, _ = http("DELETE", "/api/photo/%s" % target)
            record("拿编号删不掉（编号不是删除凭证）", status == 404, "实际 %d" % status)
            status, _, payload = http("DELETE", "/api/photo/%s" % target_token)
            deleted = json.loads(payload)
            record("参会者删除入口生效", status == 200 and deleted.get("removed") is True, str(deleted)[:120])
            status, _, _ = http("GET", "/api/photo/%s.jpg" % target_token)
            record("删除后链接立即失效", status == 404, "实际 %d" % status)
            status, _, _ = http("GET", "/p/%s" % target_token)
            record("删除后取图页仍可打开（用于显示「已删除」）", status == 200, "实际 %d" % status)

        # ---- 同意门禁在彩排里也要复核一次 ----
        status, _, payload = http("POST", "/api/capture", {"style": "natural"})
        record("彩排期间未同意拍摄仍被拦截（403）", status == 403, "实际 %d" % status)
    finally:
        stop(proc)

    # ---- 拿不到 AI 核时必须如实标 CPU 且照常出图 ----
    # 刻意用「模型不在」而不是「申请超量核」：实测在本板上申请 99 个线程照样能拿到 EP
    # （会话不硬占核，空闲的对手进程也不会把核抢走），拿它当降级场景只会得到一个假的绿。
    print("  ... 复核降级诚实性（故意让 AI 模型加载失败）")
    degraded_proc = start_server(
        PORT_DEGRADED,
        extra_env={"BOOTH_SEG_MODEL": "/opt/devcon/assets/models/__missing_for_rehearsal__.onnx"},
        mock=mock)
    try:
        if wait_ready(BASE.replace(str(PORT), str(PORT_DEGRADED))):
            base = BASE.replace(str(PORT), str(PORT_DEGRADED))
            state = wait_preloaded(base)
            infer = state.get("infer") or {}
            reason = infer.get("ep_fallback") or infer.get("error") or infer.get("preload_error")
            honest = (infer.get("ep") is False or infer.get("ok") is False) and bool(reason)
            record("拿不到 AI 核时 ep=false 且给出原因", honest,
                   "ep=%s ok=%s 原因=%s" % (infer.get("ep"), infer.get("ok"), reason),
                   {"degraded_ep": infer.get("ep"), "degraded_ok": infer.get("ok"),
                    "degraded_reason": reason})
            token = consent_token(base)
            result = one_shot(token, "natural", base=base, timeout=180.0)
            snapshot = result.get("snapshot") or {}
            record("拿不到 AI 核也照常出图（不许空手离开）",
                   bool(snapshot.get("record_id")) and snapshot.get("ep") is False,
                   "编号 %s · ep=%s · 推理 %s ms" % (snapshot.get("record_id"), snapshot.get("ep"),
                                                  snapshot.get("infer_ms")),
                   {"degraded_record": snapshot.get("record_id"),
                    "degraded_infer_ms": snapshot.get("infer_ms")})
        else:
            record("拿不到 AI 核时 ep=false 且给出原因", False, "降级实例没起来")
    finally:
        stop(degraded_proc)

    passed = sum(1 for r in RESULTS if r["ok"] is True)
    failed = [r for r in RESULTS if r["ok"] is False]
    skipped = sum(1 for r in RESULTS if r["ok"] is None)
    summary["results"] = RESULTS
    summary["passed"] = passed
    summary["failed"] = len(failed)
    summary["skipped"] = skipped
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "彩排记录.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    print("\n== 结果：%d PASS / %d FAIL / %d SKIP ==" % (passed, len(failed), skipped))
    for item in failed:
        print("  未达标：%s  %s" % (item["name"], item["detail"]))
    print("明细已写入 app/docs/彩排记录.json")
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())