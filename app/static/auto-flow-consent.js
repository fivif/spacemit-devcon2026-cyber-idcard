/* auto-flow-consent.js —— 拍摄告知与同意（T-12）。
 *
 * 用途：入队前向参与者展示中文《拍摄告知》（用途 / 仅人像分割不人脸识别 /
 * 可删除 / 外部图像处理信息 / 个人文字字段可留空或展示在照片），并在点击「同意」后
 * 调 POST /api/consent 获取拍摄同意令牌；令牌随队列提交一起交给后端绑定。
 *
 * 契约与边界：
 * - 纯逻辑（告知文案、请求体、响应解释、状态机）与 DOM 绑定分离：
 *   noticeLines()/buildCaptureConsentRequest()/interpretConsentResponse()/
 *   createConsentController() 可在 Node 下直接测试；mountConsentPanel() 才碰 DOM；
 * - 无框架、无 CDN、无外部字体、无默认勾选：同意动作必须由参与者显式点击触发；
 * - 不承诺人脸识别：本装置只做人像分割（把人物从背景分离），不做人脸识别、
 *   身份比对或人脸建库；文案见 NOTICE；
 * - 导出：浏览器挂 window.AutoFlowConsent；Node（测试用）走 module.exports。
 * - 后端契约：成功 {ok:true, token, consent_id, purpose, expires_in, retention, at}；
 *   拒绝 200 {ok:false, error:"consent_declined"}；错误 {error:{code,message,...}}。
 */
