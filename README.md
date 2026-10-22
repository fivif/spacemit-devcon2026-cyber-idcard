# 赛博开发者证件照 · Cyber Developer ID Card

> SpacemiT DevCon 2026 现场互动装置（装置 5）的开源实现。
> 参会者拍一张照片，由 **SpacemiT K3（RISC-V）的 AI 核**现场完成人像分割与风格化，
> 合成带编号、水印与取图二维码的「开发者证件照」，手机扫码取图，现场可打印。

这个装置要证明的不是「能拍照」，而是**算力被看见**：大屏上人像掩码逐像素成形，
成片旁标注真实推理耗时与 `EP SpaceMIT` 徽章——这张照片是这块芯片自己算出来的。

- 全流程本机完成：不上传云端、不做人脸识别、不做身份比对
- 出图不依赖外网：只有手机扫码取图需要与装置处于同一局域网（或自建反代/隧道）
- 照片仅存于装置本机，默认保留 24 小时后自动清理
- 取图只认 22 位随机令牌，连号编号仅用于显示，不能作为入口

---

## 一、现场形态

同一个服务（默认端口 **8090**）提供四个界面：

| 路径 | 页面 | 使用者 | 作用 |
|---|---|---|---|
| `/` | 拍摄位 | 参会者 + 值守同事 | 实时取景、选风格、填署名、勾选拍摄告知、按快门、看成片与二维码 |
| `/stage` | 算力视图（大屏） | 围观者 | 实时预览、人像掩码成形过程、真实推理耗时、EP / CPU 徽章、最近成片 |
| `/operator` | 现场管理 | 值守同事 | 运行状态、排队、成片、风格、系统维护 |
| `/p/<token>` | 取图页 | 参会者手机 | 大图、保存、复制分享文案、删除我的照片 |

管理后台另有 `/operator/queue`、`/operator/photos`、`/operator/styles`、
`/operator/settings`、`/operator/system` 五个独立页面。

## 二、技术架构

```text
手机（同一局域网）
      │  扫码 → /p/<token>
      ▼
┌──────────────────────────── K3（SpacemiT K3 Pico-ITX）────────────────────────────┐
│  booth_server.py  控制面 · 纯标准库 http.server · 端口 8090                        │
│      │                                                                            │
│      ├── camera.py        UVC 抓帧 + 预览分发 + 掉线自愈（唯一 import cv2 的地方）  │
│      ├── infer_client.py  ──► infer/worker.py   人像分割 / 检测（唯一碰 onnxruntime）│
│      │                        └─ SpaceMIT EP（AI 核）                             │
│      ├── gesture/worker.py  手势识别（OK 手势触发自动拍摄，可选）                  │
│      ├── render/            成片合成：keyvisual / stylize / card / watermark / qr  │
│      ├── store.py           编号发号器 + 记录落盘 + 留存删除                       │
│      ├── print_adapter.py   CUPS 打印（默认关闭）                                  │
│      └── aigc_*.py          可选：外接 OpenAI 兼容的 AIGC 风格化渠道               │
│                                                                                   │
│  static/   四个界面（原生 HTML/CSS/JS，无构建步骤）                                │
│  data/     照片 / 缩略图 / 掩码 / 二维码 / records.jsonl / counter.json            │
└───────────────────────────────────────────────────────────────────────────────────┘
```

关键工程设计（都是现场踩出来的，内部踩坑记录未随仓库发布）：

- **推理必须另起进程**：`cv2` 与 `spacemit_ort` 同进程会直接崩；`onnxruntime` 必须先于 `spacemit_ort` 导入。
- **MODNet 抠像固定走 CPU**：挂 AI 核是进程硬崩，不是变慢。
- **不假装成功**：没有相机就显式 MOCK 黄标，没有 AI 核就如实降级并标 `CPU`，测试里的 `SKIP` 不等于 `PASS`。
- **取图入口是令牌不是编号**：编号连号可枚举，当入口等于把全场照片变成下载器。

## 三、目录结构

