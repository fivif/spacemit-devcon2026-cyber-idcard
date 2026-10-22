"""Secure, OpenAI-compatible AIGC channel configuration and health routing."""

from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
AIGC_USER_AGENT = "SpacemiT-DevCon/2026"
# 默认渠道地址：开源版给占位值。部署时指向自己的 OpenAI 兼容渠道 ——
# 用环境变量 BOOTH_AIGC_BASE_URL，或在操作员面板里逐个渠道填写。
# 真实的网关地址与密钥不要提交进仓库。
DEFAULT_BASE_URL = "https://api.example.com/v1"
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# 示例模型预设：只用于操作员面板的「一键填表」模板。
# 型号名请按自己渠道实际提供的名称改写，不同渠道的同名型号版本并不一致。
DEFAULT_MODEL_PRESETS = (
    {"model": "gpt-image-1", "name": "GPT Image 1", "timeout": 240},
    {"model": "gemini-2.5-flash-image", "name": "Gemini 2.5 Flash Image", "timeout": 180},
    {"model": "seedream-4-0-250828", "name": "Seedream 4.0", "timeout": 300},
)


def default_base_url() -> str:
    """默认渠道地址；BOOTH_AIGC_BASE_URL 优先，便于部署时指向自己的网关。"""
    return os.environ.get("BOOTH_AIGC_BASE_URL", "").strip() or DEFAULT_BASE_URL


def model_presets() -> list[dict]:
    """Return public model presets without credentials or saved channel state."""
    base = default_base_url()
    return [dict(item, base_url=item.get("base_url") or base)
            for item in DEFAULT_MODEL_PRESETS]


def channel_api_key(channel: dict) -> str:
    """Resolve a channel secret without exposing it in public status responses."""
    configured = str(channel.get("api_key") or "").strip()
    if configured:
        return configured
    if urlparse(str(channel.get("base_url") or "")).hostname == "ark.cn-beijing.volces.com":
        return os.environ.get("ARK_API_KEY", "").strip()
    return ""


def selected_model(channel: dict) -> str:
    if "model" in channel:
        return str(channel["model"] or "").strip()
    models = channel.get("models", [])
    if isinstance(models, str):
        models = models.replace("\n", ",").split(",")
    if not isinstance(models, list) or not models:
        return ""
    return str(models[0] or "").strip()


class ChannelConfigError(ValueError):
    pass


