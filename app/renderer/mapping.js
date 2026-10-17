// Mapping editor: how tracking becomes Live2D parameters for the open model, in the three layers
// of a `v2l.mapping` file (docs/motion-editing.md §5), one tab each:
//   Inputs      per tracking input, a curve from its nominal range onto itself (as VBridger's
//               input curves), with the recorded input over time;
//   Bridge      the expression outputs (SnenkBridge format: formula or delay buffer, min, max,
//               default) with output modifiers (delay, smoothing, steps), checked by
//               `v2l bridge check`, with the recorded output over time;
//   Parameters  which input drives each parameter, over which ranges, with clamp, invert,
//               breathing, a response curve and a smoothing cutoff (as VTube Studio's parameter
//               settings).
// The base comes from `v2l mapping show` on the compute side; Studio stores only changed fields
// per model in ~/.config/v2l/mappings/<model>.json. Graphs read <result>.signals.json of the
// latest result for this model; "Preview changes" re-runs that result with the draft.
import { t } from "./i18n.js";
import { ICONS } from "./icons.js";
import { errorText } from "./prefs.js";
import { CurveEditor, evalCurve } from "./curve-editor.js";
import { fmt, SignalGraph } from "./graph.js";

const api = window.v2l;
const dlg = document.getElementById("mapping");
const TABS = ["inputs", "bridge", "parameters"];
const ENTRY_FIELDS = ["input", "in", "out", "clamp_in", "clamp_out", "invert", "curve", "cutoff", "breathing", "enabled"];
const BRIDGE_FIELDS = ["func", "min", "max", "defaultValue", "delayBuffer", "modifiers", "enabled"];
const FILTERS = ["all", "mapped", "unmapped", "changed", "physics"];
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
const round = (v, d = 3) => Number(Number(v).toFixed(d));
const clone = (v) => (v == null ? v : structuredClone(v));
let uid = 0;

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

const emptyDraft = () => ({ curves: {}, bridge: new Map(), entries: new Map() });
const ui = {
  model: null, state: "idle", error: null,
  show: null,             // `v2l mapping show` (base, without overrides)
  inputs: [],             // `v2l mapping inputs`
  saved: emptyDraft(), draft: emptyDraft(),
  file: null, tab: "parameters", selected: { inputs: null, bridge: null, parameters: null },
  query: "", filter: "all", notice: null, request: 0,
  signals: null, signalsFile: null, time: 0, playing: false,
  live: { graphs: [], curves: [] }, checks: new Map(), checkTimer: null,
};
let hooks = { canApply: () => null, apply: async () => {}, canPreview: () => null, preview: async () => {}, signalsFile: () => null,
              openCompute: () => {}, remote: () => true };

export function initMapping(options) { hooks = { ...hooks, ...options }; }
export function isMappingOpen() { return dlg.open; }
export function refreshMapping() { render(); }

// ------------------------------------------------------------ draft <-> file

function serialize(d) {
  return { format: "v2l.mapping", version: 1, input_curves: clone(d.curves), bridge: [...d.bridge.values()].map(clone), entries: [...d.entries.values()].map(clone) };
}
function deserialize(m) {
  return { curves: clone(m.input_curves || {}), bridge: new Map((m.bridge || []).map((b) => [b.name, clone(b)])),
           entries: new Map((m.entries || []).map((e) => [e.output, clone(e)])) };
}
function dirty() { return !same(serialize(ui.draft), serialize(ui.saved)); }
function changeCount() { return Object.keys(ui.draft.curves).length + ui.draft.bridge.size + ui.draft.entries.size; }

// ------------------------------------------------------------ inputs layer

function inputInfo(name) { return ui.inputs.find((i) => i.name === name) || null; }
function trackingInputs() { return ui.inputs.filter((i) => i.kind === "tracking"); }
function norm(range, v) { return range && range[1] !== range[0] ? (v - range[0]) / (range[1] - range[0]) : v; }
function denorm(range, x) { return range ? range[0] + x * (range[1] - range[0]) : x; }

// Recorded value before any input curve (the run's own curve is undone via inputs_raw).
function rawInput(name, i) {
  const s = ui.signals;
  if (!s) return null;
  const series = s.inputs_raw?.[name] ?? s.inputs?.[name];
  return series ? series[i] : null;
}
function frameAt(time) { return ui.signals ? Math.min(Math.max(Math.round(time * ui.signals.fps), 0), (ui.signals.duration * ui.signals.fps) | 0) : 0; }
function curvedInput(name, raw) {
  if (raw == null) return null;
  const r = inputInfo(name)?.range;
  return denorm(r, evalCurve(ui.draft.curves[name] || null, norm(r, raw)));
}

function setInputCurve(name, value) {
  if (value) ui.draft.curves[name] = value; else delete ui.draft.curves[name];
  render();
}

// ------------------------------------------------------------ bridge layer

function baseBridge(name) { return ui.show?.bridge_params?.find((b) => b.name === name) || null; }
function bridgeNames() {
  const names = (ui.show?.bridge_params || []).map((b) => b.name);
  for (const n of ui.draft.bridge.keys()) if (!names.includes(n)) names.push(n);
  return names;
}
function effectiveBridge(name) {
  const base = baseBridge(name);
  const o = ui.draft.bridge.get(name);
  if (!base && !o) return null;
  const e = base ? clone(base) : { name, func: "", min: 0, max: 1, defaultValue: 0, delayBuffer: null, modifiers: null, source: "added" };
  if (o) {
    for (const f of BRIDGE_FIELDS) {
      if (!(f in o)) continue;
      e[f] = f === "delayBuffer" && o[f] && e[f] ? { ...e[f], ...o[f] } : clone(o[f]);
    }
  }
  e.source = !base ? "added" : e.enabled === false ? "disabled" : o ? "override" : base.source === "disabled" ? "disabled" : "preset";
  return e;
}
// Only fields that differ from the preset are kept; an added output keeps everything.
function setBridgeField(name, field, value, { quiet = false } = {}) {
  const base = baseBridge(name);
  const o = { ...(ui.draft.bridge.get(name) || {}), name };
  if (base && (same(base[field], value) || (field === "enabled" && value === true))) delete o[field]; else o[field] = clone(value);
  if (base && Object.keys(o).length === 1) ui.draft.bridge.delete(name); else ui.draft.bridge.set(name, o);
  if (quiet) refreshLists(); else render();
}
function addBridge(kind) {
  const names = new Set(bridgeNames());
  let n = names.size + 1;
  while (names.has(`Output${n}`)) n++;
  const name = `Output${n}`;
  const out = { name, min: 0, max: 1, defaultValue: 0 };
  if (kind === "delay") out.delayBuffer = { refParam: bridgeNames()[0] || "FaceAngleX", smoothing: 1, delayCount: 1, inMin: 0, inMax: 1, outMin: 0, outMax: 1 };
  else out.func = "0";
  ui.draft.bridge.set(name, out);
  ui.selected.bridge = name;
  render();
}
function duplicateBridge(name) {
  const e = effectiveBridge(name);
  if (!e) return;
  const names = new Set(bridgeNames());
  let copy = `${name}_copy`;
  for (let n = 2; names.has(copy); n++) copy = `${name}_copy${n}`;
  const out = { name: copy };
  for (const f of ["func", "min", "max", "defaultValue", "delayBuffer", "modifiers"]) if (e[f] != null && e[f] !== "") out[f] = clone(e[f]);
  ui.draft.bridge.set(copy, out);
  ui.selected.bridge = copy;
  render();
}
function renameBridge(name, next) {
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(next) || bridgeNames().includes(next) || baseBridge(name)) { render(); return; }
  const o = ui.draft.bridge.get(name);
  ui.draft.bridge.delete(name);
  ui.draft.bridge.set(next, { ...o, name: next });
  ui.selected.bridge = next;
  render();
}

