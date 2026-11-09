import { useEffect, useState } from "react";
import { call, sx } from "../api";
import { useApp } from "../state";
import { Badge, Chip, Icon, Seg, Switch, useDialogs, useToast } from "../components/ui";

interface Anomalie { id: number; horodatage: string; niveau: string; chemin: string | null; message: string; action: string | null }
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const short = (p: string, n = 90) => (p.length > n ? "…" + p.slice(-n) : p);
const FORMATS: Record<string, [string, string]> = { DOCX: ["Word .docx", "#1D4ED8"], DOC: ["Word .doc 97-2003", "#6D28D9"], PDF_TEXT: ["PDF texte", "#15803D"], PDF_SCAN: ["PDF scanné (OCR)", "#B45309"] };
const fr = (x: number | undefined) => (x || 0).toLocaleString("fr-FR");

export function Indexation() {
  const app = useApp();
  const toast = useToast();
  const { ask } = useDialogs();
  const s = app.settings;
  const [src, setSrc] = useState(s?.source_root || "");
  const [anoms, setAnoms] = useState<Anomalie[]>([]);
  const [niveau, setNiveau] = useState<string>("");
  const [live, setLive] = useState({ total: 0, done: 0, pct: 0, detail: "", current: "", stages: [0, 0, 0, 0], status: "PRÊT" });
  const running = app.indexing.running;

  useEffect(() => { if (s && !src) setSrc(s.source_root || ""); }, [s]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void call<Anomalie[]>("anomalies", { niveau: niveau || null }).then(setAnoms).catch(() => undefined); }, [niveau, running]);

  useEffect(() => {
    const ev = app.indexing.last;
    if (!ev) return;
    setLive((l) => {
      const n = { ...l };
      if (ev.type === "start") {
        n.total = ev.total; n.stages = [ev.total, 0, 0, 0]; n.status = "EN COURS";
        n.current = [`${ev.a_traiter ?? ev.total} fichier(s) à lire`, `${ev.workers || 1} processus`, ev.exclus ? `${ev.exclus} élément(s) exclu(s)` : ""].filter(Boolean).join(" · ");
      } else if (ev.type === "progress") {
        const c = ev.counters;
        n.done = ev.done; n.total = ev.total; n.pct = Math.floor((ev.done * 100) / Math.max(1, ev.total));
        const eta = ev.eta_s ? ` · restant ≈ ${ev.eta_s >= 3600 ? `${Math.floor(ev.eta_s / 3600)} h ${String(Math.floor((ev.eta_s % 3600) / 60)).padStart(2, "0")} min` : `${Math.floor(ev.eta_s / 60)} min ${Math.floor(ev.eta_s % 60)} s`}` : "";
        n.detail = `${ev.done} / ${ev.total} fichiers · ${c.ignores} inchangé(s) · ${c.echecs} en échec${ev.rate ? ` · ${ev.rate.toFixed(1)} fichiers/s` : ""}${eta}`;
        if (ev.file) n.current = `Dernier fichier lu : ${short(ev.file)}`;
        n.stages = [n.stages[0], c.extraits, c.blocs, c.ecrits];
      } else if (ev.type === "stage") {
        n.current = `${ev.stage === "ocr" ? `OCR de ${ev.pages} page(s)` : ev.stage === "conversion" ? `Conversion → PDF (${ev.backend})` : ev.stage} : ${short(ev.file, 80)}`;
      } else if (ev.type === "warning") {
        toast(ev.message, { tone: "amber" });
      } else if (ev.type === "done") {
        n.status = "TERMINÉ"; n.pct = 100;
        n.detail = `Terminé en ${ev.duree_s} s · ${ev.ecrits} relevé(s) écrit(s) · ${ev.ignores} inchangé(s) · ${ev.echecs} en échec`;
      } else if (ev.type === "exit") {
        if (n.status === "EN COURS" || n.status === "ARRÊT…") {
          n.status = "ARRÊTÉ";
          n.detail = ev.error ? `Le robot s'est arrêté : ${ev.error}` : "Indexation arrêtée. Relancez en mode différentiel pour reprendre là où il s'est arrêté.";
          toast(n.detail, { tone: "amber", ms: 15000 });
        }
        app.refreshStats();
      }
      return n;
    });
  }, [app.indexing.last]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    try {
      await call("index_start", { source: src });
      setLive({ total: 0, done: 0, pct: 0, detail: "Analyse de l'arborescence…", current: "", stages: [0, 0, 0, 0], status: "EN COURS" });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const stop = async () => {
    const force = live.status === "ARRÊT…";
    await call("index_stop", { force });
    setLive((l) => ({ ...l, status: "ARRÊT…", detail: force ? "Arrêt forcé." : "Arrêt demandé : fin des fichiers en cours… (cliquer encore pour forcer)" }));
  };
  const reset = async () => {
    try {
      const n = await call<Record<string, number>>("index_impact");
      const fr = (x: number) => x.toLocaleString("fr-FR");
      const i = await ask({
        title: "Vider l'index",
        danger: true,
        text: `Seront retirés : ${fr(n.releves)} relevé(s) indexé(s), ${fr(n.fichiers)} fichier(s), ${fr(n.anomalies)} anomalie(s) et les aperçus Word en cache.\n\n`
          + `Seront conservés : le journal (${fr(n.journal)} ligne(s)), le registre des délivrances (${fr(n.delivrances)} document(s)) et les demandes (${fr(n.demandes)}).`
          + (n.demandes_ouvertes ? `\n\nAttention : ${n.demandes_ouvertes} demande(s) en cours. Leurs relevés devront être ajoutés à nouveau après la réindexation.` : ""),
        details: "Les archives d'origine ne sont jamais touchées. La recherche au guichet sera vide jusqu'à la fin de la réindexation.",
        choices: ["Vider et tout réindexer", "Vider seulement"],
      });
      if (i < 0) return;
      const r = await call<{ releves: number; stats: any }>("index_reset", { confirm: "VIDER" });
      app.refreshStats();
      setLive({ total: 0, done: 0, pct: 0, detail: "", current: "", stages: [0, 0, 0, 0], status: "PRÊT" });
      toast(`Index vidé : ${fr(r.releves)} relevé(s) retiré(s). Journal, registre et demandes conservés.`, { tone: "green", ms: 12000 });
      if (i === 0) {
        await call("index_start", { source: src, full: true });
        setLive({ total: 0, done: 0, pct: 0, detail: "Analyse de l'arborescence…", current: "", stages: [0, 0, 0, 0], status: "EN COURS" });
      }
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const integrity = async () => {
    try {
      const r = await call<{ orphelins: number }>("check_integrity");
      if (r.orphelins) toast(`${r.orphelins} lien(s) orphelin(s) trouvé(s) : fichiers déplacés, renommés ou supprimés (voir les anomalies). Relancez une indexation différentielle sur le nouveau dossier : seuls les fichiers déplacés seront relus.`, { tone: "amber", ms: 15000 });
      else toast("Intégrité vérifiée : tous les fichiers indexés sont présents.");
      setNiveau(""); void call<Anomalie[]>("anomalies", { niveau: null }).then(setAnoms);
      app.refreshStats();
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };
  const reveal = async (path: string) => {
    if (!(await sx.reveal(path))) toast("Fichier introuvable à cet emplacement (déplacé ou renommé) : dossier le plus proche ouvert.", { tone: "amber", sound: false });
  };
  const tone = live.status === "EN COURS" ? "blue" : live.status === "TERMINÉ" ? "green" : live.status.startsWith("ARR") ? "amber" : "gray";
  const st = app.stats;

  return (
    <div className="page scroll">
      <header className="header">
        <div className="titles"><div className="eyebrow">Robot d'indexation · processus séparé</div><h1 className="h1">Indexation des archives</h1></div>
        <Badge text={live.status} tone={tone} big />
      </header>
      <section className="card">
        <div className="row">
          <Chip icon="folder" tone="blue" />
          <label className="lbl grow">Dossier racine des archives (lu en lecture seule, jamais modifié)
            <input className="field" value={src} onChange={(e) => setSrc(e.target.value)} placeholder="\\\\serveur\\archives ou D:\\Archives" />
          </label>
          <button className="btn ghost" style={{ marginTop: 18 }} onClick={async () => { const d = await sx.pickFolder({ title: "Dossier racine des archives", defaultPath: src || undefined }); if (d) setSrc(d); }}>Parcourir…</button>
        </div>
        <div className="muted tiny">Lecture seule : aucune écriture, suppression ni renommage dans ce dossier. Index local : <span className="mono selectable">{app.info?.db_path || "—"}</span></div>
        <div className="row wrap" data-testid="index-options">
          <Switch blue checked={!!s?.differential} onChange={(v) => void app.saveSettings({ differential: v })} label="Réindexation différentielle" title="Ignore les fichiers inchangés depuis la dernière passe" />
          <Switch blue checked={!!s?.ocr_enabled} onChange={(v) => void app.saveSettings({ ocr_enabled: v })} label="OCR des scans" title="Tesseract sur les PDF d'imprimante (plus lent)" />
          <Switch blue checked={!!s?.ocr_preprocess} onChange={(v) => void app.saveSettings({ ocr_preprocess: v })} label="Deskew + contraste Otsu" title="Redresse les scans de travers avant l'OCR" />
          <Switch blue checked={!!s?.word_com_enabled} onChange={(v) => void app.saveSettings({ word_com_enabled: v })} label="Passerelle COM Word" title="Lit les .doc 97-2003 avec Word (sinon LibreOffice)" />
          <Switch blue checked={!!s?.docx_convert_at_index} onChange={(v) => void app.saveSettings({ docx_convert_at_index: v })} label="Préconvertir les .docx" title="Aperçu immédiat, mais indexation bien plus lente. Sinon : aperçu produit à la 1re consultation." />
        </div>
        <div className="row wrap">
          <span className="muted small">Processus parallèles : {s?.index_workers ? s.index_workers : `automatique (${app.info?.default_workers ?? "?"})`}</span>
          <button className="btn ghost sm" onClick={() => app.openSettings("indexation")} title={(s?.exclusions || []).join("\n") || "Aucun dossier ni motif exclu"} data-testid="index-exclusions">
            <Icon name="filter" size={14} />{s?.exclusions?.length ? `Exclusions (${s.exclusions.length})…` : "Exclure des sous-dossiers…"}</button>
          <button className="btn ghost sm" onClick={() => void integrity()} disabled={running} data-testid="index-integrity" title="Fichiers indexés déplacés, renommés ou supprimés"><Icon name="link" size={14} />Vérification d'intégrité (liens orphelins)</button>
          <span className="grow" />
          {running ? <button className="btn ink" onClick={() => void stop()} data-testid="index-stop"><Icon name="stop" />Arrêter</button>
            : <button className="btn primary" onClick={() => void start()} data-testid="index-start"><Icon name="play" />Lancer l'indexation</button>}
        </div>
        <div className="progress"><div style={{ width: `${live.pct}%` }} /></div>
        <div className="row small"><span className="h3">{live.pct} %</span><span className="muted grow">{live.detail}</span></div>
        <div className="muted tiny ellipsis">{live.current}</div>
        <div className="stats-grid">
          {["Fichiers trouvés", "Pages extraites", "Blocs identifiés", "Relevés écrits"].map((l, i) => (
            <div key={l} className="soft"><div className="eyebrow">{l}</div><div className="kpi"><span className="v">{live.stages[i].toLocaleString("fr-FR")}</span></div></div>
          ))}
        </div>
      </section>
      <div className="stats-grid five" data-testid="index-kpis">
        {([["doc", "blue", "Relevés indexés", st?.releves], ["layers", "violet", "Étudiants distincts", st?.etudiants], ["eye", "green", "Pages analysées", st?.pages],
          ["link", "amber", "Relevés sur plusieurs pages", st?.multi_pages], ["x", "red", "Fichiers en échec", st?.echecs]] as const).map(([icon, t, label, n]) => (
          <div key={label} className="card kpi" style={{ flexDirection: "row", padding: 14 }}><Chip icon={icon} tone={t} size={36} />
            <div style={{ minWidth: 0 }}><div className="eyebrow ellipsis" title={label}>{label}</div><div className="kpi"><span className="v" style={t === "red" && n ? { color: "var(--red)" } : undefined}>{st ? fr(n) : "—"}</span></div></div></div>
        ))}
      </div>
      <div className="row" style={{ alignItems: "stretch", gap: 14 }}>
        <section className="card" style={{ flex: 1 }}>
          <div className="eyebrow">Fonds documentaire</div><h2 className="h3">Formats détectés</h2>
          {Object.entries(st?.formats || {}).filter(([k]) => FORMATS[k]).map(([k, n], _i, arr) => {
            const max = Math.max(1, ...arr.map(([, x]) => x));
            return (<div key={k} className="col" style={{ gap: 3 }}>
              <div className="row small"><span className="grow">{FORMATS[k][0]}</span><span className="muted">{fr(n)}</span></div>
              <div style={{ height: 8, borderRadius: 4, background: "var(--line-3)" }}><div style={{ height: 8, borderRadius: 4, width: `${(n * 100) / max}%`, background: FORMATS[k][1] }} /></div>
            </div>);
          })}
          {!Object.keys(st?.formats || {}).length && <div className="muted small">Aucun fichier indexé.</div>}
        </section>
        <section className="card" style={{ flex: 2 }}>
          <div className="eyebrow">Relevés par année académique</div>
          <h2 className="h3">{st?.annees?.length ? `${fr(st.releves)} relevés · ${st.annees[0][0].slice(0, 4)} → ${st.annees[st.annees.length - 1][0].slice(-4)}` : "—"}</h2>
          <div className="row" style={{ alignItems: "flex-end", height: 110, gap: 6 }} data-testid="index-years">
            {(st?.annees || []).map(([a, n]) => {
              const max = Math.max(1, ...(st?.annees || []).map(([, x]) => x));
              return (<div key={a} className="col" style={{ alignItems: "center", gap: 3, flex: 1 }} title={`${a} : ${fr(n)}`}>
                <div style={{ height: (n * 80) / max, width: "100%", maxWidth: 30, borderRadius: 6, background: "#1D4ED8" }} />
                <span className="tiny muted">{a.length >= 9 ? `${a.slice(2, 4)}-${a.slice(7, 9)}` : a}</span></div>);
            })}
          </div>
        </section>
      </div>
      <section className="card">
        <div className="row"><h2 className="h2 grow">Base d'index</h2>
          <span className="muted small">{st ? `${st.releves.toLocaleString("fr-FR")} relevés · ${st.etudiants.toLocaleString("fr-FR")} étudiants · ${st.fichiers.toLocaleString("fr-FR")} fichiers · dernière passe : ${st.derniere_indexation || "jamais"}` : ""}</span></div>
        <div className="row soft">
          <Icon name="refresh" />
          <span className="grow small">Recommencer de zéro : retire tous les relevés indexés et les aperçus en cache, puis relit toutes les archives. Le journal, le registre des délivrances et les demandes sont conservés.</span>
          <button className="btn danger" disabled={running} onClick={() => void reset()} data-testid="index-reset" title={running ? "Arrêtez d'abord l'indexation en cours" : ""}><Icon name="trash" />Vider l'index…</button>
        </div>
      </section>
      <section className="card" style={{ minHeight: 280 }}>
        <div className="row"><h2 className="h2 grow">Anomalies</h2>
          <Seg value={niveau} options={[["", "Toutes"], ["ERREUR", "Erreurs"], ["AVERTISSEMENT", "Avertissements"], ["INFO", "Infos"]]} onChange={setNiveau} /></div>
        <div className="table-wrap" style={{ maxHeight: 360 }}>
          <table className="grid">
            <colgroup><col style={{ width: 130 }} /><col style={{ width: 120 }} /><col /><col style={{ width: 150 }} /></colgroup>
            <thead><tr><th>Date</th><th>Niveau</th><th>Fichier · message</th><th /></tr></thead>
            <tbody>
              {anoms.map((a) => (
                <tr key={a.id}>
                  <td className="small">{a.horodatage.slice(0, 16)}</td>
                  <td><Badge text={a.niveau} tone={a.niveau === "ERREUR" ? "red" : a.niveau === "AVERTISSEMENT" ? "amber" : "blue"} /></td>
                  <td className="cell2"><div className="l1 ellipsis selectable" title={a.chemin || ""}>{a.chemin ? short(a.chemin, 80) : "—"}</div><div className="l2 selectable">{a.message}</div></td>
                  <td>{a.chemin && <button className="btn ghost sm" onClick={() => void reveal(a.chemin!)} title="Ouvrir l'emplacement du fichier">{a.action || "Ouvrir le dossier"}</button>}</td>
                </tr>
              ))}
              {!anoms.length && <tr><td colSpan={4} className="muted" style={{ textAlign: "center", padding: 20 }}>Aucune anomalie.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
