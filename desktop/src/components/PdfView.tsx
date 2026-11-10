import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from "react";
import { createPortal } from "react-dom";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { sx } from "../api";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

// Petit cache des documents ouverts (les octets sont en mémoire : aucun fichier n'est tenu ouvert,
// Word peut régénérer un aperçu pendant qu'il est affiché).
const cache = new Map<string, Promise<{ task: pdfjs.PDFDocumentLoadingTask; doc: pdfjs.PDFDocumentProxy }>>();
const CACHE_MAX = 10;        // revue d'une demande : bordereau + plusieurs relevés à la suite (vue Dossier, vignettes)
/** Document pdf.js partagé (aperçu, vignettes de la revue, planche). */
export function openPdf(file: string, version = "") { return open(file, version); }
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
    while (cache.size > CACHE_MAX) {
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

// ------------------------------------------------------------------ ajustement de la page
/** Auto : page entière si elle reste lisible, sinon zoom de lecture avec défilement. « libre » : Ctrl+molette, + / −. */
export type FitMode = "auto" | "page" | "largeur" | "1:1" | "libre";
export type Zone = "identite" | "notes" | "decision";
export const ZONES: { id: Zone; label: string; icon: string; key: string }[] = [
  { id: "identite", label: "Identité", icon: "user", key: "Alt+1" },
  { id: "notes", label: "Notes", icon: "filter", key: "Alt+2" },
  { id: "decision", label: "Décision", icon: "check", key: "Alt+3" },
];
const PT = 96 / 72;          // points PDF → pixels CSS : 100 % = taille réelle (A4 = 794 × 1123 px à 96 ppp)
export const READABLE = 0.68; // en dessous, le texte du tableau des notes passe sous ≈ 7,5 px : illisible
const READING = 0.9;          // zoom de lecture
const PAD = 12;               // marge autour des pages (CSS .viewer .pages)

export interface PdfViewState {
  fit: FitMode;
  scale: number | null;      // échelle réellement appliquée (1 = 100 %)
  overflow: boolean;         // la page dépasse la zone : défilement (zoom de lecture)
}
export interface PdfViewHandle {
  setFit: (m: FitMode) => void;
  zoomBy: (factor: number) => void;
  goZone: (z: Zone) => void;
}

function computeScale(fit: FitMode, manual: number, w: number, h: number, dims: { w: number; h: number } | null) {
  if (!dims || w < 50 || h < 50) return null;
  const W = Math.max(50, w - 2 * PAD), H = Math.max(50, h - 2 * PAD);
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

// ------------------------------------------------------------------ points de contrôle (couche texte pdf.js)
interface ZonePos { page: number; top: number; bottom: number }       // fractions de la hauteur de la page
type ZoneMap = Partial<Record<Zone, ZonePos>>;
const norm = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toUpperCase();
const RX: Record<Zone, RegExp> = {
  identite: /\bMATRICULE\b|\bNOMS?\s*(ET|&)\s*PRENOMS?\b|^\s*NOMS?\b/,
  notes: /^\s*CODE\b|UNITE\s+D.?\s*ENSEIGNEMENT/,
  decision: /DECISION/,
};
const RX_DEC_FALLBACK = /MOYENNE\s+(ANNUELLE|GENERALE|SEMESTRIELLE)|CREDITS?\s+CAPITALISES|TOTAL\s+CREDIT/;

async function findZones(doc: pdfjs.PDFDocumentProxy, from: number, to: number): Promise<ZoneMap> {
  const hits: Record<Zone | "dec2", { page: number; y: number }[]> = { identite: [], notes: [], decision: [], dec2: [] };
  for (let n = from; n <= to; n++) {
    const page = await doc.getPage(n);
    const vp = page.getViewport({ scale: 1 });
    const tc = await page.getTextContent();
    for (const it of tc.items) {
      if (!("str" in it) || !it.str.trim()) continue;
      const t = norm(it.str);
      const y = vp.convertToViewportPoint(it.transform[4], it.transform[5])[1] / vp.height;
      for (const z of ["identite", "notes", "decision"] as Zone[]) if (RX[z].test(t)) hits[z].push({ page: n, y });
      if (RX_DEC_FALLBACK.test(t)) hits.dec2.push({ page: n, y });
    }
  }
  const order = (a: { page: number; y: number }, b: { page: number; y: number }) => a.page - b.page || a.y - b.y;
  Object.values(hits).forEach((l) => l.sort(order));
  const id = hits.identite[0];
  const notes = hits.notes.find((h) => !id || order(h, id) > 0);
  const dec = hits.decision[hits.decision.length - 1] || hits.dec2[hits.dec2.length - 1];
  const out: ZoneMap = {};
  const sameP = (a?: { page: number }, b?: { page: number }) => !!a && !!b && a.page === b.page;
  if (id) out.identite = { page: id.page, top: id.y - 0.035, bottom: sameP(id, notes) ? Math.min(notes!.y - 0.012, id.y + 0.16) : id.y + 0.08 };
  if (notes) out.notes = { page: notes.page, top: notes.y - 0.025, bottom: sameP(notes, dec) && dec!.y > notes.y ? dec!.y - 0.035 : Math.min(0.97, notes.y + 0.5) };
  if (dec) out.decision = { page: dec.page, top: dec.y - 0.04, bottom: Math.min(0.99, dec.y + 0.04) };
  return out;
}
/** Repli quand la couche texte ne dit rien (scan sans OCR) : fractions habituelles d'un relevé FMSP. */
function fallbackZone(z: Zone, first: number, last: number): ZonePos {
  if (z === "identite") return { page: first, top: 0.12, bottom: 0.27 };
  if (z === "notes") return { page: first, top: 0.27, bottom: 0.66 };
  return { page: last, top: 0.68, bottom: 0.84 };
}

// ------------------------------------------------------------------ composant
/** Un document d'une suite (vue « Dossier » de la revue : bordereau puis relevés, dans l'ordre d'impression). */
export interface PdfDoc {
  key: string;
  file: string | null;     // null : pièce pas encore prête (message affiché à sa place)
  first: number;
  last: number;            // au-delà du nombre de pages : jusqu'à la dernière
  version?: string;
  caption?: string;        // légende au-dessus de la pièce
  message?: string | null;
  stamp?: boolean;
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
  defaultFit?: FitMode;    // « largeur » par défaut (écran Vérification) ; « auto » au guichet
  toolbar?: boolean;       // barre d'affichage sous l'aperçu (sinon : commandes portées par l'écran, via ref)
  minimapHost?: HTMLElement | null;   // où dessiner la mini-carte en zoom de lecture
  onState?: (s: PdfViewState) => void;
  onDoubleClick?: () => void;
  ref?: Ref<PdfViewHandle>;
  /** Plusieurs documents à la suite (remplacent file/first/last/version/stamp), légende par pièce. */
  docs?: PdfDoc[];
  selectedDoc?: string;    // pièce mise en évidence ; l'aperçu s'y rend quand elle change
  onPickDoc?: (key: string) => void;
  onVisible?: (doc: string, page: number) => void;   // page en haut de la zone (défilement)
}

export function PdfView({ file, first, last, version = "", message, stamp = false, onError, jumpTo, defaultFit = "largeur",
  toolbar = true, minimapHost, onState, onDoubleClick, ref, docs, selectedDoc, onPickDoc, onVisible }: PdfViewProps) {
  // un seul fichier (guichet, vérification) = une suite d'un document, sans légende ni enveloppe
  const multi = !!docs;
  const list: PdfDoc[] = useMemo(() => docs ?? (file ? [{ key: "", file, first, last, version, stamp }] : []),
    [docs, file, first, last, version, stamp]);
  const selIdx = Math.max(0, list.findIndex((d) => d.key === selectedDoc));
  const zoneDoc = list[selIdx]?.file ? list[selIdx] : list.find((d) => d.file) || null;
  const zoneIdx = zoneDoc ? list.indexOf(zoneDoc) : 0;
  const box = useRef<HTMLDivElement>(null);
  const pagesRef = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<FitMode>(defaultFit);
  const [manual, setManual] = useState(1);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [overflow, setOverflow] = useState(false);
  const [thumbs, setThumbs] = useState<string[]>([]);
  const [view, setView] = useState({ top: 0, height: 1, left: 0, width: 1 });
  const [shownKey, setShownKey] = useState("");     // document réellement dessiné (sinon : ancien relevé, estompé)
  const zones = useRef<ZoneMap>({});
  const pendingZone = useRef<Zone | null>(null);
  const docKey = list.map((d) => `${d.key}@${d.file}#${d.version || ""}#${d.first}-${d.last}#${d.message || ""}#${d.stamp ? 1 : 0}`).join("|");
  const dimsKey = zoneDoc ? `${zoneDoc.file}#${zoneDoc.version || ""}#${zoneDoc.first}-${zoneDoc.last}` : "";

  // la zone suit la largeur ET la hauteur (page entière, zoom de lecture)
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setSize((s) => (s.w === el.clientWidth && s.h === el.clientHeight ? s : { w: el.clientWidth, h: el.clientHeight }));
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, []);

  // autre relevé : en haut de la page ; le mode choisi (Auto, Page…) est gardé, un zoom manuel est oublié
  const lastFit = useRef<FitMode>(defaultFit);
  if (fit !== "libre") lastFit.current = fit;
  useEffect(() => { setFit(lastFit.current); if (!multi) box.current?.scrollTo({ top: 0, left: 0 }); }, [multi ? "" : file, first]); // eslint-disable-line react-hooks/exhaustive-deps

  const scale = useMemo(() => computeScale(fit, manual, size.w, size.h, dims), [fit, manual, size, dims]);
  const scaleRef = useRef(scale);
  scaleRef.current = scale;

  // Ctrl + molette : zoom du document (à partir de l'échelle affichée)
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const cur = scaleRef.current || 1;
      setManual(Math.max(0.25, Math.min(4, cur * (e.deltaY < 0 ? 1.15 : 1 / 1.15))));
      setFit("libre");
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, []);

  // document : dimensions de la première page et repérage des points de contrôle
  useEffect(() => {
    if (!zoneDoc || !zoneDoc.file || message) return;
    const z0 = zoneDoc;
    let cancelled = false;
    zones.current = {};
    (async () => {
      try {
        const doc = await open(z0.file!, z0.version || "");
        if (cancelled) return;
        if (z0.first > doc.numPages) throw new Error(`page ${z0.first} absente du document (${doc.numPages} pages)`);
        const page = await doc.getPage(Math.max(1, z0.first));
        const vp = page.getViewport({ scale: 1 });
        if (cancelled) return;
        setDims((d) => (d && d.w === vp.width && d.h === vp.height ? d : { w: vp.width, h: vp.height }));
        const to = Math.min(doc.numPages, Math.max(z0.first, z0.last));
        findZones(doc, Math.max(1, z0.first), to).then((z) => { if (!cancelled) zones.current = z; }).catch(() => undefined);
      } catch (e: unknown) {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : String(e);
        setErr(`Aperçu impossible : ${msg}`);
        onError?.(msg);
      }
    })();
    return () => { cancelled = true; };
  }, [dimsKey, message]); // eslint-disable-line react-hooks/exhaustive-deps

  const measureView = useCallback(() => {
    const el = box.current;
    if (!el) return;
    const H = Math.max(1, el.scrollHeight), W = Math.max(1, el.scrollWidth);
    setOverflow(el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2);
    setView({ top: el.scrollTop / H, height: el.clientHeight / H, left: el.scrollLeft / W, width: el.clientWidth / W });
  }, []);

  const applyZone = useCallback((z: Zone, smooth = true) => {
    const el = box.current, host = pagesRef.current;
    if (!el || !host) return;
    const zp = zones.current[z] || fallbackZone(z, zoneDoc?.first ?? first, zoneDoc?.last ?? last);
    const c = host.querySelector(`canvas[data-doc="${zoneIdx}"][data-page="${zp.page}"]`) as HTMLCanvasElement | null;
    const wrap = c?.parentElement;
    if (!wrap) return;
    const top = Math.max(0, Math.min(1, zp.top)), bottom = Math.max(top + 0.03, Math.min(1, zp.bottom));
    const y = wrap.offsetTop + top * wrap.offsetHeight - 14;
    el.scrollTo({ top: Math.max(0, y), behavior: smooth ? "smooth" : "auto" });
    wrap.querySelectorAll(".zone-hl").forEach((n) => n.remove());
    const hl = document.createElement("div");
    hl.className = "zone-hl";
    hl.dataset.zone = z;
    Object.assign(hl.style, { top: `${top * 100}%`, height: `${(bottom - top) * 100}%` });
    wrap.appendChild(hl);
    window.setTimeout(() => hl.remove(), 1900);
  }, [first, last, zoneDoc, zoneIdx]);

  useImperativeHandle(ref, () => ({
    setFit: (m) => { setFit(m); if (m !== "libre") box.current?.scrollTo({ top: 0, left: 0 }); },
    zoomBy: (f) => { setManual(Math.max(0.25, Math.min(4, (scaleRef.current || 1) * f))); setFit("libre"); },
    goZone: (z) => {
      // page entière illisible (petit écran, mode « Page ») : on passe au zoom de lecture avant d'y aller
      if ((scaleRef.current || 1) < READABLE && dims && size.w > 50) {
        const largeur = (size.w - 2 * PAD) / (dims.w * PT);
        pendingZone.current = z;
        setManual(Math.min(READING, Math.max(READABLE, largeur)));
        setFit("libre");
        return;
      }
      applyZone(z);
    },
  }), [applyZone, dims, size.w]);

  // rendu des pages à l'échelle calculée ; les anciennes restent affichées jusqu'au remplacement
  useEffect(() => {
    const host = pagesRef.current;
    if (!host || !list.length || message || !scale) return;
    let cancelled = false;
    const tasks: pdfjs.RenderTask[] = [];
    setErr(null);
    setLoading(true);
    (async () => {
      try {
        const ratio = window.devicePixelRatio || 1;
        const canvases: HTMLCanvasElement[] = [];
        const blocks: HTMLElement[] = [];
        for (let k = 0; k < list.length; k++) {
          const d = list[k];
          const pages: HTMLElement[] = [];
          if (d.file) {
            const doc = await open(d.file, d.version || "");
            if (cancelled) return;
            const from = Math.max(1, d.first), to = Math.min(doc.numPages, Math.max(d.first, d.last));
            if (from > doc.numPages) throw new Error(`page ${d.first} absente du document (${doc.numPages} pages)`);
            for (let n = from; n <= to; n++) {
              const page = await doc.getPage(n);
              if (cancelled) return;
              const vp = page.getViewport({ scale: scale * PT * ratio });
              const c = document.createElement("canvas");
              c.width = Math.floor(vp.width);
              c.height = Math.floor(vp.height);
              c.style.width = `${Math.floor(vp.width / ratio)}px`;
              c.style.height = `${Math.floor(vp.height / ratio)}px`;
              c.dataset.page = String(n);
              c.dataset.doc = String(k);
              const t = page.render({ canvas: c, canvasContext: c.getContext("2d")!, viewport: vp });
              t.promise.catch(() => undefined);      // rendu annulé (autre pièce, autre échelle) : jamais une erreur non traitée
              tasks.push(t);
              canvases.push(c);
              const wrap = document.createElement("div");
              wrap.className = "pg";
              wrap.appendChild(c);
              if (d.stamp) {
                const s = document.createElement("div");
                s.className = "stamp-overlay";
                s.textContent = "DUPLICATA CONFORME";
                Object.assign(s.style, { right: "8%", top: "6%", border: "3px solid #B91C1C", color: "#B91C1C", borderRadius: "8px",
                  padding: "4px 10px", font: `700 ${Math.max(10, Math.round(14 * scale * PT))}px Inter`, transform: "rotate(-8deg)", opacity: "0.8", background: "rgba(255,255,255,.6)" });
                wrap.appendChild(s);
              }
              pages.push(wrap);
            }
          } else {
            // pièce pas encore prête : un emplacement de la taille d'une page, avec son message
            const ph = document.createElement("div");
            ph.className = "pg pg-wait";
            const w = (dims?.w || 595) * scale * PT, h = (dims?.h || 842) * scale * PT;
            Object.assign(ph.style, { width: `${Math.floor(w)}px`, height: `${Math.floor(Math.min(h, 260))}px` });
            ph.textContent = d.message || "Aperçu en préparation…";
            pages.push(ph);
          }
          if (multi) {
            const b = document.createElement("div");
            b.className = `pdoc${d.key === selectedDoc ? " sel" : ""}`;
            b.dataset.key = d.key;
            if (d.caption) {
              const cap = document.createElement("div");
              cap.className = "pcap";
              cap.textContent = d.caption;
              b.appendChild(cap);
            }
            pages.forEach((p) => b.appendChild(p));
            if (onPickDoc) b.addEventListener("click", () => onPickRef.current?.(d.key));
            blocks.push(b);
          } else blocks.push(...pages);
        }
        await Promise.all(tasks.map((t) => t.promise));
        if (cancelled) return;
        host.replaceChildren(...blocks);
        setLoading(false);
        setShownKey(docKey);
        measureView();
        if (pendingZone.current) { const z = pendingZone.current; pendingZone.current = null; applyZone(z, false); }
        if (multi && selectedDoc && scrolledTo.current !== selectedDoc) { scrolledTo.current = selectedDoc; scrollToDoc(selectedDoc, false); }
        // vignettes de la mini-carte : copies réduites des pages déjà dessinées (aucun rendu pdf.js de plus)
        if (minimapHost !== undefined) {
          setThumbs(canvases.map((c) => {
            const t = document.createElement("canvas");
            t.width = 200;
            t.height = Math.round((200 * c.height) / Math.max(1, c.width));
            t.getContext("2d")!.drawImage(c, 0, 0, t.width, t.height);
            return t.toDataURL("image/png");
          }));
        }
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
  }, [docKey, message, scale, onError]); // eslint-disable-line react-hooks/exhaustive-deps

  // vue Dossier : la pièce choisie dans la liste est mise en évidence et amenée en haut de la zone
  const onPickRef = useRef(onPickDoc);
  onPickRef.current = onPickDoc;
  const scrolledTo = useRef<string | undefined>(undefined);
  const scrollToDoc = useCallback((key: string, smooth = true) => {
    const el = box.current, host = pagesRef.current;
    const b = host?.querySelector(`.pdoc[data-key="${CSS.escape(key)}"]`) as HTMLElement | null;
    if (!el || !b) return;
    el.scrollTo({ top: Math.max(0, b.offsetTop - 4), behavior: smooth ? "smooth" : "auto" });
  }, []);
  useEffect(() => {
    if (!multi) return;
    pagesRef.current?.querySelectorAll(".pdoc").forEach((b) => b.classList.toggle("sel", (b as HTMLElement).dataset.key === selectedDoc));
    if (selectedDoc && scrolledTo.current !== selectedDoc && shownKey === docKey) { scrolledTo.current = selectedDoc; scrollToDoc(selectedDoc); }
  }, [selectedDoc, multi, shownKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!jumpTo || !pagesRef.current) return;
    const c = pagesRef.current.querySelector(`canvas[data-doc="${zoneIdx}"][data-page="${jumpTo}"]`);
    c?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [jumpTo]);

  useEffect(() => { measureView(); }, [size, measureView]);
  useEffect(() => { onState?.({ fit, scale, overflow }); }, [fit, scale, overflow, onState]);

  const onScroll = useRef(0);
  const scrolled = () => {
    if (onScroll.current) return;
    onScroll.current = requestAnimationFrame(() => { onScroll.current = 0; measureView(); reportVisible(); });
  };
  // page en haut de la zone (vignettes et « Page 3 / 8 » de la revue suivent le défilement)
  const lastVisible = useRef("");
  const reportVisible = () => {
    const el = box.current, host = pagesRef.current;
    if (!onVisible || !el || !host) return;
    const y = el.scrollTop + el.clientHeight * 0.3;
    let hit: HTMLCanvasElement | null = null;
    for (const c of host.querySelectorAll("canvas")) {
      const wrap = c.parentElement as HTMLElement;
      if (wrap.offsetTop <= y) hit = c as HTMLCanvasElement; else break;
    }
    if (!hit) return;
    const doc = list[Number(hit.dataset.doc)]?.key ?? "";
    const k = `${doc}#${hit.dataset.page}`;
    if (k === lastVisible.current) return;
    lastVisible.current = k;
    onVisible(doc, Number(hit.dataset.page));
  };

  const shown = message || err || (!list.length ? (multi ? "Aucune pièce à afficher." : "L'aperçu du relevé sélectionné s'affiche ici.") : null);
  const reading = overflow && !shown;
  // jamais la page d'un autre relevé sous la fiche : tant que le nouveau n'est pas dessiné, l'ancien est estompé
  const stale = shownKey !== docKey;
  const ready = !shown && !stale && !loading;
  const minimap = minimapHost && reading && !stale && thumbs.length > 0 ? createPortal(
    <MiniMap thumbs={thumbs} view={view} onJump={(fy, fx) => {
      const el = box.current;
      if (!el) return;
      el.scrollTo({ top: fy * el.scrollHeight - el.clientHeight / 2, left: fx * el.scrollWidth - el.clientWidth / 2, behavior: "smooth" });
    }} />, minimapHost) : null;

  return (
    <div className="col grow" style={{ gap: 8, minHeight: 0 }}>
      <div className={`viewer${reading ? " reading" : ""}`} ref={box} data-testid="viewer" data-fit={fit} data-scale={scale ?? ""} data-ready={ready ? "1" : "0"}
        title={onDoubleClick ? "Double-clic : plein écran · Ctrl + molette : zoomer" : "Ctrl + molette : zoomer le document"}
        onScroll={scrolled} onDoubleClick={onDoubleClick}>
        <div className={`pages${stale ? " stale" : ""}`} ref={pagesRef} style={{ visibility: shown ? "hidden" : "visible" }} />
        {shown && <div className="msg">{loading && !err ? <span className="dots">{shown}</span> : shown}</div>}
        {!shown && loading && <div className="msg" style={{ background: "transparent", pointerEvents: "none" }}><span className="spinner" /></div>}
      </div>
      {toolbar && <FitBar fit={fit} scale={scale} onFit={(m) => setFit(m)} onZoom={(f) => { setManual(Math.max(0.25, Math.min(4, (scale || 1) * f))); setFit("libre"); }} />}
      {minimap}
    </div>
  );
}

function MiniMap({ thumbs, view, onJump }: { thumbs: string[]; view: { top: number; height: number; left: number; width: number }; onJump: (fy: number, fx: number) => void }) {
  return (
    <div className="minimap" data-testid="minimap" title="Mini-carte : cliquer pour aller à cet endroit de la page"
      onMouseDown={(e) => { const r = e.currentTarget.getBoundingClientRect(); onJump((e.clientY - r.top) / r.height, (e.clientX - r.left) / r.width); }}>
      {thumbs.map((t, i) => <img key={i} src={t} alt="" draggable={false} />)}
      <div className="win" style={{ top: `${view.top * 100}%`, height: `${Math.min(1, view.height) * 100}%`, left: `${view.left * 100}%`, width: `${Math.min(1, view.width) * 100}%` }} />
    </div>
  );
}

export const FIT_LABELS: [FitMode, string, string][] = [
  ["auto", "Auto", "Page entière si elle est lisible, sinon zoom de lecture"],
  ["page", "Page", "Page entière"],
  ["largeur", "Largeur", "Ajuster à la largeur"],
  ["1:1", "1:1", "Taille réelle (100 %)"],
];

/** Barre d'affichage compacte (écran Vérification). */
export function FitBar({ fit, scale, onFit, onZoom }: { fit: FitMode; scale: number | null; onFit: (m: FitMode) => void; onZoom: (f: number) => void }) {
  return (
    <div className="row small" style={{ justifyContent: "flex-end", gap: 6 }}>
      <span className="muted tiny">{scale ? `${Math.round(scale * 100)} %` : ""}</span>
      <div className="seg sm" role="radiogroup" aria-label="Ajustement de l'aperçu">
        {FIT_LABELS.map(([m, l, t]) => <button key={m} className={fit === m ? "on" : ""} role="radio" aria-checked={fit === m} title={t} onClick={() => onFit(m)}>{l}</button>)}
      </div>
      <button className="btn ghost sm" aria-label="Zoom arrière" onClick={() => onZoom(1 / 1.2)}>−</button>
      <button className="btn ghost sm" aria-label="Zoom avant" onClick={() => onZoom(1.2)}>+</button>
    </div>
  );
}
