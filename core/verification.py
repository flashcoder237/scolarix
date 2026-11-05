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


def _grades(text: str) -> dict[str, tuple[str, str]]:
    """{clé normalisée: (libellé, note)} pour chaque ligne de note."""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if re.search(r"moyenne", line, re.I):
            continue
        m = GRADE_LINE.match(line.strip())
        if m:
            label = re.sub(r"^[A-Z]{2,4}\s?\d{2,4}\s+", "", m.group("label")).strip()
            label = re.sub(r"\s+\d+$", "", label)
            key = norm_text(label)[:28]
            if key and key not in out:
                out[key] = (label, m.group("note").replace(".", ","))
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
    rows.append(Comparison("Matricule", sub_mats[0] if sub_mats else "—", original.matricule))
    sub_norm = norm_text(submitted_text)
    rows.append(Comparison("Nom et prénom",
                           original.nom_complet if norm_text(original.nom_complet) in sub_norm else "(différent ou illisible)",
                           original.nom_complet))
    yr = re.search(r"((?:19|20)\d{2})\s*[-/–]\s*((?:19|20)\d{2})", submitted_text)
    rows.append(Comparison("Année académique", f"{yr.group(1)}-{yr.group(2)}" if yr else "—",
                           original.annee_academique or "—"))
    g_sub, g_orig = _grades(submitted_text), _grades(original_text)
    for key, (label, note) in g_orig.items():
        sub = g_sub.get(key)
        if sub is None:  # tolère une petite différence de libellé (OCR)
            from rapidfuzz import fuzz, process
            best = process.extractOne(key, list(g_sub), scorer=fuzz.ratio, score_cutoff=80)
            sub = g_sub.get(best[0]) if best else None
        rows.append(Comparison(label, sub[1] if sub else "—", note))
    rows.append(Comparison("Moyenne", _moyenne(submitted_text) or "—", _moyenne(original_text) or original.moyenne or "—"))
    rows.append(Comparison("Décision du jury", detect_statut(submitted_text)[0] or "—", original.statut or "—"))
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
