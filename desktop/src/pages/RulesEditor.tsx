import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { call, type Interligne, type TextRule } from "../api";
import { Badge, Icon, Switch, useDebounced, useDialogs } from "../components/ui";

// Réglages > Remplacements (Word) : liste ordonnée des règles | règle choisie | aperçu AVANT / APRÈS sur l'en-tête
// d'un vrai relevé Word (rendu typographique immédiat, puis rendu du moteur : copie corrigée convertie par Word ou
// LibreOffice) ou sur un texte libre (l'« Essai » d'avant). Rien n'est enregistré avant « Enregistrer ».

const FONTS = ["Arial", "Arial Narrow", "Book Antiqua", "Bookman Old Style", "Calibri", "Cambria", "Century Gothic",
  "Garamond", "Georgia", "Segoe UI", "Tahoma", "Times New Roman", "Verdana"];
const SAMPLE = "Tel  : 33 01 41 39  / 33 01 41 29.  RELEVE DE NOTES /TRANSCRIPT   FMSP/VDAAP/VDSSSE/SSSE";
// Interligne d'une règle : « » = d'origine, multiple, ou valeur en points (exact / au moins)
const lsKind = (x: Interligne | undefined) => (x == null ? "" : typeof x === "string" ? x : "exact" in x ? "exact" : "au_moins");
const lsPt = (x: Interligne | undefined) => (x && typeof x === "object" ? ("exact" in x ? x.exact : x.au_moins) : undefined);
const lsMake = (kind: string, pt: number | undefined): Interligne =>
  kind === "" ? null : kind === "exact" ? { exact: pt || 12 } : kind === "au_moins" ? { au_moins: pt || 12 } : (kind as Interligne);
const short = (s: string, n = 26) => (s.length > n ? `${s.slice(0, n)}…` : s);

interface Seg { t: string; k: "eq" | "del" | "ins"; size?: number; bold?: boolean; italic?: boolean; font?: string }
interface Par { align: string; seg: Seg[] }
interface Img { img: string; w: number; h: number; w_pt: number; h_pt: number; frac: number; rects: { k: "del" | "ins"; x: number; y: number; w: number; h: number }[] }
interface Preview {
  mode: "texte" | "releve"; avant: Par[]; apres: Par[]; text?: string; count: number | null; regles: number; avertissements: string[]; ecartees: string[];
  releve?: string; releve_id?: number; attente?: string; motif?: string | null; rendu?: { avant: Img; apres: Img } | null; sans_convertisseur?: boolean; stale?: boolean; convertisseur?: string;
}
interface Cand { id: number; label: string; nom: string; fichier: string }
// choix d'essai gardé d'une ouverture des Réglages à l'autre
let lastSource: number | "texte" | null = null;
let lastSample = SAMPLE;

