#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印适配器的验证（替换 which/_run 两个接缝，不需要真打印机）。

为什么不用"造一套假 lp 壳脚本 + 改 PATH"的老办法：本机是 Windows，
`shutil.which("lp")` 会按 PATHEXT 去找 lp.EXE / lp.COM，一个无扩展名的假
shell 脚本永远找不到 —— 于是"有 lp 但没队列"那组会**通过但理由是错的**
（本该验证"没有队列"，实际报出来的是"找不到 lp"）。所以直接替换模块级
接缝，测的就只剩适配器自己的判断逻辑，与平台无关。

重点验三条现场会踩到的性质：
  A. 没有打印机时必须**如实**报 unavailable / 503，绝不假装排入成功；
  B. 同一个幂等键重复提交只打一次（现场误点两次不该浪费两张相纸）；
  C. 照片不存在时 404，而不是打一张空白纸。

运行：.venv-test\\Scripts\\python.exe app\\tests\\test_print_adapter.py
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")
APP = Path(__file__).resolve().parents[1]        # .../app
ROOT = APP.parent                                # 项目根（app 包的上一层）
# 只加 ROOT，让一切走**包路径** `app.xxx` 导入。
# 绝不能同时把 APP 也加进 sys.path：那样 `app.auto_flow.errors` 与
# `auto_flow.errors` 会各自加载同一个文件、成为两个不同的模块对象，
# 于是 print_adapter 抛的 ApiError 和测试 except 的 ApiError 不是同一个类，
# 异常直接穿出 try（实测踩过）。
sys.path.insert(0, str(ROOT))

import app.print_adapter as pa  # noqa: E402
from app.auto_flow import errors  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("   " + detail) if detail else ""))


class FakeCups:
    """替换 `shutil.which` 与 `_run`，把 CUPS 命令行整段假掉。

    记录每一次 `lp` 调用，供"不可用时绝不调用 lp"这类断言使用。
    """

    def __init__(self, *, has_lp: bool = True, lp_exit: int = 0,
                 lpstat_out: str = "printer testq is idle.  enabled\n",
                 lpstat_exit: int = 0,
                 lp_out: str = "request id is testq-42 (1 file(s))",
                 ipp_exit: int = 0,
                 ipp_out: str = "job-id (integer) = 42\njob-state (enum) = processing",
                 has_ipptool: bool = True):
        self.has_lp = has_lp
        self.lp_exit = lp_exit
        self.lpstat_out = lpstat_out
        self.lpstat_exit = lpstat_exit
        self.lp_out = lp_out
        self.ipp_exit = ipp_exit
        self.ipp_out = ipp_out
        self.has_ipptool = has_ipptool
        self.lp_calls: list[list[str]] = []
        self.lpstat_calls: list[list[str]] = []
        self.ipp_calls: list[list[str]] = []
        self.ipp_specs: list[str] = []
        self._saved = ()

    def __enter__(self) -> FakeCups:
        self._saved = (pa.shutil.which, pa._run)
        pa.shutil.which = lambda name: (
            None if not self.has_lp or (name == "ipptool" and not self.has_ipptool)
            else "/usr/bin/" + name)

        def fake_run(cmd, timeout):
            if cmd and cmd[0] == "lp":
                self.lp_calls.append(list(cmd))
                return self.lp_exit, self.lp_out, "" if self.lp_exit == 0 else "lp: 出错"
            if cmd and cmd[0] == "lpstat":
                self.lpstat_calls.append(list(cmd))
                return self.lpstat_exit, self.lpstat_out, "" if self.lpstat_exit == 0 else "lpstat: 出错"
            if cmd and cmd[0] == "ipptool":
                self.ipp_calls.append(list(cmd))
                self.ipp_specs.append(Path(cmd[-1]).read_text(encoding="utf-8"))
                return self.ipp_exit, self.ipp_out, "" if self.ipp_exit == 0 else "IPP 查询失败"
            return 127, "", "未知命令：%r" % (cmd[:1],)

        pa._run = fake_run
        return self

    def __exit__(self, *exc):
        pa.shutil.which, pa._run = self._saved
        return False


