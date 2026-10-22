/* auto-flow.js —— 自动流程前端（T-4）：入口风格轮播、手机排队报名、队列状态轮询。
 *
 * 覆盖两个页面（同一份文件，按 <body data-af-page="entry|join"> 自动挂载）：
 *   - 入口页 index.html：风格示意自动轮播（至少两款有效本地示意）、报名二维码
 *     （GET /api/join-qr.png，失败降级显示同源 /join 地址文本）、同屏幕代为报名
 *     （拍摄同意 + 可留空字段 + 风格提交），提交后显示队列状态并每 2 秒轮询；
 *   - 手机页 join.html：扫码进来后选风格、填（可留空）信息、同意拍摄、提交排队，
 *     排队状态默认每 2 秒轮询 GET /api/queue/<id>（带 X-Queue-Session），到号显示
 *     「开始拍摄」按钮 → POST /api/queue/<id>/start（带同一拍摄同意）。
 *
 * 关键契约（与 自动流程规格 v1（内部文档）、T-2/T-12/T-20 一致）：
 * - 风格目录不硬编码数量：GET /api/styles（T-16 新增）→ 失败回退现有
 *   GET /api/aigc/styles → 再失败用内嵌静态清单（最后退路，仅为降级展示）；
 * - 轮播只改自己的展示区，绝不触碰页面表单/画廊状态（交互控件不因轮播重置）；
 * - 提交体 {request_id, style_id, optional_fields?, capture_consent_id}；可选字段全空
 *   时**整体省略** optional_fields 键（空字段留空合法）；若服务端 400 提示字段未启用，
 *   自动用同一 request_id 去掉 optional_fields 重试一次并如实告知用户；
 * - 收到 403 CONSENT_REQUIRED（同意失效/已被绑定/幂等边界）：必须**换新 request_id**
 *   并重新走一次「参与者点击同意」——同 request_id 重放会拿回已取消条目（T-2 记录）。
 *   JS 不自动代签同意：任何时候都要求用户再点一次同意按钮；
 * - 队列会话（queue_id + session + 同意令牌）存 sessionStorage：扫码页与报名都在
 *   「同一个标签页会话」里完成，关掉标签页即失效（不落 cookie、不落 localStorage，
 *   不跨标签页共享，缩小凭据驻留面）；读取时兼容旧键迁移，坏数据一律当没有；
 * - 无后台推送：手机页每 2 秒自己轮询，页面文案明确写出这一点；
 * - 无框架、无 CDN、无外部字体，断网（LAN 可达）可跑；
 * - 纯逻辑（轮播索引、提交体构造、响应解释、重试决策、轮询状态机、session 存取）
 *   不碰 DOM，Node 可直接 require：module.exports = api；浏览器挂 window.AutoFlowEntry。
 */
