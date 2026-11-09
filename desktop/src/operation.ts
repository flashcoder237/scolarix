// Opérations longues du moteur (fusion du cursus, préparation d'une demande) : progression et annulation.
//
// L'interface choisit un identifiant d'opération (« op »), le passe à l'appel, puis reçoit les événements
// {"event": "progress", "data": {op, phase, i, n, message}} du moteur (« Conversion 3/7 : EM4 S1.docx… »).
// « Annuler » envoie call("cancel", {op}) : le moteur s'arrête avant le fichier Word suivant.
import { useCallback, useEffect, useRef, useState } from "react";
import { call, sx } from "./api";

export interface Progress { op: string; phase: "conversion" | "fusion" | string; i: number; n: number; message: string }

export function newOp(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function useOperation() {
  const opRef = useRef<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [cancelling, setCancelling] = useState(false);

  // abonnement permanent, filtré par l'opération en cours : aucun événement perdu entre l'appel et le rendu
  useEffect(() => sx.onEngineEvent((msg) => {
    if (msg.event === "progress" && msg.data && msg.data.op === opRef.current) setProgress(msg.data as Progress);
  }), []);

  const start = useCallback((prefix: string) => {
    const op = newOp(prefix);
    opRef.current = op;
    setProgress(null);
    setCancelling(false);
    return op;
  }, []);
  const finish = useCallback(() => { opRef.current = null; setProgress(null); setCancelling(false); }, []);
  const cancel = useCallback(async () => {
    const op = opRef.current;
    if (!op) return;
    setCancelling(true);
    await call("cancel", { op }).catch(() => undefined);
  }, []);
  return { progress, cancelling, start, finish, cancel };
}

/** Une annulation demandée par l'agent n'est pas une erreur : message neutre. */
export const isCancelled = (e: unknown) => /annulée/i.test(e instanceof Error ? e.message : String(e));
