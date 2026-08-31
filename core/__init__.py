"""MemoryScope internals: probes, sampling history and the dashboard API."""

from .collector import MemoryCollector, Settings
from .plugin_registry import PluginEntry, PluginRegistry
from .sampler import AlertEngine, HistoryStore, Sample
from .text_report import format_bytes, render_gc, render_overview, render_plugin_detail
from .tracemalloc_probe import TracemallocProbe
from .web_api import MemoryScopeWebApi

__all__ = [
    "AlertEngine",
    "HistoryStore",
    "MemoryCollector",
    "MemoryScopeWebApi",
    "PluginEntry",
    "PluginRegistry",
    "Sample",
    "Settings",
    "TracemallocProbe",
    "format_bytes",
    "render_gc",
    "render_overview",
    "render_plugin_detail",
]