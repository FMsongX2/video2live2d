// UI strings. One JSON table per language in ./locales, shared with the main process (dialogs).
// Elements carry their message key in data-i18n (text) or data-i18n-<attr> (title, aria-label,
// placeholder), with optional JSON params in data-i18n-args, so a language switch re-renders
// static and current dynamic text in place.

export const LANGUAGES = ["en", "ko", "ja", "zh-CN"];
const FALLBACK = "en";
const ATTRS = ["title", "aria-label", "placeholder"];

const tables = Object.fromEntries(await Promise.all(LANGUAGES.map(async (lang) => {
  const response = await fetch(new URL(`locales/${lang}.json`, import.meta.url));
  if (!response.ok) throw new Error(`locales/${lang}.json: HTTP ${response.status}`);
  return [lang, await response.json()];
})));
let current = FALLBACK;

export function language() { return current; }
export function isLanguage(lang) { return LANGUAGES.includes(lang); }
export function languageName(lang) { return tables[lang]["language.name"]; }

// BCP 47 tag (navigator.language) -> supported language.
export function matchLanguage(tag) {
  const t = String(tag || "").toLowerCase();
  if (t.startsWith("ko")) return "ko";
  if (t.startsWith("ja")) return "ja";
  if (t.startsWith("zh")) return "zh-CN";
  return FALLBACK;
}

// A translatable message usable as a parameter of another message.
export function msg(key, params) { return { key, params }; }

export function t(key, params) {
  const text = tables[current][key] ?? tables[FALLBACK][key] ?? key;
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name) => {
    const value = params[name];
    if (value == null) return whole;
    return typeof value === "object" && "key" in value ? t(value.key, value.params) : String(value);
  });
}

// The text of a key in one language (the settings search also matches English labels).
export function tIn(lang, key) { return tables[lang]?.[key] ?? tables[FALLBACK][key] ?? key; }
export function has(key) { return key in tables[current] || key in tables[FALLBACK]; }

function args(el) { return el.dataset.i18nArgs ? JSON.parse(el.dataset.i18nArgs) : undefined; }

export function setText(el, key, params) {
  el.dataset.i18n = key;
  if (params) el.dataset.i18nArgs = JSON.stringify(params); else delete el.dataset.i18nArgs;
  el.textContent = t(key, params);
}
export function setRaw(el, text) {
  delete el.dataset.i18n; delete el.dataset.i18nArgs;
  el.textContent = text;
}
export function setAttr(el, attr, key) {
  el.setAttribute(`data-i18n-${attr}`, key);
  el.setAttribute(attr, t(key));
}
export function clearAttr(el, attr) {
  el.removeAttribute(`data-i18n-${attr}`);
  el.removeAttribute(attr);
}

export function localize(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n, args(el));
  for (const attr of ATTRS) {
    for (const el of root.querySelectorAll(`[data-i18n-${attr}]`)) el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`)));
  }
}

export function setLanguage(lang) {
  current = isLanguage(lang) ? lang : FALLBACK;
  document.documentElement.lang = current;
  localize();
}
