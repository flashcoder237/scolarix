// Volets de l'écran Vérification : un document (PDF via pdf.js, ou image) dessiné à une échelle pilotée par
// l'écran, avec des rectangles surlignés posés sur la page (positions fournies par le moteur, en fractions
// de la page) et un défilement pilotable (défilement lié, aller à une divergence). Mode « Calque » : deux
// documents superposés au même zoom, un rideau glissant montre l'original à gauche et le soumis à droite.
// PdfView (guichet) n'est pas modifié : on réutilise seulement ses constantes d'ajustement.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { sx } from "../api";
import { READABLE, type FitMode } from "./PdfView";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const PT = 96 / 72;          // points PDF → pixels CSS (100 % = taille réelle)
const READING = 0.9;         // zoom de lecture (même valeur que le guichet)
const PAD = 12;              // marge autour des pages
const A4W = 595.28;          // une image (photo, scan) est affichée comme une page A4 de même largeur

export type DocSrc = { kind: "pdf"; file: string; first: number; last: number; version: string } | { kind: "image"; url: string };
export interface Box { page: number; x0: number; y0: number; x1: number; y1: number }
export interface Mark extends Box {
  key: string;
  tone: "red" | "blue" | "green" | "zone";
  sel?: boolean;           // champ sélectionné : trait plus épais, bulle des valeurs
  estimated?: boolean;     // position estimée (ligne non lue sur ce document)
  tip?: ReactNode;
}
interface Dims { w: number; h: number }

// ------------------------------------------------------------------ documents PDF (octets en mémoire)
const docs = new Map<string, Promise<{ task: pdfjs.PDFDocumentLoadingTask; doc: pdfjs.PDFDocumentProxy }>>();
function openPdf(file: string, version: string) {
  const key = `${file}#${version}`;
  let p = docs.get(key);
  if (!p) {
    p = sx.readPdf(file).then(async (data) => { const task = pdfjs.getDocument({ data }); return { task, doc: await task.promise }; });
    docs.set(key, p);
    p.catch(() => docs.delete(key));
    while (docs.size > 4) {
      const [k, old] = docs.entries().next().value as [string, Promise<{ task: pdfjs.PDFDocumentLoadingTask }>];
      docs.delete(k);
      old.then((d) => d.task.destroy()).catch(() => undefined);
    }
  }
  return p.then((x) => x.doc);
}

/** Même calcul que le guichet : Auto = page entière si elle reste lisible, sinon zoom de lecture. */
export function fitScale(fit: FitMode, manual: number, w: number, h: number, dims: Dims | null, top = PAD) {
  if (!dims || w < 50 || h < 50) return null;
  const W = Math.max(50, w - 2 * PAD), H = Math.max(50, h - PAD - top);
  const pw = dims.w * PT, ph = dims.h * PT;
  const page = Math.min(W / pw, H / ph), largeur = W / pw;
  let s: number;
  if (fit === "page") s = page;
  else if (fit === "largeur") s = largeur;
  else if (fit === "1:1") s = 1;
  else if (fit === "libre") s = manual;
  else s = page >= READABLE ? Math.min(page, 1.5) : Math.min(READING, largeur);
  return Math.round(Math.max(0.2, Math.min(4, s)) * 1000) / 1000;
}

// ------------------------------------------------------------------ une couche : les pages d'un document
interface LayerProps {
  src: DocSrc | null;
  scale: number | null;
  marks: Mark[];
  onDims: (d: Dims[] | null, err?: string) => void;
  onReady: (ready: boolean) => void;
  clip?: number;            // rideau : part masquée à gauche (0..1)
  overlay?: boolean;        // couche posée sur la première (mode calque)
  testid?: string;
}

