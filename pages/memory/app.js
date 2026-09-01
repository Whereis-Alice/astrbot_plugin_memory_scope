// MemoryScope dashboard page: deliberately lightweight, dependency-free UI.
// The page never instruments Python allocations.  It only displays process
// counters, short-lived import measurements, and explicitly requested scans.

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
  sortKey: "census",
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

function fmtBytes(value, missing = "—") {
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

function fmtSigned(value, missing = "—") {
  const parsed = num(value);
  if (parsed === null) return missing;
  if (Math.abs(parsed) < KB) return "≈0";
  return (parsed > 0 ? "+" : "−") + fmtBytes(Math.abs(parsed));
}

function fmtRate(bytesPerMinute, missing = "—") {
  const parsed = num(bytesPerMinute);
  if (parsed === null) return missing;
  const perHour = parsed * 60;
  if (Math.abs(perHour) < 64 * KB) return "≈0";
  return (perHour > 0 ? "+" : "−") + fmtBytes(Math.abs(perHour)) + t("unit.perHour", "/小时");
}

function fmtCount(value, missing = "—") {
  const parsed = num(value);
  if (parsed === null) return missing;
  try {
    return Math.round(parsed).toLocaleString();
  } catch (_error) {
    return String(Math.round(parsed));
  }
}

function fmtPercent(value, digits = 1, missing = "—") {
  const parsed = num(value);
  return parsed === null ? missing : parsed.toFixed(digits) + "%";
}

function fmtMs(value, missing = "—") {
  const parsed = num(value);
  if (parsed === null) return missing;
  return parsed >= 1000 ? (parsed / 1000).toFixed(2) + " s" : Math.round(parsed) + " ms";
}

function fmtTime(value, missing = "—") {
  const parsed = num(value);
  if (parsed === null || parsed <= 0) return missing;
  try {
    return new Date(parsed * 1000).toLocaleString();
  } catch (_error) {
    return missing;
  }
}

function fmtDuration(value, missing = "—") {
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

function truncate(value, max = 80) {
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

function toast(message, kind = "ok") {
  const node = el("toast");
  if (!node) return;
  node.textContent = String(message);
  node.dataset.kind = kind === "error" ? "error" : "ok";
  node.hidden = false;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => { node.hidden = true; }, kind === "error" ? 7000 : 2800);
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

async function apiGet(endpoint, params = {}) {
  if (!bridge || typeof bridge.apiGet !== "function") throw new Error(t("error.bridge", "页面桥接不可用"));
  return unwrap(await bridge.apiGet(endpoint, params));
}

async function apiPost(endpoint, body = {}) {
  if (!bridge || typeof bridge.apiPost !== "function") throw new Error(t("error.bridge", "页面桥接不可用"));
  return unwrap(await bridge.apiPost(endpoint, body));
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
    skin.innerHTML = SKINS.map((item) => `<option value="${esc(item.id)}">${esc(t("skin." + item.id, item.label))}</option>`).join("");
    skin.value = state.skin;
  }
  const auto = el("auto-select");
  if (auto) {
    auto.innerHTML = AUTO_INTERVALS.map((seconds) => {
      const label = seconds === 0 ? t("auto.off", "关闭") : seconds + "s";
      return `<option value="${seconds}">${esc(label)}</option>`;
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
  if (desc) desc.textContent = t("desc", "用轻量探针观察进程、插件导入成本与运行时对象");
  const autoLabel = el("auto-label");
  if (autoLabel) autoLabel.textContent = t("auto.label", "自动刷新");
  const skinLabel = el("skin-label");
  if (skinLabel) skinLabel.textContent = t("skin.label", "主题");
  buildSelects();
}

function setTab(name) {
  state.tab = TABS.includes(name) ? name : "overview";
  writeStore(STORE_TAB, state.tab);
  for (const button of document.querySelectorAll(".ms-tab")) {
    const active = button.dataset.tab === state.tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".ms-panel")) {
    panel.classList.toggle("is-active", panel.id === "panel-" + state.tab);
  }
}

function setAction(action) {
  state.action = action;
  state.busy = Boolean(action);
  const buttons = document.querySelectorAll("button");
  for (const button of buttons) {
    if (button.id === "drawer-close") continue;
    button.disabled = Boolean(action) && button.id !== "btn-refresh";
  }
  const refresh = el("btn-refresh");
  if (refresh) refresh.disabled = Boolean(action) && action !== "refresh";
}

function statusRow(label, value, detail, level = "neutral") {
  return `<div class="ms-status-row" data-level="${esc(level)}">
    <div class="ms-status-main"><span class="ms-status-dot"></span><span>${esc(label)}</span></div>
    <strong>${esc(value)}</strong>
    <small>${esc(detail || "")}</small>
  </div>`;
}

function card(title, lead, subtitle, body = "", level = "neutral") {
  return `<article class="ms-card ms-metric-card" data-level="${esc(level)}">
    <div class="ms-card-head"><h2>${esc(title)}</h2></div>
    <div class="ms-card-lead">${lead}</div>
    <div class="ms-card-sub">${subtitle || ""}</div>
    ${body ? `<div class="ms-card-body">${body}</div>` : ""}
  </article>`;
}

function kv(label, value) {
  return `<div class="ms-kv-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}

function reportProcess() {
  return (state.report && state.report.process) || (state.overview && state.overview.process) || {};
}

function reportTotals() {
  return (state.report && state.report.totals) || {};
}

function censusReady() {
  return Boolean(state.report && state.report.census_meta);
}

function renderCards() {
  const node = el("cards");
  if (!node) return;
  const process = reportProcess();
  const totals = reportTotals();
  const history = (state.report && state.report.history) || (state.overview && state.overview.history) || {};
  const hook = process.import_hook || {};
  const rss = process.rss_bytes;
  const rssDelta = process.rss_delta_bytes !== undefined
    ? process.rss_delta_bytes
    : state.history && state.history.rss_delta_bytes;
  const gc = process.gc || {};
  const cgroup = process.cgroup_limit_bytes
    ? fmtPercent(process.cgroup_memory_percent)
    : t("status.unavailable", "未提供");
  const rssBody = [
    kv(t("metric.vms", "虚拟内存"), fmtBytes(process.vms_bytes)),
    kv(t("metric.swap", "已换出"), fmtBytes(process.swap_bytes)),
    kv(t("metric.baselineDelta", "相对基线"), fmtSigned(rssDelta)),
  ].join("");
  const compositionBody = [
    kv(t("metric.anon", "匿名页"), fmtBytes(process.rss_anon_bytes)),
    kv(t("metric.file", "文件页"), fmtBytes(process.rss_file_bytes)),
    kv(t("metric.shmem", "共享内存"), fmtBytes(process.rss_shmem_bytes)),
    kv(t("metric.cgroup", "容器额度"), cgroup),
  ].join("");
  const importBody = [
    kv(t("metric.pluginCode", "插件代码"), fmtBytes(totals.import_self_bytes_total, "—")),
    kv(t("metric.packages", "第三方包"), fmtBytes(totals.packages_bytes, "—")),
    kv(t("metric.hookCost", "钩子开销"), fmtMs(hook.overhead_ms)),
  ].join("");
  const censusBody = [
    kv(t("metric.censusObjects", "对象数"), censusReady() ? fmtCount(totals.census_objects) : t("status.notRun", "未运行")),
    kv(t("metric.censusTime", "扫描耗时"), censusReady() ? fmtMs(state.report.census_meta.elapsed_ms) : "—"),
    kv(t("metric.censusRate", "抽样率"), censusReady() ? "1/" + fmtCount(state.report.census_meta.sample_rate) : "—"),
  ].join("");
  const pluginCount = num(totals.plugin_count);
  const measured = num(totals.measured_plugin_count);
  const pluginSub = pluginCount === null
    ? t("status.unavailable", "未提供")
    : `${fmtCount(measured, "0")} ${t("metric.measured", "有导入记录")} / ${fmtCount(pluginCount)} ${t("metric.plugins", "个插件")}`;
  const gcSub = `${t("metric.threads", "线程")} ${fmtCount(process.threads)} · ${t("metric.modules", "模块")} ${fmtCount(process.modules)}`;
  node.innerHTML = [
    card(t("card.rss", "进程 RSS"), esc(fmtBytes(rss)),
      `${esc(fmtPercent(process.memory_percent, 1, "—"))} ${esc(t("metric.systemShare", "占系统"))} · ${esc(fmtDuration(process.uptime_seconds))}`,
      rssBody, rss !== null && num(rss) >= 700 * MB ? "warn" : "neutral"),
    card(t("card.composition", "内存构成"), esc(fmtBytes(process.rss_anon_bytes)),
      esc(t("card.compositionSub", "匿名页是 RSS 的主体；换出页另计")), compositionBody),
    card(t("card.import", "加载期导入"), esc(fmtBytes(totals.import_total_bytes, "—")),
      esc(hook.installed ? t("card.hookRunning", "导入记录仍在进行") : t("card.hookDone", "启动期记录已结束")), importBody),
    card(t("card.census", "对象普查"), esc(censusReady() ? fmtBytes(totals.census_bytes) : t("status.notRun", "未运行")),
      esc(censusReady() ? t("card.censusMeasured", "按类型模块归属估算") : t("card.censusOff", "默认关闭，避免扫描大堆")), censusBody,
      censusReady() && state.report.census_meta.truncated ? "warn" : "neutral"),
    card(t("card.plugins", "插件清单"), esc(fmtCount(pluginCount)), esc(pluginSub),
      kv(t("metric.importCoverage", "导入覆盖"), pluginCount ? fmtPercent(measured * 100 / pluginCount) : "—") +
      kv(t("metric.history", "历史采样"), fmtCount(history.samples)), "neutral"),
    card(t("card.runtime", "运行状态"), esc(fmtCount(process.threads)), esc(gcSub),
      kv(t("metric.gcCounts", "GC 代计数"), (gc.counts || []).join(" / ") || "—") +
      kv(t("metric.uncollectable", "不可回收"), fmtCount(gc.uncollectable)), "neutral"),
  ].join("");
}

function linePath(points, width, height, margin, minValue, maxValue) {
  const span = Math.max(1, maxValue - minValue);
  const time0 = points[0][0];
  const time1 = points[points.length - 1][0];
  const timeSpan = Math.max(1, time1 - time0);
  return points.map((point, index) => {
    const x = margin.left + ((point[0] - time0) / timeSpan) * (width - margin.left - margin.right);
    const y = height - margin.bottom - ((point[1] - minValue) / span) * (height - margin.top - margin.bottom);
    return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function renderLineChart(points, options = {}) {
  const valid = (points || []).map((point) => [num(point[0]), num(point[1])])
    .filter((point) => point[0] !== null && point[1] !== null);
  const node = options.node;
  if (!node) return;
  if (!valid.length) {
    node.innerHTML = `<div class="ms-empty">${esc(options.empty || t("chart.empty", "暂无历史数据"))}</div>`;
    return;
  }
  const width = 760;
  const height = options.height || 220;
  const margin = { top: 18, right: 18, bottom: 32, left: 58 };
  let minValue = Math.min(...valid.map((point) => point[1]));
  let maxValue = Math.max(...valid.map((point) => point[1]));
  if (minValue === maxValue) {
    minValue = Math.max(0, minValue - MB);
    maxValue += MB;
  } else {
    const pad = (maxValue - minValue) * 0.08;
    minValue = Math.max(0, minValue - pad);
    maxValue += pad;
  }
  const path = linePath(valid, width, height, margin, minValue, maxValue);
  const baseline = num(options.baseline);
  const innerHeight = height - margin.top - margin.bottom;
  const baselineY = baseline === null ? null : height - margin.bottom - ((baseline - minValue) / Math.max(1, maxValue - minValue)) * innerHeight;
  const first = valid[0];
  const last = valid[valid.length - 1];
  const grid = [0, 0.5, 1].map((ratio) => {
    const y = margin.top + ratio * innerHeight;
    const value = maxValue - ratio * (maxValue - minValue);
    return `<line class="ms-chart-grid" x1="${margin.left}" y1="${y.toFixed(1)}" x2="${width - margin.right}" y2="${y.toFixed(1)}" />
      <text class="ms-chart-label" x="${margin.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${esc(fmtBytes(value))}</text>`;
  }).join("");
  const baselineLine = baselineY === null || baselineY < margin.top - 2 || baselineY > height - margin.bottom + 2
    ? ""
    : `<line class="ms-chart-baseline" x1="${margin.left}" y1="${baselineY.toFixed(1)}" x2="${width - margin.right}" y2="${baselineY.toFixed(1)}" />`;
  node.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(options.label || "RSS")}">
    ${grid}${baselineLine}
    <path class="ms-chart-line" d="${path}" />
    <circle class="ms-chart-point" cx="${(width - margin.right).toFixed(1)}" cy="${(height - margin.bottom - ((last[1] - minValue) / Math.max(1, maxValue - minValue)) * innerHeight).toFixed(1)}" r="3" />
    <text class="ms-chart-label" x="${margin.left}" y="${height - 8}">${esc(fmtTime(first[0]))}</text>
    <text class="ms-chart-label" x="${width - margin.right}" y="${height - 8}" text-anchor="end">${esc(fmtTime(last[0]))}</text>
  </svg>`;
}

function renderRssChart() {
  const history = state.history || {};
  renderLineChart(history.rss || [], {
    node: el("rss-chart"),
    baseline: history.baseline_rss_bytes,
    label: t("chart.rssTitle", "进程 RSS 趋势"),
    empty: t("chart.empty", "暂无历史数据；后台采样后会出现曲线。"),
  });
  const caption = el("rss-caption");
  if (caption) {
    const trend = history.rss_trend_bytes_per_minute;
    caption.textContent = `${fmtRate(trend)} · ${fmtCount((history.rss || []).length)} ${t("chart.points", "个点")}`;
  }
}

function rowMetric(row) {
  const useCensus = censusReady();
  return useCensus ? num(row.census_bytes) : num(row.import_bytes);
}

function renderBars(node, rows, valueKey, labelKey, emptyText, valueFormatter = fmtBytes) {
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = `<div class="ms-empty">${esc(emptyText)}</div>`;
    return;
  }
  const max = Math.max(1, ...rows.map((row) => num(row[valueKey]) || 0));
  node.innerHTML = rows.map((row) => {
    const value = num(row[valueKey]) || 0;
    const percent = Math.max(0, Math.min(100, value / max * 100));
    return `<div class="ms-bar-row">
      <div class="ms-bar-label" title="${esc(row[labelKey])}">${esc(row[labelKey])}</div>
      <div class="ms-bar-value">${esc(valueFormatter(value))}</div>
      <div class="ms-bar-track"><span class="ms-bar-fill" style="width:${percent.toFixed(2)}%"></span></div>
    </div>`;
  }).join("");
}

function renderTopBars() {
  const report = state.report || {};
  const rows = (report.plugins || []).filter((row) => {
    if (censusReady()) return row.census_measured || report.census_meta;
    return row.import_measured && known(row.import_bytes);
  }).map((row) => ({
    label: row.display_name || row.name,
    value: rowMetric(row) || 0,
  })).sort((a, b) => b.value - a.value).slice(0, 8);
  renderBars(el("top-bars"), rows, "value", "label",
    censusReady() ? t("chart.noCensusRows", "普查已运行，但没有识别到插件对象") : t("chart.noImportRows", "没有可用的导入记录"));
  const caption = el("top-caption");
  if (caption) caption.textContent = censusReady()
    ? t("chart.topCensusCaption", "按存活对象浅层大小")
    : t("chart.topImportCaption", "按启动期 RSS 差分；不是当前独占占用");
}

function renderBreakdown() {
  const node = el("breakdown-bars");
  const hint = el("breakdown-hint");
  const buckets = (state.report && state.report.census_buckets) || [];
  if (!censusReady()) {
    if (hint) hint.textContent = t("breakdown.notRun", "对象普查默认关闭；运行一次后才有来源分布。");
    if (node) node.innerHTML = `<div class="ms-empty">${esc(t("breakdown.empty", "尚未运行对象普查"))}</div>`;
    return;
  }
  if (hint) hint.textContent = t("breakdown.desc", "这里只显示被 GC 跟踪且能按类型模块归类的对象，不等于 RSS。");
  renderBars(node, buckets.map((item) => ({
    label: t("bucket." + item.bucket, item.bucket),
    value: item.bytes,
  })), "value", "label", t("breakdown.empty", "没有其他对象"));
}

function renderProbeStatus() {
  const node = el("probe-status");
  if (!node) return;
  const process = reportProcess();
  const hook = process.import_hook || {};
  const report = state.report || {};
  const audit = report.audit_meta;
  const deep = report.deep_meta || {};
  const hookValue = hook.degraded
    ? t("status.degraded", "已降级")
    : hook.installed
      ? t("status.running", "运行中")
      : hook.plugin_count
        ? t("status.done", "已完成")
        : t("status.off", "无记录");
  const hookLevel = hook.degraded ? "warn" : hook.plugin_count ? "ok" : "neutral";
  const censusMeta = report.census_meta;
  const censusValue = censusMeta
    ? (censusMeta.truncated ? t("status.truncated", "已截断") : t("status.done", "已完成"))
    : t("status.notRun", "未运行");
  const auditValue = audit
    ? t("status.done", "已完成")
    : t("status.notRun", "未运行");
  const deepValue = deep.generated_at
    ? (deep.truncated ? t("status.truncated", "已截断") : t("status.done", "已有结果"))
    : t("status.notRun", "未运行");
  node.innerHTML = [
    statusRow(t("probe.import", "导入成本"), hookValue,
      `${fmtCount(hook.plugin_count, "0")} ${t("metric.plugins", "个插件")} · ${fmtMs(hook.overhead_ms)}`, hookLevel),
    statusRow(t("probe.census", "对象普查"), censusValue,
      censusMeta ? `${fmtMs(censusMeta.elapsed_ms)} · 1/${fmtCount(censusMeta.sample_rate)}` : t("probe.manual", "仅手动执行"), censusMeta && censusMeta.truncated ? "warn" : "neutral"),
    statusRow(t("probe.audit", "依赖审计"), auditValue,
      audit ? `${fmtCount(audit.audited)}/${fmtCount(audit.plugin_count)} ${t("probe.scanned", "个插件")}` : t("probe.manual", "首次查看或手动执行"), audit ? "ok" : "neutral"),
    statusRow(t("probe.deep", "引用图扫描"), deepValue,
      deep.generated_at ? fmtTime(deep.generated_at) : t("probe.manual", "仅手动执行"), deep.truncated ? "warn" : "neutral"),
  ].join("");
}

function sparkline(series) {
  const points = (series || []).map((item) => [num(item[0]), num(item[1])]).filter((item) => item[0] !== null && item[1] !== null);
  if (points.length < 2) return `<span class="ms-spark-empty">—</span>`;
  const width = 100;
  const height = 24;
  const min = Math.min(...points.map((item) => item[1]));
  const max = Math.max(...points.map((item) => item[1]));
  const span = Math.max(1, max - min);
  const path = points.map((point, index) => {
    const x = index / (points.length - 1) * width;
    const y = height - 3 - (point[1] - min) / span * (height - 6);
    return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<svg class="ms-spark" viewBox="0 0 ${width} ${height}" aria-hidden="true"><path d="${path}" /></svg>`;
}

function retainedValue(row) {
  return row && row.retained ? num(row.retained.total_bytes) : null;
}

function sortValue(row, key) {
  switch (key) {
    case "name": return String(row.display_name || row.name || "").toLowerCase();
    case "import": return num(row.import_bytes);
    case "census": return num(row.census_bytes);
    case "objects": return num(row.census_objects);
    case "retained": return retainedValue(row);
    case "trend": return num(row.trend_bytes_per_minute);
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
  node.innerHTML = COLUMNS.map((column) => {
    const sortable = column.sortable !== false;
    const active = sortable && state.sortKey === column.key;
    return `<th${sortable ? ` data-sort="${esc(column.key)}"` : ""}${active ? ` class="is-sorted" data-dir="${state.sortDir}"` : ""}>${esc(t(column.i18n, column.fallback))}</th>`;
  }).join("");
}

function pluginNameCell(row) {
  const tags = [];
  if (row.is_self) tags.push(`<span class="ms-tag">${esc(t("table.self", "本插件"))}</span>`);
  if (row.reserved) tags.push(`<span class="ms-tag">${esc(t("table.reserved", "内置"))}</span>`);
  if (!row.activated) tags.push(`<span class="ms-tag">${esc(t("table.inactive", "未启用"))}</span>`);
  return `<div class="ms-name"><div class="ms-name-text"><strong>${esc(row.display_name || row.name)}</strong><small>${esc(row.name)}</small></div>${tags.join("")}</div>`;
}

function renderTable() {
  renderTableHead();
  const body = el("plugin-body");
  if (!body) return;
  const allRows = ((state.report && state.report.plugins) || []).slice();
  const needle = state.search.trim().toLowerCase();
  const rows = allRows.filter((row) => !needle || [row.name, row.display_name, row.import_key].some((value) => String(value || "").toLowerCase().includes(needle))).sort(compareRows);
  const count = el("plugin-count");
  if (count) count.textContent = `${rows.length}/${allRows.length} ${t("metric.plugins", "个插件")}`;
  const censusState = el("census-state");
  if (censusState) {
    censusState.textContent = censusReady() ? t("table.censusReady", "对象普查：已测量") : t("table.censusMissing", "对象普查：未运行");
    censusState.dataset.level = censusReady() ? "ok" : "warn";
  }
  const deepState = el("deep-state");
  if (deepState) {
    const meta = (state.report && state.report.deep_meta) || {};
    deepState.textContent = meta.generated_at ? `${t("table.deep", "引用图")} · ${fmtTime(meta.generated_at)}` : t("table.deepMissing", "引用图：未运行");
    deepState.dataset.level = meta.generated_at && !meta.truncated ? "ok" : meta.truncated ? "warn" : "";
  }
  const hint = el("plugin-hint");
  if (hint) hint.textContent = censusReady()
    ? t("table.censusHint", "对象普查是按类型模块归属的浅层估算；点击一行查看导入、对象类型和引用图结果。")
    : t("table.importHint", "导入成本只回答“启动时加载它大约增加了多少 RSS”，不能当作当前插件独占内存；需要运行时归属时手动执行对象普查。");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="${COLUMNS.length}" class="ms-empty">${esc(needle ? t("table.noMatch", "没有匹配的插件") : t("table.empty", "暂无插件数据"))}</td></tr>`;
    return;
  }
  const historySeries = (state.history && state.history.series_by_plugin) || {};
  body.innerHTML = rows.map((row) => {
    const retained = row.retained;
    const retainedText = retained
      ? `${fmtBytes(retained.total_bytes)} <small>${fmtBytes(retained.exclusive_bytes)} / ${fmtBytes(retained.shared_bytes)}</small>`
      : "—";
    const trend = fmtRate(row.trend_bytes_per_minute);
    const censusText = censusReady() ? fmtBytes(row.census_bytes, "0 B") : "—";
    const objectsText = censusReady() ? fmtCount(row.census_objects, "0") : "—";
    return `<tr data-name="${esc(row.name)}" tabindex="0" aria-label="${esc(row.display_name || row.name)}">
      <td class="ms-cell-name">${pluginNameCell(row)}</td>
      <td class="ms-num">${esc(fmtBytes(row.import_bytes))}${row.import_measured ? "" : `<small class="ms-muted-block">${esc(t("status.unknown", "未知"))}</small>`}</td>
      <td class="ms-num">${esc(censusText)}</td>
      <td class="ms-num">${esc(objectsText)}</td>
      <td class="ms-num">${retainedText}</td>
      <td class="ms-num ${trend.startsWith("+") ? "ms-up" : trend.startsWith("−") ? "ms-down" : ""}">${esc(trend)}</td>
      <td>${sparkline(historySeries[row.name])}</td>
    </tr>`;
  }).join("");
}

function renderImports() {
  const report = state.report || {};
  const totals = report.totals || {};
  const hook = (report.process && report.process.import_hook) || {};
  const cards = el("import-cards");
  if (cards) {
    cards.innerHTML = [
      card(t("metric.totalImport", "导入账本"), esc(fmtBytes(totals.import_total_bytes, "—")),
        esc(t("imports.accounting", "插件代码与首次加载的第三方包之和")),
        kv(t("metric.pluginCode", "插件代码"), fmtBytes(totals.import_self_bytes_total, "—")) + kv(t("metric.packages", "第三方包"), fmtBytes(totals.packages_bytes, "—"))),
      card(t("metric.hookWindow", "记录窗口"), esc(fmtBytes(hook.rss_growth_bytes, "—")),
        esc(hook.rss_source || t("status.unavailable", "RSS 不可用")),
        kv(t("metric.calls", "导入调用"), fmtCount(hook.calls)) + kv(t("metric.hookCost", "钩子开销"), fmtMs(hook.overhead_ms))),
      card(t("metric.coverage", "插件覆盖"), esc(fmtCount(hook.plugin_count, "0")),
        esc(t("imports.coverageSub", "只有钩子安装后首次加载的插件可测量")),
        kv(t("metric.packagesCount", "包数量"), fmtCount(hook.package_count, "0")) + kv(t("metric.status", "状态"), hook.degraded ? t("status.degraded", "已降级") : hook.installed ? t("status.running", "运行中") : t("status.done", "已结束"))),
    ].join("");
  }
  const packageHint = el("package-hint");
  if (packageHint) packageHint.textContent = t("imports.packageHint", "同一个包只记在第一个导入者名下，共用它的插件不会重复计费。");
  const packageHead = el("package-head");
  if (packageHead) packageHead.innerHTML = [t("col.package", "包"), t("col.bytes", "RSS 差分"), t("col.modules", "模块"), t("col.importer", "首个导入者")].map((value) => `<th>${esc(value)}</th>`).join("");
  const packageBody = el("package-body");
  const packages = report.packages || [];
  if (packageBody) {
    packageBody.innerHTML = packages.length ? packages.map((item) => `<tr>
      <td class="ms-cell-name" title="${esc(item.name)}">${esc(item.name)}</td>
      <td class="ms-num">${esc(fmtBytes(item.bytes))}</td>
      <td class="ms-num">${esc(fmtCount(item.modules))}</td>
      <td>${esc(item.first_importer || "—")}</td>
    </tr>`).join("") : `<tr><td colspan="4" class="ms-empty">${esc(t("imports.empty", "没有可显示的导入记录；重启后可覆盖更多插件。"))}</td></tr>`;
  }
  const importHead = el("import-plugin-head");
  if (importHead) importHead.innerHTML = [t("col.plugin", "插件"), t("col.gross", "总成本"), t("col.self", "自身模块"), t("col.modules", "模块"), t("col.packages", "包数")].map((value) => `<th>${esc(value)}</th>`).join("");
  const importBody = el("import-plugin-body");
  const rows = (report.plugins || []).filter((row) => row.import_measured).sort((a, b) => (num(b.import_bytes) || 0) - (num(a.import_bytes) || 0));
  if (importBody) {
    importBody.innerHTML = rows.length ? rows.map((row) => `<tr data-name="${esc(row.name)}" class="ms-clickable-row">
      <td class="ms-cell-name">${pluginNameCell(row)}</td>
      <td class="ms-num">${esc(fmtBytes(row.import_bytes))}</td>
      <td class="ms-num">${esc(fmtBytes(row.import_self_bytes))}</td>
      <td class="ms-num">${esc(fmtCount(row.import_modules))}</td>
      <td class="ms-num">${esc(fmtCount((row.import_packages || []).length, "0"))}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="ms-empty">${esc(t("imports.emptyPlugins", "没有已测量的插件"))}</td></tr>`;
  }
}

function renderAudit() {
  const report = state.report || {};
  const meta = report.audit_meta;
  const summary = el("audit-summary");
  if (summary) {
    if (!meta) {
      summary.textContent = t("audit.notRun", "尚未运行；首次加载通常会自动建立缓存。");
    } else {
      const parts = [`${t("audit.scanned", "扫描")} ${fmtCount(meta.audited)}/${fmtCount(meta.plugin_count)}`, fmtMs(meta.elapsed_ms), `${t("audit.findings", "发现")} ${fmtCount(meta.finding_count)} ${t("audit.items", "处")}`];
      if (meta.pending) parts.push(`${t("audit.pending", "还剩")} ${fmtCount(meta.pending)} ${t("audit.pendingTail", "个未扫，再跑一次会接着扫")}`);
      summary.textContent = parts.join(" · ");
    }
  }
  const body = el("audit-body");
  const rows = (report.opportunities || []).slice();
  if (body) {
    body.innerHTML = meta && rows.length ? rows.map((row) => {
      const plugins = row.plugins || [];
      return `<tr>
        <td class="ms-cell-name"><strong>${esc(row.module)}</strong><small>${esc(plugins.slice(0, 4).join(", ") + (plugins.length > 4 ? " …" : ""))}</small></td>
        <td class="ms-num">${esc(fmtBytes(row.cost_bytes))}</td>
        <td class="ms-num">${esc(fmtCount(row.shared_by))}</td>
        <td>${esc(row.guarded ? `${row.guarded} ${t("audit.guarded", "处为可选导入")}` : t("audit.eager", "顶层加载"))}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="ms-empty">${esc(meta ? t("audit.empty", "没有找到带已知成本的重依赖") : t("audit.emptyNotRun", "尚未运行依赖审计"))}</td></tr>`;
  }
  const unknownNode = el("audit-unknown");
  const unknown = (meta && meta.unknown_modules) || [];
  if (unknownNode) {
    unknownNode.hidden = !unknown.length;
    unknownNode.textContent = unknown.length
      ? t("audit.unknown", "源码中还发现未建立成本表的第三方模块：") + " " + unknown.join(", ")
      : "";
  }
}

function renderCensus() {
  const report = state.report || {};
  const meta = report.census_meta;
  const summary = el("census-summary");
  if (summary) summary.textContent = meta
    ? `${t("census.scanned", "扫描")} ${fmtCount(meta.scanned)}/${fmtCount(meta.total_objects)} · ${fmtMs(meta.elapsed_ms)} · ${meta.truncated ? t("status.truncated", "已截断") : t("status.complete", "完成")}`
    : t("census.notRun", "尚未运行；这是唯一一次会遍历 GC 对象列表的操作。默认关闭。");
  const warning = el("census-warning");
  if (warning) warning.textContent = meta
    ? (meta.scaled ? t("census.sampled", "当前为抽样估算，数值已按抽样率放大。") : t("census.limit", "只统计 GC 跟踪对象的浅层大小，不能与进程 RSS 相加。"))
    : t("census.warning", "运行前请确认可以接受短暂停顿；在有 swap 的小 VPS 上，扫描可能把页面读回内存。");
  const pluginRows = (report.plugins || []).filter((row) => censusReady() && (row.census_measured || meta)).map((row) => ({
    label: row.display_name || row.name,
    value: row.census_bytes || 0,
  })).filter((row) => row.value > 0).sort((a, b) => b.value - a.value);
  renderBars(el("census-plugin-bars"), pluginRows, "value", "label", t("census.emptyPlugins", "没有识别到插件对象"));
  const bucketRows = (report.census_buckets || []).map((item) => ({
    label: t("bucket." + item.bucket, item.bucket),
    value: item.bytes || 0,
  }));
  renderBars(el("census-bucket-bars"), bucketRows, "value", "label", t("census.emptyBuckets", "没有其他对象来源"));
}

function alertKind(kind) {
  const key = kind === "rss" ? "kind.rss" : kind === "rss_growth" ? "kind.rssGrowth" : kind === "size" ? "kind.size" : "kind.growth";
  return t("alerts." + key, kind || t("alerts.unknown", "告警"));
}

function renderAlerts() {
  const node = el("alerts");
  if (!node) return;
  const summary = el("alerts-summary");
  if (summary) summary.textContent = state.alertsEnabled
    ? `${t("alerts.enabled", "告警已启用")} · ${fmtCount(state.alerts.length)} ${t("alerts.records", "条记录")}`
    : t("alerts.disabled", "当前没有设置告警阈值；可在插件配置中启用进程 RSS 或对象普查规则。");
  if (!state.alerts.length) {
    node.innerHTML = `<div class="ms-empty">${esc(t("alerts.empty", "暂无告警"))}</div>`;
    return;
  }
  node.innerHTML = state.alerts.slice().reverse().map((item) => `<article class="ms-alert" data-kind="${esc(item.kind)}">
    <div class="ms-alert-time">${esc(fmtTime(item.ts))}</div>
    <div class="ms-alert-msg"><strong>${esc(item.plugin === "__process__" ? t("alerts.process", "进程") : item.plugin)}</strong><span class="ms-tag">${esc(alertKind(item.kind))}</span><p>${esc(item.message)}</p></div>
    <div class="ms-alert-value">${esc(num(item.value) === null ? "—" : String(item.value))}</div>
  </article>`).join("");
}

function renderBanner() {
  const banner = el("banner");
  if (!banner) return;
  const notes = (state.report && state.report.notes) || [];
  const important = notes.filter((note) => note !== "census_never_run" && note !== "dep_audit_never_run");
  const shown = important.length ? important : notes.slice(0, 1);
  if (!shown.length) {
    banner.hidden = true;
    return;
  }
  const level = shown.some((note) => /truncated|degraded|missing|unavailable/.test(note)) ? "warn" : "info";
  banner.hidden = false;
  banner.dataset.level = level;
  const title = el("banner-title");
  const text = el("banner-text");
  if (title) title.textContent = level === "warn" ? t("banner.attention", "需要注意") : t("banner.note", "测量说明");
  if (text) text.textContent = shown.map((note) => t("note." + note, NOTE_FALLBACK[note] || note)).join(" ");
}

function renderHelp() {
  const node = el("help-list");
  if (node) {
    const items = [
      ["rss", "help.rss", "RSS"],
      ["import", "help.import", "导入成本"],
      ["census", "help.census", "对象普查"],
      ["retained", "help.retained", "引用图保留"],
      ["trend", "help.trend", "增长趋势"],
      ["cgroup", "help.cgroup", "容器额度"],
    ];
    node.innerHTML = items.map(([key, i18n, fallback]) => `<div class="ms-help-item"><dt>${esc(t("help.label." + key, fallback))}</dt><dd>${esc(t(i18n, "暂无说明"))}</dd></div>`).join("");
  }
  const safety = el("safety-list");
  if (safety) {
    const items = ["keepCensusOff", "watchSwap", "useRssAlert", "runDeepManually"];
    safety.innerHTML = items.map((key) => `<li>${esc(t("help.safety." + key, "—"))}</li>`).join("");
  }
}

function renderAll() {
  applyStaticText();
  renderBanner();
  renderCards();
  renderRssChart();
  renderTopBars();
  renderBreakdown();
  renderProbeStatus();
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

async function refresh({ deep = false, silent = false, allowBusy = false } = {}) {
  if (state.busy && !allowBusy) return;
  setAction("refresh");
  try {
    // A normal refresh must stay cheap.  Expensive/one-off probes are explicit
    // buttons; cached results remain visible because the backend keeps them.
    const params = { sample: "1", census: "0", audit: "0" };
    if (deep) params.deep = "1";
    const report = await apiGet("plugins", params);
    setReport(report);
    const [overview, history, alerts] = await Promise.all([
      apiGet("overview"),
      apiGet("history", { limit: 120 }),
      apiGet("alerts", { limit: ALERT_LIMIT }),
    ]);
    state.overview = overview || null;
    state.history = history || null;
    state.alerts = (alerts && alerts.alerts) || [];
    state.alertsEnabled = Boolean(alerts && alerts.enabled);
    renderAll();
    if (!silent) toast(deep ? t("toast.deepDone", "引用图扫描完成") : t("toast.refreshed", "已刷新"));
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

async function runCensus() {
  if (typeof window.confirm === "function" && !window.confirm(t("confirm.census", "对象普查会遍历 GC 对象列表，可能造成短暂停顿并读回 swap 页面。继续吗？"))) return;
  setAction("census");
  try {
    await apiPost("census", {});
    toast(t("toast.censusDone", "对象普查完成"));
    setTab("census");
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
    setTab("audit");
    await refresh({ silent: true, allowBusy: true });
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

async function runDeep() {
  if (typeof window.confirm === "function" && !window.confirm(t("confirm.deep", "引用图扫描会遍历插件可达对象，可能需要几秒。继续吗？"))) return;
  await refresh({ deep: true });
}

async function forceGc() {
  setAction("gc");
  try {
    const result = await apiPost("gc", {});
    toast(`${t("toast.gcDone", "GC 完成")} · ${fmtCount(result && result.collected)} · ${fmtBytes(result && result.rss_before)} → ${fmtBytes(result && result.rss_after)}`);
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
    await apiPost("baseline", { action });
    toast(action === "clear" ? t("toast.baselineCleared", "已清除基线") : t("toast.baselineSet", "已设为基线"));
    await refresh({ silent: true, allowBusy: true });
  } catch (error) {
    toast(errorText(error), "error");
  } finally {
    setAction(null);
  }
}

function closeDrawer() {
  state.drawerName = null;
  state.drawerPayload = null;
  const drawer = el("drawer");
  if (drawer) drawer.hidden = true;
}

function detailSection(title, content) {
  return `<section class="ms-detail-section"><h3>${esc(title)}</h3>${content}</section>`;
}

function renderDetail(payload) {
  const drawer = el("drawer");
  const node = el("drawer-content");
  if (!drawer || !node || !payload) return;
  const detail = payload.detail || payload;
  if (!detail.found) {
    node.innerHTML = `<div class="ms-empty">${esc(t("detail.notFound", "找不到这个插件"))}</div>`;
    return;
  }
  const row = detail.row || {};
  const importInfo = detail.import;
  const census = detail.census;
  const retained = row.retained;
  const audit = detail.audit;
  const packages = detail.import_packages || [];
  const importContent = importInfo
    ? `<div class="ms-detail-grid">${kv(t("detail.total", "总成本"), fmtBytes(importInfo.bytes))}${kv(t("detail.self", "自身模块"), fmtBytes(importInfo.self_bytes))}${kv(t("detail.wall", "耗时"), fmtMs(importInfo.wall_ms))}${kv(t("detail.modules", "模块"), fmtCount(importInfo.modules))}</div>`
    : `<p class="ms-muted">${esc(t("detail.importMissing", "该插件在记录窗口前已加载，导入成本未知。"))}</p>`;
  const censusContent = census
    ? `<div class="ms-detail-grid">${kv(t("detail.bytes", "浅层大小"), fmtBytes(census.bytes))}${kv(t("detail.objects", "对象数"), fmtCount(census.objects))}${kv(t("detail.types", "类型数"), fmtCount(census.type_count))}</div>
      <div class="ms-mono-list">${(census.types || []).slice(0, 12).map((item) => `<div class="ms-mono-row"><span>${esc(item.type)}</span><span>${esc(fmtBytes(item.bytes))}</span><span>${esc(fmtCount(item.objects))}</span></div>`).join("")}</div>`
    : `<p class="ms-muted">${esc(t("detail.censusMissing", "尚未对这个进程运行对象普查。"))}</p>`;
  const retainedContent = retained
    ? `<div class="ms-detail-grid">${kv(t("detail.retainedTotal", "合计"), fmtBytes(retained.total_bytes))}${kv(t("detail.exclusive", "仅此插件可达"), fmtBytes(retained.exclusive_bytes))}${kv(t("detail.shared", "多插件共享"), fmtBytes(retained.shared_bytes))}${kv(t("detail.retainedObjects", "对象数"), fmtCount((retained.exclusive_objects || 0) + (retained.shared_objects || 0)))}</div>
      ${retained.truncated ? `<p class="ms-callout ms-callout-warn">${esc(t("detail.truncated", "扫描被截断，数值是下界。"))}</p>` : ""}`
    : `<p class="ms-muted">${esc(t("detail.retainedMissing", "尚未运行引用图扫描。"))}</p>`;
  const packageContent = packages.length
    ? `<div class="ms-mono-list">${packages.slice(0, 20).map((item) => `<div class="ms-mono-row"><span>${esc(item.name)}</span><span>${esc(fmtBytes(item.bytes))}</span><span>${esc(fmtMs(item.wall_ms))}</span></div>`).join("")}</div>`
    : `<p class="ms-muted">${esc(t("detail.packagesEmpty", "没有记录到由它首次加载的第三方包。"))}</p>`;
  const auditImports = (audit && audit.imports) || [];
  const auditContent = audit
    ? (auditImports.length ? `<div class="ms-mono-list">${auditImports.map((item) => `<div class="ms-mono-row"><span>${esc(item.module)} <small>${esc(item.file)}:${esc(item.lineno)}</small></span><span>${esc(fmtBytes(item.cost_bytes))}</span><span>${item.guarded ? esc(t("audit.guardedShort", "可选")) : ""}</span></div>`).join("")}</div>` : `<p class="ms-muted">${esc(t("detail.auditEmpty", "没有识别到重依赖。"))}</p>`)
    : `<p class="ms-muted">${esc(t("detail.auditMissing", "尚未建立依赖审计结果。"))}</p>`;
  const series = detail.series || [];
  const header = el("drawer-title");
  if (header) header.textContent = row.display_name || detail.name;
  node.innerHTML = `<div class="ms-detail-meta">${esc(detail.name)} · ${esc(row.version || "—")} · ${esc(row.author || "—")}</div>
    ${detailSection(t("detail.importTitle", "加载期导入成本"), importContent)}
    ${detailSection(t("detail.censusTitle", "对象普查"), censusContent)}
    ${detailSection(t("detail.retainedTitle", "引用图保留量"), retainedContent)}
    ${detailSection(t("detail.packageTitle", "首次加载的第三方包"), packageContent)}
    ${detailSection(t("detail.auditTitle", "顶层依赖审计"), auditContent)}
    ${detailSection(t("detail.historyTitle", "插件对象趋势"), `<div class="ms-chart ms-chart-small" id="detail-chart"></div>`)}
    <p class="ms-muted">${esc(t("detail.submodules", "已加载子模块"))}: ${esc((detail.submodules || []).slice(0, 20).join(", ") || "—")}</p>`;
  renderLineChart(series, { node: el("detail-chart"), height: 160, label: detail.name, empty: t("detail.historyEmpty", "还没有对象普查历史") });
}

async function openDetail(name) {
  const drawer = el("drawer");
  const node = el("drawer-content");
  if (!drawer || !node) return;
  state.drawerName = name;
  state.drawerPayload = null;
  drawer.hidden = false;
  node.innerHTML = `<div class="ms-empty">${esc(t("status.loading", "加载中…"))}</div>`;
  try {
    const payload = await apiGet("detail", { name, deep: "0", census: "0", audit: "0" });
    if (state.drawerName !== name) return;
    state.drawerPayload = payload;
    renderDetail(payload);
  } catch (error) {
    if (state.drawerName === name) node.innerHTML = `<div class="ms-callout ms-callout-warn">${esc(errorText(error))}</div>`;
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

function stopAuto() {
  if (state.autoTimer) window.clearInterval(state.autoTimer);
  state.autoTimer = null;
}

function startAuto() {
  stopAuto();
  if (!state.auto) return;
  state.autoTimer = window.setInterval(() => {
    if (!document.hidden && !state.busy) refresh({ silent: true });
  }, state.auto * 1000);
}

function bindEvents() {
  for (const button of document.querySelectorAll(".ms-tab")) button.addEventListener("click", () => setTab(button.dataset.tab));
  el("btn-refresh")?.addEventListener("click", () => refresh({}));
  el("btn-deep")?.addEventListener("click", runDeep);
  el("btn-census")?.addEventListener("click", runCensus);
  el("btn-audit")?.addEventListener("click", runAudit);
  el("btn-baseline-set")?.addEventListener("click", () => setBaseline("set"));
  el("btn-baseline-clear")?.addEventListener("click", () => setBaseline("clear"));
  el("btn-gc")?.addEventListener("click", forceGc);
  el("drawer-close")?.addEventListener("click", closeDrawer);
  el("drawer")?.addEventListener("click", (event) => { if (event.target.closest("[data-close]")) closeDrawer(); });
  el("plugin-head")?.addEventListener("click", (event) => {
    const cell = event.target.closest("th[data-sort]");
    if (!cell) return;
    const key = cell.dataset.sort;
    if (state.sortKey === key) state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
    else { state.sortKey = key; state.sortDir = key === "name" ? "asc" : "desc"; }
    writeStore(STORE_SORT, state.sortKey + ":" + state.sortDir);
    renderTable();
  });
  const openRow = (event) => {
    const row = event.target.closest("tr[data-name]");
    if (row) openDetail(row.dataset.name);
  };
  el("plugin-body")?.addEventListener("click", openRow);
  el("import-plugin-body")?.addEventListener("click", openRow);
  el("plugin-body")?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("tr[data-name]");
    if (!row) return;
    event.preventDefault();
    openDetail(row.dataset.name);
  });
  let searchTimer = null;
  el("search")?.addEventListener("input", (event) => {
    state.search = event.target.value || "";
    if (searchTimer) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(renderTable, 100);
  });
  el("skin-select")?.addEventListener("change", (event) => {
    state.skin = SKIN_IDS.includes(event.target.value) ? event.target.value : "auto";
    writeStore(STORE_SKIN, state.skin);
    applySkin();
  });
  el("auto-select")?.addEventListener("change", (event) => {
    const value = Number(event.target.value);
    state.auto = AUTO_INTERVALS.includes(value) ? value : 0;
    writeStore(STORE_AUTO, String(state.auto));
    startAuto();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("drawer")?.hidden) closeDrawer(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.auto && !state.busy) refresh({ silent: true }); });
  window.addEventListener("beforeunload", stopAuto);
}

async function main() {
  if (!bridge) {
    document.body.innerHTML = `<p style="padding:24px;font-family:system-ui">${esc("MemoryScope 需要在 AstrBot Dashboard 的插件页内打开。")}</p>`;
    return;
  }
  if (typeof bridge.ready === "function") await bridge.ready();
  state.skin = readStore(STORE_SKIN, "auto");
  if (!SKIN_IDS.includes(state.skin)) state.skin = "auto";
  state.auto = Number(readStore(STORE_AUTO, "0")) || 0;
  if (!AUTO_INTERVALS.includes(state.auto)) state.auto = 0;
  const savedSort = String(readStore(STORE_SORT, "census:desc")).split(":");
  if (SORT_KEYS.includes(savedSort[0])) {
    state.sortKey = savedSort[0];
    state.sortDir = savedSort[1] === "asc" ? "asc" : "desc";
  }
  applyStaticText();
  applySkin();
  bindEvents();
  setTab(readStore(STORE_TAB, "overview"));
  renderHelp();
  const contextHandler = () => { applyStaticText(); applySkin(); renderAll(); };
  try {
    if (typeof bridge.onContext === "function") bridge.onContext(contextHandler);
    else if (typeof bridge.onContextChange === "function") bridge.onContextChange(contextHandler);
  } catch (_error) {
    // Older Dashboard builds have no context listener.
  }
  await refresh({ silent: true });
  startAuto();
}

main().catch((error) => toast(errorText(error), "error"));
