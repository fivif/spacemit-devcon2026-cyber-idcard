# 项目全貌速查卡（打印版）

> 贴在显示器旁边或现场工位，快速查找关键信息。

---

## 🎯 项目基本信息

**项目名称**：装置 5 · 赛博开发者证件照  
**用途**：DevCon 2026 现场互动装置（拍照 → AI 抠像 → 暗调成片 → 扫码取图）  
**硬件**：SpaceMIT K3 RISC-V 开发板 + USB 摄像头  
**技术栈**：Python + Flask + ONNX Runtime + cv2 + MediaPipe  

---

## 📍 关键路径

### Windows 开发机
```
代码：~/devcon2026\app\
知识库：...\knowledge\
启动：python booth_server.py --mock-camera
浏览：http://localhost:8090/
```

### K3 真机
```
IP：192.0.2.10
用户：root（无密码 SSH）
代码：/opt/devcon/app/
服务：systemctl restart devcon-booth.service
日志：journalctl -u devcon-booth.service -f
浏览：http://192.0.2.10:8090/
```

---

## ⚡ 5 分钟快速上手

```bash
# 1. 读知识库索引（5 分钟）
cat knowledge/00-知识库索引.md

# 2. 本地验证（10 分钟）
cd app
python booth_server.py --mock-camera
# 浏览器打开 http://localhost:8090/

# 3. 部署到 K3（5 分钟）
scp booth_server.py root@192.0.2.10:/opt/devcon/app/
ssh root@192.0.2.10 "systemctl restart devcon-booth.service"

# 4. 查看状态（1 分钟）
ssh root@192.0.2.10 'curl -s http://localhost:8090/api/state | python3 -m json.tool'
```

---

## 🚨 当前问题（2026-10-10）

**症状**：手势检测不可用（做手势没反应）  
**原因**：`infer.seg()` 超时 8 秒 → 人形框获取失败 → `gesture_obs.available=false`  
**影响**：手势触发拍照完全不可用  
**解决方案**：见 `docs/spec/自动流程规格-v1.md` 中「手势与拍摄门禁」一章。

---

## 🔧 常用命令

### 本地开发
```bash
cd app
python booth_server.py --mock-camera           # 模拟模式
python booth_server.py --port 8091             # 换端口
```

### K3 部署
```bash
# 上传文件
scp <文件> root@192.0.2.10:/opt/devcon/app/<路径>

# 重启服务
ssh root@192.0.2.10 "systemctl restart devcon-booth.service"

# 查看日志
ssh root@192.0.2.10 "journalctl -u devcon-booth.service -f"
ssh root@192.0.2.10 "journalctl -u devcon-booth.service -n 100"

# 查看状态
ssh root@192.0.2.10 'curl -s http://localhost:8090/api/state | python3 -m json.tool'
```

### 日志过滤
```bash
journalctl -u devcon-booth.service -f | grep -E "error|超时"
journalctl -u devcon-booth.service -f | grep gesture
journalctl -u devcon-booth.service -f | grep "seg|det"
```

---

## 📁 核心文件

| 文件 | 职责 | 行数 |
|------|------|------|
| `booth_server.py` | Flask 主服务 | ~2500 |
| `infer_client.py` | 推理客户端 | ~267 |
| `infer/worker.py` | YOLOE 推理 worker | ~800 |
| `gesture/worker.py` | handpose worker | ~500 |
| `camera.py` | 相机抓帧 | ~400 |
| `presence.py` | 在场检测 | ~190 |
| `render/card.py` | 成片合成 | ~600 |
| `gesture_client.py` | 手势客户端 | ~400 |

---

## 🔐 K3 约束（违反会炸）

1. ❌ **必须用 `/usr/bin/python3`**（不能用 venv）
2. ❌ **cv2 和 spacemit_ort 不能同进程**（会段错误）
3. ❌ **AI 核只有 8 槽**（seg=4 + det=2 + matte=2 = 8/8 已满）
4. ✅ **推理 worker 必须是子进程**（隔离 cv2 和 spacemit_ort）
5. ✅ **手势 worker 必须 CPU-only**（不能占用 EP）

---

## 🌐 四个界面

| URL | 页面 | 谁用 |
|-----|------|------|
| `/` | 拍摄位 | 参会者 + 值守同事 |
| `/stage` | 算力视图（大屏） | 围观者 |
| `/operator` | 操作员面板 | 值守同事 |
| `/p/<令牌>` | 取图页 | 参会者手机 |

