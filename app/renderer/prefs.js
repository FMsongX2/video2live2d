// Studio settings as held by the main process (userData/settings.json). Every change goes
// through main, which validates, stores sparsely and returns the new snapshot.
const api = window.v2l;
let snap = null;
let fields = new Map();      // "section.name" -> schema field
const listeners = new Set();

function index() {
  fields = new Map(snap.schema.sections.flatMap((s) => s.fields.map((f) => [f.key, { ...f, section: s.id }])));
}

export const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export function errorText(error) {
  return String(error?.message || error).replace(/^Error invoking remote method '[^']+': (Error: )?/, "");
}

export const prefs = {
  get snap() { return snap; },
  replace(next, changed = { all: true }) {
    if (!next) return;
    snap = next; index();
    for (const fn of listeners) fn(changed);
  },
  on(fn) { listeners.add(fn); },

  // App settings (Studio preferences).
  app(key) { return snap.effective[key]; },
  appDefault(key) { return snap.appDefaults[key]; },
  appChoices(key) { return snap.appChoices[key]; },
  lock(key) { return snap.locks[key] || null; },
  isAppDefault(key) { return !(key in snap.app); },

  // Generation defaults and tuning: schema fields over Studio's defaults.
  field(key) { return fields.get(key); },
  fields() { return [...fields.values()]; },
  section(id) { return snap.schema.sections.find((s) => s.id === id); },
  configDefault(key) {
    const [sid, name] = key.split(".");
    const studio = snap.studioConfig[sid];
    return studio && name in studio ? studio[name] : fields.get(key).default;
  },
  config(key) {
    const [sid, name] = key.split(".");
    const stored = snap.config[sid];
    return stored && name in stored ? stored[name] : this.configDefault(key);
  },
  isConfigDefault(key) { return same(this.config(key), this.configDefault(key)); },
  tunedCount() { return Object.values(snap.config).reduce((n, s) => n + Object.keys(s).length, 0); },

  presets() { return snap.presets.filter((p) => !p.error); },
  preset(name) { return snap.presets.find((p) => p.name === name) || null; },
  // The value a run starts from with this preset: Studio default < preset < settings.
  defaultFor(key, presetName) {
    const [sid, name] = key.split(".");
    if (snap.config[sid] && name in snap.config[sid]) return snap.config[sid][name];
    const settings = this.preset(presetName)?.settings || {};
    if (settings[sid] && name in settings[sid]) return settings[sid][name];
    return this.configDefault(key);
  },

  async set(patch) {
    const changed = { app: Object.keys(patch.app || {}), config: Object.keys(patch.config || {}) };
    this.replace(await api.settings.set(patch), changed);
  },
  setApp(key, value) { return this.set({ app: { [key]: value } }); },
  setConfig(key, value) { return this.set({ config: { [key]: value } }); },
};
