import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { call, sx, plural, statutTone, type Demande, type DemandeItem, type ReleveDetail, type Tone } from "../api";
import { useShortcuts } from "../state";
import { Badge, Icon, Popover, Switch, play, useDialogs, useToast } from "../components/ui";
import { PdfView, forget, type PdfDoc, type PdfViewHandle, type PdfViewState } from "../components/PdfView";
import { OpProgress, RIcon, ReviewFocusBar, ReviewShell, ViewControls, useReviewNav } from "../components/ReviewShell";
import { Stepper, hourLabel, type Step } from "../components/Stepper";
import { Thumbs, usePageCount, type ThumbItem } from "../components/Thumbs";
import { isCancelled, useOperation } from "../operation";
import { creditsLabel, periodLabel } from "./Guichet";

// Revue de la demande : vue plein cadre qui remplace l'ancienne fenêtre de 980 px. On imprime ce qu'on a vu :
// pièces dans l'ordre d'impression (bordereau en tête), aperçu du relevé source puis du fichier préparé,
// coche « contrôlé à l'écran » journalisée, étapes Préparer → Imprimer → Remettre avec leur heure.
// Le même état (useDemandeReview) sert au détail de la page Demandes.

export const STATUT_TONE: Record<string, Tone> = { "EN COURS": "blue", "PRÊTE": "violet", "IMPRIMÉE": "amber", "REMISE": "green", "ANNULÉE": "gray" };
export const STATUT_AIDE: Record<string, string> = { "EN COURS": "à préparer", "PRÊTE": "dossier prêt, à imprimer", "IMPRIMÉE": "à remettre", "REMISE": "remise au demandeur", "ANNULÉE": "annulée" };
export const MOTIFS = ["Inscription / poursuite d'études", "Concours", "Emploi / stage", "Équivalence / études à l'étranger", "Bourse", "Dossier personnel", "Autre"];
export const STEP_IDX: Record<string, number> = { "EN COURS": 0, "PRÊTE": 1, "IMPRIMÉE": 2, "REMISE": 3, "ANNULÉE": -1 };
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const base = (p: string | null | undefined) => (p || "").split(/[\\/]/).pop() || "";
const shortRef = (ref: string | null | undefined) => (ref || "").split("/")[0];

export interface ReviewInfo {
  controles: number[];
  etapes: { recue: string | null; preparer: string | null; imprimer: string | null; remettre: string | null; annuler: string | null };
  bordereau: string | null;
  a_imprimer: string | null;
  pages: number;
}
export type RvItem = DemandeItem & { prepared?: boolean };
export interface DemandeRv { demande: Demande; items: RvItem[]; review: ReviewInfo }
export type RvView = "releve" | "dossier" | "planche";
export interface Piece { key: string; kind: "bordereau" | "releve"; n: number; it?: RvItem }
interface Det { detail?: ReleveDetail; render: "" | "en cours" | "échec"; err?: string }

export const VIEWS: [string, string, string, string][] = [
  ["releve", "page1", "Relevé", "Une pièce à la fois"], ["dossier", "pages", "Dossier", "Toutes les pages, dans l'ordre d'impression"],
  ["planche", "grid", "Planche", "Vignettes de tout le dossier"],
];

export function stepsOf(d: DemandeRv, busy = ""): Step[] {
  const st = d.demande.statut, e = d.review.etapes, i = STEP_IDX[st] ?? 0;
  const n = d.items.length;
  return [
    { label: "Préparer", sub: e.preparer ? `${hourLabel(e.preparer)} · ${d.review.pages} p.` : busy ? "en cours…" : i === 0 ? `à faire · ${plural(n, "relevé")}` : null },
    { label: "Imprimer", sub: e.imprimer ? hourLabel(e.imprimer) : i === 1 ? "prochaine étape" : null },
    { label: "Remettre", sub: e.remettre ? hourLabel(e.remettre) : i === 2 ? "prochaine étape" : null },
  ];
}

