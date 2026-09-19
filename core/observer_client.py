"""Lightweight asynchronous bridge to the independent observer."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from urllib import error, parse
from urllib import request as http


class NoRedirect(http.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ObserverClient:
    def __init__(self, config):
        self.url = str(config.get("observer_url", "")).rstrip("/")
        self.token = str(config.get("observer_token", ""))
        path = os.environ.get("MEMORYSCOPE_OBSERVER_CONFIG") or config.get(
            "observer_config_path", ""
        )
        self.config_error = None
        if not self.url and path:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                host = data.get("host", "127.0.0.1")
                self.url = f"http://{'[' + host + ']' if ':' in host else host}:{int(data.get('port', 8766))}"
                self.token = data.get("token", "")
            except (OSError, ValueError, TypeError):
                self.config_error = "无法读取后端连接配置"
        self.configured = bool(self.url and self.token)
        self.enabled = self.configured or bool(path) or bool(self.url)
        self.cache = {}
        self.last_error = None
        self._lock = asyncio.Lock()

    def _request(self, endpoint, params=None, body=None):
        if not self.configured:
            raise ValueError(
                self.config_error
                or "请配置独立后端地址和凭据，或通过观测启动入口运行 AstrBot"
            )
        parsed = parse.urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("后端地址必须是 http(s) 地址，不包含凭据或查询参数")
        url = self.url + "/api/" + endpoint
        if params:
            url += "?" + parse.urlencode(params)
        data = (
            json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        )
        req = http.Request(
            url,
            data=data,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        # No implicit environment proxies; never forward a bearer token on redirect.
        opener = http.build_opener(http.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=3) as response:
                raw = response.read(12 * 1024 * 1024 + 1)
                if len(raw) > 12 * 1024 * 1024:
                    raise ValueError("后端响应超过限制")
                payload = json.loads(raw)
        except error.HTTPError as exc:
            if exc.code == 401:
                raise ValueError("后端凭据无效") from None
            if exc.code == 400:
                try:
                    message = json.loads(exc.read(4096)).get("error", "请求参数无效")
                except ValueError:
                    message = "请求参数无效"
                raise ValueError(message) from None
            raise ValueError(f"后端 HTTP {exc.code}") from None
        except (OSError, error.URLError):
            raise ValueError("独立后端不可达") from None
        if payload.get("status") != "ok":
            raise ValueError(payload.get("error", "后端响应格式无效"))
        return payload["data"]

    async def get(self, endpoint, params=None, *, cache_seconds=3):
        key = endpoint + json.dumps(params or {}, sort_keys=True)
        async with self._lock:
            entry = self.cache.get(key)
            if entry and time.monotonic() - entry[0] < cache_seconds:
                return entry[1]
            try:
                result = await asyncio.to_thread(self._request, endpoint, params)
                self.last_error = None
                self.cache[key] = (time.monotonic(), result)
                if len(self.cache) > 32:
                    self.cache.pop(next(iter(self.cache)))
                return result
            except ValueError as exc:
                self.last_error = str(exc)
                # Never turn old data into a fresh-looking report.
                if endpoint == "overview" and entry:
                    return dict(
                        entry[1],
                        stale=True,
                        bridge_error=str(exc),
                        bridge_cache_age_seconds=time.monotonic() - entry[0],
                    )
                raise

    async def post(self, endpoint, body):
        result = await asyncio.to_thread(self._request, endpoint, None, body)
        self.cache.clear()
        return result

    async def publish_inventory(self, context, *, loaded=False):
        if not self.configured:
            return
        try:
            raw = Path("/proc/self/stat").read_text()
            ticks = int(raw[raw.rindex(")") + 2 :].split()[19])
        except (OSError, IndexError, ValueError):
            return
        plugins = []
        # Only the small metadata list is needed here. Avoid the registry's
        # sys.modules / filesystem walk on the bot's event loop every minute.
        for entry in context.get_all_stars() or []:
            if not (
                getattr(entry, "root_dir_name", None) or getattr(entry, "name", None)
            ):
                continue
            plugins.append(
                {
                    "name": str(getattr(entry, "name", "") or ""),
                    "display_name": str(getattr(entry, "display_name", "") or ""),
                    "version": str(getattr(entry, "version", "") or ""),
                    "root_dir_name": str(getattr(entry, "root_dir_name", "") or ""),
                    "activated": bool(getattr(entry, "activated", True)),
                    "reserved": bool(getattr(entry, "reserved", False)),
                }
            )
        await self.post(
            "inventory",
            {
                "pid": os.getpid(),
                "start_ticks": ticks,
                "ts": time.time(),
                "plugins": plugins,
                "astrbot_loaded": loaded,
            },
        )

    async def publish_activities(self, records, recorder):
        raw = await asyncio.to_thread(Path("/proc/self/stat").read_text)
        ticks = int(raw[raw.rindex(")") + 2 :].split()[19])
        result = await self.post(
            "activities",
            {
                "pid": os.getpid(),
                "start_ticks": ticks,
                "ts": time.time(),
                "records": records,
                "recorder": recorder,
            },
        )
        if not result.get("accepted"):
            raise ValueError("Activity batch was rejected")

    async def publish_diagnostics(self, snapshot):
        if not self.configured or not isinstance(snapshot, dict):
            return
        try:
            raw = Path("/proc/self/stat").read_text()
            ticks = int(raw[raw.rindex(")") + 2 :].split()[19])
        except (OSError, IndexError, ValueError):
            return
        await self.post(
            "diagnostics",
            {
                "pid": os.getpid(),
                "start_ticks": ticks,
                "ts": time.time(),
                "snapshot": snapshot,
            },
        )


def render_observer(data: dict) -> str:
    def size(value):
        return "未采集" if value is None else f"{value / 1048576:.1f} MiB"

    latest = data.get("latest") or {}
    memory = latest.get("memory") or {}
    own = data.get("observer") or {}
    return "\n".join(
        [
            "MemoryScope 独立观察" + (" · 数据已过期" if data.get("stale") else ""),
            f"状态：{latest.get('state', '未知')} · 数据年龄：{data.get('age_seconds', 0) or 0:.1f}s",
            f"服务物理记账：{size(memory.get('current'))}",
            f"匿名内存：{size(memory.get('anon'))} · Swap：{size(memory.get('swap'))}",
            f"匿名内存＋Swap：{size(memory.get('anon_swap'))}（不是物理内存总量）",
            f"服务进程数：{len(latest.get('processes', []))} · 采集器自身：{size(own.get('rss'))}",
            f"启动批次：{(data.get('run') or {}).get('id', '未采集')}",
        ]
    )
