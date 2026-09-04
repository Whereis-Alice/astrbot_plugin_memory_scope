// MemoryScope dashboard page: dependency-free, no build step, no bundler.
// The page never instruments Python allocations.  It only renders counters that
// the backend already collected: /proc probes, import-window accounting, and
// explicitly requested object/reference scans.
//
// Style note: this file deliberately avoids template literals so it stays easy
// to patch with plain text tooling.  String concatenation only.

const bridge = window.AstrBotPluginPage;

const STORE_SKIN = "memoryscope.skin";
const STORE_AUTO = "memoryscope.auto";
const STORE_TAB = "memoryscope.tab";
const STORE_SORT = "memoryscope.sort";

const TABS = ["overview", "plugins", "imports", "audit", "census", "alerts", "help"];
const SKINS = [
  { id: "auto", label: "跟随 Dashboard" },
  { id: "dark", label: "深色" },
  { id: "light", label: "浅色" },
];
const SKIN_IDS = SKINS.map((item) => item.id);
const AUTO_INTERVALS = [0, 10, 30, 60, 300];
const ALERT_LIMIT = 40;
const KB = 1024;
const MB = 1024 * 1024;
const SVG_NS = "http://www.w3.org/2000/svg";
const TREND = { w: 840, h: 300, padX: 42, padY: 34 };

const reduceMotion = (function () {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (_error) {
    return false;
  }
})();

const COLUMNS = [
  { key: "name", i18n: "col.plugin", fallback: "插件" },
  { key: "import", i18n: "col.import", fallback: "导入成本" },
  { key: "census", i18n: "col.census", fallback: "对象普查" },
  { key: "objects", i18n: "col.objects", fallback: "对象数" },
  { key: "retained", i18n: "col.retained", fallback: "引用图保留" },
  { key: "trend", i18n: "col.trend", fallback: "增长趋势" },
  { key: "chart", i18n: "col.chart", fallback: "近期曲线", sortable: false },
];
const SORT_KEYS = COLUMNS.filter((item) => item.sortable !== false).map((item) => item.key);

const NOTE_FALLBACK = {
  import_hook_not_installed: "导入成本钩子没有留下有效记录；重启后会在更早阶段安装。",
  import_hook_degraded: "导入成本钩子已自动降级，导入账本可能不完整。",
  partial_import_coverage: "部分插件在本插件安装钩子之前已加载，导入成本显示为未知。",
  census_never_run: "对象普查尚未运行；需要时在“对象普查”页手动执行。",
  census_sampled: "对象普查使用抽样，结果是估算值，适合比较比例。",
  census_truncated: "对象普查达到时间上限，结果偏小。",
  dep_audit_never_run: "依赖审计尚未运行。",
  dep_audit_truncated: "依赖审计达到时间上限，部分源码未扫描。",
  psutil_missing: "没有 psutil，部分进程级指标不可用。",
  rss_reader_unavailable: "无法读取进程 RSS。",
  deep_scan_truncated: "引用图扫描达到上限，保留量是下界。",
  smaps_unavailable: "内核没有提供 smaps_rollup，只能显示 RSS，看不到共享页与换出页的真实足迹。",
  retained_never_run: "引用图保留量尚未扫描；点“引用图扫描”或等后台轮转采样。",
  retained_partial: "引用图扫描按轮转配额切片，本轮只覆盖了部分插件，覆盖率会逐轮补齐。",
};

const state = {
  tab: "overview",
  skin: "auto",
  auto: 0,
  autoTimer: null,
  report: null,
  overview: null,
  history: null,
  alerts: [],
  alertsEnabled: false,
  search: "",
  sortKey: "retained",
  sortDir: "desc",
  busy: false,
  action: null,
  drawerName: null,
  drawerPayload: null,
  toastTimer: null,
};

const el = (id) => document.getElementById(id);

function t(key, fallback) {
  try {
    const value = bridge && typeof bridge.t === "function"
      ? bridge.t("pages.memory." + key, fallback)
      : fallback;
    return value === undefined || value === null || value === "" ? fallback : value;
  } catch (_error) {
    return fallback;
  }
}

function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function known(value) {
  return value !== null && value !== undefined && num(value) !== null;
}

function positive(value) {
  const parsed = num(value);
  return parsed !== null && parsed > 0 ? parsed : null;
}

function fmtBytes(value, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  if (parsed === null) return missing;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.abs(parsed);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : size >= 100 ? 0 : size >= 10 ? 1 : 2;
  const valueText = size.toFixed(digits).replace(/\.0+$/, "");
  return (parsed < 0 ? "−" : "") + valueText + " " + units[unit];
}

function splitBytes(value) {
  // Same scaling as fmtBytes but the unit is returned separately so the hero
  // headline can render it in a smaller weight.
  const parsed = num(value);
  if (parsed === null) return { value: "—", unit: "" };
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.abs(parsed);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return {
    value: (parsed < 0 ? "−" : "") + size.toFixed(digits).replace(/\.0+$/, ""),
    unit: units[unit],
  };
}

function fmtSigned(value, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  if (parsed === null) return missing;
  if (Math.abs(parsed) < KB) return "≈0";
  return (parsed > 0 ? "+" : "−") + fmtBytes(Math.abs(parsed));
}

function fmtRate(bytesPerMinute, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(bytesPerMinute);
  if (parsed === null) return missing;
  const perHour = parsed * 60;
  if (Math.abs(perHour) < 64 * KB) return "≈0";
  return (perHour > 0 ? "+" : "−") + fmtBytes(Math.abs(perHour)) + t("unit.perHour", "/小时");
}

function fmtCount(value, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  if (parsed === null) return missing;
  try {
    return Math.round(parsed).toLocaleString();
  } catch (_error) {
    return String(Math.round(parsed));
  }
}

function fmtPercent(value, digits, missing) {
  if (digits === undefined) digits = 1;
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  return parsed === null ? missing : parsed.toFixed(digits) + "%";
}

function fmtMs(value, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  if (parsed === null) return missing;
  return parsed >= 1000 ? (parsed / 1000).toFixed(2) + " s" : Math.round(parsed) + " ms";
}

function fmtTime(value, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  if (parsed === null || parsed <= 0) return missing;
  try {
    return new Date(parsed * 1000).toLocaleString();
  } catch (_error) {
    return missing;
  }
}

function fmtClock(value, missing) {
  if (missing === undefined) missing = "—";
  const parsed = num(value);
  if (parsed === null || parsed <= 0) return missing;
  try {
    return new Date(parsed * 1000).toLocaleTimeString();
  } catch (_error) {
    return missing;
  }
}

function fmtDuration(value, missing) {
  if (missing === undefined) missing = "—";
  const seconds = num(value);
  if (seconds === null || seconds < 0) return missing;
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const pad = (part) => String(part).padStart(2, "0");
  const clock = pad(hours) + ":" + pad(minutes) + ":" + pad(total % 60);
  return days ? days + "d " + clock : clock;
}

function truncate(value, max) {
  if (max === undefined) max = 80;
  const text = String(value === undefined || value === null ? "" : value);
  return text.length <= max ? text : text.slice(0, Math.max(1, max - 1)) + "…";
}

function readStore(key, fallback) {
  // Plugin Pages are sandboxed and must not depend on Dashboard storage.
  // Preferences intentionally live only for the lifetime of this iframe.
  void key;
  return fallback;
}

function writeStore(key, value) {
  void key;
  void value;
}

function toast(message, kind) {
  const node = el("toast");
  if (!node) return;
  node.textContent = String(message);
  node.dataset.kind = kind === "error" ? "error" : "ok";
  node.hidden = false;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(function () { node.hidden = true; }, kind === "error" ? 7000 : 2800);
}

function errorText(error) {
  if (!error) return t("error.request", "请求失败");
  if (typeof error === "string") return error;
  return error.message || String(error);
}

function unwrap(result) {
  if (result && typeof result === "object" && result.status === "error") {
    throw new Error(result.message || t("error.request", "请求失败"));
  }
  if (result && typeof result === "object" && result.status === "ok" && "data" in result) {
    return result.data;
  }
  return result;
}

async function apiGet(endpoint, params) {
  if (!bridge || typeof bridge.apiGet !== "function") throw new Error(t("error.bridge", "页面桥接不可用"));
  return unwrap(await bridge.apiGet(endpoint, params || {}));
}

async function apiPost(endpoint, body) {
  if (!bridge || typeof bridge.apiPost !== "function") throw new Error(t("error.bridge", "页面桥接不可用"));
  return unwrap(await bridge.apiPost(endpoint, body || {}));
}

function isDarkContext() {
  try {
    const context = bridge && typeof bridge.getContext === "function" ? bridge.getContext() : null;
    if (context && typeof context.isDark === "boolean") return context.isDark;
  } catch (_error) {
    // Fall back to the browser preference.
  }
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch (_error) {
    return true;
  }
}

function applySkin() {
  const effective = state.skin === "auto" ? (isDarkContext() ? "dark" : "light") : state.skin;
  document.documentElement.dataset.skin = effective;
  document.documentElement.dataset.theme = effective;
  const select = el("skin-select");
  if (select && select.value !== state.skin) select.value = state.skin;
}

function buildSelects() {
  const skin = el("skin-select");
  if (skin) {
    skin.innerHTML = SKINS.map(function (item) {
      return '<option value="' + esc(item.id) + '">' + esc(t("skin." + item.id, item.label)) + "</option>";
    }).join("");
    skin.value = state.skin;
  }
  const auto = el("auto-select");
  if (auto) {
    auto.innerHTML = AUTO_INTERVALS.map(function (seconds) {
      const label = seconds === 0 ? t("auto.off", "关闭") : seconds + "s";
      return '<option value="' + seconds + '">' + esc(label) + "</option>";
    }).join("");
    auto.value = String(state.auto);
  }
}

function applyStaticText() {
  for (const node of document.querySelectorAll("[data-i18n]")) {
    if (node.dataset.i18nDefault === undefined) node.dataset.i18nDefault = node.textContent || "";
    node.textContent = t(node.dataset.i18n, node.dataset.i18nDefault);
  }
  const search = el("search");
  if (search) search.placeholder = t("table.search", "搜索插件名…");
  document.title = t("title", "MemoryScope 内存观察");
  const title = el("page-title");
  if (title) title.textContent = t("title", "MemoryScope 内存观察");
  const desc = el("page-desc");
  if (desc) desc.textContent = t("desc", "用轻量探针观察进程足迹、插件导入成本与引用图保留量");
  const autoLabel = el("auto-label");
  if (autoLabel) autoLabel.textContent = t("auto.label", "自动刷新");
  const skinLabel = el("skin-label");
  if (skinLabel) skinLabel.textContent = t("skin.label", "主题");
  buildSelects();
}

