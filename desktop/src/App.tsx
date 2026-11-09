import { Component, useEffect, type ReactNode } from "react";
import { sx } from "./api";
import { AppProvider, PAGES, installShortcutListener, useApp, useShortcuts, type PageId } from "./state";
import { DialogHost, Icon, ToastHost, configureSounds, useToast } from "./components/ui";
import { Guichet } from "./pages/Guichet";
import { DemandesPage } from "./pages/Demandes";
import { Indexation } from "./pages/Indexation";
import { Extraction } from "./pages/Extraction";
import { Verification } from "./pages/Verification";
import { SettingsDialog } from "./pages/Settings";

installShortcutListener();

// Une erreur dans un écran n'emporte pas l'application : l'écran affiche l'erreur et peut être rechargé.
class Boundary extends Component<{ children: ReactNode; name: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error) { void sx.log("error", `écran ${this.props.name} : ${error.stack || error}`); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card empty">
        <h2 className="h2">Cet écran a rencontré une erreur</h2>
        <div className="muted selectable">{String(this.state.error.message)}</div>
        <button className="btn primary" onClick={() => this.setState({ error: null })}>Recharger l'écran</button>
      </div>
    );
  }
}

function Rail() {
  const { page, go, stats } = useApp();
  return (
    <nav className="rail" aria-label="Navigation">
      <img className="logo" src="./scolarix_symbole.svg" alt="Scolarix" />
      {PAGES.map((p, i) => (
        <button key={p.id} className={`nav${page === p.id ? " active" : ""}`} onClick={() => go(p.id)}
          aria-current={page === p.id ? "page" : undefined} title={`${p.label} (Ctrl+${i + 1})`} data-testid={`nav-${p.id}`}>
          <Icon name={p.icon} size={21} />
          {p.label}
          <span className="kbd">Ctrl+{i + 1}</span>
          {p.id === "indexation" && !!stats?.anomalies && <span className="count" title="Anomalies à revoir">{stats.anomalies}</span>}
        </button>
      ))}
      <div className="spacer" />
    </nav>
  );
}

function EngineBanner() {
  const { engine } = useApp();
  const toast = useToast();
  useEffect(() => {
    if (engine.state === "restarting") toast(engine.message || "Le moteur a redémarré.", { tone: "amber", ms: 10000 });
  }, [engine, toast]);
  if (engine.state === "error") {
    return <div className="banner t-red" role="alert"><Icon name="alert" />{engine.message || "Moteur indisponible."}</div>;
  }
  if (engine.state === "starting") return <div className="banner t-blue"><span className="spinner" />Démarrage du moteur…</div>;
  return null;
}

function Shell() {
  const app = useApp();
  const toast = useToast();
  useEffect(() => { if (app.settings) configureSounds(app.settings.ui_sounds); }, [app.settings]);
  const zoom = async (step: number) => {
    if (!app.info || !app.settings) return;
    const scales = app.info.ui_scales;
    const cur = scales.includes(app.settings.ui_scale) ? app.settings.ui_scale : 100;
    const k = scales.indexOf(cur);
    const next = step === 0 ? 100 : scales[Math.max(0, Math.min(scales.length - 1, k + step))];
    if (next === cur) { toast(`Taille de l'interface déjà ${step > 0 ? "maximale" : step < 0 ? "minimale" : "normale"} (${cur} %).`, { tone: "blue", ms: 3000, sound: false }); return; }
    await sx.setZoom(next / 100);              // immédiat : pas besoin de redémarrer
    await app.saveSettings({ ui_scale: next });
    toast(`Taille de l'interface : ${next} %`, { tone: "blue", ms: 2500, sound: false });
  };
  const nav: Record<string, () => void> = {};
  PAGES.forEach((p, i) => { nav[`ctrl+${i + 1}`] = () => app.go(p.id as PageId); });
  useShortcuts({ ...nav, "ctrl+plus": () => void zoom(1), "ctrl+shift+plus": () => void zoom(1), "ctrl+minus": () => void zoom(-1), "ctrl+0": () => void zoom(0) });

  const body: Record<PageId, ReactNode> = {
    guichet: <Guichet />, demandes: <DemandesPage />, indexation: <Indexation />, extraction: <Extraction />, verification: <Verification />,
  };
  return (
    <div className={`app${app.compact ? " compact" : ""}`}>
      <Rail />
      <main className="main">
        <EngineBanner />
        {PAGES.map((p) => (
          <div key={p.id} className="page" style={{ display: app.page === p.id ? "flex" : "none" }} data-page={p.id}>
            <Boundary name={p.label}>{(app.page === p.id || p.id === "guichet") && body[p.id]}</Boundary>
          </div>
        ))}
      </main>
      {app.settingsOpen && <SettingsDialog />}
    </div>
  );
}

export function App() {
  return (
    <AppProvider>
      <ToastHost>
        <DialogHost>
          <Shell />
        </DialogHost>
      </ToastHost>
    </AppProvider>
  );
}
