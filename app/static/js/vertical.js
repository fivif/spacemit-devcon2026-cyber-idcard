/* vertical.js —— 竖屏主视图（/vertical，也是 / 的默认页）
 *
 * 2026-10-09 定稿的行为：
 *   - 无人排队/拍摄：待机轮播（风格样张），**不拉相机流**——后端有待机门
 *     （preview_hub.set_gate），前端此刻也不给 <img> 设 src，双保险；
   *   - 队列出现需要拍摄的条目（queued/called/ready）或 active_job 存在：开门、
 *     亮取景、进入「K3 流水线」实时展示——
 *       capture（抓帧）→ infer（人像分割·掩码揭示）→ matte（MODNet 精抠）
 *       → stylizing（AIGC 生成）→ rendering（合成）→ storing（入库编号）
 *     每一步用 active_job.steps 的真实数据（key/label/ms/detail）点亮；
 *   - 抠像揭示：推理完成拿到 mask_url 后做一次 maskReveal（绿雾扫过人像）；
 *   - 倒计时 3·2·1 / 闪光 / 成片展示沿用既有视觉。
 * 页面不提供操作按钮 —— 拍摄动作发生在手机（/join）或拍摄位页。
 */
(function () {
  "use strict";
  const { $, api, fmtMs, reducedMotion, watchWebRTC, carouselItems } = window.Booth;

  const CAROUSEL_INTERVAL_MS = 5000;
  const PRESENCE_STALE_S = 10;
  const GESTURE_OBS_STALE_S = 1.2;
  const RESULT_SHOW_MS = 8000;
  // 相机流守护只在流水线活动期间运行；空闲时轮询里主动断流
  // （这些 key 与后端 active_job.steps 里的 key 一致，动效阶段推进也按它算）
  const PIPELINE_KEYS = ["capture", "infer", "render", "store"];

  const el = {
    view: $("#view"), mask: $("#mask"), boxes: $("#boxes"),
    badgeEp: $("#badge-ep"), badgeNow: $("#badge-now"),
    mCount: $("#m-count"),
    vtip: $("#vtip"),
    preview: $("#preview"), stream: $("#preview-stream"), camState: $("#camstate"),
    idleShow: $("#idle-show"), slide: $("#idle-slide"),
    slideLabel: $("#idle-slide-label"), slideDots: $("#idle-slide-dots"),
    wakeHint: $("#wake-hint"),
    matting: $("#matting"), matteCaption: $("#matte-caption"),
    styleStage: $("#style-stage"), styleStageCopy: $("#style-stage-copy"), stylePreview: $("#style-preview"),
    countdown: $("#countdown"), flash: $("#flash"),
    vresult: $("#vresult"), vshot: $("#vresult-shot"),
    vserial: $("#vresult-serial"), footNote: $("#foot-note"),
    vprompt: $("#vprompt"), vpromptKicker: $("#vprompt-kicker"),
    vpromptTitle: $("#vprompt-title"), vpromptSub: $("#vprompt-sub"),
  };

  const core = window.BoothCreateCore ? window.BoothCreateCore() : { update() {}, clear() {} };

  let lastShownRecord = null;
  let shownResultFor = null;
  let resultTimer = null;
  let lastMatteOn = false;
  let lastStyleOn = false;
  let lastMaskFor = null;         // 已揭示过掩码的 record_id
  let streamWatcher = null;       // WebRTC 句柄（流水线期间才跑）
  let streamOn = false;
  let frozenPreviewUrl = null;
  let frozenJobId = null;
  let cutoutLoader = null;
  let resultVisible = false;
  let pendingResult = null;
  let tickRunning = false;
  let lastGestureObs = null;      // 最新手势观测框（用于 canvas 实时绘框）
  let handoffNoticeJob = null;
  let handoffNoticeUntil = 0;
  let currentPresentationMode = "full";
  // 流水线阶段渲染状态：key -> "done"|"active"|"wait"
  let pipeState = {};

  /* ---- 待机轮播 -------------------------------------------------------- */
  const carousel = { items: [], index: 0, timer: null };

  function showCarouselSlide() {
    if (!carousel.items.length) return;
    const item = carousel.items[carousel.index % carousel.items.length];
    el.slide.parentElement.classList.remove("is-empty");
    el.slide.src = item.src;
    el.slide.alt = "风格示意：" + item.label;
    if (el.slideLabel) el.slideLabel.textContent = item.label;
    if (el.slideDots) {
      Array.from(el.slideDots.children).forEach((dot, index) => {
        dot.classList.toggle("is-active", index === carousel.index % carousel.items.length);
      });
    }
    carousel.index = (carousel.index % carousel.items.length + 1) % carousel.items.length;
  }

  function buildCarousel() {
    if (!el.idleShow || !el.slide) return;
    carouselItems().then((items) => {
      carousel.items = items;
      if (!items.length) {
        el.slide.parentElement.classList.add("is-empty");
        el.slide.removeAttribute("src");
        if (el.slideDots) el.slideDots.innerHTML = "";
        return;
      }
      if (el.slideDots) {
        el.slideDots.innerHTML = "";
        items.forEach((item, index) => {
          const dot = document.createElement("span");
          dot.className = "idle-dot";
          dot.setAttribute("aria-label", item.label);
          if (index === 0) dot.classList.add("is-active");
          el.slideDots.appendChild(dot);
        });
      }
      showCarouselSlide();
      if (!reducedMotion()) {
        carousel.timer = setInterval(showCarouselSlide, CAROUSEL_INTERVAL_MS);
      }
    });
  }

  /* ---- 相机流：只在流水线活动期间挂流 ----------------------------------
     空闲时关闭 WebRTC —— 后端待机门关着，流上本来就没有新帧。 */
  function setStream(on) {
    if (frozenPreviewUrl) return;
    if (on === streamOn) return;
    streamOn = on;
    if (on) {
      el.stream.classList.add("is-visible");
      el.preview.classList.remove("is-visible");
      el.stream.setAttribute("aria-label", "实时取景");
      streamWatcher = watchWebRTC(el.stream, {
        onState: (next, reason) => {
          if (next === "live") setCamState("live");
          else setCamState(next, reason);
        },
      });
    } else {
      if (streamWatcher) { streamWatcher.stop(); streamWatcher = null; }
      el.stream.classList.remove("is-visible");
      if (!frozenPreviewUrl) el.preview.classList.remove("is-visible");
      el.view.classList.add("is-idle");
      el.view.classList.remove("is-offline", "is-mock");
      if (el.camState) el.camState.textContent = "";
    }
  }

  function freezeCapture(active) {
    if (!active.capture_preview_url || active.capture_preview_url === frozenPreviewUrl) return;
    if (streamWatcher) { streamWatcher.stop(); streamWatcher = null; }
    el.stream.classList.remove("is-visible");
    streamOn = false;
    frozenPreviewUrl = active.capture_preview_url;
    frozenJobId = active.job;
    if (el.flash) {
      el.flash.classList.remove("is-fire");
      void el.flash.offsetWidth;
      el.flash.classList.add("is-fire");
      window.setTimeout(() => el.flash.classList.remove("is-fire"), 520);
    }
    el.preview.src = frozenPreviewUrl;
    el.preview.classList.add("is-visible");
    el.preview.alt = "本次抓拍照片";
    el.view.classList.remove("is-idle", "is-offline", "is-mock");
    if (el.camState) el.camState.textContent = "";
    lastGestureObs = null;
    drawGestureBoxes(null);
  }

  function releaseCapture() {
    cutoutLoader = null;
    frozenPreviewUrl = null;
    frozenJobId = null;
    el.preview.removeAttribute("src");
    el.preview.classList.remove("is-visible");
    el.view.classList.remove("has-cutout");
  }

  function replaceWithCutout(overlay) {
    if (!frozenPreviewUrl || overlay.job !== frozenJobId || !overlay.cutout_url) return;
    const url = overlay.cutout_url;
    if (cutoutLoader && cutoutLoader.dataset.src === url) return;
    const jobId = frozenJobId;
    const loader = new Image();
    cutoutLoader = loader;
    loader.dataset.src = url;
    loader.onload = () => {
      if (cutoutLoader !== loader || frozenJobId !== jobId) return;
      el.preview.src = url;
      el.preview.classList.add("is-visible");
      el.preview.alt = "本次拍摄的透明背景人像";
      el.view.classList.add("has-cutout");
      if (el.mask) el.mask.classList.remove("is-reveal");
    };
    loader.onerror = () => {
      if (cutoutLoader === loader) cutoutLoader = null;
    };
    loader.src = url;
  }

  /* ---- 画面状态 -------------------------------------------------------- */
  function setCamState(state, reason) {
    if (frozenPreviewUrl) return;
    const stalled = state !== "live";
    el.view.classList.toggle("is-offline", stalled);
    el.view.classList.toggle("is-mock", state === "mock");
    if (!el.camState) return;
    el.camState.textContent = state === "mock"
      ? "当前为开发预览，未连接真实相机"
      : stalled
      ? (reason === "no-frame" ? "相机重连中…（收不到画面）" : "相机重连中…（画面卡住）")
      : "";
  }

  /* ---- 实时检测框（yolo 手势引擎）---------------------------------------
     从 state.gesture_obs 读 persons 和 ok_boxes，绘到 .boxes canvas 上。
     坐标系：后端框是原始图像坐标，canvas 覆盖整个 .view，用 img.preview 的
     实际显示区域（object-fit:cover 后的 naturalWidth/Height 与 clientRect）
     做比例换算。检测结果短时未刷新即视为过期，清空画布。 */
  function drawGestureBoxes(gestureObs) {
    const canvas = el.boxes;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    // 同步 canvas 物理尺寸到显示尺寸（devicePixelRatio 感知）
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(rect.width * dpr) ||
        canvas.height !== Math.round(rect.height * dpr)) {
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      ctx.scale(dpr, dpr);
    }
    ctx.clearRect(0, 0, rect.width, rect.height);

    if (!gestureObs) return;
    const age = (Date.now() / 1000) - (gestureObs.at || 0);
    if (age > GESTURE_OBS_STALE_S) return;

    const imgW = gestureObs.img_w || 0;
    const imgH = gestureObs.img_h || 0;
    if (!imgW || !imgH) return;

    // object-fit:cover 下图像在视图里的实际绘制区域
    const viewW = rect.width, viewH = rect.height;
    const scaleW = viewW / imgW, scaleH = viewH / imgH;
    const scale = Math.max(scaleW, scaleH);  // cover 取大
    const drawW = imgW * scale, drawH = imgH * scale;
    const offX = (viewW - drawW) / 2, offY = (viewH - drawH) / 2;

    function imgToView(x, y) {
      return [viewW - (x * scale + offX), y * scale + offY];
    }
    function drawBox(box, color, label) {
      const p1 = imgToView(box[0], box[1]);
      const p2 = imgToView(box[2], box[3]);
      const x1 = Math.min(p1[0], p2[0]), y1 = Math.min(p1[1], p2[1]);
      const w = Math.abs(p2[0] - p1[0]), h = Math.abs(p2[1] - p1[1]);
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.shadowColor = color;
      ctx.shadowBlur = 8;
      ctx.strokeRect(x1, y1, w, h);
      if (label) {
        ctx.font = "bold 13px system-ui, sans-serif";
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = color + "CC";
        ctx.fillRect(x1, y1 - 20, tw + 10, 20);
        ctx.fillStyle = "#000";
        ctx.shadowBlur = 0;
        ctx.fillText(label, x1 + 5, y1 - 5);
      }
      ctx.restore();
    }

    const isOk = Boolean(gestureObs.is_ok);
    (gestureObs.persons || []).forEach(function (box) {
      drawBox(box, isOk ? "#9FEF00" : "#4ea8de", "人形");
    });
    (gestureObs.ok_boxes || []).forEach(function (box) {
      drawBox(box, "#9FEF00", "OK ✓");
    });
    (gestureObs.palm_boxes || []).forEach(function (box) {
      drawBox(box, "#38D9FF", "手掌");
    });
    (gestureObs.hand_boxes || []).forEach(function (box) {
      drawBox(box, isOk ? "#9FEF00" : "#FFD43B", isOk ? "OK ✓" : "手部");
    });
    const points = gestureObs.hand_points || [];
    const links = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[0,9],[9,10],[10,11],[11,12],[0,13],[13,14],[14,15],[15,16],[0,17],[17,18],[18,19],[19,20],[5,9],[9,13],[13,17]];
    ctx.save();
    ctx.strokeStyle = isOk ? "#9FEF00" : "#FFD43B";
    ctx.fillStyle = isOk ? "#9FEF00" : "#FFFFFF";
    ctx.lineWidth = 2;
    links.forEach(function (link) {
      if (!points[link[0]] || !points[link[1]]) return;
      const a = imgToView(points[link[0]][0], points[link[0]][1]);
      const b = imgToView(points[link[1]][0], points[link[1]][1]);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    });
    points.forEach(function (point) {
      const p = imgToView(point[0], point[1]);
      ctx.beginPath(); ctx.arc(p[0], p[1], 3.5, 0, Math.PI * 2); ctx.fill();
    });
    ctx.restore();
  }

  function renderCore(active) {
    if (!active || active.stage === "countdown") {
      core.clear();
      el.view.classList.remove("is-core");
      return;
    }
    // 风格名喂给动效的顶部标签：id→中文名 的映射表在本模块（styleLabels）
    core.update(Object.assign({}, active, {
      style_label: styleLabel(active.style) || "",
      style_preview_url: currentPresentationMode === "handoff" ? null : active.style_preview_url,
    }));
    el.view.classList.toggle("is-core", Boolean(core.active));
  }

  /* ---- 抠像隔离层 ------------------------------------------------------ */
  function setMattePhase(on, source, matteMs) {
    if (!el.matting) return;
    el.matting.classList.toggle("is-on", Boolean(on));
    el.matting.setAttribute("aria-hidden", String(!on));
    if (!on) return;
    if (!el.matteCaption) return;
    const srcText = source === "modnet-roi" ? "按人框精细抠像"
      : source === "modnet" ? "整幅精细抠像"
      : source === "detect-mask" ? "检测掩码兜底"
      : "逐像素分离人像";
    el.matteCaption.textContent = matteMs ? srcText + " · " + fmtMs(matteMs) : srcText;
  }

  /* ---- 大字提示：每个阶段只说一件事，站在一两米外也看得清 ------------- */
  function setPrompt(kicker, title, sub) {
    if (!el.vprompt) return;
    const on = Boolean(title);
    el.vprompt.classList.toggle("is-on", on);
    if (!on) return;
    if (el.vpromptKicker) el.vpromptKicker.textContent = kicker || "";
    if (el.vpromptTitle) el.vpromptTitle.textContent = title;
    if (el.vpromptSub) el.vpromptSub.textContent = sub || "";
  }

  /* 作业里的 style 是风格 id（如 pixel-person），屏幕上要显示中文名 */
  let styleLabels = {};
  function styleLabel(id) {
    return (id && styleLabels[id]) || "";
  }

  function setStylePhase(on, style, previewUrl) {
    if (!el.styleStage) return;
    el.styleStage.classList.toggle("is-on", Boolean(on));
    el.styleStage.setAttribute("aria-hidden", String(!on));
    if (on && previewUrl && el.stylePreview && el.stylePreview.dataset.src !== previewUrl) {
      el.stylePreview.dataset.src = previewUrl;
      el.stylePreview.src = previewUrl + "?v=" + Date.now();
      el.styleStage.classList.add("has-preview");
    }
    if (!on) el.styleStage.classList.remove("has-preview");
    if (on && el.styleStageCopy) {
      el.styleStageCopy.textContent = (style
        ? "正在把人像画成「" + style + "」"
        : "正在把人像画成所选风格") + " · 约半分钟到一分钟";
    }
  }

  /* ---- 掩码揭示：拿到 mask_url 做一次绿雾扫过 ---------------------------
     maskReveal 动效在 stage.css（clip-path 从右往左揭示人像），竖屏复用。 */
  function maybeRevealMask(overlay) {
    if (el.view.classList.contains("has-cutout")) return;
    if (!el.mask || !overlay || !overlay.mask_url) return;
    if (overlay.record_id === lastMaskFor) return;
    lastMaskFor = overlay.record_id;
    el.mask.src = overlay.mask_url + "?v=" + Date.now();
    el.mask.classList.remove("is-reveal");
    void el.mask.offsetWidth;      // 重启动画
    el.mask.classList.add("is-reveal");
    window.setTimeout(() => el.mask.classList.remove("is-reveal"), 2400);
  }

  function applyOverlayAssets(overlay) {
    if (!overlay) return;
    replaceWithCutout(overlay);
    if (currentPresentationMode !== "handoff" && overlay.style_preview_url
        && el.stylePreview && el.stylePreview.dataset.src !== overlay.style_preview_url) {
      el.stylePreview.dataset.src = overlay.style_preview_url;
      el.stylePreview.src = overlay.style_preview_url + "?v=" + Date.now();
      if (el.styleStage) el.styleStage.classList.add("has-preview");
    }
  }

  let lastCountdownValue = null;
  let countdownJobId = null;
  function maybeCountdown(active) {
    if (!el.countdown) return;
    const raw = active && active.stage === "countdown"
      ? Number(active.countdown_remaining)
      : NaN;
    if (Number.isFinite(raw) && raw > 0) {
      const value = Math.ceil(raw);
      if (value !== lastCountdownValue || active.job !== countdownJobId) {
        lastCountdownValue = value;
        countdownJobId = active.job;
        el.countdown.textContent = String(value);
      }
      el.countdown.classList.add("is-show");
    } else {
      lastCountdownValue = null;
      countdownJobId = null;
      el.countdown.classList.remove("is-show");
    }
  }

  /* ---- 成片展示 -------------------------------------------------------- */
  function showResult(job) {
    if (currentPresentationMode === "handoff") {
      shownResultFor = job.record_id;
      pendingResult = null;
      resultVisible = false;
      if (el.vresult) el.vresult.classList.remove("is-on");
      releaseCapture();
      return;
    }
    if (!el.vresult || !job.token || shownResultFor === job.record_id
        || pendingResult === job.record_id) return;
    const recordId = job.record_id;
    pendingResult = recordId;
    el.vshot.onload = () => {
      if (pendingResult !== recordId || currentPresentationMode === "handoff") return;
      pendingResult = null;
      shownResultFor = recordId;
      el.vserial.textContent = String(recordId || "").match(/(\d+)$/)?.[1] || "—";
      $("#vresult-portrait-mode").textContent = job.portrait_mode === "chibi" ? "Q版大头像" : "写实版";
      el.vresult.classList.add("is-on");
      resultVisible = true;
      clearTimeout(resultTimer);
      resultTimer = window.setTimeout(() => {
        el.vresult.classList.remove("is-on");
        resultVisible = false;
        if (frozenPreviewUrl) releaseCapture();
      }, RESULT_SHOW_MS);
    };
    el.vshot.onerror = () => {
      if (pendingResult === recordId) pendingResult = null;
    };
    el.vshot.src = "/api/photo/" + job.token + ".jpg?v=" + Date.now();
  }

  /* ---- 主轮询 ---------------------------------------------------------- */
  async function tickState() {
    let state;
    try {
      state = await api.get("/api/state");
    } catch (err) {
      el.badgeEp.textContent = "服务未响应";
      el.badgeEp.className = "badge badge-degraded";
      return;
    }
    const ep = state.infer && state.infer.ep;
    el.badgeEp.textContent = state.camera && state.camera.ok ? "拍摄就绪" : "相机连接中";
    el.badgeEp.className = "badge " + (state.camera && state.camera.ok ? "badge-ep" : "badge-cpu");
    if (Array.isArray(state.styles)) {
      styleLabels = {};
      state.styles.forEach((item) => { if (item && item.name) styleLabels[item.name] = item.label || ""; });
    }

    const active = state.active_job;
    maybeCountdown(active);
    const queueCaptureActive = (state.queue_capture_active ?? state.queue_active ?? 0) > 0;
    const queueCameraActive = (state.queue_camera_active ?? 0) > 0;
    const presentationMode = state.presentation_mode || "full";
    currentPresentationMode = presentationMode;
    const handoffNotice = state.handoff_notice || null;
    const gestureObs = state.gesture_obs || null;
    const gestureFresh = gestureObs && Date.now() / 1000 - (gestureObs.at || 0) <= GESTURE_OBS_STALE_S;
    const visualActive = Boolean(gestureFresh && (gestureObs.hand_points || []).length);
    const activeCameraStage = Boolean(active && ["countdown", "capturing"].includes(active.stage));
    const cameraOn = queueCameraActive || activeCameraStage;

    if (presentationMode === "handoff") {
      pendingResult = null;
      clearTimeout(resultTimer);
      resultVisible = false;
      el.vresult.classList.remove("is-on");
      if (state.last_record) lastShownRecord = state.last_record;
      if (!active && frozenJobId) releaseCapture();
    }

    if (active && frozenJobId && active.job !== frozenJobId) {
      pendingResult = null;
      releaseCapture();
      clearTimeout(resultTimer);
      resultVisible = false;
      el.vresult.classList.remove("is-on");
    }
    if (active) freezeCapture(active);
    if (!active && presentationMode === "handoff" && handoffNotice &&
        handoffNotice.job && handoffNotice.job !== handoffNoticeJob) {
      handoffNoticeJob = handoffNotice.job;
      handoffNoticeUntil = Date.now() + 4000;
      releaseCapture();
    }
    if (!active && frozenJobId && !resultVisible && presentationMode !== "handoff") {
      try {
        const completed = await api.get("/api/job/" + frozenJobId);
        if (completed.stage === "done" && completed.token) {
          lastShownRecord = completed.record_id;
          showResult(completed);
        } else if (completed.stage === "failed") {
          releaseCapture();
        }
      } catch (err) {
        if (err.status === 404) releaseCapture();
      }
    }

    // 摄像头随流水线开关（后端门 + 前端断流，双保险）
    if (!resultVisible) setStream(cameraOn);

    if (!active && presentationMode === "handoff" && Date.now() < handoffNoticeUntil) {
      el.view.classList.add("is-handoff-notice");
      el.view.classList.remove("is-scanning");
      el.view.classList.remove("is-idle", "is-core", "has-cutout");
      el.idleShow.classList.add("hide");
      core.clear();
      setMattePhase(false);
      setStylePhase(false);
      lastMatteOn = false;
      lastStyleOn = false;
      el.badgeNow.textContent = "AIGC 已转后台生成";
      setPrompt("拍摄已完成", "照片正在后台生成", "可先去参观 · 请关注手机进度，完成后回来打印");
      drawGestureBoxes(null);
      return;
    }
    el.view.classList.remove("is-handoff-notice");

    // 实时手势识别框（门阶段和有人排队时绘框）
    if (!frozenPreviewUrl && !resultVisible && cameraOn) {
      lastGestureObs = gestureObs;
      drawGestureBoxes(lastGestureObs);
    } else {
      lastGestureObs = null;
      drawGestureBoxes(null);
    }

    if (active) {
      el.view.classList.add("is-scanning");
      el.view.classList.remove("is-idle");
      el.idleShow.classList.add("hide");
      const matteComplete = active.matte_applied
        || ["stylizing", "rendering", "storing"].includes(active.stage);
      // 只有服务端确认 AIGC 已满 8 秒，才允许提示后台交接；交接后
      // active_job 会被隐藏并进入上面的 handoff_notice 分支。
      const canLeave = matteComplete && presentationMode === "handoff"
        && active.handoff_ready === true;
      const phaseCopy = {
        processing: ["拍摄已完成", "正在准备人像", "正在读取刚刚定格的照片"],
        inferring: ["拍摄已完成", "K3 正在定位人像", "先锁定主体，再进入边缘精修"],
        matting: ["拍摄已完成", "正在精修人物边缘", "正在保留发丝、衣服与人物边缘"],
        stylizing: ["拍摄已完成", "正在生成风格", (styleLabel(active.style) || "所选风格") + " 正在成形"],
        rendering: ["拍摄已完成", "正在完成排版", "正在合成人像、版式与大会信息"],
        storing: ["拍摄已完成", "正在准备取图", "照片即将可以扫码查看"],
      }[active.stage] || ["拍摄已完成", "正在处理照片", "请稍候，流程正在继续"];
      el.badgeNow.textContent = active.stage === "countdown"
        ? "OK 已确认 · 请放下手保持姿势"
        : canLeave
        ? phaseCopy[1] + " · 后台生成中"
        : phaseCopy[1];
      // 抠像与风格化这两段现在由「生成之核」整体接管：旧的两套浮层
      // （扫描线抠像 + 轨道风格）与它画的是同一批数据，留着就是两套皮肤
      // 互相打架（2026-10-15 截图里出现过文字重叠）。
      // 它们只在 core 缺席时启用，作为退化路径。
      const matteOn = !core.active && (active.stage === "inferring" || active.stage === "matting")
        && (active.stage === "matting" || !(active.steps || []).some((s) => s.key === "infer"));
      const styleOn = !core.active && active.stage === "stylizing";
      setMattePhase(matteOn, active.matte_source, active.matte_ms);
      setStylePhase(styleOn, styleLabel(active.style),
        presentationMode === "handoff" ? null : active.style_preview_url);
      lastMatteOn = matteOn;
      lastStyleOn = styleOn;
      // 倒计时只做一件事：请放下手保持姿势；抓帧完成后明确告诉现场用户
      // 拍摄已经结束，可以离开去其他展区，回来再取图。
      const counting = active.stage === "countdown";
      renderCore(active);
      setPrompt(
        counting ? "" : phaseCopy[0],
        counting ? "请放下手，保持姿势" : core.active ? "" : phaseCopy[1],
        counting ? "看向镜头" : canLeave
          ? phaseCopy[2] + " · 可以先去其他展区，稍后回来取图"
          : phaseCopy[2]
      );
      if (active.cutout_url || active.style_preview_url) applyOverlayAssets(active);
      return;
    }

    if (pendingResult) return;

    el.view.classList.remove("is-scanning");
    if (lastMatteOn) { setMattePhase(false); lastMatteOn = false; }
    if (lastStyleOn) { setStylePhase(false); lastStyleOn = false; }
    el.view.classList.remove("is-core");
    core.clear();

    // 排队/到号阶段仍保持待机轮播；只有用户点击“开始拍摄”进入 ready
    // 后才开相机、关闭轮播并进入实时取景。排队期间不能出现黑屏。
    if (queueCameraActive) {
      el.view.classList.remove("is-idle");
      el.idleShow.classList.add("hide");
      const slot = state.queue_slot || null;
      const status = slot && slot.status;
      const slotNumber = String(slot && slot.number || "").match(/(\d+)$/)?.[1] || "";
      el.badgeNow.textContent = status === "called"
        ? (slotNumber ? slotNumber + " 请上前" : "下一位请上前")
        : status === "ready" ? "已就位 · 等待 OK 手势" : "排队中";
      if (status === "called") {
        setPrompt(slotNumber, "请上前", "在手机上点「开始拍摄」");
      } else if (status === "ready") {
        setPrompt("", "请比 OK 手势", state.countdown_seconds
          ? "在胸前比个 OK，识别到后倒计时 " + state.countdown_seconds + " 秒拍照"
          : "在胸前比个 OK，识别到后自动倒计时拍照");
      } else {
        setPrompt();
      }
      // 到号和等 OK 时由大字提示说清楚（生成之核只在拍摄开始后才亮）
      core.clear();
    } else if (queueCaptureActive) {
      const slot = state.queue_slot || null;
      const status = slot && slot.status;
      const slotNumber = String(slot && slot.number || "").match(/(\d+)$/)?.[1] || "";
      el.view.classList.add("is-idle");
      el.idleShow.classList.remove("hide");
      el.badgeNow.textContent = status === "called"
        ? (slotNumber ? slotNumber + " · 请在手机点击开始" : "请在手机点击开始")
        : "排队中";
      if (status === "called") {
        setPrompt(slotNumber, "请上前", "在手机上点「开始拍摄」");
      } else {
        setPrompt();
      }
      core.clear();
    } else if (visualActive && cameraOn) {
      setPrompt();
      el.view.classList.remove("is-idle");
      el.idleShow.classList.add("hide");
      el.badgeNow.textContent = gestureObs.is_ok ? "OK 手势已识别" : "手部关键点检测中";
    } else {
      setPrompt();
      el.view.classList.add("is-idle");
      el.idleShow.classList.remove("hide");
      if (el.wakeHint) el.wakeHint.textContent = "";
    }

    // 在场检测（后端门关着时 presence 读数会过期；只在它新鲜时用来亮取景）
    const presence = state.presence || {};
    const fresh = typeof presence.age_s === "number" && presence.age_s <= PRESENCE_STALE_S;
    const present = fresh ? presence.present : null;
    if (present !== true && !queueCaptureActive) {
      // 空闲待机：保持轮播
    }

    if (el.mCount) el.mCount.textContent = "已出图 " + String(state.photos || 0);
    if (el.vtip) el.vtip.textContent = "";
    // 页脚保持 HTML 里的固定说明；磁盘余量是后台信息，不放在参与者看的屏幕上

    /* 成片：刚出的那张先给本人看几秒 */
    if (presentationMode !== "handoff" && state.last_record && state.last_record !== lastShownRecord) {
      try {
        const overlay = await api.get("/api/overlay.json");
        if (overlay && overlay.record_id === state.last_record && overlay.record_id !== shownResultFor) {
          applyOverlayAssets(overlay);
          lastShownRecord = state.last_record;
          maybeRevealMask(overlay);
          if (overlay.token || (state.recent && state.recent.length)) {
            const hit = (state.recent || []).find((r) => r.id === state.last_record);
            if (hit) showResult({ record_id: hit.id, token: hit.token });
          }
        }
      } catch (err) { /* 叠加层拿不到不影响本页 */ }
    }
  }

  async function tick() {
    if (tickRunning) return;
    tickRunning = true;
    try { await tickState(); }
    finally { tickRunning = false; }
  }

  buildCarousel();
  tick();
  setInterval(tick, 400);
})();

