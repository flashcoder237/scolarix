import { useEffect, useState } from "react";
import { call, sx, type Settings, type TextRule } from "../api";
import { useApp } from "../state";
import { Icon, Modal, Seg, Switch, useDebounced, useDialogs, useToast } from "../components/ui";

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
    } catch (e) {
      const msg = (e as Error).message || String(e);
      if (/Corrections Word/.test(msg)) setTab("corrections");      // règle invalide : on montre où elle est
      toast(`Enregistrement impossible : ${msg}`, { tone: "red", ms: 10000 });
    }
  };

  const rules = v.text_rules || [];

  return (
    <Modal label="Réglages" onClose={app.closeSettings} width={900}>
      <div className="row"><h2 className="h2 grow">Réglages du poste</h2>
        <Seg value={tab} options={[["poste", "Poste"], ["indexation", "Indexation"], ["exports", "Exports"], ["corrections", "Remplacements (Word)"]]} onChange={setTab} />
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
        {tab === "corrections" && <Corrections enabled={v.text_rules_enabled} rules={rules} onEnabled={(x) => set("text_rules_enabled", x)} onRules={(r) => set("text_rules", r)} />}
      </div>
      <div className="foot"><button className="btn ghost" onClick={app.closeSettings}>Annuler</button><button className="btn primary" onClick={() => void save()} data-testid="settings-save">Enregistrer</button></div>
    </Modal>
  );
}

// ------------------------------------------------------------------ remplacements dans les relevés Word
const FONTS = ["Arial", "Arial Narrow", "Book Antiqua", "Bookman Old Style", "Calibri", "Cambria", "Century Gothic",
  "Garamond", "Georgia", "Segoe UI", "Tahoma", "Times New Roman", "Verdana"];
const SAMPLE = "Tel  : 33 01 41 39  / 33 01 41 29.  RELEVE DE NOTES /TRANSCRIPT   FMSP/VDAAP/VDSSSE/SSSE";
const tri = (x: boolean | null | undefined) => (x == null ? "" : x ? "1" : "0");
const untri = (x: string) => (x === "" ? null : x === "1");

