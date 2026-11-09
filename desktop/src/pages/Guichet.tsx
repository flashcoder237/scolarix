import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  call, sx, FILIERES, FIL_SHORT, flagTone, plural, statutTone,
  type CheckResult, type Demande, type DemandeFull, type Group, type Releve, type ReleveDetail, type SearchResult,
} from "../api";
import { useApp, useShortcuts } from "../state";
import { Alert, Badge, Chip, Icon, Seg, Switch, dateFr, highlight, play, useDebounced, useDialogs, useToast } from "../components/ui";
import { PdfView, forget } from "../components/PdfView";
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
  return sub(ta, tb) || sub(tb, ta);
}

type Pending = { kind: "print" | "export"; id: number } | null;

export function Guichet() {
  const app = useApp();
  const toast = useToast();
  const { ask, prompt } = useDialogs();
  const active = app.page === "guichet";
  const inputRef = useRef<HTMLInputElement>(null);

  // ---------------------------------------------------------------- recherche
  const [query, setQuery] = useState("");
  const [tolerant, setTolerant] = useState(true);      // comme la 1.x : tolérance aux fautes par défaut
  const [filiere, setFiliere] = useState<string | null>(null);
  const [view, setView] = useState<"releve" | "groupe">("releve");
  const [res, setRes] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const debounced = useDebounced(query, 120);
  const searchSeq = useRef(0);

  const runSearch = useCallback(async (q: string) => {
    const my = ++searchSeq.current;
    setSearching(true);
    try {
      const r = await call<SearchResult>("search", { q, tolerant, demande_id: app.activeDemande });
      if (my === searchSeq.current) { setRes(r); setSel(0); }
    } catch (e) {
      if (my === searchSeq.current) toast(`Recherche impossible : ${errMsg(e)}`, { tone: "red" });
    } finally {
      if (my === searchSeq.current) setSearching(false);
    }
  }, [tolerant, app.activeDemande, toast]);

  useEffect(() => { if (app.info) void runSearch(debounced); }, [debounced, tolerant, app.info, runSearch]);

  const results = useMemo(() => (res?.results || []).filter((r) => !filiere || r.filiere_code === filiere), [res, filiere]);
  const [sel, setSel] = useState(0);
  const current: Releve | null = results[Math.min(sel, results.length - 1)] || null;

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
            // pages exactes connues : la ligne du tableau est mise à jour
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
    } catch { /* moteur en redémarrage : la barre se remettra à jour */ }
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

  // ---------------------------------------------------------------- clavier
  const move = (step: number) => setSel((s) => Math.max(0, Math.min(results.length - 1, s + step)));
  useShortcuts({
    "ctrl+p": () => void doPrint(), "ctrl+e": () => void doExport(), "ctrl+shift+e": () => current && setCursusFor(current.matricule),
    "ctrl+d": () => void addToDemande(), "ctrl+n": () => void newDemande(current), "ctrl+k": () => inputRef.current?.select(),
    "ctrl+f": () => inputRef.current?.select(), "f2": () => inputRef.current?.select(),
  }, active && !cursusFor && !demandeOpen);
  useEffect(() => { if (active) inputRef.current?.focus(); }, [active]);
  // Échap / Ctrl+F depuis un autre écran : retour au guichet, recherche sélectionnée (nouvel étudiant)
  useEffect(() => { if (app.searchFocus) window.setTimeout(() => inputRef.current?.select(), 0); }, [app.searchFocus]);
  // chiffres du jour tenus à jour (impressions des autres fenêtres, indexation), comme la 1.x (10 s)
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => app.refreshStats(), 10000);
    return () => window.clearInterval(t);
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
  // fin d'indexation : la recherche affichée est relancée (nouveaux relevés, pages exactes)
  useEffect(() => { if (app.indexing.last?.type === "exit" && app.info) void runSearch(query); }, [app.indexing.last]); // eslint-disable-line react-hooks/exhaustive-deps

  const onInputKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "PageDown") { e.preventDefault(); move(8); }
    else if (e.key === "PageUp") { e.preventDefault(); move(-8); }
    else if (e.key === "Escape") { setQuery(""); }
    else if (e.key === "Enter") { e.preventDefault(); void runSearch(query); }     // sans attendre la fin de la frappe
  };
  const [jump, setJump] = useState(0);
  useEffect(() => setJump(0), [current?.id]);

  const counts = res?.counts || {};
  const total = res?.results.length || 0;
  const rel = detail && current && detail.releve.id === current.id ? detail.releve : current;
  const message = !current ? (query.trim() ? "Aucun résultat : voir les pistes à gauche." : null)
    : render === "en cours" ? "Préparation de l'aperçu Word…" : render === "échec" ? `Aperçu Word impossible : ${renderErr}` : null;

  return (
    <>
      <Header />
      {!app.compact && <StatsRow />}
      <DemandeBar active={activeD} list={openDemandes} onPick={(id) => app.setActiveDemande(id)} onAdd={() => void addToDemande()}
        onOpen={() => activeD && setDemandeOpen(activeD.id)} onNew={() => void newDemande(current)} canAdd={!!current} />
      <section className="card" style={{ flex: "none" }}>
        <div className="omni">
          <Icon name="search" size={22} color="#475569" />
          <input ref={inputRef} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onInputKey}
            placeholder="Matricule, nom, prénom ou année…" aria-label="Recherche" data-testid="search" spellCheck={false} autoFocus />
          {res?.norm && query.trim() && <Badge text={`Recherché : ${res.norm}`} tone="blue" title="Matricule normalisé (tirets, espaces, minuscules ignorés)" />}
          {searching && <span className="spinner" style={{ color: "#94A3B8" }} />}
          {query && <button className="btn ghost sm icon" aria-label="Effacer la recherche" title="Effacer (Échap)" onClick={() => { setQuery(""); inputRef.current?.focus(); }} data-testid="search-clear"><Icon name="x" size={14} /></button>}
          <span className="hint" title="Échap ou Ctrl+F : nouvelle recherche, depuis n'importe quel écran">Échap</span>
        </div>
        <div className="row wrap" style={{ gap: 8 }}>
          <span className="eyebrow">Filière</span>
          {FILIERES.map((f) => (
            <button key={String(f.code)} className={`pill${filiere === f.code ? " on" : ""}`} onClick={() => setFiliere(f.code)}>
              {f.label}{total > 0 && <span className="n">{f.code ? counts[f.code] || 0 : total}</span>}
            </button>
          ))}
          <span style={{ width: 16 }} />
          <span className="eyebrow">Mode</span>
          <Seg value={tolerant} options={[[false, "Exacte"], [true, "Tolérance fautes / phonétique"]]} onChange={setTolerant} label="Mode de recherche" />
          <span className="grow" />
          {!app.compact && <span className="muted tiny">{res && query.trim() ? `${res.ms} ms · au fil de la frappe` : "Tapez un matricule (18M042, 18-M-042…), un nom ou une année"}</span>}
        </div>
      </section>

      <div className="row" style={{ flex: 1, minHeight: 0, alignItems: "stretch", gap: 14 }}>
        <section className="card" style={{ flex: "57 1 0", minWidth: 0 }}>
          <div className="row">
            <div className="grow">
              <div className="eyebrow">Résultats · triés par pertinence</div>
              <div className="h3" data-testid="count">{query.trim() ? `${plural(results.length, "relevé")} · ${plural(new Set(results.map((r) => r.matricule)).size, "étudiant")}` : "Prêt"}</div>
            </div>
            <Seg value={view} options={[["releve", "Par relevé"], ["groupe", "Groupée par étudiant"]]} onChange={setView} label="Affichage" />
          </div>
          {results.length === 0 ? (
            <Empty query={query.trim()} total={total} tolerant={tolerant} onAll={() => setFiliere(null)} onTolerant={() => setTolerant(true)} />
          ) : view === "releve" ? (
            <ResultsTable rows={results} sel={sel} onSel={setSel} q={query} inDemande={demandeIds} />
          ) : (
            <GroupedView rows={results} current={current} onPick={(id) => setSel(results.findIndex((r) => r.id === id))} />
          )}
          <div className="muted tiny ellipsis">↑ ↓ parcourir + aperçu · Ctrl+P imprimer · Ctrl+E exporter · Ctrl+Maj+E cursus · Ctrl+D ajouter à la demande · Échap nouvelle recherche</div>
        </section>

        <section className="col" style={{ flex: "43 1 0", minWidth: 420, gap: 10 }}>
          <Recap rel={rel} detail={detail && current && detail.releve.id === current.id ? detail : null} compact={app.compact}
            onReveal={() => rel && void sx.reveal(rel.chemin).then((found) => toast(found ? "Dossier source ouvert (fichier surligné). Les originaux restent en lecture seule."
              : "Fichier original introuvable à son emplacement indexé (déplacé ou renommé ?) : dossier le plus proche ouvert. Lancez la vérification d'intégrité (Indexation).", { tone: found ? "green" : "amber", sound: false }))} />
          <div className="card grow" style={{ gap: 8 }}>
            <div className="row">
              {rel && rel.nb_pages > 1 && Array.from({ length: rel.nb_pages }, (_, i) => rel.numero_page + i).map((p) => (
                <button key={p} className={`badge ${jump === p ? "t-blue" : "t-gray"}`} style={{ border: "none", cursor: "pointer" }} title={`Aller à la page ${p}`}
                  onClick={() => setJump(p)} data-testid={`page-${p}`}>Page {p}</button>
              ))}
              {rel && rel.nb_pages === 1 && <span className="badge t-gray">Page {rel.numero_page}</span>}
              <span className="grow" />
              <Switch checked={duplicata} onChange={setDuplicata} label="Duplicata" title="Timbre « Duplicata conforme » horodaté et journalisé · l'original reste intact" />
            </div>
            <PdfView file={detail && current && detail.releve.id === current.id ? detail.preview_pdf : null} first={rel?.numero_page || 1}
              last={rel?.page_fin || 1} version={`${rel?.id}-${version}`} message={message} stamp={duplicata} jumpTo={jump || undefined} />
          </div>
          <div className="card" style={{ flex: "none", flexDirection: "row", gap: 8, padding: app.compact ? "10px 12px" : 14 }}>
            <button className="btn primary grow" disabled={!current || !!busy} onClick={() => void doPrint()} data-testid="print">
              {busy === "print" ? <span className="spinner" /> : <Icon name="printer" size={19} />}
              {app.compact ? "Imprimer" : "Imprimer ce relevé"} <span className="kbd">Ctrl+P</span>
            </button>
            <button className="btn secondary" disabled={!current || !!busy} onClick={() => void doExport()} title="Exporter en PDF (Ctrl+E)" data-testid="export">
              {busy === "export" ? <span className="spinner" /> : <Icon name="download" />}Exporter
            </button>
            <button className="btn secondary" disabled={!current} onClick={() => current && setCursusFor(current.matricule)} title="Cursus complet (Ctrl+Maj+E)" data-testid="cursus">
              <Icon name="layers" />Cursus
            </button>
          </div>
        </section>
      </div>
      {cursusFor && <CursusDialog matricule={cursusFor} onClose={() => { setCursusFor(null); app.refreshStats(); }} />}
      {demandeOpen && <DemandeDialog did={demandeOpen} onClose={() => { setDemandeOpen(null); app.bumpDemandes(); app.refreshStats(); }} />}
    </>
  );
}

