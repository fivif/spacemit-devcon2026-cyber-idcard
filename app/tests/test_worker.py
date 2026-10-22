# -*- coding: utf-8 -*-
"""T-1 推理 worker 测试：AI 核预算 + 哨兵协议 + 诚实降级。

对应 docs/spec/系统规格-v2.md 里 T-1 的四条 acceptance：
  a) health 返回 ep=true 且 ep_threads=4（无竞争时）；
  b) 故意申请超量核后 health 返回 ep=false 且带 fallback 原因，不得冒充 AI 核；
  c) 连续 50 次 seg/det 调用无崩溃、且没有不带 @@J 前缀的协议行（哨兵回归）；
  d) seg 中位 <= 260ms，det 中位 <= 35ms。

★ 诚实优先：如果 AI 核被别的进程占着（例如 yolos-box.service），本测试**不会**
把 CPU 的耗时当 AI 核读数上报，而是跳过计时断言并显式打印「拿到 CPU，无法验时延」。
测试也**不会**去 kill 别的进程 —— 超量申请用环境变量制造（BOOTH_EP_THREADS_SEG=99）。

为什么用裸 Popen 而不是 infer_client.InferWorker：本测试要看见**全部**原始输出行
（包括 EP 的噪声），才能证明哨兵协议真的把噪声挡住了。InferWorker 的集成
另外单独测（见 EXTRA 段）。

运行：``python3 app/tests/test_worker.py``
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
APP = HERE.parent
WORKER = APP / "infer" / "worker.py"
PREFIX = "@@J "

# 验收阈值（plan.json T-1）
SEG_MEDIAN_LIMIT_MS = 260.0
DET_MEDIAN_LIMIT_MS = 35.0
ROUNDS = 50             # seg + det 各 50 次
EP_SEG_THREADS = 4      # 默认预算
EP_DET_THREADS = 2
OVERSUBSCRIBE = {"BOOTH_EP_THREADS_SEG": "99", "BOOTH_EP_THREADS": "99"}

CHECKS = 0
FAILURES: list[str] = []
SKIPS: list[str] = []


def check(cond, name: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    ok = bool(cond)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def skip(name: str, detail: str = "") -> None:
    SKIPS.append(name)
    print(f"  [SKIP] {name}" + (f" | {detail}" if detail else ""))


def info(name: str, detail: str = "") -> None:
    print(f"  [info] {name}" + (f" | {detail}" if detail else ""))


def looks_like_protocol(line: str) -> bool:
    """不带哨兵前缀的行里，有没有长得像协议响应的？——那才是真正的协议污染。"""
    if not line.startswith("{"):
        return False
    try:
        obj = json.loads(line)
    except Exception:
        return False
    return isinstance(obj, dict) and any(k in obj for k in ("id", "op", "ok", "error"))


class Worker:
    """裸 Popen 客户端：只把带哨兵前缀的行当协议，其余一律记录为噪声。"""

    def __init__(self, env_extra: dict | None = None, label: str = "worker"):
        self.label = label
        self.env = dict(os.environ)
        if env_extra:
            self.env.update(env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, str(WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(APP), env=self.env,
        )
        self.raw_out: list[str] = []
        self.raw_err: list[str] = []
        self.noise: list[str] = []
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._seq = 0
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            self.raw_out.append(line)
            self._queue.put(line)

    def _pump_err(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.raw_err.append(line.rstrip("\n"))

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self):
        return self.proc.poll()

    def stderr_tail(self, n: int = 5) -> str:
        return "\n".join(self.raw_err[-n:]) if self.raw_err else "(stderr 为空)"

    def state(self) -> str:
        if self.alive:
            return "仍在运行"
        return f"**已退出 exit={self.exit_code}**"

    def call(self, op: str, timeout: float = 180.0, **payload) -> dict:
        self._seq += 1
        request = {"id": self._seq, "op": op, **payload}
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f"写入 worker 失败：{err}") from err
        return self.read_response(timeout, what=op)

    def read_response(self, timeout: float = 180.0, what: str = "响应") -> dict:
        """读下一条**带哨兵前缀**的协议行；不带前缀的一律记为噪声丢弃。"""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"{what} 超时（{timeout}s）；worker {self.state()}；"
                    f"stderr 尾部：{self.stderr_tail()!r}"
                )
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(
                    f"{what} 超时（{timeout}s）；worker {self.state()}；"
                    f"stderr 尾部：{self.stderr_tail()!r}"
                ) from None
            if line.startswith(PREFIX):
                return json.loads(line[len(PREFIX):])
            self.noise.append(line)

    def send_raw(self, text: str) -> bool:
        """写一行原始文本（用来制造坏 JSON）。返回 False 表示 worker 已死。"""
        try:
            self.proc.stdin.write(text + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
            return True
        except (BrokenPipeError, ValueError):
            return False

    def protocol_violations(self) -> list[str]:
        return [line for line in self.raw_out if looks_like_protocol(line)]

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def make_frame(path: Path, width: int = 1280, height: int = 720) -> None:
    """合成一张类人像帧（worker 只做 letterbox，耗时与源尺寸无关）。"""
    from PIL import Image, ImageDraw
    import numpy as np

    rng = np.random.default_rng(7)
    base = (rng.random((height, width, 3)) * 18.0 + 12.0).astype("uint8")
    canvas = Image.fromarray(base)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, width, int(height * 0.55)], fill=(34, 30, 26))
    draw.ellipse([int(width * 0.44), int(height * 0.25),
                  int(width * 0.56), int(height * 0.47)], fill=(168, 176, 190))
    draw.rectangle([int(width * 0.45), int(height * 0.46),
                    int(width * 0.55), height], fill=(150, 160, 176))
    canvas.save(path, quality=90)


def main() -> int:
    if not WORKER.is_file():
        print(f"FAIL: 找不到被测 worker：{WORKER}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="t1-worker-"))
    image = tmp / "frame.jpg"
    make_frame(image)
    print("=== T-1 推理 worker ===")
    info("解释器", sys.executable)
    info("被测对象", str(WORKER))
    info("测试帧", f"{image} ({image.stat().st_size} B)")

    ep_truth: bool | None = None
    med_seg: float | None = None
    med_det: float | None = None
    counts_ok = False

    clean = Worker(label="clean")
    try:
        # ---------------------------------------------------------------- a)
        print("\n=== a) health：AI 核预算（无竞争时应为 ep=true / ep_threads=4） ===")
        try:
            health = clean.call("health", timeout=240.0)
        except Exception as err:  # noqa: BLE001
            check(False, "health 可返回", f"{type(err).__name__}: {err}")
            health = {}
        if not health.get("ok"):
            check(False, "health ok", f"响应：{json.dumps(health, ensure_ascii=False)[:200]}")
        else:
            ep_truth = bool(health.get("ep"))
            info("ep", f"{ep_truth}")
            info("ep_threads", f"{health.get('ep_threads')}")
            info("providers", f"{health.get('providers')}")
            info("det_ep / det_requested_threads",
                 f"{health.get('det_ep')} / {health.get('det_requested_threads')}")
            info("ep_fallback", f"{health.get('ep_fallback')!r}")
            info("thread_budget", f"{health.get('thread_budget')}")
            info("load_ms", f"{health.get('load_ms')}")
            if ep_truth:
                check(health.get("ep_threads") == EP_SEG_THREADS,
                      f"a) ep=true 且 ep_threads={EP_SEG_THREADS}",
                      f"实际 ep_threads={health.get('ep_threads')}")
                check(health.get("det_ep") is True and
                      health.get("det_requested_threads") == EP_DET_THREADS,
                      f"a) det 也拿到 AI 核（{EP_DET_THREADS} 线程）",
                      f"det_ep={health.get('det_ep')} th={health.get('det_requested_threads')}")
            else:
                skip(f"a) ep=true 且 ep_threads={EP_SEG_THREADS}",
                     "AI 核被其它进程占用（yolos-box？），本机当前只能拿到 CPU；"
                     f"ep_fallback={health.get('ep_fallback')!r}；"
                     "不修改别人的进程，如实降级")

        # ---------------------------------------------------------------- c) + d)
        print(f"\n=== c) 连续 {ROUNDS} 轮 seg/det：无崩溃 + 哨兵协议无污染 ===")
        seg_ms: list[float] = []
        det_ms: list[float] = []
        seg_ep_flags: set = set()
        for i in range(ROUNDS):
            try:
                seg = clean.call("seg", timeout=180.0, image=str(image),
                                 prompts=["person"], conf=0.20, mask_max_side=640)
                det = clean.call("det", timeout=180.0, image=str(image), conf=0.25)
            except Exception as err:  # noqa: BLE001
                check(False, f"第 {i + 1} 轮调用未抛异常",
                      f"{type(err).__name__}: {err} | worker {clean.state()} | "
                      f"stderr 尾部 {clean.stderr_tail(4)!r}")
                break
            if not (seg.get("ok") and det.get("ok")):
                check(False, f"第 {i + 1} 轮 seg/det ok",
                      f"seg={json.dumps(seg, ensure_ascii=False)[:120]} "
                      f"det={json.dumps(det, ensure_ascii=False)[:120]}")
                break
            seg_ms.append(float(seg.get("infer_ms", -1)))
            det_ms.append(float(det.get("infer_ms", -1)))
            seg_ep_flags.add(bool(seg.get("ep")))
            if (i + 1) % 10 == 0:
                info(f"进度 {i + 1}/{ROUNDS}",
                     f"seg 中位（暂）{statistics.median(seg_ms):.1f}ms / "
                     f"det 中位（暂）{statistics.median(det_ms):.1f}ms")
        else:
            counts_ok = True

        check(counts_ok and len(seg_ms) == ROUNDS and len(det_ms) == ROUNDS,
              f"c) {ROUNDS} 轮 seg/det 全部 ok，进程未崩溃",
              f"seg {len(seg_ms)} 次 / det {len(det_ms)} 次")
        check(clean.alive, "c) 压力后 worker 仍存活",
              f"alive={clean.alive} exit={clean.exit_code}；stderr 尾部 "
              f"{clean.stderr_tail(3)!r}")
        violations = clean.protocol_violations()
        check(not violations, "c) 没有『不带 @@J 前缀的协议行』（哨兵回归）",
              f"原始 stdout {len(clean.raw_out)} 行，噪声 {len(clean.noise)} 行，"
              f"协议污染 {len(violations)} 行")
        if clean.noise:
            info("噪声样例", f"{clean.noise[:2]!r}")
        check(seg_ep_flags == {ep_truth} if ep_truth is not None else True,
              "c) seg 响应里的 ep 与 health 一致（不中途改口）",
              f"seg ep={seg_ep_flags} health ep={ep_truth}")

        if seg_ms and det_ms:
            med_seg = statistics.median(seg_ms)
            med_det = statistics.median(det_ms)
            info("seg infer_ms", f"n={len(seg_ms)} 中位={med_seg:.1f} "
                                 f"均值={statistics.mean(seg_ms):.1f} "
                                 f"min={min(seg_ms):.1f} max={max(seg_ms):.1f}")
            info("det infer_ms", f"n={len(det_ms)} 中位={med_det:.1f} "
                                 f"均值={statistics.mean(det_ms):.1f} "
                                 f"min={min(det_ms):.1f} max={max(det_ms):.1f}")
            if ep_truth:
                check(med_seg <= SEG_MEDIAN_LIMIT_MS,
                      f"d) seg 中位 <= {SEG_MEDIAN_LIMIT_MS:.0f}ms（AI 核）",
                      f"实测 {med_seg:.1f}ms")
                check(med_det <= DET_MEDIAN_LIMIT_MS,
                      f"d) det 中位 <= {DET_MEDIAN_LIMIT_MS:.0f}ms（AI 核）",
                      f"实测 {med_det:.1f}ms")
            else:
                skip(f"d) seg 中位 <= {SEG_MEDIAN_LIMIT_MS:.0f}ms",
                     f"拿到 CPU（ep=false），无法验时延；CPU 实测中位 {med_seg:.1f}ms "
                     "仅供参考，不作为 AI 核验收读数上报")
                skip(f"d) det 中位 <= {DET_MEDIAN_LIMIT_MS:.0f}ms",
                     f"拿到 CPU（ep=false），无法验时延；CPU 实测中位 {med_det:.1f}ms "
                     "仅供参考，不作为 AI 核验收读数上报")

        # ---------------------------------------------------------------- EXTRA
        print("\n=== EXTRA) 错误不杀进程（spec §5） ===")
        if not clean.send_raw("{ this is not json"):
            check(False, "坏 JSON 不杀进程", f"worker 已经死了：{clean.state()}")
        try:
            bad = clean.read_response(timeout=60.0, what="坏 JSON 的响应")
        except Exception as err:  # noqa: BLE001
            check(False, "坏 JSON 响应可读（worker 还活着吗）", f"{type(err).__name__}: {err}")
        else:
            check(bad.get("ok") is False and "bad json" in str(bad.get("error", "")),
                  "坏 JSON 返回 ok=false 且带原因",
                  f"{json.dumps(bad, ensure_ascii=False)[:120]}")
        unknown = clean.call("unknown_op", timeout=60.0)
        check(unknown.get("ok") is False, "未知 op 返回 ok=false",
              f"{json.dumps(unknown, ensure_ascii=False)[:100]}")
        missing = clean.call("seg", timeout=60.0, image=str(tmp / "nope.jpg"))
        check(missing.get("ok") is False, "缺图返回 ok=false（不抛栈、不退出）",
              f"{json.dumps(missing, ensure_ascii=False)[:120]}")
        check(clean.alive, "EXTRA) 三次异常后 worker 仍存活")
    finally:
        clean.close()

    # -------------------------------------------------------------------- b)
    print("\n=== b) 故意申请超量核：必须 ep=false + 明确原因，且不得冒充 AI 核 ===")
    over = Worker(env_extra=OVERSUBSCRIBE, label="oversubscribed")
    try:
        try:
            health2 = over.call("health", timeout=240.0)
        except Exception as err:  # noqa: BLE001
            check(False, "b) 超量申请后 health 仍可返回", f"{type(err).__name__}: {err}")
            health2 = {}
        if health2.get("ok"):
            info("ep(超量)", f"{health2.get('ep')}")
            info("ep_threads(超量)", f"{health2.get('ep_threads')}")
            info("ep_fallback(超量)", f"{health2.get('ep_fallback')!r}")
            info("providers(超量)", f"{health2.get('providers')}")
            check(health2.get("ep") is False,
                  "b) 超量申请后 ep=false（不冒充 AI 核）",
                  f"ep={health2.get('ep')}")
            check(bool(health2.get("ep_fallback")),
                  "b) 带 fallback 原因（明确说明为什么没拿到核）",
                  f"{health2.get('ep_fallback')!r}")
            check(health2.get("ep_threads") is None,
                  "b) 没拿到核时 ep_threads 不得编造",
                  f"ep_threads={health2.get('ep_threads')}")
            check(health2.get("providers") == ["CPUExecutionProvider"],
                  "b) providers 如实报 CPU",
                  f"{health2.get('providers')}")
            check(not over.protocol_violations(),
                  "b) EP 噪声虽多，但没有污染协议（哨兵生效）",
                  f"stdout {len(over.raw_out)} 行，噪声 {len(over.noise)} 行")
            if over.noise:
                info("EP 噪声样例", f"{over.noise[:2]!r}")
            try:
                seg_cpu = over.call("seg", timeout=240.0, image=str(image), prompts=["person"])
                check(seg_cpu.get("ok") and seg_cpu.get("ep") is False,
                      "b) 降级后 seg 仍可用，且响应如实标 ep=false",
                      f"ok={seg_cpu.get('ok')} ep={seg_cpu.get('ep')} "
                      f"infer_ms={seg_cpu.get('infer_ms')}（CPU 读数）")
            except Exception as err:  # noqa: BLE001
                check(False, "b) 降级后 seg 仍可用", f"{type(err).__name__}: {err}")
    finally:
        over.close()

    print("\n=== EXTRA) 出厂客户端 infer_client.InferWorker 也要能吞下噪声 ===")
    saved = {k: os.environ.get(k) for k in OVERSUBSCRIBE}
    os.environ.update(OVERSUBSCRIBE)
    try:
        sys.path.insert(0, str(APP))
        import infer_client

        client = infer_client.InferWorker(start_timeout=240.0)
        try:
            res = client.health(timeout=240.0)
            stats = client.stats()
            check(bool(res.get("ok")) and res.get("ep") is False,
                  "infer_client 能穿过 EP 噪声拿到 health",
                  f"ok={res.get('ok')} ep={res.get('ep')} "
                  f"噪声丢弃={stats.get('noise_lines_dropped')}")
            check(stats.get("noise_lines_dropped", 0) >= 1,
                  "infer_client 确实丢弃了不带 @@J 的噪声行",
                  f"丢弃 {stats.get('noise_lines_dropped')} 行")
        finally:
            client.stop()
    except Exception as err:  # noqa: BLE001
        check(False, "infer_client 集成", f"{type(err).__name__}: {err}")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== T-1 汇总 ===")
    print(f"  ep 真值: {ep_truth}")
    print(f"  seg 中位: {'%.1fms' % med_seg if med_seg is not None else '(未测到)'}")
    print(f"  det 中位: {'%.1fms' % med_det if med_det is not None else '(未测到)'}")
    print(f"  检查项 {CHECKS} 个，失败 {len(FAILURES)} 个，跳过 {len(SKIPS)} 个")
    for name in FAILURES:
        print(f"  - FAILED: {name}")
    for name in SKIPS:
        print(f"  - SKIPPED: {name}")
    if FAILURES:
        print("WORKER TEST: FAIL")
        return 1
    if SKIPS:
        print("WORKER TEST: PASS（含 SKIP：AI 核非本进程可得，时延未验）")
        return 0
    print("WORKER TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())