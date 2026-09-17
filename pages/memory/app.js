import {
  esc,
  finite,
  unwrap,
  size,
  sizeParts,
  signedSize,
  stamp,
  duration,
  pluginRows,
  sortedRows,
} from "./model.js";
import { TrendChart } from "./chart.js";
import { t, setLocale } from "./locale.js";
const $ = (id) => document.getElementById(id);
const embedded = document.body.dataset.mode === "embedded";
const S = {
  tab: "overview",
  mode: null,
  token: "",
  overview: null,
  report: {},
  trend: { samples: [] },
  runs: [],
  jobs: [],
  local: {},
  alerts: [],
  range: 3600,
  metric: "current",
  zero: false,
  search: "",
  filter: "all",
  sort: "delta",
  page: 0,
  runId: "",
  historyReport: null,
  phase: "all",
  phaseSearch: "",
  phasePage: 0,
  errors: [],
  busy: false,
  queued: false,
  scanBusy: false,
};
let bridge,
  chart,
  timer,
  toastTimer,
  drawerFocus,
  localLoaded = false,
  viewBuilt = "";
const navs = [
  ["overview", "总览", "overview"],
  ["plugins", "插件", "plugins"],
  ["startup", "启动记录", "activity"],
  ["diagnostics", "诊断", "scan"],
  ["experiments", "对照", "compare"],
  ["guide", "说明", "help"],
];
const paths = {
  overview:
    '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  plugins:
    '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
  activity: '<path d="M2 13h5l3-9 4 16 3-7h5"/>',
  scan: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M7 12h10m-5-5v10"/>',
  compare: '<path d="M7 3v18M17 3v18M3 7h8m2 10h8M4 4l3 3 3-3m4 16 3-3 3 3"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3m0 3h.01"/>',
  server:
    '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6m-6 11h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  box: '<path d="m12 3 9 5v9l-9 5-9-5V8zM3 8l9 5 9-5m-9 5v9"/>',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.box}</svg>`;
const tag = (label, kind = "") =>
  `<span class="tag ${kind}">${esc(label)}</span>`;
const rawDetails = (data) =>
  `<details><summary>${t("查看原始证据")}</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
const empty = (title, description = "", action = "") =>
  `<div class="empty">${icon("scan")}<h2>${esc(title)}</h2><p>${esc(description)}</p>${action}</div>`;
const note = (text) => `<p class="data-note">${esc(text)}</p>`;
const cardHead = (title, sub = "", extra = "") =>
  `<div class="card-head"><div><h2>${esc(title)}</h2>${sub ? `<small>${esc(sub)}</small>` : ""}</div>${extra}</div>`;
const metric = (
  label,
  value,
  description,
  unit = "bytes",
  symbol = "activity",
) => {
  const parts =
    unit === "bytes"
      ? sizeParts(value)
      : [finite(value) ? value.toLocaleString() : "—", unit];
  return `<article class="card metric" data-signal="${symbol}"><div class="metric-label">${esc(label)}${icon(symbol)}</div><div class="metric-value">${esc(parts[0])}<span class="unit">${esc(parts[1])}</span></div><div class="metric-note">${esc(description)}</div></article>`;
};
const rows = () => pluginRows(S.overview || {}, S.report, S.local);
const phaseName = (name) =>
  t(
    {
      import: "导入",
      construct: "构造",
      initialize: "初始化",
      startup_window: "日志窗口",
    }[name] || name,
  );
const statusName = (state) =>
  t(
    {
      running: "运行中",
      starting: "启动中",
      stopped: "已停止",
      unavailable: "不可用",
      complete: "已完成",
      queued: "排队中",
      restoring: "恢复中",
      cancelled: "已取消",
      failed: "失败",
      interrupted: "已中断",
    }[state] ||
      state ||
      "未知",
  );
const preferences = {};
let preferenceSave = Promise.resolve();
function saved(key, fallback) {
  if (preferences[key]) return preferences[key];
  try {
    return localStorage.getItem("memoryscope." + key) || fallback;
  } catch {
    return fallback;
  }
}
function save(key, value) {
  preferences[key] = value;
  if (embedded && bridge) {
    const snapshot = { ...preferences };
    preferenceSave = preferenceSave
      .then(() => api("preferences", snapshot, "POST", true))
      .catch((e) => toast(e.message));
  }
  try {
    localStorage.setItem("memoryscope." + key, value);
  } catch {
    /* Storage can be denied in embedded contexts. */
  }
}
function theme(value) {
  if (!["paper", "midnight", "plum"].includes(value)) value = "paper";
  document.documentElement.dataset.scopeTheme = value;
  $("theme").value = value;
  paintTheme();
}
function paintTheme() {
  const value = document.documentElement.dataset.scopeTheme;
  $("theme-name").textContent = t("theme." + value);
  $("theme").setAttribute(
    "aria-label",
    `${t("theme")}: ${t("theme." + value)}`,
  );
  document.querySelectorAll("[data-theme-choice]").forEach((option) => {
    option.setAttribute(
      "aria-checked",
      String(option.dataset.themeChoice === value),
    );
  });
}
function closeTheme(restoreFocus = false) {
  $("theme-menu").hidden = true;
  $("theme").setAttribute("aria-expanded", "false");
  if (restoreFocus) $("theme").focus();
}
function openTheme() {
  $("theme-menu").hidden = false;
  $("theme").setAttribute("aria-expanded", "true");
  $("theme-menu").querySelector('[aria-checked="true"]').focus();
}
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}
function paintStatic() {
  paintTheme();
  document.querySelectorAll("[data-t]").forEach((el) => {
    el.textContent = t(el.dataset.t);
  });
  $("navigation").innerHTML = navs
    .map(
      ([id, label, symbol]) =>
        `<button data-go="${id}" aria-current="${S.tab === id ? "page" : "false"}">${icon(symbol)}${t(label)}</button>`,
    )
    .join("");
  $("navigation").setAttribute("aria-label", t("页面导航"));
}
async function api(endpoint, params = {}, method = "GET", local = false) {
  let value;
  if (embedded) {
    value =
      method === "GET"
        ? await bridge.apiGet((local ? "" : "observer_") + endpoint, params)
        : await bridge.apiPost((local ? "" : "observer_") + endpoint, params);
  } else {
    const target = endpoint === "start" ? "experiments" : endpoint;
    const res = await fetch(
      "./api/" +
        target +
        (method === "GET" ? "?" + new URLSearchParams(params) : ""),
      {
        method,
        headers: {
          Authorization: "Bearer " + S.token,
          "Content-Type": "application/json",
        },
        body: method === "GET" ? undefined : JSON.stringify(params),
        signal: AbortSignal.timeout(12000),
      },
    );
    const body = await res.json();
    if (!res.ok)
      throw new Error(
        res.status === 401
          ? t("凭据无效，请重新连接")
          : body.error || `HTTP ${res.status}`,
      );
    value = body;
  }
  return unwrap(value);
}
function setNotice() {
  const o = S.overview,
    messages = [...S.errors];
  if (o?.stale)
    messages.unshift(
      t("数据已过期，下面保留上次成功结果。") +
        (o.bridge_error ? " " + o.bridge_error : ""),
    );
  if (o?.error) messages.push(o.error);
  if (S.mode === "local")
    messages.unshift(
      t("本地模式：尚未连接独立后端，仅显示 AstrBot 进程和本地诊断。"),
    );
  $("notice").hidden = !messages.length;
  $("notice").className = "notice" + (S.errors.length ? " error" : "");
  $("notice").textContent = [...new Set(messages)].join(" · ");
  const age = o?.latest?.ts ? Date.now() / 1000 - o.latest.ts : null,
    stale = o?.stale || age > Math.max(20, (o?.config?.interval || 5) * 4);
  $("connection").className =
    "connection" + (S.errors.length ? " error" : stale ? " pending" : "");
  $("connection").innerHTML =
    `<i></i><span>${S.errors.length ? t("连接异常") : S.mode === "local" ? t("本地诊断") : stale ? t("数据待更新") : t("独立后端已连接")}</span>`;
  $("updated").textContent = o?.latest?.ts
    ? `${t("采样于")} ${stamp(o.latest.ts)}`
    : "";
}
async function refresh() {
  if (S.busy) {
    S.queued = true;
    return;
  }
  if (!S.mode) return;
  S.busy = true;
  $("refresh").disabled = true;
  const errors = [];
  try {
    if (S.mode === "observer") {
      const o = await api("overview"),
        previous = S.overview?.run?.id;
      S.overview = o;
      const current = o.run?.id;
      if (previous && previous !== current) {
        S.report = {};
        S.trend = { samples: [] };
        S.local = {};
        localLoaded = false;
        chart?.reset();
      }
      const requestedRange = S.range;
      const tasks = [
        ["runs", () => api("runs")],
        ["jobs", () => api("experiments")],
      ];
      if (current)
        tasks.push(
          ["report", () => api("run", { id: current, samples: 0 })],
          [
            "trend",
            () => api("trend", { id: current, seconds: requestedRange }),
          ],
        );
      else {
        S.report = {};
        S.trend = { samples: [] };
      }
      const results = await Promise.allSettled(tasks.map(([, call]) => call()));
      results.forEach((r, i) => {
        if (r.status === "fulfilled") {
          if (tasks[i][0] !== "trend" || requestedRange === S.range)
            S[tasks[i][0]] = r.value;
        } else
          errors.push(
            t("部分数据读取失败") +
              " (" +
              tasks[i][0] +
              "): " +
              r.reason.message,
          );
      });
    } else {
      const [overview, local, history] = await Promise.all([
        api("overview", {}, "GET", true),
        api(
          "plugins",
          { sample: 0, census: 0, audit: 0, deep: 0 },
          "GET",
          true,
        ),
        api("history", { limit: 480 }, "GET", true),
      ]);
      S.local = local;
      S.overview = {
        latest: {
          ts: overview.server_time,
          state: "running",
          memory: {
            current: overview.process?.rss_bytes,
            swap: overview.process?.swap_bytes,
          },
          processes: [],
        },
        run: {},
        local: overview,
      };
      S.trend = {
        samples: (history.rss || [])
          .filter(([ts]) => !S.range || ts >= Date.now() / 1000 - S.range)
          .map(([ts, value]) => ({ ts, memory: { current: value } })),
      };
    }
    S.errors = errors;
  } catch (error) {
    S.errors = [error.message];
  } finally {
    S.busy = false;
    $("refresh").disabled = false;
    $("content").setAttribute("aria-busy", "false");
    setNotice();
    render();
    if (S.queued) {
      S.queued = false;
      refresh();
    }
  }
}
function schedule() {
  clearInterval(timer);
  const seconds = Number($("autorefresh").value);
  if (seconds)
    timer = setInterval(() => {
      if (
        !document.hidden &&
        !S.scanBusy &&
        !$("confirm").open &&
        $("drawer").hidden
      )
        refresh();
    }, seconds * 1000);
}
function title() {
  const subtitles = {
    overview: "从服务总量到插件证据，先看变化，再找原因。",
    plugins: "清单、启动增量和诊断证据，放在同一张表里。",
    startup: "还原加载顺序，区分导入、构造与初始化。",
    diagnostics: "需要时再深入，日常查看不会自动扫描对象。",
    experiments: "对比停用前后，验证实际能省下多少内存。",
    guide: "理解数字的边界，才不会把线索当结论。",
  };
  const labels = {
    overview: "MEMORY OBSERVATORY",
    plugins: "PLUGIN ATLAS",
    startup: "STARTUP TRACE",
    diagnostics: "DIAGNOSTIC LAB",
    experiments: "CONTROLLED COMPARISON",
    guide: "READING THE EVIDENCE",
  };
  $("page-title").textContent = t(navs.find((n) => n[0] === S.tab)[1]);
  $("eyebrow").textContent = labels[S.tab];
  $("page-subtitle").textContent = t(subtitles[S.tab]);
  document
    .querySelectorAll("nav [data-go]")
    .forEach((el) =>
      el.setAttribute(
        "aria-current",
        el.dataset.go === S.tab ? "page" : "false",
      ),
    );
}
function navigate(tab) {
  if (!navs.some((n) => n[0] === tab)) tab = "overview";
  S.tab = tab;
  history.replaceState(null, "", "#" + tab);
  title();
  render(true);
  if (tab === "diagnostics") loadDiagnostics();
  if (!S.busy && S.mode && !S.overview) refresh();
}
function render(force = false) {
  title();
  if (!S.overview && S.tab !== "guide") {
    if (S.errors.length)
      $("content").innerHTML =
        `<div class="card">${empty(t("暂时无法读取数据"), t("请检查插件后端配置和服务状态，然后点击刷新。"))}</div>`;
    return;
  }
  const rebuild = force || viewBuilt !== S.tab;
  if (rebuild) {
    chart?.destroy();
    chart = null;
    viewBuilt = S.tab;
  }
  if (S.tab === "overview") renderOverview(rebuild);
  if (S.tab === "plugins") renderPlugins(rebuild);
  if (S.tab === "startup") renderStartup(rebuild);
  if (S.tab === "diagnostics") renderDiagnostics(rebuild);
  if (S.tab === "experiments") renderExperiments(rebuild);
  if (S.tab === "guide" && rebuild) renderGuide();
}
function trendControls() {
  return `<div class="chart-toolbar"><div class="pills" role="group" aria-label="${t("时间范围")}">${[
    [900, "15m"],
    [3600, "1h"],
    [21600, "6h"],
    [86400, "24h"],
    [0, t("本次启动")],
  ]
    .map(
      ([v, l]) =>
        `<button data-range="${v}" aria-pressed="${S.range === v}">${l}</button>`,
    )
    .join(
      "",
    )}</div><div class="chart-options"><label class="sr-only" for="chart-metric">${t("趋势指标")}</label><select id="chart-metric">${[
    ["current", S.mode === "local" ? "进程 RSS" : "服务物理记账"],
    ["anon_swap", "匿名内存 + Swap"],
    ["swap", "Swap"],
  ]
    .filter(([v]) => S.mode !== "local" || v === "current")
    .map(
      ([v, l]) =>
        `<option value="${v}" ${S.metric === v ? "selected" : ""}>${t(l)}</option>`,
    )
    .join(
      "",
    )}</select><label><input type="checkbox" id="chart-zero" ${S.zero ? "checked" : ""}>${t("从零开始")}</label><button id="chart-reset" class="text-button" hidden>${t("复位")}</button></div></div>`;
}
function renderOverview(build) {
  if (build) {
    $("content").innerHTML =
      `<div id="overview-metrics" class="metrics"></div><div class="overview-grid"><section class="card chart-card">${cardHead(t("内存趋势"), t("真实采样点 · 悬停查看 · 拖动放大"), tag(t("服务整体")))}${trendControls()}<div id="trend" class="trend"></div><p id="trend-note" class="chart-hint"></p><div id="trend-summary" class="chart-summary"></div></section><aside id="insight" class="card insight-card"></aside></div><div class="two-col"><section class="card">${cardHead(t("启动关注项"), t("按加载期间 RSS + Swap 增量排序"), `<button data-go="plugins" class="text-button">${t("全部插件")} ↗</button>`)}<div id="top-plugins"></div></section><section class="card">${cardHead(t("服务进程"), t("进程包含在服务总量中，不要重复相加"), '<span class="tag neutral">/proc</span>')}<div id="processes"></div></section></div>`;
    chart = new TrendChart($("trend"), (stats, zoom) => {
      $("trend-summary").innerHTML = [
        ["最低", size(stats.min)],
        ["最高", size(stats.max)],
        ["区间变化", signedSize(stats.change)],
      ]
        .map(
          ([a, b]) =>
            `<div><span>${t(a)}</span><strong>${esc(b)}</strong></div>`,
        )
        .join("");
      $("chart-reset").hidden = !zoom;
    });
    $("chart-metric").onchange = (e) => {
      S.metric = e.target.value;
      chart.reset();
      chart.set(S.trend.samples || [], S.metric, S.zero);
    };
    $("chart-zero").onchange = (e) => {
      S.zero = e.target.checked;
      chart.set(S.trend.samples || [], S.metric, S.zero);
    };
    $("chart-reset").onclick = () => chart.reset();
  }
  const latest = S.overview.latest || {},
    m = latest.memory || {},
    r = rows(),
    measured = r.filter((p) => finite(p.delta));
  $("overview-metrics").innerHTML =
    metric(
      t(S.mode === "local" ? "进程 RSS" : "服务物理记账"),
      m.current,
      t(S.mode === "local" ? "仅 AstrBot 主进程" : "含子进程与文件缓存"),
      "bytes",
      "server",
    ) +
    metric(t("匿名内存 + Swap"), m.anon_swap, t("包括换出量，并非物理内存")) +
    metric("Swap", m.swap, t("已换出到磁盘的内存"), "bytes", "box") +
    metric(
      t("已发现插件"),
      r.length,
      `${measured.length} ${t("有启动增量证据")}`,
      "",
      "plugins",
    );
  const h = latest.host || {},
    available = h.available,
    total = h.total,
    ratio =
      finite(available) && total
        ? Math.max(0, Math.min(100, (available / total) * 100))
        : null,
    ready = S.overview.run?.ready_at,
    started = S.overview.run?.started_at;
  $("insight").innerHTML =
    `<div class="insight-head"><div class="eyebrow">SERVICE PULSE</div><h2 class="status-heading"><i class="dot ${latest.state !== "running" ? "bad" : ""}"></i>${statusName(latest.state)}</h2><p>${t("持续观察，不打扰日常使用。")}</p></div><div class="insight-metrics"><div><div class="stat-row"><span>${t("宿主机可用")}</span><strong>${size(available)}</strong></div>${ratio !== null ? `<svg class="meter" width="100%" height="5" viewBox="0 0 100 5" preserveAspectRatio="none" aria-label="${ratio.toFixed(1)}%"><rect width="100" height="5" fill="var(--soft)"/><rect width="${ratio}" height="5" fill="var(--accent)"/></svg>` : ""}</div><div class="stat-row"><span>${t("就绪耗时")}</span><strong>${finite(ready) && finite(started) ? duration((ready - started) * 1000) : "—"}</strong></div><div class="stat-row"><span>${t("采集器自身 RSS")}</span><strong>${size(S.overview.observer?.rss)}</strong></div><div class="stat-row"><span>${t("服务进程数")}</span><strong>${latest.processes?.length ?? "—"}</strong></div></div><div class="insight-breakdown"><div class="stat-row"><span>${t("匿名驻留")}</span><strong>${size(m.anon)}</strong></div><div class="stat-row"><span>${t("文件缓存")}</span><strong>${size(m.file)}</strong></div></div><div class="insight-foot">${t("启动增量是线索，不是插件当前独占内存。")} <button data-go="guide" class="text-button">${t("了解口径")} ↗</button></div>`;
  $("top-plugins").innerHTML = pluginTable(
    sortedRows(measured).slice(0, 5),
    true,
  );
  $("processes").innerHTML = processTable(latest.processes || []);
  chart.set(S.trend.samples || [], S.metric, S.zero);
  $("trend-note").textContent =
    (S.trend.downsampled
      ? t("长时间范围保留峰谷显示；统计基于显示点。")
      : t("自动适应纵轴，避免启动尖峰压平日常波动。")) +
    " " +
    (S.trend.samples?.length || 0) +
    " " +
    t("个显示点");
}
function processTable(items) {
  if (!items.length)
    return empty(t("暂无进程明细"), t("后端将在下一次采样时更新。"));
  return `<div class="table-wrap"><table><thead><tr><th>${t("进程 / 启动关联")}</th><th>RSS</th><th>PSS</th><th>Swap</th></tr></thead><tbody>${items.map((p) => `<tr><td>${esc(p.name || "process")} <small>#${esc(p.pid)}</small><span class="sub" title="${esc(p.startup_owner || "")}">${esc(p.startup_owner || t("未发现插件启动关联"))}</span></td><td class="mono num">${size(p.rss)}</td><td class="mono num" title="${t("明细采样于")} ${stamp(p.detailed_at, true)}">${size(p.pss)}</td><td class="mono num">${size(p.swap)}</td></tr>`).join("")}</tbody></table></div>`;
}

function pluginTable(items, compact = false) {
  if (!items.length)
    return empty(
      t("没有匹配的插件"),
      S.search
        ? t("试试清空搜索或切换筛选条件。")
        : t("插件清单通常每分钟同步；未接入启动探针时，启动增量会保持未知。"),
    );
  const max = Math.max(...items.map((p) => Math.abs(p.delta || 0)), 1);
  return `<div class="table-wrap"><table><thead><tr><th>${t("插件")}</th>${compact ? "" : `<th>${t("状态")}</th>`}<th>${t("启动增量")} <small>RSS + Swap</small></th><th>${t("加载耗时")}</th>${compact ? "" : `<th>${t("证据")}</th>`}</tr></thead><tbody>${items.map((p) => `<tr><td><button class="plugin-link" data-plugin="${esc(p.id)}" title="${esc(p.label)}">${esc(p.label)}</button><span class="sub" title="${esc(p.id)}">${esc(p.id)}</span></td>${compact ? "" : `<td>${tag(p.failed ? t("加载失败") : p.activated === true ? t("已启用") : p.activated === false ? t("已停用") : t("未同步"), p.failed ? "bad" : p.activated !== true ? "neutral" : "")}</td>`}<td class="bar-cell mono num">${signedSize(p.delta)}${p.partial ? " *" : ""}${finite(p.delta) ? `<svg class="mini-track" width="120" height="3" viewBox="0 0 120 3" aria-hidden="true"><rect width="${(Math.abs(p.delta) / max) * 120}" height="3" fill="var(--accent)"/></svg>` : ""}</td><td class="mono num">${duration(p.duration)}</td>${compact ? "" : `<td>${p.phases.length ? tag(p.phases.length + " " + t("阶段")) : tag(t("未测量"), "neutral")}</td>`}</tr>`).join("")}</tbody></table></div>`;
}
function renderPlugins(build) {
  if (build) {
    $("content").innerHTML =
      `<div class="toolbar"><label class="sr-only" for="plugin-search">${t("搜索插件")}</label><input id="plugin-search" type="search" placeholder="${t("搜索插件名称或目录…")}" value="${esc(S.search)}"><div class="inline"><span id="plugin-count" class="muted"></span><label class="sr-only" for="plugin-filter">${t("插件筛选")}</label><select id="plugin-filter">${[
        ["all", "全部状态"],
        ["on", "已启用"],
        ["off", "已停用"],
        ["unmeasured", "未测量"],
      ]
        .map(
          ([v, l]) =>
            `<option value="${v}" ${S.filter === v ? "selected" : ""}>${t(l)}</option>`,
        )
        .join(
          "",
        )}</select><label class="sr-only" for="plugin-sort">${t("排序")}</label><select id="plugin-sort">${[
        ["delta", "按启动增量"],
        ["duration", "按加载耗时"],
        ["name", "按名称"],
      ]
        .map(
          ([v, l]) =>
            `<option value="${v}" ${S.sort === v ? "selected" : ""}>${t(l)}</option>`,
        )
        .join(
          "",
        )}</select></div></div><section class="card"><div id="plugin-table"></div></section><div class="toolbar section-gap"><span class="data-note">${t("— 表示未测到；* 表示只包含部分阶段。增量不能当作当前占用。")}</span><div id="plugin-pager" class="inline"></div></div>`;
    $("plugin-search").oninput = (e) => {
      S.search = e.target.value;
      S.page = 0;
      renderPlugins(false);
    };
    $("plugin-filter").onchange = (e) => {
      S.filter = e.target.value;
      S.page = 0;
      renderPlugins(false);
    };
    $("plugin-sort").onchange = (e) => {
      S.sort = e.target.value;
      S.page = 0;
      renderPlugins(false);
    };
  }
  const all = rows(),
    filtered = sortedRows(all, S.sort, S.search, S.filter);
  S.page = Math.min(S.page, Math.max(0, Math.ceil(filtered.length / 30) - 1));
  $("plugin-count").textContent = filtered.length + " / " + all.length;
  $("plugin-table").innerHTML = pluginTable(
    filtered.slice(S.page * 30, (S.page + 1) * 30),
  );
  $("plugin-pager").innerHTML = pager("plugin", S.page, filtered.length, 30);
}
function pager(name, page, total, limit) {
  return total > limit
    ? `<button data-page="${name}" data-step="-1" ${page === 0 ? "disabled" : ""}>${t("上一页")}</button><small>${page + 1} / ${Math.ceil(total / limit)}</small><button data-page="${name}" data-step="1" ${(page + 1) * limit >= total ? "disabled" : ""}>${t("下一页")}</button>`
    : "";
}
function runOptions() {
  return S.runs
    .map(
      (run) =>
        `<option value="${esc(run.id)}" ${S.runId === run.id ? "selected" : ""}>${esc(stamp(run.started_at, true))} · PID ${esc(run.pid)}${run.id === S.overview.run?.id ? " · " + t("当前") : ""}</option>`,
    )
    .join("");
}
async function selectRun(id) {
  S.runId = id;
  S.historyReport = null;
  renderStartup(true);
  try {
    const report = await api("run", { id, samples: 0 });
    if (S.runId === id) {
      S.historyReport = report;
      if (S.tab === "startup") renderStartup(false);
    }
  } catch (e) {
    toast(e.message);
    if (S.tab === "startup" && S.runId === id)
      $("startup-data").innerHTML = empty(t("读取启动记录失败"), e.message);
  }
}
function renderStartup(build) {
  if (S.mode === "local") {
    $("content").innerHTML =
      `<div class="card">${empty(t("需要连接独立后端"), t("本地导入统计只覆盖本插件加载之后；完整启动记录需要早期探针。"))}</div>`;
    return;
  }
  if (build) {
    $("content").innerHTML =
      `<div class="toolbar"><div class="inline"><label for="run-select">${t("启动批次")}</label><select id="run-select">${runOptions()}</select></div><span class="muted">${S.runs.length} ${t("个保留批次")}</span></div><div id="startup-data"></div>`;
    $("run-select").value = S.runId || S.overview.run?.id || "";
    $("run-select").onchange = (e) => selectRun(e.target.value);
  }
  const report =
    S.runId && S.runId !== S.overview.run?.id ? S.historyReport : S.report;
  if (!report) {
    $("startup-data").innerHTML = `<div class="loading">${t("loading")}</div>`;
    return;
  }
  const phases = report.phases || [],
    run = report.run || {};
  const content = `<div class="metrics">${metric(t("就绪耗时"), finite(run.ready_at) ? Number((run.ready_at - run.started_at).toFixed(2)) : null, t("从进程出现到服务就绪"), "s", "clock")}${metric(t("已记录阶段"), phases.length, t("导入 / 构造 / 初始化"), "", "activity")}${metric(t("涉及插件"), new Set(phases.map((p) => p.plugin)).size, t("有阶段记录的插件"), "", "plugins")}${metric(t("首次出现依赖"), report.packages?.length ?? null, t("共享依赖不重复分摊"), "", "box")}</div><div class="notice">${report.probe_complete ? t("探针记录完整。时间段增量仍可能混入并发工作。") : t("探针记录不完整或未启用；没有测到的值保持未知。")} ${t("启动时间")}: ${stamp(run.started_at, true)}</div><section class="card">${cardHead(t("逐插件加载阶段"), t("按实际发生顺序排列；点击插件查看完整证据。"))}<div class="card-body toolbar"><label class="sr-only" for="phase-search">${t("搜索插件")}</label><input id="phase-search" type="search" placeholder="${t("搜索插件名称或目录…")}" value="${esc(S.phaseSearch)}"><label class="sr-only" for="phase-filter">${t("阶段筛选")}</label><select id="phase-filter">${["all", "import", "construct", "initialize", "startup_window"].map((v) => `<option value="${v}" ${S.phase === v ? "selected" : ""}>${v === "all" ? t("全部阶段") : phaseName(v)}</option>`).join("")}</select></div><div id="phase-table"></div><div class="card-body row-between section-gap"><small>${t("窗口增量不是独占内存，负值表示该时段总量下降。")}</small><div id="phase-pager" class="inline"></div></div></section><section class="card section-gap">${cardHead(t("共享依赖首次出现"), t("首次导入者不等于唯一使用者；不为共享包虚构占用大小。"))}<div class="card-body package-list">${
    (report.packages || [])
      .map(
        (p) => `<span title="${esc(p.first_importer)}">${esc(p.name)}</span>`,
      )
      .join("") || t("未记录")
  }</div></section>`;
  if (build || !$("phase-table")) {
    $("startup-data").innerHTML = content;
    $("phase-search").oninput = (e) => {
      S.phaseSearch = e.target.value;
      S.phasePage = 0;
      renderPhases(
        S.runId && S.runId !== S.overview.run?.id ? S.historyReport : S.report,
      );
    };
    $("phase-filter").onchange = (e) => {
      S.phase = e.target.value;
      S.phasePage = 0;
      renderPhases(
        S.runId && S.runId !== S.overview.run?.id ? S.historyReport : S.report,
      );
    };
  }
  if (!build && $("phase-table")) {
    const latest = document.createElement("template");
    latest.innerHTML = content;
    for (const selector of [".metrics", ".notice", ".package-list"]) {
      const target = $("startup-data").querySelector(selector);
      const updated = latest.content.querySelector(selector);
      if (target && updated) target.innerHTML = updated.innerHTML;
    }
    const select = $("run-select");
    if (document.activeElement !== select) {
      select.innerHTML = runOptions();
      select.value = S.runId || S.overview.run?.id || "";
    }
  }
  renderPhases(report);
}
function renderPhases(report) {
  const phases = (report?.phases || []).filter(
    (p) =>
      (S.phase === "all" || p.phase === S.phase) &&
      p.plugin.toLowerCase().includes(S.phaseSearch.toLowerCase()),
  );
  S.phasePage = Math.min(
    S.phasePage,
    Math.max(0, Math.ceil(phases.length / 30) - 1),
  );
  $("phase-table").innerHTML = phases.length
    ? `<div class="table-wrap"><table><thead><tr>${["插件 / 阶段", "开始时间", "耗时", "主进程增量", "服务窗口增量"].map((v) => `<th>${t(v)}</th>`).join("")}</tr></thead><tbody>${phases
        .slice(S.phasePage * 30, (S.phasePage + 1) * 30)
        .map(
          (p) =>
            `<tr><td><button class="plugin-link" data-history-plugin="${esc(p.plugin)}">${esc(p.plugin)}</button>${tag(phaseName(p.phase), p.failed ? "bad" : "neutral")}</td><td class="mono num">${stamp(p.start)}</td><td class="mono num">${duration(p.duration_ms)}</td><td class="mono num">${signedSize(p.process_rss_swap_delta)}</td><td class="mono num">${signedSize(p.service_anon_swap_delta)}</td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : empty(
        t("没有匹配的阶段"),
        t("未启用早期探针的批次，可能只有粗略日志窗口或没有记录。"),
      );
  $("phase-pager").innerHTML = pager("phase", S.phasePage, phases.length, 30);
}
async function loadDiagnostics(force = false) {
  if (!embedded || (localLoaded && !force)) return;
  try {
    const [report, alerts] = await Promise.all([
      api("plugins", { sample: 0, census: 0, audit: 0, deep: 0 }, "GET", true),
      api("alerts", { limit: 30 }, "GET", true),
    ]);
    S.local = report;
    S.alerts = alerts.alerts || [];
    localLoaded = true;
    if (S.tab === "diagnostics") renderDiagnostics(true);
  } catch (e) {
    toast(t("本地诊断读取失败") + ": " + e.message);
    if (S.tab === "diagnostics")
      $("diagnostic-results").innerHTML = empty(
        t("本地诊断读取失败"),
        e.message,
      );
  }
}
function renderDiagnostics(build) {
  if (!build) return;
  const tools = [
    [
      "audit",
      "依赖审计",
      "读取插件源码，找出可能在加载时引入的重依赖。不会扫描对象堆。",
      "box",
    ],
    [
      "census",
      "对象普查",
      "估算 Python 对象的归属。可能引起短暂停顿和换入，不覆盖所有原生库内存。",
      "scan",
    ],
    [
      "deep",
      "引用图扫描",
      "沿插件引用查找持有对象，区分独有与共享引用。会消耗 CPU，建议空闲时使用。",
      "plugins",
    ],
  ];
  $("content").innerHTML =
    `${!embedded ? `<div class="notice">${t("独立页面无法访问进程内对象。请在 AstrBot 的 MemoryScope 插件页执行诊断。")}</div>` : ""}<div class="diagnostic-grid">${tools.map(([id, label, desc, sym]) => `<section class="card tool-card" data-tool="${id}"><div class="tool-icon">${icon(sym)}</div><h2>${t(label)}</h2><p>${t(desc)}</p><span class="tool-status">${S.local[id === "audit" ? "audit_meta" : id === "census" ? "census_meta" : "deep_meta"]?.generated_at ? stamp(S.local[id === "audit" ? "audit_meta" : id === "census" ? "census_meta" : "deep_meta"].generated_at, true) : t("按需执行")}</span><button data-scan="${id}" ${!embedded || S.scanBusy ? "disabled" : ""}>${t("运行一次")}</button></section>`).join("")}</div><section class="card">${cardHead(t("诊断结果"), t("这里的对象估算不能与启动增量或 RSS 相加。"))}<div id="diagnostic-results"></div></section><div class="two-col section-gap"><section class="card">${cardHead(t("本地告警记录"), t("连接后端时不持续运行本地扫描，这不是服务器告警流。"))}<div class="card-body">${S.alerts.length ? S.alerts.map((a) => `<div class="job"><small>${stamp(a.ts, true)}</small><p>${esc(a.message)}</p></div>`).join("") : empty(t("暂无本地告警"))}</div></section><section class="card">${cardHead(t("进程维护"), t("GC 不保证降低 RSS；仅在排查问题时按需使用。"))}<div class="card-body"><button data-scan="gc" ${!embedded || S.scanBusy ? "disabled" : ""}>${t("运行垃圾回收")}</button>${note(t("导出报告可以保存当前证据，文件不包含连接凭据。"))}<button data-export class="quiet">${t("export")}</button></div></section></div>`;
  renderDiagnosticResult();
}
function renderDiagnosticResult() {
  if (!$("diagnostic-results")) return;
  const data = S.local,
    meta = [data.census_meta, data.audit_meta, data.deep_meta].filter(
      (m) => m?.generated_at,
    ),
    measured = (data.plugins || []).filter(
      (p) => p.census_measured || p.retained || p.audit_measured,
    );
  if (!meta.length && !measured.length) {
    $("diagnostic-results").innerHTML = empty(
      t("尚未运行诊断"),
      t("选择上方工具运行一次。未测量不代表插件占用为零。"),
    );
    return;
  }
  $("diagnostic-results").innerHTML =
    `<div class="scan-result"><p>${t("最近结果")}: ${stamp(data.generated_at, true)} · ${t("具体覆盖范围见原始证据")}</p></div><div class="table-wrap"><table><thead><tr><th>${t("插件")}</th><th>${t("对象普查")}</th><th>${t("引用图估算")}</th><th>${t("依赖线索")}</th></tr></thead><tbody>${measured
      .map(
        (p) =>
          `<tr><td>${esc(p.display_name || p.name)}</td><td class="mono">${p.census_measured ? size(p.census_bytes) : "—"}</td><td class="mono">${size(p.retained?.total_bytes ?? p.retained_bytes)}</td><td>${p.audit_measured ? esc(p.audit_findings) : "—"}</td></tr>`,
      )
      .join(
        "",
      )}</tbody></table></div><div class="card-body">${rawDetails({ census: data.census_meta, deep: data.deep_meta, audit: data.audit_meta, opportunities: data.opportunities, notes: data.notes })}</div>`;
}
async function confirmAction(title, message) {
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = message;
  const dialog = $("confirm");
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise((resolve) =>
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "confirm"),
      { once: true },
    ),
  );
}
async function scan(name) {
  if (S.scanBusy || !embedded) return;
  const descriptions = {
    audit: "依赖审计只读取源码，可能短时使用 CPU。",
    census:
      "对象普查可能暂停消息处理并引起内存换入。它不会得到精确的逐插件 RSS。",
    deep: "引用图扫描会分片处理对象，但仍会消耗 CPU 并触碰内存。建议在空闲时执行。",
    gc: "垃圾回收可能引起短暂停顿，并不保证内存数字下降。",
  };
  if (!(await confirmAction(t("执行进程内诊断"), t(descriptions[name]))))
    return;
  S.scanBusy = true;
  renderDiagnostics(true);
  toast(t("正在运行诊断，请稍候…"));
  try {
    const result = await api(name, {}, "POST", true);
    if (name === "gc") toast(t("垃圾回收已完成"));
    else {
      S.local = { ...S.local, ...result };
      toast(t("诊断完成"));
    }
    if (S.tab === "diagnostics") renderDiagnostics(true);
  } catch (e) {
    toast(e.message);
  } finally {
    S.scanBusy = false;
    if (S.tab === "diagnostics") renderDiagnostics(true);
  }
}

function renderExperiments(build) {
  if (S.mode !== "observer") {
    $("content").innerHTML =
      `<div class="card">${empty(t("需要连接独立后端"))}</div>`;
    return;
  }
  const allowed = S.overview.config?.allow_experiments === true;
  if (build) {
    const candidates = (S.overview.run?.inventory || []).filter(
      (p) =>
        p.activated &&
        !p.reserved &&
        p.root_dir_name !== "astrbot_plugin_memory_scope",
    );
    $("content").innerHTML =
      `<div class="two-col"><section class="card">${cardHead(t("创建对照实验"), t("A 正常启动 → B 临时排除 → A 恢复验证"))}<div class="card-body"><div class="timeline"><span>A · ${t("正常")}</span><span>B · ${t("排除")}</span><span>A · ${t("恢复")}</span></div><p class="data-note">${t("会真实重启 AstrBot，机器人会暂时离线。仅影响实验启动，不删除业务数据。")}</p>${!allowed ? `<div class="notice">${t("后端未开放重启实验，日常观察不需要开启。")}</div>` : ""}<form id="experiment-form"><div class="form-grid"><label class="field">${t("目标插件")}<select id="experiment-plugin" required><option value="">${t("选择插件")}</option>${candidates.map((p) => `<option value="${esc(p.root_dir_name)}">${esc(p.display_name || p.name || p.root_dir_name)}</option>`).join("")}</select></label><label class="field">${t("排除方式")}<select id="experiment-mode"><option value="skip">${t("跳过导入")}</option><option value="disable">${t("仅停用")}</option></select></label><label class="field">${t("实验轮数")}<select id="experiment-rounds"><option value="1">1 · ${t("重启 3 次")}</option><option value="2">2 · ${t("重启 5 次")}</option><option value="3">3 · ${t("重启 7 次")}</option></select></label></div><label class="checkline"><input id="experiment-ack" type="checkbox"><span>${t("我理解实验会多次重启 AstrBot，并已选择合适的时间。")}</span></label><button id="start-experiment" class="primary" disabled>${t("创建实验")}</button></form>${note(t("单轮仅是初步线索；多轮仍需尽量保持消息量和工作负载一致。"))}</div></section><section class="card experiment-history">${cardHead(t("实验记录"), t("任务由后端执行，关闭页面不会终止。"))}<div class="card-body" id="jobs"></div></section></div>`;
    $("experiment-form").onchange = updateExperimentButton;
    $("experiment-form").onsubmit = startExperiment;
  }
  updateExperimentButton();
  $("jobs").innerHTML = S.jobs.length
    ? S.jobs
        .map(
          (j) =>
            `<article class="job"><div class="row-between"><span class="mono">${esc(j.plugin || j.id)}</span>${tag(statusName(j.state), j.state === "failed" ? "bad" : j.state === "complete" ? "" : "neutral")}</div><small>${stamp(j.started_at, true)}</small>${j.result ? `<p>${t("条件收益")}: <strong>${signedSize(j.result.saving_bytes)}</strong></p><p>${esc(classification(j.result.classification))}</p>` : ""}${j.message ? `<p>${esc(j.message)}</p>` : ""}${["queued", "running", "restoring"].includes(j.state) ? `<button data-cancel-experiment>${t("取消并恢复")}</button>` : ""}${rawDetails(j)}</article>`,
        )
        .join("")
    : empty(t("还没有对照实验"), t("日常查看无需创建实验。"));
}
function classification(name) {
  return t(
    {
      single_trial: "单轮结果，仅供初步参考",
      within_variation: "差异未超过自然波动",
      environment_changed: "环境不同，不宜直接比较",
      insufficient_data: "稳定窗口不足",
      difference_observed: "检测到差异，请结合波动与环境判断",
    }[name] ||
      name ||
      "未知",
  );
}
function updateExperimentButton() {
  if (!$("start-experiment")) return;
  $("start-experiment").disabled = !(
    S.overview.config?.allow_experiments &&
    $("experiment-ack").checked &&
    $("experiment-plugin").value &&
    !S.jobs.some((j) => ["queued", "running", "restoring"].includes(j.state))
  );
}
async function startExperiment(e) {
  e.preventDefault();
  const rounds = Number($("experiment-rounds").value),
    plugin = $("experiment-plugin").value;
  const message = `${plugin} · ${2 * rounds + 1} ${t("次重启。期间机器人会离线，结束后恢复正常启动。")}`;
  if (!(await confirmAction(t("确认开始重启对照"), message))) return;
  $("start-experiment").disabled = true;
  try {
    await api(
      "start",
      {
        plugin,
        rounds,
        mode: $("experiment-mode").value,
        confirm_restart: true,
      },
      "POST",
    );
    $("experiment-ack").checked = false;
    toast(t("实验已交给后端执行"));
    await refresh();
  } catch (e) {
    toast(e.message);
    updateExperimentButton();
  }
}
function renderGuide() {
  const sections = [
    [
      "01 / SERVICE",
      "先看服务整体",
      "服务物理记账包括 AstrBot、其子进程和文件缓存。匿名内存 + Swap 包含换出量，不能理解为物理内存。",
      "PSS 分摊进程间的共享页，不能再拆成每个 Python 插件的独占账单。",
    ],
    [
      "02 / STARTUP",
      "启动增量是线索",
      "探针记录导入、构造、初始化的时间窗口。窗口内还有其他后台任务，共享库也可能被多个插件使用。",
      "负增量表示该时间段总量下降。— 表示未采集或无法分辨，不能当作零。",
    ],
    [
      "03 / DIAGNOSTICS",
      "对象估算不是 RSS",
      "普查主要看可追踪的 Python 对象；引用图扫描看插件仍持有的对象。原生库、共享引用与扫描预算都会影响覆盖。",
      "不开扫描也能查看后端趋势、插件清单与启动记录。日常无需开启自动普查。",
    ],
    [
      "04 / COMPARISON",
      "对照验证实际收益",
      "重复进行正常启动、临时排除、恢复验证，观察稳定窗口的差值与波动。机器人会在实验期间多次离线。",
      "结果只适用于当时的插件组合和工作负载。多个插件的节省量不能直接相加。",
    ],
    [
      "05 / READING",
      "让曲线保持可读",
      "日常选择 15 分钟或 1 小时；排查启动再选择本次启动。纵轴默认自适应，可切换从零开始。",
      "悬停或点击查看时间与数值；电脑拖动放大区间，点击复位还原。方向键逐点查看，Escape 复位。断点不会被补成零。",
    ],
    [
      "06 / CONNECTION",
      "数据从哪里来",
      "独立后端持续保存采样，插件只负责安全转发与展示。后端离线时显示错误或过期记录，不伪装成零占用。",
      "仅本地模式无法回溯早于本插件的导入。进程内诊断需在 AstrBot 插件页执行，独立页用于观察与对照。",
    ],
  ];
  $("content").innerHTML =
    `<div class="guide-grid">${sections.map(([code, title, a, b]) => `<section class="card guide-card"><div class="eyebrow">${code}</div><h2>${t(title)}</h2><p>${t(a)}</p><p>${t(b)}</p></section>`).join("")}</div>`;
}
function openDetail(id, historical = false) {
  const report = historical
    ? S.runId && S.runId !== S.overview.run?.id
      ? S.historyReport
      : S.report
    : S.report;
  const source = historical
    ? pluginRows({ run: report?.run || {} }, report || {})
    : rows();
  const row = source.find((r) => r.id === id);
  if (!row) {
    toast(t("这个批次没有该插件的详情"));
    return;
  }
  drawerFocus = document.activeElement;
  $("detail").innerHTML =
    `<h1 id="detail-title">${esc(row.label)}</h1><p class="identity mono muted">${esc(row.id)}</p><p>${tag(row.activated === true ? t("已启用") : row.activated === false ? t("已停用") : t("未同步"), "neutral")} ${row.version ? tag(row.version, "neutral") : ""}</p><div class="metrics">${metric(t("启动窗口增量"), row.delta, t("RSS + Swap；非当前独占占用"))}${metric(t("加载耗时"), finite(row.duration) ? Number((row.duration / 1000).toFixed(3)) : null, t("该插件记录到的阶段合计"), "s", "clock")}</div><p class="data-note">${t("窗口增量可能含并发工作。共享依赖首次出现在这里，不代表只被这个插件使用。")}</p><h2>${t("阶段证据")}</h2>${row.phases.length ? `<div class="table-wrap"><table><thead><tr><th>${t("阶段")}</th><th>${t("耗时")}</th><th>RSS + Swap Δ</th></tr></thead><tbody>${row.phases.map((p) => `<tr><td>${phaseName(p.phase)} ${p.failed ? tag(t("失败"), "bad") : ""}</td><td class="mono">${duration(p.duration_ms)}</td><td class="mono num">${signedSize(p.process_rss_swap_delta)}</td></tr>`).join("")}</tbody></table></div>` : empty(t("没有启动阶段证据"), t("插件清单仍可见；需要早期探针才能记录完整导入。"))}<h2>${t("首次出现的依赖")}</h2><div class="package-list">${row.packages.map((p) => `<span>${esc(p.name)}</span>`).join("") || `<p class="muted">${t("未记录")}</p>`}</div><h2>${t("关联子进程")}</h2>${row.processes.length ? processTable(row.processes) : `<p class="muted">${t("未发现插件启动关联")}</p>`}${row.local ? `<h2>${t("本地诊断证据")}</h2>${rawDetails(row.local)}` : ""}${rawDetails({ phases: row.phases, packages: row.packages })}`;
  $("drawer").hidden = false;
  $("workspace").inert = true;
  document.querySelector(".topbar").inert = true;
  document.querySelector(".navrow").inert = true;
  document.querySelector("footer").inert = true;
  document.body.style.overflow = "hidden";
  $("close-drawer").focus();
}
function closeDrawer() {
  $("drawer").hidden = true;
  $("workspace").inert = false;
  document.querySelector(".topbar").inert = false;
  document.querySelector(".navrow").inert = false;
  document.querySelector("footer").inert = false;
  document.body.style.overflow = "";
  drawerFocus?.focus();
}
function exportReport() {
  const blob = new Blob(
    [
      JSON.stringify(
        {
          exported_at: Date.now() / 1000,
          source: S.mode,
          overview: S.overview,
          startup: S.report,
          trend: S.trend,
          selected_startup: S.historyReport,
          diagnostics: S.local,
          experiments: S.jobs,
        },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download =
    "memoryscope-" + new Date().toISOString().replace(/[:.]/g, "-") + ".json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(t("报告已导出"));
}
document.addEventListener("click", async (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  if (button.dataset.go) navigate(button.dataset.go);
  if (button.dataset.plugin) openDetail(button.dataset.plugin);
  if (button.dataset.historyPlugin)
    openDetail(button.dataset.historyPlugin, true);
  if (button.dataset.range !== undefined) {
    S.range = Number(button.dataset.range);
    document
      .querySelectorAll("[data-range]")
      .forEach((el) => el.setAttribute("aria-pressed", el === button));
    chart?.reset();
    await refresh();
  }
  if (button.dataset.page) {
    const step = Number(button.dataset.step);
    if (button.dataset.page === "plugin") {
      S.page += step;
      renderPlugins(false);
    } else {
      S.phasePage += step;
      renderPhases(
        S.runId && S.runId !== S.overview.run?.id ? S.historyReport : S.report,
      );
    }
  }
  if (button.dataset.scan) scan(button.dataset.scan);
  if (button.hasAttribute("data-export")) exportReport();
  if (
    button.hasAttribute("data-cancel-experiment") &&
    (await confirmAction(
      t("取消并恢复"),
      t("停止后续实验并恢复正常启动；可能需要再重启一次 AstrBot。"),
    ))
  ) {
    try {
      await api("cancel", {}, "POST");
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  }
});
$("close-drawer").onclick = closeDrawer;
$("drawer").onclick = (e) => {
  if (e.target === $("drawer")) closeDrawer();
};
document.addEventListener("keydown", (e) => {
  if ($("drawer").hidden) return;
  if (e.key === "Escape") closeDrawer();
  if (e.key === "Tab") {
    const items = [
      ...$("drawer").querySelectorAll(
        'button,a,input,select,summary,[tabindex="0"]',
      ),
    ].filter((el) => el.getClientRects().length);
    const first = items[0],
      last = items.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});
$("theme").onclick = () => {
  if ($("theme-menu").hidden) openTheme();
  else closeTheme(true);
};
$("theme-picker").addEventListener("keydown", (e) => {
  const items = [...$("theme-menu").querySelectorAll("[data-theme-choice]")];
  if (e.key === "Escape" && !$("theme-menu").hidden) {
    e.preventDefault();
    closeTheme(true);
  } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
    e.preventDefault();
    if ($("theme-menu").hidden) {
      openTheme();
      return;
    }
    const index = items.indexOf(document.activeElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? items.length - 1
          : (index + (e.key === "ArrowDown" ? 1 : -1) + items.length) %
            items.length;
    items[next].focus();
  } else if (e.key === "Tab" && !$("theme-menu").hidden) {
    // Return to the trigger so normal tab order continues outside the menu.
    closeTheme(true);
  }
});
$("theme-menu").addEventListener("click", (e) => {
  const choice = e.target.closest("[data-theme-choice]");
  if (!choice) return;
  theme(choice.dataset.themeChoice);
  save("theme", choice.dataset.themeChoice);
  closeTheme(true);
});
document.addEventListener("pointerdown", (e) => {
  if (!$("theme-picker").contains(e.target)) closeTheme();
});
$("theme-picker").addEventListener("focusout", (e) => {
  if (!$("theme-picker").contains(e.relatedTarget)) closeTheme();
});
$("locale").onchange = (e) => {
  setLocale(e.target.value);
  save("locale", e.target.value);
  paintStatic();
  render(true);
};
$("refresh").onclick = () => {
  if (!S.mode && embedded) boot();
  else {
    refresh();
    if (S.tab === "diagnostics") loadDiagnostics(true);
  }
};
$("autorefresh").onchange = schedule;
$("export").onclick = exportReport;
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
$("auth").onsubmit = async (e) => {
  e.preventDefault();
  S.token = $("token").value.trim();
  $("token").value = "";
  S.mode = "observer";
  await refresh();
  if (S.overview) {
    $("auth").hidden = true;
    $("workspace").hidden = false;
    schedule();
    navigate(location.hash.slice(1));
  } else {
    $("token").focus();
  }
};
async function boot() {
  theme(saved("theme", "paper"));
  setLocale(saved("locale", "zh-CN"));
  $("locale").value = document.documentElement.lang;
  paintStatic();
  if (!embedded) {
    $("auth").hidden = false;
    $("workspace").hidden = true;
    $("connection").innerHTML = `<i></i><span>${t("等待连接")}</span>`;
    return;
  }
  try {
    bridge = window.AstrBotPluginPage;
    if (!bridge)
      throw new Error(t("未找到 AstrBot 页面桥接，请从插件页面重新打开。"));
    await Promise.race([
      Promise.resolve(bridge.ready?.()),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error(t("AstrBot 页面连接超时"))), 15000),
      ),
    ]);
    const context = bridge.getContext?.() || {};
    try {
      Object.assign(preferences, await api("preferences", {}, "GET", true));
    } catch {
      /* Older plugin bridge: keep session preferences. */
    }
    theme(saved("theme", context.isDark ? "midnight" : "paper"));
    setLocale(
      saved("locale", context.locale?.startsWith("en") ? "en-US" : "zh-CN"),
    );
    $("locale").value = document.documentElement.lang;
    paintStatic();
    if (!saved("theme", ""))
      theme(context.isDark || context.theme === "dark" ? "midnight" : "paper");
    if (!saved("locale", "")) {
      setLocale(context.locale?.startsWith("en") ? "en-US" : "zh-CN");
      $("locale").value = document.documentElement.lang;
      paintStatic();
    }
    const status = await api("status");
    if (status.enabled && !status.configured)
      throw new Error(status.error || t("后端连接配置不完整"));
    S.mode = status.enabled ? "observer" : "local";
    if (S.mode === "local") S.metric = "current";
    const tab = location.hash.slice(1);
    if (navs.some((n) => n[0] === tab)) S.tab = tab;
    await refresh();
    schedule();
    if (S.tab === "diagnostics") loadDiagnostics();
  } catch (e) {
    S.errors = [e.message];
    setNotice();
    $("content").innerHTML =
      `<div class="card">${empty(t("暂时无法读取数据"), e.message)}</div>`;
    $("content").setAttribute("aria-busy", "false");
  }
}
boot();
