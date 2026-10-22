# -*- coding: utf-8 -*-
"""进程外看门狗：服务真的卡死了才把它重启。

为什么要有它（进程内自愈兜不住的那一类）：
  * 抓帧线程卡在内核里（V4L2 不返回）→ 进程内任何重连逻辑都跑不到；
  * HTTP 线程池被拖死、端口还listen着但没人应答；
  * 死锁 / 内存耗尽。
这些情况下 /api/health 无响应、或 preview.seq 长时间不涨，进程自己不会好。
本脚本由 systemd timer 每 20 秒叫一次，判定为「真卡死」才 restart，其余一律不动。

判定（保守，宁可不重启也不错杀）：
  ① /api/health 连续 2 次拿不到 → 重启；
  ② preview.running 为 true 且 last_frame_s > 45s 且 clients >= 1 → 重启；
  ③ 有拍摄任务在跑（active_job 非空）→ 这一轮跳过，别把参会者那张照片打断。

用法：
  python3 tools/watchdog.py             # 正常跑（给 systemd timer 用）
  python3 tools/watchdog.py --dry-run   # 只打印判定，不动服务
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BOOTH_SELF_URL", "http://127.0.0.1:%s" % os.environ.get("BOOTH_PORT", "8090"))
UNIT = os.environ.get("BOOTH_UNIT", "devcon-booth")
STATE_FILE = Path(os.environ.get("BOOTH_WATCHDOG_STATE", "/tmp/devcon-watchdog.json"))
STALE_S = float(os.environ.get("BOOTH_WATCHDOG_STALE", "45"))
FAILS_TO_RESTART = int(os.environ.get("BOOTH_WATCHDOG_FAILS", "2"))


def log(message: str) -> None:
    print("[watchdog %s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def fetch(path: str, timeout: float = 4.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as exc:  # noqa: BLE001
        log("状态写不进去（不影响判定）：%s" % exc)


def unit_active() -> bool:
    try:
        out = subprocess.run(["systemctl", "is-active", UNIT], capture_output=True, timeout=5.0)
        return (out.stdout or b"").decode().strip() == "active"
    except Exception:
        return False


def restart(reason: str, dry: bool) -> None:
    """只重启 systemd 管着的那个服务。

    故意不自己 nohup 起一个：现场必须只有一个控制面在跑，否则会出现
    两个进程抢同一台相机、两个都出不了图。
    """
    if not unit_active():
        log("判定该重启（%s），但 %s 不是 systemd 在管 —— 不动手，"
            "请人工确认现场是怎么起的服务" % (reason, UNIT))
        return
    log("判 定：%s → 重启 %s%s" % (reason, UNIT, "（dry-run，不真的重启）" if dry else ""))
    if dry:
        return
    try:
        subprocess.run(["systemctl", "restart", UNIT], capture_output=True, timeout=60.0)
        time.sleep(3.0)
        try:
            fetch("/api/health", timeout=8.0)
            log("重启完成，服务已应答")
        except Exception as exc:  # noqa: BLE001
            log("重启后仍然拿不到 /api/health：%s" % exc)
    except Exception as exc:  # noqa: BLE001
        log("重启失败：%s" % exc)


def main() -> int:
    dry = ("--dry-run" in sys.argv
           or os.environ.get("BOOTH_WATCHDOG_DRYRUN", "0") == "1")
    state = load_state()
    fails = int(state.get("health_fails", 0))

    try:
        health = fetch("/api/health")
    except Exception as exc:  # noqa: BLE001
        fails += 1
        state["health_fails"] = fails
        save_state(state)
        log("拿不到 /api/health（连续 %d 次）：%s" % (fails, exc))
        if fails >= FAILS_TO_RESTART:
            restart("控制面无响应（连续 %d 次）" % fails, dry)
            state["health_fails"] = 0
            save_state(state)
        return 0
    if fails:
        log("服务已恢复应答（此前连续失败 %d 次）" % fails)
    state["health_fails"] = 0

    try:
        st = fetch("/api/state", timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        log("读不到 /api/state：%s" % exc)
        save_state(state)
        return 0

    preview = st.get("preview") or {}
    job = st.get("active_job") or None
    last = preview.get("last_frame_s")
    clients = preview.get("clients") or 0
    running = preview.get("running")
    log("health ok · %s · clients=%s running=%s last_frame_s=%s reopens=%s"
        % (health.get("build", "?"), clients, running, last,
           (preview.get("camera") or {}).get("reopens")))

    if job:
        # 有任务在跑就别动：重启会把参会者那张照片打断
        save_state(state)
        return 0
    if running and clients >= 1 and isinstance(last, (int, float)) and last > STALE_S:
        restart("预览 %.0fs 没有新帧且进程还活着（进程内自愈没救回来）" % last, dry)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())