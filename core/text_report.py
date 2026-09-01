"""Human readable renderers for the chat commands."""

from __future__ import annotations

from typing import Any

_UNITS = ("B", "KB", "MB", "GB", "TB")

NOTE_TEXT = {
    "import_hook_not_installed": "⚠ 导入成本钩子未安装，无法给出各插件的加载成本",
    "import_hook_degraded": "⚠ 导入成本钩子已自我降级，数据可能不完整",
    "partial_import_coverage": "ℹ 部分插件在 MemoryScope 之前加载，加载成本未测量（显示为 -）",
    "census_never_run": "ℹ 对象普查尚未运行（/mem census 手动触发）",
    "census_sampled": "ℹ 对象普查为抽样结果，数值已按抽样率放大",
    "census_truncated": "⚠ 对象普查因超时截断，结果偏小",
    "dep_audit_never_run": "ℹ 依赖审计尚未运行（/mem audit 手动触发）",
    "dep_audit_truncated": "⚠ 依赖审计因超时截断，部分插件未扫描",
    "psutil_missing": "⚠ 未安装 psutil，部分进程级数据不可用",
    "rss_reader_unavailable": "⚠ 无法读取进程 RSS，导入成本测量不可用",
    "deep_scan_truncated": "⚠ 引用图深扫因限额截断，结果为下界",
}


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


def format_optional_bytes(value: Any) -> str:
    """``-`` for unknown, so that unmeasured never reads as zero."""

    if value is None:
        return "-"
    return format_bytes(value)


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