def photo_file(tmp: Path, name: str = "photo.jpg") -> Path:
    path = tmp / name
    path.write_bytes(b"\xff\xd8\xff\xd9")     # 最小 JPEG 头尾
    return path


print("=== A) 没有 lp 命令（没装 CUPS 客户端）→ 必须不可用")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups(has_lp=False) as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp))
        health = adapter.status()
        check(health.status == "unavailable", "status 如实报 unavailable", health.reason or "")
        try:
            adapter.submit("a" * 22, "key-1")
        except errors.ApiError as exc:
            check(exc.code == errors.PRINT_UNAVAILABLE, "submit 抛 PRINT_UNAVAILABLE", exc.code)
        else:
            check(False, "submit 应当抛错", "竟然成功了")
        check(not cups.lp_calls, "不可用时没有调用 lp")

print()
print("=== B) 有 lp 但没有任何打印机队列 → 仍然不可用")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups(lpstat_out="") as cups:      # 空输出 = 没有队列
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp))
        health = adapter.status()
        check(health.status == "unavailable", "没有队列时报 unavailable", health.reason or "")
        # 关键：绝不能因为"命令能跑"就认为能打
        try:
            adapter.submit("b" * 22, "key-2")
        except errors.ApiError as exc:
            check(exc.code == errors.PRINT_UNAVAILABLE, "submit 抛 PRINT_UNAVAILABLE", exc.code)
        else:
            check(False, "应当抛 PRINT_UNAVAILABLE")
        check(not cups.lp_calls, "不可用时**没有调用 lp**（没打出空白纸）")

print()
print("=== C) 队列就绪 → 提交成功，且 lp 真的被调用")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups() as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp), queue="testq")
        health = adapter.status()
        check(health.status == "ok", "队列就绪时报 ok", health.reason or "")
        job = adapter.submit("c" * 22, "key-3")
        check(job.status == "submitted", "提交后状态是 submitted", job.status)
        check(job.print_job_id != "", "分配了 print_job_id", job.print_job_id)
        check(len(cups.lp_calls) == 1, "lp 被调用一次", str(len(cups.lp_calls)))
        argv = cups.lp_calls[0] if cups.lp_calls else []
        check("testq" in argv, "lp 收到了队列名", " ".join(argv)[:80])
        check(any(str(a).endswith("photo.jpg") for a in argv), "lp 收到了照片路径",
              " ".join(argv)[:80])
        check(argv[argv.index("-n") + 1] == "1", "明确打印单份")
        check("print-scaling=fit" in argv, "默认 fit 保留完整版面")
        check(argv[argv.index("-h") + 1] == "localhost:631", "lp 固定提交到本机 CUPS")
        check(argv[-2] == "--", "文件路径不被识别为命令选项")
        check(adapter._cups_requests[job.print_job_id] == "testq-42", "保存真实 CUPS request id")
        check(set(job.to_dict()) == {"print_job_id", "photo_token", "status", "created_at", "updated_at"},
              "PrintJob API 未增加字段")

print()
print("=== C2) media 配置会附加到 lp 参数上")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups() as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp),
                                      queue="testq", media="4x6")
        adapter.submit("c" * 22, "key-3b")
        argv = cups.lp_calls[0] if cups.lp_calls else []
        check("media=4x6" in argv, "带了 media=4x6", " ".join(argv)[:80])

print()
print("=== D) 幂等：同键重复提交只打一次")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups() as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp), queue="testq")
        first = adapter.submit("d" * 22, "same-key")
        second = adapter.submit("d" * 22, "same-key")
        check(first.print_job_id == second.print_job_id,
              "同键返回同一个作业", f"{first.print_job_id} vs {second.print_job_id}")
        check(len(cups.lp_calls) == 1, "lp 只被调用了一次", f"实际 {len(cups.lp_calls)} 次")

