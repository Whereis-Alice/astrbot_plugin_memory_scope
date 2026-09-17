// Pure view models. Missing measurements stay unknown; startup cost is not ownership.
export const finite = (value) =>
  typeof value === "number" && Number.isFinite(value);
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export function unwrap(value) {
  for (
    let i = 0;
    i < 5 && value && typeof value === "object" && !Array.isArray(value);
    i++
  ) {
    if (value.status === "error" || (value.error && !value.latest))
      throw new Error(value.message || value.error);
    if (
      "data" in value &&
      (value.status === "ok" ||
        value.status === "success" ||
        typeof value.status === "number" ||
        Object.keys(value).length === 1)
    )
      value = value.data;
    else break;
  }
  return value;
}
export function sizeParts(value) {
  if (!finite(value)) return ["—", ""];
  const n = Math.abs(value),
    unit =
      n >= 1073741824 ? "GiB" : n >= 1048576 ? "MiB" : n >= 1024 ? "KiB" : "B";
  const divisor = { GiB: 1073741824, MiB: 1048576, KiB: 1024, B: 1 }[unit];
  return [(value / divisor).toFixed(unit === "B" ? 0 : 2), unit];
}
export const size = (value) => sizeParts(value).filter(Boolean).join(" ");
export const signedSize = (value) =>
  finite(value) ? (value > 0 ? "+" : "") + size(value) : "—";
export function stamp(ts, full = false) {
  return finite(ts)
    ? new Date(ts * 1000).toLocaleString(
        document.documentElement.lang,
        full
          ? {
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
              hour12: false,
            }
          : {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
              hour12: false,
            },
      )
    : "—";
}
export function duration(ms) {
  if (!finite(ms)) return "—";
  return ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : ms.toFixed(1) + " ms";
}
export function pluginRows(overview = {}, report = {}, local = {}) {
  const rows = new Map();
  for (const item of overview.run?.inventory || []) {
    const id = item.root_dir_name || item.import_key || item.name;
    if (id) rows.set(id, { ...item, id, phases: [] });
  }
  for (const item of report.plugins || []) {
    const id = item.plugin;
    if (id) rows.set(id, { ...rows.get(id), ...item, id });
  }
  for (const item of local.plugins || []) {
    const id = item.root_dir_name || item.import_key || item.name;
    const match = [...rows.values()].find(
      (r) => r.id === id || r.name === item.name,
    );
    const key = match?.id || id;
    if (key)
      rows.set(key, {
        ...rows.get(key),
        ...(!match ? item : {}),
        id: key,
        local: item,
      });
  }
  return [...rows.values()].map((row) => {
    const phases = row.phases || [];
    const values = phases.map((p) => p.process_rss_swap_delta).filter(finite);
    return {
      ...row,
      label:
        row.display_name || row.name || row.id.replace(/^astrbot_plugin_/, ""),
      phases,
      delta: values.length
        ? values.reduce((a, b) => a + b, 0)
        : ((!overview.run?.id ? row.local?.import_bytes : null) ?? null),
      partial: values.length > 0 && values.length < phases.length,
      duration: phases.length
        ? phases.reduce((a, p) => a + (p.duration_ms || 0), 0)
        : ((!overview.run?.id ? row.local?.import_ms : null) ?? null),
      packages: (report.packages || []).filter(
        (p) => p.first_importer === row.id,
      ),
      processes: (overview.latest?.processes || []).filter(
        (p) => p.startup_owner === row.id,
      ),
      failed: phases.some((p) => p.failed),
    };
  });
}
export function sortedRows(rows, field = "delta", search = "", filter = "all") {
  const needle = search.toLocaleLowerCase();
  return rows
    .filter((r) => (r.id + " " + r.label).toLocaleLowerCase().includes(needle))
    .filter(
      (r) =>
        filter === "all" ||
        (filter === "on" && r.activated === true) ||
        (filter === "off" && r.activated === false) ||
        (filter === "unmeasured" && !finite(r.delta)),
    )
    .sort((a, b) =>
      field === "name"
        ? a.label.localeCompare(b.label)
        : (finite(b[field]) ? b[field] : -Infinity) -
            (finite(a[field]) ? a[field] : -Infinity) ||
          a.label.localeCompare(b.label),
    );
}
export function trendStats(points, key) {
  const values = points.map((p) => p.memory?.[key]).filter(finite);
  return values.length
    ? {
        min: Math.min(...values),
        max: Math.max(...values),
        change: values.at(-1) - values[0],
        last: values.at(-1),
      }
    : { min: null, max: null, change: null, last: null };
}
