# -*- coding: utf-8 -*-
"""界面截图（四页 × 三档尺寸），供「无破版」人工复核与彩排留档。

用真机上已安装的 chromium 无头模式截图 —— 不引入任何新依赖，也不需要联网。
如果当前没有服务在跑，会自己用 mock 相机起一份（截完自动关掉）。

用法：
    python3 app/tools/shoot_ui.py                      # 自动找 / 自己起服务
    python3 app/tools/shoot_ui.py --base http://127.0.0.1:8090
    python3 app/tools/shoot_ui.py --page booth,stage    # 只截某几页
    python3 app/tools/shoot_ui.py --out /tmp/shots
退出码 0 = 全部截图产出成功。

⚠️ 坑 30：chromium 的 `--window-size` 有**最小宽度**（本机实测：要 390 会被夹成
518），而 `--screenshot` 照样按 390 裁图 —— 于是「手机截图」其实是 518 px 宽的
排版被切掉了右边，看着像横向溢出，其实页面没问题。真要按手机宽度复核破版，
得走 CDP 的 `Emulation.setDeviceMetricsOverride`，`--window-size` 不够。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent

CHROME_CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")

PAGES = {
    "booth": ("/", [(1600, 1000, "desktop"), (390, 844, "phone")]),
    "stage": ("/stage", [(1920, 1080, "fhd")]),
    "operator": ("/operator", [(1440, 1100, "desktop"), (390, 844, "phone")]),
    # 取图页认令牌：/p/<令牌>。真实令牌在 main() 里从 /api/state 的
    # last_token 取（找不到就用占位串，页面会显示"已删除"，照样能查破版）。
    "pick": ("/p/PLACEHOLDER-TOKEN", [(390, 844, "phone"), (1024, 900, "tablet")]),
}


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def shoot(chrome: str, url: str, size: tuple[int, int], out: Path, wait_ms: int = 6000) -> tuple[bool, str]:
    args = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--disable-dev-shm-usage",
        "--user-data-dir=/tmp/booth-shoot-profile",
        "--window-size=%d,%d" % size,
        "--virtual-time-budget=%d" % wait_ms,
        "--screenshot=" + str(out), url,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "chromium 超时"
    if not out.is_file() or out.stat().st_size < 2000:
        tail = (result.stderr or "")[-400:]
        return False, "没有产出截图（%s）" % tail.replace("\n", " ")
    return True, "%d B" % out.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=None, help="已有服务的地址，例如 http://127.0.0.1:8090")
    parser.add_argument("--out", default=str(APP / "docs" / "screens"))
    parser.add_argument("--page", default="booth,stage,operator,pick")
    parser.add_argument("--port", type=int, default=int(os.environ.get("BOOTH_SHOOT_PORT", "8097")))
    parser.add_argument("--wait-ms", type=int, default=6000)
    parser.add_argument("--token", default="", help="取图页用哪张照片（默认自动取最近一张）")
    args = parser.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("找不到 chromium，无法截图")
        return 2

    proc = None
    base = args.base
    if base is None:
        for candidate in ("http://127.0.0.1:8090", "http://127.0.0.1:%d" % args.port):
            if http_ok(candidate + "/api/health"):
                base = candidate
                break
    if base is None:
        base = "http://127.0.0.1:%d" % args.port
        log = open("/tmp/booth-shoot.log", "wb")
        proc = subprocess.Popen(
            [sys.executable, str(APP / "booth_server.py"), "--host", "127.0.0.1",
             "--port", str(args.port), "--mock-camera"],
            cwd=str(APP), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "BOOTH_CAMERA": "mock"},
        )
        deadline = time.time() + 90
        while time.time() < deadline and not http_ok(base + "/api/health"):
            time.sleep(0.5)
        if not http_ok(base + "/api/health"):
            print("自建服务没起来，见 /tmp/booth-shoot.log")
            return 1

    # 取图页要一个真令牌才看得到照片：没有就自己问服务要最近一张的。
    if "pick" in PAGES and not args.token:
        try:
            with urllib.request.urlopen(base + "/api/state", timeout=5.0) as response:
                args.token = (json.loads(response.read().decode("utf-8")) or {}).get("last_token") or ""
        except Exception:
            args.token = ""
        if args.token:
            PAGES["pick"] = ("/p/" + args.token, PAGES["pick"][1])
            print("取图页用最近一张的令牌：%s" % args.token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [name.strip() for name in args.page.split(",") if name.strip()]
    failed = 0
    print("截图：chrome=%s base=%s out=%s" % (chrome, base, out_dir))
    try:
        for name in wanted:
            if name not in PAGES:
                print("  跳过未知页面 %s" % name)
                continue
            path, sizes = PAGES[name]
            for width, height, label in sizes:
                target = out_dir / ("%s-%dx%d-%s.png" % (name, width, height, label))
                ok, detail = shoot(chrome, base + path, (width, height), target, args.wait_ms)
                print("  %s %s %dx%d → %s" % ("PASS" if ok else "FAIL", name, width, height, detail))
                if not ok:
                    failed += 1
    finally:
        if proc is not None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    print("结果：%d 张成功 / %d 张失败" % (sum(len(v[1]) for k, v in PAGES.items() if k in wanted) - failed,
                                        failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())