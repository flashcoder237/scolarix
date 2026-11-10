import { useEffect } from "react";
import { useApp } from "../state";
import { Chip } from "./ui";
import type { Tone } from "../api";

/** Les quatre chiffres de supervision (base, indexation, impressions du jour, anomalies).
 *  Venus du guichet (2.0.x) : ils sont désormais en tête de l'écran Indexation, rafraîchis toutes les 10 s. */
export function StatsRow() {
  const { stats, indexing, refreshStats } = useApp();
  // chiffres tenus à jour (impressions des autres postes, indexation), comme la 1.x (10 s)
  useEffect(() => {
    const t = window.setInterval(() => refreshStats(), 10000);
    return () => window.clearInterval(t);
  }, [refreshStats]);
  const ev = indexing.last;
  const prog = indexing.running && ev?.type === "progress" && ev.total ? { done: ev.done as number, total: ev.total as number } : null;
  const items: [string, Tone, string, string][] = [
    ["shield", "green", "Base d'index", stats ? `${stats.releves.toLocaleString("fr-FR")} relevés · ${stats.etudiants.toLocaleString("fr-FR")} étudiants` : "—"],
    ["database", "blue", "Indexation", prog ? `${Math.floor((prog.done * 100) / prog.total)} % · ${prog.done} / ${prog.total} fichiers` : indexing.running ? "En cours…" : stats?.derniere_indexation ? `Dernière passe : ${stats.derniere_indexation}` : "Jamais lancée"],
    ["printer", "violet", "Imprimés aujourd'hui", stats ? `${stats.imprimes} relevés · ${stats.duplicatas} duplicata(s)` : "—"],
    ["alert", "amber", "Anomalies à revoir", stats ? `${stats.anomalies} fichier(s)` : "—"],
  ];
  return (
    <div className="stats-grid" data-testid="stats-row">
      {items.map(([icon, tone, label, value]) => (
        <div key={label} className="card kpi" style={{ flexDirection: "row", padding: 14 }}>
          <Chip icon={icon} tone={tone} />
          <div className="grow" style={{ minWidth: 0 }}><div className="eyebrow">{label}</div><div className="h3 ellipsis" title={value}>{value}</div>
            {label === "Indexation" && prog && <div className="progress" style={{ marginTop: 6, height: 6 }}><div style={{ width: `${(prog.done * 100) / prog.total}%` }} /></div>}</div>
        </div>
      ))}
    </div>
  );
}
