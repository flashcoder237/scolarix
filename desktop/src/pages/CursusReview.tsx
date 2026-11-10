import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { call, sx, plural, statutTone, FIL_SHORT, type Releve, type ReleveDetail } from "../api";
import { useShortcuts } from "../state";
import { Badge, Icon, Popover, Switch, play, useToast } from "../components/ui";
import { PdfView, forget, type PdfViewHandle, type PdfViewState } from "../components/PdfView";
import { OpProgress, ReviewFocusBar, ReviewShell, ViewControls, useReviewNav } from "../components/ReviewShell";
import { Thumbs, type ThumbItem } from "../components/Thumbs";
import { isCancelled, useOperation } from "../operation";
import { periodLabel } from "./Guichet";
import { typing } from "./DemandeReview";

// Cursus complet : vue plein cadre qui remplace l'ancienne fenêtre. On voit le PDF fusionné page par page
// (page de garde comprise) avant de l'imprimer ou de l'exporter ; l'aperçu est fait par le moteur sans réserver
// de numéro de délivrance (cursus_build, mode « preview »), avec les mêmes conversions Word que l'impression.

interface Doublon { id: number; chemin: string; pages_label: string; type_fichier: string; annee_academique: string | null; meme_contenu: boolean | null; version_corrigee: boolean; lien: string }
type CRel = Releve & { doublons?: Doublon[]; pages_fichier?: string };
interface CursusInfo {
  matricule: string; nom: string; releves: CRel[]; missing: string[];
  correlated: { matricule: string; filiere: string; n: number; meme_naissance: boolean }[];
  correlated_releves: CRel[]; sans_matricule: CRel[];
}
interface Preview { path: string; pages: number; missing: string[]; layout: { releve_id: number | null; first: number; last: number }[] }
interface Det { detail?: ReleveDetail; render: "" | "en cours" | "échec"; err?: string }

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const isRatt = (r: Releve) => (r.session || "").toLowerCase().startsWith("rattrap");
const order = (r: Releve) => [r.niveau_num, r.semestre || 0, r.annee_academique || "", isRatt(r) ? 1 : 0];
const cmp = (a: Releve, b: Releve) => { const x = order(a), y = order(b); for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1; return 0; };
const base = (p: string) => p.split(/[\\/]/).pop() || p;
const samePeriod = (a: Releve, b: Releve) => a.niveau === b.niveau && (a.semestre || 0) === (b.semestre || 0) && (a.semestre_fin || 0) === (b.semestre_fin || 0) && !!a.semestre;
const pagesTxt = (l: { first: number; last: number }) => (l.last > l.first ? `p. ${l.first}–${l.last}` : `p. ${l.first}`);

