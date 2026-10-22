# 贡献指南

## 开发环境

```bash
cd app
python3 booth_server.py --port 8090 --mock-camera
```

没有相机时用 `--mock-camera`，界面上会显式显示 MOCK 标记。请勿在提交里假装真实硬件可用。

## 提交前自检

```bash
cd app

# 纯逻辑测试，直接跑
python3 tests/test_qr.py
python3 tests/test_subject_rules.py

# 这几条以 "from app.xxx import" 导入，需要把仓库根也放进 sys.path
PYTHONPATH=".:.." python3 tests/test_store_retention.py
PYTHONPATH=".:.." python3 tests/test_server.py
```

- 测试用标准库断言，退出码非 0 即为失败。
- 输出里的 `SKIP` 表示环境缺失（无相机 / 无模型 / 无 AI 核），不是通过。
- 涉及视觉改动的提交，请附上 `app/tools/preflight.py` 的输出或截图。

## 代码约定

- 控制面保持纯标准库（`http.server`），不引入 Web 框架。
- `cv2` 只出现在 `app/camera.py`；`onnxruntime` / `spacemit_ort` 只出现在推理 worker。
- 中文注释与文档保持一致；面向现场值守人员的文案保持非技术表述。
- 不要提交现场采集数据、令牌、内网地址或设备凭据。
