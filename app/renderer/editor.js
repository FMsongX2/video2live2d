// Motion editor: a track list and value-over-time graph docked under the viewer, with an
// inspector in the right column. It edits the `v2l.motion-edit` form of a motion
// (docs/motion-editing.md); the viewer previews every edit at the playhead through the same
// motion3 evaluator used for playback. Python (`v2l.motionedit`) reads and writes files and runs
// the shared operations (interpolation, tangents, refit, scale, trim, ...); dragging, inserting
// and deleting keys happen here.
import { parseMotion } from "./motion.js";
import { clampTo, clone, controls, counts, deleteKeys, extent, insertKey, moveKeys, segmentsOf, setHandle, toMotion3, TYPES, valueAt } from "./curves.js";
import { ICONS } from "./icons.js";
import { msg, setAttr, setRaw, setText, t } from "./i18n.js";
import { errorText } from "./prefs.js";
import { fmt, graphTheme, niceStep, PALETTE, playhead } from "./graph.js";

const $ = (id) => document.getElementById(id);
const api = window.v2l;
const NAME_RE = /^[A-Za-z0-9_-]{1,80}$/;
const HISTORY = 200;
const GROUPS = ["head", "body", "eyes", "brows", "mouth", "other"];
const RULER = 24, GUTTER = 48, PAD = 10, HIT = 7;
const TEXT_FIELDS = "input:not([type=range]):not([type=checkbox]):not([type=radio]), textarea, select, [contenteditable]";
function groupOf(curve) {
  if (curve.target !== "Parameter") return "other";
  const id = curve.id;
  if (/Brow/i.test(id)) return "brows";
  if (/Eye|Pupil/i.test(id)) return "eyes";
  if (/Mouth|Lip|Cheek|Tongue|Jaw|Teeth/i.test(id)) return "mouth";
  if (/Body|Breath|Shoulder|Arm|Hand|Leg|Waist|Bust|Chest|Hip/i.test(id)) return "body";
  if (/Angle|Head|Face|Neck/i.test(id)) return "head";
  return "other";
}

function hash(text) { let h = 0; for (const c of text) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; }
const keyOf = (curve) => `${curve.target}:${curve.id}`;

export class MotionEditor {
  // host: {viewer, time(), seek(t), togglePlay(), setPreview(parsed|null), onSaved(saved, source),
  //        onClose(), status(key, params, tone), modelDir()}
  constructor(host) {
    this.host = host;
    this.doc = null;
    this.source = null;            // {file, name, dir, entryKey, video, inModel}
    this.history = []; this.index = -1; this.savedId = 0; this.ids = 0;
    this.ranges = new Map();       // curve key -> [min, max] (null when the model lacks it)
    this.selected = new Set();     // curve keys shown and editable in the graph
    this.visible = new Set();      // curve keys drawn as references
    this.keys = new Map();         // curve key -> Set(key index)
    this.colors = new Map();       // curve key -> palette index
    this.collapsed = new Set();
    this.anchor = null;            // track list: range-selection anchor
    this.view = { t0: 0, t1: 1, y0: 0, y1: 1 };
    this.normalized = null;        // null: automatic
    this.snap = true;
    this.drag = null;
    this.busy = false;
    this.needsDraw = true;
    this.lastTime = -1;
    this.lastValues = 0;
    this.theme = {};
    this.bind();
  }

  get open() { return !!this.doc; }
  get dirty() { return this.open && this.history[this.index]?.id !== this.savedId; }
  curve(key) { return this.doc?.curves.find((c) => keyOf(c) === key) || null; }
  range(key) { return this.ranges.get(key) || null; }
  frame() { return 1 / (this.doc?.meta.fps || 30); }

  // ------------------------------------------------------------ open / close

  async openMotion(source) {
    if (this.dirty && !await this.confirmDiscard()) return false;
    this.host.status("editor.opening", { name: source.name }, "busy");
    const read = await api.motion.read(source.file);
    this.source = { ...source, file: read.file, name: read.name, dir: read.dir };
    this.doc = read.motion;
    this.history = [{ id: ++this.ids, doc: clone(this.doc) }]; this.index = 0; this.savedId = this.ids;
    this.keys = new Map(); this.selected = new Set(); this.visible = new Set();
    this.loadRanges();
    for (const c of this.doc.curves) if (!this.colors.has(keyOf(c))) this.colors.set(keyOf(c), hash(c.id) % PALETTE);
    // Start with the head angles selected (or the first curve), as a sensible first view.
    const first = this.doc.curves.find((c) => /AngleX$/.test(c.id)) || this.doc.curves[0];
    if (first) { this.selected.add(keyOf(first)); this.anchor = keyOf(first); }
    document.body.classList.add("editing");
    $("editor-dock").classList.remove("hidden"); $("editor-panel").classList.remove("hidden");
    this.adoptVideo(true);
    $("save-name").value = `${this.source.name}_edit`.slice(0, 80); $("save-name").removeAttribute("aria-invalid");
    setRaw($("editor-name"), this.source.name); $("editor-name").title = this.source.file;
    setRaw($("editor-file"), this.source.file);
    this.renderTracks();
    this.fit();
    this.changed({ meta: true });
    this.host.status("editor.opened", { name: this.source.name }, "success");
    requestAnimationFrame(() => this.resize());
    return true;
  }

  async close({ force = false } = {}) {
    if (!this.open) return true;
    if (!force && this.dirty && !await this.confirmDiscard()) return false;
    this.doc = null; this.source = null; this.history = []; this.index = -1;
    document.body.classList.remove("editing");
    $("editor-dock").classList.add("hidden"); $("editor-panel").classList.add("hidden");
    $("editor-help").hidePopover?.();
    this.adoptVideo(false);
    this.host.onClose();
    return true;
  }

  confirmDiscard() {
    return api.confirm({ message: t("editor.discardMessage", { name: this.source?.name || "" }), detail: t("editor.discardDetail"), confirmLabel: t("editor.discard") });
  }

  // The source video moves into the inspector while editing, so it stays visible.
  adoptVideo(on) {
    const video = $("video");
    if (on && this.source?.video) { $("editor-video").append(video); $("editor-video").classList.remove("hidden"); }
    else if (!on && video.parentElement !== $("drop")) { $("drop").prepend(video); $("editor-video").classList.add("hidden"); }
    else if (on) $("editor-video").classList.add("hidden");
  }

  loadRanges() {
    const { params, parts } = this.host.viewer.parameters();
    const partSet = new Set(parts);
    this.ranges = new Map();
    for (const c of this.doc.curves) {
      let r = null;
      if (c.target === "Parameter" && params.has(c.id)) r = [params.get(c.id).min, params.get(c.id).max];
      else if (c.target === "PartOpacity" && partSet.has(c.id)) r = [0, 1];
      else if (c.target === "Model") r = [0, 1];
      this.ranges.set(keyOf(c), r);
    }
  }

  // Ranges for Python ({id: [min, max]}), so refit tolerance and clamping use the model's limits.
  rangesObject() {
    const out = {};
    for (const c of this.doc.curves) { const r = this.range(keyOf(c)); if (r && c.target === "Parameter") out[c.id] = r; }
    return out;
  }

  // Display range of a curve: the parameter's limits, or its own extent when the model lacks it.
  span(key) {
    const r = this.range(key);
    if (r && r[1] > r[0]) return r;
    const c = this.curve(key);
    const [lo, hi] = c ? extent(this.doc, c) : [0, 1];
    return hi - lo > 1e-6 ? [lo, hi] : [lo - 0.5, hi + 0.5];
  }

