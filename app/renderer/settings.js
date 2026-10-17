// The Settings window: an in-window modal <dialog> with a section list, one pane per section
// and a search box that filters settings across all sections. Generation defaults and Tuning
// are generated from the v2l configuration schema (src/v2l/config.py); nothing here is
// written per field. Every change is saved immediately through prefs (no Save button).
import { LANGUAGES, language, languageName, t, tIn } from "./i18n.js";
import { errorText, prefs } from "./prefs.js";

const api = window.v2l;
// Lucide icons (ISC license): x, rotate-ccw, lock. Inlined so they follow currentColor.
const LUCIDE = (inner) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const ICONS = {
  close: LUCIDE('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
  reset: LUCIDE('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>'),
  lock: LUCIDE('<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
};
const dlg = document.getElementById("settings");

export const SECTIONS = ["general", "compute", "output", "generation", "tuning", "presets", "viewer", "camera", "agents", "advanced", "about"];
// Schema fields shown outside Tuning; the bench owns the trim range.
const GENERATION_KEYS = ["style.name", "output.loop", "output.interpolation", "input.flip", "expressions.enabled", "expressions.stride", "output.fps", "output.fade", "bridge.preset"];
const OUTPUT_KEYS = ["preview.enabled"];
const PER_RUN_ONLY = ["input.start", "input.end"];
const TUNING_GROUPS = ["calibration", "intensity", "gaps", "expressions", "style", "filtering", "bridge", "io"];
const SMOOTHING_MARKS = [["prefs.smoothing.crisp", 1.5], ["prefs.smoothing.normal", 1.0], ["prefs.smoothing.smooth", 0.6]];
const CHOICE_KEYS = { "style.name": { cute: "settings.styleCute", faithful: "settings.styleFaithful" },
                      "output.interpolation": { linear: "editor.type.linear", bezier: "editor.type.bezier" } };

let hooks = { benchCommand: async () => null, onCheck: () => {}, cameras: async () => [], openMapping: () => {}, hasModel: () => false };
let active = "general";
let query = "";
let syncs = [];
let rendered = false;
let uid = 0;
const ui = {
  check: { remote: null, local: null }, checking: null,
  task: { kind: null, id: null, log: { remote: [], local: [] }, code: { remote: undefined, local: undefined } },
  cache: null, about: null, mcp: null, bench: null, presetForm: null, renaming: null, cameras: null, notice: null,
};

// ---------------------------------------------------------------- DOM helpers

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c != null && c !== false));
  return node;
}
const nextId = (stem) => `${stem}-${++uid}`;
const searchText = (...parts) => parts.filter(Boolean).join(" ").toLowerCase();

function decimals(step) { return (String(step ?? 1).split(".")[1] || "").length; }
function normalize(f, value) {
  let v = Number(value);
  if (f.min != null) v = Math.max(f.min, v);
  if (f.max != null) v = Math.min(f.max, v);
  return f.kind === "integer" ? Math.round(v) : Number(v.toFixed(Math.max(decimals(f.step), 2)));
}
function formatValue(f, v) {
  if (v === null) return t("prefs.automatic");
  if (Array.isArray(v)) return v.join(" – ");
  if (typeof v === "boolean") return t(v ? "prefs.on" : "prefs.off");
  return `${v}${f?.unit ? ` ${f.unit}` : ""}`;
}

function lockNote(env) {
  const p = h("p", { class: "setting-lock" });
  p.innerHTML = ICONS.lock;
  p.append(h("span", { text: t("prefs.lockedBy", { env }) }));
  return p;
}

// One setting: label, help and lock/badge on the left, the control on the right.
function row({ key, label, help, search = [], badge, advanced, lock, full }) {
  const labelId = nextId("label");
  const node = h("div", { class: `setting${advanced ? " is-advanced" : ""}${full ? " full" : ""}${lock ? " locked" : ""}`, "data-key": key || null });
  const err = h("p", { class: "setting-error", role: "alert", hidden: true });
  const text = h("div", { class: "setting-text" },
    h("div", { class: "setting-label", id: labelId }, h("span", { text: label }),
      advanced ? h("span", { class: "badge quiet", text: t("prefs.badge.advanced") }) : null,
      badge ? h("span", { class: "badge", text: badge }) : null),
    help ? h("p", { class: "setting-help", text: help }) : null,
    lock ? lockNote(lock) : null,
    err);
  const control = h("div", { class: "setting-control" });
  node.append(text, control);
  node.dataset.search = searchText(label, help, ...search, key);
  return { node, control, labelId, error: (message) => { err.hidden = !message; err.textContent = message || ""; } };
}

async function commit(r, fn) {
  try { await fn(); r.error(null); } catch (err) { r.error(errorText(err)); runSyncs(); }
}

function resetButton(isDefault, reset, describe) {
  const b = h("button", { type: "button", class: "icon reset", onclick: reset });
  b.innerHTML = ICONS.reset;
  return { node: b, sync: () => {
    b.hidden = isDefault();
    const text = t("prefs.resetTo", { value: describe() });
    b.title = text; b.setAttribute("aria-label", text);
  } };
}

// ---------------------------------------------------------------- schema widgets

function numberInput(f, id, extra = {}) {
  return h("input", { type: "number", id, class: "num", min: f.min ?? null, max: f.max ?? null, step: f.step ?? "any", inputmode: "decimal", ...extra });
}

