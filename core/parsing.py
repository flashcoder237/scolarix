"""Découpage des pages en blocs « étudiant » et extraction des métadonnées d'un relevé.

Calé sur les VRAIS relevés de la FMSP (échantillon 2012-2025) :

* un relevé = un étudiant × un SEMESTRE (LMD) : « NIVEAU: II  SEMESTRE: IV » (chiffres romains ou arabes) ;
* format récent : ligne récapitulative sous l'en-tête
  « RELEVE NIVEAU | SEMESTRE | TOTAL CREDIT /30 | MOYENNE SEMESTRIELLE /20 | MGP | GRADE | DECISION DU JURY »
  -> « 1 1 30 10,99 2,0 C SEMESTRE VALIDE » ;
* ancien format (Word 2012-2014) : pas de décision semestrielle, une colonne JURY par UE
  (VALIDEE / NON VALIDEE) ;
* l'extraction PDF coupe parfois les libellés (« N OM ET PRENOM », « N IVEAU », « N É(E) LE ») :
  chaque libellé tolère un espace entre ses lettres ;
* dates parfois écrites avec des barres inverses (« 02\\01\\1997 »).

Toutes les expressions sont regroupées dans ``PATTERNS`` pour être ajustées sur d'autres variantes.

Règle des relevés sur plusieurs pages (cas S4.2) : une page SANS nouvel en-tête d'étudiant (pas de
libellé « NOM ET PRENOM ») est la suite du relevé précédent. Une page avec un en-tête complet est
toujours un nouveau relevé, même si le matricule est identique (doublon, redoublement).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extractors import PageText
from .normalize import (FILIERES, MATRICULE_RE, filiere_code_from_matricule, find_matricules, niveau_number,
                        norm_text, roman_or_int, strip_accents)


def lab(word: str) -> str:
    """Libellé tolérant aux espaces parasites entre les lettres : NOM -> N\\s?O\\s?M."""
    parts = []
    for ch in word:
        if ch == " ":
            parts.append(None)
        elif ch.upper() in "EÉÈ":
            parts.append("[EÉÈ]")
        else:
            parts.append(re.escape(ch))
    out = ""
    for i, part in enumerate(parts):
        if part is None:
            out += r"\s+"
            continue
        if i and parts[i - 1] is not None:
            out += r"\s?"
        out += part
    return out


SEP = r"\s*[:：.]?\s*"
ROMAN_OR_INT = r"([IVX]{1,5}|\d{1,2})\b"
DATE = r"(\d{1,2}\s*[\\/.-]\s*\d{1,2}\s*[\\/.-]\s*\d{2,4})"

PATTERNS = {
    "matricule_label": re.compile(lab("MATRICULE") + r"\s*(?:n[°o]\.?|no\.?)?" + SEP, re.I),
    # « NOM ET PRENOM: », « N OM ET PRENOM: », « NOM(S) ET PRENOM(S) : », « Noms et Prénoms : »
    "nom_complet": re.compile(lab("NOM") + r"(?:\(S\)|S)?\s*(?:ET|&)\s*" + lab("PRENOM") + r"(?:\(S\)|S)?"
                              + SEP + r"([^\n]*)", re.I),
    "nom_seul": re.compile(r"(?<![A-Za-zÀ-ÿ])" + lab("NOM") + r"(?:\(S\)|S)?(?!\s*(?:ET|&|\(|S\b))" + SEP
                           + r"([^\n]+)", re.I),
    "prenom": re.compile(lab("PRENOM") + r"(?:\(S\)|S)?" + SEP + r"([^\n]+)", re.I),
    "naissance": re.compile(r"N\s?[ÉE]\s?\(?\s?E?\s?\)?\s*LE" + SEP + DATE, re.I),
    "lieu": re.compile(r"(?:^|\s)(?:A|À)\s*:\s*([A-ZÀ-ÿ][A-ZÀ-ÿ' \-]{1,40})", re.M),
    "filiere": re.compile(lab("FILIERE") + r"\s*:\s*([^\n]+)", re.I),
    "filiere_lache": re.compile(r"(?:fili[eè]re|parcours|option|sp[ée]cialit[ée])" + SEP + r"([^\n]+)", re.I),
    "cycle": re.compile(lab("CYCLE") + r"\s*:\s*([A-Za-zÀ-ÿ]+)", re.I),
    "niveau": re.compile(lab("NIVEAU") + r"\s*:\s*" + ROMAN_OR_INT, re.I),
    "niveau_lache": re.compile(r"(?:niveau|level)\s*[:：]?\s*(\d{1,2})\b", re.I),
    "niveau_ordinal": re.compile(r"\b(\d)\s*(?:e|è|ème|eme|i[eè]me)\s*ann[ée]e\b", re.I),
    "semestre": re.compile(lab("SEMESTRE") + r"\s*:\s*" + ROMAN_OR_INT, re.I),
    "master": re.compile(r"\bmaster\s*(\d)\b", re.I),
    "annee_label": re.compile(r"ANN[ÉE]E\s*ACAD[ÉE]MIQUE\s*:?\s*((?:19|20)\d{2})\s*[-/–]\s*((?:19|20)\d{2})", re.I),
    "annee": re.compile(r"\b((?:19|20)\d{2})\s*[-/–]\s*((?:19|20)\d{2})\b"),
    "session": re.compile(r"session\s*(?:de\s*)?(normale|ordinaire|rattrapage|de\s*rattrapage|sp[ée]ciale)", re.I),
    # Ligne récapitulative : niveau [semestre] crédits moyenne [MGP] grade [MGP] décision
    #   semestriel : « 1 1 30 10,99 2,0 C SEMESTRE VALIDE »
    #   annuel     : « 7 60 19,00 A+ 4,0 SEMESTRES VALIDES » ou « 7 60 18.40 4.0 A+ SEMESTRES VALIDES »
    # La case « total crédits » est parfois vide sur le relevé : elle est donc facultative.
    "recap": re.compile(r"^\s*(?P<niv>\d{1,2})\s+(?:(?P<sem>\d{1,2})\s+)?(?:(?P<cred>\d{1,3})\s+)?"
                        r"(?P<moy>\d{1,2}[,.]\d{1,3})\s+(?:(?P<mgp1>\d[,.]\d{1,2})\s+)?(?P<grade>[A-F]\s?[+-]?)\s+"
                        r"(?:(?P<mgp2>\d[,.]\d{1,2})\s+)?(?P<dec>[A-ZÀ-ÿ][A-ZÀ-ÿ '\-]{2,60}?)\s*$", re.M),
    "annuel": re.compile(r"ANNUEL|MOYENNE\s+ANNUELLE|TOTAL\s+CREDIT\s*/\s*60|SEMESTRES\s+VALID", re.I),
    "option": re.compile(lab("OPTION") + r"\s*:\s*([A-ZÀ-ÿ][A-ZÀ-ÿ '\-]{1,40})", re.I),
    "decision": re.compile(r"d[ée]cision(?:\s*du\s*jury)?\s*[:：]\s*([A-Za-zÀ-ÿ ]{4,40})", re.I),
    "moyenne": re.compile(r"moyenne[^:\n]{0,30}[:：]\s*(\d{1,2}[,.]\d{1,3})", re.I),
    # Colonne JURY de l'ancien format : « … GRADE CRÉDIT VALIDEE » (le texte d'une cellule voisine peut suivre)
    "ue_jury": re.compile(r"(?:\b[A-F]\s?[+-]?|\b)\s+\d{1,2}\s+(NON\s+VALID[EÉ]E?S?|VALID[EÉ]E?S?)\b(?!\s+(?:VALID|SEMESTRE))"
                          r"|(?<!SEMESTRE\s)(?<!SEMESTRES\s)\b(NON\s+VALID[EÉ]E?S?|VALID[EÉ]E?S?)\s*$", re.M | re.I),
    "entete": re.compile(r"relev[ée]\s*de\s*notes|bulletin\s*de\s*notes|transcript", re.I),
}

STATUTS = [  # ordre de priorité : les formes négatives d'abord
    ("NON VALID", "NON VALIDÉ"), ("NON ADMIS", "AJOURNÉ"), ("AJOURN", "AJOURNÉ"), ("EXCLU", "EXCLUSION"),
    ("ABANDON", "ABANDON"), ("REPORT", "REPORT"), ("REDOUBL", "REDOUBLEMENT"), ("SUSPEN", "SUSPENSION"),
    ("VALID", "VALIDÉ"), ("ADMIS", "ADMIS"),
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
    semestre: int | None = None


def hints_from_path(path: str) -> PathHints:
    """Indices tirés de l'arborescence : « 2020-2021\\Medecine\\N3_SR_Releves.doc », « EM2_S2.docx »."""
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
    elif re.search(r"(^|[_\- ])(SN|NORMAL|ORDINAIRE)(?=[_\- .]|$)", name):
        h.session = "Normale"
    m = re.search(r"(?:^|[_\- ])(?:NIV(?:EAU)?|[NPB]|E[MP])\s*_?(\d)(?=[_\- .]|$)", name)
    if m:
        h.niveau = f"Niveau {m.group(1)}"
    m = re.search(r"(?:^|[_\- ])(?:S|SEM)(\d{1,2})(?=[_\- .]|$)", name)
    if m:
        h.semestre = int(m.group(1))
    m = re.search(r"(?:^|[_\- ])M(?:ASTER)?\s*_?([12])(?=[_\- .]|$)", name)
    if m and "MASTER" in up:
        h.niveau = f"Master {m.group(1)}"
    return h


