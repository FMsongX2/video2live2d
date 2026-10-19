// V2L Studio: Live2D model viewer + video-to-motion test bench.
//
// The renderer is served from a loopback HTTP server owned by this process, so pixi can fetch
// model files and the <video> element gets HTTP range requests (seeking) without custom
// protocols. Generation runs on a compute host through bin/v2l-remote, or on this computer
// through a local `v2l` executable; nothing heavy runs in Studio except drawing the model.
"use strict";

const { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, nativeImage, nativeTheme, net, shell, systemPreferences } = require("electron");
const crypto = require("node:crypto");
const fs = require("node:fs");
const fsp = fs.promises;
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");                  // the v2l project
const RENDERER = path.join(__dirname, "renderer");
const LOCALES = path.join(RENDERER, "locales");                // UI strings, shared with the renderer
const WEB_ASSETS = path.join(ROOT, "src", "v2l", "web", "assets.json");  // pinned pixi.js + pixi-live2d-display
const REMOTE = path.join(ROOT, "bin", "v2l-remote");
const MCP_SCRIPT = path.join(ROOT, "bin", "v2l-mcp");
const VENV_V2L = path.join(ROOT, ".venv", "bin", "v2l");
const NOTICES = path.join(ROOT, "THIRD_PARTY_NOTICES.md");
const REPOSITORY = "https://github.com/FMsongX2/video2live2d";
const CORE_URL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js";
const PRODUCT = "V2L Studio";
// Window background before the page paints; equals --bg-app of each theme in renderer/style.css.
const THEME_BACKGROUND = { light: "#ffffff", dark: "#111111" };
const TOKEN = crypto.randomBytes(12).toString("hex");         // only our window knows the URLs
// GUI apps on macOS start with a minimal PATH; v2l-remote needs python3, rsync and ssh.
const ENV = { ...process.env, PATH: ["/opt/homebrew/bin", "/usr/local/bin", process.env.PATH || "/usr/bin:/bin"].join(":") };
const PY_ENV = { ...ENV, PYTHONPATH: path.join(ROOT, "src") + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : "") };
const NAME_RE = /^[A-Za-z0-9_-]{1,80}$/;
const PRESET_RE = /^[A-Za-z0-9_-]{1,64}$/;
// Source files by extension, as `v2l run` reads them: a video (tracking) or an audio file (Audio2Face-3D).
const VIDEO_EXT = ["mp4", "mov", "m4v", "webm", "mkv"];
const AUDIO_EXT = ["wav", "mp3", "m4a", "aac", "flac", "ogg", "opus"];

// The product name has a space; keep the data folder where earlier versions put it.
if (!app.commandLine.hasSwitch("user-data-dir")) app.setPath("userData", path.join(app.getPath("appData"), "v2l-studio"));

let win = null;
let port = 0;
let job = null;            // the running generation
let task = null;           // the running setup task
const roots = new Map();   // id -> absolute directory served under /r/<id>/
const files = new Map();   // id -> absolute file served under /f/<id>

// ---------------------------------------------------------------- language

// Dialogs and the app menu follow the renderer's language. Before the window exists, the saved
// choice or the system locale applies.
const LANGUAGES = ["en", "ko", "ja", "zh-CN"];
const STRINGS = Object.fromEntries(LANGUAGES.map((l) => [l, JSON.parse(fs.readFileSync(path.join(LOCALES, `${l}.json`), "utf8"))]));
let lang = "en";

function matchLanguage(tag) {
  const t = String(tag || "").toLowerCase();
  if (t.startsWith("ko")) return "ko";
  if (t.startsWith("ja")) return "ja";
  if (t.startsWith("zh")) return "zh-CN";
  return "en";
}

function t(key, params = {}) {
  const text = STRINGS[lang][key] ?? STRINGS.en[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (whole, name) => (params[name] == null ? whole : String(params[name])));
}

function resolveLanguage() {
  const chosen = appValue("general.language");
  return LANGUAGES.includes(chosen) ? chosen : matchLanguage(app.getLocale());
}

// ---------------------------------------------------------------- theme

// nativeTheme drives prefers-color-scheme in the renderer, native menus, dialogs and scrollbars.
function applyTheme() {
  nativeTheme.themeSource = appValue("general.theme");
  if (win && !win.isDestroyed()) { win.setBackgroundColor(themeBackground()); send("theme:changed", themeName()); }
}
const themeName = () => (nativeTheme.shouldUseDarkColors ? "dark" : "light");
const themeBackground = () => THEME_BACKGROUND[themeName()];

// ---------------------------------------------------------------- processes

// Runs a command to completion and returns its exit code and output.
function capture(cmd, args, { env = ENV, cwd = ROOT, input } = {}) {
  return new Promise((resolve) => {
    let stdout = "", stderr = "";
    let p;
    try { p = spawn(cmd, args, { env, cwd }); } catch (err) { resolve({ code: -1, stdout, stderr: err.message }); return; }
    p.stdout.on("data", (b) => { stdout += b; });
    p.stderr.on("data", (b) => { stderr += b; });
    p.on("error", (err) => resolve({ code: -1, stdout, stderr: err.message }));
    p.on("close", (code) => resolve({ code, stdout, stderr }));
    if (input != null) p.stdin.end(input);
  });
}

// `python3 -m v2l.config ...` from this checkout (standard library only, no install needed).
async function configTool(args) {
  const r = await capture("python3", ["-m", "v2l.config", ...args], { env: PY_ENV });
  if (r.code !== 0) throw new Error(r.stderr.trim().replace(/^v2l config: /, "") || t("main.pythonFailed", { code: r.code }));
  return JSON.parse(r.stdout);
}

function lastJson(text) {
  const trimmed = text.trim();
  try { return JSON.parse(trimmed); } catch { /* fall through to the last JSON line */ }
  for (const line of trimmed.split(/\r?\n/).reverse()) {
    if (!line.startsWith("{")) continue;
    try { return JSON.parse(line); } catch { /* keep looking */ }
  }
  return null;
}

// ---------------------------------------------------------------- settings

// userData/settings.json: {"version": 1, "app": {key: value}, "config": {section: {name: value}}}.
// Only values that differ from the defaults are stored. "config" is a sparse v2l configuration
// (src/v2l/config.py) holding the generation defaults and tuning.
// Precedence: schema default < settings.json < environment variable < per-run bench value.
const SETTINGS_VERSION = 1;
const APP_DEFAULTS = {
  "general.language": "system",
  "general.theme": "system",
  "general.modelDir": "",
  "general.openLastModel": true,
  "general.lastModel": "",
  "compute.mode": "remote",
  "compute.host": "",
  "compute.remoteDir": "v2l",
  "compute.executable": "",
  "output.dir": "",
  "output.onExists": "refuse",
  "output.autoInstall": false,
  "generation.preset": "default",
  "tuning.showAdvanced": false,
  "viewer.physics": true,
  "viewer.tex4k": true,
  "viewer.frame": "upper",
  "viewer.loop": true,
  "viewer.sync": true,
  "camera.device": "",
  "camera.audio": false,
  "advanced.filterLog": true,
};
const APP_CHOICES = {
  "general.language": ["system", ...LANGUAGES],
  "general.theme": ["system", "light", "dark"],
  "compute.mode": ["remote", "local"],
  "output.onExists": ["refuse", "suffix"],
  "viewer.frame": ["face", "upper", "full"],
};
const ENV_LOCKS = { "general.modelDir": "V2L_MODEL", "compute.host": "V2L_HOST", "compute.remoteDir": "V2L_DIR", "output.dir": "V2L_OUT" };
// Studio plays the motion itself, so the host does not render a preview video unless asked.
const STUDIO_CONFIG = { preview: { enabled: false } };

let schema = null;          // python -m v2l.config schema
let schemaDefaults = null;  // {section: {name: default}}
let fieldIndex = new Map(); // "section.name" -> field
let presetList = [];
let store = { app: {}, config: {} };
let writing = Promise.resolve();

const settingsFile = () => path.join(app.getPath("userData"), "settings.json");
const legacyFile = () => path.join(app.getPath("userData"), "studio-settings.json");
const clone = (v) => (v == null ? v : JSON.parse(JSON.stringify(v)));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function envLock(key) {
  const name = ENV_LOCKS[key];
  return name && process.env[name] ? name : null;
}

function appValue(key) {
  const lock = envLock(key);
  if (lock) return process.env[lock];
  return key in store.app ? store.app[key] : APP_DEFAULTS[key];
}

function outDir() {
  return path.resolve(String(appValue("output.dir") || path.join(os.homedir(), "Downloads", "v2l_out")).replace(/^~(?=$|\/)/, os.homedir()));
}

function executable() {
  const chosen = appValue("compute.executable");
  if (chosen) return chosen.replace(/^~(?=$|\/)/, os.homedir());
  return fs.existsSync(VENV_V2L) ? VENV_V2L : "v2l";
}

function whichSync(cmd) {
  if (cmd.includes("/")) return fs.existsSync(cmd) ? cmd : null;
  for (const dir of ENV.PATH.split(path.delimiter)) {
    const p = path.join(dir, cmd);
    try { fs.accessSync(p, fs.constants.X_OK); return p; } catch { /* next */ }
  }
  return null;
}

