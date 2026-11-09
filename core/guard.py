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


class VisualCheckRequired(PrintBlocked):
    """Le texte d'une page n'est pas lisible par le contrôle automatique : au guichet, l'agent peut confirmer
    sur l'aperçu que c'est le bon étudiant (journalisé). Ailleurs (demandes, cursus) : bloqué."""

    def __init__(self, message: str, pages: list[int]):
        super().__init__(message)
        self.pages = pages


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


def check_before_print(rel: Releve, visually_checked: bool = False) -> None:
    """Lève PrintBlocked si l'impression de ce relevé n'est pas sûre. ``visually_checked`` : l'agent a confirmé
    sur l'aperçu les pages que le contrôle automatique ne sait pas lire (les autres restent contrôlées)."""
    from .extractors import describe_error, read_pages

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
    what = "aperçu Word" if rel.is_word else "PDF"
    try:
        n, texts, unreadable = read_pages(Path(pdf), pages)
    except PermissionError as exc:
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : le fichier {what} est verrouillé par un autre "
                           f"programme (antivirus, Word, sauvegarde). Réessayez dans un instant. "
                           f"[{describe_error(exc)} · {pdf}]") from exc
    except Exception as exc:
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : {what} illisible par le contrôle "
                           f"automatique. [{describe_error(exc)} · {pdf}]") from exc
    if rel.numero_page < 1 or rel.page_fin > n:
        raise PrintBlocked(f"Impression bloquée pour {rel.matricule} : pages {rel.numero_page}–{rel.page_fin} "
                           f"absentes du document ({n} pages). Relancer l'indexation.")
    if rel.type_fichier == "PDF_SCAN":
        return
    if unreadable and not visually_checked:
        nums = sorted(unreadable)
        raise VisualCheckRequired(
            f"Le contrôle automatique ne peut pas lire le texte de la page {', '.join(map(str, nums))} de ce {what} "
            f"[{unreadable[nums[0]]}]. Vérifiez sur l'aperçu que c'est bien {rel.nom_complet or '—'} "
            f"(matricule {rel.matricule}).", nums)
    mat = norm_matricule(rel.matricule)
    first = texts.get(rel.numero_page, "")
    if rel.numero_page in unreadable:
        first = mat           # page confirmée à l'œil par l'agent
    if mat not in _compact(first):
        raise PrintBlocked(f"Impression bloquée : le matricule {rel.matricule} n'apparaît pas sur la page "
                           f"{rel.numero_page}. Le fichier a peut-être changé : relancer l'indexation.")
    for num in pages[1:]:
        if num in unreadable:
            continue          # confirmée à l'œil par l'agent
        text = texts.get(num, "")
        others = {norm_matricule(m) for m in _LABELED.findall(text)} - {mat}
        if others or (_HEADER.search(text) and mat not in _compact(text)):
            raise PrintBlocked(f"Impression bloquée : la page {num} semble appartenir à un autre étudiant "
                               f"({', '.join(sorted(others)) or 'en-tête sans matricule lisible'}).")
