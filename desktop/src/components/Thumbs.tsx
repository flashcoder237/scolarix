import { useEffect, useRef, useState, type ReactNode } from "react";
import { openPdf } from "./PdfView";

// Vignettes des pages (revue de la demande : planche du dossier ; cursus : PDF fusionné page par page).
// Rendu pdf.js basse résolution, une vignette à la fois (l'interface ne se fige jamais), gardé en mémoire.

const rendered = new Map<string, string>();
let queue: Promise<unknown> = Promise.resolve();

function renderThumb(file: string, version: string, page: number, width: number): Promise<string> {
  const key = `${file}#${version}#${page}#${width}`;
  const hit = rendered.get(key);
  if (hit) return Promise.resolve(hit);
  const job = queue.then(async () => {
    const again = rendered.get(key);
    if (again) return again;
    const doc = await openPdf(file, version);
    if (page > doc.numPages) throw new Error(`page ${page} absente`);
    const p = await doc.getPage(page);
    const vp1 = p.getViewport({ scale: 1 });
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const vp = p.getViewport({ scale: (width * ratio) / vp1.width });
    const c = document.createElement("canvas");
    c.width = Math.floor(vp.width);
    c.height = Math.floor(vp.height);
    await p.render({ canvas: c, canvasContext: c.getContext("2d")!, viewport: vp }).promise;
    const url = c.toDataURL("image/png");
    rendered.set(key, url);
    while (rendered.size > 400) rendered.delete(rendered.keys().next().value as string);
    return url;
  });
  queue = job.catch(() => undefined);
  return job;
}

/** Nombre de pages d'un fichier (bordereau, PDF fusionné) ; null tant qu'il n'est pas connu. */
export function usePageCount(file: string | null | undefined, version = ""): number | null {
  const [n, setN] = useState<{ k: string; n: number } | null>(null);
  const k = `${file}#${version}`;
  useEffect(() => {
    if (!file) return;
    let alive = true;
    openPdf(file, version).then((d) => alive && setN({ k, n: d.numPages })).catch(() => undefined);
    return () => { alive = false; };
  }, [k]); // eslint-disable-line react-hooks/exhaustive-deps
  return n && n.k === k ? n.n : null;
}

export interface ThumbItem {
  key: string;
  file: string | null;        // null : page pas encore prête (attente)
  page: number;
  version?: string;
  label: ReactNode;           // « 1 · N1 · S1-S2 »
  sub?: ReactNode;            // année, état du contrôle…
  title?: string;
  group?: string;             // pièce à laquelle appartient la page (glisser-déposer : on déplace la pièce)
  draggable?: boolean;
}

function Thumb({ it, width, sel, onPick, onOpen, drag }: {
  it: ThumbItem; width: number; sel: boolean; onPick?: () => void; onOpen?: () => void;
  drag?: { onStart: () => void; onOver: () => void; onDrop: () => void; over: boolean };
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setUrl(null);
    setFailed(false);
    if (!it.file) return;
    let alive = true;
    renderThumb(it.file, it.version || "", it.page, width).then((u) => alive && setUrl(u)).catch(() => alive && setFailed(true));
    return () => { alive = false; };
  }, [it.file, it.version, it.page, width]);
  useEffect(() => { if (sel) ref.current?.scrollIntoView({ block: "nearest" }); }, [sel]);
  return (
    <div ref={ref} className={`thumb${sel ? " sel" : ""}${drag?.over ? " over" : ""}`} onClick={onPick} onDoubleClick={onOpen} title={it.title}
      role="option" aria-selected={sel} data-testid="thumb" draggable={!!drag && it.draggable}
      onDragStart={(e) => { if (!drag) return; e.dataTransfer.effectAllowed = "move"; drag.onStart(); }}
      onDragOver={(e) => { if (!drag) return; e.preventDefault(); drag.onOver(); }}
      onDrop={(e) => { if (!drag) return; e.preventDefault(); drag.onDrop(); }}>
      <div className="tpage" style={{ width, height: Math.round(width * 1.414) }}>
        {url ? <img src={url} alt="" draggable={false} /> : <span className={failed ? "muted tiny" : "spinner"}>{failed ? "aperçu impossible" : null}</span>}
      </div>
      <div className="tlab">{it.label}</div>
      {it.sub && <div className="tsub">{it.sub}</div>}
    </div>
  );
}

/** Colonne (cursus) ou planche (demande) de vignettes. Glisser une vignette sur une autre : onMove(groupe, groupe cible). */
export function Thumbs({ items, width, selected, onPick, onOpen, onMove, className = "", testid, label }: {
  items: ThumbItem[]; width: number; selected?: string | null; onPick?: (it: ThumbItem) => void; onOpen?: (it: ThumbItem) => void;
  onMove?: (from: string, to: string) => void; className?: string; testid?: string; label: string;
}) {
  const [dragFrom, setDragFrom] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  return (
    <div className={`thumbs ${className}`} role="listbox" aria-label={label} data-testid={testid} onDragEnd={() => { setDragFrom(null); setOver(null); }}>
      {items.map((it) => (
        <Thumb key={it.key} it={it} width={width} sel={selected === it.key || (!!it.group && selected === it.group)}
          onPick={onPick && (() => onPick(it))} onOpen={onOpen && (() => onOpen(it))}
          drag={onMove && it.group ? {
            over: !!dragFrom && over === it.group && dragFrom !== it.group,
            onStart: () => setDragFrom(it.group!),
            onOver: () => setOver(it.group!),
            onDrop: () => { if (dragFrom && it.group && dragFrom !== it.group) onMove(dragFrom, it.group); setDragFrom(null); setOver(null); },
          } : undefined} />
      ))}
    </div>
  );
}
