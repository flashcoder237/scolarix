"""Causes normalisées des anomalies d'indexation (écran Indexation › Anomalies).

Le robot écrit des messages en texte libre (« Page 4 : aucun matricule détecté. ») : on ne voyait pas que
3 000 lignes avaient la même cause et la même correction. Chaque message est rangé ici dans une CAUSE
(code court + libellé + correction usuelle), d'après les libellés réels de core/indexer.py, core/parsing.py,
core/extractors.py et de la vérification d'intégrité.

Le code est enregistré dans la colonne ``anomalies.code`` (ajoutée par migration, voir core/database.py) ;
les lignes écrites par une version antérieure (ou par l'application 1.3.1, qui partage la base) n'ont pas
de code : elles sont classées après coup (``Database.anomalies_backfill``).

Ce module ne dépend de rien d'autre (utilisable par le robot comme par le moteur).
"""
from __future__ import annotations

import os
import re

NIVEAUX = ("ERREUR", "AVERTISSEMENT", "INFO")


class Cause:
    __slots__ = ("code", "niveau", "label", "fix", "rx")

    def __init__(self, code: str, niveau: str, label: str, fix: str, rx: str | None):
        self.code, self.niveau, self.label, self.fix = code, niveau, label, fix
        self.rx = re.compile(rx, re.I) if rx else None

    def as_dict(self) -> dict:
        return {"code": self.code, "niveau": self.niveau, "label": self.label, "fix": self.fix}


# L'ordre compte : la première expression qui correspond l'emporte (les plus précises d'abord).
CAUSES: tuple[Cause, ...] = (
    # ---------------------------------------------------------------- erreurs (fichier non lu)
    Cause("mot_de_passe", "ERREUR", "Protégé par mot de passe",
          "Retirer la protection sur une copie, puis relire.", r"mot de passe"),
    Cause("word_absent", "ERREUR", "Word .doc : Word ou LibreOffice absent",
          "Installer LibreOffice ou activer la passerelle COM Word, puis relire.",
          r"Microsoft Word ou LibreOffice est n[ée]cessaire|ni LibreOffice|ni Microsoft Word|Word n'est pas install"),
    Cause("word_echec", "ERREUR", "Word / LibreOffice : conversion impossible",
          "Ouvrir le fichier dans Word pour vérifier qu'il n'est pas endommagé, puis relire.",
          r"Word ne peut pas|Word n'a produit|LibreOffice n'a pas pu|Conversion en .* impossible|Word ne r[ée]pond"),
    Cause("verrouille", "ERREUR", "Fichier verrouillé ou déplacé pendant la lecture",
          "Relire plus tard (fichier ouvert sur un autre poste ?).", r"verrouill|Lecture du fichier impossible"),
    Cause("robot", "ERREUR", "Erreur inattendue du robot",
          "Relire ce fichier ; si l'erreur persiste, transmettre le journal (logs).",
          r"Erreur inattendue|Processus de lecture interrompu"),
    Cause("endommage", "ERREUR", "Fichier endommagé ou illisible",
          "Ouvrir le fichier ; le remplacer par une copie saine.",
          r"endommag|illisible|corrompu|Lecture impossible|signature %PDF|Format non pris en charge|sans contenu Word"),
    Cause("erreur_autre", "ERREUR", "Autre erreur de lecture", "Ouvrir le fichier et vérifier.", None),
    # ---------------------------------------------------------------- avertissements (lu, à revoir)
    Cause("ocr_off", "AVERTISSEMENT", "PDF scanné non lu (OCR désactivé)",
          "Activer l'OCR (Tesseract), puis relire ces fichiers.", r"PDF scann[ée] non lu"),
    Cause("ocr_faible", "AVERTISSEMENT", "OCR : confiance faible (< 70 %)",
          "Revoir la page ; rescanner à 300 ppp si besoin.", r"confiance faible"),
    Cause("aucun_releve", "AVERTISSEMENT", "Aucun relevé détecté",
          "Souvent des PV, listes ou attestations : exclure le dossier ou le motif.", r"Aucun relev[ée] d[ée]tect"),
    Cause("collectif", "AVERTISSEMENT", "Document collectif ignoré (PV, liste)",
          "Normal : exclure le motif pour ne plus le lire.", r"document collectif"),
    Cause("matricule", "AVERTISSEMENT", "Matricule illisible ou absent",
          "Revoir la page ; relire avec le redressement (deskew).",
          r"aucun matricule|sans matricule|matricule illisible"),
    Cause("melange", "AVERTISSEMENT", "Deux relevés mélangés sur une page",
          "Contrôle à l'écran avant toute délivrance.", r"relev[ée]s m[ée]lang"),
    Cause("titre", "AVERTISSEMENT", "Titre « RELEVÉ DE NOTES » absent",
          "Attestation, PV ou autre document : exclure s'il ne s'agit pas d'un relevé.", r"titre .{0,3}RELEV"),
    Cause("decision", "AVERTISSEMENT", "Décision du jury à vérifier",
          "Contrôle humain avant délivrance.", r"d[ée]cision|jury"),
    Cause("annee", "AVERTISSEMENT", "Année académique incohérente ou absente", "Revoir la page.",
          r"ann[ée]e acad[ée]mique"),
    Cause("niveau", "AVERTISSEMENT", "Niveau non détecté ou incohérent", "Revoir la page.",
          r"niveau"),
    Cause("naissance", "AVERTISSEMENT", "Date de naissance factice ou illisible", "Revoir la page.",
          r"date de naissance"),
    Cause("nom", "AVERTISSEMENT", "Nom non détecté", "Revoir la page.", r"nom non d[ée]tect"),
    Cause("page_ignoree", "AVERTISSEMENT", "Page sans notes ou non rattachée ignorée", "Revoir le fichier.",
          r"page sans notes|non rattach"),
    Cause("orphelin", "AVERTISSEMENT", "Lien orphelin (déplacé, renommé ou supprimé)",
          "Relancer une passe différentielle sur le nouveau dossier.", r"Lien orphelin"),
    Cause("dossier_illisible", "AVERTISSEMENT", "Dossier illisible (réseau, droits)",
          "Vérifier l'accès réseau au dossier, puis relancer.", r"dossier\(s\) illisible"),
    Cause("docx_direct", "AVERTISSEMENT", "Préconversion .docx impossible (lecture directe)",
          "Vérifier Word / LibreOffice ; la recherche fonctionne quand même.", r"Lecture directe du \.docx"),
    Cause("autres", "AVERTISSEMENT", "Autres avertissements regroupés", "Revoir le fichier.",
          r"autres avertissements"),
    Cause("avert_autre", "AVERTISSEMENT", "Autre avertissement", "Revoir le fichier.", None),
    # ---------------------------------------------------------------- informations
    Cause("multipages", "INFO", "Relevés jumelés sur plusieurs pages", "Pour information.",
          r"plusieurs pages"),
    Cause("info_autre", "INFO", "Autre information", "Pour information.", None),
)