print()
print("=== E) 照片不存在 → 404，且不调用 lp")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups() as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: None, queue="testq")
        try:
            adapter.submit("e" * 22, "key-5")
        except errors.ApiError as exc:
            check(exc.code == errors.PHOTO_NOT_FOUND, "抛 PHOTO_NOT_FOUND", exc.code)
        else:
            check(False, "应当抛 PHOTO_NOT_FOUND", "竟然成功了")
        check(not cups.lp_calls, "没调用 lp")

print()
print("=== E2) 令牌解析得到路径、但文件已不在 → 同样是 PHOTO_NOT_FOUND")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups() as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: tmp / "missing.jpg", queue="testq")
        try:
            adapter.submit("e" * 22, "key-5b")
        except errors.ApiError as exc:
            check(exc.code == errors.PHOTO_NOT_FOUND, "抛 PHOTO_NOT_FOUND", exc.code)
        else:
            check(False, "应当抛 PHOTO_NOT_FOUND")
        check(not cups.lp_calls, "没调用 lp")

print()
print("=== F) lp 失败 → 状态 failed 且带错误码，不伪报成功")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups(lp_exit=1, lp_out=""):
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp), queue="testq")
        job = adapter.submit("f" * 22, "key-6")
        check(job.status == "failed", "lp 失败时状态是 failed", job.status)
        check(job.error_code == "PRINT_FAILED", "带 PRINT_FAILED 错误码", str(job.error_code))
        check(adapter.get(job.print_job_id) is job, "作业可按 id 查回")
        check(adapter.get("不存在") is None, "未知 id 返回 None（路由层转 404）")

print()
print("=== G) 参数校验：缺 token / 缺幂等键都要 400")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    with FakeCups():
        adapter = pa.CupsPrintAdapter(photo_path=lambda tok: photo_file(tmp), queue="testq")
        for label, token, key in (("缺 token", "", "k"), ("缺幂等键", "g" * 22, ""),
                                  ("token 非字符串", None, "k")):
            try:
                adapter.submit(token, key)
            except errors.ApiError as exc:
                check(exc.code == errors.INVALID_REQUEST, f"{label} → INVALID_REQUEST", exc.code)
            else:
                check(False, f"{label} 应当抛错")