(function (root) {
  "use strict";

  var CONSENT_ENDPOINT = "/api/consent";

  // 拍摄告知：逐条展示，供页面渲染与测试断言；文案改动需同步后端保留策略说明。
  var NOTICE = {
    title: "拍摄告知（请阅读后点击“同意”）",
    introduction: "本活动由SpacemiT（以下简称“我们”）在 SpacemiT DevCon 2026 开发者大会现场运营。请在拍摄前仔细阅读：",
    consentLabel: "同意",
    declineLabel: "暂不拍摄",
    lines: [
      "活动将通过大会现场拍摄生成 SpacemiT DevCon 2026 开发者电子证件照，仅本人可领取。",
      "本次拍摄图像处理仅限于人像抠图（将人物从背景中分离）与基础证件照排版；我们不会进行人脸识别、身份比对或用于建任何人脸库或特征库。如您主动选择“AI 风格化”等增强效果，该步骤可能由第三方模型完成，我们会在您触发时单独弹出一次同意提示，并告知您第三方的名称及处理方式；不同意不影响基础拍摄与取图。",
      "活动对您的个人信息不作强制搜集，姓名、昵称等字段均可留空，仅作为排队标识，我们不会因您留空而拒绝服务。您填写的信息及拍摄照片均仅临时留存，链接自生成之日起【1】日内有效，链接失效后，相应数据将无法继续查看，活动不会留存任何副本。",
      "请在链接有效期内保存照片，您也可以随时对照片采取删除操作或请现场工作人员协助删除。"
    ],
    contact: "如有任何疑问或投诉，请联系现场工作人员。",
    confirmation: "点击“同意”即表示您已阅读并理解上述规则，同意后您将获取专属排队号码并加入排队。"
  };

  // 同意用途码（与后端 app/auto_flow/models.py 的 CONSENT_PURPOSES 逐字一致）。
  var CONSENT_PURPOSES = { capture: "capture", externalProcessing: "external_processing" };

  var DECLINED_MESSAGE = "你暂时没有同意拍摄，可以随时再点同意。";
  var GENERIC_ERROR_MESSAGE = "同意没有登记成功，请重试或找现场工作人员。";

  function noticeLines() {
    return NOTICE.lines.slice();
  }

  function noticeText() {
    return [NOTICE.title, NOTICE.introduction].concat(noticeLines().map(function (line, index) {
      return (index + 1) + ". " + line;
    }), [NOTICE.contact, NOTICE.confirmation]).join("\n");
  }

  function syncNoticeText(container) {
    if (!container) return;
    var title = container.querySelector("h2");
    var list = container.querySelector("ol");
    var paragraphs = container.querySelectorAll("p");
    if (!title || !list || paragraphs.length < 3) return;
    title.textContent = NOTICE.title;
    paragraphs[0].textContent = NOTICE.introduction;
    list.innerHTML = "";
    noticeLines().forEach(function (line) {
      var item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });
    paragraphs[1].textContent = NOTICE.contact;
    paragraphs[2].textContent = NOTICE.confirmation;
  }

  function buildCaptureConsentRequest() {
    return { agree: true, purpose: CONSENT_PURPOSES.capture };
  }

  function buildExternalConsentRequest() {
    return { agree: true, purpose: CONSENT_PURPOSES.externalProcessing };
  }

  // 旧格式请求体：{agree:true, kind:"photo"}，后端保持 {ok, token, ...} 旧响应形状。
  function buildLegacyConsentRequest() {
    return { agree: true, kind: "photo" };
  }

  /* 解释 POST /api/consent 的响应（纯逻辑）。
   * 返回 {ok, declined, token, purpose, message}；declined 表示参与者拒绝或服务端
   * 明确返回 consent_declined，ok 表示已拿到可用令牌。 */
  function interpretConsentResponse(status, payload) {
    var body = payload && typeof payload === "object" ? payload : {};
    if (body.ok === true && typeof body.token === "string" && body.token) {
      return {
        ok: true,
        declined: false,
        token: body.token,
        purpose: typeof body.purpose === "string" ? body.purpose : "",
        message: ""
      };
    }
    if (body.error === "consent_declined") {
      return { ok: false, declined: true, token: "", purpose: "", message: DECLINED_MESSAGE };
    }
    var message = GENERIC_ERROR_MESSAGE;
    if (body.error && typeof body.error === "object" && typeof body.error.message === "string") {
      message = body.error.message;
    } else if (typeof body.detail === "string" && body.detail) {
      message = body.detail;
    } else if (typeof status === "number" && status >= 500) {
      message = "同意服务暂时不可用，请重试或找现场工作人员。";
    }
    return { ok: false, declined: false, token: "", purpose: "", message: message };
  }

  /* 同意状态机（纯逻辑，不碰 DOM）。
   * options.request(body) 必须返回 Promise<{status, payload}>；
   * options.subscribe(listener) 可选，state 变化时收到快照。 */
  function createConsentController(options) {
    var opts = options || {};
    if (typeof opts.request !== "function") {
      throw new TypeError("createConsentController 需要 options.request(body) -> Promise");
    }
    var listeners = [];
    var state = { phase: "idle", token: "", purpose: "", message: "" };

    function snapshot() {
      return { phase: state.phase, token: state.token, purpose: state.purpose, message: state.message };
    }

    function emit() {
      var current = snapshot();
      for (var i = 0; i < listeners.length; i += 1) {
        listeners[i](current);
      }
    }

    function grant(purpose) {
      if (state.phase === "requesting") {
        return Promise.resolve(snapshot());
      }
      state.phase = "requesting";
      state.message = "";
      emit();
      var body = purpose === CONSENT_PURPOSES.externalProcessing
        ? buildExternalConsentRequest()
        : buildCaptureConsentRequest();
      return Promise.resolve(opts.request(body)).then(function (response) {
        var parsed = interpretConsentResponse(response && response.status, response && response.payload);
        if (parsed.ok) {
          state.phase = "granted";
          state.token = parsed.token;
          state.purpose = parsed.purpose || body.purpose;
          state.message = "";
        } else if (parsed.declined) {
          state.phase = "declined";
          state.token = "";
          state.message = parsed.message;
        } else {
          state.phase = "error";
          state.token = "";
          state.message = parsed.message;
        }
        emit();
        return snapshot();
      }, function (error) {
        state.phase = "error";
        state.token = "";
        state.message = (error && error.message) || GENERIC_ERROR_MESSAGE;
        emit();
        return snapshot();
      });
    }

    function decline() {
      state.phase = "declined";
      state.token = "";
      state.message = DECLINED_MESSAGE;
      emit();
      return snapshot();
    }

    function reset() {
      state = { phase: "idle", token: "", purpose: "", message: "" };
      emit();
      return snapshot();
    }

    function subscribe(listener) {
      if (typeof listener === "function") {
        listeners.push(listener);
      }
      return function unsubscribe() {
        var index = listeners.indexOf(listener);
        if (index >= 0) {
          listeners.splice(index, 1);
        }
      };
    }

    return {
      grant: grant,
      decline: decline,
      reset: reset,
      subscribe: subscribe,
      snapshot: snapshot,
      getToken: function () { return state.token; }
    };
  }

  function defaultRequest(body) {
    return fetch(CONSENT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (payload) {
        return { status: response.status, payload: payload };
      });
    });
  }

  /* DOM 绑定：把告知文本与两个按钮挂到容器；无默认勾选，点击才发请求。
   * options.onGranted(snapshot) / options.onDeclined(snapshot) / options.onError(snapshot)
   * 可选；返回 controller。 */
  function mountConsentPanel(container, options) {
    var opts = options || {};
    if (!container || typeof document === "undefined") {
      throw new Error("mountConsentPanel 需要浏览器 DOM 容器");
    }
    var controller = createConsentController({
      request: opts.request || defaultRequest
    });

    container.innerHTML = "";
    var title = document.createElement("h2");
    title.textContent = NOTICE.title;
    container.appendChild(title);

    var introduction = document.createElement("p");
    introduction.textContent = NOTICE.introduction;
    container.appendChild(introduction);

    var list = document.createElement("ol");
    noticeLines().forEach(function (line) {
      var item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });
    container.appendChild(list);

    var contact = document.createElement("p");
    contact.textContent = NOTICE.contact;
    container.appendChild(contact);
    var confirmation = document.createElement("p");
    confirmation.textContent = NOTICE.confirmation;
    container.appendChild(confirmation);

    var accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = NOTICE.consentLabel;
    var declineBtn = document.createElement("button");
    declineBtn.type = "button";
    declineBtn.textContent = NOTICE.declineLabel;

    var message = document.createElement("p");
    message.setAttribute("role", "status");

    function render(current) {
      message.textContent = current.message || "";
      accept.disabled = current.phase === "requesting";
      // 同意后要看得出来：按钮名不变（读屏与测试都按名字找），只换外观并加「已同意」标记
      container.classList.toggle("is-granted", current.phase === "granted");
    }

    accept.addEventListener("click", function () {
      controller.grant().then(function (current) {
        if (current.phase === "granted" && typeof opts.onGranted === "function") {
          opts.onGranted(current);
        } else if (current.phase === "declined" && typeof opts.onDeclined === "function") {
          opts.onDeclined(current);
        } else if (current.phase === "error" && typeof opts.onError === "function") {
          opts.onError(current);
        }
      });
    });
    declineBtn.addEventListener("click", function () {
      var current = controller.decline();
      if (typeof opts.onDeclined === "function") {
        opts.onDeclined(current);
      }
    });

    var buttons = document.createElement("div");
    buttons.appendChild(accept);
    buttons.appendChild(declineBtn);
    if (opts.beforeActions) container.appendChild(opts.beforeActions);
    container.appendChild(buttons);
    container.appendChild(message);
    controller.subscribe(render);
    render(controller.snapshot());
    return controller;
  }

  var api = {
    CONSENT_ENDPOINT: CONSENT_ENDPOINT,
    NOTICE: NOTICE,
    CONSENT_PURPOSES: CONSENT_PURPOSES,
    noticeLines: noticeLines,
    noticeText: noticeText,
    buildCaptureConsentRequest: buildCaptureConsentRequest,
    buildExternalConsentRequest: buildExternalConsentRequest,
    buildLegacyConsentRequest: buildLegacyConsentRequest,
    interpretConsentResponse: interpretConsentResponse,
    createConsentController: createConsentController,
    mountConsentPanel: mountConsentPanel
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.AutoFlowConsent = api;
  if (typeof document !== "undefined") {
    syncNoticeText(document.querySelector("#legal .legal"));
  }
})(typeof window !== "undefined" ? window : globalThis);
