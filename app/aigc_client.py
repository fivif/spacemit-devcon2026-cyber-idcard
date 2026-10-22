"""OpenAI-compatible image editing client for portrait stylization."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from urllib.parse import quote, urlparse

from PIL import Image, ImageChops, ImageFilter

from aigc_channels import AIGC_USER_AGENT, channel_api_key, selected_model


class AIGCError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def enabled(channels=None) -> bool:
    if (os.environ.get("AIGC_ENABLED", "0") != "1"
            or os.environ.get("AIGC_BACKEND", "disabled") != "public_api"):
        return False
    if channels is not None and channels.has_channels():
        return any(item["enabled"] and channel_api_key(item) and selected_model(item)
                   for item in channels.configs())
    return (bool(os.environ.get("AIGC_ENDPOINT", "").strip())
            and bool(os.environ.get("AIGC_API_KEY_FILE", "").strip()))


def _key() -> str:
    path = os.environ.get("AIGC_API_KEY_FILE", "").strip()
    if not path:
        raise AIGCError("AIGC_API_KEY_FILE 未配置")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as exc:
        raise AIGCError("AIGC 密钥文件不可读") from exc
    if not value:
        raise AIGCError("AIGC 密钥为空")
    return value


def _prepare_portrait_plate(photo: Image.Image, alpha, size: str):
    width, height = (int(part) for part in size.lower().split("x", 1))
    rgb = photo.convert("RGB")
    if alpha is None:
        raise AIGCError("人像抠像不可用，跳过 AIGC")
    if isinstance(alpha, Image.Image):
        alpha_img = alpha.convert("L")
    else:
        alpha_img = Image.fromarray(alpha).convert("L")
    if alpha_img.size != rgb.size:
        alpha_img = alpha_img.resize(rgb.size, Image.Resampling.BILINEAR)
    bbox = alpha_img.getbbox()
    if bbox:
        left, top, right, bottom = bbox
        pad_x = max(8, int((right - left) * 0.10))
        pad_y = max(8, int((bottom - top) * 0.08))
        left = max(0, left - pad_x)
        top = max(0, top - pad_y)
        right = min(rgb.width, right + pad_x)
        bottom = min(rgb.height, bottom + pad_y)
        rgb = rgb.crop((left, top, right, bottom))
        alpha_img = alpha_img.crop((left, top, right, bottom))
    # 摆位来自 subject_rules（现场可调），不再写死：
    #   scale_x/scale_y = 人像最多占画布宽/高的比例
    #   top             = 人像顶部相对画布顶部的距离
    scale_x, scale_y, top_ratio = _plate_framing()
    scale = min((width * scale_x) / max(1, rgb.width),
                (height * scale_y) / max(1, rgb.height))
    target = (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale)))
    rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    alpha_img = alpha_img.resize(target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (127, 127, 127))
    guide = Image.new("L", (width, height), 0)
    position = ((width - target[0]) // 2, int(height * top_ratio))
    canvas.paste(rgb, position, alpha_img)
    guide.paste(alpha_img, position)
    return canvas, guide


def _plate_framing() -> tuple[float, float, float]:
    """人像在 AIGC 画布上的摆位（比例）。读不到配置就用既定默认值。

    单独抽出来是为了让 aigc_client 不依赖 subject_rules 的存在 —— 这个模块
    在测试里被单独 import 过很多次，不该因为缺一个可选配置模块就挂。
    """
    try:
        import sys
        from pathlib import Path
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from subject_rules import rules
        r = rules()
        return float(r["plate_scale_x"]), float(r["plate_scale_y"]), float(r["plate_top"])
    except Exception:
        return 0.74, 0.84, 0.10


def _upload_image(photo: Image.Image, alpha, size: str) -> bytes:
    canvas, _guide = _prepare_portrait_plate(photo, alpha, size)
    output = BytesIO()
    canvas.save(output, "JPEG", quality=88, optimize=True)
    return output.getvalue()


def portrait_guide(photo: Image.Image, alpha, size: str) -> Image.Image:
    """Return the source silhouette in the exact coordinate system sent to AIGC."""
    _canvas, guide = _prepare_portrait_plate(photo, alpha, size)
    return guide


def _portrait_extrema(image: Image.Image, size: int, maximum: bool = True) -> Image.Image:
    import numpy as np

    pixels = np.asarray(image.convert("L"), dtype=np.uint8)
    operation = np.maximum if maximum else np.minimum
    radius = size // 2
    for axis in (0, 1):
        padding = [(0, 0), (0, 0)]
        padding[axis] = (radius, radius)
        pixels = np.pad(pixels, padding, mode="edge")
        covered = 1
        while covered < size:
            offset = min(covered, size - covered)
            before = [slice(None), slice(None)]
            after = [slice(None), slice(None)]
            before[axis] = slice(None, -offset)
            after[axis] = slice(offset, None)
            pixels = operation(pixels[tuple(before)], pixels[tuple(after)])
            covered += offset
    return Image.fromarray(pixels, "L")


def _largest_alpha_component(matte: Image.Image) -> Image.Image:
    """Drop disconnected portrait-like background objects without OpenCV."""
    from collections import deque

    import numpy as np

    width, height = matte.size
    scale = min(1.0, 384.0 / max(width, height))
    small_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    small = matte.resize(small_size, Image.Resampling.NEAREST)
    binary = np.asarray(small, np.uint8) > 32
    visited = np.zeros(binary.shape, dtype=bool)
    best: list[tuple[int, int]] = []
    rows, columns = binary.shape
    for start_y in range(rows):
        for start_x in range(columns):
            if not binary[start_y, start_x] or visited[start_y, start_x]:
                continue
            queue = deque([(start_y, start_x)])
            visited[start_y, start_x] = True
            component: list[tuple[int, int]] = []
            while queue:
                y, x = queue.popleft()
                component.append((y, x))
                for next_y in range(max(0, y - 1), min(rows, y + 2)):
                    for next_x in range(max(0, x - 1), min(columns, x + 2)):
                        if (binary[next_y, next_x]
                                and not visited[next_y, next_x]):
                            visited[next_y, next_x] = True
                            queue.append((next_y, next_x))
            if len(component) > len(best):
                best = component
    if not best:
        return matte
    selected = np.zeros(binary.shape, dtype=np.uint8)
    for y, x in best:
        selected[y, x] = 255
    guard = Image.fromarray(selected, "L").resize(matte.size, Image.Resampling.NEAREST)
    guard = _portrait_extrema(guard, 9)
    return ImageChops.darker(matte, guard)


def constrain_alpha(alpha: Image.Image, coarse_mask: Image.Image | None) -> Image.Image:
    """Keep the fine matte inside the detected main-person silhouette.

    MODNet can occasionally retain a face, reflection or poster far away from the
    detected person.  That disconnected patch expands the crop and makes the real
    portrait tiny.  The coarse person mask is less precise around hair, but is a
    reliable spatial guard, so clean tiny islands, dilate it, then use it only as
    an outer limit for the fine matte.
    """
    matte = _largest_alpha_component(alpha.convert("L"))
    if coarse_mask is None:
        return matte
    coarse = coarse_mask.convert("L")
    if coarse.size != matte.size:
        coarse = coarse.resize(matte.size, Image.Resampling.NEAREST)
    coarse = coarse.point(lambda value: 255 if value >= 96 else 0)
    if coarse.getbbox() is None:
        return matte

    shortest = min(coarse.size)
    opening = 5 if shortest >= 5 else 3
    cleaned = _portrait_extrema(_portrait_extrema(coarse, opening, maximum=False), opening)
    if cleaned.getbbox() is None:
        cleaned = coarse
    grow = min(31, max(7, int(round(shortest * 0.025)) | 1))
    allowed = _portrait_extrema(cleaned, grow)
    return ImageChops.darker(matte, allowed)


def refine_generated_alpha(alpha: Image.Image, guide: Image.Image) -> Image.Image:
    """Remove AIGC-added sticker borders, floor shadows and neutral-gray halos."""
    matte = alpha.convert("L")

    # The generated character may be taller/wider than the source guide, and
    # MODNet can split hair and clothing into separate large components.  The
    # neutral-gray AIGC plate has no real background objects, so clipping against
    # the source silhouette or keeping only one component destroys valid body
    # parts.  Trust the generated matte shape and only remove its faint halo.
    matte = matte.point(
        lambda value: 0 if value <= 64 else 255 if value >= 224
        else int(((value - 64) / 160.0) ** 1.45 * 255))
    matte = matte.filter(ImageFilter.MinFilter(5))
    return matte.filter(ImageFilter.GaussianBlur(0.45))


def _decontaminate_edges(photo: Image.Image, alpha: Image.Image, radius: float) -> Image.Image:
    """Replace gray/white plate contamination in the soft edge with foreground color."""
    import numpy as np

    rgb = np.asarray(photo.convert("RGB"), np.float32)
    matte = np.asarray(alpha.convert("L"), np.float32) / 255.0
    if not bool(((matte > 0.01) & (matte < 0.99)).any()):
        return photo.convert("RGB")
    solid = (matte >= 0.82).astype(np.float32)
    if not bool(solid.any()):
        solid = matte
    solid_image = Image.fromarray(np.clip(solid * 255, 0, 255).astype("uint8"), "L")
    denominator = np.asarray(
        solid_image.filter(ImageFilter.GaussianBlur(radius)), np.float32) / 255.0
    foreground = np.empty_like(rgb)
    for channel in range(3):
        weighted = Image.fromarray(np.clip(rgb[..., channel] * solid, 0, 255).astype("uint8"), "L")
        numerator = np.asarray(weighted.filter(ImageFilter.GaussianBlur(radius)), np.float32)
        foreground[..., channel] = numerator / np.maximum(denominator, 0.01)
    replace = (matte < 0.98) & (denominator > 0.01)
    cleaned = np.where(replace[..., None], foreground, rgb)
    return Image.fromarray(np.clip(cleaned, 0, 255).astype("uint8"), "RGB")


def fit_cutout(photo: Image.Image, alpha: Image.Image, size: tuple[int, int] = (780, 1000)):
    """Fit generated RGB and its alpha to the portrait window without distortion."""
    mask = alpha.convert("L")
    bbox = mask.point(lambda value: 255 if value > 32 else 0).getbbox()
    if not bbox:
        raise AIGCError("AIGC 人像边界为空")
    left, top, right, bottom = bbox
    bw, bh = right - left, bottom - top
    target_ratio = size[0] / float(size[1])
    pad = 0.34
    box_h = bh * (1.0 + pad)
    box_w = max(bw * (1.0 + pad), box_h * target_ratio)
    box_h = max(box_h, box_w / target_ratio)
    cx, cy = (left + right) / 2.0, (top + bottom) / 2.0 - 0.05 * box_h
    box_w = min(box_w, photo.width)
    box_h = min(box_h, photo.height)
    box_w = min(box_w, box_h * target_ratio)
    box_h = box_w / target_ratio
    cx = min(max(cx, box_w / 2.0), photo.width - box_w / 2.0)
    cy = min(max(cy, box_h / 2.0), photo.height - box_h / 2.0)
    crop = (int(round(cx - box_w / 2.0)), int(round(cy - box_h / 2.0)),
            int(round(cx + box_w / 2.0)), int(round(cy + box_h / 2.0)))
    crop = (max(0, crop[0]), max(0, crop[1]), min(photo.width, crop[2]), min(photo.height, crop[3]))
    fitted_mask = mask.crop(crop).resize(size, Image.Resampling.BILINEAR)
    fitted_photo = photo.convert("RGB").crop(crop).resize(size, Image.Resampling.LANCZOS)
    fitted_photo = _decontaminate_edges(
        fitted_photo, fitted_mask, radius=max(12.0, min(size) * 0.040))
    return fitted_photo, fitted_mask


def _multipart(fields: dict[str, str], name: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----DevConAIGC" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            ("--%s\r\n" % boundary).encode(),
            ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % key).encode(),
            str(value).encode(), b"\r\n",
        ])
    chunks.extend([
        ("--%s\r\n" % boundary).encode(),
        ("Content-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n" % (name, filename)).encode(),
        b"Content-Type: image/jpeg\r\n\r\n", payload, b"\r\n",
        ("--%s--\r\n" % boundary).encode(),
    ])
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


def _modelscope_json(request, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AIGCError("魔搭生成超时，请稍后重试")
    try:
        with urllib.request.urlopen(request, timeout=remaining,
                                    context=ssl.create_default_context()) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise AIGCError("魔搭请求失败 HTTP %s" % exc.code,
                        retryable=exc.code not in (400, 404, 422)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIGCError("魔搭网络请求失败或超时") from exc
    try:
        if len(raw) > 1024 * 1024:
            raise ValueError
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError) as exc:
        raise AIGCError("魔搭返回的任务信息不可用") from exc


def _request_modelscope(image_payload: bytes, *, prompt: str, model: str,
                        endpoint_base: str, api_key: str, timeout: float) -> Image.Image:
    if len(prompt) > 4000:
        raise AIGCError("魔搭提示词超过 4000 字符，请缩短风格提示词", retryable=False)
    endpoint = endpoint_base.rstrip("/")
    if not urlparse(endpoint).path.rstrip("/"):
        endpoint += "/v1"
    deadline = time.monotonic() + max(5.0, timeout)
    headers = {"Authorization": "Bearer " + api_key,
               "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": AIGC_USER_AGENT}
    payload = json.dumps({
        "model": model, "prompt": prompt,
        "image_url": ["data:image/jpeg;base64," + base64.b64encode(image_payload).decode("ascii")],
    }, ensure_ascii=False).encode("utf-8")
    submitted = _modelscope_json(urllib.request.Request(
        endpoint + "/images/generations", data=payload, method="POST",
        headers=headers | {"X-ModelScope-Async-Mode": "true"}), deadline)
    task_id = submitted.get("task_id")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise AIGCError("魔搭未返回生成任务编号")
    task_request = urllib.request.Request(
        endpoint + "/tasks/" + quote(task_id, safe=""), method="GET",
        headers=headers | {"X-ModelScope-Task-Type": "image_generation"})
    while True:
        result = _modelscope_json(task_request, deadline)
        status = str(result.get("task_status", "")).upper()
        if status == "SUCCEED":
            break
        if status in ("FAILED", "CANCELED", "CANCELLED"):
            raise AIGCError("魔搭生成失败或任务已取消，请检查模型权限、额度及输入图片")
        if status not in ("PENDING", "RUNNING", "QUEUED", "WAITING", "PROCESSING"):
            raise AIGCError("魔搭返回了未知任务状态")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AIGCError("魔搭生成超时，请稍后重试")
        time.sleep(min(2.0, remaining))
    urls = result.get("output_images")
    if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
        raise AIGCError("魔搭未返回生成图片")
    parsed = urlparse(urls[0])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AIGCError("魔搭返回的图片地址无效")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AIGCError("魔搭生成超时，请稍后重试")
    try:
        with urllib.request.urlopen(urllib.request.Request(
                urls[0], headers={"User-Agent": AIGC_USER_AGENT}), timeout=remaining,
                                    context=ssl.create_default_context()) as response:
            if urlparse(response.geturl()).scheme != "https":
                raise AIGCError("魔搭返回的图片地址无效")
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise AIGCError("魔搭返回图片过大")
        with Image.open(BytesIO(raw)) as source:
            if source.width * source.height > 4_000_000:
                raise AIGCError("AIGC 返回图片超过像素上限")
            image = source.convert("RGB")
            image.load()
        return image
    except AIGCError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise AIGCError("魔搭生成图片下载或读取失败") from exc


def _request_ark(image_payload: bytes, *, prompt: str, model: str,
                 endpoint_base: str, api_key: str, timeout: float) -> Image.Image:
    """Call Ark Seedream's image generation endpoint with one input image.

    Ark uses ``/images/generations`` for both text-to-image and image-to-image;
    the input is sent as a data URI in ``image``.  The response is normalized to
    the same Image object used by the OpenAI-compatible channels.
    """
    endpoint = endpoint_base.rstrip("/") + "/images/generations"
    image_data = "data:image/jpeg;base64," + base64.b64encode(image_payload).decode("ascii")
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "image": image_data,
        "size": "2K",
        "response_format": "url",
        "watermark": True,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, method="POST", headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": AIGC_USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=max(5.0, timeout),
                                    context=ssl.create_default_context()) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        retryable = exc.code not in (400, 401, 403, 404, 422)
        raise AIGCError("火山方舟请求失败 HTTP %s" % exc.code,
                        retryable=retryable) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIGCError("火山方舟网络请求失败或超时") from exc
    if len(raw) > 16 * 1024 * 1024:
        raise AIGCError("火山方舟返回图片过大")
    try:
        body = json.loads(raw.decode("utf-8"))
        item = body["data"][0]
        if item.get("b64_json"):
            image_raw = base64.b64decode(item["b64_json"], validate=True)
        else:
            image_url = item["url"]
            parsed = urlparse(image_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
            with urllib.request.urlopen(urllib.request.Request(
                    image_url, headers={"User-Agent": AIGC_USER_AGENT}),
                    timeout=max(5.0, timeout), context=ssl.create_default_context()) as response:
                image_raw = response.read(16 * 1024 * 1024 + 1)
        with Image.open(BytesIO(image_raw)) as source:
            image = source.convert("RGB")
            image.load()
        if image.width * image.height > 4_000_000:
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        return image
    except AIGCError:
        raise
    except Exception as exc:
        raise AIGCError("火山方舟返回图片不可用") from exc


def _request_edit(image_payload: bytes, *, prompt: str, model: str, size: str,
                  endpoint_base: str, api_key: str, timeout: float) -> Image.Image:
    hostname = urlparse(endpoint_base).hostname
    if hostname == "api-inference.modelscope.cn":
        return _request_modelscope(image_payload, prompt=prompt, model=model,
                                   endpoint_base=endpoint_base, api_key=api_key, timeout=timeout)
    if hostname == "ark.cn-beijing.volces.com":
        return _request_ark(image_payload, prompt=prompt, model=model,
                            endpoint_base=endpoint_base, api_key=api_key, timeout=timeout)
    endpoint = endpoint_base.rstrip("/") + "/images/edits"
    common_headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": AIGC_USER_AGENT,
    }

    def send(request):
        try:
            with urllib.request.urlopen(request, timeout=max(5.0, timeout),
                                        context=ssl.create_default_context()) as response:
                return response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, TimeoutError):
                raise AIGCError("AIGC 请求超时（等待上限 %g 秒），生成服务响应过慢" % max(5.0, timeout)) from exc
            raise AIGCError("AIGC 网络请求失败") from exc

    payload, content_type = _multipart(
        {"model": model, "prompt": prompt, "size": size, "n": "1"},
        "image", "portrait.jpg", image_payload)
    request = urllib.request.Request(endpoint, data=payload, method="POST", headers={
        **common_headers, "Content-Type": content_type,
    })
    try:
        raw = send(request)
    except urllib.error.HTTPError as first_error:
        if first_error.code not in (400, 403, 404, 405, 415, 422):
            retryable = first_error.code not in (400, 404, 422)
            raise AIGCError("AIGC 请求失败 HTTP %s" % first_error.code,
                            retryable=retryable) from first_error
        json_payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "images": ["data:image/jpeg;base64," + base64.b64encode(image_payload).decode("ascii")],
            "size": size,
            "n": 1,
            "response_format": "b64_json",
        }, ensure_ascii=False).encode("utf-8")
        json_request = urllib.request.Request(endpoint, data=json_payload, method="POST", headers={
            **common_headers, "Content-Type": "application/json",
        })
        try:
            raw = send(json_request)
        except urllib.error.HTTPError as exc:
            retryable = exc.code not in (400, 404, 422)
            raise AIGCError("AIGC 请求失败 HTTP %s" % exc.code, retryable=retryable) from exc
    if len(raw) > 8 * 1024 * 1024:
        raise AIGCError("AIGC 返回过大")
    try:
        body = json.loads(raw.decode("utf-8"))
        item = body["data"][0]
        if item.get("b64_json"):
            image_raw = base64.b64decode(item["b64_json"], validate=True)
        else:
            image_url = item["url"]
            parsed = urlparse(image_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
            image_request = urllib.request.Request(image_url, headers={"User-Agent": AIGC_USER_AGENT})
            with urllib.request.urlopen(image_request, timeout=max(5.0, timeout),
                                        context=ssl.create_default_context()) as response:
                image_raw = response.read(16 * 1024 * 1024 + 1)
            if len(image_raw) > 16 * 1024 * 1024:
                raise AIGCError("AIGC 返回图片过大")
        source = Image.open(BytesIO(image_raw))
        if source.width * source.height > 4_000_000:
            raise AIGCError("AIGC 返回图片超过像素上限")
        image = source.convert("RGB")
        image.load()
    except AIGCError:
        raise
    except Exception as exc:
        raise AIGCError("AIGC 返回图片不可用") from exc
    return image


def edit_portrait_with_channel(photo: Image.Image, alpha, *, prompt: str, model: str,
                               size: str, channel: dict, channels=None) -> Image.Image:
    """Test one exact channel's selected model without failover."""
    model = selected_model(channel)
    if not prompt.strip():
        raise AIGCError("提示词为空", retryable=False)
    if not model.strip():
        raise AIGCError("AIGC 模型未配置", retryable=False)
    if "x" not in size:
        raise AIGCError("AIGC 尺寸格式不正确", retryable=False)
    api_key = channel_api_key(channel)
    if not api_key:
        raise AIGCError("这个渠道尚未配置 API Key", retryable=False)
    image_payload = _upload_image(photo, alpha, size)
    try:
        image = _request_edit(
            image_payload, prompt=prompt, model=model, size=size,
            endpoint_base=channel["base_url"], api_key=api_key,
            timeout=float(channel.get("timeout", 120)))
    except AIGCError as exc:
        if channels is not None:
            channels.record_result(channel["id"], False, str(exc), retryable=exc.retryable,
                                   channel=channel)
        raise
    if channels is not None:
        channels.record_result(channel["id"], True, channel=channel)
    return image