// ------------------------------------------------------------------ état partagé (revue plein cadre, page Demandes)
export function useDemandeReview(did: number | null, onChanged?: () => void) {
  const toast = useToast();
  const { ask, prompt } = useDialogs();
  const [d, setD] = useState<DemandeRv | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [view, setView] = useState<RvView>("releve");
  const [bordereau, setBordereau] = useState(true);
  const [busy, setBusy] = useState<"" | "prepare" | "print">("");
  const opn = useOperation();
  const [dets, setDets] = useState<Record<number, Det>>({});
  const detsRef = useRef(dets);
  detsRef.current = dets;
  const inflight = useRef(new Set<number>());
  const [bord, setBord] = useState<{ sig: string; path: string | null; err?: string } | null>(null);
  const [demandeur, setDemandeur] = useState("");
  const [motif, setMotif] = useState("");
  const [remisA, setRemisA] = useState("");
  const fieldsFor = useRef<number | null>(null);
  const changed = useRef(onChanged);
  changed.current = onChanged;

  const load = useCallback(async () => {
    if (!did) return null;
    const r = await call<DemandeRv>("demande_review", { did });
    setD((old) => (old && old.demande.id !== r.demande.id ? old : r));
    if (fieldsFor.current !== did) {
      fieldsFor.current = did;
      setDemandeur(r.demande.demandeur || "");
      setMotif(r.demande.motif || "");
      setRemisA(r.demande.demandeur || "");
    }
    return r;
  }, [did]);

  useEffect(() => {
    setD(null); setSel(null); setDets({}); setBord(null); fieldsFor.current = null; inflight.current.clear();
    if (did) void load().catch((e) => toast(errMsg(e), { tone: "red" }));
  }, [did]); // eslint-disable-line react-hooks/exhaustive-deps

  const after = async () => { await load(); changed.current?.(); };

  const pieces: Piece[] = useMemo(() => !d ? [] : [{ key: "b", kind: "bordereau" as const, n: 0 },
    ...d.items.map((it, i) => ({ key: `r${it.item.releve_id}`, kind: "releve" as const, n: i + 1, it }))], [d]);
  const releves = pieces.filter((p) => p.kind === "releve");
  const controles = useMemo(() => new Set(d?.review.controles || []), [d]);
  // première pièce affichée : le premier relevé pas encore contrôlé à l'écran
  useEffect(() => {
    if (!d || (sel && pieces.some((p) => p.key === sel))) return;
    const first = releves.find((p) => !controles.has(p.it!.item.releve_id)) || releves[0] || pieces[0];
    setSel(first ? first.key : null);
  }, [d]); // eslint-disable-line react-hooks/exhaustive-deps
  const cur = pieces.find((p) => p.key === sel) || null;
  const st = d?.demande.statut || "EN COURS";
  const editable = !!d && st !== "REMISE" && st !== "ANNULÉE" && !busy;

  // ---------------------------------------------------------------- aperçus (source avant préparation)
  const upd = (rid: number, v: Det) => setDets((x) => ({ ...x, [rid]: v }));
  const ensure = useCallback(async (rid: number) => {
    const ex = detsRef.current[rid];
    if (inflight.current.has(rid) || (ex && ((ex.detail && !ex.detail.needs_render) || ex.render === "échec"))) return;
    inflight.current.add(rid);
    try {
      const det = await call<ReleveDetail>("releve", { releve_id: rid, consult: true });   // CONSULTATION au journal
      upd(rid, { detail: det, render: det.needs_render ? "en cours" : "" });
      if (det.needs_render) {
        try {
          const d2 = await call<ReleveDetail>("preview", { releve_id: rid });
          if (d2.preview_pdf) forget(d2.preview_pdf);
          upd(rid, { detail: d2, render: "" });
        } catch (e) { upd(rid, { detail: det, render: "échec", err: errMsg(e) }); }
      }
    } catch (e) { upd(rid, { render: "échec", err: errMsg(e) }); } finally { inflight.current.delete(rid); }
  }, []);
  useEffect(() => {
    if (!cur?.it || !cur.it.releve) return;
    const rid = cur.it.item.releve_id;
    const t = window.setTimeout(() => void ensure(rid), 90);       // ↓ maintenu : on ne charge que là où l'on s'arrête
    return () => window.clearTimeout(t);
  }, [cur?.key, ensure]);
  const itemsSig = d ? d.items.map((it) => `${it.item.releve_id}:${it.prepared ? 1 : 0}`).join(",") : "";
  useEffect(() => {                                                 // vues Dossier et Planche : toutes les pièces, l'une après l'autre
    if (view === "releve" || !d) return;
    let alive = true;
    void (async () => { for (const it of d.items) { if (!alive) return; if (!it.prepared && it.releve) await ensure(it.item.releve_id); } })();
    return () => { alive = false; };
  }, [view, itemsSig]); // eslint-disable-line react-hooks/exhaustive-deps
  // bordereau provisoire (avant préparation) : refait par le moteur seulement si la demande a changé
  const bordSig = d ? `${d.demande.id}|${d.demande.maj_le}|${d.items.map((it) => it.item.releve_id).join(",")}` : "";
  const needBord = !!d && bordereau && !d.review.bordereau && (sel === "b" || view !== "releve");
  useEffect(() => {
    if (!needBord || !d || bord?.sig === bordSig) return;
    let alive = true;
    call<{ path: string }>("demande_bordereau_preview", { did: d.demande.id })
      .then((r) => alive && setBord({ sig: bordSig, path: r.path }))
      .catch((e) => alive && setBord({ sig: bordSig, path: null, err: errMsg(e) }));
    return () => { alive = false; };
  }, [needBord, bordSig]); // eslint-disable-line react-hooks/exhaustive-deps

  const pieceLabel = (p: Piece) => p.kind === "bordereau" ? "Bordereau" : p.it!.releve ? `${p.n} · ${periodLabel(p.it!.releve)} · ${p.it!.releve.annee_academique || "?"}` : `${p.n} · relevé retiré de l'index`;
  const pieceDoc = useCallback((p: Piece): PdfDoc => {
    if (p.kind === "bordereau") {
      if (d?.review.bordereau) return { key: p.key, file: d.review.bordereau, first: 1, last: 99, version: d.demande.maj_le, caption: "Bordereau de remise" };
      const ok = bord && bord.sig === bordSig;
      return { key: p.key, file: ok ? bord!.path : null, first: 1, last: 99, caption: "Bordereau de remise (provisoire : numéros attribués à la préparation)",
        message: ok && bord!.err ? `Bordereau provisoire impossible : ${bord!.err}` : "Bordereau provisoire en préparation…" };
    }
    const it = p.it!;
    const cap = pieceLabel(p);
    if (it.prepared && it.item.fichier) return { key: p.key, file: it.item.fichier, first: 1, last: it.releve?.nb_pages || 99, version: it.item.reference || "", caption: `${cap} · fichier préparé · réf. ${it.item.reference}` };
    if (!it.releve) return { key: p.key, file: null, first: 1, last: 1, caption: cap, message: "Ce relevé n'existe plus dans l'index (réindexation ?) : retirez-le de la demande." };
    const det = dets[it.item.releve_id];
    const det2 = det?.detail && det.detail.releve.id === it.item.releve_id ? det.detail : null;
    const msg = det?.render === "échec" ? `Aperçu impossible : ${det.err}` : !det2 ? "Chargement de l'aperçu…" : det2.needs_render ? "Préparation de l'aperçu Word…" : null;
    if (msg || !det2?.preview_pdf) return { key: p.key, file: null, first: 1, last: 1, caption: cap, message: msg || "Aperçu indisponible." };
    return { key: p.key, file: det2.preview_pdf, first: det2.releve.numero_page, last: det2.releve.page_fin, stamp: !!d?.demande.duplicata, caption: cap };
  }, [d, dets, bord, bordSig]); // eslint-disable-line react-hooks/exhaustive-deps
  const dossier = pieces.filter((p) => p.kind === "releve" || bordereau);

  // ---------------------------------------------------------------- progression par pièce
  const seenFiles = useRef<string[]>([]);
  useEffect(() => {
    const m = opn.progress?.phase === "conversion" ? /Conversion \d+\/\d+ : (.+?)…?$/.exec(opn.progress.message) : null;
    if (m && !seenFiles.current.includes(m[1])) seenFiles.current.push(m[1]);
  }, [opn.progress]);
  const pieceState = (p: Piece): { text: string; tone: Tone | "muted"; title?: string; spin?: boolean } => {
    if (p.kind === "bordereau") {
      if (busy) return { text: "refait à la fin", tone: "muted" };
      if (st === "IMPRIMÉE" || st === "REMISE") return { text: bordereau ? "imprimé" : "non imprimé", tone: bordereau ? "green" : "muted" };
      return { text: d?.review.bordereau ? "prêt" : "en tête", tone: "muted" };
    }
    const it = p.it!;
    if (busy) {
      const pr = opn.progress;
      if (!pr) return { text: "en attente", tone: "muted" };
      if (pr.phase !== "conversion") return { text: "contrôle…", tone: "blue", spin: true };
      const name = base(it.releve?.chemin), last = seenFiles.current[seenFiles.current.length - 1];
      if (name && name === last) return { text: "conversion…", tone: "blue", spin: true };
      if (name && seenFiles.current.includes(name)) return { text: "converti", tone: "green" };
      return { text: "en attente", tone: "muted" };
    }
    const etat = it.item.etat || "";
    if (etat.startsWith("BLOQU")) return { text: "BLOQUÉ", tone: "red", title: etat };
    if (it.prepared) return { text: `PRÊT · ${shortRef(it.item.reference)}`, tone: "green", title: `Fichier préparé, réf. ${it.item.reference}` };
    if (!it.releve) return { text: "retiré de l'index", tone: "red" };
    return { text: etat === "PRÊT" ? "à refaire" : "à préparer", tone: etat === "PRÊT" ? "amber" : "muted",
      title: etat === "PRÊT" ? "La demande a changé depuis la préparation : le dossier sera refait" : undefined };
  };

  // ---------------------------------------------------------------- actions
  const saveFields = async () => {
    if (!d || !did) return;
    if (demandeur !== (d.demande.demandeur || "") || motif !== (d.demande.motif || "")) {
      await call("demande_update", { did, fields: { demandeur, motif } });
      await after();
    }
  };

  const setDuplicata = async (v: boolean) => {
    if (!d || !did) return;
    if (st === "PRÊTE" || st === "IMPRIMÉE") {
      const i = await ask({
        title: v ? "Apposer le timbre Duplicata" : "Retirer le timbre Duplicata",
        text: `Le dossier ${st === "PRÊTE" ? "préparé" : "déjà imprimé"} ${v ? "ne porte pas" : "porte"} le timbre « Duplicata conforme ».\n`
          + `${v ? "L'apposer" : "Le retirer"} oblige à refaire le dossier : la demande repassera « EN COURS » (à préparer).`,
        choices: [v ? "Apposer et refaire le dossier" : "Retirer et refaire le dossier"],
        details: "Les relevés seront à nouveau contrôlés et numérotés à la prochaine préparation (Imprimer tout la refait d'elle-même).",
      });
      if (i !== 0) return;
    }
    await call("demande_update", { did, fields: { duplicata: v ? 1 : 0 } });
    await after();
    if (st === "PRÊTE" || st === "IMPRIMÉE") toast(`Timbre Duplicata ${v ? "apposé" : "retiré"} : dossier à refaire, la demande est « EN COURS ».`, { tone: "amber" });
  };

  const prepare = async () => {
    if (!did || busy) return;
    await saveFields();
    setBusy("prepare");
    seenFiles.current = [];
    const op = opn.start("demande");
    toast("Préparation du dossier… (contrôle de chaque relevé avant copie)", { tone: "blue", ms: 8000, sound: false, key: "op" });
    try {
      const r = await call<DemandeRv & { prets: unknown[]; bloques: { periode: string; motif: string }[]; pages: number; dossier: string }>("demande_prepare", { did, op });
      toast(`Dossier prêt : ${plural(r.prets.length, "relevé")}, ${r.pages} page(s)` + (r.bloques.length ? ` — ${r.bloques.length} écarté(s) : ${r.bloques.map((b) => `${b.periode} (${b.motif})`).join(" ; ")}` : ""),
        { tone: r.bloques.length ? "amber" : "green", ms: 15000, key: "op", action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.dossier) } });
    } catch (e) {
      toast(isCancelled(e) ? errMsg(e) : `Préparation impossible : ${errMsg(e)}`, { tone: isCancelled(e) ? "amber" : "red", key: "op" });
    } finally { setBusy(""); opn.finish(); await after().catch(() => undefined); }
  };

  const printAll = async () => {
    if (!d || !did || busy || !editable || !d.items.length) return;
    await saveFields();
    const unseen = releves.filter((p) => p.it!.releve && !controles.has(p.it!.item.releve_id) && !(p.it!.item.etat || "").startsWith("BLOQU"));
    if (unseen.length) {
      const i = await ask({
        title: "Relevés non contrôlés à l'écran",
        text: `${plural(unseen.length, "relevé n'a", "relevés n'ont")} pas été ${unseen.length > 1 ? "contrôlés" : "contrôlé"} à l'écran : ${unseen.map((p) => periodLabel(p.it!.releve!)).join(", ")}.`,
        choices: ["Imprimer quand même", "Voir le premier"],
        details: "Imprimer sans les avoir regardés est possible : votre confirmation est inscrite au journal (contrôle visuel omis).",
      });
      if (i === 1) { setView("releve"); setSel(unseen[0].key); return; }
      if (i !== 0) return;
      await call("demande_control_skipped", { did, releve_ids: unseen.map((p) => p.it!.item.releve_id) }).catch(() => undefined);
    }
    setBusy("print");
    seenFiles.current = [];
    const op = opn.start("demande");
    toast("Préparation et envoi à l'imprimante…", { tone: "blue", ms: 8000, sound: false, key: "op" });
    try {
      await sx.printDemande({ did, with_bordereau: bordereau, op });
      play("impression");
      toast(`Envoyé à l'imprimante${bordereau ? " (avec bordereau)" : ""}. Remettez les documents puis « Remettre ».`, { sound: false, key: "op" });
    } catch (e) {
      toast(isCancelled(e) ? errMsg(e) : `Impression impossible : ${errMsg(e)}`, { tone: isCancelled(e) ? "amber" : "red", key: "op" });
    } finally { setBusy(""); opn.finish(); await after().catch(() => undefined); }
  };

  const done = async () => {
    if (!d || !did || busy || !(st === "PRÊTE" || st === "IMPRIMÉE")) return;
    let who: string | null = st === "IMPRIMÉE" ? remisA.trim() : null;
    if (!who) who = await prompt("Remise", "Remis à (nom de la personne) :", remisA || demandeur);
    if (who === null) return;
    await call("demande_mark", { did, statut: "REMISE", detail: `remis à ${who.trim() || "?"}` });
    await after();
    toast("Demande remise et close.");
  };

  const cancel = async () => {
    if (!d || !did) return;
    const i = await ask({ title: "Annuler la demande", text: `Annuler la demande ${d.demande.numero} (${d.demande.nom || "—"}, ${plural(d.items.length, "relevé")}) ?`,
      choices: ["Annuler la demande"], danger: true, details: "Elle restera visible dans « Toutes », marquée annulée." });
    if (i !== 0) return;
    await call("demande_mark", { did, statut: "ANNULÉE" });
    await after();
  };

  const addCursus = async () => {
    if (!did) return;
    try {
      const r = await call<{ added_count: number }>("demande_add_cursus", { did });
      await after();
      toast(`${plural(r.added_count, "relevé ajouté", "relevés ajoutés")} (cursus : dernier relevé validé de chaque semestre).`, { tone: r.added_count ? "green" : "blue" });
    } catch (e) { toast(errMsg(e), { tone: "amber" }); }
  };

  const reorder = async (ids: number[]) => {
    if (!did || !d) return;
    const wasReady = st === "PRÊTE";
    await call("demande_reorder", { did, releve_ids: ids });
    await after();
    if (wasReady) toast("Ordre changé : le dossier sera refait à l'impression.", { tone: "amber", ms: 5000 });
  };
  const ids = () => (d ? d.items.map((it) => it.item.releve_id) : []);
  const move = async (key: string, step: number) => {
    const list = ids(), i = list.findIndex((x) => `r${x}` === key), j = i + step;
    if (!editable || i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    await reorder(list);
  };
  const moveTo = async (from: string, to: string) => {
    const list = ids(), i = list.findIndex((x) => `r${x}` === from), j = list.findIndex((x) => `r${x}` === to);
    if (!editable || i < 0 || j < 0 || i === j) return;
    const [x] = list.splice(i, 1);
    list.splice(j, 0, x);
    await reorder(list);
  };
  const remove = async (key: string) => {
    const p = pieces.find((x) => x.key === key);
    if (!did || !editable || !p?.it) return;
    const i = await ask({ title: "Retirer de la demande", text: `Retirer ${p.it.releve ? `${periodLabel(p.it.releve)} · ${p.it.releve.annee_academique || "?"}` : "ce relevé"} de la demande ${d?.demande.numero} ?`,
      choices: ["Retirer"], danger: true, details: st === "PRÊTE" || st === "IMPRIMÉE" ? "Le dossier sera à refaire (statut EN COURS)." : undefined });
    if (i !== 0) return;
    const k = pieces.findIndex((x) => x.key === key);
    await call("demande_remove", { did, releve_id: p.it.item.releve_id });
    setSel(pieces[k + 1]?.key || pieces[k - 1]?.key || null);
    await after();
  };

  const toggleCheck = async (key: string, value?: boolean) => {
    const p = pieces.find((x) => x.key === key);
    if (!did || !p?.it || !p.it.releve) return;
    const rid = p.it.item.releve_id;
    const v = value ?? !controles.has(rid);
    if (v === controles.has(rid)) return;
    const r = await call<DemandeRv>("demande_control", { did, releve_id: rid, checked: v });
    setD(r);
  };
  const go = (step: number) => {
    const i = pieces.findIndex((p) => p.key === sel);
    const j = Math.max(0, Math.min(pieces.length - 1, i + step));
    if (pieces[j]) setSel(pieces[j].key);
  };
  /** Espace : contrôlé + suivant. */
  const checkNext = async () => {
    if (cur?.kind === "releve") await toggleCheck(cur.key, true);
    go(1);
  };
  const cycleView = () => setView((v) => (v === "releve" ? "dossier" : v === "dossier" ? "planche" : "releve"));

  return {
    did, d, load, pieces, releves, cur, sel, setSel, view, setView, cycleView, bordereau, setBordereau, busy, opn, editable, st,
    controles, pieceDoc, pieceLabel, pieceState, dossier, dets, demandeur, setDemandeur, motif, setMotif, remisA, setRemisA,
    saveFields, setDuplicata, prepare, printAll, done, cancel, addCursus, move, moveTo, remove, toggleCheck, checkNext, go,
  };
}
export type DemandeReviewState = ReturnType<typeof useDemandeReview>;