# Juste après le libellé « Matricule : », le texte d'une zone voisine peut être collé (« 98EM103Registration N° »).
_LABELED_MAT_RE = re.compile(r"(\d{2})\s*[-/. ]?\s*([A-Za-z]{1,4})\s*[-/. ]?\s*(\d{2,4})(?!\d)(?=[A-Z][a-z])")


def _labeled_matricules(text: str) -> list[tuple[str, str, int]]:
    """Matricules précédés du libellé « Matricule » : (normalisé, brut, position du libellé)."""
    out = []
    for lab_m in PATTERNS["matricule_label"].finditer(text):
        m = MATRICULE_RE.match(text, lab_m.end()) or _LABELED_MAT_RE.match(text, lab_m.end())
        if m:
            mat = f"{m.group(1)}{m.group(2).upper()}{m.group(3)}"
            if not any(o[0] == mat for o in out):
                out.append((mat, m.group(0), lab_m.start()))
    return out


def _header_matricule(text: str) -> tuple[str, str] | None:
    """À défaut de libellé : premier matricule dans les 15 premières lignes de la page."""
    head = "\n".join(text.splitlines()[:15])
    mats = find_matricules(head)
    if not mats:
        return None
    m = MATRICULE_RE.search(head)
    return mats[0], (m.group(0) if m else mats[0])


