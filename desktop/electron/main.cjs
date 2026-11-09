// Scolarix — processus principal Electron.
// Fenêtre, pont sécurisé vers le moteur Python, impression, fichiers, journal des incidents.
"use strict";

const { app, BrowserWindow, ipcMain, dialog, shell, Menu, powerSaveBlocker } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { Engine } = require("./engine.cjs");
const { printPdf } = require("./printing.cjs");

// ---------------------------------------------------------------- dossier de données et journal
function dataDir() {
  if (process.env.FMSP_DATA_DIR) return process.env.FMSP_DATA_DIR;
  if (process.platform === "win32") return path.join(process.env.APPDATA || os.homedir(), "FMSP_Archives");
  return path.join(os.homedir(), ".fmsp_archives");
}
const LOGS = path.join(dataDir(), "logs");
fs.mkdirSync(LOGS, { recursive: true });
const LOG_FILE = path.join(LOGS, "interface.log");
try { if (fs.statSync(LOG_FILE).size > 2_000_000) fs.renameSync(LOG_FILE, LOG_FILE + ".1"); } catch { /* absent */ }

function write(level, text) {
  const line = `${new Date().toISOString()} ${level} ${text}\n`;
  try { fs.appendFileSync(LOG_FILE, line); } catch { /* disque plein : on n'arrête rien pour ça */ }
  if (process.env.SCOLARIX_DEBUG) process.stderr.write(line);
}
const log = {
  info: (t) => write("INFO", t), warn: (t) => write("WARN", t), error: (t) => write("ERROR", t),
  engine: (t) => write("MOTEUR", t),
};
process.on("uncaughtException", (err) => log.error(`exception non rattrapée : ${err.stack || err}`));
process.on("unhandledRejection", (err) => log.error(`promesse rejetée : ${err && (err.stack || err)}`));

// Une seule instance : un second lancement ramène la fenêtre existante (pas deux moteurs sur la même base).
if (!app.requestSingleInstanceLock()) {
  app.quit();
}

const DIST = path.join(__dirname, "..", "dist");
const PRELOAD = path.join(__dirname, "preload.cjs");
let win = null;
let engine = null;
let settingsCache = {};

function sendToUi(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1366, height: 768, minWidth: 1100, minHeight: 640, show: false,
    title: "Scolarix", backgroundColor: "#F1F5F9",
    icon: path.join(__dirname, "..", "build", "icon.png"),
    webPreferences: { preload: PRELOAD, contextIsolation: true, nodeIntegration: false, sandbox: false, spellcheck: false },
  });
  Menu.setApplicationMenu(null);
  if (!process.env.SCOLARIX_E2E) win.maximize();
  win.once("ready-to-show", () => win.show());
  win.loadFile(path.join(DIST, "index.html"));
  // Liens externes : jamais dans l'application.
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  // Incidents de l'interface : journalisés ; un rendu planté est rechargé.
  win.webContents.on("render-process-gone", (_e, d) => {
    log.error(`interface interrompue (${d.reason}, code ${d.exitCode}) : rechargement`);
    if (d.reason !== "clean-exit") setTimeout(() => win && !win.isDestroyed() && win.reload(), 500);
  });
  win.on("unresponsive", () => log.error("interface sans réponse (fenêtre « ne répond pas »)"));
  win.on("responsive", () => log.warn("interface de nouveau réactive"));
  win.webContents.on("console-message", (_e, level, message) => { if (level >= 2) log.warn(`console : ${message}`); });
  win.on("closed", () => { win = null; });
}

// ---------------------------------------------------------------- impression orchestrée
// Le moteur prépare le fichier (contrôle, timbre, référence), Electron imprime, le moteur journalise.
const printPage = () => `file://${path.join(DIST, "print.html").replaceAll("\\", "/")}`;

async function printFile(file, duplex) {
  return printPdf(app, file, settingsCache, { duplex, preload: PRELOAD, pageUrl: printPage(), log });
}

async function printReleve({ releve_id, duplicata, visually_checked }) {
  const prep = await engine.call("print_prepare", { releve_id, duplicata: !!duplicata, visually_checked: !!visually_checked });
  try {
    const how = await printFile(prep.file, prep.duplex);
    const res = await engine.call("print_done", { releve_id, ref: prep.ref, duplicata: !!duplicata, how, file: prep.file });
    return { ...prep, how, stats: res.stats };
  } catch (err) {
    await engine.call("print_failed", { releve_id, ref: prep.ref, error: String(err.message || err), file: prep.file }).catch(() => {});
    throw err;
  }
}