print()
print("=== H) build_print_adapter：未设置开关时返回 None（这扇门默认关着）")
for env, expected_none, label in (
    ({}, True, "未设置"),
    ({"BOOTH_PRINT_ENABLED": "false"}, True, "显式 false"),
    ({"BOOTH_PRINT_ENABLED": "no"}, True, "no"),
):
    saved = {k: os.environ.get(k) for k in ("BOOTH_PRINT_ENABLED", "BOOTH_PRINT_BACKEND")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(env)
    got = pa.build_print_adapter(photo_path=lambda tok: None)
    check((got is None) == expected_none, f"{label} → 返回 {'None' if expected_none else '适配器'}",
          type(got).__name__)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# 开启后应当真的装配出适配器，并把环境变量吃进去
saved = {k: os.environ.get(k) for k in
         ("BOOTH_PRINT_ENABLED", "BOOTH_PRINT_BACKEND", "BOOTH_PRINT_QUEUE", "BOOTH_PRINT_MEDIA",
          "BOOTH_PRINT_SCALING", "BOOTH_PRINT_STATE_FILE")}
os.environ["BOOTH_PRINT_ENABLED"] = "true"
os.environ["BOOTH_PRINT_BACKEND"] = "cups"
os.environ["BOOTH_PRINT_QUEUE"] = "devq"
os.environ["BOOTH_PRINT_MEDIA"] = "A6"
os.environ["BOOTH_PRINT_SCALING"] = "fill"
os.environ["BOOTH_PRINT_STATE_FILE"] = ""
got = pa.build_print_adapter(photo_path=lambda tok: None)
check(isinstance(got, pa.CupsPrintAdapter), "开启后装配出 CupsPrintAdapter", type(got).__name__)
check(getattr(got, "queue", "") == "devq", "读到了队列名", str(getattr(got, "queue", "")))
check(getattr(got, "media", "") == "A6", "读到了纸张规格", str(getattr(got, "media", "")))
check(got.scaling == "fill", "读到了缩放选项")
check(got._state_file is None, "默认关闭持久化")
with tempfile.TemporaryDirectory() as directory:
    os.environ["BOOTH_PRINT_STATE_FILE"] = str(Path(directory) / "print-state.json")
    got = pa.build_print_adapter()
    check(got._state_file == (Path(directory) / "print-state.json").resolve(), "读到了台账路径")

# 开启 + 不支持的 backend 必须报错，而不是静默回退
os.environ["BOOTH_PRINT_BACKEND"] = "carrier-pigeon"
try:
    pa.build_print_adapter(photo_path=lambda tok: None)
except ValueError as exc:
    check(True, "不支持的 backend 报错而不是静默回退", str(exc)[:60])
else:
    check(False, "不支持的 backend 应当报错")
for k, v in saved.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

print()
print("=== I) _run 固定英文环境且保留其余环境；超时不抛异常")
with patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8", "BOOTH_TEST_VALUE": "preserved"}):
    with patch.object(pa.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["lpstat"], 0, " printer testq is idle. \n", "")) as runner:
        outcome = pa._run(["lpstat"], 3.0)
        check(outcome == (0, "printer testq is idle.", ""), "_run 返回退出码与去空白的输出")
        check(runner.call_args.kwargs["env"]["LC_ALL"] == "C", "子进程 LC_ALL=C")
        check(runner.call_args.kwargs["env"]["BOOTH_TEST_VALUE"] == "preserved", "保留其余环境")
        check(os.environ["LC_ALL"] == "zh_CN.UTF-8", "不修改父进程语言环境")
    with patch.object(pa.subprocess, "run", side_effect=subprocess.TimeoutExpired(["lp"], 3)):
        check(pa._run(["lp"], 3)[0] == 124, "超时转为 124")
    with patch.object(pa.subprocess, "run", side_effect=FileNotFoundError("missing")):
        check(pa._run(["lp"], 3)[0] == 127, "缺少命令转为 127")

print()
print("=== J) IPP 查询真实状态，不以队列消失判断完成")
for ipp_state, expected in (
        ("pending", "queued"), ("held", "queued"), ("pending-held", "queued"), ("processing", "submitted"),
        ("processing-stopped", "submitted"), ("canceled", "failed"), ("aborted", "failed"),
        ("completed", "completed"), ("3", "queued"), ("4", "queued"),
        ("5", "submitted"), ("6", "submitted"), ("7", "failed"), ("8", "failed"), ("9", "completed")):
    with tempfile.TemporaryDirectory() as directory, FakeCups(
            lp_out="request id is Canon_SELPHY_CP1500_USB-117 (1 file(s))",
            ipp_out="    job-state (enum) = " + ipp_state) as cups:
        photo = photo_file(Path(directory))
        adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo,
                                      queue="Canon_SELPHY_CP1500_USB", media="custom_54x86mm")
        job = adapter.submit("j" * 22, "state-" + ipp_state)
        queried = adapter.get(job.print_job_id)
        check(queried.status == expected, f"{ipp_state} → {expected}", queried.status)
        check(queried.error_code == ("PRINT_FAILED" if expected == "failed" else None),
              f"{ipp_state} 错误码准确")
        check(cups.ipp_calls[0][:3] == ["ipptool", "-tv", "ipp://localhost:631/jobs/117"],
              "从 request id 的数字部分查询本机 job-uri")
        specification = cups.ipp_specs[0]
        check("OPERATION Get-Job-Attributes" in specification
              and "ATTR uri job-uri $uri" in specification
              and "STATUS successful-ok" in specification
              and "EXPECT job-id OF-TYPE integer WITH-VALUE 117" in specification,
              "IPP 请求核对返回任务编号与成功状态")
        check("media=custom_54x86mm" in cups.lp_calls[0], "传递主 agent 选择的纸型")

