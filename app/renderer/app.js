import { parseMotion } from "./motion.js";
import { Viewer } from "./viewer.js";
import { MotionEditor } from "./editor.js";
import { ICONS } from "./icons.js";
import { clearAttr, msg, setAttr, setLanguage, setRaw, setText, t } from "./i18n.js";
import { initMapping, isMappingOpen, openMapping, refreshMapping } from "./mapping.js";
import { errorText, prefs, same } from "./prefs.js";
import { initSettings, isSettingsOpen, openSettings, presetTitle, runCheck } from "./settings.js";

const $ = (id) => document.getElementById(id);
const api = window.v2l;
const state = {
  model: null, entries: [], entry: null, playing: false, t: 0,
  video: null, videoReady: false, result: null, running: false, loading: false,
  compute: null, server: null, capture: "closed", run: null, jobTimer: null, logs: [], autoName: "",
};
const NEUTRAL = { from: 0.3, to: 1.3 };
const MAX_RECORDING_SECONDS = 60;
const MIN_RECORDING_SECONDS = 2.3;
// Bench fields that start from the generation defaults (Settings), per run.
const BENCH_DEFAULTS = { "style.name": "style", "output.loop": "opt-loop", "output.interpolation": "interp", "input.flip": "flip", "expressions.enabled": "rare" };
// Stages reported by `v2l run --progress json`; v2l-remote adds upload and download.
const STAGES = ["probe", "tracking", "expressions", "solving", "writing", "preview"];
let cam = null;
let cameraRequest = 0;
let selectionRequest = 0;
let scrubbing = false;
const viewer = new Viewer($("stage"));
viewer.clock = clockTime;
for (const el of document.querySelectorAll("[data-icon]")) el.insertAdjacentHTML("afterbegin", ICONS[el.dataset.icon]);

// The motion editor previews through the same entry and clock as playback.
const editor = new MotionEditor({
  viewer,
  time: () => clockTime() ?? 0,
  seek: (time) => {
    if (!state.entry?.parsed) return;
    if (synced()) $("video").currentTime = time + state.entry.video.start; else state.t = time;
  },
  togglePlay: () => setPlaying(synced() ? $("video").paused : !state.playing).catch((err) => fail(err)),
  setPreview: (parsed) => { if (state.entry) { state.entry.parsed = parsed; viewer.motion = parsed; } },
  onSaved: (saved, previous) => editorSaved(saved, previous),
  onClose: () => editorClosed(),
  status: (key, params, tone = "idle") => { setText($("editor-status"), key, params); $("editor-status").closest("footer").dataset.tone = tone; },
  modelDir: () => state.model?.dir || null,
});

function status(key, params, tone = "idle") {
  setText($("status"), key, params);
  $("status").closest("footer").dataset.tone = tone;
}
const errorMessage = errorText;
function fail(error, prefix = "status.failedGeneric") {
  status("status.failed", { prefix: msg(prefix), error: errorMessage(error) }, "error");
}
function action(fn) {
  return (...args) => {
    try { return Promise.resolve(fn(...args)).catch((err) => fail(err)); }
    catch (err) { fail(err); }
  };
}
function locked() { return state.running || state.loading || state.capture !== "closed" || editor.open; }
function notConfigured() { return state.compute && state.compute.configured === false; }
function readyMessage() {
  if (!state.model) status("status.needModel");
  else if (!state.videoReady) status("status.needVideo");
  else if (notConfigured()) status(state.compute.mode === "local" ? "status.localMissing" : "status.notConfigured", null, "error");
  else if (state.server === false) status(state.compute?.mode === "local" ? "status.localFailed" : state.compute?.ok ? "status.hostNotInstalled" : "status.serverDown", null, "error");
  else if (state.server === null) status("status.checking");
  else status("status.ready");
}
function updateControls() {
  const busy = locked();
  $("run").disabled = busy || !state.model || !state.videoReady || state.server !== true;
  setText($("run"), state.running ? "job.running" : "job.run");
  if (state.server !== true) setAttr($("run"), "title", "server.notConfiguredTitle"); else clearAttr($("run"), "title");
  $("run-again").disabled = busy || !state.result || state.server !== true || !state.videoReady || state.video?.file !== state.result.source?.file || state.model?.dir !== state.result.modelDir;
  $("use-settings").disabled = busy || !state.result?.report?.config;
  $("apply-mapping").disabled = !canApplyMapping();
  $("preview-keep").disabled = $("preview-discard").disabled = busy;
  $("result-edit").disabled = busy || !state.result?.motion;
  $("edit-motion").disabled = busy || !state.entry?.file;
  $("open-mapping").disabled = state.loading || !state.model;
  $("cancel").classList.toggle("hidden", !state.running);
  $("cancel").disabled = !state.running || !!state.run?.cancelling;
  for (const id of ["open-model", "tex4k", "open-video", "open-cam", "motion-select", "sync"]) $(id).disabled = busy;
  $("settings-fields").disabled = busy;
  $("install").disabled = busy || !state.result || state.result.modelDir !== state.model?.dir;
  $("compare").disabled = busy || !state.result;
  $("play").disabled = !state.entry?.parsed || state.loading || state.capture !== "closed";
  $("scrub").disabled = !state.entry?.parsed || state.loading || state.capture !== "closed";
  const recording = state.capture === "recording";
  $("rec").disabled = !["preview", "recording"].includes(state.capture);
  setText($("rec"), recording ? "cam.stop" : state.capture === "countdown" ? "cam.getReady" : state.capture === "saving" ? "cam.saving" : "cam.start");
  $("rec").classList.toggle("recording", recording);
  $("cam-device").disabled = state.capture !== "preview";
  $("cam-mic").disabled = !["closed", "preview"].includes(state.capture);
  // Finish the take explicitly rather than silently discarding a recording.
  $("cam-close").disabled = ["recording", "saving"].includes(state.capture);
  setText($("cam-close"), state.capture === "countdown" ? "cam.cancelCountdown" : "cam.close");
  setText($("workflow-label"), state.running ? "workflow.running" : state.capture !== "closed" ? "workflow.webcam" : state.result ? "workflow.result" : state.videoReady ? "workflow.ready" : "workflow.source");
  setText($("sync-state"), synced() ? "transport.synced" : state.entry ? "transport.motionOnly" : "transport.idle");
  $("drop").setAttribute("aria-disabled", String(busy));
  $("opts").setAttribute("aria-busy", String(state.running));
  const local = prefs.snap?.effective["compute.mode"] === "local";
  setText($("job-help"), state.capture !== "closed" ? "job.helpCapture" : state.running ? (local ? "job.helpRunningLocal" : "job.helpRunning") : (local ? "job.helpIdleLocal" : "job.helpIdle"));
}