function setTab(name) {
  state.tab = TABS.includes(name) ? name : "overview";
  writeStore(STORE_TAB, state.tab);
  for (const button of document.querySelectorAll(".nav-btn")) {
    const active = button.dataset.tab === state.tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("is-active", panel.id === "panel-" + state.tab);
  }
}

function setAction(action) {
  state.action = action;
  state.busy = Boolean(action);
  for (const button of document.querySelectorAll("button")) {
    if (button.id === "drawer-close") continue;
    if (button.classList.contains("nav-btn")) continue;
    button.disabled = Boolean(action) && button.id !== "btn-refresh";
  }
  const refresh = el("btn-refresh");
  if (refresh) refresh.disabled = Boolean(action) && action !== "refresh";
}

/* ---------------------------------------------------------------- builders */

function statusRow(label, value, detail, level) {
  return '<div class="status-row" data-level="' + esc(level || "neutral") + '">'
    + '<div class="status-main"><span class="status-dot"></span><span>' + esc(label) + "</span></div>"
    + "<strong>" + esc(value) + "</strong>"
    + "<small>" + esc(detail || "") + "</small>"
    + "</div>";
}

function kv(label, value) {
  return '<div class="kv-row"><span>' + esc(label) + "</span><strong>" + esc(value) + "</strong></div>";
}

function pill(label, level) {
  return '<span class="pill' + (level ? " " + esc(level) : "") + '">' + esc(label) + "</span>";
}

function heroStat(label, value) {
  return '<div class="hero-stat"><span>' + esc(label) + "</span><strong>" + esc(value) + "</strong></div>";
}

function reportProcess() {
  return (state.report && state.report.process) || (state.overview && state.overview.process) || {};
}

function reportTotals() {
  return (state.report && state.report.totals) || {};
}

function reportHistory() {
  return (state.report && state.report.history) || (state.overview && state.overview.history) || {};
}

function reportAttribution() {
  return (state.report && state.report.attribution) || {};
}

function censusReady() {
  return Boolean(state.report && state.report.census_meta);
}

function footprintBytes(process) {
  // Preferred accuracy order: smaps_rollup footprint (Pss + SwapPss) > Pss
  // alone > plain RSS.  Anything below the first option undercounts a process
  // that has been partially swapped out, which is the normal state on a 1.6 GB
  // VPS with zram.
  const source = process || reportProcess();
  return positive(source.footprint_bytes)
    || positive(source.pss_bytes)
    || positive(source.rss_bytes);
}

function footprintLabel(process) {
  const source = process || reportProcess();
  if (positive(source.footprint_bytes)) return t("hero.sourceFootprint", "Pss + SwapPss（真实足迹）");
  if (positive(source.pss_bytes)) return t("hero.sourcePss", "Pss（按共享页均摊）");
  return t("hero.sourceRss", "RSS（内核没提供 smaps_rollup）");
}

/* ------------------------------------------------------------- sparklines */

function sparkValues(series) {
  // Accepts either a flat number array or the [[ts, value], ...] shape the
  // history endpoint returns.  Zero means "probe did not run at this sample",
  // so it is treated as a gap rather than a real drop to zero.
  const out = [];
  for (const item of series || []) {
    const value = Array.isArray(item) ? num(item[1]) : num(item);
    if (value === null || value <= 0) continue;
    out.push(value);
  }
  return out;
}

function sparkline(series, tone) {
  const values = sparkValues(series);
  if (values.length < 2) return '<span class="spark-empty">—</span>';
  const w = 120;
  const h = 40;
  const padX = 2;
  const padY = 5;
  const maxV = Math.max.apply(null, values);
  const minV = Math.min.apply(null, values);
  const floor = maxV === minV ? Math.max(0, minV - 1) : minV - (maxV - minV) * 0.3;
  const span = Math.max(1, maxV - floor);
  const line = smoothLinePath(values.map(function (v) { return v - floor; }), w, h, padX, padY, span, 0.4);
  if (!line) return '<span class="spark-empty">—</span>';
  const base = h - padY;
  const area = line + " L " + (w - padX) + " " + base + " L " + padX + " " + base + " Z";
  const style = tone ? ' style="--tone:' + tone + '"' : "";
  return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true"' + style + ">"
    + '<path class="spark-area" d="' + area + '" />'
    + '<path class="spark-path" vector-effect="non-scaling-stroke" d="' + line + '" />'
    + "</svg>";
}

/* ------------------------------------------------------------------- hero */

function renderHero() {
  const process = reportProcess();
  const totals = reportTotals();
  const history = reportHistory();
  const attribution = reportAttribution();
  const trend = state.history || {};
  const footprint = footprintBytes(process);
  const parts = splitBytes(footprint);

  const value = el("hero-value");
  if (value) value.textContent = parts.value;
  const unit = el("hero-unit");
  if (unit) unit.textContent = parts.unit;

  const lead = el("hero-lead");
  if (lead) {
    lead.textContent = t("hero.lead",
      "足迹口径 = 常驻内存按共享页均摊后的 Pss 加上已换出的 SwapPss。它比 RSS 更接近“这个进程实际吃掉多少物理内存 + swap”。"
      + "插件级数字来自引用图保留量估算，是分摊值而不是精确独占 RSS。")
      + " " + t("hero.source", "当前来源：") + footprintLabel(process);
  }

  const tags = el("hero-tags");
  if (tags) {
    const items = [];
    items.push(pill("PID " + fmtCount(process.pid, "—")));
    items.push(pill("Python " + (process.python_version || "—")));
    items.push(pill(t("hero.uptime", "已运行") + " " + fmtDuration(process.uptime_seconds)));
    items.push(pill(t("hero.plugins", "插件") + " " + fmtCount(totals.plugin_count, "—")));
    items.push(pill(t("hero.samples", "采样点") + " " + fmtCount(history.samples, "0"),
      num(history.samples) ? "info" : "warn"));
    items.push(pill(t("hero.interval", "采样间隔") + " " + fmtCount(history.interval_seconds || trend.interval_seconds, "—") + "s"));
    if (num(process.threads) !== null) items.push(pill(t("hero.threads", "线程") + " " + fmtCount(process.threads)));
    tags.innerHTML = items.join("");
  }

  const side = el("hero-side");
  if (side) {
    const rows = [];
    rows.push(heroStat(t("hero.rss", "常驻 RSS"), fmtBytes(process.rss_bytes)));
    rows.push(heroStat(t("hero.swap", "已换出"),
      fmtBytes(positive(process.swap_pss_bytes) || positive(process.rollup_swap_bytes) || process.swap_bytes)));
    rows.push(heroStat(t("hero.baseline", "相对基线"),
      fmtSigned(known(process.rss_delta_bytes) ? process.rss_delta_bytes : trend.rss_delta_bytes)));
    rows.push(heroStat(t("hero.trendPerHour", "足迹趋势"),
      fmtRate(known(trend.footprint_trend_bytes_per_minute) ? trend.footprint_trend_bytes_per_minute : trend.rss_trend_bytes_per_minute)));
    if (positive(process.cgroup_limit_bytes)) {
      rows.push(heroStat(t("hero.cgroup", "容器额度"),
        fmtPercent(process.cgroup_memory_percent) + " / " + fmtBytes(process.cgroup_limit_bytes)));
    } else {
      rows.push(heroStat(t("hero.systemFree", "系统可用"), fmtBytes(process.system_available_bytes)));
    }
    rows.push(heroStat(t("hero.coverage", "归因覆盖率"),
      known(attribution.coverage_percent) ? fmtPercent(attribution.coverage_percent) : t("status.notRun", "未运行")));
    side.innerHTML = rows.join("");
  }
}

/* ---------------------------------------------------------- metric cards */

function metricCard(options) {
  const parts = splitBytes(options.bytes);
  const valueText = options.text !== undefined
    ? esc(options.text)
    : esc(parts.value) + (parts.unit ? "<small>" + esc(parts.unit) + "</small>" : "");
  return '<article class="metric" style="--tone:var(--' + esc(options.tone) + ')">'
    + '<div class="label"><i></i>' + esc(options.label) + "</div>"
    + '<div class="value">' + valueText + "</div>"
    + '<div class="delta">' + options.delta + "</div>"
    + '<div class="metric-viz">' + sparkline(options.series) + "</div>"
    + "</article>";
}

function renderMetrics() {
  const node = el("metrics");
  if (!node) return;
  const process = reportProcess();
  const totals = reportTotals();
  const attribution = reportAttribution();
  const trend = state.history || {};
  const points = trendPoints();
  const gc = process.gc || {};

  const footprint = footprintBytes(process);
  const swap = positive(process.swap_pss_bytes) || positive(process.rollup_swap_bytes) || positive(process.swap_bytes);
  const retained = positive(totals.retained_bytes);
  const blocks = num(process.allocated_blocks);

  const cards = [];
  cards.push(metricCard({
    tone: "pink",
    label: t("metric.footprint", "内存足迹"),
    bytes: footprint,
    delta: esc(footprintLabel(process)) + "<br /><b>" + esc(fmtRate(known(trend.footprint_trend_bytes_per_minute)
      ? trend.footprint_trend_bytes_per_minute
      : trend.rss_trend_bytes_per_minute)) + "</b>",
    series: points.map(function (p) { return p.footprint; }),
  }));
  cards.push(metricCard({
    tone: "violet",
    label: t("metric.rss", "常驻 RSS"),
    bytes: process.rss_bytes,
    delta: esc(t("metric.rssSub", "私有脏页") + " " + fmtBytes(process.private_dirty_bytes))
      + "<br /><b>" + esc(fmtPercent(process.memory_percent, 1, "—") + " " + t("metric.systemShare", "占系统")) + "</b>",
    series: points.map(function (p) { return p.rss; }),
  }));
  cards.push(metricCard({
    tone: "orange",
    label: t("metric.swap", "已换出"),
    bytes: swap === null ? 0 : swap,
    text: swap === null ? "0<small>B</small>" : undefined,
    delta: esc(t("metric.swapSub", "被内核挪到 swap/zram 的部分；重新访问会造成停顿。"))
      + "<br /><b>" + esc(footprint && swap ? fmtPercent(swap / footprint * 100) + " " + t("metric.ofFootprint", "占足迹") : t("metric.swapNone", "没有换出")) + "</b>",
    series: points.map(function (p) { return p.swap; }),
  }));
  cards.push(metricCard({
    tone: "blue",
    label: t("metric.retained", "归因保留"),
    bytes: retained,
    text: retained === null ? t("status.notRun", "未运行") : undefined,
    delta: esc(t("metric.retainedSub", "引用图可达对象；共享对象按 1/N 均摊。"))
      + "<br /><b>" + esc(known(attribution.coverage_percent)
        ? fmtPercent(attribution.coverage_percent) + " " + t("metric.coverage", "覆盖私有脏页")
        : t("metric.coverageUnknown", "覆盖率未知")) + "</b>",
    series: points.map(function (p) { return p.retained; }),
  }));
  cards.push(metricCard({
    tone: "green",
    label: t("metric.blocks", "分配块数"),
    text: fmtCount(blocks),
    delta: esc(t("metric.blocksSub", "CPython 分配器持有的块数，泄漏时会单调上涨。"))
      + "<br /><b>" + esc(fmtCount(trend.blocks_trend_per_minute === undefined ? null : Math.round(trend.blocks_trend_per_minute * 60), "—")
        + " " + t("unit.perHour", "/小时")) + "</b>",
    series: points.map(function (p) { return p.blocks; }),
  }));
  void gc;
  node.innerHTML = cards.join("");
}

