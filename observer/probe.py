"""Early, bounded startup events. No allocation tracing or persistent import hook."""

from __future__ import annotations

import contextvars
import functools
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .proc import identity


class EventSink:
    def __init__(self, port: int, token: str, max_events=20000):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.destination = ("127.0.0.1", port)
        self.token, self.max_events = token, max_events
        self.sequence = self.dropped = 0
        self.overhead_ns = 0
        self.closed = False
        self.pid = os.getpid()
        self.start_ticks = self._start_ticks()

    @staticmethod
    def _start_ticks() -> int:
        try:
            raw = Path("/proc/self/stat").read_text()
            return int(raw[raw.rindex(")") + 2 :].split()[19])
        except (OSError, ValueError, IndexError):
            return 0

    @staticmethod
    def counters() -> dict:
        result = {}
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith(("VmRSS:", "VmSwap:")):
                    key, value, *_ = line.split()
                    result[key[:-1]] = int(value) * 1024
        except (OSError, ValueError):
            pass
        return result

    def emit(self, kind: str, **data) -> int:
        if self.closed or os.getpid() != self.pid:
            return self.sequence
        start = time.perf_counter_ns()
        self.sequence += 1
        event = dict(
            kind=kind,
            seq=self.sequence,
            pid=self.pid,
            start_ticks=self.start_ticks,
            ts=time.time(),
            monotonic_ns=time.monotonic_ns(),
            dropped=self.dropped,
            overhead_ms=self.overhead_ns / 1e6,
            **data,
        )
        try:
            if self.sequence > self.max_events:
                raise ValueError("Event budget exhausted")
            wire = json.dumps(
                {"token": self.token, "event": event},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            if len(wire) > 60000:
                raise ValueError("Event too large")
            self.sock.sendto(wire, self.destination)
        except (OSError, ValueError):
            self.dropped += 1
        self.overhead_ns += time.perf_counter_ns() - start
        return self.sequence

    def begin(self, plugin: str, phase: str) -> int:
        start = time.perf_counter_ns()
        counters = self.counters()
        self.overhead_ns += time.perf_counter_ns() - start
        return self.emit("phase_start", plugin=plugin, phase=phase, counters=counters)

    def end(self, plugin: str, phase: str, span: int, failed=False, **extra):
        start = time.perf_counter_ns()
        counters = self.counters()
        self.overhead_ns += time.perf_counter_ns() - start
        self.emit(
            "phase_end",
            plugin=plugin,
            phase=phase,
            span=span,
            failed=failed,
            counters=counters,
            **extra,
        )

    def close(self):
        if not self.closed:
            self.closed = True
            self.sock.close()


class StartupProbe:
    """Adapt the loader only after AstrBot itself imports it, preserving order."""

    MODULE = "astrbot.core.star.star_manager"

    def __init__(self, sink: EventSink, plan: dict | None = None):
        self.sink, self.plan = sink, plan or {}
        self.restores = []
        self.finder = None
        self.finished = False
        self.skip_applied = False
        self.active_plugin = contextvars.ContextVar(
            "memoryscope_startup_plugin", default=None
        )

    def replace(self, owner, name, replacement):
        original = getattr(owner, name)
        own = name in vars(owner)
        descriptor = vars(owner).get(name)
        self.restores.append((owner, name, original, replacement, own, descriptor))
        setattr(owner, name, replacement)

    def install(self):
        probe = self
        original_popen = subprocess.Popen.__init__

        @functools.wraps(original_popen)
        def popen(instance, *args, **kwargs):
            original_popen(instance, *args, **kwargs)
            plugin = probe.active_plugin.get()
            if plugin and not probe.finished:
                ident = identity(instance.pid)
                probe.sink.emit(
                    "child_spawn",
                    plugin=plugin,
                    child_pid=instance.pid,
                    child_start_ticks=ident[1] if ident else None,
                    attribution="startup_execution_context",
                )

        self.replace(subprocess.Popen, "__init__", popen)

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname != probe.MODULE:
                    return None
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec is None or spec.loader is None:
                    return None
                original = spec.loader

                class Loader(importlib.abc.Loader):
                    def create_module(self, s):
                        creator = getattr(original, "create_module", None)
                        return creator(s) if creator else None

                    def exec_module(self, module):
                        original.exec_module(module)
                        try:
                            probe.patch_manager(module)
                        except Exception as exc:  # noqa: BLE001 - isolate diagnostics/control failures
                            probe.sink.emit("probe_error", error=type(exc).__name__)
                            probe.finish()

                spec.loader = Loader()
                return spec

        self.finder = Finder()
        sys.meta_path.insert(0, self.finder)
        self.sink.emit("probe_installed")

    def patch_manager(self, module):
        if self.finished:
            return
        manager = module.PluginManager
        required = (
            "_get_plugin_modules",
            "_import_plugin_with_dependency_recovery",
            "reload",
        )
        if not all(callable(getattr(manager, name, None)) for name in required):
            raise RuntimeError("Unsupported AstrBot plugin loader")
        probe = self
        old_modules = manager._get_plugin_modules

        @functools.wraps(old_modules)
        def modules(instance, *args, **kwargs):
            result = old_modules(instance, *args, **kwargs)
            target = probe.plan.get("plugin")
            if probe.plan.get("mode") != "skip" or not target:
                return result
            if not isinstance(result, list) or any(
                not isinstance(x, dict) or "pname" not in x for x in result
            ):
                raise RuntimeError("Unsupported plugin catalog for skip experiment")
            selected = [
                x for x in result if x["pname"] == target and not x.get("reserved")
            ]
            if len(selected) != 1:
                raise RuntimeError("Experiment plugin missing or reserved")
            probe.skip_applied = True
            probe.sink.emit("plugin_skipped", plugin=target)
            return [x for x in result if x not in selected]

        self.replace(manager, "_get_plugin_modules", modules)
        old_import = manager._import_plugin_with_dependency_recovery

        @functools.wraps(old_import)
        async def importer(instance, *args, **kwargs):
            plugin = kwargs.get("root_dir_name", "unknown")
            context_token = probe.active_plugin.set(plugin)
            span = probe.sink.begin(plugin, "import")
            t0 = time.perf_counter_ns()
            before = set(sys.modules)
            probe.sink.overhead_ns += time.perf_counter_ns() - t0
            failed = True
            try:
                imported = await old_import(instance, *args, **kwargs)
                failed = False
                try:
                    probe.patch_classes(imported, plugin)
                except Exception as exc:  # noqa: BLE001 - isolate diagnostics/control failures
                    probe.sink.emit(
                        "probe_error", plugin=plugin, error=type(exc).__name__
                    )
                return imported
            finally:
                t0 = time.perf_counter_ns()
                packages = sorted(
                    {
                        name.split(".")[0]
                        for name in set(sys.modules) - before
                        if not name.startswith(("data.", "astrbot."))
                    }
                )[:128]
                probe.sink.overhead_ns += time.perf_counter_ns() - t0
                probe.sink.end(plugin, "import", span, failed, new_packages=packages)
                probe.active_plugin.reset(context_token)

        self.replace(manager, "_import_plugin_with_dependency_recovery", importer)
        old_reload = manager.reload

        @functools.wraps(old_reload)
        async def reload(instance, *args, **kwargs):
            old_get = None
            replacement = None
            # This changes only the launch's in-memory read; no DB/config writes.
            if probe.plan.get("mode") == "disable" and probe.plan.get("plugin"):
                old_get = module.sp.global_get

                async def read(key, *a, **kw):
                    value = await old_get(key, *a, **kw)
                    if key == "inactivated_plugins":
                        prefix = "data.plugins." + probe.plan["plugin"]
                        values = list(value or [])
                        for plugin in old_modules(instance) or []:
                            if plugin.get("pname") == probe.plan[
                                "plugin"
                            ] and not plugin.get("reserved"):
                                # AstrBot builds exactly this path from pname/module.
                                entry = prefix + "." + plugin["module"]
                                if entry not in values:
                                    values.append(entry)
                                probe.skip_applied = True
                        probe.sink.emit(
                            "plugin_disabled",
                            plugin=probe.plan["plugin"],
                            applied=probe.skip_applied,
                        )
                        return values
                    return value

                replacement = read
                module.sp.global_get = replacement
            try:
                return await old_reload(instance, *args, **kwargs)
            finally:
                if replacement is not None and module.sp.global_get is replacement:
                    module.sp.global_get = old_get
                probe.finish()

        self.replace(manager, "reload", reload)
        self.sink.emit("loader_attached", plan_mode=self.plan.get("mode", "normal"))

    def patch_classes(self, imported, plugin):
        seen = set()
        for value in tuple(vars(imported).values()):
            if not isinstance(value, type) or value.__module__ != imported.__name__:
                continue
            if value in seen:
                continue
            seen.add(value)
            if not any(base.__name__ == "Star" for base in value.__mro__[1:]):
                continue
            probe = self
            original_init = value.__init__

            def make_init(original, probe=probe):
                @functools.wraps(original)
                def init(instance, *args, **kwargs):
                    context_token = probe.active_plugin.set(plugin)
                    span = probe.sink.begin(plugin, "construct")
                    failed = True
                    try:
                        result = original(instance, *args, **kwargs)
                        failed = False
                        return result
                    finally:
                        probe.sink.end(plugin, "construct", span, failed)
                        probe.active_plugin.reset(context_token)

                return init

            self.replace(value, "__init__", make_init(original_init))
            original_async = getattr(value, "initialize", None)
            if inspect.iscoroutinefunction(original_async):

                def make_async(original, probe=probe):
                    @functools.wraps(original)
                    async def initialize(instance, *args, **kwargs):
                        context_token = probe.active_plugin.set(plugin)
                        span = probe.sink.begin(plugin, "initialize")
                        failed = True
                        try:
                            result = await original(instance, *args, **kwargs)
                            failed = False
                            return result
                        finally:
                            probe.sink.end(plugin, "initialize", span, failed)
                            probe.active_plugin.reset(context_token)

                    return initialize

                self.replace(value, "initialize", make_async(original_async))

    def finish(self):
        if self.finished:
            return
        self.finished = True
        for owner, name, original, replacement, own, descriptor in reversed(
            self.restores
        ):
            if getattr(owner, name) is replacement:
                if own:
                    setattr(owner, name, descriptor)
                else:
                    delattr(owner, name)
        self.restores.clear()
        if self.finder in sys.meta_path:
            sys.meta_path.remove(self.finder)
        self.sink.emit("probe_finished", plan_applied=self.skip_applied)
        self.sink.close()
