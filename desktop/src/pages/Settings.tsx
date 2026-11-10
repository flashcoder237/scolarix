import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { call, sx, type Settings, type TextRule } from "../api";
import { useApp, type SettingsTab } from "../state";
import { Badge, Icon, Modal, Switch, useDialogs, useToast } from "../components/ui";
import { RulesEditor } from "./RulesEditor";

// Réglages (2.1) : feuille large (1 280 px, plein écran à 1366) à navigation gauche par sujet, recherche Ctrl+K
// dans les libellés, barre d'enregistrement qui nomme les modifications en attente (Annuler Échap / Enregistrer
// Ctrl+S). Mêmes réglages qu'avant, redistribués ; « Outils du poste » dit ce qui est détecté et ce qu'il manque.

type SectionId = "operateur" | "impression" | "recherche" | "affichage" | "indexation" | "exports" | "remplacements" | "outils" | "diagnostic";
const SECTIONS: { id: SectionId; label: string; icon: string; group: string; title: string; desc: string }[] = [
  { id: "operateur", label: "Opérateur et poste", icon: "user", group: "Poste", title: "Opérateur et poste", desc: "Nom inscrit au journal des actions et sur les bordereaux, et nom du guichet." },
  { id: "impression", label: "Impression", icon: "printer", group: "Poste", title: "Impression", desc: "Imprimante du guichet, impression silencieuse, recto-verso et numéro de délivrance." },
  { id: "recherche", label: "Recherche", icon: "search", group: "Poste", title: "Recherche", desc: "Tolérance aux fautes de frappe et nombre de résultats." },
  { id: "affichage", label: "Affichage", icon: "monitor", group: "Poste", title: "Affichage", desc: "Taille de l'interface, sons et animations." },
  { id: "indexation", label: "Indexation", icon: "database", group: "Archives", title: "Indexation", desc: "Dossier des archives (lu sans jamais être modifié), exclusions, OCR et passerelle Word." },
  { id: "exports", label: "Exports", icon: "folder", group: "Archives", title: "Exports", desc: "Où sont classés relevés, duplicatas, cursus, demandes, extractions et vérifications." },
  { id: "remplacements", label: "Remplacements (Word)", icon: "word", group: "Archives", title: "Remplacements dans les relevés Word", desc: "Appliqués à une COPIE au moment de l'aperçu : le fichier d'origine n'est jamais modifié ; PDF et scans non concernés. Ordre de la liste = ordre d'application." },
  { id: "outils", label: "Outils du poste", icon: "sliders", group: "Système", title: "Outils du poste", desc: "Détectés automatiquement. Ne renseignez un chemin que si la détection échoue." },
  { id: "diagnostic", label: "Diagnostic", icon: "info", group: "Système", title: "Diagnostic", desc: "Versions, dossiers et fichiers du poste : à transmettre à la Cellule informatique en cas de problème." },
];
const TAB_TO_SECTION: Record<SettingsTab, SectionId> = { poste: "operateur", indexation: "indexation", exports: "exports", corrections: "remplacements" };

// libellés des réglages : recherche (Ctrl+K) et barre des modifications
const LABELS: Partial<Record<keyof Settings, [string, SectionId]>> = {
  operateur: ["opérateur", "operateur"], poste: ["poste", "operateur"],
  printer_name: ["imprimante", "impression"], print_command: ["commande d'impression", "impression"], duplex: ["recto-verso", "impression"],
  delivery_ref_stamp: ["n° de délivrance en pied de page", "impression"],
  fuzzy_threshold: ["tolérance aux fautes", "recherche"], max_results: ["résultats au plus", "recherche"],
  ui_scale: ["taille de l'interface", "affichage"], ui_sounds: ["sons", "affichage"], ui_animations: ["animations", "affichage"],
  source_root: ["dossier des archives", "indexation"], exclusions: ["exclusions", "indexation"], index_workers: ["processus d'indexation", "indexation"],
  ocr_enabled: ["OCR des scans", "indexation"], ocr_preprocess: ["prétraitement OCR", "indexation"], word_com_enabled: ["passerelle COM Word", "indexation"],
  differential: ["indexation différentielle", "indexation"], docx_convert_at_index: ["aperçus Word à l'indexation", "indexation"],
  prefetch_student_previews: ["préparation anticipée", "indexation"],
  exports_root: ["dossier des exports", "exports"], exports_retention_days: ["conservation des exports", "exports"],
  text_rules_enabled: ["application des remplacements", "remplacements"],
  tesseract_cmd: ["chemin de Tesseract", "outils"], soffice_cmd: ["chemin de LibreOffice", "outils"], db_path: ["base d'index", "outils"], cache_dir: ["cache d'aperçu", "outils"],
};
const SEARCH: { label: string; section: SectionId; target?: string; words?: string }[] = [
  { label: "Opérateur", section: "operateur", target: "operateur", words: "nom agent journal" },
  { label: "Poste (nom du guichet)", section: "operateur", target: "poste", words: "guichet" },
  { label: "Imprimante", section: "impression", target: "printer_name" },
  { label: "Commande d'impression (SumatraPDF…)", section: "impression", target: "print_command", words: "silencieuse sumatra" },
  { label: "Recto-verso", section: "impression", target: "duplex", words: "duplex" },
  { label: "Numéro de délivrance en pied de page", section: "impression", target: "delivery_ref_stamp", words: "référence" },
  { label: "Seuil de tolérance aux fautes", section: "recherche", target: "fuzzy_threshold", words: "fautes frappe floue" },
  { label: "Résultats au plus", section: "recherche", target: "max_results" },
  { label: "Taille de l'interface (zoom)", section: "affichage", target: "ui_scale", words: "zoom police grande" },
  { label: "Sons discrets", section: "affichage", target: "ui_sounds" },
  { label: "Petites animations", section: "affichage", target: "ui_animations" },
  { label: "Dossier racine des archives", section: "indexation", target: "source_root", words: "source partage" },
  { label: "Dossiers et motifs exclus", section: "indexation", target: "exclusions", words: "exclusion ignorer" },
  { label: "Processus d'indexation", section: "indexation", target: "index_workers", words: "coeurs parallèle" },
  { label: "OCR des scans (Tesseract)", section: "indexation", target: "ocr_enabled", words: "scan" },
  { label: "Deskew + contraste avant l'OCR", section: "indexation", target: "ocr_preprocess" },
  { label: "Passerelle COM Word (.doc 97-2003)", section: "indexation", target: "word_com_enabled", words: "word doc" },
  { label: "Indexation différentielle", section: "indexation", target: "differential" },
  { label: "Produire les aperçus Word pendant l'indexation", section: "indexation", target: "docx_convert_at_index" },
  { label: "Préparer d'avance les relevés Word de l'étudiant", section: "indexation", target: "prefetch_student_previews" },
  { label: "Dossier des exports", section: "exports", target: "exports_root" },
  { label: "Conservation des copies recopiables", section: "exports", target: "exports_retention_days", words: "purge jours" },
  { label: "Appliquer les remplacements aux relevés Word", section: "remplacements", target: "text_rules_enabled", words: "corrections" },
  { label: "Règles de remplacement (Ref N°, téléphone…)", section: "remplacements", words: "corrections en-tête" },
  { label: "Essai des règles / aperçu avant-après", section: "remplacements", words: "essai test" },
  { label: "Tesseract (OCR)", section: "outils", target: "tesseract_cmd", words: "ocr" },
  { label: "LibreOffice (soffice)", section: "outils", target: "soffice_cmd" },
  { label: "Microsoft Word (passerelle COM)", section: "outils", target: "word" },
  { label: "Base d'index", section: "outils", target: "db_path", words: "sqlite releves.db" },
  { label: "Cache d'aperçu (vider)", section: "outils", target: "cache_dir" },
  { label: "Version de Scolarix", section: "diagnostic", words: "version" },
  { label: "Journal des incidents (logs)", section: "diagnostic", words: "logs journaux erreurs" },
  { label: "Fichier de configuration", section: "diagnostic", words: "config" },
];
const RULE_FIELDS: Record<string, string> = { description: "description", chercher: "chercher", remplacer: "remplacer", regex: "regex", sauf_si: "sauf si",
  police: "police", taille: "taille", gras: "gras", italique: "italique", style_sur: "portée", interligne: "interligne", espace_avant: "espace avant",
  espace_apres: "espace après", actif: "activation" };
