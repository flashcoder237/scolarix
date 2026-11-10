import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { call, sx, FILIERES, FIL_SHORT, statutTone } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Icon, Popover, useToast } from "../components/ui";
import { VirtualTable, type VCol } from "../components/VirtualTable";
import { CursusDialog } from "./CursusDialog";
import { JournalView } from "./JournalView";

// Écran « Extraction » du Décanat (2.1) : une seule barre (vues sœurs Extraction | Journal des actions, filtres
// actifs en puces, recherche dans le résultat), filtres en colonne avec leurs effectifs, tableau pleine hauteur
// de TOUTES les lignes (défilement virtuel, tri par colonne), dock d'export et de la ligne choisie.

interface ExtView {
  count: number; cols: string[]; rows: unknown[][]; mentions: Record<string, number>; mention_order: string[];
  per_year: [string, number][]; filieres: Record<string, number>; filieres_total: number; niveaux: Record<string, number>;
  statuts: { tous: number; admis: number; non_valides: number }; destination: string; ms: number;
}
interface Recent { horodatage: string; scope: string; statut: string; lignes: number; path: string; format: string; existe: boolean }
type View = "extraction" | "journal";

// Mentions calculées par le moteur (core/exporter.mention) : mêmes libellés et couleurs que la 1.x
export const MENTIONS: [string, string][] = [["Excellent", "#020617"], ["Très bien", "#1E3A8A"], ["Bien", "#1D4ED8"], ["Assez bien", "#3B82F6"],
  ["Passable", "#93C5FD"], ["Médiocre", "#F6D9A8"], ["Éliminé", "#F4B4B4"], ["Sans mention", "#CBD5E1"]];
const MCOLOR = Object.fromEntries(MENTIONS);
const STATUTS: [string, string][] = [["tous", "Tous"], ["admis", "Admis"], ["non_valides", "Ajournés"]];
const OPTIONAL_COLS: [string, string][] = [["niveau", "Niveau"], ["session", "Session"], ["decision", "Décision du jury"], ["credits", "Crédits"],
  ["grade", "Grade"], ["mgp", "MGP"], ["naissance", "Date de naissance"]];
const BASE_COLS: [string, string][] = [["matricule", "Matricule"], ["nom", "Nom & prénom"], ["fil", "Filière"], ["annee", "Année"], ["periode", "Période"],
  ["moyenne", "Moyenne"], ["mention", "Mention"], ["statut", "Statut"]];
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const nf = (n: number) => n.toLocaleString("fr-FR");
const fold = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const yearLabel = (y: number) => `${y}-${y + 1}`;
/** « Niveau 3 · S5-S6 (annuel) » → « N3 · S5-S6 » (le détail complet reste en infobulle). */
const shortPeriode = (p: string) => p.replace(/^Niveau\s*/i, "N").replace(/\s*\(annuel\)/, "");
export const niveauLabel = (n: number) => (n > 100 ? `M${n - 100}` : `N${n}`);

// vue affichée gardée d'une visite à l'autre de l'écran (l'écran est démonté quand on le quitte)
let lastView: View = "extraction";
let lastCols: string[] = BASE_COLS.map(([k]) => k);

/** « Au guichet » : l'écran du guichet reste monté ; on lui passe la recherche par son champ (comme une saisie). */
export function openInGuichet(go: (p: "guichet") => void, q: string) {
  go("guichet");
  window.setTimeout(() => {
    const input = document.querySelector<HTMLInputElement>('[data-page="guichet"] [data-testid="search"]');
    if (!input) return;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, q);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  }, 30);
}

