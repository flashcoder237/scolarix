// Étapes d'une demande (Préparer → Imprimer → Remettre) avec l'heure de chaque étape franchie :
// barre de la revue et bandeau de la page Demandes. Remplace badge + étapes + phrase « Étape suivante ».

export interface Step { label: string; sub?: string | null }

/** « 2026-11-10 09:20:11 » → « 09:20 » aujourd'hui, « hier 15:30 », sinon « 08/11 09:20 ». */
export function hourLabel(ts: string | null | undefined, now = new Date()): string {
  if (!ts) return "";
  const d = new Date(ts.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return ts.slice(11, 16);
  const hm = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return hm;
  if (diff === 1) return `hier ${hm}`;
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${hm}`;
}

/** Âge d'une étape : « attend depuis 2 j », « depuis 3 h ». */
export function ageLabel(ts: string | null | undefined, now = new Date()): { text: string; days: number } | null {
  if (!ts) return null;
  const d = new Date(ts.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return null;
  const h = (now.getTime() - d.getTime()) / 3600000;
  if (h < 1) return { text: "depuis moins d'1 h", days: 0 };
  if (h < 24) return { text: `depuis ${Math.floor(h)} h`, days: 0 };
  const days = Math.floor(h / 24);
  return { text: `depuis ${days} j`, days };
}

export function Stepper({ steps, current, compact = false, testid = "stepper" }: { steps: Step[]; current: number; compact?: boolean; testid?: string }) {
  return (
    <div className={`stepper${compact ? " compact" : ""}`} data-testid={testid} aria-label="Étapes de la demande">
      {steps.map((s, i) => {
        const st = current < 0 ? "" : i < current ? "done" : i === current ? "cur" : "";
        return (
          <span key={s.label} className="st-wrap">
            {i > 0 && <span className={`st-sep${i <= current ? " done" : ""}`} />}
            <span className={`st ${st}`} aria-current={st === "cur" ? "step" : undefined}>
              <span className="n">{st === "done" ? "✓" : i + 1}</span>
              <span className="tx"><span className="l">{s.label}</span>{s.sub && <span className="sub">{s.sub}</span>}</span>
            </span>
          </span>
        );
      })}
    </div>
  );
}
