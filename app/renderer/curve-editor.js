// One curve editor for the mapping editor's input curves and parameter response curves: a
// multi-point curve from normalized input (x, 0..1) to normalized output (y), linear or smooth
// (monotone cubic after Fritsch and Carlson, 1980), as stored in `v2l.mapping` ({points, smooth}).
// Beyond 0..1 the curve continues along its end slopes. Points can be added, dragged and
// deleted on the canvas, or edited as numbers in the list below it (keyboard access).
import { t } from "./i18n.js";
import { fitCanvas, fmt, graphTheme } from "./graph.js";
import { ICONS } from "./icons.js";

const api = window.v2l;
const IDENTITY = { points: [[0, 0], [1, 1]], smooth: false };
export const CURVE_PRESETS = {
  linear: IDENTITY,
  easeIn: { points: [[0, 0], [0.5, 0.25], [1, 1]], smooth: true },
  easeOut: { points: [[0, 0], [0.5, 0.75], [1, 1]], smooth: true },
  sCurve: { points: [[0, 0], [0.25, 0.1], [0.75, 0.9], [1, 1]], smooth: true },
  boost: { points: [[0, 0], [0.6, 1], [1, 1]], smooth: false },
  deadzone: { points: [[0, 0], [0.15, 0], [1, 1]], smooth: false },
};
const round = (v) => Math.round(v * 1000) / 1000;
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
let clipboard = null;  // shared by every curve editor, so a curve can be pasted onto another

// Tangents of a monotone cubic through the points (Fritsch-Carlson).
function tangents(p) {
  const n = p.length;
  const d = [];
  for (let i = 0; i < n - 1; i++) d.push((p[i + 1][1] - p[i][1]) / (p[i + 1][0] - p[i][0]));
  const m = new Array(n);
  m[0] = d[0]; m[n - 1] = d[n - 2];
  for (let i = 1; i < n - 1; i++) m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
    if (s > 9) { const k = 3 / Math.sqrt(s); m[i] = k * a * d[i]; m[i + 1] = k * b * d[i]; }
  }
  return m;
}

// The curve's value at x; null is the identity.
export function evalCurve(curve, x) {
  if (!curve || !curve.points || curve.points.length < 2) return x;
  const p = curve.points, n = p.length;
  const m = curve.smooth ? tangents(p) : null;
  const end = (i, j) => (p[j][1] - p[i][1]) / (p[j][0] - p[i][0]);
  if (x <= p[0][0]) return p[0][1] + (x - p[0][0]) * (m ? m[0] : end(0, 1));
  if (x >= p[n - 1][0]) return p[n - 1][1] + (x - p[n - 1][0]) * (m ? m[n - 1] : end(n - 2, n - 1));
  let i = 0;
  while (i < n - 2 && x > p[i + 1][0]) i++;
  const [x0, y0] = p[i], [x1, y1] = p[i + 1], h = x1 - x0, s = (x - x0) / h;
  if (!m) return y0 + (y1 - y0) * s;
  const s2 = s * s, s3 = s2 * s;
  return (2 * s3 - 3 * s2 + 1) * y0 + (s3 - 2 * s2 + s) * h * m[i] + (-2 * s3 + 3 * s2) * y1 + (s3 - s2) * h * m[i + 1];
}

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c != null && c !== false));
  return node;
}

export class CurveEditor {
  // opts: {value, onChange(value|null), id (stable id prefix), title, marker: () => x|null,
  //        xLabel, yLabel, exportName}
  constructor(opts) {
    this.opts = opts;
    this.value = opts.value ? structuredClone(opts.value) : null;
    this.selected = null;
    this.drag = null;
    this.canvas = h("canvas", { class: "curve-canvas", "aria-hidden": "true" });
    this.list = h("div", { class: "curve-points" });
    const id = opts.id || "curve";
    const tool = (name, label, onclick, icon) => h("button", { type: "button", class: "small with-icon", id: `${id}-${name}`, title: label, onclick },
      icon ? h("span", { class: "ico", html: ICONS[icon] }) : null, h("span", { text: label }));
    this.smooth = h("input", { type: "checkbox", id: `${id}-smooth`, onchange: () => this.commit({ ...this.current(), smooth: this.smooth.checked }) });
    this.presets = h("div", { class: "seg marks curve-presets", role: "group", "aria-label": t("curve.presets") },
      Object.keys(CURVE_PRESETS).map((name) => h("button", { type: "button", "data-preset": name, text: t(`curve.preset.${name}`), "aria-pressed": "false",
        onclick: () => this.commit(structuredClone(CURVE_PRESETS[name])) })));
    this.node = h("div", { class: "curve-editor" },
      h("div", { class: "curve-main" }, this.canvas,
        h("div", { class: "curve-side" },
          h("label", { class: "chk", for: `${id}-smooth` }, this.smooth, h("span", { text: t("curve.smooth") })),
          this.list,
          h("button", { type: "button", class: "small", id: `${id}-add`, text: t("curve.addPoint"), onclick: () => this.addPoint() }))),
      this.presets,
      h("div", { class: "inline wrap curve-tools" },
        tool("copy", t("curve.copy"), () => { clipboard = structuredClone(this.current()); api.copy(JSON.stringify(clipboard)); this.sync(); }, "copy"),
        tool("paste", t("curve.paste"), () => { if (clipboard) this.commit(structuredClone(clipboard)); }, "paste"),
        tool("reset", t("curve.reset"), () => this.commit(null), "reset"),
        tool("import", t("curve.import"), async () => {
          const data = await api.json.open(t("curve.importTitle"));
          if (data && Array.isArray(data.points)) this.commit({ points: data.points, smooth: !!data.smooth });
        }),
        tool("export", t("curve.export"), () => api.json.save({ title: t("curve.exportTitle"), name: `${opts.exportName || "curve"}.curve.json`, data: this.current() }))));
    this.bindCanvas();
    new ResizeObserver(() => this.draw()).observe(this.canvas);
    this.sync();
  }

