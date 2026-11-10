import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  call, sx, FILIERES, FIL_SHORT, flagTone, plural, statutTone,
  type Alert as AlertT, type CheckResult, type Demande, type DemandeFull, type Group, type Releve, type ReleveDetail, type SearchResult, type Stats,
} from "../api";
import { useApp, useShortcuts } from "../state";
import { Alert, Badge, Icon, Popover, Switch, highlight, play, useDebounced, useDialogs, useToast } from "../components/ui";
import { FIT_LABELS, PdfView, ZONES, forget, type PdfViewHandle, type PdfViewState } from "../components/PdfView";
import { PreviewVariantToggle, useOriginalPreview, type PreviewVariant } from "../components/PreviewVariantToggle";
import { DemandeDialog, STATUT_AIDE, STATUT_TONE } from "./DemandeDialog";
import { CursusDialog } from "./CursusDialog";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function normText(s: string | null | undefined) {
  return (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toUpperCase().replace(/[^A-Z0-9]+/g, " ").trim();
}
/** Même nom, à l'ordre des mots, aux accents et à la casse près (changement de filière, nom réécrit). */
export function samePerson(a: string | null | undefined, b: string | null | undefined) {
  const ta = new Set(normText(a).split(" ").filter(Boolean)), tb = new Set(normText(b).split(" ").filter(Boolean));
  if (!ta.size || !tb.size) return false;
  const sub = (x: Set<string>, y: Set<string>) => [...x].every((w) => y.has(w));
  if (sub(ta, tb) || sub(tb, ta)) return true;
  // mots collés dans certains fichiers (« PRUDENCE NOM » / « PRUDENCENOM ») : même clé sans espaces
  return normText(a).replace(/ /g, "") === normText(b).replace(/ /g, "");
}

type Pending = { kind: "print" | "export"; id: number } | null;
type View = "groupe" | "releve";
interface StudentGroup { matricule: string; nom: string; filiere: string; rows: Releve[] }

/** Regroupement par étudiant dans l'ordre de pertinence ; dans un groupe, l'ordre du cursus (niveau, semestre, année). */
export function groupByStudent(rows: Releve[]): StudentGroup[] {
  const map = new Map<string, StudentGroup>();
  for (const r of rows) {
    let g = map.get(r.matricule);
    if (!g) { g = { matricule: r.matricule, nom: r.nom_complet, filiere: FIL_SHORT[r.filiere_code || ""] || r.filiere || "?", rows: [] }; map.set(r.matricule, g); }
    g.rows.push(r);
  }
  for (const g of map.values()) {
    g.rows.sort((a, b) => a.niveau_num - b.niveau_num || (a.semestre || 0) - (b.semestre || 0) || (a.annee_academique || "").localeCompare(b.annee_academique || ""));
  }
  return [...map.values()];
}

/** Crédits tels que lus, rapportés au bon total : 60 pour un relevé annuel (deux semestres), 30 pour un semestre. */
export function creditsLabel(rel: Pick<Releve, "credits" | "annuel" | "semestre" | "semestre_fin">, long = false) {
  if (rel.credits == null) return "";
  const deux = rel.annuel || (!!rel.semestre && !!rel.semestre_fin && rel.semestre_fin > rel.semestre);
  const total = deux ? 60 : rel.semestre ? 30 : null;
  if (total && rel.credits <= total) return long ? `${rel.credits}/${total} crédits` : `${rel.credits}/${total} cr.`;
  return `${rel.credits} crédits`;
}

/** « N4 · S7-S8 » : niveau et semestres, comme la colonne Période de la 2.0. */
export function periodLabel(r: Releve) {
  return `${r.niveau_num > 0 && r.niveau_num < 100 ? `N${r.niveau_num}` : (r.niveau || "?").replace("Master ", "M")}${r.sem_label ? ` · ${r.sem_label}` : ""}`;
}

const pagesShort = (r: Releve) => (r.nb_pages > 1 ? `p. ${r.numero_page}–${r.page_fin}` : `p. ${r.numero_page}`);
const isRattrapage = (r: Releve) => (r.session || "").toLowerCase().startsWith("rattrap");
const nomAffiche = (r: Releve) => `${(r.nom || r.nom_complet).toUpperCase()} ${r.prenom || ""}`.trim();

// Emplacement réservé dans le dock, groupe « Affichage » : bascule « Aperçu : corrigé / original »
// (branche fix/apercu-doc, méthode preview_original du service). Rien n'y est affiché tant qu'elle n'est pas fusionnée.
type PreviewVariantSlot = ReactNode;

export function Guichet() {
  const app = useApp();
  const toast = useToast();
  const { ask, prompt } = useDialogs();
  const active = app.page === "guichet";
  const inputRef = useRef<HTMLInputElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const pdf = useRef<PdfViewHandle>(null);

  // ---------------------------------------------------------------- recherche
  const [query, setQuery] = useState("");
  const [tolerant, setTolerant] = useState(true);      // comme la 1.x : tolérance aux fautes par défaut
  const [filiere, setFiliere] = useState<string | null>(null);
  const [view, setViewRaw] = useState<View>("groupe");
  const [res, setRes] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const debounced = useDebounced(query, 120);
  const searchSeq = useRef(0);
  const lastQ = useRef<string | null>(null);         // dernière recherche affichée (Entrée : plein écran si elle est à jour)

  const runSearch = useCallback(async (q: string) => {
    const my = ++searchSeq.current;
    setSearching(true);
    try {
      const r = await call<SearchResult>("search", { q, tolerant, demande_id: app.activeDemande });
      if (my === searchSeq.current) { setRes(r); setSel(0); lastQ.current = q; }
    } catch (e) {
      if (my === searchSeq.current) toast(`Recherche impossible : ${errMsg(e)}`, { tone: "red" });
    } finally {
      if (my === searchSeq.current) setSearching(false);
    }
  }, [tolerant, app.activeDemande, toast]);

  useEffect(() => { if (app.info) void runSearch(debounced); }, [debounced, tolerant, app.info, runSearch]);

  const results = useMemo(() => (res?.results || []).filter((r) => !filiere || r.filiere_code === filiere), [res, filiere]);
  const groups = useMemo(() => groupByStudent(results), [results]);
  // ↑ ↓ suivent l'ordre affiché : groupé par étudiant, ou à plat (pertinence)
  const ordered = useMemo(() => (view === "groupe" ? groups.flatMap((g) => g.rows) : results), [view, groups, results]);
  const [sel, setSel] = useState(0);
  const current: Releve | null = ordered[Math.min(sel, ordered.length - 1)] || null;
  const setView = (v: View) => {
    const id = current?.id;
    const next = v === "groupe" ? groups.flatMap((g) => g.rows) : results;
    setViewRaw(v);
    setSel(Math.max(0, next.findIndex((r) => r.id === id)));
  };
  const pick = (id: number) => setSel(Math.max(0, ordered.findIndex((r) => r.id === id)));

  // ---------------------------------------------------------------- relevé affiché
  const [detail, setDetail] = useState<ReleveDetail | null>(null);
  const [render, setRender] = useState<"" | "en cours" | "échec">("");
  const [renderErr, setRenderErr] = useState("");
  const [version, setVersion] = useState(0);
  const [duplicata, setDuplicata] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState<"" | "print" | "export">("");
  const [cursusFor, setCursusFor] = useState<string | null>(null);
  const [demandeOpen, setDemandeOpen] = useState<number | null>(null);
  const detailSeq = useRef(0);

  useEffect(() => {
    const id = current?.id;
    const my = ++detailSeq.current;
    setRender("");
    setRenderErr("");
    if (!id) { setDetail(null); return; }
    const t = window.setTimeout(async () => {          // ↓ maintenu : on ne charge que là où l'on s'arrête
      try {
        const d = await call<ReleveDetail>("releve", { releve_id: id, consult: true });   // CONSULTATION au journal
        if (my !== detailSeq.current) return;
        setDetail(d);
        if (d.needs_render) {
          setRender("en cours");
          try {
            const d2 = await call<ReleveDetail>("preview", { releve_id: id });
            if (d2.preview_pdf) forget(d2.preview_pdf);
            if (my !== detailSeq.current) return;
            setDetail(d2);
            setRender("");
            setVersion((v) => v + 1);
            // pages exactes connues : la ligne de la liste est mise à jour
            setRes((r) => r && { ...r, results: r.results.map((x) => (x.id === id ? { ...x, ...d2.releve, flags: x.flags } : x)) });
          } catch (e) {
            if (my !== detailSeq.current) return;
            setRender("échec");
            setRenderErr(errMsg(e));
          }
        }
      } catch (e) {
        if (my === detailSeq.current) toast(errMsg(e), { tone: "red" });
      }
    }, 90);
    return () => window.clearTimeout(t);
  }, [current?.id, toast]);

  // impression programmée pendant la préparation Word : elle part quand l'aperçu est prêt
  useEffect(() => {
    if (!pending || !detail || detail.releve.id !== pending.id) return;
    if (render === "échec") { setPending(null); toast("Aperçu Word impossible : l'opération programmée est annulée.", { tone: "red" }); return; }
    if (!detail.needs_render && render === "") {
      const p = pending;
      setPending(null);
      void (p.kind === "print" ? doPrint() : doExport());
    }
  }, [detail, render, pending]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (pending && current && current.id !== pending.id) {
      setPending(null);
      toast("Opération programmée annulée : un autre relevé est sélectionné.", { tone: "amber" });
    }
  }, [current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Contrôle de dernière ligne avant toute délivrance ; renvoie l'accord de contrôle visuel éventuel. */
  async function guard(kind: "print" | "export"): Promise<{ ok: boolean; visual: boolean }> {
    if (!current || !detail || detail.releve.id !== current.id) return { ok: false, visual: false };
    if (render === "en cours" || detail.needs_render) {
      setPending({ kind, id: current.id });
      toast(`Aperçu Word en préparation. ${kind === "print" ? "L'impression partira" : "L'export se fera"} automatiquement dès qu'il sera prêt et contrôlé.`, { tone: "blue", ms: 30000, key: "op" });
      return { ok: false, visual: false };
    }
    if (render === "échec") { toast("Aperçu Word indisponible pour ce relevé : impossible de le délivrer.", { tone: "red" }); return { ok: false, visual: false }; }
    const c = await call<CheckResult>("check", { releve_id: current.id });
    if (c.ok) return { ok: true, visual: false };
    if (c.kind === "visual") {
      const i = await ask({ title: "Contrôle visuel", text: c.message || "", choices: [`C'est bien ${c.nom || c.matricule} : continuer`],
        details: "Comparez le nom et le matricule sur l'aperçu avant de confirmer. Votre confirmation est inscrite au journal." });
      if (i !== 0) void call("visual_refused", { releve_id: current.id, message: c.message || "" }).catch(() => undefined);
      return { ok: i === 0, visual: i === 0 };
    }
    toast(c.message || "Délivrance bloquée.", { tone: "red", ms: 20000 });
    return { ok: false, visual: false };
  }

  async function doPrint() {
    if (!current || busy) return;
    const rel = current;
    setBusy("print");
    try {
      const g = await guard("print");
      if (!g.ok) return;
      toast(`Envoi à l'imprimante… (${rel.pages_label})`, { tone: "blue", ms: 60000, sound: false, key: "op" });
      const r = await sx.printReleve({ releve_id: rel.id, duplicata, visually_checked: g.visual });
      play("impression");
      toast(`Envoyé à l'imprimante — ${r.pages_label}${duplicata ? " · timbre Duplicata apposé" : ""} · réf. ${r.ref}`, { tone: "green", sound: false, key: "op" });
      app.refreshStats();
    } catch (e) {
      toast(`Impression impossible : ${errMsg(e)}`, { tone: "red", key: "op" });
    } finally {
      setBusy("");
    }
  }

  async function doExport() {
    if (!current || busy) return;
    setBusy("export");
    try {
      const g = await guard("export");
      if (!g.ok) return;
      const r = await call<{ path: string; name: string; ref: string; location: string }>("export",
        { releve_id: current.id, duplicata, visually_checked: g.visual });
      toast(`Exporté (réf. ${r.ref}) : ${r.name} — dossier ${r.location}`, { ms: 15000, key: "op", action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
    } catch (e) {
      toast(`Export impossible : ${errMsg(e)}`, { tone: "red" });
    } finally {
      setBusy("");
    }
  }

  // ---------------------------------------------------------------- demandes
  const [openDemandes, setOpenDemandes] = useState<Demande[]>([]);
  const [demandeIds, setDemandeIds] = useState<Set<number>>(new Set());
  const refreshDemandes = useCallback(async () => {
    try {
      const list = await call<Demande[]>("demandes_list", { statuts: ["EN COURS", "PRÊTE", "IMPRIMÉE"] });
      setOpenDemandes(list);
      if (app.activeDemande && !list.some((d) => d.id === app.activeDemande)) app.setActiveDemande(null);
      if (app.activeDemande) {
        const full = await call<DemandeFull>("demande_get", { did: app.activeDemande });
        setDemandeIds(new Set(full.items.map((i) => i.item.releve_id)));
      } else setDemandeIds(new Set());
    } catch { /* moteur en redémarrage : la pastille se remettra à jour */ }
  }, [app.activeDemande]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (app.info) void refreshDemandes(); }, [app.info, app.activeDemande, app.demandeVersion, refreshDemandes]);
  const activeD = openDemandes.find((d) => d.id === app.activeDemande) || null;

  async function newDemande(rel: Releve | null): Promise<number | null> {
    const who = await prompt("Nouvelle demande", "Demandeur (l'étudiant lui-même, un parent, un mandataire…) :", "L'étudiant lui-même");
    if (who === null) return null;
    const d = await call<DemandeFull>("demande_create", { matricule: rel?.matricule ?? null, nom: rel?.nom_complet ?? null, demandeur: who.trim() });
    app.setActiveDemande(d.demande.id);
    app.bumpDemandes();
    return d.demande.id;
  }

  async function addToDemande() {
    const rel = current;
    if (!rel) { toast("Sélectionnez d'abord un relevé.", { tone: "amber" }); return; }
    try {
      let did = app.activeDemande;
      if (!did) { did = await newDemande(rel); if (!did) return; }
      let d = (await call<DemandeFull>("demande_get", { did })).demande;
      let warn = "";
      if (d.matricule && d.matricule !== rel.matricule) {
        if (samePerson(d.nom, rel.nom_complet)) {
          warn = `Attention : ce relevé (${rel.matricule}) n'a pas le matricule de la demande (${d.matricule}), mais le même nom (changement de filière ?). Ajouté : vérifiez la date de naissance.`;
        } else {
          const who = d.nom || d.matricule;
          const i = await ask({ title: "Autre étudiant", text: `La demande en cours (${d.numero}) est celle de ${who}.\nCe relevé est celui de ${rel.nom_complet} (${rel.matricule}).`,
            choices: [`Nouvelle demande pour ${rel.nom_complet}`, `Ajouter à la demande de ${who}`], details: "Une demande = une personne : ses documents lui sont remis ensemble." });
          if (i < 0) return;
          if (i === 0) { const n = await newDemande(rel); if (!n) return; did = n; d = (await call<DemandeFull>("demande_get", { did })).demande; }
          else warn = `Attention : relevé de ${rel.nom_complet} ajouté à la demande de ${who}, comme vous l'avez choisi.`;
        }
      }
      const r = await call<DemandeFull>("demande_add", { did, releve_id: rel.id });
      app.bumpDemandes();
      if (!r.added) toast(`Ce relevé est déjà dans la demande ${r.demande.numero}.`, { tone: "amber" });
      else if (warn) toast(warn, { tone: "amber", ms: 12000 });
      else { play("ajout"); toast(`Ajouté à la demande ${r.demande.numero} : ${rel.periode} · ${rel.annee_academique} (${plural(r.demande.n, "relevé")}).`, { sound: false }); }
      setDemandeIds(new Set(r.items.map((i) => i.item.releve_id)));
    } catch (e) {
      toast(errMsg(e), { tone: "red" });
    }
  }

  // ---------------------------------------------------------------- mode focus (aperçu plein écran)
  const [focus, setFocus] = useState(false);
  const enterFocus = () => {
    if (!current) return;
    setFocus(true);
    inputRef.current?.blur();
    window.setTimeout(() => stageRef.current?.focus(), 0);
    void sx.setFullScreen?.(true).catch(() => undefined);
  };
  const exitFocus = (thenSearch = true) => {
    setFocus(false);
    void sx.setFullScreen?.(false).catch(() => undefined);
    // tout de suite (un second Échap rapide doit trouver la recherche), puis après le rendu par sûreté
    if (thenSearch) { inputRef.current?.focus(); window.setTimeout(() => { if (document.activeElement === document.body) inputRef.current?.focus(); }, 0); }
  };
  useEffect(() => { if (focus && (!active || !current)) exitFocus(false); }, [active, current]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- clavier
  const move = (step: number) => setSel((s) => Math.max(0, Math.min(ordered.length - 1, s + step)));
  const selectSearch = () => { if (focus) exitFocus(false); inputRef.current?.select(); };
  /** ↑ ↓ partout sur le guichet, sauf dans un autre champ de saisie (la recherche, elle, les laisse passer ici). */
  const nav = (step: number) => (e: KeyboardEvent) => {
    const t = e.target as HTMLElement | null;
    if (t && t !== inputRef.current && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return false;
    move(step);
  };
  useShortcuts({
    "ctrl+p": () => void doPrint(), "ctrl+e": () => void doExport(), "ctrl+shift+e": () => { if (current) setCursusFor(current.matricule); },
    "ctrl+d": () => void addToDemande(), "ctrl+n": () => void newDemande(current), "ctrl+k": selectSearch,
    "ctrl+f": selectSearch, "f2": selectSearch,
    "arrowdown": nav(1), "arrowup": nav(-1), "pagedown": nav(8), "pageup": nav(-8),
    "alt+1": () => pdf.current?.goZone("identite"), "alt+2": () => pdf.current?.goZone("notes"), "alt+3": () => pdf.current?.goZone("decision"),
    "alt+t": () => setTolerant((t) => !t),
    "f11": () => (focus ? exitFocus() : enterFocus()),
    "enter": (e) => {
      // Entrée sur la liste ou l'aperçu : plein écran (dans la recherche, Entrée est traitée par le champ)
      const t = e.target as HTMLElement | null;
      if (t && (t === inputRef.current || /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName))) return false;
      if (!focus) enterFocus();
    },
  }, active && !cursusFor && !demandeOpen);
  // Échap par couches : fenêtres modales > menus > mode focus > nouvelle recherche (App)
  useShortcuts({ "escape": () => exitFocus() }, active && focus && !cursusFor && !demandeOpen);

  useEffect(() => { if (active) inputRef.current?.focus(); }, [active]);
  // Échap / Ctrl+F depuis un autre écran : retour au guichet, recherche sélectionnée (nouvel étudiant)
  useEffect(() => { if (app.searchFocus) window.setTimeout(() => { if (focus) exitFocus(false); inputRef.current?.select(); }, 0); }, [app.searchFocus]); // eslint-disable-line react-hooks/exhaustive-deps
  // chiffres du jour tenus à jour (impressions des autres fenêtres, indexation), comme la 1.x (10 s)
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => app.refreshStats(), 10000);
    return () => window.clearInterval(t);
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
  // fin d'indexation : la recherche affichée est relancée (nouveaux relevés, pages exactes)
  useEffect(() => { if (app.indexing.last?.type === "exit" && app.info) void runSearch(query); }, [app.indexing.last]); // eslint-disable-line react-hooks/exhaustive-deps

  const onInputKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { if (focus) { e.preventDefault(); exitFocus(); } else setQuery(""); }
    else if (e.key === "Enter") {
      e.preventDefault();
      // recherche déjà à jour : Entrée ouvre le relevé sélectionné en plein écran ; sinon elle part sans attendre la frappe
      if (lastQ.current === query && current && !searching) enterFocus();
      else void runSearch(query);
    }
  };
  const [jump, setJump] = useState(0);
  useEffect(() => setJump(0), [current?.id]);
  const [pv, setPv] = useState<PdfViewState>({ fit: "auto", scale: null, overflow: false });
  const [mmHost, setMmHost] = useState<HTMLDivElement | null>(null);

  const counts = res?.counts || {};
  const total = res?.results.length || 0;
  const curDetail = detail && current && detail.releve.id === current.id ? detail : null;
  const rel = curDetail ? curDetail.releve : current;
  const message = !current ? (query.trim() ? "Aucun résultat pour cette recherche." : null)
    : render === "en cours" ? "Préparation de l'aperçu Word…" : render === "échec" ? `Aperçu Word impossible : ${renderErr}` : null;
  const onReveal = () => rel && void sx.reveal(rel.chemin).then((found) => toast(found ? "Dossier source ouvert (fichier surligné). Les originaux restent en lecture seule."
    : "Fichier original introuvable à son emplacement indexé (déplacé ou renommé ?) : dossier le plus proche ouvert. Lancez la vérification d'intégrité (Indexation).", { tone: found ? "green" : "amber", sound: false }));
  const students = new Set(results.map((r) => r.matricule)).size;
  const idx = current ? ordered.indexOf(current) : -1;
    const [variant, setVariant] = useState<PreviewVariant>("corrige");
  useEffect(() => setVariant("corrige"), [current?.id]);
  const apercu = useOriginalPreview(curDetail?.apercu?.original_disponible ? curDetail.releve.id : null, variant);
  const previewVariant: PreviewVariantSlot = curDetail?.apercu?.original_disponible
    ? <PreviewVariantToggle releveId={curDetail.releve.id} value={variant} onChange={setVariant} available /> : null;

  return (
    <>
      <header className="card topbar" data-testid="topbar">
        <div className="omni">
          <Icon name="search" size={19} color="#475569" />
          <input ref={inputRef} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onInputKey}
            placeholder="Matricule, nom, prénom ou année…" aria-label="Recherche" data-testid="search" spellCheck={false} autoFocus />
          {res?.norm && query.trim() && <Badge text={`Recherché : ${res.norm}`} tone="blue" title="Matricule normalisé (tirets, espaces, minuscules ignorés)" />}
          {res && query.trim() && !searching && <span className="muted tiny nowrap opt-wide" title="Résultats au fil de la frappe">{plural(students, "étudiant")} · {res.ms} ms</span>}
          {searching && <span className="spinner" style={{ color: "#94A3B8" }} />}
          {query && <button className="btn ghost sm icon" aria-label="Effacer la recherche" title="Effacer (Échap)" onClick={() => { setQuery(""); inputRef.current?.focus(); }} data-testid="search-clear"><Icon name="x" size={14} /></button>}
          {!query && <span className="hint" title="Échap, F2 ou Ctrl+F : nouvelle recherche, depuis n'importe quel écran">Échap</span>}
        </div>
        <div className="fseg" role="radiogroup" aria-label="Filière" data-testid="filieres">
          {FILIERES.map((f) => (
            <button key={String(f.code)} className={filiere === f.code ? "on" : ""} role="radio" aria-checked={filiere === f.code} onClick={() => setFiliere(f.code)} title={f.label}>
              {f.code ? <><span className="lg">{f.label}</span><span className="sh">{FIL_TINY[f.code] || f.label}</span></> : f.label}
              {total > 0 && <span className="n">{f.code ? counts[f.code] || 0 : total}</span>}
            </button>
          ))}
        </div>
        <span data-testid="tolerance"><Switch blue checked={tolerant} onChange={setTolerant} label={<span className="opt-mid">Tolérance</span>}
          title={`Tolérance aux fautes et phonétique (Alt+T) : ${tolerant ? "activée" : "désactivée, recherche exacte"}`} /></span>
        <span className="grow" />
        <DemandeChip active={activeD} list={openDemandes} onPick={(id) => app.setActiveDemande(id)} onAdd={() => void addToDemande()}
          onOpen={() => activeD && setDemandeOpen(activeD.id)} onNew={() => void newDemande(current)} canAdd={!!current} />
        <StatsButton />
      </header>

      <div className="gx-body">
        <section className="card gx-list" aria-label="Résultats">
          <div className="row" style={{ gap: 8 }}>
            <div className="grow">
              <div className="eyebrow">Résultats</div>
              <div className="h3 ellipsis" data-testid="count">{query.trim() ? `${plural(results.length, "relevé")} · ${plural(students, "étudiant")}` : "Prêt"}</div>
            </div>
            <div className="seg sm" role="radiogroup" aria-label="Présentation des résultats">
              <button className={view === "groupe" ? "on" : ""} role="radio" aria-checked={view === "groupe"} aria-label="Groupée par étudiant" title="Groupée par étudiant" onClick={() => setView("groupe")} data-testid="view-groupe"><Icon name="user" size={15} /></button>
              <button className={view === "releve" ? "on" : ""} role="radio" aria-checked={view === "releve"} aria-label="Par relevé" title="Par relevé (liste à plat, par pertinence)" onClick={() => setView("releve")} data-testid="view-releve"><Icon name="filter" size={15} /></button>
            </div>
          </div>
          {results.length === 0 ? (
            <Empty query={query.trim()} total={total} tolerant={tolerant} onAll={() => setFiliere(null)} onTolerant={() => setTolerant(true)} />
          ) : (
            <ResultsList groups={view === "groupe" ? groups : null} rows={ordered} current={current} onPick={pick} q={query}
              inDemande={demandeIds} onOpen={(id) => { pick(id); window.setTimeout(enterFocus, 0); }} />
          )}
          <div className="muted tiny gx-hints"><span className="key">↑</span><span className="key">↓</span> aperçu · <span className="key">Entrée</span> plein écran · <span className="key">Échap</span> nouvelle recherche</div>
        </section>

        <section className="gx-preview">
          <FicheStrip rel={rel} detail={curDetail} onReveal={onReveal} />
          <div className={`gx-stage${focus ? " focus" : ""}`} ref={stageRef} tabIndex={-1} data-testid="stage" aria-label="Aperçu du relevé">
            {focus && rel && (
              <FocusBar rel={rel} alerts={curDetail?.alerts || []} pos={idx} total={ordered.length} busy={busy} duplicata={duplicata}
                onPrint={() => void doPrint()} onDemande={() => void addToDemande()} onExport={() => void doExport()} onExit={() => exitFocus()}
                onMove={move} pv={pv} pdf={pdf} setMm={setMmHost} />
            )}
            <div className="gx-view">
              <PdfView ref={pdf} file={apercu.original?.file ?? (curDetail ? curDetail.preview_pdf : null)}
                first={apercu.original?.first ?? (rel?.numero_page || 1)} last={apercu.original?.last ?? (rel?.page_fin || 1)}
                version={`${rel?.id}-${version}${apercu.original ? "-o" : ""}`} message={apercu.message ?? message}
                stamp={duplicata && !apercu.original} jumpTo={jump || undefined}
                defaultFit="auto" toolbar={false} minimapHost={mmHost} onState={setPv} onDoubleClick={() => (focus ? exitFocus() : enterFocus())} />
            </div>
            {!focus && (
              <Dock rel={current ? rel : null} busy={busy} duplicata={duplicata} setDuplicata={setDuplicata} onPrint={() => void doPrint()}
                onDemande={() => void addToDemande()} onExport={() => void doExport()} onCursus={() => current && setCursusFor(current.matricule)}
                onFocus={enterFocus} pv={pv} pdf={pdf} setMm={setMmHost} jump={jump} setJump={setJump} variant={previewVariant} />
            )}
          </div>
        </section>
      </div>
      {cursusFor && <CursusDialog matricule={cursusFor} onClose={() => { setCursusFor(null); app.refreshStats(); }} />}
      {demandeOpen && <DemandeDialog did={demandeOpen} onClose={() => { setDemandeOpen(null); app.bumpDemandes(); app.refreshStats(); }} />}
    </>
  );
}

const FIL_TINY: Record<string, string> = { M: "Méd.", P: "Pharma", B: "Bioméd.", MS: "Masters" };

// ------------------------------------------------------------------ barre supérieure : demande, chiffres du jour
function DemandeChip({ active, list, onPick, onAdd, onOpen, onNew, canAdd }: {
  active: Demande | null; list: Demande[]; onPick: (id: number | null) => void; onAdd: () => void; onOpen: () => void; onNew: () => void; canAdd: boolean;
}) {
  const [open, setOpen] = useState(false);
  const run = (f: () => void) => () => { setOpen(false); f(); };
  return (
    <div className="anchor">
      <button className={`dchip${active ? " on" : ""}`} onClick={() => setOpen((o) => !o)} data-testid="demande-chip" aria-expanded={open}
        title="Demande en cours : une demande réunit les relevés demandés par une même personne. Ctrl+D ajoute le relevé affiché, Ctrl+N en crée une nouvelle.">
        <Icon name="folder" size={17} />
        <span className="dtxt">
          <span className="eyebrow opt-wide">{active ? `Demande ${active.numero}` : "Demande en cours"}</span>
          <span className="dn ellipsis" data-testid="demande-label">{active ? `${active.nom || active.matricule || "—"} · ${plural(active.n, "relevé")}` : "Aucune"}</span>
        </span>
        <Icon name="chevron_down" size={15} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} width={340} label="Demande en cours" testid="demande-menu">
        {active ? (
          <div className="pop-head">
            <div className="row"><span className="h3 grow ellipsis">{active.numero} · {active.nom || "—"}</span>
              <Badge text={STATUT_AIDE[active.statut] || active.statut} tone={STATUT_TONE[active.statut] || "gray"} /></div>
            <div className="muted small">Demandeur : {active.demandeur || "—"} · matricule {active.matricule || "—"} · {plural(active.n, "relevé")}</div>
          </div>
        ) : <div className="pop-head muted small">Aucune demande en cours. Une demande réunit les relevés demandés par une même personne.</div>}
        <button className="mi" disabled={!canAdd} onClick={run(onAdd)}><Icon name="plus" size={15} />Ajouter le relevé affiché<span className="kbd">Ctrl+D</span></button>
        <button className="mi" disabled={!active} onClick={run(onOpen)} data-testid="demande-open"><Icon name="eye" size={15} />Voir / préparer…</button>
        <button className="mi" onClick={run(onNew)} data-testid="demande-new"><Icon name="folder" size={15} />Nouvelle demande<span className="kbd">Ctrl+N</span></button>
        <div className="sep" />
        <div className="eyebrow" style={{ padding: "2px 10px" }}>Choisir une autre demande</div>
        <div className="mlist" data-testid="demande-picker" role="radiogroup" aria-label="Demande active">
          <button className={`mi${!active ? " on" : ""}`} role="radio" aria-checked={!active} onClick={run(() => onPick(null))} data-testid="demande-option">— aucune —</button>
          {list.map((d) => (
            <button key={d.id} className={`mi${active?.id === d.id ? " on" : ""}`} role="radio" aria-checked={active?.id === d.id} onClick={run(() => onPick(d.id))} data-testid="demande-option"
              title={`${STATUT_AIDE[d.statut] || d.statut} · demandeur : ${d.demandeur || "—"}`}>
              <span className="grow ellipsis">{d.numero} · {d.nom || d.matricule || "?"} ({d.n})</span>
            </button>
          ))}
        </div>
      </Popover>
    </div>
  );
}

