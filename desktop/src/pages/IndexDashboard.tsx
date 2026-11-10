import { useMemo, useState } from "react";
import { call, sx } from "../api";
import { useApp } from "../state";
import { Badge, Icon, Switch, useToast } from "../components/ui";
import { MODE_LABEL, NIV_TONE, dateCourte, duree, errMsg, fr, pl, signed, type Fix, type Overview, type Pass } from "../indexation";
import type { IndexRun } from "./Indexation";

// Vue « État du fonds » : répond d'abord à « le fonds est-il complet et sain ? », sans défilement de l'écran.

interface Props {
  ov: Overview | null; run: IndexRun; running: boolean; forceIn: number | null;
  go: (v: "etat" | "anomalies", cause?: string) => void; refresh: () => void; onReset: () => void;
}

export function IndexDashboard({ ov, run, running, forceIn, go, refresh, onReset }: Props) {
  return (
    <div className="ix-body">
      <Hero ov={ov} go={go} />
      <div className="ix-grid">
        <div className="ix-col">{running ? <PassLive run={run} forceIn={forceIn} /> : <NextPass ov={ov} run={run} />}</div>
        <div className="ix-col"><YearsChart ov={ov} /><Formats ov={ov} /></div>
        <div className="ix-col"><CausesCard ov={ov} go={go} running={running} /><Maintenance ov={ov} running={running} refresh={refresh} onReset={onReset} /></div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ héros : 4 chiffres + santé du fonds + activité du jour
function Hero({ ov, go }: { ov: Overview | null; go: Props["go"] }) {
  const h = ov?.sante;
  const tot = Math.max(1, h?.fichiers || 0);
  const pctOk = h ? Math.round((h.ok * 1000) / tot) / 10 : 0;
  const seg = (n: number, cls: string, title: string) => (n > 0 ? <div className={cls} style={{ flexGrow: n }} title={`${title} : ${fr(n)} fichier(s)`} /> : null);
  const big: [number | undefined, string, string?][] = [[ov?.releves, "relevés indexés"], [ov?.etudiants, "étudiants distincts"], [ov?.pages, "pages analysées", "ix-hide13"], [ov?.fichiers, "fichiers lus"]];
  return (
    <section className="card ix-hero" data-testid="index-kpis">
      <div className="ix-big">
        {big.map(([n, l, cls]) => (
          <div key={l} className={cls} title={l === "fichiers lus" && ov ? `${fr(ov.pages)} pages analysées` : undefined}>
            <div className="n">{ov ? fr(n) : "—"}</div><div className="l">{l}</div></div>
        ))}
      </div>
      <div className="ix-health" data-testid="index-health">
        <div className="row" style={{ gap: 8 }}><span className="h3">Santé du fonds</span>
          <span className="muted small ellipsis">— {h ? `${pl(h.fichiers, "fichier")}${h.exclus ? ` · ${pl(h.exclus, "élément exclu", "éléments exclus")}` : ""}` : "…"}</span></div>
        <div className="ix-hbar" role="img" aria-label={h ? `${fr(h.ok)} lus sans remarque, ${fr(h.avertissement)} avec avertissement, ${fr(h.echec)} en échec` : ""}>
          {h && h.fichiers ? <>{seg(h.ok, "ok", "Lus sans remarque")}{seg(h.avertissement, "warn", "Lus avec avertissement")}{seg(h.echec, "err", "En échec")}</> : <div className="none" />}
        </div>
        <div className="ix-hleg">
          <span><i className="sw ok" /><Icon name="check" size={13} />{h ? `${fr(h.ok)} lus sans remarque · ${String(pctOk).replace(".", ",")} %` : "—"}</span>
          <span><i className="sw warn" /><Icon name="alert" size={13} /><button className="lnk" onClick={() => go("anomalies", "@AVERTISSEMENT")} data-testid="health-warn">{fr(h?.avertissement)} avec avertissement</button></span>
          <span><i className="sw err" /><Icon name="x" size={13} /><button className="lnk" onClick={() => go("anomalies", "@ERREUR")} data-testid="health-err">{fr(h?.echec)} en échec</button></span>
        </div>
      </div>
      <div className="ix-today" data-testid="index-today">
        <span className="eyebrow">Aujourd'hui</span>
        <span><b>{fr(ov?.aujourdhui.imprimes)}</b> imprimé(s) · <b>{fr(ov?.aujourdhui.duplicatas)}</b> duplicata(s)</span>
        <span><b>{fr(ov?.aujourdhui.verifications)}</b> vérification(s) · <b>{fr(ov?.aujourdhui.extractions)}</b> extraction(s)</span>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ prochaine passe (options enregistrées tout de suite)
function NextPass({ ov, run }: { ov: Overview | null; run: IndexRun }) {
  const app = useApp();
  const toast = useToast();
  const s = app.settings;
  const src = s?.source_root || "";
  const save = (v: Parameters<typeof app.saveSettings>[0]) => void app.saveSettings(v).catch((e) => toast(errMsg(e), { tone: "red" }));
  const change = async () => {
    const d = await sx.pickFolder({ title: "Dossier racine des archives", defaultPath: src || undefined });
    if (d) { save({ source_root: d }); toast("Dossier des archives enregistré : il servira à la prochaine passe.", { tone: "blue", ms: 4000, sound: false }); }
  };
  const last = ov?.passes?.[0];
  const excl = s?.exclusions?.length || 0;
  return (
    <section className="card ix-pass" data-testid="index-next">
      <div className="ix-ctitle"><span className="h3">Prochaine passe</span>
        <button className="btn link small" onClick={() => app.openSettings("indexation")} title="Réglages › Indexation">Réglages ›</button></div>
      <div className={`ix-src${ov && !ov.source.existe ? " bad" : ""}`} title={src}>
        <Icon name="folder" size={17} />
        <span className="p ellipsis selectable ix-rtl" data-testid="index-source"><bdi>{src || "Aucun dossier choisi"}</bdi></span>
        <span className="badge t-gray" title="Lecture seule : aucune écriture, suppression ni renommage dans ce dossier"><Icon name="lock" size={11} />LECTURE SEULE</span>
        <button className="btn ghost xs" onClick={() => void change()} data-testid="index-source-change">Changer…</button>
      </div>
      {ov && src && !ov.source.existe && <div className="alert t-red"><Icon name="alert" size={15} />Dossier introuvable depuis ce poste (réseau, lecteur déconnecté ?).</div>}
      <div className="ix-opts" data-testid="index-options">
        <Switch blue checked={!!s?.ocr_enabled} onChange={(v) => save({ ocr_enabled: v })} label="OCR des scans" title="Tesseract sur les PDF d'imprimante (plus lent)" />
        <Switch blue checked={!!s?.ocr_preprocess} onChange={(v) => save({ ocr_preprocess: v })} label="Deskew + Otsu" title="Redresse les scans de travers avant l'OCR" />
        <Switch blue checked={!!s?.word_com_enabled} onChange={(v) => save({ word_com_enabled: v })} label="Passerelle COM Word" title="Lit les .doc 97-2003 avec Word (sinon LibreOffice)" />
        <Switch blue checked={!!s?.docx_convert_at_index} onChange={(v) => save({ docx_convert_at_index: v })} label="Préconvertir .docx" title="Aperçu immédiat, mais indexation bien plus lente. Sinon : aperçu produit à la 1re consultation." />
      </div>
      <div className="tiny muted">Ces options et le mode (Différentielle / Complète) sont enregistrés dès le clic, sans « Enregistrer ».</div>
      {ov && ov.outils.ocr && !ov.outils.tesseract && <div className="alert t-amber"><Icon name="alert" size={15} />Tesseract introuvable : les scans ne seront pas lus.
        <button className="btn link small" onClick={() => app.openSettings("poste")}>Indiquer son chemin…</button></div>}
      <div className="ix-kv">
        <span>Processus</span><b>{s?.index_workers ? `${s.index_workers} en parallèle` : `automatique (${app.info?.default_workers ?? "?"})`}</b>
        <span>Exclusions</span><b><button className="btn link small" style={{ padding: 0 }} onClick={() => app.openSettings("indexation")} data-testid="index-exclusions"
          title={(s?.exclusions || []).join("\n") || "Aucun dossier ni motif exclu"}>{excl ? pl(excl, "dossier ou motif", "dossiers ou motifs") + "…" : "Exclure des sous-dossiers…"}</button></b>
        <span>Index local</span><b className="mono-s ellipsis selectable" title={app.info?.db_path}>{app.info?.db_path || "—"}</b>
      </div>
      {run.live.error && <div className="alert t-red" data-testid="index-robot-error"><Icon name="alert" size={15} />Le robot s'est arrêté anormalement : {run.live.error}</div>}
      {last && <LastPass p={last} />}
      {(ov?.passes?.length || 0) > 1 && (
        <>
          <div className="eyebrow" style={{ marginTop: 2 }}>Passes récentes</div>
          <div className="ix-passes" data-testid="index-passes">
            {ov!.passes.slice(1, 7).map((p) => (
              <div key={p.id} title={`${MODE_LABEL[p.mode] || p.mode} · ${p.source}`}>
                <span>{dateCourte(p.debut)}</span><span>{MODE_LABEL[p.mode] || p.mode}</span><span>{p.fin ? duree(p.duree_s) : "—"}</span>
                <b>{p.mode === "COMPLETE" ? fr(p.ecrits) : signed(p.releves_delta)}</b>
                <i>{!p.fin ? "interrompue" : p.arretee ? "arrêtée" : pl(p.echecs, "échec")}</i>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function LastPass({ p }: { p: Pass }) {
  const state = !p.fin ? " · interrompue" : p.arretee ? " · arrêtée" : "";
  return (
    <div className="soft col ix-last" data-testid="index-last-pass">
      <div className="eyebrow">Dernière passe · {dateCourte(p.debut, true)}{state}</div>
      <div className="small"><b>{MODE_LABEL[p.mode] || p.mode}</b> · {p.fin ? duree(p.duree_s) : "durée inconnue"} · {pl(p.lus, "fichier lu", "fichiers lus")}{p.ignores ? ` · ${fr(p.ignores)} inchangé(s)` : ""}</div>
      {p.fin ? <div className="small">{signed(p.releves_delta)} relevés · {pl(p.echecs, "échec")} · {signed(p.anomalies_delta)} anomalies</div>
        : <div className="small" style={{ color: "var(--amber)" }}>Le robot n'a pas terminé (arrêt forcé ou poste éteint) : relancez en différentielle.</div>}
    </div>
  );
}

// ------------------------------------------------------------------ passe en cours
function PassLive({ run, forceIn }: { run: IndexRun; forceIn: number | null }) {
  const app = useApp();
  const l = run.live;
  const stopping = l.status === "ARRÊT…";
  const eta = l.eta ? ` · restant ≈ ${duree(l.eta)}` : "";
  const funnel: [string, number, string][] = [["Trouvés", l.stages[0], "Fichiers trouvés dans l'arborescence"], ["Lus", l.stages[1], "Fichiers lus pendant cette passe"],
    ["Blocs", l.stages[2], "Blocs de relevé identifiés"], ["Relevés", l.stages[3], "Relevés écrits dans l'index"]];
  const mode = l.mode || (app.settings?.differential ? "DIFFERENTIELLE" : "COMPLETE");
  return (
    <section className="card ix-pass" data-testid="index-live">
      <div className="ix-ctitle"><span className="h3">Passe en cours</span><Badge text={(MODE_LABEL[mode] || mode).toUpperCase()} tone="blue" /></div>
      <div className="row" style={{ gap: 10 }}>
        <span className="ix-pct">{l.pct} %</span>
        <div className="grow"><div className="progress"><div style={{ width: `${l.pct}%` }} /></div>
          <div className="small muted" style={{ marginTop: 4 }}>{l.total ? `${fr(l.done)} / ${fr(l.total)} fichiers` : l.detail || "Analyse de l'arborescence…"}
            {l.rate ? ` · ${String(l.rate.toFixed(1)).replace(".", ",")} fichiers/s` : ""}{eta}</div></div>
      </div>
      <div className="ix-funnel">
        {funnel.map(([lab, n, tip], i) => <div key={lab} className={i === 1 ? "cur" : ""} title={tip}><span className="eyebrow">{lab}</span><b>{fr(n)}</b></div>)}
      </div>
      <div className="ix-kv">
        <span>Fichiers</span><b>{fr(l.ignores)} inchangé(s) ignoré(s) · {fr(l.echecs)} en échec</b>
        <span>Processus</span><b>{l.workers ? `${l.workers} en parallèle` : "—"}</b>
        <span>En cours</span><b className="mono-s ellipsis selectable" title={l.current} data-testid="index-current">{l.current || "—"}</b>
      </div>
      <div className="alert t-blue"><Icon name="info" size={15} />
        {stopping ? `Arrêt demandé : le robot finit les fichiers en cours. Sans fin propre, l'arrêt est forcé automatiquement${forceIn ? ` dans ${forceIn} s` : ""} (Word compris).`
          : "Le guichet reste utilisable pendant la passe. « Arrêter » finit les fichiers en cours ; un second clic force l'arrêt."}</div>
      <div className="row">
        <button className="btn ink" onClick={() => void run.stop()} disabled={stopping}><Icon name="stop" size={15} />Arrêter</button>
        <button className="btn danger sm" onClick={() => void run.stop(true)} disabled={!stopping} data-testid="index-force" title="Arrête tout de suite le robot et Word">Forcer l'arrêt</button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ relevés par année (trous visibles)
function YearsChart({ ov }: { ov: Overview | null }) {
  const [hov, setHov] = useState<number | null>(null);
  const ys = ov?.annees || [];
  const max = Math.max(1, ...ys.map((y) => y.n));
  const step = Math.max(1, Math.ceil(ys.length / 8));
  const firstGap = ys.findIndex((y) => y.trou);
  const range = ys.length ? `${ys[0].annee.slice(0, 4)} → ${ys[ys.length - 1].annee.slice(-4)}` : "";
  const tipAt = (i: number) => `${((i + 0.5) * 100) / Math.max(1, ys.length)}%`;
  return (
    <section className="card ix-years">
      <div className="ix-ctitle"><span className="h3">Relevés par année académique</span><span className="muted small">{range}</span></div>
      {ys.length ? (
        <>
          <div className="ix-ybars" data-testid="index-years" onMouseLeave={() => setHov(null)}>
            {ys.map((y, i) => (
              <div key={y.annee} className={`b${y.trou ? " gap" : ""}${hov === i ? " hov" : ""}`} onMouseEnter={() => setHov(i)}
                title={`${y.annee} : ${pl(y.n, "relevé")}${y.trou ? " — dossier manquant ?" : ""}`}>
                <i style={{ height: `${Math.max(y.n ? 2 : 0.6, (y.n * 100) / max)}%` }} />
              </div>
            ))}
            {firstGap >= 0 && hov === null && (
              <span className="ix-gapnote" style={{ left: tipAt(firstGap) }} data-testid="index-year-gap">
                {ys[firstGap].annee.slice(0, 4)}-{ys[firstGap].annee.slice(7, 9)} : {pl(ys[firstGap].n, "relevé")} — dossier manquant ?</span>
            )}
            {hov !== null && ys[hov] && (
              <span className="ix-ytip" style={{ left: tipAt(hov) }}>{ys[hov].annee} · <b>{fr(ys[hov].n)}</b> relevé(s)</span>
            )}
          </div>
          <div className="ix-yax">{ys.map((y, i) => <span key={y.annee}>{i % step === 0 ? y.annee.slice(2, 4) : ""}</span>)}</div>
        </>
      ) : <div className="muted small">Aucun relevé indexé.</div>}
    </section>
  );
}

// ------------------------------------------------------------------ fichiers par format
function Formats({ ov }: { ov: Overview | null }) {
  const fm = ov?.formats || [];
  const max = Math.max(1, ...fm.map((f) => f.n));
  return (
    <section className="card ix-formats" data-testid="index-formats">
      <div className="ix-ctitle"><span className="h3">Fichiers par format</span><span className="muted small">{fr(ov?.fichiers)}</span></div>
      {fm.map((f) => (
        <div key={f.type} className="ix-hb">
          <span>{f.label}</span><div className="t"><i style={{ width: `${(f.n * 100) / max}%` }} /></div><b>{fr(f.n)}</b>
          {f.note && <span className="s">{f.note}</span>}
        </div>
      ))}
      {!fm.length && <div className="muted small">Aucun fichier indexé.</div>}
    </section>
  );
}

// ------------------------------------------------------------------ anomalies par cause + correction à fort levier
export function FixAction({ fix, compact = false }: { fix: Fix; compact?: boolean }) {
  const app = useApp();
  const toast = useToast();
  if (!fix.bouton) return null;
  const act = async () => {
    if (fix.action === "ocr_enable") {
      try { await app.saveSettings({ ocr_enabled: true }); toast("OCR des scans activé (enregistré). Relisez maintenant ces fichiers.", { tone: "green" }); }
      catch (e) { toast(errMsg(e), { tone: "red" }); }
    } else app.openSettings("poste");
  };
  return <button className={`btn ghost ${compact ? "xs" : "sm"}`} onClick={() => void act()} data-testid="fix-action">{fix.bouton}</button>;
}

function CausesCard({ ov, go, running }: { ov: Overview | null; go: Props["go"]; running: boolean }) {
  void running;
  const top = (ov?.causes || []).slice(0, 7);
  return (
    <section className="card ix-causes-card" data-testid="index-causes">
      <div className="ix-ctitle"><span className="h3">Anomalies par cause</span>
        <span className="muted small">{ov ? `${fr(ov.anomalies.a_traiter)} · ${pl(ov.anomalies.causes, "cause")}` : ""}</span></div>
      {ov?.fix && (
        <div className="alert t-amber ix-fixline" data-testid="index-fix"><Icon name="wrench" size={15} />
          <span className="grow"><b>1 correction règle {pl(ov.fix.n, "anomalie")}</b> : {ov.fix.titre}.</span><FixAction fix={ov.fix} compact /></div>
      )}
      <div className="ix-czlist">
        {top.map((c) => (
          <button key={c.code} className="ix-cz" onClick={() => go("anomalies", c.code)} title={`${c.label} : ${pl(c.a_traiter_fichiers, "fichier")} — ${c.fix}`}>
            <i className={`d t-${NIV_TONE[c.niveau]}`} /><span className="ellipsis">{c.label}</span><b>{fr(c.a_traiter)}</b>
          </button>
        ))}
        {ov && !top.length && <div className="alert t-green"><Icon name="check" size={15} />Aucune anomalie à traiter.</div>}
      </div>
      <button className="btn secondary sm" style={{ alignSelf: "flex-start" }} onClick={() => go("anomalies")} data-testid="index-triage">Trier les anomalies ›</button>
    </section>
  );
}

// ------------------------------------------------------------------ maintenance (intégrité) et zone rouge (vider l'index)
function Maintenance({ ov, running, refresh, onReset }: { ov: Overview | null; running: boolean; refresh: () => void; onReset: () => void }) {
  const app = useApp();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const integrity = async () => {
    setBusy(true);
    try {
      const r = await call<{ orphelins: number }>("check_integrity");
      if (r.orphelins) toast(`${r.orphelins} lien(s) orphelin(s) trouvé(s) : fichiers déplacés, renommés ou supprimés (voir les anomalies). Relancez une indexation différentielle sur le nouveau dossier : seuls les fichiers déplacés seront relus.`, { tone: "amber", ms: 15000 });
      else toast("Intégrité vérifiée : tous les fichiers indexés sont présents.");
      refresh();
      app.refreshStats();
    } catch (e) { toast(errMsg(e), { tone: "red" }); } finally { setBusy(false); }
  };
  const integ = ov?.integrite;
  const label = useMemo(() => (integ?.verifiee ? `vérifiée le ${(integ.date || "").slice(0, 5)}` : "jamais vérifiée"), [integ]);
  return (
    <section className="card ix-maint" data-testid="index-maintenance">
      <div className="ix-ctitle"><span className="h3">Maintenance</span></div>
      <div className="row small">
        <span className="grow"><Icon name="link" size={14} /><span title="Fichiers indexés déplacés, renommés ou supprimés depuis leur lecture">Intégrité : {label} · <b>{fr(integ?.orphelins)}</b> {(integ?.orphelins || 0) > 1 ? "liens orphelins" : "lien orphelin"}</span></span>
        <button className="btn ghost xs" onClick={() => void integrity()} disabled={running || busy} data-testid="index-integrity" title="Fichiers indexés déplacés, renommés ou supprimés">{busy ? "Vérification…" : "Vérifier"}</button>
      </div>
      <div className="row small ix-danger" title="Retire tous les relevés indexés et les aperçus en cache, puis relit toutes les archives. Le journal, le registre des délivrances et les demandes sont conservés.">
        <span className="grow"><Icon name="trash" size={14} /><span>Vider l'index et tout relire</span></span>
        <button className="btn danger xs" disabled={running} onClick={onReset} data-testid="index-reset" title={running ? "Arrêtez d'abord l'indexation en cours" : "Recommencer de zéro (confirmation demandée)"}>Vider l'index…</button>
      </div>
    </section>
  );
}
