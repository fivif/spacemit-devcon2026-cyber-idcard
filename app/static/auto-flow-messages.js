/* auto-flow-messages.js —— 自动流程「错误码 → 中文界面文案」前端文案层（T-20）。
 *
 * 用途：前台页面（排队页 / 拍摄页 / 取图页 / 操作员页）拿到新契约错误体
 *   { "error": { "code": "...", "message": "...", "request_id": "..." } }
 * 时，用 messageFor(code) 得到统一的中文文案；未知错误码回落到兜底文案。
 *
 * 契约与边界：
 * - 文案与后端权威表 app/auto_flow/errors.py 的中文逐字一致；任何一侧改动必须两边同步；
 * - 纯查表：不发起请求、不做渲染、无框架、无 CDN、无外部字体，现场离线可用；
 * - 导出：浏览器挂 window.AutoFlowMessages；Node（测试用）走 module.exports。
 * 覆盖：02-spec「Error handling」表 17 个错误码 + 路由表补充码 6 个，与 errors.py 对齐。
 */
(function (root) {
  "use strict";

  var FALLBACK = "出了点小问题，请稍后重试或找现场工作人员。";

  // 键 = 稳定错误码；值 = 与 app/auto_flow/errors.py 逐字一致的中文文案。
  var MESSAGES = {
    // —— 「Error handling」表（02-spec）——
    INVALID_REQUEST: "请求格式不正确，请检查后重试。",
    INVALID_TOKEN: "取图凭证无效。",
    CONSENT_REQUIRED: "缺少有效的拍摄授权，请先同意拍摄。",
    STYLE_NOT_FOUND: "所选风格不存在。",
    IDEMPOTENCY_CONFLICT: "该请求编号已用于另一份提交，请刷新后重试。",
    QUEUE_FULL: "当前排队人数已满，请稍后再试。",
    QUEUE_EXPIRED: "排队已超时，请重新提交。",
    NOT_CALLED: "还未轮到本次拍摄，请等待叫号。",
    CAMERA_UNAVAILABLE: "相机暂时不可用，请找现场工作人员。",
    GESTURE_DISABLED: "手势拍照暂不可用，请用按钮开始。",
    CAPTURE_FAILED: "拍摄失败，未生成照片；请重试或找工作人员。",
    AIGC_FAILED: "风格化处理失败，已保留原图结果。",
    AIGC_TIMEOUT: "风格化处理超时，已保留原图结果。",
    PHOTO_NOT_FOUND: "照片不存在。",
    PHOTO_DELETED: "照片已被删除。",
    PRINT_UNAVAILABLE: "打印服务暂不可用。",
    INTERNAL_ERROR: "服务内部错误，请稍后重试。",
    // —— 路由表补充码 ——
    QUEUE_NOT_FOUND: "排队记录不存在。",
    JOB_NOT_FOUND: "作业不存在。",
    JOB_NOT_WAITING_FOR_CONSENT: "当前作业不在等待外置处理确认的状态。",
    INTERNAL_ONLY: "该接口仅供内部使用。",
    DELETE_IN_PROGRESS: "删除正在进行，请稍后重试。",
    PRINT_JOB_NOT_FOUND: "打印任务不存在。"
  };

  function messageFor(code) {
    if (typeof code === "string" && Object.prototype.hasOwnProperty.call(MESSAGES, code)) {
      return MESSAGES[code];
    }
    return FALLBACK;
  }

  var api = {
    messages: MESSAGES,
    codes: Object.keys(MESSAGES),
    fallback: FALLBACK,
    messageFor: messageFor
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.AutoFlowMessages = api;
})(typeof window !== "undefined" ? window : globalThis);