function StatsButton() {
  const { stats, indexing, info, settings, openSettings, go } = useApp();
  const [open, setOpen] = useState(false);
  const s: Stats | null = stats;
  const lbl = s ? `${plural(s.imprimes, "imprimé")}${s.anomalies ? ` · ${plural(s.anomalies, "anomalie")}` : ""}` : "Chiffres du jour";
  return (
    <div className="anchor">
      <button className="btn ghost sm statsbtn" onClick={() => setOpen((o) => !o)} aria-expanded={open} data-testid="stats-btn" title={`Chiffres du jour : ${lbl}`}>
        <Icon name="chart" size={16} /><span className="opt-wide">{lbl}</span><Icon name="chevron_down" size={14} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} width={320} label="Chiffres du jour" testid="stats-pop">
        <div className="pop-head"><div className="eyebrow">Chiffres du jour</div></div>
        <div className="kv"><span>Imprimés aujourd'hui</span><b data-testid="stat-imprimes">{s ? plural(s.imprimes, "relevé") : "—"}</b></div>
        <div className="kv"><span>dont duplicata</span><b>{s ? s.duplicatas : "—"}</b></div>
        <div className="kv"><span>Anomalies à revoir</span><b style={s?.anomalies ? { color: "var(--amber)" } : undefined}>{s ? `${s.anomalies} fichier(s)` : "—"}</b></div>
        <div className="kv"><span>Base d'index</span><b>{s ? `${s.releves.toLocaleString("fr-FR")} relevés · ${s.etudiants.toLocaleString("fr-FR")} étudiants` : "—"}</b></div>
        <div className="kv"><span>Indexation</span><b>{indexing.running ? "en cours…" : s?.derniere_indexation || "jamais lancée"}</b></div>
        <div className="sep" />
        <div className="row" style={{ padding: "2px 6px", gap: 6 }}>
          <Badge text="Sources en lecture seule" tone="green" dot />
          <span className="grow" />
          <button className="btn link tiny" onClick={() => { setOpen(false); go("indexation"); }}>Écran Indexation</button>
        </div>
        <button className="mi" onClick={() => { setOpen(false); openSettings(); }} data-testid="settings" title="Réglages du poste : opérateur, imprimante, chemins (Ctrl+,)">
          <Icon name="sliders" size={15} />Poste {settings?.poste || info?.poste || ""} · {settings?.operateur || info?.operateur || "?"}<span className="kbd">Ctrl+,</span>
        </button>
      </Popover>
    </div>
  );
}

