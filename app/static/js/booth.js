/* booth.js —— 拍摄位状态机：idle → counting → working → done */
(function () {
  "use strict";
  const { $, $$, api, toast, fmtMs, reducedMotion, renderSteps, pickMnemonic, watchWebRTC,
          notice, buildGuard } = window.Booth;

  const STEPS = [
    { key: "capture", label: "抓帧" },
    { key: "infer", label: "人像分割" },
    { key: "render", label: "风格化合成" },
    { key: "store", label: "入库编号" },
  ];
  const GESTURE_OBS_STALE_S = 1.2;

  const state = { mode: "idle", style: null, styles: [], styleCatalog: [], aigcEnabled: false,
                  orientation: "vertical", token: "", consented: false, job: null, timer: null };
  const el = {
    stage: $("#stagebox"), preview: $("#preview"), stream: $("#preview-stream"), camState: $("#camstate"),
    countdown: $("#countdown"), flash: $("#flash"),
    aigcVisual: $("#aigc-visual"), aigcVisualText: $("#aigc-visual-text"),
    panel: $("#panel"), styles: $("#styles"), orientations: $("#orientations"),
    styleNote: $("#style-note"),
    name: $("#name"), handle: $("#handle"),
    agree: $("#agree"), fire: $("#fire"), hint: $("#hint"), flow: $("#flow"),
    steps: $("#steps"), flowHint: $("#flow-hint"), result: $("#result"), shot: $("#shot"),
    serial: $("#serial"), qr: $("#qr"), download: $("#download"), copy: $("#copy"),
    again: $("#again"),
    badgeEp: $("#badge-ep"),
    badgeCam: $("#badge-cam"),
    badgeMock: $("#badge-mock"),
    scrim: $("#scrim"), legal: $("#legal"), openLegal: $("#open-legal"),
    statInfer: $("#stat-infer"), statEp: $("#stat-ep"), statTotal: $("#stat-total"), statPersons: $("#stat-persons"),
    boxesCanvas: $("#boxes-stage"),
  };
  let modalReturnFocus = null;
  let capturePreviewUrl = "";
  let streamWatcher = null;
  const portraitMode = window.Booth.mountPortraitMode($("#capture-portrait-mode"));

  function resumePreview() {
    capturePreviewUrl = "";
    if (streamWatcher) return;
    el.stream.classList.add("is-visible");
    el.preview.classList.remove("is-visible");
    streamWatcher = watchWebRTC(el.stream, { onState: setCamState });
  }

  function showCapturePreview(snapshot) {
    const url = snapshot && snapshot.capture_preview_url;
    if (!url || url === capturePreviewUrl) return;
    capturePreviewUrl = url;
    if (streamWatcher) streamWatcher.stop();
    streamWatcher = null;
    el.stream.classList.remove("is-visible");
    drawBoxes(null);
    setCamState("live");
    el.preview.src = url;
    el.preview.classList.add("is-visible");
  }

  function setMode(mode) {
    state.mode = mode;
    el.panel.dataset.mode = mode === "working" ? "flow" : "idle";
    el.flow.classList.toggle("is-on", mode === "working");
  }

  function setHint(text) { el.hint.textContent = text; }

  function setAigcVisual(on, elapsedMs) {
    if (!el.aigcVisual) return;
    el.aigcVisual.classList.toggle("is-on", on);
    el.aigcVisual.setAttribute("aria-hidden", String(!on));
    if (!on || !el.aigcVisualText) return;
    const seconds = Math.floor((elapsedMs || 0) / 1000);
    const messages = [
      "正在保留五官与姿势",
      "正在抽取一张匹配背景",
      "正在统一人物与场景光线",
      "正在完成最后的画面细节",
    ];
    el.aigcVisualText.textContent = messages[Math.min(messages.length - 1, Math.floor(seconds / 9))];
  }

  function refreshFireState() {
    // 自然风格（原片）不需要 AIGC，拍了就能出图；
    // AIGC 风格需要 aigcEnabled。只要同意了、空闲状态、且选了风格，就亮按钮。
    const styleNeedsAigc = state.style && state.style !== "natural";
    const ready = state.consented && state.mode === "idle" &&
                  Boolean(state.style) &&
                  (!styleNeedsAigc || state.aigcEnabled);
    el.fire.disabled = !ready;
    if (!state.consented) setHint("先同意《拍摄告知》，按钮才会亮");
    else if (!state.style) setHint("选一种风格");
    else if (styleNeedsAigc && !state.aigcEnabled) setHint("生成服务未开启，请选「原片」风格或联系工作人员");
    else if (state.mode === "idle") setHint("按下按钮，8 秒后自动拍摄");
  }

  /* ---- 同意门禁 ---- */
  function openLegal() {
    modalReturnFocus = document.activeElement;
    el.scrim.classList.add("is-open");
    el.legal.classList.add("is-open");
    el.scrim.setAttribute("aria-hidden", "false");
    el.legal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    const firstAction = el.legal.querySelector("[data-legal-action]");
    if (firstAction) firstAction.focus({ preventScroll: true });
  }
  function closeLegal() {
    el.scrim.classList.remove("is-open");
    el.legal.classList.remove("is-open");
    el.scrim.setAttribute("aria-hidden", "true");
    el.legal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus({ preventScroll: true });
    modalReturnFocus = null;
  }

  el.agree.addEventListener("change", async () => {
    if (!el.agree.checked) {
      state.consented = false;
      state.token = "";
      refreshFireState();
      return;
    }
    openLegal();
  });

  $$("[data-legal-action]", el.legal).forEach((node) => {
    node.addEventListener("click", async () => {
      closeLegal();
      if (node.dataset.legalAction === "agree") {
        try {
          const granted = await api.post("/api/consent", { agree: true, kind: "photo" });
          state.token = granted.token;
          state.consented = true;
          el.agree.checked = true;
          setHint("已记录同意 · 按下按钮，8 秒后自动拍摄");
        } catch (err) {
          el.agree.checked = false;
          toast("同意登记失败：" + err.message);
        }
      } else {
        el.agree.checked = false;
        state.consented = false;
        state.token = "";
      }
      refreshFireState();
    });
  });
  el.openLegal.addEventListener("click", (event) => { event.preventDefault(); openLegal(); });
  // 点遮罩 = 关掉但没表态。勾选框要跟着退回未勾选，
  // 否则会出现「看着已经勾上、开始拍摄却还是灰的」——现场没人能想出为什么。
  function dismissLegal() {
    closeLegal();
    if (!state.consented) {
      el.agree.checked = false;
      refreshFireState();
    }
  }
  el.scrim.addEventListener("click", dismissLegal);

  let stylePreviewGeneration = 0;

  function refreshStyleThumbs(mode) {
    const generation = ++stylePreviewGeneration;
    $$(".style-visual img", el.styles).forEach((image) => {
      const id = image.dataset.styleId;
      function probeMode(candidateMode) {
        const url = window.Booth.styleThumb(id, candidateMode);
        const probe = new Image();
        probe.onload = () => {
          if (generation === stylePreviewGeneration && image.isConnected) image.src = url;
        };
        probe.onerror = () => {
          if (generation !== stylePreviewGeneration || !image.isConnected) return;
          if (candidateMode === "chibi") probeMode("standard");
          else image.removeAttribute("src");
        };
        probe.src = url;
      }
      probeMode(window.Booth.normalizePortraitMode(mode));
    });
  }

  window.addEventListener("booth:portrait-mode", (event) => refreshStyleThumbs(event.detail));

  function renderStyles(list, current, enabled) {
    state.styles = list || [];
    const items = state.styleCatalog;
    const available = new Set(items.map((item) => item.id || item.name));
    const preferred = available.has(state.style) ? state.style : current;
    state.style = available.has(preferred) ? preferred : (items[0]?.id || items[0]?.name || "");
    el.styles.innerHTML = "";
    items.forEach((item) => {
      const id = item.id || item.name;
      const selectable = enabled;
      const card = document.createElement("button");
      card.type = "button";
      card.className = "style-card" + (id === state.style ? " is-selected" : "") + (!selectable ? " is-locked" : "");
      card.disabled = !selectable;
      card.setAttribute("aria-pressed", String(id === state.style));
      const visual = document.createElement("span");
      visual.className = "style-visual";
      if (id) {
        const image = document.createElement("img");
        image.dataset.styleId = id;
        image.alt = "";
        image.loading = "lazy";
        image.decoding = "async";
        visual.appendChild(image);
      }
      const name = document.createElement("strong");
      name.textContent = window.Booth.styleDisplay(item).title;
      const description = document.createElement("small");
      description.textContent = window.Booth.styleDisplay(item).description;
      card.append(visual, name, description);
      card.addEventListener("click", () => {
        state.style = id;
        $$(".style-card", el.styles).forEach((node) => {
          const selected = node === card;
          node.classList.toggle("is-selected", selected);
          node.setAttribute("aria-pressed", String(selected));
        });
      });
      el.styles.appendChild(card);
    });
    refreshStyleThumbs(portraitMode.value());
    if (el.styleNote) {
      el.styleNote.textContent = enabled ? "生成服务已连接 · 每次抽取简约背景" : "生成服务未开启 · 暂不可生成";
    }
  }

  function renderOrientation(value) {
    state.orientation = value === "wide" ? "wide" : "vertical";
    $$(".segment", el.orientations).forEach((node) => {
      const selected = node.dataset.orientation === state.orientation;
      node.classList.toggle("is-active", selected);
      node.setAttribute("aria-pressed", String(selected));
    });
  }

  $$(".segment", el.orientations).forEach((node) => {
    node.addEventListener("click", () => renderOrientation(node.dataset.orientation));
  });

  /* ---- 实时检测框（yolo 手势 + 分割）--------------------------------------
     state.gesture_obs 从 /api/state 读取，含 persons / ok_boxes / img_w / img_h。
     canvas 覆盖整个 stagebox，坐标换算同 vertical.js 的 drawGestureBoxes。 */
  function drawBoxes(gestureObs) {
    const canvas = el.boxesCanvas;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
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
    const age = Date.now() / 1000 - (gestureObs.at || 0);
    if (age > GESTURE_OBS_STALE_S) return;
    const imgW = gestureObs.img_w || 0, imgH = gestureObs.img_h || 0;
    if (!imgW || !imgH) return;
    const vW = rect.width, vH = rect.height;
    const scale = Math.max(vW / imgW, vH / imgH);
    const offX = (vW - imgW * scale) / 2, offY = (vH - imgH * scale) / 2;
    function imgToView(x, y) {
      return [vW - (x * scale + offX), y * scale + offY];
    }
    function drawBox(box, color, label) {
      const p1 = imgToView(box[0], box[1]), p2 = imgToView(box[2], box[3]);
      const x1 = Math.min(p1[0], p2[0]), y1 = Math.min(p1[1], p2[1]);
      const w = Math.abs(p2[0] - p1[0]), h = Math.abs(p2[1] - p1[1]);
      ctx.save();
      ctx.strokeStyle = color; ctx.lineWidth = 2.5;
      ctx.shadowColor = color; ctx.shadowBlur = 8;
      ctx.strokeRect(x1, y1, w, h);
      if (label) {
        ctx.font = "bold 12px system-ui, sans-serif";
        const tw = ctx.measureText(label).width;
        ctx.shadowBlur = 0;
        ctx.fillStyle = color + "CC";
        ctx.fillRect(x1, y1 - 19, tw + 10, 19);
        ctx.fillStyle = "#000";
        ctx.fillText(label, x1 + 5, y1 - 5);
      }
      ctx.restore();
    }
    const isOk = Boolean(gestureObs.is_ok);
    (gestureObs.persons || []).forEach((b) => drawBox(b, isOk ? "#9FEF00" : "#4ea8de", "人形"));
    (gestureObs.ok_boxes || []).forEach((b) => drawBox(b, "#9FEF00", "OK ✓"));
    (gestureObs.palm_boxes || []).forEach((b) => drawBox(b, "#38D9FF", "手掌"));
    (gestureObs.hand_boxes || []).forEach((b) => drawBox(b, isOk ? "#9FEF00" : "#FFD43B", isOk ? "OK ✓" : "手部"));
    const points = gestureObs.hand_points || [];
    const links = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[0,9],[9,10],[10,11],[11,12],[0,13],[13,14],[14,15],[15,16],[0,17],[17,18],[18,19],[19,20],[5,9],[9,13],[13,17]];
    ctx.save();
    ctx.strokeStyle = isOk ? "#9FEF00" : "#FFD43B";
    ctx.fillStyle = isOk ? "#9FEF00" : "#FFFFFF";
    ctx.lineWidth = 2;
    links.forEach((link) => {
      if (!points[link[0]] || !points[link[1]]) return;
      const a = imgToView(points[link[0]][0], points[link[0]][1]);
      const b = imgToView(points[link[1]][0], points[link[1]][1]);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    });
    points.forEach((point) => {
      const p = imgToView(point[0], point[1]);
      ctx.beginPath(); ctx.arc(p[0], p[1], 3.5, 0, Math.PI * 2); ctx.fill();
    });
    ctx.restore();
  }

  /* ---- 状态轮询（徽章必须是真实读数） ---- */
  let statePolling = false;
  async function pollState() {
    if (statePolling) return;
    statePolling = true;
    try {
      const s = await api.get("/api/state");
      state.aigcEnabled = Boolean(s.aigc_enabled);
      if (!state.styleCatalog.length) {
        try {
          const catalog = await api.get("/api/aigc/styles");
          state.styleCatalog = catalog.styles || [];
        } catch (err) { state.styleCatalog = []; }
      }
      if (!state.styles.length || (state.aigcEnabled && !state.style)) {
        renderStyles(s.styles || [], state.aigcEnabled ? s.default_style : null, state.aigcEnabled);
      }
      renderOrientation(state.orientation);
      const ep = s.infer && s.infer.ep;
      if (el.badgeEp) {
        el.badgeEp.textContent = ep
          ? "EP SpaceMIT · " + (s.infer.ep_threads || 0) + " 线程"
          : (s.infer && s.infer.preloaded ? "CPU · 未拿到 AI 核" : "算力预热中");
        el.badgeEp.className = "badge " + (ep ? "badge-ep" : "badge-cpu");
      }
      const cam = s.camera || {};
      if (el.badgeCam) {
        el.badgeCam.textContent = cam.ok
          ? (cam.width + "×" + cam.height + " " + (cam.fourcc || ""))
          : "相机不可用";
        el.badgeCam.className = "badge " + (cam.ok ? "" : "badge-degraded");
        el.badgeCam.classList.remove("hide");
      }
      const isMock = cam.device === "mock" || cam.fourcc === "MOCK" || cam.real === false;
      const staticPhoto = Boolean(s.camera_static_photo);
      el.stage.classList.toggle("is-mock", isMock && !staticPhoto && !capturePreviewUrl);
      if (el.badgeMock) el.badgeMock.classList.toggle("hide", !(isMock && !staticPhoto));
      if (isMock && !staticPhoto) setCamState("mock");
      // 实时检测框：队列有活动或手势观测数据时绘框
      const gestureObs = s.gesture_obs || null;
      drawBoxes(capturePreviewUrl ? null : gestureObs);
      refreshFireState();
    } catch (err) {
      if (el.badgeCam) {
        el.badgeCam.textContent = "服务未响应";
        el.badgeCam.className = "badge badge-degraded";
      }
    } finally {
      statePolling = false;
    }
  }

  /* ---- 拍摄流程 ---- */
  function countdown(seconds) {
    return new Promise((resolve) => {
      const deadline = Date.now() + seconds * 1000;
      el.countdown.style.transition = reducedMotion() ? "none" : "";
      const tick = () => {
        const remaining = deadline - Date.now();
        const left = Math.ceil(remaining / 1000);
        if (left <= 0) {
          el.countdown.classList.remove("is-show");
          return resolve();
        }
        el.countdown.textContent = String(left);
        el.countdown.classList.add("is-show");
        setTimeout(tick, Math.min(1000, remaining));
      };
      tick();
    });
  }

  async function ensureConsent() {
    if (state.token) return state.token;
    const granted = await api.post("/api/consent", { agree: true, kind: "photo" });
    state.token = granted.token;
    state.consented = true;
    return state.token;
  }

  async function capture() {
    if (state.mode !== "idle" || !state.consented) return;
    const selectedPortraitMode = portraitMode.value();
    resumePreview();
    setMode("working");
    setAigcVisual(false, 0);
    /* 抓帧 + 推理这十来秒别让版本看门狗把页面刷掉：参会者正对着镜头站着，
       刷掉就等于让他重来一次（net.js 里那份看门狗，坑 38）。 */
    buildGuard.hold(true);
    el.fire.classList.add("busy");
    setHint("保持姿势，正在拍摄…");
    el.flowHint.textContent = "准备中…";
    renderSteps(el.steps, "capture", [], STEPS);

    try {
      await countdown(8);
      el.flash.classList.add("is-fire");
      setTimeout(() => el.flash.classList.remove("is-fire"), 520);

      const token = await ensureConsent();
      let job;
      try {
        job = await api.post("/api/capture", {
          style: state.style, orientation: state.orientation, portrait_mode: selectedPortraitMode,
          name: el.name.value.trim(), handle: el.handle.value.trim(),
          mnemonic: pickMnemonic(),
        }, { "X-Booth-Session": token });
      } catch (err) {
        if (err.status === 403) {
          state.token = "";
          const fresh = await ensureConsent();
          job = await api.post("/api/capture", {
            style: state.style, orientation: state.orientation, portrait_mode: selectedPortraitMode,
            name: el.name.value.trim(), handle: el.handle.value.trim(),
            mnemonic: pickMnemonic(),
          }, { "X-Booth-Session": fresh });
        } else {
          throw err;
        }
      }
      state.job = job;
      showCapturePreview(job);
      await follow(job.job);
    } catch (err) {
      setAigcVisual(false, 0);
      setMode("idle");
      buildGuard.hold(false);
      el.fire.classList.remove("busy");
      toast("拍摄失败：" + err.message);
      setHint("出了点问题，再按一次即可");
      refreshFireState();
    }
  }

  function follow(jobId) {
    return new Promise((resolve, reject) => {
      let lastStage = "";
      const poll = async () => {
        if (state.mode !== "working") return resolve();
        let snapshot;
        try {
          snapshot = await api.get("/api/job/" + jobId);
        } catch (err) {
          return reject(err);
        }
        showCapturePreview(snapshot);
        const doneKeys = (snapshot.steps || []).map((s) => s.key);
        const active = snapshot.stage === "stylizing" ? "render" : (snapshot.steps || []).length
          ? snapshot.steps[snapshot.steps.length - 1].key : "capture";
        renderSteps(el.steps, active, doneKeys.slice(0, -1), STEPS);
        if (snapshot.steps && snapshot.steps.length) {
          const last = snapshot.steps[snapshot.steps.length - 1];
          el.flowHint.textContent = last.label + (last.detail ? " · " + last.detail : "");
        }
        if (snapshot.stage !== lastStage) lastStage = snapshot.stage;
        setAigcVisual(snapshot.stage === "stylizing", snapshot.elapsed_ms);

        if (snapshot.stage === "done" && snapshot.ok) { setAigcVisual(false, 0); return showResult(snapshot); }
        if (snapshot.stage === "failed" || snapshot.ok === false) {
          setAigcVisual(false, 0);
          return reject(new Error(snapshot.error || "处理失败"));
        }
        if (snapshot.elapsed_ms > 125000) return reject(new Error("处理超时"));
        state.timer = setTimeout(poll, 320);
      };
      poll();
    });
  }

  function showResult(snapshot) {
    setAigcVisual(false, 0);
    /* 编号是给参会者看的，取图一律用令牌：编号连号（SM-2026-0001…），
       挂上公网谁都能顺着数下去；令牌是 22 位随机串，猜不出来。 */
    const id = snapshot.record_id;
    const token = snapshot.token;
    el.shot.src = "/api/photo/" + token + ".jpg?v=" + Date.now();
    el.serial.textContent = String(id || "").match(/(\d+)$/)?.[1] || "—";
    $("#result-portrait-mode").textContent = window.Booth.portraitModeLabel(snapshot.portrait_mode);
    el.qr.src = "/api/qr/" + token + ".png";
    el.statInfer.textContent = fmtMs(snapshot.infer_ms);
    el.statEp.textContent = snapshot.degraded ? "L0 降级" : (snapshot.ep ? "EP SpaceMIT" : "CPU");
    el.statTotal.textContent = fmtMs(snapshot.total_ms);
    el.statPersons.textContent = snapshot.person_count + " 人";
    el.download.href = "/api/photo/" + token + ".jpg";
    el.download.setAttribute("download", id + ".jpg");
    el.copy.dataset.serial = el.serial.textContent;
    el.result.classList.add("is-open");
    el.result.setAttribute("aria-hidden", "false");
    el.again.focus({ preventScroll: true });
    setMode("done");
    el.fire.classList.remove("busy");
    setTimeout(() => { setMode("idle"); refreshFireState(); }, 400);
    if (snapshot.degraded) toast("本次走了降级路径，成片已生成（" + (snapshot.degraded_reason || "") + "）", 4200);
  }

  el.fire.addEventListener("click", capture);
  /* 成片卡关掉 = 这一轮结束，可以把「待刷新」放行了 */
  el.again.addEventListener("click", () => {
    if (state.mode === "working") return;
    resumePreview();
    el.result.classList.remove("is-open");
    el.result.setAttribute("aria-hidden", "true");
    buildGuard.hold(false);
    el.fire.focus({ preventScroll: true });
    refreshFireState();
  });
  el.copy.addEventListener("click", async () => {
    const id = el.copy.dataset.serial;
    const text = "我在 SpacemiT DevCon 2026 拍了一张开发者证件照，编号 " + id +
      "。RISC-V AI CPU 现场出图。 #SpacemiTDevCon";
    try {
      await navigator.clipboard.writeText(text);
      toast("文案已复制，粘贴即可发布");
    } catch (err) {
      toast("复制失败，请手动选择文本");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (state.mode !== "working" && el.result.classList.contains("is-open")) resumePreview();
      el.result.classList.remove("is-open");
      el.result.setAttribute("aria-hidden", "true");
      buildGuard.hold(false);
      if (el.legal.classList.contains("is-open")) dismissLegal();
    }
    const target = event.target;
    const isTyping = target && (target.matches("input, textarea, select, button, a") || target.isContentEditable);
    if (event.key === " " && !isTyping && state.mode === "idle" && state.consented && !el.fire.disabled) {
      event.preventDefault();
      capture();
    }
  });

  /* ---- 起跑 ---- */
  /* 画面停了却什么都不说，参会者只会以为是相机坏了、站在那里干等。
     把「流是活的、还是在重连」如实写在取景区上。 */
  function setCamState(state, reason) {
    if (capturePreviewUrl) state = "live";
    const stalled = state !== "live";
    el.stage.classList.toggle("is-offline", stalled);
    el.stage.classList.toggle("is-mock", state === "mock");
    if (!el.camState) return;
    el.camState.textContent = state === "mock"
      ? "当前为开发预览，未连接真实相机"
      : stalled
      ? (reason === "no-frame" ? "相机重连中…（收不到画面）" : "相机重连中…（画面卡住）")
      : "";
  }

  /* 成片图裂掉时页面上只有一行 alt 文本，现场没人能从一张裂图里读出原因。
     失败一定要说出来，并且点一下就刷新（多半是手上这份页面旧了，见 net.js 坑 38）。 */
  el.shot.addEventListener("error", () => {
    if (el.shot.getAttribute("src")) notice("成片图没取到 · 点这里刷新页面");
  });
  el.qr.addEventListener("error", () => {
    if (el.qr.getAttribute("src")) notice("取图二维码没取到 · 点这里刷新页面");
  });

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
    if (el.stage.classList.contains("is-mock")) setCamState("mock");
    else setCamState("live");
  });
  /* 断了要自己接回来，别指望现场的同事发现屏幕黑了再去刷新 */
  resumePreview();
  pollState();
  setInterval(pollState, 500);
  refreshFireState();
})();

