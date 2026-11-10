import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { call, plural, type Demande } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Icon, useDialogs, useToast } from "../components/ui";
import { type PdfViewHandle, type PdfViewState } from "../components/PdfView";
import { OpProgress, ReviewFocusBar, refocus } from "../components/ReviewShell";
import { Stepper, ageLabel, hourLabel } from "../components/Stepper";
import {
  DemandeMenu, DemandeReview, DemandeStage, PieceStrip, ReviewDock, STATUT_AIDE, STATUT_TONE, STEP_IDX, stepsOf, useDemandeReview,
  type DemandeReviewState,
} from "./DemandeReview";
import { periodLabel } from "./Guichet";

// Page Demandes : maître–détail comme le guichet. Barre unique (recherche + filtres avec compteurs), liste
// groupée par prochaine action avec l'ancienneté, la demande choisie se déplie sur ses pièces ; à droite, le
// même bandeau, la même scène et le même dock que la revue de la demande (Entrée : revue plein cadre).

type BoardRow = Demande & { pages: number; bloques: number };
const FILTRES: [string, string, string[] | null][] = [
  ["a_traiter", "À traiter", ["EN COURS", "PRÊTE", "IMPRIMÉE"]], ["a_imprimer", "À imprimer", ["EN COURS", "PRÊTE"]],
  ["a_remettre", "À remettre", ["IMPRIMÉE"]], ["remises", "Remises", ["REMISE"]], ["annulees", "Annulées", ["ANNULÉE"]], ["toutes", "Toutes", null],
];
const GROUPES: [string, string, string[]][] = [
  ["printer", "À imprimer", ["EN COURS", "PRÊTE"]], ["hand", "À remettre", ["IMPRIMÉE"]], ["check", "Remises", ["REMISE"]], ["x", "Annulées", ["ANNULÉE"]],
];
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const typing = (e: KeyboardEvent, except?: HTMLElement | null) => {
  const t = e.target as HTMLElement | null;
  return !!t && t !== except && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
};