// ------------------------------------------------------------------ résultats
function ResultsList({ groups, rows, current, onPick, q, inDemande, onOpen }: {
  groups: StudentGroup[] | null; rows: Releve[]; current: Releve | null; onPick: (id: number) => void; q: string; inDemande: Set<number>; onOpen: (id: number) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    (box.current?.querySelector(".it.sel") as HTMLElement | null)?.scrollIntoView({ block: "nearest" });
  }, [current?.id, groups]);
  // dossier complet de chaque étudiant (périodes manquantes) : en tête de groupe, sans retarder la liste
  const [dossiers, setDossiers] = useState<Record<string, Group>>({});
  const ids = rows.map((r) => r.id).join(",");
  useEffect(() => {
    if (!groups) return;
    let alive = true;
    const t = window.setTimeout(() => {
      void call<Group[]>("groups", { releve_ids: rows.map((r) => r.id) })
        .then((gs) => alive && setDossiers(Object.fromEntries(gs.map((g) => [g.matricule, g])))).catch(() => undefined);
    }, 250);
    return () => { alive = false; window.clearTimeout(t); };
  }, [ids, !!groups]); // eslint-disable-line react-hooks/exhaustive-deps

  const item = (r: Releve, flat: boolean) => {
    const flags = inDemande.has(r.id) && !r.flags.includes("✓ DEMANDE") ? ["✓ DEMANDE", ...r.flags] : r.flags;
    const on = current?.id === r.id;
    return (
      <div key={r.id} className={`it${on ? " sel" : ""}`} onClick={() => onPick(r.id)} onDoubleClick={() => onOpen(r.id)}
        role="option" aria-selected={on} data-testid="result-item" title="Double-clic ou Entrée : plein écran">
        {flat && <div className="l0"><span className="mono">{highlight(r.matricule, q)}</span><span className="ellipsis">{highlight(r.nom_complet, q)}</span></div>}
        <div className="l1"><b className="nowrap">{periodLabel(r)}</b><span className="muted nowrap">{r.annee_academique || "?"}</span><span className="grow" />
          <Badge text={r.statut || "—"} tone={statutTone(r.statut)} dot /></div>
        <div className="l2"><span className="ellipsis">{ligne2(r) || (flat ? FIL_SHORT[r.filiere_code || ""] || r.filiere || "" : "")}</span><span className="grow" /><span className="nowrap">{pagesShort(r)}</span></div>
        {flags.length > 0 && <div className="flags">{flags.map((f) => <Badge key={f} text={f} tone={flagTone(f)} />)}</div>}
      </div>
    );
  };
  return (
    <div className="gx-items" ref={box} role="listbox" aria-label="Relevés trouvés" data-testid="results">
      {groups ? groups.map((g) => {
        const d = dossiers[g.matricule];
        return (
          <div key={g.matricule} className="grp" data-testid="result-group">
            <div className="ghead">
              <div className="row" style={{ gap: 8 }}><span className="mono">{highlight(g.matricule, q)}</span><span className="gname grow ellipsis" title={g.nom}>{highlight(g.nom, q)}</span></div>
              <div className="row tiny muted" style={{ gap: 6 }}>
                <span className="nowrap">{g.filiere} · {plural(g.rows.length, "relevé")}</span><span className="grow" />
                {d && <Badge text={d.missing.length ? `${d.missing.join(", ")} manquant${d.missing.length > 1 ? "s" : ""}` : `Complet · ${d.valides}/${d.total} validé(s)`} tone={d.tone}
                  title={d.missing.length ? `Périodes absentes du dossier indexé : ${d.missing.join(", ")}` : "Toutes les périodes du cursus sont indexées"} />}
              </div>
            </div>
            {g.rows.map((r) => item(r, false))}
          </div>
        );
      }) : rows.map((r) => item(r, true))}
    </div>
  );
}