function widget(f, id, r, labelId, set) {
  const key = f.key;
  if (f.kind === "boolean") {
    const box = h("input", { type: "checkbox", role: "switch", id, class: "switch", onchange: () => commit(r, () => set(box.checked)) });
    return { node: box, sync: (v) => { box.checked = !!v; } };
  }
  if (f.kind === "enum") {
    const names = CHOICE_KEYS[key] || {};
    const sel = h("select", { id, onchange: () => commit(r, () => set(sel.value)) },
      f.choices.map((c) => h("option", { value: c, text: names[c] ? t(names[c]) : c })));
    return { node: sel, sync: (v) => { sel.value = v; } };
  }
  if (f.kind === "string") {
    const input = h("input", { type: "text", id, spellcheck: "false", onchange: () => commit(r, () => set(input.value.trim() || null)) });
    const parts = [input];
    if (key === "bridge.preset") {
      parts.push(h("button", { type: "button", text: t("prefs.chooseFile"), onclick: async () => {
        const file = await api.settings.chooseFile("json");
        if (file) commit(r, () => set(file));
      } }));
    }
    return { node: h("div", { class: "inline" }, parts), sync: (v) => { if (document.activeElement !== input) input.value = v ?? ""; } };
  }
  if (f.kind === "integer" || f.kind === "number") {
    const num = numberInput(f, id, { placeholder: f.nullable ? t("prefs.automatic") : null });
    const unit = f.unit ? h("span", { class: "unit", text: f.unit }) : null;
    num.addEventListener("change", () => {
      if (num.value === "" && f.nullable) { commit(r, () => set(null)); return; }
      if (num.value === "" || num.validity.badInput) { runSyncs(); return; }
      commit(r, () => set(normalize(f, num.value)));
    });
    if (f.min == null || f.max == null) {
      return { node: h("div", { class: "inline" }, num, unit), sync: (v) => { num.value = v ?? ""; } };
    }
    const slider = h("input", { type: "range", min: f.min, max: f.max, step: f.step ?? "any", "aria-labelledby": labelId,
      oninput: () => { num.value = slider.value; }, onchange: () => commit(r, () => set(normalize(f, slider.value))) });
    const parts = [h("div", { class: "slider" }, slider, num, unit)];
    let marks = null;
    if (key === "filtering.cutoff_scale") {
      marks = SMOOTHING_MARKS.map(([label, value]) => h("button", { type: "button", "aria-pressed": "false", "data-value": value,
        onclick: () => commit(r, () => set(value)) }, h("span", { text: t(label) }), h("small", { text: `${value}×` })));
      parts.push(h("div", { class: "seg marks", role: "group", "aria-label": t("prefs.smoothing.marks") }, marks));
    }
    return { node: h("div", { class: "stack" }, parts), sync: (v) => {
      slider.value = v; num.value = v;
      for (const m of marks || []) m.setAttribute("aria-pressed", String(Number(m.dataset.value) === v));
    } };
  }
  if (f.kind === "range" || f.kind === "pair" || f.kind === "window") {
    const [la, lb] = { range: ["prefs.low", "prefs.high"], pair: ["prefs.horizontal", "prefs.vertical"], window: ["prefs.start", "prefs.end"] }[f.kind];
    const a = numberInput(f, id), b = numberInput(f, nextId("b"));
    const unit = f.unit ? h("span", { class: "unit", text: f.unit }) : null;
    let auto = null;
    const read = () => [Number(a.value), Number(b.value)];
    const valid = ([x, y]) => [x, y].every(Number.isFinite) && (f.kind === "pair" || (f.kind === "range" ? x <= y : x < y));
    const push = (changed) => {
      if (a.value === "" || b.value === "") return;
      let [x, y] = read().map((v) => normalize({ ...f, kind: "number" }, v));
      if (f.kind === "range" && x > y) { if (changed === a) y = x; else x = y; }
      if (!valid([x, y])) { r.error(t("prefs.windowInvalid")); return; }
      commit(r, () => set([x, y]));
    };
    a.addEventListener("change", () => push(a));
    b.addEventListener("change", () => push(b));
    const pairNode = h("div", { class: "pair-input" },
      h("label", {}, h("span", { text: t(la) }), a), h("label", {}, h("span", { text: t(lb) }), b), unit);
    if (f.nullable) {
      const autoId = nextId("auto");
      auto = h("input", { type: "checkbox", id: autoId, onchange: () => {
        if (auto.checked) commit(r, () => set(null));
        else { a.disabled = b.disabled = false; a.value = a.value || "0"; b.value = b.value || "1"; push(b); }
      } });
      return { node: h("div", { class: "stack" }, h("label", { class: "chk", for: autoId }, auto, h("span", { text: t("prefs.automatic") })), pairNode),
        sync: (v) => { auto.checked = v === null; a.disabled = b.disabled = v === null; if (v) { a.value = v[0]; b.value = v[1]; } else { a.value = ""; b.value = ""; } } };
    }
    return { node: pairNode, sync: (v) => { a.value = v[0]; b.value = v[1]; } };
  }
  return { node: h("span", { text: String(f.kind) }), sync: () => {} };
}

function fieldRow(key, { prominent } = {}) {
  const f = prefs.field(key);
  const id = nextId(`f-${key.replace(".", "-")}`);
  const labelKey = `param.${key}.label`, helpKey = `param.${key}.help`;
  const r = row({ key, label: t(labelKey), help: t(helpKey), search: [tIn("en", labelKey)], advanced: f.advanced,
                  badge: f.affects === "tracking" ? t("prefs.badge.tracking") : null });
  if (prominent) r.node.classList.add("prominent");
  r.node.querySelector(".setting-label").setAttribute("for", id);
  const set = (value) => prefs.setConfig(key, value);
  const w = widget(f, id, r, r.labelId, set);
  const reset = resetButton(() => prefs.isConfigDefault(key), () => commit(r, () => set(null)), () => formatValue(f, prefs.configDefault(key)));
  r.control.append(w.node, reset.node);
  const label = r.node.querySelector(".setting-label");
  const realLabel = h("label", { class: "setting-label", id: r.labelId, for: id }, ...label.childNodes);
  label.replaceWith(realLabel);
  syncs.push(() => { w.sync(prefs.config(key)); reset.sync(); });
  return r.node;
}

// ---------------------------------------------------------------- app-setting widgets