export function DemandesPage() {
  const app = useApp();
  const toast = useToast();
  const { prompt } = useDialogs();
  const active = app.page === "demandes";
  const [filtre, setFiltre] = useState("a_traiter");
  const [all, setAll] = useState<BoardRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"age" | "num">("age");
  const [selId, setSelId] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [onHeader, setOnHeader] = useState(true);
  const [full, setFull] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const a = await call<BoardRow[]>("demandes_board", {});
    setAll(a);
    setLoaded(true);
  }, []);
  const rv = useDemandeReview(selId, () => { void load(); app.bumpDemandes(); });
  useEffect(() => { void load().catch((e) => toast(errMsg(e), { tone: "red" })); }, [load, app.demandeVersion, toast]);
  useEffect(() => { if (selId) void rv.load().catch(() => undefined); }, [app.demandeVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  const statuts = FILTRES.find((f) => f[0] === filtre)![2];
  const needle = q.trim().toLowerCase();
  const rows = useMemo(() => {
    const r = all.filter((d) => (!statuts || statuts.includes(d.statut))
      && (!needle || [d.numero, d.matricule, d.nom, d.demandeur, d.motif].map((x) => x || "").join(" ").toLowerCase().includes(needle)));
    return r.sort((a, b) => (sort === "num" ? b.numero.localeCompare(a.numero)
      : (a.statut === "IMPRIMÉE" ? a.maj_le : a.creee_le).localeCompare(b.statut === "IMPRIMÉE" ? b.maj_le : b.creee_le)));
  }, [all, statuts, needle, sort]);
  const groups = GROUPES.map(([icon, label, st]) => ({ icon, label, rows: rows.filter((d) => st.includes(d.statut)) })).filter((g) => g.rows.length);
  const ordered = groups.flatMap((g) => g.rows);
  // la sélection suit le filtre : la première demande à traiter si l'ancienne n'est plus affichée
  useEffect(() => {
    if (!loaded) return;
    if (!selId || !ordered.some((d) => d.id === selId)) { setSelId(ordered[0]?.id ?? null); setExpanded(true); setOnHeader(true); }
  }, [loaded, filtre, needle, all]); // eslint-disable-line react-hooks/exhaustive-deps

  const count = (st: string[] | null) => all.filter((d) => !st || st.includes(d.statut)).length;
  const today = new Date().toISOString().slice(0, 10);
  const imprimeesJour = all.filter((d) => (d.statut === "IMPRIMÉE" || d.statut === "REMISE") && (d.maj_le || "").startsWith(today)).length;
  const remisesJour = all.filter((d) => d.statut === "REMISE" && (d.maj_le || "").startsWith(today)).length;

  const create = async () => {
    const who = await prompt("Nouvelle demande", "Demandeur (l'étudiant lui-même, un parent, un mandataire…) :", "L'étudiant lui-même");
    if (who === null) return;
    const d = await call<{ demande: Demande }>("demande_create", { demandeur: who.trim() });
    app.setActiveDemande(d.demande.id);
    app.bumpDemandes();
    toast(`Demande ${d.demande.numero} ouverte : ajoutez les relevés au guichet (Ctrl+D).`, { action: { label: "Aller au guichet", run: () => app.go("guichet") } });
  };

  // ---------------------------------------------------------------- clavier : ↑ ↓ demandes et pièces, → ← déplier
  const entries = useMemo(() => {
    const out: { did: number; key: string | null }[] = [];
    for (const d of ordered) {
      out.push({ did: d.id, key: null });
      if (d.id === selId && expanded && rv.d && rv.d.demande.id === d.id) rv.pieces.forEach((p) => out.push({ did: d.id, key: p.key }));
    }
    return out;
  }, [ordered, selId, expanded, rv.d, rv.pieces]);
  const pos = entries.findIndex((e) => e.did === selId && (onHeader ? e.key === null : e.key === rv.sel));
  const step = (n: number) => {
    const e = entries[Math.max(0, Math.min(entries.length - 1, (pos < 0 ? 0 : pos) + n))];
    if (!e) return;
    if (e.did !== selId) { setSelId(e.did); setExpanded(false); setOnHeader(true); return; }
    if (e.key === null) setOnHeader(true); else { setOnHeader(false); rv.setSel(e.key); }
  };
  const pickDemande = (id: number) => { if (id !== selId) setSelId(id); setExpanded(true); setOnHeader(true); };
  const openFull = () => { if (selId) setFull(selId); };
  const nav = (n: number) => (e: KeyboardEvent) => (typing(e, inputRef.current) ? false : step(n));
  const pdf = useRef<PdfViewHandle>(null);
  useShortcuts({
    "ctrl+n": () => void create(), "ctrl+f": () => inputRef.current?.select(), "f2": () => inputRef.current?.select(),
    "arrowdown": nav(1), "arrowup": nav(-1),
    "arrowright": (e) => (typing(e) ? false : (setExpanded(true), undefined)),
    "arrowleft": (e) => (typing(e) ? false : (setExpanded(false), setOnHeader(true), undefined)),
    "enter": (e) => (typing(e, inputRef.current) || /^(BUTTON|A)$/.test((e.target as HTMLElement)?.tagName || "") ? false : openFull()),
    "ctrl+p": () => void rv.printAll(), "ctrl+enter": () => void rv.done(),
    "v": (e) => (typing(e) ? false : rv.cycleView()),
    "alt+1": () => pdf.current?.goZone("identite"), "alt+2": () => pdf.current?.goZone("notes"), "alt+3": () => pdf.current?.goZone("decision"),
  }, active && !full);

  return (
    <>
      <header className="card topbar" data-testid="demandes-topbar">
        <div className="omni">
          <Icon name="search" size={19} color="#475569" />
          <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Demande : numéro, nom, matricule, demandeur…"
            aria-label="Filtrer les demandes" data-testid="demandes-filter" spellCheck={false}
            onKeyDown={(e) => { if (e.key === "Escape" && q) { e.preventDefault(); e.stopPropagation(); setQ(""); } else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) { e.preventDefault(); openFull(); } }} />
          {q ? <button className="btn ghost sm icon" aria-label="Effacer" onClick={() => { setQ(""); inputRef.current?.focus(); }}><Icon name="x" size={14} /></button>
            : <span className="hint">Ctrl+F</span>}
        </div>
        <div className="fseg" role="radiogroup" aria-label="Statut" data-testid="demandes-statuts">
          {FILTRES.map(([id, label, st]) => (
            <button key={id} className={`${filtre === id ? "on" : ""}${id === "annulees" ? " opt-wide" : ""}`} role="radio" aria-checked={filtre === id} onClick={() => setFiltre(id)}>
              {label}<span className="n">{count(st)}</span></button>
          ))}
        </div>
        <span className="grow" />
        <span className="muted small nowrap opt-wide" title="Demandes imprimées et remises aujourd'hui"><Icon name="chart" size={14} /> Aujourd'hui : {plural(imprimeesJour, "imprimée")} · {plural(remisesJour, "remise")}</span>
        <button className="btn primary sm dm-new" onClick={() => void create()} data-testid="demande-new-page"><Icon name="plus" size={16} />Nouvelle demande<span className="kbd">Ctrl+N</span></button>
      </header>

      <div className="gx-body">
        <section className="card gx-list" aria-label="Demandes">
          <div className="row" style={{ gap: 8 }}>
            <div className="grow">
              <div className="eyebrow ellipsis">Demandes {FILTRES.find((f) => f[0] === filtre)![1].toLowerCase()}</div>
              <div className="h3 ellipsis" data-testid="demandes-count">{plural(rows.length, "demande")} · {plural(rows.reduce((n, d) => n + d.n, 0), "relevé")}</div>
            </div>
            <div className="seg sm" role="radiogroup" aria-label="Ordre">
              <button className={sort === "age" ? "on" : ""} role="radio" aria-checked={sort === "age"} onClick={() => setSort("age")} title="La plus ancienne d'abord">Ancienneté</button>
              <button className={sort === "num" ? "on" : ""} role="radio" aria-checked={sort === "num"} onClick={() => setSort("num")} title="Par numéro, la plus récente d'abord">N°</button>
            </div>
          </div>
          <div className="gx-items" role="listbox" aria-label="Demandes" data-testid="demandes-list">
            {groups.map((g) => (
              <div key={g.label} className="grp">
                <div className="dm-ghead"><RowIcon name={g.icon} />{g.label}<span className="n">{g.rows.length}</span></div>
                {g.rows.map((d) => <DemandeCard key={d.id} d={d} on={d.id === selId} onHeader={onHeader} expanded={expanded && d.id === selId} rv={d.id === selId ? rv : null}
                  onPick={() => pickDemande(d.id)} onOpen={() => { pickDemande(d.id); setFull(d.id); }} onPiece={(k) => { setOnHeader(false); rv.setSel(k); }} />)}
              </div>
            ))}
            {!rows.length && loaded && <div className="empty"><div className="muted small">{needle && count(statuts) ? `Aucune demande ne correspond à « ${q.trim()} ».`
              : `Aucune demande ${FILTRES.find((f) => f[0] === filtre)![1].toLowerCase()}. Au guichet, « Ajouter » (Ctrl+D) ouvre une demande pour l'étudiant affiché.`}</div></div>}
          </div>
          <div className="muted tiny gx-hints"><span className="key">↑</span><span className="key">↓</span> parcourir · <span className="key">→</span> déplier · <span className="key">Entrée</span> revue plein cadre · <span className="key">Ctrl+P</span> imprimer tout</div>
        </section>
        <DemandeDetail rv={rv} pdf={pdf} onReview={openFull} onGuichet={() => { if (selId) { app.setActiveDemande(selId); app.go("guichet"); } }} />
      </div>
      {full && <DemandeReview did={full} backLabel="Demandes" onClose={() => { setFull(null); void load(); void rv.load().catch(() => undefined); app.bumpDemandes(); }} />}
    </>
  );
}

