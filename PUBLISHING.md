# 发布前检查单（内部）

这份文件面向**把仓库推出去的人**，不是给读者看的。发布前逐条过一遍，别跳过最后两节。

## 一、本次开源化做了什么

| 处理 | 内容 |
|---|---|
| 数据清空 | `app/data/` 只留空目录与 `.gitkeep`；无照片、掩码、二维码、`records.jsonl`、`counter.json` |
| 敏感信息 | 内网设备 IP → `192.0.2.10`（RFC 5737 测试段）；域名 → `pick.example.com` / `photo.example.com`；服务器 IP → `198.51.100.20`；设备密码 → `<DEVICE_PASSWORD>`；个人绝对路径 → `~/devcon2026` |
| 规划流水线 | `.plan/`（`state.json`、`run-log.jsonl`、`evidence/`、历史版本）整体不带入；只有三份规格进 `docs/spec/` |
| 早期原型 | 浅色版原型目录整体移除（仓库只保留 `app/` 一份现场实现），相关文档改为「未随开源发布」表述 |
| 一次性探针 | 根目录 100+ 个 `*.py` 探测脚本、`app/tools` 里的临时校验脚本、`app/tests` 里的一次性核查脚本未带入 |
| 模型 | `app/assets/models/` 与 `app/assets/video/` 只留 `.gitkeep`；运行时按 `BOOTH_*_MODEL` 指向外部路径 |
| 体积 | 部署包 `samples/`、`.venv-test/`、抓取来的品牌参考页未带入 |
| **AIGC 渠道** | 商用中转商的地址与私有型号名全部清掉：默认地址改为占位 `https://api.example.com/v1`（可用 `BOOTH_AIGC_BASE_URL` 覆盖），模型预设从 10 条内部型号改为 3 条通用示例，代码里 `AIGC_MODEL` 默认值改为公开型号 `gpt-image-1`；公开平台（魔搭 / 火山方舟）的协议适配保留 |
| 风格目录 | `app/data/style-catalog.json` 作为出厂数据带回：9 个风格保留，**46 条修订历史清空**（212 KB → 48 KB），型号名统一为示例值 |
| 发号器 | `counter.json` 重置为 `{"year": 2026, "value": 0}`，现场编号不外泄 |

## 二、已实测（本机 Windows + 内置 Python 3.12）

- `python -m compileall app` → 全部通过
- `app/tests/test_qr.py` → 45 项，失败 0，跳过 1（cv2 交叉验证，本机无 cv2）
- `app/tests/test_subject_rules.py` → 23 项全部通过
- `app/tests/test_aigc_channels.py` → 30 项通过；`test_auto_flow_config.py` → 55 项通过；
  `test_aigc_catalog.py` → 10/10；`test_aigc_catalog_v3.py` → 39/39
- 渠道接口实测：`GET /api/operator/aigc/channels` 返回 3 条预设，`base_url` 全为占位域名，
  型号为 `gpt-image-1` / `gemini-2.5-flash-image` / `seedream-4-0-250828`
- `app/tests/test_store_retention.py` → 8 项；**存在偶发**：以 24 小时整点边界断言令牌失效，浮点往返可能差 1 微秒，约有概率误报 1 项失败（源项目同一测试也如此）
- `python app/booth_server.py --port 8090 --mock-camera` → 启动成功，`/api/health`、`/`、`/stage`、`/operator`、`/join`、`/p/<token>`、`/api/state` 全部 200；
  静态资源 `/static/assets/brand/spacemit-logo.svg` 也返回 200
- `app/tests/test_server.py` → 路由、同意门禁、令牌格式、404 语义等前 40 项全部 PASS；
  进入"出图"链路后报 `ModuleNotFoundError: No module named 'cv2'`，其后约 10 项连带 FAIL。
  **这是本机缺 cv2 导致的环境缺失，不是代码问题**，在 K3 或装齐依赖的开发机上应全绿

## 三、未实测（别在发布说明里写成"已验证"）

- **K3 真机**：本轮未连设备，未跑 `tests/test_worker.py`、`test_camera.py`、`e2e_test.py`
- **完整出图链路**：本机无 cv2，`test_server.py` 的出图段与 `/api/photo`、`/api/qr` 断言未通过（见上）
- **打印链路**：CP1500 相关脚本未跑（仓库里默认 `BOOTH_PRINT_ENABLED=false`）
- **手势子系统**：未跑（需要 cv2 + MediaPipe + 真机）
- **AIGC 风格化**：未跑（需要外部渠道凭据）

### 关于 AIGC 渠道

仓库里已经没有任何真实渠道：没有网关地址、没有密钥、没有私有型号名。
启用风格化需要你自己接一个 OpenAI 兼容渠道，两种方式写在 `docs/魔搭模型接入.md` 开头：

1. 操作员面板「模型渠道」逐个填 `base_url` / `api_key` / 模型名；
2. 部署时设 `BOOTH_AIGC_BASE_URL` 作为默认地址。

密钥只存本机数据目录，接口仅返回 `key_configured: true`。

## 四、发布前必须由人拍板

1. **许可与版权主体**：当前 `LICENSE` 为 Apache-2.0 全文，`NOTICE` 写 `Copyright 2026 SpacemiT`。若主体名称不同，改 `NOTICE` 第一段即可。
2. **品牌素材**：`app/assets/brand/`、`app/assets/bg/`、`app/assets/cards/` 属 SpacemiT 视觉资产，已在 `NOTICE` 声明仅用于活动语境；对外发布需品牌方确认。
3. **合规文案**：`docs/04-合规与现场文案.md` 与拍摄告知文案需法务确认后才能作为公开示例。
4. **字体**：`app/assets/fonts/NotoSansCJK-Bold.ttc` 为 SIL OFL 1.1，允许再分发，需保留 `OFL.txt`（已在位）。

## 五、建议的发布动作（由你自己执行）

```bash
cd spacemit-devcon2026-cyber-idcard
git init
git add -A
git status            # 逐项确认没有 data/、models/、*.onnx、__pycache__
git commit -m "开源发布：SpacemiT DevCon 2026 装置 5 赛博开发者证件照"
```

如果素材体积让仓库偏大（当前约 93 MB，主要来自字体与底图），可选 Git LFS 或把
`app/assets/bg`、`app/assets/styles` 的历史候选图移出仓库并在文档里给下载方式。

## 六、回滚

原始工作副本未做任何删除，仍在
`<原始工作副本目录>`。
本仓库是独立新建的目录，直接删除本目录即可回到开工前状态。