function appRow(key, { label, help, control, extra, labelFor }) {
  const r = row({ key, label: t(label), help: help ? t(help) : null, search: [tIn("en", label)], lock: prefs.lock(key) });
  if (labelFor) {
    const old = r.node.querySelector(".setting-label");
    old.replaceWith(h("label", { class: "setting-label", id: r.labelId, for: labelFor }, ...old.childNodes));
  }
  r.control.append(...[control].flat().filter(Boolean));
  if (!prefs.lock(key)) {
    const reset = resetButton(() => prefs.isAppDefault(key), () => commit(r, () => prefs.setApp(key, null)),
      () => describeApp(key, prefs.appDefault(key)));
    r.control.append(reset.node);
    syncs.push(reset.sync);
  }
  if (extra) r.node.append(extra);
  return r;
}

function describeApp(key, value) {
  if (typeof value === "boolean") return t(value ? "prefs.on" : "prefs.off");
  if (value === "") return t(`prefs.default.${key}`);
  if (prefs.appChoices(key)) return t(`prefs.choice.${key}.${value}`);
  return String(value);
}

function appToggle(key, label, help) {
  const id = nextId("t");
  let r;
  const box = h("input", { type: "checkbox", role: "switch", id, class: "switch", disabled: !!prefs.lock(key),
    onchange: () => commit(r, () => prefs.setApp(key, box.checked)) });
  r = appRow(key, { label, help, control: box, labelFor: id });
  syncs.push(() => { box.checked = prefs.app(key); });
  return r.node;
}

function appSelect(key, label, help, options) {
  const id = nextId("s");
  let r;
  const sel = h("select", { id, disabled: !!prefs.lock(key), onchange: () => commit(r, () => prefs.setApp(key, sel.value)) });
  const fill = () => {
    const opts = typeof options === "function" ? options() : options;
    sel.replaceChildren(...opts.map(([value, text]) => h("option", { value, text })));
    sel.value = prefs.app(key);
  };
  r = appRow(key, { label, help, control: sel, labelFor: id });
  syncs.push(fill);
  return r.node;
}

function appChoice(key, label, help) {
  // A radio group for two or three choices.
  let r;
  const name = nextId("radio");
  const radios = prefs.appChoices(key).map((value) => {
    const input = h("input", { type: "radio", name, value, disabled: !!prefs.lock(key), onchange: () => commit(r, () => prefs.setApp(key, value)) });
    return { value, input, node: h("label", { class: "radio" }, input, h("span", { text: t(`prefs.choice.${key}.${value}`) })) };
  });
  r = appRow(key, { label, help, control: h("div", { class: "radios", role: "radiogroup", "aria-label": t(label) }, radios.map((x) => x.node)) });
  syncs.push(() => { for (const x of radios) x.input.checked = prefs.app(key) === x.value; });
  return r.node;
}

function appText(key, label, help, { placeholder, button } = {}) {
  const id = nextId("x");
  let r;
  const input = h("input", { type: "text", id, spellcheck: "false", autocomplete: "off", readonly: !!prefs.lock(key), "aria-readonly": prefs.lock(key) ? "true" : null,
    onchange: () => commit(r, () => prefs.setApp(key, input.value.trim() || null)) });
  r = appRow(key, { label, help, control: h("div", { class: "inline" }, input, button || null), labelFor: id });
  syncs.push(() => {
    if (document.activeElement !== input) input.value = prefs.app(key);
    input.placeholder = typeof placeholder === "function" ? placeholder() : placeholder || "";
  });
  return { node: r.node, row: r, input };
}

function appFolder(key, label, help, { placeholder, reveal } = {}) {
  let r;
  const path = h("output", { class: "path" });
  const choose = prefs.lock(key) ? null : h("button", { type: "button", text: t("prefs.choose"), onclick: async () => {
    const dir = await api.settings.chooseFolder(key);
    if (dir) commit(r, () => prefs.setApp(key, dir));
  } });
  const open = reveal ? h("button", { type: "button", text: t("prefs.reveal"), onclick: () => api.openFolder("output") }) : null;
  r = appRow(key, { label, help, control: h("div", { class: "folder" }, path, choose || open ? h("div", { class: "inline" }, choose, open) : null) });
  syncs.push(() => {
    const value = prefs.app(key);
    // Left-to-right marks keep the leading "/" in place while the start of a long path is cut.
    path.textContent = `\u200E${value || placeholder()}\u200E`;
    path.title = value || placeholder();
    path.classList.toggle("placeholder", !value);
  });
  return r.node;
}

function actionRow(label, help, control) {
  const r = row({ label: t(label), help: help ? t(help) : null, search: [tIn("en", label)] });
  r.control.append(...[control].flat().filter(Boolean));
  return r;
}

function note(key, params) { return h("p", { class: "pane-note", text: t(key, params) }); }

function codeBlock(label, help, text, { wide = true } = {}) {
  const pre = h("pre", { class: "code", tabindex: "0", text: text ?? "…" });
  let r;
  const copy = h("button", { type: "button", text: t("prefs.copy"), onclick: async () => {
    await api.copy(pre.textContent);
    copy.textContent = t("prefs.copied");
    setTimeout(() => { copy.textContent = t("prefs.copy"); }, 1600);
  } });
  r = row({ label: t(label), help: help ? t(help) : null, search: [tIn("en", label)], full: wide });
  r.control.append(pre, copy);
  return { node: r.node, pre };
}

// ---------------------------------------------------------------- sections

function paneHead(id) {
  return h("header", { class: "pane-head" },
    h("h3", { id: `pane-${id}-title`, text: t(`prefs.section.${id}`) }),
    h("p", { class: "pane-help", text: t(`prefs.section.${id}.help`) }));
}

