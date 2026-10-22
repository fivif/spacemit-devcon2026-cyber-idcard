# 装置 5 · 赛博开发者证件照 —— 部署与预检

真机：`root@192.0.2.10`（K3 Pico-ITX，Bianbu 4.0.1，kernel 6.18.3-riscv64，8 核 / 7.7 GiB）
代码：`/opt/devcon/app`　模型：`/opt/devcon/assets/models`　服务端口：**8090**
解释器：`/usr/bin/python3`（3.14.4，自带 cv2 4.10 / onnxruntime 1.24.2+spacemit.a1 / spacemit_ort 2.0.6）
**禁止 venv、禁止 pip 安装**（规格 §10）。视频预览使用 `Fitz8863/spacemit-mediamtx` 的 riscv64 版本，
相机只由应用读取一次，K3 用 `h264_stcodec` 硬件编码，浏览器通过 WebRTC 播放 2560×1440 预览。

> 本目录交付 4 件东西：
> `deploy/devcon-booth.service`（开机自启单元）、`tools/deploy.sh`（一键同步+重启）、
> `tools/preflight.py`（开场前逐项预检）、以及本文件（上线/回滚/大屏）。
>
> 大屏 kiosk **故意不单独交付单元**：它依赖具体图形会话（本机是 SDDM + labwc/Wayland），
> 用一段会话自启比硬塞一个 systemd 单元更稳。命令与可选单元片段见 §4。

---

## 1. 十分钟上线（一次性）

```bash
# ── 0) 连上设备（用 ssh / scp，或你偏好的同步工具）
#    rsync -av app/ root@<K3-IP>:/opt/devcon/app/
#    scp <local> root@<K3-IP>:<remote>
#    ssh root@<K3-IP> "<命令>"
```

### 步骤 1 · 确认模型在位（不在就先去下载，见 §6）

```bash
ls -l /opt/devcon/assets/models/
#   yoloe-v8s-seg.dynq.onnx   13.9 MB   分割（开放词汇实例分割，走 AI 核）
#   mobileclip.q.onnx        140.0 MB   文本编码器（走 CPU）
ls -l /usr/share/yolo-demo/yolov8/yolov8n.q.onnx
#   yolov8n.q.onnx             3.5 MB   检测（预览人形框 / 无分割时的兜底）
```

★ 现场事实：**检测模型不在 `/opt/devcon/assets/models`**，而在 `/usr/share/yolo-demo/yolov8/`。
`infer/worker.py` 的默认值就是该路径，systemd 单元里也用 `BOOTH_DET_MODEL` 显式钉住了它。
`deploy.sh` **不搬模型**（模型是资产，不是代码）。

### 步骤 2 · 同步代码（幂等，可反复执行）

```bash
# 先把要发布的 app/ 放到设备上的暂存目录，再由设备侧脚本同步（推荐）
cp -a /opt/devcon/app /opt/devcon/staging/app        # 或者用 k3.py 上传到该目录
bash /opt/devcon/staging/app/tools/deploy.sh --source /opt/devcon/staging/app --install-unit
```