def _has_student_header(text: str) -> bool:
    return bool(PATTERNS["nom_complet"].search(text) or PATTERNS["nom_seul"].search(text[:1500]))


def split_blocks(pages: list[PageText]) -> tuple[list[Block], list[str]]:
    """Regroupe les pages en blocs étudiants. Renvoie (blocs, avertissements)."""
    blocks: list[Block] = []
    warnings: list[str] = []
    current: Block | None = None
    any_label = any(_labeled_matricules(p.text or "") for p in pages)

    for page in pages:
        text = page.text or ""
        if not text.strip():
            continue
        labeled = _labeled_matricules(text) if any_label else []
        if not labeled and not any_label:
            hm = _header_matricule(text)
            labeled = [(hm[0], hm[1], 0)] if hm else []

        def attach():
            current.page_fin = page.number
            current.texts.append(text)
            if page.ocr_confidence is not None:
                current.ocr.append(page.ocr_confidence)

        if not labeled:
            if current is not None and not re.search(r"\d{1,2}[,.]\d{2}", text):
                # Page de fin vide (gabarit sans notes) : on ne l'imprime pas avec le relevé
                warnings.append(f"Page {page.number} : page sans notes ignorée (suite vide du relevé "
                                f"{current.matricule}).")
            elif current is not None:    # suite du tableau de notes, sans nouvel en-tête
                attach()
            else:
                warnings.append(f"Page {page.number} : aucun matricule détecté.")
            continue

        if (len(labeled) == 1 and current is not None and labeled[0][0] == current.matricule
                and page.number == current.page_fin + 1 and not _has_student_header(text)):
            attach()                      # même matricule, page de suite sans en-tête complet
            continue

        for i, (mat, brut, pos) in enumerate(labeled):
            end = labeled[i + 1][2] if i + 1 < len(labeled) else len(text)
            start = 0 if i == 0 else pos
            current = Block(mat, brut, page.number, page.number, [text[start:end]])
            if page.ocr_confidence is not None:
                current.ocr.append(page.ocr_confidence)
            blocks.append(current)
    return blocks, warnings