function general() {
  return [
    appSelect("general.language", "prefs.language", "prefs.language.help",
      () => [["system", t("prefs.language.system", { name: languageName(matchSystem()) })], ...LANGUAGES.map((l) => [l, languageName(l)])]),
    appChoice("general.theme", "prefs.theme", "prefs.theme.help"),
    appFolder("general.modelDir", "prefs.modelDir", "prefs.modelDir.help", { placeholder: () => t("prefs.default.general.modelDir") }),
    appToggle("general.openLastModel", "prefs.openLastModel", "prefs.openLastModel.help"),
  ];
}

function matchSystem() {
  const tag = String(navigator.language || "").toLowerCase();
  return tag.startsWith("ko") ? "ko" : tag.startsWith("ja") ? "ja" : tag.startsWith("zh") ? "zh-CN" : "en";
}

function checkOutput(mode) {
  const out = h("p", { class: "inline-result", role: "status" });
  syncs.push(() => {
    const res = ui.check[mode];
    out.className = "inline-result";
    if (ui.checking === mode) { out.textContent = t("prefs.compute.checking"); return; }
    if (!res) { out.textContent = ""; return; }
    out.classList.add(res.ok ? "ok" : "bad");
    if (res.ok && mode === "remote") out.textContent = t(res.installed ? "prefs.compute.remoteOk" : "prefs.compute.remoteNotInstalled", { ms: res.latency_ms });
    else if (res.ok) out.textContent = t("prefs.compute.localOk", { version: res.version, python: res.python || "?" });
    else if (!res.configured && mode === "remote") out.textContent = t("prefs.compute.noHost");
    else out.textContent = t("prefs.compute.failed", { error: res.error?.message || t("prefs.compute.unknownError") });
  });
  return out;
}

export async function runCheck(mode = prefs.app("compute.mode")) {
  ui.checking = mode; runSyncs();
  let res;
  try { res = await api.compute.check(); } catch (err) { res = { ok: false, configured: true, error: { message: errorText(err) } }; }
  ui.checking = null; ui.check[res.mode || mode] = res; runSyncs();
  hooks.onCheck(res);
  return res;
}

function taskBlock(mode) {
  const kind = mode === "remote" ? "setup-remote" : "setup-local";
  const log = h("pre", { class: "code log-view", tabindex: "0", "aria-live": "off" });
  const state = h("p", { class: "inline-result", role: "status" });
  const start = h("button", { type: "button", text: t(mode === "remote" ? "prefs.compute.setupRemote" : "prefs.compute.setupLocal"),
    onclick: async () => {
      ui.task.log[mode] = []; ui.task.code[mode] = undefined;
      try { const { id } = await api.compute.startTask(kind); ui.task.kind = kind; ui.task.id = id; }
      catch (err) { ui.task.log[mode] = [errorText(err)]; ui.task.code[mode] = -1; }
      runSyncs();
    } });
  const stop = h("button", { type: "button", text: t("prefs.compute.stopSetup"), onclick: () => api.compute.cancelTask() });
  const r = actionRow(mode === "remote" ? "prefs.compute.setupRemote" : "prefs.compute.setupLocal",
    mode === "remote" ? "prefs.compute.setupRemote.help" : "prefs.compute.setupLocal.help", [start, stop]);
  r.node.classList.add("full");
  r.node.append(state, log);
  syncs.push(() => {
    const running = ui.task.kind === kind;
    start.disabled = !!ui.task.kind; stop.hidden = !running;
    log.hidden = !ui.task.log[mode].length;
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    log.textContent = ui.task.log[mode].join("\n");
    if (follow) log.scrollTop = log.scrollHeight;
    const code = ui.task.code[mode];
    state.className = `inline-result${code === 0 ? " ok" : code != null ? " bad" : ""}`;
    state.textContent = running ? t("prefs.compute.setupRunning") : code === 0 ? t("prefs.compute.setupDone") : code != null ? t("prefs.compute.setupFailed", { code }) : "";
  });
  return r.node;
}

function compute() {
  const mode = appChoice("compute.mode", "prefs.compute.mode", "prefs.compute.mode.help");
  const remote = h("div", { class: "group", "data-mode": "remote" },
    appText("compute.host", "prefs.compute.host", "prefs.compute.host.help", { placeholder: "user@host" }).node,
    appText("compute.remoteDir", "prefs.compute.remoteDir", "prefs.compute.remoteDir.help", { placeholder: "v2l" }).node,
    (() => { const r = actionRow("prefs.compute.test", "prefs.compute.test.help", h("button", { type: "button", text: t("prefs.compute.test"), onclick: () => runCheck("remote") })); r.node.append(checkOutput("remote")); return r.node; })(),
    taskBlock("remote"));
  const exe = appText("compute.executable", "prefs.compute.executable", "prefs.compute.executable.help", {
    placeholder: () => prefs.snap.paths.executable,
    button: h("button", { type: "button", text: t("prefs.chooseFile"), onclick: async () => {
      const file = await api.settings.chooseFile("executable");
      if (file) await commit(exe.row, () => prefs.setApp("compute.executable", file));
    } }),
  });
  const local = h("div", { class: "group", "data-mode": "local" }, exe.node,
    (() => { const r = actionRow("prefs.compute.testLocal", "prefs.compute.testLocal.help", h("button", { type: "button", text: t("prefs.compute.testLocal"), onclick: () => runCheck("local") })); r.node.append(checkOutput("local")); return r.node; })(),
    taskBlock("local"));
  syncs.push(() => { const m = prefs.app("compute.mode"); remote.hidden = m !== "remote"; local.hidden = m !== "local"; });
  return [mode, remote, local];
}

function output() {
  return [
    appFolder("output.dir", "prefs.output.dir", "prefs.output.dir.help", { placeholder: () => prefs.snap.paths.outDir, reveal: true }),
    appChoice("output.onExists", "prefs.output.onExists", "prefs.output.onExists.help"),
    appToggle("output.autoInstall", "prefs.output.autoInstall", "prefs.output.autoInstall.help"),
    ...OUTPUT_KEYS.map((k) => fieldRow(k)),
  ];
}