const fold = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Ce qui sera enregistré, en clair : « imprimante », « règle 2 (taille) », « ordre des règles »… */
export function describeChanges(o: Settings, v: Settings): { label: string; section: SectionId }[] {
  const out: { label: string; section: SectionId }[] = [];
  for (const k of Object.keys(LABELS) as (keyof Settings)[]) {
    if (!same(o[k], v[k])) out.push({ label: LABELS[k]![0], section: LABELS[k]![1] });
  }
  const a = o.text_rules || [], b = v.text_rules || [];
  if (!same(a, b)) {
    const norm = (r: TextRule) => JSON.stringify(Object.keys(r).sort().map((k) => [k, (r as unknown as Record<string, unknown>)[k] ?? null]));
    if (a.length === b.length && same(a.map(norm).sort(), b.map(norm).sort())) out.push({ label: "ordre des règles", section: "remplacements" });
    else {
      if (b.length > a.length) out.push({ label: `${b.length - a.length} règle(s) ajoutée(s)`, section: "remplacements" });
      if (b.length < a.length) out.push({ label: `${a.length - b.length} règle(s) supprimée(s)`, section: "remplacements" });
      for (let i = 0; i < Math.min(a.length, b.length); i++) {
        const f = Object.keys(RULE_FIELDS).filter((k) => !same((a[i] as unknown as Record<string, unknown>)[k], (b[i] as unknown as Record<string, unknown>)[k]));
        if (f.length) out.push({ label: `règle ${i + 1} (${f.map((k) => RULE_FIELDS[k]).join(", ")})`, section: "remplacements" });
      }
    }
  }
  return out;
}

export interface Tools {
  tesseract: { found: boolean; path: string | null; origin: string; version?: string | null; langs?: string[]; fra?: boolean; detail?: string };
  libreoffice: { found: boolean; path: string | null; origin: string; version?: string | null; detail?: string };
  word: { found: boolean; enabled: boolean; platform: boolean; path?: string | null; version?: string; probe?: string; detail?: string };
  ocr_enabled: boolean;
  counts: { scans_en_attente: number; scans: number; doc: number; docx: number; anomalies: number };
}
interface Diag {
  app: string; version: string; python: string; systeme: string; machine: string; fige: boolean; pid: number; data_dir: string; logs_dir: string; config_path: string;
  db_path: string; db_bytes: number; db_reseau: boolean; cache_dir: string; cache_bytes: number; cache_fichiers: number; cache_apercus: number; exports_dir: string;
  releves: number; fichiers: number; derniere_indexation: string | null; convertisseur: string; regles_actives: boolean;
}
const bytes = (n: number) => (n >= 1e9 ? `${(n / 1e9).toFixed(2).replace(".", ",")} Go` : n >= 1e6 ? `${(n / 1e6).toFixed(1).replace(".", ",")} Mo` : `${Math.round(n / 1e3)} ko`);
const nf = (n: number) => (n || 0).toLocaleString("fr-FR");