  current() { return this.value ? structuredClone(this.value) : structuredClone(IDENTITY); }

  set(value) { this.value = value ? structuredClone(value) : null; this.sync(); }

  // Identity (two end points on the diagonal, linear) is stored as null.
  commit(value) {
    if (value) {
      value.points = value.points.map(([x, y]) => [round(x), round(y)]).sort((a, b) => a[0] - b[0]);
      if (same(value.points, IDENTITY.points) && !value.smooth) value = null;
    }
    this.value = value;
    this.sync();
    this.opts.onChange(value ? structuredClone(value) : null);
  }

  sync() {
    const c = this.current();
    this.smooth.checked = !!c.smooth;
    for (const b of this.presets.querySelectorAll("button")) b.setAttribute("aria-pressed", String(same(c, CURVE_PRESETS[b.dataset.preset])));
    const paste = this.node?.querySelector(`[id$="-paste"]`);
    if (paste) paste.disabled = !clipboard;
    const id = this.opts.id || "curve";
    this.list.replaceChildren(...c.points.map(([x, y], i) => {
      const end = i === 0 || i === c.points.length - 1;
      const num = (axis, v) => h("input", { type: "number", class: "num", id: `${id}-p${i}${axis}`, value: fmt(v, 3), step: 0.01, disabled: axis === "x" && end,
        "aria-label": t(axis === "x" ? "curve.pointX" : "curve.pointY", { n: i + 1 }),
        onchange: (e) => this.setPoint(i, axis, Number(e.target.value)) });
      return h("div", { class: `curve-point${this.selected === i ? " selected" : ""}` },
        h("span", { class: "curve-point-n", text: String(i + 1) }), num("x", x), num("y", y),
        h("button", { type: "button", class: "icon tiny", html: ICONS.close, disabled: end, title: t("curve.deletePoint"), "aria-label": t("curve.deletePoint"),
          onclick: () => this.deletePoint(i) }));
    }));
    this.draw();
  }

  setPoint(i, axis, v) {
    const c = this.current();
    if (!Number.isFinite(v)) { this.sync(); return; }
    const p = c.points;
    if (axis === "x") {
      if (i === 0 || i === p.length - 1) return;
      p[i][0] = Math.min(Math.max(v, p[i - 1][0] + 0.005), p[i + 1][0] - 0.005);
    } else p[i][1] = v;
    this.commit(c);
  }

  addPoint() {
    const c = this.current();
    let best = 0;
    for (let i = 1; i < c.points.length - 1; i++) if (c.points[i + 1][0] - c.points[i][0] > c.points[best + 1][0] - c.points[best][0]) best = i;
    const x = (c.points[best][0] + c.points[best + 1][0]) / 2;
    c.points.splice(best + 1, 0, [x, evalCurve(c, x)]);
    this.selected = best + 1;
    this.commit(c);
  }

  deletePoint(i) {
    const c = this.current();
    if (i <= 0 || i >= c.points.length - 1) return;
    c.points.splice(i, 1);
    this.selected = null;
    this.commit(c);
  }

  geometry() {
    const w = this.canvas.clientWidth, hh = this.canvas.clientHeight, pad = 14;
    const size = Math.min(w, hh) - pad * 2;
    // y shows -0.25..1.25 so overshoot stays visible.
    return { pad, size, X: (x) => pad + x * size, Y: (y) => pad + (1 - (y + 0.25) / 1.5) * size,
             x: (px) => (px - pad) / size, y: (py) => 1.25 - (py - pad) / size * 1.5 };
  }

