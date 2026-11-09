import { useEffect, useState } from "react";
import { call, sx, type Settings, type TextRule } from "../api";
import { useApp } from "../state";
import { Icon, Modal, Seg, Switch, useToast } from "../components/ui";

type Tab = "poste" | "indexation" | "exports" | "corrections";

export function SettingsDialog() {
  const app = useApp();
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("poste");
  const [v, setV] = useState<Settings | null>(app.settings);
  const [printers, setPrinters] = useState<{ name: string; isDefault: boolean }[]>([]);
  const [logs, setLogs] = useState("");

  useEffect(() => {
    void sx.printers().then(setPrinters).catch(() => undefined);
    void sx.appInfo().then((i) => setLogs(i.logs)).catch(() => undefined);
    void call<Settings>("settings_get").then(setV).catch(() => undefined);
  }, []);
  if (!v) return null;
  const set = <K extends keyof Settings>(k: K, val: Settings[K]) => setV({ ...v, [k]: val });

  const save = async () => {
    try {
      const s = await app.saveSettings(v);
      if (s.ui_scale) await sx.setZoom(s.ui_scale / 100);
      toast("Réglages enregistrés.");
      app.closeSettings();
    } catch (e) { toast(`Enregistrement impossible : ${(e as Error).message}`, { tone: "red" }); }
  };

  const rules = v.text_rules || [];
  const setRule = (i: number, patch: Partial<TextRule>) => set("text_rules", rules.map((r, k) => (k === i ? { ...r, ...patch } : r)));

  return (
    <Modal label="Réglages" onClose={app.closeSettings} width={900}>
      <div className="row"><h2 className="h2 grow">Réglages du poste</h2>
        <Seg value={tab} options={[["poste", "Poste"], ["indexation", "Indexation"], ["exports", "Exports"], ["corrections", "Corrections Word"]]} onChange={setTab} />
        <button className="btn ghost icon" onClick={app.closeSettings} aria-label="Fermer"><Icon name="x" /></button></div>
      <div className="col" style={{ overflow: "auto", minHeight: 360, maxHeight: "62vh", paddingRight: 4 }}>
        {tab === "poste" && <>
          <div className="row wrap">
            <label className="lbl grow">Opérateur<input className="field" value={v.operateur} onChange={(e) => set("operateur", e.target.value)} /></label>
            <label className="lbl">Poste<input className="field" value={v.poste} onChange={(e) => set("poste", e.target.value)} /></label>
          </div>
          <label className="lbl">Imprimante
            <select className="field" value={v.printer_name || ""} onChange={(e) => set("printer_name", e.target.value)}>
              <option value="">Imprimante par défaut de Windows</option>
              {printers.map((p) => <option key={p.name} value={p.name}>{p.name}{p.isDefault ? " (par défaut)" : ""}</option>)}
            </select></label>
          <label className="lbl">Commande d'impression (facultatif, ex. SumatraPDF ; {"{file}"} et {"{printer}"} sont remplacés)
            <input className="field mono" value={v.print_command || ""} onChange={(e) => set("print_command", e.target.value)} placeholder='"C:\Outils\SumatraPDF.exe" -print-to-default -silent "{file}"' /></label>
          <div className="row wrap" style={{ gap: 18 }}>
            <Switch blue checked={v.duplex} onChange={(x) => set("duplex", x)} label="Recto-verso pour les relevés de plusieurs pages" />
            <Switch blue checked={v.delivery_ref_stamp} onChange={(x) => set("delivery_ref_stamp", x)} label="Numéro de délivrance en pied de page" />
          </div>
          <div className="row wrap" style={{ gap: 18 }}>
            <label className="lbl">Seuil de tolérance aux fautes (%)<input className="field" type="number" min={50} max={100} value={v.fuzzy_threshold} onChange={(e) => set("fuzzy_threshold", Number(e.target.value))} /></label>
            <label className="lbl">Résultats au plus<input className="field" type="number" min={20} max={2000} value={v.max_results} onChange={(e) => set("max_results", Number(e.target.value))} /></label>
            <label className="lbl">Taille de l'interface (Ctrl+Plus / Ctrl+Moins / Ctrl+0)
              <select className="field" value={v.ui_scale || 100} onChange={(e) => set("ui_scale", Number(e.target.value))}>
                {(app.info?.ui_scales || [80, 90, 100, 110, 125, 150]).map((x) => <option key={x} value={x}>{x} %{x === 100 ? " (normale)" : ""}</option>)}
              </select></label>
          </div>
          <div className="row wrap" style={{ gap: 18 }}>
            <Switch blue checked={v.ui_sounds} onChange={(x) => set("ui_sounds", x)} label="Sons discrets" />
            <Switch blue checked={v.ui_animations} onChange={(x) => set("ui_animations", x)} label="Petites animations" />
          </div>
          <div className="soft row"><Icon name="info" /><span className="grow small">Journal des incidents (à transmettre à la Cellule informatique en cas de problème) : <span className="mono selectable">{logs}</span></span>
            <button className="btn ghost sm" onClick={() => logs && void sx.openPath(logs)}>Ouvrir</button></div>
        </>}
        {tab === "indexation" && <>
          <label className="lbl">Dossier racine des archives<div className="row">
            <input className="field grow" value={v.source_root} onChange={(e) => set("source_root", e.target.value)} />
            <button className="btn ghost" onClick={async () => { const d = await sx.pickFolder({ defaultPath: v.source_root || undefined }); if (d) set("source_root", d); }}>Parcourir…</button></div></label>
          <label className="lbl">Exclusions (un dossier ou un motif par ligne : sous-dossiers à ne pas indexer)
            <textarea className="field mono" rows={6} value={(v.exclusions || []).join("\n")} onChange={(e) => set("exclusions", e.target.value.split("\n").map((x) => x.trim()).filter(Boolean))} /></label>
          <div className="row wrap" style={{ gap: 18 }}>
            <label className="lbl">Processus d'indexation (0 = automatique)<input className="field" type="number" min={0} max={16} value={v.index_workers} onChange={(e) => set("index_workers", Number(e.target.value))} /></label>
            <Switch blue checked={v.ocr_enabled} onChange={(x) => set("ocr_enabled", x)} label="OCR des scans (Tesseract)" />
            <Switch blue checked={v.differential} onChange={(x) => set("differential", x)} label="Indexation différentielle" />
            <Switch blue checked={v.docx_convert_at_index} onChange={(x) => set("docx_convert_at_index", x)} label="Produire les aperçus Word pendant l'indexation" />
          </div>
        </>}
        {tab === "exports" && <>
          <label className="lbl">Dossier des exports (Relevés, Duplicatas, Cursus, Demandes, Extractions, Vérifications)<div className="row">
            <input className="field grow" value={v.exports_root} onChange={(e) => set("exports_root", e.target.value)} placeholder="Documents\Exports Scolarix" />
            <button className="btn ghost" onClick={async () => { const d = await sx.pickFolder({ defaultPath: v.exports_root || undefined }); if (d) set("exports_root", d); }}>Parcourir…</button></div></label>
          <label className="lbl">Conservation des copies recopiables (jours, 0 = toujours)<input className="field" type="number" min={0} value={v.exports_retention_days} onChange={(e) => set("exports_retention_days", Number(e.target.value))} /></label>
          <div className="muted small">Le registre des délivrances et les extractions ne sont jamais purgés.</div>
        </>}
        {tab === "corrections" && <>
          <Switch blue checked={v.text_rules_enabled} onChange={(x) => set("text_rules_enabled", x)} label="Corriger le texte des relevés Word à l'aperçu et à l'impression (le fichier d'origine n'est jamais modifié)" />
          {rules.map((r, i) => (
            <div key={i} className="soft col" style={{ gap: 8 }}>
              <div className="row"><Switch blue checked={r.actif !== false} onChange={(x) => setRule(i, { actif: x })} label="" />
                <input className="field grow" placeholder="Description" value={r.description || ""} onChange={(e) => setRule(i, { description: e.target.value })} />
                <Switch blue checked={!!r.regex} onChange={(x) => setRule(i, { regex: x })} label="Regex" />
                <button className="btn ghost sm" aria-label="Supprimer la règle" onClick={() => set("text_rules", rules.filter((_, k) => k !== i))}><Icon name="trash" size={14} /></button></div>
              <div className="row wrap">
                <label className="lbl grow">Chercher<input className="field mono" value={r.chercher} onChange={(e) => setRule(i, { chercher: e.target.value })} /></label>
                <label className="lbl grow">Remplacer par<input className="field mono" value={r.remplacer} onChange={(e) => setRule(i, { remplacer: e.target.value })} /></label>
                <label className="lbl grow">Sauf si déjà présent<input className="field mono" value={r.sauf_si || ""} onChange={(e) => setRule(i, { sauf_si: e.target.value })} /></label>
              </div>
              <div className="row wrap">
                <label className="lbl">Police<input className="field" style={{ width: 160 }} value={r.police || ""} placeholder="celle du texte" onChange={(e) => setRule(i, { police: e.target.value || null })} /></label>
                <label className="lbl">Taille<input className="field" style={{ width: 80 }} type="number" min={5} max={30} value={r.taille ?? ""} onChange={(e) => setRule(i, { taille: e.target.value ? Number(e.target.value) : null })} /></label>
                <label className="lbl">Gras<select className="field" value={r.gras == null ? "" : r.gras ? "1" : "0"} onChange={(e) => setRule(i, { gras: e.target.value === "" ? null : e.target.value === "1" })}><option value="">inchangé</option><option value="1">oui</option><option value="0">non</option></select></label>
                <label className="lbl">Italique<select className="field" value={r.italique == null ? "" : r.italique ? "1" : "0"} onChange={(e) => setRule(i, { italique: e.target.value === "" ? null : e.target.value === "1" })}><option value="">inchangé</option><option value="1">oui</option><option value="0">non</option></select></label>
              </div>
            </div>
          ))}
          <button className="btn secondary" style={{ alignSelf: "flex-start" }} onClick={() => set("text_rules", [...rules, { actif: true, chercher: "", remplacer: "", regex: false }])}><Icon name="plus" />Ajouter une règle</button>
        </>}
      </div>
      <div className="foot"><button className="btn ghost" onClick={app.closeSettings}>Annuler</button><button className="btn primary" onClick={() => void save()} data-testid="settings-save">Enregistrer</button></div>
    </Modal>
  );
}
