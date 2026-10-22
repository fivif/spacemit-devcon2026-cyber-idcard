(function () {
  "use strict";

  function createScene() {
    const root = document.querySelector("#v-core");
    const capture = document.querySelector("#v-core-capture");
    const cutout = document.querySelector("#v-core-cutout");
    const result = document.querySelector("#v-core-result");
    const caption = document.querySelector("#v-core-caption");
    const engine = document.querySelector("#v-core-engine");
    const segmentation = document.querySelector("#v-core-segmentation");
    const particleField = document.querySelector("#v-core-particle-field");
    const contourParticles = document.querySelector("#v-core-contour-particles");
    const subjectFrame = document.querySelector("#v-core-subject-frame");
    const detail = document.querySelector("#v-core-detail");
    const style = document.querySelector("#v-core-style");
    const mode = document.querySelector("#v-core-mode");
    const elapsed = document.querySelector("#v-core-elapsed");
    const steps = Array.from(document.querySelectorAll("[data-create-step]"));
    if (!root || !capture || !cutout || !result) {
      return { update() {}, clear() {}, destroy() {}, active: false };
    }

    let shown = false;
    let disposed = false;
    let job = null;
    let generation = 0;
    let captionPhase = null;
    let styleStartedAt = null;
    let waitTimer = null;
    let currentSegmentation = null;
    let contourPoints = [];
    let contourSize = { width: 0, height: 0 };
    let fallbackContour = false;
    const slots = [
      { element: capture, className: "has-capture", url: null, loader: null },
      { element: cutout, className: "has-cutout", url: null, loader: null },
      { element: result, className: "has-result", url: null, loader: null },
    ];
    if (segmentation) slots.push({ element: segmentation, className: "has-segmentation", url: null, loader: null });

    function clear() {
      stopWaiting();
      styleStartedAt = null;
      if (elapsed) { elapsed.hidden = true; elapsed.textContent = ""; }
      generation += 1;
      shown = false;
      job = null;
      currentSegmentation = null;
      contourPoints = [];
      contourSize = { width: 0, height: 0 };
      fallbackContour = false;
      captionPhase = null;
      root.classList.remove("is-on", "has-capture", "has-cutout", "has-result", "has-segmentation");
      root.setAttribute("aria-hidden", "true");
      root.removeAttribute("data-phase");
      if (engine) engine.textContent = "";
      if (detail) detail.textContent = "";
      if (subjectFrame) {
        subjectFrame.hidden = true;
        subjectFrame.removeAttribute("style");
      }
      if (contourParticles) {
        contourParticles.replaceChildren();
        contourParticles.classList.remove("is-fallback", "is-visible");
      }
      slots.forEach((slot) => {
        if (slot.loader) {
          slot.loader.onload = null;
          slot.loader.onerror = null;
        }
        slot.loader = null;
        slot.url = null;
        if (slot.className === "has-segmentation") {
          slot.element.style.maskImage = "";
          if (particleField) {
            particleField.style.maskImage = "";
            particleField.style.maskMode = "luminance";
          }
        }
        else slot.element.removeAttribute("src");
      });
      steps.forEach((step) => {
        step.classList.remove("is-current", "is-done");
        step.removeAttribute("aria-current");
      });
    }

    function stopWaiting() {
      if (waitTimer !== null) window.clearInterval(waitTimer);
      waitTimer = null;
    }

    function updateWaiting() {
      if (styleStartedAt === null || !shown || root.classList.contains("has-result")) return;
      const seconds = Math.max(0, Math.floor((performance.now() - styleStartedAt) / 1000));
      if (elapsed) {
        elapsed.hidden = false;
        const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
        elapsed.textContent = "已等待 " + minutes + ":" + String(seconds % 60).padStart(2, "0");
      }
      setCaption(seconds >= 45 ? "style-wait" : "style",
        seconds >= 45 ? "仍在生成，请稍候" : "你的风格正在成形");
    }

    function syncVisibility() {
      root.classList.toggle("is-paused", Boolean(document.hidden));
      stopWaiting();
      if (!document.hidden && shown && styleStartedAt !== null && !root.classList.contains("has-result")) {
        updateWaiting();
        waitTimer = window.setInterval(updateWaiting, 1000);
      }
    }

    document.addEventListener("visibilitychange", syncVisibility);

    function setCaption(key, message) {
      if (captionPhase === key) return;
      captionPhase = key;
      caption.setAttribute("aria-label", message);
      const text = document.createElement("span");
      text.className = "vcore-message";
      text.textContent = message;
      text.setAttribute("aria-hidden", "true");
      caption.replaceChildren(text);
    }

    function load(slot, url) {
      if (!url || slot.url === url) return;
      slot.url = url;
      const revision = generation;
      const loader = new Image();
      slot.loader = loader;
      loader.decoding = "async";
      loader.onload = async () => {
        try {
          if (loader.decode) await loader.decode();
        } catch (error) {
          if (revision === generation && slot.loader === loader) {
            slot.url = null;
            slot.loader = null;
          }
          return;
        }
        if (!shown || disposed || revision !== generation || slot.loader !== loader) return;
        if (slot.className === "has-segmentation") {
          const maskImage = 'url(' + JSON.stringify(url) + ')';
          slot.element.style.maskImage = maskImage;
          if (particleField) {
            particleField.style.maskImage = maskImage;
            particleField.style.maskMode = "luminance";
          }
          buildContourParticles(loader);
        }
        else slot.element.src = url;
        if (slot.className === "has-cutout" && !root.classList.contains("has-segmentation")) {
          if (particleField) {
            particleField.style.maskImage = 'url(' + JSON.stringify(url) + ')';
            particleField.style.maskMode = "alpha";
          }
          buildContourParticles(loader, true);
        }
        root.classList.add(slot.className);
        if (slot.className === "has-cutout" && root.dataset.phase === "matte") {
          setCaption("matte-ready", "人像已分离");
        }
        if (slot.className === "has-result") {
          stopWaiting();
          if (elapsed) elapsed.hidden = true;
          setCaption("finish", "正在完成照片");
        }
        slot.loader = null;
      };
      loader.onerror = () => {
        if (revision !== generation || slot.loader !== loader) return;
        slot.url = null;
        slot.loader = null;
      };
      loader.src = url;
    }

    function buildContourParticles(image, alphaOnly = false) {
      if (!contourParticles || !image.naturalWidth || !image.naturalHeight) return;
      const canvas = document.createElement("canvas");
      const sampleWidth = 192;
      const sampleHeight = Math.max(1, Math.round(sampleWidth * image.naturalHeight / image.naturalWidth));
      canvas.width = sampleWidth;
      canvas.height = sampleHeight;
      const context = canvas.getContext && canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return;
      let pixels;
      try {
        context.drawImage(image, 0, 0, sampleWidth, sampleHeight);
        pixels = context.getImageData(0, 0, sampleWidth, sampleHeight).data;
      } catch (error) { return; }
      const points = [];
      for (let y = 1; y < sampleHeight - 1; y += 2) {
        for (let x = 1; x < sampleWidth - 1; x += 2) {
          const offset = (y * sampleWidth + x) * 4;
          const inside = pixels[offset + 3] > 80 && (alphaOnly || pixels[offset] > 128);
          if (!inside) continue;
          const neighbors = [
            ((y - 1) * sampleWidth + x) * 4,
            ((y + 1) * sampleWidth + x) * 4,
            (y * sampleWidth + x - 1) * 4,
            (y * sampleWidth + x + 1) * 4,
          ];
          if (neighbors.some((index) => pixels[index + 3] <= 80 || (!alphaOnly && pixels[index] <= 128))) {
            points.push({ x: x / sampleWidth, y: y / sampleHeight });
          }
        }
      }
      if (points.length < 12) return;
      fallbackContour = false;
      contourParticles.classList.remove("is-fallback");
      const step = Math.max(1, Math.ceil(points.length / 48));
      contourPoints = points.filter((point, index) => index % step === 0).slice(0, 48);
      contourParticles.replaceChildren(...contourPoints.map((point, index) => {
        const particle = document.createElement("i");
        particle.style.left = (point.x * 100).toFixed(3) + "%";
        particle.style.top = (point.y * 100).toFixed(3) + "%";
        particle.style.setProperty("--contour-delay", ((index % 9) * -0.16).toFixed(2) + "s");
        return particle;
      }));
      contourSize = { width: image.naturalWidth, height: image.naturalHeight };
      positionContourParticles();
      contourParticles.classList.add("is-visible");
    }

    function buildFallbackContourParticles() {
      if (!contourParticles || contourPoints.length) return;
      fallbackContour = true;
      contourParticles.classList.add("is-fallback", "is-visible");
      const points = [];
      const count = 44;
      for (let index = 0; index < count; index += 1) {
        const angle = -Math.PI / 2 + (index / count) * Math.PI * 2;
        points.push({
          x: 0.5 + Math.cos(angle) * 0.24,
          y: 0.43 + Math.sin(angle) * 0.38,
        });
      }
      contourPoints = points;
      contourParticles.replaceChildren(...points.map((point, index) => {
        const particle = document.createElement("i");
        particle.style.left = (point.x * 100).toFixed(3) + "%";
        particle.style.top = (point.y * 100).toFixed(3) + "%";
        particle.style.setProperty("--contour-delay", ((index % 14) * -0.09).toFixed(2) + "s");
        return particle;
      }));
    }

    function positionContourParticles() {
      if (!contourParticles || !contourPoints.length || !contourSize.width || !root.getBoundingClientRect) return;
      const rect = root.getBoundingClientRect();
      if (!(rect.width > 0 && rect.height > 0)) return;
      const scale = Math.max(rect.width / contourSize.width, rect.height / contourSize.height);
      const offsetX = (rect.width - contourSize.width * scale) / 2;
      const offsetY = (rect.height - contourSize.height * scale) / 2;
      [...contourParticles.children].forEach((particle, index) => {
        const point = contourPoints[index];
        particle.style.left = (((point.x * contourSize.width * scale + offsetX) / rect.width) * 100).toFixed(3) + "%";
        particle.style.top = (((point.y * contourSize.height * scale + offsetY) / rect.height) * 100).toFixed(3) + "%";
      });
    }

    function syncSubjectFrame() {
      if (!subjectFrame || !currentSegmentation || !Array.isArray(currentSegmentation.box)) return;
      if (root.dataset.phase !== "matte" || root.classList.contains("has-cutout")) {
        subjectFrame.hidden = true;
        return;
      }
      const values = currentSegmentation.box.map(Number);
      const width = Number(currentSegmentation.width);
      const height = Number(currentSegmentation.height);
      if (values.length < 4 || !values.every(Number.isFinite) || !(width > 0 && height > 0)) return;
      const rect = root.getBoundingClientRect();
      if (!(rect.width > 0 && rect.height > 0)) return;
      const scale = Math.max(rect.width / width, rect.height / height);
      const renderedWidth = width * scale;
      const renderedHeight = height * scale;
      const offsetX = (rect.width - renderedWidth) / 2;
      const offsetY = (rect.height - renderedHeight) / 2;
      const left = ((rect.width - values[2] * scale - offsetX) / rect.width) * 100;
      const top = ((values[1] * scale + offsetY) / rect.height) * 100;
      const frameWidth = (((values[2] - values[0]) * scale) / rect.width) * 100;
      const frameHeight = (((values[3] - values[1]) * scale) / rect.height) * 100;
      subjectFrame.style.left = left + "%";
      subjectFrame.style.top = top + "%";
      subjectFrame.style.width = frameWidth + "%";
      subjectFrame.style.height = frameHeight + "%";
      subjectFrame.hidden = false;
    }

    function handleResize() {
      syncSubjectFrame();
      if (!fallbackContour) positionContourParticles();
    }

    if (window.addEventListener) window.addEventListener("resize", handleResize);

    function update(active) {
      if (disposed) return;
      if (!active || ["countdown", "failed", "done"].includes(active.stage)) {
        clear();
        return;
      }
      const identity = active.job || active.record_id || active.capture_preview_url;
      if (job !== identity) clear();
      job = identity;
      shown = true;
      const phase = ["processing", "capturing"].includes(active.stage) ? "capture"
         : ["inferring", "matting"].includes(active.stage) ? "matte"
         : active.stage === "stylizing" ? "style" : "finish";
      const phaseChanged = root.dataset.phase !== phase;
      root.dataset.phase = phase;
      root.classList.add("is-on");
      root.setAttribute("aria-hidden", "false");
      const matteReady = phase === "matte" && root.classList.contains("has-cutout");
      const message = matteReady ? "人像已分离" : ({
        capture: "正在定格这一刻",
        matte: active.stage === "matting" ? "正在精修人物边缘" : "K3 正在定位人像",
        style: "你的风格正在成形",
        finish: "正在完成照片",
      }[phase]);
      if (phase !== "style" || root.classList.contains("has-result")) {
        stopWaiting();
        styleStartedAt = null;
        if (elapsed) elapsed.hidden = true;
        setCaption(root.classList.contains("has-result") ? "finish" : matteReady ? "matte-ready" : active.stage,
          root.classList.contains("has-result") ? "正在完成照片" : message);
      } else if (phaseChanged || styleStartedAt === null) {
        styleStartedAt = performance.now();
        syncVisibility();
      }
      style.textContent = active.style_label || "";
      mode.textContent = active.portrait_mode === "chibi" ? "Q版大头像" : "写实版";
      if (engine) {
        engine.textContent = phase === "matte"
          ? (active.segmentation ? (active.segmentation.ep ? "K3 AI 核 · 轮廓已定位" : "K3 · 轮廓已定位") : "K3 · 人像分离")
          : phase === "style" ? "AIGC 风格生成" : "";
      }
      currentSegmentation = active.segmentation || null;
      if (phase === "matte" || phase === "style") {
        // 掩码是异步资产，不能让它决定粒子层是否存在；先给用户稳定的
        // K3 粒子环，真实掩码到位后再用轮廓替换它。
        buildFallbackContourParticles();
      }
      if (subjectFrame) {
        subjectFrame.hidden = phase !== "matte" || !currentSegmentation || !currentSegmentation.box;
      }
      syncSubjectFrame();
      if (detail) {
        detail.textContent = phase === "capture" ? "读取刚刚定格的照片"
          : phase === "matte" ? (matteReady ? "背景已移除，保留真实人物轮廓"
            : active.stage === "matting" ? "光粒子正在覆盖已选中的人像 · 正在保留发丝与衣服边缘"
            : "先找到拍摄主体，再分离人物与背景")
          : phase === "style" && active.presentation_mode === "handoff" && active.matte_applied
            ? "拍摄已结束 · 可以先逛展，稍后回来取图打印"
          : phase === "style" ? "拍摄已结束 · 正在绘制你选择的风格"
          : "正在合成人像、版式与大会信息";
      }
      const current = phase === "capture" || phase === "matte" ? 0 : phase === "style" ? 1 : 2;
      steps.forEach((step, index) => {
        step.classList.toggle("is-current", index === current);
        step.classList.toggle("is-done", index < current);
        if (index === current) step.setAttribute("aria-current", "step");
        else step.removeAttribute("aria-current");
      });
      load(slots[0], active.capture_preview_url);
      load(slots[1], active.cutout_url);
      load(slots[2], active.style_preview_url);
      if (slots[3]) load(slots[3], active.segmentation && active.segmentation.mask_url);
    }

    function destroy() {
      clear();
      disposed = true;
      document.removeEventListener("visibilitychange", syncVisibility);
      if (window.removeEventListener) window.removeEventListener("resize", handleResize);
    }

    return { update, clear, destroy, get active() { return shown; } };
  }

  window.BoothCreateCore = createScene;
})();
