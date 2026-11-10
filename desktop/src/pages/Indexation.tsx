import { useCallback, useEffect, useRef, useState } from "react";
import { call } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Icon, useDialogs, useToast } from "../components/ui";
import { duree, errMsg, fr, short, type Overview } from "../indexation";
import { IndexDashboard } from "./IndexDashboard";
import { AnomalyTriage } from "./AnomalyTriage";

// Écran Indexation (2.1) : une seule barre en haut et deux vues sœurs, « État du fonds » et « Anomalies ».
// Le robot tourne dans un processus séparé : le guichet reste utilisable pendant une passe.

export interface Live {
  status: "PRÊT" | "EN COURS" | "ARRÊT…" | "TERMINÉ" | "ARRÊTÉ";
  total: number; done: number; pct: number; aTraiter: number; workers: number; mode: string;
  stages: [number, number, number, number];        // trouvés, fichiers lus, blocs, relevés écrits
  ignores: number; echecs: number; rate: number | null; eta: number | null; current: string; detail: string;
  stopAt: number | null;                           // heure de la demande d'arrêt (arrêt forcé au bout de 60 s)
  error: string | null;                            // cause d'un arrêt anormal du robot
}
const IDLE: Live = { status: "PRÊT", total: 0, done: 0, pct: 0, aTraiter: 0, workers: 0, mode: "", stages: [0, 0, 0, 0], ignores: 0,
  echecs: 0, rate: null, eta: null, current: "", detail: "", stopAt: null, error: null };
// gardé d'un affichage de l'écran à l'autre (l'écran est démonté quand on passe au guichet)
let kept: Live = IDLE;
let keptView: "etat" | "anomalies" = "etat";
export const FORCE_AFTER_S = 60;

export interface IxNav { view: "etat" | "anomalies"; cause?: string; nonce: number }