  // ------------------------------------------------------------ history

  commit(doc, { keepKeys = true } = {}) {
    this.history = this.history.slice(0, this.index + 1);
    this.history.push({ id: ++this.ids, doc: clone(doc) });
    if (this.history.length > HISTORY) this.history.shift();
    this.index = this.history.length - 1;
    this.doc = doc;
    if (!keepKeys) this.keys = new Map();
    this.changed({ meta: true });
  }

  undo() { if (this.index > 0) this.restore(this.index - 1); }
  redo() { if (this.index < this.history.length - 1) this.restore(this.index + 1); }
  restore(i) {
    this.index = i;
    this.doc = clone(this.history[i].doc);
    for (const [key, set] of this.keys) {
      const c = this.curve(key);
      if (!c) { this.keys.delete(key); continue; }
      for (const k of set) if (k >= c.keys.length) set.delete(k);
    }
    const known = new Set(this.doc.curves.map(keyOf));
    for (const k of [...this.selected]) if (!known.has(k)) this.selected.delete(k);
    this.renderTracks();
    this.changed({ meta: true });
  }

  // ------------------------------------------------------------ change propagation

  changed({ meta = false } = {}) {
    if (!this.doc) return;
    this.host.setPreview(parseMotion(toMotion3(this.doc)));
    this.updateControls();
    if (meta) this.fillMeta();
    this.updateValues(true);
    this.needsDraw = true;
  }

  selectionSegments() {
    const out = [];
    for (const key of this.selected) {
      const c = this.curve(key);
      if (!c) continue;
      const ks = [...(this.keys.get(key) || [])].sort((a, b) => a - b);
      const segs = ks.length ? segmentsOf(c, ks) : c.segments.map((_, i) => i);
      if (segs.length) out.push({ curve: c, segs });
    }
    return out;
  }

  selectedKeyCount() { let n = 0; for (const [k, s] of this.keys) if (this.selected.has(k)) n += s.size; return n; }

  updateControls() {
    const has = this.selected.size > 0;
    const keyCount = this.selectedKeyCount();
    $("ed-undo").disabled = this.index <= 0 || this.busy;
    $("ed-redo").disabled = this.index >= this.history.length - 1 || this.busy;
    const segs = this.selectionSegments();
    const types = new Set(segs.flatMap(({ curve, segs: s }) => s.map((i) => curve.segments[i].type)));
    for (const type of TYPES) {
      const b = $(`ed-type-${type}`);
      b.disabled = !segs.length || this.busy;
      b.setAttribute("aria-pressed", String(types.size === 1 && types.has(type)));
    }
    for (const id of ["ed-auto", "ed-flat", "ed-insert", "refit", "scale-apply", "offset-apply"]) $(id).disabled = !has || this.busy;
    $("ed-delete").disabled = !keyCount || this.busy;
    for (const id of ["trim-apply", "retime-apply", "save-new", "save-overwrite", "save-model", "save-revert"]) $(id).disabled = this.busy;
    $("save-revert").disabled = this.busy || !this.dirty;
    $("save-model").classList.toggle("hidden", !!this.source?.inModel || !this.host.modelDir());
    $("ed-snap").setAttribute("aria-pressed", String(this.snap));
    $("ed-normalize").setAttribute("aria-pressed", String(this.isNormalized()));
    // Selection inspector: one key gets time and value fields.
    const one = keyCount === 1 ? this.singleKey() : null;
    $("key-time").disabled = $("key-value").disabled = !one || this.busy;
    if (one) {
      if (document.activeElement !== $("key-time")) $("key-time").value = fmt(one.curve.keys[one.index][0], 4);
      if (document.activeElement !== $("key-value")) $("key-value").value = fmt(one.curve.keys[one.index][1], 4);
      const r = this.range(one.key);
      $("key-value").min = r ? r[0] : ""; $("key-value").max = r ? r[1] : "";
    } else { $("key-time").value = ""; $("key-value").value = ""; }
    // Bezier handles next to one key: the incoming segment's c2 and the outgoing segment's c1.
    for (const [id, seg, n] of [["handle-in", one ? one.index - 1 : -1, 2], ["handle-out", one ? one.index : -1, 1]]) {
      const bez = one && seg >= 0 && seg < one.curve.segments.length && one.curve.segments[seg].type === "bezier";
      $(id).disabled = !bez || this.busy;
      if (!bez) $(id).value = "";
      else if (document.activeElement !== $(id)) $(id).value = fmt(controls(this.doc, one.curve, seg)[n - 1][1], 4);
    }
    const curvesWithKeys = [...this.keys].filter(([k, s]) => this.selected.has(k) && s.size).length;
    if (keyCount) setText($("sel-summary"), "editor.selKeys", { keys: keyCount, curves: curvesWithKeys });
    else if (has) setText($("sel-summary"), "editor.selCurves", { curves: this.selected.size });
    else setText($("sel-summary"), "editor.selNone");
    const dirty = this.dirty;
    $("editor-dock").dataset.dirty = String(dirty); $("editor-panel").dataset.dirty = String(dirty);
    setText($("editor-dirty"), dirty ? "editor.unsaved" : "editor.saved");
    setText($("dock-dirty"), dirty ? "editor.unsaved" : "editor.saved");
    const c = counts(this.doc);
    setText($("editor-counts"), "editor.counts", c);
    $("graph-empty").classList.toggle("hidden", has);
  }

  singleKey() {
    for (const [key, set] of this.keys) {
      if (!this.selected.has(key) || !set.size) continue;
      return { key, curve: this.curve(key), index: [...set][0] };
    }
    return null;
  }

  fillMeta() {
    const m = this.doc.meta;
    const set = (id, v) => { if (document.activeElement !== $(id)) $(id).value = v; };
    set("meta-duration", fmt(m.duration, 3)); set("meta-fps", fmt(m.fps, 2));
    set("meta-fade-in", fmt(m.fade_in ?? 0, 2)); set("meta-fade-out", fmt(m.fade_out ?? 0, 2));
    $("meta-loop").checked = !!m.loop;
    set("trim-from", "0"); set("trim-to", fmt(m.duration, 3));
  }

  // Track rows show the value at the playhead (10 updates per second at most).
  updateValues(force = false) {
    const now = performance.now();
    if (!force && now - this.lastValues < 100) return;
    this.lastValues = now;
    const time = this.host.time();
    for (const row of $("track-list").querySelectorAll(".track")) {
      const c = this.curve(row.dataset.key);
      if (c) row.querySelector(".track-value").textContent = fmt(valueAt(this.doc, c, time), 2);
    }
    const f = Math.round(time / this.frame());
    setText($("ed-time"), "editor.timeReadout", { time: time.toFixed(2), frame: f });
  }

  // ------------------------------------------------------------ track list

