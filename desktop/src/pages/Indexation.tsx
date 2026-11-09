import { useEffect, useState } from "react";
import { call, sx } from "../api";
import { useApp } from "../state";
import { Badge, Chip, Icon, Seg, Switch, useToast } from "../components/ui";

interface Anomalie { id: number; horodatage: string; niveau: string; chemin: string | null; message: string; action: string | null }
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const short = (p: string, n = 90) => (p.length > n ? "…" + p.slice(-n) : p);

export function Indexation() {
  const app = useApp();
  const toast = useToast();
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
        if (n.status === "EN COURS" || n.status === "ARRÊT…") { n.status = "ARRÊTÉ"; n.detail = n.detail || "Indexation arrêtée."; }
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
        <div className="row wrap">
          <Switch blue checked={!!s?.differential} onChange={(v) => void app.saveSettings({ differential: v })} label="Seulement les fichiers nouveaux ou modifiés" />
          <Switch blue checked={!!s?.ocr_enabled} onChange={(v) => void app.saveSettings({ ocr_enabled: v })} label="OCR des scans" />
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
      <section className="card">
        <div className="row"><h2 className="h2 grow">Base d'index</h2>
          <span className="muted small">{st ? `${st.releves.toLocaleString("fr-FR")} relevés · ${st.etudiants.toLocaleString("fr-FR")} étudiants · ${st.fichiers.toLocaleString("fr-FR")} fichiers · dernière passe : ${st.derniere_indexation || "jamais"}` : ""}</span></div>
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
                  <td>{a.chemin && <button className="btn ghost sm" onClick={() => void sx.reveal(a.chemin!)}>Voir le fichier</button>}</td>
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
