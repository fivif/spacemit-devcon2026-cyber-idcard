# 佳能 SELPHY CP1500 接入 K3 与打印 SOP

更新日期：2026-10-12

## 2026-10-12 已接入：USB · 54×86mm

- 用户已经接线，明确使用 54×86mm 卡片纸并授权实测。以下原始接入预案保留作历史参考，现场配置以本节为准。
- K3 已有 CUPS 2.4.16、ipp-usb 0.9.31；设备 04a9:3302，media-ready=om_card_54x86mm。
- USB 专用队列 DevCon_CP1500，直接连接 ipp://127.0.0.1:60000/ipp/print；默认 54x86mm.Borderless、print-scaling=fill。
- 现在采用 USB 主方案，局域网 IPP 作为备用。已有两张真实作业 completed，每张 42 秒；第二张经项目取图 API 发出。
- 取图页“打印 54×86 卡片”已启用，真实状态回传、持久化幂等、设备在线和纸型检查已接入。
- 故障策略为 stop-printer，避免不确定失败后自动重新出片；补纸/排障后由工作人员检查作业再恢复队列。
- 相机与打印机同 USB Hub；本轮预览仍为 12fps、0 USB 复位，尚未做十张长时间连打。
- 打印画布 638×1016、300dpi，居中等比裁切并铺满，不加设计白边、不拉伸人像。满版修复后的真实作业 DevCon_CP1500-14 已 completed，纸面颜色/剩余机械边缘仍需人工查看样片。
- 完整证据见 [CP1500-54x86实测记录-20261012.md](CP1500-54x86实测记录-20261012.md)，可复用配置为 app/deploy/cp1500-usb.conf。

## 当前状态

以下“当前状态”与预案为 2026-10-11 未接线时的历史记录。

- 设备：Canon SELPHY CP1500，已到场，尚未连接 K3。
- 本轮：只完成资料核对、方案记录和软件边界检查，**没有连接、没有安装驱动、没有打印**。
- 项目当前打印开关保持关闭：`BOOTH_PRINT_ENABLED` 未开启，systemd 中的 `BOOTH_PRINTER_ENABLED=false` 保持不变。
- 项目已有 CUPS 打印适配器和取图页打印按钮，但 CP1500 的真实队列、纸张裁切、连续出片和打印完成状态仍未验收。
- 电子照片仍是主交付；打印机异常不能阻塞拍摄、AIGC 或扫码取图。

## 结论

### 首选：同一局域网内的 IPP

CP1500 是 AirPrint / IPP Everywhere 设备。优先让打印机加入与 K3 相同的局域网，并在路由器中为打印机保留固定 IP。K3 使用 CUPS 的 `everywhere` 驱动，不安装 CP1300 老驱动，不把打印机当普通裸 USB 打印机。

推荐队列形态：

```text
Canon_CP1500 -> ipp://<打印机固定IP>/ipp/print -> CUPS -> CP1500
```

这样不占用 K3 的 USB 总线，也避开 CP1500 USB 端点和旧版 `ipp-usb` 的兼容问题。现场如果 Wi-Fi 不稳定，再切 USB 方案。

### 备用：USB + ipp-usb

CP1500 的 USB ID 是 `04a9:3302`。USB 方案必须走 `ipp-usb`，不要创建 `usb://` 裸队列。Debian/Bianbu 系统自带的旧版 `ipp-usb 0.9.23` 有 CP1500 双接口问题，目标版本至少为 `0.9.24`，现场优先使用已验证的较新版本。

备用队列形态：

```text
Canon_CP1500_USB -> ipp://127.0.0.1:60000/ipp/print -> ipp-usb -> USB -> CP1500
```

USB 方案只有在安装并确认 `ipp-usb` 版本后才能进入验收，不能看到 USB 设备就直接开启应用打印。

## 纸张和版面

- CP1500 目标纸型：明信片 / 4x6 英寸同级纸，具体以打印机自报的 `media` 为准。
- 项目打印版面按 100x148mm、300dpi 记录为竖版 `1181x1748`、横版 `1748x1181`。
- CP1500 协议资料还出现 `1800x1200` 的满幅像素规格，最终以 CUPS 队列自报的纸型和第一张样片裁切结果为准，不能只凭文件名判断。
- 重要内容距离边缘至少 3mm，按 300dpi 约 36px；人脸、Logo、二维码不能落入安全区。
- 横版不能把屏幕的 1600x900 直接缩放后打印，必须使用打印版面；竖版也不能依赖打印机自动裁切。
- 第一张必须先做样片，检查比例、白边、裁切位置、颜色和覆膜/亚克力反光，再决定是否批量。

## 连接后执行顺序

以下命令是预案，**本轮没有执行**。连接 CP1500 后严格按顺序操作。

### A. Wi-Fi / IPP 路线

1. 在 CP1500 上完成入网，在路由器中查看并固定打印机 IP。
2. K3 上确认能访问打印机 IP 的 IPP 端口。
3. 安装 CUPS 客户端和无驱动发现工具；不要先装 Gutenprint 或 CP1300 驱动。
4. 创建队列：