  renderTracks() {
    const list = $("track-list");
    const q = $("track-search").value.trim().toLowerCase();
    const groups = new Map(GROUPS.map((g) => [g, []]));
    for (const c of this.doc.curves) if (!q || c.id.toLowerCase().includes(q)) groups.get(groupOf(c)).push(c);
    const nodes = [];
    for (const [g, curves] of groups) {
      if (!curves.length) continue;
      const collapsed = this.collapsed.has(g) && !q;
      const head = document.createElement("div");
      head.className = "track-group"; head.setAttribute("role", "presentation");
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "group-toggle"; toggle.tabIndex = -1;
      toggle.setAttribute("aria-expanded", String(!collapsed));
      setText(toggle, "editor.group", { name: msg(`editor.group.${g}`), count: curves.length });
      toggle.addEventListener("click", () => { if (this.collapsed.has(g)) this.collapsed.delete(g); else this.collapsed.add(g); this.renderTracks(); });
      const all = document.createElement("button");
      all.type = "button"; all.className = "icon tiny"; all.tabIndex = -1;
      const shown = curves.every((c) => this.selected.has(keyOf(c)) || this.visible.has(keyOf(c)));
      all.innerHTML = shown ? ICONS.eye : ICONS.eyeOff;
      setAttr(all, "title", shown ? "editor.hideGroup" : "editor.showGroup"); setAttr(all, "aria-label", shown ? "editor.hideGroup" : "editor.showGroup");
      all.addEventListener("click", () => {
        for (const c of curves) { if (shown) { this.visible.delete(keyOf(c)); this.selected.delete(keyOf(c)); } else this.visible.add(keyOf(c)); }
        this.renderTracks(); this.changed();
      });
      head.append(toggle, all);
      const box = document.createElement("div");
      box.setAttribute("role", "group"); box.setAttribute("aria-label", t(`editor.group.${g}`));
      if (!collapsed) for (const c of curves) box.append(this.trackRow(c));
      nodes.push(head, box);
    }
    if (!nodes.length) { const p = document.createElement("p"); p.className = "track-empty"; setText(p, "editor.noTracks"); nodes.push(p); }
    const focused = document.activeElement?.closest?.(".track")?.dataset.key;
    list.replaceChildren(...nodes);
    if (focused) list.querySelector(`.track[data-key="${CSS.escape(focused)}"]`)?.focus();
    this.updateValues(true);
  }

  trackRow(c) {
    const key = keyOf(c);
    const row = document.createElement("div");
    const sel = this.selected.has(key), vis = sel || this.visible.has(key);
    row.className = `track${sel ? " selected" : ""}${vis ? "" : " off"}`;
    row.dataset.key = key;
    row.setAttribute("role", "option"); row.setAttribute("aria-selected", String(sel));
    row.tabIndex = key === (this.anchor || keyOf(this.doc.curves[0])) ? 0 : -1;
    row.style.setProperty("--curve", `var(--curve-${this.colors.get(key) ?? 0})`);
    const swatch = document.createElement("button");
    swatch.type = "button"; swatch.className = "swatch"; swatch.tabIndex = -1;
    setAttr(swatch, "title", "editor.color"); setAttr(swatch, "aria-label", "editor.color");
    swatch.addEventListener("click", (e) => { e.stopPropagation(); this.colors.set(key, ((this.colors.get(key) ?? 0) + 1) % PALETTE); this.renderTracks(); this.needsDraw = true; });
    const name = document.createElement("span");
    name.className = "track-name"; name.textContent = c.id;
    const r = this.range(key);
    name.title = r ? `${c.id}  ${fmt(r[0])} … ${fmt(r[1])}` : `${c.id}  ${t("editor.notInModel")}`;
    if (!r) name.classList.add("unknown");
    const value = document.createElement("span"); value.className = "track-value";
    const eye = document.createElement("button");
    eye.type = "button"; eye.className = "icon tiny eye"; eye.tabIndex = -1;
    eye.innerHTML = vis ? ICONS.eye : ICONS.eyeOff;
    setAttr(eye, "title", vis ? "editor.hide" : "editor.show"); setAttr(eye, "aria-label", vis ? "editor.hide" : "editor.show");
    eye.addEventListener("click", (e) => { e.stopPropagation(); this.toggleVisible(key); });
    row.append(swatch, name, value, eye);
    row.addEventListener("click", (e) => this.pickTrack(key, e));
    return row;
  }

  toggleVisible(key) {
    if (this.selected.has(key) || this.visible.has(key)) { this.selected.delete(key); this.visible.delete(key); this.keys.delete(key); }
    else this.visible.add(key);
    this.renderTracks(); this.changed();
  }

  // Click: only this track; Cmd/Ctrl-click: toggle; Shift-click: range from the anchor.
  pickTrack(key, e) {
    const order = [...$("track-list").querySelectorAll(".track")].map((r) => r.dataset.key);
    if (e.shiftKey && this.anchor && order.includes(this.anchor)) {
      const [a, b] = [order.indexOf(this.anchor), order.indexOf(key)].sort((x, y) => x - y);
      if (!(e.metaKey || e.ctrlKey)) this.selected.clear();
      for (const k of order.slice(a, b + 1)) this.selected.add(k);
    } else if (e.metaKey || e.ctrlKey) {
      if (this.selected.has(key)) this.selected.delete(key); else this.selected.add(key);
      this.anchor = key;
    } else {
      this.selected = new Set([key]); this.anchor = key;
    }
    for (const k of [...this.keys.keys()]) if (!this.selected.has(k)) this.keys.delete(k);
    this.renderTracks();
    if (this.selected.size) this.fit({ time: false });
    this.changed();
  }