/** Saisie en cours (champ texte) : les touches lui reviennent. Une case à cocher n'est pas une saisie. */
export const typing = (e: KeyboardEvent) => {
  const t = e.target as HTMLInputElement | null;
  if (!t) return false;
  if (t.tagName === "INPUT") return !/^(checkbox|radio|button|submit|range|color)$/.test(t.type);
  return /^(TEXTAREA|SELECT)$/.test(t.tagName);
};

// ------------------------------------------------------------------ liste des pièces (colonne de gauche)
function PieceRow({ rv, p }: { rv: DemandeReviewState; p: Piece }) {
  const on = rv.sel === p.key;
  const s = rv.pieceState(p);
  const [over, setOver] = useState(false);
  if (p.kind === "bordereau") return (
    <div className={`ditem bord${on ? " sel" : ""}${rv.bordereau ? "" : " off"}`} onClick={() => rv.setSel(p.key)} role="option" aria-selected={on} data-testid="piece" data-key="b">
      <span className="dh"><Icon name="doc" size={14} /></span>
      <span className="dn">0</span>
      <div className="dm">
        <div className="d1"><b>Bordereau de remise</b></div>
        <div className="d2">feuille de route · à faire signer</div>
      </div>
      <span className={`dst ${s.tone === "muted" ? "muted" : `c-${s.tone}`}`}>{s.text}</span>
      <span onClick={(e) => e.stopPropagation()}><Switch blue checked={rv.bordereau} onChange={rv.setBordereau} label="" title="Imprimer le bordereau en tête du dossier" /></span>
    </div>
  );
  const it = p.it!, r = it.releve;
  const checked = rv.controles.has(it.item.releve_id);
  const ratt = (r?.session || "").toLowerCase().startsWith("rattrap");
  return (
    <div className={`ditem${on ? " sel" : ""}${over ? " over" : ""}`} onClick={() => rv.setSel(p.key)} role="option" aria-selected={on} data-testid="piece" data-key={p.key}
      draggable={rv.editable} onDragStart={(e) => { e.dataTransfer.setData("text/x-piece", p.key); e.dataTransfer.effectAllowed = "move"; }}
      onDragOver={(e) => { if (rv.editable && e.dataTransfer.types.includes("text/x-piece")) { e.preventDefault(); setOver(true); } }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); const from = e.dataTransfer.getData("text/x-piece"); if (from) void rv.moveTo(from, p.key); }}>
      <span className="dh" title={rv.editable ? "Glisser pour changer l'ordre (ou Alt+↑ ↓)" : undefined}>{rv.editable && <RIcon name="grip" size={14} />}</span>
      <span className="dn">{p.n}</span>
      <div className="dm">
        <div className="d1"><b className="nowrap">{r ? periodLabel(r) : "Relevé retiré de l'index"}</b>{ratt && <Badge text="RATT." tone="amber" />}</div>
        <div className="d2">{r ? <>{r.annee_academique || "?"} · {plural(r.nb_pages, "page")}<span className="opt-wide"> · source {r.nb_pages > 1 ? `p. ${r.numero_page}–${r.page_fin}` : `p. ${r.numero_page}`}</span></> : "à retirer de la demande"}</div>
      </div>
      <div className="dr">
        {r && <Badge text={r.statut || "—"} tone={statutTone(r.statut)} dot />}
        <span className={`dst ${s.tone === "muted" ? "muted" : `c-${s.tone}`}`} title={s.title}>{s.spin && <span className="spinner" />}{s.text}</span>
      </div>
      <button className={`dchk${checked ? " on" : ""}`} disabled={!r} onClick={(e) => { e.stopPropagation(); void rv.toggleCheck(p.key); }}
        aria-pressed={checked} aria-label={checked ? "Contrôlé à l'écran (cliquer pour retirer)" : "Marquer contrôlé à l'écran"}
        title={checked ? "Contrôlé à l'écran (inscrit au journal)" : "Marquer « contrôlé à l'écran » (Espace : contrôlé + suivant)"} data-testid="piece-check">
        {checked && <Icon name="check" size={13} stroke={2.6} />}
      </button>
      {rv.editable && <button className="drm" onClick={(e) => { e.stopPropagation(); void rv.remove(p.key); }} aria-label="Retirer de la demande" title="Retirer de la demande (Suppr)"><Icon name="trash" size={13} /></button>}
    </div>
  );
}