// ------------------------------------------------------------------ en-tête et chiffres
export function greeting(h = new Date().getHours()) {
  return h < 12 ? "Bonjour" : h < 18 ? "Bon après-midi" : "Bonsoir";
}

function Header() {
  const { info, stats, compact, openSettings, settings } = useApp();
  return (
    <header className="header">
      <div className="titles">
        {!compact && <div className="eyebrow">{dateFr()} · Guichet d'exploitation</div>}
        <h1 className="h1">{greeting()}, {settings?.operateur || info?.operateur || "…"}</h1>
      </div>
      {compact && stats && (
        <span className="muted small" title={`Indexation : ${stats.derniere_indexation || "jamais"}`}>
          {stats.releves.toLocaleString("fr-FR")} relevés · {stats.imprimes} imprimé(s) aujourd'hui{stats.anomalies ? ` · ${stats.anomalies} anomalie(s)` : ""}
        </span>
      )}
      <Badge text="Sources en lecture seule" tone="green" big dot />
      <button className="btn ghost" onClick={() => openSettings()} title="Réglages du poste : opérateur, imprimante, chemins" data-testid="settings">
        <Icon name="sliders" />Poste {settings?.poste || ""}
      </button>
    </header>
  );
}

function StatsRow() {
  const { stats, indexing } = useApp();
  const ev = indexing.last;
  const prog = indexing.running && ev?.type === "progress" && ev.total ? { done: ev.done as number, total: ev.total as number } : null;
  const items: [string, any, string, string][] = [
    ["shield", "green", "Base d'index", stats ? `${stats.releves.toLocaleString("fr-FR")} relevés · ${stats.etudiants.toLocaleString("fr-FR")} étudiants` : "—"],
    ["database", "blue", "Indexation", prog ? `${Math.floor((prog.done * 100) / prog.total)} % · ${prog.done} / ${prog.total} fichiers` : indexing.running ? "En cours…" : stats?.derniere_indexation ? `Dernière passe : ${stats.derniere_indexation}` : "Jamais lancée"],
    ["printer", "violet", "Imprimés aujourd'hui", stats ? `${stats.imprimes} relevés · ${stats.duplicatas} duplicata(s)` : "—"],
    ["alert", "amber", "Anomalies à revoir", stats ? `${stats.anomalies} fichier(s)` : "—"],
  ];
  return (
    <div className="stats-grid">
      {items.map(([icon, tone, label, value]) => (
        <div key={label} className="card kpi" style={{ flexDirection: "row", padding: 14 }}>
          <Chip icon={icon} tone={tone} />
          <div className="grow" style={{ minWidth: 0 }}><div className="eyebrow">{label}</div><div className="h3 ellipsis" title={value}>{value}</div>
            {label === "Indexation" && prog && <div className="progress" style={{ marginTop: 6, height: 6 }}><div style={{ width: `${(prog.done * 100) / prog.total}%` }} /></div>}</div>
        </div>
      ))}
    </div>
  );
}

function DemandeBar({ active, list, onPick, onAdd, onOpen, onNew, canAdd }: {
  active: Demande | null; list: Demande[]; onPick: (id: number | null) => void; onAdd: () => void; onOpen: () => void; onNew: () => void; canAdd: boolean;
}) {
  const { compact } = useApp();
  return (
    <section className="card" style={{ flexDirection: "row", alignItems: "center", flex: "none", padding: compact ? "8px 12px" : 14 }} data-testid="demande-bar">
      {!compact && <Chip icon="folder" tone="violet" size={36} />}
      <div className="grow" style={{ minWidth: 0 }}>
        {!compact && <div className="eyebrow">Demande en cours</div>}
        <div className="h3 ellipsis" data-testid="demande-label" title="Une demande réunit les relevés demandés par une même personne : Ctrl+D ajoute le relevé affiché.">
          {active ? (compact ? `Pour : ${active.nom || active.matricule || "—"} · ${plural(active.n, "relevé")}` : `${active.numero} · ${active.nom || "—"} · ${plural(active.n, "relevé")}`)
            : compact ? "Demande en cours : aucune" : "Aucune — une demande réunit les relevés demandés par une même personne"}
        </div>
      </div>
      {active && <Badge text={STATUT_AIDE[active.statut] || active.statut} tone={STATUT_TONE[active.statut] || "gray"} title={`Demandeur : ${active.demandeur || "—"} · matricule ${active.matricule || "—"}`} />}
      <select className="field" style={{ width: compact ? 190 : 230 }} value={active?.id ?? ""} onChange={(e) => onPick(e.target.value ? Number(e.target.value) : null)} aria-label="Demande active" data-testid="demande-picker">
        <option value="">— aucune —</option>
        {list.map((d) => <option key={d.id} value={d.id}>{d.numero} · {d.nom || d.matricule || "?"} ({d.n})</option>)}
      </select>
      <button className="btn secondary" disabled={!canAdd} onClick={onAdd} title="Ajouter le relevé affiché à la demande (Ctrl+D)"><Icon name="plus" />Ajouter <span className="kbd">Ctrl+D</span></button>
      <button className="btn ghost" disabled={!active} onClick={onOpen}>Voir / préparer</button>
      <button className="btn ghost" onClick={onNew} title="Nouvelle demande (Ctrl+N)">Nouvelle <span className="kbd">Ctrl+N</span></button>
    </section>
  );
}

// ------------------------------------------------------------------ résultats
function ResultsTable({ rows, sel, onSel, q, inDemande }: { rows: Releve[]; sel: number; onSel: (i: number) => void; q: string; inDemande: Set<number> }) {
  const body = useRef<HTMLTableSectionElement>(null);
  useEffect(() => {
    const tr = body.current?.children[sel] as HTMLElement | undefined;
    tr?.scrollIntoView({ block: "nearest" });
  }, [sel]);
  return (
    <div className="table-wrap" data-testid="results">
      <table className="grid">
        <colgroup><col style={{ width: 92 }} /><col /><col style={{ width: 112 }} /><col style={{ width: 104 }} /><col style={{ width: 104 }} /><col style={{ width: 58 }} /></colgroup>
        <thead><tr><th>Matricule</th><th>Nom &amp; prénom</th><th>Période</th><th>Année</th><th>Statut</th><th>Pages</th></tr></thead>
        <tbody ref={body}>
          {rows.map((r, i) => {
            const flags = inDemande.has(r.id) && !r.flags.includes("✓ DEMANDE") ? ["✓ DEMANDE", ...r.flags] : r.flags;
            return (
              <tr key={r.id} className={i === sel ? "sel" : ""} onClick={() => onSel(i)} aria-selected={i === sel}>
                <td className="mono" style={{ fontSize: 12.5 }}>{highlight(r.matricule, q)}</td>
                <td className="cell2">
                  <div className="l1 ellipsis" title={r.nom_complet}>{highlight(r.nom_complet, q)}</div>
                  {flags.length > 0 && <div className="flags">{flags.map((f) => <Badge key={f} text={f} tone={flagTone(f)} />)}</div>}
                </td>
                <td className="cell2"><div className="l1 ellipsis">{r.niveau_num > 0 && r.niveau_num < 100 ? `N${r.niveau_num}` : (r.niveau || "?").replace("Master ", "M")}{r.sem_label ? ` · ${r.sem_label}` : ""}</div>
                  <div className="l2">{FIL_SHORT[r.filiere_code || ""] || r.filiere || "?"}</div></td>
                <td className="cell2"><div className="l1">{r.annee_academique || "?"}</div><div className="l2">{ligne2(r)}</div></td>
                <td><Badge text={r.statut || "—"} tone={statutTone(r.statut)} dot /></td>
                <td className="muted">{r.nb_pages > 1 ? `${r.numero_page}–${r.page_fin}` : r.numero_page}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Seconde ligne de la colonne Année (1.x) : rattrapage, sinon moyenne · grade, sinon session ; rien d'inventé. */
export function ligne2(r: Releve) {
  if ((r.session || "").toLowerCase().startsWith("rattrap")) return "Session de rattrapage";
  if (r.moyenne) return `Moy. ${r.moyenne}${r.grade ? ` · ${r.grade}` : ""}`;
  if (r.session) return `Session ${r.session.toLowerCase()}`;
  return "";
}

function GroupedView({ rows, current, onPick }: { rows: Releve[]; current: Releve | null; onPick: (id: number) => void }) {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const ids = rows.map((r) => r.id).join(",");
  useEffect(() => {
    let alive = true;
    void call<Group[]>("groups", { releve_ids: rows.map((r) => r.id) }).then((g) => alive && setGroups(g)).catch(() => alive && setGroups([]));
    return () => { alive = false; };
  }, [ids]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!groups) return <div className="empty muted"><span className="spinner" /></div>;
  return (
    <div className="table-wrap col" style={{ gap: 10 }}>
      {groups.map((g) => (
        <div key={g.matricule} className="soft col" style={{ gap: 6 }}>
          <div className="row">
            <span className="mono">{g.matricule}</span><span className="h3 grow ellipsis">{g.nom}</span>
            <Badge text={g.missing.length ? `${g.missing.join(", ")} manquant${g.missing.length > 1 ? "s" : ""}` : `Tous présents · ${g.valides}/${g.total} validé(s)`} tone={g.tone} />
          </div>
          {g.found.map((r) => (
            <div key={r.id} className="row small" style={{ cursor: "pointer", padding: "4px 6px", borderRadius: 8, background: current?.id === r.id ? "#EEF4FF" : undefined }}
              onClick={() => onPick(r.id)}>
              <span style={{ width: 170 }} className="ellipsis">{r.periode}</span>
              <span className="muted" style={{ width: 150 }}>{r.annee_academique} · moy. {r.moyenne || "—"}</span>
              <Badge text={r.statut || "—"} tone={statutTone(r.statut)} />
              <span className="grow" />
              <span className="muted tiny">{r.pages_label}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function Empty({ query, total, tolerant, onAll, onTolerant }: { query: string; total: number; tolerant: boolean; onAll: () => void; onTolerant: () => void }) {
  if (!query) return (
    <div className="empty"><h2 className="h2">Qui se présente au guichet ?</h2>
      <div className="muted" style={{ maxWidth: 460 }}>Tapez le matricule (18M042, 18-M-042, 18 m 042…), le nom ou le prénom, ou une année. Les résultats s'affichent pendant la frappe ; ↑ ↓ pour parcourir.</div></div>
  );
  if (total) return (
    <div className="empty"><h2 className="h2">Aucun relevé de cette filière pour « {query} »</h2>
      <div className="muted">{plural(total, "relevé trouvé", "relevés trouvés")} dans d'autres filières.</div>
      <button className="btn secondary" onClick={onAll}>Afficher toutes les filières ({total})</button></div>
  );
  return (
    <div className="empty" data-testid="empty"><h2 className="h2">Aucun relevé pour « {query} »</h2>
      <div className="muted" style={{ maxWidth: 480 }}>Pistes : vérifiez l'orthographe ou tapez seulement le nom de famille ; pour un matricule, essayez sans la lettre de filière (ex. 18 042) ; un relevé récent n'est peut-être pas encore indexé (écran Indexation, Ctrl+3).</div>
      {!tolerant && <button className="btn secondary" onClick={onTolerant}>Essayer avec la tolérance aux fautes</button>}</div>
  );
}

// ------------------------------------------------------------------ récapitulatif du contrôle
function Recap({ rel, detail, compact, onReveal }: { rel: Releve | null; detail: ReleveDetail | null; compact: boolean; onReveal: () => void }) {
  if (!rel) return (
    <div className="card" style={{ flex: "none" }}><div className="eyebrow">Contrôle avant impression</div><h2 className="h2">Aucun relevé sélectionné</h2></div>
  );
  let sem = rel.semestre ? rel.sem_label + (rel.annuel ? " (annuel)" : "") : "?";
  if (rel.credits != null) sem += ` · ${rel.credits}/30 crédits`;
  if ((rel.session || "").toLowerCase().startsWith("rattrap")) sem += " · rattrapage";
  const pages = rel.pages_label + (rel.nb_pages > 1 ? " liées" : "");
  const bits: string[] = [];
  if (rel.moyenne) bits.push(`Moyenne ${rel.moyenne}/20`);
  if (rel.grade) bits.push(`grade ${rel.grade}${rel.mgp ? ` (MGP ${rel.mgp})` : ""}`);
  if (rel.decision_deduite) bits.push(`décision déduite : ${rel.decision}`);
  else if (rel.decision) bits.push(`décision : « ${rel.decision} »`);
  if (rel.date_connue) bits.push(`né(e) le ${rel.date_connue}${rel.lieu_naissance ? ` à ${rel.lieu_naissance}` : ""}`);
  else if (rel.date_naissance) bits.push("date de naissance non renseignée sur le relevé");
  const nom = `${(rel.nom || rel.nom_complet).toUpperCase()} ${rel.prenom || ""}`.trim();
  return (
    <div className="card" style={{ flex: "none", maxHeight: "45%", overflow: "auto" }} data-testid="recap">
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="grow" style={{ minWidth: 0 }}>
          {!compact && <div className="eyebrow">Contrôle avant impression</div>}
          <h2 className="h2 ellipsis selectable" title={rel.nom_complet} style={{ fontSize: compact ? 16 : 19 }}>{nom}</h2>
          <div className="muted small ellipsis selectable" title={bits.join(" · ")} data-testid="recap-details">{bits.join(" · ") || "Moyenne non lue"}</div>
        </div>
        <div className="col" style={{ alignItems: "flex-end", gap: 4 }}>
          <Badge text={rel.statut || "Décision non lue"} tone={statutTone(rel.statut)} big dot />
          <button className="btn link tiny" onClick={onReveal} title={`Ouvrir l'emplacement du fichier original (lecture seule) : ${rel.chemin}`} data-testid="recap-reveal">Fichier original</button>
        </div>
      </div>
      {compact ? (
        <div className="soft selectable" style={{ padding: "8px 12px", fontWeight: 600 }} data-testid="recap-line">
          <span className="mono">{rel.matricule}</span> · {rel.annee_academique || "?"} · {rel.niveau || "?"}{sem !== "?" ? ` · ${sem}` : ""} · {pages}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {[["Matricule", rel.matricule, true], ["Année · niveau", `${rel.annee_academique || "?"} · ${rel.niveau || "?"}`, false], ["Semestre", sem, false], ["À imprimer", pages, false]].map(([k, v, mono]) => (
            <div key={String(k)} className="soft" style={{ padding: "8px 12px" }}><div className="eyebrow">{k}</div><div className={mono ? "mono selectable" : "h3 selectable"}>{v}</div></div>
          ))}
        </div>
      )}
      {(detail?.alerts || []).map((a, i) => <Alert key={i} text={a.text} tone={a.tone} />)}
      {!compact && <div className="muted tiny ellipsis selectable" title={rel.chemin} data-testid="recap-source">Source : {rel.chemin} · {rel.type_fichier}</div>}
    </div>
  );
}
