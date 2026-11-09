// Impression silencieuse d'un PDF, sans ouvrir Word ni Adobe.
//
// Ordre de préférence (comme la version 1.x) :
//  1. la commande configurée dans les réglages ({file}, {printer}) ;
//  2. SumatraPDF.exe ou PDFtoPrinter.exe déposé dans le dossier « tools » de l'application (vectoriel) ;
//  3. Chromium : pages rendues par pdf.js à 300 ppp dans une fenêtre invisible, puis impression silencieuse.
// Tests : SCOLARIX_PRINT_TO_PDF=<dossier> remplace l'imprimante par un fichier PDF (imprimante simulée).
"use strict";

const { BrowserWindow, ipcMain } = require("electron");
const { exec } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

function toolsDir(app) {
  return app.isPackaged ? path.join(process.resourcesPath, "tools") : path.resolve(__dirname, "..", "tools");
}

function autoCommand(app) {
  const dir = toolsDir(app);
  const sumatra = path.join(dir, "SumatraPDF.exe");
  if (fs.existsSync(sumatra)) return `"${sumatra}" -print-to-default -print-settings "duplex" -silent "{file}"`;
  const p2p = path.join(dir, "PDFtoPrinter.exe");
  if (fs.existsSync(p2p)) return `"${p2p}" "{file}"`;
  return null;
}

function runCommand(command, file, printer) {
  const cmd = command.replaceAll("{file}", file).replaceAll("{printer}", printer || "");
  return new Promise((resolve, reject) => {
    exec(cmd, { windowsHide: true, timeout: 120_000 }, (err) => {
      if (err) reject(new Error(`La commande d'impression a échoué (${err.code ?? err.message}).`));
      else resolve("commande d'impression");
    });
  });
}

let printSeq = 0;

function printWithChromium(file, { printer, duplex, preload, pageUrl, log }) {
  return new Promise((resolve, reject) => {
    const id = `p${++printSeq}`;
    const win = new BrowserWindow({
      show: false, width: 900, height: 1200,
      webPreferences: { preload, contextIsolation: true, sandbox: false, nodeIntegration: false, offscreen: false },
    });
    let done = false;
    const finish = (err, how) => {
      if (done) return;
      done = true;
      ipcMain.removeAllListeners(`print-ready:${id}`);
      clearTimeout(timer);
      setTimeout(() => !win.isDestroyed() && win.destroy(), 200);
      if (err) reject(err); else resolve(how);
    };
    const timer = setTimeout(() => finish(new Error("Préparation de l'impression trop longue (120 s).")), 120_000);
    ipcMain.once(`print-ready:${id}`, async (_e, info) => {
      if (info && info.error) return finish(new Error(`PDF illisible pour l'impression : ${info.error}`));
      const simulated = process.env.SCOLARIX_PRINT_TO_PDF;
      try {
        if (simulated) {
          const data = await win.webContents.printToPDF({ pageSize: "A4", printBackground: true, margins: { marginType: "none" } });
          fs.mkdirSync(simulated, { recursive: true });
          const out = path.join(simulated, `${String(Date.now())}_${path.basename(file)}`);
          fs.writeFileSync(out, data);
          return finish(null, `imprimante simulée (${info.pages} page(s))`);
        }
        win.webContents.print({
          silent: true, printBackground: true, deviceName: printer || undefined, pageSize: "A4",
          margins: { marginType: "none" }, duplexMode: duplex ? "longEdge" : "simplex", landscape: false,
        }, (ok, reason) => {
          if (ok) finish(null, printer ? `imprimante « ${printer} »` : "imprimante par défaut");
          else finish(new Error(reason === "cancelled" ? "Impression annulée." : `Impression impossible : ${reason || "imprimante indisponible"}.`));
        });
      } catch (err) {
        finish(err);
      }
    });
    win.webContents.on("render-process-gone", (_e, d) => finish(new Error(`Rendu d'impression interrompu (${d.reason}).`)));
    win.loadURL(`${pageUrl}?file=${encodeURIComponent(file)}&id=${id}`).catch((err) => {
      log.error(`page d'impression : ${err.message}`);
      finish(err);
    });
  });
}

async function printPdf(app, file, settings, opts) {
  if (!fs.existsSync(file)) throw new Error("Fichier à imprimer introuvable.");
  const command = (settings && settings.print_command) || (process.env.SCOLARIX_PRINT_TO_PDF ? null : autoCommand(app));
  if (command) return runCommand(command, file, settings && settings.printer_name);
  return printWithChromium(file, { ...opts, printer: settings && settings.printer_name, duplex: opts.duplex });
}

module.exports = { printPdf, toolsDir };
