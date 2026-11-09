import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ICONS } from "../icons";
import type { Tone } from "../api";

// ------------------------------------------------------------------ icônes
export function Icon({ name, size = 18, color = "currentColor", stroke = 1.8 }: { name: string; size?: number; color?: string; stroke?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={stroke}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: ICONS[name] || ICONS.info }} />
  );
}

export function Chip({ icon, tone = "blue", size = 40 }: { icon: string; tone?: Tone; size?: number }) {
  return <div className={`chip t-${tone}`} style={{ width: size, height: size }}><Icon name={icon} size={size * 0.48} /></div>;
}

export function Badge({ text, tone = "gray", big = false, dot = false, title }: { text: string; tone?: Tone; big?: boolean; dot?: boolean; title?: string }) {
  return <span className={`badge t-${tone}${big ? " big" : ""}`} title={title}>{dot && <span className="dot" />}{text}</span>;
}

export function Alert({ text, tone }: { text: string; tone: Tone }) {
  const icon = tone === "red" ? "alert" : tone === "green" ? "check" : tone === "amber" ? "alert" : "info";
  return <div className={`alert t-${tone}`} role={tone === "red" ? "alert" : undefined}><Icon name={icon} size={16} />{text}</div>;
}

export function Seg<T extends string | number | boolean>({ value, options, onChange, label }: {
  value: T; options: [T, string][]; onChange: (v: T) => void; label?: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map(([v, l]) => (
        <button key={String(v)} className={v === value ? "on" : ""} role="radio" aria-checked={v === value}
          onClick={() => onChange(v)}>{l}</button>
      ))}
    </div>
  );
}

export function Switch({ checked, onChange, label, blue = false, title }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; blue?: boolean; title?: string }) {
  return (
    <label className={`switch${blue ? " blue" : ""}`} title={title}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="track" />{label}
    </label>
  );
}

export function Dots({ text }: { text: string }) {
  return <span className="dots">{text}</span>;
}

// ------------------------------------------------------------------ sons discrets
let soundsOn = true;
const SOUNDS: Record<string, HTMLAudioElement> = {};
export function configureSounds(on: boolean) { soundsOn = on; }
export function play(name: "succes" | "impression" | "attention" | "erreur" | "ajout") {
  if (!soundsOn) return;
  try {
    const a = SOUNDS[name] || (SOUNDS[name] = new Audio(`./sounds/${name}.wav`));
    a.volume = 0.35;
    a.currentTime = 0;
    void a.play().catch(() => undefined);
  } catch { /* pas de carte son : sans importance */ }
}