// Formula checks (debounced), keyed by formula text.
function checkFormula(func) {
  if (ui.checks.has(func)) return;
  clearTimeout(ui.checkTimer);
  ui.checkTimer = setTimeout(async () => {
    try {
      // The edited formula itself is checked, so the draft's copy of it stays out of the mapping
      // (an unfinished formula would make the whole mapping invalid).
      const mapping = serialize(ui.draft);
      for (const b of mapping.bridge) if (b.name === ui.selected.bridge && "func" in b) { if (baseBridge(b.name)) delete b.func; else b.func = "0"; }
      mapping.bridge = mapping.bridge.filter((b) => Object.keys(b).length > 1);
      const r = await api.bridge.check({ func, mapping });
      ui.checks.set(func, r);
    } catch (err) { ui.checks.set(func, { ok: false, problem: { message: errorText(err) } }); }
    paintFormula();
  }, 220);
}

// ------------------------------------------------------------ parameters layer

function baseEntry(id) { return ui.show?.entries.find((e) => e.output === id) || null; }
function param(id) { return ui.show?.parameters.find((p) => p.id === id) || null; }
function entryInputs() {
  const names = ui.inputs.filter((i) => !i.shadowed).map((i) => i.name);
  for (const n of bridgeNames()) if (!names.includes(n)) names.push(n);
  return names;
}
function effective(id) {
  const base = baseEntry(id);
  const o = ui.draft.entries.get(id);
  if (!base && !o) return null;
  const p = param(id);
  const e = base ? { ...base } : { output: id, input: null, in: inputInfo(o?.input)?.range || [0, 1], out: p ? [p.min, p.max] : [0, 1],
    clamp_in: true, clamp_out: true, invert: false, curve: null, cutoff: null, breathing: false, enabled: true, source: "added" };
  if (o) for (const f of ENTRY_FIELDS) if (f in o) e[f] = clone(o[f]);
  e.source = !base ? "added" : o ? "override" : base.source;
  return e;
}
function setField(id, field, value) {
  const base = baseEntry(id);
  const o = { ...(ui.draft.entries.get(id) || {}), output: id };
  if (base && same(base[field], value)) delete o[field]; else o[field] = clone(value);
  if (!base) {
    if (field === "input" && value && !o.in) o.in = inputInfo(value)?.range || [0, 1];
    if (!o.out) { const p = param(id); o.out = p ? [p.min, p.max] : [0, 1]; }
  }
  if (Object.keys(o).length === 1) ui.draft.entries.delete(id); else ui.draft.entries.set(id, o);
  render();
}

// ------------------------------------------------------------ open / load / save

export async function openMapping(model) {
  if (!model) return;
  if (ui.model?.dir !== model.dir) {
    ui.model = { dir: model.dir, name: model.name };
    ui.state = "idle"; ui.show = null; ui.saved = emptyDraft(); ui.draft = emptyDraft(); ui.notice = null;
    ui.selected = { inputs: null, bridge: null, parameters: null };
  }
  if (!dlg.open) dlg.showModal();
  await loadSignals();
  render();
  if (ui.state !== "ready") await load();
}

async function loadSignals() {
  const file = hooks.signalsFile();
  if (file === ui.signalsFile) return;
  ui.signalsFile = file; ui.signals = file ? await api.signals(file) : null;
  ui.time = Math.min(ui.time, ui.signals?.duration || 0);
}

async function load() {
  const request = ++ui.request;
  ui.state = "loading"; ui.error = null; render();
  try {
    const [show, inputs, stored] = await Promise.all([api.mapping.show(ui.model.dir), api.mapping.inputs(), api.mapping.load(ui.model.dir)]);
    if (request !== ui.request) return;
    ui.show = show; ui.inputs = inputs.inputs || [];
    ui.file = stored.file;
    ui.saved = deserialize(stored.mapping);
    ui.draft = deserialize(stored.mapping);
    ui.selected.parameters ||= show.entries.find((e) => e.enabled !== false)?.output || show.parameters[0]?.id || null;
    ui.selected.inputs ||= trackingInputs()[0]?.name || null;
    ui.selected.bridge ||= show.bridge_params?.[0]?.name || null;
    ui.state = "ready";
  } catch (err) {
    if (request !== ui.request) return;
    ui.state = "error"; ui.error = errorText(err);
  }
  render();
}

async function save() {
  const r = await api.mapping.save({ modelDir: ui.model.dir, mapping: serialize(ui.draft) });
  ui.file = r.file;
  ui.saved = deserialize(r.mapping);
  ui.notice = { key: "mapping.saved", params: { count: changeCount() } };
  render();
}

async function guard(fn) {
  try { await fn(); } catch (err) { ui.notice = { key: "mapping.error", params: { error: errorText(err) }, bad: true }; render(); }
}

// ------------------------------------------------------------ playhead over the recorded signals