# ------------------------------------------------------------ champs du bloc
_CUT = re.compile(r"\s{3,}|\t|\s(?:" + lab("MATRICULE") + r"|n[ée]\(?e?\)?\s*le|fili[eè]re|niveau|session|"
                  r"ann[ée]e|surname|registration)\b", re.I)


def _clean(value: str) -> str:
    value = _CUT.split(value, maxsplit=1)[0]
    return value.strip(" :;,-– ")


def split_nom_prenom(full: str) -> tuple[str, str]:
    """« TCHOUA NGUEMO Brice » -> (« TCHOUA NGUEMO », « Brice »). Tout en capitales -> (tout, « »)."""
    words = full.split()
    nom, prenom = [], []
    for w in words:
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", w)
        if not prenom and letters and letters.upper() == letters:
            nom.append(w)
        else:
            prenom.append(w)
    if not nom:  # tout en casse mixte : premier mot = nom
        return (words[0].upper(), " ".join(words[1:])) if words else ("", "")
    return " ".join(nom), " ".join(prenom)


def _valid_name(value: str) -> bool:
    letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", value)
    return len(letters) >= 3 and not MATRICULE_RE.fullmatch(value.strip()) and \
        not re.match(r"(?i)\s*(surname|registration|born|at)\b", value)


def extract_name(text: str, matricule_brut: str) -> tuple[str, str, str]:
    m = PATTERNS["nom_complet"].search(text)
    if m:
        full = _clean(m.group(1))
        if not _valid_name(full):
            # Valeur sur la ligne suivante (mise en page en deux lignes)
            after = text[m.end():].splitlines()
            nxt = next((ln for ln in after[:3] if ln.strip() and not re.match(r"(?i)\s*surname", ln)), "")
            full = _clean(nxt)
        if _valid_name(full):
            full = re.sub(r"\s+", " ", full)
            nom, prenom = split_nom_prenom(full)
            return nom, prenom, f"{nom} {prenom}".strip()
    nom_m = PATTERNS["nom_seul"].search(text)
    pre_m = PATTERNS["prenom"].search(text)
    if nom_m:
        nom = _clean(nom_m.group(1))
        prenom = _clean(pre_m.group(1)) if pre_m else ""
        if nom and pre_m and prenom and nom.endswith(prenom):
            nom = nom[: -len(prenom)].strip()
        if _valid_name(nom):
            return nom.upper(), prenom, f"{nom.upper()} {prenom}".strip()
    for line in text.splitlines():  # dernier recours : la ligne du matricule
        if matricule_brut and matricule_brut in line:
            rest = line.replace(matricule_brut, " ")
            rest = re.sub(r"(?i)matricule\s*(?:n[°o])?\s*[:：]?", " ", rest)
            rest = re.sub(r"\s+", " ", rest).strip(" :;-")
            if _valid_name(rest):
                nom, prenom = split_nom_prenom(rest)
                return nom, prenom, f"{nom} {prenom}".strip()
    return "", "", ""


def _statut_from(text: str) -> str | None:
    zone = norm_text(text)
    for key, label in STATUTS:
        if re.search(r"\b" + key, zone):
            return label
    return None


def extract_recap(text: str) -> dict | None:
    """Ligne récapitulative du format récent (niveau, semestre, crédits, moyenne, MGP, grade, décision)."""
    candidates = list(PATTERNS["recap"].finditer(text))
    for m in reversed(candidates):  # la récapitulation est en bas du relevé
        decision = re.sub(r"\s+", " ", m.group("dec")).strip()
        if not re.search(r"(?i)valid|admis|ajourn|exclu|abandon|report|redoubl|suspen|semestre", decision):
            continue
        niv, sem, cred = int(m.group("niv")), m.group("sem"), m.group("cred")
        if cred is None and sem and int(sem) > 14:   # « 7 60 18,40 » : niveau, crédits, moyenne (relevé annuel)
            sem, cred = None, sem
        cred = int(cred) if cred is not None else None
        if niv > 14 or (sem and int(sem) > 14) or (cred is not None and cred > 90):
            continue
        mgp = m.group("mgp1") or m.group("mgp2")
        return {"niveau": niv, "semestre": int(sem) if sem else None, "credits": cred,
                "moyenne": m.group("moy").replace(".", ","), "mgp": mgp.replace(".", ",") if mgp else None,
                "grade": m.group("grade").replace(" ", ""), "decision": decision,
                "annuel": sem is None and (cred is None or cred >= 45 or "SEMESTRES" in norm_text(decision))}
    return None


