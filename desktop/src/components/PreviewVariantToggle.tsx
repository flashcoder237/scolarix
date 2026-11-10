import { useEffect, useState } from "react";
import { call } from "../api";
import { Seg } from "./ui";

// Bascule « Aperçu : corrigé / original », autonome (l'écran Guichet n'a qu'à la monter) :
// le document d'origine, SANS aucune correction de texte, est rendu à la demande par le moteur
// (preview_original) dans un cache séparé. Consultation seulement : l'impression et l'export
// utilisent toujours l'aperçu normal (corrigé, ou d'origine si les corrections ont été écartées).

export type PreviewVariant = "corrige" | "original";

export interface OriginalPreview { file: string; first: number; last: number }

/** Sélecteur. ``available`` : faux quand l'aperçu affiché EST déjà le document d'origine (rien n'a été corrigé). */
export function PreviewVariantToggle({ releveId, value, onChange, available = true }: {
  releveId: number | null | undefined; value: PreviewVariant; onChange: (v: PreviewVariant) => void; available?: boolean;
}) {
  if (!releveId || !available) return null;
  return (
    <span title="« Original » : le document tel qu'il est archivé, sans les corrections d'en-tête (consultation seulement ; l'impression reste corrigée)"
      data-testid="apercu-variant">
      <Seg<PreviewVariant> value={value} onChange={onChange} label="Aperçu"
        options={[["corrige", "Corrigé"], ["original", "Original"]]} />
    </span>
  );
}

/** Rendu d'origine du relevé quand ``variant`` vaut « original » : {original, message} pour PdfView. */
export function useOriginalPreview(releveId: number | null | undefined, variant: PreviewVariant):
  { original: OriginalPreview | null; message: string | null } {
  const [orig, setOrig] = useState<{ id: number; doc: OriginalPreview } | null>(null);
  const [state, setState] = useState("");
  useEffect(() => {
    if (variant !== "original" || !releveId || orig?.id === releveId) return;
    let live = true;
    setState("en cours");
    call<{ preview_pdf: string; numero_page: number; page_fin: number }>("preview_original", { releve_id: releveId })
      .then((r) => { if (live) { setOrig({ id: releveId, doc: { file: r.preview_pdf, first: r.numero_page, last: r.page_fin } }); setState(""); } })
      .catch((e: unknown) => { if (live) setState(`Aperçu d'origine impossible : ${e instanceof Error ? e.message : String(e)}`); });
    return () => { live = false; };
  }, [variant, releveId, orig]);
  if (variant !== "original" || !releveId) return { original: null, message: null };
  const ready = orig?.id === releveId ? orig.doc : null;
  return { original: ready, message: ready ? null : state === "en cours" || !state ? "Rendu du document d'origine…" : state };
}
