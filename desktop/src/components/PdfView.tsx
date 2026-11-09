import { useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { sx } from "../api";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

// Petit cache des documents ouverts (les octets sont en mémoire : aucun fichier n'est tenu ouvert,
// Word peut régénérer un aperçu pendant qu'il est affiché).
const cache = new Map<string, Promise<{ task: pdfjs.PDFDocumentLoadingTask; doc: pdfjs.PDFDocumentProxy }>>();
function open(file: string, version: string) {
  const key = `${file}#${version}`;
  let p = cache.get(key);
  if (!p) {
    p = sx.readPdf(file).then(async (data) => {
      const task = pdfjs.getDocument({ data });
      return { task, doc: await task.promise };
    });
    cache.set(key, p);
    p.catch(() => cache.delete(key));
    while (cache.size > 4) {
      const [k, old] = cache.entries().next().value as [string, Promise<{ task: pdfjs.PDFDocumentLoadingTask }>];
      cache.delete(k);
      old.then((d) => d.task.destroy()).catch(() => undefined);
    }
  }
  return p.then((x) => x.doc);
}
export function forget(file: string) {
  for (const k of [...cache.keys()]) if (k.startsWith(file + "#")) cache.delete(k);
}

export interface PdfViewProps {
  file: string | null;
  first: number;           // première page (1 = première)
  last: number;
  version?: string;        // change quand le fichier est régénéré
  message?: string | null; // message à la place du document (préparation, erreur…)
  stamp?: boolean;         // aperçu du timbre « Duplicata conforme »
  onError?: (msg: string) => void;
  jumpTo?: number;         // faire défiler jusqu'à cette page
}

export function PdfView({ file, first, last, version = "", message, stamp = false, onError, jumpTo }: PdfViewProps) {
  const box = useRef<HTMLDivElement>(null);
  const pagesRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);          // 1 = largeur ajustée
  const [width, setWidth] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => { setZoom(1); }, [file, first]);

  // Ctrl + molette : zoom du document
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      setZoom((z) => Math.max(0.5, Math.min(4, z * (e.deltaY < 0 ? 1.15 : 1 / 1.15))));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, []);

  useEffect(() => {
    const host = pagesRef.current;
    if (!host || !file || message || width < 50) return;
    let cancelled = false;
    const tasks: pdfjs.RenderTask[] = [];
    setErr(null);
    setLoading(true);
    (async () => {
      try {
        const doc = await open(file, version);
        if (cancelled) return;
        const from = Math.max(1, first), to = Math.min(doc.numPages, Math.max(first, last));
        if (from > doc.numPages) throw new Error(`page ${first} absente du document (${doc.numPages} pages)`);
        const canvases: HTMLCanvasElement[] = [];
        const ratio = window.devicePixelRatio || 1;
        for (let n = from; n <= to; n++) {
          const page = await doc.getPage(n);
          if (cancelled) return;
          const base = page.getViewport({ scale: 1 });
          const cssW = Math.max(200, (width - 26) * zoom);
          const scale = cssW / base.width;
          const vp = page.getViewport({ scale: scale * ratio });
          const c = document.createElement("canvas");
          c.width = Math.floor(vp.width);
          c.height = Math.floor(vp.height);
          c.style.width = `${Math.floor(vp.width / ratio)}px`;
          c.style.height = `${Math.floor(vp.height / ratio)}px`;
          c.dataset.page = String(n);
          const t = page.render({ canvas: c, canvasContext: c.getContext("2d")!, viewport: vp });
          tasks.push(t);
          canvases.push(c);
        }
        await Promise.all(tasks.map((t) => t.promise));
        if (cancelled) return;
        host.replaceChildren(...canvases.map((c) => {
          const wrap = document.createElement("div");
          wrap.style.position = "relative";
          wrap.appendChild(c);
          if (stamp) {
            const s = document.createElement("div");
            s.className = "stamp-overlay";
            s.textContent = "DUPLICATA CONFORME";
            Object.assign(s.style, { right: "8%", top: "6%", border: "3px solid #B91C1C", color: "#B91C1C", borderRadius: "8px",
              padding: "4px 10px", font: "700 14px Inter", transform: "rotate(-8deg)", opacity: "0.8", background: "rgba(255,255,255,.6)" });
            wrap.appendChild(s);
          }
          return wrap;
        }));
        setLoading(false);
      } catch (e: unknown) {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        if (/cancel/i.test(msg)) return;
        setErr(`Aperçu impossible : ${msg}`);
        setLoading(false);
        onError?.(msg);
      }
    })();
    return () => { cancelled = true; tasks.forEach((t) => t.cancel()); };
  }, [file, first, last, version, message, width, zoom, stamp, onError]);

  useEffect(() => {
    if (!jumpTo || !pagesRef.current) return;
    const c = pagesRef.current.querySelector(`canvas[data-page="${jumpTo}"]`);
    c?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [jumpTo]);

  const shown = message || err || (!file ? "L'aperçu du relevé sélectionné s'affiche ici." : null);
  return (
    <div className="col grow" style={{ gap: 8, minHeight: 0 }}>
      <div className="viewer" ref={box} data-testid="viewer" title="Ctrl + molette : zoomer le document">
        <div className="pages" ref={pagesRef} style={{ visibility: shown ? "hidden" : "visible" }} />
        {shown && <div className="msg">{loading && !err ? <span className="dots">{shown}</span> : shown}</div>}
        {!shown && loading && <div className="msg" style={{ background: "transparent" }}><span className="spinner" /></div>}
      </div>
      <ZoomBar zoom={zoom} setZoom={setZoom} />
    </div>
  );
}

function ZoomBar({ zoom, setZoom }: { zoom: number; setZoom: (f: (z: number) => number) => void }) {
  return (
    <div className="row small" style={{ justifyContent: "flex-end", gap: 6 }}>
      <span className="muted tiny">{zoom === 1 ? "Largeur ajustée" : `${Math.round(zoom * 100)} %`}</span>
      <button className="btn ghost sm" title="Ajuster à la largeur" onClick={() => setZoom(() => 1)}>Ajuster</button>
      <button className="btn ghost sm" aria-label="Zoom arrière" onClick={() => setZoom((z) => Math.max(0.5, z / 1.2))}>−</button>
      <button className="btn ghost sm" aria-label="Zoom avant" onClick={() => setZoom((z) => Math.min(4, z * 1.2))}>+</button>
    </div>
  );
}