function checkAppValue(key, value) {
  if (!(key in APP_DEFAULTS)) throw new Error(t("main.unknownSetting", { key }));
  const def = APP_DEFAULTS[key];
  if (typeof value !== typeof def) throw new Error(t("main.badSetting", { key }));
  if (APP_CHOICES[key] && !APP_CHOICES[key].includes(value)) throw new Error(t("main.badSetting", { key }));
  if (key === "compute.host" && value && !/^[A-Za-z0-9._@:\[\]-]+$/.test(value)) throw new Error(t("main.badHost"));
  if (key === "compute.remoteDir" && value && /[\s'"`$;&|]/.test(value)) throw new Error(t("main.badSetting", { key }));
  if (key === "generation.preset" && !PRESET_RE.test(value)) throw new Error(t("main.badSetting", { key }));
  return value;
}

// Studio's default for one config field (the schema default, unless Studio differs).
function studioDefault(sid, name) {
  if (STUDIO_CONFIG[sid] && name in STUDIO_CONFIG[sid]) return STUDIO_CONFIG[sid][name];
  return schemaDefaults[sid][name];
}

// Client-side check of one config value against the schema; Python validates every run.
function checkConfigValue(key, value) {
  const f = fieldIndex.get(key);
  if (!f) throw new Error(t("main.unknownSetting", { key }));
  if (value === null) { if (f.nullable) return null; throw new Error(t("main.badSetting", { key })); }
  const num = (x) => typeof x === "number" && Number.isFinite(x) && (f.min == null || x >= f.min) && (f.max == null || x <= f.max);
  const ok = {
    boolean: () => typeof value === "boolean",
    integer: () => Number.isInteger(value) && num(value),
    number: () => num(value),
    string: () => typeof value === "string" && value.length > 0,
    enum: () => f.choices.includes(value),
    range: () => Array.isArray(value) && value.length === 2 && value.every(num) && value[0] <= value[1],
    pair: () => Array.isArray(value) && value.length === 2 && value.every(num),
    window: () => Array.isArray(value) && value.length === 2 && value.every(num) && value[0] < value[1],
  }[f.kind];
  if (!ok || !ok()) throw new Error(t("main.badSetting", { key }));
  return value;
}

// Stored when the value differs from Studio's default, and also when it equals that default but
// the default preset sets something else (so a choice like "cute over a faithful preset" sticks).
function setConfigIn(config, key, value, preset = appValue("generation.preset")) {
  const [sid, name] = key.split(".");
  const fromPreset = presetSettings(preset)[sid]?.[name];
  const redundant = same(value, studioDefault(sid, name)) && (fromPreset === undefined || same(fromPreset, value));
  if (value === undefined || redundant) {
    if (config[sid]) { delete config[sid][name]; if (!Object.keys(config[sid]).length) delete config[sid]; }
  } else {
    (config[sid] ||= {})[name] = value;
  }
}

// A stored or imported settings object, cleaned to known keys with valid values.
function sanitizeStore(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("not an object");
  const out = { app: {}, config: {} };
  for (const [key, value] of Object.entries(data.app || {})) {
    try { if (!same(checkAppValue(key, value), APP_DEFAULTS[key])) out.app[key] = value; } catch { /* dropped */ }
  }
  // Before the schema is loaded (startup), the configuration part is kept as read and checked later.
  if (!schema) { out.config = data.config && typeof data.config === "object" ? clone(data.config) : {}; return out; }
  for (const [sid, values] of Object.entries(data.config || {})) {
    if (!values || typeof values !== "object") continue;
    for (const [name, value] of Object.entries(values)) {
      try { setConfigIn(out.config, `${sid}.${name}`, checkConfigValue(`${sid}.${name}`, value)); } catch { /* dropped */ }
    }
  }
  return out;
}

function loadStore() {
  const file = settingsFile();
  if (fs.existsSync(file)) {
    try {
      const data = JSON.parse(fs.readFileSync(file, "utf8"));
      if (data.version !== SETTINGS_VERSION) throw new Error(`unsupported version ${data.version}`);
      store = sanitizeStore(data);
    } catch (err) {
      // Keep the unreadable file for inspection and continue with defaults.
      const aside = `${file}.unreadable-${Date.now()}`;
      try { fs.renameSync(file, aside); } catch { /* best effort */ }
      console.error(`settings.json unreadable (${err.message}); moved to ${aside}`);
      store = { app: {}, config: {} };
    }
  }
  // One-time migration of the language file used by earlier versions.
  if (fs.existsSync(legacyFile())) {
    try {
      const old = JSON.parse(fs.readFileSync(legacyFile(), "utf8")).lang;
      if (LANGUAGES.includes(old) && !("general.language" in store.app)) store.app["general.language"] = old;
    } catch { /* ignore a broken legacy file */ }
    saveStore().then(() => fsp.rm(legacyFile(), { force: true })).catch(() => {});
  }
}

// Atomic write: temp file + rename, serialized so concurrent changes cannot interleave.
function saveStore() {
  const body = JSON.stringify({ version: SETTINGS_VERSION, app: store.app, config: store.config }, null, 1) + "\n";
  writing = writing.catch(() => {}).then(async () => {
    const file = settingsFile();
    await fsp.mkdir(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    await fsp.writeFile(tmp, body);
    await fsp.rename(tmp, file);
  });
  return writing;
}

function presetsDir() {
  return path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "v2l", "presets");
}

async function refreshPresets() {
  presetList = await configTool(["presets"]);
  return presetList;
}

function presetSettings(name) {
  return presetList.find((p) => p.name === name)?.settings || {};
}

function snapshot() {
  const exe = executable();
  return {
    schema, appDefaults: APP_DEFAULTS, appChoices: APP_CHOICES, studioConfig: STUDIO_CONFIG,
    app: clone(store.app), config: clone(store.config),
    effective: Object.fromEntries(Object.keys(APP_DEFAULTS).map((k) => [k, appValue(k)])),
    locks: Object.fromEntries(Object.keys(ENV_LOCKS).filter(envLock).map((k) => [k, envLock(k)])),
    presets: presetList, language: lang,
    paths: {
      outDir: outDir(), executable: exe, executableFound: !!whichSync(exe), venvExecutable: VENV_V2L,
      presetsDir: presetsDir(), repo: ROOT,
    },
  };
}

// Effective configuration for one run: schema defaults < Studio defaults < preset < settings
// (generation defaults + tuning) < per-run values; returned sparse against the schema defaults.
function nestedSet(obj, key, value) {
  const [sid, name] = key.split(".");
  (obj[sid] ||= {})[name] = value;
}

function effectiveConfig(preset, perRun = {}) {
  const full = clone(schemaDefaults);
  const apply = (sparse) => { for (const [sid, v] of Object.entries(sparse || {})) if (full[sid]) Object.assign(full[sid], clone(v)); };
  apply(STUDIO_CONFIG);
  apply(presetSettings(preset));
  apply(store.config);
  for (const [key, value] of Object.entries(perRun)) nestedSet(full, key, checkConfigValue(key, value));
  return full;
}

function sparseAgainst(full, baseline) {
  const out = {};
  for (const [sid, values] of Object.entries(full)) {
    for (const [name, value] of Object.entries(values)) {
      if (!same(value, baseline[sid]?.[name])) (out[sid] ||= {})[name] = value;
    }
  }
  return out;
}

function presetBaseline(preset) {
  const base = clone(schemaDefaults);
  for (const [sid, v] of Object.entries(presetSettings(preset))) if (base[sid]) Object.assign(base[sid], clone(v));
  return base;
}

async function validateFile(file) {
  const r = await capture("python3", ["-m", "v2l.config", "validate", file], { env: PY_ENV });
  if (r.code !== 0) throw new Error(r.stderr.trim().replace(/^v2l config: /, "") || t("main.pythonFailed", { code: r.code }));
}

async function writeJsonAtomic(file, data) {
  await fsp.mkdir(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.part`;
  await fsp.writeFile(tmp, JSON.stringify(data, null, 1) + "\n");
  return tmp;
}

// Generation defaults + tuning as a preset body (no per-run trim, no Studio output choices).
function tuningAsPreset() {
  const full = effectiveConfig(appValue("generation.preset"));
  const sparse = sparseAgainst(full, schemaDefaults);
  delete sparse.preview;
  if (sparse.input) { delete sparse.input.start; delete sparse.input.end; if (!Object.keys(sparse.input).length) delete sparse.input; }
  return sparse;
}

function shellQuote(s) {
  const text = String(s);
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(text) ? text : `'${text.replace(/'/g, "'\\''")}'`;
}

function cliValue(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function cliCommand(o) {
  const preset = o.preset || "default";
  const sets = sparseAgainst(effectiveConfig(preset, o.perRun), presetBaseline(preset));
  const args = ["v2l", "run", o.input || "INPUT", "--model", o.modelDir || "MODEL_DIR", "--name", o.name || "NAME",
                "--out", path.join(outDir(), o.name || "NAME")];
  if (o.audio) args.push("--audio", o.audio);
  if (preset !== "default") args.push("--preset", preset);
  const mapping = o.modelDir ? mappingOverrides(o.modelDir) : null;
  if (mapping) args.push("--mapping", mapping);
  for (const [sid, values] of Object.entries(sets)) {
    for (const [name, value] of Object.entries(values)) args.push("--set", `${sid}.${name}=${cliValue(value)}`);
  }
  return args.map(shellQuote).join(" ");
}

function mcpInfo() {
  const exe = whichSync(executable());
  const command = exe ? path.resolve(exe) : MCP_SCRIPT;
  const args = exe ? ["mcp"] : [];
  const env = {};
  if (appValue("compute.mode") === "remote" && appValue("compute.host")) {
    env.V2L_HOST = appValue("compute.host");
    if (appValue("compute.remoteDir") !== APP_DEFAULTS["compute.remoteDir"]) env.V2L_DIR = appValue("compute.remoteDir");
  }
  env.V2L_OUT = outDir();
  const envFlags = (flag) => Object.entries(env).flatMap(([k, v]) => [flag, `${k}=${v}`]);
  const tail = ["--", command, ...args].map(shellQuote).join(" ");
  return {
    claude: ["claude", "mcp", "add", "v2l", ...envFlags("-e")].map(shellQuote).join(" ") + " " + tail,
    codex: ["codex", "mcp", "add", "v2l", ...envFlags("--env")].map(shellQuote).join(" ") + " " + tail,
    json: JSON.stringify({ mcpServers: { v2l: { command, args, env } } }, null, 2),
    local: !!exe,
  };
}

// ---------------------------------------------------------------- file server

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".moc3": "application/octet-stream",
  ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
  ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
  ".ogg": "audio/ogg", ".opus": "audio/ogg",
};

function serveFile(req, res, file) {
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) { res.writeHead(404).end(); return; }
    const type = MIME[path.extname(file).toLowerCase()] || "application/octet-stream";
    const headers = { "Content-Type": type, "Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store" };
    const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || "");
    if (range) {
      const start = range[1] ? Number(range[1]) : st.size - Number(range[2]);
      const end = range[1] && range[2] ? Math.min(Number(range[2]), st.size - 1) : st.size - 1;
      if (start > end || start < 0) { res.writeHead(416, { "Content-Range": `bytes */${st.size}` }).end(); return; }
      res.writeHead(206, { ...headers, "Content-Range": `bytes ${start}-${end}/${st.size}`, "Content-Length": end - start + 1 });
      fs.createReadStream(file, { start, end }).pipe(res);
    } else {
      res.writeHead(200, { ...headers, "Content-Length": st.size });
      fs.createReadStream(file).pipe(res);
    }
  });
}