(function (root) {
  "use strict";

  var VERSION = "auto-flow-t4-1";
  var CAROUSEL_INTERVAL_MS = 5000;   // 轮播切换间隔（浏览器计时断言以此为基准）
  var POLL_INTERVAL_MS = 2000;       // 队列状态默认轮询间隔（规格：每 2 秒）
  var POLL_BACKOFF_MAX_MS = 15000;   // 网络错误退避上限
  var presentationMode = "full";
  var STORE_KEY = "autoflow.queue.v1";
  var RECOVERY_SKIP_KEY = "autoflow.recovery.skip.v1";
  var LEGACY_STORE_KEYS = ["autoflow.queue"];
  var STYLE_ENDPOINTS = ["/api/styles", "/api/aigc/styles"];
  var STYLE_ASSET_BASE = "/static/assets/styles/";

  /* 客户端候选可选字段键。服务端是否收集由 BOOTH_OPTIONAL_FIELDS_ALLOWLIST 决定
     （默认空 = 不收集）：提交遇到 400 时自动去字段重试，见 createSubmitController。 */
  var OPTIONAL_FIELD_KEYS = ["display_name", "developer_id"];

  /* 内嵌静态清单：两个风格接口都不可用时的最后退路。 */
  var STATIC_STYLE_FALLBACK = [
    { id: "hand-painted-animation", title: "晴空手绘动画", description: "清透天空、自然光线与细腻手绘质感。" },
    { id: "american-comic", title: "高能漫画封面", description: "硬朗墨线、半色调与鲜明封面张力。" },
    { id: "hand-doodle", title: "开发者手账速写", description: "钢笔速写、局部彩绘与轻巧开发者元素。" },
    { id: "professional-suit", title: "未来职业肖像", description: "精致西装造型与克制的未来影像质感。" },
    { id: "chinese-elegance", title: "东方新潮国风", description: "现代东方剪裁、雅致配色与新潮国风表达。" },
    { id: "pixel-person", title: "复古像素头像", description: "经典像素色块、清晰轮廓与复古游戏质感。" },
  ];

  /* 兜底中文文案：只在 T-20 的 auto-flow-messages.js 未加载（如 Node 纯测试）时生效；
     与 app/auto_flow/errors.py 逐字一致，两边改动需同步。 */
  var FALLBACK_MESSAGES = {
    INVALID_REQUEST: "请求格式不正确，请检查后重试。",
    CONSENT_REQUIRED: "缺少有效的拍摄授权，请先同意拍摄。",
    STYLE_NOT_FOUND: "所选风格不存在。",
    IDEMPOTENCY_CONFLICT: "该请求编号已用于另一份提交，请刷新后重试。",
    QUEUE_FULL: "当前排队人数已满，请稍后再试。",
    QUEUE_EXPIRED: "排队已超时，请重新提交。",
    NOT_CALLED: "还未轮到本次拍摄，请等待叫号。",
    CAMERA_UNAVAILABLE: "相机暂时不可用，请找现场工作人员。",
    CAPTURE_FAILED: "拍摄失败，未生成照片；请重试或找工作人员。",
    PHOTO_DELETED: "照片已被删除。",
    QUEUE_NOT_FOUND: "排队记录不存在。",
    INTERNAL_ERROR: "服务内部错误，请稍后重试。",
  };
  var GENERIC_MESSAGE = "出了点小问题，请稍后重试或找现场工作人员。";

  var TERMINAL_STATUSES = ["expired", "cancelled", "failed"];
  var STOP_POLL_STATUSES = TERMINAL_STATUSES.concat(["ready_for_pickup"]);

  var STATUS_VIEWS = {
    queued: { tone: "waiting", title: "已排队", detail: "还没到号，留在本页即可，到号时会自动更新。" },
    called: { tone: "called", title: "到号了！", detail: "请马上到拍摄位，点「开始拍摄」。" },
    ready: { tone: "active", title: "拍摄位已就绪", detail: "请看向镜头，按现场大屏提示操作。" },
    capturing: { tone: "active", title: "正在拍摄", detail: "请保持一下，马上就好。" },
    processing: { tone: "active", title: "拍摄已完成", detail: "照片正在生成，请留在本页，完成后回来取图并打印。" },
    ready_for_pickup: { tone: "done", title: "成片已就绪", detail: "点下方「查看我的证件照」，保存或删除。" },
    expired: { tone: "error", title: "排队已超时", detail: "请重新报名排队。" },
    cancelled: { tone: "error", title: "排队已取消", detail: "请重新报名排队。" },
    failed: { tone: "error", title: "生成未完成", detail: "本次照片生成未完成，请重新报名再试，或找现场工作人员。" },
  };
  var QUEUE_MISSING_VIEW = {
    tone: "error", title: "排队访问已失效",
    detail: "本页已无法访问这次排队记录，请重新报名或找现场工作人员。",
    terminal: true, canStart: false, canAbandon: false,
  };

  /* 旧扁平错误码 → 新稳定错误码；只映射队列/同意相关且在既有路由真实出现过的值。 */
  var LEGACY_CODE_MAP = {
    consent_required: "CONSENT_REQUIRED",
    gone: "PHOTO_NOT_FOUND",
    not_found: "QUEUE_NOT_FOUND",
    unknown_job: "JOB_NOT_FOUND",
    internal: "INTERNAL_ERROR",
    bad_url: "INVALID_REQUEST",
    invalid_style: "INVALID_REQUEST",
  };

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function trim(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function displayNumberText(value) {
    var match = trim(value).match(/(\d+)$/);
    return match ? match[1] : "";
  }

  function messageFor(code) {
    if (root.AutoFlowMessages && typeof root.AutoFlowMessages.messageFor === "function") {
      var external = root.AutoFlowMessages.messageFor(code);
      if (external && external !== root.AutoFlowMessages.fallback) return external;
    }
    if (typeof code === "string" && Object.prototype.hasOwnProperty.call(FALLBACK_MESSAGES, code)) {
      return FALLBACK_MESSAGES[code];
    }
    return GENERIC_MESSAGE;
  }
  /* ------------------------------------------------------------------ */
  /* 纯逻辑：request_id / 风格目录 / 轮播                                  */
  /* ------------------------------------------------------------------ */

  /* 客户端幂等键。换新 request_id 是 403/已取消重试的关键动作（见 nextSubmitAction）。 */
  function newRequestId(options) {
    var opts = options || {};
    var now = typeof opts.nowMs === "number" ? opts.nowMs : Date.now();
    var random = typeof opts.random === "function" ? opts.random : Math.random;
    var a = Math.floor(random() * 0xffffffff) >>> 0;
    var b = Math.floor(random() * 0xffffffff) >>> 0;
    var c = Math.floor(random() * 0xffffffff) >>> 0;
    function pad(value) { while (value.length < 8) value = "0" + value; return value; }
    return "af-" + now.toString(36) + "-" + pad(a.toString(36)) + pad(b.toString(36)) + pad(c.toString(36));
  }

  /* 缩略图候选：显式 thumb 字段优先（相对文件名自动补资产前缀），其余按本仓库
     既有命名逐个探测（`-thumb.png` / `-aigc-thumb.png` / `<id>.png`）。 */
  function thumbCandidatesFor(styleId, explicit, mode) {
    var list = [];
    var seen = {};
    function push(url) {
      if (url && root.Booth && typeof root.Booth.versionedAssetUrl === "function") {
        url = root.Booth.versionedAssetUrl(url);
      }
      if (!url || seen[url]) return;
      seen[url] = true;
      list.push(url);
    }
    var id = trim(styleId);
    if (id && normalizePortraitMode(mode) === "chibi") {
      push(STYLE_ASSET_BASE + encodeURIComponent(id) + "-chibi-preview.jpg");
    }
    if (typeof explicit === "string" && explicit.trim()) {
      var value = explicit.trim();
      push(value.indexOf("/") === 0 || /^https?:/i.test(value) ? value : STYLE_ASSET_BASE + value);
    }
    if (id) {
      var safe = encodeURIComponent(id);
      push(STYLE_ASSET_BASE + safe + "-preview.jpg");
      push(STYLE_ASSET_BASE + safe + "-thumb.png");
      push(STYLE_ASSET_BASE + safe + "-aigc-thumb.png");
      push(STYLE_ASSET_BASE + safe + ".png");
    }
    return list;
  }

  /* 把 /api/styles、/api/aigc/styles 或内嵌清单统一成条目数组；不合法条目直接跳过。
     返回顺序即目录顺序，数量完全由数据决定（不硬编码「10 款」之类的常量）。 */
  function normalizeStyleList(payload) {
    var raw = payload;
    if (isObject(payload) && Array.isArray(payload.styles)) raw = payload.styles;
    if (!Array.isArray(raw)) return [];
    var out = [];
    var seen = {};
    raw.forEach(function (item) {
      if (!isObject(item)) return;
      var id = trim(item.id) || trim(item.name) || trim(item.style_id);
      if (!id || seen[id]) return;
      seen[id] = true;
      /* {name,label} 是 /api/state 的风格形状（name 即 id）；{id,name} 同时出现时
         name 更可能是标题。两种形状都兼容。 */
      var nameAsTitle = trim(item.name) && trim(item.name) !== id ? trim(item.name) : "";
      var title = trim(item.title) || trim(item.label) || nameAsTitle || id;
      var description = trim(item.description);
      if (id === "pixel-person") {
        if (title === "Q版像素风" || title === "像素风") title = "复古像素头像";
        description = description.replace(/大头小身体的软萌像素小人|全身小人，大头小身体/g, "像素色块与清晰轮廓");
      }
      var thumb = item.thumb || item.thumbnail || item.thumb_url || "";
      out.push({
        id: id,
        title: title,
        description: description,
        orientation: trim(item.default_orientation),
        thumb: trim(thumb),
        thumbCandidates: thumbCandidatesFor(id, thumb),
      });
    });
    return out;
  }

  function carouselNextIndex(index, count) {
    var total = Math.floor(Number(count) || 0);
    if (total <= 0) return 0;
    var current = Math.floor(Number(index) || 0);
    current = ((current % total) + total) % total;
    return (current + 1) % total;
  }

  function defaultTimers() {
    return {
      setTimeout: function (fn, ms) { return root.setTimeout(fn, ms); },
      clearTimeout: function (id) { root.clearTimeout(id); },
      setInterval: function (fn, ms) { return root.setInterval(fn, ms); },
      clearInterval: function (id) { root.clearInterval(id); },
    };
  }

  /* 轮播模型：只维护 index/count，并把每次切换广播给订阅者（展示区渲染器）。
     注入 timers 便于 Node 假时钟测试；count < 2 时不启动定时器（无从轮播）。 */
  function createCarousel(options) {
    var opts = options || {};
    var count = Math.max(0, Math.floor(Number(opts.count) || 0));
    var intervalMs = Number(opts.intervalMs) > 0 ? Math.floor(opts.intervalMs) : CAROUSEL_INTERVAL_MS;
    var timers = opts.timers || defaultTimers();
    var listeners = [];
    var index = 0;
    var timer = null;

    function emit(reason) {
      var snapshot = { index: index, count: count, reason: reason };
      listeners.forEach(function (listener) {
        try { listener(snapshot); } catch (err) { /* 渲染失败不影响轮播心跳 */ }
      });
    }

    function setIndex(value) {
      if (count <= 0) return 0;
      var next = Math.floor(Number(value) || 0);
      next = ((next % count) + count) % count;
      if (next !== index) {
        index = next;
        emit("set");
      }
      return index;
    }

    var carousel = {
      count: function () { return count; },
      index: function () { return index; },
      snapshot: function () { return { index: index, count: count }; },
      setIndex: setIndex,
      advance: function () {
        if (count <= 0) return 0;
        index = (index + 1) % count;
        emit("advance");
        return index;
      },
      subscribe: function (listener) {
        if (typeof listener === "function") listeners.push(listener);
        return function unsubscribe() {
          var at = listeners.indexOf(listener);
          if (at >= 0) listeners.splice(at, 1);
        };
      },
      start: function () {
        if (timer !== null || count < 2) return timer !== null;
        timer = timers.setInterval(function () { carousel.advance(); }, intervalMs);
        return true;
      },
      stop: function () {
        if (timer === null) return false;
        timers.clearInterval(timer);
        timer = null;
        return true;
      },
      isRunning: function () { return timer !== null; },
      intervalMs: intervalMs,
    };
    return carousel;
  }
  /* ------------------------------------------------------------------ */
  /* 纯逻辑：提交体 / 响应解释 / 重试决策                                  */
  /* ------------------------------------------------------------------ */

  /* 只收集允许清单内、且值非空的字段；全空返回 null = 不发送 optional_fields 键。 */
  function collectOptionalFields(values, allowlist) {
    var source = isObject(values) ? values : {};
    var keys = Array.isArray(allowlist) ? allowlist : OPTIONAL_FIELD_KEYS;
    var out = null;
    keys.forEach(function (key) {
      var value = typeof source[key] === "string" ? source[key].trim() : "";
      if (!value) return;
      if (!out) out = {};
      out[key] = value;
    });
    return out;
  }

  /* 构造 POST /api/queue 请求体；缺必填或字段非法返回 {ok:false, error}（不抛错）。 */
  function normalizePortraitMode(value) {
    return value === "chibi" ? "chibi" : "standard";
  }

  function portraitModeLabel(value) {
    return normalizePortraitMode(value) === "chibi" ? "Q版大头像" : "写实版";
  }

  function entryStyleLabel(entry, styles) {
    var style = styles.find(function (item) { return item.id === entry.styleId; });
    return (style ? style.title + " · " : "") + portraitModeLabel(entry.portraitMode);
  }

  function buildQueueSubmission(input) {
    var src = input || {};
    var requestId = trim(src.requestId);
    var styleId = trim(src.styleId);
    var consentId = trim(src.consentId || src.captureConsentId);
    if (!requestId) return { ok: false, error: "缺少 request_id（内部错误），请重试。" };
    if (!styleId) return { ok: false, error: "请先选择一款风格。" };
    if (!consentId) return { ok: false, error: "还没同意拍摄：请先阅读拍摄告知并点「同意」。" };
    var body = { request_id: requestId, style_id: styleId, capture_consent_id: consentId,
      portrait_mode: normalizePortraitMode(src.portraitMode), ai_watermark: src.ai_watermark !== false };
    if (!src.omitOptionalFields) {
      var fields = collectOptionalFields(src.optionalFields || {}, src.optionalFieldAllowlist);
      if (fields) body.optional_fields = fields;
    }
    return { ok: true, body: body };
  }

  /* 统一抽错误码：新契约 {error:{code,message,request_id}} 与既有扁平
     {ok:false, error:"...", detail:"..."} 都要认。 */
  function extractError(status, payload) {
    var body = isObject(payload) ? payload : {};
    var code = "";
    var message = "";
    var requestId = "";
    if (isObject(body.error)) {
      code = trim(body.error.code);
      message = trim(body.error.message);
      requestId = trim(body.error.request_id);
    } else if (typeof body.error === "string" && body.error) {
      code = LEGACY_CODE_MAP[body.error] || body.error.toUpperCase();
      message = trim(body.detail);
    }
    if (!code && typeof status === "number" && status >= 400) code = "INTERNAL_ERROR";
    if (!message) message = messageFor(code);
    return { code: code, message: message, requestId: requestId };
  }

  /* QueueEntry → 前端使用的扁平结构；缺 queue_id 视为坏数据返回 null。 */
  function normalizeEntry(raw) {
    if (!isObject(raw)) return null;
    var queueId = trim(raw.queue_id || raw.queueId);
    var status = trim(raw.status);
    if (!queueId || !status) return null;
    var pickupToken = trim(raw.photo_token || raw.photoToken);
    return {
      queueId: queueId,
      requestId: trim(raw.request_id || raw.requestId),
      status: status,
      styleId: trim(raw.style_id || raw.styleId),
      portraitMode: normalizePortraitMode(raw.portrait_mode || raw.portraitMode),
      ai_watermark: raw.ai_watermark !== false,
      displayNumber: trim(raw.display_number || raw.displayNumber),
      createdAt: trim(raw.created_at || raw.createdAt),
      calledAt: trim(raw.called_at || raw.calledAt),
      startDeadline: trim(raw.start_deadline || raw.startDeadline || raw.called_deadline),
      startedAt: trim(raw.started_at || raw.startedAt),
      expiredAt: trim(raw.expired_at || raw.expiredAt),
      pickupUrl: pickupToken ? "/p/" + encodeURIComponent(pickupToken) : "",
    };
  }

  function parseUtcMs(value) {
    if (typeof value !== "string" || !value) return null;
    var ms = Date.parse(value);
    return isFinite(ms) ? ms : null;
  }

  /* 取图链接只认令牌。QueueEntry 按规格不带 photo_token（photo_token 属于
     CaptureJob，见 自动流程规格 v1（内部文档） 的 Data models），所以成片就绪后
     要另外问一次 GET /api/job/<queue_id>（带 X-Queue-Session）才拿得到令牌。
     只回 /p/<token>，不把令牌本身暴露给页面其它部分。缺令牌时返回 ""。 */
  function pickupPathFromJob(payload) {
    var body = isObject(payload) ? payload : {};
    var job = isObject(body.job) ? body.job : null;
    var token = job ? trim(job.photo_token || job.photoToken) : "";
    return token ? "/p/" + encodeURIComponent(token) : "";
  }

  function interpretQueueEntryResponse(status, payload) {
    var body = isObject(payload) ? payload : {};
    if (status === 200 && body.entry) {
      var entry = normalizeEntry(body.entry);
      if (entry) {
        return { ok: true, status: status, entry: entry, view: queueStatusView(entry),
                 code: "", message: "", stop: shouldStopPolling(entry) };
      }
    }
    var error = extractError(status, body);
    var stop = (typeof status === "number" && status === 404) || error.code === "QUEUE_NOT_FOUND";
    return { ok: false, status: status, entry: null, view: null,
             code: error.code, message: error.message, stop: stop };
  }

  function interpretSubmitResponse(status, payload) {
    var body = isObject(payload) ? payload : {};
    if (!status) return { ok: false, status: 0, created: false, entry: null, session: "",
      code: "NETWORK", message: "网络连接不稳定，暂时无法确认报名结果，请重试或找现场工作人员。" };
    var error = extractError(status, body);
    var failed = typeof status === "number" ? status >= 400 : true;
    if (failed) {
      return { ok: false, status: status, created: false, entry: null, session: "",
               code: error.code, message: error.message };
    }
    var entry = normalizeEntry(body.entry);
    if (!entry) {
      return { ok: false, status: status, created: false, entry: null, session: "",
               code: "INVALID_REQUEST", message: messageFor("INVALID_REQUEST") };
    }
    return { ok: true, status: status, created: status === 201, entry: entry,
             session: trim(body.session), recoveryUrl: recoveryPath(body.recovery_url),
             pickupUrl: pickupPath(body.pickup_url), code: "", message: "" };
  }

  function safeLinkPath(value, pattern) {
    var source = trim(value);
    if (!source) return "";
    try {
      var origin = root.location ? root.location.origin : "http://localhost";
      var parsed = new URL(source, origin);
      if (parsed.origin !== origin || parsed.username || parsed.password || parsed.search) return "";
      var path = parsed.pathname + parsed.hash;
      return pattern.test(path) ? path : "";
    } catch (err) { return ""; }
  }

  function recoveryPath(value) {
    return safeLinkPath(value, /^\/join#restore=[A-Za-z0-9_-]{32}$/);
  }

  function pickupPath(value) {
    return safeLinkPath(value, /^\/p\/[A-Za-z0-9_-]{22}$/);
  }

  /* 提交失败/异常后的下一步决策（纯函数）：
   * - 403（同意失效）与「重放拿回已取消条目」都必须换新 request_id，且必须由参与者
   *   重新点击同意——这里只给出动作，绝不自动补签同意；
   * - 400 且本次带了 optional_fields：服务端未启用该字段收集（默认空允许清单），
   *   用同一 request_id 去掉字段重试一次（400 发生在建档之前，不产生幂等记录）。 */
  function nextSubmitAction(result, context) {
    var ctx = context || {};
    if (result && result.ok) {
      var status = result.entry && result.entry.status;
      if (status === "expired" || status === "cancelled" || status === "failed") {
        return { action: "resubmit", message: "之前的排队已" + (status === "expired" ? "超时" : "失效") +
          "，请重新点击同意后再提交（会换用新的请求编号）。" };
      }
      return { action: "accept", message: "" };
    }
    var code = (result && result.code) || "";
    if (code === "CONSENT_REQUIRED") {
      return { action: "reconsent", message: "拍摄授权已失效或被占用，请再点一次同意后重新提交。" };
    }
    if (code === "INVALID_REQUEST" && ctx.hadOptionalFields && !ctx.fieldsDropped) {
      return { action: "retry_without_fields", message: "现场未启用个人信息收集，改为不带上个人信息重试。" };
    }
    return { action: "stop", message: (result && result.message) || messageFor(code) };
  }

  /* 队列状态 → 中文视图模型；到号时带剩余秒数（start_deadline）。 */
  function queueStatusView(entry, nowMs) {
    var status = (entry && entry.status) || "queued";
    var base = STATUS_VIEWS[status] || {
      tone: "waiting", title: "排队状态更新", detail: "当前状态：" + status,
    };
    var terminal = TERMINAL_STATUSES.indexOf(status) >= 0;
    var view = {
      status: status,
      tone: base.tone,
      title: base.title,
      detail: base.detail,
      terminal: terminal,
      canStart: status === "called",
      canAbandon: !terminal && status !== "capturing" && status !== "processing" && status !== "ready_for_pickup",
      deadlineSeconds: null,
    };
    if (status === "called" && entry && entry.startDeadline) {
      var deadline = parseUtcMs(entry.startDeadline);
      if (deadline !== null) {
        var now = typeof nowMs === "number" ? nowMs : Date.now();
        var left = Math.max(0, Math.ceil((deadline - now) / 1000));
        view.deadlineSeconds = left;
        view.detail = base.detail + "（请尽快，等待窗口约剩 " + left + " 秒）";
        if (left === 0) {
          view.canStart = false;
          view.title = "叫号时间已到";
          view.detail = "正在确认排队状态，请稍候…";
        }
      }
    }
    if (status === "processing" && presentationMode === "handoff") {
      view.detail = "AIGC 正在后台继续生成，请关注本页，完成后回来取图并打印。";
    }
    return view;
  }

  function shouldStopPolling(entry) {
    var status = entry && typeof entry === "object" ? entry.status : entry;
    return STOP_POLL_STATUSES.indexOf(status) >= 0;
  }

  /* 轮询间隔：正常固定 base（默认 2 秒）；网络错误按 2^n 退避，封顶 15 秒。 */
  function pollDelayFor(entry, baseMs, attempt) {
    var base = Number(baseMs) > 0 ? Math.floor(baseMs) : POLL_INTERVAL_MS;
    var count = Math.max(0, Math.floor(Number(attempt) || 0));
    if (!count) return base;
    return Math.min(base * Math.pow(2, count), POLL_BACKOFF_MAX_MS);
  }

  /* 轮询器：fetchEntry() -> Promise<{status,payload}>；timers 可注入（Node 假时钟）。
   * 终态/记录消失（404）自动停止；网络错误继续退避重试（不惊扰参与者）。 */
  function createQueuePoller(options) {
    var opts = options || {};
    var intervalMs = Number(opts.intervalMs) > 0 ? Math.floor(opts.intervalMs) : POLL_INTERVAL_MS;
    var timers = opts.timers || defaultTimers();
    var running = false;
    var timer = null;
    var attempt = 0;
    var stoppedReason = "";
    var generation = 0;
    var inFlight = null;

    function schedule(delay) {
      if (!running) return;
      timer = timers.setTimeout(function () { timer = null; tick(); }, delay);
    }

    function stop(reason) {
      generation += 1;
      inFlight = null;
      running = false;
      if (timer !== null) {
        timers.clearTimeout(timer);
        timer = null;
      }
      stoppedReason = reason || "stop";
      if (typeof opts.onStop === "function") opts.onStop(stoppedReason);
    }

    function tick() {
      if (!running) return Promise.resolve(null);
      if (inFlight) return inFlight;
      if (timer !== null) { timers.clearTimeout(timer); timer = null; }
      var currentGeneration = generation;
      var request = Promise.resolve()
        .then(function () {
          if (!running || generation !== currentGeneration) return null;
          return opts.fetchEntry();
        })
        .then(function (response) {
          if (!running || generation !== currentGeneration) return null;
          var out = interpretQueueEntryResponse(
            (response && response.status) || 0, response && response.payload);
          if (out.ok) {
            attempt = 0;
            if (typeof opts.onUpdate === "function") opts.onUpdate(out);
            if (out.stop) { stop("terminal"); return out; }
            schedule(intervalMs);
            return out;
          }
          if (out.stop) {
            if (typeof opts.onUpdate === "function") opts.onUpdate(out);
            stop("gone");
            return out;
          }
          attempt += 1;
          if (typeof opts.onError === "function") opts.onError(out);
          schedule(pollDelayFor(null, intervalMs, attempt));
          return out;
        }, function () {
          if (!running || generation !== currentGeneration) return null;
          attempt += 1;
          var out = { ok: false, status: 0, entry: null, view: null, stop: false,
                      code: "NETWORK", message: "网络连接不稳定，正在自动重试…" };
          if (typeof opts.onError === "function") opts.onError(out);
          schedule(pollDelayFor(null, intervalMs, attempt));
          return out;
        }).then(function (out) {
          if (generation === currentGeneration) inFlight = null;
          return out;
        });
      inFlight = request;
      return request;
    }

    return {
      start: function () {
        if (running) return;
        running = true;
        generation += 1;
        attempt = 0;
        stoppedReason = "";
        tick();
      },
      stop: stop,
      tick: tick,
      isRunning: function () { return running; },
      attempts: function () { return attempt; },
      stoppedReason: function () { return stoppedReason; },
      intervalMs: intervalMs,
    };
  }
  /* ------------------------------------------------------------------ */
  /* 纯逻辑：开始拍摄 / session 存取 / 同源地址                            */
  /* ------------------------------------------------------------------ */

  function buildStartRequest(consentId) {
    var id = trim(consentId);
    if (!id) return { ok: false, error: "缺少拍摄同意，请先同意拍摄后再开始。" };
    return { ok: true, body: { capture_consent_id: id } };
  }

  function interpretStartResponse(status, payload) {
    var body = isObject(payload) ? payload : {};
    var error = extractError(status, body);
    if (typeof status === "number" && (status === 200 || status === 202) && body.entry) {
      var entry = normalizeEntry(body.entry);
      if (entry) {
        return { ok: true, status: status, entry: entry, gestureEnabled: Boolean(body.gesture_enabled),
                 code: "", message: "", needsConsent: false, cameraUnavailable: false };
      }
    }
    return {
      ok: false,
      status: status,
      entry: null,
      code: error.code,
      message: error.message,
      needsConsent: error.code === "CONSENT_REQUIRED",
      cameraUnavailable: error.code === "CAMERA_UNAVAILABLE",
    };
  }

  function validSessionShape(value) {
    return isObject(value) && trim(value.queueId) && trim(value.session);
  }

  /* sessionStorage 可能因隐私模式抛错：统一降级成内存假存储，流程不中断。 */
  function safeStorage(storage) {
    var candidate = storage;
    if (!candidate && typeof root.sessionStorage !== "undefined") {
      try {
        root.sessionStorage.setItem("__af_probe__", "1");
        root.sessionStorage.removeItem("__af_probe__");
        candidate = root.sessionStorage;
      } catch (err) { candidate = null; }
    }
    if (candidate) return candidate;
    var memory = {};
    return {
      getItem: function (key) { return Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : null; },
      setItem: function (key, value) { memory[key] = String(value); },
      removeItem: function (key) { delete memory[key]; },
    };
  }

  /* 读队列会话：只认形状正确的 JSON；旧键命中后迁移到新键并删除旧键；坏数据当没有。 */
  function readQueueSession(storage) {
    var store = safeStorage(storage);
    var found = null;
    try {
      var raw = store.getItem(STORE_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (validSessionShape(parsed)) found = { source: STORE_KEY, value: parsed };
      }
    } catch (err) { found = null; }
    if (!found) {
      for (var i = 0; i < LEGACY_STORE_KEYS.length; i += 1) {
        try {
          var legacyRaw = store.getItem(LEGACY_STORE_KEYS[i]);
          if (!legacyRaw) continue;
          var legacy = JSON.parse(legacyRaw);
          if (validSessionShape(legacy)) { found = { source: LEGACY_STORE_KEYS[i], value: legacy }; }
        } catch (err2) { /* 坏数据：跳过 */ }
        if (found) break;
      }
    }
    if (!found) return null;
    var value = found.value;
    var normalized = {
      queueId: trim(value.queueId),
      session: trim(value.session),
      consentId: trim(value.consentId),
      recoverUrl: recoveryPath(value.recoverUrl),
      pickupUrl: pickupPath(value.pickupUrl),
      styleId: trim(value.styleId),
      portraitMode: normalizePortraitMode(value.portraitMode || (value.entry && (value.entry.portrait_mode || value.entry.portraitMode))),
      ai_watermark: typeof value.ai_watermark === "boolean" ? value.ai_watermark : !(value.entry && value.entry.ai_watermark === false),
      displayNumber: trim(value.displayNumber || (value.entry && value.entry.display_number)),
      status: trim(value.status || (value.entry && value.entry.status)),
      submittedAt: typeof value.submittedAt === "number" ? value.submittedAt : null,
      entry: isObject(value.entry) ? value.entry : null,
    };
    if (found.source !== STORE_KEY) {
      writeQueueSession(store, normalized);
      store.removeItem(found.source);
    }
    return normalized;
  }

  function writeQueueSession(storage, value) {
    var store = safeStorage(storage);
    if (!validSessionShape(value)) return false;
    try {
      store.setItem(STORE_KEY, JSON.stringify(value));
      return true;
    } catch (err) { return false; }
  }

  function clearQueueSession(storage) {
    var store = safeStorage(storage);
    var removed = false;
    [STORE_KEY].concat(LEGACY_STORE_KEYS).forEach(function (key) {
      try {
        if (store.getItem(key) !== null) { store.removeItem(key); removed = true; }
      } catch (err) { /* 忽略：清不掉也不阻塞流程 */ }
    });
    return removed;
  }

  function saveQueueEntry(storage, session, entry) {
    if (!session || session.queueId !== entry.queueId) return session;
    var updated = Object.assign({}, session, {
      status: entry.status, entry: entry,
      displayNumber: entry.displayNumber || session.displayNumber,
      styleId: entry.styleId || session.styleId,
      portraitMode: entry.portraitMode,
      pickupUrl: pickupPath(entry.pickupUrl) || session.pickupUrl || "",
    });
    writeQueueSession(storage, updated);
    return updated;
  }

  function queueSessionEntry(session) {
    if (!session) return null;
    return Object.assign({}, session.entry || {}, {
      queueId: session.queueId, displayNumber: session.displayNumber,
      status: session.status || (session.entry && session.entry.status) || "queued",
      styleId: session.styleId, portraitMode: session.portraitMode,
      ai_watermark: session.ai_watermark !== false,
      pickupUrl: session.pickupUrl || (session.entry && session.entry.pickupUrl) || "",
    });
  }

  /* 同源手机入口地址：回环地址仅本机可用，不能作为手机扫码提示。 */
  function joinUrlFromOrigin(origin) {
    var value = trim(origin);
    if (!value || value === "null" || value.indexOf("file:") === 0) return "";
    try {
      var parsed = new URL(value);
      var host = (parsed.hostname || "").toLowerCase();
      if (host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]") return "";
    } catch (err) { return ""; }
    return value.replace(/\/+$/, "") + "/join";
  }

  /* ------------------------------------------------------------------ */
  /* 纯逻辑：提交控制器（同意 + 提交 + 重试决策，注入 request 后可 Node 测试） */
  /* ------------------------------------------------------------------ */

  function createSubmitController(options) {
    var opts = options || {};
    if (typeof opts.request !== "function") throw new TypeError("createSubmitController 需要 options.request");
    var storage = opts.storage ? safeStorage(opts.storage) : null;
    var listeners = [];
    var uncertainRequest = null;
    var submissionGeneration = 0;
    var state = {
      phase: "idle", entry: null, session: "", queueId: "", error: "", message: "",
      requestId: "",
      droppedOptionalFields: false,
    };

    function snapshot() {
      return {
        phase: state.phase, entry: state.entry, session: state.session, queueId: state.queueId,
        requestId: state.requestId,
        error: state.error, message: state.message, droppedOptionalFields: state.droppedOptionalFields,
      };
    }

    function emit() {
      var current = snapshot();
      listeners.forEach(function (listener) {
        try { listener(current); } catch (err) { /* 订阅者异常不影响控制器 */ }
      });
      if (typeof opts.onState === "function") {
        try { opts.onState(current); } catch (err2) { /* 同上 */ }
      }
    }

    function setState(patch) {
      var key;
      for (key in patch) {
        if (Object.prototype.hasOwnProperty.call(patch, key)) state[key] = patch[key];
      }
      emit();
      return snapshot();
    }

    function accept(result, context) {
      var stored = {
        queueId: result.entry.queueId, session: result.session,
        consentId: trim(opts.getConsentId && opts.getConsentId()),
        styleId: context.styleId, portraitMode: result.entry.portraitMode, displayNumber: result.entry.displayNumber,
        ai_watermark: context.ai_watermark,
        status: result.entry.status, submittedAt: Date.now(),
        recoverUrl: result.recoveryUrl || "", pickupUrl: result.pickupUrl || "",
        entry: result.entry,
      };
      if (storage) writeQueueSession(storage, stored);
      return setState({
        phase: "queued", entry: result.entry, session: result.session,
        queueId: result.entry.queueId, error: "", droppedOptionalFields: !!context.droppedOptionalFields,
        message: result.created ? "报名成功。" : "已回到你的排队记录。",
      });
    }

    function submit(input) {
      var src = input || {};
      if (state.phase === "submitting") return Promise.resolve(snapshot());
      var consentId = trim(opts.getConsentId && opts.getConsentId());
      var fingerprint = JSON.stringify([src.styleId, src.portraitMode, src.optionalFields, consentId, src.ai_watermark !== false]);
      var requestId = opts.reuseUncertainRequest && uncertainRequest && uncertainRequest.fingerprint === fingerprint
        ? uncertainRequest.requestId : (typeof opts.newRequestId === "function" ? opts.newRequestId : newRequestId)();
      var currentGeneration = ++submissionGeneration;
      var built = buildQueueSubmission({
        requestId: requestId, styleId: src.styleId, consentId: consentId, portraitMode: src.portraitMode,
        ai_watermark: src.ai_watermark,
        optionalFields: src.optionalFields, optionalFieldAllowlist: src.optionalFieldAllowlist,
      });
      if (!built.ok) {
        return Promise.resolve(setState({ phase: "invalid", error: "INVALID_REQUEST", message: built.error }));
      }
      var context = {
        styleId: trim(src.styleId),
        portraitMode: normalizePortraitMode(src.portraitMode),
        ai_watermark: src.ai_watermark !== false,
        hadOptionalFields: !!built.body.optional_fields,
        droppedOptionalFields: false,
      };
      setState({ phase: "submitting", requestId: requestId, error: "", message: "正在提交报名…" });

      function post(body) {
        return Promise.resolve(opts.request("/api/queue", body));
      }

      function step(body) {
        return post(body).then(function (response) {
          if (submissionGeneration !== currentGeneration) return snapshot();
          uncertainRequest = opts.reuseUncertainRequest && (!response || !response.status || response.status >= 500)
            ? { fingerprint: fingerprint, requestId: requestId } : null;
          var result = interpretSubmitResponse(
            (response && response.status) || 0, response && response.payload);
          if (result.ok) {
            var successAction = nextSubmitAction(result, {});
            if (successAction.action === "accept") return accept(result, context);
            /* 重放拿回已取消/过期条目：不能当成功展示，必须换新 request_id 重交 */
            return setState({ phase: "needs_consent", error: result.entry.status, message: successAction.message });
          }
          var action = nextSubmitAction(result, {
            hadOptionalFields: context.hadOptionalFields,
            fieldsDropped: context.droppedOptionalFields,
          });
          if (action.action === "retry_without_fields") {
            context.droppedOptionalFields = true;
            var retry = buildQueueSubmission({
              requestId: requestId, styleId: context.styleId, consentId: consentId, portraitMode: context.portraitMode,
              ai_watermark: context.ai_watermark,
              omitOptionalFields: true,
            });
            if (retry.ok) return step(retry.body);
            return setState({ phase: "failed", error: "INVALID_REQUEST", message: retry.error });
          }
          if (action.action === "reconsent" || action.action === "resubmit") {
            return setState({ phase: "needs_consent", error: result.code, message: action.message });
          }
          return setState({ phase: "failed", error: result.code, message: action.message });
        }, function () {
          if (submissionGeneration !== currentGeneration) return snapshot();
          if (opts.reuseUncertainRequest) uncertainRequest = { fingerprint: fingerprint, requestId: requestId };
          return setState({ phase: "failed", error: "NETWORK", message: "网络不可用，报名没有提交成功，请重试。" });
        });
      }

      return step(built.body).then(function (snap) {
        if (snap.phase === "queued" && context.droppedOptionalFields) {
          return setState({ message: "已报名（个人信息未上传：现场未启用信息收集）。" });
        }
        return snap;
      });
    }

    return {
      submit: submit,
      snapshot: snapshot,
      subscribe: function (listener) {
        if (typeof listener === "function") listeners.push(listener);
        return function () {
          var at = listeners.indexOf(listener);
          if (at >= 0) listeners.splice(at, 1);
        };
      },
      reset: function () {
        submissionGeneration += 1;
        uncertainRequest = null;
        return setState({
          phase: "idle", entry: null, session: "", queueId: "", error: "", message: "",
          requestId: "",
          droppedOptionalFields: false,
        });
      },
    };
  }
  /* ------------------------------------------------------------------ */
  /* 浏览器侧：网络 / DOM 小工具                                          */
  /* ------------------------------------------------------------------ */

  function jsonFetch(path, options) {
    var opts = options || {};
    var init = { method: opts.method || "GET", headers: {} };
    if (opts.cache) init.cache = opts.cache;
    if (opts.headers) {
      Object.keys(opts.headers).forEach(function (key) { init.headers[key] = opts.headers[key]; });
    }
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return root.fetch(path, init).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (payload) {
        return { status: response.status, payload: payload };
      });
    });
  }

  function requestNoThrow(path, body) {
    return jsonFetch(path, { method: "POST", body: body }).catch(function () {
      return { status: 0, payload: null };
    });
  }

  function $(selector, scope) {
    var owner = scope || (typeof document !== "undefined" ? document : null);
    return owner ? owner.querySelector(selector) : null;
  }

  function $$(selector, scope) {
    var owner = scope || (typeof document !== "undefined" ? document : null);
    return owner ? Array.prototype.slice.call(owner.querySelectorAll(selector)) : [];
  }

  function notify(message) {
    if (root.Booth && typeof root.Booth.toast === "function") {
      root.Booth.toast(message);
      return;
    }
    if (typeof console !== "undefined" && console.log) console.log(message);
  }

  function loadStyleCatalog() {
    var tried = [];
    function next(index) {
      if (index >= STYLE_ENDPOINTS.length) {
        return Promise.resolve({
          source: "static-fallback",
          styles: normalizeStyleList(STATIC_STYLE_FALLBACK),
          tried: tried,
        });
      }
      var endpoint = STYLE_ENDPOINTS[index];
      tried.push(endpoint);
      return jsonFetch(endpoint).then(function (response) {
        var ok = response.status >= 200 && response.status < 300;
        var styles = ok ? normalizeStyleList(response.payload) : [];
        if (styles.length) return { source: endpoint, styles: styles, tried: tried };
        return next(index + 1);
      }, function () { return next(index + 1); });
    }
    return next(0);
  }

  /* 逐个探测缩略图候选，返回第一个真实存在的 URL；全失败返回 ""。 */
  function probeThumbUrl(candidates) {
    return new Promise(function (resolve) {
      var index = 0;
      function tryNext() {
        if (!candidates || index >= candidates.length) { resolve(""); return; }
        var url = candidates[index];
        index += 1;
        var probe = new root.Image();
        probe.onload = function () { resolve(url); };
        probe.onerror = function () { tryNext(); };
        probe.src = url;
      }
      tryNext();
    });
  }

  function resolveStyleThumbs(styles, mode) {
    return Promise.all(styles.map(function (style) {
      return probeThumbUrl(thumbCandidatesFor(style.id, style.thumb, mode)).then(function (url) {
        return url ? Object.assign({}, style, { thumbUrl: url }) : null;
      });
    })).then(function (rows) {
      return rows.filter(Boolean);
    });
  }

  function emitDomEvent(name, detail) {
    if (typeof document === "undefined" || typeof root.CustomEvent !== "function") return;
    try { document.dispatchEvent(new root.CustomEvent(name, { detail: detail })); }
    catch (err) { /* 事件只是给自动化用的旁路，失败不影响流程 */ }
  }

  /* ------------------------------------------------------------------ */
  /* 页面装配：入口页（轮播 + 二维码 + 代为报名 + 排队状态）               */
  /* ------------------------------------------------------------------ */

  function mountEntryPage() {
    var el = {
      root: $("#af-entry"), carousel: $("#af-carousel"), img: $("#af-carousel-img"),
      carouselTitle: $("#af-carousel-title"), carouselDesc: $("#af-carousel-desc"),
      carouselDots: $("#af-carousel-dots"), carouselEmpty: $("#af-carousel-empty"),
      unavailable: $("#af-entry-unavailable"),
      qr: $("#af-join-qr"), qrFallback: $("#af-join-fallback"), joinUrl: $("#af-join-url"),
      joinLink: $("#af-join-link"), inline: $("#af-inline"), styleSelect: $("#af-style"),
      name: $("#af-name"), handle: $("#af-handle"), consent: $("#af-consent"),
      submit: $("#af-submit"), reset: $("#af-reset"), status: $("#af-status"),
      queue: $("#af-queue"), queueNumber: $("#af-queue-number"), queueStatus: $("#af-queue-status"),
      queueHint: $("#af-queue-hint"), start: $("#af-start"), abandon: $("#af-abandon"),
    };
    if (!el.root) return null;

    var storage = safeStorage();
    var timers = defaultTimers();
    var styleSource = "";
    var styles = [];
    var carousel = null;
    var consentController = null;
    var poller = null;
    var startPending = false;
    var session = readQueueSession(storage);
    var portraitMode = root.Booth.mountPortraitMode($("#af-portrait-mode"), session ? session.portraitMode : undefined);

    function currentConsentId() {
      var token = consentController && consentController.getToken();
      if (token) return token;
      return session ? session.consentId : "";
    }

    function setStatus(text, tone) {
      if (!el.status) return;
      el.status.textContent = text || "";
      el.status.setAttribute("data-af-tone", tone || "");
    }

    /* ---- 轮播：只写自己的展示区，绝不触碰页面表单/画廊 ---- */
    var carouselItems = [];
    var carouselGeneration = 0;

    function refreshCarousel(mode) {
      var generation = ++carouselGeneration;
      return resolveStyleThumbs(styles, mode).then(function (loaded) {
        if (generation !== carouselGeneration) return;
        buildCarousel(loaded);
      });
    }

    root.addEventListener("booth:portrait-mode", function (event) { refreshCarousel(event.detail); });

    function renderCarousel(snapshot) {
      var item = carouselItems[snapshot.index];
      if (!item) return;
      el.img.src = item.thumbUrl;
      el.img.alt = "风格示意：" + item.title;
      if (el.carouselTitle) el.carouselTitle.textContent = item.title;
      if (el.carouselDesc) el.carouselDesc.textContent = item.description || "";
      el.carousel.setAttribute("data-af-index", String(snapshot.index));
      el.carousel.setAttribute("data-af-style-id", item.id);
      if (el.carouselDots) {
        Array.prototype.forEach.call(el.carouselDots.children, function (dot, index) {
          var active = index === snapshot.index;
          dot.classList.toggle("is-active", active);
          dot.setAttribute("aria-pressed", String(active));
        });
      }
      emitDomEvent("autoflow:carousel", { index: snapshot.index, styleId: item.id, reason: snapshot.reason });
    }

    function buildCarousel(loaded) {
      var current = carousel && carouselItems[carousel.index()];
      if (carousel) carousel.stop();
      carouselItems = loaded;
      if (el.carouselEmpty) el.carouselEmpty.classList.toggle("hide", loaded.length > 0);
      if (!loaded.length) {
        if (el.carouselTitle) el.carouselTitle.textContent = "风格示意加载失败";
        return;
      }
      if (el.carouselDots) {
        el.carouselDots.innerHTML = "";
        loaded.forEach(function (item, index) {
          var dot = document.createElement("button");
          dot.type = "button";
          dot.className = "af-carousel-dot";
          dot.setAttribute("aria-label", "查看风格示意：" + item.title);
          dot.addEventListener("click", function () { carousel.setIndex(index); });
          el.carouselDots.appendChild(dot);
        });
      }
      carousel = createCarousel({ count: loaded.length, intervalMs: CAROUSEL_INTERVAL_MS, timers: timers });
      var selectedIndex = current ? loaded.findIndex(function (item) { return item.id === current.id; }) : 0;
      carousel.setIndex(Math.max(0, selectedIndex));
      carousel.subscribe(renderCarousel);
      renderCarousel({ index: carousel.index(), count: loaded.length, reason: "init" });
      carousel.start();
    }

    function fillStyleSelect() {
      if (!el.styleSelect) return;
      el.styleSelect.innerHTML = "";
      styles.forEach(function (style) {
        var option = document.createElement("option");
        option.value = style.id;
        option.textContent = style.title;
        el.styleSelect.appendChild(option);
      });
      if (session && styles.some(function (style) { return style.id === session.styleId; })) el.styleSelect.value = session.styleId;
      refreshSubmitState();
    }

    function refreshSubmitState() {
      if (!el.submit) return;
      var consented = Boolean(consentController && consentController.getToken());
      var hasStyle = Boolean(el.styleSelect && el.styleSelect.value);
      el.submit.disabled = !(consented && hasStyle);
    }

    /* ---- 报名二维码：404/加载失败 → 显示同源 /join 地址文本（降级） ---- */
    function setupJoinQr(queueReady, aigcReady) {
      var url = joinUrlFromOrigin(typeof location !== "undefined" ? location.origin : "");
      if (el.joinUrl) el.joinUrl.textContent = url || "当前页面是本机地址，手机无法直接访问；请让现场工作人员提供局域网地址";
      if (el.joinLink && !url) {
        el.joinLink.removeAttribute("href");
        el.joinLink.textContent = "手机地址暂不可显示";
      }
      if (!queueReady || !aigcReady) {
        if (el.root) el.root.classList.add("is-unavailable");
        if (el.unavailable) {
          el.unavailable.textContent = !queueReady
            ? "手机排队暂未开放。上方风格图仅用于预览，请使用下方“现场直接拍摄”，或让现场工作人员协助。"
            : "风格生成暂未开放，扫码排队暂不可用。请使用下方“现场直接拍摄”，或让现场工作人员协助。";
          el.unavailable.classList.remove("hide");
        }
        return;
      }
      if (!el.qr) return;
      el.qr.addEventListener("load", function () {
        if (el.qrFallback) el.qrFallback.classList.add("hide");
      });
      el.qr.addEventListener("error", function () {
        /* T-16 尚未注册 /api/join-qr.png 时必走这里：不假装有二维码，给出可手输的地址 */
        el.qr.classList.add("hide");
        if (el.qrFallback) el.qrFallback.classList.remove("hide");
        notify("报名二维码暂不可用：已改为显示可手输的 /join 地址");
      });
      el.qr.src = "/api/join-qr.png";
    }

    function loadFeatureState() {
      var stateRequest = jsonFetch("/api/state").catch(function () { return { status: 0, payload: null }; });
      var queueRequest = jsonFetch("/api/queue", { method: "POST", body: {} }).catch(function () { return { status: 0, payload: null }; });
      return Promise.all([stateRequest, queueRequest]).then(function (rows) {
        var statePayload = rows[0].payload || {};
        presentationMode = statePayload.presentation_mode === "handoff" ? "handoff" : "full";
        var aigcReady = Boolean(rows[0].payload && rows[0].payload.aigc_enabled);
        var queueReady = rows[1].status !== 404 && rows[1].status !== 0;
        setupJoinQr(queueReady, aigcReady);
        return { queueReady: queueReady, aigcReady: aigcReady };
      });
    }
    /* ---- 报名提交 ---- */
    var submitController = createSubmitController({
      storage: storage,
      request: requestNoThrow,
      getConsentId: currentConsentId,
      onState: function (snap) {
        if (snap.phase === "submitting") setStatus(snap.message, "busy");
        if (snap.phase === "invalid" || snap.phase === "failed") setStatus(snap.message, "error");
        if (snap.phase === "needs_consent") {
          setStatus(snap.message, "error");
          if (consentController) consentController.reset();
          refreshSubmitState();
          var button = el.consent && el.consent.querySelector("button");
          if (button && button.focus) button.focus();
        }
        if (snap.phase === "queued") {
          session = readQueueSession(storage);
          setStatus(snap.message, "ok");
          showQueueCard(snap.entry);
          startPolling();
        }
      },
    });

    function onSubmit() {
      if (el.submit && el.submit.disabled) return;
      submitController.submit({
        styleId: el.styleSelect ? el.styleSelect.value : "",
        portraitMode: portraitMode.value(),
        optionalFields: {
          display_name: el.name ? el.name.value : "",
          developer_id: el.handle ? el.handle.value : "",
        },
      });
    }

    /* ---- 排队状态视图 ---- */
    function showQueueCard(entry) {
      if (el.queue) el.queue.classList.remove("hide");
      if (el.inline) el.inline.classList.add("hide");
      renderQueueEntry(entry);
    }

    function hideQueueCard() {
      if (el.queue) el.queue.classList.add("hide");
      if (el.inline) el.inline.classList.remove("hide");
      if (el.reset) el.reset.classList.add("hide");
    }

    function renderQueueEntry(entry, viewOverride) {
      var view = viewOverride || (entry ? queueStatusView(entry) : null);
      if (el.queueNumber) el.queueNumber.textContent = displayNumberText(entry && entry.displayNumber) || "—";
      if (!view) return;
      if (el.queueStatus) {
        el.queueStatus.textContent = view.title;
        el.queueStatus.setAttribute("data-af-tone", view.tone);
      }
      if (el.queueHint) el.queueHint.textContent = entryStyleLabel(entry, styles) + "。" + view.detail;
      if (el.start) el.start.classList.toggle("hide", !view.canStart);
      if (el.abandon) el.abandon.classList.toggle("hide", !view.canAbandon);
      /* 「重新报名」只在终态出现：排队中重复报名会多占一个名额（旧条目要等超时才释放） */
      if (el.reset) el.reset.classList.toggle("hide", !view.terminal);
      if (entry) session = readQueueSession(storage) || session;
    }

    function renderQueueMessage(text, tone) {
      if (!el.queueHint) return;
      el.queueHint.textContent = text || "";
      if (el.queueStatus) el.queueStatus.setAttribute("data-af-tone", tone || "error");
    }

    function startPolling() {
      if (poller) poller.stop("restart");
      if (!session || !session.queueId || !session.session) return;
      if (shouldStopPolling(queueSessionEntry(session))) return;
      poller = createQueuePoller({
        intervalMs: POLL_INTERVAL_MS,
        timers: timers,
        fetchEntry: function () {
          return jsonFetch("/api/queue/" + encodeURIComponent(session.queueId),
            { headers: { "X-Queue-Session": session.session } });
        },
        onUpdate: function (out) {
          if (out.ok) {
            session = saveQueueEntry(storage, session, out.entry);
            renderQueueEntry(out.entry);
            emitDomEvent("autoflow:queue", { status: out.entry.status, displayNumber: out.entry.displayNumber });
          } else if (out.stop) {
            renderQueueEntry(queueSessionEntry(session), QUEUE_MISSING_VIEW);
          }
        },
        onError: function (out) {
          renderQueueMessage(out.message, "warn");
        },
      });
      poller.start();
    }

    function startCapture() {
      if (!session || startPending) return;
      var built = buildStartRequest(currentConsentId());
      if (!built.ok) {
        renderQueueMessage(built.error, "error");
        if (consentController) consentController.reset();
        refreshSubmitState();
        return;
      }
      var startingSession = session;
      startPending = true;
      if (poller) poller.stop("start");
      if (el.start) el.start.disabled = true;
      return jsonFetch("/api/queue/" + encodeURIComponent(startingSession.queueId) + "/start", {
        method: "POST",
        body: built.body,
        headers: { "X-Queue-Session": startingSession.session },
      }).then(function (response) {
        if (!session || session.queueId !== startingSession.queueId) return;
        var out = interpretStartResponse(response.status, response.payload);
        if (out.ok) {
          session = saveQueueEntry(storage, session, out.entry);
          renderQueueEntry(out.entry);
          /* 手势开着时，点「开始」只是占住拍摄位，真正开拍要等 OK 手势 ——
             文案必须如实说，否则用户以为已经在拍了。 */
          if (out.gestureEnabled) {
            renderQueueMessage("已就位，请看向镜头比个 OK 手势，识别到后自动倒计时拍照。", "ok");
          } else {
            renderQueueMessage("已开始拍摄，请配合现场提示。", "ok");
          }
          return;
        }
        renderQueueMessage(out.message, "error");
        if (out.needsConsent && consentController) {
          consentController.reset();
          refreshSubmitState();
        }
      }, function () {
        if (!session || session.queueId !== startingSession.queueId) return;
        renderQueueMessage("网络不可用，开始动作没有发出去，请重试。", "error");
      }).then(function () {
        startPending = false;
        if (el.start) el.start.disabled = false;
        if (session && session.queueId === startingSession.queueId) startPolling();
      });
    }

    function abandonQueue() {
      var ok = true;
      if (typeof root.confirm === "function") {
        ok = root.confirm("放弃排队？本机将停止显示状态；还没开始的排队名额会按超时自动释放。");
      }
      if (!ok) return;
      if (poller) poller.stop("abandon");
      clearQueueSession(storage);
      session = null;
      submitController.reset();
      hideQueueCard();
      setStatus("已停止显示。名额到期会自动释放；需要的话可以重新报名。", "ok");
      refreshSubmitState();
    }

    function resetFlow() {
      if (poller) poller.stop("reset");
      clearQueueSession(storage);
      session = null;
      submitController.reset();
      if (consentController) consentController.reset();
      hideQueueCard();
      setStatus("可以从头再来一次：选风格、同意拍摄、点「加入排队」。", "ok");
      refreshSubmitState();
    }

    /* ---- 装配与首次加载 ---- */
    function wire() {
      if (el.styleSelect) el.styleSelect.addEventListener("change", refreshSubmitState);
      if (el.submit) el.submit.addEventListener("click", onSubmit);
      if (el.start) el.start.addEventListener("click", startCapture);
      if (el.abandon) el.abandon.addEventListener("click", abandonQueue);
      if (el.reset) el.reset.addEventListener("click", resetFlow);
    }

    function mountConsent() {
      if (!el.consent || !root.AutoFlowConsent) return;
      consentController = root.AutoFlowConsent.mountConsentPanel(el.consent, {
        request: function (body) { return requestNoThrow(root.AutoFlowConsent.CONSENT_ENDPOINT, body); },
        onGranted: function () { refreshSubmitState(); setStatus("已记录同意。现在可以「加入排队」。", "ok"); },
        onDeclined: function () { refreshSubmitState(); setStatus("没有同意也没关系，填写的风格不会提交。", ""); },
        onError: function (snap) { refreshSubmitState(); setStatus(snap.message || "同意登记失败，请重试。", "error"); },
      });
    }

    wire();
    mountConsent();
    loadFeatureState();
    loadStyleCatalog().then(function (loaded) {
      styles = loaded.styles;
      styleSource = loaded.source;
      fillStyleSelect();
      return refreshCarousel(portraitMode.value());
    }, function () {
      buildCarousel([]);
    });

    /* 恢复上次会话（同一标签页内刷新/回转）：直接进排队状态视图 */
    if (session) {
      showQueueCard(queueSessionEntry(session));
      startPolling();
    }

    return {
      page: "entry",
      carousel: function () {
        return carousel ? carousel.snapshot() : { index: 0, count: 0 };
      },
      carouselAdvance: function () { return carousel ? carousel.advance() : 0; },
      styles: function () { return styles.slice(); },
      styleSource: function () { return styleSource; },
      submit: onSubmit,
      startCapture: startCapture,
      abandonQueue: abandonQueue,
      resetFlow: resetFlow,
      openInlineForm: function () { if (el.inline) el.inline.open = true; },
      state: function () {
        return {
          page: "entry",
          styleSource: styleSource,
          stylesCount: styles.length,
          carouselIndex: carousel ? carousel.index() : 0,
          carouselCount: carousel ? carousel.count() : 0,
          consentPhase: consentController ? consentController.snapshot().phase : "unavailable",
          queue: session ? { queueId: session.queueId, status: session.status } : null,
          controller: submitController.snapshot(),
        };
      },
    };
  }
  /* ------------------------------------------------------------------ */
  /* 页面装配：手机页（选风格 + 同意 + 提交 + 轮询 + 到号开始）             */
  /* ------------------------------------------------------------------ */

  function mountJoinPage() {
    var el = {
      gate: $("#af-join-gate"), watermark: $("#af-join-watermark"), watermarkNote: $("#af-join-watermark-note"),
      watermarkMessage: $("#af-join-watermark-message"),
      root: $("#af-join"), form: $("#af-join-form"), styleSelect: $("#af-join-style"),
      styleGrid: $("#af-join-styles"), styleChoice: $("#af-join-style-choice"),
      name: $("#af-join-name"), handle: $("#af-join-handle"),
      consent: $("#af-join-consent"), submit: $("#af-join-submit"),
      message: $("#af-join-message"), status: $("#af-join-status"),
      number: $("#af-join-number"), state: $("#af-join-state"), detail: $("#af-join-detail"),
      hint: $("#af-join-hint"), start: $("#af-join-start"), abandon: $("#af-join-abandon"),
      reset: $("#af-join-reset"), unavailable: $("#af-join-unavailable"),
      pickup: $("#af-join-pickup"),
      recovery: $("#af-join-recovery"), copy: $("#af-join-recovery-copy"),
      recoveryDetails: $("#af-join-recovery-details"), recoveryLink: $("#af-join-recovery-link"),
      recoveryMessage: $("#af-join-recovery-message"), controlNote: $("#af-join-control-note"),
      deadline: $("#af-join-deadline"),
    };
    if (!el.root) return null;

    var storage = safeStorage();
    var timers = defaultTimers();
    var styles = [];
    var styleSource = "";
    var consentController = null;
    var poller = null;
    var startPending = false;
    var session = readQueueSession(storage);
    var recoveryMode = /^#restore(?:=|$)/.test(root.location.hash);
    var recoveryToken = /^#restore=([A-Za-z0-9_-]{32})$/.exec(root.location.hash);
    recoveryToken = recoveryToken ? recoveryToken[1] : "";
    if (recoveryMode) session = null;
    var recoveryEntry = null;
    var recoveryUrl = "";
    var recoveryPending = false;
    var flowGeneration = 0;
    var recoveryRequest = 0;
    var recoveryTimer = null;
    var attachAttempted = "";
    var queueUnavailable = false;
    var registrationVisible = !session && !recoveryMode;
    var captureReconsent = false;
    /* 点「开始」时服务端告诉我们要不要比 OK 手势；记在 sessionStorage 里，
       刷新页面后「请比 OK 手势」的提示也还在（只记当前这条排队）。 */
    var GESTURE_KEY = "autoflow.gesture.v1";
    var lastStatus = "";
    function gestureOn(queueId) {
      try { return Boolean(queueId && storage && storage.getItem(GESTURE_KEY) === queueId); } catch (e) { return false; }
    }
    function rememberGesture(queueId, enabled) {
      try {
        if (!storage) return;
        if (enabled && queueId) storage.setItem(GESTURE_KEY, queueId);
        else storage.removeItem(GESTURE_KEY);
      } catch (e) { /* 存不下只影响刷新后的提示文案 */ }
    }
    var modeField = $("#af-join-portrait-mode");
    var portraitMode = root.Booth.mountPortraitMode(
      modeField, session ? session.portraitMode : undefined, "chibi");
    if (session && el.styleSelect) el.styleSelect.value = session.styleId;
    if (el.watermark) el.watermark.checked = !session || session.ai_watermark !== false;

    function renderGate() {
      var granted = Boolean(consentController && consentController.getToken());
      if (el.gate) el.gate.hidden = !(registrationVisible || captureReconsent) || granted;
      if (el.form) el.form.hidden = !registrationVisible || !granted;
    }

    function requestCaptureConsent(message) {
      captureReconsent = true;
      if (consentController) consentController.reset();
      renderGate();
      if (el.hint) el.hint.textContent = message;
    }

    function isCurrent(expected, generation) {
      return !recoveryMode && generation === flowGeneration && session && expected &&
        session.queueId === expected.queueId && session.session === expected.session;
    }

    function recoveryFetch(path, options) {
      return new Promise(function (resolve) {
        var timeout = timers.setTimeout(function () { resolve({ status: 0, payload: null }); }, 8000);
        jsonFetch(path, Object.assign({}, options, { cache: "no-store" })).then(function (response) {
          timers.clearTimeout(timeout);
          resolve(response);
        }, function () {
          timers.clearTimeout(timeout);
          resolve({ status: 0, payload: null });
        });
      });
    }

    function updateRecoveryLink() {
      var path = recoveryPath(recoveryMode ? recoveryUrl : session && session.recoverUrl);
      // 中断、取消、超时之后备用链接已经没用了，不再占地方
      var ended = ["failed", "cancelled", "expired"].indexOf(lastStatus) >= 0;
      if (el.recovery) el.recovery.classList.toggle("hide", !path || ended);
      if (el.recoveryLink) el.recoveryLink.value = path ? root.location.origin + path : "";
    }

    function attachRecovery() {
      if (recoveryMode || !session || session.recoverUrl) return;
      var expected = session;
      var generation = flowGeneration;
      var key = generation + ":" + expected.queueId + ":" + expected.session;
      if (attachAttempted === key) return;
      attachAttempted = key;
      recoveryFetch("/api/recovery/attach", {
        method: "POST", body: { queue_id: expected.queueId }, headers: { "X-Queue-Session": expected.session },
      }).then(function (response) {
        if (!isCurrent(expected, generation)) return;
        var path = response.status === 200 && response.payload && recoveryPath(response.payload.recovery_url);
        if (!path) return;
        session = Object.assign({}, session, { recoverUrl: path });
        writeQueueSession(storage, session);
        updateRecoveryLink();
      });
    }

    function recoverCurrent(afterSubmit) {
      if (recoveryMode || session || recoveryPending) return Promise.resolve(false);
      if (!afterSubmit) {
        try { if (storage.getItem(RECOVERY_SKIP_KEY)) return Promise.resolve(false); } catch (err) { }
      }
      recoveryPending = true;
      var generation = flowGeneration;
      var request = ++recoveryRequest;
      var expectedRequestId = afterSubmit ? submitController.snapshot().requestId : "";
      refreshSubmitState();
      return recoveryFetch("/api/recovery/current").then(function (response) {
        if (generation !== flowGeneration || request !== recoveryRequest || session || recoveryMode) return false;
        recoveryPending = false;
        var body = response.payload || {};
        var entry = response.status === 200 && body.ok === true && normalizeEntry(body.entry);
        if (entry && trim(body.session) && body.read_only !== true &&
            (!afterSubmit || expectedRequestId && entry.requestId === expectedRequestId)) {
          session = {
            queueId: entry.queueId, session: trim(body.session), consentId: trim(body.consent_id) || trim(body.submission_consent_id),
            styleId: entry.styleId, portraitMode: entry.portraitMode, displayNumber: entry.displayNumber,
            ai_watermark: typeof body.ai_watermark === "boolean" ? body.ai_watermark : entry.ai_watermark,
            status: entry.status, entry: entry, recoverUrl: recoveryPath(body.recovery_url),
            pickupUrl: pickupPath(body.pickup_url), submittedAt: Date.now(),
          };
          writeQueueSession(storage, session);
          if (el.styleSelect) el.styleSelect.value = session.styleId;
          showStatus(queueSessionEntry(session));
          startPolling();
          attachRecovery();
          return true;
        }
        refreshSubmitState();
        if (afterSubmit) setMessage("暂时无法确认报名结果，可重试本次报名或找现场工作人员。", "warn");
        return false;
      });
    }

    function renderRecoveryFailure(missing) {
      renderEntry(recoveryEntry, {
        tone: missing ? "error" : "waiting", title: missing ? "拍摄链接已失效" : "暂时无法连接",
        detail: missing ? "这条链接已失效或记录已清理。可以主动重新报名，或找现场工作人员。"
          : "正在尝试重新连接，请稍等；也可以回到原报名页继续。",
        terminal: missing, canStart: false, canAbandon: false,
      });
      if (el.reset) el.reset.classList.remove("hide");
    }

    function syncRecoveryLink() {
      if (!recoveryMode || !recoveryToken || document.hidden) return;
      if (recoveryEntry && shouldStopPolling(recoveryEntry) && recoveryEntry.status !== "ready_for_pickup") return;
      if (recoveryTimer !== null) timers.clearTimeout(recoveryTimer);
      recoveryTimer = null;
      var generation = flowGeneration;
      var request = ++recoveryRequest;
      recoveryFetch("/api/recovery/" + recoveryToken).then(function (response) {
        if (!recoveryMode || generation !== flowGeneration || request !== recoveryRequest) return;
        var body = response.payload || {};
        var raw = body.entry;
        if (response.status === 200 && body.ok === true && body.read_only === true && isObject(raw) && trim(raw.status)) {
          recoveryEntry = {
            displayNumber: trim(raw.display_number), status: trim(raw.status), styleId: trim(raw.style_id),
            portraitMode: normalizePortraitMode(raw.portrait_mode), startDeadline: trim(raw.start_deadline || raw.called_deadline),
            ai_watermark: typeof body.ai_watermark === "boolean" ? body.ai_watermark : raw.ai_watermark !== false,
            pickupUrl: pickupPath(body.pickup_url),
          };
          recoveryUrl = recoveryPath(body.recovery_url);
          showStatus(recoveryEntry);
          if (shouldStopPolling(recoveryEntry) && (recoveryEntry.status !== "ready_for_pickup" || recoveryEntry.pickupUrl)) return;
        } else if (response.status === 404 || response.status === 410) {
          recoveryUrl = "";
          updateRecoveryLink();
          renderRecoveryFailure(true);
          return;
        } else {
          if (recoveryEntry) renderEntry(recoveryEntry);
          else renderRecoveryFailure(false);
          if (el.hint) el.hint.textContent = "网络连接不稳定，正在自动重试…";
        }
        if (!document.hidden) recoveryTimer = timers.setTimeout(syncRecoveryLink, POLL_INTERVAL_MS);
      });
    }

    function copyRecoveryLink() {
      var path = recoveryPath(recoveryMode ? recoveryUrl : session && session.recoverUrl);
      if (!path) return;
      var url = root.location.origin + path;
      var generation = flowGeneration;
      function feedback(copied) {
        if (generation !== flowGeneration) return;
        if (el.recoveryMessage) el.recoveryMessage.textContent = copied
          ? "已复制，可收藏或发到文件传输助手。" : "请展开下方链接，长按选择并复制。";
        if (!copied && el.recoveryDetails) el.recoveryDetails.open = true;
      }
      try {
        if (!root.navigator.clipboard || !root.navigator.clipboard.writeText) { feedback(false); return; }
        Promise.resolve(root.navigator.clipboard.writeText(url)).then(function () { feedback(true); }, function () { feedback(false); });
      } catch (err) { feedback(false); }
    }

    function showUnavailable(text) {
      registrationVisible = false;
      renderGate();
      if (el.form) el.form.classList.add("hide");
      if (el.status) el.status.classList.add("hide");
      if (el.unavailable) { el.unavailable.textContent = text; el.unavailable.classList.remove("hide"); }
    }

    function currentConsentId() {
      var token = consentController && consentController.getToken();
      if (token) return token;
      return session ? session.consentId : "";
    }

    function setMessage(text, tone) {
      if (!el.message) return;
      el.message.textContent = text || "";
      el.message.setAttribute("data-af-tone", tone || "");
    }

    function refreshSubmitState() {
      renderGate();
      if (!el.submit) return;
      var consented = Boolean(consentController && consentController.getToken());
      var hasStyle = Boolean(el.styleSelect && el.styleSelect.value);
      el.submit.disabled = !(consented && hasStyle);
      if (!consented) setMessage("请先阅读上方拍摄告知并点「同意」。", "");
      else if (!hasStyle) setMessage("正在加载风格列表…", "");
      else setMessage("可以提交了：风格已选，信息可以留空。", "");
      el.submit.disabled = el.submit.disabled || recoveryMode || recoveryPending || submitController.snapshot().phase === "submitting";
      if (recoveryPending) setMessage("正在找回本浏览器的拍摄记录…", "busy");
    }

    function selectStyle(id) {
      if (!el.styleSelect) return;
      el.styleSelect.value = id || "";
      if (modeField) modeField.hidden = !el.styleSelect.value;
      renderStyleSelection();
      refreshSubmitState();
    }

    function renderStyleSelection() {
      if (!el.styleSelect) return;
      var id = el.styleSelect.value;
      var hit = null;
      styles.forEach(function (style) { if (style.id === id) hit = style; });
      if (el.styleChoice) {
        el.styleChoice.textContent = hit
          ? "已选择 " + hit.title + (hit.description ? " · " + hit.description : "")
          : "请选择一种风格";
      }
      if (!el.styleGrid) return;
      $$(".af-style-card", el.styleGrid).forEach(function (card) {
        var selected = card.dataset.styleId === id;
        card.classList.toggle("is-selected", selected);
        card.setAttribute("aria-checked", String(selected));
      });
    }

    var stylePreviewGeneration = 0;

    function refreshStyleThumbs(mode) {
      var generation = ++stylePreviewGeneration;
      if (!el.styleGrid) return;
      $$(".af-style-card", el.styleGrid).forEach(function (card) {
        var style = styles.find(function (item) { return item.id === card.dataset.styleId; });
        if (!style) return;
        probeThumbUrl(thumbCandidatesFor(style.id, style.thumb, mode)).then(function (url) {
          if (generation !== stylePreviewGeneration || !card.isConnected) return;
          var visual = card.querySelector(".af-style-card-visual");
          var image = visual.querySelector("img");
          if (!url) {
            if (image) image.remove();
            card.classList.remove("has-image");
            return;
          }
          if (!image) {
            image = document.createElement("img");
            image.alt = "";
            image.decoding = "async";
            visual.insertBefore(image, visual.querySelector(".af-style-card-label"));
          }
          image.src = url;
          card.classList.add("has-image");
        });
      });
    }

    root.addEventListener("booth:portrait-mode", function (event) { refreshStyleThumbs(event.detail); });

    function fillStyleSelect() {
      if (!el.styleSelect || !el.styleGrid) return;
      var selected = styles.some(function (style) { return style.id === el.styleSelect.value; })
        ? el.styleSelect.value : (styles[0] ? styles[0].id : "");
      el.styleGrid.innerHTML = "";
      styles.forEach(function (style) {
        var card = document.createElement("button");
        card.type = "button";
        card.className = "af-style-card";
        card.dataset.styleId = style.id;
        card.setAttribute("role", "radio");
        card.setAttribute("aria-checked", "false");
        card.setAttribute("aria-label", "选择" + style.title);

        var visual = document.createElement("span");
        visual.className = "af-style-card-visual";
        var label = document.createElement("span");
        label.className = "af-style-card-label";
        label.textContent = style.title;
        visual.appendChild(label);
        card.appendChild(visual);
        card.addEventListener("click", function () { selectStyle(style.id); });
        el.styleGrid.appendChild(card);

      });
      selectStyle(selected);
      refreshStyleThumbs(portraitMode.value());
    }

    var submitController = createSubmitController({
      storage: storage,
      request: requestNoThrow,
      getConsentId: currentConsentId,
      onState: function (snap) {
        if (snap.phase === "submitting") setMessage(snap.message, "busy");
        if (snap.phase === "invalid" || snap.phase === "failed") setMessage(snap.message, "error");
        if (snap.phase === "needs_consent") {
          setMessage(snap.message, "error");
          if (consentController) consentController.reset();
          refreshSubmitState();
        }
        if (snap.phase === "queued") {
          session = readQueueSession(storage);
          showStatus(snap.entry);
          startPolling();
          attachRecovery();
        }
      },
      reuseUncertainRequest: true,
    });

    function onSubmit() {
      if (recoveryMode || recoveryPending || session) return;
      if (el.submit && el.submit.disabled) return;
      flowGeneration += 1;
      try { storage.removeItem(RECOVERY_SKIP_KEY); } catch (err) { }
      var generation = flowGeneration;
      var submitted = submitController.submit({
        styleId: el.styleSelect ? el.styleSelect.value : "",
        portraitMode: portraitMode.value(),
        ai_watermark: !el.watermark || el.watermark.checked,
        optionalFields: {
          display_name: el.name ? el.name.value : "",
          developer_id: el.handle ? el.handle.value : "",
        },
      });
      refreshSubmitState();
      return submitted.then(function (snap) {
        if (generation !== flowGeneration) return;
        refreshSubmitState();
        if (snap.phase === "failed" && (snap.error === "NETWORK" || snap.error === "INTERNAL_ERROR")) return recoverCurrent(true);
      });
    }

    function showStatus(entry) {
      registrationVisible = false;
      captureReconsent = false;
      if (el.watermark) el.watermark.checked = (session ? session.ai_watermark : entry && entry.ai_watermark) !== false;
      renderGate();
      if (el.form) el.form.classList.add("hide");
      if (el.status) el.status.classList.remove("hide");
      renderEntry(entry);
    }

    function showForm() {
      registrationVisible = true;
      captureReconsent = false;
      renderGate();
      if (el.form) el.form.classList.remove("hide");
      if (el.status) el.status.classList.add("hide");
    }

    function renderEntry(entry, viewOverride) {
      var view = viewOverride || (entry ? queueStatusView(entry) : null);
      if (el.number) el.number.textContent = displayNumberText(entry && entry.displayNumber) || "—";
      if (!view) return;
      if (view.terminal && captureReconsent) {
        captureReconsent = false;
        renderGate();
      }
      if (el.hint) el.hint.textContent = "";
      var status = entry ? entry.status : "";
      var live = !recoveryMode && !viewOverride;
      if (el.status) el.status.setAttribute("data-af-status", status);
      var title = view.title;
      var detail = view.detail;
      if (live && status === "ready" && gestureOn(entry.queueId)) {
        /* 点了开始只是占住拍摄位，要比 OK 手势才开拍 —— 这一步必须说清楚 */
        title = "请比 OK 手势";
        detail = "看向镜头，在胸前比个 OK，识别到后自动倒计时拍照。";
      } else if (live && view.deadlineSeconds > 0) {
        detail = STATUS_VIEWS.called.detail;   // 剩余秒数在下面单独醒目显示
      }
      if (el.state) {
        el.state.textContent = title;
        el.state.setAttribute("data-af-tone", view.tone);
      }
      if (recoveryMode && !viewOverride && entry && ["called", "ready", "capturing"].indexOf(entry.status) >= 0) {
        detail = "到号后请到现场确认，或回原报名页继续拍摄。";
      }
      if (el.detail) el.detail.textContent = (entry ? entryStyleLabel(entry, styles) + "。" : "") + detail;
      if (el.deadline) {
        var showDeadline = live && view.deadlineSeconds !== null && view.deadlineSeconds > 0;
        el.deadline.classList.toggle("hide", !showDeadline);
        el.deadline.innerHTML = showDeadline
          ? "<b>" + Number(view.deadlineSeconds) + "</b> 秒内点「开始拍摄」" : "";
      }
      if (el.start) el.start.classList.toggle("hide", recoveryMode || !view.canStart);
      if (el.abandon) el.abandon.classList.toggle("hide", recoveryMode || !view.canAbandon);
      if (el.reset) el.reset.classList.toggle("hide", !view.terminal);
      if (el.controlNote) {
        el.controlNote.textContent = recoveryMode
          ? "此链接可查看进度与取图，不能控制排队或开始拍摄。取图后仍可按原页面保存、删除或申请一次打印。"
          : status === "ready"
            ? "要离开请告诉现场工作人员，把拍摄位让给下一位。"
            : "放弃后本页不再提醒，名额会在叫号超时后自动让给下一位。";
        el.controlNote.classList.toggle("hide", !recoveryMode && !view.canAbandon);
      }
      if (live && status === "called" && lastStatus !== "called" && root.navigator &&
          typeof root.navigator.vibrate === "function") {
        try { root.navigator.vibrate([180, 90, 180]); } catch (e) { /* 不支持震动就算了 */ }
      }
      lastStatus = status;
      updateRecoveryLink();
      /* 成片就绪：后端只在会话匹配时下发 photo_token（T-16 的授权门），
         有链接才亮「查看我的证件照」，拿不到就如实不显示。 */
      var pickupUrl = !viewOverride && entry && (recoveryMode ? pickupPath(entry.pickupUrl) : pickupPath(entry.pickupUrl) || pickupPath(session && session.pickupUrl));
      if (el.pickup) {
        if (pickupUrl) {
          el.pickup.href = pickupUrl;
          el.pickup.classList.remove("hide");
        } else {
          el.pickup.classList.add("hide");
        }
      }
      /* QueueEntry 不带令牌，成片就绪后要单独问一次作业接口，否则这个按钮
         永远不亮（复现过：拍完 status=ready_for_pickup 但 href 仍是 "#"）。 */
      if (!recoveryMode && !viewOverride && !pickupUrl && entry && entry.status === "ready_for_pickup") {
        resolvePickupUrl(entry.queueId);
      }
    }

    /* 成片就绪后单独问作业接口取令牌。两个约束：
       - 要有闸：轮询每 2 秒跑一次，没闸就会变成每秒打一次；
       - 不能一次失败就放弃 —— ready_for_pickup 是**轮询停止**状态，若照片记录
         比状态晚一步落库，那一次就会白问，按钮永远不亮。
       所以自己带一个有限次重试定时器，最多 PICKUP_MAX_TRIES 次，成功或超限即止。 */
    var PICKUP_MAX_TRIES = 5;
    var PICKUP_RETRY_MS = 2000;
    var pickupLookups = {};
    function resolvePickupUrl(queueId) {
      if (recoveryMode || document.hidden || !queueId || !session || !session.session || session.queueId !== queueId) return;
      var expected = session;
      var generation = flowGeneration;
      var rec = pickupLookups[queueId];
      if (rec && rec.url) { applyPickupUrl(rec.url); return; }
      if (rec && (rec.inFlight || rec.tries >= PICKUP_MAX_TRIES)) return;
      var tries = (rec ? rec.tries : 0) + 1;
      pickupLookups[queueId] = { tries: tries, url: "", inFlight: true };
      jsonFetch("/api/job/" + encodeURIComponent(queueId),
        { headers: { "X-Queue-Session": session.session } }).then(function (response) {
        if (!isCurrent(expected, generation)) return;
        var path = response.status === 200 ? pickupPath(pickupPathFromJob(response.payload)) : "";
        settlePickupLookup(queueId, tries, path);
      }, function () {
        if (!isCurrent(expected, generation)) return;
        settlePickupLookup(queueId, tries, "");
      });
    }

    function settlePickupLookup(queueId, tries, path) {
      pickupLookups[queueId] = { tries: tries, url: path, inFlight: false };
      if (path) {
        applyPickupUrl(path);
        return;
      }
      if (tries < PICKUP_MAX_TRIES) {
        var expected = session;
        var generation = flowGeneration;
        timers.setTimeout(function () {
          if (isCurrent(expected, generation)) resolvePickupUrl(queueId);
        }, PICKUP_RETRY_MS);
      } else if (el.hint) {
        el.hint.textContent = "成片已生成，但取图链接暂时取不到；可以下拉刷新本页，或找现场工作人员协助。";
      }
    }

    function applyPickupUrl(url) {
      url = pickupPath(url);
      if (!el.pickup || !url || recoveryMode || !session) return;
      session = Object.assign({}, session, { pickupUrl: url });
      writeQueueSession(storage, session);
      el.pickup.href = url;
      el.pickup.classList.remove("hide");
      if (el.detail) el.detail.textContent = "点下方「查看我的证件照」，保存或删除。";
    }

    function startPolling() {
      if (poller) poller.stop("restart");
      if (recoveryMode || document.hidden || queueUnavailable || !session || !session.queueId || !session.session) return;
      if (shouldStopPolling(queueSessionEntry(session))) return;
      var expected = session;
      var generation = flowGeneration;
      poller = createQueuePoller({
        intervalMs: POLL_INTERVAL_MS,
        timers: timers,
        fetchEntry: function () {
          return jsonFetch("/api/queue/" + encodeURIComponent(expected.queueId),
            { headers: { "X-Queue-Session": expected.session } });
        },
        onUpdate: function (out) {
          if (!isCurrent(expected, generation)) return;
          if (out.ok) {
            session = saveQueueEntry(storage, session, out.entry);
            renderEntry(out.entry);
            emitDomEvent("autoflow:queue", { status: out.entry.status, displayNumber: out.entry.displayNumber });
          } else if (out.stop) {
            queueUnavailable = true;
            renderEntry(queueSessionEntry(session), QUEUE_MISSING_VIEW);
          }
        },
        onError: function (out) {
          if (el.hint) el.hint.textContent = out.message;
        },
      });
      poller.start();
    }

    function startCapture() {
      if (recoveryMode || !session || startPending) return;
      var built = buildStartRequest(currentConsentId());
      if (!built.ok) {
        requestCaptureConsent(built.error);
        return;
      }
      var startingSession = session;
      var generation = flowGeneration;
      startPending = true;
      if (poller) poller.stop("start");
      if (el.start) el.start.disabled = true;
      return jsonFetch("/api/queue/" + encodeURIComponent(startingSession.queueId) + "/start", {
        method: "POST",
        body: built.body,
        headers: { "X-Queue-Session": startingSession.session },
      }).then(function (response) {
        if (!isCurrent(startingSession, generation)) return;
        var out = interpretStartResponse(response.status, response.payload);
        if (out.ok) {
          session = saveQueueEntry(storage, session, out.entry);
          rememberGesture(out.entry && out.entry.queueId, out.gestureEnabled);
          renderEntry(out.entry);
          /* 同上：手势开着时要说"比 OK 手势"，不能说"已开始拍摄"。 */
          if (out.gestureEnabled) {
            setMessage("已就位，请看向镜头比个 OK 手势，识别到后自动倒计时拍照。", "ok");
          } else {
            setMessage("已开始拍摄，请看镜头。", "ok");
          }
          return;
        }
        setMessage(out.message, "error");
        if (el.hint) el.hint.textContent = out.message;
        if (out.needsConsent) requestCaptureConsent(out.message);
      }, function () {
        if (!isCurrent(startingSession, generation)) return;
        if (el.hint) el.hint.textContent = "网络连接不稳定，正在同步拍摄状态…";
      }).then(function () {
        if (!isCurrent(startingSession, generation)) return;
        startPending = false;
        if (el.start) el.start.disabled = false;
        if (session && session.queueId === startingSession.queueId) startPolling();
      });
    }

    function abandonQueue() {
      var ok = true;
      if (typeof root.confirm === "function") {
        ok = root.confirm("放弃排队？本机将停止刷新状态；还没开始的排队名额会按超时自动释放。");
      }
      if (!ok) return;
      leaveRecovery();
      if (poller) poller.stop("abandon");
      clearQueueSession(storage);
      session = null;
      submitController.reset();
      showForm();
      if (consentController) consentController.reset();
      if (el.watermark) el.watermark.checked = true;
      if (el.watermarkMessage) el.watermarkMessage.textContent = "";
      setMessage("已停止排队显示。可重新阅读告知并报名。", "ok");
      refreshSubmitState();
    }

    function resetFlow() {
      leaveRecovery();
      if (poller) poller.stop("reset");
      clearQueueSession(storage);
      session = null;
      submitController.reset();
      if (consentController) consentController.reset();
      if (el.watermark) el.watermark.checked = true;
      if (el.watermarkMessage) el.watermarkMessage.textContent = "";
      showForm();
      setMessage("请先同意拍摄，再选择风格并提交排队。", "ok");
      refreshSubmitState();
    }

    function leaveRecovery() {
      flowGeneration += 1;
      recoveryRequest += 1;
      recoveryPending = false;
      recoveryMode = false;
      recoveryEntry = null;
      recoveryUrl = "";
      queueUnavailable = false;
      startPending = false;
      pickupLookups = {};
      if (recoveryTimer !== null) timers.clearTimeout(recoveryTimer);
      recoveryTimer = null;
      try { storage.setItem(RECOVERY_SKIP_KEY, "1"); } catch (err) { }
      if (/^#restore(?:=|$)/.test(root.location.hash)) root.history.replaceState(null, "", "/join");
      if (el.unavailable) el.unavailable.classList.add("hide");
      if (el.recoveryDetails) el.recoveryDetails.open = false;
      if (el.recoveryMessage) el.recoveryMessage.textContent = "";
    }

    function syncVisible(event) {
      if (event && event.type === "pageshow" && !event.persisted) return;
      if (document.hidden) {
        if (poller) poller.stop("hidden");
        if (recoveryMode) recoveryRequest += 1;
        if (recoveryTimer !== null) timers.clearTimeout(recoveryTimer);
        recoveryTimer = null;
        return;
      }
      if (recoveryMode) { syncRecoveryLink(); return; }
      if (session) {
        if (queueUnavailable) return;
        pickupLookups = {};
        renderEntry(queueSessionEntry(session));
        if (!startPending) startPolling();
      } else {
        var snapshot = submitController.snapshot();
        if (snapshot.phase !== "submitting") recoverCurrent(snapshot.phase === "failed" && !!snapshot.requestId);
      }
    }

    function mountConsent() {
      if (!el.consent || !root.AutoFlowConsent) return;
      consentController = root.AutoFlowConsent.mountConsentPanel(el.consent, {
        beforeActions: el.watermarkNote,
        request: function (body) { return requestNoThrow(root.AutoFlowConsent.CONSENT_ENDPOINT, body); },
        onGranted: function () {
          captureReconsent = false;
          refreshSubmitState();
          if (registrationVisible && el.form) {
            el.form.setAttribute("tabindex", "-1");
            el.form.focus();
          }
        },
        onDeclined: function () { refreshSubmitState(); },
        onError: function (snap) { setMessage(snap.message || "同意登记失败，请重试。", "error"); },
      });
    }

    function loadFeatureState() {
      return jsonFetch("/api/public/status").catch(function () {
        return { status: 0, payload: null };
      }).then(function (result) {
        var payload = result.payload || {};
        presentationMode = payload.presentation_mode === "handoff" ? "handoff" : "full";
        var aigcReady = result.status === 200 && payload.aigc_enabled === true;
        var queueReady = result.status === 200 && payload.queue_enabled === true;
        if (!queueReady || !aigcReady) {
          if (!session && !recoveryMode) showUnavailable(!queueReady
            ? "手机排队暂未开放，请到现场直接拍摄。"
            : "风格生成暂未开放，当前不能手机排队；请到现场直接拍摄。\n");
          return false;
        }
        return true;
      });
    }

    if (el.submit) el.submit.addEventListener("click", onSubmit);
    if (el.start) el.start.addEventListener("click", startCapture);
    if (el.abandon) el.abandon.addEventListener("click", abandonQueue);
    if (el.reset) el.reset.addEventListener("click", resetFlow);
    if (el.copy) el.copy.addEventListener("click", copyRecoveryLink);
    if (el.recoveryLink) el.recoveryLink.addEventListener("click", function () { el.recoveryLink.select(); });
    if (el.watermark) el.watermark.addEventListener("change", function () {
      if (el.watermark.checked) {
        if (el.watermarkMessage) el.watermarkMessage.textContent = "";
        return;
      }
      var confirmed = typeof root.confirm === "function" && root.confirm("申请关闭照片上的 AI 生成标识前，请确认：发布或分享时，你将依法主动声明该内容由 AI 生成，并承担相应使用责任。确认关闭？");
      el.watermark.checked = !confirmed;
      if (el.watermarkMessage) el.watermarkMessage.textContent = confirmed
        ? "已关闭显式标识；发布或分享时，请依法主动声明由 AI 生成。" : "已保留 AI 生成标识。";
    });
    document.addEventListener("visibilitychange", syncVisible);
    root.addEventListener("pageshow", syncVisible);
    mountConsent();
    loadFeatureState().then(function (ready) {
      if (!ready) return;
      return loadStyleCatalog();
    }).then(function (loaded) {
      if (!loaded) return;
      styles = loaded.styles;
      styleSource = loaded.source;
      fillStyleSelect();
    });

    if (recoveryMode) {
      showStatus(null);
      if (recoveryToken) {
        renderRecoveryFailure(false);
        syncRecoveryLink();
      } else renderRecoveryFailure(true);
    } else if (session) {
      showStatus(queueSessionEntry(session));
      startPolling();
      attachRecovery();
    } else {
      showForm();
      recoverCurrent(false);
    }

    return {
      page: "join",
      styles: function () { return styles.slice(); },
      styleSource: function () { return styleSource; },
      submit: onSubmit,
      startCapture: startCapture,
      abandonQueue: abandonQueue,
      resetFlow: resetFlow,
      state: function () {
        return {
          page: "join",
          styleSource: styleSource,
          stylesCount: styles.length,
          consentPhase: consentController ? consentController.snapshot().phase : "unavailable",
          queue: session ? { queueId: session.queueId, status: session.status } : null,
          controller: submitController.snapshot(),
        };
      },
    };
  }
  /* ------------------------------------------------------------------ */
  /* 导出与自动挂载                                                       */
  /* ------------------------------------------------------------------ */

  var api = {
    VERSION: VERSION,
    CAROUSEL_INTERVAL_MS: CAROUSEL_INTERVAL_MS,
    POLL_INTERVAL_MS: POLL_INTERVAL_MS,
    POLL_BACKOFF_MAX_MS: POLL_BACKOFF_MAX_MS,
    STORE_KEY: STORE_KEY,
    LEGACY_STORE_KEYS: LEGACY_STORE_KEYS,
    STYLE_ENDPOINTS: STYLE_ENDPOINTS,
    OPTIONAL_FIELD_KEYS: OPTIONAL_FIELD_KEYS,
    STATIC_STYLE_FALLBACK: STATIC_STYLE_FALLBACK,
    TERMINAL_STATUSES: TERMINAL_STATUSES,
    messageFor: messageFor,
    displayNumberText: displayNumberText,
    newRequestId: newRequestId,
    thumbCandidatesFor: thumbCandidatesFor,
    normalizeStyleList: normalizeStyleList,
    carouselNextIndex: carouselNextIndex,
    createCarousel: createCarousel,
    collectOptionalFields: collectOptionalFields,
    buildQueueSubmission: buildQueueSubmission,
    normalizePortraitMode: normalizePortraitMode,
    portraitModeLabel: portraitModeLabel,
    extractError: extractError,
    normalizeEntry: normalizeEntry,
    interpretQueueEntryResponse: interpretQueueEntryResponse,
    interpretSubmitResponse: interpretSubmitResponse,
    recoveryPath: recoveryPath,
    pickupPath: pickupPath,
    nextSubmitAction: nextSubmitAction,
    queueStatusView: queueStatusView,
    shouldStopPolling: shouldStopPolling,
    pollDelayFor: pollDelayFor,
    createQueuePoller: createQueuePoller,
    buildStartRequest: buildStartRequest,
    interpretStartResponse: interpretStartResponse,
    readQueueSession: readQueueSession,
    writeQueueSession: writeQueueSession,
    clearQueueSession: clearQueueSession,
    joinUrlFromOrigin: joinUrlFromOrigin,
    createSubmitController: createSubmitController,
    mountEntryPage: mountEntryPage,
    mountJoinPage: mountJoinPage,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.AutoFlowEntry = api;

  function boot() {
    if (typeof document === "undefined") return;
    var page = document.body ? document.body.getAttribute("data-af-page") : "";
    try {
      if (page === "entry") root.AutoFlowEntryUi = mountEntryPage();
      else if (page === "join") root.AutoFlowEntryUi = mountJoinPage();
    } catch (err) {
      if (typeof console !== "undefined" && console.error) console.error("auto-flow 挂载失败：", err);
    }
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})(typeof window !== "undefined" ? window : globalThis);