class AIGCChannels:
    def __init__(self, data_dir: Path | str, *, monitor_interval: float = 20.0):
        self.path = Path(data_dir) / "aigc-channels.json"
        self.monitor_interval = max(5.0, float(monitor_interval))
        self._lock = threading.RLock()
        self._health: dict[str, dict] = {}
        self._generation_cooldown: dict[str, float] = {}
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "channels": []}
        except Exception as exc:
            raise ChannelConfigError("模型渠道配置无法读取") from exc
        if not isinstance(value, dict) or not isinstance(value.get("channels", []), list):
            raise ChannelConfigError("模型渠道配置格式错误")
        value["channels"] = [dict(item, model=selected_model(item))
                             for item in value.get("channels", [])]
        for item in value["channels"]:
            item.pop("models", None)
        return value

    @staticmethod
    def _validate(payload: dict, previous: dict) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("channels"), list):
            raise ChannelConfigError("渠道列表格式错误")
        if len(payload["channels"]) > 20:
            raise ChannelConfigError("最多配置 20 个渠道")
        old = {item["id"]: item for item in previous.get("channels", [])
               if isinstance(item, dict) and item.get("id")}
        channels = []
        seen = set()
        for raw in payload["channels"]:
            if not isinstance(raw, dict):
                raise ChannelConfigError("渠道格式错误")
            channel_id = str(raw.get("id") or "").strip()
            name = str(raw.get("name") or "").strip()[:64]
            base_url = str(raw.get("base_url") or "").strip().rstrip("/")
            parsed = urlparse(base_url)
            if not CHANNEL_ID_RE.fullmatch(channel_id) or channel_id in seen:
                raise ChannelConfigError("渠道编号无效或重复")
            if not name:
                raise ChannelConfigError("渠道名称不能为空")
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ChannelConfigError("请填写不含账号密码、参数或片段的 HTTP(S) 地址")
            try:
                if parsed.port is not None and not 1 <= parsed.port <= 65535:
                    raise ValueError
            except ValueError as exc:
                raise ChannelConfigError("服务地址端口无效") from exc
            if parsed.hostname == "api-inference.modelscope.cn" and not parsed.path.rstrip("/"):
                base_url += "/v1"
            model = selected_model(raw)
            if not MODEL_RE.fullmatch(model):
                raise ChannelConfigError("每个渠道请选择一个有效的模型名称")
            health_url = str(raw.get("health_url") or "").strip()
            if health_url:
                health = urlparse(health_url)
                if (health.scheme not in ("http", "https") or not health.hostname
                        or health.username or health.password or health.fragment):
                    raise ChannelConfigError("健康检查地址须为完整 HTTP(S) 地址")
            old_item = old.get(channel_id, {})
            key = str(raw.get("api_key") or "").strip() or str(old_item.get("api_key") or "")
            if len(key) > 4096:
                raise ChannelConfigError("API Key 长度超出限制")
            channels.append({
                "id": channel_id,
                "name": name,
                "base_url": base_url,
                "health_url": health_url,
                "model": model,
                "api_key": key,
                "enabled": bool(raw.get("enabled", True)),
                "priority": max(1, min(999, int(raw.get("priority", 100)))),
                "timeout": max(5, min(300, int(raw.get("timeout", old_item.get("timeout",
                    300 if parsed.hostname == "api-inference.modelscope.cn" else 120))))),
            })
            seen.add(channel_id)
        return {"version": 1, "channels": channels}

    def save(self, payload: dict) -> dict:
        with self._lock:
            previous = self._load()
            config = self._validate(payload, previous)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".aigc-channels-", dir=str(self.path.parent))
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(config, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.path)
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
            old = {item["id"]: item for item in previous.get("channels", [])}
            current = {item["id"]: item for item in config["channels"]}
            for channel_id in old.keys() | current.keys():
                if old.get(channel_id) != current.get(channel_id):
                    self._health.pop(channel_id, None)
                    self._generation_cooldown.pop(channel_id, None)
            self._ensure_monitor()
            return self.public_status()

    def configs(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._load()["channels"]]

    def get(self, channel_id: str) -> dict:
        """Return one private channel config for server-side use only."""
        wanted = str(channel_id or "").strip()
        for item in self.configs():
            if item["id"] == wanted:
                return item
        raise ChannelConfigError("找不到这个渠道")

    def has_channels(self) -> bool:
        return bool(self.configs())

    def start_monitor(self) -> None:
        if self.has_channels():
            self._ensure_monitor()

    def public_status(self) -> dict:
        with self._lock:
            channels = self._load()["channels"]
            result = []
            now = time.time()
            active_channel_id = None
            secrets = [channel_api_key(item) for item in channels if channel_api_key(item)]
            for item in sorted(channels, key=lambda value: (value["priority"], value["name"].lower())):
                status = dict(self._health.get(item["id"], {}))
                retry_after = self._retry_after(item["id"])
                health = status.get("health", "unknown")
                detail = status.get("detail", "尚未检测")
                if retry_after > now:
                    health = "unavailable"
                    detail = "生成失败，暂时切换渠道" if health != status.get("health") else detail
                elif health == "unavailable":
                    health = "unknown"
                    detail = "冷却已结束，等待重新检测或生成"
                if not item["enabled"]:
                    routing = "disabled"
                elif not channel_api_key(item) or not item["model"]:
                    routing = "unconfigured"
                elif retry_after > now:
                    routing = "cooldown"
                elif active_channel_id is None:
                    routing = "primary"
                    active_channel_id = item["id"]
                else:
                    routing = "standby"
                for secret in secrets:
                    detail = detail.replace(secret, "[已隐藏]")
                result.append({key: item[key] for key in
                               ("id", "name", "base_url", "health_url", "model", "enabled", "priority")}
                              | {"key_configured": bool(channel_api_key(item)),
                                 "models": [item["model"]] if item["model"] else [],
                                 "routing": routing,
                                 "retry_after": retry_after if retry_after > now else None,
                                 "timeout": item.get("timeout", 120),
                                 "health": health,
                                 "checked_at": status.get("checked_at"),
                                 "latency_ms": status.get("latency_ms"),
                                 "detail": detail})
            active = next((item for item in result if item["id"] == active_channel_id), None)
            race_model = active.get("model") if active else None
            for item in result:
                item["race_eligible"] = bool(
                    race_model and item["routing"] in ("primary", "standby")
                    and item.get("model") == race_model)
            return {"ok": True, "channels": result,
                    "model_presets": model_presets(),
                    "active_channel_id": active_channel_id,
                    "race_model": race_model,
                    "race_channel_count": sum(item["race_eligible"] for item in result),
                    "monitor_interval": int(self.monitor_interval)}

    def _probe(self, channel: dict) -> dict:
        base_url = channel["base_url"].rstrip("/")
        parsed = urlparse(base_url)
        if parsed.hostname == "api-inference.modelscope.cn" and not parsed.path.rstrip("/"):
            base_url += "/v1"
        url = channel.get("health_url") or base_url + "/models"
        headers = {"Accept": "application/json", "User-Agent": AIGC_USER_AGENT}
        api_key = channel_api_key(channel)
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(url, headers=headers, method="GET")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=5, context=ssl.create_default_context()) as response:
                response.read(65537)
                status = int(response.status)
            if status < 200 or status >= 300:
                raise OSError("HTTP %d" % status)
            return {"health": "available", "detail": "连接正常", "latency_ms": round((time.perf_counter() - started) * 1000)}
        except urllib.error.HTTPError as exc:
            latency = round((time.perf_counter() - started) * 1000)
            if exc.code in (404, 405):
                return {"health": "unknown", "detail": "模型列表接口不支持，可填写健康检查地址",
                        "latency_ms": latency}
            detail = "鉴权失败" if exc.code in (401, 403) else "服务返回 HTTP %d" % exc.code
            return {"health": "unavailable", "detail": detail, "latency_ms": latency}
        except Exception:
            return {"health": "unavailable", "detail": "连接失败或超时",
                    "latency_ms": round((time.perf_counter() - started) * 1000)}

    def check(self, channel_id: str | None = None) -> dict:
        channels = self.configs()
        selected = [item for item in channels if channel_id is None or item["id"] == channel_id]
        if channel_id and not selected:
            raise ChannelConfigError("找不到这个渠道")
        checked_at = time.time()
        results = [(item, self._probe(item)) for item in selected]
        with self._lock:
            current = {item["id"]: item for item in self.configs()}
            for item, result in results:
                if current.get(item["id"]) != item:
                    continue
                result["checked_at"] = checked_at
                if result["health"] == "unavailable":
                    result["retry_after"] = checked_at + self.monitor_interval * 2
                self._health[item["id"]] = result
        return self.public_status()

    def record_result(self, channel_id: str, success: bool, detail: str = "",
                      *, retryable: bool = True, channel: dict | None = None) -> None:
        with self._lock:
            current = next((item for item in self.configs() if item["id"] == channel_id), None)
            if current is None or (channel is not None and current != channel):
                return
            now = time.time()
            previous = self._health.get(channel_id, {})
            if success or not retryable:
                self._generation_cooldown.pop(channel_id, None)
            else:
                self._generation_cooldown[channel_id] = now + 60
            self._health[channel_id] = {
                "health": "available" if success else ("unavailable" if retryable else "unknown"),
                "detail": "连接与生成正常" if success else (detail or "生成失败，暂时切换渠道"),
                "checked_at": now,
                "latency_ms": previous.get("latency_ms"),
                "retry_after": 0 if success or not retryable else now + 60,
            }

    def _retry_after(self, channel_id: str) -> float:
        state = self._health.get(channel_id, {})
        return max(self._generation_cooldown.get(channel_id, 0),
                   state.get("retry_after", 0) if state.get("health") == "unavailable" else 0)

    def candidates(self, preferred_model: str | None = None) -> list[tuple[dict, str]]:
        with self._lock:
            channels = self.configs()
            now = time.time()
            return [(item, item["model"]) for item in sorted(
                channels, key=lambda value: (value["priority"], value["name"].lower()))
                if item["enabled"] and channel_api_key(item) and item["model"]
                and self._retry_after(item["id"]) <= now]

    def race_candidates(self) -> tuple[str, list[tuple[dict, str]]]:
        candidates = self.candidates()
        if not candidates:
            return "", []
        model = candidates[0][1]
        return model, [(channel, model) for channel, selected in candidates
                       if selected == model]

    def _ensure_monitor(self) -> None:
        if self._monitor and self._monitor.is_alive():
            return
        self._stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name="aigc-channel-health", daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        try:
            self.check()
        except Exception:
            pass
        while not self._stop.wait(self.monitor_interval):
            try:
                self.check()
            except Exception:
                pass