for exit_code, output in (
        (1, "job-state (enum) = completed"), (124, ""),
        (0, ""), (0, "job-state (enum) = unknown"),
        (0, "job-state-reasons (keyword) = completed-successfully")):
    with tempfile.TemporaryDirectory() as directory, FakeCups(
            ipp_exit=exit_code, ipp_out=output) as cups:
        adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)), queue="testq")
        job = adapter.submit("k" * 22, "query-error")
        adapter.get(job.print_job_id)
        check(job.status == "submitted" and job.error_code == "PRINT_STATUS_UNAVAILABLE",
              "查询失败或无效响应不会伪报完成", repr((exit_code, output)))
        cups.ipp_exit, cups.ipp_out = 0, "job-state (enum) = pending"
        adapter.get(job.print_job_id)
        check(job.status == "queued" and job.error_code is None, "查询恢复后清除查询异常")
        check(len(cups.lp_calls) == 1, "查询异常不触发重新打印")

with tempfile.TemporaryDirectory() as directory, FakeCups(has_ipptool=False) as cups:
    adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)), queue="testq")
    check(adapter.status().status == "unavailable", "缺少 ipptool 时打印不可用")
    try:
        adapter.submit("l" * 22, "missing-ipptool")
    except errors.ApiError as exc:
        check(exc.code == errors.PRINT_UNAVAILABLE, "缺少 ipptool 拒绝新打印")
    else:
        check(False, "缺少 ipptool 应拒绝")
    check(not cups.lp_calls, "缺少状态查询工具时不调用 lp")

print()
print("=== K) 幂等冲突、并发与重启")
with tempfile.TemporaryDirectory() as directory, FakeCups() as cups:
    photo = photo_file(Path(directory))
    state_file = Path(directory) / "state" / "jobs.json"
    adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo, queue="testq",
                                  state_file=state_file, scaling="fill")
    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = list(executor.map(lambda index: adapter.submit("m" * 22, "same-key"), range(16)))
    first = jobs[0]
    check(len({job.print_job_id for job in jobs}) == 1 and len(cups.lp_calls) == 1,
          "并发重复提交只打印一次")
    check("print-scaling=fill" in cups.lp_calls[0], "可配置缩放选项")
    data = json.loads(state_file.read_text(encoding="utf-8"))
    check(data["cups_requests"][first.print_job_id] == "testq-42"
          and data["by_key"]["same-key"] == first.print_job_id, "真实编号与幂等记录一起落盘")
    restarted = pa.CupsPrintAdapter(photo_path=lambda token: None, queue="testq", state_file=state_file)
    cups.lpstat_exit = 1
    replayed = restarted.submit("m" * 22, "same-key")
    check(replayed.to_dict() == first.to_dict() and len(cups.lp_calls) == 1,
          "重启后即使队列离线、源文件消失，也返回原任务")
    for current in (adapter, restarted):
        try:
            current.submit("n" * 22, "same-key")
        except errors.ApiError as exc:
            check(exc.code == errors.IDEMPOTENCY_CONFLICT, "同键不同 token 返回冲突")
        else:
            check(False, "同键不同 token 应拒绝")
    cups.ipp_out = "job-state (enum) = completed"
    restarted.get(first.print_job_id)
    last = pa.CupsPrintAdapter(state_file=state_file)
    check(last.get(first.print_job_id).status == "completed", "完成状态可跨重启读取")
    check(last.submit("m" * 22, "same-key").status == "completed" and len(cups.lp_calls) == 1,
          "已完成任务不重复出片")

