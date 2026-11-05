"""Découpage des pages en blocs « étudiant » et extraction des métadonnées d'un relevé.

Le format exact des relevés a varié au fil des années : toutes les expressions sont regroupées
dans ``PATTERNS`` pour être ajustées facilement sur un échantillon réel d'archives.

Règle des relevés sur plusieurs pages (cas S4.2) : une page qui porte le même matricule que le
bloc précédent, ou qui ne contient aucun nouvel en-tête d'étudiant (suite du tableau de notes),
est rattachée au bloc précédent -> page_debut=34, page_fin=35.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extractors import PageText
from .normalize import (FILIERES, MATRICULE_RE, filiere_code_from_matricule, find_matricules,
                        niveau_number, norm_text, strip_accents)

SEP = r"\s*[:：.]?\s*"

PATTERNS = {
    "matricule_label": re.compile(r"matricule\s*(?:n[°o]\.?|no\.?)?" + SEP, re.I),
    "nom_complet": re.compile(r"(?:noms?\s*(?:et|&)\s*pr[ée]noms?|nom\s*complet|[ée]tudiant\(?e?\)?)" + SEP
                              + r"([^\n]+)", re.I),
    "nom": re.compile(r"(?<![a-zé])noms?(?!\s*(?:et|&))" + SEP + r"([^\n]+)", re.I),
    "prenom": re.compile(r"pr[ée]noms?(?:\(s\))?" + SEP + r"([^\n]+)", re.I),
    "naissance": re.compile(r"n[ée]\(?e?\)?\s*le" + SEP + r"(\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4})", re.I),
    "filiere": re.compile(r"(?:fili[eè]re|parcours|option|sp[ée]cialit[ée])" + SEP + r"([^\n]+)", re.I),
    "niveau": re.compile(r"(?:niveau|level|ann[ée]e\s*d['’]?[ée]tudes?)" + SEP + r"(\d{1,2})", re.I),
    "niveau_ordinal": re.compile(r"\b(\d)\s*(?:e|è|ème|eme|i[eè]me)\s*ann[ée]e\b", re.I),
    "master": re.compile(r"\bmaster\s*(\d)\b|\bM\s?([12])\b", re.I),
    "annee": re.compile(r"\b((?:19|20)\d{2})\s*[-/–]\s*((?:19|20)\d{2})\b"),
    "session": re.compile(r"session\s*(?:de\s*)?(normale|ordinaire|rattrapage|de\s*rattrapage|sp[ée]ciale)", re.I),
    "decision": re.compile(r"d[ée]cision(?:\s*du\s*jury)?" + SEP + r"([A-Za-zÀ-ÿ ]{4,40})", re.I),
    "moyenne": re.compile(r"moyenne[^:\n]{0,30}[:：]\s*(\d{1,2}[,.]\d{1,3})", re.I),
    "entete": re.compile(r"relev[ée]\s*de\s*notes|bulletin\s*de\s*notes|transcript", re.I),
}

STATUTS = [  # ordre de priorité : les formes négatives d'abord
    ("NON ADMIS", "AJOURNÉ"), ("AJOURN", "AJOURNÉ"), ("EXCLU", "EXCLUSION"), ("ABANDON", "ABANDON"),
    ("REPORT", "REPORT"), ("REDOUBL", "REDOUBLEMENT"), ("SUSPEN", "SUSPENSION"),
    ("ADMIS", "ADMIS"), ("ADMISE", "ADMIS"),
]

FILIERE_KEYWORDS = [
    ("PHARMAC", "P"), ("BIOMED", "B"), ("SCIENCES BIOMED", "B"), ("MASTER", "MS"),
    ("DOCTORAT", "D"), ("MEDECINE", "M"), ("MEDICAL", "M"),
]


@dataclass
class Block:
    matricule: str
    matricule_brut: str
    page_debut: int
    page_fin: int
    texts: list[str] = field(default_factory=list)
    ocr: list[float] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


@dataclass
class PathHints:
    annee: str | None = None
    filiere_code: str | None = None
    session: str | None = None
    niveau: str | None = None


def hints_from_path(path: str) -> PathHints:
    """Indices tirés de l'arborescence : « 2020-2021\\Medecine\\N3_SR_Releves.doc »."""
    h = PathHints()
    p = strip_accents(path).replace("\\", "/")
    m = PATTERNS["annee"].search(p)
    if m:
        h.annee = f"{m.group(1)}-{m.group(2)}"
    up = p.upper()
    for kw, code in FILIERE_KEYWORDS:
        if kw in up:
            h.filiere_code = code
            break
    name = up.rsplit("/", 1)[-1]
    if re.search(r"(^|[_\- ])(SR|RATTRAP)", name):
        h.session = "Rattrapage"
    elif re.search(r"(^|[_\- ])(SN|NORMAL|ORDINAIRE)", name):
        h.session = "Normale"
    m = re.search(r"(?:^|[_\- ])(?:NIV(?:EAU)?|[NPB])\s*_?(\d)(?=[_\- .]|$)", name)
    if m:
        h.niveau = f"Niveau {m.group(1)}"
    m = re.search(r"(?:^|[_\- ])M(?:ASTER)?\s*_?([12])(?=[_\- .]|$)", name)
    if m and "MASTER" in up:
        h.niveau = f"Master {m.group(1)}"
    return h


