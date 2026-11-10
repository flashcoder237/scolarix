import { useCallback, useEffect, useRef, useState } from "react";
import { call, sx } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Icon, useDebounced, useToast } from "../components/ui";
import { VirtualTable, type VCol } from "../components/VirtualTable";
import { openInGuichet } from "./Extraction";

// Vue sœur « Journal des actions » de l'écran Extraction : qui a consulté, imprimé ou réédité quel relevé, et quand.
// Toutes les lignes (pages de 500 chargées au défilement, plus de plafond à 1 000), filtres avec effectifs.

interface J { id: number; horodatage: string; operateur: string; poste: string; action: string; matricule: string | null; detail: string | null }
interface Page { total: number; offset: number; rows: J[]; actions?: [string, number][]; operateurs?: [string, number][]; periodes?: Record<string, number> }
const PERIODS: [string, string][] = [["0", "Aujourd'hui"], ["7", "7 derniers jours"], ["30", "30 derniers jours"], ["", "Tout"]];
const ACTION_TONE: Record<string, "blue" | "gray" | "amber" | "green" | "violet" | "red"> = { IMPRESSION: "blue", CONSULTATION: "gray", DUPLICATA: "amber", "EXPORT PDF": "green", EXTRACTION: "violet", CURSUS: "green", VERIFICATION: "red" };
const PAGE = 500;
const nf = (n: number) => n.toLocaleString("fr-FR");
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const tone = (a: string) => (a.includes("BLOQU") || a.includes("ÉCHOU") ? "red" : ACTION_TONE[a] || "gray");

// filtres gardés d'une visite à l'autre (l'écran est démonté quand on le quitte)
const saved = { period: "7", from: "", to: "", excluded: [] as string[], op: "" };

