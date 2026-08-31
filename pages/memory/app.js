// MemoryScope dashboard page (v1.0.2)
// Talks to core/web_api.py through the dashboard plugin bridge.
// Layout rule: every flex/grid child sets min-width:0 in style.css, so long
// plugin names and file paths truncate instead of stretching the page.

const bridge = window.AstrBotPluginPage;

const STORE_SKIN = "memoryscope.skin";
const STORE_AUTO = "memoryscope.auto";
const STORE_TAB = "memoryscope.tab";
const STORE_SORT = "memoryscope.sort";

const TABS = ["overview", "plugins", "alerts", "help"];
const SKINS = [
  { id: "auto", label: "跟随 Dashboard" },
  { id: "dark", label: "深色" },
  { id: "light", label: "浅色" },
];
const SKIN_IDS = SKINS.map((item) => item.id);
const AUTO_INTERVALS = [0, 10, 30, 60, 300];

const HISTORY_POINTS = 60;
const TOP_CHART_ROWS = 10;
const DETAIL_LINES = 25;
const ALERT_LIMIT = 30;
const KB = 1024;

// Columns of the plugin table.  "sortable:false" marks the sparkline column.
const COLUMNS = [
  { key: "name", i18n: "col.plugin", fallback: "插件" },
  { key: "attributed", i18n: "col.attributed", fallback: "归因分配" },
  { key: "direct", i18n: "col.direct", fallback: "直接分配" },
  { key: "exclusive", i18n: "col.exclusive", fallback: "独占保留" },
  { key: "shared", i18n: "col.shared", fallback: "共享保留" },
  { key: "objects", i18n: "col.objects", fallback: "对象数" },
  { key: "delta", i18n: "col.delta", fallback: "Δ 基线" },
  { key: "trend", i18n: "col.trend", fallback: "增长趋势" },
  { key: "share", i18n: "col.share", fallback: "占比" },
  { key: "chart", i18n: "col.chart", fallback: "近期曲线", sortable: false },
];
const SORT_KEYS = COLUMNS.filter((col) => col.sortable !== false).map((c) => c.key);

// The help texts are written as "Label: explanation" in every locale, so the
// definition list is derived from the sentence itself instead of duplicating
// the labels in the i18n files.  "env" has no label, hence the fallbacks.
const HELP_KEYS = [
  "attributed",
  "direct",
  "exclusive",
  "shared",
  "trend",
  "limits",
  "env",
  "cost",
];
const HELP_FALLBACK = {
  attributed: ["col.attributed", "归因分配"],
  direct: ["col.direct", "直接分配"],
  exclusive: ["col.exclusive", "独占保留"],
  shared: ["col.shared", "共享保留"],
  trend: ["col.trend", "增长趋势"],
  limits: ["help.title", "局限"],
  env: ["help.title", "PYTHONTRACEMALLOC"],
  cost: ["help.title", "开销"],
};

const NOTE_FALLBACK = {
  tracemalloc_off: "tracemalloc 未开启，只记录进程 RSS 趋势；需要按插件归因请点上方「开启追踪」。",
  tracing_started_late: "追踪在插件加载后启动，插件导入期内存未计入，数值偏小。",
  no_snapshot: "本次未取得内存快照。",
  psutil_missing: "未安装 psutil，进程级指标不可用。",
  deep_scan_truncated: "深度扫描被截断，独占/共享保留量偏小。",
};

const state = {
  tab: "overview",
  skin: "auto",
  auto: 0,
  autoTimer: null,
  report: null,
  overview: null,
  seriesByPlugin: {},
  alerts: [],
  alertsEnabled: false,
  search: "",
  sortKey: "attributed",
  sortDir: "desc",
  busy: false,
  drawerName: null,
  drawerPayload: null,
  toastTimer: null,
};

const el = (id) => document.getElementById(id);

// -- helpers ---------------------------------------------------------------