def _labeled_matricules(text: str) -> list[tuple[str, str, int]]:
    """Matricules précédés du libellé « Matricule » : (normalisé, brut, position)."""
    out = []
    for lab in PATTERNS["matricule_label"].finditer(text):
        m = MATRICULE_RE.match(text, lab.end())
        if m:
            mat = f"{m.group(1)}{m.group(2).upper()}{m.group(3)}"
            if not any(o[0] == mat for o in out):
                out.append((mat, m.group(0), lab.start()))
    return out


def _header_matricule(text: str) -> tuple[str, str] | None:
    """À défaut de libellé : premier matricule dans les 15 premières lignes de la page."""
    head = "\n".join(text.splitlines()[:15])
    mats = find_matricules(head)
    if not mats:
        return None
    m = MATRICULE_RE.search(head)
    return mats[0], (m.group(0) if m else mats[0])


def split_blocks(pages: list[PageText]) -> tuple[list[Block], list[str]]:
    """Regroupe les pages en blocs étudiants. Renvoie (blocs, avertissements)."""
    blocks: list[Block] = []
    warnings: list[str] = []
    current: Block | None = None
    any_label = any(_labeled_matricules(p.text) for p in pages)

    for page in pages:
        text = page.text or ""
        if not text.strip():
            continue
        labeled = _labeled_matricules(text) if any_label else []
        if not labeled and not any_label:
            hm = _header_matricule(text)
            labeled = [(hm[0], hm[1], 0)] if hm else []

        if not labeled:
            # Suite du tableau de notes sans nouvel en-tête : rattachée au relevé précédent
            if current is not None:
                current.page_fin = page.number
                current.texts.append(text)
                if page.ocr_confidence is not None:
                    current.ocr.append(page.ocr_confidence)
            else:
                warnings.append(f"Page {page.number} : aucun matricule détecté.")
            continue

        if len(labeled) == 1 and current is not None and labeled[0][0] == current.matricule \
                and page.number == current.page_fin + 1:
            # Même matricule sur la page suivante : relevé sur plusieurs pages
            current.page_fin = page.number
            current.texts.append(text)
            if page.ocr_confidence is not None:
                current.ocr.append(page.ocr_confidence)
            continue

        # Un ou plusieurs nouveaux étudiants sur cette page
        for i, (mat, brut, pos) in enumerate(labeled):
            end = labeled[i + 1][2] if i + 1 < len(labeled) else len(text)
            start = 0 if i == 0 else pos
            chunk = text[start:end]
            current = Block(mat, brut, page.number, page.number, [chunk])
            if page.ocr_confidence is not None:
                current.ocr.append(page.ocr_confidence)
            blocks.append(current)
    return blocks, warnings


# ------------------------------------------------------------ champs du bloc
def _clean(value: str) -> str:
    value = re.split(r"\s{3,}|\t|\s(?:matricule|n[ée]e?\s*le|fili[eè]re|niveau|session|ann[ée]e)\b",
                     value, maxsplit=1, flags=re.I)[0]
    return value.strip(" :;,-– ")


def split_nom_prenom(full: str) -> tuple[str, str]:
    """« TCHOUA NGUEMO Brice » -> (« TCHOUA NGUEMO », « Brice »). Les mots en capitales forment le nom."""
    words = full.split()
    nom, prenom = [], []
    for w in words:
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", w)
        if not prenom and letters and letters.upper() == letters and len(letters) >= 1:
            nom.append(w)
        else:
            prenom.append(w)
    if not nom:  # tout en casse mixte : premier mot = nom
        return (words[0].upper(), " ".join(words[1:])) if words else ("", "")
    return " ".join(nom), " ".join(prenom)


