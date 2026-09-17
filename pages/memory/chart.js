import { esc, finite, size, stamp, trendStats } from "./model.js";
import { t } from "./locale.js";

let chartSequence = 0;

// SVG uses actual pixel dimensions, so text never shrinks with the desktop viewBox.
export class TrendChart {
  constructor(root, onSummary) {
    this.root = root;
    this.fillId = `scope-trend-fill-${++chartSequence}`;
    this.onSummary = onSummary;
    this.points = [];
    this.key = "current";
    this.zoom = null;
    this.zero = false;
    this.index = 0;
    this.observer = new ResizeObserver(() => this.render());
    this.observer.observe(root);
  }
  destroy() {
    this.observer.disconnect();
  }
  set(points, key, zero = false) {
    this.points = points;
    this.key = key;
    this.zero = zero;
    this.render();
  }
  reset() {
    this.zoom = null;
    this.render();
  }
  render() {
    const points = this.points.filter(
      (p) => !this.zoom || (p.ts >= this.zoom[0] && p.ts <= this.zoom[1]),
    );
    const valid = points.filter((p) => finite(p.memory?.[this.key]));
    this.onSummary?.(trendStats(points, this.key), Boolean(this.zoom));
    if (!valid.length) {
      this.root.innerHTML = `<div class="chart-empty">${t("还没有这个范围的数据")}<small>${t("缩短或扩大时间范围，或等待下一次采样。")}</small></div>`;
      return;
    }
    const w = Math.max(260, this.root.clientWidth),
      h = w < 500 ? 260 : w > 1000 ? 340 : 310;
    const left = 65,
      right = 24,
      top = 30,
      bottom = h - 37;
    const ts0 = points[0].ts,
      ts1 = points.at(-1).ts;
    const stats = trendStats(points, this.key),
      span = Math.max(stats.max - stats.min, 1048576);
    let low = this.zero ? 0 : Math.max(0, stats.min - span * 0.16),
      high = stats.max + span * 0.16;
    if (high <= low) high = low + 1048576;
    const unit = high >= 1073741824 ? 1073741824 : 1048576,
      unitName = unit === 1073741824 ? "GiB" : "MiB";
    const precision =
      (high - low) / unit < 0.1 ? 3 : (high - low) / unit < 1 ? 2 : 1;
    const x = (ts) =>
      left + ((ts - ts0) / Math.max(ts1 - ts0, 1)) * (w - left - right);
    const y = (value) =>
      bottom - ((value - low) / (high - low)) * (bottom - top);
    const intervals = points
      .slice(1)
      .map((p, i) => p.ts - points[i].ts)
      .filter((n) => n > 0)
      .sort((a, b) => a - b);
    const gap = Math.max(
      15,
      (intervals[Math.floor(intervals.length / 2)] || 5) * 4,
    );
    const segments = [];
    let segment = [];
    for (const p of points) {
      if (!finite(p.memory?.[this.key])) {
        if (segment.length) segments.push(segment);
        segment = [];
        continue;
      }
      if (segment.length && p.ts - segment.at(-1).ts > gap) {
        segments.push(segment);
        segment = [];
      }
      segment.push(p);
    }
    if (segment.length) segments.push(segment);
    const paths = segments
      .map((s) => {
        const path = s
          .map(
            (p, i) =>
              `${i ? "L" : "M"}${x(p.ts).toFixed(2)},${y(p.memory[this.key]).toFixed(2)}`,
          )
          .join(" ");
        return `<path class="chart-area" fill="url(#${this.fillId})" d="${path} L${x(s.at(-1).ts)},${bottom} L${x(s[0].ts)},${bottom} Z"/><path class="chart-line" d="${path}"/>`;
      })
      .join("");
    const grids = Array.from({ length: 5 }, (_, i) => {
      const v = low + ((high - low) * i) / 4,
        py = y(v);
      return `<line class="chart-grid" x1="${left}" x2="${w - right}" y1="${py}" y2="${py}"/><text class="chart-axis" x="${left - 10}" y="${py + 4}" text-anchor="end">${(v / unit).toFixed(precision)}</text>`;
    }).join("");
    const tickCount = w < 500 ? 3 : 5;
    const ticks = Array.from({ length: tickCount }, (_, i) => {
      const ts = ts0 + ((ts1 - ts0) * i) / (tickCount - 1);
      return `<text class="chart-axis" x="${x(ts)}" y="${h - 12}" text-anchor="${i === 0 ? "start" : i === tickCount - 1 ? "end" : "middle"}">${esc(ts1 - ts0 < 300 ? stamp(ts) : stamp(ts).slice(0, 5))}</text>`;
    }).join("");
    this.root.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(t("内存趋势，左右方向键查看采样点，拖动放大，Escape 复位。"))}" tabindex="0"><defs><linearGradient id="${this.fillId}" x1="0" y1="0" x2="0" y2="1"><stop class="chart-stop" offset="0" stop-opacity=".22"/><stop class="chart-stop" offset="1" stop-opacity=".015"/></linearGradient></defs><title>${esc(t("内存趋势"))} · ${size(stats.min)} — ${size(stats.max)}</title><text class="chart-axis" x="${left}" y="15">${unitName} · ${esc(this.zero ? t("从零开始") : t("自适应刻度"))}</text>${grids}${paths}${ticks}<circle class="chart-marker" r="4" cx="${x(valid.at(-1).ts)}" cy="${y(valid.at(-1).memory[this.key])}"/><rect class="chart-selection" x="0" y="${top}" width="0" height="${bottom - top}"/><g class="cursor" visibility="hidden"><line class="chart-cross" y1="${top}" y2="${bottom}"/><circle class="chart-marker" r="5"/></g></svg><div class="chart-tip" hidden role="status"></div>`;
    const svg = this.root.querySelector("svg"),
      cursor = svg.querySelector(".cursor"),
      line = cursor.querySelector("line"),
      dot = cursor.querySelector("circle"),
      tip = this.root.querySelector(".chart-tip"),
      selection = svg.querySelector(".chart-selection");
    const show = (index) => {
      this.index = Math.max(0, Math.min(valid.length - 1, index));
      const p = valid[this.index],
        px = x(p.ts),
        py = y(p.memory[this.key]);
      cursor.setAttribute("visibility", "visible");
      line.setAttribute("x1", px);
      line.setAttribute("x2", px);
      dot.setAttribute("cx", px);
      dot.setAttribute("cy", py);
      tip.hidden = false;
      tip.innerHTML = `<small>${esc(stamp(p.ts, true))}</small>${esc(size(p.memory[this.key]))}`;
      tip.style.left = Math.max(4, Math.min(w - 174, px + 12)) + "px";
      svg.setAttribute(
        "aria-label",
        `${stamp(p.ts, true)} · ${size(p.memory[this.key])}`,
      );
    };
    const coordinate = (e) =>
      Math.max(
        left,
        Math.min(
          w - right,
          ((e.clientX - svg.getBoundingClientRect().left) * w) /
            svg.getBoundingClientRect().width,
        ),
      );
    const nearest = (px) => {
      const ts = ts0 + ((px - left) / (w - left - right)) * (ts1 - ts0);
      let lo = 0,
        hi = valid.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (valid[mid].ts < ts) lo = mid + 1;
        else hi = mid;
      }
      return lo > 0 && ts - valid[lo - 1].ts < valid[lo].ts - ts ? lo - 1 : lo;
    };
    let down = null;
    svg.onpointermove = (e) => {
      const px = coordinate(e);
      show(nearest(px));
      if (down !== null) {
        selection.setAttribute("x", Math.min(down, px));
        selection.setAttribute("width", Math.abs(px - down));
      }
    };
    svg.onpointerdown = (e) => {
      if (e.pointerType !== "touch" && e.button === 0) {
        down = coordinate(e);
        svg.setPointerCapture(e.pointerId);
      }
      show(nearest(coordinate(e)));
    };
    svg.onpointerup = (e) => {
      if (down !== null) {
        const px = coordinate(e);
        if (Math.abs(px - down) > 18) {
          const a = valid[nearest(Math.min(px, down))].ts,
            b = valid[nearest(Math.max(px, down))].ts;
          if (a < b) this.zoom = [a, b];
        }
        down = null;
        this.render();
      }
    };
    svg.onpointercancel = () => {
      down = null;
      selection.setAttribute("width", 0);
    };
    svg.onpointerleave = () => {
      if (down === null) {
        cursor.setAttribute("visibility", "hidden");
        tip.hidden = true;
      }
    };
    svg.onkeydown = (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        show(this.index + (e.key === "ArrowRight" ? 1 : -1));
      } else if (e.key === "Escape") {
        this.reset();
        this.root.querySelector("svg")?.focus();
      }
    };
    svg.onfocus = () => show(this.index);
    svg.onblur = () => {
      tip.hidden = true;
      cursor.setAttribute("visibility", "hidden");
    };
  }
}