// Playback uses either the selected source's clock or an independent motion clock.
function synced() {
  return !!(state.entry?.video && $("sync").checked && state.videoReady && state.video?.file === state.entry.video.file);
}
function clockTime() {
  if (!state.entry?.parsed) return null;
  return synced() ? Math.min(Math.max($("video").currentTime - state.entry.video.start, 0), state.entry.parsed.duration) : state.t;
}
function playLabel(on) {
  setText($("play"), on ? "transport.pause" : "transport.play");
  setAttr($("play"), "aria-label", on ? "transport.pauseMotion" : "transport.playMotion");
}
async function setPlaying(on) {
  if (state.capture !== "closed") return;
  if (!state.entry?.parsed) return;
  if (synced()) {
    if (on) {
      if (clockTime() >= state.entry.parsed.duration - 0.02) $("video").currentTime = state.entry.video.start;
      await $("video").play();
    } else $("video").pause();
  } else {
    if (on && state.t >= state.entry.parsed.duration) state.t = 0;
    state.playing = on;
    playLabel(on);
  }
}
let last = performance.now();
function frame(now) {
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  const e = state.entry;
  if (e?.parsed) {
    if (synced()) {
      if (!$("video").paused && $("video").currentTime >= e.video.start + e.parsed.duration) {
        if ($("loop").checked) $("video").currentTime = e.video.start;
        else $("video").pause();
      }
    } else if (state.playing) {
      state.t += dt;
      if (state.t >= e.parsed.duration) {
        if ($("loop").checked) state.t %= Math.max(e.parsed.duration, 0.001);
        else { state.t = e.parsed.duration; state.playing = false; playLabel(false); }
      }
    }
  }
  const t = clockTime() ?? 0, d = e?.parsed?.duration || 0;
  $("time").textContent = `${t.toFixed(2)} / ${d.toFixed(2)} s`;
  if (!scrubbing) $("scrub").value = d ? Math.round(t / d * 1000) : 0;
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
$("play").addEventListener("click", action(() => setPlaying(synced() ? $("video").paused : !state.playing)));
$("video").addEventListener("play", () => { if (synced()) playLabel(true); });
$("video").addEventListener("pause", () => { if (synced()) playLabel(false); });
$("video").addEventListener("ended", action(async () => {
  if (synced() && $("loop").checked) { $("video").currentTime = state.entry.video.start; await $("video").play(); }
}));
$("scrub").addEventListener("input", () => {
  if (!state.entry?.parsed) return;
  scrubbing = true;
  const t = $("scrub").value / 1000 * state.entry.parsed.duration;
  if (synced()) $("video").currentTime = t + state.entry.video.start;
  else state.t = t;
});
for (const event of ["change", "blur", "pointerup", "pointercancel"]) $("scrub").addEventListener(event, () => { scrubbing = false; });
document.addEventListener("keydown", action((e) => {
  // Settings (⌘, / Ctrl+,). The app menu has the same accelerator; opening twice is harmless.
  if (e.key === "," && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) { e.preventDefault(); openSettings(); return; }
  if (isSettingsOpen()) return;
  if (e.code !== "Space" || e.repeat || e.altKey || e.ctrlKey || e.metaKey || e.target.closest("input, select, textarea, button, summary, video, [contenteditable]")) return;
  if (!state.entry?.parsed) return;
  e.preventDefault();
  return setPlaying(synced() ? $("video").paused : !state.playing);
}));

function renderMotionList() {
  const select = $("motion-select");
  const idle = new Option("", "");
  setText(idle, "transport.idle");
  select.replaceChildren(idle);
  for (const entry of state.entries) {
    const option = new Option("", entry.key);
    if (entry.generated) setText(option, "motion.generated", { name: entry.name }); else if (entry.edited) setText(option, "motion.edited", { name: entry.name }); else setRaw(option, entry.label);
    select.add(option);
  }
  select.value = state.entry?.key || "";
  updateControls();
}
async function selectMotion(key) {
  const request = ++selectionRequest;
  $("video").pause(); state.playing = false; playLabel(false);
  const entry = state.entries.find((e) => e.key === key) || null;
  if (entry && !entry.parsed) {
    const response = await fetch(entry.url);
    if (!response.ok) throw new Error(t("motion.readFailed", { status: response.status }));
    entry.parsed = parseMotion(await response.json());
  }
  if (request !== selectionRequest) return;
  state.entry = entry; state.t = 0;
  viewer.motion = entry?.parsed || null;
  if (entry?.video && $("sync").checked && state.video?.file !== entry.video.file) await loadVideo(entry.video.file, { keepSettings: true });
  if (request !== selectionRequest) return;
  if (synced()) $("video").currentTime = entry.video.start;
  renderMotionList();
}
$("motion-select").addEventListener("change", action((e) => selectMotion(e.target.value)));
$("sync").addEventListener("change", action(() => selectMotion(state.entry?.key || "")));
function setModelMotions(list) {
  const selected = state.entry?.key;
  state.entries = [...state.entries.filter((e) => !e.key.startsWith("model:")), ...list.map((m) => ({ key: `model:${m.file}`, label: m.name, ...m }))];
  if (state.entry && !state.entries.some((e) => e.key === selected)) { state.entry = null; viewer.motion = null; }
  renderMotionList();
}

async function openModel(dir) {
  if (locked()) return;
  state.loading = true; updateControls();
  ++selectionRequest;
  setText($("stage-hint"), "stage.loadingFirst");
  $("stage-hint").hidden = false;
  let replacing = false;
  try {
    const model = await api.openModel(dir, $("tex4k").checked ? 4096 : 0);
    if (!model) { $("stage-hint").hidden = !!state.model; return; }
    replacing = true;
    $("video").pause(); state.playing = false; playLabel(false);
    const info = await viewer.load(model.settingsUrl);
    viewer.motion = null; viewer.setPhysics($("physics").checked);
    state.entries = state.model?.dir === model.dir ? state.entries.filter((e) => !e.key.startsWith("model:")) : [];
    if (state.model?.dir !== model.dir) { state.result = null; $("result").classList.add("hidden"); }
    state.model = model; state.entry = null; state.t = 0;
    setRaw($("model-name"), model.name);
    setText($("model-info"), "model.parameters", { count: info.parameters });
    $("model-info").title = model.dir;
    $("stage-hint").hidden = true;
    setModelMotions(model.motions);
    readyMessage();
  } catch (err) {
    if (replacing) { state.model = null; state.entry = null; state.entries = []; viewer.motion = null; renderMotionList(); }
    setText($("stage-hint"), "stage.loadFailed", { error: errorMessage(err) });
    fail(err, "status.modelFailed");
  } finally { state.loading = false; updateControls(); }
}
$("open-model").addEventListener("click", () => openModel(null));
$("tex4k").addEventListener("change", () => state.model && openModel(state.model.dir));
$("physics").addEventListener("change", (e) => viewer.setPhysics(e.target.checked));
function selectFrame(frame) {
  for (const button of $("frame").querySelectorAll("button")) {
    const selected = button.dataset.frame === frame;
    button.classList.toggle("on", selected); button.setAttribute("aria-pressed", String(selected));
  }
  viewer.setFrame(frame);
}
$("frame").addEventListener("click", (e) => { if (e.target.dataset.frame) selectFrame(e.target.dataset.frame); });
$("reset-view").addEventListener("click", () => viewer.setFrame($("frame").querySelector(".on").dataset.frame));

function defaultName() {
  const stem = state.video?.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 36) || "take";
  return `v2l_${stem}_${$("style").value}_${Date.now().toString(36)}`;
}
async function loadVideo(file, { keepSettings = false } = {}) {
  const video = await api.openVideo(file);
  if (!video) return false;
  state.video = video; state.videoReady = false;
  $("video").src = video.url;
  $("drop").classList.add("has-video");
  $("source-name").textContent = video.name; $("source-name").title = video.file;
  $("source-name").classList.remove("hidden");
  setText($("source-meta"), "source.loading");
  if (!keepSettings) {
    $("name").value = state.autoName = defaultName();
    for (const id of ["start", "end", "neutral"]) { $(id).value = ""; $(id).removeAttribute("aria-invalid"); }
    $("name").removeAttribute("aria-invalid"); $("flip").checked = benchDefault("input.flip");
    $("neutral").value = neutralText(benchDefault("calibration.neutral_window"));
  }
  updateControls();
  return true;
}
$("video").addEventListener("loadedmetadata", () => {
  const video = $("video");
  state.videoReady = Number.isFinite(video.duration) && video.duration > 0;
  if (state.videoReady) setText($("source-meta"), "source.meta", videoMeta()); else setText($("source-meta"), "source.noDuration");
  if (synced()) video.currentTime = state.entry.video.start;
  if (!state.running && state.capture === "closed") readyMessage();
  updateControls();
});
$("video").addEventListener("error", () => {
  state.videoReady = false; updateControls();
  setText($("source-meta"), "source.unplayable");
  status("video.openFailed", null, "error");
});
$("open-video").addEventListener("click", action(async () => {
  if (locked()) return;
  state.loading = true; updateControls();
  try { await loadVideo(null); } finally { state.loading = false; updateControls(); }
}));
$("style").addEventListener("change", () => {
  if (state.video && $("name").value === state.autoName) $("name").value = state.autoName = defaultName();
  setText($("style-help"), $("style").value === "cute" ? "settings.styleCuteHelp" : "settings.styleFaithfulHelp");
});
for (const event of ["dragover", "drop"]) document.addEventListener(event, (e) => e.preventDefault());
$("drop").addEventListener("dragover", (e) => { e.preventDefault(); if (!locked()) $("drop").classList.add("over"); });
$("drop").addEventListener("dragleave", () => $("drop").classList.remove("over"));
$("drop").addEventListener("drop", action(async (e) => {
  e.preventDefault(); $("drop").classList.remove("over");
  if (locked()) return;
  const file = e.dataTransfer.files[0];
  if (!file) return;
  if (!/\.(mp4|mov|m4v|webm|mkv)$/i.test(file.name)) { status("drop.wrongType", null, "error"); return; }
  state.loading = true; updateControls();
  try { await loadVideo(api.pathForFile(file)); } finally { state.loading = false; updateControls(); }
}));