/** Seconde ligne (1.x) : rattrapage, sinon moyenne · grade, sinon session ; rien d'inventé. */
export function ligne2(r: Releve) {
  if (isRattrapage(r)) return "Session de rattrapage";
  if (r.moyenne) return `Moy. ${r.moyenne}${r.grade ? ` · ${r.grade}` : ""}`;
  if (r.session) return `Session ${r.session.toLowerCase()}`;
  return "";
}

function Empty({ query, total, tolerant, onAll, onTolerant }: { query: string; total: number; tolerant: boolean; onAll: () => void; onTolerant: () => void }) {
  if (!query) return (
    <div className="empty"><h2 className="h2">Qui se présente au guichet ?</h2>
      <div className="muted small">Tapez le matricule (18M042, 18-M-042, 18 m 042…), le nom ou le prénom, ou une année. Les résultats s'affichent pendant la frappe ; ↑ ↓ pour parcourir, Entrée pour l'aperçu en plein écran.</div></div>
  );
  if (total) return (
    <div className="empty"><h2 className="h2">Aucun relevé de cette filière pour « {query} »</h2>
      <div className="muted small">{plural(total, "relevé trouvé", "relevés trouvés")} dans d'autres filières.</div>
      <button className="btn secondary" onClick={onAll}>Afficher toutes les filières ({total})</button></div>
  );
  return (
    <div className="empty" data-testid="empty"><h2 className="h2">Aucun relevé pour « {query} »</h2>
      <div className="muted small">Pistes : vérifiez l'orthographe ou tapez seulement le nom de famille ; pour un matricule, essayez sans la lettre de filière (ex. 18 042) ; un relevé récent n'est peut-être pas encore indexé (écran Indexation, Ctrl+3).</div>
      {!tolerant && <button className="btn secondary" onClick={onTolerant}>Essayer avec la tolérance aux fautes</button>}</div>
  );
}