function presetTitle(p) {
  if (!p) return "";
  return p.builtin && t(`preset.${p.name}.title`) !== `preset.${p.name}.title` ? t(`preset.${p.name}.title`) : p.title || p.name;
}
function presetDescription(p) {
  return p.builtin && t(`preset.${p.name}.description`) !== `preset.${p.name}.description` ? t(`preset.${p.name}.description`) : p.description || "";
}
export { presetTitle };

function generation() {
  return [
    note("prefs.nextRun"),
    appSelect("generation.preset", "prefs.generation.preset", "prefs.generation.preset.help",
      () => prefs.presets().map((p) => [p.name, presetTitle(p)])),
    ...GENERATION_KEYS.map((k) => fieldRow(k)),
    mappingRow(),
  ];
}

// Deep link to the open model's mapping editor (overrides are stored per model).
function mappingRow() {
  const button = h("button", { type: "button", text: t("mapping.open"), onclick: () => { dlg.close(); hooks.openMapping(); } });
  const r = actionRow("prefs.mapping", "prefs.mapping.help", button);
  syncs.push(() => { button.disabled = !hooks.hasModel(); });
  return r.node;
}

function tuningGroups() {
  const placed = new Set([...GENERATION_KEYS, ...OUTPUT_KEYS, ...PER_RUN_ONLY]);
  const groups = [];
  for (const id of TUNING_GROUPS) {
    let keys;
    if (id === "io") keys = prefs.fields().filter((f) => ["input", "output", "preview"].includes(f.section) && !placed.has(f.key)).map((f) => f.key);
    else if (id === "bridge") keys = prefs.section("bridge").fields.map((f) => f.key);
    else keys = prefs.section(id).fields.map((f) => f.key).filter((k) => !placed.has(k));
    if (id === "filtering") keys = ["filtering.cutoff_scale", ...keys.filter((k) => k !== "filtering.cutoff_scale")];
    groups.push({ id, keys });
  }
  return groups;
}

function tuning() {
  const adv = appToggle("tuning.showAdvanced", "prefs.tuning.showAdvanced", "prefs.tuning.showAdvanced.help");
  adv.classList.add("compact");
  syncs.push(() => { dlg.dataset.advanced = String(prefs.app("tuning.showAdvanced")); });
  const parts = [note("prefs.nextRun"), note("prefs.tuning.order"), adv];
  for (const g of tuningGroups()) {
    const label = g.id === "io" ? t("prefs.tuning.io") : t(`section.${g.id}.label`);
    const help = g.id === "io" ? t("prefs.tuning.io.help") : t(`section.${g.id}.help`);
    const reset = h("button", { type: "button", class: "small", text: t("prefs.resetGroup"), onclick: () => prefs.set({ config: Object.fromEntries(g.keys.map((k) => [k, null])) }) });
    const head = h("header", { class: "group-head" }, h("div", {}, h("h4", { id: `tuning-${g.id}`, text: label }), h("p", { class: "setting-help", text: help })), reset);
    syncs.push(() => { reset.disabled = g.keys.every((k) => prefs.isConfigDefault(k)); });
    parts.push(h("section", { class: "group tuning-group", "data-group": g.id, "aria-labelledby": `tuning-${g.id}` }, head,
      g.keys.map((k) => fieldRow(k, { prominent: k === "filtering.cutoff_scale" }))));
  }
  return parts;
}

