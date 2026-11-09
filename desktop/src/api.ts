// Accès au moteur et au système, typé. Tout passe par window.scolarix (preload) : aucune API Node dans l'interface.

export type Tone = "green" | "amber" | "red" | "blue" | "violet" | "gray";

export interface Releve {
  id: number;
  matricule: string;
  nom: string | null;
  prenom: string | null;
  nom_complet: string;
  date_naissance: string | null;
  filiere: string | null;
  filiere_code: string | null;
  niveau: string | null;
  niveau_num: number;
  annee_academique: string | null;
  session: string | null;
  statut: string | null;
  moyenne: string | null;
  grade: string | null;
  decision: string | null;
  credits: number | null;
  numero_page: number;
  page_fin: number;
  semestre: number | null;
  semestre_fin: number | null;
  chemin: string;
  type_fichier: string;
  score: number;
  flags: string[];
  nb_pages: number;
  pages_label: string;
  preview_pdf: string | null;
  is_success: boolean;
  periode: string;
  sem_label: string;
  annuel: boolean;
  is_word: boolean;
  ocr_confiance: number | null;
  selected?: boolean;
}

export interface Alert { text: string; tone: Tone }

export interface ReleveDetail {
  releve: Releve;
  siblings: Releve[];
  alerts: Alert[];
  needs_render: boolean;
  preview_pdf: string | null;
}

export interface SearchResult {
  q: string;
  results: Releve[];
  counts: Record<string, number>;
  ms: number;
  students: number;
}

export interface Stats {
  releves: number;
  etudiants: number;
  fichiers: number;
  imprimes: number;
  duplicatas: number;
  anomalies: number;
  derniere_indexation: string | null;
  annees: [string, number][];
  formats: Record<string, number>;
}

export interface Settings {
  operateur: string;
  poste: string;
  source_root: string;
  exports_root: string;
  exports_retention_days: number;
  printer_name: string;
  print_command: string;
  duplex: boolean;
  fuzzy_threshold: number;
  max_results: number;
  ocr_enabled: boolean;
  differential: boolean;
  index_workers: number;
  exclusions: string[];
  text_rules_enabled: boolean;
  text_rules: TextRule[];
  ui_sounds: boolean;
  ui_animations: boolean;
  ui_scale: number;
  delivery_ref_stamp: boolean;
  docx_convert_at_index: boolean;
}

export interface TextRule {
  actif?: boolean;
  description?: string;
  chercher: string;
  remplacer: string;
  regex?: boolean;
  sauf_si?: string;
  taille?: number | null;
  gras?: boolean | null;
  italique?: boolean | null;
  police?: string | null;
}

export interface Info {
  app: string;
  version: string;
  operateur: string;
  poste: string;
  rules_active: boolean;
  ui_scales: number[];
  stats: Stats;
  settings: Settings;
}

export interface Demande {
  id: number;
  numero: string;
  creee_le: string;
  maj_le: string;
  matricule: string | null;
  nom: string | null;
  demandeur: string | null;
  motif: string | null;
  duplicata: number;
  statut: string;
  dossier: string | null;
  n: number;
}

export interface DemandeItem {
  item: { id: number; releve_id: number; ordre: number; etat: string | null; reference: string | null; fichier: string | null };
  releve: Releve | null;
}

export interface DemandeFull { demande: Demande; items: DemandeItem[]; added?: boolean }

export interface CheckResult { ok: boolean; kind?: "render" | "visual" | "blocked"; message?: string; pages?: number[]; nom?: string; matricule?: string }

export interface Group { matricule: string; nom: string; found: Releve[]; missing: string[]; valides: number; total: number; tone: Tone }

interface Bridge {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
  printReleve(p: { releve_id: number; duplicata: boolean; visually_checked?: boolean }): Promise<{ ref: string; how: string; pages_label: string; stats: Stats }>;
  printCursus(p: { matricule: string; releve_ids: number[]; cover: boolean; duplicata: boolean }): Promise<{ ref: string; how: string; pages: number; missing: string[]; stats: Stats }>;
  printDemande(p: { did: number; with_bordereau: boolean }): Promise<DemandeFull & { stats: Stats }>;
  printFile(file: string): Promise<string>;
  printers(): Promise<{ name: string; isDefault: boolean }[]>;
  readPdf(file: string): Promise<Uint8Array>;
  reveal(file: string): Promise<boolean>;
  openPath(file: string): Promise<string>;
  pickFile(opts?: { title?: string; filters?: { name: string; extensions: string[] }[]; defaultPath?: string }): Promise<string | null>;
  pickFolder(opts?: { title?: string; defaultPath?: string }): Promise<string | null>;
  saveFile(opts?: { title?: string; defaultPath?: string; filters?: { name: string; extensions: string[] }[] }): Promise<string | null>;
  appInfo(): Promise<{ version: string; logs: string; platform: string; e2e: boolean }>;
  setZoom(factor: number): Promise<boolean>;
  log(level: string, text: string): Promise<boolean>;
  relaunch(): Promise<boolean>;
  onEngineEvent(cb: (msg: { event: string; data: any }) => void): () => void;
  onEngineStatus(cb: (st: { state: string; message?: string }) => void): () => void;
  printPageReady(payload: { id: string; pages?: number; error?: string }): void;
}

declare global {
  interface Window { scolarix: Bridge }
}

export const sx = window.scolarix;

export function call<T>(method: string, params?: Record<string, unknown>): Promise<T> {
  return sx.call<T>(method, params);
}

export const FILIERES: { code: string | null; label: string }[] = [
  { code: null, label: "Toutes" },
  { code: "M", label: "Médecine" },
  { code: "P", label: "Pharmacie" },
  { code: "B", label: "Biomédical" },
  { code: "MS", label: "Masters" },
];
export const FIL_SHORT: Record<string, string> = { M: "Médecine", P: "Pharmacie", B: "Biomédical", MS: "Master", D: "Doctorat" };

export function statutTone(statut: string | null | undefined): Tone {
  const s = (statut || "").toUpperCase();
  if (s.startsWith("ADMIS") || s.startsWith("VALID")) return "green";
  if (!s) return "gray";
  if (s.includes("VÉRIFIER") || s.includes("VERIFIER")) return "amber";
  return "red";
}

export function flagTone(flag: string): Tone {
  if (flag === "SCAN OCR") return "gray";
  if (flag === "FICHIER INTROUVABLE" || flag === "MATRICULE ILLISIBLE") return "red";
  if (flag === "✓ DEMANDE") return "green";
  return "amber";
}

export const plural = (n: number, word: string, many?: string) => `${n} ${n > 1 ? many || word + "s" : word}`;