function t(key, fallback) {
  try {
    const value = bridge.t("pages.memory." + key, fallback);
    return value === undefined || value === null || value === "" ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function fmtBytes(value, dash) {
  const parsed = num(value);
  if (parsed === null) return dash === undefined ? "-" : dash;
  const sign = parsed < 0 ? "-" : "";
  let size = Math.abs(parsed);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const text = unit === 0 ? String(Math.round(size)) : size.toFixed(size >= 100 ? 0 : 1);
  return sign + text + " " + units[unit];
}

function fmtSigned(value) {
  const parsed = num(value);
  if (parsed === null) return "-";
  if (Math.abs(parsed) < KB) return "≈0";
  return (parsed > 0 ? "+" : "-") + fmtBytes(Math.abs(parsed));
}

function fmtPerHour(bytesPerMinute) {
  const parsed = num(bytesPerMinute);
  if (parsed === null) return "-";
  const perHour = parsed * 60;
  if (Math.abs(perHour) < 64 * KB) return "≈0";
  return (perHour > 0 ? "+" : "-") + fmtBytes(Math.abs(perHour)) + t("unit.perHour", "/小时");
}

function fmtPercent(value, digits) {
  const parsed = num(value);
  if (parsed === null) return "-";
  return parsed.toFixed(digits === undefined ? 2 : digits) + "%";
}

function fmtCount(value) {
  const parsed = num(value);
  if (parsed === null) return "-";
  return parsed.toLocaleString();
}

function fmtUptime(seconds) {
  const parsed = num(seconds);
  if (parsed === null || parsed < 0) return "-";
  const total = Math.floor(parsed);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const pad = (input) => (input < 10 ? "0" + input : String(input));
  const clock = pad(hours) + ":" + pad(minutes) + ":" + pad(total % 60);
  return days > 0 ? days + "d " + clock : clock;
}

function fmtTime(epochSeconds) {
  const parsed = num(epochSeconds);
  if (parsed === null || parsed <= 0) return "-";
  try {
    return new Date(parsed * 1000).toLocaleString();
  } catch (err) {
    return "-";
  }
}

function truncate(text, max) {
  const value = String(text === undefined || text === null ? "" : text);
  return value.length <= max ? value : value.slice(0, Math.max(1, max - 1)) + "…";
}

function shortPath(filename) {
  const parts = String(filename || "").replace(/\\/g, "/").split("/");
  return parts.length > 2 ? parts.slice(-2).join("/") : parts.join("/");
}

function readStore(key, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null || value === "" ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function writeStore(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (err) {
    /* private mode or storage disabled: the preference is simply not kept */
  }
}

function toast(message, kind) {
  const node = el("toast");
  if (!node) return;
  node.textContent = String(message);
  node.dataset.kind = kind === "error" ? "error" : "ok";
  node.hidden = false;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(
    () => {
      node.hidden = true;
    },
    kind === "error" ? 6000 : 2600,
  );
}

function unwrap(res) {
  if (res && typeof res === "object" && "data" in res) {
    if (res.status && res.status !== "ok") {
      throw new Error(res.message || t("error.request", "请求失败"));
    }
    return res.data === undefined || res.data === null ? res : res.data;
  }
  return res;
}

function errorText(err) {
  if (!err) return t("error.request", "请求失败");
  if (typeof err === "string") return err;
  return err.message || String(err);
}

async function apiGet(endpoint, params) {
  return unwrap(await bridge.apiGet(endpoint, params || {}));
}

async function apiPost(endpoint, body) {
  return unwrap(await bridge.apiPost(endpoint, body || {}));
}

function setText(id, text) {
  const node = el(id);
  if (node) node.textContent = text;
}

// -- skin ------------------------------------------------------------------

function isDarkContext() {
  try {
    const context = bridge.getContext ? bridge.getContext() : null;
    if (context && typeof context.isDark === "boolean") return context.isDark;
  } catch (err) {
    /* fall through to the media query */
  }
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch (err) {
    return true;
  }
}

function applySkin() {
  const effective = state.skin === "auto" ? (isDarkContext() ? "dark" : "light") : state.skin;
  document.documentElement.dataset.skin = effective;
  const select = el("skin-select");
  if (select && select.value !== state.skin) select.value = state.skin;
}

function buildSkinOptions() {
  const select = el("skin-select");
  if (!select) return;
  select.innerHTML = SKINS.map(
    (item) =>
      "<option value=\"" +
      esc(item.id) +
      "\">" +
      esc(t("skin." + item.id, item.label)) +
      "</option>",
  ).join("");
  select.value = state.skin;
}

function buildAutoOptions() {
  const select = el("auto-select");
  if (!select) return;
  select.innerHTML = AUTO_INTERVALS.map((seconds) => {
    const label = seconds === 0 ? t("auto.off", "关闭") : seconds + "s";
    return "<option value=\"" + seconds + "\">" + esc(label) + "</option>";
  }).join("");
  select.value = String(state.auto);
}

// -- tabs ------------------------------------------------------------------

function setTab(name) {
  const tab = TABS.indexOf(name) === -1 ? "overview" : name;
  state.tab = tab;
  writeStore(STORE_TAB, tab);
  for (const button of document.querySelectorAll(".ms-tab")) {
    const active = button.dataset.tab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".ms-panel")) {
    const active = panel.id === "panel-" + tab;
    panel.classList.toggle("is-active", active);
  }
}

// -- static text -----------------------------------------------------------

function applyStaticText() {
  for (const node of document.querySelectorAll("[data-i18n]")) {
    if (node.dataset.i18nDefault === undefined) {
      node.dataset.i18nDefault = node.textContent || "";
    }
    node.textContent = t(node.dataset.i18n, node.dataset.i18nDefault);
  }
  const title = t("title", "MemoryScope 内存分析");
  document.title = title;
  setText("page-title", title);
  setText("page-desc", t("desc", "按插件查看内存归因、保留量、增长趋势与告警"));
  setText("auto-label", t("auto.label", "自动刷新"));
  setText("skin-label", t("skin.label", "主题"));
  setText("btn-refresh", t("action.refresh", "刷新"));
  setText("btn-deep", t("action.deep", "深度扫描"));
  setText("btn-baseline-set", t("action.baselineSet", "设为基线"));
  setText("btn-baseline-clear", t("action.baselineClear", "清除基线"));
  setText("btn-gc", t("action.gc", "强制 GC"));
  setText("drawer-close", t("action.close", "关闭"));
  const search = el("search");
  if (search) search.placeholder = t("table.search", "搜索插件名…");
  buildSkinOptions();
  buildAutoOptions();
}

// -- overview cards --------------------------------------------------------

function kv(pairs) {
  const rows = pairs
    .filter((pair) => pair && pair[1] !== null && pair[1] !== undefined && pair[1] !== "")
    .map(
      (pair) =>
        "<dt>" +
        esc(pair[0]) +
        "</dt><dd title=\"" +
        esc(pair[1]) +
        "\">" +
        esc(pair[1]) +
        "</dd>",
    )
    .join("");
  return rows ? "<dl class=\"ms-kv\">" + rows + "</dl>" : "";
}

function card(title, lead, sub, body, tools) {
  return (
    "<section class=\"ms-card\"><header class=\"ms-card-head\"><h2>" +
    esc(title) +
    "</h2>" +
    (tools ? "<div class=\"ms-card-tools\">" + tools + "</div>" : "") +
    "</header>" +
    (lead ? "<div class=\"ms-card-lead\">" + esc(lead) + "</div>" : "") +
    (sub ? "<div class=\"ms-card-sub\">" + esc(sub) + "</div>" : "") +
    (body || "") +
    "</section>"
  );
}

function renderCards() {
  const node = el("cards");
  if (!node) return;
  const report = state.report || {};
  const process = report.process || {};
  const trace = process.tracemalloc || {};
  const totals = report.totals || {};
  const history = report.history || {};
  const gc = process.gc || {};

  const systemTotal = num(process.system_total_bytes);
  const processCard = card(
    t("card.process", "进程内存"),
    fmtBytes(process.rss_bytes),
    process.psutil_available === false
      ? t("note.psutil_missing", NOTE_FALLBACK.psutil_missing)
      : fmtPercent(process.memory_percent, 1) + (systemTotal ? " / " + fmtBytes(systemTotal) : ""),
    kv([
      [t("metric.vms", "VMS 虚拟内存"), fmtBytes(process.vms_bytes)],
      [t("metric.threads", "线程数"), fmtCount(process.threads)],
      [t("metric.uptime", "运行时长"), fmtUptime(process.uptime_seconds)],
      [t("metric.pid", "进程 PID"), process.pid === undefined ? null : String(process.pid)],
      [t("metric.python", "Python"), process.python_version || null],
    ]),
  );

  const tracing = !!trace.tracing;
  const traceTools =
    "<button class=\"ms-btn ms-btn-ghost ms-btn-sm\" type=\"button\" data-act=\"reset-peak\"" +
    (tracing ? "" : " disabled") +
    ">" +
    esc(t("action.resetPeak", "重置峰值")) +
    "</button>";
  const traceCard = card(
    t("card.tracemalloc", "已跟踪内存"),
    fmtBytes(trace.current_bytes),
    (tracing ? t("trace.on", "tracemalloc 已开启") : t("trace.off", "tracemalloc 未开启")) +
      " · " +
      t("metric.frames", "栈深度") +
      " " +
      (trace.frames === undefined ? "-" : trace.frames),
    kv([
      [t("metric.peak", "峰值"), fmtBytes(trace.peak_bytes)],
      [t("col.blocks", "内存块"), fmtCount(totals.traced_blocks)],
      [
        t("trace.full", "追踪覆盖插件导入阶段，数据完整。").split(/[：:]/)[0],
        trace.covers_plugin_import ? t("status.yes", "是") : t("status.no", "否"),
      ],
    ]),
    traceTools,
  );

  const tracedBytes = num(totals.traced_bytes) || 0;
  const pluginBytes = num(totals.plugin_bytes) || 0;
  const pluginsCard = card(
    t("card.plugins", "插件合计"),
    fmtBytes(pluginBytes),
    t("metric.pluginShare", "占已跟踪比例") +
      " " +
      (tracedBytes > 0 ? fmtPercent((pluginBytes * 100) / tracedBytes, 1) : "-"),
    kv([
      [
        t("metric.measured", "有数据插件"),
        fmtCount(totals.measured_plugin_count) + " / " + fmtCount(totals.plugin_count),
      ],
      [
        t("metric.samples", "采样点"),
        // Samples taken with tracing off carry RSS only, so show how many of
        // them actually contain per-plugin attribution.
        num(history.traced_samples) !== null &&
        num(history.traced_samples) !== num(history.samples)
          ? fmtCount(history.samples) +
            " (" +
            t("metric.tracedSamples", "含归因") +
            " " +
            fmtCount(history.traced_samples) +
            ")"
          : fmtCount(history.samples),
      ],
      [
        t("metric.interval", "采样间隔"),
        history.interval_seconds ? history.interval_seconds + t("unit.seconds", "秒") : null,
      ],
      [
        t("metric.baselineAt", "基线时间"),
        history.baseline_at ? fmtTime(history.baseline_at) : t("status.never", "从未"),
      ],
    ]),
  );

  const counts = Array.isArray(gc.counts) ? gc.counts : [];
  const gcCard = card(
    t("card.gc", "垃圾回收"),
    counts.length ? counts.join(" / ") : "-",
    t("metric.gcCounts", "各代待回收") +
      (Array.isArray(gc.thresholds) && gc.thresholds.length
        ? " · " + gc.thresholds.join(" / ")
        : ""),
    kv([
      [
        t("metric.gcCollections", "各代回收次数"),
        Array.isArray(gc.collections) && gc.collections.length ? gc.collections.join(" / ") : null,
      ],
      [t("metric.uncollectable", "不可回收对象"), fmtCount(gc.uncollectable)],
      [
        t("metric.trackedObjects", "GC 跟踪对象"),
        gc.tracked_objects === undefined || gc.tracked_objects === null
          ? null
          : fmtCount(gc.tracked_objects),
      ],
    ]),
  );

  node.innerHTML = processCard + traceCard + pluginsCard + gcCard;
}

// -- banner ----------------------------------------------------------------

function renderBanner() {
  const banner = el("banner");
  const button = el("btn-trace");
  if (!banner) return;
  const report = state.report;
  if (!report) {
    banner.hidden = true;
    return;
  }
  const trace = (report.process || {}).tracemalloc || {};
  const notes = Array.isArray(report.notes) ? report.notes : [];
  const extra = notes
    .filter((note) => note !== "tracemalloc_off" && note !== "tracing_started_late")
    .map((note) => t("note." + note, NOTE_FALLBACK[note] || note));

  let level = "ok";
  let title = "";
  let text = "";
  if (!trace.tracing) {
    level = "err";
    title = t("trace.off", "tracemalloc 未开启");
    text = t("trace.offHint", "未开启内存追踪时无法按插件归因，点击右侧按钮开启。");
  } else if (!trace.covers_plugin_import) {
    level = "warn";
    title = t("trace.late", "追踪在插件加载之后才启动");
    text = t("trace.lateHint", "插件导入阶段已分配的内存不会被计入。");
  } else if (extra.length) {
    level = "warn";
    title = t("note.title", "需要注意");
  } else {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.dataset.level = level;
  setText("banner-title", title);
  setText("banner-text", [text].concat(extra).filter(Boolean).join(" "));
  if (button) {
    button.textContent = trace.tracing
      ? t("action.traceOff", "关闭追踪")
      : t("action.traceOn", "开启追踪");
    button.dataset.mode = trace.tracing ? "stop" : "start";
  }
}

// -- charts ----------------------------------------------------------------

function renderTopChart() {
  const node = el("top-chart");
  if (!node) return;
  const rows = (((state.report || {}).plugins) || []).filter(
    (row) => (num(row.attributed_bytes) || 0) > 0,
  );
  if (!rows.length) {
    node.innerHTML =
      "<div class=\"ms-empty\">" + esc(t("chart.empty", "还没有可归因的数据")) + "</div>";
    return;
  }
  const top = rows.slice(0, TOP_CHART_ROWS);
  const max = num(top[0].attributed_bytes) || 1;
  const width = 720;
  const labelWidth = 210;
  const valueWidth = 92;
  const barWidth = width - labelWidth - valueWidth - 16;
  const rowHeight = 26;
  const height = top.length * rowHeight + 6;

  const parts = top.map((row, index) => {
    const y = index * rowHeight + 4;
    const value = num(row.attributed_bytes) || 0;
    const filled = Math.max(2, (value / max) * barWidth);
    const label = truncate(row.display_name || row.name, 28);
    return (
      "<text x=\"0\" y=\"" +
      (y + 13) +
      "\" font-size=\"12\" fill=\"var(--text)\">" +
      esc(label) +
      "</text>" +
      "<rect x=\"" +
      labelWidth +
      "\" y=\"" +
      (y + 4) +
      "\" width=\"" +
      barWidth +
      "\" height=\"12\" rx=\"3\" fill=\"var(--track)\"></rect>" +
      "<rect x=\"" +
      labelWidth +
      "\" y=\"" +
      (y + 4) +
      "\" width=\"" +
      filled.toFixed(1) +
      "\" height=\"12\" rx=\"3\" fill=\"var(--accent)\"></rect>" +
      "<text x=\"" +
      width +
      "\" y=\"" +
      (y + 13) +
      "\" font-size=\"11\" text-anchor=\"end\" fill=\"var(--muted)\">" +
      esc(fmtBytes(value)) +
      "</text>"
    );
  });

  node.innerHTML =
    "<svg viewBox=\"0 0 " +
    width +
    " " +
    height +
    "\" role=\"img\" aria-label=\"" +
    esc(t("chart.top", "占用最高的插件")) +
    "\">" +
    parts.join("") +
    "</svg>";
}

function bucketLabel(bucket) {
  const name = String(bucket || "");
  if (name === "…") return t("others.bucket_rest", "其余来源");
  if (name.indexOf("lib:") === 0) return name.slice(4);
  const known = ["astrbot_core", "python_stdlib", "other", "unknown"];
  return known.indexOf(name) === -1 ? name : t("others.bucket_" + name, name);
}

function renderOthers() {
  const node = el("others");
  if (!node) return;
  const others = ((state.report || {}).others) || [];
  if (!others.length) {
    node.innerHTML = "<div class=\"ms-empty\">" + esc(t("table.empty", "暂无数据")) + "</div>";
    return;
  }
  const max = others.reduce((acc, item) => Math.max(acc, num(item.bytes) || 0), 0) || 1;
  node.innerHTML = others
    .map((item) => {
      const value = num(item.bytes) || 0;
      const label = bucketLabel(item.bucket);
      return (
        "<div class=\"ms-bar-row\"><span class=\"ms-bar-label\" title=\"" +
        esc(item.bucket) +
        "\">" +
        esc(label) +
        "</span><span class=\"ms-bar-value\">" +
        esc(fmtBytes(value)) +
        "</span><span class=\"ms-bar-track\"><span class=\"ms-bar-fill\" style=\"width:" +
        ((value / max) * 100).toFixed(1) +
        "%\"></span></span></div>"
      );
    })
    .join("");
}

function sparkline(series, className) {
  const points = (Array.isArray(series) ? series : [])
    .map((point) => num(Array.isArray(point) ? point[1] : null))
    .filter((value) => value !== null);
  if (points.length < 2) return "";
  const max = Math.max.apply(null, points);
  const min = Math.min.apply(null, points);
  const span = max - min || 1;
  const width = 96;
  const height = 20;
  const pad = 2;
  const step = (width - pad * 2) / (points.length - 1);
  let path = "";
  points.forEach((value, index) => {
    const x = pad + step * index;
    const y = height - pad - ((value - min) / span) * (height - pad * 2);
    path += (index === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
  });
  return (
    "<svg class=\"" +
    (className || "ms-spark") +
    "\" viewBox=\"0 0 " +
    width +
    " " +
    height +
    "\" preserveAspectRatio=\"none\" aria-hidden=\"true\"><path d=\"" +
    path +
    "\"></path></svg>"
  );
}

function lineChart(series) {
  const rows = (Array.isArray(series) ? series : []).filter(
    (point) => Array.isArray(point) && num(point[1]) !== null,
  );
  if (rows.length < 2) return "";
  const values = rows.map((point) => num(point[1]));
  const max = Math.max.apply(null, values);
  const min = Math.min.apply(null, values);
  const span = max - min || 1;
  const width = 600;
  const height = 140;
  const padX = 8;
  const padY = 16;
  const step = (width - padX * 2) / (rows.length - 1);
  let path = "";
  values.forEach((value, index) => {
    const x = padX + step * index;
    const y = height - padY - ((value - min) / span) * (height - padY * 2);
    path += (index === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
  });
  return (
    "<div class=\"ms-chart\"><svg viewBox=\"0 0 " +
    width +
    " " +
    height +
    "\" role=\"img\" aria-label=\"" +
    esc(t("chart.series", "采样趋势")) +
    "\">" +
    "<path d=\"" +
    path +
    "\" fill=\"none\" stroke=\"var(--info)\" stroke-width=\"1.6\"></path>" +
    "<text x=\"" +
    padX +
    "\" y=\"11\" font-size=\"10\" fill=\"var(--muted)\">" +
    esc(fmtBytes(max)) +
    "</text>" +
    "<text x=\"" +
    padX +
    "\" y=\"" +
    (height - 4) +
    "\" font-size=\"10\" fill=\"var(--muted)\">" +
    esc(fmtBytes(min)) +
    "</text>" +
    "<text x=\"" +
    (width - padX) +
    "\" y=\"" +
    (height - 4) +
    "\" font-size=\"10\" text-anchor=\"end\" fill=\"var(--muted)\">" +
    esc(fmtTime(rows[rows.length - 1][0])) +
    "</text>" +
    "</svg></div>"
  );
}

// -- plugin table ----------------------------------------------------------

function retainedValue(row, field) {
  const retained = row.retained;
  if (!retained) return null;
  return num(retained[field]);
}

function sortValue(row, key) {
  switch (key) {
    case "name":
      return String(row.display_name || row.name || "").toLowerCase();
    case "attributed":
      return num(row.attributed_bytes) || 0;
    case "direct":
      return num(row.direct_bytes) || 0;
    case "exclusive":
      return retainedValue(row, "exclusive_bytes");
    case "shared":
      return retainedValue(row, "shared_bytes");
    case "objects":
      return retainedValue(row, "exclusive_objects");
    case "delta":
      return num(row.delta_bytes);
    case "trend":
      return num(row.trend_bytes_per_minute);
    case "share":
      return num(row.traced_share) || 0;
    default:
      return 0;
  }
}

function visibleRows() {
  const rows = (((state.report || {}).plugins) || []).slice();
  const needle = state.search.trim().toLowerCase();
  const filtered = needle
    ? rows.filter(
        (row) =>
          String(row.name || "").toLowerCase().indexOf(needle) !== -1 ||
          String(row.display_name || "").toLowerCase().indexOf(needle) !== -1,
      )
    : rows;
  const key = state.sortKey;
  const factor = state.sortDir === "asc" ? 1 : -1;
  filtered.sort((left, right) => {
    const a = sortValue(left, key);
    const b = sortValue(right, key);
    // Missing measurements always sort last, whatever the direction is.
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    if (typeof a === "string" || typeof b === "string") {
      return String(a).localeCompare(String(b)) * factor;
    }
    if (a === b) return String(left.name).localeCompare(String(right.name));
    return (a < b ? -1 : 1) * factor;
  });
  return filtered;
}

function renderTableHead() {
  const head = el("plugin-head");
  if (!head) return;
  head.innerHTML = COLUMNS.map((col) => {
    const sortable = col.sortable !== false;
    const sorted = sortable && state.sortKey === col.key;
    return (
      "<th" +
      (sortable ? " data-sort=\"" + esc(col.key) + "\"" : "") +
      (sorted ? " class=\"is-sorted\" data-dir=\"" + state.sortDir + "\"" : "") +
      ">" +
      esc(t(col.i18n, col.fallback)) +
      "</th>"
    );
  }).join("");
}

function tagsFor(row) {
  const tags = [];
  if (row.is_self) tags.push(t("table.self", "本插件"));
  if (row.activated === false) tags.push(t("table.inactive", "已禁用"));
  if (row.reserved) tags.push(t("table.reserved", "内置"));
  return tags
    .map((tag) => "<span class=\"ms-tag\">" + esc(tag) + "</span>")
    .join("");
}

function numCell(text, extraClass) {
  return (
    "<td class=\"ms-num" +
    (extraClass ? " " + extraClass : "") +
    "\">" +
    esc(text) +
    "</td>"
  );
}

function renderTableBody() {
  const body = el("plugin-body");
  if (!body) return;
  const rows = visibleRows();
  if (!rows.length) {
    const message = state.report
      ? state.search
        ? t("table.noMatch", "没有匹配的插件")
        : t("table.empty", "暂无数据")
      : t("toast.loading", "加载中…");
    body.innerHTML =
      "<tr><td colspan=\"" +
      COLUMNS.length +
      "\"><div class=\"ms-empty\">" +
      esc(message) +
      "</div></td></tr>";
    return;
  }
  body.innerHTML = rows
    .map((row) => {
      const delta = num(row.delta_bytes);
      const trend = num(row.trend_bytes_per_minute);
      const exclusive = retainedValue(row, "exclusive_bytes");
      const shared = retainedValue(row, "shared_bytes");
      const objects = retainedValue(row, "exclusive_objects");
      const truncated = row.retained && row.retained.truncated;
      const label = row.display_name || row.name;
      return (
        "<tr data-name=\"" +
        esc(row.name) +
        "\" tabindex=\"0\">" +
        "<td class=\"ms-cell-name\"><div class=\"ms-name\"><span class=\"ms-name-text\" title=\"" +
        esc(label) +
        "\">" +
        esc(label) +
        "</span>" +
        tagsFor(row) +
        "</div><span class=\"ms-name-id\" title=\"" +
        esc(row.name) +
        "\">" +
        esc(row.name) +
        "</span></td>" +
        numCell(fmtBytes(row.attributed_bytes)) +
        numCell(fmtBytes(row.direct_bytes), "ms-quiet") +
        numCell(exclusive === null ? "-" : fmtBytes(exclusive) + (truncated ? " ~" : "")) +
        numCell(shared === null ? "-" : fmtBytes(shared), "ms-quiet") +
        numCell(objects === null ? "-" : fmtCount(objects), "ms-quiet") +
        numCell(
          delta === null ? "-" : fmtSigned(delta),
          delta === null || Math.abs(delta) < KB ? "ms-quiet" : delta > 0 ? "ms-up" : "ms-down",
        ) +
        numCell(
          trend === null ? "-" : fmtPerHour(trend),
          trend === null || Math.abs(trend * 60) < 64 * KB
            ? "ms-quiet"
            : trend > 0
              ? "ms-up"
              : "ms-down",
        ) +
        numCell(fmtPercent(row.traced_share, 1), "ms-quiet") +
        "<td>" +
        (sparkline(state.seriesByPlugin[row.name]) ||
          "<span class=\"ms-quiet\">-</span>") +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
}

function renderTableChips() {
  const report = state.report || {};
  const totals = report.totals || {};
  const meta = report.deep_meta || {};
  const chip = el("deep-state");
  if (chip) {
    if (!meta.generated_at) {
      chip.textContent = t("table.deepNever", "尚未执行深度扫描");
      chip.removeAttribute("data-level");
    } else if (meta.truncated) {
      chip.textContent = t("table.deepTruncated", "深度扫描已达上限被截断，独占/共享偏小。");
      chip.dataset.level = "warn";
    } else if (meta.fresh) {
      chip.textContent =
        t("status.fresh", "本次刷新") +
        (meta.elapsed_ms ? " · " + Math.round(meta.elapsed_ms) + " " + t("unit.ms", "毫秒") : "");
      chip.dataset.level = "ok";
    } else {
      chip.textContent = t("table.deepStale", "深度扫描数据来自") + " " + fmtTime(meta.generated_at);
      chip.removeAttribute("data-level");
    }
  }
  const count = el("plugin-count");
  if (count) {
    const total = num(totals.plugin_count) || 0;
    const text = t("table.total", "共 {n} 个插件").replace("{n}", String(total));
    count.textContent = state.search ? visibleRows().length + " / " + text : text;
  }
}

function renderTable() {
  renderTableHead();
  renderTableBody();
  renderTableChips();
}

// -- alerts / help ---------------------------------------------------------

function renderAlerts() {
  const node = el("alerts");
  const badge = el("alert-badge");
  if (badge) {
    badge.textContent = String(state.alerts.length);
    badge.hidden = state.alerts.length === 0;
  }
  if (!node) return;
  if (!state.alerts.length) {
    const message = state.alertsEnabled
      ? t("alerts.empty", "暂无告警")
      : t("alerts.disabled", "未设置告警阈值。");
    node.innerHTML = "<div class=\"ms-empty\">" + esc(message) + "</div>";
    return;
  }
  node.innerHTML = state.alerts
    .slice()
    .reverse()
    .map(
      (alert) =>
        "<div class=\"ms-alert\" data-kind=\"" +
        esc(alert.kind) +
        "\"><span class=\"ms-alert-time\">" +
        esc(fmtTime(alert.ts)) +
        "</span><span class=\"ms-alert-msg\">" +
        esc(t("alerts.kind_" + alert.kind, alert.kind)) +
        " · " +
        esc(alert.message) +
        "</span></div>",
    )
    .join("");
}

function renderHelp() {
  const node = el("help-list");
  if (!node) return;
  node.innerHTML = HELP_KEYS.map((key) => {
    const fallback = HELP_FALLBACK[key] || ["help.title", key];
    const text = t("help." + key, "");
    if (!text) return "";
    const match = /^(.{1,32}?)(?:：|:\s)/.exec(text);
    const term = match ? match[1] : t(fallback[0], fallback[1]);
    const body = match ? text.slice(match[0].length) : text;
    return "<dt>" + esc(term) + "</dt><dd>" + esc(body) + "</dd>";
  }).join("");
}

// -- detail drawer ---------------------------------------------------------

function closeDrawer() {
  const drawer = el("drawer");
  if (drawer) drawer.hidden = true;
  state.drawerName = null;
  state.drawerPayload = null;
}

function monoList(rows, emptyText) {
  if (!rows.length) {
    return "<p class=\"ms-hint\">" + esc(emptyText) + "</p>";
  }
  return "<div class=\"ms-mono-list\">" + rows.join("") + "</div>";
}

function renderDetail(payload) {
  const content = el("drawer-content");
  if (!content) return;
  const detail = (payload && payload.detail) || {};
  const row = detail.row || {};
  setText("drawer-title", row.display_name || detail.name || t("detail.title", "插件详情"));
  if (!detail.found) {
    content.innerHTML =
      "<div class=\"ms-empty\">" +
      esc(t("detail.notFound", "未找到该插件，可能已被卸载。")) +
      "</div>";
    return;
  }
  const retained = row.retained || {};
  const meta = card(
    t("detail.meta", "基本信息"),
    "",
    "",
    kv([
      [t("detail.version", "版本"), row.version || "-"],
      [t("detail.author", "作者"), row.author || "-"],
      [t("detail.module", "模块路径"), row.module_path || "-"],
      [t("detail.root", "插件目录"), row.root_dir || "-"],
    ]),
    "<button class=\"ms-btn ms-btn-ghost ms-btn-sm\" type=\"button\" data-act=\"copy-detail\">" +
      esc(t("action.copy", "复制")) +
      "</button>",
  );

  const numbers = card(
    t("detail.numbers", "内存指标"),
    fmtBytes(row.attributed_bytes),
    t("col.share", "占比") + " " + fmtPercent(row.traced_share, 2),
    kv([
      [t("col.direct", "直接分配"), fmtBytes(row.direct_bytes)],
      [t("col.blocks", "内存块"), fmtCount(row.blocks)],
      [
        t("col.exclusive", "独占保留"),
        retained.exclusive_bytes === undefined ? "-" : fmtBytes(retained.exclusive_bytes),
      ],
      [
        t("col.shared", "共享保留"),
        retained.shared_bytes === undefined ? "-" : fmtBytes(retained.shared_bytes),
      ],
      [
        t("col.objects", "对象数"),
        retained.exclusive_objects === undefined ? "-" : fmtCount(retained.exclusive_objects),
      ],
      [t("col.delta", "Δ 基线"), row.delta_bytes === null ? "-" : fmtSigned(row.delta_bytes)],
      [
        t("col.trend", "增长趋势"),
        row.trend_bytes_per_minute === null ? "-" : fmtPerHour(row.trend_bytes_per_minute),
      ],
    ]),
  );

  const hotspots = card(
    t("detail.hotspots", "分配热点"),
    "",
    "",
    monoList(
      (detail.lines || []).map(
        (line) =>
          "<div class=\"ms-mono-row\"><span title=\"" +
          esc(line.filename) +
          "\">" +
          esc(shortPath(line.filename)) +
          ":" +
          esc(line.lineno) +
          "</span><span>" +
          esc(fmtBytes(line.bytes)) +
          "</span><span>" +
          esc(fmtCount(line.blocks)) +
          "</span></div>",
      ),
      t("detail.hotspotsEmpty", "没有采集到分配位置。"),
    ),
  );

  const series = detail.series || [];
  const seriesCard = card(
    t("detail.series", "归因内存趋势"),
    "",
    "",
    lineChart(series) ||
      "<p class=\"ms-hint\">" + esc(t("detail.seriesEmpty", "采样点不足，稍后再看。")) + "</p>",
  );

  const submodules = card(
    t("detail.submodules", "已加载子模块"),
    "",
    "",
    monoList(
      (detail.submodules || []).map(
        (name) => "<div class=\"ms-mono-row\"><span>" + esc(name) + "</span></div>",
      ),
      t("detail.submodulesEmpty", "没有额外子模块"),
    ),
  );

  content.innerHTML = meta + numbers + seriesCard + hotspots + submodules;
  content.scrollTop = 0;
}

async function openDetail(name) {
  if (!name) return;
  const drawer = el("drawer");
  if (drawer) drawer.hidden = false;
  state.drawerName = name;
  const content = el("drawer-content");
  if (content) {
    content.innerHTML =
      "<div class=\"ms-empty\">" + esc(t("toast.loading", "加载中…")) + "</div>";
  }
  try {
    const payload = await apiGet("detail", { name: name, limit: DETAIL_LINES });
    if (state.drawerName !== name) return;
    state.drawerPayload = payload;
    renderDetail(payload);
  } catch (err) {
    if (content) {
      content.innerHTML = "<div class=\"ms-empty\">" + esc(errorText(err)) + "</div>";
    }
    toast(errorText(err), "error");
  }
}

async function copyDetail() {
  const payload = state.drawerPayload;
  if (!payload) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
    toast(t("toast.copied", "已复制"));
  } catch (err) {
    toast(t("toast.copyFailed", "复制失败"), "error");
  }
}

// -- data loading ----------------------------------------------------------

function renderAll() {
  renderCards();
  renderBanner();
  renderTopChart();
  renderOthers();
  renderTable();
  renderAlerts();
  renderHelp();
  const deepButton = el("btn-deep");
  const settings = (state.overview || {}).settings || {};
  if (deepButton && settings.deep_scan_enabled === false) {
    deepButton.disabled = true;
    deepButton.title = t("table.deepHint", "");
  }
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["btn-refresh", "btn-deep", "btn-gc", "btn-baseline-set", "btn-baseline-clear"]) {
    const node = el(id);
    if (node) node.disabled = busy;
  }
}

async function refresh(options) {
  const opts = options || {};
  if (state.busy) return;
  setBusy(true);
  if (opts.deep) toast(t("toast.deepRunning", "正在扫描引用图…"));
  try {
    // The report is fetched first so the history request already contains the
    // sample it may have just recorded.
    const report = await apiGet("plugins", {
      deep: opts.deep ? 1 : 0,
      sample: opts.silent ? 0 : 1,
    });
    state.report = report;
    const [overview, history, alerts] = await Promise.all([
      apiGet("overview").catch(() => state.overview),
      apiGet("history", { limit: HISTORY_POINTS }).catch(() => null),
      apiGet("alerts", { limit: ALERT_LIMIT }).catch(() => null),
    ]);
    if (overview) state.overview = overview;
    if (history && history.series_by_plugin) state.seriesByPlugin = history.series_by_plugin;
    if (alerts) {
      state.alerts = Array.isArray(alerts.alerts) ? alerts.alerts : [];
      state.alertsEnabled = !!alerts.enabled;
    }
    renderAll();
    if (!opts.silent) {
      toast(opts.deep ? t("toast.deepDone", "深度扫描完成") : t("toast.refreshed", "已刷新"));
    }
    if (state.drawerName) await openDetail(state.drawerName);
  } catch (err) {
    toast(errorText(err), "error");
  } finally {
    setBusy(false);
  }
}

function stopAuto() {
  if (state.autoTimer) {
    window.clearInterval(state.autoTimer);
    state.autoTimer = null;
  }
}

function startAuto() {
  stopAuto();
  if (!state.auto) return;
  state.autoTimer = window.setInterval(() => {
    if (document.hidden || state.busy) return;
    refresh({ silent: true });
  }, state.auto * 1000);
}

function setAuto(seconds) {
  const value = AUTO_INTERVALS.indexOf(Number(seconds)) === -1 ? 0 : Number(seconds);
  state.auto = value;
  writeStore(STORE_AUTO, String(value));
  startAuto();
}

async function toggleTracing() {
  const button = el("btn-trace");
  const action = button && button.dataset.mode === "stop" ? "stop" : "start";
  try {
    await apiPost("tracing", { action: action });
    toast(
      action === "start"
        ? t("toast.traceStarted", "已开启 tracemalloc")
        : t("toast.traceStopped", "已关闭 tracemalloc"),
    );
    await refresh({ silent: true });
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function resetPeak() {
  try {
    await apiPost("tracing", { action: "reset_peak" });
    toast(t("toast.peakReset", "已重置峰值"));
    await refresh({ silent: true });
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function setBaseline(action) {
  try {
    await apiPost("baseline", { action: action });
    toast(
      action === "clear"
        ? t("toast.baselineCleared", "已清除基线")
        : t("toast.baselineSet", "已设为基线"),
    );
    await refresh({ silent: true });
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function forceGc() {
  setBusy(true);
  try {
    const result = await apiPost("gc", {});
    toast(
      t("toast.gcDone", "GC 完成") +
        " · " +
        fmtCount(result.collected) +
        " · " +
        fmtBytes(result.freed_bytes),
    );
  } catch (err) {
    toast(errorText(err), "error");
  } finally {
    setBusy(false);
  }
  await refresh({ silent: true });
}

// -- events ----------------------------------------------------------------

function bindEvents() {
  for (const button of document.querySelectorAll(".ms-tab")) {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  }
  el("btn-refresh").addEventListener("click", () => refresh({}));
  el("btn-deep").addEventListener("click", () => refresh({ deep: true }));
  el("btn-trace").addEventListener("click", toggleTracing);
  el("btn-baseline-set").addEventListener("click", () => setBaseline("set"));
  el("btn-baseline-clear").addEventListener("click", () => setBaseline("clear"));
  el("btn-gc").addEventListener("click", forceGc);
  el("drawer-close").addEventListener("click", closeDrawer);

  el("cards").addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (button && button.dataset.act === "reset-peak") resetPeak();
  });

  el("drawer").addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) {
      closeDrawer();
      return;
    }
    const button = event.target.closest("[data-act]");
    if (button && button.dataset.act === "copy-detail") copyDetail();
  });

  el("plugin-head").addEventListener("click", (event) => {
    const cell = event.target.closest("th[data-sort]");
    if (!cell) return;
    const key = cell.dataset.sort;
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
    } else {
      state.sortKey = key;
      state.sortDir = key === "name" ? "asc" : "desc";
    }
    writeStore(STORE_SORT, state.sortKey + ":" + state.sortDir);
    renderTable();
  });

  el("plugin-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-name]");
    if (row) openDetail(row.dataset.name);
  });
  el("plugin-body").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("tr[data-name]");
    if (!row) return;
    event.preventDefault();
    openDetail(row.dataset.name);
  });

  let searchTimer = null;
  el("search").addEventListener("input", (event) => {
    state.search = event.target.value || "";
    if (searchTimer) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(renderTable, 120);
  });

  el("skin-select").addEventListener("change", (event) => {
    state.skin = SKIN_IDS.indexOf(event.target.value) === -1 ? "auto" : event.target.value;
    writeStore(STORE_SKIN, state.skin);
    applySkin();
  });
  el("auto-select").addEventListener("change", (event) => setAuto(event.target.value));

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("drawer").hidden) closeDrawer();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.auto) refresh({ silent: true });
  });
  window.addEventListener("beforeunload", stopAuto);
}

