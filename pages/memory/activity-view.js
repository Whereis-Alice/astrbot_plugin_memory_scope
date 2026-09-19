import { esc, size, signedSize, stamp, duration, finite } from "./model.js";
import { t } from "./locale.js";

export const categories = {
  handler: "插件处理", llm: "模型请求", tool: "工具调用", lifecycle: "生命周期",
  diagnostic: "内存诊断", trace: "分配追踪", process: "进程变化",
};
const states = { running: "进行中", ok: "已结束", error: "失败", cancelled: "已取消", expired: "未收到结束" };
export const evidenceNote = () => `<p class="data-note">${t("同期活动只是排查线索，不等于造成了这些内存增长；并发任务、原生库和未接入的后台任务仍可能是原因。")}</p>`;
export function recorderNote(recorder) {
  if (!recorder) return `<div class="notice">${t("该批次没有运行活动记录。旧数据不能还原当时的插件操作；请更新插件和独立后端。")}</div>`;
  const stale = Date.now() / 1000 - recorder.received_at > 90;
  return `<div class="notice">${t(!recorder.enabled ? "活动记录已关闭" : stale ? "活动记录连接过期" : "活动记录已连接")} · ${t("已接入处理函数")} ${esc(recorder.handlers)} · ${t("限流或队列丢弃")} ${esc(recorder.dropped)}<br>${t("只记录功能名称与时间，不保存聊天内容、提示词、工具参数。未接入的后台任务不会自动出现。")}</div>`;
}
export function allocationTable(rows) {
  return rows?.length ? `<div class="table-wrap"><table><thead><tr><th>${t("分配位置")}</th><th>${t("Python 净增长")}</th></tr></thead><tbody>${rows.map(r => `<tr><td class="mono">${esc(r.file)}:${esc(r.line)}</td><td class="mono">${signedSize(r.bytes)}</td></tr>`).join("")}</tbody></table></div>` : "";
}
function activityMemory(value) {
  if (!value?.available) {
    const reason = {unfinished:"未收到结束", pending_sample:"等待下一次采样", distant_samples:"附近采样间隔过大", missing_samples:"附近采样不足"}[value?.reason] || "附近采样不足";
    return `<div class="activity-memory unavailable"><small>${t("同期服务内存")}</small><strong class="mono">—</strong><span>${t(reason)}</span></div>`;
  }
  const delta = value.deltas.current;
  const tone = finite(delta) ? delta > 0 ? "increase" : delta < 0 ? "decrease" : "steady" : "steady";
  return `<div class="activity-memory" data-delta="${tone}"><div class="activity-memory-main"><small>${t("同期服务内存")}</small><strong class="mono">${signedSize(delta)}</strong></div>
    <div class="activity-memory-parts"><span>${t("匿名驻留")} <b class="mono">${signedSize(value.deltas.anon)}</b></span><span>Swap <b class="mono">${signedSize(value.deltas.swap)}</b></span></div>
    <div class="activity-window mono" title="${esc(t("邻近真实采样之间的总量变化，不是该插件独占，也不能逐行相加。"))}">${stamp(value.actual[0])} → ${stamp(value.actual[1])} · ${duration(value.seconds * 1000)}<br>${t("窗口内活动")} ${esc(value.activity_count)} · ${t("非插件独占")}</div></div>`;
}
export function activityList(items, { analysis = true, recorder = null, quickFilter = false, memory = false } = {}) {
  if (!items?.length) return `<div class="empty"><h2>${t("这个范围没有活动记录")}</h2><p>${t("没有记录不等于没有活动；可能尚未接入、被限流，或记录已过保留期限。")}</p></div>`;
  return `<div class="activity-list">${items.map(item => {
    const unclosed = item.status === "running" && (!recorder?.enabled || Date.now()/1000 - recorder.received_at > 90 || Date.now()/1000 - item.start > 1800);
    const status = unclosed ? "expired" : item.status;
    const growthStart = item.memory_window?.available ? item.memory_window.actual[0] : item.start - 15;
    const growthEnd = item.memory_window?.available ? item.memory_window.actual[1] : Math.min(item.end || item.start + 30, item.start + 86350) + 15;
    return `<article class="activity-item" data-category="${esc(item.category)}" data-memory="${memory}"><div class="activity-time mono">${stamp(item.start, true)}<small>${item.end ? stamp(item.end) : "—"}</small></div><div class="activity-body"><div class="activity-heading"><strong>${esc(item.plugin || "AstrBot")}</strong><span class="tag neutral">${t(categories[item.category] || "未知")}</span></div><div class="activity-operation mono">${esc(item.operation)}</div>${allocationTable(item.allocations)}${quickFilter && item.plugin ? `<button class="text-button activity-only" data-activity-only="${esc(item.plugin)}">${t("只看此插件")}</button>` : ""}</div>${memory ? activityMemory(item.memory_window) : ""}<div class="activity-meta"><span class="tag ${status === "error" ? "bad" : "neutral"}">${t(states[status] || "未知")}</span><small class="mono">${item.end ? duration(item.duration_ms) : "—"}</small>${analysis ? `<button class="text-button" data-growth-start="${growthStart}" data-growth-end="${growthEnd}">${t("查看同期内存")} ↗</button>` : ""}</div></article>`;
  }).join("")}</div>`;
}
export function growthView(report) {
  const memory = report.memory || {};
  const names = { current: "服务物理记账", anon: "匿名驻留", file: "文件缓存", kernel: "内核内存", swap: "Swap", anon_swap: "匿名内存 + Swap" };
  return `<h1 id="detail-title">${t("增长分析")}</h1>${evidenceNote()}${recorderNote(report.recorder)}${report.available ? `
    <p class="mono muted">${stamp(report.actual[0], true)} → ${stamp(report.actual[1], true)}</p>
    <p class="data-note">${t("使用区间内真实采样点，不补零、不跨启动批次。")}</p>
    <div class="growth-summary"><div><small>${t("区间变化")}</small><strong>${signedSize(memory.current?.delta)}</strong></div><div><small>${t("区间峰值")}</small><strong>${size(report.peak)}</strong></div><div><small>${t("最大采样间隔")}</small><strong>${duration(report.max_gap_seconds * 1000)}</strong></div></div>
    <h2>${t("增长构成")}</h2><div class="table-wrap"><table><thead><tr><th>${t("指标")}</th><th>${t("开始")}</th><th>${t("结束")}</th><th>${t("变化")}</th></tr></thead><tbody>${Object.entries(names).map(([key,name]) => `<tr><td>${t(name)}</td><td class="mono">${size(memory[key]?.before)}</td><td class="mono">${size(memory[key]?.after)}</td><td class="mono">${signedSize(memory[key]?.delta)}</td></tr>`).join("")}</tbody></table></div>
    <p class="data-note">${t("服务总量已经包含子进程和文件缓存；RSS 与这些分类不是可相加的独立账单。文件缓存也不是插件独占内存。")}</p>
    <h2>${t("进程变化")}</h2><div class="table-wrap"><table><thead><tr><th>${t("进程")}</th><th>RSS Δ</th><th>${t("状态")}</th></tr></thead><tbody>${(report.processes || []).map(p => `<tr><td>${esc(p.name)} <small class="mono">${esc(p.pid)} / ${esc(p.start_ticks)}</small>${p.main ? ` · ${t("主进程")}` : ""}</td><td class="mono">${signedSize(p.rss_delta)}</td><td>${t({present:"区间两端存在",appeared:"区间末端新增",disappeared:"区间末端消失"}[p.state])}</td></tr>`).join("")}</tbody></table></div>
    <p class="data-note">${t("进程按 PID 和启动标识匹配；新增或退出的进程不拿缺失值当零。采样之间的短命进程可能遗漏。")}</p>
    ` : `<div class="empty"><h2>${t("区间采样不足")}</h2><p>${t("至少需要两个真实采样点。请选择更大的区间，或等待下一次采样。")}</p></div>`}
    <h2>${t("同期活动")}</h2>${report.events_truncated ? `<p class="notice">${t("活动较多，仅显示最近 200 条；可到最近活动页缩小范围筛选。")}</p>` : ""}${activityList(report.activities, {analysis: false, recorder: report.recorder})}`;
}