export function Extraction() {
  const [view, setViewState] = useState<View>(lastView);
  const setView = (v: View) => { lastView = v; setViewState(v); };
  const [q, setQ] = useState("");
  const [chips, setChips] = useState<{ key: string; label: string; clear: () => void }[]>([]);
  const searchRef = useRef<HTMLInputElement>(null);
  useShortcuts({
    "ctrl+f": () => { searchRef.current?.focus(); searchRef.current?.select(); },
    // Échap : vide d'abord la recherche locale ; sinon, comme partout, retour au guichet
    "escape": () => { if (q && document.activeElement === searchRef.current) { setQ(""); return; } return false; },
    "ctrl+tab": () => setView(view === "extraction" ? "journal" : "extraction"),
  });
  return (
    <div className="xr-page">
      <header className="card topbar xr-top">
        <div className="xr-title"><div className="eyebrow">Décanat</div><h1 className="h3" aria-label="Extraction et journal">Extraction</h1></div>
        <div className="fseg" role="tablist" aria-label="Vues">
          <button role="tab" aria-selected={view === "extraction"} className={view === "extraction" ? "on" : ""} onClick={() => setView("extraction")} data-testid="view-extraction"><Icon name="chart" size={15} /> Extraction</button>
          <button role="tab" aria-selected={view === "journal"} className={view === "journal" ? "on" : ""} onClick={() => setView("journal")} data-testid="view-journal"><Icon name="clock" size={15} /> Journal des actions</button>
        </div>
        <div className="xr-chips" data-testid="filter-chips">
          {chips.map((c) => (
            <span key={c.key} className="xr-chip">{c.label}<button aria-label={`Retirer le filtre ${c.label}`} title="Retirer ce filtre" onClick={c.clear}><Icon name="x" size={12} /></button></span>
          ))}
        </div>
        <label className="xr-search">
          <Icon name="search" size={16} />
          <input ref={searchRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder={view === "extraction" ? "Nom ou matricule dans le résultat…" : "Matricule, opérateur, détail…"}
            aria-label="Rechercher dans le résultat" data-testid="xr-search" />
          {q ? <button className="btn ghost xs" aria-label="Effacer" onClick={() => setQ("")}><Icon name="x" size={12} /></button> : <span className="key">Ctrl+F</span>}
        </label>
      </header>
      {view === "extraction" ? <ExtractionView q={q} setChips={setChips} /> : <JournalView q={q} setQ={setQ} setChips={setChips} />}
    </div>
  );
}

function ExtractionView({ q, setChips }: { q: string; setChips: (c: { key: string; label: string; clear: () => void }[]) => void }) {
  const app = useApp();
  const toast = useToast();
  const [years, setYears] = useState<number[]>([]);
  const [fil, setFil] = useState<string | null>(null);
  const [y1, setY1] = useState<number | null>(null);
  const [y2, setY2] = useState<number | null>(null);
  const [statut, setStatut] = useState("tous");
  const [last, setLast] = useState(true);            // 1.x : « Dernier niveau atteint (diplômés) » par défaut
  const [niveaux, setNiveaux] = useState<number[]>([]);
  const [mentions, setMentions] = useState<string[] | null>(null);   // null = toutes
  const [cols, setColsState] = useState<string[]>(lastCols);
  const setCols = (c: string[]) => { lastCols = c; setColsState(c); };
  const [res, setRes] = useState<ExtView | null>(null);
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [recent, setRecent] = useState<Recent[]>([]);
  const [colsOpen, setColsOpen] = useState(false);
  const [cursusFor, setCursusFor] = useState<string | null>(null);
  const seq = useRef(0);

  const extra = cols.filter((c) => OPTIONAL_COLS.some(([k]) => k === c) && c !== "niveau");
  const params = { filiere: fil, year_from: y1, year_to: y2, statut, dernier_niveau: last, niveaux: niveaux.length ? niveaux : null, mentions };
  const run = useCallback(async () => {
    const my = ++seq.current;
    setBusy(true);
    try {
      const r = await call<ExtView>("extraction_view", { ...params, extra_cols: extra });
      if (my === seq.current) { setRes(r); setSel(null); }
    } catch (e) { toast(errMsg(e), { tone: "red" }); } finally { if (my === seq.current) setBusy(false); }
  }, [fil, y1, y2, statut, last, niveaux.join(","), JSON.stringify(mentions), extra.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void run(); }, [run]);
  const loadRecent = useCallback(() => { void call<Recent[]>("extraction_recent", { limit: 4 }).then(setRecent).catch(() => undefined); }, []);
  useEffect(() => { void call<number[]>("years").then(setYears).catch(() => undefined); loadRecent(); }, [loadRecent]);

  const filLabel = FILIERES.find((f) => f.code === fil)?.label || "Toutes filières";
  const scope = `${fil ? filLabel : "Toutes"} · ${y1 ? yearLabel(y1) : "début"} → ${y2 ? yearLabel(y2) : "aujourd'hui"}`;
  const reset = () => { setFil(null); setY1(null); setY2(null); setStatut("tous"); setLast(true); setNiveaux([]); setMentions(null); };

  // puces des filtres actifs, dans la barre du haut
  useEffect(() => {
    const c: { key: string; label: string; clear: () => void }[] = [];
    if (fil) c.push({ key: "fil", label: filLabel, clear: () => setFil(null) });
    if (y1 || y2) c.push({ key: "y", label: `${y1 ? yearLabel(y1) : "début"} → ${y2 ? yearLabel(y2) : "aujourd'hui"}`, clear: () => { setY1(null); setY2(null); } });
    c.push(last ? { key: "last", label: "Dernier niveau", clear: () => setLast(false) } : { key: "last", label: "Tous les relevés", clear: () => setLast(true) });
    if (niveaux.length) c.push({ key: "niv", label: niveaux.map(niveauLabel).join(", "), clear: () => setNiveaux([]) });
    if (statut !== "tous") c.push({ key: "st", label: STATUTS.find(([v]) => v === statut)?.[1] || statut, clear: () => setStatut("tous") });
    if (mentions) c.push({ key: "men", label: mentions.length === 1 ? mentions[0] : `${mentions.length} mentions`, clear: () => setMentions(null) });
    setChips(c);
  }, [fil, y1, y2, last, niveaux.join(","), statut, JSON.stringify(mentions)]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => setChips([]), []); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- lignes : recherche locale et tri, sur les colonnes compactes du moteur
  const ci = useMemo(() => Object.fromEntries((res?.cols || []).map((c, i) => [c, i])), [res]);
  const moy = useMemo(() => (res?.rows || []).map((r) => { const v = parseFloat(String(r[ci.moyenne] ?? "").replace(",", ".")); return Number.isFinite(v) ? v : -1; }), [res, ci]);
  const hay = useMemo(() => (res?.rows || []).map((r) => fold(`${r[ci.matricule]} ${r[ci.nom]}`)), [res, ci]);
  const order = useMemo(() => {
    const rows = res?.rows || [];
    const words = fold(q.trim()).split(/\s+/).filter(Boolean);
    let idx = rows.map((_, i) => i);
    if (words.length) idx = idx.filter((i) => words.every((w) => hay[i].includes(w)));
    if (sort) {
      const k = sort.key;
      const val = (i: number): string | number => k === "moyenne" ? moy[i] : k === "mention" ? Number(rows[i][ci.mention] ?? 99) : k === "niveau" ? Number(rows[i][ci.niveau])
        : k === "credits" ? Number(rows[i][ci.credits] || -1) : String(rows[i][ci[k]] ?? "");
      const coll = new Intl.Collator("fr", { sensitivity: "base", numeric: true });
      idx.sort((a, b) => { const x = val(a), y = val(b); return (typeof x === "number" && typeof y === "number" ? x - y : coll.compare(String(x), String(y))) * sort.dir; });
    }
    return idx;
  }, [res, q, sort, moy, hay, ci]);
  useEffect(() => setSel(null), [q, sort]);
  const row = (i: number) => res?.rows[order[i]];
  const selRow = sel != null ? row(sel) : undefined;
  const selMat = selRow ? String(selRow[ci.matricule]) : "";

  const exportAs = async (kind: "csv" | "xlsx") => {
    if (!res?.count) { toast("Aucune ligne à exporter avec ces filtres.", { tone: "amber" }); return; }
    const name = `Decanat_${fil || "Toutes"}_${statut}_${y1 || "debut"}-${y2 ? y2 + 1 : "fin"}.${kind}`;
    const def = await call<string>("suggest_path", { nature: "extraction", filename: name }).catch(() => name);   // dossier Extractions du mois
    const path = await sx.saveFile({ title: "Exporter", filters: [kind === "xlsx" ? { name: "Classeur Excel", extensions: ["xlsx"] } : { name: "CSV", extensions: ["csv"] }], defaultPath: def });
    if (!path) return;
    try {
      toast(`Export de ${nf(res.count)} lignes…`, { tone: "blue", key: "xr-export", sound: false, ms: 60000 });
      const r = await call<{ path: string; name: string; count: number }>("extraction_view_export", { ...params, kind, path, scope });
      toast(`Exporté : ${r.name} (${nf(r.count)} lignes) · extraction journalisée`, { key: "xr-export", ms: 15000, action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
      loadRecent();
    } catch (e) { toast(`Export impossible : ${errMsg(e)}`, { tone: "red", key: "xr-export" }); }
  };
  const toGuichet = () => { if (selMat) openInGuichet(app.go, selMat); };
  useShortcuts({ "ctrl+e": () => void exportAs("xlsx") });

  const perYear = res?.per_year || [];
  const maxY = Math.max(1, ...perYear.map(([, n]) => n));
  const yearOf = (a: string) => Number(a.slice(0, 4));
  const inRange = (a: string) => (!y1 || yearOf(a) >= y1) && (!y2 || yearOf(a) <= y2);
  const pickYear = (a: string, ext: boolean) => {
    const y = yearOf(a);
    if (!Number.isFinite(y)) return;
    if (ext && (y1 || y2)) { const lo = Math.min(y1 ?? y, y), hi = Math.max(y2 ?? y, y); setY1(lo); setY2(hi); } else { setY1(y); setY2(y); }
  };
  const allMentions = MENTIONS.map(([m]) => m).filter((m) => m !== "Sans mention" || (res?.mentions["Sans mention"] || 0) > 0);
  const toggleMention = (m: string) => {
    const cur = mentions ?? allMentions;
    const next = cur.includes(m) ? cur.filter((x) => x !== m) : [...cur, m];
    setMentions(next.length === allMentions.length && allMentions.every((x) => next.includes(x)) ? null : next);
  };
  const nivKeys = Array.from(new Set([1, 2, 3, 4, 5, 6, 7, ...Object.keys(res?.niveaux || {}).map(Number).filter((n) => n > 0)])).sort((a, b) => a - b);
  const totalM = Object.values(res?.mentions || {}).reduce((a, b) => a + b, 0) || 1;

  const columns: VCol<unknown[]>[] = [
    { key: "matricule", label: "Matricule", width: 88, sortable: true, className: "mono small", render: (r) => String(r[ci.matricule]) },
    { key: "nom", label: "Nom & prénom", sortable: true, className: "ellipsis", render: (r) => String(r[ci.nom]), title: (r) => String(r[ci.nom]) },
    { key: "fil", label: "Filière", width: 92, sortable: true, className: "ellipsis", render: (r) => FIL_SHORT[String(r[ci.fil])] || String(r[ci.fil] || "—") },
    { key: "annee", label: "Année", width: 100, sortable: true, render: (r) => String(r[ci.annee]) },
    { key: "periode", label: "Période", width: 112, sortable: true, className: "ellipsis small", render: (r) => shortPeriode(String(r[ci.periode])), title: (r) => String(r[ci.periode]) },
    { key: "niveau", label: "Niveau", width: 70, sortable: true, render: (r) => niveauLabel(Number(r[ci.niveau])) },
    { key: "moyenne", label: "Moyenne", width: 80, sortable: true, align: "right", className: "xr-moy", render: (r) => String(r[ci.moyenne] || "—") },
    { key: "mention", label: "Mention", width: 96, sortable: true, className: "ellipsis", render: (r) => { const m = Number(r[ci.mention]); return m >= 0 ? res?.mention_order[m] : "—"; } },
    { key: "statut", label: "Statut", width: 108, sortable: true, render: (r) => <Badge text={String(r[ci.statut] || "—")} tone={statutTone(String(r[ci.statut] || ""))} /> },
    { key: "session", label: "Session", width: 100, sortable: true, className: "ellipsis small", render: (r) => String(r[ci.session] ?? "") },
    { key: "decision", label: "Décision", width: 160, sortable: true, className: "ellipsis small", render: (r) => String(r[ci.decision] ?? ""), title: (r) => String(r[ci.decision] ?? "") },
    { key: "credits", label: "Crédits", width: 72, sortable: true, align: "right", render: (r) => String(r[ci.credits] ?? "") },
    { key: "grade", label: "Grade", width: 64, sortable: true, render: (r) => String(r[ci.grade] ?? "") },
    { key: "mgp", label: "MGP", width: 60, sortable: true, align: "right", render: (r) => String(r[ci.mgp] ?? "") },
    { key: "naissance", label: "Naissance", width: 100, sortable: true, className: "small", render: (r) => String(r[ci.naissance] ?? "") },
  ];
  const shown = columns.filter((c) => cols.includes(c.key) && (c.key in ci || c.key === "niveau"));

  return (
    <div className="xr-body">
      <aside className="card xr-filters" aria-label="Filtres" data-testid="xr-filters">
        <div className="xr-fh">Filière</div>
        <div className="xr-list" role="radiogroup" aria-label="Filière">
          {FILIERES.map((f) => {
            const n = f.code ? res?.filieres[f.code] || 0 : res?.filieres_total || 0;
            return (
              <button key={String(f.code)} role="radio" aria-checked={fil === f.code} className={`xr-opt${fil === f.code ? " on" : ""}`} onClick={() => setFil(f.code)} data-testid={`fil-${f.code || "toutes"}`}>
                <span className="rad" /><span className="grow">{f.label}</span><span className="n">{nf(n)}</span>
              </button>
            );
          })}
        </div>
        <div className="xr-fh row"><span className="grow">Années académiques</span><span className="n">{perYear.length} an{perYear.length > 1 ? "s" : ""}</span></div>
        <div className="xr-hist" aria-label="Relevés par année : cliquer pour choisir une année, Maj+clic pour étendre">
          {perYear.map(([a, n]) => (
            <button key={a} className={inRange(a) ? "in" : ""} style={{ height: `${Math.max(8, (n * 100) / maxY)}%` }} title={`${a} : ${nf(n)}`} onClick={(e) => pickYear(a, e.shiftKey)} aria-label={`${a} : ${n}`} />
          ))}
        </div>
        <div className="xr-years">
          <select className="field" value={y1 ?? ""} onChange={(e) => setY1(e.target.value ? Number(e.target.value) : null)} aria-label="De" data-testid="year-from"><option value="">début</option>{years.map((y) => <option key={y} value={y}>{yearLabel(y)}</option>)}</select>
          <select className="field" value={y2 ?? ""} onChange={(e) => setY2(e.target.value ? Number(e.target.value) : null)} aria-label="À" data-testid="year-to"><option value="">aujourd'hui</option>{years.map((y) => <option key={y} value={y}>{yearLabel(y)}</option>)}</select>
        </div>
        <div className="xr-fh">Niveau</div>
        <div className="xr-list" role="radiogroup" aria-label="Niveau">
          <button role="radio" aria-checked={last} className={`xr-opt${last ? " on" : ""}`} onClick={() => setLast(true)} data-testid="niveau-dernier"><span className="rad" /><span className="grow">Dernier niveau atteint (diplômés)</span></button>
          <button role="radio" aria-checked={!last} className={`xr-opt${!last ? " on" : ""}`} onClick={() => setLast(false)} data-testid="niveau-choisis"><span className="rad" /><span className="grow">{niveaux.length ? "Niveaux choisis" : "Tous les niveaux"}</span></button>
        </div>
        <div className="xr-nivs">
          {nivKeys.map((n) => {
            const on = niveaux.includes(n);
            const c = res?.niveaux[n] || 0;
            return <button key={n} className={`xr-niv${on ? " on" : ""}`} aria-pressed={on} title={`${niveauLabel(n)} : ${nf(c)} relevé(s)`} disabled={!c && !on}
              onClick={() => setNiveaux(on ? niveaux.filter((x) => x !== n) : [...niveaux, n].sort((a, b) => a - b))} data-testid={`niv-${n}`}>{niveauLabel(n)}</button>;
          })}
        </div>
        <div className="xr-fh">Statut</div>
        <div className="seg xr-seg" role="radiogroup" aria-label="Statut">
          {STATUTS.map(([v, l]) => <button key={v} role="radio" aria-checked={statut === v} className={statut === v ? "on" : ""} onClick={() => setStatut(v)}
            title={`${nf(res?.statuts[v as keyof ExtView["statuts"]] || 0)} relevé(s)`} data-testid={`statut-${v}`}>{l}</button>)}
        </div>
        <div className="xr-fh row"><span className="grow">Mentions</span><span className="n">{(mentions ?? allMentions).length} / {allMentions.length}</span></div>
        <div className="xr-list">
          {allMentions.map((m) => {
            const on = (mentions ?? allMentions).includes(m);
            return (
              <label key={m} className="xr-check" data-testid={`mention-${m}`}>
                <input type="checkbox" checked={on} onChange={() => toggleMention(m)} />
                <span className="sw" style={{ background: MCOLOR[m] }} /><span className="grow">{m}</span><span className="n">{nf(res?.mentions[m] || 0)}</span>
              </label>
            );
          })}
        </div>
        <button className="btn ghost sm xr-reset" onClick={reset} data-testid="xr-reset">Réinitialiser les filtres</button>
      </aside>

      <section className="card xr-main">
        <div className="xr-sum">
          <span className="xr-count" data-testid="xr-count">{nf(order.length)}</span>
          <span className="muted xr-scope ellipsis">{q.trim() ? `sur ${nf(res?.count || 0)} · ` : ""}{last ? "diplômés" : "relevés"} · {scope}</span>
          {busy && <span className="spinner" />}
          <div className="xr-dist" title="Répartition des mentions" data-testid="xr-dist">
            {MENTIONS.map(([m, c]) => (res?.mentions[m] ? <div key={m} style={{ width: `${(res.mentions[m] * 100) / totalM}%`, background: c }} title={`${m} : ${nf(res.mentions[m])}`} /> : null))}
          </div>
        </div>
        <div className="xr-legend">
          {MENTIONS.filter(([m]) => m !== "Sans mention" || res?.mentions[m]).map(([m, c]) => <span key={m}><i style={{ background: c }} />{m} <b>{nf(res?.mentions[m] || 0)}</b></span>)}
        </div>
        <VirtualTable count={order.length} getRow={row} cols={shown} selected={sel} onSelect={setSel} onActivate={(i) => { setSel(i); const r = row(i); if (r) openInGuichet(app.go, String(r[ci.matricule])); }}
          sort={sort} onSort={(k) => setSort(sort && sort.key === k ? (sort.dir === 1 ? { key: k, dir: -1 } : null) : { key: k, dir: k === "moyenne" ? -1 : 1 })}
          rowKey={(r, i) => (r ? Number(r[ci.id]) : `p${i}`)} testid="xr-table" label="Résultat de l'extraction"
          empty={<span className="muted">{busy ? "Calcul…" : "Aucun relevé avec ces filtres."}</span>} />
        <div className="xr-foot muted small">
          <span className="grow">{nf(order.length)} ligne{order.length > 1 ? "s" : ""}, toutes consultables (défilement virtuel){res ? ` · calcul ${res.ms} ms` : ""}</span>
          <span className="opt-mid"><span className="key">↑</span><span className="key">↓</span> ligne · <span className="key">Entrée</span> ouvrir au guichet · clic sur un en-tête : trier</span>
        </div>
      </section>

      <aside className="dock xr-dock" aria-label="Export et ligne choisie">
        <div className="grp">
          <div className="lbl">Exporter · {nf(res?.count || 0)}</div>
          <button className="btn primary" onClick={() => void exportAs("xlsx")} data-testid="xr-excel"><span className="r"><Icon name="download" size={16} />Excel</span><span className="kbd">Ctrl+E</span></button>
          <button className="btn secondary sm" onClick={() => void exportAs("csv")} data-testid="xr-csv"><Icon name="download" size={14} />CSV</button>
          <div className="anchor">
            <button className="btn ghost sm" onClick={() => setColsOpen(!colsOpen)} aria-expanded={colsOpen} data-testid="xr-cols"><Icon name="filter" size={14} />Colonnes ({shown.length})</button>
            <Popover open={colsOpen} onClose={() => setColsOpen(false)} label="Colonnes du tableau" align="right" width={250} testid="xr-cols-pop">
              <div className="pop-head"><b>Colonnes du tableau</b><span className="muted tiny">L'export Excel / CSV contient toujours toutes les colonnes.</span></div>
              {[...BASE_COLS, ...OPTIONAL_COLS].map(([k, l]) => (
                <label key={k} className="mi xr-colopt"><input type="checkbox" checked={cols.includes(k)} disabled={k === "matricule" || k === "nom"}
                  onChange={() => setCols(cols.includes(k) ? cols.filter((x) => x !== k) : [...cols, k])} />{l}</label>
              ))}
            </Popover>
          </div>
          <div className="tiny muted xr-dest" title="Dossier proposé pour l'export (modifiable dans la boîte Enregistrer sous)">Classé dans Exports\{(res?.destination || "Extractions").replace(/\//g, "\\")} · journalisé</div>
        </div>
        <div className="grp">
          <div className="lbl">Ligne choisie</div>
          {selMat && <div className="tiny mono xr-selmat ellipsis" title={String(selRow?.[ci.nom] || "")}>{selMat}</div>}
          <button className="btn ghost sm" disabled={!selMat} onClick={toGuichet} data-testid="xr-guichet"><Icon name="search" size={14} />Au guichet<span className="kbd">Entrée</span></button>
          <button className="btn ghost sm" disabled={!selMat} onClick={() => setCursusFor(selMat)} data-testid="xr-cursus"><Icon name="layers" size={14} />Cursus</button>
        </div>
        <div className="grp xr-recent" data-testid="xr-recent">
          <div className="lbl">Dernières extractions</div>
          {!recent.length && <div className="tiny muted">Aucune pour l'instant.</div>}
          {recent.map((r, k) => (
            <button key={k} className="xr-rec" disabled={!r.existe} onClick={() => void sx.reveal(r.path)} title={r.existe ? `${r.path}\n(afficher dans le dossier)` : `${r.path}\n(fichier déplacé ou supprimé)`}>
              <b className="ellipsis">{r.scope.replace(/ · /, ", ")}</b>
              <span className="tiny muted">{r.horodatage.slice(8, 10)}/{r.horodatage.slice(5, 7)} · {nf(r.lignes)} lignes · {r.format}</span>
            </button>
          ))}
        </div>
      </aside>
      {cursusFor && <CursusDialog matricule={cursusFor} onClose={() => { setCursusFor(null); app.refreshStats(); }} />}
    </div>
  );
}