// ------------------------------------------------------------------ messages flottants
export interface ToastOpts { tone?: Tone; ms?: number; action?: { label: string; run: () => void }; sound?: boolean; key?: string }
interface ToastItem extends ToastOpts { id: number; text: string }
const ToastCtx = createContext<(text: string, opts?: ToastOpts) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const show = useCallback((text: string, opts: ToastOpts = {}) => {
    const id = ++seq.current;
    const tone = opts.tone || "green";
    // même clé : le message remplace le précédent (« Préparation… » devient « Exporté »)
    setItems((xs) => [...xs.filter((x) => !(opts.key && x.key === opts.key)).slice(-1), { ...opts, tone, id, text }]);
    if (opts.sound !== false) play(tone === "red" ? "erreur" : tone === "amber" ? "attention" : "succes");
    const ms = opts.ms ?? (tone === "red" ? 15000 : 8000);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), ms);
  }, []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast t-${t.tone}`} data-testid="toast">
            <Icon name={t.tone === "red" || t.tone === "amber" ? "alert" : t.tone === "blue" ? "info" : "check"} size={18} />
            <span className="msg selectable">{t.text}</span>
            {t.action && <button className="btn sm secondary" onClick={() => { t.action!.run(); setItems((xs) => xs.filter((x) => x.id !== t.id)); }}>{t.action.label}</button>}
            <button className="btn link tiny" aria-label="Fermer" onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}><Icon name="x" size={14} /></button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ------------------------------------------------------------------ fenêtres modales
const modalStack: object[] = [];     // Échap ne ferme que la fenêtre du dessus (une question posée par-dessus les Réglages)
export function Modal({ children, onClose, width = 560, label }: { children: ReactNode; onClose: () => void; width?: number; label: string }) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const me = {};
    modalStack.push(me);
    const k = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || modalStack[modalStack.length - 1] !== me) return;
      e.stopPropagation();
      close.current();
    };
    window.addEventListener("keydown", k, true);
    return () => { window.removeEventListener("keydown", k, true); const i = modalStack.indexOf(me); if (i >= 0) modalStack.splice(i, 1); };
  }, []);
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={label} style={{ width }}>{children}</div>
    </div>
  );
}

// Questions : « ask » (boutons explicites) et « prompt » (saisie), sous forme de promesses.
type Dialog =
  | { kind: "ask"; title: string; text: string; details?: string; choices: string[]; danger?: boolean; resolve: (i: number) => void }
  | { kind: "prompt"; title: string; label: string; value: string; resolve: (v: string | null) => void };
const DialogCtx = createContext<{ ask: (o: Omit<Extract<Dialog, { kind: "ask" }>, "kind" | "resolve">) => Promise<number>; prompt: (title: string, label: string, value?: string) => Promise<string | null> }>({
  ask: async () => -1, prompt: async () => null,
});
export const useDialogs = () => useContext(DialogCtx);

export function DialogHost({ children }: { children: ReactNode }) {
  const [d, setD] = useState<Dialog | null>(null);
  const ask = useCallback((o: Omit<Extract<Dialog, { kind: "ask" }>, "kind" | "resolve">) =>
    new Promise<number>((resolve) => setD({ ...o, kind: "ask", resolve })), []);
  const prompt = useCallback((title: string, label: string, value = "") =>
    new Promise<string | null>((resolve) => setD({ kind: "prompt", title, label, value, resolve })), []);
  return (
    <DialogCtx.Provider value={{ ask, prompt }}>
      {children}
      {d?.kind === "ask" && (
        <Modal label={d.title} onClose={() => { d.resolve(-1); setD(null); }} width={520}>
          <div className="row"><Chip icon="alert" tone={d.danger ? "red" : "amber"} /><h2 className="h2">{d.title}</h2></div>
          <div className="selectable" style={{ whiteSpace: "pre-line", fontWeight: 600 }}>{d.text}</div>
          {d.details && <div className="muted small">{d.details}</div>}
          <div className="foot">
            <button className="btn ghost" onClick={() => { d.resolve(-1); setD(null); }}>Annuler</button>
            {d.choices.map((c, i) => (
              <button key={c} autoFocus={i === 0} className={`btn ${i === 0 ? (d.danger ? "danger" : "primary") : "secondary"}`}
                onClick={() => { d.resolve(i); setD(null); }}>{c}</button>
            ))}
          </div>
        </Modal>
      )}
      {d?.kind === "prompt" && <PromptDialog d={d} close={() => setD(null)} />}
    </DialogCtx.Provider>
  );
}

function PromptDialog({ d, close }: { d: Extract<Dialog, { kind: "prompt" }>; close: () => void }) {
  const [v, setV] = useState(d.value);
  const done = (val: string | null) => { d.resolve(val); close(); };
  return (
    <Modal label={d.title} onClose={() => done(null)} width={460}>
      <h2 className="h2">{d.title}</h2>
      <label className="lbl">{d.label}
        <input className="field" autoFocus value={v} onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") done(v); }} />
      </label>
      <div className="foot">
        <button className="btn ghost" onClick={() => done(null)}>Annuler</button>
        <button className="btn primary" onClick={() => done(v)}>Valider</button>
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------------ divers
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = window.setTimeout(() => setV(value), ms); return () => window.clearTimeout(t); }, [value, ms]);
  return v;
}

export function highlight(text: string, q: string): ReactNode {
  const words = q.trim().split(/\s+/).filter((w) => w.length >= 2);
  if (!words.length || !text) return text;
  const norm = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const nt = norm(text);
  const marks: [number, number][] = [];
  for (const w of words) {
    const nw = norm(w);
    let i = nt.indexOf(nw);
    while (i >= 0) { marks.push([i, i + nw.length]); i = nt.indexOf(nw, i + nw.length); }
  }
  if (!marks.length) return text;
  marks.sort((a, b) => a[0] - b[0]);
  const out: ReactNode[] = [];
  let pos = 0;
  marks.forEach(([a, b], k) => {
    if (a < pos) return;
    out.push(text.slice(pos, a), <mark key={k}>{text.slice(a, b)}</mark>);
    pos = b;
  });
  out.push(text.slice(pos));
  return out;
}

export function dateFr(d = new Date()) {
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}