export function RulesEditor({ rules, onRules, flash }: { rules: TextRule[]; onRules: (r: TextRule[]) => void; flash: string | null }) {
  const { ask } = useDialogs();
  const [open, setOpen] = useState<number | null>(rules.length ? 0 : null);
  const [drag, setDrag] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => { if (flash?.startsWith("rule:")) setOpen(Number(flash.slice(5))); }, [flash]);
  useEffect(() => { if (open != null && open >= rules.length) setOpen(rules.length ? rules.length - 1 : null); }, [rules.length, open]);

  const setRule = (i: number, patch: Partial<TextRule>) => onRules(rules.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  const moveTo = (i: number, j: number) => {
    if (j < 0 || j >= rules.length || i === j) return;
    const next = [...rules];
    const [r] = next.splice(i, 1);
    next.splice(j, 0, r);
    onRules(next);
    setOpen(j);
  };
  const add = () => { onRules([...rules, { actif: true, description: "Nouvelle règle", chercher: "", remplacer: "", regex: false }]); setOpen(rules.length); };
  const remove = async (i: number) => {
    const r = rules[i];
    if (r.chercher.trim()) {
      const k = await ask({ title: "Supprimer la règle", text: `« ${r.description || r.chercher} » ne sera plus appliquée.`, details: "La suppression ne devient définitive qu'avec « Enregistrer ».", choices: ["Supprimer"], danger: true });
      if (k !== 0) return;
    }
    onRules(rules.filter((_, k) => k !== i));
    setOpen(rules.length > 1 ? Math.max(0, i - 1) : null);
  };
  const restore = async () => {
    const k = await ask({ title: "Rétablir les règles d'origine", text: "Les règles actuelles seront remplacées par les 3 règles livrées avec Scolarix (ancien téléphone, « Ref N° », référence des services).", details: "Rien n'est perdu tant que vous n'avez pas cliqué sur « Enregistrer ».", choices: ["Rétablir"] });
    if (k !== 0) return;
    onRules(await call<TextRule[]>("rules_defaults"));
    setOpen(0);
  };

  // Ctrl+N nouvelle règle, Alt+↑ / Alt+↓ déplacer la règle choisie (fenêtre des Réglages au premier plan)
  const keys = useRef({ add, moveTo, open });
  keys.current = { add, moveTo, open };
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (document.querySelectorAll(".overlay").length > 1) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") { e.preventDefault(); keys.current.add(); }
      else if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && keys.current.open != null) {
        e.preventDefault();
        keys.current.moveTo(keys.current.open, keys.current.open + (e.key === "ArrowUp" ? -1 : 1));
      }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);

  const r = open != null ? rules[open] : null;
  const kind = r ? lsKind(r.interligne) : "";
  return (
    <div className="rr" data-testid="rules">
      <div className="rr-list" ref={listRef}>
        <div className="rr-items" role="listbox" aria-label="Règles (ordre d'application)">
          {rules.map((x, i) => (
            <div key={i} role="option" aria-selected={open === i} tabIndex={0} draggable
              className={`rr-item${open === i ? " on" : ""}${x.actif === false ? " off" : ""}${over === i && drag !== null && drag !== i ? (drag < i ? " drop-after" : " drop-before") : ""}`}
              onClick={() => setOpen(i)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(i); } }}
              onDragStart={(e) => { setDrag(i); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(i)); }}
              onDragOver={(e) => { e.preventDefault(); setOver(i); }} onDragLeave={() => setOver((o) => (o === i ? null : o))}
              onDrop={(e) => { e.preventDefault(); if (drag !== null) moveTo(drag, i); setDrag(null); setOver(null); }} onDragEnd={() => { setDrag(null); setOver(null); }}
              data-testid={`rule-${i}`} title={`${x.chercher} → ${x.remplacer || "(supprimé)"}`}>
              <span className="rr-grip" aria-hidden="true">⠿</span>
              <span onClick={(e) => e.stopPropagation()}><Switch blue checked={x.actif !== false} onChange={(v) => setRule(i, { actif: v })} label="" title="Règle active" /></span>
              <span className="rr-txt">
                <b className="ellipsis">{i + 1}. {x.description || "Règle sans nom"}</b>
                <span className="mono tiny ellipsis">{x.actif === false ? `${short(x.chercher || "…", 18)} → (désactivée)` : `${short(x.chercher || "rien à chercher", 18)} → ${x.remplacer ? short(x.remplacer, 14) : "supprimé"}${x.taille ? ` · ${x.taille} pt` : ""}`}</span>
              </span>
            </div>
          ))}
          {!rules.length && <div className="muted small" style={{ padding: 10 }}>Aucune règle : les relevés Word sont montrés tels quels.</div>}
        </div>
        <div className="row wrap" style={{ gap: 6 }}>
          <button className="btn secondary sm" onClick={add} data-testid="rule-add" title="Nouvelle règle (Ctrl+N)"><Icon name="plus" size={14} />Ajouter une règle</button>
          <button className="btn ghost sm" onClick={() => void restore()} data-testid="rules-restore">Rétablir d'origine…</button>
        </div>
        <div className="tiny muted">Glisser <span aria-hidden="true">⠿</span> pour changer l'ordre · <span className="key">Alt</span><span className="key">↑</span><span className="key">↓</span></div>
      </div>

      <div className="rr-right">
        {r && open != null ? (
          <div className="rr-editor" data-testid="rule-editor">
            <div className="rr-row1">
              <label className="lbl">Description<input className="field" value={r.description || ""} onChange={(e) => setRule(open, { description: e.target.value })} data-testid="rule-description" /></label>
              <label className="lbl"><span className="row" style={{ gap: 6 }}>Chercher
                <button type="button" className={`rr-regex${r.regex ? " on" : ""}`} aria-pressed={!!r.regex} onClick={(e) => { e.preventDefault(); setRule(open, { regex: !r.regex }); }}
                  title={r.regex ? "Expression régulière (cliquer : texte simple)" : "Texte simple : les espaces acceptent n'importe quel nombre d'espaces (cliquer : expression régulière)"} data-testid="rule-regex">{r.regex ? "REGEX" : "TEXTE"}</button></span>
                <input className="field mono" value={r.chercher} onChange={(e) => setRule(open, { chercher: e.target.value })} data-testid="rule-chercher" /></label>
              <label className="lbl">Remplacer par (vide = supprimer)<input className="field mono" value={r.remplacer} onChange={(e) => setRule(open, { remplacer: e.target.value })} data-testid="rule-remplacer" /></label>
              <button className="btn ghost icon rr-del" aria-label="Supprimer la règle" title="Supprimer la règle" onClick={() => void remove(open)} data-testid="rule-remove"><Icon name="trash" size={16} /></button>
            </div>
            <div className="rr-band">
              <div className="rr-bandh">
                <span>Mise en forme du texte {r.style_sur === "tout" ? "de remplacement" : "ajouté"}</span>
                <span className="rr-sauf" title="La règle est ignorée pour un en-tête qui contient déjà ce texte (un relevé déjà corrigé à la main ne reçoit pas un second « Ref N° »).">· Sauf si déjà présent :
                  <input className="field mono" value={r.sauf_si || ""} placeholder="(facultatif)" onChange={(e) => setRule(open, { sauf_si: e.target.value })} data-testid="rule-sauf" /></span>
              </div>
              <div className="rr-fmt">
                <label className="lbl">Police<input className="field" list="scolarix-fonts" style={{ width: 112 }} value={r.police || ""} placeholder="d'origine" onChange={(e) => setRule(open, { police: e.target.value || null })} /></label>
                <label className="lbl">Taille<input className="field" style={{ width: 64 }} type="number" min={5} max={30} step={0.5} value={r.taille ?? ""} placeholder="—" onChange={(e) => setRule(open, { taille: e.target.value ? Number(e.target.value) : null })} data-testid="rule-taille" /></label>
                <Tri label="Gras" value={r.gras} onChange={(x) => setRule(open, { gras: x })} />
                <Tri label="Italique" value={r.italique} onChange={(x) => setRule(open, { italique: x })} />
                <div className="lbl" title="« Texte ajouté » : seul ce qui change est mis en forme (« Ref N° » ajouté devant le titre ne réduit pas le titre). « Tout » : pour changer la police ou la taille d'un texte existant (le remplacer par lui-même).">S'applique à
                  <div className="seg sm"><button className={r.style_sur !== "tout" ? "on" : ""} onClick={() => setRule(open, { style_sur: null })}>Texte ajouté</button><button className={r.style_sur === "tout" ? "on" : ""} onClick={() => setRule(open, { style_sur: "tout" })}>Tout</button></div></div>
                <label className="lbl" title="S'applique au seul paragraphe qui reçoit le remplacement, jamais aux autres. Vide = celui du document.">Interligne
                  <span className="row" style={{ gap: 4 }}>
                    <select className="field" value={kind} onChange={(e) => setRule(open, { interligne: lsMake(e.target.value, lsPt(r.interligne)) })} data-testid="rule-interligne">
                      <option value="">d'origine</option><option value="simple">simple</option><option value="1.15">1,15</option><option value="1.5">1,5</option>
                      <option value="double">double</option><option value="exact">exactement</option><option value="au_moins">au moins</option></select>
                    {(kind === "exact" || kind === "au_moins") && <input className="field" style={{ width: 62 }} type="number" min={1} max={200} step={0.5} value={lsPt(r.interligne) ?? ""} aria-label="Interligne (pt)"
                      onChange={(e) => setRule(open, { interligne: lsMake(kind, e.target.value ? Number(e.target.value) : undefined) })} />}
                  </span></label>
                <label className="lbl" title="Espace avant / après le paragraphe modifié (pt). Vide = d'origine.">Espace av./ap.
                  <span className="row" style={{ gap: 4 }}>
                    <input className="field" style={{ width: 50 }} type="number" min={0} max={200} step={0.5} value={r.espace_avant ?? ""} placeholder="—" aria-label="Espace avant (pt)"
                      onChange={(e) => setRule(open, { espace_avant: e.target.value ? Number(e.target.value) : null })} />
                    <input className="field" style={{ width: 50 }} type="number" min={0} max={200} step={0.5} value={r.espace_apres ?? ""} placeholder="—" aria-label="Espace après (pt)"
                      onChange={(e) => setRule(open, { espace_apres: e.target.value ? Number(e.target.value) : null })} />
                  </span></label>
              </div>
            </div>
          </div>
        ) : <div className="rr-editor muted small" style={{ padding: 14 }}>Choisissez une règle à gauche ou ajoutez-en une (Ctrl+N).</div>}
        <HeaderDiff rules={rules} />
      </div>
      <datalist id="scolarix-fonts">{FONTS.map((f) => <option key={f} value={f} />)}</datalist>
    </div>
  );

}

