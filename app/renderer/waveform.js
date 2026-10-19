// Audio views on a canvas: the waveform of an audio source with a playhead (Waveform), and the
// live input of a microphone while recording a voice take (LiveLevel). Colors come from the
// theme tokens in style.css and follow theme changes.

// Peaks are computed from a mono copy decoded at a low rate: enough for drawing, and a 10-minute
// file stays below 20 MB in memory.
const PEAK_RATE = 8000;
const BAR = 2, GAP = 1;   // CSS pixels per bar and between bars

function tokens(el) {
  const s = getComputedStyle(el);
  const v = (name) => s.getPropertyValue(name).trim();
  return { played: v("--accent-visual"), rest: v("--text-tertiary"), head: v("--text"), axis: v("--border"), dim: v("--bg-well") };
}

// Fits the canvas backing store to its CSS size; returns [width, height] in CSS pixels.
function fit(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [w, h, ctx];
}

function watchTheme(fn) {
  const observer = new MutationObserver(fn);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return observer;
}

export class Waveform {
  // media: the <audio> element that is the clock. The canvas is a slider: pointer drags and
  // arrow keys seek the media; onToggle plays or pauses (Space, Enter).
  constructor(canvas, media, { onToggle = () => {}, describe = (t) => `${t.toFixed(1)} s` } = {}) {
    this.canvas = canvas; this.media = media; this.onToggle = onToggle; this.describe = describe;
    this.samples = null; this.columns = null; this.duration = 0; this.range = null;
    this.drawn = ""; this.colors = tokens(canvas); this.request = 0;
    new ResizeObserver(() => { this.columns = null; this.tick(); }).observe(canvas);
    watchTheme(() => { this.colors = tokens(canvas); this.drawn = ""; this.tick(); });
    canvas.addEventListener("pointerdown", (e) => {
      if (!this.duration || e.button !== 0) return;
      canvas.setPointerCapture(e.pointerId); canvas.focus({ preventScroll: true });
      this.seekTo(this.timeAt(e.clientX));
    });
    canvas.addEventListener("pointermove", (e) => { if (canvas.hasPointerCapture(e.pointerId)) this.seekTo(this.timeAt(e.clientX)); });
    canvas.addEventListener("keydown", (e) => this.key(e));
  }

  // Decodes the file at url; resolves to false when a newer load replaced it.
  async load(url) {
    const request = ++this.request;
    this.samples = null; this.columns = null; this.duration = 0; this.drawn = ""; this.tick();
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const bytes = await response.arrayBuffer();
    const buffer = await new OfflineAudioContext(1, 1, PEAK_RATE).decodeAudioData(bytes);
    if (request !== this.request) return false;
    const mono = new Float32Array(buffer.length);
    for (let c = 0; c < buffer.numberOfChannels; c++) {
      const data = buffer.getChannelData(c);
      for (let i = 0; i < data.length; i++) mono[i] += data[i] / buffer.numberOfChannels;
    }
    this.samples = mono; this.duration = buffer.duration;
    this.canvas.setAttribute("aria-valuemax", this.duration.toFixed(1));
    this.tick();
    return true;
  }

  clear() { ++this.request; this.samples = null; this.columns = null; this.duration = 0; this.range = null; this.drawn = ""; this.tick(); }

  timeAt(clientX) {
    const r = this.canvas.getBoundingClientRect();
    return Math.min(Math.max((clientX - r.left) / Math.max(r.width, 1), 0), 1) * this.duration;
  }

  seekTo(t) {
    if (!this.duration) return;
    this.media.currentTime = Math.min(Math.max(t, 0), this.duration);
    this.tick();
  }

  key(e) {
    if (!this.duration) return;
    const now = this.media.currentTime, step = e.shiftKey ? 5 : 1;
    const to = { ArrowLeft: now - step, ArrowDown: now - step, ArrowRight: now + step, ArrowUp: now + step,
                 PageDown: now - 10, PageUp: now + 10, Home: 0, End: this.duration }[e.key];
    if (to !== undefined) { e.preventDefault(); this.seekTo(to); return; }
    if ((e.code === "Space" || e.key === "Enter") && !e.repeat) { e.preventDefault(); e.stopPropagation(); this.onToggle(); }
  }

