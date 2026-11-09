import { useState } from "react";
import { call, sx, statutTone, type Releve } from "../api";
import { Badge, Chip, Icon, useToast } from "../components/ui";
import { PdfView } from "../components/PdfView";

interface Ident { delivered: Record<string, any> | null; ident: Record<string, any>; matricule: string | null; releves: Releve[]; best: number | null }
interface Cmp { original: Releve; rows: { champ: string; soumis: string; original: string; conforme: boolean }[]; conformes: number; total: number }
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Verification() {
  const toast = useToast();
  const [path, setPath] = useState<string | null>(null);
  const [ident, setIdent] = useState<Ident | null>(null);
  const [mat, setMat] = useState("");
  const [rels, setRels] = useState<Releve[]>([]);
  const [orig, setOrig] = useState<number | null>(null);
  const [cmp, setCmp] = useState<Cmp | null>(null);
  const [busy, setBusy] = useState("");
  const [demandeur, setDemandeur] = useState("");
  const [reference, setReference] = useState("");

  const compare = async (p: string, id: number) => {
    setBusy("Comparaison…");
    try { setCmp(await call<Cmp>("verify_compare", { path: p, releve_id: id })); } catch (e) { toast(`Lecture impossible : ${errMsg(e)}`, { tone: "red" }); } finally { setBusy(""); }
  };

  const load = async () => {
    const p = await sx.pickFile({ title: "Document soumis", filters: [{ name: "Documents", extensions: ["pdf", "png", "jpg", "jpeg", "tif", "tiff"] }] });
    if (!p) return;
    setPath(p); setCmp(null); setIdent(null);
    setBusy("Lecture du document soumis…");
    try {
      const r = await call<Ident>("verify_identify", { path: p });
      setIdent(r);
      if (r.delivered) toast(`Fichier identique au document délivré le ${String(r.delivered.horodatage).slice(0, 16).replaceAll("-", "/")} sous la référence ${r.delivered.reference} (${String(r.delivered.nature).toLowerCase()}, matricule ${r.delivered.matricule || "?"}, par ${r.delivered.operateur || "?"}).`, { ms: 20000 });
      else if (p.toLowerCase().endsWith(".pdf")) toast("Ce fichier ne correspond à aucun document délivré par le guichet (modifié, réimprimé puis scanné, ou délivré avant Scolarix) : comparaison du contenu.", { tone: "amber", ms: 12000 });
      if (r.matricule) setMat(r.matricule);
      setRels(r.releves);
      if (r.best) { setOrig(r.best); await compare(p, r.best); }
      else toast("Matricule non reconnu sur le document soumis : saisissez le matricule déclaré puis « Rechercher l'original ».", { tone: "amber", ms: 12000 });
    } catch (e) { toast(errMsg(e), { tone: "red" }); } finally { setBusy(""); }
  };

  const findOriginal = async () => {
    const r = await call<{ results: Releve[] }>("search", { q: mat });
    const list = r.results.filter((x) => x.matricule === r.results[0]?.matricule);
    setRels(list);
    if (!list.length) { toast("Aucun relevé pour ce matricule.", { tone: "amber" }); return; }
    const id = list[list.length - 1].id;
    setOrig(id);
    if (path) await compare(path, id);
  };

  const report = async () => {
    if (!path || !orig) return;
    try {
      const r = await call<{ path: string; name: string }>("verify_report", { path, releve_id: orig, demandeur, reference });
      toast(`Rapport généré : ${r.name}`, { ms: 12000, action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };

  const bad = cmp ? cmp.total - cmp.conformes : 0;
  const o = cmp?.original;
  return (
    <div className="page scroll">
      <header className="header"><div className="titles"><div className="eyebrow">Authenticité</div><h1 className="h1">Vérification d'un relevé soumis</h1></div></header>
      <section className="card">
        <div className="row wrap">
          <button className="btn primary" onClick={() => void load()} data-testid="verify-load"><Icon name="upload" />Charger le document soumis</button>
          <span className="muted small grow ellipsis selectable">{path || "PDF ou image (scan, photo) envoyé par un employeur, une université…"}</span>
          {busy && <span className="row muted small"><span className="spinner" />{busy}</span>}
        </div>
        <div className="row wrap">
          <label className="lbl">Matricule déclaré<input className="field mono" value={mat} onChange={(e) => setMat(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void findOriginal()} data-testid="verify-mat" /></label>
          <button className="btn secondary" style={{ marginTop: 18 }} onClick={() => void findOriginal()}>Rechercher l'original</button>
          <label className="lbl grow">Original indexé
            <select className="field" value={orig ?? ""} onChange={(e) => { const id = Number(e.target.value); setOrig(id); if (path) void compare(path, id); }}>
              <option value="">—</option>
              {rels.map((r) => <option key={r.id} value={r.id}>{r.periode} · {r.annee_academique} · {r.statut || "?"}</option>)}
            </select></label>
        </div>
        {ident?.delivered && <div className="alert t-green"><Icon name="check" size={16} />Registre : document délivré par le guichet, réf. {ident.delivered.reference}.</div>}
      </section>
      {cmp && (
        <section className={`card ${bad ? "t-red" : "t-green"}`} style={{ flexDirection: "row", alignItems: "center", border: "none" }} data-testid="verdict">
          <Chip icon={bad ? "alert" : "check"} tone={bad ? "red" : "green"} size={48} />
          <div className="grow"><h2 className="h2">{bad ? `${bad} divergence(s) : document soumis non conforme` : "Document conforme à l'original indexé"}</h2>
            <div className="small">{cmp.conformes}/{cmp.total} éléments identiques · original : {o?.periode} · {o?.annee_academique}</div></div>
        </section>
      )}
      <div className="row" style={{ alignItems: "stretch", gap: 14, minHeight: 420 }}>
        <section className="card" style={{ flex: 1 }}>
          <h2 className="h3">Comparaison champ par champ</h2>
          <div className="table-wrap">
            <table className="grid">
              <colgroup><col style={{ width: 150 }} /><col /><col /><col style={{ width: 90 }} /></colgroup>
              <thead><tr><th>Champ</th><th>Document soumis</th><th>Original</th><th /></tr></thead>
              <tbody>{(cmp?.rows || []).map((r, i) => (
                <tr key={i}><td className="small">{r.champ}</td><td className="selectable small">{r.soumis || "—"}</td><td className="selectable small">{r.original || "—"}</td>
                  <td><Badge text={r.conforme ? "OK" : "DIFFÉRENT"} tone={r.conforme ? "green" : "red"} /></td></tr>
              ))}</tbody>
            </table>
          </div>
          <div className="row wrap">
            <input className="field grow" placeholder="Demandeur (organisme)" value={demandeur} onChange={(e) => setDemandeur(e.target.value)} />
            <input className="field" placeholder="Sa référence" value={reference} onChange={(e) => setReference(e.target.value)} />
            <button className="btn secondary" disabled={!cmp} onClick={() => void report()}><Icon name="doc" />Rapport PDF</button>
          </div>
        </section>
        <section className="card" style={{ flex: 1 }}>
          <div className="row"><h2 className="h3 grow">Original indexé</h2>{o && <Badge text={o.statut || "—"} tone={statutTone(o.statut)} />}</div>
          <PdfView file={o?.preview_pdf || null} first={o?.numero_page || 1} last={o?.page_fin || 1} version={String(o?.id)} />
        </section>
      </div>
    </div>
  );
}