function inside(base, rel) {
  const p = path.resolve(base, rel);
  return p === base || p.startsWith(base + path.sep) ? p : null;
}

function startServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    const parts = url.pathname.split("/").map(decodeURIComponent);  // ["", token, kind, ...]
    if (parts[1] !== TOKEN) { res.writeHead(403).end(); return; }
    const [kind, a, ...rest] = parts.slice(2);
    let file = null;
    if (kind === "app") file = inside(RENDERER, [a, ...rest].join("/"));
    else if (kind === "lib") file = inside(path.join(app.getPath("userData"), "lib"), [a, ...rest].join("/"));
    else if (kind === "core.js") file = path.join(app.getPath("userData"), "live2dcubismcore.min.js");
    else if (kind === "r" && roots.has(a)) file = inside(roots.get(a), rest.join("/"));
    else if (kind === "f" && files.has(a)) file = files.get(a);
    if (!file) { res.writeHead(404).end(); return; }
    serveFile(req, res, file);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
}

const base = () => `http://127.0.0.1:${port}/${TOKEN}`;

function rootUrl(dir) {
  const id = crypto.createHash("sha1").update(dir).digest("hex").slice(0, 12);
  roots.set(id, dir);
  return `${base()}/r/${id}`;
}

function fileUrl(file) {
  const id = crypto.createHash("sha1").update(file).digest("hex").slice(0, 12);
  files.set(id, file);
  return `${base()}/f/${id}`;
}

// ---------------------------------------------------------------- assets

// Third-party runtime files are fetched on first use, not shipped with v2l.
async function fetchTo(url, dest, sha256) {
  const res = await net.fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (v2l-studio)" } });
  if (!res.ok) throw new Error(`${path.basename(dest)} download failed: HTTP ${res.status}`);
  const body = Buffer.from(await res.arrayBuffer());
  if (sha256 && crypto.createHash("sha256").update(body).digest("hex") !== sha256) {
    throw new Error(`${path.basename(dest)} download does not match its pinned SHA-256`);
  }
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  await fsp.writeFile(dest + ".part", body);
  await fsp.rename(dest + ".part", dest);
}

async function ensureAssets() {
  // Live2D Cubism Core (Live2D Proprietary Software License) comes from Live2D's site.
  const core = path.join(app.getPath("userData"), "live2dcubismcore.min.js");
  if (!fs.existsSync(core)) await fetchTo(CORE_URL, core, null);
  // pixi.js (MIT) and pixi-live2d-display (MIT, bundles the Cubism Framework under the Live2D
  // Open Software License), pinned by version and SHA-256 in src/v2l/web/assets.json.
  const manifest = JSON.parse(await fsp.readFile(WEB_ASSETS, "utf8"));
  for (const [name, { url, sha256 }] of Object.entries(manifest)) {
    const dest = path.join(app.getPath("userData"), "lib", name);
    const ok = fs.existsSync(dest) && crypto.createHash("sha256").update(await fsp.readFile(dest)).digest("hex") === sha256;
    if (!ok) await fetchTo(url, dest, sha256);
  }
}

function findSettings(dir) {
  const hits = fs.readdirSync(dir).filter((f) => f.endsWith(".model3.json") && !f.endsWith("items_pinned_to_model.json"));
  if (hits.length !== 1) throw new Error(t("main.settingsCount", { dir, count: hits.length }));
  return hits[0];
}

const modelCache = () => path.join(app.getPath("userData"), "model-cache");

// A viewing copy of the model with textures capped at maxTex pixels: five 8K textures need
// ~1.3 GB of GPU memory at full size and ~0.3 GB at 4K. The original folder is untouched.
async function viewCopy(dir, maxTex) {
  if (!maxTex) return dir;
  const settingsFile = findSettings(dir);
  const settings = JSON.parse(await fsp.readFile(path.join(dir, settingsFile), "utf8"));
  const textures = (settings.FileReferences && settings.FileReferences.Textures) || [];
  const stamp = crypto.createHash("sha1");
  stamp.update(`${dir}|${maxTex}`);
  for (const t of textures) stamp.update(`${t}:${(await fsp.stat(path.join(dir, t))).mtimeMs}`);
  const dest = path.join(modelCache(), `${path.basename(dir)}-${stamp.digest("hex").slice(0, 10)}`);
  if (fs.existsSync(path.join(dest, ".complete"))) return dest;
  await fsp.rm(dest, { recursive: true, force: true });
  await fsp.cp(dir, dest, { recursive: true, filter: (src) => !src.endsWith(".motion3.json") });
  for (const t of textures) {
    const img = nativeImage.createFromPath(path.join(dir, t));
    const { width, height } = img.getSize();
    if (Math.max(width, height) <= maxTex) continue;
    const scale = maxTex / Math.max(width, height);
    const small = img.resize({ width: Math.round(width * scale), height: Math.round(height * scale), quality: "best" });
    await fsp.writeFile(path.join(dest, t), small.toPNG());
  }
  await fsp.writeFile(path.join(dest, ".complete"), "");
  return dest;
}

// Motions in the model folder and its subfolders (models often keep them in motions/), two levels deep.
async function listMotions(dir) {
  const found = [];
  const walk = async (rel, depth) => {
    let entries = [];
    try { entries = await fsp.readdir(path.join(dir, rel), { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (e.name.startsWith(".")) continue;
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory() && depth < 2) await walk(r, depth + 1);
      else if (e.isFile() && e.name.endsWith(".motion3.json")) found.push(r);
    }
  };
  await walk("", 0);
  found.sort((a, b) => (a.includes("/") - b.includes("/")) || a.localeCompare(b));
  const url = rootUrl(dir);
  return found.map((r) => ({ name: path.basename(r).replace(MOTION_RE, ""), label: r.replace(MOTION_RE, ""), file: path.join(dir, r),
                             url: `${url}/${r.split("/").map(encodeURIComponent).join("/")}` }));
}

function hasModel(dir) {
  try { findSettings(dir); return true; } catch { return false; }
}

function startupModel() {
  const env = process.env.V2L_MODEL;
  if (env) return fs.existsSync(env) ? env : null;
  const last = appValue("general.lastModel");
  if (appValue("general.openLastModel") && last && hasModel(last)) return last;
  const folder = appValue("general.modelDir");
  return folder && hasModel(folder) ? folder : null;
}

async function dirSize(dir) {
  let total = 0;
  let entries = [];
  try { entries = await fsp.readdir(dir, { withFileTypes: true }); } catch { return 0; }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) total += await dirSize(p);
    else { try { total += (await fsp.stat(p)).size; } catch { /* vanished */ } }
  }
  return total;
}

