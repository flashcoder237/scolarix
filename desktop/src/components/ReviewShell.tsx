import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./ui";
import { useShortcuts } from "../state";
import { FIT_LABELS, ZONES, type PdfViewHandle, type PdfViewState } from "./PdfView";

// Gabarit plein cadre des revues (demande, cursus), sur le modèle du guichet « Aperçu d'abord » :
// barre unique en haut, colonne à gauche, bandeau + scène grise + dock à droite. Une vue, pas une fenêtre :
// elle couvre toute l'application (le rail ne sert pas pendant la préparation) ; Échap ramène où l'on était.

const EXTRA: Record<string, string> = {
  grip: '<circle cx="9" cy="6" r="1.3"/><circle cx="15" cy="6" r="1.3"/><circle cx="9" cy="12" r="1.3"/><circle cx="15" cy="12" r="1.3"/><circle cx="9" cy="18" r="1.3"/><circle cx="15" cy="18" r="1.3"/>',
  page1: '<rect x="6" y="3" width="12" height="18" rx="2"/>',
  pages: '<rect x="6" y="3" width="12" height="8" rx="1.5"/><rect x="6" y="13" width="12" height="8" rx="1.5"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  more: '<circle cx="6" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="18" cy="12" r="1.4"/>',
  hand: '<path d="M8 11V5.5a1.5 1.5 0 0 1 3 0V10m0-1V4.5a1.5 1.5 0 0 1 3 0V10m0-.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-1a6 6 0 0 1-4.6-2.2L3.6 14a1.5 1.5 0 0 1 2.3-1.9L8 14"/>',
};
export function RIcon({ name, size = 16 }: { name: string; size?: number }) {
  if (!EXTRA[name]) return <Icon name={name} size={size} />;
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round"
    strokeLinejoin="round" aria-hidden="true" dangerouslySetInnerHTML={{ __html: EXTRA[name] }} />;
}

/** Ctrl+1…5 (autre écran) pendant une revue : la revue se ferme d'abord, puis l'écran demandé s'affiche. */
export function useReviewNav(close: () => boolean | void) {
  const map: Record<string, () => boolean | void> = {};
  // close() renvoie false : la revue ne peut pas se fermer (opération en cours), la touche est ignorée
  for (let i = 1; i <= 5; i++) map[`ctrl+${i}`] = () => (close() === false ? undefined : false);
  useShortcuts(map);
}

export function ReviewShell({ label, testid, backLabel, onBack, icon, tone = "violet", eyebrow, title, mat, facts, center, right, side, children }: {
  label: string; testid: string; backLabel: string; onBack: () => void; icon: string; tone?: "violet" | "blue";
  eyebrow: ReactNode; title: ReactNode; mat?: string | null; facts?: ReactNode; center?: ReactNode; right?: ReactNode;
  side: ReactNode; children: ReactNode;
}) {
  const root = useRef<HTMLDivElement>(null);
  // le focus quitte le champ de recherche du guichet (sinon Espace, Échap… y seraient tapés), et y revient
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    root.current?.focus();
    return () => { if (before && document.contains(before)) before.focus(); };
  }, []);
  return createPortal(
    <div className="review" ref={root} tabIndex={-1} role="region" aria-label={label} data-testid={testid}>
      <header className="card rv-top">
        <button className="btn ghost sm rv-back" onClick={onBack} data-testid="review-back" title={`Retour (Échap)`}>
          <Icon name="arrow_left" size={16} />{backLabel}<span className="kbd">Échap</span></button>
        <span className={`rv-ico t-${tone}`}><Icon name={icon} size={18} /></span>
        <div className="rv-title">
          <span className="eyebrow">{eyebrow}</span>
          <span className="rv-h ellipsis">{title}</span>
        </div>
        {mat && <span className="rv-mat selectable">{mat}</span>}
        {facts && <span className="rv-facts muted small ellipsis opt-wide">{facts}</span>}
        <span className="grow" />
        {center}
        <span className="grow" />
        {right}
      </header>
      <div className="rv-body">
        <aside className="card rv-side" aria-label="Pièces">{side}</aside>
        <section className="gx-preview">{children}</section>
      </div>
    </div>,
    document.body,
  );
}