function Tri({ label, value, onChange }: { label: string; value: boolean | null | undefined; onChange: (x: boolean | null) => void }) {
  return (
    <div className="lbl">{label}
      <div className="seg sm" role="radiogroup" aria-label={label}>
        <button className={value == null ? "on" : ""} title="Comme l'original" onClick={() => onChange(null)}>=</button>
        <button className={value === true ? "on" : ""} onClick={() => onChange(true)}>Oui</button>
        <button className={value === false ? "on" : ""} onClick={() => onChange(false)}>Non</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ aperçu AVANT / APRÈS
function HeaderDiff({ rules }: { rules: TextRule[] }) {
  const { prompt } = useDialogs();
  const [cands, setCands] = useState<Cand[] | null>(null);
  const [source, setSource] = useState<number | "texte" | null>(lastSource);
  const [sample, setSample] = useState(lastSample);
  const [fast, setFast] = useState<Preview | null>(null);
  const [img, setImg] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rendering, setRendering] = useState(false);
  const seq = useRef(0);
  lastSource = source;
  lastSample = sample;

  useEffect(() => {
    void call<Cand[]>("rules_preview_releves", {}).then((c) => {
      setCands(c);
      setSource((s) => (s == null ? (c.length ? c[0].id : "texte") : s));
    }).catch(() => { setCands([]); setSource((s) => s ?? "texte"); });
  }, []);
  const releveId = typeof source === "number" ? source : null;
  const key = JSON.stringify([rules, source, source === "texte" ? sample : ""]);
  const quick = useDebounced(key, 200);
  const slow = useDebounced(key, 650);

  // rendu typographique (immédiat) : texte libre ou paragraphes de l'en-tête du relevé
  useEffect(() => {
    if (source == null) return;
    let alive = true;
    call<Preview>("rules_preview", { rules, releve_id: releveId, text: releveId ? null : sample })
      .then((r) => { if (alive) { setFast(r); setError(null); } })
      .catch((e) => alive && setError((e as Error).message || String(e)));
    return () => { alive = false; };
  }, [quick]); // eslint-disable-line react-hooks/exhaustive-deps

  // rendu du moteur (relevé seulement) : on garde l'image précédente, estompée, pendant le calcul
  useEffect(() => {
    if (!releveId) { setImg(null); return; }
    const my = ++seq.current;
    setRendering(true);
    call<Preview>("rules_preview", { rules, releve_id: releveId, rendu: true, req: my })
      .then((r) => { if (my === seq.current && !r.stale) setImg(r); })
      .catch(() => undefined)
      .finally(() => { if (my === seq.current) setRendering(false); });
  }, [slow]); // eslint-disable-line react-hooks/exhaustive-deps

  const other = async () => {
    const q = await prompt("Essai sur un autre relevé", "Matricule ou nom de l'étudiant (relevés Word seulement) :");
    if (!q) return;
    const found = await call<Cand[]>("rules_preview_releves", { q, limit: 30 }).catch(() => []);
    if (!found.length) { setError(`Aucun relevé Word pour « ${q} ».`); return; }
    setCands([...found, ...(cands || []).filter((c) => !found.some((f) => f.id === c.id))]);
    setSource(found[0].id);
  };

  const rendered = releveId && img?.rendu && img.releve_id === releveId ? img : null;
  const p = fast && ((releveId && fast.mode === "releve" && fast.releve_id === releveId) || (!releveId && fast.mode === "texte")) ? fast : null;
  const warnings = [
    ...(rendered?.motif ? [`L'aperçu réel écarterait ces corrections (mise en page à risque) : ${rendered.motif}`] : []),
    ...((rendered || p)?.ecartees || []),
    ...(p?.avertissements || []),
  ];
  const count = (rendered || p)?.count;
  const ys = rendered?.rendu ? [...rendered.rendu.avant.rects, ...rendered.rendu.apres.rects].map((r) => r.y) : [];
  const focusY = ys.length ? Math.min(...ys) : null;
  return (
    <div className="hd" data-testid="rules-preview">
      <div className={`hd-panels${error ? " stale" : ""}`}>
        <Panel kind="avant" title="Avant" sub={releveId ? "En-tête d'origine" : "Texte d'essai"} legend="rouge barré = texte retiré" img={rendered?.rendu?.avant} pars={p?.avant} stale={rendering} wait={p?.attente} focusY={focusY} />
        <Panel kind="apres" title="Après" sub={count == null ? "…" : `${p?.regles ?? 0} règle${(p?.regles ?? 0) > 1 ? "s" : ""} · ${count} remplacement${count > 1 ? "s" : ""}`}
          legend="vert = texte ajouté, mis en forme" img={rendered?.rendu?.apres} pars={p?.apres} stale={rendering} wait={p?.attente} testid="rules-after" focusY={focusY} />
      </div>
      <div className="hd-bar">
        <span className="small muted nowrap">Essai sur l'en-tête de</span>
        <select className="field hd-select" value={source == null ? "" : String(source)} onChange={(e) => { const x = e.target.value; if (x === "__autre") void other(); else setSource(x === "texte" ? "texte" : Number(x)); }} data-testid="rules-source">
          {(cands || []).map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          {!cands?.length && <option value="" disabled>{cands ? "aucun relevé Word indexé" : "…"}</option>}
          <option value="__autre">Autre matricule…</option>
          <option value="texte">Texte libre (essai)</option>
        </select>
        <button className={`btn sm ${source === "texte" ? "on" : "ghost"}`} onClick={() => setSource("texte")} data-testid="rules-texte">Texte libre…</button>
        {source === "texte" && <input className="field mono grow" value={sample} onChange={(e) => setSample(e.target.value)} aria-label="Texte d'essai" data-testid="rules-sample" />}
        {releveId != null && <span className="tiny muted grow ellipsis">{rendering ? <><span className="spinner" style={{ width: 11, height: 11, verticalAlign: -1 }} /> rendu du moteur…</>
          : rendered ? `rendu ${rendered.convertisseur || ""}` : img?.sans_convertisseur || p?.sans_convertisseur ? "rendu typographique (aucun convertisseur sur ce poste)" : ""}</span>}
      </div>
      {source === "texte" && (
        <div className="selectable mono small hd-result" data-testid="rules-result" role="status" style={error ? { background: "var(--red-bg)", color: "#991B1B" } : undefined}>
          {error ? `⚠ ${error}` : !p ? "…" : `Résultat (${p.count} remplacement(s)) : ${p.text}`}
        </div>
      )}
      {source !== "texte" && error && <div className="small hd-error" data-testid="rules-result" role="status">⚠ {error}</div>}
      {!error && warnings.length > 0 && <div className="hd-warn">{warnings.map((w, k) => <div key={k} className="small" data-testid="rules-warning">⚠ {w}</div>)}</div>}
    </div>
  );
}

function Panel({ kind, title, sub, legend, img, pars, stale, wait, testid, focusY }: {
  kind: "avant" | "apres"; title: string; sub: string; legend: string; img?: Img; pars?: Par[]; stale: boolean; wait?: string; testid?: string;
  focusY?: number | null;       // même hauteur (fraction de l'image) amenée en vue dans les deux panneaux
}) {
  const stage = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<[number, number]>([600, 180]);
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setBox([el.clientWidth, el.clientHeight]));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  // le premier texte retiré / ajouté est amené dans la zone (en-tête plus haut que le panneau)
  const firstY = img ? focusY ?? null : null;
  useEffect(() => {
    const el = stage.current;
    if (!el || firstY == null || !img) return;
    const pageH = el.scrollHeight - 16;
    const y = firstY * pageH;
    if (y < el.scrollTop + 10 || y > el.scrollTop + el.clientHeight - 30) el.scrollTop = Math.max(0, y - el.clientHeight / 3);
  }, [img?.img, firstY, box[0]]); // eslint-disable-line react-hooks/exhaustive-deps
  let content: ReactNode;
  let zoom = 0;
  if (img) {
    // page affichée en entier en largeur si possible, sinon ajustée à la hauteur de la zone
    const pw = img.w_pt * 96 / 72, ph = img.h_pt * 96 / 72;
    zoom = Math.min((box[0] - 24) / pw, 1.5);            // pleine largeur ; la zone défile si l'en-tête est haut
    const w = Math.max(80, pw * zoom), h = ph * zoom;
    content = (
      <div className={`hd-page${stale ? " stale" : ""}`} style={{ width: w, height: h }} data-testid={testid ? `${testid}-img` : undefined}>
        <img src={img.img} alt={`${title} : haut de la page`} width={w} height={h} />
        {img.rects.map((r, k) => <span key={k} className={`hd-rect ${r.k}`} style={{ left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`, height: `${r.h * 100}%` }} />)}
      </div>
    );
  } else if (pars && pars.length) {
    zoom = Math.min(1, (box[0] - 24) / 640);
    content = (
      <div className={`hd-typo${stale ? " stale" : ""}`} style={{ zoom } as CSSProperties} data-testid={testid}>
        {pars.map((p, i) => (
          <p key={i} style={{ textAlign: p.align as CSSProperties["textAlign"] }}>
            {p.seg.map((s, k) => <span key={k} className={s.k !== "eq" ? `hd-${s.k}` : undefined}
              style={{ fontSize: s.size ? `${s.size * 1.333}px` : undefined, fontWeight: s.bold ? 700 : s.bold === false ? 400 : undefined, fontStyle: s.italic ? "italic" : undefined, fontFamily: s.font || undefined }}>{s.t}</span>)}
            {!p.seg.length && " "}
          </p>
        ))}
      </div>
    );
  } else content = <div className="muted small hd-wait">{wait || (stale ? "Rendu…" : "…")}</div>;
  return (
    <div className={`hd-panel ${kind}`}>
      <div className="hd-label">
        <Badge text={title.toUpperCase()} tone={kind === "avant" ? "gray" : "green"} />
        <b className="small">{sub}</b>
        <span className="tiny muted">{legend}</span>
      </div>
      <div className="hd-stage">
        <div className="hd-scroll" ref={stage}>{content}</div>
        {zoom > 0 && <span className="hd-zoom mono">{Math.round(zoom * 100)} %</span>}
      </div>
    </div>
  );
}
