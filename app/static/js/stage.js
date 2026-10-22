/* stage.js —— 大屏算力视图：只显示真实读数（耗时、EP/CPU 徽章都来自 worker 的回报） */
(function () {
  "use strict";
  const { $, api, fmtMs, reducedMotion, renderSteps, watchWebRTC, carouselItems } = window.Booth;

  const STEPS = [
    { key: "capture", label: "抓帧" },
    { key: "infer", label: "人像分割" },
    { key: "matte", label: "抠像" },
    { key: "render", label: "风格化合成" },
    { key: "store", label: "入库编号" },
  ];

  /* 待机轮播：无人值守时的默认画面。
     取哪几张、怎么筛，全在 Booth.carouselItems（net.js）里 —— 大屏和竖屏
     共用一份，加风格只需改风格目录，两个屏自动跟上。
     一张都拿不到就如实隐藏整个待机层，不放占位图假装有内容。 */
  const CAROUSEL_INTERVAL_MS = 5000;
  const PRESENCE_STALE_S = 10;
  const GESTURE_OBS_STALE_S = 1.2;

  const el = {
    view: $("#view"), mask: $("#mask"), boxes: $("#boxes"), badgeEp: $("#badge-ep"),
    badgeCam: $("#badge-cam"), badgeMock: $("#badge-mock"), badgeNow: $("#badge-now"),
    clock: $("#clock"), mInfer: $("#m-infer"), mEp: $("#m-ep"), mTotal: $("#m-total"),
    mPersons: $("#m-persons"), mAvg: $("#m-avg"), mCount: $("#m-count"),
    steps: $("#steps"), recent: $("#recent"), degraded: $("#degraded-note"),
    degradedText: $("#degraded-text"), preview: $("#preview"), stream: $("#preview-stream"), footNote: $("#foot-note"),
    camState: $("#camstate"), idleShow: $("#idle-show"),
    slide: $("#idle-slide"), slideLabel: $("#idle-slide-label"), slideDots: $("#idle-slide-dots"),
    matting: $("#matting"), matteCaption: $("#matte-caption"),
    wakeHint: $("#wake-hint"),
  };

  let lastShown = null;
  let animating = false;
  let apiPreviewHealthy = false;
  let lastStageKey = "";
  let capturePreviewUrl = "";
  let capturePreviewJob = null;
  let streamWatcher = null;

  function resumePreview() {
    if (streamWatcher) return;
    el.stream.classList.add("is-visible");
    el.preview.classList.remove("is-visible");
    streamWatcher = watchWebRTC(el.stream, { onState: setCamState });
  }

  function pausePreview() {
    if (streamWatcher) streamWatcher.stop();
    streamWatcher = null;
    el.stream.classList.remove("is-visible");
    if (!capturePreviewUrl) el.preview.classList.remove("is-visible");
    // 预览门关闭是有意待机，不是相机故障；轮播层必须继续可见。
    el.view.classList.remove("is-offline");
  }

  function syncCapturePreview(state) {
    const active = state.active_job;
    if (!active && state.presentation_mode === "handoff" && capturePreviewUrl) {
      capturePreviewUrl = "";
      capturePreviewJob = null;
      if (streamWatcher) streamWatcher.stop();
      streamWatcher = null;
      el.stream.classList.remove("is-visible");
      el.preview.classList.remove("is-visible");
      el.mask.removeAttribute("src");
    }
    const url = active && active.capture_preview_url;
    if (url) {
      if (url === capturePreviewUrl) return;
      capturePreviewUrl = url;
      capturePreviewJob = active.job;
      if (streamWatcher) streamWatcher.stop();
      streamWatcher = null;
      drawGestureOverlay(null);
      setCamState("live");
      el.view.classList.remove("is-idle");
      if (el.idleShow) el.idleShow.classList.add("hide");
      el.preview.src = url;
      el.preview.classList.add("is-visible");
    } else if (capturePreviewUrl && active && active.job
      && active.job !== capturePreviewJob
      && ["queued", "called", "ready", "countdown", "capturing"].includes(active.stage)) {
      capturePreviewUrl = "";
      resumePreview();
    }
  }

  /* ---- 待机轮播 -------------------------------------------------------
     只在 is-idle（待机）且取景活着时显示；探测不到任何样张时整个隐藏，
     退回 hero 欢迎语。绝不显示破图。 */
  const carousel = { items: [], index: 0, timer: null };

  function showCarouselSlide() {
    if (!carousel.items.length) return;
    const item = carousel.items[carousel.index % carousel.items.length];
    el.slide.src = item.src;
    el.slide.alt = "风格示意：" + item.label;
    if (el.slideLabel) el.slideLabel.textContent = item.label;
    if (el.slideDots) {
      Array.from(el.slideDots.children).forEach((dot, index) => {
        const active = index === carousel.index % carousel.items.length;
        dot.classList.toggle("is-active", active);
      });
    }
    carousel.index = (carousel.index + 1) % carousel.items.length;
  }

  function buildCarousel() {
    if (!el.idleShow || !el.slide) return;
    carouselItems()
      .then((items) => {
      carousel.items = items;
      if (!items.length) {
        el.idleShow.classList.add("hide");
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

  /* ---- 抠像阶段动画：隔离出「把人从背景里剥出来」这一步 --------------- */
  function setMattePhase(on, source, matteMs) {
    if (!el.matting) return;
    el.matting.classList.toggle("is-on", Boolean(on));
    el.matting.setAttribute("aria-hidden", String(!on));
    if (!on || !el.matteCaption) return;
    const srcText = source === "modnet-roi" ? "按人框精细抠像"
      : source === "modnet" ? "整幅精细抠像"
      : source === "detect-mask" ? "检测掩码兜底"
      : "逐像素分离人像";
    el.matteCaption.textContent = matteMs
      ? srcText + " · " + fmtMs(matteMs)
      : srcText;
  }

  /* 大屏没人守着：流断了要在画面上直说，不能对着一张不动的图让人猜 */
  function setCamState(state, reason) {
    if (capturePreviewUrl) state = "live";
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

  function clock() {
    el.clock.textContent = new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }).format(new Date()).replace(/\//g, ".");
  }

  function drawBoxes(persons, width, height) {
    const canvas = el.boxes;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    if (!persons || !persons.length || !width || !height) return;
    // preview 用 object-fit: cover，这里按 cover 的缩放比换算坐标
    const scale = Math.max(rect.width / width, rect.height / height);
    const offsetX = (rect.width - width * scale) / 2;
    const offsetY = (rect.height - height * scale) / 2;
    ctx.lineWidth = 1.5;
    ctx.font = "12px ui-monospace, monospace";
    persons.forEach((person, index) => {
      const box = person.box || [];
      if (box.length < 4) return;
      const x = box[0] * scale + offsetX;
      const y = box[1] * scale + offsetY;
      const w = (box[2] - box[0]) * scale;
      const h = (box[3] - box[1]) * scale;
      // 取景画面是镜像的（.mirror-x），框要跟着翻到同一侧
      const mx = rect.width - (x + w);
      ctx.strokeStyle = index === 0 ? "rgba(159,239,0,0.95)" : "rgba(159,239,0,0.55)";
      ctx.strokeRect(mx, y, w, h);
      const label = "person " + (person.score ? person.score.toFixed(2) : "");
      ctx.fillStyle = "rgba(0,0,0,0.55)";
      const textWidth = ctx.measureText(label).width + 10;
      ctx.fillRect(mx, Math.max(0, y - 18), textWidth, 18);
      ctx.fillStyle = "rgba(159,239,0,0.95)";
      ctx.fillText(label, mx + 5, Math.max(12, y - 5));
    });
  }

  function drawGestureOverlay(obs) {
    const canvas = el.boxes;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    if (!obs || Date.now() / 1000 - (obs.at || 0) > GESTURE_OBS_STALE_S || !obs.img_w || !obs.img_h) return;
    const scale = Math.max(rect.width / obs.img_w, rect.height / obs.img_h);
    const offX = (rect.width - obs.img_w * scale) / 2;
    const offY = (rect.height - obs.img_h * scale) / 2;
    const map = (point) => [rect.width - (point[0] * scale + offX), point[1] * scale + offY];
    function box(item, color, label) {
      const a = map([item[0], item[1]]), b = map([item[2], item[3]]);
      const x = Math.min(a[0], b[0]), y = Math.min(a[1], b[1]);
      const w = Math.abs(b[0] - a[0]), h = Math.abs(b[1] - a[1]);
      ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.strokeRect(x, y, w, h);
      ctx.font = "bold 12px system-ui, sans-serif";
      const tw = ctx.measureText(label).width + 10;
      ctx.fillStyle = "rgba(0,0,0,.65)"; ctx.fillRect(x, Math.max(0, y - 19), tw, 19);
      ctx.fillStyle = color; ctx.fillText(label, x + 5, Math.max(13, y - 5));
    }
    const ok = Boolean(obs.is_ok);
    (obs.hand_boxes || []).forEach((item) => box(item, ok ? "#9FEF00" : "#FFD43B", ok ? "OK ✓" : "手部"));
    (obs.palm_boxes || []).forEach((item) => box(item, "#38D9FF", "手掌"));
    (obs.ok_boxes || []).forEach((item) => box(item, "#9FEF00", "OK ✓"));
    const points = obs.hand_points || [];
    const links = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[0,9],[9,10],[10,11],[11,12],[0,13],[13,14],[14,15],[15,16],[0,17],[17,18],[18,19],[19,20],[5,9],[9,13],[13,17]];
    ctx.strokeStyle = ok ? "#9FEF00" : "#FFD43B"; ctx.fillStyle = "#FFFFFF"; ctx.lineWidth = 2;
    links.forEach((link) => {
      if (!points[link[0]] || !points[link[1]]) return;
      const a = map(points[link[0]]), b = map(points[link[1]]);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    });
    points.forEach((point) => {
      const p = map(point); ctx.beginPath(); ctx.arc(p[0], p[1], 3.5, 0, Math.PI * 2); ctx.fill();
    });
  }

  async function animateResult(overlay) {
    if (animating || !overlay || !overlay.record_id) return;
    animating = true;
    // 退回上一张的读数时要说清楚是哪一张，不能让「本次推理」指着上一张的旧数字
    const serial = String(overlay.record_id || "").match(/(\d+)$/)?.[1] || "—";
    el.badgeNow.textContent = (overlay.restored ? "最近一次 · " : "已出图 ") + serial +
      " · " + (overlay.portrait_mode === "chibi" ? "Q版大头像" : "写实版");
    if (overlay.mask_url) {
      el.mask.src = overlay.mask_url + "?v=" + Date.now();
      el.mask.classList.remove("is-reveal");
      if (!reducedMotion()) {
        void el.mask.offsetWidth;
        el.mask.classList.add("is-reveal");
        window.setTimeout(() => el.mask.classList.remove("is-reveal"), 2400);
      } else {
        el.mask.style.opacity = "0.12";
      }
    }
    drawBoxes(capturePreviewUrl ? [] : overlay.persons, overlay.width, overlay.height);
    window.setTimeout(() => { animating = false; }, 1400);
  }

  function renderRecent(list) {
    if (!list || !list.length) {
      el.recent.innerHTML = '<span class="empty">还没有成片</span>';
      return;
    }
    el.recent.innerHTML = "";
    list.slice(0, 6).forEach((item) => {
      const serial = String(item.id || "").match(/(\d+)$/)?.[1] || "—";
      const node = document.createElement("div");
      node.className = "thumb";
      node.title = serial + " · " + fmtMs(item.infer_ms) + " · " + (item.portrait_mode === "chibi" ? "Q版大头像" : "写实版");
      const img = document.createElement("img");
      img.src = item.thumb + "?v=" + item.id;
      img.alt = serial + " · " + (item.portrait_mode === "chibi" ? "Q版大头像" : "写实版");
      img.loading = "lazy";
      node.appendChild(img);
      el.recent.appendChild(node);
    });
  }

  async function tick() {
    let state;
    try {
      state = await api.get("/api/state");
    } catch (err) {
      el.badgeCam.textContent = "服务未响应";
      el.badgeCam.className = "badge badge-degraded";
      return;
    }
    syncCapturePreview(state);
    const previewState = state.preview || {};
    const previewGateOpen = previewState.gate_open !== false;
    if (previewGateOpen || state.active_job || capturePreviewUrl) resumePreview();
    else pausePreview();
    const ep = state.infer && state.infer.ep;
    el.badgeEp.textContent = ep
      ? "EP SpaceMIT · " + (state.infer.ep_threads || 0) + " 线程"
      : (state.infer && state.infer.preloaded ? "CPU · 未拿到 AI 核" : "算力预热中");
    el.badgeEp.className = "badge " + (ep ? "badge-ep" : "badge-cpu");
    const cam = state.camera || {};
    const previewCamera = previewState.camera || {};
    const isMock = cam.device === "mock" || cam.fourcc === "MOCK" || cam.real === false;
    // 静态照片联调源：画面有真内容，不藏画面（与空灰合成源区分开）
    const staticPhoto = Boolean(state.camera_static_photo);
    const previewHealthy = Boolean(cam.ok && previewCamera.open !== false
      && (previewState.last_frame_s != null || staticPhoto));
    apiPreviewHealthy = previewHealthy;
    el.badgeCam.textContent = cam.ok ? (cam.width + "×" + cam.height + " " + (cam.fourcc || "")) : "相机不可用";
    el.badgeCam.className = "badge " + (cam.ok ? "" : "badge-degraded");
    el.view.classList.toggle("is-mock", isMock && !staticPhoto && !capturePreviewUrl);
    if (isMock && !staticPhoto) {
      setCamState("mock");
    } else if (!previewHealthy) {
      el.badgeCam.textContent = cam.ok ? "取景流断开" : "相机不可用";
      el.badgeCam.className = "badge badge-degraded";
      setCamState("stalled", previewState.last_frame_s == null ? "no-frame" : "stalled");
    }
    el.badgeMock.classList.toggle("hide", !isMock);
    el.mCount.innerHTML = (state.photos || 0) + "<small>张</small>";
    el.mAvg.textContent = fmtMs(state.avg_infer_ms);
    el.footNote.textContent = "成片保存 1 天 · 已用 " + (state.disk && state.disk.free_gb ? state.disk.free_gb + " GB 可用" : "—");

    const active = state.active_job;
    const gestureObs = state.gesture_obs || null;
    const gestureFresh = gestureObs && Date.now() / 1000 - (gestureObs.at || 0) <= GESTURE_OBS_STALE_S;
    const visualActive = Boolean(gestureFresh && (gestureObs.hand_points || []).length);
    if (!active) drawGestureOverlay(capturePreviewUrl ? null : gestureObs);
    if (active) {
      el.view.classList.add("is-scanning");
      el.view.classList.remove("is-idle");
      el.badgeNow.textContent = "处理中 · " + (active.stage || "");
      const doneKeys = (active.steps || []).map((s) => s.key);
      const activeKey = doneKeys.length ? doneKeys[doneKeys.length - 1] : "capture";
      renderSteps(el.steps, activeKey, doneKeys.slice(0, -1), STEPS);
      el.mTotal.textContent = fmtMs(active.elapsed_ms);
      if (active.infer_ms) el.mInfer.textContent = fmtMs(active.infer_ms);
      // 抠像阶段的专属动画：stage=inferring 且 alpha 还没落时，隔离层亮起。
      const matteOn = active.stage === "inferring" && !active.steps.some((s) => s.key === "infer");
      setMattePhase(matteOn, active.matte_source, active.matte_ms);
      lastStageKey = active.stage || "";
    } else {
      el.view.classList.remove("is-scanning");
      if (state.photos) el.view.classList.remove("is-idle");
      if (state.last_record) {
        renderSteps(el.steps, null, STEPS.map((item) => item.key), STEPS);
      } else {
        renderSteps(el.steps, "capture", [], STEPS);
      }
      if (lastStageKey) { setMattePhase(false); lastStageKey = ""; }
    }

    // 在场驱动：待机画面（轮播）默认在；「有人走近」→ 退场亮取景；
    // 「确认没人」→ 回到待机。读数过期或缺失按「测不了」，保持现状不抖动。
    const presence = state.presence || {};
    const fresh = typeof presence.age_s === "number" && presence.age_s <= PRESENCE_STALE_S;
    const present = fresh ? presence.present : null;
    if (el.idleShow && !active) {
      // 相机门关闭时没有新的在场数据，必须保持待机轮播，不能被旧状态或
      // MJPEG 断流状态切成黑屏。
      const someoneClose = previewGateOpen
        && (Boolean(capturePreviewUrl) || present === true || visualActive);
      // 有人走近：待机层退场、取景全亮（is-idle 的 0.42 暗化必须摘掉）；
      // 没人 / 测不了：待机层在，暗化取景垫在轮播底下。
      el.view.classList.toggle("is-idle", !someoneClose);
      el.idleShow.classList.toggle("hide", someoneClose);
      if (el.wakeHint) {
        el.wakeHint.textContent = someoneClose
          ? (visualActive ? "手部关键点已识别 · 请面对镜头" : "欢迎！到拍摄位站好，选好风格就能拍")
          : "站到镜头前 · K3 当场用 RISC-V AI 核分离人像 · 扫码取走成片";
      }
    }

    if (state.last_record && state.last_record !== lastShown && !active
        && (state.presentation_mode || "full") !== "handoff") {
      try {
        const overlay = await api.get("/api/overlay.json");
        if (overlay && overlay.record_id === state.last_record) {
          lastShown = state.last_record;   // 真拿到读数才算数，否则下一轮继续试
          el.mInfer.textContent = fmtMs(overlay.infer_ms);
          el.mEp.textContent = overlay.degraded ? "L0 降级" : (overlay.ep ? "EP SpaceMIT" : "CPU");
          el.mEp.parentElement.className = "metric " + (overlay.ep && !overlay.degraded ? "hot" : "warn");
          el.mPersons.textContent = (overlay.person_count || 0) + " 人";
          // 端到端只认服务端记下的真实总耗时。以前这里写的是 infer_ms + 300，
          // 在一个号称「只显示真实读数」的屏幕上编了个数。
          el.mTotal.textContent = overlay.total_ms ? fmtMs(overlay.total_ms) : "—";
           el.degraded.hidden = !overlay.degraded;
          el.degradedText.textContent = "本次降级：" + (overlay.degraded_reason || "未知原因") + "（成片照常生成）";
           const record = (state.recent || []).find((item) => item.id === overlay.record_id);
           animateResult({ ...overlay, portrait_mode: record ? record.portrait_mode : overlay.portrait_mode });
        }
      } catch (err) { /* 叠加层拿不到不影响大屏 */ }
    }
    const hideRecent = state.presentation_mode === "handoff";
    el.recent.hidden = hideRecent;
    renderRecent(hideRecent ? [] : state.recent);
  }

  clock();
  setInterval(clock, 1000);
  buildCarousel();
  tick();
  setInterval(tick, 400);
  window.addEventListener("resize", () => drawBoxes([], 0, 0));
  el.stream.addEventListener("error", () => {
    if (capturePreviewUrl || !streamWatcher) return;
    const watcher = streamWatcher;
    setCamState("stalled", "no-frame");
    setTimeout(() => {
      if (!capturePreviewUrl && streamWatcher === watcher) watcher.reopen("no-frame");
    }, 2000);
  });
  el.stream.addEventListener("loadeddata", () => {
    if (capturePreviewUrl) return;
    if (el.view.classList.contains("is-mock")) setCamState("mock");
    else setCamState("live");
  });
  /* 大屏是无人值守的：断流后必须自己恢复，不能停在最后一帧等人工刷新 */
  // 由 /api/state 的 preview.gate_open 控制视频连接：待机时只展示轮播，
  // 用户点击“开始拍摄”后才打开相机和实时取景。
})();