function seek(time) { ui.time = time; updateLive(); }
function togglePlay() {
  if (!ui.signals) return;
  ui.playing = !ui.playing;
  let last = performance.now();
  const step = (now) => {
    if (!ui.playing || !dlg.open) { ui.playing = false; updateLive(); return; }
    ui.time += (now - last) / 1000; last = now;
    if (ui.time > ui.signals.duration) ui.time = 0;
    updateLive();
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  updateLive();
}

// Values at the playhead without rebuilding the dialog.
function updateLive() {
  const i = frameAt(ui.time);
  for (const row of dlg.querySelectorAll("[data-live]")) {
    const [kind, name] = row.dataset.live.split(":");
    let v = null, range = null;
    if (kind === "in") { v = curvedInput(name, rawInput(name, i)); range = inputInfo(name)?.range; }
    else if (kind === "br") { const e = effectiveBridge(name); v = ui.signals?.bridge?.[name]?.[i] ?? null; range = e ? [e.min, e.max] : null; }
    const fill = row.querySelector(".live-fill"), out = row.querySelector(".live-value");
    if (out) out.textContent = v == null ? "—" : fmt(v, 2);
    if (fill) {
      // The bar grows from zero (or the range's low end) to the value.
      const zero = range ? Math.min(Math.max(norm(range, 0), 0), 1) : 0;
      const x = v == null || !range ? zero : Math.min(Math.max(norm(range, v), 0), 1);
      fill.style.left = `${Math.min(x, zero) * 100}%`; fill.style.width = `${Math.abs(x - zero) * 100}%`;
    }
  }
  for (const g of ui.live.graphs) { g.time = ui.time; g.draw(); }
  for (const c of ui.live.curves) c.draw();
  const play = dlg.querySelector("#map-play");
  if (play) play.textContent = t(ui.playing ? "transport.pause" : "transport.play");
  const time = dlg.querySelector("#map-time");
  if (time) time.textContent = ui.signals ? t("mapping.timeReadout", { time: ui.time.toFixed(2), duration: ui.signals.duration.toFixed(2) }) : "";
}

// ------------------------------------------------------------ shared widgets

function sourceBadge(source) {
  if (!source) return h("span", { class: "badge quiet", text: t("mapping.source.none") });
  return h("span", { class: `badge src-${source}`, text: t(`mapping.source.${source}`) });
}
function liveBar(kind, name) {
  return h("span", { class: "live", "data-live": `${kind}:${name}` }, h("span", { class: "live-bar", "aria-hidden": "true" }, h("span", { class: "live-fill" })),
    h("output", { class: "live-value", text: "—" }));
}
function numberField(label, value, { min, max, step = "any", placeholder, onchange, id }) {
  const fid = id || `map-${++uid}`;
  const input = h("input", { type: "number", id: fid, class: "num", value: value ?? "", min: min ?? null, max: max ?? null, step, placeholder: placeholder ?? null,
    onchange: () => {
      if (input.value === "") { onchange(null); return; }
      const n = Number(input.value);
      if (!Number.isFinite(n)) { input.value = value ?? ""; return; }
      onchange(n);
    } });
  return h("label", { class: "map-num", for: fid }, h("span", { text: label }), input);
}
function rangeEditor(title, value, limits, onchange, help) {
  const [lo, hi] = limits;
  const step = (hi - lo) / 200 || 0.01;
  const line = (which, label) => {
    const id = `map-${++uid}`;
    const v = value[which];
    const num = h("input", { type: "number", class: "num", id, value: round(v), step: "any", "aria-label": `${title} ${label}` });
    const slider = h("input", { type: "range", id: `${id}-s`, min: Math.min(lo, v), max: Math.max(hi, v), step, value: v, "aria-label": `${title} ${label}` });
    const set = (n) => { const next = [...value]; next[which] = round(n, 4); onchange(next); };
    slider.addEventListener("input", () => { num.value = round(slider.value); });
    slider.addEventListener("change", () => set(Number(slider.value)));
    num.addEventListener("change", () => { const n = Number(num.value); if (Number.isFinite(n) && num.value !== "") set(n); else num.value = round(v); });
    return h("div", { class: "slider" }, h("label", { class: "map-sub", for: id, text: label }), slider, num);
  };
  return h("fieldset", { class: "map-range" }, h("legend", { text: title }), help ? h("p", { class: "setting-help", text: help }) : null,
    line(0, t("mapping.low")), line(1, t("mapping.high")),
    h("p", { class: "setting-help", text: t("mapping.limits", { lo: round(lo), hi: round(hi) }) }));
}
function toggle(label, checked, onchange, help) {
  const id = `map-${++uid}`;
  return h("div", { class: "map-toggle" },
    h("input", { type: "checkbox", role: "switch", class: "switch", id, checked: !!checked, onchange: (e) => onchange(e.target.checked) }),
    h("label", { for: id }, h("span", { text: label }), help ? h("small", { text: help }) : null));
}
function listbox(label, items, selected, onpick) {
  const ul = h("ul", { class: "map-list", role: "listbox", "aria-label": label });
  for (const it of items) {
    const on = it.id === selected;
    ul.append(h("li", { class: `map-row${on ? " selected" : ""}${it.changed ? " changed" : ""}`, role: "option", "aria-selected": String(on),
      tabindex: on ? "0" : "-1", "data-id": it.id, onclick: () => onpick(it.id) }, it.cells));
  }
  ul.addEventListener("keydown", (ev) => {
    const all = [...ul.querySelectorAll(".map-row")];
    const i = all.indexOf(ev.target.closest(".map-row"));
    if (i < 0) return;
    const j = { ArrowDown: Math.min(i + 1, all.length - 1), ArrowUp: Math.max(i - 1, 0), Home: 0, End: all.length - 1 }[ev.key];
    if (j == null) return;
    ev.preventDefault();
    onpick(all[j].dataset.id);
    dlg.querySelector(`.map-row[data-id="${CSS.escape(all[j].dataset.id)}"]`)?.focus();
  });
  return items.length ? ul : h("p", { class: "map-empty", text: t("mapping.noMatch") });
}
function matches(...texts) { const q = ui.query.trim().toLowerCase(); return !q || texts.filter(Boolean).some((s) => String(s).toLowerCase().includes(q)); }

// Values over time from the latest result's signals, with the shared playhead.
function signalGraph(series, range, caption) {
  if (!ui.signals) return h("p", { class: "map-empty graph-none", text: t("mapping.noSignals") });
  const canvas = h("canvas", { class: "signal-canvas", "aria-label": t("mapping.graphLabel") });
  const graph = new SignalGraph(canvas, { onSeek: seek });
  graph.fps = ui.signals.fps; graph.duration = ui.signals.duration; graph.range = range; graph.time = ui.time; graph.series = series;
  ui.live.graphs.push(graph);
  requestAnimationFrame(() => graph.draw());
  return h("div", { class: "signal-graph" },
    h("div", { class: "signal-head" },
      h("button", { type: "button", class: "small", id: "map-play", text: t(ui.playing ? "transport.pause" : "transport.play"), onclick: togglePlay }),
      h("span", { class: "hint", id: "map-time" }),
      h("span", { class: "signal-legend" }, series.map((s) => h("span", { class: "legend-item" }, h("span", { class: "legend-swatch", style: `--swatch:${s.color}; border-top-style:${s.dash ? "dashed" : "solid"}` }), h("span", { text: s.label }))))),
    canvas, h("p", { class: "setting-help", text: caption }));
}
const color = (name) => getComputedStyle(dlg).getPropertyValue(name).trim();

// ------------------------------------------------------------ tab: inputs

function inputsTab() {
  const items = trackingInputs().filter((i) => matches(i.name, i.meaning) && (ui.filter !== "changed" || ui.draft.curves[i.name]))
    .map((i) => ({ id: i.name, changed: !!ui.draft.curves[i.name], cells: [
      h("span", { class: "map-name" }, h("strong", { text: i.name }), h("small", { text: i.meaning || "" })),
      liveBar("in", i.name),
      h("span", { class: "map-badges" }, ui.draft.curves[i.name] ? h("span", { class: "badge src-override", html: ICONS.spline, title: t("mapping.hasCurve") }) : null)] }));
  const name = ui.selected.inputs;
  const info = inputInfo(name);
  let detail;
  if (!info) detail = h("p", { class: "map-empty", text: t("mapping.pickInput") });
  else {
    const range = info.range || [0, 1];
    const editor = new CurveEditor({ id: "in-curve", value: ui.draft.curves[name] || null, exportName: name,
      onChange: (v) => setInputCurve(name, v),
      marker: () => { const raw = rawInput(name, frameAt(ui.time)); return raw == null ? null : norm(range, raw); } });
    ui.live.curves.push(editor);
    const n = ui.signals ? Math.round(ui.signals.duration * ui.signals.fps) + 1 : 0;
    const raw = Array.from({ length: n }, (_, i) => rawInput(name, i));
    const curved = raw.map((v) => curvedInput(name, v));
    detail = h("div", { class: "map-detail" },
      h("header", { class: "map-detail-head" },
        h("div", {}, h("h3", { text: name }), h("p", { class: "setting-help", text: info.meaning || "" }),
          h("p", { class: "setting-help", text: t("mapping.inputRange", { lo: round(range[0]), hi: round(range[1]) }) })),
        h("div", { class: "map-badges" }, h("span", { class: "badge quiet", text: t("mapping.inputKind.tracking") }))),
      h("fieldset", { class: "map-response" }, h("legend", { text: t("mapping.inputCurve") }),
        h("p", { class: "setting-help", text: t("mapping.inputCurveHelp") }), editor.node),
      signalGraph([{ values: raw, color: color("--text-tertiary"), dash: [4, 3], width: 1.25, label: t("mapping.recorded") },
                   { values: curved, color: color("--accent-visual"), label: t("mapping.withCurve") }], range, t("mapping.inputGraphHelp", { result: resultName() })),
      h("div", { class: "inline wrap" },
        h("button", { type: "button", class: "small", disabled: !Object.keys(ui.draft.curves).length, text: t("mapping.exportCurves"),
          onclick: () => guard(() => api.json.save({ title: t("mapping.exportCurves"), name: `${ui.model.name}.input-curves.json`, data: { input_curves: ui.draft.curves } })) }),
        h("button", { type: "button", class: "small", text: t("mapping.importCurves"), onclick: () => guard(async () => {
          const data = await api.json.open(t("mapping.importCurves"));
          const curves = data?.input_curves || data;
          if (!curves || typeof curves !== "object" || Array.isArray(curves)) throw new Error(t("mapping.notCurves"));
          ui.draft.curves = { ...ui.draft.curves, ...curves }; render();
        }) }),
        h("button", { type: "button", class: "small danger", disabled: !Object.keys(ui.draft.curves).length, text: t("mapping.resetCurves"),
          onclick: () => { ui.draft.curves = {}; render(); } })));
  }
  return h("div", { class: "map-body" }, h("div", { class: "map-list-wrap" }, listbox(t("mapping.tab.inputs"), items, name, (id) => { ui.selected.inputs = id; render(); })), detail);
}

// ------------------------------------------------------------ tab: bridge outputs

function modifierIcons(e) {
  const m = e.modifiers || {};
  return [
    e.delayBuffer ? h("span", { class: "mod-icon", title: t("mapping.delayBuffer"), "aria-label": t("mapping.delayBuffer"), html: ICONS.copy }) : null,
    m.delayMs ? h("span", { class: "mod-icon", title: t("mapping.mod.delay"), "aria-label": t("mapping.mod.delay"), html: ICONS.timer }) : null,
    m.smoothing ? h("span", { class: "mod-icon", title: t("mapping.mod.smoothing"), "aria-label": t("mapping.mod.smoothing"), html: ICONS.waves }) : null,
    m.steps?.length ? h("span", { class: "mod-icon", title: t("mapping.mod.steps"), "aria-label": t("mapping.mod.steps"), html: ICONS.stepped }) : null,
  ];
}

function bridgeTab() {
  const items = bridgeNames().map(effectiveBridge).filter((e) => e && matches(e.name, e.func) && (ui.filter !== "changed" || ui.draft.bridge.has(e.name)))
    .map((e) => ({ id: e.name, changed: ui.draft.bridge.has(e.name), cells: [
      h("span", { class: "map-name" }, h("strong", { text: e.name }),
        h("small", { class: "mono", text: e.delayBuffer ? t("mapping.follows", { name: e.delayBuffer.refParam }) : e.func || "—" })),
      liveBar("br", e.name),
      h("span", { class: "map-badges" }, h("span", { class: "mods" }, modifierIcons(e)), sourceBadge(e.source))] }));
  const name = ui.selected.bridge;
  const e = effectiveBridge(name);
  const list = h("div", { class: "map-list-wrap" },
    h("div", { class: "inline wrap list-tools" },
      h("button", { type: "button", class: "small", id: "br-add", text: t("mapping.addOutput"), onclick: () => addBridge("formula") }),
      h("button", { type: "button", class: "small", id: "br-add-delay", text: t("mapping.addDelay"), onclick: () => addBridge("delay") })),
    listbox(t("mapping.tab.bridge"), items, name, (id) => { ui.selected.bridge = id; render(); }));
  if (!e) return h("div", { class: "map-body" }, list, h("div", { class: "map-detail" }, h("p", { class: "map-empty", text: t("mapping.pickOutput") })));
  const base = baseBridge(name);
  const set = (f, v, o) => setBridgeField(name, f, v, o);
  const parts = [h("header", { class: "map-detail-head" },
    h("div", {}, base ? h("h3", { text: name })
      : h("label", { class: "field", for: "br-name" }, h("span", { text: t("mapping.outputName") }),
          h("input", { id: "br-name", value: name, spellcheck: "false", onchange: (ev) => renameBridge(name, ev.target.value.trim()) }))),
    h("div", { class: "map-badges" }, h("span", { class: "mods" }, modifierIcons(e)), sourceBadge(e.source)))];
  parts.push(toggle(t("mapping.outputEnabled"), e.enabled !== false, (v) => set("enabled", v), t("mapping.outputEnabledHelp")));
  if (e.delayBuffer) parts.push(delayBufferEditor(name, e));
  else parts.push(formulaEditor(name, e));
  parts.push(h("div", { class: "map-grid3" },
    numberField(t("mapping.min"), e.min, { id: "br-min", onchange: (v) => set("min", v) }),
    numberField(t("mapping.max"), e.max, { id: "br-max", onchange: (v) => set("max", v) }),
    numberField(t("mapping.default"), e.defaultValue, { id: "br-default", onchange: (v) => set("defaultValue", v) })));
  parts.push(modifiersEditor(name, e));
  parts.push(signalGraph([{ values: ui.signals?.bridge?.[name] || [], color: color("--accent-visual"), label: t("mapping.recordedOutput") }], [e.min, e.max],
    t("mapping.bridgeGraphHelp", { result: resultName() })));
  parts.push(h("div", { class: "inline wrap" },
    h("button", { type: "button", class: "small", text: t("mapping.duplicate"), onclick: () => duplicateBridge(name) }),
    base ? h("button", { type: "button", class: "small", disabled: !ui.draft.bridge.has(name), text: t("mapping.resetRow"), onclick: () => { ui.draft.bridge.delete(name); render(); } })
      : h("button", { type: "button", class: "small danger", text: t("mapping.removeOutput"), onclick: () => { ui.draft.bridge.delete(name); ui.selected.bridge = null; render(); } })));
  return h("div", { class: "map-body" }, list, h("div", { class: "map-detail" }, parts));
}

// Formula with token colouring (known names, functions, unknown names) and the check result.
function formulaEditor(name, e) {
  const area = h("textarea", { id: "br-func", class: "formula-input", rows: 3, spellcheck: "false", "aria-describedby": "br-func-status" });
  area.value = e.func || "";  // a textarea's value is a property, not an attribute
  const layer = h("pre", { class: "formula-hl", "aria-hidden": "true" });
  const status = h("p", { id: "br-func-status", class: "formula-status", role: "status" });
  area.addEventListener("input", () => {
    setBridgeField(name, "func", area.value, { quiet: true });
    paintFormula(); checkFormula(area.value);
  });
  area.addEventListener("scroll", () => { layer.scrollTop = area.scrollTop; });
  queueMicrotask(() => { paintFormula(); checkFormula(area.value); });
  return h("div", { class: "field formula-field" }, h("label", { for: "br-func", text: t("mapping.formula") }),
    h("div", { class: "formula" }, layer, area), status, h("p", { class: "setting-help", text: t("mapping.formulaHelp") }));
}

function paintFormula() {
  const area = dlg.querySelector("#br-func"), layer = dlg.querySelector(".formula-hl"), status = dlg.querySelector("#br-func-status");
  if (!area || !layer) return;
  const text = area.value;
  const check = ui.checks.get(text);
  const cls = new Array(text.length).fill("");
  const unknown = new Set(check?.unknown || []);
  for (const tok of check?.tokens || []) {
    const word = text.slice(tok.start, tok.end);
    const c = tok.kind === "name" ? (unknown.has(word) ? "tok-bad" : "tok-name") : tok.kind === "function" ? "tok-fn" : tok.kind === "number" ? "tok-num" : "tok-op";
    for (let i = tok.start; i < tok.end && i < text.length; i++) cls[i] = c;
  }
  const p = check?.problem;
  const err = (i) => p && p.position != null && i >= p.position && i < Math.max(p.end ?? p.position + 1, p.position + 1);
  const nodes = [];
  let i = 0;
  while (i < text.length) {
    let j = i + 1;
    while (j < text.length && cls[j] === cls[i] && err(j) === err(i)) j++;
    nodes.push(h("span", { class: `${cls[i]}${err(i) ? " tok-err" : ""}`, text: text.slice(i, j) }));
    i = j;
  }
  // A problem at the end of the text (e.g. a missing operand) gets a visible marker.
  if (p && p.position != null && p.position >= text.length) nodes.push(h("span", { class: "tok-err", text: " " }));
  layer.replaceChildren(...nodes, "\n");
  const field = area.closest(".formula");
  field.dataset.state = !check ? "" : check.valid ? "ok" : "bad";
  if (!status) return;
  if (!check) status.textContent = text.trim() ? t("mapping.checking") : "";
  else if (check.valid) status.textContent = check.inputs?.length ? t("mapping.formulaOk", { names: check.inputs.join(", ") }) : t("mapping.formulaOkConst");
  else status.textContent = t("mapping.formulaBad", { message: p?.message || (unknown.size ? [...unknown].join(", ") : "") });
  status.classList.toggle("bad", !!check && !check.valid);
}

function delayBufferEditor(name, e) {
  const d = e.delayBuffer;
  const set = (f, v) => setBridgeField(name, "delayBuffer", { ...d, [f]: v });
  const sid = "br-ref";
  return h("fieldset", { class: "map-range" }, h("legend", { text: t("mapping.delayBuffer") }),
    h("p", { class: "setting-help", text: t("mapping.delayBufferHelp") }),
    h("label", { class: "field", for: sid }, h("span", { text: t("mapping.followsParam") }),
      h("select", { id: sid, onchange: (ev) => set("refParam", ev.target.value) },
        bridgeNames().filter((n) => n !== name).map((n) => h("option", { value: n, text: n, selected: n === d.refParam })))),
    h("div", { class: "map-grid3" },
      numberField(t("mapping.smoothing"), d.smoothing, { id: "br-db-smooth", onchange: (v) => set("smoothing", v ?? 0) }),
      numberField(t("mapping.delayFrames"), d.delayCount, { id: "br-db-delay", step: 1, min: 0, onchange: (v) => set("delayCount", Math.round(v ?? 0)) }),
      h("span"),
      numberField(t("mapping.inMin"), d.inMin, { id: "br-db-inmin", onchange: (v) => set("inMin", v ?? 0) }),
      numberField(t("mapping.inMax"), d.inMax, { id: "br-db-inmax", onchange: (v) => set("inMax", v ?? 1) }),
      h("span"),
      numberField(t("mapping.outMin"), d.outMin, { id: "br-db-outmin", onchange: (v) => set("outMin", v ?? 0) }),
      numberField(t("mapping.outMax"), d.outMax, { id: "br-db-outmax", onchange: (v) => set("outMax", v ?? 1) })));
}

// Output modifiers, applied in order: delay, smoothing, steps (as VBridger's output panel).
function modifiersEditor(name, e) {
  const m = e.modifiers || {};
  const set = (next) => {
    const clean = Object.fromEntries(Object.entries(next).filter(([, v]) => v != null && v !== 0 && !(Array.isArray(v) && !v.length)));
    setBridgeField(name, "modifiers", Object.keys(clean).length ? clean : null);
  };
  const steps = m.steps || [];
  const stepRow = (s, i) => {
    const cell = (f, label) => h("td", {}, h("input", { type: "number", class: "num", id: `br-step${i}-${f}`, step: "any", value: s[f] ?? "", "aria-label": t("mapping.stepCell", { field: label, n: i + 1 }),
      onchange: (ev) => { const next = steps.map((x) => ({ ...x })); const v = ev.target.value === "" ? undefined : Number(ev.target.value); if (v === undefined) delete next[i][f]; else next[i][f] = v; set({ ...m, steps: next }); } }));
    return h("tr", {}, cell("trigger", t("mapping.step.trigger")), cell("target", t("mapping.step.target")), cell("holdMs", t("mapping.step.hold")), cell("threshold", t("mapping.step.threshold")),
      h("td", {}, h("button", { type: "button", class: "icon tiny", html: ICONS.close, title: t("mapping.removeStep"), "aria-label": t("mapping.removeStep"),
        onclick: () => set({ ...m, steps: steps.filter((_, j) => j !== i) }) })));
  };
  const smoothId = "br-mod-smooth";
  return h("fieldset", { class: "map-range modifiers" }, h("legend", { text: t("mapping.modifiers") }),
    h("p", { class: "setting-help", text: t("mapping.modifiersHelp") }),
    h("div", { class: "mod-line" }, h("span", { class: "mod-icon", html: ICONS.timer }),
      numberField(t("mapping.mod.delayMs"), m.delayMs ?? 0, { id: "br-mod-delay", min: 0, step: 10, onchange: (v) => set({ ...m, delayMs: v || null }) })),
    h("div", { class: "mod-line" }, h("span", { class: "mod-icon", html: ICONS.waves }),
      h("div", { class: "slider" }, h("label", { class: "map-sub", for: smoothId, text: t("mapping.mod.smoothing") }),
        h("input", { type: "range", id: smoothId, min: 0, max: 0.95, step: 0.05, value: m.smoothing ?? 0, onchange: (ev) => set({ ...m, smoothing: Number(ev.target.value) || null }),
          oninput: (ev) => { ev.target.nextSibling.textContent = fmt(Number(ev.target.value), 2); } }),
        h("output", { class: "num-out", text: fmt(m.smoothing ?? 0, 2) }))),
    h("div", { class: "mod-line steps" }, h("span", { class: "mod-icon", html: ICONS.stepped }),
      h("div", { class: "steps-body" }, h("span", { class: "map-sub", text: t("mapping.mod.steps") }),
        steps.length ? h("table", { class: "steps-table" },
          h("thead", {}, h("tr", {}, ["trigger", "target", "hold", "threshold"].map((k) => h("th", { text: t(`mapping.step.${k}`) })), h("th"))),
          h("tbody", {}, steps.map(stepRow))) : null,
        h("button", { type: "button", class: "small", id: "br-add-step", text: t("mapping.addStep"),
          onclick: () => { const last = steps[steps.length - 1]; set({ ...m, steps: [...steps.map((x) => ({ ...x })), { trigger: round(last ? Math.min(last.trigger + 0.3, e.max) : (e.min + e.max) / 2), target: round(last ? e.max : (e.min + e.max) / 2) }] }); } }))));
}

// ------------------------------------------------------------ tab: parameters

function rangeBar(p, e) {
  const bar = h("span", { class: "range-bar", "aria-hidden": "true" });
  if (e && e.enabled !== false && !e.breathing && p && p.max > p.min) {
    const [a, b] = e.out;
    const lo = (Math.min(a, b) - p.min) / (p.max - p.min), hi = (Math.max(a, b) - p.min) / (p.max - p.min);
    const fill = h("span", { class: `range-fill${e.invert ? " inverted" : ""}` });
    fill.style.left = `${Math.max(0, lo) * 100}%`; fill.style.width = `${Math.max(0.02, Math.min(1, hi) - Math.max(0, lo)) * 100}%`;
    bar.append(fill);
  }
  return bar;
}

function parametersTab() {
  const items = (ui.show?.parameters || []).filter((p) => {
    const e = effective(p.id);
    const mapped = !!e && e.enabled !== false;
    if (ui.filter === "mapped" && !mapped) return false;
    if (ui.filter === "unmapped" && mapped) return false;
    if (ui.filter === "changed" && !ui.draft.entries.has(p.id)) return false;
    if (ui.filter === "physics" && !p.physics) return false;
    return matches(p.id, p.name, e?.input);
  }).map((p) => {
    const e = effective(p.id);
    return { id: p.id, changed: ui.draft.entries.has(p.id), cells: [
      h("span", { class: "map-name" }, h("strong", { text: p.name || p.id }), p.name ? h("small", { text: p.id }) : null),
      h("span", { class: "map-input", text: e && e.enabled !== false ? (e.breathing ? t("mapping.breathing") : e.input || "—") : "—" }),
      rangeBar(p, e),
      h("span", { class: "map-badges" }, e?.enabled === false ? h("span", { class: "badge off", text: t("mapping.source.disabled") }) : sourceBadge(e?.source),
        p.physics ? h("span", { class: "badge quiet", text: t("mapping.physics") }) : null)] };
  });
  return h("div", { class: "map-body" }, h("div", { class: "map-list-wrap" }, listbox(t("mapping.parameters"), items, ui.selected.parameters, (id) => { ui.selected.parameters = id; render(); })),
    parameterDetail(ui.selected.parameters));
}

function inputSelect(id, value) {
  const sid = "par-input";
  const known = entryInputs();
  const groups = ["bridge", "tracking"].map((kind) => {
    const names = kind === "bridge" ? bridgeNames() : ui.inputs.filter((i) => i.kind === "tracking" && !i.shadowed).map((i) => i.name);
    return names.length ? h("optgroup", { label: t(`mapping.inputKind.${kind}`) }, names.map((n) => h("option", { value: n, text: n }))) : null;
  });
  const sel = h("select", { id: sid, onchange: () => setField(id, "input", sel.value || null) },
    h("option", { value: "", text: t("mapping.chooseInput") }), groups);
  if (value && !known.includes(value)) sel.append(h("option", { value, text: value }));
  sel.value = value || "";
  return h("label", { class: "field", for: sid }, h("span", { text: t("mapping.input") }), sel);
}

function parameterDetail(id) {
  const p = param(id);
  if (!p) return h("div", { class: "map-detail" }, h("p", { class: "map-empty", text: t("mapping.pick") }));
  const e = effective(id);
  const base = baseEntry(id);
  const parts = [h("header", { class: "map-detail-head" },
    h("div", {}, h("h3", { text: p.name || p.id }), h("p", { class: "setting-help mono", text: p.id }),
      h("p", { class: "setting-help", text: t("mapping.paramRange", { min: round(p.min), max: round(p.max), def: round(p.default) }) })),
    h("div", { class: "map-badges" }, sourceBadge(e?.source), p.physics ? h("span", { class: "badge quiet", text: t("mapping.physics") }) : null))];
  if (p.physics) parts.push(h("p", { class: "pane-note", text: t("mapping.physicsNote") }));
  for (const note of base?.corrections || []) parts.push(h("p", { class: "pane-note", text: t("mapping.corrected", { note }) }));
  if (!e) {
    parts.push(h("p", { class: "setting-help", text: t("mapping.unmappedHelp") }), inputSelect(id, null));
    return h("div", { class: "map-detail" }, parts);
  }
  parts.push(toggle(t("mapping.enabled"), e.enabled !== false, (v) => setField(id, "enabled", v), t("mapping.enabledHelp")));
  if (e.enabled !== false) {
    parts.push(toggle(t("mapping.breathing"), e.breathing, (v) => setField(id, "breathing", v), t("mapping.breathingHelp")));
    if (!e.breathing) {
      parts.push(inputSelect(id, e.input));
      const info = inputInfo(e.input);
      const be = !info ? effectiveBridge(e.input) : null;
      const r = info?.range || (be ? [be.min, be.max] : null);
      const inLimits = r ? [Math.min(r[0], e.in[0], e.in[1]), Math.max(r[1], e.in[0], e.in[1])] : [Math.min(...e.in), Math.max(...e.in)];
      parts.push(rangeEditor(t("mapping.inRange"), e.in, inLimits, (v) => setField(id, "in", v), info?.meaning || null));
    }
    parts.push(rangeEditor(t("mapping.outRange"), e.out, [p.min, p.max], (v) => setField(id, "out", v)));
    parts.push(h("div", { class: "map-toggles" },
      toggle(t("mapping.invert"), e.invert, (v) => setField(id, "invert", v), t("mapping.invertHelp")),
      e.breathing ? null : toggle(t("mapping.clampIn"), e.clamp_in, (v) => setField(id, "clamp_in", v)),
      toggle(t("mapping.clampOut"), e.clamp_out, (v) => setField(id, "clamp_out", v))));
    if (!e.breathing) {
      const editor = new CurveEditor({ id: "par-curve", value: e.curve || null, exportName: id, onChange: (v) => setField(id, "curve", v) });
      ui.live.curves.push(editor);
      parts.push(h("fieldset", { class: "map-response" }, h("legend", { text: t("mapping.response") }),
        h("p", { class: "setting-help", text: t("mapping.responseHelp") }), editor.node));
    }
    parts.push(h("div", { class: "map-cutoff" },
      numberField(t("mapping.cutoff"), e.cutoff, { id: "par-cutoff", min: 0.1, step: 0.5, placeholder: e.default_cutoff != null ? fmt(e.default_cutoff, 1) : t("prefs.automatic"),
        onchange: (v) => setField(id, "cutoff", v) }),
      h("p", { class: "setting-help", text: e.default_cutoff != null ? t("mapping.cutoffHelp", { hz: fmt(e.default_cutoff, 1), group: e.filter_group || "—" }) : t("mapping.cutoffHelpNone") })));
  }
  parts.push(h("div", { class: "inline" }, h("button", { type: "button", class: "small", disabled: !ui.draft.entries.has(id), text: t(base ? "mapping.resetRow" : "mapping.removeRow"),
    onclick: () => { ui.draft.entries.delete(id); render(); } })));
  return h("div", { class: "map-detail" }, parts);
}

// ------------------------------------------------------------ frame

function resultName() { return hooks.signalsFile()?.split("/").pop().replace(/\.signals\.json$/, "") || "—"; }

function body() {
  if (ui.state === "loading" || ui.state === "idle") {
    return h("div", { class: "map-state", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      h("p", { text: t(hooks.remote() ? "mapping.loadingRemote" : "mapping.loading", { model: ui.model?.name || "" }) }));
  }
  if (ui.state === "error") {
    return h("div", { class: "map-state error", role: "alert" }, h("p", { text: t("mapping.loadFailed", { error: ui.error }) }),
      h("div", { class: "inline" }, h("button", { type: "button", class: "primary", text: t("mapping.retry"), onclick: () => load() }),
        h("button", { type: "button", text: t("run.openCompute"), onclick: () => { dlg.close(); hooks.openCompute(); } })));
  }
  const panel = { inputs: inputsTab, bridge: bridgeTab, parameters: parametersTab }[ui.tab]();
  panel.id = `map-panel-${ui.tab}`; panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", `map-tab-${ui.tab}`);
  return panel;
}

// Row badges and live values after a quiet change (the formula field keeps focus and caret).
function refreshLists() {
  const wrap = dlg.querySelector(".map-list-wrap");
  if (!wrap || ui.tab !== "bridge") return;
  const name = ui.selected.bridge;
  const e = effectiveBridge(name);
  const row = wrap.querySelector(`.map-row[data-id="${CSS.escape(name)}"]`);
  if (row && e) {
    row.classList.toggle("changed", ui.draft.bridge.has(name));
    row.querySelector(".map-name small").textContent = e.delayBuffer ? t("mapping.follows", { name: e.delayBuffer.refParam }) : e.func || "—";
    row.querySelector(".map-badges").replaceChildren(h("span", { class: "mods" }, modifierIcons(e)), sourceBadge(e.source));
  }
  renderFoot();
}

function tabs() {
  const list = h("div", { class: "seg map-tabs", role: "tablist", "aria-label": t("mapping.layers") },
    TABS.map((id) => h("button", { type: "button", role: "tab", id: `map-tab-${id}`, "aria-selected": String(ui.tab === id), "aria-controls": `map-panel-${id}`,
      tabindex: ui.tab === id ? "0" : "-1", class: ui.tab === id ? "on" : null, disabled: ui.state !== "ready",
      onclick: () => { ui.tab = id; render(); dlg.querySelector(`#map-tab-${id}`)?.focus(); } },
      h("span", { text: t(`mapping.tab.${id}`) }),
      count(id) ? h("span", { class: "tab-count", text: String(count(id)) }) : null)));
  list.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const i = TABS.indexOf(ui.tab);
    ui.tab = TABS[(i + (e.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length];
    render(); dlg.querySelector(`#map-tab-${ui.tab}`)?.focus();
  });
  return list;
}
function count(tab) { return tab === "inputs" ? Object.keys(ui.draft.curves).length : tab === "bridge" ? ui.draft.bridge.size : ui.draft.entries.size; }

function foot() {
  const ready = ui.state === "ready";
  const status = ui.notice ? t(ui.notice.key, ui.notice.params) : t(changeCount() ? "mapping.changedCount" : "mapping.noChanges", { count: changeCount() });
  const apply = hooks.canApply();
  const preview = hooks.canPreview();
  return h("footer", { class: "map-foot" },
    h("div", { class: "map-foot-text" },
      h("p", { class: `map-status${ui.notice?.bad ? " bad" : ""}`, role: "status", text: status }),
      // Left-to-right marks keep the leading "/" in place while the start of a long path is cut.
      h("p", { class: "setting-help mono map-file", text: ui.file ? `\u200E${ui.file}\u200E` : "", title: ui.file || "" })),
    h("div", { class: "inline wrap" },
      h("button", { type: "button", class: "small", disabled: !ready, text: t("mapping.import"), onclick: () => guard(async () => {
        const r = await api.mapping.importFile(); if (!r) return;
        ui.draft = deserialize(r.mapping); ui.notice = { key: "mapping.imported", params: { name: r.name } }; render();
      }) }),
      h("button", { type: "button", class: "small", disabled: !ready || !changeCount(), text: t("mapping.export"), onclick: () => guard(async () => {
        const name = await api.mapping.exportFile({ modelDir: ui.model.dir, mapping: serialize(ui.draft) });
        if (name) { ui.notice = { key: "mapping.exported", params: { name } }; render(); }
      }) }),
      h("button", { type: "button", class: "small", disabled: !ui.model, text: t("mapping.reveal"), onclick: () => api.mapping.reveal(ui.model.dir) }),
      h("button", { type: "button", class: "small danger", disabled: !ready || !changeCount(), text: t("mapping.resetAll"), onclick: () => { ui.draft = emptyDraft(); render(); } }),
      h("button", { type: "button", class: "small", disabled: !ready || !dirty(), text: t("mapping.revert"), onclick: () => { ui.draft = deserialize(serialize(ui.saved)); ui.notice = null; render(); } }),
      h("button", { type: "button", id: "map-preview", disabled: !ready || !preview, title: preview ? t("mapping.previewTitle", { name: preview.name }) : t("mapping.previewNeedsResult"),
        text: t("mapping.preview"), onclick: () => guard(async () => { dlg.close(); await hooks.preview(serialize(ui.draft)); }) }),
      h("button", { type: "button", id: "map-save", class: "primary", disabled: !ready || !dirty(), text: t("mapping.save"), onclick: () => guard(save) }),
      apply ? h("button", { type: "button", id: "map-apply", class: "primary", disabled: !ready, text: t(dirty() ? "mapping.saveApply" : "mapping.apply", { name: apply.name }), onclick: () => guard(async () => {
        if (dirty()) await save();
        dlg.close();
        await hooks.apply();
      }) }) : null));
}
function renderFoot() { const f = dlg.querySelector(".map-foot"); if (f) f.replaceWith(foot()); dlg.dataset.dirty = String(dirty()); }

function render() {
  if (!dlg.open) return;
  uid = 0;  // the same controls get the same ids, so focus survives a re-render
  const focusId = dlg.contains(document.activeElement) ? document.activeElement.id : null;
  const focusRow = document.activeElement?.closest?.(".map-row") ? document.activeElement.dataset.id : null;
  const scroll = dlg.querySelector(".map-list-wrap")?.scrollTop || 0;
  const detailScroll = dlg.querySelector(".map-detail")?.scrollTop || 0;
  ui.live = { graphs: [], curves: [] };
  const ready = ui.state === "ready";
  const search = h("input", { type: "search", id: "map-search", class: "settings-search", value: ui.query, placeholder: t("mapping.search"), "aria-label": t("mapping.search"), disabled: !ready,
    oninput: () => { ui.query = search.value; render(); } });
  const filters = h("div", { class: "seg map-filters", role: "group", "aria-label": t("mapping.filter") },
    (ui.tab === "parameters" ? FILTERS : ["all", "changed"]).map((f) => h("button", { type: "button", class: ui.filter === f ? "on" : null,
      "aria-pressed": String(ui.filter === f), disabled: !ready, text: t(`mapping.filter.${f}`), onclick: () => { ui.filter = f; render(); } })));
  if (ui.tab !== "parameters" && !["all", "changed"].includes(ui.filter)) ui.filter = "all";
  const close = h("button", { type: "button", class: "icon close", html: ICONS.close, "aria-label": t("prefs.close"), title: t("prefs.close"), onclick: () => requestClose() });
  const top = h("header", { class: "settings-top map-top" },
    h("div", { class: "map-title" }, h("h2", { id: "mapping-title", text: t("mapping.title") }),
      h("span", { class: "hint", text: ui.show ? t(`mapping.base.${ui.show.base}`, { model: ui.model?.name || "" }) : ui.model?.name || "" })),
    tabs(), search, filters, close);
  dlg.replaceChildren(h("div", { class: "settings-frame map-frame" }, top, body(), foot()));
  dlg.dataset.dirty = String(dirty());
  const wrap = dlg.querySelector(".map-list-wrap"); if (wrap) wrap.scrollTop = scroll;
  const det = dlg.querySelector(".map-detail"); if (det) det.scrollTop = detailScroll;
  if (focusRow) dlg.querySelector(`.map-row[data-id="${CSS.escape(focusRow)}"]`)?.focus();
  else if (focusId && document.getElementById(focusId)) {
    const el = document.getElementById(focusId);
    el.focus();
    if (focusId === "map-search") el.setSelectionRange(el.value.length, el.value.length);
  }
  updateLive();
}

async function requestClose() {
  if (dirty() && !await api.confirm({ message: t("mapping.discardMessage"), detail: t("mapping.discardDetail"), confirmLabel: t("editor.discard") })) return;
  ui.draft = deserialize(serialize(ui.saved));
  ui.playing = false;
  dlg.close();
}

dlg.addEventListener("cancel", (e) => { e.preventDefault(); requestClose(); });
dlg.addEventListener("click", (e) => { if (e.target === dlg) requestClose(); });
dlg.addEventListener("close", () => { ui.notice = null; ui.playing = false; });
// Canvases read theme colours when drawn.
new MutationObserver(() => render()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