async function printCursus({ matricule, releve_ids, cover, duplicata }) {
  const out = await engine.call("cursus_build", { matricule, releve_ids, cover, duplicata, mode: "print" });
  const how = await printFile(out.path, !!settingsCache.duplex);
  const res = await engine.call("cursus_printed", { matricule, ref: out.ref, pages: out.pages, how, file: out.path });
  return { ...out, how, stats: res.stats };
}

async function printDemande({ did, with_bordereau }) {
  const res = await engine.call("demande_files_to_print", { did, with_bordereau: !!with_bordereau });
  const hows = [];
  for (const f of res.files) hows.push(await printFile(f, !!settingsCache.duplex));
  return engine.call("demande_printed", { did, how: hows[hows.length - 1] || "" });
}

// ---------------------------------------------------------------- pont IPC (seules ces actions sont possibles)
function errorPayload(err) {
  return { __error: true, message: String((err && err.message) || err), kind: (err && err.kind) || "internal" };
}
function handle(channel, fn) {
  ipcMain.handle(channel, async (_e, ...args) => {
    try { return await fn(...args); } catch (err) { return errorPayload(err); }
  });
}

function registerIpc() {
  handle("engine:call", async (method, params) => {
    const result = await engine.call(method, params || {});
    if (method === "settings_get" || method === "settings_save") settingsCache = result;
    if (method === "info") settingsCache = result.settings;
    return result;
  });
  handle("print:releve", printReleve);
  handle("print:cursus", printCursus);
  handle("print:demande", printDemande);
  handle("print:file", ({ file }) => printFile(file, false));
  handle("printers", async () => (win ? (await win.webContents.getPrintersAsync()).map((p) => ({ name: p.name, isDefault: p.isDefault })) : []));
  handle("pdf:read", async (file) => {
    // Lecture seule, PDF uniquement : l'aperçu pdf.js reçoit les octets, aucun fichier n'est tenu ouvert.
    if (typeof file !== "string" || !file.toLowerCase().endsWith(".pdf")) throw new Error("Seuls les PDF sont lisibles.");
    const data = await fs.promises.readFile(file);
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  });
  handle("shell:reveal", (file) => { shell.showItemInFolder(file); return true; });
  handle("shell:open", (file) => shell.openPath(file));
  handle("dialog:file", async (opts) => {
    const r = await dialog.showOpenDialog(win, { properties: ["openFile"], ...opts });
    return r.canceled ? null : r.filePaths[0];
  });
  handle("dialog:folder", async (opts) => {
    const r = await dialog.showOpenDialog(win, { properties: ["openDirectory"], ...opts });
    return r.canceled ? null : r.filePaths[0];
  });
  handle("dialog:save", async (opts) => {
    const r = await dialog.showSaveDialog(win, opts || {});
    return r.canceled ? null : r.filePath;
  });
  handle("app:info", () => ({ version: app.getVersion(), logs: LOGS, platform: process.platform, e2e: !!process.env.SCOLARIX_E2E }));
  handle("app:zoom", (factor) => { if (win) win.webContents.setZoomFactor(Math.max(0.5, Math.min(2, factor))); return true; });
  handle("app:log", (level, text) => { write(level === "error" ? "ERROR" : "UI", String(text).slice(0, 4000)); return true; });
  handle("app:relaunch", () => { app.relaunch(); app.quit(); return true; });
  ipcMain.on("print-page:ready", (e, payload) => ipcMain.emit(`print-ready:${payload.id}`, e, payload));
}

// ---------------------------------------------------------------- démarrage
app.on("second-instance", () => {
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
});

app.whenReady().then(async () => {
  log.info(`Scolarix ${app.getVersion()} — démarrage (${process.platform}, Electron ${process.versions.electron})`);
  engine = new Engine(app, log);
  engine.on("event", (msg) => sendToUi("engine:event", msg));
  engine.on("status", (st) => sendToUi("engine:status", st));
  registerIpc();
  createWindow();
  engine.start();
  // Pendant une indexation, Windows ne met pas le poste en veille.
  let blocker = null;
  engine.on("event", (msg) => {
    if (msg.event !== "index") return;
    if (msg.data.type === "start" && blocker === null) blocker = powerSaveBlocker.start("prevent-app-suspension");
    if ((msg.data.type === "exit" || msg.data.type === "done") && blocker !== null) { powerSaveBlocker.stop(blocker); blocker = null; }
  });
});

let quitting = false;
app.on("before-quit", async (e) => {
  if (quitting || !engine) return;
  e.preventDefault();
  quitting = true;
  log.info("fermeture : arrêt du moteur");
  await engine.stop();
  app.quit();
});
app.on("window-all-closed", () => app.quit());