export function CheckProgress({ rv }: { rv: DemandeReviewState }) {
  const n = rv.releves.filter((p) => p.it!.releve).length;
  const k = rv.releves.filter((p) => rv.controles.has(p.it!.item.releve_id)).length;
  return (
    <div className="rv-checks" data-testid="checks" title="Relevés regardés et cochés « contrôlé à l'écran » (inscrit au journal)">
      <span className="small muted nowrap">Contrôlés à l'écran</span>
      <div className="bar"><div style={{ width: `${n ? (100 * k) / n : 0}%` }} /></div>
      <b className="small nowrap" data-testid="checks-count">{k} / {n}</b>
    </div>
  );
}

function PieceList({ rv }: { rv: DemandeReviewState }) {
  const d = rv.d!;
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { (box.current?.querySelector(".ditem.sel") as HTMLElement | null)?.scrollIntoView({ block: "nearest" }); }, [rv.sel]);
  const pages = d.review.pages;
  return (
    <>
      <div className="rv-fields">
        <label className="lbl">Demandeur<input className="field" value={rv.demandeur} disabled={!rv.editable} onChange={(e) => rv.setDemandeur(e.target.value)} onBlur={() => void rv.saveFields()} data-testid="demandeur" /></label>
        <label className="lbl">Motif
          <input className="field" list="motifs" value={rv.motif} disabled={!rv.editable} onChange={(e) => rv.setMotif(e.target.value)} onBlur={() => void rv.saveFields()} />
          <datalist id="motifs">{MOTIFS.map((m) => <option key={m} value={m} />)}</datalist>
        </label>
      </div>
      <div className="row rv-head">
        <div className="grow">
          <div className="eyebrow">Dossier · ordre d'impression</div>
          <div className="h3">{plural(d.items.length, "relevé")} · {pages} page{pages > 1 ? "s" : ""}{rv.bordereau ? " + bordereau" : ""}</div>
        </div>
        <button className="btn ghost sm" disabled={!rv.editable || !d.demande.matricule} onClick={() => void rv.addCursus()} data-testid="demande-cursus"
          title={d.demande.matricule ? "Ajouter tout le cursus validé : le dernier relevé validé de chaque semestre de l'étudiant" : "Ajoutez d'abord un relevé de l'étudiant"}><Icon name="layers" size={14} />Cursus</button>
      </div>
      {rv.busy ? <OpProgress title={rv.busy === "print" ? "Préparation et impression" : "Préparation"} progress={rv.opn.progress} cancelling={rv.opn.cancelling}
        onCancel={() => void rv.opn.cancel()} testid="demande-progress" /> : <CheckProgress rv={rv} />}
      <div className="rv-items" ref={box} role="listbox" aria-label="Pièces du dossier, dans l'ordre d'impression" data-testid="pieces">
        {rv.pieces.map((p) => <PieceRow key={p.key} rv={rv} p={p} />)}
        {!d.items.length && <div className="muted small" style={{ padding: 14, textAlign: "center" }}>Aucun relevé : au guichet, sélectionnez un relevé puis Ctrl+D.</div>}
      </div>
      <div className="muted tiny gx-hints"><span className="key">↑</span><span className="key">↓</span> pièce · <span className="key">Espace</span> contrôlé + suivant · <span className="key">Alt+↑↓</span> déplacer · <span className="key">Suppr</span> retirer · <span className="key">V</span> vue · <span className="key">Entrée</span> plein écran</div>
    </>
  );
}

