import { useCallback, useEffect, useState } from "react";
import { call, plural, sx, type Demande } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Chip, Icon, Seg, useDialogs, useToast } from "../components/ui";
import { DemandeDialog, STATUT_AIDE, STATUT_TONE } from "./DemandeDialog";

const FILTRES: [string, string, string[] | null][] = [
  ["a_traiter", "À traiter", ["EN COURS", "PRÊTE"]], ["a_remettre", "À remettre", ["IMPRIMÉE"]],
  ["remises", "Remises", ["REMISE"]], ["toutes", "Toutes", null],
];
const NEXT: Record<string, string> = { "EN COURS": "Imprimer tout", "PRÊTE": "Imprimer tout", "IMPRIMÉE": "Remettre", "REMISE": "—", "ANNULÉE": "—" };

export function DemandesPage() {
  const app = useApp();
  const toast = useToast();
  const { prompt } = useDialogs();
  const [filtre, setFiltre] = useState("a_traiter");
  const [rows, setRows] = useState<Demande[]>([]);
  const [all, setAll] = useState<Demande[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [q, setQ] = useState("");

  const load = useCallback(async () => {
    const statuts = FILTRES.find((f) => f[0] === filtre)![2];
    const [r, a] = await Promise.all([call<Demande[]>("demandes_list", { statuts }), call<Demande[]>("demandes_list", {})]);
    setRows(r); setAll(a);
  }, [filtre]);
  useEffect(() => { void load().catch((e) => toast(String(e.message), { tone: "red" })); }, [load, app.demandeVersion, toast]);

  const create = async () => {
    const who = await prompt("Nouvelle demande", "Demandeur (l'étudiant lui-même, un parent, un mandataire…) :", "L'étudiant lui-même");
    if (who === null) return;
    const d = await call<{ demande: Demande }>("demande_create", { demandeur: who.trim() });
    app.setActiveDemande(d.demande.id);
    app.bumpDemandes();
    toast(`Demande ${d.demande.numero} ouverte : ajoutez les relevés au guichet (Ctrl+D).`, { action: { label: "Aller au guichet", run: () => app.go("guichet") } });
  };
  useShortcuts({ "ctrl+n": () => void create() }, app.page === "demandes" && !open);

  const needle = q.trim().toLowerCase();
  const shown = needle ? rows.filter((d) => [d.numero, d.matricule, d.nom, d.demandeur, d.motif].map((x) => x || "").join(" ").toLowerCase().includes(needle)) : rows;
  const count = (st: string[]) => all.filter((d) => st.includes(d.statut)).length;
  const today = new Date().toISOString().slice(0, 10);
  return (
    <>
      <header className="header">
        <div className="titles"><div className="eyebrow">Suivi</div><h1 className="h1">Demandes de relevés</h1></div>
        <button className="btn primary" onClick={() => void create()}><Icon name="plus" />Nouvelle demande <span className="kbd">Ctrl+N</span></button>
      </header>
      <div className="stats-grid">
        {([["folder", "blue", "À traiter", count(["EN COURS", "PRÊTE"])], ["printer", "amber", "À remettre", count(["IMPRIMÉE"])],
          ["check", "green", "Remises aujourd'hui", all.filter((d) => d.statut === "REMISE" && (d.maj_le || "").startsWith(today)).length],
          ["layers", "violet", "Total", all.length]] as const).map(([icon, tone, label, n]) => (
          <div key={label} className="card kpi" style={{ flexDirection: "row", padding: 14 }}><Chip icon={icon} tone={tone} /><div><div className="eyebrow">{label}</div><div className="kpi"><span className="v">{n}</span></div></div></div>
        ))}
      </div>
      <section className="card grow">
        <div className="row"><Seg value={filtre} options={FILTRES.map((f) => [f[0], f[1]] as [string, string])} onChange={setFiltre} label="Filtre" />
          <input className="field grow" style={{ maxWidth: 360 }} placeholder="Filtrer : numéro, matricule, nom, demandeur…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filtrer les demandes" data-testid="demandes-filter" /><span className="grow" />
          <span className="muted small">Double-clic : ouvrir · « Au guichet » : la rendre active pour y ajouter des relevés</span></div>
        <div className="table-wrap">
          <table className="grid">
            <colgroup><col style={{ width: 130 }} /><col style={{ width: 120 }} /><col /><col /><col style={{ width: 70 }} /><col style={{ width: 200 }} /><col style={{ width: 130 }} /><col style={{ width: 110 }} /></colgroup>
            <thead><tr><th>N° de demande</th><th>Reçue le</th><th>Étudiant</th><th>Demandeur · motif</th><th>Relevés</th><th>Statut</th><th>Prochaine étape</th><th /></tr></thead>
            <tbody>
              {shown.map((d) => (
                <tr key={d.id} onDoubleClick={() => setOpen(d.id)}>
                  <td className="mono small">{d.numero}</td>
                  <td className="small">{(d.creee_le || "").slice(0, 16).replaceAll("-", "/")}</td>
                  <td className="cell2"><div className="l1 ellipsis">{d.nom || "—"}</div><div className="l2 mono">{d.matricule || ""}</div></td>
                  <td className="cell2"><div className="l1 ellipsis">{d.demandeur || "—"}</div><div className="l2 ellipsis">{d.motif || ""}</div></td>
                  <td>{d.n}</td>
                  <td><Badge text={`${d.statut} — ${STATUT_AIDE[d.statut] || ""}`} tone={STATUT_TONE[d.statut] || "gray"} /></td>
                  <td className="small">{NEXT[d.statut]}</td>
                  <td><div className="row" style={{ gap: 4 }}>
                    <button className="btn ghost sm" onClick={() => setOpen(d.id)}>Ouvrir</button>
                    {["EN COURS", "PRÊTE", "IMPRIMÉE"].includes(d.statut) && <button className="btn ghost sm" title="Rendre active au guichet" onClick={() => { app.setActiveDemande(d.id); app.go("guichet"); }}>Au guichet</button>}
                    {d.dossier && <button className="btn ghost sm icon" title="Ouvrir le dossier préparé (bordereau, relevés numérotés, A_IMPRIMER_TOUT)" aria-label="Ouvrir le dossier" onClick={() => void sx.reveal(d.dossier!)}><Icon name="folder" size={14} /></button>}
                  </div></td>
                </tr>
              ))}
              {!shown.length && <tr><td colSpan={8} className="muted" style={{ textAlign: "center", padding: 24 }}>{needle && rows.length ? `Aucune demande ne correspond à « ${q.trim()} ».` : `Aucune demande ${FILTRES.find((f) => f[0] === filtre)![1].toLowerCase()}. Au guichet, « Ajouter » (Ctrl+D) ouvre une demande pour l'étudiant affiché.`}</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="muted tiny">{plural(shown.length, "demande")}{needle ? ` sur ${rows.length}` : ""}</div>
      </section>
      {open && <DemandeDialog did={open} onClose={() => { setOpen(null); app.bumpDemandes(); void load(); }} />}
    </>
  );
}