def extract_name(text: str, matricule_brut: str) -> tuple[str, str, str]:
    m = PATTERNS["nom_complet"].search(text)
    if m:
        full = _clean(m.group(1))
        if full and not MATRICULE_RE.fullmatch(full):
            nom, prenom = split_nom_prenom(full)
            return nom, prenom, f"{nom} {prenom}".strip()
    nom_m = PATTERNS["nom"].search(text)
    pre_m = PATTERNS["prenom"].search(text)
    if nom_m:
        nom = _clean(nom_m.group(1))
        prenom = _clean(pre_m.group(1)) if pre_m else ""
        if nom and pre_m and prenom and nom.endswith(prenom):
            nom = nom[: -len(prenom)].strip()
        if nom:
            return nom.upper(), prenom, f"{nom.upper()} {prenom}".strip()
    # Dernier recours : la ligne du matricule, débarrassée des libellés
    for line in text.splitlines():
        if matricule_brut and matricule_brut in line:
            rest = line.replace(matricule_brut, " ")
            rest = re.sub(r"(?i)matricule\s*(?:n[°o])?\s*[:：]?", " ", rest)
            rest = re.sub(r"\s+", " ", rest).strip(" :;-")
            if len(re.sub(r"[^A-Za-z]", "", rest)) >= 3:
                nom, prenom = split_nom_prenom(rest)
                return nom, prenom, f"{nom} {prenom}".strip()
    return "", "", ""


def detect_statut(text: str) -> str | None:
    m = PATTERNS["decision"].search(text)
    zone = norm_text(m.group(1)) if m else ""
    if not zone:
        # Pas de libellé « Décision » : on regarde la fin du relevé (là où figure la décision du jury)
        zone = norm_text(text[-600:])
    zone = re.sub(r"\bNON VALIDEE?S?\b|\bVALIDEE?S?\b", " ", zone)
    for key, label in STATUTS:
        if re.search(r"\b" + key, zone):
            return label
    return None


def detect_filiere(text: str, matricule: str, hints: PathHints) -> tuple[str, str]:
    m = PATTERNS["filiere"].search(text)
    if m:
        val = norm_text(m.group(1))
        for kw, code in FILIERE_KEYWORDS:
            if kw in val:
                return code, FILIERES.get(code, _clean(m.group(1)))
    head = norm_text(text[:800])
    if "MASTER" in head:
        return "MS", FILIERES["MS"]
    code = filiere_code_from_matricule(matricule) or hints.filiere_code or ""
    if code == "MS" or code not in FILIERES:
        code = hints.filiere_code or code
    return code, FILIERES.get(code, code or "")


def detect_niveau(text: str, hints: PathHints) -> str | None:
    m = PATTERNS["niveau"].search(text)
    if m:
        return f"Niveau {int(m.group(1))}"
    m = PATTERNS["niveau_ordinal"].search(text)
    if m:
        return f"Niveau {int(m.group(1))}"
    m = PATTERNS["master"].search(text[:1200])
    if m:
        return f"Master {m.group(1) or m.group(2)}"
    return hints.niveau


def detect_session(text: str, hints: PathHints) -> str | None:
    m = PATTERNS["session"].search(text)
    if m:
        v = norm_text(m.group(1))
        if "RATTRAP" in v:
            return "Rattrapage"
        if "SPECIAL" in v:
            return "Spéciale"
        return "Normale"
    return hints.session or "Normale"


def parse_block(block: Block, hints: PathHints) -> tuple[dict, list[str]]:
    text = block.text
    warnings: list[str] = []
    nom, prenom, full = extract_name(text, block.matricule_brut)
    if not full:
        full = "(nom non détecté)"
        warnings.append(f"{block.matricule} (page {block.page_debut}) : nom non détecté.")
    code, filiere = detect_filiere(text, block.matricule, hints)
    niveau = detect_niveau(text, hints)
    annee_m = PATTERNS["annee"].search(text)
    annee = f"{annee_m.group(1)}-{annee_m.group(2)}" if annee_m else hints.annee
    moy_m = PATTERNS["moyenne"].search(text)
    nais_m = PATTERNS["naissance"].search(text)
    statut = detect_statut(text)
    if not annee:
        warnings.append(f"{block.matricule} (page {block.page_debut}) : année académique non détectée.")
    excerpt = re.sub(r"\s+", " ", text)[:400]
    return {
        "matricule": block.matricule,
        "matricule_brut": block.matricule_brut.strip(),
        "nom": nom,
        "prenom": prenom,
        "nom_complet": full,
        "date_naissance": re.sub(r"\s", "", nais_m.group(1)) if nais_m else None,
        "filiere": filiere,
        "filiere_code": code,
        "niveau": niveau,
        "niveau_num": niveau_number(niveau),
        "annee_academique": annee,
        "session": detect_session(text, hints),
        "statut": statut,
        "moyenne": moy_m.group(1).replace(".", ",") if moy_m else None,
        "numero_page": block.page_debut,
        "page_fin": block.page_fin,
        "ocr_confiance": round(sum(block.ocr) / len(block.ocr), 1) if block.ocr else None,
        "extrait": excerpt,
    }, warnings


def parse_pages(pages: list[PageText], source_path: str) -> tuple[list[dict], list[str]]:
    hints = hints_from_path(source_path)
    blocks, warnings = split_blocks(pages)
    releves = []
    for b in blocks:
        rel, w = parse_block(b, hints)
        releves.append(rel)
        warnings.extend(w)
    return releves, warnings