function Corrections({ enabled, rules, onEnabled, onRules }: {
  enabled: boolean; rules: TextRule[]; onEnabled: (x: boolean) => void; onRules: (r: TextRule[]) => void;
}) {
  const { ask } = useDialogs();
  const [sample, setSample] = useState(SAMPLE);
  const [result, setResult] = useState<{ text?: string; count?: number; error?: string }>({});
  const [open, setOpen] = useState<number | null>(rules.length ? 0 : null);
  const probe = useDebounced(JSON.stringify([rules, sample]), 250);
  useEffect(() => {
    let alive = true;
    call<{ text: string; count: number }>("rules_test", { rules, text: sample })
      .then((r) => alive && setResult(r))
      .catch((e) => alive && setResult({ error: (e as Error).message || String(e) }));
    return () => { alive = false; };
  }, [probe]);   // eslint-disable-line react-hooks/exhaustive-deps

  const setRule = (i: number, patch: Partial<TextRule>) => onRules(rules.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  const move = (i: number, d: number) => {
    const j = i + d;
    if (j < 0 || j >= rules.length) return;
    const next = [...rules];
    [next[i], next[j]] = [next[j], next[i]];
    onRules(next);
    setOpen(j);
  };
  const add = () => { onRules([...rules, { actif: true, description: "Nouvelle règle", chercher: "", remplacer: "", regex: false }]); setOpen(rules.length); };
  const remove = async (i: number) => {
    const r = rules[i];
    if (r.chercher.trim()) {
      const k = await ask({ title: "Supprimer la règle", text: `« ${r.description || r.chercher} » ne sera plus appliquée.`, details: "La suppression ne devient définitive qu'avec « Enregistrer ».", choices: ["Supprimer"], danger: true });
      if (k !== 0) return;
    }
    onRules(rules.filter((_, k) => k !== i));
    setOpen(null);
  };
  const restore = async () => {
    const k = await ask({ title: "Rétablir les règles d'origine", text: "Les règles actuelles seront remplacées par les 3 règles livrées avec Scolarix (ancien téléphone, « Ref N° », référence des services).", details: "Rien n'est perdu tant que vous n'avez pas cliqué sur « Enregistrer ».", choices: ["Rétablir"] });
    if (k !== 0) return;
    onRules(await call<TextRule[]>("rules_defaults"));
    setOpen(0);
  };

  return (
    <div className="col" style={{ gap: 10 }} data-testid="rules">
      <Switch blue checked={enabled} onChange={onEnabled} label="Appliquer ces remplacements aux relevés Word (aperçu, impression, export, cursus)" />
      <div className="muted small">Les remplacements sont faits sur une COPIE au moment de produire l'aperçu : le fichier d'origine n'est jamais modifié, et les PDF/scans ne sont pas concernés. Les règles s'appliquent dans l'ordre de la liste.</div>

      {rules.map((r, i) => {
        const isOpen = open === i;
        const style = [r.police, r.taille ? `${r.taille} pt` : "", r.gras == null ? "" : r.gras ? "gras" : "non gras", r.italique == null ? "" : r.italique ? "italique" : "droit"].filter(Boolean).join(" · ");
        return (
          <div key={i} className="soft col" style={{ gap: 8, opacity: r.actif === false ? 0.6 : 1 }} data-testid={`rule-${i}`}>
            <div className="row">
              <Switch blue checked={r.actif !== false} onChange={(x) => setRule(i, { actif: x })} label="" title="Règle active" />
              <button className="btn ghost" style={{ flex: "1 1 0", minWidth: 0, justifyContent: "flex-start", textAlign: "left" }} onClick={() => setOpen(isOpen ? null : i)} aria-expanded={isOpen} title={`${r.chercher} → ${r.remplacer || "(supprimé)"}`}>
                <Icon name={isOpen ? "chevron_down" : "chevron_right"} size={14} />
                <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}><b>{r.description || "Règle sans nom"}</b>
                  <span className="muted small"> — {r.chercher ? <span className="mono">{r.chercher.length > 28 ? r.chercher.slice(0, 28) + "…" : r.chercher}</span> : "rien à chercher"} → {r.remplacer ? <span className="mono">{r.remplacer.length > 28 ? r.remplacer.slice(0, 28) + "…" : r.remplacer}</span> : "supprimé"}{style ? ` · ${style}` : ""}</span></span>
              </button>
              <button className="btn ghost sm" aria-label="Monter" title="Monter" disabled={i === 0} onClick={() => move(i, -1)}><Icon name="arrow_up" size={14} /></button>
              <button className="btn ghost sm" aria-label="Descendre" title="Descendre" disabled={i === rules.length - 1} onClick={() => move(i, 1)}><Icon name="arrow_down" size={14} /></button>
              <button className="btn ghost sm" aria-label="Supprimer la règle" title="Supprimer la règle" onClick={() => void remove(i)}><Icon name="trash" size={14} /></button>
            </div>
            {isOpen && <>
              <label className="lbl">Description<input className="field" value={r.description || ""} onChange={(e) => setRule(i, { description: e.target.value })} /></label>
              <div className="row wrap">
                <label className="lbl grow">Chercher<input className="field mono" value={r.chercher} onChange={(e) => setRule(i, { chercher: e.target.value })} data-testid={`rule-${i}-chercher`} /></label>
                <label className="lbl grow">Remplacer par (vide = supprimer)<input className="field mono" value={r.remplacer} onChange={(e) => setRule(i, { remplacer: e.target.value })} data-testid={`rule-${i}-remplacer`} /></label>
              </div>
              <div className="row wrap">
                <label className="lbl grow" title="La règle est ignorée pour un en-tête qui contient déjà ce texte (un relevé déjà corrigé à la main ne reçoit pas un second « Ref N° »).">Sauf si déjà présent (facultatif)
                  <input className="field mono" value={r.sauf_si || ""} onChange={(e) => setRule(i, { sauf_si: e.target.value })} /></label>
                <Switch blue checked={!!r.regex} onChange={(x) => setRule(i, { regex: x })} label="Expression régulière" title="Désactivé : texte simple, les espaces acceptent n'importe quel nombre d'espaces." />
              </div>
              <div className="eyebrow">Mise en forme du texte de remplacement</div>
              <div className="row wrap">
                <label className="lbl">Police<input className="field" list="scolarix-fonts" style={{ width: 190 }} value={r.police || ""} placeholder="police d'origine" onChange={(e) => setRule(i, { police: e.target.value || null })} /></label>
                <label className="lbl">Taille (pt)<input className="field" style={{ width: 96 }} type="number" min={5} max={30} step={0.5} value={r.taille ?? ""} placeholder="d'origine" onChange={(e) => setRule(i, { taille: e.target.value ? Number(e.target.value) : null })} /></label>
                <label className="lbl">Gras<select className="field" value={tri(r.gras)} onChange={(e) => setRule(i, { gras: untri(e.target.value) })}><option value="">comme l'original</option><option value="1">oui</option><option value="0">non</option></select></label>
                <label className="lbl">Italique<select className="field" value={tri(r.italique)} onChange={(e) => setRule(i, { italique: untri(e.target.value) })}><option value="">comme l'original</option><option value="1">oui</option><option value="0">non</option></select></label>
                <label className="lbl" title="« Texte ajouté » : seul ce qui change est mis en forme (« Ref N° » ajouté devant le titre ne réduit pas le titre). « Tout le texte » : pour changer la police ou la taille d'un texte existant (le remplacer par lui-même).">Appliquer
                  <select className="field" value={r.style_sur === "tout" ? "tout" : "ajout"} onChange={(e) => setRule(i, { style_sur: e.target.value === "tout" ? "tout" : null })}>
                    <option value="ajout">au texte ajouté seulement</option><option value="tout">à tout le texte de remplacement</option></select></label>
              </div>
              <div style={{ background: "#fff", color: "#0F172A", border: "1px solid var(--line)", borderRadius: 10, padding: "6px 12px", minHeight: 40, display: "flex", alignItems: "center",
                fontFamily: r.police || undefined, fontSize: `${Math.min(r.taille || 11, 28) * 1.333}px`, fontWeight: r.gras ? 700 : 400, fontStyle: r.italique ? "italic" : "normal" }}>
                {r.remplacer || <span className="muted small" style={{ fontFamily: "inherit" }}>(suppression : aucun texte de remplacement)</span>}
              </div>
            </>}
          </div>
        );
      })}
      <datalist id="scolarix-fonts">{FONTS.map((f) => <option key={f} value={f} />)}</datalist>

      <div className="row wrap">
        <button className="btn secondary" onClick={add} data-testid="rule-add"><Icon name="plus" />Ajouter une règle</button>
        <button className="btn ghost" onClick={() => void restore()} data-testid="rules-restore">Rétablir les règles d'origine</button>
      </div>

      <div className="col" style={{ gap: 6 }}>
        <label className="lbl">Essai : tapez ou collez un texte d'en-tête pour voir le résultat des règles (rien n'est enregistré)
          <input className="field mono" value={sample} onChange={(e) => setSample(e.target.value)} data-testid="rules-sample" /></label>
        <div className="selectable mono small" data-testid="rules-result" role="status"
          style={{ background: result.error ? "var(--red-soft, #FEE2E2)" : "#0F172A", color: result.error ? "#991B1B" : "#E2E8F0", borderRadius: 12, padding: "8px 12px", fontWeight: 500 }}>
          {result.error ? `⚠ ${result.error}` : result.text === undefined ? "…" : `Résultat (${result.count} remplacement(s)) : ${result.text}`}
        </div>
      </div>
      <div className="muted small">Toute modification refait les aperçus à la consultation suivante. Une taille plus petite évite d'allonger l'en-tête.</div>
    </div>
  );
}
