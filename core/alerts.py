"""Alertes du contrôle avant impression : ce que l'agent doit savoir sur un relevé avant de le délivrer.
Partagées par l'interface Qt et l'interface Electron (moteur)."""
from __future__ import annotations

from pathlib import Path

from .database import Releve


def compute(rel: Releve, siblings: list[Releve], rules_active: bool) -> list[tuple[str, str]]:
    """[(message, ton)] — ton : red, amber, blue, green."""
    out = []
    ok_sib = [s for s in siblings if s.is_success]
    if not Path(rel.chemin).exists():
        out.append(("Fichier source introuvable (déplacé ou renommé) : lancez la vérification d'intégrité.", "red"))
    periode = rel.periode
    if not rel.is_success and ok_sib:
        s = ok_sib[0]
        out.append((f"Relevé NON VALIDÉ ({periode}, {rel.annee_academique}). Un relevé {s.statut} existe pour la "
                    f"même période ({s.annee_academique}, {s.pages_label.lower()}"
                    + (", rattrapage" if (s.session or "").lower().startswith("rattrap") else "")
                    + ") : vérifiez lequel délivrer.", "red"))
    elif siblings:
        autres = ", ".join(f"{s.annee_academique} {s.statut or '?'}" for s in siblings[:3])
        out.append((f"{len(siblings) + 1} relevés existent pour {periode} (autres : {autres}). "
                    f"Vous êtes sur celui de {rel.annee_academique}.", "amber"))
    elif rel.statut and not rel.is_success:
        cred = f" ({rel.credits}/30 crédits)" if rel.credits is not None else ""
        out.append((f"« {rel.decision or rel.statut} »{cred} : {periode} non validé. "
                    "Délivrance soumise à la scolarité.", "red"))
    elif not rel.statut:
        out.append(("Décision du jury non lue automatiquement : contrôlez-la sur l'aperçu.", "amber"))
    if rel.nb_pages > 1:
        pages = " et ".join(str(p) for p in range(rel.numero_page, rel.page_fin + 1))
        out.append((f"Ce relevé comporte {rel.nb_pages} pages ({pages}) : elles partent ensemble en recto-verso.", "amber"))
    if rel.score < 100:
        out.append((f"Résultat approché ({rel.score} %) : le nom indexé est « {rel.nom_complet} ». "
                    "Confirmez l'identité avec l'étudiant.", "amber"))
    if rel.type_fichier == "PDF_SCAN":
        conf = f" (confiance {rel.ocr_confiance:.0f} %)" if rel.ocr_confiance is not None else ""
        tone = "amber" if (rel.ocr_confiance or 100) < 70 else "blue"
        out.append((f"Texte issu de l'OCR{conf} : contrôlez le matricule sur l'image avant impression.", tone))
    if rel.matricule.startswith("ILLISIBLE"):
        out.append(("Matricule illisible sur ce relevé : identifiez l'étudiant sur l'aperçu. L'impression est "
                    "bloquée tant que le fichier n'est pas corrigé ou réindexé.", "red"))
    if (rel.statut or "") == "À VÉRIFIER":
        out.append(("Décision et crédits incohérents (ou décision ambiguë) : vérifiez la décision du jury sur "
                    "l'aperçu avant de délivrer.", "amber"))
    if rel.decision_deduite:
        out.append(("Ancien format (2012-2014) : aucune décision semestrielle n'est imprimée sur ce relevé ; "
                    "le statut est déduit de la colonne JURY des UE. Vérifiez sur l'aperçu.", "blue"))
    if rel.date_naissance and not rel.date_connue:
        out.append((f"Date de naissance fictive sur le relevé ({rel.date_naissance}) : à faire corriger "
                    "par la scolarité avant délivrance.", "amber"))
    if rel.is_word and rules_active:
        out.append(("En-tête corrigé à l'aperçu et à l'impression (règles de Réglages › Corrections). "
                    "Le fichier d'origine n'est pas modifié.", "blue"))
    if not rel.preview_pdf and not rel.is_word:
        out.append(("Aperçu indisponible : fichier source introuvable.", "blue"))
    if not out:
        out.append(("Document prêt — aucune anomalie détectée.", "green"))
    return out