```text
.
├── README.md                    本文件
├── CONTRIBUTING.md              开发与自检约定
├── PUBLISHING.md                发布前检查单
├── LICENSE                      Apache-2.0
├── NOTICE                       第三方素材与商标声明
├── requirements.txt             运行依赖（K3 上多数已预装）
└── app/                         ★ 现场系统：部署到设备上的就是这一份
    ├── booth_server.py            控制面（纯标准库，端口 8090）
    ├── camera.py                  UVC 抓帧与预览分发
    ├── infer_client.py            推理客户端（worker 子进程）
    ├── store.py                   编号发号器 + 记录落盘 + 留存删除
    ├── auto_flow/                 排队 / 同意 / 倒计时 / 自动拍摄（可选子系统）
    ├── infer/                     worker（onnxruntime / spacemit_ort）+ 文本编码
    ├── gesture/                   手势 worker
    ├── render/                    成片合成 / 风格化 / 水印 / 二维码 / 打印排版
    ├── static/                    四个界面（原生 HTML/JS，无构建）
    ├── assets/                    底图、品牌、字体、风格预览（模型不随仓库分发）
    ├── data/                      运行时数据（出厂三份目录文件 + 空目录）
    ├── deploy/                    systemd 单元、Mediamtx、CUPS 配置与部署说明
    ├── docs/                      现场故障处置手册 + 彩排记录
    ├── samples/                   出样张与合成自检图
    └── tests/                     控制面 / 相机 / 推理 / 前端契约测试
```

> 物料清单、现场 SOP、合规文案、展台设计、技术规格与技术知识库属于内部文档，
> 本仓库只发布可运行的现场系统与它自己的说明。

## 四、快速开始（无相机空跑）

服务是纯标准库实现，不需要安装 Web 框架；只有推理、图像处理和相机需要额外依赖。

```bash
cd app

# 合成源空跑：界面上会显示 MOCK 黄标，不会冒充真相机
python3 booth_server.py --port 8090 --mock-camera
```

然后浏览：

- 拍摄位 `http://127.0.0.1:8090/`
- 大屏 `http://127.0.0.1:8090/stage`
- 操作员 `http://127.0.0.1:8090/operator`
- 取图 `http://127.0.0.1:8090/p/<取图令牌>`

运行环境：

- Python 3.10+（K3 上使用系统 `/usr/bin/python3`）
- 必需：`Pillow`、`numpy`、`opencv-python`（`cv2`）
- 推理：`onnxruntime`，K3 上另需 `spacemit_ort`
- 前端测试脚本另需 Node.js

## 五、真机部署（K3）

```bash
# 1) 安装依赖（Bianbu 系统通常已预装 cv2 / onnxruntime / spacemit_ort）
python3 -m pip install -r requirements.txt

# 2) 放置模型（不随仓库分发，见下节）
#    默认路径：/opt/devcon/assets/models/

# 3) 部署文件并安装服务单元
bash app/tools/deploy.sh --ssh root@<K3-IP> --source ./app --install-unit

# 4) 重启并自检
ssh root@<K3-IP> "systemctl restart devcon-booth"
curl -s http://<K3-IP>:8090/api/health
```

完整步骤、systemd 单元、大屏 kiosk、介质与权限见 `app/deploy/README.md` 与
`app/deploy/展前检查单.md`；现场排障见 `app/docs/现场故障处置.md`。

## 六、模型与素材

模型因体积与分发约定不随仓库提供，放到 `app/assets/models/` 或 `/opt/devcon/assets/models/`
（路径可用环境变量覆盖）：

| 用途 | 默认路径 | 环境变量 | 备注 |
|---|---|---|---|
| 人像分割 | `/opt/devcon/assets/models/yoloe-v8s-seg.dynq.onnx` | `BOOTH_SEG_MODEL` | SpaceMIT EP |
| 文本编码 | `/opt/devcon/assets/models/mobileclip.q.onnx` | `BOOTH_CLIP_MODEL` | 固定 CPU |
| 人形检测 | `/usr/share/yolo-demo/yolov8/yolov8n.q.onnx` | `BOOTH_DET_MODEL` | 取景辅助 |
| 人像抠像 | `/opt/devcon/assets/models/modnet.onnx` | `BOOTH_MATTE_MODEL` | 固定 CPU |