def detect_statut(text: str) -> tuple[str | None, str | None]:
    """(statut normalisé, décision telle qu'écrite)."""
    recap = extract_recap(text)
    if recap:
        return _statut_from(recap["decision"]), recap["decision"]
    m = PATTERNS["decision"].search(text)
    if m:
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        return _statut_from(raw), raw
    # Ancien format : colonne JURY par UE (VALIDEE / NON VALIDEE)
    jury = [norm_text(a or b) for a, b in PATTERNS["ue_jury"].findall(text)]
    if jury:
        ko = sum(1 for j in jury if j.startswith("NON"))
        if ko:
            return "NON VALIDÉ", f"{ko} UE non validée(s) sur {len(jury)}"
        return "VALIDÉ", f"{len(jury)} UE validées sur {len(jury)}"
    # Pas de libellé : la fin du relevé (là où figure la décision), sans les « Validée » des UE
    zone = re.sub(r"\bNON VALIDEE?S?\b|\bVALIDEE?S?\b", " ", norm_text(text[-600:]))
    for key, label in STATUTS:
        if key.startswith(("NON VALID", "VALID")):
            continue
        if re.search(r"\b" + key, zone):
            return label, label
    return None, None


def detect_filiere(text: str, matricule: str, hints: PathHints) -> tuple[str, str]:
    for pat in ("filiere", "filiere_lache"):
        m = PATTERNS[pat].search(text)
        if m:
            raw = _clean(m.group(1))
            val = norm_text(raw)
            for kw, code in FILIERE_KEYWORDS:
                if kw in val:
                    return code, FILIERES.get(code, raw)
    if re.search(r"\bMASTER\s*\d", norm_text(text[:800])):
        return "MS", FILIERES["MS"]
    code = filiere_code_from_matricule(matricule) or hints.filiere_code or ""
    return code, FILIERES.get(code, code or "")


def detect_niveau(text: str, hints: PathHints, recap: dict | None) -> tuple[str | None, int | None]:
    """(libellé « Niveau 2 » / « Master 1 », numéro de semestre)."""
    sem = None
    sem_from_hint = False  # noqa: F841 (conservé pour lisibilité)
    m = PATTERNS["semestre"].search(text)
    if m:
        sem = roman_or_int(m.group(1))
    if sem is None and recap:
        sem = recap["semestre"]
    if sem is None:
        # Valeur décalée par la mise en page (« Academic Year VI » une ligne au-dessus de « SEMESTRE: »)
        lm = re.search(lab("SEMESTRE") + r"\s*:", text, re.I)
        if lm:
            lines = text.splitlines()
            idx = text[:lm.start()].count("\n")
            window = "\n".join(lines[max(0, idx - 2): idx + 3])
            window = re.sub(lab("SEMESTRE") + r"\s*:", " ", window, flags=re.I)
            window = PATTERNS["niveau"].sub(" ", window)   # ne pas confondre avec « NIVEAU: III »
            for tok in re.findall(r"(?<![A-Za-z])([IVX]{1,4})(?![A-Za-z])", window):
                v = roman_or_int(tok)
                if v and v <= 12:
                    sem = v
                    break
    if sem is None and hints.semestre and not (recap and recap.get("annuel")):
        sem = hints.semestre
    cycle = PATTERNS["cycle"].search(text)
    is_master = bool(cycle and norm_text(cycle.group(1)).startswith("MASTER"))
    n = None
    m = PATTERNS["niveau"].search(text)
    if m:
        n = roman_or_int(m.group(1))
    if n is None and recap:
        n = recap["niveau"]
    if n is None:
        for pat in ("niveau_lache", "niveau_ordinal"):
            m = PATTERNS[pat].search(text)
            if m:
                n = int(m.group(1))
                break
    if n is None:
        m = PATTERNS["master"].search(text[:1200])
        if m:
            return f"Master {m.group(1)}", sem
        if hints.niveau:
            return hints.niveau, sem
        if sem:  # niveau déduit du semestre : S1-S2 -> 1, S3-S4 -> 2...
            n = (sem + 1) // 2
    if n is None:
        return None, sem
    if sem and sem <= 2 and n > 1:
        # « NIVEAU: VI SEMESTRE: I » ou fichier « EM3_S2 » : semestre DANS l'année -> semestre du cursus
        sem = 2 * (n - 1) + sem
    return (f"Master {n}" if is_master else f"Niveau {n}"), sem