/* ------------------------------------------------------------ chart core */

function smoothLinePath(values, w, h, padX, padY, max, curve) {
  curve = curve === undefined ? 0.42 : curve;
  const span = Math.max(1, values.length - 1);
  const pts = values.map(function (v, i) {
    return [padX + i * (w - padX * 2) / span, h - padY - (Number(v) || 0) / max * (h - padY * 2)];
  });
  if (!pts.length) return "";
  let path = "M " + pts[0][0].toFixed(1) + " " + pts[0][1].toFixed(1);
  for (let i = 1; i < pts.length; i++) {
    const p0 = pts[i - 1];
    const p1 = pts[i];
    const dx = (p1[0] - p0[0]) * curve;
    path += " C " + (p0[0] + dx).toFixed(1) + " " + p0[1].toFixed(1)
      + ", " + (p1[0] - dx).toFixed(1) + " " + p1[1].toFixed(1)
      + ", " + p1[0].toFixed(1) + " " + p1[1].toFixed(1);
  }
  return path;
}

function animateSvgPaths(root) {
  if (reduceMotion || !root) return;
  for (const path of root.querySelectorAll(".chart-path,.spark-path")) {
    let length = 0;
    try {
      length = path.getTotalLength();
    } catch (_error) {
      continue;
    }
    if (!length) continue;
    path.style.strokeDasharray = length;
    path.style.strokeDashoffset = length;
    window.requestAnimationFrame(function () {
      path.style.transition = "stroke-dashoffset 1.25s var(--ease)";
      path.style.strokeDashoffset = "0";
    });
  }
}

function trendPoints() {
  // points[i] = [ts, rss, pss, swap_pss, allocated_blocks, retained_total, census_bytes]
  // A zero means "that probe did not run for this sample" and must be treated
  // as a gap, never as a real drop to zero.
  const raw = (state.history && state.history.points) || [];
  const out = [];
  for (const point of raw) {
    if (!Array.isArray(point)) continue;
    const ts = num(point[0]);
    if (ts === null) continue;
    const rss = num(point[1]) || 0;
    const pss = num(point[2]) || 0;
    const swap = num(point[3]) || 0;
    const blocks = num(point[4]) || 0;
    const retained = num(point[5]) || 0;
    const census = num(point[6]) || 0;
    out.push({
      ts: ts,
      rss: rss,
      pss: pss,
      swap: swap,
      blocks: blocks,
      retained: retained,
      census: census,
      footprint: pss > 0 ? pss + swap : rss,
    });
  }
  return out;
}

function chartGeometry(node) {
  const width = Math.max(360, Math.round(node.clientWidth || TREND.w));
  const height = Math.max(180, Math.round(node.clientHeight || TREND.h));
  return { w: width, h: height, padX: TREND.padX, padY: TREND.padY };
}

function renderTrendChart(node, points) {
  if (!node) return;
  if (points.length < 2) {
    node.innerHTML = '<div class="empty">' + esc(t("chart.empty", "暂无历史趋势数据；后台采样几轮后会出现曲线。")) + "</div>";
    return;
  }
  const geom = chartGeometry(node);
  const w = geom.w;
  const h = geom.h;
  const padX = geom.padX;
  const padY = geom.padY;
  const inner = h - padY * 2;
  const hasPss = points.some(function (p) { return p.pss > 0; });

  const footprint = points.map(function (p) { return p.footprint; });
  const rss = points.map(function (p) { return p.rss; });
  const scale = hasPss ? footprint.concat(rss) : footprint;
  let maxV = Math.max.apply(null, scale);
  const minV = Math.min.apply(null, scale);
  // Keep a floor under the lowest sample.  Without it a process sitting at
  // 600 MB draws a dead-flat line and small regressions become invisible.
  let floorV = maxV === minV
    ? Math.max(0, minV - Math.max(1, minV * 0.1))
    : Math.max(0, minV - (maxV - minV) * 0.35);
  maxV = maxV * 1.08;
  const spanV = Math.max(1, maxV - floorV);
  const yOf = function (value) { return h - padY - ((value - floorV) / spanV) * inner; };

  const defs = '<defs>'
    + '<linearGradient id="trendArea" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0%" stop-color="var(--pink)" stop-opacity="0.38" />'
    + '<stop offset="100%" stop-color="var(--pink)" stop-opacity="0" />'
    + "</linearGradient>"
    + '<linearGradient id="drawBars" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0%" stop-color="var(--blue)" stop-opacity="0.42" />'
    + '<stop offset="100%" stop-color="var(--blue)" stop-opacity="0.08" />'
    + "</linearGradient>"
    + "</defs>";

  let grid = "";
  for (const ratio of [0, 0.25, 0.5, 0.75, 1]) {
    const y = padY + ratio * inner;
    const label = floorV + (maxV - floorV) * (1 - ratio);
    grid += '<line class="gridline" x1="' + padX + '" y1="' + y.toFixed(1) + '" x2="' + (w - padX) + '" y2="' + y.toFixed(1) + '" />'
      + '<text class="chart-label" x="5" y="' + (y + 4).toFixed(1) + '">' + esc(fmtBytes(label)) + "</text>";
  }

  const step = (w - padX * 2) / Math.max(1, points.length - 1);
  const barMax = Math.max.apply(null, points.map(function (p) { return p.retained; }).concat([0]));
  let bars = "";
  if (barMax > 0) {
    const barWidth = Math.max(8, Math.min(step * 0.34, 24));
    points.forEach(function (point, index) {
      if (point.retained <= 0) return;
      const barHeight = (point.retained / barMax) * inner * 0.42;
      if (barHeight <= 0.5) return;
      const x = padX + index * step - barWidth / 2;
      const y = h - padY - barHeight;
      bars += '<rect class="chart-draw-bar" x="' + x.toFixed(1) + '" y="' + y.toFixed(1)
        + '" width="' + barWidth.toFixed(1) + '" height="' + barHeight.toFixed(1)
        + '" rx="' + Math.min(6, barWidth / 2).toFixed(1) + '" fill="url(#drawBars)"'
        + ' style="--delay:' + (index * 28) + 'ms" />';
    });
  }

  const base = h - padY;
  const footprintLine = smoothLinePath(footprint.map(function (v) { return v - floorV; }), w, h, padX, padY, spanV);
  const area = footprintLine + " L " + (w - padX).toFixed(1) + " " + base.toFixed(1)
    + " L " + padX.toFixed(1) + " " + base.toFixed(1) + " Z";
  let lines = '<path class="chart-area" d="' + area + '" fill="url(#trendArea)" />'
    + '<path class="chart-path" d="' + footprintLine + '" stroke-width="4" style="stroke:var(--pink);color:var(--pink)" />';
  if (hasPss) {
    const rssLine = smoothLinePath(rss.map(function (v) { return v - floorV; }), w, h, padX, padY, spanV);
    lines += '<path class="chart-path" d="' + rssLine + '" stroke-width="3" style="stroke:var(--violet);color:var(--violet)" />';
  }

  const first = points[0];
  const middle = points[Math.floor((points.length - 1) / 2)];
  const last = points[points.length - 1];
  const axis = '<text class="chart-label" x="' + padX + '" y="' + (h - 7) + '">' + esc(fmtClock(first.ts)) + "</text>"
    + '<text class="chart-label" x="' + (w / 2).toFixed(1) + '" y="' + (h - 7) + '" text-anchor="middle">' + esc(fmtClock(middle.ts)) + "</text>"
    + '<text class="chart-label" x="' + (w - padX) + '" y="' + (h - 7) + '" text-anchor="end">' + esc(fmtClock(last.ts)) + "</text>";

  node.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="'
    + esc(t("trend.title", "内存足迹趋势")) + '">'
    + defs + grid + bars + lines + axis + '<g class="chart-hover"></g>'
    + "</svg><div class=\"chart-tooltip\"></div>";

  animateSvgPaths(node);
  bindTrendPointer(node, points, { w: w, h: h, padX: padX, padY: padY, hasPss: hasPss, yOf: yOf, barMax: barMax, inner: inner });
}

function bindTrendPointer(root, points, geom) {
  const svg = root.querySelector("svg");
  const tooltip = root.querySelector(".chart-tooltip");
  const hover = root.querySelector("g.chart-hover");
  if (!svg || !tooltip || !hover) return;
  const total = points.length;

  const move = function (event) {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const x = (event.clientX - rect.left) / rect.width * geom.w;
    const ratio = (x - geom.padX) / Math.max(1, geom.w - geom.padX * 2);
    const index = Math.max(0, Math.min(total - 1, Math.round(ratio * (total - 1))));
    const point = points[index];
    const px = geom.padX + index * (geom.w - geom.padX * 2) / Math.max(1, total - 1);

    while (hover.firstChild) hover.removeChild(hover.firstChild);
    const guide = document.createElementNS(SVG_NS, "line");
    guide.setAttribute("class", "chart-guide");
    guide.setAttribute("x1", px.toFixed(1));
    guide.setAttribute("y1", String(geom.padY));
    guide.setAttribute("x2", px.toFixed(1));
    guide.setAttribute("y2", String(geom.h - geom.padY));
    hover.appendChild(guide);

    const marks = [["--pink", point.footprint]];
    if (geom.hasPss) marks.push(["--violet", point.rss]);
    for (const mark of marks) {
      const dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("class", "chart-point");
      dot.setAttribute("cx", px.toFixed(1));
      dot.setAttribute("cy", geom.yOf(mark[1]).toFixed(1));
      dot.setAttribute("r", "6");
      // Presentation attributes cannot resolve var(); inline style can.
      dot.setAttribute("style", "fill:var(" + mark[0] + ");color:var(" + mark[0] + ")");
      hover.appendChild(dot);
    }

    const rows = [
      [t("trend.footprint", "内存足迹"), fmtBytes(point.footprint)],
      [t("trend.rss", "常驻 RSS"), fmtBytes(point.rss)],
      [t("trend.swap", "换出"), point.swap > 0 ? fmtBytes(point.swap) : "—"],
      [t("trend.retained", "归因保留"), point.retained > 0 ? fmtBytes(point.retained) : "—"],
      [t("trend.blocks", "分配块"), point.blocks > 0 ? fmtCount(point.blocks) : "—"],
    ];
    tooltip.innerHTML = '<div class="tooltip-date">' + esc(fmtTime(point.ts)) + "</div>"
      + rows.map(function (row) {
        return '<div class="tooltip-row"><span>' + esc(row[0]) + "</span><b>" + esc(row[1]) + "</b></div>";
      }).join("");
    tooltip.style.left = (px / geom.w * 100).toFixed(2) + "%";
    const localY = event.clientY - rect.top;
    tooltip.style.top = Math.max(60, Math.min(localY, rect.height - 20)).toFixed(0) + "px";
    tooltip.classList.add("show");
  };

  const leave = function () {
    while (hover.firstChild) hover.removeChild(hover.firstChild);
    tooltip.classList.remove("show");
  };

  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", leave);
  svg.addEventListener("pointercancel", leave);
}