function presets() {
  const list = h("ul", { class: "preset-list" });
  const form = h("form", { class: "preset-form", hidden: true });
  const nameIn = h("input", { type: "text", id: nextId("pn"), required: true, pattern: "[A-Za-z0-9_\\-]{1,64}", maxlength: 64, spellcheck: "false" });
  const titleIn = h("input", { type: "text", id: nextId("pt"), maxlength: 80 });
  const descIn = h("input", { type: "text", id: nextId("pd"), maxlength: 300 });
  const formError = h("p", { class: "setting-error", role: "alert", hidden: true });
  form.append(
    h("label", { class: "field", for: nameIn.id }, h("span", { text: t("prefs.presets.name") }), nameIn, h("small", { class: "setting-help", text: t("prefs.presets.nameHelp") })),
    h("label", { class: "field", for: titleIn.id }, h("span", { text: t("prefs.presets.title") }), titleIn),
    h("label", { class: "field", for: descIn.id }, h("span", { text: t("prefs.presets.description") }), descIn),
    formError,
    h("div", { class: "inline" }, h("button", { type: "submit", class: "primary", text: t("prefs.presets.save") }),
      h("button", { type: "button", text: t("main.cancel"), onclick: () => { form.hidden = true; } })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(nameIn.value)) { formError.hidden = false; formError.textContent = t("prefs.presets.nameHelp"); nameIn.focus(); return; }
    try {
      const next = await api.presets.save({ name: nameIn.value, title: titleIn.value.trim() || nameIn.value, description: descIn.value.trim() });
      if (next) { prefs.replace(next); form.hidden = true; ui.notice = t("prefs.presets.saved", { name: nameIn.value }); runSyncs(); }
    } catch (err) { formError.hidden = false; formError.textContent = errorText(err); }
  });
  const run = (fn) => async () => {
    try { const next = await fn(); if (next && typeof next === "object") prefs.replace(next); }
    catch (err) { ui.notice = errorText(err); runSyncs(); }
  };
  const toolbar = h("div", { class: "toolbar" },
    h("button", { type: "button", class: "primary", text: t("prefs.presets.saveAs"), onclick: () => {
      form.hidden = false; formError.hidden = true; nameIn.value = ""; titleIn.value = ""; descIn.value = ""; nameIn.focus();
    } }),
    h("button", { type: "button", text: t("prefs.presets.import"), onclick: run(() => api.presets.importFile()) }),
    h("button", { type: "button", text: t("prefs.presets.reveal"), onclick: () => api.openFolder("presets") }));
  const status = h("p", { class: "inline-result", role: "status" });
  const fill = () => {
    status.textContent = ui.notice || "";
    list.replaceChildren(...prefs.snap.presets.map((p) => {
      const isDefault = prefs.app("generation.preset") === p.name;
      const title = presetTitle(p);
      const item = h("li", { class: "preset setting", "data-search": searchText(title, presetDescription(p), p.name, p.title) });
      const renameIn = h("input", { type: "text", value: p.name, "aria-label": t("prefs.presets.rename"), pattern: "[A-Za-z0-9_\\-]{1,64}" });
      const actions = h("div", { class: "inline wrap" },
        h("button", { type: "button", class: "small", text: t("prefs.presets.apply"), disabled: !!p.error, onclick: run(async () => {
          const next = await api.presets.apply({ name: p.name });
          if (next) ui.notice = t("prefs.presets.applied", { name: title });
          return next;
        }) }),
        h("button", { type: "button", class: "small", text: t("prefs.presets.export"), onclick: run(() => api.presets.exportFile({ name: p.name })) }),
        p.builtin ? null : h("button", { type: "button", class: "small", text: t("prefs.presets.rename"), onclick: () => { ui.renaming = p.name; fill(); } }),
        p.builtin ? null : h("button", { type: "button", class: "small danger", text: t("prefs.presets.delete"), onclick: run(() => api.presets.remove({ name: p.name })) }));
      const renameForm = ui.renaming === p.name ? h("form", { class: "inline", onsubmit: (e) => { e.preventDefault(); ui.renaming = null; run(() => api.presets.rename({ name: p.name, next: renameIn.value.trim() }))(); } },
        renameIn, h("button", { type: "submit", class: "small primary", text: t("prefs.presets.renameSave") }),
        h("button", { type: "button", class: "small", text: t("main.cancel"), onclick: () => { ui.renaming = null; fill(); } })) : null;
      item.append(h("div", { class: "setting-text" },
        h("div", { class: "setting-label" }, h("span", { text: title }),
          p.builtin ? h("span", { class: "badge quiet", text: t("prefs.presets.builtin") }) : h("span", { class: "badge quiet", text: t("prefs.presets.user") }),
          isDefault ? h("span", { class: "badge", text: t("prefs.presets.isDefault") }) : null),
        h("p", { class: "setting-help", text: p.error ? t("prefs.presets.invalid", { error: p.error }) : presetDescription(p) }),
        h("p", { class: "setting-help mono", text: p.name })), h("div", { class: "setting-control" }, renameForm || actions));
      if (renameForm) queueMicrotask(() => renameIn.focus());
      return item;
    }));
    filter();
  };
  syncs.push(fill);
  return [note("prefs.presets.note", { dir: prefs.snap.paths.presetsDir }), toolbar, form, status, list];
}

function viewer() {
  return [
    appToggle("viewer.physics", "prefs.viewer.physics", "prefs.viewer.physics.help"),
    appToggle("viewer.tex4k", "prefs.viewer.tex4k", "prefs.viewer.tex4k.help"),
    appSelect("viewer.frame", "prefs.viewer.frame", "prefs.viewer.frame.help", () => prefs.appChoices("viewer.frame").map((v) => [v, t(`prefs.choice.viewer.frame.${v}`)])),
    appToggle("viewer.loop", "prefs.viewer.loop", "prefs.viewer.loop.help"),
    appToggle("viewer.sync", "prefs.viewer.sync", "prefs.viewer.sync.help"),
  ];
}

function camera() {
  const detect = h("button", { type: "button", text: t("prefs.camera.detect"), onclick: async () => {
    ui.cameras = await hooks.cameras(true); runSyncs();
  } });
  const device = appSelect("camera.device", "prefs.camera.device", "prefs.camera.device.help", () => {
    const names = (ui.cameras || []).map((d) => d.label).filter(Boolean);
    const current = prefs.app("camera.device");
    if (current && !names.includes(current)) names.push(current);
    return [["", t("prefs.camera.systemDefault")], ...names.map((n) => [n, n])];
  });
  device.querySelector(".setting-control").prepend(detect);
  syncs.push(() => { detect.hidden = !!ui.cameras?.some((d) => d.label); });
  if (!ui.cameras) hooks.cameras(false).then((list) => { ui.cameras = list; runSyncs(); }).catch(() => {});
  return [device, appToggle("camera.audio", "prefs.camera.audio", "prefs.camera.audio.help")];
}

function agents() {
  const claude = codeBlock("prefs.agents.claude", "prefs.agents.claude.help");
  const codex = codeBlock("prefs.agents.codex", "prefs.agents.codex.help");
  const json = codeBlock("prefs.agents.json", "prefs.agents.json.help");
  const cli = codeBlock("prefs.agents.cli", "prefs.agents.cli.help");
  const localNote = note("prefs.agents.checkout");
  syncs.push(async () => {
    const info = await api.mcpInfo();
    claude.pre.textContent = info.claude; codex.pre.textContent = info.codex; json.pre.textContent = info.json;
    localNote.hidden = info.local;
    cli.pre.textContent = (await hooks.benchCommand()) || t("prefs.agents.cliEmpty");
  });
  return [note("prefs.agents.note"), localNote, claude.node, codex.node, json.node, cli.node];
}