def detect_session(text: str, hints: PathHints) -> str | None:
    m = PATTERNS["session"].search(text)
    if m:
        v = norm_text(m.group(1))
        if "RATTRAP" in v:
            return "Rattrapage"
        if "SPECIAL" in v:
            return "Spéciale"
        return "Normale"
    return hints.session


def _date(raw: str) -> str:
    parts = re.split(r"\s*[\\/.-]\s*", raw.strip())
    if len(parts) == 3:
        d, mth, y = parts
        if len(y) == 2:
            y = ("19" if int(y) > 30 else "20") + y
        return f"{int(d):02d}/{int(mth):02d}/{y}"
    return raw


def parse_block(block: Block, hints: PathHints) -> tuple[dict, list[str]]:
    text = block.text
    warnings: list[str] = []
    nom, prenom, full = extract_name(text, block.matricule_brut)
    if not full:
        full = "(nom non détecté)"
        warnings.append(f"{block.matricule} (page {block.page_debut}) : nom non détecté.")
    recap = extract_recap(text)
    code, filiere = detect_filiere(text, block.matricule, hints)
    niveau, semestre = detect_niveau(text, hints, recap)
    am = PATTERNS["annee_label"].search(text) or PATTERNS["annee"].search(text)
    annee = f"{am.group(1)}-{am.group(2)}" if am else hints.annee
    statut, decision = detect_statut(text)
    annuel = bool(recap and recap.get("annuel")) or (semestre is None and bool(PATTERNS["annuel"].search(text)))
    n_num = niveau_number(niveau)
    semestre_fin = semestre
    if annuel and 0 < n_num < 100:
        semestre, semestre_fin = 2 * n_num - 1, 2 * n_num   # relevé annuel : couvre les 2 semestres
    cycle_m = PATTERNS["cycle"].search(text)
    option_m = PATTERNS["option"].search(text)
    moyenne = recap["moyenne"] if recap else None
    if not moyenne:
        mm = PATTERNS["moyenne"].search(text)
        moyenne = mm.group(1).replace(".", ",") if mm else None
    nais = PATTERNS["naissance"].search(text)
    lieu = PATTERNS["lieu"].search(text)
    if not annee:
        warnings.append(f"{block.matricule} (page {block.page_debut}) : année académique non détectée.")
    if not niveau:
        warnings.append(f"{block.matricule} (page {block.page_debut}) : niveau non détecté.")
    if not statut:
        warnings.append(f"{block.matricule} (page {block.page_debut}) : décision du jury non détectée.")
    n = niveau_number(niveau)
    if semestre and not annuel and 0 < n < 100 and not (2 * n - 1 <= semestre <= 2 * n):
        warnings.append(f"{block.matricule} (page {block.page_debut}) : niveau {n} incohérent avec le semestre "
                        f"{semestre} — à vérifier.")
    date_naissance = _date(nais.group(1)) if nais else None
    if date_naissance and date_naissance.endswith("/1900"):
        warnings.append(f"{block.matricule} (page {block.page_debut}) : date de naissance factice ({date_naissance}).")
        date_naissance = None
    elif not nais:
        warnings.append(f"{block.matricule} (page {block.page_debut}) : date de naissance illisible.")
    return {
        "matricule": block.matricule,
        "matricule_brut": block.matricule_brut.strip(),
        "nom": nom,
        "prenom": prenom,
        "nom_complet": full,
        "date_naissance": date_naissance,
        "lieu_naissance": _clean(lieu.group(1)) if lieu else None,
        "filiere": filiere,
        "filiere_code": code,
        "niveau": niveau,
        "niveau_num": niveau_number(niveau),
        "semestre": semestre,
        "semestre_fin": semestre_fin,
        "cycle": cycle_m.group(1).upper() if cycle_m else None,
        "option_filiere": _clean(option_m.group(1)) if option_m else None,
        "annee_academique": annee,
        "session": detect_session(text, hints),
        "statut": statut,
        "decision": decision,
        "moyenne": moyenne,
        "credits": recap["credits"] if recap else None,
        "grade": recap["grade"] if recap else None,
        "mgp": recap["mgp"] if recap else None,
        "numero_page": block.page_debut,
        "page_fin": block.page_fin,
        "ocr_confiance": round(sum(block.ocr) / len(block.ocr), 1) if block.ocr else None,
        "extrait": re.sub(r"\s+", " ", text)[:400],
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