function renderMiniChart(node, series, retainedSeries) {
  if (!node) return;
  const primary = sparkValues(series);
  const secondary = sparkValues(retainedSeries);
  if (primary.length < 2 && secondary.length < 2) {
    node.innerHTML = '<div class="empty">' + esc(t("detail.historyEmpty", "还没有这个插件的历史样本")) + "</div>";
    return;
  }
  const geom = chartGeometry(node);
  const w = geom.w;
  const h = geom.h;
  const padX = 34;
  const padY = 18;
  const all = primary.concat(secondary);
  let maxV = Math.max.apply(null, all);
  const minV = Math.min.apply(null, all);
  let floorV = maxV === minV ? Math.max(0, minV - Math.max(1, minV * 0.1)) : Math.max(0, minV - (maxV - minV) * 0.3);
  maxV = maxV * 1.08;
  const spanV = Math.max(1, maxV - floorV);
  let grid = "";
  for (const ratio of [0, 0.5, 1]) {
    const y = padY + ratio * (h - padY * 2);
    grid += '<line class="gridline" x1="' + padX + '" y1="' + y.toFixed(1) + '" x2="' + (w - padX) + '" y2="' + y.toFixed(1) + '" />'
      + '<text class="chart-label" x="3" y="' + (y + 4).toFixed(1) + '">' + esc(fmtBytes(floorV + (maxV - floorV) * (1 - ratio))) + "</text>";
  }
  let lines = "";
  if (primary.length >= 2) {
    const path = smoothLinePath(primary.map(function (v) { return v - floorV; }), w, h, padX, padY, spanV);
    lines += '<path class="chart-path" d="' + path + '" stroke-width="3" style="stroke:var(--pink);color:var(--pink)" />';
  }
  if (secondary.length >= 2) {
    const path = smoothLinePath(secondary.map(function (v) { return v - floorV; }), w, h, padX, padY, spanV);
    lines += '<path class="chart-path" d="' + path + '" stroke-width="2.4" style="stroke:var(--blue);color:var(--blue)" />';
  }
  node.innerHTML = '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-hidden="true">' + grid + lines + "</svg>";
  animateSvgPaths(node);
}

/* ------------------------------------------------------- overview: trend */

function legendItem(varName, label) {
  return '<span><i style="background:var(' + esc(varName) + ')"></i>' + esc(label) + "</span>";
}

function trendSummaryItem(label, value, detail) {
  return '<div class="trend-summary-item"><span>' + esc(label) + "</span><strong>" + esc(value)
    + '</strong><small title="' + esc(detail || "") + '">' + esc(detail || "") + "</small></div>";
}

function renderTrend() {
  const points = trendPoints();
  const trend = state.history || {};
  const process = reportProcess();
  const attribution = reportAttribution();
  const hasPss = points.some(function (point) { return point.pss > 0; });
  const hasRetained = points.some(function (point) { return point.retained > 0; });

  renderTrendChart(el("trend-chart"), points);

  const caption = el("trend-caption");
  if (caption) {
    if (points.length < 2) {
      caption.textContent = t("trend.captionEmpty",
        "采样线程每个周期写一个点，插件加载完成后才开始；等两轮之后这里会出现曲线。");
    } else {
      const window_ = points[points.length - 1].ts - points[0].ts;
      caption.textContent = fmtCount(points.length) + " " + t("trend.points", "个采样点")
        + " · " + t("trend.window", "窗口") + " " + fmtDuration(window_)
        + " · " + footprintLabel(process);
    }
  }

  const legend = el("trend-legend");
  if (legend) {
    const items = [legendItem("--pink", t("trend.footprint", "内存足迹"))];
    if (hasPss) items.push(legendItem("--violet", t("trend.rss", "常驻 RSS")));
    if (hasRetained) items.push(legendItem("--blue", t("trend.retainedBars", "归因保留（柱状 · 独立缩放）")));
    legend.innerHTML = items.join("");
  }

  const summary = el("trend-summary");
  if (!summary) return;
  if (!points.length) {
    summary.innerHTML = "";
    return;
  }
  const values = points.map(function (point) { return point.footprint; }).filter(function (v) { return v > 0; });
  const peak = values.length ? Math.max.apply(null, values) : null;
  const mean = values.length
    ? values.reduce(function (acc, v) { return acc + v; }, 0) / values.length
    : null;
  const peakPoint = peak === null
    ? null
    : points.filter(function (point) { return point.footprint === peak; })[0];
  const first = values.length ? values[0] : null;
  const last = values.length ? values[values.length - 1] : null;
  const delta = first === null || last === null ? null : last - first;
  const rate = known(trend.footprint_trend_bytes_per_minute)
    ? trend.footprint_trend_bytes_per_minute
    : trend.rss_trend_bytes_per_minute;

  summary.innerHTML = [
    trendSummaryItem(t("trend.peak", "窗口峰值"), fmtBytes(peak),
      peakPoint ? fmtClock(peakPoint.ts) : t("trend.noSample", "还没有样本")),
    trendSummaryItem(t("trend.mean", "窗口均值"), fmtBytes(mean),
      fmtCount(values.length) + " " + t("trend.validPoints", "个有效点")),
    trendSummaryItem(t("trend.delta", "窗口增量"), fmtSigned(delta), fmtRate(rate)),
    trendSummaryItem(t("trend.coverage", "归因覆盖率"),
      known(attribution.coverage_percent) ? fmtPercent(attribution.coverage_percent) : t("status.notRun", "未运行"),
      known(attribution.measured_bytes)
        ? fmtBytes(attribution.measured_bytes) + " / " + fmtBytes(attribution.private_dirty_bytes)
        : t("trend.coverageHint", "跑一次引用图扫描才有")),
  ].join("");
}

/* --------------------------------------------------------- overview: bars */

function paintBarWidths(node) {
  const fills = node.querySelectorAll(".bar-fill,.stack-seg");
  const apply = function () {
    for (const fill of fills) {
      if (fill.dataset.width !== undefined) fill.style.width = fill.dataset.width;
    }
  };
  if (reduceMotion) apply();
  else window.requestAnimationFrame(apply);
}

function renderBars(node, rows, valueKey, labelKey, emptyText, valueFormatter) {
  if (!node) return;
  if (valueFormatter === undefined) valueFormatter = fmtBytes;
  if (!rows.length) {
    node.innerHTML = '<div class="empty">' + esc(emptyText) + "</div>";
    return;
  }
  const scale = rows.map(function (row) { return num(row[valueKey]) || 0; });
  const max = Math.max.apply(null, [1].concat(scale));
  node.innerHTML = rows.map(function (row, index) {
    const value = num(row[valueKey]) || 0;
    const percent = Math.max(0, Math.min(100, value / max * 100));
    return '<div class="bar-row">'
      + '<div class="bar-label" title="' + esc(row[labelKey]) + '">' + esc(row[labelKey]) + "</div>"
      + '<div class="bar-track"><span class="bar-fill" data-width="' + percent.toFixed(2) + '%"'
      + ' style="width:0;--delay:' + (index * 40) + 'ms"></span></div>'
      + '<div class="bar-value">' + esc(valueFormatter(value)) + "</div>"
      + "</div>";
  }).join("");
  paintBarWidths(node);
}

function topBarSource() {
  // Accuracy order for the "who is using the memory" list: retained graph
  // share > census shallow size > import-window RSS delta.  Only the first one
  // reflects the live heap; the last one is a startup measurement.
  const totals = reportTotals();
  if (positive(totals.retained_bytes)) return "retained";
  if (censusReady()) return "census";
  return "import";
}

function renderTopBars() {
  const report = state.report || {};
  const source = topBarSource();
  const rows = (report.plugins || []).map(function (row) {
    let value = 0;
    if (source === "retained") value = num(row.retained_bytes) || 0;
    else if (source === "census") value = num(row.census_bytes) || 0;
    else if (row.import_measured) value = num(row.import_bytes) || 0;
    return { label: row.display_name || row.name, value: value };
  }).filter(function (row) { return row.value > 0; })
    .sort(function (a, b) { return b.value - a.value; })
    .slice(0, 8);

  const empty = source === "retained"
    ? t("chart.noRetainedRows", "引用图扫描还没有覆盖到任何插件")
    : source === "census"
      ? t("chart.noCensusRows", "普查已运行，但没有识别到插件对象")
      : t("chart.noImportRows", "没有可用的导入记录");
  renderBars(el("top-bars"), rows, "value", "label", empty);

  const caption = el("top-caption");
  if (caption) {
    caption.textContent = source === "retained"
      ? t("top.captionRetained", "按引用图保留量；共享对象已按 1/N 均摊")
      : source === "census"
        ? t("top.captionCensus", "按存活对象浅层大小")
        : t("top.captionImport", "按启动期 RSS 差分；不是当前独占占用");
  }
}

function renderBreakdown() {
  const node = el("breakdown-bars");
  const hint = el("breakdown-hint");
  const buckets = (state.report && state.report.census_buckets) || [];
  if (!censusReady()) {
    if (hint) hint.textContent = t("breakdown.notRun", "对象普查默认关闭；运行一次后才有来源分布。");
    if (node) node.innerHTML = '<div class="empty">' + esc(t("breakdown.censusEmpty", "尚未运行对象普查")) + "</div>";
    return;
  }
  if (hint) hint.textContent = t("breakdown.desc", "只统计被 GC 跟踪、且能按类型模块归类的对象，不等于 RSS。");
  renderBars(node, buckets.map(function (item) {
    return { label: t("bucket." + item.bucket, item.bucket), value: item.bytes };
  }), "value", "label", t("breakdown.empty", "没有其他对象"));
}

/* ------------------------------------------------- overview: probe status */

