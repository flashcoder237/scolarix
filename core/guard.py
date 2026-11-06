"""Contrôle de dernière ligne avant toute impression, tout export et toute fusion.

Une erreur ici imprimerait le relevé d'un autre étudiant : c'est la règle la plus importante du guichet.
Juste avant d'envoyer des pages, on vérifie :

1. que le fichier source existe encore et n'a pas changé depuis l'indexation (taille + date) ;
2. que les pages demandées existent dans le PDF à imprimer ;
3. (PDF texte et Word) que le matricule de l'étudiant figure sur la première page, et qu'aucune page
   de suite ne porte l'en-tête ou le matricule d'un autre étudiant ;
4. (Word) que la page a bien été confirmée sur le rendu de Word.

Un scan (PDF_SCAN) n'a pas de couche texte fiable : seuls les points 1 et 2 s'appliquent, et l'interface
rappelle de contrôler le matricule sur l'image.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .database import Releve
from .normalize import norm_matricule


class PrintBlocked(Exception):
    """Impression refusée : le message explique pourquoi et quoi faire."""


_LABELED = re.compile(r"MATRICULE\s*(?:N[°O]\.?)?\s*[:：]?\s*(\d{2}\s*[-/. ]?\s*[A-Z]{1,4}\s*[-/. ]?\s*\d{2,4})", re.I)
_HEADER = re.compile(r"NOMS?\s*(?:\(S\))?\s*(?:ET|&)\s*PR[ÉE]NOMS?", re.I)


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def source_unchanged(rel: Releve) -> str | None:
    try:
        st = os.stat(rel.chemin)
    except OSError:
        return "fichier source introuvable (déplacé, renommé ou supprimé)"
    if rel.fichier_statut == "ERREUR":
        return "la dernière lecture du fichier a échoué : relancer l'indexation différentielle"
    if rel.fichier_taille is not None and (st.st_size != rel.fichier_taille
                                           or abs(st.st_mtime - (rel.fichier_mtime or 0)) >= 1):
        return "fichier modifié depuis l'indexation : relancer l'indexation différentielle"
    return None


def check_before_print(rel: Releve) -> None:
    """Lève PrintBlocked si l'impression de ce relevé n'est pas sûre."""
    from .extractors import page_count_and_text

    why = source_unchanged(rel)
    if why:
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : {why}.")
    pdf = rel.preview_pdf
    if not pdf or not Path(pdf).exists():
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : aperçu PDF indisponible.")
    if rel.is_word and not rel.page_verifiee:
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : la page n'a pas été confirmée sur le rendu "
                           "de Word. Ouvrir le fichier d'origine.")
    pages = list(range(rel.numero_page, rel.page_fin + 1))
    try:
        n, texts = page_count_and_text(Path(pdf), pages)
    except Exception as exc:
        raise PrintBlocked(f"Impression bloquée : PDF illisible ({exc.__class__.__name__}).") from exc
    if rel.numero_page < 1 or rel.page_fin > n:
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : pages {rel.numero_page}–{rel.page_fin} "
                           f"absentes du document ({n} pages). Relancer l'indexation.")
    if rel.type_fichier == "PDF_SCAN":
        return
    mat = norm_matricule(rel.matricule)
    first = texts.get(rel.numero_page, "")
    if mat not in _compact(first):
        raise PrintBlocked(f"Impression bloquée : le matricule {rel.matricule} n'apparaît pas sur la page "
                           f"{rel.numero_page}. Le fichier a peut-être changé : relancer l'indexation.")
    for num in pages[1:]:
        text = texts.get(num, "")
        others = {norm_matricule(m) for m in _LABELED.findall(text)} - {mat}
        if others or (_HEADER.search(text) and mat not in _compact(text)):
            raise PrintBlocked(f"Impression bloquée : la page {num} semble appartenir à un autre étudiant "
                               f"({', '.join(sorted(others)) or 'en-tête sans matricule lisible'}).")
