"""Découpe, fusion et filigrane des PDF — toujours dans des fichiers temporaires ou d'export,
jamais dans le dossier des archives (l'original reste intact sur le disque)."""
from __future__ import annotations

import io
import re
import tempfile
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .database import Releve
from .normalize import norm_text


def export_filename(rel: Releve, suffix: str = "") -> str:
    nom = re.sub(r"[^A-Z0-9]", "", (norm_text(rel.nom or rel.nom_complet).split() or ["ETUDIANT"])[0])
    annee = (rel.annee_academique or "").split("-")[-1] or "NA"
    niv = f"Niv{rel.niveau_num}" if rel.niveau_num and rel.niveau_num < 100 else \
        (rel.niveau or "").replace(" ", "") or "NivNA"
    sem = f"_S{rel.semestre}" if rel.semestre else ""
    sess = "_Rattrapage" if (rel.session or "").lower().startswith("rattrap") else ""
    return f"FMSP_{rel.matricule}_{nom}_{annee}_{niv}{sem}{sess}{suffix}.pdf"


def _stamp_page(width: float, height: float, title: str, subtitle: str):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.saveState()
    c.translate(width / 2, height / 2)
    c.rotate(30)
    red = Color(0.72, 0.11, 0.11, alpha=0.32)
    c.setFillColor(red)
    c.setStrokeColor(red)
    size = min(width, height) / 13
    c.setFont("Helvetica-Bold", size)
    tw = c.stringWidth(title, "Helvetica-Bold", size)
    c.setLineWidth(3)
    c.roundRect(-tw / 2 - 18, -size * 1.15, tw + 36, size * 2.35, 10, stroke=1, fill=0)
    c.drawCentredString(0, size * 0.35, title)
    c.setFont("Helvetica-Bold", size * 0.36)
    c.drawCentredString(0, -size * 0.65, subtitle)
    c.restoreState()
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def stamp_text(kind: str = "DUPLICATA CONFORME") -> tuple[str, str]:
    return kind, f"Cellule Informatique FMSP · {datetime.now():%d/%m/%Y %H:%M}"


def extract_pages(src_pdf: str | Path, first: int, last: int, dest: str | Path | None = None,
                  stamp: tuple[str, str] | None = None) -> Path:
    """Copie les pages [first, last] (1-based) dans un nouveau PDF, avec timbre optionnel."""
    reader = PdfReader(str(src_pdf))
    writer = PdfWriter()
    last = min(last, len(reader.pages))
    for i in range(first - 1, last):
        page = writer.add_page(reader.pages[i])
        if stamp:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            page.merge_page(_stamp_page(w, h, *stamp))
    if dest is None:
        fd = tempfile.NamedTemporaryFile(prefix="fmsp_", suffix=".pdf", delete=False)
        fd.close()
        dest = fd.name
    with open(dest, "wb") as fh:
        writer.write(fh)
    return Path(dest)


def _cover_page(rels: list[Releve], title: str) -> PdfReader:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, h - 60, "UNIVERSITÉ DE DOUALA — FACULTÉ DE MÉDECINE ET DES SCIENCES PHARMACEUTIQUES")
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 100, title)
    first = rels[0]
    c.setFont("Helvetica", 11)
    c.drawString(50, h - 128, f"{first.nom_complet}   ·   matricule(s) : {', '.join(sorted({r.matricule for r in rels}))}")
    c.drawString(50, h - 146, f"Édité le {datetime.now():%d/%m/%Y à %H:%M} par la Cellule Informatique")
    y = h - 190
    c.setFont("Helvetica-Bold", 10)
    for x, label in ((50, "#"), (72, "Niveau · semestre"), (180, "Année"), (250, "Moyenne"), (310, "Crédits"),
                     (360, "Décision du jury"), (500, "Pages")):
        c.drawString(x, y, label)
    c.setFont("Helvetica", 9.5)
    for i, r in enumerate(rels, start=1):
        y -= 17
        if y < 60:
            c.showPage()
            y = h - 60
            c.setFont("Helvetica", 9.5)
        c.drawString(50, y, str(i))
        c.drawString(72, y, r.periode)
        c.drawString(180, y, r.annee_academique or "?")
        c.drawString(250, y, r.moyenne or "—")
        c.drawString(310, y, str(r.credits) if r.credits is not None else "—")
        c.drawString(360, y, (r.decision or r.statut or "?")[:26])
        c.drawString(500, y, r.pages_label.replace("Pages ", "p. ").replace("Page ", "p. "))
    c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def merge_releves(rels: list[Releve], dest: str | Path, cover: bool = True,
                  stamp: tuple[str, str] | None = None, title: str = "Cursus académique") -> tuple[Path, int, list[str]]:
    """Fusionne plusieurs relevés (dans l'ordre donné) en un seul PDF. Renvoie (chemin, nb pages, manquants)."""
    writer = PdfWriter()
    missing: list[str] = []
    if cover and rels:
        writer.add_page(_cover_page(rels, title).pages[0])
    readers: dict[str, PdfReader] = {}
    for r in rels:
        src = r.preview_pdf
        if not src or not Path(src).exists():
            missing.append(f"{r.periode} {r.annee_academique}")
            continue
        reader = readers.setdefault(src, PdfReader(src))
        for i in range(r.numero_page - 1, min(r.page_fin, len(reader.pages))):
            page = writer.add_page(reader.pages[i])
            if stamp:
                page.merge_page(_stamp_page(float(page.mediabox.width), float(page.mediabox.height), *stamp))
    with open(dest, "wb") as fh:
        writer.write(fh)
    return Path(dest), len(writer.pages), missing


def select_for_cursus(rels: list[Releve]) -> list[Releve]:
    """Sélection par défaut pour le cursus : pour chaque (niveau, semestre), si un relevé validé existe
    (rattrapage ou redoublement compris), on écarte les relevés non validés de la même période (cas S2.1)
    et on ne garde que le plus récent s'il y a plusieurs exemplaires validés."""
    by_period: dict[tuple, list[Releve]] = {}
    for r in rels:
        by_period.setdefault((r.matricule, r.niveau, r.semestre or 0), []).append(r)
    keep = []
    for group in by_period.values():
        ok = [r for r in group if r.is_success]
        if ok:
            keep.append(max(ok, key=lambda r: (r.annee_academique or "", r.id)))
        else:
            keep.extend(group)
    keep.sort(key=lambda r: (r.niveau_num, r.semestre or 0, r.annee_academique or "",
                             1 if (r.session or "").lower().startswith("rattrap") else 0))
    return keep