function renderProbeStatus() {
  const node = el("probe-status");
  if (!node) return;
  const process = reportProcess();
  const report = state.report || {};
  const hook = process.import_hook || {};
  const audit = report.audit_meta;
  const deep = report.deep_meta || {};
  const census = report.census_meta;
  const attribution = reportAttribution();
  const smaps = (state.overview && state.overview.smaps) || {};

  const hookValue = hook.degraded
    ? t("status.degraded", "已降级")
    : hook.installed
      ? t("status.running", "运行中")
      : hook.plugin_count
        ? t("status.done", "已完成")
        : t("status.off", "无记录");

  const smapsSupported = smaps.supported === undefined
    ? positive(process.pss_bytes) !== null
    : Boolean(smaps.supported);
  const smapsDetail = smapsSupported
    ? t("probe.smapsReads", "读取") + " " + fmtCount(smaps.reads, "0")
      + " · " + t("probe.smapsCost", "单次") + " " + fmtMs(smaps.elapsed_ms || process.rollup_elapsed_ms)
      + " · " + t("probe.smapsTtl", "最短间隔") + " " + fmtCount(smaps.min_interval_seconds, "—") + "s"
      + " · " + t("probe.smapsAge", "样本年龄") + " " + fmtCount(process.rollup_age_seconds, "—") + "s"
    : t("probe.smapsMissing", "内核没有 /proc/self/smaps_rollup，退回 RSS");

  const retainedValueText = known(attribution.measured_bytes) && attribution.plugin_count
    ? (attribution.truncated_count ? t("status.partial", "部分覆盖") : t("status.done", "已完成"))
    : t("status.notRun", "未运行");
  const retainedDetail = known(attribution.measured_bytes) && attribution.plugin_count
    ? fmtBytes(attribution.measured_bytes)
      + " · " + t("probe.retainedCoverage", "覆盖") + " "
      + (known(attribution.coverage_percent) ? fmtPercent(attribution.coverage_percent) : "—")
      + " · " + fmtCount(attribution.complete_count, "0") + "/" + fmtCount(attribution.plugin_count, "0")
      + " " + t("probe.plugins", "个插件完整")
      + " · " + fmtMs(attribution.work_ms || attribution.elapsed_ms)
    : t("probe.retainedIdle", "后台按轮转配额切片扫描；也可以点“引用图扫描”");

  node.innerHTML = [
    statusRow(t("probe.import", "导入成本"), hookValue,
      fmtCount(hook.plugin_count, "0") + " " + t("probe.plugins2", "个插件") + " · " + fmtMs(hook.overhead_ms),
      hook.degraded ? "warn" : hook.plugin_count ? "ok" : "neutral"),
    statusRow(t("probe.smaps", "smaps 足迹"),
      smapsSupported ? t("status.running", "运行中") : t("status.missing", "不可用"),
      smapsDetail, smapsSupported ? "ok" : "warn"),
    statusRow(t("probe.retained", "引用图保留"), retainedValueText, retainedDetail,
      attribution.truncated_count ? "warn" : attribution.plugin_count ? "ok" : "neutral"),
    statusRow(t("probe.census", "对象普查"),
      census ? (census.truncated ? t("status.truncated", "已截断") : t("status.done", "已完成")) : t("status.notRun", "未运行"),
      census ? fmtMs(census.elapsed_ms) + " · 1/" + fmtCount(census.sample_rate) : t("probe.manual", "仅手动执行"),
      census && census.truncated ? "warn" : census ? "ok" : "neutral"),
    statusRow(t("probe.audit", "依赖审计"),
      audit ? t("status.done", "已完成") : t("status.notRun", "未运行"),
      audit ? fmtCount(audit.audited) + "/" + fmtCount(audit.plugin_count) + " " + t("probe.plugins2", "个插件")
        : t("probe.manualFirst", "首次查看或手动执行"),
      audit ? "ok" : "neutral"),
    statusRow(t("probe.deep", "引用图扫描"),
      deep.generated_at
        ? (deep.truncated ? t("status.truncated", "已截断") : t("status.ready", "已有结果"))
        : t("status.notRun", "未运行"),
      deep.generated_at
        ? fmtTime(deep.generated_at) + " · " + fmtCount(deep.rounds, "0") + " " + t("probe.round", "轮")
        : t("probe.manual", "仅手动执行"),
      deep.truncated ? "warn" : deep.generated_at ? "ok" : "neutral"),
  ].join("");
}

/* ------------------------------------------------- overview: attribution */

function renderAttribution() {
  const attribution = reportAttribution();
  const process = reportProcess();
  const stack = el("attribution-stack");
  const grid = el("attribution-grid");
  const caption = el("attribution-caption");

  const exclusive = Math.max(0, num(attribution.exclusive_bytes) || 0);
  const shared = Math.max(0, num(attribution.shared_bytes) || 0);
  const measured = Math.max(0, num(attribution.measured_bytes) || 0);
  const floorBytes = positive(attribution.private_dirty_bytes)
    || positive(process.private_dirty_bytes)
    || positive(attribution.footprint_bytes)
    || footprintBytes(process)
    || 0;
  const uncovered = Math.max(0, floorBytes - measured);
  const total = Math.max(1, exclusive + shared + uncovered);

  if (caption) {
    caption.textContent = measured > 0
      ? t("attribution.desc",
        "引用图从每个插件对象出发走 gc.get_referents；只被一个插件引用的算独占，被多个插件共享的按 1/N 均摊。"
        + "剩下那截灰色是解释器内部、C 扩展 arena 和原生缓冲，Python 层面拿不到归属，所以覆盖率永远不到 100%。")
      : t("attribution.notRun", "引用图扫描还没有结果；后台会按轮转配额补齐，也可以点右上角“引用图扫描”。");
  }

  if (stack) {
    if (measured <= 0) {
      stack.innerHTML = "";
    } else {
      const segments = [
        ["--blue", exclusive, t("attribution.exclusive", "插件独占")],
        ["--violet", shared, t("attribution.shared", "共享均摊")],
        ["--line-strong", uncovered, t("attribution.uncovered", "未归因")],
      ];
      stack.innerHTML = segments.map(function (segment) {
        const percent = segment[1] / total * 100;
        if (percent <= 0) return "";
        return '<span class="stack-seg" data-width="' + percent.toFixed(2) + '%"'
          + ' style="width:0;background:var(' + esc(segment[0]) + ')"'
          + ' title="' + esc(segment[2] + " " + fmtBytes(segment[1]) + " (" + percent.toFixed(1) + "%)") + '"></span>';
      }).join("");
      paintBarWidths(stack);
    }
  }

  if (!grid) return;
  const method = attribution.method || t("status.notRun", "未运行");
  grid.innerHTML = [
    kv(t("attribution.method", "归因方法"), method),
    kv(t("attribution.coverage", "覆盖私有脏页"),
      known(attribution.coverage_percent) ? fmtPercent(attribution.coverage_percent) : "—"),
    kv(t("attribution.exclusive", "插件独占"), fmtBytes(positive(exclusive))),
    kv(t("attribution.shared", "共享均摊"), fmtBytes(positive(shared))),
    kv(t("attribution.complete", "完整 / 截断"),
      fmtCount(attribution.complete_count, "0") + " / " + fmtCount(attribution.truncated_count, "0")),
    kv(t("attribution.scanned", "已扫描对象"), fmtCount(attribution.scanned_objects, "0")),
  ].join("");
}

/* -------------------------------------------------------------- plugins */

function retainedValue(row) {
  if (!row) return null;
  const flat = num(row.retained_bytes);
  if (flat !== null && flat > 0) return flat;
  return row.retained ? num(row.retained.total_bytes) : null;
}

function rowTrend(row) {
  // Retained trend is the honest one when the graph scan has covered this
  // plugin; otherwise fall back to the census-based trend.
  const retained = num(row.retained_trend_bytes_per_minute);
  if (retained !== null && retainedValue(row) !== null) return retained;
  return num(row.trend_bytes_per_minute);
}

function rowSeries(row) {
  const retained = (state.history && state.history.retained_series_by_plugin) || {};
  const census = (state.history && state.history.series_by_plugin) || {};
  const primary = sparkValues(retained[row.name]);
  if (primary.length >= 2) return retained[row.name];
  return census[row.name];
}

function sortValue(row, key) {
  switch (key) {
    case "name": return String(row.display_name || row.name || "").toLowerCase();
    case "import": return num(row.import_bytes);
    case "census": return num(row.census_bytes);
    case "objects": return num(row.census_objects);
    case "retained": return retainedValue(row);
    case "trend": return rowTrend(row);
    default: return null;
  }
}

function compareRows(left, right) {
  const a = sortValue(left, state.sortKey);
  const b = sortValue(right, state.sortKey);
  const aMissing = a === null || a === undefined || a === "";
  const bMissing = b === null || b === undefined || b === "";
  if (aMissing !== bMissing) return aMissing ? 1 : -1;
  if (aMissing) return String(left.name || "").localeCompare(String(right.name || ""));
  let result;
  if (typeof a === "string" || typeof b === "string") result = String(a).localeCompare(String(b));
  else result = a === b ? 0 : a < b ? -1 : 1;
  return state.sortDir === "asc" ? result : -result;
}

function renderTableHead() {
  const node = el("plugin-head");
  if (!node) return;
  node.innerHTML = COLUMNS.map(function (column) {
    const sortable = column.sortable !== false;
    const active = sortable && state.sortKey === column.key;
    return "<th"
      + (sortable ? ' data-sort="' + esc(column.key) + '"' : "")
      + (active ? ' class="is-sorted" data-dir="' + esc(state.sortDir) + '"' : "")
      + ">" + esc(t(column.i18n, column.fallback)) + "</th>";
  }).join("");
}

function pluginNameCell(row) {
  const tags = [];
  if (row.is_self) tags.push('<span class="tag">' + esc(t("table.self", "本插件")) + "</span>");
  if (row.reserved) tags.push('<span class="tag">' + esc(t("table.reserved", "内置")) + "</span>");
  if (!row.activated) tags.push('<span class="tag">' + esc(t("table.inactive", "未启用")) + "</span>");
  return '<div class="name-cell"><div class="name-text"><strong>' + esc(row.display_name || row.name)
    + "</strong><small>" + esc(row.name) + "</small></div>" + tags.join("") + "</div>";
}