function useIndexRun() {
  const app = useApp();
  const toast = useToast();
  const [live, setLiveRaw] = useState<Live>(() => {
    if (app.indexing.running) return { ...kept, status: kept.status === "PRÊT" || kept.status === "TERMINÉ" ? "EN COURS" : kept.status };
    // passe finie pendant que l'écran était fermé : on ne la montre plus « en cours »
    return kept.status === "EN COURS" || kept.status === "ARRÊT…" ? { ...kept, status: "PRÊT", stopAt: null } : kept;
  });
  const setLive = useCallback((f: (l: Live) => Live) => setLiveRaw((l) => (kept = f(l))), []);

  useEffect(() => {
    const ev = app.indexing.last;
    if (!ev) return;
    setLive((l) => {
      const n: Live = { ...l };
      if (ev.type === "start") {
        Object.assign(n, { ...IDLE, status: "EN COURS", total: ev.total, aTraiter: ev.a_traiter ?? ev.total, workers: ev.workers || 1,
          mode: ev.mode || "", stages: [ev.total, 0, 0, 0],
          current: [`${fr(ev.a_traiter ?? ev.total)} fichier(s) à lire`, ev.exclus ? `${fr(ev.exclus)} élément(s) exclu(s)` : "",
            ev.retires ? `${fr(ev.retires)} retiré(s) de l'index (exclusions)` : ""].filter(Boolean).join(" · ") });
      } else if (ev.type === "progress") {
        const c = ev.counters;
        if (n.status === "PRÊT" || n.status === "TERMINÉ" || n.status === "ARRÊTÉ") n.status = "EN COURS";
        n.done = ev.done; n.total = ev.total; n.pct = Math.floor((ev.done * 100) / Math.max(1, ev.total));
        n.rate = ev.rate ?? null; n.eta = ev.eta_s ?? null; n.ignores = c.ignores; n.echecs = c.echecs;
        if (ev.file) n.current = `Dernier fichier lu : ${short(ev.file, 110)}`;
        n.stages = [n.stages[0] || ev.total, c.extraits + c.echecs, c.blocs, c.ecrits];
      } else if (ev.type === "stage") {
        n.current = `${ev.stage === "ocr" ? `OCR de ${ev.pages} page(s)` : ev.stage === "conversion" ? `Conversion → PDF (${ev.backend})` : "Lecture"} : ${short(ev.file, 100)}`;
      } else if (ev.type === "warning") {
        toast(ev.message, { tone: "amber" });
      } else if (ev.type === "done") {
        n.status = "TERMINÉ"; n.pct = 100; n.stopAt = null;
        n.detail = `${fr(ev.extraits + ev.echecs)} fichier(s) lu(s) en ${duree(ev.duree_s)} · ${fr(ev.ecrits)} relevé(s) écrit(s) · ${fr(ev.ignores)} inchangé(s) · ${fr(ev.echecs)} en échec`;
        if (ev.stopped) { n.status = "ARRÊTÉ"; n.detail = `Passe arrêtée : ${n.detail}`; }
      } else if (ev.type === "exit") {
        n.stopAt = null;
        if (n.status === "EN COURS" || n.status === "ARRÊT…") {
          n.status = "ARRÊTÉ";
          n.error = ev.error || null;
          n.detail = ev.error ? `Le robot s'est arrêté : ${ev.error}` : "Indexation arrêtée. Relancez en mode différentiel pour reprendre là où il s'est arrêté.";
          toast(n.detail, { tone: "amber", ms: 15000 });
        }
        app.refreshStats();
      }
      return n;
    });
  }, [app.indexing.last]); // eslint-disable-line react-hooks/exhaustive-deps

  const started = useCallback(() => setLive(() => ({ ...IDLE, status: "EN COURS", detail: "Analyse de l'arborescence…" })), [setLive]);
  const start = useCallback(async (full = false) => {
    try {
      const src = app.settings?.source_root || "";
      await call("index_start", { source: src, full });
      started();
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  }, [app.settings, started, toast]);
  const stop = useCallback(async (forceIt = false) => {
    const force = forceIt || live.status === "ARRÊT…";
    await call("index_stop", { force }).catch(() => undefined);
    setLive((l) => ({ ...l, status: "ARRÊT…", stopAt: l.stopAt ?? Date.now(),
      detail: force ? "Arrêt forcé." : "Arrêt demandé : fin des fichiers en cours… (cliquer encore pour forcer)" }));
  }, [live.status, setLive]);
  const reset = useCallback(() => setLive(() => IDLE), [setLive]);
  return { live, start, stop, started, reset };
}
export type IndexRun = ReturnType<typeof useIndexRun>;

export function Indexation() {
  const app = useApp();
  const toast = useToast();
  const dialogs = useDialogs();
  const run = useIndexRun();
  const { live } = run;
  const running = app.indexing.running || live.status === "EN COURS" || live.status === "ARRÊT…";
  const [nav, setNav] = useState<IxNav>({ view: keptView, nonce: 0 });
  const [ov, setOv] = useState<Overview | null>(null);
  const [now, setNow] = useState(Date.now());
  const loading = useRef(false);
  keptView = nav.view;

  const refresh = useCallback(() => {
    if (loading.current) return;
    loading.current = true;
    void call<Overview>("index_overview").then(setOv).catch((e) => toast(errMsg(e), { tone: "red" })).finally(() => { loading.current = false; });
  }, [toast]);
  // chiffres tenus à jour (impressions des autres écrans, robot qui écrit), comme la 1.x : toutes les 10 s
  useEffect(() => {
    refresh();
    const t = window.setInterval(() => { refresh(); app.refreshStats(); }, 10000);
    return () => window.clearInterval(t);
  }, [refresh]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (app.indexing.last?.type === "exit" || app.indexing.last?.type === "done") refresh(); }, [app.indexing.last, refresh]);
  // compte à rebours de l'arrêt forcé
  useEffect(() => {
    if (!live.stopAt) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [live.stopAt]);

  const go = useCallback((view: "etat" | "anomalies", cause?: string) => {
    setNav((n) => ({ view, cause, nonce: n.nonce + 1 }));
    refresh();                 // compteurs de la barre à jour à chaque changement de vue
  }, [refresh]);
  const full = !app.settings?.differential;
  const launch = () => { if (!running) void run.start(); };
  useShortcuts({
    "ctrl+enter": () => launch(),
    "ctrl+tab": () => go(nav.view === "etat" ? "anomalies" : "etat"),
    "ctrl+shift+tab": () => go(nav.view === "etat" ? "anomalies" : "etat"),
  });

  const lastPass = ov?.passes?.[0];
  const pct = live.status === "EN COURS" ? ` · ${live.pct} %` : "";
  let chip: { text: string; tone: "green" | "blue" | "amber" | "gray" | "red"; icon: string };
  if (live.status === "ARRÊT…") chip = { text: "ARRÊT EN COURS…", tone: "amber", icon: "stop" };
  else if (running) chip = { text: `EN COURS${pct}`, tone: "blue", icon: "refresh" };
  else if (live.status === "TERMINÉ") chip = { text: `TERMINÉ · ${live.detail.split(" · ")[0] || ""}`, tone: "green", icon: "check" };
  else if (live.status === "ARRÊTÉ") chip = { text: live.error ? "ARRÊT ANORMAL" : "ARRÊTÉE · relancer en différentielle", tone: live.error ? "red" : "amber", icon: "alert" };
  else if (lastPass && (!lastPass.fin || lastPass.arretee)) chip = { text: `INTERROMPUE · passe du ${(ov?.derniere_indexation || "").slice(0, 5)} ${(ov?.derniere_indexation || "").slice(11)}`.trim(), tone: "amber", icon: "alert" };
  else if (ov?.derniere_indexation) chip = { text: `À JOUR · passe du ${ov.derniere_indexation.slice(0, 5)} ${ov.derniere_indexation.slice(11)}`, tone: "green", icon: "check" };
  else chip = { text: ov ? "JAMAIS INDEXÉ" : "…", tone: "gray", icon: "info" };
  const todo = ov?.anomalies.a_traiter ?? 0;
  const forceIn = live.stopAt ? Math.max(0, FORCE_AFTER_S - Math.floor((now - live.stopAt) / 1000)) : null;

  const reset = async () => {
    try {
      const n = await call<Record<string, number>>("index_impact");
      const i = await dialogs.ask({
        title: "Vider l'index",
        danger: true,
        text: `Seront retirés : ${fr(n.releves)} relevé(s) indexé(s), ${fr(n.fichiers)} fichier(s), ${fr(n.anomalies)} anomalie(s) et les aperçus Word en cache.\n\n`
          + `Seront conservés : le journal (${fr(n.journal)} ligne(s)), le registre des délivrances (${fr(n.delivrances)} document(s)) et les demandes (${fr(n.demandes)}), `
          + "ainsi que les anomalies marquées « vues » et l'historique des passes."
          + (n.demandes_ouvertes ? `\n\nAttention : ${n.demandes_ouvertes} demande(s) en cours. Leurs relevés devront être ajoutés à nouveau après la réindexation.` : ""),
        details: "Les archives d'origine ne sont jamais touchées. La recherche au guichet sera vide jusqu'à la fin de la réindexation.",
        choices: ["Vider et tout réindexer", "Vider seulement"],
      });
      if (i < 0) return;
      const r = await call<{ releves: number }>("index_reset", { confirm: "VIDER" });
      app.refreshStats();
      run.reset();
      refresh();
      toast(`Index vidé : ${fr(r.releves)} relevé(s) retiré(s). Journal, registre et demandes conservés.`, { tone: "green", ms: 12000 });
      if (i === 0) {
        await call("index_start", { source: app.settings?.source_root || "", full: true });
        run.started();
      }
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };

  return (
    <div className="ix-page" data-testid="indexation">
      <section className="card topbar ix-top">
        <div className="ix-title"><span className="eyebrow">Robot d'indexation</span><b>Indexation</b></div>
        <div className="ix-vsep" />
        <div className="fseg" role="tablist" aria-label="Vues de l'indexation (Ctrl+Tab)">
          <button role="tab" aria-selected={nav.view === "etat"} className={nav.view === "etat" ? "on" : ""} onClick={() => go("etat")} data-testid="ix-view-etat" title="État du fonds (Ctrl+Tab)">
            <Icon name="database" size={14} /> État du fonds</button>
          <button role="tab" aria-selected={nav.view === "anomalies"} className={nav.view === "anomalies" ? "on" : ""} onClick={() => go("anomalies")} data-testid="ix-view-anomalies" title="Anomalies à traiter (Ctrl+Tab)">
            <Icon name="alert" size={14} /> Anomalies<span className="n" data-testid="ix-anom-count">{ov ? fr(todo) : ""}</span></button>
        </div>
        <span data-testid="index-status"><Badge text={chip.text} tone={chip.tone} big title={live.detail || undefined} /></span>
        <span className="grow" />
        {!running && (
          <div className="fseg" role="radiogroup" aria-label="Mode de la prochaine passe" data-testid="index-mode">
            <button role="radio" aria-checked={!full} className={!full ? "on" : ""} onClick={() => void app.saveSettings({ differential: true })}
              title="Ne relit que les fichiers nouveaux ou modifiés (enregistré tout de suite)">Différentielle</button>
            <button role="radio" aria-checked={full} className={full ? "on" : ""} onClick={() => void app.saveSettings({ differential: false })}
              title="Relit tout le fonds : plusieurs heures (enregistré tout de suite)">Complète</button>
          </div>
        )}
        {running ? (
          <>
            {live.status === "ARRÊT…" && <span className="muted small ix-wide">{forceIn ? `arrêt forcé dans ${forceIn} s` : "arrêt forcé…"}</span>}
            {live.status === "ARRÊT…"
              ? <button className="btn danger" onClick={() => void run.stop(true)} data-testid="index-stop" title="Arrête tout de suite le robot (et Word)"><Icon name="stop" />Forcer l'arrêt</button>
              : <button className="btn ink" onClick={() => void run.stop()} data-testid="index-stop" title="Finit les fichiers en cours puis s'arrête"><Icon name="stop" />Arrêter</button>}
          </>
        ) : (
          <button className="btn primary ix-go" onClick={launch} data-testid="index-start" title={`Lancer une passe ${full ? "complète" : "différentielle"} (Ctrl+Entrée)`}>
            <Icon name="play" size={16} />Lancer l'indexation<span className="kbd ix-wide">Ctrl+Entrée</span></button>
        )}
      </section>
      {nav.view === "etat"
        ? <IndexDashboard ov={ov} run={run} running={running} forceIn={forceIn} go={go} refresh={refresh} onReset={() => void reset()} />
        : <AnomalyTriage nav={nav} running={running} refreshOverview={refresh} />}
    </div>
  );
}
