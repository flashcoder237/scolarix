// Page d'impression (fenêtre invisible) : chaque page du PDF est rendue à 300 ppp par pdf.js puis imprimée
// en A4 sans marge par Chromium. Utilisée quand aucun SumatraPDF n'est installé.
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const params = new URLSearchParams(location.search);
const file = params.get("file") || "";
const id = params.get("id") || "";
const DPI = 300;

async function run() {
  const data = await window.scolarix.readPdf(file);
  const doc = await pdfjs.getDocument({ data }).promise;
  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n);
    const vp = page.getViewport({ scale: DPI / 72 });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({ canvas, canvasContext: ctx, viewport: vp }).promise;
    const blob: Blob = await new Promise((r) => canvas.toBlob((b) => r(b!), "image/png"));
    const img = new Image();
    img.src = URL.createObjectURL(blob);
    await img.decode();
    document.body.appendChild(img);
    page.cleanup();
  }
  window.scolarix.printPageReady({ id, pages: doc.numPages });
}

run().catch((e) => window.scolarix.printPageReady({ id, error: String(e && e.message ? e.message : e) }));

import "./api";
