"""Small metadata-only flight recorder. Never traces allocations by default."""

from __future__ import annotations

import asyncio
import functools
import inspect
import re
import time
import uuid
import weakref
from collections import OrderedDict
from contextlib import contextmanager


def identifier(value):
    return re.sub(r"[^\w.:/\- ]", "_", str(value or ""))[:120]


class ActivityRecorder:
    """Fail-open instrumentation with no per-handler networking or RSS reads."""

    def __init__(self, client, *, enabled=True, max_per_minute=300):
        self.client = client
        self.enabled = str(enabled).lower() in {"true", "1"} and bool(client.configured)
        self.started_at = time.time()
        try:
            self.max_per_minute = max(30, min(3000, int(max_per_minute)))
        except (ValueError, TypeError, OverflowError):
            self.max_per_minute = 300
        self.pending = {}
        self.queue = OrderedDict()
        self.keys = {}
        self.patches = []
        self.dropped = 0
        self.overhead_ms = 0.0
        self._window, self._count = time.monotonic(), 0
        self._task = None
        self._closed = False
        self._last_sent = 0.0

    def _enqueue(self, record):
        self.queue[record["id"]] = dict(record)
        if len(self.queue) > 512:
            self.queue.popitem(last=False)
            self.dropped += 1

    def begin(self, category, plugin, operation):
        if not self.enabled or self._closed:
            return None
        start = time.perf_counter()
        now = time.monotonic()
        if now - self._window >= 60:
            self._window, self._count = now, 0
        if self._count >= self.max_per_minute or len(self.pending) >= 256:
            self.dropped += 1
            return None
        self._count += 1
        uid = uuid.uuid4().hex
        record = {
            "id": uid,
            "start": time.time(),
            "end": None,
            "category": category,
            "plugin": identifier(plugin),
            "operation": identifier(operation),
            "status": "running",
        }
        self.pending[uid] = (record, now)
        self._enqueue(record)
        self.overhead_ms += (time.perf_counter() - start) * 1000
        return uid

    def end(self, uid, status="ok", **extra):
        start = time.perf_counter()
        value = self.pending.pop(uid, None)
        if value is None:
            return
        record, mono = value
        record.update(
            end=time.time(),
            duration_ms=max(0, (time.monotonic() - mono) * 1000),
            status=status,
        )
        if "allocations" in extra:
            record["allocations"] = extra["allocations"][:20]
        self._enqueue(record)
        self.overhead_ms += (time.perf_counter() - start) * 1000

    def begin_key(self, key, category, plugin, operation):
        if key in self.keys:
            self.end(self.keys.pop(key), "expired")
        uid = self.begin(category, plugin, operation)
        if uid:
            self.keys[key] = uid

    def end_key(self, key):
        self.end(self.keys.pop(key, None))

    @contextmanager
    def span(self, category, plugin, operation):
        uid = self.begin(category, plugin, operation)
        status = "ok"
        try:
            yield uid
        except BaseException as exc:
            status = (
                "cancelled"
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                else "error"
            )
            raise
        finally:
            self.end(uid, status)

    def wrap(self, function, plugin, operation):
        if inspect.isasyncgenfunction(function):

            @functools.wraps(function)
            async def generator(*args, **kwargs):
                with self.span("handler", plugin, operation):
                    stream = function(*args, **kwargs)
                    try:
                        async for value in stream:
                            yield value
                    finally:
                        await stream.aclose()

            return generator
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def coroutine(*args, **kwargs):
                with self.span("handler", plugin, operation):
                    return await function(*args, **kwargs)

            return coroutine
        return None

    def instrument(self, registry=None, stars=None):
        if not self.enabled or self._closed:
            return
        if registry is None:
            from astrbot.core.star.star import star_map
            from astrbot.core.star.star_handler import star_handlers_registry

            registry, stars = star_handlers_registry, star_map
        entries = list(registry)
        live = {id(item) for item in entries}
        # Drop strong function/instance references after other plugins are unloaded.
        self.patches = [
            p
            for p in self.patches
            if p[0]() is not None and id(p[0]()) in live and p[0]().handler is p[2]
        ]
        patched = {id(ref()): replacement for ref, _, replacement in self.patches}
        for item in entries:
            if patched.get(id(item)) is getattr(item, "handler", None):
                continue
            meta = (stars or {}).get(getattr(item, "handler_module_path", ""))
            if meta is None or not getattr(meta, "activated", False):
                continue
            plugin = getattr(meta, "root_dir_name", None) or getattr(meta, "name", "")
            if plugin == "astrbot_plugin_memory_scope":
                continue
            original = item.handler
            replacement = self.wrap(
                original, plugin, getattr(item, "handler_name", "handler")
            )
            if replacement is not None:
                try:
                    ref = weakref.ref(item)
                except TypeError:
                    continue  # Unsupported SDK metadata must not break bot handlers.
                self.patches.append((ref, original, replacement))
                item.handler = replacement

    def status(self):
        return {
            "enabled": self.enabled and not self._closed,
            "started_at": self.started_at,
            "dropped": self.dropped,
            "handlers": len(self.patches),
            "overhead_ms": round(self.overhead_ms, 3),
        }

    def expire(self):
        now = time.monotonic()
        for uid, (_, start) in list(self.pending.items()):
            if now - start > 1800:
                self.end(uid, "expired")
        self.keys = {k: v for k, v in self.keys.items() if v in self.pending}

    async def flush(self):
        cutoff = time.time() - 86390
        expired = [uid for uid, item in self.queue.items() if item["start"] < cutoff]
        for uid in expired:
            self.queue.pop(uid, None)
        self.dropped += len(expired)
        batch = list(self.queue.values())[:100]
        await self.client.publish_activities(batch, self.status())
        for item in batch:
            if self.queue.get(item["id"]) is item:
                self.queue.pop(item["id"], None)
        self._last_sent = time.monotonic()

    async def _loop(self):
        refresh = 0.0
        while True:
            try:
                self.expire()
                if time.monotonic() - refresh >= 60:
                    refresh = time.monotonic()
                    try:
                        self.instrument()
                    except Exception:  # noqa: BLE001,S110 - isolate optional SDK instrumentation
                        pass  # Still send hook activity and heartbeat on older SDKs.
                if self.queue or time.monotonic() - self._last_sent >= 30:
                    await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001,S110 - no secrets/log spam during backend outages
                # The bounded queue retains metadata during an outage; bot work continues.
                pass
            await asyncio.sleep(5)

    def start(self):
        if self._task is None and self.client.configured and not self._closed:
            self._task = asyncio.create_task(self._loop())

    async def close(self):
        self._closed = True
        for ref, original, replacement in reversed(self.patches):
            item = ref()
            if item is not None and item.handler is replacement:
                item.handler = original
        self.patches.clear()
        for uid in list(self.pending):
            self.end(uid, "cancelled")
        self.keys.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.client.configured:
            try:
                await asyncio.wait_for(self.flush(), timeout=1)
            except Exception:  # noqa: BLE001,S110 - shutdown must not depend on backend availability
                pass
        self.queue.clear()