```bash
sudo lpadmin -p Canon_CP1500 -E \
  -v ipp://<打印机固定IP>/ipp/print \
  -m everywhere \
  -D "Canon SELPHY CP1500"
sudo lpadmin -p Canon_CP1500 -o printer-error-policy=retry-current-job
sudo lpoptions -p Canon_CP1500 -o print-color-mode=color
lpstat -p Canon_CP1500 -l
```

5. 只做属性检查时使用：

```bash
ipptool -tv ipp://<打印机固定IP>/ipp/print \
  get-printer-attributes.test
```

属性查询需要分开进行；CP1500 对把 `all` 和 `media-col-database` 放在同一次请求的行为不稳定。

### B. USB / ipp-usb 路线

1. 连接 USB 后只确认设备，不直接打印：

```bash
lsusb | grep -i canon
systemctl status ipp-usb --no-pager
ipp-usb --version
```

2. 确认版本达到目标后，再检查本地 IPP：

```bash
ipptool -tv ipp://127.0.0.1:60000/ipp/print \
  get-printer-attributes.test
```

3. 确认自报型号包含 `Canon SELPHY CP1500` 后创建队列：

```bash
sudo lpadmin -p Canon_CP1500_USB -E \
  -v ipp://127.0.0.1:60000/ipp/print \
  -m everywhere \
  -D "Canon SELPHY CP1500 (USB)"
sudo lpadmin -p Canon_CP1500_USB -o printer-error-policy=retry-current-job
sudo lpoptions -p Canon_CP1500_USB -o print-color-mode=color
```

## K3 应用接线

真实队列验收通过后，才在 K3 的 `/etc/devcon/booth.env` 中加入对应配置。Wi-Fi 方案示例：

```ini
BOOTH_PRINT_ENABLED=true
BOOTH_PRINT_BACKEND=cups
BOOTH_PRINT_QUEUE=Canon_CP1500
BOOTH_PRINT_MEDIA=Postcard.Borderless
BOOTH_PRINT_TIMEOUT=30
```

USB 方案只替换队列名为 `Canon_CP1500_USB`。配置写入后执行：

```bash
sudo systemctl restart devcon-booth
curl -s http://127.0.0.1:8090/api/state
```

应用侧现有保护：

- CUPS 队列不存在或不可用时，取图页显示打印暂不可用，不伪报已打印。
- 同一照片使用固定幂等键，重复点击不会重复提交。
- 照片不存在时不会调用 `lp`。
- 打印失败不影响电子照片保存和扫码取图。

尚未完成的应用工作：

- 将统一版式渲染器的打印输出接入真实打印调用；
- 用 CP1500 实测 `Postcard.Borderless` 的裁切，必要时升级 CUPS/cups-filters 或调整版面；
- 实现并验证 CUPS 作业从“已提交”到“已完成/失败”的真实状态回传；
- 根据每张约 55-60 秒的出片节拍决定“限量打印”还是“人人打印”。

## 验收清单

第一轮只打 1 张，第二轮再做连续测试。每一步都记录结果，不用“队列提交成功”代替“照片已出纸”。

| 项目 | 验收标准 | 状态 |
|---|---|---|
| 设备识别 | Wi-Fi IPP 或 USB `04a9:3302` 可稳定识别 | 待连接 |
| 队列协议 | `-m everywhere` 队列可查询，自报型号正确 | 待连接 |
| 纸型 | 自报纸型与实际相纸一致 | 待连接 |
| 第一张样片 | 画面完整、比例正确、二维码可扫、无关键内容裁切 | 待连接 |
| 连续出片 | 至少 10 张，横竖各半，记录失败和间隔 | 待连接 |
| 缺纸/卡纸 | 队列不拖死，电子版仍可取 | 待连接 |
| 重复点击 | 同一照片只生成一个打印作业 | 软件已测，现场待验 |
| 产能 | 记录单张打印、取纸、装板耗时，再决定限量策略 | 待连接 |

## 排障优先级

| 现象 | 处理 |
|---|---|
| 任务完成但不出纸 | 检查是否误建成 `usb://` 队列；CP1500 应走 IPP 或 `ipp-usb` |
| USB 写入超时 | 检查 `ipp-usb` 版本，旧版先停用，不继续重复发任务 |
| 满幅裁掉一块 | 先检查 `media=Postcard.Borderless` 与 `print-scaling=fill`，再检查 CUPS/cups-filters 版本 |
| 第二张卡住 | 不使用 CP1300 老驱动和 stock `usb` 后端；查看 CUPS 与 `ipp-usb` 日志 |
| 打印机缺纸后队列停用 | 保持 `retry-current-job`，补纸后检查 `lpstat` 再继续 |
| 打印慢 | CP1500 热升华一张约 55-60 秒属于设备节拍，不能靠重复提交提速 |

## 资料来源

本方案结合项目资料 `佳能CP1500-Linux打印知识.md` 与以下公开资料整理。公开资料用于形成方案，不能替代 K3 和现场打印机实测：

- [OpenPrinting/ipp-usb](https://github.com/OpenPrinting/ipp-usb)
- [CP1500 USB + ipp-usb 流程](https://github.com/rachelramsay/canon-selphy-cp1500-to-pi-print)
- [CP1500 PPD 与 ICC 资料](https://github.com/richi9514/Canon-SELPHY-CP1500)