// -- boot ------------------------------------------------------------------

async function main() {
  if (!bridge) {
    document.body.innerHTML =
      "<p style=\"padding:24px;font-family:system-ui\">MemoryScope 需要在 AstrBot Dashboard 的插件页内打开。</p>";
    return;
  }
  await bridge.ready();

  state.skin = readStore(STORE_SKIN, "auto");
  if (SKIN_IDS.indexOf(state.skin) === -1) state.skin = "auto";
  state.auto = Number(readStore(STORE_AUTO, "0")) || 0;
  if (AUTO_INTERVALS.indexOf(state.auto) === -1) state.auto = 0;
  const savedSort = String(readStore(STORE_SORT, "attributed:desc")).split(":");
  if (SORT_KEYS.indexOf(savedSort[0]) !== -1) {
    state.sortKey = savedSort[0];
    state.sortDir = savedSort[1] === "asc" ? "asc" : "desc";
  }
  const startTab = readStore(STORE_TAB, "overview");

  applyStaticText();
  applySkin();
  bindEvents();
  setTab(startTab);
  renderHelp();

  let first = true;
  const onContextChange = () => {
    if (first) {
      first = false;
      return;
    }
    applyStaticText();
    applySkin();
    renderAll();
    if (state.drawerPayload) renderDetail(state.drawerPayload);
  };
  try {
    if (typeof bridge.onContextChange === "function") bridge.onContextChange(onContextChange);
    else if (typeof bridge.onContext === "function") bridge.onContext(onContextChange);
  } catch (err) {
    /* older dashboards expose neither callback */
  }

  await refresh({ silent: true });
  startAuto();
}

main().catch((err) => toast(errorText(err), "error"));