模型可从 SpacemiT 模型动物园获取（`https://archive.spacemit.com/spacemit-ai/model_zoo/vision/`）。
全部环境变量、默认值与调参边界见 `app/README.md`。

### 可选：AIGC 风格化渠道

风格化能力（把真人抠像重绘成手绘/漫画等风格）需要外接一个 OpenAI 兼容的图像接口，
**本仓库不带任何渠道地址与密钥**：

- 代码里的默认地址是占位值 `https://api.example.com/v1`，模型预设是三条示例；
- 部署时用 `BOOTH_AIGC_BASE_URL` 指定默认地址，或在操作员面板「模型渠道」里逐个填；
- 密钥只存本机数据目录，接口只回 `key_configured: true`，不回显原文；
- 不接渠道时功能默认关闭（`BOOTH_AIGC_ENABLED=false`），拍摄与出图不受影响。

接入步骤与型号改写提示见 `内部文档`。

## 七、隐私与合规

- **不做人脸识别**：只做人像/前景分割，不提取人脸特征、不做身份比对、不建人脸库。
- **数据不出机**：照片、掩码、成片全部写在本机数据目录；只有取图页在局域网内提供下载。
- **默认 24 小时保留**：`retention_hours=24`，过期照片与派生文件会被清理。
- **取图令牌**：每张照片一个 22 位随机串（128 bit），与编号解耦。
- **同意门禁**：拍摄前必须勾选《拍摄告知》，同意令牌有时效；服务端强制校验。
- **访问日志默认关闭**：不记录 IP。

现场文案与合规边界见 `开发文档`；分享能力（什么能做、什么做不到）
见 `开发文档`。正式对外使用前请按当地法规完成法务审核。

## 八、测试

测试不依赖 pytest，使用标准库断言，**退出码非 0 表示 FAIL**；输出中的 `SKIP`
表示如实降级（没相机、没 AI 核、没模型），**不是通过**。

```bash
cd app

python3 tests/test_qr.py            # 二维码编码/解码回环
python3 tests/test_subject_rules.py # 主体选取规则（坏值不崩）

# 以 app 包方式导入的测试，需要把仓库根也加进 sys.path
PYTHONPATH=".:.." python3 tests/test_store_retention.py
PYTHONPATH=".:.." python3 tests/test_server.py   # 全部路由 + 同意门禁 + 删除链路

python3 tests/test_camera.py        # UVC 抓帧（无相机时如实 SKIP）
python3 tests/test_worker.py        # 推理 worker（需要 onnxruntime 与模型）
node   tests/test_preview_client.js # 前端取景流守护（需要 node）
```

彩排脚本 `app/tests/e2e_test.py --n 20 [--mock]` 会跑一遍完整流程并输出成绩单。

## 九、文档

仓库内自带、随代码一起维护的说明：

| 文件 | 内容 |
|---|---|
| `app/README.md` | 系统全貌、接口契约、全部环境变量、参数边界 |
| `app/deploy/README.md` | 部署、预检、大屏 kiosk |
| `app/deploy/展前检查单.md` | 开场前逐项勾选 |
| `app/docs/现场故障处置.md` | 面向非技术值守同事的排障手册 |
| `app/docs/彩排记录.md` | 彩排过程中的实测记录 |
| `app/tests/README.md` | 测试怎么跑、`SKIP` 是什么意思 |
| `CONTRIBUTING.md` | 开发环境与提交前自检 |

物料清单、现场 SOP、合规文案、展台设计、技术规格与知识库属于活动内部文档，
没有随本仓库发布；`app/README.md` 里已经把系统层面的接口与参数写全，照着就能部署。

## 十、许可与致谢

代码以 **Apache-2.0** 发布，见 `LICENSE`。第三方素材、字体与商标的边界见 `NOTICE`：

- `app/assets/fonts/NotoSansCJK-Bold.ttc` 为 Noto Sans CJK，SIL Open Font License 1.1（`app/assets/fonts/OFL.txt`）。
- SpacemiT 名称、Logo 与活动主视觉属于 SpacemiT，仅用于本项目现场装置语境。
- 风格预览图中的人像为合成/AIGC 素材，不含真实参会者照片。

装置设计、现场流程与运维知识来自 SpacemiT DevCon 2026 装置 5 的实际落地过程。