function renderTable() {
  renderTableHead();
  const body = el("plugin-body");
  if (!body) return;
  const allRows = ((state.report && state.report.plugins) || []).slice();
  const needle = state.search.trim().toLowerCase();
  const rows = allRows.filter(function (row) {
    if (!needle) return true;
    return [row.name, row.display_name, row.import_key].some(function (value) {
      return String(value || "").toLowerCase().includes(needle);
    });
  }).sort(compareRows);

  const count = el("plugin-count");
  if (count) count.textContent = rows.length + "/" + allRows.length + " " + t("metric.plugins", "个插件");

  const censusState = el("census-state");
  if (censusState) {
    censusState.textContent = censusReady()
      ? t("table.censusReady", "对象普查：已测量")
      : t("table.censusMissing", "对象普查：未运行");
    censusState.dataset.level = censusReady() ? "ok" : "warn";
  }

  const deepState = el("deep-state");
  if (deepState) {
    const meta = (state.report && state.report.deep_meta) || {};
    deepState.textContent = meta.generated_at
      ? t("table.deep", "引用图") + " · " + fmtTime(meta.generated_at)
      : t("table.deepMissing", "引用图：未运行");
    deepState.dataset.level = meta.generated_at && !meta.truncated ? "ok" : meta.truncated ? "warn" : "";
  }

  const hint = el("plugin-hint");
  if (hint) {
    const attribution = reportAttribution();
    hint.textContent = known(attribution.measured_bytes) && attribution.plugin_count
      ? t("table.retainedHint",
        "引用图保留量 = 该插件对象独占的字节 + 共享对象按 1/N 均摊的份额。它是分摊值，不是精确独占 RSS；点一行看细节。")
      : censusReady()
        ? t("table.censusHint", "对象普查是按类型模块归属的浅层估算；点击一行查看导入、对象类型和引用图结果。")
        : t("table.importHint",
          "导入成本只回答“启动时加载它大约增加了多少 RSS”，不能当作当前插件独占内存；等后台轮转扫描补齐引用图保留量。");
  }

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="' + COLUMNS.length + '" class="empty">'
      + esc(needle ? t("table.noMatch", "没有匹配的插件") : t("table.empty", "暂无插件数据"))
      + "</td></tr>";
    return;
  }

  body.innerHTML = rows.map(function (row) {
    const retained = row.retained;
    const retainedTotal = retainedValue(row);
    const retainedText = retained && retainedTotal !== null
      ? esc(fmtBytes(retainedTotal)) + "<small class=\"muted-block\">"
        + esc(fmtBytes(retained.exclusive_bytes) + " " + t("table.exclusiveShort", "独占")
          + " / " + fmtBytes(retained.shared_bytes) + " " + t("table.sharedShort", "均摊"))
        + (retained.truncated ? " " + esc(t("table.truncatedShort", "· 截断")) : "")
        + "</small>"
      : "—";
    const trend = fmtRate(rowTrend(row));
    const trendClass = trend.indexOf("+") === 0 ? " up" : trend.indexOf("−") === 0 ? " down" : "";
    const censusText = censusReady() ? fmtBytes(row.census_bytes, "0 B") : "—";
    const objectsText = censusReady() ? fmtCount(row.census_objects, "0") : "—";
    return '<tr data-name="' + esc(row.name) + '" tabindex="0" aria-label="' + esc(row.display_name || row.name) + '">'
      + '<td class="cell-name">' + pluginNameCell(row) + "</td>"
      + '<td class="num">' + esc(fmtBytes(row.import_bytes))
      + (row.import_measured ? "" : '<small class="muted-block">' + esc(t("status.unknown", "未知")) + "</small>")
      + "</td>"
      + '<td class="num">' + esc(censusText) + "</td>"
      + '<td class="num">' + esc(objectsText) + "</td>"
      + '<td class="num">' + retainedText + "</td>"
      + '<td class="num' + trendClass + '">' + esc(trend) + "</td>"
      + "<td>" + sparkline(rowSeries(row)) + "</td>"
      + "</tr>";
  }).join("");
}

/* --------------------------------------------------------------- imports */

function renderImports() {
  const report = state.report || {};
  const totals = report.totals || {};
  const hook = (report.process && report.process.import_hook) || {};

  const cards = el("import-cards");
  if (cards) {
    cards.innerHTML = [
      metricCard({
        tone: "pink",
        label: t("metric.totalImport", "导入账本"),
        bytes: totals.import_total_bytes,
        delta: esc(t("imports.accounting", "插件代码与首次加载的第三方包之和"))
          + "<br /><b>" + esc(t("metric.pluginCode", "插件代码") + " " + fmtBytes(totals.import_self_bytes_total)
            + " · " + t("metric.packages", "第三方包") + " " + fmtBytes(totals.packages_bytes)) + "</b>",
      }),
      metricCard({
        tone: "violet",
        label: t("metric.hookWindow", "记录窗口"),
        bytes: hook.rss_growth_bytes,
        delta: esc(hook.rss_source
          ? t("hero.source", "当前来源：") + hook.rss_source
          : t("status.unavailable", "RSS 不可用"))
          + "<br /><b>" + esc(t("metric.calls", "导入调用") + " " + fmtCount(hook.calls, "0")
            + " · " + t("metric.hookCost", "钩子开销") + " " + fmtMs(hook.overhead_ms)) + "</b>",
      }),
      metricCard({
        tone: "green",
        label: t("metric.pluginCoverage", "插件覆盖"),
        text: fmtCount(hook.plugin_count, "0"),
        delta: esc(t("imports.coverageSub", "只有钩子安装后首次加载的插件可测量"))
          + "<br /><b>" + esc(t("metric.packagesCount", "包数量") + " " + fmtCount(hook.package_count, "0")
            + " · " + (hook.degraded
              ? t("status.degraded", "已降级")
              : hook.installed ? t("status.running", "运行中") : t("status.finished", "已结束"))) + "</b>",
      }),
    ].join("");
  }

  const packageHint = el("package-hint");
  if (packageHint) {
    packageHint.textContent = t("imports.packageHint", "同一个包只记在第一个导入者名下，共用它的插件不会重复计费。");
  }

  const packageHead = el("package-head");
  if (packageHead) {
    packageHead.innerHTML = [
      t("col.package", "包"), t("col.bytes", "RSS 差分"),
      t("col.modules", "模块"), t("col.importer", "首个导入者"),
    ].map(function (value) { return "<th>" + esc(value) + "</th>"; }).join("");
  }

  const packageBody = el("package-body");
  const packages = report.packages || [];
  if (packageBody) {
    packageBody.innerHTML = packages.length
      ? packages.map(function (item) {
        return "<tr>"
          + '<td class="cell-name" title="' + esc(item.name) + '">' + esc(item.name) + "</td>"
          + '<td class="num">' + esc(fmtBytes(item.bytes)) + "</td>"
          + '<td class="num">' + esc(fmtCount(item.modules)) + "</td>"
          + "<td>" + esc(item.first_importer || "—") + "</td>"
          + "</tr>";
      }).join("")
      : '<tr><td colspan="4" class="empty">'
        + esc(t("imports.empty", "没有可显示的导入记录；重启后可覆盖更多插件。")) + "</td></tr>";
  }

  const importHead = el("import-plugin-head");
  if (importHead) {
    importHead.innerHTML = [
      t("col.plugin", "插件"), t("col.gross", "总成本"), t("col.self", "自身模块"),
      t("col.modules", "模块"), t("col.packages", "包数"),
    ].map(function (value) { return "<th>" + esc(value) + "</th>"; }).join("");
  }

  const importBody = el("import-plugin-body");
  const rows = (report.plugins || []).filter(function (row) { return row.import_measured; })
    .sort(function (a, b) { return (num(b.import_bytes) || 0) - (num(a.import_bytes) || 0); });
  if (importBody) {
    importBody.innerHTML = rows.length
      ? rows.map(function (row) {
        return '<tr data-name="' + esc(row.name) + '" tabindex="0">'
          + '<td class="cell-name">' + pluginNameCell(row) + "</td>"
          + '<td class="num">' + esc(fmtBytes(row.import_bytes)) + "</td>"
          + '<td class="num">' + esc(fmtBytes(row.import_self_bytes)) + "</td>"
          + '<td class="num">' + esc(fmtCount(row.import_modules)) + "</td>"
          + '<td class="num">' + esc(fmtCount((row.import_packages || []).length, "0")) + "</td>"
          + "</tr>";
      }).join("")
      : '<tr><td colspan="5" class="empty">' + esc(t("imports.emptyPlugins", "没有已测量的插件")) + "</td></tr>";
  }
}

/* ----------------------------------------------------------------- audit */

function renderAudit() {
  const report = state.report || {};
  const meta = report.audit_meta;

  const summary = el("audit-summary");
  if (summary) {
    if (!meta) {
      summary.textContent = t("audit.notRun", "尚未运行；打开这一页会自动建立一次缓存。");
    } else {
      const parts = [
        t("audit.scanned", "扫描") + " " + fmtCount(meta.audited) + "/" + fmtCount(meta.plugin_count),
        fmtMs(meta.elapsed_ms),
        t("audit.findings", "发现") + " " + fmtCount(meta.finding_count) + " " + t("audit.items", "处"),
      ];
      if (positive(meta.pending)) {
        parts.push(t("audit.pending", "还剩") + " " + fmtCount(meta.pending) + " "
          + t("audit.pendingTail", "个未扫，再跑一次会接着扫"));
      }
      summary.textContent = parts.join(" · ");
    }
  }

  const head = el("audit-head");
  if (head) {
    head.innerHTML = [
      t("col.module", "模块"), t("col.cost", "已知成本"),
      t("col.sharedBy", "共用插件"), t("col.loadStyle", "加载方式"),
    ].map(function (value) { return "<th>" + esc(value) + "</th>"; }).join("");
  }

  const body = el("audit-body");
  const rows = report.opportunities || [];
  if (body) {
    body.innerHTML = meta && rows.length
      ? rows.map(function (row) {
        const plugins = row.plugins || [];
        const tail = plugins.length > 4 ? " …" : "";
        return "<tr>"
          + '<td class="cell-name"><strong>' + esc(row.module) + "</strong><small>"
          + esc(plugins.slice(0, 4).join(", ") + tail) + "</small></td>"
          + '<td class="num">' + esc(fmtBytes(row.cost_bytes)) + "</td>"
          + '<td class="num">' + esc(fmtCount(row.shared_by)) + "</td>"
          + "<td>" + esc(positive(row.guarded)
            ? row.guarded + " " + t("audit.guarded", "处为可选导入")
            : t("audit.eager", "顶层加载")) + "</td>"
          + "</tr>";
      }).join("")
      : '<tr><td colspan="4" class="empty">' + esc(meta
        ? t("audit.empty", "没有找到带已知成本的重依赖")
        : t("audit.emptyNotRun", "尚未运行依赖审计")) + "</td></tr>";
  }

  const unknownNode = el("audit-unknown");
  const unknown = (meta && meta.unknown_modules) || [];
  if (unknownNode) {
    unknownNode.hidden = !unknown.length;
    unknownNode.textContent = unknown.length
      ? t("audit.unknown", "源码里还发现没有成本表的第三方模块：") + " " + unknown.slice(0, 24).join(", ")
      : "";
  }
}

