"""Explicit, time-limited Python allocation tracing. Never enabled at startup."""

from __future__ import annotations

import asyncio
import threading
import time


class AllocationTrace:
    def __init__(self, recorder):
        self.recorder = recorder
        self.task = None
        self.lock = threading.Lock()
        self.owned = False
        self.result = {"state": "idle"}

    def _start(self):
        import tracemalloc

        with self.lock:
            if tracemalloc.is_tracing():
                raise ValueError("已有其他工具开启 tracemalloc，本插件不会接管或关闭它")
            tracemalloc.start(1)
            self.owned = True
            return tracemalloc.take_snapshot()

    def _finish(self, baseline, collect):
        import tracemalloc

        with self.lock:
            if not self.owned:
                return []
            try:
                if not collect or not tracemalloc.is_tracing():
                    return []
                snapshot = tracemalloc.take_snapshot()
                # Stop tracing before allocating the diff report itself.
                tracemalloc.stop()
                self.owned = False
                findings = snapshot.compare_to(baseline, "lineno")
                items = []
                for row in findings:
                    if row.size_diff <= 0:
                        continue
                    frame = row.traceback[0]
                    path = frame.filename.replace("\\", "/")
                    parts = path.split("/")
                    start = next(
                        (
                            i
                            for i, part in enumerate(parts)
                            if part.startswith("astrbot_plugin_")
                        ),
                        None,
                    )
                    path = (
                        "/".join(parts[start:])
                        if start is not None
                        else "/".join(parts[-2:])
                    )
                    items.append(
                        {
                            "file": path[:180],
                            "line": frame.lineno,
                            "bytes": row.size_diff,
                            "count": row.count_diff,
                        }
                    )
                    if len(items) == 20:
                        break
                return items
            finally:
                if self.owned and tracemalloc.is_tracing():
                    tracemalloc.stop()
                self.owned = False

    def start(self, seconds=30):
        if not self.recorder.enabled:
            raise ValueError("请先连接后端并开启活动记录")
        if self.task and not self.task.done():
            raise ValueError("追踪已在运行")
        seconds = int(seconds)
        if not 10 <= seconds <= 60:
            raise ValueError("追踪时间必须为 10～60 秒")
        self.result = {
            "state": "starting",
            "started_at": time.time(),
            "seconds": seconds,
        }
        self.task = asyncio.create_task(self._run(seconds))
        return dict(self.result)

    async def _run(self, seconds):
        import tracemalloc

        baseline = None
        uid = self.recorder.begin(
            "trace", "astrbot_plugin_memory_scope", "python_allocations"
        )
        status = "error"
        try:
            if uid is None:
                raise ValueError("Activity recorder is busy")
            # _start is short and atomic: a cancellation cannot orphan a starting worker.
            baseline = self._start()
            self.result["state"] = "running"
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                if tracemalloc.get_tracemalloc_memory() > 16 * 1024 * 1024:
                    self.result.update(state="limited", reason="tracer_memory_limit")
                    break
            limited = self.result["state"] == "limited"
            findings = await asyncio.to_thread(self._finish, baseline, not limited)
            self.result.update(
                state="limited" if limited else "complete", allocations=findings
            )
            status = "expired" if limited else "ok"
        except asyncio.CancelledError:
            self.result["state"] = "cancelled"
            status = "cancelled"
            raise
        except Exception:  # noqa: BLE001 - tracing must not break bot operation
            # Only a fixed reason; never publish exception data or source contents.
            self.result.update(state="error", reason="tracer_unavailable")
        finally:
            self._finish(baseline, False)
            self.result["ended_at"] = time.time()
            self.recorder.end(
                uid, status, allocations=self.result.get("allocations", [])
            )

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self.result.get("state") == "starting":
                self.result.update(state="cancelled", ended_at=time.time())

    def register(self, context, plugin_id):
        from .web_api import _body, error_response, ok

        async def get_status():
            return ok(dict(self.result))

        async def post_trace():
            try:
                body = await _body()
                if body.get("action") == "cancel":
                    await self.close()
                    return ok(dict(self.result))
                if body.get("action") not in (None, "start"):
                    raise ValueError("Invalid action")
                return ok(self.start(body.get("seconds", 30)))
            except (ValueError, TypeError):
                return error_response(
                    "追踪不可用：检查是否已运行、是否开启活动记录，以及时长是否为 10～60 秒",
                    status_code=400,
                )

        if callable(getattr(context, "register_web_api", None)):
            context.register_web_api(
                f"/{plugin_id}/trace_status", get_status, ["GET"], "限时分配追踪状态"
            )
            context.register_web_api(
                f"/{plugin_id}/trace", post_trace, ["POST"], "手动限时分配追踪"
            )