async function confirm(message, detail, confirmLabel, type = "warning") {
  const { response } = await dialog.showMessageBox(win, {
    type, title: PRODUCT, message, detail, buttons: [t("main.cancel"), confirmLabel], defaultId: 0, cancelId: 0,
  });
  return response === 1;
}

const send = (ch, v) => win && !win.isDestroyed() && win.webContents.send(ch, v);

// ---------------------------------------------------------------- IPC: settings

ipcMain.handle("init", async () => ({ base: base(), model: startupModel(), settings: snapshot() }));

ipcMain.handle("settings:get", () => snapshot());

// patch: {app: {key: value | null}, config: {"section.name": value | null}}; null resets to the default.
ipcMain.handle("settings:set", async (_e, patch = {}) => {
  const nextApp = { ...store.app };
  for (const [key, value] of Object.entries(patch.app || {})) {
    if (ENV_LOCKS[key] && envLock(key)) throw new Error(t("main.lockedSetting", { env: envLock(key) }));
    if (value === null || same(checkAppValue(key, value), APP_DEFAULTS[key])) delete nextApp[key];
    else nextApp[key] = value;
  }
  const nextConfig = clone(store.config);
  for (const [key, value] of Object.entries(patch.config || {})) {
    const f = fieldIndex.get(key);
    if (!f) throw new Error(t("main.unknownSetting", { key }));
    setConfigIn(nextConfig, key, value === null && !f.nullable ? undefined : checkConfigValue(key, value),
                nextApp["generation.preset"] ?? APP_DEFAULTS["generation.preset"]);
  }
  store = { app: nextApp, config: nextConfig };
  await saveStore();
  if (patch.app && "general.language" in patch.app) { lang = resolveLanguage(); buildMenu(); }
  if (patch.app && "general.theme" in patch.app) applyTheme();
  return snapshot();
});