`deploy.sh` 会依次做：**备份 data/** → rsync 同步（永不删 `data/`、永不碰 `assets/models`）
→ 安装 `devcon-booth.service` + `daemon-reload` → `systemctl enable --now/restart` → 探活 `/api/state`。
带 `--dry-run` 可以先看它要干什么；带 `--prune` 才删目标里多余的文件（默认不删）。

> 已经用 `k3.py sync` 直接把本地 app/ 传上去了也行 —— 那条路 `data/` 本来就在排除表里，
> 但**不会**做备份。想留一份备份就照上面走 `deploy.sh`。

### 步骤 3 · 装单元并开机自启

```bash
install -m 0644 /opt/devcon/app/deploy/devcon-booth.service /etc/systemd/system/
# 看门狗（进程外那道保险）：单元 + 定时器一起装
install -m 0644 /opt/devcon/app/deploy/devcon-booth-watchdog.service /etc/systemd/system/
install -m 0644 /opt/devcon/app/deploy/devcon-booth-watchdog.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now devcon-booth.service
systemctl enable --now devcon-booth-watchdog.timer
systemctl status devcon-booth.service --no-pager
systemctl list-timers devcon-booth-watchdog.timer --no-pager
journalctl -u devcon-booth -n 40 --no-pager
```

（`deploy.sh --install-unit` 会同时安装 MediaMTX；首次部署会从 `Fitz8863/spacemit-mediamtx`
下载 `v1.20.1` 的 `linux_riscv64` 包。）

### 视频服务端口

| 服务 | 端口 | 用途 |
|---|---:|---|
| MediaMTX RTSP | 8554/TCP | K3 本机发布入口，仅本机使用 |
| MediaMTX WebRTC | 8889/TCP | 浏览器 WHEP 协商 |
| MediaMTX 媒体 | 8189/UDP/TCP | WebRTC 视频媒体 |
| 拍摄服务 | 8090/TCP | 页面、状态、同源 WHEP 转发 |

浏览器页面只访问 `8090`，不用直接访问 RTSP；`/api/preview.whep` 会同源转发到
`127.0.0.1:8889/booth/whep`。云端反代或防火墙需要放行 WebRTC 的 8889 和 8189，
否则页面会显示「视频服务未就绪」。

### 步骤 4 · 开场前预检（**必须跑**）

```bash
/usr/bin/python3 /opt/devcon/app/tools/preflight.py ; echo "exit=$?"
```

有 FAIL 就非零退出。逐项含义见 §3。

### 步骤 5 · 冒烟：服务 + 大屏

```bash
curl -fsS http://127.0.0.1:8090/api/health ; echo
curl -fsS http://127.0.0.1:8090/api/state | head -c 400 ; echo
chromium --kiosk --app=http://127.0.0.1:8090/stage &
```

---

## 2. systemd 单元（为什么这么写）

`deploy/devcon-booth.service` 里几处「按现场实际」的取舍（都有理由，别照抄教程改掉）：

| 写法 | 理由 |
|---|---|
| `After=` + `Wants=network-online.target` | 本装置**不依赖公网**（断网也要 100% 可用），但取图二维码里的地址来自 `booth_server.lan_ip()` 的本机地址探测；网卡还没拿到 IP 时会退到 127.0.0.1，手机扫不到。`network.target` 只说明网络栈起来了、不保证拿到地址，所以用 `network-online.target`。（若现场是固定 IP，建议再显式钉一行 `Environment=BOOTH_PUBLIC_HOST=192.0.2.10`。） |
| `WantedBy=multi-user.target` | 服务只做 HTTP + V4L2 抓帧 + 子进程推理，不碰 X/Wayland。挂 `multi-user.target` 才能保证「桌面挂了、服务还在」——大屏 kiosk 反而是它的下游。 |
| 不写 `After=systemd-udev-settle.service` | `camera.py` 每次抓帧都会重新解析 by-id 并带 2s 退避重连，USB 枚举慢会自愈；而 `udev-settle` 会把开机拖慢几十秒，对「开机就要能用」是净损失。 |
| `Restart=always` + `RestartSec=2` + `StartLimitIntervalSec=0` | 无人值守：崩了无限重启；2s 间隔让 AI 核线程槽有机会被回收（起太快会撞 `Not enough available AI cores` 而静默退回 CPU）。 |
| `Environment=BOOTH_EP_THREADS_SEG=4` / `DET=2` | 全板只有 **8 个** AI 核线程槽，留 2 槽余量（规格 §1.2）。 |
| `User=root` | `/dev/video1` 抓帧 + `libspine_tcm` 释放 AI 核残留都需要权限，真机验证也是 root 跑的。 |
| `StandardOutput/Error=journal` | 日志统一走 journald：`journalctl -u devcon-booth -f`。 |
| 另有 `devcon-booth-watchdog.{service,timer}` | 进程外看门狗：进程内自愈救不了「进程自己卡死」（抓帧卡在内核、HTTP 线程被拖死、死锁）。它每 20s 只看 `/api/*`，判定「真卡死」才 `systemctl restart devcon-booth`；有拍摄任务在跑时不动手，服务不是 systemd 管着时也不动手（宁可人工确认，也不在台下多起一个抢相机的进程）。日志：`journalctl -u devcon-booth-watchdog -f` |
### 环境变量（单元里已写死，现场可用 `/etc/devcon/booth.env` 覆盖）

| 变量 | 单元里的值 | 说明 |
|---|---|---|
| `BOOTH_PORT` | `8090` | HTTP 端口。**8080 是另一套 yolos-box，不要动** |
| `BOOTH_HOST` | `0.0.0.0` | 手机要能扫码取图，不能只听 127.0.0.1 |
| `BOOTH_DATA` | `/opt/devcon/app/data` | 照片/记录/编号落盘处，**照片 1 天留存** |
| `BOOTH_ACCESS_LOG` | `0` | 合规：默认不记访问日志（不落 IP） |
| `BOOTH_CONSENT_TTL` | `600` | 同意令牌有效期（秒） |
| `BOOTH_INFER_TIMEOUT` | `2.5` | 超过就降级出图，不让参会者干等 |
| `BOOTH_SEG_MODEL` | `/opt/devcon/assets/models/yoloe-v8s-seg.dynq.onnx` | 分割（AI 核） |
| `BOOTH_CLIP_MODEL` | `/opt/devcon/assets/models/mobileclip.q.onnx` | 文本编码（CPU） |
| `BOOTH_DET_MODEL` | `/usr/share/yolo-demo/yolov8/yolov8n.q.onnx` | 检测（AI 核，2 线程） |
| `BOOTH_EP_THREADS_SEG` / `_DET` | `4` / `2` | AI 核线程预算（全板 8 槽） |
| `BOOTH_PUBLIC_HOST` | 未设 | 设了就钉死二维码里的主机名（固定 IP 现场建议设为 `192.0.2.10`） |

### 大屏交接模式

默认是 `full`：一位用户从抓帧、AI 分割、抠像、AIGC 到入库完整展示，完成后回到待机预览。
多人现场可切为 `handoff`：抠像完成后继续展示 5 秒，随后自动回到实时预览，当前 AIGC 在后台继续，下一位可以开始。

```ini
Environment=BOOTH_PRESENTATION_MODE=full
Environment=BOOTH_AIGC_HANDOFF_DELAY_SECONDS=5
```

改完 systemd 配置后执行 `systemctl daemon-reload && systemctl restart devcon-booth`。

---

## 3. 预检 `tools/preflight.py`

```bash
/usr/bin/python3 /opt/devcon/app/tools/preflight.py                 # 人读，FAIL 即非零退出
/usr/bin/python3 /opt/devcon/app/tools/preflight.py --json | tee /opt/devcon/app/data/preflight.json
/usr/bin/python3 /opt/devcon/app/tools/preflight.py --skip-ai       # 跳过 AI 核探测（几秒出结果）
/usr/bin/python3 /opt/devcon/app/tools/preflight.py --probe-ai      # 服务在跑也另起 worker 复核
```

退出码：`0` = 没有 FAIL（可以有 WARN）；`1` = 至少一项 FAIL；`2` = 用法错误。

覆盖的 9 组：**代码与资源 / 模型 / 相机 / AI 核 / 端口 / 磁盘与留存 / 数据目录 / 零 pip 依赖 / 附加**。

两个容易误判的地方，脚本已经处理好了：

- **服务正在跑**时，8090 一定被占、相机也一定被占 —— 这不算故障。脚本会先认进程：
  占用者是 `booth_server.py`（本服务自己）→ PASS，并改用 `http://127.0.0.1:8090/api/state`
  读相机与 AI 核的**实时读数**；占用者是别的东西（例如某个跑飞的 `camd.py`）→ FAIL 并点名 PID。
- **AI 核读数是复核过的**：不看「进程起没起来」，而是读 worker 的 `get_providers()` 结果。
  拿不到核时是 **WARN**（不是 FAIL）—— 这是设计内的降级（worker 会静默退回 CPU），
  但绝不是小事：分割 205ms → 1160ms。WARN 里会给出原因和排查命令。

---

## 4. 大屏 kiosk（DP，1920x1080）

**手动启动（图形会话里）**

```bash
chromium --kiosk --app=http://127.0.0.1:8090/stage
# 需要的话再加：--noerrdialogs --disable-infobars --disable-session-crashed-bubble
# 127.0.0.1 属安全上下文，不必加 --unsafely-treat-insecure-origin-as-secure
```

**开机自启（推荐，最稳）** —— 本机图形会话是 SDDM + **labwc（Wayland）**，桌面账号是 `jax`（uid 1000）。
labwc 会话在启动时会执行 `~/.config/labwc/autostart`，那里天然带着 `WAYLAND_DISPLAY` /
`XDG_RUNTIME_DIR`，不会有「连不上显示服务器」的问题：

```bash
# /home/jax/.config/labwc/autostart 末尾追加一行（等 5s 让 booth 先起来）
( sleep 5; /usr/bin/chromium --kiosk --app=http://127.0.0.1:8090/stage ) &
```

想免登录（现场别停在 SDDM 登录界面）：在 `/etc/sddm.conf.d/sddm-bianbu.conf` 的 `[Autologin]`
下补一行 `User=jax`（该文件现在只有 `Session=bianbu-lite`，没有 `User=`）。

**可选：用 systemd 管 kiosk（`/etc/systemd/system/devcon-stage.service`）**

```ini
[Unit]
Description=SpacemiT DevCon 2026 装置5 · 大屏 kiosk（/stage）
After=devcon-booth.service network-online.target
Wants=devcon-booth.service

[Service]
Type=simple
User=jax
Group=jax
Environment=HOME=/home/jax
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-0
# 等 labwc 的 Wayland socket 出现（会话没起来时别急着起 chromium）
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/bin/chromium --kiosk --app=http://127.0.0.1:8090/stage --noerrdialogs --disable-infobars --disable-session-crashed-bubble
Restart=always
RestartSec=3

[Install]
WantedBy=graphical.target
```

```bash
systemctl daemon-reload && systemctl enable --now devcon-stage.service
```

> 只有登录进 labwc 会话之后这个单元才起得来（它靠 `/run/user/1000/wayland-0` 连显示）。
> 两种方式选一种即可，别同时开（两个 kiosk 会抢同一个显示）。
---

## 5. 回滚

每次 `deploy.sh` 都会写一份快照到 `/opt/devcon/backup/<时间戳>/`（另有 `/opt/devcon/backup/LAST`
指向最近一次）：

```
<时间戳>/data/records.jsonl      现场记录（逐条留档）
<时间戳>/data/counter.json       编号计数器
<时间戳>/data.tar.gz             整个 data/ 的打包（photos/ thumbs/ captures/ masks/ qr/）
<时间戳>/app-code.tar.gz         同步前的代码树（不含 data/）
<时间戳>/SHA256SUMS              备份文件校验
```

```bash
SNAP=$(cat /opt/devcon/backup/LAST)          # 最近一次快照

# 只回滚代码（最常见：新版本起不来）
systemctl stop devcon-booth
tar xzf "$SNAP/app-code.tar.gz" -C /opt/devcon/app
systemctl start devcon-booth

# 回滚数据（覆盖式还原）
tar xzf "$SNAP/data.tar.gz" -C /opt/devcon/app

# 只把编号/记录放回去
cp -a "$SNAP/data/records.jsonl" "$SNAP/data/counter.json" /opt/devcon/app/data/

# 彻底停掉现场服务
systemctl disable --now devcon-booth
```

数据保护是 `deploy.sh` 的硬要求：**同步前**先备份 `data/records.jsonl` 与 `data/counter.json`，
且 rsync 永远带 `--exclude=data`、`--exclude=assets/models` —— 一次误操作不会抹掉现场数据。
（快照会一直累积；硬盘 ~99 GB 可用，一场活动到不了需要清理的量。）

---

## 6. 模型来源（官方 model-zoo-vision）

```
https://archive.spacemit.com/spacemit-ai/model_zoo/vision/<name>/<file>
```

| 文件 | 放到 | 备注 |
|---|---|---|
| `yoloe-v8s-seg.dynq.onnx`（13.9 MB） | `/opt/devcon/assets/models/` | INT8 动态量化 + 开放词汇 + 实例分割，走 AI 核 |
| `mobileclip.q.onnx`（140.0 MB） | `/opt/devcon/assets/models/` | 与上面这个**必须配对**（换编码器会导致检测数恒为 0） |
| `yolov8n.q.onnx`（3.5 MB） | `/usr/share/yolo-demo/yolov8/` | 检测；`yolo-demo` 包自带，别覆盖成别的模型 |

`preflight.py` 会按 `BOOTH_*_MODEL` 的**实际取值**逐个核对路径与大小，所以在单元里改了路径也能查到。

---

## 7. 排障速查

CP1500 USB 卡片打印于 2026-10-12 已接入，使用 54×86mm 耗材。
配置模板为 `cp1500-usb.conf`，安装到 `/etc/systemd/system/devcon-booth.service.d/print.conf`；
队列必须先用 `-m everywhere` 创建为 `DevCon_CP1500`，URI 是 `ipp://127.0.0.1:60000/ipp/print`。
使用 `54x86mm.Borderless` / `fit`，完整画面带安全白边。模板同时开启设备/纸型检查与
`/opt/devcon/app/data/print-jobs.json` 持久化幂等。升级代码不得覆盖这个台账。
用户取图页只能打印带标识版，每张照片一次；服务端会把公共请求固定归一为
`user:<token>`，不能通过更换前端幂等键重复打印。操作员面板使用
`POST /api/operator/print`，可选择 `watermarked` / `clean` 版本并使用新的任务键重复打印。
照片同时提供 `/api/photo/<token>.jpg`（带标识）与
`/api/photo/<token>.clean.jpg`（无标识）两个下载入口；状态不明时先查询同一作业，不要换键再次提交。
详细验收记录见项目 `docs/CP1500-54x86实测记录-20261012.md`。

```bash
systemctl status devcon-booth --no-pager      # 服务活着吗
journalctl -u devcon-booth -f                 # 实时日志
ss -ltnp | grep -E ':8090|:8080'              # 谁在听端口
ps -ef | grep -E 'booth_server|model_test_worker'   # 进程
curl -s http://127.0.0.1:8090/api/state       # 相机/AI 核/磁盘的实时读数
curl -s http://127.0.0.1:8090/api/state | python3 -m json.tool | grep -A9 '"preview"'   # 预览流/相机自愈读数
curl -s -X POST http://127.0.0.1:8090/api/operator/restore   # 一键恢复到就绪
systemctl list-timers devcon-booth-watchdog.timer --no-pager # 看门狗还活着吗
journalctl -u devcon-booth-watchdog -n 20 --no-pager
```

| 现象 | 先看什么 | 处理 |
|---|---|---|
| 预检报 **8090 被别的进程占用** | `ps -p <pid> -o cmd=` | 停掉那个进程（例如手动起的 `camd.py`），再 `systemctl restart devcon-booth`。**不要动 8080 的 yolos-box** |
| 预检报 **AI 核退回 CPU（WARN）** | `ss -ltnp \| grep 8080`、`ps -ef \| grep model_test_worker` | 8080 上 yolos-box 的常驻 worker 会先占核。切到 CPU 也能跑（分割 ~1.16 s），大屏徽章会如实显示 `CPU`。想拿回核：停掉 yolos-box 的 worker 后 `systemctl restart devcon-booth` |
| 相机打不开 | `fuser -v /dev/video1`、`v4l2-ctl --list-devices` | 有别的进程占着（ffmpeg/camd）→ 停掉它；`camera.py` 会自己重连（2s 退避），也可点操作员面板的「一键恢复」 |
| **预览老是断流/黑屏** | `curl -s http://127.0.0.1:8090/api/state \| python3 -m json.tool \| grep -A12 preview` | 先看 `preview.webrtc.online`、`preview.webrtc.sent` 和 `camera.reopens`。若 `webrtc.online=false`，看 `journalctl -u devcon-mediamtx -n 40 --no-pager` 与 `journalctl -u devcon-booth -n 40 --no-pager`；若相机读数停住，再看 `dmesg -T \| grep -E "uvc\|usb 7"`。 |
| 画面有横向滚动条（频闪） | `v4l2-ctl -d /dev/v4l/by-id/usb-*DICOTA*-video-index0 -C power_line_frequency` | 应为 50Hz（`camera.py` 开流前已用 `v4l2-ctl` 设好并读回验证）。若被相机复位成 60Hz，重启服务即可 |
| 大屏白屏 | `curl -s http://127.0.0.1:8090/stage \| head` | 服务没起来 → 看 journalctl；服务正常 → 看 chromium 是否带对了 `WAYLAND_DISPLAY` |
| 手机扫码打不开取图页 | `/api/state` 里的 `link_base` | 若是 `127.0.0.1` → 网线/无线没通；或显式 `Environment=BOOTH_PUBLIC_HOST=192.0.2.10` |

**开机自检**：`systemctl enable` 之后，重启设备，等 30 s，跑一次
`/usr/bin/python3 /opt/devcon/app/tools/preflight.py` —— 应该 0 退出码、无 FAIL。