function Layer({ src, scale, marks, onDims, onReady, clip, overlay, testid }: LayerProps) {
  const [dims, setDims] = useState<Dims[] | null>(null);
  const [pages, setPages] = useState<number[]>([]);
  const holders = useRef(new Map<number, HTMLDivElement>());
  const key = src ? (src.kind === "pdf" ? `${src.file}#${src.version}#${src.first}-${src.last}` : src.url.slice(0, 200) + src.url.length) : "";

  // dimensions des pages (points PDF), avant tout dessin : l'écran en déduit l'échelle
  useEffect(() => {
    let cancelled = false;
    setDims(null);
    setPages([]);
    onReady(false);
    if (!src) { onDims(null); return; }
    (async () => {
      try {
        if (src.kind === "image") {
          const img = new Image();
          await new Promise<void>((ok, ko) => { img.onload = () => ok(); img.onerror = () => ko(new Error("image illisible")); img.src = src.url; });
          if (cancelled) return;
          const d = [{ w: A4W, h: (A4W * img.naturalHeight) / Math.max(1, img.naturalWidth) }];
          setDims(d); setPages([1]); onDims(d);
          return;
        }
        const doc = await openPdf(src.file, src.version);
        if (cancelled) return;
        const from = Math.max(1, src.first), to = Math.min(doc.numPages, Math.max(src.first, src.last));
        if (from > doc.numPages) throw new Error(`page ${src.first} absente du document (${doc.numPages} pages)`);
        const d: Dims[] = [], nums: number[] = [];
        for (let n = from; n <= to; n++) {
          const vp = (await doc.getPage(n)).getViewport({ scale: 1 });
          d.push({ w: vp.width, h: vp.height });
          nums.push(n);
        }
        if (cancelled) return;
        setDims(d); setPages(nums); onDims(d);
      } catch (e) {
        if (!cancelled) onDims(null, e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  // dessin des pages PDF à l'échelle demandée ; l'ancien dessin reste affiché jusqu'au remplacement
  useEffect(() => {
    if (!src || !scale || !dims || !pages.length) return;
    if (src.kind === "image") { onReady(true); return; }
    let cancelled = false;
    const tasks: pdfjs.RenderTask[] = [];
    const t = window.setTimeout(async () => {
      try {
        const doc = await openPdf(src.file, src.version);
        const ratio = window.devicePixelRatio || 1;
        const out: [number, HTMLCanvasElement][] = [];
        for (const n of pages) {
          const page = await doc.getPage(n);
          if (cancelled) return;
          const vp = page.getViewport({ scale: scale * PT * ratio });
          const c = document.createElement("canvas");
          c.width = Math.floor(vp.width);
          c.height = Math.floor(vp.height);
          const task = page.render({ canvas: c, canvasContext: c.getContext("2d")!, viewport: vp });
          tasks.push(task);
          out.push([n, c]);
        }
        await Promise.all(tasks.map((x) => x.promise));
        if (cancelled) return;
        for (const [n, c] of out) holders.current.get(n)?.replaceChildren(c);
        onReady(true);
      } catch (e) {
        if (!cancelled && !/cancel/i.test(String(e))) onDims(null, e instanceof Error ? e.message : String(e));
      }
    }, 30);
    return () => { cancelled = true; window.clearTimeout(t); tasks.forEach((x) => x.cancel()); };
  }, [key, scale, dims, pages]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!src || !dims || !scale) return null;
  const style = clip !== undefined ? { clipPath: `inset(0 0 0 ${Math.max(0, Math.min(1, clip)) * 100}%)` } : undefined;
  return (
    <div className={`vf-layer${overlay ? " over" : ""}`} style={style} data-testid={testid}>
      {pages.map((n, i) => {
        const w = Math.floor(dims[i].w * PT * scale), h = Math.floor(dims[i].h * PT * scale);
        return (
          <div key={n} className="vf-pg" data-page={n} style={{ width: w, height: h }}>
            {src.kind === "image"
              ? <img src={src.url} alt="Document soumis" draggable={false} style={{ width: w, height: h }} />
              : <div className="cv" ref={(el) => { if (el) holders.current.set(n, el); else holders.current.delete(n); }} />}
            {marks.filter((m) => m.page === n).map((m) => (
              <div key={m.key} className={`vf-hl t-${m.tone}${m.sel ? " sel" : ""}${m.estimated ? " est" : ""}`} data-testid="vf-mark" data-tone={m.tone}
                style={{ left: `calc(${m.x0 * 100}% - 4px)`, top: `calc(${m.y0 * 100}% - 3px)`, width: `calc(${(m.x1 - m.x0) * 100}% + 8px)`, height: `calc(${(m.y1 - m.y0) * 100}% + 6px)` }}>
                {m.tip && <div className="vf-tip" data-testid="vf-tip">{m.tip}</div>}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ volet : zone qui défile
export interface PaneState { scale: number | null; natural: number | null; ready: boolean; overflow: boolean; error: string | null; dims: Dims | null }
export interface PaneHandle {
  el: () => HTMLDivElement | null;
  /** Fait venir ce rectangle au tiers haut du volet (et au centre en largeur si la page déborde). */
  scrollToBox: (b: Box, smooth?: boolean) => void;
  frac: () => { y: number; x: number };
  setFrac: (y: number, x: number) => void;
}
interface PaneProps {
  layers: { src: DocSrc | null; marks: Mark[]; testid?: string }[];
  fit: FitMode;
  manual: number;
  onZoom: (manual: number) => void;            // Ctrl + molette : zoom libre, partagé par les volets
  onState?: (s: PaneState) => void;
  onUserScroll?: (y: number, x: number) => void;
  message?: string | null;
  tag: ReactNode;
  curtain?: { value: number; onChange: (v: number) => void };
  cap?: number | null;      // zoom lié : les deux volets prennent la plus petite des deux échelles
  testid?: string;
  onDoubleClick?: () => void;
}

export const ComparePane = forwardRef<PaneHandle, PaneProps>(function ComparePane(
  { layers, fit, manual, onZoom, onState, onUserScroll, message, tag, curtain, cap, testid, onDoubleClick }, ref) {
  const box = useRef<HTMLDivElement>(null);
  const stack = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0, top: 44 });
  const [dims, setDims] = useState<(Dims[] | null)[]>([]);
  const [errs, setErrs] = useState<(string | null)[]>([]);
  const [ready, setReady] = useState<boolean[]>([]);
  const [overflow, setOverflow] = useState(false);
  const expect = useRef<{ y: number; x: number } | null>(null);   // position posée par programme (pas de rebond)
  const quietUntil = useRef(0);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => {
      const top = parseFloat(getComputedStyle(el).getPropertyValue("--vf-top")) || 44;
      setSize((s) => (s.w === el.clientWidth && s.h === el.clientHeight && s.top === top ? s : { w: el.clientWidth, h: el.clientHeight, top }));
    };
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, []);

  const primary = layers.findIndex((l) => !!l.src);
  const pdims = primary >= 0 ? dims[primary]?.[0] || null : null;
  const natural = useMemo(() => fitScale(fit, manual, size.w, size.h, pdims, size.top), [fit, manual, size, pdims]);
  const scale = natural && cap ? Math.min(natural, cap) : natural;
  // calque : la couche posée dessus prend la largeur de la première, quelle que soit sa taille d'origine
  const layerScale = (i: number) => {
    const d = dims[i]?.[0];
    if (!scale || !d || !pdims || i === primary) return scale;
    return Math.round(((scale * pdims.w) / d.w) * 1000) / 1000;
  };
  const scaleRef = useRef(scale);
  scaleRef.current = scale;

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      onZoom(Math.max(0.25, Math.min(4, (scaleRef.current || 1) * (e.deltaY < 0 ? 1.15 : 1 / 1.15))));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [onZoom]);

  // nouveau document : en haut de la page
  const srcKey = layers.map((l) => (l.src ? (l.src.kind === "pdf" ? `${l.src.file}#${l.src.version}` : l.src.url.length) : "")).join("|");
  useEffect(() => { box.current?.scrollTo({ top: 0, left: 0 }); }, [srcKey]);

  const allReady = layers.every((l, i) => !l.src || ready[i]) && primary >= 0;
  const error = errs.find((e) => !!e) || null;
  const measure = useCallback(() => {
    const el = box.current;
    if (el) setOverflow(el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2);
  }, []);
  useEffect(() => { measure(); }, [scale, allReady, size, measure]);
  useEffect(() => { onState?.({ scale, natural, ready: allReady && !error, overflow, error, dims: pdims }); }, [scale, natural, allReady, overflow, error, pdims, onState]);

  const frac = () => {
    const el = box.current;
    if (!el) return { y: 0, x: 0 };
    const my = el.scrollHeight - el.clientHeight, mx = el.scrollWidth - el.clientWidth;
    return { y: my > 0 ? el.scrollTop / my : 0, x: mx > 0 ? el.scrollLeft / mx : 0 };
  };
  useImperativeHandle(ref, () => ({
    el: () => box.current,
    frac,
    setFrac: (y, x) => {
      const el = box.current;
      if (!el) return;
      const top = Math.round(y * (el.scrollHeight - el.clientHeight)), left = Math.round(x * (el.scrollWidth - el.clientWidth));
      expect.current = { y: top, x: left };
      el.scrollTo({ top, left });
    },
    scrollToBox: (b, smooth = true) => {
      const el = box.current, host = stack.current;
      const pg = host?.querySelector(`.vf-pg[data-page="${b.page}"]`) as HTMLElement | null;
      if (!el || !pg) return;
      const er = el.getBoundingClientRect(), pr = pg.getBoundingClientRect();
      const y = pr.top - er.top + el.scrollTop + b.y0 * pr.height;
      const x = pr.left - er.left + el.scrollLeft + ((b.x0 + b.x1) / 2) * pr.width;
      quietUntil.current = Date.now() + (smooth ? 700 : 80);
      el.scrollTo({ top: Math.max(0, y - el.clientHeight * 0.34), left: Math.max(0, x - el.clientWidth / 2), behavior: smooth ? "smooth" : "auto" });
    },
  }));

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    if (expect.current && Math.abs(el.scrollTop - expect.current.y) < 3 && Math.abs(el.scrollLeft - expect.current.x) < 3) { expect.current = null; return; }
    expect.current = null;
    if (Date.now() < quietUntil.current) return;
    const f = frac();
    onUserScroll?.(f.y, f.x);
  };

  // rideau du calque : poignée posée sur la page, glissée à la souris ou au clavier (← →)
  const [knobX, setKnobX] = useState<number | null>(null);
  const placeKnob = useCallback(() => {
    const el = box.current, host = stack.current;
    if (!curtain || !el || !host) { setKnobX(null); return; }
    const er = el.getBoundingClientRect(), hr = host.getBoundingClientRect();
    setKnobX(hr.left - er.left + curtain.value * hr.width);
  }, [curtain]);
  useLayoutEffect(() => { placeKnob(); }, [placeKnob, scale, size, allReady]);
  const drag = (e: React.PointerEvent) => {
    if (!curtain) return;
    e.preventDefault();
    const host = stack.current;
    if (!host) return;
    const move = (ev: PointerEvent) => {
      const hr = host.getBoundingClientRect();
      curtain.onChange(Math.max(0, Math.min(1, (ev.clientX - hr.left) / Math.max(1, hr.width))));
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    move(e.nativeEvent);
  };

  const shown = message || error;
  return (
    <div className={`vf-pane${curtain ? " calque" : ""}`} data-testid={testid}>
      <div className="vf-tag">{tag}</div>
      <div className={`vf-view${overflow ? " reading" : ""}`} ref={box} onScroll={() => { onScroll(); if (curtain) placeKnob(); }} onDoubleClick={onDoubleClick}
        data-testid="vf-view" data-scale={scale ?? ""} data-ready={allReady && !shown ? "1" : "0"} data-fit={fit}
        title={onDoubleClick ? "Double-clic : plein écran · Ctrl + molette : zoomer" : "Ctrl + molette : zoomer"}>
        <div className="vf-pages" style={{ visibility: shown ? "hidden" : "visible", justifyContent: !overflow && fit !== "1:1" ? "center" : undefined }}>
          <div className="vf-stack" ref={stack}>
            {layers.map((l, i) => (
              <Layer key={i} src={l.src} scale={layerScale(i)} marks={l.marks} testid={l.testid} overlay={i > 0 && primary === 0}
                clip={curtain && i > 0 ? curtain.value : undefined}
                onDims={(d, err) => { setDims((x) => { const n = [...x]; n[i] = d; return n; }); setErrs((x) => { const n = [...x]; n[i] = err ? `Aperçu impossible : ${err}` : null; return n; }); }}
                onReady={(r) => setReady((x) => { if (x[i] === r) return x; const n = [...x]; n[i] = r; return n; })} />
            ))}
          </div>
        </div>
        {shown && <div className="msg">{shown}</div>}
        {!shown && primary >= 0 && !allReady && <div className="msg" style={{ background: "transparent", pointerEvents: "none" }}><span className="spinner" /></div>}
      </div>
      {curtain && knobX !== null && !shown && allReady && (
        <div className="vf-knob" style={{ left: knobX }} onPointerDown={drag} data-testid="vf-knob" role="slider" aria-label="Rideau original | soumis"
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(curtain.value * 100)} title="Glisser : original à gauche, soumis à droite (← →)">
          <i>⇆</i>
        </div>
      )}
    </div>
  );
});