// Champ d'un réglage : marqué s'il est modifié, mis en évidence quand la recherche y mène (data-set = clé)
const FieldCtx = createContext<{ dirty: Set<string>; flash: string | null }>({ dirty: new Set(), flash: null });
// ``block`` : contenu composé (liste, boutons) — jamais dans un <label>, dont le clic activerait le 1er bouton
function F({ k, label, children, hint, grow, block }: { k: string; label?: ReactNode; children: ReactNode; hint?: ReactNode; grow?: boolean; block?: boolean }) {
  const { dirty, flash } = useContext(FieldCtx);
  return (
    <div className={`rx-field${dirty.has(k) ? " dirty" : ""}${flash === k ? " flash" : ""}${grow ? " grow" : ""}`} data-set={k}>
      {label === undefined ? children : block ? <div className="lbl rx-lbl">{label}{children}</div> : <label className="lbl">{label}{children}</label>}
      {hint && <div className="muted small">{hint}</div>}
    </div>
  );
}

export function SettingsDialog() {
  const app = useApp();
  const toast = useToast();
  const { ask } = useDialogs();
  const [section, setSection] = useState<SectionId>(TAB_TO_SECTION[app.settingsTab] || "operateur");
  const [orig, setOrig] = useState<Settings | null>(app.settings);
  const [v, setV] = useState<Settings | null>(app.settings);
  const [printers, setPrinters] = useState<{ name: string; isDefault: boolean }[]>([]);
  const [appInfo, setAppInfo] = useState<{ version: string; logs: string; platform: string } | null>(null);
  const [tools, setTools] = useState<Tools | null>(null);
  const [q, setQ] = useState("");
  const [hit, setHit] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const body = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void sx.printers().then(setPrinters).catch(() => undefined);
    void sx.appInfo().then(setAppInfo).catch(() => undefined);
    void call<Settings>("settings_get").then((s) => { setV(s); setOrig(s); }).catch(() => undefined);
    void call<Tools>("tools_status").then(setTools).catch(() => undefined);
  }, []);
  const changes = useMemo(() => (orig && v ? describeChanges(orig, v) : []), [orig, v]);
  const dirtyKeys = useMemo(() => new Set(orig && v ? (Object.keys(LABELS) as (keyof Settings)[]).filter((k) => !same(orig[k], v[k])) : []), [orig, v]);

  const results = useMemo(() => {
    const w = fold(q.trim()).split(/\s+/).filter(Boolean);
    if (!w.length) return [];
    const rules = (v?.text_rules || []).map((r, i) => ({ label: `Règle ${i + 1} : ${r.description || r.chercher}`, section: "remplacements" as SectionId, target: `rule:${i}`, words: `${r.chercher} ${r.remplacer}` }));
    return [...SEARCH, ...rules].filter((s) => { const h = fold(`${s.label} ${s.words || ""} ${SECTIONS.find((x) => x.id === s.section)?.label}`); return w.every((x) => h.includes(x)); }).slice(0, 12);
  }, [q, v?.text_rules]);
  useEffect(() => setHit(0), [q]);

  const goTo = (s: SectionId, target?: string) => {
    setSection(s);
    setQ("");
    if (target) {
      setFlash(target);
      window.setTimeout(() => {
        const el = body.current?.querySelector<HTMLElement>(`[data-set="${target}"]`);
        el?.scrollIntoView({ block: "center" });
        el?.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
      }, 60);
      window.setTimeout(() => setFlash(null), 1800);
    }
  };

  const save = async () => {
    if (!v) return;
    try {
      const before = app.settings;
      const s = await app.saveSettings(v);
      if (s.ui_scale) await sx.setZoom(s.ui_scale / 100);
      const moved = !!before && (s.db_path !== before.db_path || s.cache_dir !== before.cache_dir);
      if (moved) toast("Réglages enregistrés. La nouvelle base d'index ou le nouveau cache d'aperçu s'appliquent au redémarrage de Scolarix.",
        { tone: "blue", ms: 30000, action: { label: "Redémarrer maintenant", run: () => void sx.relaunch() } });
      else toast("Réglages enregistrés.");
      app.closeSettings();
    } catch (e) {
      const msg = (e as Error).message || String(e);
      if (/Corrections Word/.test(msg)) setSection("remplacements");      // règle invalide : on montre où elle est
      if (/base d'index/.test(msg)) setSection("outils");
      toast(`Enregistrement impossible : ${msg}`, { tone: "red", ms: 10000 });
    }
  };
  // Échap par couches : recherche > confirmation des modifications > fermeture
  const close = async () => {
    if (q) { setQ(""); return; }
    if (changes.length) {
      const k = await ask({ title: "Modifications non enregistrées", text: `${changes.length} modification${changes.length > 1 ? "s" : ""} : ${changes.map((c) => c.label).join(", ")}.`,
        details: "« Abandonner » ferme les Réglages sans rien enregistrer.", choices: ["Abandonner les modifications"], danger: true });
      if (k !== 0) return;
    }
    app.closeSettings();
  };
  const closeRef = useRef(close);
  closeRef.current = close;
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (document.querySelectorAll(".overlay").length > 1) return;       // une question est posée par-dessus
      const key = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && key === "s") { e.preventDefault(); void saveRef.current(); }
      else if ((e.ctrlKey || e.metaKey) && key === "k") { e.preventDefault(); searchRef.current?.focus(); searchRef.current?.select(); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  if (!v) return null;
  const set = <K extends keyof Settings>(k: K, val: Settings[K]) => setV({ ...v, [k]: val });
  const sec = SECTIONS.find((s) => s.id === section)!;
  const toolsAlert = !!tools && ((v.ocr_enabled && !tools.tesseract.found) || (!tools.libreoffice.found && !tools.word.found));
  const navKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const i = SECTIONS.findIndex((s) => s.id === section);
    const n = SECTIONS[(i + (e.key === "ArrowDown" ? 1 : SECTIONS.length - 1)) % SECTIONS.length];
    setSection(n.id);
    (e.currentTarget.querySelector(`[data-sec="${n.id}"]`) as HTMLElement | null)?.focus();
  };

  return (
    <Modal label="Réglages" onClose={() => void closeRef.current()} width={1280}>
      <aside className="rx-nav">
        <div className="rx-brand">Réglages</div>
        <label className="rx-search">
          <Icon name="search" size={15} />
          <input ref={searchRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Chercher un réglage…" aria-label="Chercher un réglage" data-testid="settings-search"
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setHit(Math.min(results.length - 1, hit + 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setHit(Math.max(0, hit - 1)); }
              else if (e.key === "Enter" && results[hit]) { e.preventDefault(); goTo(results[hit].section, results[hit].target); }
            }} />
          {!q && <span className="key">Ctrl+K</span>}
        </label>
        {q ? (
          <div className="rx-results" role="listbox" aria-label="Réglages trouvés" data-testid="settings-results">
            {results.map((r, i) => (
              <button key={`${r.label}-${i}`} role="option" aria-selected={i === hit} className={`rx-res${i === hit ? " on" : ""}`} onMouseEnter={() => setHit(i)} onClick={() => goTo(r.section, r.target)}>
                <span>{r.label}</span><span className="tiny muted">{SECTIONS.find((s) => s.id === r.section)?.label}</span>
              </button>
            ))}
            {!results.length && <div className="muted small" style={{ padding: 10 }}>Aucun réglage ne correspond.</div>}
          </div>
        ) : (
          <nav className="rx-sections" onKeyDown={navKey} aria-label="Sections des réglages">
            {["Poste", "Archives", "Système"].map((g) => (
              <div key={g} className="rx-group">
                <div className="rx-gh">{g}</div>
                {SECTIONS.filter((s) => s.group === g).map((s) => {
                  const n = changes.filter((c) => c.section === s.id).length;
                  return (
                    <button key={s.id} data-sec={s.id} className={`rx-sec${section === s.id ? " on" : ""}`} onClick={() => setSection(s.id)} aria-current={section === s.id ? "page" : undefined} data-testid={`sec-${s.id}`}>
                      <Icon name={s.icon} size={17} /><span className="grow">{s.label}</span>
                      {s.id === "remplacements" && <span className="tiny muted rx-badge">{(v.text_rules || []).length} règle{(v.text_rules || []).length > 1 ? "s" : ""}</span>}
                      {s.id === "outils" && toolsAlert && <span className="rx-dot" title="Un outil est introuvable" />}
                      {n > 0 && <span className="rx-mod" title={`${n} modification(s) non enregistrée(s)`} />}
                    </button>
                  );
                })}
              </div>
            ))}
          </nav>
        )}
        <div className="rx-navfoot mono">Scolarix {appInfo?.version || app.info?.version || ""} · poste {v.poste || "?"}</div>
      </aside>

      <section className="rx-main" data-section={section}>
        <header className="rx-head">
          <div className="grow">
            <div className="eyebrow">{sec.group}</div>
            <h2 className="h2">{sec.title}</h2>
            <div className="muted small">{sec.desc}</div>
          </div>
          {section === "remplacements" && <Switch blue checked={v.text_rules_enabled} onChange={(x) => set("text_rules_enabled", x)} label="Appliquer aux relevés Word" title="Aperçu, impression, export et cursus" />}
          {section === "outils" && <button className="btn ghost sm" onClick={() => void call<Tools>("tools_status", { tesseract_cmd: v.tesseract_cmd, soffice_cmd: v.soffice_cmd }).then(setTools)} data-testid="tools-redetect"><Icon name="refresh" size={14} />Tout redétecter</button>}
          <button className="btn ghost icon" onClick={() => void close()} aria-label="Fermer" title="Fermer (Échap)"><Icon name="x" /></button>
        </header>

        <FieldCtx.Provider value={{ dirty: dirtyKeys as Set<string>, flash }}>
        <div ref={body} className={`rx-body${section === "remplacements" ? " fill" : ""}`}>
          {section === "operateur" && <div className="rx-form">
            <div className="rx-row">
              <F k="operateur" label="Opérateur" grow hint="Inscrit au journal des actions (traçabilité) et sur les bordereaux."><input className="field" value={v.operateur} onChange={(e) => set("operateur", e.target.value)} data-testid="set-operateur" /></F>
              <F k="poste" label="Poste"><input className="field" style={{ width: 160 }} value={v.poste} onChange={(e) => set("poste", e.target.value)} /></F>
            </div>
          </div>}

          {section === "impression" && <div className="rx-form">
            <F k="printer_name" label="Imprimante">
              <select className="field" value={v.printer_name || ""} onChange={(e) => set("printer_name", e.target.value)}>
                <option value="">Imprimante par défaut de Windows</option>
                {printers.map((p) => <option key={p.name} value={p.name}>{p.name}{p.isDefault ? " (par défaut)" : ""}</option>)}
              </select></F>
            <F k="print_command" label={<>Commande d'impression (facultatif, ex. SumatraPDF ; {"{file}"} et {"{printer}"} sont remplacés)</>}>
              <input className="field mono" value={v.print_command || ""} onChange={(e) => set("print_command", e.target.value)} placeholder='"C:\Outils\SumatraPDF.exe" -print-to-default -silent "{file}"' /></F>
            <F k="duplex"><Switch blue checked={v.duplex} onChange={(x) => set("duplex", x)} label="Recto-verso pour les relevés de plusieurs pages" /></F>
            <F k="delivery_ref_stamp"><Switch blue checked={v.delivery_ref_stamp} onChange={(x) => set("delivery_ref_stamp", x)} label="Numéro de délivrance en pied de page" /></F>
          </div>}

          {section === "recherche" && <div className="rx-form">
            <div className="rx-row">
              <F k="fuzzy_threshold" label="Seuil de tolérance aux fautes (%)" hint="Plus bas : plus de noms proches proposés (« tchoa alin » → TCHOUA Alain)."><input className="field" style={{ width: 140 }} type="number" min={50} max={100} value={v.fuzzy_threshold} onChange={(e) => set("fuzzy_threshold", Number(e.target.value))} /></F>
              <F k="max_results" label="Résultats au plus"><input className="field" style={{ width: 140 }} type="number" min={20} max={2000} value={v.max_results} onChange={(e) => set("max_results", Number(e.target.value))} /></F>
            </div>
          </div>}

          {section === "affichage" && <div className="rx-form">
            <F k="ui_scale" label="Taille de l'interface (Ctrl+Plus / Ctrl+Moins / Ctrl+0)">
              <select className="field" style={{ width: 220 }} value={v.ui_scale || 100} onChange={(e) => set("ui_scale", Number(e.target.value))}>
                {(app.info?.ui_scales || [80, 90, 100, 110, 125, 150]).map((x) => <option key={x} value={x}>{x} %{x === 100 ? " (normale)" : ""}</option>)}
              </select></F>
            <F k="ui_sounds"><Switch blue checked={v.ui_sounds} onChange={(x) => set("ui_sounds", x)} label="Sons discrets" /></F>
            <F k="ui_animations"><Switch blue checked={v.ui_animations} onChange={(x) => set("ui_animations", x)} label="Petites animations" /></F>
          </div>}

          {section === "indexation" && <div className="rx-form">
            <F k="source_root" label="Dossier racine des archives" block><div className="row">
              <input className="field grow" value={v.source_root} onChange={(e) => set("source_root", e.target.value)} />
              <button className="btn ghost" onClick={async () => { const d = await sx.pickFolder({ defaultPath: v.source_root || undefined }); if (d) set("source_root", d); }}>Parcourir…</button></div></F>
            <F k="exclusions" label="Dossiers et motifs exclus" block><Exclusions items={v.exclusions || []} root={v.source_root} onChange={(x) => set("exclusions", x)} /></F>
            <F k="index_workers" label={`Processus d'indexation (0 = automatique : ${app.info?.default_workers ?? "?"})`}
              hint={`${app.info?.cpu_count || "?"} cœurs détectés. Chaque processus lit ses fichiers et, pour les .doc, pilote sa propre instance de Word : au-delà de 4, le gain est faible et la mémoire augmente. Sur un partage réseau lent, 2 à 3 processus suffisent souvent.`}>
              <input className="field" style={{ width: 120 }} type="number" min={0} max={16} value={v.index_workers} onChange={(e) => set("index_workers", Number(e.target.value))} /></F>
            <div className="rx-grid2">
              <F k="ocr_enabled"><Switch blue checked={v.ocr_enabled} onChange={(x) => set("ocr_enabled", x)} label="OCR des scans (Tesseract)" /></F>
              <F k="ocr_preprocess"><Switch blue checked={v.ocr_preprocess} onChange={(x) => set("ocr_preprocess", x)} label="Deskew + contraste Otsu avant l'OCR" /></F>
              <F k="word_com_enabled"><Switch blue checked={v.word_com_enabled} onChange={(x) => set("word_com_enabled", x)} label="Passerelle COM Word (.doc 97-2003)" /></F>
              <F k="differential"><Switch blue checked={v.differential} onChange={(x) => set("differential", x)} label="Indexation différentielle" /></F>
              <F k="docx_convert_at_index"><Switch blue checked={v.docx_convert_at_index} onChange={(x) => set("docx_convert_at_index", x)} label="Produire les aperçus Word pendant l'indexation" /></F>
              <F k="prefetch_student_previews"><Switch blue checked={v.prefetch_student_previews !== false} onChange={(x) => set("prefetch_student_previews", x)} label="Préparer d'avance les autres relevés Word de l'étudiant affiché" /></F>
            </div>
          </div>}

          {section === "exports" && <div className="rx-form">
            <F k="exports_root" label="Dossier des exports (Relevés, Duplicatas, Cursus, Demandes, Extractions, Vérifications)" block><div className="row">
              <input className="field grow" value={v.exports_root} onChange={(e) => set("exports_root", e.target.value)} placeholder={app.info?.exports_dir || "Documents\\Exports Scolarix"} />
              <button className="btn ghost" onClick={async () => { const d = await sx.pickFolder({ defaultPath: v.exports_root || undefined }); if (d) set("exports_root", d); }}>Parcourir…</button></div></F>
            <F k="exports_retention_days" label="Conservation des copies recopiables (jours, 0 = toujours)"><input className="field" style={{ width: 140 }} type="number" min={0} value={v.exports_retention_days} onChange={(e) => set("exports_retention_days", Number(e.target.value))} /></F>
            <div className="muted small">Le registre des délivrances et les extractions ne sont jamais purgés. Classement par nature de demande puis par mois (Releves\2026\11\…) ; un fichier n'est jamais écrasé (date et heure dans le nom). Un dossier partagé du service réunit les exports de tous les guichets : réservez-en l'accès au personnel habilité.</div>
            <div className="row"><button className="btn secondary" onClick={async () => {
              try {
                const dir = v.exports_root && v.exports_root !== app.settings?.exports_root ? v.exports_root : await call<string>("exports_dir");
                const err = await sx.openPath(dir);
                if (err) toast(`Dossier inaccessible : ${err}`, { tone: "red" });
              } catch (e) { toast(`Dossier inaccessible : ${(e as Error).message}`, { tone: "red" }); }
            }} data-testid="open-exports"><Icon name="folder" />Ouvrir le dossier des exports</button></div>
          </div>}

          {section === "remplacements" && <RulesEditor rules={v.text_rules || []} onRules={(r) => set("text_rules", r)} flash={flash} />}

          {section === "outils" && <ToolsSection v={v} set={set} tools={tools} setTools={setTools} flash={flash} dirty={dirtyKeys} />}

          {section === "diagnostic" && <DiagnosticSection appInfo={appInfo} />}
        </div>
        </FieldCtx.Provider>

        <footer className="rx-bar" data-testid="settings-bar">
          {changes.length
            ? <span className="rx-pending grow" data-testid="settings-pending" title={changes.map((c) => c.label).join(", ")}><i />{changes.length} modification{changes.length > 1 ? "s" : ""} non enregistrée{changes.length > 1 ? "s" : ""} : {changes.map((c) => c.label).join(", ")}</span>
            : <span className="grow muted small">Aucune modification.</span>}
          <button className="btn ghost" onClick={() => void close()} data-testid="settings-cancel">Annuler <span className="kbd">Échap</span></button>
          <button className="btn primary" onClick={() => void save()} data-testid="settings-save">Enregistrer <span className="kbd">Ctrl+S</span></button>
        </footer>
      </section>
    </Modal>
  );
}

