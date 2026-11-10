// Écran Indexation (2.1) : types des réponses du moteur, petites aides de mise en forme, icônes propres à l'écran.
import { ICONS } from "./icons";

// Icônes de l'écran (ajoutées ici plutôt que dans icons.ts, partagé par tous les écrans)
Object.assign(ICONS, {
  wrench: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>',
  eye_off: '<path d="M3 3l18 18"/><path d="M10.6 5.1A10.9 10.9 0 0 1 12 5c6 0 10 7 10 7a17.6 17.6 0 0 1-3.2 3.9M6.6 6.6A17.2 17.2 0 0 0 2 12s4 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
  folder_open: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v1"/><path d="M3 7v11a1 1 0 0 0 1 1h14l3-8H7l-3 8"/>',
});

export interface Pass {
  id: number; debut: string; fin: string | null; mode: string; source: string; fichiers: number; lus: number;
  ecrits: number; echecs: number; ignores: number; exclus: number; releves_delta: number | null;
  anomalies_delta: number | null; duree_s: number | null; arretee: number; workers: number | null;
}
export interface CauseCount { code: string; niveau: Niveau; label: string; fix: string; n: number; fichiers: number }
export interface Fix { code: string; titre: string; texte: string; action: "ocr_enable" | "settings" | "reread"; bouton: string | null; n: number; fichiers: number }
export type Niveau = "ERREUR" | "AVERTISSEMENT" | "INFO";

export interface Overview {
  releves: number; etudiants: number; pages: number; fichiers: number;
  sante: { fichiers: number; ok: number; avertissement: number; echec: number; exclus: number | null };
  aujourdhui: { imprimes: number; duplicatas: number; verifications: number; extractions: number };
  formats: { type: string; label: string; n: number; echecs: number; note: string }[];
  annees: { annee: string; n: number; trou: boolean }[];
  anomalies: { a_traiter: number; a_traiter_fichiers: number; tout: number; tout_fichiers: number; causes: number };
  causes: (Omit<CauseCount, "n" | "fichiers"> & { a_traiter: number; a_traiter_fichiers: number })[];
  fix: Fix | null;
  passes: Pass[];
  derniere_indexation: string | null;
  integrite: { date: string | null; orphelins: number; verifiee: boolean };
  outils: { ocr: boolean; tesseract: boolean };
  source: { chemin: string; existe: boolean };
}

export type Vue = "a_traiter" | "vues" | "tout";
export interface Summary { vue: Vue; causes: CauseCount[]; totaux: Record<Vue, { n: number; fichiers: number }>; fix: Fix | null }
export interface Filter { cause: string; vue: Vue; q?: string; dossier?: string; annee?: string }
export interface FileRow {
  chemin: string; nom: string; dossier: string; annee: string | null; n: number; message: string; niveau: Niveau;
  codes: string[]; vu: boolean; nb_pages: number | null; type_fichier: string | null; statut: string | null; page: number | null;
}
export interface FilesPage { offset: number; rows: FileRow[]; total: number; anomalies: number; dossiers: { dossier: string; label: string; n: number }[]; annees: { annee: string; n: number }[] }
export interface Detail {
  chemin: string; nom: string; dossier: string; dossier_label: string; existe: boolean; type_fichier: string | null;
  nb_pages: number | null; statut: string | null; preview_pdf: string | null; is_word: boolean; releve_id: number | null;
  anomalies: { id: number; code: string; label: string; niveau: Niveau; message: string; action: string | null; horodatage: string; vu: boolean; page: number | null }[];
}

export const NIVEAUX: [Niveau, string][] = [["ERREUR", "Erreurs"], ["AVERTISSEMENT", "Avertissements"], ["INFO", "Infos"]];
export const NIV_TONE: Record<string, "red" | "amber" | "blue"> = { ERREUR: "red", AVERTISSEMENT: "amber", INFO: "blue" };
export const NIV_ONE: Record<string, string> = { ERREUR: "Erreur", AVERTISSEMENT: "Avertissement", INFO: "Information" };

export const fr = (x: number | null | undefined) => (x || 0).toLocaleString("fr-FR");
export const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
export const short = (p: string, n = 90) => (p.length > n ? "…" + p.slice(-n) : p);
export const pl = (n: number, one: string, many?: string) => `${fr(n)} ${n > 1 ? many || one + "s" : one}`;

/** « 2 h 14 min », « 47 min », « 12 s » */
export function duree(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  if (s >= 60) return `${Math.floor(s / 60)} min ${String(Math.floor(s % 60)).padStart(2, "0")} s`;
  return `${Math.round(s * 10) / 10} s`.replace(".", ",");
}
/** « 2026-11-09 18:42:10 » → « 09/11 18:42 » (ou « 09/11/2026 18:42 » si long) */
export function dateCourte(d: string | null | undefined, long = false): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(d || "");
  if (!m) return d || "—";
  return `${m[3]}/${m[2]}${long ? `/${m[1]}` : ""} ${m[4]}:${m[5]}`;
}
export const MODE_LABEL: Record<string, string> = { DIFFERENTIELLE: "Différentielle", COMPLETE: "Complète", RELECTURE: "Relecture" };
export const signed = (n: number | null | undefined) => (n == null ? "—" : n > 0 ? `+${fr(n)}` : n < 0 ? `−${fr(-n)}` : "0");

/** La touche vient-elle d'une zone de saisie ? (les raccourcis à une lettre ne s'y appliquent pas) */
export function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
}
