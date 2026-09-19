import { esc } from "./model.js";
import { t } from "./locale.js";

export function cleanActivityFilters(raw) {
  const fallback = { mode: "all", plugins: [], range: 3600, category: "" };
  try {
    const value = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!value || typeof value !== "object" || Array.isArray(value)) return fallback;
    const plugins = [...new Set((Array.isArray(value.plugins) ? value.plugins : [])
      .filter(p => typeof p === "string" && p.length > 0 && [...p].length <= 120))].slice(0, 200);
    const result = {
      mode: ["all", "include", "exclude"].includes(value.mode) ? value.mode : "all",
      plugins,
      range: [900, 3600, 21600, 86400].includes(value.range) ? value.range : 3600,
      category: ["", "handler", "llm", "tool", "lifecycle", "diagnostic", "trace", "process"].includes(value.category) ? value.category : "",
    };
    return new TextEncoder().encode(JSON.stringify(result)).length <= 12000 ? result : fallback;
  } catch { return fallback; }
}

const modeLabel = mode => ({ all: "全部插件", include: "只看所选", exclude: "排除所选" })[mode];

/** Native details + checkbox controls: searchable, keyboard-safe and bounded. */
export class ActivityPluginFilter {
  constructor(root, { getValue, getOptions, onApply, onError }) {
    this.root = root;
    this.getValue = getValue;
    this.getOptions = getOptions;
    this.onApply = onApply;
    this.onError = onError;
    root.innerHTML = `<span class="filter-label">${t("插件筛选")}</span><details class="plugin-filter-picker">
      <summary aria-label="${t("插件筛选")}"><span class="filter-summary"></span><span aria-hidden="true">⌄</span></summary>
      <div class="plugin-filter-panel">
        <fieldset class="plugin-filter-modes"><legend class="sr-only">${t("筛选方式")}</legend>${["all", "include", "exclude"].map(mode => `<label><input type="radio" name="activity-plugin-mode" value="${mode}"><span>${t(modeLabel(mode))}</span></label>`).join("")}</fieldset>
        <label class="sr-only" for="activity-plugin-search">${t("搜索插件名称或 ID")}</label><input type="search" id="activity-plugin-search" placeholder="${t("搜索插件名称或 ID")}" autocomplete="off">
        <div class="plugin-filter-tools"><span class="filter-count" aria-live="polite"></span><button type="button" class="text-button" data-filter-clear>${t("清空勾选")}</button></div>
        <div class="plugin-filter-options" role="group" aria-label="${t("选择插件")}"></div>
        <div class="plugin-filter-footer"><span>${t("应用后自动记住选择")}</span><button type="button" data-filter-cancel>${t("取消")}</button><button type="button" class="primary" data-filter-apply>${t("应用筛选")}</button></div>
      </div></details>`;
    this.details = root.querySelector("details");
    this.search = root.querySelector("input[type=search]");
    this.draft = cleanActivityFilters(getValue());
    this.details.addEventListener("toggle", () => {
      if (!this.details.open) return;
      this.draft = cleanActivityFilters(this.getValue());
      this.search.value = "";
      this.renderOptions();
    });
    root.querySelector("summary").addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); this.details.open = true; this.search.focus(); }
    });
    root.addEventListener("keydown", e => {
      if (e.key === "Escape" && this.details.open) { e.stopPropagation(); this.close(); }
    });
    this.search.oninput = () => this.renderOptions();
    root.addEventListener("change", e => {
      if (e.target.name === "activity-plugin-mode") this.draft.mode = e.target.value;
      if (e.target.matches("[data-filter-plugin]")) {
        const id = e.target.dataset.filterPlugin;
        const next = new Set(this.draft.plugins);
        if (e.target.checked) next.add(id); else next.delete(id);
        if (next.size > 200 || new TextEncoder().encode(JSON.stringify([...next])).length > 11000) {
          e.target.checked = false;
          this.onError(t("最多选择 200 个插件，筛选内容不能过长。"));
          return;
        }
        this.draft.plugins = [...next];
        if (this.draft.mode === "all" && next.size) this.draft.mode = "include";
      }
      this.updateDraft();
    });
    root.querySelector("[data-filter-clear]").onclick = () => { this.draft.plugins = []; this.renderOptions(); };
    root.querySelector("[data-filter-cancel]").onclick = () => this.close();
    root.querySelector("[data-filter-apply]").onclick = () => {
      this.onApply(cleanActivityFilters(this.draft));
      this.close();
      this.update();
    };
    this.update();
  }
  close() { this.details.open = false; this.root.querySelector("summary").focus(); }
  update() {
    const value = this.getValue();
    this.root.querySelector(".filter-summary").textContent = t(modeLabel(value.mode)) + (value.mode === "all" ? "" : ` · ${value.plugins.length}`);
    // Never rebuild an open picker during automatic refresh; preserve draft and focus.
  }
  updateDraft() {
    this.root.querySelectorAll("input[name=activity-plugin-mode]").forEach(el => { el.checked = el.value === this.draft.mode; });
    this.root.querySelector(".filter-count").textContent = `${t("已勾选")} ${this.draft.plugins.length} / 200`;
  }
  renderOptions() {
    const known = new Map(this.getOptions().map(row => [row.id, row]));
    for (const id of this.draft.plugins) if (!known.has(id)) known.set(id, { id, label: id });
    const query = this.search.value.trim().toLocaleLowerCase();
    const selected = new Set(this.draft.plugins);
    const options = [...known.values()].sort((a,b) => Number(selected.has(b.id))-Number(selected.has(a.id)) || a.label.localeCompare(b.label));
    const visible = options.filter(row => `${row.label} ${row.id}`.toLocaleLowerCase().includes(query));
    this.root.querySelector(".plugin-filter-options").innerHTML = visible.length ? visible.map(row => `<label class="plugin-filter-option"><input type="checkbox" data-filter-plugin="${esc(row.id)}" ${selected.has(row.id)?"checked":""}><span><strong>${esc(row.label)}</strong><small class="mono">${esc(row.id)}</small></span></label>`).join("") : `<p class="data-note">${t("没有匹配的插件")}</p>`;
    this.updateDraft();
  }
}
