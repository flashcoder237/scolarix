import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { call, plural, sx, statutTone, type Releve } from "../api";
import { useApp, useShortcuts } from "../state";
import { Badge, Icon, Switch, useToast } from "../components/ui";
import { FIT_LABELS, ZONES, type FitMode, type Zone } from "../components/PdfView";
import { ComparePane, type Box, type DocSrc, type Mark, type PaneHandle, type PaneState } from "../components/ComparePane";

// Vérification d'un relevé soumis (cas S3.1) : les deux documents côte à côte en grand, zoom et défilement liés ;
// le résultat du moteur devient un bandeau de puces (une par champ) qui amène les deux volets sur la ligne ;
// les divergences sont surlignées sur les documents ; rapport et impression « Vérifié » dans le dock.

interface SoumisInfo { nature: string; pages: number; ocr: number | null; positions: boolean }
interface Ident { delivered: Record<string, any> | null; ident: Record<string, any>; matricule: string | null; releves: Releve[]; best: number | null; soumis?: SoumisInfo }
interface Row {
  champ: string; soumis: string; original: string; conforme: boolean;
  cle?: string; code?: string; pos?: { soumis: Box | null; original: Box | null };
}
interface Cmp { original: Releve; rows: Row[]; conformes: number; total: number; soumis?: SoumisInfo }
type View = "cote" | "calque";
type Chip = { kind: "row"; i: number } | { kind: "group"; rows: number[] };

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const baseName = (p: string) => p.split(/[\\/]/).pop() || p;
const isPdf = (p: string | null) => !!p && p.toLowerCase().endsWith(".pdf");
const ZONE_OF: Record<string, Zone> = { matricule: "identite", nom: "identite", annee: "identite", ue: "notes", moyenne: "decision", decision: "decision" };
/** Repli quand une ligne n'est pas située (scan sans OCR) : fractions habituelles d'un relevé FMSP. */
const ZONE_FALLBACK: Record<Zone, [number, number]> = { identite: [0.12, 0.27], notes: [0.27, 0.66], decision: [0.68, 0.84] };
const cleOf = (r: Row) => r.cle || "ue";

function chipLabel(r: Row) {
  const c = cleOf(r);
  if (c === "matricule") return "Matricule";
  if (c === "nom") return "Nom";
  if (c === "annee") return "Année";
  if (c === "moyenne") return "Moyenne";
  if (c === "decision") return "Décision";
  return r.code || (r.champ.length > 22 ? `${r.champ.slice(0, 21)}…` : r.champ);
}
/** « N5 · S9-S10 » : niveau et semestres (colonne Période du guichet). */
function periodShort(r: Releve) {
  return `${r.niveau_num > 0 && r.niveau_num < 100 ? `N${r.niveau_num}` : (r.niveau || "?").replace("Master ", "M")}${r.sem_label ? ` · ${r.sem_label}` : ""}`;
}
function natureLabel(s?: SoumisInfo | null) {
  if (!s) return "";
  const kind = s.nature === "PDF texte" ? "PDF texte" : s.nature === "Image" ? "image" : "scan";
  return [kind, plural(s.pages, "p.", "p."), s.ocr != null ? `OCR ${Math.round(s.ocr)} %` : ""].filter(Boolean).join(" · ");
}
function union(boxes: Box[]): Box | null {
  if (!boxes.length) return null;
  const page = boxes[0].page, same = boxes.filter((b) => b.page === page);
  return { page, x0: Math.min(...same.map((b) => b.x0)), y0: Math.min(...same.map((b) => b.y0)), x1: Math.max(...same.map((b) => b.x1)), y1: Math.max(...same.map((b) => b.y1)) };
}