export function CursusReview({ matricule, onClose, backLabel = "Guichet" }: { matricule: string; onClose: () => void; backLabel?: string }) {
  const toast = useToast();
  const [info, setInfo] = useState<CursusInfo | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [choice, setChoice] = useState<Record<number, number>>({});      // exemplaire choisi pour un relevé en plusieurs exemplaires
  const [extra, setExtra] = useState<Set<number>>(new Set());             // relevés sans matricule inclus par l'agent
  const [link, setLink] = useState(false);
  const [cover, setCover] = useState(true);
  const [dup, setDup] = useState(false);
  const [sel, setSel] = useState("garde");
  const [view, setView] = useState<"releve" | "dossier">("dossier");
  const [peek, setPeek] = useState<CRel | null>(null);                    // « Voir » un relevé hors de la liste
  const [preview, setPreview] = useState<Preview | null>(null);
  const [pvErr, setPvErr] = useState("");
  const [building, setBuilding] = useState(false);
  const [queued, setQueued] = useState<"" | "print" | "export">("");
  const [busy, setBusy] = useState<"" | "export" | "print">("");
  const pvOp = useOperation();
  const opn = useOperation();
  const [dets, setDets] = useState<Record<number, Det>>({});
  const [jump, setJump] = useState(0);
  const [visPage, setVisPage] = useState(1);
  const [focus, setFocusRaw] = useState(false);
  const setFocus = (f: boolean) => { setFocusRaw(f); void sx.setFullScreen?.(f).catch(() => undefined); };
  const pdf = useRef<PdfViewHandle>(null);
  const [pv, setPv] = useState<PdfViewState>({ fit: "auto", scale: null, overflow: false });
  const [mmHost, setMmHost] = useState<HTMLDivElement | null>(null);

  useEffect(() => {
    call<CursusInfo>("cursus_info", { matricule })
      .then((i) => { setInfo(i); setChecked(new Set(i.releves.filter((r) => r.selected).map((r) => r.id))); })
      .catch((e) => { toast(errMsg(e), { tone: "red" }); onClose(); });
  }, [matricule]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { void sx.setFullScreen?.(false).catch(() => undefined); }, []);

  const rows: CRel[] = useMemo(() => !info ? [] : [...info.releves, ...(link ? info.correlated_releves : []),
    ...info.sans_matricule.filter((r) => extra.has(r.id))].sort(cmp), [info, link, extra]);
  const eff = useCallback((r: CRel) => choice[r.id] ?? r.id, [choice]);
  const selected = rows.filter((r) => checked.has(r.id));
  const ids = selected.map(eff);
  const keys = ["garde", ...rows.map((r) => `r${r.id}`)];
  const curRow = rows.find((r) => `r${r.id}` === sel) || null;
  const layoutOf = (r: CRel) => preview?.layout.find((l) => l.releve_id === eff(r));

  // ---------------------------------------------------------------- aperçu fusionné (moteur, sans numéro de délivrance)
  const seq = useRef(0);
  const buildKey = `${ids.join(",")}|${cover ? 1 : 0}|${dup ? 1 : 0}`;
  useEffect(() => {
    if (!info) return;
    const my = ++seq.current;
    if (!ids.length) { setPreview(null); setPvErr(""); setBuilding(false); void pvOp.cancel(); return; }
    const t = window.setTimeout(async () => {
      await pvOp.cancel();                       // sélection changée pendant un aperçu : on repart de la nouvelle
      if (my !== seq.current) return;
      const op = pvOp.start("cursus-apercu");
      setBuilding(true);
      setPvErr("");
      try {
        const r = await call<Preview>("cursus_build", { matricule: info.matricule, releve_ids: ids, cover, duplicata: dup, mode: "preview", op });
        if (my === seq.current) { forget(r.path); setPreview(r); }
      } catch (e) {
        if (my === seq.current && !isCancelled(e)) { setPreview(null); setPvErr(errMsg(e)); }
      } finally {
        if (my === seq.current) { setBuilding(false); pvOp.finish(); }
      }
    }, 300);
    return () => window.clearTimeout(t);
  }, [buildKey, info]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- vue Relevé : aperçu du relevé source
  const shownRel: CRel | null = peek || curRow;
  const shownId = shownRel ? (peek ? peek.id : eff(shownRel)) : null;
  useEffect(() => {
    if (view !== "releve" || !shownId || dets[shownId]?.detail || dets[shownId]?.render === "échec") return;
    let alive = true;
    const upd = (v: Det) => alive && setDets((x) => ({ ...x, [shownId]: v }));
    void (async () => {
      try {
        const d = await call<ReleveDetail>("releve", { releve_id: shownId, consult: true });
        upd({ detail: d, render: d.needs_render ? "en cours" : "" });
        if (d.needs_render) {
          const d2 = await call<ReleveDetail>("preview", { releve_id: shownId });
          if (d2.preview_pdf) forget(d2.preview_pdf);
          upd({ detail: d2, render: "" });
        }
      } catch (e) { upd({ render: "échec", err: errMsg(e) }); }
    })();
    return () => { alive = false; };
  }, [view, shownId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- impression, export
  const run = async (mode: "export" | "print") => {
    if (!info || !ids.length || busy) return;
    if (building) { setQueued(mode); toast(`Aperçu en préparation : ${mode === "print" ? "l'impression partira" : "l'export se fera"} dès qu'il sera prêt.`, { tone: "blue", key: "op" }); return; }
    setBusy(mode);
    const op = opn.start("cursus");
    toast("Préparation du dossier fusionné…", { tone: "blue", ms: 8000, sound: false, key: "op" });
    try {
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
  useEffect(() => {
    if (!queued || building) return;
    const m = queued;
    setQueued("");
    if (preview) void run(m);
  }, [queued, building]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- sélection, pages
  const toggle = (r: CRel) => setChecked((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n; });
  const goPage = (p: number) => { setJump(0); window.requestAnimationFrame(() => setJump(p)); };
  const pick = (key: string) => {
    setSel(key);
    setPeek(null);
    if (view !== "dossier" || !preview) return;
    if (key === "garde") { if (cover) goPage(1); return; }
    const r = rows.find((x) => `r${x.id}` === key);
    const l = r && layoutOf(r);
    if (l) goPage(l.first);
  };
  const pageOwner = (p: number) => preview?.layout.find((l) => p >= l.first && p <= l.last) || null;
  const onVisible = (_d: string, p: number) => {
    setVisPage(p);
    const l = pageOwner(p);
    if (!l) return;
    const key = l.releve_id == null ? "garde" : `r${rows.find((r) => eff(r) === l.releve_id)?.id}`;
    if (key !== sel && !peek) setSel(key);
  };
  const go = (step: number) => { const i = keys.indexOf(sel); const j = Math.max(0, Math.min(keys.length - 1, i + step)); pick(keys[j]); };
  const pageStep = (step: number) => { if (preview) goPage(Math.max(1, Math.min(preview.pages, visPage + step))); };
  const presetValides = () => info && setChecked(new Set([...info.releves.filter((r) => r.selected).map((r) => r.id), ...[...checked].filter((i) => extra.has(i))]));
  const presetTous = () => setChecked(new Set(rows.map((r) => r.id)));
  const isPresetValides = !!info && rows.every((r) => checked.has(r.id) === (!!r.selected || (extra.has(r.id) && checked.has(r.id))));

  useReviewNav(() => { if (busy) return false; onClose(); });
  useShortcuts({ "escape": () => { if (busy) void opn.cancel(); else if (building) void pvOp.cancel(); else if (focus) setFocus(false); else if (peek) setPeek(null); else onClose(); } });
  useShortcuts({
    "arrowdown": (e) => (typing(e) ? false : go(1)), "arrowup": (e) => (typing(e) ? false : go(-1)),
    " ": (e) => {
      if (typing(e) || (e.target as HTMLElement)?.tagName === "BUTTON") return false;
      if (sel === "garde") setCover((c) => !c); else if (curRow) toggle(curRow);
    },
    "pagedown": () => pageStep(1), "pageup": () => pageStep(-1),
    "v": (e) => (typing(e) ? false : setView((v) => (v === "releve" ? "dossier" : "releve"))),
    "enter": (e) => (typing(e) || /^(BUTTON|A)$/.test((e.target as HTMLElement)?.tagName || "") ? false : setFocus(!focus)),
    "f11": () => setFocus(!focus),
    "alt+1": () => pdf.current?.goZone("identite"), "alt+2": () => pdf.current?.goZone("notes"), "alt+3": () => pdf.current?.goZone("decision"),
    "ctrl+p": () => void run("print"), "ctrl+e": () => void run("export"),
  }, !!info);

  // ---------------------------------------------------------------- rendu
  const pages = preview?.pages ?? selected.reduce((n, r) => n + r.nb_pages, 0) + (cover ? 1 : 0);
  const exemplaires = rows.filter((r) => (r.doublons || []).length > 0);
  const valid = info ? info.releves : [];
  const years = valid.map((r) => r.annee_academique || "").filter(Boolean).sort();
  const dob = valid.find((r) => r.date_connue)?.date_connue;
  const fil = valid[0] ? FIL_SHORT[valid[0].filiere_code || ""] || valid[0].filiere || "" : "";
  const owner = pageOwner(visPage);
  const ownerRow = owner?.releve_id != null ? rows.find((r) => eff(r) === owner.releve_id) : null;

  const reason = (r: CRel): string => {
    if (r.matricule.startsWith("ILLISIBLE")) return "matricule illisible : à identifier à l'écran";
    const same = rows.filter((x) => x.id !== r.id && samePeriod(x, r) && checked.has(x.id));
    if (!r.is_success && same.some(isRatt)) return "remplacé par la session de rattrapage";
    if (!r.is_success && same.some((x) => x.is_success)) return "remplacé par le relevé validé de la même période";
    if (r.is_success && same.length) return "un autre relevé de la même période est coché";
    if (!r.is_success) return `non validé${r.decision ? ` (« ${r.decision.slice(0, 30)} »)` : ""} : non inclus par défaut`;
    return "décoché";
  };
  const progressFor = (r: CRel): { text: string; tone: string; spin?: boolean } | null => {
    if (!checked.has(r.id)) return null;
    const pr = pvOp.progress;
    if (building) {
      if (!pr) return { text: "en attente", tone: "muted" };
      if (pr.phase !== "conversion") return { text: "contrôle…", tone: "c-blue", spin: true };
      const m = /Conversion \d+\/\d+ : (.+?)…?$/.exec(pr.message);
      if (m && m[1] === base(r.chemin)) return { text: "conversion…", tone: "c-blue", spin: true };
      return { text: "…", tone: "muted" };
    }
    const l = layoutOf(r);
    if (l) return { text: pagesTxt(l), tone: "" };
    if (preview) return { text: "écarté", tone: "c-red" };
    return null;
  };

  const thumbW = window.innerWidth >= 1700 ? 84 : 66;
  const thumbs: ThumbItem[] = preview ? Array.from({ length: preview.pages }, (_, k) => {
    const p = k + 1, l = pageOwner(p);
    const r = l?.releve_id != null ? rows.find((x) => eff(x) === l.releve_id) : null;
    return { key: `p${p}`, file: preview.path, version: preview.path, page: p, label: <b>{p}</b>,
      sub: !l ? "" : l.releve_id == null ? "garde" : p > l.first ? `${r ? periodLabel(r).replace(/^N(\d+) · /, "N$1 ") : ""} suite` : r ? periodLabel(r) : "" };
  }) : Array.from({ length: Math.min(12, pages) }, (_, k) => ({ key: `w${k}`, file: null, page: k + 1, label: <b>{k + 1}</b> }));

  if (!info) return (
    <ReviewShell label="Cursus complet" testid="cursus-review" backLabel={backLabel} onBack={onClose} icon="layers" tone="blue" eyebrow="Cursus complet" title="…" mat={matricule}
      side={<div className="empty"><span className="spinner" /></div>}><div className="gx-stage"><div className="empty"><span className="spinner" /></div></div></ReviewShell>
  );

  const det = shownId ? dets[shownId] : undefined;
  const d2 = det?.detail && det.detail.releve.id === shownId ? det.detail : null;
  const relMsg = !shownRel ? (sel === "garde" ? "La page de garde fait partie du dossier fusionné : vue Dossier (V)." : "Choisissez un relevé.")
    : det?.render === "échec" ? `Aperçu impossible : ${det.err}` : !d2 ? "Chargement de l'aperçu…" : d2.needs_render ? "Préparation de l'aperçu Word…" : null;
  const dosMsg = !ids.length ? "Cochez au moins un relevé : l'aperçu du dossier fusionné s'affiche ici."
    : pvErr ? pvErr : !preview ? "Préparation de l'aperçu fusionné…" : null;

  const primaryLabel = `${plural(pages, "page")}${cover ? " · page de garde" : ""}`;
  const dock = (
    <div className="grp">
      {busy ? (
        <>
          <button className="btn primary" disabled data-testid="cursus-print"><span className="r"><span className="spinner" />{busy === "print" ? "Impression…" : "Export…"}</span>
            <span className="sub">{opn.progress?.message || "fusion contrôlée"}</span></button>
          <button className="btn ghost sm" disabled={opn.cancelling} onClick={() => void opn.cancel()} data-testid="cursus-cancel"><Icon name="x" size={14} />Annuler<span className="kbd">Échap</span></button>
        </>
      ) : (
        <>
          <button className="btn primary" disabled={!ids.length} onClick={() => void run("print")} data-testid="cursus-print" title="Imprimer le dossier fusionné (Ctrl+P)">
            <span className="r">{building ? <span className="spinner" /> : <Icon name="printer" size={18} />}Imprimer</span>
            <span className="sub">{queued === "print" ? "dès que l'aperçu est prêt" : building ? "aperçu en cours ; impression ensuite" : primaryLabel}</span><span className="kbd">Ctrl+P</span></button>
          <button className="btn secondary sm" disabled={!ids.length} onClick={() => void run("export")} data-testid="cursus-export" title="Fusionner et exporter en PDF (Ctrl+E)">
            <Icon name="download" size={15} />Exporter<span className="kbd">Ctrl+E</span></button>
        </>
      )}
      <Switch checked={dup} onChange={setDup} label="Duplicata" title="Timbre « Duplicata conforme » sur chaque relevé du dossier" />
    </div>
  );

  return (
    <ReviewShell label="Cursus complet" testid="cursus-review" backLabel={backLabel} icon="layers" tone="blue"
      onBack={() => (busy ? toast("Opération en cours : Échap ou « Annuler » d'abord.", { tone: "amber", ms: 4000 }) : onClose())}
      eyebrow="Cursus complet" title={info.nom} mat={info.matricule}
      facts={<>{fil}{years.length ? ` · ${years[0].slice(0, 4)} → ${years[years.length - 1].slice(-4)}` : ""}{dob ? ` · né(e) le ${dob}` : ""}</>}
      right={<span className="rv-sum" data-testid="cursus-summary"><Icon name="doc" size={14} />Dossier fusionné : {plural(ids.length, "relevé")} · {plural(pages, "page")}</span>}
      side={<>
        {(info.missing.length > 0 || info.sans_matricule.length > 0) && (
          <div className="rv-alert t-amber" data-testid="cursus-missing">
            <div className="row" style={{ gap: 6 }}><Icon name="alert" size={15} />
              <b className="grow">{info.missing.length ? `${info.missing.join(", ")} absent${info.missing.length > 1 ? "s" : ""} du fonds` : "Relevé sans matricule au même nom"}</b>
              {info.missing.length > 0 && cover && <span className="tiny nowrap">signalé p. 1</span>}</div>
            {info.sans_matricule.length > 0 ? info.sans_matricule.map((r) => (
              <div key={r.id} className="small">
                <div>Un relevé <b>sans matricule</b> au même nom {info.missing.length ? "pourrait les couvrir" : "existe"} : {periodLabel(r)} · {r.annee_academique || "?"}.</div>
                <div className="row" style={{ gap: 6, marginTop: 4 }}>
                  <button className="btn ghost xs" onClick={() => { setPeek(r); setView("releve"); }} data-testid="sans-matricule-voir"><Icon name="eye" size={13} />Voir</button>
                  <button className="btn secondary xs" disabled={extra.has(r.id)} onClick={() => { setExtra((s) => new Set(s).add(r.id)); setChecked((s) => new Set(s).add(r.id)); setPeek(null); setSel(`r${r.id}`); }}
                    title="À n'inclure qu'après avoir vérifié à l'écran que c'est bien le même étudiant (le contrôle avant fusion s'applique)"><Icon name="plus" size={13} />{extra.has(r.id) ? "Inclus" : "Inclure"}</button>
                </div>
              </div>
            )) : <div className="small">Signalé{info.missing.length > 1 ? "s" : ""} sur la page de garde.</div>}
          </div>
        )}
        {info.correlated.length > 0 && (
          <div className="rv-alert t-gray" data-testid="cursus-correlated">
            <div className="row" style={{ gap: 6 }}><Icon name="link" size={15} /><b className="grow">Même personne ?</b>
              <Switch blue checked={link} onChange={setLink} label="Inclure" title="Ajouter à la liste les relevés de ces autres matricules (même nom)" /></div>
            {info.correlated.map((c) => {
              const first = info.correlated_releves.find((r) => r.matricule === c.matricule);
              return (
                <div key={c.matricule} className="small"><span className="mono">{c.matricule}</span> · {c.filiere || "?"} · {plural(c.n, "relevé")}{c.meme_naissance ? " · même date de naissance" : ""}
                  {first && <> · <button className="btn link tiny" onClick={() => { setPeek(first); setView("releve"); }}>Voir</button></>}</div>
              );
            })}
          </div>
        )}
        <div className="row rv-head">
          <div className="grow"><div className="eyebrow">Ordre du cursus</div><div className="h3" data-testid="cursus-count">{selected.length} / {rows.length} relevés cochés</div></div>
          <div className="seg sm" role="radiogroup" aria-label="Sélection rapide">
            <button className={isPresetValides ? "on" : ""} role="radio" aria-checked={isPresetValides} onClick={presetValides} title="Sélection par défaut : dernier relevé validé de chaque période">Validés</button>
            <button className={selected.length === rows.length ? "on" : ""} role="radio" aria-checked={selected.length === rows.length} onClick={presetTous} title="Tout cocher">Tous</button>
          </div>
        </div>
        {building && <OpProgress title="Aperçu fusionné" progress={pvOp.progress} cancelling={pvOp.cancelling} onCancel={() => void pvOp.cancel()} testid="cursus-progress" />}
        <div className="rv-items" role="listbox" aria-label="Relevés du cursus" data-testid="cursus-rows">
          <div className={`ditem cv${sel === "garde" ? " sel" : ""}${cover ? "" : " off"}`} onClick={() => pick("garde")} role="option" aria-selected={sel === "garde"} data-testid="cursus-row">
            <input type="checkbox" checked={cover} onChange={() => setCover((c) => !c)} onClick={(e) => { e.stopPropagation(); setSel("garde"); setPeek(null); }} aria-label="Page de garde" />
            <span className="dn">·</span>
            <div className="dm"><div className="d1"><b>Page de garde</b></div><div className="d2">liste des relevés{info.missing.length ? ` · signale ${info.missing.join("-")}` : ""}</div></div>
            <div className="dr"><span className="dst">{cover ? (preview && preview.layout[0]?.releve_id == null ? pagesTxt(preview.layout[0]) : "p. 1") : "non incluse"}</span></div>
          </div>
          {rows.map((r) => {
            const on = checked.has(r.id), k = selected.indexOf(r);
            const pr = progressFor(r);
            const chosen = choice[r.id];
            return (
              <div key={r.id} className={`ditem cv${sel === `r${r.id}` ? " sel" : ""}${on ? "" : " off"}`} onClick={() => pick(`r${r.id}`)} role="option" aria-selected={sel === `r${r.id}`} data-testid="cursus-row">
                <input type="checkbox" checked={on} onChange={() => toggle(r)} onClick={(e) => { e.stopPropagation(); setSel(`r${r.id}`); setPeek(null); }} aria-label={`Inclure ${r.periode}`} />
                <span className="dn">{on ? k + 1 : "–"}</span>
                <div className="dm">
                  <div className="d1"><b className={on ? "nowrap" : "nowrap strike"}>{periodLabel(r)}</b>{isRatt(r) && <Badge text="RATT." tone="amber" />}
                    {r.matricule !== info.matricule && <span className="mono tiny" title="Autre matricule">{r.matricule.startsWith("ILLISIBLE") ? "sans matricule" : r.matricule}</span>}</div>
                  <div className="d2">{r.annee_academique || "?"} · {plural(r.nb_pages, "page")}<span className="opt-wide"> · source {r.nb_pages > 1 ? `p. ${r.numero_page}–${r.page_fin}` : `p. ${r.numero_page}`}</span></div>
                  {!on && <div className="d3 c-amber">{reason(r)}</div>}
                  {(r.doublons || []).length > 0 && <Exemplaires r={r} chosen={chosen} onChoose={(id) => setChoice((c) => ({ ...c, [r.id]: id }))} />}
                </div>
                <div className="dr">
                  <Badge text={r.statut || "—"} tone={statutTone(r.statut)} dot />
                  {pr ? <span className={`dst ${pr.tone}`} title={pr.text === "écarté" ? preview?.missing.join("\n") : undefined}>{pr.spin && <span className="spinner" />}{pr.text}</span>
                    : !on && <span className="dst muted">non inclus</span>}
                </div>
              </div>
            );
          })}
        </div>
        <div className="muted tiny gx-hints"><span className="key">↑</span><span className="key">↓</span> relevé · <span className="key">Espace</span> cocher · <span className="key">Page↑↓</span> pages · <span className="key">V</span> vue · <span className="key">Ctrl+E</span> exporter</div>
      </>}>
      <div className="card strip" data-testid="cursus-strip">
        {view === "dossier" ? <>
          <Badge text="APERÇU FUSIONNÉ" tone="blue" big />
          <span className="nm nowrap">{preview ? `Page ${visPage} / ${preview.pages}` : "Aperçu"}{owner ? ` · ${owner.releve_id == null ? "Page de garde" : ownerRow ? periodLabel(ownerRow) : ""}` : ""}</span>
          <span className="facts opt-mid">ce qui sortira de l'imprimante, dans l'ordre<span className="opt-wide"><i>·</i>chaque relevé contrôlé avant fusion</span></span>
        </> : <>
          <Badge text={peek ? "HORS DOSSIER" : "RELEVÉ SOURCE"} tone={peek ? "amber" : "gray"} big />
          <span className="nm nowrap">{shownRel ? `${periodLabel(shownRel)} · ${shownRel.annee_academique || "?"}` : sel === "garde" ? "Page de garde" : "—"}</span>
          {shownRel && <span className="facts"><span className="mono">{shownRel.matricule}</span><i>·</i>{shownRel.nom_complet}{peek && <><i>·</i>vérifiez que c'est bien le même étudiant</>}</span>}
          {peek && <button className="btn link tiny" onClick={() => setPeek(null)}>Revenir au dossier</button>}
        </>}
        <span className="grow" />
        {info.missing.length > 0 && <span className="achip t-amber" title={`Périodes absentes du fonds : ${info.missing.join(", ")}`}><Icon name="alert" size={13} />{info.missing.join(", ")} manquant{info.missing.length > 1 ? "s" : ""}</span>}
        {exemplaires.map((r) => <span key={r.id} className="achip t-amber opt-mid" title="Plusieurs exemplaires de ce relevé dans le fonds : choisissez lequel part"><Icon name="alert" size={13} />{periodLabel(r).split(" · ")[0]} : {(r.doublons || []).length + 1} exemplaires</span>)}
        {preview && preview.missing.length > 0 && <span className="achip t-red" title={preview.missing.join("\n")}><Icon name="alert" size={13} />{plural(preview.missing.length, "relevé écarté", "relevés écartés")}</span>}
      </div>
      <div className={`gx-stage${focus ? " focus" : ""}`} tabIndex={-1} data-testid="stage" aria-label="Aperçu du cursus">
        {focus && <ReviewFocusBar onExit={() => setFocus(false)}>
          <Badge text="CURSUS" tone="blue" dot /><b className="nm ellipsis">{info.nom}</b><span className="mono">{info.matricule}</span>
          <span className="muted small nowrap">{preview ? `page ${visPage}/${preview.pages}` : ""}</span>
          <button className="btn primary sm" disabled={!ids.length || !!busy} onClick={() => void run("print")}><Icon name="printer" size={16} />Imprimer<span className="kbd">Ctrl+P</span></button>
          <button className="btn ghost sm" disabled={!ids.length || !!busy} onClick={() => void run("export")}><Icon name="download" size={14} />Exporter</button>
        </ReviewFocusBar>}
        {view === "dossier" && !focus && (
          <div className="cthumbs">
            <Thumbs items={thumbs} width={thumbW} selected={preview ? `p${visPage}` : null} label="Pages du dossier fusionné" testid="cursus-thumbs"
              onPick={(it) => { if (preview) { goPage(it.page); const l = pageOwner(it.page); if (l) setSel(l.releve_id == null ? "garde" : `r${rows.find((r) => eff(r) === l.releve_id)?.id}`); } }} />
          </div>
        )}
        <div className="gx-view">
          {view === "dossier" ? (
            <PdfView ref={pdf} file={preview?.path ?? null} first={1} last={preview?.pages || 1} version={preview?.path || ""} message={dosMsg} jumpTo={jump || undefined}
              onVisible={onVisible} defaultFit="auto" toolbar={false} minimapHost={null} onState={setPv} onDoubleClick={() => setFocus(!focus)} />
          ) : (
            <PdfView ref={pdf} file={d2?.preview_pdf ?? null} first={d2?.releve.numero_page || 1} last={d2?.releve.page_fin || 1} version={`${shownId}`}
              message={relMsg} stamp={dup && !peek} defaultFit="auto" toolbar={false} minimapHost={mmHost} onState={setPv} onDoubleClick={() => setFocus(!focus)} />
          )}
        </div>
        {!focus && <aside className="dock" data-testid="dock" aria-label="Actions et affichage">
          {dock}
          <ViewControls pv={pv} pdf={pdf} onFocus={() => setFocus(true)} setMm={setMmHost}
            views={[["releve", "page1", "Relevé", "Le relevé choisi, fichier source"], ["dossier", "pages", "Dossier", "Le PDF fusionné, page par page"]]}
            view={view} setView={(v) => { setView(v as "releve" | "dossier"); if (v === "dossier") setPeek(null); }} />
        </aside>}
      </div>
    </ReviewShell>
  );
}

function Exemplaires({ r, chosen, onChoose }: { r: CRel; chosen?: number; onChoose: (id: number) => void }) {
  const [open, setOpen] = useState(false);
  const all = [{ id: r.id, chemin: r.chemin, pages_label: r.pages_fichier || r.pages_label, annee_academique: r.annee_academique, lien: "exemplaire retenu par l'index", version_corrigee: false, meme_contenu: true as boolean | null },
    ...(r.doublons || [])];
  const cur = chosen ?? r.id;
  return (
    <div className="d3 anchor" onClick={(e) => e.stopPropagation()}>
      <span className="c-amber">{all.length} exemplaires dans le fonds · {cur === r.id ? "le retenu par défaut" : "autre exemplaire choisi"}</span>{" "}
      <button className="btn link tiny" onClick={() => setOpen((o) => !o)} data-testid="exemplaire-choisir">choisir</button>
      <Popover open={open} onClose={() => setOpen(false)} align="left" width={380} label="Choisir l'exemplaire" testid="exemplaires">
        <div className="pop-head"><div className="eyebrow">Exemplaire qui part au dossier</div></div>
        {all.map((x) => (
          <button key={x.id} className={`mi${cur === x.id ? " on" : ""}`} role="radio" aria-checked={cur === x.id} onClick={() => { onChoose(x.id); setOpen(false); }}
            title={x.chemin}>
            <span className="grow" style={{ minWidth: 0 }}>
              <span className="ellipsis" style={{ display: "block" }}>{base(x.chemin)}</span>
              <span className="tiny muted">{x.pages_label} · {x.annee_academique || "?"} · {x.lien}{x.version_corrigee ? " · version corrigée" : ""}{x.meme_contenu === false ? " · contenu différent" : ""}</span>
            </span>
          </button>
        ))}
      </Popover>
    </div>
  );
}