function advanced() {
  const cacheOut = h("p", { class: "inline-result", role: "status" });
  const cache = actionRow("prefs.advanced.cache", "prefs.advanced.cache.help", h("button", { type: "button", text: t("prefs.advanced.cacheClear"), onclick: async () => {
    const bytes = await api.clearCache();
    ui.cache = t("prefs.advanced.cacheCleared", { size: (bytes / 1048576).toFixed(1) }); runSyncs();
  } }));
  cache.node.append(cacheOut);
  const out = h("p", { class: "inline-result", role: "status" });
  const transfer = actionRow("prefs.advanced.transfer", "prefs.advanced.transfer.help", [
    h("button", { type: "button", text: t("prefs.advanced.export"), onclick: async () => {
      try { const name = await api.settings.exportAll(); if (name) { ui.notice = t("prefs.advanced.exported", { name }); runSyncs(); } } catch (err) { ui.notice = errorText(err); runSyncs(); }
    } }),
    h("button", { type: "button", text: t("prefs.advanced.import"), onclick: async () => {
      try { const next = await api.settings.importAll(); if (next) { ui.notice = t("prefs.advanced.imported"); prefs.replace(next); } } catch (err) { ui.notice = errorText(err); runSyncs(); }
    } })]);
  transfer.node.append(out);
  const reset = actionRow("prefs.advanced.reset", "prefs.advanced.reset.help", h("button", { type: "button", class: "danger", text: t("prefs.advanced.resetButton"), onclick: async () => {
    const next = await api.settings.resetAll(); if (next) prefs.replace(next);
  } }));
  syncs.push(() => { cacheOut.textContent = ui.cache || ""; out.textContent = ui.notice || ""; });
  return [appToggle("advanced.filterLog", "prefs.advanced.filterLog", "prefs.advanced.filterLog.help"), cache.node, transfer.node, reset.node];
}

function about() {
  const dl = h("dl", { class: "facts" });
  const diag = actionRow("prefs.about.diagnostics", "prefs.about.diagnostics.help", h("button", { type: "button", text: t("prefs.about.copyDiagnostics"), onclick: async (e) => {
    const info = ui.about || await api.aboutInfo();
    const s = prefs.snap;
    const lines = [
      `V2L Studio ${info.app}`, `V2L ${info.v2l || "unknown"} (Python ${info.python || "unknown"})`,
      `Electron ${info.electron}, Chromium ${info.chrome}, Node ${info.node}`, `OS ${info.os}`,
      `Language ${language()}`, `Theme ${prefs.snap.effective["general.theme"]} (${document.documentElement.dataset.theme})`, `Compute mode ${s.effective["compute.mode"]}`,
      `Host set ${s.effective["compute.host"] ? "yes" : "no"}${s.locks["compute.host"] ? " (V2L_HOST)" : ""}`,
      `Local executable found ${s.paths.executableFound ? "yes" : "no"}`,
      `Tuned settings ${prefs.tunedCount()}, default preset ${s.effective["generation.preset"]}`,
    ];
    await api.copy(lines.join("\n"));
    e.target.textContent = t("prefs.copied");
    setTimeout(() => { e.target.textContent = t("prefs.about.copyDiagnostics"); }, 1600);
  } }));
  const links = actionRow("prefs.about.links", null, [
    h("button", { type: "button", text: t("prefs.about.notices"), onclick: () => api.openNotices() }),
    h("button", { type: "button", text: t("prefs.about.repository"), onclick: () => api.openRepository() })]);
  syncs.push(async () => {
    ui.about ||= await api.aboutInfo();
    const a = ui.about;
    dl.replaceChildren(...[["prefs.about.app", a.app], ["prefs.about.v2l", a.v2l || t("prefs.about.unknown")], ["prefs.about.python", a.python || t("prefs.about.unknown")],
      ["prefs.about.electron", a.electron], ["prefs.about.license", t("prefs.about.mit")], ["prefs.about.repo", a.repository]]
      .flatMap(([k, v]) => [h("dt", { text: t(k) }), h("dd", { text: v })]));
  });
  return [h("div", { class: "about-mark" }, h("strong", { text: "V2L" }), h("span", { text: "Studio" })), dl, note("prefs.about.cubism"), links.node, diag.node];
}

const BUILDERS = { general, compute, output, generation, tuning, presets, viewer, camera, agents, advanced, about };

// ---------------------------------------------------------------- shell

function runSyncs() {
  for (const fn of syncs) {
    try { const r = fn(); if (r?.catch) r.catch(() => {}); } catch (err) { console.error(err); }
  }
}

function navButton(id) {
  return h("button", { type: "button", class: "nav-item", "data-section": id, "aria-current": "false",
    onclick: () => { clearSearch(); show(id); } }, h("span", { text: t(`prefs.section.${id}`) }));
}

export function renderSettings() {
  const focusKey = dlg.contains(document.activeElement) ? document.activeElement.closest("[data-key]")?.dataset.key : null;
  const focusTag = document.activeElement?.tagName;
  const pane = dlg.querySelector(".settings-pane");
  const scroll = pane?.scrollTop || 0;
  syncs = [];
  const nav = h("nav", { class: "settings-nav", "aria-label": t("prefs.sections") },
    h("ul", {}, SECTIONS.map((id) => h("li", {}, navButton(id),
      id === "tuning" ? h("ul", { class: "sub" }, TUNING_GROUPS.map((g) => h("li", {}, h("button", { type: "button", class: "nav-sub", "data-group": g,
        onclick: () => { clearSearch(); show("tuning", null, g); } }, h("span", { text: g === "io" ? t("prefs.tuning.io") : t(`section.${g}.label`) }))))) : null))));
  const sections = SECTIONS.map((id) => h("section", { class: "pane", "data-section": id, "aria-labelledby": `pane-${id}-title`, hidden: true },
    paneHead(id), BUILDERS[id]()));
  const empty = h("p", { class: "search-empty", role: "status", hidden: true });
  const body = h("div", { class: "settings-pane", tabindex: "-1" }, sections, empty);
  const search = h("input", { type: "search", class: "settings-search", placeholder: t("prefs.search"), "aria-label": t("prefs.search"), value: query,
    oninput: () => { query = search.value; filter(); },
    onkeydown: (e) => { if (e.key === "Escape" && search.value) { e.preventDefault(); e.stopPropagation(); clearSearch(); } } });
  const close = h("button", { type: "button", class: "icon close", "aria-label": t("prefs.close"), title: t("prefs.close"), onclick: () => dlg.close() });
  close.innerHTML = ICONS.close;
  close.setAttribute("aria-label", t("prefs.close"));
  dlg.replaceChildren(h("div", { class: "settings-frame" },
    h("header", { class: "settings-top" }, h("h2", { id: "settings-title", text: t("prefs.title") }), search, close),
    h("div", { class: "settings-body" }, nav, body)));
  rendered = true;
  runSyncs();
  show(active, null, null, { keepScroll: true });
  filter();
  if (scroll) dlg.querySelector(".settings-pane").scrollTop = scroll;
  if (focusKey) {
    const target = dlg.querySelector(`[data-key="${focusKey}"] ${focusTag === "SELECT" ? "select" : "input, select, button"}`);
    target?.focus();
  }
}