export function Verification() {
  const app = useApp();
  const toast = useToast();
  const active = app.page === "verification";
  // ---------------------------------------------------------------- document soumis et original
  const [path, setPath] = useState<string | null>(null);
  const [ident, setIdent] = useState<Ident | null>(null);
  const [mat, setMat] = useState("");
  const [matLu, setMatLu] = useState(false);              // matricule lu sur le document (pas retapé)
  const [rels, setRels] = useState<Releve[]>([]);
  const [orig, setOrig] = useState<number | null>(null);
  const [cmp, setCmp] = useState<Cmp | null>(null);
  const [busy, setBusy] = useState("");
  const [demandeur, setDemandeur] = useState("");
  const [reference, setReference] = useState("");
  const [image, setImage] = useState<string | null>(null);
  const [imageErr, setImageErr] = useState("");
  const [printing, setPrinting] = useState(false);
  const demandeurRef = useRef<HTMLInputElement>(null);
  const askedFields = useRef(false);

  // ---------------------------------------------------------------- affichage
  const [view, setView] = useState<View>("cote");
  const [fit, setFitRaw] = useState<FitMode>("auto");
  const [manual, setManual] = useState(1);
  const [linked, setLinked] = useState(true);
  const [focus, setFocus] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [zoneFlash, setZoneFlash] = useState<{ zone: Zone; n: number } | null>(null);
  const [expandUe, setExpandUe] = useState(false);
  const [curtain, setCurtain] = useState(0.5);
  const paneS = useRef<PaneHandle>(null);
  const paneO = useRef<PaneHandle>(null);
  const paneC = useRef<PaneHandle>(null);
  const [stS, setStS] = useState<PaneState | null>(null);
  const [stO, setStO] = useState<PaneState | null>(null);
  const [stC, setStC] = useState<PaneState | null>(null);
  const autoJump = useRef(false);

  const setFit = (m: FitMode) => { setFitRaw(m); if (m !== "libre") { paneS.current?.setFrac(0, 0); paneO.current?.setFrac(0, 0); paneC.current?.setFrac(0, 0); } };
  const onZoom = useCallback((m: number) => { setManual(m); setFitRaw("libre"); }, []);

  const compare = async (p: string, id: number) => {
    setBusy("Comparaison…");
    try {
      const r = await call<Cmp>("verify_compare", { path: p, releve_id: id, demandeur, reference });
      setCmp(r); setSel(null); setExpandUe(false); autoJump.current = true;
    } catch (e) { toast(`Lecture impossible : ${errMsg(e)}`, { tone: "red" }); } finally { setBusy(""); }
  };

  const load = async () => {
    const p = await sx.pickFile({ title: "Document soumis", filters: [{ name: "Documents", extensions: ["pdf", "png", "jpg", "jpeg", "tif", "tiff"] }] });
    if (!p) return;
    setPath(p); setCmp(null); setIdent(null); setImage(null); setImageErr(""); setSel(null); setDrawer(false); askedFields.current = false;
    if (!isPdf(p)) void sx.readImage(p).then(setImage).catch((e) => setImageErr(errMsg(e)));
    setBusy("Lecture du document soumis…");
    try {
      const r = await call<Ident>("verify_identify", { path: p });
      setIdent(r);
      if (r.delivered) toast(`Fichier identique au document délivré le ${String(r.delivered.horodatage).slice(0, 16).replaceAll("-", "/")} sous la référence ${r.delivered.reference} (${String(r.delivered.nature).toLowerCase()}, matricule ${r.delivered.matricule || "?"}, par ${r.delivered.operateur || "?"}).`, { ms: 20000 });
      else if (isPdf(p)) toast("Ce fichier ne correspond à aucun document délivré par le guichet (modifié, réimprimé puis scanné, ou délivré avant Scolarix) : comparaison du contenu.", { tone: "amber", ms: 12000 });
      if (r.matricule) { setMat(r.matricule); setMatLu(true); } else setMatLu(false);
      setRels(r.releves);
      if (r.best) { setOrig(r.best); await compare(p, r.best); }
      else { setOrig(null); toast("Matricule non reconnu sur le document soumis : saisissez le matricule déclaré puis « Entrée » (recherche exacte de l'original).", { tone: "amber", ms: 12000 }); }
    } catch (e) { toast(errMsg(e), { tone: "red" }); } finally { setBusy(""); }
  };

  const findOriginal = async () => {
    if (!mat.trim()) return;
    // matricule exact (tous les relevés de l'étudiant), comme la 1.x : pas de recherche approchée ici
    const list = await call<{ releves: Releve[] }>("cursus_info", { matricule: mat.trim() }).then((r) => r.releves).catch(() => [] as Releve[]);
    setRels(list);
    if (!list.length) { setOrig(null); setCmp(null); toast("Aucun relevé indexé pour ce matricule : document probablement non authentique ou matricule erroné.", { tone: "red" }); return; }
    const id = list[list.length - 1].id;
    setOrig(id);
    if (path) await compare(path, id);
  };

  const report = async () => {
    if (!path || !orig || !cmp) return;
    if (!demandeur.trim() && !reference.trim() && !askedFields.current) {
      // Ctrl+R sans demandeur ni référence : le curseur y va (ils figurent sur le rapport), Ctrl+R de nouveau génère sans
      askedFields.current = true;
      demandeurRef.current?.focus();
      toast("Organisme demandeur et référence figurent sur le rapport : complétez-les puis Ctrl+R (ou Ctrl+R de nouveau pour générer sans).", { tone: "blue", ms: 8000 });
      return;
    }
    try {
      const r = await call<{ path: string; name: string }>("verify_report", { path, releve_id: orig, demandeur, reference });
      toast(`Rapport généré : ${r.name}`, { ms: 12000, action: { label: "Ouvrir le dossier", run: () => void sx.reveal(r.path) } });
    } catch (e) { toast(errMsg(e), { tone: "red" }); }
  };

  const printOriginal = async () => {
    if (!orig) { toast("Aucun original sélectionné.", { tone: "amber" }); return; }
    if (printing) return;
    setPrinting(true);
    try {
      await sx.printVerified({ releve_id: orig });
      toast("Original envoyé à l'imprimante avec le timbre « Vérifié ».");
    } catch (e) { toast(`Impression impossible : ${errMsg(e)}`, { tone: "red" }); } finally { setPrinting(false); }
  };

  // ---------------------------------------------------------------- champs : puces, divergences
  const rows = cmp?.rows || [];
  const o: Releve | null = (cmp && cmp.original.id === orig ? cmp.original : rels.find((r) => r.id === orig)) || null;
  const chips: Chip[] = useMemo(() => {
    const idx = rows.map((_, i) => i);
    const of = (c: string) => idx.filter((i) => cleOf(rows[i]) === c);
    const ues = of("ue");
    const ko = ues.filter((i) => !rows[i].conforme), ok = ues.filter((i) => rows[i].conforme);
    const one = (l: number[]) => l.map((i) => ({ kind: "row", i }) as Chip);
    return [...one(of("matricule")), ...one(of("nom")), ...one(of("annee")), ...one(ko), ...one(of("moyenne")),
      ...(ok.length ? (expandUe ? one(ok) : [{ kind: "group", rows: ok } as Chip]) : []), ...one(of("decision"))];
  }, [rows, expandUe]);
  const order = useMemo(() => {
    const idx = rows.map((_, i) => i), rank = (i: number) => ["matricule", "nom", "annee", "ue", "moyenne", "decision"].indexOf(cleOf(rows[i]));
    return idx.sort((a, b) => rank(a) - rank(b) || a - b);
  }, [rows]);
  const divergences = order.filter((i) => !rows[i].conforme);
  const bad = cmp ? cmp.total - cmp.conformes : 0;

  // ---------------------------------------------------------------- aller à un champ, à une zone
  const boxOf = (r: Row, side: "soumis" | "original"): { box: Box; estimated: boolean } | null => {
    const own = r.pos?.[side];
    if (own) return { box: own, estimated: false };
    const other = r.pos?.[side === "soumis" ? "original" : "soumis"];
    const first = side === "original" ? o?.numero_page || 1 : 1;
    if (other) return { box: { ...other, page: side === "original" ? first + (other.page - 1) : other.page - (o?.numero_page || 1) + 1 }, estimated: true };
    const [y0, y1] = ZONE_FALLBACK[ZONE_OF[cleOf(r)] || "notes"];
    return { box: { page: first, x0: 0.06, y0, x1: 0.94, y1 }, estimated: true };
  };
  const goTo = (i: number, smooth = true) => {
    const r = rows[i];
    if (!r) return;
    setSel(i);
    const s = boxOf(r, "soumis"), og = boxOf(r, "original");
    if (view === "calque") { if (og) paneC.current?.scrollToBox(og.box, smooth); return; }
    if (s) paneS.current?.scrollToBox(s.box, smooth);
    if (og) paneO.current?.scrollToBox(og.box, smooth);
  };
  const nextDivergence = (step = 1) => {
    if (!divergences.length) { if (cmp) toast("Aucune divergence : document conforme à l'original indexé.", { tone: "green", ms: 3000, sound: false }); return; }
    const k = sel === null ? -1 : divergences.indexOf(sel);
    const n = k < 0 ? (step > 0 ? 0 : divergences.length - 1) : (k + step + divergences.length) % divergences.length;
    goTo(divergences[n]);
  };
  const zoneBox = (z: Zone, side: "soumis" | "original"): Box | null => {
    const list = rows.filter((r) => ZONE_OF[cleOf(r)] === z).map((r) => r.pos?.[side]).filter((b): b is Box => !!b);
    const u = union(list);
    if (u) return u;
    if (side === "original" && !o) return null;
    const [y0, y1] = ZONE_FALLBACK[z];
    return { page: side === "original" ? (z === "decision" ? o!.page_fin : o!.numero_page) : 1, x0: 0.05, y0, x1: 0.95, y1 };
  };
  const goZone = (z: Zone) => {
    if (!path) return;
    setSel(null);
    setZoneFlash((f) => ({ zone: z, n: (f?.n || 0) + 1 }));
    const s = zoneBox(z, "soumis"), og = zoneBox(z, "original");
    if (view === "calque") { if (og) paneC.current?.scrollToBox(og); return; }
    if (s) paneS.current?.scrollToBox(s);
    if (og) paneO.current?.scrollToBox(og);
  };
  useEffect(() => {
    if (!zoneFlash) return;
    const t = window.setTimeout(() => setZoneFlash(null), 1900);
    return () => window.clearTimeout(t);
  }, [zoneFlash]);

  // ouverture sur la 1re divergence, dès que les deux volets sont dessinés
  const panesReady = view === "calque" ? !!stC?.ready : !!stS?.ready && !!stO?.ready;
  useEffect(() => {
    if (!autoJump.current || !cmp || !panesReady) return;
    autoJump.current = false;
    if (divergences.length) window.setTimeout(() => goTo(divergences[0], false), 60);
  }, [panesReady, cmp]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- surlignages posés sur les documents
  const marks = (side: "soumis" | "original"): Mark[] => {
    const out: Mark[] = [];
    rows.forEach((r, i) => {
      const isSel = sel === i;
      if (r.conforme && !isSel) return;
      const b = isSel ? boxOf(r, side) : r.pos?.[side] ? { box: r.pos[side]!, estimated: false } : null;
      if (!b) return;
      const tone = r.conforme ? "green" : side === "soumis" ? "red" : "blue";
      const tipSide = r.pos?.soumis || !r.pos?.original ? "soumis" : "original";
      out.push({ key: `r${i}`, ...b.box, tone, sel: isSel, estimated: b.estimated,
        tip: isSel && side === tipSide && view === "cote" ? <><b className="nm">{chipLabel(r)}</b>{r.conforme ? <> · identique <b>{r.original || "—"}</b></> : <> · soumis <s>{r.soumis || "—"}</s> · original <b>{r.original || "—"}</b></>}{b.estimated && " · position estimée"}</> : undefined });
    });
    if (zoneFlash) {
      const z = zoneBox(zoneFlash.zone, side);
      if (z) out.push({ key: `z${zoneFlash.n}`, ...z, tone: "zone" });
    }
    return out;
  };

  // ---------------------------------------------------------------- mode focus (F11) et clavier
  const enterFocus = () => { if (!path) return; setFocus(true); void sx.setFullScreen?.(true).catch(() => undefined); };
  const exitFocus = () => { setFocus(false); void sx.setFullScreen?.(false).catch(() => undefined); };
  useEffect(() => { if (!active && focus) exitFocus(); }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
  /** Touche simple : laissée aux champs de saisie ; un raccourci qui renvoie false laisse passer la touche. */
  const notTyping = (fn: () => void | boolean) => (e: KeyboardEvent) => {
    const t = e.target as HTMLElement | null;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return false;
    return fn();
  };
  useShortcuts({
    "ctrl+o": () => void load(), "ctrl+r": () => void report(), "ctrl+p": () => void printOriginal(),
    "f8": () => nextDivergence(1), "shift+f8": () => nextDivergence(-1),
    "alt+1": () => goZone("identite"), "alt+2": () => goZone("notes"), "alt+3": () => goZone("decision"),
    "t": notTyping(() => { if (!cmp) return false; setDrawer((d) => !d); }),
    "f11": () => (focus ? exitFocus() : enterFocus()),
    "arrowleft": notTyping(() => { if (view !== "calque") return false; setCurtain((c) => Math.max(0, c - 0.05)); }),
    "arrowright": notTyping(() => { if (view !== "calque") return false; setCurtain((c) => Math.min(1, c + 0.05)); }),
  }, active);
  // Échap par couches : tableau > plein écran > (application : nouvelle recherche au guichet)
  useShortcuts({ "escape": () => (drawer ? setDrawer(false) : exitFocus()) }, active && (drawer || focus));

  // ---------------------------------------------------------------- défilement lié
  const sync = (from: "s" | "o") => (y: number, x: number) => {
    if (!linked || view !== "cote") return;
    (from === "s" ? paneO : paneS).current?.setFrac(y, x);
  };

  const soumisInfo = cmp?.soumis || ident?.soumis || null;
  const srcS: DocSrc | null = path && isPdf(path) ? { kind: "pdf", file: path, first: 1, last: Math.min(20, soumisInfo?.pages || 20), version: path }
    : image ? { kind: "image", url: image } : null;
  const msgS = !path ? "Aucun document soumis." : srcS ? null : imageErr || "Chargement…";
  const srcO: DocSrc | null = o?.preview_pdf ? { kind: "pdf", file: o.preview_pdf, first: o.numero_page || 1, last: o.page_fin || 1, version: String(o.id) } : null;
  const msgO = srcO ? null : !path ? null : busy ? "Recherche de l'original…" : o ? "Aperçu de l'original indisponible (Word absent ?)." : "Aucun original indexé : saisissez le matricule déclaré dans la barre puis Entrée.";
  const scale = (view === "calque" ? stC : stO?.scale ? stO : stS)?.scale || null;
  // zoom lié : même échelle sur les deux volets (la plus petite des deux, pour que chacun tienne dans sa zone)
  const cap = view === "cote" && stS?.natural && stO?.natural ? Math.min(stS.natural, stO.natural) : null;
  const k = orig ? rels.findIndex((r) => r.id === orig) : -1;
  const noPos = soumisInfo && !soumisInfo.positions;

  const tagS = <><span className="sw s" />Soumis<span className="meta ellipsis">{natureLabel(soumisInfo)}{noPos ? " · lignes non situées" : ""}</span></>;
  const tagO = <><span className="sw o" />Original indexé<span className="meta ellipsis" title={o?.chemin}>{o ? `${o.chemin.split(/[\\/]/).slice(-3).join("\\")} · ${o.pages_label}` : ""}</span>
    {o && <Badge text={o.statut || "—"} tone={statutTone(o.statut)} />}</>;

  return (
    <>
      <header className="card topbar vf-top" data-testid="vf-topbar">
        <div className="vf-title"><span className="eyebrow">Authenticité</span><b>Vérification</b></div>
        <div className="vf-vsep" />
        <div className="vf-doc" title={path || undefined}>
          <Icon name="scan" size={17} color="#475569" />
          <div className="dtxt"><span className="eyebrow">Document soumis</span>
            <span className="dn ellipsis selectable" data-testid="vf-docname">{path ? baseName(path) : "PDF, photo ou scan reçu"}</span></div>
          {soumisInfo && <span className="badge t-gray vf-wide" data-testid="vf-nature" title="Nature du document et confiance de la lecture (OCR)">{natureLabel(soumisInfo)}</span>}
          <button className="btn ghost xs" onClick={() => void load()} data-testid="verify-load" title="Charger le document soumis (Ctrl+O)"><Icon name="upload" size={14} />Charger…<span className="kbd opt-wide">Ctrl+O</span></button>
        </div>
        <Icon name="chevron_right" size={16} color="#94A3B8" />
        <label className={`vf-mat${mat ? "" : " vf-nomat"}`} title="Matricule lu sur le document (modifiable) : Entrée = recherche exacte de l'original">
          <Icon name="user" size={15} color="#475569" />
          <input value={mat} onChange={(e) => { setMat(e.target.value); setMatLu(false); }} onKeyDown={(e) => e.key === "Enter" && void findOriginal()}
            placeholder="Matricule" spellCheck={false} data-testid="verify-mat" aria-label="Matricule déclaré" />
          {mat && <span className={`badge ${matLu ? "t-green" : "t-gray"}`} data-testid="vf-mat-badge" title={matLu ? "Lu automatiquement sur le document soumis" : "Saisi : Entrée pour rechercher l'original"}>{matLu ? "LU" : "SAISI"}</span>}
        </label>
        <div className={`vf-sel${rels.length ? "" : " off"}`} title="Original indexé : choisir parmi les relevés de ce matricule">
          <Icon name="doc" size={16} color="#475569" />
          <div className="dtxt"><span className="eyebrow">Original indexé{rels.length ? ` · ${k >= 0 ? k + 1 : "—"} sur ${rels.length}` : ""}</span>
            <b className="ellipsis" data-testid="vf-orig-label">{o ? `${periodShort(o)} · ${o.annee_academique || "?"} · ${o.statut || "?"}` : "—"}</b></div>
          <Icon name="chevron_down" size={15} color="#475569" />
          <select value={orig ?? ""} disabled={!rels.length} onChange={(e) => { const id = Number(e.target.value); if (!id) return; setOrig(id); if (path) void compare(path, id); }} data-testid="verify-orig" aria-label="Original indexé">
            <option value="">—</option>
            {rels.map((r) => <option key={r.id} value={r.id}>{r.periode} · {r.annee_academique} · {r.statut || "?"}</option>)}
          </select>
        </div>
        <span className="grow" />
        {ident && (ident.delivered
          ? <span className="badge t-green big" data-testid="vf-registre" title={`Fichier identique, à l'octet près, au document délivré le ${String(ident.delivered.horodatage).slice(0, 16)} (${String(ident.delivered.nature).toLowerCase()})`}>
              <Icon name="check" size={13} /><span className="lg">Délivré par le guichet · réf. {ident.delivered.reference}</span><span className="sh">Au registre</span></span>
          : <span className="badge t-amber big" data-testid="vf-registre" title="Empreinte du fichier absente du registre des délivrances (modifié, scanné, ou délivré avant Scolarix)">
              <Icon name="info" size={13} /><span className="lg">Absent du registre des délivrances</span><span className="sh">Hors registre</span></span>)}
        <div className="fseg" role="radiogroup" aria-label="Présentation">
          <button className={view === "cote" ? "on" : ""} role="radio" aria-checked={view === "cote"} onClick={() => setView("cote")} data-testid="vf-view-cote" title="Les deux documents côte à côte"><Icon name="layers" size={14} /> Côte à côte</button>
          <button className={view === "calque" ? "on" : ""} role="radio" aria-checked={view === "calque"} onClick={() => setView("calque")} data-testid="vf-view-calque"
            title="Calque : original et soumis superposés au même zoom, rideau glissant (← →)"><Icon name="eye" size={14} /> Calque</button>
        </div>
      </header>

      <div className="vf-body">
        <section className="card vf-strip" data-testid="vf-strip">
          {cmp ? (
            <div className={`vf-verdict ${bad ? "t-red" : "t-green"}`} data-testid="verdict">
              <Icon name={bad ? "alert" : "check"} size={20} />
              <div className="vt"><b>{bad ? "Non conforme" : "Conforme"}</b><span>{bad ? `${plural(bad, "divergence")} · ` : ""}{cmp.conformes}/{cmp.total} identiques</span></div>
            </div>
          ) : (
            <div className="vf-verdict t-gray"><Icon name={busy ? "clock" : "shield"} size={20} />
              <div className="vt"><b>{busy ? "Lecture…" : "En attente"}</b><span>{busy || (path ? "Original à choisir" : "Aucun document soumis")}</span></div></div>
          )}
          <div className="vf-chips" data-testid="vf-chips">
            {busy && <span className="row muted small"><span className="spinner" />{busy}</span>}
            {!cmp && !busy && <span className="muted small ellipsis">{path ? "Choisissez l'original indexé (matricule puis Entrée) pour lancer la comparaison." : "Ctrl+O : charger le PDF, la photo ou le scan reçu (employeur, université, ambassade…). Le matricule est lu sur le document et l'original retrouvé tout seul."}</span>}
            {cmp && !busy && chips.map((c) => {
              if (c.kind === "group") return (
                <button key="grp" className="vf-chip ok grp" onClick={() => setExpandUe(true)} data-testid="vf-chip-group" title="Unités d'enseignement identiques : cliquer pour les afficher une par une">
                  <Icon name="check" size={13} stroke={2.6} />{c.rows.length} autre{c.rows.length > 1 ? "s" : ""} UE</button>);
              const r = rows[c.i];
              return (
                <button key={c.i} className={`vf-chip ${r.conforme ? "ok" : "ko"}${sel === c.i ? " sel" : ""}`} onClick={() => goTo(c.i)} data-testid="vf-chip" data-cle={cleOf(r)}
                  title={`${r.champ} — soumis : ${r.soumis || "—"} · original : ${r.original || "—"}\n(cliquer : les deux documents vont à cette ligne)`}>
                  <Icon name={r.conforme ? "check" : "x"} size={13} stroke={2.6} />{chipLabel(r)}
                  {cleOf(r) === "ue" && r.code && !r.conforme && <span className="lbl13"> {r.champ.length > 18 ? `${r.champ.slice(0, 17)}…` : r.champ}</span>}
                  {!r.conforme && <span className="vals"><s>{r.soumis || "—"}</s> ≠ {r.original || "—"}</span>}
                </button>);
            })}
            {cmp && expandUe && <button className="btn link tiny" onClick={() => setExpandUe(false)}>replier</button>}
          </div>
          <button className={`btn ghost xs${drawer ? " on" : ""}`} disabled={!cmp} onClick={() => setDrawer((d) => !d)} data-testid="vf-table-btn" title="Tableau complet champ par champ (T)">
            <Icon name="filter" size={14} />Tableau<span className="kbd">T</span></button>
        </section>

        <div className={`vf-stage${focus ? " focus" : ""}`} data-testid="vf-stage">
          {focus && (
            <div className="fbar vf-fbar" data-testid="vf-focus-bar">
              <b className="nm ellipsis">{o?.nom_complet || (path ? baseName(path) : "")}</b>
              {o && <span className="mono">{o.matricule}</span>}
              {cmp && <span className={`badge ${bad ? "t-red" : "t-green"}`}><Icon name={bad ? "alert" : "check"} size={12} />{bad ? plural(bad, "divergence") : "Conforme"}</span>}
              <button className="btn ghost sm" disabled={!cmp} onClick={() => nextDivergence(1)}><Icon name="alert" size={14} />Divergence<span className="kbd">F8</span></button>
              <button className="btn primary sm" disabled={!cmp} onClick={() => void report()}><Icon name="doc" size={15} />Rapport<span className="kbd">Ctrl+R</span></button>
              <button className="btn ghost sm" onClick={exitFocus} data-testid="vf-focus-exit" title="Quitter le plein écran (Échap)"><Icon name="x" size={14} />Quitter<span className="kbd">Échap</span></button>
            </div>
          )}
          <div className={`vf-panes${view === "calque" || !path ? " one" : ""}`}>
            {!path ? (
              <div className="vf-empty" data-testid="vf-empty">
                <div className="vf-empty-card">
                  <Icon name="shield" size={34} color="#1D4ED8" />
                  <h2 className="h2">Vérifier un relevé soumis</h2>
                  <p className="muted small">Le document reçu (PDF, photo ou scan) est lu, son matricule reconnu et l'original indexé retrouvé.
                    Les deux s'affichent côte à côte, les divergences surlignées sur les documents.</p>
                  <button className="btn primary" onClick={() => void load()} data-testid="verify-load-empty"><Icon name="upload" />Charger le document soumis<span className="kbd">Ctrl+O</span></button>
                </div>
              </div>
            ) : view === "cote" ? (
              <>
                <ComparePane ref={paneS} testid="vf-pane-soumis" layers={[{ src: srcS, marks: marks("soumis"), testid: "verify-submitted" }]}
                  message={msgS} fit={fit} manual={manual} onZoom={onZoom} onState={setStS} onUserScroll={sync("s")} tag={tagS} cap={cap} onDoubleClick={() => (focus ? exitFocus() : enterFocus())} />
                <ComparePane ref={paneO} testid="vf-pane-original" layers={[{ src: srcO, marks: marks("original"), testid: "verify-original" }]}
                  message={msgO} fit={fit} manual={manual} onZoom={onZoom} onState={setStO} onUserScroll={sync("o")} tag={tagO} cap={cap} onDoubleClick={() => (focus ? exitFocus() : enterFocus())} />
              </>
            ) : (
              <ComparePane ref={paneC} testid="vf-pane-calque" message={srcO ? null : msgO || "Aucun original à superposer."}
                layers={[{ src: srcO, marks: marks("original"), testid: "verify-original" }, { src: srcS, marks: marks("soumis"), testid: "verify-submitted" }]}
                fit={fit} manual={manual} onZoom={onZoom} onState={setStC} curtain={{ value: curtain, onChange: setCurtain }}
                tag={<><span className="sw o" />Original<span className="meta">◀ glisser ▶</span><span className="sw s" />Soumis{o && <span className="meta ellipsis">{o.pages_label}</span>}</>}
                onDoubleClick={() => (focus ? exitFocus() : enterFocus())} />
            )}
            {drawer && cmp && <TableDrawer rows={rows} order={order} sel={sel} onPick={goTo} onClose={() => setDrawer(false)} />}
          </div>
          {path && <Dock disabled={!cmp} hasOrig={!!orig} printing={printing} onReport={() => void report()} onPrint={() => void printOriginal()}
            demandeur={demandeur} setDemandeur={setDemandeur} reference={reference} setReference={setReference} demandeurRef={demandeurRef}
            fit={fit} setFit={setFit} scale={scale} zoom={(f) => onZoom(Math.max(0.25, Math.min(4, (scale || 1) * f)))} onFocus={enterFocus} canFocus={!!path}
            linked={linked} setLinked={setLinked} calque={view === "calque"} onNext={() => nextDivergence(1)} nDiv={divergences.length} onZone={goZone} hasDoc={!!path} />}
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ tiroir « Tableau » : le détail complet
function TableDrawer({ rows, order, sel, onPick, onClose }: { rows: Row[]; order: number[]; sel: number | null; onPick: (i: number) => void; onClose: () => void }) {
  return (
    <aside className="vf-drawer" data-testid="verify-table" aria-label="Comparaison champ par champ">
      <div className="row"><h2 className="h3 grow">Comparaison champ par champ</h2>
        <button className="btn ghost sm icon" onClick={onClose} aria-label="Fermer le tableau" title="Fermer (Échap ou T)"><Icon name="x" size={14} /></button></div>
      <div className="table-wrap">
        <table className="grid">
          <colgroup><col style={{ width: 150 }} /><col /><col /><col style={{ width: 92 }} /></colgroup>
          <thead><tr><th>Champ</th><th>Document soumis</th><th>Original</th><th /></tr></thead>
          <tbody>{order.map((i) => {
            const r = rows[i];
            return (
              <tr key={i} className={`${r.conforme ? "" : "ko"}${sel === i ? " sel" : ""}`} onClick={() => onPick(i)} title="Aller à cette ligne sur les deux documents">
                <td className="small">{r.code ? <><b className="mono">{r.code}</b> </> : null}{r.champ}</td>
                <td className="selectable small">{r.soumis || "—"}</td><td className="selectable small">{r.original || "—"}</td>
                <td><Badge text={r.conforme ? "OK" : "DIFFÉRENT"} tone={r.conforme ? "green" : "red"} /></td></tr>);
          })}</tbody>
        </table>
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------ dock : conclure, rapport, affichage, aller à
interface DockProps {
  disabled: boolean; hasOrig: boolean; printing: boolean; onReport: () => void; onPrint: () => void;
  demandeur: string; setDemandeur: (v: string) => void; reference: string; setReference: (v: string) => void; demandeurRef: React.RefObject<HTMLInputElement | null>;
  fit: FitMode; setFit: (m: FitMode) => void; scale: number | null; zoom: (f: number) => void; onFocus: () => void; canFocus: boolean;
  linked: boolean; setLinked: (v: boolean) => void; calque: boolean; onNext: () => void; nDiv: number; onZone: (z: Zone) => void; hasDoc: boolean;
}
function Dock(p: DockProps): ReactNode {
  return (
    <aside className="dock vf-dock" data-testid="vf-dock" aria-label="Conclusion et affichage">
      <div className="grp">
        <span className="lbl">Conclure</span>
        <button className="btn primary" disabled={p.disabled} onClick={p.onReport} data-testid="verify-report" title="Rapport de vérification en PDF (Ctrl+R)">
          <span className="r"><Icon name="doc" size={18} />Rapport</span><span className="kbd">Ctrl+R</span></button>
        <button className="btn secondary two" disabled={!p.hasOrig || p.printing} onClick={p.onPrint} data-testid="verify-print" title="Imprimer l'original avec le timbre « Vérifié » (Ctrl+P)">
          <span className="r">{p.printing ? <span className="spinner" /> : <Icon name="printer" size={15} />}Imprimer<span className="kbd">Ctrl+P</span></span><span className="sub">original « Vérifié »</span></button>
      </div>
      <div className="grp">
        <span className="lbl">Pour le rapport</span>
        <span className="mini-l">Demandeur</span>
        <input ref={p.demandeurRef} className="field" placeholder="Ambassade, Ordre, université…" value={p.demandeur} onChange={(e) => p.setDemandeur(e.target.value)} data-testid="verify-demandeur" aria-label="Organisme demandeur" />
        <span className="mini-l">Référence</span>
        <input className="field" placeholder="N° de courrier" value={p.reference} onChange={(e) => p.setReference(e.target.value)} data-testid="verify-reference" aria-label="Référence du courrier" />
      </div>
      <div className="grp">
        <span className="lbl row" style={{ gap: 4 }}><span className="grow">Affichage</span><span className="pct" data-testid="vf-scale">{p.scale ? `${Math.round(p.scale * 100)} %` : ""}</span></span>
        <div className="fitgrid" role="radiogroup" aria-label="Ajustement des documents">
          {FIT_LABELS.map(([m, l, t]) => (
            <button key={m} className={`btn ghost xs${p.fit === m ? " on" : ""}`} role="radio" aria-checked={p.fit === m} title={`${t} (les deux volets)`} onClick={() => p.setFit(m)} data-testid={`vf-fit-${m}`}>{l}</button>
          ))}
        </div>
        <div className="fitgrid three">
          <button className="btn ghost xs" aria-label="Zoom arrière" title="Zoom arrière (ou Ctrl + molette)" onClick={() => p.zoom(1 / 1.2)}>−</button>
          <button className="btn ghost xs" aria-label="Zoom avant" title="Zoom avant (ou Ctrl + molette)" onClick={() => p.zoom(1.2)}>+</button>
          <button className="btn ghost xs" disabled={!p.canFocus} onClick={p.onFocus} data-testid="vf-focus-open" aria-label="Plein écran" title="Les deux volets en plein écran (F11, double-clic ; Échap pour sortir)"><Icon name="fit" size={14} /></button>
        </div>
        <span data-testid="vf-linked" className={p.calque ? "vf-off" : ""}><Switch blue checked={p.linked} onChange={p.setLinked} label={<span className="small">Défilement lié</span>}
          title="Défilement lié : faire défiler un volet fait défiler l'autre au même endroit" /></span>
      </div>
      <div className="grp">
        <span className="lbl">Aller à</span>
        <button className="btn ghost xs zone" disabled={!p.nDiv} onClick={p.onNext} data-testid="vf-next" title="Divergence suivante (F8 ; Maj+F8 : précédente)">
          <Icon name="alert" size={13} /><span className="grow">Divergence</span><span className="kbd">F8</span></button>
        {ZONES.map((z) => (
          <button key={z.id} className="btn ghost xs zone" disabled={!p.hasDoc} onClick={() => p.onZone(z.id)} title={`${z.label} : les deux documents vont à cette zone (${z.key})`} data-testid={`vf-zone-${z.id}`}>
            <Icon name={z.icon} size={13} /><span className="grow">{z.label}</span><span className="kbd">{z.key}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}
