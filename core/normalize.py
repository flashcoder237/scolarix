"""Normalisation des matricules, noms et libellés.

Règle FMSP : accepter indifféremment XXMXXX, XX-M-XXX, XX/M/XXX, XX M XXX, etc.
"""
from __future__ import annotations

import re
import unicodedata

# Matricule FMSP : 2 chiffres (année d'entrée) + 1 à 4 lettres (filière) + 2 à 4 chiffres.
# Observés sur les vrais relevés : 19MM071 (Médecine), 19MP001 (Pharmacie), 11EM641 (ancienne
# codification Médecine), 23LSB010 (Licence Sciences biomédicales).
# Les séparateurs tolérés sont l'espace, le tiret, le slash et le point.
MATRICULE_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{2})\s*[-/. ]?\s*([A-Za-z]{1,4})\s*[-/. ]?\s*(\d{2,4})(?![A-Za-z0-9])"
)
_SEP_RE = re.compile(r"[\s\-/.]+")

FILIERES = {
    "M": "Médecine générale",
    "P": "Pharmacie",
    "B": "Sciences biomédicales",
    "MS": "Master",
    "D": "Doctorat",
}
# Codes lettres des matricules -> filière (repli quand le relevé n'indique pas la filière)
MATRICULE_FILIERE = {
    "M": "M", "MM": "M", "EM": "M", "MED": "M",
    "P": "P", "MP": "P", "EP": "P", "PH": "P",
    "B": "B", "LSB": "B", "SB": "B", "MB": "B",
    "MS": "MS",
}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
         "XI": 11, "XII": 12, "XIII": 13, "XIV": 14}


def roman_or_int(token: str | None) -> int | None:
    """« III » -> 3, « 2 » -> 2, sinon None."""
    if not token:
        return None
    t = token.strip().upper()
    if t.isdigit():
        return int(t)
    return ROMAN.get(t)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def norm_text(text: str | None) -> str:
    """Majuscules, sans accents ni apostrophes, espaces compactés."""
    if not text:
        return ""
    t = strip_accents(text).upper()
    t = t.replace("'", "").replace("’", "").replace("`", "")
    t = re.sub(r"[^A-Z0-9 \-]", " ", t)
    t = t.replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


def norm_matricule(text: str | None) -> str:
    """'19-m/042' -> '19M042'. Renvoie la saisie compactée même si ce n'est pas un matricule."""
    if not text:
        return ""
    return _SEP_RE.sub("", strip_accents(text)).upper()


def looks_like_matricule(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2}[A-Z]{1,4}\d{2,4}", norm_matricule(text)))


def find_matricules(text: str) -> list[str]:
    """Tous les matricules d'un texte, normalisés, dans l'ordre d'apparition (sans doublon)."""
    seen: list[str] = []
    for m in MATRICULE_RE.finditer(text or ""):
        letters = m.group(2).upper()
        # Évite les faux positifs du type « 12 de 2021 », « 20 h 30 »
        if letters in {"H", "DE", "LE", "LA", "ET", "AU", "EN", "DU", "LES", "DES", "AUX", "SUR", "UE"}:
            continue
        mat = f"{m.group(1)}{letters}{m.group(3)}"
        if mat not in seen:
            seen.append(mat)
    return seen


def filiere_code_from_matricule(mat: str) -> str:
    m = re.fullmatch(r"\d{2}([A-Z]{1,4})\d+", mat or "")
    return MATRICULE_FILIERE.get(m.group(1), "") if m else ""


def niveau_number(niveau: str | None) -> int:
    """'Niveau 3' -> 3, 'Master 2' -> 102, 'Doctorat' -> 200, inconnu -> 0 (tri chronologique)."""
    if not niveau:
        return 0
    n = norm_text(niveau)
    m = re.search(r"(\d+)", n)
    num = int(m.group(1)) if m else 0
    if n.startswith("MASTER") or n.startswith("M ") and num:
        return 100 + num
    if n.startswith("DOCTORAT"):
        return 200 + num
    return num


def year_start(annee: str | None) -> int:
    m = re.search(r"(\d{4})", annee or "")
    return int(m.group(1)) if m else 0