// ------------------------------------------------------------------ bandeau de la pièce affichée
export function PieceStrip({ rv, floating = false }: { rv: DemandeReviewState; floating?: boolean }) {
  const d = rv.d!, p = rv.cur;
  const total = rv.releves.length;
  if (rv.view === "planche") return (
    <div className={`card strip${floating ? " float" : ""}`} data-testid="piece-strip">
      <Badge text="PLANCHE" tone="blue" big />
      <span className="nm">Tout le dossier d'un coup d'œil</span>
      <span className="facts">{rv.dossier.length} pièce{rv.dossier.length > 1 ? "s" : ""}<i>·</i>ordre d'impression<i>·</i>glisser une vignette pour changer l'ordre</span>
    </div>
  );
  if (rv.view === "dossier") {
    const st = d.demande.statut;
    return (
      <div className={`card strip${floating ? " float" : ""}`} data-testid="piece-strip">
        <Badge text={st} tone={STATUT_TONE[st] || "gray"} big dot />
        <span className="nm">{st === "IMPRIMÉE" || st === "REMISE" ? "Dossier imprimé" : "Dossier"} · {d.review.pages + (rv.bordereau ? 1 : 0)} pages</span>
        <span className="facts">{rv.bordereau ? "bordereau + " : ""}{plural(d.items.length, "relevé")}{d.review.a_imprimer && <><i>·</i>{base(d.review.a_imprimer)}</>}</span>
        <span className="grow" />
        {d.demande.dossier && <button className="btn link tiny nowrap" onClick={() => void sx.reveal(d.review.a_imprimer || d.demande.dossier!)}>Ouvrir le dossier</button>}
      </div>
    );
  }
  if (!p) return <div className="card strip" data-testid="piece-strip"><span className="nm muted">Aucune pièce</span></div>;
  if (p.kind === "bordereau") return (
    <div className={`card strip${floating ? " float" : ""}`} data-testid="piece-strip">
      <Badge text={d.review.bordereau ? "PRÊT" : "PROVISOIRE"} tone={d.review.bordereau ? "green" : "violet"} big dot />
      <span className="nm">Bordereau de remise</span>
      <span className="facts">feuille de route de l'agent<i>·</i>{rv.bordereau ? "imprimé en tête du dossier" : "ne sera pas imprimé"}{!d.review.bordereau && <><i>·</i>numéros de délivrance attribués à la préparation</>}</span>
      <span className="grow" />
      {d.review.bordereau && <button className="btn link tiny nowrap" onClick={() => void sx.reveal(d.review.bordereau!)}>Fichier préparé</button>}
    </div>
  );
  const it = p.it!, r = it.releve;
  const det = rv.dets[it.item.releve_id]?.detail;
  const alerts = det?.alerts || [];
  return (
    <div className={`card strip${floating ? " float" : ""}`} data-testid="piece-strip">
      {it.prepared ? <Badge text="PRÊT" tone="green" big dot /> : <Badge text={r?.statut || "—"} tone={statutTone(r?.statut)} big dot />}
      <span className="nm nowrap">Relevé {p.n}/{total}{r ? ` · ${periodLabel(r)}` : ""}</span>
      {r && (it.prepared
        ? <span className="facts">{r.annee_academique}<i>·</i>fichier préparé<i>·</i><span className="mono">réf. {it.item.reference}</span><span className="opt-mid"><i>·</i>pied de page de délivrance{d.demande.duplicata ? " et timbre Duplicata" : ""}</span></span>
        : <span className="facts">{r.annee_academique || "?"}<i>·</i>{r.niveau || "?"}{r.credits != null && <span className="opt-wide"><i>·</i>{creditsLabel(r)}</span>}{r.moyenne && <span className="opt-mid"><i>·</i>moy. {r.moyenne}</span>}<i>·</i>{plural(r.nb_pages, "page")} (source {r.nb_pages > 1 ? `p. ${r.numero_page}–${r.page_fin}` : `p. ${r.numero_page}`})</span>)}
      {(it.item.etat || "").startsWith("BLOQU") && !it.prepared && <Badge text={it.item.etat!.slice(0, 80)} tone="red" title={it.item.etat || ""} />}
      <span className="grow" />
      {!floating && alerts.length > 0 && (
        <span className="chips">{alerts.filter((a) => a.tone !== "green").slice(0, 2).map((a, i) => (
          <span key={i} className={`achip t-${a.tone}`} title={a.text}><Icon name={a.tone === "blue" ? "info" : "alert"} size={13} /><span className="ellipsis">{a.text.split(/(?<=[^.]{12,})(?:\. | : | — )/)[0].slice(0, 70)}</span></span>
        ))}</span>
      )}
      {!floating && it.prepared && <button className="btn link tiny nowrap" onClick={() => void sx.reveal(it.item.fichier!)} title={it.item.fichier || ""}>Fichier préparé</button>}
      {!floating && r && <button className="btn link tiny nowrap opt-mid" onClick={() => void sx.reveal(r.chemin)} title={`Fichier original (lecture seule) : ${r.chemin}`}>Fichier original</button>}
    </div>
  );
}