// ------------------------------------------------------------------ outils du poste
function ToolsSection({ v, set, tools, setTools, flash, dirty }: {
  v: Settings; set: <K extends keyof Settings>(k: K, val: Settings[K]) => void; tools: Tools | null; setTools: (t: Tools) => void; flash: string | null; dirty: Set<keyof Settings>;
}) {
  const app = useApp();
  const toast = useToast();
  const { ask } = useDialogs();
  const [diag, setDiag] = useState<Diag | null>(null);
  const [editT, setEditT] = useState(!!v.tesseract_cmd);
  const [editL, setEditL] = useState(!!v.soffice_cmd);
  const [probing, setProbing] = useState(false);
  useEffect(() => { void call<Diag>("diagnostic").then(setDiag).catch(() => undefined); }, []);
  const redetect = (extra: Record<string, unknown> = {}) => call<Tools>("tools_status", { tesseract_cmd: v.tesseract_cmd, soffice_cmd: v.soffice_cmd, ...extra }).then(setTools);
  if (!tools) return <div className="muted"><span className="spinner" /> Détection des outils…</div>;
  const t = tools.tesseract, l = tools.libreoffice, w = tools.word, c = tools.counts;
  const cls = (k: string, bad: boolean) => `rx-tool${bad ? " bad" : ""}${flash === k ? " flash" : ""}${dirty.has(k as keyof Settings) ? " dirty" : ""}`;
  const tBad = !t.found || t.fra === false;
  const dbPath = v.db_path || app.info?.db_path || diag?.db_path || "";
  const cachePath = v.cache_dir || app.info?.cache_dir || diag?.cache_dir || "";
  const dbChanged = !!app.info && !!v.db_path && v.db_path !== app.info.db_path;
  const cacheChanged = !!app.info && !!v.cache_dir && v.cache_dir !== app.info.cache_dir;
  return (
    <div className="rx-tools" data-testid="tools">
      <div className={cls("tesseract_cmd", tBad && tools.ocr_enabled)} data-set="tesseract_cmd" data-testid="tool-tesseract">
        <div className="rx-ticon t-red"><Icon name="scan" size={20} /></div>
        <div className="grow col" style={{ gap: 4 }}>
          <div className="row"><b>Tesseract (OCR)</b>
            {t.found ? <Badge text={t.fra === false ? "LANGUE « fra » ABSENTE" : `DÉTECTÉ${t.version ? ` · ${t.version}` : ""}`} tone={t.fra === false ? "amber" : "green"} /> : <Badge text="INTROUVABLE" tone="red" />}
            {!tools.ocr_enabled && <Badge text="OCR DÉSACTIVÉ" tone="gray" />}</div>
          <div className="mono small selectable ellipsis">{t.found ? t.path : t.detail}</div>
          {!t.found || t.fra === false
            ? <div className="small rx-cons">{c.scans_en_attente ? <><b>{nf(c.scans_en_attente)} PDF scanné{c.scans_en_attente > 1 ? "s" : ""}</b> attend{c.scans_en_attente > 1 ? "ent" : ""} l'OCR (anomalies d'indexation). </> : c.scans ? <>{nf(c.scans)} PDF scanné(s) dans l'index ne seront pas relus. </> : null}Installez Tesseract 5 + langue « fra », ou indiquez son chemin :</div>
            : <div className="small muted">{t.origin === "réglage" ? "Chemin indiqué dans les Réglages" : "Détecté automatiquement"} · OCR des PDF scannés à l'indexation{c.scans ? ` (${nf(c.scans)} scan(s) dans l'index)` : ""}</div>}
          {(editT || !t.found) && <div className="row">
            <input className="field grow mono" value={v.tesseract_cmd || ""} placeholder="C:\Program Files\Tesseract-OCR\tesseract.exe" onChange={(e) => set("tesseract_cmd", e.target.value)} data-testid="set-tesseract" />
            <button className="btn ghost sm" onClick={async () => { const p = await sx.pickFile({ title: "Programme Tesseract", defaultPath: v.tesseract_cmd || undefined }); if (p) { set("tesseract_cmd", p); void redetect({ tesseract_cmd: p }); } }}>Parcourir…</button>
            <button className="btn secondary sm" onClick={() => void redetect()} data-testid="tesseract-detect">Détecter</button></div>}
        </div>
        {t.found && !editT && <button className="btn ghost sm" onClick={() => setEditT(true)}>Changer…</button>}
      </div>

      <div className={cls("soffice_cmd", !l.found && !w.found)} data-set="soffice_cmd" data-testid="tool-libreoffice">
        <div className="rx-ticon t-green"><Icon name="doc" size={20} /></div>
        <div className="grow col" style={{ gap: 4 }}>
          <div className="row"><b>LibreOffice</b>{l.found ? <Badge text={`DÉTECTÉ${l.version ? ` · ${l.version}` : ""}`} tone="green" /> : <Badge text="INTROUVABLE" tone={w.found ? "gray" : "red"} />}</div>
          <div className="mono small selectable ellipsis">{l.found ? l.path : l.detail}</div>
          <div className="small muted">{l.found ? `${w.found && w.enabled ? "Secours de Word" : "Convertisseur"} · conversion .doc → .docx et aperçus Word` : w.found ? "Facultatif : Word fait les conversions." : <span className="rx-cons">{nf(c.doc + c.docx)} fichier(s) Word ne peuvent être ni affichés ni imprimés sans Word ni LibreOffice.</span>}</div>
          {(editL || !l.found) && <div className="row">
            <input className="field grow mono" value={v.soffice_cmd || ""} placeholder="C:\Program Files\LibreOffice\program\soffice.exe" onChange={(e) => set("soffice_cmd", e.target.value)} data-testid="set-soffice" />
            <button className="btn ghost sm" onClick={async () => { const p = await sx.pickFile({ title: "Programme LibreOffice (soffice)", defaultPath: v.soffice_cmd || undefined }); if (p) { set("soffice_cmd", p); void redetect({ soffice_cmd: p }); } }}>Parcourir…</button>
            <button className="btn secondary sm" onClick={() => void redetect()}>Détecter</button></div>}
        </div>
        {l.found && !editL && <button className="btn ghost sm" onClick={() => setEditL(true)}>Changer…</button>}
      </div>

      <div className={cls("word", false)} data-set="word" data-testid="tool-word">
        <div className="rx-ticon t-blue"><Icon name="word" size={20} /></div>
        <div className="grow col" style={{ gap: 4 }}>
          <div className="row"><b>Microsoft Word (passerelle COM)</b>
            {w.found ? <Badge text={`DÉTECTÉ${w.version ? ` · ${w.version}` : ""}`} tone="green" /> : <Badge text={w.platform ? "INTROUVABLE" : "WINDOWS SEULEMENT"} tone="gray" />}
            {w.found && !w.enabled && <Badge text="PASSERELLE DÉSACTIVÉE" tone="amber" />}</div>
          <div className="mono small selectable ellipsis">{w.path || w.detail || "WINWORD.EXE"}</div>
          <div className="small muted">Utilisé en priorité pour les .doc 97-2003 si la passerelle est activée (Indexation){c.doc ? ` · ${nf(c.doc)} fichier(s) .doc dans l'index` : ""}{w.probe ? ` · test : ${w.probe}` : ""}</div>
        </div>
        {w.platform && <button className="btn ghost sm" disabled={probing} onClick={async () => { setProbing(true); try { await redetect({ probe_word: true }); } finally { setProbing(false); } }}>{probing ? <span className="spinner" /> : null}Tester</button>}
      </div>

      <div className="rx-gh" style={{ marginTop: 6 }}>Fichiers du poste · changement au redémarrage</div>
      <div className={cls("db_path", !!diag?.db_reseau)} data-set="db_path" data-testid="tool-db">
        <div className="rx-ticon t-violet"><Icon name="database" size={20} /></div>
        <div className="grow col" style={{ gap: 4 }}>
          <div className="row"><b>Base d'index</b><Badge text={diag?.db_reseau ? "PARTAGE RÉSEAU" : "DISQUE LOCAL"} tone={diag?.db_reseau ? "red" : "gray"} />{dbChanged && <Badge text="AU REDÉMARRAGE" tone="blue" />}</div>
          <div className="mono small selectable ellipsis" data-testid="set-db">{dbPath}</div>
          <div className="small muted">{diag ? `${bytes(diag.db_bytes)} · ${nf(diag.releves)} relevés · ` : ""}à garder sur le disque local du poste (pas sur un partage réseau)</div>
        </div>
        <button className="btn ghost sm" onClick={async () => { const p = await sx.saveFile({ title: "Base d'index", defaultPath: v.db_path || app.info?.db_path, filters: [{ name: "SQLite", extensions: ["db"] }] }); if (p) set("db_path", p); }}>Changer…</button>
        <button className="btn ghost sm" onClick={() => void sx.reveal(app.info?.db_path || dbPath)}>Ouvrir le dossier</button>
      </div>
      <div className={cls("cache_dir", false)} data-set="cache_dir" data-testid="tool-cache">
        <div className="rx-ticon t-amber"><Icon name="layers" size={20} /></div>
        <div className="grow col" style={{ gap: 4 }}>
          <div className="row"><b>Cache d'aperçu</b>{cacheChanged && <Badge text="AU REDÉMARRAGE" tone="blue" />}</div>
          <div className="mono small selectable ellipsis">{cachePath}</div>
          <div className="small muted">{diag ? `${bytes(diag.cache_bytes)} · ${nf(diag.cache_apercus)} aperçu(s) Word déjà produits · ` : ""}se reconstruit à la consultation</div>
        </div>
        <button className="btn ghost sm" onClick={async () => { const p = await sx.pickFolder({ title: "Cache d'aperçu", defaultPath: v.cache_dir || app.info?.cache_dir }); if (p) set("cache_dir", p); }}>Changer…</button>
        <button className="btn danger sm" onClick={async () => {
          const k = await ask({ title: "Vider le cache d'aperçu", text: `${diag ? `${nf(diag.cache_fichiers)} fichier(s), ${bytes(diag.cache_bytes)}` : "Les aperçus Word"} seront supprimés.`,
            details: "L'index, le journal et les exports ne sont pas touchés. Chaque relevé Word sera reconverti à sa prochaine consultation (quelques secondes la première fois).", choices: ["Vider le cache"], danger: true });
          if (k !== 0) return;
          try { const r = await call<{ supprimes: number; conserves: number }>("cache_clear", { confirm: "VIDER" }); toast(`Cache d'aperçu vidé : ${nf(r.supprimes)} élément(s) supprimé(s).`); void call<Diag>("diagnostic").then(setDiag); }
          catch (e) { toast((e as Error).message, { tone: "red" }); }
        }} data-testid="cache-clear">Vider…</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ diagnostic
function DiagnosticSection({ appInfo }: { appInfo: { version: string; logs: string; platform: string } | null }) {
  const toast = useToast();
  const [d, setD] = useState<Diag | null>(null);
  useEffect(() => { void call<Diag>("diagnostic").then(setD).catch((e) => toast((e as Error).message, { tone: "red" })); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const ua = navigator.userAgent;
  const electron = /Electron\/([\d.]+)/.exec(ua)?.[1];
  const chrome = /Chrome\/([\d.]+)/.exec(ua)?.[1];
  const rows: [string, ReactNode, (() => void)?, string?][] = d ? [
    ["Scolarix", <span className="mono" data-testid="settings-version">{appInfo?.version || d.version}</span>],
    ["Moteur", `Python ${d.python} · ${d.fige ? "exécutable installé" : "développement"} · processus ${d.pid}`],
    ["Interface", `Electron ${electron || "?"} · Chromium ${chrome || "?"}`],
    ["Système", `${d.systeme} (${d.machine})`],
    ["Convertisseur Word → PDF", d.convertisseur],
    ["Index", `${nf(d.releves)} relevés · ${nf(d.fichiers)} fichiers · dernière indexation ${d.derniere_indexation || "jamais"}`],
    ["Dossier de données", <span className="mono selectable">{d.data_dir}</span>, () => void sx.openPath(d.data_dir), "Ouvrir"],
    ["Journal des incidents", <span className="mono selectable">{appInfo?.logs || d.logs_dir}</span>, () => void sx.openPath(appInfo?.logs || d.logs_dir), "Ouvrir"],
    ["Journaux du moteur", <span className="mono selectable">{d.logs_dir}</span>, () => void sx.openPath(d.logs_dir), "Ouvrir"],
    ["Fichier de configuration", <span className="mono selectable">{d.config_path}</span>, () => void sx.reveal(d.config_path), "Afficher"],
    ["Base d'index", <span className="mono selectable">{d.db_path} · {bytes(d.db_bytes)}</span>, () => void sx.reveal(d.db_path), "Afficher"],
    ["Cache d'aperçu", <span className="mono selectable">{d.cache_dir} · {bytes(d.cache_bytes)}</span>, () => void sx.openPath(d.cache_dir), "Ouvrir"],
    ["Dossier des exports", <span className="mono selectable">{d.exports_dir || "—"}</span>, d.exports_dir ? () => void sx.openPath(d.exports_dir) : undefined, "Ouvrir"],
  ] : [];
  const copy = async () => {
    if (!d) return;
    const text = [`Scolarix ${appInfo?.version || d.version}`, `Moteur : Python ${d.python} (${d.fige ? "installé" : "dév."})`, `Interface : Electron ${electron} / Chromium ${chrome}`,
      `Système : ${d.systeme} ${d.machine}`, `Convertisseur : ${d.convertisseur}`, `Index : ${d.releves} relevés, ${d.fichiers} fichiers, dernière indexation ${d.derniere_indexation || "jamais"}`,
      `Données : ${d.data_dir}`, `Journaux : ${appInfo?.logs || d.logs_dir}`, `Configuration : ${d.config_path}`, `Base : ${d.db_path} (${bytes(d.db_bytes)})`, `Cache : ${d.cache_dir} (${bytes(d.cache_bytes)})`].join("\n");
    try { await navigator.clipboard.writeText(text); toast("Diagnostic copié : collez-le dans votre message à la Cellule informatique."); } catch { toast("Copie impossible.", { tone: "red" }); }
  };
  if (!d) return <div className="muted"><span className="spinner" /> Lecture du poste…</div>;
  return (
    <div className="col" data-testid="diagnostic">
      <table className="rx-kv"><tbody>
        {rows.map(([k, val, act, al]) => <tr key={k}><th>{k}</th><td>{val}</td><td>{act && <button className="btn ghost sm" onClick={act}>{al}</button>}</td></tr>)}
      </tbody></table>
      <div className="row"><button className="btn secondary" onClick={() => void copy()} data-testid="diag-copy"><Icon name="doc" />Copier le diagnostic</button>
        <span className="muted small">Aucune donnée d'étudiant : versions et chemins seulement.</span></div>
    </div>
  );
}

// ------------------------------------------------------------------ exclusions de l'indexation (1.x : ExclusionsEditor)
function Exclusions({ items, root, onChange }: { items: string[]; root: string; onChange: (x: string[]) => void }) {
  const { prompt } = useDialogs();
  const [sel, setSel] = useState<number | null>(null);
  const add = (x: string | null) => { const t = (x || "").trim(); if (t && !items.includes(t)) onChange([...items, t]); };
  return (
    <div className="col" style={{ gap: 6 }} data-testid="exclusions">
      <div className="table-wrap" style={{ maxHeight: 160, minHeight: 60, border: "1px solid var(--line)" }}>
        <table className="grid"><tbody>
          {items.map((x, i) => (
            <tr key={x} className={sel === i ? "sel" : ""} onClick={() => setSel(i)}><td className="mono small selectable">{x}</td></tr>
          ))}
          {!items.length && <tr><td className="muted small" style={{ textAlign: "center", padding: 14 }}>Aucun dossier ni motif exclu.</td></tr>}
        </tbody></table>
      </div>
      <div className="row wrap">
        <button className="btn secondary sm" onClick={async () => add(await sx.pickFolder({ title: "Sous-dossier à exclure", defaultPath: root || undefined }))}>Ajouter un sous-dossier…</button>
        <button className="btn secondary sm" onClick={async () => add(await prompt("Motif à exclure", "Nom ou motif (ex. *DIPLOME*, Brouillons, *.tmp) :"))} data-testid="exclusion-pattern">Ajouter un motif…</button>
        <button className="btn ghost sm" disabled={sel === null || sel >= items.length} onClick={() => { onChange(items.filter((_, k) => k !== sel)); setSel(null); }} data-testid="exclusion-remove">Retirer</button>
      </div>
      <div className="muted small">Un dossier exclu est ignoré avec tout son contenu. Un motif (* = n'importe quel texte) s'applique au nom des dossiers ET des fichiers, sans tenir compte des majuscules ni des accents : « *DIPLOME* », « Brouillons », « *attestation* ». Les fichiers déjà indexés qui deviennent exclus sont retirés de l'index à la prochaine indexation (jamais du disque).</div>
    </div>
  );
}