---

## 📊 关键 API

```bash
GET  /api/state              # 全量状态（相机/推理/手势/在场/磁盘）
GET  /api/health             # 探活
POST /api/capture            # 触发拍照
GET  /api/preview.mjpg       # 实时预览流
POST /api/consent            # 记录同意
DELETE /api/photo/<令牌>     # 删除照片
GET  /api/photo/<令牌>.jpg   # 成片
```

---

## 🔍 排查清单

### 服务起不来
```bash
# 1. 查看详细错误
journalctl -u devcon-booth.service -n 50

# 2. 检查 Python 路径
ssh root@192.0.2.10 "which python3"  # 必须是 /usr/bin/python3

# 3. 手动启动看错误
ssh root@192.0.2.10 "cd /opt/devcon/app && /usr/bin/python3 booth_server.py"
```

### 手势不触发
```bash
# 1. 检查是否启用
ssh root@192.0.2.10 "systemctl show devcon-booth.service | grep BOOTH_GESTURE_ENABLED"

# 2. 查看 gesture_obs
curl http://192.0.2.10:8090/api/state | jq .gesture_obs

# 3. 查看日志
journalctl -u devcon-booth.service -f | grep gesture
```

### 相机黑屏
```bash
# 1. 检查设备
ssh root@192.0.2.10 "ls -l /dev/video0"

# 2. 检查占用
ssh root@192.0.2.10 "lsof /dev/video0"

# 3. 查看相机状态
curl http://192.0.2.10:8090/api/state | jq .camera
```

---

## 📚 知识库导航

| 文档 | 用途 | 何时读 |
|------|------|--------|
| `00-知识库索引.md` | 导航入口 | 第一个读 |
| `07-新接手者必读.md` | 项目概览 | 必读 |
| `10-当前阻塞问题与解决方案.md` | 当前问题 | 必读 |
| `04-踩坑与教训.md` | 避坑指南 | 必读 |
| `08-技术架构与数据流.md` | 系统架构 | 改核心逻辑前 |
| `09-K3真机部署与调试.md` | 部署指南 | 第一次部署前 |

---

## ⚙️ 环境变量速查

```ini
# 手势检测
BOOTH_GESTURE_ENABLED=true               # 启用手势检测
BOOTH_GESTURE_ENGINE=handpose            # handpose / yolo
BOOTH_GESTURE_TYPE=thumbs_up             # thumbs_up / ok
BOOTH_GESTURE_PROBE_INTERVAL=1.5         # 检测间隔（秒）
BOOTH_GESTURE_STABLE_FRAMES=3            # 连续多少帧触发
BOOTH_GESTURE_HAND_CONF=0.30             # 置信度门槛

# 在场检测
BOOTH_PRESENCE=true                      # 启用在场检测
BOOTH_PRESENCE_INTERVAL=1.2              # 检测间隔（秒）

# 其他
BOOTH_DATA=<data目录>                    # 数据目录
BOOTH_RETENTION_HOURS=168                # 照片留存期（7天）
```

---

## 🎯 核心概念

**取图令牌 vs 编号**
- 编号 `SM-2026-0001`：连号，只显示，不能取图
- 令牌 `V3f2O3CT2bl_Yt8hov2LLQ`：128 位随机，真正凭证

**SpaceMIT EP（AI 核）**
- 8 槽位，seg 占 4、det 占 2、matte 占 2（已满）
- 单进程独占，cv2 不能同时用

**行式 JSON 协议**
- worker 是子进程，stdin/stdout 通信
- 响应带 `@@J ` 前缀（区分 EP 噪声）

**人形框验真**
- handpose 会凭空拟合关键点（空画面 bug）
- 需要先 seg 检测人形框，手在框内才有效
- **当前问题**：seg 超时，框获取不到

---

## 🆘 紧急联系

**知识库位置**：`~/devcon2026\knowledge\`

**在线文档**：知识库 13 个 Markdown 文件，总计 ~5 万字

**新手路径**：
1. `00-知识库索引.md`（5 分钟）
2. `07-新接手者必读.md`（15 分钟）
3. `10-当前阻塞问题与解决方案.md`（10 分钟）

**总时间**：15 分钟理解 + 1 小时修复当前问题

---

最后更新：2026-10-10
