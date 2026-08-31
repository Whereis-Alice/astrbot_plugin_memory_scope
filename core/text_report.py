"""Human readable renderers for the chat commands."""

from __future__ import annotations

from typing import Any

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(value: Any, precision: int = 1) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if number < 0 else ""
    number = abs(number)
    index = 0
    while number >= 1024 and index < len(_UNITS) - 1:
        number /= 1024.0
        index += 1
    if index == 0:
        return f"{sign}{int(number)} B"
    return f"{sign}{number:.{precision}f} {_UNITS[index]}"


def format_delta(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(number) < 1024:
        return ""
    arrow = "+" if number > 0 else "-"
    return f"{arrow}{format_bytes(abs(number))}"


def format_trend(value: Any) -> str:
    if value is None:
        return ""
    try:
        per_hour = float(value) * 60.0
    except (TypeError, ValueError):
        return ""
    if abs(per_hour) < 256 * 1024:
        return ""
    return f"{format_bytes(per_hour)}/h"


def render_overview(report: dict[str, Any], top_n: int = 8) -> str:
    process = report.get("process") or {}
    totals = report.get("totals") or {}
    trace = process.get("tracemalloc") or {}
    lines: list[str] = ["📊 MemoryScope 内存占用"]

    rss = process.get("rss_bytes") or 0
    lines.append(
        f"进程 RSS {format_bytes(rss)}"
        + (
            f" · 占系统 {process['memory_percent']:.1f}%"
            if isinstance(process.get("memory_percent"), (int, float))
            else ""
        ),
    )
    if trace.get("tracing"):
        lines.append(
            f"tracemalloc 当前 {format_bytes(trace.get('current_bytes'))}"
            f" · 峰值 {format_bytes(trace.get('peak_bytes'))}"
            f" · 深度 {trace.get('frames')} 帧",
        )
    else:
        lines.append("tracemalloc 未开启，无法归因到插件（/mem trace on 开启）")

    lines.append(
        f"插件归因合计 {format_bytes(totals.get('plugin_bytes'))}"
        f" / 已跟踪 {format_bytes(totals.get('traced_bytes'))}"
        f" · 插件 {totals.get('measured_plugin_count', 0)}/{totals.get('plugin_count', 0)}",
    )

    notes = set(report.get("notes") or [])
    if "tracing_started_late" in notes:
        lines.append("⚠ 追踪在插件加载后才启动，导入期内存未计入（见 README 的 PYTHONTRACEMALLOC）")
    if "psutil_missing" in notes:
        lines.append("⚠ 未安装 psutil，进程级数据不可用")

    rows = [row for row in (report.get("plugins") or []) if row.get("attributed_bytes")]
    lines.append("")
    if not rows:
        if "tracemalloc_off" in notes:
            # Waiting changes nothing while tracing is off; say what to do.
            lines.append("归因数据需要先开启追踪：/mem trace on（此前只记录进程 RSS 趋势）。")
        else:
            lines.append("暂无可归因数据，等待下一次采样。")
        return "\n".join(lines)

    for index, row in enumerate(rows[: max(1, top_n)], start=1):
        label = row.get("display_name") or row.get("name")
        parts = [f"{index}. {label} {format_bytes(row.get('attributed_bytes'))}"]
        delta = format_delta(row.get("delta_bytes"))
        if delta:
            parts.append(f"Δ{delta}")
        trend = format_trend(row.get("trend_bytes_per_minute"))
        if trend:
            parts.append(f"趋势 {trend}")
        retained = row.get("retained") or {}
        if retained.get("exclusive_bytes"):
            parts.append(f"独占 {format_bytes(retained['exclusive_bytes'])}")
        lines.append(" · ".join(parts))

    hidden = len(rows) - max(1, top_n)
    if hidden > 0:
        lines.append(f"…其余 {hidden} 个插件占用较小")

    others = report.get("others") or []
    if others:
        others_total = sum(int(item.get("bytes") or 0) for item in others)
        top_others = " / ".join(
            f"{item['bucket']} {format_bytes(item['bytes'])}" for item in others[:3]
        )
        lines.append("")
        lines.append(f"非插件部分合计 {format_bytes(others_total)}：{top_others}")
    return "\n".join(lines)


def render_plugin_detail(detail: dict[str, Any], limit: int = 8) -> str:
    if not detail or not detail.get("found"):
        return "未找到该插件，请用 /mem top 查看可用名称。"
    row = detail.get("row") or {}
    lines = [f"📦 {row.get('display_name') or detail.get('name')}"]
    version = row.get("version")
    author = row.get("author")
    if version or author:
        lines.append(f"版本 {version or '-'} · 作者 {author or '-'}")
    lines.append(
        f"归因分配 {format_bytes(row.get('attributed_bytes'))}"
        f" · 其中直接分配 {format_bytes(row.get('direct_bytes'))}"
        f" · 内存块 {row.get('blocks', 0)}",
    )
    retained = row.get("retained") or {}
    if retained:
        lines.append(
            f"引用图保留 独占 {format_bytes(retained.get('exclusive_bytes'))}"
            f" · 共享 {format_bytes(retained.get('shared_bytes'))}"
            f" · 对象 {retained.get('exclusive_objects', 0)}"
            + ("（已截断）" if retained.get("truncated") else ""),
        )
    delta = format_delta(row.get("delta_bytes"))
    trend = format_trend(row.get("trend_bytes_per_minute"))
    if delta or trend:
        lines.append(
            "相对基线 " + (delta or "≈0") + (f" · 趋势 {trend}" if trend else ""),
        )

    allocation_lines = detail.get("lines") or []
    if allocation_lines:
        lines.append("")
        lines.append("分配热点：")
        for item in allocation_lines[: max(1, limit)]:
            filename = str(item.get("filename") or "")
            short = filename.replace("\\", "/").rsplit("/", 2)
            display = "/".join(short[-2:]) if len(short) > 1 else filename
            lines.append(
                f"· {display}:{item.get('lineno')}"
                f" {format_bytes(item.get('bytes'))} ({item.get('blocks')} 块)",
            )
    return "\n".join(lines)


def render_gc(result: dict[str, Any]) -> str:
    return (
        "♻ 手动 GC 完成\n"
        f"回收对象 {result.get('collected', 0)} 个"
        f" · 已跟踪内存 {format_bytes(result.get('traced_before'))}"
        f" → {format_bytes(result.get('traced_after'))}"
        f"（释放 {format_bytes(result.get('freed_bytes'))}）\n"
        f"不可回收对象 {result.get('uncollectable', 0)} 个"
    )