// ------------------------------------------------------------------ fiche condensée (au-dessus de la page)
function shortAlert(t: string) {
  // libellé court de la puce (texte complet en infobulle et au clic) : jusqu'au premier « . », « : » ou « — »
  const cut = t.split(/(?<=[^.]{12,})(?:\. | : | — | \(autres)/)[0];
  return cut.length > 90 ? cut.slice(0, 88) + "…" : cut;
}

function recapBits(rel: Releve) {
  const bits: [string, string][] = [];
  if (rel.moyenne) bits.push(["Moyenne", `${rel.moyenne}/20${rel.grade ? ` · grade ${rel.grade}` : ""}${rel.mgp ? ` (MGP ${rel.mgp})` : ""}`]);
  else bits.push(["Moyenne", "non lue"]);
  if (rel.decision_deduite) bits.push(["Décision", `déduite : ${rel.decision}`]);
  else if (rel.decision) bits.push(["Décision", `« ${rel.decision} »`]);
  bits.push(["Session", isRattrapage(rel) ? "rattrapage" : rel.session ? rel.session.toLowerCase() : "—"]);
  if (rel.credits != null) bits.push(["Crédits", creditsLabel(rel, true)]);
  if (rel.date_connue) bits.push(["Naissance", `${rel.date_connue}${rel.lieu_naissance ? ` à ${rel.lieu_naissance}` : ""}`]);
  else if (rel.date_naissance) bits.push(["Naissance", "non renseignée sur le relevé"]);
  bits.push(["Filière", rel.filiere || FIL_SHORT[rel.filiere_code || ""] || "?"]);
  bits.push(["À imprimer", rel.pages_label + (rel.nb_pages > 1 ? " liées" : "")]);
  return bits;
}

function FicheStrip({ rel, detail, onReveal }: { rel: Releve | null; detail: ReleveDetail | null; onReveal: () => void }) {
  const [expand, setExpand] = useState(false);
  const [details, setDetails] = useState(false);
  useEffect(() => { setDetails(false); }, [rel?.id]);
  if (!rel) return (
    <div className="card strip" data-testid="recap"><span className="eyebrow">Contrôle avant impression</span><span className="nm muted">Aucun relevé sélectionné</span></div>
  );
  const alerts = detail?.alerts || [];
  const sem = rel.semestre ? rel.sem_label + (rel.annuel ? " annuel" : "") : "";
  const facts: ReactNode[] = [rel.annee_academique || "année ?", rel.niveau || "niveau ?"];
  if (sem) facts.push(sem);
  if (rel.credits != null) facts.push(<span className="opt-mid" key="cr">{creditsLabel(rel)}</span>);
  if (rel.date_connue) facts.push(<span key="nais">né(e) le {rel.date_connue}<span className="opt-wide">{rel.lieu_naissance ? ` à ${rel.lieu_naissance}` : ""}</span></span>);
  facts.push(rel.nb_pages > 1 ? `pages ${rel.numero_page}–${rel.page_fin}` : `page ${rel.numero_page}`);
  const bits = recapBits(rel);
  const bitsText = bits.map(([k, v]) => `${k} : ${v}`).join(" · ");
  return (
    <div className="card strip" data-testid="recap">
      <Badge text={rel.statut || "Décision non lue"} tone={statutTone(rel.statut)} big dot />
      <span className="nm ellipsis selectable" title={rel.nom_complet}>{nomAffiche(rel)}</span>
      <span className="mat selectable">{rel.matricule}</span>
      <span className="facts selectable" data-testid="recap-line">{facts.map((f, i) => <span key={i}>{i > 0 && <i>·</i>}{f}</span>)}</span>
      {isRattrapage(rel) && <Badge text="RATTRAPAGE" tone="amber" />}
      <span className="grow" />
      {alerts.length > 0 && (
        <span className="chips">
          {alerts.map((a, i) => (
            <button key={i} className={`achip t-${a.tone}${a.tone === "red" ? " strong" : ""}`} title={`${a.text}\n(cliquer pour ${expand ? "replier" : "déplier"} les alertes)`}
              onClick={() => setExpand((x) => !x)} data-testid="alert-chip">
              <Icon name={a.tone === "green" ? "check" : a.tone === "blue" ? "info" : "alert"} size={13} /><span className="ellipsis">{shortAlert(a.text)}</span>
            </button>
          ))}
        </span>
      )}
      <div className="anchor">
        <button className="btn link tiny" onClick={() => setDetails((d) => !d)} title={bitsText} data-testid="recap-details" aria-expanded={details}>Détails</button>
        <Popover open={details} onClose={() => setDetails(false)} width={380} label="Détails du relevé" testid="recap-pop">
          <div className="pop-head"><div className="eyebrow">Contrôle avant impression</div><div className="h3 selectable">{nomAffiche(rel)} · <span className="mono">{rel.matricule}</span></div></div>
          {bits.map(([k, v]) => <div key={k} className="kv"><span>{k}</span><b className="selectable">{v}</b></div>)}
          <div className="kv src"><span>Source</span><b className="selectable" data-testid="recap-source">{rel.chemin} · {rel.type_fichier}</b></div>
          {alerts.map((a, i) => <Alert key={i} text={a.text} tone={a.tone} />)}
        </Popover>
      </div>
      <button className="btn link tiny nowrap" onClick={onReveal} title={`Ouvrir l'emplacement du fichier original (lecture seule) : ${rel.chemin}`} data-testid="recap-reveal">Fichier original</button>
      {expand && alerts.length > 0 && <div className="strip-alerts">{alerts.map((a, i) => <Alert key={i} text={a.text} tone={a.tone} />)}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ dock d'actions et d'affichage
interface DockProps {
  rel: Releve | null; busy: "" | "print" | "export"; duplicata: boolean; setDuplicata: (v: boolean) => void;
  onPrint: () => void; onDemande: () => void; onExport: () => void; onCursus: () => void; onFocus: () => void;
  pv: PdfViewState; pdf: React.RefObject<PdfViewHandle | null>; setMm: (el: HTMLDivElement | null) => void;
  jump: number; setJump: (p: number) => void;
  variant?: PreviewVariantSlot;     // bascule « Aperçu : corrigé / original » (fix/apercu-doc)
}

function Dock({ rel, busy, duplicata, setDuplicata, onPrint, onDemande, onExport, onCursus, onFocus, pv, pdf, setMm, jump, setJump, variant }: DockProps) {
  const fitOn = (m: string) => pv.fit === m;
  return (
    <aside className="dock" data-testid="dock" aria-label="Actions et affichage">
      <div className="grp">
        <button className="btn primary" disabled={!rel || !!busy} onClick={onPrint} data-testid="print" title="Imprimer ce relevé (Ctrl+P)">
          <span className="r">{busy === "print" ? <span className="spinner" /> : <Icon name="printer" size={18} />}Imprimer</span><span className="kbd">Ctrl+P</span>
        </button>
        <button className="btn secondary sm" disabled={!rel} onClick={onDemande} data-testid="add-demande" title="Ajouter à la demande en cours (Ctrl+D)"><Icon name="plus" size={15} />Demande<span className="kbd">Ctrl+D</span></button>
        <button className="btn ghost sm" disabled={!rel || !!busy} onClick={onExport} data-testid="export" title="Exporter en PDF (Ctrl+E)">
          {busy === "export" ? <span className="spinner" /> : <Icon name="download" size={15} />}Exporter<span className="kbd">Ctrl+E</span></button>
        <button className="btn ghost sm" disabled={!rel} onClick={onCursus} data-testid="cursus" title="Cursus complet (Ctrl+Maj+E)"><Icon name="layers" size={15} />Cursus<span className="kbd">Ctrl+Maj+E</span></button>
        <Switch checked={duplicata} onChange={setDuplicata} label="Duplicata" title="Timbre « Duplicata conforme » horodaté et journalisé · l'original reste intact" />
      </div>
      <div className="grp">
        <span className="lbl row" style={{ gap: 4 }}><span className="grow">Affichage</span><span className="pct" data-testid="scale">{pv.scale ? `${Math.round(pv.scale * 100)} %` : ""}</span></span>
        <div className="fitgrid" role="radiogroup" aria-label="Ajustement de l'aperçu">
          {FIT_LABELS.map(([m, l, t]) => (
            <button key={m} className={`btn ghost xs${fitOn(m) ? " on" : ""}`} role="radio" aria-checked={fitOn(m)} title={t} onClick={() => pdf.current?.setFit(m)} data-testid={`fit-${m}`}>{l}</button>
          ))}
        </div>
        <div className="fitgrid three">
          <button className="btn ghost xs" aria-label="Zoom arrière" title="Zoom arrière (ou Ctrl + molette)" onClick={() => pdf.current?.zoomBy(1 / 1.2)}>−</button>
          <button className="btn ghost xs" aria-label="Zoom avant" title="Zoom avant (ou Ctrl + molette)" onClick={() => pdf.current?.zoomBy(1.2)}>+</button>
          <button className="btn ghost xs" disabled={!rel} onClick={onFocus} data-testid="focus-open" aria-label="Plein écran"
            title="Aperçu en plein écran (Entrée, F11 ou double-clic sur la page ; Échap pour sortir)"><Icon name="fit" size={14} /></button>
        </div>
        {/* emplacement réservé : « Aperçu : corrigé / original » (branche fix/apercu-doc) */}
        {variant && <div className="variant" data-testid="dock-variant">{variant}</div>}
        {rel && rel.nb_pages > 1 && (
          <>
            <span className="lbl">Pages</span>
            <div className="fitgrid">{Array.from({ length: rel.nb_pages }, (_, i) => rel.numero_page + i).map((p) => (
              <button key={p} className={`btn ghost xs${jump === p ? " on" : ""}`} title={`Aller à la page ${p}`} onClick={() => setJump(p)} data-testid={`page-${p}`}>p. {p}</button>
            ))}</div>
          </>
        )}
        <span className="lbl">Points de contrôle</span>
        {ZONES.map((z) => (
          <button key={z.id} className="btn ghost xs zone" disabled={!rel} onClick={() => pdf.current?.goZone(z.id)} title={`${z.label} : aller à la zone et la surligner (${z.key})`} data-testid={`zone-${z.id}`}>
            <Icon name={z.icon} size={13} /><span className="grow">{z.label}</span><span className="kbd">{z.key}</span>
          </button>
        ))}
        <div className="mm-host" ref={setMm} />
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------ mode focus : barre flottante
function FocusBar({ rel, alerts, pos, total, busy, duplicata, onPrint, onDemande, onExport, onExit, onMove, pv, pdf, setMm }: {
  rel: Releve; alerts: AlertT[]; pos: number; total: number; busy: string; duplicata: boolean;
  onPrint: () => void; onDemande: () => void; onExport: () => void; onExit: () => void; onMove: (n: number) => void;
  pv: PdfViewState; pdf: React.RefObject<PdfViewHandle | null>; setMm: (el: HTMLDivElement | null) => void;
}) {
  const worst = alerts.find((a) => a.tone === "red") || alerts.find((a) => a.tone === "amber");
  return (
    <>
      <div className="fbar" data-testid="focus-bar">
        <Badge text={rel.statut || "Décision non lue"} tone={statutTone(rel.statut)} dot />
        <b className="nm ellipsis" title={rel.nom_complet}>{nomAffiche(rel)}</b>
        <span className="mono">{rel.matricule}</span>
        <span className="muted small nowrap opt-mid" data-testid="focus-facts">{rel.annee_academique || "?"} · {periodLabel(rel)} · {rel.nb_pages > 1 ? `pages ${rel.numero_page}–${rel.page_fin}` : `page ${rel.numero_page}`}</span>
        {worst && <span className={`badge t-${worst.tone}`} title={alerts.map((a) => a.text).join("\n")}><Icon name="alert" size={12} />{plural(alerts.length, "alerte")}</span>}
        {duplicata && <Badge text="DUPLICATA" tone="red" title="Timbre « Duplicata conforme » apposé à l'impression" />}
        <button className="btn primary sm" disabled={!!busy} onClick={onPrint} data-testid="focus-print">{busy === "print" ? <span className="spinner" /> : <Icon name="printer" size={16} />}Imprimer<span className="kbd">Ctrl+P</span></button>
        <button className="btn secondary sm" onClick={onDemande}><Icon name="plus" size={14} />Demande<span className="kbd opt-wide">Ctrl+D</span></button>
        <button className="btn ghost sm" disabled={!!busy} onClick={onExport}><Icon name="download" size={14} />Exporter<span className="kbd opt-wide">Ctrl+E</span></button>
        <button className="btn ghost sm" onClick={onExit} data-testid="focus-exit" title="Quitter le plein écran (Échap)"><Icon name="x" size={14} />Quitter<span className="kbd">Échap</span></button>
      </div>
      <div className="fside" aria-label="Navigation et affichage">
        <button className="btn ghost xs icon" aria-label="Relevé précédent" title="Relevé précédent (↑)" disabled={pos <= 0} onClick={() => onMove(-1)}><Icon name="arrow_up" size={15} /></button>
        <span className="tiny muted" style={{ textAlign: "center" }} data-testid="focus-pos">{pos + 1}/{total}</span>
        <button className="btn ghost xs icon" aria-label="Relevé suivant" title="Relevé suivant (↓)" disabled={pos >= total - 1} onClick={() => onMove(1)}><Icon name="arrow_down" size={15} /></button>
        <div className="sep" />
        {FIT_LABELS.filter(([m]) => m !== "largeur").map(([m, l, t]) => (
          <button key={m} className={`btn ghost xs${pv.fit === m ? " on" : ""}`} title={t} onClick={() => pdf.current?.setFit(m)}>{l}</button>
        ))}
        <div className="sep" />
        {ZONES.map((z, i) => (
          <button key={z.id} className="btn ghost xs icon" title={`${z.label} (${z.key})`} aria-label={z.label} onClick={() => pdf.current?.goZone(z.id)}><Icon name={z.icon} size={14} /><sub>{i + 1}</sub></button>
        ))}
        <div className="mm-host" ref={setMm} />
      </div>
    </>
  );
}