  // Peak per bar column, scaled so the loudest bar fills the height.
  layout(width) {
    const n = Math.max(1, Math.floor(width / (BAR + GAP)));
    const peaks = new Float32Array(n);
    const per = this.samples.length / n;
    let max = 0;
    for (let i = 0; i < n; i++) {
      let p = 0;
      for (let j = Math.floor(i * per), end = Math.min(this.samples.length, Math.floor((i + 1) * per)); j < end; j++) {
        const v = Math.abs(this.samples[j]);
        if (v > p) p = v;
      }
      peaks[i] = p; if (p > max) max = p;
    }
    for (let i = 0; i < n; i++) peaks[i] = max > 0 ? Math.sqrt(peaks[i] / max) : 0;   // sqrt: quiet speech stays visible
    this.columns = peaks;
  }

  // Redraws when the playhead, the range, the size or the theme changed (call every frame).
  tick() {
    const c = this.canvas;
    if (!c.isConnected || !c.clientWidth) return;
    const t = this.media.currentTime || 0;
    const key = `${c.clientWidth}x${c.clientHeight}|${t.toFixed(3)}|${this.range?.join(",")}|${!!this.samples}`;
    if (key === this.drawn && this.columns) return;
    this.drawn = key;
    c.setAttribute("aria-valuenow", t.toFixed(1));
    c.setAttribute("aria-valuetext", this.describe(t, this.duration));
    const [w, h, ctx] = fit(c);
    ctx.clearRect(0, 0, w, h);
    const mid = h / 2;
    ctx.fillStyle = this.colors.axis;
    ctx.fillRect(0, Math.round(mid), w, 1);
    if (!this.samples) return;
    if (!this.columns || this.columns.length !== Math.max(1, Math.floor(w / (BAR + GAP)))) this.layout(w);
    const head = this.duration ? t / this.duration * w : 0;
    const [r0, r1] = this.range && this.duration ? this.range.map((x) => x / this.duration * w) : [0, w];
    for (let i = 0; i < this.columns.length; i++) {
      const x = i * (BAR + GAP);
      const amp = Math.max(1, this.columns[i] * (mid - 4));
      ctx.globalAlpha = x + BAR < r0 || x > r1 ? 0.35 : 1;
      ctx.fillStyle = x + BAR / 2 <= head ? this.colors.played : this.colors.rest;
      ctx.fillRect(x, mid - amp, BAR, amp * 2);
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = this.colors.head;
    ctx.fillRect(Math.min(Math.max(Math.round(head) - 1, 0), w - 2), 0, 2, h);
  }
}

// Scrolling input level of a microphone stream, plus the current level in dBFS for a meter.
export class LiveLevel {
  constructor(canvas, stream) {
    this.canvas = canvas;
    this.context = new AudioContext();
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 2048;
    this.context.createMediaStreamSource(stream).connect(this.analyser);
    this.buffer = new Float32Array(this.analyser.fftSize);
    this.history = [];
    this.colors = tokens(canvas);
    this.observer = watchTheme(() => { this.colors = tokens(canvas); });
  }

  // Reads the newest samples; returns the RMS level in dBFS (-Infinity for silence).
  read() {
    this.analyser.getFloatTimeDomainData(this.buffer);
    let sum = 0, peak = 0;
    for (const v of this.buffer) { sum += v * v; peak = Math.max(peak, Math.abs(v)); }
    this.history.push(peak);
    if (this.history.length > 600) this.history.shift();
    return 10 * Math.log10(sum / this.buffer.length);
  }

  draw(recording) {
    const c = this.canvas;
    if (!c.clientWidth) return;
    const [w, h, ctx] = fit(c);
    ctx.clearRect(0, 0, w, h);
    const mid = h / 2, n = Math.floor(w / (BAR + GAP));
    ctx.fillStyle = this.colors.axis;
    ctx.fillRect(0, Math.round(mid), w, 1);
    ctx.fillStyle = recording ? this.colors.played : this.colors.rest;
    const start = Math.max(0, this.history.length - n);
    for (let i = start; i < this.history.length; i++) {
      const amp = Math.max(1, Math.sqrt(Math.min(this.history[i], 1)) * (mid - 6));
      ctx.fillRect(w - (this.history.length - i) * (BAR + GAP), mid - amp, BAR, amp * 2);
    }
  }

  close() { this.observer.disconnect(); this.context.close().catch(() => {}); }
}
