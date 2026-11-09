import { useCallback, useEffect, useState } from "react";
import { call, sx, FILIERES, statutTone, type Releve } from "../api";
import { Badge, Icon, Seg, Switch, useToast } from "../components/ui";

interface Ext { count: number; mentions: Record<string, number>; per_year: [string, number][]; rows: Releve[] }
interface J { id: number; horodatage: string; operateur: string; poste: string; action: string; matricule: string | null; detail: string | null }
const MENTIONS: [string, string][] = [["Excellent", "#15803D"], ["Très bien", "#22C55E"], ["Bien", "#1D4ED8"], ["Assez bien", "#93B4F5"], ["Passable", "#F59E0B"], ["Insuffisant", "#B91C1C"]];
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Extraction() {
  const toast = useToast();
  const [years, setYears] = useState<number[]>([]);
  const [fil, setFil] = useState<string | null>(null);
  const [y1, setY1] = useState<number | null>(null);
  const [y2, setY2] = useState<number | null>(null);
  const [statut, setStatut] = useState("tous");
  const [last, setLast] = useState(false);
  const [res, setRes] = useState<Ext | null>(null);
  const [busy, setBusy] = useState(false);
  const [jq, setJq] = useState("");
  const [journal, setJournal] = useState<J[]>([]);

  const scope = `${FILIERES.find((f) => f.code === fil)?.label || "Toutes"} · ${y1 ? `${y1}-${y1 + 1}` : "début"} → ${y2 ? `${y2}-${y2 + 1}` : "aujourd'hui"}`;
  const params = { filiere: fil, year_from: y1, year_to: y2, statut, dernier_niveau: last };

  const run = useCallback(async () => {
    setBusy(true);
    try { setRes(await call<Ext>("extraction", params)); } catch (e) { toast(errMsg(e), { tone: "red" }); } finally { setBusy(false); }
  }, [fil, y1, y2, statut, last]); // eslint-disable-line react-hooks/exhaustive-deps
  const loadJournal = useCallback(async () => {
    setJournal(await call<J[]>("journal", { matricule: jq.trim() || null, since: jq.trim() ? null : new Date().toISOString().slice(0, 10), limit: 500 }));
  }, [jq]);

  useEffect(() => { void call<number[]>("years").then(setYears).catch(() => undefined); void loadJournal(); }, [loadJournal]);
  useEffect(() => { void run(); }, [run]);

  const exportAs = async (kind: "csv" | "xlsx") => {
    if (!res?.count) { toast("Aucune ligne à exporter avec ces filtres.", { tone: "amber" }); return; }
    const path = await sx.saveFile({ title: "Exporter", filters: [kind === "xlsx" ? { name: "Classeur Excel", extensions: ["xlsx"] } : { name: "CSV", extensions: ["csv"] }],
      defaultPath: `Decanat_${fil || "Toutes"}_${statut}_${y1 || "debut"}-${y2 ? y2 + 1 : "fin"}.${kind}` });
    if (!path) return;
    try {
      const r = await call<{ path: string; name: string; count: number }>("extraction_export", { ...params, kind, path, scope });
      toast(`Exporté : ${r.name} (${r.count} lignes) · extraction journalisée`, { ms: 15000, action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
      void loadJournal();
    } catch (e) { toast(`Export impossible : ${errMsg(e)}`, { tone: "red" }); }
  };

  const totalM = Object.values(res?.mentions || {}).reduce((a, b) => a + b, 0) || 1;
  const maxY = Math.max(1, ...(res?.per_year || []).map(([, n]) => n));
  return (
    <div className="page scroll">
      <header className="header"><div className="titles"><div className="eyebrow">Décanat</div><h1 className="h1">Extraction et journal</h1></div></header>
      <section className="card">
        <div className="row wrap">
          <label className="lbl">Filière<select className="field" value={fil ?? ""} onChange={(e) => setFil(e.target.value || null)}>{FILIERES.map((f) => <option key={String(f.code)} value={f.code ?? ""}>{f.label}</option>)}</select></label>
          <label className="lbl">De<select className="field" value={y1 ?? ""} onChange={(e) => setY1(e.target.value ? Number(e.target.value) : null)}><option value="">début</option>{years.map((y) => <option key={y} value={y}>{y}-{y + 1}</option>)}</select></label>
          <label className="lbl">À<select className="field" value={y2 ?? ""} onChange={(e) => setY2(e.target.value ? Number(e.target.value) : null)}><option value="">aujourd'hui</option>{years.map((y) => <option key={y} value={y}>{y}-{y + 1}</option>)}</select></label>
          <div className="col" style={{ gap: 5 }}><span className="lbl">Statut</span><Seg value={statut} options={[["tous", "Tous"], ["admis", "Admis"], ["non_valides", "Non validés"]]} onChange={setStatut} /></div>
          <div style={{ paddingTop: 18 }}><Switch blue checked={last} onChange={setLast} label="Dernier niveau de chaque étudiant" /></div>
          <span className="grow" />
          {busy && <span className="spinner" />}
        </div>
        <div className="row"><span className="h2">{(res?.count || 0).toLocaleString("fr-FR")}</span><span className="muted grow">relevés · {scope}</span>
          <button className="btn secondary" onClick={() => void exportAs("csv")}><Icon name="download" />CSV</button>
          <button className="btn primary" onClick={() => void exportAs("xlsx")}><Icon name="download" />Excel</button></div>
        <div style={{ display: "flex", height: 14, borderRadius: 7, overflow: "hidden", background: "#E8EDF4" }} title="Répartition des mentions">
          {MENTIONS.map(([m, c]) => <div key={m} style={{ width: `${((res?.mentions[m] || 0) * 100) / totalM}%`, background: c }} />)}
        </div>
        <div className="muted tiny">{MENTIONS.map(([m]) => `${m} · ${res?.mentions[m] || 0}`).join("   ")}</div>
        <div className="row" style={{ alignItems: "flex-end", height: 90, gap: 6 }}>
          {(res?.per_year || []).map(([a, n], i, arr) => (
            <div key={a} className="col" style={{ alignItems: "center", gap: 3, flex: 1 }} title={`${a} : ${n}`}>
              <div style={{ height: (n * 64) / maxY, width: "100%", maxWidth: 34, borderRadius: 6, background: i === arr.length - 1 ? "#1D4ED8" : "#93B4F5" }} />
              <span className="tiny muted">{a.length >= 9 ? `${a.slice(2, 4)}-${a.slice(7, 9)}` : a}</span>
            </div>
          ))}
        </div>
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table className="grid">
            <colgroup><col style={{ width: 96 }} /><col /><col style={{ width: 110 }} /><col /><col style={{ width: 80 }} /><col style={{ width: 120 }} /></colgroup>
            <thead><tr><th>Matricule</th><th>Nom &amp; prénom</th><th>Année</th><th>Période</th><th>Moyenne</th><th>Statut</th></tr></thead>
            <tbody>{(res?.rows || []).slice(0, 200).map((r) => (
              <tr key={r.id}><td className="mono small">{r.matricule}</td><td className="ellipsis">{r.nom_complet}</td><td>{r.annee_academique}</td><td className="ellipsis">{r.periode}</td><td>{r.moyenne}</td><td><Badge text={r.statut || "—"} tone={statutTone(r.statut)} /></td></tr>
            ))}</tbody>
          </table>
        </div>
        {(res?.count || 0) > 200 && <div className="muted tiny">200 premières lignes affichées ; l'export contient les {res?.count.toLocaleString("fr-FR")}.</div>}
      </section>
      <section className="card">
        <div className="row"><h2 className="h2 grow">Journal {jq.trim() ? `· ${jq.trim()}` : "· aujourd'hui"}</h2>
          <input className="field" style={{ width: 240 }} placeholder="Matricule (tout l'historique)" value={jq} onChange={(e) => setJq(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void loadJournal()} /></div>
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table className="grid">
            <colgroup><col style={{ width: 140 }} /><col style={{ width: 150 }} /><col style={{ width: 150 }} /><col style={{ width: 96 }} /><col /></colgroup>
            <thead><tr><th>Date</th><th>Opérateur · poste</th><th>Action</th><th>Matricule</th><th>Détail</th></tr></thead>
            <tbody>{journal.map((j) => (
              <tr key={j.id}><td className="small">{j.horodatage.slice(0, 16)}</td><td className="small ellipsis">{j.operateur} · {j.poste}</td><td><Badge text={j.action} tone={j.action.includes("BLOQU") ? "red" : j.action.includes("IMPRESSION") || j.action === "DUPLICATA" ? "violet" : "gray"} /></td>
                <td className="mono small">{j.matricule || ""}</td><td className="small ellipsis selectable" title={j.detail || ""}>{j.detail}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
