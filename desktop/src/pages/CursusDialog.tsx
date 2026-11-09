import { useEffect, useMemo, useState } from "react";
import { call, sx, statutTone, type Releve } from "../api";
import { Badge, Chip, Icon, Modal, Switch, play, useToast } from "../components/ui";
import { isCancelled, useOperation } from "../operation";

interface CursusInfo {
  matricule: string; nom: string; releves: Releve[]; missing: string[];
  correlated: { matricule: string; filiere: string; n: number; meme_naissance: boolean }[];
  correlated_releves: Releve[];
}
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const order = (r: Releve) => [r.niveau_num, r.semestre || 0, r.annee_academique || "", (r.session || "").toLowerCase().startsWith("rattrap") ? 1 : 0];
const cmp = (a: Releve, b: Releve) => { const x = order(a), y = order(b); for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1; return 0; };

export function CursusDialog({ matricule, onClose }: { matricule: string; onClose: () => void }) {
  const toast = useToast();
  const [info, setInfo] = useState<CursusInfo | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [link, setLink] = useState(false);
  const [cover, setCover] = useState(true);
  const [dup, setDup] = useState(false);
  const [busy, setBusy] = useState<"" | "export" | "print">("");
  const opn = useOperation();

  useEffect(() => {
    call<CursusInfo>("cursus_info", { matricule })
      .then((i) => { setInfo(i); setChecked(new Set(i.releves.filter((r) => r.selected).map((r) => r.id))); })
      .catch((e) => { toast(errMsg(e), { tone: "red" }); onClose(); });
  }, [matricule]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(() => !info ? [] : [...info.releves, ...(link ? info.correlated_releves : [])].sort(cmp), [info, link]);
  const selected = rows.filter((r) => checked.has(r.id));
  const pages = selected.reduce((n, r) => n + r.nb_pages, 0);

  const run = async (mode: "export" | "print") => {
    if (!info || !selected.length) return;
    setBusy(mode);
    const op = opn.start("cursus");
    toast("Préparation du dossier fusionné…", { tone: "blue", ms: 8000, sound: false, key: "op" });
    try {
      const ids = selected.map((r) => r.id);
      if (mode === "export") {
        const r = await call<{ path: string; name: string; pages: number; missing: string[]; ref: string }>("cursus_build", { matricule: info.matricule, releve_ids: ids, cover, duplicata: dup, mode: "export", op });
        toast(`Exporté (réf. ${r.ref}) : ${r.name} (${r.pages} pages)${r.missing.length ? ` — écartés : ${r.missing.join(", ")}` : ""}`,
          { tone: r.missing.length ? "amber" : "green", ms: 20000, key: "op", action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
      } else {
        const r = await sx.printCursus({ matricule: info.matricule, releve_ids: ids, cover, duplicata: dup, op });
        play("impression");
        toast(`Cursus envoyé à l'imprimante : ${r.pages} pages${r.missing.length ? `. Écartés : ${r.missing.join(", ")}` : ""}`, { tone: r.missing.length ? "amber" : "green", sound: false, ms: 15000, key: "op" });
      }
    } catch (e) {
      toast(errMsg(e), { tone: isCancelled(e) ? "amber" : "red", ms: isCancelled(e) ? 6000 : 30000, key: "op" });
    } finally { setBusy(""); opn.finish(); }
  };

  return (
    <Modal label="Cursus complet" onClose={() => !busy && onClose()} width={860}>
      <div className="row">
        <Chip icon="layers" tone="blue" size={44} />
        <div className="grow">
          <h2 className="h2">Cursus complet · {info?.nom || "…"}</h2>
          <div className="muted small">Matricule <span className="mono">{info?.matricule || matricule}</span> · les relevés cochés sont fusionnés dans l'ordre du cursus, chacun contrôlé avant fusion.</div>
        </div>
        <button className="btn ghost icon" onClick={onClose} aria-label="Fermer" disabled={!!busy}><Icon name="x" /></button>
      </div>
      {info && info.missing.length > 0 && <div className="alert t-amber"><Icon name="alert" size={16} />Périodes absentes du fonds : {info.missing.join(", ")} (signalées sur la page de garde).</div>}
      {info && info.correlated.length > 0 && (
        <Switch blue checked={link} onChange={setLink} label={`Inclure aussi ${info.correlated.map((c) => `${c.matricule} (${c.filiere || "?"}, ${c.n} relevé(s)${c.meme_naissance ? ", même date de naissance" : ""})`).join(", ")} — même personne ?`} />
      )}
      <div className="table-wrap" style={{ maxHeight: 360, border: "1px solid var(--line)" }}>
        {!info ? <div className="empty"><span className="spinner" /></div> : (
          <table className="grid">
            <colgroup><col style={{ width: 44 }} /><col style={{ width: 90 }} /><col /><col style={{ width: 110 }} /><col style={{ width: 110 }} /><col style={{ width: 80 }} /></colgroup>
            <thead><tr><th /><th>Matricule</th><th>Période</th><th>Année</th><th>Statut</th><th>Pages</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} onClick={() => setChecked((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n; })}>
                  <td><input type="checkbox" checked={checked.has(r.id)} readOnly aria-label={`Inclure ${r.periode}`} /></td>
                  <td className="mono small">{r.matricule}</td>
                  <td className="ellipsis">{r.periode}{(r.session || "").toLowerCase().startsWith("rattrap") ? " · rattrapage" : ""}</td>
                  <td>{r.annee_academique}</td>
                  <td><Badge text={r.statut || "—"} tone={statutTone(r.statut)} /></td>
                  <td className="muted">{r.pages_label.replace("Pages ", "").replace("Page ", "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="row wrap">
        <Switch blue checked={cover} onChange={setCover} label="Page de garde" />
        <Switch checked={dup} onChange={setDup} label="Duplicata" />
        <span className="grow muted small">{selected.length} relevé(s) · {pages + (cover ? 1 : 0)} page(s)</span>
      </div>
      <div className="foot">
        {busy && <span className="grow muted small" role="status" data-testid="cursus-progress">
          {opn.cancelling ? "Annulation… (arrêt avant le fichier suivant)" : opn.progress?.message || "Préparation du dossier fusionné…"}</span>}
        {busy && <button className="btn ghost" disabled={opn.cancelling} onClick={() => void opn.cancel()} data-testid="cursus-cancel">
          <Icon name="x" />Annuler</button>}
        <button className="btn secondary" disabled={!selected.length || !!busy} onClick={() => void run("print")} data-testid="cursus-print">
          {busy === "print" ? <span className="spinner" /> : <Icon name="printer" />}Imprimer</button>
        <button className="btn primary" disabled={!selected.length || !!busy} onClick={() => void run("export")} data-testid="cursus-export">
          {busy === "export" ? <span className="spinner" /> : <Icon name="download" />}Fusionner et exporter</button>
      </div>
    </Modal>
  );
}
