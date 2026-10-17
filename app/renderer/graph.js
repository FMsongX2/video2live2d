// Shared drawing primitives for Studio's graphs (the motion editor, the mapping editor's curve
// editors and signal graphs): theme colours from CSS tokens, grid steps, a HiDPI canvas, and a
// time-series graph with a playhead.

export const PALETTE = 8;

// Graph colours of the current theme, read from CSS custom properties on `el`.
export function graphTheme(el) {
  const s = getComputedStyle(el);
  const v = (n) => s.getPropertyValue(n).trim();
  return {
    bg: v("--graph-bg"), grid: v("--graph-grid"), gridStrong: v("--graph-grid-strong"), text: v("--text-secondary"),
    ruler: v("--bg-panel"), accent: v("--accent-visual"), out: v("--graph-outside"), fade: v("--graph-fade"),
    keyFill: v("--bg-raised"), selection: v("--accent-visual"), muted: v("--text-tertiary"),
    curves: Array.from({ length: PALETTE }, (_, i) => v(`--curve-${i}`)),
  };
}

// A grid step of 1, 2, 2.5 or 5 times a power of ten at least as large as `raw`.
export function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}

export const fmt = (v, d = 2) => (Number.isFinite(v) ? Number(v.toFixed(d)).toString() : "—");

// Sizes the canvas backing store to its CSS box and returns a context in CSS pixels.
export function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr)), h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: canvas.clientWidth, h: canvas.clientHeight };
}

// Horizontal value grid with labels in the left gutter.
export function valueGrid(ctx, g, y0, y1, yOf, theme, label = (v, step) => fmt(v, step < 0.1 ? 2 : 1)) {
  const step = niceStep((y1 - y0) / Math.max(g.gh / 34, 1));
  ctx.font = "11px -apple-system, 'SF Pro Text', sans-serif";
  ctx.textBaseline = "middle"; ctx.textAlign = "right";
  for (let v = Math.ceil(y0 / step) * step; v <= y1 + 1e-9; v += step) {
    const y = Math.round(yOf(v)) + 0.5;
    ctx.strokeStyle = Math.abs(v) < step / 2 ? theme.gridStrong : theme.grid;
    ctx.beginPath(); ctx.moveTo(g.gx, y); ctx.lineTo(g.gx + g.gw, y); ctx.stroke();
    ctx.fillStyle = theme.text; ctx.fillText(label(v, step), g.gx - 6, y);
  }
}

// Vertical playhead with a small flag at the top.
export function playhead(ctx, x, top, bottom, theme) {
  ctx.strokeStyle = theme.accent; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
  ctx.fillStyle = theme.accent;
  ctx.beginPath(); ctx.moveTo(x - 5, top); ctx.lineTo(x + 5, top); ctx.lineTo(x + 5, top + 8); ctx.lineTo(x, top + 13); ctx.lineTo(x - 5, top + 8); ctx.closePath(); ctx.fill();
  ctx.lineWidth = 1;
}

// Values over time (one sample per frame at `fps`) with a playhead; click or drag to move the
// playhead (onSeek), arrow keys move it by one frame.
export class SignalGraph {
  constructor(canvas, { onSeek } = {}) {
    this.canvas = canvas;
    this.series = [];       // [{values, color, width, dash, label}]
    this.fps = 30; this.duration = 0; this.range = [0, 1]; this.time = 0;
    this.onSeek = onSeek || (() => {});
    canvas.tabIndex = 0;
    const seek = (e) => {
      const r = canvas.getBoundingClientRect(), g = this.geometry();
      const t = (e.clientX - r.left - g.gx) / g.gw * this.duration;
      this.onSeek(Math.min(Math.max(t, 0), this.duration));
    };
    let down = false;
    canvas.addEventListener("pointerdown", (e) => { down = true; canvas.setPointerCapture(e.pointerId); seek(e); });
    canvas.addEventListener("pointermove", (e) => { if (down) seek(e); });
    canvas.addEventListener("pointerup", () => { down = false; });
    canvas.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const step = (e.shiftKey ? 10 : 1) / this.fps;
      this.onSeek(Math.min(Math.max(this.time + (e.key === "ArrowRight" ? step : -step), 0), this.duration));
    });
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  geometry() { const w = this.canvas.clientWidth, h = this.canvas.clientHeight; return { gx: 44, gy: 8, gw: Math.max(w - 54, 10), gh: Math.max(h - 26, 10), w, h }; }

  draw() {
    if (!this.canvas.isConnected || !this.canvas.clientWidth) return;
    const { ctx, w, h } = fitCanvas(this.canvas);
    const theme = graphTheme(this.canvas);
    const g = this.geometry();
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = theme.bg; ctx.fillRect(0, 0, w, h);
    let [lo, hi] = this.range;
    if (!(hi > lo)) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.06;
    lo -= pad; hi += pad;
    const xOf = (t) => g.gx + (this.duration ? t / this.duration : 0) * g.gw;
    const yOf = (v) => g.gy + g.gh - (v - lo) / (hi - lo) * g.gh;
    valueGrid(ctx, g, lo, hi, yOf, theme);
    // Time labels.
    ctx.textAlign = "left"; ctx.fillStyle = theme.text;
    const step = [0.25, 0.5, 1, 2, 5, 10, 30, 60].find((s) => s / Math.max(this.duration, 1e-6) * g.gw >= 60) || 60;
    for (let t = 0; t <= this.duration + 1e-9; t += step) ctx.fillText(`${fmt(t, 2)}s`, xOf(t) + 3, g.gy + g.gh + 10);
    ctx.save(); ctx.beginPath(); ctx.rect(g.gx, g.gy, g.gw, g.gh); ctx.clip();
    for (const s of this.series) {
      ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.5; ctx.setLineDash(s.dash || []);
      ctx.globalAlpha = s.alpha ?? 1;
      ctx.beginPath();
      let pen = false;
      s.values.forEach((v, i) => {
        if (v == null || !Number.isFinite(v)) { pen = false; return; }
        const x = xOf(i / this.fps), y = yOf(v);
        if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
      });
      ctx.stroke();
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    ctx.restore();
    if (this.duration) playhead(ctx, Math.round(xOf(this.time)) + 0.5, g.gy, g.gy + g.gh, theme);
  }
}