/* ---------------------------------------------------------------- census */

function renderCensus() {
  const report = state.report || {};
  const meta = report.census_meta;

  const summary = el("census-summary");
  if (summary) {
    summary.textContent = meta
      ? t("census.scanned", "扫描") + " " + fmtCount(meta.scanned) + "/" + fmtCount(meta.total_objects)
        + " · " + fmtMs(meta.elapsed_ms)
        + " · " + (meta.truncated ? t("status.truncated", "已截断") : t("status.complete", "完成"))
      : t("census.notRun", "尚未运行。这是唯一一次会遍历整个 GC 对象列表的操作，默认关闭。");
  }

  const warning = el("census-warning");
  if (warning) {
    warning.textContent = meta
      ? (meta.scaled
        ? t("census.sampled", "当前是抽样估算，数值已按抽样率放大，适合比较比例而不是绝对值。")
        : t("census.limit", "只统计 GC 跟踪对象的浅层大小，不能与进程足迹直接相加。"))
      : t("census.warning", "运行前请确认可以接受一次短暂停顿；在有 swap 的小机器上，遍历会把换出的页面读回内存。");
  }

  const pluginRows = (report.plugins || [])
    .filter(function (row) { return censusReady() && positive(row.census_bytes) !== null; })
    .map(function (row) {
      return { label: truncate(row.display_name || row.name, 28), value: num(row.census_bytes) || 0 };
    })
    .sort(function (a, b) { return b.value - a.value; })
    .slice(0, 18);
  renderBars(el("census-plugin-bars"), pluginRows, "value", "label",
    t("census.emptyPlugins", "没有识别到归属插件的对象"));

  const bucketRows = (report.census_buckets || []).map(function (item) {
    return { label: t("bucket." + item.bucket, item.bucket), value: num(item.bytes) || 0 };
  });
  renderBars(el("census-bucket-bars"), bucketRows, "value", "label",
    t("census.emptyBuckets", "没有其他对象来源"));
}

/* ---------------------------------------------------------------- alerts */

function alertKind(kind) {
  const key = kind === "rss" ? "kind.rss"
    : kind === "rss_growth" ? "kind.rssGrowth"
      : kind === "size" ? "kind.size"
        : "kind.growth";
  return t("alerts." + key, kind || t("alerts.unknown", "告警"));
}

function alertValue(item) {
  /* Alert.value comes from the sampler in MB (or MB/hour for growth kinds), not bytes. */
  const value = num(item.value);
  if (value === null) return "—";
  const bytes = value * MB;
  return /growth/.test(String(item.kind)) ? fmtRate(bytes / 60) : fmtBytes(bytes);
}

function renderAlerts() {
  const count = state.alerts.length;

  const badge = el("alert-badge");
  if (badge) {
    badge.textContent = String(count);
    badge.hidden = !count;
  }

  const summary = el("alerts-summary");
  if (summary) {
    summary.textContent = state.alertsEnabled
      ? t("alerts.enabled", "告警已启用") + " · " + fmtCount(count) + " " + t("alerts.records", "条记录")
      : t("alerts.disabled", "还没有设置阈值；可以在插件配置里打开进程足迹或对象普查规则。");
  }

  const node = el("alerts");
  if (!node) return;
  if (!count) {
    node.innerHTML = '<div class="empty">' + esc(t("alerts.empty", "暂无告警")) + "</div>";
    return;
  }
  node.innerHTML = state.alerts.slice().reverse().map(function (item) {
    const who = item.plugin === "__process__" ? t("alerts.process", "进程") : item.plugin;
    return '<article class="alert" data-kind="' + esc(item.kind) + '">'
      + '<div class="alert-time">' + esc(fmtTime(item.ts)) + "</div>"
      + '<div class="alert-msg"><strong>' + esc(who) + "</strong>"
      + '<span class="tag">' + esc(alertKind(item.kind)) + "</span>"
      + "<p>" + esc(item.message) + "</p></div>"
      + '<div class="alert-value">' + esc(alertValue(item)) + "</div>"
      + "</article>";
  }).join("");
}

/* ------------------------------------------------------------ banner/help */

function renderBanner() {
  const banner = el("banner");
  if (!banner) return;
  const notes = (state.report && state.report.notes) || [];
  const quiet = { census_never_run: 1, dep_audit_never_run: 1, retained_never_run: 1 };
  const important = notes.filter(function (note) { return !quiet[note]; });
  const shown = important.length ? important : notes.slice(0, 1);
  if (!shown.length) {
    banner.hidden = true;
    return;
  }
  const level = shown.some(function (note) {
    return /truncated|degraded|missing|unavailable|partial/.test(note);
  }) ? "warn" : "info";
  banner.hidden = false;
  banner.dataset.level = level;
  const title = el("banner-title");
  if (title) title.textContent = level === "warn" ? t("banner.attention", "需要注意") : t("banner.note", "测量说明");
  const text = el("banner-text");
  if (text) {
    text.textContent = shown.slice(0, 3).map(function (note) {
      return t("note." + note, NOTE_FALLBACK[note] || note);
    }).join(" ");
  }
}

function renderHelp() {
  const node = el("help-list");
  if (node) {
    const items = [
      ["footprint", "help.footprint", "内存足迹"],
      ["rss", "help.rss", "常驻 RSS"],
      ["retained", "help.retained", "引用图保留"],
      ["import", "help.import", "导入成本"],
      ["census", "help.census", "对象普查"],
      ["trend", "help.trend", "增长趋势"],
      ["cgroup", "help.cgroup", "容器额度"],
    ];
    node.innerHTML = items.map(function (item) {
      return '<div class="help-item"><dt>' + esc(t("help.label." + item[0], item[2])) + "</dt>"
        + "<dd>" + esc(t(item[1], "暂无说明")) + "</dd></div>";
    }).join("");
  }
  const safety = el("safety-list");
  if (safety) {
    const items = ["noTracemalloc", "cheapProbes", "keepCensusOff", "watchSwap", "useRssAlert", "runDeepManually"];
    safety.innerHTML = items.map(function (key) {
      return "<li>" + esc(t("help.safety." + key, "—")) + "</li>";
    }).join("");
  }
}

/* ------------------------------------------------------------- orchestration */

function renderAll() {
  applyStaticText();
  renderBanner();
  renderHero();
  renderMetrics();
  renderTrend();
  renderTopBars();
  renderProbeStatus();
  renderAttribution();
  renderBreakdown();
  renderTable();
  renderImports();
  renderAudit();
  renderCensus();
  renderAlerts();
  renderHelp();
  if (state.drawerPayload) renderDetail(state.drawerPayload);
}

function setReport(report) {
  if (!report || typeof report !== "object") return;
  state.report = report;
}