ipcMain.handle("settings:export", async () => {
  const r = await dialog.showSaveDialog(win, { title: t("main.exportSettingsTitle"), defaultPath: "v2l-studio-settings.json",
                                               filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled || !r.filePath) return null;
  await fsp.writeFile(r.filePath, JSON.stringify({ version: SETTINGS_VERSION, app: store.app, config: store.config }, null, 1) + "\n");
  return path.basename(r.filePath);
});

ipcMain.handle("settings:import", async () => {
  const r = await dialog.showOpenDialog(win, { title: t("main.importSettingsTitle"), properties: ["openFile"],
                                               filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled) return null;
  let data;
  try { data = JSON.parse(await fsp.readFile(r.filePaths[0], "utf8")); } catch (err) { throw new Error(t("main.notJson", { error: err.message })); }
  if (data?.version !== SETTINGS_VERSION || typeof data.app !== "object" || typeof data.config !== "object") throw new Error(t("main.notSettings"));
  // The configuration part must pass the same validation as a run.
  const tmp = path.join(app.getPath("temp"), `v2l-import-${process.pid}.json`);
  await fsp.writeFile(tmp, JSON.stringify({ version: 1, ...data.config }));
  try { await validateFile(tmp); } finally { await fsp.rm(tmp, { force: true }); }
  if (!await confirm(t("main.importSettingsMessage"), t("main.importSettingsDetail"), t("main.replace"))) return null;
  store = sanitizeStore(data);
  await saveStore();
  lang = resolveLanguage(); buildMenu(); applyTheme();
  return snapshot();
});

ipcMain.handle("settings:reset", async () => {
  if (!await confirm(t("main.resetAllMessage"), t("main.resetAllDetail"), t("main.resetAll"))) return null;
  store = { app: {}, config: {} };
  await saveStore();
  lang = resolveLanguage(); buildMenu(); applyTheme();
  return snapshot();
});

ipcMain.handle("settings:chooseFolder", async (_e, { key }) => {
  const current = key === "output.dir" ? outDir() : appValue(key) || os.homedir();
  const r = await dialog.showOpenDialog(win, { title: t(key === "output.dir" ? "main.outputDialogTitle" : "main.modelDialogTitle"),
                                               properties: ["openDirectory", "createDirectory"], defaultPath: current });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("settings:chooseFile", async (_e, { kind }) => {
  const opts = kind === "json"
    ? { title: t("main.bridgeDialogTitle"), filters: [{ name: "JSON", extensions: ["json"] }] }
    : { title: t("main.executableDialogTitle") };
  const r = await dialog.showOpenDialog(win, { ...opts, properties: ["openFile"], defaultPath: ROOT });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("cli:command", (_e, o) => cliCommand(o || {}));
ipcMain.handle("mcp:info", () => mcpInfo());

ipcMain.handle("clipboard:write", (_e, text) => { clipboard.writeText(String(text)); return true; });

ipcMain.handle("cache:clear", async () => {
  const bytes = await dirSize(modelCache());
  await fsp.rm(modelCache(), { recursive: true, force: true });
  return bytes;
});

ipcMain.handle("about:info", async () => {
  const r = await capture(REMOTE, ["version", "--json"], { env: ENV });
  const v2l = r.code === 0 ? lastJson(r.stdout) : null;
  return {
    app: app.getVersion(), electron: process.versions.electron, chrome: process.versions.chrome, node: process.versions.node,
    os: `${os.type()} ${os.release()} ${os.arch()}`, v2l: v2l?.version || null, python: v2l?.python || null,
    repository: REPOSITORY, license: "MIT",
  };
});

ipcMain.handle("about:notices", () => shell.openPath(NOTICES));
ipcMain.handle("about:repository", () => shell.openExternal(REPOSITORY));
ipcMain.handle("open:folder", async (_e, which) => {
  const dir = which === "presets" ? presetsDir() : outDir();
  await fsp.mkdir(dir, { recursive: true });
  return shell.openPath(dir);
});

// ---------------------------------------------------------------- IPC: presets

ipcMain.handle("presets:list", async () => { await refreshPresets(); return snapshot(); });

ipcMain.handle("presets:save", async (_e, { name, title, description }) => {
  if (!PRESET_RE.test(name || "")) throw new Error(t("main.badPresetName"));
  const dest = path.join(presetsDir(), `${name}.json`);
  if (fs.existsSync(dest) && !await confirm(t("main.presetExists", { name }), "", t("main.overwrite"))) return null;
  const meta = { name: String(title || name).slice(0, 80) };
  if (description) meta.description = String(description).slice(0, 300);
  const tmp = await writeJsonAtomic(dest, { version: 1, meta, ...tuningAsPreset() });
  try { await validateFile(tmp); await fsp.rename(tmp, dest); } finally { await fsp.rm(tmp, { force: true }); }
  await refreshPresets();
  return snapshot();
});

function userPreset(name) {
  const p = presetList.find((x) => x.name === name);
  if (!p || p.builtin || !PRESET_RE.test(name)) throw new Error(t("main.notUserPreset"));
  return p;
}

ipcMain.handle("presets:rename", async (_e, { name, next }) => {
  const p = userPreset(name);
  if (!PRESET_RE.test(next || "")) throw new Error(t("main.badPresetName"));
  if (next === name) return snapshot();
  const dest = path.join(presetsDir(), `${next}.json`);
  if (fs.existsSync(dest)) throw new Error(t("main.presetTaken", { name: next }));
  const data = JSON.parse(await fsp.readFile(p.path, "utf8"));
  if (!data.meta || data.meta.name === name || !data.meta.name) data.meta = { ...(data.meta || {}), name: next };
  const tmp = await writeJsonAtomic(dest, data);
  try { await validateFile(tmp); await fsp.rename(tmp, dest); } finally { await fsp.rm(tmp, { force: true }); }
  await fsp.rm(p.path);
  if (appValue("generation.preset") === name) { store.app["generation.preset"] = next; await saveStore(); }
  await refreshPresets();
  return snapshot();
});

ipcMain.handle("presets:delete", async (_e, { name }) => {
  const p = userPreset(name);
  if (!await confirm(t("main.deletePresetMessage", { name: p.title || name }), t("main.deletePresetDetail"), t("main.delete"))) return null;
  await fsp.rm(p.path);
  if (appValue("generation.preset") === name) { delete store.app["generation.preset"]; await saveStore(); }
  await refreshPresets();
  return snapshot();
});

ipcMain.handle("presets:import", async () => {
  const r = await dialog.showOpenDialog(win, { title: t("main.importPresetTitle"), properties: ["openFile"],
                                               filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled) return null;
  const src = r.filePaths[0];
  await validateFile(src);
  const name = path.basename(src, ".json").replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64) || "imported";
  const dest = path.join(presetsDir(), `${name}.json`);
  if (fs.existsSync(dest) && !await confirm(t("main.presetExists", { name }), "", t("main.overwrite"))) return null;
  await fsp.mkdir(presetsDir(), { recursive: true });
  await fsp.copyFile(src, dest);
  await refreshPresets();
  return snapshot();
});

ipcMain.handle("presets:export", async (_e, { name }) => {
  const p = presetList.find((x) => x.name === name);
  if (!p) throw new Error(t("main.notUserPreset"));
  const r = await dialog.showSaveDialog(win, { title: t("main.exportPresetTitle"), defaultPath: `${name}.json`,
                                               filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled || !r.filePath) return null;
  await fsp.copyFile(p.path, r.filePath);
  return path.basename(r.filePath);
});

// Copies a preset's values into the generation defaults and tuning (replacing them) and makes
// "default" the default preset, so the values are not applied twice.
ipcMain.handle("presets:apply", async (_e, { name }) => {
  const p = presetList.find((x) => x.name === name);
  if (!p) throw new Error(t("main.notUserPreset"));
  const tuned = Object.keys(store.config).some((sid) => sid !== "preview");
  if (tuned && !await confirm(t("main.applyPresetMessage", { name: p.title || name }), t("main.applyPresetDetail"), t("main.apply"))) return null;
  const config = store.config.preview ? { preview: store.config.preview } : {};
  for (const [sid, values] of Object.entries(p.settings || {})) {
    if (sid === "preview") continue;
    for (const [field, value] of Object.entries(values)) setConfigIn(config, `${sid}.${field}`, value);
  }
  store.config = config;
  delete store.app["generation.preset"];
  await saveStore();
  return snapshot();
});

// ---------------------------------------------------------------- IPC: model, video

ipcMain.handle("model:open", async (_e, { dir, maxTex }) => {
  if (!dir) {
    const start = appValue("general.modelDir") || (appValue("general.lastModel") && path.dirname(appValue("general.lastModel"))) || os.homedir();
    const r = await dialog.showOpenDialog(win, { title: t("main.modelDialogTitle"), properties: ["openDirectory"], defaultPath: start });
    if (r.canceled) return null;
    dir = r.filePaths[0];
  }
  const settingsFile = findSettings(dir);
  const view = await viewCopy(dir, maxTex);
  if (store.app["general.lastModel"] !== dir) { store.app["general.lastModel"] = dir; saveStore().catch(() => {}); }
  return {
    dir, name: settingsFile.replace(/\.model3\.json$/, ""),
    settingsUrl: `${rootUrl(view)}/${encodeURIComponent(settingsFile)}`,
    motions: await listMotions(dir),
  };
});

ipcMain.handle("model:motions", async (_e, dir) => listMotions(dir));

// Streams of a media file from ffprobe: {duration, video: {width, height} | null, audio: {codec,
// sampleRate, channels} | null}, or null when ffprobe is not installed or cannot read the file.
async function probeMedia(file) {
  const r = await capture("ffprobe", ["-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,sample_rate,channels,width,height",
                                      "-of", "json", file], { cwd: os.homedir() });
  if (r.code !== 0) return null;
  let info;
  try { info = JSON.parse(r.stdout); } catch { return null; }
  const streams = info.streams || [];
  const v = streams.find((s) => s.codec_type === "video" && s.width), a = streams.find((s) => s.codec_type === "audio");
  return {
    duration: Number(info.format?.duration) || null,
    video: v ? { width: v.width, height: v.height } : null,
    audio: a ? { codec: a.codec_name || "", sampleRate: Number(a.sample_rate) || null, channels: a.channels || null } : null,
  };
}

// A source for `v2l run`: a video or an audio file. only: "audio" picks the separate audio track
// of a video (`--audio`). probe is null when ffprobe is missing; the renderer then checks itself.
ipcMain.handle("source:open", async (_e, { file, only } = {}) => {
  if (!file) {
    const filters = only === "audio"
      ? [{ name: t("main.audioFilter"), extensions: AUDIO_EXT }]
      : [{ name: t("main.mediaFilter"), extensions: [...VIDEO_EXT, ...AUDIO_EXT] }, { name: t("main.videoFilter"), extensions: VIDEO_EXT }, { name: t("main.audioFilter"), extensions: AUDIO_EXT }];
    const r = await dialog.showOpenDialog(win, { title: t(only === "audio" ? "main.audioDialogTitle" : "main.videoDialogTitle"), properties: ["openFile"], filters });
    if (r.canceled) return null;
    file = r.filePaths[0];
  }
  const ext = path.extname(file).slice(1).toLowerCase();
  const kind = AUDIO_EXT.includes(ext) ? "audio" : VIDEO_EXT.includes(ext) ? "video" : null;
  if (!kind || (only === "audio" && kind !== "audio")) throw new Error(t("drop.wrongType"));
  const size = (await fsp.stat(file)).size;
  return { file, name: path.basename(file), url: fileUrl(file), kind, size, probe: await probeMedia(file) };
});

// ---------------------------------------------------------------- IPC: compute

function computeEnv() {
  const env = { ...ENV };
  const host = appValue("compute.host"), dir = appValue("compute.remoteDir");
  if (host) env.V2L_HOST = host; else delete env.V2L_HOST;
  env.V2L_DIR = dir || APP_DEFAULTS["compute.remoteDir"];
  return env;
}

// {ok, mode, ...}: remote -> `v2l-remote check --json`, local -> `<v2l> version --json`.
ipcMain.handle("compute:check", async () => {
  const mode = appValue("compute.mode");
  if (mode === "remote") {
    if (!appValue("compute.host")) return { ok: false, mode, configured: false };
    const r = await capture(REMOTE, ["check", "--json"], { env: computeEnv() });
    const out = lastJson(r.stdout) || { ok: false, error: { type: "remote", message: r.stderr.trim().slice(-400) || `exit ${r.code}` } };
    return { ...out, mode, configured: true, code: r.code };
  }
  const exe = executable();
  if (!whichSync(exe)) return { ok: false, mode, configured: false, error: { type: "missing", message: t("main.executableMissing", { path: exe }) } };
  const r = await capture(exe, ["version", "--json"], { env: ENV, cwd: os.homedir() });
  const out = lastJson(r.stdout) || { ok: false, error: { type: "runtime", message: r.stderr.trim().slice(-400) || `exit ${r.code}` } };
  return { ...out, mode, configured: true, code: r.code };
});

// Setup tasks stream plain text lines on "task:log" and finish with "task:done".
ipcMain.handle("task:start", async (_e, { kind }) => {
  if (task) throw new Error(t("main.taskBusy"));
  if (job) throw new Error(t("main.busy"));
  let steps;
  if (kind === "setup-remote") {
    if (!appValue("compute.host")) throw new Error(t("main.noHost"));
    steps = [[REMOTE, ["setup"], computeEnv()]];
  } else if (kind === "setup-local") {
    const venv = path.join(ROOT, ".venv");
    steps = [["python3", ["-m", "venv", venv], ENV], [path.join(venv, "bin", "pip"), ["install", "-e", ROOT], ENV]];
  } else throw new Error(t("main.unknownTask"));
  const id = crypto.randomBytes(4).toString("hex");
  task = { id, kind, proc: null };
  (async () => {
    let code = 0;
    for (const [cmd, args, env] of steps) {
      send("task:log", { id, line: `$ ${[cmd, ...args].map(shellQuote).join(" ")}` });
      code = await new Promise((resolve) => {
        const p = spawn(cmd, args, { env, cwd: ROOT });
        task.proc = p;
        const forward = (buf) => { for (const line of buf.toString().split(/\r?\n/)) if (line.trim()) send("task:log", { id, line }); };
        p.stdout.on("data", forward);
        p.stderr.on("data", forward);
        p.on("error", (err) => { send("task:log", { id, line: t("main.spawnFailed", { error: err.message }) }); resolve(-1); });
        p.on("close", (c) => resolve(c));
      });
      if (code !== 0) break;
    }
    task = null;
    send("task:done", { id, kind, code });
  })();
  return { id };
});

ipcMain.handle("task:cancel", () => { task?.proc?.kill("SIGTERM"); return !!task; });

// ---------------------------------------------------------------- IPC: runs

// MediaPipe / TensorFlow chatter carries no information for the user.
const NOISE = /^(W0000|I0000|INFO:|WARNING: All log|WARNING: Logging)/;

function freeName(dir, name) {
  if (!fs.existsSync(path.join(dir, name))) return name;
  if (appValue("output.onExists") !== "suffix") throw new Error(t("main.exists"));
  const stem = name.replace(/_\d+$/, "").slice(0, 74);
  for (let n = 2; n < 10000; n++) {
    const candidate = `${stem}_${n}`;
    if (!fs.existsSync(path.join(dir, candidate))) return candidate;
  }
  throw new Error(t("main.exists"));
}

async function installMotion(file, modelDir) {
  const dest = path.join(modelDir, path.basename(file));
  if (fs.existsSync(dest)) {
    const { response } = await dialog.showMessageBox(win, {
      type: "warning", title: t("main.overwriteTitle"), message: t("main.overwriteMessage"),
      detail: path.basename(dest), buttons: [t("main.cancel"), t("main.overwrite")], defaultId: 0, cancelId: 0,
    });
    if (response !== 1) return null;
  }
  await fsp.copyFile(file, dest);
  return dest;
}

// o: {input (video or audio file), audio?: <separate audio track for a video>, modelDir, name, preset,
//     perRun: {"section.name": value}, suffix?: true, reuseConfig?: <configFile of an earlier run>,
//     preview?: true, mapping?: <draft v2l.mapping>}.
// The model's saved mapping overrides apply, or the draft for a preview. Previews go to a
// temporary folder ("Keep" moves them to the output folder) and are never installed.
const previewsDir = () => path.join(app.getPath("userData"), "previews");
ipcMain.handle("run:start", async (_e, o) => {
  if (job) throw new Error(t("main.busy"));
  if (task) throw new Error(t("main.taskBusy"));
  let name = String(o.name || "");
  if (!NAME_RE.test(name)) throw new Error(t("main.badName"));
  const out = o.preview ? previewsDir() : outDir();
  const mode = appValue("compute.mode");
  if (mode === "remote" && !appValue("compute.host")) throw new Error(t("main.noHost"));
  const taken = (n) => fs.existsSync(path.join(out, n)) || (o.preview && fs.existsSync(path.join(outDir(), n)));
  if (o.suffix || o.preview) {
    for (let n = 2; taken(name); n++) name = `${String(o.name).replace(/_\d+$/, "").slice(0, 74)}_${n}`;
  } else name = freeName(out, name);
  const runsDir = path.join(app.getPath("userData"), "runs");
  const configFile = path.join(runsDir, `${name}.json`);
  let body;
  if (o.reuseConfig) {
    // "Apply mapping": the same configuration as the earlier run, so tracking is reused.
    const prior = inside(runsDir, path.relative(runsDir, String(o.reuseConfig)));
    if (!prior || !fs.existsSync(prior)) throw new Error(t("main.runConfigMissing"));
    body = JSON.parse(await fsp.readFile(prior, "utf8"));
  } else {
    const preset = PRESET_RE.test(o.preset || "") ? o.preset : "default";
    if (preset !== "default") await refreshPresets();
    body = { version: 1, ...sparseAgainst(effectiveConfig(preset, o.perRun || {}), schemaDefaults) };
  }
  const tmp = await writeJsonAtomic(configFile, body);
  try { await validateFile(tmp); await fsp.rename(tmp, configFile); } finally { await fsp.rm(tmp, { force: true }); }
  let mapping = mappingOverrides(o.modelDir);
  if (o.mapping) {
    const draft = checkMapping({ ...o.mapping, format: "v2l.mapping" });
    mapping = null;
    if (!mappingEmpty(draft)) {
      mapping = path.join(runsDir, `${name}.mapping.json`);
      const part = await writeJsonAtomic(mapping, draft);
      try { await validateMapping(part); await fsp.rename(part, mapping); } finally { await fsp.rm(part, { force: true }); }
    }
  }

  // --dump-signals: inputs and bridge outputs over time, for the mapping editor's graphs.
  const common = ["--model", o.modelDir, "--name", name, "--no-install", "--config", configFile, ...(mapping ? ["--mapping", mapping] : []),
                  ...(o.audio ? ["--audio", String(o.audio)] : []), "--dump-signals", "--json", "--progress", "json"];
  const [cmd, args, env, cwd] = mode === "remote"
    ? [REMOTE, ["run", o.input, ...common.slice(0, 4), "--out", out, ...common.slice(4)], computeEnv(), ROOT]
    : [executable(), ["run", o.input, ...common.slice(0, 4), "--out", path.join(out, name), ...common.slice(4)], ENV, out];
  await fsp.mkdir(out, { recursive: true });
  const filter = appValue("advanced.filterLog");
  send("run:log", `$ ${[mode === "remote" ? "v2l-remote" : cmd, ...args].map(shellQuote).join(" ")}`);
  send("run:event", { event: "start", mode, name });
  const proc = spawn(cmd, args, { env, cwd });
  job = { proc, name, cancelled: false };
  let stdout = "", pending = "";
  proc.stdout.on("data", (b) => { stdout += b; });
  proc.stderr.on("data", (b) => {
    pending += b.toString();
    const lines = pending.split(/\r?\n/);
    pending = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let event = null;
      if (line.startsWith("{")) { try { event = JSON.parse(line); } catch { /* plain text */ } }
      if (event && event.event) {
        send("run:event", event);
        if (event.event === "log" && event.message) send("run:log", event.message);
      } else if (!filter || !NOISE.test(line)) send("run:log", line);
    }
  });
  proc.on("error", (err) => send("run:log", t("main.spawnFailed", { error: err.message })));
  proc.on("close", async (code, signal) => {
    if (pending.trim()) send("run:log", pending);
    const cancelled = job?.cancelled || false;
    job = null;
    const final = lastJson(stdout);
    const dir = final?.out_dir ? path.resolve(cwd, final.out_dir) : path.join(out, name);
    const pick = (p, fallback) => (p ? path.resolve(cwd, p) : fallback);
    const motionFile = pick(final?.files?.motion, path.join(dir, `${name}.motion3.json`));
    const reportFile = pick(final?.files?.report, path.join(dir, `${name}.report.json`));
    const signalsFile = pick(final?.files?.signals, path.join(dir, `${name}.signals.json`));
    let report = null;
    try { report = JSON.parse(await fsp.readFile(reportFile, "utf8")); } catch { /* failed run */ }
    const ok = code === 0 && fs.existsSync(motionFile);
    const result = {
      code: signal ? null : code, cancelled, name, dir, mode, configFile, mappingFile: mapping, preview: !!o.preview,
      motion: ok ? { file: motionFile, url: fileUrl(motionFile), name } : null,
      signals: ok && fs.existsSync(signalsFile) ? signalsFile : null,
      report, error: final && final.ok === false ? final.error : null, summary: final?.summary || null, installed: null,
    };
    if (ok && !o.preview && appValue("output.autoInstall") && o.modelDir) {
      try { result.installed = await installMotion(motionFile, o.modelDir); } catch (err) { send("run:log", err.message); }
    }
    send("run:done", result);
  });
  return { name };
});

// A kept preview moves from the temporary folder into the output folder (files keep their names).
function previewDir(dir) {
  const resolved = path.resolve(String(dir || ""));
  if (path.dirname(resolved) !== previewsDir() || !fs.existsSync(resolved)) throw new Error(t("main.previewMissing"));
  return resolved;
}
ipcMain.handle("run:keep", async (_e, { dir }) => {
  const src = previewDir(dir);
  const name = path.basename(src);
  if (fs.existsSync(path.join(outDir(), name))) throw new Error(t("main.exists"));
  await fsp.mkdir(outDir(), { recursive: true });
  const dest = path.join(outDir(), name);
  try { await fsp.rename(src, dest); } catch { await fsp.cp(src, dest, { recursive: true }); await fsp.rm(src, { recursive: true, force: true }); }
  const motion = path.join(dest, `${name}.motion3.json`);
  const signals = path.join(dest, `${name}.signals.json`);
  return { dir: dest, motion: { file: motion, url: fileUrl(motion), name }, signals: fs.existsSync(signals) ? signals : null };
});
ipcMain.handle("run:discard", async (_e, { dir }) => { await fsp.rm(previewDir(dir), { recursive: true, force: true }); return true; });

ipcMain.handle("run:cancel", async () => {
  const active = job;
  if (!active) return false;
  const { response } = await dialog.showMessageBox(win, {
    type: "warning", title: t("main.cancelTitle"), message: t("main.cancelMessage"),
    detail: t("main.cancelDetail"),
    buttons: [t("main.keepWaiting"), t("main.stopWaiting")], defaultId: 0, cancelId: 0,
  });
  if (response !== 1 || job !== active) return false;
  active.cancelled = true;
  active.proc.kill("SIGTERM");
  return true;
});

ipcMain.handle("run:report", async (_e, file) => JSON.parse(await fsp.readFile(file, "utf8")));

ipcMain.handle("motion:install", async (_e, { file, modelDir }) => installMotion(file, modelDir));

// ---------------------------------------------------------------- IPC: motion editing

// `python3 -m v2l.motionedit` (standard library only) reads, writes, edits and validates
// motions; Studio edits interactively and writes through it so files match the CLI exactly.
const scratch = () => path.join(app.getPath("temp"), `v2l-studio-${process.pid}`);
let scratchId = 0;

async function scratchFile(name, data) {
  const dir = path.join(scratch(), String(++scratchId));
  await fsp.mkdir(dir, { recursive: true });
  const file = path.join(dir, name);
  if (data !== undefined) await fsp.writeFile(file, typeof data === "string" ? data : JSON.stringify(data));
  return file;
}

function toolError(out, stderr, code) {
  const e = out?.error;
  if (!e) return new Error(stderr.trim().split("\n").slice(-3).join("\n") || t("main.pythonFailed", { code }));
  const problems = (e.problems || out.problems || []).slice(0, 6);
  return new Error([e.message, ...problems].filter(Boolean).join("\n"));
}

async function motionTool(args, input) {
  const r = await capture("python3", ["-m", "v2l.motionedit", ...args, "--json"], { env: PY_ENV, input });
  const out = lastJson(r.stdout);
  if (r.code !== 0 || !out || out.ok === false) throw toolError(out, r.stderr, r.code);
  return out;
}

const MOTION_RE = /\.motion3\.json$/;
const motionName = (file) => path.basename(file).replace(MOTION_RE, "");

function checkMotionFile(file) {
  if (typeof file !== "string" || !path.isAbsolute(file) || !MOTION_RE.test(file)) throw new Error(t("main.badMotionFile"));
  return path.resolve(file);
}

async function rangesFile(ranges) {
  return ranges && Object.keys(ranges).length ? scratchFile("ranges.json", ranges) : null;
}

ipcMain.handle("motion:read", async (_e, { file }) => {
  file = checkMotionFile(file);
  const out = await motionTool(["read", file]);
  return { file, name: motionName(file), dir: path.dirname(file), motion: out.motion };
});

// Runs operations of the shared vocabulary (docs/motion-editing.md §4) on an editable motion
// and returns the result; nothing is written.
ipcMain.handle("motion:apply", async (_e, { motion, ops, ranges }) => {
  const src = await scratchFile("edit.json", motion);
  const args = ["edit", src, "--ops", "-", "--out", "-"];
  const r = await rangesFile(ranges);
  if (r) args.push("--ranges", r);
  try {
    const out = await motionTool(args, JSON.stringify({ operations: ops }));
    return { motion: out.motion, counts: out.counts || null, curves: out.curves || null };
  } finally { await fsp.rm(path.dirname(src), { recursive: true, force: true }); if (r) await fsp.rm(path.dirname(r), { recursive: true, force: true }); }
});

// mode: "new" (name, next to the source), "overwrite" (the source file) or "model" (copy into
// the model folder, as "Save to model folder" for results).
ipcMain.handle("motion:save", async (_e, { motion, mode, file, name, modelDir }) => {
  file = checkMotionFile(file);
  let dest;
  if (mode === "new") {
    if (!NAME_RE.test(name || "")) throw new Error(t("main.badName"));
    dest = path.join(path.dirname(file), `${name}.motion3.json`);
    if (fs.existsSync(dest) && !await confirm(t("main.motionExists", { name: path.basename(dest) }), path.dirname(dest), t("main.overwrite"))) return null;
  } else if (mode === "overwrite") {
    dest = file;
    if (!await confirm(t("main.overwriteMotionMessage", { name: path.basename(file) }), t("main.overwriteMotionDetail"), t("main.overwrite"))) return null;
  } else if (mode === "model") {
    if (!modelDir || !hasModel(modelDir)) throw new Error(t("main.noModel"));
    if (!NAME_RE.test(name || "")) throw new Error(t("main.badName"));
    const staged = await scratchFile(`${name}.motion3.json`);
    try {
      await motionTool(["write", "-", "--out", staged, "--overwrite"], JSON.stringify(motion));
      const installed = await installMotion(staged, modelDir);
      return installed ? { file: installed, name: motionName(installed), dir: modelDir, url: fileUrl(installed) } : null;
    } finally { await fsp.rm(path.dirname(staged), { recursive: true, force: true }); }
  } else throw new Error(t("main.badSaveMode"));
  // Written beside the destination, then renamed over it, so a failed write leaves the old file.
  const tmp = path.join(path.dirname(dest), `.${path.basename(dest)}.${process.pid}.part.motion3.json`);
  try {
    const out = await motionTool(["write", "-", "--out", tmp, "--overwrite"], JSON.stringify(motion));
    await fsp.rename(tmp, dest);
    return { file: dest, name: motionName(dest), dir: path.dirname(dest), url: fileUrl(dest), counts: out.counts || null };
  } finally { await fsp.rm(tmp, { force: true }); }
});

// ---------------------------------------------------------------- IPC: mapping

// Overrides per model in ~/.config/v2l/mappings/<model folder name>.json: one `v2l.mapping` file
// with three optional layers (input_curves, bridge, entries), each holding only changed fields.
// The base comes from `v2l mapping show` on the compute side.
const mappingsDir = () => path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "v2l", "mappings");
const mappingFile = (modelDir) => path.join(mappingsDir(), `${path.basename(path.resolve(modelDir))}.json`);

function mappingEmpty(m) { return !Object.keys(m.input_curves || {}).length && !(m.bridge || []).length && !(m.entries || []).length; }

function mappingOverrides(modelDir) {
  const file = modelDir ? mappingFile(modelDir) : null;
  if (!file || !fs.existsSync(file)) return null;
  try { return mappingEmpty(JSON.parse(fs.readFileSync(file, "utf8"))) ? null : file; } catch { return null; }
}

// Shape check of the three layers; Python validates the content.
function checkMapping(data) {
  const bad = () => new Error(t("main.notMapping"));
  if (!data || data.format !== "v2l.mapping") throw bad();
  const curves = data.input_curves ?? {};
  const bridge = data.bridge ?? [];
  const entries = data.entries ?? [];
  if (typeof curves !== "object" || Array.isArray(curves) || !Array.isArray(bridge) || !Array.isArray(entries)) throw bad();
  for (const c of Object.values(curves)) if (!c || !Array.isArray(c.points)) throw bad();
  for (const b of bridge) if (!b || typeof b.name !== "string" || !b.name) throw bad();
  for (const e of entries) if (!e || typeof e.output !== "string" || !e.output) throw bad();
  const out = { format: "v2l.mapping", version: 1 };
  if (Object.keys(curves).length) out.input_curves = curves;
  if (bridge.length) out.bridge = bridge;
  if (entries.length) out.entries = entries;
  return out;
}

async function validateMapping(file) {
  const r = await capture("python3", ["-m", "v2l.cli", "mapping", "validate", file, "--json"], { env: PY_ENV });
  const out = lastJson(r.stdout);
  if (r.code !== 0 || !out || out.ok === false) throw toolError(out, r.stderr, r.code);
}

// One `v2l ...` call on the configured compute side (v2l-remote for remote, the local executable).
async function computeJson(args) {
  const mode = appValue("compute.mode");
  if (mode === "remote" && !appValue("compute.host")) throw new Error(t("main.noHost"));
  const r = mode === "remote"
    ? await capture(REMOTE, args, { env: computeEnv() })
    : await capture(executable(), args, { env: ENV, cwd: os.homedir() });
  const out = lastJson(r.stdout);
  if (r.code !== 0 || !out || out.ok === false) throw toolError(out, r.stderr, r.code);
  return out;
}

ipcMain.handle("mapping:show", async (_e, { modelDir }) => {
  if (!modelDir || !hasModel(modelDir)) throw new Error(t("main.noModel"));
  return computeJson(["mapping", "show", "--model", modelDir, "--json"]);
});

ipcMain.handle("mapping:inputs", async () => computeJson(["mapping", "inputs", "--json"]));

ipcMain.handle("mapping:load", async (_e, { modelDir }) => {
  const file = mappingFile(modelDir);
  if (!fs.existsSync(file)) return { file, mapping: checkMapping({ format: "v2l.mapping" }) };
  let data;
  try { data = JSON.parse(await fsp.readFile(file, "utf8")); } catch (err) { throw new Error(t("main.notJson", { error: err.message })); }
  return { file, mapping: checkMapping(data) };
});

ipcMain.handle("mapping:save", async (_e, { modelDir, mapping }) => {
  if (!modelDir || !hasModel(modelDir)) throw new Error(t("main.noModel"));
  const file = mappingFile(modelDir);
  const data = checkMapping({ ...mapping, format: "v2l.mapping" });
  if (mappingEmpty(data)) { await fsp.rm(file, { force: true }); return { file, mapping: data }; }
  const tmp = await writeJsonAtomic(file, data);
  try { await validateMapping(tmp); await fsp.rename(tmp, file); } finally { await fsp.rm(tmp, { force: true }); }
  return { file, mapping: data };
});

ipcMain.handle("mapping:import", async () => {
  const r = await dialog.showOpenDialog(win, { title: t("main.importMappingTitle"), properties: ["openFile"],
                                               filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled) return null;
  let data;
  try { data = JSON.parse(await fsp.readFile(r.filePaths[0], "utf8")); } catch (err) { throw new Error(t("main.notJson", { error: err.message })); }
  return { name: path.basename(r.filePaths[0]), mapping: checkMapping(data) };
});

ipcMain.handle("mapping:export", async (_e, { modelDir, mapping }) => {
  const r = await dialog.showSaveDialog(win, { title: t("main.exportMappingTitle"), defaultPath: `${path.basename(modelDir || "model")}.mapping.json`,
                                               filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled || !r.filePath) return null;
  await fsp.writeFile(r.filePath, JSON.stringify(checkMapping({ ...mapping, format: "v2l.mapping" }), null, 1) + "\n");
  return path.basename(r.filePath);
});

ipcMain.handle("mapping:reveal", async (_e, { modelDir }) => {
  const file = mappingFile(modelDir);
  if (fs.existsSync(file)) shell.showItemInFolder(file);
  else { await fsp.mkdir(mappingsDir(), { recursive: true }); await shell.openPath(mappingsDir()); }
});

// Formula check for the bridge editor (standard library, runs here in both compute modes). The
// draft mapping is passed so outputs added in the editor count as known names. An invalid
// formula is a normal answer (ok: false with a problem), not an error.
ipcMain.handle("bridge:check", async (_e, { func, mapping }) => {
  const args = ["-m", "v2l.cli", "bridge", "check", "--func", String(func), "--json"];
  let tmp = null;
  if (mapping && !mappingEmpty(mapping)) { tmp = await scratchFile("mapping.json", checkMapping({ ...mapping, format: "v2l.mapping" })); args.push("--mapping", tmp); }
  try {
    const r = await capture("python3", args, { env: PY_ENV });
    const out = lastJson(r.stdout);
    if (!out) throw toolError(null, r.stderr, r.code);
    // A draft mapping that does not load is reported as the formula's problem.
    if (!out.tokens && out.error) return { ok: false, valid: false, tokens: [], unknown: [], problem: { message: out.error.message } };
    return out;
  } finally { if (tmp) await fsp.rm(path.dirname(tmp), { recursive: true, force: true }); }
});

// Plain JSON files for curve import/export.
ipcMain.handle("json:open", async (_e, { title }) => {
  const r = await dialog.showOpenDialog(win, { title: String(title), properties: ["openFile"], filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled) return null;
  try { return JSON.parse(await fsp.readFile(r.filePaths[0], "utf8")); } catch (err) { throw new Error(t("main.notJson", { error: err.message })); }
});

ipcMain.handle("json:save", async (_e, { title, name, data }) => {
  const r = await dialog.showSaveDialog(win, { title: String(title), defaultPath: String(name || "data.json"), filters: [{ name: "JSON", extensions: ["json"] }] });
  if (r.canceled || !r.filePath) return null;
  await fsp.writeFile(r.filePath, JSON.stringify(data, null, 1) + "\n");
  return path.basename(r.filePath);
});

// Signals of a result (tracking inputs and bridge outputs over time) for the mapping graphs.
ipcMain.handle("run:signals", async (_e, file) => {
  if (typeof file !== "string" || !file.endsWith(".signals.json")) return null;
  try { return JSON.parse(await fsp.readFile(file, "utf8")); } catch { return null; }
});

ipcMain.handle("dialog:confirm", async (_e, { message, detail, confirmLabel }) => confirm(String(message), String(detail || ""), String(confirmLabel)));

// Native text undo/redo for the focused field (the Edit menu routes through the renderer).
ipcMain.handle("edit:native", (_e, which) => { if (which === "redo") win?.webContents.redo(); else win?.webContents.undo(); });

ipcMain.handle("reveal", (_e, p) => shell.showItemInFolder(p));

// camera: false asks for the microphone only (voice takes). Chromium's fake capture devices
// (--use-fake-device-for-media-stream, for automated tests) need no system permission.
ipcMain.handle("camera:access", async (_e, { camera = true, audio } = {}) => {
  if (process.platform !== "darwin" || app.commandLine.hasSwitch("use-fake-device-for-media-stream")) return true;
  const ask = async (kind) => systemPreferences.getMediaAccessStatus(kind) === "granted" || systemPreferences.askForMediaAccess(kind);
  if (camera && !await ask("camera")) return false;
  return !audio || ask("microphone");
});

// A take from MediaRecorder: written as-is, then remuxed (no re-encode) with ffmpeg when available
// so the file has a proper index for seeking and an exact duration. Webcam takes stay MP4 or WebM;
// voice takes become .m4a (from MP4) or .opus (Ogg, from WebM/Opus) so `v2l run` reads them as
// audio. Without ffmpeg a WebM voice take cannot be remuxed: ok is false and the renderer saves a
// WAV (ext "wav", written as-is) instead.
ipcMain.handle("recording:save", async (_e, { bytes, ext, voice = false }) => {
  const dir = path.join(outDir(), "recordings");
  await fsp.mkdir(dir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "_").slice(0, 15);
  const prefix = voice ? "voice" : "rec";
  if (voice && ext === "wav") {
    const file = path.join(dir, `${prefix}_${stamp}.wav`);
    await fsp.writeFile(file, Buffer.from(bytes));
    return { ok: true, file, name: path.basename(file) };
  }
  const mp4 = ext === "mp4";
  const raw = path.join(dir, `${prefix}_${stamp}.raw.${mp4 ? "mp4" : "webm"}`);
  await fsp.writeFile(raw, Buffer.from(bytes));
  const out = path.join(dir, `${prefix}_${stamp}.${voice ? (mp4 ? "m4a" : "opus") : (mp4 ? "mp4" : "webm")}`);
  const code = await new Promise((resolve) => {
    const ff = spawn("ffmpeg", ["-v", "error", "-y", "-i", raw, "-c", "copy", ...(mp4 ? ["-movflags", "+faststart"] : []), out], { env: ENV, stdio: "ignore" });
    ff.on("error", () => resolve(-1));
    ff.on("close", resolve);
  });
  if (code === 0 && fs.existsSync(out)) await fsp.rm(raw);
  else if (voice && !mp4) { await fsp.rm(raw, { force: true }); await fsp.rm(out, { force: true }); return { ok: false }; }
  else await fsp.rename(raw, out);
  return { ok: true, file: out, name: path.basename(out) };
});

// ---------------------------------------------------------------- menu and window

function openSettings(section) { send("settings:open", section); }

function buildMenu() {
  const mac = process.platform === "darwin";
  const settings = { id: "settings", label: t("main.menu.settings"), accelerator: "CmdOrCtrl+,", click: () => openSettings("general") };
  const template = [
    ...(mac ? [{ label: PRODUCT, submenu: [
      { label: t("main.menu.about"), click: () => openSettings("about") }, { type: "separator" },
      settings, { type: "separator" },
      { role: "services", label: t("main.menu.services") }, { type: "separator" },
      { role: "hide", label: t("main.menu.hide") }, { role: "hideOthers", label: t("main.menu.hideOthers") },
      { role: "unhide", label: t("main.menu.showAll") }, { type: "separator" },
      { role: "quit", label: t("main.menu.quit") },
    ] }] : [{ label: t("main.menu.file"), submenu: [settings, { type: "separator" }, { role: "quit", label: t("main.menu.quit") }] }]),
    { label: t("main.menu.edit"), submenu: [
      // Undo/Redo go to the renderer, which undoes text in a focused field natively (edit:native)
      // and otherwise steps the motion editor's history.
      { label: t("main.menu.undo"), accelerator: "CmdOrCtrl+Z", click: () => send("menu:edit", "undo") },
      { label: t("main.menu.redo"), accelerator: mac ? "Shift+CmdOrCtrl+Z" : "CmdOrCtrl+Y", click: () => send("menu:edit", "redo") },
      { type: "separator" },
      { role: "cut", label: t("main.menu.cut") }, { role: "copy", label: t("main.menu.copy") },
      { role: "paste", label: t("main.menu.paste") }, { role: "selectAll", label: t("main.menu.selectAll") },
    ] },
    { label: t("main.menu.view"), submenu: [
      { role: "togglefullscreen", label: t("main.menu.fullscreen") },
      { role: "toggleDevTools", label: t("main.menu.devtools") },
    ] },
    { label: t("main.menu.window"), submenu: [
      { role: "minimize", label: t("main.menu.minimize") },
      { role: "close", label: t("main.menu.close") },
    ] },
    { label: t("main.menu.help"), submenu: [
      ...(mac ? [] : [{ label: t("main.menu.about"), click: () => openSettings("about") }]),
      { label: t("main.menu.repository"), click: () => shell.openExternal(REPOSITORY) },
      { label: t("main.menu.notices"), click: () => shell.openPath(NOTICES) },
    ] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  port = await startServer();
  loadStore();
  lang = resolveLanguage();
  applyTheme();
  buildMenu();
  try {
    schema = await configTool(["schema"]);
    schemaDefaults = Object.fromEntries(schema.sections.map((s) => [s.id, Object.fromEntries(s.fields.map((f) => [f.name, f.default]))]));
    fieldIndex = new Map(schema.sections.flatMap((s) => s.fields.map((f) => [f.key, f])));
    await refreshPresets();
    store = sanitizeStore({ app: store.app, config: store.config });
  } catch (err) {
    dialog.showErrorBox(PRODUCT, t("main.schemaFailed", { error: err.message }));
    app.quit();
    return;
  }
  try {
    await ensureAssets();
  } catch (err) {
    dialog.showErrorBox(PRODUCT, t("main.assetsFailed", { error: err.message }));
    app.quit();
    return;
  }
  win = new BrowserWindow({
    width: 1440, height: 940, minWidth: 960, minHeight: 680, title: PRODUCT, backgroundColor: themeBackground(),
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.loadURL(`${base()}/app/index.html?theme=${themeName()}`);
  win.webContents.on("will-prevent-unload", (event) => {
    const response = dialog.showMessageBoxSync(win, {
      type: "warning", title: t("main.leaveTitle"), message: t("main.leaveMessage"),
      detail: t("main.leaveDetail"),
      buttons: [t("main.stay"), t("main.leave")], defaultId: 0, cancelId: 0,
    });
    if (response === 1) event.preventDefault();
  });
  win.on("closed", () => { win = null; });
});

nativeTheme.on("updated", () => {
  if (!win || win.isDestroyed()) return;
  win.setBackgroundColor(themeBackground());
  send("theme:changed", themeName());
});

app.on("window-all-closed", () => {
  if (job) job.proc.kill("SIGTERM");
  if (task) task.proc?.kill("SIGTERM");
  app.quit();
});

// Scratch files and previews that were not kept are temporary.
app.on("will-quit", () => {
  for (const dir of [scratch(), previewsDir()]) { try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ } }
});
