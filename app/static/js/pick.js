/* pick.js —— 取图页：保存 / 复制文案 / 分享引导 / 删除（合规要求） */
(function () {
  "use strict";
  const { $, api, toast } = window.Booth;

  /* 链接里最后一截是**取图令牌**（22 位随机串），不是编号 —— 编号连号，
     谁都能顺着数下去。编号只从 /api/photo-meta/<令牌>.json 拿回来，只用来显示。 */
  const recordToken = decodeURIComponent((location.pathname.replace(/\/+$/, "").split("/").pop() || "").trim());
  let recordId = "";
  const el = {
    pick: $("#pick"), shot: $("#shot"), serial: $("#serial"), badgeState: $("#badge-state"),
    badgeEp: $("#badge-ep"), save: $("#save"), saveClean: $("#save-clean"), share: $("#share"),
    delete: $("#delete"), gone: $("#gone"),
    qr: $("#qr"), shareQr: $("#share-qr"), print: $("#print"), printNote: $("#print-note"),
    printMedia: $("#print-media"),
    saveHint: $("#save-hint"), saveHelp: $("#save-help"), savePreview: $("#save-preview"),
    saveTitle: $("#save-preview-title"), saveImage: $("#save-preview-image"),
    saveStatus: $("#save-preview-status"), saveClose: $("#save-preview-close"),
    saveMarked: $("#save-version-marked"), saveUnmarked: $("#save-version-clean"),
    saveOpen: $("#save-open-image"), saveBrowserTip: $("#save-browser-tip"),
    saveCopy: $("#save-copy-link"), saveLink: $("#save-link"),
  };

  const photoUrl = "/api/photo/" + recordToken + ".jpg";
  const cleanPhotoUrl = "/api/photo/" + recordToken + ".clean.jpg";
  const inWechat = /MicroMessenger/i.test(navigator.userAgent);
  let cleanAvailable = false;
  let saveTrigger = null;
  const caption = () => "我在 SpacemiT DevCon 2026 拍了一张开发者证件照" +
    (recordId ? "，编号 " + recordId : "") + "。现场用 RISC-V AI CPU 实时出图。 #SpacemiTDevCon";

  /* 复制文案。明文 HTTP 下 navigator.clipboard 根本不存在（实测
     isSecureContext=false）—— 所以必须留 execCommand 这条老路，
     否则「复制文案」在手机上点了只会弹一句"请长按" 。 */
  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(text); return true; } catch (err) { /* 继续兜底 */ }
    }
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.position = "fixed";
    box.style.top = "-1000px";
    box.style.opacity = "0";
    document.body.appendChild(box);
    try {
      box.select();
      box.setSelectionRange(0, box.value.length);
      return document.execCommand("copy");
    } catch (err) {
      return false;
    } finally {
      document.body.removeChild(box);
    }
  }

  function closeSavePreview(restoreFocus) {
    el.savePreview.hidden = true;
    el.saveImage.hidden = true;
    el.saveImage.removeAttribute("src");
    el.saveOpen.hidden = true;
    el.saveOpen.removeAttribute("href");
    el.saveLink.hidden = true;
    el.saveLink.value = "";
    el.saveHelp.setAttribute("aria-expanded", "false");
    if (restoreFocus && saveTrigger && !photoGone()) saveTrigger.focus({ preventScroll: true });
  }

  function showSavePreview(clean, trigger) {
    if (photoGone() || !recordToken || (clean && !cleanAvailable)) return;
    if (trigger) saveTrigger = trigger;
    const version = clean ? "无标识版" : "带标识版";
    el.saveTitle.textContent = "保存" + version;
    el.saveMarked.setAttribute("aria-pressed", String(!clean));
    el.saveUnmarked.setAttribute("aria-pressed", String(clean));
    el.saveImage.hidden = true;
    el.saveImage.alt = "可长按保存的" + version + "证件照";
    el.saveStatus.textContent = "正在加载" + version + "图片…";
    el.saveOpen.hidden = true;
    el.saveOpen.href = clean ? cleanPhotoUrl : photoUrl;
    el.savePreview.hidden = false;
    el.saveHelp.setAttribute("aria-expanded", "true");
    el.saveImage.src = clean ? cleanPhotoUrl : photoUrl;
    if (trigger) {
      el.saveTitle.focus({ preventScroll: true });
      el.savePreview.scrollIntoView({ block: "start", behavior: "auto" });
    }
  }

  el.saveImage.addEventListener("load", () => {
    if (photoGone() || el.savePreview.hidden) return;
    el.saveImage.hidden = false;
    el.saveOpen.hidden = false;
    el.saveStatus.textContent = "长按下方图片，选择“保存图片”或“存储到照片”。";
  });
  el.saveImage.addEventListener("error", () => {
    if (photoGone() || el.savePreview.hidden) return;
    el.saveImage.hidden = true;
    el.saveOpen.hidden = true;
    el.saveStatus.textContent = "图片加载失败，请重新选择版本重试，或在浏览器中打开取图链接。";
  });
  el.saveHelp.addEventListener("click", () => showSavePreview(false, el.saveHelp));
  el.saveClose.addEventListener("click", () => closeSavePreview(true));
  el.savePreview.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSavePreview(true);
  });
  el.saveMarked.addEventListener("click", () => showSavePreview(false));
  el.saveUnmarked.addEventListener("click", () => showSavePreview(true));
  el.saveCopy.addEventListener("click", async () => {
    if (photoGone()) return;
    const url = new URL("/p/" + encodeURIComponent(recordToken), location.origin).href;
    const copied = await copyText(url);
    if (photoGone() || el.savePreview.hidden) return;
    if (copied) {
      toast("取图链接已复制");
    } else {
      el.saveLink.value = url;
      el.saveLink.hidden = false;
      el.saveLink.focus();
      el.saveLink.select();
      toast("请长按链接复制，再用浏览器打开");
    }
  });
  if (inWechat) {
    el.saveHint.hidden = false;
    el.saveBrowserTip.textContent = "没有保存选项？点微信右上角“…”选择在浏览器打开，再保存。";
    for (const [button, clean] of [[el.save, false], [el.saveClean, true]]) {
      if (!button) continue;
      button.removeAttribute("download");
      button.addEventListener("click", (event) => {
        event.preventDefault();
        showSavePreview(clean, button);
      });
    }
  }

  function showGone() {
    el.pick.classList.add("is-gone");
    el.gone.classList.remove("hide");
    el.badgeState.textContent = "已删除";
    el.badgeState.className = "badge badge-degraded";
    el.badgeState.hidden = false;
    closeSavePreview(false);
    syncPrintButton();
    if (el.save) el.save.classList.add("hide");
    if (el.saveClean) el.saveClean.classList.add("hide");
    setPrintNote("照片已删除，无法打印。");
  }

  /* 编号和 EP 徽章都只从这一个端点拿：/api/photo-meta/<令牌>.json。
     公网那一侧只放行它 —— /api/state 会连编号清单、相机状态、磁盘余量
     一起带出去，不能公开（见 docs/06）。拿不到就当作"这张已经不在了"：
     端点对无效令牌回 404，正好是删除后的同一种结局。 */
  async function loadMeta() {
    try {
      return await api.get("/api/photo-meta/" + recordToken + ".json");
    } catch (err) {
      return null;
    }
  }

  async function boot() {
    if (!recordToken) {
      toast("链接不完整");
      return;
    }
    el.save.href = photoUrl;
    if (el.saveClean) el.saveClean.href = cleanPhotoUrl;
    el.shot.src = photoUrl;
    el.shot.addEventListener("error", showGone);

    /* 取图二维码：指向**本页自身**（令牌已在 URL 里），同伴扫码就能拿到同一张。
       服务端按令牌生成 PNG 并缓存（见 booth_server 的 /api/qr/ 路由）。 */
    if (el.qr && el.shareQr) {
      el.qr.addEventListener("error", () => {
        // 生成失败就整块撤掉：留一个破图图标比给一句解释更糟。
        el.shareQr.classList.add("hide");
      });
      el.qr.src = "/api/qr/" + encodeURIComponent(recordToken) + ".png";
    }

    const meta = await loadMeta();
    if (meta) {
      recordId = meta.id || "";
      const styleNames = { "hand-painted-animation": "晴空手绘动画",
        "american-comic": "高能漫画封面", "hand-doodle": "开发者手账速写",
        "professional-suit": "未来职业肖像", "chinese-elegance": "东方新潮国风",
        "pixel-person": "复古像素头像", "korean-pop": "韩式流行风" };
      const suppliedTitle = meta.style_title || styleNames[meta.style];
      const styleTitle = suppliedTitle ? window.Booth.styleDisplay({ id: meta.style, title: suppliedTitle }).title : "";
      $("#photo-portrait-mode").textContent = (styleTitle ? styleTitle + " · " : "") + window.Booth.portraitModeLabel(meta.portrait_mode);
      const ep = !!meta.ep;
      el.badgeEp.textContent = ep ? "EP SpaceMIT" : "CPU";
      el.badgeEp.className = "badge " + (ep ? "badge-ep" : "badge-cpu");
    }
    printAvailable = !!meta && meta.print_available === true;
    if (el.printMedia) {
      el.printMedia.textContent = meta && meta.print_media_label || "54×86 mm 卡片";
    }
    syncPrintButton();
    if (photoGone()) {
      setPrintNote("照片已删除，无法打印。");
    } else if (!printAvailable) {
      const reason = meta && meta.print_unavailable_reason;
      setPrintNote(!meta ? "状态未知" :
        /未启用|未开放|disabled|not_enabled/i.test(String(reason || "")) ?
          "打印暂未开放" : "打印不可用");
    } else {
      setPrintNote("");   // 可打印时上面已有「54×86 mm 卡片」，这里再写一遍就重复了
    }
    /* 元信息拿不到，通常是"这张已经删了"。这时候别把那串随机令牌摆到
       「编号」那一栏去 —— 编号写成 —，图片自己会再报一次错、翻成"已删除"。 */
    const serial = String(recordId || "").match(/(\d+)$/)?.[1] || "—";
    el.serial.textContent = serial;
    if (!inWechat) el.save.setAttribute("download", (serial === "—" ? "devcon2026" : serial) + "-带标识.jpg");
    cleanAvailable = !!meta && meta.download_clean_available === true;
    el.saveUnmarked.hidden = !cleanAvailable;
    if (el.saveClean) {
      if (!inWechat) el.saveClean.setAttribute("download", (serial === "—" ? "devcon2026" : serial) + "-无标识.jpg");
      el.saveClean.hidden = !cleanAvailable;
    }
  }

  let printing = false;
  let printAvailable = false;
  let printJobId = "";
  let printState = "";
  let submissionUnconfirmed = false;
  const printIdempotencyKey = "pick:" + recordToken;
  const printPollInterval = 2000;
  const printPollLimit = 5 * 60 * 1000;

  function photoGone() {
    return el.pick.classList.contains("is-gone");
  }

  function syncPrintButton() {
    if (!el.print) return;
    const terminal = ["completed", "failed", "unavailable"].includes(printState);
    el.print.disabled = photoGone() || !recordToken || printing || terminal || (!printJobId && !printAvailable);
    el.print.setAttribute("aria-disabled", String(el.print.disabled));
    el.print.setAttribute("aria-busy", String(printing));
    if (photoGone()) el.print.textContent = "照片已删除";
    else if (printState === "completed") el.print.textContent = "打印已完成";
    else if (printState === "failed" || printState === "unavailable") el.print.textContent = "请联系工作人员";
    else if (printing) el.print.textContent = printJobId ? "正在确认状态…" : "正在提交…";
    else if (printJobId) el.print.textContent = "查询打印状态";
    else if (submissionUnconfirmed) el.print.textContent = "确认打印请求";
    else if (!printAvailable) el.print.textContent = "打印暂未开放";
    else el.print.textContent = "开始打印";
  }

  function setPrintNote(text, kind) {
    if (!el.printNote) return;
    const content = text || "";
    const className = "note print-note" + (kind ? " is-" + kind : "");
    if (el.printNote.textContent !== content) el.printNote.textContent = content;
    if (el.printNote.className !== className) el.printNote.className = className;
  }

  function showPrintJob(data) {
    if (photoGone()) return true;
    const job = data && data.print_job;
    const status = String(job && job.status || "").toLowerCase();
    if (status === "completed") {
      printState = status;
      setPrintNote("打印完成", "done");
      return true;
    }
    if (status === "failed" || status === "unavailable") {
      printState = status;
      setPrintNote(status === "failed" ? "打印失败" : "打印不可用", "error");
      return true;
    }
    if (status === "queued" || status === "submitted") {
      printState = status;
      setPrintNote(status === "queued" ? "排队中" : "打印中");
    } else {
      printState = "confirming";
      setPrintNote("确认中");
    }
    return false;
  }

  async function queryPrintJob(jobId, deadline) {
    let timer;
    try {
      return await Promise.race([
        api.get("/api/print/" + encodeURIComponent(jobId)),
        new Promise((resolve, reject) => {
          timer = setTimeout(() => reject(new Error("timeout")), Math.max(0, deadline - Date.now()));
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  async function pollPrintJob(jobId) {
    const deadline = Date.now() + printPollLimit;
    while (!photoGone() && Date.now() < deadline) {
      try {
        const data = await queryPrintJob(jobId, deadline);
        if (Date.now() >= deadline || photoGone()) break;
        if (showPrintJob(data)) return;
      } catch (err) {
        if (photoGone()) return;
        setPrintNote("确认中");
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await new Promise((resolve) => setTimeout(resolve, Math.min(printPollInterval, remaining)));
    }
    if (!photoGone()) {
      printState = "timeout";
      setPrintNote("确认已暂停");
    }
  }

  async function submitPrint() {
    if (printing || photoGone() || !recordToken ||
      ["completed", "failed", "unavailable"].includes(printState) || (!printJobId && !printAvailable)) return;
    printing = true;
    syncPrintButton();
    try {
      if (printJobId) {
        setPrintNote("确认中");
        await pollPrintJob(printJobId);
        return;
      }
      setPrintNote(submissionUnconfirmed ? "确认中" : "提交中");
      const data = await api.post("/api/print", {
        photo_token: recordToken,
        idempotency_key: printIdempotencyKey,
      });
      if (photoGone()) return;
      const job = data && data.print_job;
      if (job && typeof job.print_job_id === "string" && job.print_job_id) {
        printJobId = job.print_job_id;
        submissionUnconfirmed = false;
        syncPrintButton();
        if (!showPrintJob(data)) await pollPrintJob(printJobId);
      } else {
        submissionUnconfirmed = true;
        setPrintNote("确认中");
      }
    } catch (err) {
      if (photoGone()) return;
      const status = err && err.status;
      if (status === 503 && !submissionUnconfirmed) {
        printAvailable = false;
        setPrintNote("打印不可用", "error");
      } else if ((status === 404 || status === 410) && !submissionUnconfirmed) {
        showGone();
      } else {
        submissionUnconfirmed = true;
        setPrintNote("确认中");
      }
    } finally {
      printing = false;
      syncPrintButton();
    }
  }

  if (el.print) el.print.addEventListener("click", submitPrint);
  const printGuard = new MutationObserver(() => {
    if (photoGone()) {
      closeSavePreview(false);
      syncPrintButton();
      setPrintNote("照片已删除，无法打印。");
    }
  });
  printGuard.observe(el.pick, { attributes: true, attributeFilter: ["class"] });
  syncPrintButton();

  el.share.addEventListener("click", async () => {
    if (photoGone()) return;
    const text = caption();
    const link = new URL("/p/" + encodeURIComponent(recordToken), location.origin).href;
    if (navigator.canShare) {
      try {
        const blob = await (await fetch(photoUrl, { cache: "no-store" })).blob();
        const file = new File([blob], (recordId || "devcon2026") + ".jpg", { type: "image/jpeg" });
        if (navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], text: text });
          return;
        }
      } catch (err) { /* 取消或不被支持，走兜底 */ }
    }
    if (await copyText(link + "\n" + text)) {
      toast("链接和文案已复制，打开微信或小红书粘贴");
      return;
    }
    showSavePreview(false, el.share);
    toast("请先保存图片，再到微信或小红书选择图片发布");
  });

  el.delete.addEventListener("click", async () => {
    if (!window.confirm("确定删除这张照片？删除后链接立即失效，不可恢复。")) return;
    try {
      await api.del("/api/photo/" + recordToken);
      showGone();
      toast("已删除，链接已失效");
    } catch (err) {
      toast("删除失败：" + err.message);
    }
  });

  boot();
})();
