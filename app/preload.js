"use strict";
const { contextBridge, ipcRenderer, webUtils } = require("electron");

const invoke = (channel) => (arg) => ipcRenderer.invoke(channel, arg);
const listen = (channel) => (fn) => ipcRenderer.on(channel, (_e, value) => fn(value));

contextBridge.exposeInMainWorld("v2l", {
  pathForFile: (file) => webUtils.getPathForFile(file),
  init: invoke("init"),
  openModel: (dir, maxTex) => ipcRenderer.invoke("model:open", { dir, maxTex }),
  listMotions: invoke("model:motions"),
  openVideo: invoke("video:open"),
  run: invoke("run:start"),
  cancel: invoke("run:cancel"),
  readReport: invoke("run:report"),
  cliCommand: invoke("cli:command"),
  install: (file, modelDir) => ipcRenderer.invoke("motion:install", { file, modelDir }),
  reveal: invoke("reveal"),
  cameraAccess: (audio) => ipcRenderer.invoke("camera:access", { audio }),
  saveRecording: (bytes, ext) => ipcRenderer.invoke("recording:save", { bytes, ext }),
  onLog: listen("run:log"),
  onEvent: listen("run:event"),
  onDone: listen("run:done"),
  onTheme: listen("theme:changed"),
  settings: {
    get: invoke("settings:get"),
    set: invoke("settings:set"),
    exportAll: invoke("settings:export"),
    importAll: invoke("settings:import"),
    resetAll: invoke("settings:reset"),
    chooseFolder: (key) => ipcRenderer.invoke("settings:chooseFolder", { key }),
    chooseFile: (kind) => ipcRenderer.invoke("settings:chooseFile", { kind }),
    onOpen: listen("settings:open"),
  },
  presets: {
    list: invoke("presets:list"),
    save: invoke("presets:save"),
    rename: invoke("presets:rename"),
    remove: invoke("presets:delete"),
    importFile: invoke("presets:import"),
    exportFile: invoke("presets:export"),
    apply: invoke("presets:apply"),
  },
  compute: {
    check: invoke("compute:check"),
    startTask: (kind) => ipcRenderer.invoke("task:start", { kind }),
    cancelTask: invoke("task:cancel"),
    onTaskLog: listen("task:log"),
    onTaskDone: listen("task:done"),
  },
  mcpInfo: invoke("mcp:info"),
  copy: invoke("clipboard:write"),
  clearCache: invoke("cache:clear"),
  aboutInfo: invoke("about:info"),
  openNotices: invoke("about:notices"),
  openRepository: invoke("about:repository"),
  openFolder: invoke("open:folder"),
});
