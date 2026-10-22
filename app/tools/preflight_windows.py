#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 本地演示预检；只依赖 Python 标准库。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8090
TIMEOUT = 3.0


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.items.append((status, name, detail))

    def print(self) -> int:
        for status, name, detail in self.items:
            print(f"[{status}] {name}: {detail}")
        counts = {key: sum(item[0] == key for item in self.items)
                  for key in ("PASS", "WARN", "FAIL")}
        print("汇总：PASS {PASS} · WARN {WARN} · FAIL {FAIL}".format(**counts))
        return 1 if counts["FAIL"] else 0


def request(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "DevCon-Windows-Preflight/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return response.status, response.read(1024 * 1024), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096), exc.headers.get("Content-Type", "")


def probe_mock_camera() -> tuple[str, str]:
    """运行应用的 mock camera 冒烟；camera.py 的运行依赖由应用环境提供。"""
    code = (
        "import os; os.environ['BOOTH_CAMERA']='mock'; "
        "from camera import Camera; c=Camera(); s=c.open(); f=c.read_bgr(); "
        "assert s.ok and not s.real and f is not None and f.size; "
        "print('%dx%d' % (f.shape[1], f.shape[0])); c.close()"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(APP_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "WARN", f"mock camera 子进程无法完成：{exc}"
    if result.returncode == 0:
        return "PASS", f"合成相机可打开并出帧（{result.stdout.strip()}）"
    detail = (result.stderr or result.stdout).strip().splitlines()
    reason = detail[-1] if detail else f"退出码 {result.returncode}"
    return "WARN", f"mock camera 未能实测：{reason}（检查应用依赖）"


def run(port: int) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report = Report()
    report.add("PASS", "Python", f"{sys.version.split()[0]} · {sys.executable}")

    required = (
        APP_DIR / "booth_server.py",
        APP_DIR / "static" / "index.html",
        APP_DIR / "static" / "join.html",
        APP_DIR / "static" / "auto-flow.css",
        APP_DIR / "static" / "auto-flow-messages.js",
        APP_DIR / "static" / "auto-flow-consent.js",
        APP_DIR / "static" / "auto-flow.js",
    )
    missing = [str(path.relative_to(APP_DIR)) for path in required if not path.is_file()]
    if missing:
        report.add("FAIL", "本地静态文件", "缺少：" + ", ".join(missing))
    else:
        report.add("PASS", "本地静态文件", "拍摄页、报名页及脚本文件齐全")

    host = "127.0.0.1"
    base = f"http://{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            listening = True
    except OSError:
        listening = False
    if not listening:
        report.add("WARN", f"端口 {port}", "当前没有服务监听；启动演示服务后可再次检查 HTTP 路由")
        report.add("WARN", "静态页 /", "未检测：服务未运行")
        report.add("WARN", "报名页 /join", "未检测：服务未运行；浏览器入口应为 http://<本机局域网地址>:%d/join" % port)
        report.add("WARN", "接口 /api/health", "未检测：服务未运行")
    else:
        report.add("PASS", f"端口 {port}", "有进程监听 127.0.0.1")
        try:
            status, body, content_type = request(base + "/")
            if status == 200 and "text/html" in content_type and b"<html" in body.lower():
                report.add("PASS", "静态页 /", f"HTTP {status} · HTML")
            else:
                report.add("FAIL", "静态页 /", f"HTTP {status} · {content_type or '无内容类型'}")
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            report.add("FAIL", "静态页 /", str(exc))
        try:
            status, body, content_type = request(base + "/join")
            if status == 200 and "text/html" in content_type and b"data-af-page=\"join\"" in body:
                report.add("PASS", "报名页 /join", "HTTP 200 · 手机排队页")
            elif status == 404:
                report.add("WARN", "报名页 /join", "HTTP 404；当前服务未提供此路由（队列功能默认关闭/尚未接线）")
            else:
                report.add("FAIL", "报名页 /join", f"HTTP {status} · {content_type or '无内容类型'}")
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            report.add("FAIL", "报名页 /join", str(exc))
        try:
            status, _, content_type = request(base + "/api/join-qr.png")
            if status == 200 and content_type.startswith("image/png"):
                report.add("PASS", "报名二维码", "HTTP 200 · PNG")
            else:
                report.add("FAIL", "报名二维码", f"HTTP {status} · {content_type or '无内容类型'}")
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            report.add("FAIL", "报名二维码", str(exc))
        try:
            status, body, content_type = request(base + "/api/health")
            payload = json.loads(body.decode("utf-8")) if status == 200 else {}
            if status == 200 and payload.get("ok") is True and "json" in content_type:
                report.add("PASS", "接口 /api/health", f"HTTP 200 · build={payload.get('build', '未知')}")
            else:
                report.add("FAIL", "接口 /api/health", f"HTTP {status} · 响应不是有效健康状态")
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            report.add("FAIL", "接口 /api/health", str(exc))

    status, detail = probe_mock_camera()
    report.add(status, "mock camera", detail)
    report.add("WARN", "手机访问地址", f"手机请用同一局域网可达的地址： http://<本机 LAN IP>:{port}/join；127.0.0.1 只代表当前设备")
    return report.print()


def main() -> int:
    parser = argparse.ArgumentParser(description="DevCon 装置 5 · Windows 本地演示预检")
    parser.add_argument("--port", type=int, default=int(os.environ.get("BOOTH_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1~65535 之间")
    return run(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