  bindCanvas() {
    const at = (e) => { const r = this.canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const hit = (px, py) => {
      const g = this.geometry();
      return this.current().points.findIndex(([x, y]) => Math.hypot(g.X(x) - px, g.Y(y) - py) <= 8);
    };
    this.canvas.addEventListener("pointerdown", (e) => {
      const [px, py] = at(e);
      const i = hit(px, py);
      const g = this.geometry();
      if (i >= 0 && (e.altKey || e.button === 2)) { this.deletePoint(i); return; }
      this.canvas.setPointerCapture(e.pointerId);
      if (i >= 0) { this.selected = i; this.drag = { i, base: this.current() }; this.sync(); return; }
      // Empty area: a new point under the pointer, then drag it.
      const c = this.current();
      const x = Math.min(Math.max(g.x(px), 0.005), 0.995);
      if (c.points.some(([px2]) => Math.abs(px2 - x) < 0.01)) return;
      c.points.push([x, Math.min(Math.max(g.y(py), -0.25), 1.25)]);
      c.points.sort((a, b) => a[0] - b[0]);
      const ni = c.points.findIndex(([px2]) => px2 === x);
      this.value = c; this.selected = ni; this.drag = { i: ni, base: structuredClone(c) };
      this.sync();
    });
    this.canvas.addEventListener("pointermove", (e) => {
      if (!this.drag) { const [px, py] = at(e); this.canvas.dataset.hover = hit(px, py) >= 0 ? "point" : ""; return; }
      const [px, py] = at(e);
      const g = this.geometry();
      const c = structuredClone(this.drag.base);
      const i = this.drag.i, p = c.points;
      if (i > 0 && i < p.length - 1) p[i][0] = Math.min(Math.max(g.x(px), p[i - 1][0] + 0.005), p[i + 1][0] - 0.005);
      p[i][1] = Math.min(Math.max(g.y(py), -0.25), 1.25);
      this.value = c; this.drag.moved = true;
      this.draw();
    });
    const up = () => { if (!this.drag) return; this.drag = null; this.commit(this.current()); };
    this.canvas.addEventListener("pointerup", up);
    this.canvas.addEventListener("pointercancel", up);
    this.canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  draw() {
    if (!this.canvas.isConnected || !this.canvas.clientWidth) return;
    const { ctx, w, h: hh } = fitCanvas(this.canvas);
    const th = graphTheme(this.canvas);
    const g = this.geometry();
    ctx.clearRect(0, 0, w, hh);
    ctx.fillStyle = th.bg; ctx.fillRect(0, 0, w, hh);
    ctx.strokeStyle = th.grid; ctx.lineWidth = 1;
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      ctx.strokeStyle = v === 0 || v === 1 ? th.gridStrong : th.grid;
      ctx.beginPath(); ctx.moveTo(g.X(0), g.Y(v)); ctx.lineTo(g.X(1), g.Y(v)); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(g.X(v), g.Y(-0.25)); ctx.lineTo(g.X(v), g.Y(1.25)); ctx.stroke();
    }
    ctx.setLineDash([3, 3]); ctx.strokeStyle = th.muted;
    ctx.beginPath(); ctx.moveTo(g.X(0), g.Y(0)); ctx.lineTo(g.X(1), g.Y(1)); ctx.stroke(); ctx.setLineDash([]);
    const c = this.current();
    ctx.strokeStyle = th.accent; ctx.lineWidth = 2;
    ctx.beginPath();
    for (let k = 0; k <= 120; k++) { const x = k / 120, y = evalCurve(c, x); if (k) ctx.lineTo(g.X(x), g.Y(y)); else ctx.moveTo(g.X(x), g.Y(y)); }
    ctx.stroke();
    const mx = this.opts.marker?.();
    if (mx != null && Number.isFinite(mx)) {
      const x = Math.min(Math.max(mx, -0.05), 1.05), y = evalCurve(c, x);
      ctx.strokeStyle = th.text; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(g.X(x), g.Y(-0.25)); ctx.lineTo(g.X(x), g.Y(1.25)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = th.text; ctx.beginPath(); ctx.arc(g.X(x), g.Y(y), 3.5, 0, Math.PI * 2); ctx.fill();
    }
    c.points.forEach(([x, y], i) => {
      ctx.fillStyle = i === this.selected ? th.accent : th.keyFill; ctx.strokeStyle = th.accent; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(g.X(x), g.Y(y), 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    });
    ctx.lineWidth = 1;
  }
}