print()
print("=== L) 不确定提交与失败任务跨重启不自动重印")
for exit_code, output in ((124, ""), (0, "无法解析编号"), (1, ""),
                          (1, "request id is testq-42 (1 file(s))")):
    with tempfile.TemporaryDirectory() as directory, FakeCups(
            lp_exit=exit_code, lp_out=output) as cups:
        state_file = Path(directory) / "jobs.json"
        adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)),
                                      queue="testq", state_file=state_file)
        first = adapter.submit("p" * 22, "uncertain")
        check(first.status == "failed", "未确认 lp 成功返回失败", str(exit_code))
        restarted = pa.CupsPrintAdapter(state_file=state_file)
        check(restarted.submit("p" * 22, "uncertain").to_dict() == first.to_dict()
              and len(cups.lp_calls) == 1, "失败或超时任务重启后仍不重复打印")
        if output.startswith("request id"):
            cups.ipp_out = "job-state (enum) = completed"
            check(restarted.get(first.print_job_id).status == "completed",
                  "提交有异常但保留真实编号时，仍能核实最终状态")

with tempfile.TemporaryDirectory() as directory, FakeCups() as cups:
    state_file = Path(directory) / "jobs.json"
    adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)),
                                  queue="testq", state_file=state_file)
    original_run = pa._run

    def interrupted_run(cmd, timeout):
        if cmd[0] == "lp":
            recorded = json.loads(state_file.read_text(encoding="utf-8"))
            check(recorded["by_key"].get("crash") is not None, "调用 lp 之前已预留幂等键")
            raise SystemExit("模拟 lp 调用窗口内进程退出")
        return original_run(cmd, timeout)

    with patch.object(pa, "_run", interrupted_run):
        try:
            adapter.submit("q" * 22, "crash")
        except SystemExit:
            pass
        else:
            check(False, "应模拟中断")
    restarted = pa.CupsPrintAdapter(state_file=state_file)
    reserved = restarted.submit("q" * 22, "crash")
    check(reserved.status == "queued" and reserved.error_code == "PRINT_SUBMISSION_UNKNOWN",
          "崩溃窗口恢复为待确认任务")
    check(restarted.get(reserved.print_job_id).status != "completed" and not cups.lp_calls,
          "无法确定是否送出时不自动重印、不伪报完成")

print()
print("=== M) 台账故障只阻止打印，且不损坏原记录")
with tempfile.TemporaryDirectory() as directory, FakeCups() as cups:
    state_file = Path(directory) / "jobs.json"
    with patch.object(pa.os, "replace", side_effect=OSError("只读目录")):
        adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)),
                                      queue="testq", state_file=state_file)
        try:
            adapter.submit("r" * 22, "disk-error")
        except errors.ApiError as exc:
            check(exc.code == errors.PRINT_UNAVAILABLE, "提交前持久化失败返回 PRINT_UNAVAILABLE")
        else:
            check(False, "持久化失败应拒绝新打印")
    check(not cups.lp_calls, "提交前写入失败不调用 lp")

with tempfile.TemporaryDirectory() as directory, FakeCups() as cups:
    state_file = Path(directory) / "jobs.json"
    adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)),
                                  queue="testq", state_file=state_file)
    original_replace = pa.os.replace
    replacements = []

    def fail_after_submission(source, destination):
        replacements.append(source)
        if len(replacements) == 2:
            raise OSError("提交后磁盘故障")
        return original_replace(source, destination)

    with patch.object(pa.os, "replace", fail_after_submission):
        first = adapter.submit("s" * 22, "post-submit-error")
    check(first.error_code == "PRINT_STATE_UNAVAILABLE" and len(cups.lp_calls) == 1,
          "提交后写入失败如实标记")
    restarted = pa.CupsPrintAdapter(state_file=state_file)
    recovered = restarted.submit("s" * 22, "post-submit-error")
    check(recovered.print_job_id == first.print_job_id and len(cups.lp_calls) == 1,
          "提交后落盘失败也不会因重启再打一次")