// ------------------------------------------------------------------ scène : relevé, dossier ou planche
function useWidth(ref: React.RefObject<HTMLElement | null>) {
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

function Planche({ rv, size }: { rv: DemandeReviewState; size: "S" | "M" | "L" }) {
  const box = useRef<HTMLDivElement>(null);
  const w = useWidth(box);
  const docs = rv.dossier.map((p) => ({ p, doc: rv.pieceDoc(p) }));
  const bdoc = docs.find((x) => x.p.kind === "bordereau")?.doc;
  const bpages = usePageCount(bdoc?.file, bdoc?.version) || 1;
  const cols = { S: 7, M: 5, L: 3 }[size];
  const width = Math.max(90, Math.floor((w - 28 - (cols - 1) * 14) / cols) - 12);
  const items: ThumbItem[] = [];
  for (const { p, doc } of docs) {
    const first = doc.file ? doc.first : 1;
    const last = p.kind === "bordereau" ? bpages : doc.file ? doc.last : 1;
    const n = Math.max(1, last - first + 1);
    const checked = p.it && rv.controles.has(p.it.item.releve_id);
    for (let k = 0; k < n; k++) {
      items.push({
        key: `${p.key}:${first + k}`, group: p.key, file: doc.file, page: first + k, version: doc.version, draggable: p.kind === "releve" && rv.editable,
        label: <>{p.n} · {p.kind === "bordereau" ? "Bordereau" : p.it!.releve ? periodLabel(p.it!.releve) : "?"}{n > 1 ? ` · p. ${k + 1}/${n}` : ""}</>,
        sub: p.kind === "bordereau" ? (rv.d!.review.bordereau ? "prêt" : "provisoire") : <>{p.it!.releve?.annee_academique || ""}{" "}
          <span className={checked ? "c-green" : "muted"}>{checked ? "✓ contrôlé" : "à contrôler"}</span></>,
        title: doc.message || undefined,
      });
    }
  }
  return (
    <div className="planche" ref={box} data-testid="planche">
      <Thumbs items={items} width={width} selected={rv.sel} label="Planche du dossier" testid="planche-thumbs"
        onPick={(it) => it.group && rv.setSel(it.group)} onOpen={(it) => { if (it.group) { rv.setSel(it.group); rv.setView("releve"); } }}
        onMove={(from, to) => void rv.moveTo(from, to)} />
    </div>
  );
}

export function DemandeStage({ rv, focus, setFocus, dock, focusBar, pdf, pv, setPv, setMm, mmHost, caption }: {
  rv: DemandeReviewState; focus: boolean; setFocus: (f: boolean) => void; dock: ReactNode; focusBar: ReactNode;
  pdf: React.RefObject<PdfViewHandle | null>; pv: PdfViewState; setPv: (s: PdfViewState) => void; setMm: (el: HTMLDivElement | null) => void;
  mmHost: HTMLDivElement | null; caption?: ReactNode;
}) {
  const [size, setSize] = useState<"S" | "M" | "L">("M");
  const p = rv.cur;
  const single = p ? rv.pieceDoc(p) : null;
  const docs = rv.view === "dossier" ? rv.dossier.map(rv.pieceDoc) : null;
  return (
    <div className={`gx-stage${focus ? " focus" : ""}`} tabIndex={-1} data-testid="stage" aria-label="Aperçu de la pièce">
      {focus && focusBar}
      {!focus && caption}
      <div className="gx-view">
        {rv.view === "planche" ? <Planche rv={rv} size={size} /> : docs ? (
          <PdfView ref={pdf} file={null} first={1} last={1} docs={docs} selectedDoc={rv.sel || undefined} onPickDoc={(k) => rv.setSel(k)}
            defaultFit="auto" toolbar={false} minimapHost={null} onState={setPv} onDoubleClick={() => setFocus(!focus)} />
        ) : (
          <PdfView ref={pdf} file={single?.file ?? null} first={single?.first || 1} last={single?.last || 1} version={`${p?.key}-${single?.version || ""}`}
            message={single && !single.file ? single.message : null} stamp={!!single?.stamp}
            defaultFit="auto" toolbar={false} minimapHost={mmHost} onState={setPv} onDoubleClick={() => setFocus(!focus)} />
        )}
      </div>
      {!focus && <aside className="dock" data-testid="dock" aria-label="Actions et affichage">
        {dock}
        {rv.view === "planche" ? (
          <div className="grp">
            <span className="lbl row"><span className="grow">Vue</span><span className="pct">Planche</span></span>
            <div className="fitgrid three" role="radiogroup" aria-label="Vue (V)">
              {VIEWS.map(([id, ic, l, t]) => <button key={id} className={`btn ghost xs${rv.view === id ? " on" : ""}`} role="radio" aria-checked={rv.view === id} aria-label={l} title={`${t} (V)`} onClick={() => rv.setView(id as RvView)} data-testid={`view-${id}`}><RIcon name={ic} size={14} /></button>)}
            </div>
            <span className="lbl">Taille des vignettes</span>
            <div className="fitgrid three">{(["S", "M", "L"] as const).map((s) => <button key={s} className={`btn ghost xs${size === s ? " on" : ""}`} onClick={() => setSize(s)}>{s}</button>)}</div>
          </div>
        ) : (
          <ViewControls pv={pv} pdf={pdf} onFocus={() => setFocus(true)} views={VIEWS} view={rv.view} setView={(v) => rv.setView(v as RvView)} setMm={setMm} />
        )}
      </aside>}
    </div>
  );
}

/** Dock de la revue : l'action principale suit l'étape (Imprimer, puis Remettre). */
export function ReviewDock({ rv, extra }: { rv: DemandeReviewState; extra?: ReactNode }) {
  const d = rv.d!, st = rv.st;
  const pages = d.review.pages + (rv.bordereau ? 1 : 0);
  if (rv.busy) {
    const pr = rv.opn.progress;
    return (
      <div className="grp">
        <button className="btn primary" disabled data-testid="demande-print"><span className="r"><span className="spinner" />{pr && pr.n ? `${pr.phase === "conversion" ? pr.i : pr.n} / ${pr.n}` : "…"}</span>
          <span className="sub">{rv.busy === "print" ? "préparation puis impression" : "préparation : contrôle de chaque relevé"}</span></button>
        <button className="btn ghost sm" disabled={rv.opn.cancelling} onClick={() => void rv.opn.cancel()}><Icon name="x" size={14} />Arrêter<span className="kbd">Échap</span></button>
      </div>
    );
  }
  if (st === "IMPRIMÉE") return (
    <div className="grp">
      <span className="lbl">Remis à</span>
      <input className="field sm" value={rv.remisA} onChange={(e) => rv.setRemisA(e.target.value)} aria-label="Remis à" data-testid="remis-a"
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void rv.done(); } }} />
      <button className="btn primary" onClick={() => void rv.done()} data-testid="demande-done" title="Remettre les documents et clore la demande (Ctrl+Entrée)">
        <span className="r"><Icon name="check" size={18} />Remettre</span><span className="sub">clôt la demande</span><span className="kbd">Ctrl+Entrée</span></button>
      <button className="btn ghost sm" onClick={() => void rv.printAll()} data-testid="demande-print" title="Réimprimer le dossier (Ctrl+P)"><Icon name="printer" size={15} />Réimprimer</button>
      <button className="btn ghost sm" disabled={!d.demande.dossier} onClick={() => d.demande.dossier && void sx.reveal(d.review.a_imprimer || d.demande.dossier)}><Icon name="folder" size={15} />Dossier</button>
      {extra}
      <Switch checked={!!d.demande.duplicata} onChange={(v) => void rv.setDuplicata(v)} label="Duplicata" title="Timbre « Duplicata conforme » sur chaque relevé" />
    </div>
  );
  if (st === "REMISE" || st === "ANNULÉE") return (
    <div className="grp">
      <Badge text={st === "REMISE" ? "REMISE" : "ANNULÉE"} tone={STATUT_TONE[st]} big dot />
      <span className="small muted">{st === "REMISE" ? `Remise ${hourLabel(d.review.etapes.remettre)} : demande close.` : "Demande annulée."}</span>
      <button className="btn ghost sm" disabled={!d.demande.dossier} onClick={() => d.demande.dossier && void sx.reveal(d.demande.dossier)}><Icon name="folder" size={15} />Dossier</button>
      {extra}
    </div>
  );
  return (
    <div className="grp">
      <button className="btn primary" disabled={!rv.editable || !d.items.length} onClick={() => void rv.printAll()} data-testid="demande-print"
        title="Imprimer tout le dossier (préparation incluse) — Ctrl+P">
        <span className="r"><Icon name="printer" size={18} />Imprimer</span>
        <span className="sub">tout : {plural(d.items.length, "relevé")}{rv.bordereau ? " + bordereau" : ""} ({pages} p.)</span><span className="kbd">Ctrl+P</span></button>
      <button className="btn ghost sm" disabled={!rv.editable || !d.items.length} onClick={() => void rv.prepare()} data-testid="demande-prepare"
        title={st === "PRÊTE" ? "Refaire le dossier (bordereau + relevés numérotés) sans l'envoyer à l'imprimante" : "Préparer le dossier (bordereau + relevés numérotés) sans l'envoyer à l'imprimante"}><Icon name="doc" size={15} />{st === "PRÊTE" ? "Refaire" : "Préparer"}</button>
      <button className="btn secondary sm" disabled={st !== "PRÊTE"} onClick={() => void rv.done()} data-testid="demande-done" title="Remettre les documents (Ctrl+Entrée)"><Icon name="check" size={15} />Remettre</button>
      {extra}
      <Switch checked={!!d.demande.duplicata} onChange={(v) => void rv.setDuplicata(v)} label="Duplicata" title="Timbre « Duplicata conforme » horodaté sur chaque relevé · l'original reste intact" />
    </div>
  );
}

