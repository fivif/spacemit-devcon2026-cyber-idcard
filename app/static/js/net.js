/* net.js —— 四页共用的最小工具集（无框架、无 CDN，现场离线可用） */
(function (global) {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  async function request(path, options) {
    const opts = Object.assign({ method: "GET" }, options || {});
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body !== undefined && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    opts.headers = headers;
    const response = await fetch(path, opts);
    let payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok || (payload && payload.ok === false && response.status >= 400)) {
      const detail = (payload && (payload.detail || payload.error)) || ("HTTP " + response.status);
      const error = new Error(detail);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  const api = {
    get: (path) => request(path),
    post: (path, body, headers) => request(path, { method: "POST", body: body || {}, headers }),
    del: (path) => request(path, { method: "DELETE" }),
  };

  let toastTimer = null;
  function toast(message, ms) {
    let node = $("#toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "toast";
      node.className = "toast";
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      node.setAttribute("aria-atomic", "true");
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.classList.add("is-open");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("is-open"), ms || 2600);
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined || ms <= 0) return "—";
    return ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : Math.round(ms) + " ms";
  }

  function reducedMotion() {
    return global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /* 真实 RISC-V 助记符（与 app/render/card.py 的指令库一致，不用编造的名字） */
  const RISCV_MNEMONICS = [
    "add", "addi", "sub", "and", "or", "xor", "slli", "srli", "srai", "lui",
    "ld", "sd", "lw", "sw", "beq", "bne", "jal", "jalr", "ecall", "fence",
    "mul", "div", "rem", "csrr", "csrw", "mret", "wfi", "sfence.vma",
    "vsetvli", "vle8.v", "vadd.vv", "vmul.vx", "vfmacc.vf", "vredsum.vs",
    "fadd.s", "fsqrt.d", "lr.w", "amoadd.d",
  ];
  const pickMnemonic = () => RISCV_MNEMONICS[Math.floor(Math.random() * RISCV_MNEMONICS.length)];

  /* 步骤条：大屏与拍摄位共用，只认 key/label/ms/detail */
  function renderSteps(container, activeKey, doneKeys, items) {
    container.innerHTML = "";
    items.forEach((item) => {
      const node = document.createElement("div");
      node.className = "step";
      if (doneKeys.indexOf(item.key) >= 0) node.classList.add("is-done");
      else if (item.key === activeKey) node.classList.add("is-active");
      const pip = document.createElement("span");
      pip.className = "pip";
      if (doneKeys.indexOf(item.key) >= 0) pip.textContent = "✓";
      const label = document.createElement("span");
      label.className = "grow";
      label.textContent = item.label;
      const ms = document.createElement("span");
      ms.className = "ms";
      ms.textContent = item.detail || "";
      node.appendChild(pip);
      node.appendChild(label);
      node.appendChild(ms);
      container.appendChild(node);
    });
  }

  /* 取景流守护 ------------------------------------------------------------
     MJPEG 只有在「一帧都没拿到」时才会触发 error；连接中途被掐断是不报错的，
     画面就永远停在最后一帧。现场表现是取景框一直不动（或重启服务后一直黑着），
     只能人工刷新页面 —— 无人值守的大屏不能这样。
     这里每 4 秒给画面拍一张 24x24 的指纹，连续 3 次完全一样（或压根没帧）就重新拉流，
     并把「流是活的、还是在重连」用 onState 交给页面 —— 画面停了却什么都不说，
     现场的人只会以为是相机坏了，站在那里干等。 */
  function watchStream(img, options) {
    const opts = options || {};
    const url = opts.src || "/api/preview.mjpg";
    const period = opts.period || 4000;
    const staleLimit = opts.stale || 3;
    const side = 24;
    const canvas = document.createElement("canvas");
    canvas.width = side;
    canvas.height = side;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    let mark = null, stale = 0, blank = 0, tainted = false, state = "";
    let stopped = false;

    function fingerprint() {
      ctx.drawImage(img, 0, 0, side, side);
      const data = ctx.getImageData(0, 0, side, side).data;
      let hash = 2166136261;
      for (let i = 0; i < data.length; i += 4) {
        hash = Math.imul(hash ^ data[i], 16777619);
        hash = Math.imul(hash ^ data[i + 1], 16777619);
      }
      return hash >>> 0;
    }

    function setState(next, detail) {
      if (next === state) return;
      state = next;
      if (opts.onState) opts.onState(next, detail || "");
    }

    function reopen(reason) {
      if (stopped) return;
      stale = 0; blank = 0; mark = null;
      setState("stalled", reason);
      img.src = url + "?t=" + Date.now();
      if (opts.onRestart) opts.onRestart(reason);
    }

    function beat() {
      if (stopped) return;
      if (!img.naturalWidth) {
        blank += 1;
        if (blank >= 2) reopen("no-frame");
        return;
      }
      blank = 0;
      if (tainted) return;
      let next;
      try { next = fingerprint(); } catch (err) { tainted = true; return; }
      if (next === mark) stale += 1; else { stale = 0; mark = next; }
      if (stale >= staleLimit) reopen("frozen");
    }

    /* 收到新的首帧（含重连后的）就认定流活着 */
    function onLoad() {
      if (stopped) return;
      blank = 0; stale = 0; mark = null; setState("live");
    }
    img.addEventListener("load", onLoad);
    const timer = setInterval(beat, period);
    return {
      stop: () => {
        stopped = true;
        clearInterval(timer);
        img.removeEventListener("load", onLoad);
      },
      beat, reopen,
    };
  }

  /* WebRTC/WHEP 取景播放器 ---------------------------------------------
     现场预览不再把 JPEG 帧一张张塞进 HTTP。服务端只把相机源帧送给 K3
     硬件 H.264 编码器，MediaMTX 通过同源 WHEP 协商给浏览器播放。
     只保留最新连接；断开或画面停住时自动重协商。 */
  function watchWebRTC(video, options) {
    const opts = options || {};
    const url = opts.src || "/api/preview.whep";
    const period = opts.period || 1000;
    const staleLimit = opts.stale || 3;
    const firstFrameTimeout = opts.firstFrameTimeout || 5000;
    const disconnectGrace = opts.disconnectGrace || 1200;
    const retryBase = opts.retryDelay || 800;
    let stopped = false;
    let pc = null;
    let retryTimer = null;
    let beatTimer = null;
    let firstFrameTimer = null;
    let disconnectTimer = null;
    let frameCallback = null;
    let framePeer = null;
    let retryDelay = retryBase;
    let lastTime = -1;
    let stale = 0;
    let frameSeen = false;
    let state = "";

    function setState(next, detail) {
      if (next === state && !detail) return;
      state = next;
      if (opts.onState) opts.onState(next, detail || "");
    }

    function waitForIce(peer) {
      if (peer.iceGatheringState === "complete") return Promise.resolve();
      return new Promise((resolve) => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          peer.removeEventListener("icegatheringstatechange", finish);
          resolve();
        };
        peer.addEventListener("icegatheringstatechange", () => {
          if (peer.iceGatheringState === "complete") finish();
        });
        window.setTimeout(finish, 1800);
      });
    }

    function cancelFrameMonitor() {
      if (frameCallback != null && typeof video.cancelVideoFrameCallback === "function") {
        try { video.cancelVideoFrameCallback(frameCallback); } catch (err) {}
      }
      frameCallback = null;
      framePeer = null;
    }

    function clearConnectionTimers() {
      if (firstFrameTimer) window.clearTimeout(firstFrameTimer);
      if (disconnectTimer) window.clearTimeout(disconnectTimer);
      firstFrameTimer = null;
      disconnectTimer = null;
    }

    function closePeer(clearVideo) {
      clearConnectionTimers();
      cancelFrameMonitor();
      if (pc) {
        pc.ontrack = null;
        pc.onconnectionstatechange = null;
        try { pc.close(); } catch (err) {}
        pc = null;
      }
      // 重连时保留 video 的最后一帧，避免网络抖动造成黑屏；只有 stop() 才清理。
      if (clearVideo) {
        video.pause();
        video.srcObject = null;
        video.classList.remove("is-visible");
      }
    }

    function scheduleRetry(reason) {
      if (stopped || retryTimer) return;
      closePeer(false);
      setState("stalled", reason || "webrtc-reconnect");
      const delay = retryDelay;
      retryDelay = Math.min(5000, Math.round(retryDelay * 1.6));
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        void connect();
      }, delay);
    }

    async function connect() {
      if (stopped) return;
      if (typeof RTCPeerConnection === "undefined") {
        scheduleRetry("webrtc-unsupported");
        return;
      }
      closePeer(false);
      setState("connecting");
      const peer = new RTCPeerConnection({ iceServers: [] });
      pc = peer;
      frameSeen = false;
      lastTime = -1;
      stale = 0;
      peer.addTransceiver("video", { direction: "recvonly" });

      function markFrame() {
        if (stopped || pc !== peer || frameSeen) return;
        frameSeen = true;
        retryDelay = retryBase;
        lastTime = video.currentTime;
        stale = 0;
        if (firstFrameTimer) window.clearTimeout(firstFrameTimer);
        firstFrameTimer = null;
        video.classList.add("is-visible");
        setState("live");
      }

      function monitorFrames() {
        if (stopped || pc !== peer || !video.srcObject) return;
        if (typeof video.requestVideoFrameCallback === "function") {
          framePeer = peer;
          const onFrame = () => {
            if (stopped || pc !== peer || framePeer !== peer) return;
            markFrame();
            frameCallback = video.requestVideoFrameCallback(onFrame);
          };
          frameCallback = video.requestVideoFrameCallback(onFrame);
        } else {
          const onPlaying = () => {
            if (pc === peer && video.readyState >= 2 && video.videoWidth > 0) markFrame();
          };
          video.addEventListener("playing", onPlaying, { once: true });
          video.addEventListener("loadeddata", onPlaying, { once: true });
          void video.play().then(onPlaying).catch(() => {});
        }
      }

      peer.ontrack = (event) => {
        if (stopped || pc !== peer) return;
        const stream = event.streams && event.streams[0];
        if (!stream) return;
        video.srcObject = stream;
        setState("connecting", "等待首帧");
        monitorFrames();
        firstFrameTimer = window.setTimeout(() => {
          if (!frameSeen && pc === peer) scheduleRetry("webrtc-first-frame-timeout");
        }, firstFrameTimeout);
      };
      peer.onconnectionstatechange = () => {
        if (pc !== peer) return;
        if (["failed", "closed"].includes(peer.connectionState)) {
          scheduleRetry("webrtc-" + peer.connectionState);
        } else if (peer.connectionState === "disconnected") {
          // disconnected 常是短暂网络抖动，给当前连接留一点恢复时间。
          if (!disconnectTimer) {
            disconnectTimer = window.setTimeout(() => {
              disconnectTimer = null;
              if (pc === peer && peer.connectionState === "disconnected") {
                scheduleRetry("webrtc-disconnected");
              }
            }, disconnectGrace);
          }
        } else if (peer.connectionState === "connected" && disconnectTimer) {
          window.clearTimeout(disconnectTimer);
          disconnectTimer = null;
        }
      };
      try {
        const offer = await peer.createOffer();
        if (stopped || pc !== peer) return;
        await peer.setLocalDescription(offer);
        await waitForIce(peer);
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/sdp", "Accept": "application/sdp" },
          body: peer.localDescription && peer.localDescription.sdp,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("WHEP " + response.status);
        const answer = await response.text();
        await peer.setRemoteDescription({ type: "answer", sdp: answer });
      } catch (err) {
        if (pc === peer) scheduleRetry(err && err.message ? err.message : "webrtc-error");
      }
    }

    function beat() {
      if (stopped || !pc || !video.srcObject) return;
      if (video.readyState < 2 || !video.videoWidth) {
        stale += 1;
      } else if (video.currentTime === lastTime) {
        stale += 1;
      } else {
        lastTime = video.currentTime;
        stale = 0;
      }
      if (stale >= staleLimit) scheduleRetry("frozen");
    }

    beatTimer = window.setInterval(beat, period);
    void connect();
    return {
      stop: () => {
        stopped = true;
        if (retryTimer) window.clearTimeout(retryTimer);
        if (beatTimer) window.clearInterval(beatTimer);
        retryTimer = null;
        beatTimer = null;
        closePeer(true);
      },
      reopen: (reason) => {
        if (!stopped) scheduleRetry(reason || "manual");
      },
      beat,
    };
  }

  function setCookieValue(name, value) {
    document.cookie = name + "=" + encodeURIComponent(value) + "; path=/; SameSite=Lax";
  }
  function getCookieValue(name) {
    const hit = document.cookie.split("; ").find((row) => row.startsWith(name + "="));
    return hit ? decodeURIComponent(hit.split("=").slice(1).join("=")) : "";
  }

  /* 页面版本看门狗 --------------------------------------------------------
     坑 38：取图入口从编号改成令牌之后，现场那些**一直开着**的页面还在跑老 JS，
     继续用编号拼 /api/photo/SM-2026-0001.jpg —— 一律 404。而编号、耗时、人像数
     是 JSON 拿的，照常显示，所以现场看到的只是「图没出来」，没人知道该刷新。
     HTML 里的 __BUILD__ 由服务端替换成当前页面版本（语义版本 + static/ 指纹），
     这里每 20 秒对一次 /api/build，对不上就自己刷新。
     手机取图页用 <meta name="build-guard" content="manual"> 关掉：扫码的人可能
     正停在系统分享面板上，不该被刷新打断。 */
  const BUILD = (document.querySelector('meta[name="build"]') || { content: "" }).content;
  let holdReload = false;

  function notice(text) {
    let node = $("#build-notice");
    if (!node) {
      node = document.createElement("div");
      node.id = "build-notice";
      node.setAttribute("role", "status");
      node.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:2147483647;" +
        "padding:10px 16px;text-align:center;cursor:pointer;" +
        "font:600 14px/1.5 system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;" +
        "background:#c9f227;color:#11160a;box-shadow:0 2px 12px rgba(0,0,0,.35)";
      node.addEventListener("click", () => global.location.reload());
      document.body.appendChild(node);
    }
    node.textContent = text;
    return node;
  }

  function watchBuild() {
    const guard = document.querySelector('meta[name="build-guard"]');
    const auto = BUILD && (!guard || guard.content === "auto");
    let reloading = false;
    async function tick() {
      if (reloading) return;
      let info = null;
      try { info = await request("/api/build"); } catch (err) { return; }
      if (!info || !info.build || info.build === BUILD) return;
      if (holdReload) {
        notice("装置程序已更新 · 本次拍摄结束后自动刷新");
        return;
      }
      reloading = true;
      notice("装置程序已更新 · 正在刷新页面…");
      setTimeout(() => global.location.reload(), 1500);
    }
    if (!auto) return { stop: () => {}, tick, hold: () => {} };
    const timer = setInterval(tick, 20000);
    setTimeout(tick, 2000);
    return { stop: () => clearInterval(timer), tick, hold: (value) => { holdReload = !!value; } };
  }

  const buildGuard = watchBuild();

  /* 待机轮播（大屏 /stage 与竖屏 /vertical 共用）---------------------------
     以前 stage.js 和 vertical.js 各抄了一份**硬编码 4 个样张**的实现：加一种
     风格要改两个文件，漏一个就两个屏轮的东西不一样。
     现在统一走风格目录：向 /api/aigc/styles 要当前上架的每一种风格，按
     `<id>-aigc-thumb.png` 拼样张地址，逐张探测哪些真的存在。
     拿不到的（例如 main-film-chip 就没有自己的样张）**当破图丢掉** ——
     不给别名映射，否则轮播里会连着出现两张一模一样的图。
     目录取不到才退回下面这份兜底清单；一张都拿不到就把整层隐藏，
     绝不显示占位图假装有内容。 */
  const CAROUSEL_FALLBACK = [
    { id: "hand-painted-animation", label: "晴空手绘动画" },
    { id: "american-comic", label: "高能漫画封面" },
    { id: "hand-doodle", label: "开发者手账速写" },
    { id: "professional-suit", label: "未来职业肖像" },
    { id: "chinese-elegance", label: "东方新潮国风" },
    { id: "pixel-person", label: "复古像素头像" },
  ];

  const STYLE_PREVIEW_VERSION = "20261016-generated-reference-12-v3";

  function versionedAssetUrl(url) {
    if (typeof url !== "string" || !url.startsWith("/static/")) return url;
    const hashIndex = url.indexOf("#");
    const fragment = hashIndex < 0 ? "" : url.slice(hashIndex);
    const source = hashIndex < 0 ? url : url.slice(0, hashIndex);
    const queryIndex = source.indexOf("?");
    const pathname = queryIndex < 0 ? source : source.slice(0, queryIndex);
    const isStylePreview = /^\/static\/assets\/styles\/[^/]+-preview\.jpg$/.test(pathname);
    if (!BUILD && !isStylePreview) return url;
    const params = new URLSearchParams(queryIndex < 0 ? "" : source.slice(queryIndex + 1));
    if (BUILD) params.set("v", BUILD);
    if (isStylePreview) params.set("preview_v", STYLE_PREVIEW_VERSION);
    return pathname + "?" + params.toString() + fragment;
  }

  function styleThumb(styleId, mode) {
    const suffix = normalizePortraitMode(mode) === "chibi" ? "-chibi-preview.jpg" : "-preview.jpg";
    return versionedAssetUrl("/static/assets/styles/" + encodeURIComponent(styleId) + suffix);
  }

  function probeImages(candidates) {
    return Promise.all(candidates.map((item) => new Promise((resolve) => {
      const probe = new Image();
      probe.onload = () => resolve(item);
      probe.onerror = () => resolve(null);
      probe.src = item.src;
    }))).then((rows) => rows.filter(Boolean));
  }

  /* 返回 Promise<可用的轮播项数组>：[{src, label}] */
  function carouselItems() {
    return api.get("/api/aigc/styles")
      .then((data) => {
        const styles = (data && data.styles) || [];
        const wanted = CAROUSEL_FALLBACK.map((item) => [item.id, item.label]);
        const byId = new Map(styles.filter((s) => s && s.id).map((s) => [s.id, s]));
        const items = wanted
          .flatMap(([id, label]) => {
            const style = byId.get(id);
            if (!style) return [];
            return ["standard", "chibi"].map((portraitMode) => ({
              id,
              portrait_mode: portraitMode,
              src: styleThumb(id, portraitMode),
              label: label + (portraitMode === "chibi" ? " · Q版" : " · 写实版"),
              style,
            }));
          });
        if (!items.length) throw new Error("风格目录为空");
        return items;
      })
      .catch(() => CAROUSEL_FALLBACK.flatMap((style) =>
        ["standard", "chibi"].map((portraitMode) => ({
          id: style.id,
          portrait_mode: portraitMode,
          src: styleThumb(style.id, portraitMode),
          label: style.label + (portraitMode === "chibi" ? " · Q版" : " · 写实版"),
        }))))
      .then((candidates) => probeImages(candidates));
  }

  function normalizePortraitMode(value) {
    return value === "chibi" ? "chibi" : "standard";
  }

  function portraitModeLabel(value) {
    return normalizePortraitMode(value) === "chibi" ? "Q版大头像" : "写实版";
  }

  function styleDisplay(style) {
    let title = style.title || style.label || style.id || style.name;
    let description = style.description || "";
    if ((style.id || style.name) === "pixel-person") {
      if (title === "Q版像素风" || title === "像素风") title = "复古像素头像";
      description = description.replace(/大头小身体的软萌像素小人|全身小人，大头小身体/g, "像素色块与清晰轮廓");
    }
    return { title, description };
  }

  function mountPortraitMode(container, initialMode, defaultMode) {
    let selected = normalizePortraitMode(defaultMode || "standard");
    let saved = null;
    try { saved = global.localStorage.getItem("booth.portrait_mode.v1"); }
    catch (err) { saved = null; }
    if (saved !== null) selected = normalizePortraitMode(saved);
    if (initialMode !== undefined) selected = normalizePortraitMode(initialMode);
    const inputs = container ? Array.from(container.querySelectorAll('input[type="radio"]')) : [];
    function render(value) {
      selected = normalizePortraitMode(value);
      inputs.forEach((input) => { input.checked = input.value === selected; });
    }
    inputs.forEach((input) => input.addEventListener("change", () => {
      if (!input.checked) return;
      render(input.value);
      try { global.localStorage.setItem("booth.portrait_mode.v1", selected); } catch (err) {}
      global.dispatchEvent(new CustomEvent("booth:portrait-mode", { detail: selected }));
    }));
    global.addEventListener("booth:portrait-mode", (event) => render(event.detail));
    render(selected);
    return { value: () => selected };
  }

  global.Booth = { $, $$, api, toast, fmtMs, reducedMotion, renderSteps,
                   normalizePortraitMode, portraitModeLabel, styleDisplay, mountPortraitMode,
                   RISCV_MNEMONICS, pickMnemonic, setCookieValue, getCookieValue, watchStream, watchWebRTC,
                   BUILD, STYLE_PREVIEW_VERSION, notice, buildGuard, versionedAssetUrl, styleThumb, carouselItems, probeImages };
})(window);