function RowIcon({ name }: { name: string }) {
  if (name === "hand") return <svg width={13} height={13} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M8 11V5.5a1.5 1.5 0 0 1 3 0V10m0-1V4.5a1.5 1.5 0 0 1 3 0V10m0-.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-1a6 6 0 0 1-4.6-2.2L3.6 14a1.5 1.5 0 0 1 2.3-1.9L8 14" /></svg>;
  return <Icon name={name} size={13} />;
}

function MiniSteps({ statut }: { statut: string }) {
  const i = STEP_IDX[statut] ?? 0;
  return <span className="mini-steps" title={STATUT_AIDE[statut] || statut}>{[0, 1, 2].map((k) => <i key={k} className={i < 0 ? "" : k < i ? "done" : k === i ? "cur" : ""} />)}</span>;
}

function DemandeCard({ d, on, onHeader, expanded, rv, onPick, onOpen, onPiece }: {
  d: BoardRow; on: boolean; onHeader: boolean; expanded: boolean; rv: DemandeReviewState | null; onPick: () => void; onOpen: () => void; onPiece: (k: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (on) ref.current?.scrollIntoView({ block: "nearest" }); }, [on, expanded]);
  const when = d.statut === "IMPRIMÉE" ? `imprimée ${hourLabel(d.maj_le)}` : d.statut === "REMISE" ? `remise ${hourLabel(d.maj_le)}` : hourLabel(d.creee_le);
  const age = d.statut === "IMPRIMÉE" ? ageLabel(d.maj_le) : null;
  const ready = rv && rv.d && rv.d.demande.id === d.id ? rv : null;
  return (
    <div ref={ref} className={`it dm-card${on ? " sel" : ""}${on && onHeader ? " head" : ""}`} onClick={onPick} onDoubleClick={onOpen} role="option" aria-selected={on}
      data-testid="demande-item" title="Double-clic ou Entrée : revue plein cadre">
      <div className="l1"><span className="mono">{d.numero}</span><span className="muted small nowrap">{when}</span><span className="grow" /><MiniSteps statut={d.statut} /></div>
      <div className="l1"><b className="ellipsis dm-nom">{d.nom || "—"}</b>{d.matricule && <span className="dm-mat">{d.matricule}</span>}</div>
      <div className="l2"><span className="ellipsis">{d.demandeur || "—"}{d.motif ? ` · ${d.motif.split(/[ /]/)[0]}` : ""} · {plural(d.n, "relevé")}{d.pages ? ` · ${d.pages} p.` : ""}</span><span className="grow" />
        {d.bloques > 0 && <Badge text={plural(d.bloques, "bloqué")} tone="red" />}
        {age && <Badge text={`attend ${age.text}`} tone={age.days >= 1 ? "amber" : "gray"} />}
        {(d.statut === "ANNULÉE") && <Badge text="annulée" tone="gray" />}</div>
      {on && expanded && ready && (
        <div className="dm-pieces" onClick={(e) => e.stopPropagation()} data-testid="demande-pieces">
          {ready.busy && <OpProgress title={ready.busy === "print" ? "Impression" : "Préparation"} progress={ready.opn.progress} cancelling={ready.opn.cancelling} onCancel={() => void ready.opn.cancel()} testid="demande-progress" />}
          {ready.pieces.map((p) => {
            const s = ready.pieceState(p);
            const r = p.it?.releve;
            const chk = p.it && ready.controles.has(p.it.item.releve_id);
            return (
              <div key={p.key} className={`dp${ready.sel === p.key && !onHeader ? " sel" : ""}${p.kind === "bordereau" && !ready.bordereau ? " off" : ""}`} onClick={() => onPiece(p.key)} data-testid="demande-piece">
                <span className="dn">{p.n}</span>
                <span className="grow ellipsis">{p.kind === "bordereau" ? "Bordereau de remise" : r ? <><b>{periodLabel(r)}</b> {r.annee_academique}{(r.session || "").toLowerCase().startsWith("rattrap") && <span className="c-amber tiny"> RATT.</span>}</> : "relevé retiré de l'index"}</span>
                <span className={`tiny nowrap ${s.tone === "muted" ? "muted" : `c-${s.tone}`}`} title={s.title}>{p.kind === "bordereau" ? (p.n === 0 ? "1 p." : "") : s.text.replace("PRÊT · ", "")}</span>
                {p.kind === "releve" && <span className={`dpchk${chk ? " on" : ""}`} title={chk ? "Contrôlé à l'écran" : "Pas encore contrôlé à l'écran"}>{chk && <Icon name="check" size={11} stroke={2.6} />}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function DemandeDetail({ rv, pdf, onReview, onGuichet }: { rv: DemandeReviewState; pdf: React.RefObject<PdfViewHandle | null>; onReview: () => void; onGuichet: () => void }) {
  const [pv, setPv] = useState<PdfViewState>({ fit: "auto", scale: null, overflow: false });
  const [mmHost, setMmHost] = useState<HTMLDivElement | null>(null);
  const [focus, setFocus] = useState(false);
  const box = useRef<HTMLElement>(null);
  const d = rv.d;
  useShortcuts({ "escape": () => setFocus(false) }, focus);
  if (!d) return (
    <section className="gx-preview">
      <div className="card strip" data-testid="demande-strip"><span className="nm muted">{rv.did ? "Chargement…" : "Aucune demande sélectionnée"}</span></div>
      <div className="gx-stage"><div className="empty muted small">{rv.did ? <span className="spinner" /> : "Choisissez une demande dans la liste : ses pièces s'affichent ici."}</div></div>
    </section>
  );
  const st = d.demande.statut;
  const open = st === "EN COURS" || st === "PRÊTE" || st === "IMPRIMÉE";
  return (
    <section className="gx-preview" tabIndex={-1} ref={box} onClick={(e) => refocus(e, box.current)} style={{ outline: "none" }}>
      <div className="card strip dm-strip" data-testid="demande-strip">
        <Badge text={st} tone={STATUT_TONE[st] || "gray"} big dot />
        <span className="mono nowrap">{d.demande.numero}</span>
        <span className="nm ellipsis">{d.demande.nom || "—"}</span>
        {d.demande.matricule && <span className="mat">{d.demande.matricule}</span>}
        <span className="facts opt-wide ellipsis">{d.demande.demandeur || "—"}{d.demande.motif ? <><i>·</i>{d.demande.motif}</> : null}</span>
        <span className="grow" />
        {st !== "ANNULÉE" && <Stepper steps={stepsOf(d, rv.busy)} current={STEP_IDX[st] ?? 0} compact />}
        <DemandeMenu rv={rv} />
      </div>
      <DemandeStage rv={rv} focus={focus} setFocus={setFocus} pdf={pdf} pv={pv} setPv={setPv} setMm={setMmHost} mmHost={mmHost}
        caption={rv.view === "releve" && rv.cur ? <div className="stage-cap"><PieceStrip rv={rv} floating /></div> : null}
        dock={<ReviewDock rv={rv} extra={<>
          <button className="btn ghost sm" onClick={onReview} data-testid="demande-review-open" title="Revue plein cadre : réordonner, contrôler pièce par pièce (Entrée)"><Icon name="fit" size={15} />Revue<span className="kbd">Entrée</span></button>
          {open && <button className="btn ghost sm" onClick={onGuichet} title="Rendre la demande active au guichet pour y ajouter des relevés (Ctrl+D)"><Icon name="search" size={15} />Au guichet</button>}
        </>} />}
        focusBar={<ReviewFocusBar onExit={() => setFocus(false)}>
          <Badge text={st} tone={STATUT_TONE[st] || "gray"} dot /><b className="nm ellipsis">{d.demande.nom || "—"}</b>
          <span className="muted small nowrap">{rv.cur ? rv.pieceLabel(rv.cur) : ""}</span>
        </ReviewFocusBar>} />
    </section>
  );
}