function clearSearch() {
  query = "";
  const s = dlg.querySelector(".settings-search");
  if (s) s.value = "";
  filter();
}

function filter() {
  if (!rendered) return;
  const q = query.trim().toLowerCase();
  const tokens = q.split(/\s+/).filter(Boolean);
  dlg.dataset.searching = tokens.length ? "true" : "false";
  const panes = [...dlg.querySelectorAll(".pane")];
  let total = 0;
  for (const pane of panes) {
    let hits = 0;
    for (const r of pane.querySelectorAll(".setting")) {
      const ok = !tokens.length || tokens.every((tok) => (r.dataset.search || "").includes(tok));
      r.classList.toggle("no-match", !ok);
      if (ok) hits++;
    }
    for (const g of pane.querySelectorAll(".group")) g.classList.toggle("no-match", !!tokens.length && !g.querySelector(".setting:not(.no-match)"));
    if (tokens.length) pane.hidden = !hits;
    else pane.hidden = pane.dataset.section !== active;
    total += hits;
    dlg.querySelector(`.nav-item[data-section="${pane.dataset.section}"]`)?.classList.toggle("dim", !!tokens.length && !hits);
  }
  const empty = dlg.querySelector(".search-empty");
  empty.hidden = !tokens.length || total > 0;
  empty.textContent = t("prefs.noResults", { query: query.trim() });
}

function show(id, key, group, { keepScroll } = {}) {
  active = SECTIONS.includes(id) ? id : "general";
  for (const b of dlg.querySelectorAll(".nav-item")) b.setAttribute("aria-current", String(b.dataset.section === active));
  filter();
  const pane = dlg.querySelector(".settings-pane");
  if (!keepScroll) pane.scrollTop = 0;
  let target = null;
  if (group) target = dlg.querySelector(`.pane[data-section="tuning"] [data-group="${group}"]`);
  if (key) {
    target = dlg.querySelector(`.pane[data-section="${active}"] [data-key="${key}"]`);
    if (target?.classList.contains("is-advanced") && !prefs.app("tuning.showAdvanced")) prefs.setApp("tuning.showAdvanced", true);
  }
  if (target) {
    target.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    if (key) {
      target.classList.remove("flash"); void target.offsetWidth; target.classList.add("flash");
      target.querySelector("input:not([type=range]), select, button")?.focus({ preventScroll: true });
    }
  }
}

// Deep link: openSettings("compute"), openSettings("tuning", "filtering.cutoff_scale").
export function openSettings(section = active, key = null) {
  if (!rendered) renderSettings();
  if (!dlg.open) dlg.showModal();
  let id = section, group = null;
  if (section === "tuning" && key && !key.includes(".")) { group = key; key = null; }
  if (key && !dlg.querySelector(`.pane[data-section="${id}"] [data-key="${key}"]`)) {
    id = dlg.querySelector(`.pane [data-key="${key}"]`)?.closest(".pane")?.dataset.section || id;
  }
  clearSearch();
  show(id, key, group);
  if (!key) dlg.querySelector(`.nav-item[data-section="${active}"]`)?.focus();
  // Values that come from outside (agents, about, camera) are refreshed on open.
  runSyncs();
}

export function isSettingsOpen() { return dlg.open; }

// Focus stays inside the dialog; arrow keys move through the section list.
dlg.addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    const items = [...dlg.querySelectorAll("button, input, select, textarea, pre[tabindex], [tabindex='0'], summary")]
      .filter((n) => !n.disabled && n.offsetParent !== null && n.tabIndex >= 0);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && e.target.closest?.(".settings-nav")) {
    e.preventDefault();
    const items = [...dlg.querySelectorAll(".settings-nav button")].filter((n) => n.offsetParent !== null);
    const i = items.indexOf(e.target);
    const next = items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
    next.focus();
  }
});
dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // backdrop
dlg.addEventListener("close", () => { ui.notice = null; });

export function initSettings(options) {
  hooks = { ...hooks, ...options };
  prefs.on((changed) => {
    if (!rendered) return;
    const keys = [...(changed.app || [])];
    if (changed.all || keys.includes("general.language")) { renderSettings(); return; }
    runSyncs();
  });
  api.compute.onTaskLog(({ id, line }) => {
    if (id !== ui.task.id) return;
    const mode = ui.task.kind === "setup-remote" ? "remote" : "local";
    ui.task.log[mode].push(line);
    if (ui.task.log[mode].length > 2000) ui.task.log[mode].shift();
    runSyncs();
  });
  api.compute.onTaskDone(({ id, kind, code }) => {
    if (id !== ui.task.id) return;
    const mode = kind === "setup-remote" ? "remote" : "local";
    ui.task.kind = null; ui.task.id = null; ui.task.code[mode] = code;
    api.settings.get().then((s) => prefs.replace(s, { app: [] })).then(() => runCheck(mode));
  });
}