for corrupt in ("{broken", '{"version": 2}',
                '{"version":1,"jobs":{},"by_key":{"orphan":"missing"},"cups_requests":{}}'):
    with tempfile.TemporaryDirectory() as directory, FakeCups() as cups:
        state_file = Path(directory) / "jobs.json"
        state_file.write_text(corrupt, encoding="utf-8")
        adapter = pa.CupsPrintAdapter(photo_path=lambda token: photo_file(Path(directory)),
                                      queue="testq", state_file=state_file)
        check(adapter.status().status == "unavailable", "台账损坏时健康状态不可用")
        try:
            adapter.submit("t" * 22, "bad-state")
        except errors.ApiError as exc:
            check(exc.code == errors.PRINT_UNAVAILABLE, "台账损坏返回受控打印错误")
        else:
            check(False, "台账损坏应拒绝新打印")
        check(not cups.lp_calls and state_file.read_text(encoding="utf-8") == corrupt,
              "台账损坏时不打印也不覆盖原记录")

print("=== N) 实际设备离线、耗材错误和暂停不接受新打印")
for state, media, accepting, reason, exit_code, expected in (
    ("idle", "om_card_54x86mm", "true", "none", 0, True),
    ("processing", "om_card_54x86mm", "true", "none", 0, True),
    ("idle", "jpn_hagaki_100x148mm", "true", "none", 0, False),
    ("stopped", "om_card_54x86mm", "true", "none", 0, False),
    ("idle", "om_card_54x86mm", "false", "none", 0, False),
    ("idle", "om_card_54x86mm", "true", "media-empty-warning", 0, False),
    ("idle", "om_card_54x86mm", "true", "none", 124, False),
):
    response = ("printer-state (enum) = " + state + "\n"
                "media-ready (keyword) = " + media + "\n"
                "printer-is-accepting-jobs (boolean) = " + accepting + "\n"
                "printer-state-reasons (keyword) = " + reason)
    with tempfile.TemporaryDirectory() as folder, FakeCups(ipp_out=response, ipp_exit=exit_code) as cups:
        adapter = pa.CupsPrintAdapter(
            photo_path=lambda token: photo_file(Path(folder)), queue="testq",
            device_uri="ipp://127.0.0.1:60000/ipp/print", expected_media="om_card_54x86mm")
        check((adapter.status().status == "ok") == expected,
              "设备实际状态检查", str((state, media, accepting, reason, exit_code)))
        if not expected:
            try:
                adapter.submit("u" * 22, "device-probe")
            except errors.ApiError as exc:
                check(exc.code == errors.PRINT_UNAVAILABLE and not cups.lp_calls,
                      "设备不可用时不调用打印")
            else:
                check(False, "设备不可用应拒绝打印")

print("=== N2) 明信片配置检查实际耗材，错误提示使用对应纸型")
for media, expected in (("jpn_hagaki_100x148mm", True), ("om_card_54x86mm", False)):
    response = ("printer-state (enum) = idle\n"
                "media-ready (keyword) = " + media + "\n"
                "printer-is-accepting-jobs (boolean) = true\n"
                "printer-state-reasons (keyword) = none")
    with FakeCups(ipp_out=response) as cups:
        adapter = pa.CupsPrintAdapter(
            queue="testq", media="Postcard.Borderless", scaling="fill",
            device_uri="ipp://127.0.0.1:60000/ipp/print", expected_media="jpn_hagaki_100x148mm")
        available, reason = adapter.probe()
        check(available == expected, "100×148mm 耗材匹配检查", media)
        if not expected:
            check("100×148mm" in reason and "54×86mm" not in reason,
                  "明信片配置提示更换明信片相纸")

failed = [r for r in results if not r[0]]
print()
print(f"共 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    for _, name, detail in failed:
        print(f"  ❌ {name}  {detail}")
    sys.exit(1)
print("✅ 打印适配器全部通过")
