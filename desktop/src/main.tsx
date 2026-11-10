import { createRoot } from "react-dom/client";
import "./styles.css";
import "./styles/verification.css";
import "./styles/indexation.css";
import "./styles/revue.css";
import { App } from "./App";
import { sx } from "./api";

// Toute erreur de l'interface est écrite dans le journal (dossier logs) : plus jamais de plantage sans trace.
window.addEventListener("error", (e) => void sx.log("error", `${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", (e) => void sx.log("error", `promesse : ${e.reason && (e.reason.stack || e.reason)}`));

createRoot(document.getElementById("root")!).render(<App />);