BY_CODE: dict[str, Cause] = {c.code: c for c in CAUSES}
_FALLBACK = {"ERREUR": "erreur_autre", "AVERTISSEMENT": "avert_autre", "INFO": "info_autre"}


def classify(niveau: str | None, message: str | None) -> str:
    """Code de cause d'un message d'anomalie (jamais vide)."""
    niv = (niveau or "").upper()
    if niv not in NIVEAUX:
        niv = "AVERTISSEMENT"
    text = message or ""
    for c in CAUSES:
        if c.niveau == niv and c.rx is not None and c.rx.search(text):
            return c.code
    return _FALLBACK[niv]


def label(code: str | None) -> str:
    c = BY_CODE.get(code or "")
    return c.label if c else (code or "Non classée")


def niveau_of(code: str | None) -> str:
    c = BY_CODE.get(code or "")
    return c.niveau if c else "AVERTISSEMENT"


def folder_of(chemin: str | None) -> str:
    """Dossier d'un fichier (chemin absolu du dossier parent) ; vide si inconnu."""
    if not chemin:
        return ""
    # chemins Windows lus sous un autre système (base copiée) : le séparateur « \\ » compte aussi
    cut = max(chemin.rfind("/"), chemin.rfind("\\"))
    return chemin[:cut] if cut > 0 else os.path.dirname(chemin)


_YEAR_RX = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-_./ ]\s*((?:19|20)?\d{2})(?!\d)")


def year_of(chemin: str | None) -> str | None:
    """Année académique lue dans les dossiers du chemin (« …\\2004-2005\\Pharmacie\\… » → « 2004-2005 »).
    La plus proche du fichier l'emporte ; None si aucune."""
    folder = folder_of(chemin)
    found = None
    for m in _YEAR_RX.finditer(folder):
        y1 = int(m.group(1))
        y2 = int(m.group(2)[-2:])
        y2 = (y1 // 100) * 100 + y2
        if y2 < y1:
            y2 += 100
        if y2 == y1 + 1:
            found = f"{y1}-{y2}"
    return found


def page_of(message: str | None) -> int | None:
    """Page citée par le message (« Page 4 : … », « 18M042 (page 3) : … ») : l'aperçu s'y place."""
    m = re.search(r"\bpages?\s+(\d{1,4})", message or "", re.I)
    return int(m.group(1)) if m else None


def catalogue() -> list[dict]:
    return [c.as_dict() for c in CAUSES]
