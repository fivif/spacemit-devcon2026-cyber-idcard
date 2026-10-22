# app/tests · 真机验收测试

测试都**不依赖 pytest**，用标准库断言，直接跑即可（失败返回非零退出码）。

> 少数测试（`test_store_retention.py`、`test_server.py`）以 `from app.xxx import` 形式导入，
> 需要把仓库根也放进导入路径：`PYTHONPATH=".:.." python3 tests/test_store_retention.py`。
> 其余测试从 `app/` 目录直接跑即可。
前三个必须在 K3 真机上跑（解释器 `/usr/bin/python3`）：

```bash
cd /opt/devcon/app
python3 tests/test_qr.py            # T-4 二维码：纯 Python 独立解码回环 + EC 边界
python3 tests/test_worker.py        # T-1 推理 worker：AI 核预算 / 哨兵协议 / 降级（约 40s）
python3 tests/test_camera.py        # T-2 UVC 抓帧：by-id / 双分辨率 / 明确报错 / mock
```

下面这几个**不需要相机**（抓帧器是假的），任何机器都能跑 —— 它们守的是现场最疼的几件事：

```bash
python3 tests/test_preview_hub.py     # 预览分发：单生产者 / 多页面共享 / 掉线后自己接回来
python3 tests/test_camera_heal.py     # 相机自愈判定：连续读空 → 重开、冷却防抖、拍照降级
python3 tests/test_matte_policy.py    # 抠像顺序：先裁切后整幅、三道兜底、单路失败不拖垮
```

`test_matte_policy.py` 最后一段是**真模型冒烟**：本机放了 `modnet.onnx`
（或设了 `BOOTH_MATTE_MODEL`）就真跑一次，没放就如实 `SKIP`。
抠像固定走 CPU，所以这一段**不需要 AI 核**，开发机上就能验。

前端那一段（取景流守护）用 node 跑，开发机上有 node 就行，K3 上没有 node 可跳过：

```bash
node tests/test_preview_client.js   # 没帧/画面卡住 → 报状态 + 自动重连
```

想看单条结论就看 `[PASS] / [FAIL] / [SKIP]` 行；`[SKIP]` 是**如实降级**
（AI 核被 yolos-box 占着、相机没插、本机没有 cv2），不是通过。
`test_worker.py` 里 seg/det 的时延只在 `ep=true` 时才作为验收读数 ——
拿到 CPU 时只打印 CPU 中位数并明确标注「无法验时延」。