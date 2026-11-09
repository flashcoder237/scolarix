// Moteur Python (processus séparé) : lancement, appels JSON ligne par ligne, battement de cœur, relance.
//
// Garantie pour l'agent : l'interface ne se fige jamais. Un appel trop long échoue proprement (délai par
// méthode) ; un moteur bloqué (plus de réponse au « ping ») ou planté est relancé, et l'interface en est avertie.
"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const readline = require("node:readline");
const { EventEmitter } = require("node:events");

// Délais maximaux (ms). Les aperçus Word d'un recueil de 100 pages peuvent prendre plusieurs minutes.
const TIMEOUTS = {
  default: 60_000,
  preview: 300_000,
  cursus_build: 900_000,
  demande_prepare: 900_000,
  demande_files_to_print: 900_000,
  verify_identify: 180_000,
  verify_compare: 300_000,
  verify_report: 300_000,
  extraction_export: 300_000,
  print_prepare: 120_000,
  export: 120_000,
};
const HEARTBEAT_MS = 5_000;
const HEARTBEAT_DEAD_MS = 20_000;

function engineCommand(app) {
  if (process.env.SCOLARIX_ENGINE_CMD) {
    // développement / tests : « python -m engine.server » depuis le dépôt
    const [cmd, ...args] = JSON.parse(process.env.SCOLARIX_ENGINE_CMD);
    return { cmd, args, cwd: process.env.SCOLARIX_ENGINE_CWD || path.resolve(__dirname, "..", "..") };
  }
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, "engine");
    return { cmd: path.join(dir, process.platform === "win32" ? "scolarix-engine.exe" : "scolarix-engine"), args: [], cwd: dir };
  }
  const repo = path.resolve(__dirname, "..", "..");
  const py = process.platform === "win32" ? "python" : "python3";
  return { cmd: py, args: ["-m", "engine.server"], cwd: repo };
}

class Engine extends EventEmitter {
  constructor(app, log) {
    super();
    this.app = app;
    this.log = log;
    this.proc = null;
    this.pending = new Map();
    this.nextId = 1;
    this.lastPong = Date.now();
    this.restarts = 0;
    this.stopping = false;
    this.ready = null;
  }

  start() {
    const { cmd, args, cwd } = engineCommand(this.app);
    this.log.info(`moteur : ${cmd} ${args.join(" ")} (dossier ${cwd})`);
    if (!fs.existsSync(cwd)) this.log.error(`dossier du moteur introuvable : ${cwd}`);
    const env = { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" };
    this.proc = spawn(cmd, args, { cwd, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    const proc = this.proc;
    let resolveReady;
    const next = this._resolveNext;
    this._resolveNext = null;
    this.ready = new Promise((r) => (resolveReady = (v) => { r(v); if (next) next(v); }));
    readline.createInterface({ input: proc.stdout }).on("line", (line) => this._onLine(line, resolveReady));
    readline.createInterface({ input: proc.stderr }).on("line", (line) => this.log.engine(line));
    proc.on("error", (err) => {
      this.log.error(`moteur impossible à lancer : ${err.message}`);
      this.emit("status", { state: "error", message: `Moteur impossible à lancer : ${err.message}` });
    });
    proc.on("exit", (code, signal) => this._onExit(proc, code, signal));
    this.lastPong = Date.now();
    clearInterval(this.heartbeat);
    this.heartbeat = setInterval(() => this._beat(), HEARTBEAT_MS);
    this.emit("status", { state: "starting" });
    return this.ready;
  }

  _onLine(line, resolveReady) {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      this.log.warn(`ligne du moteur illisible : ${line.slice(0, 200)}`);
      return;
    }
    if (msg.event) {
      if (msg.event === "ready") {
        this.restarts = 0;
        resolveReady(msg.data);
        this.emit("status", { state: "ready", pid: msg.data.pid });
      }
      this.emit("event", msg);
      return;
    }
    if (msg.id === "hb") {
      this.lastPong = Date.now();
      return;
    }
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    clearTimeout(p.timer);
    if (msg.error) {
      if (msg.error.kind === "internal") this.log.error(`${p.method} : ${msg.error.message}\n${msg.error.trace || ""}`);
      p.reject(Object.assign(new Error(msg.error.message), { kind: msg.error.kind }));
    } else {
      p.resolve(msg.result);
    }
  }

  _beat() {
    if (!this.proc || this.stopping) return;
    if (Date.now() - this.lastPong > HEARTBEAT_DEAD_MS) {
      this.log.error("moteur sans réponse depuis 20 s : relance");
      this.emit("status", { state: "restarting", message: "Le moteur ne répondait plus : il est relancé." });
      this.proc.kill();
      return;
    }
    this._write({ id: "hb", method: "ping" });
  }

  _onExit(proc, code, signal) {
    if (proc !== this.proc) return;
    clearInterval(this.heartbeat);
    this.proc = null;
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(Object.assign(new Error("Le moteur s'est arrêté pendant l'opération : réessayez."), { kind: "engine" }));
    }
    this.pending.clear();
    if (this.stopping) return;
    // Les appels lancés pendant la relance attendent le nouveau moteur au lieu d'échouer.
    let resolveNext;
    this.ready = new Promise((r) => (resolveNext = r));
    this._resolveNext = resolveNext;
    this.restarts += 1;
    this.log.error(`moteur arrêté (code ${code}, signal ${signal}) : relance n° ${this.restarts}`);
    const delay = Math.min(10_000, 500 * 2 ** Math.min(this.restarts, 5));
    this.emit("status", { state: "restarting", message: "Le moteur a redémarré. Réessayez l'opération en cours." });
    setTimeout(() => !this.stopping && this.start(), delay);
  }

  _write(msg) {
    if (!this.proc || !this.proc.stdin.writable) return false;
    this.proc.stdin.write(JSON.stringify(msg) + "\n");
    return true;
  }

  async call(method, params = {}) {
    if (this.ready) {
      const ok = await Promise.race([this.ready.then(() => true), new Promise((r) => setTimeout(() => r(false), 30_000))]);
      if (!ok) throw Object.assign(new Error("Le moteur ne démarre pas : consultez le journal des incidents (Réglages)."), { kind: "engine" });
    }
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      const ms = TIMEOUTS[method] || TIMEOUTS.default;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.log.warn(`${method} : délai de ${ms / 1000} s dépassé`);
        reject(Object.assign(new Error(`Opération trop longue (${Math.round(ms / 1000)} s) : réessayez. Si le problème `
          + "persiste, le fichier source est peut-être sur un partage réseau lent ou endommagé."), { kind: "timeout" }));
      }, ms);
      this.pending.set(id, { resolve, reject, timer, method });
      if (!this._write({ id, method, params })) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(Object.assign(new Error("Moteur indisponible (redémarrage en cours) : réessayez dans un instant."), { kind: "engine" }));
      }
    });
  }

  async stop() {
    this.stopping = true;
    clearInterval(this.heartbeat);
    const proc = this.proc;
    if (!proc) return;
    try {
      proc.stdin.end();
    } catch { /* déjà fermé */ }
    await new Promise((resolve) => {
      const t = setTimeout(() => { try { proc.kill(); } catch { /* */ } resolve(); }, 25_000);
      proc.once("exit", () => { clearTimeout(t); resolve(); });
    });
  }
}

module.exports = { Engine, TIMEOUTS };