async function refresh(options) {
  const opts = options || {};
  if (state.busy && !opts.allowBusy) return;
  setAction("refresh");
  try {
    // A normal refresh must stay cheap: one /proc read plus cached probe
    // results.  The expensive one-off probes are explicit buttons, and the
    // backend keeps their last result so the panels stay populated.
    const params = { sample: "1", census: "0", audit: "0" };
    if (opts.deep) params.deep = "1";
    setReport(await apiGet("plugins", params));
    const rest = await Promise.all([
      apiGet("overview"),
      apiGet("history", { limit: 120 }),
      apiGet("alerts", { limit: ALERT_LIMIT }),
    ]);
    state.overview = rest[0] || null;
    state.history = rest[1] || null;
    state.alerts = (rest[2] && rest[2].alerts) || [];
    state.alertsEnabled = Boolean(rest[2] && rest[2].enabled);
    renderAll();
    if (!opts.silent) {
      toast(opts.deep ? t("toast.deepDone", "引用图扫描完成") : t("toast.refreshed", "已刷新"));
    }
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

/* ---------------------------------------------------------------- actions */

async function runCensus() {
  if (typeof window.confirm === "function"
    && !window.confirm(t("confirm.census", "对象普查会遍历整个 GC 对象列表，可能造成短暂停顿，并把换出的页面读回内存。继续吗？"))) return;
  setAction("census");
  try {
    await apiPost("census", {});
    toast(t("toast.censusDone", "对象普查完成"));
    gotoTab("census");
    await refresh({ silent: true, allowBusy: true });
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

async function runAudit() {
  setAction("audit");
  try {
    await apiPost("audit", {});
    toast(t("toast.auditDone", "依赖审计完成"));
    gotoTab("audit");
    await refresh({ silent: true, allowBusy: true });
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

async function runDeep() {
  if (typeof window.confirm === "function"
    && !window.confirm(t("confirm.deep", "引用图扫描会遍历插件可达对象，通常几秒内完成，期间会占用一点 CPU。继续吗？"))) return;
  await refresh({ deep: true });
}

async function forceGc() {
  setAction("gc");
  try {
    const result = await apiPost("gc", {});
    toast(t("toast.gcDone", "GC 完成") + " · " + fmtCount(result && result.collected)
      + " · " + fmtBytes(result && result.rss_before) + " → " + fmtBytes(result && result.rss_after));
    await refresh({ silent: true, allowBusy: true });
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

async function setBaseline(action) {
  setAction("baseline");
  try {
    await apiPost("baseline", { action: action });
    toast(action === "clear" ? t("toast.baselineCleared", "已清除基线") : t("toast.baselineSet", "已设为基线"));
    await refresh({ silent: true, allowBusy: true });
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

/* ----------------------------------------------------------------- drawer */

function closeDrawer() {
  state.drawerName = null;
  state.drawerPayload = null;
  const drawer = el("drawer");
  if (drawer) drawer.hidden = true;
}

function detailSection(title, content) {
  return '<section class="detail-section"><h3>' + esc(title) + "</h3>" + content + "</section>";
}

function monoRow(left, middle, right) {
  return '<div class="mono-row"><span>' + left + "</span><span>" + esc(middle) + "</span><span>"
    + esc(right || "") + "</span></div>";
}

function renderDetail(payload) {
  const drawer = el("drawer");
  const node = el("drawer-content");
  if (!drawer || !node || !payload) return;
  const detail = payload.detail || payload;
  if (!detail.found) {
    node.innerHTML = '<div class="empty">' + esc(t("detail.notFound", "找不到这个插件")) + "</div>";
    return;
  }
  const row = detail.row || {};
  const importInfo = detail.import;
  const census = detail.census;
  const retained = row.retained;
  const audit = detail.audit;
  const packages = detail.import_packages || [];

  const retainedContent = retained
    ? '<div class="detail-grid">'
      + kv(t("detail.retainedTotal", "分摊后合计"), fmtBytes(retained.total_bytes))
      + kv(t("detail.exclusive", "仅此插件可达"), fmtBytes(retained.exclusive_bytes))
      + kv(t("detail.shared", "与其他插件共享"), fmtBytes(retained.shared_bytes))
      + kv(t("detail.sharedFull", "共享对象原始大小"), fmtBytes(retained.shared_full_bytes))
      + kv(t("detail.retainedObjects", "对象数"),
        fmtCount((num(retained.exclusive_objects) || 0) + (num(retained.shared_objects) || 0)))
      + kv(t("detail.scanned", "本轮扫描对象"), fmtCount(retained.scanned_objects))
      + "</div>"
      + '<p class="muted">' + esc(t("detail.sharedNote", "共享对象按共用它的插件数量均摊，所以各插件之和不会超过真实占用。")) + "</p>"
      + (retained.truncated
        ? '<p class="callout callout-warn">' + esc(t("detail.truncated", "扫描在配额上限处截断，数值是下界。")) + "</p>"
        : "")
    : '<p class="muted-block">' + esc(t("detail.retainedMissing", "还没扫到这个插件；后台是轮转配额，等几轮或手动点“引用图扫描”。")) + "</p>";

  const importContent = importInfo
    ? '<div class="detail-grid">'
      + kv(t("detail.total", "总成本"), fmtBytes(importInfo.bytes))
      + kv(t("detail.self", "自身模块"), fmtBytes(importInfo.self_bytes))
      + kv(t("detail.wall", "耗时"), fmtMs(importInfo.wall_ms))
      + kv(t("detail.modules", "模块"), fmtCount(importInfo.modules))
      + "</div>"
    : '<p class="muted-block">' + esc(t("detail.importMissing", "这个插件在钩子安装之前就加载完了，导入成本未知。")) + "</p>";

  const censusContent = census
    ? '<div class="detail-grid">'
      + kv(t("detail.bytes", "浅层大小"), fmtBytes(census.bytes))
      + kv(t("detail.objects", "对象数"), fmtCount(census.objects))
      + kv(t("detail.types", "类型数"), fmtCount(census.type_count))
      + "</div>"
      + '<div class="mono-list">' + (census.types || []).slice(0, 12).map(function (item) {
        return monoRow(esc(item.type), fmtBytes(item.bytes), fmtCount(item.objects));
      }).join("") + "</div>"
    : '<p class="muted-block">' + esc(t("detail.censusMissing", "尚未对这个进程运行对象普查。")) + "</p>";

  const packageContent = packages.length
    ? '<div class="mono-list">' + packages.slice(0, 20).map(function (item) {
      return monoRow(esc(item.name), fmtBytes(item.bytes), fmtMs(item.wall_ms));
    }).join("") + "</div>"
    : '<p class="muted-block">' + esc(t("detail.packagesEmpty", "没有记录到由它首次加载的第三方包。")) + "</p>";

  const auditImports = (audit && audit.imports) || [];
  const auditContent = audit
    ? (auditImports.length
      ? '<div class="mono-list">' + auditImports.map(function (item) {
        return monoRow(esc(item.module) + " <small>" + esc(item.file) + ":" + esc(item.lineno) + "</small>",
          fmtBytes(item.cost_bytes), item.guarded ? t("audit.guardedShort", "可选") : "");
      }).join("") + "</div>"
      : '<p class="muted-block">' + esc(t("detail.auditEmpty", "没有识别到重依赖。")) + "</p>")
    : '<p class="muted-block">' + esc(t("detail.auditMissing", "尚未建立依赖审计结果。")) + "</p>";

  const header = el("drawer-title");
  if (header) {
    header.textContent = row.display_name || detail.name;
    header.title = t("detail.copyHint", "点击标题复制这份 JSON");
  }

  node.innerHTML = '<div class="detail-meta">' + esc(detail.name) + " · " + esc(row.version || "—")
      + " · " + esc(row.author || "—") + "</div>"
    + detailSection(t("detail.retainedTitle", "引用图保留量"), retainedContent)
    + detailSection(t("detail.historyTitle", "历史曲线"),
      '<div class="chart chart-small" id="detail-chart"></div>'
      + '<p class="muted">' + esc(t("detail.historyLegend", "粉线：对象普查浅层大小 · 蓝线：引用图保留量")) + "</p>")
    + detailSection(t("detail.importTitle", "加载期导入成本"), importContent)
    + detailSection(t("detail.censusTitle", "对象普查"), censusContent)
    + detailSection(t("detail.packageTitle", "由它首次加载的第三方包"), packageContent)
    + detailSection(t("detail.auditTitle", "顶层依赖审计"), auditContent)
    + '<p class="muted">' + esc(t("detail.submodules", "已加载子模块")) + ": "
      + esc((detail.submodules || []).slice(0, 20).join(", ") || "—") + "</p>";

  renderMiniChart(el("detail-chart"), detail.series, detail.retained_series);
}

async function openDetail(name) {
  const drawer = el("drawer");
  const node = el("drawer-content");
  if (!drawer || !node) return;
  state.drawerName = name;
  state.drawerPayload = null;
  drawer.hidden = false;
  node.innerHTML = '<div class="empty">' + esc(t("status.loading", "加载中…")) + "</div>";
  try {
    const payload = await apiGet("detail", { name: name, deep: "0", census: "0", audit: "0" });
    if (state.drawerName !== name) return;
    state.drawerPayload = payload;
    renderDetail(payload);
  } catch (error) {
    if (state.drawerName === name) {
      node.innerHTML = '<div class="callout callout-warn">' + esc(errorText(error)) + "</div>";
    }
  }
}

async function copyDetail() {
  if (!state.drawerPayload) return;
  const text = JSON.stringify(state.drawerPayload.detail || state.drawerPayload, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    toast(t("toast.copied", "已复制"));
  } catch (_error) {
    toast(t("toast.copyFailed", "复制失败"), "error");
  }
}

/* ------------------------------------------------------------------ events */

function debounce(fn, wait) {
  let timer = null;
  return function () {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      timer = null;
      fn();
    }, wait);
  };
}

function gotoTab(name) {
  setTab(name);
  // The trend SVG is sized from clientWidth, which is 0 while the panel is
  // display:none.  Re-render once the browser has laid the panel out.
  if (state.tab === "overview") window.requestAnimationFrame(renderTrend);
}

function stopAuto() {
  if (state.autoTimer) window.clearInterval(state.autoTimer);
  state.autoTimer = null;
}

function startAuto() {
  stopAuto();
  if (!state.auto) return;
  state.autoTimer = window.setInterval(function () {
    if (!document.hidden && !state.busy) refresh({ silent: true });
  }, state.auto * 1000);
}

function bindEvents() {
  for (const button of document.querySelectorAll(".nav-btn")) {
    button.addEventListener("click", function () { gotoTab(button.dataset.tab); });
  }

  const on = function (id, event, handler) {
    const node = el(id);
    if (node) node.addEventListener(event, handler);
  };

  on("btn-refresh", "click", function () { refresh({}); });
  on("btn-deep", "click", runDeep);
  on("btn-census", "click", runCensus);
  on("btn-audit", "click", runAudit);
  on("btn-baseline-set", "click", function () { setBaseline("set"); });
  on("btn-baseline-clear", "click", function () { setBaseline("clear"); });
  on("btn-gc", "click", forceGc);
  on("drawer-close", "click", closeDrawer);
  on("drawer-title", "click", copyDetail);
  on("drawer", "click", function (event) {
    if (event.target.closest("[data-close]")) closeDrawer();
  });

  on("plugin-head", "click", function (event) {
    const cell = event.target.closest("th[data-sort]");
    if (!cell) return;
    const key = cell.dataset.sort;
    if (state.sortKey === key) state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
    else {
      state.sortKey = key;
      state.sortDir = key === "name" ? "asc" : "desc";
    }
    writeStore(STORE_SORT, state.sortKey + ":" + state.sortDir);
    renderTable();
  });

  const openRow = function (event) {
    const row = event.target.closest("tr[data-name]");
    if (row) openDetail(row.dataset.name);
  };
  on("plugin-body", "click", openRow);
  on("import-plugin-body", "click", openRow);
  on("plugin-body", "keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("tr[data-name]");
    if (!row) return;
    event.preventDefault();
    openDetail(row.dataset.name);
  });

  let searchTimer = null;
  on("search", "input", function (event) {
    state.search = event.target.value || "";
    if (searchTimer) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(renderTable, 120);
  });

  on("skin-select", "change", function (event) {
    state.skin = SKIN_IDS.includes(event.target.value) ? event.target.value : "auto";
    writeStore(STORE_SKIN, state.skin);
    applySkin();
  });

  on("auto-select", "change", function (event) {
    const value = Number(event.target.value);
    state.auto = AUTO_INTERVALS.includes(value) ? value : 0;
    writeStore(STORE_AUTO, String(state.auto));
    startAuto();
  });

  document.addEventListener("keydown", function (event) {
    const drawer = el("drawer");
    if (event.key === "Escape" && drawer && !drawer.hidden) closeDrawer();
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && state.auto && !state.busy) refresh({ silent: true });
  });
  window.addEventListener("resize", debounce(function () {
    if (state.tab === "overview") renderTrend();
    if (state.drawerPayload) {
      const detail = state.drawerPayload.detail || state.drawerPayload;
      renderMiniChart(el("detail-chart"), detail.series, detail.retained_series);
    }
  }, 200));
  window.addEventListener("beforeunload", stopAuto);
}

async function main() {
  if (!bridge) {
    document.body.textContent = "MemoryScope 需要在 AstrBot Dashboard 的插件页内打开。";
    return;
  }
  if (typeof bridge.ready === "function") await bridge.ready();

  state.skin = readStore(STORE_SKIN, "auto");
  if (!SKIN_IDS.includes(state.skin)) state.skin = "auto";
  state.auto = Number(readStore(STORE_AUTO, "0")) || 0;
  if (!AUTO_INTERVALS.includes(state.auto)) state.auto = 0;
  const savedSort = String(readStore(STORE_SORT, "retained:desc")).split(":");
  if (SORT_KEYS.includes(savedSort[0])) {
    state.sortKey = savedSort[0];
    state.sortDir = savedSort[1] === "asc" ? "asc" : "desc";
  }

  applyStaticText();
  applySkin();
  bindEvents();
  gotoTab(readStore(STORE_TAB, "overview"));
  renderHelp();

  const contextHandler = function () {
    applySkin();
    applyStaticText();
    renderAll();
  };
  try {
    if (typeof bridge.onContext === "function") bridge.onContext(contextHandler);
    else if (typeof bridge.onContextChange === "function") bridge.onContextChange(contextHandler);
  } catch (_error) {
    // Older Dashboard builds ship no context listener; the static skin is fine.
  }

  await refresh({ silent: true });
  startAuto();
}

main().catch(function (error) { toast(errorText(error), "error"); });