export function DemandeMenu({ rv }: { rv: DemandeReviewState }) {
  const [open, setOpen] = useState(false);
  const d = rv.d!;
  const run = (f: () => void) => () => { setOpen(false); f(); };
  return (
    <div className="anchor">
      <button className="btn ghost sm icon" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-label="Autres actions" data-testid="demande-more"><RIcon name="more" size={16} /></button>
      <Popover open={open} onClose={() => setOpen(false)} width={300} label="Autres actions" testid="demande-more-menu">
        <div className="pop-head"><div className="h3">{d.demande.numero}</div>
          <div className="muted small">Reçue {hourLabel(d.demande.creee_le)}{d.demande.operateur ? ` par ${d.demande.operateur}` : ""} · {d.demande.dossier ? "dossier préparé" : "dossier pas encore préparé"}</div></div>
        <button className="mi" disabled={!d.demande.dossier} onClick={run(() => void sx.reveal(d.review.a_imprimer || d.demande.dossier!))}><Icon name="folder" size={15} />Ouvrir le dossier</button>
        <button className="mi" disabled={!rv.editable || !d.demande.matricule} onClick={run(() => void rv.addCursus())}><Icon name="layers" size={15} />Ajouter tout le cursus validé</button>
        <button className="mi" disabled={!rv.editable || !d.items.length} onClick={run(() => void rv.prepare())}><Icon name="doc" size={15} />{rv.st === "PRÊTE" ? "Refaire le dossier" : "Préparer sans imprimer"}</button>
        <div className="sep" />
        <button className="mi" disabled={!rv.editable} onClick={run(() => void rv.cancel())} data-testid="demande-cancel" style={{ color: rv.editable ? "var(--red)" : undefined }}><Icon name="x" size={15} />Annuler la demande</button>
      </Popover>
    </div>
  );
}