// A take owns its recorder, buffers and timers. Late callbacks never mutate a newer take.
function cue(key, params) {
  if (key) setText($("cam-cue"), key, params); else setRaw($("cam-cue"), "");
  $("cam-cue").classList.toggle("hidden", !key);
}
function videoMeta() {
  return { seconds: $("video").duration.toFixed(1), width: $("video").videoWidth, height: $("video").videoHeight };
}
function releaseCamera() {
  ++cameraRequest;
  if (cam) {
    clearInterval(cam.timer);
    cam.recorder?.state === "recording" && cam.recorder.stop();
    for (const track of cam.stream.getTracks()) track.stop();
  }
  cam = null;
  $("cam").srcObject = null;
  $("cam-panel").classList.add("hidden"); $("camera-controls").classList.add("hidden");
  $("drop").classList.remove("cam-on"); cue(null);
  $("source-name").classList.toggle("hidden", !state.video);
  if (state.videoReady) setText($("source-meta"), "source.meta", videoMeta()); else setText($("source-meta"), "source.none");
}
async function openCamera() {
  if (state.running || state.loading || !["closed", "preview"].includes(state.capture)) return;
  releaseCamera();
  const request = ++cameraRequest;
  state.capture = "opening"; updateControls(); status("cam.connecting", null, "busy");
  try {
    if (!await api.cameraAccess($("cam-mic").checked)) throw new Error(t("cam.permission"));
    if (request !== cameraRequest) return;
    // The default camera is stored by name: device ids change with this window's origin.
    let deviceId = $("cam-device").value;
    const preferred = prefs.app("camera.device");
    if (!deviceId && preferred) {
      deviceId = (await navigator.mediaDevices.enumerateDevices()).find((d) => d.kind === "videoinput" && d.label === preferred)?.deviceId || "";
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { deviceId: deviceId ? { exact: deviceId } : undefined, width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } },
      audio: $("cam-mic").checked,
    });
    if (request !== cameraRequest) { stream.getTracks().forEach((t) => t.stop()); return; }
    cam = { stream, chunks: [] };
    $("cam").srcObject = stream; $("video").pause();
    state.playing = false; playLabel(false);
    $("cam-panel").classList.remove("hidden"); $("camera-controls").classList.remove("hidden"); $("drop").classList.add("cam-on");
    $("source-name").classList.add("hidden");
    setText($("source-meta"), "source.webcam");
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    if (request !== cameraRequest) return;
    $("cam-device").replaceChildren(...devices.map((d, i) => {
      const option = new Option("", d.deviceId);
      if (d.label) setRaw(option, d.label); else setText(option, "cam.deviceN", { n: i + 1 });
      return option;
    }));
    const settings = stream.getVideoTracks()[0].getSettings();
    $("cam-device").value = settings.deviceId || "";
    setRaw($("rec-time"), `${settings.width} × ${settings.height}`);
    state.capture = "preview"; status("cam.ready"); updateControls();
  } catch (err) {
    if (request !== cameraRequest) return;
    releaseCamera(); state.capture = "closed"; updateControls(); fail(err, "cam.failed");
  }
}
async function startRecording() {
  if (!cam || state.capture !== "preview") return;
  const take = cam;
  state.capture = "countdown"; updateControls();
  status("rec.countdown", null, "busy");
  try {
    for (const n of [3, 2, 1]) {
      cue("rec.countdownNumber", { n }); await new Promise((r) => setTimeout(r, 1000));
      if (cam !== take || state.capture !== "countdown") return;
    }
    const mimeType = ["video/mp4;codecs=avc1,mp4a.40.2", "video/mp4", "video/webm;codecs=vp9,opus", "video/webm"].find((type) => MediaRecorder.isTypeSupported(type));
    if (!mimeType) throw new Error(t("rec.unsupported"));
    take.recorder = new MediaRecorder(take.stream, { mimeType, videoBitsPerSecond: 8_000_000 });
    take.recorder.ondataavailable = (event) => { if (event.data.size) take.chunks.push(event.data); };
    take.recorder.onerror = (event) => {
      if (cam !== take) return;
      releaseCamera(); state.capture = "closed"; updateControls(); fail(event.error || new Error(t("rec.deviceLost")), "rec.failed");
    };
    take.recorder.start(500); take.t0 = performance.now(); state.capture = "recording";
    cue("rec.cueNeutral"); updateControls(); status("rec.recording", null, "busy");
    take.timer = setInterval(() => {
      if (cam !== take || state.capture !== "recording") return;
      const seconds = (performance.now() - take.t0) / 1000;
      setText($("rec-time"), "rec.time", { seconds: seconds.toFixed(1), max: MAX_RECORDING_SECONDS });
      if (seconds > NEUTRAL.to + 0.2) cue(seconds < 3 ? "rec.cueMove" : null);
      if (seconds >= MAX_RECORDING_SECONDS) stopRecording().catch((err) => fail(err));
    }, 100);
  } catch (err) {
    if (cam !== take) return;
    releaseCamera(); state.capture = "closed"; updateControls(); fail(err, "rec.startFailed");
  }
}
async function stopRecording() {
  if (!cam || state.capture !== "recording") return;
  const take = cam;
  state.capture = "saving"; clearInterval(take.timer); updateControls(); status("rec.saving", null, "busy");
  try {
    const seconds = (performance.now() - take.t0) / 1000;
    await new Promise((resolve, reject) => {
      take.recorder.onstop = resolve; take.recorder.onerror = (event) => reject(event.error || new Error(t("rec.saveFailed"))); take.recorder.stop();
    });
    const blob = new Blob(take.chunks, { type: take.recorder.mimeType });
    releaseCamera();
    if (seconds < MIN_RECORDING_SECONDS) throw new Error(t("rec.tooShort", { min: MIN_RECORDING_SECONDS }));
    const saved = await api.saveRecording(await blob.arrayBuffer(), take.recorder.mimeType.includes("mp4") ? "mp4" : "webm");
    await loadVideo(saved.file);
    $("neutral").value = `${NEUTRAL.from}:${NEUTRAL.to}`;
    status("rec.saved", { seconds: seconds.toFixed(1) }, "success");
  } catch (err) { releaseCamera(); fail(err, "rec.saveFailed"); }
  finally { state.capture = "closed"; updateControls(); }
}
$("open-cam").addEventListener("click", openCamera);
$("cam-close").addEventListener("click", () => {
  if (["recording", "saving"].includes(state.capture)) return;
  releaseCamera(); state.capture = "closed"; readyMessage(); updateControls();
});
for (const id of ["cam-device", "cam-mic"]) $(id).addEventListener("change", openCamera);
$("rec").addEventListener("click", action(() => state.capture === "recording" ? stopRecording() : startRecording()));
window.addEventListener("pagehide", releaseCamera);
window.addEventListener("beforeunload", (event) => {
  if (state.running || ["countdown", "recording", "saving"].includes(state.capture) || editor.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

function invalid(id, key) {
  if ($("advanced").contains($(id))) $("advanced").open = true;
  $(id).setAttribute("aria-invalid", "true"); $(id).focus();
  status(key, null, "error");
  return false;
}
function validate() {
  for (const id of ["name", "start", "end", "neutral"]) $(id).removeAttribute("aria-invalid");
  if (!/^[A-Za-z0-9_-]{1,80}$/.test($("name").value.trim())) return invalid("name", "validate.name");
  const duration = $("video").duration;
  const start = $("start").value === "" ? 0 : Number($("start").value);
  const end = $("end").value === "" ? duration : Number($("end").value);
  if ($("start").validity.badInput || !Number.isFinite(start) || start < 0 || start >= duration) return invalid("start", "validate.start");
  if ($("end").validity.badInput || !Number.isFinite(end) || end <= start || end > duration + 0.01) return invalid("end", "validate.end");
  if ($("neutral").value.trim()) {
    const match = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec($("neutral").value.trim());
    if (!match || Number(match[2]) <= Number(match[1]) || Number(match[2]) > end - start) return invalid("neutral", "validate.neutral");
  }
  return true;
}
function log(line) {
  const el = $("log");
  const follow = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  state.logs.push(line);
  if (state.logs.length > 1000) state.logs.shift();
  setRaw(el, state.logs.join("\n")); $("log-count").textContent = String(state.logs.length);
  if (follow) el.scrollTop = el.scrollHeight;
}
function updateJobTime() {
  if (!state.running || !state.run) return;
  const seconds = Math.floor((performance.now() - state.run.started) / 1000);
  status("job.elapsed", { stage: msg(state.run.stage), time: `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}` }, "busy");
}

// ---------------------------------------------------------------- bench values

function benchDefault(key) { return prefs.defaultFor(key, $("preset").value || prefs.app("generation.preset")); }
function neutralText(v) { return Array.isArray(v) ? `${v[0]}:${v[1]}` : ""; }
function styleHelp() { setText($("style-help"), $("style").value === "cute" ? "settings.styleCuteHelp" : "settings.styleFaithfulHelp"); }
function setBenchField(key, value) {
  const el = $(BENCH_DEFAULTS[key]);
  if (el.type === "checkbox") el.checked = !!value; else el.value = value;
  if (key === "style.name") { styleHelp(); if (state.video && $("name").value === state.autoName) $("name").value = state.autoName = defaultName(); }
}
function fillBench(keys = Object.keys(BENCH_DEFAULTS)) {
  for (const key of keys) setBenchField(key, benchDefault(key));
}
function renderPresetSelect() {
  const select = $("preset");
  const current = select.value || prefs.app("generation.preset");
  select.replaceChildren(...prefs.presets().map((p) => {
    const option = new Option("", p.name);
    setText(option, "bench.presetOption", { name: presetTitle(p) });
    return option;
  }));
  select.value = prefs.preset(current) ? current : "default";
  renderTuningSummary();
}
function renderTuningSummary() {
  const count = prefs.tunedCount() - (prefs.snap.config.preview ? Object.keys(prefs.snap.config.preview).length : 0);
  setText($("tuning-summary"), count ? "bench.tuned" : "bench.untuned", { count });
}
function perRun() {
  const neutral = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec($("neutral").value.trim());
  return {
    "style.name": $("style").value, "output.loop": $("opt-loop").checked, "output.interpolation": $("interp").value, "input.flip": $("flip").checked,
    "expressions.enabled": $("rare").checked,
    "input.start": $("start").value === "" ? null : Number($("start").value),
    "input.end": $("end").value === "" ? null : Number($("end").value),
    "calibration.neutral_window": neutral ? [Number(neutral[1]), Number(neutral[2])] : null,
  };
}
function runOptions(extra = {}) {
  return { video: state.video?.file, modelDir: state.model?.dir, name: $("name").value.trim(), preset: $("preset").value, perRun: perRun(), ...extra };
}
async function benchCommand() {
  if (!state.video || !state.model) return null;
  return api.cliCommand(runOptions());
}
function benchNote(key, params) { setText($("bench-note"), key, params); }
$("preset").addEventListener("change", () => { fillBench(); benchNote("bench.presetChanged"); });
$("save-defaults").addEventListener("click", action(async () => {
  $("bench-menu").open = false;
  // Stored only where the bench differs from what the chosen preset alone would give.
  const preset = $("preset").value;
  const config = {};
  for (const [key, id] of Object.entries(BENCH_DEFAULTS)) {
    const value = $(id).type === "checkbox" ? $(id).checked : $(id).value;
    const [sid, name] = key.split(".");
    const base = prefs.preset(preset)?.settings?.[sid]?.[name] ?? prefs.configDefault(key);
    config[key] = same(value, base) ? null : value;
  }
  await prefs.set({ app: { "generation.preset": preset }, config });
  benchNote("bench.savedDefault");
}));
$("reset-bench").addEventListener("click", () => {
  $("bench-menu").open = false;
  $("preset").value = prefs.app("generation.preset");
  fillBench();
  $("neutral").value = neutralText(benchDefault("calibration.neutral_window"));
  benchNote("bench.resetDone");
});
$("copy-cli").addEventListener("click", action(async () => {
  $("bench-menu").open = false;
  const command = await api.cliCommand(runOptions());
  await api.copy(command);
  benchNote("bench.copied");
}));
$("open-tuning").addEventListener("click", () => { $("bench-menu").open = false; openSettings("tuning"); });
$("bench-menu").addEventListener("keydown", (e) => { if (e.key === "Escape") { $("bench-menu").open = false; $("bench-menu").querySelector("summary").focus(); } });
document.addEventListener("click", (e) => { if (!$("bench-menu").contains(e.target)) $("bench-menu").open = false; });

// ---------------------------------------------------------------- runs

function stageList(mode, preview) {
  return [...(mode === "remote" ? ["upload"] : []), ...STAGES.filter((s) => s !== "preview" || preview), ...(mode === "remote" ? ["download"] : [])];
}
function renderStages() {
  const run = state.run || state.lastRun;
  if (!run) return;
  $("stages").replaceChildren(...run.stages.map((name) => {
    const li = document.createElement("li");
    const s = run.status[name] || "pending";
    li.className = `stage ${s}`;
    const label = document.createElement("span");
    setText(label, `stage.${name}`);
    const mark = document.createElement("span");
    mark.className = "stage-state";
    setText(mark, `stage.state.${s}`);
    li.append(label, mark);
    if (s === "active") li.setAttribute("aria-current", "step");
    return li;
  }));
  const p = run.progress;
  $("frames").classList.toggle("hidden", !p);
  if (p) {
    $("frame-bar").max = Math.max(p.total, 1); $("frame-bar").value = p.done;
    setText($("frame-text"), "progress.frameCount", { stage: msg(`stage.${p.stage}`), done: p.done, total: p.total });
  }
}
function onEvent(event) {
  const run = state.run;
  if (!run) return;
  if (event.event === "start") { run.mode = event.mode; setText($("progress-mode"), event.mode === "remote" ? "progress.remote" : "progress.local"); return; }
  if (event.event === "stage" && event.stage) {
    if (!run.stages.includes(event.stage)) run.stages.push(event.stage);
    for (const name of run.stages) {
      if (name === event.stage) break;
      run.status[name] = run.status[name] === "active" || !run.status[name] ? "done" : run.status[name];
    }
    run.status[event.stage] = "active";
    run.stage = `stage.${event.stage}`;
    if (run.progress && run.progress.stage !== event.stage) run.progress = null;
  } else if (event.event === "progress" && Number.isFinite(event.total)) {
    run.progress = { stage: event.stage, done: event.done, total: event.total };
  } else return;
  renderStages(); updateJobTime();
}
function showRunError(key, params, computeLink) {
  setText($("run-error-text"), key, params);
  $("run-error").classList.remove("hidden");
  $("run-error-action").classList.toggle("hidden", !computeLink);
}
// extra.reuse: {configFile, video, modelDir, start} of an earlier result ("Apply mapping"): the same
// source and configuration, so the compute side reuses its tracking.
async function startRun(extra = {}) {
  const reuse = extra.reuse;
  if (locked() || !state.model || state.server !== true) return;
  if (!reuse && (!state.videoReady || !validate())) return;
  const opts = reuse ? { video: reuse.source.file, modelDir: reuse.modelDir, name: extra.name, suffix: true, reuseConfig: reuse.configFile,
                         preview: !!extra.preview, mapping: extra.mapping } : runOptions(extra);
  const preview = prefs.defaultFor("preview.enabled", opts.preset);
  const mode = prefs.app("compute.mode");
  // Freeze ownership before IPC; completion may arrive before invoke resolves.
  state.run = { source: reuse ? reuse.source : state.video, modelDir: state.model.dir, start: reuse ? reuse.start : Number($("start").value) || 0, started: performance.now(),
                stage: "stage.preparing", stages: stageList(mode, preview), status: {}, progress: null, mode };
  state.lastRun = null;
  state.running = true; state.logs = []; setRaw($("log"), ""); $("log-count").textContent = "0";
  $("run-error").classList.add("hidden");
  $("progress").classList.remove("hidden");
  setText($("progress-mode"), mode === "remote" ? "progress.remote" : "progress.local");
  renderStages();
  $("progress").scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  $("video").pause(); state.playing = false; playLabel(false);
  updateControls(); updateJobTime(); state.jobTimer = setInterval(updateJobTime, 1000);
  try {
    const { name } = await api.run(opts);
    if (name !== opts.name && state.run && !reuse) { $("name").value = name; benchNote("bench.renamed", { name }); }
  } catch (err) {
    clearInterval(state.jobTimer); state.running = false; state.run = null; $("progress").classList.add("hidden"); updateControls(); fail(err, "status.runFailed");
  }
}
$("opts").addEventListener("submit", (e) => e.preventDefault());
$("run").addEventListener("click", action(() => startRun()));
$("run-again").addEventListener("click", action(() => {
  if (!state.result) return;
  return startRun({ name: state.result.name, suffix: true });
}));

// ---------------------------------------------------------------- mapping

function canApplyMapping({ ignoreEditor = false } = {}) {
  const r = state.result;
  return !!(r?.configFile && r.motion && state.model?.dir === r.modelDir && state.server === true
    && !state.running && !state.loading && state.capture === "closed" && (ignoreEditor || !editor.open));
}
// "Apply mapping to this result": the same source and configuration with the model's current
// mapping overrides, under an auto-suffixed name.
async function applyMapping() {
  if (!canApplyMapping()) return;
  const r = state.result;
  await startRun({ name: `${baseName(r.name)}_map`.slice(0, 80), reuse: reuseOf(r) });
}
// "Preview changes" in the mapping editor: the same source and configuration with the draft
// mapping, into a temporary result that plays right away; Keep makes it a normal result.
async function previewMapping(draft) {
  if (!canApplyMapping()) return;
  const r = state.result;
  await startRun({ name: `${baseName(r.name)}_preview`.slice(0, 80), reuse: reuseOf(r), preview: true, mapping: draft });
}
function baseName(name) { return name.replace(/_(map|preview)(_\d+)?$/, ""); }
function reuseOf(r) { return { configFile: r.configFile, source: r.source, modelDir: r.modelDir, start: r.start || 0 }; }
$("open-mapping").addEventListener("click", action(() => state.model && openMapping(state.model)));
$("apply-mapping").addEventListener("click", action(() => applyMapping()));
$("preview-keep").addEventListener("click", action(async () => {
  const r = state.result;
  if (!r?.preview || locked()) return;
  const kept = await api.keepPreview(r.dir);
  const oldKey = `gen:${r.motion.file}`;
  const entry = state.entries.find((e) => e.key === oldKey);
  state.result = { ...r, dir: kept.dir, motion: kept.motion, signals: kept.signals, preview: false };
  if (entry) Object.assign(entry, { key: `gen:${kept.motion.file}`, ...kept.motion });
  if (state.entry === entry) renderMotionList();
  state.previousResult = null;
  showResultHeader();
  status("status.previewKept", { name: r.name }, "success");
}));
$("preview-discard").addEventListener("click", action(async () => {
  const r = state.result;
  if (!r?.preview || locked()) return;
  await api.discardPreview(r.dir);
  const key = `gen:${r.motion.file}`;
  state.entries = state.entries.filter((e) => e.key !== key);
  const prev = state.previousResult;
  state.result = prev; state.previousResult = null;
  if (prev) {
    showReport(prev.report); showResultHeader();
    await selectMotion(`gen:${prev.motion.file}`);
  } else { $("result").classList.add("hidden"); await selectMotion(""); }
  updateControls();
  status("status.previewDiscarded", null, "idle");
}));
function showResultHeader() {
  const preview = !!state.result?.preview;
  setRaw($("result-name"), state.result?.name || "");
  setText($("result-badge"), preview ? "result.previewBadge" : "result.done");
  $("result-badge").classList.toggle("preview", preview);
  $("preview-bar").classList.toggle("hidden", !preview);
  for (const id of ["install", "apply-mapping", "use-settings", "run-again"]) $(id).closest(".result-actions").classList.toggle("hidden", preview && id !== "install");
  $("install").classList.toggle("hidden", preview);
}

// ---------------------------------------------------------------- motion editor

function inModelFolder(file) { return !!state.model && file.startsWith(`${state.model.dir}/`); }
async function openEditor(entry) {
  if (!entry?.file || locked()) return;
  if (state.entry?.key !== entry.key) await selectMotion(entry.key);
  if (state.entry?.key !== entry.key) return;
  $("video").pause(); state.playing = false; playLabel(false);
  state.loading = true; updateControls();
  try {
    await editor.openMotion({ file: entry.file, name: entry.name, entryKey: entry.key, video: synced() ? entry.video : null,
                              inModel: inModelFolder(entry.file) });
  } finally { state.loading = false; updateControls(); }
  $("graph").focus({ preventScroll: true });
}
// Saved edits become a playable entry; a copy in the model folder refreshes its motion list.
async function editorSaved(saved, previous) {
  if (inModelFolder(saved.file)) setModelMotions(await api.listMotions(state.model.dir));
  if (saved.mode === "model") return;
  const old = state.entries.find((e) => e.key === previous.entryKey);
  const key = inModelFolder(saved.file) ? `model:${saved.file}` : `edit:${saved.file}`;
  let entry = state.entries.find((e) => e.key === key);
  if (!entry) {
    entry = { key, name: saved.name, label: saved.name, file: saved.file, url: saved.url, edited: true, video: old?.video || null };
    state.entries = [entry, ...state.entries];
  }
  entry.url = saved.url || entry.url;
  entry.parsed = state.entry?.parsed || null;
  if (old && old !== entry && saved.mode === "new") old.parsed = null;  // re-read the untouched original next time
  state.entry = entry;
  editor.source.entryKey = key;
  renderMotionList();
}
// Closing drops the preview; the motion is read again from its file.
async function editorClosed() {
  const key = state.entry?.key || "";
  if (state.entry) state.entry.parsed = null;
  updateControls();
  await selectMotion(key).catch((err) => fail(err));
}
$("edit-motion").addEventListener("click", action(() => openEditor(state.entry)));
$("result-edit").addEventListener("click", action(() => state.result && openEditor(state.entries.find((e) => e.key === `gen:${state.result.motion.file}`))));
$("ed-defaults").addEventListener("click", () => openSettings("generation", "output.interpolation"));
$("run-error-action").addEventListener("click", () => openSettings("compute"));
$("cancel").addEventListener("click", action(async () => {
  if (!state.running || state.run?.cancelling) return;
  const job = state.run; job.cancelling = true; updateControls();
  try { await api.cancel(); }
  finally { if (state.run === job) { job.cancelling = false; updateControls(); } }
}));
api.onLog(log);
api.onEvent(onEvent);
api.onDone(action(async (result) => {
  const job = state.run;
  if (!job) return;
  clearInterval(state.jobTimer); state.running = false; state.run = null; state.lastRun = job;
  const ok = result.code === 0 && result.motion;
  for (const name of job.stages) {
    if (ok) job.status[name] = job.status[name] === "active" || !job.status[name] ? "done" : job.status[name];
    else if (job.status[name] === "active") job.status[name] = "failed";
  }
  if (ok) { job.progress = null; }
  renderStages();
  if (!ok) {
    updateControls(); $("log-panel").open = true;
    const detail = result.error?.message || "";
    if (result.cancelled || result.code === null || result.code === 130) { status("status.cancelled"); showRunError("run.cancelled"); }
    else if (result.code === 3) { status("run.noFaceStatus", null, "error"); showRunError("run.noFace", { detail }); }
    else if (result.code === 4) {
      status("run.unreachableStatus", null, "error"); showRunError("run.unreachable", { detail }, true);
      state.server = false; runCheck().catch(() => {});
    } else if (result.code === 2) { status("run.invalidStatus", null, "error"); showRunError("run.invalid", { detail }); }
    else { status("status.generationFailed", { code: result.code }, "error"); if (detail) showRunError("run.failed", { detail }); }
    return;
  }
  // A preview stands in for the result it came from until it is kept or discarded; a newer
  // preview replaces an older one, which is deleted.
  const superseded = result.preview && state.result?.preview ? state.result : null;
  if (result.preview) state.previousResult = superseded ? state.previousResult : state.result;
  else if (state.result?.preview) state.previousResult = null;
  if (superseded) {
    state.entries = state.entries.filter((e) => e.key !== `gen:${superseded.motion.file}`);
    api.discardPreview(superseded.dir).catch(() => {});
  }
  state.result = { ...result, modelDir: job.modelDir, source: job.source, start: job.start };
  const entry = { key: `gen:${result.motion.file}`, ...result.motion, generated: true, video: { file: job.source.file, start: job.start } };
  state.entries = [entry, ...state.entries.filter((e) => e.key !== entry.key)];
  showReport(result.report);
  showResultHeader();
  if (result.installed) { setText($("install-status"), "result.installed"); $("install-status").title = result.installed; setModelMotions(await api.listMotions(job.modelDir)); }
  else setText($("install-status"), "result.installHint");
  $("result").classList.remove("hidden"); $("log-panel").open = false;
  await selectMotion(entry.key);
  updateControls();
  status("status.done", null, "success");
  $("result").scrollIntoView({ block: "nearest" });
  try { await setPlaying(true); } catch { status("status.donePressPlay", null, "success"); }
}));
// Loads the configuration recorded in the result into the bench (per-run values) and Tuning.
$("use-settings").addEventListener("click", action(async () => {
  const config = state.result?.report?.config;
  if (!config) return;
  const perRunKeys = new Set([...Object.keys(BENCH_DEFAULTS), "input.start", "input.end", "calibration.neutral_window"]);
  const patch = {};
  for (const f of prefs.fields()) {
    if (perRunKeys.has(f.key) || f.section === "preview" || f.section === "input" && !["analysis_width"].includes(f.name)) continue;
    const value = config[f.section]?.[f.name];
    if (value === undefined) continue;
    patch[f.key] = same(value, prefs.configDefault(f.key)) ? null : value;
  }
  await prefs.set({ app: { "generation.preset": "default" }, config: patch });
  $("preset").value = "default";
  for (const [key, id] of Object.entries(BENCH_DEFAULTS)) {
    const [sid, name] = key.split(".");
    if (config[sid]?.[name] !== undefined) setBenchField(key, config[sid][name]);
  }
  $("start").value = config.input?.start ?? ""; $("end").value = config.input?.end ?? "";
  $("neutral").value = neutralText(config.calibration?.neutral_window);
  benchNote("bench.loadedResult", { name: state.result.name });
  status("status.loadedSettings", { name: state.result.name }, "success");
}));
function showReport(report) {
  const box = $("result-summary"); box.replaceChildren();
  if (!report) { const p = document.createElement("p"); setText(p, "result.noReport"); box.append(p); return; }
  const format = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : "—";
  const blinks = Object.values(report.blinks || {});
  // Each value is a raw string or a [key, params] message.
  const mappingChanges = (m) => (m ? Object.keys(m.input_curves || {}).length + (m.bridge || []).length + (m.entries || []).length : 0);
  const rows = [
    ["report.face", `${format(report.face_rate * 100)}%`],
    ["report.blinks", blinks.length ? ["report.blinkCount", { count: Math.max(...blinks) }] : "—"],
    ["report.rare", report.rare_seconds ? ["report.rareSeconds", { cheek: format(report.rare_seconds.CheekPuff), tongue: format(report.rare_seconds.TongueOut) }] : ["report.off"]],
    ["report.neutral", report.neutral_window_s ? ["report.neutralRange", { from: format(report.neutral_window_s[0], 2), to: format(report.neutral_window_s[1], 2) }] : "—"],
    ["report.curves", report.curves != null ? ["report.curveCount", { count: report.curves }] : "—"],
    ["report.mappingChanges", mappingChanges(report.mapping_overrides) ? ["report.mappingChangeCount", { count: mappingChanges(report.mapping_overrides) }] : ["report.mappingBase"]],
  ];
  const dl = document.createElement("dl");
  for (const [key, value] of rows) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    setText(dt, key);
    if (Array.isArray(value)) setText(dd, value[0], value[1]); else setRaw(dd, value);
    dl.append(dt, dd);
  }
  box.append(dl);
  for (const warning of report.warnings || []) { const p = document.createElement("p"); p.className = "warn"; p.textContent = warning; box.append(p); }
  if (report.mapping_notes?.length) {
    const details = document.createElement("details"), summary = document.createElement("summary"), ul = document.createElement("ul");
    details.className = "report-details"; setText(summary, "report.mapping", { count: report.mapping_notes.length });
    for (const note of report.mapping_notes) { const li = document.createElement("li"); li.textContent = note; ul.append(li); }
    details.append(summary, ul); box.append(details);
  }
}
$("install").addEventListener("click", action(async () => {
  if (locked() || !state.result || state.result.modelDir !== state.model?.dir) return;
  state.loading = true; updateControls();
  try {
    const dest = await api.install(state.result.motion.file, state.result.modelDir);
    if (!dest) { setText($("install-status"), "result.installCancelled"); return; }
    setText($("install-status"), "result.installed"); $("install-status").title = dest;
    setModelMotions(await api.listMotions(state.result.modelDir));
    status("status.installed", null, "success");
  } finally { state.loading = false; updateControls(); }
}));
$("compare").addEventListener("click", action(async () => {
  if (locked() || !state.result) return;
  state.loading = true; updateControls();
  try {
    $("sync").checked = true;
    await selectMotion(`gen:${state.result.motion.file}`);
    $("drop").scrollIntoView({ block: "center" });
    status("status.compare");
  } finally { state.loading = false; updateControls(); }
}));
$("reveal").addEventListener("click", action(() => state.result && api.reveal(state.result.motion.file)));

// ---------------------------------------------------------------- compute status

function applyCheck(res) {
  state.compute = res;
  const mode = res.mode;
  state.server = !!res.ok && (mode !== "remote" || res.installed !== false);
  $("server").disabled = false;
  let key;
  if (res.configured === false) key = mode === "local" ? "server.localMissing" : "server.notConfigured";
  else if (mode === "local") key = res.ok ? "server.localReady" : "server.localFailed";
  else if (res.ok && res.installed === false) key = "server.notInstalled";
  else key = res.ok ? "server.connected" : "server.offline";
  $("server").className = `connection ${state.server ? "ok" : "bad"}`;
  setText($("server"), key);
  setAttr($("server"), "title", state.server ? "server.recheckTitle" : "server.openComputeTitle");
  updateControls();
  if (!locked() && !state.result) readyMessage();
}
async function checkServer() {
  state.server = null;
  $("server").disabled = true; $("server").className = "connection"; setText($("server"), "server.checking");
  updateControls();
  if (!locked() && !state.result) readyMessage();
  await runCheck();
}
// Connected: check again. Not configured or unreachable: open Settings → Compute.
$("server").addEventListener("click", action(() => (state.server === true ? checkServer() : openSettings("compute"))));
$("open-settings").addEventListener("click", () => openSettings());

// ---------------------------------------------------------------- settings

function applyViewerDefaults() {
  $("physics").checked = prefs.app("viewer.physics"); viewer.setPhysics($("physics").checked);
  $("tex4k").checked = prefs.app("viewer.tex4k");
  $("loop").checked = prefs.app("viewer.loop");
  $("sync").checked = prefs.app("viewer.sync");
  selectFrame(prefs.app("viewer.frame"));
}
async function cameras(requestAccess) {
  let list = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  if (requestAccess && !list.some((d) => d.label) && state.capture === "closed" && await api.cameraAccess(false)) {
    const stream = await navigator.mediaDevices.getUserMedia({ video: true });
    stream.getTracks().forEach((track) => track.stop());
    list = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  }
  return list.map((d) => ({ label: d.label }));
}
prefs.on((changed) => {
  const app = changed.all ? null : changed.app || [];
  const config = changed.all ? null : changed.config || [];
  const touched = (keys, test) => keys === null || keys.some(test);
  if (touched(app, (k) => k === "general.language")) { setLanguage(prefs.snap.language); refreshMapping(); }
  if (touched(app, (k) => k.startsWith("viewer."))) {
    if (app === null || app.includes("viewer.physics")) { $("physics").checked = prefs.app("viewer.physics"); viewer.setPhysics($("physics").checked); }
    if (app === null || app.includes("viewer.loop")) $("loop").checked = prefs.app("viewer.loop");
    if (app === null || app.includes("viewer.sync")) { $("sync").checked = prefs.app("viewer.sync"); selectMotion(state.entry?.key || "").catch(() => {}); }
    if (app === null || app.includes("viewer.frame")) selectFrame(prefs.app("viewer.frame"));
    if ((app === null || app.includes("viewer.tex4k")) && $("tex4k").checked !== prefs.app("viewer.tex4k")) {
      $("tex4k").checked = prefs.app("viewer.tex4k");
      if (state.model && !locked()) openModel(state.model.dir);
    }
  }
  if (touched(app, (k) => k === "camera.audio") && state.capture === "closed") $("cam-mic").checked = prefs.app("camera.audio");
  if (touched(app, (k) => k === "generation.preset")) { $("preset").value = prefs.app("generation.preset"); fillBench(); }
  renderPresetSelect();
  if (config !== null) { const keys = config.filter((k) => k in BENCH_DEFAULTS); if (keys.length) fillBench(keys); }
  if (touched(config, (k) => k === "calibration.neutral_window") && !state.running) $("neutral").value = neutralText(benchDefault("calibration.neutral_window"));
  if (touched(app, (k) => k.startsWith("compute.")) && !state.running) checkServer().catch(() => {});
  updateControls();
});

(async () => {
  updateControls();
  const init = await api.init();
  prefs.replace(init.settings, { init: true });
  setLanguage(prefs.snap.language);
  initSettings({ benchCommand, onCheck: applyCheck, cameras, openMapping: () => state.model && openMapping(state.model), hasModel: () => !!state.model });
  initMapping({
    canApply: () => (canApplyMapping({ ignoreEditor: false }) && !state.result.preview ? { name: state.result.name } : null),
    canPreview: () => (canApplyMapping() ? { name: state.result.name } : null),
    preview: (draft) => previewMapping(draft),
    signalsFile: () => (state.result && state.result.modelDir === state.model?.dir ? state.result.signals : null),
    apply: () => applyMapping(),
    openCompute: () => openSettings("compute"),
    remote: () => prefs.app("compute.mode") === "remote",
  });
  api.settings.onOpen((section) => openSettings(section));
  applyViewerDefaults();
  $("cam-mic").checked = prefs.app("camera.audio");
  $("preset").value = prefs.app("generation.preset");
  renderPresetSelect();
  fillBench();
  $("neutral").value = neutralText(benchDefault("calibration.neutral_window"));
  checkServer().catch(() => {});
  if (init.model) await openModel(init.model);
  else { setText($("model-name"), "model.none"); setText($("stage-hint"), "stage.chooseModel"); readyMessage(); }
})().catch((err) => fail(err, "status.initFailed"));
