// Pont entre l'interface (React) et le processus principal : seules ces fonctions sont exposées.
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

async function invoke(channel, ...args) {
  const res = await ipcRenderer.invoke(channel, ...args);
  if (res && res.__error) {
    const err = new Error(res.message);
    err.kind = res.kind;
    throw err;
  }
  return res;
}

contextBridge.exposeInMainWorld("scolarix", {
  call: (method, params) => invoke("engine:call", method, params),
  printReleve: (p) => invoke("print:releve", p),
  printCursus: (p) => invoke("print:cursus", p),
  printDemande: (p) => invoke("print:demande", p),
  printVerified: (p) => invoke("print:verified", p),
  readImage: (file) => invoke("image:read", file),
  printFile: (file) => invoke("print:file", { file }),
  printers: () => invoke("printers"),
  readPdf: (file) => invoke("pdf:read", file),
  reveal: (file) => invoke("shell:reveal", file),
  openPath: (file) => invoke("shell:open", file),
  pickFile: (opts) => invoke("dialog:file", opts),
  pickFolder: (opts) => invoke("dialog:folder", opts),
  saveFile: (opts) => invoke("dialog:save", opts),
  appInfo: () => invoke("app:info"),
  setZoom: (factor) => invoke("app:zoom", factor),
  log: (level, text) => invoke("app:log", level, text),
  relaunch: () => invoke("app:relaunch"),
  onEngineEvent: (cb) => {
    const fn = (_e, msg) => cb(msg);
    ipcRenderer.on("engine:event", fn);
    return () => ipcRenderer.removeListener("engine:event", fn);
  },
  onEngineStatus: (cb) => {
    const fn = (_e, st) => cb(st);
    ipcRenderer.on("engine:status", fn);
    return () => ipcRenderer.removeListener("engine:status", fn);
  },
  // page d'impression (fenêtre invisible)
  printPageReady: (payload) => ipcRenderer.send("print-page:ready", payload),
});
