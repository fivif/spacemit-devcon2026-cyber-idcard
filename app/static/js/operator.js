
(function () {
  "use strict";
  const { $, api, toast, fmtMs, renderSteps } = window.Booth;
  if (window.AutoFlowConsent) $("#operator-notice").textContent = window.AutoFlowConsent.noticeText();

  const pages = {
    overview: ["总览", "查看现场状态，快速进入常用管理页。"],
    queue: ["排队管理", "查看当前排队，处理尚未开始拍摄的参与者。"],
    photos: ["成片管理", "查看最近成片与取图链接。"],
    styles: ["风格管理", "管理参与者可选的画风与默认风格。"],
    models: ["生图渠道", "配置统一模型，多个可用渠道同时竞速生成。"],
    settings: ["设置", "维护取图地址、生成设置与现场告知。"],
    system: ["设备与维护", "查看设备状态，处理现场异常。"],
  };
  const path = window.location.pathname.replace(/\/+$/, "");
  const route = path === "/operator" ? "overview" : path.split("/").pop();
  const page = Object.prototype.hasOwnProperty.call(pages, route) ? route : "overview";
  document.querySelectorAll("[data-page]").forEach((section) => {
    section.hidden = section.dataset.page !== page;
  });
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    if (link.dataset.pageLink === page) link.setAttribute("aria-current", "page");
  });
  $("#page-title").textContent = pages[page][0];
  $("#page-description").textContent = pages[page][1];
  document.title = pages[page][0] + " · SpacemiT 现场管理";

  function pageError(message) {
    const node = $("#page-error");
    node.textContent = message || "";
    node.hidden = !message;
  }

  function feedback(node, message, failed) {
    node.textContent = message;
    node.classList.toggle("is-error", Boolean(failed));
  }

  function assertResult(result) {
    if (result && result.ok === false) throw new Error(result.detail || result.error || "操作未完成");
    return result;
  }

  const STEPS = [
    { key: "capture", label: "抓帧" },
    { key: "infer", label: "人像分割" },
    { key: "render", label: "风格化合成" },
    { key: "store", label: "入库编号" },
  ];

  const el = {
    kvCamera: $("#kv-camera"), kvInfer: $("#kv-infer"), kvStore: $("#kv-store"),
    kvLast: $("#kv-last"), kvDegraded: $("#kv-degraded"), steps: $("#steps"),
    shots: $("#shots"), shotsCount: $("#shots-count"), badgeEp: $("#badge-ep"),
    restore: $("#restore"), purge: $("#purge"), reset: $("#reset"),
    usbReset: $("#usb-reset"),
    opResult: $("#op-result"), build: $("#build-note"),
    cfgBase: $("#cfg-base"), cfgSave: $("#cfg-save"), cfgTest: $("#cfg-test"),
    cfgClear: $("#cfg-clear"), cfgBadge: $("#cfg-badge"), kvConfig: $("#kv-config"),
    qrCheck: $("#qr-check"), cfgResult: $("#cfg-result"),
    cfgHandoff: $("#cfg-handoff"), cfgPresentationBadge: $("#cfg-presentation-badge"),
    cfgPresentationSave: $("#cfg-presentation-save"), cfgPresentationResult: $("#cfg-presentation-result"),
    aigcBadge: $("#aigc-badge"), aigcStyles: $("#aigc-styles"),
    aigcAdd: $("#aigc-add"), aigcResult: $("#aigc-result"),
    channels: $("#aigc-channels"), channelCount: $("#channel-count"),
    channelAdd: $("#channel-add"), channelCheck: $("#channel-check"),
    channelSave: $("#channel-save"), channelResult: $("#channel-result"),
    channelReload: $("#channel-reload"), channelDirty: $("#channel-dirty"),
    channelActive: $("#channel-active"),
    queueAdminCount: $("#queue-admin-count"), queueAdminNote: $("#queue-admin-note"),
    queueAdminList: $("#queue-admin-list"),
    uploadForm: $("#operator-upload-form"), uploadImage: $("#upload-image"),
    uploadMode: $("#upload-mode"), uploadStyle: $("#upload-style"),
    uploadSubmit: $("#upload-submit"), uploadResult: $("#upload-result"),
    uploadBadge: $("#upload-badge"), uploadPreview: $("#upload-preview"),
    regeneratePanel: $("#regenerate-panel"), regenerateForm: $("#regenerate-form"),
    regenerateStyle: $("#regenerate-style"), regeneratePortrait: $("#regenerate-portrait"),
    regenerateWatermark: $("#regenerate-ai-watermark"), regenerateSubmit: $("#regenerate-submit"),
    regenerateCancel: $("#regenerate-cancel"), regenerateReload: $("#regenerate-reload"),
    regenerateBadge: $("#regenerate-badge"), regenerateResult: $("#regenerate-result"),
  };

  let uploadBusy = false;
  let uploadStylesReady = false;
  let uploadPreviewUrl = "";
  let uploadTimer = null;
  let uploadJob = "";
  let uploadChecking = false;
  const UPLOAD_STORAGE = "booth-operator-upload-job";
  const UPLOAD_STAGES = { queued: "等待处理", capturing: "读取图片", inferring: "分离人像",
    matting: "精细抠像", stylizing: "生成风格", rendering: "合成成片", storing: "保存成片" };
  const REGENERATE_STORAGE = "booth-operator-regenerate-job";
  let regeneratePhoto = null;
  let regenerateBusy = false;
  let regenerateJob = "";
  let regenerateTimer = null;
  let regenerateChecking = false;
  let regenerateTrigger = null;
  let recentPhotos = [];

  function canRegenerate(item) {
    if (!item || !item.token || item.deleted || item.tombstone || item.expired ||
        item.extra && item.extra.tombstone || item.capture_available === false || item.regenerate_available === false) return false;
    if (item.expires_at != null) {
      const expiry = typeof item.expires_at === "number" ? item.expires_at * 1000 : Date.parse(item.expires_at);
      if (!Number.isFinite(expiry) || expiry <= Date.now()) return false;
    }
    return true;
  }

  function regenerateControls() {
    el.regenerateForm.querySelectorAll("input, select").forEach((node) => { node.disabled = regenerateBusy; });
    el.regenerateStyle.disabled = regenerateBusy || !uploadStylesReady;
    el.regenerateSubmit.disabled = regenerateBusy || !uploadStylesReady || !canRegenerate(regeneratePhoto);
    el.regenerateSubmit.textContent = regenerateBusy ? "生成中…" : "重新生成";
    el.regenerateCancel.disabled = regenerateBusy;
    el.regenerateReload.hidden = uploadStylesReady;
    el.regenerateReload.disabled = regenerateBusy;
    el.regenerateForm.setAttribute("aria-busy", String(regenerateBusy));
    el.shots.querySelectorAll("[data-regenerate]").forEach((button) => {
      button.disabled = regenerateBusy;
      button.textContent = regenerateBusy && regeneratePhoto && button.dataset.regenerate === regeneratePhoto.token
        ? "生成中…" : "重新生成";
    });
  }

  function openRegenerate(item, trigger) {
    if (regenerateBusy || !canRegenerate(item)) return;
    regeneratePhoto = item;
    regenerateTrigger = trigger;
    el.regeneratePanel.hidden = false;
    $("#regenerate-source").textContent = "原片：" + (String(item.id || "").match(/(\d+)$/)?.[1] || "—");
    const style = Array.from(el.regenerateStyle.options).find((option) => option.value === item.style);
    el.regenerateStyle.value = style ? style.value : el.uploadStyle.value;
    el.regeneratePortrait.value = item.portrait_mode === "chibi" ? "chibi" : "standard";
    el.regenerateWatermark.checked = true;
    el.regenerateBadge.textContent = "待提交";
    feedback(el.regenerateResult, uploadStylesReady ? "" : "风格尚未读取，请重试读取风格。", !uploadStylesReady);
    regenerateControls();
    el.regeneratePanel.scrollIntoView({ block: "nearest" });
    (uploadStylesReady ? el.regenerateStyle : el.regenerateReload).focus();
  }

  function rememberRegenerate() {
    try {
      if (regenerateJob) sessionStorage.setItem(REGENERATE_STORAGE, JSON.stringify({ job: regenerateJob, photo: regeneratePhoto,
        style: el.regenerateStyle.value, portrait_mode: el.regeneratePortrait.value, ai_watermark: el.regenerateWatermark.checked }));
      else sessionStorage.removeItem(REGENERATE_STORAGE);
    } catch (err) {}
  }

  function readPhotoJob(jobId) {
    return api.get("/api/job/" + encodeURIComponent(jobId));
  }

  async function checkRegenerate() {
    if (!regenerateJob || regenerateChecking || document.hidden) return;
    clearTimeout(regenerateTimer);
    regenerateChecking = true;
    try {
      const job = await readPhotoJob(regenerateJob);
      if (!job || !job.stage) throw new Error("处理状态暂时无法读取");
      if (job.stage === "done" || job.stage === "failed") {
        const failed = job.stage === "failed" || job.ok === false;
        regenerateJob = "";
        regenerateBusy = false;
        rememberRegenerate();
        el.regenerateBadge.textContent = failed ? "未完成" : job.degraded ? "备用处理" : "已完成";
        feedback(el.regenerateResult, failed
          ? "重新生成未完成：" + (job.error || "请重试") + "。原片保留，可再次重新生成。"
          : job.degraded ? "风格生成未完成，已使用备用处理。原片保留，可再次重新生成。"
          : "新成片已保存，请在下方查看。原片和已有成片保留。", failed || Boolean(job.degraded));
        regenerateControls();
        await refresh();
      } else {
        const label = UPLOAD_STAGES[job.stage] || "处理中";
        el.regenerateBadge.textContent = label;
        feedback(el.regenerateResult, label + "，完成后自动显示新成片。", false);
      }
    } catch (err) {
      if (err.status === 404) {
        regenerateJob = "";
        regenerateBusy = false;
        rememberRegenerate();
        el.regenerateBadge.textContent = "状态不可用";
        feedback(el.regenerateResult, "任务状态已失效，请先检查最近成片，再决定是否重新生成。", true);
        regenerateControls();
      } else {
        feedback(el.regenerateResult, "状态暂未读取成功，正在重试，请勿重复提交。", true);
      }
    } finally {
      regenerateChecking = false;
      if (regenerateJob && !document.hidden) regenerateTimer = setTimeout(checkRegenerate, 1500);
    }
  }

  el.regenerateCancel.addEventListener("click", () => {
    if (regenerateBusy) return;
    el.regeneratePanel.hidden = true;
    regeneratePhoto = null;
    if (regenerateTrigger && regenerateTrigger.isConnected) regenerateTrigger.focus();
  });
  el.regenerateReload.addEventListener("click", async () => {
    el.regenerateReload.disabled = true;
    await loadUploadStyles();
    feedback(el.regenerateResult, uploadStylesReady ? "风格已读取，请选择后重新生成。" : "风格读取未完成，请重试。", !uploadStylesReady);
  });
  el.regenerateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (regenerateBusy || !uploadStylesReady) return;
    const current = recentPhotos.find((item) => regeneratePhoto && item.token === regeneratePhoto.token);
    if (!canRegenerate(current)) {
      feedback(el.regenerateResult, "这张照片已失效或不可用，请刷新成片列表。", true);
      return;
    }
    const payload = { token: current.token, style: el.regenerateStyle.value,
      portrait_mode: el.regeneratePortrait.value, ai_watermark: el.regenerateWatermark.checked };
    regenerateBusy = true;
    regenerateControls();
    el.regenerateBadge.textContent = "提交中";
    feedback(el.regenerateResult, "正在提交重新生成…", false);
    try {
      const result = assertResult(await api.post("/api/operator/photos/regenerate", payload));
      regenerateJob = typeof result.job === "string" ? result.job : result.job && result.job.job;
      if (!regenerateJob) throw new Error("任务状态无法确认");
      rememberRegenerate();
      await checkRegenerate();
    } catch (err) {
      regenerateBusy = false;
      el.regenerateBadge.textContent = err.status === 409 ? "暂不可提交" : err.status === 400 ? "无法生成" : "提交未确认";
      feedback(el.regenerateResult, err.status === 409
        ? "暂时无法重新生成：" + err.message + "。请稍后重试。"
        : err.status === 400 ? "无法重新生成：" + err.message + "。请检查照片是否仍有效，并刷新成片列表。"
        : "提交未确认：" + err.message + "。请先检查最近成片，避免重复提交，再重试。", true);
      regenerateControls();
    }
  });

  function uploadControls() {
    const original = el.uploadMode.value === "original";
    $("#upload-style-wrap").hidden = original;
    $("#upload-portrait-wrap").hidden = original;
    $("#upload-mode-hint").textContent = original
      ? "保留原图背景与衣服。带标识版按所选版式排版，无标识版保留原图，可直接打印。"
      : "仅处理图片中的人像，个人信息在本机排版。";
    el.uploadForm.querySelectorAll("input, select").forEach((node) => { node.disabled = uploadBusy; });
    el.uploadStyle.disabled = uploadBusy || original || !uploadStylesReady;
    el.uploadForm.elements.portrait_mode.disabled = uploadBusy || original;
    el.uploadSubmit.disabled = uploadBusy || (!original && !uploadStylesReady);
    el.uploadSubmit.textContent = uploadBusy ? "处理中…" : "提交图片";
    el.uploadForm.setAttribute("aria-busy", String(uploadBusy));
  }

  async function loadUploadStyles() {
    try {
      const result = assertResult(await api.get("/api/operator/aigc/styles"));
      const styles = (result.styles || []).filter((style) => style.enabled !== false);
      el.uploadStyle.replaceChildren();
      styles.forEach((style) => {
        const option = document.createElement("option");
        option.value = style.id;
        option.textContent = window.Booth.styleDisplay(style).title;
        option.selected = Boolean(style.is_default);
        el.uploadStyle.appendChild(option);
      });
      const selected = el.regenerateStyle.value;
      el.regenerateStyle.replaceChildren(...Array.from(el.uploadStyle.options, (option) => option.cloneNode(true)));
      el.regenerateStyle.value = styles.some((style) => style.id === selected) ? selected : el.uploadStyle.value;
      uploadStylesReady = styles.length > 0;
      if (!uploadStylesReady) throw new Error("还没有启用的风格，可先选择原图");
    } catch (err) {
      feedback(el.uploadResult, "风格读取未完成：" + err.message, true);
    }
    uploadControls();
    regenerateControls();
  }

  function rememberUpload(jobId) {
    uploadJob = jobId;
    try {
      if (jobId) sessionStorage.setItem(UPLOAD_STORAGE, jobId);
      else sessionStorage.removeItem(UPLOAD_STORAGE);
    } catch (err) {}
  }

  async function checkUpload() {
    if (!uploadJob || uploadChecking || document.hidden) return;
    uploadChecking = true;
    try {
      const job = await readPhotoJob(uploadJob);
      if (!job || !job.stage) throw new Error("处理状态暂时无法读取");
      if (job.stage === "done" || job.stage === "failed") {
        const failed = job.stage === "failed" || job.ok === false;
        const reason = String(job.degraded_reason || "");
        let fallbackMessage = "";
        if (job.degraded) {
          if (reason.includes("infer_failed")) {
            fallbackMessage = "人像处理未完成，已保留原图，请重试。";
          } else if (reason.includes("matte_failed") || /aigc_failed:\s*人像抠像不可用/.test(reason)) {
            fallbackMessage = "抠像未完成，已保留原图，请重试。";
          } else if (/aigc_failed:\s*画面里没有检出人像/.test(reason)) {
            fallbackMessage = "未检测到人像，已保留原图，请更换图片后重试。";
          } else if (reason.includes("aigc_failed")) {
            fallbackMessage = "风格生成未完成，已保留原图。请查看下方成片。";
          }
        }
        el.uploadBadge.textContent = "更新成片";
        rememberUpload("");
        uploadBusy = false;
        uploadControls();
        await refresh();
        el.uploadBadge.textContent = failed ? "未完成" : (fallbackMessage ? "已保存原图" : "已完成");
        feedback(el.uploadResult, failed ? (job.error || "处理未完成，请重新提交") :
          (fallbackMessage || "成片已保存，可在下方下载或开始打印。"), failed || Boolean(fallbackMessage));
      } else {
        const label = UPLOAD_STAGES[job.stage] || "处理中";
        el.uploadBadge.textContent = label;
        feedback(el.uploadResult, label + "，完成后自动显示在下方。", false);
      }
    } catch (err) {
      if (err.status === 404) {
        rememberUpload("");
        uploadBusy = false;
        el.uploadBadge.textContent = "状态不可用";
        uploadControls();
        await refresh();
        feedback(el.uploadResult, "任务状态已失效，请先检查最近成片，再决定是否重新提交。", true);
      } else {
        feedback(el.uploadResult, "状态暂未读取成功，正在重试，请勿重复提交。", true);
      }
    } finally {
      uploadChecking = false;
      if (uploadJob && !document.hidden) uploadTimer = setTimeout(checkUpload, 1500);
    }
  }

  el.uploadMode.addEventListener("change", uploadControls);
  el.uploadImage.addEventListener("change", () => {
    if (uploadPreviewUrl) URL.revokeObjectURL(uploadPreviewUrl);
    uploadPreviewUrl = "";
    const file = el.uploadImage.files[0];
    el.uploadPreview.hidden = !file;
    el.uploadPreview.removeAttribute("src");
    $("#upload-file-name").textContent = file ? file.name : "支持 JPG、PNG、WEBP，单张不超过 20 MB";
    if (file) {
      uploadPreviewUrl = URL.createObjectURL(file);
      el.uploadPreview.src = uploadPreviewUrl;
    }
  });
  el.uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (uploadBusy) return;
    const file = el.uploadImage.files[0];
    if (!file || file.size > 20 * 1024 * 1024) {
      feedback(el.uploadResult, "请选择一张不超过 20 MB 的图片。", true);
      return;
    }
    const form = new FormData(el.uploadForm);
    form.set("ai_watermark", String($("#upload-ai-watermark").checked));
    uploadBusy = true;
    uploadControls();
    el.uploadBadge.textContent = "提交中";
    feedback(el.uploadResult, "正在提交图片…", false);
    try {
      const result = assertResult(await api.post("/api/operator/upload", form));
      const jobId = typeof result.job === "string" ? result.job : result.job && result.job.job;
      if (!jobId) throw new Error("任务状态无法确认，请检查最近成片");
      rememberUpload(jobId);
      await checkUpload();
    } catch (err) {
      uploadBusy = false;
      el.uploadBadge.textContent = "提交未确认";
      feedback(el.uploadResult, "提交未确认：" + err.message + "。请先检查最近成片，避免重复提交。", true);
      uploadControls();
    }
  });

  function kv(container, key, value, tone) {
    const row = document.createElement("div");
    row.className = "kv";
    const k = document.createElement("span");
    k.className = "k";
    k.textContent = key;
    const v = document.createElement("span");
    const text = value === null || value === undefined || value === "" ? "—" : String(value);
    v.className = "v" + (tone ? " " + tone : "") + (text.length > 90 ? " clamp" : "");
    v.textContent = text;
    if (text.length > 90) v.title = text;
    row.appendChild(k);
    row.appendChild(v);
    container.appendChild(row);
  }

  function fill(container, rows) {
    container.innerHTML = "";
    rows.forEach((row) => kv(container, row[0], row[1], row[2]));
  }

  async function printFromOperator(item, variant, button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "提交中…";
    const random = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID() : (Date.now() + "-" + Math.random().toString(16).slice(2));
    try {
      const data = assertResult(await api.post("/api/operator/print", {
        photo_token: item.token,
        variant,
        idempotency_key: "operator:" + item.token + ":" + random,
      }));
      const jobId = data && data.print_job && data.print_job.print_job_id;
      if (!jobId) throw new Error("打印任务未返回");
      button.textContent = "打印中…";
      const deadline = Date.now() + 5 * 60 * 1000;
      while (Date.now() < deadline) {
        const result = assertResult(await api.get("/api/print/" + encodeURIComponent(jobId)));
        const status = String(result && result.print_job && result.print_job.status || "").toLowerCase();
        if (status === "completed") {
          toast((variant === "clean" ? "无标识版" : "带标识版") + "已打印，可再次打印");
          button.textContent = original;
          return;
        }
        if (status === "failed" || status === "unavailable") throw new Error("打印未完成");
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      throw new Error("打印状态确认超时，请在设备上确认");
    } catch (err) {
      pageError("打印未完成：" + (err.message || "请检查打印机"));
      toast("打印未完成");
    } finally {
      button.disabled = false;
      if (button.textContent === "提交中…" || button.textContent === "打印中…") button.textContent = original;
    }
  }

  let shotsSignature = "";
  function renderShots(recent) {
    recentPhotos = recent;
    const signature = JSON.stringify([recent, recent.map(canRegenerate)]);
    if (signature === shotsSignature) return;
    shotsSignature = signature;
    el.shotsCount.textContent = recent.length ? ("最近 " + recent.length + " 张") : "暂无";
    el.shots.replaceChildren();
    if (!recent.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "还没有成片。完成一次拍摄后，照片会出现在这里。";
      el.shots.appendChild(empty);
      return;
    }
    recent.forEach((item) => {
      const serial = String(item.id || "").match(/(\d+)$/)?.[1] || "—";
      const card = document.createElement("article");
      card.className = "shot-card";
      const pic = document.createElement("div");
      pic.className = "pic";
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = item.thumb + (item.thumb.includes("?") ? "&" : "?") + "v=" + encodeURIComponent(item.token);
      img.alt = serial + " 成片";
      pic.appendChild(img);
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = serial + " · " + window.Booth.portraitModeLabel(item.portrait_mode) + " · " + fmtMs(item.infer_ms) + " · " +
        (item.degraded ? "备用处理" : "已完成") +
        (item.ai_generated === false ? " · 原片" : item.ai_generated ? " · AI 成片" : "");
      if (item.aigc_channel_name) {
        const source = document.createElement("span");
        source.className = "shot-source";
        source.textContent = "生图渠道：" + item.aigc_channel_name +
          (item.aigc_model ? " · " + item.aigc_model : "");
        meta.append(document.createElement("br"), source);
      }
      const ops = document.createElement("div");
      ops.className = "ops";
      if (canRegenerate(item)) {
        const regenerate = document.createElement("button");
        regenerate.className = "btn btn-sm" + (item.degraded ? " btn-primary" : "");
        regenerate.type = "button";
        regenerate.textContent = "重新生成";
        regenerate.dataset.regenerate = item.token;
        regenerate.setAttribute("aria-label", "重新生成 " + serial);
        regenerate.addEventListener("click", () => openRegenerate(item, regenerate));
        ops.appendChild(regenerate);
        if (item.degraded) {
          const recovery = document.createElement("p");
          recovery.className = "shot-recovery";
          recovery.textContent = item.ai_generated === false
            ? "风格生成未完成，已保留原片，可重新生成。"
            : "当前为备用成片，可从保留的原片重新生成风格。";
          card.appendChild(recovery);
        }
      }
      const open = document.createElement("a");
      open.className = "btn btn-ghost btn-sm";
      open.href = "/p/" + encodeURIComponent(item.token);
      open.target = "_blank";
      open.rel = "noopener";
      open.textContent = "打开取图页";
      const downloadWatermarked = document.createElement("a");
      downloadWatermarked.className = "btn btn-ghost btn-sm";
      downloadWatermarked.href = "/api/photo/" + encodeURIComponent(item.token) + ".jpg";
      downloadWatermarked.download = serial + "-带标识.jpg";
      downloadWatermarked.textContent = "下载带标识版";
      const downloadClean = document.createElement("a");
      downloadClean.className = "btn btn-ghost btn-sm";
      downloadClean.href = "/api/photo/" + encodeURIComponent(item.token) + ".clean.jpg";
      downloadClean.download = serial + "-无标识.jpg";
      downloadClean.textContent = "下载无标识版";
      downloadClean.hidden = item.clean_available === false;
      const printWatermarked = document.createElement("button");
      printWatermarked.className = "btn btn-sm";
      printWatermarked.type = "button";
      printWatermarked.textContent = "打印带标识版";
      printWatermarked.addEventListener("click", () => printFromOperator(item, "watermarked", printWatermarked));
      const printClean = document.createElement("button");
      printClean.className = "btn btn-sm";
      printClean.type = "button";
      printClean.textContent = "打印无标识版";
      printClean.hidden = item.clean_available === false;
      printClean.addEventListener("click", () => printFromOperator(item, "clean", printClean));
      const del = document.createElement("button");
      del.className = "btn btn-danger btn-sm";
      del.type = "button";
      del.textContent = "删除";
      del.setAttribute("aria-label", "删除成片 " + serial);
      del.addEventListener("click", async () => {
        if (!window.confirm("删除 " + serial + "？删除后取图链接立即失效，不可恢复。")) return;
        del.disabled = true;
        try {
          assertResult(await api.del("/api/photo/" + encodeURIComponent(item.token)));
          toast("已删除 " + serial);
          await refresh();
        } catch (err) {
          pageError("删除未完成：" + err.message);
        } finally { del.disabled = false; }
      });
      ops.append(open, downloadWatermarked, downloadClean, printWatermarked, printClean, del);
      card.append(pic, meta, ops);
      el.shots.appendChild(card);
    });
    regenerateControls();
  }

  const QUEUE_STATUS_LABELS = {
    queued: "等待叫号", called: "已叫号", ready: "准备拍摄",
    capturing: "拍摄中", processing: "处理中",
  };

  function formatQueueTime(value) {
    if (!value) return "时间未知";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {
      hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  async function refreshQueueAdmin() {
    if (page !== "queue" || document.hidden) return;
    try {
      const data = assertResult(await api.get("/api/operator/queue"));
      el.badgeEp.textContent = "名单已更新";
      const entries = Array.isArray(data.entries) ? data.entries : [];
      el.queueAdminCount.textContent = data.unlimited
        ? (data.active || 0) + " 人处理中"
        : (data.active || 0) + " / " + data.max_active;
      el.queueAdminNote.textContent = data.unlimited
        ? "排队不设人数上限；可取消尚未开始拍摄的参与者。"
        : "当前排队容量：" + data.active + " / " + data.max_active + "。";
      el.queueAdminList.innerHTML = "";
      if (!entries.length) {
        el.queueAdminList.innerHTML = '<p class="muted">当前没有等待处理的排队记录。</p>';
        return;
      }
      entries.forEach((item) => {
        const row = document.createElement("div");
        row.className = "queue-admin-row";
        const number = document.createElement("strong");
        number.className = "queue-admin-number";
        const displayNumber = String(item.display_number || "").match(/(\d+)$/)?.[1] || "未编号";
        number.textContent = displayNumber;
        const style = document.createElement("span");
        style.className = "queue-admin-meta";
        style.textContent = "风格：" + window.Booth.styleDisplay({ id: item.style_id,
          title: item.style_title || (item.style_id === "pixel-person" ? "复古像素头像" : item.style_id) || "—" }).title +
          " · " + window.Booth.portraitModeLabel(item.portrait_mode);
        const status = document.createElement("span");
        status.className = "queue-admin-status";
        status.textContent = QUEUE_STATUS_LABELS[item.status] || item.status || "未知状态";
        const time = document.createElement("span");
        time.className = "queue-admin-meta";
        time.textContent = "加入：" + formatQueueTime(item.created_at);
        const actions = document.createElement("div");
        actions.className = "queue-admin-actions";
        const cancel = document.createElement("button");
        cancel.className = "btn btn-danger btn-sm";
        cancel.type = "button";
        cancel.textContent = "取消排队";
        const canCancel = ["queued", "called", "ready"].indexOf(item.status) >= 0;
        cancel.disabled = !canCancel;
        if (!canCancel) cancel.title = "已进入拍摄或处理阶段";
        cancel.addEventListener("click", async () => {
          if (!window.confirm("取消 " + displayNumber + " 的排队？")) return;
          cancel.disabled = true;
          try {
            await api.post("/api/operator/queue/" + encodeURIComponent(item.queue_id) + "/cancel", {});
            toast("已取消 " + displayNumber);
            refreshQueueAdmin();
          } catch (err) {
            toast("取消失败：" + err.message);
            refreshQueueAdmin();
          }
        });
        actions.appendChild(cancel);
        row.append(number, style, status, time, actions);
        el.queueAdminList.appendChild(row);
      });
    } catch (err) {
      el.badgeEp.textContent = "读取未完成";
      el.queueAdminCount.textContent = "不可用";
      el.queueAdminNote.textContent = "排队管理暂时无法读取：" + err.message;
      el.queueAdminList.innerHTML = "";
    }
  }

  async function refresh() {
    if (document.hidden || page === "styles") return;
    if (page === "queue") return refreshQueueAdmin();
    let s;
    try {
      s = assertResult(await api.get("/api/state"));
      pageError("");
    } catch (err) {
      el.badgeEp.textContent = "服务未响应";
      el.badgeEp.className = "badge badge-degraded";
      pageError("现场状态读取未完成：" + err.message + "。稍后自动重试，也可刷新页面。");
      return;
    }
    const cam = s.camera || {};
    const pv = s.preview || {};
    const pvc = pv.camera || {};
    const infer = s.infer || {};
    const counters = s.counters || {};
    const disk = s.disk || {};

    if (page !== "system") {
      el.badgeEp.textContent = "状态已更新";
      el.badgeEp.className = "badge";
      if (page === "overview") {
        const ready = cam.ok && cam.real && infer.running;
        el.badgeEp.textContent = ready ? "正常运行" : "需要检查";
        el.badgeEp.className = "badge " + (ready ? "badge-ep" : "badge-degraded");
        fill($("#overview-state"), [
          ["相机", cam.ok ? (cam.real ? "已连接" : "演示画面") : "未就绪", cam.ok && cam.real ? "good" : "warn"],
          ["人像处理", infer.running ? "运行中" : "未就绪", infer.running ? "good" : "bad"],
          ["当前拍摄", s.active_job ? "处理中" : "空闲"],
          ["已完成", s.photos == null ? "—" : s.photos + " 张"],
          ["照片保存", "1 天"],
          ["可用空间", disk.free_gb == null ? "—" : disk.free_gb + " GB"],
        ]);
      } else if (page === "photos") {
        renderShots(s.recent || []);
      } else if (page === "settings") {
        qrToken = s.last_token || "";
        Object.assign(cfg, s.share || {});
        renderConfig();
      }
      return;
    }

    el.badgeEp.textContent = infer.ep
      ? "EP SpaceMIT · " + (infer.ep_threads || 0) + " 线程"
      : "CPU（未拿到 AI 核）";
    el.badgeEp.className = "badge " + (infer.ep ? "badge-ep" : "badge-cpu");

    fill(el.kvCamera, [
      ["设备", cam.device || "未解析", cam.ok ? "good" : "bad"],
      ["分辨率", cam.ok ? cam.width + "×" + cam.height : "不可用", cam.ok ? "" : "bad"],
      ["像素格式", cam.fourcc || "—", cam.fourcc === "MJPG" ? "good" : "warn"],
      ["帧率", cam.fps ? cam.fps.toFixed(0) + " fps" : "—"],
      ["数据源", cam.real ? "真实相机" : "演示画面", cam.real ? "" : "warn"],
      ["备注", cam.detail || "ok"],

      ["预览流", pv.last_frame_s == null ? "—"
        : ((pv.clients || 0) + " 个页面 · 最近一帧 " + pv.last_frame_s + "s 前 · 已发 " + (pv.seq || 0) + " 帧"),
       pv.last_frame_s != null && pv.last_frame_s > 5 ? "warn" : ""],
      ["掉线自愈", pvc.reopens == null ? "—" : ("已自动重开 " + pvc.reopens + " 次"),
       (pvc.reopens || 0) > 0 ? "warn" : ""],
      ["最近一次重开", pvc.last_recover_note || "—"],
      ["USB 总线复位", (pvc.usb_resets || 0) > 0
        ? ("已复位 " + pvc.usb_resets + " 次 · 最近 " +
           (pvc.last_usb_reset_s == null ? "—" : pvc.last_usb_reset_s + "s 前"))
        : ("未用过 · 控制器 " + (pvc.usb_host || "未知")),
       (pvc.usb_resets || 0) > 0 ? "warn" : ""],
      ["最近一次 USB 复位", pvc.last_usb_reset_note || "—"],

      ["USB 复位未生效", (pvc.usb_reset_fails || 0) > 0
        ? (pvc.usb_reset_fails + " 次（软件救不回来，需人工重新插拔相机）") : "0 次",
       (pvc.usb_reset_fails || 0) > 0 ? "bad" : ""],
      ["拍照质量", pvc.snapshot_note || "按拍照分辨率正常"],
    ]);

    fill(el.kvInfer, [
      ["进程", infer.running ? "运行中" : "未运行", infer.running ? "good" : "bad"],
      ["预热", infer.preloaded ? "完成" : "进行中", infer.preloaded ? "good" : "warn"],
      ["AI 核", infer.ep ? "已拿到（" + (infer.ep_threads || 0) + " 线程）" : "未拿到", infer.ep ? "good" : "warn"],
      ["降级原因", infer.ep_fallback || "—", infer.ep_fallback ? "warn" : ""],
      ["重启次数", infer.restarts, infer.restarts > 0 ? "warn" : ""],
      ["丢弃噪声行", infer.noise_lines_dropped],
      ["运行时长", infer.uptime_s ? infer.uptime_s + " s" : "—"],
      ["最近错误", infer.last_error || "无", infer.last_error ? "bad" : "good"],
    ]);

    fill(el.kvStore, [
      ["已出图", s.photos + " 张"],
      ["编号格式", "SM-" + new Date().getFullYear() + "-NNNN"],
      ["留存", "1 天（自动清理）"],
      ["磁盘可用", disk.free_gb ? disk.free_gb + " GB / " + disk.total_gb + " GB" : "—"],
      ["降级出图", counters.degraded || 0, counters.degraded ? "warn" : ""],
      ["CPU 推理", counters.cpu_infers || 0, counters.cpu_infers ? "warn" : ""],
      ["错误", counters.errors || 0, counters.errors ? "bad" : "good"],
      ["近 20 次均值", fmtMs(s.avg_infer_ms)],
    ]);

    const active = s.active_job;
    if (active) {
      const doneKeys = (active.steps || []).map((x) => x.key);
      const activeKey = doneKeys.length ? doneKeys[doneKeys.length - 1] : "capture";
      renderSteps(el.steps, activeKey, doneKeys.slice(0, -1), STEPS);
    } else if (s.last_record) {
      renderSteps(el.steps, null, STEPS.map((x) => x.key), STEPS);
    } else {
      renderSteps(el.steps, "capture", [], STEPS);
    }

    fill(el.kvLast, [
      ["最近编号", s.last_record || "—"],
      ["任务状态", active ? active.stage : "空闲"],
      ["拍摄同意", ((s.consent && s.consent.live_tokens) || 0) + " 个有效"],
    ]);

    fill(el.kvDegraded, [
      ["最近降级原因", (active && active.degraded_reason) || "—"],
      ["MOCK 模式", cam.real ? "否" : "是（成片上会带 MOCK 标记）"],
    ]);

    el.build.textContent = "构建 " + s.build + " · 服务已运行 " + Math.round(s.uptime_s) + " s · " +
      (s.link_base ? "局域网地址 " + s.link_base : "");

  }

  const cfg = { public_base: "", configured: "", lan_base: "", source: "lan",
                secure: false, qr_cached: null, presentation_mode: "full",
                handoff_enabled: false, aigc_handoff_delay_seconds: 8 };
  const SOURCE_LABEL = {
    panel: "面板填写（优先级最高）",
    env: "设备预设地址",
    lan: "局域网（没有配置公网地址）",
  };
  let qrEpoch = Date.now();
  let qrToken = "";

  function renderConfig() {
    el.cfgBadge.textContent = cfg.secure ? "HTTPS 已配置" : "局域网 / HTTP";
    el.cfgBadge.className = "badge " + (cfg.secure ? "badge-ep" : "badge-degraded");
    fill(el.kvConfig, [
      ["生效地址", cfg.public_base || "—", cfg.secure ? "good" : "warn"],
      ["来源", SOURCE_LABEL[cfg.source] || cfg.source || "—"],
      ["局域网地址", cfg.lan_base || "—"],
      ["二维码缓存", cfg.qr_cached == null ? "—"
        : cfg.qr_cached + " 张（改地址会自动清空）"],
      ["手机分享", cfg.secure ? "已配置 HTTPS，请用手机验证分享" : "请配置 HTTPS 地址后验证",
       cfg.secure ? "good" : "warn"],
    ]);
    renderQr();
    if (el.cfgHandoff) el.cfgHandoff.checked = cfg.presentation_mode === "handoff" || cfg.handoff_enabled === true;
    if (el.cfgPresentationBadge) {
      const enabled = cfg.presentation_mode === "handoff" || cfg.handoff_enabled === true;
      el.cfgPresentationBadge.textContent = enabled ? "后台生成" : "完整展示";
      el.cfgPresentationBadge.className = "badge " + (enabled ? "badge-ep" : "badge-degraded");
    }
  }

  let qrSignature = "";
  function renderQr() {
    const signature = JSON.stringify([qrToken, qrEpoch, cfg.public_base]);
    if (signature === qrSignature) return;
    qrSignature = signature;
    el.qrCheck.replaceChildren();
    if (!qrToken) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "完成一次拍摄后，这里会显示取图二维码，方便用手机验证地址。";
      el.qrCheck.appendChild(empty);
      return;
    }
    const box = document.createElement("div");
    box.className = "qr-box";
    const img = document.createElement("img");
    img.alt = "最近成片的取图二维码";
    img.src = "/api/qr/" + encodeURIComponent(qrToken) + ".png?v=" + qrEpoch;
    box.appendChild(img);
    const side = document.createElement("div");
    side.className = "qr-side";
    const url = document.createElement("div");
    url.className = "qr-url";
    url.textContent = (cfg.public_base || "") + "/p/" + qrToken;
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "请用手机扫码确认能打开取图页，浏览器连接检查无法替代手机验证。";
    side.append(url, hint);
    el.qrCheck.append(box, side);
  }

  async function loadConfig() {
    try {
      Object.assign(cfg, assertResult(await api.get("/api/operator/config")));
      el.cfgBase.value = cfg.configured || "";
      renderConfig();

      [el.cfgSave, el.cfgClear, el.cfgPresentationSave].filter(Boolean)
        .forEach((node) => { node.disabled = false; });
    } catch (err) { feedback(el.cfgResult, "设置读取未完成：" + err.message + "，请刷新重试。", true); }
  }

  let channelRows = [];
  let channelsBusy = false;
  let channelsReady = false;
  let channelModelPresets = [];
  let channelsDirty = false;
  let channelsLoading = false;
  let channelEpoch = 0;
  let activeChannelId = null;
  let savedChannelRows = [];
  const channelHealth = new Map();
  const channelTests = new Set();

  function channelModel(channel) {
    return typeof channel.model === "string" ? channel.model : (channel.models || [])[0] || "";
  }

  function channelPayload(channel) {
    const model = channelModel(channel).trim();
    return {
      id: channel.id, name: (channel.name || "").trim(),
      base_url: (channel.base_url || "").trim(), model, models: model ? [model] : [],
      api_key: channel.api_key || "", health_url: (channel.health_url || "").trim(),
      priority: Number(channel.priority), timeout: Number(channel.timeout),
      enabled: channel.enabled !== false,
    };
  }

  function channelResponse(result) {
    assertResult(result);
    if (!result || !Array.isArray(result.channels) || result.channels.some((item) =>
      !item || typeof item.id !== "string" || !item.id ||
      (typeof item.model !== "string" && !Array.isArray(item.models))) ||
      new Set(result.channels.map((item) => item.id)).size !== result.channels.length) {
      throw new Error("渠道数据不完整，请重新读取；当前输入已保留");
    }
    return result;
  }

  function channelTestControls() {
    const busy = channelsBusy || channelTests.size > 0;
    el.channelSave.disabled = busy || !channelsReady || !channelsDirty;
    el.channelAdd.disabled = busy || !channelsReady;
    el.channelCheck.disabled = busy || !channelsReady;
    el.channelReload.disabled = busy || channelsLoading;
    el.channels.querySelectorAll("input, button").forEach((node) => { node.disabled = busy; });
    el.channels.querySelectorAll(".channel-test-button").forEach((node) => {
      const saved = savedChannelRows.find((item) => item.id === node.closest(".channel-row").dataset.channelId);
      node.disabled = busy || channelsDirty || !saved || !saved.key_configured || !channelModel(saved);
    });
    el.channelDirty.textContent = channelsReady
      ? (channelsDirty ? "有未保存修改 · 状态按已保存配置显示" : "已与保存配置同步") : "读取成功后才能编辑和保存";
    el.channelDirty.classList.toggle("is-dirty", channelsDirty);
    if (page === "models") window.Booth.buildGuard.hold(channelsDirty || busy);
  }

  function updateChannelDirty() {
    channelsDirty = JSON.stringify(readChannelRows()) !== JSON.stringify(savedChannelRows.map(channelPayload));
    channelTestControls();
  }

  function preserveChannelInputs() {
    const inputs = new Map(readChannelRows().map((item) => [item.id, item]));
    channelRows = channelRows.map((item) => ({ ...item, ...inputs.get(item.id) }));
  }

  function channelInput(labelText, value, type, placeholder, fieldName) {
    const label = document.createElement("label");
    label.className = "channel-field";
    const caption = document.createElement("span");
    caption.textContent = labelText;
    const input = document.createElement("input");
    input.className = "field";
    input.type = type || "text";
    input.dataset.field = fieldName;
    input.autocomplete = "off";
    input.value = value == null ? "" : String(value);
    input.placeholder = placeholder || "";
    label.append(caption, input);
    return { label, input };
  }

  function channelStatusText(channel) {
    const names = { available: "可用", unavailable: "不可用", unknown: "待检测" };
    const routes = { primary: "模型来源", standby: channel.race_eligible ? "参与竞速" : "其它模型",
      cooldown: "故障冷却", disabled: "已停用", unconfigured: "待配置" };
    const badge = document.createElement("span");
    badge.className = "badge " + (channel.routing === "primary" ? "badge-default" :
      channel.routing === "cooldown" ? "badge-degraded" : "");
    badge.textContent = routes[channel.routing] || "待保存";
    const detail = document.createElement("span");
    const latency = Number.isFinite(channel.latency_ms) ? " · " + channel.latency_ms + " ms" : "";
    detail.textContent = (names[channel.health] || "待检测") + " · " + (channel.detail || "尚未检测") + latency;
    return { badge, detail };
  }

  function updateChannelHealth(result) {
    activeChannelId = result.active_channel_id || null;
    channelHealth.clear();
    result.channels.forEach((item) => { channelHealth.set(item.id, item); });
    const active = result.channels.find((item) => item.id === activeChannelId);
    el.channelActive.textContent = active
      ? "竞速模型：" + (result.race_model || channelModel(active)) + " · " +
        (result.race_channel_count || 1) + " 个渠道并发"
      : "当前没有可用渠道，请检查启用状态、密钥或故障冷却。";
    el.channels.querySelectorAll(".channel-row").forEach((card) => {
      const channel = channelHealth.get(card.dataset.channelId) || {};
      const state = channelStatusText(channel);
      card.querySelector(".channel-health").replaceChildren(state.badge, state.detail);
      card.classList.toggle("is-primary", channel.routing === "primary");
    });
  }

  function renderChannels() {
    const existing = new Map(Array.from(el.channels.querySelectorAll(".channel-row"))
      .map((card) => [card.dataset.channelId, card]));
    el.channels.querySelectorAll(".channel-empty").forEach((node) => { node.remove(); });
    existing.forEach((card, id) => {
      if (!channelRows.some((item) => item.id === id)) card.remove();
    });
    el.channelCount.textContent = channelRows.length + " 个渠道";
    if (!channelRows.length) {
      const empty = document.createElement("p");
      empty.className = "hint channel-empty";
      empty.textContent = "还没有渠道。新增渠道后，保存并启用即可参与生图。";
      el.channels.appendChild(empty);
      channelTestControls();
      return;
    }
    channelRows.forEach((channel) => {
      if (existing.has(channel.id)) return;
      const card = document.createElement("article");
      card.className = "channel-row";
      card.dataset.channelId = channel.id;
      const heading = document.createElement("div");
      heading.className = "row-between channel-heading";
      const title = document.createElement("strong");
      title.textContent = channel.name || "新渠道";
      const state = channelHealth.get(channel.id) || {};
      const status = channelStatusText(state);
      card.classList.toggle("is-primary", state.routing === "primary");
      const statusWrap = document.createElement("div");
      statusWrap.className = "channel-health";
      statusWrap.append(status.badge, status.detail);
      heading.append(title, statusWrap);

      const fields = document.createElement("div");
      fields.className = "channel-fields";
      const name = channelInput("渠道名称", channel.name, "text", "例如：主服务", "name");
      name.input.required = true;
      name.input.maxLength = 64;
      const url = channelInput("服务 URL", channel.base_url, "url", "https://api.example.com/v1", "base_url");
      url.input.required = true;
      url.input.spellcheck = false;
      const model = channelInput("生成模型", channelModel(channel), "text", "选择已有模型或直接输入名称", "model");
      model.input.required = true;
      model.input.spellcheck = false;
      const suggestions = document.createElement("datalist");
      suggestions.id = "channel-models-" + channel.id;
      model.input.setAttribute("list", suggestions.id);
      Array.from(new Set([channelModel(channel), ...(channel.models || []),
        ...channelModelPresets.map((item) => item.model)])).filter(Boolean).forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        suggestions.appendChild(option);
      });
      model.label.appendChild(suggestions);
      const key = channelInput("API Key", channel.api_key || "", "password", channel.key_configured ? "已保存，留空保留" : "输入服务密钥", "api_key");
      key.input.autocomplete = "new-password";
      key.input.name = "aigc-key-" + channel.id;
      key.input.maxLength = 4096;
      const healthUrl = channelInput("健康检查 URL（可选）", channel.health_url, "url", "默认检查服务地址 + /models", "health_url");
      healthUrl.input.spellcheck = false;
      const priority = channelInput("优先级 · 数字越小越先用", channel.priority, "number", "1", "priority");
      priority.input.min = "1";
      priority.input.max = "999";
      priority.input.required = true;
      priority.input.step = "1";
      const timeout = channelInput("生成超时（秒）", channel.timeout ?? 120, "number", "120", "timeout");
      timeout.input.min = "5";
      timeout.input.max = "300";
      timeout.input.required = true;
      timeout.input.step = "1";
      model.input.addEventListener("change", () => {
        const preset = channelModelPresets.find((item) => item.model === model.input.value.trim());
        if (!preset) return;
        url.input.value = preset.base_url;
        timeout.input.value = String(preset.timeout || 120);
        if (!name.input.value.trim() || name.input.value === "新渠道") name.input.value = preset.name;
        updateChannelDirty();
      });
      const enabledLabel = document.createElement("label");
      enabledLabel.className = "channel-toggle";
      const enabled = document.createElement("input");
      enabled.type = "checkbox";
      enabled.dataset.field = "enabled";
      enabled.checked = channel.enabled !== false;
      const enabledText = document.createElement("span");
      enabledText.textContent = "启用渠道";
      enabledLabel.append(enabled, enabledText);
      fields.append(name.label, model.label, url.label, key.label, priority.label, enabledLabel);

      const advanced = document.createElement("details");
      advanced.className = "channel-advanced";
      const advancedSummary = document.createElement("summary");
      advancedSummary.textContent = "高级设置";
      const advancedFields = document.createElement("div");
      advancedFields.className = "channel-fields";
      advancedFields.append(healthUrl.label, timeout.label);
      advanced.append(advancedSummary, advancedFields);

      const test = document.createElement("details");
      test.className = "channel-test";
      const testSummary = document.createElement("summary");
      testSummary.textContent = "测试生成";
      const testHint = document.createElement("p");
      testHint.className = "hint";
      testHint.textContent = "使用此渠道已保存的模型：" + (channelModel(channel) || "尚未设置") + "。修改后请先保存；测试会实际生成一张图片。";
      const testControls = document.createElement("div");
      testControls.className = "channel-test-controls";
      const testFile = document.createElement("input");
      testFile.className = "channel-test-file";
      testFile.type = "file";
      testFile.accept = "image/jpeg,image/png,image/webp";
      testFile.setAttribute("aria-label", "测试图片");
      const testButton = document.createElement("button");
      testButton.type = "button";
      testButton.className = "btn btn-primary channel-test-button";
      testButton.textContent = "测试生成";
      const testResult = document.createElement("div");
      testResult.className = "channel-test-result";
      testResult.setAttribute("role", "status");
      testResult.setAttribute("aria-live", "polite");
      testButton.addEventListener("click", async () => {
        if (!channelsReady || channelsBusy || channelTests.size) return;
        updateChannelDirty();
        if (channelsDirty) {
          testResult.classList.add("is-error");
          testResult.textContent = "请先保存渠道";
          return;
        }
        const file = testFile.files && testFile.files[0];
        if (!file) {
          testResult.classList.add("is-error");
          testResult.textContent = "请选择测试图片";
          testFile.focus();
          return;
        }
        if (file.size > 20 * 1024 * 1024) {
          testResult.classList.add("is-error");
          testResult.textContent = "图片不能超过 20 MB";
          return;
        }
        channelTests.add(channel.id);
        channelEpoch++;
        channelTestControls();
        test.setAttribute("aria-busy", "true");
        testButton.textContent = "生成中…";
        testResult.classList.remove("is-error");
        testResult.textContent = "正在生成";
        const form = new FormData();
        form.append("id", channel.id);
        form.append("image", file, file.name);
        try {
          const result = assertResult(await api.post("/api/operator/aigc/channels/test", form));
          if (!result || typeof result.image !== "string" || !result.image) throw new Error("生成结果缺少图片，请重试");
          const preview = document.createElement("img");
          preview.src = result.image;
          preview.alt = channel.name + " 测试生成结果";
          const summary = document.createElement("strong");
          summary.textContent = "生成成功 · " + fmtMs(result.elapsed_ms);
          const used = document.createElement("span");
          used.textContent = result.model || channelModel(channel);
          testResult.replaceChildren(preview, summary, used);
        } catch (err) {
          testResult.classList.add("is-error");
          testResult.textContent = err.message;
        } finally {
          channelTests.delete(channel.id);
          channelTestControls();
          test.setAttribute("aria-busy", "false");
          testButton.textContent = "测试生成";
          await loadAigcChannels(true);
        }
      });
      testControls.append(testFile, testButton);
      test.append(testSummary, testHint, testControls, testResult);

      const footer = document.createElement("div");
      footer.className = "channel-footer";
      const id = document.createElement("span");
      id.className = "hint";
      id.textContent = "渠道编号：" + channel.id;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-ghost btn-sm";
      remove.textContent = "移除";
      remove.disabled = channelsBusy;
      remove.addEventListener("click", () => {
        if (!channelsReady || channelsBusy || channelTests.size) return;
        if (!window.confirm("移除此渠道？保存后才会生效。")) return;
        preserveChannelInputs();
        channelRows = channelRows.filter((item) => item.id !== channel.id);
        renderChannels();
        updateChannelDirty();
      });
      footer.append(id, remove);
      card.append(heading, fields, advanced, test, footer);
      el.channels.appendChild(card);
    });
    channelTestControls();
  }

  function readChannelRows() {
    return Array.from(el.channels.querySelectorAll(".channel-row")).map((card) => {
      const values = { id: card.dataset.channelId };
      card.querySelectorAll("[data-field]").forEach((input) => {
        values[input.dataset.field] = input.type === "checkbox" ? input.checked : input.value;
      });
      return channelPayload(values);
    });
  }

  async function loadAigcChannels(updateHealthOnly) {
    if (channelsLoading || channelsBusy || channelTests.size) return;
    const epoch = channelEpoch;
    const initial = !channelsReady;
    const fullLoad = !updateHealthOnly || initial;
    channelsLoading = true;
    if (fullLoad) channelsBusy = true;
    channelTestControls();
    try {
      const result = channelResponse(await api.get("/api/operator/aigc/channels"));
      if (epoch !== channelEpoch) return;
      channelModelPresets = Array.isArray(result.model_presets) ? result.model_presets : [];
      if (fullLoad) {
        savedChannelRows = result.channels.map((item) => ({ ...item, model: channelModel(item), api_key: "" }));
        channelRows = savedChannelRows.map((item) => ({ ...item }));
        channelsReady = true;
        channelsDirty = false;
        el.channels.replaceChildren();
        updateChannelHealth(result);
        renderChannels();
        feedback(el.channelResult, "", false);
      } else {
        updateChannelHealth(result);
      }
      return true;
    } catch (err) {
      if (epoch !== channelEpoch) return;
      if (!channelsReady) el.channelCount.textContent = "读取失败";
      feedback(el.channelResult, "渠道状态读取未完成：" + err.message, true);
      return false;
    } finally {
      channelsLoading = false;
      if (fullLoad) channelsBusy = false;
      el.channels.setAttribute("aria-busy", "false");
      channelTestControls();
    }
  }

  async function saveAigcChannels() {
    if (!channelsReady || channelsBusy || channelTests.size || !channelsDirty) return;
    for (const input of el.channels.querySelectorAll(".channel-field input")) {
      if (!input.checkValidity()) {
        const details = input.closest("details");
        if (details) details.open = true;
        input.reportValidity();
        return;
      }
    }
    const payload = readChannelRows();
    channelsBusy = true;
    channelEpoch++;
    channelTestControls();
    el.channels.setAttribute("aria-busy", "true");
    feedback(el.channelResult, "正在保存渠道…", false);
    try {
      const result = channelResponse(await api.post("/api/operator/aigc/channels", { channels: payload }));
      savedChannelRows = result.channels.map((item) => ({ ...item, model: channelModel(item), api_key: "" }));
      channelRows = savedChannelRows.map((item) => ({ ...item }));
      channelsDirty = false;
      el.channels.replaceChildren();
      updateChannelHealth(result);
      renderChannels();
      feedback(el.channelResult, "渠道配置已保存；可用状态会自动监测。", false);
    } catch (err) {
      feedback(el.channelResult, "保存未完成：" + err.message, true);
    } finally {
      channelsBusy = false;
      el.channels.setAttribute("aria-busy", "false");
      channelTestControls();
    }
  }

  async function checkAigcChannels() {
    if (!channelsReady || channelsBusy || channelTests.size) return;
    channelsBusy = true;
    channelEpoch++;
    channelTestControls();
    feedback(el.channelResult, "正在检查渠道连接…", false);
    try {
      const result = channelResponse(await api.post("/api/operator/aigc/channels/check", {}));
      updateChannelHealth(result);
      feedback(el.channelResult, channelsDirty ? "已检查已保存的渠道，未保存输入已保留。" : "渠道检查完成。", false);
    } catch (err) {
      feedback(el.channelResult, "检查未完成：" + err.message, true);
    } finally {
      channelsBusy = false;
      channelTestControls();
    }
  }

  el.channelAdd.addEventListener("click", () => {
    if (!channelsReady || channelsBusy || channelTests.size) return;
    preserveChannelInputs();
    const id = "channel-" + (window.crypto && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(36).slice(2, 14));
    channelRows.push({ id, name: "", base_url: "", health_url: "", model: "", models: [],
      enabled: true, priority: 100, timeout: 120, key_configured: false, health: "unknown", detail: "尚未检测" });
    renderChannels();
    updateChannelDirty();
    el.channels.querySelector('[data-channel-id="' + CSS.escape(id) + '"] .channel-field input').focus();
  });
  el.channelSave.addEventListener("click", saveAigcChannels);
  el.channelCheck.addEventListener("click", checkAigcChannels);
  el.channelReload.addEventListener("click", () => {
    if (channelsBusy || channelTests.size || channelsLoading) return;
    if (channelsDirty && !window.confirm("重新读取会放弃尚未保存的渠道修改，是否继续？")) return;
    loadAigcChannels(false);
  });
  el.channels.addEventListener("input", (event) => {
    if (event.target.matches("[data-field]")) updateChannelDirty();
  });
  el.channels.addEventListener("change", (event) => {
    if (event.target.matches("[data-field]")) updateChannelDirty();
  });

  let aigcCatalog = { version: 0, styles: [] };
  let catalogReady = false;
  let styleBusy = false;
  let editorDirty = false;
  let editorState = null;
  let returnFocus = null;
  const dialog = $("#style-dialog");
  const styleForm = $("#style-form");
  const editor = $("#style-editor");
  const editorResult = $("#style-editor-result");
  const styleSave = $("#style-save");
  const styleSearch = $("#style-search");
  const styleFilter = $("#style-filter");
  const styleReload = $("#style-reload");

  function field(label, type, value, className) {
    const wrap = document.createElement("label");
    wrap.className = "prompt-field" + (className ? " " + className : "");
    const caption = document.createElement("span");
    caption.textContent = label;
    const input = document.createElement(type === "textarea" ? "textarea" : "input");
    input.className = "field";
    input.value = value == null ? "" : String(value);
    if (type === "textarea") input.rows = 6;
    else input.type = type || "text";
    wrap.append(caption, input);
    return { wrap, input };
  }

  function selectField(label, value, options) {
    const wrap = document.createElement("label");
    wrap.className = "prompt-field";
    const caption = document.createElement("span");
    caption.textContent = label;
    const input = document.createElement("select");
    input.className = "field";
    options.forEach((option) => {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      input.appendChild(node);
    });
    input.value = value;
    wrap.append(caption, input);
    return { wrap, input };
  }

  function button(label, action, className, disabled) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "btn btn-sm " + (className || "btn-ghost");
    node.textContent = label;
    node.disabled = Boolean(disabled);
    node.addEventListener("click", action);
    return node;
  }

  function stylePath(style) {
    return "/api/operator/aigc/styles/" + encodeURIComponent(style.id);
  }

  function catalogControls() {
    el.aigcAdd.disabled = !catalogReady || styleBusy;
    styleReload.disabled = styleBusy;
    el.aigcStyles.setAttribute("aria-busy", String(styleBusy));
    el.aigcStyles.querySelectorAll("button").forEach((node) => {
      node.disabled = styleBusy || node.dataset.unavailable === "true";
    });
  }

  async function styleMutation(request, message, pendingMessage) {
    if (styleBusy) return false;
    styleBusy = true;
    catalogControls();
    feedback(el.aigcResult, pendingMessage || "正在保存…", false);
    try {
      assertResult(await request());
      const loaded = await loadAigcStyles();
      feedback(el.aigcResult, message + (loaded ? "" : "，目录刷新未完成，请点击刷新目录。"), !loaded);
      toast(message);
      return true;
    } catch (err) {
      feedback(el.aigcResult, "操作未完成：" + err.message, true);
      return false;
    } finally {
      styleBusy = false;
      catalogControls();
    }
  }

  function uniqueId(base) {
    const stem = base.slice(0, 42).replace(/-+$/, "");
    let candidate = stem;
    let suffix = 2;
    while (aigcCatalog.styles.some((style) => style.id === candidate)) {
      candidate = stem + "-" + suffix++;
    }
    return candidate;
  }

  function closeEditor() {
    if (styleBusy) return;
    if (editorDirty && !window.confirm("放弃尚未保存的风格修改？")) return;
    dialog.close();
  }

  function showDialog(title) {
    returnFocus = document.activeElement;
    $("#style-dialog-title").textContent = title;
    editor.replaceChildren();
    feedback(editorResult, "", false);
    editorDirty = false;
    window.Booth.buildGuard.hold(true);
    dialog.showModal();
  }

  function openStyleEditor(style, isNew) {
    showDialog(isNew ? "新增风格" : "编辑风格");
    styleSave.hidden = false;
    editorState = { style, isNew };
    const grid = document.createElement("div");
    grid.className = "prompt-fields";
    const id = field("风格标识", "text", style.id);
    id.input.readOnly = !isNew;
    id.input.required = true;
    id.input.pattern = "[a-z][a-z0-9-]{2,48}";
    id.input.maxLength = 49;
    const title = field("风格名称", "text", window.Booth.styleDisplay(style).title);
    title.input.required = true;
    title.input.maxLength = 80;
    const description = field("给参与者的简短说明", "text", window.Booth.styleDisplay(style).description, "prompt-full");
    description.input.maxLength = 240;
    const orientation = selectField("默认版式", style.default_orientation || "vertical", [
      { value: "vertical", label: "竖版" }, { value: "wide", label: "横版" },
    ]);
    const enabled = document.createElement("label");
    enabled.className = "prompt-toggle";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = style.enabled !== false;
    enabled.append(enabledInput, document.createTextNode("启用，供参与者选择"));
    grid.append(id.wrap, title.wrap, description.wrap, orientation.wrap, enabled);
    editor.appendChild(grid);
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = isNew
      ? "风格标识需使用 3–49 位小写字母、数字和短横线，并以字母开头。填写生成说明后保存。"
      : (style.is_default ? "当前为默认风格。停用时会同时取消默认。" : "设为默认可在目录中操作。");
    editor.appendChild(note);
    const advanced = document.createElement("details");
    advanced.className = "editor-advanced";
    const summary = document.createElement("summary");
    summary.textContent = "横版设置（选填）";
    const advancedBody = document.createElement("div");
    advancedBody.className = "editor-body";
    const wide = field("横版生成说明", "textarea", style.wide_prompt, "prompt-textarea");
    const vertical = field("竖版生成说明", "textarea", style.vertical_prompt, "prompt-textarea");
    const negative = field("统一避免内容（选填）", "textarea", style.negative_prompt, "prompt-textarea");
    [wide, vertical, negative].forEach((item) => { item.input.maxLength = 12000; });
    vertical.input.required = true;
    vertical.input.minLength = 20;
    negative.input.placeholder = "可留空";
    wide.input.placeholder = "可留空，沿用竖版生成说明";
    editor.append(vertical.wrap, negative.wrap);
    advancedBody.append(wide.wrap);
    advanced.append(summary, advancedBody);
    editor.appendChild(advanced);
    editorState.fields = { id, title, description, orientation, enabledInput, wide, vertical, negative, advanced };
    title.input.focus();
  }

  styleForm.addEventListener("input", () => { editorDirty = true; });
  styleForm.addEventListener("change", () => { editorDirty = true; });
  styleForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!editorState || styleBusy || styleSave.hidden) return;
    const { style, isNew, fields } = editorState;
    const missing = [fields.vertical, fields.wide].find((item) => {
      const value = item.input.value.trim();
      return value.length < 20 && (item === fields.vertical || value.length > 0);
    });
    if (missing) {
      if (missing === fields.wide) fields.advanced.open = true;
      feedback(editorResult, missing === fields.vertical ? "竖版生成说明至少 20 个字符。" : "横版生成说明可留空，填写时至少 20 个字符。", true);
      missing.input.focus();
      return;
    }
    const payload = {
      id: fields.id.input.value.trim(), title: fields.title.input.value.trim(),
      description: fields.description.input.value.trim(),
      sizes: style.sizes || ["1024x1536"],
      backdrop_id: style.backdrop_id || "dynamic-minimal",
      default_orientation: fields.orientation.input.value,
      wide_prompt: fields.wide.input.value.trim(),
      vertical_prompt: fields.vertical.input.value.trim(),
      negative_prompt: fields.negative.input.value.trim(),
      enabled: fields.enabledInput.checked,
      is_default: fields.enabledInput.checked && Boolean(style.is_default),
      preserve_identity: true, single_person: true, no_generated_text: true, no_border: true,
    };
    if (isNew && aigcCatalog.styles.some((item) => item.id === payload.id)) {
      feedback(editorResult, "这个风格标识已存在，请换一个标识。", true);
      fields.id.input.focus();
      return;
    }
    styleSave.disabled = true;
    feedback(editorResult, "正在保存…", false);
    const saved = await styleMutation(
      () => api.post(isNew ? "/api/operator/aigc/styles" : stylePath(style), payload),
      "风格已保存"
    );
    styleSave.disabled = false;
    if (saved) {
      editorDirty = false;
      dialog.close();
    } else {
      feedback(editorResult, el.aigcResult.textContent, true);
    }
  });
  $("#style-close").addEventListener("click", closeEditor);
  $("#style-cancel").addEventListener("click", closeEditor);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeEditor();
  });
  dialog.addEventListener("close", () => {
    editorDirty = false;
    editorState = null;
    window.Booth.buildGuard.hold(false);
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    else el.aigcAdd.focus();
  });

  function openHistory(style) {
    showDialog("恢复历史 · " + window.Booth.styleDisplay(style).title);
    editorState = null;
    styleSave.hidden = true;
    const history = (style.history || []).slice().reverse();
    if (!history.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "还没有可恢复的历史版本。修改并保存后会保留上一版本。";
      editor.appendChild(empty);
    }
    history.forEach((revision) => {
      const row = document.createElement("div");
      row.className = "history-row";
      const text = document.createElement("span");
      const stamp = revision.updated_at ? formatQueueTime(Number(revision.updated_at) * 1000) : "时间未知";
      text.textContent = "版本 " + revision.revision + " · " + stamp + " · " + (revision.title || style.title);
      const restore = button("恢复这一版", async () => {
        if (styleBusy || !window.confirm("将「" + style.title + "」恢复到版本 " + revision.revision + "？当前版本会保留在历史中。")) return;
        editor.querySelectorAll("button").forEach((node) => { node.disabled = true; });
        const saved = await styleMutation(
          () => api.post(stylePath(style) + "/rollback", { revision: revision.revision }),
          "已恢复历史版本"
        );
        if (saved) dialog.close();
        else {
          feedback(editorResult, el.aigcResult.textContent, true);
          editor.querySelectorAll("button").forEach((node) => { node.disabled = false; });
        }
      });
      row.append(text, restore);
      editor.appendChild(row);
    });
  }

  function renderAigcStyles() {
    el.aigcStyles.replaceChildren();
    const query = styleSearch.value.trim().toLocaleLowerCase();
    const filter = styleFilter.value;
    const rows = aigcCatalog.styles.filter((style) => {
      const enabled = style.enabled !== false;
      return (filter === "all" || (filter === "enabled" ? enabled : !enabled)) &&
        [window.Booth.styleDisplay(style).title, window.Booth.styleDisplay(style).description, style.id].some((value) => String(value || "").toLocaleLowerCase().includes(query));
    });
    el.aigcBadge.textContent = aigcCatalog.styles.length + " 种风格";
    el.aigcBadge.className = "badge";
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = aigcCatalog.styles.length ? "没有匹配的风格，请调整搜索或筛选。" : "还没有风格，点击「新增风格」开始创建。";
      el.aigcStyles.appendChild(empty);
    }
    rows.forEach((style) => {
      const row = document.createElement("article");
      row.className = "style-row";
      const copy = document.createElement("div");
      copy.className = "style-copy";
      const heading = document.createElement("div");
      heading.className = "style-heading";
      const title = document.createElement("strong");
      title.textContent = window.Booth.styleDisplay(style).title;
      const status = document.createElement("span");
      status.className = "badge" + (style.enabled === false ? "" : " badge-ep");
      status.textContent = style.enabled === false ? "已停用" : "已启用";
      heading.append(title, status);
      if (style.is_default) {
        const mark = document.createElement("span");
        mark.className = "badge badge-default";
        mark.textContent = "默认";
        heading.appendChild(mark);
      }
      copy.append(heading);
      const actions = document.createElement("div");
      actions.className = "style-actions";
      const edit = button("编辑", () => openStyleEditor(style, false));
      const toggle = button(style.enabled === false ? "启用" : "停用", () => {
        if (styleBusy) return;
        const enabled = style.enabled === false;
        styleMutation(
          () => api.post(stylePath(style), { enabled, is_default: enabled && Boolean(style.is_default) }),
          enabled ? "风格已启用" : "风格已停用"
        );
      });
      const setDefault = button(style.is_default ? "当前默认" : "设为默认", () => {
        if (styleBusy || style.enabled === false || style.is_default) return;
        styleMutation(() => api.post("/api/operator/aigc/styles", {
          styles: aigcCatalog.styles.map((item) => {
            const payload = { ...item, is_default: item.id === style.id && item.enabled !== false };
            delete payload.model;
            return payload;
          }),
        }), "默认风格已更新");
      }, "btn-ghost", style.enabled === false || style.is_default);
      setDefault.dataset.unavailable = String(style.enabled === false || Boolean(style.is_default));
      if (style.enabled === false) setDefault.title = "请先启用这个风格";
      const more = document.createElement("details");
      more.className = "more-actions";
      const summary = document.createElement("summary");
      summary.textContent = "更多";
      summary.setAttribute("aria-label", window.Booth.styleDisplay(style).title + "的更多操作");
      const menu = document.createElement("div");
      menu.className = "more-menu";
      const duplicate = button("复制为新风格", () => {
        more.open = false;
        openStyleEditor({
          ...style, id: uniqueId(style.id + "-copy"), title: window.Booth.styleDisplay(style).title.slice(0, 75) + "（副本）",
          description: window.Booth.styleDisplay(style).description,
          enabled: false, is_default: false, history: [], revision: 1,
        }, true);
      });
      const history = button("恢复历史", () => { more.open = false; openHistory(style); });
      const remove = button("删除风格", () => {
        if (styleBusy) return;
        if (!window.confirm("删除风格「" + (style.title || style.id) + "」及其历史？此操作无法恢复。正在使用或最后一个启用风格无法删除。")) return;
        more.open = false;
        styleMutation(() => api.post(stylePath(style) + "/delete", {}), "风格已删除", "正在删除风格…");
      }, "btn-danger");
      menu.append(duplicate, history, remove);
      more.append(summary, menu);
      actions.append(edit, toggle, setDefault, more);
      row.append(copy, actions);
      el.aigcStyles.appendChild(row);
    });
    catalogControls();
  }

  async function loadAigcStyles() {
    el.aigcStyles.setAttribute("aria-busy", "true");
    try {
      const result = assertResult(await api.get("/api/operator/aigc/styles"));
      if (!result || !Array.isArray(result.styles)) throw new Error("返回的风格目录无法读取");
      aigcCatalog = result;
      catalogReady = true;
      el.badgeEp.textContent = "目录已更新";
      el.badgeEp.className = "badge";
      renderAigcStyles();
      return true;
    } catch (err) {
      catalogReady = false;
      el.aigcBadge.textContent = "目录不可用";
      el.badgeEp.textContent = "读取未完成";
      el.badgeEp.className = "badge badge-degraded";
      feedback(el.aigcResult, "风格目录读取未完成：" + err.message + "。请点击刷新目录重试。", true);
      el.aigcStyles.replaceChildren();
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "暂时无法读取风格目录。恢复连接后刷新重试。";
      el.aigcStyles.appendChild(empty);
      return false;
    } finally {
      el.aigcStyles.setAttribute("aria-busy", String(styleBusy));
      catalogControls();
    }
  }

  styleSearch.addEventListener("input", () => { if (catalogReady) renderAigcStyles(); });
  styleFilter.addEventListener("change", () => { if (catalogReady) renderAigcStyles(); });
  styleReload.addEventListener("click", async () => {
    if (styleBusy) return;
    styleReload.disabled = true;
    const loaded = await loadAigcStyles();
    if (loaded) feedback(el.aigcResult, "风格目录已刷新", false);
    styleReload.disabled = false;
  });
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".more-actions[open]").forEach((details) => {
      if (!details.contains(event.target)) details.open = false;
    });
  });
  el.aigcAdd.addEventListener("click", () => {
    if (!catalogReady || styleBusy) return;
    openStyleEditor({
      id: uniqueId("new-style"), title: "新风格", description: "",
      sizes: ["1024x1536"],
      backdrop_id: "dynamic-minimal", default_orientation: "vertical",
      wide_prompt: "", vertical_prompt: "", negative_prompt: "",
      enabled: false, is_default: false,
      preserve_identity: true, single_person: true, no_generated_text: true, no_border: true,
    }, true);
  });

  async function saveConfig(value) {
    if (value && !el.cfgBase.reportValidity()) return;
    el.cfgSave.disabled = true;
    el.cfgClear.disabled = true;
    try {
      Object.assign(cfg, assertResult(await api.post("/api/operator/config", { public_base: value })));
      el.cfgBase.value = cfg.configured || "";
      qrEpoch = Date.now();
      el.cfgResult.textContent = cfg.configured
        ? ("已保存：" + cfg.public_base + " · 清掉 " + (cfg.qr_cleared || 0) + " 张二维码缓存")
        : ("已清空，回落局域网地址：" + cfg.lan_base + " · 清掉 " + (cfg.qr_cleared || 0) + " 张二维码缓存");
      toast("取图地址已更新");
      renderConfig();
    } catch (err) {
      el.cfgResult.textContent = "保存失败：" + err.message;
      toast("保存失败：" + err.message);
    } finally {
      el.cfgSave.disabled = false;
      el.cfgClear.disabled = false;
    }
  }

  el.cfgSave.addEventListener("click", () => saveConfig(el.cfgBase.value));
  el.cfgClear.addEventListener("click", () => {
    if (!window.confirm("清空公网地址，二维码回到局域网地址？")) return;
    saveConfig("");
  });

  async function savePresentationConfig() {
    if (!el.cfgHandoff || !el.cfgPresentationSave) return;
    el.cfgPresentationSave.disabled = true;
    try {
      Object.assign(cfg, assertResult(await api.post("/api/operator/config", {
        handoff_enabled: el.cfgHandoff.checked,
      })));
      renderConfig();
      const enabled = cfg.presentation_mode === "handoff";
      feedback(el.cfgPresentationResult,
        enabled ? "已启用：AIGC 开始 8 秒后回到预览，手机继续显示生成状态。"
          : "已启用：完整展示当前用户的生成流程。", false);
      toast("现场展示模式已更新");
    } catch (err) {
      feedback(el.cfgPresentationResult, "保存失败：" + err.message, true);
    } finally {
      el.cfgPresentationSave.disabled = false;
    }
  }

  if (el.cfgPresentationSave) el.cfgPresentationSave.addEventListener("click", savePresentationConfig);

  el.cfgTest.addEventListener("click", async () => {
    const base = (el.cfgBase.value || "").trim().replace(/\/+$/, "");
    if (base && !el.cfgBase.reportValidity()) return;
    if (!base) {
      el.cfgResult.textContent = "先填一个地址再测";
      return;
    }
    el.cfgTest.disabled = true;
    el.cfgResult.textContent = "正在测 " + base + " …";
    try {
      const t0 = performance.now();


      await fetch(base + "/static/assets/brand/spacemit-logo.svg",
                  { mode: "no-cors", cache: "no-store" });
      el.cfgResult.textContent = "浏览器已完成连接检查（" + Math.round(performance.now() - t0) + " ms）。请用手机扫码确认取图页可以打开；此检查无法确认页面内容或手机证书。";
      toast("连接检查已完成");
    } catch (err) {
      el.cfgResult.textContent = "连接检查未完成：" + err.message + "。请确认地址和网络连接。";
      toast("地址不可访问");
    } finally {
      el.cfgTest.disabled = false;
    }
  });

  el.restore.addEventListener("click", async () => {
    el.restore.disabled = true;
    el.opResult.textContent = "正在恢复到就绪…";
    try {
      const result = assertResult(await api.post("/api/operator/restore", {}));
      el.opResult.textContent = (result.actions || []).join(" · ");
      toast("恢复操作已完成，请确认设备状态");
    } catch (err) {
      el.opResult.textContent = "恢复失败：" + err.message;
      toast("恢复失败：" + err.message);
    } finally {
      el.restore.disabled = false;
      refresh();
    }
  });

  el.usbReset.addEventListener("click", async () => {
    const yes = window.confirm(
      "重新连接相机？这会让同一接口上的其他设备短暂重连，大约需要 10–20 秒。请在无人拍摄时操作。");
    if (!yes) return;
    el.usbReset.disabled = true;
    el.opResult.textContent = "正在复位 USB 总线（约 10~20 秒）…";
    try {
      const r = await api.post("/api/operator/usb-reset", {});
      const after = r.after || {};
      el.opResult.textContent = (r.ok ? "USB 总线已复位，相机回来了：" : "复位没能救回相机：") +
        (r.host || "-") + " · " + (after.device || "-") + " " +
        [after.fourcc, after.ok ? "ok" : "打不开"].filter(Boolean).join(" ") +
        (r.detail ? " · " + r.detail : "") + " · 耗时 " + Math.round(r.elapsed_ms || 0) + " ms";
      toast(r.ok ? "相机已恢复" : "仍未恢复，请人工检查 USB 线与插口");
    } catch (err) {
      el.opResult.textContent = "USB 复位失败：" + err.message;
      toast("USB 复位失败：" + err.message);
    } finally {
      el.usbReset.disabled = false;
      refresh();
    }
  });

  el.purge.addEventListener("click", async () => {
    if (!window.confirm("按留存期（1 天）清理过期照片？未过期的不会被删除。")) return;
    el.purge.disabled = true;
    try {
      const result = assertResult(await api.post("/api/operator/purge", {}));
      toast("已清理 " + result.removed_count + " 张，保留 " + result.kept + " 张");
      el.opResult.textContent = "清库完成：删除 " + result.removed_count + " 张，保留 " + result.kept + " 张";
      refresh();
    } catch (err) {
      toast("清库失败：" + err.message);
    } finally {
      el.purge.disabled = false;
    }
  });

  el.reset.addEventListener("click", async () => {
    const yes = window.confirm(
      "彩排清场：删除本机全部成片，编号从 SM-" + new Date().getFullYear() + "-0001 重新开始。\n\n" +
      "用来在活动开始前清掉彩排样张，让大屏上的「已出图」是活动的真实数字。\n" +
      "记录会归档留痕，但成片删了就找不回来。确定吗？");
    if (!yes) return;
    el.reset.disabled = true;
    try {
      const result = assertResult(await api.post("/api/operator/reset", {}));
      el.opResult.textContent = "彩排清场完成：删除 " + result.files_removed + " 个文件，" +
        "编号归零" + (result.archived ? "，记录已归档为 " + result.archived : "，无历史记录");
      toast("已清场，编号从 0001 重新开始");
    } catch (err) {
      el.opResult.textContent = "清场失败：" + err.message;
      toast("清场失败：" + err.message);
    } finally {
      el.reset.disabled = false;
      refresh();
    }
  });

  let pollTimer = null;
  let pollRunning = false;
  let initialPending = true;
  let buildTimer = null;
  let buildRunning = false;
  window.Booth.buildGuard.stop();

  async function pollBuild() {
    if (document.hidden || buildRunning) return;
    buildRunning = true;
    try { await window.Booth.buildGuard.tick(); }
    finally {
      buildRunning = false;
      if (!document.hidden) buildTimer = setTimeout(pollBuild, 20000);
    }
  }

  async function poll() {
    if (document.hidden || pollRunning) return;
    pollRunning = true;
    try {
      if (initialPending) {
        initialPending = false;
        if (page === "settings") {
          await loadConfig();
        }
        if (page === "styles") await loadAigcStyles();
        if (page === "photos") {
          await loadUploadStyles();
          try { uploadJob = sessionStorage.getItem(UPLOAD_STORAGE) || ""; } catch (err) {}
          if (uploadJob) { uploadBusy = true; uploadControls(); checkUpload(); }
          try {
            const saved = JSON.parse(sessionStorage.getItem(REGENERATE_STORAGE) || "null");
            if (saved && typeof saved.job === "string" && saved.job && saved.photo && saved.photo.token) {
              regenerateJob = saved.job;
              regeneratePhoto = saved.photo;
              if (Array.from(el.regenerateStyle.options).some((option) => option.value === saved.style)) el.regenerateStyle.value = saved.style;
              el.regeneratePortrait.value = saved.portrait_mode === "chibi" ? "chibi" : "standard";
              el.regenerateWatermark.checked = saved.ai_watermark !== false;
              regenerateBusy = true;
              el.regeneratePanel.hidden = false;
              $("#regenerate-source").textContent = "原片：" + (String(saved.photo.id || "").match(/(\d+)$/)?.[1] || "—");
              el.regenerateBadge.textContent = "恢复进度";
              regenerateControls();
              checkRegenerate();
            }
          } catch (err) {}
        }
      }
      if (page === "models") await loadAigcChannels(channelsReady);
      await refresh();
    } finally {
      pollRunning = false;
      if (!document.hidden && page !== "styles") pollTimer = setTimeout(poll, 4000);
    }
  }

  document.addEventListener("visibilitychange", () => {
    clearTimeout(pollTimer);
    clearTimeout(buildTimer);
    clearTimeout(uploadTimer);
    clearTimeout(regenerateTimer);
    if (!document.hidden) { poll(); pollBuild(); if (page === "photos") { checkUpload(); checkRegenerate(); } }
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(pollTimer);
    clearTimeout(buildTimer);
    clearTimeout(uploadTimer);
    clearTimeout(regenerateTimer);
    if (uploadPreviewUrl) URL.revokeObjectURL(uploadPreviewUrl);
  });
  window.addEventListener("beforeunload", (event) => {
    if (!editorDirty && !styleBusy && !channelsDirty && !channelsBusy && !channelTests.size) return;
    event.preventDefault();
    event.returnValue = "";
  });
  buildTimer = setTimeout(pollBuild, 20000);
  poll();
})();
