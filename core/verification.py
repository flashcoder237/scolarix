"""Vérification d'authenticité (cas S3.1) : comparaison du document soumis avec l'original indexé.

Le texte du document soumis est lu directement (PDF texte) ou par OCR (scan, photo). On compare
l'identité, chaque note d'unité d'enseignement, la moyenne et la décision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .config import Settings
from .database import Releve
from .extractors import MIN_TEXT_CHARS, configure_tesseract, ocr_pdf_pages, pdf_pages_text, preprocess_for_ocr
from .normalize import find_matricules, norm_text
from .parsing import detect_statut, extract_recap

GRADE_LINE = re.compile(r"^(?P<label>.*?[A-Za-zÀ-ÿ]{3}.*?)\s+(?:\d{1,2}\s+)?(?P<note>\d{1,2}[,.]\d{1,2})\b")
MOYENNE = re.compile(r"moyenne[^:\n]{0,30}[:：]\s*(\d{1,2}[,.]\d{1,3})", re.I)


@dataclass
class Comparison:
    champ: str
    soumis: str
    original: str
    cle: str = ""        # matricule, nom, annee, ue, moyenne, decision : sert à situer le champ sur les documents
    code: str = ""       # code de l'unité d'enseignement (MED355…), s'il est écrit sur l'original

    @property
    def conforme(self) -> bool:
        return norm_text(self.soumis) == norm_text(self.original)


def read_submitted(path: str | Path, settings: Settings) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        pages = pdf_pages_text(path)
        if sum(len(p.text.strip()) for p in pages) < MIN_TEXT_CHARS * len(pages) and configure_tesseract(settings):
            ocr = ocr_pdf_pages(path, [p.number for p in pages], settings)
            return "\n".join(ocr[p.number].text for p in pages)
        return "\n".join(p.text for p in pages)
    # Image (photo, scan JPG/PNG)
    import pytesseract
    from PIL import Image
    if not configure_tesseract(settings):
        raise RuntimeError("Tesseract est nécessaire pour lire une image.")
    img, _ = preprocess_for_ocr(Image.open(path))
    return pytesseract.image_to_string(img, lang=settings.ocr_lang or "fra", config="--psm 6")


def identify(text: str) -> dict:
    """Ce que dit le document soumis de lui-même : matricule, année académique, niveau et semestre.
    Sert à retrouver l'original sans faire retaper le matricule à l'agent."""
    from .normalize import find_matricules
    from .parsing import PathHints, _header_matricule, _labeled_matricules, detect_niveau, extract_recap
    labeled = _labeled_matricules(text)
    mat = labeled[0][0] if labeled else ((_header_matricule(text) or (None,))[0] or
                                         next(iter(find_matricules(text)), None))
    years = re.search(r"\b((?:19|20)\d\d)\s*[-/]\s*((?:19|20)\d\d)\b", text)
    annee = f"{years.group(1)}-{years.group(2)}" if years else None
    try:
        niveau, semestre = detect_niveau(text, PathHints(), extract_recap(text))
    except Exception:
        niveau, semestre = None, None
    return {"matricule": mat, "annee": annee, "niveau": niveau, "semestre": semestre}


def best_match(rels: list, ident: dict):
    """L'original le plus probable parmi les relevés d'un matricule (même année, même niveau…)."""
    if not rels:
        return None

    def score(r):
        return ((ident.get("annee") is not None and r.annee_academique == ident["annee"]) * 4
                + (ident.get("niveau") is not None and (r.niveau or "") == ident["niveau"]) * 2
                + (ident.get("semestre") is not None and r.semestre == ident["semestre"]) * 1)
    return max(rels, key=lambda r: (score(r), r.annee_academique or ""))


UE_CODE = re.compile(r"^([A-Z]{2,4}\s?\d{2,4})\s+")


def _grades(text: str) -> dict[str, tuple[str, str, str]]:
    """{clé normalisée: (libellé, note, code UE)} pour chaque ligne de note."""
    out: dict[str, tuple[str, str, str]] = {}
    for line in text.splitlines():
        if re.search(r"moyenne", line, re.I):
            continue
        m = GRADE_LINE.match(line.strip())
        if m:
            code = UE_CODE.match(m.group("label"))
            label = UE_CODE.sub("", m.group("label")).strip()
            label = re.sub(r"\s+\d+$", "", label)
            key = norm_text(label)[:28]
            if key and key not in out:
                out[key] = (label, m.group("note").replace(".", ","), code.group(1).replace(" ", "") if code else "")
    return out


def _moyenne(text: str) -> str | None:
    recap = extract_recap(text)
    if recap:
        return recap["moyenne"]
    m = MOYENNE.search(text)
    return m.group(1).replace(".", ",") if m else None


def compare(submitted_text: str, original_text: str, original: Releve) -> list[Comparison]:
    rows: list[Comparison] = []
    sub_mats = find_matricules(submitted_text)
    rows.append(Comparison("Matricule", sub_mats[0] if sub_mats else "—", original.matricule, "matricule"))
    sub_norm = norm_text(submitted_text)
    rows.append(Comparison("Nom et prénom",
                           original.nom_complet if norm_text(original.nom_complet) in sub_norm else "(différent ou illisible)",
                           original.nom_complet, "nom"))
    yr = re.search(r"((?:19|20)\d{2})\s*[-/–]\s*((?:19|20)\d{2})", submitted_text)
    rows.append(Comparison("Année académique", f"{yr.group(1)}-{yr.group(2)}" if yr else "—",
                           original.annee_academique or "—", "annee"))
    g_sub, g_orig = _grades(submitted_text), _grades(original_text)
    for key, (label, note, code) in g_orig.items():
        sub = g_sub.get(key)
        if sub is None:  # tolère une petite différence de libellé (OCR)
            from rapidfuzz import fuzz, process
            best = process.extractOne(key, list(g_sub), scorer=fuzz.ratio, score_cutoff=80)
            sub = g_sub.get(best[0]) if best else None
        rows.append(Comparison(label, sub[1] if sub else "—", note, "ue", code))
    rows.append(Comparison("Moyenne", _moyenne(submitted_text) or "—", _moyenne(original_text) or original.moyenne or "—",
                           "moyenne"))
    rows.append(Comparison("Décision du jury", detect_statut(submitted_text)[0] or "—", original.statut or "—", "decision"))
    return rows


def report_pdf(rows: list[Comparison], original: Releve, dest: str | Path, demandeur: str, reference: str,
               operateur: str) -> Path:
    c = canvas.Canvas(str(dest), pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, h - 55, "UNIVERSITÉ DE DOUALA — FMSP — CELLULE INFORMATIQUE")
    c.setFont("Helvetica-Bold", 17)
    c.drawString(50, h - 90, "Rapport de vérification d'authenticité")
    c.setFont("Helvetica", 10)
    c.drawString(50, h - 112, f"Demandeur : {demandeur or '—'}     Référence : {reference or '—'}")
    c.drawString(50, h - 127, f"Original : {original.nom_complet} · {original.matricule} · {original.niveau} · "
                              f"{original.annee_academique} · {original.pages_label}")
    c.drawString(50, h - 142, f"Établi le {datetime.now():%d/%m/%Y à %H:%M} par {operateur}")
    bad = [r for r in rows if not r.conforme]
    c.setFont("Helvetica-Bold", 12)
    if bad:
        c.setFillColorRGB(0.72, 0.11, 0.11)
    else:
        c.setFillColorRGB(0.08, 0.5, 0.24)
    c.drawString(50, h - 172, f"{len(bad)} divergence(s) détectée(s)" if bad else "Document conforme à l'original")
    c.setFillColorRGB(0, 0, 0)
    y = h - 205
    c.setFont("Helvetica-Bold", 10)
    for x, t in ((50, "Champ"), (250, "Document soumis"), (370, "Original indexé"), (490, "Résultat")):
        c.drawString(x, y, t)
    c.setFont("Helvetica", 10)
    for r in rows:
        y -= 17
        if y < 60:
            c.showPage()
            y = h - 60
            c.setFont("Helvetica", 10)
        c.drawString(50, y, r.champ[:34])
        c.drawString(250, y, r.soumis[:20])
        c.drawString(370, y, r.original[:20])
        c.drawString(490, y, "Conforme" if r.conforme else "DIVERGENT")
    c.save()
    return Path(dest)


# ---------------------------------------------------------------- positions sur les documents
# L'écran de vérification surligne chaque champ comparé sur les deux documents et y amène les deux volets.
# Positions en fractions de la page (origine en haut à gauche) : indépendantes du zoom de l'affichage.

@dataclass
class Ligne:
    page: int            # numéro de page dans le fichier affiché (1 = première)
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    def boite(self) -> dict:
        r = lambda v: round(max(0.0, min(1.0, v)), 4)  # noqa: E731
        return {"page": self.page, "x0": r(self.x0), "y0": r(self.y0), "x1": r(self.x1), "y1": r(self.y1)}


@dataclass
class Lecture:
    """Le document soumis tel que lu : texte, lignes situées (si possible), nature et confiance OCR."""
    text: str
    lignes: list[Ligne]
    nature: str           # « PDF texte », « PDF scanné », « Image »
    pages: int
    ocr: float | None = None

    def info(self) -> dict:
        return {"nature": self.nature, "pages": self.pages, "ocr": self.ocr, "positions": bool(self.lignes)}


def _group_lines(items: list[tuple[int, float, float, float, float, str]]) -> list[Ligne]:
    """Regroupe des morceaux de texte situés (page, x0, y0, x1, y1, texte) en lignes : même page, même hauteur ;
    dans une ligne, les morceaux sont remis de gauche à droite (cellules d'un tableau Word)."""
    groups: list[list[tuple[int, float, float, float, float, str]]] = []
    for it in sorted((t for t in items if t[5].strip()), key=lambda t: (t[0], (t[2] + t[4]) / 2, t[1])):
        page, _x0, y0, _x1, y1, _t = it
        cy = (y0 + y1) / 2
        g = groups[-1] if groups else None
        if g and g[0][0] == page:
            gy0, gy1 = min(p[2] for p in g), max(p[4] for p in g)
            if gy0 <= cy <= gy1 and abs((gy0 + gy1) / 2 - cy) < max(0.006, (y1 - y0) * 0.6):
                g.append(it)
                continue
        groups.append([it])
    out = []
    for g in groups:
        g.sort(key=lambda p: p[1])
        out.append(Ligne(page=g[0][0], text=re.sub(r"\s+", " ", " ".join(p[5].strip() for p in g)),
                         x0=min(p[1] for p in g), y0=min(p[2] for p in g), x1=max(p[3] for p in g), y1=max(p[4] for p in g)))
    return out


def pdf_lines(path: str | Path, first: int = 1, last: int | None = None) -> list[Ligne]:
    """Lignes d'un PDF texte, situées (PDFium : rectangles de texte de la couche texte)."""
    from .extractors import open_pdf
    items: list[tuple[int, float, float, float, float, str]] = []
    try:
        doc = open_pdf(Path(path))
    except Exception:
        return []
    try:
        n = len(doc)
        for num in range(max(1, first), min(n, last or n) + 1):
            page = doc[num - 1]
            try:
                w, h = page.get_size()
                tp = page.get_textpage()
                try:
                    rects = [tp.get_rect(i) for i in range(tp.count_rects())]
                    rects.sort(key=lambda r: (-(r[1] + r[3]) / 2, r[0]))
                    for left, bottom, right, top in rects:
                        text = (tp.get_text_bounded(left, bottom, right, top) or "").replace("\r", " ").replace("\n", " ")
                        items.append((num, left / w, (h - top) / h, right / w, (h - bottom) / h, text))
                finally:
                    tp.close()
            except Exception:
                continue
            finally:
                page.close()
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return _group_lines(items)


def _unrotate(x: float, y: float, w: int, h: int, w0: int, h0: int, angle: float) -> tuple[float, float]:
    """Point de l'image redressée (PIL rotate(angle, expand=True)) ramené dans l'image d'origine."""
    import math
    t = math.radians(angle)
    dx, dy = x - w / 2, y - h / 2
    ox = dx * math.cos(t) - dy * math.sin(t)
    oy = dx * math.sin(t) + dy * math.cos(t)
    return ox + w0 / 2, oy + h0 / 2


def _ocr_image(img, settings: Settings, page: int) -> tuple[str, list[Ligne], float | None]:
    """OCR d'une image avec les boîtes des mots (Tesseract image_to_data) : texte, lignes situées, confiance."""
    import pytesseract
    w0, h0 = img.size
    angle = 0.0
    if settings.ocr_preprocess:
        img, angle = preprocess_for_ocr(img)
    w, h = img.size
    data = pytesseract.image_to_data(img, lang=settings.ocr_lang or "fra", config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    groups: dict[tuple, list[int]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        if not str(word).strip():
            continue
        groups.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
        try:
            c = float(data["conf"][i])
            if c >= 0:
                confs.append(c)
        except (TypeError, ValueError):
            pass
    texts, lignes = [], []
    for _key, idx in sorted(groups.items()):
        words = " ".join(str(data["text"][i]) for i in idx)
        texts.append(words)
        pts = []
        for i in idx:
            x, y, bw, bh = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            for px, py in ((x, y), (x + bw, y), (x, y + bh), (x + bw, y + bh)):
                pts.append(_unrotate(px, py, w, h, w0, h0, angle) if abs(angle) >= 0.01 else (px, py))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        lignes.append(Ligne(page=page, text=words, x0=min(xs) / w0, y0=min(ys) / h0, x1=max(xs) / w0, y1=max(ys) / h0))
    return "\n".join(texts), lignes, (round(sum(confs) / len(confs), 1) if confs else None)


def read_layout(path: str | Path, settings: Settings) -> Lecture:
    """Comme read_submitted, avec en plus les lignes situées : couche texte d'un PDF, boîtes OCR d'un scan."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        pages = pdf_pages_text(path)
        if sum(len(p.text.strip()) for p in pages) < MIN_TEXT_CHARS * len(pages) and configure_tesseract(settings):
            from .extractors import open_pdf
            texts, lignes, confs = [], [], []
            doc = open_pdf(path)
            try:
                for p in pages:
                    img = doc[p.number - 1].render(scale=300 / 72).to_pil()
                    t, ls, c = _ocr_image(img, settings, p.number)
                    texts.append(t)
                    lignes += ls
                    if c is not None:
                        confs.append(c)
            finally:
                doc.close()
            return Lecture("\n".join(texts), lignes, "PDF scanné", len(pages),
                           round(sum(confs) / len(confs), 1) if confs else None)
        return Lecture("\n".join(p.text for p in pages), pdf_lines(path), "PDF texte", len(pages))
    from PIL import Image, ImageOps
    if not configure_tesseract(settings):
        raise RuntimeError("Tesseract est nécessaire pour lire une image.")
    with Image.open(path) as im:
        img = ImageOps.exif_transpose(im).convert("RGB")      # photo de téléphone : sens réel de la prise de vue
    text, lignes, conf = _ocr_image(img, settings, 1)
    return Lecture(text, lignes, "Image", 1, conf)


_VAL = lambda v: (v or "").replace(".", ",").strip()  # noqa: E731
RX_LABEL = {
    "matricule": re.compile(r"\bMATRICULE\b"),
    "nom": re.compile(r"\bNOMS?\b.*\bPRENOMS?\b|^\s*NOMS?\b"),
    "annee": re.compile(r"\bANNEE\s+ACADEMIQUE\b|\bACADEMIC\s+YEAR\b"),
    "moyenne": re.compile(r"\bMOYENNE\b"),
    "decision": re.compile(r"\bDECISION\b"),
}


def _find_line(row: Comparison, value: str, lignes: list[Ligne]) -> Ligne | None:
    """La ligne du document qui porte ce champ (valeur et/ou libellé)."""
    if not lignes:
        return None
    norms = [norm_text(l.text) for l in lignes]
    raw = [l.text.replace(".", ",") for l in lignes]
    val = _VAL(value)
    has_val = bool(val) and val not in ("—", "(différent ou illisible)")
    cle = row.cle
    if cle == "ue":
        from rapidfuzz import fuzz
        key = norm_text(row.champ)[:24]
        best, best_score = None, 0.0
        for i, n in enumerate(norms):
            if RX_LABEL["moyenne"].search(n):
                continue
            score = fuzz.partial_ratio(key, n) if key else 0
            if row.code and row.code in n.replace(" ", ""):
                score += 40
            if has_val and re.search(r"(?<![\d,])" + re.escape(val) + r"(?![\d])", raw[i]):
                score += 15
            if score > best_score:
                best, best_score = lignes[i], score
        return best if best_score >= 80 else None
    if cle == "matricule":
        if has_val:
            v = norm_text(val).replace(" ", "")
            for i, n in enumerate(norms):
                if v and v in n.replace(" ", ""):
                    return lignes[i]
        return next((lignes[i] for i, n in enumerate(norms) if RX_LABEL["matricule"].search(n)), None)
    if cle == "nom":
        if has_val:
            v = norm_text(val)
            for i, n in enumerate(norms):
                if v and v in n:
                    return lignes[i]
        return next((lignes[i] for i, n in enumerate(norms) if RX_LABEL["nom"].search(n)), None)
    if cle == "annee":
        if has_val:
            m = re.match(r"((?:19|20)\d\d)\D+((?:19|20)\d\d)", val)
            if m:
                rx = re.compile(m.group(1) + r"\s*[-/–]\s*" + m.group(2))
                for i, l in enumerate(lignes):
                    if rx.search(l.text):
                        return l
        return next((lignes[i] for i, n in enumerate(norms) if RX_LABEL["annee"].search(n)), None)
    if cle == "moyenne":
        cands = [i for i, n in enumerate(norms) if RX_LABEL["moyenne"].search(n)]
        if has_val:
            with_val = [i for i in cands if val in raw[i]] or [i for i, r in enumerate(raw) if val in r and i not in cands and
                                                               re.search(r"(?<![\d,])" + re.escape(val) + r"(?![\d])", r)]
            if with_val:
                return lignes[with_val[-1]]
        pref = [i for i in cands if re.search(r"ANNUELLE|GENERALE", norms[i])]
        return lignes[(pref or cands)[-1]] if (pref or cands) else None
    if cle == "decision":
        cands = [i for i, n in enumerate(norms) if RX_LABEL["decision"].search(n)]
        if cands:
            return lignes[cands[-1]]
        if has_val:
            v = norm_text(val)
            hits = [i for i, n in enumerate(norms) if v and re.search(r"\b" + re.escape(v) + r"\b", n)]
            return lignes[hits[-1]] if hits else None
    return None


def locate(rows: list[Comparison], soumis: list[Ligne], original: list[Ligne]) -> list[dict]:
    """Pour chaque champ comparé : sa ligne sur le document soumis et sur l'original (ou None)."""
    out = []
    for r in rows:
        s = _find_line(r, r.soumis, soumis) if soumis else None
        o = _find_line(r, r.original, original) if original else None
        out.append({"soumis": s.boite() if s else None, "original": o.boite() if o else None})
    return out