/** Groupe « Affichage » du dock : ajustement, zoom, plein écran, vue, pages, points de contrôle, mini-carte. */
export function ViewControls({ pv, pdf, onFocus, views, view, setView, pages, jump, setJump, setMm, zones = true, children }: {
  pv: PdfViewState; pdf: React.RefObject<PdfViewHandle | null>; onFocus?: () => void;
  views?: [string, string, string, string][]; view?: string; setView?: (v: string) => void;   // [id, icône, libellé, infobulle]
  pages?: number[]; jump?: number; setJump?: (p: number) => void; setMm: (el: HTMLDivElement | null) => void; zones?: boolean;
  children?: ReactNode;
}) {
  const cur = views?.find((v) => v[0] === view);
  return (
    <div className="grp">
      <span className="lbl row" style={{ gap: 4 }}><span className="grow">Affichage</span><span className="pct" data-testid="scale">{pv.scale ? `${Math.round(pv.scale * 100)} %` : ""}</span></span>
      <div className="fitgrid" role="radiogroup" aria-label="Ajustement de l'aperçu">
        {FIT_LABELS.map(([m, l, t]) => (
          <button key={m} className={`btn ghost xs${pv.fit === m ? " on" : ""}`} role="radio" aria-checked={pv.fit === m} title={t} onClick={() => pdf.current?.setFit(m)} data-testid={`fit-${m}`}>{l}</button>
        ))}
      </div>
      <div className="fitgrid three">
        <button className="btn ghost xs" aria-label="Zoom arrière" title="Zoom arrière (ou Ctrl + molette)" onClick={() => pdf.current?.zoomBy(1 / 1.2)}>−</button>
        <button className="btn ghost xs" aria-label="Zoom avant" title="Zoom avant (ou Ctrl + molette)" onClick={() => pdf.current?.zoomBy(1.2)}>+</button>
        <button className="btn ghost xs" disabled={!onFocus} onClick={onFocus} data-testid="focus-open" aria-label="Plein écran"
          title="Aperçu en plein écran (Entrée, F11 ou double-clic sur la page ; Échap pour sortir)"><Icon name="fit" size={14} /></button>
      </div>
      {views && setView && (
        <>
          <span className="lbl row" style={{ gap: 4 }}><span className="grow">Vue</span><span className="pct">{cur?.[2]}</span></span>
          <div className={`fitgrid${views.length === 3 ? " three" : ""}`} role="radiogroup" aria-label="Vue (V)">
            {views.map(([id, ic, l, t]) => (
              <button key={id} className={`btn ghost xs${view === id ? " on" : ""}`} role="radio" aria-checked={view === id} aria-label={l}
                title={`${t} (V)`} onClick={() => setView(id)} data-testid={`view-${id}`}><RIcon name={ic} size={14} /></button>
            ))}
          </div>
        </>
      )}
      {children}
      {pages && pages.length > 1 && setJump && (
        <>
          <span className="lbl">Pages</span>
          <div className="fitgrid">{pages.map((p) => (
            <button key={p} className={`btn ghost xs${jump === p ? " on" : ""}`} title={`Aller à la page ${p}`} onClick={() => setJump(p)}>p. {p}</button>
          ))}</div>
        </>
      )}
      {zones && <>
        <span className="lbl">Points de contrôle</span>
        {ZONES.map((z) => (
          <button key={z.id} className="btn ghost xs zone" onClick={() => pdf.current?.goZone(z.id)} title={`${z.label} : aller à la zone et la surligner (${z.key})`} data-testid={`zone-${z.id}`}>
            <Icon name={z.icon} size={13} /><span className="grow">{z.label}</span><span className="kbd">{z.key}</span>
          </button>
        ))}
      </>}
      <div className="mm-host" ref={setMm} />
    </div>
  );
}

/** Barre flottante du plein écran d'une revue (même grammaire que celle du guichet). */
export function ReviewFocusBar({ children, onExit }: { children: ReactNode; onExit: () => void }) {
  return (
    <div className="fbar" data-testid="focus-bar">
      {children}
      <button className="btn ghost sm" onClick={onExit} data-testid="focus-exit" title="Quitter le plein écran (Échap)"><Icon name="x" size={14} />Quitter<span className="kbd">Échap</span></button>
    </div>
  );
}

/** Bloc de progression d'une opération longue (conversion 3/7…), avec Arrêter au même endroit. */
export function OpProgress({ title, progress, cancelling, onCancel, testid }: {
  title: string; progress: { i: number; n: number; message: string; phase: string } | null; cancelling: boolean; onCancel: () => void; testid: string;
}) {
  const pct = progress && progress.n ? Math.round((100 * (progress.phase === "conversion" ? progress.i - 0.5 : progress.n)) / progress.n) : 8;
  return (
    <div className="rv-op" role="status" data-testid={testid}>
      <span className="spinner" />
      <div className="grow">
        <div className="t">{title}{progress && progress.n ? ` · ${progress.phase === "conversion" ? progress.i : progress.n}/${progress.n}` : ""}</div>
        <div className="m ellipsis" title={progress?.message}>{cancelling ? "Annulation… (arrêt avant le fichier suivant)" : progress?.message || "Démarrage…"}</div>
        <div className="bar"><div style={{ width: `${Math.max(4, Math.min(100, pct))}%` }} /></div>
      </div>
      <button className="btn ghost sm" disabled={cancelling} onClick={onCancel} data-testid={`${testid}-cancel`}><Icon name="x" size={14} />Arrêter</button>
    </div>
  );
}