def edit_portrait(photo: Image.Image, alpha, *, prompt: str, model: str,
                  size: str, channels=None, result_meta: dict | None = None) -> Image.Image:
    if not prompt.strip():
        raise AIGCError("提示词为空")
    has_channels = channels is not None and channels.has_channels()
    if not has_channels and not model.strip():
        raise AIGCError("AIGC 模型未配置")
    if "x" not in size:
        raise AIGCError("AIGC 尺寸格式不正确")
    image_payload = _upload_image(photo, alpha, size)
    if has_channels:
        primary_model, candidates = channels.race_candidates()
        if not candidates:
            raise AIGCError("当前没有可用的生成渠道，请检查渠道状态与密钥")
        if len(candidates) == 1:
            channel, candidate_model = candidates[0]
            started = time.perf_counter()
            try:
                image = _request_edit(
                    image_payload, prompt=prompt, model=candidate_model, size=size,
                    endpoint_base=channel["base_url"], api_key=channel_api_key(channel),
                    timeout=float(channel.get("timeout", 120)))
            except AIGCError as exc:
                channels.record_result(channel["id"], False, str(exc),
                                       retryable=exc.retryable, channel=channel)
                raise
            channels.record_result(channel["id"], True, channel=channel)
            if result_meta is not None:
                result_meta.update({
                    "channel_id": channel["id"], "channel_name": channel["name"],
                    "model": candidate_model,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                })
            return image
    else:
        primary_model = model
        candidates = [(None, model)]
    if not has_channels:
        endpoint_base = os.environ.get("AIGC_ENDPOINT", "").strip().rstrip("/")
        started = time.perf_counter()
        image = _request_edit(
            image_payload, prompt=prompt, model=primary_model, size=size,
            endpoint_base=endpoint_base, api_key=_key(),
            timeout=float(os.environ.get("AIGC_TIMEOUT", "120")))
        if result_meta is not None:
            result_meta.update({"model": primary_model,
                                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        return image

    def attempt(channel: dict, candidate_model: str):
        started = time.perf_counter()
        try:
            image = _request_edit(
                image_payload, prompt=prompt, model=candidate_model, size=size,
                endpoint_base=channel["base_url"], api_key=channel_api_key(channel),
                timeout=float(channel.get("timeout", 120)))
        except AIGCError as exc:
            channels.record_result(channel["id"], False, str(exc),
                                   retryable=exc.retryable, channel=channel)
            return None, None, time.perf_counter(), exc
        except Exception as exc:
            failure = AIGCError("AIGC 请求失败，请检查渠道配置后重试")
            channels.record_result(channel["id"], False, str(failure),
                                   retryable=failure.retryable, channel=channel)
            return None, None, time.perf_counter(), failure
        channels.record_result(channel["id"], True, channel=channel)
        metadata = {
            "channel_id": channel["id"], "channel_name": channel["name"],
            "model": candidate_model,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return image, metadata, time.perf_counter(), None

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(candidates), thread_name_prefix="aigc-race")
    futures = {executor.submit(attempt, channel, candidate_model): channel
               for channel, candidate_model in candidates}
    errors = []
    try:
        while futures:
            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED)
            outcomes = sorted((future.result() for future in done), key=lambda item: item[2])
            for outcome in outcomes:
                image, metadata, _finished_at, error = outcome
                if error is not None:
                    errors.append(error)
                    continue
                if image is None:
                    continue
                if result_meta is not None:
                    result_meta.update(metadata)
                return image
            for future in done:
                futures.pop(future, None)
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    if errors:
        raise errors[0]
    raise AIGCError("AIGC 请求失败")
