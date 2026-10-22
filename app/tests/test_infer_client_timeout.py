#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""infer_client 超时修复的验证（不需要真模型，用假 worker）。

背景：原来 self._proc.stdout.readline() 是阻塞读，deadline 只在读之前比一次，
所以 worker 一旦不回包就永久挂住 —— 实测卡过 900 秒，拍摄位被占死、队列全堵。
这次改成在管道 fd 上用 selectors 等可读 + 自己拼行。

三个必须成立的断言：
  A. worker 不回包时，call() 在 timeout 附近抛出 InferError（不是永久挂住）；
  B. 超时后客户端能自愈：重启 worker 后下一次调用正常返回；
  C. 先吐噪声行、再吐 `@@J ` 响应行时，能正确跳过噪声取到响应
     （二进制模式 + 自己拼行后最容易在这里出错）。

运行：.venv-test\\Scripts\\python.exe app\\tests\\test_infer_client_timeout.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

from infer_client import InferError, InferWorker  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("   " + detail) if detail else ""))
    return bool(ok)


def write_fake_workers(tmp: Path) -> tuple[Path, Path]:
    """两个假 worker：一个永远不回包；一个先吐噪声再正常回包。"""
    silent = tmp / "silent_worker.py"
    silent.write_text(
        "import sys, time\n"
        "for line in sys.stdin:\n"
        "    time.sleep(3600)\n",        # 收到请求后永远不响应
        encoding="utf-8")

    noisy = tmp / "noisy_worker.py"
    noisy.write_text(
        "import json, sys\n"
        "P = '@@J '\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    # 先打几行 EP 噪声（含非 ASCII 与非法 UTF-8 字节）\n"
        "    sys.stdout.write('EP Error: not enough AI cores\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stdout.buffer.write(b'\\xff\\xfe binary noise \\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "    sys.stdout.write(P + json.dumps({'ok': True, 'op': req.get('op'),\n"
        "                                     'id': req.get('id'), 'infer_ms': 1.0}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8")

    # 交替 worker：第 1 次不回包（模拟卡死），之后的进程正常回包。
    # 用它验「超时 → 杀进程重启 → 下一次调用成功」这条自愈链。
    flag = tmp / "wedged_once.flag"
    flaky = tmp / "flaky_worker.py"
    flaky.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "P = '@@J '\n"
        "flag = Path(r'%s')\n"
        "first = not flag.exists()\n"
        "if first:\n"
        "    flag.write_text('1')\n"
        "for line in sys.stdin:\n"
        "    if first:\n"
        "        time.sleep(3600)\n"
        "    sys.stdout.write(P + json.dumps({'ok': True, 'op': 'health'}) + '\\n')\n"
        "    sys.stdout.flush()\n" % flag,
        encoding="utf-8")
    return silent, noisy, flaky


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="infer-client-test-"))
    silent, noisy, flaky = write_fake_workers(tmp)
    py = sys.executable
    print(f"假 worker 目录：{tmp}\n")

    # ---- A/B：卡死的 worker 必须在超时处抛错，且随后能自愈
    print("=== A) worker 不回包：call() 必须超时抛错（不能永久挂住）")
    w = InferWorker(script=silent, python=py, start_timeout=30.0)
    t0 = time.time()
    try:
        w.call("seg", timeout=3.0, image="/tmp/x.jpg")
    except InferError as exc:
        elapsed = time.time() - t0
        check(3.0 <= elapsed < 9.0,
              "3 秒超时在预期时间内抛出", f"实测 {elapsed:.1f}s · {exc}")
    except Exception as exc:  # noqa: BLE001
        check(False, "抛的是 InferError", f"{type(exc).__name__}: {exc}")
    else:
        check(False, "不回包时必须抛错", "竟然返回了结果")

    print("\n=== B) 超时后自愈：下一次调用能正常拿到响应")
    # silent worker 永远不回包，所以换一个「第一次卡死、之后正常」的
    w.stop()
    w2 = InferWorker(script=flaky, python=py, start_timeout=30.0)
    t0 = time.time()
    try:
        w2.call("health", timeout=3.0)
        check(False, "第一次应卡死超时", "竟然成功了")
    except InferError as exc:
        check(True, "第一次按预期超时", f"{time.time() - t0:.1f}s · {exc}")
    t0 = time.time()
    try:
        resp = w2.call("health", timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        check(False, "自愈后第二次调用成功", f"{type(exc).__name__}: {exc}")
    else:
        check(bool(resp.get("ok")), "自愈后第二次调用成功",
              f"{time.time() - t0:.1f}s · ok={resp.get('ok')}")
    w2.stop()

    # ---- C：噪声行 + 非法 UTF-8 之后仍能取到响应
    print("\n=== C) 前置噪声/非法 UTF-8 之后仍能取到响应行")
    w3 = InferWorker(script=noisy, python=py, start_timeout=30.0)
    try:
        resp = w3.call("health", timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        check(False, "跳过噪声取到响应", f"{type(exc).__name__}: {exc}")
    else:
        check(bool(resp.get("ok")) and resp.get("op") == "health",
              "跳过噪声取到响应", json.dumps(resp, ensure_ascii=False)[:120])
        check(w3._noise_lines >= 1, "噪声行被计数（没当成协议行）",
              f"noise_lines={w3._noise_lines}")
    w3.stop()

    # ---- D：连续多次调用不会因为缓冲区残留而错位
    print("\n=== D) 连续 5 次调用，响应与请求一一对应（缓冲区不串行）")
    w4 = InferWorker(script=noisy, python=py, start_timeout=30.0)
    ok = True
    for i in range(5):
        try:
            resp = w4.call("seg", timeout=20.0, image=f"/tmp/{i}.jpg")
        except Exception as exc:  # noqa: BLE001
            ok = False
            check(False, f"第 {i + 1} 次调用", f"{type(exc).__name__}: {exc}")
            break
        if resp.get("id") != i + 1:
            ok = False
            check(False, f"第 {i + 1} 次调用 id 对上", f"期望 {i + 1}，得到 {resp.get('id')}")
            break
    if ok:
        check(True, "连续 5 次调用 id 全部对应", "")
    w4.stop()

    failed = [r for r in results if not r[0]]
    print()
    print(f"共 {len(results)} 项，失败 {len(failed)} 项")
    if failed:
        for _, name, detail in failed:
            print(f"  ❌ {name}  {detail}")
        return 1
    print("✅ infer_client 超时与噪声处理全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