// ------------------------------------------------------------------ revue plein cadre
export function DemandeReview({ did, onClose, backLabel = "Guichet" }: { did: number; onClose: () => void; backLabel?: string }) {
  const rv = useDemandeReview(did);
  const pdf = useRef<PdfViewHandle>(null);
  const [pv, setPv] = useState<PdfViewState>({ fit: "auto", scale: null, overflow: false });
  const [mmHost, setMmHost] = useState<HTMLDivElement | null>(null);
  const [focus, setFocusRaw] = useState(false);
  const setFocus = (f: boolean) => { setFocusRaw(f); void sx.setFullScreen?.(f).catch(() => undefined); };
  const toast = useToast();
  const close = () => {
    if (rv.busy) { toast("Opération en cours : Échap ou « Arrêter » d'abord, puis revenez.", { tone: "amber", ms: 4000 }); return; }
    if (focus) setFocus(false);
    void rv.saveFields().catch(() => undefined);
    onClose();
  };
  const d = rv.d;
  const ok = !!d;
  useReviewNav(() => { if (rv.busy) return false; close(); });
  // Échap, même pendant le chargement : retour (jamais la recherche du guichet derrière la revue)
  useShortcuts({ "escape": () => { if (rv.busy) void rv.opn.cancel(); else if (focus) setFocus(false); else close(); } });
  useShortcuts({
    "arrowdown": (e) => (typing(e) ? false : rv.go(1)), "arrowup": (e) => (typing(e) ? false : rv.go(-1)),
    " ": (e) => (typing(e) || (e.target as HTMLElement)?.tagName === "BUTTON" ? false : void rv.checkNext()),
    "alt+arrowup": (e) => (typing(e) || !rv.cur ? false : void rv.move(rv.cur.key, -1)),
    "alt+arrowdown": (e) => (typing(e) || !rv.cur ? false : void rv.move(rv.cur.key, 1)),
    "delete": (e) => (typing(e) || !rv.cur || rv.cur.kind !== "releve" ? false : void rv.remove(rv.cur.key)),
    "v": (e) => (typing(e) ? false : rv.cycleView()),
    "enter": (e) => (typing(e) || /^(BUTTON|A)$/.test((e.target as HTMLElement)?.tagName || "") ? false : setFocus(!focus)),
    "f11": () => setFocus(!focus),
    "alt+1": () => pdf.current?.goZone("identite"), "alt+2": () => pdf.current?.goZone("notes"), "alt+3": () => pdf.current?.goZone("decision"),
    "ctrl+p": () => void rv.printAll(),
    "ctrl+enter": () => void rv.done(),
  }, ok);
  useEffect(() => () => { void sx.setFullScreen?.(false).catch(() => undefined); }, []);

  if (!d) return (
    <ReviewShell label="Revue de la demande" testid="demande-review" backLabel={backLabel} onBack={onClose} icon="folder" eyebrow="Demande" title="…" side={<div className="empty"><span className="spinner" /></div>}>
      <div className="gx-stage"><div className="empty"><span className="spinner" /></div></div>
    </ReviewShell>
  );
  const st = d.demande.statut;
  return (
    <ReviewShell label={`Revue de la demande ${d.demande.numero}`} testid="demande-review" backLabel={backLabel} onBack={close} icon="folder"
      eyebrow={<>Demande <span className="mono">{d.demande.numero}</span></>} title={d.demande.nom || "—"} mat={d.demande.matricule}
      center={st === "ANNULÉE" ? <Badge text="ANNULÉE" tone="gray" big /> : <Stepper steps={stepsOf(d, rv.busy)} current={STEP_IDX[st] ?? 0} />}
      right={<>
        <span className="muted small nowrap opt-wide">reçue {hourLabel(d.demande.creee_le)}{d.demande.operateur ? ` · ${d.demande.operateur}` : ""}{d.demande.poste ? ` · ${d.demande.poste}` : ""}</span>
        <DemandeMenu rv={rv} />
      </>}
      side={<PieceList rv={rv} />}>
      <PieceStrip rv={rv} />
      <DemandeStage rv={rv} focus={focus} setFocus={setFocus} pdf={pdf} pv={pv} setPv={setPv} setMm={setMmHost} mmHost={mmHost}
        dock={<ReviewDock rv={rv} />}
        focusBar={<ReviewFocusBar onExit={() => setFocus(false)}>
          <Badge text={st} tone={STATUT_TONE[st] || "gray"} dot />
          <b className="nm ellipsis">{d.demande.nom || "—"}</b><span className="mono">{d.demande.matricule}</span>
          <span className="muted small nowrap">{rv.cur ? rv.pieceLabel(rv.cur) : ""}</span>
          {rv.cur?.kind === "releve" && <button className="btn ghost sm" onClick={() => void rv.checkNext()}><Icon name="check" size={14} />Contrôlé, suivant<span className="kbd">Espace</span></button>}
          {(st === "EN COURS" || st === "PRÊTE") && <button className="btn primary sm" disabled={!!rv.busy} onClick={() => void rv.printAll()}><Icon name="printer" size={16} />Imprimer tout<span className="kbd">Ctrl+P</span></button>}
        </ReviewFocusBar>} />
    </ReviewShell>
  );
}