def format_ms(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1000:
        return f"{number / 1000.0:.2f} s"
    return f"{number:.0f} ms"


def _notes(report: dict[str, Any]) -> list[str]:
    return [NOTE_TEXT[note] for note in (report.get("notes") or []) if note in NOTE_TEXT]


def render_overview(report: dict[str, Any], top_n: int = 8) -> str:
    process = report.get("process") or {}
    totals = report.get("totals") or {}
    hook = process.get("import_hook") or {}
    lines: list[str] = ["📊 MemoryScope 内存概览"]

    percent = process.get("memory_percent")
    head = f"进程 RSS {format_bytes(process.get('rss_bytes'))}"
    if isinstance(percent, (int, float)):
        head += f" · 占系统 {percent:.1f}%"
    if process.get("threads"):
        head += f" · 线程 {process['threads']}"
    if process.get("modules"):
        head += f" · 模块 {process['modules']}"
    lines.append(head)

    plugin_self = totals.get("import_self_bytes_total") or 0
    packages_bytes = totals.get("packages_bytes") or 0
    if hook.get("installed") or plugin_self or packages_bytes:
        lines.append(
            f"加载成本 插件代码 {format_bytes(plugin_self)}"
            f" + 第三方包 {format_bytes(packages_bytes)}"
            f"（{totals.get('packages_count', 0)} 个）"
            f" = {format_bytes(totals.get('import_total_bytes'))}",
        )
    lines.append(
        f"已测量插件 {totals.get('measured_plugin_count', 0)}"
        f"/{totals.get('plugin_count', 0)}"
        f" · 钩子自身开销 {format_ms(hook.get('overhead_ms'))}",
    )

    if totals.get("lazy_savings_bytes"):
        lines.append(
            f"改惰性导入可省 {format_bytes(totals['lazy_savings_bytes'])}"
            "（/mem audit 看清单）",
        )
    if totals.get("census_bytes"):
        lines.append(
            f"对象普查 插件对象合计 {format_bytes(totals['census_bytes'])}"
            f" · {totals.get('census_objects', 0)} 个对象",
        )

    lines.extend(_notes(report))

    rows = [
        row
        for row in (report.get("plugins") or [])
        if row.get("import_bytes") or row.get("census_bytes")
    ]
    lines.append("")
    if not rows:
        lines.append(
            "暂无归因数据：插件在 MemoryScope 之前就加载完了。"
            "重启 AstrBot 后本插件会先装好钩子，届时即可看到各插件的加载成本。",
        )
        return "\n".join(lines)

    for index, row in enumerate(rows[: max(1, top_n)], start=1):
        label = row.get("display_name") or row.get("name")
        parts = [f"{index}. {label} {format_optional_bytes(row.get('import_bytes'))}"]
        packages = row.get("import_packages") or []
        if packages:
            parts.append(f"依赖 {len(packages)} 包")
        if row.get("import_ms"):
            parts.append(format_ms(row["import_ms"]))
        if row.get("census_bytes"):
            parts.append(f"对象 {format_bytes(row['census_bytes'])}")
        delta = format_delta(row.get("delta_bytes"))
        if delta:
            parts.append(f"Δ{delta}")
        trend = format_trend(row.get("trend_bytes_per_minute"))
        if trend:
            parts.append(f"趋势 {trend}")
        lines.append(" · ".join(parts))

    hidden = len(rows) - max(1, top_n)
    if hidden > 0:
        lines.append(f"…其余 {hidden} 个插件占用较小")
    return "\n".join(lines)


def render_imports(report: dict[str, Any], top_n: int = 12) -> str:
    """The package table: what each third-party dependency cost to import."""

    totals = report.get("totals") or {}
    hook = (report.get("process") or {}).get("import_hook") or {}
    packages = report.get("packages") or []
    lines = ["📦 加载成本（第三方包）"]
    lines.append(
        f"合计 {format_bytes(totals.get('packages_bytes'))}"
        f" / {totals.get('packages_count', 0)} 个包"
        f" · 钩子期间 RSS +{format_bytes(hook.get('rss_growth_bytes'))}",
    )
    lines.extend(_notes(report))
    if not packages:
        lines.append("")
        lines.append("暂无数据：钩子安装时这些包已经导入完成，重启后可见。")
        return "\n".join(lines)
    lines.append("")
    for index, item in enumerate(packages[: max(1, top_n)], start=1):
        parts = [f"{index}. {item.get('name')} {format_bytes(item.get('bytes'))}"]
        if item.get("wall_ms"):
            parts.append(format_ms(item["wall_ms"]))
        if item.get("modules"):
            parts.append(f"{item['modules']} 模块")
        if item.get("first_importer"):
            parts.append(f"首个导入者 {item['first_importer']}")
        lines.append(" · ".join(parts))
    lines.append("")
    lines.append(
        "注：成本只记在第一个导入该包的插件名下，共用它的插件都是搭便车，"
        "所以不要把它读成那个插件的专属占用。",
    )
    return "\n".join(lines)


def render_audit(report: dict[str, Any], top_n: int = 12) -> str:
    """Lazy-import opportunities, sorted by what they would recover."""

    totals = report.get("totals") or {}
    meta = report.get("audit_meta") or {}
    rows = report.get("opportunities") or []
    lines = ["🔍 顶层导入审计（可改惰性导入的重依赖）"]
    if meta:
        lines.append(
            f"扫描 {meta.get('audited', 0)}/{meta.get('plugin_count', 0)} 个插件"
            f" · {format_ms(meta.get('elapsed_ms'))}"
            f" · 命中 {meta.get('finding_count', 0)} 处",
        )
    lines.append(f"若全部改成按需导入，可省 {format_bytes(totals.get('lazy_savings_bytes'))}")
    lines.extend(_notes(report))
    known = [row for row in rows if row.get("cost_bytes")]
    if not known:
        lines.append("")
        lines.append("没有已知成本的重依赖，或本次启动未测到导入成本。")
        return "\n".join(lines)
    lines.append("")
    for index, row in enumerate(known[: max(1, top_n)], start=1):
        plugins = row.get("plugins") or []
        head = f"{index}. {row.get('module')} {format_bytes(row.get('cost_bytes'))}"
        head += f" · {len(plugins)} 个插件在顶层导入"
        lines.append(head)
        preview = ", ".join(plugins[:4])
        if len(plugins) > 4:
            preview += f" 等 {len(plugins)} 个"
        lines.append(f"   {preview}")
    lines.append("")
    lines.append("注：只有列出的插件全部改成函数内导入，这笔内存才真正省下来。")
    return "\n".join(lines)


def render_census(report: dict[str, Any], top_n: int = 8) -> str:
    """Object census: live objects owned by each plugin module."""

    meta = report.get("census_meta") or {}
    lines = ["🧮 对象普查"]
    if not meta:
        lines.append("尚未运行。普查会遍历 GC 堆，进程越大停顿越明显，因此默认关闭。")
        return "\n".join(lines)
    lines.append(
        f"扫描 {meta.get('scanned', 0)}/{meta.get('total_objects', 0)} 个对象"
        f" · 抽样 1/{meta.get('sample_rate', 1)}"
        f" · {format_ms(meta.get('elapsed_ms'))}",
    )
    lines.append(
        f"插件对象合计 {format_bytes(meta.get('plugin_bytes'))}"
        f" · {meta.get('plugin_objects', 0)} 个对象"
        f" · 涉及 {meta.get('plugin_count', 0)} 个插件",
    )
    lines.extend(_notes(report))
    rows = [row for row in (report.get("plugins") or []) if row.get("census_bytes")]
    rows.sort(key=lambda item: item.get("census_bytes") or 0, reverse=True)
    if rows:
        lines.append("")
        for index, row in enumerate(rows[: max(1, top_n)], start=1):
            label = row.get("display_name") or row.get("name")
            parts = [f"{index}. {label} {format_bytes(row.get('census_bytes'))}"]
            parts.append(f"{row.get('census_objects', 0)} 对象")
            types = row.get("census_types") or []
            if types:
                parts.append(f"最大 {types[0].get('type')}")
            lines.append(" · ".join(parts))
    buckets = report.get("census_buckets") or []
    if buckets:
        preview = " / ".join(
            f"{item.get('bucket')} {format_bytes(item.get('bytes'))}"
            for item in buckets[:3]
        )
        lines.append("")
        lines.append(f"非插件部分：{preview}")
    lines.append("")
    lines.append(
        "注：str/bytes/int 等不被 GC 跟踪的对象不在普查范围内，"
        "所以合计必然远小于进程 RSS。",
    )
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

    cost = detail.get("import") or {}
    if cost:
        lines.append(
            f"加载成本 {format_bytes(cost.get('bytes'))}（含依赖）"
            f" · 其中自身代码 {format_bytes(cost.get('self_bytes'))}"
            f" · {format_ms(cost.get('wall_ms'))}"
            f" · {cost.get('modules', 0)} 个模块",
        )
    else:
        lines.append("加载成本未测量：该插件在 MemoryScope 装好钩子之前就加载了。")

    census = detail.get("census") or {}
    if census:
        lines.append(
            f"存活对象 {format_bytes(census.get('bytes'))}"
            f" · {census.get('objects', 0)} 个"
            f" · {census.get('type_count', 0)} 种类型",
        )

    retained = row.get("retained") or {}
    if retained:
        suffix = "（已截断）" if retained.get("truncated") else ""
        lines.append(
            f"引用图保留 独占 {format_bytes(retained.get('exclusive_bytes'))}"
            f" · 共享 {format_bytes(retained.get('shared_bytes'))}"
            f" · 对象 {retained.get('exclusive_objects', 0)}" + suffix,
        )

    delta = format_delta(row.get("delta_bytes"))
    trend = format_trend(row.get("trend_bytes_per_minute"))
    if delta or trend:
        tail = f" · 趋势 {trend}" if trend else ""
        lines.append("相对基线 " + (delta or "≈0") + tail)

    packages = detail.get("import_packages") or []
    if packages:
        lines.append("")
        lines.append("首次由它触发的第三方包：")
        for item in packages[: max(1, limit)]:
            lines.append(
                f"· {item.get('name')} {format_bytes(item.get('bytes'))}"
                f" · {format_ms(item.get('wall_ms'))}",
            )

    audit = detail.get("audit") or {}
    findings = [item for item in (audit.get("imports") or []) if item.get("cost_bytes")]
    if findings:
        lines.append("")
        lines.append(f"顶层重依赖（可省 {format_bytes(audit.get('known_bytes'))}）：")
        for item in findings[: max(1, limit)]:
            note = "，可选依赖" if item.get("guarded") else ""
            lines.append(
                f"· {item.get('module')} {format_bytes(item.get('cost_bytes'))}"
                f"（{item.get('file')}:{item.get('lineno')}{note}）",
            )

    types = (census.get("types") if census else None) or []
    if types:
        lines.append("")
        lines.append("对象类型 Top：")
        for item in types[: max(1, limit)]:
            lines.append(
                f"· {item.get('type')} {format_bytes(item.get('bytes'))}"
                f"（{item.get('objects', 0)} 个）",
            )
    return "\n".join(lines)


def render_gc(result: dict[str, Any]) -> str:
    freed = result.get("freed_bytes") or 0
    tail = (
        f"（RSS 释放 {format_bytes(freed)}）"
        if freed
        else "（RSS 未下降：CPython 把内存还给自己的分配器，通常不还给内核）"
    )
    return (
        "♻ 手动 GC 完成\n"
        f"回收对象 {result.get('collected', 0)} 个"
        f" · 进程 RSS {format_bytes(result.get('rss_before'))}"
        f" → {format_bytes(result.get('rss_after'))}"
        + tail
        + f"\n不可回收对象 {result.get('uncollectable', 0)} 个"
    )