export function JournalView({ q, setQ, setChips }: { q: string; setQ: (q: string) => void; setChips: (c: { key: string; label: string; clear: () => void }[]) => void }) {
  const app = useApp();
  const toast = useToast();
  const [period, setPeriod] = useState(saved.period);
  const [custom, setCustom] = useState(!!(saved.from || saved.to));
  const [from, setFrom] = useState(saved.from);
  const [to, setTo] = useState(saved.to);
  // types d'action DÉCOCHÉS (et non cochés) : un type apparu entre-temps (INDEX VIDÉ…) reste visible
  const [excluded, setExcluded] = useState<string[]>(saved.excluded);
  const [op, setOp] = useState(saved.op);
  const dq = useDebounced(q, 250);
  const [head, setHead] = useState<Page | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const pages = useRef(new Map<number, J[]>());
  const loading = useRef(new Set<number>());
  const gen = useRef(0);
  const [, bump] = useState(0);
  Object.assign(saved, { period, from: custom ? from : "", to: custom ? to : "", excluded, op });

  const filters = { period: custom ? null : period, date_from: custom ? from || null : null, date_to: custom ? to || null : null, sans_actions: excluded.length ? excluded : null, operateur: op || null, q: dq.trim() || null };
  const key = JSON.stringify(filters);
  const load = useCallback(async () => {
    const my = ++gen.current;
    try {
      const r = await call<Page>("journal_view", { ...filters, offset: 0, limit: PAGE });
      if (my !== gen.current) return;
      pages.current = new Map([[0, r.rows]]);
      loading.current = new Set();
      setHead(r);
      setSel(null);
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void load(); }, [load]);

  const fetchPage = (p: number) => {
    if (pages.current.has(p) || loading.current.has(p)) return;
    loading.current.add(p);
    const my = gen.current;
    void call<Page>("journal_view", { ...filters, offset: p * PAGE, limit: PAGE, facets: false }).then((r) => {
      if (my !== gen.current) return;
      pages.current.set(p, r.rows);
      bump((n) => n + 1);
    }).catch(() => loading.current.delete(p));
  };
  const getRow = (i: number) => pages.current.get(Math.floor(i / PAGE))?.[i % PAGE];
  const onRange = (a: number, b: number) => { for (let p = Math.floor(a / PAGE); p <= Math.floor(b / PAGE); p++) fetchPage(p); };

  const periodLabel = custom ? `${from || "début"} → ${to || "aujourd'hui"}` : PERIODS.find(([v]) => v === period)?.[1] || "";
  useEffect(() => {
    const c: { key: string; label: string; clear: () => void }[] = [{ key: "p", label: periodLabel, clear: () => { setCustom(false); setPeriod(""); } }];
    if (excluded.length) c.push({ key: "a", label: excluded.length === 1 ? `sans ${excluded[0]}` : `${excluded.length} types masqués`, clear: () => setExcluded([]) });
    if (op) c.push({ key: "o", label: op, clear: () => setOp("") });
    setChips(c);
  }, [periodLabel, excluded.join("|"), op]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => setChips([]), []); // eslint-disable-line react-hooks/exhaustive-deps

  const allActions = (head?.actions || []).map(([a]) => a);
  const toggle = (a: string) => setExcluded(excluded.includes(a) ? excluded.filter((x) => x !== a) : [...excluded, a]);
  const exportCsv = async () => {
    try {
      const def = await call<string>("suggest_path", { nature: "journal", filename: "Journal.csv" });
      const path = await sx.saveFile({ title: "Exporter le journal", defaultPath: def, filters: [{ name: "CSV", extensions: ["csv"] }] });
      if (!path) return;
      const r = await call<{ path: string; name: string; count: number }>("journal_view_export", { ...filters, path });
      toast(`Journal exporté : ${r.name} (${nf(r.count)} lignes)`, { ms: 12000, action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
    } catch (e) { toast(`Export impossible : ${errMsg(e)}`, { tone: "red" }); }
  };
  useShortcuts({ "ctrl+e": () => void exportCsv() });
  const selRow = sel != null ? getRow(sel) : undefined;

  const cols: VCol<J>[] = [
    { key: "date", label: "Date ↓", width: 128, className: "small nowrap", render: (j) => `${j.horodatage.slice(8, 10)}/${j.horodatage.slice(5, 7)}/${j.horodatage.slice(2, 4)} ${j.horodatage.slice(11, 16)}`, title: (j) => j.horodatage },
    { key: "op", label: "Opérateur · poste", width: 170, className: "small ellipsis", render: (j) => `${j.operateur || "?"} · ${j.poste || "?"}`, title: (j) => `${j.operateur} · ${j.poste}` },
    { key: "action", label: "Action", width: 168, render: (j) => <Badge text={j.action} tone={tone(j.action)} /> },
    { key: "mat", label: "Matricule", width: 96, className: "mono small", render: (j) => j.matricule || "" },
    { key: "detail", label: "Détail", className: "small ellipsis selectable", render: (j) => j.detail || "", title: (j) => j.detail || undefined },
  ];
  const total = head?.total || 0;
  return (
    <div className="xr-body">
      <aside className="card xr-filters" aria-label="Filtres du journal" data-testid="journal-filters">
        <div className="xr-fh">Période</div>
        <div className="xr-list" role="radiogroup" aria-label="Période" data-testid="journal-period">
          {PERIODS.map(([v, l]) => (
            <button key={v} role="radio" aria-checked={!custom && period === v} className={`xr-opt${!custom && period === v ? " on" : ""}`} onClick={() => { setCustom(false); setPeriod(v); }} data-testid={`journal-period-${v || "tout"}`}>
              <span className="rad" /><span className="grow">{l}</span><span className="n">{nf(head?.periodes?.[v] ?? 0)}</span>
            </button>
          ))}
          <button role="radio" aria-checked={custom} className={`xr-opt${custom ? " on" : ""}`} onClick={() => setCustom(true)} data-testid="journal-period-dates"><span className="rad" /><span className="grow">Dates choisies…</span></button>
        </div>
        {custom && <div className="xr-years">
          <input className="field" type="date" value={from} onChange={(e) => setFrom(e.target.value)} aria-label="Du" />
          <input className="field" type="date" value={to} onChange={(e) => setTo(e.target.value)} aria-label="Au" />
        </div>}
        <div className="xr-fh row"><span className="grow">Actions</span><span className="n">{allActions.filter((a) => !excluded.includes(a)).length} / {allActions.length}</span></div>
        <div className="xr-list" data-testid="journal-actions">
          {(head?.actions || []).map(([a, n]) => (
            <label key={a} className="xr-check"><input type="checkbox" checked={!excluded.includes(a)} onChange={() => toggle(a)} />
              <span className="grow xr-act">{a}</span><span className="n">{nf(n)}</span></label>
          ))}
          {excluded.filter((a) => !allActions.includes(a)).map((a) => (
            <label key={a} className="xr-check"><input type="checkbox" checked={false} onChange={() => toggle(a)} /><span className="grow xr-act">{a}</span><span className="n">0</span></label>
          ))}
          {!head?.actions?.length && !excluded.length && <div className="tiny muted">Aucune action sur cette période.</div>}
        </div>
        <div className="xr-fh">Opérateur · poste</div>
        <select className="field" value={op} onChange={(e) => setOp(e.target.value)} aria-label="Opérateur et poste" data-testid="journal-op">
          <option value="">Tous ({nf(head?.operateurs?.length || 0)} opérateur{(head?.operateurs?.length || 0) > 1 ? "s" : ""})</option>
          {(head?.operateurs || []).map(([o, n]) => <option key={o} value={o}>{o} ({nf(n)})</option>)}
        </select>
      </aside>

      <section className="card xr-main">
        <div className="xr-sum">
          <span className="xr-count" data-testid="journal-count">{nf(total)}</span>
          <span className="muted xr-scope ellipsis">action{total > 1 ? "s" : ""} · {periodLabel.toLowerCase()} · {op || "tous postes"}</span>
          <span className="grow" />
          <Badge text="Non modifiable" tone="gray" title="Le journal ne peut être ni modifié ni effacé depuis Scolarix" />
        </div>
        <VirtualTable count={total} getRow={getRow} cols={cols} selected={sel} onSelect={setSel} onRange={onRange}
          onActivate={(i) => { const j = getRow(i); if (j?.matricule) openInGuichet(app.go, j.matricule); }}
          rowKey={(j, i) => (j ? j.id : `p${i}`)} testid="journal-table" label="Journal des actions"
          empty={<span className="muted">{head ? "Aucune action avec ces filtres." : "Chargement…"}</span>} />
        <div className="xr-foot muted small">
          <span className="grow">{nf(total)} ligne{total > 1 ? "s" : ""}, toutes consultables</span>
          <span className="opt-mid">La preuve en cas de contestation : qui a consulté, imprimé ou réédité quel relevé, et quand.</span>
        </div>
      </section>

      <aside className="dock xr-dock" aria-label="Export du journal">
        <div className="grp">
          <div className="lbl">Exporter · {nf(total)}</div>
          <button className="btn primary" onClick={() => void exportCsv()} data-testid="journal-export"><span className="r"><Icon name="download" size={16} />CSV</span><span className="kbd">Ctrl+E</span></button>
          <div className="tiny muted">Période et filtres affichés</div>
        </div>
        <div className="grp">
          <div className="lbl">Ligne choisie</div>
          {selRow?.matricule && <div className="tiny mono xr-selmat">{selRow.matricule}</div>}
          <button className="btn ghost sm" disabled={!selRow?.matricule} onClick={() => selRow?.matricule && openInGuichet(app.go, selRow.matricule)} data-testid="journal-guichet"><Icon name="search" size={14} />Au guichet<span className="kbd">Entrée</span></button>
          <button className="btn ghost sm" disabled={!selRow?.matricule} onClick={() => selRow?.matricule && setQ(selRow.matricule)} data-testid="journal-this"><Icon name="filter" size={14} />Ce matricule</button>
        </div>
      </aside>
    </div>
  );
}
