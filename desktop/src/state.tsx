import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { call, sx, type Info, type Settings, type Stats } from "./api";

export type PageId = "guichet" | "demandes" | "indexation" | "extraction" | "verification";
export const PAGES: { id: PageId; label: string; icon: string }[] = [
  { id: "guichet", label: "Guichet", icon: "search" },
  { id: "demandes", label: "Demandes", icon: "folder" },
  { id: "indexation", label: "Indexation", icon: "database" },
  { id: "extraction", label: "Extraction", icon: "chart" },
  { id: "verification", label: "Vérification", icon: "shield" },
];

interface AppState {
  info: Info | null;
  settings: Settings | null;
  stats: Stats | null;
  page: PageId;
  go: (p: PageId) => void;
  refreshStats: () => void;
  saveSettings: (v: Partial<Settings>) => Promise<Settings>;
  activeDemande: number | null;
  setActiveDemande: (id: number | null) => void;
  demandeVersion: number;              // change quand une demande est modifiée (rafraîchir les vues)
  bumpDemandes: () => void;
  compact: boolean;
  engine: { state: string; message?: string };
  indexing: { running: boolean; last: any };
  openSettings: (tab?: SettingsTab) => void;
  settingsOpen: boolean;
  settingsTab: SettingsTab;
  closeSettings: () => void;
  searchFocus: number;                 // change quand Échap / Ctrl+F demande la recherche (depuis tout écran)
  focusSearch: () => void;
}

export type SettingsTab = "poste" | "indexation" | "exports" | "corrections";

const Ctx = createContext<AppState>(null as unknown as AppState);
export const useApp = () => useContext(Ctx);

export function AppProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<Info | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [page, setPage] = useState<PageId>("guichet");
  const [activeDemande, setActiveDemande] = useState<number | null>(null);
  const [demandeVersion, setDemandeVersion] = useState(0);
  const [compact, setCompact] = useState(window.innerHeight < 880);
  const [engine, setEngine] = useState<{ state: string; message?: string }>({ state: "starting" });
  const [indexing, setIndexing] = useState<{ running: boolean; last: any }>({ running: false, last: null });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("poste");
  const [searchFocus, setSearchFocus] = useState(0);
  const loaded = useRef(false);

  const load = useCallback(async () => {
    try {
      const i = await call<Info>("info");
      setInfo(i);
      setSettings(i.settings);
      setStats(i.stats);
      setEngine({ state: "ready" });      // le moteur a répondu : il est prêt (même si son signal est parti avant nous)
      loaded.current = true;
      if (i.settings.ui_scale && i.settings.ui_scale !== 100) void sx.setZoom(i.settings.ui_scale / 100);
      document.body.classList.toggle("reduce-motion", !i.settings.ui_animations);
    } catch (e) {
      setEngine({ state: "error", message: String((e as Error).message) });
    }
  }, []);

  useEffect(() => {
    void load();
    const offS = sx.onEngineStatus((st) => {
      setEngine(st);
      if (st.state === "ready" && !loaded.current) void load();
    });
    const offE = sx.onEngineEvent((msg) => {
      if (msg.event !== "index") return;
      const t = msg.data.type;
      setIndexing({ running: t !== "exit", last: msg.data });
      if (t === "exit") void call<Stats>("stats").then(setStats).catch(() => undefined);
    });
    const onResize = () => setCompact(window.innerHeight < 880);
    window.addEventListener("resize", onResize);
    return () => { offS(); offE(); window.removeEventListener("resize", onResize); };
  }, [load]);

  const refreshStats = useCallback(() => { void call<Stats>("stats").then(setStats).catch(() => undefined); }, []);
  const saveSettings = useCallback(async (v: Partial<Settings>) => {
    const s = await call<Settings>("settings_save", { values: v });
    setSettings(s);
    document.body.classList.toggle("reduce-motion", !s.ui_animations);
    return s;
  }, []);

  return (
    <Ctx.Provider value={{
      info, settings, stats, page, go: setPage, refreshStats, saveSettings, activeDemande, setActiveDemande,
      demandeVersion, bumpDemandes: () => setDemandeVersion((v) => v + 1), compact, engine, indexing,
      openSettings: (tab: SettingsTab = "poste") => { setSettingsTab(tab); setSettingsOpen(true); }, settingsOpen, settingsTab,
      closeSettings: () => setSettingsOpen(false),
      searchFocus, focusSearch: () => { setPage("guichet"); setSearchFocus((n) => n + 1); },
    }}>
      {children}
    </Ctx.Provider>
  );
}

// ------------------------------------------------------------------ raccourcis clavier
// Chaque page déclare ses raccourcis quand elle est affichée ; ils sont prioritaires sur ceux de l'application.
// Un raccourci qui renvoie false laisse passer la touche (au raccourci suivant, puis au comportement normal).
type Handler = (e: KeyboardEvent) => void | boolean;
const registry: { id: number; map: Record<string, Handler> }[] = [];
let seq = 0;

export function keyName(e: KeyboardEvent) {
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push("ctrl");
  if (e.shiftKey) parts.push("shift");
  if (e.altKey) parts.push("alt");
  let k = e.key.length === 1 ? e.key.toLowerCase() : e.key.toLowerCase();
  if (k === "+" || k === "=") k = "plus";
  if (k === "-" || k === "_") k = "minus";
  if (e.code && /^Digit\d$/.test(e.code)) k = e.code.slice(5);
  parts.push(k);
  return parts.join("+");
}

export function useShortcuts(map: Record<string, Handler>, active = true) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!active) return;
    const entry = { id: ++seq, map: {} as Record<string, Handler> };
    for (const k of Object.keys(ref.current)) entry.map[k] = (e) => ref.current[k]?.(e) ?? undefined;
    registry.push(entry);
    return () => { const i = registry.indexOf(entry); if (i >= 0) registry.splice(i, 1); };
  }, [active, Object.keys(map).join("|")]);
}

export function installShortcutListener() {
  window.addEventListener("keydown", (e) => {
    if (document.querySelector(".overlay")) return;      // fenêtre modale ouverte : elle gère ses touches
    const name = keyName(e);
    for (let i = registry.length - 1; i >= 0; i--) {
      const h = registry[i].map[name];
      if (h) {
        if (h(e) === false) continue;
        e.preventDefault();
        return;
      }
    }
  });
}