  trackKeydown(e) {
    const rows = [...$("track-list").querySelectorAll(".track")];
    const i = rows.indexOf(e.target.closest?.(".track"));
    if (i < 0) return;
    const go = (j) => { const r = rows[clampTo(j, 0, rows.length - 1)]; for (const x of rows) x.tabIndex = -1; r.tabIndex = 0; r.focus(); return r; };
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const r = go(i + (e.key === "ArrowDown" ? 1 : -1));
      if (e.shiftKey) { this.selected.add(r.dataset.key); this.renderTracks(); this.changed(); }
      else if (!(e.metaKey || e.ctrlKey)) this.pickTrack(r.dataset.key, {});
    } else if (e.key === "Home" || e.key === "End") { e.preventDefault(); go(e.key === "Home" ? 0 : rows.length - 1); }
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); this.pickTrack(rows[i].dataset.key, { metaKey: e.key === " " }); }
    else if (e.key.toLowerCase() === "h" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); this.toggleVisible(rows[i].dataset.key); }
  }

  // ------------------------------------------------------------ view

  isNormalized() {
    if (this.normalized !== null) return this.normalized;
    const spans = [...this.shown()].map((k) => this.span(k).join(","));
    return new Set(spans).size > 1;
  }

  // Curves drawn: selected (editable) first, then visible references.
  shown() { return new Set([...this.selected, ...this.visible].filter((k) => this.curve(k))); }

  toDisplay(key, v) {
    if (!this.isNormalized()) return v;
    const [lo, hi] = this.span(key);
    return (v - lo) / (hi - lo);
  }
  fromDisplay(key, d) {
    if (!this.isNormalized()) return d;
    const [lo, hi] = this.span(key);
    return lo + d * (hi - lo);
  }
  displayScale(key) { if (!this.isNormalized()) return 1; const [lo, hi] = this.span(key); return hi - lo; }

  fit({ time = true, value = true } = {}) {
    if (!this.doc) return;
    if (time) { const pad = this.doc.meta.duration * 0.02; this.view.t0 = -pad; this.view.t1 = this.doc.meta.duration + pad; }
    if (value) {
      let lo = Infinity, hi = -Infinity;
      const keys = this.selected.size ? this.selected : this.shown();
      for (const k of keys) {
        const c = this.curve(k); if (!c) continue;
        const r = this.range(k);
        const [a, b] = r ? [r[0], r[1]] : extent(this.doc, c);
        lo = Math.min(lo, this.toDisplay(k, a)); hi = Math.max(hi, this.toDisplay(k, b));
      }
      if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
      if (hi - lo < 1e-6) { lo -= 0.5; hi += 0.5; }
      const pad = (hi - lo) * 0.08;
      this.view.y0 = lo - pad; this.view.y1 = hi + pad;
    }
    this.needsDraw = true;
  }

  geometry() {
    const c = $("graph");
    const w = c.clientWidth, h = c.clientHeight;
    return { w, h, gx: GUTTER, gy: RULER, gw: Math.max(w - GUTTER - PAD, 10), gh: Math.max(h - RULER - PAD, 10) };
  }
  xOf(t, g) { return g.gx + (t - this.view.t0) / (this.view.t1 - this.view.t0) * g.gw; }
  tOf(x, g) { return this.view.t0 + (x - g.gx) / g.gw * (this.view.t1 - this.view.t0); }
  yOf(d, g) { return g.gy + g.gh - (d - this.view.y0) / (this.view.y1 - this.view.y0) * g.gh; }
  dOf(y, g) { return this.view.y0 + (g.gy + g.gh - y) / g.gh * (this.view.y1 - this.view.y0); }
  snapTime(t) { if (!this.snap) return t; const f = this.frame(); return Math.round(t / f) * f; }

  zoom(axis, factor, at) {
    const v = this.view;
    if (axis === "t") {
      const span = clampTo((v.t1 - v.t0) * factor, this.frame() * 4, Math.max(this.doc.meta.duration * 4, 1));
      const k = (at - v.t0) / (v.t1 - v.t0);
      v.t0 = at - k * span; v.t1 = v.t0 + span;
    } else {
      const span = clampTo((v.y1 - v.y0) * factor, 1e-4, 1e6);
      const k = (at - v.y0) / (v.y1 - v.y0);
      v.y0 = at - k * span; v.y1 = v.y0 + span;
    }
    this.needsDraw = true;
  }

  resize() {
    const c = $("graph");
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.max(1, Math.round(c.clientWidth * dpr)); c.height = Math.max(1, Math.round(c.clientHeight * dpr));
    this.needsDraw = true;
  }

  readTheme() {
    this.theme = graphTheme($("editor-dock"));
    this.needsDraw = true;
  }

  // ------------------------------------------------------------ drawing

  tick() {
    if (!this.open) return;
    const time = this.host.time();
    if (Math.abs(time - this.lastTime) > 1e-6) { this.lastTime = time; this.needsDraw = true; this.updateValues(); }
    if (this.needsDraw) { this.needsDraw = false; this.draw(); }
  }

  draw() {
    const c = $("graph");
    const ctx = c.getContext("2d");
    const dpr = c.width / Math.max(c.clientWidth, 1);
    const g = this.geometry();
    const th = this.theme;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, g.w, g.h);
    ctx.fillStyle = th.bg; ctx.fillRect(0, 0, g.w, g.h);
    ctx.font = "11px -apple-system, 'SF Pro Text', sans-serif";
    ctx.textBaseline = "middle";
    const m = this.doc.meta;
    // Outside the motion.
    ctx.fillStyle = th.out;
    const xs = this.xOf(0, g), xe = this.xOf(m.duration, g);
    if (xs > g.gx) ctx.fillRect(g.gx, g.gy, xs - g.gx, g.gh);
    if (xe < g.gx + g.gw) ctx.fillRect(xe, g.gy, g.gx + g.gw - xe, g.gh);
    // Value grid.
    const norm = this.isNormalized();
    const vstep = niceStep((this.view.y1 - this.view.y0) / Math.max(g.gh / 34, 1));
    ctx.textAlign = "right";
    for (let d = Math.ceil(this.view.y0 / vstep) * vstep; d <= this.view.y1; d += vstep) {
      const y = Math.round(this.yOf(d, g)) + 0.5;
      ctx.strokeStyle = Math.abs(d) < vstep / 2 ? th.gridStrong : th.grid;
      ctx.beginPath(); ctx.moveTo(g.gx, y); ctx.lineTo(g.gx + g.gw, y); ctx.stroke();
      ctx.fillStyle = th.text;
      ctx.fillText(norm ? `${Math.round(d * 100)}%` : fmt(d, vstep < 0.1 ? 2 : 1), g.gx - 6, y);
    }
    if (norm) {
      ctx.strokeStyle = th.gridStrong; ctx.setLineDash([4, 4]);
      for (const d of [0, 1]) { const y = Math.round(this.yOf(d, g)) + 0.5; ctx.beginPath(); ctx.moveTo(g.gx, y); ctx.lineTo(g.gx + g.gw, y); ctx.stroke(); }
      ctx.setLineDash([]);
    }
    // Time grid and ruler.
    ctx.fillStyle = th.ruler; ctx.fillRect(0, 0, g.w, RULER);
    const f = this.frame();
    const pxPerSec = g.gw / (this.view.t1 - this.view.t0);
    if (f * pxPerSec >= 7) {
      ctx.strokeStyle = th.grid; ctx.globalAlpha = 0.5;
      for (let k = Math.ceil(Math.max(this.view.t0, 0) / f); k * f <= Math.min(this.view.t1, m.duration); k++) {
        const x = Math.round(this.xOf(k * f, g)) + 0.5; ctx.beginPath(); ctx.moveTo(x, g.gy); ctx.lineTo(x, g.gy + g.gh); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    const tstep = [f, 2 * f, 5 * f, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 30].find((s) => s * pxPerSec >= 64) || 60;
    ctx.textAlign = "left";
    for (let s = Math.ceil(this.view.t0 / tstep) * tstep; s <= this.view.t1; s += tstep) {
      const x = Math.round(this.xOf(s, g)) + 0.5;
      ctx.strokeStyle = th.grid; ctx.beginPath(); ctx.moveTo(x, RULER - 6); ctx.lineTo(x, g.gy + g.gh); ctx.stroke();
      ctx.fillStyle = th.text;
      ctx.fillText(tstep < 0.1 ? `${Math.round(s / f)}f` : `${fmt(s, 2)}s`, x + 3, RULER / 2);
    }
    // Fades in the ruler.
    ctx.fillStyle = th.fade;
    if (m.fade_in > 0) ctx.fillRect(xs, RULER - 4, this.xOf(m.fade_in, g) - xs, 4);
    if (m.fade_out > 0) ctx.fillRect(this.xOf(m.duration - m.fade_out, g), RULER - 4, xe - this.xOf(m.duration - m.fade_out, g), 4);
    ctx.save();
    ctx.beginPath(); ctx.rect(g.gx, g.gy, g.gw, g.gh); ctx.clip();
    // Curves: references first, then the editable ones.
    const shown = [...this.shown()].sort((a, b) => Number(this.selected.has(a)) - Number(this.selected.has(b)));
    for (const key of shown) this.drawCurve(ctx, g, key, this.selected.has(key));
    for (const key of shown) if (this.selected.has(key)) this.drawKeys(ctx, g, key);
    if (this.drag?.kind === "box") {
      const b = this.drag;
      ctx.fillStyle = th.selection; ctx.globalAlpha = 0.12;
      ctx.fillRect(Math.min(b.x0, b.x1), Math.min(b.y0, b.y1), Math.abs(b.x1 - b.x0), Math.abs(b.y1 - b.y0));
      ctx.globalAlpha = 1; ctx.strokeStyle = th.selection; ctx.lineWidth = 1;
      ctx.strokeRect(Math.min(b.x0, b.x1) + 0.5, Math.min(b.y0, b.y1) + 0.5, Math.abs(b.x1 - b.x0), Math.abs(b.y1 - b.y0));
    }
    ctx.restore();
    // Playhead.
    const x = Math.round(this.xOf(this.host.time(), g)) + 0.5;
    if (x >= g.gx && x <= g.gx + g.gw) playhead(ctx, x, 2, g.gy + g.gh, th);
  }

  color(key) { return this.theme.curves[this.colors.get(key) ?? 0]; }

  drawCurve(ctx, g, key, editable) {
    const c = this.curve(key);
    const P = (t, v) => [this.xOf(t, g), this.yOf(this.toDisplay(key, v), g)];
    ctx.strokeStyle = this.color(key);
    ctx.lineWidth = editable ? 2 : 1.25;
    ctx.globalAlpha = editable ? 1 : 0.45;
    ctx.beginPath();
    const [x0, y0] = P(...c.keys[0]);
    ctx.moveTo(g.gx, y0); ctx.lineTo(x0, y0);
    c.segments.forEach((seg, i) => {
      const [ta, va] = c.keys[i], [tb, vb] = c.keys[i + 1];
      const [xa, ya] = P(ta, va), [xb, yb] = P(tb, vb);
      if (seg.type === "bezier") {
        const [c1, c2] = controls(this.doc, c, i);
        const [x1, y1] = P(...c1), [x2, y2] = P(...c2);
        ctx.bezierCurveTo(x1, y1, x2, y2, xb, yb);
      } else if (seg.type === "stepped") { ctx.lineTo(xb, ya); ctx.lineTo(xb, yb); }
      else if (seg.type === "inverse_stepped") { ctx.lineTo(xa, yb); ctx.lineTo(xb, yb); }
      else ctx.lineTo(xb, yb);
    });
    const last = c.keys[c.keys.length - 1];
    ctx.lineTo(g.gx + g.gw, P(...last)[1]);
    ctx.stroke();
    ctx.globalAlpha = 1; ctx.lineWidth = 1;
  }

  drawKeys(ctx, g, key) {
    const c = this.curve(key);
    const sel = this.keys.get(key) || new Set();
    const color = this.color(key);
    const P = (t, v) => [this.xOf(t, g), this.yOf(this.toDisplay(key, v), g)];
    // Handles of bezier segments next to selected keys (values only; times are fixed thirds).
    ctx.setLineDash([3, 3]);
    for (const i of sel) {
      for (const s of [i - 1, i]) {
        if (s < 0 || s >= c.segments.length || c.segments[s].type !== "bezier") continue;
        const [c1, c2] = controls(this.doc, c, s);
        for (const [cp, end] of [[c1, c.keys[s]], [c2, c.keys[s + 1]]]) {
          const [hx, hy] = P(...cp), [kx, ky] = P(...end);
          ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(kx, ky); ctx.lineTo(hx, hy); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = this.theme.keyFill; ctx.beginPath(); ctx.arc(hx, hy, 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
          ctx.setLineDash([3, 3]);
        }
      }
    }
    ctx.setLineDash([]);
    const dense = c.keys.length > 1 && (this.xOf(c.keys[c.keys.length - 1][0], g) - this.xOf(c.keys[0][0], g)) / c.keys.length < 4;
    c.keys.forEach(([t, v], i) => {
      const [x, y] = P(t, v);
      if (x < g.gx - 8 || x > g.gx + g.gw + 8) return;
      const on = sel.has(i);
      if (dense && !on) return;
      const r = on ? 5 : 4;
      ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
      ctx.fillStyle = on ? color : this.theme.keyFill; ctx.fill();
      ctx.strokeStyle = on ? this.theme.keyFill : color; ctx.lineWidth = on ? 1.5 : 1.25; ctx.stroke();
      if (on) { ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(x, y, r + 2.5, 0, Math.PI * 2); ctx.stroke(); }
    });
    ctx.lineWidth = 1;
  }

  // ------------------------------------------------------------ hit testing

  hitKey(x, y, g) {
    let best = null, bestD = HIT;
    for (const key of this.selected) {
      const c = this.curve(key); if (!c) continue;
      c.keys.forEach(([t, v], i) => {
        const d = Math.hypot(this.xOf(t, g) - x, this.yOf(this.toDisplay(key, v), g) - y);
        if (d <= bestD) { bestD = d; best = { key, index: i }; }
      });
    }
    return best;
  }

  hitHandle(x, y, g) {
    for (const key of this.selected) {
      const c = this.curve(key); const sel = this.keys.get(key); if (!c || !sel) continue;
      for (const i of sel) {
        for (const s of [i - 1, i]) {
          if (s < 0 || s >= c.segments.length || c.segments[s].type !== "bezier") continue;
          const [c1, c2] = controls(this.doc, c, s);
          for (const [n, cp] of [[1, c1], [2, c2]]) {
            if (Math.hypot(this.xOf(cp[0], g) - x, this.yOf(this.toDisplay(key, cp[1]), g) - y) <= HIT) return { key, seg: s, n };
          }
        }
      }
    }
    return null;
  }

  // Nearest shown curve at x within a few pixels (references included, so a click selects them).
  hitCurve(x, y, g, { editableOnly = false } = {}) {
    const time = this.tOf(x, g);
    let best = null, bestD = 10;
    for (const key of this.shown()) {
      if (editableOnly && !this.selected.has(key)) continue;
      const d = Math.abs(this.yOf(this.toDisplay(key, valueAt(this.doc, this.curve(key), time)), g) - y);
      if (d < bestD) { bestD = d; best = key; }
    }
    return best;
  }

  // ------------------------------------------------------------ pointer and keyboard

  bind() {
    const c = $("graph");
    new ResizeObserver(() => this.resize()).observe(c);
    new MutationObserver(() => this.readTheme()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    this.readTheme();
    c.addEventListener("pointerdown", (e) => this.pointerDown(e));
    c.addEventListener("pointermove", (e) => this.pointerMove(e));
    c.addEventListener("pointerup", (e) => this.pointerUp(e));
    c.addEventListener("pointercancel", (e) => this.pointerUp(e, true));
    c.addEventListener("dblclick", (e) => this.doubleClick(e));
    c.addEventListener("wheel", (e) => this.wheel(e), { passive: false });
    c.addEventListener("keydown", (e) => this.graphKey(e));
    $("track-list").addEventListener("keydown", (e) => this.trackKeydown(e));
    $("track-search").addEventListener("input", () => this.renderTracks());
    $("track-search").addEventListener("keydown", (e) => { if (e.key === "Escape" && e.target.value) { e.preventDefault(); e.target.value = ""; this.renderTracks(); } });
    const act = (fn) => () => Promise.resolve().then(fn).catch((err) => this.fail(err));
    $("ed-undo").addEventListener("click", () => this.undo());
    $("ed-redo").addEventListener("click", () => this.redo());
    for (const type of TYPES) $(`ed-type-${type}`).addEventListener("click", act(() => this.setInterpolation(type)));
    $("ed-auto").addEventListener("click", act(() => this.tangents("auto_tangents")));
    $("ed-flat").addEventListener("click", act(() => this.tangents("flatten_tangents")));
    $("ed-insert").addEventListener("click", () => this.insertAtPlayhead());
    $("ed-delete").addEventListener("click", () => this.deleteSelected());
    $("ed-snap").addEventListener("click", () => { this.snap = !this.snap; this.updateControls(); });
    $("ed-normalize").addEventListener("click", () => { this.normalized = !this.isNormalized(); this.fit({ time: false }); this.updateControls(); });
    $("ed-fit").addEventListener("click", () => this.fit());
    $("ed-play").addEventListener("click", () => this.host.togglePlay());
    $("editor-close").addEventListener("click", act(() => this.close()));
    $("dock-close").addEventListener("click", act(() => this.close()));
    $("refit-tol").addEventListener("input", () => { $("refit-tol-num").value = $("refit-tol").value; });
    $("refit-tol-num").addEventListener("change", () => { $("refit-tol").value = clampTo(Number($("refit-tol-num").value) || 0.01, 0.001, 0.2); $("refit-tol-num").value = $("refit-tol").value; });
    $("refit").addEventListener("click", act(() => this.refit()));
    $("scale-apply").addEventListener("click", act(() => this.scaleValues()));
    $("offset-apply").addEventListener("click", act(() => this.offsetValues()));
    $("trim-apply").addEventListener("click", act(() => this.trim()));
    $("retime-apply").addEventListener("click", act(() => this.retime()));
    for (const id of ["meta-duration", "meta-fps", "meta-fade-in", "meta-fade-out", "meta-loop"]) $(id).addEventListener("change", act(() => this.setMeta(id)));
    $("key-time").addEventListener("change", () => this.typeKey("time"));
    $("key-value").addEventListener("change", () => this.typeKey("value"));
    $("handle-in").addEventListener("change", () => this.typeHandle(-1, 2, $("handle-in")));
    $("handle-out").addEventListener("change", () => this.typeHandle(0, 1, $("handle-out")));
    $("save-new").addEventListener("click", act(() => this.save("new")));
    $("save-overwrite").addEventListener("click", act(() => this.save("overwrite")));
    $("save-model").addEventListener("click", act(() => this.save("model")));
    $("save-revert").addEventListener("click", () => this.revert());
    $("save-name").addEventListener("input", () => $("save-name").removeAttribute("aria-invalid"));
    this.bindResize();
    document.addEventListener("keydown", (e) => this.globalKey(e));
    api.onMenuEdit((which) => this.menuEdit(which));
    const loop = () => { this.tick(); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  // The dock's top edge resizes it (pointer or arrow keys on the separator).
  bindResize() {
    const handle = $("dock-resize");
    const pane = document.querySelector(".viewer-pane");
    const set = (px) => {
      const max = pane.clientHeight - 220;
      const h = clampTo(px, 230, Math.max(max, 230));
      pane.style.setProperty("--dock-h", `${Math.round(h)}px`);
      handle.setAttribute("aria-valuenow", String(Math.round(h)));
    };
    let start = null;
    handle.addEventListener("pointerdown", (e) => { start = { y: e.clientY, h: $("editor-dock").offsetHeight }; handle.setPointerCapture(e.pointerId); });
    handle.addEventListener("pointermove", (e) => { if (start) set(start.h + start.y - e.clientY); });
    handle.addEventListener("pointerup", () => { start = null; });
    handle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      set($("editor-dock").offsetHeight + (e.key === "ArrowUp" ? 24 : -24));
    });
  }

  point(e) { const r = $("graph").getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

  pointerDown(e) {
    if (!this.open || this.busy) return;
    const g = this.geometry();
    const [x, y] = this.point(e);
    $("graph").focus({ preventScroll: true });
    $("graph").setPointerCapture(e.pointerId);
    if (e.button === 1 || (e.button === 0 && e.altKey && !e.shiftKey)) { this.drag = { kind: "pan", x, y, view: { ...this.view } }; return; }
    if (e.button !== 0) return;
    if (y < RULER) { this.drag = { kind: "scrub" }; this.host.seek(clampTo(this.snapTime(this.tOf(x, g)), 0, this.doc.meta.duration)); return; }
    const handle = this.hitHandle(x, y, g);
    if (handle) { this.drag = { kind: "handle", ...handle, base: clone(this.doc), y }; return; }
    const hit = this.hitKey(x, y, g);
    if (hit) {
      const set = this.keys.get(hit.key) || new Set();
      if (e.shiftKey || e.metaKey || e.ctrlKey) {
        if (set.has(hit.index)) set.delete(hit.index); else set.add(hit.index);
        this.keys.set(hit.key, set);
        this.changed(); return;
      }
      if (!set.has(hit.index)) { this.keys = new Map([[hit.key, new Set([hit.index])]]); }
      const c = this.curve(hit.key);
      this.drag = { kind: "keys", grab: hit, t0: c.keys[hit.index][0], x, y, base: clone(this.doc), moved: false };
      this.changed();
      return;
    }
    const curve = this.hitCurve(x, y, g);
    if (curve && !this.selected.has(curve)) { this.selected.add(curve); this.renderTracks(); this.changed(); return; }
    this.drag = { kind: "box", x0: x, y0: y, x1: x, y1: y, add: e.shiftKey || e.metaKey || e.ctrlKey };
  }

  pointerMove(e) {
    const d = this.drag;
    if (!d) { this.hover(e); return; }
    const g = this.geometry();
    const [x, y] = this.point(e);
    if (d.kind === "pan") {
      const dt = (x - d.x) / g.gw * (d.view.t1 - d.view.t0), dy = (y - d.y) / g.gh * (d.view.y1 - d.view.y0);
      this.view = { t0: d.view.t0 - dt, t1: d.view.t1 - dt, y0: d.view.y0 + dy, y1: d.view.y1 + dy };
      this.needsDraw = true;
    } else if (d.kind === "scrub") {
      this.host.seek(clampTo(this.snapTime(this.tOf(x, g)), 0, this.doc.meta.duration));
    } else if (d.kind === "box") {
      d.x1 = x; d.y1 = y; this.needsDraw = true;
    } else if (d.kind === "handle") {
      this.doc = clone(d.base);
      const c = this.curve(d.key);
      const [c1, c2] = controls(this.doc, c, d.seg);
      const start = d.n === 1 ? c1[1] : c2[1];
      const dv = (this.dOf(y, g) - this.dOf(d.y, g)) * this.displayScale(d.key);
      setHandle(this.doc, c, d.seg, d.n, start + dv, this.range(d.key));
      d.moved = true;
      this.changed();
    } else if (d.kind === "keys") {
      if (!d.moved && Math.hypot(x - d.x, y - d.y) < 3) return;
      d.moved = true;
      this.doc = clone(d.base);
      let dt = this.snapTime(d.t0 + this.tOf(x, g) - this.tOf(d.x, g)) - d.t0;
      if (e.shiftKey) { if (Math.abs(x - d.x) > Math.abs(y - d.y)) d.lock = "t"; else d.lock = "v"; } else d.lock = null;
      const dd = this.dOf(y, g) - this.dOf(d.y, g);
      if (d.lock === "v") dt = 0;
      // One time offset for every curve (the most restrictive wins), so keys stay aligned.
      let allowed = dt;
      for (const [key, set] of this.keys) {
        if (!this.selected.has(key) || !set.size) continue;
        const probe = clone(this.curve(key));
        const [ok] = moveKeys(this.doc, probe, [...set].sort((a, b) => a - b), allowed, 0, null, this.frame() * 0.999);
        allowed = Math.abs(ok) < Math.abs(allowed) ? ok : allowed;
      }
      for (const [key, set] of this.keys) {
        if (!this.selected.has(key) || !set.size) continue;
        const dv = d.lock === "t" ? 0 : dd * this.displayScale(key);
        moveKeys(this.doc, this.curve(key), [...set].sort((a, b) => a - b), allowed, dv, this.range(key), this.frame() * 0.999);
      }
      this.changed();
    }
  }

  pointerUp(e, cancel = false) {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.kind === "box") {
      const g = this.geometry();
      const [ta, tb] = [this.tOf(d.x0, g), this.tOf(d.x1, g)].sort((a, b) => a - b);
      const [da, db] = [this.dOf(d.y0, g), this.dOf(d.y1, g)].sort((a, b) => a - b);
      if (!d.add) this.keys = new Map();
      if (Math.abs(d.x1 - d.x0) > 2 || Math.abs(d.y1 - d.y0) > 2) {
        for (const key of this.selected) {
          const c = this.curve(key); const set = this.keys.get(key) || new Set();
          c.keys.forEach(([t, v], i) => { const dv = this.toDisplay(key, v); if (t >= ta && t <= tb && dv >= da && dv <= db) set.add(i); });
          if (set.size) this.keys.set(key, set);
        }
      }
      this.changed();
      return;
    }
    if ((d.kind === "keys" || d.kind === "handle") && d.moved) {
      if (cancel) { this.doc = d.base; this.changed(); return; }
      this.commit(this.doc);
    }
  }

  hover(e) {
    if (!this.open) return;
    const g = this.geometry();
    const [x, y] = this.point(e);
    const c = $("graph");
    c.dataset.hover = y < RULER ? "ruler" : this.hitHandle(x, y, g) ? "handle" : this.hitKey(x, y, g) ? "key" : "";
  }

  doubleClick(e) {
    if (!this.open || this.busy) return;
    const g = this.geometry();
    const [x, y] = this.point(e);
    if (y < RULER || this.hitKey(x, y, g)) return;
    const key = this.hitCurve(x, y, g, { editableOnly: true }) || (this.selected.size === 1 ? [...this.selected][0] : null);
    if (!key) return;
    const time = clampTo(this.snapTime(this.tOf(x, g)), 0, this.doc.meta.duration);
    const doc = clone(this.doc); const c = doc.curves.find((k) => keyOf(k) === key);
    // Alt: at the pointer's value; otherwise on the curve (the shape stays the same).
    const r = this.range(key);
    const value = e.altKey ? clampTo(this.fromDisplay(key, this.dOf(y, g)), ...(r || [-Infinity, Infinity])) : undefined;
    const i = insertKey(doc, c, time, value);
    if (i < 0) return;
    this.keys = new Map([[key, new Set([i])]]);
    this.commit(doc);
  }

  wheel(e) {
    if (!this.open) return;
    e.preventDefault();
    const g = this.geometry();
    const [x, y] = this.point(e);
    if (e.shiftKey && !e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
      this.zoom("v", Math.exp(e.deltaY * 0.002), this.dOf(y, g));
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const dt = e.deltaX / g.gw * (this.view.t1 - this.view.t0);
      this.view.t0 += dt; this.view.t1 += dt; this.needsDraw = true;
    } else {
      this.zoom("t", Math.exp(e.deltaY * 0.002), this.tOf(x, g));
    }
  }

  graphKey(e) {
    if (!this.open || this.busy) return;
    const mod = e.metaKey || e.ctrlKey;
    const k = e.key;
    if (k === "ArrowLeft" || k === "ArrowRight" || k === "ArrowUp" || k === "ArrowDown") {
      e.preventDefault();
      const n = e.shiftKey ? 10 : 1;
      if (!this.selectedKeyCount()) {
        if (k === "ArrowLeft" || k === "ArrowRight") this.host.seek(clampTo(this.snapTime(this.host.time()) + (k === "ArrowRight" ? n : -n) * this.frame(), 0, this.doc.meta.duration));
        return;
      }
      const doc = clone(this.doc);
      for (const [key, set] of this.keys) {
        if (!this.selected.has(key) || !set.size) continue;
        const c = doc.curves.find((x) => keyOf(x) === key);
        const [lo, hi] = this.span(key);
        const dt = k === "ArrowLeft" ? -n * this.frame() : k === "ArrowRight" ? n * this.frame() : 0;
        const dv = k === "ArrowUp" ? n * (hi - lo) / 100 : k === "ArrowDown" ? -n * (hi - lo) / 100 : 0;
        moveKeys(doc, c, [...set].sort((a, b) => a - b), dt, dv, this.range(key), this.frame() * 0.999);
      }
      this.commit(doc);
      return;
    }
    if (mod && k.toLowerCase() === "a") { e.preventDefault(); this.selectAllKeys(); return; }
    if (mod) return;
    if (k === "Delete" || k === "Backspace") { e.preventDefault(); this.deleteSelected(); }
    else if (k === "Escape") { if (this.selectedKeyCount()) { e.preventDefault(); this.keys = new Map(); this.changed(); } }
    else if (k.toLowerCase() === "k") { e.preventDefault(); this.insertAtPlayhead(); }
    else if (k.toLowerCase() === "f") { e.preventDefault(); this.fit(); }
    else if (k.toLowerCase() === "s" && !e.shiftKey) { e.preventDefault(); this.snap = !this.snap; this.updateControls(); }
    else if (["1", "2", "3", "4"].includes(k)) { e.preventDefault(); this.setInterpolation(TYPES[Number(k) - 1]).catch((err) => this.fail(err)); }
    else if (k === "=" || k === "+" || k === "-") { e.preventDefault(); this.zoom("t", k === "-" ? 1.25 : 0.8, this.host.time()); }
  }

  // Undo/redo reach the editor two ways: the keydown below, and the app menu's Edit items (whose
  // accelerators may take the keystroke before the page sees it). Whichever comes first wins;
  // the other is ignored for a moment so one keystroke is one step. Text fields keep native undo.
  editsHere() { return this.open && !document.querySelector("dialog[open]"); }
  globalKey(e) {
    if (!this.editsHere()) return;
    const mod = e.metaKey || e.ctrlKey;
    if (!mod || e.altKey) return;
    const key = e.key.toLowerCase();
    const which = key === "z" ? (e.shiftKey ? "redo" : "undo") : key === "y" && !e.shiftKey ? "redo" : null;
    if (!which || e.target.closest?.(TEXT_FIELDS)) return;
    e.preventDefault();
    this.step(which, "key");
  }
  menuEdit(which) {
    if (this.editsHere() && !document.activeElement?.closest?.(TEXT_FIELDS)) this.step(which, "menu");
    else api.nativeEdit(which);
  }
  step(which, via) {
    const now = performance.now();
    if (this.lastEdit && this.lastEdit.via !== via && now - this.lastEdit.at < 300) return;
    this.lastEdit = { via, at: now };
    if (which === "redo") this.redo(); else this.undo();
  }

  selectAllKeys() {
    this.keys = new Map([...this.selected].map((key) => [key, new Set(this.curve(key).keys.map((_, i) => i))]));
    this.changed();
  }

  // ------------------------------------------------------------ edits

  insertAtPlayhead() {
    if (!this.selected.size || this.busy) return;
    const time = clampTo(this.snapTime(this.host.time()), 0, this.doc.meta.duration);
    const doc = clone(this.doc);
    const keys = new Map();
    for (const key of this.selected) {
      const c = doc.curves.find((x) => keyOf(x) === key);
      const i = insertKey(doc, c, time);
      if (i >= 0) keys.set(key, new Set([i]));
    }
    if (!keys.size) { this.host.status("editor.keyExists", null, "idle"); return; }
    this.keys = keys;
    this.commit(doc);
  }

  deleteSelected() {
    if (!this.selectedKeyCount() || this.busy) return;
    const doc = clone(this.doc);
    let removed = 0;
    for (const [key, set] of this.keys) {
      if (!this.selected.has(key)) continue;
      removed += deleteKeys(doc, doc.curves.find((x) => keyOf(x) === key), [...set]);
    }
    if (!removed) { this.host.status("editor.endsStay", null, "idle"); return; }
    this.keys = new Map();
    this.commit(doc);
    this.host.status("editor.deleted", { count: removed }, "idle");
  }

  typeHandle(offset, n, input) {
    const one = this.singleKey();
    const v = Number(input.value);
    if (!one || input.value === "" || !Number.isFinite(v)) { this.updateControls(); return; }
    const doc = clone(this.doc);
    const c = doc.curves.find((x) => keyOf(x) === one.key);
    const seg = one.index + offset;
    if (seg < 0 || seg >= c.segments.length || c.segments[seg].type !== "bezier") return;
    setHandle(doc, c, seg, n, v, this.range(one.key));
    this.commit(doc);
  }

  typeKey(which) {
    const one = this.singleKey();
    if (!one) return;
    const input = which === "time" ? $("key-time") : $("key-value");
    const n = Number(input.value);
    if (input.value === "" || !Number.isFinite(n)) { this.updateControls(); return; }
    const doc = clone(this.doc);
    const c = doc.curves.find((x) => keyOf(x) === one.key);
    const [t0, v0] = c.keys[one.index];
    if (which === "time") moveKeys(doc, c, [one.index], this.snapTime(n) - t0, 0, this.range(one.key), this.frame() * 0.999);
    else moveKeys(doc, c, [one.index], 0, n - v0, this.range(one.key));
    this.commit(doc);
  }

  // Runs operations in Python and records the result as one undoable step.
  async python(ops, { keepKeys = true, note } = {}) {
    if (this.busy) return null;
    this.busy = true; this.updateControls();
    $("editor-dock").setAttribute("aria-busy", "true");
    this.host.status("editor.applying", null, "busy");
    try {
      const r = await api.motion.apply({ motion: this.doc, ops, ranges: this.rangesObject() });
      this.busy = false;
      this.commit(r.motion, { keepKeys });
      this.renderTracks();
      this.host.status(note?.[0] || "editor.applied", note?.[1], "success");
      return r;
    } finally {
      this.busy = false; $("editor-dock").removeAttribute("aria-busy"); this.updateControls();
    }
  }

  // Segment runs of the selection as [from, to] windows strictly inside the segments.
  windows() {
    const out = [];
    for (const { curve, segs } of this.selectionSegments()) {
      let start = segs[0], prev = segs[0];
      const flush = () => {
        const from = curve.keys[start][0], to = curve.keys[prev + 1][0];
        const eps = Math.min(1e-4, (to - from) / 4);
        out.push({ id: curve.id, from: from + eps, to: to - eps });
      };
      for (const s of segs.slice(1)) { if (s !== prev + 1) { flush(); start = s; } prev = s; }
      flush();
    }
    return out;
  }

  setInterpolation(type) {
    const ops = this.windows().map((w) => ({ op: "set_interpolation", curves: [w.id], type, from: w.from, to: w.to }));
    if (!ops.length) return null;
    return this.python(ops, { note: ["editor.interpolationSet", { type: msg(`editor.type.${type}`) }] });
  }

  tangents(op) {
    const ops = [];
    for (const key of this.selected) {
      const c = this.curve(key);
      const set = [...(this.keys.get(key) || [])];
      if (set.length) ops.push({ op, curves: [c.id], from: Math.min(...set.map((i) => c.keys[i][0])), to: Math.max(...set.map((i) => c.keys[i][0])) });
      else ops.push({ op, curves: [c.id] });
    }
    return this.python(ops);
  }

  selectedIds() { return [...this.selected].map((k) => this.curve(k)?.id).filter(Boolean); }

  async refit() {
    const ids = this.selectedIds();
    if (!ids.length) return;
    const before = new Map(ids.map((id) => [id, this.doc.curves.find((c) => c.id === id).keys.length]));
    const tolerance = Number($("refit-tol").value);
    const interpolation = $("refit-bezier").checked ? "bezier" : "linear";
    await this.python([{ op: "refit", curves: ids, tolerance, interpolation }], { keepKeys: false });
    const after = ids.reduce((n, id) => n + (this.doc.curves.find((c) => c.id === id)?.keys.length || 0), 0);
    const total = [...before.values()].reduce((a, b) => a + b, 0);
    setText($("refit-note"), "editor.refitDone", { curves: ids.length, before: total, after });
  }

  scaleValues() {
    const factor = Number($("scale-factor").value);
    if (!Number.isFinite(factor)) return null;
    return this.python([{ op: "scale_values", curves: this.selectedIds(), factor, pivot: $("scale-pivot").value }]);
  }

  offsetValues() {
    const delta = Number($("offset-delta").value);
    if (!Number.isFinite(delta) || !delta) return null;
    return this.python([{ op: "offset_values", curves: this.selectedIds(), delta }]);
  }

  trim() {
    const from = Number($("trim-from").value), to = Number($("trim-to").value);
    if (!(Number.isFinite(from) && Number.isFinite(to) && from >= 0 && to > from && to <= this.doc.meta.duration + 1e-6)) {
      $("trim-to").setAttribute("aria-invalid", "true"); this.host.status("editor.trimInvalid", null, "error"); return null;
    }
    $("trim-to").removeAttribute("aria-invalid");
    return this.python([{ op: "trim", from, to }], { keepKeys: false }).then(() => this.fit());
  }

  retime() {
    const factor = Number($("retime-factor").value);
    if (!(factor > 0)) return null;
    return this.python([{ op: "retime", factor }], { keepKeys: false }).then(() => this.fit());
  }

  setMeta(id) {
    const field = { "meta-duration": "duration", "meta-fps": "fps", "meta-fade-in": "fade_in", "meta-fade-out": "fade_out", "meta-loop": "loop" }[id];
    const value = field === "loop" ? $(id).checked : Number($(id).value);
    if (field !== "loop" && (!Number.isFinite(value) || value < 0 || (field !== "fade_in" && field !== "fade_out" && value <= 0))) { this.fillMeta(); return null; }
    if (this.doc.meta[field] === value) return null;
    return this.python([{ op: "set_meta", [field]: value }]);
  }

  revert() {
    if (!this.dirty) return;
    const saved = this.history.find((h) => h.id === this.savedId);
    if (!saved) return;
    this.keys = new Map();
    this.commit(clone(saved.doc));
    this.savedId = this.history[this.index].id;
    this.renderTracks();
    this.changed({ meta: true });
    this.host.status("editor.reverted", null, "idle");
  }

  async save(mode) {
    const name = $("save-name").value.trim();
    if ((mode === "new" || mode === "model") && !NAME_RE.test(name)) {
      $("save-name").setAttribute("aria-invalid", "true"); $("save-name").focus();
      this.host.status("validate.name", null, "error");
      return;
    }
    this.busy = true; this.updateControls();
    this.host.status("editor.saving", null, "busy");
    let saved;
    try {
      saved = await api.motion.save({ motion: this.doc, mode, file: this.source.file, name, modelDir: this.host.modelDir() });
    } finally { this.busy = false; this.updateControls(); }
    if (!saved) { this.host.status("editor.saveCancelled", null, "idle"); return; }
    const previous = this.source;
    if (mode !== "model") {
      this.source = { ...this.source, file: saved.file, name: saved.name, dir: saved.dir };
      setRaw($("editor-name"), saved.name); $("editor-name").title = saved.file; setRaw($("editor-file"), saved.file);
    }
    this.savedId = this.history[this.index].id;
    this.updateControls();
    await this.host.onSaved({ ...saved, mode }, previous);
    this.host.status(mode === "model" ? "editor.savedModel" : mode === "overwrite" ? "editor.overwritten" : "editor.savedNew", { name: `${saved.name}.motion3.json` }, "success");
  }

  fail(err) { this.host.status("status.failed", { prefix: msg("editor.failed"), error: errorText(err) }, "error"); }
}
