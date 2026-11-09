import { useCallback, useEffect, useState } from "react";
import { call, sx, plural, statutTone, type DemandeFull, type Tone } from "../api";
import { Badge, Chip, Icon, Modal, Switch, play, useDialogs, useToast } from "../components/ui";
import { isCancelled, useOperation } from "../operation";

export const STATUT_TONE: Record<string, Tone> = { "EN COURS": "blue", "PRÊTE": "violet", "IMPRIMÉE": "amber", "REMISE": "green", "ANNULÉE": "gray" };
export const STATUT_AIDE: Record<string, string> = { "EN COURS": "à préparer", "PRÊTE": "dossier prêt, à imprimer", "IMPRIMÉE": "à remettre", "REMISE": "remise au demandeur", "ANNULÉE": "annulée" };
const NEXT: Record<string, string> = {
  "EN COURS": "Étape suivante : « Imprimer tout » (le dossier est préparé et contrôlé au passage).",
  "PRÊTE": "Étape suivante : « Imprimer tout », puis remettre les documents.",
  "IMPRIMÉE": "Étape suivante : remettre les documents et « Marquer remise ».",
  "REMISE": "Demande terminée.", "ANNULÉE": "Demande annulée.",
};
export const MOTIFS = ["Inscription / poursuite d'études", "Concours", "Emploi / stage", "Équivalence / études à l'étranger", "Bourse", "Dossier personnel", "Autre"];
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function DemandeDialog({ did, onClose }: { did: number; onClose: () => void }) {
  const toast = useToast();
  const { ask, prompt } = useDialogs();
  const [d, setD] = useState<DemandeFull | null>(null);
  const [busy, setBusy] = useState<"" | "prepare" | "print">("");
  const opn = useOperation();
  const [bordereau, setBordereau] = useState(true);
  const [demandeur, setDemandeur] = useState("");
  const [motif, setMotif] = useState("");

  const load = useCallback(async () => {
    const r = await call<DemandeFull>("demande_get", { did });
    setD(r);
    setDemandeur(r.demande.demandeur || "");
    setMotif(r.demande.motif || "");
  }, [did]);
  useEffect(() => { void load().catch((e) => toast(errMsg(e), { tone: "red" })); }, [load, toast]);

  const saveFields = async () => {
    if (!d) return;
    if (demandeur !== (d.demande.demandeur || "") || motif !== (d.demande.motif || ""))
      setD(await call<DemandeFull>("demande_update", { did, fields: { demandeur, motif } }));
  };

  if (!d) return <Modal label="Demande" onClose={onClose} width={960}><div className="empty"><span className="spinner" /></div></Modal>;
  const st = d.demande.statut;
  const editable = st !== "REMISE" && st !== "ANNULÉE" && !busy;
  const pages = d.items.reduce((n, it) => n + (it.releve?.nb_pages || 0), 0);
  const stepIdx = { "EN COURS": 0, "PRÊTE": 1, "IMPRIMÉE": 2, "REMISE": 3, "ANNULÉE": -1 }[st] ?? 0;

  const prepare = async () => {
    await saveFields();
    setBusy("prepare");
    const op = opn.start("demande");
    toast("Préparation du dossier… (contrôle de chaque relevé avant copie)", { tone: "blue", ms: 8000, sound: false, key: "op" });
    try {
      const r = await call<DemandeFull & { prets: unknown[]; bloques: { periode: string; motif: string }[]; pages: number; dossier: string }>("demande_prepare", { did, op });
      setD(r);
      toast(`Dossier prêt : ${plural(r.prets.length, "relevé")}, ${r.pages} page(s)` + (r.bloques.length ? ` — ${r.bloques.length} écarté(s) : ${r.bloques.map((b) => `${b.periode} (${b.motif})`).join(" ; ")}` : ""),
        { tone: r.bloques.length ? "amber" : "green", ms: 15000, key: "op", action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.dossier) } });
    } catch (e) {
      toast(isCancelled(e) ? errMsg(e) : `Préparation impossible : ${errMsg(e)}`, { tone: isCancelled(e) ? "amber" : "red", key: "op" });
    } finally { setBusy(""); opn.finish(); }
  };

  const printAll = async () => {
    await saveFields();
    setBusy("print");
    const op = opn.start("demande");
    toast("Préparation et envoi à l'imprimante…", { tone: "blue", ms: 8000, sound: false, key: "op" });
    try {
      const r = await sx.printDemande({ did, with_bordereau: bordereau, op });
      setD(r);
      play("impression");
      toast(`Envoyé à l'imprimante${bordereau ? " (avec bordereau)" : ""}. Remettez les documents puis « Marquer remise ».`, { sound: false, key: "op" });
    } catch (e) {
      toast(isCancelled(e) ? errMsg(e) : `Impression impossible : ${errMsg(e)}`, { tone: isCancelled(e) ? "amber" : "red", key: "op" });
    } finally { setBusy(""); opn.finish(); }
  };

  const done = async () => {
    const who = await prompt("Remise", "Remis à (nom de la personne) :", demandeur);
    if (who === null) return;
    setD(await call<DemandeFull>("demande_mark", { did, statut: "REMISE", detail: `remis à ${who.trim() || "?"}` }));
    toast("Demande remise et close.");
  };

  const cancel = async () => {
    const i = await ask({ title: "Annuler la demande", text: `Annuler la demande ${d.demande.numero} (${d.demande.nom || "—"}, ${plural(d.items.length, "relevé")}) ?`,
      choices: ["Annuler la demande"], danger: true, details: "Elle restera visible dans « Toutes », marquée annulée." });
    if (i !== 0) return;
    setD(await call<DemandeFull>("demande_mark", { did, statut: "ANNULÉE" }));
  };

  const addCursus = async () => {
    try {
      const r = await call<DemandeFull & { added_count: number }>("demande_add_cursus", { did });
      setD(r);
      toast(`${plural(r.added_count, "relevé ajouté", "relevés ajoutés")} (cursus : dernier relevé validé de chaque semestre).`, { tone: r.added_count ? "green" : "blue" });
    } catch (e) { toast(errMsg(e), { tone: "amber" }); }
  };

  const move = async (idx: number, step: number) => {
    const ids = d.items.map((it) => it.item.releve_id);
    const j = idx + step;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j], ids[idx]];
    setD(await call<DemandeFull>("demande_reorder", { did, releve_ids: ids }));
  };

  const primary = st === "IMPRIMÉE" ? "done" : (st === "EN COURS" || st === "PRÊTE") ? "print" : "";
  return (
    <Modal label={`Demande ${d.demande.numero}`} onClose={onClose} width={980}>
      <div className="row">
        <Chip icon="folder" tone="violet" size={44} />
        <div className="grow">
          <h2 className="h2">Demande {d.demande.numero} · {d.demande.nom || "—"} ({d.demande.matricule || "—"})</h2>
          <div className="muted small">Reçue le {(d.demande.creee_le || "").slice(0, 16).replaceAll("-", "/")}{d.demande.operateur ? ` par ${d.demande.operateur}` : ""} · {d.demande.dossier ? `dossier : ${d.demande.dossier}` : "dossier pas encore préparé"}</div>
        </div>
        <Badge text={`${st} — ${STATUT_AIDE[st] || ""}`} tone={STATUT_TONE[st] || "gray"} big />
        <button className="btn ghost icon" onClick={onClose} aria-label="Fermer"><Icon name="x" /></button>
      </div>
      <div className="steps">
        {["Préparer", "Imprimer", "Remettre"].map((s, i) => (
          <span key={s} style={{ display: "contents" }}>
            {i > 0 && <span className="sep" />}
            <span className={`s${stepIdx > i ? " done" : stepIdx === i ? " cur" : ""}`}><span className="n">{stepIdx > i ? "✓" : i + 1}</span>{s}</span>
          </span>
        ))}
      </div>
      <div className="row wrap">
        <label className="lbl grow">Demandeur<input className="field" value={demandeur} disabled={!editable} onChange={(e) => setDemandeur(e.target.value)} onBlur={() => void saveFields()} /></label>
        <label className="lbl grow">Motif
          <input className="field" list="motifs" value={motif} disabled={!editable} onChange={(e) => setMotif(e.target.value)} onBlur={() => void saveFields()} />
          <datalist id="motifs">{MOTIFS.map((m) => <option key={m} value={m} />)}</datalist>
        </label>
        <div className="col" style={{ gap: 6, paddingTop: 16 }}>
          <Switch checked={!!d.demande.duplicata} label="Duplicata" onChange={async (v) => setD(await call<DemandeFull>("demande_update", { did, fields: { duplicata: v ? 1 : 0 } }))} />
          <Switch blue checked={bordereau} onChange={setBordereau} label="Imprimer le bordereau" />
        </div>
      </div>
      <div className="table-wrap" style={{ maxHeight: 300, border: "1px solid var(--line)" }}>
        <table className="grid">
          <colgroup><col style={{ width: 40 }} /><col /><col style={{ width: 120 }} /><col style={{ width: 100 }} /><col style={{ width: 90 }} /><col style={{ width: 210 }} /><col style={{ width: 110 }} /></colgroup>
          <thead><tr><th>#</th><th>Période</th><th>Année</th><th>Statut</th><th>Pages</th><th>État</th><th /></tr></thead>
          <tbody>
            {d.items.map((it, i) => (
              <tr key={it.item.id}>
                <td className="muted">{i + 1}</td>
                <td className="ellipsis">{it.releve ? it.releve.periode : "relevé retiré de l'index"}</td>
                <td>{it.releve?.annee_academique || "?"}</td>
                <td>{it.releve && <Badge text={it.releve.statut || "—"} tone={statutTone(it.releve.statut)} />}</td>
                <td className="muted">{it.releve?.pages_label.replace("Pages ", "").replace("Page ", "")}</td>
                <td className="small ellipsis" title={it.item.etat || ""}>{it.item.etat ? <Badge text={it.item.etat.startsWith("PRÊT") ? `PRÊT · ${it.item.reference}` : it.item.etat} tone={it.item.etat.startsWith("PRÊT") ? "green" : "red"} /> : <span className="muted">à préparer</span>}</td>
                <td>
                  {editable && <div className="row" style={{ gap: 2 }}>
                    <button className="btn ghost sm" aria-label="Monter" onClick={() => void move(i, -1)}><Icon name="arrow_up" size={14} /></button>
                    <button className="btn ghost sm" aria-label="Descendre" onClick={() => void move(i, 1)}><Icon name="arrow_down" size={14} /></button>
                    <button className="btn ghost sm" aria-label="Retirer" onClick={async () => setD(await call<DemandeFull>("demande_remove", { did, releve_id: it.item.releve_id }))}><Icon name="trash" size={14} /></button>
                  </div>}
                </td>
              </tr>
            ))}
            {!d.items.length && <tr><td colSpan={7} className="muted" style={{ textAlign: "center", padding: 18 }}>Aucun relevé : au guichet, sélectionnez un relevé puis Ctrl+D.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="row">
        <span className="muted small grow">{plural(d.items.length, "relevé")} · {pages} page(s) à imprimer · {NEXT[st]}</span>
        <button className="btn secondary sm" disabled={!editable || !d.demande.matricule || !!busy} onClick={() => void addCursus()} data-testid="demande-cursus"
          title={d.demande.matricule ? "Ajoute le dernier relevé validé de chaque semestre de l'étudiant" : "Ajoutez d'abord un relevé de l'étudiant"}><Icon name="layers" size={14} />Ajouter tout le cursus validé</button>
      </div>
      {busy && (
        <div className="row small" role="status" data-testid="demande-progress">
          <span className="spinner" />
          <span className="grow muted">{opn.cancelling ? "Annulation… (arrêt avant le fichier suivant)" : opn.progress?.message || "Préparation du dossier…"}</span>
          <button className="btn ghost" disabled={opn.cancelling} onClick={() => void opn.cancel()}><Icon name="x" />Arrêter</button>
        </div>
      )}
      <div className="foot" style={{ justifyContent: "space-between" }}>
        <div className="row">
          <button className="btn ghost" disabled={!d.demande.dossier} onClick={() => d.demande.dossier && void sx.reveal(d.demande.dossier)}><Icon name="folder" />Ouvrir le dossier</button>
          <button className="btn danger" disabled={!editable} onClick={() => void cancel()}>Annuler la demande</button>
        </div>
        <div className="row">
          <button className="btn ghost" disabled={!editable || !d.items.length} onClick={() => void prepare()} title="Préparer le dossier (bordereau + relevés numérotés) sans l'envoyer à l'imprimante">
            {busy === "prepare" && <span className="spinner" />}{st === "EN COURS" ? "Préparer sans imprimer" : "Refaire le dossier"}</button>
          <button className={`btn ${primary === "print" ? "primary" : "secondary"}`} disabled={!editable || !d.items.length} onClick={() => void printAll()} data-testid="demande-print">
            {busy === "print" ? <span className="spinner" /> : <Icon name="printer" />}Imprimer tout</button>
          <button className={`btn ${primary === "done" ? "primary" : "secondary"}`} disabled={!(st === "PRÊTE" || st === "IMPRIMÉE") || !!busy} onClick={() => void done()} data-testid="demande-done">
            <Icon name="check" />Marquer remise</button>
        </div>
      </div>
    </Modal>
  );
}
