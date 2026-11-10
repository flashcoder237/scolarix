import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { call, sx } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Icon, useDebounced, useDialogs, useToast } from "../components/ui";
import { PdfView } from "../components/PdfView";
import {
  NIVEAUX, NIV_ONE, NIV_TONE, errMsg, fr, pl, typing,
  type Detail, type FileRow, type FilesPage, type Filter, type Summary, type Vue,
} from "../indexation";
import { FixAction } from "./IndexDashboard";
import type { IxNav } from "./Indexation";

// Vue « Anomalies » : un écran de TRI. Causes groupées (erreurs, avertissements, infos) | fichiers de la cause,
// groupés par dossier, avec actions en lot | détail du fichier avec son aperçu.
// La liste est paginée par le moteur (200 fichiers à la fois) et virtualisée : 10 000 anomalies restent fluides.

const PAGE = 200;
const ROW_H = 44;          // hauteur fixe d'une ligne (fichier ou en-tête de dossier) : liste virtualisée
const OVERSCAN = 8;

type Item = { kind: "fold"; dossier: string; label: string; n: number } | { kind: "file"; row: FileRow; idx: number };

export function AnomalyTriage({ nav, running, refreshOverview }: { nav: IxNav; running: boolean; refreshOverview: () => void }) {
  const app = useApp();
  const toast = useToast();
  const { ask } = useDialogs();
  const [vue, setVue] = useState<Vue>("a_traiter");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [cause, setCause] = useState<string>(nav.cause || "");
  const [qRaw, setQ] = useState("");
  const q = useDebounced(qRaw, 250);
  const [dossier, setDossier] = useState("");
  const [annee, setAnnee] = useState("");
  const [grouped, setGrouped] = useState(true);
  const [page, setPage] = useState<FilesPage | null>(null);
  const [rows, setRows] = useState<FileRow[]>([]);
  const [cur, setCur] = useState(0);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [allChecked, setAllChecked] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [rendering, setRendering] = useState(false);
  const seq = useRef(0);
  const loadingMore = useRef(false);
  const lastClick = useRef<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const causesRef = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState({ top: 0, h: 600 });

  const filter: Filter = useMemo(() => ({ cause, vue, q: q.trim(), dossier, annee }), [cause, vue, q, dossier, annee]);

  // ------------------------------------------------------------------ chargement
  const loadSummary = useCallback(async (v: Vue = vue) => {
    try {
      const s = await call<Summary>("anomalies_summary", { vue: v });
      setSummary(s);
      return s;
    } catch (e) { toast(errMsg(e), { tone: "red" }); return null; }
  }, [vue, toast]);

  const loadFiles = useCallback(async (reset: boolean) => {
    const my = reset ? ++seq.current : seq.current;
    if (!reset && loadingMore.current) return;
    loadingMore.current = true;
    try {
      const offset = reset ? 0 : rows.length;
      const p = await call<FilesPage>("anomalies_files", { filter, offset, limit: PAGE });
      if (my !== seq.current) return;
      setPage(p);
      setRows((old) => (reset ? p.rows : [...old, ...p.rows]));
    } catch (e) { toast(errMsg(e), { tone: "red" }); } finally { loadingMore.current = false; }
  }, [filter, rows.length, toast]);

  // vue À traiter / Vues / Tout : effectifs par cause ; la cause choisie reste si elle existe encore
  useEffect(() => {
    void loadSummary(vue).then((s) => {
      if (!s) return;
      setCause((c) => (c.startsWith("@") || s.causes.some((x) => x.code === c) ? c : s.causes[0]?.code || ""));
    });
  }, [vue]); // eslint-disable-line react-hooks/exhaustive-deps
  // arrivée depuis l'état du fonds (clic sur une cause, sur « avec avertissement »…)
  useEffect(() => { if (nav.cause !== undefined) { setCause(nav.cause); setDossier(""); setAnnee(""); } }, [nav.nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  // nouveau filtre : liste rechargée depuis le début, sélection vidée
  useEffect(() => {
    setChecked(new Set()); setAllChecked(false); setCur(0); lastClick.current = null;
    listRef.current?.scrollTo({ top: 0 });
    void loadFiles(true);
  }, [filter]); // eslint-disable-line react-hooks/exhaustive-deps
  // fin d'une passe (relecture) : tout est relu
  const lastEv = app.indexing.last?.type;
  useEffect(() => { if (lastEv === "exit") { void loadSummary(); void loadFiles(true); } }, [app.indexing.last]); // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------------ éléments affichés (en-têtes de dossier + fichiers)
  const counts = useMemo(() => new Map((page?.dossiers || []).map((d) => [d.dossier, d])), [page]);
  const { items, itemOfRow } = useMemo(() => {
    const out: Item[] = [];
    const map: number[] = [];
    let prev: string | null = null;
    rows.forEach((r, idx) => {
      if (grouped && r.dossier !== prev) {
        const d = counts.get(r.dossier);
        out.push({ kind: "fold", dossier: r.dossier, label: d?.label || r.dossier || "(sans dossier)", n: d?.n || 0 });
        prev = r.dossier;
      }
      map[idx] = out.length;
      out.push({ kind: "file", row: r, idx });
    });
    return { items: out, itemOfRow: map };
  }, [rows, grouped, counts]);
  const total = page?.total || 0;
  const nFolders = (page?.dossiers || []).filter((d) => d.n > 0).length;
  const estimate = Math.max(items.length, total + (grouped ? nFolders : 0));

  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setScroll((s) => ({ ...s, h: el.clientHeight })));
    ro.observe(el);
    setScroll({ top: el.scrollTop, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);
  const from = Math.max(0, Math.floor(scroll.top / ROW_H) - OVERSCAN);
  const to = Math.min(items.length, Math.ceil((scroll.top + scroll.h) / ROW_H) + OVERSCAN);
  // défilement vers des lignes pas encore chargées : page suivante
  useEffect(() => {
    if (rows.length < total && to >= items.length - 20) void loadFiles(false);
  }, [to, items.length, rows.length, total]); // eslint-disable-line react-hooks/exhaustive-deps
  const firstVisible = items[Math.floor(scroll.top / ROW_H)];
  const stickFold = grouped && firstVisible && firstVisible.kind === "file" && scroll.top > 0
    ? counts.get(firstVisible.row.dossier) : undefined;

  const current = rows[cur] as FileRow | undefined;
  const ensureVisible = useCallback((idx: number) => {
    const el = listRef.current;
    const i = itemOfRow[idx];
    if (!el || i === undefined) return;
    const top = i * ROW_H, bottom = top + ROW_H;
    if (top < el.scrollTop + ROW_H) el.scrollTop = Math.max(0, top - ROW_H);
    else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight;
  }, [itemOfRow]);

  // ------------------------------------------------------------------ détail du fichier courant
  const curPath = current?.chemin;
  useEffect(() => {
    if (curPath === undefined) { setDetail(null); return; }
    let gone = false;
    const t = window.setTimeout(() => {
      void call<Detail>("anomalies_detail", { chemin: curPath }).then((d) => { if (!gone) setDetail(d); }).catch(() => undefined);
    }, 90);
    return () => { gone = true; window.clearTimeout(t); };
  }, [curPath]);

  // ------------------------------------------------------------------ sélection
  const selCount = allChecked ? total : checked.size;
  const target = (): { items: string[] | null; label: string } =>
    allChecked ? { items: null, label: pl(total, "fichier") } : { items: [...checked], label: pl(checked.size, "fichier") };
  const toggle = (idx: number, shift = false) => {
    const r = rows[idx];
    if (!r) return;
    setAllChecked(false);
    setChecked((old) => {
      const n = new Set(allChecked ? rows.map((x) => x.chemin) : old);
      if (shift && lastClick.current !== null) {
        const [a, b] = [Math.min(lastClick.current, idx), Math.max(lastClick.current, idx)];
        const on = !n.has(r.chemin);
        for (let i = a; i <= b; i++) { if (on) n.add(rows[i].chemin); else n.delete(rows[i].chemin); }
      } else if (n.has(r.chemin)) n.delete(r.chemin); else n.add(r.chemin);
      return n;
    });
    lastClick.current = idx;
  };
  const toggleFolder = async (d: string) => {
    try {
      const p = await call<FilesPage>("anomalies_files", { filter: { ...filter, dossier: d }, offset: 0, limit: 2000 });
      const paths = p.rows.map((r) => r.chemin);
      setAllChecked(false);
      setChecked((old) => {
        const n = new Set(old);
        const allIn = paths.every((x) => n.has(x));
        paths.forEach((x) => (allIn ? n.delete(x) : n.add(x)));
        return n;
      });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const clearSel = () => { setChecked(new Set()); setAllChecked(false); };

  // ------------------------------------------------------------------ actions
  const afterSeen = (paths: string[] | null, seen: boolean) => {
    const leaves = (vue === "a_traiter" && seen) || (vue === "vues" && !seen);
    if (paths === null) { void loadFiles(true); }
    else if (leaves) {
      const gone = new Set(paths);
      const kept = rows.filter((r) => !gone.has(r.chemin));
      // la ligne suivante devient la ligne courante (traitement à la chaîne)
      const nextIdx = Math.min(kept.length - 1, rows.slice(0, cur).filter((r) => !gone.has(r.chemin)).length);
      setRows(kept);
      setPage((p) => (p ? { ...p, total: Math.max(0, p.total - (rows.length - kept.length)) } : p));
      setCur(Math.max(0, nextIdx));
    } else {
      const set = new Set(paths);
      setRows((rs) => rs.map((r) => (set.has(r.chemin) ? { ...r, vu: seen } : r)));
    }
    setDetail((d) => (d && (paths === null || paths.includes(d.chemin)) ? { ...d, anomalies: d.anomalies.map((a) => ({ ...a, vu: cause.startsWith("@") || !cause || a.code === cause ? seen : a.vu })) } : d));
    void loadSummary();
    refreshOverview();
    app.refreshStats();
  };
  const markSeen = async (items: string[] | null, seen = true, label?: string) => {
    try {
      const r = await call<{ n: number; fichiers: number }>("anomalies_mark_seen", { items, filter, seen });
      clearSel();
      afterSeen(items, seen);
      toast(seen ? `${label || pl(r.fichiers, "fichier")} marqué(s) « vu » : ${pl(r.n, "anomalie")} ne reviendront plus dans « À traiter ».`
        : `${label || pl(r.fichiers, "fichier")} remis « à traiter ».`, {
        tone: "blue", ms: 7000, sound: false, key: "anom-vu",
        action: items ? { label: "Annuler", run: () => void call("anomalies_mark_seen", { items, filter: { cause: filter.cause }, seen: !seen }).then(() => { void loadSummary(); void loadFiles(true); refreshOverview(); }) } : undefined,
      });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const reread = async (items: string[] | null) => {
    if (running) { toast("Une passe est déjà en cours : relisez ces fichiers quand elle sera terminée.", { tone: "amber" }); return; }
    try {
      const r = await call<{ fichiers: number }>("index_reread", { items, filter: items ? { cause: filter.cause, vue: "tout" } : filter });
      clearSel();
      toast(`Relecture de ${pl(r.fichiers, "fichier")} lancée : le guichet reste utilisable pendant la passe.`, { tone: "blue", ms: 6000 });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const exportCsv = async (items: string[] | null) => {
    try {
      const dir = await call<string>("exports_dir").catch(() => "");
      const sep = dir.includes("\\") ? "\\" : "/";
      const stamp = new Date().toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
      const path = await sx.saveFile({ title: "Exporter les anomalies (CSV)", defaultPath: dir ? `${dir}${sep}Anomalies_${stamp}.csv` : `Anomalies_${stamp}.csv`,
        filters: [{ name: "CSV (Excel)", extensions: ["csv"] }] });
      if (!path) return;
      const r = await call<{ path: string; count: number }>("anomalies_export", { path, items, filter: items ? { cause: filter.cause, vue: "tout" } : filter });
      toast(`Anomalies exportées : ${pl(r.count, "ligne")} (${r.path}).`, { tone: "green", action: { label: "Ouvrir", run: () => void sx.openPath(r.path) } });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const excludeFolder = async (d: string) => {
    if (running) { toast("Une passe est en cours : excluez ce dossier quand elle sera terminée.", { tone: "amber" }); return; }
    try {
      const imp = await call<{ label: string; fichiers: number; releves: number; anomalies: number; deja: boolean }>("index_exclude_folder", { dossier: d });
      const i = await ask({
        title: "Exclure ce dossier",
        danger: imp.releves > 0,
        text: `« ${imp.label} » ${imp.deja ? "est déjà dans les exclusions" : "sera ajouté aux exclusions (enregistré tout de suite)"} : il ne sera plus lu par le robot.\n\n`
          + `Retirés de l'index dès maintenant : ${pl(imp.fichiers, "fichier")}, ${pl(imp.releves, "relevé")} et ${pl(imp.anomalies, "anomalie")}.`
          + (imp.releves ? "\nCes relevés ne seront plus trouvés au guichet." : ""),
        details: "Les archives d'origine ne sont jamais modifiées. Pour annuler : retirer le dossier des exclusions (Réglages › Indexation) puis relancer une passe.",
        choices: ["Exclure ce dossier"],
      });
      if (i !== 0) return;
      const r = await call<{ exclusions: string[]; fichiers: number }>("index_exclude_folder", { dossier: d, apply: true });
      await app.saveSettings({ exclusions: r.exclusions }).catch(() => undefined);
      toast(`Dossier exclu : ${pl(r.fichiers, "fichier")} retiré(s) de l'index.`, { tone: "green" });
      if (dossier === d) setDossier("");
      void loadSummary(); void loadFiles(true); refreshOverview(); app.refreshStats();
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const openFile = async (p: string) => {
    const err = await sx.openPath(p);
    if (err) toast(`Ouverture impossible : ${err}`, { tone: "amber" });
  };
  const reveal = async (p: string) => {
    if (!(await sx.reveal(p))) toast("Fichier introuvable à cet emplacement (déplacé ou renommé) : dossier le plus proche ouvert.", { tone: "amber", sound: false });
  };
  const renderWord = async () => {
    if (!detail?.releve_id) return;
    setRendering(true);
    try {
      const r = await call<{ preview_pdf: string | null }>("preview", { releve_id: detail.releve_id });
      setDetail((d) => (d && d.chemin === detail.chemin ? { ...d, preview_pdf: r.preview_pdf } : d));
    } catch (e) { toast(errMsg(e), { tone: "amber" }); } finally { setRendering(false); }
  };

  // ------------------------------------------------------------------ clavier
  const move = (d: number, extend = false) => {
    if (!rows.length) return;
    const n = Math.max(0, Math.min(rows.length - 1, cur + d));
    if (extend) { if (!checked.has(rows[cur].chemin)) toggle(cur); if (!checked.has(rows[n].chemin)) toggle(n); }
    setCur(n);
    ensureVisible(n);
  };
  const causeList = useMemo(() => {
    const out: string[] = [];
    for (const [niv] of NIVEAUX) {
      const cs = (summary?.causes || []).filter((c) => c.niveau === niv);
      if (cs.length) { out.push(`@${niv}`); cs.forEach((c) => out.push(c.code)); }
    }
    return out;
  }, [summary]);
  const moveCause = (d: number) => {
    const i = causeList.indexOf(cause);
    const n = causeList[Math.max(0, Math.min(causeList.length - 1, (i < 0 ? 0 : i) + d))];
    if (n !== undefined) setCause(n);
  };
  const inCauses = () => !!causesRef.current?.contains(document.activeElement);
  const isFilter = (e: KeyboardEvent) => (e.target as HTMLElement | null)?.dataset?.testid === "anom-filter";
  useShortcuts({
    "arrowdown": (e) => { if (typing(e) && !isFilter(e)) return false; if (inCauses()) moveCause(1); else move(1); },
    "arrowup": (e) => { if (typing(e) && !isFilter(e)) return false; if (inCauses()) moveCause(-1); else move(-1); },
    "shift+arrowdown": (e) => { if (typing(e)) return false; move(1, true); },
    "shift+arrowup": (e) => { if (typing(e)) return false; move(-1, true); },
    " ": (e) => { if (typing(e) || inCauses()) return false; toggle(cur); },
    "enter": (e) => { if (typing(e) || inCauses()) return false; if (current) void openFile(current.chemin); },
    "shift+enter": (e) => { if (typing(e)) return false; if (current) void reveal(current.chemin); },
    "v": (e) => { if (typing(e)) return false; if (current) void markSeen([current.chemin], !current.vu, current.nom); },
    "r": (e) => { if (typing(e)) return false; if (current) void reread([current.chemin]); },
    "ctrl+a": (e) => { if (typing(e)) return false; if (total) { setAllChecked(true); setChecked(new Set()); } },
    "escape": () => { if (selCount) { clearSel(); return; } return false; },
  });

  // ------------------------------------------------------------------ rendu
  const causes = summary?.causes || [];
  const maxN = Math.max(1, ...causes.map((c) => c.n));
  const sel = causes.find((c) => c.code === cause);
  const niv = cause.startsWith("@") ? cause.slice(1) : sel?.niveau || "";
  const ALL: Record<string, string> = { ERREUR: "Toutes les erreurs", AVERTISSEMENT: "Tous les avertissements", INFO: "Toutes les informations" };
  const title = cause.startsWith("@") ? ALL[niv] || "Toutes les anomalies" : sel?.label || (cause ? cause : "Toutes les anomalies");
  const fix = summary?.fix && summary.fix.code === cause ? summary.fix : null;
  const tot = summary?.totaux;
  const leaves = vue !== "vues";

  return (
    <div className="ax" data-testid="anomalies">
      {/* ---------------------------------------------------------------- causes */}
      <section className="card ax-causes">
        <div className="row" style={{ padding: "0 4px" }}><span className="h3 grow">Causes</span>
          <span className="muted tiny" data-testid="anom-total">{tot ? `${fr(tot[vue].n)} · ${pl(tot[vue].fichiers, "fichier")}` : ""}</span></div>
        <div className="seg ax-vues" role="radiogroup" aria-label="Anomalies à traiter, vues ou toutes" data-testid="anom-vue">
          {([["a_traiter", "À traiter"], ["vues", "Vues"], ["tout", "Tout"]] as [Vue, string][]).map(([v, l]) => (
            <button key={v} role="radio" aria-checked={vue === v} className={vue === v ? "on" : ""} onClick={() => setVue(v)}>
              {l}{v === "vues" && tot ? <span className="n"> {fr(tot.vues.fichiers)}</span> : null}</button>
          ))}
        </div>
        <div className="ax-czs" ref={causesRef} tabIndex={0} role="listbox" aria-label="Causes (↑ ↓ quand la liste a le focus)" data-testid="anom-causes">
          {NIVEAUX.map(([n, lab]) => {
            const cs = causes.filter((c) => c.niveau === n);
            if (!cs.length) return null;
            return (
              <div key={n}>
                <button className={`ax-grp${cause === `@${n}` ? " sel" : ""}`} onClick={() => setCause(`@${n}`)} title={`Tous les fichiers : ${lab.toLowerCase()}`}>
                  <span>{lab}</span><span>{fr(cs.reduce((a, c) => a + c.n, 0))}</span></button>
                {cs.map((c) => (
                  <button key={c.code} role="option" aria-selected={cause === c.code} className={`ax-cz${cause === c.code ? " sel" : ""}`} onClick={() => setCause(c.code)}
                    title={`${c.label} : ${pl(c.n, "anomalie")} dans ${pl(c.fichiers, "fichier")}\nCorrection usuelle : ${c.fix}`} data-testid="anom-cause" data-code={c.code}>
                    <i className={`d ${NIV_TONE[c.niveau]}`} /><span className="ellipsis">{c.label}</span><b>{fr(c.n)}</b>
                    <div className="bar"><i className={NIV_TONE[c.niveau]} style={{ width: `${(c.n * 100) / maxN}%` }} /></div>
                  </button>
                ))}
              </div>
            );
          })}
          {summary && !causes.length && <div className="empty small muted">{vue === "vues" ? "Aucune anomalie marquée « vue »." : "Aucune anomalie à traiter."}</div>}
        </div>
      </section>

      {/* ---------------------------------------------------------------- fichiers */}
      <section className="card ax-files">
        <div className="row"><div className="grow" style={{ minWidth: 0 }}>
          <div className="eyebrow">{niv ? `${NIV_ONE[niv]} · ` : ""}{pl(total, "fichier")} · {pl(nFolders, "dossier")}{page && page.anomalies !== total ? ` · ${pl(page.anomalies, "anomalie")}` : ""}</div>
          <div className="h2 ellipsis" data-testid="anom-title">{title}</div></div></div>
        {fix ? (
          <div className="ax-fix" data-testid="anom-fix"><Icon name="wrench" size={22} />
            <div className="txt"><b>Une cause commune : {fix.titre}.</b>{fix.texte}</div>
            <FixAction fix={fix} />
            <button className="btn primary sm" onClick={() => void reread(null)} disabled={running || !total} data-testid="anom-reread-all"><Icon name="refresh" size={14} />Relire les {fr(total)}</button>
          </div>
        ) : sel?.fix ? <div className="muted small ax-hint"><Icon name="info" size={13} /> Correction usuelle : {sel.fix}</div> : null}
        <div className="ax-filters">
          <label className="field ax-search"><Icon name="search" size={14} />
            <input value={qRaw} onChange={(e) => setQ(e.target.value)} placeholder="Filtrer par chemin, nom ou message…" data-testid="anom-filter"
              onKeyDown={(e) => { if (e.key === "Escape" && qRaw) { e.stopPropagation(); setQ(""); } }} /></label>
          <select className="field" value={dossier} onChange={(e) => setDossier(e.target.value)} data-testid="anom-folder" title="Dossier">
            <option value="">Tous les dossiers ({fr(nFolders)})</option>
            {(page?.dossiers || []).map((d) => <option key={d.dossier} value={d.dossier}>{d.label} ({fr(d.n)})</option>)}
          </select>
          <select className="field ax-hide13" value={annee} onChange={(e) => setAnnee(e.target.value)} data-testid="anom-year" title="Année académique (lue dans le chemin)">
            <option value="">Toutes les années</option>
            {(page?.annees || []).map((y) => <option key={y.annee} value={y.annee}>{y.annee === "-" ? "Sans année" : y.annee} ({fr(y.n)})</option>)}
          </select>
          <div className="seg sm" role="radiogroup" aria-label="Présentation">
            <button role="radio" aria-checked={grouped} className={grouped ? "on" : ""} onClick={() => setGrouped(true)}>Par dossier</button>
            <button role="radio" aria-checked={!grouped} className={!grouped ? "on" : ""} onClick={() => setGrouped(false)}>Liste</button>
          </div>
        </div>
        <div className="ax-head">
          <span><Check on={allChecked || (rows.length > 0 && checked.size >= rows.length)} mid={!allChecked && checked.size > 0 && checked.size < rows.length}
            onClick={() => (allChecked || checked.size ? clearSel() : (setAllChecked(true), setChecked(new Set())))} label="Tout sélectionner (Ctrl+A)" /></span>
          <span>Fichier</span><span>Pages</span><span />
        </div>
        <div className="ax-listwrap">
        {stickFold && <div className="ax-fold stick"><span /><Icon name="folder" size={13} /><span className="ellipsis">{stickFold.label}</span><span className="muted nowrap">· {pl(stickFold.n, "fichier")}</span></div>}
        <div className="ax-list" ref={listRef} tabIndex={-1} onScroll={(e) => { const t = e.currentTarget.scrollTop; setScroll((s) => (s.top === t ? s : { ...s, top: t })); }} data-testid="anom-files">
          <div style={{ height: estimate * ROW_H, position: "relative" }}>
            {items.slice(from, to).map((it, k) => {
              const i = from + k;
              if (it.kind === "fold") {
                return (
                  <div key={`f:${it.dossier}`} className="ax-fold" style={{ top: i * ROW_H }}>
                    <Check on={false} onClick={() => void toggleFolder(it.dossier)} label="Cocher les fichiers de ce dossier" />
                    <Icon name="folder" size={13} /><span className="ellipsis" title={it.dossier}>{it.label}</span><span className="muted nowrap">· {pl(it.n, "fichier")}</span>
                    <button className="btn link small nowrap" onClick={() => void excludeFolder(it.dossier)} disabled={running} data-testid="anom-exclude">Exclure ce dossier</button>
                  </div>
                );
              }
              const r = it.row;
              const on = allChecked || checked.has(r.chemin);
              return (
                <div key={r.chemin} className={`ax-row${it.idx === cur ? " cur" : ""}${on ? " on" : ""}${r.vu ? " vu" : ""}`} style={{ top: i * ROW_H }}
                  onClick={(e) => { listRef.current?.focus({ preventScroll: true }); if (e.shiftKey) toggle(it.idx, true); else setCur(it.idx); }} onDoubleClick={() => void openFile(r.chemin)} data-testid="anom-file">
                  <Check on={on} onClick={(e) => { e.stopPropagation(); toggle(it.idx, e.shiftKey); }} label="Cocher (Espace)" />
                  <div className="nm"><div className="l1 ellipsis" title={r.chemin}>{r.nom}{r.vu && <span className="badge t-gray ax-vu">VU</span>}</div>
                    <div className="l2 ellipsis" title={r.message}>{r.message}{r.n > 1 ? ` (+${r.n - 1})` : ""}</div></div>
                  <span className="small muted">{r.nb_pages ? `${r.nb_pages} p.` : "—"}</span>
                  <span className="acts">
                    <button className="btn ghost xs" onClick={(e) => { e.stopPropagation(); void openFile(r.chemin); }} title="Ouvrir le fichier (Entrée)"><Icon name="file" size={13} /></button>
                    <button className="btn ghost xs" onClick={(e) => { e.stopPropagation(); void reveal(r.chemin); }} title="Ouvrir le dossier (Maj+Entrée)"><Icon name="folder_open" size={13} /></button>
                  </span>
                </div>
              );
            })}
          </div>
          {page && !total && <div className="empty muted small ax-empty">{q || dossier || annee ? "Aucun fichier pour ces filtres." : vue === "vues" ? "Aucun fichier marqué « vu » pour cette cause." : "Rien à traiter pour cette cause."}</div>}
        </div>
        </div>
        {selCount > 0 && (
          <div className="ax-selbar" data-testid="anom-selbar">
            <Icon name="check" size={15} /><span className="grow">{allChecked ? `Tous les ${pl(total, "fichier")} de la vue sélectionnés` : `${pl(selCount, "fichier sélectionné", "fichiers sélectionnés")}`}</span>
            <button className="btn xs" onClick={() => void reread(target().items)} disabled={running} data-testid="anom-reread"><Icon name="refresh" size={13} />Relire</button>
            <button className="btn xs" onClick={() => void markSeen(target().items, leaves, target().label)} data-testid="anom-seen"><Icon name={leaves ? "eye_off" : "eye"} size={13} />{leaves ? "Marquer vus" : "Remettre à traiter"}</button>
            <button className="btn xs" onClick={() => void exportCsv(target().items)} data-testid="anom-csv"><Icon name="download" size={13} />CSV</button>
            <button className="btn xs" onClick={clearSel} aria-label="Vider la sélection (Échap)" title="Vider la sélection (Échap)"><Icon name="x" size={13} /></button>
          </div>
        )}
      </section>

      {/* ---------------------------------------------------------------- détail */}
      <section className="card ax-det" data-testid="anom-detail">
        {detail && current ? (
          <>
            <div className="row"><span className="h3 grow ellipsis" title={detail.nom}>{detail.nom}</span>{detail.nb_pages ? <Badge text={`${detail.nb_pages} P.`} tone="gray" /> : null}</div>
            <div className="ax-pv">
              {detail.preview_pdf ? (
                <PdfView file={detail.preview_pdf} version={detail.chemin} defaultFit="largeur" toolbar={false}
                  first={pageFor(detail, cause)} last={Math.min(detail.nb_pages || pageFor(detail, cause), pageFor(detail, cause) + 2)} />
              ) : (
                <div className="ax-ph">
                  <Icon name={detail.is_word ? "word" : "file"} size={34} />
                  {!detail.existe ? <span>Fichier introuvable à cet emplacement (déplacé, renommé ou supprimé).</span>
                    : detail.is_word ? <>
                      <span>Aperçu Word pas encore produit.</span>
                      {detail.releve_id ? <button className="btn secondary sm" onClick={() => void renderWord()} disabled={rendering} data-testid="anom-render">{rendering ? "Préparation de l'aperçu…" : "Produire l'aperçu"}</button>
                        : <span className="tiny">Aucun relevé lu dans ce fichier : ouvrez-le dans Word.</span>}
                    </> : <span>Pas d'aperçu pour ce fichier.</span>}
                </div>
              )}
            </div>
            <div className="ax-msgs">
              {detail.anomalies.map((a) => (
                <div key={a.id} className={`ax-msg t-${NIV_TONE[a.niveau]}${cause && !cause.startsWith("@") && a.code !== cause ? " other" : ""}`} title={`${a.label} · ${a.horodatage}`}>
                  <Icon name={a.niveau === "INFO" ? "info" : "alert"} size={14} /><span className="grow">{a.message}</span>{a.vu && <span className="badge t-gray ax-vu">VU</span>}
                </div>
              ))}
            </div>
            <div className="ax-path selectable" data-testid="anom-path">{detail.chemin}</div>
            <div className="ax-acts">
              <button className="btn ghost sm" onClick={() => void openFile(detail.chemin)} disabled={!detail.existe}><Icon name="file" size={14} />Ouvrir le fichier</button>
              <button className="btn ghost sm" onClick={() => void reveal(detail.chemin)}><Icon name="folder_open" size={14} />Ouvrir le dossier</button>
              <button className="btn secondary sm" onClick={() => void reread([detail.chemin])} disabled={running || !detail.existe} data-testid="anom-reread-one"><Icon name="refresh" size={14} />Relire ce fichier</button>
              <button className="btn ghost sm" onClick={() => void markSeen([detail.chemin], !current.vu, detail.nom)} data-testid="anom-seen-one">
                <Icon name={current.vu ? "eye" : "eye_off"} size={14} />{current.vu ? "Remettre à traiter" : "Marquer vu"}</button>
            </div>
            <div className="tiny muted ax-keys"><span className="key">↑</span><span className="key">↓</span> fichier · <span className="key">Espace</span> cocher · <span className="key">Entrée</span> ouvrir · <span className="key">V</span> marquer vu · <span className="key">R</span> relire</div>
          </>
        ) : (
          <div className="empty muted small">{total ? "Choisissez un fichier." : "Aucun fichier sélectionné."}
            <div className="tiny ax-keys"><span className="key">↑</span><span className="key">↓</span> fichier · <span className="key">Espace</span> cocher · <span className="key">Ctrl+A</span> tout · <span className="key">Entrée</span> ouvrir · <span className="key">V</span> vu · <span className="key">R</span> relire · <span className="key">Ctrl+Tab</span> état du fonds</div>
          </div>
        )}
      </section>
    </div>
  );
}

/** Page à montrer : celle citée par l'anomalie de la cause choisie (« Page 4 : … »), sinon la première. */
function pageFor(d: Detail, cause: string): number {
  const a = d.anomalies.find((x) => x.page && (!cause || cause.startsWith("@") || x.code === cause));
  const p = a?.page || 1;
  return d.nb_pages && p > d.nb_pages ? 1 : p;
}

function Check({ on, mid = false, onClick, label }: { on: boolean; mid?: boolean; onClick: (e: React.MouseEvent) => void; label: string }) {
  return (
    <button className={`ax-cb${on ? " on" : ""}${mid ? " mid" : ""}`} role="checkbox" aria-checked={mid ? "mixed" : on} aria-label={label} title={label}
      onClick={(e) => { e.stopPropagation(); onClick(e); }} tabIndex={-1}>
      {on ? <Icon name="check" size={12} stroke={3} /> : mid ? <i /> : null}
    </button>
  );
}
